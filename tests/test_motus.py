"""Тесты MOTUS. Только stdlib: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import argparse
import copy
import json
import math
import unittest.mock
import os
import pathlib
import random
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motus import appraisal as appraisal_mod  # noqa: E402
from motus import config, replay  # noqa: E402
from motus.clock import VirtualClock  # noqa: E402
from motus.core import Homeostat  # noqa: E402
from motus.engine import Engine  # noqa: E402
from motus.events import Appraisal, Event, Impulse  # noqa: E402
from motus.gates import REGIME_POLICY, Gatekeeper  # noqa: E402
from motus.journal import Journal, NullJournal  # noqa: E402
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

    def test_initiation_disabled_by_default_no_phantom_penalty(self):
        """default.json: проактив выключен. 72 ч тишины → ни одной инициации и
        ноль act_penalty: движок не наказывает себя за неотправленные сообщения."""
        cfg = cfg_full()
        self.assertFalse(cfg["budget"].get("initiation_enabled", True))
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
        a.rep.record("explore_unread_file", -0.4, 100.0, True)
        ea = next(t for t in a.rep.data["templates"] if t["id"] == "explore_unread_file")
        eb = next(t for t in b.rep.data["templates"] if t["id"] == "explore_unread_file")
        ec = next(t for t in cfg["_repertoire"]["templates"] if t["id"] == "explore_unread_file")
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
        p = tier1_executor.PREAMBLE.format(tools="read, memory", drop="/d",
                                           max_tokens=200, prompt="сделай X")
        self.assertIn("НЕ отправляй никаких сообщений", p)
        self.assertIn("сделай X", p)
        self.assertIn("/d", p)

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
                drop = pathlib.Path(tmp) / "drops" / "prepare_reentry-111"
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
