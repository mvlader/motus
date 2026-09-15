"""L-1 — оценка: события мира → импульсы.

Ветви:
  * правила (детерминированные) — датчики, таймеры, коды ошибок (Appraiser.impulses);
  * оценка текста сообщения — по `Appraiser.mode`:
      "model" (умолчание) — модель как СЕНСОР, выход строго по схеме Appraisal;
      "lexical" — детерминированный словарь `lexicon_l1.py`, без сети;
      "off" — нули.

Модель ничего не рассказывает про эмоции: заполняет шесть полей с известными
диапазонами. Дрейфовать негде, невалидная схема падает в нули и логируется как
appraisal_invalid — отказ сенсора не должен двигать состояние.

История словаря: отключался 2026-09-10 как основной путь — словарь один на язык,
поддерживать его руками под каждый следующий пользователь не хочет, а модель (в
конфиге — gemma-4 E4B `ge4b-heretic` на ПК по LAN через ollama) на живом
сравнении точнее и безопаснее (docs/04-model-l1.md, `tools/bench_l1_live.py`).

Тем же вечером вскрылась цена: ПК не всегда включён, а Pi не тянет модель такого
уровня — без сети L-1 просто молчит (нули). Добавлен `appraisal.model_fallback`:
при `"lexical"` отказ сенсора (сеть упала, таймаут, невалидная схема) откатывает
на словарь вместо нулей — не потому что словарь снова основной путь, а потому что
"честный сигнал по-русски, пока ПК не поднялся" лучше "тишины, пока ПК не
поднялся", и это ничего не стоит: код и тесты никогда не удалялись, вызов
локальный и бесплатный. `appraisal_invalid` в журнале всё равно пишется — видно,
что сенсор был недоступен, даже если состояние всё же сдвинулось по словарю.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from . import modelcall
from .events import Appraisal, Event, Impulse
from .lexicon_l1 import lexical_appraise

#: Промпт для режима "model" (по умолчанию L-1 работает без модели, см.
#: lexicon_l1.py). Few-shot: без примеров Qwen3-0.6B/1.7B систематически заваливали
#: valence в минус (всё подряд «негативно») — tools/bench_l1_model.py,
#: docs/04-model-l1.md. Примеры намеренно НЕ пересекаются с выборкой GOLD в
#: бенчмарке, иначе цифры точности врут. Заканчивается на «Сообщение:\n» — код
#: дописывает сюда текст сообщения (SENSOR_PROMPT + text).
SENSOR_PROMPT = """Ты — классификатор эмоционального сигнала. Для одного сообщения заполни шесть полей и верни ТОЛЬКО JSON.

Деловые сообщения без эмоций — нейтральные, все поля 0. Но если человек радуется,
злится, благодарит, ругается, грозит уйти, застрял в работе или сообщает об
опасности — обязательно отметь это, не ставь 0.

valence — тон содержания:
  -2 злость, отчаяние, ругань;  -1 недовольство, жалоба;  0 нейтрально, по делу;
  1 доволен, что-то получилось;  2 радость, благодарность
threat — опасность или риск: 0 нет; 1 назван риск; 2 срочная угроза, авария
novelty — насколько содержание новое: 0 рутина; 1 новая деталь; 2 явно новая идея или тема
social_warmth — как человек обращается к собеседнику (отдельно от тона содержания):
  -2 враждебно, оскорбления, «отстань»;  -1 холодно, сухо, отмахивается;
  0 обычно, по-деловому;  1 дружелюбно;  2 тепло, забота, участие
loss — расставание или утрата: 0 нет; 1 временная пауза, отъезд;
  2 уход, разрыв, «удаляю и ухожу», «больше не могу продолжать»
agency_blocked — true, если человек пишет, что не может закончить начатое
  (застрял, не двигается, который раз не выходит); иначе false

Примеры (сообщение, затем его JSON):

Запусти линтер по всему проекту и покажи ошибки.
{"valence":0,"threat":0,"novelty":0,"social_warmth":0,"loss":0,"agency_blocked":false}

Ты сегодня прямо молодец, спасибо за помощь!
{"valence":2,"threat":0,"novelty":0,"social_warmth":2,"loss":0,"agency_blocked":false}

Прочитал про новый способ сжатия логов, никогда о таком не думал.
{"valence":1,"threat":0,"novelty":2,"social_warmth":0,"loss":0,"agency_blocked":false}

Не могу разобраться, третий час бьюсь и никак не двигается.
{"valence":-1,"threat":0,"novelty":0,"social_warmth":0,"loss":0,"agency_blocked":true}

да сколько можно, ты опять всё испортил, достал уже
{"valence":-2,"threat":0,"novelty":0,"social_warmth":-2,"loss":0,"agency_blocked":false}

Осторожно: на проде течёт память, надо срочно смотреть.
{"valence":-1,"threat":2,"novelty":0,"social_warmth":0,"loss":0,"agency_blocked":false}

Всё, с меня хватит, закрываю проект и больше не вернусь.
{"valence":-2,"threat":0,"novelty":0,"social_warmth":-1,"loss":2,"agency_blocked":false}

Уехал к родителям до понедельника, буду недоступен.
{"valence":0,"threat":0,"novelty":0,"social_warmth":0,"loss":1,"agency_blocked":false}

Какой порт сейчас слушает сервис?
{"valence":0,"threat":0,"novelty":0,"social_warmth":0,"loss":0,"agency_blocked":false}

Верни ТОЛЬКО JSON для последнего сообщения.

Сообщение:
"""

#: §12. Матрица «признак → импульс». Единственное место, где признаки становятся числами.
_MATRIX = (
    ("threat", "FEAR", 0.30),
    ("novelty", "SEEKING", 0.25),
    ("loss", "PANIC", 0.30),
)


def impulses_from_appraisal(a: Appraisal, key: str) -> List[Impulse]:
    out: List[Impulse] = []
    for field_name, drive, k in _MATRIX:
        v = getattr(a, field_name)
        if v > 0:
            out.append(Impulse(drive, k * v / 2.0, f"{key}:{field_name}"))
    if a.agency_blocked and a.valence < 0:
        out.append(Impulse("RAGE", 0.35, f"{key}:blocked"))
    if a.social_warmth > 0:
        w = a.social_warmth / 2.0
        out.append(Impulse("CARE", 0.20 * w, f"{key}:warmth"))
        out.append(Impulse("PANIC", -0.40 * w, f"{key}:warmth_relief"))
    if a.valence > 0 and a.threat == 0:
        out.append(Impulse("PLAY", 0.15 * a.valence / 2.0, f"{key}:positive"))
    return out


def make_sensor(cfg: Dict[str, Any]) -> Callable[[str], Dict[str, Any]]:
    """Собрать sensor-callable для режима "model" (appraisal.mode=model).

    cfg["api"] выбирает рантайм: "llamacpp" (рабочий) или "ollama" (легаси). Оба
    гоняют один и тот же SENSOR_PROMPT и одну и ту же Appraisal.json_schema() как
    grammar-constrained decoding — разошлась только обёртка HTTP.
    """
    api = cfg.get("api", "llamacpp")
    if api == "llamacpp":
        return llamacpp_sensor(cfg)
    if api == "ollama":
        return ollama_sensor(cfg)
    if api == "claude_cli":
        return claude_cli_sensor(cfg)
    raise ValueError(f"appraisal.api: неизвестное значение {api!r} (llamacpp|ollama|claude_cli)")


def claude_cli_sensor(cfg: Dict[str, Any]) -> Callable[[str], Dict[str, Any]]:
    """L-1 через Claude (`claude -p`, подписка). Тот же SENSOR_PROMPT и та же схема,
    что у локальных рантаймов; Appraisal.parse() остаётся последним рубежом."""
    claude_bin = cfg.get("claude_bin", "/opt/claude/claude")
    model = cfg["model"]
    timeout_s = float(cfg.get("timeout_s", 55.0))
    effort = cfg.get("effort", "low")

    def sensor(text: str) -> Dict[str, Any]:
        return modelcall.claude_cli_complete(
            claude_bin, model, SENSOR_PROMPT + text, Appraisal.json_schema(),
            timeout_s=timeout_s, effort=effort,
        )

    return sensor


def llamacpp_sensor(cfg: Dict[str, Any]) -> Callable[[str], Dict[str, Any]]:
    """Собрать sensor-callable поверх локального llama.cpp `llama-server`.
    Только stdlib (urllib) — проект не тянет зависимостей ради одного HTTP-вызова.

    cfg — секция "appraisal": base_url (адрес llama-server, напр.
    http://127.0.0.1:8080), timeout_s, num_predict, temperature. Поле model
    информационное (какой .gguf поднят) — сам сервер уже привязан к одной модели,
    в запрос оно не идёт. Обоснование выбора модели и промпта, deploy/llama-l1.service
    и tools/bench_l1_model.py — в docs/04-model-l1.md.

    json_schema=Appraisal.json_schema() заставляет llama.cpp держать GBNF-грамматику
    и отдавать значения строго из допустимых диапазонов — первый рубеж защиты, до
    Appraisal.parse() в Appraiser.appraise_text(). Двойная защита, а не замена
    одного другим: сенсор может быть подменён на модель без поддержки схемы.

    cache_prompt=true: SENSOR_PROMPT (с few-shot) — постоянный префикс, llama-server
    кэширует его KV между вызовами, платим только за сам текст сообщения.

    Бросает исключение при сбое сети/таймауте/не-200 — Appraiser сам ловит любое
    исключение и падает в нули/словарь (docs/02-terms.md: «отказ сенсора не
    должен двигать состояние»), поэтому здесь ничего не глушится молча.

    HTTP-логика — в modelcall.llamacpp_complete(), общая с curator.py.
    """
    base_url = cfg["base_url"]
    timeout_s = float(cfg.get("timeout_s", 8.0))
    num_predict = int(cfg.get("num_predict", 96))
    temperature = float(cfg.get("temperature", 0.0))

    def sensor(text: str) -> Dict[str, Any]:
        return modelcall.llamacpp_complete(
            base_url, SENSOR_PROMPT + text, Appraisal.json_schema(),
            timeout_s=timeout_s, num_predict=num_predict, temperature=temperature,
        )

    return sensor


def ollama_sensor(cfg: Dict[str, Any]) -> Callable[[str], Dict[str, Any]]:
    """Адаптер под ollama `/api/generate` (`format` = JSON-схема, 0.3.0+) —
    рабочий рантайм для gemma-4 E4B на ПК по LAN, см. docs/04-model-l1.md.
    HTTP-логика — в modelcall.ollama_generate(), общая с curator.py.
    """
    base_url = cfg["base_url"]
    model = cfg["model"]
    timeout_s = float(cfg.get("timeout_s", 8.0))
    num_predict = int(cfg.get("num_predict", 96))
    temperature = float(cfg.get("temperature", 0.0))

    def sensor(text: str) -> Dict[str, Any]:
        return modelcall.ollama_generate(
            base_url, model, SENSOR_PROMPT + text, Appraisal.json_schema(),
            timeout_s=timeout_s, num_predict=num_predict, temperature=temperature,
        )

    return sensor


class Appraiser:
    """Правила (события мира → импульсы) плюс оценка текста сообщения.

    Текст оценивается одним из трёх способов (`mode`):
      * "model" — малая модель через `sensor` (llama.cpp / ollama), **по умолчанию**.
        Отказ модели → см. `model_fallback` ниже, и в любом случае пишется в
        журнал как appraisal_invalid — отказ сенсора виден, даже если состояние
        всё же сдвинулось запасным путём.
      * "lexical" — детерминированный словарь (`motus/lexicon_l1.py`), без сети
        и без задержки. Один язык (русский), но бесплатный и не зависит от
        того, что где-то включено.
      * "off" — всегда нули, текст не оценивается вовсе.

    `model_fallback` (используется только при `mode == "model"`):
      * "null" (умолчание) — отказ сенсора не двигает состояние (docs/02-terms.md).
      * "lexical" — отказ сенсора (сеть, таймаут, невалидная схема) откатывает на
        словарь вместо нулей. Для ситуации «модель на удалённом ПК, который не
        всегда включён, а платить за облако не хочется» — честный сигнал по
        словарю лучше тишины, и это не возврат словаря в основной путь: пока
        модель отвечает, используется она.

    Совместимость: `Appraiser(sensor=fn)` без явного mode → "model" (так строят
    тесты и старый код).
    """

    def __init__(self, sensor: Optional[Callable[[str], Any]] = None,
                 mode: Optional[str] = None, lexical_strict: bool = False,
                 model_fallback: str = "null") -> None:
        self.sensor = sensor
        self.mode = mode or "model"
        self.lexical_strict = lexical_strict
        self.model_fallback = model_fallback
        self.invalid_count = 0
        self.last_failed = False

    def _fallback(self, text: str) -> Appraisal:
        """Что вернуть при отказе сенсора в mode='model'. Вызывающий уже
        отметил invalid_count/last_failed — здесь только выбор значения."""
        if self.model_fallback == "lexical":
            return lexical_appraise(text, strict=self.lexical_strict)
        return Appraisal()

    def appraise_text(self, text: str) -> Appraisal:
        self.last_failed = False
        if self.mode == "off":
            return Appraisal()
        if self.mode == "lexical":
            return lexical_appraise(text, strict=self.lexical_strict)
        # mode == "model"
        if not self.sensor:
            return Appraisal()
        try:
            raw = self.sensor(text)
        except Exception:
            self.invalid_count += 1
            self.last_failed = True
            return self._fallback(text)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                self.invalid_count += 1
                self.last_failed = True
                return self._fallback(text)
        # is_null() одинаков и для «модель ответила валидно и честно нейтрально»,
        # и для «модель ответила мусором, parse() уронил в нули» — is_well_formed()
        # смотрит на raw ДО того, как parse() эту разницу стёр. Настоящую
        # нейтральную оценку подменять запасным путём нельзя.
        a = Appraisal.parse(raw)
        if a.is_null() and raw and not Appraisal.is_well_formed(raw):
            self.invalid_count += 1
            self.last_failed = True
            return self._fallback(text)
        return a

    # ------------------------------------------------------------- правила

    def impulses(self, ev: Event) -> List[Impulse]:
        """Событие → импульсы. Детерминированная часть."""
        p = ev.payload
        k = ev.kind

        if k == "user_message":
            # Контакт восстановлен — прямое и безусловное гашение сепарации.
            out = [Impulse("PANIC", -0.60, "contact")]
            if "appraisal" in p:
                out += impulses_from_appraisal(Appraisal.parse(p["appraisal"]), "msg")
            return out

        if k == "tool_error":
            out = [Impulse("FEAR", 0.20, f"tool_error:{p.get('tool', 'unknown')}")]
            if p.get("blocking"):
                out.append(Impulse("RAGE", 0.25, f"blocked:{p.get('tool', 'unknown')}"))
            return out

        if k == "net_down":
            # Единственный случай, где железо бьёт прямо в аффект: для этой системы
            # потеря сети и есть потеря мира.
            return [Impulse("PANIC", 0.25, "net"), Impulse("FEAR", 0.20, "net")]

        if k == "net_up":
            return [Impulse("PANIC", -0.20, "net_up")]

        if k == "sensor":
            return self._sensor_impulses(p)

        if k == "task_result":
            # Насыщение начисляет ядро через consummate(), не импульсом:
            # это разные механизмы, и путать их нельзя.
            return []

        return []

    #: Прирост FEAR на каждый процентный пункт роста использования 5-часового
    #: лимита Claude. 10 п.п. роста -> 0.12 FEAR — тот же порядок, что и разовый
    #: скачок FEAR от integrity_drop (0.15). Не конфиг: как и integrity_drop,
    #: это разовая соматическая константа, а не поведенческий параметр.
    _FEAR_PER_LIMIT_PCT = 0.012

    @staticmethod
    def _sensor_impulses(p: Dict[str, Any]) -> List[Impulse]:
        out: List[Impulse] = []
        if p.get("integrity_drop"):
            out.append(Impulse("FEAR", 0.15, "integrity"))
            out.append(Impulse("CARE", 0.15, "integrity"))
        delta = p.get("claude_limit_delta")
        if delta and delta > 0:
            # Рост, не сам процент: держит квоту вплотную к лимиту не страшнее,
            # чем к нему подойти — страшно РЕЗКО его приближение (см. gates.py
            # для порогов реакции на сам уровень, это отдельная, немгновенная ось).
            amt = float(delta) * Appraiser._FEAR_PER_LIMIT_PCT
            out.append(Impulse("FEAR", amt, "claude_limit"))
        return out

    @staticmethod
    def somatic_update(somatic: Dict[str, float], p: Dict[str, Any]) -> Dict[str, float]:
        """Датчики железа → сома. Не аффект: только модуляция гейнов и порогов."""
        s = dict(somatic)
        if "temp_c" in p:
            t = float(p["temp_c"])
            s["thermal"] = max(0.0, min(1.0, (t - 55.0) / 30.0))
        if p.get("throttled"):
            s["thermal"] = max(s["thermal"], 0.8)

        # integrity РЕКОНСТРУИРУЕТСЯ из фактов текущего опроса, а не тянется вниз
        # монотонно. Старый код (`s["integrity"] = min(s["integrity"], ...)`) был
        # храповиком: один опрос с services_ok=false ронял integrity до 0.5
        # НАВСЕГДА — `min(0.5, 1.0)` так и остаётся 0.5, даже когда сервис
        # вернулся. В логах это выглядело как «Часть окружения неисправна» на
        # карточке ещё сутки после разовой недоступности openclaw.
        components = []
        if "disk_free_frac" in p:
            components.append(max(0.0, min(1.0, float(p["disk_free_frac"]) / 0.10)))
        if "services_ok" in p:
            components.append(1.0 if p["services_ok"] else 0.5)
        if "integrity" in p:
            # Явное значение датчика — приоритетнее любых производных.
            s["integrity"] = max(0.0, min(1.0, float(p["integrity"])))
        elif components:
            s["integrity"] = min(components)

        if "energy" in p:
            s["energy"] = max(0.0, min(1.0, float(p["energy"])))

        if "claude_limit_pct" in p:
            s["limit"] = max(0.0, min(1.0, float(p["claude_limit_pct"]) / 100.0))
        return s
