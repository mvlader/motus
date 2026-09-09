"""Репертуар задач — консумматорные акты драйвов.

Разделение полномочий (docs/01-structure.md §6):
  * ядро драйва (drive, consummation, preconditions) — константа, не правится;
  * efficacy — правит только код, по измерениям из журнала;
  * rationale, prompt, порядок — правит модель, ночью, в пределах квоты.

Какой драйв доминирует — решает математика. Какую задачу взять внутри драйва —
решает модель. Дать модели выбирать драйв значит превратить гомеостат в декорацию.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .core import Homeostat
from .gates import Gate
from .state import State

#: Ключи предусловий, известные системе. Неизвестное предусловие = запрет
#: (fail-closed): опечатка в конфиге не должна открывать ветку.
PRECONDITIONS = ("no_aversive_active", "budget_ok", "energy_ok", "context_fresh")

QUOTA_PER_NIGHT = 3  # сколько правок репертуара разрешено модели за один ночной цикл


@dataclass
class Task:
    template_id: str
    drive: str
    prompt: str
    cost_tier: str
    max_tokens: int
    consummation: Dict[str, Any]
    allowed_tools: tuple
    issued_t: float
    expires_t: float
    drive_at_issue: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "template_id": self.template_id,
            "drive": self.drive,
            "prompt": self.prompt,
            "cost_tier": self.cost_tier,
            "max_tokens": self.max_tokens,
            "consummation": self.consummation,
            "allowed_tools": list(self.allowed_tools),
            "issued_t": round(self.issued_t, 3),
            "expires_t": round(self.expires_t, 3),
            "drive_at_issue": round(self.drive_at_issue, 5),
        }


class Repertoire:
    def __init__(self, cfg: Dict[str, Any], homeostat: Homeostat) -> None:
        self.cfg = cfg
        self.h = homeostat
        # Глубокая копия: efficacy — изменяемое состояние, и разделять его между
        # движками нельзя. Иначе реплей в том же процессе стартует с эффективностью,
        # накопленной боевым прогоном, prior меняется, выбирается другой шаблон —
        # и детерминизм рушится (ловилось только на длинных прогонах).
        self.data = copy.deepcopy(cfg["_repertoire"])
        self.queue: List[Task] = []
        r = cfg.get("repertoire", {})
        #: Непрожитая задача должна исчезать, а не копиться: очередь, растущая
        #: быстрее исполнения, превращается в генератор перегрузки.
        self.TASK_TTL_S = float(r.get("task_ttl_s", 3600.0))
        self.MAX_QUEUE = int(r.get("max_queue", 3))
        #: Порог насыщения репертуаром: привыкание не только переставляет шаблоны
        #: местами, но и может запретить брать задачу вообще. Иначе единственный
        #: шаблон драйва крутится бесконечно — аттракторный коллапс в поведении.
        self.MIN_SCORE = float(r.get("min_score", 0.30))
        #: Надбавка к привыканию за протухшую невыполненной задачу.
        self.EXPIRY_PENALTY = float(r.get("expiry_penalty", 2.0))

    def templates_for(self, drive: str) -> List[Dict[str, Any]]:
        return [t for t in self.data["templates"] if t["drive"] == drive]

    # ------------------------------------------------------------ выбор

    def _precondition_ok(self, name: str, st: State, gate: Gate) -> bool:
        if name == "no_aversive_active":
            return self.h.max_aversive(st) < 0.30
        if name == "budget_ok":
            return st.tokens >= 0.5
        if name == "energy_ok":
            return self.h.energy(st) >= 0.35
        if name == "context_fresh":
            return gate.context_band in ("fresh", "aging")
        return False  # fail-closed

    def _score(self, st: State, tpl: Dict[str, Any]) -> float:
        """Габитуация по ШАБЛОНУ, а не только по теме: иначе система залипнет
        на любимом действии — тот же аттракторный коллапс, только в поведении."""
        hab = self.h.habituation_factor(st, f"template:{tpl['id']}")
        eff = tpl.get("efficacy", {})
        n = eff.get("n", 0)
        # Пока измерений мало, эффективность не знаем — не даём ей решать.
        prior = 0.5 if n < 3 else max(0.0, min(1.0, eff.get("mean_delta", 0.0) * 2.0))
        return hab * (0.5 + 0.5 * prior)

    def select(self, st: State, gate: Gate) -> Optional[Task]:
        """Взять задачу для доминирующего драйва. None — если нечего или нельзя."""
        self.expire(st.t, st)
        if len(self.queue) >= self.MAX_QUEUE:
            return None
        drive = gate.regime
        if drive not in self.cfg["drives"]:
            return None
        cands = []
        for tpl in self.templates_for(drive):
            if any(not self._precondition_ok(p, st, gate) for p in tpl["preconditions"]):
                continue
            cands.append((self._score(st, tpl), tpl))
        if not cands:
            return None
        cands.sort(key=lambda kv: kv[0], reverse=True)
        if cands[0][0] < self.MIN_SCORE:
            return None
        best = cands[0][1]
        task = Task(
            template_id=best["id"],
            drive=drive,
            prompt=best["prompt"],
            cost_tier=best["cost_tier"],
            max_tokens=min(best["max_tokens"], gate.max_tokens),
            consummation=best["consummation"],
            # Правило 2 из engine.py, зафиксированное в данных: фоновая задача
            # физически не получает канал наружу. Единственный путь к исходящему
            # сообщению — Tier 2.
            allowed_tools=tuple(x for x in gate.allowed_tools if x != "outbound"),
            issued_t=st.t,
            expires_t=st.t + self.TASK_TTL_S,
            drive_at_issue=st.drives[drive],
        )
        st.habituation[f"template:{best['id']}"] = (
            st.habituation.get(f"template:{best['id']}", 0.0) + 1.0
        )
        self.queue.append(task)
        return task

    def expire(self, t: float, st: Optional[State] = None) -> int:
        """Протухшие задачи выбрасываются и наказывают свой шаблон привыканием.

        Задача, которую никто не выполнил, — свидетельство, что исполнитель не
        работает или шаблон нереализуем. Ставить её ещё двести раз бессмысленно.
        """
        expired = [q for q in self.queue if q.expires_t <= t]
        self.queue = [q for q in self.queue if q.expires_t > t]
        if st is not None:
            for q in expired:
                k = f"template:{q.template_id}"
                st.habituation[k] = st.habituation.get(k, 0.0) + self.EXPIRY_PENALTY
        return len(expired)

    def pop(self, template_id: str) -> Optional[Task]:
        for i, q in enumerate(self.queue):
            if q.template_id == template_id:
                return self.queue.pop(i)
        return None

    # ----------------------------------------------------- эффективность

    def record(self, template_id: str, delta: float, cost: float, verified: bool) -> None:
        """§10. EMA по измерениям. Пишет ТОЛЬКО код."""
        alpha = 0.25
        for tpl in self.data["templates"]:
            if tpl["id"] != template_id:
                continue
            e = tpl.setdefault(
                "efficacy", {"n": 0, "mean_delta": 0.0, "success_rate": 0.0, "mean_cost": 0.0}
            )
            e["n"] += 1
            e["mean_delta"] = (1 - alpha) * e["mean_delta"] + alpha * (-delta)
            e["success_rate"] = (1 - alpha) * e["success_rate"] + alpha * (1.0 if verified else 0.0)
            e["mean_cost"] = (1 - alpha) * e["mean_cost"] + alpha * cost
            return

    def save(self, path: str) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
