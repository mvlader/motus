#!/usr/bin/env python3
"""motusctl — меню-конфигуратор MOTUS (описание — README, раздел «Конфигуратор»).

    python3 tools/motusctl.py

Запускается на хосте, где лежит git-репозиторий. Правит config/ ИМЕННО в
репозитории (единственная точка правды) и уже оттуда раскладывает файл в деплой —
локально или в контейнер через `incus exec` (настройка «запуск через»). Так деплой
и git не расходятся молча: перед записью меню сверяет деплой с репозиторием.

Что меню не делает никогда:
  * не пишет state.json — им владеет демон, а живые драйвы не «настройка»;
  * не трогает gates.REGIME_POLICY — это код, не конфиг;
  * не делает вид, что правка действует до рестарта motusd.

Только stdlib.
"""

from __future__ import annotations

import copy
import datetime as _dt
import difflib
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from motus import config, replay            # noqa: E402
from motus.config import ConfigError         # noqa: E402
from motus.events import Event               # noqa: E402

REPO_CONFIG = os.path.join(REPO, "config")

# ============================================================== настройки меню

SETTINGS_PATH = os.environ.get(
    "MOTUSCTL_SETTINGS",
    os.path.join(os.path.expanduser("~"), ".config", "motusctl", "settings.json"),
)

#: Настройки самого меню, не MOTUS. Живут вне репозитория: это про машину
#: оператора, а не про систему.
DEFAULT_SETTINGS: Dict[str, Any] = {
    # Пусто — команды выполняются локально; имя — `incus exec <имя> --`.
    "motus_via": "",
    "openclaw_via": "",
    "motus_code_dir": "/opt/motus",
    "motus_config_dir": "/opt/motus/config",
    "motus_var_dir": "/var/lib/motus",
    "motus_admin_socket": "/run/motusd/adm.sock",
    "motus_unit": "motusd",
    "motus_user": "motus",
    "openclaw_user": "openclaw",
    "openclaw_bin": "/home/openclaw/.npm-global/bin/openclaw",
    "openclaw_unit": "openclaw-gateway",
    "tier1_executor": "/opt/motus-tier1/tier1_executor.py",
}

SETTINGS_HELP = {
    "motus_via": "запуск через: контейнер incus с motusd (пусто — локально)",
    "openclaw_via": "запуск через: контейнер incus с openclaw (пусто — локально)",
    "motus_code_dir": "код motusd в деплое",
    "motus_config_dir": "config/ в деплое",
    "motus_var_dir": "состояние и журнал боевого демона",
    "motus_admin_socket": "админский unix-сокет",
    "motus_unit": "systemd-юнит демона",
    "motus_user": "пользователь демона",
    "openclaw_user": "пользователь openclaw (systemd --user)",
    "openclaw_bin": "бинарь openclaw",
    "openclaw_unit": "юнит гейтвея (systemd --user)",
    "tier1_executor": "путь исполнителя Tier 1 в контейнере openclaw",
}


def load_settings(path: str = SETTINGS_PATH) -> Dict[str, Any]:
    out = dict(DEFAULT_SETTINGS)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return out
    for k, v in raw.items():
        if k in DEFAULT_SETTINGS and isinstance(v, str):
            out[k] = v
    return out


def save_settings(settings: Dict[str, Any], path: str = SETTINGS_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write_text(path, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")


# ============================================================== исполнение команд


class Target:
    """Где выполнять команду: локально или `incus exec <via> --`."""

    def __init__(self, via: str = "", runner: Callable[..., subprocess.CompletedProcess] = None):
        self.via = via.strip()
        self._runner = runner or subprocess.run

    def argv(self, cmd: Sequence[str], user: Optional[str] = None) -> List[str]:
        cmd = list(cmd)
        if self.via:
            if user:
                cmd = ["runuser", "-u", user, "--"] + cmd
            return ["incus", "exec", self.via, "--"] + cmd
        if user and user != _current_user():
            return ["sudo", "-n", "-u", user, "--"] + cmd
        return cmd

    def shell_as(self, user: str, script: str) -> List[str]:
        """Login-оболочка пользователя: нужна systemd --user и HOME openclaw."""
        if self.via:
            return ["incus", "exec", self.via, "--", "su", "-", user, "-s", "/bin/sh", "-c", script]
        if user == _current_user():
            return ["sh", "-c", script]
        return ["sudo", "-n", "-i", "-u", user, "sh", "-c", script]

    def run(self, cmd: Sequence[str], user: Optional[str] = None, input: Optional[str] = None,
            timeout: float = 120.0, check: bool = False) -> subprocess.CompletedProcess:
        return self._exec(self.argv(cmd, user), input, timeout, check)

    def run_shell_as(self, user: str, script: str, timeout: float = 120.0,
                     check: bool = False) -> subprocess.CompletedProcess:
        return self._exec(self.shell_as(user, script), None, timeout, check)

    def _exec(self, argv, input, timeout, check):
        # Без явного stdin `incus exec` наследует терминал меню и съедает ввод оператора.
        kw = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
        res = self._runner(argv, capture_output=True, text=True, timeout=timeout, **kw)
        if check and res.returncode != 0:
            raise RuntimeError(f"команда упала ({res.returncode}): {shlex.join(argv)}\n"
                               f"{(res.stderr or res.stdout).strip()}")
        return res

    def label(self) -> str:
        return f"incus:{self.via}" if self.via else "локально"

    def read_file(self, path: str) -> Optional[str]:
        res = self.run(["cat", path])
        return res.stdout if res.returncode == 0 else None

    def write_file(self, path: str, data: str) -> None:
        """Атомарно: временный файл рядом → mv. Владелец и права — как у старого файла."""
        if os.path.basename(path) in STATE_FILENAMES:
            raise PermissionError(f"motusctl не пишет {path}: состоянием владеет демон")
        script = ('set -e; tmp=$(mktemp "$1.tmp.XXXXXX"); cat > "$tmp"; '
                  'if [ -e "$1" ]; then chown --reference="$1" "$tmp" 2>/dev/null || true; '
                  'chmod --reference="$1" "$tmp"; else chmod 644 "$tmp"; fi; mv -f "$tmp" "$1"')
        self.run(["sh", "-c", script, "sh", path], input=data, check=True)

    def backup_file(self, path: str, stamp: str) -> Optional[str]:
        dst = f"{path}.bak-{stamp}"
        res = self.run(["sh", "-c", 'if [ -e "$1" ]; then cp -p "$1" "$2"; echo ok; fi',
                        "sh", path, dst], check=True)
        return dst if res.stdout.strip() == "ok" else None


def _current_user() -> str:
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return os.environ.get("USER", "")


# ============================================================== конфиг: чистые функции

STATE_FILENAMES = frozenset(("state.json",))


def stamp(now: Optional[float] = None) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))


def dump_json(data: Any) -> str:
    # Тот же формат, в котором файлы уже лежат в git (indent=2, без \n в конце) —
    # иначе каждая правка через меню даёт шумный diff на весь файл.
    return json.dumps(data, indent=2, ensure_ascii=False)


def atomic_write_text(path: str, text: str) -> None:
    if os.path.basename(path) in STATE_FILENAMES:
        raise PermissionError(f"motusctl не пишет {path}: состоянием владеет демон")
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp.", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.exists(path):
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def get_path(cfg: Dict[str, Any], path: str) -> Any:
    node: Any = cfg
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(path)
        node = node[part]
    return node


def has_path(cfg: Dict[str, Any], path: str) -> bool:
    try:
        get_path(cfg, path)
        return True
    except KeyError:
        return False


def set_path(cfg: Dict[str, Any], path: str, value: Any, create: bool = False) -> None:
    parts = path.split(".")
    node = cfg
    for part in parts[:-1]:
        if part not in node:
            if not create:
                raise KeyError(path)
            node[part] = {}
        node = node[part]
        if not isinstance(node, dict):
            raise KeyError(path)
    if parts[-1] not in node and not create:
        raise KeyError(path)
    node[parts[-1]] = value


_TRUE = {"true", "да", "yes", "y", "д", "1", "on", "вкл"}
_FALSE = {"false", "нет", "no", "n", "н", "0", "off", "выкл"}


def parse_value(text: str, old: Any) -> Any:
    """Разобрать ввод оператора по типу старого значения. ValueError — если не подходит."""
    t = text.strip()
    if isinstance(old, bool):
        if t.lower() in _TRUE:
            return True
        if t.lower() in _FALSE:
            return False
        raise ValueError("ожидается да/нет")
    if isinstance(old, int):
        v = float(t.replace(",", "."))
        if not math.isfinite(v):
            raise ValueError("нужно конечное число")
        return int(v) if v == int(v) else v
    if isinstance(old, float):
        v = float(t.replace(",", "."))
        if not math.isfinite(v):
            raise ValueError("нужно конечное число")
        return v
    if isinstance(old, str):
        return t
    return json.loads(t)


def leaves(node: Any, prefix: str = "") -> List[Tuple[str, Any]]:
    """Скалярные листья без служебных ключей `_*` (комментарии в JSON)."""
    out: List[Tuple[str, Any]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k.startswith("_"):
                continue
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                out.extend(leaves(v, p))
            else:
                out.append((p, v))
    return out


def diff_configs(old: Dict[str, Any], new: Dict[str, Any]) -> List[Tuple[str, Any, Any]]:
    a, b = dict(leaves(old)), dict(leaves(new))
    out = []
    for k in list(a) + [k for k in b if k not in a]:
        if a.get(k, "<нет>") != b.get(k, "<нет>"):
            out.append((k, a.get(k, "<нет>"), b.get(k, "<нет>")))
    return out


def validate_candidate(cfg: Dict[str, Any], config_dir: str = REPO_CONFIG) -> Dict[str, Any]:
    """Тот же путь, каким демон загрузит файл: config.assemble → validate. Бросает ConfigError."""
    try:
        return config.assemble(copy.deepcopy(cfg), config_dir)
    except ConfigError:
        raise
    except (KeyError, TypeError, ValueError, FileNotFoundError) as exc:
        # validate() местами полагается на наличие ключей — отсутствие это тоже битый конфиг.
        raise ConfigError(f"конфиг не проходит проверку: {type(exc).__name__}: {exc}") from exc


def save_config(cfg: Dict[str, Any], config_dir: str = REPO_CONFIG,
                now: Optional[float] = None) -> Optional[str]:
    """validate → бэкап → атомарная запись default.json. Возвращает путь бэкапа.

    Порядок важен: на невалидном конфиге не создаётся ни бэкап, ни запись.
    """
    validate_candidate(cfg, config_dir)
    path = os.path.join(config_dir, "default.json")
    backup = None
    if os.path.exists(path):
        backup = f"{path}.bak-{stamp(now)}"
        shutil.copy2(path, backup)
    atomic_write_text(path, dump_json(cfg))
    return backup


def load_raw_config(config_dir: str = REPO_CONFIG) -> Dict[str, Any]:
    return config.load_json(os.path.join(config_dir, "default.json"))


# ============================================================== схема меню

#: (заголовок, префикс пути, пояснение). Префикс — ключ верхнего уровня default.json.
AGENT_SECTIONS: List[Tuple[str, str, str]] = [
    ("Драйвы", "drives", "ядро каждого из 6 драйвов; theta_hi/lo — гистерезис режима"),
    ("Темперамент", "temperament", "базовые уровни модуляторов, к ним стягивается сон"),
    ("Модуляторы", "modulator", "скорость возврата к темпераменту и сила влияния"),
    ("Пороги", "thresholds", "сдвиг порогов модуляторами; dwell_s — минимум в режиме"),
    ("Возбуждение", "arousal", "формула общего возбуждения"),
    ("Циркадный ритм", "circadian", "суточный ритм энергии"),
    ("Разлука (PANIC)", "separation", "через сколько молчания растёт PANIC — рычаг «напишет первым»"),
    ("Скука (SEEKING)", "boredom", "рост SEEKING от простоя"),
    ("Контекст", "context", "свежесть нити разговора"),
    ("Привыкание", "habituation", "привыкание к повторам"),
    ("Сердцебиение", "heartbeat", "theta_act — порог инициации, theta_task — фоновой задачи"),
    ("Консумация", "consummation", "насколько акт гасит драйв"),
    ("Репертуар", "repertoire", "очередь фоновых задач"),
    ("Бюджет инициаций", "budget", "токены, рефрактерность, штраф за молчание"),
    ("Сон", "sleep", "ночной цикл консолидации"),
    ("Вербализатор", "verbalizer", "бюджет карточки"),
    ("Самокурирование", "curation", "ночная правка репертуара моделью: до 3 правок, каждую проверяет код"),
]

KEY_HELP: Dict[str, str] = {
    "kind": "tonic|phasic", "setpoint": "сетпоинт ∈ [0,1]", "tau_relax_s": "релаксация к сетпоинту, с",
    "theta_hi": "порог входа в режим", "theta_lo": "порог выхода из режима",
    "priority": "приоритет режима", "gain": "усиление импульсов",
    "separation.grace_s": "молчание до начала роста PANIC, с",
    "separation.max": "пик прироста сетпоинта PANIC",
    "heartbeat.theta_act": "порог проактивной инициации (Tier 2)",
    "heartbeat.theta_task": "порог фоновой задачи (Tier 1)",
    "heartbeat.task_min_interval_s": "минимум между фоновыми задачами, с",
    "budget.initiation_enabled": "разрешена ли инициация контакта вообще",
    "budget.refractory_s": "пауза после инициации, с",
    "budget.quiet_hours.enabled": "тихие часы включены",
    "budget.quiet_hours.start_hour": "начало тихих часов (час, локальное время)",
    "budget.quiet_hours.end_hour": "конец тихих часов",
    "budget.quiet_hours.grace_after_contact_s": "игнор тихих часов после сообщения человека, с",
    "clock.timezone": "IANA-пояс; по умолчанию — пояс консоли motusctl",
    "verbalizer.lexicon": "язык карточки для модели: ru|en",
    "verbalizer.max_chars": "бюджет карточки, символов",
    "appraisal.mode": "model|lexical|off",
    "appraisal.api": "llamacpp|ollama|claude_cli",
    "appraisal.model": "модель L-1 (для claude_cli — например claude-sonnet-5)",
    "appraisal.model_fallback": "что при отказе модели: null|lexical",
    "curation.api": "llamacpp|ollama|claude_cli",
    "curation.model": "модель курирования",
    "curation.enabled": "право агента самому править свой репертуар",
}

#: Статус: (путь, пояснение) — только показ, правка идёт через свои разделы.
STATUS_KEYS: List[Tuple[str, str]] = [
    ("budget.initiation_enabled", "может ли бот сам писать первым (Tier 2)"),
    ("heartbeat.theta_act", "порог активации для инициации контакта: выше — пишет реже"),
    ("heartbeat.theta_task", "порог активации для фоновой задачи (Tier 1)"),
    ("heartbeat.task_min_interval_s", "не чаще одной фоновой задачи за столько секунд"),
    ("separation.grace_s", "сколько секунд молчания до начала роста PANIC"),
    ("separation.max", "насколько сильно может вырасти PANIC от разлуки"),
    ("boredom.grace_s", "сколько секунд без нового входа до начала скуки (SEEKING)"),
    ("budget.refractory_s", "пауза после каждой инициации, с"),
    ("budget.capacity", "сколько инициаций может накопиться про запас"),
    ("budget.quiet_hours.enabled", "тихие часы: в окне бот не пишет первым"),
    ("budget.quiet_hours.start_hour", "начало тихих часов"),
    ("budget.quiet_hours.end_hour", "конец тихих часов"),
    ("clock.timezone", "часовой пояс, в котором считаются часы суток"),
    ("thresholds.dwell_s", "минимальное время в режиме до смены, с"),
    ("verbalizer.lexicon", "язык карточки для модели"),
    ("curation.enabled", "ночное самокурирование репертуара"),
    ("curation.model", "модель самокурирования"),
]

USER_KEYS = [
    "budget.quiet_hours.enabled",
    "budget.quiet_hours.start_hour",
    "budget.quiet_hours.end_hour",
    "budget.quiet_hours.grace_after_contact_s",
    "clock.timezone",
]

#: Ключи, правка которых требует явного подтверждения с объяснением.
DANGEROUS: Dict[str, str] = {
    "curation.enabled": (
        "Ночное самокурирование даёт агенту право САМОМУ править свой репертуар:\n"
        "не больше 3 правок за ночь, только из суженного словаря значений —\n"
        "каждую правку проверяет код (repertoire.apply_edits), а не модель."
    ),
    "budget.initiation_enabled": "Включает/выключает проактивные сообщения бота (Tier 2).",
}


def console_timezone() -> str:
    """IANA-пояс консоли, на которой запущен motusctl: TZ → /etc/localtime → /etc/timezone.

    Умолчание для clock.timezone. Демон в контейнере сам его узнать не может
    (контейнер может жить в UTC), поэтому пояс явно пишется в конфиг.
    """
    from zoneinfo import ZoneInfo
    cands = [os.environ.get("TZ", "").lstrip(":")]
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            cands.append(link.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    try:
        with open("/etc/timezone", encoding="utf-8") as fh:
            cands.append(fh.read().strip())
    except OSError:
        pass
    for name in cands:
        if not name or name.startswith("/"):
            continue
        try:
            ZoneInfo(name)
            return name
        except Exception:
            continue
    return ""


def apply_console_timezone(cfg: Dict[str, Any], tz: Optional[str] = None) -> bool:
    """Пустой clock.timezone → пояс консоли. True, если что-то подставлено."""
    tz = console_timezone() if tz is None else tz
    if not tz or (cfg.get("clock") or {}).get("timezone"):
        return False
    set_path(cfg, "clock.timezone", tz, create=True)
    return True


def help_for(path: str) -> str:
    return KEY_HELP.get(path) or KEY_HELP.get(path.rsplit(".", 1)[-1], "")


# ============================================================== тесты драйвов (реплей-стенд)

#: 2026-01-01 00:00 UTC — точка отсчёта синтетики (как в tools/simulate.py).
SYNTH_T0 = 1767225600.0


def parse_duration(text: str) -> float:
    """'90m', '1.5h', '2ч', '30с', '3h30m', голое число — часы. → секунды."""
    t = text.strip().lower().replace(",", ".")
    if not t:
        raise ValueError("пустая длительность")
    units = {"h": 3600, "ч": 3600, "m": 60, "м": 60, "s": 1, "с": 1, "d": 86400, "д": 86400}
    total, num = 0.0, ""
    for ch in t:
        if ch.isdigit() or ch == ".":
            num += ch
        elif ch in units:
            if not num:
                raise ValueError(f"нет числа перед {ch!r}")
            total += float(num) * units[ch]
            num = ""
        elif ch.isspace():
            continue
        else:
            raise ValueError(f"непонятная единица {ch!r}")
    if num:
        total += float(num) * 3600
    return total


def fmt_dur(seconds: float) -> str:
    h, m = divmod(int(round(seconds / 60.0)), 60)
    return f"{h}ч{m:02d}м"


def _coerce(v: str) -> Any:
    lv = v.lower()
    if lv in ("true", "false"):
        return lv == "true"
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def parse_script(text: str) -> List[Tuple[float, Optional[Event]]]:
    """Сценарий потока событий, по строке на событие:

        0     user_message novelty=1 social_warmth=1
        2h    tool_error tool=net blocking=true
        3h    net_down
        12h   end

    Для user_message поля Appraisal кладутся в payload.appraisal, остальное — как есть.
    """
    from motus.events import KINDS, Appraisal
    script: List[Tuple[float, Optional[Event]]] = [(0.0, None)]
    end = 0.0
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            raise ValueError(f"строка {lineno}: нужно «время вид [ключ=значение…]»")
        off = parse_duration(parts[0])
        kind = parts[1]
        if kind == "end":
            end = max(end, off)
            continue
        if kind not in KINDS:
            raise ValueError(f"строка {lineno}: неизвестный вид события {kind!r}")
        payload: Dict[str, Any] = {}
        for kv in parts[2:]:
            if "=" not in kv:
                raise ValueError(f"строка {lineno}: {kv!r} — ожидается ключ=значение")
            k, v = kv.split("=", 1)
            payload[k] = _coerce(v)
        if kind == "user_message":
            ap = {k: payload.pop(k) for k in list(payload)
                  if k in Appraisal.RANGES or k == "agency_blocked"}
            payload["appraisal"] = ap
        script.append((off, Event(kind, 0.0, payload)))
        end = max(end, off)
    script.sort(key=lambda p: p[0])
    script.append((end, None))
    return script


def run_scenario(cfg: Dict[str, Any], script, start_hour: float = 0.0,
                 tick_s: float = 60.0) -> List[Dict[str, Any]]:
    cfg = copy.deepcopy(cfg)
    # Синтетика идёт в UTC-часах с нулевым смещением: стартовый час задаёт фазу суток.
    return replay.synthetic(cfg, script, tick_s=tick_s,
                            start_t=SYNTH_T0 + start_hour * 3600.0, tz_offset_s=0.0)


def silence_script(hours: float):
    return [(0.0, None), (hours * 3600.0, None)]


def timeline(rows: List[Dict[str, Any]]) -> List[str]:
    """Смены режима и срабатывания Tier 1/2 — человеческий таймлайн."""
    out: List[str] = []
    prev_regime = None
    prev_tier = 0
    for r in rows:
        at = fmt_dur(r["hours"] * 3600)
        if r["regime"] != prev_regime:
            top = sorted(r["drives"].items(), key=lambda kv: -kv[1])[:3]
            vals = " ".join(f"{k}={v:.2f}" for k, v in top)
            out.append(f"{at:>8}  режим → {r['regime']:<9} {vals}")
            prev_regime = r["regime"]
        if r["tier"] and r["tier"] != prev_tier:
            what = "Tier 2: ИНИЦИАЦИЯ контакта" if r["tier"] == 2 else "Tier 1: фоновая задача"
            out.append(f"{at:>8}  {what}")
        prev_tier = r["tier"]
    return out


def metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    first_t2 = next((r["hours"] for r in rows if r["tier"] == 2), None)
    tiers = [r["tier"] for r in rows]
    regimes: Dict[str, int] = {}
    for r in rows:
        regimes[r["regime"]] = regimes.get(r["regime"], 0) + 1
    share = sorted(regimes.items(), key=lambda kv: -kv[1])
    return {
        "first_initiation_h": first_t2,
        "tier1": _episodes(tiers, 1),
        "tier2": _episodes(tiers, 2),
        "regimes": [(k, round(100.0 * v / max(1, len(rows)))) for k, v in share],
    }


def _episodes(tiers: List[int], tier: int) -> int:
    """Число эпизодов подряд идущих тиков с данным tier (а не тиков)."""
    n, prev = 0, None
    for t in tiers:
        if t == tier and prev != tier:
            n += 1
        prev = t
    return n


def sweep(cfg: Dict[str, Any], path: str, values: Sequence[Any], script,
          start_hour: float = 0.0, tick_s: float = 60.0) -> List[Dict[str, Any]]:
    """Значение параметра → наблюдаемое поведение. Конфиг копируется, диск не трогается."""
    if not has_path(cfg, path):
        raise KeyError(path)
    out = []
    for v in values:
        cand = copy.deepcopy(cfg)
        set_path(cand, path, v)
        row: Dict[str, Any] = {"value": v}
        try:
            config.validate(cand)
        except (ConfigError, KeyError, TypeError) as exc:
            row["error"] = str(exc)
            out.append(row)
            continue
        row.update(metrics(run_scenario(cand, script, start_hour, tick_s)))
        out.append(row)
    return out


def frange(lo: float, hi: float, step: float) -> List[float]:
    if step <= 0:
        raise ValueError("шаг должен быть > 0")
    n = int(math.floor((hi - lo) / step + 1e-9)) + 1
    if n > 500:
        raise ValueError("слишком много точек (> 500)")
    return [round(lo + i * step, 10) for i in range(max(0, n))]


def records_from_boot(files: Sequence[Tuple[str, str]], day: str) -> List[Dict[str, Any]]:
    """Записи для реплея дня `day`: от последнего boot перед началом дня до конца дня.

    files — [(имя_файла, содержимое)] по возрастанию даты. Реплею нужен boot:
    дневной файл без рестарта демона начинается с середины жизни движка.
    """
    names = [n for n, _ in files]
    target = f"{day}.jsonl"
    if target not in names:
        raise KeyError(target)
    idx = names.index(target)

    def parse(text: str) -> List[Dict[str, Any]]:
        return [json.loads(ln) for ln in text.splitlines() if ln.strip()]

    day_recs = parse(files[idx][1])
    if day_recs and day_recs[0].get("kind") == "boot":
        return day_recs
    prefix: List[Dict[str, Any]] = []
    for _, text in reversed(files[:idx]):
        prefix = parse(text) + prefix
        boots = [i for i, r in enumerate(prefix) if r.get("kind") == "boot"]
        if boots:
            return prefix[boots[-1]:] + day_recs
    return day_recs  # раньше boot нет — реплей начнётся с первого boot внутри дня


def describe_divergences(divs, all_divs=None, legacy_boots: Sequence[int] = ()) -> List[str]:
    """Сводка расхождений дня по тикам + диагноз по САМОМУ первому расхождению
    прогона (all_divs): реплей мог начаться с boot в предыдущий день."""
    by_seq: Dict[int, list] = {}
    for dv in divs:
        by_seq.setdefault(dv.seq, []).append(dv)
    seqs = sorted(by_seq)
    out = [f"расхождений: {len(divs)} полей в {len(seqs)} тиках"]
    first = min(dv.seq for dv in (all_divs or divs))
    legacy_before = [b for b in legacy_boots if b < first]
    if legacy_before:
        out.append(f"⚠ журнал старого формата: boot seq {legacy_before[-1]} без полного состояния")
        out.append("  (до 2026-09-15). Реплей стартовал с начального вектора, а демон — со")
        out.append("  state.json, поэтому расхождение ожидаемо и о детерминизме ничего не говорит.")
    else:
        out.append("⚠ boot полного формата, а реплей всё равно разошёлся: баг детерминизма, правка")
        out.append("  состояния мимо журнала или конфиг, отличный от того, с которым работал демон.")
    for seq in seqs[:8]:
        g = by_seq[seq]
        when = _dt.datetime.fromtimestamp(g[0].t).strftime("%H:%M:%S")
        fields = ", ".join(f"{dv.field} {dv.expected}≠{dv.got}" for dv in g[:4])
        out.append(f"  seq {seq} {when}: {fields}" + (" …" if len(g) > 4 else ""))
    return out


# ============================================================== UI-примитивы


def ask(prompt: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    try:
        ans = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        raise KeyboardInterrupt
    return ans if ans else (default or "")


def confirm(prompt: str, default: bool = False) -> bool:
    ans = ask(f"{prompt} (да/нет)", "да" if default else "нет").lower()
    return ans in _TRUE


def choose(title: str, items: Sequence[str], zero: str = "назад") -> Optional[int]:
    print(f"\n== {title} ==")
    for i, it in enumerate(items, 1):
        print(f" {i:>2}. {it}")
    print(f"  0. {zero}")
    while True:
        a = ask("выбор")
        if a in ("0", "q", "й", ""):
            return None
        if a.isdigit() and 1 <= int(a) <= len(items):
            return int(a) - 1
        print("  нет такого пункта")


def fmt_val(v: Any) -> str:
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, str):
        return repr(v) if v == "" else v
    return json.dumps(v, ensure_ascii=False)


def hr(title: str = "") -> None:
    print("\n" + (f"— {title} " if title else "") + "—" * max(4, 60 - len(title)))


# ============================================================== приложение


class App:
    def __init__(self, settings_path: str = SETTINGS_PATH) -> None:
        self.settings_path = settings_path
        self.s = load_settings(settings_path)
        self.base = load_raw_config()
        self.pending = copy.deepcopy(self.base)
        if apply_console_timezone(self.pending):
            print(f"clock.timezone не задан — подставлен пояс консоли "
                  f"{self.pending['clock']['timezone']} (не сохранено)")

    # ---------------------------------------------------------------- цели

    @property
    def motus(self) -> Target:
        return Target(self.s["motus_via"])

    @property
    def openclaw(self) -> Target:
        return Target(self.s["openclaw_via"])

    def deploy_is_repo(self) -> bool:
        return (not self.s["motus_via"]
                and os.path.realpath(self.s["motus_config_dir"]) == os.path.realpath(REPO_CONFIG))

    # ---------------------------------------------------------------- админ-API

    def admin_get(self, path: str, socket_path: Optional[str] = None,
                  target: Optional[Target] = None, method: str = "GET") -> Optional[Dict[str, Any]]:
        sock = socket_path or self.s["motus_admin_socket"]
        cmd = ["curl", "-sS", "--max-time", "5", "--unix-socket", sock]
        if method == "POST":
            cmd += ["-X", "POST", "-H", "Content-Type: application/json", "-d", "{}"]
        cmd.append(f"http://motusd{path}")
        try:
            res = (target or self.motus).run(cmd, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if res.returncode != 0:
            return None
        try:
            return json.loads(res.stdout)
        except ValueError:
            return None

    def live_status(self) -> str:
        h = self.admin_get("/health")
        if not h:
            return f"motusd ({self.motus.label()}): нет ответа админ-сокета"
        raw = self.admin_get("/state/raw") or {}
        reg = raw.get("regime", "?")
        up = fmt_dur(h.get("uptime_s", 0))
        stale = self.config_newer_than_daemon(h.get("uptime_s", 0))
        lines = [f"motusd ({self.motus.label()}): работает {up}, режим {reg}"]
        if stale:
            lines.append("⚠ конфиг в деплое новее запуска демона — правка ещё не действует, нужен рестарт")
        if not self.deploy_is_repo():
            deployed = self.motus.read_file(os.path.join(self.s["motus_config_dir"], "default.json"))
            repo = open(os.path.join(REPO_CONFIG, "default.json"), encoding="utf-8").read()
            if deployed is not None and deployed != repo:
                lines.append("⚠ config/default.json в деплое расходится с git-репозиторием")
            if not self.deployed_code_is_current():
                lines.append("⚠ код motusd в деплое старше репозитория (нет clock.timezone/verbalizer.lexicon)")
        return "\n".join(lines)

    def deployed_code_is_current(self) -> bool:
        """Грубая, но честная проверка: знает ли задеплоенный config.py про assemble()."""
        if self.deploy_is_repo():
            return True
        src = self.motus.read_file(os.path.join(self.s["motus_code_dir"], "motus", "config.py"))
        return src is None or "def assemble(" in src

    def config_newer_than_daemon(self, uptime_s: float) -> bool:
        path = os.path.join(self.s["motus_config_dir"], "default.json")
        res = self.motus.run(["stat", "-c", "%Y", path])
        if res.returncode != 0:
            return False
        try:
            return float(res.stdout.strip()) > time.time() - float(uptime_s) + 1.0
        except ValueError:
            return False

    # ---------------------------------------------------------------- главное меню

    def main(self) -> None:
        print("motusctl — конфигуратор MOTUS")
        print(f"репозиторий: {REPO}")
        while True:
            hr()
            print(self.live_status())
            changes = diff_configs(self.base, self.pending)
            if changes:
                print(f"несохранённых изменений: {len(changes)}")
            items = [
                "Агент",
                "Пользователь",
                "Тесты драйвов",
                "OpenClaw",
                f"Сохранить и применить ({len(changes)})",
                "Отменить изменения",
            ]
            i = choose("MOTUS", items, zero="выход")
            if i is None:
                if changes and not confirm("Есть несохранённые изменения. Выйти без сохранения?"):
                    continue
                return
            [self.menu_agent, self.menu_user, self.menu_replay, self.menu_openclaw,
             self.save_flow, self.discard][i]()

    def discard(self) -> None:
        self.pending = copy.deepcopy(self.base)
        print("изменения отменены")

    # ---------------------------------------------------------------- правка ключей

    def menu_agent(self) -> None:
        while True:
            labels = ["Статус               главное одним экраном + L-1 оценка текста"]
            labels += [f"{t:<20} {d}" for t, _, d in AGENT_SECTIONS]
            i = choose("Агент", labels)
            if i is None:
                return
            if i == 0:
                self.menu_status()
                continue
            title, prefix, _ = AGENT_SECTIONS[i - 1]
            if prefix == "drives":
                self.menu_drives()
            else:
                self.edit_keys(title, [p for p, _ in leaves(self.pending[prefix], prefix)])

    def menu_drives(self) -> None:
        raw = self.admin_get("/state/raw") or {}
        live = raw.get("drives", {})
        while True:
            names = list(self.pending["drives"])
            labels = []
            for n in names:
                d = self.pending["drives"][n]
                cur = f"сейчас {live[n]:.2f}" if n in live else ""
                labels.append(f"{n:<8} setpoint={d['setpoint']} θhi={d['theta_hi']} "
                              f"θlo={d['theta_lo']} τ={d['tau_relax_s']}  {cur}")
            print("\n(живые значения драйвов — состояние, а не настройка: здесь не правятся)")
            i = choose("Драйвы", labels)
            if i is None:
                return
            n = names[i]
            self.edit_keys(f"Драйв {n}", [p for p, _ in leaves(self.pending["drives"][n], f"drives.{n}")])

    def menu_status(self) -> None:
        while True:
            hr("Статус")
            raw = self.admin_get("/state/raw") or {}
            if raw:
                drives = " ".join(f"{k}={v:.2f}" for k, v in raw.get("drives", {}).items())
                print(f"сейчас: режим {raw.get('regime')}; {drives}")
            else:
                print("сейчас: живое состояние недоступно (админ-сокет не ответил)")
            print()
            for path, why in STATUS_KEYS:
                val = get_path(self.pending, path) if has_path(self.pending, path) else "<не задано>"
                mark = " *" if has_path(self.base, path) and get_path(self.base, path) != val else ""
                print(f"  {path:<34} = {fmt_val(val):<16}{mark} {why}")
            ap = self.pending.get("appraisal", {})
            print(f"\n  L-1: режим {ap.get('mode')}, {ap.get('api')} / {ap.get('model')}, "
                  f"при отказе — {ap.get('model_fallback')}")
            print("  (* — изменено, не сохранено)")
            i = choose("Статус", ["L-1 оценка текста — изменить"])
            if i is None:
                return
            self.edit_keys("L-1 оценка текста",
                           [p for p, _ in leaves(self.pending["appraisal"], "appraisal")])

    def menu_user(self) -> None:
        while True:
            labels = []
            for p in USER_KEYS:
                cur = get_path(self.pending, p) if has_path(self.pending, p) else "<не задано>"
                was = get_path(self.base, p) if has_path(self.base, p) else "<не задано>"
                mark = " *" if cur != was else ""
                labels.append(f"{p:<44} = {fmt_val(cur)}{mark}   — {help_for(p)}")
            lex = config.lexicon_name(self.pending)
            labels.append(f"Язык карточки ({lex})                        — язык, на котором карточка говорит с моделью")
            labels.append(f"Запуск через (motus: {fmt_val(self.s['motus_via'])}, "
                          f"openclaw: {fmt_val(self.s['openclaw_via'])})")
            i = choose("Пользователь", labels)
            if i is None:
                return
            if i < len(USER_KEYS):
                self.edit_one(USER_KEYS[i])
            elif i == len(USER_KEYS):
                self.edit_one("verbalizer.lexicon")
            else:
                self.menu_settings()

    def edit_keys(self, title: str, paths: List[str]) -> None:
        while True:
            labels = []
            for p in paths:
                cur = get_path(self.pending, p) if has_path(self.pending, p) else "<не задано>"
                was = get_path(self.base, p) if has_path(self.base, p) else "<не задано>"
                mark = " *" if cur != was else ""
                h = help_for(p)
                labels.append(f"{p:<44} = {fmt_val(cur)}{mark}" + (f"   — {h}" if h else ""))
            i = choose(title, labels)
            if i is None:
                return
            self.edit_one(paths[i])

    def edit_one(self, path: str) -> None:
        old = get_path(self.pending, path) if has_path(self.pending, path) else ""
        if path == "clock.timezone":
            print(f"  пояс консоли: {console_timezone() or 'не определён'}; «-» — системный пояс контейнера")
        if path in DANGEROUS:
            print("\n⚠ " + DANGEROUS[path].replace("\n", "\n  "))
        raw = ask(f"{path} = {fmt_val(old)}; новое значение (пусто — не менять)")
        if raw == "":
            return
        if raw == "-" and isinstance(old, str):
            raw = " "
        try:
            val = parse_value(raw, old)
        except ValueError as exc:
            print(f"  не принято: {exc}")
            return
        if val == old:
            return
        if path in DANGEROUS and not confirm(f"Точно {path} = {fmt_val(val)}?"):
            return
        cand = copy.deepcopy(self.pending)
        set_path(cand, path, val, create=True)
        try:
            validate_candidate(cand)
        except ConfigError as exc:
            print(f"  отвергнуто валидатором: {exc}")
            return
        self.pending = cand
        print(f"  {path}: {fmt_val(old)} → {fmt_val(val)} (не сохранено)")

    # ---------------------------------------------------------------- сохранение

    def save_flow(self) -> None:
        changes = diff_configs(self.base, self.pending)
        if not changes:
            print("изменений нет")
            return
        hr("изменения")
        for p, a, b in changes:
            print(f"  {p}: {fmt_val(a)} → {fmt_val(b)}")
        try:
            validate_candidate(self.pending)
        except ConfigError as exc:
            print(f"валидатор отверг конфиг, ничего не записано: {exc}")
            return

        repo_path = os.path.join(REPO_CONFIG, "default.json")
        deploy_path = os.path.join(self.s["motus_config_dir"], "default.json")
        repo_text = open(repo_path, encoding="utf-8").read()
        if not self.deploy_is_repo():
            deployed = self.motus.read_file(deploy_path)
            if deployed is None:
                print(f"не удалось прочитать деплой {self.motus.label()}:{deploy_path}")
                if not confirm("Записать только в репозиторий, без деплоя?"):
                    return
            elif deployed != repo_text:
                print(f"\n⚠ деплой {self.motus.label()}:{deploy_path} РАСХОДИТСЯ с git:")
                for ln in list(difflib.unified_diff(repo_text.splitlines(), deployed.splitlines(),
                                                    "git", "деплой", lineterm=""))[:40]:
                    print("   " + ln)
                print("Запись через меню перезапишет деплой версией из git + ваши правки.")
                if not confirm("Продолжить?"):
                    return
        if not confirm("Записать?", default=True):
            return

        try:
            backup = save_config(self.pending)
        except (ConfigError, OSError) as exc:
            print(f"запись не удалась: {exc}")
            return
        print(f"записано: {repo_path}" + (f"\nбэкап:    {backup}" if backup else ""))
        self.base = copy.deepcopy(self.pending)

        if not self.deploy_is_repo():
            try:
                st = stamp()
                b = self.motus.backup_file(deploy_path, st)
                self.motus.write_file(deploy_path, open(repo_path, encoding="utf-8").read())
                print(f"деплой:   {self.motus.label()}:{deploy_path}" + (f" (бэкап {b})" if b else ""))
                self.sync_lexicon(st)
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                print(f"⚠ деплой не обновлён: {exc}\nрепозиторий и деплой теперь расходятся!")
                return
            if not self.deployed_code_is_current():
                print("\n⚠ Код motusd в деплое старше репозитория: он не знает clock.timezone и")
                print("  verbalizer.lexicon и молча их проигнорирует. Сначала задеплойте код (deploy/DEPLOY.md).")

        print("\n⚠ Правка НЕ действует, пока motusd не перезапущен: конфиг читается один раз при старте.")
        if confirm("Перезапустить motusd сейчас?"):
            self.restart_motusd()
        self.offer_commit(changes)

    def sync_lexicon(self, st: str) -> None:
        """Выбранный лексикон обязан лежать в деплое, иначе демон не стартует."""
        name = f"lexicon.{config.lexicon_name(self.pending)}.json"
        repo_text = open(os.path.join(REPO_CONFIG, name), encoding="utf-8").read()
        path = os.path.join(self.s["motus_config_dir"], name)
        deployed = self.motus.read_file(path)
        if deployed == repo_text:
            return
        if deployed is not None and not confirm(f"{name} в деплое отличается от git. Перезаписать версией из git?"):
            print(f"⚠ {name} в деплое оставлен как есть")
            return
        b = self.motus.backup_file(path, st)
        self.motus.write_file(path, repo_text)
        print(f"деплой:   {self.motus.label()}:{path}" + (f" (бэкап {b})" if b else " (новый файл)"))

    def restart_motusd(self) -> bool:
        cmd = ["systemctl", "restart", self.s["motus_unit"]]
        if not self.s["motus_via"] and os.geteuid() != 0:
            cmd = ["sudo"] + cmd
        res = self.motus.run(cmd, timeout=60)
        if res.returncode != 0:
            print(f"рестарт не удался: {(res.stderr or res.stdout).strip()}")
            return False
        return self.wait_health()

    def wait_health(self, socket_path: Optional[str] = None, timeout_s: float = 30.0,
                    target: Optional[Target] = None) -> bool:
        # Сокет поднимается не мгновенно — опрос, а не фиксированный sleep.
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            h = self.admin_get("/health", socket_path, target)
            if h and h.get("ok"):
                print(f"motusd отвечает: версия {h.get('version')}, seq {h.get('seq')}")
                return True
            time.sleep(0.5)
        print("motusd не ответил на /health за отведённое время")
        return False

    def offer_commit(self, changes) -> None:
        res = subprocess.run(["git", "-C", REPO, "status", "--porcelain", "config/default.json"],
                             capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if res.returncode != 0 or not res.stdout.strip():
            return
        keys = ", ".join(p for p, _, _ in changes[:4]) + (" …" if len(changes) > 4 else "")
        msg = f"Конфиг через motusctl: {keys}"
        print(f"\nconfig/default.json изменён в git-репозитории.")
        if confirm(f"Закоммитить («{msg}»)?"):
            subprocess.run(["git", "-C", REPO, "commit", "-m", msg, "--", "config/default.json"])

    # ---------------------------------------------------------------- реплей-стенд

    def menu_replay(self) -> None:
        print("\nВсё здесь работает на копии конфига в памяти и виртуальных часах: ни state.json,")
        print("ни config/ не пишутся. Берётся текущий конфиг ВМЕСТЕ с несохранёнными правками.")
        while True:
            i = choose("Тесты драйвов", [
                "Сценарий молчания",
                "Сценарий потока событий",
                "Подбор параметра (sweep)",
                "Прогон настоящего журнала",
            ])
            if i is None:
                return
            try:
                [self.replay_silence, self.replay_events, self.replay_sweep, self.replay_journal][i]()
            except (ValueError, KeyError, ConfigError) as exc:
                print(f"  ошибка: {exc}")

    def _sim_cfg(self) -> Dict[str, Any]:
        return validate_candidate(self.pending)

    def replay_silence(self) -> None:
        hours = parse_duration(ask("длительность молчания", "24h")) / 3600.0
        start = float(ask("стартовый час суток (локальный)", "12"))
        tick = float(ask("шаг тика, с", "60"))
        rows = run_scenario(self._sim_cfg(), silence_script(hours), start, tick)
        hr(f"молчание {hours:g} ч с {start:g}:00")
        for ln in timeline(rows):
            print(ln)
        self._print_metrics(metrics(rows), hours)

    def _print_metrics(self, m: Dict[str, Any], hours: float) -> None:
        fi = m["first_initiation_h"]
        print(f"\nпервая инициация: {fmt_dur(fi * 3600) if fi is not None else f'за {hours:g} ч ни разу'}")
        print(f"эпизодов Tier 1: {m['tier1']}, Tier 2: {m['tier2']}")
        print("режимы: " + ", ".join(f"{k} {v}%" for k, v in m["regimes"]))

    def replay_events(self) -> None:
        print("По строке на событие: «время вид ключ=значение…», пустая строка — конец.")
        print("Виды: user_message (valence threat novelty social_warmth loss agency_blocked),")
        print("      tool_error tool=net blocking=true, sensor …, net_down, net_up, «12h end».")
        lines = []
        while True:
            ln = input("  > ")
            if not ln.strip():
                break
            lines.append(ln)
        script = parse_script("\n".join(lines))
        start = float(ask("стартовый час суток", "12"))
        tick = float(ask("шаг тика, с", "60"))
        rows = run_scenario(self._sim_cfg(), script, start, tick)
        hr("поток событий")
        for ln in timeline(rows):
            print(ln)
        hours = script[-1][0] / 3600.0
        self._print_metrics(metrics(rows), hours)
        if rows:
            print("итог: " + " ".join(f"{k}={v:.2f}" for k, v in rows[-1]["drives"].items()))

    def replay_sweep(self) -> None:
        cfg = self._sim_cfg()
        path = ask("ключ (например heartbeat.theta_act, separation.max, drives.PANIC.theta_hi)")
        if not has_path(cfg, path):
            raise KeyError(f"нет такого ключа: {path}")
        cur = get_path(cfg, path)
        print(f"текущее: {fmt_val(cur)}")
        spec = ask("значения: «от до шаг» или список через запятую")
        if "," in spec or len(spec.split()) == 1:
            values = [parse_value(v, cur) for v in spec.split(",") if v.strip()]
        else:
            lo, hi, step = (float(x) for x in spec.split())
            values = frange(lo, hi, step)
            if isinstance(cur, int) and not isinstance(cur, bool):
                values = [int(v) if v == int(v) else v for v in values]
        hours = parse_duration(ask("сценарий: молчание длительностью", "24h")) / 3600.0
        start = float(ask("стартовый час суток", "12"))
        tick = float(ask("шаг тика, с", "60"))
        rows = sweep(cfg, path, values, silence_script(hours), start, tick)
        hr(f"sweep {path}, молчание {hours:g} ч с {start:g}:00")
        print(f"{'значение':>10} | {'1-я инициация':>14} | {'T1':>3} | {'T2':>3} | режимы")
        for r in rows:
            if "error" in r:
                print(f"{fmt_val(r['value']):>10} | невалидно: {r['error']}")
                continue
            fi = r["first_initiation_h"]
            fis = fmt_dur(fi * 3600) if fi is not None else "никогда"
            reg = ", ".join(f"{k} {v}%" for k, v in r["regimes"][:3])
            print(f"{fmt_val(r['value']):>10} | {fis:>14} | {r['tier1']:>3} | {r['tier2']:>3} | {reg}")
        print("\nПонравилось значение — поменяйте его в «Агент — гомеостат» и сохраните.")

    def replay_journal(self) -> None:
        jdir = os.path.join(self.s["motus_var_dir"], "journal")
        res = self.motus.run(["ls", jdir])
        if res.returncode != 0:
            print(f"не удалось прочитать {self.motus.label()}:{jdir}: {res.stderr.strip()}")
            return
        days = sorted(n[:-6] for n in res.stdout.split() if n.endswith(".jsonl"))
        if not days:
            print("журнал пуст")
            return
        i = choose("дата журнала", days[-14:])
        if i is None:
            return
        day = days[-14:][i]
        files: List[Tuple[str, str]] = []
        for d in days[: days.index(day) + 1][::-1]:
            text = self.motus.read_file(os.path.join(jdir, f"{d}.jsonl")) or ""
            files.insert(0, (f"{d}.jsonl", text))
            if d != day and '"kind":"boot"' in text:
                break
            if d == day and text.startswith('{"seq"') and '"kind":"boot"' in text.splitlines()[0]:
                break
        recs = records_from_boot(files, day)
        day_text = files[-1][1]
        first_line = next((ln for ln in day_text.splitlines() if ln.strip()), None)
        day_seq0 = json.loads(first_line)["seq"] if first_line else 0
        cfg = self._sim_cfg()
        t0 = time.time()
        r = replay.replay(cfg, recs)
        hr(f"реплей журнала {day}")
        print(f"записей {len(recs)}, тиков {r.ticks}, событий {r.events}, {time.time() - t0:.1f} с")
        divs = [dv for dv in r.divergences if dv.seq >= day_seq0]
        if not divs:
            print(f"расхождений за {day} нет — детерминизм цел")
            return
        for line in describe_divergences(divs, r.divergences, r.legacy_boots):
            print(line)

    # ---------------------------------------------------------------- openclaw

    OC_KEYS = [
        ("plugins.entries.motus.config.applyGate",
         "жёсткое применение гейта плагином: обрезка под max_tokens, отмена отправки при forbidden: outbound"),
        ("plugins.entries.motus.config.timeoutMs",
         "сколько плагин ждёт motusd, мс (model L-1 — десятки секунд)"),
    ]

    def oc(self, *args: str) -> subprocess.CompletedProcess:
        cmd = shlex.join([self.s["openclaw_bin"], *args])
        return self.openclaw.run_shell_as(self.s["openclaw_user"], cmd, timeout=120)

    def menu_openclaw(self) -> None:
        print(f"\nЭто другая система: конфиг openclaw ({self.openclaw.label()}), правка через")
        print("`openclaw config set`, применяется рестартом гейтвея (systemd --user), НЕ motusd.")
        while True:
            labels = []
            for path, h in self.OC_KEYS:
                res = self.oc("config", "get", path)
                val = res.stdout.strip().splitlines()[0] if res.returncode == 0 and res.stdout.strip() else "?"
                labels.append(f"{path.rsplit('.', 1)[-1]:<10} = {val:<7} — {h}")
            labels.append("перезапустить гейтвей openclaw")
            i = choose("OpenClaw", labels)
            if i is None:
                return
            if i == len(self.OC_KEYS):
                self.restart_gateway()
                continue
            path = self.OC_KEYS[i][0]
            res = self.oc("config", "get", path)
            try:
                old = json.loads(res.stdout.strip())
            except ValueError:
                old = ""
            raw = ask(f"{path} = {fmt_val(old)}; новое (пусто — не менять)")
            if not raw:
                continue
            try:
                val = parse_value(raw, old)
            except ValueError as exc:
                print(f"  не принято: {exc}")
                continue
            if path.endswith("applyGate") and val and not confirm(
                    "applyGate=true: плагин начнёт реально резать и отменять исходящее. Точно?"):
                continue
            self.oc_set(path, val)

    def oc_set(self, path: str, val: Any) -> None:
        f = self.oc("config", "file")
        cfg_file = f.stdout.strip().splitlines()[-1] if f.returncode == 0 and f.stdout.strip() else ""
        if cfg_file:
            cfg_file = os.path.expanduser(cfg_file) if not self.s["openclaw_via"] else cfg_file
            b = self.openclaw.run_shell_as(
                self.s["openclaw_user"],
                f"cp -p {shlex.quote(cfg_file)} {shlex.quote(cfg_file + '.bak-' + stamp())} && echo ok")
            if b.stdout.strip() != "ok":
                print(f"бэкап {cfg_file} не удался — ничего не меняю: {b.stderr.strip()}")
                return
            print(f"бэкап: {cfg_file}.bak-…")
        else:
            print("не удалось узнать путь openclaw.json — ничего не меняю")
            return
        res = self.oc("config", "set", path, json.dumps(val), "--strict-json")
        if res.returncode != 0:
            print(f"openclaw config set упал: {(res.stderr or res.stdout).strip()}")
            return
        print(f"{path} = {fmt_val(val)}")
        print("⚠ действует после рестарта гейтвея openclaw (не motusd).")
        if confirm("Перезапустить гейтвей сейчас?"):
            self.restart_gateway()

    def restart_gateway(self) -> None:
        script = (f'export XDG_RUNTIME_DIR=/run/user/$(id -u); '
                  f'systemctl --user restart {shlex.quote(self.s["openclaw_unit"])} && '
                  f'systemctl --user is-active {shlex.quote(self.s["openclaw_unit"])}')
        res = self.openclaw.run_shell_as(self.s["openclaw_user"], script, timeout=90)
        print(("гейтвей: " + res.stdout.strip()) if res.returncode == 0
              else f"рестарт гейтвея не удался: {(res.stderr or res.stdout).strip()}")

    # ---------------------------------------------------------------- настройки меню

    def menu_settings(self) -> None:
        keys = list(DEFAULT_SETTINGS)
        while True:
            labels = [f"{k:<20} = {fmt_val(self.s[k]):<38} — {SETTINGS_HELP[k]}" for k in keys]
            i = choose(f"Запуск через ({self.settings_path})", labels)
            if i is None:
                return
            k = keys[i]
            v = ask(f"{k} (пусто — оставить, «-» — очистить)", "")
            if v == "":
                continue
            v = "" if v == "-" else v
            if k.endswith("_via") and v:
                chk = subprocess.run(["incus", "info", v], capture_output=True, text=True,
                                     stdin=subprocess.DEVNULL)
                if chk.returncode != 0:
                    print(f"  incus не знает контейнер {v!r}: {chk.stderr.strip()}")
                    continue
            self.s[k] = v
            save_settings(self.s, self.settings_path)
            print(f"  сохранено: {k} = {fmt_val(v)}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="motusctl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--settings", default=SETTINGS_PATH, help="файл настроек меню")
    args = ap.parse_args(argv)
    try:
        App(args.settings).main()
    except KeyboardInterrupt:
        print("\nпрервано")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
