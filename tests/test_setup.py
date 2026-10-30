import gzip
import json
import re
from html import unescape
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from backend.app import create_app, VERSION
from backend.network import public_addresses, public_origin, verify_route

ORIGIN = 'https://dev.example.com'
LOCAL = 'http://reeldock.test:8790'


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.verifications = []
        self.google_calls = []
        self.config = {
            'TESTING': True, 'DATA_DIR': self.tmp.name, 'LOCAL_SETUP_URL': LOCAL,
            'CANDIDATE_URL': ORIGIN, 'GOOGLE_CLIENT_ID': '', 'GOOGLE_CLIENT_SECRET': '',
            'ROUTE_VERIFIER': self.verify, 'GOOGLE_REQUEST': self.google,
        }
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        self.store = self.app.extensions['store']
        self.csrf = self.client.get('/api/setup/state', base_url=LOCAL).json['csrf']

    def tearDown(self):
        self.tmp.cleanup()

    def verify(self, origin, nonce, instance):
        self.verifications.append(origin)
        probe = self.client.get('/setup/probe/' + nonce, base_url=origin)
        self.assertEqual(probe.json, {'nonce': nonce, 'instance': instance})

    def google(self, host, path, **kwargs):
        self.google_calls.append((host, path, kwargs))
        if host == 'oauth2.googleapis.com':
            return {'access_token': 'test-access', 'refresh_token': 'test-refresh', 'expires_in': 3600}
        return {'items': []}

    def post(self, path, data=None, origin=None):
        origin = origin or self.store.get('public_origin') or LOCAL
        return self.client.post('/api/setup/' + path, json=data or {}, base_url=origin,
                                headers={'Origin': origin, 'X-CSRF-Token': self.csrf})

    def local_admin(self):
        response = self.post('admin', {'username': 'owner', 'password': 'long-test-password', 'confirm': 'long-test-password'})
        self.assertEqual(response.status_code, 200)
        self.csrf = response.json['csrf']
        return response

    def test_local_setup_without_public_address_or_google(self):
        self.local_admin()
        self.assertEqual(self.post('skip-route').status_code, 200)
        state = self.client.get('/api/setup/state', base_url=LOCAL).json
        self.assertTrue(state['local_only'])
        self.assertEqual(state['origin'], LOCAL)
        self.assertFalse(state['use_google_api'])
        self.assertEqual(self.store.get('public_origin'), '')
        self.assertEqual(self.post('import-method', {'method': 'google'}).status_code, 409)
        self.assertEqual(self.post('youtube-mode', {'enabled': True}).status_code, 409)
        self.assertEqual(self.post('source-ready').status_code, 200)
        self.assertEqual(self.post('finish').status_code, 200)
        self.assertEqual(self.client.get('/app/', base_url=LOCAL).status_code, 200)
        restarted = create_app(self.config)
        self.assertEqual(restarted.extensions['store'].get('local_only'), '1')
        self.assertEqual(self.google_calls, [])

    def test_skip_route_preserves_auth_csrf_and_local_origin(self):
        self.assertEqual(self.post('skip-route').status_code, 409)
        self.local_admin()
        response = self.client.post('/api/setup/skip-route', json={}, base_url=LOCAL,
                                    headers={'Origin': LOCAL})
        self.assertEqual(response.status_code, 403)
        response = self.client.post('/api/setup/skip-route', json={}, base_url=LOCAL,
                                    headers={'Origin': 'http://evil.example', 'X-CSRF-Token': self.csrf})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.get('local_only'), '')
        self.assertEqual(self.post('skip-route').status_code, 200)
        self.assertEqual(self.client.get('/setup', base_url=ORIGIN).status_code, 403)
        response = self.client.get('/setup', base_url=LOCAL, headers={'X-Forwarded-For': '127.0.0.1'})
        self.assertEqual(response.status_code, 403)

    def create_admin(self):
        self.local_admin()
        self.assertEqual(self.post('verify-route', {'url': ORIGIN}).status_code, 200)
        self.client.get('/login', base_url=ORIGIN)
        with self.client.session_transaction(base_url=ORIGIN) as cookie:
            token = cookie['csrf']
        response = self.client.post('/login', data={'csrf': token, 'username': 'owner', 'password': 'long-test-password'},
                                    base_url=ORIGIN, headers={'Origin': ORIGIN})
        self.assertEqual(response.status_code, 302)
        self.csrf = self.client.get('/api/setup/state', base_url=ORIGIN).json['csrf']
        return response

    def connect(self):
        self.create_admin()
        self.assertEqual(self.post('google', {'client_id': 'test.apps.googleusercontent.com', 'client_secret': 'test-client-secret'}).status_code, 200)
        response = self.post('connect-google')
        self.oauth_url = response.json['url']
        self.oauth_state = parse_qs(urlsplit(self.oauth_url).query)['state'][0]
        return self.client.get('/oauth/google/callback', query_string={'state': self.oauth_state, 'code': 'test-code'}, base_url=ORIGIN)

    def test_account_switch_after_setup_uses_chooser_and_keeps_old_connection_on_cancel(self):
        self.connect(); self.store.set('setup_complete','1')
        previous=self.store.get('google_tokens')
        headers={'Origin':ORIGIN,'X-CSRF-Token':self.csrf}
        denied=self.client.post('/api/youtube/connect',json={},base_url=ORIGIN,headers={'Origin':ORIGIN})
        self.assertEqual(denied.status_code,403)
        result=self.client.post('/api/youtube/connect',json={},base_url=ORIGIN,headers=headers)
        self.assertEqual(result.status_code,200)
        query=parse_qs(urlsplit(result.json['url']).query)
        self.assertEqual(set(query['prompt'][0].split()),{'select_account','consent'})
        self.assertEqual(query['scope'],['https://www.googleapis.com/auth/youtube.readonly'])
        cancelled=self.client.get('/oauth/google/callback',query_string={'state':query['state'][0],'error':'access_denied'},base_url=ORIGIN)
        self.assertEqual(cancelled.status_code,400)
        self.assertEqual(self.store.get('google_tokens'),previous)
        result=self.client.post('/api/youtube/connect',json={},base_url=ORIGIN,headers=headers)
        query=parse_qs(urlsplit(result.json['url']).query)
        done=self.client.get('/oauth/google/callback',query_string={'state':query['state'][0],'code':'new-code'},base_url=ORIGIN)
        self.assertEqual(done.location,'/app/?view=YouTube')
        self.assertEqual(self.store.get('setup_complete'),'1')

    def test_subscription_management_permission_returns_to_youtube_page(self):
        self.connect(); self.store.set('setup_complete','1')
        result=self.client.post('/api/youtube/authorise',json={},base_url=ORIGIN,headers={'Origin':ORIGIN,'X-CSRF-Token':self.csrf})
        query=parse_qs(urlsplit(result.json['url']).query)
        self.assertEqual(query['scope'],['https://www.googleapis.com/auth/youtube'])
        granted=lambda host,path,**kwargs:{'access_token':'manage-access','refresh_token':'manage-refresh','expires_in':3600,'scope':'https://www.googleapis.com/auth/youtube'}
        self.app.config['GOOGLE_REQUEST']=granted
        done=self.client.get('/oauth/google/callback',query_string={'state':query['state'][0],'code':'manage-code'},base_url=ORIGIN)
        # Previously '/guide/?settings=subscriptions', which redirected to the
        # TV Guide and dropped the parameter.
        self.assertEqual(done.location,'/app/?view=YouTube')
        self.assertIn('manage-refresh',self.store.get('google_tokens'))

    def test_google_credentials_reconnect_opens_connection_step(self):
        self.connect(); self.store.set('setup_complete','1')
        response=self.client.post('/google/reconnect',data={'csrf':self.csrf},base_url=ORIGIN,headers={'Origin':ORIGIN})
        self.assertEqual(response.location,'/setup')
        state=self.client.get('/api/setup/state',base_url=ORIGIN).json
        self.assertTrue(state['reconnecting'])
        self.assertTrue(state['youtube_connected'])

    def test_candidate_not_adopted_on_start(self):
        self.assertEqual(self.store.get('public_origin'), '')
        self.assertIn(ORIGIN.encode(), self.client.get('/setup', base_url=LOCAL).data)

    def test_local_setup_rejects_tunnel_and_wrong_host(self):
        for headers in ({'CF-Ray': 'test'}, {'CF-Connecting-IP': '192.168.1.2'},
                        {'Forwarded': 'for=192.168.1.2'}, {'X-Forwarded-For': ''}):
            with self.subTest(headers=headers):
                self.assertEqual(self.client.get('/setup', base_url=LOCAL, headers=headers).status_code, 403)
                self.assertEqual(self.client.post('/api/setup/admin', base_url=LOCAL, json={}, headers=headers).status_code, 403)
        for origin in (ORIGIN, 'http://evil.example:8790'):
            self.assertEqual(self.client.get('/setup', base_url=origin).status_code, 403)
        self.assertEqual(self.client.get('/setup', base_url=LOCAL, environ_overrides={'REMOTE_ADDR': '8.8.8.8'}).status_code, 403)
        self.assertIsNone(self.store.user())
        self.assertFalse((Path(self.tmp.name) / 'setup-key.txt').exists())

    def test_route_requires_administrator(self):
        self.assertEqual(self.post('verify-route', {'url': ORIGIN}).status_code, 409)
        self.assertEqual(self.verifications, [])

    def test_local_csrf_and_origin_required(self):
        for headers in ({'Origin': LOCAL}, {'Origin': 'http://evil.example', 'X-CSRF-Token': self.csrf}):
            self.assertEqual(self.client.post('/api/setup/admin', base_url=LOCAL, json={}, headers=headers).status_code, 403)
        self.assertIsNone(self.store.user())

    def test_local_session_is_separate_and_can_resume(self):
        response = self.local_admin()
        self.assertNotIn('Secure;', response.headers['Set-Cookie'])
        cookie = self.client.get_cookie('reeldock_dev_local', domain='reeldock.test')
        self.assertIsNotNone(cookie)
        self.assertEqual(self.post('connect-google').status_code, 409)
        self.assertEqual(self.post('verify-route', {'url': ORIGIN}).status_code, 200)
        stranger = self.app.test_client()
        stranger.set_cookie('reeldock_dev_session', cookie.value, domain='dev.example.com')
        self.assertEqual(stranger.get('/api/setup/state', base_url=ORIGIN).status_code, 401)

    def test_old_unclaimed_install_resumes_locally(self):
        self.store.set('public_origin', ORIGIN)
        restarted = create_app(self.config)
        self.assertEqual(restarted.extensions['store'].get('public_origin'), '')
        self.assertEqual(restarted.test_client().get('/setup', base_url=LOCAL).status_code, 200)
        self.assertEqual(restarted.test_client().get('/setup', base_url=ORIGIN).status_code, 403)

    def test_failed_route_allows_local_resume_after_restart(self):
        self.local_admin()
        self.app.config['ROUTE_VERIFIER'] = lambda *args: (_ for _ in ()).throw(ValueError('Offline'))
        self.assertEqual(self.post('verify-route', {'url': ORIGIN}).status_code, 400)
        restarted = create_app(self.config).test_client()
        self.assertEqual(restarted.get('/setup', base_url=LOCAL).location, '/login')
        restarted.get('/login', base_url=LOCAL)
        with restarted.session_transaction(base_url=LOCAL) as cookie:
            token = cookie['csrf']
        result = restarted.post('/login', base_url=LOCAL, headers={'Origin': LOCAL},
                                data={'csrf': token, 'username': 'owner', 'password': 'long-test-password'})
        self.assertEqual(result.status_code, 302)
        self.assertTrue(restarted.get('/api/setup/state', base_url=LOCAL).json['admin'])

    def test_public_hostname_must_differ_from_local(self):
        self.local_admin()
        self.assertEqual(self.post('verify-route', {'url': 'https://reeldock.test'}).status_code, 400)
        self.assertEqual(self.verifications, [])

    def test_route_probe_and_origin_persist(self):
        self.local_admin()
        self.assertEqual(self.post('verify-route', {'url': ORIGIN}).status_code, 200)
        self.assertEqual(self.verifications, [ORIGIN])
        self.assertEqual(self.store.get('public_origin'), ORIGIN)
        self.assertEqual(self.client.get('/setup/probe/invalid').status_code, 404)
        self.assertEqual(self.store.get('route_nonce'), '')
        reopened = create_app(self.config)
        self.assertEqual(reopened.extensions['store'].get('public_origin'), ORIGIN)

    def test_failed_route_does_not_adopt_origin(self):
        self.local_admin()
        self.app.config['ROUTE_VERIFIER'] = lambda *args: (_ for _ in ()).throw(ValueError('Wrong installation'))
        self.assertEqual(self.post('verify-route', {'url': ORIGIN}).status_code, 400)
        self.assertEqual(self.store.get('public_origin'), '')
        self.assertEqual(self.store.get('route_nonce'), '')

    def test_local_entry_redirects_only_after_verification(self):
        self.local_admin()
        self.assertEqual(self.client.get('/setup', base_url=LOCAL).status_code, 200)
        self.post('verify-route', {'url': ORIGIN})
        response = self.client.get('/setup', base_url=LOCAL)
        self.assertEqual(response.location, ORIGIN + '/setup')
        self.assertEqual(self.post('admin', origin=LOCAL).status_code, 403)

    def test_password_hash_secure_cookie_and_single_admin(self):
        response = self.create_admin()
        self.assertNotEqual(self.store.user()['password_hash'], 'long-test-password')
        self.assertIn('Secure;', response.headers['Set-Cookie'])
        self.assertIn('HttpOnly;', response.headers['Set-Cookie'])
        self.assertIn('SameSite=Lax', response.headers['Set-Cookie'])
        self.assertEqual(self.post('admin', {'username': 'other', 'password': 'long-test-password', 'confirm': 'long-test-password'}).status_code, 409)

    def test_bad_password_and_confirmation(self):
        self.assertEqual(self.post('admin', {'username': 'owner', 'password': 'short', 'confirm': 'short'}).status_code, 400)
        self.assertEqual(self.post('admin', {'username': 'owner', 'password': 'long-test-password', 'confirm': 'different-password'}).status_code, 400)
        self.assertIsNone(self.store.user())

    def test_bad_username_message_is_readable(self):
        response = self.post('admin', {'username': 'x', 'password': 'long-test-password', 'confirm': 'long-test-password'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('Use 3\u201364 letters', response.get_json()['error'])

    def test_source_has_no_double_encoded_text(self):
        # UTF-8 read as Windows-1252 shows up as these sequences in user-facing text.
        root = Path(__file__).resolve().parent.parent
        files = [*root.glob('backend/*.py'), *root.glob('frontend/src/*.ts'), *root.glob('frontend/src/*.tsx'), *root.glob('frontend/src/*.css')]
        for path in files:
            text = path.read_text(encoding='utf-8')
            for marker in ('\u00c3\u0192', '\u00e2\u20ac', '\u00c3\u00a2'):
                self.assertNotIn(marker, text, path.name)

    def test_anonymous_cannot_modify_after_admin(self):
        self.create_admin()
        stranger = self.app.test_client()
        response = stranger.get('/api/setup/state', base_url=ORIGIN, headers={})
        self.assertEqual(response.status_code, 401)

    def test_csrf_and_origin_enforced(self):
        self.create_admin()
        self.csrf = 'incorrect'
        self.assertEqual(self.post('google').status_code, 403)
        response = self.client.post('/api/setup/google', base_url=ORIGIN, json={}, headers={'Origin': 'https://evil.example'})
        self.assertEqual(response.status_code, 403)

    def test_env_credentials_are_detected_without_exposing_secret(self):
        self.app.config.update(GOOGLE_CLIENT_ID='env.apps.googleusercontent.com', GOOGLE_CLIENT_SECRET='private-test-secret')
        response = self.client.get('/api/setup/state', base_url=LOCAL)
        self.assertTrue(response.json['id_from_yaml'])
        self.assertTrue(response.json['secret_from_yaml'])
        self.assertNotIn(b'private-test-secret', response.data)
        self.assertEqual(self.store.get('google_client_secret'), '')

    def test_partial_env_credentials(self):
        self.create_admin()
        self.app.config['GOOGLE_CLIENT_ID'] = 'env.apps.googleusercontent.com'
        self.assertEqual(self.post('google', {'client_secret': 'entered-test-secret'}).status_code, 200)
        self.assertEqual(self.store.get('google_client_id'), '')
        self.assertEqual(self.store.get('google_client_secret'), 'entered-test-secret')

    def test_google_oauth_readonly_state_pkce_and_api_verification(self):
        response = self.connect()
        self.assertEqual(response.status_code, 302)
        args = parse_qs(urlsplit(self.oauth_url).query)
        self.assertEqual(args['scope'], ['https://www.googleapis.com/auth/youtube.readonly'])
        self.assertEqual(args['redirect_uri'], [ORIGIN + '/oauth/google/callback'])
        self.assertEqual(args['code_challenge_method'], ['S256'])
        self.assertEqual(len(self.google_calls), 2)
        self.assertEqual(json.loads(self.store.get('google_tokens'))['refresh_token'], 'test-refresh')
        replay = self.client.get('/oauth/google/callback', query_string={'state': self.oauth_state, 'code': 'test'}, base_url=ORIGIN)
        self.assertEqual(replay.status_code, 400)

    def test_oauth_wrong_state_makes_no_network_calls(self):
        self.create_admin()
        self.assertEqual(self.client.get('/oauth/google/callback?state=wrong&code=test', base_url=ORIGIN).status_code, 400)
        self.assertEqual(self.google_calls, [])

    def test_google_api_failure_does_not_complete_connection(self):
        self.create_admin()
        self.post('google', {'client_id': 'test.apps.googleusercontent.com', 'client_secret': 'test-client-secret'})
        response = self.post('connect-google')
        state = parse_qs(urlsplit(response.json['url']).query)['state'][0]
        def fail(host, path, **kwargs):
            if host == 'oauth2.googleapis.com':
                return {'access_token': 'test-access', 'refresh_token': 'test-refresh'}
            raise ValueError('API not enabled')
        self.app.config['GOOGLE_REQUEST'] = fail
        response = self.client.get('/oauth/google/callback', query_string={'state': state, 'code': 'test'}, base_url=ORIGIN)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.get('google_tokens'), '')

    def test_finish_requires_connection_then_locks_setup(self):
        self.create_admin()
        self.assertEqual(self.post('finish').status_code, 409)
        self.store.set('google_tokens', '{"refresh_token":"fixture"}')
        self.assertEqual(self.post('finish').status_code, 200)
        self.assertEqual(self.store.get('refresh_minutes'), '360')
        self.assertEqual(self.post('google').status_code, 409)

    def test_restart_keeps_users_and_login_works(self):
        self.connect()
        self.post('finish')
        restarted = create_app(self.config).test_client()
        response = restarted.get('/login', base_url=ORIGIN)
        self.assertEqual(response.status_code, 200)
        with restarted.session_transaction(base_url=ORIGIN) as cookie:
            token = cookie['csrf']
        response = restarted.post('/login', data={'csrf': token, 'username': 'owner', 'password': 'long-test-password'},
                                  base_url=ORIGIN, headers={'Origin': ORIGIN})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(restarted.get('/guide/', base_url=ORIGIN).location, '/app/?view=TV+Guide')

    def test_guide_and_preview_require_login(self):
        self.connect()
        self.post('finish')
        response = self.client.get('/guide/preview/index.html', base_url=ORIGIN)
        self.assertEqual(response.location, '/preview/')
        response.close()
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get('/guide/preview/index.html', base_url=ORIGIN).location, '/login')
        response = self.client.get('/guide/preview/../../backend/app.py', base_url=ORIGIN)
        self.assertEqual(response.status_code, 404)

    def test_readiness_and_health_do_not_require_external_services(self):
        response = self.client.get('/api/setup/state', base_url=LOCAL)
        self.assertTrue(all(item['ok'] for item in response.json['checks']))
        self.assertTrue((Path(self.tmp.name) / 'artwork').is_dir())
        self.assertEqual(self.google_calls, [])
        self.assertEqual(self.client.get('/health').json['status'], 'ok')

    def test_malformed_setup_request_and_large_body(self):
        self.assertEqual(self.client.post('/api/setup/admin', base_url=LOCAL, json=[], headers={}).status_code, 400)
        self.assertEqual(self.post('admin', {'password': 'x' * 20000}).status_code, 413)

    def test_cancelled_google_authorisation_can_restart(self):
        self.create_admin()
        self.post('google', {'client_id': 'test.apps.googleusercontent.com', 'client_secret': 'test-client-secret'})
        url = self.post('connect-google').json['url']
        state = parse_qs(urlsplit(url).query)['state'][0]
        response = self.client.get('/oauth/google/callback', query_string={'state': state, 'error': 'access_denied'}, base_url=ORIGIN)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.google_calls, [])
        self.assertEqual(self.post('connect-google').status_code, 200)

    def test_react_setup_resumes_saved_state_without_exposing_secrets(self):
        def initial(client, origin):
            page = client.get('/setup', base_url=origin)
            self.assertEqual(page.status_code, 200)
            return json.loads(re.search(r'<script id="reeldock-bootstrap" type="application/json">(.*?)</script>', page.text)[1])['initial']
        self.assertFalse(initial(self.client, LOCAL)['admin'])
        self.local_admin()
        self.assertTrue(initial(self.client, LOCAL)['admin'])
        self.post('verify-route', {'url': ORIGIN})
        browser = create_app(self.config).test_client()
        browser.get('/login', base_url=ORIGIN)
        with browser.session_transaction(base_url=ORIGIN) as cookie:
            token = cookie['csrf']
        browser.post('/login', base_url=ORIGIN, headers={'Origin': ORIGIN},
                     data={'csrf': token, 'username': 'owner', 'password': 'long-test-password'})
        state = initial(browser, ORIGIN)
        self.assertTrue(state['admin'])
        self.assertEqual(state['origin'], ORIGIN)
        self.assertFalse(state['youtube_connected'])
        self.store.set('google_tokens', '{"refresh_token":"private-test-token"}')
        self.store.set('google_client_secret', 'private-client-secret')
        state = initial(browser, ORIGIN)
        self.assertTrue(state['youtube_connected'])
        self.assertNotIn('private-test-token', json.dumps(state))
        self.assertNotIn('private-client-secret', json.dumps(state))

    def test_install_manifest_and_early_theme_script_are_public_and_revalidated(self):
        page = self.client.get('/setup', base_url=LOCAL).text
        head = page.split('</head>')[0]
        self.assertIn('<link rel="manifest" href="/preview/assets/manifest.webmanifest" />', head)
        boot = head.index('<script src="/preview/assets/theme-boot.js"></script>')
        self.assertLess(boot, head.index('type="module"'))  # Runs before the app and its stylesheet.
        anonymous = self.app.test_client()
        with anonymous.get('/preview/assets/manifest.webmanifest', base_url=LOCAL) as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, 'application/manifest+json')
            self.assertEqual(response.headers['Cache-Control'], 'no-cache')
            manifest = response.get_json(force=True)
        self.assertEqual((manifest['start_url'], manifest['display']), ('/app/', 'standalone'))
        for icon in manifest['icons']:
            with anonymous.get(icon['src'], base_url=LOCAL) as response:
                self.assertEqual(response.status_code, 200, icon['src'])
        with anonymous.get('/preview/assets/theme-boot.js', base_url=LOCAL) as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn('javascript', response.mimetype)
            self.assertEqual(response.headers['Cache-Control'], 'no-cache')
        policy = self.client.get('/setup', base_url=LOCAL).headers['Content-Security-Policy']
        self.assertIn("script-src 'self'", policy)  # External same-origin script: no inline script or CSP change needed.
        self.assertNotIn('unsafe-inline', policy.split('script-src')[1].split(';')[0])

    def test_react_setup_assets_hashed_cached_and_compressed_but_page_is_not(self):
        response = self.client.get('/setup', base_url=LOCAL)
        page = response.text
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        urls = re.findall(r'(?:src|href)="(/preview/assets/[^" ]+-[A-Za-z0-9_-]{8}\.(?:js|css))"', page)
        self.assertEqual(len(urls), 2)
        for url in urls:
            with self.client.get(url, base_url=LOCAL) as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['Cache-Control'], 'public, max-age=31536000, immutable')
                plain = response.get_data()
            with self.client.get(url, base_url=LOCAL, headers={'Accept-Encoding': 'gzip, br'}) as response:
                self.assertEqual(response.headers['Content-Encoding'], 'gzip')
                self.assertIn('Accept-Encoding', response.headers['Vary'])
                self.assertEqual(gzip.decompress(response.get_data()), plain)
                self.assertLess(len(response.get_data()), len(plain))
                etag = response.headers['ETag']
            self.assertTrue(etag.startswith('W/'))
            with self.client.get(url, base_url=LOCAL, headers={'Accept-Encoding': 'gzip', 'If-None-Match': etag}) as response:
                self.assertEqual(response.status_code, 304)
            # Byte ranges are served from the identity file, never from gzip.
            with self.client.get(url, base_url=LOCAL, headers={'Accept-Encoding': 'gzip', 'Range': 'bytes=0-9'}) as response:
                self.assertNotIn('Content-Encoding', response.headers)
        self.assertIn(VERSION, page)
        self.assertNotIn('/static/setup.js', page)
        with self.client.get('/static/tmdb-logo.svg', base_url=LOCAL, headers={'Accept-Encoding': 'gzip'}) as response:
            self.assertEqual(response.headers['Cache-Control'], 'no-cache')
            self.assertEqual(response.headers['Content-Encoding'], 'gzip')
        self.assertEqual(self.client.get('/static/vendor/hls.min.js', base_url=LOCAL).status_code, 404)
        self.assertEqual(self.client.get('/api/setup/state', base_url=LOCAL).headers['Cache-Control'], 'no-store')
        self.assertEqual(self.client.get('/preview/assets/missing-AAAAAAAA.js', base_url=LOCAL).headers['Cache-Control'], 'no-store')

    def test_bootstrap_escapes_html_and_public_error_assets_do_not_expose_state(self):
        self.app.config['CANDIDATE_URL'] = '</script><script>alert(1)</script>'
        page = self.client.get('/setup', base_url=LOCAL).text
        self.assertNotIn('</script><script>alert', page)
        value = json.loads(re.search(r'<script id="reeldock-bootstrap" type="application/json">(.*?)</script>', page)[1])
        self.assertEqual(value['candidate'], self.app.config['CANDIDATE_URL'])
        asset = re.search(r'src="(/preview/assets/[^" ]+\.js)"', page)[1]
        with self.client.get(asset, base_url='http://foreign.example:8790') as response:
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/api/setup/state', base_url='http://foreign.example:8790').status_code, 403)

    def test_native_form_policy_and_public_login_behind_tunnel(self):
        self.local_admin()
        self.post('verify-route', {'url': ORIGIN})
        browser = self.app.test_client()
        tunnel = {'CF-Ray': 'fixture', 'X-Forwarded-Proto': 'https'}
        # Match the HTTP origin connection behind the public HTTPS tunnel.
        internal = 'http://dev.example.com'
        page = browser.get('/login', base_url=internal, headers=tunnel)
        self.assertEqual(page.headers['Referrer-Policy'], 'same-origin')
        self.assertIn(b'"page": "login"', page.data)
        self.assertIn(b'"csrf_token":', page.data)
        with browser.session_transaction(base_url=internal) as cookie:
            token = cookie['csrf']
        result = browser.post('/login', base_url=internal,
                              headers={**tunnel, 'Origin': ORIGIN},
                              data={'csrf': token, 'username': 'owner', 'password': 'long-test-password'})
        self.assertEqual(result.status_code, 302)
        self.assertIn('Secure;', result.headers['Set-Cookie'])
        self.assertEqual(browser.get('/setup', base_url=internal, headers=tunnel).status_code, 200)

    def test_null_missing_and_foreign_login_origins_stay_rejected(self):
        self.create_admin()
        browser = self.app.test_client()
        browser.get('/login', base_url=ORIGIN)
        with browser.session_transaction(base_url=ORIGIN) as cookie:
            token = cookie['csrf']
        for origin in (None, 'null', 'https://other.example'):
            with self.subTest(origin=origin):
                result = browser.post('/login', base_url=ORIGIN,
                                      headers={} if origin is None else {'Origin': origin},
                                      data={'csrf': token, 'username': 'owner', 'password': 'long-test-password'})
                self.assertEqual(result.status_code, 403)
                self.assertEqual(result.mimetype, 'text/html')
                self.assertIn(b'Reopen sign-in', result.data)
                self.assertIn(b'"continue_url": "/login"', result.data)
                with browser.session_transaction(base_url=ORIGIN) as cookie:
                    self.assertNotIn('user_id', cookie)

    def test_logout_and_login_failure_limit(self):
        self.create_admin()
        response = self.client.post('/logout', data={'csrf': self.csrf}, base_url=ORIGIN, headers={'Origin': ORIGIN})
        self.assertEqual(response.status_code, 302)
        self.client.get('/login', base_url=ORIGIN)
        with self.client.session_transaction(base_url=ORIGIN) as cookie:
            token = cookie['csrf']
        for _ in range(5):
            self.client.post('/login', data={'csrf': token, 'username': 'owner', 'password': 'wrong'}, base_url=ORIGIN, headers={'Origin': ORIGIN})
        response = self.client.post('/login', data={'csrf': token, 'username': 'owner', 'password': 'long-test-password'}, base_url=ORIGIN, headers={'Origin': ORIGIN})
        self.assertIn(b'Too many failed attempts', response.data)


    def login_attempt(self, client_ip, password='wrong', peer='127.0.0.1'):
        browser = self.app.test_client()
        browser.get('/login', base_url=ORIGIN)
        with browser.session_transaction(base_url=ORIGIN) as cookie:
            token = cookie['csrf']
        headers = {'Origin': ORIGIN}
        if client_ip: headers['CF-Connecting-IP'] = client_ip
        return browser.post('/login', data={'csrf': token, 'username': 'owner', 'password': password},
                            base_url=ORIGIN, headers=headers, environ_base={'REMOTE_ADDR': peer})

    def test_failed_sign_ins_from_one_client_do_not_lock_out_another(self):
        self.create_admin()
        for _ in range(5):
            self.login_attempt('203.0.113.10')
        self.assertIn(b'Too many failed attempts', self.login_attempt('203.0.113.10', 'long-test-password').data)
        owner = self.login_attempt('198.51.100.20', 'long-test-password')
        self.assertEqual(owner.status_code, 302)

    def test_failures_across_many_clients_still_hit_a_shared_ceiling(self):
        from backend.app import LOGIN_GLOBAL_LIMIT
        self.create_admin()
        for n in range(LOGIN_GLOBAL_LIMIT):
            self.login_attempt(f'203.0.113.{n + 1}')
        self.assertIn(b'Too many failed attempts', self.login_attempt('198.51.100.20', 'long-test-password').data)

    def test_forwarded_address_from_a_public_peer_is_not_trusted(self):
        self.create_admin()
        # A directly exposed client cannot dodge its lock by changing the header.
        for n in range(5):
            self.login_attempt(f'203.0.113.{n + 1}', peer='192.0.2.50')
        self.assertIn(b'Too many failed attempts', self.login_attempt('203.0.113.99', 'long-test-password', peer='192.0.2.50').data)
        with self.store.connect() as db:
            buckets = [r[0] for r in db.execute("SELECT bucket FROM attempts WHERE bucket LIKE 'login:%'")]
        self.assertTrue(buckets)
        self.assertFalse(any('192.0.2.50' in bucket for bucket in buckets))  # addresses are stored hashed

    def test_per_client_sign_in_rows_stay_bounded(self):
        self.create_admin()
        with patch('backend.app.LOGIN_CLIENT_ROWS', 3):
            for n in range(6):
                self.login_attempt(f'203.0.113.{n + 1}')
        with self.store.connect() as db:
            self.assertLessEqual(db.execute("SELECT count(*) FROM attempts WHERE bucket LIKE 'login:%'").fetchone()[0], 3)

class RouteValidationTests(unittest.TestCase):
    def test_normalisation(self):
        self.assertEqual(public_origin('DEV.example.com/'), ORIGIN)

    def test_reject_invalid_origins(self):
        for value in ('http://dev.example.com', 'https://dev.example.com/path', 'https://user:pass@dev.example.com',
                      'https://dev.example.com:9999', 'https://127.0.0.1', 'https://localhost', 'https://dev.example.com?x=1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                public_origin(value)

    def test_dns_failure_is_a_retryable_setup_error(self):
        with patch('backend.network.socket.getaddrinfo', side_effect=OSError('DNS unavailable')):
            with self.assertRaisesRegex(ValueError, 'hostname could not be found'):
                public_addresses('dev.example.com')

    def test_dns_rejects_private_and_mixed_addresses(self):
        for addresses in (['127.0.0.1'], ['192.168.1.2'], ['::1'], ['1.1.1.1', '10.0.0.1']):
            with patch('backend.network.socket.getaddrinfo', return_value=[(None,None,None,None,(address,443)) for address in addresses]):
                with self.assertRaises(ValueError):
                    public_addresses('dev.example.com')

    def test_probe_pins_ip_checks_tls_and_matches_instance(self):
        with patch('backend.network.public_addresses', return_value=['1.1.1.1']), \
             patch('backend.network.socket.create_connection') as dial, \
             patch('backend.network.ssl.create_default_context') as tls, \
             patch('backend.network.http.client.HTTPSConnection') as https:
            response = https.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"nonce":"nonce","instance":"instance"}'
            verify_route(ORIGIN, 'nonce', 'instance')
            dial.assert_called_once_with(('1.1.1.1', 443), timeout=8)
            tls.return_value.wrap_socket.assert_called_once_with(dial.return_value, server_hostname='dev.example.com')
            response.read.return_value = b'{"nonce":"nonce","instance":"wrong"}'
            with self.assertRaises(ValueError):
                verify_route(ORIGIN, 'nonce', 'instance')
            response.status = 302
            with self.assertRaises(ValueError):
                verify_route(ORIGIN, 'nonce', 'instance')


if __name__ == '__main__':
    unittest.main()
