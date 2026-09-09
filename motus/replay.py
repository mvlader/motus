"""Реплей-стенд: прогон журнала событий через ядро заново.

Две функции сразу:
  * регрессионный тест на детерминизм — расхождение снимков означает, что где-то
    появилась зависимость от реального времени, от порядка словаря или от случайности;
  * ответ на вопрос «почему она это сказала» — состояние на любой момент прошлого.

Плюс синтетические сценарии для калибровки: сутки прогоняются за миллисекунды,
и параметры настраиваются по графикам, а не подбором вслепую.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .clock import Clock, VirtualClock
from .engine import Engine
from .events import Event
from .journal import Journal, NullJournal
from .state import State


#: Реплей использует те же часы, что и боевой прогон: смещение от UTC берётся из
#: записи boot, поэтому циркадный ритм воспроизводится точно даже на машине в
#: другом часовом поясе.
ReplayClock = VirtualClock


@dataclass
class Divergence:
    seq: int
    t: float
    field: str
    expected: Any
    got: Any


@dataclass
class ReplayResult:
    ticks: int
    events: int
    divergences: List[Divergence] = field(default_factory=list)
    final: Optional[Dict[str, Any]] = None

    @property
    def deterministic(self) -> bool:
        return not self.divergences


def replay(cfg: Dict[str, Any], records: Iterable[Dict[str, Any]],
           tolerance: float = 1e-9) -> ReplayResult:
    recs = sorted(records, key=lambda r: r["seq"])
    if not recs:
        return ReplayResult(0, 0)

    boot = next((r for r in recs if r["kind"] == "boot"), None)
    tz = (boot or {}).get("payload", {}).get("tz_offset_s", 0.0)
    clock = ReplayClock(recs[0]["t"], tz)
    engine: Optional[Engine] = None
    res = ReplayResult(0, 0)

    for rec in recs:
        kind, t = rec["kind"], rec["t"]
        clock.t = t
        if kind == "boot":
            st = State.initial(cfg, t)
            engine = Engine(cfg, clock, NullJournal(), st)
            continue
        if engine is None:
            continue

        if kind == "event":
            if "kind" not in rec["payload"]:
                continue  # служебные записи вроде initiation_unanswered
            engine.submit_event(Event.from_dict(rec["payload"]))
            res.events += 1
        elif kind == "consummation":
            p = rec["payload"]
            engine.consummate(p["template_id"], p["verified"], p.get("cost", 0.0))
        elif kind == "tick":
            engine.tick(t)
            res.ticks += 1
            if "state" in rec:
                _compare(rec, engine.state, res, tolerance)

    res.final = engine.state.snapshot() if engine else None
    return res


def _compare(rec: Dict[str, Any], st: State, res: ReplayResult, tol: float) -> None:
    exp, got = rec["state"], st.snapshot()
    for group in ("drives", "modulators", "somatic"):
        for k, v in exp.get(group, {}).items():
            g = got[group].get(k)
            if g is None or abs(g - v) > tol:
                res.divergences.append(Divergence(rec["seq"], rec["t"], f"{group}.{k}", v, g))
    if exp.get("regime") != got.get("regime"):
        res.divergences.append(
            Divergence(rec["seq"], rec["t"], "regime", exp.get("regime"), got.get("regime"))
        )


def replay_journal(cfg: Dict[str, Any], journal: Journal) -> ReplayResult:
    return replay(cfg, journal.read_all())


# --------------------------------------------------------- синтетика для калибровки


def synthetic(cfg: Dict[str, Any], script: List[Tuple[float, Optional[Event]]],
              tick_s: float = 300.0, start_t: float = 1767225600.0,
              tz_offset_s: float = 0.0) -> List[Dict[str, Any]]:
    """Прогнать сценарий и вернуть ряд снимков для графиков.

    script — список (абсолютное_смещение_в_секундах, событие|None), отсортированный.
    """
    clock = ReplayClock(start_t, tz_offset_s)
    eng = Engine(cfg, clock, NullJournal())
    out: List[Dict[str, Any]] = []
    pending = list(script)
    t_end = max(off for off, _ in script) if script else 0.0
    t = 0.0
    while t <= t_end:
        while pending and pending[0][0] <= t:
            _, ev = pending.pop(0)
            if ev is not None:
                ev.t = start_t + t
                eng.submit_event(ev)
        clock.t = start_t + t
        d = eng.tick(clock.now())
        snap = eng.state.snapshot()
        snap["tier"] = d.tier
        snap["hours"] = round(t / 3600.0, 3)
        snap["arousal"] = round(eng.h.arousal(eng.state), 4)
        out.append(snap)
        t += tick_s
    return out
