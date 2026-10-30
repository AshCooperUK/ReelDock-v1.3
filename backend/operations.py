"""Cross-process lock so shutdown and factory reset can stop downloads."""

from __future__ import annotations

import fcntl
import os
import threading
from contextlib import contextmanager


class Halted(RuntimeError):
    pass


class Operations:
    def __init__(self, path: str) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.fp = open(path, "a+", encoding="utf-8")
        self.halted = threading.Event()
        self.engine = None

    def register(self, engine) -> None:
        self.engine = engine

    @contextmanager
    def shared_section(self):
        if self.halted.is_set():
            raise Halted("downloads are stopped")
        fcntl.flock(self.fp.fileno(), fcntl.LOCK_SH)
        try:
            if self.halted.is_set():
                raise Halted("downloads are stopped")
            yield
        finally:
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)

    def halt(self) -> None:
        """Stop torrent work, then take the exclusive lock. Does not delete media."""
        self.halted.set()
        engine = self.engine
        if engine is not None:
            engine.shutdown()
        fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX)
