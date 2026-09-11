"""Тонкий HTTP-клиент к локальной/LAN модели — grammar-constrained JSON-выход
через llama.cpp `llama-server` или ollama. Ни диалога, ни истории: один
запрос, один структурированный ответ по жёсткой схеме.

Единственная точка правды для "как звать модель по HTTP" в проекте — раньше
это было продублировано внутри appraisal.py; теперь оба вызывающих
(appraisal.py — L-1, curator.py — курирование репертуара) используют этот
модуль. Логика не изменилась ни на строчку — реорганизация, не переписывание;
appraisal.py остаётся зелёным на тех же тестах, что и раньше.

Только stdlib (urllib) — проект не тянет зависимостей ради HTTP-вызова.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict


def llamacpp_complete(
    base_url: str, prompt: str, schema: Dict[str, Any], *,
    timeout_s: float = 8.0, num_predict: int = 96, temperature: float = 0.0,
    cache_prompt: bool = True,
) -> Any:
    """`POST {base_url}/completion` с `json_schema` (GBNF-грамматика у llama.cpp).

    Бросает исключение при сбое сети/таймауте/не-200 — вызывающий сам решает,
    что делать с отказом (Appraiser → нули/fallback, curator → пропустить цикл).
    Здесь ничего не глушится молча.
    """
    base_url = base_url.rstrip("/")
    body = json.dumps({
        "prompt": prompt,
        "json_schema": schema,
        "n_predict": num_predict,
        "temperature": temperature,
        "cache_prompt": cache_prompt,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/completion", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        if resp.status != 200:
            raise urllib.error.HTTPError(
                base_url, resp.status, "llama-server non-200", resp.headers, None
            )
        out = json.loads(resp.read().decode("utf-8"))
    return json.loads(out["content"])


def ollama_generate(
    base_url: str, model: str, prompt: str, schema: Dict[str, Any], *,
    timeout_s: float = 8.0, num_predict: int = 96, temperature: float = 0.0,
) -> Any:
    """`POST {base_url}/api/generate` с `format` = JSON-схема (ollama 0.3.0+)."""
    base_url = base_url.rstrip("/")
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "format": schema,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": num_predict},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/generate", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        if resp.status != 200:
            raise urllib.error.HTTPError(
                base_url, resp.status, "ollama non-200", resp.headers, None
            )
        out = json.loads(resp.read().decode("utf-8"))
    return json.loads(out["response"])
