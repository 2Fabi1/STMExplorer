#!/usr/bin/env python3
"""
STM Stats server: serves the site and refreshes the data every 24 hours.

    python server.py                         # http://127.0.0.1:8080, refresh every 24 h
    python server.py --host 0.0.0.0 --port 80   # reachable from other machines
    python server.py --no-update-on-start    # don't refresh at startup even if the data is stale

Only these URLs are served (nothing else on disk is exposed):
    /              the site (data/index.html)
    /events.json   every detected new STM
    /status        JSON: last update, next update, whether a refresh is running

The refresh itself is update.py; run `python update.py` yourself (cron / Task Scheduler) if you
would rather not keep this process alive.
"""
import argparse
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler

import update

log = logging.getLogger("stm")
RETRY_AFTER_FAILURE_H = 1.0     # a failed refresh is retried after this long instead of waiting a day


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


class State:
    """Shared between the scheduler thread and the HTTP handlers."""
    next_update: datetime | None = None


def scheduler(interval: timedelta, update_on_start: bool, stop: threading.Event) -> None:
    meta = update.ensure_state()
    last = parse_ts(meta.get("updated"))
    now = datetime.now(timezone.utc)
    if update_on_start:
        due = last + interval if last else now      # stale (or never updated) data is due immediately
    else:
        due = now + interval
    while not stop.is_set():
        State.next_update = due
        wait = (due - datetime.now(timezone.utc)).total_seconds()
        if wait > 0 and stop.wait(min(wait, 3600)):     # wake hourly so a changed clock can't strand us
            return
        if datetime.now(timezone.utc) < due:
            continue
        started = datetime.now(timezone.utc)
        try:
            summary = update.run_update()
            ok = summary is not None and not summary["failed"]
        except Exception:
            log.exception("Update crashed")
            ok = False
        due = started + (interval if ok else timedelta(hours=RETRY_AFTER_FAILURE_H))
        if not ok:
            log.warning("Update incomplete; retrying at %s", due.isoformat(timespec="seconds"))


class Handler(BaseHTTPRequestHandler):
    server_version = "STMStats"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            f = update.DATA / "index.html"
            if f.exists():
                return self._send(200, f.read_bytes(), "text/html; charset=utf-8")
            return self._send(503, b"The site is not built yet - the first update is still running.", "text/plain")
        if path == "/events.json":
            f = update.DATA / "events.json"
            return self._send(200, f.read_bytes() if f.exists() else b"[]", "application/json")
        if path == "/status":
            meta = update.read_json(update.DATA / "meta.json", {})
            nxt = State.next_update
            body = json.dumps({**meta, "updating": update.is_updating(),
                               "next_update": nxt.isoformat(timespec="seconds").replace("+00:00", "Z") if nxt else None})
            return self._send(200, body.encode(), "application/json")
        self._send(404, b"Not found", "text/plain")

    do_HEAD = do_GET

    def log_message(self, fmt, *args) -> None:      # route access logs through logging
        log.debug("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--interval-hours", type=float, default=24)
    ap.add_argument("--no-update-on-start", action="store_true",
                    help="wait a full interval before the first refresh instead of refreshing stale data at once")
    args = ap.parse_args()

    update.DATA.mkdir(exist_ok=True)
    update.LOGS.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(update.LOGS / "server.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")])

    stop = threading.Event()
    threading.Thread(target=scheduler, args=(timedelta(hours=args.interval_hours), not args.no_update_on_start, stop),
                     daemon=True, name="scheduler").start()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    log.info("Serving on http://%s:%d (refresh every %g h)", args.host, args.port, args.interval_hours)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
    finally:
        stop.set()
        update.stop_children()
        httpd.server_close()


if __name__ == "__main__":
    main()
