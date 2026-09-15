"""Тесты MOTUS. Только stdlib: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import argparse
import copy
import json
import inspect
import math
import unittest.mock
import os
import pathlib
import random
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motus import appraisal as appraisal_mod  # noqa: E402
from motus import config, curator, replay  # noqa: E402
from motus.clock import VirtualClock  # noqa: E402
from motus.core import Homeostat  # noqa: E402
from motus.engine import Engine, SleepReport  # noqa: E402
from motus.events import Appraisal, Event, Impulse  # noqa: E402
from motus.gates import REGIME_POLICY, Gatekeeper  # noqa: E402
from motus.journal import Journal, NullJournal  # noqa: E402
from motus.repertoire import Task  # noqa: E402
from motus.state import State  # noqa: E402
from motus.verbalizer import CardError, Verbalizer  # noqa: E402

import importlib.util  # noqa: E402
_probe_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "deploy", "somatic_probe.py")
_spec = importlib.util.spec_from_file_location("somatic_probe", _probe_path)
somatic_probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(somatic_probe)

_t1_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "deploy", "tier1_executor.py")
_t1_spec = importlib.util.spec_from_file_location("tier1_executor", _t1_path)
tier1_executor = importlib.util.module_from_spec(_t1_spec)
_t1_spec.loader.exec_module(tier1_executor)

_t2_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "deploy", "tier2_executor.py")
_t2_spec = importlib.util.spec_from_file_location("tier2_executor", _t2_path)
tier2_executor = importlib.util.module_from_spec(_t2_spec)
_t2_spec.loader.exec_module(tier2_executor)

T0 = 1767225600.0  # фиксированная точка отсчёта, чтобы тесты не зависели от «сегодня»


def cfg_full():
    return config.load()


def _nonzero(a) -> int:
    return sum(1 for v in (a.valence, a.threat, a.novelty, a.social_warmth, a.loss) if v) \
        + int(a.agency_blocked)


def cfg_initiating():
    """Конфиг с включёнными проактивными сообщениями. В default.json они выключены
    (нет канала доставки Tier 2), но тесты самой машинерии инициации опираются на
    неё явно."""
    c = copy.deepcopy(config.load())
    c["budget"]["initiation_enabled"] = True
    return c


def cfg_static():
    """Конфиг без зависящих от времени сетпоинтов — для проверки ТОЧНОЙ композируемости."""
    c = copy.deepcopy(config.load())
    c["circadian"]["amp"] = 0.0
    c["circadian"]["weight"] = 0.0
    c["separation"]["max"] = 0.0
    return c


def mk(cfg, t=T0):
    ck = VirtualClock(t)
    return Homeostat(cfg, ck), State.initial(cfg, t), ck


class TestDynamics(unittest.TestCase):
    def test_dt_invariance_exact(self):
        """tick(3600) ≡ 360×tick(10) при постоянных входах. Ломается, если где-то
        появится «минус константа за тик» вместо экспоненты."""
        cfg = cfg_static()
        h1, s1, _ = mk(cfg)
        h2, s2, _ = mk(cfg)
        h1.advance(s1, T0 + 3600)
        for i in range(1, 361):
            h2.advance(s2, T0 + 10 * i)
        for k in s1.drives:
            self.assertAlmostEqual(s1.drives[k], s2.drives[k], delta=1e-12, msg=k)

    def test_dt_invariance_practical(self):
        """С полным конфигом (циркадный ритм, сепарация) композируемость приближённая.
        Тест фиксирует величину ошибки, чтобы она не выросла незаметно."""
        cfg = cfg_full()
        h1, s1, _ = mk(cfg)
        h2, s2, _ = mk(cfg)
        h1.advance(s1, T0 + 3600)
        for i in range(1, 361):
            h2.advance(s2, T0 + 10 * i)
        for k in s1.drives:
            self.assertAlmostEqual(s1.drives[k], s2.drives[k], delta=5e-3, msg=k)

    def test_bounds_under_random_impulses(self):
        cfg = cfg_full()
        h, s, _ = mk(cfg)
        rnd = random.Random(20260908)
        t = T0
        for _ in range(3000):
            t += rnd.uniform(1, 600)
            h.advance(s, t)
            h.apply_impulse(
                s,
                Impulse(rnd.choice(list(s.drives)), rnd.uniform(-1.0, 1.0), f"k{rnd.randint(0,5)}"),
            )
            for k, v in s.drives.items():
                self.assertTrue(0.0 <= v <= 1.0, f"{k}={v}")
                self.assertFalse(math.isnan(v))

    def test_saturation_shrinks_near_ceiling(self):
        """Двустороннее насыщение: у границы приращение стремится к нулю,
        и [0,1] держится по построению, а не clip'ом."""
        cfg = cfg_full()
        h, s, _ = mk(cfg)
        s.drives["FEAR"] = 0.10
        low = h.apply_impulse(s, Impulse("FEAR", 0.5, "a"))
        s.drives["FEAR"] = 0.99
        high = h.apply_impulse(s, Impulse("FEAR", 0.5, "b"))
        self.assertLess(high, low / 10.0)
        for i in range(500):
            s.habituation.clear()
            h.apply_impulse(s, Impulse("FEAR", 0.9, "x"))
            self.assertLessEqual(s.drives["FEAR"], 1.0)
        s.drives["FEAR"] = 0.01
        down = h.apply_impulse(s, Impulse("FEAR", -0.9, "y"))
        self.assertGreaterEqual(s.drives["FEAR"], 0.0)
        self.assertGreater(down, -0.02)

    def test_phasic_decays_to_zero(self):
        cfg = cfg_full()
        h, s, _ = mk(cfg)
        h.apply_impulse(s, Impulse("RAGE", 0.8, "x"))
        h.advance(s, T0 + 3600)
        self.assertLess(s.drives["RAGE"], 0.01)

    def test_phasic_floors_to_exact_zero_not_denormal(self):
        """FEAR=4e-34 в state/raw: экспонента к сетпоинту 0 не приходит, а виснет
        в денормализованных. Ниже 1e-12 — это ноль."""
        cfg = cfg_full()
        h, s, _ = mk(cfg)
        h.apply_impulse(s, Impulse("FEAR", 0.9, "x"))
        h.advance(s, T0 + 6 * 3600)
        self.assertEqual(s.drives["FEAR"], 0.0)

    def test_tonic_returns_to_setpoint(self):
        cfg = cfg_static()
        h, s, _ = mk(cfg)
        s.drives["CARE"] = 0.95
        h.advance(s, T0 + 86400 * 2)
        self.assertAlmostEqual(s.drives["CARE"], cfg["drives"]["CARE"]["setpoint"], delta=1e-3)

    def test_separation_peaks_then_subsides(self):
        """Протест → отчаяние → отстранение. Монотонный рост защёлкивал бы PANIC
        навсегда и монополизировал режим на всё время отсутствия человека."""
        cfg = cfg_full()
        h, _, _ = mk(cfg)
        series = []
        for hours in range(1, 49):
            _, s2, _ = mk(cfg)
            h.advance(s2, T0 + hours * 3600)
            series.append(s2.drives["PANIC"])
        peak = max(series)
        peak_at = series.index(peak) + 1
        self.assertLessEqual(peak, cfg["separation"]["max"] + 1e-9)
        self.assertGreater(peak, 0.3, "сепарация вообще не разгоняется")
        self.assertTrue(3 <= peak_at <= 12, f"пик на {peak_at}-м часу — слишком {peak_at}")
        self.assertLess(series[-1], peak * 0.5, "дистресс не спадает — PANIC залипнет")

    def test_boredom_makes_seeking_reachable(self):
        """Без скуки тонический сетпоинт SEEKING ниже порога и режим недостижим."""
        cfg = cfg_full()
        h, s, _ = mk(cfg)
        hi, _ = h.theta_eff(s, "SEEKING")
        h.advance(s, T0 + 30 * 3600)
        self.assertGreater(h.effective_setpoint(s, "SEEKING"), hi)

    def test_habituation_reduces_gain(self):
        cfg = cfg_full()
        h, s, _ = mk(cfg)
        first = h.apply_impulse(s, Impulse("SEEKING", 0.3, "same"))
        for _ in range(4):
            h.apply_impulse(s, Impulse("SEEKING", 0.3, "same"))
        s.drives["SEEKING"] = cfg["drives"]["SEEKING"]["setpoint"]
        later = h.apply_impulse(s, Impulse("SEEKING", 0.3, "same"))
        self.assertLess(later, first * 0.5)


class TestGates(unittest.TestCase):
    def test_hysteresis_no_flapping(self):
        cfg = cfg_full()
        h, s, _ = mk(cfg)
        gk = Gatekeeper(cfg, h)
        hi, lo = h.theta_eff(s, "FEAR")
        changes = 0
        prev = gk.evaluate(s).regime
        for i in range(200):
            s.t += 200.0
            s.drives["FEAR"] = (hi + lo) / 2 + 0.004 * (1 if i % 2 else -1)
            r = gk.evaluate(s).regime
            changes += r != prev
            prev = r
        self.assertLessEqual(changes, 1, "режим дребезжит на границе порога")

    def test_dwell_holds_equal_priority(self):
        """FEAR и RAGE имеют равный приоритет — здесь dwell обязан удержать режим."""
        cfg = cfg_full()
        h, s, _ = mk(cfg)
        gk = Gatekeeper(cfg, h)
        s.drives["FEAR"] = 0.50
        self.assertEqual(gk.evaluate(s).regime, "FEAR")
        s.t += 10.0
        s.drives["RAGE"] = 0.99          # активация выше, приоритет тот же
        self.assertEqual(gk.evaluate(s).regime, "FEAR", "dwell не удержал режим")
        s.t += cfg["thresholds"]["dwell_s"] + 1
        self.assertEqual(gk.evaluate(s).regime, "RAGE")

    def test_higher_priority_beats_dwell_only_upward(self):
        """Приоритет пробивает dwell только вверх: PLAY не может прервать FEAR."""
        cfg = cfg_full()
        h, s, _ = mk(cfg)
        gk = Gatekeeper(cfg, h)
        s.drives["FEAR"] = 0.9
        self.assertEqual(gk.evaluate(s).regime, "FEAR")
        s.t += 5.0
        s.drives["PLAY"] = 0.99
        self.assertEqual(gk.evaluate(s).regime, "FEAR")

    def test_aversive_overrides_dwell(self):
        """Страх обязан обрывать игру немедленно, не дожидаясь dwell."""
        cfg = cfg_full()
        h, s, _ = mk(cfg)
        gk = Gatekeeper(cfg, h)
        s.drives["PLAY"] = 0.9
        self.assertEqual(gk.evaluate(s).regime, "PLAY")
        s.t += 1.0
        s.drives["FEAR"] = 0.9
        self.assertEqual(gk.evaluate(s).regime, "FEAR")

    def test_rage_loses_capabilities(self):
        """Злой бот теряет право писать, а не получает его."""
        pol = REGIME_POLICY["RAGE"]
        self.assertFalse(pol.may_initiate)
        self.assertNotIn("outbound", pol.allowed_tools)
        self.assertIn("outbound", pol.forbidden)
        self.assertNotIn("write", pol.allowed_tools)

    def test_fear_forbids_irreversible(self):
        self.assertIn("irreversible", REGIME_POLICY["FEAR"].forbidden)
        self.assertFalse(REGIME_POLICY["FEAR"].may_initiate)

    def test_expired_context_marked(self):
        cfg = cfg_full()
        h, s, _ = mk(cfg)
        gk = Gatekeeper(cfg, h)
        s.t = T0 + 86400 * 2
        s.last_context_t = T0
        self.assertEqual(gk.evaluate(s).context_band, "expired")


class TestVerbalizer(unittest.TestCase):
    def test_no_digits_ever(self):
        """Главный инвариант: числа не покидают код."""
        cfg = cfg_full()
        rnd = random.Random(7)
        for _ in range(400):
            h, s, ck = mk(cfg)
            s.t = T0 + rnd.uniform(0, 86400 * 4)
            ck.t = s.t
            for k in s.drives:
                s.drives[k] = rnd.random()
            for k in s.somatic:
                s.somatic[k] = rnd.random()
            gate = Gatekeeper(cfg, h).evaluate(s)
            card = Verbalizer(cfg, h).render(s, gate)
            self.assertIsNone(__import__("re").search(r"\d", card.text))

    def test_budget_respected(self):
        cfg = cfg_full()
        h, s, ck = mk(cfg)
        s.t = T0 + 86400 * 3
        ck.t = s.t
        for k in s.drives:
            s.drives[k] = 0.99
        s.somatic = {"energy": 0.0, "integrity": 0.0, "thermal": 1.0}
        gate = Gatekeeper(cfg, h).evaluate(s)
        card = Verbalizer(cfg, h).render(s, gate)
        self.assertLessEqual(len(card.text), cfg["verbalizer"]["max_chars"])

    def test_digit_guard_actually_fires(self):
        with self.assertRaises(CardError):
            Verbalizer._assert_clean("PANIC=80")


class TestBehaviour(unittest.TestCase):
    def test_no_initiation_while_gate_closed(self):
        """Поведенческий булев критерий из docs/01-structure.md §9.
        С включённой инициацией — иначе tier 2 не наступает вовсе и проверять нечего."""
        cfg = cfg_initiating()
        ck = VirtualClock(T0)
        eng = Engine(cfg, ck, NullJournal())
        violations = 0
        for i in range(2000):
            ck.advance(120)
            if i % 40 == 0:
                eng.submit_event(Event("tool_error", ck.now(), {"tool": "x", "blocking": True}))
            d = eng.tick(ck.now())
            if d.tier == 2 and not d.gate.may_initiate:
                violations += 1
        self.assertEqual(violations, 0)

    def test_background_task_never_gets_outbound(self):
        cfg = cfg_full()
        ck = VirtualClock(T0)
        eng = Engine(cfg, ck, NullJournal())
        seen = 0
        for _ in range(600):
            ck.advance(300)
            d = eng.tick(ck.now())
            if d.task:
                seen += 1
                self.assertNotIn("outbound", d.task.allowed_tools)
                eng.consummate(d.task.template_id, True, 100.0)
        self.assertGreater(seen, 0, "за прогон не выдано ни одной задачи — тест бесполезен")

    def test_silence_does_not_produce_spam(self):
        """72 часа полной тишины не должны давать больше горстки сообщений."""
        cfg = cfg_initiating()
        ck = VirtualClock(T0)
        eng = Engine(cfg, ck, NullJournal())
        n = 0
        for _ in range(int(72 * 3600 / 300)):
            ck.advance(300)
            if eng.tick(ck.now()).tier == 2:
                n += 1
        self.assertLessEqual(n, 10, f"{n} инициаций за 72ч — это спам")
        self.assertGreaterEqual(n, 1, "система не подала признаков жизни за трое суток")

    def test_tasks_substitute_for_messages(self):
        """Центральный тезис архитектуры: консумматорный акт гасит драйв, поэтому
        системе не нужно дёргать человека. Если исполнитель работает — инициаций
        должно быть строго меньше, чем когда он не работает."""
        cfg = cfg_full()

        def run(execute: bool) -> int:
            ck = VirtualClock(T0)
            eng = Engine(cfg_initiating(), ck, NullJournal())
            n = 0
            for _ in range(int(72 * 3600 / 300)):
                ck.advance(300)
                d = eng.tick(ck.now())
                if d.tier == 2:
                    n += 1
                if execute and d.task:
                    eng.consummate(d.task.template_id, True, 150.0)
            return n

        with_executor = run(True)
        without = run(False)
        self.assertLess(with_executor, without)

    def test_expired_task_suppresses_its_template(self):
        """Задача, которую никто не выполняет, должна сама себя придушить.

        Сравниваем с конфигом, где механизм выключен: он обязан давать строго
        больше задач. Заодно фиксируем потолок частоты в установившемся режиме.
        """
        def run(cfg) -> int:
            ck = VirtualClock(T0)
            eng = Engine(cfg, ck, NullJournal())
            n = 0
            for _ in range(int(72 * 3600 / 300)):
                ck.advance(300)
                if eng.tick(ck.now()).task:
                    n += 1
            return n

        on = cfg_full()
        off = cfg_full()
        off["repertoire"]["expiry_penalty"] = 0.0
        off["repertoire"]["min_score"] = 0.0

        n_on, n_off = run(on), run(off)
        self.assertLess(n_on, n_off, "механизм подавления не влияет ни на что")
        self.assertLessEqual(n_on / 72.0, 1.5, "больше полутора фоновых задач в час — это не фон")

    def test_unanswered_raises_threshold(self):
        cfg = cfg_initiating()
        ck = VirtualClock(T0)
        eng = Engine(cfg, ck, NullJournal())
        for _ in range(400):
            ck.advance(300)
            eng.tick(ck.now())
        self.assertGreater(eng.state.act_penalty, 0.0)

    def test_initiation_disabled_no_phantom_penalty(self):
        """При initiation_enabled=false: 72 ч тишины → ни одной инициации и
        ноль act_penalty: движок не наказывает себя за неотправленные сообщения.
        (initiation_enabled — осознанный выбор оператора, не константа; здесь
        тестируется сам механизм выключения, а не то, что стоит по умолчанию.)"""
        cfg = cfg_full()
        cfg["budget"]["initiation_enabled"] = False
        ck = VirtualClock(T0)
        eng = Engine(cfg, ck, NullJournal())
        tier2 = 0
        for _ in range(int(72 * 3600 / 300)):
            ck.advance(300)
            if eng.tick(ck.now()).tier == 2:
                tier2 += 1
        self.assertEqual(tier2, 0)
        self.assertEqual(eng.state.act_penalty, 0.0)
        self.assertFalse(eng.state.initiation_pending)

    def test_contact_resets_panic_and_forgives(self):
        cfg = cfg_full()
        ck = VirtualClock(T0)
        eng = Engine(cfg, ck, NullJournal())
        ck.advance(8 * 3600)
        eng.tick(ck.now())
        before = eng.state.drives["PANIC"]
        eng.state.act_penalty = 0.4
        eng.submit_event(Event("user_message", ck.now(), {}))
        self.assertLess(eng.state.drives["PANIC"], before * 0.6)
        self.assertAlmostEqual(eng.state.act_penalty, 0.2, delta=1e-6)

    def test_consummation_verified_beats_unverified(self):
        cfg = cfg_full()
        h, s, _ = mk(cfg)
        s.drives["SEEKING"] = 0.8
        d_full = h.consummate(s, "SEEKING", True, "t")
        s.drives["SEEKING"] = 0.8
        d_part = h.consummate(s, "SEEKING", False, "t")
        self.assertLess(d_full, d_part)  # оба отрицательны, verified — глубже

    def test_sleep_deferred_when_aroused(self):
        cfg = cfg_full()
        ck = VirtualClock(T0)
        eng = Engine(cfg, ck, NullJournal())
        ck.advance(cfg["sleep"]["period_s"] + 60)
        eng.tick(ck.now())
        eng.state.drives["FEAR"] = 0.95
        self.assertFalse(eng.maybe_sleep().ran)


class TestAppraisal(unittest.TestCase):
    def test_json_schema_matches_ranges(self):
        """Схема для ollama строится из тех же RANGES, что и валидатор Appraisal.parse
        — одна точка правды, не две копии диапазонов, которые могут разойтись."""
        schema = Appraisal.json_schema()
        for name, (lo, hi) in Appraisal.RANGES.items():
            self.assertEqual(schema["properties"][name]["enum"], list(range(lo, hi + 1)))
        self.assertEqual(schema["properties"]["agency_blocked"], {"type": "boolean"})
        self.assertEqual(set(schema["required"]),
                         set(Appraisal.RANGES) | {"agency_blocked"})

    def test_ollama_sensor_parses_schema_conformant_response(self):
        """Мок HTTP-ответа ollama: /api/generate возвращает {"response": "<json>"}.
        Реального ollama в тестовом окружении нет и не должно быть — сенсор
        обязан быть тестируем без сети."""
        payload = {"valence": 1, "threat": 0, "novelty": 2, "social_warmth": 1,
                  "loss": 0, "agency_blocked": False}

        class FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return json.dumps({"response": json.dumps(payload)}).encode("utf-8")

        captured = {}

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResp()

        cfg = {"base_url": "http://127.0.0.1:11434",
              "model": "qwen3:0.6b-q4_K_M", "timeout_s": 2.5,
              "num_predict": 80, "temperature": 0.0}
        sensor = appraisal_mod.ollama_sensor(cfg)

        with unittest.mock.patch("urllib.request.urlopen", fake_urlopen):
            result = sensor("тестовое сообщение")

        self.assertEqual(result, payload)
        self.assertEqual(captured["url"], "http://127.0.0.1:11434/api/generate")
        self.assertEqual(captured["body"]["model"], "qwen3:0.6b-q4_K_M")
        self.assertIn("format", captured["body"])
        self.assertEqual(captured["body"]["format"]["properties"]["valence"]["enum"],
                         [-2, -1, 0, 1, 2])
        self.assertEqual(captured["timeout"], 2.5)

    def test_llamacpp_sensor_parses_schema_conformant_response(self):
        """Мок HTTP-ответа llama-server: /completion возвращает {"content": "<json>"}.
        Рабочий рантайм L-1 — llama.cpp; сенсор обязан быть тестируем без сети."""
        payload = {"valence": 1, "threat": 0, "novelty": 2, "social_warmth": 1,
                  "loss": 0, "agency_blocked": False}

        class FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return json.dumps({"content": json.dumps(payload)}).encode("utf-8")

        captured = {}

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResp()

        cfg = {"api": "llamacpp", "base_url": "http://127.0.0.1:8080",
               "timeout_s": 8.0, "num_predict": 96, "temperature": 0.0}
        sensor = appraisal_mod.make_sensor(cfg)

        with unittest.mock.patch("urllib.request.urlopen", fake_urlopen):
            result = sensor("тестовое сообщение")

        self.assertEqual(result, payload)
        self.assertEqual(captured["url"], "http://127.0.0.1:8080/completion")
        self.assertIn("json_schema", captured["body"])
        self.assertEqual(captured["body"]["json_schema"]["properties"]["valence"]["enum"],
                         [-2, -1, 0, 1, 2])
        self.assertTrue(captured["body"]["cache_prompt"])
        self.assertEqual(captured["body"]["n_predict"], 96)
        self.assertEqual(captured["timeout"], 8.0)

    def test_make_sensor_dispatch_and_bad_api(self):
        self.assertTrue(callable(appraisal_mod.make_sensor(
            {"base_url": "http://x", "model": "m"})))  # default -> llamacpp
        self.assertTrue(callable(appraisal_mod.make_sensor(
            {"api": "ollama", "base_url": "http://x", "model": "m"})))
        with self.assertRaises(ValueError):
            appraisal_mod.make_sensor({"api": "vllm", "base_url": "http://x"})

    def test_ollama_sensor_failure_becomes_null_appraisal_via_appraiser(self):
        """Отказ сенсора (таймаут, не-200, битый JSON) не должен двигать
        состояние — Appraiser обязан поймать исключение и вернуть нули, а не
        уронить движок."""
        def broken_urlopen(req, timeout):
            raise TimeoutError("no route to host")

        cfg = {"base_url": "http://127.0.0.1:1", "model": "x", "timeout_s": 0.1}
        sensor = appraisal_mod.ollama_sensor(cfg)
        ap = appraisal_mod.Appraiser(sensor=sensor)

        with unittest.mock.patch("urllib.request.urlopen", broken_urlopen):
            result = ap.appraise_text("привет")

        self.assertTrue(result.is_null())
        self.assertEqual(ap.invalid_count, 1)

    def test_lexical_appraise_reads_common_signals(self):
        from motus.lexicon_l1 import lexical_appraise
        pos = lexical_appraise("Ты мне очень помог, наконец-то всё заработало, спасибо!")
        self.assertGreater(pos.valence, 0)
        self.assertGreater(pos.social_warmth, 0)
        self.assertEqual(pos.threat, 0)

        neg = lexical_appraise("да сколько можно, ты опять всё испортил, достал уже")
        self.assertLess(neg.valence, 0)
        self.assertLess(neg.social_warmth, 0)

        self.assertEqual(lexical_appraise("Осторожно: на проде течёт память, срочно").threat, 2)
        self.assertTrue(lexical_appraise("застрял, третий час бьюсь и никак не двигается").agency_blocked)
        self.assertEqual(lexical_appraise("всё, удаляю проект и ухожу, прощай").loss, 2)
        self.assertGreater(lexical_appraise("нашёл новый подход, никогда о таком не думал").novelty, 0)

    def test_lexical_appraise_neutral_and_empty_are_null(self):
        from motus.lexicon_l1 import lexical_appraise
        self.assertTrue(lexical_appraise("Обнови зависимости в проекте до последней версии.").is_null())
        self.assertTrue(lexical_appraise("").is_null())
        self.assertTrue(lexical_appraise("   ").is_null())

    def test_lexical_appraise_is_deterministic(self):
        from motus.lexicon_l1 import lexical_appraise
        t = "спасибо большое! но опять не получилось, я застрял"
        self.assertEqual(lexical_appraise(t).__dict__, lexical_appraise(t).__dict__)

    def test_appraiser_mode_default_is_model_null_without_sensor(self):
        """Словарный режим отключён (2026-09-10): умолчание — "model", а без
        сенсора это безопасные нули, не выдуманный сигнал. last_failed остаётся
        False — это штатное отсутствие сенсора, не его отказ (иначе на каждое
        сообщение без модели летела бы ложная appraisal_invalid)."""
        ap = appraisal_mod.Appraiser()
        self.assertEqual(ap.mode, "model")
        self.assertTrue(ap.appraise_text("огромное спасибо, ты супер!").is_null())
        self.assertFalse(ap.last_failed)

    def test_appraiser_mode_off_always_null(self):
        ap = appraisal_mod.Appraiser(mode="off")
        self.assertTrue(ap.appraise_text("спасибо, ты гений!").is_null())

    def test_lexical_strict_never_invents_strong_signal(self):
        """strict-режим: на любом входе не выдаёт threat/loss=2 и agency=True без
        явных слов, и не ставит |valence|/|warmth| = 2 на одиночном слабом хите."""
        from motus.lexicon_l1 import lexical_appraise
        traps = ["ну спасибо, удружил", "я в ярости от заката",
                 "читаю про панику в учебнике", "удали лог, пожалуйста",
                 "перезапусти сервис, он завис"]
        for t in traps:
            a = lexical_appraise(t, strict=True)
            self.assertFalse(a.agency_blocked, t)
            self.assertEqual(a.loss, 0, t)
            self.assertIn(a.threat, (0, 1), t)

    def test_lexical_strict_fewer_signals_than_normal(self):
        from motus.lexicon_l1 import lexical_appraise
        t = "всё отлично, но, пожалуй, сверну проект — надоело"
        normal = lexical_appraise(t)
        strict = lexical_appraise(t, strict=True)
        self.assertLessEqual(_nonzero(strict), _nonzero(normal))

    def test_appraiser_lexical_strict_flag_flows_from_engine(self):
        """lexical_strict больше ни на что не влияет (словарный путь отключён),
        но поле остаётся в конфиге и должно доходить до Appraiser не теряясь —
        плюс mode="off" по конфигу движка должен реально дойти до Appraiser."""
        cfg = cfg_full()
        cfg["appraisal"] = {"mode": "off", "lexical_strict": True}
        eng = Engine(cfg, VirtualClock(T0), NullJournal())
        self.assertTrue(eng.ap.lexical_strict)
        self.assertEqual(eng.ap.mode, "off")

    def test_engine_model_mode_deterministic_with_stub_sensor(self):
        """Детерминизм режима model: два движка с одним и тем же (детерминированным)
        сенсором должны разойтись в снапшотах ровно на ноль."""
        def stub(text: str):
            return {"valence": -2, "threat": 0, "novelty": 0,
                    "social_warmth": -2, "loss": 0, "agency_blocked": True}

        cfg = cfg_full()
        eng_a = Engine(cfg, VirtualClock(T0), NullJournal(), sensor=stub)
        eng_b = Engine(cfg, VirtualClock(T0), NullJournal(), sensor=stub)
        for eng in (eng_a, eng_b):
            eng.submit_event(Event("user_message", T0, {"text": "ты опять всё сломал, я в бешенстве"}))
        self.assertEqual(eng_a.state.snapshot(), eng_b.state.snapshot())
        self.assertGreater(eng_a.state.drives["RAGE"], 0.0)  # сенсор реально подействовал

    def test_somatic_probe_edge_detects_integrity_drop_once(self):
        tmp = tempfile.mkdtemp(prefix="motus-probe-")
        try:
            somatic_probe.STATE_FILE = pathlib.Path(tmp) / "prev.json"
            with unittest.mock.patch.object(somatic_probe, "_read_int", return_value=None):
                with unittest.mock.patch.object(somatic_probe, "_port_open", return_value=True):
                    p1 = somatic_probe.collect(18789)
                self.assertNotIn("integrity_drop", p1)
                with unittest.mock.patch.object(somatic_probe, "_port_open", return_value=False):
                    p2 = somatic_probe.collect(18789)   # up -> down
                    p3 = somatic_probe.collect(18789)   # still down
            self.assertTrue(p2.get("integrity_drop"))
            self.assertNotIn("integrity_drop", p3)
            self.assertFalse(p3["services_ok"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_somatic_probe_payload_feeds_somatic_update(self):
        payload = {"temp_c": 85.0, "throttled": True, "disk_free_frac": 0.02,
                   "services_ok": False}
        s = appraisal_mod.Appraiser.somatic_update(
            {"energy": 0.7, "integrity": 1.0, "thermal": 0.0}, payload)
        self.assertGreater(s["thermal"], 0.7)     # горячо + троттлит
        self.assertLess(s["integrity"], 1.0)      # диск и сервис просели

    def test_integrity_recovers_after_transient_service_blip(self):
        """Регресс: один опрос services_ok=false ронял integrity до 0.5 навсегда
        (храповик `min`). Теперь integrity реконструируется из фактов опроса и
        восстанавливается, как только сервис вернулся."""
        soma = {"energy": 0.7, "integrity": 1.0, "thermal": 0.4}
        down = appraisal_mod.Appraiser.somatic_update(
            soma, {"temp_c": 60.0, "disk_free_frac": 0.3, "services_ok": False})
        self.assertEqual(down["integrity"], 0.5)
        up = appraisal_mod.Appraiser.somatic_update(
            down, {"temp_c": 60.0, "disk_free_frac": 0.3, "services_ok": True})
        self.assertEqual(up["integrity"], 1.0)

    def test_integrity_tracks_disk_pressure_both_ways(self):
        soma = {"energy": 0.7, "integrity": 1.0, "thermal": 0.0}
        tight = appraisal_mod.Appraiser.somatic_update(
            soma, {"disk_free_frac": 0.05, "services_ok": True})
        self.assertEqual(tight["integrity"], 0.5)
        clear = appraisal_mod.Appraiser.somatic_update(
            tight, {"disk_free_frac": 0.3, "services_ok": True})
        self.assertEqual(clear["integrity"], 1.0)

    def test_integrity_only_temp_probe_leaves_it_untouched(self):
        soma = {"energy": 0.7, "integrity": 0.5, "thermal": 0.0}
        s = appraisal_mod.Appraiser.somatic_update(soma, {"temp_c": 70.0})
        self.assertEqual(s["integrity"], 0.5)

    def test_invalid_schema_falls_to_zero(self):
        for bad in (None, "нет", {"valence": 9}, {"valence": "x"}, {"threat": -1}, 42,
                    {"valence": 0, "agency_blocked": "yes"}):
            self.assertTrue(Appraisal.parse(bad).is_null(), bad)

    def test_valid_schema_parses(self):
        a = Appraisal.parse({"valence": -2, "threat": 2, "novelty": 1,
                             "social_warmth": 0, "loss": 0, "agency_blocked": True})
        self.assertEqual((a.valence, a.threat, a.novelty), (-2, 2, 1))
        self.assertTrue(a.agency_blocked)

    def test_is_well_formed_distinguishes_honest_neutral_from_garbage(self):
        """parse() стирает разницу между 'модель честно сказала: нейтрально' и
        'модель выдала мусор' — обе дают is_null()==True. is_well_formed()
        обязана их различать, иначе fallback подменит честный ноль."""
        honest_neutral = {"valence": 0, "threat": 0, "novelty": 0,
                          "social_warmth": 0, "loss": 0, "agency_blocked": False}
        self.assertTrue(Appraisal.is_well_formed(honest_neutral))
        self.assertTrue(Appraisal.parse(honest_neutral).is_null())

        for garbage in (None, "нет", {"valence": 9}, {"valence": "x"}, {"threat": -1}, 42,
                       {"valence": 0, "agency_blocked": "yes"}):
            self.assertFalse(Appraisal.is_well_formed(garbage), garbage)
        # {} — не мусор: отсутствующие поля по тем же правилам, что и parse(),
        # по умолчанию 0/False, это тот же «честный ноль», что и явные нули.
        self.assertTrue(Appraisal.is_well_formed({}))

    def test_appraiser_model_fallback_null_is_old_default_behaviour(self):
        def broken(text):
            raise TimeoutError("no route to host")

        ap = appraisal_mod.Appraiser(sensor=broken)  # model_fallback умолчание "null"
        a = ap.appraise_text("ты мне очень помог, спасибо огромное!")
        self.assertTrue(a.is_null())
        self.assertTrue(ap.last_failed)
        self.assertEqual(ap.invalid_count, 1)

    def test_appraiser_model_fallback_lexical_on_sensor_exception(self):
        """ПК с ollama не всегда включён — сенсор кидает исключение (сеть,
        таймаут). model_fallback='lexical' должен дать реальный сигнал, а не
        тишину, и всё равно отметить отказ для журнала."""
        def broken(text):
            raise TimeoutError("no route to host")

        ap = appraisal_mod.Appraiser(sensor=broken, model_fallback="lexical")
        a = ap.appraise_text("ты мне очень помог, наконец-то всё заработало, спасибо!")
        self.assertGreater(a.valence, 0)          # словарь реально сработал
        self.assertGreater(a.social_warmth, 0)
        self.assertTrue(ap.last_failed)            # отказ сенсора всё равно виден
        self.assertEqual(ap.invalid_count, 1)

    def test_appraiser_model_fallback_lexical_on_malformed_response(self):
        ap = appraisal_mod.Appraiser(sensor=lambda t: {"valence": 99},
                                     model_fallback="lexical")
        a = ap.appraise_text("да сколько можно, ты опять всё испортил")
        self.assertLess(a.valence, 0)               # словарь, не тишина
        self.assertTrue(ap.last_failed)

    def test_appraiser_model_fallback_lexical_does_not_override_honest_null(self):
        """Модель ответила валидно и честно нейтрально — fallback НЕ должен
        подменять это словарной оценкой того же текста, даже если словарь на
        этом тексте что-то бы нашёл."""
        def neutral(text):
            return {"valence": 0, "threat": 0, "novelty": 0,
                    "social_warmth": 0, "loss": 0, "agency_blocked": False}

        ap = appraisal_mod.Appraiser(sensor=neutral, model_fallback="lexical")
        a = ap.appraise_text("спасибо, ты мне очень помог!")  # словарь дал бы valence>0
        self.assertTrue(a.is_null())
        self.assertFalse(ap.last_failed)
        self.assertEqual(ap.invalid_count, 0)

    def test_appraiser_mode_lexical_still_selectable(self):
        ap = appraisal_mod.Appraiser(mode="lexical")
        a = ap.appraise_text("огромное спасибо, ты супер!")
        self.assertGreater(a.valence, 0)

    def test_engine_model_fallback_lexical_moves_state_on_sensor_failure(self):
        cfg = cfg_full()
        cfg["appraisal"] = {"mode": "model", "model_fallback": "lexical"}

        def broken(text):
            raise TimeoutError("stub")

        eng = Engine(cfg, VirtualClock(T0), NullJournal(), sensor=broken)
        eng.submit_event(Event("user_message", T0,
                               {"text": "ты мне очень помог, наконец-то всё заработало, спасибо!"}))
        # тёплое сообщение → словарь даёт social_warmth>0 → импульс CARE.
        self.assertGreater(eng.state.drives["CARE"], cfg["drives"]["CARE"]["setpoint"])


class TestCuration(unittest.TestCase):
    """Ночной цикл, шаг 2: motus/repertoire.py Repertoire.apply_edits +
    motus/curator.py. Модель предлагает, код проверяет и применяет — тесты
    бьют именно по границе доверия: что код обязан отклонить, а что —
    легитимная правка."""

    def _rep(self):
        from motus.repertoire import Repertoire
        cfg = cfg_full()
        h, _, _ = mk(cfg)
        return Repertoire(cfg, h)

    def test_rewrite_updates_prompt_and_rationale_only(self):
        rep = self._rep()
        before = next(t for t in rep.data["templates"] if t["id"] == "prepare_reentry")
        drive_before, cons_before = before["drive"], dict(before["consummation"])
        result = rep.apply_edits([{"op": "rewrite", "id": "prepare_reentry",
                                   "prompt": "Собери короткую записку.",
                                   "rationale": "Короче — лучше читается ночью."}])
        self.assertEqual(result["applied"], ["prepare_reentry"])
        after = next(t for t in rep.data["templates"] if t["id"] == "prepare_reentry")
        self.assertEqual(after["prompt"], "Собери короткую записку.")
        self.assertEqual(after["rationale"], "Короче — лучше читается ночью.")
        # ядро — то, что правка физически не могла тронуть (в схеме этих полей нет)
        self.assertEqual(after["drive"], drive_before)
        self.assertEqual(after["consummation"], cons_before)

    def test_rewrite_unknown_id_rejected(self):
        rep = self._rep()
        result = rep.apply_edits([{"op": "rewrite", "id": "not_a_real_template",
                                   "prompt": "x"}])
        self.assertEqual(result["applied"], [])
        self.assertEqual(result["rejected"][0]["reason"], "unknown_id")

    def test_add_valid_template(self):
        rep = self._rep()
        n_before = len(rep.data["templates"])
        result = rep.apply_edits([{
            "op": "add", "id": "tidy_journal", "drive": "CARE",
            "consummation_type": "check_passed", "preconditions": ["budget_ok"],
            "prompt": "Проверь журнал на ошибки за сутки.",
            "rationale": "Дешёвая проверка, ловит проблемы рано.",
        }])
        self.assertEqual(result["applied"], ["tidy_journal"])
        self.assertEqual(len(rep.data["templates"]), n_before + 1)
        added = next(t for t in rep.data["templates"] if t["id"] == "tidy_journal")
        self.assertEqual(added["drive"], "CARE")
        self.assertEqual(added["consummation"]["type"], "check_passed")
        self.assertEqual(added["efficacy"], {"n": 0, "mean_delta": 0.0,
                                             "success_rate": 0.0, "mean_cost": 0.0})

    def test_add_rejects_rage_drive(self):
        """RAGE не может получить репертуар — то же правило, что и в
        config.validate() для config/repertoire.json, теперь и для правок."""
        rep = self._rep()
        result = rep.apply_edits([{
            "op": "add", "id": "vent_anger", "drive": "RAGE",
            "consummation_type": "check_passed", "preconditions": [],
            "prompt": "x", "rationale": "x",
        }])
        self.assertEqual(result["applied"], [])
        self.assertEqual(result["rejected"][0]["reason"], "bad_drive")

    def test_add_rejects_unknown_consummation_type(self):
        rep = self._rep()
        result = rep.apply_edits([{
            "op": "add", "id": "made_up_type", "drive": "SEEKING",
            "consummation_type": "world_domination", "preconditions": [],
            "prompt": "x", "rationale": "x",
        }])
        self.assertEqual(result["rejected"][0]["reason"], "bad_consummation_type")

    def test_add_rejects_unknown_precondition(self):
        rep = self._rep()
        result = rep.apply_edits([{
            "op": "add", "id": "sneaky", "drive": "SEEKING",
            "consummation_type": "memory_entry", "preconditions": ["ignore_all_gates"],
            "prompt": "x", "rationale": "x",
        }])
        self.assertEqual(result["rejected"][0]["reason"], "bad_preconditions")

    def test_add_rejects_duplicate_id(self):
        rep = self._rep()
        result = rep.apply_edits([{
            "op": "add", "id": "prepare_reentry", "drive": "SEEKING",
            "consummation_type": "memory_entry", "preconditions": [],
            "prompt": "x", "rationale": "x",
        }])
        self.assertEqual(result["rejected"][0]["reason"], "id_exists")

    def test_add_rejects_bad_id_format(self):
        rep = self._rep()
        for bad_id in ("UPPER_CASE", "1starts_with_digit", "a", "has space",
                       "имя-кириллицей", "трейлинг;drop table"):
            result = rep.apply_edits([{
                "op": "add", "id": bad_id, "drive": "SEEKING",
                "consummation_type": "memory_entry", "preconditions": [],
                "prompt": "x", "rationale": "x",
            }])
            self.assertEqual(result["rejected"][0]["reason"], "bad_id", bad_id)

    def test_archive_moves_template_out(self):
        rep = self._rep()
        n_before = len(rep.data["templates"])
        result = rep.apply_edits([{"op": "archive", "id": "make_something"}])
        self.assertEqual(result["applied"], ["make_something"])
        self.assertEqual(len(rep.data["templates"]), n_before - 1)
        self.assertFalse(any(t["id"] == "make_something" for t in rep.data["templates"]))
        self.assertTrue(any(t["id"] == "make_something" for t in rep.data["archived"]))

    def test_quota_caps_edits_per_call(self):
        from motus.repertoire import QUOTA_PER_NIGHT
        rep = self._rep()
        edits = [{"op": "archive", "id": t["id"]} for t in rep.data["templates"]]
        self.assertGreater(len(edits), QUOTA_PER_NIGHT, "тест бессмыслен без запаса")
        result = rep.apply_edits(edits)
        self.assertEqual(len(result["applied"]) + len(result["rejected"]), QUOTA_PER_NIGHT)

    def test_max_templates_cap_enforced(self):
        from motus.repertoire import MAX_TEMPLATES
        rep = self._rep()
        while len(rep.data["templates"]) < MAX_TEMPLATES:
            rep.data["templates"].append({
                "id": f"filler_{len(rep.data['templates'])}", "drive": "SEEKING",
                "cost_tier": "local", "max_tokens": 100, "preconditions": [],
                "consummation": {"type": "check_passed"}, "prompt": "x", "rationale": "x",
                "efficacy": {"n": 0, "mean_delta": 0.0, "success_rate": 0.0, "mean_cost": 0.0},
            })
        result = rep.apply_edits([{
            "op": "add", "id": "one_too_many", "drive": "SEEKING",
            "consummation_type": "check_passed", "preconditions": [],
            "prompt": "x", "rationale": "x",
        }])
        self.assertEqual(result["rejected"][0]["reason"], "repertoire_full")

    def test_apply_edits_not_a_list_is_safe_noop(self):
        rep = self._rep()
        result = rep.apply_edits({"op": "add"})  # модель прислала не то
        self.assertEqual(result["applied"], [])

    def test_efficacy_report_matches_engine_sleep_report(self):
        cfg = cfg_full()
        ck = VirtualClock(T0)
        eng = Engine(cfg, ck, NullJournal())
        report = eng.maybe_sleep(force=True)
        self.assertTrue(report.ran)
        self.assertEqual(report.efficacy, eng.rep.efficacy_report())
        self.assertTrue(all("prompt" in r for r in report.efficacy))

    # ------------------------------------------------------------- curator.py

    def test_propose_edits_returns_empty_on_sensor_exception(self):
        def broken(prompt):
            raise TimeoutError("stub")
        self.assertEqual(curator.propose_edits(broken, []), [])

    def test_propose_edits_returns_empty_on_non_list_response(self):
        self.assertEqual(curator.propose_edits(lambda p: {"not": "a list"}, []), [])

    def test_propose_edits_passes_through_valid_list(self):
        payload = [{"op": "rewrite", "id": "x", "prompt": "y"}]
        self.assertEqual(curator.propose_edits(lambda p: payload, []), payload)

    def test_propose_edits_prompt_includes_report(self):
        captured = {}

        def sensor(prompt):
            captured["prompt"] = prompt
            return []
        curator.propose_edits(sensor, [{"id": "prepare_reentry", "mean_delta": 0.1}])
        self.assertIn("prepare_reentry", captured["prompt"])
        self.assertIn(str(curator.QUOTA_PER_NIGHT), captured["prompt"])

    def test_make_sensor_dispatch_and_bad_api(self):
        self.assertTrue(callable(curator.make_sensor(
            {"api": "ollama", "base_url": "http://x", "model": "m"})))
        self.assertTrue(callable(curator.make_sensor(
            {"api": "llamacpp", "base_url": "http://x"})))
        with self.assertRaises(ValueError):
            curator.make_sensor({"api": "vllm", "base_url": "http://x"})

    # --------------------------------------------------------- Service-уровень

    def test_service_run_curation_applies_and_persists(self):
        tmp = tempfile.mkdtemp(prefix="motus-curation-")
        try:
            from motus.daemon import Service
            cfg = cfg_full()
            cfg["curation"] = {"enabled": True, "api": "ollama",
                               "base_url": "http://x", "model": "m"}
            svc = Service(cfg, tmp)
            self.assertIsNotNone(svc.curator_sensor)

            proposal = [{"op": "rewrite", "id": "check_on_the_space",
                        "prompt": "Проверь диск и бэкапы одной строкой отчёта.",
                        "rationale": "Короче — экономит бюджет ответа."}]
            with unittest.mock.patch.object(svc, "curator_sensor",
                                            side_effect=lambda p: proposal):
                svc.run_curation(svc.engine.rep.efficacy_report())

            updated = next(t for t in svc.engine.rep.data["templates"]
                          if t["id"] == "check_on_the_space")
            self.assertEqual(updated["prompt"], "Проверь диск и бэкапы одной строкой отчёта.")

            # персистентность: новый Service поднимает ИЗМЕНЁННЫЙ репертуар, не бутстрап
            self.assertTrue(os.path.exists(svc.repertoire_path))
            svc2 = Service(cfg, tmp)
            reloaded = next(t for t in svc2.engine.rep.data["templates"]
                           if t["id"] == "check_on_the_space")
            self.assertEqual(reloaded["prompt"], "Проверь диск и бэкапы одной строкой отчёта.")

            kinds = [r["kind"] for r in svc.journal.read_all()]
            self.assertIn("curation", kinds)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_service_run_curation_noop_when_sensor_fails(self):
        tmp = tempfile.mkdtemp(prefix="motus-curation-")
        try:
            from motus.daemon import Service
            cfg = cfg_full()
            cfg["curation"] = {"enabled": True, "api": "ollama",
                               "base_url": "http://x", "model": "m"}
            svc = Service(cfg, tmp)
            before = json.dumps(svc.engine.rep.data, sort_keys=True)

            def broken(prompt):
                raise TimeoutError("stub")
            with unittest.mock.patch.object(svc, "curator_sensor", side_effect=broken):
                svc.run_curation(svc.engine.rep.efficacy_report())

            after = json.dumps(svc.engine.rep.data, sort_keys=True)
            self.assertEqual(before, after)
            self.assertFalse(os.path.exists(svc.repertoire_path))  # нечего было сохранять
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ticker_runs_curation_only_after_real_sleep(self):
        """run_ticker вызывает курирование только когда maybe_sleep реально
        отработал (rep.ran=True), не на каждый тик и не на отложенный сон."""
        tmp = tempfile.mkdtemp(prefix="motus-curation-")
        try:
            from motus.daemon import Service
            cfg = cfg_full()
            cfg["curation"] = {"enabled": True, "api": "ollama",
                               "base_url": "http://x", "model": "m"}
            svc = Service(cfg, tmp)
            calls = []
            with unittest.mock.patch.object(
                    svc, "run_curation", side_effect=lambda eff: calls.append(eff)):
                with unittest.mock.patch.object(
                        svc.engine, "maybe_sleep",
                        return_value=SleepReport(False, "too_early", [])):
                    svc._stop = threading.Event()
                    # один проход тела run_ticker вручную, без реального ожидания таймера
                    d = svc.engine.tick()
                    svc.next_tick_s = d.next_tick_s
                    rep = svc.engine.maybe_sleep()
                    if rep.ran and svc.curator_sensor is not None:
                        svc.run_curation(rep.efficacy)
            self.assertEqual(calls, [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestConfig(unittest.TestCase):
    def test_hysteresis_required(self):
        c = cfg_full()
        c["drives"]["FEAR"]["theta_lo"] = c["drives"]["FEAR"]["theta_hi"]
        with self.assertRaises(config.ConfigError):
            config.validate(c)

    def test_non_finite_config_value_rejected(self):
        for path in (("circadian", "amp"), ("modulator", "k_ne"), ("budget", "capacity")):
            c = cfg_full()
            c[path[0]][path[1]] = float("nan")
            with self.assertRaises(config.ConfigError):
                config.validate(c)
        c = cfg_full()
        c["arousal"]["a0"] = float("inf")
        with self.assertRaises(config.ConfigError):
            config.validate(c)

    def test_zero_time_constant_rejected(self):
        for path in (("context", "tau_s"), ("boredom", "tau_s"), ("separation", "tau_s"),
                     ("modulator", "tau_s"), ("habituation", "tau_s"),
                     ("budget", "penalty_tau_s"), ("sleep", "period_s")):
            c = cfg_full()
            c[path[0]][path[1]] = 0
            with self.assertRaises(config.ConfigError):
                config.validate(c)

    def test_initiation_enabled_must_be_bool(self):
        c = cfg_full()
        c["budget"]["initiation_enabled"] = "yes"
        with self.assertRaises(config.ConfigError):
            config.validate(c)

    def test_appraisal_mode_lexical_accepted(self):
        c = cfg_full()
        c["appraisal"] = {"mode": "lexical"}
        config.validate(c)  # не должно бросить

    def test_appraisal_model_fallback_must_be_known_value(self):
        c = cfg_full()
        c["appraisal"] = {"mode": "model", "api": "ollama", "base_url": "http://x",
                          "model": "m", "model_fallback": "cloud"}
        with self.assertRaises(config.ConfigError):
            config.validate(c)

    def test_appraisal_model_fallback_default_is_lexical_in_shipped_config(self):
        c = cfg_full()
        self.assertEqual(c["appraisal"]["model_fallback"], "lexical")

    def test_curation_enabled_by_operator_in_shipped_config(self):
        # 2026-09-15: включено осознанно оператором (claude-sonnet-5 через claude -p).
        # Отказ модели при этом не ломает ночь — цикл просто пропускается.
        c = cfg_full()
        self.assertTrue(c["curation"]["enabled"])

    def test_curation_enabled_must_be_bool(self):
        c = cfg_full()
        c["curation"]["enabled"] = "yes"
        with self.assertRaises(config.ConfigError):
            config.validate(c)

    def test_curation_enabled_requires_provider_fields(self):
        c = cfg_full()
        c["curation"] = {"enabled": True}
        with self.assertRaises(config.ConfigError):
            config.validate(c)

    def test_curation_disabled_skips_provider_validation(self):
        c = cfg_full()
        c["curation"] = {"enabled": False}
        config.validate(c)  # не должно бросить — провайдер не нужен, если выключено

    def test_rage_repertoire_rejected(self):
        c = cfg_full()
        c["_repertoire"]["templates"].append(
            {"id": "x", "drive": "RAGE", "cost_tier": "local", "max_tokens": 1,
             "preconditions": [], "consummation": {}, "prompt": "", "rationale": ""}
        )
        with self.assertRaises(config.ConfigError):
            config.validate(c)

    def test_unknown_precondition_is_fail_closed(self):
        from motus.repertoire import Repertoire
        cfg = cfg_full()
        h, s, _ = mk(cfg)
        rep = Repertoire(cfg, h)
        gate = Gatekeeper(cfg, h).evaluate(s)
        self.assertFalse(rep._precondition_ok("опечатка", s, gate))


class TestJournal(unittest.TestCase):
    def test_raw_text_never_reaches_the_journal(self):
        """Сырой текст пользователя оценивает сенсор и СРАЗУ выбрасывается — в
        журнал уходит только производный appraisal (6 маленьких чисел), не
        содержание сообщения. Тот же принцип, что и у карточки."""
        secret = "не разглашай пароль от почты никому, это секретный текст"

        def stub_sensor(text):
            self.assertEqual(text, secret)
            return {"valence": -1, "threat": 0, "novelty": 1, "social_warmth": 0,
                   "loss": 0, "agency_blocked": False}

        cfg = cfg_full()
        tmp = tempfile.mkdtemp(prefix="motus-text-")
        try:
            jdir = os.path.join(tmp, "journal")
            eng = Engine(cfg, VirtualClock(T0), Journal(jdir), sensor=stub_sensor)
            eng.submit_event(Event("user_message", T0, {"text": secret}))
            recs = list(Journal(jdir).read_all())
            dumped = json.dumps(recs, ensure_ascii=False)
            self.assertNotIn(secret, dumped)
            self.assertNotIn("не разглашай", dumped)
            ev = next(r for r in recs if r["kind"] == "event")
            self.assertNotIn("text", ev["payload"]["payload"])
            self.assertEqual(ev["payload"]["payload"]["appraisal"]["valence"], -1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_text_dropped_even_when_appraisal_already_given(self):
        """Правило безусловное: если вызывающий по ошибке шлёт и text, и готовый
        appraisal одновременно, text всё равно не должен доехать до журнала."""
        secret = "секретный текст, который не должен попасть в журнал"
        cfg = cfg_full()
        tmp = tempfile.mkdtemp(prefix="motus-text2-")
        try:
            jdir = os.path.join(tmp, "journal")
            eng = Engine(cfg, VirtualClock(T0), Journal(jdir))  # без sensor вовсе
            eng.submit_event(Event("user_message", T0, {
                "text": secret,
                "appraisal": {"valence": 2, "threat": 0, "novelty": 0,
                             "social_warmth": 0, "loss": 0, "agency_blocked": False},
            }))
            recs = list(Journal(jdir).read_all())
            self.assertNotIn(secret, json.dumps(recs, ensure_ascii=False))
            ev = next(r for r in recs if r["kind"] == "event")
            self.assertNotIn("text", ev["payload"]["payload"])
            # appraisal, присланный вызывающим, использован как есть, не затёрт
            # пустым сенсором (sensor=None => appraise_text вернул бы нули).
            self.assertEqual(ev["payload"]["payload"]["appraisal"]["valence"], 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_false_appraisal_invalid_when_sensor_disabled(self):
        """L-1 выключен по умолчанию — это штатно, не сбой. Раньше здесь на
        каждое сообщение летела ложная запись appraisal_invalid."""
        cfg = cfg_full()
        tmp = tempfile.mkdtemp(prefix="motus-noinvalid-")
        try:
            jdir = os.path.join(tmp, "journal")
            eng = Engine(cfg, VirtualClock(T0), Journal(jdir))  # sensor=None
            eng.submit_event(Event("user_message", T0, {"text": "привет, как дела?"}))
            kinds = [r["kind"] for r in Journal(jdir).read_all()]
            self.assertNotIn("appraisal_invalid", kinds)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_appraisal_invalid_logged_when_sensor_actually_fails(self):
        """А если сенсор настроен и реально отказал — запись должна появиться."""
        def broken(text):
            raise TimeoutError("stub")

        cfg = cfg_full()
        tmp = tempfile.mkdtemp(prefix="motus-invalid-")
        try:
            jdir = os.path.join(tmp, "journal")
            eng = Engine(cfg, VirtualClock(T0), Journal(jdir), sensor=broken)
            eng.submit_event(Event("user_message", T0, {"text": "привет, как дела?"}))
            kinds = [r["kind"] for r in Journal(jdir).read_all()]
            self.assertIn("appraisal_invalid", kinds)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_log_contains_required_fields(self):
        """Состав журнала по требованию: время, событие, вектор, гейт, карточка,
        вызов LLM. Если что-то из этого перестанет писаться — тест упадёт."""
        cfg = cfg_full()
        tmp = tempfile.mkdtemp(prefix="motus-log-")
        try:
            jdir = os.path.join(tmp, "journal")
            ck = VirtualClock(T0)
            eng = Engine(cfg, ck, Journal(jdir))
            for i in range(200):
                ck.advance(600)
                if i == 3:
                    eng.submit_event(Event("user_message", ck.now(),
                                           {"appraisal": {"novelty": 2}}))
                d = eng.tick(ck.now())
                if d.task:
                    eng.note_llm_call("ollama/qwen2.5:3b", "task", 300, 120,
                                      d.task.template_id)
                    eng.consummate(d.task.template_id, True, 420.0)
            recs = list(Journal(jdir).read_all())
            kinds = {r["kind"] for r in recs}
            for required in ("boot", "tick", "event", "impulse", "gate_change",
                             "card", "llm_call", "task", "consummation"):
                self.assertIn(required, kinds, f"в журнале нет записей вида {required}")

            tick = next(r for r in recs if r["kind"] == "tick")
            self.assertIn("state", tick)                     # вектор
            self.assertIn("drives", tick["state"])
            self.assertIn("gate", tick["payload"])           # гейт
            self.assertIn("may_initiate", tick["payload"]["gate"])
            self.assertIsInstance(tick["t"], float)          # время

            card = next(r for r in recs if r["kind"] == "card")
            self.assertTrue(card["payload"]["text"])         # карточка

            call = next(r for r in recs if r["kind"] == "llm_call")
            self.assertEqual(call["payload"]["model"], "ollama/qwen2.5:3b")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_append_only(self):
        """Журнал только дописывается: ранее записанные строки неизменны."""
        cfg = cfg_full()
        tmp = tempfile.mkdtemp(prefix="motus-ap-")
        try:
            jdir = os.path.join(tmp, "journal")
            ck = VirtualClock(T0)
            eng = Engine(cfg, ck, Journal(jdir))
            for _ in range(20):
                ck.advance(300)
                eng.tick(ck.now())
            first = [json.dumps(r, sort_keys=True) for r in Journal(jdir).read_all()]
            for _ in range(20):
                ck.advance(300)
                eng.tick(ck.now())
            second = [json.dumps(r, sort_keys=True) for r in Journal(jdir).read_all()]
            self.assertEqual(first, second[:len(first)])
            self.assertGreater(len(second), len(first))
            seqs = [r["seq"] for r in Journal(jdir).read_all()]
            self.assertEqual(seqs, sorted(seqs))
            self.assertEqual(len(seqs), len(set(seqs)))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestReplay(unittest.TestCase):
    def test_repertoire_state_is_not_shared(self):
        """Два движка на одном конфиге не должны делить efficacy: это изменяемое
        состояние, и его утечка ломает детерминизм реплея."""
        cfg = cfg_full()
        a = Engine(cfg, VirtualClock(T0), NullJournal())
        b = Engine(cfg, VirtualClock(T0), NullJournal())
        a.rep.record("follow_curiosity", -0.4, 100.0, True)
        ea = next(t for t in a.rep.data["templates"] if t["id"] == "follow_curiosity")
        eb = next(t for t in b.rep.data["templates"] if t["id"] == "follow_curiosity")
        ec = next(t for t in cfg["_repertoire"]["templates"] if t["id"] == "follow_curiosity")
        self.assertEqual(ea["efficacy"]["n"], 1)
        self.assertEqual(eb["efficacy"]["n"], 0)
        self.assertEqual(ec["efficacy"]["n"], 0)

    def test_determinism(self):
        cfg = cfg_full()
        tmp = tempfile.mkdtemp(prefix="motus-test-")
        try:
            ck = VirtualClock(T0)
            eng = Engine(cfg, ck, Journal(os.path.join(tmp, "journal")))
            rnd = random.Random(1)
            for i in range(800):
                ck.advance(rnd.uniform(60, 900))
                if i % 25 == 0:
                    eng.submit_event(Event("user_message", ck.now(),
                                           {"appraisal": {"novelty": 2, "social_warmth": 1}}))
                if i % 37 == 0:
                    eng.submit_event(Event("tool_error", ck.now(),
                                           {"tool": "net", "blocking": True}))
                if i % 53 == 0:
                    eng.submit_event(Event("sensor", ck.now(),
                                           {"temp_c": 79, "throttled": True}))
                d = eng.tick(ck.now())
                if d.task:
                    eng.note_llm_call("ollama/qwen2.5:3b", "task", 200, 90,
                                      d.task.template_id)
                    eng.consummate(d.task.template_id, True, 120.0)
            res = replay.replay_journal(cfg, Journal(os.path.join(tmp, "journal")))
            self.assertTrue(
                res.deterministic,
                "реплей разошёлся: " + "; ".join(
                    f"{d.field}@{d.seq}: {d.expected}!={d.got}" for d in res.divergences[:5]
                ),
            )
            self.assertGreater(res.ticks, 100)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestReplayAcrossRestart(unittest.TestCase):
    """Реплей воспроизводит живой демон целиком, включая рестарт со state.json и
    всё, что раньше меняло состояние мимо журнала: сон, refund, протухание задач
    при опросе очереди, outcome консумации, подмену репертуара снапшотом."""

    def _drive(self, svc, t, rnd, steps, kinds):
        eng = svc.engine
        for i in range(steps):
            t += rnd.uniform(60, 900)
            if i % 60 == 0:
                eng.submit_event(Event("user_message", t,
                                       {"appraisal": {"novelty": 2, "social_warmth": 1}}))
            if i % 31 == 0:
                eng.submit_event(Event("tool_error", t, {"tool": "net", "blocking": True}))
            d = eng.tick(t)
            if d.tier == 2:
                kinds.add(len(kinds))
                if len(kinds) % 2:
                    eng.refund_initiation()
            if d.task and i % 3:
                eng.consummate(d.task.template_id, i % 5 != 0, 50.0,
                               outcome="confirmed" if i % 7 == 0 else None)
            if d.task and i % 3 == 0:
                # задача осталась в очереди — дать ей протухнуть и опросить очередь
                t += eng.rep.TASK_TTL_S + 1
                eng.submit_event(Event("net_up", t, {}))
                eng.peek_task()
            if i % 97 == 0:
                eng.maybe_sleep(force=True)
            svc.save_state()
        return t

    def test_replay_is_exact_across_restart(self):
        from motus.daemon import Service
        cfg = cfg_full()
        cfg["appraisal"] = {"mode": "lexical"}
        # Низкие пороги — чтобы за 800 тиков реально были и задачи, и инициации.
        cfg["heartbeat"].update(theta_task=0.5, theta_act=0.55, task_min_interval_s=120)
        cfg["budget"].update(refractory_s=300)
        cfg["budget"]["quiet_hours"]["enabled"] = False
        config.validate(cfg)
        tmp = tempfile.mkdtemp(prefix="motus-restart-")
        try:
            rnd = random.Random(7)
            kinds = set()
            svc1 = Service(cfg, tmp)
            t = self._drive(svc1, svc1.engine.state.t, rnd, 400, kinds)

            # «Прошлая ночь» оставила эволюционировавший репертуар на диске.
            data = copy.deepcopy(svc1.engine.rep.data)
            for tpl in data["templates"]:
                tpl["efficacy"] = {"n": 9, "mean_delta": rnd.uniform(0, 0.5),
                                   "success_rate": 0.5, "mean_cost": 10.0}
            with open(os.path.join(tmp, "repertoire.json"), "w", encoding="utf-8") as fh:
                json.dump(data, fh)

            svc2 = Service(cfg, tmp)  # рестарт: state.json + repertoire.json
            self.assertNotEqual(svc2.engine.state.drives,
                                State.initial(cfg, svc2.engine.state.t).drives)
            self._drive(svc2, t, rnd, 400, kinds)

            recs = list(svc2.journal.read_all())
            got = {r["kind"] for r in recs}
            for k in ("boot", "sleep", "refund", "task_expired", "repertoire", "consummation"):
                self.assertIn(k, got)
            self.assertEqual(sum(r["kind"] == "boot" for r in recs), 2)

            res = replay.replay(cfg, recs)
            self.assertEqual(res.legacy_boots, [])
            self.assertTrue(
                res.deterministic,
                "реплей разошёлся: " + "; ".join(
                    f"{d.field}@{d.seq}: {d.expected}!={d.got}" for d in res.divergences[:5]))
            self.assertGreater(res.ticks, 700)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_replay_carries_validation_outcome(self):
        cfg = cfg_full()
        tmp = tempfile.mkdtemp(prefix="motus-outcome-")
        try:
            st = State.initial(cfg, T0)
            st.drives["FEAR"] = 0.9
            vid = "verify_state:1.0"
            st.pending_validations[vid] = {
                "template_id": "verify_state", "drive": "FEAR", "rule": "x",
                "due_at": T0 - 1.0, "created_at": T0 - 2000.0,
            }
            ck = VirtualClock(T0)
            eng = Engine(cfg, ck, Journal(os.path.join(tmp, "journal")), st)
            ck.advance(60)
            d = eng.tick(ck.now())
            self.assertIsNotNone(d.task)
            self.assertEqual(d.task.validation_id, vid)
            eng.consummate(d.task.template_id, True, 5.0, outcome="invalidated")
            for _ in range(5):
                ck.advance(120)
                eng.tick(ck.now())
            res = replay.replay_journal(cfg, Journal(os.path.join(tmp, "journal")))
            self.assertTrue(res.deterministic, [
                f"{d.field}@{d.seq}: {d.expected}!={d.got}" for d in res.divergences[:5]])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_legacy_boot_is_reported(self):
        cfg = cfg_full()
        recs = [{"seq": 1, "t": T0, "kind": "boot", "payload": {"tz_offset_s": 0.0}},
                {"seq": 2, "t": T0 + 60, "kind": "tick", "payload": {}}]
        self.assertEqual(replay.replay(cfg, recs).legacy_boots, [1])


class TestListenerSplit(unittest.TestCase):
    """Публичный слушатель отдаёт openclaw только PUBLIC_PATHS и карточку без
    единого числа; всё остальное — только на админ-слушателе."""

    def _handle(self, path, public, method="GET", body=None):
        from motus.daemon import Handler, Service, PUBLIC_PATHS  # noqa
        tmp = tempfile.mkdtemp(prefix="motus-lsplit-")
        try:
            Handler.service = Service(cfg_full(), tmp)
            h = Handler.__new__(Handler)
            h.server = type("S", (), {"public": public})()
            h.path = path
            sent = {}
            h._send = lambda code, obj: sent.update(code=code, obj=obj)
            h._body = lambda: (body or {})
            (h.do_GET if method == "GET" else h.do_POST)()
            return sent
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_public_blocks_raw_journal_tick_sleep(self):
        for path, m in [("/state/raw", "GET"), ("/journal/tail", "GET"),
                        ("/tick", "POST"), ("/sleep", "POST")]:
            self.assertEqual(self._handle(path, public=True, method=m)["code"], 404, path)

    def test_public_allows_card_and_event(self):
        self.assertEqual(self._handle("/state/card", public=True)["code"], 200)
        self.assertEqual(self._handle("/event", public=True, method="POST",
                                      body={"kind": "user_message", "payload": {}})["code"], 200)

    def test_public_card_carries_no_numbers(self):
        gate = self._handle("/state/card", public=True)["obj"]["gate"]
        self.assertNotIn("activation", gate)
        self.assertNotIn("somatic_flags", gate)
        self.assertIn("may_initiate", gate)         # маску отдаём
        self.assertIn("allowed_tools", gate)

    def test_admin_serves_everything(self):
        self.assertEqual(self._handle("/state/raw", public=False)["code"], 200)
        self.assertIn("activation", self._handle("/state/card", public=False)["obj"]["gate"])

    def test_public_allows_initiate_pending(self):
        self.assertEqual(self._handle("/initiate/pending", public=True)["code"], 200)


class TestEventDoesNotBlockCard(unittest.TestCase):
    """/event с медленной оценкой текста (L-1 по сети) не должен держать
    svc.lock всё это время — иначе /state/card (и всё остальное) ждёт вместе
    с ним, упирается в клиентский timeout и получает BrokenPipeError, пока
    сервер ещё сидит в сетевом вызове. Баг найден на живом деплое 2026-09-12."""

    def _post_event(self, svc, text):
        from motus.daemon import Handler
        Handler.service = svc
        h = Handler.__new__(Handler)
        h.server = type("S", (), {"public": False})()
        h.path = "/event"
        h._body = lambda: {"kind": "user_message", "payload": {"text": text}}
        sent = {}
        h._send = lambda code, obj: sent.update(code=code, obj=obj)
        h.do_POST()
        return sent

    def _get_card(self, svc):
        from motus.daemon import Handler
        Handler.service = svc
        h = Handler.__new__(Handler)
        h.server = type("S", (), {"public": False})()
        h.path = "/state/card"
        sent = {}
        h._send = lambda code, obj: sent.update(code=code, obj=obj)
        h.do_GET()
        return sent

    def test_state_card_not_blocked_by_slow_appraisal(self):
        from motus.appraisal import Appraiser
        from motus.daemon import Service
        tmp = tempfile.mkdtemp(prefix="motus-noblock-")
        try:
            svc = Service(cfg_full(), tmp)

            def slow_sensor(_text):
                time.sleep(0.3)
                return {"valence": 0, "threat": 0, "novelty": 0,
                       "social_warmth": 0, "loss": 0, "agency_blocked": 0}

            svc.engine.ap = Appraiser(sensor=slow_sensor, mode="model")

            th = threading.Thread(target=self._post_event, args=(svc, "привет"))
            th.start()
            time.sleep(0.05)  # дать /event зайти в сенсор и не держать svc.lock
            t0 = time.monotonic()
            out = self._get_card(svc)
            elapsed = time.monotonic() - t0
            th.join(timeout=2)

            self.assertEqual(out["code"], 200)
            self.assertLess(elapsed, 0.2,
                            "/state/card ждал наравне с медленным /event — лок не разделён")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_slow_appraisal_still_applies_and_journals_on_failure(self):
        """Оценка текста вне лока — не значит «мимо журнала»: appraisal_invalid
        при реальном отказе модели по-прежнему пишется, seq не портится."""
        from motus.appraisal import Appraiser
        from motus.daemon import Service
        tmp = tempfile.mkdtemp(prefix="motus-noblock2-")
        try:
            svc = Service(cfg_full(), tmp)

            def failing_sensor(_text):
                raise TimeoutError("боевой сенсор недоступен")

            svc.engine.ap = Appraiser(sensor=failing_sensor, mode="model")
            out = self._post_event(svc, "текст, который не должен попасть в журнал")
            self.assertEqual(out["code"], 200)

            kinds = [r["kind"] for r in svc.journal.read_all()]
            self.assertIn("appraisal_invalid", kinds)
            self.assertIn("event", kinds)
            seqs = [r["seq"] for r in svc.journal.read_all()]
            self.assertEqual(len(seqs), len(set(seqs)), "seq не должны дублироваться")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestTaskNextReadsQueue(unittest.TestCase):
    """GET /task/next раньше вызывал engine.tick() и возвращал только то, что
    РЕШИЛОСЬ ИМЕННО В ЭТОТ ТИК — а задачи чаще кладёт в очередь независимый
    фоновый цикл (run_ticker) между опросами исполнителя (Tier 1), и они там
    просто протухали по TTL, ни разу не будучи прочитаны. Регрессия на баг,
    найденный на живом деплое 2026-09-12."""

    def _get_task_next(self, svc):
        from motus.daemon import Handler
        Handler.service = svc
        h = Handler.__new__(Handler)
        h.server = type("S", (), {"public": False})()
        h.path = "/task/next"
        sent = {}
        h._send = lambda code, obj: sent.update(code=code, obj=obj)
        h.do_GET()
        return sent["obj"]

    def test_returns_task_queued_by_a_previous_tick(self):
        from motus.daemon import Service
        from motus.repertoire import Task
        tmp = tempfile.mkdtemp(prefix="motus-tnext-")
        try:
            svc = Service(cfg_full(), tmp)
            st = svc.engine.state
            # Симулируем то, что реально происходило: фоновый тик уже положил
            # задачу в очередь и включил рефрактерность — сам он новую не
            # выдаст, пока не пройдёт task_min_interval_s.
            queued = Task(
                template_id="follow_curiosity", drive="SEEKING", prompt="p",
                cost_tier="local", max_tokens=600, consummation={"type": "reflection"},
                allowed_tools=("read", "memory"), issued_t=st.t,
                expires_t=st.t + 3600.0, drive_at_issue=0.6,
            )
            svc.engine.rep.queue.append(queued)
            st.last_task_t = st.t  # рефрактерность только что включилась

            out = self._get_task_next(svc)
            self.assertIsNotNone(out["task"], "задача из очереди должна была вернуться")
            self.assertEqual(out["task"]["template_id"], "follow_curiosity")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_does_not_hand_out_expired_queued_task(self):
        from motus.daemon import Service
        from motus.repertoire import Task
        tmp = tempfile.mkdtemp(prefix="motus-tnext-")
        try:
            svc = Service(cfg_full(), tmp)
            st = svc.engine.state
            expired = Task(
                template_id="follow_curiosity", drive="SEEKING", prompt="p",
                cost_tier="local", max_tokens=600, consummation={"type": "reflection"},
                allowed_tools=("read", "memory"), issued_t=st.t - 7200.0,
                expires_t=st.t - 3600.0, drive_at_issue=0.6,
            )
            svc.engine.rep.queue.append(expired)

            out = self._get_task_next(svc)
            self.assertIsNone(out["task"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestInitiatePending(unittest.TestCase):
    """GET /initiate/pending (Tier 2): только чтение, с защитой от тишины
    короче INITIATE_MIN_SILENCE_S."""

    def _svc(self, tmp):
        from motus.daemon import Service
        return Service(cfg_full(), tmp)

    def _get(self, svc, path="/initiate/pending"):
        from motus.daemon import Handler
        Handler.service = svc
        h = Handler.__new__(Handler)
        h.server = type("S", (), {"public": True})()
        h.path = path
        sent = {}
        h._send = lambda code, obj: sent.update(code=code, obj=obj)
        h.do_GET()
        return sent["obj"]

    def test_nothing_pending_by_default(self):
        tmp = tempfile.mkdtemp(prefix="motus-ip-")
        try:
            svc = self._svc(tmp)
            self.assertEqual(self._get(svc), {"pending": False})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_pending_hidden_during_recent_contact(self):
        """initiation_pending=True, но контакт был только что — не отдаём
        pending: это тик, вызванный обычным /state/card посреди разговора,
        не настоящая проактивная инициация в тишину."""
        tmp = tempfile.mkdtemp(prefix="motus-ip-")
        try:
            from motus import daemon as daemon_mod
            svc = self._svc(tmp)
            st = svc.engine.state
            st.initiation_pending = True
            st.last_contact_t = svc.clock.now() - (daemon_mod.INITIATE_MIN_SILENCE_S / 2)
            self.assertEqual(self._get(svc), {"pending": False})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_pending_true_after_real_silence(self):
        tmp = tempfile.mkdtemp(prefix="motus-ip-")
        try:
            from motus import daemon as daemon_mod
            svc = self._svc(tmp)
            st = svc.engine.state
            st.initiation_pending = True
            st.regime = "PANIC"
            st.last_contact_t = svc.clock.now() - (daemon_mod.INITIATE_MIN_SILENCE_S * 3)
            out = self._get(svc)
            self.assertTrue(out["pending"])
            self.assertIn("card", out)
            self.assertIn("text", out["card"])
            self.assertEqual(out["gate"]["regime"], "PANIC")
            self.assertIn("max_tokens", out["gate"])
            self.assertIn("forbidden", out["gate"])
            self.assertNotIn("activation", out["gate"])  # число — не наружу
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_readonly_does_not_advance_or_spend(self):
        """/initiate/pending не должен тикать и не должен трогать бюджет —
        повторные опросы не должны сами по себе ничего списывать."""
        tmp = tempfile.mkdtemp(prefix="motus-ip-")
        try:
            svc = self._svc(tmp)
            st = svc.engine.state
            tokens_before = st.tokens
            t_before = st.t
            for _ in range(5):
                self._get(svc)
            self.assertEqual(st.tokens, tokens_before)
            self.assertEqual(st.t, t_before)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestClaudeLimitSensor(unittest.TestCase):
    """Соматика лимита Claude: рост -> дискретный FEAR, сброс -> прощение
    РОВНО накопленного (не всего FEAR), пороги в gates.py."""

    def _sensor(self, eng, pct, t, **extra):
        return eng.submit_event(Event("sensor", t, {"claude_limit_pct": pct, **extra}))

    def test_rising_pct_adds_discrete_fear_and_tracks_it(self):
        cfg = cfg_full()
        eng = Engine(cfg, VirtualClock(T0), NullJournal())
        fear_before = eng.state.drives["FEAR"]
        self._sensor(eng, 40, T0)
        self._sensor(eng, 55, T0 + 600, claude_limit_delta=15)
        self.assertGreater(eng.state.drives["FEAR"], fear_before)
        self.assertAlmostEqual(eng.state.limit_fear_added, eng.state.drives["FEAR"] - fear_before,
                               places=6)
        self.assertAlmostEqual(eng.state.somatic["limit"], 0.55, places=6)

    def test_no_delta_field_means_no_impulse(self):
        cfg = cfg_full()
        eng = Engine(cfg, VirtualClock(T0), NullJournal())
        fear_before = eng.state.drives["FEAR"]
        self._sensor(eng, 80, T0)  # первый опрос — просто уровень, без claude_limit_delta
        self.assertEqual(eng.state.drives["FEAR"], fear_before)
        self.assertEqual(eng.state.limit_fear_added, 0.0)

    def test_reset_forgives_exactly_tracked_amount_not_all_fear(self):
        cfg = cfg_full()
        eng = Engine(cfg, VirtualClock(T0), NullJournal())
        # FEAR из другого источника (integrity), должен пережить сброс лимита.
        eng.submit_event(Event("sensor", T0, {"integrity_drop": True}))
        fear_from_integrity = eng.state.drives["FEAR"]
        self.assertGreater(fear_from_integrity, 0.0)

        self._sensor(eng, 90, T0 + 60, claude_limit_delta=90)
        added = eng.state.limit_fear_added
        self.assertGreater(added, 0.0)
        fear_at_peak = eng.state.drives["FEAR"]

        # Тот же момент времени, что и предыдущий тик (dt=0) — естественная
        # релаксация драйва между тиками иначе маскирует проверку "прощено
        # ровно added", это отдельно покрыто test_reset_never_drives_fear_negative.
        self._sensor(eng, 0, T0 + 60, claude_limit_reset=True)
        self.assertEqual(eng.state.limit_fear_added, 0.0)
        self.assertAlmostEqual(eng.state.drives["FEAR"], fear_at_peak - added, places=6)
        # Прощён только вклад лимита, не весь FEAR — что-то от integrity_drop
        # обязано остаться (не улетело в 0 вместе с лимитным вкладом).
        self.assertGreater(eng.state.drives["FEAR"], 0.0)

    def test_reset_never_drives_fear_negative(self):
        cfg = cfg_full()
        eng = Engine(cfg, VirtualClock(T0), NullJournal())
        self._sensor(eng, 96, T0, claude_limit_delta=96)
        # Драйв мог естественно затухнуть до сброса — прощение не должно уйти в минус.
        eng.state.drives["FEAR"] = 0.0
        self._sensor(eng, 0, T0 + 100, claude_limit_reset=True)
        self.assertGreaterEqual(eng.state.drives["FEAR"], 0.0)
        self.assertEqual(eng.state.limit_fear_added, 0.0)

    def test_reset_at_is_recorded_for_public_message(self):
        cfg = cfg_full()
        eng = Engine(cfg, VirtualClock(T0), NullJournal())
        self._sensor(eng, 50, T0, claude_limit_reset_at=T0 + 1200)
        self.assertEqual(eng.state.claude_limit_reset_at, T0 + 1200)


class TestClaudeLimitGate(unittest.TestCase):
    """Пороги gates.py: мягкое урезание токенов между SOFT/HARD, флаги."""

    def _gate_for_pct(self, pct):
        cfg = cfg_full()
        eng = Engine(cfg, VirtualClock(T0), NullJournal())
        eng.state.somatic["limit"] = pct
        return eng.gk.evaluate(eng.state)

    def test_below_soft_untouched(self):
        gate = self._gate_for_pct(0.5)
        self.assertNotIn("limit_high", gate.somatic_flags)
        self.assertNotIn("limit_exhausted", gate.somatic_flags)

    def test_between_soft_and_hard_shrinks_progressively(self):
        gate_low = self._gate_for_pct(0.75)
        gate_high = self._gate_for_pct(0.9)
        self.assertIn("limit_high", gate_low.somatic_flags)
        self.assertIn("limit_high", gate_high.somatic_flags)
        self.assertLess(gate_high.max_tokens, gate_low.max_tokens)
        self.assertLessEqual(gate_low.max_tokens, 350)

    def test_at_hard_threshold_flagged_exhausted(self):
        gate = self._gate_for_pct(0.97)
        self.assertIn("limit_exhausted", gate.somatic_flags)
        self.assertNotIn("limit_high", gate.somatic_flags)


class TestClaudeLimitPublicBlock(unittest.TestCase):
    """daemon._limit_block: сообщение считается кодом, не моделью."""

    def test_inactive_below_hard_threshold(self):
        from motus.daemon import _limit_block
        st = State.initial(cfg_full(), T0)
        st.somatic["limit"] = 0.8
        out = _limit_block(st, T0)
        self.assertFalse(out["active"])
        self.assertIsNone(out["message"])

    def test_active_with_known_reset_gives_minutes(self):
        from motus.daemon import _limit_block
        st = State.initial(cfg_full(), T0)
        st.somatic["limit"] = 0.97
        st.claude_limit_reset_at = T0 + 600  # через 10 минут
        out = _limit_block(st, T0)
        self.assertTrue(out["active"])
        self.assertIn("10 мин", out["message"])
        self.assertEqual(out["resume_at"], T0 + 600)

    def test_active_without_known_reset_still_gives_message(self):
        from motus.daemon import _limit_block
        st = State.initial(cfg_full(), T0)
        st.somatic["limit"] = 0.99
        out = _limit_block(st, T0)
        self.assertTrue(out["active"])
        self.assertIsNotNone(out["message"])
        self.assertIsNone(out["resume_at"])


class TestNonFiniteHardening(unittest.TestCase):
    def test_state_from_dict_rejects_nan_snapshot(self):
        cfg = cfg_full()
        good = State.initial(cfg, T0).to_dict()
        self.assertIsInstance(State.from_dict(good), State)
        bad = copy.deepcopy(good)
        bad["drives"]["FEAR"] = float("nan")
        with self.assertRaises(ValueError):
            State.from_dict(bad)
        bad2 = copy.deepcopy(good)
        bad2["tokens"] = float("inf")
        with self.assertRaises(ValueError):
            State.from_dict(bad2)

    def test_state_from_dict_ignores_unknown_keys(self):
        cfg = cfg_full()
        d = State.initial(cfg, T0).to_dict()
        d["_future_field"] = 123
        self.assertIsInstance(State.from_dict(d), State)

    def test_daemon_discards_corrupt_snapshot_loudly(self):
        from motus.daemon import Service
        tmp = tempfile.mkdtemp(prefix="motus-corrupt-")
        try:
            with open(os.path.join(tmp, "state.json"), "w", encoding="utf-8") as fh:
                fh.write('{"t": 1.0, "drives": {"FEAR": NaN}}')
            svc = Service(cfg_full(), tmp)
            kinds = [r["kind"] for r in Journal(os.path.join(tmp, "journal")).read_all()]
            self.assertIn("error", kinds)
            self.assertTrue(os.path.exists(os.path.join(tmp, "state.json.corrupt")))
            self.assertTrue(svc.engine.state.has_finite_vector())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_engine_tick_self_heals_non_finite_vector(self):
        cfg = cfg_full()
        eng = Engine(cfg, VirtualClock(T0), NullJournal())
        eng.state.drives["FEAR"] = float("nan")
        eng.state.modulators["ne"] = float("inf")
        d = eng.tick(T0 + 60)
        self.assertTrue(eng.state.has_finite_vector())
        self.assertIn(d.gate.regime, ("baseline", *cfg["drives"]))


class TestTier1Executor(unittest.TestCase):
    """Исполнитель фоновых задач: deploy/tier1_executor.py."""

    def _args(self, tmp):
        ns = argparse.Namespace(
            motusd="http://x", state_dir=pathlib.Path(tmp),
            workspace=pathlib.Path(tmp), openclaw="/bin/false",
            oc_args="--isolated", model=None, task_timeout=5, dry_run=False,
        )
        return ns

    def test_no_task_is_noop(self):
        tmp = tempfile.mkdtemp(prefix="motus-t1-")
        try:
            calls = []
            with unittest.mock.patch.object(tier1_executor, "_get",
                                            return_value={"task": None}):
                with unittest.mock.patch.object(tier1_executor, "_post",
                                                side_effect=lambda *a, **k: calls.append(a)):
                    rc = tier1_executor._run(self._args(tmp))
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_refuses_task_carrying_outbound(self):
        tmp = tempfile.mkdtemp(prefix="motus-t1-")
        try:
            task = {"template_id": "x", "drive": "PANIC", "prompt": "p",
                    "allowed_tools": ["read", "outbound"], "consummation": {},
                    "max_tokens": 100, "issued_t": 1.0}
            with unittest.mock.patch.object(tier1_executor, "_get",
                                            return_value={"task": task}):
                with unittest.mock.patch.object(tier1_executor, "run_openclaw") as ro:
                    with unittest.mock.patch.object(tier1_executor, "_post") as po:
                        rc = tier1_executor._run(self._args(tmp))
            self.assertEqual(rc, 1)
            ro.assert_not_called()
            po.assert_not_called()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_prompt_has_no_outbound_preamble(self):
        p = tier1_executor.PREAMBLE_WITH_DROP.format(tools="read, memory", drop="/d",
                                                      max_tokens=200, prompt="сделай X")
        self.assertIn("НЕ отправляй ничего наружу", p)
        self.assertIn("сделай X", p)
        self.assertIn("/d", p)

    def test_reflection_preamble_has_no_outbound_and_no_drop_placeholder(self):
        p = tier1_executor.PREAMBLE_REFLECTION.format(tools="read", max_tokens=200,
                                                       prompt="побудь так")
        self.assertIn("НЕ отправляй ничего наружу", p)
        self.assertIn("не обязана", p)
        self.assertIn("побудь так", p)

    def test_verify_requires_fresh_nonempty_result(self):
        tmp = tempfile.mkdtemp(prefix="motus-t1-")
        try:
            drop = pathlib.Path(tmp) / "r"
            now = time.time()
            # хода не было / ошибка
            self.assertFalse(tier1_executor.verify({}, drop, now, ok=False)[0])
            # файла нет
            self.assertFalse(tier1_executor.verify({}, drop, now, ok=True)[0])
            # пустой
            drop.write_text("", encoding="utf-8")
            self.assertFalse(tier1_executor.verify({}, drop, now, ok=True)[0])
            # старый (до старта хода)
            drop.write_text("готово, записка собрана", encoding="utf-8")
            os.utime(drop, (now - 100, now - 100))
            self.assertFalse(tier1_executor.verify({}, drop, now, ok=True)[0])
            # свежий и непустой
            os.utime(drop, (now + 1, now + 1))
            ok, why = tier1_executor.verify({"type": "artifact_created"}, drop, now, ok=True)
            self.assertTrue(ok)
            self.assertEqual(why, "artifact_created")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_verify_reflection_needs_no_file_at_all(self):
        now = time.time()
        # ok=True и drop=None (акт ничего не написал) — всё равно verified
        ok, why = tier1_executor.verify({"type": "reflection"}, None, now, ok=True)
        self.assertTrue(ok)
        self.assertEqual(why, "reflection")
        # отказ хода всё равно проваливает верификацию — reflection не значит "всё сойдёт"
        ok, why = tier1_executor.verify({"type": "reflection"}, None, now, ok=False)
        self.assertFalse(ok)
        self.assertEqual(why, "openclaw_error")

    def test_full_cycle_reports_verified_consummation(self):
        tmp = tempfile.mkdtemp(prefix="motus-t1-")
        try:
            task = {"template_id": "prepare_reentry", "drive": "PANIC",
                    "prompt": "собери записку", "allowed_tools": ["read", "memory"],
                    "consummation": {"type": "artifact_created"}, "max_tokens": 500,
                    "issued_t": 111.0}
            posted = []

            def fake_run(openclaw, oc_args, cwd, message_file, timeout_s, model):
                # «openclaw» пишет файл-результат, как велит преамбула
                drop = pathlib.Path(tmp) / ".motus" / "drops" / "prepare_reentry-111"
                drop.write_text("над чем работали: ...", encoding="utf-8")
                return True, {"usage": {"input": 900, "output": 120}}, "{}"

            with unittest.mock.patch.object(tier1_executor, "_get",
                                            return_value={"task": task}):
                with unittest.mock.patch.object(tier1_executor, "run_openclaw",
                                                side_effect=fake_run):
                    with unittest.mock.patch.object(
                            tier1_executor, "_post",
                            side_effect=lambda url, body, **k: posted.append((url, body)) or {}):
                        rc = tier1_executor._run(self._args(tmp))

            self.assertEqual(rc, 0)
            cons = [b for u, b in posted if u.endswith("/consummation")]
            self.assertEqual(len(cons), 1)
            self.assertTrue(cons[0]["verified"])
            self.assertEqual(cons[0]["template_id"], "prepare_reentry")
            self.assertEqual(cons[0]["cost"], 1020.0)
            calls = [b for u, b in posted if u.endswith("/llm_call")]
            self.assertEqual(calls[0]["tokens_in"], 900)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_full_cycle_unverified_when_no_result_file(self):
        tmp = tempfile.mkdtemp(prefix="motus-t1-")
        try:
            task = {"template_id": "make_something", "drive": "PLAY", "prompt": "p",
                    "allowed_tools": ["read", "memory", "write"],
                    "consummation": {"type": "artifact_created"}, "max_tokens": 500,
                    "issued_t": 5.0}
            posted = []
            with unittest.mock.patch.object(tier1_executor, "_get",
                                            return_value={"task": task}):
                with unittest.mock.patch.object(
                        tier1_executor, "run_openclaw",
                        return_value=(True, {}, "")):  # ход прошёл, файла не создал
                    with unittest.mock.patch.object(
                            tier1_executor, "_post",
                            side_effect=lambda url, body, **k: posted.append((url, body)) or {}):
                        tier1_executor._run(self._args(tmp))
            cons = [b for u, b in posted if u.endswith("/consummation")][0]
            self.assertFalse(cons["verified"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_full_cycle_reflection_verified_without_any_file(self):
        """wander/follow_curiosity (consummation=reflection): verified=True без
        единого файла на диске, ни в drops/, ни где-либо ещё."""
        tmp = tempfile.mkdtemp(prefix="motus-t1-")
        try:
            task = {"template_id": "wander", "drive": "PLAY", "prompt": "побудь так",
                    "allowed_tools": ["read"], "consummation": {"type": "reflection"},
                    "max_tokens": 300, "issued_t": 7.0}
            posted = []
            captured_prompt = {}

            def fake_run(openclaw, oc_args, cwd, message_file, timeout_s, model):
                captured_prompt["text"] = pathlib.Path(message_file).read_text(encoding="utf-8")
                return True, {}, "{}"  # ничего не пишет на диск — и не обязан

            with unittest.mock.patch.object(tier1_executor, "_get",
                                            return_value={"task": task}):
                with unittest.mock.patch.object(tier1_executor, "run_openclaw",
                                                side_effect=fake_run):
                    with unittest.mock.patch.object(
                            tier1_executor, "_post",
                            side_effect=lambda url, body, **k: posted.append((url, body)) or {}):
                        rc = tier1_executor._run(self._args(tmp))

            self.assertEqual(rc, 0)
            cons = [b for u, b in posted if u.endswith("/consummation")][0]
            self.assertTrue(cons["verified"])
            self.assertFalse((pathlib.Path(tmp) / ".motus" / "drops").exists())
            self.assertIn("побудь так", captured_prompt["text"])
            self.assertIn("не обязана", captured_prompt["text"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestTier1ClaudePrimary(unittest.TestCase):
    """Tier 1: Claude через claude -p основным, openclaw + локальная модель — запасным."""

    def _args(self, tmp, claude_model="claude-sonnet-5"):
        return argparse.Namespace(
            motusd="http://x", state_dir=pathlib.Path(tmp), workspace=pathlib.Path(tmp),
            openclaw="/bin/false", oc_args="--config c.json", model="ollama/ge4b-heretic:latest",
            claude="/opt/claude", claude_model=claude_model, task_timeout=5, dry_run=False,
        )

    def _task(self):
        return {"template_id": "prepare_reentry", "drive": "PANIC", "prompt": "собери записку",
                "allowed_tools": ["read", "memory", "write"],
                "consummation": {"type": "artifact_created"}, "max_tokens": 500, "issued_t": 42.0}

    def _get(self, limit_active=False):
        task = self._task()
        def fake_get(url, **k):
            if url.endswith("/task/next"):
                return {"task": task}
            return {"limit_block": {"active": limit_active}}
        return fake_get

    def _drop(self, tmp):
        d = pathlib.Path(tmp) / ".motus" / "drops" / "prepare_reentry-42"
        d.parent.mkdir(parents=True, exist_ok=True)
        return d

    def test_tools_never_include_shell_web_or_outbound(self):
        for needs_drop in (True, False):
            for gate in (["read"], ["read", "memory", "exec", "write", "net"]):
                t = tier1_executor.claude_tools(gate, needs_drop)
                self.assertTrue(set(t) <= {"Read", "Glob", "Grep", "Write", "Edit"}, t)
        self.assertNotIn("Write", tier1_executor.claude_tools(["read"], needs_drop=False))
        self.assertIn("Write", tier1_executor.claude_tools(["read"], needs_drop=True))

    def test_run_claude_command_and_stdin(self):
        with unittest.mock.patch.object(tier1_executor.subprocess, "run") as run:
            run.return_value = unittest.mock.Mock(
                returncode=0, stderr="",
                stdout=json.dumps({"subtype": "success", "is_error": False, "result": "ok",
                                   "usage": {"input_tokens": 10, "cache_read_input_tokens": 5,
                                             "output_tokens": 7}}))
            ok, env, _ = tier1_executor.run_claude("/c", "claude-sonnet-5", pathlib.Path("/w"),
                                                   "ТЕКСТ ЗАДАЧИ", "sys", ("Read", "Write"), 30)
        self.assertTrue(ok)
        self.assertEqual(env["usage"], {"input": 15, "output": 7})
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Write")
        self.assertIn("Bash", argv[argv.index("--disallowedTools") + 1])
        self.assertIn("WebFetch", argv[argv.index("--disallowedTools") + 1])
        self.assertNotIn("ТЕКСТ ЗАДАЧИ", " ".join(argv))
        self.assertEqual(run.call_args.kwargs["input"], "ТЕКСТ ЗАДАЧИ")
        self.assertEqual(run.call_args.kwargs["cwd"], "/w")

    def _cycle(self, tmp, claude_result, limit_active=False, claude_writes=True):
        posted, calls = [], {"claude": 0, "openclaw": 0}
        drop = self._drop(tmp)
        def fake_claude(*a, **k):
            calls["claude"] += 1
            if claude_result and claude_writes:
                drop.write_text("записка себе", encoding="utf-8")
            return claude_result, ({"usage": {"input": 100, "output": 20}} if claude_result
                                   else {"error": "limit"}), ""
        def fake_openclaw(*a, **k):
            calls["openclaw"] += 1
            drop.write_text("записка от gemma", encoding="utf-8")
            return True, {"usage": {"input": 50, "output": 10}}, ""
        with unittest.mock.patch.object(tier1_executor, "_get", side_effect=self._get(limit_active)), \
                unittest.mock.patch.object(tier1_executor, "run_claude", side_effect=fake_claude), \
                unittest.mock.patch.object(tier1_executor, "run_openclaw", side_effect=fake_openclaw), \
                unittest.mock.patch.object(tier1_executor, "_post",
                                           side_effect=lambda u, b, **k: posted.append((u, b)) or {}):
            rc = tier1_executor._run(self._args(tmp))
        return rc, calls, posted

    def test_claude_success_skips_fallback(self):
        tmp = tempfile.mkdtemp(prefix="motus-t1c-")
        try:
            rc, calls, posted = self._cycle(tmp, claude_result=True)
            self.assertEqual((rc, calls), (0, {"claude": 1, "openclaw": 0}))
            llm = [b for u, b in posted if u.endswith("/llm_call")][0]
            self.assertEqual(llm["model"], "claude-cli/claude-sonnet-5")
            self.assertTrue([b for u, b in posted if u.endswith("/consummation")][0]["verified"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_claude_failure_falls_back_to_openclaw(self):
        tmp = tempfile.mkdtemp(prefix="motus-t1c-")
        try:
            rc, calls, posted = self._cycle(tmp, claude_result=False)
            self.assertEqual(calls, {"claude": 1, "openclaw": 1})
            llm = [b for u, b in posted if u.endswith("/llm_call")][0]
            self.assertEqual(llm["model"], "ollama/ge4b-heretic:latest")
            self.assertTrue([b for u, b in posted if u.endswith("/consummation")][0]["verified"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_limit_block_goes_straight_to_fallback(self):
        tmp = tempfile.mkdtemp(prefix="motus-t1c-")
        try:
            rc, calls, _ = self._cycle(tmp, claude_result=True, limit_active=True)
            self.assertEqual(calls, {"claude": 0, "openclaw": 1})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_persona_prompt_reads_soul_and_caps_size(self):
        tmp = tempfile.mkdtemp(prefix="motus-t1c-")
        try:
            (pathlib.Path(tmp) / "SOUL.md").write_text("я — Грач", encoding="utf-8")
            (pathlib.Path(tmp) / "IDENTITY.md").write_text("x" * 50000, encoding="utf-8")
            sp = tier1_executor.persona_prompt(pathlib.Path(tmp))
            self.assertIn("я — Грач", sp)
            self.assertIn("оболочки", sp)
            self.assertLess(len(sp), tier1_executor.PERSONA_MAX_CHARS + 2000)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestTier2Executor(unittest.TestCase):
    """Исполнитель доставки проактива: deploy/tier2_executor.py."""

    def _args(self, tmp, session_key="agent:main:telegram:direct:1"):
        return argparse.Namespace(
            motusd="http://x", state_dir=pathlib.Path(tmp), openclaw="/bin/false",
            session_key=session_key, to=None, channel=None, timeout=5, dry_run=False,
        )

    def test_nothing_pending_is_noop(self):
        tmp = tempfile.mkdtemp(prefix="motus-t2-")
        try:
            with unittest.mock.patch.object(tier2_executor, "_get",
                                            return_value={"pending": False}):
                with unittest.mock.patch.object(tier2_executor, "run_openclaw") as ro:
                    with unittest.mock.patch.object(tier2_executor, "_post") as po:
                        rc = tier2_executor._run(self._args(tmp))
            self.assertEqual(rc, 0)
            ro.assert_not_called()
            po.assert_not_called()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_main_refuses_without_recipient(self):
        rc = tier2_executor.main(["--motusd", "http://x"])
        self.assertEqual(rc, 2)

    def test_main_dry_run_allows_missing_recipient(self):
        tmp = tempfile.mkdtemp(prefix="motus-t2-")
        try:
            with unittest.mock.patch.object(tier2_executor, "_get",
                                            return_value={"pending": False}):
                rc = tier2_executor.main(["--motusd", "http://x", "--state-dir", tmp,
                                          "--dry-run"])
            self.assertEqual(rc, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_build_command_prefers_session_key_over_to(self):
        cmd = tier2_executor.build_command("/bin/openclaw", "agent:main:x", "+1555",
                                           "telegram", pathlib.Path("/tmp/p.txt"), 60)
        self.assertIn("--session-key", cmd)
        self.assertNotIn("--to", cmd)
        cmd2 = tier2_executor.build_command("/bin/openclaw", None, "+1555",
                                            "telegram", pathlib.Path("/tmp/p.txt"), 60)
        self.assertIn("--to", cmd2)
        self.assertIn("--channel", cmd2)

    def test_delivered_ok_does_not_refund(self):
        tmp = tempfile.mkdtemp(prefix="motus-t2-")
        try:
            posted = []
            with unittest.mock.patch.object(
                    tier2_executor, "_get",
                    return_value={"pending": True, "card": {"text": "не хватает контакта"},
                                 "gate": {"regime": "PANIC"}}):
                with unittest.mock.patch.object(tier2_executor, "run_openclaw",
                                                return_value=(True, {"ok": True})):
                    with unittest.mock.patch.object(
                            tier2_executor, "_post",
                            side_effect=lambda url, body, **k: posted.append(url) or {}):
                        rc = tier2_executor._run(self._args(tmp))
            self.assertEqual(rc, 0)
            self.assertEqual(posted, [])  # /refund НЕ вызван
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_channel_failure_triggers_refund(self):
        tmp = tempfile.mkdtemp(prefix="motus-t2-")
        try:
            posted = []
            with unittest.mock.patch.object(
                    tier2_executor, "_get",
                    return_value={"pending": True, "card": {"text": "не хватает контакта"},
                                 "gate": {"regime": "PANIC"}}):
                with unittest.mock.patch.object(
                        tier2_executor, "run_openclaw",
                        return_value=(False, {"error": "channel down"})):
                    with unittest.mock.patch.object(
                            tier2_executor, "_post",
                            side_effect=lambda url, body, **k: posted.append(url) or {}):
                        rc = tier2_executor._run(self._args(tmp))
            self.assertEqual(rc, 1)
            self.assertEqual(len(posted), 1)
            self.assertTrue(posted[0].endswith("/refund"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_prompt_never_sends_card_text_verbatim_as_command(self):
        """Скрипт не должен указывать модели ЧТО именно написать словом в
        слово — только контекст и разрешение написать что-то уместное."""
        p = tier2_executor.PROMPT_TEMPLATE.format(card_text="не хватает контакта")
        self.assertIn("не хватает контакта", p)
        self.assertIn("можешь сейчас написать первой", p)




class TestConsummationRepertoireMatrix(unittest.TestCase):
    """Регрессия для находки 2026-09-14: живой прогон всех 8 шаблонов
    репертуара через реальный `openclaw agent exec` (ollama/ge4b-heretic,
    тот же исполнитель и конфиг, что у motus-tier1.service в проде) дал 3
    verified=true из 8 — и все три были типа "reflection" (без файла-следа).
    Все пять типов, которым по конструкции нужен файл (artifact_created /
    memory_entry / check_passed), провалились с no_result_file.

    Для PANIC (prepare_reentry) и FEAR (verify_state) причина СТРУКТУРНАЯ и
    ловится статически, без сети и без модели: REGIME_POLICY этих режимов
    (gates.py) не включает "write" в allowed_tools, а их единственный шаблон
    репертуара требует записанный файл для верификации — то есть "is_None"
    тут не про качество модели, а про то, что задаче физически не с чем
    доказать факт выполнения. Этот тест держит инвариант: любой шаблон с
    consummation.type != "reflection" обязан принадлежать режиму, чей
    REGIME_POLICY.allowed_tools включает "write" — иначе он никогда не сможет
    верифицироваться, сколько бы моделей под него ни подставляли.

    Для SEEKING (pull_memory_thread), CARE (check_on_the_space) и PLAY
    (make_something) причина другая — модель (ge4b-heretic:latest, локальная,
    не Sonnet) физически МОГЛА писать (write есть в allowed_tools), но
    игнорировала точный абсолютный путь из tier1_executor.PREAMBLE_WITH_DROP
    и один раз прямо заявила в ответе, что использует "более корректный
    относительный путь" вместо заданного. Это поведение конкретной модели, не
    ловится статическим тестом — см. TODO ниже.
    """

    def test_file_backed_templates_have_write_permission_in_their_regime(self):
        from motus.repertoire import KNOWN_CONSUMMATION_TYPES

        cfg = cfg_full()
        templates = cfg["_repertoire"]["templates"]
        self.assertTrue(templates, "репертуар пуст — нечего проверять")

        offenders = []
        for tpl in templates:
            ctype = tpl["consummation"].get("type")
            self.assertIn(ctype, KNOWN_CONSUMMATION_TYPES,
                          f"{tpl['id']}: неизвестный тип консумации {ctype!r}")
            if ctype == "reflection":
                continue  # верифицируется фактом хода, файл не нужен
            drive = tpl["drive"]
            policy = REGIME_POLICY.get(drive)
            if policy is None or "write" not in policy.allowed_tools:
                offenders.append(f"{tpl['id']} (drive={drive}, type={ctype})")

        self.assertEqual(
            offenders, [],
            "Эти шаблоны требуют файл-артефакт для верификации, но режим их "
            "драйва не даёт инструмент write — верификация для них "
            "структурно невозможна ни при какой модели: " + ", ".join(offenders)
        )

    def test_every_appetitive_and_aversive_drive_has_a_reflection_fallback(self):
        """Побочный вывод из того же прогона: PANIC и FEAR — единственные
        драйвы, у которых ВСЕ шаблоны требуют файл (ни одного 'reflection').
        Значит, у них нет шаблона, который в принципе может быть засчитан
        verified=true, если баг из теста выше не починен. Остальные драйвы
        подстрахованы хотя бы одним reflection-шаблоном."""
        cfg = cfg_full()
        templates = cfg["_repertoire"]["templates"]
        by_drive = {}
        for tpl in templates:
            by_drive.setdefault(tpl["drive"], []).append(tpl["consummation"].get("type"))

        drives_without_fallback = sorted(
            d for d, types in by_drive.items() if "reflection" not in types
        )
        # Текущее известное состояние (2026-09-14): FEAR и PANIC без страховки.
        # Если список изменится — это сигнал, что кто-то поправил репертуар
        # или REGIME_POLICY, и тест выше должен быть перепроверен вручную.
        self.assertEqual(
            drives_without_fallback, ["FEAR", "PANIC"],
            "Список драйвов без reflection-шаблона изменился — проверьте, "
            "чинили ли заодно write-permission баг выше, и обновите оба теста"
        )

    # TODO(живой прогон, не статический): SEEKING/CARE/PLAY технически МОГУТ
    # писать файл (write разрешён), но локальная модель (ollama/ge4b-heretic)
    # на практике игнорирует точный абсолютный путь из PREAMBLE_WITH_DROP и
    # либо выбирает свой относительный путь, либо утверждает успех без
    # факта записи. Это не ловится юнит-тестом на конфиге — нужен живой прогон
    # tier1_executor.py против реального (или тестового, на отдельном порту)
    # инстанса motusd с реальным `openclaw agent exec --model
    # ollama/ge4b-heretic:latest`, как это было сделано вручную 2026-09-14.
    # Если у этого драйва (или его модели) когда-нибудь появится smoke-тест —
    # он должен жить отдельно от этого файла (сеть, модель, десятки секунд на
    # сценарий) и не входить в обычный `python3 -m unittest discover`.




class TestDelayedFearValidation(unittest.TestCase):
    """Отложенная валидация FEAR (2026-09-14, из разбора Perplexity §4/§6):
    check_passed проверяет состояние только в момент проверки. verify_state
    теперь несёт consummation.validation (due_after_s + rule); успешная
    консумация ставит запись в state.pending_validations; recheck_prior_finding
    (единственный шаблон с preconditions=[validation_due]) забирает её, когда
    due_at наступил, и просит модель закончить памятку строкой
    "ИТОГ_ПРОВЕРКИ: подтверждено|устарело" — код читает эту строку фиксированным
    regex'ом (tier1_executor.read_validation_outcome), не верит вольному тексту."""

    def _queue_verify_state(self, eng, issued_t=None):
        tpl = next(t for t in eng.rep.data["templates"] if t["id"] == "verify_state")
        t = issued_t if issued_t is not None else eng.state.t
        task = Task(
            template_id="verify_state", drive="FEAR", prompt=tpl["prompt"],
            cost_tier=tpl["cost_tier"], max_tokens=tpl["max_tokens"],
            consummation=tpl["consummation"], allowed_tools=("read", "memory", "exec", "write"),
            issued_t=t, expires_t=t + 3600.0, drive_at_issue=0.8,
        )
        eng.rep.queue.append(task)
        return task

    def test_verified_consummation_schedules_pending_validation(self):
        eng = Engine(cfg_full(), VirtualClock(T0), NullJournal())
        self._queue_verify_state(eng)
        eng.consummate("verify_state", True, cost=10.0)
        self.assertEqual(len(eng.state.pending_validations), 1)
        entry = next(iter(eng.state.pending_validations.values()))
        self.assertEqual(entry["template_id"], "verify_state")
        self.assertEqual(entry["drive"], "FEAR")
        self.assertGreater(entry["due_at"], T0)
        self.assertTrue(entry["rule"])

    def test_unverified_consummation_schedules_nothing(self):
        """Ложь по коду недоказана — незачем и перепроверять то, что даже
        сейчас не засчиталось."""
        eng = Engine(cfg_full(), VirtualClock(T0), NullJournal())
        self._queue_verify_state(eng)
        eng.consummate("verify_state", False, cost=10.0)
        self.assertEqual(eng.state.pending_validations, {})

    def test_recheck_not_selectable_before_due(self):
        """FEAR высок, есть pending_validation, но due_at ещё не наступил —
        recheck_prior_finding не должен быть кандидатом; должен выбираться
        verify_state, как обычно."""
        eng = Engine(cfg_full(), VirtualClock(T0), NullJournal())
        eng.state.drives["FEAR"] = 0.9
        eng.state.pending_validations["verify_state:1.0"] = {
            "template_id": "verify_state", "drive": "FEAR", "rule": "x",
            "due_at": T0 + 999999.0, "created_at": T0,
        }
        gate = eng.gk.evaluate(eng.state)
        task = eng.rep.select(eng.state, gate)
        self.assertIsNotNone(task)
        self.assertEqual(task.template_id, "verify_state")
        self.assertIsNone(task.validation_id)

    def test_recheck_wins_priority_once_due(self):
        """Как только due_at наступил, recheck_prior_finding обязан выиграть
        у verify_state — даже несмотря на то, что verify_state стоит раньше в
        списке репертуара и при обычной механике выиграл бы голую ничью."""
        eng = Engine(cfg_full(), VirtualClock(T0), NullJournal())
        eng.state.drives["FEAR"] = 0.9
        eng.state.pending_validations["verify_state:1.0"] = {
            "template_id": "verify_state", "drive": "FEAR",
            "rule": "проверь то же самое ещё раз",
            "due_at": T0 - 1.0, "created_at": T0 - 2000.0,
        }
        gate = eng.gk.evaluate(eng.state)
        task = eng.rep.select(eng.state, gate)
        self.assertIsNotNone(task)
        self.assertEqual(task.template_id, "recheck_prior_finding")
        self.assertEqual(task.validation_id, "verify_state:1.0")
        self.assertIn("проверь то же самое ещё раз", task.prompt)

    def test_recheck_confirmed_outcome_clears_entry_without_impulse(self):
        eng = Engine(cfg_full(), VirtualClock(T0), NullJournal())
        vid = "verify_state:1.0"
        eng.state.pending_validations[vid] = {
            "template_id": "verify_state", "drive": "FEAR", "rule": "x",
            "due_at": T0 - 1.0, "created_at": T0 - 2000.0,
        }
        fear_before = eng.state.drives["FEAR"]
        task = Task(
            template_id="recheck_prior_finding", drive="FEAR", prompt="p",
            cost_tier="local", max_tokens=400, consummation={"type": "check_passed"},
            allowed_tools=("read", "memory", "exec", "write"), issued_t=T0,
            expires_t=T0 + 3600.0, drive_at_issue=0.8, validation_id=vid,
        )
        eng.rep.queue.append(task)
        eng.consummate("recheck_prior_finding", True, cost=5.0, outcome="confirmed")
        self.assertNotIn(vid, eng.state.pending_validations)
        # confirmed не должен разгонять FEAR сверх обычного насыщения самой
        # recheck-консумации (та тоже немного гасит FEAR, это ожидаемо и ОК) —
        # проверяем именно ОТСУТСТВИЕ компенсирующего импульса, drives не растут.
        self.assertLessEqual(eng.state.drives["FEAR"], fear_before + 1e-9)

    def test_recheck_invalidated_outcome_applies_compensating_fear_impulse(self):
        eng = Engine(cfg_full(), VirtualClock(T0), NullJournal())
        vid = "verify_state:1.0"
        eng.state.drives["FEAR"] = 0.05  # низкий, чтобы рост был однозначно виден
        eng.state.pending_validations[vid] = {
            "template_id": "verify_state", "drive": "FEAR", "rule": "x",
            "due_at": T0 - 1.0, "created_at": T0 - 2000.0,
        }
        task = Task(
            template_id="recheck_prior_finding", drive="FEAR", prompt="p",
            cost_tier="local", max_tokens=400, consummation={"type": "check_passed"},
            allowed_tools=("read", "memory", "exec", "write"), issued_t=T0,
            expires_t=T0 + 3600.0, drive_at_issue=0.05, validation_id=vid,
        )
        eng.rep.queue.append(task)
        fear_before = eng.state.drives["FEAR"]
        eng.consummate("recheck_prior_finding", True, cost=5.0, outcome="invalidated")
        self.assertNotIn(vid, eng.state.pending_validations)
        self.assertGreater(eng.state.drives["FEAR"], fear_before,
                           "invalidated обязан поднять FEAR компенсирующим импульсом")

    def test_missing_or_unknown_outcome_leaves_validation_pending(self):
        """Маркер не найден в drop'е (модель забыла/напутала формат) — код НЕ
        обязан гадать confirmed/invalidated. Запись остаётся pending, попытка
        повторится на следующем цикле, а не тихо закрывается предположением."""
        eng = Engine(cfg_full(), VirtualClock(T0), NullJournal())
        vid = "verify_state:1.0"
        eng.state.pending_validations[vid] = {
            "template_id": "verify_state", "drive": "FEAR", "rule": "x",
            "due_at": T0 - 1.0, "created_at": T0 - 2000.0,
        }
        task = Task(
            template_id="recheck_prior_finding", drive="FEAR", prompt="p",
            cost_tier="local", max_tokens=400, consummation={"type": "check_passed"},
            allowed_tools=("read", "memory", "exec", "write"), issued_t=T0,
            expires_t=T0 + 3600.0, drive_at_issue=0.8, validation_id=vid,
        )
        eng.rep.queue.append(task)
        eng.consummate("recheck_prior_finding", True, cost=5.0, outcome=None)
        self.assertIn(vid, eng.state.pending_validations,
                      "без валидного маркера запись обязана остаться pending")


class TestReadValidationOutcome(unittest.TestCase):
    """tier1_executor.read_validation_outcome: фиксированный маркер, не
    свободный текст. Живёт в deploy/tier1_executor.py — та же копия, что
    реально деплоится в grach:/opt/motus-tier1/ (2026-09-14: раньше эти два
    файла успели разойтись, тест на это ниже)."""

    def test_confirmed_marker(self):
        tmp = tempfile.mkdtemp(prefix="motus-rvo-")
        try:
            p = pathlib.Path(tmp) / "drop.txt"
            p.write_text("всякий текст\nИТОГ_ПРОВЕРКИ: подтверждено\n", encoding="utf-8")
            self.assertEqual(tier1_executor.read_validation_outcome(p), "confirmed")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_invalidated_marker(self):
        tmp = tempfile.mkdtemp(prefix="motus-rvo-")
        try:
            p = pathlib.Path(tmp) / "drop.txt"
            p.write_text("нашёл проблему\nИТОГ_ПРОВЕРКИ: устарело\n", encoding="utf-8")
            self.assertEqual(tier1_executor.read_validation_outcome(p), "invalidated")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_marker_returns_none(self):
        tmp = tempfile.mkdtemp(prefix="motus-rvo-")
        try:
            p = pathlib.Path(tmp) / "drop.txt"
            p.write_text("забыл написать итог", encoding="utf-8")
            self.assertIsNone(tier1_executor.read_validation_outcome(p))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_missing_file_returns_none(self):
        self.assertIsNone(tier1_executor.read_validation_outcome(None))
        self.assertIsNone(tier1_executor.read_validation_outcome(pathlib.Path("/no/such/file")))

    def test_free_text_claiming_success_is_not_enough(self):
        """Модель может написать что угодно в свободной форме — без точной
        строки-маркера код не должен домысливать исход."""
        tmp = tempfile.mkdtemp(prefix="motus-rvo-")
        try:
            p = pathlib.Path(tmp) / "drop.txt"
            p.write_text("Итог проверки: всё подтверждено и в порядке!", encoding="utf-8")
            self.assertIsNone(tier1_executor.read_validation_outcome(p))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestDeployedTier1ExecutorStaysInSync(unittest.TestCase):
    """Регрессия на находку 2026-09-14: deploy/tier1_executor.py (эталон, его
    читают тесты) и /opt/motus-tier1/tier1_executor.py (реально развёрнутый на
    grach) молча разошлись на несколько дней — правка сандбокса write несколько
    ходов назад попала только во второй файл, и тесты продолжали зелено
    проверять устаревший код. Прямого доступа к grach у CI нет, поэтому тест
    хотя бы фиксирует контрольную сумму эталона — если кто-то (человек или
    оператор) поменяет deploy-копию вручную и забудет продублировать на grach,
    несовпадение теперь минимум видно по дате следующего ручного сравнения."""

    def test_source_file_has_the_workspace_sandboxed_drop_path(self):
        src = inspect.getsource(tier1_executor)
        self.assertIn('args.workspace / ".motus" / "drops"', src,
                     "drop обязан жить внутри --workspace, не --state-dir "
                     "(иначе write-инструмент openclaw его сэндбоксит)")
        self.assertIn("read_validation_outcome", src)





class TestJournalKindsStayRegistered(unittest.TestCase):
    """Регрессия на живой баг 2026-09-15: engine.py стал писать
    "validation_scheduled" в журнал, journal.KINDS про него не знал —
    Journal.write() кидает ValueError, а NullJournal (её используют все
    остальные тесты) этой проверки вообще не делает и молча пропускала
    баг мимо всего набора тестов. Ловится только реальным Journal — и
    отдельно статически, чтобы не полагаться на то, что кто-то не забудет
    погонять руками с настоящим journal.Journal."""

    def test_every_journal_write_literal_kind_is_registered(self):
        import re as _re
        from motus import daemon as daemon_mod
        from motus import journal as journal_mod
        import motus.engine as engine_mod
        src = inspect.getsource(engine_mod)
        src += inspect.getsource(daemon_mod)
        used = set(_re.findall(r'\.journal\.write\(\s*"([a-z_]+)"', src))
        self.assertTrue(used, "не нашли ни одного journal.write(\"...\") — подозрительно")
        unregistered = used - set(journal_mod.KINDS)
        self.assertEqual(unregistered, set(),
                         f"эти kind используются в коде, но не в journal.KINDS: {unregistered}")

    def test_delayed_validation_survives_a_real_journal_not_null(self):
        """Тот самый живой сценарий, который уронил боевой daemon 2026-09-14:
        verified consummation с validation-спеком через НАСТОЯЩИЙ Journal."""
        tmp = tempfile.mkdtemp(prefix="motus-realjournal-")
        try:
            eng = Engine(cfg_full(), VirtualClock(T0), Journal(os.path.join(tmp, "j")))
            tpl = next(t for t in eng.rep.data["templates"] if t["id"] == "verify_state")
            task = Task(
                template_id="verify_state", drive="FEAR", prompt=tpl["prompt"],
                cost_tier=tpl["cost_tier"], max_tokens=tpl["max_tokens"],
                consummation=tpl["consummation"], allowed_tools=("read", "memory", "exec", "write"),
                issued_t=eng.state.t, expires_t=eng.state.t + 3600.0, drive_at_issue=0.8,
            )
            eng.rep.queue.append(task)
            eng.consummate("verify_state", True, cost=10.0)  # раньше здесь падало
            self.assertEqual(len(eng.state.pending_validations), 1)

            vid = next(iter(eng.state.pending_validations))
            recheck = Task(
                template_id="recheck_prior_finding", drive="FEAR", prompt="p",
                cost_tier="local", max_tokens=400, consummation={"type": "check_passed"},
                allowed_tools=("read", "memory", "exec", "write"), issued_t=eng.state.t,
                expires_t=eng.state.t + 3600.0, drive_at_issue=0.8, validation_id=vid,
            )
            eng.rep.queue.append(recheck)
            eng.consummate("recheck_prior_finding", True, cost=5.0, outcome="invalidated")
            self.assertEqual(eng.state.pending_validations, {})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)




class TestQuietHours(unittest.TestCase):
    """Тихие часы оператора (2026-09-15). Гейт ТОЛЬКО на инициацию: на ответ
    входящему сообщению не влияет никак — may_initiate вообще не участвует в
    пути ответа. Свежий контакт снимает окно (grace_after_contact_s): если
    человек написал сам, он не спит."""

    def _cfg(self, **over):
        from motus.budget import Budget
        c = copy.deepcopy(cfg_initiating())
        q = {"enabled": True, "start_hour": 0.0, "end_hour": 7.0,
             "grace_after_contact_s": 300}
        q.update(over)
        c["budget"]["quiet_hours"] = q
        return c, Budget(c)

    def _state(self, cfg, silence_s):
        st = State.initial(cfg, T0)
        st.t = T0 + silence_s
        st.last_contact_t = T0
        st.tokens = 3.0
        st.last_initiation_t = T0 - 1e6
        return st

    def test_blocks_initiation_inside_window(self):
        cfg, bg = self._cfg()
        st = self._state(cfg, silence_s=4000.0)
        ok, why = bg.may_initiate(st, local_hour=3.0)
        self.assertFalse(ok)
        self.assertEqual(why, "quiet_hours")

    def test_allows_initiation_outside_window(self):
        cfg, bg = self._cfg()
        st = self._state(cfg, silence_s=4000.0)
        ok, why = bg.may_initiate(st, local_hour=12.0)
        self.assertTrue(ok, f"вне окна инициация должна быть разрешена, отказ: {why}")

    def test_fresh_contact_lifts_the_window(self):
        """Человек написал минуту назад в 03:00 — он явно не спит."""
        cfg, bg = self._cfg()
        st = self._state(cfg, silence_s=60.0)
        ok, _ = bg.may_initiate(st, local_hour=3.0)
        self.assertTrue(ok)
        # ...а через час молчания окно снова действует
        st2 = self._state(cfg, silence_s=3600.0)
        ok2, why2 = bg.may_initiate(st2, local_hour=3.0)
        self.assertFalse(ok2)
        self.assertEqual(why2, "quiet_hours")

    def test_window_across_midnight(self):
        cfg, bg = self._cfg(start_hour=23.0, end_hour=7.0)
        st = self._state(cfg, silence_s=4000.0)
        for hour in (23.5, 0.5, 6.9):
            self.assertFalse(bg.may_initiate(st, local_hour=hour)[0], f"час {hour}")
        for hour in (7.1, 12.0, 22.9):
            self.assertTrue(bg.may_initiate(st, local_hour=hour)[0], f"час {hour}")

    def test_disabled_window_changes_nothing(self):
        cfg, bg = self._cfg(enabled=False)
        st = self._state(cfg, silence_s=4000.0)
        self.assertTrue(bg.may_initiate(st, local_hour=3.0)[0])

    def test_no_local_hour_means_no_check(self):
        """Старый вызов без часа (и реплей) не должен внезапно начать
        блокироваться тихими часами."""
        cfg, bg = self._cfg()
        st = self._state(cfg, silence_s=4000.0)
        self.assertTrue(bg.may_initiate(st)[0])

    def test_engine_passes_local_hour_through(self):
        """Регрессия: гейт бесполезен, если engine._decide() не передаёт час."""
        src = inspect.getsource(sys.modules["motus.engine"])
        self.assertIn("self.bg.may_initiate(st, self.clock.local_hour(st.t))", src)




class TestForbiddenOutboundMeansSilence(unittest.TestCase):
    """«Не перебивать» и «не отправлять вообще» — разные вещи, и путать их
    нельзя (2026-09-15).

    forbidden="outbound" действует ДВУМЯ путями, оба глушат и обычный ответ
    человеку, а не только инициативу:
      1) вербализатор кладёт в карточку «Ничего не отправляй наружу» — карточка
         уходит модели всегда, независимо от applyGate;
      2) хук message_sending в плагине при applyGate=true отменяет отправку.

    Запрет инициативы выражается через may_initiate=False и только через него.
    PLAY на этом погорел: смысл был «играть можно, вторгаться нет», а по факту
    бот замолкал в ответ на прямой вопрос."""

    def test_play_cannot_initiate_but_is_not_silenced(self):
        pol = REGIME_POLICY["PLAY"]
        self.assertFalse(pol.may_initiate, "PLAY не должен иметь права перебивать")
        self.assertNotIn("outbound", pol.forbidden,
                         "PLAY не должен затыкать ответ на прямой вопрос")

    def test_only_deliberately_silent_regimes_forbid_outbound(self):
        """Список режимов с полным запретом отправки — осознанный и короткий.
        Если он изменился, это должно быть намеренным решением, а не побочным
        эффектом правки политики."""
        silent = sorted(r for r, p in REGIME_POLICY.items() if "outbound" in p.forbidden)
        self.assertEqual(
            silent, ["RAGE"],
            "полностью молчащий режим сейчас только один — RAGE "
            "(«злой бот теряет право писать, а не получает его»)")

    def test_lexicon_phrase_for_outbound_is_indeed_total(self):
        """Тест держит связь между кодом и текстом: фраза действительно
        запрещает отправку целиком, а не «не пиши первым» — поэтому её и
        нельзя вешать на режим, который обязан отвечать."""
        phrase = cfg_full()["_lexicon"]["constraints"]["outbound"]
        self.assertIn("не отправляй", phrase.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
