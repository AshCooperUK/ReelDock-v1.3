"""Explicit, read-only TMDB matching jobs. Never fetch stream or artwork URLs."""
from .metadata_control import Cancelled, checkpoint, vod_task
import json
import re
import time
import unicodedata

import requests

from .m3u_naming import match_key


def matches(store, source):
    with store.connect() as db:
        return {r['key']: json.loads(r['data']) for r in db.execute('SELECT key,data FROM m3u_matches WHERE source=?', (source,))}


def save_credential(store, payload):
    if payload.get('clear') is True:
        store.set('m3u_tmdb_token', '')
        return
    token = payload.get('token', '')
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{20,4096}', token.strip()):
        raise ValueError('Enter your TMDB API Read Access Token or API key.')
    store.set('m3u_tmdb_token', token.strip())


def request(store, source_id):
    if store.get('m3u_share_metadata', 'true') != 'true':
        raise ValueError('Enable VOD metadata in Settings > Metadata first.')
    if not store.get('m3u_tmdb_token'):
        raise ValueError('Save your TMDB API credential in Settings > Metadata first.')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM m3u_sources WHERE id=?', (source_id,)).fetchone()
        busy = db.execute("SELECT 1 FROM m3u_dry_runs WHERE source=? AND state IN ('queued','running') UNION ALL SELECT 1 FROM m3u_metadata_runs WHERE source=? AND state IN ('queued','running')", (source_id, source_id)).fetchone()
        if not row or row['state'] in ('running', 'queued') or busy:
            raise ValueError('Wait for the current playlist task to finish.')
        options = json.loads(row['options'])
        if options.get('naming', 'legacy') == 'legacy':
            raise ValueError('Choose a media-server naming preset for this playlist first.')
        # The user reviews a dry run before applying new matched names to disk.
        options['enabled'] = False; options['hours'] = 0
        db.execute("UPDATE m3u_sources SET options=?,message='Metadata matching requested; automatic refresh paused for review.' WHERE id=?", (json.dumps(options), source_id))
        db.execute('DELETE FROM m3u_dry_runs WHERE source=?', (source_id,))
        db.execute("INSERT OR REPLACE INTO m3u_metadata_runs VALUES(?,'queued','Waiting for metadata matching.','{}',?)", (source_id, time.time()))


class TMDB:
    def __init__(self, store, progress=None, cache_seconds=86400):
        self.store = store
        self.cache_seconds = cache_seconds
        self.token = store.get('m3u_tmdb_token')
        self.deadline = time.monotonic() + 600
        self.calls = 0
        self.progress = progress

    def get(self, path, **params):
        checkpoint()
        key = json.dumps([path, params], sort_keys=True)
        with self.store.connect() as db:
            cached = db.execute('SELECT * FROM m3u_tmdb_cache WHERE key=?', (key,)).fetchone()
        if cached and cached['updated'] > time.time() - self.cache_seconds:
            return json.loads(cached['data'])
        if not self.token:
            raise ValueError('Save your TMDB API credential before matching metadata.')
        if self.calls >= 1000 or time.monotonic() > self.deadline:
            raise ValueError('Metadata task reached its request or time limit. Completed matches are saved; run it again to continue.')
        self.calls += 1
        if self.progress: self.progress()
        headers = {'Accept': 'application/json'}
        if re.fullmatch(r'[a-fA-F0-9]{32}', self.token): params['api_key'] = self.token
        else: headers['Authorization'] = 'Bearer ' + self.token
        try:
            with requests.get('https://api.themoviedb.org/3/' + path, params=params, headers=headers,
                              timeout=(5, 20), allow_redirects=False, stream=True) as response:
                if response.status_code == 404: result = {}
                elif response.status_code in (401, 403): raise ValueError('TMDB rejected the API credential. Check it in Settings > Metadata.')
                elif response.status_code == 429: raise ValueError('TMDB rate limit reached. Completed matches are saved; retry later.')
                elif response.status_code != 200: raise ValueError('TMDB is unavailable. Completed matches are saved; retry later.')
                else:
                    raw = bytearray()
                    for chunk in response.iter_content(65536):
                        checkpoint()
                        raw.extend(chunk)
                        if len(raw) > 4*1024*1024: raise ValueError('TMDB metadata response exceeded the size limit.')
                    result = json.loads(raw)
                    if not isinstance(result, dict): raise ValueError('TMDB returned invalid metadata.')
        except (requests.RequestException, json.JSONDecodeError, UnicodeError):
            raise ValueError('Could not read TMDB metadata. Completed matches are saved; retry later.') from None
        checkpoint()
        # Never store API credentials or provider stream addresses in this cache.
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO m3u_tmdb_cache VALUES(?,?,?)', (key, json.dumps(result), time.time()))
        return result

    def detect(self, info):
        """Confirm an unlabelled numeric ID; never infer its source from existence alone."""
        kind = 'movie' if info['kind'] == 'movies' else 'tv'
        identity = info['identity']
        if not re.fullmatch(r'\d{1,12}', identity): return {}
        def title_key(value):
            return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())
        expected = title_key(info['name'])
        def agrees(item):
            names = [item.get(k, '') for k in ('title', 'original_title') if kind == 'movie'] or [item.get(k, '') for k in ('name', 'original_name')]
            day = item.get('release_date' if kind == 'movie' else 'first_air_date', '')
            return (bool(expected) and type(item.get('id')) is int and item['id'] > 0
                    and expected in [title_key(n) for n in names if isinstance(n, str)]
                    and (not info.get('year') or day[:4] == str(info['year'])))
        candidates = [self.get(f'{kind}/{identity}')]
        if kind == 'tv':
            candidates.extend(self.get('find/' + identity, external_source='tvdb_id').get('tv_results', []))
        verified = {item['id']: item for item in candidates if agrees(item)}
        if len(verified) != 1: return {}
        chosen = next(iter(verified.values()))
        return self.get(f"tv/{chosen['id']}") if kind == 'tv' else chosen

    def entity(self, info):
        kind = 'movie' if info['kind'] == 'movies' else 'tv'
        provider, identity = info['provider'], info['identity']
        if provider == 'auto': return self.detect(info)
        if provider == 'tmdb': return self.get(f'{kind}/{identity}')
        result = self.get('find/' + identity, external_source=provider+'_id')
        candidates = result.get(kind+'_results', [])
        if len(candidates) != 1: return {}
        return candidates[0] if kind == 'movie' else self.get(f"tv/{candidates[0]['id']}")

    def movie(self, info):
        item = self.entity(info)
        day = item.get('release_date', '')
        return {'year': int(day[:4])} if re.match(r'^(19|20)\d{2}-\d{2}-\d{2}$', day) else {}

    def episodes(self, info):
        show = self.entity(info)
        if not show.get('id'): return {}
        by_date = {}
        # Fetch each season once, including specials. Multiple different episodes
        # on one date are ambiguous and must not be assigned arbitrarily.
        for season in show.get('seasons', []):
            number = season.get('season_number')
            if not isinstance(number, int) or not 0 <= number <= 9999: continue
            for episode in self.get(f"tv/{show['id']}/season/{number}").get('episodes', []):
                day = episode.get('air_date'); ep = episode.get('episode_number')
                if day and isinstance(ep, int) and 1 <= ep <= 9999:
                    by_date.setdefault(day, set()).add((number, ep))
        return {day:dict(season=next(iter(values))[0], episode=next(iter(values))[1])
                for day, values in by_date.items() if len(values) == 1}


def tick(store, fetch):
    return vod_task(store, lambda: _tick(store, fetch))

def _tick(store, fetch):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT s.* FROM m3u_sources s JOIN m3u_metadata_runs r ON r.source=s.id WHERE r.state='queued' ORDER BY r.updated LIMIT 1").fetchone()
        if not row: return False
        source = dict(row)
        db.execute("UPDATE m3u_metadata_runs SET state='running',message='Reading the playlist for metadata matching.',updated=? WHERE source=?", (time.time(), source['id']))
    report = dict(checked=0, matched=0, unresolved=0, total=0, samples=[])
    def update(message, state='running'):
        checkpoint()
        with store.connect() as db:
            db.execute('UPDATE m3u_metadata_runs SET state=?,message=?,report=?,updated=? WHERE source=?', (state, message, json.dumps(report), time.time(), source['id']))
    try:
        from .m3u import parse
        # Keep the original playlist identity as the cache key on every run.
        # Applying old matches here would change auto IDs to TMDB IDs before saving.
        entries, _ = parse(fetch(source['url']), json.loads(source['options']))
        candidates = {}
        for item in entries:
            info = item.get('info', {})
            if (info.get('kind') == 'movies' and not info.get('year')) or info.get('kind') == 'tv' or (info.get('provider') == 'auto' and info.get('kind') in ('movies', 'series')):
                key = match_key(info)
                candidates.setdefault(key or '|unidentified|'+info['name']+'|'+info['day'], info)
        report['total'] = len(candidates)
        client = TMDB(store, lambda: update(f"Reading TMDB metadata: {report['checked']:,} of {report['total']:,} titles checked.")); shows = {}
        for key, info in candidates.items():
            update(f"Matching metadata: {report['checked']:,} of {report['total']:,} titles checked.")
            result = {}
            resolved = info
            if info.get('provider') == 'auto':
                entity = client.detect(info)
                if entity.get('id'):
                    result = dict(provider='tmdb', identity=str(entity['id']))
                    resolved = dict(info, **result)
                else:
                    resolved = dict(info, identity='')
            if resolved['identity']:
                if info['kind'] == 'movies': result.update(client.movie(resolved))
                elif info['kind'] == 'tv':
                    show_key = (resolved['provider'], resolved['identity'])
                    if show_key not in shows: shows[show_key] = client.episodes(resolved)
                    result.update(shows[show_key].get(info['day'], {}))
            checkpoint()
            if result:
                with store.connect() as db:
                    db.execute('INSERT OR REPLACE INTO m3u_matches VALUES(?,?,?)', (source['id'], key, json.dumps(result)))
                report['matched'] += 1
            else: report['unresolved'] += 1
            report['checked'] += 1
            if len(report['samples']) < 20:
                report['samples'].append(dict(title=info['name'], date=info['day'], result=result,
                    message=('Numeric ID verified against title and year.' if info['provider'] == 'auto' else 'Matched by provider ID.') if result else 'No unique match. Check the ID source or add a confirmed episode match.'))
        update('Metadata matching complete. Review a new dry run before importing; automatic refresh remains paused.', 'complete')
    except Cancelled:
        raise
    except Exception as exc:
        update(str(exc) if isinstance(exc, ValueError) else 'Metadata task interrupted. Completed matches are saved; retry later.', 'error')
    return True
