"""BitTorrent peer wire over TCP, with an optional MSE handshake."""

from __future__ import annotations

import socket
import struct
import threading
import time

_CLIENT_VERSION = "v1.3"
from .bencode import bdecode, bencode
from .mse import MSEError, initiator_handshake, responder_handshake, skey_matches

PROTOCOL = b"BitTorrent protocol"
BLOCK = 16 * 1024


class WireError(ConnectionError):
    pass


def reserved_bytes(extended: bool = True, dht: bool = True) -> bytes:
    raw = bytearray(8)
    if extended:
        raw[5] |= 0x10
    if dht:
        raw[7] |= 0x01
    return bytes(raw)


def make_handshake(info_hash: bytes, peer_id: bytes, flags: bytes | None = None) -> bytes:
    return bytes([19]) + PROTOCOL + (flags or reserved_bytes()) + info_hash + peer_id


def parse_handshake(data: bytes) -> tuple[bytes, bytes, bytes]:
    if len(data) < 68 or data[0] != 19 or data[1:20] != PROTOCOL:
        raise WireError("not a bittorrent handshake")
    return data[20:28], data[28:48], data[48:68]


def pack_message(message_id: int, payload: bytes = b"") -> bytes:
    body = bytes([message_id]) + payload
    return struct.pack(">I", len(body)) + body


def bitfield_bytes(have: list[bool] | set[int], piece_count: int, as_set: bool = False) -> bytes:
    raw = bytearray((piece_count + 7) // 8)
    if as_set or isinstance(have, set):
        indexes = have
    else:
        indexes = {index for index, flag in enumerate(have) if flag}
    for index in indexes:
        if 0 <= index < piece_count:
            raw[index // 8] |= 0x80 >> (index % 8)
    return bytes(raw)


def parse_bitfield(payload: bytes, piece_count: int) -> set[int]:
    have: set[int] = set()
    for index in range(piece_count):
        byte = index // 8
        if byte >= len(payload):
            break
        if payload[byte] & (0x80 >> (index % 8)):
            have.add(index)
    return have


class Stream:
    def __init__(self, sock: socket.socket, stop: threading.Event, send_cipher=None, recv_cipher=None, early: bytes = b"") -> None:
        self.sock = sock
        self.stop = stop
        self.send_cipher = send_cipher
        self.recv_cipher = recv_cipher
        self.buf = early
        self._send_lock = threading.Lock()

    def send(self, data: bytes) -> None:
        if self.send_cipher is not None:
            data = self.send_cipher.crypt(data)
        with self._send_lock:
            self.sock.sendall(data)

    def read(self, count: int, deadline: float | None = None) -> bytes:
        while len(self.buf) < count:
            if self.stop.is_set():
                raise WireError("stopped")
            if deadline is not None and time.monotonic() >= deadline:
                raise WireError("timed out")
            try:
                chunk = self.sock.recv(max(4096, count - len(self.buf)))
            except socket.timeout:
                continue
            except OSError as exc:
                raise WireError(str(exc)) from exc
            if not chunk:
                raise WireError("closed")
            if self.recv_cipher is not None:
                chunk = self.recv_cipher.crypt(chunk)
            self.buf += chunk
        out, self.buf = self.buf[:count], self.buf[count:]
        return out

    def read_message(self, deadline: float | None = None) -> tuple[int, bytes] | None:
        length = struct.unpack(">I", self.read(4, deadline))[0]
        if length == 0:
            return None
        if length > 128 * 1024:
            raise WireError("message too large")
        body = self.read(length, deadline)
        return body[0], body[1:]


def read_raw(sock: socket.socket, stop: threading.Event, count: int, deadline: float | None = None) -> bytes:
    buf = b""
    while len(buf) < count:
        if stop.is_set():
            raise WireError("stopped")
        if deadline is not None and time.monotonic() >= deadline:
            raise WireError("timed out")
        try:
            chunk = sock.recv(count - len(buf))
        except socket.timeout:
            continue
        except OSError as exc:
            raise WireError(str(exc)) from exc
        if not chunk:
            raise WireError("closed")
        buf += chunk
    return buf


def _raw_io(sock: socket.socket, stop: threading.Event, deadline: float | None = None):
    def send(data: bytes) -> None:
        sock.sendall(data)

    def read(count: int) -> bytes:
        return read_raw(sock, stop, count, deadline)

    return send, read


def open_outbound(
    sock: socket.socket,
    stop: threading.Event,
    info_hash: bytes,
    peer_id: bytes,
    mode: str,
    timeout: float = 8,
) -> tuple[Stream, bytes]:
    sock.settimeout(1.0)
    deadline = time.monotonic() + timeout
    handshake = make_handshake(info_hash, peer_id)
    if mode == "disabled":
        stream = Stream(sock, stop)
        stream.send(handshake)
        remote = stream.read(68, deadline)
        _flags, got_hash, _peer = parse_handshake(remote)
        if got_hash != info_hash:
            raise WireError("info-hash mismatch")
        return stream, remote
    send, read = _raw_io(sock, stop, deadline)
    try:
        session = initiator_handshake(send, read, info_hash, handshake)
    except MSEError:
        raise
    stream = Stream(sock, stop, session.send, session.recv, session.early)
    remote = stream.read(68)
    _flags, got_hash, _peer = parse_handshake(remote)
    if got_hash != info_hash:
        raise WireError("info-hash mismatch")
    return stream, remote


def open_inbound(sock: socket.socket, stop: threading.Event, peer_id: bytes, mode: str, lookup_job, timeout: float = 8):
    """Return a stream and the info-hash. Sends our handshake."""
    sock.settimeout(1.0)
    deadline = time.monotonic() + timeout
    first = read_raw(sock, stop, 1, deadline)
    if first == b"\x13":
        if mode == "required":
            raise MSEError("plaintext handshake refused")
        remote = first + read_raw(sock, stop, 67, deadline)
        _flags, info_hash, _peer = parse_handshake(remote)
        if lookup_job(info_hash) is None:
            raise WireError("unknown torrent")
        stream = Stream(sock, stop)
        stream.send(make_handshake(info_hash, peer_id))
        return stream, info_hash, remote
    if mode == "disabled":
        raise MSEError("encrypted handshake refused")
    pending = first

    def read(count: int) -> bytes:
        nonlocal pending
        if len(pending) >= count:
            out, pending = pending[:count], pending[count:]
            return out
        need = count - len(pending)
        out = pending + read_raw(sock, stop, need, deadline)
        pending = b""
        return out

    def send(data: bytes) -> None:
        sock.sendall(data)

    def lookup(obfuscated: bytes, secret: bytes):
        # Try every active job. The engine passes a callable via closure below.
        return lookup_skey(obfuscated, secret)

    def lookup_skey(obfuscated: bytes, secret: bytes):
        return lookup_job(None, obfuscated, secret)

    session = responder_handshake(send, read, lookup_skey)
    stream = Stream(sock, stop, session.send, session.recv, b"")
    remote = session.early
    if len(remote) < 68:
        remote += stream.read(68 - len(remote))
        stream.buf = remote[68:] + stream.buf
        remote = remote[:68]
    else:
        stream.buf = remote[68:] + stream.buf
        remote = remote[:68]
    _flags, info_hash, _peer = parse_handshake(remote)
    if info_hash != session.info_hash:
        raise WireError("info-hash mismatch")
    stream.send(make_handshake(info_hash, peer_id))
    return stream, info_hash, remote


def extended_handshake(info_bytes: bytes | None, listen_port: int) -> bytes:
    body: dict[bytes, object] = {
        b"m": {b"ut_pex": 1, b"ut_metadata": 2},
        b"v": f"ReelDock {_CLIENT_VERSION}".encode(),
        b"reqq": 64,
        b"p": listen_port,
    }
    if info_bytes is not None:
        body[b"metadata_size"] = len(info_bytes)
    return pack_message(20, b"\x00" + bencode(body))


class MetaAssembler:
    def __init__(self) -> None:
        self.pieces: dict[int, bytes] = {}
        self.total: int | None = None

    def add(self, index: int, data: bytes, total: int) -> bytes | None:
        self.total = total
        self.pieces[index] = data
        count = (total + BLOCK - 1) // BLOCK
        if len(self.pieces) < count:
            return None
        if any(item not in self.pieces for item in range(count)):
            return None
        blob = b"".join(self.pieces[index] for index in range(count))
        return blob[:total]


def run_peer(stream: Stream, job, peer_key: str, stop: threading.Event, remote_handshake: bytes) -> None:
    """Speak to one peer. `job` is the engine job for this info-hash."""
    remote_ext: dict[str, int] = {}
    peer_has: set[int] = set()
    choked = True
    metadata = MetaAssembler()
    requested_meta: set[int] = set()
    piece_count = job.meta.piece_count if job.meta is not None else 0
    next_pex = 0.0
    last_progress = time.monotonic()
    try:
        flags = remote_handshake[20:28]
        if len(flags) >= 6 and flags[5] & 0x10:
            stream.send(extended_handshake(job.info_bytes, job.listen_port))
        if job.storage is not None and piece_count and any(job.storage.have):
            stream.send(pack_message(5, bitfield_bytes(job.storage.have, piece_count)))
        if not job.metadata_only and job.picker is not None and job.picker.wanted:
            stream.send(pack_message(2))
        while not stop.is_set() and not job.removed:
            gate = getattr(job, "wait_for_resume", None)
            gate_started = time.monotonic()
            if gate is not None and not gate():
                return
            last_progress += time.monotonic() - gate_started
            if not job.metadata_only and not choked and job.picker is not None and job.io:
                for index, begin, length in job.picker.reserve_blocks(peer_key, peer_has, 4):
                    stream.send(pack_message(6, struct.pack(">III", index, begin, length)))
                    job.payload_requests_sent += 1
            if job.metadata_only and remote_ext.get("ut_metadata") and job.info_bytes is None:
                _request_metadata(stream, remote_ext["ut_metadata"], metadata, requested_meta)
            if remote_ext.get("ut_pex") and time.monotonic() >= next_pex and not job.metadata_only:
                peers = job.exchange_peers()
                if peers:
                    from .pex import encode_pex

                    stream.send(pack_message(20, bytes([remote_ext["ut_pex"]]) + encode_pex(peers[:50])))
                next_pex = time.monotonic() + 60
            # A peer that never unchokes, or unchokes and then sends nothing,
            # has to give the slot back. Otherwise every connection sits at
            # 0 B/s until the torrent is removed.
            try:
                message = stream.read_message(last_progress + (20 if choked else 45))
            except WireError as exc:
                if "timed out" in str(exc):
                    return
                raise
            if message is None:
                continue
            message_id, payload = message
            if message_id == 0:
                choked = True
            elif message_id == 1:
                choked = False
                last_progress = time.monotonic()
            elif message_id == 4 and len(payload) >= 4:
                index = struct.unpack(">I", payload[:4])[0]
                peer_has.add(index)
            elif message_id == 5:
                if piece_count:
                    peer_has = parse_bitfield(payload, piece_count)
                elif job.meta is None:
                    peer_has = set()
            elif message_id == 6:
                # Download-only: never read or serve media pieces, even when a
                # peer sends a request while choked or old settings allow seeds.
                stream.send(pack_message(0))
            elif message_id == 7 and len(payload) >= 8:
                last_progress = time.monotonic()
                index, begin = struct.unpack(">II", payload[:8])
                block = payload[8:]
                if job.metadata_only or job.picker is None or job.storage is None:
                    continue
                if not job.take_download(len(block)):
                    continue
                full = job.picker.block_arrived(index, begin, block)
                if full is None or job.removed:
                    continue
                with job._lock:
                    job.writers += 1
                try:
                    saved = not job.removed and job.storage.write_piece(index, full)
                finally:
                    with job._lock:
                        job.writers = max(0, job.writers - 1)
                if saved:
                    job.picker.mark_verified(index)
                    job.note_download(len(full))
                    job.broadcast_have(index)
                    job.publish()
                else:
                    job.picker.release_span([index], peer_key)
            elif message_id == 9 and len(payload) >= 2:
                port = struct.unpack(">H", payload[:2])[0]
                job.note_dht_port(port)
            elif message_id == 20 and payload:
                _on_extended(stream, job, payload, remote_ext, metadata)
            elif message_id == 2:
                stream.send(pack_message(0))
    finally:
        if job.picker is not None:
            job.picker.release_owner(peer_key)


def _request_metadata(stream: Stream, ext_id: int, metadata: MetaAssembler, requested: set[int]) -> None:
    total = metadata.total
    count = 1 if total is None else (total + BLOCK - 1) // BLOCK
    for index in range(count):
        if index in metadata.pieces or index in requested:
            continue
        body = bencode({b"msg_type": 0, b"piece": index})
        stream.send(pack_message(20, bytes([ext_id]) + body))
        requested.add(index)
        if total is None:
            break


def _on_extended(stream: Stream, job, payload: bytes, remote_ext: dict[str, int], metadata: MetaAssembler) -> None:
    ext_id = payload[0]
    body = payload[1:]
    try:
        value, consumed = bdecode(body, 0)
    except Exception:
        return
    if ext_id == 0 and isinstance(value, dict):
        mapping = value.get(b"m", {})
        if isinstance(mapping, dict):
            for key, ident in mapping.items():
                if isinstance(key, bytes) and isinstance(ident, int):
                    remote_ext[key.decode("ascii", "replace")] = ident
        size = value.get(b"metadata_size")
        if isinstance(size, int) and size > 0:
            metadata.total = size
        return
    if remote_ext.get("ut_metadata") == ext_id or ext_id == 2:
        if not isinstance(value, dict):
            return
        msg_type = value.get(b"msg_type")
        piece = value.get(b"piece", 0)
        if msg_type == 0 and job.info_bytes is not None:
            start = int(piece) * BLOCK
            chunk = job.info_bytes[start : start + BLOCK]
            header = bencode({b"msg_type": 1, b"piece": int(piece), b"total_size": len(job.info_bytes)})
            remote_id = remote_ext.get("ut_metadata", 2)
            stream.send(pack_message(20, bytes([remote_id]) + header + chunk))
            return
        if msg_type == 1 and job.metadata_only:
            total = int(value.get(b"total_size") or metadata.total or 0)
            data = body[consumed:]
            if total <= 0 or total > 2_000_000:
                return
            done = metadata.add(int(piece), data, total)
            if done is not None:
                job.metadata_ready(done)
        return
    if ext_id == 1 or remote_ext.get("ut_pex") == ext_id:
        from .pex import decode_pex

        try:
            job.add_peers(decode_pex(body))
        except Exception:
            return
