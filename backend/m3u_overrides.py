"""User corrections for M3U and Xtream film/series identification.

A correction is either a chosen TMDB title or the user's own details. Both are
locked: automatic matching, browsing and refreshes never overwrite them until
the user chooses "Revert to automatic". Overrides are keyed by catalogue title,
so every episode of a series shares one correction.
"""
import hashlib
import hmac
import http.client
import ipaddress
import json
import re
import secrets
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit

from flask import jsonify, request, send_file, session

from .network import public_addresses

POSTER_LIMIT = 8 * 1024 * 1024
KINDS = ('movie', 'tv')


def initialise(store):
    with store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS m3u_title_overrides(
            key TEXT PRIMARY KEY, mode TEXT NOT NULL, data TEXT NOT NULL, updated REAL NOT NULL)''')
        columns = {r[1] for r in db.execute('PRAGMA table_info(m3u_catalog_titles)')}
        if 'locked' not in columns:
            db.execute('ALTER TABLE m3u_catalog_titles ADD COLUMN locked INTEGER NOT NULL DEFAULT 0')


def override(db, key):
    row = db.execute('SELECT mode,data FROM m3u_title_overrides WHERE key=?', (key,)).fetchone()
    return dict(mode=row['mode'], data=json.loads(row['data'])) if row else None


def entry_key(store, source, path):
    from .m3u_catalogue import index
    for attempt in (0, 1):
        with store.connect() as db:
            row = db.execute('''SELECT c.key,f.kind,f.title,t.info FROM m3u_files f
                JOIN m3u_catalog_entries c ON c.source=f.source AND c.path=f.path
                JOIN m3u_catalog_titles t ON t.key=c.key WHERE f.source=? AND f.path=?''', (source, path)).fetchone()
        if row or attempt: break
        index(store)
    if not row or row['kind'] not in ('movies', 'series', 'tv'):
        raise ValueError('Only films and series can be matched.')
    return dict(row)


def reapply(db):
    """Re-lock catalogue titles that were dropped and re-added by an import."""
    db.execute('''UPDATE m3u_catalog_titles SET locked=1,checked=?,priority=0,
        data=(SELECT data FROM m3u_title_overrides o WHERE o.key=m3u_catalog_titles.key)
        WHERE locked=0 AND key IN (SELECT key FROM m3u_title_overrides)''', (time.time(),))


def _store(store, key, mode, data):
    from .m3u_catalogue import REVISION_KEY
    data = dict(data, locked=True, override=mode)
    with store.connect() as db:
        db.execute('INSERT OR REPLACE INTO m3u_title_overrides VALUES(?,?,?,?)', (key, mode, json.dumps(data), time.time()))
        # checked moves forward so local NFO and artwork files are rewritten.
        db.execute('UPDATE m3u_catalog_titles SET data=?,locked=1,checked=?,priority=0 WHERE key=?', (json.dumps(data), time.time(), key))
        db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (REVISION_KEY, str(time.time_ns())))
    return data


def summary(entity, kind):
    from .m3u_catalogue import art, normal_date
    return dict(tmdb_id=entity['id'], media_type=kind, title=entity.get('title') or entity.get('name'),
                release_date=normal_date(entity.get('release_date') or entity.get('first_air_date')),
                rating=entity.get('vote_average'), description=entity.get('overview', ''),
                artwork=art(entity.get('backdrop_path') or entity.get('poster_path')),
                tmdb_url=f"https://www.themoviedb.org/{kind}/{entity['id']}")


def search(store, query, year, kind):
    from .m3u_metadata import TMDB
    from .m3u_player import portrait
    if not store.get('m3u_tmdb_token'): raise ValueError('Connect TMDB in M3U Settings first.')
    query = str(query or '').strip()
    if not 1 <= len(query) <= 200: raise ValueError('Enter a title to search for.')
    if kind not in KINDS: raise ValueError('Choose film or TV series.')
    params = dict(query=query)
    if year not in (None, ''):
        if not re.fullmatch(r'(18|19|20|21)\d{2}', str(year)): raise ValueError('Enter a four-digit year, or leave it blank.')
        params['year' if kind == 'movie' else 'first_air_date_year'] = str(year)
    results = []
    for item in TMDB(store).get('search/' + kind, **params).get('results', [])[:12]:
        identity = item.get('id')
        if type(identity) is not int or identity <= 0: continue
        day = item.get('release_date') or item.get('first_air_date') or ''
        overview = item.get('overview') or ''
        results.append(dict(id=identity, media_type=kind, title=item.get('title') or item.get('name') or 'Untitled',
                            year=day[:4], overview=overview[:240] + ('…' if len(overview) > 240 else ''),
                            poster=portrait(item.get('poster_path'))))
    return results


def choose(store, key, kind, identity):
    from .m3u_metadata import TMDB
    if kind not in KINDS or type(identity) is not int or not 0 < identity < 2147483648:
        raise ValueError('Choose a TMDB result.')
    entity = TMDB(store).get(f'{kind}/{identity}')
    if not entity.get('id'): raise ValueError('That TMDB title is unavailable.')
    return _store(store, key, 'tmdb', summary(entity, kind))


def _public_https(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or len(url) > 2048 or any(ord(c) < 33 for c in url)):
        raise ValueError('Use an HTTPS image address without a port or login.')
    try: ipaddress.ip_address(parsed.hostname.strip('[]')); literal = True
    except ValueError: literal = False
    if literal: raise ValueError('Use an image address with a hostname, not an IP address.')
    return parsed


def fetch_poster(url):
    """HTTPS only, public addresses pinned per hop, image type and 8 MB cap."""
    for _ in range(4):
        parsed = _public_https(url)
        try: address = public_addresses(parsed.hostname)[0]
        except ValueError: raise ValueError('The poster address must be a public internet host.') from None
        connection = http.client.HTTPSConnection(parsed.hostname, timeout=15)
        try:
            raw = socket.create_connection((address, 443), timeout=15)
            try: connection.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname)
            except Exception:
                raw.close(); raise
            connection.request('GET', (parsed.path or '/') + ('?' + parsed.query if parsed.query else ''),
                               headers={'Host': parsed.hostname, 'User-Agent': 'ReelDock', 'Accept': 'image/jpeg,image/png'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                url = urljoin(url, response.getheader('Location', '')); continue
            if response.status != 200: raise ValueError('The poster image could not be downloaded.')
            if not (response.getheader('Content-Type', '').split(';')[0].strip().lower() in ('image/jpeg', 'image/png', 'image/jpg')):
                raise ValueError('The poster address must point to a JPEG or PNG image.')
            body = response.read(POSTER_LIMIT + 1)
            if len(body) > POSTER_LIMIT: raise ValueError('The poster image is larger than 8 MB.')
            if not body.startswith((b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n')):
                raise ValueError('The poster address must point to a JPEG or PNG image.')
            return body
        except (OSError, http.client.HTTPException):
            raise ValueError('The poster image could not be downloaded.') from None
        finally:
            connection.close()
    raise ValueError('The poster address redirected too many times.')


def poster_path(store, key):
    return store.directory / 'm3u-artwork' / ('override-' + hashlib.sha256(key.encode()).hexdigest())


def manual(store, key, kind, fields, fetch=None):
    if not isinstance(fields, dict): raise ValueError('Enter the title details.')
    def text(name, limit):
        value = fields.get(name, '')
        if value is None: value = ''
        if not isinstance(value, (str, int, float)) or isinstance(value, bool): raise ValueError('Check the details you entered.')
        value = str(value).strip()
        if len(value) > limit: raise ValueError(f'{name.capitalize()} is too long.')
        return value
    title = text('title', 300)
    if not title: raise ValueError('Enter a title.')
    year = text('year', 4)
    if year and not re.fullmatch(r'(18|19|20|21)\d{2}', year): raise ValueError('Enter a four-digit year, or leave it blank.')
    rating = text('rating', 5)
    if rating:
        try: rating = round(float(rating), 1)
        except ValueError: raise ValueError('Enter a rating from 0 to 10.') from None
        if not 0 <= rating <= 10: raise ValueError('Enter a rating from 0 to 10.')
    else: rating = None
    def names(name, count, limit):
        values = [v.strip() for v in text(name, 4000).split(',') if v.strip()]
        if len(values) > count or any(len(v) > limit for v in values): raise ValueError(f'Use up to {count} {name} names, separated by commas.')
        return values
    poster = text('poster_url', 2048)
    data = dict(manual=True, media_type='movie' if kind == 'movies' else 'tv', title=title, year=int(year) if year else None,
                release_date='', rating=rating, description=text('plot', 10000), genres=names('genres', 20, 60),
                cast=names('cast', 50, 120), poster_url=poster, artwork='')
    if poster:
        body = (fetch or fetch_poster)(poster)
        path = poster_path(store, key)
        path.parent.mkdir(exist_ok=True)
        temporary = path.with_name(secrets.token_hex(16) + '.tmp')
        try:
            temporary.write_bytes(body); temporary.replace(path)
        finally: temporary.unlink(missing_ok=True)
        data['artwork'] = '/api/m3u-title-art/' + key + '?v=' + hashlib.sha256(body).hexdigest()[:12]
    else:
        poster_path(store, key).unlink(missing_ok=True)
    return _store(store, key, 'manual', data)


def revert(store, key):
    from .m3u_catalogue import REVISION_KEY
    with store.connect() as db:
        db.execute('DELETE FROM m3u_title_overrides WHERE key=?', (key,))
        db.execute("UPDATE m3u_catalog_titles SET data='{}',locked=0,checked=0,priority=1 WHERE key=?", (key,))
        db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (REVISION_KEY, str(time.time_ns())))
    poster_path(store, key).unlink(missing_ok=True)


def manual_details(data, row):
    facts = {'Type': {'movies': 'Movie', 'series': 'Episode', 'tv': 'Dated TV'}.get(row['kind'], row['kind'])}
    if data.get('year'): facts['Year'] = str(data['year'])
    if data.get('genres'): facts['Genres'] = ', '.join(data['genres'])
    if data.get('rating') is not None: facts['Rating'] = str(data['rating'])
    if data.get('cast'): facts['Cast'] = ', '.join(data['cast'])
    facts['Playlist group'] = row.get('group_name') or 'Not specified'
    return dict(title=data.get('title') or row['title'], description=data.get('description', ''), facts=facts,
                artwork=data.get('artwork', ''), tmdb_url='', people=[], message='Showing your own details. Automatic matching is locked for this title.')


def state(store, source, path):
    entry = entry_key(store, source, path)
    info = json.loads(entry['info'] or '{}')
    with store.connect() as db: saved = override(db, entry['key'])
    return dict(locked=bool(saved), mode=saved['mode'] if saved else '', data=saved['data'] if saved else {},
                media_type='movie' if entry['kind'] == 'movies' else 'tv', name=info.get('name') or entry['title'],
                year=info.get('year') or '', tmdb_saved=bool(store.get('m3u_tmdb_token')))


def register(app, store, access):
    @app.route('/api/m3u/<source>/match', methods=['GET', 'POST'])
    def m3u_match(source):
        denied = access(local_action=True)
        if denied: return denied
        if request.method == 'GET':
            try: return jsonify(state(store, source, request.args.get('path', '')))
            except ValueError as exc: return jsonify(error=str(exc)), 404
        if not session.get('csrf') or not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), session['csrf']):
            return jsonify(error='Reload the page.'), 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict): return jsonify(error='Send the match details.'), 400
        try:
            entry = entry_key(store, source, str(payload.get('path', '')))
            action = payload.get('action')
            if action == 'search':
                return jsonify(results=search(store, payload.get('query'), payload.get('year'), payload.get('media_type')))
            if action == 'tmdb': choose(store, entry['key'], payload.get('media_type'), payload.get('tmdb_id'))
            elif action == 'manual': manual(store, entry['key'], entry['kind'], payload.get('fields'))
            elif action == 'revert': revert(store, entry['key'])
            else: raise ValueError('Choose a match action.')
            return jsonify(state(store, source, str(payload.get('path', ''))))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    @app.get('/api/m3u-title-art/<key>')
    def m3u_title_art(key):
        denied = access(local_action=True)
        if denied: return denied
        if not re.fullmatch(r'[a-f0-9]{64}', key): return '', 404
        path = poster_path(store, key)
        if not path.is_file(): return '', 404
        with path.open('rb') as file: prefix = file.read(8)
        return send_file(path, mimetype='image/png' if prefix.startswith(b'\x89PNG') else 'image/jpeg')
