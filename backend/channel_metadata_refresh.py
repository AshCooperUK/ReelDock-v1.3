"""Durable, bounded public channel information for the channel being viewed.

This queue changes display metadata only. It never changes subscriptions,
upload cursors, download rules, history or acquisition holds.
"""
import hashlib
import json
import re
import secrets
import time

CHANNEL = re.compile(r'UC[A-Za-z0-9_-]{22}')
FRESH_SECONDS = 86400
COMPLETE_SECONDS = 30 * 86400
REQUEST_SECONDS = 300
RETRY_SECONDS = 60
LEASE_SECONDS = 120


def initialise(store):
    with store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS channel_metadata_refresh_requests (
            channel TEXT PRIMARY KEY,requested REAL NOT NULL DEFAULT 0,expires REAL NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'idle',checked REAL NOT NULL DEFAULT 0,retry_at REAL NOT NULL DEFAULT 0,
            claim TEXT NOT NULL DEFAULT '',lease_until REAL NOT NULL DEFAULT 0)''')
        db.execute('''CREATE TRIGGER IF NOT EXISTS channel_metadata_refresh_removed
            AFTER DELETE ON channels BEGIN
            DELETE FROM channel_metadata_refresh_requests WHERE channel=OLD.id; END''')


def _paused(db):
    return db.execute("SELECT 1 FROM settings WHERE key IN ('youtube_sync_paused','reset_in_progress') AND value IN ('true','1') LIMIT 1").fetchone() is not None


def _fresh(data, now):
    try:
        checked = max(float(data.get('metadataChecked') or 0), float(data.get('art_checked') or 0))
    except (TypeError, ValueError):
        return False
    complete = bool(data.get('banner')) and data.get('subscribers') not in (None, '')
    return checked > 0 and checked > now - (COMPLETE_SECONDS if complete else FRESH_SECONDS)


def status(store, channel):
    now = time.time()
    with store.connect() as db:
        owner = db.execute('SELECT data FROM channels WHERE id=? AND active=1', (channel,)).fetchone()
        row = db.execute('SELECT * FROM channel_metadata_refresh_requests WHERE channel=?', (channel,)).fetchone()
        paused = _paused(db)
    state, notice, delay = 'idle', '', 0
    if owner:
        if paused:
            state, notice = 'paused', 'Channel information updates are paused. Showing saved information.'
        elif row and row['lease_until'] > now:
            state, notice, delay = 'running', 'Checking this channel’s banner and information…', 2000
        elif row and row['expires'] > now and row['retry_at'] > now:
            state, notice, delay = 'retry', 'Channel information will retry shortly. Showing saved information.', min(5000, max(1000, int((row['retry_at']-now)*1000)))
        elif _fresh(json.loads(owner['data']), now):
            state = 'fresh'
        elif row and row['expires'] > now and row['state'] in ('queued', 'running', 'retry'):
            state, notice, delay = 'queued', 'This channel’s banner and information are next to update…', 2000
    result = dict(state=state, notice=notice, refreshAfterMs=delay)
    if row and row['retry_at'] > now:
        result['retryAt'] = row['retry_at']
    return result


def request(store, channel):
    if not isinstance(channel, str) or not CHANNEL.fullmatch(channel):
        raise ValueError('Choose a valid YouTube channel.')
    now = time.time()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        owner = db.execute('SELECT data FROM channels WHERE id=? AND active=1', (channel,)).fetchone()
        if not owner:
            raise LookupError('Add this channel to ReelDock before requesting its information.')
        if not _paused(db):
            db.execute('INSERT OR IGNORE INTO channel_metadata_refresh_requests(channel) VALUES(?)', (channel,))
            row = db.execute('SELECT * FROM channel_metadata_refresh_requests WHERE channel=?', (channel,)).fetchone()
            state = 'running' if row['lease_until'] > now else 'retry' if row['retry_at'] > now else 'fresh' if _fresh(json.loads(owner['data']), now) else 'queued'
            db.execute('UPDATE channel_metadata_refresh_requests SET requested=?,expires=?,state=? WHERE channel=?',
                       (now, now+REQUEST_SECONDS, state, channel))
    return status(store, channel)


def _claim(store):
    now = time.time()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if _paused(db):
            return None
        db.execute('DELETE FROM channel_metadata_refresh_requests WHERE channel NOT IN (SELECT id FROM channels WHERE active=1)')
        row = db.execute('''SELECT r.*,c.data FROM channel_metadata_refresh_requests r JOIN channels c ON c.id=r.channel
            WHERE c.active=1 AND r.requested>0 AND r.expires>? AND r.retry_at<=? AND r.lease_until<=?
            AND r.state IN ('queued','retry','running') ORDER BY r.requested DESC,r.channel LIMIT 1''', (now, now, now)).fetchone()
        if not row:
            return None
        if _fresh(json.loads(row['data']), now):
            db.execute("UPDATE channel_metadata_refresh_requests SET state='fresh',claim='',lease_until=0 WHERE channel=?", (row['channel'],))
            return None
        token = secrets.token_hex(16)
        db.execute("UPDATE channel_metadata_refresh_requests SET state='running',claim=?,lease_until=? WHERE channel=?", (token, now+LEASE_SECONDS, row['channel']))
        return row['channel'], token


def tick(importer, stop=None):
    store = importer.store
    if (stop is not None and stop.is_set()) or not store.get('setup_complete') or store.reset_pending():
        return False
    claim = _claim(store)
    if claim is None:
        return False
    channel, token = claim
    try:
        if stop is not None and stop.is_set():
            return False
        from .youtube_fallback import extract, channel_banner, channel_followers
        data = extract(importer, channel, 'channel', public=True, timeout=30)
        if not isinstance(data, dict) or (data.get('channel_id') or data.get('id')) != channel:
            raise ValueError('Unexpected channel information.')
        banner = channel_banner(data)
        followers = channel_followers(data)
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            owner = db.execute('''SELECT 1 FROM channels c JOIN channel_metadata_refresh_requests r ON r.channel=c.id
                WHERE c.id=? AND c.active=1 AND r.claim=?''', (channel, token)).fetchone()
            if not owner or _paused(db) or (stop is not None and stop.is_set()):
                return True
            now = time.time()
            updates = {'art_checked': now, 'metadataChecked': now}
            if isinstance(data.get('description'), str) and data['description'].strip():
                updates['description'] = data['description'][:10000]
            if followers is not None:
                updates['subscribers'] = followers
            if banner:
                from .m3u import validate_url
                banner = validate_url('https:'+banner if banner.startswith('//') else banner)
                digest = hashlib.sha256(banner.encode()).hexdigest()
                # Same thumbnail cache contract, inside the authoritative write
                # transaction so removal/pause cannot leave late artwork rows.
                db.execute('INSERT OR IGNORE INTO artwork(id,url) VALUES(?,?)', (digest, banner))
                db.execute('''INSERT INTO artwork_requests(id,requested) VALUES(?,?)
                    ON CONFLICT(id) DO UPDATE SET requested=excluded.requested''', (digest, now))
                updates['banner'] = '/artwork/'+digest
            paths = ','.join("'$."+key+"',?" for key in updates)
            db.execute('UPDATE channels SET data=json_set(data,'+paths+') WHERE id=? AND active=1', (*updates.values(), channel))
            db.execute("UPDATE channel_metadata_refresh_requests SET state='fresh',checked=?,retry_at=0,claim='',lease_until=0 WHERE channel=? AND claim=?", (now, channel, token))
        return True
    except Exception as exc:
        from .sync_controls import SyncPaused
        if isinstance(exc, SyncPaused):
            return True
        with store.connect() as db:
            db.execute("UPDATE channel_metadata_refresh_requests SET state='retry',retry_at=?,claim='',lease_until=0 WHERE channel=? AND claim=?", (time.time()+RETRY_SECONDS, channel, token))
        return True
    finally:
        with store.connect() as db:
            db.execute("UPDATE channel_metadata_refresh_requests SET state=CASE WHEN expires>? THEN 'queued' ELSE 'idle' END,claim='',lease_until=0 WHERE channel=? AND claim=?", (time.time(), channel, token))


def recover(store):
    with store.connect() as db:
        db.execute('DELETE FROM channel_metadata_refresh_requests WHERE channel NOT IN (SELECT id FROM channels WHERE active=1)')
        db.execute("UPDATE channel_metadata_refresh_requests SET state=CASE WHEN expires>? THEN 'queued' ELSE 'idle' END,claim='',lease_until=0 WHERE state='running'", (time.time(),))


def loop(app,store,stop):
    from .catalogue import Importer
    importer=Importer(app)
    while not stop.is_set():
        with store.background_operation():
            try:tick(importer,stop)
            except Exception:app.logger.warning('Viewed channel information will retry.')
        stop.wait(1)
