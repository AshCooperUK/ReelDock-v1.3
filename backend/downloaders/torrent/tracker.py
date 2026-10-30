"""HTTP and UDP tracker announces (BEP 3 and BEP 15)."""

from __future__ import annotations

import os
import socket
import struct
import urllib.error
import urllib.parse
import urllib.request

from .bencode import BencodeError, bdecode_all
from .pex import decode_compact

CONNECT_MAGIC = 0x41727101980
_EVENTS = {"": 0, "completed": 1, "started": 2, "stopped": 3}


class TrackerError(ConnectionError):
    pass


def http_announce_url(
    url: str,
    info_hash: bytes,
    peer_id: bytes,
    listen_port: int,
    uploaded: int,
    downloaded: int,
    left: int,
    event: str = "",
) -> str:
    query = (
        f"info_hash={urllib.parse.quote_from_bytes(info_hash)}"
        f"&peer_id={urllib.parse.quote_from_bytes(peer_id)}"
        f"&port={listen_port}&uploaded={uploaded}&downloaded={downloaded}&left={left}"
        "&compact=1&numwant=50"
    )
    if event:
        query += f"&event={event}"
    sep = "&" if "?" in url else "?"
    return url + sep + query


def peers_from_http_body(body: bytes) -> tuple[list[tuple[str, int]], int]:
    try:
        payload = bdecode_all(body)
    except BencodeError as exc:
        raise TrackerError("bad tracker response") from exc
    if not isinstance(payload, dict):
        raise TrackerError("bad tracker response")
    if payload.get(b"failure reason"):
        reason = payload[b"failure reason"]
        text = reason.decode("utf-8", "replace") if isinstance(reason, bytes) else str(reason)
        raise TrackerError(text)
    interval = int(payload.get(b"interval", 1800) or 1800)
    peers: list[tuple[str, int]] = []
    raw = payload.get(b"peers")
    if isinstance(raw, bytes):
        peers.extend(decode_compact(raw))
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            ip = item.get(b"ip", b"")
            if isinstance(ip, bytes):
                ip = ip.decode("ascii", "replace")
            try:
                port = int(item.get(b"port", 0))
            except (TypeError, ValueError):
                continue
            if ip and port:
                peers.append((ip, port))
    return peers, interval


def announce_http(
    url: str,
    info_hash: bytes,
    peer_id: bytes,
    listen_port: int,
    uploaded: int,
    downloaded: int,
    left: int,
    event: str = "",
    timeout: float = 4,
) -> tuple[list[tuple[str, int]], int]:
    target = http_announce_url(url, info_hash, peer_id, listen_port, uploaded, downloaded, left, event)
    request = urllib.request.Request(target, headers={"User-Agent": "ReelDock/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(1024 * 1024)
    except (OSError, urllib.error.URLError) as exc:
        raise TrackerError(str(exc)) from exc
    return peers_from_http_body(body)


def announce_udp(
    host: str,
    port: int,
    info_hash: bytes,
    peer_id: bytes,
    listen_port: int,
    uploaded: int,
    downloaded: int,
    left: int,
    event: str = "",
    timeout: float = 4,
) -> tuple[list[tuple[str, int]], int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        transaction = int.from_bytes(os.urandom(4), "big")
        sock.sendto(struct.pack("!QII", CONNECT_MAGIC, 0, transaction), (host, port))
        data, _addr = sock.recvfrom(2048)
        if len(data) < 16:
            raise TrackerError("short UDP connect response")
        action, rx_tid, connection = struct.unpack("!IIQ", data[:16])
        if action != 0 or rx_tid != transaction:
            raise TrackerError("UDP connect mismatch")
        transaction = int.from_bytes(os.urandom(4), "big")
        packet = struct.pack(
            "!QII20s20sQQQIIIiH",
            connection,
            1,
            transaction,
            info_hash,
            peer_id,
            downloaded,
            left,
            uploaded,
            _EVENTS.get(event, 0),
            0,
            int.from_bytes(os.urandom(4), "big"),
            -1,
            listen_port,
        )
        sock.sendto(packet, (host, port))
        data, _addr = sock.recvfrom(4096)
        if len(data) < 20:
            raise TrackerError("short UDP announce response")
        action, rx_tid, interval, _leechers, _seeders = struct.unpack("!IIIII", data[:20])
        if action != 1 or rx_tid != transaction:
            raise TrackerError("UDP announce mismatch")
        return decode_compact(data[20:]), interval
    except socket.timeout as exc:
        raise TrackerError("UDP tracker timed out") from exc
    finally:
        sock.close()


def announce(
    url: str,
    info_hash: bytes,
    peer_id: bytes,
    listen_port: int,
    uploaded: int,
    downloaded: int,
    left: int,
    event: str = "",
    timeout: float = 4,
) -> tuple[list[tuple[str, int]], int]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in {"http", "https"}:
        return announce_http(
            url, info_hash, peer_id, listen_port, uploaded, downloaded, left, event, timeout
        )
    if parsed.scheme == "udp":
        return announce_udp(
            parsed.hostname or "",
            parsed.port or 80,
            info_hash,
            peer_id,
            listen_port,
            uploaded,
            downloaded,
            left,
            event,
            timeout,
        )
    raise TrackerError(f"unsupported tracker {parsed.scheme}")
