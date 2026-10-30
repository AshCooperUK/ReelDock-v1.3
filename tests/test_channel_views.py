import unittest,json
import test_guide as fixture
class ChannelViewTests(unittest.TestCase):
    setUp=fixture.GuideTests.setUp
    tearDown=fixture.GuideTests.tearDown
    google=fixture.GuideTests.google
    def post(self,value,csrf='csrf-fixture'):
        return self.client.post('/api/channel-view',base_url=fixture.ORIGIN,json={'view':value},headers={'Origin':fixture.ORIGIN,'X-CSRF-Token':csrf})
    def test_preference_persists_between_signed_in_devices_without_google(self):
        self.store.set('youtube_use_google_api','false')
        self.assertEqual(self.client.get('/api/channel-view',base_url=fixture.ORIGIN).json['view'],'tile')
        for value in ('list','compact','detailed','max','tile'):
            self.assertEqual(self.post(value).status_code,200)
            other=self.app.test_client()
            with other.session_transaction(base_url=fixture.ORIGIN) as session:session['user_id']=1
            self.assertEqual(other.get('/api/channel-view',base_url=fixture.ORIGIN).json['view'],value)
        self.assertEqual(self.calls,[])
    def test_preference_rejects_unauthenticated_invalid_and_cross_origin_writes(self):
        self.assertEqual(self.app.test_client().get('/api/channel-view',base_url=fixture.ORIGIN).status_code,401)
        self.assertEqual(self.post('list','wrong').status_code,403)
        self.assertEqual(self.post('unknown').status_code,400)
        self.assertEqual(self.post(['list']).status_code,400)
        self.assertEqual(self.client.post('/api/channel-view',base_url=fixture.ORIGIN,json={'view':'list'},headers={'Origin':'https://other.test','X-CSRF-Token':'csrf-fixture'}).status_code,403)
        self.assertEqual(self.store.get('channel_view','tile'),'tile')
    def test_stats_use_indexed_published_uploads_not_scheduled_videos(self):
        channel=dict(id=fixture.CHANNEL,name='Example',avatar='')
        with self.store.connect() as db:db.execute('INSERT INTO channels VALUES(?,?,1)',(fixture.CHANNEL,json.dumps(channel)))
        for identity,date,state in [('published01','2026-09-26T10:00:00Z','none'),('scheduled01','2026-12-26T10:00:00Z','upcoming')]:
            self.importer.save_videos([dict(id=identity,snippet=dict(channelId=fixture.CHANNEL,title=identity,publishedAt=date,liveBroadcastContent=state),statistics={'viewCount':'20'})])
        item=self.client.get('/api/subscriptions',base_url=fixture.ORIGIN).json['items'][0]
        self.assertEqual(item['indexed_videos'],2)
        self.assertTrue(item['latest_upload'].startswith('2026-09-26'))
        self.assertEqual(item['recent_average'],20)
        self.assertEqual(item['saved_count'],0)
        self.assertEqual(self.calls,[])
if __name__=='__main__':unittest.main()
