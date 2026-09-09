#!/usr/bin/env python3
"""Стенд калибровки: прогнать сценарий и нарисовать драйвы в терминале.

    python3 tools/simulate.py --hours 72 --scenario silence
    python3 tools/simulate.py --hours 48 --scenario chatty --width 100
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motus import config, replay          # noqa: E402
from motus.events import Event            # noqa: E402

BLOCKS = " ▁▂▃▄▅▆▇█"


def scenario(name: str, hours: float):
    end = hours * 3600
    if name == "silence":
        return [(0.0, None), (end, None)]
    if name == "chatty":
        s = [(0.0, None)]
        t = 0.0
        while t < end:
            s.append((t, Event("user_message", 0, {"appraisal": {"novelty": 1, "social_warmth": 1}})))
            t += 2 * 3600
        s.append((end, None))
        return s
    if name == "broken":
        s = [(0.0, None)]
        t = 0.0
        while t < end:
            s.append((t, Event("tool_error", 0, {"tool": "net", "blocking": True})))
            t += 1800
        s.append((end, None))
        return s
    if name == "abandon":
        # Поговорили и пропали — самый важный сценарий для проверки на спам.
        return [(0.0, Event("user_message", 0, {"appraisal": {"social_warmth": 2}})),
                (end, None)]
    raise SystemExit(f"неизвестный сценарий: {name}")


def sparkline(values, width):
    if not values:
        return ""
    step = max(1, len(values) // width)
    sampled = values[::step][:width]
    return "".join(BLOCKS[min(8, int(v * 8.999))] for v in sampled)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=72.0)
    ap.add_argument("--scenario", default="silence",
                    choices=["silence", "chatty", "broken", "abandon"])
    ap.add_argument("--tick", type=float, default=300.0)
    ap.add_argument("--width", type=int, default=80)
    args = ap.parse_args()

    cfg = config.load()
    rows = replay.synthetic(cfg, scenario(args.scenario, args.hours), tick_s=args.tick)

    print(f"сценарий: {args.scenario}, {args.hours:g} ч, тик {args.tick:g} с, "
          f"{len(rows)} точек\n")
    for name in cfg["drives"]:
        vals = [r["drives"][name] for r in rows]
        print(f"{name:<8} {sparkline(vals, args.width)}  "
              f"мин {min(vals):.2f} макс {max(vals):.2f} кон {vals[-1]:.2f}")
    print(f"{'arousal':<8} {sparkline([r['arousal'] for r in rows], args.width)}")

    tiers = [r["tier"] for r in rows]
    print(f"\nTier 1 (фоновых задач): {tiers.count(1)}")
    print(f"Tier 2 (инициаций):     {tiers.count(2)}")
    regimes = {}
    for r in rows:
        regimes[r["regime"]] = regimes.get(r["regime"], 0) + 1
    share = ", ".join(f"{k} {100*v/len(rows):.0f}%"
                      for k, v in sorted(regimes.items(), key=lambda kv: -kv[1]))
    print(f"режимы: {share}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
