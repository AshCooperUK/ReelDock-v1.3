import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from backend.app import create_app

LOCAL = 'http://reeldock.test:8790'
PASSWORD = 'a-long-reset-test-password'


class FactoryResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / 'media'
        self.media.mkdir()
        self.config = dict(TESTING=True, DATA_DIR=str(self.root/'data'), MEDIA_DIR=str(self.media/'YouTube'),
                           M3U_DIR=str(self.media/'M3U'), REELDOCK_STORAGE_ROOTS=json.dumps([str(self.media)]),
                           LOCAL_SETUP_URL=LOCAL, GOOGLE_CLIENT_ID='', GOOGLE_CLIENT_SECRET='')
        self.app = create_app(self.config)
        self.store = self.app.extensions['store']
        self.client = self.app.test_client()
        self.csrf = self.client.get('/api/setup/state', base_url=LOCAL).json['csrf']
        self.admin()
        self.post('/api/setup/skip-route', {})
        self.assertEqual(self.post('/api/setup/storage', {'base':str(self.media)}).status_code, 200)
        self.post('/api/setup/source-ready', {})
        self.assertEqual(self.post('/api/setup/finish', {}).status_code, 200)
        (self.media/'YouTube'/'saved.mp4').write_text('local media')
        (self.store.directory/'youtube-cookies.txt').write_text('private cookie')
        self.store.set('google_tokens', 'private tokens')

    def admin(self):
        response = self.post('/api/setup/admin', dict(username='owner', password=PASSWORD, confirm=PASSWORD))
        self.assertEqual(response.status_code, 200)
        self.csrf = response.json['csrf']

    def post(self, url='/api/account/reset', body=None, client=None, **kwargs):
        return (client or self.client).post(url, json=body if body is not None else dict(password=PASSWORD, confirmed=True),
            base_url=LOCAL, headers=kwargs.get('headers', {'Origin':LOCAL, 'X-CSRF-Token':self.csrf}))

    def test_reset_clears_data_and_requires_new_setup(self):
        with self.client.session_transaction(base_url=LOCAL) as session:
            old_session = dict(session)
        revision = self.store.get('auth_revision', '0')
        response = self.post()
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['url'], LOCAL+'/setup')
        self.assertIsNone(self.store.user())
        self.assertNotEqual(self.store.get('auth_revision'), revision)
        self.assertEqual(self.store.get('setup_complete'), '')
        self.assertEqual(self.store.get('google_tokens'), '')
        self.assertFalse((self.store.directory/'youtube-cookies.txt').exists())
        self.assertFalse((self.media/'YouTube'/'saved.mp4').exists())
        self.assertTrue((self.media/'YouTube').is_dir())
        self.assertTrue((self.media/'M3U').is_dir())
        with self.store.connect() as db:
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                if row[0] != 'settings':self.assertEqual(db.execute('SELECT count(*) FROM "'+row[0]+'"').fetchone()[0], 0, row[0])
        self.assertEqual(self.client.get('/api/dashboard', base_url=LOCAL).json['state'], 'setup')
        self.csrf = self.client.get('/api/setup/state', base_url=LOCAL).json['csrf']
        self.admin()
        stale = self.app.test_client()
        with stale.session_transaction(base_url=LOCAL) as session:session.update(old_session)
        self.assertEqual(stale.get('/api/dashboard', base_url=LOCAL).json['state'], 'sign_in')
        restarted = create_app(self.config)
        self.assertEqual(restarted.extensions['store'].get('setup_complete'), '')
        self.assertEqual(self.app.config['MEDIA_DIR'], self.config['MEDIA_DIR'])

    def test_auth_origin_csrf_password_confirmation_and_rate_limit(self):
        self.assertEqual(self.post(client=self.app.test_client()).status_code, 401)
        self.assertEqual(self.post(headers={'Origin':LOCAL}).status_code, 403)
        self.assertEqual(self.post(headers={'Origin':'https://evil.example','X-CSRF-Token':self.csrf}).status_code, 403)
        self.assertEqual(self.post(body=dict(password=PASSWORD,confirmed=False)).status_code, 400)
        for _ in range(5):self.assertEqual(self.post(body=dict(password='wrong', confirmed=True)).status_code, 400)
        self.assertEqual(self.post().status_code, 429)
        self.assertIsNotNone(self.store.user())
        self.assertTrue((self.media/'YouTube'/'saved.mp4').exists())

    def test_worker_operation_blocks_reset_without_deleting(self):
        ready, release = threading.Event(), threading.Event()
        def work():
            with self.store.operation():
                ready.set();release.wait(5)
        thread = threading.Thread(target=work);thread.start();ready.wait(5)
        try:
            self.assertEqual(self.post().status_code, 202)
            self.assertTrue((self.media/'YouTube'/'saved.mp4').exists())
        finally:
            release.set();thread.join()
        self.assertEqual(self.post().status_code, 200)

    def test_symlinks_do_not_delete_external_files(self):
        outside = self.root/'outside';outside.mkdir();(outside/'keep').write_text('keep')
        (self.media/'link').symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.post().status_code, 200)
        self.assertTrue((outside/'keep').exists())

    def test_unsafe_root_rejected_before_any_deletion(self):
        self.store.set('storage_base', '/tmp')
        response = self.post()
        self.assertEqual(response.status_code, 400)
        self.assertTrue((self.media/'YouTube'/'saved.mp4').exists())
        self.assertIsNotNone(self.store.user())

    def test_file_failure_keeps_account_and_pauses_work_for_retry(self):
        with patch('backend.reset.clear_directory', side_effect=PermissionError):
            response = self.post()
        self.assertEqual(response.status_code, 400)
        self.assertIsNotNone(self.store.user())
        self.assertEqual(self.store.get('reset_in_progress'), '1')
        self.assertEqual(self.post().status_code, 200)

    def test_bind_alias_identity_preserves_mount_directory(self):
        from backend.reset import clear_directory
        parent = self.root/'alias-test';nested = parent/'outer'/'mounted'
        nested.mkdir(parents=True);(nested/'media.mp4').write_text('remove')
        (parent/'obsolete').mkdir();(parent/'obsolete'/'old').write_text('remove')
        info = nested.stat()
        clear_directory(parent, preserve_inodes={(info.st_dev, info.st_ino)})
        self.assertTrue(nested.is_dir())
        self.assertEqual(list(nested.iterdir()), [])
        self.assertFalse((parent/'obsolete').exists())

    def test_active_worker_unwinds_automatically_before_deletion(self):
        ready, cleanup, finished = threading.Event(), threading.Event(), threading.Event()
        def work():
            with self.store.background_operation():
                try:
                    ready.set()
                    while True:
                        # Real workers check the database between provider calls
                        # and during download progress; this is the same boundary.
                        self.store.get('setup_complete')
                        finished.wait(.01)
                finally:
                    cleanup.set()
            finished.set()
        thread=threading.Thread(target=work);thread.start();self.assertTrue(ready.wait(2))
        try:
            result=self.post()
            self.assertIn(result.status_code,(200,202))
            self.assertTrue(finished.wait(3))
            self.assertTrue(cleanup.is_set())
            if result.status_code==202:
                self.assertEqual(result.json['state'],'stopping')
                result=self.post()
            self.assertEqual(result.status_code,200,result.json)
            self.assertIsNone(self.store.user())
        finally:
            self.store.set('reset_in_progress','1')
            thread.join(timeout=3)

    def test_unconfirmed_reset_does_not_pause_background_work(self):
        response=self.post(body=dict(password=PASSWORD))
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.store.get('reset_in_progress'),'')

    def test_metadata_subprocess_is_stopped_by_reset(self):
        import os
        import sys
        import time
        from backend.youtube_fallback import run_extractor
        output=self.root/'process.txt'
        finished=threading.Event()
        def work():
            try:
                with self.store.background_operation(), output.open('wb') as stream:
                    run_extractor([sys.executable,'-c','import os,time; print(os.getpid(),flush=True); time.sleep(60)'],input=b'',stdout=stream,store=self.store)
            finally:finished.set()
        thread=threading.Thread(target=work);thread.start()
        try:
            deadline=time.monotonic()+3
            while (not output.exists() or not output.read_text().strip()) and time.monotonic()<deadline:time.sleep(.02)
            pid=int(output.read_text().strip())
            self.store.set('reset_in_progress','1')
            self.assertTrue(finished.wait(5),'metadata subprocess did not stop')
            with self.assertRaises(ProcessLookupError):os.kill(pid,0)
        finally:
            self.store.set('reset_in_progress','1');thread.join(timeout=5)
