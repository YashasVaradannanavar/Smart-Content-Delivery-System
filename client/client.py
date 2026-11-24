#!/usr/bin/env python3
"""TCP client for the Smart Content Delivery System.

The client only ever knows the virtual IP.  It has no idea which physical
server answers, and it must not: that is the controller's job.  The server
identity is recovered afterwards from the X-Served-By response header, purely
so the measurements can be attributed correctly in the CSV.

Measurements
------------
latency_ms      connect start -> last byte of the response body
ttfb_ms         connect start -> first byte of the response body
throughput_mbps body_bytes * 8 / transfer_time / 1e6, where transfer_time is
                first byte -> last byte (a defined, consistent method: it
                excludes connection setup and server-side think time)

Every request uses a fresh TCP connection, which is what makes each request an
independent load-balancing decision inside the switch.
"""

from __future__ import annotations

import argparse
import csv
import http.client
import os
import statistics
import sys
import threading
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import config  # noqa: E402

FIELDS = [
    "iso_time",
    "phase",
    "seq",
    "ok",
    "server_host",
    "server_ip",
    "status",
    "latency_ms",
    "ttfb_ms",
    "bytes",
    "throughput_mbps",
    "error",
]


def request_once(host: str, port: int, filename: str, timeout: float) -> dict:
    """Perform one HTTP GET over a fresh TCP connection and measure it."""
    started = time.perf_counter()
    result = {
        "ok": False,
        "server_host": "",
        "server_ip": "",
        "status": 0,
        "latency_ms": 0.0,
        "ttfb_ms": 0.0,
        "bytes": 0,
        "throughput_mbps": 0.0,
        "error": "",
    }
    connection = None
    try:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        connection.request("GET", "/content/%s" % filename)
        response = connection.getresponse()
        first_byte = time.perf_counter()
        body = response.read()
        finished = time.perf_counter()

        result["status"] = response.status
        result["server_host"] = response.getheader("X-Served-By") or "?"
        result["server_ip"] = response.getheader("X-Served-IP") or "?"
        result["bytes"] = len(body)
        result["latency_ms"] = (finished - started) * 1000.0
        result["ttfb_ms"] = (first_byte - started) * 1000.0
        transfer_s = max(finished - first_byte, 1e-9)
        result["throughput_mbps"] = len(body) * 8.0 / transfer_s / 1e6
        result["ok"] = response.status == 200 and len(body) > 0
        if not result["ok"]:
            result["error"] = "HTTP %d" % response.status
    except Exception as exc:  # noqa: BLE001 - any failure is a measurement
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
        result["latency_ms"] = (time.perf_counter() - started) * 1000.0
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:  # pragma: no cover
                pass
    return result


def run_phase(args) -> list[dict]:
    rows: list[dict] = []
    lock = threading.Lock()
    counter = {"next": 0}

    def worker() -> None:
        while True:
            with lock:
                index = counter["next"]
                if index >= args.requests:
                    return
                counter["next"] = index + 1
            outcome = request_once(args.host, args.port, args.file, args.timeout)
            outcome["iso_time"] = datetime.now().isoformat(timespec="milliseconds")
            outcome["phase"] = args.phase
            outcome["seq"] = index
            with lock:
                rows.append(outcome)

    threads = [
        threading.Thread(target=worker, daemon=True)
        for _ in range(max(1, min(args.concurrency, args.requests)))
    ]
    began = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - began

    rows.sort(key=lambda r: r["seq"])
    print_summary(rows, args, elapsed)
    return rows


def print_summary(rows: list[dict], args, elapsed: float) -> None:
    ok_rows = [r for r in rows if r["ok"]]
    print("")
    print("  phase            : %s" % args.phase)
    print("  file             : %s" % args.file)
    print("  requests         : %d (%d ok, %d failed)" % (len(rows), len(ok_rows), len(rows) - len(ok_rows)))
    print("  concurrency      : %d" % args.concurrency)
    print("  wall clock       : %.2f s" % elapsed)
    if ok_rows:
        latencies = [r["latency_ms"] for r in ok_rows]
        throughputs = [r["throughput_mbps"] for r in ok_rows]
        print("  latency ms       : mean %.2f  median %.2f  min %.2f  max %.2f"
              % (
                  statistics.fmean(latencies),
                  statistics.median(latencies),
                  min(latencies),
                  max(latencies),
              ))
        print("  throughput Mbps  : mean %.2f  median %.2f"
              % (statistics.fmean(throughputs), statistics.median(throughputs)))
        served = {}
        for row in ok_rows:
            served[row["server_host"]] = served.get(row["server_host"], 0) + 1
        print("  served by        : %s" % ", ".join(
            "%s=%d" % (host, count) for host, count in sorted(served.items())
        ))
        if len(latencies) >= 2:
            ordered = sorted(latencies)
            index = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
            print("  latency p95 ms   : %.2f" % ordered[index])
    for row in rows:
        if not row["ok"]:
            print("  FAILED seq=%s: %s" % (row["seq"], row["error"]))
    print("")


def write_csv(path: str, rows: list[dict], reset: bool) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    exists = os.path.exists(path) and not reset
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in FIELDS})


def main() -> int:
    parser = argparse.ArgumentParser(description="SCD test client")
    parser.add_argument("--phase", default="manual")
    parser.add_argument("--host", default=config.VIP)
    parser.add_argument("--port", type=int, default=config.CONTENT_PORT)
    parser.add_argument("--file", default=config.SMALL_FILE)
    parser.add_argument("--requests", type=int, default=config.DEFAULT_REQUESTS)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=config.CLIENT_TIMEOUT_S)
    parser.add_argument("--csv", default=config.RESULTS_CSV)
    parser.add_argument("--reset-csv", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if not args.quiet:
        print(
            "client: %d request(s) to http://%s:%d/content/%s (concurrency %d)"
            % (args.requests, args.host, args.port, args.file, args.concurrency)
        )
    rows = run_phase(args)
    write_csv(args.csv, rows, args.reset_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
