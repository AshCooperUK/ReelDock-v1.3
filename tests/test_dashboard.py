import tempfile
import json
import unittest
from pathlib import Path
from backend.app import create_app

LOCAL = 'http://127.0.0.1:8790'

class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({'TESTING': True, 'DATA_DIR': self.tmp.name,
            'MEDIA_DIR': str(Path(self.tmp.name) / 'media'), 'M3U_DIR': str(Path(self.tmp.name) / 'm3u'),
            'LOCAL_SETUP_URL': LOCAL, 'CANDIDATE_URL': '', 'GOOGLE_CLIENT_ID': '', 'GOOGLE_CLIENT_SECRET': ''})
        self.client = self.app.test_client()
        self.store = self.app.extensions['store']
    def tearDown(self):
        self.tmp.cleanup()
    def get(self):
        return self.client.get('/api/dashboard', base_url=LOCAL)
    def admin(self):
        token = self.client.get('/api/setup/state', base_url=LOCAL).json['csrf']
        response = self.client.post('/api/setup/admin', base_url=LOCAL,
            headers={'Origin': LOCAL, 'X-CSRF-Token': token},
            json={'username': 'owner', 'password': 'long-test-password', 'confirm': 'long-test-password'})
        self.assertEqual(response.status_code, 200)
    def test_fresh_dashboard_only_returns_setup_action(self):
        self.assertEqual(self.get().json, {'state': 'setup', 'continue_url': '/setup'})
        self.assertEqual(self.client.get('/api/dashboard', base_url='http://evil.example:8790').status_code, 403)
    def test_worker_online_uses_one_threshold_everywhere(self):
        import time
        from backend.system_stats import WORKER_OFFLINE_SECONDS
        from backend.activity import snapshot
        self.admin();self.store.set('setup_complete','true')
        # Between the old 120 s and 600 s thresholds the chip said "online"
        # while the alert said "offline".
        self.store.set('worker_heartbeat',time.time()-WORKER_OFFLINE_SECONDS-60)
        self.assertFalse(self.get().json['overview']['worker_online'])
        self.assertTrue(any(a['id'].startswith('worker-offline') for a in snapshot(self.store)['alerts']))
        self.store.set('worker_heartbeat',time.time())
        self.assertTrue(self.get().json['overview']['worker_online'])
        self.assertFalse(any(a['id'].startswith('worker-offline') for a in snapshot(self.store)['alerts']))

    def test_setup_required_then_real_counts_and_anonymous_isolation(self):
        self.admin()
        self.assertEqual(self.get().json, {'state': 'setup', 'continue_url': '/setup'})
        self.store.set('setup_complete', 'true')
        result = self.get().json
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['overview']['channels'], 0)
        self.assertEqual(result['overview']['downloads'], {})
        self.assertEqual(result['m3u_sources'], 0)
        self.assertEqual(result['overview']['m3u'],dict(playlists=0,movies=0,shows=0,episodes=0,dated_tv=0,live_playlists=0,unsorted=0))
        self.assertFalse(result['overview']['worker_online'])
        anonymous = self.app.test_client().get('/api/dashboard', base_url=LOCAL)
        self.assertEqual(anonymous.json, {'state': 'sign_in', 'continue_url': '/login'})
        self.store.set('auth_revision', 'changed')
        self.assertEqual(self.get().json, {'state': 'sign_in', 'continue_url': '/login'})

    def test_m3u_counts_use_owned_entries_and_distinguish_shows_from_episodes(self):
        from backend import m3u
        self.admin();self.store.set('setup_complete','true')
        source=m3u.save(self.store,dict(name='Count test',url='https://example.com/playlist'))
        with self.store.connect() as db:
            for path,kind,name in [('one','series','Show'),('two','series','Show'),('dated','tv','News'),('movie','movies','Film'),('live','playlist',''),('other','unsorted','')]:
                db.execute('INSERT INTO m3u_files VALUES(?,?,?,?,?,?)',(source,path,path,kind,'','digest'))
                db.execute('INSERT INTO m3u_file_info VALUES(?,?,?)',(source,path,json.dumps(dict(info=dict(name=name)))))
            db.execute('INSERT INTO m3u_files VALUES(?,?,?,?,?,?)',('removed-source','orphan','Orphan','movies','','digest'))
        stats=self.get().json['overview']['m3u']
        self.assertEqual(stats,dict(playlists=1,movies=1,shows=2,episodes=2,dated_tv=1,live_playlists=1,unsorted=1))
        with self.store.connect() as db:db.execute('DELETE FROM m3u_file_info WHERE path=?',('two',))
        self.assertIsNone(self.get().json['overview']['m3u']['shows'])
        self.assertEqual(self.app.test_client().get('/api/overview',base_url=LOCAL).status_code,401)

    def test_both_authenticated_overviews_include_configured_storage_and_legacy_fields(self):
        self.admin();self.store.set('setup_complete','true')
        extra = Path(self.tmp.name) / 'additional-media'
        extra.mkdir()
        self.app.config['REELDOCK_STORAGE_ROOTS'] = json.dumps([str(extra)])
        for endpoint in ('/api/dashboard', '/api/overview'):
            response = self.client.get(endpoint, base_url=LOCAL)
            self.assertEqual(response.status_code, 200)
            overview = response.json['overview'] if endpoint == '/api/dashboard' else response.json
            self.assertIn('media_disk', overview)
            self.assertIn('data_disk', overview)
            self.assertTrue(any(str(extra) in row['paths'] for row in overview['storage_disks']))
            anonymous = self.app.test_client().get(endpoint, base_url=LOCAL)
            self.assertNotIn('storage_disks', anonymous.json)

    def test_storage_configuration_warning_is_sanitized_private_and_clears_when_corrected(self):
        self.admin();self.store.set('setup_complete','true')
        self.app.config['REELDOCK_STORAGE_ROOTS'] = '[/private-invalid-imported-value]'
        for endpoint in ('/api/dashboard', '/api/overview'):
            response = self.client.get(endpoint, base_url=LOCAL)
            self.assertEqual(response.status_code, 200)
            overview = response.json['overview'] if endpoint == '/api/dashboard' else response.json
            self.assertTrue(overview['storage_disks_incomplete'])
            self.assertEqual(len(overview['storage_configuration_warnings']), 1)
            self.assertIn('REELDOCK_STORAGE_ROOTS must be a JSON array', overview['storage_configuration_warnings'][0])
            self.assertNotIn('private-invalid-imported-value', response.get_data(as_text=True))
            self.assertNotIn('storage_configuration_warnings', self.app.test_client().get(endpoint, base_url=LOCAL).json)
        self.app.config['REELDOCK_STORAGE_ROOTS'] = '[]'
        self.assertEqual(self.get().json['overview']['storage_configuration_warnings'], [])

if __name__ == '__main__': unittest.main()
