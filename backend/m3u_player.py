"""Authenticated details, artwork and bounded browser downloads for imported stream links."""
from .storage_layout import source_base
import http.client
import hashlib
import json
import os
import re
import secrets
import socket
import sqlite3
import ssl
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlencode, parse_qs, quote

from flask import Response, jsonify, request, send_file, session
from werkzeug.utils import secure_filename

from . import m3u, m3u_naming
from .m3u_metadata import TMDB
from .network import public_addresses
from .media_duration import fields as duration_fields
from .vpn.transport import metadata, vod


def _remember_duration(store,row,data):
    facts=duration_fields(seconds=data.get('durationSeconds'),minutes=data.get('runtimeMinutes'))
    if not facts:return
    with store.connect() as db:
        current=db.execute('SELECT data FROM m3u_file_info WHERE source=? AND path=?',(row['source'],row['path'])).fetchone()
        if current:
            saved=json.loads(current['data'])
            if isinstance(saved,dict):
                saved.update(facts)
                db.execute('UPDATE m3u_file_info SET data=? WHERE source=? AND path=?',(json.dumps(saved),row['source'],row['path']))


def video_suffix(url, mime, prefix=b''):
    """Recognise containers when a provider uses an opaque URL and generic MIME."""
    if len(prefix) >= 12 and prefix[4:8] == b'ftyp': return '.mp4'
    if prefix.startswith(b'\x1a\x45\xdf\xa3'):
        return '.webm' if b'webm' in prefix[:512] else '.mkv'
    if prefix.startswith(b'RIFF') and prefix[8:12] == b'AVI ': return '.avi'
    if prefix.startswith(b'OggS'): return '.ogv'
    if len(prefix) > 376 and all(prefix[i] == 0x47 for i in (0,188,376)): return '.ts'
    if prefix.startswith(b'\x00\x00\x01\xba'): return '.mpg'
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in {'.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v', '.ts', '.mpg', '.mpeg', '.ogv'}: return suffix
    return {'video/mp4':'.mp4', 'video/x-matroska':'.mkv', 'video/webm':'.webm', 'video/quicktime':'.mov', 'video/mp2t':'.ts', 'video/x-msvideo':'.avi', 'video/mpeg':'.mpg', 'video/ogg':'.ogv'}.get(mime, '.bin')


def upstream(url, byte_range=''):
    """Validate and pin every redirect. Never forward cookies or credentials."""
    for _ in range(6):
        parsed = urlsplit(m3u.validate_url(url))
        address = public_addresses(parsed.hostname)[0]
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=20)
        try:
            connection.sock = socket.create_connection((address, port), timeout=20)
            if parsed.scheme == 'https':
                connection.sock = ssl.create_default_context().wrap_socket(connection.sock, server_hostname=parsed.hostname)
            headers = {'Host': parsed.netloc, 'User-Agent': 'ReelDock', 'Accept-Encoding': 'identity'}
            if byte_range: headers['Range'] = byte_range
            connection.request('GET', (parsed.path or '/') + ('?' + parsed.query if parsed.query else ''), headers=headers)
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                url = urljoin(url, response.getheader('Location', ''))
                connection.close()
                continue
            return connection, response, url
        except Exception:
            connection.close()
            raise
    raise ValueError('The stream redirected too many times.')


def nfo_data(base, path):
    result = {}
    candidates = [path.with_suffix('.nfo'), path.parent / 'movie.nfo', path.parent / 'tvshow.nfo', path.parent.parent / 'tvshow.nfo']
    for candidate in reversed(candidates):
        try:
            candidate = m3u.contained(base, candidate.relative_to(base))
            if not candidate.is_file() or candidate.stat().st_size > 1024*1024: continue
            raw = candidate.read_bytes()
            if b'<!DOCTYPE' in raw.replace(b'\x00', b'').upper() or b'<!ENTITY' in raw.replace(b'\x00', b'').upper(): continue
            root = ET.fromstring(raw)
            for tag in ('title', 'plot', 'year', 'premiered', 'aired', 'runtime', 'genre', 'country', 'studio', 'director', 'rating', 'season', 'episode'):
                values = [node.text.strip()[:10000] for node in root.findall(tag) if node.text and node.text.strip()]
                if values: result['showtitle' if tag == 'title' and root.tag == 'tvshow' else tag] = ', '.join(values)
            # Episode TMDB IDs identify the episode, not its parent show.
            for node in ([] if root.tag == 'episodedetails' else root.findall('uniqueid')):
                provider = node.get('type', '')
                value = (node.text or '').strip()
                if provider in ('imdb', 'tmdb', 'tvdb') and re.fullmatch(r'tt\d{1,12}' if provider == 'imdb' else r'\d{1,12}', value):
                    result[provider+'-id'] = value
            cast = [node.findtext('name', '').strip() for node in root.findall('actor')][:12]
            if any(cast): result['cast'] = ', '.join(filter(None, cast))
        except (OSError, ValueError, ET.ParseError):
            continue
    return result


def local_art(base, path):
    for folder in (path.parent, path.parent.parent):
        for name in (path.stem+'-thumb.jpg', 'poster.jpg', 'folder.jpg', 'fanart.jpg', 'poster.png'):
            try:
                candidate = m3u.contained(base, (folder / name).relative_to(base))
                if candidate.is_file() and candidate.stat().st_size <= 12*1024*1024: return candidate
            except (OSError, ValueError): pass
    return None


@metadata
def cached_image_path(store, url):
    from .metadata_control import checkpoint
    checkpoint()
    cache = store.directory / 'm3u-artwork'
    cache.mkdir(exist_ok=True)
    path = cache / hashlib.sha256(url.encode()).hexdigest()
    if not path.is_file():
        connection = None
        try:
            connection, response, _ = upstream(url)
            raw = response.read(8*1024*1024+1)
            checkpoint()
            if response.status != 200 or len(raw)>8*1024*1024 or not raw.startswith((b'\xff\xd8', b'\x89PNG')):
                raise ValueError('Artwork unavailable.')
            temporary = cache / (secrets.token_hex(16)+'.tmp')
            try:
                temporary.write_bytes(raw); temporary.replace(path)
            finally: temporary.unlink(missing_ok=True)
        finally:
            if connection: connection.close()
    return path


def cached_image(store, url):
    path = cached_image_path(store, url)
    with path.open('rb') as file: prefix = file.read(8)
    return send_file(path, mimetype='image/png' if prefix.startswith(b'\x89PNG') else 'image/jpeg')


def portrait(path):
    return '/api/m3u-artwork/'+path[1:]+'?size=w185' if isinstance(path, str) and re.fullmatch(r'/[A-Za-z0-9_-]+\.(?:jpg|png)', path) else ''


def people(credits, guests=()):
    result, seen = [], set()
    entries = list(credits.get('cast', []))[:24] + list(guests)[:12]
    entries += [p for p in credits.get('crew', []) if p.get('job') in ('Director', 'Writer', 'Screenplay', 'Creator')][:12]
    for person in entries:
        identity = person.get('id')
        if type(identity) is not int or not 0 < identity < 2147483648 or not person.get('name'): continue
        role = person.get('character') or person.get('job') or ''
        if identity in seen:
            existing = next(p for p in result if p['id'] == identity)
            if role and role not in existing['role'].split(' · '): existing['role'] += ' · '+role
            continue
        seen.add(identity)
        result.append(dict(id=identity, name=person['name'], role=role, image=portrait(person.get('profile_path'))))
    return result


def details(store, row, base, path):
    local = nfo_data(base, path)
    with store.connect() as db:
        saved = db.execute('SELECT data FROM m3u_file_info WHERE source=? AND path=?', (row['source'], row['path'])).fetchone()
    saved = json.loads(saved['data']) if saved else {}
    attrs = dict(saved.get('attrs', {}))
    attrs.update({k:v for k,v in local.items() if k.endswith('-id')})
    for provider, identity in re.findall(r'(?i)[\[{](imdb|tmdb|tvdb)(?:id)?[=-](tt\d+|\d+)[\]}]', row['path']):
        attrs.setdefault(provider.lower()+'-id', identity)
    if row['kind'] == 'movies': attrs['tvg-type'] = 'movie'
    info = saved.get('info') or m3u_naming.describe(row['title'], '0', row['group_name'], '', json.loads(row['options']), attrs)
    provider, identity = m3u_naming.provider_id(attrs, json.loads(row['options']))
    if identity: info.update(provider=provider, identity=identity)
    for key in ('year', 'season', 'episode'):
        if re.fullmatch(r'\d{1,4}', local.get(key, '')): info[key] = int(local[key])
    data = dict(title=local.get('title') or row['title'], description=local.get('plot', ''), facts={}, artwork='', tmdb_url='', message='')
    data.update(duration_fields(seconds=saved.get('durationSeconds'),minutes=local.get('runtime') or saved.get('runtimeMinutes')))
    if saved.get('attrs', {}).get('tvg-logo'):
        data['artwork'] = f"/api/m3u/{row['source']}/artwork?"+urlencode({'path':row['path']})
    labels = {'year':'Year', 'premiered':'Released', 'aired':'Air date', 'runtime':'Runtime (minutes)', 'genre':'Genres', 'country':'Country', 'studio':'Studio', 'director':'Director', 'rating':'Rating', 'season':'Season', 'episode':'Episode', 'cast':'Cast', 'showtitle':'Series'}
    data['facts'] = {label:local[key] for key,label in labels.items() if local.get(key)}
    data['facts'].update({'Type':{'movies':'Movie','series':'Episode','tv':'Dated TV','unsorted':'Unsorted'}.get(row['kind'],row['kind']), 'Playlist group':row['group_name'] or 'Not specified'})
    for key,label in [('year','Year'), ('season','Season'), ('episode','Episode'), ('day','Air date')]:
        if info.get(key) is not None and info.get(key) != '': data['facts'].setdefault(label, str(info[key]))
    for key,label in [('x-genre','Genres'),('x-cast','Cast'),('x-director','Director'),('x-rating','Rating')]:
        if attrs.get(key): data['facts'].setdefault(label, attrs[key])
    data['description'] = data['description'] or attrs.get('x-plot', '')
    from .m3u_overrides import override
    with store.connect() as db:
        entry = db.execute('SELECT key FROM m3u_catalog_entries WHERE source=? AND path=?', (row['source'], row['path'])).fetchone()
        locked = override(db, entry['key']) if entry else None
    if locked and locked['mode'] == 'manual':
        from .m3u_overrides import manual_details
        return manual_details(locked['data'], row)
    if row['kind'] == 'movies' and str(attrs.get('x-source', '')).startswith('xtream'):
        from .xtream import vod_details
        extra = vod_details(store, row, path)
        data.update(duration_fields(seconds=extra.get('durationSeconds')))
        data['description'] = data['description'] or extra.get('plot', '')
        for key,label in [('genre','Genres'),('cast','Cast'),('director','Director'),('rating','Rating'),('releasedate','Released')]:
            if extra.get(key): data['facts'].setdefault(label, str(extra[key]))
    if locked:
        # A chosen TMDB title replaces every automatic identification.
        info.update(provider='tmdb', identity=str(locked['data']['tmdb_id']))
        data['title'] = locked['data'].get('title') or data['title']
    if not store.get('m3u_tmdb_token'):
        _remember_duration(store,row,data)
        return data
    try:
        client = TMDB(store)
        kind = 'movie' if row['kind'] == 'movies' else 'tv'
        entity = {}
        if locked:
            kind = locked['data'].get('media_type') or kind
            entity = {'id': locked['data']['tmdb_id']}
        elif info.get('identity'): entity = client.entity(info)
        elif row['kind'] in ('movies', 'series', 'tv'):
            candidates = client.get('search/'+kind, query=info['name']).get('results', [])
            matches = [c for c in candidates if (c.get('title') or c.get('name') or '').casefold() == info['name'].casefold()
                       and (not info.get('year') or str(c.get('release_date', c.get('first_air_date', ''))).startswith(str(info['year'])))]
            if len(matches) == 1: entity = matches[0]
        identity = entity.get('id')
        if type(identity) is not int or identity <= 0:
            data['message'] = 'No unique TMDB match found. Showing library information.'
            return data
        entity = client.get(f'{kind}/{identity}', append_to_response='credits')
        programme = entity
        guests = [];episode_runtime=None
        if kind == 'tv' and info.get('season') is not None and info.get('episode') is not None:
            episode = client.get(f"tv/{identity}/season/{int(info['season'])}/episode/{int(info['episode'])}")
            if episode:
                episode_runtime=episode.get('runtime')
                guests = episode.get('guest_stars', [])
                entity = dict(entity, **{k:v for k,v in episode.items() if v is not None and v != ''})
        data['people'] = people(programme.get('credits', {}), guests)
        # A series' usual runtime is not the selected episode's known length.
        data.update(duration_fields(minutes=entity.get('runtime') if kind=='movie' else episode_runtime))
        data['description'] = data['description'] or entity.get('overview', '')
        fields = {'Series':programme.get('name') if kind == 'tv' else '',
                  'Episode title':entity.get('name') if kind == 'tv' and info.get('episode') is not None else '',
                  'Release / air date':entity.get('air_date') or entity.get('release_date') or entity.get('first_air_date'),
                  'Runtime (minutes)':entity.get('runtime'), 'Language':entity.get('original_language'),
                  'Rating (TMDB)':entity.get('vote_average'), 'Status':entity.get('status'),
                  'Genres':', '.join(g['name'] for g in programme.get('genres', []) if g.get('name')),
                  'Country':', '.join(g['name'] for g in programme.get('production_countries', []) if g.get('name')),
                  'Cast':', '.join(g['name'] for g in programme.get('credits', {}).get('cast', [])[:12] if g.get('name'))}
        for label,value in fields.items():
            if value is not None and value != '': data['facts'].setdefault(label, str(value))
        if data['people']: data['facts'].pop('Cast', None)
        data['tmdb_url'] = f'https://www.themoviedb.org/{kind}/{identity}'
        from .m3u_catalogue import art as catalogue_art, normal_date
        summary=dict(tmdb_id=identity,media_type=kind,title=programme.get('title') or programme.get('name'),
            release_date=normal_date(programme.get('release_date') or programme.get('first_air_date')),
            rating=programme.get('vote_average'),description=programme.get('overview',''),
            artwork=catalogue_art(programme.get('backdrop_path') or programme.get('poster_path')),tmdb_url=data['tmdb_url'],
            **({'runtimeMinutes':programme['runtime']} if kind=='movie' and type(programme.get('runtime')) is int and 0<programme['runtime']<=525600 else {}))
        if locked: data['message'] = 'Matched by you. Automatic matching is locked for this title.'
        else:
          with store.connect() as db:
            db.execute('UPDATE m3u_catalog_titles SET data=?,checked=?,priority=0 WHERE locked=0 AND key=(SELECT key FROM m3u_catalog_entries WHERE source=? AND path=?)',(json.dumps(summary),time.time(),row['source'],row['path']))
        art = entity.get('still_path') or programme.get('backdrop_path') or programme.get('poster_path') or ''
        if re.fullmatch(r'/[A-Za-z0-9_-]+\.(?:jpg|png)', art): data['artwork'] = '/api/m3u-artwork/'+art[1:]
    except (ValueError, KeyError, TypeError):
        data['message'] = 'TMDB details are unavailable. Showing library information.'
    _remember_duration(store,row,data)
    return data


def register(app, store, access):
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS m3u_stream_resources (token TEXT PRIMARY KEY, url TEXT NOT NULL, expires REAL NOT NULL)')
        db.execute('CREATE INDEX IF NOT EXISTS m3u_stream_url ON m3u_stream_resources(url,expires)')
        db.execute('CREATE INDEX IF NOT EXISTS m3u_stream_expiry ON m3u_stream_resources(expires)')

    def local_copy(source=None,relative=None,identity=None):
        from .m3u_downloads import roots
        with store.connect() as db:
            row=db.execute("SELECT d.*,s.name FROM m3u_downloads d LEFT JOIN m3u_sources s ON s.id=d.source WHERE d.state='complete' AND "+('d.id=?' if identity else 'd.source=? AND d.path=?'),(identity,) if identity else (source,relative)).fetchone()
        if not row:return None
        path=Path(row['destination'])
        for root in roots(app):
            if path.is_relative_to(root):
                try:path=m3u.contained(root,path.relative_to(root))
                except ValueError:return None
                if path.is_file():return dict(row),path
        return None

    def resource(url):
        # Opaque handles keep provider credentials out of browser URLs and manifests.
        with store.connect() as db:
            db.execute('DELETE FROM m3u_stream_resources WHERE expires<?', (time.time(),))
            row = db.execute('SELECT token FROM m3u_stream_resources WHERE url=? AND expires>? LIMIT 1', (url, time.time()+3600)).fetchone()
            if row: token = row['token']
            else:
                token = secrets.token_urlsafe(32)
                db.execute('INSERT INTO m3u_stream_resources VALUES(?,?,?)', (token, url, time.time()+86400))
        return token

    def lookup(source):
        relative = request.args.get('path', '')
        with store.connect() as db:
            row = db.execute('SELECT f.*,s.folder,s.name,s.options FROM m3u_files f JOIN m3u_sources s ON s.id=f.source WHERE f.source=? AND f.path=?', (source, relative)).fetchone()
        if not row or not relative.lower().endswith('.strm'): raise ValueError('Title unavailable.')
        base = source_base(store, app.config['M3U_DIR'], row['folder'])
        path = m3u.contained(base, relative)
        if not path.is_file(): raise ValueError('Stream file no longer exists. Refresh this playlist.')
        return dict(row), base, path

    @app.get('/api/m3u/<source>/download-info')
    def download_info(source):
        denied = access(local_action=True)
        if denied: return denied
        try:
            saved=local_copy(source,request.args.get('path',''))
            session.setdefault('csrf',secrets.token_urlsafe(32))
            if saved:
                row,path=saved
                return jsonify(csrf=session['csrf'],local=True,download=f'/api/m3u/{source}/download?'+urlencode({'path':row['path']}),downloadAvailable=True,downloadReason='')
            row, base, path = lookup(source)
            if path.stat().st_size > 16384: raise ValueError('Stream link is too large.')
            lines = [line.strip() for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip() and not line.startswith('#')]
            if len(lines) != 1: raise ValueError('This stream file must contain one media link.')
            url = m3u.validate_url(lines[0])
            parsed = urlsplit(url)
            youtube = (parsed.path.strip('/') if parsed.hostname == 'youtu.be' else parse_qs(parsed.query).get('v', [''])[0] if parsed.hostname in ('youtube.com', 'www.youtube.com', 'm.youtube.com') and parsed.path == '/watch' else '')
            unavailable = bool(re.fullmatch(r'[A-Za-z0-9_-]{11}', youtube)) or parsed.path.lower().endswith('.m3u8')
            return jsonify(csrf=session['csrf'],local=False,download='' if unavailable else f'/api/m3u/{source}/download?'+urlencode({'path':row['path']}),
                           downloadAvailable=not unavailable,downloadReason='Download this source to the server or export its stream link.' if unavailable else '')
        except (OSError, UnicodeError, ValueError): return jsonify(error='This imported stream file is unavailable or invalid.'), 404

    @app.get('/api/m3u/<source>/download')
    def download_video(source):
        denied = access(local_action=True)
        if denied: return denied
        try:
            saved=local_copy(source,request.args.get('path',''))
            if saved:
                row,path=saved
                response=send_file(path,mimetype='application/octet-stream',as_attachment=True,download_name=path.name,conditional=True)
                response.headers['Cache-Control']='private, no-store'
                return response
            row, base, path = lookup(source)
            if path.stat().st_size > 16384: raise ValueError('Stream link is too large.')
            lines = [line.strip() for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip() and not line.startswith('#')]
            if len(lines) != 1: raise ValueError('Invalid stream link.')
            url = m3u.validate_url(lines[0])
            return deliver(resource(url), Path(row['path']).stem)
        except (OSError, UnicodeError, ValueError):
            return jsonify(error='This video file is unavailable.'), 404

    @app.get('/api/m3u/<source>/details')
    def media_details(source):
        denied = access(local_action=True)
        if denied: return denied
        try:
            row, base, path = lookup(source)
            result = details(store, row, base, path)
            if local_art(base, path) and 'locked' not in result.get('message', ''): result['artwork'] = f'/api/m3u/{source}/artwork?'+urlencode({'path':row['path']})
            return jsonify(result)
        except (OSError, ValueError): return jsonify(error='Title information unavailable.'), 404

    @app.get('/api/m3u/<source>/artwork')
    def media_artwork(source):
        denied = access(local_action=True)
        if denied: return denied
        try:
            row, base, path = lookup(source)
            from .m3u_overrides import override, poster_path
            with store.connect() as db:
                entry = db.execute('SELECT key FROM m3u_catalog_entries WHERE source=? AND path=?', (source, row['path'])).fetchone()
                locked = override(db, entry['key']) if entry else None
            if locked and locked['mode'] == 'manual' and poster_path(store, entry['key']).is_file():
                return send_file(poster_path(store, entry['key']), mimetype='image/png' if poster_path(store, entry['key']).read_bytes()[:4] == b'\x89PNG' else 'image/jpeg')
            if locked and locked['data'].get('artwork', '').startswith('/api/m3u-artwork/'):
                return cached_image(store, 'https://image.tmdb.org/t/p/w780/'+locked['data']['artwork'].rsplit('/',1)[-1])
            art = local_art(base, path)
            if art: return send_file(art)
            with store.connect() as db:
                saved = db.execute('SELECT data FROM m3u_file_info WHERE source=? AND path=?', (source, row['path'])).fetchone()
            logo = json.loads(saved['data']).get('attrs', {}).get('tvg-logo', '') if saved else ''
            if logo:
                try:return cached_image(store, m3u.validate_url(logo))
                except (OSError,ValueError,http.client.HTTPException):pass
            with store.connect() as db:
                entry=db.execute('SELECT t.data FROM m3u_catalog_entries c JOIN m3u_catalog_titles t ON t.key=c.key WHERE c.source=? AND c.path=?',(source,row['path'])).fetchone()
            artwork=json.loads(entry['data']).get('artwork','') if entry else ''
            if artwork.startswith('/api/m3u-artwork/'):
                return cached_image(store,'https://image.tmdb.org/t/p/w780/'+artwork.rsplit('/',1)[-1])
        except (OSError, ValueError, http.client.HTTPException): pass
        return '', 404

    @app.get('/api/m3u-person/<int:identity>')
    def person_details(identity):
        denied = access(local_action=True)
        if denied: return denied
        if not 0 < identity < 2147483648: return jsonify(error='Person unavailable.'), 404
        try:
            entity = TMDB(store).get(f'person/{identity}')
            if not entity.get('name'): return jsonify(error='Person unavailable.'), 404
            return jsonify(name=entity['name'], biography=entity.get('biography') or 'No biography available.',
                           image=portrait(entity.get('profile_path')), facts={label:entity[key] for key,label in
                           [('known_for_department','Known for'),('birthday','Born'),('deathday','Died'),('place_of_birth','Place of birth')]
                           if entity.get(key)}, tmdb_url=f'https://www.themoviedb.org/person/{identity}')
        except (ValueError, TypeError, KeyError): return jsonify(error='Biography unavailable. Try again later.'), 503

    @app.get('/api/m3u-artwork/<name>')
    def tmdb_artwork(name):
        denied = access(local_action=True)
        if denied: return denied
        if not re.fullmatch(r'[A-Za-z0-9_-]+\.(?:jpg|png)', name): return '', 404
        size = 'w185' if request.args.get('size') == 'w185' else 'w780'
        try: return cached_image(store, 'https://image.tmdb.org/t/p/'+size+'/'+name)
        except (OSError, ValueError, http.client.HTTPException): return '', 404

    @vod
    def deliver(token, download_name):
        with store.connect() as db:
            row = db.execute('SELECT url FROM m3u_stream_resources WHERE token=? AND expires>?', (token, time.time())).fetchone()
        if not row: return '', 404
        byte_range = request.headers.get('Range', '')
        if byte_range and not re.fullmatch(r'bytes=\d*-\d*', byte_range): return '', 416
        connection = None
        try:
            connection, response, url = upstream(row['url'], byte_range)
            if response.status not in (200, 206):
                connection.close()
                return '', 416 if response.status == 416 else 502
            mime = response.getheader('Content-Type', '').split(';')[0].lower()
            if 'mpegurl' in mime or urlsplit(url).path.lower().endswith('.m3u8'):
                connection.close()
                return jsonify(error='This source is a streaming playlist, not a single video file.'), 409
            headers = {key:response.getheader(key) for key in ('Content-Length','Content-Range','Accept-Ranges') if response.getheader(key)}
            # Provider MIME only informs the filename suffix; the body is always an opaque attachment.
            if not (mime.startswith(('video/', 'audio/')) or mime in ('application/octet-stream','application/mp4')):
                mime = 'application/octet-stream'
            first_chunk = b''
            suffix = video_suffix(url, mime)
            if suffix == '.bin': suffix = video_suffix(row['url'], mime)
            if suffix == '.bin':
                # Keep the prefix in the response body. Resumed downloads need
                # a separate bounded header request because they start mid-file.
                if not byte_range or byte_range.startswith('bytes=0-'):
                    first_chunk = response.read(4096)
                    prefix = first_chunk
                else:
                    probe_connection = None
                    try:
                        probe_connection, probe, _ = upstream(row['url'], 'bytes=0-4095')
                        prefix = probe.read(4096) if probe.status in (200, 206) else b''
                    finally:
                        if probe_connection: probe_connection.close()
                suffix = video_suffix(url, mime, prefix)
            from .media_library import safe_name
            filename = safe_name(download_name) + suffix
            fallback = secure_filename(filename) or 'ReelDock-video'+suffix
            headers['Content-Disposition'] = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"
            transfer_revision=store.get('auth_revision')
            def reset_stopped():
                return store.reset_pending() or store.get('auth_revision') != transfer_revision
            def chunks():
                identity='browser-download-'+secrets.token_hex(12)
                sent=0;updated=0;state='cancelled'
                expected=int(headers.get('Content-Length','0'))
                def report(status,message):
                    with store.operation():
                        if reset_stopped():return
                        try:
                            from .activity import record
                            record(store,identity,'Browser download · '+download_name,status,message,
                                   min(100,100*sent/expected) if expected else None)
                        except (OSError,sqlite3.Error):pass
                report('running','Sending the video file to your browser.')
                try:
                    if first_chunk:
                        sent+=len(first_chunk)
                        yield first_chunk
                    while not reset_stopped():
                        chunk = response.read(65536)
                        if not chunk: break
                        sent+=len(chunk)
                        if time.monotonic()-updated>=1:
                            report('running',f'Sending video to your browser: {sent/1048576:.1f} MB transferred.')
                            updated=time.monotonic()
                        yield chunk
                    state='complete' if not expected or sent==expected else 'failed'
                except (OSError, http.client.HTTPException):state='failed'
                finally:
                    connection.close()
                    report(state,{'complete':'Video transfer to your browser finished.','cancelled':'Browser transfer stopped.','failed':'The video transfer was interrupted. Retry the download.'}[state])
            result = Response(chunks(), status=response.status, headers=headers, mimetype='application/octet-stream')
            result.call_on_close(connection.close)
            return result
        except (OSError, ValueError, UnicodeError, http.client.HTTPException):
            if connection: connection.close()
            return jsonify(error='The stream provider could not be reached.'), 502
