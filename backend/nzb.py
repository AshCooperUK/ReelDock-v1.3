"""NZB downloads inside ReelDock: fetch articles, then rename."""
import hmac
import threading
import time
from pathlib import Path

from flask import jsonify, request, session
from werkzeug.exceptions import RequestEntityTooLarge

from .downloaders.nzb.engine import EngineError, NzbEngine
from .storage_layout import download_base
from .operations import Halted, Operations

NZB_FILE_LIMIT = 20_000_000


def stop(app):
    event = app.extensions.get('auto_nzb_stop')
    if event is not None:
        event.set()
    operations = app.extensions.get('nzb_operations')
    engine = app.extensions.get('nzb_engine')
    if operations is not None:
        operations.halt()
        return
    if engine is not None:
        engine.shutdown()


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
        raise EngineError('Choose a ReelDock directory before downloading NZBs.', 409)
    base = Path(saved).absolute()
    if base.is_symlink() or not base.is_dir():
        raise EngineError('The ReelDock directory is not available.', 409)
    lock = app.extensions.setdefault('nzb_lock', threading.Lock())
    with lock:
        engine = app.extensions.get('nzb_engine')
        if engine is not None and not engine.stop.is_set() and Path(engine.media_base) == base:
            return _attach(engine, store, base)
        if engine is not None and not engine.stop.is_set():
            engine.shutdown()
        from .download_control import reader
        operations = Operations(str(Path(store.directory) / 'nzb.lock'))
        engine = NzbEngine(base, Path(store.directory) / 'nzb-engine', operations, global_paused=reader(store, 'nzb'))
        app.extensions['nzb_operations'] = operations
        app.extensions['nzb_engine'] = engine
        return _attach(engine, store, base)


def _attach(engine, store, base):
    engine.layout = lambda: _library_layout(store)
    engine.metadata = lambda job, created: _metadata(store, base, job, created)
    engine.metadata_ready = lambda: bool(store.get('m3u_tmdb_token'))
    return engine


def _csrf_ok():
    token = session.get('csrf') or ''
    return bool(token) and hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), token)


def _body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise EngineError('Send a JSON object.')
    return body


def _limited(app, key, maximum):
    now = time.monotonic()
    hits = app.extensions.setdefault(key, [])
    fresh = [stamp for stamp in hits if now - stamp < 60]
    if len(fresh) >= maximum:
        hits[:] = fresh
        return False
    fresh.append(now)
    hits[:] = fresh
    return True


def _upload():
    request.max_content_length = NZB_FILE_LIMIT + 16_384
    upload = request.files.get('nzb')
    if upload is None:
        raise EngineError('Choose an NZB file.')
    name = Path(str(upload.filename or '')).name
    if not name or len(name) > 180 or not name.lower().endswith('.nzb'):
        raise EngineError('Choose an NZB file.')
    data = upload.read(NZB_FILE_LIMIT + 1)
    if not data:
        raise EngineError('That NZB file is empty.')
    if len(data) > NZB_FILE_LIMIT:
        raise EngineError('Choose an NZB file smaller than 20 MB.')
    return data, name


def _with_found_title(store, engine, payload):
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
        if name:
            from .downloaders.nzb.engine import _with_episode
            name = _with_episode(name, path)
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


def _require(engine, job_id):
    job = engine._jobs.get(job_id)
    if job is None or job.removed:
        raise EngineError('Unknown NZB', 404)
    return job


def _lookup_path(job) -> str:
    """Use the release name when the video filename does not contain the episode."""
    from .downloaders.nzb.engine import _described, _name_for_library
    from .downloaders.torrent.media import classify_path

    videos = []
    for item in getattr(job, 'files', ()) or ():
        if classify_path(getattr(item, 'name', ''))[0] != 'video':
            continue
        videos.append((sum(int(segment.size or 0) for segment in item.segments), item.name))
    video = max(videos)[1] if videos else ''
    release = (getattr(job, 'name', '') or '').strip()
    if video and _described(video):
        return video
    if release and _described(release):
        return _name_for_library(release, video or release + '.mkv')
    if video:
        return video
    title = (getattr(job, 'save_as', '') or release or 'video').strip()
    return title if Path(title).suffix else title + '.mkv'


def register(app, store, access):
    def open_engine():
        try:
            return _engine(app, store), None
        except Halted as exc:
            return None, (jsonify(error=str(exc)), 409)
        except EngineError as exc:
            return None, (jsonify(error=str(exc)), exc.status)

    @app.post('/api/nzb/preview')
    def nzb_preview():
        denied = access(local_action=True)
        if denied:
            return denied
        if not _csrf_ok():
            return jsonify(error='Reload the page.'), 403
        if not _limited(app, 'nzb_preview_hits', 30):
            return jsonify(error='Too many NZB lookups. Wait a moment.'), 429
        engine, failure = open_engine()
        if failure:
            return failure
        try:
            if request.mimetype == 'multipart/form-data':
                data, name = _upload()
                payload = engine.preview_bytes(data, name)
            else:
                body = _body()
                payload = engine.preview(str(body.get('url') or ''))
            return jsonify(_with_found_title(store, engine, payload))
        except RequestEntityTooLarge:
            return jsonify(error='Choose an NZB file smaller than 20 MB.'), 413
        except Halted as exc:
            return jsonify(error=str(exc)), 409
        except EngineError as exc:
            return jsonify(error=str(exc)), exc.status

    @app.get('/api/nzb/preview/<preview_id>/lookup')
    def nzb_preview_lookup(preview_id):
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

    @app.get('/api/nzb/preview/<preview_id>')
    def nzb_preview_status(preview_id):
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

    @app.route('/api/nzb/settings', methods=['GET', 'POST'])
    def nzb_settings():
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
            return jsonify(engine.update_settings(_body()))
        except EngineError as exc:
            return jsonify(error=str(exc)), exc.status

    @app.route('/api/nzb', methods=['GET', 'POST'])
    def nzb_jobs():
        denied = access(local_action=True)
        if denied:
            return denied
        if request.method == 'GET' and not download_base(store):
            return jsonify(nzbs=[])
        engine, failure = open_engine()
        if failure:
            return failure
        if request.method == 'GET':
            from .activity_media import native_items
            return jsonify(nzbs=native_items(store,'nzb',engine.jobs()))
        if not _csrf_ok():
            return jsonify(error='Reload the page.'), 403
        try:
            body = _body()
            if body.get('action') != 'start':
                raise EngineError('Choose a scanned NZB to start.')
            files = body.get('files')
            if files is not None and not isinstance(files, list):
                raise EngineError('Choose files from the scan.')
            title = body.get('title')
            if title is not None and not isinstance(title, str):
                raise EngineError('Enter a name for the saved files.')
            from .media_holds import resume
            resume(store,engine.preview_activity_key(str(body.get('preview_id') or ''),files))
            return jsonify(engine.start(str(body.get('preview_id') or ''), files, title))
        except Halted as exc:
            return jsonify(error=str(exc)), 409
        except EngineError as exc:
            return jsonify(error=str(exc)), exc.status

    @app.post('/api/nzb/<job_id>')
    def nzb_job(job_id):
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
                resume(store,[engine.activity_key(job_id),'native:nzb-job:'+job_id])
                return jsonify(engine.update(job_id, {'paused': False}))
            if action == 'remove':
                engine.remove(job_id)
                return jsonify(ok=True)
            if action == 'placement':
                return jsonify(engine.update(job_id, {'placement': {'index': body.get('index'), 'value': body.get('category')}}))
            raise EngineError('Choose pause, resume, remove, or where to save.')
        except EngineError as exc:
            return jsonify(error=str(exc)), exc.status

    @app.get('/api/nzb/<job_id>/lookup')
    def nzb_lookup(job_id):
        denied = access(local_action=True)
        if denied:
            return denied
        engine, failure = open_engine()
        if failure:
            return failure
        try:
            from .downloaders.torrent.metadata import preview, scan_lookup
            job = _require(engine, job_id)
            from .activity_media import lookup_item
            if job.meta is None:
                from .downloaders.torrent.rename import release_save_as
                title = release_save_as((job.save_as or job.name or '').strip())
                return jsonify(lookup_item(store,'nzb',job_id,scan_lookup(store, _lookup_path(job), title)))
            return jsonify(lookup_item(store,'nzb',job_id,preview(store, job)))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except EngineError as exc:
            return jsonify(error=str(exc)), exc.status

    @app.route('/api/nzb/<job_id>/match', methods=['GET', 'POST'])
    def nzb_match(job_id):
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
        try:
            _engine(app, store)
        except (EngineError, Halted):
            pass
        event = app.extensions.setdefault('auto_nzb_stop', threading.Event())
        if not app.extensions.get('auto_nzb_thread'):
            from .auto_downloads import loop as auto_loop
            thread = threading.Thread(target=auto_loop, args=(app, store, event, {'nzb'}), daemon=True, name='auto-nzb')
            app.extensions['auto_nzb_thread'] = thread
            thread.start()
