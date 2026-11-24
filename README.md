# Smart Content Delivery System

**A Mininet and OpenFlow 1.3 project that selects a healthy content server for every client connection and proves the decision by rewriting the switch's forwarding rules.**

The client only knows one virtual address, `10.0.0.100:8080`. A custom OpenFlow 1.3 controller health-checks three content servers, chooses one from live load and latency measurements, and installs a request/reply flow pair that steers that connection through an Open vSwitch. When a server fails, the controller removes its rules and the client keeps working without being reconfigured.

The full mini-project report is available at **[CN_Project_Report.pdf](CN_Project_Report.pdf)**.

---

## Project overview

The repository contains two related demonstrations:

1. **Main controller-based system** — `controller/`, `servers/`, `client/`, `topo.py`, `config.py`, and `run_demo.sh`. This is the implementation described in the report. It has a real controller on TCP port 6653, a real OpenFlow 1.3 switch, three threaded HTTP content servers, a concurrent client, health monitoring over a separate management bridge, automatic failover, recovery, CSV measurements, charts, and an end-to-end acceptance test.
2. **Standalone single-file harnesses** — `simple_scdn.py` and `simple_scdn_v2.py`. These use `ovs-ofctl` directly with no controller. They are retained because they produce independent evidence for timeout-versus-refused failure signatures and link/process fault injection. `simple_scdn_v2.py` is the version documented in `OUTPUT_EXPLAINED.md`.

---

## Features

- **Virtual-IP content delivery** — one client address, three interchangeable servers, no client-side server list.
- **Custom OpenFlow 1.3 controller** — hand-written `struct` encoder/decoder, no Ryu or OS-Ken dependency.
- **Proxy ARP** — the controller answers ARP for the VIP with a virtual MAC.
- **Per-connection forwarding** — each TCP connection receives a request rule and a reply rule, so concurrent requests can use different servers.
- **Load-aware selection** — fewest active connections first, then probe RTT in 2 ms buckets, with a 4 s dwell time and a load margin of 2.
- **Burst handling** — recently steered connections are counted before the 1 Hz probe catches up.
- **Health monitoring** — one probe per second over a plain Linux management bridge; two consecutive failures mark a server DOWN.
- **Automatic failover and recovery** — one cookie-masked OpenFlow delete removes every rule for the failed server; the restarted server is re-admitted after passing probes.
- **Strict data plane** — a table-miss rule drops unmatched traffic, so forwarding only works because the controller installed a rule.
- **Measurement** — latency, time to first byte, bytes, throughput, success rate, per-server distribution, flow dumps, controller events and server access logs.
- **Reproducible evidence** — 28 unit tests, a scripted five-phase demonstration, and an end-to-end artifact checker.

---

## Technologies used

| Technology | Role in the project |
|---|---|
| Python 3 (tested with 3.14.4) | Application code, controller, client, servers, tests and scripts |
| Python standard library | `http.client`, `http.server`, `socket`, `struct`, `threading`, `urllib`, `csv`, `json` |
| Mininet 2.3.0 | Virtual hosts, Open vSwitch switch, links and namespaces |
| Open vSwitch 3.7.1 | Software switch that executes the OpenFlow rules |
| OpenFlow 1.3 | Southbound control protocol on TCP port 6653 |
| Bash | Launchers, environment check and acceptance test |
| SVG | Measurement charts generated from the CSV |

There are **no third-party Python packages** and no `pip install` step. The `.venv/` directory is not required and is intentionally not committed.

---

## Prerequisites

- Linux, or Windows with WSL2 and an Ubuntu distribution.
- Root access (`sudo`), because Mininet creates network namespaces.
- Mininet, Open vSwitch and Python 3 available inside the same Linux environment.
- `curl` for the controller readiness check used by `run_demo.sh`.
- The project stored in the Linux filesystem, not under `/mnt/c`, for usable filesystem performance.

Verified environment for the recorded results: Ubuntu 26.04.1 LTS on WSL2, Mininet 2.3.0, Open vSwitch 3.7.1, Python 3.14.4, OpenFlow 1.3.

---

## Installation and setup

```bash
# Ubuntu / WSL2
sudo apt update
sudo apt install -y mininet openvswitch-switch python3 curl

# Start Open vSwitch if it is not already running
sudo service openvswitch-switch start

# From the repository directory
chmod +x run_demo.sh scripts/*.sh tests/test_e2e.sh
```

The sample content (`servers/content/small.txt`, 4096 bytes, and `servers/content/large.bin`, 4 MiB) is committed so the demonstration is reproducible. `run_demo.sh` can regenerate it with `scripts/make_content.py` if it is missing.

---

## Run the main demonstration

From inside WSL/Ubuntu:

```bash
cd ~/smart-content-delivery
sudo ./run_demo.sh
```

From Windows without entering WSL (replace `<user>` with the Ubuntu username):

```bash
wsl.exe -u root -d Ubuntu -- bash -lc "cd /home/<user>/smart-content-delivery && ./run_demo.sh"
```

The run takes about two minutes and executes five phases: baseline, concurrent load, throughput, failover and recovery. It ends with `DEMONSTRATION COMPLETED SUCCESSFULLY` when all phases pass.

Useful commands during or after a run:

```bash
# Inspect the live flow table while the demo is running
sudo ./scripts/show_flows.sh --short

# Check the environment and run the unit tests
sudo ./scripts/check_env.sh

# Show the generated CSV as aligned columns
column -s, -t results/results.csv
```

To stop leftover processes or bridges after an interrupted run:

```bash
sudo pkill -f controller.sdn_controller
sudo pkill -f servers/content_server.py
sudo ip link del m1 2>/dev/null
sudo ovs-vsctl --if-exists del-br s1
```

---

## Tests

```bash
# Unit tests: selector policy and OpenFlow 1.3 encoder (28 tests)
python3 -m unittest discover -s tests -v

# End-to-end: reruns the demonstration, then validates the artifacts
sudo ./tests/test_e2e.sh
```

The end-to-end test checks that every phase ran with zero failed requests, that the load phase used at least two servers, that the killed server received no traffic and had zero remaining rules, that the restarted server was used again, and that all three SVG charts are valid. **It overwrites `results/` with a new run**, so preserve the committed evidence first if you need to keep it.

A short demonstration/viva runbook is in **[DEMO.md](DEMO.md)**.

---

## Standalone demonstrations (optional)

These are independent harnesses that program Open vSwitch directly with `ovs-ofctl`; they do not use the controller.

```bash
# Recommended standalone harness (link and process failure injection)
sudo python3 simple_scdn_v2.py

# Original single-file version
sudo python3 simple_scdn.py
```

Their outputs are written to:

- `results/simple_scdn_results.csv` — original harness (27 request rows).
- `results/simple_scdn_v2_results.csv` — second harness (41 request rows, 38 successful, three deliberate failures).
- `results/simple_scdn_v2_run.log` and `output.txt` — recorded console evidence.

`OUTPUT_EXPLAINED.md` explains every timing and throughput number produced by `simple_scdn_v2.py`.

---

## Results directory

| Path | Contents |
|---|---|
| `results/results.csv` | Main demonstration, one row per request: phase, sequence, status, server, latency, TTFB, bytes, throughput and error |
| `results/summary.md` | Per-phase latency/p95, per-server throughput, controller activity and limitations |
| `results/charts/` | Three SVG charts: latency by phase, requests per server, throughput per server |
| `results/logs/controller.log` | Controller operation and health transitions |
| `results/logs/controller_events.jsonl` | Machine-readable selections, failovers and flow deletions |
| `results/logs/demo.log` | Console output from the recorded demonstration |
| `results/logs/flows_*.txt` | OpenFlow table dumps per phase, including before/after failure |
| `results/logs/status_*.json` | Controller snapshots at the start, after failure and at the end |
| `results/logs/server_h*.log` | Independent access logs from the three content servers |
| `results/simple_scdn*_results.csv` | Standalone harness measurements |
| `results/simple_scdn_v2_run.log`, `output.txt` | Recorded standalone console output |

Key recorded results: **57 of 57** scripted requests succeeded; the failure phase removed **33 h2 flows** and sent **10 of 10** requests to the surviving servers; recovery re-admitted h2 for **4 of 10** requests; the controller logged **150 selection decisions**, one failure, one failover and one recovery.

---

## Documentation map

- `README.md` — this file: canonical overview, setup, commands and results guide.
- `DEMO.md` — ten-minute controller demonstration and viva runbook.
- `OUTPUT_EXPLAINED.md` — line-by-line interpretation of the standalone v2 output.
- `README.txt`, `howtorun.txt` — detailed guide for the original standalone `simple_scdn.py`.
- `old_project_docs/` — archived earlier documentation, retained for history; not required to run anything.
- `Smart_Content_Delivery_Team_Guide_and_Viva.docx` — optional Word guide for the standalone v2 harness.
- `CN_Project_Report.pdf` — concise academic report with architecture, results and rubric coverage.

---

## Limitations

- One switch and one path, so there is no path diversity to evaluate.
- Content is static; no cache, TLS or authentication layer is implemented.
- Throughput is measured over virtual Ethernet inside WSL2 and reflects memory-copy limits, not internet-grade bandwidth.
- The 150 ms server delay is simulated so concurrent requests overlap and load is observable.
- Steering is per connection; an established TCP connection is not migrated.
- Failures are induced by killing a process or downing a link, not by physical hardware faults.

---

## Repository hygiene

No API keys, passwords, tokens or private credentials are present. `.env` files, keys, virtual environments, bytecode caches, runtime `state/`, editor files and OS metadata are excluded by `.gitignore`. The recorded logs are experimental evidence and are kept intentionally.

---

## Report

**[CN_Project_Report.pdf](CN_Project_Report.pdf)** — six-page academic report covering the problem, architecture, TCP socket implementation, Mininet/OpenFlow implementation, socket–SDN integration, dynamic behaviour, failure testing, measured results, demonstration instructions, viva questions and an explicit D1/D2 rubric coverage table.
