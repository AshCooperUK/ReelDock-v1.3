"""Message Stream Encryption (MSE) with Diffie-Hellman and RC4.

The handshake is tested between two ReelDock peers. Interoperation with other
clients was not verified here. Plaintext-inside-MSE is not offered: when this
engine speaks MSE it selects RC4. "Prefer" falls back to a separate plaintext
connection if the peer rejects MSE.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

# Oakley Group 1, 768-bit MODP prime used by MSE.
# 768-bit MSE prime, the same group used by mainstream clients. 96-byte keys.
_P_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A63A36210000000000090563"
)
PRIME = int(_P_HEX, 16)
GENERATOR = 2
KEY_BYTES = 96
RC4 = 0x02
MAX_PAD = 512


class MSEError(ConnectionError):
    pass


class RC4Cipher:
    def __init__(self, key: bytes) -> None:
        schedule = list(range(256))
        mixed = 0
        for index, value in enumerate(schedule):
            mixed = (mixed + value + key[index % len(key)]) & 255
            schedule[index], schedule[mixed] = schedule[mixed], schedule[index]
        self._schedule = schedule
        self._i = 0
        self._j = 0

    def crypt(self, data: bytes) -> bytes:
        out = bytearray(len(data))
        schedule = self._schedule
        i = self._i
        j = self._j
        for offset, value in enumerate(data):
            i = (i + 1) & 255
            j = (j + schedule[i]) & 255
            schedule[i], schedule[j] = schedule[j], schedule[i]
            keystream = schedule[(schedule[i] + schedule[j]) & 255]
            out[offset] = value ^ keystream
        self._i = i
        self._j = j
        return bytes(out)

    def discard(self, count: int = 1024) -> None:
        self.crypt(b"\x00" * count)


def _sha1(*parts: bytes) -> bytes:
    digest = hashlib.sha1()
    for part in parts:
        digest.update(part)
    return digest.digest()


def dh_keypair() -> tuple[int, bytes]:
    private = int.from_bytes(os.urandom(20), "big") or 2
    public = pow(GENERATOR, private, PRIME)
    return private, public.to_bytes(KEY_BYTES, "big")


def shared_secret(private: int, peer_public: bytes) -> bytes:
    secret = pow(int.from_bytes(peer_public, "big"), private, PRIME)
    return secret.to_bytes(KEY_BYTES, "big")


@dataclass
class CryptoSession:
    send: RC4Cipher
    recv: RC4Cipher
    info_hash: bytes
    early: bytes = b""


def _pad() -> bytes:
    return os.urandom(int.from_bytes(os.urandom(1), "big") % 64)


def _keys(secret: bytes, info_hash: bytes) -> tuple[bytes, bytes, bytes, bytes]:
    return (
        _sha1(b"keyA", secret, info_hash),
        _sha1(b"keyB", secret, info_hash),
        _sha1(b"req1", secret),
        bytes(a ^ b for a, b in zip(_sha1(b"req2", info_hash), _sha1(b"req3", secret))),
    )


def initiator_handshake(send, read, info_hash: bytes, initial: bytes) -> CryptoSession:
    private, public = dh_keypair()
    send(public + _pad())
    peer_public = read(KEY_BYTES)
    secret = shared_secret(private, peer_public)
    key_a, key_b, req1, obfuscated = _keys(secret, info_hash)
    outgoing = RC4Cipher(key_a)
    outgoing.discard()
    body = b"\x00" * 8 + RC4.to_bytes(4, "big") + (0).to_bytes(2, "big")
    body += len(initial).to_bytes(2, "big") + initial
    send(req1 + obfuscated + outgoing.crypt(body))
    buffered = b""
    incoming = None
    pad_len = None
    while incoming is None:
        buffered += read(1)
        if len(buffered) < 8:
            continue
        found = _match_vc(buffered, key_b)
        if found is not None:
            pad_len, incoming, rest = found
            break
        if len(buffered) > MAX_PAD + 14:
            raise MSEError("MSE verification failed")
    assert incoming is not None and pad_len is not None
    need = 6
    while len(rest) < need:
        rest += incoming.crypt(read(need - len(rest)))
    provide = int.from_bytes(rest[:4], "big")
    if provide & RC4 == 0:
        raise MSEError("peer refused RC4")
    pad_d = int.from_bytes(rest[4:6], "big")
    if pad_d > MAX_PAD:
        raise MSEError("pad too large")
    rest = rest[6:]
    while len(rest) < pad_d:
        rest += incoming.crypt(read(pad_d - len(rest)))
    return CryptoSession(outgoing, incoming, info_hash, rest[pad_d:])


def responder_handshake(send, read, lookup) -> CryptoSession:
    """lookup(obfuscated, secret) -> info_hash or None."""
    private, public = dh_keypair()
    peer_public = read(KEY_BYTES)
    secret = shared_secret(private, peer_public)
    send(public + _pad())
    req1 = _sha1(b"req1", secret)
    window = b""
    while req1 not in window:
        window += read(1)
        if len(window) > MAX_PAD + 20:
            raise MSEError("MSE sync failed")
    start = window.find(req1)
    if start > MAX_PAD:
        raise MSEError("MSE sync failed")
    tail = window[start + 20 :]
    while len(tail) < 20:
        tail += read(20 - len(tail))
    obfuscated, pending = tail[:20], tail[20:]
    info_hash = lookup(obfuscated, secret)
    if info_hash is None:
        raise MSEError("unknown info-hash")
    key_a, key_b, _req1, _obf = _keys(secret, info_hash)
    incoming = RC4Cipher(key_a)
    incoming.discard()
    if pending:
        pending = incoming.crypt(pending)

    def take(count: int) -> bytes:
        nonlocal pending
        while len(pending) < count:
            pending += incoming.crypt(read(count - len(pending)))
        out, pending = pending[:count], pending[count:]
        return out

    if take(8) != b"\x00" * 8:
        raise MSEError("bad verification constant")
    provide = int.from_bytes(take(4), "big")
    if provide & RC4 == 0:
        raise MSEError("peer refused RC4")
    pad_c = int.from_bytes(take(2), "big")
    if pad_c > MAX_PAD:
        raise MSEError("pad too large")
    take(pad_c)
    ia_len = int.from_bytes(take(2), "big")
    if ia_len > 65535:
        raise MSEError("initial payload too large")
    initial = take(ia_len)
    outgoing = RC4Cipher(key_b)
    outgoing.discard()
    step = b"\x00" * 8 + RC4.to_bytes(4, "big") + (0).to_bytes(2, "big")
    send(outgoing.crypt(step))
    return CryptoSession(outgoing, incoming, info_hash, initial)


def _match_vc(data: bytes, key: bytes) -> tuple[int, RC4Cipher, bytes] | None:
    limit = min(MAX_PAD, len(data) - 8)
    for pad in range(limit + 1):
        trial = RC4Cipher(key)
        trial.discard()
        if trial.crypt(data[pad : pad + 8]) == b"\x00" * 8:
            rest = trial.crypt(data[pad + 8 :])
            return pad, trial, rest
    return None


def skey_matches(obfuscated: bytes, secret: bytes, info_hash: bytes) -> bool:
    expected = bytes(a ^ b for a, b in zip(_sha1(b"req2", info_hash), _sha1(b"req3", secret)))
    return expected == obfuscated
