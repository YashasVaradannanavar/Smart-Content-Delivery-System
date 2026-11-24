"""Health and load monitoring.

Every server exposes a /stats endpoint on the management network.  This module
polls it once per second, over the plain Linux bridge m1, which means probes
never traverse the OpenFlow switch.  That separation is deliberate: a server
must not be declared dead just because the controller deleted a flow rule, and
a failed flow must not hide a healthy server.

Each poll measures:
    reachability  - did the HTTP request complete at all
    probe RTT     - round-trip time of the probe itself, in milliseconds
    active        - requests currently in flight on that server
    total         - requests served since the server started
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Optional

import config


log = logging.getLogger("monitor")


@dataclass
class ServerState:
    host: str
    ip: str
    mgmt_ip: str
    index: int
    healthy: bool = True
    failures: int = 0
    last_rtt_ms: Optional[float] = None
    active: int = 0
    total: int = 0
    last_error: str = ""
    ever_seen: bool = False
    transitions: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "host": self.host,
            "ip": self.ip,
            "healthy": self.healthy,
            "failures": self.failures,
            "rtt_ms": None if self.last_rtt_ms is None else round(self.last_rtt_ms, 3),
            "active": self.active,
            "total": self.total,
            "last_error": self.last_error,
            "ever_seen": self.ever_seen,
        }


class HealthMonitor(threading.Thread):
    """Polls every server's /stats endpoint and updates shared state."""

    def __init__(self, on_transition=None):
        super().__init__(name="health-monitor", daemon=True)
        self.states: Dict[str, ServerState] = {
            spec.host: ServerState(
                host=spec.host, ip=spec.ip, mgmt_ip=spec.mgmt_ip, index=spec.index
            )
            for spec in config.SERVERS
        }
        self._stop = threading.Event()
        self._on_transition = on_transition

    # ------------------------------------------------------------------ thread
    def run(self) -> None:
        while not self._stop.is_set():
            for state in self.states.values():
                self._probe(state)
            self._stop.wait(config.PROBE_INTERVAL_S)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ probes
    def _probe(self, state: ServerState) -> None:
        url = "http://%s:%d/stats" % (state.mgmt_ip, config.CONTENT_PORT)
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(url, timeout=config.PROBE_TIMEOUT_S) as response:
                payload = json.loads(response.read().decode("utf-8"))
            rtt_ms = (time.perf_counter() - started) * 1000.0
            was_healthy = state.healthy
            state.last_rtt_ms = rtt_ms
            state.active = int(payload.get("active", 0))
            state.total = int(payload.get("total", 0))
            state.failures = 0
            state.last_error = ""
            state.healthy = True
            state.ever_seen = True
            if not was_healthy:
                self._transition(state, "UP")
        except Exception as exc:  # noqa: BLE001 - any failure means unreachable
            state.failures += 1
            state.last_error = "%s: %s" % (type(exc).__name__, exc)
            if state.failures >= config.PROBE_FAIL_THRESHOLD and state.healthy:
                state.healthy = False
                self._transition(state, "DOWN")

    def _transition(self, state: ServerState, direction: str) -> None:
        log.warning("server %s is %s (%s)", state.host, direction, state.last_error or "ok")
        state.transitions.append(
            {"time": time.time(), "host": state.host, "event": direction}
        )
        if self._on_transition:
            try:
                self._on_transition(state, direction)
            except Exception:  # pragma: no cover - never kill the monitor
                log.exception("transition callback failed")

    # -------------------------------------------------------------- accessors
    def snapshot(self) -> Dict[str, ServerState]:
        return dict(self.states)

    def all_healthy(self) -> bool:
        return all(s.healthy for s in self.states.values())
