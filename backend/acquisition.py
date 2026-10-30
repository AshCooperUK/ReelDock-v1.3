"""One hold for every download source.

Stop all downloads writes this hold before it enumerates work. Enqueue and
final-start paths refuse new work until Resume downloads clears the hold.
The generation only increases, so a restart cannot revive a stopped job.
"""
import json
import os
import sqlite3
import time
from pathlib import Path

HOLD_NAME = 'acquisition-hold.json'
ACTIVE_DISCOVER = ('searching', 'starting', 'waiting', 'queued', 'downloading', 'verifying', 'paused', 'cancelling')
ACTIVE_NZB = {'queued', 'download', 'paused', 'choose'}
ACTIVE_TORRENT = {'download', 'queued', 'paused', 'seed', 'choose'}


class AcquisitionHeld(ValueError):
    pass


def hold_path(directory):
    return Path(directory) / HOLD_NAME


def file_held(directory):
    for candidate in (hold_path(directory), Path(directory).parent / HOLD_NAME):
        try:
            payload = json.loads(Path(candidate).read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get('hold'):
            return True
    return False


def _write_file(store, hold, generation):
    path = hold_path(store.directory)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps({'hold': bool(hold), 'generation': int(generation)}), encoding='utf-8')
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def held(store):
    if store.get('acquisition_hold') == '1':
        return True
    return file_held(store.directory)


def generation(store):
    try:
        return int(store.get('acquisition_generation') or '0')
    except (TypeError, ValueError):
        return 0


def ensure_open(store):
    if held(store):
        raise AcquisitionHeld('Downloads are stopped. Resume downloads in Activity before starting another.')


def begin_hold(store):
    """Persist the hold before any source is enumerated."""
    value = generation(store) + 1
    _write_file(store, True, value)
    store.set('acquisition_generation', str(value))
    store.set('acquisition_hold', '1')
    return value


def resume(store):
    value = generation(store)
    store.set('acquisition_hold', '0')
    _write_file(store, False, value)
    return value


def settle_after_restart(store):
    """A held generation stays cancelled across a worker or web restart."""
    if not held(store):
        return False
    now = time.time()
    with store.connect() as db:
        db.execute(
            "UPDATE downloads SET state='cancelled', message='Cancelled. Saved files were kept.', updated=? "
            "WHERE state IN ('queued','downloading','cancelling')",
            (now,))
        try:
            db.execute(
                "UPDATE m3u_downloads SET state='cancelled', message='Cancelled. Saved files were kept.', updated=? "
                "WHERE state IN ('queued','running','cancelling')",
                (now,))
        except sqlite3.OperationalError:
            pass
        try:
            db.execute(
                "UPDATE discover_requests SET state='cancelled', message='Cancelled. Saved files were kept.', updated=? "
                "WHERE state IN ('searching','starting','waiting','queued','downloading','verifying','paused','cancelling')",
                (now,))
        except sqlite3.OperationalError:
            pass
    return True


def _source(stopped=0, pending=0, errors=None):
    return {'stopped': stopped, 'pending': pending, 'errors': list(errors or [])}


def _wait_rows(store, query, arguments, timeout):
    deadline = time.time() + timeout
    pending = 0
    while True:
        with store.connect() as db:
            try:
                pending = db.execute(query, arguments).fetchone()[0]
            except sqlite3.OperationalError:
                return 0
        if not pending or time.time() >= deadline:
            return pending
        time.sleep(0.2)


def _stop_downloads(store):
    now = time.time()
    try:
        with store.connect() as db:
            queued = db.execute(
                "UPDATE downloads SET state='cancelled', message='Cancelled. Saved files were kept.', updated=? WHERE state='queued'",
                (now,)).rowcount
            running = db.execute(
                "UPDATE downloads SET state='cancelling', message='Cancelling download...', updated=? WHERE state='downloading'",
                (now,)).rowcount
    except sqlite3.OperationalError:
        return _source()
    pending = _wait_rows(
        store,
        "SELECT count(*) FROM downloads WHERE state IN ('downloading','cancelling')",
        (),
        12,
    )
    return _source(stopped=queued + running - pending, pending=pending, errors=['The YouTube worker has not confirmed every stop.'] if pending else [])


def _stop_m3u(store):
    now = time.time()
    try:
        with store.connect() as db:
            queued = db.execute(
                "UPDATE m3u_downloads SET state='cancelled', message='Cancelled. Saved files were kept.', updated=? WHERE state='queued'",
                (now,)).rowcount
            running = db.execute(
                "UPDATE m3u_downloads SET state='cancelling', updated=? WHERE state='running'",
                (now,)).rowcount
    except sqlite3.OperationalError:
        return _source()
    pending = _wait_rows(
        store,
        "SELECT count(*) FROM m3u_downloads WHERE state IN ('running','cancelling')",
        (),
        12,
    )
    return _source(stopped=queued + running - pending, pending=pending, errors=['An M3U transfer has not confirmed its stop.'] if pending else [])


def _engine(app, store, kind):
    if kind == 'nzb':
        from .nzb import _engine as open_engine
    else:
        from .torrents import _engine as open_engine
    return open_engine(app, store)


def _stop_nzb(app, store):
    from .storage_layout import download_base
    if not app.extensions.get('nzb_engine') and not download_base(store):
        return _source()
    try:
        engine = _engine(app, store, 'nzb')
        adopt = getattr(engine, '_adopt', None)
        if callable(adopt):
            adopt() # Include worker-persisted jobs before the next scheduler tick.
    except Exception as exc:
        from .discover_logic import redact
        return _source(errors=[redact(exc)])
    stopped = 0
    pending = 0
    errors = []
    for job in list(engine.jobs()):
        if job.get('state') not in {'queued', 'downloading', 'paused', 'choose'}:
            continue
        try:
            engine.remove(job['id'], timeout=20)
            stopped += 1
        except Exception as exc:
            if 'Unknown' in str(exc):
                stopped += 1
                continue
            pending += 1
            from .discover_logic import redact
            errors.append(redact(exc) or 'An NZB is still stopping.')
    return _source(stopped, pending, errors)


def _stop_torrents(app, store):
    from .storage_layout import download_base
    if not app.extensions.get('torrent_engine') and not download_base(store):
        return _source()
    try:
        engine = _engine(app, store, 'torrent')
    except Exception as exc:
        from .discover_logic import redact
        return _source(errors=[redact(exc)])
    for preview in list(getattr(engine, '_previews', {}).values()):
        done = getattr(preview, 'done', None)
        if done is not None:
            done.set()
    stopped = 0
    pending = 0
    errors = []
    for job in list(engine.jobs()):
        if job.get('state') not in {'queued', 'downloading', 'paused', 'seeding', 'choose'}:
            continue
        try:
            engine.remove(job['id'], timeout=20)
            stopped += 1
        except Exception as exc:
            if 'Unknown' in str(exc):
                stopped += 1
                continue
            pending += 1
            from .discover_logic import redact
            errors.append(redact(exc) or 'A torrent is still stopping.')
    return _source(stopped, pending, errors)


def cancel_discover(app, store, request_id):
    """Mark one request cancelling, stop its engine, then record cancelled."""
    from .discover_store import cancel_request, initialise
    initialise(store)
    changed, row = cancel_request(store, request_id)
    if row is None:
        return {'ok': False, 'error': 'Unknown request.', 'status': 404}
    if row.get('state') == 'complete' or (not changed and row.get('state') == 'cancelled'):
        return {'ok': True, 'state': row.get('state'), 'message': row.get('message') or ''}
    errors = []
    engine_name = row.get('engine') or ''
    engine_id = row.get('engine_id') or ''
    if engine_name == 'nzb' and engine_id:
        errors.extend(_remove_named(app, store, 'nzb', engine_id))
    elif engine_name == 'torrent' and engine_id:
        errors.extend(_remove_named(app, store, 'torrent', engine_id))
    if engine_name=='vod' and engine_id:
        with store.connect() as db:
            db.execute("UPDATE m3u_downloads SET state=CASE WHEN state='queued' THEN 'cancelled' ELSE 'cancelling' END,updated=? WHERE id=? AND state IN ('queued','running')",(time.time(),engine_id))
    video_id = ''
    try:
        video_id = json.loads(row.get('body') or '{}').get('videoId') or ''
    except json.JSONDecodeError:
        video_id = ''
    if video_id:
        with store.connect() as db:
            db.execute(
                "UPDATE downloads SET state=CASE WHEN state='queued' THEN 'cancelled' ELSE 'cancelling' END, updated=? "
                "WHERE id=? AND state IN ('queued','downloading')",
                (time.time(), video_id))
    if errors:
        return {'ok': False, 'state': 'cancelling', 'errors': errors, 'status': 409}
    now = time.time()
    with store.connect() as db:
        db.execute(
            "UPDATE discover_requests SET state='cancelled', message='Cancelled. Saved files were kept.', updated=? "
            "WHERE id=? AND state='cancelling'",
            (now, request_id))
    return {'ok': True, 'state': 'cancelled', 'message': 'Cancelled. Saved files were kept.'}


def _remove_named(app, store, kind, job_id):
    try:
        engine = _engine(app, store, kind)
        engine.remove(job_id, timeout=20)
    except Exception as exc:
        text = str(exc)
        if 'Unknown' in text:
            return []
        from .discover_logic import redact
        return [redact(exc) or 'The download is still stopping.']
    return []


def _stop_discover(app, store):
    from .discover_store import initialise
    initialise(store)
    with store.connect() as db:
        try:
            rows = db.execute(
                f"SELECT id FROM discover_requests WHERE state IN ({','.join('?' for _ in ACTIVE_DISCOVER)})",
                ACTIVE_DISCOVER,
            ).fetchall()
        except sqlite3.OperationalError:
            return _source()
    stopped = 0
    pending = 0
    errors = []
    for row in rows:
        result = cancel_discover(app, store, row['id'])
        if result.get('ok'):
            stopped += 1
        else:
            pending += 1
            errors.extend(result.get('errors') or [result.get('error') or 'A Discover request is still stopping.'])
    return _source(stopped, pending, errors)


def stop_all(app, store):
    """Hold acquisition, then stop every owning engine. Partial failure stays visible."""
    value = begin_hold(store)
    sources = {
        'discover': _stop_discover(app, store),
        'youtube': _stop_downloads(store),
        'm3u': _stop_m3u(store),
        'nzb': _stop_nzb(app, store),
        'torrent': _stop_torrents(app, store),
    }
    pending = sum(item['pending'] for item in sources.values())
    errors = [f'{name}: {error}' for name, item in sources.items() for error in item['errors']]
    acknowledged = pending == 0 and not errors
    if acknowledged:
        message = 'All stopped.'
    elif pending:
        message = 'Stopping downloads… ' + ', '.join(f'{name} {item["pending"]} still stopping' for name, item in sources.items() if item['pending'])
    else:
        message = 'Stopping downloads… ' + '; '.join(errors)
    from .activity import record
    record(store, 'acquisition-stop', 'Stop all downloads', 'complete' if acknowledged else 'failed', message[:500])
    return {
        'held': True,
        'generation': value,
        'acknowledged': acknowledged,
        'sources': sources,
        'message': message,
    }
