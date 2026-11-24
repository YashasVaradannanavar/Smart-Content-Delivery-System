#!/usr/bin/env python3
"""Content server for the Smart Content Delivery System.

One instance runs inside each server host (h2, h3, h4).  It serves three things:

    GET /content/<file>   the actual content (with X-Served-By / X-Served-IP)
    GET /health           "OK", used by simple liveness checks
    GET /stats            JSON: active requests, totals, uptime, pid

/metrics endpoints are deliberately excluded from the active-request counter so
the controller's own probes never look like client load.

The optional --delay-ms simulates content preparation time.  It exists so that
concurrent client requests actually overlap, which is what makes the load
balancing visible and measurable.  It is a simulated processing delay and is
documented as such.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import config  # noqa: E402


class Metrics:
    def __init__(self, host: str, data_ip: str):
        self.host = host
        self.data_ip = data_ip
        self.started = time.time()
        self.lock = threading.Lock()
        self.active = 0
        self.total = 0
        self.bytes_served = 0

    def enter(self) -> None:
        with self.lock:
            self.active += 1

    def leave(self, served_bytes: int) -> None:
        with self.lock:
            self.active = max(0, self.active - 1)
            self.total += 1
            self.bytes_served += served_bytes

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "host": self.host,
                "data_ip": self.data_ip,
                "pid": os.getpid(),
                "active": self.active,
                "total": self.total,
                "bytes_served": self.bytes_served,
                "uptime_s": round(time.time() - self.started, 2),
            }


class ContentHandler(BaseHTTPRequestHandler):
    server_version = "SCDContent/1.0"
    protocol_version = "HTTP/1.0"

    # -------------------------------------------------------------- utilities
    @property
    def metrics(self) -> Metrics:
        return self.server.metrics  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        sys.stdout.write(
            "%s %s - %s\n"
            % (time.strftime("%H:%M:%S"), self.metrics.host, fmt % args)
        )
        sys.stdout.flush()

    def _send(self, status: int, body: bytes, content_type: str, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Served-By", self.metrics.host)
        self.send_header("X-Served-IP", self.metrics.data_ip)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    # ---------------------------------------------------------------- routing
    def do_GET(self):  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/health":
            self._send(200, b"OK\n", "text/plain")
            return
        if path == "/stats":
            self._send(200, json.dumps(self.metrics.snapshot()).encode(), "application/json")
            return
        if path.startswith("/content/"):
            self._serve_content(unquote(path[len("/content/") :]))
            return
        if path == "/":
            self._send(
                200,
                b"Smart Content Delivery content server. Try /content/small.txt\n",
                "text/plain",
            )
            return
        self._send(404, b"not found\n", "text/plain")

    def _serve_content(self, name: str) -> None:
        safe_name = os.path.basename(name or "")
        file_path = os.path.join(config.CONTENT_DIR, safe_name)
        if not safe_name or not os.path.isfile(file_path):
            self._send(404, b"no such content\n", "text/plain")
            return

        self.metrics.enter()
        try:
            with open(file_path, "rb") as handle:
                body = handle.read()
            if self.server.delay_ms:  # type: ignore[attr-defined]
                time.sleep(self.server.delay_ms / 1000.0)  # type: ignore[attr-defined]
            self._send(
                200,
                body,
                "application/octet-stream",
                {"Content-Disposition": 'attachment; filename="%s"' % safe_name},
            )
            self.metrics.leave(len(body))
        except BrokenPipeError:
            self.metrics.leave(0)
        except Exception:  # pragma: no cover - defensive
            self.metrics.leave(0)
            raise


class ContentServer:
    def __init__(self, host: str, data_ip: str, port: int, delay_ms: int = 0):
        self.metrics = Metrics(host, data_ip)
        self.httpd = ThreadingHTTPServer(("0.0.0.0", port), ContentHandler)
        self.httpd.daemon_threads = True
        self.httpd.allow_reuse_address = True
        self.httpd.metrics = self.metrics  # type: ignore[attr-defined]
        self.httpd.delay_ms = delay_ms  # type: ignore[attr-defined]
        self.host = host
        self.port = port

    def serve_forever(self) -> None:
        addr = socket.gethostbyname(socket.gethostname())
        print(
            "%s listening on 0.0.0.0:%d (data ip %s, host %s, delay %d ms)"
            % (self.host, self.port, addr, socket.gethostname(), self.httpd.delay_ms),
            flush=True,
        )
        self.httpd.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description="SCD content server")
    parser.add_argument("--name", required=True, help="host name, e.g. h2")
    parser.add_argument("--data-ip", required=True, help="data-plane IP, e.g. 10.0.0.11")
    parser.add_argument("--port", type=int, default=config.CONTENT_PORT)
    parser.add_argument("--delay-ms", type=int, default=0)
    args = parser.parse_args()

    config.ensure_dirs()
    print(
        "starting %s: data-ip=%s port=%d delay=%dms"
        % (args.name, args.data_ip, args.port, args.delay_ms),
        flush=True,
    )
    ContentServer(args.name, args.data_ip, args.port, args.delay_ms).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
