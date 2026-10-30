"""Bundled UI code and health remain available without mutable app storage."""
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from backend.app import create_app, public_state_free_request
from backend import storage_layout

LOCAL='http://127.0.0.1:8790'


class PublicRequestFastlaneTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.app=create_app({'TESTING':True,'DATA_DIR':str(root/'data'),
            'MEDIA_DIR':str(root/'media'),'M3U_DIR':str(root/'m3u'),
            'LOCAL_SETUP_URL':LOCAL,'CANDIDATE_URL':'','GOOGLE_CLIENT_ID':'','GOOGLE_CLIENT_SECRET':''})
        self.store=self.app.extensions['store'];self.client=self.app.test_client()

    def unavailable_state(self):
        scope=ExitStack()
        for method in ('begin_operation','end_operation','begin_reuse','end_reuse','connect','get','set','user'):
            scope.enter_context(patch.object(self.store,method,side_effect=AssertionError('Public file touched mutable storage: '+method)))
        scope.enter_context(patch.object(storage_layout,'apply',side_effect=AssertionError('Public file applied media paths')))
        return scope

    def test_actual_bundled_assets_and_health_get_head_need_no_store_or_layout(self):
        paths=('/health','/preview/assets/theme-boot.js','/preview/assets/reeldock-brand.svg','/static/tmdb-logo.svg')
        with self.unavailable_state():
            for method in ('GET','HEAD'):
                for path in paths:
                    with self.subTest(method=method,path=path),self.client.open(path,method=method,base_url=LOCAL) as response:
                        self.assertEqual(response.status_code,200)
                        self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')
                        self.assertIn("script-src 'self'",response.headers['Content-Security-Policy'])
                        if method=='HEAD':self.assertEqual(response.get_data(),b'')
                        elif path=='/health':self.assertEqual(response.json['status'],'ok')
                        else:self.assertTrue(response.get_data())

    def test_public_files_remain_available_on_foreign_host_without_exposing_state(self):
        self.store.set('public_origin','https://verified.example.test')
        self.store.set('reset_in_progress','1')
        with self.unavailable_state():
            for path in ('/health','/preview/assets/theme-boot.js','/static/tmdb-logo.svg'):
                with self.subTest(path=path),self.client.get(path,base_url='http://foreign.example.test') as response:
                    self.assertEqual(response.status_code,200)
                    self.assertNotIn('Location',response.headers)
                    self.assertNotIn(b'verified.example.test',response.get_data())

    def test_public_binary_asset_does_not_enter_streamed_media_reset_guard(self):
        folder=Path(self.temp.name)/'public';folder.mkdir()
        (folder/'fixture.bin').write_bytes(b'public bundled fixture')
        self.app.static_folder=str(folder)
        with self.unavailable_state(),self.client.get('/static/fixture.bin',base_url=LOCAL) as response:
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.mimetype,'application/octet-stream')
            self.assertEqual(response.get_data(),b'public bundled fixture')

    def test_missing_public_asset_stays_404_and_cannot_traverse_source(self):
        with self.unavailable_state():
            for path in ('/preview/assets/missing-AAAAAAAA.js','/preview/assets/../../backend/app.py'):
                with self.subTest(path=path),self.client.get(path,base_url=LOCAL) as response:
                    self.assertEqual(response.status_code,404)
                    self.assertEqual(response.headers['Cache-Control'],'no-store')
                    self.assertNotIn(b'def create_app',response.get_data())

    def test_only_exact_get_head_health_or_asset_prefix_is_state_free(self):
        for method in ('GET','HEAD','POST','OPTIONS','DELETE'):
            for path in ('/health','/health/','/static/icon.svg','/preview/assets/icon.svg',
                         '/static-private/icon.svg','/preview/assets-private/icon.svg',
                         '/app/','/login','/api/dashboard','/api/setup/state','/setup/probe/fixture'):
                with self.subTest(method=method,path=path),self.app.test_request_context(path,method=method):
                    expected=method in ('GET','HEAD') and (path=='/health' or path.startswith(('/static/','/preview/assets/')))
                    self.assertEqual(public_state_free_request(),expected)

    def test_protected_html_api_and_setup_probe_still_enter_storage_scope(self):
        for path in ('/app/','/login','/setup','/api/dashboard','/api/setup/state','/setup/probe/fixture','/health/'):
            with self.subTest(path=path),patch.object(self.store,'begin_operation',wraps=self.store.begin_operation) as begin,patch.object(self.store,'end_operation',wraps=self.store.end_operation) as end,patch.object(self.store,'begin_reuse',wraps=self.store.begin_reuse) as reuse,patch.object(self.store,'end_reuse',wraps=self.store.end_reuse) as release,patch.object(storage_layout,'apply',side_effect=AssertionError('protected media state')):
                with self.assertRaisesRegex(AssertionError,'protected media state'):
                    self.client.get(path,base_url=LOCAL)
                begin.assert_called_once();end.assert_called_once();reuse.assert_called_once();release.assert_called_once()

    def test_foreign_protected_pages_and_api_keep_verified_origin_guard(self):
        self.store.set('public_origin','https://verified.example.test')
        for path in ('/app/','/api/dashboard','/login'):
            with self.subTest(path=path),self.client.get(path,base_url=LOCAL) as response:
                self.assertEqual(response.status_code,302)
                self.assertTrue(response.headers['Location'].startswith('https://verified.example.test/'))
        with self.client.post('/api/activity/clear',json={'section':'completed'},base_url=LOCAL,headers={'Origin':LOCAL}) as response:
            self.assertEqual(response.status_code,403)

    def test_anonymous_and_invalid_csrf_activity_actions_are_still_rejected(self):
        self.store.set('setup_complete','1')
        with self.client.get('/api/activity',base_url=LOCAL) as response:self.assertEqual(response.status_code,401)
        with self.store.connect() as db:db.execute("INSERT INTO users(id,username,password_hash) VALUES(1,'fixture','unused-fixture-hash')")
        with self.client.session_transaction(base_url=LOCAL) as session:
            session.update(user_id=1,auth_revision=self.store.get('auth_revision','0'),csrf='fixture-csrf')
        with self.client.post('/api/activity/clear',json={'section':'completed'},base_url=LOCAL,headers={'Origin':LOCAL,'X-CSRF-Token':'wrong'}) as response:
            self.assertEqual(response.status_code,403)
        with self.client.post('/api/activity/clear',json={'section':'completed'},base_url=LOCAL,headers={'Origin':'http://foreign.example.test','X-CSRF-Token':'fixture-csrf'}) as response:
            self.assertEqual(response.status_code,403)


if __name__=='__main__':unittest.main()
