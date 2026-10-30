"""Bounded, coalesced recent-upload reads for a channel the owner is viewing.

The web request only records demand. One dedicated worker lane reads public
RSS; the ordinary import uses the same claim and never duplicates that read.
"""
import json
import re
import secrets
import time

CHANNEL = re.compile(r'UC[A-Za-z0-9_-]{22}')
FRESH_SECONDS = 300
REQUEST_SECONDS = 300
RETRY_SECONDS = 60
LEASE_SECONDS = 120


def initialise(store):
    with store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS channel_refresh_requests (
            channel TEXT PRIMARY KEY,requested REAL NOT NULL DEFAULT 0,
            expires REAL NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'idle',
            checked REAL NOT NULL DEFAULT 0,retry_at REAL NOT NULL DEFAULT 0,
            claim TEXT NOT NULL DEFAULT '',lease_until REAL NOT NULL DEFAULT 0)''')
        db.execute('''CREATE TRIGGER IF NOT EXISTS channel_refresh_removed
            AFTER DELETE ON channels BEGIN
            DELETE FROM channel_refresh_requests WHERE channel=OLD.id; END''')


def _paused(db):
    return db.execute("SELECT 1 FROM settings WHERE key IN ('youtube_sync_paused','reset_in_progress') AND value IN ('true','1') LIMIT 1").fetchone() is not None


def status(store, channel, now=None):
    """Cached read only: opening/polling GET never creates a refresh request."""
    now = time.time() if now is None else now
    with store.connect() as db:
        owner = db.execute('SELECT data FROM channels WHERE id=? AND active=1', (channel,)).fetchone()
        row = db.execute('SELECT * FROM channel_refresh_requests WHERE channel=?', (channel,)).fetchone()
        paused = _paused(db)
    state, notice, wait = 'idle', '', 0
    if owner:
        data = json.loads(owner['data'])
        if paused:
            state, notice = 'paused', 'YouTube updates are paused. Showing saved uploads.'
        elif row and row['lease_until'] > now:
            state, notice, wait = 'running', 'Checking recent uploads for this channel…', 2000
        elif row and row['expires'] > now and row['retry_at'] > now:
            state, notice, wait = 'retry', 'Recent uploads could not update yet. Showing saved uploads; retrying shortly.', min(5000, max(1000, int((row['retry_at']-now)*1000)))
        elif row and row['expires'] > now and row['state'] in ('queued','running','retry'):
            state, notice, wait = 'queued', 'This channel is next for a recent-upload check…', 2000
        elif float(data.get('last_checked') or 0) > now-FRESH_SECONDS and not data.get('uploads_error'):
            state = 'fresh'
    result = dict(state=state, notice=notice, refreshAfterMs=wait)
    if row and row['retry_at'] > now:
        result['retryAt'] = row['retry_at']
    return result


def request_recent(store, channel, now=None):
    if not isinstance(channel, str) or not CHANNEL.fullmatch(channel):
        raise ValueError('Choose a valid YouTube channel.')
    now = time.time() if now is None else now
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        owner = db.execute('SELECT data FROM channels WHERE id=? AND active=1', (channel,)).fetchone()
        if not owner:
            raise LookupError('Add this channel to ReelDock before requesting its uploads.')
        if not _paused(db):
            data = json.loads(owner['data'])
            db.execute('INSERT OR IGNORE INTO channel_refresh_requests(channel) VALUES(?)', (channel,))
            row = db.execute('SELECT * FROM channel_refresh_requests WHERE channel=?', (channel,)).fetchone()
            fresh = float(data.get('last_checked') or 0) > now-FRESH_SECONDS and not data.get('uploads_error')
            state = 'running' if row['lease_until'] > now else 'retry' if row['retry_at'] > now else 'fresh' if fresh else 'queued'
            db.execute('UPDATE channel_refresh_requests SET requested=?,expires=?,state=? WHERE channel=?',
                       (now, now+REQUEST_SECONDS, state, channel))
    return status(store, channel, now)


def _claim(store, channel, priority, since):
    now = time.time()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        owner = db.execute('SELECT data FROM channels WHERE id=? AND active=1', (channel,)).fetchone()
        if not owner or _paused(db):
            return None
        db.execute('INSERT OR IGNORE INTO channel_refresh_requests(channel) VALUES(?)', (channel,))
        row = db.execute('SELECT * FROM channel_refresh_requests WHERE channel=?', (channel,)).fetchone()
        if row['lease_until'] > now or row['retry_at'] > now:
            return None
        data = json.loads(owner['data'])
        checked = float(data.get('last_checked') or 0)
        if not data.get('uploads_error') and (since is not None and checked >= since or checked > now-FRESH_SECONDS and (priority or row['expires'] > now)):
            db.execute("UPDATE channel_refresh_requests SET state='fresh' WHERE channel=?", (channel,))
            return None
        token = secrets.token_hex(16)
        db.execute("UPDATE channel_refresh_requests SET state='running',claim=?,lease_until=? WHERE channel=?", (token, now+LEASE_SECONDS, channel))
        return token


def _can_publish(store, channel, token, stop):
    if stop is not None and stop.is_set():
        return False
    with store.connect() as db:
        return not _paused(db) and db.execute('''SELECT 1 FROM channels c JOIN channel_refresh_requests r ON r.channel=c.id
            WHERE c.id=? AND c.active=1 AND r.claim=?''', (channel, token)).fetchone() is not None


def refresh(importer, channel, *, priority=False, stop=None, since=None):
    """Share the one feed claim with the bulk importer; provider I/O is outside SQL."""
    store = importer.store
    if stop is not None and stop.is_set():
        return None
    token = _claim(store, channel, priority, since)
    if token is None:
        return None
    try:
        from .youtube_fallback import feed, placeholder, queue_details
        items = feed(importer, channel)
        if not _can_publish(store, channel, token, stop):
            return None
        placeholder(importer, items)
        if not _can_publish(store, channel, token, stop):
            return None
        queue_details(store, items)
        with store.connect() as db:
            changing = [dict(id=row['id'], snippet={'channelId': channel}) for row in db.execute("SELECT id FROM videos WHERE channel=? AND json_extract(data,'$.state') IN ('live','scheduled')", (channel,))]
        queue_details(store, changing)
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if _paused(db) or not db.execute('SELECT 1 FROM channel_refresh_requests WHERE channel=? AND claim=?', (channel, token)).fetchone():
                return None
            # Never replace a channel JSON object read before a slow request.
            changed = db.execute("UPDATE channels SET data=json_remove(json_set(data,'$.last_checked',?),'$.uploads_error') WHERE id=? AND active=1", (time.time(), channel)).rowcount
            if changed:
                db.execute("UPDATE channel_refresh_requests SET state='fresh',checked=?,retry_at=0,claim='',lease_until=0 WHERE channel=? AND claim=?", (time.time(), channel, token))
        return dict(success=bool(changed), items=items)
    except Exception as exc:
        from .sync_controls import SyncPaused
        if isinstance(exc, SyncPaused):
            raise
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM channel_refresh_requests WHERE channel=? AND claim=?', (channel, token)).fetchone():
                db.execute("UPDATE channels SET data=json_set(data,'$.last_checked',?,'$.uploads_error',?) WHERE id=? AND active=1", (time.time(), 'Upload feed unavailable. The next check will retry.', channel))
                db.execute("UPDATE channel_refresh_requests SET state='retry',retry_at=?,claim='',lease_until=0 WHERE channel=? AND claim=?", (time.time()+RETRY_SECONDS, channel, token))
        return dict(success=False, items=[])
    finally:
        # A pause, close or membership change must not leave an in-flight lease.
        with store.connect() as db:
            db.execute("UPDATE channel_refresh_requests SET state=CASE WHEN expires>? THEN 'queued' ELSE 'idle' END,claim='',lease_until=0 WHERE channel=? AND claim=?", (time.time(), channel, token))


def recover(store):
    with store.connect() as db:
        db.execute('DELETE FROM channel_refresh_requests WHERE channel NOT IN (SELECT id FROM channels WHERE active=1)')
        db.execute("UPDATE channel_refresh_requests SET state=CASE WHEN expires>? THEN 'queued' ELSE 'idle' END,claim='',lease_until=0 WHERE state='running'", (time.time(),))


def tick(importer, stop=None):
    store = importer.store
    if stop is not None and stop.is_set() or not store.get('setup_complete') or store.reset_pending() or store.get('youtube_sync_paused') == 'true':
        return None
    now = time.time()
    with store.connect() as db:
        db.execute('DELETE FROM channel_refresh_requests WHERE channel NOT IN (SELECT id FROM channels WHERE active=1)')
        row = db.execute('''SELECT r.channel FROM channel_refresh_requests r JOIN channels c ON c.id=r.channel
            WHERE c.active=1 AND r.requested>0 AND r.expires>? AND r.retry_at<=? AND r.lease_until<=?
            AND r.state IN ('queued','retry','running') ORDER BY r.requested DESC,r.channel LIMIT 1''', (now, now, now)).fetchone()
    return refresh(importer, row['channel'], priority=True, stop=stop) if row else None


def loop(app, store, stop):
    from .catalogue import Importer
    importer = Importer(app)
    while not stop.is_set():
        with store.background_operation():
            try:
                tick(importer, stop)
            except Exception:
                # Provider/HTTP text can contain access data. Store fixed text only.
                app.logger.warning('Viewed channel upload check will retry.')
        stop.wait(1)
