"""Explicit per-media Clear intent, independent of section clearing and scheduling."""
import hashlib
import fcntl
import json
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

_NATIVE_CACHE = {}
_NATIVE_REFRESH_LOCKS = {}
_LOCK = threading.Lock()
_ACTION_LOCKS = {}
_ACTION_LOCAL = threading.local()


@contextmanager
def explicit_action(store, *, blocking=True):
    """Serialize Clear/Resume; publication may check the same fence without waiting."""
    directory=Path(store.directory).resolve()
    identity=str(directory)
    with _LOCK:
        lock=_ACTION_LOCKS.get(identity)
        if lock is None:
            lock=threading.RLock();_ACTION_LOCKS[identity]=lock
    acquired=lock.acquire(blocking=blocking)
    if not acquired:
        yield False
        return
    descriptor=None
    try:
        depths=getattr(_ACTION_LOCAL,'depths',None)
        if depths is None:
            depths={};_ACTION_LOCAL.depths=depths
        if identity in depths:
            depths[identity]+=1
            try:yield True
            finally:depths[identity]-=1
            return
        descriptor=os.open(directory/'activity-actions.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
        try:
            info=os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
                raise OSError('The Activity action lock is unavailable.')
            os.fchmod(descriptor,0o600)
            try:
                fcntl.flock(descriptor,fcntl.LOCK_EX|(0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                if blocking:
                    raise
                os.close(descriptor)
                descriptor=None
                lock.release()
                acquired=False
                yield False
                return
            depths[identity]=1
            try:yield True
            finally:depths.pop(identity,None)
        finally:
            # Closing this outermost thread-owned descriptor releases flock;
            # nested calls never open another descriptor for the same file.
            if descriptor is not None:
                os.close(descriptor)
    finally:
        if acquired:
            lock.release()


def initialise(store):
    with store.connect() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS media_clear_holds(
            key TEXT PRIMARY KEY,group_id TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS media_clear_rows(
            kind TEXT NOT NULL,identity TEXT NOT NULL,group_id TEXT NOT NULL,
            PRIMARY KEY(kind,identity));
            CREATE INDEX IF NOT EXISTS media_clear_group ON media_clear_holds(group_id);
            CREATE INDEX IF NOT EXISTS media_clear_row_group ON media_clear_rows(group_id);
            CREATE TABLE IF NOT EXISTS media_request_keys(
            request_id TEXT NOT NULL,key TEXT NOT NULL,PRIMARY KEY(request_id,key));''')


def _keys(keys):
    return list(dict.fromkeys([keys] if isinstance(keys,str) else keys))


def vod_key(source, path):
    return 'vod:'+hashlib.sha256(json.dumps([source,path],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def request_keys(row):
    """Only verified structured identities, never display titles or filenames."""
    row=dict(row);raw=row.get('body') or {}
    try:body=json.loads(raw) if isinstance(raw,str) else raw
    except (TypeError,ValueError):body={}
    if not isinstance(body,dict):body={}
    keys=[]
    if row.get('id'):keys.append('request:'+str(row['id']))
    if row.get('episode_key'):keys.append('managed:'+str(row['episode_key']))
    item=body.get('item') if isinstance(body.get('item'),dict) else {}
    video=body.get('videoId') or item.get('videoId')
    if re.fullmatch(r'[A-Za-z0-9_-]{11}',str(video or '')):keys+=['video:'+video,'managed:video:'+video]
    if row.get('engine') in ('nzb','torrent') and row.get('engine_id'):
        keys.append('native:'+row['engine']+'-job:'+str(row['engine_id']))
    return list(dict.fromkeys(keys))


def blocked_in(db, keys):
    keys=_keys(keys)
    if not keys or not db.execute("SELECT 1 FROM sqlite_master WHERE name='media_clear_holds'").fetchone():return False
    return bool(db.execute('SELECT 1 FROM media_clear_holds WHERE key IN ('+','.join('?' for _ in keys)+') LIMIT 1',keys).fetchone())


def blocked(store, keys):
    with store.connect() as db:return blocked_in(db,keys)


def request_keys_in(db, row):
    keys=request_keys(row)
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='media_request_keys'").fetchone():
        keys.extend(value[0] for value in db.execute('SELECT key FROM media_request_keys WHERE request_id=?',(row['id'],)))
    return list(dict.fromkeys(keys))


def bind_request(store, request_id, generation, keys, *, body=None):
    """Bind verified downstream identities before any engine may begin work.

    Clear either wins this write fence, or sees the binding and holds the new
    content identity before acknowledging. Provider/preview work stays outside.
    """
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM discover_requests WHERE id=?',(request_id,)).fetchone()
        keys=_keys(keys)
        if not row or row['state']!='starting' or int(row['stop_generation'] or 0)!=int(generation):return False
        if blocked_in(db,request_keys_in(db,row)+keys):return False
        db.executemany('INSERT OR IGNORE INTO media_request_keys VALUES(?,?)',[(request_id,key) for key in keys])
        if body is not None:db.execute('UPDATE discover_requests SET body=? WHERE id=?',(json.dumps(body),request_id))
    return True


def hold(db, keys, rows):
    """Caller holds BEGIN IMMEDIATE: fence retries before cancelling any worker."""
    keys=_keys(keys)
    groups=set()
    for key in keys:
        found=db.execute('SELECT group_id FROM media_clear_holds WHERE key=?',(key,)).fetchone()
        if found:groups.add(found[0])
    for kind,identity in rows:
        found=db.execute('SELECT group_id FROM media_clear_rows WHERE kind=? AND identity=?',(kind,str(identity))).fetchone()
        if found:groups.add(found[0])
    group=sorted(groups)[0] if groups else uuid.uuid4().hex
    for previous in groups-{group}:
        db.execute('UPDATE media_clear_holds SET group_id=? WHERE group_id=?',(group,previous))
        db.execute('UPDATE media_clear_rows SET group_id=? WHERE group_id=?',(group,previous))
    db.executemany('INSERT OR REPLACE INTO media_clear_holds VALUES(?,?,?)',[(key,group,time.time()) for key in keys])
    db.executemany('INSERT OR REPLACE INTO media_clear_rows VALUES(?,?,?)',[(kind,str(identity),group) for kind,identity in rows])
    with _LOCK:_NATIVE_CACHE.clear()
    return group


def resume(store, keys):
    """Only explicit selected-media Download/Retry/Resume may release its group."""
    keys=_keys(keys)
    with explicit_action(store):
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='media_clear_holds'").fetchone():return False
            groups={row[0] for key in keys for row in db.execute('SELECT group_id FROM media_clear_holds WHERE key=?',(key,))}
            for group in groups:
                linked=[row[0] for row in db.execute('SELECT key FROM media_clear_holds WHERE group_id=?',(group,))]
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='managed_media'").fetchone():
                    db.executemany('UPDATE managed_media SET hold=0 WHERE key=?',[(key[len('managed:'):],) for key in linked if key.startswith('managed:')])
                db.execute('DELETE FROM media_clear_rows WHERE group_id=?',(group,))
                db.execute('DELETE FROM media_clear_holds WHERE group_id=?',(group,))
            if groups and db.execute("SELECT 1 FROM sqlite_master WHERE name='activity_clear_revision'").fetchone():
                db.execute('UPDATE activity_clear_revision SET revision=revision+1 WHERE id=1')
        with _LOCK:_NATIVE_CACHE.clear()
        return bool(groups)


def suppressed_ids(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='media_clear_rows'").fetchone():return {}
    result={}
    for kind,identity in db.execute('SELECT r.kind,r.identity FROM media_clear_rows r WHERE EXISTS(SELECT 1 FROM media_clear_holds h WHERE h.group_id=r.group_id)'):
        result.setdefault(kind,[]).append(identity)
    return result


def suppressed_sql(kind, alias):
    return f"NOT EXISTS(SELECT 1 FROM media_clear_rows mr WHERE mr.kind='{kind}' AND mr.identity=CAST({alias}.id AS TEXT) AND EXISTS(SELECT 1 FROM media_clear_holds mh WHERE mh.group_id=mr.group_id))"


def native_blocked(data_dir, keys, *, fresh=False):
    """Read-only native-engine checkpoint, cached at most 200ms; no Store creation."""
    keys=tuple(_keys(keys));path=Path(data_dir).parent/'reeldock.sqlite3'
    if not path.is_file():return False
    identity=(str(path),keys);now=time.monotonic()
    with _LOCK:
        cached=_NATIVE_CACHE.get(identity)
        if not fresh and cached and now-cached[0]<0.2:return cached[1]
        refresh=_NATIVE_REFRESH_LOCKS.get(identity)
        if refresh is None:
            refresh=threading.Lock();_NATIVE_REFRESH_LOCKS[identity]=refresh
    # All article readers for one job reach the same 200ms expiry together.
    # Refresh once rather than opening dozens of identical SQLite readers.
    # Different jobs remain independent; explicit publication checks still
    # read authoritatively even if another reader has just refreshed the cache.
    with refresh:
        now=time.monotonic()
        with _LOCK:cached=_NATIVE_CACHE.get(identity)
        if not fresh and cached and now-cached[0]<0.2:return cached[1]
        try:
            db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)
            try:value=blocked_in(db,keys)
            finally:db.close()
        except (OSError,sqlite3.Error):value=True  # Never restart a job through an unreadable hold.
        with _LOCK:_NATIVE_CACHE[identity]=(now,value)
        return value
