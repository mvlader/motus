"""L0 — гомеостат. Чистая математика, ни одной строки текста, ни одного вызова LLM.

Все формулы — docs/03-formulas.md. Главное свойство, которое обязано сохраняться при
любой правке: точная композируемость по Δt (advance(60) ≡ 6×advance(10)) при
постоянных входах. Проверяется tests/test_core.py::test_dt_invariance.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

from .clock import Clock, circadian_energy, context_band, freshness
from .config import DRIVES, MODULATORS
from .events import Impulse
from .state import State


def _clip(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


class Homeostat:
    def __init__(self, cfg: Dict[str, Any], clock: Clock) -> None:
        self.cfg = cfg
        self.clock = clock
        self.appetitive = set(cfg["appetitive"])
        self.aversive = set(cfg["aversive"])

    # ------------------------------------------------------------------ время

    def energy(self, st: State) -> float:
        """Смешанная энергия: циркадный ритм + датчик. §1.1"""
        c = self.cfg["circadian"]
        circ = circadian_energy(self.clock.local_hour(st.t), c["amp"], c["peak_hour"])
        w = c["weight"]
        return _clip(w * circ + (1.0 - w) * st.somatic["energy"], 0.0, 1.0)

    def silence_s(self, st: State) -> float:
        return max(0.0, st.t - st.last_contact_t)

    def context_freshness(self, st: State) -> float:
        return freshness(max(0.0, st.t - st.last_context_t), self.cfg["context"]["tau_s"])

    def context_band(self, st: State) -> str:
        c = self.cfg["context"]
        return context_band(
            self.context_freshness(st), c["band_fresh"], c["band_aging"], c["band_stale"]
        )

    # --------------------------------------------------------- производные

    def max_aversive(self, st: State) -> float:
        return max(st.drives[n] for n in self.aversive)

    def arousal(self, st: State) -> float:
        """§5. Усталость снижает активацию, а не повышает."""
        a = self.cfg["arousal"]
        ne0 = self.cfg["temperament"]["ne"]
        v = (
            a["a0"]
            + a["k_ne"] * (st.modulators["ne"] - ne0)
            + a["k_aversive"] * self.max_aversive(st)
            - a["k_energy"] * (1.0 - self.energy(st))
        )
        return _clip(v, 0.0, 1.0)

    # ------------------------------------------------------------ сетпоинты

    def idle_s(self, st: State) -> float:
        """Время без нового входа. Основа скуки."""
        return max(0.0, st.t - st.last_context_t)

    def effective_setpoint(self, st: State, name: str) -> float:
        """§2. Четыре контекстных слагаемых — и больше никаких."""
        base = self.cfg["drives"][name]["setpoint"]
        if name == "PANIC":
            # Альфа-функция: протест → отчаяние → отстранение. Сепарационный
            # дистресс нарастает, достигает пика и спадает — как у Панксеппа, а не
            # как ступенька. Монотонный вариант защёлкивал PANIC навсегда и
            # монополизировал режим на всё время отсутствия человека.
            sep = self.cfg["separation"]
            u = (self.silence_s(st) - sep["grace_s"]) / sep["tau_s"]
            if u <= 0.0:
                return 0.0
            return _clip(sep["max"] * u * math.exp(1.0 - u), 0.0, 1.0)
        if name == "SEEKING":
            b = self.cfg["boredom"]
            over = self.idle_s(st) - b["grace_s"]
            bored = b["max"] * (1.0 - math.exp(-over / b["tau_s"])) if over > 0 else 0.0
            return _clip(base * (0.5 + 0.5 * self.energy(st)) + bored, 0.0, 1.0)
        if name == "PLAY":
            # Игра подавляется любым активным аверсивным драйвом — прямо из Панксеппа.
            return _clip(base * self.energy(st) * (1.0 - self.max_aversive(st)), 0.0, 1.0)
        return base

    def tau_eff(self, st: State, name: str) -> float:
        """§3. ne ускоряет всё; ht5 ускоряет спад только аверсивных."""
        m = self.cfg["modulator"]
        temp = self.cfg["temperament"]
        denom = 1.0 + m["k_ne"] * (st.modulators["ne"] - temp["ne"])
        if name in self.aversive:
            denom += m["k_ht5"] * (st.modulators["ht5"] - temp["ht5"])
        return self.cfg["drives"][name]["tau_relax_s"] / _clip(denom, 0.25, 4.0)

    # -------------------------------------------------------------- пороги

    def theta_eff(self, st: State, name: str) -> Tuple[float, float]:
        """§6. ht5 поднимает пороги (терпимость), da опускает (нетерпение)."""
        th = self.cfg["thresholds"]
        temp = self.cfg["temperament"]
        shift = th["k_ht5"] * (st.modulators["ht5"] - temp["ht5"]) - th["k_da"] * (
            st.modulators["da"] - temp["da"]
        )
        d = self.cfg["drives"][name]
        hi = _clip(d["theta_hi"] + shift, 0.05, 0.98)
        lo = min(d["theta_lo"] + shift, hi - 0.05)
        return hi, lo

    def activation(self, st: State, name: str) -> float:
        """Нормированная активация a = x / θ_hi_eff. Сравнима между драйвами."""
        hi, _ = self.theta_eff(st, name)
        return st.drives[name] / hi if hi > 0 else 0.0

    # ------------------------------------------------------------ импульсы

    def modulator_gain(self, st: State, name: str) -> float:
        """§4."""
        m = self.cfg["modulator"]
        temp = self.cfg["temperament"]
        if name in self.appetitive:
            g = 1.0 + m["k_da"] * (st.modulators["da"] - temp["da"])
        else:
            g = (
                1.0
                + m["k_ne"] * (st.modulators["ne"] - temp["ne"])
                - m["k_ht5"] * (st.modulators["ht5"] - temp["ht5"])
            )
        return _clip(g, 0.2, 2.0)

    def habituation_factor(self, st: State, key: str) -> float:
        n = st.habituation.get(key, 0.0)
        return math.exp(-n / self.cfg["habituation"]["kappa"])

    def apply_impulse(self, st: State, imp: Impulse) -> float:
        """§4. Двустороннее насыщение: x не покидает [0,1] по построению, не clip'ом."""
        if imp.drive not in st.drives:
            return 0.0
        delta = (
            imp.amplitude
            * self.cfg["drives"][imp.drive]["gain"]
            * self.modulator_gain(st, imp.drive)
            * self.habituation_factor(st, imp.key)
        )
        x = st.drives[imp.drive]
        x_new = x + delta * (1.0 - x) if delta > 0 else x + delta * x
        st.drives[imp.drive] = _clip(x_new, 0.0, 1.0)
        st.habituation[imp.key] = st.habituation.get(imp.key, 0.0) + 1.0
        return st.drives[imp.drive] - x

    def consummate(self, st: State, drive: str, verified: bool, key: str) -> float:
        """§9. Насыщение пропорционально текущему уровню; в ноль один акт не гасит."""
        c = self.cfg["consummation"]
        rho = c["rho_full"] if verified else c["rho_part"]
        x = st.drives[drive]
        st.drives[drive] = _clip(x - rho * x, 0.0, 1.0)
        if not verified:
            hk = f"unverified:{key}"
            st.habituation[hk] = st.habituation.get(hk, 0.0) + 1.0
        return st.drives[drive] - x

    # ----------------------------------------------------------------- тик

    def advance(self, st: State, t: float) -> float:
        """Продвинуть состояние до момента t. Возвращает фактический Δt."""
        dt = t - st.t
        if dt < 0:
            # Микроскопический откат — это round-trip float через JSON, а не сбой часов.
            if dt > -1e-6:
                return 0.0
            raise ValueError(f"время идёт назад: {t} < {st.t}")
        if dt == 0:
            return 0.0

        # Габитуация восстанавливается.
        tau_hab = self.cfg["habituation"]["tau_s"]
        decay = math.exp(-dt / tau_hab)
        for k in list(st.habituation):
            n = st.habituation[k] * decay
            if n < 1e-4:
                del st.habituation[k]
            else:
                st.habituation[k] = n

        # Модуляторы стягиваются к темпераменту той же экспонентой, что и драйвы
        # (см. §11 формул) — никакой отдельной процедуры, просто ещё одна релаксация.
        mtau = self.cfg["modulator"]["tau_s"]
        mdecay = math.exp(-dt / mtau)
        for k in MODULATORS:
            base = self.cfg["temperament"][k]
            st.modulators[k] = base + (st.modulators[k] - base) * mdecay

        # Драйвы: релаксация к эффективному сетпоинту.
        # Время двигаем ДО расчёта сетпоинтов: сепарация и циркадный ритм зависят
        # от нового t, и это правильная семантика — «за прошедшее время стало так».
        st.t = t
        for name in DRIVES:
            s = self.effective_setpoint(st, name)
            tau = self.tau_eff(st, name)
            x = st.drives[name]
            st.drives[name] = _clip(s + (x - s) * math.exp(-dt / tau), 0.0, 1.0)

        # Штраф за молчание спадает.
        st.act_penalty *= math.exp(-dt / self.cfg["budget"]["penalty_tau_s"])
        if st.act_penalty < 1e-4:
            st.act_penalty = 0.0

        return dt
