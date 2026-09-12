"""Append-only JSONL. Одна запись на событие, со seq, временем и снимком вектора.

Из журнала событий состояние восстанавливается побитово (replay.py). Это
одновременно регрессионный тест и единственный способ ответить на вопрос
«почему она это сказала».
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Iterator, List, Optional

KINDS = (
    "boot", "tick", "event", "impulse", "gate_change", "card",
    "llm_call", "task", "consummation", "sleep", "curation",
    "appraisal_invalid", "error",
)


class Journal:
    def __init__(self, directory: str) -> None:
        self.dir = directory
        os.makedirs(self.dir, exist_ok=True)
        self._seq_path = os.path.join(self.dir, "seq")
        self._seq = self._read_seq()
        # write() зовётся и из-под общего svc.lock демона, и (для appraisal_invalid)
        # из вызова модели, который теперь намеренно идёт ВНЕ svc.lock (см.
        # daemon.py /event) — без этого лока инкремент self._seq гонится между
        # потоками и портит порядок/уникальность seq, на которых держится реплей.
        self._lock = threading.Lock()

    def _read_seq(self) -> int:
        try:
            with open(self._seq_path, "r", encoding="utf-8") as fh:
                return int(fh.read().strip() or 0)
        except (OSError, ValueError):
            return 0

    def _path_for(self, t: float) -> str:
        day = time.strftime("%Y-%m-%d", time.localtime(t))
        return os.path.join(self.dir, f"{day}.jsonl")

    def write(
        self,
        kind: str,
        t: float,
        payload: Optional[Dict[str, Any]] = None,
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> int:
        if kind not in KINDS:
            raise ValueError(f"неизвестный вид записи журнала: {kind}")
        with self._lock:
            return self._write_locked(kind, t, payload, snapshot)

    def _write_locked(
        self,
        kind: str,
        t: float,
        payload: Optional[Dict[str, Any]],
        snapshot: Optional[Dict[str, Any]],
    ) -> int:
        self._seq += 1
        rec = {
            "seq": self._seq,
            # Без округления: реплей должен попадать в те же моменты времени.
            "t": t,
            "kind": kind,
            "payload": payload or {},
        }
        if snapshot is not None:
            rec["state"] = snapshot
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        with open(self._path_for(t), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        with open(self._seq_path, "w", encoding="utf-8") as fh:
            fh.write(str(self._seq))
        return self._seq

    # ------------------------------------------------------------- чтение

    def files(self) -> List[str]:
        return sorted(
            os.path.join(self.dir, f)
            for f in os.listdir(self.dir)
            if f.endswith(".jsonl")
        )

    def read_all(self) -> Iterator[Dict[str, Any]]:
        for path in self.files():
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        yield json.loads(line)

    def tail(self, n: int = 50) -> List[Dict[str, Any]]:
        recs: List[Dict[str, Any]] = []
        for path in reversed(self.files()):
            with open(path, "r", encoding="utf-8") as fh:
                lines = [ln for ln in fh.read().splitlines() if ln.strip()]
            recs = [json.loads(ln) for ln in lines[-n:]] + recs
            if len(recs) >= n:
                break
        return recs[-n:]


class NullJournal(Journal):
    """Для тестов и реплея: ничего не пишет, но считает seq."""

    def __init__(self) -> None:  # noqa: D107  (namesake init намеренно не вызываем)
        self.dir = ""
        self._seq = 0

    def write(self, kind, t, payload=None, snapshot=None) -> int:  # type: ignore[override]
        self._seq += 1
        return self._seq

    def files(self):  # type: ignore[override]
        return []

    def read_all(self):  # type: ignore[override]
        return iter(())

    def tail(self, n: int = 50):  # type: ignore[override]
        return []
