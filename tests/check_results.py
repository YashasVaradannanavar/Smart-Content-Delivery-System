#!/usr/bin/env python3
"""Assert that the last demonstration run produced valid evidence.

This is the automated acceptance test.  It reads the artifacts the demo left
behind (CSV, controller events, flow dumps, status snapshots, charts) and checks
the claims the project makes:

    every phase ran, every request succeeded
    the controller logged real selection decisions
    the load phase actually spread connections over several servers
    after the failure, no traffic reached the killed server
    the switch's flow table had that server's rules removed
    the restarted server was used again
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import config  # noqa: E402

PHASES = ["baseline", "load", "throughput", "failover", "recovery"]
problems: list[str] = []
notes: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        problems.append(message)


def load_rows() -> list[dict]:
    if not os.path.exists(config.RESULTS_CSV):
        problems.append("results.csv is missing")
        return []
    with open(config.RESULTS_CSV, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_json(path: str) -> dict:
    if not os.path.exists(path):
        problems.append("%s is missing" % path)
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def load_events() -> list[dict]:
    if not os.path.exists(config.EVENTS_JSONL):
        problems.append("controller_events.jsonl is missing")
        return []
    events = []
    with open(config.EVENTS_JSONL, encoding="utf-8") as handle:
        for line in handle:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def flow_count_for(path: str, host: str) -> int:
    """Count flow rules whose cookie names the given server."""
    if not os.path.exists(path):
        problems.append("%s is missing" % path)
        return -1
    index = config.SERVER_BY_HOST[host].index + 1
    count = 0
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            found = re.search(r"cookie=0x([0-9a-fA-F]+)", line)
            if found and ((int(found.group(1), 16) >> 40) & 0xFF) == index:
                count += 1
    return count


def main() -> int:
    rows = load_rows()
    events = load_events()
    after_failure = load_json(os.path.join(config.LOGS_DIR, "status_after_failure.json"))

    if not rows:
        return report()

    by_phase: dict[str, list[dict]] = {}
    for row in rows:
        by_phase.setdefault(row["phase"], []).append(row)

    for phase in PHASES:
        phase_rows = by_phase.get(phase, [])
        check(bool(phase_rows), "phase '%s' produced no results" % phase)
        failed = [r for r in phase_rows if r.get("ok") != "True"]
        check(not failed, "phase '%s' had %d failed requests" % (phase, len(failed)))

    # --- the controller really did choose, not just forward ------------------
    decisions = [e for e in events if e.get("kind") == "selection"]
    check(len(decisions) > 10, "only %d selection decisions were logged" % len(decisions))
    check(
        all("reason" in e and e["reason"] for e in decisions),
        "some selection decisions have no logged reason",
    )

    # --- the load phase spread connections ----------------------------------
    load_hosts = {r["server_host"] for r in by_phase.get("load", []) if r.get("ok") == "True"}
    check(len(load_hosts) >= 2, "the concurrent load phase used only %s" % (load_hosts or "no server"))
    notes.append("load phase servers: %s" % ", ".join(sorted(load_hosts)))

    # --- failure handling ---------------------------------------------------
    servers = after_failure.get("servers", {})
    down = [host for host, state in servers.items() if not state.get("healthy")]
    check(len(down) == 1, "expected exactly one server down after the failure phase, saw %s" % down)
    if len(down) == 1:
        killed = down[0]
        notes.append("killed server: %s" % killed)

        failover_rows = [r for r in by_phase.get("failover", []) if r.get("ok") == "True"]
        reached = [r for r in failover_rows if r["server_host"] == killed]
        check(not reached, "%d requests still reached the failed server %s" % (len(reached), killed))

        recovery_rows = [r for r in by_phase.get("recovery", []) if r.get("ok") == "True"]
        if recovery_rows:
            check(
                any(r["server_host"] == killed for r in recovery_rows),
                "the restarted server %s never rejoined the pool" % killed,
            )

        check(
            flow_count_for(os.path.join(config.LOGS_DIR, "flows_after_failure.txt"), killed) == 0,
            "the switch still had flows pointing at the failed server %s" % killed,
        )
        before = flow_count_for(os.path.join(config.LOGS_DIR, "flows_before_failure.txt"), killed)
        check(before > 0, "no flows were installed for %s before the failure" % killed)
        notes.append("flows for %s: %d before failure, 0 after" % (killed, before))

    check(
        any(e.get("kind") == "failover" for e in events),
        "the controller logged no failover event",
    )
    check(
        any(e.get("kind") == "flow_delete" for e in events),
        "the controller logged no flow deletion",
    )

    # --- throughput actually measured --------------------------------------
    throughput_rows = [r for r in by_phase.get("throughput", []) if r.get("ok") == "True"]
    if throughput_rows:
        values = [float(r["throughput_mbps"]) for r in throughput_rows]
        check(all(value > 0 for value in values), "some throughput measurements are zero")
        notes.append("throughput samples: %s Mbps" % ", ".join("%.0f" % v for v in values))

    # --- artifacts ----------------------------------------------------------
    for name in ("latency_by_phase.svg", "throughput_by_server.svg", "requests_by_server.svg"):
        path = os.path.join(config.CHARTS_DIR, name)
        check(os.path.exists(path), "chart %s is missing" % name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                check("<svg" in handle.read(200), "chart %s is not valid SVG" % name)
    check(
        os.path.exists(os.path.join(config.RESULTS_DIR, "summary.md")),
        "summary.md is missing",
    )

    return report()


def report() -> int:
    print("")
    for note in notes:
        print("  ok   %s" % note)
    if problems:
        print("")
        for problem in problems:
            print("  FAIL %s" % problem)
        print("")
        print("E2E RESULT: FAILED (%d problems)" % len(problems))
        return 1
    print("")
    print("E2E RESULT: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
