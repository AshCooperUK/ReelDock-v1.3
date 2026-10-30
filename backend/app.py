import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, jsonify, redirect, request, send_file, send_from_directory, session, url_for
from flask.sessions import SecureCookieSessionInterface
from werkzeug.exceptions import ServiceUnavailable
from werkzeug.security import check_password_hash, generate_password_hash

from .network import google_request, public_origin, verify_route
from .storage import Store
from . import system_stats

ROOT = Path(__file__).resolve().parent.parent
VERSION = 'v1.3.9'
HOME_NETWORKS = tuple(ipaddress.ip_network(network) for network in
                      ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8', 'fc00::/7', 'fe80::/10', '::1/128'))
# Sign-in limits: failures per client before a five-minute lock, failures
# across all clients before everyone is locked, and per-client rows kept.
LOGIN_CLIENT_LIMIT = 5
LOGIN_GLOBAL_LIMIT = 25
LOGIN_CLIENT_ROWS = 1000
# Finished downloads (each of complete and failed/cancelled/skipped) in the live feed.
DOWNLOAD_HISTORY_LIMIT = 15


mimetypes.add_type('application/manifest+json', '.webmanifest')
STATIC_PREFIXES = ('/preview/assets/', '/static/')
# Vite names built files <name>-<8 character hash>.<ext>; their content never changes.
HASHED_ASSET = re.compile(r'/preview/assets/[^/]+-[A-Za-z0-9_-]{8}\.(?:js|css|svg|png|jpg|webp|woff2?)')
COMPRESSIBLE = {'text/javascript', 'application/javascript', 'text/css', 'image/svg+xml', 'application/json', 'text/plain'}
_gzip_cache = {}


def public_state_free_request():
    """Public bundled files and health never depend on mutable media state."""
    return request.method in ('GET','HEAD') and (request.path=='/health' or request.path.startswith(STATIC_PREFIXES))


def static_cache_policy(path, status):
    """HTML and API responses are never cached; built assets are."""
    if path.startswith('/artwork/') and status == 200:
        return 'private, max-age=86400'
    if status in (200, 304) and HASHED_ASSET.fullmatch(path):
        return 'public, max-age=31536000, immutable'
    if status in (200, 304) and path.startswith(STATIC_PREFIXES):
        # Unhashed public files (for example the TMDB logo) revalidate cheaply.
        return 'no-cache'
    return 'no-store'


def static_file(path):
    from werkzeug.security import safe_join
    if path.startswith('/preview/assets/'):
        found = safe_join(str(ROOT / 'frontend' / 'dist' / 'assets'), path[len('/preview/assets/'):])
    else:
        found = safe_join(str(Path(__file__).resolve().parent / 'static'), path[len('/static/'):])
    return Path(found) if found else None


def compress_static(response):
    """Gzip public text assets once per file version and serve from memory."""
    if response.mimetype not in COMPRESSIBLE:
        return
    response.vary.add('Accept-Encoding')
    if (request.method != 'GET' or response.status_code != 200 or 'gzip' not in request.headers.get('Accept-Encoding', '')
            or request.headers.get('Range') or response.headers.get('Content-Encoding')):
        return
    path = static_file(request.path)
    try:
        stat = path.stat() if path else None
    except OSError:
        return
    if not stat or stat.st_size > 8_000_000 or stat.st_size < 1024:
        return
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    body = _gzip_cache.get(key)
    if body is None:
        import gzip
        body = gzip.compress(path.read_bytes(), compresslevel=6, mtime=0)
        for old in [k for k in _gzip_cache if k[0] == key[0]]:
            del _gzip_cache[old]
        _gzip_cache[key] = body
    etag, _ = response.get_etag()
    response.close()
    response.direct_passthrough = False
    response.set_data(body)
    response.headers['Content-Encoding'] = 'gzip'
    if etag:
        # Same representation, different encoding: a weak validator still lets
        # If-None-Match revalidation return 304.
        response.set_etag(etag, weak=True)


def create_app(config=None):
    from .vpn.transport import install as install_vpn_transport
    install_vpn_transport()
    app = Flask(__name__, static_folder='static')
    started=time.monotonic()
    app.config.update(
        DATA_DIR=os.environ.get('DATA_DIR', str(ROOT / 'data')),
        REELDOCK_EMBY_ROOT=os.environ.get('REELDOCK_EMBY_ROOT', ''),
        REELDOCK_MEDIA_ROOT=os.environ.get('REELDOCK_MEDIA_ROOT', ''),
        MEDIA_DIR=os.environ.get('MEDIA_DIR', str(ROOT / 'media')),
        M3U_DIR=os.environ.get('M3U_DIR', ''),
        M3U_DOWNLOAD_ROOTS=os.environ.get('M3U_DOWNLOAD_ROOTS', '[]'),
        REELDOCK_STORAGE_ROOTS=os.environ.get('REELDOCK_STORAGE_ROOTS', '[]'),
        REELDOCK_STORAGE_LOCATIONS=os.environ.get('REELDOCK_STORAGE_LOCATIONS', '[]'),
        REELDOCK_HOST_ROOT=os.environ.get('REELDOCK_HOST_ROOT', ''),
        REELDOCK_DOWNLOAD_BASE=os.environ.get('REELDOCK_DOWNLOAD_BASE', ''),
        CANDIDATE_URL=os.environ.get('APP_URL', ''),
        GOOGLE_CLIENT_ID=os.environ.get('GOOGLE_CLIENT_ID', ''),
        GOOGLE_CLIENT_SECRET=os.environ.get('GOOGLE_CLIENT_SECRET', ''),
        LOCAL_SETUP_URL=os.environ.get('LOCAL_SETUP_URL', 'http://127.0.0.1:8790'),
        AUTO_LAN_SETUP=os.environ.get('REELDOCK_AUTO_LAN_SETUP') == '1',
        # Host-side port the browser uses for local setup. The container still listens
        # on 8790; a side-by-side install maps another host port (e.g. 8791) to it.
        APP_PORT=os.environ.get('APP_PORT', '8790'),
        MAX_CONTENT_LENGTH=16_384,
        SESSION_COOKIE_NAME='reeldock_dev_session', SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        ROUTE_VERIFIER=verify_route, GOOGLE_REQUEST=google_request,
    )
    if config:
        app.config.update(config)
    def render_template(name, **context):
        """Serve the React AuthApp for setup, sign-in and system messages.

        The legacy Jinja templates were removed; these three page names are
        the only ones AuthApp renders.
        """
        if name not in ('setup.html', 'login.html', 'message.html'):
            raise ValueError('Unknown page: ' + name)
        built = ROOT / 'frontend' / 'dist' / 'index.html'
        if not built.is_file():
            # Useful build error without silently switching back to the old UI.
            raise ServiceUnavailable('Build the ReelDock frontend, then reload.')
        payload = dict(page=name.removesuffix('.html'), version=VERSION, **context)
        encoded = json.dumps(payload).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
        html = built.read_text(encoding='utf-8')
        return html.replace('</head>', '<script id="reeldock-bootstrap" type="application/json">' + encoded + '</script></head>')

    store = Store(app.config['DATA_DIR'])
    app.extensions['store'] = store
    store.legacy_download_base = app.config['REELDOCK_DOWNLOAD_BASE']
    from .event_log import record as record_event, started as record_start
    if not app.testing and app.config.get('PROCESS_ROLE')!='worker':record_start(store,VERSION)
    from .quota import clear_legacy_pause
    clear_legacy_pause(store)
    from .catalogue import initialise
    initialise(store)
    from . import downloads
    downloads.initialise(store)
    from . import media_library, channel_downloads
    media_library.initialise(store)
    channel_downloads.initialise(store)
    from . import emby
    emby.initialise(store)
    from . import youtube_push
    youtube_push.register(app)
    from . import m3u
    m3u.initialise(store)
    app.config['M3U_DIR'] = app.config['M3U_DIR'] or str(Path(app.config['MEDIA_DIR']) / 'M3U')
    from . import storage_layout
    from .storage_layout import source_base
    app.extensions['storage_defaults'] = {key: app.config[key] for key in ('MEDIA_DIR', 'M3U_DIR', 'M3U_DOWNLOAD_ROOTS')}
    storage_layout.configure(app,store)
    storage_layout.apply(app,store)
    app.secret_key = store.secret_file('session-secret.txt')
    instance = store.secret_file('instance-id.txt')
    # Earlier builds verified the hostname before creating an account. Resume those
    # unfinished installations locally; never reset an existing administrator.
    if not store.user() and not store.get('setup_complete') and store.get('public_origin'):
        store.set('public_origin', '')

    port = str(app.config['APP_PORT']).strip()
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError('APP_PORT must be a port number between 1 and 65535.')
    port = int(port)
    configured_local = app.config['LOCAL_SETUP_URL'].strip()
    trusted_hosts = None
    if not configured_local and app.config['AUTO_LAN_SETUP']:
        from . import lan_setup
        trusted_hosts, default_host = lan_setup.read_hosts()
        fallback_local_origin = lan_setup.default_origin(default_host, port)
    else:
        local = urlsplit(configured_local.rstrip('/'))
        try:
            local_port = local.port
        except ValueError:
            local_port = None
        # An explicit URL retains its exact Host and port contract, including
        # when automatic LAN setup is enabled in the installation template.
        if (local.scheme != 'http' or not local.hostname or local.username or local.password
                or local.path or local.query or local.fragment or local_port != port):
            raise ValueError(f'LOCAL_SETUP_URL must be an HTTP NAS address on port {port} (APP_PORT), without a path.')
        fallback_local_origin = 'http://' + local.netloc.lower()
    # Browsers share cookies across ports on one host name, so a second
    # installation on another port gets its own local-setup cookie.
    local_cookie = 'reeldock_dev_local' if port == 8790 else f'reeldock_dev_local_{port}'
    def local_host():
        if trusted_hosts is not None:
            return lan_setup.origin_for(request.host, request.scheme, port, trusted_hosts) is not None
        return request.host.lower() == local.netloc.lower()

    def local_origin():
        if trusted_hosts is not None:
            return lan_setup.origin_for(request.host, request.scheme, port, trusted_hosts) or fallback_local_origin
        return fallback_local_origin

    def home_network(value):
        try:
            address = ipaddress.ip_address(value or '')
        except ValueError:
            return False
        return any(address in network for network in HOME_NETWORKS)

    def direct_local():
        # Reject proxy markers, including an overridden local Host from a tunnel.
        # The exact configured Host also prevents arbitrary DNS-rebinding origins.
        if not local_host() or request.scheme != 'http':
            return False
        if any(name.lower().startswith(('cf-', 'x-forwarded-')) or name.lower() in ('forwarded', 'x-real-ip')
               for name in request.headers.keys()):
            return False
        return home_network(request.remote_addr)

    class SetupSessions(SecureCookieSessionInterface):
        def get_cookie_name(self, app):
            return local_cookie if local_host() else super().get_cookie_name(app)

        def get_cookie_secure(self, app):
            return not local_host()

        def get_signing_serializer(self, app):
            serializer = super().get_signing_serializer(app)
            if serializer and local_host():
                serializer.salt = b'reeldock-local-setup-session'
            return serializer

    app.session_interface = SetupSessions()

    def logged_in():
        return session.get('user_id') == 1 and store.user() is not None and session.get('auth_revision','0') == store.get('auth_revision','0')

    def csrf():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        return session['csrf']

    def error(message, status=400):
        return jsonify(error=message), status

    def limited(bucket):
        with store.connect() as db:
            row = db.execute('SELECT * FROM attempts WHERE bucket=?', (bucket,)).fetchone()
        return row and row['locked_until'] > time.time()

    def record_failure(bucket, threshold=5):
        with store.connect() as db:
            db.execute('''INSERT INTO attempts VALUES (?,1,0) ON CONFLICT(bucket)
                          DO UPDATE SET failures=CASE WHEN locked_until>0 AND locked_until<? THEN 1 ELSE failures+1 END,
                          locked_until=CASE WHEN locked_until>0 AND locked_until<? THEN 0 ELSE locked_until END''',
                       (bucket, time.time(), time.time()))
            db.execute('UPDATE attempts SET locked_until=? WHERE bucket=? AND failures>=?',
                       (time.time() + 300, bucket, threshold))
            if bucket.startswith('login:'):
                # Keep per-client rows bounded even if many addresses are tried.
                db.execute('''DELETE FROM attempts WHERE bucket LIKE 'login:%' AND locked_until<? AND rowid NOT IN
                              (SELECT rowid FROM attempts WHERE bucket LIKE 'login:%' ORDER BY rowid DESC LIMIT ?)''',
                           (time.time(), LOGIN_CLIENT_ROWS))

    def login_client_bucket():
        """Rate-limit bucket for the signing-in client.

        Behind the Cloudflare tunnel every request arrives from the local
        connector, so the visitor address comes from CF-Connecting-IP. That
        header is only trusted when the direct peer is on a home/loopback
        network (the same list as direct_local); any other peer is keyed by
        its own address. Addresses are
        stored hashed.
        """
        client = request.remote_addr or ''
        forwarded = request.headers.get('CF-Connecting-IP', '').strip()
        if forwarded and home_network(client):
            try:
                client = str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        return 'login:' + hashlib.sha256(client.encode()).hexdigest()[:24]

    def clear_failures(bucket):
        with store.connect() as db:
            db.execute('DELETE FROM attempts WHERE bucket=?', (bucket,))

    @app.before_request
    def reuse_connection():
        # Registered before guard(): every query in this request shares one
        # SQLite connection, closed again in teardown.
        if public_state_free_request():return
        store.begin_operation()
        request.environ['reeldock.operation'] = True
        store.begin_reuse()
        request.environ['reeldock.reuse'] = True
        storage_layout.apply(app,store)

    @app.teardown_request
    def release_connection(_error):
        try:
            if request.environ.pop('reeldock.reuse',False):store.end_reuse()
        finally:
            if request.environ.pop('reeldock.operation', False):store.end_operation()

    @app.before_request
    def guard():
        # Error/setup pages need these public files even when mutable storage
        # is busy or unavailable. No HTML, API or setup probe uses this lane.
        if public_state_free_request():return
        origin = store.get('public_origin')
        exempt = request.path == '/health' or request.path.startswith('/setup/probe/')
        if not origin and not exempt and not direct_local():
            message = 'Open ' + local_origin() + '/setup on your home network to complete first installation.'
            if request.method == 'GET' and not request.path.startswith('/api/'):
                help_link = {'continue_url': local_origin() + '/setup'} if trusted_hosts is not None else {}
                return render_template('message.html', title='Continue setup on your NAS', message=message, **help_link), 403
            return error(message, 403)
        if origin and not exempt and request.host.lower() != urlsplit(origin).netloc:
            if request.method in ('GET', 'HEAD'):
                return redirect(origin + ('/setup' if not store.get('setup_complete') else '/login'), 302)
            return error('Continue using the verified public HTTPS address.', 403)
        if store.get('reset_in_progress') == '1' and not exempt and request.path not in ('/api/account/reset', '/login', '/logout'):
            return error('ReelDock is stopping work for a reset. Keep the reset window open.', 503)
        if request.path.startswith('/api/setup/'):
            if request.method == 'POST' and request.path != '/api/setup/cookies' and not isinstance(request.get_json(silent=True), dict):
                return error('Send a JSON object for this setup step.')
            if store.get('setup_complete') and not store.get('reconnect_pending'):
                return error('Initial setup is complete.', 409)
            if store.user():
                if not logged_in():
                    return error('Sign in as the administrator to resume setup.', 401)
                if request.method == 'POST' and not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), csrf()):
                    return error('Your session needs refreshing. Reload the page.', 403)
            else:
                if request.path not in ('/api/setup/state', '/api/setup/admin'):
                    return error('Create the administrator locally first.', 409)
                if request.method == 'POST' and not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), csrf()):
                    return error('Reload the local setup page and try again.', 403)
            if not origin and store.get('local_only') != '1' and request.path not in ('/api/setup/state', '/api/setup/admin', '/api/setup/verify-route', '/api/setup/skip-route'):
                return error('Verify your public address before connecting YouTube.', 409)
        # Signed WebSub callbacks alone accept server POSTs without a browser Origin.
        if request.endpoint == 'youtube_notification':
            return None
        if request.method == 'POST' and request.headers.get('Origin') != (origin or local_origin()):
            if request.path == '/login':
                return render_template('message.html', title='Please reopen the sign-in page',
                                       message='The browser could not confirm this sign-in came from this app. Reopen sign-in and try again.',
                                       continue_url='/login', continue_label='Reopen sign-in'), 403
            return error('Open the installation address and try again.', 403)

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        # no-referrer turns native form POST origins into "null" in browsers,
        # which conflicts with our strict Origin check on login and logout.
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data: https://api.nzbgeek.info https://api.nzb.life https://nzb.life https://www.nzb.life https://i.ytimg.com https://yt3.ggpht.com https://yt3.googleusercontent.com https://static.tvmaze.com https://image.tmdb.org; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-src 'none'; media-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers['Cache-Control'] = static_cache_policy(request.path, response.status_code)
        if request.path.startswith(STATIC_PREFIXES):
            compress_static(response)
        return response

    @app.after_request
    def stop_media_on_reset(response):
        if public_state_free_request():return response
        if response.is_streamed and (response.mimetype.startswith(('video/', 'audio/')) or response.mimetype == 'application/octet-stream'):
            revision = store.get('auth_revision')
            body = response.response
            def chunks():
                checked = 0
                try:
                    for chunk in body:
                        now = time.monotonic()
                        if now - checked >= .25:
                            if store.reset_pending() or store.get('auth_revision') != revision:
                                break
                            checked = now
                        yield chunk
                finally:
                    if hasattr(body, 'close'):body.close()
            response.response = chunks()
        return response

    @app.after_request
    def log_changes(response):
        if request.method not in ('POST','PUT','PATCH','DELETE') or not 200<=response.status_code<300 or not session.get('user_id'):
            return response
        labels={'/api/settings/media-players':'Media player preferences changed','/api/settings/auto-download':'Auto-Download settings changed','/api/settings/torrent':'Torrent settings changed','/api/settings/nzb':'NZB settings changed','/api/settings/m3u':'M3U and Xtream settings changed','/api/settings/youtube':'YouTube settings changed','/api/sync-all':'Sync All requested','/api/youtube-sync':'YouTube sync settings changed',
                '/api/youtube-schedule':'YouTube scan schedule changed','/api/emby':'Emby action applied',
                '/api/m3u':'M3U subscription action applied','/api/youtube/cookies':'YouTube cookies updated',
                '/api/guide/sync':'Guide sync requested','/api/subscriptions':'YouTube subscriptions updated',
                '/api/m3u-download-settings':'M3U download settings changed',
                '/api/torrents':'Torrent download changed','/api/torrents/preview':'Torrent scanned','/api/torrents/settings':'Torrent settings changed',
                '/api/youtube/channels':'YouTube channel action applied','/api/youtube/mode':'YouTube source changed',
                '/api/channel-view':'Channel layout changed','/api/guide/week':'Guide week requested'}
        title=labels.get(request.path)
        if request.path.startswith('/api/channel-favourite/'):title='Channel favourite changed'
        elif request.path.startswith('/api/channel-downloads/'):title='Channel download options changed'
        elif title is None and request.path.startswith('/api/torrents/'):title='Torrent download changed'
        if title:
            try:record_event(store,title,'The requested action was accepted.','ReelDock')
            except (sqlite3.Error,OSError):app.logger.warning('Could not record activity event.')
        return response

    @app.errorhandler(sqlite3.OperationalError)
    def database_unavailable(exc):
        code = getattr(exc, 'sqlite_errorcode', 0) & 255
        locked = code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or 'locked' in str(exc).lower()
        app.logger.exception('Database request failed')
        # Do not consult storage while reporting a storage error, including
        # failures from the authentication guard before a route is reached.
        if locked:
            response = jsonify(error='The database is busy while the Guide updates. Please try again shortly.')
            response.status_code = 503
            response.headers['Retry-After'] = '10'
            return response
        return error('The server could not read its database. Check the Guide container logs.', 500)

    @app.errorhandler(413)
    def too_large(_error):
        return error('The submitted form is too large.', 413)

    def credentials():
        return (app.config['GOOGLE_CLIENT_ID'] or store.get('google_client_id'),
                app.config['GOOGLE_CLIENT_SECRET'] or store.get('google_client_secret'))

    def readiness():
        checks = []
        if storage_layout.fixed(store):
            from .setup_storage import state as storage_state
            checks.append({'name': 'Compose media folders writable', 'ok': storage_state(app,store)['ready']})
        try:
            store.set('storage_check', int(time.time()))
            cache = store.directory / 'artwork'
            cache.mkdir(exist_ok=True, mode=0o700)
            probe = cache / ('.check-' + secrets.token_hex(8))
            try:
                with probe.open('x') as file:
                    file.write('ok')
            finally:
                probe.unlink(missing_ok=True)
            checks.append({'name': 'Database and new artwork directory writable', 'ok': True})
        except (OSError, sqlite3.Error):
            checks.append({'name': 'Database or artwork directory needs write permission', 'ok': False})
        try:
            ZoneInfo('Europe/London')
            checks.append({'name': 'Europe/London timezone available', 'ok': True})
        except ZoneInfoNotFoundError:
            checks.append({'name': 'Timezone data missing', 'ok': False})
        return checks

    @app.get('/health')
    def health():
        return jsonify(status='ok', version=VERSION)

    @app.get('/')
    def home():
        return redirect('/app/' if store.get('setup_complete') and logged_in() else '/setup' if not store.get('setup_complete') else '/login')

    def saved_guide_available():
        # A completed import is evidence of a previously configured installation.
        # This never grants access to an anonymous visitor or starts new imports.
        if not store.get('public_origin') or not store.get('last_sync'):
            return False
        with store.connect() as db:
            return db.execute('SELECT 1 FROM channels LIMIT 1').fetchone() is not None

    @app.get('/setup')
    def setup():
        if store.get('setup_complete') and not store.get('reconnect_pending'):
            return redirect('/')
        if store.user() and not logged_in():
            return redirect('/login')
        return render_template('setup.html', initial=setup_state(),
                               candidate=store.get('public_origin') or app.config['CANDIDATE_URL'],
                               saved_guide=saved_guide_available())

    def setup_state():
        client_id, secret = credentials()
        from .youtube_source import enabled
        from .setup_storage import state as storage_state
        return dict(storage=storage_state(app, store), import_method=store.get('youtube_setup_method') or ('google' if enabled(store) else 'manual'),
                       source_ready=store.get('youtube_setup_source_ready')=='1' or (not store.get('youtube_setup_method') and not enabled(store)),
                       cookies_saved=(store.directory/'youtube-cookies.txt').is_file(),
                       cookie_import_status=store.get('youtube_cookie_subscriptions_status'),
                       use_google_api=enabled(store),origin=store.get('public_origin') or (local_origin() if store.get('local_only') == '1' else ''),
                       local_only=store.get('local_only') == '1', admin=bool(store.user()),
                       client_id_present=bool(client_id), client_secret_present=bool(secret),
                       id_from_yaml=bool(app.config['GOOGLE_CLIENT_ID']), secret_from_yaml=bool(app.config['GOOGLE_CLIENT_SECRET']),
                       youtube_connected=bool(store.get('google_tokens')), reconnecting=store.get('reconnect_pending')=='1', checks=readiness(),
                       csrf=csrf())

    @app.get('/api/setup/state')
    def state():
        return jsonify(setup_state())

    @app.get('/setup/probe/<nonce>')
    def probe(nonce):
        if (store.get('setup_complete') or not hmac.compare_digest(nonce, store.get('route_nonce'))
                or float(store.get('route_expiry', '0')) < time.time()):
            return error('Probe not found.', 404)
        return jsonify(nonce=nonce, instance=instance)

    @app.post('/api/setup/storage')
    def setup_storage():
        from .setup_storage import save
        try:
            save(app, store, request.get_json())
        except (ValueError, OSError) as exc:
            return error(str(exc))
        return jsonify(ok=True)

    @app.post('/api/setup/skip-route')
    def skip_route():
        if not direct_local() or store.get('public_origin'):
            return error('Local mode must be selected at the local setup address.', 409)
        store.set('youtube_use_google_api', 'false')
        store.set('youtube_setup_method', 'manual')
        store.set('youtube_setup_source_ready', '0')
        store.set('local_only', '1')
        return jsonify(ok=True)

    @app.post('/api/setup/verify-route')
    def verify():
        if store.get('public_origin'):
            return error('The public URL is already fixed for this installation.', 409)
        payload = request.get_json(silent=True) or {}
        try:
            origin = public_origin(str(payload.get('url', '')))
            public_host = urlsplit(origin).hostname
            if (public_host == urlsplit(local_origin()).hostname
                    or (trusted_hosts is not None and public_host in trusted_hosts)):
                raise ValueError('Use a separate public hostname from your local setup address.')
            nonce = secrets.token_urlsafe(32)
            store.set('route_nonce', nonce)
            store.set('route_expiry', time.time() + 120)
            app.config['ROUTE_VERIFIER'](origin, nonce, instance)
            store.set('public_origin', origin)
            store.set('local_only', '')
        except ValueError as exc:
            return error(str(exc))
        finally:
            store.set('route_nonce', '')
            store.set('route_expiry', 0)
        session.clear()
        return jsonify(origin=origin, continue_url=origin + '/setup')

    @app.post('/api/setup/admin')
    def admin():
        if not direct_local() or store.get('public_origin'):
            return error('Create the administrator at the local NAS address before verification.', 409)
        payload = request.get_json(silent=True) or {}
        username = str(payload.get('username', '')).strip()
        password = str(payload.get('password', ''))
        if not re.fullmatch(r'[A-Za-z0-9_.-]{3,64}', username):
            return error('Use 3–64 letters, numbers, dots, underscores or hyphens for the username.')
        if len(password) < 12 or len(password) > 256:
            return error('Use a password between 12 and 256 characters.')
        if password != payload.get('confirm'):
            return error('The passwords do not match.')
        try:
            with store.connect() as db:
                db.execute('INSERT INTO users VALUES (1,?,?)', (username, generate_password_hash(password)))
        except sqlite3.IntegrityError:
            return error('The administrator account already exists.', 409)
        session.clear()
        session['user_id'] = 1
        session.permanent = True
        return jsonify(csrf=csrf())

    @app.post('/api/setup/import-method')
    @app.post('/api/setup/source-ready')
    def setup_import_method():
        if not logged_in():return error('Sign in first.',401)
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return error('Reload the page.',403)
        body=request.get_json(silent=True) or {}
        if not isinstance(body,dict):return error('Choose an import method.')
        if request.path.endswith('import-method'):
            method=body.get('method')
            if method not in ('google','cookies','manual'):return error('Choose an import method.')
            if method == 'google' and store.get('local_only') == '1':
                return error('Google API requires a verified public HTTPS address.', 409)
            store.set('youtube_setup_method',method)
            if method=='cookies':store.set('youtube_cookie_import_channels','true')
            store.set('youtube_use_google_api','true' if method=='google' else 'false')
            store.set('youtube_setup_source_ready','0')
        else:
            method=store.get('youtube_setup_method','google')
            if method=='google' and not store.get('google_tokens'):return error('Connect Google first.',409)
            if method=='cookies' and not (store.directory/'youtube-cookies.txt').is_file():return error('Upload cookies.txt first.',409)
            store.set('youtube_setup_source_ready','1')
        store.set('next_sync','0');store.set('youtube_subscriptions_next','0');store.set('youtube_cookie_subscriptions_next','0')
        return jsonify(ok=True)

    @app.post('/api/setup/youtube-mode')
    @app.post('/api/youtube/mode')
    def youtube_mode():
        if not logged_in():return error('Sign in first.',401)
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return error('Reload the page.',403)
        body=request.get_json(silent=True)
        if not isinstance(body,dict) or type(body.get('enabled')) is not bool:return error('Choose on or off.')
        from . import settings
        try: settings.save(store, 'youtube', {'use_google_api': body['enabled']})
        except settings.Conflict as exc: return error(str(exc), 409)
        return jsonify(ok=True)

    @app.post('/api/setup/channels')
    @app.route('/api/youtube/channels',methods=['GET','POST'])
    def youtube_local_channels():
        request.max_content_length=600000
        denied=(None if logged_in() else error('Sign in first.',401)) if request.path.startswith('/api/setup/') else guide_access(local_action=True)
        if denied:return denied
        from .youtube_fallback import queue_channels
        if request.method=='POST':
            if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return error('Reload the page.',403)
            body=request.get_json(silent=True)
            if not isinstance(body,dict):return error('Enter a channel list.')
            try:count=queue_channels(store,body.get('channels'))
            except ValueError as exc:return error(str(exc))
            return jsonify(message=f'{count} channels queued for checking.')
        with store.connect() as db:
            rows=[dict(r) for r in db.execute('SELECT url,state,message FROM youtube_channel_requests ORDER BY updated DESC LIMIT 20')]
            count=db.execute("SELECT count(*) FROM youtube_channel_requests WHERE state='queued'").fetchone()[0]
        return jsonify(items=rows,queued=count)

    @app.post('/api/setup/google')
    def save_google():
        if not logged_in():
            return error('Create the administrator first.', 403)
        payload = request.get_json(silent=True) or {}
        old_id, old_secret = credentials()
        client_id = app.config['GOOGLE_CLIENT_ID'] or str(payload.get('client_id', '')).strip() or old_id
        secret = app.config['GOOGLE_CLIENT_SECRET'] or str(payload.get('client_secret', '')).strip() or old_secret
        if not client_id.endswith('.apps.googleusercontent.com') or len(client_id) > 512 or len(secret) < 10 or len(secret) > 4096:
            return error('Enter the Client ID and Client secret from a Google Web application OAuth client.')
        if client_id != old_id or secret != old_secret:
            from .quota import bind
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                bind(db,old_id)
                bind(db,client_id)
            store.set('google_tokens', '')
        if not app.config['GOOGLE_CLIENT_ID']:
            store.set('google_client_id', client_id)
        if not app.config['GOOGLE_CLIENT_SECRET']:
            store.set('google_client_secret', secret)
        return jsonify(ok=True)

    @app.post('/api/youtube/connect')
    @app.post('/api/youtube/authorise')
    @app.post('/api/setup/connect-google')
    def connect_google():
        if not logged_in():
            return error('Sign in first.', 403)
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return error('Reload the page.',403)
        if not store.get('public_origin'):return error('Verify your public address before connecting YouTube.',409)
        from .youtube_source import enabled
        if not enabled(store):return error('Turn on Use Google API before connecting Google.',409)
        manage=request.path=='/api/youtube/authorise'
        client_id, secret = credentials()
        if not client_id or not secret:
            return error('Save your Google credentials first.')
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        session['oauth'] = {'state': state, 'verifier': verifier, 'expires': time.time() + 600, 'manage':manage, 'settings':request.path=='/api/youtube/connect'}
        import base64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        return jsonify(url='https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({
            'client_id': client_id, 'redirect_uri': store.get('public_origin') + '/oauth/google/callback',
            'response_type': 'code', 'scope': 'https://www.googleapis.com/auth/youtube' if manage else 'https://www.googleapis.com/auth/youtube.readonly',
            'access_type': 'offline', 'prompt': 'select_account consent', 'state': state,
            'code_challenge': challenge, 'code_challenge_method': 'S256',
        }))

    @app.get('/oauth/google/callback')
    def callback():
        from .youtube_source import enabled
        if not enabled(store):
            session.pop('oauth',None)
            return render_template('message.html',title='Google API is off',message='Your Google connection was not changed. Enable Google API in Settings to connect.'),409
        pending = session.pop('oauth', {})
        if (not logged_in() or pending.get('expires', 0) < time.time()
                or not pending.get('state') or not hmac.compare_digest(request.args.get('state', ''), pending['state'])):
            return render_template('message.html', title='Google connection expired', message='Return to setup and start Connect YouTube again.'), 400
        try:
            if request.args.get('error') or not request.args.get('code'):
                raise ValueError('Google authorisation was cancelled. You can try again from setup.')
            client_id, secret = credentials()
            tokens = app.config['GOOGLE_REQUEST']('oauth2.googleapis.com', '/token', body=urlencode({
                'code': request.args['code'], 'client_id': client_id, 'client_secret': secret,
                'redirect_uri': store.get('public_origin') + '/oauth/google/callback',
                'grant_type': 'authorization_code', 'code_verifier': pending['verifier'],
            }))
            if not tokens.get('access_token') or not tokens.get('refresh_token'):
                raise ValueError('Google did not grant background access. Reconnect and accept the requested access.')
            if pending.get('manage'):
                if 'https://www.googleapis.com/auth/youtube' not in tokens.get('scope','').split():raise ValueError('Google did not grant subscription management access. Your existing connection is unchanged.')
                tokens['received_at']=time.time()
                store.set('google_tokens',json.dumps(tokens))
                # Land on the YouTube channels page, like the account-connect flow.
                return redirect('/app/?view=YouTube')
            quota_error = ''
            try:
                app.config['GOOGLE_REQUEST']('www.googleapis.com', '/youtube/v3/subscriptions?part=id&mine=true&maxResults=1', token=tokens['access_token'])
            except ValueError as exc:
                # OAuth succeeded. A quota failure is not a rejected login.
                if any(code in str(exc) for code in ('quotaExceeded', 'dailyLimitExceeded')):
                    quota_error = str(exc)
                    from .quota import next_reset
                    store.set('google_quota_retry_at',next_reset())
                    store.set('google_quota_error',quota_error)
                else:
                    raise

            tokens['received_at'] = time.time()
            if not quota_error:
                from .quota import bind, KEYS
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    # Old releases did not record which client incurred these
                    # counts. Establish a fresh baseline once, only after a
                    # successful authenticated YouTube API check.
                    if (not db.execute("SELECT 1 FROM settings WHERE key='quota_client'").fetchone()
                            or db.execute("SELECT 1 FROM settings WHERE key='quota_legacy_unattributed'").fetchone()):
                        db.executemany('DELETE FROM settings WHERE key=?',[(key,) for key in KEYS])
                        db.execute("DELETE FROM settings WHERE key='quota_legacy_unattributed'")
                    bind(db,client_id)
            store.set('google_tokens', json.dumps(tokens))
            store.set('youtube_subscriptions_next','0')
            store.set('youtube_api_unavailable_until','0')
            store.set('reconnect_pending', '')
            store.set('sync_error', quota_error)
            if not quota_error:
                store.set('sync_message', 'Google connected. Waiting to check for updates.')
                store.set('history_message', '')
                store.set('next_history', '0')
            if quota_error:
                store.set('setup_complete', '1')
            store.set('next_sync', str(time.time()+1800) if quota_error else '0')
            store.set('recent_scan_started', '0')
        except ValueError as exc:
            return render_template('message.html', title='YouTube is not connected yet', message=str(exc)), 400
        return redirect('/app/?view=YouTube') if pending.get('settings') else redirect('/setup')

    @app.post('/api/setup/finish')
    def finish():
        from .youtube_source import enabled
        if not logged_in() or (enabled(store) and not store.get('google_tokens')):
            return error('Connect YouTube and verify API access before finishing.', 409)
        if not all(check['ok'] for check in readiness()):
            return error('Resolve the storage/timezone checks before finishing.', 409)
        store.set('reconnect_pending', '')
        store.set('refresh_minutes', 360)
        store.set('setup_complete', '1')
        return jsonify(url='/app/')

    @app.get('/api/credits')
    def software_credits():
        from .credits import summaries
        return jsonify(items=summaries())

    @app.get('/licenses/<key>')
    def software_notice(key):
        from .credits import notice
        text = notice(key)
        if text is None:
            return 'Notice not found.', 404
        return app.response_class(text, mimetype='text/plain')

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if not store.user():
            return redirect('/setup')
        message = ''
        if request.method == 'POST':
            if not hmac.compare_digest(request.form.get('csrf', ''), csrf()):
                return render_template('login.html', csrf_token=csrf(), message='Your sign-in page expired. Please try again.'), 403
            client_bucket = login_client_bucket()
            # Five failures lock that client for five minutes, as before. The
            # shared 'login' bucket is a ceiling across all clients, so failures
            # from other addresses can no longer lock the owner out after five.
            if limited(client_bucket) or limited('login'):
                message = 'Too many failed attempts. Wait five minutes.'
            else:
                user = store.user()
                valid = check_password_hash(user['password_hash'], request.form.get('password', ''))
                if valid and hmac.compare_digest(user['username'], request.form.get('username', '').strip()):
                    clear_failures(client_bucket)
                    clear_failures('login')
                    session.clear()
                    session['user_id'] = 1
                    session['auth_revision'] = store.get('auth_revision','0')
                    session.permanent = True
                    record_event(store,'Signed in','Account signed in successfully.','Account')
                    return redirect('/')
                record_failure(client_bucket, LOGIN_CLIENT_LIMIT)
                record_failure('login', LOGIN_GLOBAL_LIMIT)
                record_event(store,'Sign-in failed','An incorrect username or password was entered.','Account','failed')
                message = 'Username or password was not recognised.'
        return render_template('login.html', csrf_token=csrf(), message=message)

    @app.post('/api/account/password')
    def change_password():
        if not logged_in():return error('Sign in again.',401)
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return error('Reload the page.',403)
        if limited('password-change'):return error('Too many failed attempts. Wait five minutes.',429)
        body=request.get_json(silent=True)
        if not isinstance(body,dict):return error('Enter your current and new passwords.')
        current=body.get('current');new=body.get('password');confirm=body.get('confirm')
        if not all(isinstance(v,str) for v in (current,new,confirm)) or len(current)>256:return error('Enter your current and new passwords.')
        if not 12<=len(new)<=256:return error('Use 12 to 256 characters for your new password.')
        if new!=confirm:return error('The new passwords do not match.')
        user=store.user()
        if not check_password_hash(user['password_hash'],current):
            record_failure('password-change')
            return error('Your current password was not recognised.',400)
        with store.connect() as db:
            changed=db.execute('UPDATE users SET password_hash=? WHERE id=1 AND password_hash=?',(generate_password_hash(new),user['password_hash']))
            if not changed.rowcount:return error('Your account changed. Sign in again.',409)
            db.execute("INSERT INTO settings VALUES('auth_revision',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(secrets.token_urlsafe(24),))
        record_event(store,'Password changed','Account password changed; existing sessions require sign-in.','Account')
        clear_failures('password-change')
        session.clear()
        return jsonify(url='/login')

    @app.post('/api/account/reset')
    def reset_reeldock():
        if not logged_in():return error('Sign in again.', 401)
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), csrf()):return error('Reload the page.', 403)
        if limited('factory-reset'):return error('Too many failed attempts. Wait five minutes.', 429)
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or body.get('confirmed') is not True:
            return error('Confirm permanent deletion in the reset popup.')
        password = body.get('password')
        if not isinstance(password, str) or len(password) > 256:return error('Enter your current password.')
        if not check_password_hash(store.user()['password_hash'], password):
            record_failure('factory-reset')
            return error('Your current password was not recognised.', 400)
        from .reset import reset, targets, ResetBusy
        try:
            # Validate paths before pausing any work. Shared database access lets
            # active workers see this signal while their operation locks drain.
            targets(app, store)
            store.set('reset_in_progress', '1')
            from .torrents import stop as stop_torrents
            stop_torrents(app)
            from .nzb import stop as stop_nzb
            stop_nzb(app)
            with store.exclusive_operation():
                # Recheck after upgrading the cross-process request lock.
                if not logged_in():return error('Sign in again.', 401)
                reset(app, store)
                storage_layout.apply(app, store)
                session.clear()
        except ResetBusy:
            return jsonify(state='stopping', message='Stopping active work…'), 202
        except ValueError as exc:return error(str(exc), 400)
        return jsonify(url=local_origin() + '/setup')

    @app.post('/logout')
    def logout():
        if not logged_in() or not hmac.compare_digest(request.form.get('csrf', ''), csrf()):
            return error('Reload the page and try again.', 403)
        record_event(store,'Signed out','Account signed out.','Account')
        session.clear()
        return redirect('/login')

    @app.get('/api/network-activity')
    def network_activity():
        denied = guide_access(local_action=True)
        if denied:return denied
        from .network_activity import snapshot
        response = jsonify(snapshot(store))
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/api/dashboard')
    def dashboard():
        # Only the next sign-in/setup action is public; no library or disk data.
        if not logged_in():
            return jsonify(state='sign_in' if store.user() else 'setup',
                           continue_url='/login' if store.user() else '/setup')
        if not store.get('setup_complete') and not saved_guide_available():
            return jsonify(state='setup', continue_url='/setup')
        from .system_stats import snapshot
        overview = snapshot(store, app.config['MEDIA_DIR'], VERSION, started)
        with store.connect() as db:
            sources = db.execute('SELECT count(*) FROM m3u_sources').fetchone()[0]
        from .sync_all import status as sync_all_status
        from .at_a_glance import snapshot as glance_snapshot
        from .discover_initial import snapshot as discover_initial
        response = jsonify(state='ready', overview=overview, m3u_sources=sources, csrf=csrf(),
                           sync_all=sync_all_status(store), glance=glance_snapshot(app, store, overview), discover_initial=discover_initial(store))
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/app/')
    @app.get('/preview/')
    def live_interface():
        if request.path == '/preview/':
            return redirect('/app/', 302)
        built = ROOT / 'frontend' / 'dist'
        if not (built / 'index.html').is_file():
            return render_template('message.html', title='Build the ReelDock interface',
                                   message='Run pnpm build in frontend, then reload this page.'), 503
        return send_from_directory(built, 'index.html')

    @app.get('/preview/assets/<path:filename>')
    def interface_assets(filename):
        return send_from_directory(ROOT / 'frontend' / 'dist' / 'assets', filename)

    @app.get('/guide/')
    def guide():
        if not logged_in():
            return redirect('/login')
        if not store.get('setup_complete') and not saved_guide_available():
            return redirect('/setup')
        return redirect('/app/?view=TV+Guide')

    def guide_access(local_action=False):
        if not logged_in():
            return error('Sign in to open your Guide.', 401)
        if not store.get('setup_complete'):
            if (request.method in ('GET', 'HEAD') or local_action) and saved_guide_available():
                return None
            return error('Reconnect YouTube in setup before making changes. Your saved Guide is still available.', 409)

    @app.get('/api/storage-folders')
    def storage_folders():
        if not logged_in():return error('Sign in to choose a storage folder.',401)
        if storage_layout.fixed(store): return error(storage_layout.LOCKED, 409)
        from .storage_locations import browse
        try:return jsonify(browse(app,store,request.args.get('path','')))
        except (ValueError,OSError) as exc:return error(str(exc) if isinstance(exc,ValueError) else 'The storage folder is unavailable.')

    @app.route('/api/storage-settings',methods=['GET','POST'])
    def storage_preferences():
        denied=guide_access(local_action=True)
        if denied:return denied
        if request.method=='POST':
            if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return error('Reload the page.',403)
            try:
                body=request.get_json(silent=True)
                if storage_layout.fixed(store): raise ValueError(storage_layout.LOCKED)
                if not isinstance(body,dict):raise ValueError('Choose valid storage settings.')
                if 'base' in body:
                    from .storage_move import move
                    from .reset import ResetBusy
                    try:
                        with store.exclusive_operation():move(app,store,body['base'])
                    except ResetBusy:
                        return error('The library is in use. Finish downloads and scans before changing its directory.',409)
                elif 'auto_download_folder' in body:
                    from .auto_downloads import save_folder
                    save_folder(app,store,body.get('auto_download_folder'))
                else:storage_layout.save_names(app,store,body.get('names'))
            except (ValueError,OSError) as exc:return error(str(exc) if isinstance(exc,ValueError) else 'The storage location is unavailable.')
        from .setup_storage import state
        return jsonify(state(app,store))

    @app.route('/api/channel-view',methods=['GET','POST'])
    def channel_view():
        denied=guide_access(local_action=True)
        if denied:return denied
        if request.method=='POST':
            if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return error('Reload the page.',403)
            body=request.get_json(silent=True)
            value=body.get('view') if isinstance(body,dict) else None
            if value not in ('tile','list','compact','detailed','max'):return error('Choose a channel view.')
            store.set('channel_view',value)
        return jsonify(view=store.get('channel_view','tile'))

    def subscription_page(query='', offset=0, channel='', sort='favourites', favourites_only=False):
        # Escape LIKE metacharacters: a search is literal text, not a wildcard pattern.
        query = query[:150]
        pattern = '%' + query.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
        name_order = "json_extract(c.data,'$.name') COLLATE NOCASE,c.id"
        orders = {
            'favourites': '(fav.id IS NOT NULL) DESC,' + name_order,
            'az': name_order,
            'za': "json_extract(c.data,'$.name') COLLATE NOCASE DESC,c.id",
            'latest': "COALESCE((SELECT MAX(v.published) FROM videos v WHERE v.channel=c.id AND json_extract(v.data,'$.state')='published'),'') DESC," + name_order,
            'subscribers': "COALESCE(CAST(NULLIF(json_extract(c.data,'$.subscribers'),'') AS REAL),-1) DESC," + name_order,
        }
        if sort not in orders:
            raise ValueError('Choose a supported channel order.')
        with store.connect() as db:
            rows = db.execute("SELECT c.data,COALESCE(p.shown,1) AS shown,COALESCE(f.enabled,1) AS forecasts FROM channels c LEFT JOIN channel_preferences p ON p.id=c.id LEFT JOIN channel_forecast_preferences f ON f.id=c.id LEFT JOIN channel_favourites fav ON fav.id=c.id WHERE c.active=1 AND (?='' OR c.id=?) AND json_extract(c.data,'$.name') LIKE ? ESCAPE '!' AND (?=0 OR fav.id IS NOT NULL) ORDER BY " + orders[sort] + " LIMIT 16 OFFSET ?", (channel,channel,pattern,int(favourites_only),offset)).fetchall()
            total = db.execute('SELECT COUNT(*) FROM channels WHERE active=1').fetchone()[0]
            shown = db.execute('SELECT COUNT(*) FROM channels c LEFT JOIN channel_preferences p ON p.id=c.id WHERE c.active=1 AND COALESCE(p.shown,1)=1').fetchone()[0]
            rules = {r['channel']:dict(r) for r in db.execute('SELECT * FROM download_rules')}
            tasks = {r['channel']:dict(r) for r in db.execute('SELECT channel,state,message FROM channel_tasks')}
        default_quality = store.get('download_quality','1080')
        sizes = media_library.disk_usage(store,app.config['MEDIA_DIR'],[json.loads(r['data'])['id'] for r in rows[:15]])
        items = []
        from datetime import datetime
        from zoneinfo import ZoneInfo
        for row in rows[:15]:
            item = json.loads(row['data'])
            checked = item.get('last_checked')
            item['checked_label'] = datetime.fromtimestamp(checked,ZoneInfo('Europe/London')).strftime('%d.%m.%Y %H:%M') if checked else 'Not checked yet'
            item['shown'] = bool(row['shown'])
            item['forecasts'] = bool(row['forecasts'])
            item['downloads'] = rules.get(item['id'],dict(quality=default_quality,quality_override=0,videos=0,shorts=0,live=0,range_days=180,range_override=0,keep_files=0,sponsorblock='default',force_transcode='default'))
            if not item['downloads']['quality_override']:item['downloads']['quality']=default_quality
            item['downloads']=channel_downloads.resolve_defaults(store,item['downloads'])
            item['disk_bytes'] = sizes.get(item['id'],0)
            item['task'] = tasks.get(item['id'])
            with store.connect() as db:
                stats=db.execute("SELECT count(*) AS indexed_videos,MAX(CASE WHEN json_extract(data,'$.state')='published' THEN published END) AS latest_upload FROM videos WHERE channel=?",(item['id'],)).fetchone()
                saved=db.execute('SELECT count(*) FROM media_files WHERE channel=? AND bytes>0',(item['id'],)).fetchone()[0]
            item.update(dict(stats));item['saved_count']=saved
            item.update(channel_metrics(item['id']))
            from .youtube_source import fallback
            item['can_manage_youtube']=not fallback(store)
            items.append(item)
        from .history import coverage
        return dict(items=cached_art(coverage(store,items)), more=len(rows)>15, total=total, shown=shown, csrf=csrf())

    @app.route('/api/m3u', methods=['GET', 'POST'])
    def m3u_subscriptions():
        denied = guide_access(local_action=True)
        if denied: return denied
        if request.method == 'POST':
            if not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), csrf()): return error('Reload the page.', 403)
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict): return error('Send subscription settings.')
            try:
                action = payload.get('action', 'save')
                if action == 'metadata-settings':
                    from .m3u_metadata import save_credential
                    save_credential(store, payload)
                    return jsonify(saved=bool(store.get('m3u_tmdb_token')))
                if action == 'preview-naming':
                    return jsonify(items=m3u.preview_naming(payload))
                if action == 'save':
                    source_id = m3u.save(store, payload)
                elif action == 'sync':
                    source_id = str(payload.get('id', ''))
                    m3u.request_sync(store, source_id)
                elif action == 'dry-run':
                    source_id = str(payload.get('id', ''))
                    m3u.request_dry_run(store, source_id)
                elif action == 'metadata':
                    from .m3u_metadata import request as request_metadata
                    source_id = str(payload.get('id', ''))
                    request_metadata(store, source_id)
                elif action == 'remove':
                    source_id = str(payload.get('id', ''))
                    with store.connect() as db:
                        db.execute('BEGIN IMMEDIATE')
                        row = db.execute('SELECT state FROM m3u_sources WHERE id=?', (source_id,)).fetchone()
                        checking = db.execute("SELECT 1 FROM m3u_dry_runs WHERE source=? AND state IN ('queued','running') UNION ALL SELECT 1 FROM m3u_metadata_runs WHERE source=? AND state IN ('queued','running')", (source_id,source_id)).fetchone()
                        if not row or row['state'] == 'running' or checking: raise ValueError('Wait for the import or dry run to finish before removing it.')
                        if payload.get('delete_files') is True:
                            from .m3u_removal import queue
                            queue(db, source_id)
                            return jsonify(id=source_id, removing=True)
                        db.execute('DELETE FROM m3u_sources WHERE id=?', (source_id,))
                        db.execute('DELETE FROM m3u_files WHERE source=?', (source_id,))
                        db.execute('DELETE FROM m3u_file_info WHERE source=?', (source_id,))
                        db.execute('DELETE FROM m3u_file_aliases WHERE source=?', (source_id,))
                        db.execute('DELETE FROM m3u_runs WHERE source=?', (source_id,))
                        db.execute('DELETE FROM m3u_dry_runs WHERE source=?', (source_id,))
                        db.execute('DELETE FROM m3u_metadata_runs WHERE source=?', (source_id,))
                        db.execute('DELETE FROM m3u_matches WHERE source=?', (source_id,))
                        db.execute('DELETE FROM m3u_removals WHERE source=?', (source_id,))
                        db.execute('DELETE FROM xtream_accounts WHERE source=?', (source_id,))
                        db.execute('DELETE FROM xtream_series_cache WHERE source=?', (source_id,))
                        db.execute('DELETE FROM xtream_vod_info WHERE source=?', (source_id,))
                else: raise ValueError('Choose a subscription action.')
            except ValueError as exc: return error(str(exc))
            return jsonify(id=source_id)
        return jsonify(items=m3u.subscriptions(store, app.config['M3U_DIR']), root=app.config['M3U_DIR'], defaults=m3u.DEFAULTS, tmdb_saved=bool(store.get('m3u_tmdb_token')))

    @app.get('/api/m3u/<source_id>/metadata-status')
    def m3u_metadata_status(source_id):
        denied = guide_access(local_action=True)
        if denied: return denied
        with store.connect() as db:
            if not db.execute('SELECT 1 FROM m3u_sources WHERE id=?', (source_id,)).fetchone(): return error('Subscription not found.', 404)
            row = db.execute('SELECT * FROM m3u_metadata_runs WHERE source=?', (source_id,)).fetchone()
        result = dict(row) if row else dict(state='idle', message='Ready to match missing years and dated episodes.', report='{}')
        result['report'] = json.loads(result['report'])
        return jsonify(result)

    @app.get('/api/m3u/<source_id>/dry-run')
    def m3u_dry_run_status(source_id):
        denied = guide_access(local_action=True)
        if denied: return denied
        with store.connect() as db:
            row = db.execute('SELECT * FROM m3u_dry_runs WHERE source=?', (source_id,)).fetchone()
        if not row: return jsonify(state='idle', phase='idle', message='Start a dry run to preview this subscription.', report={})
        result = dict(row); result['report'] = json.loads(result['report'])
        return jsonify(result)

    from .m3u_player import register as register_m3u_player
    register_m3u_player(app, store, guide_access)
    from .m3u_downloads import register as register_m3u_downloads
    register_m3u_downloads(app, store, guide_access)
    from .strm import register as register_strm
    register_strm(app, store, guide_access)
    from .m3u_overrides import register as register_m3u_overrides
    register_m3u_overrides(app, store, guide_access)
    from .xtream import register as register_xtream
    register_xtream(app, store, guide_access)
    from .torrents import register as register_torrents
    register_torrents(app, store, guide_access)
    from .nzb import register as register_nzb
    register_nzb(app, store, guide_access)
    from .media_players import register as register_media_players
    register_media_players(app, store, guide_access, csrf)
    from .discover import register as register_discover
    register_discover(app, store, guide_access, csrf)
    from .source_alternatives import register as register_source_alternatives
    register_source_alternatives(app,store,guide_access,csrf)
    from .quick_download import register as register_quick_download
    register_quick_download(app,store,guide_access,csrf)
    from .media_management import register as register_management
    register_management(app, store, guide_access, csrf)
    from .discovery_engine import register as register_discovery_engine
    register_discovery_engine(app,store,guide_access,csrf)
    from .view_media import register as register_view_media
    register_view_media(app, store, guide_access)
    from .favourites import register as register_favourites
    register_favourites(app, store, guide_access, csrf)
    from .watchlist import register as register_watchlist
    register_watchlist(app, store, guide_access, csrf)
    from .activity_clear import initialise as initialise_activity_clear
    initialise_activity_clear(store)

    @app.get('/api/m3u/<source_id>/file')
    def m3u_file(source_id):
        denied = guide_access(local_action=True)
        if denied: return denied
        relative = request.args.get('path', '')
        with store.connect() as db:
            row = db.execute('SELECT s.folder FROM m3u_files f JOIN m3u_sources s ON s.id=f.source WHERE f.source=? AND f.path=?', (source_id, relative)).fetchone()
        if not row: return error('File not found.', 404)
        try: path = m3u.contained(source_base(store, app.config['M3U_DIR'], row['folder']), relative)
        except ValueError: return error('File unavailable.', 404)
        if not path.is_file(): return error('File not found.', 404)
        return send_file(path, as_attachment=True, download_name=path.name, mimetype='application/octet-stream')

    @app.get('/api/m3u/<source_id>/entries')
    def m3u_entries(source_id):
        denied = guide_access(local_action=True)
        if denied: return denied
        query = request.args.get('q', '')[:150]
        pattern = '%' + query.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
        try: offset = max(0, min(250000, int(request.args.get('offset', 0))))
        except ValueError: return error('Choose a valid page.')
        kind = request.args.get('kind', '')
        orders = {'title':'title COLLATE NOCASE', 'title-desc':'title COLLATE NOCASE DESC',
                  'file':'f.path COLLATE NOCASE', 'file-desc':'f.path COLLATE NOCASE DESC',
                  'group':'group_name COLLATE NOCASE,title COLLATE NOCASE',
                  'type':'kind,title COLLATE NOCASE',
                  'year':'year IS NULL,year,title COLLATE NOCASE',
                  'year-desc':'year IS NULL,year DESC,title COLLATE NOCASE'}
        orders.update({'added':'c.added DESC','release':'release_date IS NULL,release_date DESC','rating':'rating IS NULL,rating DESC','random':'shuffle(f.path)'})
        sort = request.args.get('sort', 'title')
        if sort not in orders: return error('Choose a valid sort option.')
        from . import m3u_catalogue
        # The worker catalogues new files; only index here if this source has
        # files it has not reached yet (for example straight after an import).
        with store.connect() as db:
            stale=m3u_catalogue.needs_index(db,source_id)
        if stale:m3u_catalogue.index(store)
        with store.connect() as db:
            seed=request.args.get('seed','reeldock')[:80]
            db.create_function('shuffle',1,lambda path:__import__('hashlib').sha256((seed+path).encode()).hexdigest())
            rows = db.execute("""SELECT f.source,f.title,f.kind,f.path,f.group_name,c.key AS catalog_key,t.data AS metadata,
                COALESCE(NULLIF(c.release_date,''),json_extract(t.data,'$.release_date')) AS release_date,
                json_extract(t.data,'$.rating') AS rating,c.added,
                CASE WHEN CAST(json_extract(i.data,'$.info.year') AS INTEGER) BETWEEN 1800 AND 2200
                     THEN CAST(json_extract(i.data,'$.info.year') AS INTEGER)
                     WHEN f.title GLOB '*([12][0-9][0-9][0-9])' THEN CAST(substr(f.title,-5,4) AS INTEGER)
                     WHEN f.title GLOB '*[12][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' THEN CAST(substr(f.title,-10,4) AS INTEGER)
                     ELSE NULL END AS year
                FROM m3u_files f LEFT JOIN m3u_file_info i ON i.source=f.source AND i.path=f.path
                LEFT JOIN m3u_catalog_entries c ON c.source=f.source AND c.path=f.path
                LEFT JOIN m3u_catalog_titles t ON t.key=c.key
                WHERE f.source=? AND f.title LIKE ? ESCAPE '!' AND (?='' OR f.kind=?)
                ORDER BY """ + orders[sort] + ',f.path LIMIT 51 OFFSET ?', (source_id, pattern, kind, kind, offset)).fetchall()
            runs = db.execute('SELECT finished,state,message,stats FROM m3u_runs WHERE source=? ORDER BY id DESC LIMIT 5', (source_id,)).fetchall()
            items=m3u_catalogue.decorate(db,rows[:50])
        return jsonify(items=items, more=len(rows)>50, runs=[dict(r) for r in runs])

    @app.get('/api/m3u-highlights')
    def m3u_highlights():
        denied=guide_access(local_action=True)
        if denied:return denied
        from .m3u_catalogue import cached_shelves
        return jsonify(cached_shelves(store))

    @app.route('/api/youtube-sync',methods=['GET','POST'])
    def youtube_sync_control():
        denied=guide_access(local_action=True)
        if denied:return denied
        if request.method=='POST':
            if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return error('Reload the page.',403)
            body=request.get_json(silent=True)
            if not isinstance(body,dict) or type(body.get('paused')) is not bool:return error('Choose on or off.')
            from . import settings
            settings.save(store, 'youtube', {'sync_paused': body['paused']})
        return jsonify(paused=store.get('youtube_sync_paused')=='true')

    @app.get('/api/overview')
    def overview_stats():
        denied=guide_access(local_action=True)
        if denied:return denied
        from .system_stats import snapshot
        return jsonify(snapshot(store,app.config['MEDIA_DIR'],VERSION,started))

    @app.post('/api/channel-favourite/<channel>')
    def favourite_channel(channel):
        denied=guide_access(local_action=True)
        if denied:return denied
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return error('Reload the page.',403)
        body=request.get_json(silent=True)
        if not isinstance(body,dict) or type(body.get('favourite')) is not bool:return error('Choose on or off.')
        with store.connect() as db:
            if not db.execute('SELECT 1 FROM channels WHERE id=? AND active=1',(channel,)).fetchone():return error('Channel not found.',404)
            if body['favourite']:db.execute('INSERT OR IGNORE INTO channel_favourites VALUES (?)',(channel,))
            else:db.execute('DELETE FROM channel_favourites WHERE id=?',(channel,))
        return jsonify(id=channel,favourite=body['favourite'])

    @app.get('/api/channel-tasks')
    def channel_task_status():
        denied=guide_access(local_action=True)
        if denied:return denied
        ids=request.args.get('ids','').split(',')[:50]
        with store.connect() as db:
            tasks={r['channel']:dict(r) for r in db.execute('SELECT * FROM channel_tasks') if r['channel'] in ids}
        return jsonify(tasks=tasks,sizes=media_library.disk_usage(store,app.config['MEDIA_DIR'],ids))

    @app.post('/api/channel-downloads/<channel>')
    def channel_download_controls(channel):
        denied=guide_access(local_action=True)
        if denied:return denied
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return error('Reload the page.',403)
        body=request.get_json(silent=True)
        if not isinstance(body,dict):return error('Choose a channel setting.')
        with store.connect() as db:
            if not db.execute('SELECT 1 FROM channels WHERE id=? AND active=1',(channel,)).fetchone():return error('Channel not found.',404)
        try:
            if 'settings' in body:
                result=channel_downloads.save(store,channel,body['settings'])
                return jsonify(downloads=result)
            action=body.get('action')
            if action=='unsubscribe_youtube':
                from .youtube_source import enabled
                if not enabled(store) or float(store.get('google_quota_retry_at','0'))>time.time():return error('Unsubscribing on YouTube needs Google API and available allowance. You can still remove the channel locally.',409)
                if body.get('confirm') is not True:return error('Confirm unsubscribing on YouTube first.')
                tokens=json.loads(store.get('google_tokens') or '{}')
                scopes=set(tokens.get('scope','').split())
                if not scopes.intersection({'https://www.googleapis.com/auth/youtube','https://www.googleapis.com/auth/youtube.force-ssl'}):
                    return jsonify(authorise=True,message='Google permission is needed to manage subscriptions.')
                from .catalogue import Importer
                importer=Importer(app)
                result=importer.api('subscriptions',part='id',mine='true',forChannelId=channel,maxResults=50)
                for subscription in result.get('items',[]):
                    importer.api('subscriptions',_method='DELETE',_cost=50,id=subscription['id'])
                with store.connect() as db:
                    db.execute('UPDATE channels SET active=0 WHERE id=?',(channel,))
                    db.execute('UPDATE download_rules SET videos=0,shorts=0,live=0 WHERE channel=?',(channel,))
                    db.execute("UPDATE downloads SET state='cancelled' WHERE state='queued' AND id IN (SELECT id FROM videos WHERE channel=?)",(channel,))
                store.set('next_sync','0')
                return jsonify(message='Unsubscribed on YouTube. Downloaded files were kept; auto downloads are off.')

            if action in ('redownload_existing','delete_media','remove_channel','remove_all') and body.get('confirm') is not True:
                return error('Confirm this channel action first.')
            channel_downloads.queue_action(store,channel,action)
            return jsonify(message='Channel action queued. Refresh this list to see the result.')
        except ValueError as exc:return error(str(exc))

    @app.get('/subscriptions')
    def subscriptions():
        if not logged_in():
            return redirect('/login')
        if not store.get('setup_complete'):
            return redirect('/setup')
        return redirect('/app/?view=YouTube')

    @app.route('/api/subscriptions', methods=['GET', 'POST'])
    def subscription_data():
        denied = guide_access()
        if denied:
            return denied
        if request.method == 'GET':
            try:
                offset = max(0,min(int(request.args.get('offset',0)),1000000))
                only = request.args.get('favourites','0')
                if only not in ('0','1'):
                    return error('Choose all channels or favourites only.')
                channel=request.args.get('channel','')[:128]
                return jsonify(subscription_page('' if channel else request.args.get('q',''),offset,channel,request.args.get('sort','favourites'),only=='1'))
            except ValueError:
                return error('Choose a valid channel page and order.')
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):
            return error('Reload subscriptions and try again.',403)
        payload = request.get_json(silent=True)
        if not isinstance(payload,dict):
            return error('Choose a channel preference.')
        fields = [key for key in ('shown', 'forecasts') if key in payload]
        if len(fields) != 1 or type(payload[fields[0]]) is not bool:
            return error('Choose one channel preference to change.')
        field = fields[0]
        ids = payload.get('ids')
        all_channels = payload.get('all') is True
        if not all_channels and (not isinstance(ids,list) or not 1 <= len(ids) <= 50 or not all(isinstance(i,str) for i in ids)):
            return error('Choose up to 50 channels or all channels.')
        with store.connect() as db:
            active = {r[0] for r in db.execute('SELECT id FROM channels WHERE active=1')}
            selected = active if all_channels else set(ids)
            if not selected <= active:
                return error('The channel list changed. Reload subscriptions and try again.')
            if field == 'shown':
                db.executemany('INSERT INTO channel_preferences VALUES (?,?) ON CONFLICT(id) DO UPDATE SET shown=excluded.shown',
                               [(channel,int(payload[field])) for channel in selected])
            else:
                db.executemany('INSERT INTO channel_forecast_preferences VALUES (?,?) ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled',
                               [(channel,int(payload[field])) for channel in selected])
            shown = db.execute('SELECT COUNT(*) FROM channels c LEFT JOIN channel_preferences p ON p.id=c.id WHERE c.active=1 AND COALESCE(p.shown,1)=1').fetchone()[0]
        return jsonify(ok=True, shown=shown, total=len(active))

    @app.post('/google/reconnect')
    def reconnect_google():
        if not logged_in() or not hmac.compare_digest(request.form.get('csrf', ''), csrf()):
            return error('Sign in and reload the Guide first.', 403)
        if store.get('sync_running') == '1' or store.get('history_running') == '1':
            return render_template('message.html', title='Import in progress', message='Wait for the current import to finish, then reconnect Google.'), 409
        # Keep the existing working connection until a replacement succeeds.
        store.set('reconnect_pending', '1')
        return redirect('/setup')

    def cached_art(items):
        with store.connect() as db:favourites={r[0] for r in db.execute('SELECT id FROM channel_favourites')}
        for item in items:
            if 'name' in item:item['favourite']=item.get('id') in favourites
        videos=[item for item in items if item.get('state') in ('published','live','scheduled')]
        saved=media_library.download_status(store,app.config['MEDIA_DIR'],[item['id'] for item in videos]) if videos else {}
        for item in videos:item['downloaded']=saved.get(item['id'],False)
        return items

    @app.post('/api/guide/week')
    def request_guide_week():
        denied=guide_access(local_action=True)
        if denied:return denied
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):
            return error('Reload the Guide and try again.',403)
        from .guide_weeks import request_week, status
        body=request.get_json(silent=True) or {}
        if not isinstance(body,dict):return error('Choose a valid week.')
        try:
            start=body.get('start','')
            request_week(store,start,foreground=True)
        except (ValueError,TypeError):return error('Choose a valid week beginning on Monday.')
        return jsonify(notice=status(store,start))

    @app.get('/api/guide')
    def guide_data():
        denied = guide_access()
        if denied:
            return denied
        from datetime import date
        try:
            start, end = date.fromisoformat(request.args['start']), date.fromisoformat(request.args['end'])
            if not 0 <= (end-start).days <= 31:
                raise ValueError()
        except (KeyError, ValueError):
            return error('Choose a date range of up to 31 days.')
        with store.connect() as db:
            channels = [json.loads(r[0]) for r in db.execute('SELECT c.data FROM channels c LEFT JOIN channel_preferences p ON p.id=c.id WHERE c.active=1 AND COALESCE(p.shown,1)=1 ORDER BY c.id')]
            videos = [json.loads(r[0]) for r in db.execute('SELECT v.data FROM videos v JOIN channels c ON c.id=v.channel WHERE c.active=1 AND NOT EXISTS(SELECT 1 FROM channel_preferences p WHERE p.id=c.id AND p.shown=0) AND v.day BETWEEN ? AND ? ORDER BY v.published DESC LIMIT 5000', (start.isoformat(), end.isoformat()))]
            expected = [json.loads(r[0]) for r in db.execute('SELECT f.data FROM forecasts f JOIN channels c ON c.id=f.channel WHERE c.active=1 AND NOT EXISTS(SELECT 1 FROM channel_preferences p WHERE p.id=c.id AND p.shown=0) AND NOT EXISTS(SELECT 1 FROM channel_forecast_preferences fp WHERE fp.id=c.id AND fp.enabled=0) AND f.day BETWEEN ? AND ?', (start.isoformat(), end.isoformat()))]
        from datetime import datetime, timezone
        expected = [item for item in expected if datetime.fromisoformat(item['window_end']) > datetime.now(timezone.utc)]
        if time.time()-float(store.get('forecasts_built','0')) > 48*3600:
            expected = []
        # A newly imported real video takes precedence even while forecasts rebuild.
        expected = [item for item in expected if not any(v['channel']==item['channel'] and v['date']==item['date'] and abs(int(v['time'][:2])*60+int(v['time'][3:])-int(item['time'][:2])*60-int(item['time'][3:])) <= 180 for v in videos)]
        from .history import coverage
        from .channel_sort import add_sort_metrics
        from .guide_weeks import status as week_status
        from .movie_options import guide as movie_guide
        from .show_options import guide as show_guide
        return jsonify(notice=week_status(store,start.isoformat()),channels=cached_art(coverage(store,add_sort_metrics(store,channels))), uploads=cached_art(videos), forecasts=expected, movies=movie_guide(store,start.isoformat(),end.isoformat()),shows=show_guide(app,store,start.isoformat(),end.isoformat()),truncated=len(videos)==5000, csrf=csrf())

    @app.post('/api/setup/cookies')
    @app.route('/api/youtube/cookies', methods=['GET', 'POST'])
    def youtube_cookies():
        denied = (None if logged_in() else error('Sign in first.',401)) if request.path.startswith('/api/setup/') else guide_access(local_action=True)
        if denied:
            return denied
        from .cookies import LIMIT, save
        target = store.directory / 'youtube-cookies.txt'
        if request.method == 'POST':
            if not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), csrf()):
                return error('Reload the Guide and try again.', 403)
            request.max_content_length = LIMIT + 16384
            if request.form.get('action') == 'remove':
                target.unlink(missing_ok=True)
                store.set('youtube_cookie_subscriptions_status','')
            else:
                upload = request.files.get('cookies')
                if not upload:
                    return error('Choose a cookies.txt file first.')
                try:
                    save(store.directory, upload.stream.read(LIMIT + 1))
                    store.set('emby_cookie_check', 'queued')
                    store.set('youtube_cookie_subscriptions_next','0')
                    store.set('next_sync','0')
                except ValueError as exc:
                    return error(str(exc))
        return jsonify(saved=target.is_file(),import_status=store.get('youtube_cookie_subscriptions_status'))

    def catalogue_video(video_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            return None
        with store.connect() as db:
            row = db.execute('SELECT v.data FROM videos v JOIN channels c ON c.id=v.channel WHERE v.id=? AND c.active=1', (video_id,)).fetchone()
        if row:return json.loads(row[0])
        with store.connect() as db:
            discovered = db.execute("SELECT data FROM videos WHERE id=? AND json_extract(data,'$.discover')=1", (video_id,)).fetchone()
        if discovered:
            return json.loads(discovered[0])
        from .local_library import entry
        saved=entry(store,app.config['MEDIA_DIR'],video_id)
        return saved['video'] if saved and saved['bytes'] else None

    def local_video(video_id):
        return media_library.local_file(store,app.config['MEDIA_DIR'],video_id)

    def channel_metrics(channel):
        with store.connect() as db:
            rows = db.execute("SELECT data FROM videos WHERE channel=? AND json_extract(data,'$.state')='published' ORDER BY published DESC,id LIMIT 20", (channel,)).fetchall()
        views = [json.loads(row[0]).get('views') for row in rows]
        views = [int(value) for value in views if value is not None and str(value).isdigit()]
        return dict(recent_average=sum(views)/len(views) if views else None, recent_sample=len(views))

    @app.get('/api/guide/channel/<channel>/info')
    def channel_info(channel):
        denied = guide_access()
        if denied:
            return denied
        with store.connect() as db:
            row = db.execute('SELECT data FROM channels WHERE id=?', (channel,)).fetchone()
        if not row:
            from .youtube_discover import public_channel_details
            try:return jsonify(channel=public_channel_details(store,channel),csrf=csrf())
            except ValueError:return error('Channel not found.',404)
        item = json.loads(row[0])
        item.update(channel_metrics(channel))
        from .youtube_source import fallback
        item['can_manage_youtube']=not fallback(store)
        from .channel_metadata_refresh import status as metadata_status
        return jsonify(channel=cached_art([item])[0], csrf=csrf(),metadata=metadata_status(store,channel))

    @app.get('/api/guide/channel/<channel>/history')
    def channel_history(channel):
        denied = guide_access(local_action=True)
        if denied:
            return denied
        from .channel_details import history_page, valid_channel
        try:
            limit = int(request.args.get('limit', 20))
            offset = int(request.args.get('offset', 0))
        except ValueError:
            return error('Choose a valid history page.')
        if not valid_channel(channel) or not 1 <= limit <= 50 or not 0 <= offset <= 1000000:
            return error('Choose a valid history page.')
        return jsonify(history_page(store, channel, limit, offset))

    @app.get('/api/guide/channel/<channel>/forecasts')
    def channel_forecasts(channel):
        denied = guide_access(local_action=True)
        if denied:
            return denied
        from .channel_details import forecasts, valid_channel
        if not valid_channel(channel):
            return error('Choose a valid channel.')
        return jsonify(forecasts(store, channel))

    @app.route('/api/youtube/watched-matching', methods=['GET','POST'])
    def youtube_watched_matching():
        from . import youtube_matching as matching
        denied=guide_access(local_action=True)
        if denied:return denied
        if request.method=='POST':
            if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return error('Reload the page.',403)
            body=request.get_json(silent=True)
            if not isinstance(body,dict):return error('Invalid watched matching request.')
            try:
                action=body.get('action')
                if action=='preferences':matching.save(store,body.get('values'))
                elif action=='retry':matching.retry_watched(store,body.get('id'))
                elif action=='check_cookies':
                    store.set('emby_cookie_check','queued')
                    store.set('emby_cookie_status','YouTube cookie check queued. This check does not send a watched update.')
                elif action=='sync':
                    if store.get('emby_watched')!='true':raise ValueError('Enable watched matching first.')
                    store.set('emby_watched_paused','')
                    store.set('emby_next_check','0')
                else:raise ValueError('Choose a supported watched matching action.')
            except ValueError as exc:return error(str(exc))
        return jsonify(**matching.settings(store))

    @app.route('/api/emby', methods=['GET','POST'])
    def emby_settings():
        denied=guide_access(local_action=True)
        if denied:return denied
        if request.method=='POST':
            if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return error('Reload the page.',403)
            body=request.get_json(silent=True)
            if not isinstance(body,dict):return error('Invalid Emby request.')
            try:
                if body.get('action') in ('test','save'):
                    emby.connect(store,body)
                elif body.get('action')=='preferences':emby.save_preferences(store,body.get('preferences'))
                elif body.get('action')=='retry_watched':emby.retry_watched(store,body.get('id'))
                elif body.get('action')=='check_cookies':
                    store.set('emby_cookie_check','queued')
                    store.set('emby_cookie_status','YouTube cookie check queued on the NAS. No watched request will be sent by this test.')
                elif body.get('action')=='sync':
                    if store.get('emby_watched')!='true':return error('Enable and save watched matching first.')
                    store.set('emby_watched_paused','')
                    store.set('emby_next_check','0')
                else:return error('Unknown Emby action.')
            except ValueError as exc:return error(str(exc))
        data=emby.settings(store)
        from .local_library import entry
        for attempt in data['attempts']:
            saved=entry(store,app.config['MEDIA_DIR'],attempt['id'])
            if saved:attempt.update({k:v for k,v in saved.items() if k not in ('id','updated','created')})
        return jsonify(**data)

    @app.get('/api/download-status')
    def downloaded_status():
        denied=guide_access(local_action=True)
        if denied:return denied
        ids=request.args.get('ids','').split(',')
        if len(ids)>100 or any(not re.fullmatch(r'[A-Za-z0-9_-]{11}',i) for i in ids):return error('Choose up to 100 videos.')
        with store.connect() as db:
            marks=','.join('?' for _ in ids)
            jobs={row['id']:row['state'] for row in db.execute(
                'SELECT id,state FROM downloads WHERE id IN ('+marks+')',ids)}
        with store.connect() as db:
            details=cached_art([json.loads(r[0]) for r in db.execute('SELECT data FROM videos WHERE id IN ('+marks+')',ids)])
        return jsonify(downloaded=media_library.download_status(store,app.config['MEDIA_DIR'],ids),jobs=jobs,details=details)

    @app.post('/api/activity/subscription-return')
    def subscription_return():
        denied=guide_access(local_action=True)
        if denied:return denied
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return error('Reload the page.',403)
        body=request.get_json(silent=True)
        if not isinstance(body,dict) or type(body.get('accept')) is not bool or not isinstance(body.get('channel'),str) or not isinstance(body.get('subscription'),str):return error('Choose a subscription alert.')
        from .subscription_returns import decide
        try:decide(store,body['channel'],body['subscription'],body['accept'])
        except ValueError as exc:return error(str(exc),409)
        return jsonify(ok=True)

    @app.post('/api/activity/dismiss')
    def dismiss_activity():
        denied=guide_access(local_action=True)
        if denied:return denied
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return error('Reload the page.',403)
        body=request.get_json(silent=True)
        if not isinstance(body,dict):return error('Choose an alert.')
        identity=body.get('id')
        if not isinstance(identity,str) or not 1<=len(identity)<=500:return error('Choose an alert.')
        identities=[identity]
        if identity.startswith('download-group-'):
            from .activity import snapshot
            group=next((a for a in snapshot(store)['alerts'] if a['id']==identity),None)
            if not group:return error('This alert changed. Reload and try again.',409)
            identities+=group.get('members',[])
        with store.connect() as db:db.executemany('INSERT OR REPLACE INTO dismissed_alerts VALUES (?,?)',[(key,time.time()) for key in identities])
        return jsonify(ok=True)

    @app.post('/api/activity/clear')
    def clear_activity_section():
        denied=guide_access(local_action=True)
        if denied:return denied
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return error('Reload the page.',403)
        body=request.get_json(silent=True)
        if not isinstance(body,dict) or set(body)!={'section'} or body.get('section') not in ('attention','completed','logs'):
            return error('Choose Needs attention, Completed or Clear Logs.')
        from .activity_clear import clear, native_jobs
        try:return jsonify(clear(store,app.config['MEDIA_DIR'],body['section'],native_jobs(app)))
        except ValueError as exc:return error(str(exc))

    @app.post('/api/activity/clear-item')
    def clear_activity_item():
        denied=guide_access(local_action=True)
        if denied:return denied
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return error('Reload the page.',403)
        body=request.get_json(silent=True)
        from .activity_items import KINDS, clear_one
        from .activity_observation import valid
        if isinstance(body,dict) and 'observed' not in body:return error('Refresh Activity before clearing this item.')
        if not isinstance(body,dict) or set(body)!={'kind','id','observed'} or body.get('kind') not in KINDS or type(body.get('id')) not in (str,int) or not 1<=len(str(body['id']))<=500:
            return error('Choose an Activity item.')
        if not valid(body['observed']):return error('Refresh Activity before clearing this item.')
        try:return jsonify(clear_one(app,store,body['kind'],body['id'],observed=body['observed']))
        except ValueError as exc:return error(str(exc),getattr(exc,'status',400))

    @app.route('/api/youtube-schedule',methods=['GET','POST'])
    def youtube_schedule():
        denied=guide_access(local_action=True)
        if denied:return denied
        from .sync_schedule import read,save
        if request.method=='POST':
            if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return error('Reload the page.',403)
            body=request.get_json(silent=True)
            if not isinstance(body,dict):return error('Choose an upload check interval.')
            try:save(store,body.get('minutes'))
            except ValueError as exc:return error(str(exc))
        return jsonify(read(store))

    @app.get('/api/activity')
    def activity():
        denied=guide_access()
        if denied:return denied
        with store.connect() as db:
            row=db.execute("SELECT id,title,phase,progress,quality,message,processing_info FROM downloads WHERE state IN ('downloading','cancelling') ORDER BY created,id LIMIT 1").fetchone()
            queued=db.execute("SELECT count(*) FROM downloads WHERE state='queued'").fetchone()[0]
        item=dict(row) if row else None
        if item:item['phase_label']={'encoding':'Encoding with FFmpeg','merging':'Merging audio and video','sponsors':'Removing sponsor sections','metadata':'Saving metadata','preparing':'Preparing','processing':'Processing'}.get(item['phase'],'Downloading')
        if item:
            with store.connect() as db:
                saved=db.execute('SELECT v.data,c.data FROM videos v LEFT JOIN channels c ON c.id=v.channel WHERE v.id=?',(item['id'],)).fetchone()
            video=json.loads(saved[0]) if saved else {}
            channel=json.loads(saved[1]) if saved and saved[1] else {}
            item.update(video=video,channel=channel.get('id',''),channel_name=channel.get('name',''),avatar=channel.get('avatar',''))
            item=cached_art([item])[0]

        from .cookie_recovery import warning
        cookie_warning=warning(store)
        with store.connect() as db:
            m3u_task = db.execute("SELECT name,message FROM m3u_sources WHERE state IN ('running','queued') ORDER BY state DESC LIMIT 1").fetchone()
        from .activity import snapshot as activity_snapshot
        return jsonify(**activity_snapshot(store),m3u=dict(m3u_task) if m3u_task else None,cookie_warning=cookie_warning,download=item,queued=queued,syncing=store.get('sync_running')=='1' or store.get('sync_requested')=='1',sync_message=store.get('sync_message','Checking for uploads'))

    @app.get('/api/downloads/<video_id>')
    def video_download_status(video_id):
        denied=guide_access(local_action=True)
        if denied:return denied
        video=catalogue_video(video_id) if re.fullmatch(r'[A-Za-z0-9_-]{11}',video_id) else None
        if not video:return error('Video not found in your saved catalogue.',404)
        with store.connect() as db:
            row=db.execute('SELECT state,progress,message FROM downloads WHERE id=?',(video_id,)).fetchone()
        response=jsonify(job=dict(row) if row else None,saved=bool(local_video(video_id)),
            quality=channel_downloads.preferences(store,video['channel'])['quality'],
            worker_online=system_stats.worker_online(store.get('worker_heartbeat','0')))
        response.headers['Cache-Control']='no-store'
        return response

    from .vpn import register as register_vpn
    register_vpn(app, guide_access)

    @app.route('/api/settings/<engine>', methods=['GET', 'POST'])
    def engine_settings(engine):
        denied = guide_access(local_action=True)
        if denied: return denied
        from . import settings
        try:
            settings.module(engine)
        except KeyError:
            return error('Unknown settings engine.', 404)
        if request.method == 'POST':
            if not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), csrf()):
                return error('Reload the page before changing settings.', 403)
            body = request.get_json(silent=True)
            if not isinstance(body, dict) or set(body) != {'values'}:
                return error('Choose supported settings.')
            try:
                values = settings.save(store, engine, body['values'])
            except settings.Conflict as exc:
                return error(str(exc), 409)
            except ValueError as exc:
                return error(str(exc))
        else:
            try:
                values = settings.read(store, engine)
            except settings.Conflict as exc:
                return error(str(exc), 409)
        response = jsonify(engine=engine, values=values)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.route('/api/downloads', methods=['GET', 'POST'])
    def download_queue():
        denied = guide_access(local_action=True)
        if denied:
            return denied
        if request.method == 'POST':
            if not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), csrf()):
                return error('Reload the page before changing downloads.', 403)
            body = request.get_json(silent=True)
            if not isinstance(body, dict):
                return error('Invalid download request.')
            action = body.get('action', 'queue')
            from . import download_options
            quality = body.get('quality', store.get('download_quality', '1080'))
            if action == 'options':
                try:download_options.save(store,body.get('options'))
                except ValueError as exc:return error(str(exc))
            elif action == 'metadata':
                profile=body.get('metadata_type')
                if profile not in media_library.PROFILES:return error('Choose Emby, Plex or Kodi.')
                store.set('metadata_type',profile)
            elif action == 'quality':
                if quality not in downloads.QUALITIES:
                    return error('Choose a supported quality.')
                from . import settings
                settings.save(store, 'youtube', {'quality': quality})
            elif action in ('queue', 'retry'):
                video_id = body.get('id')
                if not isinstance(video_id, str):
                    return error('Choose a saved video.')
                video = catalogue_video(video_id)
                if not video:
                    return error('Video not found in your saved catalogue.', 404)
                if local_video(video_id):
                    return error('This video is already saved on your NAS.')
                if video.get('browse_only') and video.get('state')=='unknown' and video.get('metadata_pending'):
                    return error('Video details are still being checked. Try again shortly.',409)
                # Channel settings apply to manual requests too (date range and types).
                refused=channel_downloads.refusal(store,video)
                if refused:
                    return error(refused,409)
                try:
                    with store.connect() as db:
                        previous=db.execute('SELECT quality FROM downloads WHERE id=?',(video_id,)).fetchone() if action=='retry' else None
                    quality=previous['quality'] if previous else body.get('quality',channel_downloads.preferences(store,video['channel'])['quality'])
                    from .media_holds import resume
                    resume(store,['video:'+video_id,'managed:video:'+video_id])
                    downloads.enqueue(store, video, quality)
                except ValueError as exc:
                    return error(str(exc))
            elif action == 'stop_all':
                from .acquisition import stop_all
                return jsonify(stop_all(app, store))
            elif action == 'resume_downloads':
                from .acquisition import resume
                resume(store)
                return jsonify(held=False, message='Downloads can start again.')
            elif action == 'cancel_all':
                from .acquisition import stop_all
                return jsonify(stop_all(app, store))
            elif action == 'cancel':
                video_id = body.get('id')
                if not isinstance(video_id, str):
                    return error('Choose a download.')
                with store.connect() as db:
                    db.execute("UPDATE downloads SET state=CASE WHEN state='queued' THEN 'cancelled' ELSE 'cancelling' END,updated=? WHERE id=? AND state IN ('queued','downloading')", (time.time(), video_id))
            else:
                return error('Unknown download action.')
        # The Activity feed polls this every few seconds. Return every active row
        # (the queue order matters) but only the newest finished rows; the full
        # history stays available through /api/download-history.
        with store.connect() as db:
            rows = db.execute("""SELECT d.*,v.data AS video_data,c.data AS channel_data FROM downloads d
                LEFT JOIN videos v ON v.id=d.id LEFT JOIN channels c ON c.id=v.channel
                WHERE d.state IN ('queued','downloading','cancelling')
                   OR d.id IN (SELECT id FROM downloads WHERE state='complete' ORDER BY updated DESC,id LIMIT ?)
                   OR d.id IN (SELECT id FROM downloads WHERE state NOT IN ('complete','queued','downloading','cancelling') ORDER BY updated DESC,id LIMIT ?)
                ORDER BY CASE WHEN d.state IN ('downloading','cancelling') THEN 0 WHEN d.state='queued' THEN 1 ELSE 2 END,
                CASE WHEN d.state IN ('queued','downloading','cancelling') THEN d.created END ASC,
                CASE WHEN d.state NOT IN ('queued','downloading','cancelling') THEN d.updated END DESC,d.id""",
                (DOWNLOAD_HISTORY_LIMIT, DOWNLOAD_HISTORY_LIMIT)).fetchall()
            total = db.execute('SELECT count(*) FROM downloads').fetchone()[0]
            favourites={r[0] for r in db.execute('SELECT id FROM channel_favourites')}
            finished=[row['id'] for row in rows if row['state'] not in ('queued','downloading','cancelling')]
            sizes=media_library.file_sizes(store,app.config['MEDIA_DIR'],finished,db)
        items=[]
        for row in rows:
            item=dict(row)
            video=json.loads(item.pop('video_data') or '{}')
            channel_data=item.pop('channel_data')
            channel=json.loads(channel_data) if channel_data else {}
            item['favourite']=channel.get('id') in favourites
            item.update(video=video,channel=channel.get('id',''),channel_name=channel.get('name',''),avatar=channel.get('avatar',''),banner=channel.get('banner',''),description=video.get('description',''),published=video.get('date',''))
            # Active rows cannot have a finished file yet; skip their disk checks.
            item['bytes']=sizes.get(row['id'],0)
            video['downloaded']=bool(item['bytes'])
            items.append(item)
        from .download_options import read
        from .storage_locations import host_path
        auto_folder=storage_layout.fixed_folders(store)['auto'] if storage_layout.fixed(store) else store.get('auto_download_folder') or ''
        return jsonify(items=cached_art(items), options=read(store), total=total, shown=len(items), completed_history_limit=DOWNLOAD_HISTORY_LIMIT,
                       quality=store.get('download_quality', '1080'), metadata_type=store.get('metadata_type','emby'),
                       auto_download_folder=host_path(app,auto_folder) if auto_folder else '',
                       worker_online=system_stats.worker_online(store.get('worker_heartbeat','0')))

    @app.get('/api/download-history')
    def download_history():
        denied=guide_access(local_action=True)
        if denied:return denied
        from .download_history import page
        try:return jsonify(page(store,app.config['MEDIA_DIR'],int(request.args.get('limit','15')),int(request.args.get('offset','0')),plain=request.args.get('plain')=='1',include_archived=request.args.get('include_archived')=='1',cached=request.args.get('cached')=='1'))
        except ValueError as exc:return error(str(exc))

    @app.get('/api/download-history/snapshot')
    def download_history_snapshot():
        denied=guide_access(local_action=True)
        if denied:return denied
        from .download_history import snapshot
        try:return jsonify(snapshot(store,app.config['MEDIA_DIR']))
        except ValueError as exc:return error(str(exc))

    @app.get('/api/local-library')
    def local_library_page():
        denied=guide_access(local_action=True)
        if denied:return denied
        from .local_library import page
        try:return jsonify(page(store,app.config['MEDIA_DIR'],request.args.get('cursor','')))
        except ValueError as exc:return error(str(exc))

    @app.get('/api/guide/channel/<channel>')
    @app.post('/api/guide/channel/<channel>')
    def channel_videos(channel):
        denied = guide_access(local_action=request.method=='POST')
        if denied:
            return denied
        from .channel_refresh import request_recent, status as refresh_status
        from .channel_upload_browse import request_page, status as browse_status
        if request.method=='POST':
            if not session.get('csrf') or not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),session['csrf']):
                return error('Reload the page.',403)
            body=request.get_json(silent=True)
            if not isinstance(body,dict) or not (body=={'action':'prioritise_recent'} or set(body)=={'action','cursor'} and body.get('action')=='browse_uploads'):
                return error('Choose the recent channel upload check.')
            try:
                if body['action']=='browse_uploads':
                    browse=request_page(store,channel,body['cursor'])
                    return jsonify(browse=browse),202 if browse['state'] in ('queued','running','retry') else 200
                refresh=request_recent(store,channel)
                from .channel_metadata_refresh import request as request_metadata
                metadata=request_metadata(store,channel)
                return jsonify(refresh=refresh,metadata=metadata),202 if refresh['state'] in ('queued','running','retry') or metadata['state'] in ('queued','running','retry') else 200
            except LookupError as exc:return error(str(exc),404)
            except ValueError as exc:return error(str(exc))
        try:
            offset = max(0, min(int(request.args.get('offset', 0)), 1000000))
        except ValueError:
            return error('Invalid video offset.')
        with store.connect() as db:
            rows = db.execute('SELECT v.data FROM videos v JOIN channels c ON c.id=v.channel WHERE c.active=1 AND v.channel=? ORDER BY v.published DESC,v.id LIMIT 16 OFFSET ?', (channel, offset)).fetchall()
            total = db.execute('SELECT COUNT(*) FROM videos v JOIN channels c ON c.id=v.channel WHERE c.active=1 AND v.channel=?', (channel,)).fetchone()[0]
            channel_row = db.execute('SELECT data FROM channels WHERE id=? AND active=1', (channel,)).fetchone()
        notice = json.loads(channel_row[0]).get('uploads_error', '') if channel_row else ''
        refresh=refresh_status(store,channel)
        try:browse=browse_status(store,channel,request.args.get('browse_cursor','0'))
        except ValueError as exc:return error(str(exc))
        if refresh['notice']:notice=refresh['notice']
        if not rows and refresh['state']=='fresh' and not notice:
            notice = 'No recent public uploads were returned for this channel.'
        if not rows and not notice and store.get('sync_running') == '1':
            notice = 'Importing channel uploads…'
        minimum = channel_downloads.preferences(store, channel)['minimum_video_minutes']
        if type(minimum) is not int or not 0 <= minimum <= 120:
            from .settings.youtube import DOWNLOAD_FIELDS
            minimum = DOWNLOAD_FIELDS['minimum_video_minutes'].read(store)
        return jsonify(items=cached_art([json.loads(r[0]) for r in rows[:15]]), more=len(rows)>15,total=total, notice=notice,refresh=refresh,browse=browse,minimumVideoMinutes=minimum)

    @app.post('/api/sync-all')
    def sync_all():
        denied=guide_access(local_action=True)
        if denied:return denied
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):
            return error('Reload the page before syncing.',403)
        from .sync_all import queue
        return jsonify(queue(store)),202

    @app.route('/api/guide/sync', methods=['GET', 'POST'])
    def guide_sync():
        denied = guide_access()
        if denied:
            return denied
        if request.method == 'POST':
            if not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), csrf()):
                return error('Reload the Guide and try again.', 403)
            if store.get('youtube_sync_paused')=='true':return error('Resume YouTube sync in Settings > Google & YouTube first.',409)
            if time.time() - float(store.get('last_manual_sync', '0')) < 60:
                return error('A refresh was requested recently. Please wait a minute.', 429)
            store.set('last_manual_sync', time.time())
            if store.get('sync_running') != '1':
                store.set('sync_requested', '1')
        heartbeat = float(store.get('worker_heartbeat', '0'))
        worker_version = store.get('worker_version')
        worker_warning = ('Background worker version is unknown. Update both Guide and worker containers.' if not worker_version else
                          'Guide and worker versions differ (' + VERSION + ' / ' + worker_version + '). Update both containers.' if worker_version != VERSION else '')
        from .artwork import queue_counts
        artwork_pending, artwork_deferred = queue_counts(store)
        progress = json.loads(store.get('sync_progress') or '{}')
        connected = bool(store.get('google_tokens'))
        sync_error = store.get('sync_error')
        quota_paused = any(code in sync_error for code in ('quotaExceeded', 'dailyLimitExceeded'))
        from .quota import client_id
        client_hash=hashlib.sha256(client_id(app,store).encode()).hexdigest()
        with store.connect() as db:
            usage=[dict(row) for row in db.execute('SELECT day,resource,requests,units FROM youtube_api_usage WHERE client=? ORDER BY day DESC,resource',(client_hash,))]
        local_allowance_reached = False
        from .youtube_source import enabled,fallback,description
        return jsonify(use_google_api=enabled(store),fallback_active=fallback(store),source_message=description(store),api_usage=usage,youtube_sync_paused=store.get('youtube_sync_paused')=='true',local_allowance_reached=local_allowance_reached, youtube_connected=connected, quota_paused=quota_paused, setup_complete=bool(store.get('setup_complete')), progress=progress, artwork_pending=artwork_pending, artwork_deferred=artwork_deferred, artwork_online=time.time()-float(store.get('artwork_heartbeat','0')) < 120, artwork_error=store.get('artwork_error'), sync_error=store.get('sync_error'), web_version=VERSION, worker_version=worker_version, worker_warning=worker_warning, message=store.get('sync_message') or 'Waiting for the first import.',
                       history_message=store.get('history_message'),
                       running=store.get('sync_running') == '1', queued=store.get('sync_requested') == '1',
                       worker_online=system_stats.worker_online(heartbeat), last_sync=store.get('last_sync'), next_sync=store.get('next_sync'))

    @app.get('/artwork/<key>')
    def artwork(key):
        denied = guide_access()
        if denied:
            return denied
        if not re.fullmatch('[a-f0-9]{64}', key):
            return error('Not found.', 404)
        path = store.directory / 'artwork' / key
        if not path.is_file():
            from .artwork import request_artwork
            request_artwork(store, key)
            return '', 204
        with path.open('rb') as image:
            prefix = image.read(12)
        mime = 'image/png' if prefix.startswith(b'\x89PNG') else 'image/webp' if prefix.startswith(b'RIFF') else 'image/jpeg'
        return send_from_directory(path.parent, key, mimetype=mime, conditional=True, max_age=86400)

    @app.get('/guide/preview/<path:filename>')
    def preview(filename):
        if not logged_in():
            return redirect('/login')
        if not store.get('setup_complete'):
            return redirect('/setup')
        if filename == 'index.html':
            return redirect('/preview/')
        return error('Not found.', 404)

    if not app.testing and app.config.get('PROCESS_ROLE') != 'worker':
        import threading
        from .network_activity import loop as network_loop
        network_stop = threading.Event()
        threading.Thread(
            target=network_loop, args=(store, network_stop, 'web_network'),
            daemon=True, name='network-web',
        ).start()
        app.extensions['network_stop'] = network_stop
    return app
