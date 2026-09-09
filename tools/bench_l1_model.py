#!/usr/bin/env python3
"""Сравнить кандидатов на роль L-1 (оценщик сигнала) прямо на целевом железе.

Меряет то, что реально важно для L-1: задержку и ток/сек на честных цифрах от
самой ollama (`eval_count`/`eval_duration`, а не секундомер снаружи — в него
подмешивается сеть и время загрузки модели), и заодно — отвечает ли модель
вообще валидным JSON по нашей схеме, потому что бесполезно сравнивать скорость
моделей, одна из которых половину времени возвращает мусор.

Запускать НА ЦЕЛЕВОЙ МАШИНЕ (там, где будет жить motusd), не отсюда — я не
дотягиваюсь по сети до Raspberry Pi этого проекта.

    python3 tools/bench_l1_model.py
    python3 tools/bench_l1_model.py --models qwen3:0.6b-q4_K_M gemma3:270m
    python3 tools/bench_l1_model.py --host http://127.0.0.1:11435 --n 8 --out bench.json

Перед запуском модели должны быть скачаны:
    ollama pull qwen3:0.6b-q4_K_M
    ollama pull gemma3:270m
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from motus.appraisal import SENSOR_PROMPT  # noqa: E402
from motus.events import Appraisal  # noqa: E402

#: Восемь сообщений, специально подобранных так, чтобы задеть разные поля схемы
#: (угроза, новизна, потеря, тепло/холод, блокировка) — не для измерения точности
#: (для этого нужна размеченная выборка, отдельная задача), а чтобы увидеть
#: живьём, не сыплется ли модель в нули или в очевидно неверные значения.
SAMPLES = [
    "Обнови зависимости в проекте до последней версии.",
    "Ты мне очень помог, наконец-то всё заработало, огромное спасибо!",
    "Внимание, диск почти заполнен, надо срочно освободить место!",
    "Опять не получилось запустить тесты, падают на одном и том же месте третий раз.",
    "Я уезжаю на две недели в командировку, буду редко на связи.",
    "Смотри, нашёл классную библиотеку, никогда о такой раньше не слышал.",
    "Ладно, неважно, забудь.",
    "Доброе утро! Как спалось?",
]


def call_ollama(base_url: str, model: str, text: str, timeout_s: float,
                 num_predict: int) -> Dict[str, Any]:
    body = json.dumps({
        "model": model,
        "prompt": SENSOR_PROMPT + text,
        "format": Appraisal.json_schema(),
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": num_predict},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/generate", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    wall_ms = (time.monotonic() - t0) * 1000.0
    raw["_wall_ms"] = wall_ms
    return raw


def bench_one(base_url: str, model: str, n: int, timeout_s: float,
              num_predict: int) -> Dict[str, Any]:
    print(f"\n== {model} ==")
    # Прогрев: первый вызов ollama грузит веса в память — секунды, не миллисекунды.
    # Не в счёт, иначе он один перевесит всю остальную статистику.
    try:
        print("  прогрев...", end=" ", flush=True)
        call_ollama(base_url, model, SAMPLES[0], max(timeout_s, 60.0), num_predict)
        print("готово")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"ОШИБКА: {exc}")
        return {"model": model, "error": str(exc)}

    wall_ms: List[float] = []
    tok_s: List[float] = []
    valid = 0
    samples_out = []

    for i in range(n):
        text = SAMPLES[i % len(SAMPLES)]
        try:
            raw = call_ollama(base_url, model, text, timeout_s, num_predict)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"  [{i+1}/{n}] сбой: {exc}")
            continue

        wall_ms.append(raw["_wall_ms"])
        eval_count = raw.get("eval_count", 0)
        eval_ns = raw.get("eval_duration", 0)
        if eval_count and eval_ns:
            tok_s.append(eval_count / (eval_ns / 1e9))

        parsed = Appraisal.parse(_safe_json(raw.get("response", "")))
        is_valid = raw.get("response") is not None and _safe_json(raw["response"]) is not None
        if is_valid:
            valid += 1
        samples_out.append({"text": text, "response": raw.get("response"),
                            "parsed": parsed.__dict__})
        print(f"  [{i+1}/{n}] {wall_ms[-1]:6.0f} мс  {text[:40]!r:42s} → {raw.get('response')}")

    result = {
        "model": model,
        "n_ok": len(wall_ms),
        "n_requested": n,
        "valid_json_rate": valid / n if n else 0.0,
        "wall_ms": _stats(wall_ms),
        "tokens_per_s": _stats(tok_s),
        "samples": samples_out,
    }
    return result


def _safe_json(s: Optional[str]):
    if not s:
        return None
    try:
        return json.loads(s)
    except ValueError:
        return None


def _stats(xs: List[float]) -> Dict[str, Optional[float]]:
    if not xs:
        return {"min": None, "mean": None, "max": None}
    return {"min": round(min(xs), 1), "mean": round(statistics.mean(xs), 1),
            "max": round(max(xs), 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="http://127.0.0.1:11435",
                    help="адрес ollama на ЭТОЙ машине (не host.docker.internal — "
                         "этот скрипт запускается нативно, не в контейнере)")
    ap.add_argument("--models", nargs="+",
                    default=["qwen3:0.6b-q4_K_M", "gemma3:270m"])
    ap.add_argument("--n", type=int, default=8, help="запросов на модель")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--num-predict", type=int, default=80)
    ap.add_argument("--out", default=None, help="сохранить сырые результаты в JSON")
    args = ap.parse_args()

    print(f"ollama: {args.host}")
    print(f"модели: {', '.join(args.models)}")
    print(f"(если модель не скачана: ollama pull <имя>)")

    results = [bench_one(args.host, m, args.n, args.timeout, args.num_predict)
               for m in args.models]

    print("\n" + "=" * 78)
    print(f"{'модель':<22}{'ток/сек (mean)':<18}{'задержка мс (mean)':<20}{'валидный JSON':<15}")
    print("-" * 78)
    for r in results:
        if "error" in r:
            print(f"{r['model']:<22}ОШИБКА: {r['error']}")
            continue
        ts = r["tokens_per_s"]["mean"]
        wm = r["wall_ms"]["mean"]
        print(f"{r['model']:<22}{ts if ts else '—':<18}{wm if wm else '—':<20}"
              f"{r['valid_json_rate']*100:.0f}%")
    print("=" * 78)
    print("Смотри и на сами ответы выше, не только на скорость: если модель быстрая,\n"
          "но валидный JSON реже 90% или значения выглядят наугад — она не годится,\n"
          "как бы быстро ни отвечала.")

    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"\nсырые результаты: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
