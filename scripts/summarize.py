#!/usr/bin/env python3
"""Turn results/results.csv into a readable summary.

Writes results/summary.md and prints the same tables to stdout.
"""

from __future__ import annotations

import csv
import json
import os
import statistics
import sys
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import config  # noqa: E402

PHASE_ORDER = ["baseline", "load", "throughput", "failover", "recovery"]


def load_rows(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_events(path: str) -> list[dict]:
    events = []
    if not os.path.exists(path):
        return events
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events


def fmt(value: float, digits: int = 2) -> str:
    return "%.*f" % (digits, value)


def phase_table(rows: list[dict]) -> str:
    lines = [
        "| Phase | Requests | OK | Failed | Mean latency (ms) | p95 latency (ms) | Mean throughput (Mbps) | Served by |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for phase in PHASE_ORDER:
        phase_rows = [r for r in rows if r.get("phase") == phase]
        if not phase_rows:
            continue
        ok = [r for r in phase_rows if r.get("ok") == "True"]
        latencies = [float(r["latency_ms"]) for r in ok if r.get("latency_ms")]
        throughputs = [float(r["throughput_mbps"]) for r in ok if r.get("throughput_mbps")]
        served: dict[str, int] = defaultdict(int)
        for row in ok:
            served[row.get("server_host") or "?"] += 1
        p95 = "-"
        if latencies:
            ordered = sorted(latencies)
            index = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
            p95 = fmt(ordered[index])
        lines.append(
            "| %s | %d | %d | %d | %s | %s | %s | %s |"
            % (
                phase,
                len(phase_rows),
                len(ok),
                len(phase_rows) - len(ok),
                fmt(statistics.fmean(latencies)) if latencies else "-",
                p95,
                fmt(statistics.fmean(throughputs)) if throughputs else "-",
                ", ".join("%s=%d" % item for item in sorted(served.items())) or "-",
            )
        )
    return "\n".join(lines)


def server_throughput_table(rows: list[dict]) -> str:
    lines = [
        "| Server | Transfers | Mean throughput (Mbps) | Median (Mbps) | Mean latency (ms) |",
        "|---|---|---|---|---|",
    ]
    by_server: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("phase") == "throughput" and row.get("ok") == "True":
            by_server[row.get("server_host") or "?"].append(row)
    for server in sorted(by_server):
        group = by_server[server]
        throughputs = [float(r["throughput_mbps"]) for r in group]
        latencies = [float(r["latency_ms"]) for r in group]
        lines.append(
            "| %s | %d | %s | %s | %s |"
            % (
                server,
                len(group),
                fmt(statistics.fmean(throughputs)),
                fmt(statistics.median(throughputs)),
                fmt(statistics.fmean(latencies)),
            )
        )
    if len(lines) == 2:
        lines.append("| - | 0 | - | - | - |")
    return "\n".join(lines)


def main() -> int:
    rows = load_rows(config.RESULTS_CSV)
    events = load_events(config.EVENTS_JSONL)

    if not rows:
        print("no results yet: %s is missing or empty" % config.RESULTS_CSV)
        return 1

    decisions = [e for e in events if e.get("kind") == "selection"]
    failovers = [e for e in events if e.get("kind") == "failover"]
    down_events = [e for e in events if e.get("kind") == "server_down"]
    up_events = [e for e in events if e.get("kind") == "server_up"]

    table = phase_table(rows)
    throughput_table = server_throughput_table(rows)

    lines = [
        "# Smart Content Delivery System - measurement summary",
        "",
        "Generated from `results/results.csv` and `results/logs/controller_events.jsonl`.",
        "",
        "## Per-phase results",
        "",
        table,
        "",
        "## Throughput phase, per server",
        "",
        throughput_table,
        "",
        "## Controller activity",
        "",
        "- selection decisions logged: **%d**" % len(decisions),
        "- failure detections: **%d**, recoveries: **%d**" % (len(down_events), len(up_events)),
        "- failover events: **%d**" % len(failovers),
        "",
        "## How these numbers were produced",
        "",
        "- The client connects to the virtual IP `%s:%d` on every request and never to a"
        % (config.VIP, config.CONTENT_PORT),
        "  real server address, so the server identity is decided by the controller.",
        "- `latency_ms` is measured from the start of the TCP connect to the last byte of the",
        "  response body.",
        "- `throughput_mbps` is `bytes * 8 / (time from first byte to last byte) / 1e6`, a single",
        "  consistent definition applied to every request.",
        "- Every request opens a new TCP connection, so every request is an independent",
        "  load-balancing decision inside the switch.",
        "",
        "## Honest limitations",
        "",
        "- These throughput figures are limited by the virtual Ethernet and loopback paths inside",
        "  WSL2. They are meaningful *comparatively* (server vs server, before vs after a",
        "  failure), not as absolute internet-grade bandwidth.",
        "- Server-side processing delay is simulated (`--delay-ms`) so that concurrent requests",
        "  overlap and the load metric becomes observable.",
        "- Failures are induced by killing the server process, not by a physical fault.",
        "",
    ]

    summary = "\n".join(lines)
    with open(os.path.join(config.RESULTS_DIR, "summary.md"), "w", encoding="utf-8") as handle:
        handle.write(summary)

    print(table)
    print("")
    print(throughput_table)
    print("")
    print(
        "controller: %d selection decisions, %d failure detections, %d recoveries, %d failovers"
        % (len(decisions), len(down_events), len(up_events), len(failovers))
    )
    print("written: %s" % os.path.join(config.RESULTS_DIR, "summary.md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
