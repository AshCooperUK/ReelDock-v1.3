"""Piece and block accounting shared by peers and web seeds."""

from __future__ import annotations

import threading

BLOCK = 16 * 1024


class PiecePicker:
    def __init__(self, piece_lengths: list[int], wanted: set[int]) -> None:
        self.lengths = piece_lengths
        self.wanted = set(wanted)
        self.verified: set[int] = set()
        self.piece_owner: dict[int, str] = {}
        self.blocks: dict[int, dict[int, bytes]] = {}
        self.pending: dict[tuple[int, int], str] = {}
        self._lock = threading.Lock()

    def mark_verified(self, index: int) -> None:
        with self._lock:
            self.verified.add(index)
            self.wanted.discard(index)
            self.piece_owner.pop(index, None)
            self.blocks.pop(index, None)
            self.pending = {
                key: owner for key, owner in self.pending.items() if key[0] != index
            }

    def claim_span(self, owner: str, limit: int = 4) -> list[int] | None:
        """Claim up to `limit` consecutive wanted pieces for a web seed."""
        with self._lock:
            ordered = sorted(self.wanted)
            for index in ordered:
                if index in self.piece_owner or index in self.verified:
                    continue
                span = [index]
                nxt = index + 1
                while len(span) < limit and nxt in self.wanted and nxt not in self.piece_owner:
                    if nxt != span[-1] + 1:
                        break
                    span.append(nxt)
                    nxt += 1
                for item in span:
                    self.piece_owner[item] = owner
                return span
        return None

    def release_span(self, indexes: list[int], owner: str) -> None:
        with self._lock:
            for index in indexes:
                if self.piece_owner.get(index) == owner and index not in self.verified:
                    self.piece_owner.pop(index, None)

    def reserve_blocks(self, owner: str, peer_has: set[int], limit: int) -> list[tuple[int, int, int]]:
        with self._lock:
            reserved: list[tuple[int, int, int]] = []
            already = sum(1 for key, who in self.pending.items() if who == owner)
            room = max(0, limit - already)
            if room == 0:
                return []
            for index in sorted(self.wanted):
                if index not in peer_has or index in self.verified:
                    continue
                owner_now = self.piece_owner.get(index)
                if owner_now not in {None, owner}:
                    continue
                self.piece_owner[index] = owner
                length = self.lengths[index]
                begin = 0
                while begin < length and len(reserved) < room:
                    size = min(BLOCK, length - begin)
                    key = (index, begin)
                    if key not in self.pending and begin not in self.blocks.get(index, {}):
                        self.pending[key] = owner
                        reserved.append((index, begin, size))
                    begin += size
                if len(reserved) >= room:
                    break
            return reserved

    def block_arrived(self, index: int, begin: int, data: bytes) -> bytes | None:
        """Return the full piece bytes once every block is present."""
        with self._lock:
            if index in self.verified or index >= len(self.lengths):
                return None
            length = self.lengths[index]
            if begin < 0 or begin + len(data) > length or len(data) == 0:
                return None
            self.pending.pop((index, begin), None)
            bucket = self.blocks.setdefault(index, {})
            bucket[begin] = data
            covered = 0
            cursor = 0
            while cursor < length:
                block = bucket.get(cursor)
                if block is None:
                    return None
                cursor += len(block)
                covered += len(block)
            if covered != length or cursor != length:
                return None
            out = bytearray()
            cursor = 0
            while cursor < length:
                block = bucket[cursor]
                out += block
                cursor += len(block)
            return bytes(out)

    def release_owner(self, owner: str) -> None:
        with self._lock:
            self.pending = {key: who for key, who in self.pending.items() if who != owner}
            for index, who in list(self.piece_owner.items()):
                if who == owner and index not in self.verified:
                    # Keep blocks already received; allow another peer to finish.
                    if index not in self.blocks:
                        self.piece_owner.pop(index, None)
                    else:
                        self.piece_owner.pop(index, None)

    def peer_has_useful(self, peer_has: set[int]) -> bool:
        with self._lock:
            return any(index in self.wanted for index in peer_has)
