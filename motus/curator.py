"""Ночной цикл, шаг 2 — курирование репертуара моделью.

Из четырёх шагов ночного цикла (docs/01-structure.md §7) первый и третий делает
код (`engine.maybe_sleep()` — реплей/эффективность, ренормализация), четвёртый
не переписывается (штатная консолидация памяти openclaw). Этот модуль — второй:
единственное место в проекте, где модели разрешено МЕНЯТЬ данные, а не только
читать карточку. Разрешено — не значит бесконтрольно:

  * Модель предлагает — `propose_edits()`, структурированный ответ по жёсткой
    схеме (та же техника, что у L-1: grammar-constrained JSON, не диалог).
  * Код проверяет и применяет — `Repertoire.apply_edits()`, полная валидация,
    ничего не принимается на веру.
  * Квота — не больше `QUOTA_PER_NIGHT` (3) правок за цикл.
  * Ядро драйва (drive/consummation/preconditions уже существующего шаблона)
    модель физически не может тронуть — в схеме правки "rewrite" этих полей нет.
  * Пространство значений для НОВОГО шаблона сужено заранее (CURATABLE_DRIVES,
    KNOWN_CONSUMMATION_TYPES, PRECONDITIONS) — тот же fail-closed принцип, что
    у выбора задачи (`Repertoire._precondition_ok`).

Вызывается ТОЛЬКО из Service (daemon.py) после успешного (не отложенного) сна —
у Engine/Homeostat нет и не будет сетевого доступа, L0 обязана оставаться чистой
математикой. Отказ модели (сеть, таймаут, невалидная схема) — цикл куратора
просто пропускается, репертуар остаётся как был; это тот же принцип «отказ
сенсора не двигает состояние», что и у L-1.

Только stdlib.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from . import modelcall
from .repertoire import CURATABLE_DRIVES, KNOWN_CONSUMMATION_TYPES, PRECONDITIONS, QUOTA_PER_NIGHT

#: Промпт курирования. В отличие от SENSOR_PROMPT (L-1, классификация одного
#: сообщения) — это не оценка, а предложение: модель видит, что из репертуара
#: работает (mean_delta, success_rate), а что нет, и вправе переформулировать,
#: добавить или архивировать в пределах квоты и жёстко заданного словаря
#: значений (перечислен явно в промпте, а не только в схеме — модель должна
#: видеть границы, а не угадывать их по отказам схемы).
CURATION_PROMPT = f"""Ты не отвечаешь пользователю — ты раз в сутки просматриваешь
репертуар: то, чем система занимается сама с собой в фоне, когда не разговаривает
с человеком. Это не список поручений и не задачи для пользователя — здесь не
нужно оправдание пользой ни для одного пункта. Ниже — текущие шаблоны и то, как
они реально сработали за последние сутки: mean_delta (насколько шаблон снижал
влечение драйва — больше по модулю лучше), success_rate (доля успешных
выполнений), n (сколько раз бралась). У шаблона с n < 3 измерений мало — не
суди его строго.

Тебе можно (и только это):
  * "rewrite" — переписать prompt и/или rationale существующего шаблона по его id.
    Драйв, тип консумации и предусловия шаблона трогать нельзя — их в правке нет.
  * "add" — предложить новый шаблон: id (латиница/цифры/подчёркивание, строчными,
    3-64 симв.), drive (один из: {", ".join(CURATABLE_DRIVES)}),
    consummation_type (один из: {", ".join(KNOWN_CONSUMMATION_TYPES)} — "reflection"
    не требует артефакта, годится для того, что не обязано ничем закончиться),
    preconditions (подмножество: {", ".join(PRECONDITIONS)}), prompt, rationale.
  * "archive" — убрать шаблон по id, если он раз за разом не работает.

Не больше {QUOTA_PER_NIGHT} правок за раз. Если менять нечего — верни пустой список.
Не выдумывай id, drive, consummation_type или preconditions вне перечисленного —
такая правка будет отклонена кодом целиком.

Верни ТОЛЬКО JSON-массив правок.

ТЕКУЩИЙ РЕПЕРТУАР И ЭФФЕКТИВНОСТЬ:
"""


def _edit_item_schema() -> Dict[str, Any]:
    """Одна плоская схема на все три вида правки — грамматике так надёжнее
    держаться, чем oneOf/anyOf; семантическую проверку по op всё равно делает
    Repertoire._apply_one(), эта схема — только первый (мягкий) рубеж, не
    единственный, ровно как у Appraisal.json_schema() для L-1."""
    return {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["rewrite", "add", "archive"]},
            "id": {"type": "string"},
            "drive": {"type": "string", "enum": list(CURATABLE_DRIVES)},
            "consummation_type": {"type": "string", "enum": list(KNOWN_CONSUMMATION_TYPES)},
            "preconditions": {"type": "array", "items": {"type": "string", "enum": list(PRECONDITIONS)}},
            "prompt": {"type": "string"},
            "rationale": {"type": "string"},
        },
        "required": ["op", "id"],
    }


def edits_schema() -> Dict[str, Any]:
    return {"type": "array", "items": _edit_item_schema(), "maxItems": QUOTA_PER_NIGHT}


def make_sensor(cfg: Dict[str, Any]) -> Callable[[str], Any]:
    """Собрать callable для курирования — тот же api-диспетчер (llamacpp|ollama),
    что и у L-1 (appraisal.make_sensor), но своя схема и не тот, обязательно,
    провайдер: курирование раз в сутки, задержка в 30-60 с не критична, и это
    рассуждение сложнее классификации одного сообщения — здесь уместна модель
    посильнее, чем для L-1 (см. docs/04-model-l1.md, «curation»)."""
    api = cfg.get("api", "ollama")
    base_url = cfg["base_url"]
    timeout_s = float(cfg.get("timeout_s", 60.0))
    num_predict = int(cfg.get("num_predict", 800))
    temperature = float(cfg.get("temperature", 0.2))

    if api == "llamacpp":
        def sensor(prompt: str) -> Any:
            return modelcall.llamacpp_complete(
                base_url, prompt, edits_schema(),
                timeout_s=timeout_s, num_predict=num_predict, temperature=temperature,
            )
        return sensor
    if api == "ollama":
        model = cfg["model"]

        def sensor(prompt: str) -> Any:
            return modelcall.ollama_generate(
                base_url, model, prompt, edits_schema(),
                timeout_s=timeout_s, num_predict=num_predict, temperature=temperature,
            )
        return sensor
    raise ValueError(f"curation.api: неизвестное значение {api!r} (llamacpp|ollama)")


def propose_edits(sensor: Callable[[str], Any], efficacy_report: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Один вызов модели → список ПРЕДЛОЖЕНИЙ (ещё не применённых — это делает
    Repertoire.apply_edits). Любой отказ (сеть, битый JSON, не список) → пустой
    список: цикл куратора просто ничего не меняет в эту ночь, тихо и безопасно."""
    prompt = CURATION_PROMPT + json.dumps(efficacy_report, ensure_ascii=False, indent=1)
    try:
        raw = sensor(prompt)
    except Exception:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    return raw
