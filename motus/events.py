"""Типы событий и импульсов.

Событие — то, что случилось в мире. Импульс — то, во что L-1 его перевёл.
Ядро принимает только импульсы: событие само по себе состояние не двигает.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

#: Виды событий, известные системе. Неизвестный вид — не ошибка, но и не импульс:
#: он попадает в журнал и игнорируется ядром.
KINDS = (
    "user_message",       # пришло сообщение от пользователя
    "assistant_message",  # ответ отправлен
    "silence_probe",      # тик заметил тишину (генерируется движком)
    "tool_error",         # инструмент упал
    "task_result",        # фоновая задача завершилась
    "sensor",             # датчик железа
    "net_down",           # сеть недоступна
    "net_up",             # сеть вернулась
    "operator",           # ручное вмешательство человека
)


@dataclass
class Event:
    kind: str
    t: float
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "t": self.t, "payload": self.payload}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Event":
        return cls(kind=d["kind"], t=d["t"], payload=d.get("payload", {}))


@dataclass
class Impulse:
    """Приращение драйва. amplitude ∈ [-1, 1], до применения гейнов и габитуации."""

    drive: str
    amplitude: float
    key: str  # ключ габитуации

    def to_dict(self) -> Dict[str, Any]:
        return {"drive": self.drive, "amplitude": round(self.amplitude, 5), "key": self.key}


@dataclass
class Appraisal:
    """Строгая схема выхода L-1. Любое отклонение → всё в ноль."""

    valence: int = 0          # -2..2
    threat: int = 0           # 0..2
    novelty: int = 0          # 0..2
    social_warmth: int = 0    # -2..2
    loss: int = 0             # 0..2
    agency_blocked: bool = False

    RANGES = {
        "valence": (-2, 2),
        "threat": (0, 2),
        "novelty": (0, 2),
        "social_warmth": (-2, 2),
        "loss": (0, 2),
    }

    @classmethod
    def parse(cls, raw: Any) -> "Appraisal":
        """Валидация с падением в нули. Отказ сенсора не должен двигать состояние."""
        if not isinstance(raw, dict):
            return cls()
        out: Dict[str, Any] = {}
        for field_name, (lo, hi) in cls.RANGES.items():
            v = raw.get(field_name, 0)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return cls()
            iv = int(round(v))
            if not lo <= iv <= hi:
                return cls()
            out[field_name] = iv
        blocked = raw.get("agency_blocked", False)
        if not isinstance(blocked, bool):
            return cls()
        out["agency_blocked"] = blocked
        return cls(**out)

    def is_null(self) -> bool:
        return (
            self.valence == 0 and self.threat == 0 and self.novelty == 0
            and self.social_warmth == 0 and self.loss == 0 and not self.agency_blocked
        )

    @classmethod
    def json_schema(cls) -> Dict[str, Any]:
        """JSON-схема выхода L-1, сгенерированная из RANGES — одна точка правды с
        parse(). Уходит рантайму L-1 как json_schema (llama.cpp) или format
        (ollama): грамматика ограничивает декодирование самим набором допустимых
        значений, а не только синтаксисом JSON. Это защита до parse(), а не вместо
        неё — parse() остаётся последним рубежом на случай сенсора, который эту
        схему не умеет."""
        props = {
            name: {"type": "integer", "enum": list(range(lo, hi + 1))}
            for name, (lo, hi) in cls.RANGES.items()
        }
        props["agency_blocked"] = {"type": "boolean"}
        return {
            "type": "object",
            "properties": props,
            "required": list(cls.RANGES) + ["agency_blocked"],
        }
