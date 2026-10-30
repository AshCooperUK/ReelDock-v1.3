"""Xtream Codes logins as an alternative to M3U playlist URLs.

An Xtream login is stored as an ordinary playlist subscription whose address is
``xtream:<id>``. On each import the provider's player_api.php is read and an
extended M3U playlist is generated, so naming, .strm export, Live TV, dry runs,
scheduling, TMDB matching and Fix match all work exactly as for M3U sources.

Credentials: the password is stored on the server like the TMDB credential and
is never returned to the browser, logged or included in error messages. As with
M3U provider URLs, the generated stream addresses inherently contain the login;
they are written only to the exported .strm files and Live_TV/livetv.m3u (the
same places an M3U source's stream URLs go), and browser downloads use opaque
handles.
"""
import hmac
import json
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, urlencode, urlsplit

from flask import jsonify, request, session

from . import m3u
from .media_duration import clock_seconds, positive

PREFIX = 'xtream:'
DEFAULT_HOURS = 24
SERIES_FETCH_LIMIT = 1500  # new or changed series read per import; the rest follow next time


def initialise(store):
    with store.connect() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS xtream_accounts(
                source TEXT PRIMARY KEY, server TEXT NOT NULL, username TEXT NOT NULL,
                password TEXT NOT NULL, info TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS xtream_series_cache(
                source TEXT NOT NULL, series_id TEXT NOT NULL, modified TEXT NOT NULL,
                data TEXT NOT NULL, PRIMARY KEY(source,series_id));
            CREATE TABLE IF NOT EXISTS xtream_vod_info(
                source TEXT NOT NULL, stream_id TEXT NOT NULL, data TEXT NOT NULL,
                updated REAL NOT NULL, PRIMARY KEY(source,stream_id));
        ''')


def normal_server(value):
    value = str(value or '').strip()
    if value and '://' not in value: value = 'http://' + value
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not 0 < (parsed.port or 80) < 65536
                or len(value) > 500 or any(ord(c) < 33 for c in value)):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Enter the Xtream server address, for example http://provider.example:8080.') from None
    path = re.sub(r'/(player_api\.php|get\.php)$', '', parsed.path.rstrip('/'), flags=re.I).rstrip('/')
    return f'{parsed.scheme}://{parsed.netloc}{path}'


def login_value(value, label):
    value = str(value or '').strip()
    if not 1 <= len(value) <= 200 or any(ord(c) < 32 for c in value):
        raise ValueError(f'Enter the Xtream {label}.')
    return value


class Client:
    def __init__(self, server, username, password, fetch=None):
        self.server, self.username, self.password = server, username, password
        self.fetch = fetch or m3u.fetch_playlist

    def api(self, action=None, **params):
        query = dict(username=self.username, password=self.password)
        if action: query['action'] = action
        query.update(params)
        try:
            text = self.fetch(self.server + '/player_api.php?' + urlencode(query))
        except ValueError:
            # The underlying message never includes the address or login.
            raise ValueError('Could not reach the Xtream server. Check the server address and your connection.') from None
        try: return json.loads(text) if text.strip() else {}
        except ValueError:
            raise ValueError('The Xtream server did not return valid data. Check the server address.') from None

    def stream(self, kind, identity, extension):
        identity = str(identity)
        if not re.fullmatch(r'\d{1,12}', identity): return ''
        extension = extension if isinstance(extension, str) and re.fullmatch(r'[A-Za-z0-9]{1,6}', extension) else ('ts' if kind == 'live' else 'mp4')
        return f'{self.server}/{kind}/{quote(self.username, safe="")}/{quote(self.password, safe="")}/{identity}.{extension}'


def authenticate(client):
    data = client.api()
    user = data.get('user_info') if isinstance(data, dict) else None
    if not isinstance(user, dict) or str(user.get('auth', '')) != '1':
        raise ValueError('The Xtream server rejected this login. Check the username and password.')
    status = str(user.get('status') or 'Active')
    if status.lower() not in ('active', ''):
        raise ValueError(f'This Xtream account is {status.lower()}.')
    expires = ''
    if str(user.get('exp_date') or '').isdigit():
        expires = datetime.fromtimestamp(int(user['exp_date']), timezone.utc).date().isoformat()
    return dict(status=status, expires=expires, max_connections=str(user.get('max_connections') or ''),
                active_connections=str(user.get('active_cons') or ''))


def account(store, source):
    with store.connect() as db:
        row = db.execute('SELECT * FROM xtream_accounts WHERE source=?', (source,)).fetchone()
    return dict(row) if row else None


def client_for(store, source, fetch=None):
    saved = account(store, source)
    if not saved: raise ValueError('This Xtream login is missing. Add it again in Settings → Xtream.')
    return Client(saved['server'], saved['username'], saved['password'], fetch)


def clean(value, limit=2000):
    if isinstance(value, list): value = ', '.join(str(v) for v in value if v not in (None, ''))
    value = '' if value is None else str(value)
    return re.sub(r'[\x00-\x1f"]+', ' ', value).replace('"', "'").strip()[:limit]


def as_list(value):
    if isinstance(value, list): return [v for v in value if isinstance(v, dict)]
    if isinstance(value, dict): return [v for v in value.values() if isinstance(v, dict)]
    return []


def extinf(duration, attrs, title):
    parts = ' '.join(f'{k}="{clean(v)}"' for k, v in attrs.items() if clean(v))
    return f'#EXTINF:{duration} {parts},{clean(title, 300)}'


def duration_seconds(info):
    """Xtream duration_secs is seconds; duration is an explicit clock string."""
    return positive(info.get('duration_secs')) or clock_seconds(info.get('duration')) or 0


def year_of(item):
    for key in ('year', 'releaseDate', 'releasedate', 'release_date'):
        match = re.match(r'((?:18|19|20|21)\d{2})', str(item.get(key) or ''))
        if match: return match[1]
    return ''


def tmdb_id(item):
    value = str(item.get('tmdb') or item.get('tmdb_id') or '').strip()
    return value if re.fullmatch(r'\d{1,12}', value) else ''


def categories(client, action):
    return {str(c.get('category_id')): clean(c.get('category_name'), 140) for c in as_list(client.api(action))}


def playlist(store, source, fetch=None):
    """Generate an extended M3U playlist from the provider's API."""
    client = client_for(store, source, fetch)
    authenticate(client)
    lines = ['#EXTM3U']
    live_groups = categories(client, 'get_live_categories')
    for item in as_list(client.api('get_live_streams')):
        url = client.stream('live', item.get('stream_id'), 'ts')
        if not url or not item.get('name'): continue
        lines += [extinf(-1, {'tvg-id': item.get('epg_channel_id'), 'tvg-name': item.get('name'), 'tvg-logo': logo(item.get('stream_icon')),
                              'group-title': live_groups.get(str(item.get('category_id')), 'Live TV'), 'x-source': 'xtream-live'}, item['name']), url]
    vod_groups = categories(client, 'get_vod_categories')
    for item in as_list(client.api('get_vod_streams')):
        url = client.stream('movie', item.get('stream_id'), item.get('container_extension'))
        name = clean(item.get('name'), 300)
        if not url or not name: continue
        year = year_of(item)
        if year and not re.search(r'\b' + year + r'\b', name): name += f' ({year})'
        lines += [extinf(duration_seconds(item), {'tvg-type': 'movie', 'tvg-logo': logo(item.get('stream_icon')), 'tmdb-id': tmdb_id(item),
                             'group-title': vod_groups.get(str(item.get('category_id')), 'Movies'), 'release-date': item.get('releasedate') or item.get('releaseDate'),
                             'x-plot': item.get('plot'), 'x-rating': rating(item.get('rating')), 'x-genre': item.get('genre'),
                             'x-cast': item.get('cast'), 'x-director': item.get('director'), 'x-source': 'xtream-movie'}, name), url]
    series_groups = categories(client, 'get_series_categories')
    fetched = 0
    with store.connect() as db:
        cache = {r['series_id']: (r['modified'], r['data']) for r in db.execute('SELECT * FROM xtream_series_cache WHERE source=?', (source,))}
    for show in as_list(client.api('get_series')):
        identity = str(show.get('series_id') or '')
        name = clean(show.get('name'), 200)
        if not re.fullmatch(r'\d{1,12}', identity) or not name: continue
        modified = str(show.get('last_modified') or '')
        cached = cache.get(identity)
        if cached and (cached[0] == modified or fetched >= SERIES_FETCH_LIMIT):
            data = json.loads(cached[1])
        elif fetched < SERIES_FETCH_LIMIT:
            fetched += 1
            data = client.api('get_series_info', series_id=identity)
            data = dict(episodes=data.get('episodes', {})) if isinstance(data, dict) else {}
            with store.connect() as db:
                db.execute('INSERT OR REPLACE INTO xtream_series_cache VALUES(?,?,?,?)', (source, identity, modified, json.dumps(data)))
        else: continue
        year = year_of(show)
        heading = name + (f' ({year})' if year and not re.search(r'\b' + year + r'\b', name) else '')
        episodes = data.get('episodes', {})
        for episode in [e for group in (episodes.values() if isinstance(episodes, dict) else [episodes]) for e in as_list(group)]:
            try: season, number = int(episode.get('season') or 0), int(episode.get('episode_num') or 0)
            except (TypeError, ValueError): continue
            url = client.stream('series', episode.get('id'), episode.get('container_extension'))
            if not url or not 0 <= season <= 999 or not 1 <= number <= 999: continue
            info = episode.get('info') if isinstance(episode.get('info'), dict) else {}
            lines += [extinf(duration_seconds(info), {'tvg-type': 'series', 'tvg-logo': logo(info.get('movie_image') or show.get('cover')), 'tmdb-id': tmdb_id(show),
                                 'group-title': series_groups.get(str(show.get('category_id')), 'Series'), 'release-date': info.get('releasedate') or info.get('air_date'),
                                 'x-plot': show.get('plot'), 'x-rating': rating(show.get('rating')), 'x-genre': show.get('genre'),
                                 'x-cast': show.get('cast'), 'x-director': show.get('director'), 'x-source': 'xtream-series'},
                          f'{heading} S{season:02d}E{number:02d}'), url]
    return '\n'.join(lines) + '\n'


def logo(value):
    value = str(value or '').strip()
    try: return m3u.validate_url(value) if value else ''
    except ValueError: return ''


def rating(value):
    try: value = round(float(value), 1)
    except (TypeError, ValueError): return ''
    return str(value) if 0 < value <= 10 else ''


def vod_details(store, row, path, fetch=None):
    """Plot, cast and genre from get_vod_info, read once per film and cached."""
    with store.connect() as db:
        source = db.execute('SELECT url FROM m3u_sources WHERE id=?', (row['source'],)).fetchone()
    if not source or not source['url'].startswith(PREFIX): return {}
    try:
        if path.stat().st_size > 32768: return {}
        stream = path.read_text(encoding='utf-8-sig').strip()
    except (OSError, UnicodeError):
        return {}
    match = re.search(r'/movie/[^/]+/[^/]+/(\d{1,12})\.', stream)
    if not match: return {}
    identity = match[1]
    with store.connect() as db:
        cached = db.execute('SELECT data,updated FROM xtream_vod_info WHERE source=? AND stream_id=?', (row['source'], identity)).fetchone()
    if cached and cached['updated'] > time.time() - 7 * 86400: return json.loads(cached['data'])
    try:
        data = client_for(store, row['source'], fetch).api('get_vod_info', vod_id=identity)
    except ValueError:
        return json.loads(cached['data']) if cached else {}
    info = data.get('info') if isinstance(data, dict) and isinstance(data.get('info'), dict) else {}
    result = {k: clean(info.get(k), 4000) for k in ('plot', 'cast', 'genre', 'director', 'releasedate') if clean(info.get(k), 4000)}
    if duration_seconds(info):result['durationSeconds']=duration_seconds(info)
    if rating(info.get('rating')): result['rating'] = rating(info.get('rating'))
    with store.connect() as db:
        db.execute('INSERT OR REPLACE INTO xtream_vod_info VALUES(?,?,?,?)', (row['source'], identity, json.dumps(result), time.time()))
    return result


def items(store):
    with store.connect() as db:
        db.execute('DELETE FROM xtream_accounts WHERE source NOT IN (SELECT id FROM m3u_sources)')
        rows = db.execute('''SELECT s.id,s.name,s.state,s.message,s.options,a.server,a.username,a.info FROM m3u_sources s
            JOIN xtream_accounts a ON a.source=s.id ORDER BY s.name COLLATE NOCASE''').fetchall()
    result = []
    for row in rows:
        options = json.loads(row['options'])
        result.append(dict(id=row['id'], name=row['name'], state=row['state'], message=row['message'],
                           server=row['server'], username=row['username'], password_saved=True,
                           enabled=bool(options.get('enabled') and options.get('hours')), hours=options.get('hours', 0),
                           account=json.loads(row['info'] or '{}')))
    return result


def save(store, payload):
    name = str(payload.get('name', '')).strip()
    if not name or len(name) > 100: raise ValueError('Give this Xtream login a name (up to 100 characters).')
    enabled = payload.get('enabled', True)
    if not isinstance(enabled, bool): raise ValueError('Choose on or off.')
    source_id = str(payload.get('id') or '')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT s.*,a.server,a.username,a.password FROM m3u_sources s JOIN xtream_accounts a ON a.source=s.id WHERE s.id=?', (source_id,)).fetchone() if source_id else None
        if source_id and not old: raise ValueError('Xtream login not found.')
        if old and old['state'] in ('running', 'queued'): raise ValueError('Wait for the current import to finish before editing.')
        server = normal_server(payload.get('server') or (old['server'] if old else ''))
        username = login_value(payload.get('username') or (old['username'] if old else ''), 'username')
        password = login_value(payload.get('password') or (old['password'] if old else ''), 'password')
        if old:
            options = json.loads(old['options'])
        else:
            if db.execute('SELECT COUNT(*) FROM m3u_sources').fetchone()[0] >= 50: raise ValueError('Up to 50 subscriptions are supported.')
            options = dict(m3u.DEFAULTS)
            source_id = uuid.uuid4().hex
        if enabled: options['hours'] = options.get('hours') or options.get('xtream_hours') or DEFAULT_HOURS
        elif options.get('hours'): options['xtream_hours'] = options['hours']; options['hours'] = 0
        options['enabled'] = enabled
        if old:
            db.execute('UPDATE m3u_sources SET name=?,options=?,next_run=? WHERE id=?',
                       (name, json.dumps(options), time.time() + options['hours'] * 3600, source_id))
            if (server, username, password) != (old['server'], old['username'], old['password']):
                db.execute('DELETE FROM xtream_series_cache WHERE source=?', (source_id,))
                db.execute('DELETE FROM xtream_vod_info WHERE source=?', (source_id,))
        else:
            from .storage_layout import source_folder
            db.execute('INSERT INTO m3u_sources(id,name,url,options,folder,next_run) VALUES(?,?,?,?,?,?)',
                       (source_id, name, PREFIX + source_id, json.dumps(options), source_folder(store,db,name,source_id), 0 if enabled else time.time()))
            if enabled: db.execute("UPDATE m3u_sources SET state='queued',message='Waiting for the first Xtream import.' WHERE id=?", (source_id,))
        db.execute('INSERT OR REPLACE INTO xtream_accounts(source,server,username,password,info) VALUES(?,?,?,?,COALESCE((SELECT info FROM xtream_accounts WHERE source=?),\'{}\'))',
                   (source_id, server, username, password, source_id))
    return source_id


def test(store, payload, fetch=None):
    saved = account(store, str(payload.get('id') or '')) if payload.get('id') else None
    if payload.get('id') and not saved: raise ValueError('Xtream login not found.')
    server = normal_server(payload.get('server') or (saved['server'] if saved else ''))
    username = login_value(payload.get('username') or (saved['username'] if saved else ''), 'username')
    password = login_value(payload.get('password') or (saved['password'] if saved else ''), 'password')
    result = authenticate(Client(server, username, password, fetch))
    if saved:
        with store.connect() as db:
            db.execute('UPDATE xtream_accounts SET info=? WHERE source=?', (json.dumps(result), saved['source']))
    message = 'Connected. Account ' + result['status'].lower()
    if result['expires']: message += f", expires {result['expires']}"
    if result['max_connections']: message += f", {result['max_connections']} connection{'s' if result['max_connections'] != '1' else ''} allowed"
    return dict(result, message=message + '.')


def register(app, store, access):
    @app.route('/api/xtream', methods=['GET', 'POST'])
    def xtream_settings():
        denied = access(local_action=True)
        if denied: return denied
        if request.method == 'GET': return jsonify(items=items(store))
        if not session.get('csrf') or not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), session['csrf']):
            return jsonify(error='Reload the page.'), 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict): return jsonify(error='Send the Xtream login details.'), 400
        try:
            action = payload.get('action', 'save')
            if action == 'save': return jsonify(id=save(store, payload))
            if action == 'test': return jsonify(test(store, payload))
            raise ValueError('Choose an Xtream action.')
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
