"""Durable Discover handoffs; recovery links saved work without rebuilding metadata.

The per-request flock is held across preparation and creation. A recovering
process can prove the old creator is gone without relying on an elapsed lease.
Native jobs checkpoint their receipt before becoming visible to a scheduler;
SQLite-backed queues commit their job and request link in one transaction.
"""
from contextlib import contextmanager
from functools import wraps
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import threading
import time
import uuid

_GUARD = threading.Lock()
_LOCKS = {}
_LOCAL = threading.local()
LEGACY_GRACE = 600


def initialise(db):
    db.execute('''CREATE TABLE IF NOT EXISTS discover_handoffs (
        request_id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE,
        stop_generation INTEGER NOT NULL, acquisition_generation INTEGER NOT NULL,
        engine TEXT NOT NULL DEFAULT '', job_id TEXT NOT NULL DEFAULT '',
        candidate_id TEXT NOT NULL DEFAULT '', content_key TEXT NOT NULL DEFAULT '',
        stage TEXT NOT NULL DEFAULT 'claimed', created REAL NOT NULL, updated REAL NOT NULL)''')


@contextmanager
def locked(store, request_id, *, blocking=False):
    """One live creator/reconciler per request, across threads and processes."""
    directory = Path(store.directory).resolve()
    identity = (str(directory), str(request_id))
    depths = getattr(_LOCAL, 'depths', None)
    if depths is None:
        depths = _LOCAL.depths = {}
    if identity in depths:
        depths[identity] += 1
        try:
            yield True
        finally:
            depths[identity] -= 1
        return
    with _GUARD:
        local_lock = _LOCKS.setdefault(identity, threading.Lock())
    if not local_lock.acquire(blocking=blocking):
        yield False
        return
    descriptor = None
    try:
        folder = directory / 'discover-handoff-locks'
        folder.mkdir(mode=0o700, exist_ok=True)
        if folder.is_symlink() or not folder.is_dir():
            raise OSError('The download handoff lock is unavailable.')
        leaf = hashlib.sha256(str(request_id).encode()).hexdigest() + '.lock'
        descriptor = os.open(folder / leaf, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError('The download handoff lock is unavailable.')
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        depths[identity] = 1
        try:
            yield True
        finally:
            depths.pop(identity, None)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        local_lock.release()


def serialised(function):
    @wraps(function)
    def run(app, store, row, *args, **kwargs):
        with locked(store, row['id']) as acquired:
            if not acquired:
                return False
            return function(app, store, row, *args, **kwargs)
    return run


def _setting(db, key, default='0'):
    row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def open_in(db, store, receipt=None):
    from .acquisition import file_held
    if _setting(db, 'acquisition_hold') == '1' or file_held(store.directory):
        return False
    if receipt is not None:
        try:
            return int(_setting(db, 'acquisition_generation')) == receipt['acquisition_generation']
        except (ValueError, TypeError):
            return False
    return True


def begin(db, store, row):
    """Called in the same writer transaction that changes the request to starting."""
    if not open_in(db, store):
        return False
    now = time.time()
    db.execute('''INSERT INTO discover_handoffs
        (request_id,token,stop_generation,acquisition_generation,stage,created,updated)
        VALUES (?,?,?,?,'claimed',?,?) ON CONFLICT(request_id) DO UPDATE SET
        token=excluded.token,stop_generation=excluded.stop_generation,
        acquisition_generation=excluded.acquisition_generation,engine='',job_id='',
        candidate_id='',content_key='',stage='claimed',created=excluded.created,updated=excluded.updated''',
        (row['id'], uuid.uuid4().hex, int(row['stop_generation'] or 0),
         int(_setting(db, 'acquisition_generation')), now, now))
    return True


def allowed_in(db, store, row, generation):
    if not row or row['state'] != 'starting' or int(row['stop_generation'] or 0) != int(generation):
        return False
    from .media_holds import blocked_in, request_keys_in
    if blocked_in(db, request_keys_in(db, row)):
        return False
    receipt = db.execute('SELECT * FROM discover_handoffs WHERE request_id=?', (row['id'],)).fetchone()
    return bool(receipt and receipt['stage'] in ('claimed', 'prepared')
                and receipt['stop_generation'] == int(generation) and open_in(db, store, receipt))


def prepare_nzb(store, request_id, generation, content_key, candidate_id):
    """Persist the exact native job identity before its checkpoint can be created."""
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM discover_requests WHERE id=?', (request_id,)).fetchone()
        if not allowed_in(db, store, row, generation):
            return None
        receipt = dict(db.execute('SELECT * FROM discover_handoffs WHERE request_id=?', (request_id,)).fetchone())
        if receipt['stage'] == 'prepared':
            if receipt['content_key'] != content_key or receipt['candidate_id'] != str(candidate_id or ''):
                raise ValueError('The previous download handoff needs reconciliation.')
            return receipt
        identity = uuid.uuid4().hex
        db.execute("UPDATE discover_handoffs SET engine='nzb',job_id=?,content_key=?,candidate_id=?,stage='prepared',updated=? WHERE request_id=? AND token=?",
                   (identity, content_key, str(candidate_id or ''), time.time(), request_id, receipt['token']))
        from .media_holds import blocked_in, request_keys_in
        keys = [content_key, 'native:nzb-job:' + identity]
        if blocked_in(db, request_keys_in(db, row) + keys):
            raise ValueError('This download was cleared.')
        db.executemany('INSERT OR IGNORE INTO media_request_keys VALUES (?,?)', [(request_id, key) for key in keys])
        return dict(db.execute('SELECT * FROM discover_handoffs WHERE request_id=?', (request_id,)).fetchone())


def committed_in(db, request_id, engine, job_id):
    db.execute("UPDATE discover_handoffs SET stage='committed',engine=?,job_id=?,updated=? WHERE request_id=?",
               (engine, job_id, time.time(), request_id))


def native_checkpoint(store, receipt):
    from .downloaders.nzb.engine import handoff_checkpoint
    return handoff_checkpoint(Path(store.directory) / 'nzb-engine', receipt['job_id'],
                              receipt['token'], receipt['content_key'])


def reset_missing(store, request_id, generation):
    """A live creator may try another candidate only after proving no job exists."""
    with store.connect() as db:
        receipt = db.execute('SELECT * FROM discover_handoffs WHERE request_id=?', (request_id,)).fetchone()
    if not receipt or receipt['stage'] != 'prepared':
        return True
    if native_checkpoint(store, receipt) is not None:
        return False
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM discover_requests WHERE id=?', (request_id,)).fetchone()
        if not allowed_in(db, store, row, generation):
            return False
        return db.execute("UPDATE discover_handoffs SET stage='claimed',engine='',job_id='',candidate_id='',content_key='',updated=? WHERE request_id=? AND token=? AND stage='prepared'",
                          (time.time(), request_id, receipt['token'])).rowcount == 1


def attach_native(store, request_id, generation):
    """Attach the exact saved job without touching its checkpoint or metadata."""
    with store.connect() as db:
        receipt = db.execute('SELECT * FROM discover_handoffs WHERE request_id=?', (request_id,)).fetchone()
    if not receipt or receipt['stage'] != 'prepared' or receipt['engine'] != 'nzb':
        return False
    job = native_checkpoint(store, receipt)
    if job is None:
        return False
    # The normal sync loop validates published paths and settles completion.
    # Linking as queued also lets it reconcile failed/paused/placement states
    # without duplicating metadata/publication logic in crash recovery.
    from .discover_store import commit_engine
    return commit_engine(store, request_id, generation, 'nzb', receipt['job_id'],
                         'Reconnected to the saved NZB download.')


def recover(app, store, limit=10):
    """Bounded, provider-free reconciliation before ordinary acquisition work."""
    from . import discover_store as ds
    ds.initialise(store)
    with store.connect() as db:
        rows = db.execute('''SELECT r.id,r.stop_generation,h.token FROM discover_requests r
            LEFT JOIN discover_handoffs h ON h.request_id=r.id
            WHERE r.state='starting' AND (h.stage IN ('claimed','prepared') OR
                (h.request_id IS NULL AND r.updated<?)) ORDER BY r.updated LIMIT ?''',
            (time.time() - LEGACY_GRACE, limit)).fetchall()
    result = dict(attached=0, requeued=0, attention=0, deferred=0)
    for pending in rows:
        with locked(store, pending['id']) as acquired:
            if not acquired:
                result['deferred'] += 1
                continue
            with store.connect() as db:
                row = db.execute('SELECT * FROM discover_requests WHERE id=?', (pending['id'],)).fetchone()
                receipt = db.execute('SELECT * FROM discover_handoffs WHERE request_id=?', (pending['id'],)).fetchone()
                if not row or row['state'] != 'starting':
                    continue
                if receipt and receipt['token'] != pending['token']:
                    continue
                permitted = allowed_in(db, store, row, row['stop_generation']) if receipt else False
            if receipt and not permitted:
                # Do not release any hold or resurrect a stopped generation.
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    current=db.execute('SELECT * FROM discover_requests WHERE id=?',(row['id'],)).fetchone()
                    latest=db.execute('SELECT * FROM discover_handoffs WHERE request_id=?',(row['id'],)).fetchone()
                    if not latest or latest['token']!=receipt['token']:continue
                    if current and current['state']=='starting' and not allowed_in(db,store,current,current['stop_generation']):
                        db.execute("UPDATE discover_requests SET state='cancelled',message='Interrupted handoff was stopped. Saved files and metadata were kept.',updated=? WHERE id=?",
                                   (time.time(),row['id']))
                    db.execute("UPDATE discover_handoffs SET stage='abandoned',updated=? WHERE request_id=? AND token=? AND stage IN ('claimed','prepared')",
                               (time.time(), row['id'], receipt['token']))
                continue
            try:
                if receipt and receipt['stage'] == 'prepared' and attach_native(store, row['id'], row['stop_generation']):
                    result['attached'] += 1
                    continue
            except (OSError, ValueError):
                # Corrupt or mismatched evidence is never treated as absence.
                message = 'Interrupted download handoff could not be verified. Saved files and metadata were kept; retry explicitly.'
            else:
                message = '' if receipt else 'An older interrupted handoff has no saved job identity. Saved files and metadata were kept; retry explicitly.'
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT * FROM discover_requests WHERE id=?', (row['id'],)).fetchone()
                if receipt and not allowed_in(db, store, current, row['stop_generation']):
                    continue
                if not receipt and (not current or current['state'] != 'starting' or current['updated'] >= time.time() - LEGACY_GRACE):
                    continue
                from .media_holds import blocked_in, request_keys_in
                if blocked_in(db, request_keys_in(db, current)) or not open_in(db, store):
                    continue
                state = 'failed' if message else 'searching'
                db.execute('UPDATE discover_requests SET state=?,message=?,updated=? WHERE id=?',
                           (state, message or 'Recovering an interrupted source search.', time.time(), row['id']))
                if receipt:
                    db.execute("UPDATE discover_handoffs SET stage='abandoned',updated=? WHERE request_id=? AND token=?",
                               (time.time(), row['id'], receipt['token']))
                result['attention' if message else 'requeued'] += 1
    return result
