#!/usr/bin/env bash
# End-to-end acceptance test.
#
# Runs the complete demonstration (about two minutes) and then asserts on the
# artifacts it produced.  Must run as root.
#
#   sudo ./tests/test_e2e.sh
#   wsl.exe -u root -d Ubuntu -- bash -lc "cd /home/<user>/smart-content-delivery && ./tests/test_e2e.sh"

set -uo pipefail
cd "$(dirname "$0")/.."
BASE="$(pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root (Mininet). Try:"
    echo "  wsl.exe -u root -d Ubuntu -- bash -lc \"cd $BASE && ./tests/test_e2e.sh\""
    exit 2
fi

echo "== unit tests =="
python3 -m unittest discover -s tests -q || exit 1

echo ""
echo "== full demonstration (~2 minutes) =="
./run_demo.sh > /tmp/scd_e2e_demo.log 2>&1
demo_status=$?
echo "run_demo.sh exit status: $demo_status"
tail -30 /tmp/scd_e2e_demo.log

echo ""
echo "== artifact assertions =="
python3 tests/check_results.py
check_status=$?

if [ "$check_status" -ne 0 ]; then
    echo ""
    echo "E2E: FAILED (see /tmp/scd_e2e_demo.log)"
    exit 1
fi

echo ""
echo "E2E: PASSED"
exit 0
