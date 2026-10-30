import tempfile
import unittest
from pathlib import Path
from backend.app import create_app, VERSION
from backend.storage import Store

class ReelDockIsolationTests(unittest.TestCase):
    def test_rejects_ytsd_data_before_writing_anything(self):
        with tempfile.TemporaryDirectory() as folder:
            old = Path(folder) / 'ytsd-dev.sqlite3'
            old.write_bytes(b'original YTSD data')
            with self.assertRaisesRegex(RuntimeError, 'own empty data directory'):
                Store(folder)
            self.assertEqual(old.read_bytes(), b'original YTSD data')
            self.assertEqual([p.name for p in Path(folder).iterdir()], ['ytsd-dev.sqlite3'])

    def test_fresh_app_has_own_database_cookie_and_no_accounts(self):
        with tempfile.TemporaryDirectory() as folder:
            app = create_app({'TESTING': True, 'DATA_DIR': folder,
                'MEDIA_DIR': str(Path(folder) / 'media'), 'M3U_DIR': str(Path(folder) / 'm3u'),
                'LOCAL_SETUP_URL': 'http://127.0.0.1:8790',
                'CANDIDATE_URL': '', 'GOOGLE_CLIENT_ID': '', 'GOOGLE_CLIENT_SECRET': ''})
            store = app.extensions['store']
            self.assertEqual(store.path.name, 'reeldock.sqlite3')
            self.assertEqual(app.config['SESSION_COOKIE_NAME'], 'reeldock_dev_session')
            self.assertIsNone(store.user())
            for key in ('google_client_secret', 'google_token', 'public_origin', 'emby_api_key', 'setup_complete'):
                self.assertFalse(store.get(key))
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM channels').fetchone()[0], 0)
            client = app.test_client()
            self.assertEqual(client.get('/health').json['version'], VERSION)
            response = client.get('/setup', base_url='http://127.0.0.1:8790')
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'ReelDock', response.data)
            self.assertNotEqual(client.get('/api/guide/sync', base_url='http://127.0.0.1:8790').status_code, 200)
            self.assertEqual(client.get('/preview/', base_url='http://evil.example:8790').status_code, 403)
            self.assertEqual(client.get('/preview/', base_url='http://127.0.0.1:8790').location, '/app/')

if __name__ == '__main__': unittest.main()
