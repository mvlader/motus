"""Вектор состояния и его сериализация. Никакой динамики — только данные."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict

from .config import DRIVES, MODULATORS, SOMATIC


@dataclass
class State:
    t: float = 0.0
    seq: int = 0

    drives: Dict[str, float] = field(default_factory=dict)
    modulators: Dict[str, float] = field(default_factory=dict)
    somatic: Dict[str, float] = field(default_factory=dict)

    #: счётчики привыкания: ключ источника/шаблона -> n
    habituation: Dict[str, float] = field(default_factory=dict)
    #: защёлки гистерезиса: драйв -> активен ли
    latched: Dict[str, bool] = field(default_factory=dict)

    regime: str = "baseline"
    regime_since: float = 0.0

    last_contact_t: float = 0.0
    last_context_t: float = 0.0

    #: бюджет инициаций
    tokens: float = 0.0
    last_initiation_t: float = -1e12
    initiation_pending: bool = False
    act_penalty: float = 0.0

    last_task_t: float = -1e12
    last_sleep_t: float = 0.0

    @classmethod
    def initial(cls, cfg: Dict[str, Any], t: float) -> "State":
        return cls(
            t=t,
            drives={n: cfg["drives"][n]["setpoint"] for n in DRIVES},
            modulators=dict(cfg["temperament"]),
            somatic={k: cfg["somatic"][k] for k in SOMATIC},
            latched={n: False for n in DRIVES},
            regime_since=t,
            last_contact_t=t,
            last_context_t=t,
            tokens=cfg["budget"]["capacity"],
            last_sleep_t=t,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "State":
        return cls(**d)

    def snapshot(self) -> Dict[str, Any]:
        """Компактный снимок для журнала: округлён, но полон."""
        return {
            "t": round(self.t, 3),
            "drives": {k: round(v, 5) for k, v in self.drives.items()},
            "modulators": {k: round(v, 4) for k, v in self.modulators.items()},
            "somatic": {k: round(v, 4) for k, v in self.somatic.items()},
            "regime": self.regime,
            "tokens": round(self.tokens, 4),
            "act_penalty": round(self.act_penalty, 4),
        }
