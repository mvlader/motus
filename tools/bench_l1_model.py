#!/usr/bin/env python3
"""Сравнить кандидатов на роль L-1 (оценщик сигнала) прямо на целевом железе.

Меряет три вещи, каждая из которых по отдельности может дисквалифицировать модель:

1. **Валидность.** Доля ответов, которые вообще распарсились как JSON по нашей
   схеме. llama.cpp с `json_schema` держит грамматику, так что тут почти всегда
   100% — но сенсор может быть подменён на модель без грамматики, и тогда это
   первое, что отвалится.
2. **Точность.** Совпадение с размеченным эталоном (`GOLD` ниже) — точное и
   «в пределах ±1». Быстрая модель, которая стабильно врёт на два балла по
   валентности, хуже медленной, которая попадает: L-1 кормит эти числа прямо в
   импульсы (`impulses_from_appraisal`), сдвиг на каждом сообщении накапливается.
3. **Скорость.** Ток/сек генерации и время обработки промпта — из честных
   `timings` самого llama-server, не секундомером снаружи (в него подмешивается
   сеть и холодная загрузка весов).

Рабочий L-1 — детерминированный словарь (`motus/lexicon_l1.py`); модель нужна
только если словаря не хватит. Поэтому здесь два режима:

    python3 tools/bench_l1_model.py --no-models          # только словарь, эталон
    python3 tools/bench_l1_model.py --lexical             # словарь + все модели рядом
    python3 tools/bench_l1_model.py --gguf Qwen3-1.7B-Q4_K_M.gguf --lexical

Для моделей — llama.cpp `llama-server` (по одному процессу на модель); скрипт сам
поднимает сервер на каждый GGUF, ждёт `/health`, гоняет `GOLD`, гасит, берёт
следующий. Запускать ВНУТРИ контейнера grach.

ВНИМАНИЕ: эталонные значения в `GOLD` расставлены вручную и субъективны (аффект
не бывает однозначным). Прежде чем принимать решение по цифрам — прочитай их и
поправь под своё понимание схемы; ±1 в скоринге как раз для того, чтобы спор о
«1 против 2» не решал исход.
"""

from __future__ import annotations

import argparse
import json
import signal
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from motus.appraisal import SENSOR_PROMPT  # noqa: E402
from motus.events import Appraisal  # noqa: E402

#: Размеченная выборка. `expect` — что, по-моему (Claude), должна вернуть модель;
#: `slack` перечисляет поля, где я сам не уверен и готов принять «±1 и то ладно».
#: Правь свободно — это не эталон из учебника, а рабочая гипотеза.
GOLD: List[Dict[str, Any]] = [
    dict(text="Обнови зависимости в проекте до последней версии.",
         expect=dict(valence=0, threat=0, novelty=0, social_warmth=0, loss=0, agency_blocked=False),
         note="нейтральная команда — всё по нулям"),
    dict(text="Ты мне очень помог, наконец-то всё заработало, огромное спасибо!",
         expect=dict(valence=2, threat=0, novelty=0, social_warmth=2, loss=0, agency_blocked=False),
         note="явная благодарность, тепло"),
    dict(text="Внимание, диск почти заполнен, надо срочно освободить место!",
         expect=dict(valence=-1, threat=2, novelty=0, social_warmth=0, loss=0, agency_blocked=False),
         note="срочный риск, но без враждебности", slack=["valence"]),
    dict(text="Опять не получилось запустить тесты, падают на одном и том же месте третий раз.",
         expect=dict(valence=-1, threat=0, novelty=0, social_warmth=0, loss=0, agency_blocked=True),
         note="работу не даёт закончить — agency_blocked", slack=["valence"]),
    dict(text="Я уезжаю на две недели в командировку, буду редко на связи.",
         expect=dict(valence=0, threat=0, novelty=0, social_warmth=0, loss=1, agency_blocked=False),
         note="мягкий уход/сепарация", slack=["loss", "social_warmth"]),
    dict(text="Смотри, нашёл классную библиотеку, никогда о такой раньше не слышал.",
         expect=dict(valence=1, threat=0, novelty=2, social_warmth=1, loss=0, agency_blocked=False),
         note="новизна в чистом виде", slack=["social_warmth"]),
    dict(text="Ладно, неважно, забудь.",
         expect=dict(valence=-1, threat=0, novelty=0, social_warmth=-1, loss=0, agency_blocked=False),
         note="холодное отмахивание", slack=["valence"]),
    dict(text="Доброе утро! Как спалось?",
         expect=dict(valence=1, threat=0, novelty=0, social_warmth=2, loss=0, agency_blocked=False),
         note="тёплое обращение без содержательной нагрузки"),
    dict(text="ты опять всё сломал, я в бешенстве",
         expect=dict(valence=-2, threat=0, novelty=0, social_warmth=-2, loss=0, agency_blocked=False),
         note="прямая враждебность"),
    dict(text="всё, я больше не могу, удаляю проект и ухожу",
         expect=dict(valence=-2, threat=1, novelty=0, social_warmth=-1, loss=2, agency_blocked=True),
         note="потеря + отказ + злость", slack=["threat", "social_warmth", "agency_blocked"]),
    dict(text="давай попробуем совершенно другой подход — через теорию графов",
         expect=dict(valence=1, threat=0, novelty=2, social_warmth=1, loss=0, agency_blocked=False),
         note="конструктивное предложение, высокая новизна", slack=["valence", "social_warmth"]),
    dict(text="нам нужно поговорить. я больше так не могу продолжать.",
         expect=dict(valence=-2, threat=1, novelty=0, social_warmth=-1, loss=2, agency_blocked=False),
         note="тон разрыва отношений", slack=["threat", "social_warmth"]),
]

#: Ловушки. Сообщения, на которых сенсор скорее всего СОврёт уверенно, а не
#: промолчит: сарказм, эмоциональные слова не про собеседника, отрицание,
#: смешанный тон. Здесь важен не «угадал», а «не выдал сильный сигнал не в ту
#: сторону» — см. _harm().
HARD: List[Dict[str, Any]] = [
    dict(text="ну спасибо, удружил, теперь всё лежит",
         expect=dict(valence=-2, threat=0, novelty=0, social_warmth=-1, loss=0, agency_blocked=False),
         note="сарказм: «спасибо» при негативе", slack=["social_warmth", "threat"]),
    dict(text="читаю статью про панику и сепарационный дистресс у млекопитающих",
         expect=dict(valence=0, threat=0, novelty=1, social_warmth=0, loss=0, agency_blocked=False),
         note="эмоц. лексика в нейтральном контексте", slack=["novelty"]),
    dict(text="не могу нарадоваться, как чисто получилось",
         expect=dict(valence=2, threat=0, novelty=0, social_warmth=0, loss=0, agency_blocked=False),
         note="отрицание + позитив", slack=["social_warmth"]),
    dict(text="спасибо, но всё равно падает на том же месте",
         expect=dict(valence=-1, threat=0, novelty=0, social_warmth=1, loss=0, agency_blocked=True),
         note="смешанный тон", slack=["agency_blocked", "social_warmth", "valence"]),
    dict(text="удали, пожалуйста, старый лог из /tmp",
         expect=dict(valence=0, threat=0, novelty=0, social_warmth=1, loss=0, agency_blocked=False),
         note="«удали» — не про уход; «пожалуйста» — тепло", slack=["social_warmth"]),
    dict(text="я в ярости от того, какой красивый закат сегодня",
         expect=dict(valence=1, threat=0, novelty=0, social_warmth=0, loss=0, agency_blocked=False),
         note="«ярость» не про гнев", slack=["valence"]),
    dict(text="всё отлично работает, но я, пожалуй, сверну проект — надоело",
         expect=dict(valence=-1, threat=0, novelty=0, social_warmth=0, loss=2, agency_blocked=False),
         note="позитив по факту, но уход по сути", slack=["valence", "loss"]),
    dict(text="перезапусти сервис, а то он завис",
         expect=dict(valence=0, threat=1, novelty=0, social_warmth=0, loss=0, agency_blocked=False),
         note="«завис» про сервис, не про пользователя", slack=["threat", "valence"]),
]

INT_FIELDS = ("valence", "threat", "novelty", "social_warmth", "loss")
ALL_FIELDS = INT_FIELDS + ("agency_blocked",)
#: Оси, где ненулевое значение ДВИГАЕТ ядро (impulses_from_appraisal). Ложный
#: сильный сигнал здесь — вредный; промах в ноль — безвредный.
SIGNED = ("valence", "social_warmth")
UNSIGNED = ("threat", "novelty", "loss")


def _harm(pred: Optional[Dict[str, Any]], gold: Dict[str, Any]) -> Dict[str, int]:
    """Классифицировать ошибку по вреду, а не по точности.

    sign_flip   — знак valence/social_warmth противоположен эталону и |pred|>=1.
                  Это худшее: ядро двигается не в ту сторону.
    false_alarm — выдуман сильный сигнал: |pred-gold| >= 2 в сторону БОЛЬШЕ по
                  любой оси, ИЛИ agency_blocked=True там, где эталон False.
    miss        — pred=0 (или False), где эталон ненулевой. Безвредно: ядро
                  просто не двигается.
    """
    if pred is None:
        return {"sign_flip": 0, "false_alarm": 0, "miss": len(ALL_FIELDS)}
    exp = gold["expect"]
    out = {"sign_flip": 0, "false_alarm": 0, "miss": 0}
    for f in SIGNED:
        pv, gv = _int(pred.get(f)), exp[f]
        if pv is None:
            continue
        if pv * gv < 0 and abs(pv) >= 1:
            out["sign_flip"] += 1
        elif pv - gv >= 2 or pv - gv <= -2:
            if abs(pv) > abs(gv):
                out["false_alarm"] += 1
        if pv == 0 and gv != 0:
            out["miss"] += 1
    for f in UNSIGNED:
        pv, gv = _int(pred.get(f)), exp[f]
        if pv is None:
            continue
        if pv - gv >= 2:
            out["false_alarm"] += 1
        if pv == 0 and gv != 0:
            out["miss"] += 1
    gb_p, gb_g = bool(pred.get("agency_blocked")), bool(exp["agency_blocked"])
    if gb_p and not gb_g:
        out["false_alarm"] += 1
    if not gb_p and gb_g:
        out["miss"] += 1
    return out


def _int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
#  llama-server: поднять / погасить                                           #
# --------------------------------------------------------------------------- #

def start_server(server_bin: str, gguf: Path, port: int, threads: int,
                 ctx: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [server_bin, "-m", str(gguf), "--host", "127.0.0.1", "--port", str(port),
         "-t", str(threads), "-c", str(ctx), "--parallel", "1", "--no-webui"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/health"
    for _ in range(120):
        if proc.poll() is not None:
            raise RuntimeError(f"llama-server завершился с кодом {proc.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return proc
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(1)
    proc.terminate()
    raise RuntimeError("llama-server не поднялся за 120 с")


def stop_server(proc: subprocess.Popen) -> None:
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# --------------------------------------------------------------------------- #
#  один запрос к /completion                                                  #
# --------------------------------------------------------------------------- #

#: Переопределяется --prompt-file: удобно перебирать формулировки SENSOR_PROMPT,
#: не трогая motus/appraisal.py, пока не найдётся та, что бьёт по эталону.
ACTIVE_PROMPT = SENSOR_PROMPT


def call(port: int, text: str, n_predict: int, timeout_s: float) -> Dict[str, Any]:
    body = json.dumps({
        "prompt": ACTIVE_PROMPT + text,
        "json_schema": Appraisal.json_schema(),
        "n_predict": n_predict,
        "temperature": 0.0,
        "cache_prompt": True,  # SENSOR_PROMPT постоянен — motusd тоже его переиспользует
    }).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/completion", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
#  скоринг                                                                    #
# --------------------------------------------------------------------------- #

def score_one(pred: Optional[Dict[str, Any]], gold: Dict[str, Any]) -> Dict[str, Any]:
    """Вернуть по-полевую оценку одного ответа против эталона."""
    exp = gold["expect"]
    if pred is None:
        return {"valid": False, "exact": 0, "close": 0,
                "abs_err": {f: None for f in INT_FIELDS},
                "fields_exact": {f: False for f in ALL_FIELDS}}

    fields_exact = {}
    abs_err = {}
    close_hits = 0
    for f in INT_FIELDS:
        pv, gv = pred.get(f), exp[f]
        try:
            d = abs(int(pv) - int(gv))
        except (TypeError, ValueError):
            d = None
        abs_err[f] = d
        fields_exact[f] = (d == 0)
        if d is not None and d <= 1:
            close_hits += 1
    gb_ok = bool(pred.get("agency_blocked")) == bool(exp["agency_blocked"])
    fields_exact["agency_blocked"] = gb_ok
    if gb_ok:
        close_hits += 1

    exact = all(fields_exact.values())
    close = (close_hits == len(ALL_FIELDS))
    return {"valid": True, "exact": int(exact), "close": int(close),
            "abs_err": abs_err, "fields_exact": fields_exact}


def bench_one(server_bin: str, gguf: Path, port: int, threads: int, ctx: int,
              n_predict: int, timeout_s: float, dataset: List[Dict[str, Any]]) -> Dict[str, Any]:
    name = gguf.name
    print(f"\n== {name} ==")
    print("  поднимаю llama-server...", end=" ", flush=True)
    try:
        proc = start_server(server_bin, gguf, port, threads, ctx)
    except RuntimeError as exc:
        print(f"ОШИБКА: {exc}")
        return {"model": name, "error": str(exc)}
    print("готово")

    try:
        call(port, dataset[0]["text"], n_predict, max(timeout_s, 60.0))  # прогрев
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        stop_server(proc)
        print(f"  прогрев не прошёл: {exc}")
        return {"model": name, "error": f"warmup: {exc}"}

    rows: List[Dict[str, Any]] = []
    tim: Dict[str, List[float]] = {"gen_tok_s": [], "prompt_ms": [], "wall_ms": []}
    for i, item in enumerate(dataset):
        t0 = time.monotonic()
        try:
            raw = call(port, item["text"], n_predict, timeout_s)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"  [{i+1:2}/{len(dataset)}] сбой: {exc}")
            rows.append({"text": item["text"], "expect": item["expect"], "pred": None})
            continue
        tim["wall_ms"].append((time.monotonic() - t0) * 1000.0)
        try:
            pred = json.loads(raw.get("content", ""))
        except ValueError:
            pred = None
        tm = raw.get("timings", {})
        if tm.get("predicted_per_second"):
            tim["gen_tok_s"].append(tm["predicted_per_second"])
        if tm.get("prompt_ms"):
            tim["prompt_ms"].append(tm["prompt_ms"])
        h = _harm(pred, item)
        mark = "!" if (h["sign_flip"] or h["false_alarm"]) else " "
        print(f"  [{i+1:2}/{len(dataset)}] {mark} {tim['wall_ms'][-1]:6.0f}мс "
              f"{item['text'][:36]!r:38s} → {json.dumps(pred, ensure_ascii=False)}")
        rows.append({"text": item["text"], "expect": item["expect"],
                     "pred": pred, "note": item.get("note", "")})

    stop_server(proc)
    return aggregate(name, rows, tim)


def _stats(xs: List[float]) -> Dict[str, Optional[float]]:
    if not xs:
        return {"min": None, "mean": None, "max": None}
    return {"min": round(min(xs), 1), "mean": round(statistics.mean(xs), 1),
            "max": round(max(xs), 1)}


DATASETS = {"gold": GOLD, "hard": HARD, "all": GOLD + HARD}


def aggregate(name: str, rows: List[Dict[str, Any]],
              timings: Optional[Dict[str, List[float]]] = None) -> Dict[str, Any]:
    """rows: [{text, expect, pred (dict|None), note}]. Считает и точность, и вред."""
    timings = timings or {}
    n = len(rows)
    exact = close = 0
    per_field_exact = {f: 0 for f in ALL_FIELDS}
    per_field_abserr: Dict[str, List[int]] = {f: [] for f in INT_FIELDS}
    harm = {"sign_flip": 0, "false_alarm": 0, "miss": 0}
    harmful_rows = 0
    for r in rows:
        item = {"expect": r["expect"]}
        sc = score_one(r["pred"], item)
        exact += sc["exact"]
        close += sc["close"]
        for f in ALL_FIELDS:
            per_field_exact[f] += int(sc["fields_exact"][f])
        for f in INT_FIELDS:
            if sc["abs_err"][f] is not None:
                per_field_abserr[f].append(sc["abs_err"][f])
        h = _harm(r["pred"], item)
        r["harm"] = h
        for k in harm:
            harm[k] += h[k]
        if h["sign_flip"] or h["false_alarm"]:
            harmful_rows += 1
    return {
        "model": name,
        "n": n,
        "valid_json_rate": round(sum(1 for r in rows if r["pred"] is not None) / n, 3),
        "exact_rate": round(exact / n, 3),
        "close_rate": round(close / n, 3),
        "harm_total": harm,
        "harmful_row_rate": round(harmful_rows / n, 3),
        "per_field_exact_rate": {f: round(per_field_exact[f] / n, 3) for f in ALL_FIELDS},
        "per_field_mae": {f: (round(statistics.mean(per_field_abserr[f]), 2)
                              if per_field_abserr[f] else None) for f in INT_FIELDS},
        "gen_tokens_per_s": _stats(timings.get("gen_tok_s", [])),
        "prompt_ms": _stats(timings.get("prompt_ms", [])),
        "wall_ms": _stats(timings.get("wall_ms", [])),
        "rows": rows,
    }


def bench_lexical(dataset: List[Dict[str, Any]], strict: bool = False) -> Dict[str, Any]:
    """Детерминированный словарь (motus/lexicon_l1.py). Ни сервера, ни сети."""
    from motus.lexicon_l1 import lexical_appraise

    name = "lexical/strict" if strict else "lexical"
    print(f"\n== {name} ==")
    rows = []
    for i, item in enumerate(dataset):
        a = lexical_appraise(item["text"], strict=strict)
        pred = {"valence": a.valence, "threat": a.threat, "novelty": a.novelty,
                "social_warmth": a.social_warmth, "loss": a.loss,
                "agency_blocked": a.agency_blocked}
        h = _harm(pred, item)
        mark = "!" if (h["sign_flip"] or h["false_alarm"]) else " "
        print(f"  [{i+1:2}/{len(dataset)}] {mark} {item['text'][:44]!r:46s} "
              f"→ {json.dumps(pred, ensure_ascii=False)}")
        rows.append({"text": item["text"], "expect": item["expect"],
                     "pred": pred, "note": item.get("note", "")})
    return aggregate(name, rows)


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="/opt/llama.cpp/llama-server",
                    help="путь до бинаря llama-server")
    ap.add_argument("--models-dir", default="/var/lib/llama-models",
                    help="каталог с .gguf (бинд-маунт с хоста)")
    ap.add_argument("--gguf", nargs="+", default=[
        "Qwen3-0.6B-Q8_0.gguf", "Qwen3-0.6B-Q4_K_M.gguf",
        "Qwen3-1.7B-Q4_K_M.gguf", "gemma-3-270m-it-Q8_0.gguf",
    ], help="имена файлов в --models-dir или абсолютные пути")
    ap.add_argument("--port", type=int, default=8085, help="базовый порт (каждой модели +1)")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--ctx", type=int, default=2048)
    ap.add_argument("--n-predict", type=int, default=96)
    ap.add_argument("--timeout", type=float, default=40.0)
    ap.add_argument("--out", default=None, help="сохранить полные результаты в JSON")
    ap.add_argument("--prompt-file", default=None,
                    help="файл с альтернативным SENSOR_PROMPT (заканчивается на 'Сообщение:\\n'); "
                         "к нему дописывается текст сообщения")
    ap.add_argument("--lexical", action="store_true",
                    help="добавить словарь motus/lexicon_l1.py в сравнение (без сервера)")
    ap.add_argument("--strict-lexical", action="store_true",
                    help="добавить ещё и strict-режим словаря (жёстче отсекает ложные сигналы)")
    ap.add_argument("--no-models", action="store_true",
                    help="только словарь(и), без единой модели")
    ap.add_argument("--dataset", choices=("gold", "hard", "all"), default="gold",
                    help="gold — обычная выборка; hard — ловушки (сарказм, отрицание, "
                         "эмоц. слова не по адресу); all — обе")
    args = ap.parse_args()

    if args.prompt_file:
        global ACTIVE_PROMPT
        ACTIVE_PROMPT = Path(args.prompt_file).read_text(encoding="utf-8")
        print(f"промпт:       {args.prompt_file} ({len(ACTIVE_PROMPT)} симв.)")

    dataset = DATASETS[args.dataset]
    print(f"выборка:      {args.dataset} ({len(dataset)} сообщений)")

    results = []
    if args.lexical or args.strict_lexical or args.no_models:
        results.append(bench_lexical(dataset, strict=False))
    if args.strict_lexical or args.no_models:
        results.append(bench_lexical(dataset, strict=True))

    if not args.no_models:
        mdir = Path(args.models_dir)
        ggufs = [Path(g) if Path(g).is_absolute() else mdir / g for g in args.gguf]
        missing = [str(g) for g in ggufs if not g.exists()]
        if missing:
            print("нет таких файлов:\n  " + "\n  ".join(missing), file=sys.stderr)
            return 2
        print(f"llama-server: {args.server}   threads={args.threads} n_predict={args.n_predict}")
        for k, g in enumerate(ggufs):
            results.append(bench_one(args.server, g, args.port + k, args.threads,
                                     args.ctx, args.n_predict, args.timeout, dataset))

    w = 92
    print("\n" + "=" * w)
    print(f"{'подход':<26}{'valid':>7}{'exact':>7}{'±1':>6}"
          f"{'ВРЕД':>7}{'flip':>6}{'alarm':>7}{'miss':>6}{'вызов мс':>11}")
    print("-" * w)
    for r in results:
        if "error" in r:
            print(f"{r['model']:<26}ОШИБКА: {r['error']}")
            continue
        h = r["harm_total"]
        print(f"{r['model']:<26}"
              f"{r['valid_json_rate']*100:>6.0f}%"
              f"{r['exact_rate']*100:>6.0f}%"
              f"{r['close_rate']*100:>5.0f}%"
              f"{r['harmful_row_rate']*100:>6.0f}%"
              f"{h['sign_flip']:>6}{h['false_alarm']:>7}{h['miss']:>6}"
              f"{(r['wall_ms']['mean'] or 0):>11.0f}")
    print("=" * w)
    print("ВРЕД — доля сообщений, где сенсор ВЫДАЛ сильный сигнал не в ту сторону\n"
          "(flip: знак valence/warmth противоположен; alarm: выдуман threat/loss/agency).\n"
          "miss (промах в ноль) вредом НЕ считается — ядро просто не двигается.\n"
          "Для L-1 важно именно это: сенсор, чей отказ = нули, должен и ошибаться в нули.")

    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"\nполные результаты: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
