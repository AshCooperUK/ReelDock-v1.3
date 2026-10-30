import socket
import struct
import threading
import time
import unittest

from backend.downloaders.torrent.bencode import bdecode_all
from backend.downloaders.torrent.dht import DHTNode
from backend.downloaders.torrent.lsd import build_packet, parse_packet
from backend.downloaders.torrent.mse import skey_matches
from backend.downloaders.torrent.pex import decode_pex, encode_pex
from backend.downloaders.torrent.portmap import (
    find_wan_service,
    natpmp_request,
    parse_natpmp_response,
    parse_ssdp_location,
    soap_add_port,
)
from backend.downloaders.torrent.tracker import announce_udp, peers_from_http_body
from backend.downloaders.torrent.wire import WireError, open_inbound, open_outbound


class ProtocolTests(unittest.TestCase):
    def test_mse_between_two_local_peers(self) -> None:
        info_hash = b"i" * 20
        peer_id = b"-RD0100-" + b"0123456789ab"
        stop = threading.Event()
        gate = threading.Event()
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        seen: dict[str, bytes] = {}

        def lookup(*args):
            if len(args) == 1:
                return object() if args[0] == info_hash else None
            _ignored, obfuscated, secret = args
            if skey_matches(obfuscated, secret, info_hash):
                return info_hash
            return None

        def accept() -> None:
            conn, _addr = server.accept()
            try:
                stream, got_hash, _remote = open_inbound(conn, stop, peer_id, "required", lookup)
                seen["hash"] = got_hash
                seen["inbound"] = stream.read(4)
                stream.send(b"pong")
                gate.set()
            finally:
                conn.close()

        thread = threading.Thread(target=accept)
        thread.start()
        client = socket.create_connection(server.getsockname(), timeout=5)
        try:
            stream, _remote = open_outbound(client, stop, info_hash, peer_id, "required")
            stream.send(b"ping")
            seen["outbound"] = stream.read(4)
            self.assertTrue(gate.wait(5))
        finally:
            client.close()
            server.close()
            stop.set()
            thread.join(timeout=3)
        self.assertEqual(seen["hash"], info_hash)
        self.assertEqual(seen["inbound"], b"ping")
        self.assertEqual(seen["outbound"], b"pong")

    def test_outbound_handshake_stops_when_the_peer_is_silent(self) -> None:
        info_hash = b"i" * 20
        peer_id = b"-RD0100-" + b"0123456789ab"
        stop = threading.Event()
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client = socket.create_connection(server.getsockname(), timeout=5)
        accepted, _addr = server.accept()
        started = time.monotonic()
        try:
            with self.assertRaises(WireError):
                open_outbound(client, stop, info_hash, peer_id, "disabled", timeout=0.4)
            self.assertLess(time.monotonic() - started, 2)
        finally:
            client.close()
            accepted.close()
            server.close()

    def test_pex_and_lsd_packets(self) -> None:
        payload = encode_pex([("10.1.2.3", 6881), ("10.9.8.7", 1234)])
        self.assertEqual(decode_pex(payload), [("10.1.2.3", 6881), ("10.9.8.7", 1234)])
        info_hash = bytes(range(20))
        packet = build_packet(info_hash, 6881, "abc")
        parsed = parse_packet(packet)
        self.assertEqual(parsed, (info_hash, 6881))
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(1)
        try:
            sender.sendto(packet, receiver.getsockname())
            data, _addr = receiver.recvfrom(2048)
        finally:
            sender.close()
            receiver.close()
        self.assertEqual(parse_packet(data), (info_hash, 6881))

    def test_dht_get_peers_on_localhost(self) -> None:
        info_hash = b"d" * 20
        bob = DHTNode(("127.0.0.1", 0))
        alice = DHTNode(("127.0.0.1", 0))
        try:
            bob.remember(info_hash, [("203.0.113.8", 6881)])
            found = alice.get_peers(info_hash, [("127.0.0.1", bob.port)], timeout=2)
            self.assertIn(("203.0.113.8", 6881), found)
            alice.announce(info_hash, 4242, [("127.0.0.1", bob.port)])
            deadline = 0
            import time

            while ("127.0.0.1", 4242) not in bob.peers.get(info_hash, set()) and deadline < 20:
                time.sleep(0.05)
                deadline += 1
            self.assertIn(("127.0.0.1", 4242), bob.peers.get(info_hash, set()))
        finally:
            alice.close()
            bob.close()

    def test_udp_tracker_announce(self) -> None:
        info_hash = b"t" * 20
        peer_id = b"-RD0100-" + b"trackerxxxxx"
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(2)
        connection_id = 0x1122334455667788

        def serve() -> None:
            data, addr = sock.recvfrom(2048)
            action, transaction = struct.unpack("!II", data[8:16])
            self.assertEqual(action, 0)
            sock.sendto(struct.pack("!IIQ", 0, transaction, connection_id), addr)
            data, addr = sock.recvfrom(4096)
            action, transaction = struct.unpack_from("!II", data, 8)
            self.assertEqual(action, 1)
            peers = socket.inet_aton("198.51.100.10") + struct.pack("!H", 51413)
            sock.sendto(struct.pack("!IIIII", 1, transaction, 60, 1, 1) + peers, addr)

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            peers, interval = announce_udp(
                "127.0.0.1",
                sock.getsockname()[1],
                info_hash,
                peer_id,
                6881,
                0,
                0,
                10,
                "started",
                timeout=2,
            )
        finally:
            thread.join(timeout=3)
            sock.close()
        self.assertEqual(peers, [("198.51.100.10", 51413)])
        self.assertEqual(interval, 60)

    def test_http_tracker_body_and_port_mapping_bytes(self) -> None:
        from backend.downloaders.torrent.bencode import bencode

        body = bencode({b"interval": 10, b"peers": socket.inet_aton("192.0.2.4") + struct.pack("!H", 6881)})
        peers, interval = peers_from_http_body(body)
        self.assertEqual(interval, 10)
        self.assertEqual(peers, [("192.0.2.4", 6881)])
        packet = natpmp_request(2, 6881, 6881, 3600)
        response = struct.pack("!BBHIHHI", 0, 130, 0, 5, 6881, 6881, 3600)
        parsed = parse_natpmp_response(response)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["external_port"], 6881)
        self.assertEqual(len(packet), 12)
        location = parse_ssdp_location(b"HTTP/1.1 200 OK\r\nLOCATION: http://192.0.2.1:5000/root.xml\r\n\r\n")
        self.assertEqual(location, "http://192.0.2.1:5000/root.xml")
        xml = b"""<root><device><serviceList><service>
            <serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
            <controlURL>/ctl</controlURL>
            </service></serviceList></device></root>"""
        self.assertEqual(find_wan_service(xml)[1], "/ctl")
        soap = soap_add_port("urn:schemas-upnp-org:service:WANIPConnection:1", 6881, 6881, "192.0.2.20", "TCP")
        self.assertIn(b"<NewExternalPort>6881</NewExternalPort>", soap)
        self.assertIn(b"AddPortMapping", soap)
        self.assertTrue(bdecode_all(b"de") == {})


if __name__ == "__main__":
    unittest.main()
