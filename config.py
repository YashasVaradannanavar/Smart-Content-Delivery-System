"""Central configuration for the Smart Content Delivery System.

Every address, port and tuning value used anywhere in the project is defined
here exactly once, so the topology, controller, servers, client and test
scripts can never disagree about the addressing plan.

Addressing plan
---------------
    data plane (through the OpenFlow switch s1)
        10.0.0.1     client        h1
        10.0.0.11-13 content servers h2 h3 h4
        10.0.0.100   VIP presented to the client (owned by no real host)

    management plane (plain Linux bridge m1, no OpenFlow involved)
        10.255.0.254 controller
        10.255.0.11-13 the same three servers

The VIP is the whole point of the project: the client only ever knows
10.0.0.100.  Which physical server answers is decided by the controller and
enforced by OpenFlow rules in the switch.
"""

import os
from dataclasses import dataclass


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- data plane
VIP = "10.0.0.100"
VIP_MAC = "00:00:00:00:00:aa"

CLIENT_HOST = "h1"
CLIENT_IP = "10.0.0.1"
CLIENT_MAC = "00:00:00:00:00:01"

CONTENT_PORT = 8080

# --------------------------------------------------------------- management
MGMT_BRIDGE = "m1"
MGMT_PREFIX = "10.255.0"
CONTROLLER_MGMT_IP = "10.255.0.254"

# --------------------------------------------------------------- controller
CONTROLLER_LISTEN_IP = "0.0.0.0"      # switch connects to 127.0.0.1:6653
CONTROLLER_PORT = 6653
CONTROL_API_IP = "127.0.0.1"
CONTROL_API_PORT = 9100

PROBE_INTERVAL_S = 1.0
PROBE_TIMEOUT_S = 0.5
PROBE_FAIL_THRESHOLD = 2              # consecutive failures => server down
REBALANCE_MIN_DWELL_S = 4.0           # do not move traffic again too quickly
REBALANCE_LOAD_MARGIN = 2             # only move if the win is at least this big


@dataclass(frozen=True)
class ServerSpec:
    """One content server: its identity on both planes."""

    host: str
    ip: str
    mac: str
    mgmt_ip: str
    index: int

    @property
    def data_port(self) -> int:
        return CONTENT_PORT


SERVERS = (
    ServerSpec("h2", "10.0.0.11", "00:00:00:00:00:11", "10.255.0.11", 0),
    ServerSpec("h3", "10.0.0.12", "00:00:00:00:00:12", "10.255.0.12", 1),
    ServerSpec("h4", "10.0.0.13", "00:00:00:00:00:13", "10.255.0.13", 2),
)

SERVER_BY_IP = {s.ip: s for s in SERVERS}
SERVER_BY_HOST = {s.host: s for s in SERVERS}
SERVER_BY_MGMT = {s.mgmt_ip: s for s in SERVERS}

# IP -> MAC map used by the controller's proxy-ARP responder.
ARP_TABLE = {VIP: VIP_MAC, CLIENT_IP: CLIENT_MAC}
ARP_TABLE.update({s.ip: s.mac for s in SERVERS})

# ------------------------------------------------------------------ file io
CONTENT_DIR = os.path.join(BASE_DIR, "servers", "content")
STATE_DIR = os.path.join(BASE_DIR, "state")
RESULTS_DIR = os.path.join(BASE_DIR, "results")
LOGS_DIR = os.path.join(RESULTS_DIR, "logs")
CHARTS_DIR = os.path.join(RESULTS_DIR, "charts")

RESULTS_CSV = os.path.join(RESULTS_DIR, "results.csv")
EVENTS_JSONL = os.path.join(LOGS_DIR, "controller_events.jsonl")
CONTROLLER_LOG = os.path.join(LOGS_DIR, "controller.log")

# --------------------------------------------------------------- demo files
SMALL_FILE = "small.txt"        # ~4 KB   latency measurements
LARGE_FILE = "large.bin"        # 4 MB    throughput measurements

# ------------------------------------------------------------- demo defaults
DEFAULT_REQUESTS = 20
CLIENT_TIMEOUT_S = 10.0


def ensure_dirs() -> None:
    """Create the runtime directories if they do not exist yet."""
    for path in (CONTENT_DIR, STATE_DIR, RESULTS_DIR, LOGS_DIR, CHARTS_DIR):
        os.makedirs(path, exist_ok=True)
