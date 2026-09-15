"""Тесты tools/motusctl.py и того, что под него добавлено в ядро (лексикон, часовой пояс).

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import copy
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from motus import config  # noqa: E402
from motus.clock import VirtualClock  # noqa: E402
from motus.config import ConfigError  # noqa: E402
from motus.engine import Engine  # noqa: E402
from motus.state import State  # noqa: E402

_spec = importlib.util.spec_from_file_location("motusctl", os.path.join(ROOT, "tools", "motusctl.py"))
motusctl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(motusctl)

T0 = 1767225600.0


def raw_cfg():
    return motusctl.load_raw_config()


class TempConfigDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="motusctl-test-")
        self.cdir = os.path.join(self.tmp, "config")
        shutil.copytree(os.path.join(ROOT, "config"), self.cdir,
                        ignore=shutil.ignore_patterns("*.bak-*"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ------------------------------------------------------------------ ядро: лексикон, пояс


class LexiconTests(unittest.TestCase):
    def test_lexicons_have_same_structure(self):
        ru = config.load_json(os.path.join(ROOT, "config", "lexicon.ru.json"))
        en = config.load_json(os.path.join(ROOT, "config", "lexicon.en.json"))

        def shape(node):
            if isinstance(node, dict):
                return {k: shape(v) for k, v in node.items() if not k.startswith("_")}
            if isinstance(node, list):
                return len(node)
            return type(node).__name__
        self.assertEqual(shape(ru), shape(en))

    def test_no_digits_in_any_lexicon(self):
        for lang in config.LEXICONS:
            text = open(os.path.join(ROOT, "config", f"lexicon.{lang}.json"), encoding="utf-8").read()
            body = json.loads(text)
            body.pop("_rule", None)
            self.assertIsNone(
                __import__("re").search(r"\d", json.dumps(body, ensure_ascii=False)), lang)

    def test_default_lexicon_is_en_when_key_absent(self):
        cfg = raw_cfg()
        cfg["verbalizer"].pop("lexicon", None)
        full = config.assemble(cfg)
        self.assertEqual(full["_lexicon"]["state_label"], "State")

    def test_operator_config_speaks_russian(self):
        self.assertEqual(config.lexicon_name(config.load()), "ru")

    def test_unknown_lexicon_rejected(self):
        cfg = raw_cfg()
        cfg["verbalizer"]["lexicon"] = "de"
        with self.assertRaises(ConfigError):
            config.assemble(cfg)

    def test_english_card_renders_without_russian_label(self):
        cfg = raw_cfg()
        cfg["verbalizer"]["lexicon"] = "en"
        full = config.assemble(cfg)
        eng = Engine(full, VirtualClock(T0))
        text = eng.tick(T0).card.text
        self.assertIn("State:", text)
        self.assertNotIn("Состояние", text)


class TimezoneTests(unittest.TestCase):
    def test_empty_means_system(self):
        cfg = raw_cfg()
        cfg["clock"]["timezone"] = ""
        self.assertIsNone(config.tz_offset_s(cfg, T0))

    def test_named_zone_offset(self):
        cfg = raw_cfg()
        cfg["clock"]["timezone"] = "Europe/Moscow"
        self.assertEqual(config.tz_offset_s(cfg, T0), 3 * 3600.0)

    def test_console_timezone_fills_only_empty(self):
        cfg = raw_cfg()
        cfg["clock"]["timezone"] = ""
        self.assertTrue(motusctl.apply_console_timezone(cfg, "America/Toronto"))
        self.assertEqual(cfg["clock"]["timezone"], "America/Toronto")
        self.assertFalse(motusctl.apply_console_timezone(cfg, "Europe/Moscow"))
        self.assertEqual(cfg["clock"]["timezone"], "America/Toronto")
        del cfg["clock"]
        self.assertTrue(motusctl.apply_console_timezone(cfg, "Europe/Moscow"))
        config.assemble(cfg)

    def test_console_timezone_from_env(self):
        with unittest.mock.patch.dict(os.environ, {"TZ": "Asia/Tokyo"}):
            self.assertEqual(motusctl.console_timezone(), "Asia/Tokyo")

    def test_unknown_zone_rejected_by_validate(self):
        cfg = raw_cfg()
        cfg["clock"]["timezone"] = "Mars/Olympus"
        with self.assertRaises(ConfigError):
            config.assemble(cfg)


# ------------------------------------------------------------------ правка конфига


class ValueParsingTests(unittest.TestCase):
    def test_types_follow_old_value(self):
        self.assertIs(motusctl.parse_value("да", False), True)
        self.assertIs(motusctl.parse_value("off", True), False)
        self.assertEqual(motusctl.parse_value("600", 1200), 600)
        self.assertEqual(motusctl.parse_value("1,1", 1.25), 1.1)
        self.assertEqual(motusctl.parse_value("Europe/Moscow", ""), "Europe/Moscow")

    def test_rejects_garbage_and_non_finite(self):
        with self.assertRaises(ValueError):
            motusctl.parse_value("может быть", True)
        for bad in ("nan", "inf", "-inf"):
            with self.assertRaises(ValueError):
                motusctl.parse_value(bad, 1.0)

    def test_set_path_refuses_unknown_key_without_create(self):
        cfg = raw_cfg()
        with self.assertRaises(KeyError):
            motusctl.set_path(cfg, "heartbeat.theta_actt", 1.0)

    def test_diff_ignores_comment_keys(self):
        a = raw_cfg()
        b = copy.deepcopy(a)
        b["budget"]["_note"] = "другой комментарий"
        b["heartbeat"]["theta_act"] = 1.2
        self.assertEqual(motusctl.diff_configs(a, b), [("heartbeat.theta_act", 1.1, 1.2)])


class SaveConfigTests(TempConfigDir):
    def _path(self):
        return os.path.join(self.cdir, "default.json")

    def test_invalid_config_writes_nothing_and_makes_no_backup(self):
        before = open(self._path(), encoding="utf-8").read()
        cfg = raw_cfg()
        cfg["drives"]["PANIC"]["theta_lo"] = 0.9  # выше theta_hi
        with self.assertRaises(ConfigError):
            motusctl.save_config(cfg, self.cdir)
        self.assertEqual(open(self._path(), encoding="utf-8").read(), before)
        self.assertEqual([f for f in os.listdir(self.cdir) if ".bak-" in f], [])

    def test_theta_act_below_theta_task_rejected(self):
        cfg = raw_cfg()
        cfg["heartbeat"]["theta_act"] = 0.5
        with self.assertRaises(ConfigError):
            motusctl.save_config(cfg, self.cdir)

    def test_missing_key_is_config_error_not_crash(self):
        cfg = raw_cfg()
        del cfg["heartbeat"]["tick_max_s"]
        with self.assertRaises(ConfigError):
            motusctl.save_config(cfg, self.cdir)

    def test_backup_then_write_same_format(self):
        before = open(self._path(), encoding="utf-8").read()
        cfg = raw_cfg()
        cfg["heartbeat"]["theta_act"] = 1.2
        backup = motusctl.save_config(cfg, self.cdir, now=T0)
        self.assertTrue(os.path.basename(backup).startswith("default.json.bak-"))
        self.assertEqual(open(backup, encoding="utf-8").read(), before)
        after = open(self._path(), encoding="utf-8").read()
        self.assertEqual(json.loads(after)["heartbeat"]["theta_act"], 1.2)
        # формат как в git: правка одного ключа — одна строка diff
        changed = [a for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
        self.assertEqual(len(changed), 1)
        self.assertEqual(len(before.splitlines()), len(after.splitlines()))

    def test_write_is_atomic_via_os_replace(self):
        cfg = raw_cfg()
        with unittest.mock.patch.object(motusctl.os, "replace", side_effect=OSError("диск")):
            with self.assertRaises(OSError):
                motusctl.save_config(cfg, self.cdir)
        leftovers = [f for f in os.listdir(self.cdir) if ".tmp." in f]
        self.assertEqual(leftovers, [])
        json.loads(open(self._path(), encoding="utf-8").read())  # файл цел

    def test_state_json_is_never_written(self):
        with self.assertRaises(PermissionError):
            motusctl.atomic_write_text(os.path.join(self.tmp, "state.json"), "{}")
        t = motusctl.Target("", runner=unittest.mock.Mock())
        with self.assertRaises(PermissionError):
            t.write_file("/var/lib/motus/state.json", "{}")
        t._runner.assert_not_called()

    def test_no_code_path_writes_state_json(self):
        src = open(os.path.join(ROOT, "tools", "motusctl.py"), encoding="utf-8").read()
        # Единственное упоминание записи state.json — засев песочницы под SANDBOX_PREFIX.
        hits = [ln for ln in src.splitlines() if "state.json" in ln and "write_file" in ln]
        self.assertEqual(len(hits), 1)
        self.assertIn("{root}/var/state.json", hits[0])


class TargetTests(unittest.TestCase):
    def test_local_and_incus_argv(self):
        self.assertEqual(motusctl.Target("").argv(["cat", "x"]), ["cat", "x"])
        self.assertEqual(motusctl.Target("motus").argv(["cat", "x"]),
                         ["incus", "exec", "motus", "--", "cat", "x"])
        self.assertEqual(motusctl.Target("motus").argv(["id"], user="motus"),
                         ["incus", "exec", "motus", "--", "runuser", "-u", "motus", "--", "id"])

    def test_settings_default_via_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            s = motusctl.load_settings(os.path.join(d, "nope.json"))
        self.assertEqual(s["motus_via"], "")
        self.assertEqual(s["openclaw_via"], "")

    def test_settings_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "settings.json")
            s = motusctl.load_settings(p)
            s["motus_via"] = "motus"
            motusctl.save_settings(s, p)
            self.assertEqual(motusctl.load_settings(p)["motus_via"], "motus")

    def test_local_file_write_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            t = motusctl.Target("")
            p = os.path.join(d, "default.json")
            t.write_file(p, '{"a": 1}')
            self.assertEqual(t.read_file(p), '{"a": 1}')
            b = t.backup_file(p, "20260101-000000")
            self.assertTrue(os.path.exists(b))


class SaveFlowDeployTests(TempConfigDir):
    """Полный save_flow на локальном «деплое» в другой каталог — без incus и systemd."""

    def _app(self, deploy_dir):
        app = motusctl.App.__new__(motusctl.App)
        app.settings_path = os.path.join(self.tmp, "settings.json")
        app.s = dict(motusctl.DEFAULT_SETTINGS, motus_config_dir=deploy_dir)
        app.base = motusctl.load_raw_config(self.cdir)
        app.pending = copy.deepcopy(app.base)
        return app

    def _run(self, app, answers):
        it = iter(answers)
        with unittest.mock.patch.object(motusctl, "REPO_CONFIG", self.cdir), \
                unittest.mock.patch.object(motusctl, "confirm", lambda *a, **k: next(it)), \
                unittest.mock.patch.object(motusctl.App, "offer_commit", lambda *a: None), \
                unittest.mock.patch("builtins.print"):
            orig = motusctl.save_config
            with unittest.mock.patch.object(motusctl, "save_config",
                                            lambda cfg: orig(cfg, self.cdir)):
                app.save_flow()

    def test_deploy_updated_and_restart_not_implied(self):
        deploy = os.path.join(self.tmp, "deploy")
        shutil.copytree(self.cdir, deploy)
        app = self._app(deploy)
        app.pending["heartbeat"]["theta_act"] = 1.2
        restarted = []
        app.restart_motusd = lambda: restarted.append(1)
        self._run(app, [True, False])  # записать — да, рестарт — нет
        for d in (self.cdir, deploy):
            self.assertEqual(json.load(open(os.path.join(d, "default.json")))["heartbeat"]["theta_act"], 1.2)
        self.assertTrue(any(".bak-" in f for f in os.listdir(deploy)))
        self.assertEqual(restarted, [])

    def test_drift_detected_and_declined_writes_nothing(self):
        deploy = os.path.join(self.tmp, "deploy")
        shutil.copytree(self.cdir, deploy)
        p = os.path.join(deploy, "default.json")
        drifted = open(p, encoding="utf-8").read().replace('"theta_act": 1.1', '"theta_act": 1.3')
        open(p, "w", encoding="utf-8").write(drifted)
        app = self._app(deploy)
        app.pending["heartbeat"]["theta_task"] = 0.95
        self._run(app, [False])  # «продолжить поверх расхождения?» — нет
        self.assertEqual(open(p, encoding="utf-8").read(), drifted)
        self.assertEqual(json.load(open(os.path.join(self.cdir, "default.json")))["heartbeat"]["theta_task"], 1.0)


# ------------------------------------------------------------------ реплей-стенд


class ReplayBenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = motusctl.validate_candidate(raw_cfg())

    def test_parse_duration(self):
        self.assertEqual(motusctl.parse_duration("90m"), 5400)
        self.assertEqual(motusctl.parse_duration("3h30m"), 12600)
        self.assertEqual(motusctl.parse_duration("2ч"), 7200)
        self.assertEqual(motusctl.parse_duration("24"), 86400)
        with self.assertRaises(ValueError):
            motusctl.parse_duration("завтра")

    def test_parse_script(self):
        script = motusctl.parse_script(
            "0 user_message novelty=1 social_warmth=1\n"
            "2h tool_error tool=net blocking=true  # сеть\n"
            "12h end\n")
        evs = [(off, ev) for off, ev in script if ev is not None]
        self.assertEqual(evs[0][1].payload, {"appraisal": {"novelty": 1, "social_warmth": 1}})
        self.assertEqual(evs[1][1].payload, {"tool": "net", "blocking": True})
        self.assertEqual(script[-1][0], 12 * 3600)
        with self.assertRaises(ValueError):
            motusctl.parse_script("1h teleport")

    def test_reference_calibration_from_spec(self):
        # Эталон §3.4: theta_act=1.25 → за 24ч ни разу, 1.1 → 3ч30м, 1.0 → 3ч14м.
        rows = motusctl.sweep(self.cfg, "heartbeat.theta_act", [1.0, 1.1, 1.25],
                              motusctl.silence_script(24), start_hour=20, tick_s=60)
        got = {r["value"]: r["first_initiation_h"] for r in rows}
        self.assertEqual(motusctl.fmt_dur(got[1.0] * 3600), "3ч14м")
        self.assertEqual(motusctl.fmt_dur(got[1.1] * 3600), "3ч30м")
        self.assertIsNone(got[1.25])

    def test_sweep_marks_invalid_values_and_does_not_touch_cfg(self):
        before = copy.deepcopy(self.cfg)
        rows = motusctl.sweep(self.cfg, "heartbeat.theta_act", [0.5, 1.1],
                              motusctl.silence_script(2))
        self.assertIn("error", rows[0])
        self.assertNotIn("error", rows[1])
        self.assertEqual(self.cfg, before)

    def test_bench_writes_no_files(self):
        with unittest.mock.patch("builtins.open", side_effect=AssertionError("открыт файл")), \
                unittest.mock.patch.object(motusctl.os, "replace", side_effect=AssertionError("запись")):
            rows = motusctl.run_scenario(self.cfg, motusctl.silence_script(6), 12, 300)
        self.assertTrue(motusctl.timeline(rows))

    def test_records_from_boot_spans_previous_day(self):
        def rec(seq, kind):
            return json.dumps({"seq": seq, "t": T0 + seq, "kind": kind, "payload": {}})
        files = [
            ("2026-09-12.jsonl", "\n".join([rec(1, "tick"), rec(2, "boot"), rec(3, "tick")])),
            ("2026-09-13.jsonl", "\n".join([rec(4, "tick"), rec(5, "boot"), rec(6, "tick")])),
        ]
        recs = motusctl.records_from_boot(files, "2026-09-13")
        self.assertEqual([r["seq"] for r in recs], [2, 3, 4, 5, 6])
        files[1] = ("2026-09-13.jsonl", "\n".join([rec(4, "boot"), rec(5, "tick")]))
        self.assertEqual([r["seq"] for r in motusctl.records_from_boot(files, "2026-09-13")], [4, 5])


# ------------------------------------------------------------------ песочница


class SandboxSeedTests(unittest.TestCase):
    def setUp(self):
        self.cfg = motusctl.validate_candidate(raw_cfg())

    def test_seed_clips_and_loads_back(self):
        d = motusctl.seed_state(self.cfg, {"SEEKING": 1.7, "PANIC": -0.2}, T0)
        st = State.from_dict(json.loads(json.dumps(d)))
        self.assertEqual(st.drives["SEEKING"], 1.0)
        self.assertEqual(st.drives["PANIC"], 0.0)

    def test_seed_rejects_nan_and_unknown(self):
        with self.assertRaises(ValueError):
            motusctl.seed_state(self.cfg, {"SEEKING": math.nan}, T0)
        with self.assertRaises(ValueError):
            motusctl.seed_state(self.cfg, {"JOY": 0.5}, T0)

    def test_parse_assignments(self):
        self.assertEqual(motusctl.parse_assignments("SEEKING=0.9, PANIC=0.4"),
                         {"SEEKING": "0.9", "PANIC": "0.4"})
        with self.assertRaises(ValueError):
            motusctl.parse_assignments("SEEKING")


if __name__ == "__main__":
    unittest.main()
