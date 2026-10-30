"""Peer Exchange (BEP 11) payloads."""

from __future__ import annotations

import socket
import struct

from .bencode import bdecode, bencode


def compact(peers: list[tuple[str, int]]) -> bytes:
    return b"".join(socket.inet_aton(ip) + struct.pack("!H", port) for ip, port in peers)


def decode_compact(raw: bytes) -> list[tuple[str, int]]:
    peers = []
    for offset in range(0, len(raw) - 5, 6):
        ip = socket.inet_ntoa(raw[offset : offset + 4])
        port = struct.unpack("!H", raw[offset + 4 : offset + 6])[0]
        if port:
            peers.append((ip, port))
    return peers


def encode_pex(added: list[tuple[str, int]], dropped: list[tuple[str, int]] | None = None) -> bytes:
    dropped = dropped or []
    return bencode(
        {
            b"added": compact(added),
            b"added.f": bytes(len(added)),
            b"dropped": compact(dropped),
        }
    )


def decode_pex(payload: bytes) -> list[tuple[str, int]]:
    value, _consumed = bdecode(payload, 0)
    if not isinstance(value, dict):
        return []
    raw = value.get(b"added", b"")
    if not isinstance(raw, bytes):
        return []
    return decode_compact(raw)
