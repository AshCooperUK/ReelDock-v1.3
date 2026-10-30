"""Fetch only bounded images from YouTube's artwork hosts, never browser hotlinks."""
import http.client
import ssl
import socket
import time
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from .network import public_addresses


def fetch_image(url):
    from .metadata_control import checkpoint
    checkpoint()
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not any(host == suffix or host.endswith('.' + suffix) for suffix in ('ytimg.com', 'ggpht.com', 'googleusercontent.com'))):
        raise ValueError('Unsupported artwork address')
    addresses = public_addresses(host)
    connection = http.client.HTTPSConnection(host, timeout=8)
    try:
        raw = socket.create_connection((addresses[0], 443), timeout=8)
        try:
            connection.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        except Exception:
            raw.close()
            raise
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        connection.request('GET', path, headers={'Host': host})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('Artwork unavailable')
        body = response.read(2_000_001)
        checkpoint()
        if len(body) > 2_000_000 or not (body.startswith(b'\xff\xd8\xff') or body.startswith(b'\x89PNG\r\n\x1a\n')
                or (body.startswith(b'RIFF') and body[8:12] == b'WEBP')):
            raise ValueError('Unsupported image')
        return body
    finally:
        connection.close()


# Old queue entries remain available, but only logos, recent thumbnails and
# artwork requested by a browser are eligible for background downloading.
QUEUE = """
    WITH refs AS (
      SELECT substr(json_extract(c.data,'$.banner'),10) id, -1 priority, r.requested stamp
        FROM channels c JOIN channel_metadata_refresh_requests r ON r.channel=c.id
        WHERE c.active=1 AND r.expires>=:now AND r.requested>0
      UNION ALL
      SELECT substr(json_extract(data,'$.avatar'),10) id, 0 priority, 0 stamp FROM channels WHERE active=1
      UNION ALL
      SELECT substr(json_extract(data,'$.thumbnail'),10), 2, unixepoch(published) FROM videos
        WHERE unixepoch(published)>=:recent
      UNION ALL
      SELECT id, 1, 0 FROM artwork_requests WHERE requested>=:requested
    ), ranked AS (SELECT id, MIN(priority) priority, MAX(stamp) stamp FROM refs WHERE id IS NOT NULL GROUP BY id),
    eligible AS (
      SELECT a.*, r.priority, r.stamp FROM ranked r
        JOIN artwork a ON a.id=r.id
    )
"""


def repair_relative_urls(store):
    """Earlier cookie imports saved '//host/...' avatars that fetch_image rejects.

    Keep each id (channels reference it) and queue the image again."""
    with store.connect() as db:
        db.execute("UPDATE artwork SET url='https:'||url, checked=0 WHERE url LIKE '//%'")


def queue_times():
    return dict(now=time.time(), recent=time.time()-7*86400, requested=time.time()-86400, stale=time.time()-7*86400)


def queue_counts(store):
    with store.connect() as db:
        pending = db.execute(QUEUE+'SELECT COUNT(*) FROM eligible WHERE checked=0', queue_times()).fetchone()[0]
        total = db.execute('SELECT COUNT(*) FROM artwork WHERE checked=0').fetchone()[0]
    return pending, max(0,total-pending)


def request_artwork(store, key):
    # Queue only known URLs; never accept a browser-supplied upstream address.
    with store.connect() as db:
        db.execute("""INSERT INTO artwork_requests(id,requested)
            SELECT id,? FROM artwork WHERE id=?
            ON CONFLICT(id) DO UPDATE SET requested=excluded.requested
            WHERE artwork_requests.requested<?""", (time.time(),key,time.time()-3600))


def cache_batch(store, fetch=fetch_image, limit=30, workers=4):
    if store.get("youtube_share_metadata", "true") != "true": return 0
    generation = store.get("youtube_metadata_generation", "0")
    def enabled():
        return store.get("youtube_share_metadata", "true") == "true" and store.get("youtube_metadata_generation", "0") == generation
    folder = store.directory / 'artwork'
    folder.mkdir(exist_ok=True, mode=0o700)
    with store.connect() as db:
        rows = db.execute(QUEUE+"""
            SELECT * FROM eligible WHERE checked<:stale
            ORDER BY priority, (checked=0) DESC, stamp DESC, id LIMIT :limit
        """, {**queue_times(),'limit':limit}).fetchall()

    def download(row):
        if store.reset_pending() or not enabled():return False
        temporary = folder / (row['id'] + '.tmp')
        try:
            data = fetch(row['url'])
            if not enabled(): return False
            temporary.write_bytes(data)
        except (OSError, ValueError, http.client.HTTPException):
            checked = time.time()-6*86400  # Retry tomorrow, not on every batch.
            success = False
        else:
            checked, success = time.time(), True
        try:
            with store.connect() as db:
                # Serialize publication with catalogue pruning. A pruned item
                # must not reappear as an orphaned file after its fetch finishes.
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT 1 FROM artwork WHERE id=?', (row['id'],)).fetchone():
                    if success:
                        temporary.replace(folder / row['id'])
                    db.execute('UPDATE artwork SET checked=? WHERE id=?', (checked,row['id']))
        finally:
            temporary.unlink(missing_ok=True)
        store.set('artwork_heartbeat', time.time())
        return success

    from .activity import record
    if rows:record(store,'youtube-artwork','YouTube artwork','running',f'Downloading {len(rows)} channel images and thumbnails.')
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='artwork') as pool:
            results=list(pool.map(download, rows))
    except Exception:
        record(store,'youtube-artwork','YouTube artwork','error','Artwork processing was interrupted. The worker will retry.')
        raise
    else:
        if rows and enabled():
            from .event_log import artwork_batch
            artwork_batch(store,sum(results),len(results)-sum(results))
            record(store,'youtube-artwork','YouTube artwork','complete',f'Finished processing {len(rows)} images.')
    return len(rows)


def cache_loop(store, stop):
    """A separate bounded downloader continues while metadata scans are busy."""
    while not stop.is_set():
        paused = True
        count = 0
        with store.background_operation():
            if store.reset_pending():
                paused = True
            else:
                paused = False
                try:
                    store.set('artwork_heartbeat', time.time())
                    count = cache_batch(store) if store.get('setup_complete') else 0
                    store.set('artwork_error', '')
                except Exception:
                    count = 0
                    logging.exception('Artwork cycle failed; retrying')
                    try:
                        store.set('artwork_error', 'Artwork downloads paused after an error. Retrying shortly.')
                    except (sqlite3.Error, OSError):
                        logging.warning('Could not save artwork status; retrying on the next cycle')
        if paused:
            stop.wait(1)
            continue
        if stop.wait(1 if count else 5):
            break
