"""Signed YouTube WebSub notifications with persistent leases and retryable work."""
import hashlib
import hmac
import http.client
import logging
import re
import secrets
import time
from urllib.parse import urlencode
from xml.etree import ElementTree

CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}")
VIDEO = re.compile(r"[A-Za-z0-9_-]{11}")
ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
LIMIT = 256 * 1024


def initialise(store):
    with store.connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS youtube_push_subscriptions(
            channel TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, secret TEXT NOT NULL,
            origin TEXT NOT NULL, expires REAL NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL DEFAULT 0, intent TEXT NOT NULL DEFAULT 'subscribe');
        CREATE TABLE IF NOT EXISTS youtube_push_events(
            id TEXT PRIMARY KEY, channel TEXT NOT NULL, digest TEXT NOT NULL,
            received REAL NOT NULL, retry_at REAL NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0);
        """)


def topic(channel):
    return 'https://www.youtube.com/feeds/videos.xml?channel_id=' + channel


def available(store):
    return bool(store.get('setup_complete') and store.get('public_origin').startswith('https://')
)


def register(app):
    from flask import request, Response
    store = app.extensions['store']
    initialise(store)

    @app.route('/hooks/youtube/<token>', methods=['GET', 'POST'])
    def youtube_notification(token):
        with store.connect() as db:
            row = db.execute('SELECT * FROM youtube_push_subscriptions WHERE token=?', (token,)).fetchone()
            active = row and db.execute('SELECT 1 FROM channels WHERE id=? AND active=1', (row['channel'],)).fetchone()
        if not row or not available(store) or row['origin'] != store.get('public_origin'):
            return Response(status=404)
        if request.method == 'GET':
            mode = request.args.get('hub.mode')
            challenge = request.args.get('hub.challenge', '')
            if (mode != row['intent'] or mode not in ('subscribe', 'unsubscribe')
                    or request.args.get('hub.topic') != topic(row['channel'])
                    or not challenge or len(challenge) > 4096
                    or (mode == 'subscribe' and not active)):
                return Response(status=404)
            try:
                lease = int(request.args.get('hub.lease_seconds', '0'))
            except ValueError:
                return Response(status=400)
            if mode == 'subscribe' and not 0 < lease <= 31 * 86400:
                return Response(status=400)
            with store.connect() as db:
                if mode == 'unsubscribe':
                    db.execute('DELETE FROM youtube_push_subscriptions WHERE token=?', (token,))
                else:
                    db.execute('UPDATE youtube_push_subscriptions SET expires=?,retry_at=? WHERE token=?',
                               (time.time()+lease, time.time()+max(30, lease*0.8), token))
            return Response(challenge, content_type='text/plain; charset=utf-8')
        if not active or row['intent'] != 'subscribe':
            return Response(status=404)
        request.max_content_length = LIMIT
        body = request.get_data()
        signature = request.headers.get('X-Hub-Signature', '')
        algorithm, _, supplied = signature.partition('=')
        if algorithm not in ('sha1', 'sha256', 'sha384', 'sha512'):
            return Response(status=403)
        expected = hmac.new(row['secret'].encode(), body, algorithm).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            return Response(status=403)
        try:
            xml = body.decode('utf-8')
        except UnicodeDecodeError:
            return Response(status=400)
        if '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper() or '\x00' in xml:
            return Response(status=400)
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError:
            return Response(status=400)
        if root.tag != ATOM+'feed':
            return Response(status=400)
        events = []; basics=[]
        for entry in root.findall(ATOM+'entry'):
            vid = entry.findtext(YT+'videoId', '')
            channel = entry.findtext(YT+'channelId', '')
            if channel != row['channel'] or not VIDEO.fullmatch(vid):
                return Response(status=400)
            digest = hashlib.sha256(ElementTree.tostring(entry)).hexdigest()
            events.append((vid, channel, digest, time.time()))
            basics.append(dict(id=vid,_pending=True,snippet=dict(channelId=channel,title=entry.findtext(ATOM+'title') or vid,
                publishedAt=entry.findtext(ATOM+'published') or '',thumbnails={'medium':{'url':'https://i.ytimg.com/vi/'+vid+'/hqdefault.jpg'}})))
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM youtube_push_events WHERE done=0').fetchone()[0]+len(events)>10000:
                return Response(status=503)
            db.executemany("""INSERT INTO youtube_push_events(id,channel,digest,received) VALUES (?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET channel=excluded.channel,digest=excluded.digest,
                received=excluded.received,retry_at=0,attempts=0,done=0
                WHERE youtube_push_events.digest!=excluded.digest""", events)
        from .youtube_fallback import placeholder
        from .catalogue import Importer
        placeholder(Importer(app),basics)
        store.set('youtube_push_received', time.time())
        return Response(status=204)


def send_subscription(fields):
    connection = http.client.HTTPSConnection('pubsubhubbub.appspot.com', timeout=15)
    try:
        connection.request('POST', '/subscribe', urlencode(fields),
                           {'Content-Type': 'application/x-www-form-urlencoded'})
        response = connection.getresponse()
        if response.status not in (202, 204):
            raise ValueError('YouTube notification subscription could not be renewed.')
    finally:
        connection.close()


def renew(store, sender=send_subscription):
    if not available(store):
        return
    origin = store.get('public_origin')
    now = time.time()
    with store.connect() as db:
        channels = {r[0] for r in db.execute('SELECT id FROM channels WHERE active=1') if CHANNEL.fullmatch(r[0])}
        known = {r[0] for r in db.execute('SELECT channel FROM youtube_push_subscriptions')}
        for channel in channels-known:
            db.execute('INSERT OR IGNORE INTO youtube_push_subscriptions(channel,token,secret,origin) VALUES (?,?,?,?)',
                       (channel, secrets.token_urlsafe(32), secrets.token_urlsafe(32), origin))
        for row in db.execute('SELECT channel FROM youtube_push_subscriptions WHERE origin!=?', (origin,)).fetchall():
            db.execute('UPDATE youtube_push_subscriptions SET token=?,secret=?,origin=?,expires=0,retry_at=0 WHERE channel=?',
                       (secrets.token_urlsafe(32), secrets.token_urlsafe(32), origin, row['channel']))
        db.execute("UPDATE youtube_push_subscriptions SET intent='unsubscribe',retry_at=0 WHERE intent='subscribe' AND channel NOT IN (SELECT id FROM channels WHERE active=1)")
        db.execute("UPDATE youtube_push_subscriptions SET intent='subscribe',retry_at=0 WHERE intent='unsubscribe' AND channel IN (SELECT id FROM channels WHERE active=1)")
        rows = db.execute('SELECT * FROM youtube_push_subscriptions WHERE retry_at<=? ORDER BY retry_at,channel LIMIT 5', (now,)).fetchall()
        for row in rows:
            db.execute('UPDATE youtube_push_subscriptions SET retry_at=? WHERE channel=?', (now+900, row['channel']))
        db.execute('DELETE FROM youtube_push_events WHERE channel NOT IN (SELECT id FROM channels WHERE active=1) OR (done=1 AND received<?)', (now-7*86400,))
    for row in rows:
        try:
            sender({'hub.callback': origin+'/hooks/youtube/'+row['token'], 'hub.topic': topic(row['channel']),
                    'hub.mode': row['intent'], 'hub.verify': 'async', 'hub.secret': row['secret'],
                    'hub.lease_seconds': '864000'})
        except Exception:
            store.set('youtube_push_status', 'Notification connection will retry; scheduled checks remain active.')
    with store.connect() as db:
        count = db.execute("SELECT count(*) FROM youtube_push_subscriptions WHERE expires>? AND intent='subscribe'", (now,)).fetchone()[0]
    store.set('youtube_push_connected', count)


def process(importer):
    store = importer.store
    if not store.get('setup_complete') or store.get('youtube_sync_paused') == 'true':
        return
    from .youtube_source import fallback
    batch_limit=3 if fallback(store) else 50
    with store.connect() as db:
        rows = db.execute('''SELECT e.* FROM youtube_push_events e JOIN channels c ON c.id=e.channel
            WHERE c.active=1 AND e.done=0 AND e.retry_at<=? ORDER BY e.received LIMIT ?''', (time.time(),batch_limit)).fetchall()
        priority = db.execute('''SELECT e.* FROM youtube_push_events e JOIN channels c ON c.id=e.channel
            JOIN channel_refresh_requests r ON r.channel=e.channel
            WHERE c.active=1 AND e.done=0 AND e.retry_at<=? AND r.requested>0 AND r.expires>?
            ORDER BY r.requested DESC,e.received,e.id LIMIT 1''',(time.time(),time.time())).fetchone()
        if priority:
            # Reserve only one slot; the remaining slots keep global FIFO and
            # every existing retry deadline. Viewing cannot starve bulk work.
            rows = [priority, *[row for row in rows if row['id'] != priority['id']]][:batch_limit]
    if not rows:
        return
    store.set('youtube_push_running',str(time.time()))
    try:
        result = importer.api('videos', part='snippet,contentDetails,liveStreamingDetails,statistics',
                              id=','.join(r['id'] for r in rows), maxResults=50, _fallback_limit=3)
        # Quota can be reached during this request: only the fallback's three
        # attempted IDs should enter retry backoff; leave the rest ready.
        if fallback(store):rows=rows[:3]
        wanted = {r['id']:r['channel'] for r in rows}
        items = [i for i in result.get('items', []) if i.get('id') in wanted
                 and i.get('snippet', {}).get('channelId') == wanted[i['id']]]
        with store.connect() as db:
            active = {r[0] for r in db.execute('SELECT id FROM channels WHERE active=1')}
        items = [i for i in items if wanted[i['id']] in active]
        importer.save_videos(items,preserve_browse_only=True)
        saved = {i['id'] for i in items}
        with store.connect() as db:
            for row in rows:
                if row['id'] in saved:
                    db.execute('UPDATE youtube_push_events SET done=1 WHERE id=? AND digest=?', (row['id'],row['digest']))
                else:
                    db.execute('UPDATE youtube_push_events SET retry_at=?,attempts=attempts+1 WHERE id=? AND digest=?',
                               (time.time()+min(3600,60*2**min(row['attempts'],6)),row['id'],row['digest']))
        store.set('next_auto_check', '0')
        store.set('youtube_push_status', 'Video details checked; eligible downloads will be queued.' if len(saved)==len(rows) else 'Some video details are unavailable. Retrying with backoff; public access may require sign-in.')
    except Exception:
        with store.connect() as db:
            for row in rows:
                db.execute('UPDATE youtube_push_events SET retry_at=?,attempts=attempts+1 WHERE id=? AND digest=?',
                           (time.time()+min(3600,60*2**min(row['attempts'],6)),row['id'],row['digest']))
        store.set('youtube_push_status', 'An upload notification is waiting for video details. Retrying automatically.')
    finally:
        store.set('youtube_push_running','0')


def loop(app, stop):
    from .catalogue import Importer
    importer = Importer(app)
    while not stop.is_set():
        paused = True
        with importer.store.background_operation():
            if importer.store.reset_pending():
                paused = True
            else:
                paused = False
                try:
                    process(importer)
                    renew(importer.store)
                except Exception:
                    logging.warning('YouTube notification cycle failed; retrying shortly')
        if paused:
            stop.wait(1)
            continue
        stop.wait(5)
