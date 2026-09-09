"""L-1 — оценка: события мира → импульсы.

Две ветви:
  * правила (детерминированные) — датчики, таймеры, коды ошибок;
  * малая локальная модель как СЕНСОР — выход строго по схеме Appraisal.

Модель здесь ничего не рассказывает про эмоции: она заполняет шесть полей с
известными диапазонами. Дрейфовать негде, невалидная схема падает в нули и
логируется как appraisal_invalid — отказ сенсора не должен двигать состояние.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from .events import Appraisal, Event, Impulse

#: Промпт малой модели. Намеренно не содержит ни слова про чувства и настроение —
#: только измеримые признаки сообщения.
SENSOR_PROMPT = """Ты — датчик. Оцени сообщение по шести признакам и верни ТОЛЬКО JSON.
valence: -2..2 (насколько сообщение негативно/позитивно по содержанию)
threat: 0..2 (есть ли угроза, риск, срочная опасность)
novelty: 0..2 (насколько содержание ново по сравнению с обычным)
social_warmth: -2..2 (холодность/теплота обращения)
loss: 0..2 (говорится ли о потере, разрыве, уходе)
agency_blocked: true/false (мешают ли выполнить начатое)
Никакого текста кроме JSON.

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


def ollama_sensor(cfg: Dict[str, Any]) -> Callable[[str], Dict[str, Any]]:
    """Собрать sensor-callable поверх локальной ollama. Только stdlib (urllib) —
    проект не тянет зависимостей ради одного HTTP-вызова.

    cfg — секция "appraisal" из конфига: base_url, model, timeout_s, num_predict,
    temperature. Модель по умолчанию — qwen3:0.6b-q4_K_M, обоснование выбора и
    команда `ollama pull` — в docs/04-model-l1.md.

    format=Appraisal.json_schema() заставляет ollama (0.3.0+, поддержка grammar-
    constrained decoding) отдавать значения строго из допустимых диапазонов —
    это первый рубеж защиты, до Appraisal.parse() в Appraiser.appraise_text().
    Двойная защита, а не замена одного другим: сенсор может быть подменён на
    что угодно, включая модель без поддержки схемы.

    Бросает исключение при сбое сети/таймауте/не-200 — Appraiser сам ловит любое
    исключение и падает в нули (docs/02-terms.md: «отказ сенсора не должен
    двигать состояние»), поэтому здесь ничего не глушится молча.
    """
    base_url = cfg["base_url"].rstrip("/")
    model = cfg["model"]
    timeout_s = float(cfg.get("timeout_s", 3.0))
    num_predict = int(cfg.get("num_predict", 80))
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
    """Правила плюс необязательный адаптер малой модели.

    sensor: callable(text) -> dict | None. Если None или бросил исключение —
    работают только правила. Система обязана быть полностью работоспособной
    без всякой LLM.
    """

    def __init__(self, sensor: Optional[Callable[[str], Any]] = None) -> None:
        self.sensor = sensor
        self.invalid_count = 0

    def appraise_text(self, text: str) -> Appraisal:
        if not self.sensor:
            return Appraisal()
        try:
            raw = self.sensor(text)
        except Exception:
            self.invalid_count += 1
            return Appraisal()
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                self.invalid_count += 1
                return Appraisal()
        a = Appraisal.parse(raw)
        if a.is_null() and raw:
            self.invalid_count += 1
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
        if "disk_free_frac" in p:
            free = float(p["disk_free_frac"])
            s["integrity"] = min(s["integrity"], max(0.0, min(1.0, free / 0.10)))
        if "services_ok" in p:
            s["integrity"] = min(s["integrity"], 1.0 if p["services_ok"] else 0.5)
        if "integrity" in p:
            s["integrity"] = max(0.0, min(1.0, float(p["integrity"])))
        if "energy" in p:
            s["energy"] = max(0.0, min(1.0, float(p["energy"])))
        return s
