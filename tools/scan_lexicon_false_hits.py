#!/usr/bin/env python3
"""Найти вероятные ложные срабатывания словаря L-1 (motus/lexicon_l1.py) на
реальных сообщениях: случаи, когда токен совпал со стемом ТОЛЬКО как префикс
(не точное слово и не его прямая словоформа) — самый частый источник
false_alarm вроде "живо" -> "живой" или "отвали" -> "отваливается".

Вход: JSON-файл со списком строк (сообщений). Не решает, ложное ли
совпадение — печатает кандидатов для ручного просмотра.

Использование:
    python3 tools/scan_lexicon_false_hits.py messages.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from motus import lexicon_l1 as L  # noqa: E402

_LEXICONS = ("_VALENCE", "_WARMTH", "_THREAT", "_NOVELTY", "_LOSS", "_BLOCKED")


def scan(messages: list[str]) -> None:
    seen: set[tuple[str, str]] = set()
    for msg in messages:
        norm = L._normalize(msg)
        toks = L._tokens(norm)
        for name in _LEXICONS:
            lex = getattr(L, name)
            for _weight, idx, _has_neg in L._hits(norm, toks, lex):
                tok = toks[idx] if idx < len(toks) else "?"
                key = (name, tok)
                if key in seen:
                    continue
                for stem in lex:
                    s = stem.replace("ё", "е").strip()
                    if " " in s or tok == s:
                        continue
                    if len(s) >= 4 and tok.startswith(s):
                        seen.add(key)
                        print(f"{name}: stem={stem!r} matched token={tok!r} in: {msg[:90]!r}")
                        break


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("Usage: scan_lexicon_false_hits.py messages.json", file=sys.stderr)
        return 2
    messages = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    scan(messages)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
