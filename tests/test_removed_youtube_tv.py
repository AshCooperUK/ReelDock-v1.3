"""Removed TV feeds stay unavailable without disrupting ordinary YouTube work."""
import unittest
from unittest.mock import patch
from tests import test_engine_settings as fixture
from backend.sync_all import queue, tick, status

class RemovedYouTubeTvTests(unittest.TestCase):
    setUp = fixture.EngineSettingsTests.setUp

    def test_old_settings_and_feed_routes_are_gone_even_with_legacy_config(self):
        self.store.set('youtube_tv_enabled', 'true')
        self.store.set('youtube_tv_token', 'retired-fixture-token')
        paths = ['/api/youtube-tv', '/youtube-tv/retired-fixture-token/channels.m3u',
                 '/youtube-tv/retired-fixture-token/guide.xml',
                 '/youtube-tv/retired-fixture-token/artwork/channel',
                 '/youtube-tv/retired-fixture-token/channel/channel.ts']
        self.assertFalse(any('youtube-tv' in rule.rule for rule in self.app.url_map.iter_rules()))
        for client in (self.client, self.app.test_client()):
            for path in paths:
                self.assertEqual(client.get(path, base_url=fixture.ORIGIN).status_code, 404, path)
        response = self.client.post('/api/youtube-tv', base_url=fixture.ORIGIN,
            headers={'Origin':fixture.ORIGIN,'X-CSRF-Token':'test-token'}, json={'enabled':True})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get('/api/settings/youtube', base_url=fixture.ORIGIN).status_code, 200)

    def test_fresh_database_does_not_create_tv_channel_table(self):
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='youtube_tv_numbers'").fetchone())

    def test_sync_all_ignores_retired_pending_work(self):
        self.store.set('youtube_tv_requested', '1')
        self.store.set('youtube_tv_enabled', 'true')
        with patch('backend.sync_all.time.time', return_value=1000):
            queue(self.store)
        self.store.set('sync_requested', '0')
        with patch('backend.sync_all.time.time', return_value=1100):
            tick(self.store)
            tick(self.store)
            self.assertFalse(status(self.store)['busy'])
