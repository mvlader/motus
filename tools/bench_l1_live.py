#!/usr/bin/env python3
"""Живое сравнение кандидатов L-1 без переустановки инфраструктуры.

Переиспользует датасет и скоринг из `tools/bench_l1_model.py` (GOLD/HARD,
`_harm`, `aggregate`), добавляет только то, чего там нет: кандидата через
`ollama_sensor` (модель на ПК по LAN, без локального llama-server). Ничего
не пишет в конфиг MOTUS и не трогает боевой `motusd` — чистое измерение.

Пример:
    python3 tools/bench_l1_live.py \\
        --server /opt/llama.cpp/bin/llama-server \\
        --models-dir /mnt/torrents/llama-models --gguf Qwen3-1.7B-Q4_K_M.gguf \\
        --ollama-url http://192.168.2.27:11434 --ollama-model ge4b-heretic:latest \\
        --lexical --dataset all
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from motus.appraisal import ollama_sensor  # noqa: E402

_spec = importlib.util.spec_from_file_location("bench_l1_model", ROOT / "tools" / "bench_l1_model.py")
bl1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bl1)  # даёт GOLD/HARD/DATASETS/aggregate/bench_lexical/bench_one/start_server/stop_server


def bench_ollama(name: str, base_url: str, model: str, dataset: List[Dict[str, Any]],
                 timeout_s: float, num_predict: int) -> Dict[str, Any]:
    """Кандидат без локального сервера: зовём уже поднятую ollama по LAN."""
    sensor = ollama_sensor({"base_url": base_url, "model": model,
                            "timeout_s": timeout_s, "num_predict": num_predict,
                            "temperature": 0.0})
    print(f"\n== {name} ({base_url}, {model}) ==")
    rows: List[Dict[str, Any]] = []
    wall: List[float] = []
    for i, item in enumerate(dataset):
        t0 = time.monotonic()
        try:
            raw = sensor(item["text"])
        except Exception as exc:  # сеть, таймаут, не-200 — то же, что ловит Appraiser
            print(f"  [{i+1:2}/{len(dataset)}] сбой: {exc}")
            rows.append({"text": item["text"], "expect": item["expect"], "pred": None})
            continue
        wall.append((time.monotonic() - t0) * 1000.0)
        pred = raw if isinstance(raw, dict) else None
        h = bl1._harm(pred, item)
        mark = "!" if (h["sign_flip"] or h["false_alarm"]) else " "
        print(f"  [{i+1:2}/{len(dataset)}] {mark} {wall[-1]:6.0f}мс "
              f"{item['text'][:36]!r:38s} → {json_dump(pred)}")
        rows.append({"text": item["text"], "expect": item["expect"], "pred": pred,
                     "note": item.get("note", "")})
    return bl1.aggregate(name, rows, {"wall_ms": wall})


def json_dump(pred):
    import json
    return json.dumps(pred, ensure_ascii=False)


def print_table(results: List[Dict[str, Any]]) -> None:
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="/opt/llama.cpp/bin/llama-server")
    ap.add_argument("--models-dir", default="/mnt/torrents/llama-models")
    ap.add_argument("--gguf", nargs="*", default=["Qwen3-1.7B-Q4_K_M.gguf"])
    ap.add_argument("--port", type=int, default=8085)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--ctx", type=int, default=2048)
    ap.add_argument("--n-predict", type=int, default=96)
    ap.add_argument("--timeout", type=float, default=40.0)
    ap.add_argument("--ollama-url", default=None, help="напр. http://192.168.2.27:11434")
    ap.add_argument("--ollama-model", default=None, help="напр. ge4b-heretic:latest")
    ap.add_argument("--ollama-timeout", type=float, default=60.0)
    ap.add_argument("--lexical", action="store_true")
    ap.add_argument("--strict-lexical", action="store_true")
    ap.add_argument("--dataset", choices=("gold", "hard", "all"), default="all")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    dataset = bl1.DATASETS[args.dataset]
    print(f"выборка: {args.dataset} ({len(dataset)} сообщений)")

    results = []
    if args.lexical or args.strict_lexical:
        results.append(bl1.bench_lexical(dataset, strict=False))
    if args.strict_lexical:
        results.append(bl1.bench_lexical(dataset, strict=True))

    if args.gguf:
        mdir = Path(args.models_dir)
        for k, g in enumerate(args.gguf):
            gp = Path(g) if Path(g).is_absolute() else mdir / g
            if not gp.exists():
                print(f"нет файла: {gp}", file=sys.stderr)
                continue
            results.append(bl1.bench_one(args.server, gp, args.port + k, args.threads,
                                         args.ctx, args.n_predict, args.timeout, dataset))

    if args.ollama_url and args.ollama_model:
        results.append(bench_ollama(f"ollama/{args.ollama_model}", args.ollama_url,
                                    args.ollama_model, dataset, args.ollama_timeout,
                                    args.n_predict))

    print_table(results)
    if args.out:
        import json
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"\nполные результаты: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
