#!/usr/bin/env python3
r"""Mininet topology for the Smart Content Delivery System.

    h1  client 10.0.0.1
         |
        s1  Open vSwitch, OpenFlow 1.3  --->  controller 127.0.0.1:6653
       / | \
     h2  h3  h4     10.0.0.11 / .12 / .13   content servers
      \  |  /
        m1  plain Linux bridge, no OpenFlow
            10.255.0.254 controller side, 10.255.0.11-13 server side

The data plane carries client traffic through the OpenFlow switch.  The
management plane is a separate Linux bridge with no OpenFlow on it at all, so
the controller's health probes are never affected by the rules it installs.

Must run as root (Mininet requirement):

    python3 -m topo                 full scripted demonstration
    python3 -m topo --interactive   topology only, then the Mininet CLI
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import config  # noqa: E402


def sh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


class SCDTopology:
    LINK_ORDER = [config.CLIENT_HOST] + [spec.host for spec in config.SERVERS]

    def __init__(self, delay_ms: int = 150, verbose: bool = False):
        self.delay_ms = delay_ms
        self.verbose = verbose
        self.net = None
        self.hosts = {}
        self.switch = None
        self.controller = None
        self.ports: dict[str, int] = {}

    # ------------------------------------------------------------------ build
    def start(self) -> None:
        from mininet.net import Mininet
        from mininet.node import OVSSwitch, RemoteController
        from mininet.log import setLogLevel

        cleanup_stale_state()
        setLogLevel("info" if self.verbose else "warning")
        net = Mininet(
            controller=RemoteController,
            switch=OVSSwitch,
            build=False,
            autoStaticArp=False,
            ipBase="10.0.0.0/24",
        )
        self.net = net
        self.controller = net.addController(
            "c0", controller=RemoteController, ip="127.0.0.1", port=config.CONTROLLER_PORT
        )
        self.switch = net.addSwitch("s1", cls=OVSSwitch, protocols="OpenFlow13")

        self.hosts[config.CLIENT_HOST] = net.addHost(
            config.CLIENT_HOST, ip="%s/24" % config.CLIENT_IP, mac=config.CLIENT_MAC
        )
        for spec in config.SERVERS:
            self.hosts[spec.host] = net.addHost(spec.host, ip="%s/24" % spec.ip, mac=spec.mac)

        for host_name in self.LINK_ORDER:
            net.addLink(self.hosts[host_name], self.switch)

        net.build()
        net.start()
        self._enforce_macs()
        self._disable_offloads()
        self._setup_management()
        self.ports = self.discover_ports()

    def _enforce_macs(self) -> None:
        """Make the MAC plan in config.py authoritative."""
        for host_name, mac in [(config.CLIENT_HOST, config.CLIENT_MAC)] + [
            (spec.host, spec.mac) for spec in config.SERVERS
        ]:
            host = self.hosts[host_name]
            interface = "%s-eth0" % host_name
            current = host.cmd("cat /sys/class/net/%s/address" % interface).strip()
            if current != mac:
                host.cmd("ip link set dev %s down" % interface)
                host.cmd("ip link set dev %s address %s" % (interface, mac))
                host.cmd("ip link set dev %s up" % interface)
            actual = host.cmd("cat /sys/class/net/%s/address" % interface).strip()
            if actual != mac:
                raise RuntimeError("could not set MAC %s on %s (got %s)" % (mac, interface, actual))

    def _disable_offloads(self) -> None:
        """Checksum offloads confuse rewritten packets on some virtual NICs."""
        if not shutil.which("ethtool"):
            return
        for host_name in self.LINK_ORDER:
            self.hosts[host_name].cmd(
                "ethtool -K %s-eth0 tx off rx off 2>/dev/null || true" % host_name
            )

    def _setup_management(self) -> None:
        """Build the out-of-band management network: a plain Linux bridge."""
        sh("ip", "link", "del", config.MGMT_BRIDGE)
        result = sh("ip", "link", "add", config.MGMT_BRIDGE, "type", "bridge")
        if result.returncode != 0:
            raise RuntimeError("cannot create %s: %s" % (config.MGMT_BRIDGE, result.stderr))
        sh("ip", "addr", "add", "%s/24" % config.CONTROLLER_MGMT_IP, "dev", config.MGMT_BRIDGE)
        sh("ip", "link", "set", config.MGMT_BRIDGE, "up")

        for spec in config.SERVERS:
            host = self.hosts[spec.host]
            inside = "%s-m1" % spec.host
            outside = "m1-%s" % spec.host
            sh("ip", "link", "del", inside)
            result = sh("ip", "link", "add", inside, "type", "veth", "peer", "name", outside)
            if result.returncode != 0:
                raise RuntimeError("cannot create management veth for %s: %s" % (spec.host, result.stderr))
            moved = sh("ip", "link", "set", inside, "netns", str(host.pid))
            if moved.returncode != 0:
                raise RuntimeError("cannot move %s into %s: %s" % (inside, spec.host, moved.stderr))
            sh("ip", "link", "set", outside, "master", config.MGMT_BRIDGE)
            sh("ip", "link", "set", outside, "up")
            host.cmd("ip link set %s up" % inside)
            host.cmd("ip addr add %s/24 dev %s" % (spec.mgmt_ip, inside))
            host.cmd("ip link set lo up")

        # Verify the management plane before anything else depends on it.
        time.sleep(0.3)
        for spec in config.SERVERS:
            result = sh("ping", "-c", "1", "-W", "1", spec.mgmt_ip)
            if result.returncode != 0:
                raise RuntimeError(
                    "management network unreachable: cannot ping %s (%s)"
                    % (spec.mgmt_ip, (result.stderr or result.stdout).strip())
                )

    # ------------------------------------------------------------- port table
    def discover_ports(self) -> dict[str, int]:
        """Map host name -> OpenFlow port number on s1."""
        output = sh("ovs-ofctl", "-O", "OpenFlow13", "show", "s1").stdout
        port_by_interface: dict[str, int] = {}
        for line in output.splitlines():
            line = line.strip()
            if "(" not in line or ")" not in line:
                continue
            number, rest = line.split("(", 1)
            name = rest.split(")", 1)[0]
            if number.isdigit():
                port_by_interface[name] = int(number)

        ports: dict[str, int] = {}
        for index, host_name in enumerate(self.LINK_ORDER, start=1):
            interface = "s1-eth%d" % index
            if interface not in port_by_interface:
                raise RuntimeError(
                    "switch port for %s (%s) not found; ovs-ofctl output was:\n%s"
                    % (host_name, interface, output)
                )
            ports[host_name] = port_by_interface[interface]
        if len(set(ports.values())) != len(ports):
            raise RuntimeError("duplicate switch port numbers: %s" % ports)
        return ports

    def publish_ports(self) -> None:
        """Tell the controller which port each host is on."""
        payload = json.dumps({"ports": self.ports}).encode("utf-8")
        url = "http://%s:%d/ports" % (config.CONTROL_API_IP, config.CONTROL_API_PORT)
        last_error = None
        for _ in range(50):
            request = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
            )
            try:
                with urllib.request.urlopen(request, timeout=2.0) as response:
                    json.loads(response.read().decode("utf-8"))
                    return
            except Exception as exc:  # noqa: BLE001 - retry until the controller is up
                last_error = exc
                time.sleep(0.2)
        raise RuntimeError("controller control API unreachable: %s" % last_error)

    # ---------------------------------------------------------------- servers
    def server_log_path(self, host_name: str) -> str:
        return os.path.join(config.LOGS_DIR, "server_%s.log" % host_name)

    def start_server(self, host_name: str) -> None:
        spec = config.SERVER_BY_HOST[host_name]
        host = self.hosts[host_name]
        host.cmd("pkill -f 'content_server.py.*--name %s'" % host_name)
        command = (
            "cd %s && nohup python3 %s/servers/content_server.py "
            "--name %s --data-ip %s --port %d --delay-ms %d "
            ">> %s 2>&1 &"
            % (
                BASE_DIR,
                BASE_DIR,
                host_name,
                spec.ip,
                config.CONTENT_PORT,
                self.delay_ms,
                self.server_log_path(host_name),
            )
        )
        host.cmd(command)

    def stop_server(self, host_name: str) -> None:
        self.hosts[host_name].cmd("pkill -f 'content_server.py.*--name %s'" % host_name)

    def start_all_servers(self) -> None:
        for spec in config.SERVERS:
            self.start_server(spec.host)
        for spec in config.SERVERS:
            if not self.wait_for_server(spec, timeout=15.0):
                raise RuntimeError(
                    "server %s did not start; see %s"
                    % (spec.host, self.server_log_path(spec.host))
                )

    def wait_for_server(self, spec: config.ServerSpec, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        url = "http://%s:%d/health" % (spec.mgmt_ip, config.CONTENT_PORT)
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=0.5) as response:
                    if response.status == 200:
                        return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.2)
        return False

    # --------------------------------------------------------------- teardown
    def stop(self) -> None:
        for spec in config.SERVERS:
            try:
                self.stop_server(spec.host)
            except Exception:  # noqa: BLE001
                pass
        sh("ip", "link", "del", config.MGMT_BRIDGE)
        if self.net is not None:
            self.net.stop()


def cleanup_stale_state() -> None:
    """Remove anything an interrupted previous run may have left behind.

    Mininet fails with "RTNETLINK answers: File exists" if a host interface
    from a previous run survived, so every run starts from a known-clean state
    instead of relying on the last run having shut down tidily.
    """
    sh("ip", "link", "del", config.MGMT_BRIDGE)
    sh("ovs-vsctl", "--if-exists", "del-br", "s1")
    names = ["%s-eth0" % config.CLIENT_HOST]
    names += ["%s-eth0" % spec.host for spec in config.SERVERS]
    names += ["s1-eth%d" % index for index in range(1, len(config.SERVERS) + 2)]
    for name in names:
        sh("ip", "link", "del", name)


def install_signal_handlers() -> None:
    """Make SIGTERM/SIGHUP unwind through the cleanup block.

    Without this, killing the topology process (or closing the terminal it was
    started from) leaves the s1 bridge and the m1 management bridge behind,
    and the next run has to clean them up by hand.
    """

    def handler(signum, _frame):
        raise SystemExit(128 + signum)

    for signum in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError):  # pragma: no cover - not the main thread
            pass


def preflight() -> None:
    if os.geteuid() != 0:
        raise SystemExit(
            "Mininet must run as root. From Windows use:\n"
            '  wsl.exe -u root -d Ubuntu -- bash -lc "cd %s && ./run_demo.sh"\n'
            "From inside WSL use:  sudo python3 -m topo" % BASE_DIR
        )
    for binary in ("mn", "ovs-vsctl", "ovs-ofctl"):
        if not shutil.which(binary):
            raise SystemExit("required tool not found: %s" % binary)


def controller_status() -> dict:
    url = "http://%s:%d/status" % (config.CONTROL_API_IP, config.CONTROL_API_PORT)
    with urllib.request.urlopen(url, timeout=3.0) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_controller(timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            return controller_status()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.3)
    raise SystemExit("controller is not answering on port %d (%s)" % (config.CONTROL_API_PORT, last_error))


def main() -> int:
    parser = argparse.ArgumentParser(description="SCD Mininet topology")
    parser.add_argument("--interactive", action="store_true", help="hand over to the Mininet CLI")
    parser.add_argument("--delay-ms", type=int, default=150, help="simulated server delay")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    preflight()
    install_signal_handlers()
    config.ensure_dirs()
    wait_for_controller()
    topology = SCDTopology(delay_ms=args.delay_ms, verbose=args.verbose)
    exit_code = 0
    try:
        topology.start()
        topology.publish_ports()
        topology.start_all_servers()
        print("topology ready: s1 + h1..h4, management bridge m1")
        print("switch ports: %s" % topology.ports)
        if args.interactive:
            from mininet.cli import CLI

            CLI(topology.net)
        else:
            from demo import run_demonstration

            exit_code = run_demonstration(topology)
    finally:
        topology.stop()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
