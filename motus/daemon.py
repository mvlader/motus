"""motusd — демон, единственный владелец состояния.

Держит вектор в памяти, тикает по таймеру, пишет журнал и снапшот.

Почему демон, а не библиотека: у состояния должен быть ровно один владелец.
openclaw перезапускается, каждая сессия — отдельный процесс; N копий вектора
разойдутся, и кто последний записал — тот и прав. Плюс тикать нужно и когда
сессий нет вообще, иначе время между разговорами для системы не существует.

Два слушателя (см. docs/04-model-l1.md, «Изоляция»):
  * ПУБЛИЧНЫЙ — то, что нужно openclaw, и только оно: карточка (без единого числа),
    события, задачи, консумация, refund, health. Ни `/state/raw`, ни журнала.
  * АДМИН — всё, включая сырые числа. Unix-сокет, доступен только владельцу
    процесса (mvlader/cron внутри контейнера `motus`), не проброшен наружу.
Если ни --pub-port, ни --admin-socket не заданы — один общий TCP (--host/--port),
режим для разработки и тестов.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from . import __version__, appraisal, config, curator
from .clock import Clock
from .engine import Engine
from .events import Event
from .journal import Journal
from .state import State

DEFAULT_PORT = 18790  # у openclaw gateway 18789 — не конфликтуем

#: Что видит openclaw. Всё остальное — только на админ-сокете.
PUBLIC_PATHS = frozenset((
    "/health", "/state/card", "/event", "/task/next", "/consummation",
    "/refund", "/llm_call", "/initiate/pending",
))

#: Tier 2: не отдавать "pending", если тишина короче этого порога — даже если
#: движок уже решил "initiate" внутри тика, вызванного ОБЫЧНЫМ /state/card
#: посреди живого разговора (плагин дёргает его на каждый ход). Настоящая
#: проактивная инициация — это когда пользователя не было какое-то время, а
#: не артефакт того, что кто-то просто спросил карточку. Исполнитель Tier 2
#: опрашивает этот путь по таймеру, не по каждому ходу, так что отдельная
#: живая инициация внутри разговора (если когда-нибудь понадобится) должна
#: идти другим путём — не через этот эндпоинт.
INITIATE_MIN_SILENCE_S = 300.0


class Service:
    def __init__(self, cfg: Dict[str, Any], var_dir: str) -> None:
        self.cfg = cfg
        self.var_dir = var_dir
        os.makedirs(var_dir, exist_ok=True)
        self.state_path = os.path.join(var_dir, "state.json")
        self.repertoire_path = os.path.join(var_dir, "repertoire.json")
        self.journal = Journal(os.path.join(var_dir, "journal"))
        self.clock = Clock()
        self.lock = threading.Lock()
        # Отдельный лок для оценки текста (L-1): сама она идёт ВНЕ self.lock
        # (см. /event), но модельный сенсор — один на процесс, параллельные
        # вызовы к нему были бы гонкой за ap.last_failed и лишней нагрузкой на
        # тот же локальный/LAN-инстанс модели.
        self.appraise_lock = threading.Lock()
        st = self._load_state()
        sensor = None
        if cfg.get("appraisal", {}).get("mode") == "model":
            sensor = appraisal.make_sensor(cfg["appraisal"])
            self._warm_up_sensor(sensor, self.journal)
        # sensor=None → Engine берёт appraisal.mode из конфига (lexical|off).
        self.engine = Engine(cfg, self.clock, self.journal, st, sensor=sensor)
        self._load_repertoire()  # поверх бутстрап-дефолта из config/repertoire.json
        self.curator_sensor = None
        if cfg.get("curation", {}).get("enabled"):
            self.curator_sensor = curator.make_sensor(cfg["curation"])
        self.started = time.time()
        self.next_tick_s = float(cfg["heartbeat"]["tick_min_s"])
        self._stop = threading.Event()

    @staticmethod
    def _warm_up_sensor(sensor: Any, journal: Journal) -> None:
        """Фоновый прогрев L-1: первый вызов llama-server/ollama грузит веса и
        считает весь few-shot промпт без кэша — секунды-десятки секунд, дольше
        любого timeout_s. Прогнать один холостой запрос в отдельном потоке, чтобы
        к первому реальному сообщению сервер был горячим. Best-effort: отдельные
        неудачи не мешают появлению — сбой сенсора appraise_text переживёт и без
        прогрева (нули или словарный fallback). Раньше итог прогрева нигде не
        отмечался — 6 неудач подряд (например, ПК выключен) проходили тихо, и
        единственным способом узнать было ждать первую appraisal_invalid на
        живом сообщении. Теперь полный отказ (все 6 попыток) пишется в журнал
        как error — тем же kind, что и другие сбои демона (tick_failed,
        corrupt_state_snapshot). Успех не логируется — как и успешная загрузка
        state.json, это штатный путь."""
        def run() -> None:
            for attempt in range(1, 7):
                try:
                    sensor("прогрев")
                    return
                except Exception as exc:
                    if attempt == 6:
                        journal.write("error", time.time(), {
                            "what": "sensor_warmup_failed", "attempts": attempt,
                            "error": repr(exc),
                        })
                    time.sleep(10)
        threading.Thread(target=run, name="l1-warmup", daemon=True).start()

    def _load_state(self) -> Optional[State]:
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError:
            return None
        try:
            st = State.from_dict(json.loads(raw))
        except (ValueError, TypeError) as exc:
            # Битый или NaN-отравленный снапшот. Стартуем с чистого состояния,
            # но громко: молча потерять накопленное состояние тоже плохо.
            self.journal.write("error", time.time(),
                               {"what": "corrupt_state_snapshot", "detail": str(exc)})
            try:
                os.replace(self.state_path, self.state_path + ".corrupt")
            except OSError:
                pass
            return None
        # Простой снапшота не отменяет времени: до текущего момента состояние
        # доводится обычной релаксацией на первом же тике.
        return st

    def save_state(self) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.engine.state.to_dict(), fh, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    # --------------------------------------------------------- репертуар (ночь)

    def _load_repertoire(self) -> None:
        """Поверх бутстрап-дефолта из config/repertoire.json (уже в
        self.engine.rep.data) — если есть эволюционировавший снапшот на диске
        (правки прошлых ночей), он и есть источник правды. Тот же принцип, что
        у _load_state: битый файл — громко в журнал, старт с бутстрапа, не молча."""
        try:
            with open(self.repertoire_path, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError:
            return
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or "templates" not in data:
                raise ValueError("нет ключа templates")
        except ValueError as exc:
            self.journal.write("error", time.time(),
                               {"what": "corrupt_repertoire_snapshot", "detail": str(exc)})
            try:
                os.replace(self.repertoire_path, self.repertoire_path + ".corrupt")
            except OSError:
                pass
            return
        self.engine.rep.data = data

    def save_repertoire(self) -> None:
        self.engine.rep.save(self.repertoire_path)

    def run_curation(self, efficacy: Any) -> None:
        """Шаг 2 ночного цикла (docs/01-structure.md §7). Вызывается только из
        run_ticker после успешного (не отложенного) сна. Отказ сенсора —
        curator.propose_edits уже ловит и отдаёт [] — цикл просто ничего не
        меняет, никакого шума."""
        edits = curator.propose_edits(self.curator_sensor, efficacy)
        if not edits:
            self.journal.write("curation", self.engine.state.t,
                               {"proposed": 0, "applied": [], "rejected": []})
            return
        result = self.engine.rep.apply_edits(edits)
        self.journal.write("curation", self.engine.state.t,
                           {"proposed": len(edits), **result})
        if result["applied"]:
            self.save_repertoire()

    # ------------------------------------------------------------- фоновый цикл

    def run_ticker(self) -> None:
        while not self._stop.wait(self.next_tick_s):
            try:
                with self.lock:
                    d = self.engine.tick()
                    self.next_tick_s = d.next_tick_s
                    rep = self.engine.maybe_sleep()
                    if rep.ran and self.curator_sensor is not None:
                        self.run_curation(rep.efficacy)
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

    @property
    def _public(self) -> bool:
        """True на слушателе, который смотрит наружу к openclaw."""
        return getattr(self.server, "public", False)

    def _blocked(self, path: str) -> bool:
        if self._public and path not in PUBLIC_PATHS:
            self._send(404, {"error": "not found"})
            return True
        return False

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

    # -------------------------------------------------------- обёртка/логирование

    def do_GET(self) -> None:
        self._guarded(self._handle_get)

    def do_POST(self) -> None:
        self._guarded(self._handle_post)

    def _guarded(self, handler) -> None:
        """Любая необработанная ошибка внутри ветки эндпоинта раньше просто
        валила соединение (http.server печатает трейсбек в stderr, но это не
        попадает в структурный журнал var/journal/*.jsonl — только сырой
        stderr, который никто не парсит). Теперь — 500 клиенту и запись в
        журнал с путём и текстом ошибки, тем же способом, что tick_failed
        в run_ticker."""
        try:
            handler()
        except Exception as exc:
            svc = self.service
            if svc is not None:
                try:
                    svc.journal.write("error", time.time(), {
                        "what": "handler_exception",
                        "path": self.path.split("?")[0],
                        "method": self.command,
                        "error": repr(exc),
                    })
                except Exception:
                    pass  # журнал сам не пишется — не молчать полностью, но и не падать вдвойне
            try:
                self._send(500, {"error": "internal error"})
            except Exception:
                pass  # соединение могло уже порваться — второй сбой здесь не спасти

    # ------------------------------------------------------------------- GET

    def _handle_get(self) -> None:
        svc = self.service
        path = self.path.split("?")[0]
        if self._blocked(path):
            return
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
            gate = d.gate.to_dict()
            if self._public:
                # Наружу — только текст карточки и маска полномочий. Ни activation,
                # ни somatic-флагов: это числа, им в промпте делать нечего.
                gate = {k: gate[k] for k in ("regime", "may_initiate", "max_tokens",
                                             "allowed_tools", "forbidden", "context_band")}
            return self._send(200, {"card": d.card.to_dict(), "gate": gate,
                                    "tier": d.tier})
        if path == "/state/raw":
            # Только отладка и дашборд. Эти числа не должны попадать в промпт.
            with svc.lock:
                return self._send(200, svc.engine.state.to_dict())
        if path == "/task/next":
            with svc.lock:
                d = svc.engine.tick()
                svc.next_tick_s = d.next_tick_s
                # d.task — то, что решилось именно в этом тике; но задачи чаще
                # кладёт в очередь независимый фоновый цикл (run_ticker) между
                # опросами исполнителя. peek() отдаёт то, что уже лежит там и
                # ждёт (см. repertoire.Repertoire.peek).
                task = d.task or svc.engine.rep.peek(svc.engine.state.t, svc.engine.state)
                return self._send(200, {"task": task.to_dict() if task else None,
                                        "tier": d.tier})
        if path == "/initiate/pending":
            # Tier 2: только чтение, ничего не тикает и не спишет — движок уже
            # решил (или нет) внутри обычного тика; здесь лишь докладываем.
            with svc.lock:
                st = svc.engine.state
                if not st.initiation_pending:
                    return self._send(200, {"pending": False})
                silence_s = svc.clock.now() - st.last_contact_t
                if silence_s < INITIATE_MIN_SILENCE_S:
                    # Инициация решена внутри тика, вызванного живым ходом
                    # (например, /state/card из before_prompt_build) — не
                    # выдаём её как повод писать поверх активного разговора.
                    return self._send(200, {"pending": False})
                gate = svc.engine.gk.evaluate(st)
                card = svc.engine.vb.render(st, gate)
                return self._send(200, {"pending": True, "card": card.to_dict(),
                                        "gate": {"regime": gate.regime,
                                                 "max_tokens": gate.max_tokens,
                                                 "forbidden": list(gate.forbidden)}})
        if path == "/journal/tail":
            n = int(query.get("n", 50))
            return self._send(200, {"records": svc.journal.tail(min(n, 500))})
        return self._send(404, {"error": "not found"})

    # ------------------------------------------------------------------ POST

    def _handle_post(self) -> None:
        svc = self.service
        path = self.path.split("?")[0]
        if self._blocked(path):
            return
        body = self._body()

        if path == "/event":
            kind = body.get("kind")
            if not kind:
                return self._send(400, {"error": "нужно поле kind"})
            payload = dict(body.get("payload", {}))
            if kind == "user_message" and "text" in payload and "appraisal" not in payload:
                # Сетевой вызов модели (L-1) — вне svc.lock и вне времени, за
                # которое клиент (плагин openclaw) готов ждать: иначе он на
                # каждом ходе блокирует /state/card и другие эндпоинты на всё
                # то же время, что уходит на инференс (docs/04-model-l1.md,
                # «Латентность»). appraise_lock только сериализует сами вызовы
                # к модели между собой — общего состояния не трогает.
                text = payload.pop("text")
                with svc.appraise_lock:
                    payload["appraisal"] = svc.engine.appraise_text_now(text)
            with svc.lock:
                imps = svc.engine.submit_event(
                    Event(kind=kind, t=svc.clock.now(), payload=payload)
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


class _TCPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, addr, handler, public: bool):
        self.public = public
        super().__init__(addr, handler)


class _UnixServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX

    def __init__(self, path: str, handler, public: bool, mode: int):
        self.public = public
        self._path = path
        self._mode = mode
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        super().__init__(path, handler)

    def server_bind(self):
        # http.server.HTTPServer.server_bind лезет в server_address[:2] как в
        # (host, port) — для пути это мусор. Делаем ручной эквивалент.
        self.socket.bind(self._path)
        os.chmod(self._path, self._mode)
        self.server_name = "motusd"
        self.server_port = 0

    def server_close(self):
        super().server_close()
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="motusd", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=config.CONFIG_DIR)
    ap.add_argument("--var", default=os.path.join(config.ROOT, "var"))
    ap.add_argument("--pub-port", type=int, default=None,
                    help="TCP-порт публичного слушателя (только PUBLIC_PATHS); "
                         "проксируется в grach")
    ap.add_argument("--pub-host", default="0.0.0.0")
    ap.add_argument("--admin-socket", default=None,
                    help="unix-сокет полного API (сырые числа, журнал, tick/sleep) — "
                         "локальный для контейнера motus, наружу не проброшен")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="общий TCP-слушатель со ВСЕМИ эндпоинтами — только если не "
                         "заданы --pub-port/--admin-socket (разработка, тесты)")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)

    cfg = config.load(args.config)
    svc = Service(cfg, args.var)
    Handler.service = svc
    threading.Thread(target=svc.run_ticker, daemon=True).start()

    servers = []
    if args.pub_port is not None or args.admin_socket is not None:
        if args.pub_port is not None:
            servers.append(_TCPServer((args.pub_host, args.pub_port), Handler, public=True))
            print(f"motusd {__version__}: публичный tcp://{args.pub_host}:{args.pub_port}")
        if args.admin_socket is not None:
            servers.append(_UnixServer(args.admin_socket, Handler, public=False, mode=0o600))
            print(f"motusd {__version__}: админ unix:{args.admin_socket}")
    else:
        servers.append(_TCPServer((args.host, args.port), Handler, public=False))
        print(f"motusd {__version__}: общий tcp://{args.host}:{args.port} (var={args.var})")

    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()

    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    try:
        done.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()   # _UnixServer.server_close снимает файл сокета
        svc.stop()
        svc.save_state()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
