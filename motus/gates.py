"""L1 — гейткиперы. Числа → режим и capability mask. Полностью детерминированно.

Гейт — не подсказка модели, а блокировка ветки кода. Если may_initiate=False,
исходящее сообщение физически не отправляется: движок до этой ветки не доходит.

REGIME_POLICY — «ядро драйва» из docs/01-structure.md §6: константа в коде,
которую не правит ни модель, ни ночной цикл, ни конфиг.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from .config import DRIVES
from .core import Homeostat
from .state import State

#: Классы инструментов, которыми оперирует маска.
TOOLS_ALL = ("read", "memory", "exec", "write", "net", "outbound")


@dataclass(frozen=True)
class Policy:
    may_initiate: bool
    max_tokens: int
    allowed_tools: Tuple[str, ...]
    forbidden: Tuple[str, ...]


REGIME_POLICY: Dict[str, Policy] = {
    "baseline": Policy(False, 900, TOOLS_ALL, ()),
    "SEEKING":  Policy(True,  900, TOOLS_ALL, ()),
    "CARE":     Policy(True,  700, TOOLS_ALL, ()),
    # PLAY не даёт права перебивать: играть можно, вторгаться — нет.
    "PLAY":     Policy(False, 500, ("read", "memory", "write"), ("outbound",)),
    # Страх сужает полномочия: проверять можно, ломать нельзя.
    "FEAR":     Policy(False, 400, ("read", "memory", "exec"), ("irreversible", "promises")),
    # Злость сокращает полномочия до минимума. Это и реализм (импульс-контроль),
    # и безопасность: злой бот теряет право писать, а не получает его.
    "RAGE":     Policy(False, 250, ("read",), ("outbound", "irreversible", "write", "net")),
    # PANIC — единственный аверсивный драйв с правом инициации: в этом его смысл.
    # Ограничивают его бюджет, рефрактерность и штраф за молчание, а не гейт.
    "PANIC":    Policy(True,  180, ("read", "memory"), ("new_topics", "long_form")),
}


@dataclass
class Gate:
    regime: str
    activation: float
    may_initiate: bool
    max_tokens: int
    allowed_tools: Tuple[str, ...]
    forbidden: Tuple[str, ...]
    context_band: str
    somatic_flags: Tuple[str, ...] = ()

    def allows(self, tool: str) -> bool:
        return tool in self.allowed_tools

    def to_dict(self) -> Dict[str, Any]:
        return {
            "regime": self.regime,
            "activation": round(self.activation, 4),
            "may_initiate": self.may_initiate,
            "max_tokens": self.max_tokens,
            "allowed_tools": list(self.allowed_tools),
            "forbidden": list(self.forbidden),
            "context_band": self.context_band,
            "somatic_flags": list(self.somatic_flags),
        }


class Gatekeeper:
    def __init__(self, cfg: Dict[str, Any], homeostat: Homeostat) -> None:
        self.cfg = cfg
        self.h = homeostat

    # ------------------------------------------------------------ защёлки

    def update_latches(self, st: State) -> None:
        """Гистерезис §6: вход по θ_hi, выход по θ_lo. Без него режим дребезжит."""
        for name in DRIVES:
            hi, lo = self.h.theta_eff(st, name)
            x = st.drives[name]
            if st.latched.get(name):
                if x < lo:
                    st.latched[name] = False
            else:
                if x >= hi:
                    st.latched[name] = True

    def _candidate(self, st: State) -> str:
        active = [n for n in DRIVES if st.latched.get(n)]
        if not active:
            return "baseline"
        return max(
            active,
            key=lambda n: (self.cfg["drives"][n]["priority"], self.h.activation(st, n)),
        )

    def _priority(self, regime: str) -> int:
        return self.cfg["drives"][regime]["priority"] if regime in self.cfg["drives"] else 0

    def select_regime(self, st: State) -> Tuple[str, bool]:
        """Возвращает (режим, сменился ли). Применяет dwell с override по приоритету."""
        self.update_latches(st)
        cand = self._candidate(st)
        if cand == st.regime:
            return st.regime, False

        held = st.t - st.regime_since
        dwell_ok = held >= self.cfg["thresholds"]["dwell_s"]
        # Намеренный override: аверсивный драйв строго более высокого приоритета
        # прерывает что угодно немедленно. Страх должен обрывать игру, не дожидаясь dwell.
        override = self._priority(cand) > self._priority(st.regime)
        if not (dwell_ok or override):
            return st.regime, False

        st.regime = cand
        st.regime_since = st.t
        return cand, True

    # ---------------------------------------------------------------- маска

    def _somatic_flags(self, st: State) -> Tuple[str, ...]:
        flags: List[str] = []
        if self.h.energy(st) < 0.35:
            flags.append("low_energy")
        if st.somatic["thermal"] > 0.6:
            flags.append("hot")
        if st.somatic["integrity"] < 0.7:
            flags.append("degraded")
        return tuple(flags)

    def evaluate(self, st: State) -> Gate:
        regime, _ = self.select_regime(st)
        pol = REGIME_POLICY[regime]
        band = self.h.context_band(st)
        forbidden = list(pol.forbidden)
        max_tokens = pol.max_tokens
        tools = pol.allowed_tools

        flags = self._somatic_flags(st)
        if "low_energy" in flags or "hot" in flags:
            max_tokens = min(max_tokens, 350)
        if "degraded" in flags and "irreversible" not in forbidden:
            forbidden.append("irreversible")

        activation = (
            self.h.activation(st, regime) if regime in self.cfg["drives"] else 0.0
        )
        return Gate(
            regime=regime,
            activation=activation,
            may_initiate=pol.may_initiate,
            max_tokens=max_tokens,
            allowed_tools=tools,
            forbidden=tuple(forbidden),
            context_band=band,
            somatic_flags=flags,
        )
