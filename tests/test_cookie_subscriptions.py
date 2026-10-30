import io
import json
import time
import unittest
from unittest.mock import patch
import test_youtube_optional as fixture
from backend.cookie_subscriptions import sync
from backend.youtube_fallback import channel_url

COOKIE=b'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t4102444800\tSID\tfixture\n'
NEW='UC'+'n'*22
REMOVED='UC'+'r'*22
class CookieSubscriptionTests(unittest.TestCase):
    setUp=fixture.OptionalYouTubeTests.setUp
    tearDown=fixture.OptionalYouTubeTests.tearDown
    google=fixture.OptionalYouTubeTests.google
    extract=fixture.OptionalYouTubeTests.extract
    feed=fixture.OptionalYouTubeTests.feed
    xml=fixture.OptionalYouTubeTests.xml
    post=fixture.OptionalYouTubeTests.post

    def test_cookie_import_is_additive_and_respects_local_removals(self):
        (self.store.directory/'youtube-cookies.txt').write_bytes(COOKIE);self.store.set('youtube_cookie_import_channels','true')
        with self.store.connect() as db:db.execute('INSERT INTO removed_channels VALUES(?)',(REMOVED,))
        entries=[dict(id=identity,channel_id=identity,title='Channel') for identity in [NEW,REMOVED,fixture.CHANNEL]]
        with patch('backend.cookie_subscriptions.extract',return_value={'entries':entries}) as extract:
            sync(self.importer);sync(self.importer);extract.assert_called_once()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM channels').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT videos FROM download_rules WHERE channel=?',(fixture.CHANNEL,)).fetchone()[0],1)
        self.assertIn('1 added',self.store.get('youtube_cookie_subscriptions_status'))
        self.assertFalse(self.calls)

    def test_rejected_or_partial_cookie_listing_preserves_channels(self):
        (self.store.directory/'youtube-cookies.txt').write_bytes(COOKIE);self.store.set('youtube_cookie_import_channels','true')
        for result in ({'entries':[]},{'entries':[{'id':'not-a-channel'}]}):
            self.store.set('youtube_cookie_subscriptions_next',0)
            with patch('backend.cookie_subscriptions.extract',return_value=result):sync(self.importer)
            with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM channels').fetchone()[0],1)
            self.assertIn('could not finish',self.store.get('youtube_cookie_subscriptions_status'))

    def test_setup_cookie_and_manual_paths_require_login_and_csrf(self):
        self.store.set('setup_complete','')
        url='/api/setup/import-method'
        self.assertEqual(self.app.test_client().post(url,base_url=fixture.ORIGIN,json={'method':'cookies'},headers={'Origin':fixture.ORIGIN}).status_code,401)
        self.assertEqual(self.client.post(url,base_url=fixture.ORIGIN,json={'method':'cookies'},headers={'Origin':fixture.ORIGIN}).status_code,403)
        self.assertEqual(self.post(url,{'method':'cookies'}).status_code,200)
        self.assertEqual(self.post('/api/setup/source-ready',{}).status_code,409)
        response=self.client.post('/api/setup/cookies',base_url=fixture.ORIGIN,data={'cookies':(io.BytesIO(COOKIE),'cookies.txt')},headers={'Origin':fixture.ORIGIN,'X-CSRF-Token':'csrf-fixture'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.post('/api/setup/source-ready',{}).status_code,200)
        state=self.client.get('/api/setup/state',base_url=fixture.ORIGIN).json
        self.assertEqual(state['import_method'],'cookies');self.assertTrue(state['source_ready']);self.assertFalse(state['use_google_api'])
        self.assertNotIn('SID',json.dumps(state))
        self.assertEqual(self.post(url,{'method':'manual'}).status_code,200)
        self.assertEqual(self.post('/api/setup/channels',{'channels':'@TopDeadC'}).status_code,200)
        self.assertEqual(channel_url('TopDeadC'),'https://www.youtube.com/@TopDeadC')

    def test_available_google_takes_priority_over_cookie_import(self):
        self.store.set('youtube_use_google_api','true');self.store.set('youtube_subscriptions_next',time.time()+3600)
        with patch('backend.cookie_subscriptions.sync') as cookies:self.importer.run();cookies.assert_not_called()
