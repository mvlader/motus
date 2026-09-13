"""Вектор состояния и его сериализация. Никакой динамики — только данные."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict

from .config import DRIVES, MODULATORS, SOMATIC


def _assert_finite(node: Any, path: str = "state") -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            _assert_finite(v, f"{path}.{k}")
    elif isinstance(node, bool):
        pass
    elif isinstance(node, float) and not math.isfinite(node):
        raise ValueError(f"нечисловое значение в снапшоте: {path} = {node}")


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

    #: сколько FEAR сейчас накоплено именно от роста лимита Claude — прощается
    #: РОВНО этим значением (не всем FEAR) на сброс лимита, см. engine.py.
    limit_fear_added: float = 0.0
    #: unix-время следующего сброса 5-часового лимита Claude, по последним
    #: данным датчика (0.0 = неизвестно). Только для расчёта сообщения на
    #: публичном API — само число наружу не идёт, идёт готовый текст.
    claude_limit_reset_at: float = 0.0

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
        """Восстановить состояние из снапшота. Бросает ValueError при NaN/Inf в
        любом числовом поле: json.load принимает эти литералы молча, а битый
        var/state.json (порча SD-карты на Pi — не гипотетика) иначе тихо отравил
        бы весь вектор. Вызывающий (daemon._load_state) ловит и стартует с чистого
        состояния, залогировав факт."""
        _assert_finite(d)
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def has_finite_vector(self) -> bool:
        """Быстрая проверка живого состояния: все драйвы/модуляторы/сома конечны
        и в разумных пределах. Дешёвый инвариант для tick()."""
        for v in (*self.drives.values(), *self.modulators.values(),
                  *self.somatic.values(), self.tokens, self.act_penalty, self.t,
                  self.limit_fear_added, self.claude_limit_reset_at):
            if not math.isfinite(v):
                return False
        return True

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
