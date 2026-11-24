"""Server-selection algorithm.

The rule, in one sentence:

    Pick the healthy server with the fewest active connections; break ties on
    the lowest measured probe round-trip time; then on the lowest index.

Keeping this as a pure function (no sockets, no clocks beyond a passed-in
timestamp) means the whole decision policy is unit-testable, and the reason
string it returns is what gets logged during the demonstration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass
class Candidate:
    """Immutable view of one server's state, as seen by the selector."""

    host: str
    ip: str
    index: int
    healthy: bool
    active: int = 0
    rtt_ms: Optional[float] = None


RTT_BUCKET_MS = 2.0


def _rank(candidate: Candidate):
    """Sort key: lower is better.

    1. fewest active connections        (the primary load signal)
    2. probe latency, bucketed at 2 ms  (so probe noise cannot move traffic)
    3. lowest configured index          (fully deterministic last resort)

    Without the bucketing, two idle servers whose probe round-trip times
    differ by a fraction of a millisecond would swap places on every probe,
    which looks like random assignment in the demonstration and is not what
    the algorithm claims to do.
    """
    rtt = candidate.rtt_ms if candidate.rtt_ms is not None else 1e9
    return (
        candidate.active,
        round(rtt / RTT_BUCKET_MS),
        candidate.index,
    )


def choose(
    candidates: Iterable[Candidate],
    current_host: Optional[str],
    dwell_elapsed: bool = True,
    margin: int = 2,
) -> tuple[Optional[Candidate], str]:
    """Choose a server.

    Returns (chosen_candidate_or_None, human_readable_reason).
    """
    candidates = list(candidates)
    healthy = [c for c in candidates if c.healthy]

    if not healthy:
        return None, "no healthy server: every content server failed its probe"

    best = min(healthy, key=_rank)
    current = next((c for c in candidates if c.host == current_host), None)

    if current is None:
        return best, (
            "initial selection: %s has the lowest load (%d active, %s ms)"
            % (best.host, best.active, _fmt(best.rtt_ms))
        )

    if not current.healthy:
        return best, (
            "failover: %s is unhealthy, moving to %s (%d active, %s ms)"
            % (current.host, best.host, best.active, _fmt(best.rtt_ms))
        )

    if best.host == current.host:
        return current, (
            "keep %s: still best (%d active, %s ms)"
            % (current.host, current.active, _fmt(current.rtt_ms))
        )

    if not dwell_elapsed:
        return current, (
            "keep %s: %s is better but the dwell time has not elapsed"
            % (current.host, best.host)
        )

    if current.active - best.active >= margin:
        return best, (
            "rebalance: %s has %d active vs %d on %s (margin %d)"
            % (best.host, best.active, current.active, current.host, margin)
        )

    return current, (
        "keep %s: %s is only marginally better (%d vs %d active, margin %d)"
        % (current.host, best.host, best.active, current.active, margin)
    )


def _fmt(value: Optional[float]) -> str:
    return "n/a" if value is None else "%.2f" % value


def to_candidates(states, specs, extra_load=None) -> list[Candidate]:
    """Adapter used by the controller: ServerState objects -> Candidates.

    `extra_load` maps host name -> connections the controller has steered very
    recently but that the 1 Hz health probe may not have counted yet.  The
    higher of the two counts is used, so a burst of simultaneous connections is
    spread over the pool instead of all landing on the same server.
    """
    extra_load = extra_load or {}
    return [
        Candidate(
            host=spec.host,
            ip=spec.ip,
            index=spec.index,
            healthy=state.healthy,
            active=max(state.active, extra_load.get(spec.host, 0)),
            rtt_ms=state.last_rtt_ms,
        )
        for spec, state in zip(specs, states)
    ]
