"""ReelDock subscriptions, with a later YouTube account sync.

Subscribe always adds the channel to ReelDock. If Google can manage
subscriptions, or signed-in cookies are saved, the same change is sent to
YouTube. Otherwise it waits and is sent when either of those is connected.
"""
import hashlib
import http.cookiejar
import json
import re
import time

import requests

CHANNEL = re.compile(r'UC[A-Za-z0-9_-]{22}')
WRITE_SCOPES = {
    'https://www.googleapis.com/auth/youtube',
    'https://www.googleapis.com/auth/youtube.force-ssl',
}
RETRY_AFTER = 900


def initialise(store):
    from .catalogue import initialise as catalogue_initialise
    from .media_library import initialise as media_initialise
    from .youtube_fallback import initialise as fallback_initialise
    catalogue_initialise(store)
    media_initialise(store)
    fallback_initialise(store)
    with store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS youtube_account_subscriptions(
            channel TEXT PRIMARY KEY, intent TEXT NOT NULL, status TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '', updated REAL NOT NULL)''')


def channel_of(item):
    if not isinstance(item, dict):
        return ''
    channel_id = str(item.get('channelId') or '')
    if not CHANNEL.fullmatch(channel_id) and str(item.get('id') or '').startswith('youtube:channel:'):
        channel_id = str(item['id']).split(':', 2)[-1]
    return channel_id if CHANNEL.fullmatch(channel_id) else ''


def active_ids(store):
    initialise(store)
    with store.connect() as db:
        return [row[0] for row in db.execute('SELECT id FROM channels WHERE active=1 ORDER BY id')]


def google_ready(store):
    from .youtube_source import enabled, fallback
    if not enabled(store) or fallback(store) or not store.get('google_tokens'):
        return False
    try:
        tokens = json.loads(store.get('google_tokens') or '{}')
    except json.JSONDecodeError:
        return False
    return bool(WRITE_SCOPES.intersection(set(str(tokens.get('scope') or '').split())))


def cookies_ready(store):
    from .watched_status import cookie_session_file_status
    cookie = store.directory / 'youtube-cookies.txt'
    return cookie.is_file() and cookie_session_file_status(cookie)[0] == 'unchecked'


def _queue(store, channel, intent):
    initialise(store)
    with store.connect() as db:
        current = db.execute('SELECT intent, status FROM youtube_account_subscriptions WHERE channel=?', (channel,)).fetchone()
        if current and current['status'] == 'synced' and current['intent'] == intent:
            return
        db.execute('''INSERT INTO youtube_account_subscriptions(channel,intent,status,message,updated)
            VALUES(?,?,'waiting','',?)
            ON CONFLICT(channel) DO UPDATE SET intent=excluded.intent, status='waiting', message='', updated=excluded.updated''',
                   (channel, intent, time.time()))


def _finish(store, channel, status, message):
    with store.connect() as db:
        db.execute('UPDATE youtube_account_subscriptions SET status=?, message=?, updated=? WHERE channel=?',
                   (status, message[:300], time.time(), channel))


def subscribe_local(store, item):
    """Add the channel to ReelDock subscriptions. Saved files and rules stay."""
    channel_id = channel_of(item)
    if not channel_id:
        raise ValueError('This channel has no YouTube channel ID, so it cannot be subscribed.')
    initialise(store)
    name = str(item.get('title') or item.get('channel') or 'YouTube')[:180]
    avatar = str(item.get('image') or '')
    if not avatar.startswith(('https://yt3.ggpht.com/', 'https://yt3.googleusercontent.com/', '/artwork/')):
        avatar = ''
    from .download_options import read
    options = read(store)
    with store.connect() as db:
        old = db.execute('SELECT data FROM channels WHERE id=?', (channel_id,)).fetchone()
        channel = json.loads(old[0]) if old else {
            'id': channel_id, 'initials': ''.join(word[0] for word in name.split()[:2]),
            'color': '#9ee6cf', 'art': 'landscape',
        }
        channel.update(id=channel_id, name=name, playlist='UU' + channel_id[2:], coverage=channel.get('coverage') or 'Current week queued')
        if avatar:
            channel['avatar'] = avatar
        if item.get('description') and not channel.get('description'):
            channel['description'] = str(item.get('description'))[:500]
        db.execute('INSERT OR REPLACE INTO channels VALUES(?,?,1)', (channel_id, json.dumps(channel)))
        db.execute('INSERT OR IGNORE INTO youtube_manual_channels VALUES(?)', (channel_id,))
        db.execute('DELETE FROM removed_channels WHERE id=?', (channel_id,))
        db.execute('''INSERT OR IGNORE INTO download_rules(channel,quality,videos,shorts,live,range_days,enabled_at,quality_override,range_override)
            VALUES(?,?,?,0,0,?,?,0,0)''',
                   (channel_id, store.get('download_quality', '1080'), int(options['new_channel_videos']), options['default_range'], time.time()))
    store.set('next_sync', '0')
    _queue(store, channel_id, 'subscribe')
    return channel_id


def unsubscribe_local(store, channel_id):
    if not CHANNEL.fullmatch(channel_id or ''):
        raise ValueError('Choose a channel to unsubscribe.')
    initialise(store)
    with store.connect() as db:
        db.execute('INSERT OR IGNORE INTO removed_channels VALUES(?)', (channel_id,))
        db.execute('DELETE FROM youtube_manual_channels WHERE id=?', (channel_id,))
        db.execute('UPDATE channels SET active=0 WHERE id=?', (channel_id,))
        db.execute('UPDATE download_rules SET videos=0, shorts=0, live=0 WHERE channel=?', (channel_id,))
    _queue(store, channel_id, 'unsubscribe')
    return channel_id


def adopt_follows(store):
    """Move the old Discover follow list into ReelDock subscriptions."""
    from .discover_store import follows
    for item in follows(store):
        channel_id = channel_of(item)
        if not channel_id:
            continue
        subscribe_local(store, item)
        with store.connect() as db:
            db.execute('DELETE FROM discover_follows WHERE id=?', (item['id'],))


def _google(app, store, channel, intent):
    from .catalogue import Importer
    importer = Importer(app)
    listed = importer.google_api('subscriptions', part='id', mine='true', forChannelId=channel, maxResults=50)
    existing = [item.get('id') for item in listed.get('items') or [] if item.get('id')]
    if intent == 'subscribe':
        if existing:
            return
        importer.google_api(
            'subscriptions', _method='POST', _cost=50, part='snippet',
            _body=json.dumps({'snippet': {'resourceId': {'kind': 'youtube#channel', 'channelId': channel}}}),
        )
        return
    for subscription in existing:
        importer.google_api('subscriptions', _method='DELETE', _cost=50, id=subscription)


def _cookie(store, channel, intent):
    from .watched_status import cookie_session_file_status
    path = store.directory / 'youtube-cookies.txt'
    state, message = cookie_session_file_status(path)
    if state != 'unchecked':
        raise ValueError(message)
    jar = http.cookiejar.MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    sapisid = next((cookie.value for cookie in jar if cookie.name in ('SAPISID', '__Secure-3PAPISID') and cookie.value), '')
    if not sapisid:
        raise ValueError('Signed-in YouTube cookies are missing. Upload a fresh cookie file in Settings.')
    stamp = str(int(time.time()))
    digest = hashlib.sha1(f'{stamp} {sapisid} https://www.youtube.com'.encode()).hexdigest()
    action = 'subscribe' if intent == 'subscribe' else 'unsubscribe'
    try:
        with requests.Session() as session:
            session.cookies.update(jar)
            response = session.post(
                'https://www.youtube.com/youtubei/v1/subscription/' + action,
                params={'prettyPrint': 'false'},
                json={
                    'context': {'client': {'hl': 'en', 'gl': 'GB', 'clientName': 'WEB', 'clientVersion': '2.20251001.01.00'}},
                    'channelIds': [channel],
                },
                headers={
                    'Authorization': f'SAPISIDHASH {stamp}_{digest}',
                    'Origin': 'https://www.youtube.com',
                    'X-Origin': 'https://www.youtube.com',
                    'Referer': 'https://www.youtube.com/channel/' + channel,
                    'Content-Type': 'application/json',
                },
                timeout=(5, 20),
            )
    except requests.RequestException as exc:
        raise ValueError('YouTube could not be reached with the saved cookies.') from exc
    if response.status_code not in (200, 204):
        raise ValueError('YouTube did not accept the subscription change from the saved cookies.')


def push(app, store, channel, intent):
    """Send one queued change. Google is used when it can manage subscriptions."""
    if google_ready(store):
        _google(app, store, channel, intent)
        return 'synced', 'YouTube updated through Google.'
    if cookies_ready(store):
        _cookie(store, channel, intent)
        return 'synced', 'YouTube updated through the saved cookies.'
    return 'waiting', ''


def apply(app, store, channel, intent):
    status, detail = push(app, store, channel, intent)
    if status == 'waiting':
        if intent == 'subscribe':
            message = 'Subscribed in ReelDock. YouTube will be updated when Google or cookies are connected.'
        else:
            message = 'Removed from ReelDock. YouTube will be updated when Google or cookies are connected. Saved files were kept.'
        _finish(store, channel, 'waiting', message)
        return {'youtube': 'waiting', 'message': message}
    if intent == 'subscribe':
        message = 'Subscribed in ReelDock and on YouTube.'
    else:
        message = 'Removed from ReelDock and unsubscribed on YouTube. Saved files were kept.'
    _finish(store, channel, 'synced', message)
    return {'youtube': 'synced', 'message': message}


def subscribe(app, store, item):
    adopt_follows(store)
    channel = subscribe_local(store, item)
    try:
        remote = apply(app, store, channel, 'subscribe')
    except ValueError:
        message = 'Subscribed in ReelDock. YouTube subscribe did not finish and will retry.'
        _finish(store, channel, 'pending', message)
        remote = {'youtube': 'pending', 'message': message}
    return {'subscribed': True, 'channelId': channel, 'subscriptions': active_ids(store), **remote}


def unsubscribe(app, store, item):
    channel = channel_of(item)
    unsubscribe_local(store, channel)
    try:
        remote = apply(app, store, channel, 'unsubscribe')
    except ValueError:
        message = 'Removed from ReelDock. YouTube unsubscribe did not finish and will retry. Saved files were kept.'
        _finish(store, channel, 'pending', message)
        remote = {'youtube': 'pending', 'message': message}
    return {'subscribed': False, 'channelId': channel, 'subscriptions': active_ids(store), **remote}


def toggle(app, store, item):
    channel = channel_of(item)
    if not channel:
        raise ValueError('This channel has no YouTube channel ID, so it cannot be subscribed.')
    initialise(store)
    with store.connect() as db:
        active = db.execute('SELECT 1 FROM channels WHERE id=? AND active=1', (channel,)).fetchone()
    if active:
        return unsubscribe(app, store, item)
    return subscribe(app, store, item)


def flush(app, store, limit=3):
    """Send subscription changes that were saved before Google or cookies existed."""
    if not store.get('setup_complete'):
        return 0
    import sqlite3
    waiting = followed = 0
    with store.connect() as db:
        try:
            waiting = db.execute("SELECT count(*) FROM youtube_account_subscriptions WHERE status IN ('waiting','pending')").fetchone()[0]
        except sqlite3.OperationalError:
            waiting = 0
        try:
            followed = db.execute('SELECT count(*) FROM discover_follows').fetchone()[0]
        except sqlite3.OperationalError:
            followed = 0
    if not waiting and not followed:
        return 0
    initialise(store)
    if followed:
        adopt_follows(store)
    if not google_ready(store) and not cookies_ready(store):
        return 0
    cutoff = time.time() - RETRY_AFTER
    with store.connect() as db:
        rows = db.execute(
            """SELECT channel, intent FROM youtube_account_subscriptions
               WHERE status='waiting' OR (status='pending' AND updated<?)
               ORDER BY updated LIMIT ?""",
            (cutoff, max(1, min(int(limit), 10)))).fetchall()
    sent = 0
    for row in rows:
        try:
            apply(app, store, row['channel'], row['intent'])
            sent += 1
        except ValueError as exc:
            _finish(store, row['channel'], 'pending', str(exc) or 'YouTube subscribe will retry.')
    return sent
