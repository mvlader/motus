"""motusd — демон, единственный владелец состояния.

Держит вектор в памяти, тикает по таймеру, пишет журнал и снапшот. Всё остальное
(openclaw, cron, дашборд) обращается к нему по HTTP на localhost.

Почему демон, а не библиотека: у состояния должен быть ровно один владелец.
openclaw живёт в Docker, перезапускается, каждая сессия — отдельный процесс; N копий
вектора разойдутся, и кто последний записал — тот и прав. Плюс тикать нужно и когда
сессий нет вообще, иначе время между разговорами для системы не существует.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from . import __version__, appraisal, config
from .clock import Clock
from .engine import Engine
from .events import Event
from .journal import Journal
from .state import State

DEFAULT_PORT = 18790  # у openclaw gateway 18789 — не конфликтуем


class Service:
    def __init__(self, cfg: Dict[str, Any], var_dir: str) -> None:
        self.cfg = cfg
        self.var_dir = var_dir
        os.makedirs(var_dir, exist_ok=True)
        self.state_path = os.path.join(var_dir, "state.json")
        self.journal = Journal(os.path.join(var_dir, "journal"))
        self.clock = Clock()
        self.lock = threading.Lock()
        st = self._load_state()
        sensor = None
        if cfg.get("appraisal", {}).get("enabled"):
            sensor = appraisal.ollama_sensor(cfg["appraisal"])
        self.engine = Engine(cfg, self.clock, self.journal, st, sensor=sensor)
        self.started = time.time()
        self.next_tick_s = float(cfg["heartbeat"]["tick_min_s"])
        self._stop = threading.Event()

    def _load_state(self) -> Optional[State]:
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                st = State.from_dict(json.load(fh))
        except (OSError, ValueError, TypeError):
            return None
        # Простой снапшота не отменяет времени: до текущего момента состояние
        # доводится обычной релаксацией на первом же тике.
        return st

    def save_state(self) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.engine.state.to_dict(), fh, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------- фоновый цикл

    def run_ticker(self) -> None:
        while not self._stop.wait(self.next_tick_s):
            try:
                with self.lock:
                    d = self.engine.tick()
                    self.next_tick_s = d.next_tick_s
                    self.engine.maybe_sleep()
                    self.save_state()
            except Exception as exc:  # демон не имеет права умирать от одного тика
                self.journal.write("error", time.time(), {"what": "tick_failed",
                                                          "error": repr(exc)})

    def stop(self) -> None:
        self._stop.set()


class Handler(BaseHTTPRequestHandler):
    service: Service = None  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # тише в journalctl
        pass

    def _send(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    # ------------------------------------------------------------------- GET

    def do_GET(self) -> None:
        svc = self.service
        path = self.path.split("?")[0]
        query = dict(
            kv.split("=", 1) for kv in self.path.split("?")[1].split("&") if "=" in kv
        ) if "?" in self.path else {}

        if path == "/health":
            return self._send(200, {"ok": True, "version": __version__,
                                    "uptime_s": round(time.time() - svc.started, 1),
                                    "seq": svc.engine.state.seq})
        if path == "/state/card":
            with svc.lock:
                d = svc.engine.tick()
                svc.next_tick_s = d.next_tick_s
                svc.save_state()
            return self._send(200, {"card": d.card.to_dict(), "gate": d.gate.to_dict(),
                                    "tier": d.tier})
        if path == "/state/raw":
            # Только отладка и дашборд. Эти числа не должны попадать в промпт.
            with svc.lock:
                return self._send(200, svc.engine.state.to_dict())
        if path == "/task/next":
            with svc.lock:
                d = svc.engine.tick()
                return self._send(200, {"task": d.task.to_dict() if d.task else None,
                                        "tier": d.tier})
        if path == "/journal/tail":
            n = int(query.get("n", 50))
            return self._send(200, {"records": svc.journal.tail(min(n, 500))})
        return self._send(404, {"error": "not found"})

    # ------------------------------------------------------------------ POST

    def do_POST(self) -> None:
        svc = self.service
        path = self.path.split("?")[0]
        body = self._body()

        if path == "/event":
            kind = body.get("kind")
            if not kind:
                return self._send(400, {"error": "нужно поле kind"})
            with svc.lock:
                imps = svc.engine.submit_event(
                    Event(kind=kind, t=svc.clock.now(), payload=body.get("payload", {}))
                )
                svc.save_state()
            return self._send(200, {"applied": [i.to_dict() for i in imps]})

        if path == "/consummation":
            tid, verified = body.get("template_id"), bool(body.get("verified"))
            if not tid:
                return self._send(400, {"error": "нужно поле template_id"})
            with svc.lock:
                delta = svc.engine.consummate(tid, verified, float(body.get("cost", 0.0)))
                svc.save_state()
            return self._send(200, {"delta": round(delta, 5)})

        if path == "/tick":
            with svc.lock:
                d = svc.engine.tick()
                svc.next_tick_s = d.next_tick_s
                svc.save_state()
            return self._send(200, d.to_dict())

        if path == "/sleep":
            with svc.lock:
                rep = svc.engine.maybe_sleep(force=bool(body.get("force")))
                svc.save_state()
            return self._send(200, {"ran": rep.ran, "reason": rep.reason,
                                    "efficacy": rep.efficacy})

        if path == "/llm_call":
            with svc.lock:
                svc.engine.note_llm_call(
                    body.get("model", "?"), body.get("purpose", "?"),
                    int(body.get("tokens_in", 0)), int(body.get("tokens_out", 0)),
                    body.get("template_id", ""),
                )
            return self._send(200, {"ok": True})

        if path == "/refund":
            with svc.lock:
                svc.engine.refund_initiation()
                svc.save_state()
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "not found"})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="motusd")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--config", default=config.CONFIG_DIR)
    ap.add_argument("--var", default=os.path.join(config.ROOT, "var"))
    args = ap.parse_args(argv)

    cfg = config.load(args.config)
    svc = Service(cfg, args.var)
    Handler.service = svc

    ticker = threading.Thread(target=svc.run_ticker, daemon=True)
    ticker.start()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"motusd {__version__} на http://{args.host}:{args.port} (var={args.var})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        svc.stop()
        svc.save_state()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
