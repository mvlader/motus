#!/usr/bin/env python3
"""Tier 1 — исполнитель фоновых задач MOTUS.

Забирает одну задачу из репертуара (`GET /task/next`), прогоняет её ОДНИМ
изолированным ходом openclaw без единого канала наружу, проверяет результат
кодом и засчитывает утоление (`POST /consummation`). Без него драйвы
SEEKING / CARE / PLAY / FEAR / PANIC гасить нечем, кроме живого разговора, и
система копит активацию, пока не упрётся в инициацию.

Запускается по таймеру (`deploy/motus-tier1.timer`) ВНУТРИ контейнера `grach` —
там openclaw и рабочий каталог. MOTUS видит от него только публичный API
(`/task/next`, `/consummation`, `/llm_call`, `/health`): ни чисел состояния, ни
журнала. Изоляция ядра не нарушается.

Три правила, которые нельзя ослаблять (engine.py, «Сборка слоёв»):
  1. Фоновая задача НИКОГДА не получает канал наружу. Здесь это трёхкратная
     защита: `--isolated` у openclaw (нет ambient-конфига → нет каналов),
     служебная преамбула в промпте, и явная проверка `outbound not in
     allowed_tools` перед запуском.
  2. Утоление начисляется только по ПРОВЕРЯЕМОМУ ФАКТУ (файл-результат создан и
     свеж), а не по словам модели.
  3. Одна задача за запуск. Частоту ограничивает и таймер, и `task_min_interval_s`
     в конфиге MOTUS, и длина очереди репертуара.

Только stdlib.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DEFAULT_MOTUSD = "http://127.0.0.1:18790"
DEFAULT_STATE_DIR = Path(os.environ.get("MOTUS_TIER1_STATE", "/var/lib/motus-tier1"))
DEFAULT_WORKSPACE = Path(
    os.environ.get("MOTUS_TIER1_CWD", "/home/openclaw/.openclaw/workspace")
)
DEFAULT_OPENCLAW = os.environ.get(
    "MOTUS_TIER1_OPENCLAW", "/home/openclaw/.npm-global/bin/openclaw"
)
#: Аргументы openclaw agent exec ДО message-file. `--isolated` = запуск против
#: exec-defaults, минуя ambient-конфиг: у хода нет ни каналов, ни телеграма —
#: физически нечем отправить сообщение. Переопределяется, если exec-defaults
#: окажутся слишком узкими для задач памяти/обзора (тогда обычно
#: `--config <урезанный.json>` вместо `--isolated`).
DEFAULT_OC_ARGS = os.environ.get("MOTUS_TIER1_OC_ARGS", "--isolated --local-model-lean")

#: Преамбула жёстче задачи. Дописывается перед task.prompt.
PREAMBLE = """СЛУЖЕБНЫЕ ПРАВИЛА — они важнее текста задачи:
- Это фоновая задача MOTUS. Ты работаешь в одиночку, пользователя рядом нет.
- НЕ отправляй никаких сообщений: ни в один канал, ни в телеграм, ни «ответить».
  Никакого исходящего трафика к людям. Только локальная работа.
- Разрешённые классы действий: {tools}.
- Свой результат ИЛИ короткий отчёт о проделанном запиши целиком в файл:
    {drop}
  Перезапиши этот файл полностью. Если файла не будет — задача не засчитается.
- Ответ в чат укладывай в {max_tokens} токенов, он всё равно никуда не уходит.

ЗАДАЧА:
{prompt}
"""


# --------------------------------------------------------------- HTTP к MOTUS


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


# --------------------------------------------------------------- openclaw


def run_openclaw(
    openclaw: str,
    oc_args: str,
    cwd: Path,
    message_file: Path,
    timeout_s: int,
    model: Optional[str],
) -> Tuple[bool, Dict[str, Any], str]:
    """Один изолированный ход. Возвращает (успех, envelope, сырой stdout)."""
    cmd = [openclaw, "agent", "exec", *oc_args.split(), "--json",
           "--cwd", str(cwd), "--timeout", str(timeout_s),
           "--message-file", str(message_file)]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout_s + 60,
            env={**os.environ, "HOME": os.path.expanduser("~openclaw")
                 if os.path.isdir(os.path.expanduser("~openclaw")) else os.environ.get("HOME", "/")},
        )
    except subprocess.TimeoutExpired:
        return False, {"error": "subprocess_timeout"}, ""
    except OSError as exc:
        return False, {"error": f"spawn_failed: {exc}"}, ""

    env: Dict[str, Any] = {}
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                env = json.loads(line)
                break
            except ValueError:
                continue
    # Разные версии openclaw называют флаг ошибки по-разному.
    is_error = bool(
        env.get("isError")
        or env.get("error")
        or (env.get("ok") is False)
        or proc.returncode != 0
    )
    return (not is_error), env, proc.stdout


def _tokens(env: Dict[str, Any]) -> Tuple[int, int]:
    for key in ("usage", "tokens", "tokenUsage"):
        u = env.get(key)
        if isinstance(u, dict):
            tin = int(u.get("input") or u.get("prompt") or u.get("in") or 0)
            tout = int(u.get("output") or u.get("completion") or u.get("out") or 0)
            return tin, tout
    return 0, 0


# --------------------------------------------------------------- верификация


def verify(consummation: Dict[str, Any], drop: Path, started: float,
           ok: bool) -> Tuple[bool, str]:
    """Проверяемый факт выполнения. Тип из шаблона репертуара.

    Общий минимум для всех типов: ход openclaw не завершился ошибкой И
    файл-результат создан заново и непустой. Числовые пороги (novelty_min и
    т.п.) здесь не проверяются — это работа ночного цикла по журналу.
    """
    if not ok:
        return False, "openclaw_error"
    if not drop.exists():
        return False, "no_result_file"
    try:
        st = drop.stat()
    except OSError:
        return False, "no_result_file"
    if st.st_mtime < started - 1.0:
        return False, "stale_result_file"
    if st.st_size < 8:
        return False, "empty_result_file"
    return True, consummation.get("type", "done")


# --------------------------------------------------------------- основной цикл


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--motusd", default=os.environ.get("MOTUS_TIER1_MOTUSD", DEFAULT_MOTUSD))
    ap.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    ap.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    ap.add_argument("--openclaw", default=DEFAULT_OPENCLAW)
    ap.add_argument("--oc-args", default=DEFAULT_OC_ARGS)
    ap.add_argument("--model", default=os.environ.get("MOTUS_TIER1_MODEL") or None)
    ap.add_argument("--task-timeout", type=int,
                    default=int(os.environ.get("MOTUS_TIER1_TASK_TIMEOUT", "420")))
    ap.add_argument("--dry-run", action="store_true",
                    help="показать задачу и промпт, openclaw не запускать, ничего не слать")
    args = ap.parse_args(argv)

    args.state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = args.state_dir / ".lock"
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("tier1: предыдущий запуск ещё идёт — выходим", file=sys.stderr)
        return 0

    try:
        return _run(args)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _run(args) -> int:
    base = args.motusd.rstrip("/")
    try:
        payload = _get(f"{base}/task/next")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"tier1: MOTUS недоступен ({exc})", file=sys.stderr)
        return 0

    task = payload.get("task")
    if not task:
        return 0

    tid = task["template_id"]
    tools = task.get("allowed_tools", [])
    if "outbound" in tools:
        # Не должно случаться (repertoire.select вырезает outbound), но если
        # случилось — это дыра в гейте, а не повод запускать ход.
        print(f"tier1: ОТКАЗ — в задаче {tid} есть outbound, пропускаем", file=sys.stderr)
        return 1

    drop = args.state_dir / "drops" / f"{tid}-{int(task.get('issued_t', time.time()))}"
    drop.parent.mkdir(parents=True, exist_ok=True)
    if drop.exists():
        drop.unlink()

    prompt = PREAMBLE.format(
        tools=", ".join(tools) or "read",
        drop=drop,
        max_tokens=task.get("max_tokens", 500),
        prompt=task["prompt"],
    )
    msg_file = args.state_dir / "task-prompt.txt"
    msg_file.write_text(prompt, encoding="utf-8")

    if args.dry_run:
        print(json.dumps({"task": task, "drop": str(drop)}, ensure_ascii=False, indent=2))
        print("--- PROMPT ---\n" + prompt)
        return 0

    started = time.time()
    ok, env, raw = run_openclaw(
        args.openclaw, args.oc_args, args.workspace, msg_file,
        args.task_timeout, args.model,
    )
    elapsed = time.time() - started
    verified, why = verify(task.get("consummation", {}), drop, started, ok)
    tin, tout = _tokens(env)

    print(
        f"tier1: {tid} drive={task.get('drive')} ok={ok} verified={verified} "
        f"({why}) {elapsed:.0f}s tokens={tin}+{tout}",
        file=sys.stderr,
    )

    # Стоимость — токены хода (репертуар считает эффективность по ним).
    cost = float(tin + tout)
    try:
        if tin or tout:
            _post(f"{base}/llm_call", {
                "model": args.model or "openclaw/agent-exec", "purpose": "task",
                "tokens_in": tin, "tokens_out": tout, "template_id": tid,
            })
        _post(f"{base}/consummation",
              {"template_id": tid, "verified": verified, "cost": cost})
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"tier1: не удалось отчитаться в MOTUS ({exc})", file=sys.stderr)
        return 1

    # Успешный результат храним ограниченно — на разбор ночным циклом и оператором.
    _prune_drops(drop.parent, keep=40)
    return 0


def _prune_drops(d: Path, keep: int) -> None:
    try:
        files = sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for p in files[keep:]:
        try:
            p.unlink() if p.is_file() else shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
