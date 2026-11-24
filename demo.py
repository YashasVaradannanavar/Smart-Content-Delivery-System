#!/usr/bin/env python3
"""Scripted demonstration of the Smart Content Delivery System.

Five phases, each producing evidence:

    1. baseline   20 sequential requests, all served by one server
    2. load       12 requests at concurrency 3, spread across healthy servers
    3. throughput 5 large transfers, measured
    4. failover   kill a server, watch the flows disappear and traffic move
    5. recovery   restart it, watch it rejoin the pool

Every phase writes its numbers to results/results.csv, its logs to
results/logs/, and its summary to stdout (captured by run_demo.sh).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.request

import config


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
H1 = config.CLIENT_HOST


# --------------------------------------------------------------------- helpers
def api(path: str = "/status") -> dict:
    url = "http://%s:%d%s" % (config.CONTROL_API_IP, config.CONTROL_API_PORT, path)
    with urllib.request.urlopen(url, timeout=5.0) as response:
        return json.loads(response.read().decode("utf-8"))


def banner(text: str) -> None:
    print("")
    print("=" * 78)
    print(text)
    print("=" * 78)


def run_client(
    topology,
    phase: str,
    requests: int,
    filename: str = config.SMALL_FILE,
    concurrency: int = 1,
    timeout: float = config.CLIENT_TIMEOUT_S,
) -> int:
    command = (
        "python3 %s/client/client.py --phase %s --requests %d --file %s "
        "--concurrency %d --timeout %.1f --csv %s"
        % (
            BASE_DIR,
            phase,
            requests,
            filename,
            concurrency,
            timeout,
            config.RESULTS_CSV,
        )
    )
    output = topology.hosts[H1].cmd(command)
    print(output.rstrip())
    failures = len(re.findall(r"^\s+FAILED seq=", output, re.MULTILINE))
    return failures


def dump_flows(topology, label: str) -> str:
    result = subprocess.run(
        ["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", "s1"],
        capture_output=True,
        text=True,
    )
    text = result.stdout
    os.makedirs(config.LOGS_DIR, exist_ok=True)
    path = os.path.join(config.LOGS_DIR, "flows_%s.txt" % label)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    counts = flow_counts(text)
    print(
        "[flows:%s] %d rules in the table, per server: %s"
        % (label, len([l for l in text.splitlines() if "cookie=0x1a" in l]), counts)
    )
    return text


def flow_counts(dump: str) -> dict:
    counts = {spec.host: 0 for spec in config.SERVERS}
    for line in dump.splitlines():
        match = re.search(r"cookie=0x([0-9a-fA-F]+)", line)
        if not match:
            continue
        value = int(match.group(1), 16)
        index = (value >> 40) & 0xFF
        if 1 <= index <= len(config.SERVERS):
            counts[config.SERVERS[index - 1].host] += 1
    return counts


def save_status(label: str) -> dict:
    status = api()
    path = os.path.join(config.LOGS_DIR, "status_%s.json" % label)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(status, handle, indent=2)
    return status


def read_csv_phase(phase: str) -> list[dict]:
    import csv

    if not os.path.exists(config.RESULTS_CSV):
        return []
    with open(config.RESULTS_CSV, newline="", encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle) if row.get("phase") == phase]


def distribution(rows: list[dict]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        if row.get("ok") == "True":
            counts[row.get("server_host") or "?"] = counts.get(row.get("server_host") or "?", 0) + 1
    return ", ".join("%s=%d" % item for item in sorted(counts.items())) or "none"


def wait_for_health(topology, host: str, healthy: bool, timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status = api()
            state = status["servers"][host]
            if bool(state["healthy"]) is healthy:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.25)
    return False


def print_status(status: dict) -> None:
    servers = status["servers"]
    parts = []
    for host in sorted(servers):
        state = servers[host]
        parts.append(
            "%s:%s(load=%d,rtt=%sms)"
            % (
                host,
                "up" if state["healthy"] else "DOWN",
                state["active"],
                "n/a" if state["rtt_ms"] is None else "%.1f" % state["rtt_ms"],
            )
        )
    print("[controller] switch=%s active=%s | %s"
          % ("connected" if status["switch"]["connected"] else "DOWN",
             status["active_host"], "  ".join(parts)))
    print("[controller] last decision: %s" % status["last_reason"])


# ------------------------------------------------------------------ the phases
def run_demonstration(topology) -> int:
    if os.path.exists(config.RESULTS_CSV):
        os.remove(config.RESULTS_CSV)
    problems: list[str] = []

    banner("PHASE 0 - topology and controller state")
    # Give the monitor one probe cycle so phase 0 reports a settled pool.
    deadline = time.time() + 15.0
    while time.time() < deadline:
        status = api()
        if status["switch"]["connected"] and all(
            state["healthy"] and state["ever_seen"] for state in status["servers"].values()
        ):
            break
        time.sleep(0.3)
    status = save_status("initial")
    print_status(status)
    print("[topology] switch ports: %s" % topology.ports)
    print("[topology] management plane: 10.255.0.254 <-> 10.255.0.11-13 (no OpenFlow)")
    if not status["switch"]["connected"]:
        problems.append("switch is not connected to the controller")
    if not all(state["healthy"] for state in status["servers"].values()):
        problems.append("not all servers are healthy at the start")

    banner("PHASE 1 - baseline: 20 sequential requests through the virtual IP")
    phase = "baseline"
    failures = run_client(topology, phase, requests=20)
    rows = read_csv_phase(phase)
    print("[result] served by: %s" % distribution(rows))
    dump_flows(topology, "baseline")
    if failures:
        problems.append("baseline had %d failed requests" % failures)

    banner("PHASE 2 - load: 12 requests at concurrency 3 (servers appear busy)")
    phase = "load"
    failures = run_client(topology, phase, requests=12, concurrency=3)
    rows = read_csv_phase(phase)
    print("[result] served by: %s" % distribution(rows))
    dump_flows(topology, "load")
    if failures:
        problems.append("load phase had %d failed requests" % failures)

    banner("PHASE 3 - throughput: 5 transfers of %s" % config.LARGE_FILE)
    phase = "throughput"
    failures = run_client(topology, phase, requests=5, filename=config.LARGE_FILE)
    dump_flows(topology, "throughput")
    if failures:
        problems.append("throughput phase had %d failed requests" % failures)

    banner("PHASE 4 - failure: kill a server and watch the switch stop using it")
    status = api()
    target = status["active_host"] or config.SERVERS[0].host
    before = flow_counts(dump_flows(topology, "before_failure"))
    print("[failure] killing content server on %s" % target)
    topology.stop_server(target)
    detected = wait_for_health(topology, target, healthy=False, timeout=12.0)
    print("[failure] controller detected the outage: %s" % detected)
    if not detected:
        problems.append("controller did not detect the %s outage" % target)
    after = flow_counts(dump_flows(topology, "after_failure"))
    print("[failure] flows for %s: %d before, %d after" % (target, before[target], after[target]))
    print_status(save_status("after_failure"))

    phase = "failover"
    failures = run_client(topology, phase, requests=10, timeout=4.0)
    rows = read_csv_phase(phase)
    print("[result] served by: %s" % distribution(rows))
    if rows and any(row.get("server_host") == target for row in rows if row.get("ok") == "True"):
        problems.append("traffic still reached the failed server %s" % target)
    if failures:
        problems.append("failover phase had %d failed requests" % failures)
    dump_flows(topology, "failover")

    banner("PHASE 5 - recovery: restart %s and let it rejoin" % target)
    topology.start_server(target)
    recovered = topology.wait_for_server(config.SERVER_BY_HOST[target], timeout=15.0)
    healthy = wait_for_health(topology, target, healthy=True, timeout=15.0)
    print("[recovery] server process up: %s, controller sees it healthy: %s" % (recovered, healthy))
    if not (recovered and healthy):
        problems.append("server %s did not recover" % target)

    phase = "recovery"
    failures = run_client(topology, phase, requests=10)
    rows = read_csv_phase(phase)
    print("[result] served by: %s" % distribution(rows))
    if failures:
        problems.append("recovery phase had %d failed requests" % failures)
    dump_flows(topology, "recovery")
    print_status(save_status("final"))

    banner("DEMONSTRATION RESULT")
    if problems:
        print("FAILED:")
        for problem in problems:
            print("  - %s" % problem)
        return 1
    print("All phases completed: selection, load spreading, measured throughput,")
    print("failure detection, failover, and recovery all produced real evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit("run this through topo.py: python3 -m topo")
