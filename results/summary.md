# Smart Content Delivery System - measurement summary

Generated from `results/results.csv` and `results/logs/controller_events.jsonl`.

## Per-phase results

| Phase | Requests | OK | Failed | Mean latency (ms) | p95 latency (ms) | Mean throughput (Mbps) | Served by |
|---|---|---|---|---|---|---|---|
| baseline | 20 | 20 | 0 | 192.28 | 210.84 | 238.65 | h2=9, h3=5, h4=6 |
| load | 12 | 12 | 0 | 200.70 | 240.35 | 221.47 | h2=5, h3=4, h4=3 |
| throughput | 5 | 5 | 0 | 230.39 | 260.51 | 1216.25 | h2=2, h3=2, h4=1 |
| failover | 10 | 10 | 0 | 199.54 | 203.16 | 244.26 | h3=5, h4=5 |
| recovery | 10 | 10 | 0 | 187.26 | 200.15 | 219.22 | h2=4, h3=3, h4=3 |

## Throughput phase, per server

| Server | Transfers | Mean throughput (Mbps) | Median (Mbps) | Mean latency (ms) |
|---|---|---|---|---|
| h2 | 2 | 1146.72 | 1146.72 | 241.46 |
| h3 | 2 | 1433.92 | 1433.92 | 215.23 |
| h4 | 1 | 919.98 | 919.98 | 238.57 |

## Controller activity

- selection decisions logged: **150**
- failure detections: **1**, recoveries: **1**
- failover events: **1**

## How these numbers were produced

- The client connects to the virtual IP `10.0.0.100:8080` on every request and never to a
  real server address, so the server identity is decided by the controller.
- `latency_ms` is measured from the start of the TCP connect to the last byte of the
  response body.
- `throughput_mbps` is `bytes * 8 / (time from first byte to last byte) / 1e6`, a single
  consistent definition applied to every request.
- Every request opens a new TCP connection, so every request is an independent
  load-balancing decision inside the switch.

## Honest limitations

- These throughput figures are limited by the virtual Ethernet and loopback paths inside
  WSL2. They are meaningful *comparatively* (server vs server, before vs after a
  failure), not as absolute internet-grade bandwidth.
- Server-side processing delay is simulated (`--delay-ms`) so that concurrent requests
  overlap and the load metric becomes observable.
- Failures are induced by killing the server process, not by a physical fault.
