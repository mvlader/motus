"""Часы. Реальные и виртуальные — второе нужно реплею и тестам.

Ядро НИКОГДА не вызывает time.time() напрямую: всё время приходит через Clock.
Иначе реплей перестаёт быть детерминированным.
"""

from __future__ import annotations

import math
import time


class Clock:
    """Реальное время.

    local_hour считается через фиксированное смещение от UTC, а не через localtime():
    так реплей воспроизводится точно, а переход на летнее время не сдвигает
    циркадную фазу на час дважды в год. Смещение фиксируется при создании часов
    и пишется в журнал при загрузке.
    """

    def __init__(self, tz_offset_s: float | None = None) -> None:
        if tz_offset_s is None:
            lt = time.localtime()
            tz_offset_s = float(-time.timezone + (3600 if lt.tm_isdst > 0 else 0))
        self.tz_offset_s = float(tz_offset_s)

    def now(self) -> float:
        return time.time()

    def local_hour(self, t: float) -> float:
        return ((t + self.tz_offset_s) % 86400.0) / 3600.0


class VirtualClock(Clock):
    """Управляемое время для тестов и реплея. Сутки прогоняются за миллисекунды."""

    def __init__(self, t: float = 0.0, tz_offset_s: float = 0.0) -> None:
        super().__init__(tz_offset_s)
        self.t = float(t)

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


def circadian_energy(hour: float, amp: float, peak_hour: float) -> float:
    """Суточная синусоида ∈ [0.5-amp, 0.5+amp]. Пик в peak_hour."""
    return 0.5 + amp * math.cos(2.0 * math.pi * (hour - peak_hour) / 24.0)


def freshness(age_s: float, tau_s: float) -> float:
    """Свежесть контекста: exp(-age/tau) ∈ (0, 1]."""
    if age_s <= 0.0:
        return 1.0
    return math.exp(-age_s / tau_s)


def context_band(f: float, band_fresh: float, band_aging: float, band_stale: float) -> str:
    if f > band_fresh:
        return "fresh"
    if f > band_aging:
        return "aging"
    if f > band_stale:
        return "stale"
    return "expired"


#: Границы «человеческих» формулировок времени. Ни одной цифры не уходит в промпт —
#: вербализатор берёт отсюда ключ и подставляет фразу из лексикона.
_CONTACT_BUCKETS = (
    (120.0, "now"),
    (600.0, "minutes"),
    (2700.0, "under_hour"),
    (7200.0, "hour"),
    (14400.0, "hours"),
    (28800.0, "half_day"),
    (72000.0, "today_old"),
    (129600.0, "yesterday"),
    (259200.0, "day_before"),
)


def contact_bucket(silence_s: float) -> str:
    for edge, key in _CONTACT_BUCKETS:
        if silence_s < edge:
            return key
    return "long"
