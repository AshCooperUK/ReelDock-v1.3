import time
import unittest
import test_channel_rules_everywhere as fixtures
from backend import downloads, activity, discover_store

class RetryTests(unittest.TestCase):
    setUp=fixtures.ChannelRulesEverywhereTests.setUp
    tearDown=fixtures.ChannelRulesEverywhereTests.tearDown
    video=fixtures.ChannelRulesEverywhereTests.video
    row=fixtures.ChannelRulesEverywhereTests.row
    post=fixtures.ChannelRulesEverywhereTests.post

    def test_failure_has_one_retry_alert_and_retry_keeps_quality(self):
        video=self.video(1,100)
        discover_store.set_quality_policy(self.store,video['id'],False)
        request,_=discover_store.insert_request(self.store,dict(idempotency='retry-fixture',kind='video',title=video['title'],quality='720',state='failed',message='Interrupted',body={'videoId':video['id']}))
        downloads.enqueue(self.store,video,'720')
        downloads.update(self.store,video['id'],'failed',message='Interrupted')
        discover_store.update_request(self.store,request['id'],state='failed')
        alerts=activity.snapshot(self.store)['alerts']
        self.assertEqual(len(alerts),1)
        self.assertEqual(alerts[0]['retry'],{'id':video['id']})
        self.store.set('download_quality','2160')
        response=self.post({'id':video['id'],'action':'retry'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.row(video['id'])['quality'],'720')
        self.assertEqual(self.row(video['id'])['state'],'queued')
        self.assertFalse(activity.snapshot(self.store)['alerts'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('select state from discover_requests where id=?',(request['id'],)).fetchone()[0],'queued')
        self.assertEqual(self.post({'id':video['id'],'action':'retry'}).status_code,200)
        with self.store.connect() as db:self.assertEqual(db.execute('select count(*) from downloads').fetchone()[0],1)

    def test_retry_still_requires_csrf(self):
        from test_channel_rules_everywhere import ORIGIN
        video=self.video(1,0)
        downloads.enqueue(self.store,video,'720')
        downloads.update(self.store,video['id'],'failed',message='Interrupted')
        response=self.client.post('/api/downloads',base_url=ORIGIN,json={'id':video['id'],'action':'retry'},headers={'Origin':ORIGIN})
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.row(video['id'])['state'],'failed')
