#!/usr/bin/env python3
"""Tier 2 — доставка проактивных сообщений MOTUS.

Опрашивает `GET /initiate/pending` (публичный API MOTUS). Если движок решил
инициировать контакт (гейт открыт, бюджет позволяет, тишина достаточно долгая —
всё это уже проверено ядром и `daemon.py:/initiate/pending`, здесь не
перепроверяется), просит openclaw сделать ОДИН обычный ход через реальный
гейтвей (`openclaw agent --deliver`, НЕ `agent exec --isolated` — это не
фоновая задача Tier 1, а настоящее сообщение в канал, с памятью и историей
сессии). Сам текст сообщения пишет модель, а не этот скрипт: сюда идёт только
`card.text` как контекст-нудж, формулировку выбирает модель в пределах того,
что разрешает `gate` (те же ограничения увидит и `message_sending`-хук плагина
на отправке — двойная защита, не замена одной другой).

Успех / провал доставки НЕ подтверждается отдельным вызовом в MOTUS: состояние
`initiation_pending` само снимется либо `note_answer()` (пользователь ответил),
либо `check_unanswered()` по таймауту (`budget.unanswered_after_s`) — это уже
реализовано в движке. Единственное, что обязан делать исполнитель сам, —
`POST /refund`, если доставка НЕ СОСТОЯЛАСЬ (сбой канала, а не молчание
пользователя): иначе токен бюджета списан впустую за сообщение, которое
никуда не ушло.

Запускается по таймеру (`deploy/motus-tier2.timer`) ВНУТРИ grach, от
пользователя `openclaw` (нужны его креды и рабочий гейтвей) — как и Tier 1.

Требует конфигурации получателя (кому проактивно писать) — единственное, что
скрипт не может знать сам:
    MOTUS_TIER2_SESSION_KEY=agent:main:telegram:direct:<id>   (точнее всего)
  или
    MOTUS_TIER2_TO=+1555...  MOTUS_TIER2_CHANNEL=telegram

Только stdlib.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DEFAULT_MOTUSD = "http://127.0.0.1:18790"
DEFAULT_STATE_DIR = Path(os.environ.get("MOTUS_TIER2_STATE", "/var/lib/motus-tier2"))
DEFAULT_OPENCLAW = os.environ.get(
    "MOTUS_TIER2_OPENCLAW", "/home/openclaw/.npm-global/bin/openclaw"
)

#: Нудж поверх card.text: явно называет, что это самостоятельная инициация,
#: не ответ — иначе модель может решить, что отвечает на несуществующее
#: сообщение пользователя.
PROMPT_TEMPLATE = """[Внутренний повод: ты можешь сейчас написать первой, не дожидаясь вопроса.]
{card_text}

Если есть что сказать в этих рамках — напиши коротко, по-человечески, без
предисловий вида "я решила написать, потому что...". Если сказать нечего —
не пиши длинно ради галочки, хватит одной фразы или дружелюбного слова.
"""


def _get(url: str, timeout: float = 5.0) -> Dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(url: str, body: Dict[str, Any], timeout: float = 5.0) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_command(openclaw: str, session_key: Optional[str], to: Optional[str],
                  channel: Optional[str], prompt_file: Path, timeout_s: int) -> list:
    cmd = [openclaw, "agent", "--deliver", "--json",
           "--message-file", str(prompt_file), "--timeout", str(timeout_s)]
    if session_key:
        cmd += ["--session-key", session_key]
    elif to:
        cmd += ["--to", to]
        if channel:
            cmd += ["--channel", channel]
    return cmd


def run_openclaw(cmd: list, timeout_s: int) -> Tuple[bool, Dict[str, Any]]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + 60)
    except subprocess.TimeoutExpired:
        return False, {"error": "subprocess_timeout"}
    except OSError as exc:
        return False, {"error": f"spawn_failed: {exc}"}

    env: Dict[str, Any] = {}
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                env = json.loads(line)
                break
            except ValueError:
                continue
    is_error = bool(
        env.get("isError") or env.get("error") or (env.get("ok") is False)
        or proc.returncode != 0
    )
    return (not is_error), env


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--motusd", default=os.environ.get("MOTUS_TIER2_MOTUSD", DEFAULT_MOTUSD))
    ap.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    ap.add_argument("--openclaw", default=DEFAULT_OPENCLAW)
    ap.add_argument("--session-key", default=os.environ.get("MOTUS_TIER2_SESSION_KEY") or None)
    ap.add_argument("--to", default=os.environ.get("MOTUS_TIER2_TO") or None)
    ap.add_argument("--channel", default=os.environ.get("MOTUS_TIER2_CHANNEL") or None)
    ap.add_argument("--timeout", type=int,
                    default=int(os.environ.get("MOTUS_TIER2_TIMEOUT", "180")))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not args.dry_run and not (args.session_key or args.to):
        print("tier2: не задан получатель (MOTUS_TIER2_SESSION_KEY или "
              "MOTUS_TIER2_TO) — некому писать, выходим", file=sys.stderr)
        return 2

    args.state_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(args.state_dir / ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("tier2: предыдущий запуск ещё идёт — выходим", file=sys.stderr)
        return 0
    try:
        return _run(args)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _run(args) -> int:
    base = args.motusd.rstrip("/")
    try:
        payload = _get(f"{base}/initiate/pending")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"tier2: MOTUS недоступен ({exc})", file=sys.stderr)
        return 0

    if not payload.get("pending"):
        return 0

    card_text = (payload.get("card") or {}).get("text", "")
    prompt = PROMPT_TEMPLATE.format(card_text=card_text)
    msg_file = args.state_dir / "initiate-prompt.txt"
    msg_file.write_text(prompt, encoding="utf-8")

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("--- PROMPT ---\n" + prompt)
        return 0

    cmd = build_command(args.openclaw, args.session_key, args.to, args.channel,
                        msg_file, args.timeout)
    ok, env = run_openclaw(cmd, args.timeout)
    print(f"tier2: инициация regime={payload.get('gate', {}).get('regime')} "
          f"ok={ok} {json.dumps(env, ensure_ascii=False)[:200]}", file=sys.stderr)

    if not ok:
        # Канал не сработал — вернуть токен, а не списывать за несостоявшееся
        # сообщение. Совпадает с docstring engine.refund_initiation().
        try:
            _post(f"{base}/refund", {})
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            print(f"tier2: /refund тоже не прошёл ({exc})", file=sys.stderr)
            return 1
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
