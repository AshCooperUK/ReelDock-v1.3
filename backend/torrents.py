"""Torrent downloads inside ReelDock: scan, rename into Downloads, then manage."""
import hmac
import threading
import time
from pathlib import Path

from flask import jsonify, request, session
from werkzeug.exceptions import RequestEntityTooLarge

from .downloaders.torrent.engine import EngineError, TorrentEngine
from .storage_layout import download_base
from .operations import Halted, Operations


def stop(app):
    """Stop the in-process engine. Safe when torrents were never started."""
    event = app.extensions.get('auto_download_stop')
    if event is not None:
        event.set()
    operations = app.extensions.get('torrent_operations')
    engine = app.extensions.get('torrent_engine')
    if operations is not None:
        operations.halt()
        return
    if engine is not None:
        engine.shutdown()


def _quiet_settings():
    return {
        'enable_dht': False,
        'enable_lsd': False,
        'encryption': 'disabled',
        'enable_upnp': False,
        'enable_natpmp': False,
        'listen_port': 0,
    }


def _metadata(store, base, job, created):
    from .downloaders.torrent.metadata import schedule
    schedule(store, base, job, created)


def _library_layout(store):
    from .storage_layout import names
    profile = store.get('metadata_type', 'emby') or 'emby'
    if profile not in ('emby', 'plex', 'kodi'):
        profile = 'emby'
    values = names(store)
    return {'profile': profile, 'movies': values['movies'], 'series': values['series']}


def _engine(app, store):
    if store.get('reset_in_progress') == '1':
        raise EngineError('ReelDock is stopping work for a reset.', 409)
    saved = download_base(store)
    if not saved:
        raise EngineError('Choose a ReelDock directory before downloading torrents.', 409)
    base = Path(saved).absolute()
    if base.is_symlink() or not base.is_dir():
        raise EngineError('The ReelDock directory is not available.', 409)
    lock = app.extensions.setdefault('torrent_lock', threading.Lock())
    with lock:
        engine = app.extensions.get('torrent_engine')
        if engine is not None and not engine.stop.is_set() and Path(engine.media_base) == base:
            return _attach(engine, store, base)
        if engine is not None and not engine.stop.is_set():
            engine.shutdown()
        from .download_control import reader
        operations = Operations(str(Path(store.directory) / 'torrent.lock'))
        engine = TorrentEngine(
            base,
            Path(store.directory) / 'torrent-engine',
            operations,
            _quiet_settings() if app.testing else None,
            global_paused=reader(store, 'torrent'),
        )
        app.extensions['torrent_operations'] = operations
        app.extensions['torrent_engine'] = engine
        return _attach(engine, store, base)


def _attach(engine, store, base):
    from .downloaders.torrent.metadata import catch_up
    engine.layout = lambda: _library_layout(store)
    engine.metadata = lambda job, created: _metadata(store, base, job, created)
    engine.metadata_resume = lambda: catch_up(store, base, engine)
    catch_up(store, base, engine)
    return engine


def _preview_allowed(app):
    now = time.monotonic()
    hits = app.extensions.setdefault('torrent_preview_hits', [])
    fresh = [stamp for stamp in hits if now - stamp < 60]
    if len(fresh) >= 30:
        hits[:] = fresh
        return False
    fresh.append(now)
    hits[:] = fresh
    return True


def _csrf_ok():
    token = session.get('csrf') or ''
    return bool(token) and hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), token)


TORRENT_FILE_LIMIT = 2_000_000


def _torrent_upload():
    """Read one uploaded .torrent. The name is a label only and is never used as a path."""
    request.max_content_length = TORRENT_FILE_LIMIT + 16_384
    upload = request.files.get('torrent')
    if upload is None:
        raise EngineError('Choose a .torrent file.')
    name = Path(str(upload.filename or '')).name
    if not name or len(name) > 180 or not name.lower().endswith('.torrent'):
        raise EngineError('Choose a .torrent file.')
    data = upload.read(TORRENT_FILE_LIMIT + 1)
    if not data:
        raise EngineError('That .torrent file is empty.')
    if len(data) > TORRENT_FILE_LIMIT:
        raise EngineError('Choose a .torrent file smaller than 2 MB.')
    return data, name


def _require(engine, job_id):
    job = engine._jobs.get(job_id)
    if job is None or job.removed or job.meta is None:
        raise EngineError('Unknown torrent', 404)
    return job


def _with_found_title(store, engine, payload):
    """Use a found show or movie title when no library folder already matches."""
    if not isinstance(payload, dict) or payload.get('status') != 'ready' or not payload.get('accepted'):
        return payload
    if payload.get('suggested_folder'):
        return payload
    preview = engine._previews.get(payload.get('id') or '')
    if preview is not None and preview.found_checked:
        if preview.found_name:
            payload = dict(payload)
            payload['suggested_name'] = preview.found_name
        return payload
    try:
        from .downloaders.torrent.metadata import matched_save_as, scan_lookup
        path, _current = engine.preview_video(payload.get('id') or '')
        name = matched_save_as(scan_lookup(store, path))
    except (EngineError, ValueError, OSError):
        name = ''
    if preview is not None:
        preview.found_checked = True
        preview.found_name = name
    if not name:
        return payload
    payload = dict(payload)
    payload['suggested_name'] = name
    return payload


def _body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise EngineError('Send a JSON object.')
    return body


def register(app, store, access):
    def open_engine():
        try:
            return _engine(app, store), None
        except Halted as exc:
            return None, (jsonify(error=str(exc)), 409)
        except EngineError as exc:
            return None, (jsonify(error=str(exc)), exc.status)

    @app.post('/api/torrents/preview')
    def torrent_preview():
        denied = access(local_action=True)
        if denied:
            return denied
        if not _csrf_ok():
            return jsonify(error='Reload the page.'), 403
        if not _preview_allowed(app):
            return jsonify(error='Too many torrent lookups. Wait a moment.'), 429
        engine, failure = open_engine()
        if failure:
            return failure
        try:
            if request.mimetype == 'multipart/form-data':
                data, name = _torrent_upload()
                payload = engine.preview_bytes(data, name)
            else:
                body = _body()
                source = str(body.get('url') or body.get('magnet') or '')
                payload = engine.preview(source)
            return jsonify(_with_found_title(store, engine, payload))
        except RequestEntityTooLarge:
            return jsonify(error='Choose a .torrent file smaller than 2 MB.'), 413
        except Halted as exc:
            return jsonify(error=str(exc)), 409
        except EngineError as exc:
            return jsonify(error=str(exc)), exc.status

    @app.get('/api/torrents/preview/<preview_id>/lookup')
    def torrent_preview_lookup(preview_id):
        denied = access(local_action=True)
        if denied:
            return denied
        engine, failure = open_engine()
        if failure:
            return failure
        try:
            from .downloaders.torrent.metadata import scan_lookup
            path, save_as = engine.preview_video(preview_id)
            return jsonify(scan_lookup(store, path, save_as))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except EngineError as exc:
            return jsonify(error=str(exc)), exc.status

    @app.get('/api/torrents/preview/<preview_id>')
    def torrent_preview_status(preview_id):
        denied = access(local_action=True)
        if denied:
            return denied
        engine, failure = open_engine()
        if failure:
            return failure
        try:
            return jsonify(_with_found_title(store, engine, engine.preview_status(preview_id)))
        except EngineError as exc:
            return jsonify(error=str(exc)), exc.status

    @app.route('/api/torrents/settings', methods=['GET', 'POST'])
    def torrent_settings():
        denied = access(local_action=True)
        if denied:
            return denied
        engine, failure = open_engine()
        if failure:
            return failure
        if request.method == 'GET':
            return jsonify(engine.settings())
        if not _csrf_ok():
            return jsonify(error='Reload the page.'), 403
        try:
            body = _body()
            body.pop('theme', None)
            return jsonify(engine.update_settings(body))
        except EngineError as exc:
            return jsonify(error=str(exc)), exc.status

    @app.route('/api/torrents', methods=['GET', 'POST'])
    def torrent_jobs():
        denied = access(local_action=True)
        if denied:
            return denied
        if request.method == 'GET' and not download_base(store):
            return jsonify(torrents=[])
        engine, failure = open_engine()
        if failure:
            return failure
        if request.method == 'GET':
            from .activity_media import native_items
            return jsonify(torrents=native_items(store,'torrent',engine.jobs()))
        if not _csrf_ok():
            return jsonify(error='Reload the page.'), 403
        try:
            body = _body()
            if body.get('action') != 'start':
                raise EngineError('Choose a scanned torrent to start.')
            files = body.get('files')
            if files is not None and not isinstance(files, list):
                raise EngineError('Choose media files from the scan.')
            title = body.get('title')
            if title is not None and not isinstance(title, str):
                raise EngineError('Enter a name for the saved files.')
            from .media_holds import resume
            key=engine.preview_activity_key(str(body.get('preview_id') or ''),files)
            resume(store,[key,'native:torrent-job:'+key.rsplit(':',1)[-1]])
            return jsonify(engine.start(str(body.get('preview_id') or ''), files, title))
        except Halted as exc:
            return jsonify(error=str(exc)), 409
        except EngineError as exc:
            return jsonify(error=str(exc)), exc.status

    @app.post('/api/torrents/<job_id>')
    def torrent_job(job_id):
        denied = access(local_action=True)
        if denied:
            return denied
        if not _csrf_ok():
            return jsonify(error='Reload the page.'), 403
        engine, failure = open_engine()
        if failure:
            return failure
        try:
            body = _body()
            action = body.get('action')
            if action == 'pause':
                return jsonify(engine.update(job_id, {'paused': True}))
            if action == 'resume':
                from .media_holds import resume
                resume(store,[engine.activity_key(job_id),'native:torrent-job:'+job_id])
                return jsonify(engine.update(job_id, {'paused': False}))
            if action == 'remove':
                engine.remove(job_id)
                return jsonify(ok=True)
            if action == 'trackers':
                return jsonify(engine.update(job_id, {'trackers': body.get('trackers')}))
            if action == 'placement':
                return jsonify(engine.update(job_id, {'placement': {'index': body.get('index'), 'value': body.get('category')}}))
            if action == 'limits':
                patch = {key: body[key] for key in ('download_limit_bps', 'upload_limit_bps', 'share_ratio_limit', 'enable_dht', 'enable_pex', 'enable_lsd', 'enable_web_seeds') if key in body}
                return jsonify(engine.update(job_id, patch))
            raise EngineError('Choose pause, resume, remove, trackers, or limits.')
        except EngineError as exc:
            return jsonify(error=str(exc)), exc.status

    @app.get('/api/torrents/<job_id>/lookup')
    def torrent_lookup(job_id):
        denied = access(local_action=True)
        if denied:
            return denied
        engine, failure = open_engine()
        if failure:
            return failure
        try:
            from .downloaders.torrent.metadata import preview
            from .activity_media import lookup_item
            return jsonify(lookup_item(store,'torrent',job_id,preview(store, _require(engine, job_id))))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except EngineError as exc:
            return jsonify(error=str(exc)), exc.status

    @app.route('/api/torrents/<job_id>/match', methods=['GET', 'POST'])
    def torrent_match(job_id):
        denied = access(local_action=True)
        if denied:
            return denied
        engine, failure = open_engine()
        if failure:
            return failure
        try:
            from .downloaders.torrent.metadata import apply_match, match_state, primary_video
            job = _require(engine, job_id)
            if request.method == 'POST':
                if not _csrf_ok():
                    return jsonify(error='Reload the page.'), 403
                body = _body()
                raw = body.get('path')
            else:
                body = {}
                raw = request.args.get('path')
            index = primary_video(job).index if raw in (None, '') else int(raw)
            if request.method == 'GET':
                return jsonify(match_state(store, job, index))
            return jsonify(apply_match(store, Path(engine.media_base), job, index, str(body.get('action') or ''), body))
        except (TypeError, ValueError) as exc:
            return jsonify(error=str(exc) or 'Choose a match action.'), 400
        except EngineError as exc:
            return jsonify(error=str(exc)), exc.status

    if not app.testing and app.config.get('PROCESS_ROLE') != 'worker':
        event = app.extensions.setdefault('auto_download_stop', threading.Event())
        if not app.extensions.get('auto_download_thread'):
            from .auto_downloads import loop as auto_loop
            thread = threading.Thread(target=auto_loop, args=(app, store, event, {'torrent'}), daemon=True, name='auto-torrents')
            app.extensions['auto_download_thread'] = thread
            thread.start()
        if download_base(store):
            try:
                _engine(app, store)
            except (EngineError, Halted, OSError):
                app.logger.exception('Torrent engine did not start')
