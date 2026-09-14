#!/usr/bin/env python3
"""Датчик 5-часового лимита Claude → MOTUS. По образцу somatic_probe.py:
платформенная специфика (запуск `claude -p "/usage"`, разбор его текста) живёт
здесь, в deploy/, ядро (motus/appraisal.py) знает только про готовые числа.

Что шлём и почему:
  * claude_limit_pct       — текущий % использования СЕССИОННОГО (5-часового)
                              лимита. Недельный сознательно игнорируется
                              (осознанное решение оператора, 2026-09-13) —
                              недельный сбрасывается днями, а не часами, реакция
                              на него была бы неотличима от "бот сломался".
  * claude_limit_delta     — рост в процентных пунктах с прошлого опроса.
                              Считаем здесь (не в ядре) по тому же принципу, что
                              integrity_drop в somatic_probe.py: фронт-детекция
                              на дешёвом stdlib-состоянии между вызовами.
  * claude_limit_reset     — True, если % резко упал с прошлого опроса (сброс
                              лимита случился между опросами).
  * claude_limit_reset_at  — unix-время следующего сброса, как его называет
                              сама команда ("resets Sep 13, 7:09pm").

`claude -p "/usage"` не бьёт по самому лимиту (это служебная CLI-команда, не
вызов модели с системным промптом — проверено вручную: несколько подряд идущих
вызовов не двигают процент, кроме реальной параллельной работы).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

#: Между поднятием % и наступлением сброса — резкий скачок ВНИЗ считается
#: сбросом, а не шумом измерения (единичный % туда-сюда — это не сброс).
RESET_DROP_THRESHOLD = 15.0

STATE_FILE = Path(os.environ.get("MOTUS_LIMIT_PROBE_STATE", "/tmp/motus-limit-probe.json"))

#: "· resets ..." — ОПЦИОНАЛЬНО: живой прогон поймал реальный случай — прямо
#: после сброса, пока использование ровно 0%, CLI печатает "Current session:
#: 0% used" БЕЗ времени следующего сброса вообще. Жёсткое требование суффикса
#: молча роняло парсинг на каждом опросе после сброса — MOTUS замерзал на
#: последнем УСПЕШНО распознанном значении (в этом случае — на 97%) навсегда,
#: потому что build_payload() ни разу не получал шанс увидеть падение процента.
_SESSION_RE = re.compile(
    r"Current session:\s*(\d+)%\s*used"
    r"(?:\s*·\s*resets\s+([A-Za-z]+ \d+, \d+:\d+(?:am|pm)))?",
    re.IGNORECASE,
)


def run_usage(claude_bin: str, timeout_s: float) -> str:
    proc = subprocess.run(
        [claude_bin, "-p", "/usage"], capture_output=True, text=True, timeout=timeout_s,
    )
    return proc.stdout


def parse_usage(text: str, now: datetime.datetime) -> Optional[tuple[float, Optional[float]]]:
    """-> (pct, reset_at_epoch_или_None) или None, если pct вообще не распознан.

    reset_at может отсутствовать в тексте (см. _SESSION_RE) — прямо после
    сброса, пока использование ровно 0%, CLI не печатает время следующего
    сброса вообще. pct — обязателен: он единственное, что позволяет вообще
    заметить, что сброс произошёл (см. build_payload).

    Год не печатается — берём текущий, и если получившаяся дата в прошлом
    (переход через полночь/полночь года — редкость, но не должна дать сброс
    в прошлое), сдвигаем на год вперёд.

    Часовой пояс НЕ парсится из текста ("(America/Toronto)") — берётся системный
    (`now.tzinfo`). Верно, пока системный TZ контейнера совпадает с тем, что
    показывает `/usage` (проверено на деплое: оба America/Toronto). Если это
    когда-то разъедется — здесь тихая ошибка на час-два, не на дни.
    """
    m = _SESSION_RE.search(text)
    if not m:
        return None
    pct = float(m.group(1))
    when = m.group(2)
    if not when:
        return pct, None
    when_str = f"{when} {now.year}"
    try:
        reset_dt = datetime.datetime.strptime(when_str, "%b %d, %I:%M%p %Y")
    except ValueError:
        return pct, None
    reset_dt = reset_dt.replace(tzinfo=now.tzinfo)
    if reset_dt < now:
        reset_dt = reset_dt.replace(year=reset_dt.year + 1)
    return pct, reset_dt.timestamp()


def _load_prev() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_prev(d: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass


def build_payload(pct: float, reset_at: Optional[float], prev: dict) -> dict:
    payload: dict = {"claude_limit_pct": pct}
    if reset_at is not None:
        payload["claude_limit_reset_at"] = reset_at
    prev_pct = prev.get("pct")
    if prev_pct is not None:
        if pct + RESET_DROP_THRESHOLD < prev_pct:
            payload["claude_limit_reset"] = True
        elif pct > prev_pct:
            payload["claude_limit_delta"] = pct - prev_pct
    return payload


def post_event(motusd: str, payload: dict, timeout_s: float = 5) -> int:
    body = json.dumps({"kind": "sensor", "payload": payload}).encode("utf-8")
    req = urllib.request.Request(
        f"{motusd.rstrip('/')}/event", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            if resp.status != 200:
                print(f"motusd вернул {resp.status}", file=sys.stderr)
                return 1
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"motusd недоступен: {exc}", file=sys.stderr)
        return 0  # не ошибка сборки — нечего кормить
    return 0


def poll_once(claude_bin: str, timeout_s: float, motusd: str, dry_run: bool) -> tuple[int, Optional[float]]:
    """Один опрос + отправка. -> (код возврата, reset_at или None)."""
    now = datetime.datetime.now().astimezone()
    try:
        text = run_usage(claude_bin, timeout_s)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"claude -p /usage не выполнился: {exc}", file=sys.stderr)
        return 0, None  # временная недоступность CLI — не повод падать самому

    parsed = parse_usage(text, now)
    if parsed is None:
        print("не удалось разобрать вывод /usage:", text[:200], file=sys.stderr)
        return 0, None

    pct, reset_at = parsed
    prev = _load_prev()
    payload = build_payload(pct, reset_at, prev)
    # reset_at может отсутствовать именно в этом ответе (сразу после сброса,
    # 0% без времени следующего) — не затираем последний известный None'ом.
    _save_prev({"pct": pct, "reset_at": reset_at if reset_at is not None else prev.get("reset_at")})

    if dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0, reset_at

    return post_event(motusd, payload), reset_at


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--motusd", default="http://127.0.0.1:18790")
    ap.add_argument("--claude-bin", default="claude")
    ap.add_argument("--no-precise-recheck", action="store_true",
                    help="не ждать до момента сброса, если он близко (см. --max-wait)")
    ap.add_argument("--max-wait", type=float, default=11 * 60,
                    help="верхняя граница ожидания точного момента сброса, секунды")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rc, reset_at = poll_once(args.claude_bin, args.timeout, args.motusd, args.dry_run)

    # Точная допроверка на reset_at + 20с (см. DEPLOY.md): обычный 10-минутный
    # таймер может проспать момент сброса почти на 10 минут; здесь известно
    # ТОЧНОЕ время (сама команда его называет). Ждём внутри уже запущенного
    # процесса, а не планируем отдельный юнит — тому нужны права, которых у
    # обычного `User=openclaw` нет (не root, systemd-run --user из-под
    # системного oneshot ненадёжен без гарантированного user-bus).
    if reset_at is not None and not args.no_precise_recheck:
        wait_s = reset_at + 20 - time.time()
        if 0 < wait_s <= args.max_wait:
            time.sleep(wait_s)
            rc, _ = poll_once(args.claude_bin, args.timeout, args.motusd, args.dry_run)

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
