"""Account-scoped Discover preferences, lists, rules and requests.

ReelDock has one administrator database. These rows live in that database and
are only available through authenticated routes, so they follow the same
account boundary as the rest of the library.
"""
import json
import math
import re
import time
import uuid
from contextlib import nullcontext

from .discover_logic import QUALITIES
from .media_library import safe_name

IMAGE_HOSTS = (
    'https://image.tmdb.org/t/p/',
    'https://api.nzbgeek.info/covers/',
    'https://api.nzb.life/covers/',
    'https://i.ytimg.com/vi/',
    'https://yt3.ggpht.com/',
    'https://yt3.googleusercontent.com/',
    'https://static.tvmaze.com/uploads/images/',
)
LAYOUTS = ('grid', 'shelves', 'list')
DENSITIES = ('comfortable', 'compact')
SOURCES = ('NZB first', 'Torrent first', 'YouTube')
KINDS = ('movie', 'show', 'video', 'channel')


def initialise(store):
    with store.connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS discover_preferences (
            id INTEGER PRIMARY KEY CHECK(id=1),
            layout TEXT NOT NULL DEFAULT 'grid',
            density TEXT NOT NULL DEFAULT 'comfortable',
            quality TEXT NOT NULL DEFAULT '1080',
            fallback INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'NZB first');
        CREATE TABLE IF NOT EXISTS discover_watchlist (
            id TEXT PRIMARY KEY, body TEXT NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS discover_follows (
            id TEXT PRIMARY KEY, body TEXT NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS discover_rules (
            id TEXT PRIMARY KEY, show_id TEXT NOT NULL, scope TEXT NOT NULL, season INTEGER NOT NULL,
            quality TEXT NOT NULL, fallback INTEGER NOT NULL, enabled INTEGER NOT NULL,
            boundary_locked INTEGER NOT NULL DEFAULT 1, last_check REAL NOT NULL DEFAULT 0,
            next_check REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
            lease_until REAL NOT NULL DEFAULT 0, body TEXT NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS discover_requests (
            id TEXT PRIMARY KEY, idempotency TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, title TEXT NOT NULL,
            state TEXT NOT NULL, quality TEXT NOT NULL, fallback INTEGER NOT NULL, body TEXT NOT NULL,
            engine TEXT NOT NULL DEFAULT '', engine_id TEXT NOT NULL DEFAULT '', manual INTEGER NOT NULL DEFAULT 1,
            rule_id TEXT NOT NULL DEFAULT '', episode_key TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
            progress REAL NOT NULL DEFAULT 0, updated REAL NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS discover_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
            source TEXT NOT NULL, quality TEXT NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS discover_candidates (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL, external_id TEXT NOT NULL,
            quality TEXT NOT NULL, size INTEGER NOT NULL, title TEXT NOT NULL, grab TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS discover_owned (
            key TEXT PRIMARY KEY, request_id TEXT NOT NULL, path TEXT NOT NULL DEFAULT '', state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS discover_charts (
            name TEXT PRIMARY KEY, body TEXT NOT NULL, fetched REAL NOT NULL, error TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS discover_feeds (
            name TEXT PRIMARY KEY, body TEXT NOT NULL, fetched REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS discover_show_lookup (
            name TEXT PRIMARY KEY, tvdb TEXT NOT NULL, checked REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS discover_show_media (
            tvdb TEXT PRIMARY KEY, body TEXT NOT NULL, checked REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS discover_quality_policy (
            id TEXT PRIMARY KEY, allow_lower INTEGER NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS discover_episode_open
            ON discover_requests(episode_key)
            WHERE episode_key != '' AND state NOT IN ('failed', 'cancelled');
        ''')
        db.execute('INSERT OR IGNORE INTO discover_preferences(id) VALUES (1)')
        columns = {row[1] for row in db.execute('PRAGMA table_info(discover_requests)')}
        if 'stop_generation' not in columns:
            db.execute('ALTER TABLE discover_requests ADD COLUMN stop_generation INTEGER NOT NULL DEFAULT 0')
        from .discover_handoff import initialise as initialise_handoffs
        initialise_handoffs(db)


def preferences(store):
    initialise(store)
    with store.connect() as db:
        row = db.execute('SELECT layout,density,quality,fallback,source FROM discover_preferences WHERE id=1').fetchone()
    return {
        'layout': row['layout'], 'density': row['density'], 'quality': row['quality'],
        'fallback': bool(row['fallback']), 'source': row['source'],
    }


def save_preference(store, key, value):
    initialise(store)
    if key == 'layout' and value not in LAYOUTS:
        raise ValueError('Choose a layout.')
    if key == 'density' and value not in DENSITIES:
        raise ValueError('Choose a card size.')
    if key == 'quality' and value not in QUALITIES:
        raise ValueError('Choose 720p, 1080p or 2160p.')
    if key == 'fallback' and type(value) is not bool:
        raise ValueError('Choose whether a lower quality is allowed.')
    if key == 'source' and value not in SOURCES:
        raise ValueError('Choose a source order.')
    if key not in ('layout', 'density', 'quality', 'fallback', 'source'):
        raise ValueError('Unknown preference.')
    stored = int(value) if key == 'fallback' else value
    with store.connect() as db:
        db.execute(f'UPDATE discover_preferences SET {key}=? WHERE id=1', (stored,))
    return preferences(store)


def _image(value):
    text = str(value or '')
    return text if text.startswith(IMAGE_HOSTS) and len(text) < 400 and ' ' not in text else ''


def clean_item(item):
    if not isinstance(item, dict):
        raise ValueError('Choose a title.')
    kind = item.get('kind')
    identity = str(item.get('id') or '')
    if kind not in KINDS or not re.fullmatch(r'[A-Za-z0-9:_-]{1,80}', identity):
        raise ValueError('Choose a title.')
    external = str(item.get('externalId') or '')
    if external and not re.fullmatch(r'[0-9]{1,12}', external):
        external = ''
    video = str(item.get('videoId') or '')
    if video and not re.fullmatch(r'[A-Za-z0-9_-]{11}', video):
        video = ''
    channel_id = str(item.get('channelId') or '')
    if channel_id and not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', channel_id):
        channel_id = ''
    cleaned = {
        'id': identity,
        'kind': kind,
        'title': str(item.get('title') or 'Untitled')[:300],
        'image': _image(item.get('image')),
        'year': str(item.get('year') or '')[:4],
        'provider': str(item.get('provider') or '')[:40],
        'externalId': external,
        'imdbId': str(item.get('imdbId') or '') if re.fullmatch(r'tt[0-9]{1,12}', str(item.get('imdbId') or '')) else '',
        'videoId': video,
        'channelId': channel_id,
        'channel': str(item.get('channel') or '')[:180],
        'description': str(item.get('description') or '')[:800],
        'url': str(item.get('url') or '') if str(item.get('url') or '').startswith('https://www.youtube.com/') else '',
        'public': bool(item.get('public')),
        'live': bool(item.get('live')),
        'rank': item.get('rank') if isinstance(item.get('rank'), int) else None,
        'views': item.get('views') if isinstance(item.get('views'), int) else None,
        'dailyViews': item.get('dailyViews') if isinstance(item.get('dailyViews'), int) else None,
        'subscribers': item.get('subscribers') if isinstance(item.get('subscribers'), int) else None,
        'growth': item.get('growth') if isinstance(item.get('growth'), int) else None,
        'growthDays': item.get('growthDays') if isinstance(item.get('growthDays'), (int, float)) else None,
        'growthChart': bool(item.get('growthChart')),
        'genre': str(item.get('genre') or '')[:80],
        'country': str(item.get('country') or '')[:8],
        'published': str(item.get('published') or '')[:40],
        'duration': str(item.get('duration') or '')[:40],
        'releaseDate': str(item.get('releaseDate') or '')[:10] if re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(item.get('releaseDate') or '')) else '',
        'rating': str(item.get('rating') or '')[:12],
        'backdrop': _image(item.get('backdrop')),
    }
    for field, maximum in (('durationSeconds',31536000),('runtimeMinutes',525600)):
        value = item.get(field)
        if type(value) in (int,float) and math.isfinite(value) and 0<value<=maximum:
            cleaned[field] = value
    return {key: value for key, value in cleaned.items() if value not in ('', None, False) or key in ('id', 'kind', 'title', 'public', 'live')}


def _list(store, table):
    initialise(store)
    with store.connect() as db:
        rows = db.execute(f'SELECT body FROM {table} ORDER BY updated DESC LIMIT 500').fetchall()
    return [json.loads(row['body']) for row in rows]


def watchlist(store):
    return _list(store, 'discover_watchlist')


def follows(store):
    return _list(store, 'discover_follows')


def toggle_watch(store, item):
    from .watchlist import toggle
    return toggle(store,item)


def toggle_follow(store, item):
    cleaned = clean_item(item)
    if cleaned['kind'] != 'channel':
        raise ValueError('Choose a channel to follow.')
    with store.connect() as db:
        existing = db.execute('SELECT 1 FROM discover_follows WHERE id=?', (cleaned['id'],)).fetchone()
        if existing:
            db.execute('DELETE FROM discover_follows WHERE id=?', (cleaned['id'],))
            following = False
        else:
            count = db.execute('SELECT count(*) FROM discover_follows').fetchone()[0]
            if count >= 200:
                raise ValueError('Unfollow a channel before following another.')
            db.execute('INSERT INTO discover_follows(id,body,updated) VALUES (?,?,?)',
                       (cleaned['id'], json.dumps(cleaned), time.time()))
            following = True
    return {'following': following, 'items': follows(store)}


def rules(store):
    initialise(store)
    with store.connect() as db:
        rows = db.execute('SELECT * FROM discover_rules ORDER BY updated DESC').fetchall()
    result = []
    for row in rows:
        item = json.loads(row['body'])
        result.append({
            'id': row['id'], 'showId': row['show_id'], 'scope': row['scope'], 'season': row['season'],
            'quality': row['quality'], 'fallback': bool(row['fallback']), 'enabled': bool(row['enabled']),
            'boundaryLocked': bool(row['boundary_locked']), 'lastCheck': row['last_check'],
            'nextCheck': row['next_check'], 'lastError': row['last_error'], 'item': item,
            'updatedAt': row['updated'],
        })
    return result


def public_requests(store):
    initialise(store)
    with store.connect() as db:
        rows = db.execute('SELECT * FROM discover_requests ORDER BY created DESC LIMIT 100').fetchall()
        attempts = {}
        if rows:
            marks = ','.join('?' for _ in rows)
            for attempt in db.execute(
                    f'SELECT * FROM discover_attempts WHERE request_id IN ({marks}) ORDER BY id',
                    [row['id'] for row in rows]):
                attempts.setdefault(attempt['request_id'], []).append({
                    'candidateId': attempt['candidate_id'], 'source': attempt['source'],
                    'quality': attempt['quality'], 'state': attempt['state'], 'message': attempt['message'],
                })
    result = []
    for row in rows:
        body = json.loads(row['body'])
        body.pop('grab', None)
        result.append({
            'id': row['id'], 'title': row['title'], 'kind': row['kind'], 'state': row['state'],
            'quality': row['quality'], 'fallback': bool(row['fallback']), 'manual': bool(row['manual']),
            'message': row['message'], 'progress': row['progress'], 'engine': row['engine'],
            'episodeKey': row['episode_key'], 'created': row['created'], 'updated': row['updated'],
            'scope': body.get('scope') or '', 'attempts': attempts.get(row['id'], []),
            'videoId': body.get('videoId') or '',
            'itemId': body.get('itemId') or '',
            'selectedQuality': next((item['quality'] for item in reversed(attempts.get(row['id'], [])) if item['state'] == 'queued' and item['quality']), ''),
        })
    return result


def remember_candidates(store, releases):
    """Persist grab addresses server-side. Callers must already have removed them from public objects."""
    now = time.time()
    with store.connect() as db:
        for release in releases:
            grab = release.get('grab') or ''
            if not grab or not release.get('id'):
                continue
            db.execute('''INSERT INTO discover_candidates(id,source,kind,external_id,quality,size,title,grab,created)
                VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET grab=excluded.grab, quality=excluded.quality,
                size=excluded.size, title=excluded.title, created=excluded.created''',
                       (release['id'], release.get('source') or 'nzb', release.get('kind') or '',
                        release.get('externalId') or '', release.get('quality') or '', int(release.get('size') or 0),
                        str(release.get('title') or '')[:300], grab, now))


def candidate(store, candidate_id):
    with store.connect() as db:
        row = db.execute('SELECT * FROM discover_candidates WHERE id=?', (candidate_id,)).fetchone()
    return dict(row) if row else None


def owned(store, key):
    if not key:
        return None
    with store.connect() as db:
        row = db.execute('SELECT * FROM discover_owned WHERE key=?', (key,)).fetchone()
    return dict(row) if row else None


def mark_owned(store, key, request_id, path, state='complete'):
    with store.connect() as db:
        db.execute('''INSERT INTO discover_owned(key,request_id,path,state) VALUES (?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET request_id=excluded.request_id, path=excluded.path, state=excluded.state''',
                   (key, request_id, path or '', state))
    from .storage_layout import download_base
    root=download_base(store)
    if root:
        from .view_media.local import invalidate
        invalidate(root)


def open_request_for(store, episode_key):
    if not episode_key:
        return None
    with store.connect() as db:
        row = db.execute(
            'SELECT * FROM discover_requests WHERE episode_key=? AND state NOT IN ("failed","cancelled") ORDER BY created DESC LIMIT 1',
            (episode_key,)).fetchone()
    return dict(row) if row else None


def insert_request(store, payload):
    initialise(store)
    now = time.time()
    request_id = payload.get('id') or str(uuid.uuid4())
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        plan_claim=payload.get('_planClaim')
        if plan_claim:
            from .series_plan_work import valid_in,PlanDeferred
            from .acquisition import file_held
            from .media_management.service import today
            body=payload.get('body') or {}
            if file_held(store.directory) or not valid_in(db,plan_claim,payload.get('episode_key'),body.get('plan'),today()):raise PlanDeferred()
            if plan_claim.get('all_work'):
                previous=db.execute('''SELECT * FROM discover_requests WHERE episode_key=?
                    AND json_extract(body,'$.planParent')=?
                    AND json_extract(body,'$.plan') IN ('all','future')
                    ORDER BY created DESC,rowid DESC LIMIT 1''',
                    (payload.get('episode_key'),plan_claim['parent'])).fetchone()
                if previous:return dict(previous),False
        existing = db.execute('SELECT * FROM discover_requests WHERE idempotency=?', (payload['idempotency'],)).fetchone()
        if existing:
            return dict(existing), False
        from .media_holds import blocked_in,request_keys
        if blocked_in(db,request_keys(payload)):
            if payload.get('automatic'):
                latest=db.execute('SELECT * FROM discover_requests WHERE episode_key=? ORDER BY created DESC LIMIT 1',(payload.get('episode_key',''),)).fetchone()
                return (dict(latest) if latest else None),False
            raise ValueError('This media was cleared. Use its explicit Download or Retry action to resume it.')
        if payload.get('episode_key'):
            if payload.get('automatic'):
                # A series plan may have read its episode before the cross was
                # clicked. Check again inside the same write transaction as the
                # insert. Also honour cancellations made before per-item holds
                # were recorded; only an explicit new request can supersede one.
                latest = db.execute('SELECT * FROM discover_requests WHERE episode_key=? ORDER BY created DESC, rowid DESC LIMIT 1',
                                    (payload['episode_key'],)).fetchone()
                if latest:
                    from .source_choice import eligibility
                    if eligibility(db,latest):return dict(latest),False
                policy = db.execute('SELECT hold,removed FROM managed_media WHERE key=?',
                                    (payload['episode_key'],)).fetchone()
                body=payload.get('body') or {}
                if payload.get('rule_id'):
                    rule=db.execute('SELECT enabled FROM discover_rules WHERE id=?',(payload['rule_id'],)).fetchone()
                    if rule and not rule['enabled']:return (dict(latest) if latest else None),False
                if body.get('movieAutomatic'):
                    option=db.execute("SELECT automatic FROM movie_options WHERE key=? OR json_extract(item,'$.id')=? LIMIT 1",(payload['episode_key'],body.get('itemId',''))).fetchone()
                    if option and not option['automatic']:return (dict(latest) if latest else None),False
                if body.get('plan') in ('all','future') and body.get('planParent'):
                    plan=db.execute('SELECT download_all,download_future FROM discover_series_plan WHERE parent=?',(body['planParent'],)).fetchone()
                    if not plan or not plan['download_'+body['plan']]:return (dict(latest) if latest else None),False
                resumed_movie=False
                if latest and payload.get('kind')=='movie' and payload.get('body',{}).get('movieAutomatic'):
                    option=db.execute("SELECT automatic,enabled_at FROM movie_options WHERE key=? OR json_extract(item,'$.id')=? LIMIT 1",
                                      ('movie:'+str(int(payload['body']['externalId'])) if payload['body'].get('externalId') else payload['episode_key'],payload['body'].get('itemId',''))).fetchone()
                    resumed_movie=bool(option and option['automatic'] and option['enabled_at']>latest['updated'])
                if (latest and latest['state'] in ('cancelled', 'cancelling') and not resumed_movie) or (policy and (policy['hold'] or policy['removed'])):
                    return (dict(latest) if latest else None), False
            clash = db.execute(
                'SELECT * FROM discover_requests WHERE episode_key=? AND state NOT IN ("failed","cancelled")',
                (payload['episode_key'],)).fetchone()
            if clash:
                return dict(clash), False
        db.execute('''INSERT INTO discover_requests(id,idempotency,kind,title,state,quality,fallback,body,engine,engine_id,
            manual,rule_id,episode_key,message,progress,updated,created)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                request_id, payload['idempotency'], payload['kind'], payload['title'][:300], payload.get('state') or 'searching',
                payload['quality'], int(bool(payload.get('fallback'))), json.dumps(payload.get('body') or {}),
                payload.get('engine') or '', payload.get('engine_id') or '', int(bool(payload.get('manual', True))),
                payload.get('rule_id') or '', payload.get('episode_key') or '', payload.get('message') or '',
                float(payload.get('progress') or 0), now, now))
        row = db.execute('SELECT * FROM discover_requests WHERE id=?', (request_id,)).fetchone()
    return dict(row), True


def update_request(store, request_id, *, only_unbound=False, **fields):
    allowed = {'state', 'message', 'progress', 'engine', 'engine_id', 'quality', 'fallback', 'title'}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError('Unknown request update.')
    assignments = []
    values = []
    for key, value in fields.items():
        if key == 'fallback':
            value = int(bool(value))
        if key == 'title':
            value = str(value or 'Untitled')[:300]
        assignments.append(f'{key}=?')
        values.append(value)
    assignments.append('updated=?')
    values.append(time.time())
    values.append(request_id)
    guard = ''
    if fields.get('state') not in ('cancelled', 'cancelling'):
        guard = " AND state NOT IN ('cancelled', 'cancelling')"
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current=db.execute('SELECT * FROM discover_requests WHERE id=?',(request_id,)).fetchone()
        if only_unbound and current and current['state'] not in ('searching','waiting','starting'):
            return dict(current)
        if current and current['state']=='starting' and fields.get('state') not in (None,'starting','cancelled','cancelling'):
            pending=db.execute("SELECT 1 FROM discover_handoffs WHERE request_id=? AND stage='prepared'",(request_id,)).fetchone()
            if pending:
                # A saved native job may exist even if its caller raised before
                # acknowledging it. Only reconciliation may settle this gap.
                return dict(current)
        from .media_holds import blocked_in,request_keys_in
        if current and fields.get('state') not in ('cancelled','cancelling') and blocked_in(db,request_keys_in(db,current)):
            return dict(current)
        db.execute(f'UPDATE discover_requests SET {", ".join(assignments)} WHERE id=?{guard}', values)
        row = db.execute('SELECT * FROM discover_requests WHERE id=?', (request_id,)).fetchone()
    return dict(row) if row else None


OPEN_START = ('searching', 'waiting')


def claim_start(store, request_id):
    """Move an open request to starting, or return None when cancellation won."""
    from .acquisition import held
    if held(store):
        return None
    initialise(store)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM discover_requests WHERE id=?', (request_id,)).fetchone()
        if not row or row['state'] not in OPEN_START:
            return None
        from .media_holds import blocked_in,request_keys_in
        if blocked_in(db,request_keys_in(db,row)):return None
        generation = int(row['stop_generation'] or 0)
        from .discover_handoff import begin
        if not begin(db,store,row):return None
        changed = db.execute(
            "UPDATE discover_requests SET state='starting', updated=? WHERE id=? AND state=? AND IFNULL(stop_generation,0)=?",
            (time.time(), request_id, row['state'], generation)).rowcount
    return generation if changed else None


def fence_start(store, request_id, generation, *, transaction=None):
    """True only while this start generation is still the one allowed to create an engine job."""
    from .acquisition import held
    if held(store):
        return False
    with (store.connect() if transaction is None else nullcontext(transaction)) as db:
        row = db.execute('SELECT * FROM discover_requests WHERE id=?', (request_id,)).fetchone()
        from .discover_handoff import allowed_in
        return allowed_in(db,store,row,generation)


def commit_engine(store, request_id, generation, engine, engine_id, message, title='', *, transaction=None):
    assignments = "state='queued', engine=?, engine_id=?, message=?, updated=?"
    values = [engine, engine_id, message, time.time()]
    if title:
        assignments += ', title=?'
        values.append(str(title)[:300])
    values.extend([request_id, int(generation)])
    with (store.connect() if transaction is None else nullcontext(transaction)) as db:
        if transaction is None:db.execute('BEGIN IMMEDIATE')
        current=db.execute('SELECT * FROM discover_requests WHERE id=?',(request_id,)).fetchone()
        from .discover_handoff import allowed_in,committed_in
        if not allowed_in(db,store,current,generation):return False
        receipt=db.execute('SELECT * FROM discover_handoffs WHERE request_id=?',(request_id,)).fetchone()
        if receipt['stage']=='prepared' and (receipt['engine']!=engine or receipt['job_id']!=engine_id):return False
        changed = db.execute(
            f"UPDATE discover_requests SET {assignments} WHERE id=? AND state='starting' AND IFNULL(stop_generation,0)=?",
            values).rowcount
        if changed:committed_in(db,request_id,engine,engine_id)
    return changed == 1


def cancel_request(store, request_id, message='Cancelling.'):
    initialise(store)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        changed = db.execute(
            """UPDATE discover_requests SET state='cancelling', stop_generation=IFNULL(stop_generation,0)+1,
               message=?, updated=? WHERE id=? AND state NOT IN ('complete','cancelled')""",
            (message, time.time(), request_id)).rowcount
        row = db.execute('SELECT * FROM discover_requests WHERE id=?', (request_id,)).fetchone()
        if changed and row['episode_key'] and db.execute("SELECT 1 FROM sqlite_master WHERE name='managed_media'").fetchone():
            db.execute('UPDATE managed_media SET hold=1,updated=? WHERE key=?', (time.time(), row['episode_key']))
    return changed, (dict(row) if row else None)


def add_attempt(store, request_id, candidate_id, source, quality, state, message):
    from .discover_logic import redact
    with store.connect() as db:
        db.execute('''INSERT INTO discover_attempts(request_id,candidate_id,source,quality,state,message,created)
            VALUES (?,?,?,?,?,?,?)''', (request_id, candidate_id or '', source, quality or '', state, redact(message), time.time()))


def set_quality_policy(store, video_id, allow_lower, *, transaction=None):
    if transaction is None:initialise(store)
    with (store.connect() if transaction is None else nullcontext(transaction)) as db:
        db.execute('INSERT INTO discover_quality_policy(id,allow_lower) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET allow_lower=excluded.allow_lower',
                   (video_id, int(bool(allow_lower))))


def quality_policy(store, video_id):
    initialise(store)
    with store.connect() as db:
        row = db.execute('SELECT allow_lower FROM discover_quality_policy WHERE id=?', (video_id,)).fetchone()
    if not row:
        return None
    return bool(row['allow_lower'])


def readable_name(title, suffix='.mkv'):
    return safe_name(title, 180) + suffix
