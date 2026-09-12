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
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .config import DRIVES
from .core import Homeostat
from .gates import Gate
from .state import State

#: Ключи предусловий, известные системе. Неизвестное предусловие = запрет
#: (fail-closed): опечатка в конфиге не должна открывать ветку.
PRECONDITIONS = ("no_aversive_active", "budget_ok", "energy_ok", "context_fresh")

QUOTA_PER_NIGHT = 3  # сколько правок репертуара разрешено модели за один ночной цикл

#: Драйвы, для которых модели разрешено предлагать шаблоны. RAGE исключён тем
#: же правилом, что и в config.validate(): «у RAGE не может быть репертуара» —
#: это ядро, не то, что курирование вправе тронуть.
CURATABLE_DRIVES = tuple(d for d in DRIVES if d != "RAGE")

#: Типы консумматорного акта, известные исполнителю (Tier 1). Модель не может
#: выдумать новый тип — он должен быть понятен коду, который его проверяет.
#: "reflection" (2026-09-11) — единственный тип БЕЗ артефакта: верифицируется
#: только тем, что ход состоялся (не оборвался ошибкой), файла не требует.
#: Не всё, чем личность занимается сама с собой, обязано превращаться в отчёт —
#: см. config/repertoire.json, "_note_2026_09_11".
KNOWN_CONSUMMATION_TYPES = ("memory_entry", "artifact_queued", "artifact_created",
                           "check_passed", "reflection")

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_MAX_PROMPT_LEN = 400
_MAX_RATIONALE_LEN = 300
#: Потолок числа шаблонов в репертуаре. Без него множество ночей подряд с
#: op=add и без соразмерного archive устроили бы неограниченный рост файла и
#: неограниченный рост пространства выбора в select() — тот же аттракторный
#: риск, что и у бесконечной очереди задач (task_ttl_s/max_queue).
MAX_TEMPLATES = 40


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

    def peek(self, t: float, st: Optional[State] = None) -> Optional[Task]:
        """Отдать самую раннюю ещё не выданную задачу из очереди, НЕ убирая её.

        select() кладёт задачи в очередь на тиках фонового цикла, идущего
        независимо от опросов исполнителя (Tier 1). Без peek() исполнитель
        видел только то, что select() решит выдать в момент ЕГО опроса, а
        реально положенные фоновым тиком задачи никто не читал — они просто
        протухали по TTL. Удаление из очереди — по template_id, при
        /consummation (см. pop()), не здесь.
        """
        self.expire(t, st)
        return self.queue[0] if self.queue else None

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

    # ------------------------------------------------- курирование (ночь)

    def efficacy_report(self) -> List[Dict[str, Any]]:
        """Снимок для модели на ночном курировании: то же самое, что уже
        возвращает engine.maybe_sleep() как SleepReport.efficacy — вынесено
        сюда как метод репертуара, а не только побочный продукт сна."""
        return [
            {"id": t["id"], "drive": t["drive"], **t.get("efficacy", {}),
             "rationale": t.get("rationale", ""), "prompt": t["prompt"]}
            for t in self.data["templates"]
        ]

    def apply_edits(self, edits: Any) -> Dict[str, Any]:
        """Применить ПРЕДЛОЖЕНИЯ модели (curator.propose_edits) с полной
        code-side проверкой — вызывающая сторона не обязана доверять модели
        ни в чём. Правки, не прошедшие проверку, отбрасываются по отдельности
        (fail-closed на каждую, не на всю пачку), с причиной — для журнала.

        Три границы, которые эта функция обязана держать (docs/01 §6):
          1. Квота — не больше QUOTA_PER_NIGHT штук за вызов, остальное игнор.
          2. Ядро существующего шаблона (drive/consummation/preconditions)
             правка "rewrite" тронуть не может физически — в её схеме этих
             полей просто нет, трогаются только prompt/rationale.
          3. Пространство значений для НОВОГО шаблона (op=add) сужено заранее:
             drive только из CURATABLE_DRIVES, consummation.type только из
             KNOWN_CONSUMMATION_TYPES, preconditions только из PRECONDITIONS.
        """
        applied: List[str] = []
        rejected: List[Dict[str, str]] = []
        if not isinstance(edits, list):
            return {"applied": applied, "rejected": [{"id": "?", "reason": "not_a_list"}]}
        for edit in edits[:QUOTA_PER_NIGHT]:
            ok, eid, reason = self._apply_one(edit)
            if ok:
                applied.append(eid)
            else:
                rejected.append({"id": eid, "reason": reason})
        return {"applied": applied, "rejected": rejected}

    def _apply_one(self, edit: Any) -> Tuple[bool, str, str]:
        """Вернуть (применено?, id_для_журнала, причина_если_нет)."""
        if not isinstance(edit, dict):
            return False, "?", "not_a_dict"
        eid = edit.get("id")
        if not isinstance(eid, str) or not _ID_RE.match(eid):
            return False, str(eid), "bad_id"
        op = edit.get("op")

        if op == "rewrite":
            tpl = next((t for t in self.data["templates"] if t["id"] == eid), None)
            if tpl is None:
                return False, eid, "unknown_id"
            prompt = edit.get("prompt")
            rationale = edit.get("rationale")
            changed = False
            if isinstance(prompt, str) and prompt.strip() and len(prompt) <= _MAX_PROMPT_LEN:
                tpl["prompt"] = prompt.strip()
                changed = True
            if (isinstance(rationale, str) and rationale.strip()
                    and len(rationale) <= _MAX_RATIONALE_LEN):
                tpl["rationale"] = rationale.strip()
                changed = True
            return (changed, eid, "rewritten" if changed else "empty_rewrite")

        if op == "add":
            if any(t["id"] == eid for t in self.data["templates"]):
                return False, eid, "id_exists"
            if len(self.data["templates"]) >= MAX_TEMPLATES:
                return False, eid, "repertoire_full"
            drive = edit.get("drive")
            if drive not in CURATABLE_DRIVES:
                return False, eid, "bad_drive"
            ctype = edit.get("consummation_type")
            if ctype not in KNOWN_CONSUMMATION_TYPES:
                return False, eid, "bad_consummation_type"
            preconds = edit.get("preconditions", [])
            if not isinstance(preconds, list) or any(p not in PRECONDITIONS for p in preconds):
                return False, eid, "bad_preconditions"
            prompt = edit.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > _MAX_PROMPT_LEN:
                return False, eid, "bad_prompt"
            rationale = edit.get("rationale", "")
            if not isinstance(rationale, str):
                rationale = ""
            self.data["templates"].append({
                "id": eid, "drive": drive, "cost_tier": "local", "max_tokens": 600,
                "preconditions": list(preconds), "consummation": {"type": ctype},
                "prompt": prompt.strip(), "rationale": rationale.strip()[:_MAX_RATIONALE_LEN],
                "efficacy": {"n": 0, "mean_delta": 0.0, "success_rate": 0.0, "mean_cost": 0.0},
            })
            return True, eid, "added"

        if op == "archive":
            idx = next((i for i, t in enumerate(self.data["templates"]) if t["id"] == eid), None)
            if idx is None:
                return False, eid, "unknown_id"
            tpl = self.data["templates"].pop(idx)
            self.data.setdefault("archived", []).append(tpl)
            # Задачи этого шаблона, уже стоящие в очереди, пусть доживут —
            # архивация не отменяет то, что уже выдано и, может, выполняется.
            return True, eid, "archived"

        return False, eid, "unknown_op"
