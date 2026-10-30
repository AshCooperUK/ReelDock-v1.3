"""Serialize job-state writers and publish complete JSON snapshots atomically."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile


@contextmanager
def _locked(path):
    with path.with_name('.'+path.name+'.lock').open('a') as handle:
        os.chmod(handle.fileno(),0o600)
        fcntl.flock(handle.fileno(),fcntl.LOCK_EX)
        yield


def _write(path,payload):
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,prefix='.'+path.stem+'.',suffix='.tmp',delete=False) as handle:
            temporary=Path(handle.name)
            json.dump(payload,handle)
        os.replace(temporary,path)
    finally:
        if temporary:temporary.unlink(missing_ok=True)


def save(path,payload):
    with _locked(path):_write(path,payload)


def update(path,change):
    with _locked(path):
        payload=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(payload,dict):return
        change(payload)
        _write(path,payload)
