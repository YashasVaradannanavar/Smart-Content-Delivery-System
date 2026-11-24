#!/usr/bin/env bash
# Smart Content Delivery System - one-command demonstration.
#
# Needs root because Mininet builds network namespaces.
#
#   From Windows:  wsl.exe -u root -d Ubuntu -- bash -lc "cd /home/<user>/smart-content-delivery && ./run_demo.sh"
#   From WSL:      sudo ./run_demo.sh
#
# What it does:
#   1. generates the sample content
#   2. starts the OpenFlow controller (log: results/logs/controller.log)
#   3. builds the Mininet topology and runs the five demonstration phases
#   4. writes results/results.csv, results/summary.md and the SVG charts
#   5. shuts everything down again

set -uo pipefail
cd "$(dirname "$0")"
BASE="$(pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "This demonstration must run as root (Mininet requirement)."
    echo ""
    echo "From Windows, run:"
    echo "  wsl.exe -u root -d Ubuntu -- bash -lc \"cd $BASE && ./run_demo.sh\""
    echo ""
    echo "From inside WSL, run:"
    echo "  sudo $BASE/run_demo.sh"
    exit 2
fi

# Start from a clean slate: an interrupted previous run may still hold the
# controller ports, the switch bridge or the management bridge.
pkill -f "controller.sdn_controller" 2>/dev/null
pkill -f "servers/content_server.py" 2>/dev/null
sleep 0.5
ip link del m1 2>/dev/null
ovs-vsctl --if-exists del-br s1 2>/dev/null

mkdir -p results/logs results/charts state
rm -f results/results.csv
: > results/logs/controller.log
: > results/logs/controller_events.jsonl
: > results/logs/demo.log

echo "== 1/4 generating sample content =="
python3 scripts/make_content.py || exit 1

echo "== 2/4 starting the OpenFlow 1.3 controller =="
python3 -m controller.sdn_controller >> results/logs/controller.log 2>&1 &
CONTROLLER_PID=$!

cleanup() {
    echo ""
    echo "== shutting down =="
    kill "$CONTROLLER_PID" 2>/dev/null
    wait "$CONTROLLER_PID" 2>/dev/null
    ip link del m1 2>/dev/null
    pkill -f "servers/content_server.py" 2>/dev/null
}
trap cleanup EXIT INT TERM

ready=0
for _ in $(seq 1 60); do
    if curl -sf http://127.0.0.1:9100/healthz >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 0.25
done
if [ "$ready" -ne 1 ]; then
    echo "controller did not start; see results/logs/controller.log"
    tail -20 results/logs/controller.log
    exit 1
fi

echo "== 3/4 building the topology and running the demonstration =="
python3 -m topo --delay-ms 150 2>&1 | tee -a results/logs/demo.log
DEMO_STATUS=${PIPESTATUS[0]}

echo ""
echo "== 4/4 writing the report =="
python3 scripts/summarize.py || true
python3 scripts/make_charts.py || true

echo ""
echo "Artifacts:"
echo "  results/results.csv              raw per-request measurements"
echo "  results/summary.md               summary tables"
echo "  results/charts/*.svg             latency, throughput and distribution charts"
echo "  results/logs/controller.log      controller log"
echo "  results/logs/controller_events.jsonl  machine-readable event log"
echo "  results/logs/flows_*.txt         OpenFlow table dumps per phase"
echo "  results/logs/demo.log            this run's console output"

if [ "$DEMO_STATUS" -eq 0 ]; then
    echo ""
    echo "DEMONSTRATION COMPLETED SUCCESSFULLY"
else
    echo ""
    echo "DEMONSTRATION REPORTED PROBLEMS (exit $DEMO_STATUS) - see results/logs/demo.log"
fi
exit "$DEMO_STATUS"
