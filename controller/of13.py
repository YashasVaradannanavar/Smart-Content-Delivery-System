"""Minimal OpenFlow 1.3 (wire version 0x04) encoder and decoder.

Standard library only: no Ryu, no OS-Ken, no third-party packages.  This module
implements exactly the subset of the protocol the project needs:

    HELLO, ECHO_REQUEST/REPLY, FEATURES_REQUEST/REPLY,
    PACKET_IN (decode), PACKET_OUT (build), FLOW_MOD (build),
    BARRIER_REQUEST/REPLY, ERROR (decode)

Every byte layout follows the OpenFlow 1.3.5 specification, section 7.
"""

import socket
import struct

OFP_VERSION = 0x04

# ---------------------------------------------------------------- message types
OFPT_HELLO = 0
OFPT_ERROR = 1
OFPT_ECHO_REQUEST = 2
OFPT_ECHO_REPLY = 3
OFPT_EXPERIMENTER = 4
OFPT_FEATURES_REQUEST = 5
OFPT_FEATURES_REPLY = 6
OFPT_PACKET_IN = 10
OFPT_FLOW_REMOVED = 11
OFPT_PORT_STATUS = 12
OFPT_PACKET_OUT = 13
OFPT_FLOW_MOD = 14
OFPT_MULTIPART_REQUEST = 18
OFPT_MULTIPART_REPLY = 19
OFPT_BARRIER_REQUEST = 20
OFPT_BARRIER_REPLY = 21

# ------------------------------------------------------------------ port numbers
OFPP_MAX = 0xFFFFFF00
OFPP_IN_PORT = 0xFFFFFFF8
OFPP_CONTROLLER = 0xFFFFFFFD
# "no restriction" values used by flow_mod delete requests.  They are NOT the
# same as OFPP_MAX: using OFPP_MAX there filters on a literal output port and
# silently matches nothing.
OFPP_ANY = 0xFFFFFFFF
OFPG_ANY = 0xFFFFFFFF

# ------------------------------------------------------------- flow-mod commands
OFPFC_ADD = 0
OFPFC_MODIFY = 1
OFPFC_MODIFY_STRICT = 2
OFPFC_DELETE = 3
OFPFC_DELETE_STRICT = 4

# ------------------------------------------------------------------- oxm basics
OFPMT_OXM = 1
OFPXMC_OPENFLOW_BASIC = 0x8000

OXM_IN_PORT = 0
OXM_ETH_DST = 3
OXM_ETH_SRC = 4
OXM_ETH_TYPE = 5
OXM_IP_PROTO = 10
OXM_IPV4_SRC = 11
OXM_IPV4_DST = 12
OXM_TCP_SRC = 13
OXM_TCP_DST = 14
OXM_UDP_SRC = 15
OXM_UDP_DST = 16
OXM_ARP_OP = 21
OXM_ARP_SPA = 22
OXM_ARP_TPA = 23

ETH_TYPE_IP = 0x0800
ETH_TYPE_ARP = 0x0806

# --------------------------------------------------------------------- actions
OFPAT_OUTPUT = 0
OFPAT_SET_FIELD = 25

# ---------------------------------------------------------------- instructions
OFPIT_GOTO_TABLE = 1
OFPIT_WRITE_ACTIONS = 3
OFPIT_APPLY_ACTIONS = 4

# --------------------------------------------------------------- packet-in reasons
OFPR_NO_MATCH = 0
OFPR_ACTION = 1
OFPR_INVALID_TTL = 2


# --------------------------------------------------------------------- helpers
def mac_bytes(mac: str) -> bytes:
    """'00:11:22:33:44:55' -> 6 raw bytes."""
    return bytes(int(part, 16) for part in mac.split(":"))


def mac_str(raw: bytes) -> str:
    return ":".join("%02x" % b for b in raw)


def ip_bytes(ip: str) -> bytes:
    return socket.inet_aton(ip)


def ip_str(raw: bytes) -> str:
    return socket.inet_ntoa(raw)


def message(msg_type: int, body: bytes, xid: int = 0) -> bytes:
    return struct.pack("!BBHI", OFP_VERSION, msg_type, 8 + len(body), xid) + body


def hello(xid: int = 1) -> bytes:
    # A version bitmap element is optional; a bare 8-byte HELLO is legal and OVS
    # accepts it, negotiating the lower of the two versions (both are 1.3 here).
    return message(OFPT_HELLO, b"", xid)


def echo_reply(xid: int, payload: bytes = b"") -> bytes:
    return message(OFPT_ECHO_REPLY, payload, xid)


def features_request(xid: int = 2) -> bytes:
    return message(OFPT_FEATURES_REQUEST, b"", xid)


def barrier_request(xid: int) -> bytes:
    return message(OFPT_BARRIER_REQUEST, b"", xid)


def oxm(field: int, value: bytes) -> bytes:
    """One OXM TLV: class(2) field/mask(1) length(1) value(n)."""
    return struct.pack("!HBB", OFPXMC_OPENFLOW_BASIC, field << 1, len(value)) + value


def build_match(fields) -> bytes:
    """Build an ofp_match from a list of OXM TLVs.

    The length field excludes the padding (OpenFlow 1.3.5, 7.2.3.2), while the
    serialised match is padded to a multiple of eight bytes.  Open vSwitch
    computes the end of the match as round_up(length, 8), so the two must
    agree exactly or the flow-mod is rejected.
    """
    body = b"".join(fields)
    length = 4 + len(body)
    pad = (-length) % 8
    return struct.pack("!HH", OFPMT_OXM, length) + body + b"\x00" * pad


def action_output(port: int, max_len: int = 0) -> bytes:
    return struct.pack("!HHIH6x", OFPAT_OUTPUT, 16, port, max_len)


def action_set_field(field: int, value: bytes) -> bytes:
    tlv = oxm(field, value)
    length = 4 + len(tlv)
    pad = (8 - length % 8) % 8
    return struct.pack("!HH", OFPAT_SET_FIELD, length + pad) + tlv + b"\x00" * pad


def instruction_apply_actions(actions) -> bytes:
    body = b"".join(actions)
    return struct.pack("!HH4x", OFPIT_APPLY_ACTIONS, 8 + len(body)) + body


def flow_mod(
    match: bytes,
    instructions,
    priority: int = 100,
    cookie: int = 0,
    command: int = OFPFC_ADD,
    buffer_id: int = 0xFFFFFFFF,
    out_port: int = OFPP_ANY,
    out_group: int = OFPG_ANY,
    flags: int = 0,
    table_id: int = 0,
    cookie_mask: int = 0,
    idle_timeout: int = 0,
    hard_timeout: int = 0,
    xid: int = 0,
) -> bytes:
    """Build an OFPT_FLOW_MOD (ofp_flow_mod, 48 bytes of fixed fields)."""
    body = struct.pack(
        "!QQBBHHHIIIH2x",
        cookie,
        cookie_mask,
        table_id,
        command,
        idle_timeout,
        hard_timeout,
        priority,
        buffer_id,
        out_port,
        out_group,
        flags,
    )
    body += match
    for instruction in instructions:
        body += instruction
    return message(OFPT_FLOW_MOD, body, xid)


def add_flow(match_fields, actions, priority=100, cookie=0, idle_timeout=0, xid=0) -> bytes:
    """Convenience wrapper: match fields + apply-actions -> FLOW_MOD add."""
    return flow_mod(
        build_match(match_fields),
        [instruction_apply_actions(actions)],
        priority=priority,
        cookie=cookie,
        idle_timeout=idle_timeout,
        command=OFPFC_ADD,
        xid=xid,
    )


def delete_flow(match_fields, priority=0, cookie=0, cookie_mask=0, xid=0) -> bytes:
    """Non-strict delete: removes every flow matching these fields."""
    return flow_mod(
        build_match(match_fields),
        [],
        priority=priority,
        cookie=cookie,
        cookie_mask=cookie_mask,
        command=OFPFC_DELETE,
        xid=xid,
    )


def packet_out(
    in_port: int, actions, buffer_id: int = 0xFFFFFFFF, data: bytes = b"", xid=0
) -> bytes:
    body = b"".join(actions)
    return message(
        OFPT_PACKET_OUT,
        struct.pack("!IIH6x", buffer_id, in_port, len(body)) + body + data,
        xid,
    )


# -------------------------------------------------------------------- decoding
class Message:
    """A decoded OpenFlow message header plus its raw body."""

    __slots__ = ("msg_type", "xid", "body", "raw")

    def __init__(self, msg_type: int, xid: int, body: bytes, raw: bytes):
        self.msg_type = msg_type
        self.xid = xid
        self.body = body
        self.raw = raw

    @property
    def version(self) -> int:
        return self.raw[0]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<Message type=%d xid=%d len=%d>" % (
            self.msg_type,
            self.xid,
            len(self.raw),
        )


def parse_header(raw: bytes):
    """Return (version, msg_type, length, xid) from an 8-byte header."""
    return struct.unpack("!BBHI", raw[:8])


def parse_oxm_list(data: bytes):
    """Parse a run of OXM TLVs into {field_number: raw_value_bytes}."""
    fields = {}
    offset = 0
    while offset + 4 <= len(data):
        _cls, field_and_mask, length = struct.unpack("!HBB", data[offset : offset + 4])
        if length == 0:  # zero padding at the end of the match
            break
        if offset + 4 + length > len(data):
            break
        value = data[offset + 4 : offset + 4 + length]
        fields[field_and_mask >> 1] = value
        offset += 4 + length
    return fields


def parse_features_reply(body: bytes):
    """Return (datapath_id, n_buffers, n_tables)."""
    dpid, n_buffers, n_tables = struct.unpack("!QIB", body[:13])
    return dpid, n_buffers, n_tables


def parse_packet_in(body: bytes):
    """Decode ofp_packet_in.

    Returns a dict with buffer_id, total_len, reason, table_id, cookie,
    match fields, in_port (if present) and the ethernet frame.
    """
    buffer_id, total_len, reason, table_id = struct.unpack("!IHBB", body[:8])
    cookie = struct.unpack("!Q", body[8:16])[0]
    match_start = 16
    match_type, match_length = struct.unpack("!HH", body[match_start : match_start + 4])
    oxm_body = body[match_start + 4 : match_start + match_length]
    fields = parse_oxm_list(oxm_body) if match_type == OFPMT_OXM else {}
    # The match occupies round_up(length, 8) bytes, then two bytes of padding.
    data_offset = match_start + ((match_length + 7) // 8) * 8 + 2
    frame = body[data_offset:]
    return {
        "buffer_id": buffer_id,
        "total_len": total_len,
        "reason": reason,
        "table_id": table_id,
        "cookie": cookie,
        "fields": fields,
        "in_port": struct.unpack("!I", fields[OXM_IN_PORT])[0]
        if OXM_IN_PORT in fields
        else None,
        "frame": frame,
    }


# ------------------------------------------------------- ethernet / arp helpers
class EthernetFrame:
    """Just enough of an Ethernet frame to parse ARP requests."""

    __slots__ = ("dst", "src", "eth_type", "payload")

    def __init__(self, raw: bytes):
        self.dst = raw[0:6]
        self.src = raw[6:12]
        (self.eth_type,) = struct.unpack("!H", raw[12:14])
        self.payload = raw[14:]

    def is_arp(self) -> bool:
        return self.eth_type == ETH_TYPE_ARP and len(self.payload) >= 28


def build_arp_reply(
    target_mac: bytes, sender_mac: bytes, sender_ip: str, target_ip: str
) -> bytes:
    """Build a complete Ethernet + ARP reply frame."""
    eth = target_mac + sender_mac + struct.pack("!H", ETH_TYPE_ARP)
    arp = struct.pack(
        "!HHBBH6s4s6s4s",
        1,  # hardware type: ethernet
        ETH_TYPE_IP,  # protocol type
        6,  # hardware address length
        4,  # protocol address length
        2,  # opcode: reply
        sender_mac,
        ip_bytes(sender_ip),
        target_mac,
        ip_bytes(target_ip),
    )
    return eth + arp


def parse_arp_request(payload: bytes):
    """Return (sender_mac, sender_ip, target_ip) for an ARP request, else None."""
    if len(payload) < 28:
        return None
    htype, ptype, hlen, plen, opcode = struct.unpack("!HHBBH", payload[:8])
    if htype != 1 or ptype != ETH_TYPE_IP or hlen != 6 or plen != 4:
        return None
    if opcode != 1:  # only requests
        return None
    sender_mac = payload[8:14]
    sender_ip = ip_str(payload[14:18])
    target_ip = ip_str(payload[24:28])
    return sender_mac, sender_ip, target_ip
