"""L-1 — оценка: события мира → импульсы.

Ветви:
  * правила (детерминированные) — датчики, таймеры, коды ошибок (Appraiser.impulses);
  * оценка текста сообщения — по `Appraiser.mode`:
      "model" (умолчание) — модель как СЕНСОР, выход строго по схеме Appraisal;
      "off" — нули.

Модель ничего не рассказывает про эмоции: заполняет шесть полей с известными
диапазонами. Дрейфовать негде, невалидная схема падает в нули и логируется как
appraisal_invalid — отказ сенсора не должен двигать состояние.

БЫЛ третий режим — "lexical", детерминированный словарь `lexicon_l1.py`. Отключён
2026-09-10 по требованию пользователя: словарь один на язык, поддерживать его
руками под каждый следующий язык он не хочет. Модель этого не требует — оценивает
любой язык тем же промптом. Обоснование выбора конкретной модели (gemma-4 E4B
`ge4b-heretic` вместо прежнего Qwen3-1.7B) — docs/04-model-l1.md, живые цифры —
`tools/bench_l1_live.py`. `lexicon_l1.py` не удалён (референс, офлайн-бенчмарк),
но из этого модуля больше не импортируется:
    # from .lexicon_l1 import lexical_appraise
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from .events import Appraisal, Event, Impulse
# from .lexicon_l1 import lexical_appraise  # словарная математика отключена (см. выше)

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
    raise ValueError(f"appraisal.api: неизвестное значение {api!r} (llamacpp|ollama)")


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
    исключение и падает в нули (docs/02-terms.md: «отказ сенсора не должен
    двигать состояние»), поэтому здесь ничего не глушится молча.
    """
    base_url = cfg["base_url"].rstrip("/")
    timeout_s = float(cfg.get("timeout_s", 8.0))
    num_predict = int(cfg.get("num_predict", 96))
    temperature = float(cfg.get("temperature", 0.0))

    def sensor(text: str) -> Dict[str, Any]:
        body = json.dumps({
            "prompt": SENSOR_PROMPT + text,
            "json_schema": Appraisal.json_schema(),
            "n_predict": num_predict,
            "temperature": temperature,
            "cache_prompt": True,
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

    return sensor


def ollama_sensor(cfg: Dict[str, Any]) -> Callable[[str], Dict[str, Any]]:
    """Легаси-адаптер под ollama `/api/generate` (`format` = JSON-схема, 0.3.0+).
    Оставлен на случай, если L-1 будут гонять через ollama, а не llama.cpp;
    рабочий рантайм проекта — llamacpp_sensor, см. docs/04-model-l1.md.
    """
    base_url = cfg["base_url"].rstrip("/")
    model = cfg["model"]
    timeout_s = float(cfg.get("timeout_s", 8.0))
    num_predict = int(cfg.get("num_predict", 96))
    temperature = float(cfg.get("temperature", 0.0))

    def sensor(text: str) -> Dict[str, Any]:
        body = json.dumps({
            "model": model,
            "prompt": SENSOR_PROMPT + text,
            "format": Appraisal.json_schema(),
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

    return sensor


class Appraiser:
    """Правила (события мира → импульсы) плюс оценка текста сообщения.

    Текст оценивается одним из двух способов (`mode`):
      * "model" — малая модель через `sensor` (llama.cpp / ollama), **по умолчанию**.
        Отказ модели → нули (docs/02-terms.md: «отказ сенсора не двигает
        состояние»), и это пишется в журнал как appraisal_invalid.
      * "off" — всегда нули, текст не оценивается вовсе.

    ОТКЛЮЧЕНО (2026-09-10, по требованию пользователя): режим "lexical" —
    детерминированный словарь `motus/lexicon_l1.py`. На русской выборке он был
    точнее и безопаснее Qwen3-1.7B (docs/04-model-l1.md), но словарь один на
    язык и держать его руками под каждый следующий язык пользователь не хочет.
    Живое сравнение на той же выборке (`tools/bench_l1_live.py`, GOLD+HARD, 20
    сообщений) с gemma-4 E4B (`ge4b-heretic` на ПК по LAN через ollama) дало
    ±1 85% / вред 5% / false_alarm 0 — лучше и словаря (80%/15%), и прежнего
    кандидата Qwen3-1.7B на Pi (55%/25%). Ветка словаря НЕ вызывается:
        # if self.mode == "lexical":
        #     return lexical_appraise(text, strict=self.lexical_strict)
    Сам `lexicon_l1.py` и его тесты не удалены — референс и офлайн-инструмент
    (`tools/bench_l1_model.py --lexical` для будущих сравнений), но из
    боевого пути исключён. `lexical_strict` остаётся в конфиге как поле без
    действия (dead config), пока словарный путь не понадобится снова.

    Совместимость: `Appraiser(sensor=fn)` без явного mode → "model" (так строят
    тесты и старый код).
    """

    def __init__(self, sensor: Optional[Callable[[str], Any]] = None,
                 mode: Optional[str] = None, lexical_strict: bool = False) -> None:
        self.sensor = sensor
        self.mode = mode or "model"
        self.lexical_strict = lexical_strict
        self.invalid_count = 0
        self.last_failed = False

    def appraise_text(self, text: str) -> Appraisal:
        self.last_failed = False
        if self.mode == "off":
            return Appraisal()
        # словарная математика отключена — см. докстринг класса.
        # if self.mode == "lexical":
        #     return lexical_appraise(text, strict=self.lexical_strict)
        # mode == "model"
        if not self.sensor:
            return Appraisal()
        try:
            raw = self.sensor(text)
        except Exception:
            self.invalid_count += 1
            self.last_failed = True
            return Appraisal()
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                self.invalid_count += 1
                self.last_failed = True
                return Appraisal()
        a = Appraisal.parse(raw)
        if a.is_null() and raw:
            self.invalid_count += 1
            self.last_failed = True
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

    @staticmethod
    def _sensor_impulses(p: Dict[str, Any]) -> List[Impulse]:
        out: List[Impulse] = []
        if p.get("integrity_drop"):
            out.append(Impulse("FEAR", 0.15, "integrity"))
            out.append(Impulse("CARE", 0.15, "integrity"))
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
        return s
