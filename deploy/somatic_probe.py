#!/usr/bin/env python3
"""Датчик железа → MOTUS. Читает то немногое, что на Pi 5 реально соматично, и
шлёт одним событием `kind=sensor` в motusd. Запускается по таймеру
(`deploy/motus-somatic.timer`) внутри контейнера `grach` (там openclaw, чью живость проверяем).

Платформенная специфика (пути в /sys, отсутствие vcgencmd в контейнере) намеренно
живёт здесь, в deploy/, а не в ядре: `motus/appraisal.py:somatic_update()` принимает
готовый payload и знает только про поля, не про то, откуда они.

Что шлём и почему:
  * temp_c        — /sys/class/thermal/thermal_zone0 (cpu-thermal). Единственный
                    по-настоящему соматический сигнал на этой машине: Pi 5 под
                    нагрузкой доходит до мягкого температурного лимита. Ядро само
                    выводит из temp_c уровень thermal (somatic_update: линейно
                    55→85 °C), отдельный флаг throttled не нужен.
  * disk_free_frac — statvfs('/'). На SSD 954 ГБ это почти всегда ~0.3, но если
                    контейнер однажды прижмёт к стенке — сигнал честный.
  * services_ok   — отвечает ли openclaw на своём порту. Если бот лёг — это
                    integrity-drop, ядро поднимет FEAR+CARE.

throttled НЕ шлём: в контейнере нет vcgencmd, а `scaling_cur_freq < max` на Pi 5
верно и просто на холостом ходу (governor снижает частоту), то есть даёт ложную
тревогу о троттлинге. Энергию тоже не шлём: Pi от сети, «заряда» нет — пусть
остаётся конфижный дефолт.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

THERMAL = "/sys/class/thermal/thermal_zone0/temp"
#: Где помнить прошлый опрос (для детекции фронта integrity_drop). Между запусками
#: oneshot-сервиса переживает только каталог из StateDirectory=, отсюда env.
STATE_FILE = Path(os.environ.get("MOTUS_PROBE_STATE", "/tmp/motus-somatic-probe.json"))


def _read_int(path: str) -> int | None:
    try:
        with open(path, encoding="ascii") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def collect(openclaw_port: int) -> dict:
    payload: dict = {}

    milli = _read_int(THERMAL)
    if milli is not None:
        payload["temp_c"] = round(milli / 1000.0, 1)

    try:
        st = os.statvfs("/")
        payload["disk_free_frac"] = round(st.f_bavail / st.f_blocks, 3)
    except (OSError, ZeroDivisionError):
        pass

    ok = _port_open("127.0.0.1", openclaw_port)
    payload["services_ok"] = ok

    # integrity_drop — импульс FEAR+CARE в ядре — только на ФРОНТЕ «было ок → стало
    # плохо», не на каждом опросе, пока сервис лежит. Иначе тревога капает вечно.
    prev = _load_prev()
    if prev.get("services_ok", True) and not ok:
        payload["integrity_drop"] = True
    _save_prev({"services_ok": ok})
    return payload


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


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--motusd", default="http://127.0.0.1:18790",
                    help="адрес публичного API MOTUS (проксирован в grach как 127.0.0.1:18790)")
    ap.add_argument("--openclaw-port", type=int, default=18789,
                    help="порт openclaw gateway для проверки живости")
    ap.add_argument("--dry-run", action="store_true", help="только напечатать payload")
    args = ap.parse_args()

    payload = collect(args.openclaw_port)
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    body = json.dumps({"kind": "sensor", "payload": payload}).encode("utf-8")
    req = urllib.request.Request(
        f"{args.motusd.rstrip('/')}/event", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                print(f"motusd вернул {resp.status}", file=sys.stderr)
                return 1
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # motusd не поднят — не ошибка сборки, просто нечего кормить.
        print(f"motusd недоступен: {exc}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
