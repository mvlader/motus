"""Тесты MOTUS. Только stdlib: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import copy
import json
import math
import os
import random
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motus import config, replay  # noqa: E402
from motus.clock import VirtualClock  # noqa: E402
from motus.core import Homeostat  # noqa: E402
from motus.engine import Engine  # noqa: E402
from motus.events import Appraisal, Event, Impulse  # noqa: E402
from motus.gates import REGIME_POLICY, Gatekeeper  # noqa: E402
from motus.journal import Journal, NullJournal  # noqa: E402
from motus.state import State  # noqa: E402
from motus.verbalizer import CardError, Verbalizer  # noqa: E402

T0 = 1767225600.0  # фиксированная точка отсчёта, чтобы тесты не зависели от «сегодня»


def cfg_full():
    return config.load()


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
        """Поведенческий булев критерий из docs/01-structure.md §9."""
        cfg = cfg_full()
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
        cfg = cfg_full()
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
            eng = Engine(cfg_full(), ck, NullJournal())
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
        cfg = cfg_full()
        ck = VirtualClock(T0)
        eng = Engine(cfg, ck, NullJournal())
        for _ in range(400):
            ck.advance(300)
            eng.tick(ck.now())
        self.assertGreater(eng.state.act_penalty, 0.0)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
