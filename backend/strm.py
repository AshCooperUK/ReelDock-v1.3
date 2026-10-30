"""Explicit, private uploads of single direct-video pointers.

Preview is stateless and offline. Only confirmed Start persists the pointer;
the existing server-download worker owns its transfer and cancellation.
"""
import hashlib
import hmac
import ipaddress
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from flask import jsonify, request, session
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from . import m3u

MAX_BYTES = 16 * 1024
MANUAL_PATH = 'media.strm'
DIRECT_ONLY = 'Use one direct HTTP or HTTPS video URL. YouTube, live streams and streaming playlists are not supported.'


def validate_url(value):
    """Syntax checks only; public DNS and every redirect are pinned at transfer."""
    try:
        if not isinstance(value, str) or '|' in value:
            raise ValueError()
        value = m3u.validate_url(value)
        parsed = urlsplit(value)
        host = parsed.hostname.lower().rstrip('.')
        if (host in ('localhost', 'youtu.be', 'youtube.com', 'youtube-nocookie.com')
                or host.endswith(('.localhost', '.local', '.youtube.com', '.youtu.be', '.youtube-nocookie.com'))
                or Path(parsed.path).suffix.lower() in ('.m3u8', '.m3u', '.mpd')):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        # Never expose the uploaded URL, provider credentials or parser text.
        raise ValueError(DIRECT_ONLY) from None


def parse(raw, filename):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Choose a non-empty STRM file no larger than 16 KiB.')
    if not isinstance(filename, str) or len(filename) > 255 or not filename.lower().endswith('.strm'):
        raise ValueError('Choose a .strm file.')
    name = secure_filename(filename.replace('\\', '/').split('/')[-1])
    if not name or not name.lower().endswith('.strm'):
        raise ValueError('Choose a .strm file with a valid filename.')
    try:
        lines = [line.strip() for line in raw.decode('utf-8-sig').splitlines() if line.strip()]
    except UnicodeError:
        raise ValueError('Save the STRM file as UTF-8 text with one direct video URL.') from None
    if len(lines) != 1:
        raise ValueError(DIRECT_ONLY)
    url = validate_url(lines[0])
    title = m3u.filename(Path(name).stem.replace('_', ' ').replace('.', ' '))
    kind = 'series' if re.search(r'(?i)\bS\d{1,3}E\d{1,4}\b', title) else 'movies'
    return dict(name=name, title=title, kind=kind, url=url)


def _digest(app, purpose, value):
    key = app.secret_key.encode() if isinstance(app.secret_key, str) else app.secret_key
    return hmac.new(key, purpose + b'\0' + value, hashlib.sha256).hexdigest()


def fingerprint(app, raw, filename):
    return _digest(app, b'strm-source-v1', filename.encode('utf-8') + b'\0' + raw)


def preview_id(app, digest):
    # A different signed-in session/account revision cannot reuse a preview.
    owner = '\0'.join(str(session.get(key, '')) for key in ('user_id', 'auth_revision', 'csrf'))
    return _digest(app, b'strm-preview-v1', (owner + '\0' + digest).encode('utf-8'))


def lookup(store, source, path):
    if path != MANUAL_PATH:
        raise ValueError('This uploaded STRM source is unavailable.')
    with store.connect() as db:
        saved = db.execute('SELECT * FROM strm_sources WHERE id=?', (source,)).fetchone()
    if not saved:
        raise ValueError('This uploaded STRM source is unavailable.')
    return dict(source=source, path=path, title=saved['title'], kind=saved['kind'], folder='', sourceType='strm'), validate_url(saved['url'])


def public_metadata(db, source):
    if not isinstance(source, str) or not source.startswith('strm:'):
        return None
    saved = db.execute('SELECT name FROM strm_sources WHERE id=?', (source,)).fetchone()
    # Even an orphaned manual row must never fabricate a playlist/artwork URL.
    return dict(sourceType='strm', sourceName=saved['name'] if saved else 'Uploaded STRM file',
                subscription='STRM', artwork='', description='Media downloaded from an uploaded STRM file.')


def destination(title, kind):
    """Only organise seasons when the confirmed title supplies that identity."""
    title = m3u.filename(title)
    episode = re.search(r'(?i)^(.+?)\s+S(\d{1,3})E\d{1,4}(?:\b|\s)', title)
    if kind == 'series' and episode:
        return Path(m3u.filename(episode[1])) / f'Season {int(episode[2]):02d}' / title
    return Path(title)


def validate_video_response(mime, prefix):
    """A video-looking URL must not publish an error page or manifest."""
    opening = prefix.lstrip().lower()
    if (mime.startswith('text/') or mime in ('application/json', 'application/xml', 'application/dash+xml',
            'application/vnd.apple.mpegurl', 'application/x-mpegurl')
            or opening.startswith((b'<!doctype', b'<html', b'<?xml', b'<mpd', b'{', b'[', b'#extm3u'))):
        raise ValueError('This source is not a supported video file.')


def transfer_error(exc):
    # Provider-controlled headers can raise ValueError containing private text.
    # Only fixed messages from the shared transfer are suitable for Activity.
    safe = {
        'This uploaded STRM source is unavailable.', DIRECT_ONLY,
        'The provider could not supply this video. Try again later.',
        'This source is not a supported video file.',
        'A file already exists at the destination; it was kept.',
        'This video is outside the VOD download size range.',
        'There is not enough free space for this download.',
        'The temporary download path is unavailable.',
        'This video exceeds the VOD maximum download size.',
        'The provider ended the download early. Retry the download.',
        'The provider returned an empty file.',
        'This video is below the VOD minimum download size.',
        'Library paths must not contain symbolic links.',
        'Unsafe library path; import stopped.',
        'The stream redirected too many times.',
        'The hostname could not be found. Check its spelling and Cloudflare DNS route, then retry.',
        'The hostname must resolve to public addresses. Check its Cloudflare DNS route.',
    }
    message = str(exc)
    return message if message in safe else 'The video could not be downloaded. Check the source and destination, then retry.'


def _validated_title(title,kind):
    if not isinstance(title, str) or not title.strip() or len(title) > 140 or any(ord(c) < 32 for c in title):
        raise ValueError('Enter a title up to 140 characters.')
    if kind not in ('movies', 'series'):
        raise ValueError('Choose Movie or TV episode.')
    title = m3u.filename(title)
    return title


def start(app, store, parsed, digest, title, kind):
    from .acquisition import ensure_open
    from .m3u_downloads import enqueue_resolved
    ensure_open(store)
    title=_validated_title(title,kind)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT * FROM strm_sources WHERE fingerprint=?', (digest,)).fetchone()
        source = previous['id'] if previous else 'strm:' + secrets.token_hex(16)
        old = db.execute('SELECT id,state FROM m3u_downloads WHERE source=? AND path=?', (source, MANUAL_PATH)).fetchone()
        from .media_holds import blocked_in,vod_key
        from .m3u_downloads import CLEARED_MESSAGE
        if blocked_in(db,vod_key(source,MANUAL_PATH)):raise ValueError(CLEARED_MESSAGE)
        # A second Start must not retitle/reclassify an active transfer.
        if previous and old and old['state'] in ('queued', 'running', 'cancelling'):
            return dict(id=old['id'], state=old['state'], duplicate=True)
        if not previous:
            db.execute('INSERT INTO strm_sources VALUES(?,?,?,?,?,?,?)',
                       (source, digest, parsed['name'], title, kind, parsed['url'], time.time()))
        else:
            db.execute('UPDATE strm_sources SET title=?,kind=? WHERE id=?', (title, kind, source))
        row = dict(title=title, kind=kind, sourceType='strm', folder='')
        identity = enqueue_resolved(app, store, source, MANUAL_PATH, row, transaction=db)
        return dict(id=identity, state='queued', duplicate=False)


def register(app, store, access):
    def upload(allowed_fields):
        # Permit the advertised file size plus bounded multipart form overhead.
        # Set this before files/form are parsed; the app-wide JSON bound is 16KiB.
        request.max_content_length = MAX_BYTES + 16 * 1024
        if set(request.files) != {'strm'} or len(request.files.getlist('strm')) != 1 or set(request.form) - allowed_fields:
            raise ValueError('Choose one STRM file and the supported download options.')
        if any(len(request.form.getlist(key)) != 1 for key in request.form):
            raise ValueError('Choose one value for each download option.')
        file = request.files['strm']
        raw = file.stream.read(MAX_BYTES + 1)
        return raw, file.filename, parse(raw, file.filename)

    def denied():
        response = access(local_action=True)
        if response:
            return response
        token = session.get('csrf')
        if not token or not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), token):
            return jsonify(error='Reload the page.'), 403

    @app.post('/api/strm/preview')
    def strm_preview():
        response = denied()
        if response:
            return response
        try:
            raw, filename, parsed = upload(set())
            identity = preview_id(app, fingerprint(app, raw, filename))
            return jsonify(id=identity, **{key: parsed[key] for key in ('name', 'title', 'kind')})
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except RequestEntityTooLarge:
            return jsonify(error='Choose a STRM file no larger than 16 KiB.'), 413

    @app.post('/api/strm-downloads')
    def strm_start():
        response = denied()
        if response:
            return response
        try:
            raw, filename, parsed = upload({'preview_id', 'title', 'kind'})
            digest = fingerprint(app, raw, filename)
            token = request.form.get('preview_id', '')
            if not re.fullmatch(r'[0-9a-f]{64}', token) or not hmac.compare_digest(token, preview_id(app, digest)):
                raise ValueError('Check this STRM file again before starting the download.')
            title=_validated_title(request.form.get('title'),request.form.get('kind'))
            from .acquisition import ensure_open
            ensure_open(store)
            with store.connect() as db:previous=db.execute('SELECT id FROM strm_sources WHERE fingerprint=?',(digest,)).fetchone()
            if previous:
                from .m3u_downloads import _resume_selected
                _resume_selected(store,previous['id'],MANUAL_PATH)
            result = start(app,store,parsed,digest,title,request.form.get('kind'))
            return jsonify(result)
        except (ValueError, OSError) as exc:
            return jsonify(error=str(exc) if isinstance(exc, ValueError) else 'The download destination is unavailable.'), 400
        except RequestEntityTooLarge:
            return jsonify(error='Choose a STRM file no larger than 16 KiB.'), 413
