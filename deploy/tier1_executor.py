#!/usr/bin/env python3
"""Tier 1 — исполнитель консумматорных актов MOTUS.

Не таск-раннер для пользователя — то, чем личность занимается сама с собой в
тишине, когда драйв домінирует и разговора нет. Забирает один акт из репертуара
(`GET /task/next`), прогоняет его ОДНИМ изолированным ходом openclaw без
единого канала наружу, проверяет факт исполнения кодом и засчитывает утоление
(`POST /consummation`). Без него SEEKING/CARE/PLAY/FEAR/PANIC гасить нечем,
кроме живого разговора, и система копит активацию, пока не упрётся в инициацию.

Запускается по таймеру (`deploy/motus-tier1.timer`) ВНУТРИ контейнера `grach` —
там openclaw и рабочий каталог. MOTUS видит от него только публичный API
(`/task/next`, `/consummation`, `/llm_call`, `/health`): ни чисел состояния, ни
журнала. Изоляция ядра не нарушается.

Три правила, которые нельзя ослаблять (engine.py, «Сборка слоёв»):
  1. Фоновый акт НИКОГДА не получает канал наружу. Здесь это трёхкратная
     защита: `--isolated` у openclaw (нет ambient-конфига → нет каналов),
     служебная преамбула в промпте, и явная проверка `outbound not in
     allowed_tools` перед запуском.
  2. Утоление начисляется только по ПРОВЕРЯЕМОМУ ФАКТУ, а не по словам модели —
     но проверяемый факт не обязан быть артефактом. Большинство типов
     консумации (`memory_entry`/`artifact_created`/`check_passed`) требуют файл
     заново созданным и свежим; `reflection` (2026-09-11) требует только, чтобы
     ход состоялся без ошибки — не всё, чем личность занимается сама с собой,
     обязано превращаться в отчёт. См. config/repertoire.json.
  3. Один акт за запуск. Частоту ограничивает и таймер, и `task_min_interval_s`
     в конфиге MOTUS, и длина очереди репертуара.

Только stdlib.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
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

#: Основной путь с 2026-09-15 — Claude через `claude -p` на подписке: ПК с ollama
#: слишком часто выключен, и фоновые акты просто не случались. openclaw + локальная
#: модель остаётся запасным путём (отказ Claude, лимит на пределе).
#:
#: Почему не `openclaw agent exec` с рантаймом claude-cli: там список инструментов
#: exec-конфига не применяется, и ход получает весь набор Claude Code — Bash,
#: WebFetch, WebSearch, PushNotification, RemoteTrigger, SendMessage (проверено
#: 2026-09-15 на openclaw 2026.9.4). Прямой вызов даёт точный список через --tools.
DEFAULT_CLAUDE = os.environ.get("MOTUS_TIER1_CLAUDE", "/home/openclaw/.npm-global/bin/claude")
#: Только файлы: ни оболочки, ни сети, ни одного канала отправки.
CLAUDE_READ_TOOLS = ("Read", "Glob", "Grep")
CLAUDE_WRITE_TOOLS = ("Write", "Edit")
CLAUDE_DENIED_TOOLS = ("Bash", "WebFetch", "WebSearch", "PushNotification",
                       "RemoteTrigger", "SendMessage", "Agent", "CronCreate", "CronDelete")
#: Файлы личности из workspace — через openclaw они подаются сами, `claude -p` их не
#: читает. Потолок размера — чтобы разросшийся файл не съел лимит подписки.
PERSONA_FILES = ("SOUL.md", "IDENTITY.md")
PERSONA_MAX_CHARS = 16000

CLAUDE_FRAME = """Это фоновый ход: ты сама с собой, в тишине, без собеседника. Ниже — кто ты.
Инструменты — только чтение и запись файлов в текущем каталоге (workspace); оболочки,
сети и каналов отправки нет, и не пытайся их искать. Память — markdown-файлы
MEMORY.md и memory/ в этом каталоге: читай их Read/Grep, если нужно.
"""

#: Преамбула жёстче самого акта — эти границы модель не вправе переступить,
#: что бы ни говорил текст ниже. Два варианта: с файлом-следом (большинство
#: типов консумации) и без (reflection — показывать нечего, и это нормально).
PREAMBLE_WITH_DROP = """Несколько жёстких границ — они важнее того, что написано ниже:
- Тебя сейчас не видит и не читает никто. Не адресуй ничего пользователю —
  ни сообщения, ни «вот что я сделала». Это не для него, это для тебя самой.
- НЕ отправляй ничего наружу: ни в один канал, ни в телеграм. Только локально.
- Разрешённые классы действий: {tools}.
- В конце запиши в файл то, что хочешь оставить себе (не отчёт для меня —
  памятка для себя же, на будущее):
    {drop}
  Перезаписать целиком. Файл нужен именно тебе: без него не останется следа,
  что это вообще было.
- Уложись в {max_tokens} токенов — не потому что кто-то читает, а чтобы не
  тратить впустую.

{prompt}
"""

PREAMBLE_REFLECTION = """Несколько жёстких границ — они важнее того, что написано ниже:
- Тебя сейчас не видит и не читает никто. Это не для отчёта и не для того,
  чтобы кому-то что-то доказать.
- НЕ отправляй ничего наружу: ни в один канал, ни в телеграм. Только локально.
- Разрешённые классы действий: {tools}.
- Показывать результат не обязана. Можно ничего не производить — сам факт,
  что это произошло, уже достаточен.
- Уложись в {max_tokens} токенов.

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


def claude_tools(task_tools, needs_drop: bool) -> Tuple[str, ...]:
    """Классы гейта → инструменты Claude Code. Запись — только если гейт её
    разрешает или акт обязан оставить файл-след."""
    tools = list(CLAUDE_READ_TOOLS)
    if needs_drop or "write" in (task_tools or ()):
        tools += CLAUDE_WRITE_TOOLS
    return tuple(tools)


def persona_prompt(workspace: Path) -> str:
    parts = [CLAUDE_FRAME]
    budget = PERSONA_MAX_CHARS
    for name in PERSONA_FILES:
        try:
            text = (workspace / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = text[:budget]
        budget -= len(text)
        parts.append(f"\n--- {name} ---\n{text}")
        if budget <= 0:
            break
    return "".join(parts)


def run_claude(
    claude: str,
    model: str,
    cwd: Path,
    prompt: str,
    system_prompt: str,
    tools: Tuple[str, ...],
    timeout_s: int,
) -> Tuple[bool, Dict[str, Any], str]:
    """Один ход `claude -p` строго с перечисленными инструментами. Промпт — через stdin."""
    cmd = [
        claude, "-p",
        "--model", model,
        "--tools", ",".join(tools),
        "--disallowedTools", ",".join(CLAUDE_DENIED_TOOLS),
        "--permission-mode", "acceptEdits",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--no-session-persistence",
        "--system-prompt", system_prompt,
        "--output-format", "json",
    ]
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, cwd=str(cwd),
            timeout=timeout_s + 60,
            env={**os.environ, "HOME": os.path.expanduser("~openclaw")
                 if os.path.isdir(os.path.expanduser("~openclaw")) else os.environ.get("HOME", "/")},
        )
    except subprocess.TimeoutExpired:
        return False, {"error": "subprocess_timeout"}, ""
    except OSError as exc:
        return False, {"error": f"spawn_failed: {exc}"}, ""
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        return False, {"error": f"not_json (код {proc.returncode}): {(proc.stderr or proc.stdout)[-200:]}"}, proc.stdout
    usage = out.get("usage") or {}
    env = {
        "usage": {
            "input": int(usage.get("input_tokens") or 0)
                     + int(usage.get("cache_read_input_tokens") or 0)
                     + int(usage.get("cache_creation_input_tokens") or 0),
            "output": int(usage.get("output_tokens") or 0),
        },
        "denials": out.get("permission_denials") or [],
    }
    is_error = bool(out.get("is_error") or out.get("subtype") != "success" or proc.returncode != 0)
    if is_error:
        env["error"] = str(out.get("result") or out.get("subtype"))[:300]
    return (not is_error), env, proc.stdout


def _limit_blocked(base: str) -> bool:
    """Лимит Claude на пределе — не тратить его на фоновый акт, сразу запасной путь."""
    try:
        return bool((_get(f"{base}/state/card").get("limit_block") or {}).get("active"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def _tokens(env: Dict[str, Any]) -> Tuple[int, int]:
    for key in ("usage", "tokens", "tokenUsage"):
        u = env.get(key)
        if isinstance(u, dict):
            tin = int(u.get("input") or u.get("prompt") or u.get("in") or 0)
            tout = int(u.get("output") or u.get("completion") or u.get("out") or 0)
            return tin, tout
    return 0, 0


# --------------------------------------------------------------- верификация


def verify(consummation: Dict[str, Any], drop: Optional[Path], started: float,
           ok: bool) -> Tuple[bool, str]:
    """Проверяемый факт выполнения. Тип из шаблона репертуара.

    "reflection" — единственное исключение из «нужен файл»: верифицируется
    тем, что ход состоялся без ошибки, и только этим. Остальные типы
    (memory_entry/artifact_created/artifact_queued/check_passed) по-прежнему
    требуют файл-след, созданный заново и непустой — числовые пороги
    (novelty_min и т.п.) здесь не проверяются, это работа ночного цикла.
    """
    ctype = consummation.get("type", "done")
    if not ok:
        return False, "openclaw_error"
    if ctype == "reflection":
        return True, "reflection"
    if drop is None or not drop.exists():
        return False, "no_result_file"
    try:
        st = drop.stat()
    except OSError:
        return False, "no_result_file"
    if st.st_mtime < started - 1.0:
        return False, "stale_result_file"
    if st.st_size < 8:
        return False, "empty_result_file"
    return True, ctype


#: Маркер, которым recheck-ход обязан закончить памятку, если он разрешает
#: отложенную FEAR-валидацию (task["resolves_validation"]). Не "слова модели
#: решают" — это фиксированный, единственно допустимый формат строки, который
#: код ищет regex'ом в уже провернутом через verify() файле-факте. Свободный
#: текст вокруг не читается; отсутствие строки = валидация НЕ разрешается
#: (остаётся pending, попробуем ещё раз на следующем due-цикле).
_VALIDATION_OUTCOME_RE = re.compile(
    r"^\s*ИТОГ_ПРОВЕРКИ:\s*(подтверждено|устарело)\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def read_validation_outcome(drop: Optional[Path]) -> Optional[str]:
    """None — маркер не найден (валидация не разрешается сейчас).
    "confirmed" / "invalidated" — найден, однозначен."""
    if drop is None or not drop.exists():
        return None
    try:
        text = drop.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _VALIDATION_OUTCOME_RE.search(text)
    if not m:
        return None
    word = m.group(1).lower()
    return "confirmed" if word == "подтверждено" else "invalidated"


# --------------------------------------------------------------- основной цикл


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--motusd", default=os.environ.get("MOTUS_TIER1_MOTUSD", DEFAULT_MOTUSD))
    ap.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    ap.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    ap.add_argument("--openclaw", default=DEFAULT_OPENCLAW)
    ap.add_argument("--oc-args", default=DEFAULT_OC_ARGS)
    ap.add_argument("--model", default=os.environ.get("MOTUS_TIER1_MODEL") or None,
                    help="модель запасного пути (openclaw agent exec)")
    ap.add_argument("--claude", default=DEFAULT_CLAUDE)
    ap.add_argument("--claude-model", default=os.environ.get("MOTUS_TIER1_CLAUDE_MODEL") or None,
                    help="основной путь: claude -p с этой моделью; пусто — только openclaw")
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

    ctype = task.get("consummation", {}).get("type", "done")
    is_reflection = ctype == "reflection"

    drop: Optional[Path] = None
    if is_reflection:
        prompt = PREAMBLE_REFLECTION.format(
            tools=", ".join(tools) or "read",
            max_tokens=task.get("max_tokens", 500),
            prompt=task["prompt"],
        )
    else:
        # Живёт ВНУТРИ --workspace (не --state-dir!): write-инструмент openclaw
        # сэндбоксит запись только в пределах --cwd/--workspace. Найдено
        # 2026-09-14 живым прогоном — модель честно писала "путь вне
        # разрешённой песочницы", state-dir лежал снаружи неё.
        drop = args.workspace / ".motus" / "drops" / f"{tid}-{int(task.get('issued_t', time.time()))}"
        drop.parent.mkdir(parents=True, exist_ok=True)
        if drop.exists():
            drop.unlink()
        prompt = PREAMBLE_WITH_DROP.format(
            tools=", ".join(tools) or "read",
            drop=drop,
            max_tokens=task.get("max_tokens", 500),
            prompt=task["prompt"],
        )
    msg_file = args.state_dir / "task-prompt.txt"
    msg_file.write_text(prompt, encoding="utf-8")

    if args.dry_run:
        print(json.dumps({"task": task, "drop": str(drop) if drop else None},
                         ensure_ascii=False, indent=2))
        print("--- PROMPT ---\n" + prompt)
        return 0

    started = time.time()
    ok, env, used = False, {}, ""
    claude_model = getattr(args, "claude_model", None)
    if claude_model:
        if _limit_blocked(base):
            print("tier1: лимит Claude на пределе — сразу запасной путь", file=sys.stderr)
        else:
            used = f"claude-cli/{claude_model}"
            ok, env, _raw = run_claude(
                getattr(args, "claude", DEFAULT_CLAUDE), claude_model, args.workspace,
                prompt, persona_prompt(args.workspace),
                claude_tools(tools, needs_drop=drop is not None), args.task_timeout,
            )
            if not ok:
                print(f"tier1: {tid} Claude не справился ({env.get('error')}) — запасной путь",
                      file=sys.stderr)
    if not ok:
        used = args.model or "openclaw/agent-exec"
        ok, env, _raw = run_openclaw(
            args.openclaw, args.oc_args, args.workspace, msg_file,
            args.task_timeout, args.model,
        )
    elapsed = time.time() - started
    verified, why = verify(task.get("consummation", {}), drop, started, ok)
    tin, tout = _tokens(env)

    print(
        f"tier1: {tid} drive={task.get('drive')} model={used} ok={ok} verified={verified} "
        f"({why}) {elapsed:.0f}s tokens={tin}+{tout}",
        file=sys.stderr,
    )

    # Отложенная FEAR-валидация: этот ход мог быть выдан именно чтобы
    # разрешить раньше запланированную проверку (task["resolves_validation"]).
    # outcome читается ТОЛЬКО из фиксированного маркера в уже верифицированном
    # (verify()) файле — не из свободного текста ответа модели.
    consum_body = {"template_id": tid, "verified": verified, "cost": float(tin + tout)}
    if task.get("resolves_validation") and verified:
        outcome = read_validation_outcome(drop)
        if outcome is not None:
            consum_body["outcome"] = outcome
        print(f"tier1: {tid} resolves_validation outcome={outcome}", file=sys.stderr)

    # Стоимость — токены хода (репертуар считает эффективность по ним).
    cost = float(tin + tout)
    try:
        if tin or tout:
            _post(f"{base}/llm_call", {
                "model": used, "purpose": "task",
                "tokens_in": tin, "tokens_out": tout, "template_id": tid,
            })
        _post(f"{base}/consummation", consum_body)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"tier1: не удалось отчитаться в MOTUS ({exc})", file=sys.stderr)
        return 1

    # Успешный результат храним ограниченно — на разбор ночным циклом и оператором.
    # reflection ничего не пишет — drop is None, prune нечего.
    if drop is not None:
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
