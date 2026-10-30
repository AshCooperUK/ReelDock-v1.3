import json
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
import test_youtube_optional as fixture
from backend import youtube_push, youtube_fallback
from backend.worker import tick

class FeedFirstTests(unittest.TestCase):
    setUp=fixture.OptionalYouTubeTests.setUp
    tearDown=fixture.OptionalYouTubeTests.tearDown
    google=fixture.OptionalYouTubeTests.google
    extract=fixture.OptionalYouTubeTests.extract
    feed=fixture.OptionalYouTubeTests.feed
    xml=fixture.OptionalYouTubeTests.xml

    def enable(self):
        self.store.set('youtube_use_google_api','true')
        self.store.set('youtube_subscriptions_next',time.time()+3600)

    def test_public_scan_uses_no_api_then_batches_missing_details_once(self):
        self.enable()
        def google(host,path,**kwargs):
            if host=='oauth2.googleapis.com':return {'access_token':'test','expires_in':3600}
            self.assertTrue(urlsplit(path).path.endswith('/videos'))
            return {'items':[youtube_fallback.video_item(self.extract('https://www.youtube.com/watch?v='+fixture.VID,'video',0,0),fixture.VID)]}
        self.importer.request=Mock(side_effect=google)
        self.importer.run()
        self.importer.request.assert_not_called()
        youtube_push.process(self.importer)
        self.assertEqual(self.importer.request.call_count,2)
        self.importer.request.reset_mock()
        with self.store.connect() as db:db.execute('UPDATE videos SET checked=?',(time.time()-86401,))
        self.importer.run();youtube_push.process(self.importer)
        self.importer.request.assert_not_called()

    def test_failed_account_sync_keeps_scanning_known_channels(self):
        self.enable();self.store.set('youtube_subscriptions_next',0)
        self.importer.request=Mock(side_effect=ValueError('Google authorisation unavailable'))
        self.importer.run()
        self.assertIn('authorisation',self.store.get('youtube_subscriptions_error'))
        youtube_push.process(self.importer)
        with self.store.connect() as db:
            video=json.loads(db.execute('SELECT data FROM videos WHERE id=?',(fixture.VID,)).fetchone()[0])
        self.assertFalse(video['metadata_pending'])
        self.assertTrue(self.extracts)

    def test_google_enabled_playlist_catchup_is_public(self):
        self.enable();self.importer.request=Mock(side_effect=AssertionError('No Google playlist requests'))
        result=self.importer.api('playlistItems',playlistId='UU'+fixture.CHANNEL[2:])
        self.assertEqual(result['items'][0]['contentDetails']['videoId'],fixture.VID)
        self.importer.request.assert_not_called()

    def test_incomplete_public_scan_continues_promptly_with_api_enabled(self):
        self.enable();self.store.set('next_sync',0);self.store.set('next_history',time.time()+3600)
        def partial():self.store.set('youtube_fallback_remaining',10)
        with patch.object(self.importer,'run',side_effect=partial):tick(self.importer,download_artwork=False)
        self.assertLess(float(self.store.get('next_sync')),time.time()+10)

    def test_subscription_and_channel_details_are_cached(self):
        self.enable();self.store.set('youtube_subscriptions_next',0)
        self.importer.run()
        first=len(self.calls)
        self.importer.run()
        self.assertEqual(len(self.calls),first)
        self.calls.clear();self.store.set('youtube_subscriptions_next',0)
        self.importer.run()
        resources=[urlsplit(path).path.rsplit('/',1)[-1] for _,path in self.calls]
        self.assertEqual(resources,['subscriptions'])
