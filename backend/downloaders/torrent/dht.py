"""Kademlia DHT (BEP 5) over UDP. IPv4 only, no holepunch."""

from __future__ import annotations

import hashlib
import os
import socket
import threading
from collections import deque

from .bencode import BencodeError, bdecode, bencode
from .pex import compact, decode_compact
from ...vpn.transport import torrent


def xor_distance(left: bytes, right: bytes) -> int:
    return int.from_bytes(bytes(a ^ b for a, b in zip(left, right)), "big")


class DHTNode:
    @torrent
    def __init__(self, bind: tuple[str, int] | None = None, node_id: bytes | None = None) -> None:
        self.node_id = node_id or os.urandom(20)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.settimeout(0.4)
        self.sock.bind(bind or ("0.0.0.0", 0))
        self.port = self.sock.getsockname()[1]
        self.nodes: dict[bytes, tuple[str, int]] = {}
        self.peers: dict[bytes, set[tuple[str, int]]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._pending: dict[bytes, deque] = {}
        self._thread = threading.Thread(target=torrent(self._loop), name="reeldock-dht", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)

    def _token(self, addr: tuple[str, int]) -> bytes:
        return hashlib.sha1(self.node_id + socket.inet_aton(addr[0])).digest()[:8]

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data.startswith(b"d"):
                continue
            try:
                message, _end = bdecode(data, 0)
            except BencodeError:
                continue
            if not isinstance(message, dict):
                continue
            try:
                self._on_message(message, addr)
            except Exception:
                continue

    def _on_message(self, message: dict, addr: tuple[str, int]) -> None:
        kind = message.get(b"y")
        tid = message.get(b"t", b"")
        if kind == b"q":
            self._on_query(message, addr, tid if isinstance(tid, bytes) else b"aa")
            return
        if kind == b"r" and isinstance(tid, bytes):
            with self._lock:
                waiters = self._pending.get(tid)
                if waiters is not None:
                    waiters.append((message.get(b"r", {}), addr))

    def _on_query(self, message: dict, addr: tuple[str, int], tid: bytes) -> None:
        name = message.get(b"q")
        args = message.get(b"a") if isinstance(message.get(b"a"), dict) else {}
        node = args.get(b"id")
        if isinstance(node, bytes) and len(node) == 20:
            with self._lock:
                self.nodes[node] = addr
        if name == b"ping":
            self._reply(tid, addr, {b"id": self.node_id})
        elif name == b"find_node":
            target = args.get(b"target", self.node_id)
            self._reply(tid, addr, {b"id": self.node_id, b"nodes": self.compact_nodes(target if isinstance(target, bytes) else self.node_id)})
        elif name == b"get_peers":
            info_hash = args.get(b"info_hash", b"")
            body: dict[bytes, object] = {b"id": self.node_id, b"token": self._token(addr)}
            peers = self.peers.get(info_hash, set()) if isinstance(info_hash, bytes) else set()
            if peers:
                body[b"values"] = [compact([(ip, port)]) for ip, port in list(peers)[:32]]
            else:
                body[b"nodes"] = self.compact_nodes(info_hash if isinstance(info_hash, bytes) else self.node_id)
            self._reply(tid, addr, body)
        elif name == b"announce_peer":
            token = args.get(b"token")
            info_hash = args.get(b"info_hash")
            if token != self._token(addr) or not isinstance(info_hash, bytes):
                self._error(tid, addr, b"bad token")
                return
            port = addr[1]
            if args.get(b"implied_port", 0) != 1:
                try:
                    port = int(args.get(b"port", port))
                except (TypeError, ValueError):
                    port = addr[1]
            with self._lock:
                self.peers.setdefault(info_hash, set()).add((addr[0], port))
            self._reply(tid, addr, {b"id": self.node_id})

    def _reply(self, tid: bytes, addr: tuple[str, int], body: dict) -> None:
        try:
            self.sock.sendto(bencode({b"t": tid, b"y": b"r", b"r": body}), addr)
        except OSError:
            pass

    def _error(self, tid: bytes, addr: tuple[str, int], text: bytes) -> None:
        try:
            self.sock.sendto(bencode({b"t": tid, b"y": b"e", b"e": [203, text]}), addr)
        except OSError:
            pass

    def compact_nodes(self, target: bytes) -> bytes:
        with self._lock:
            ranked = sorted(self.nodes.items(), key=lambda item: xor_distance(item[0], target))[:8]
        blob = b""
        for node_id, (ip, port) in ranked:
            blob += node_id + socket.inet_aton(ip) + struct_port(port)
        return blob

    def remember(self, info_hash: bytes, peers: list[tuple[str, int]]) -> None:
        with self._lock:
            self.peers.setdefault(info_hash, set()).update(peers)

    def get_peers(self, info_hash: bytes, seeds: list[tuple[str, int]], timeout: float = 1.5) -> list[tuple[str, int]]:
        found: list[tuple[str, int]] = []
        for host, port in seeds:
            tid = os.urandom(2)
            query = bencode(
                {
                    b"t": tid,
                    b"y": b"q",
                    b"q": b"get_peers",
                    b"a": {b"id": self.node_id, b"info_hash": info_hash},
                }
            )
            bucket: deque = deque()
            with self._lock:
                self._pending[tid] = bucket
            try:
                self.sock.sendto(query, (host, port))
            except OSError:
                continue
            deadline = timeout
            waited = 0.0
            while waited < deadline:
                if bucket:
                    body, _addr = bucket.popleft()
                    if isinstance(body, dict):
                        values = body.get(b"values", []) or []
                        for value in values:
                            if isinstance(value, bytes):
                                found.extend(decode_compact(value))
                            elif isinstance(value, list):
                                for item in value:
                                    if isinstance(item, bytes):
                                        found.extend(decode_compact(item))
                        nodes = body.get(b"nodes", b"")
                        if isinstance(nodes, bytes):
                            self._store_nodes(nodes)
                    break
                if self._stop.wait(0.05):
                    break
                waited += 0.05
            with self._lock:
                self._pending.pop(tid, None)
        # Also return peers we already store locally.
        with self._lock:
            found.extend(self.peers.get(info_hash, set()))
        unique = []
        seen = set()
        for item in found:
            if item not in seen and item[1]:
                seen.add(item)
                unique.append(item)
        return unique

    def announce(self, info_hash: bytes, port: int, seeds: list[tuple[str, int]]) -> None:
        for host, seed_port in seeds:
            tid = os.urandom(2)
            query = bencode(
                {
                    b"t": tid,
                    b"y": b"q",
                    b"q": b"get_peers",
                    b"a": {b"id": self.node_id, b"info_hash": info_hash},
                }
            )
            bucket: deque = deque()
            with self._lock:
                self._pending[tid] = bucket
            try:
                self.sock.sendto(query, (host, seed_port))
            except OSError:
                continue
            token = None
            waited = 0.0
            while waited < 1.0:
                if bucket:
                    body, _addr = bucket.popleft()
                    if isinstance(body, dict) and isinstance(body.get(b"token"), bytes):
                        token = body[b"token"]
                    break
                if self._stop.wait(0.05):
                    return
                waited += 0.05
            with self._lock:
                self._pending.pop(tid, None)
            if token is None:
                continue
            announce = bencode(
                {
                    b"t": os.urandom(2),
                    b"y": b"q",
                    b"q": b"announce_peer",
                    b"a": {
                        b"id": self.node_id,
                        b"info_hash": info_hash,
                        b"port": port,
                        b"token": token,
                        b"implied_port": 0,
                    },
                }
            )
            try:
                self.sock.sendto(announce, (host, seed_port))
            except OSError:
                pass

    def _store_nodes(self, blob: bytes) -> None:
        for offset in range(0, len(blob) - 25, 26):
            node_id = blob[offset : offset + 20]
            ip = socket.inet_ntoa(blob[offset + 20 : offset + 24])
            port = int.from_bytes(blob[offset + 24 : offset + 26], "big")
            if port:
                with self._lock:
                    self.nodes[node_id] = (ip, port)


def struct_port(port: int) -> bytes:
    return port.to_bytes(2, "big")
