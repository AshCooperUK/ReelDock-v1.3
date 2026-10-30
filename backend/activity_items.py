"""Clear one authoritative Activity identity and fence its related acquisition work.

No title matching, provider calls or media-file deletion belongs on this path.
The durable fence is committed before opening a native engine or stopping work.
"""
import json
import os
import re
import stat
import threading
import time
from pathlib import Path

from . import activity_clear, media_holds

KINDS = ('youtube', 'm3u', 'discover', 'torrent', 'nzb', 'event', 'm3u_sync')
TABLES = {kind: activity_clear.TABLES[kind] for kind in KINDS if kind not in ('torrent', 'nzb')}
ACTIVE_DISCOVER = ('searching', 'starting', 'waiting', 'queued', 'downloading', 'verifying', 'paused', 'cancelling')


class ActivityChanged(ValueError):
    status = 409


def _observed(db, kind, row):
    from .activity_observation import capture
    version = None if kind in ('torrent', 'nzb') else activity_clear.generation(db, kind, row['item']['id'])
    return capture(kind, row['item'], version)


def _superseded_terminal(kind, identity, target, rows):
    """Historical failure cleanup must not become a stop on its replacement.

    A Discover parent can briefly lag its failed native child. That exact pair
    is still one failed attempt, rather than proof of a newer active attempt.
    """
    if activity_clear.section(kind, target['item']) is None:
        return False
    active = {'queued', 'downloading', 'seeding', 'paused', 'choose', 'running',
              'searching', 'starting', 'waiting', 'verifying', 'cancelling'}
    for key, row in rows.items():
        if key == (kind, identity) or row is None or row['item'].get('state') not in active:
            continue
        item = row['item']
        if key[0] == 'discover' and kind in ('torrent', 'nzb') and item.get('engine') == kind and str(item.get('engine_id')) == identity:
            continue
        return True
    return False


def _ack(store, kind, identity, cleared=0, held=False, stopping=False, scope='media'):
    return dict(ok=True, kind=kind, id=identity, cleared=cleared, held=bool(held),
                stopping=bool(stopping), scope=scope, **activity_clear.metadata(store))


def _json(value):
    try:
        parsed = json.loads(value or '{}')
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _read(path, limit):
    """Bounded regular-file reads: never follow a shared-tree symlink."""
    path = Path(path).absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError('Unavailable saved job.')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
            raise ValueError('Unavailable saved job.')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            result = stream.read(limit + 1)
        if len(result) > limit:
            raise ValueError('Unavailable saved job.')
        return result
    finally:
        os.close(fd)


def _saved_native(store, kind, identity):
    """Verify an unopened native job before allowing its job-ID restore fence."""
    if not re.fullmatch(r'[0-9a-f]{40}' if kind == 'torrent' else r'[0-9a-f]{32}', identity):
        return None
    folder = Path(store.directory) / (kind + '-engine') / ('torrents' if kind == 'torrent' else 'jobs')
    try:
        payload = json.loads(_read(folder / (identity + '.json'), 32 * 1024 * 1024))
        if not isinstance(payload, dict) or not isinstance(payload.get('selected'), list):
            return None
        if kind == 'torrent':
            from .downloaders.torrent.metainfo import parse_torrent
            meta = parse_torrent(_read(folder / (identity + '.torrent'), 2_000_000))
            if meta.info_hash.hex() != identity:
                return None
            indexes = {item.index for item in meta.files}
            if not payload['selected'] or any(type(index) is not int or index not in indexes for index in payload['selected']):
                return None
            title = payload.get('save_as') or meta.name
            content_key = 'native:torrent:' + meta.info_hash.hex()
        else:
            if payload.get('id') != identity or not isinstance(payload.get('files'), list) or not payload['files']:
                return None
            # Use the native parser's own schema, without constructing an engine.
            from .downloaders.nzb.engine import checkpoint_activity_key
            content_key = checkpoint_activity_key(payload)
            title = payload.get('save_as') or payload.get('name') or 'NZB'
        phase = str(payload.get('phase') or '')
        state = {'download': 'downloading', 'failed': 'failed', 'error': 'error', 'queued': 'queued',
                 'paused': 'paused', 'choose': 'choose', 'stopped': 'stopped', 'seed': 'seeding'}.get(phase, 'downloading')
        if (kind == 'torrent' and payload.get('user_paused') or
                kind == 'nzb' and payload.get('user_paused') is True and phase in {'queued', 'download', 'paused'}):
            state = 'paused'
        added = payload.get('added', 0)
        if type(added) not in (int, float) or not 0 <= added < 1e12:
            added = 0
        version = payload.get('activity_generation', 0)
        if type(version) is not int or not 0 <= version < 9_007_199_254_740_991:
            version = 0
        return dict(kind=kind, _activity_key=content_key, item=dict(id=identity, name=str(title), state=state, phase=phase,
            progress=1 if phase == 'stopped' and not payload.get('error') else 0,
            added=added, error=str(payload.get('error') or ''), activity_generation=version))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None


def _native(app, store, kind, identity):
    engine = app.extensions.get(kind + '_engine')
    if engine is not None:
        match = next((item for item in engine.jobs() if str(item.get('id')) == identity), None)
        if match is not None:
            try:
                key = engine.activity_key(identity)
            except ValueError:
                key = None
            return dict(kind=kind, _activity_key=key, item=dict(match))
    return _saved_native(store, kind, identity)


def _row(db, kind, identity):
    found = db.execute('SELECT * FROM ' + TABLES[kind] + ' WHERE id=?', (identity,)).fetchone()
    if found is None:
        return None
    item = dict(found)
    if kind == 'event':
        item.update(subscription=item.pop('category'), finished=item['created'])
    elif kind == 'm3u_sync':
        item.update(title='M3U library sync', subscription='M3U', created=item.get('finished', 0))
    elif kind == 'discover':
        item.update(subscription='Discover', mediaKind='movie' if item['kind'] == 'movie' else 'show')
    return dict(kind=kind, item=item)


def _graph(db, kind, identity, native, seeds=None):
    """Follow stored IDs/content identities only, never a title or parent channel."""
    rows = {}; keys = set(); pending = list(seeds) if seeds is not None else [(kind, identity)]; managed = set()
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    while pending:
        current, target = pending.pop()
        target = str(target)
        if (current, target) in rows:
            continue
        found = native.get((current, target)) if current in ('torrent', 'nzb') else _row(db, current, target)
        if found is None and current not in ('torrent', 'nzb'):
            continue
        rows[current, target] = found
        if current == 'youtube':
            keys.add('video:' + target)
            keys.add('managed:video:' + target)
            pending.extend(('discover', row[0]) for row in db.execute(
                "SELECT id FROM discover_requests WHERE kind='video' AND json_valid(body) AND json_extract(body,'$.videoId')=?", (target,)))
        elif current == 'm3u':
            item = found['item']; keys.add(media_holds.vod_key(item['source'], item['path']))
        elif current in ('torrent', 'nzb'):
            keys.add('native:' + current + '-job:' + target)
            if found is not None and found.get('_activity_key'):
                keys.add(found['_activity_key'])
        elif current == 'discover':
            item = found['item']; body = _json(item.get('body'))
            keys.update(media_holds.request_keys(item))
            if 'media_request_keys' in tables:
                keys.update(row[0] for row in db.execute('SELECT key FROM media_request_keys WHERE request_id=?',(target,)))
            if item.get('episode_key'):
                key = item['episode_key']; keys.add('managed:' + key); managed.add(key)
                pending.extend(('discover', row[0]) for row in db.execute('SELECT id FROM discover_requests WHERE episode_key=?', (key,)))
            if item['kind'] == 'video' and isinstance(body.get('videoId'), str) and body['videoId']:
                keys.add('video:' + body['videoId']); pending.append(('youtube', body['videoId']))
            engine = item.get('engine'); job = item.get('engine_id')
            if engine in ('torrent', 'nzb', 'vod', 'youtube') and job:
                pending.append(({'vod': 'm3u', 'youtube': 'youtube'}.get(engine, engine), job))
        if current in ('m3u', 'torrent', 'nzb', 'youtube'):
            engine = 'vod' if current == 'm3u' else current
            pending.extend(('discover', row[0]) for row in db.execute(
                'SELECT id FROM discover_requests WHERE engine=? AND engine_id=?', (engine, target)))
        if current in ('m3u', 'torrent', 'nzb', 'discover'):
            engine = 'vod' if current == 'm3u' else current
            for table in ('movie_download_policy', 'show_download_policy'):
                if table not in tables:
                    continue
                if current == 'discover':
                    policies = db.execute('SELECT engine,job,body FROM ' + table + " WHERE json_valid(body) AND json_extract(body,'$.requestId')=?", (target,))
                    for policy in policies:
                        if policy['engine'] in ('torrent', 'nzb', 'vod'):
                            pending.append(('m3u' if policy['engine'] == 'vod' else policy['engine'], policy['job']))
                else:
                    policy = db.execute('SELECT body FROM ' + table + ' WHERE engine=? AND job=?', (engine, target)).fetchone()
                    request_id = _json(policy['body']).get('requestId') if policy else None
                    if isinstance(request_id, str) and request_id:
                        pending.append(('discover', request_id))
        if keys and 'media_request_keys' in tables:
            pending.extend(('discover', row[0]) for row in db.execute(
                'SELECT DISTINCT request_id FROM media_request_keys WHERE key IN ('+','.join('?' for _ in keys)+')',tuple(keys)))
    managed.update(key[len('managed:'):] for key in keys if key.startswith('managed:'))
    return rows, keys, managed


def _same_native(app, keys, rows):
    """A second native ID may own exactly the same content, especially NZBs."""
    matches = {}
    for kind in ('torrent', 'nzb'):
        engine = app.extensions.get(kind + '_engine')
        if engine is None or not any(key.startswith('native:' + kind + ':') for key in keys):
            continue
        for item in engine.jobs():
            identity = str(item.get('id'))
            if (kind, identity) in rows:
                continue
            try:
                key = engine.activity_key(identity)
            except ValueError:
                continue
            if key in keys:
                matches[kind, identity] = dict(kind=kind, _activity_key=key, item=dict(item))
    return matches


def _resolve_graph(app, store, db, kind, identity, native, seeds=None):
    while True:
        rows, keys, managed = _graph(db, kind, identity, native, seeds)
        for current, target_id in list(rows):
            if current in ('torrent', 'nzb') and rows[current, target_id] is None:
                rows[current, target_id] = _native(app, store, current, target_id)
                saved = rows[current, target_id]
                if saved is not None:
                    native[current, target_id] = saved
                    if saved.get('_activity_key'):
                        keys.add(saved['_activity_key'])
        matches = _same_native(app, keys, rows)
        if not matches:
            return rows, keys, managed
        native.update(matches)
        seeds = [*rows, *matches]


def _stop_sql(db, rows, managed, now):
    stopping = False
    for key in managed:
        db.execute('UPDATE managed_media SET hold=1,updated=? WHERE key=?', (now, key))
    for (kind, identity), row in rows.items():
        if row is None:
            continue
        state = row['item'].get('state')
        if kind == 'youtube' and state in ('queued', 'downloading', 'cancelling'):
            next_state = 'cancelled' if state == 'queued' else 'cancelling'
            db.execute('UPDATE downloads SET state=?,message=?,updated=? WHERE id=?', (next_state, 'Cleared from Activity.', now, identity))
            stopping |= next_state == 'cancelling'
        elif kind == 'm3u' and state in ('queued', 'running', 'cancelling'):
            next_state = 'cancelled' if state == 'queued' else 'cancelling'
            db.execute('UPDATE m3u_downloads SET state=?,message=?,updated=? WHERE id=?', (next_state, 'Cleared from Activity.', now, identity))
            stopping |= next_state == 'cancelling'
        elif kind == 'discover' and state in ACTIVE_DISCOVER:
            if state != 'cancelling':
                db.execute("UPDATE discover_requests SET state='cancelling',stop_generation=stop_generation+1,message=?,updated=? WHERE id=?", ('Cleared from Activity.', now, identity))
            stopping = True
    return stopping


def _commit(db, rows, keys, managed, now):
    previous = media_holds.suppressed_ids(db)
    cleared = 0
    new_rows = [(current, target_id) for current, target_id in rows if target_id not in previous.get(current, [])]
    for current, target_id in new_rows:
        row = rows[current, target_id]
        if row is not None:
            cleared += activity_clear.archive_row(db, row, now)
    held = bool(keys)
    if held:
        media_holds.hold(db, keys, rows)
    stopping = _stop_sql(db, rows, managed, now)
    if cleared or (held and new_rows):
        db.execute('UPDATE activity_clear_revision SET revision=revision+1 WHERE id=1')
    return cleared, held, stopping


def _clear_one(app, store, kind, identity, observed=None):
    now = time.time(); native = {}; archive_only = False; fences = {}; versions = {}
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        # The action may have waited for a writer while an explicit replacement
        # or same-ID retry advanced. Read its native generation inside this fence.
        if kind in ('torrent', 'nzb'):
            saved = _native(app, store, kind, identity)
            if saved is not None:
                native[kind, identity] = saved
        previous = media_holds.suppressed_ids(db)
        known = identity in previous.get(kind, [])
        target = native.get((kind, identity)) if kind in ('torrent', 'nzb') else _row(db, kind, identity)
        if observed is not None:
            from .activity_observation import valid
            if not valid(observed):
                raise ActivityChanged('Refresh Activity before clearing this item.')
            if target is None or _observed(db, kind, target) != observed:
                # Replaying an already archived observation must not resolve its
                # identity again into newer work. The acknowledgement is read-only.
                if activity_clear.is_hidden(db, kind, identity, observed['generation']):
                    return _ack(store, kind, identity, scope='attempt')
                raise ActivityChanged('This download attempt changed. Activity has been refreshed; clear the current item only if you want to stop it.')
        if target is None and not known:
            rows, keys, managed = {}, set(), set()
        else:
            rows, keys, managed = _resolve_graph(app, store, db, kind, identity, native)
        if target is not None and _superseded_terminal(kind, identity, target, rows):
            # Keep the failed outcome in TXT history, but add no media/content
            # hold: historical content keys are also retained by the replacement.
            rows, keys, managed = {(kind, identity): target}, set(), set()
            archive_only = True
        cleared, held, stopping = _commit(db, rows, keys, managed, now)
        for (current, target_id), row in rows.items():
            if current in ('torrent', 'nzb'):
                fence = db.execute('SELECT group_id FROM media_clear_holds WHERE key=?', ('native:' + current + '-job:' + target_id,)).fetchone()
                fences[current, target_id] = fence[0] if fence else None
                versions[current, target_id] = (row or {}).get('item', {}).get('activity_generation', 0)
    if archive_only:
        return _ack(store, kind, identity, cleared, scope='attempt')
    # The fence is durable BEFORE this factory. Held restore cannot start work.
    pending = list(rows); processed = set()
    while pending:
        current, target_id = pending.pop()
        if current not in ('torrent', 'nzb') or (current, target_id) in processed:
            continue
        processed.add((current, target_id)); saved = rows[current, target_id]
        engine = app.extensions.get(current + '_engine')
        if engine is None and saved is None:
            continue
        try:
            with store.connect() as db:
                fence = db.execute('SELECT group_id FROM media_clear_holds WHERE key=?', ('native:' + current + '-job:' + target_id,)).fetchone()
                if not fence or not fences.get((current, target_id)) or fence[0] != fences[current, target_id]:
                    continue
            if engine is None:
                if current == 'torrent':
                    from .torrents import _engine
                else:
                    from .nzb import _engine
                engine = _engine(app, store)
            content_key = engine.activity_key(target_id)
            keys.add(content_key)
            # A factory can reveal another persisted ID with identical content.
            # Archive/hold its authoritative SQL links before removing either.
            matches = _same_native(app, keys, rows)
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                fence = db.execute('SELECT group_id FROM media_clear_holds WHERE key=?', ('native:' + current + '-job:' + target_id,)).fetchone()
                latest = _native(app, store, current, target_id)
                if (not fence or fence[0] != fences.get((current, target_id)) or latest is None
                        or latest['item'].get('activity_generation', 0) != versions.get((current, target_id), 0)):
                    continue
                if matches:
                    native.update(matches)
                    expanded, expanded_keys, managed = _resolve_graph(app, store, db, kind, identity, native, [*rows, *matches])
                    more_cleared, more_held, more_stopping = _commit(db, expanded, keys | expanded_keys, managed, now)
                    cleared += more_cleared; held |= more_held; stopping |= more_stopping
                    rows = expanded; keys |= expanded_keys
                    for (extra_kind, extra_id), extra_row in rows.items():
                        if extra_kind in ('torrent', 'nzb') and (extra_kind, extra_id) not in fences:
                            extra_fence = db.execute('SELECT group_id FROM media_clear_holds WHERE key=?', ('native:' + extra_kind + '-job:' + extra_id,)).fetchone()
                            fences[extra_kind, extra_id] = extra_fence[0] if extra_fence else None
                            versions[extra_kind, extra_id] = (extra_row or {}).get('item', {}).get('activity_generation', 0)
                    pending.extend(row for row in rows if row not in processed)
                else:
                    media_holds.hold(db, keys, rows)
            engine.remove(target_id, timeout=0.5)
        except ValueError as exc:
            if getattr(exc, 'status', 400) != 404:
                stopping = True
        except (OSError, RuntimeError):
            stopping = True
    return _ack(store, kind, identity, cleared, held, stopping)


def clear_one(app, store, kind, identity, observed=None):
    if kind not in KINDS or type(identity) not in (str, int) or not 1 <= len(str(identity)) <= 500:
        raise ValueError('Choose an Activity item.')
    # Serialise explicit Clears only, not workers or ordinary Activity reads.
    lock = app.extensions.setdefault('activity_item_clear_lock', threading.RLock())
    with media_holds.explicit_action(store):
        with lock:
            return _clear_one(app, store, kind, str(identity), observed)
