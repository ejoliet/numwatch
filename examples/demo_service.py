#!/usr/bin/env python3
"""demo_service.py — fake job-queue metric source + mock Slack webhook receiver.

Local stand-in for a real Jenkins/DuckDB target so numwatch's Gate 3 dogfood
harness can exercise the full sample -> alert -> notify -> render loop without
any real secrets or network calls. Stdlib only.

Run: python3 examples/demo_service.py [--port 8765] [--pings-log examples/pings.jsonl]

Endpoints:
  GET  /metrics       -> plain-text integer queue depth (scripted timeline, see /timeline)
  GET  /metrics.json  -> {"queue_depth": int, "t": int}
  GET  /health        -> 200 "1" normally, 503 "degraded" for 150<=t<190
  GET  /reset         -> resets the t=0 clock, returns "ok"
  GET  /timeline      -> human-readable description of the scripted schedule
  POST /hooks/slack   -> mock incoming webhook; appends one JSON line to the pings log
"""
from __future__ import annotations

import argparse
import datetime
import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TIMELINE_DESC = """demo_service scripted timeline (t = seconds since last /reset or process start):
  t < 60        : queue depth 25 + (t % 7)   (calm)
  60 <= t < 120 : queue depth 95 + (t % 3)   (high -> expect BREACH on demo-queue-depth)
  120 <= t < 200: queue depth 30 + (t % 5)   (recovered -> expect RECOVERED)
  200 <= t < 240: queue depth 97             (second breach)
  t >= 240      : queue depth 28             (second recovery)

  150 <= t < 190: /health returns 503 "degraded" (curl -sf fails -> error samples
                  -> after 3 consecutive: demo-health source FAILING, then RECOVERED)
  otherwise     : /health returns 200 "1"
"""

# AIDEV-note: mutable start time in a list so /reset can rebind it from the request handler.
_start_time = [time.monotonic()]
_pings_lock = threading.Lock()
_pings_log_path = "examples/pings.jsonl"


def _elapsed() -> float:
    return time.monotonic() - _start_time[0]


def _queue_depth(t: float) -> int:
    ti = int(t)
    if ti < 60:
        return 25 + (ti % 7)
    if ti < 120:
        return 95 + (ti % 3)
    if ti < 200:
        return 30 + (ti % 5)
    if ti < 240:
        return 97
    return 28


def _health_ok(t: float) -> bool:
    return not (150 <= t < 190)


class Handler(BaseHTTPRequestHandler):
    server_version = "numwatch-demo/1.0"

    def log_message(self, fmt: str, *args) -> None:
        # AIDEV-note: quiet the default access-log-to-stdout; we log our own short line to stderr.
        sys.stderr.write(f"[demo_service] {self.address_string()} {fmt % args}\n")

    def _write(self, status: int, body: bytes, content_type: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        t = _elapsed()
        if self.path == "/metrics":
            self._write(200, str(_queue_depth(t)).encode())
        elif self.path == "/metrics.json":
            body = json.dumps({"queue_depth": _queue_depth(t), "t": int(t)}).encode()
            self._write(200, body, "application/json")
        elif self.path == "/health":
            if _health_ok(t):
                self._write(200, b"1")
            else:
                self._write(503, b"degraded")
        elif self.path == "/reset":
            _start_time[0] = time.monotonic()
            self._write(200, b"ok")
        elif self.path == "/timeline":
            self._write(200, TIMELINE_DESC.encode())
        else:
            self._write(404, b"not found")

    def do_POST(self) -> None:
        if self.path != "/hooks/slack":
            self._write(404, b"not found")
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"text": raw.decode(errors="replace")}
        text = payload.get("text", "")
        now = time.time()
        line = {
            "ts": int(now),
            "iso": datetime.datetime.fromtimestamp(now, tz=datetime.UTC).isoformat(),
            "text": text,
        }
        line_json = json.dumps(line)
        with _pings_lock, open(_pings_log_path, "a") as f:
            f.write(line_json + "\n")
        print(line_json)
        self._write(200, b"ok")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--port", type=int, default=8765, help="port to bind (default 8765)")
    parser.add_argument(
        "--pings-log",
        default="examples/pings.jsonl",
        help="path to append received webhook pings as JSON lines (default examples/pings.jsonl)",
    )
    args = parser.parse_args()

    global _pings_log_path
    _pings_log_path = args.pings_log
    # AIDEV-note: touch the pings log up front so consumers can rely on it
    # existing (possibly empty) even if the run ends before any ping fires.
    open(_pings_log_path, "a").close()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)

    def _shutdown(signum, frame):
        sys.stderr.write(f"[demo_service] received signal {signum}, shutting down\n")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    sys.stderr.write(f"[demo_service] listening on 127.0.0.1:{args.port}, pings-log={_pings_log_path}\n")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        sys.stderr.write("[demo_service] stopped\n")


if __name__ == "__main__":
    main()
