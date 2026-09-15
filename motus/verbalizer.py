"""L2 — вербализатор. Режим и гейт → карточка для промпта.

Три инварианта, каждый проверяется тестом:
  1. НИ ОДНОЙ ЦИФРЫ. Время выражается словами из лексикона.
  2. Не длиннее бюджета: лишние сегменты отбрасываются по приоритету.
  3. Директивы — императивы о действии, а не описания чувств. Описание чувства
     модель отыграет одной репликой и забудет; директиву либо выполняет, либо нет.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from .clock import contact_bucket
from .core import Homeostat
from .gates import Gate
from .state import State

_DIGIT = re.compile(r"\d")

#: Приоритет сегментов при обрезке под бюджет. Меньше — важнее, отбрасывается позже.
_P_CONSTRAINT = 0
_P_DIRECTIVE0 = 1
_P_TIME = 2
_P_TONE = 3
_P_DIRECTIVE1 = 4
_P_SOMATIC = 5


@dataclass
class Card:
    text: str
    regime: str
    dropped: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "regime": self.regime, "dropped": self.dropped,
                "chars": len(self.text)}


class CardError(AssertionError):
    pass


class Verbalizer:
    def __init__(self, cfg: Dict[str, Any], homeostat: Homeostat) -> None:
        self.cfg = cfg
        self.lex = cfg["_lexicon"]
        self.h = homeostat
        self.max_chars = cfg["verbalizer"]["max_chars"]

    def render(self, st: State, gate: Gate) -> Card:
        segs: List[Tuple[int, str]] = []

        # Время: одной фразой, словами. Ключ выбирает код, текст берётся из лексикона.
        contact = self.lex["contact"][contact_bucket(self.h.silence_s(st))]
        band = self.lex["context_band"][gate.context_band]
        segs.append((_P_TIME, f"{contact}; {band}."))

        reg = self.lex["regimes"][gate.regime]
        label = self.lex.get("state_label", "Состояние")
        segs.append((_P_TONE, f"{label}: {reg['tone']}."))
        for i, d in enumerate(reg["directives"][:2]):
            segs.append((_P_DIRECTIVE0 if i == 0 else _P_DIRECTIVE1, d))

        for f in gate.forbidden:
            phrase = self.lex["constraints"].get(f)
            if phrase:
                segs.append((_P_CONSTRAINT, phrase))

        for flag in gate.somatic_flags:
            phrase = self.lex["somatic"].get(flag)
            if phrase:
                segs.append((_P_SOMATIC, phrase[0].upper() + phrase[1:]))

        # Дедупликация с сохранением наивысшего приоритета.
        best: Dict[str, int] = {}
        for pri, text in segs:
            if text not in best or pri < best[text]:
                best[text] = pri
        ordered = sorted(best.items(), key=lambda kv: (kv[1], kv[0]))

        kept: List[Tuple[int, str]] = []
        total = 0
        dropped = 0
        for text, pri in ordered:
            add = len(text) + (1 if kept else 0)
            if total + add > self.max_chars:
                dropped += 1
                continue
            kept.append((pri, text))
            total += add

        kept.sort(key=lambda kv: kv[0])
        # Порядок вывода читаемый: время → состояние → директивы → запреты.
        text = " ".join(t for _, t in sorted(kept, key=lambda kv: _READ_ORDER.get(kv[0], 9)))
        self._assert_clean(text)
        return Card(text=text, regime=gate.regime, dropped=dropped)

    @staticmethod
    def _assert_clean(text: str) -> None:
        if _DIGIT.search(text):
            raise CardError(
                "в карточке появилась цифра — инвариант «числа не покидают код» нарушен: "
                + text
            )


_READ_ORDER = {
    _P_TIME: 0,
    _P_TONE: 1,
    _P_DIRECTIVE0: 2,
    _P_DIRECTIVE1: 3,
    _P_CONSTRAINT: 4,
    _P_SOMATIC: 5,
}
