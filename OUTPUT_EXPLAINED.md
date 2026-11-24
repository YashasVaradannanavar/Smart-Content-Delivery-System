# Understanding the output of `simple_scdn_v2.py`

Every number this demo prints is either **a measured time**, **a rate**, or **a
count from the switch**. This document says which is which, in what unit, and
why some of them are hundreds of times larger than others. All example values
below are from one real recorded run (`output.txt` in this folder); your own
run will differ by a few milliseconds.

---

## 1. How to run it

From a WSL/Ubuntu terminal (Mininet needs root, so `sudo`):

```bash
cd ~/smart-content-delivery
sudo python3 simple_scdn_v2.py
```

From Windows without entering WSL:

```bash
wsl.exe -u root -d Ubuntu -- bash -lc "cd /home/<user>/smart-content-delivery && python3 simple_scdn_v2.py"
```

Keep a copy of the run (this is how `output.txt` was produced):

```bash
sudo python3 simple_scdn_v2.py 2>&1 | tee output.txt
```

See the machine-readable results as a table:

```bash
column -s, -t results/simple_scdn_v2_results.csv
```

The run takes roughly **40-70 seconds**, because it deliberately waits in a few
places (see §7). Nothing else is needed: no controller, no `pip install`.

---

## 2. The mental model in one paragraph

One client (`h1`, 10.0.0.1) always asks for the same address,
`10.0.0.100:8080`. Three web servers (`h2` .11, `h3` .12, `h4` .13) all own that
address as a /32. The switch `s1` holds one rule that rewrites the destination
MAC of VIP traffic and sends it out one server's port. Changing that single rule
is what "selecting a server" means in this project. The client is never told
which server answered; the answer body says which one did
(`served_by=h2/h3/h4`), and that is checked against where the rule pointed.

---

## 3. The four kinds of number in this output

| Printed as | What it actually is | Unit | Who decides the size |
|---|---|---|---|
| **request time** | Full HTTP request inside `h1`: TCP connect start → last byte of body | ms | the whole path + server work |
| **probe time** | One bare TCP connect to a server's own address, no data | ms | the path only |
| **transfer time** | Same request time, but for the 8 MiB file | ms | file size ÷ speed |
| **throughput** | bytes received ÷ transfer time, 1 MB = 1,000,000 bytes | MB/s | memory/veth speed |
| **`duration=`** in a flow dump | Age of a rule inside the switch | s | when the rule was installed |
| **`n_packets` / `n_bytes`** | Packets/bytes that rule has matched | count | how much traffic hit it |

Two of these are easy to confuse and must never be mixed up in the report:

* **request time is not RTT.** It contains the TCP handshake, the server reading
  the file, building the HTTP response, and sending the body. RTT is one
  round trip and would be a fraction of it.
* **`duration=` is not a request time at all.** It is a stopwatch inside the
  switch that starts when the rule is installed. A rule can show
  `duration=12.4s` while every request through it took 2 ms.

---

## 4. Every time value in the output, in order of appearance

| Where | Value in the recorded run | What it means |
|---|---|---|
| STEP 1, `no-rules` | **2504.8 ms FAIL** | The client's own 2.5 s timeout expiring. No rule exists, so the SYN is dropped and nothing ever answers. |
| STEP 2, `baseline` | **7.6 ms OK** | First real request: ARP resolution + cold server. Slower than later requests. |
| STEP 3, health probes | **0.8-0.9 ms UP** | TCP connect to each server's own IP. Handshake only, no file, no body. |
| STEP 4, `smart` | **1.6-6.4 ms** | Normal request after the path is warm; the 6 ms ones are right after a rule change. |
| STEP 6, transfer time | **20.1-28.6 ms** | 8 MiB through the virtual link; 8,388,621 bytes ÷ that time gives the MB/s column. |
| STEP 7, `path-reference` | **2.9 ms OK** | The "port connected" row of the comparison: cable in, h3 healthy. |
| STEP 7, `path-failure` | **2502.9 ms FAIL** | Cable pulled: the frame dies at the switch, so no answer and no error can come back. Only the client's 2.5 s timeout ends it. |
| STEP 7, probe after unplug | **400.9 ms DOWN** | The probe's own 400 ms timeout. Close to 400 ms = "nothing came back", i.e. a broken path. |
| STEP 7, `path-back` | **2.0 ms OK** | Cable back in; VIP traffic reaches h3 again. |
| STEP 8, `failure` | **0.2 ms FAIL** | h3's process is dead but the cable is fine, so the kernel answers with RST. Refused instantly — no timeout to wait for. |
| STEP 8, probe after kill | **0.1 ms DOWN** | Refused instantly, the signature of "host up, nothing listening on port 8080". |
| STEP 9, `availability` | **1.4-1.8 ms OK** | Requests keep succeeding on the two healthy servers. |
| STEP 10, `recovery-back` | **6.8 ms OK** | First request to h3 after its process restarted: interpreter start-up and cold caches. |
| STEP 10, `recovery` | **1.7-2.3 ms OK** | Steady state again with h3 back in the pool. |
| Flow dumps | `duration=1.021s` etc. | Age of that rule in the switch at the moment of the dump. |
| SUMMARY, `mean time` | e.g. `smart 3.4 ms` | Mean of the **successful** requests of that phase only. Failures are excluded on purpose. |

Not printed as a number, but it controls how long the run takes: `link settle`
= **1500 ms** after each unplug/replug, the **1000 ms** wait before each flow
dump (Open vSwitch refreshes counters about once a second), and the **2.5 s**
timeout paid twice in a run.

---

## 5. Why some times are hundreds of times larger

Each cause below is visible somewhere in the output.

**1. A timeout is a floor, not a measurement of anything.**
`2504.8 ms` in STEP 1 and `2502.9 ms` in STEP 7 are the script's
`REQUEST_TIMEOUT = 2.5` seconds plus overhead. They say "nothing came back
within 2.5 s"; they do **not** say the request was 2.5 s slow. This is why the
summary excludes them from the mean: one dead path would otherwise make the
whole phase look 500x slower than it really is.

**2. A refused connection returns immediately.**
`0.2 ms` (STEP 8) looks impossible next to the 2.5 s timeout, and it is the
single most interesting number in the demo: the packet reached the host, the
host had no listener, so the kernel replied RST at once. Same failed request to
the user, completely different mechanism and duration. That contrast is the
whole point of STEP 11.

**3. The first request to a server is slower than the rest.**
`baseline 7.6 ms` vs `distribution 1.7 ms`, and `recovery-back 6.8 ms` vs
`recovery 2.0 ms`. Reasons: ARP has to resolve the next hop, the Python HTTP
server may be starting a handler thread, the file is read from disk the first
time, and the CPU is busy building the topology. After that, everything is in
page cache and the path is warm.

**4. A rule change costs a little.**
The requests immediately after `point_vip_at()` (the 6.4 ms and 6.2 ms `smart`
rows) are the ones right after the switch's flow table was modified. A few
milliseconds is normal; you are looking at noise plus the switch's first packet
on a new rule.

**5. Probe time is always much smaller than request time.**
Probes are `0.1-0.9 ms`, requests `1.4-6.4 ms`, and that is expected: a probe is
one TCP handshake and then the connection is closed. A request does a handshake
*and* an HTTP exchange *and* the server reads and sends a file. Probes are used
for health only, never as a latency figure in the report.

**6. File size dominates transfer time.**
8,388,621 bytes in `20.1 ms` is 416.6 MB/s; the same size in `28.6 ms` is
293.2 MB/s. The spread between servers is scheduler and memory-copy noise, not
link quality — on a Mininet veth pair, "throughput" mostly measures how fast the
machine can copy memory. Report the comparison, not the absolute MB/s.

**7. `duration=` grows through the run.**
The rules installed at STEP 2 show small durations; the same rules dumped at
STEP 9 show ~20 s. It is the age of the rule. The only time-related thing it
proves is that the rule survived, which is why it is printed next to
`n_packets`.

**8. The run takes a minute because of deliberate pauses.**
Printed times add up to well under 100 ms, but the run sleeps on purpose:
`LINK_SETTLE = 1.5 s` twice (unplug, replug), 1 s before each of the five flow
dumps, and the 2.5 s timeout three times (STEP 1, STEP 7, and once more during
the failure demonstration). If a teacher asks why the wall clock is long, that
is the answer — not slow code.

---

## 6. The tables, column by column

### Request table (STEP 1 onward)

```
pass? phase         rule-> served        time   what that time measures
OK    distribution  h2     h2          1.8 ms   completed: TCP connect + 13 byte body
```

* `pass?` — `OK` means an HTTP 200 arrived, `FAIL` means no usable answer.
* `phase` — which part of the demo produced the row; the same name appears in
  the CSV.
* `rule->` — where the VIP rule pointed **before** the request was sent.
* `served` — which server really answered, read from the response body.
  `rule->` and `served` must match; a mismatch would be a real bug, and this
  column is how you prove the switch (not the client) picked the server.
* `time` — the request time, defined above.
* last column — a plain-English note saying what that specific time adds up to:
  a completed request, a client timeout, or an instant refusal.

### Health table (STEP 3 onward)

```
server address     state  probe time   what that probe time means
h3     10.0.0.12   DOWN     400.9 ms   no answer within the 400 ms probe timeout
```

`state` is `UP` only if the TCP connect succeeded. `probe time` near 400 ms
means packets are going nowhere; `probe time` near 0 ms with `DOWN` means the
host refused (process gone). Those two DOWN signatures are how the policy
distinguishes a link failure from a process failure without being told which
happened.

### Throughput table (STEP 6 / summary)

```
result rule-> served  bytes received  transfer time    throughput
OK     h2     h2           8,388,621       20.1 ms    416.6 MB/s
```

`bytes received` is what actually arrived (8,388,621 = 8 MiB file plus HTTP
headers), `transfer time` is the measured request time for that download, and
`throughput` is bytes ÷ time in decimal MB/s (1 MB = 1,000,000 bytes, not MiB —
say so if you quote the number).

### Flow dump (STEP 1, 2, 4, 5, 9)

```
cookie=0x0, duration=1.021s, table=0, n_packets=6, n_bytes=481, priority=100,
  ip,nw_dst=10.0.0.100 actions=set_field:00:00:00:00:00:11->eth_dst,output:2
```

Read it as: *rule `cookie` has existed for `duration` seconds, sits in `table 0`,
has matched `n_packets` packets / `n_bytes` bytes, wins at `priority 100` when a
packet matches `ip,nw_dst=10.0.0.100`, and then the switch rewrites the
destination MAC and sends it out port 2 (= h2).* The `n_packets` counter is the
honest proof that the rule is being used: it is counted inside the switch, not
by the script.

### Comparison table (STEP 11)

Five rows, identical request, one changed condition each. Read across: the
`result` and `served` columns show *what* the user got, the two time columns show
*why the two failure modes feel different* (timeout vs refusal), and the delta
paragraphs below the table state the ratios so you do not have to compute them
on stage.

### Summary table

`requests / succeeded / failed / success / mean time` per phase. `mean time`
covers successful requests only, which is why `no-rules` and `failure` print
`-` instead of a number. The two cross-checks at the bottom (script counts vs
each server's own access log) are the strongest evidence in the demo: they agree
exactly, so the numbers are not self-reported fiction.

---

## 7. Walkthrough: what each step proves

| Step | What happens | The number to point at |
|---|---|---|
| 0 | Build topology, print the legend | — |
| 1 | No rules installed | `2504.8 ms` FAIL: with no rule, nothing works |
| 2 | Install per-host + ARP + VIP rules | `n_packets` on the VIP rule is non-zero |
| 3 | Probe each server directly | sub-ms probes: the servers are alive |
| 4 | Choose by health + latency, then request | `decision: ... -> VIP -> h4`, and `served=h4` |
| 5 | Round-robin over the three servers | h2/h3/h4 rotate, client never changes |
| 6 | 8 MiB download per server | transfer time and MB/s |
| 7 | Unplug h3's cable | `2502.9 ms` FAIL + probe `400.9 ms` DOWN, then 4 OK on other servers, then `path-back 2.0 ms` |
| 8 | Kill h3's process | `0.2 ms` FAIL (refused) — different failure from step 7 |
| 9 | Keep serving while h3 is dead | 6/6 served, all on h2/h4 |
| 10 | Restart h3 | `recovery-back 6.8 ms` served by h3, then policy picks it again |
| 11 | One request, five conditions | the 5-row comparison table |
| — | Summary | success %, means, per-server counts, access-log cross-check |

---

## 8. Traps to avoid when presenting

1. **Do not call request time "latency" or "RTT".** It is completion time and
   includes server work. If you need RTT, the probe time is the closer number.
2. **Do not quote 2.5 s as a server speed.** It is the client's timeout, a
   constant in the script.
3. **Do not average failures into the mean.** The summary deliberately does not;
   if you compute your own average from the CSV, filter `ok==True` first.
4. **Do not compare MB/s across machines or with internet speeds.** A veth pair
   in WSL2 measures memory bandwidth. Compare server to server, run to run.
5. **Do not read `duration=` as a request time.** It is rule age in seconds.
6. **Do not claim h3 was "re-enabled" at step 10.** No code re-enables it: the
   next probe measured it as UP, so the policy offered it again. That is the
   measurement-driven design and the strongest thing to say in the viva.
7. **Do not be surprised that the killed server is faster.** Refused in 0.2 ms
   vs timed out in 2.5 s is the correct, explainable behaviour, not a bug.

---

## 9. Likely viva questions and short answers

**Why is a dead path 2500 ms and a dead process 0.2 ms?**
A pulled cable produces silence, so only the client timeout can end the wait. A
dead process produces a TCP RST from a live kernel, so the connect fails
immediately. Different failures, different errors (`TimeoutError` vs
`ConnectionRefusedError`), same user-visible outcome.

**Why do the probes stay under 1 ms?**
They are a single TCP handshake with no HTTP and no file, over a virtual link
between namespaces on the same machine.

**Why does the same server win repeatedly in step 4?**
The three links are identical, so latency differences are noise. A lowest-latency
policy therefore keeps choosing whichever server is marginally ahead. Step 5
exists to show traffic can also be spread when the policy says round-robin.

**Why is `path-failure` mean 1.8 ms when it contains a 2503 ms failure?**
Because the mean uses successful requests only. The failure is recorded in the
`failed` column and in the CSV, not averaged in.

**Why does `recovery-back` show 6.8 ms instead of ~2 ms?**
First request after restarting the process: interpreter start-up, cold file
cache, first packet on a re-established path.

**Why is throughput different on each server with identical links?**
CPU scheduling and memory copies dominate on a veth pair; the spread is noise
around the same ceiling. The claim to make is "all three serve 8 MiB
successfully in ~20-30 ms", not an exact MB/s ranking.

**Where does the CSV come from and what are its columns?**
`results/simple_scdn_v2_results.csv`, written by the run:
`phase, expected_server, served_by, healthy, ok, latency_ms, bytes,
throughput_mbps, error`. One row per request, so it can be charted directly.

---

## 10. Where each number is produced in the code

| Number | Function |
|---|---|
| request time, bytes, throughput | `fetch()` in `simple_scdn_v2.py` |
| probe time, UP/DOWN | `check_health()` |
| `served_by` | `served_by()` (parses the response body) |
| rule-> | `point_vip_at()` / `choose_server()` |
| flow dump fields | `show_flows()` (raw `ovs-ofctl dump-flows s1`) |
| comparison table | `snapshot()` and `comparison()` |
| summary and CSV | `summarise()` and `save_csv()` |
| constants 2.5 s / 400 ms / 1500 ms | top of the file: `REQUEST_TIMEOUT`, `PROBE_TIMEOUT`, `LINK_SETTLE` |

---

## Appendix: the recorded run these numbers come from

41 requests, 38 successful. All 3 failures are deliberate: one in `no-rules`
(step 1), one in `path-failure` (step 7) and one in `failure` (step 8). Every
other phase is 100%.

| phase | requests | success | mean time |
|---|---|---|---|
| no-rules | 1 | 0% | - |
| baseline | 1 | 100% | 7.6 ms |
| smart | 6 | 100% | 3.4 ms |
| distribution | 9 | 100% | 1.7 ms |
| throughput | 3 | 100% | 23.3 ms |
| path-reference | 1 | 100% | 2.9 ms |
| path-failure | 5 | 80% | 1.8 ms |
| path-back | 1 | 100% | 2.0 ms |
| failure | 1 | 0% | - |
| availability | 6 | 100% | 1.7 ms |
| recovery-back | 1 | 100% | 6.8 ms |
| recovery | 6 | 100% | 2.0 ms |

Served: h2 = 9, h3 = 11, h4 = 18 (verified independently from each server's own
access log). Throughput: h2 416.6 MB/s, h3 293.2 MB/s, h4 393.8 MB/s for
8,388,621 bytes each.
