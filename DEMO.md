# Demonstration and viva runbook

Ten minutes, start to finish. Everything below has been run on the project
machine; the numbers quoted are from a real verified run, not from a plan.

---

## 1. Before you start (2 minutes)

Check the environment:

```bash
sudo ./scripts/check_env.sh
```

You want `OK` on: python3, mininet, mn, ovs-vsctl, ovs-ofctl, the openvswitch
daemon, and both TCP ports free. Unit tests run as part of that script and
should report 28 passing.

Make sure no earlier run is still alive:

```bash
sudo pkill -f controller.sdn_controller
sudo pkill -f content_server.py
sudo ip link del m1 2>/dev/null
```

Keep two terminals open: one for the demonstration, one for `show_flows.sh`.

---

## 2. The command

From Windows (no password prompt):

```
wsl.exe -u root -d Ubuntu -- bash -lc "cd /home/<user>/smart-content-delivery && ./run_demo.sh"
```

From inside WSL:

```bash
cd /home/<user>/smart-content-delivery
sudo ./run_demo.sh
```

Duration: about two minutes. It prints each phase as it runs and ends with
`DEMONSTRATION COMPLETED SUCCESSFULLY`.

---

## 3. The script, step by step

### Step 1 — topology and controller (about 15 seconds)

You will see:

```
topology ready: s1 + h1..h4, management bridge m1
switch ports: {'h1': 1, 'h2': 2, 'h3': 3, 'h4': 4}
[controller] switch=connected active=None | h2:up(load=0,rtt=1.7ms) ...
```

Say: *"s1 is a real Open vSwitch bridge speaking OpenFlow 1.3 to my controller
on port 6653. h1 is the client, h2, h3 and h4 are three content servers, each
on its own switch port. m1 is a separate plain Linux bridge used only for
health probes, so the controller's view of server health never depends on the
forwarding rules it is changing."*

If you have a second terminal, this is a good moment to show:

```bash
sudo ./scripts/show_flows.sh --short
```

and point at the ARP rule, the VIP-to-controller rule and the explicit
table-miss drop with its packet counter.

### Step 2 — baseline, 20 sequential requests

```
phase            : baseline
requests         : 20 (20 ok, 0 failed)
latency ms       : mean 192.21  median ...  max ...
served by        : h2=8, h3=7, h4=5
[flows:baseline] 45 rules in the table ...
```

Say: *"The client only knows 10.0.0.100. Every request is a fresh TCP
connection, so the controller made twenty independent decisions. Each decision
installed a flow pair: one that rewrites the destination to the chosen server,
one that rewrites the reply back to the virtual IP. The distribution is even
because all three servers were idle and the controller spreads by least
connections."*

Point at one installed rule in the dump:

```
priority=300,tcp,in_port=1,nw_dst=10.0.0.100,tp_src=57878,tp_dst=8080
    actions=set_field:00:00:00:00:00:11->eth_dst,
            set_field:10.0.0.11->ip_dst,output:2
```

Say: *"That is the entire load-balancing decision, expressed as an OpenFlow
rule. The switch does the rewriting; the controller is not in the data path."*

### Step 3 — load, 12 requests at concurrency 3

```
requests         : 12 (12 ok, 0 failed)
wall clock       : 0.80 s
served by        : h2=4, h3=4, h4=4
```

Say: *"Now three requests overlap. The health monitor samples once a second,
which cannot see a burst, so the controller also counts the connections it has
steered in the last half second. That is why the pool spreads one connection to
each server instead of piling all three onto the idle one."*

### Step 4 — throughput, five 4 MB transfers

```
throughput Mbps  : mean 1344.96 ...
```

Say: *"Throughput is measured over the transfer itself, first byte to last
byte, and it is limited by virtual Ethernet inside the kernel. The comparison
between servers is meaningful; the absolute rate is not an internet figure, and
I say so in the report."*

### Step 5 — failure and failover

```
[failure] killing content server on h4
[failure] controller detected the outage: True
[failure] flows for h4: 21 before, 0 after
[flows:failover] ...
[result] served by: h2=7, h3=3
```

Say: *"The server process is dead. The health probe fails twice, about two
seconds, and the controller does exactly one thing to the data plane: a
flow-mod delete carrying that server's cookie and a mask. Twenty-one rules
pointing at h4 disappear in one message. The next ten requests all succeed on
h2 and h3, and none reached h4."*

This is the strongest moment of the demonstration: the flow table is the proof.
Show `results/logs/flows_before_failure.txt` and
`results/logs/flows_after_failure.txt` side by side, or run
`./scripts/show_flows.sh --short` if the demo is still running (it is not
between phases, so use the dumps).

### Step 6 — recovery

```
[recovery] server process up: True, controller sees it healthy: True
[result] served by: h2=4, h3=3, h4=3
```

Say: *"The restarted server answers two probes, re-enters the pool, and takes
traffic again with no client-visible change."*

### Step 7 — the report

The run finishes by writing:

```
results/results.csv        raw measurements, one row per request
results/summary.md         per-phase and per-server tables
results/charts/*.svg       three charts, open them in a browser
```

Close with the acceptance test, which is what turns "it looked fine" into a
check:

```bash
sudo ./tests/test_e2e.sh
```

It runs the whole demonstration again and then asserts on the artifacts:
every phase present with zero failures, at least two servers used under
concurrency, the killed server absent from the failover results, zero flows
left for it, the restarted server used again, and all three charts valid SVG.

---

## 4. Expected numbers (last verified run)

| Phase | Requests | OK | Failed | Mean latency | p95 | Served by |
|---|---|---|---|---|---|---|
| baseline | 20 | 20 | 0 | 192.21 ms | 202.86 ms | h2, h3, h4 |
| load | 12 | 12 | 0 | 200.08 ms | 240.21 ms | h2, h3, h4 |
| throughput | 5 | 5 | 0 | 235.31 ms | 245.43 ms | h2, h3, h4 |
| failover | 10 | 10 | 0 | 198.80 ms | 205.32 ms | h2, h3 |
| recovery | 10 | 10 | 0 | 197.56 ms | 202.43 ms | h2, h3, h4 |

Latency is dominated by a 150 ms simulated server delay, which exists so that
concurrent requests overlap and load becomes observable. That is stated in the
README and in the summary, not hidden.

Exact distributions vary run to run. What must hold, and what the acceptance
test enforces: zero failed requests, at least two servers used under load, the
killed server used by nobody afterwards, and its flow count dropping to zero.

---

## 5. If something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| "must run as root" | Mininet needs namespaces | use the `wsl.exe -u root` command |
| "controller did not start" | a previous run still holds port 6653/9100 | `sudo pkill -f controller.sdn_controller` |
| "switch is not connected" | controller not listening | check `results/logs/controller.log` |
| `No route to host` | ARP responder not working | `./scripts/show_flows.sh --short`; the ARP rule's packet counter must increase |
| `timed out` | SYN forwarded but no reply | `sudo ovs-ofctl -O OpenFlow13 dump-flows s1 \| grep priority=300` |
| leftover `m1` bridge | previous run killed | `sudo ip link del m1` |
| charts missing | no successful requests | re-run the demonstration |

Full troubleshooting is in `README.txt`, section 16.

---

## 6. Viva questions

**What is SDN here, in one sentence?**
The controller decides forwarding by installing OpenFlow rules in a switch
that forwards packets itself; it is not a proxy in the traffic path.

**Show me the decision.**
`priority=300,tcp,in_port=1,nw_dst=10.0.0.100,tp_src=...,tp_dst=8080
actions=set_field:<server mac>->eth_dst,set_field:<server ip>->ip_dst,output:N`.
The output port is the chosen server.

**Why per connection instead of one rule for the VIP?**
One rule can only point at one server. Per connection, three simultaneous
requests can go to three servers, and the table shows each decision
separately.

**Why does the client not see the servers' addresses?**
The client connects to 10.0.0.100. The request rule rewrites the destination to
the server, and the reply rule rewrites the source back to 10.0.0.100 and to
the virtual MAC, so the client's TCP connection stays consistent.

**Who answers ARP for 10.0.0.100?**
The controller. The ARP rule sends requests to it as packet-ins and it replies
with a packet-out claiming the virtual MAC.

**What happens when a server dies?**
Two failed probes in a row mark it down; the controller sends one flow-mod
delete with that server's cookie mask, which removes every rule pointing at
it; new connections go to healthy servers.

**How do you prove the flows were removed?**
`flows_before_failure.txt` against `flows_after_failure.txt`, plus the
acceptance test, which counts rules by cookie and requires zero for the dead
server.

**What is the table-miss rule for?**
It drops, with counters, so nothing is forwarded unless the controller
installed a rule.

**How is load measured?**
Each server reports active connections and total served on `/stats`; the
controller polls once a second, and also counts connections it steered in the
last half second so a burst is not invisible.

**Why two networks?**
Health probes go over a plain Linux bridge, so a rule change can never make a
healthy server look dead.

**What is simulated?**
The 150 ms server processing delay, process-kill failures, static content and a
scripted client. Everything in the forwarding path is real.

**Why not Ryu or OS-Ken?**
Only Python 3.14 is available here and the OS-Ken package was not functional on
it. The OpenFlow 1.3 layer is therefore implemented in this repository with the
standard library, which also means every byte can be explained.

**What would you do differently at scale?**
Group tables with selection buckets instead of a flow per connection, multiple
switches and paths so path selection matters, and conntrack NAT instead of
per-connection rewriting.

---

## 7. Where the evidence lives

| Requirement | Artifact |
|---|---|
| TCP client, response time | `results/results.csv` (`latency_ms`, `ttfb_ms`) |
| Three servers, health, load | `server_h*.log`, `/stats`, `controller.log` |
| Dynamic selection with reason | `controller_events.jsonl` (`selection`, `reason`) |
| OpenFlow rules installed | `results/logs/flows_*.txt` |
| Rules changed on failure | `flows_before_failure.txt` vs `flows_after_failure.txt` |
| Failover | `status_after_failure.json`, failover rows in the CSV |
| Recovery | recovery rows in the CSV |
| Throughput | `throughput_mbps` in the CSV, `throughput_by_server.svg` |
| Summary and charts | `summary.md`, `charts/*.svg` |
| Automated proof | `tests/test_e2e.sh` + `tests/check_results.py` |
