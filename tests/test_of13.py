"""Structural tests for the OpenFlow 1.3 encoder.

These do not need a switch: they check the byte layout against the sizes the
specification mandates, which is where hand-written protocol code usually
breaks (padding, lengths, and the 8-byte alignment of OXM matches).
"""

import os
import socket
import struct
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from controller import of13  # noqa: E402


class HeaderTests(unittest.TestCase):
    def test_hello_header(self):
        raw = of13.hello(7)
        version, msg_type, length, xid = of13.parse_header(raw)
        self.assertEqual(version, 4)
        self.assertEqual(msg_type, of13.OFPT_HELLO)
        self.assertEqual(length, len(raw))
        self.assertEqual(xid, 7)

    def test_header_length_matches_payload(self):
        raw = of13.features_request(9)
        self.assertEqual(len(raw), 8)
        self.assertEqual(of13.parse_header(raw)[2], 8)


class MatchTests(unittest.TestCase):
    def test_empty_match_is_eight_bytes(self):
        match = of13.build_match([])
        self.assertEqual(len(match), 8)
        match_type, length = struct.unpack("!HH", match[:4])
        self.assertEqual(match_type, of13.OFPMT_OXM)
        # The length field excludes padding: 4-byte header, 4 bytes of padding.
        self.assertEqual(length, 4)
        self.assertEqual(match[4:], b"\x00" * 4)

    def test_match_length_excludes_padding(self):
        eth = of13.oxm(of13.OXM_ETH_TYPE, struct.pack("!H", 0x0800))
        match = of13.build_match([eth])
        _type, length = struct.unpack("!HH", match[:4])
        self.assertEqual(length, 10)  # 4 header + 6 OXM bytes
        self.assertEqual(len(match), 16)  # rounded up to 8
        self.assertEqual(match[4:10], eth)
        self.assertEqual(match[10:], b"\x00" * 6)

    def test_match_is_always_eight_byte_aligned(self):
        for count in range(0, 7):
            fields = [of13.oxm(of13.OXM_IN_PORT, struct.pack("!I", i + 1)) for i in range(count)]
            match = of13.build_match(fields)
            self.assertEqual(len(match) % 8, 0, "match with %d fields" % count)
            _type, length = struct.unpack("!HH", match[:4])
            self.assertEqual(len(match), ((length + 7) // 8) * 8)
            self.assertEqual(len(match) - length, len(match) - 4 - 8 * count)

    def test_oxm_encoding(self):
        raw = of13.oxm(of13.OXM_ETH_TYPE, struct.pack("!H", 0x0800))
        cls, field_and_mask, length = struct.unpack("!HBB", raw[:4])
        self.assertEqual(cls, of13.OFPXMC_OPENFLOW_BASIC)
        self.assertEqual(field_and_mask >> 1, of13.OXM_ETH_TYPE)
        self.assertEqual(length, 2)
        self.assertEqual(raw[4:], b"\x08\x00")


class ActionTests(unittest.TestCase):
    def test_output_action(self):
        action = of13.action_output(3)
        self.assertEqual(len(action), 16)
        action_type, length, port = struct.unpack("!HHI", action[:8])
        self.assertEqual(action_type, of13.OFPAT_OUTPUT)
        self.assertEqual(length, 16)
        self.assertEqual(port, 3)

    def test_set_field_action_is_aligned(self):
        action = of13.action_set_field(of13.OXM_IPV4_DST, socket.inet_aton("10.0.0.11"))
        self.assertEqual(len(action) % 8, 0)
        action_type, length = struct.unpack("!HH", action[:4])
        self.assertEqual(action_type, of13.OFPAT_SET_FIELD)
        self.assertEqual(length, len(action))
        tlv = of13.oxm(of13.OXM_IPV4_DST, socket.inet_aton("10.0.0.11"))
        self.assertEqual(action[4 : 4 + len(tlv)], tlv)
        self.assertEqual(action[4 + len(tlv) :], b"\x00" * (len(action) - 4 - len(tlv)))

    def test_set_field_mac(self):
        action = of13.action_set_field(of13.OXM_ETH_DST, of13.mac_bytes("00:00:00:00:00:11"))
        self.assertEqual(len(action), 16)


class FlowModTests(unittest.TestCase):
    def test_flow_mod_fixed_header(self):
        raw = of13.add_flow(
            [of13.oxm(of13.OXM_ETH_TYPE, struct.pack("!H", 0x0806))],
            [of13.action_output(of13.OFPP_CONTROLLER, 0xFFFF)],
            priority=200,
            cookie=0x1A03000000000000,
        )
        version, msg_type, length, _xid = of13.parse_header(raw)
        self.assertEqual(version, 4)
        self.assertEqual(msg_type, of13.OFPT_FLOW_MOD)
        self.assertEqual(length, len(raw))
        cookie, cookie_mask, table_id, command, idle, hard, priority = struct.unpack(
            "!QQBBHHH", raw[8:32]
        )
        self.assertEqual(cookie, 0x1A03000000000000)
        self.assertEqual(cookie_mask, 0)
        self.assertEqual(table_id, 0)
        self.assertEqual(command, of13.OFPFC_ADD)
        self.assertEqual(priority, 200)
        # 8 header + 40 fixed + 16 match + 8 instr header + 16 action
        self.assertEqual(len(raw), 88)

    def test_delete_flow_carries_cookie_mask(self):
        raw = of13.flow_mod(
            of13.build_match([]),
            [],
            command=of13.OFPFC_DELETE,
            cookie=0x1A01000000000000,
            cookie_mask=0xFF00FF0000000000,
        )
        self.assertEqual(of13.parse_header(raw)[1], of13.OFPT_FLOW_MOD)
        cookie, cookie_mask = struct.unpack("!QQ", raw[8:24])
        self.assertEqual(cookie, 0x1A01000000000000)
        self.assertEqual(cookie_mask, 0xFF00FF0000000000)
        self.assertEqual(struct.unpack("!B", raw[25:26])[0], of13.OFPFC_DELETE)


class PacketTests(unittest.TestCase):
    def test_packet_out_with_data(self):
        frame = b"\x00" * 60
        raw = of13.packet_out(
            in_port=1,
            actions=[of13.action_output(2)],
            buffer_id=0xFFFFFFFF,
            data=frame,
        )
        _v, msg_type, length, _xid = of13.parse_header(raw)
        self.assertEqual(msg_type, of13.OFPT_PACKET_OUT)
        self.assertEqual(length, len(raw))
        buffer_id, in_port, actions_len = struct.unpack("!IIH", raw[8:18])
        self.assertEqual(buffer_id, 0xFFFFFFFF)
        self.assertEqual(in_port, 1)
        self.assertEqual(actions_len, 16)
        self.assertEqual(raw[24:40], of13.action_output(2))
        self.assertEqual(raw[40:], frame)

    def test_arp_request_round_trip(self):
        # Build a request the way a host would, then decode it with our parser.
        payload = struct.pack(
            "!HHBBH6s4s6s4s",
            1,
            of13.ETH_TYPE_IP,
            6,
            4,
            1,
            of13.mac_bytes("00:00:00:00:00:01"),
            socket.inet_aton("10.0.0.1"),
            b"\x00" * 6,
            socket.inet_aton("10.0.0.100"),
        )
        frame = b"\xff" * 6 + of13.mac_bytes("00:00:00:00:00:01") + struct.pack("!H", 0x0806) + payload
        ethernet = of13.EthernetFrame(frame)
        self.assertTrue(ethernet.is_arp())
        parsed = of13.parse_arp_request(ethernet.payload)
        self.assertIsNotNone(parsed)
        sender_mac, sender_ip, target_ip = parsed
        self.assertEqual(sender_ip, "10.0.0.1")
        self.assertEqual(target_ip, "10.0.0.100")
        self.assertEqual(of13.mac_str(sender_mac), "00:00:00:00:00:01")

    def test_build_arp_reply(self):
        reply = of13.build_arp_reply(
            target_mac=of13.mac_bytes("00:00:00:00:00:01"),
            sender_mac=of13.mac_bytes("00:00:00:00:00:aa"),
            sender_ip="10.0.0.100",
            target_ip="10.0.0.1",
        )
        self.assertEqual(len(reply), 42)
        self.assertEqual(reply[12:14], b"\x08\x06")
        self.assertEqual(reply[20:22], b"\x00\x02")  # ARP reply
        self.assertEqual(of13.ip_str(reply[28:32]), "10.0.0.100")
        self.assertEqual(of13.ip_str(reply[38:42]), "10.0.0.1")

    def test_parse_packet_in(self):
        match = of13.build_match(
            [
                of13.oxm(of13.OXM_IN_PORT, struct.pack("!I", 1)),
                of13.oxm(of13.OXM_ETH_TYPE, struct.pack("!H", 0x0800)),
            ]
        )
        frame = b"\xaa" * 14
        body = (
            struct.pack("!IHBB", 0xFFFFFFFF, len(frame), of13.OFPR_ACTION, 0)
            + struct.pack("!Q", 0x1234)
            + match
            + b"\x00\x00"
            + frame
        )
        parsed = of13.parse_packet_in(body)
        self.assertEqual(parsed["in_port"], 1)
        self.assertEqual(parsed["cookie"], 0x1234)
        self.assertEqual(parsed["frame"], frame)


if __name__ == "__main__":
    unittest.main(verbosity=2)
