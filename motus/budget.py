"""Бюджет инициаций: токен-бакет (token bucket), рефрактерный период, штраф за молчание. §7.

Защита от спама, не зависящая от разумности модели. Знак штрафа принципиален:
неотвеченная инициация ПОВЫШАЕТ порог следующей.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .state import State


def _in_window(hour: float, start: float, end: float) -> bool:
    """Час внутри окна [start, end). Окно через полночь (23→7) тоже работает."""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


class Budget:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg["budget"]

    def refill(self, st: State, dt: float) -> None:
        st.tokens = min(self.cfg["capacity"], st.tokens + self.cfg["refill_per_s"] * dt)

    def theta_act_eff(self, cfg: Dict[str, Any], st: State) -> float:
        return cfg["heartbeat"]["theta_act"] + st.act_penalty

    def may_initiate(self, st: State, local_hour: Optional[float] = None) -> Tuple[bool, str]:
        """Возвращает (можно, причина отказа). Причина уходит в журнал.

        local_hour (2026-09-15) — локальный час оператора для «тихих часов».
        None означает «часы не переданы» и отключает проверку: так старый
        вызывающий код и тесты не ломаются, но боевой путь (engine._decide)
        передаёт час всегда.
        """
        quiet = self.cfg.get("quiet_hours") or {}
        if quiet.get("enabled") and local_hour is not None:
            grace_s = float(quiet.get("grace_after_contact_s", 0.0))
            silence_s = st.t - st.last_contact_t
            # Свежий контакт снимает тихие часы: если человек сам написал,
            # значит он не спит, и молчать «из уважения ко сну» незачем.
            if silence_s >= grace_s and _in_window(
                local_hour,
                float(quiet.get("start_hour", 0.0)),
                float(quiet.get("end_hour", 0.0)),
            ):
                return False, "quiet_hours"
        if not self.cfg.get("initiation_enabled", True):
            # Канала доставки проактивных сообщений (Tier 2) ещё нет: плагин
            # openclaw читает только карточку и события. При initiation_enabled=false
            # движок не тратит токен и не ставит initiation_pending — иначе
            # каждая инициация «висит без ответа» ровно потому, что её никто не
            # отправлял, и через unanswered_after_s накручивается act_penalty.
            # Осознанный шаг фазы 2 «блокировка проактивных сообщений».
            return False, "initiation_disabled"
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
