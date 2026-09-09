"""Загрузка и валидация конфига. Никакой логики — только данные и проверки."""

from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, Iterable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")

DRIVES = ("SEEKING", "CARE", "PLAY", "FEAR", "RAGE", "PANIC")
MODULATORS = ("da", "ne", "ht5")
SOMATIC = ("energy", "integrity", "thermal")


class ConfigError(ValueError):
    pass


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load(config_dir: str = CONFIG_DIR) -> Dict[str, Any]:
    cfg = load_json(os.path.join(config_dir, "default.json"))
    cfg["_lexicon"] = load_json(os.path.join(config_dir, "lexicon.ru.json"))
    cfg["_repertoire"] = load_json(os.path.join(config_dir, "repertoire.json"))
    validate(cfg)
    return cfg


#: Пути в конфиге (точечная нотация), значение которых делит exp(−·/τ) или иначе
#: стоит в знаменателе. Ноль здесь → ZeroDivisionError на первом же тике; отрицание
#: → тихо неверная динамика. Валидируем строго > 0. (drives[*].tau_relax_s
#: проверяется отдельно в цикле по драйвам.)
_POSITIVE_TIME_CONSTANTS = (
    "context.tau_s",
    "separation.tau_s",
    "boredom.tau_s",
    "habituation.tau_s", "habituation.kappa",
    "modulator.tau_s",
    "budget.penalty_tau_s",
    "sleep.period_s",
    "heartbeat.tick_min_s",
)


def _dig(cfg: Dict[str, Any], path: str) -> Any:
    node: Any = cfg
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"в конфиге нет пути {path}")
        node = node[part]
    return node


def _iter_numbers(node: Any, path: str = "") -> Iterable[tuple]:
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _iter_numbers(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _iter_numbers(v, f"{path}[{i}]")
    elif isinstance(node, bool):
        pass
    elif isinstance(node, (int, float)):
        yield path, node


def validate(cfg: Dict[str, Any]) -> None:
    # Ни NaN, ни ±Inf нигде в конфиге: json.load принимает эти литералы молча,
    # а дальше они «вирусом» расходятся по всему вектору состояния (_clip их не
    # ловит). Ключи, начинающиеся с "_" — подгруженные lexicon/repertoire, их
    # тоже проверяем.
    for path, val in _iter_numbers(cfg):
        if isinstance(val, float) and not math.isfinite(val):
            raise ConfigError(f"нечисловое значение в конфиге: {path} = {val}")

    for path in _POSITIVE_TIME_CONSTANTS:
        if _dig(cfg, path) <= 0:
            raise ConfigError(f"{path} должен быть > 0 (стоит в знаменателе)")

    missing = set(DRIVES) - set(cfg["drives"])
    if missing:
        raise ConfigError(f"нет параметров для драйвов: {sorted(missing)}")
    extra = set(cfg["drives"]) - set(DRIVES)
    if extra:
        raise ConfigError(f"неизвестные драйвы: {sorted(extra)}")

    for name, d in cfg["drives"].items():
        if d["kind"] not in ("tonic", "phasic"):
            raise ConfigError(f"{name}: kind должен быть tonic|phasic")
        if not 0.0 <= d["setpoint"] <= 1.0:
            raise ConfigError(f"{name}: setpoint вне [0,1]")
        if d["tau_relax_s"] <= 0:
            raise ConfigError(f"{name}: tau_relax_s должен быть > 0")
        if not 0.0 < d["theta_lo"] < d["theta_hi"] < 1.0:
            raise ConfigError(f"{name}: нужно 0 < theta_lo < theta_hi < 1 (гистерезис)")
        if d["kind"] == "phasic" and name != "PANIC" and d["setpoint"] != 0.0:
            raise ConfigError(f"{name}: у фазического драйва сетпоинт должен быть 0")

    hb = cfg["heartbeat"]
    if hb["tick_min_s"] >= hb["tick_max_s"]:
        raise ConfigError("tick_min_s должен быть меньше tick_max_s")
    if hb["theta_act"] < hb["theta_task"]:
        raise ConfigError("theta_act должен быть не ниже theta_task")

    ctx = cfg["context"]
    if not 1.0 > ctx["band_fresh"] > ctx["band_aging"] > ctx["band_stale"] > 0.0:
        raise ConfigError("полосы свежести должны убывать: fresh > aging > stale > 0")

    lex = cfg.get("_lexicon", {})
    for name in DRIVES:
        if name not in lex.get("regimes", {}):
            raise ConfigError(f"в лексиконе нет режима {name}")

    ap = cfg.get("appraisal", {})
    mode = ap.get("mode", "lexical")
    if mode not in ("lexical", "model", "off"):
        raise ConfigError(f"appraisal.mode: ожидается lexical|model|off, получено {mode!r}")
    if "lexical_strict" in ap and not isinstance(ap["lexical_strict"], bool):
        raise ConfigError("appraisal.lexical_strict: ожидается true|false")
    if mode == "model":
        api = ap.get("api", "llamacpp")
        if api not in ("llamacpp", "ollama"):
            raise ConfigError(f"appraisal.api: ожидается llamacpp|ollama, получено {api!r}")
        required = ("base_url",) if api == "llamacpp" else ("base_url", "model")
        for key in required:
            if not ap.get(key):
                raise ConfigError(f"appraisal.mode=model, но не задано appraisal.{key}")

    for tpl in cfg.get("_repertoire", {}).get("templates", []):
        if tpl["drive"] not in DRIVES:
            raise ConfigError(f"шаблон {tpl['id']}: неизвестный драйв {tpl['drive']}")
        if tpl["drive"] == "RAGE":
            # Осознанное правило, а не недосмотр: RAGE не порождает фоновой
            # деятельности. Правильное поведение при высоком RAGE — сокращение
            # полномочий и остывание.
            raise ConfigError(f"шаблон {tpl['id']}: у RAGE не может быть репертуара")
