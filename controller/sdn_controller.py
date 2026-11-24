#!/usr/bin/env python3
"""Smart Content Delivery System - OpenFlow 1.3 controller.

This is a real OpenFlow controller, not a load balancer wearing an SDN costume.
It speaks the wire protocol itself (see controller/of13.py), and every
forwarding decision it makes becomes an entry in the switch's flow table.

What it does
------------
1.  Accepts the switch's OpenFlow connection on port 6653 and completes the
    HELLO / FEATURES handshake.
2.  Installs the base rules: ARP to the controller, new TCP connections to the
    VIP to the controller, and an explicit table-miss drop.
3.  Answers ARP for the virtual IP (and for the real hosts), so the client can
    reach 10.0.0.100 without any real host owning that address.
4.  On the first packet of every TCP connection to the VIP it picks a healthy
    server (fewest active connections, then lowest probe RTT), installs a pair
    of per-connection flows that rewrite the destination and the reply source,
    and forwards that first packet.
5.  Polls /stats of all servers over the management bridge.  When a server dies
    it deletes every flow that points at it, so new connections are steered to
    a healthy server and the switch itself stops sending traffic to the corpse.

The controller never sits in the data path: after the first packet of a
connection, all traffic is handled by the switch at line rate.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import socket
import struct
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import config  # noqa: E402
from controller import of13  # noqa: E402
from controller import selector  # noqa: E402
from controller.monitor import HealthMonitor  # noqa: E402


log = logging.getLogger("controller")

# ------------------------------------------------------------------ constants
KIND_CONN_REQUEST = 1
KIND_CONN_REPLY = 2
KIND_ARP = 3
COOKIE_PREFIX = 0x1A
COOKIE_MASK_SERVER = 0xFF00FF0000000000  # version byte + server byte, ignores kind
FLOW_PRIORITY_CONNECTION = 300
FLOW_PRIORITY_ARP = 200
FLOW_PRIORITY_VIP = 200
FLOW_PRIORITY_TABLE_MISS = 0
CONN_IDLE_TIMEOUT = 60
CONN_CACHE_TTL = 120.0
# How long a connection the controller has just steered still counts as load.
# The health monitor samples once a second, so without this a burst of
# simultaneous connections would all be steered to the same idle server before
# any probe could observe that it became busy.
STEER_LOAD_WINDOW_S = 0.5


COOKIE_NO_SERVER = -1  # encodes a server byte of 0, which no real server uses


def cookie(kind: int, server_index: int, seq: int = 0) -> int:
    """Build a flow cookie.

    Layout: 0x1a | kind | server index + 1 | sequence.

    Rules that belong to no particular server (the ARP responder and the VIP
    catch-all) must use COOKIE_NO_SERVER.  If they used index 0 they would be
    encoded with the same server byte as h2 and would be deleted when h2 dies,
    which silently breaks ARP and therefore the whole data path.
    """
    return (
        (COOKIE_PREFIX << 56) | ((kind & 0xFF) << 48) | (((server_index + 1) & 0xFF) << 40) | seq
    )


def u16(value: int) -> bytes:
    return struct.pack("!H", value)


def u32(value: int) -> bytes:
    return struct.pack("!I", value)


def parse_ipv4_tcp(payload: bytes):
    """Return (src_ip, dst_ip, src_port, dst_port) for IPv4/TCP, else None."""
    if len(payload) < 20:
        return None
    version_ihl = payload[0]
    if version_ihl >> 4 != 4:
        return None
    ihl = (version_ihl & 0x0F) * 4
    if len(payload) < ihl + 20:
        return None
    if payload[9] != 6:  # protocol: TCP
        return None
    src_ip = socket.inet_ntoa(payload[12:16])
    dst_ip = socket.inet_ntoa(payload[16:20])
    src_port, dst_port = struct.unpack("!HH", payload[ihl : ihl + 4])
    return src_ip, dst_ip, src_port, dst_port


# --------------------------------------------------------------------- switch
class SwitchConnection(threading.Thread):
    """One OpenFlow channel to one switch."""

    def __init__(self, sock: socket.socket, addr, controller: "SdnController"):
        super().__init__(name="switch-%s" % (addr,), daemon=True)
        self.sock = sock
        self.addr = addr
        self.controller = controller
        self.dpid = None
        self.connected = False
        self._send_lock = threading.Lock()
        self._xid = 100
        self._barriers = {}
        self._stop = threading.Event()
        self.n_buffers = 0
        self.n_tables = 0

    # ------------------------------------------------------------------ wire
    def next_xid(self) -> int:
        with self._send_lock:
            self._xid = (self._xid + 1) & 0xFFFFFFFF
            return self._xid

    def send(self, payload: bytes) -> None:
        with self._send_lock:
            self.sock.sendall(payload)

    def barrier(self, timeout: float = 2.0) -> bool:
        xid = self.next_xid()
        event = threading.Event()
        self._barriers[xid] = event
        self.send(of13.barrier_request(xid))
        done = event.wait(timeout)
        self._barriers.pop(xid, None)
        return done

    # ------------------------------------------------------------------ loop
    def run(self) -> None:
        self.sock.settimeout(1.0)
        self.send(of13.hello(self.next_xid()))
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = self.sock.recv(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            while len(buffer) >= 8:
                version, msg_type, length, xid = of13.parse_header(buffer)
                if length < 8 or len(buffer) < length:
                    break
                raw = buffer[:length]
                buffer = buffer[length:]
                try:
                    self._handle(of13.Message(msg_type, xid, raw[8:], raw))
                except Exception:  # pragma: no cover - defensive
                    log.exception("failed to handle OpenFlow message type %d", msg_type)
        self._stop.set()
        self.connected = False
        log.warning("switch connection %s closed", self.addr)
        self.controller.on_switch_down(self)

    def close(self) -> None:
        self._stop.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    # ---------------------------------------------------------------- handlers
    def _handle(self, message: of13.Message) -> None:
        if message.msg_type == of13.OFPT_HELLO:
            self.send(of13.features_request(self.next_xid()))
        elif message.msg_type == of13.OFPT_ECHO_REQUEST:
            self.send(of13.echo_reply(message.xid, message.body))
        elif message.msg_type == of13.OFPT_FEATURES_REPLY:
            dpid, n_buffers, n_tables = of13.parse_features_reply(message.body)
            self.dpid = dpid
            self.n_buffers = n_buffers
            self.n_tables = n_tables
            self.connected = True
            log.info(
                "switch connected: dpid=0x%016x buffers=%d tables=%d (%s)",
                dpid,
                n_buffers,
                n_tables,
                self.addr,
            )
            self.controller.on_switch_ready(self)
        elif message.msg_type == of13.OFPT_BARRIER_REPLY:
            event = self._barriers.get(message.xid)
            if event:
                event.set()
        elif message.msg_type == of13.OFPT_PACKET_IN:
            payload = of13.parse_packet_in(message.body)
            # Never do the work on this thread: handling a packet-in installs
            # flows and may wait on a barrier, and only this thread can read
            # the barrier reply.  Hand it to the packet-in worker instead.
            self.controller.enqueue_packet_in(self, payload)
        elif message.msg_type == of13.OFPT_PORT_STATUS:
            reason = message.body[0] if message.body else 0
            log.info("port status change (reason=%d)", reason)
        elif message.msg_type == of13.OFPT_ERROR:
            err_type, err_code = struct.unpack("!HH", message.body[:4])
            detail = message.body[4:]
            log.error(
                "OpenFlow error from switch: type=%d code=%d detail=%s",
                err_type,
                err_code,
                detail[:64].hex(),
            )
        elif message.msg_type in (of13.OFPT_MULTIPART_REPLY, of13.OFPT_EXPERIMENTER):
            pass
        else:
            log.debug("ignoring OpenFlow message type %d", message.msg_type)


# ------------------------------------------------------------------ controller
class SdnController:
    def __init__(self):
        self.monitor = HealthMonitor(on_transition=self._on_transition)
        self.conn: SwitchConnection | None = None
        self.ports: dict[str, int] = {}
        self.lock = threading.RLock()
        self.started_at = time.time()
        self.last_switch_down = 0.0

        self.active_host: str | None = None
        self.active_since = 0.0
        self.conn_cache: dict[int, tuple[str, float]] = {}
        self.steer_log: dict[str, list[float]] = {spec.host: [] for spec in config.SERVERS}
        self.counters = {
            "packet_ins": 0,
            "arp_replies": 0,
            "connections_steered": 0,
            "flows_installed": 0,
            "flows_deleted": 0,
            "failovers": 0,
        }
        self.decisions: list[dict] = []
        self.last_reason = ""
        self._packet_queue: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._events_lock = threading.Lock()

    # ------------------------------------------------------------- event log
    def event(self, kind: str, message: str, **fields) -> None:
        record = {
            "time": round(time.time(), 3),
            "iso": datetime.now().isoformat(timespec="milliseconds"),
            "kind": kind,
            "message": message,
        }
        record.update(fields)
        with self._events_lock:
            try:
                os.makedirs(config.LOGS_DIR, exist_ok=True)
                with open(config.EVENTS_JSONL, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record) + "\n")
            except OSError:
                pass
        log.info("%s: %s", kind, message)

    def _record_decision(self, spec, reason: str, source_port: int | None = None) -> None:
        entry = {
            "iso": datetime.now().isoformat(timespec="milliseconds"),
            "time": round(time.time(), 3),
            "host": spec.host if spec else None,
            "ip": spec.ip if spec else None,
            "reason": reason,
            "source_port": source_port,
        }
        self.last_reason = reason
        self.decisions.append(entry)
        if len(self.decisions) > 100:
            del self.decisions[:-100]
        self.event(
            "selection",
            reason,
            reason=reason,
            host=spec.host if spec else None,
            ip=spec.ip if spec else None,
            source_port=source_port,
        )

    # ------------------------------------------------------------------ setup
    def start(self) -> None:
        self.monitor.start()
        threading.Thread(target=self._serve_openflow, name="of-listener", daemon=True).start()
        threading.Thread(target=self._serve_control_api, name="control-api", daemon=True).start()
        threading.Thread(target=self._housekeeping, name="housekeeping", daemon=True).start()
        threading.Thread(target=self._packet_worker, name="packet-in", daemon=True).start()
        self.event("startup", "controller listening on port %d" % config.CONTROLLER_PORT)

    def stop(self) -> None:
        self._stop.set()
        self.monitor.stop()
        if self._listener:
            try:
                self._listener.close()
            except OSError:
                pass

    def enqueue_packet_in(self, connection: SwitchConnection, packet_in: dict) -> None:
        self._packet_queue.put((connection, packet_in))

    def _packet_worker(self) -> None:
        """Handle packet-ins off the switch reader thread."""
        while not self._stop.is_set():
            try:
                connection, packet_in = self._packet_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.on_packet_in(connection, packet_in)
            except Exception:  # pragma: no cover - keep the worker alive
                log.exception("packet-in handling failed")

    def _serve_openflow(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((config.CONTROLLER_LISTEN_IP, config.CONTROLLER_PORT))
        listener.listen(8)
        listener.settimeout(1.0)
        self._listener = listener
        log.info("OpenFlow listener bound to %s:%d", config.CONTROLLER_LISTEN_IP, config.CONTROLLER_PORT)
        while not self._stop.is_set():
            try:
                sock, addr = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.event("switch_connect", "switch channel opened from %s:%d" % addr)
            connection = SwitchConnection(sock, addr, self)
            with self.lock:
                self.conn = connection
            connection.start()

    def on_switch_ready(self, connection: SwitchConnection) -> None:
        """Install the base pipeline as soon as the handshake finishes."""
        with self.lock:
            self._install_base_rules(connection)
        if self.ports:
            with self.lock:
                self._install_reply_scaffolding(connection)

    def on_switch_down(self, connection: SwitchConnection) -> None:
        with self.lock:
            if self.conn is connection:
                self.conn = None
        self.last_switch_down = time.time()

    # ------------------------------------------------------------------- rules
    def _install_base_rules(self, connection: SwitchConnection) -> None:
        """ARP to the controller, VIP SYNs to the controller, everything else dropped."""
        arp_rule = of13.add_flow(
            [of13.oxm(of13.OXM_ETH_TYPE, u16(of13.ETH_TYPE_ARP))],
            [of13.action_output(of13.OFPP_CONTROLLER, 0xFFFF)],
            priority=FLOW_PRIORITY_ARP,
            cookie=cookie(KIND_ARP, COOKIE_NO_SERVER),
            xid=connection.next_xid(),
        )
        connection.send(arp_rule)

        vip_rule = of13.add_flow(
            [
                of13.oxm(of13.OXM_ETH_TYPE, u16(of13.ETH_TYPE_IP)),
                of13.oxm(of13.OXM_IPV4_DST, of13.ip_bytes(config.VIP)),
                of13.oxm(of13.OXM_IP_PROTO, b"\x06"),
                of13.oxm(of13.OXM_TCP_DST, u16(config.CONTENT_PORT)),
            ],
            [of13.action_output(of13.OFPP_CONTROLLER, 128)],
            priority=FLOW_PRIORITY_VIP,
            cookie=cookie(KIND_CONN_REQUEST, COOKIE_NO_SERVER),
            xid=connection.next_xid(),
        )
        connection.send(vip_rule)

        # Explicit table-miss drop: nothing is forwarded unless the controller
        # put a rule in the table.  This is what makes the demonstration strict.
        table_miss = of13.flow_mod(
            of13.build_match([]),
            [],
            priority=FLOW_PRIORITY_TABLE_MISS,
            cookie=0,
            xid=connection.next_xid(),
        )
        connection.send(table_miss)
        # No barrier here: it would run on the switch reader thread, which is the
        # only thread able to read the barrier reply.  OpenFlow messages on one
        # connection are processed in order, so the rules are active before the
        # first packet-in can arrive.
        self.counters["flows_installed"] += 3
        self.event(
            "base_rules",
            "installed ARP-to-controller, VIP-to-controller and table-miss drop",
            dpid=("%016x" % connection.dpid) if connection.dpid else None,
        )

    def set_ports(self, ports: dict) -> None:
        """Called by the topology once the switch port numbers are known."""
        with self.lock:
            self.ports = {str(k): int(v) for k, v in ports.items()}
            if self.conn and self.conn.connected:
                self._install_reply_scaffolding(self.conn)
        self.event("ports", "learned switch port map", ports=self.ports)

    def _install_reply_scaffolding(self, connection: SwitchConnection) -> None:
        """Replies from any server to the client must look like they come from the VIP.

        These rules are intentionally static: they only rewrite the source MAC
        back to the VIP MAC.  The per-connection rules installed later are the
        ones that decide *which* traffic is allowed and where it goes.
        """
        client_port = self.ports.get(config.CLIENT_HOST)
        if client_port is None:
            return
        for spec in config.SERVERS:
            server_port = self.ports.get(spec.host)
            if server_port is None:
                continue
            match = [
                of13.oxm(of13.OXM_IN_PORT, u32(server_port)),
                of13.oxm(of13.OXM_ETH_TYPE, u16(of13.ETH_TYPE_IP)),
                of13.oxm(of13.OXM_IPV4_SRC, of13.ip_bytes(spec.ip)),
                of13.oxm(of13.OXM_IP_PROTO, b"\x06"),
                of13.oxm(of13.OXM_TCP_SRC, u16(config.CONTENT_PORT)),
            ]
            actions = [of13.action_set_field(of13.OXM_ETH_SRC, of13.mac_bytes(config.VIP_MAC))]
            connection.send(
                of13.add_flow(
                    match,
                    actions,
                    priority=FLOW_PRIORITY_CONNECTION - 100,
                    cookie=cookie(KIND_CONN_REPLY, spec.index),
                    xid=connection.next_xid(),
                )
            )

    def _install_connection_flows(
        self, connection: SwitchConnection, spec: config.ServerSpec, source_port: int
    ) -> bool:
        """Install the flow pair that pins one client connection to one server."""
        client_port = self.ports[config.CLIENT_HOST]
        server_port = self.ports[spec.host]

        request_match = [
            of13.oxm(of13.OXM_IN_PORT, u32(client_port)),
            of13.oxm(of13.OXM_ETH_TYPE, u16(of13.ETH_TYPE_IP)),
            of13.oxm(of13.OXM_IPV4_DST, of13.ip_bytes(config.VIP)),
            of13.oxm(of13.OXM_IP_PROTO, b"\x06"),
            of13.oxm(of13.OXM_TCP_DST, u16(config.CONTENT_PORT)),
            of13.oxm(of13.OXM_TCP_SRC, u16(source_port)),
        ]
        request_actions = [
            of13.action_set_field(of13.OXM_ETH_DST, of13.mac_bytes(spec.mac)),
            of13.action_set_field(of13.OXM_IPV4_DST, of13.ip_bytes(spec.ip)),
            of13.action_output(server_port),
        ]
        reply_match = [
            of13.oxm(of13.OXM_IN_PORT, u32(server_port)),
            of13.oxm(of13.OXM_ETH_TYPE, u16(of13.ETH_TYPE_IP)),
            of13.oxm(of13.OXM_IPV4_SRC, of13.ip_bytes(spec.ip)),
            of13.oxm(of13.OXM_IP_PROTO, b"\x06"),
            of13.oxm(of13.OXM_TCP_SRC, u16(config.CONTENT_PORT)),
            of13.oxm(of13.OXM_TCP_DST, u16(source_port)),
        ]
        reply_actions = [
            of13.action_set_field(of13.OXM_ETH_SRC, of13.mac_bytes(config.VIP_MAC)),
            of13.action_set_field(of13.OXM_IPV4_SRC, of13.ip_bytes(config.VIP)),
            of13.action_output(client_port),
        ]

        connection.send(
            of13.flow_mod(
                of13.build_match(request_match),
                [of13.instruction_apply_actions(request_actions)],
                priority=FLOW_PRIORITY_CONNECTION,
                cookie=cookie(KIND_CONN_REQUEST, spec.index, source_port),
                idle_timeout=CONN_IDLE_TIMEOUT,
                xid=connection.next_xid(),
            )
        )
        connection.send(
            of13.flow_mod(
                of13.build_match(reply_match),
                [of13.instruction_apply_actions(reply_actions)],
                priority=FLOW_PRIORITY_CONNECTION,
                cookie=cookie(KIND_CONN_REPLY, spec.index, source_port),
                idle_timeout=CONN_IDLE_TIMEOUT,
                xid=connection.next_xid(),
            )
        )
        self.counters["flows_installed"] += 2
        # The flow_mods above and the packet_out that follows travel on the same
        # connection, so the switch installs them before forwarding the packet.
        return True

    def _delete_server_flows(self, server_index: int) -> int:
        """Delete every flow whose cookie names this server (request + reply)."""
        if not 0 <= server_index < len(config.SERVERS):
            raise ValueError("refusing to delete with invalid server index %r" % (server_index,))
        connection = self.conn
        if connection is None or not connection.connected:
            return 0
        with self.lock:
            connection.send(
                of13.flow_mod(
                    of13.build_match([]),
                    [],
                    priority=0,
                    cookie=cookie(0, server_index) & COOKIE_MASK_SERVER,
                    cookie_mask=COOKIE_MASK_SERVER,
                    command=of13.OFPFC_DELETE,
                    xid=connection.next_xid(),
                )
            )
            connection.barrier()
            self.counters["flows_deleted"] += 1
        self.event(
            "flow_delete",
            "removed all flows pointing at server index %d" % (server_index + 1),
            server_index=server_index,
        )
        return 1

    # -------------------------------------------------------------- packet-in
    def on_packet_in(self, connection: SwitchConnection, packet_in: dict) -> None:
        self.counters["packet_ins"] += 1
        frame = packet_in["frame"]
        if len(frame) < 14:
            return
        ethernet = of13.EthernetFrame(frame)

        if ethernet.eth_type == of13.ETH_TYPE_ARP:
            self._handle_arp(connection, packet_in, ethernet)
            return
        if ethernet.eth_type != of13.ETH_TYPE_IP:
            return

        parsed = parse_ipv4_tcp(ethernet.payload)
        if not parsed:
            return
        _src_ip, dst_ip, source_port, dest_port = parsed
        if dst_ip != config.VIP or dest_port != config.CONTENT_PORT:
            return
        if config.CLIENT_HOST not in self.ports:
            log.warning("packet-in before the port map was learned; dropping")
            return

        spec, reason = self._choose_for_connection(source_port)
        if spec is None:
            self.event("no_server", reason)
            return

        if not self._install_connection_flows(connection, spec, source_port):
            log.warning("barrier timed out while installing per-connection flows")

        self._forward_first_packet(connection, packet_in, spec)
        self.counters["connections_steered"] += 1
        self.active_host = spec.host
        self.active_since = time.time()
        self._record_decision(spec, reason, source_port)

    def _choose_for_connection(self, source_port: int):
        """Pick the server for one new connection, reusing a live mapping if any."""
        now = time.time()
        cached = self.conn_cache.get(source_port)
        if cached:
            host, seen = cached
            if now - seen < CONN_CACHE_TTL:
                state = self.monitor.states.get(host)
                if state and state.healthy:
                    spec = config.SERVER_BY_HOST[host]
                    return spec, (
                        "keep %s for source port %d: connection already steered"
                        % (spec.host, source_port)
                    )

        extra_load = self._recent_steers()
        states = [self.monitor.states[spec.host] for spec in config.SERVERS]
        candidates = selector.to_candidates(states, list(config.SERVERS), extra_load)
        spec, reason = selector.choose(candidates, None)
        if spec is not None:
            self.conn_cache[source_port] = (spec.host, now)
            self.steer_log[spec.host].append(now)
        return (config.SERVER_BY_HOST[spec.host] if spec else None), reason

    def _recent_steers(self) -> dict[str, int]:
        """Count connections steered recently, per server, pruning old entries."""
        now = time.time()
        counts: dict[str, int] = {}
        for host, stamps in self.steer_log.items():
            fresh = [stamp for stamp in stamps if now - stamp < STEER_LOAD_WINDOW_S]
            self.steer_log[host] = fresh
            counts[host] = len(fresh)
        return counts

    def _forward_first_packet(self, connection: SwitchConnection, packet_in: dict, spec) -> None:
        """Send the first packet of the connection on with the destination rewritten."""
        server_port = self.ports[spec.host]
        actions = [
            of13.action_set_field(of13.OXM_ETH_DST, of13.mac_bytes(spec.mac)),
            of13.action_set_field(of13.OXM_IPV4_DST, of13.ip_bytes(spec.ip)),
            of13.action_output(server_port),
        ]
        buffer_id = packet_in.get("buffer_id", 0xFFFFFFFF)
        data = b"" if buffer_id != 0xFFFFFFFF else packet_in["frame"]
        connection.send(
            of13.packet_out(
                in_port=packet_in.get("in_port") or self.ports[config.CLIENT_HOST],
                actions=actions,
                buffer_id=buffer_id,
                data=data,
                xid=connection.next_xid(),
            )
        )

    def _handle_arp(self, connection: SwitchConnection, packet_in: dict, ethernet) -> None:
        request = of13.parse_arp_request(ethernet.payload)
        if request is None:
            return
        sender_mac, sender_ip, target_ip = request
        target_mac = config.ARP_TABLE.get(target_ip)
        if target_mac is None:
            return
        reply = of13.build_arp_reply(
            target_mac=sender_mac,
            sender_mac=of13.mac_bytes(target_mac),
            sender_ip=target_ip,
            target_ip=sender_ip,
        )
        in_port = packet_in.get("in_port") or 1
        # Output to OFPP_IN_PORT, not to the ingress port number: Open vSwitch
        # drops a packet whose output port equals its input port unless the
        # virtual OFPP_IN_PORT port is named explicitly.  This is the single
        # most important detail in the whole ARP responder.
        connection.send(
            of13.packet_out(
                in_port=in_port,
                actions=[of13.action_output(of13.OFPP_IN_PORT)],
                buffer_id=0xFFFFFFFF,
                data=reply,
                xid=connection.next_xid(),
            )
        )
        self.counters["arp_replies"] += 1

    # ------------------------------------------------------------- supervision
    def _on_transition(self, state, direction: str) -> None:
        self.event(
            "server_%s" % direction.lower(),
            "server %s (%s) is %s" % (state.host, state.ip, direction),
            host=state.host,
            ip=state.ip,
            error=state.last_error,
        )
        if direction == "DOWN":
            spec = config.SERVER_BY_HOST[state.host]
            self._delete_server_flows(spec.index)
            self.conn_cache = {k: v for k, v in self.conn_cache.items() if v[0] != state.host}
            self.counters["failovers"] += 1
            self.event(
                "failover",
                "server %s removed from the pool; new connections will use a healthy server"
                % state.host,
                host=state.host,
            )

    def _housekeeping(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            self.conn_cache = {
                port: entry
                for port, entry in self.conn_cache.items()
                if now - entry[1] < CONN_CACHE_TTL
            }
            self._stop.wait(5.0)

    # ------------------------------------------------------------- status view
    def status(self) -> dict:
        with self.lock:
            states = self.monitor.snapshot()
            connection = self.conn
            return {
                "controller": {
                    "uptime_s": round(time.time() - self.started_at, 2),
                    "of_port": config.CONTROLLER_PORT,
                    "api_port": config.CONTROL_API_PORT,
                },
                "switch": {
                    "connected": bool(connection and connection.connected),
                    "dpid": ("%016x" % connection.dpid) if connection and connection.dpid else None,
                    "ports": dict(self.ports),
                },
                "vip": config.VIP,
                "active_host": self.active_host,
                "servers": {host: state.as_dict() for host, state in states.items()},
                "decisions": self.decisions[-15:],
                "last_reason": self.last_reason,
                "counters": dict(self.counters),
            }

    # ------------------------------------------------------------- control api
    def _serve_control_api(self) -> None:
        controller = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, fmt, *args):  # silence per-request logging
                return

            def _json(self, payload: dict, status: int = 200) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if self.path in ("/status", "/"):
                    self._json(controller.status())
                elif self.path == "/healthz":
                    self._json({"ok": True})
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    self._json({"error": "invalid json"}, 400)
                    return
                if self.path == "/ports":
                    controller.set_ports(payload.get("ports", payload))
                    self._json({"ok": True, "ports": controller.ports})
                else:
                    self._json({"error": "not found"}, 404)

        server = ThreadingHTTPServer(
            (config.CONTROL_API_IP, config.CONTROL_API_PORT), Handler
        )
        server.daemon_threads = True
        log.info("control API listening on %s:%d", config.CONTROL_API_IP, config.CONTROL_API_PORT)
        server.serve_forever()


# ------------------------------------------------------------------------ main
def main() -> int:
    parser = argparse.ArgumentParser(description="Smart Content Delivery SDN controller")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config.ensure_dirs()
    controller = SdnController()
    controller.start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        controller.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
