"""Resolve a film or episode to a real NZB, then hand it to the existing engine.

Torrent results are never invented. When no torrent index is configured the
attempt is recorded and, if NZB is also selected, the NZB indexer is tried.
"""
import json
import os
import re
import time
import uuid
from .vpn.transport import nzb as vpn_nzb
from pathlib import Path
from .discover_handoff import serialised

from .discover_logic import (
    QUALITIES, same_external_id, episode_identity, movie_identity, rank_releases, release_pairs, single_episode, redact, shared_failure,
)
from .discover_store import (
    add_attempt, candidate, claim_start, commit_engine, fence_start, insert_request, mark_owned,
    open_request_for, owned, update_request,
)

class CandidateError(ValueError):
    pass


class CancelledRequest(Exception):
    pass


def replace_failed_source(app,store,row,candidate,attempt_key):
    from .source_choice import select
    return select(app,store,row,candidate,attempt_key)


def _body(row):
    try:
        return json.loads(row.get('body') or '{}')
    except json.JSONDecodeError:
        return {}


def choose(releases, wanted, allow_lower, external_id, episode=None):
    matched = []
    for release in releases or []:
        if 'sample' in str(release.get('title') or '').lower():continue
        if not same_external_id(release.get('externalId'),external_id):
            continue
        if episode and not single_episode(release.get('title') or '',episode.get('season'),episode.get('number')):
            continue
        matched.append(release)
    return rank_releases(matched, wanted, allow_lower)


def _providers(source):
    if source == 'Torrent first':
        return ('torrent', 'nzb')
    return ('nzb', 'torrent')


@vpn_nzb
def submit_nzb(app, store, release, request_row, generation=None):
    if generation is None or not fence_start(store, request_row['id'], generation):
        raise CancelledRequest()
    saved = candidate(store, release['id'])
    grab = (saved or {}).get('grab') or release.get('grab') or ''
    if not grab:
        raise CandidateError('This release has no download address.')
    from .discover_sources import fetch
    data = fetch(grab, limit=20_000_000)
    if not fence_start(store, request_row['id'], generation):
        raise CancelledRequest()
    if not data.startswith(b'<') and b'<nzb' not in data[:400].lower():
        raise CandidateError('That address did not return an NZB file.')
    from .nzb import _engine
    engine = _engine(app, store)
    preview = engine.preview_bytes(data, 'discover.nzb')
    if not preview.get('accepted'):
        raise CandidateError(preview.get('reason') or 'This NZB was rejected.')
    if preview.get('on_server') == 'missing':
        raise CandidateError('This Usenet server does not have these articles.')
    if not fence_start(store, request_row['id'], generation):
        raise CancelledRequest()
    provider, identity = _catalogue_identity(request_row)
    from .movie_options import minimum_for_request
    minimum=minimum_for_request(store,request_row)
    if request_row['kind']=='episode':
        from .show_options import minimum_for_request as show_minimum
        minimum=show_minimum(store,request_row)
    if request_row['kind']=='movie':
        from .movie_options import observe
        observe(store,[_body(request_row).get('item') or dict(kind='movie',id='movie:'+_body(request_row)['externalId'],externalId=_body(request_row)['externalId'])],'NZB')
    from .media_holds import bind_request
    content_key=engine.preview_activity_key(preview['id'],None)
    if not bind_request(store,request_row['id'],generation,[content_key]):
        raise CancelledRequest()
    from . import discover_handoff as handoff
    receipt=handoff.prepare_nzb(store,request_row['id'],generation,content_key,release['id'])
    if receipt is None:raise CancelledRequest()
    try:
        job = engine.start(preview['id'], None, library_name(request_row, release.get('title') or ''),
                           lookup_provider=provider, lookup_identity=identity,
                           handoff_token=receipt['token'],job_id=receipt['job_id'],
                           **({'minimum_minutes':minimum} if minimum is not None else {}))
    except Exception:
        # A start may have saved its checkpoint before raising. Never try a
        # second candidate until the first exact job has been reconciled.
        if handoff.attach_native(store,request_row['id'],generation):
            return dict(id=receipt['job_id'])
        if not handoff.reset_missing(store,request_row['id'],generation):raise CancelledRequest()
        raise
    if job.get('id')!=receipt['job_id']:
        raise CandidateError('The downloader returned a different handoff identity.')
    if not commit_engine(store, request_row['id'], generation, 'nzb', job['id'], 'Queued with the NZB downloader.', job.get('name') or ''):
        try:
            engine.remove(job['id'])
        except Exception:
            pass
        raise CancelledRequest()
    return {'engine': 'nzb', 'id': job['id'], 'state': job.get('state') or 'queued'}


def _catalogue_identity(request_row):
    """IMDb for a film and TVDB for an episode, taken from the catalogue request."""
    body=_body(request_row)
    if body.get('manualTvmaze') and re.fullmatch(r'tt\d{7,12}',str(body.get('imdbId') or '')):
        return 'imdb',body['imdbId']
    external = str(body.get('externalId') or '')
    if not external.isdigit():
        return '', ''
    if request_row.get('kind') == 'movie':
        return 'imdb', 'tt' + str(int(external)).zfill(7)
    if request_row.get('kind') == 'episode':
        return 'tvdb', external
    return '', ''


def library_name(request_row, release_title):
    """Catalogue title in the library form: Title (Year) or Show (Year) SxxExx."""
    from .downloaders.torrent.rename import _describe, parse_save_as, release_save_as
    body = _body(request_row)
    episode = body.get('episode') or {}
    if body.get('replaces') and body.get('replacementName'):
        # Managed replacements must publish away from the occupied library leaf.
        name=body['replacementName']
        year=str(body.get('year') or '')
        if re.fullmatch(r'\d{4}',year):name+=' ('+year+')'
        numbers=body.get('releaseEpisode') or episode
        if request_row.get('kind')=='episode' and numbers.get('season') is not None and numbers.get('number') is not None:
            name+=f" S{int(numbers['season']):02d}E{int(numbers['number']):02d}"
        return name
    if body.get('directTitleKey'):
        # The indexer has no episode number; retain the title/year without
        # assigning an invented season or leaking the release's encoding suffix.
        return f"{request_row['title']} ({body['year']})"[:180]
    if body.get('manualReleaseId'):
        release_episode = body['releaseEpisode']
        special = body['catalogueEpisode']
        year = str(body.get('year') or '')
        year_text = f' ({year})' if re.fullmatch(r'\d{4}', year) else ''
        return f"{body['showTitle']}{year_text} S{release_episode['season']:02d}E{release_episode['number']:02d} - {special['name']}"[:180]
    raw = release_save_as(str(request_row.get('title') or '').split(' · ')[0].strip())
    chosen = parse_save_as(raw)
    scene = bool(re.search(r'[._]|(?:1080|720|2160)p', raw, re.I))
    described = _describe((release_title or raw or 'video') + '.mkv')
    name = chosen['name'] if chosen.get('year') or chosen.get('episode') else described.get('name') if scene else raw
    name = str(name or described.get('name') or 'Video').strip()
    year = str(body.get('year') or chosen.get('year') or '')
    if not re.fullmatch(r'\d{4}', year) and described.get('year'):
        year = str(described['year'])
    year_text = f' ({year})' if re.fullmatch(r'\d{4}', year) else ''
    if request_row.get('kind') == 'episode' and episode.get('season') is not None and episode.get('number') is not None:
        episode_title = str(episode.get('name') or chosen.get('episode_title') or '').strip()
        suffix = ' - ' + episode_title if episode_title else ''
        return f"{name}{year_text} S{int(episode['season']):02d}E{int(episode['number']):02d}{suffix}"[:180]
    if year_text and request_row.get('kind') == 'movie':
        return f'{name}{year_text}'[:180]
    return (release_save_as(release_title or raw) or name)[:180]


def _attempted(store, request_id):
    with store.connect() as db:
        rows = db.execute("SELECT candidate_id FROM discover_attempts WHERE request_id=? AND state IN ('failed','queued')", (request_id,)).fetchall()
    return {row['candidate_id'] for row in rows if row['candidate_id']}


@serialised
def resolve_release(app, store, request_row, releases):
    """Try ranked candidates. Shared outages stop the cycle; a bad release does not."""
    generation = claim_start(store, request_row['id'])
    if generation is None:
        return
    body = _body(request_row)
    external = body.get('externalId') or ''
    episode = body.get('episode')
    source = body.get('source') or 'NZB first'
    wanted = request_row['quality']
    allow_lower = bool(request_row['fallback'])
    if wanted not in QUALITIES:
        update_request(store, request_row['id'], state='failed', message='Choose 720p, 1080p or 2160p.')
        return
    ranked = choose(releases, wanted, allow_lower, external, episode)
    if not ranked:
        message = 'No release matched this title, identity and quality.'
        if not allow_lower:
            message = 'No release matched this title at the selected quality. Lower quality is turned off.'
        add_attempt(store, request_row['id'], '', 'nzb', wanted, 'no-match', message)
        from .source_choice import fail
        if fail(store,request_row,message):return
        update_request(store, request_row['id'], state='waiting', message='Waiting for a release. ' + message)
        return
    from .settings.nzb import selection_limits
    minimum, maximum = selection_limits(store)
    order = _providers(source)
    tried = _attempted(store, request_row['id'])
    selected_id=body.get('managedReleaseId') or body.get('manualReleaseId')
    if selected_id and selected_id in tried:
        from .source_choice import restore
        if restore(store,dict(request_row,state='waiting')):return
    for provider in order:
        if provider == 'torrent':
            add_attempt(store, request_row['id'], '', 'torrent', wanted, 'unavailable',
                        'Torrent search is not configured. No torrent index is set up, so none was queried.')
            continue
        seen = set(tried)
        for release in ranked:
            if release.get('id') in seen:
                continue
            if not fence_start(store, request_row['id'], generation):
                return
            seen.add(release.get('id'))
            # Missing indexer sizes must still reach the authoritative manifest check.
            try:
                size = int(release.get('size') or 0)
            except (ValueError, TypeError):
                size = 0
            if size > 0 and not minimum <= size <= maximum:
                add_attempt(store, request_row['id'], release.get('id') or '', 'nzb',
                            release.get('quality') or '', 'size-filtered',
                            f'Indexer size ({size / 1_000_000:,.0f} MB) is outside the NZB range '
                            f'({minimum / 1_000_000:,.0f}–{maximum / 1_000_000:,.0f} MB).')
                continue
            try:
                submit_nzb(app, store, release, request_row, generation)
            except CancelledRequest:
                return
            except CandidateError as exc:
                add_attempt(store, request_row['id'], release.get('id') or '', 'nzb', release.get('quality') or '', 'failed', str(exc))
                from .source_choice import fail
                if selected_id==release.get('id') and fail(store,request_row,str(exc),candidate_id=release.get('id')):return
                continue
            except Exception as exc:
                message = redact(exc)
                add_attempt(store, request_row['id'], release.get('id') or '', 'nzb', release.get('quality') or '',
                            'rate-limited' if '429' in message else 'failed', message)
                if '429' in message:
                    update_request(store, request_row['id'], state='waiting', message=indexer_busy_message(exc,release.get('provider') or ''))
                    return
                if shared_failure(message):
                    update_request(store, request_row['id'], state='failed', message=message)
                    return
                continue
            add_attempt(store, request_row['id'], release.get('id') or '', 'nzb', release.get('quality') or '', 'queued', 'NZB accepted by the downloader.')
            return
    message='No usable release was found among the returned candidates within your NZB size and quality settings.'
    from .source_choice import fail
    if not fail(store,request_row,message):
        update_request(store, request_row['id'], state='waiting',message='Waiting for release. '+message)


def queue_manual(store, item, episodes, quality, fallback, source, request_token, *, automatic=False):
    from .acquisition import ensure_open
    ensure_open(store)
    if quality not in QUALITIES or type(fallback) is not bool or source not in ('NZB first', 'Torrent first'):
        raise ValueError('Choose a quality and source order.')
    if not isinstance(request_token, str) or not re_token(request_token):
        raise ValueError('Invalid request identity.')
    kind = item.get('kind')
    external = str(item.get('externalId') or '')
    if kind == 'movie':
        if not external.isdigit():
            raise ValueError('This film has no IMDb identity, so it cannot be matched safely.')
        key = movie_identity(external)
        blocked = _blocked(store,key)
        if blocked:return [blocked]
        if not automatic:
            from .media_holds import resume
            resume(store,'managed:'+key)
        row, _created = insert_request(store, {
            'idempotency': request_token + ':movie', 'kind': 'movie', 'title': item.get('title') or 'Film',
            'quality': quality, 'fallback': fallback, 'manual': not automatic, 'automatic': automatic, 'episode_key': key, 'state': 'searching',
            'body': _catalogue_body(item, {'externalId': external, 'source': source, 'itemId': item.get('id')}),
        })
        return [row] if row is not None else []
    if kind != 'show':
        raise ValueError('Choose a film or TV episodes.')
    if not external.isdigit():
        raise ValueError('This show has no TVDB identity, so episodes cannot be matched safely.')
    if not episodes:
        raise ValueError('Select episodes or a season first.')
    # Validate the whole batch before inserting any request.
    if not isinstance(episodes, list) or any(
        not isinstance(episode, dict)
        or type(episode.get('season')) is not int or episode['season'] < 0
        or type(episode.get('number')) is not int or episode['number'] < 1
        or type(episode.get('id')) not in (int, str) or not str(episode['id']).strip()
        for episode in episodes
    ):
        raise ValueError('Choose episodes with a season and episode number. Unnumbered specials cannot be matched safely.')
    rows = []
    for episode in episodes:
        season = episode.get('season')
        number = episode.get('number')
        episode_id = episode.get('id')
        if type(season) is not int or number is None or type(episode_id) not in (int, str):
            raise ValueError('An episode is missing its season or identity.')
        key = episode_identity(external, season, number)
        blocked = _blocked(store,key)
        if blocked:
            rows.append(blocked)
            continue
        if not automatic:
            from .media_holds import resume
            resume(store,'managed:'+key)
        label = f"S{int(season):02d}E{int(number):02d}"
        row, _created = insert_request(store, {
            'idempotency': request_token + ':' + str(episode_id), 'kind': 'episode',
            'title': (item.get('title') or 'Show') + ' · ' + label, 'quality': quality, 'fallback': fallback,
            'manual': not automatic, 'automatic': automatic, 'episode_key': key, 'state': 'searching',
            'body': _catalogue_body(item, {'externalId': external, 'source': source, 'itemId': item.get('id'),
                     'episode': {'id': episode_id, 'season': season, 'number': number, 'name': str(episode.get('name') or '')[:180]},
                     'scope': label}),
        })
        if row is not None:rows.append(row)
    return rows


def _catalogue_body(item, body):
    image = str(item.get('image') or '')
    if image.startswith(('https://api.nzbgeek.info/covers/', 'https://api.nzb.life/covers/', 'https://static.tvmaze.com/uploads/images/')) and len(image) < 400 and ' ' not in image:
        body['image'] = image
    year = str(item.get('year') or '')
    if re.fullmatch(r'\d{4}', year):
        body['year'] = year
    genre = str(item.get('genre') or '').replace('\n', ' ').strip()[:80]
    rating = str(item.get('rating') or '').strip()
    description = str(item.get('description') or '').strip()[:800]
    if genre:
        body['genre'] = genre
    if re.fullmatch(r'\d{1,2}(?:\.\d)?', rating):
        body['rating'] = rating
    if description and not description.startswith('Discovered through the NZB catalogue'):
        body['description'] = description
    return body


def re_token(value):
    import re
    return bool(re.fullmatch(r'[A-Za-z0-9-]{16,64}', value))


def _blocked(store, key):
    have = owned(store, key)
    if have and have.get('state') == 'complete':
        return {'id': have.get('request_id'), 'state': 'complete', 'message': 'Already in the library.', 'episode_key': key, 'blocked': True}
    current = open_request_for(store, key)
    if current:
        current['blocked'] = True
        current['message'] = current.get('message') or 'Already requested.'
        return current
    return None


def indexer_busy_message(error=None,provider=''):
    """Say when the indexer was asked and when the next try is due. London time, same as the guide."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    zone = ZoneInfo('Europe/London')
    now = datetime.now(zone)
    nxt = datetime.fromtimestamp(time.time() + 120, zone)
    from .discover_indexer_rate import limited_indexers,limit_message
    from .discover_sources import _provider_name
    names=limited_indexers(error) or ((_provider_name(dict(name=provider)),) if provider else ())
    return limit_message(names)+f" Last try at {now:%H:%M}. Next try after {nxt:%H:%M}."


def search_for(app, store, request_row):
    body = _body(request_row)
    if body.get('directTitleKey'):
        from .discover_title_releases import releases
        return [r for r in releases(app,store,body['item']) if r['id']==body.get('managedReleaseId')]
    if body.get('managedReleaseId'):
        from .discover_store import candidate
        release=candidate(store,body['managedReleaseId'])
        if not release or time.time()-release['created']>86400:return []
        return [dict(release,externalId=release['external_id'])]
    if body.get('manualTvmaze'):
        from .discover_manual_episodes import selected
        return [selected(store,body['manualTvmaze'],body['catalogueEpisode']['id'],body['manualReleaseId'])]
    if body.get('manualReleaseId'):
        from .discover_specials import selected
        return [selected(store, body.get('externalId'), (body.get('catalogueEpisode') or {}).get('id'), body['manualReleaseId'])]
    provider = 'movies' if request_row['kind'] == 'movie' else 'shows'
    title = request_row['title'].split(' · ')[0]
    episode = body.get('episode') or {}
    query = title
    if episode:
        query = f"{title} S{int(episode['season']):02d}E{int(episode['number']):02d}"
    if episode and provider == 'shows':
        from .discover_sources import search_episode
        return search_episode(app, store, body.get('externalId') or '', episode.get('season'), episode.get('number'))
    if request_row['kind'] == 'movie':
        from .discover_sources import search_movie
        return search_movie(app, store, body.get('externalId') or '')
    from .discover_sources import search_nzb
    result = search_nzb(app, store, provider, query, 0)
    releases = []
    for item in result.get('items') or []:
        if str(item.get('externalId') or '') != str(body.get('externalId') or ''):
            continue
        for release in item.get('releases') or []:
            releases.append({**release, 'externalId': item.get('externalId'), 'kind': item.get('kind')})
    return releases


def process(app, store, request_row):
    if request_row.get('state') in ('cancelled', 'cancelling', 'complete', 'failed'):
        return request_row
    if request_row.get('state') not in ('searching', None) and request_row.get('engine_id'):
        return request_row
    if request_row.get('blocked'):
        return request_row
    from .acquisition import held
    from .media_holds import blocked,request_keys
    if held(store) or blocked(store,request_keys(request_row)):
        return request_row
    try:
        releases = search_for(app, store, request_row)
        resolve_release(app, store, request_row, releases)
    except Exception as exc:
        message = redact(exc)
        state = 'failed' if shared_failure(message) else 'waiting'
        update_request(store, request_row['id'], only_unbound=True, state=state, message=message or 'The search could not finish.')
    from .discover_store import public_requests
    current = next((item for item in public_requests(store) if item['id'] == request_row['id']), None)
    return current or request_row


def _retry_candidate(store, row, message):
    """A bad release can try the next candidate. Account, disk and quota failures stay put."""
    from .acquisition import held
    from .media_holds import blocked,request_keys
    if held(store) or blocked(store,request_keys(row)) or row['state'] == 'cancelled' or _body(dict(row)).get('manualReleaseId'):
        return False
    lowered = message.lower()
    if any(part in lowered for part in ('password', 'connection', 'disk', 'permission', 'storage', 'reset')):
        return False
    if not any(part in lowered for part in ('article', 'incomplete', 'did not contain', 'not a rar', 'compressed')):
        return False
    return True


def resume_waiting(app, store, limit=1):
    """Search again for requests that found nothing. One at a time, so the indexer is not flooded."""
    from .acquisition import held
    from .discover_store import initialise
    initialise(store)
    if held(store):
        return
    with store.connect() as db:
        rows = db.execute(
            "SELECT * FROM discover_requests WHERE state='waiting' AND kind IN ('episode','movie') AND coalesce(json_extract(body,'$.discoveryEngine'),0)=0 AND updated<? ORDER BY updated LIMIT ?",
            (time.time() - 90, limit)).fetchall()
    for row in rows:
        from .source_choice import restore
        if restore(store,dict(row)):continue
        from .media_management.store import policy_blocks
        if policy_blocks(store,row['episode_key']):continue
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            from .media_holds import blocked_in,request_keys
            if blocked_in(db,request_keys(row)):continue
            claimed = db.execute(
                "UPDATE discover_requests SET state='searching', message='Searching for a release.', updated=? WHERE id=? AND state='waiting'",
                (time.time(), row['id'])).rowcount
        if not claimed:
            continue
        current = dict(row)
        current['state'] = 'searching'
        process(app, store, current)


def sync_videos(store):
    """A YouTube download finishes in the normal queue. Keep the Discover row in step with it."""
    with store.connect() as db:
        rows = db.execute("SELECT * FROM discover_requests WHERE kind='video' AND state IN ('queued','downloading','searching')").fetchall()
    for row in rows:
        try:
            video_id = json.loads(row['body'] or '{}').get('videoId') or ''
        except json.JSONDecodeError:
            continue
        with store.connect() as db:
            job = db.execute('SELECT state, message, progress FROM downloads WHERE id=?', (video_id,)).fetchone()
        if not job:
            continue
        mapped = {'complete': 'complete', 'failed': 'failed', 'cancelled': 'cancelled', 'skipped': 'failed',
                  'downloading': 'downloading', 'cancelling': 'downloading', 'queued': 'queued'}.get(job['state'])
        if not mapped or mapped == row['state']:
            continue
        message = {
            'complete': 'Saved in the library.',
            'failed': job['message'] or 'The download failed.',
            'cancelled': 'Cancelled.',
            'downloading': 'Downloading with yt-dlp.',
            'queued': row['message'] or 'Queued with yt-dlp.',
        }.get(mapped, row['message'])
        update_request(store, row['id'], state=mapped, message=message, progress=float(job['progress'] or 0))


LOST_JOB = 'The NZB job is no longer in the downloader.'


def saved_paths(store, paths, *, kind=None):
    """Only positive playable library files can establish ownership."""
    from .downloaders.torrent.media import classify_path
    from .media_management.files import checked, snapshot
    from .storage_layout import download_base
    base_text = str(download_base(store) or '')
    if not base_text or not Path(base_text).is_absolute():
        return []
    base = Path(base_text)
    allowed = {'video', 'audio'} if kind is None else (
        {'video'} if kind in ('movie', 'episode', 'show', 'video') else {'audio'} if kind == 'audio' else set())
    found = []
    for path in paths:
        text = str(path or '')
        if not text:
            continue
        try:
            candidate = checked(store, text)
            media_kind, _ = classify_path(candidate.relative_to(base).as_posix())
            if media_kind in allowed and snapshot(store, candidate).st_size > 0:
                found.append((media_kind, str(candidate)))
        except (OSError, ValueError):
            continue
    # A soundtrack must not take ownership ahead of the movie/episode itself.
    return list(dict.fromkeys(path for media_kind, path in sorted(found, key=lambda item: item[0] != 'video')))


def status_from_job_file(path):
    """Read a download's saved state. The worker does not own the live downloader."""
    try:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return {'state': 'downloading', 'progress': 0.0, 'error': '', 'published': [], 'stale_progress': True}
    if not isinstance(payload, dict):
        return {'state': 'downloading', 'progress': 0.0, 'error': '', 'published': [], 'stale_progress': True}
    phase = str(payload.get('phase') or '')
    state = {'download': 'downloading', 'paused': 'paused', 'choose': 'choose', 'failed': 'failed', 'queued': 'queued'}.get(phase, 'stopped')
    if payload.get('user_paused') is True and phase in ('queued', 'download', 'paused'):
        state = 'paused'
    total = int(payload.get('total_parts') or 0) if str(payload.get('total_parts') or '0').isdigit() else 0
    done = int(payload.get('done_parts') or 0) if str(payload.get('done_parts') or '0').isdigit() else 0
    progress = 1.0 if state == 'stopped' and not payload.get('error') else ((done / total) if total else 0.0)
    published = payload.get('published') if isinstance(payload.get('published'), dict) else {}
    # NZB article sizes estimate the transfer, not the eventual extracted video.
    files=payload.get('files') if isinstance(payload.get('files'),list) else []
    estimated=sum(int(segment['size']) for item in files if isinstance(item,dict)
        for segment in (item.get('segments') if isinstance(item.get('segments'),list) else [])
        if isinstance(segment,dict) and str(segment.get('size') or '').isdigit())
    return {
        'state': state, 'phase':phase, 'progress_stage':str(payload.get('progress_stage') or ''), 'progress': progress, 'error': str(payload.get('error') or ''),
        'published': [value for value in published.values() if value], 'stale_progress': True, 'estimated_bytes':estimated,
        'transfer_progress':min(1,done/total) if total else None,
    }


def status_for_job(app, store, job_id):
    """Read the owning downloader's live status, without creating an engine.

    Article counters need not be checkpointed after every article. The popup
    must use the same in-memory job as NZB Downloads while that owner is alive.
    Workers and missing/stopped owners retain the existing saved-state fallback.
    """
    engine = app.extensions.get('nzb_engine') if app is not None else None
    if engine is not None and not engine.stop.is_set():
        from .downloaders.nzb.engine import EngineError
        try:
            job = engine.job(job_id)
        except EngineError:
            pass  # This process may not own that job; its checkpoint still does.
        else:
            progress = max(0.0, min(1.0, float(job.get('progress') or 0)))
            state = job.get('state') or ''
            return dict(state=state, phase={'downloading':'download'}.get(state,state),
                        progress_stage=job.get('progress_stage') or '', progress=progress,
                        transfer_progress=progress, estimated_bytes=job.get('total_bytes') or 0,
                        published=job.get('published') or [], error=job.get('error') or '',
                        stale_progress=False)
    return status_from_job_file(Path(store.directory)/'nzb-engine/jobs'/f'{job_id}.json')


def _native_checkpoint_absent(store, kind, identity):
    """Prove absence without following symlinks or opening a native engine."""
    if kind not in ('nzb', 'torrent') or not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{32}' if kind == 'nzb' else r'[0-9a-f]{40}', identity):
        return False
    folder = 'jobs' if kind == 'nzb' else 'torrents'
    path = (Path(store.directory) / (kind + '-engine') / folder / (identity + '.json')).absolute()
    descriptor = None
    try:
        descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for component in path.parts[1:-1]:
            following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        return False  # Any existing checkpoint, including a symlink, is not absent.
    except FileNotFoundError:
        return True
    except OSError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _native_job_absent(app, kind, identity):
    engine = app.extensions.get(kind + '_engine') if app is not None else None
    if engine is None:
        return True
    if (kind == 'nzb' and os.environ.get('REELDOCK_DOWNLOADS') == 'worker'
            and getattr(app, 'config', {}).get('PROCESS_ROLE') == 'worker'):
        # The dedicated worker can retain queued NZB metadata after the web
        # owner removes its checkpoint. NzbEngine._launch cannot pull in this
        # role. The caller still proves no-follow checkpoint absence twice.
        return True
    try:
        # jobs() intentionally omits removed jobs, even while their I/O owner
        # is stopping. Retained private ownership must also block settlement.
        if identity in getattr(engine, '_jobs', {}):
            return False
        jobs = engine.jobs()
        return isinstance(jobs, list) and all(isinstance(job, dict) and job.get('id')
            and str(job['id']) != identity for job in jobs)
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
        return False


def settle_activity_cancellations(app, store):
    """Complete only held Activity Clears whose exact native owner is gone."""
    from .media_holds import blocked_in, request_keys_in
    reason = 'Cleared from Activity.'
    with store.connect() as db:
        rows = db.execute("""SELECT * FROM discover_requests WHERE state='cancelling'
            AND message=? AND engine IN ('nzb','torrent') AND engine_id!=''""", (reason,)).fetchall()
    for row in rows:
        kind, identity = row['engine'], row['engine_id']
        if not _native_checkpoint_absent(store, kind, identity) or not _native_job_absent(app, kind, identity):
            continue
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT * FROM discover_requests WHERE id=?', (row['id'],)).fetchone()
            if (not current or current['state'] != 'cancelling' or current['message'] != reason
                    or current['engine'] != kind or current['engine_id'] != identity
                    or current['stop_generation'] != row['stop_generation']):
                continue
            if not blocked_in(db, request_keys_in(db, current)):
                continue
            if not _native_checkpoint_absent(store, kind, identity) or not _native_job_absent(app, kind, identity):
                continue
            db.execute("""UPDATE discover_requests SET state='cancelled',message=?,updated=?
                WHERE id=? AND state='cancelling' AND message=? AND engine=? AND engine_id=? AND stop_generation=?""",
                (reason, time.time(), row['id'], reason, kind, identity, row['stop_generation']))


def sync_jobs(app, store):
    """Move Discover requests forward from the NZB engine without treating a grab as a finished file.

    The web process owns the download. Another process must read the saved job
    file instead of starting a second download or reporting the job as gone.
    """
    from .discover_store import initialise
    initialise(store)
    settle_activity_cancellations(app, store)
    with store.connect() as db:
        rows = db.execute(
            """SELECT * FROM discover_requests WHERE engine='nzb' AND engine_id!='' AND (
                state IN ('queued','downloading','verifying','paused') OR (state='failed' AND message=?)
            )""",
            (LOST_JOB,)).fetchall()
    if not rows:
        return
    engine = app.extensions.get('nzb_engine') if app is not None else None
    if engine is not None and not engine.stop.is_set():
        jobs = {job['id']: job for job in engine.jobs()}
        job_dir = Path(engine.state_dir)
    else:
        jobs = {}
        job_dir = Path(store.directory) / 'nzb-engine' / 'jobs'
    now = time.time()
    for row in rows:
        job = jobs.get(row['engine_id'])
        if not job:
            job = status_from_job_file(job_dir / f"{row['engine_id']}.json")
        if not job:
            if row['state'] == 'failed' or now - float(row['updated'] or 0) < 120:
                continue
            update_request(store, row['id'], state='failed', message=LOST_JOB)
            continue
        state = job.get('state') or ''
        published = saved_paths(store, job.get('published') or [], kind=row['kind'])
        if _body(dict(row)).get('replaces') and state=='stopped' and job.get('progress_stage')=='complete' and not job.get('error'):
            from .media_management.files import finish_replacement
            from .reset import ResetBusy
            try:
                path=finish_replacement(app,store,dict(row),job.get('published') or [])
            except ResetBusy:
                continue  # A playback/metadata reader can finish before the next tick.
            except (OSError,ValueError) as exc:
                from .media_management.store import history
                message=redact(str(exc))
                update_request(store,row['id'],state='failed',message=message)
                history(store,row['episode_key'],'Replacement stopped: '+message)
                try:
                    from .media_management.files import cleanup_replacement
                    from .downloaders.torrent.media import VIDEO
                    token=json.loads(row['body'] or '{}').get('replacementName','')
                    for published_path in published:
                        if token and Path(published_path).suffix.lower() in VIDEO:
                            cleanup_replacement(store,Path(published_path),token)
                except (OSError,ValueError):
                    pass  # Staging is hidden from the library scan; retry is safe.
                continue
            if path:
                update_request(store,row['id'],state='complete',progress=100,message='Replacement saved in the library.')
            else:
                update_request(store,row['id'],state='verifying',message='Replacement ready; waiting for acquisition to resume.')
            continue
        if _body(dict(row)).get('replaces') and state=='stopped' and not job.get('error'):
            update_request(store,row['id'],state='verifying',message='Publishing replacement metadata.')
            continue
        if state == 'stopped' and published and not job.get('error') and job.get('progress_stage')=='publishing':
            update_request(store,row['id'],state='verifying',message='Publishing library metadata.')
            continue
        if state == 'stopped' and published and not job.get('error'):
            completed = update_request(store, row['id'], state='complete', progress=100, message='Saved in the library.')
            if not completed or completed['state'] != 'complete' or any(
                    completed[key] != row[key] for key in ('id', 'engine', 'engine_id', 'stop_generation')):
                continue  # Clear/cancellation can win while the native checkpoint is being read.
            mark_owned(store, row['episode_key'], row['id'], published[0], 'complete')
            from .activity import record
            record(store, 'discover-' + row['id'], row['title'], 'complete', 'Saved in the library.')
            continue
        if state == 'paused':
            update_request(store, row['id'], state='paused', message='Paused in NZB downloads.')
            continue
        if state == 'failed' or job.get('error'):
            message = redact(job.get('error') or 'The NZB download failed.')
            from .source_choice import fail
            if fail(store,dict(row),message):continue
            if _retry_candidate(store, row, message):
                update_request(store, row['id'], state='waiting', message='Trying another release. ' + message)
            else:
                update_request(store, row['id'], state='failed', message=message)
                from .activity import record
                record(store, 'discover-' + row['id'], row['title'], 'failed', message)
            continue
        if state == 'choose':
            update_request(store, row['id'], state='verifying', message='Downloaded. Choose where to save it in NZB Downloads.')
            continue
        mapped = 'downloading' if state == 'downloading' else 'queued'
        raw = float(job.get('progress') or 0)
        progress = raw * 100 if raw <= 1 else raw
        if job.get('stale_progress'):
            progress = max(progress, float(row['progress'] or 0))
        update_request(store, row['id'], state=mapped, progress=progress,
                       message='Downloading from Usenet.' if mapped == 'downloading' else 'Queued with the NZB downloader.')


def new_token():
    return uuid.uuid4().hex
