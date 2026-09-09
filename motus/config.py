"""Загрузка и валидация конфига. Никакой логики — только данные и проверки."""

from __future__ import annotations

import json
import os
from typing import Any, Dict

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


def validate(cfg: Dict[str, Any]) -> None:
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

    for tpl in cfg.get("_repertoire", {}).get("templates", []):
        if tpl["drive"] not in DRIVES:
            raise ConfigError(f"шаблон {tpl['id']}: неизвестный драйв {tpl['drive']}")
        if tpl["drive"] == "RAGE":
            # Осознанное правило, а не недосмотр: RAGE не порождает фоновой
            # деятельности. Правильное поведение при высоком RAGE — сокращение
            # полномочий и остывание.
            raise ConfigError(f"шаблон {tpl['id']}: у RAGE не может быть репертуара")
