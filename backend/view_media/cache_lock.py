"""Serialize catalogue refreshes across web/worker without holding SQLite writes."""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path


@contextmanager
def refresh_lock(store,key):
    folder=Path(store.directory)/'catalogue-locks'
    folder.mkdir(mode=0o700,exist_ok=True)
    descriptor=os.open(folder/(hashlib.sha256(key.encode()).hexdigest()+'.lock'),os.O_RDWR|os.O_CREAT,0o600)
    try:
        fcntl.flock(descriptor,fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)
