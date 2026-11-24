#!/usr/bin/env bash
# Verify that this machine can run the Smart Content Delivery System.
# Safe to run at any time; it changes nothing.

set -uo pipefail
cd "$(dirname "$0")/.."

fail=0
say()  { printf '%-42s %s\n' "$1" "$2"; }
ok()   { say "$1" "OK ($2)"; }
bad()  { say "$1" "MISSING"; fail=1; }

echo "Smart Content Delivery System - environment check"
echo "-------------------------------------------------"

if [ "$(id -u)" -eq 0 ]; then
    ok "running as root" "required by Mininet"
else
    say "running as root" "NO - the demonstration needs root, unit tests do not"
fi

if command -v python3 >/dev/null 2>&1; then
    ok "python3" "$(python3 --version 2>&1)"
else
    bad "python3"
fi

if python3 -c "import mininet" >/dev/null 2>&1; then
    ok "mininet python package" "$(python3 -c 'import mininet; print(mininet.__file__)')"
else
    bad "mininet python package"
fi

if command -v mn >/dev/null 2>&1; then
    ok "mn command" "$(command -v mn)"
else
    bad "mn command"
fi

for tool in ovs-vsctl ovs-ofctl; do
    if command -v "$tool" >/dev/null 2>&1; then
        ok "$tool" "$($tool --version 2>/dev/null | head -1)"
    else
        bad "$tool"
    fi
done

if ovs-vsctl show >/dev/null 2>&1; then
    ok "openvswitch daemon" "reachable"
else
    bad "openvswitch daemon"
fi

for port in 6653 9100; do
    if ss -ltn 2>/dev/null | grep -q ":$port "; then
        say "tcp port $port" "IN USE (a previous run may still be alive)"
        fail=1
    else
        ok "tcp port $port" "free"
    fi
done

if [ -f servers/content/small.txt ] && [ -f servers/content/large.bin ]; then
    ok "sample content" "small.txt + large.bin"
else
    say "sample content" "not generated yet (run_demo.sh creates it)"
fi

echo ""
echo "unit tests:"
python3 -m unittest discover -s tests -q 2>&1 | tail -3

echo ""
if [ "$fail" -eq 0 ]; then
    echo "environment looks good. Run: ./run_demo.sh (as root)"
else
    echo "environment has problems, see MISSING lines above"
fi
exit "$fail"
