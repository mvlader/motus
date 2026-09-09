"""Бюджет инициаций: токен-бакет (token bucket), рефрактерный период, штраф за молчание. §7.

Защита от спама, не зависящая от разумности модели. Знак штрафа принципиален:
неотвеченная инициация ПОВЫШАЕТ порог следующей.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from .state import State


class Budget:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg["budget"]

    def refill(self, st: State, dt: float) -> None:
        st.tokens = min(self.cfg["capacity"], st.tokens + self.cfg["refill_per_s"] * dt)

    def theta_act_eff(self, cfg: Dict[str, Any], st: State) -> float:
        return cfg["heartbeat"]["theta_act"] + st.act_penalty

    def may_initiate(self, st: State) -> Tuple[bool, str]:
        """Возвращает (можно, причина отказа). Причина уходит в журнал."""
        if st.initiation_pending:
            # Второе сообщение не отправляется, пока первое не получило ответа и
            # не разрешилось штрафом. Без этого правила бюджет всё равно
            # выпускает по инициации в час — то есть спам.
            return False, "awaiting_answer"
        if st.tokens < 1.0:
            return False, "budget_empty"
        if st.t - st.last_initiation_t < self.cfg["refractory_s"]:
            return False, "refractory"
        return True, ""

    def spend(self, st: State) -> None:
        st.tokens -= 1.0
        st.last_initiation_t = st.t
        st.initiation_pending = True

    def note_answer(self, st: State) -> None:
        """Пользователь ответил — инициация засчитана как удачная."""
        st.initiation_pending = False

    def check_unanswered(self, st: State) -> bool:
        """Инициация провисела без ответа дольше окна — штрафуем. Вызывается на тике."""
        if not st.initiation_pending:
            return False
        if st.t - st.last_initiation_t < self.cfg["unanswered_after_s"]:
            return False
        st.initiation_pending = False
        st.act_penalty = min(
            self.cfg["penalty_max"], st.act_penalty + self.cfg["penalty_delta"]
        )
        return True
