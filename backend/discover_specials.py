"""Explicit release choices for catalogue specials without episode numbering."""
import json
import re
import time
from . import discover_sources as sources, discover_store as store_api
from .discover_logic import opaque_id, release_pairs


def catalogue(store, tvdb, episode_id):
    if not re.fullmatch(r'\d{1,12}', str(tvdb or '')):
        raise ValueError('Choose a show with a TVDB identity.')
    data = sources.episodes(str(tvdb), store)
    episode = next((e for e in data.get('episodes', []) if str(e.get('id')) == str(episode_id)), None)
    if not episode or episode.get('number') is not None or not episode.get('name'):
        raise ValueError('Choose an unnumbered special from this show.')
    with store.connect() as db:
        row = db.execute('SELECT body FROM discover_show_media WHERE tvdb=?', (str(tvdb),)).fetchone()
    show = json.loads(row['body']) if row else {}
    if not show.get('title'):
        raise ValueError('Show information is unavailable. Search for the show again.')
    return show, episode


def search(app, store, tvdb, episode_id):
    from .discover_search import query_all
    show, episode = catalogue(store, tvdb, episode_id)
    words = set(re.findall(r'[a-z0-9]+', episode['name'].lower())) - {'the', 'a', 'an'}
    query = episode['name'] if set(re.findall(r'[a-z0-9]+', show['title'].lower())) - {'the'} <= words else show['title'] + ' ' + episode['name']
    pages, warnings = query_all(sources.indexers_for(app, store), {'t': 'search', 'cat': '5000', 'q': query, 'limit': 100, 'offset': 0},store=store)
    words |= set(re.findall(r'[a-z0-9]+', show['title'].lower())) - {'the', 'a', 'an'}
    results, secrets, seen = [], [], set()
    for provider, root in pages:
        for row in root.findall('./channel/item')[:100]:
            title = row.findtext('title') or ''
            attrs = sources._attrs(row)
            identity = str(attrs.get('tvdbid') or attrs.get('tvdb') or '')
            grab = sources._grab(row)
            pairs = release_pairs(title)
            if not grab or len(pairs) != 1 or (identity and identity != str(tvdb)):
                continue
            if not words or not words <= set(re.findall(r'[a-z0-9]+', title.lower())):
                continue
            key = opaque_id(str(tvdb) + ':' + str(episode_id) + ':' + grab)
            if key in seen: continue
            seen.add(key)
            quality = re.search(r'(?i)(2160|1080|720|480)[pi]', title)
            size = str(attrs.get('size') or '0')
            result = dict(id=key, title=title, quality=quality.group(0).lower() if quality else 'Unspecified',
                          size=int(size) if size.isdigit() else 0, provider=provider,
                          releaseSeason=pairs[0][0], releaseNumber=pairs[0][1])
            results.append(result)
            secrets.append({**result, 'kind': 'special:' + str(episode_id), 'externalId': str(tvdb), 'grab': grab})
    store_api.remember_candidates(store, secrets)
    return dict(items=results, warnings=warnings, query=query)


def selected(store, tvdb, episode_id, release_id):
    if not isinstance(release_id, str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,100}', release_id):
        raise ValueError('Choose a release from this search.')
    saved = store_api.candidate(store, release_id)
    if not saved or saved['kind'] != 'special:' + str(episode_id) or saved['external_id'] != str(tvdb):
        raise ValueError('Search this special and choose one of its releases.')
    if time.time() - saved['created'] > 86400:
        raise ValueError('This release choice has expired. Search again.')
    return {**saved, 'externalId': saved['external_id']}


def queue(app, store, tvdb, episode_id, release_id, token):
    from .acquisition import ensure_open
    from .discover_acquire import _blocked, _catalogue_body, re_token, resolve_release
    ensure_open(store)
    if not isinstance(token, str) or not re_token(token): raise ValueError('Invalid request identity.')
    show, episode = catalogue(store, tvdb, episode_id)
    release = selected(store, tvdb, episode_id, release_id)
    quality = release['quality'].rstrip('pi')
    if quality not in ('720', '1080', '2160'):
        raise ValueError('Choose a 720p, 1080p or 2160p release.')
    key = f'show:{tvdb}:special:{episode_id}'
    blocked = _blocked(store, key)
    if blocked: return blocked
    from .media_holds import resume
    resume(store,'managed:'+key)
    season, number = release_pairs(release['title'])[0]
    body = _catalogue_body(show, dict(externalId=str(tvdb), source='NZB first', itemId='show:' + str(tvdb),
        catalogueEpisode=episode, manualReleaseId=release_id, releaseEpisode=dict(season=season, number=number),
        scope='Special · ' + episode['name'], showTitle=show['title']))
    row, created = store_api.insert_request(store, dict(idempotency=token + ':special', kind='episode',
        title=show['title'] + ' · ' + episode['name'], quality=quality, fallback=False, episode_key=key, body=body))
    if created: resolve_release(app, store, row, [release])
    with store.connect() as db:
        current = db.execute('SELECT id,state,message FROM discover_requests WHERE id=?', (row['id'],)).fetchone()
    return dict(current)
