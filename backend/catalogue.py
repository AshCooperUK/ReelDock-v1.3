"""Persistent, bounded YouTube catalogue imports. No video downloads."""
import hashlib
import json
import re
import time
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo


def initialise(store):
    from .subscription_returns import initialise as initialise_returns
    initialise_returns(store)
    with store.connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS youtube_api_usage(day TEXT,client TEXT,resource TEXT,requests INTEGER DEFAULT 0,units INTEGER DEFAULT 0,PRIMARY KEY(day,client,resource));
        CREATE TABLE IF NOT EXISTS removed_channels(id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS history_jobs(channel TEXT PRIMARY KEY, playlist TEXT NOT NULL, cursor TEXT NOT NULL,
            state TEXT NOT NULL, updated REAL NOT NULL, failures INTEGER NOT NULL, error TEXT NOT NULL, trail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS channel_forecast_preferences(id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS channel_preferences(id TEXT PRIMARY KEY, shown INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS forecasts(id TEXT PRIMARY KEY, channel TEXT NOT NULL, day TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS channels(id TEXT PRIMARY KEY, data TEXT NOT NULL, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS videos(id TEXT PRIMARY KEY, channel TEXT NOT NULL, day TEXT NOT NULL, published TEXT NOT NULL, data TEXT NOT NULL, checked REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS videos_day ON videos(day);
        CREATE INDEX IF NOT EXISTS videos_channel_date ON videos(channel,published DESC,id);
        CREATE TABLE IF NOT EXISTS artwork_requests(id TEXT PRIMARY KEY, requested REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS artwork(id TEXT PRIMARY KEY, url TEXT NOT NULL, checked REAL DEFAULT 0);
        ''')

    from .guide_weeks import initialise as initialise_weeks
    initialise_weeks(store)
    from .youtube_fallback import initialise as fallback_initialise
    fallback_initialise(store)
    from .channel_refresh import initialise as refresh_initialise
    refresh_initialise(store)
    from .channel_upload_browse import initialise as browse_initialise
    browse_initialise(store)
    from .channel_metadata_refresh import initialise as metadata_initialise
    metadata_initialise(store)
    from .artwork import repair_relative_urls
    repair_relative_urls(store)

def thumbnail(store, snippet, *, transaction=None):
    choices = snippet.get('thumbnails', {})
    url = next((choices[k]['url'] for k in ('medium', 'high', 'default') if choices.get(k, {}).get('url')), '')
    if not url:
        return ''
    if url.startswith('//'):
        # yt-dlp's cookie/feed listings give protocol-relative avatar links.
        url = 'https:' + url
    key = hashlib.sha256(url.encode()).hexdigest()
    if transaction is not None:
        transaction.execute('INSERT OR IGNORE INTO artwork(id,url) VALUES (?,?)',(key,url))
    else:
        with store.connect() as db:
            db.execute('INSERT OR IGNORE INTO artwork(id,url) VALUES (?,?)', (key, url))
    return '/artwork/' + key


class Importer:
    def __init__(self, app):
        self.app = app
        self.store = app.extensions['store']
        self.request = app.config['GOOGLE_REQUEST']
        initialise(self.store)

    def progress(self, message):
        self.store.set('sync_message', message)
        self.store.set('worker_heartbeat', time.time())

    def token(self):
        from .youtube_source import enabled
        if not enabled(self.store):raise ValueError('Google API is switched off.')
        tokens = json.loads(self.store.get('google_tokens') or '{}')
        if tokens.get('access_token') and tokens.get('received_at', 0) + tokens.get('expires_in', 0) > time.time() + 120:
            return tokens['access_token']
        if not tokens.get('refresh_token'):
            raise ValueError('Google authorisation is missing. Reconnect Google in setup.')
        refreshed = self.request('oauth2.googleapis.com', '/token', body=urlencode({
            'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token'],
            'client_id': self.app.config['GOOGLE_CLIENT_ID'] or self.store.get('google_client_id'),
            'client_secret': self.app.config['GOOGLE_CLIENT_SECRET'] or self.store.get('google_client_secret')}))
        if not refreshed.get('access_token'):
            raise ValueError('Google access could not be renewed. Reconnect Google.')
        tokens.update(refreshed)
        tokens['received_at'] = time.time()
        self.store.set('google_tokens', json.dumps(tokens))
        return tokens['access_token']

    def api(self, resource, _method=None, _cost=1, _fallback_limit=None, **params):
        from .youtube_source import fallback
        from .youtube_fallback import api as fallback_api
        from .sync_controls import check
        check(self.store)
        use_fallback=resource == 'playlistItems' or fallback(self.store) or str(params.get('pageToken','')).startswith('fb:')
        if use_fallback:
            if resource not in ('videos','playlistItems') and float(self.store.get('google_quota_retry_at','0'))>time.time():raise ValueError(self.store.get('google_quota_error') or 'YouTube API quotaExceeded; using fallback for videos.')
            if _method:raise ValueError('This account action requires Google API. Local channel controls are still available.')
            return fallback_api(self,resource,_limit=_fallback_limit,**params)
        try:return self.google_api(resource,_method=_method,_cost=_cost,**params)
        except ValueError as exc:
            from .sync_controls import SyncPaused
            if isinstance(exc,SyncPaused):raise
            if not _method and resource == 'videos':
                if not any(code in str(exc) for code in ('quotaExceeded','dailyLimitExceeded')):
                    self.store.set('youtube_api_unavailable_until', time.time()+300)
                return fallback_api(self,resource,_limit=_fallback_limit,**params)
            raise

    def google_api(self, resource, _method=None, _cost=1, _body=None, _content_type=None, **params):
        from .sync_controls import check
        check(self.store)
        if float(self.store.get('google_quota_retry_at', '0')) > time.time():
            raise ValueError(self.store.get('google_quota_error') or 'YouTube API quotaExceeded; retry pending.')
        token = self.token()
        # Count requests for information only; Google enforces the quota.
        day = datetime.now(ZoneInfo('America/Los_Angeles')).date().isoformat()
        from .quota import bind, client_id
        client = client_id(self.app,self.store)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            bind(db,client)
            db.execute('''INSERT INTO youtube_api_usage VALUES(?,?,?,1,?)
                ON CONFLICT(day,client,resource) DO UPDATE SET requests=requests+1,units=units+excluded.units''',
                (day,hashlib.sha256(client.encode()).hexdigest(),resource,_cost))
            db.execute("DELETE FROM youtube_api_usage WHERE day<date(?,'-35 days')",(day,))
            row = db.execute("SELECT value FROM settings WHERE key='quota_day'").fetchone()
            used = db.execute("SELECT value FROM settings WHERE key='quota_used'").fetchone()
            count = int(used[0]) if row and row[0] == day and used else 0
            for key, value in (('quota_day', day), ('quota_used', str(count + _cost))):
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, value))
        self.progress(self.store.get('sync_message'))
        try:
            options = {}
            if _method:
                options['method'] = _method
            if _body is not None:
                options['body'] = _body
                options['method'] = _method or 'POST'
                options['content_type'] = _content_type or 'application/json'
            return self.request('www.googleapis.com', '/youtube/v3/' + resource + '?' + urlencode(params), token=token, **options)
        except ValueError as exc:
            if any(code in str(exc) for code in ('quotaExceeded', 'dailyLimitExceeded')):
                from .quota import next_reset
                self.store.set('google_quota_retry_at', next_reset())
                self.store.set('google_quota_error', str(exc))
            raise

    def stage(self, name, done=0, total=None):
        self.store.set('sync_progress', json.dumps(dict(stage=name, done=done, total=total)))

    def run(self):
        from .youtube_source import fallback
        from .youtube_fallback import run
        now = time.time()
        due = float(self.store.get('youtube_subscriptions_next', '0')) <= now
        if not fallback(self.store) and due:
            try:
                self.google_run()
                self.store.set('youtube_subscriptions_error', '')
                self.store.set('youtube_subscriptions_next', time.time()+6*3600)
            except ValueError as exc:
                from .sync_controls import SyncPaused
                if isinstance(exc, SyncPaused): raise
                self.store.set('youtube_subscriptions_error', str(exc))
                self.store.set('youtube_subscriptions_next', time.time()+1800)
        if fallback(self.store) or self.store.get('youtube_subscriptions_error'):
            from .cookie_subscriptions import sync
            sync(self)
        return run(self)

    def google_run(self):
        """Refresh account membership; routine uploads always use public feeds."""
        self.stage('subscriptions')
        self.store.set('sync_error', '')
        self.progress('Reading your subscriptions...')
        ids, page, seen = [], '', set()
        subscription_items=[]
        while True:
            result = self.api('subscriptions', part='snippet', mine='true', maxResults=50, pageToken=page)
            subscription_items.extend(result.get('items', []))
            ids.extend(i['snippet']['resourceId']['channelId'] for i in result.get('items', []))
            self.stage('subscriptions', len(set(ids)))
            page = result.get('nextPageToken', '')
            if not page:
                break
            if page in seen:
                raise ValueError('YouTube repeated a subscription page. Try refreshing again.')
            seen.add(page)
        from .subscription_returns import observe
        observe(self.store,subscription_items)
        with self.store.connect() as db:
            removed={r[0] for r in db.execute('SELECT id FROM removed_channels')}
        with self.store.connect() as db:
            ids.extend(r[0] for r in db.execute('SELECT id FROM youtube_manual_channels'))
        ids = [i for i in dict.fromkeys(ids) if i not in removed]
        from .download_options import read as download_options
        defaults=download_options(self.store)
        default_quality=self.store.get('download_quality','1080')
        with self.store.connect() as db:
            cached = {r['id']: json.loads(r['data']) for r in db.execute('SELECT id,data FROM channels')}
        detail_ids = [identity for identity in ids if identity not in cached
                      or float(cached[identity].get('metadata_checked') or 0) < time.time()-7*86400]
        self.stage('channels', 0, len(ids))
        for offset in range(0, len(detail_ids), 50):
            self.stage('channels', offset, len(ids))
            result = self.api('channels', part='snippet,contentDetails,statistics,brandingSettings,topicDetails', id=','.join(detail_ids[offset:offset+50]), maxResults=50)
            for item in result.get('items', []):
                sn = item['snippet']
                channel = dict(id=item['id'], name=sn['title'], description=sn.get('description', ''),
                               initials=''.join(w[0] for w in sn['title'].split()[:2]), color='#9ee6cf',
                               art='landscape', avatar=thumbnail(self.store, sn), coverage='Recent uploads pending',
                               playlist=item.get('contentDetails', {}).get('relatedPlaylists', {}).get('uploads', ''))
                stats = item.get('statistics', {})
                branding = item.get('brandingSettings', {})
                banner = branding.get('image', {}).get('bannerExternalUrl', '')
                channel.update(metadata_checked=time.time(), handle=sn.get('customUrl', ''), created=sn.get('publishedAt', ''),
                               country=sn.get('country', ''), language=sn.get('defaultLanguage', ''),
                               subscribers=None if stats.get('hiddenSubscriberCount') else stats.get('subscriberCount'),
                               total_views=stats.get('viewCount'), video_count=stats.get('videoCount'),
                               topics=item.get('topicDetails', {}).get('topicCategories', []),
                               banner=thumbnail(self.store, {'thumbnails': {'medium': {'url': banner}}}) if banner else '')
                with self.store.connect() as db:
                    previous = db.execute('SELECT data FROM channels WHERE id=?', (item['id'],)).fetchone()
                    if previous:
                        saved = json.loads(previous[0])
                        channel['last_checked'] = saved.get('last_checked')
                        channel['coverage'] = saved.get('coverage', channel['coverage'])
                    db.execute('INSERT OR REPLACE INTO channels VALUES (?,?,1)', (item['id'], json.dumps(channel)))
                    if not previous:
                        # Seed only newly discovered channels; never reset a user's rules.
                        db.execute('''INSERT OR IGNORE INTO download_rules
                            (channel,quality,videos,shorts,live,range_days,enabled_at,quality_override,range_override)
                            VALUES (?,?,?,0,0,?,?,0,0)''',
                            (item['id'],default_quality,int(defaults['new_channel_videos']),defaults['default_range'],time.time()))
        # Only reconcile subscriptions after the complete subscription list succeeds.
        with self.store.connect() as db:
            db.execute('UPDATE channels SET active=0')
            db.executemany('UPDATE channels SET active=1 WHERE id=?', [(i,) for i in ids])
            db.execute('DELETE FROM videos WHERE channel IN (SELECT id FROM channels WHERE active=0)')
            db.execute('DELETE FROM history_jobs WHERE channel IN (SELECT id FROM channels WHERE active=0)')
            db.execute('DELETE FROM channels WHERE active=0 AND id NOT IN (SELECT id FROM removed_channels)')
            rows = db.execute('SELECT data FROM channels WHERE active=1').fetchall()
        self.progress(f'Subscriptions checked: {len(rows)} channels. Checking public upload feeds next.')

    def prune_artwork(self):
        with self.store.connect() as db:
            used = set()
            for row in db.execute('SELECT data FROM channels UNION ALL SELECT data FROM videos'):
                item = json.loads(row[0])
                for field in ('avatar', 'thumbnail', 'banner'):
                    url = item.get(field)
                    if url:
                        used.add(url.rsplit('/', 1)[-1])
            stale = [r[0] for r in db.execute('SELECT id FROM artwork') if r[0] not in used]
            db.executemany('DELETE FROM artwork WHERE id=?', [(key,) for key in stale])
            db.execute('DELETE FROM artwork_requests WHERE id NOT IN (SELECT id FROM artwork)')
        for key in stale:
            (self.store.directory / 'artwork' / key).unlink(missing_ok=True)

    def save_videos(self, items, *, preserve_browse_only=False, guard=None):
        for item in items:
            sn = item['snippet']
            if not re.fullmatch(r'[A-Za-z0-9_-]{11}', item['id']):
                continue
            state = {'upcoming': 'scheduled', 'live': 'live'}.get(sn.get('liveBroadcastContent'), 'published')
            published = item.get('liveStreamingDetails', {}).get('scheduledStartTime') if state == 'scheduled' else None
            published = published or sn.get('publishedAt')
            try:
                stamp = datetime.fromisoformat(published.replace('Z', '+00:00')).astimezone(ZoneInfo('Europe/London'))
            except (ValueError, AttributeError):
                continue
            duration = item.get('contentDetails', {}).get('duration', '')
            parts = re.fullmatch(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration)
            duration = ''
            if parts:
                h, m, sec = [int(x or 0) for x in parts.groups()]
                duration = f'{h}:{m:02}:{sec:02}' if h else f'{m}:{sec:02}'
            video = dict(metadata_pending=bool(item.get('_pending')),id=item['id'], channel=sn['channelId'], date=stamp.date().isoformat(), time=stamp.strftime('%H:%M'),
                         forecast_eligible=state == 'published' and not item.get('liveStreamingDetails') and not item.get('_pending'),
                         views=item.get('statistics', {}).get('viewCount'), title=sn['title'], description=sn.get('description', ''), state=state, duration=duration,
                         thumbnail=thumbnail(self.store, sn) if guard is None else '')
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if guard is not None and not guard(db):continue
                if guard is not None:video['thumbnail']=thumbnail(self.store,sn,transaction=db)
                prior=db.execute('SELECT data FROM videos WHERE id=?',(item['id'],)).fetchone()
                previous=json.loads(prior[0]) if prior else {}
                if item.get('_pending'):
                    # A concurrent rich-metadata result or channel removal wins
                    # over a stale RSS/notification placeholder.
                    if prior or not db.execute('SELECT 1 FROM channels WHERE id=? AND active=1',(sn['channelId'],)).fetchone():
                        continue
                    if db.execute("SELECT 1 FROM settings WHERE key IN ('youtube_sync_paused','reset_in_progress') AND value IN ('true','1') LIMIT 1").fetchone():
                        continue
                video['kind']='live' if item.get('liveStreamingDetails') else (json.loads(prior[0]).get('kind','unknown') if prior else 'unknown')
                if preserve_browse_only and previous.get('browse_only'):
                    video['browse_only']=True
                    video['forecast_eligible']=False
                db.execute('INSERT OR REPLACE INTO videos VALUES (?,?,?,?,?,?)',
                           (item['id'], sn['channelId'], video['date'], stamp.isoformat(), json.dumps(video), time.time()))
