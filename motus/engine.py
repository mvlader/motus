"""Сборка слоёв: тик → решение об уровне пробуждения.

Единственное место, где слои встречаются. Здесь же — три правила, которые нельзя
нарушать при любых правках:

  1. Уровень Tier 2 (право говорить) выдаётся только при открытом гейте, достаточной
     активации И непустом бюджете. Три условия, все обязательны.
  2. Фоновая задача (Tier 1) НИКОГДА не получает инструмент "outbound". Единственный
     путь к исходящему сообщению — Tier 2. Иначе гейты обходятся через репертуар.
  3. Насыщение начисляется только через consummate() по проверяемому факту, никогда
     по заявлению модели.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .appraisal import Appraiser
from .budget import Budget
from .clock import Clock
from .core import Homeostat, _clip
from .events import Event, Impulse
from .gates import Gate, Gatekeeper
from .journal import Journal, NullJournal
from .repertoire import Repertoire, Task
from .state import State
from .verbalizer import Card, Verbalizer


@dataclass
class Decision:
    tier: int
    gate: Gate
    card: Card
    task: Optional[Task]
    next_tick_s: float
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier,
            "gate": self.gate.to_dict(),
            "card": self.card.to_dict(),
            "task": self.task.to_dict() if self.task else None,
            "next_tick_s": round(self.next_tick_s, 2),
            "reason": self.reason,
        }


@dataclass
class SleepReport:
    ran: bool
    reason: str
    efficacy: List[Dict[str, Any]]


class Engine:
    def __init__(
        self,
        cfg: Dict[str, Any],
        clock: Clock,
        journal: Optional[Journal] = None,
        st: Optional[State] = None,
        sensor=None,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self.journal = journal or NullJournal()
        self.h = Homeostat(cfg, clock)
        self.gk = Gatekeeper(cfg, self.h)
        self.vb = Verbalizer(cfg, self.h)
        self.bg = Budget(cfg)
        self.rep = Repertoire(cfg, self.h)
        # Явно переданный sensor означает режим "model" (так строят тесты и
        # daemon при appraisal.mode=model); иначе — режим из конфига, по
        # умолчанию "model" (docs/04-model-l1.md).
        ap_cfg = cfg.get("appraisal", {})
        ap_mode = "model" if sensor is not None else ap_cfg.get("mode", "model")
        self.ap = Appraiser(sensor, mode=ap_mode,
                            lexical_strict=bool(ap_cfg.get("lexical_strict", False)),
                            model_fallback=ap_cfg.get("model_fallback", "null"))
        self.state = st or State.initial(cfg, clock.now())
        self._last_regime = self.state.regime
        self._last_card = ""
        #: Отложенный сон логируется один раз за эпизод, а не на каждом тике:
        #: run_ticker зовёт maybe_sleep() каждые 45–600 с, и незатухающая петля
        #: иначе засыпала бы журнал записями "sleep deferred".
        self._sleep_deferred_logged = False
        self.journal.write(
            "boot", self.state.t,
            {"version": cfg.get("schema"),
             "tz_offset_s": getattr(clock, "tz_offset_s", 0.0)},
            self.state.snapshot(),
        )

    # ---------------------------------------------------------------- события

    def appraise_text_now(self, text: str, t: Optional[float] = None) -> Dict[str, Any]:
        """Оценить текст сообщения (L-1) и вернуть готовый appraisal-словарь.

        Не трогает State — вызывающий код (daemon.py) намеренно зовёт это ВНЕ
        общего лока: при appraisal.mode=model это сетевой вызов (ollama/llama.cpp),
        секунды на тёплом старте и до ~50с на холодном (docs/04-model-l1.md).
        Раньше этот вызов сидел внутри submit_event() под общим локом демона —
        /state/card и другие эндпоинты ждали его всю дорогу и сами упирались в
        клиентский timeout плагина, из-за чего оба получали BrokenPipeError
        (сервер отвечал уже в закрытый клиентом сокет).
        """
        a = self.ap.appraise_text(text)
        # appraisal_invalid — только когда режим "model" и модель РЕАЛЬНО
        # отказала (таймаут, битый JSON, пустой ответ на непустой текст).
        # Штатный ноль от словаря или от mode=off сбоем не считается.
        if self.ap.last_failed and text.strip():
            self.journal.write("appraisal_invalid", t if t is not None else self.state.t,
                               {"chars": len(text)})
        return {
            "valence": a.valence, "threat": a.threat, "novelty": a.novelty,
            "social_warmth": a.social_warmth, "loss": a.loss,
            "agency_blocked": a.agency_blocked,
        }

    def submit_event(self, ev: Event) -> List[Impulse]:
        """Событие мира → импульсы → состояние. Время события двигает часы вперёд."""
        if ev.t < self.state.t:
            ev.t = self.state.t  # события из прошлого не отматывают время назад
        self.h.advance(self.state, ev.t)

        if ev.kind == "user_message" and "text" in ev.payload:
            # Сырой текст пользователя оценивает L-1 и БЕЗУСЛОВНО выбрасывается:
            # в журнал уходит только производный результат (6 маленьких чисел),
            # не содержание сообщения. Безусловно — то есть даже если вызывающий
            # по ошибке прислал text вместе с уже готовым appraisal: правило
            # «текст не покидает ядро» не должно иметь обходного пути через
            # чужую ошибку. Тот же принцип, что «вывод карточки не попадает в
            # память как текст» — числа наружу, текст остаётся снаружи ядра.
            # Побочный эффект — реплей остаётся детерминированным и офлайновым:
            # он читает уже посчитанный appraisal из журнала и никогда не
            # вызывает сеть повторно.
            text = ev.payload.pop("text")
            if "appraisal" not in ev.payload:
                ev.payload["appraisal"] = self.appraise_text_now(text, ev.t)

        self.journal.write("event", ev.t, ev.to_dict())

        if ev.kind == "user_message":
            self.state.last_contact_t = ev.t
            self.state.last_context_t = ev.t
            self.bg.note_answer(self.state)
            # Контакт восстановлен — часть накопленного штрафа прощается.
            # Штраф снимается возвращением человека, а не течением времени.
            self.state.act_penalty *= 0.5
        elif ev.kind == "assistant_message":
            self.state.last_context_t = ev.t
        elif ev.kind == "sensor":
            self.state.somatic = self.ap.somatic_update(self.state.somatic, ev.payload)

        imps = self.ap.impulses(ev)
        for imp in imps:
            applied = self.h.apply_impulse(self.state, imp)
            self.journal.write(
                "impulse", ev.t, {**imp.to_dict(), "applied": round(applied, 5)}
            )
        return imps

    def consummate(self, template_id: str, verified: bool, cost: float = 0.0) -> float:
        """Засчитать выполнение задачи. Верификацию делает вызывающий КОД, не модель."""
        task = self.rep.pop(template_id)
        if task is None:
            self.journal.write(
                "error", self.state.t, {"what": "consummation_without_task", "id": template_id}
            )
            return 0.0
        delta = self.h.consummate(self.state, task.drive, verified, template_id)
        self.rep.record(template_id, delta, cost, verified)
        self.journal.write(
            "consummation",
            self.state.t,
            {"template_id": template_id, "drive": task.drive, "verified": verified,
             "delta": round(delta, 5), "cost": cost},
            self.state.snapshot(),
        )
        return delta

    def note_llm_call(self, model: str, purpose: str, tokens_in: int = 0,
                      tokens_out: int = 0, template_id: str = "") -> None:
        """Зарегистрировать вызов языковой модели.

        Ядро само модель не вызывает — это делает исполнитель. Но лог обязан знать
        обо всех вызовах: иначе стоимость и эффективность шаблонов не посчитать.
        """
        self.journal.write("llm_call", self.state.t, {
            "model": model, "purpose": purpose, "tokens_in": tokens_in,
            "tokens_out": tokens_out, "template_id": template_id,
        })

    def refund_initiation(self) -> None:
        """Инициация не состоялась (ошибка канала) — вернуть токен."""
        st = self.state
        st.tokens = min(self.cfg["budget"]["capacity"], st.tokens + 1.0)
        st.initiation_pending = False

    # ------------------------------------------------------------------- тик

    def tick(self, t: Optional[float] = None) -> Decision:
        st = self.state
        t = self.clock.now() if t is None else t
        if not st.has_finite_vector():
            # Вектор стал NaN/Inf (порча снапшота, проскочивший коэффициент).
            # Само-восстановление: числа — к дефолтам конфига, часы сохраняем.
            # Тихо продолжать с NaN хуже, чем потерять накопленное; вечно падать
            # на каждом тике — тоже.
            self.journal.write("error", t, {"what": "non_finite_state_reset"})
            st.drives = {n: self.cfg["drives"][n]["setpoint"] for n in self.cfg["drives"]}
            st.modulators = dict(self.cfg["temperament"])
            st.somatic = {k: self.cfg["somatic"][k] for k in st.somatic}
            st.tokens = float(self.cfg["budget"]["capacity"])
            st.act_penalty = 0.0
            if not isinstance(st.t, (int, float)) or not math.isfinite(st.t):
                st.t = t
        dt = self.h.advance(st, t)
        self.bg.refill(st, dt)
        if self.bg.check_unanswered(st):
            # Знак принципиален: молчание в ответ ПОВЫШАЕТ порог следующей инициации.
            self.journal.write("event", t, {"kind": "initiation_unanswered",
                                            "penalty": round(st.act_penalty, 4)})

        gate = self.gk.evaluate(st)
        if gate.regime != self._last_regime:
            self.journal.write("gate_change", t,
                               {"from": self._last_regime, "to": gate.regime},
                               st.snapshot())
            self._last_regime = gate.regime

        card = self.vb.render(st, gate)
        # Карточка пишется отдельной записью при каждом изменении текста: она —
        # единственное, что видит модель, и без неё лог не отвечает на вопрос
        # «почему она это сказала».
        if card.text != self._last_card:
            self.journal.write("card", t, {"text": card.text, "regime": card.regime,
                                           "dropped": card.dropped})
            self._last_card = card.text

        tier, task, reason = self._decide(st, gate)

        st.seq = self.journal.write(
            "tick", t,
            {"dt": round(dt, 3), "tier": tier, "arousal": round(self.h.arousal(st), 4),
             "activation": round(gate.activation, 4), "reason": reason,
             "gate": {"regime": gate.regime, "may_initiate": gate.may_initiate,
                      "max_tokens": gate.max_tokens, "forbidden": list(gate.forbidden),
                      "context_band": gate.context_band}},
            st.snapshot(),
        )
        return Decision(tier, gate, card, task, self._next_tick_s(st, task), reason)

    def _decide(self, st: State, gate: Gate):
        hb = self.cfg["heartbeat"]
        act = gate.activation

        if gate.may_initiate and act >= self.bg.theta_act_eff(self.cfg, st):
            ok, why = self.bg.may_initiate(st)
            if ok:
                # Токен списывается сразу: если вызывающий не воспользуется правом,
                # оно просто пропадёт. Fail-closed — так спам невозможен даже при
                # ошибке в вызывающем коде. Есть refund_initiation() для сбоев канала.
                self.bg.spend(st)
                return 2, None, "initiate"
            reason = why
        else:
            reason = "below_theta_act" if gate.may_initiate else "gate_closed"

        if act >= hb["theta_task"]:
            if st.t - st.last_task_t < hb["task_min_interval_s"]:
                return 0, None, reason + "|task_refractory"
            task = self.rep.select(st, gate)
            if task is not None:
                st.last_task_t = st.t
                self.journal.write("task", st.t, task.to_dict())
                return 1, task, "task:" + task.template_id
            return 0, None, reason + "|no_task"
        return 0, None, reason

    def _next_tick_s(self, st: State, task: Optional[Task]) -> float:
        hb = self.cfg["heartbeat"]
        if task is not None or self.rep.queue:
            return float(hb["tick_min_s"])
        a = self.h.arousal(st)
        span = hb["tick_max_s"] - hb["tick_min_s"]
        return _clip(hb["tick_min_s"] + span * (1.0 - a) ** hb["gamma"],
                     hb["tick_min_s"], hb["tick_max_s"])

    # -------------------------------------------------------------------- сон

    def maybe_sleep(self, force: bool = False) -> SleepReport:
        st = self.state
        s = self.cfg["sleep"]
        if not force:
            if st.t - st.last_sleep_t < s["period_s"]:
                self._sleep_deferred_logged = False
                return SleepReport(False, "too_early", [])
            if self.h.arousal(st) > s["max_arousal"]:
                # Отложенный сон логируется: это индикатор незатухающей петли.
                # Один раз за эпизод — сброс флага при удачном сне или при
                # возврате в "too_early" после следующего периода.
                if not self._sleep_deferred_logged:
                    self.journal.write("sleep", st.t, {"deferred": True,
                                                       "arousal": round(self.h.arousal(st), 4)})
                    self._sleep_deferred_logged = True
                return SleepReport(False, "aroused", [])

        lam = s["lambda"]
        for k, base in self.cfg["temperament"].items():
            st.modulators[k] += lam * (base - st.modulators[k])
        for k in list(st.habituation):
            st.habituation[k] *= 0.5
        self.rep.expire(st.t, st)
        st.last_sleep_t = st.t
        self._sleep_deferred_logged = False

        eff = self.rep.efficacy_report()
        self.journal.write("sleep", st.t, {"deferred": False, "templates": len(eff)},
                           st.snapshot())
        # Курирование репертуара моделью (шаг 2 ночного цикла) — не здесь:
        # отчёт возвращается наружу (SleepReport.efficacy), решение и вызов
        # модели — Service.run_curation() в daemon.py. L0/L1 (Homeostat,
        # Engine) сетевого доступа не имеют и не должны — см. motus/curator.py.
        return SleepReport(True, "ok", eff)
