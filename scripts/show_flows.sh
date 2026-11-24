#!/usr/bin/env bash
# Show what the OpenFlow switch is really doing.
#
#   ./scripts/show_flows.sh          summary plus full table
#   ./scripts/show_flows.sh --short  summary only
#
# Every rule the controller installs carries a cookie that encodes the server
# it points at: bits 63-56 = 0x1a (project tag), 55-48 = rule kind,
# 47-40 = server index + 1.  This script decodes that so the table can be read.

set -uo pipefail
cd "$(dirname "$0")/.."

if ! ovs-vsctl br-exists s1 2>/dev/null; then
    echo "switch s1 does not exist: the demonstration is not running."
    echo "start it with ./run_demo.sh, or in another terminal run ./scripts/show_flows.sh while it runs"
    exit 1
fi

dump="$(ovs-ofctl -O OpenFlow13 dump-flows s1)"

echo "cookie map: 0x1a<kind><server><seq>  kind 1=request 2=reply 3=arp"
echo ""

python3 - "$dump" <<'PY'
import re
import sys

text = sys.argv[1]
kinds = {0: "table-miss", 1: "connection request", 2: "connection reply", 3: "arp", 4: "scaffolding"}
counts = {}
total = 0
for line in text.splitlines():
    if "cookie=" not in line:
        continue
    total += 1
    cookie = int(re.search(r"cookie=0x([0-9a-fA-F]+)", line).group(1), 16)
    if cookie >> 56 != 0x1A:
        continue
    kind = (cookie >> 48) & 0xFF
    server = ((cookie >> 40) & 0xFF) - 1
    key = (server, kinds.get(kind, str(kind)))
    counts[key] = counts.get(key, 0) + 1

print("rules in the table: %d" % total)
if counts:
    print("")
    print("  server   rule kind              rules")
    print("  -------  ---------------------  -----")
    for (server, kind), count in sorted(counts.items()):
        if server == -1:
            name = "vip/any"
        elif 0 <= server <= 2:
            name = "h%d" % (server + 2)
        else:
            name = "?"
        print("  %-7s  %-21s  %5d" % (name, kind, count))
PY

if [ "${1:-}" != "--short" ]; then
    echo ""
    echo "full flow table:"
    echo "$dump"
fi
