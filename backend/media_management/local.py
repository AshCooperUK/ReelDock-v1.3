"""Enrol scanned library leaves without guessing a catalogue identity from names."""
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from . import files, service, store as records
from .. import discover_store as ds
from ..discover_logic import episode_identity, movie_identity
from ..view_media import local as scan


def nfo(store, path):
    """Read a bounded regular sidecar through the same no-link descriptor walk."""
    for sidecar in dict.fromkeys((path.with_suffix('.nfo'), path.parent/'movie.nfo',
                                  path.parent/'episodedetails.nfo', path.parent/'episode.nfo')):
        try:
            before = files.snapshot(store, sidecar)
            if not 0 < before.st_size < 100000:
                continue
            with files.directory(store, sidecar.parent) as fd:
                source = os.open(sidecar.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(source, 'rb') as stream:
                    files.same(before, os.fstat(stream.fileno()))
                    raw = stream.read(100000)
                files.verify(fd, sidecar.name, before)
            if len(raw) >= 100000:
                continue
            meta = ET.fromstring(raw)
            if meta.tag in ('movie', 'episodedetails'):
                return meta
        except (OSError, ValueError, ET.ParseError):
            pass
    return None


def catalogue(store, path, scanned):
    meta = nfo(store, path)
    def text(name):
        return (meta.findtext(name) or '').strip() if meta is not None else ''
    def valid_id(provider, value):
        match = re.fullmatch(r'(?:tt)?(\d{1,12})' if provider == 'imdb' else r'([1-9]\d{0,11})', value)
        return match[1] if match and int(match[1]) else ''
    ids = {name: valid_id(name, text(name+'id')) for name in ('tmdb', 'imdb', 'tvdb')}
    if meta is not None:
        for node in meta.findall('uniqueid'):
            provider = str(node.get('type') or '').lower()
            if provider in ids and not ids[provider]:
                ids[provider] = valid_id(provider, (node.text or '').strip())
    kind = 'episode' if meta is not None and meta.tag == 'episodedetails' else {'movies':'movie', 'shows':'episode', 'youtube':'video'}.get(scanned.get('kind'), 'movie')
    title = text('title') or path.stem
    year = text('year') or (text('premiered') or text('aired'))[:4]
    if not re.fullmatch(r'\d{4}', year):
        match = re.search(r'\((\d{4})\)', path.stem)
        year = match[1] if match else ''
    item = dict(id=scanned['id'], kind='show' if kind == 'episode' else kind,
                title=(text('showtitle') or title) if kind == 'episode' else title, year=year,
                releaseDate=text('premiered') or text('aired'), externalId='', imdbId='tt'+ids['imdb'] if ids['imdb'] else '')
    episode = None
    if kind == 'episode':
        episode = dict(name=title, airdate=text('aired') or text('premiered'))
        for field, tag in (('season', 'season'), ('number', 'episode')):
            if re.fullmatch(r'\d{1,4}', text(tag)):
                episode[field] = int(text(tag))
        if ids['tvdb']:
            item.update(id='tvdb:show:'+ids['tvdb'], externalId=ids['tvdb'])
        elif ids['tmdb'] or ids['imdb']:
            item['id'] = ('tmdb:show:'+ids['tmdb']) if ids['tmdb'] else 'imdb:show:'+ids['imdb']
    elif kind == 'movie':
        item['externalId'] = ids['imdb']
        if ids['tmdb'] or ids['imdb']:
            item['id'] = ('tmdb:movie:'+ids['tmdb']) if ids['tmdb'] else 'imdb:movie:'+ids['imdb']
    # A TMDB movie needs its IMDb identity; a TMDB/IMDb show needs its TVDB
    # identity. Lookup failure still leaves the file available for local actions.
    if item['id'].startswith(('tmdb:', 'imdb:show:')) and not item['externalId']:
        from ..view_media.providers import details
        try:
            detail = details(store, item['id'])
            item = {**item, **detail}
        except Exception:
            pass  # Optional catalogue enrichment must never hide a saved file.
    return kind, ds.clean_item(item), episode


def existing(app, store, path, scanned):
    with store.connect() as db:
        owned = db.execute("SELECT * FROM discover_owned WHERE state='complete' AND path=?", (str(path),)).fetchone()
        video = db.execute('''SELECT m.id,m.channel,m.day,d.title FROM media_files m
            JOIN downloads d ON d.id=m.id WHERE m.path COLLATE BINARY IN (?,?)''',
            (str(path), str(path.relative_to(Path(app.config['MEDIA_DIR']).absolute())))).fetchone()
    if owned:
        key = owned['key']
        r = records.row(store, key)
        if r:
            return r
        q = service.request_for(store, key)
        payload = json.loads(q['body']) if q else {}
        episode_match = re.fullmatch(r'show:(\d{1,12}):(\d{1,4}):(\d{1,4})', key)
        movie_match = re.fullmatch(r'movie:(\d{1,12})', key)
        kind = 'episode' if episode_match else (q or {}).get('kind', 'movie')
        external = payload.get('externalId') or ((episode_match or movie_match)[1] if episode_match or movie_match else '')
        item = ds.clean_item(payload.get('item') or dict(id=payload.get('itemId') or key,
            kind='show' if kind == 'episode' else kind, title=(q or {}).get('title') or scanned['title'],
            year=payload.get('year'), externalId=external))
        b = dict(item=item, automatic=bool(item.get('externalId') and (movie_match or episode_match)))
        if episode_match:
            b['episode'] = payload.get('episode') or dict(season=int(episode_match[2]), number=int(episode_match[3]), name=scanned['title'])
        key = records.remember(store, key, item['id'], kind, b)
        return records.row(store, key)
    if video and re.fullmatch(r'[A-Za-z0-9_-]{11}', video['id']):
        key = 'video:'+video['id']
        if not records.row(store, key):
            item = ds.clean_item(dict(id='youtube:video:'+video['id'], kind='video', videoId=video['id'],
                title=video['title'], channelId=video['channel'], published=video['day']))
            key = records.remember(store, key, item['id'], 'video', dict(item=item, date=video['day'], dateLabel='Published', automatic=True))
        return records.row(store, key)
    return None


def enrol(app, store, value):
    identity = value.get('id') if isinstance(value, dict) else None
    if not isinstance(identity, str) or not identity:
        raise ValueError('Choose a local media file from the library scan.')
    try:
        path = files.checked(store, scan.media_file(app.config['MEDIA_DIR'], identity, 'file').absolute())
        before = files.snapshot(store, path)
        if path.suffix.lower() not in files.VIDEO or not before.st_size:
            raise ValueError('Not a saved video file.')
    except (ValueError, OSError) as exc:
        raise ValueError('Local media identity does not resolve to an available video file.') from exc
    scanned = next((i for i in scan.inventory(app.config['MEDIA_DIR'])['items'] if i.get('action',{}).get('id') == identity),
                   dict(id='local:'+identity, title=path.stem, kind='movies'))
    # Opening Manage only registers verified identity/path metadata. A shared
    # operation keeps resets and file mutations fenced while allowing ongoing
    # downloads/scans to coexist with this foreground information request.
    with store.operation():
        r = existing(app, store, path, scanned)
    if r is None:
        kind, item, episode = catalogue(store, path, scanned)
        with store.operation():
            files.same(before, files.snapshot(store, path))
            r = existing(app, store, path, scanned)
            if r is None:
                if (kind == 'movie' and item['id'].startswith(('tmdb:', 'imdb:'))) or (kind == 'episode' and item.get('externalId') and 'season' in episode and 'number' in episode):
                    service.enrol(app, store, item, episode=episode, refresh=False)
                    key = (movie_identity(item['externalId']) if item.get('externalId') else item['id']) if kind == 'movie' else episode_identity(item['externalId'], episode['season'], episode['number'])
                    r = records.locate(store, key, item['id'], kind, dict(item=item, episode=episode))
                else:
                    key = 'local:'+identity
                    b = dict(item=item, automatic=False, date=item.get('releaseDate',''))
                    if episode:
                        b['episode'] = episode
                    records.remember(store, key, key, kind, b)
                    r = records.row(store, key)
    with store.operation():
        files.same(before, files.snapshot(store, path))
        records.patch_body(store, r['key'], localPath=str(path))
    return dict(key=r['key'], entry=service.compose(app, store, records.row(store, r['key'])))
