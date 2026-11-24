"""Unit tests for the server-selection policy.

The policy is a pure function, so every rule in it can be tested without
starting Mininet, a switch, or a single server.
"""

import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from controller import selector  # noqa: E402
from controller.selector import Candidate  # noqa: E402


def cand(host, index, active=0, rtt=1.0, healthy=True):
    return Candidate(host=host, ip="10.0.0.%d" % (10 + index), index=index,
                     healthy=healthy, active=active, rtt_ms=rtt)


class SelectionTests(unittest.TestCase):
    def test_empty_pool_returns_nothing(self):
        chosen, reason = selector.choose([], None)
        self.assertIsNone(chosen)
        self.assertIn("no healthy server", reason)

    def test_least_loaded_server_wins(self):
        pool = [cand("h2", 0, active=3), cand("h3", 1, active=1), cand("h4", 2, active=2)]
        chosen, reason = selector.choose(pool, None)
        self.assertEqual(chosen.host, "h3")
        self.assertIn("lowest load", reason)

    def test_unhealthy_server_is_never_chosen(self):
        pool = [cand("h2", 0, active=0, healthy=False), cand("h3", 1, active=5)]
        chosen, _ = selector.choose(pool, None)
        self.assertEqual(chosen.host, "h3")

    def test_all_unhealthy(self):
        pool = [cand("h2", 0, healthy=False), cand("h3", 1, healthy=False)]
        chosen, reason = selector.choose(pool, None)
        self.assertIsNone(chosen)
        self.assertIn("every content server failed", reason)

    def test_tie_breaks_on_lowest_index(self):
        pool = [cand("h3", 1, active=0, rtt=1.0), cand("h2", 0, active=0, rtt=1.0)]
        chosen, _ = selector.choose(pool, None)
        self.assertEqual(chosen.host, "h2")

    def test_probe_noise_does_not_move_traffic(self):
        # 1.2 ms vs 2.6 ms is inside the same 2 ms bucket: the lower index wins.
        pool = [cand("h2", 0, active=0, rtt=2.6), cand("h3", 1, active=0, rtt=1.2)]
        chosen, _ = selector.choose(pool, None)
        self.assertEqual(chosen.host, "h2")

    def test_clearly_faster_server_wins_inside_a_bucket(self):
        # 1.0 ms vs 8.0 ms lands in different buckets.
        pool = [cand("h2", 0, active=0, rtt=8.0), cand("h3", 1, active=0, rtt=1.0)]
        chosen, _ = selector.choose(pool, None)
        self.assertEqual(chosen.host, "h3")

    def test_failover_when_current_dies(self):
        pool = [cand("h2", 0, active=0, healthy=False), cand("h3", 1, active=2)]
        chosen, reason = selector.choose(pool, "h2")
        self.assertEqual(chosen.host, "h3")
        self.assertIn("failover", reason)

    def test_keeps_current_while_it_is_still_best(self):
        pool = [cand("h2", 0, active=1), cand("h3", 1, active=4)]
        chosen, reason = selector.choose(pool, "h2")
        self.assertEqual(chosen.host, "h2")
        self.assertIn("still best", reason)

    def test_margin_prevents_pointless_movement(self):
        pool = [cand("h2", 0, active=1), cand("h3", 1, active=0)]
        chosen, reason = selector.choose(pool, "h2", dwell_elapsed=True, margin=2)
        self.assertEqual(chosen.host, "h2")
        self.assertIn("marginally better", reason)

    def test_moves_when_the_gap_is_big_enough(self):
        pool = [cand("h2", 0, active=4), cand("h3", 1, active=0)]
        chosen, reason = selector.choose(pool, "h2", dwell_elapsed=True, margin=2)
        self.assertEqual(chosen.host, "h3")
        self.assertIn("rebalance", reason)

    def test_dwell_time_holds_traffic_in_place(self):
        pool = [cand("h2", 0, active=9), cand("h3", 1, active=0)]
        chosen, reason = selector.choose(pool, "h2", dwell_elapsed=False, margin=2)
        self.assertEqual(chosen.host, "h2")
        self.assertIn("dwell", reason)

    def test_recent_steers_raise_effective_load(self):
        """A burst the 1 Hz probe has not seen yet must still spread."""

        class State:
            def __init__(self, active):
                self.healthy = True
                self.active = active
                self.last_rtt_ms = 1.0

        class Spec:
            def __init__(self, host, index):
                self.host = host
                self.ip = "10.0.0.%d" % (10 + index)
                self.index = index

        specs = [Spec("h2", 0), Spec("h3", 1), Spec("h4", 2)]
        states = [State(0), State(0), State(0)]

        candidates = selector.to_candidates(states, specs, {"h2": 2})
        chosen, _ = selector.choose(candidates, None)
        self.assertNotEqual(chosen.host, "h2", "a server just given two connections must not win again")

        candidates = selector.to_candidates(states, specs, {"h2": 1, "h3": 1})
        chosen, _ = selector.choose(candidates, None)
        self.assertEqual(chosen.host, "h4")


if __name__ == "__main__":
    unittest.main(verbosity=2)
