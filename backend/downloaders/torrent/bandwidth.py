"""Token buckets and download/seed slot assignment."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


class TokenBucket:
    def __init__(self, rate_bps: int = 0, clock=None, sleep=None) -> None:
        self.rate = max(0, int(rate_bps))
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.tokens = float(self.rate)
        self.updated = self.clock()
        self._lock = threading.Lock()

    def set_rate(self, rate_bps: int) -> None:
        with self._lock:
            self._refill()
            self.rate = max(0, int(rate_bps))
            if self.rate == 0:
                self.tokens = 0.0

    def _refill(self) -> None:
        now = self.clock()
        elapsed = max(0.0, now - self.updated)
        self.updated = now
        if self.rate <= 0:
            self.tokens = 0.0
            return
        self.tokens = min(float(self.rate), self.tokens + elapsed * self.rate)

    def consume(self, nbytes: int, stop: threading.Event | None = None) -> bool:
        if nbytes <= 0:
            return True
        while True:
            with self._lock:
                if self.rate <= 0:
                    return True
                self._refill()
                if self.tokens >= nbytes:
                    self.tokens -= nbytes
                    return True
                wait = (nbytes - self.tokens) / self.rate
            if stop is not None and stop.is_set():
                return False
            self.sleep(min(max(wait, 0.0), 0.05))


@dataclass
class SlotJob:
    id: str
    phase: str
    paused: bool


def choose_active(jobs: list[SlotJob], max_downloads: int, max_seeds: int) -> set[str]:
    """Return ids that may use the network, preserving queue order."""
    active: set[str] = set()
    downloads = 0
    seeds = 0
    max_downloads = max(0, int(max_downloads))
    max_seeds = max(0, int(max_seeds))
    for job in jobs:
        if job.paused or job.phase == "stopped":
            continue
        if job.phase == "download" and downloads < max_downloads:
            active.add(job.id)
            downloads += 1
        elif job.phase == "seed" and seeds < max_seeds:
            active.add(job.id)
            seeds += 1
    return active


def ratio_reached(uploaded: int, downloaded: int, limit: float) -> bool:
    if limit <= 0 or downloaded <= 0:
        return False
    return (uploaded / downloaded) >= limit
