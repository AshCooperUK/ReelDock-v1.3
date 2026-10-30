"""Local Peer Discovery (BEP 14)."""

from __future__ import annotations

import socket

MULTICAST = ("239.192.152.143", 6771)


def build_packet(info_hash: bytes, port: int, cookie: str) -> bytes:
    body = (
        "BT-SEARCH * HTTP/1.1\r\n"
        "Host: 239.192.152.143:6771\r\n"
        f"Port: {port}\r\n"
        f"Infohash: {info_hash.hex().upper()}\r\n"
        f"cookie: {cookie}\r\n"
        "\r\n"
    )
    return body.encode("ascii")


def parse_packet(packet: bytes) -> tuple[bytes, int] | None:
    try:
        text = packet.decode("latin1")
    except UnicodeError:
        return None
    if "BT-SEARCH" not in text.upper() and "BT-SEARCH" not in text:
        return None
    port = None
    info_hash = None
    for line in text.split("\r\n"):
        lower = line.lower()
        if lower.startswith("port:"):
            try:
                port = int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
        elif lower.startswith("infohash:"):
            try:
                info_hash = bytes.fromhex(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    if port is None or info_hash is None or len(info_hash) != 20 or not (0 < port < 65536):
        return None
    return info_hash, port


def send_packet(packet: bytes, destination: tuple[str, int] = MULTICAST, ttl: int = 1) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
        sock.sendto(packet, destination)
    finally:
        sock.close()
