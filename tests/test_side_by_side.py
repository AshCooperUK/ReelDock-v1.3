"""A second ReelDock on another host port (side-by-side install) keeps every local-setup check."""
import os
import tempfile
import unittest
from unittest.mock import patch
from backend.app import create_app

DEFAULT = 'http://reeldock.test:8790'
SIDE = 'http://reeldock.test:8821'
PASSWORD = 'long-test-password'


class SideBySidePortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def app(self, **config):
        base = {'TESTING': True, 'DATA_DIR': self.tmp.name, 'CANDIDATE_URL': '',
                'GOOGLE_CLIENT_ID': '', 'GOOGLE_CLIENT_SECRET': ''}
        return create_app({**base, **config})

    def admin(self, client, local):
        csrf = client.get('/api/setup/state', base_url=local).json['csrf']
        return client.post('/api/setup/admin', base_url=local, headers={'Origin': local, 'X-CSRF-Token': csrf},
                           json={'username': 'owner', 'password': PASSWORD, 'confirm': PASSWORD})

    def test_default_port_8790_still_works(self):
        app = self.app(LOCAL_SETUP_URL=DEFAULT)
        client = app.test_client()
        self.assertEqual(client.get('/setup', base_url=DEFAULT).status_code, 200)
        self.assertEqual(self.admin(client, DEFAULT).status_code, 200)
        self.assertIsNotNone(client.get_cookie('reeldock_dev_local', domain='reeldock.test'))
        self.assertEqual(client.get('/setup', base_url=SIDE).status_code, 403)  # Another port is another host.

    def test_configured_port_8821_is_accepted_for_local_setup(self):
        app = self.app(APP_PORT='8821', LOCAL_SETUP_URL=SIDE)
        client = app.test_client()
        self.assertEqual(client.get('/setup', base_url=SIDE).status_code, 200)
        response = self.admin(client, SIDE)
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(app.extensions['store'].user())
        # Its own local cookie, so it cannot clash with an install on 8790 of the same NAS name.
        self.assertIsNotNone(client.get_cookie('reeldock_dev_local_8821', domain='reeldock.test'))
        self.assertIsNone(client.get_cookie('reeldock_dev_local', domain='reeldock.test'))
        self.assertIn('Open http://reeldock.test:8821/setup', self.app(APP_PORT='8821', LOCAL_SETUP_URL=SIDE)
                      .test_client().get('/api/dashboard', base_url='http://evil.example:8821').json['error'])

    def test_configured_port_keeps_host_origin_csrf_and_proxy_checks(self):
        app = self.app(APP_PORT='8821', LOCAL_SETUP_URL=SIDE)
        client = app.test_client()
        for base in ('http://evil.example:8821', DEFAULT, 'http://reeldock.test', 'http://127.0.0.1:8821'):
            with self.subTest(host=base):
                self.assertEqual(client.get('/setup', base_url=base).status_code, 403)
                self.assertEqual(client.get('/api/setup/state', base_url=base).status_code, 403)
        csrf = client.get('/api/setup/state', base_url=SIDE).json['csrf']
        body = {'username': 'owner', 'password': PASSWORD, 'confirm': PASSWORD}
        for headers in ({'Origin': DEFAULT, 'X-CSRF-Token': csrf},            # wrong port in Origin
                        {'Origin': 'http://evil.example:8821', 'X-CSRF-Token': csrf},
                        {'Origin': SIDE},                                       # no CSRF token
                        {'Origin': SIDE, 'X-CSRF-Token': 'wrong'},
                        {'Origin': SIDE, 'X-CSRF-Token': csrf, 'X-Forwarded-For': '1.2.3.4'},
                        {'Origin': SIDE, 'X-CSRF-Token': csrf, 'CF-Connecting-IP': '192.168.1.2'}):
            with self.subTest(headers=headers):
                self.assertEqual(client.post('/api/setup/admin', base_url=SIDE, json=body, headers=headers).status_code, 403)
        self.assertEqual(client.get('/setup', base_url=SIDE, environ_overrides={'REMOTE_ADDR': '8.8.8.8'}).status_code, 403)
        self.assertIsNone(app.extensions['store'].user())

    def test_port_settings_must_agree_and_be_valid(self):
        for config in ({'APP_PORT': '8821', 'LOCAL_SETUP_URL': DEFAULT},
                       {'LOCAL_SETUP_URL': SIDE},                               # APP_PORT defaults to 8790
                       {'APP_PORT': '8821', 'LOCAL_SETUP_URL': 'http://reeldock.test'},
                       {'APP_PORT': '8821', 'LOCAL_SETUP_URL': 'https://reeldock.test:8821'},
                       {'APP_PORT': '8821', 'LOCAL_SETUP_URL': SIDE + '/setup'},
                       {'APP_PORT': '8821', 'LOCAL_SETUP_URL': 'http://user:pw@reeldock.test:8821'},
                       {'APP_PORT': '8821', 'LOCAL_SETUP_URL': 'http://reeldock.test:99999'},
                       {'APP_PORT': 'abc', 'LOCAL_SETUP_URL': SIDE},
                       {'APP_PORT': '0', 'LOCAL_SETUP_URL': 'http://reeldock.test:0'},
                       {'APP_PORT': '70000', 'LOCAL_SETUP_URL': 'http://reeldock.test:70000'},
                       {'APP_PORT': '-8821', 'LOCAL_SETUP_URL': SIDE}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.app(**config)

    def test_environment_variables_configure_the_port(self):
        with patch.dict(os.environ, {'APP_PORT': '8821', 'LOCAL_SETUP_URL': SIDE}):
            app = create_app({'TESTING': True, 'DATA_DIR': self.tmp.name})
        self.assertEqual(app.test_client().get('/setup', base_url=SIDE).status_code, 200)


if __name__ == '__main__':
    unittest.main()
