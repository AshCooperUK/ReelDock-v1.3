"""Two independent YouTube cookie permissions: channel import (off) and downloads (on)."""
import json
import subprocess
import threading
import unittest
from unittest.mock import patch

import test_downloads as fixture
from backend import auto_downloads, cookie_subscriptions, cookies, discover_youtube, downloads
from backend.settings import youtube as youtube_settings

ORIGIN = fixture.ORIGIN
COOKIE = '# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t4102444800\tSID\tfixture\n'
NEW = 'UC' + 'n' * 22


class Stop(Exception):
    pass


class CookiePermissionTests(unittest.TestCase):
    setUp = fixture.DownloadTests.setUp
    tearDown = fixture.DownloadTests.tearDown
    post = fixture.DownloadTests.post
    row = fixture.DownloadTests.row

    def save(self, **values):
        return self.client.post('/api/settings/youtube', base_url=ORIGIN, json={'values': values},
                                headers={'Origin': ORIGIN, 'X-CSRF-Token': 'token'})

    def cookie(self):
        (self.store.directory / 'youtube-cookies.txt').write_text(COOKIE)

    def test_defaults_api_persistence_validation_and_independence(self):
        values = self.client.get('/api/settings/youtube', base_url=ORIGIN).json['values']
        self.assertIs(values['cookie_import_channels'], False)
        self.assertIs(values['cookie_downloads'], True)
        self.assertFalse(cookies.allow_channel_import(self.store))
        self.assertTrue(cookies.allow_downloads(self.store))
        self.assertEqual(self.save(cookie_import_channels=True).status_code, 200)
        self.assertTrue(cookies.allow_channel_import(self.store))
        self.assertTrue(cookies.allow_downloads(self.store))
        self.assertEqual(self.save(cookie_downloads=False).status_code, 200)
        self.assertTrue(cookies.allow_channel_import(self.store))
        self.assertFalse(cookies.allow_downloads(self.store))
        stored = self.client.get('/api/settings/youtube', base_url=ORIGIN).json['values']
        self.assertEqual((stored['cookie_import_channels'], stored['cookie_downloads']), (True, False))
        for bad in ('yes', 1, None):
            self.assertEqual(self.save(cookie_downloads=bad).status_code, 400)
            self.assertEqual(self.save(cookie_import_channels=bad).status_code, 400)
        self.assertFalse(cookies.allow_downloads(self.store))
        self.assertEqual(self.client.post('/api/settings/youtube', base_url=ORIGIN, json={'values': {'cookie_downloads': True}},
                                          headers={'Origin': ORIGIN, 'X-CSRF-Token': 'wrong'}).status_code, 403)
        self.assertEqual(self.app.test_client().post('/api/settings/youtube', base_url=ORIGIN, json={'values': {'cookie_downloads': True}}).status_code in (401, 403), True)
        self.assertFalse(cookies.allow_downloads(self.store))

    def test_unset_or_corrupt_values_use_defaults(self):
        self.store.set('youtube_cookie_import_channels', 'maybe')
        self.store.set('youtube_cookie_downloads', 'maybe')
        self.assertFalse(cookies.allow_channel_import(self.store))
        self.assertTrue(cookies.allow_downloads(self.store))

    def test_enabling_import_schedules_and_disabling_clears_status_only(self):
        self.store.set('youtube_cookie_subscriptions_status', 'old status')
        self.store.set('youtube_cookie_subscriptions_next', '999')
        self.cookie()
        self.assertEqual(self.save(cookie_import_channels=True).status_code, 200)
        self.assertEqual((self.store.get('youtube_cookie_subscriptions_next'), self.store.get('next_sync')), ('0', '0'))
        self.assertEqual(self.save(cookie_import_channels=False).status_code, 200)
        self.assertEqual(self.store.get('youtube_cookie_subscriptions_status'), '')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM channels').fetchone()[0], 1)
        self.assertTrue((self.store.directory / 'youtube-cookies.txt').is_file())

    # Channel import
    def importer(self):
        from types import SimpleNamespace
        return SimpleNamespace(store=self.store)

    def test_import_default_off_touches_nothing(self):
        self.cookie()
        with patch.object(cookie_subscriptions, 'extract') as extract:
            cookie_subscriptions.sync(self.importer())
            extract.assert_not_called()
        self.assertEqual(self.store.get('youtube_cookie_subscriptions_next', '0'), '0')
        self.assertEqual(self.store.get('youtube_cookie_subscriptions_status', ''), '')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM channels').fetchone()[0], 1)

    def test_enabled_import_adds_and_disable_during_extraction_adds_nothing(self):
        self.cookie()
        self.store.set('youtube_cookie_import_channels', 'true')
        entries = [dict(id=NEW, channel_id=NEW, title='New')]
        with patch.object(cookie_subscriptions, 'extract', return_value={'entries': entries}):
            cookie_subscriptions.sync(self.importer())
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM channels').fetchone()[0], 2)

        def disable(*args, **kwargs):
            self.store.set('youtube_cookie_import_channels', 'false')
            return {'entries': [dict(id='UC' + 'q' * 22, channel_id='UC' + 'q' * 22, title='Q')]}
        self.store.set('youtube_cookie_import_channels', 'true')
        self.store.set('youtube_cookie_subscriptions_next', '0')
        with patch.object(cookie_subscriptions, 'extract', side_effect=disable):
            cookie_subscriptions.sync(self.importer())
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM channels').fetchone()[0], 2)

    def test_disable_between_channel_additions_stops_remaining(self):
        self.cookie()
        self.store.set('youtube_cookie_import_channels', 'true')
        first, second = 'UC' + 'a' * 22, 'UC' + 'b' * 22
        entries = [dict(id=first, channel_id=first, title='A'), dict(id=second, channel_id=second, title='B')]
        real = cookies.allow_channel_import
        calls = []

        def allowed(store):
            calls.append(1)
            if len(calls) == 5:  # start, extraction, entry one (loop, pre-insert), entry two loop
                store.set('youtube_cookie_import_channels', 'false')
            return real(store)
        with patch.object(cookie_subscriptions, 'extract', return_value={'entries': entries}), patch.object(cookie_subscriptions, 'allow_channel_import', side_effect=allowed):
            cookie_subscriptions.sync(self.importer())
        with self.store.connect() as db:
            ids = {r[0] for r in db.execute('SELECT id FROM channels')}
        self.assertIn(first, ids)
        self.assertNotIn(second, ids)

    def test_thumbnail_work_cannot_import_after_disable(self):
        self.cookie()
        self.store.set('youtube_cookie_import_channels', 'true')
        entries = [dict(id=NEW, channel_id=NEW, title='New', thumbnails=[{'url': 'https://example.invalid/a.jpg'}])]

        def slow(store, payload):
            store.set('youtube_cookie_import_channels', 'false')
            return '/avatar.jpg'
        with patch.object(cookie_subscriptions, 'extract', return_value={'entries': entries}), patch('backend.catalogue.thumbnail', side_effect=slow) as thumbnail:
            cookie_subscriptions.sync(self.importer())
        thumbnail.assert_called_once()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM channels').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM youtube_manual_channels WHERE id=?', (NEW,)).fetchone()[0], 0)

    def setup_post(self, path, body):
        return self.client.post(path, base_url=ORIGIN, json=body, headers={'Origin': ORIGIN, 'X-CSRF-Token': 'token'})

    def test_only_explicit_setup_cookies_choice_enables_import(self):
        self.store.set('setup_complete', '')
        self.assertFalse(cookies.allow_channel_import(self.store))
        self.assertEqual(self.store.get('youtube_cookie_import_channels'), '')
        for method in ('manual', 'google'):
            if method == 'google':
                self.store.set('local_only', '0')
            self.setup_post('/api/setup/import-method', {'method': method})
            self.assertFalse(cookies.allow_channel_import(self.store), method)
        upload = self.client.post('/api/setup/cookies', base_url=ORIGIN, data={'cookies': (__import__('io').BytesIO(COOKIE.encode()), 'cookies.txt')},
                                  headers={'Origin': ORIGIN, 'X-CSRF-Token': 'token'})
        self.assertEqual(upload.status_code, 200)
        self.assertFalse(cookies.allow_channel_import(self.store))
        self.setup_post('/api/setup/source-ready', {})
        self.client.get('/api/setup/state', base_url=ORIGIN)
        self.assertFalse(cookies.allow_channel_import(self.store))
        self.assertEqual(self.setup_post('/api/setup/import-method', {'method': 'bogus'}).status_code, 400)
        self.assertFalse(cookies.allow_channel_import(self.store))
        self.assertEqual(self.setup_post('/api/setup/import-method', {'method': 'cookies'}).status_code, 200)
        self.assertTrue(cookies.allow_channel_import(self.store))
        self.assertTrue(cookies.allow_downloads(self.store))
        self.store.set('youtube_cookie_downloads', 'false')
        self.setup_post('/api/setup/import-method', {'method': 'cookies'})
        self.assertFalse(cookies.allow_downloads(self.store))
        self.setup_post('/api/setup/import-method', {'method': 'manual'})
        self.assertTrue(cookies.allow_channel_import(self.store))

    # Download use
    def standard(self, **options):
        self.cookie()
        self.post({'action': 'options', 'options': options})
        self.post({'id': fixture.VIDEO['id']})
        calls = []

        def process(store, job, stop, args, log, root):
            calls.append(args[:])
            fixture.completed_media(log.parent,job,b'complete')
            return 0, ''
        with patch('backend.downloads.run_process', side_effect=process):
            downloads.tick(self.store, self.media, threading.Event())
        return calls[0]

    def test_standard_download_gate(self):
        self.assertIn('--cookies', self.standard(authenticated_retry=True))

    def test_standard_download_gate_off(self):
        self.store.set('youtube_cookie_downloads', 'false')
        self.assertNotIn('--cookies', self.standard(authenticated_retry=True))

    def test_standard_download_retry_setting_still_required(self):
        self.assertNotIn('--cookies', self.standard(authenticated_retry=False))

    def test_standard_probe_gate_off(self):
        from backend import channel_downloads
        from types import SimpleNamespace
        channel_downloads.save(self.store, 'channel', {'videos': True, 'range_days': 0})
        self.cookie()
        self.post({'action': 'options', 'options': {'authenticated_retry': True}})
        results = {}
        for allowed in ('true', 'false'):
            self.store.set('youtube_cookie_downloads', allowed)
            seen = []

            def probe(args, stdout, stderr, **kwargs):
                seen.append(args[:])
                stdout.write(json.dumps({'width': 1920, 'height': 1080, 'duration': 120}).encode())
                return SimpleNamespace(returncode=0)
            with patch('backend.downloads.subprocess.run', side_effect=probe), patch('backend.downloads.shutil.disk_usage', return_value=SimpleNamespace(free=2 * 1024 ** 3)):
                try:
                    downloads.run_download(self.store, self.media, {**fixture.VIDEO, 'quality': '720', 'automatic': True}, threading.Event())
                except Exception:
                    pass
            results[allowed] = ['--cookies' in args for args in seen]
        self.assertEqual(results['true'], [True])
        self.assertEqual(results['false'], [False])

    def test_discover_runner_gate(self):
        self.cookie()
        for allowed,retry,expected in (('true',True,True),('false',True,False),('true',False,False)):
            self.store.set('youtube_cookie_downloads', allowed)
            self.store.set('download_authenticated_retry','true' if retry else 'false')
            seen = []

            def process(store, job, stop, args, log, root):
                seen.append(args[:])
                raise Stop
            with patch('backend.discover_youtube.quality_policy', return_value=True), patch('backend.downloads.check_rules'), \
                 patch('backend.downloads.run_process', side_effect=process), patch('shutil.disk_usage', return_value=type('U', (), {'free': 2 * 1024 ** 3})()):
                with self.assertRaises(Stop):
                    discover_youtube.run(self.store, self.media, {'id': 'abcdefghijk', 'title': 'T', 'quality': '720'}, threading.Event())
            self.assertEqual('--cookies' in seen[0], expected)

    def test_auto_download_gate(self):
        self.cookie()
        info = dict(title='Auto', upload_date='20261010', channel='C', channel_id='UC' + 'a' * 22, id='abcdefghijk')
        for allowed, retry, expected in (('true', True, True), ('false', True, False), ('true', False, False)):
            self.store.set('youtube_cookie_downloads', allowed)
            self.store.set('download_authenticated_retry', 'true' if retry else 'false')
            seen = []

            def popen(args, **kwargs):
                seen.append(args[:])
                raise Stop
            with patch.object(auto_downloads, '_probe_youtube', return_value=info), patch.object(auto_downloads, '_channel_images', return_value={}), \
                 patch('backend.auto_downloads.subprocess.Popen', side_effect=popen), patch('backend.activity.record'):
                with self.assertRaises(Stop):
                    auto_downloads._save_youtube(self.app, self.store, 'abcdefghijk', threading.Event(), 'task')
            self.assertEqual('--cookies' in seen[0], expected, (allowed, retry))

    def test_extract_youtube_video_gate_and_channel_unchanged(self):
        self.cookie()
        video = 'https://www.youtube.com/watch?v=abcdefghijk'
        for mode,url,allowed,retry,expected in (('video',video,'true',True,True),('video',video,'false',True,False),
                                               ('video',video,'true',False,False),('channel','https://www.youtube.com/@Some','false',False,True)):
            self.store.set('youtube_cookie_downloads', allowed)
            self.store.set('download_authenticated_retry','true' if retry else 'false')
            seen = []

            def run(args, input=b'', **kwargs):
                seen.append(json.loads(input))
                return subprocess.CompletedProcess(args, 0, b'{}')

            def extractor(args, input, stdout, **kwargs):
                seen.append(json.loads(input))
                stdout.write(b'{}')
            with patch('backend.auto_downloads.subprocess.run', side_effect=run), patch('backend.youtube_fallback.run_extractor', side_effect=extractor):
                auto_downloads._extract_youtube(self.store, url, mode)
            self.assertEqual(len(seen), 1)
            self.assertEqual('cookie' in seen[0], expected, (mode, allowed))
        self.assertEqual(list(self.store.directory.glob('youtube-cookies-*')), [])

    def test_field_definitions_are_single_source(self):
        self.assertIs(youtube_settings.FIELDS['cookie_import_channels'].default, False)
        self.assertIs(youtube_settings.FIELDS['cookie_downloads'].default, True)


if __name__ == '__main__':
    unittest.main()
