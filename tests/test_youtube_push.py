import hashlib
import hmac
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from urllib.parse import urlsplit
from unittest.mock import patch
from backend.app import create_app
from backend.catalogue import Importer
from backend import youtube_push as push, channel_downloads, downloads

ORIGIN='https://reeldock.example.com'
CHANNEL='UC'+'a'*22
VID='abcdefghijk'

class PushTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=create_app({'TESTING':True,'DATA_DIR':self.temp.name,'MEDIA_DIR':self.temp.name+'/media'})
        self.store=self.app.extensions['store'];self.client=self.app.test_client()
        for k,v in [('public_origin',ORIGIN),('setup_complete','1'),('google_tokens','{}')]:self.store.set(k,v)
        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES (?,?,1)',(CHANNEL,json.dumps({'id':CHANNEL,'name':'Channel'})))
        self.requests=[]
        push.renew(self.store,self.requests.append)
        self.path=urlsplit(self.requests[0]['hub.callback']).path
        self.secret=self.requests[0]['hub.secret']
        self.importer=Importer(self.app)

    def tearDown(self):self.temp.cleanup()

    def feed(self,title='New video',channel=CHANNEL):
        return f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"><entry><yt:videoId>{VID}</yt:videoId><yt:channelId>{channel}</yt:channelId><title>{title}</title></entry></feed>'.encode()

    def deliver(self,body=None,signature=True):
        body=self.feed() if body is None else body
        headers={'Content-Type':'application/atom+xml'}
        if signature:headers['X-Hub-Signature']='sha1='+hmac.new(self.secret.encode(),body,hashlib.sha1).hexdigest()
        return self.client.post(self.path,base_url=ORIGIN,data=body,headers=headers)

    def event(self):
        with self.store.connect() as db:
            r=db.execute('SELECT * FROM youtube_push_events').fetchone()
            return dict(r) if r else None

    def video(self):
        return {'id':VID,'snippet':{'channelId':CHANNEL,'title':'New video','publishedAt':datetime.now(timezone.utc).isoformat(),
                'thumbnails':{'medium':{'url':'https://i.ytimg.com/vi/'+VID+'/mqdefault.jpg'}}},'contentDetails':{'duration':'PT5M'}}

    def test_hub_verification_is_plain_text_and_persists_lease(self):
        r=self.client.get(self.path,base_url=ORIGIN,query_string={'hub.mode':'subscribe','hub.topic':push.topic(CHANNEL),
                          'hub.challenge':'<challenge>','hub.lease_seconds':'86400'})
        self.assertEqual(r.status_code,200);self.assertEqual(r.data,b'<challenge>');self.assertTrue(r.content_type.startswith('text/plain'))
        with self.store.connect() as db:self.assertGreater(db.execute('SELECT expires FROM youtube_push_subscriptions').fetchone()[0],time.time()+86000)
        push.renew(self.store,self.requests.append);self.assertEqual(len(self.requests),1)

    def test_wrong_challenge_topic_or_token_rejected(self):
        r=self.client.get(self.path,base_url=ORIGIN,query_string={'hub.mode':'subscribe','hub.topic':'wrong','hub.challenge':'x','hub.lease_seconds':'100'})
        self.assertEqual(r.status_code,404)
        self.assertEqual(self.client.post('/hooks/youtube/unknown',base_url=ORIGIN).status_code,404)

    def test_unsigned_tampered_and_other_channel_rejected(self):
        self.assertEqual(self.deliver(signature=False).status_code,403)
        self.assertEqual(self.deliver(self.feed(channel='UC'+'b'*22)).status_code,400)
        r=self.client.post(self.path,base_url=ORIGIN,data=self.feed(),headers={'X-Hub-Signature':'sha1='+'0'*40})
        self.assertEqual(r.status_code,403);self.assertIsNone(self.event())
        self.assertEqual(self.client.post('/api/downloads',base_url=ORIGIN,json={'action':'cancel_all'}).status_code,403)

    def test_payload_limits_and_xml_entities(self):
        self.assertEqual(self.deliver(b'x'*(push.LIMIT+1)).status_code,413)
        self.assertEqual(self.deliver(b'<!DOCTYPE feed [<!ENTITY a "x">]><feed/>').status_code,400)
        self.assertEqual(self.deliver(b'<feed').status_code,400)

    def test_duplicate_delivery_and_title_update(self):
        self.assertEqual(self.deliver().status_code,204)
        with self.store.connect() as db:db.execute('UPDATE youtube_push_events SET done=1')
        self.assertEqual(self.deliver().status_code,204);self.assertEqual(self.event()['done'],1)
        self.deliver(self.feed('Edited title'));self.assertEqual(self.event()['done'],0)

    def test_import_artwork_with_downloads_disabled_then_enabled_and_no_repeat(self):
        self.deliver()
        with patch.object(self.importer,'api',return_value={'items':[self.video()]}):push.process(self.importer)
        self.assertEqual(self.event()['done'],1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM videos').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM artwork').fetchone()[0],1)
        self.assertEqual(channel_downloads.schedule(self.store,self.app.config['MEDIA_DIR']),0)
        channel_downloads.save(self.store,CHANNEL,{'videos':True})
        self.assertEqual(channel_downloads.schedule(self.store,self.app.config['MEDIA_DIR']),1)
        downloads.update(self.store,VID,'complete',100)
        self.deliver(self.feed('New title'))
        with patch.object(self.importer,'api',return_value={'items':[self.video()]}):push.process(self.importer)
        self.assertEqual(channel_downloads.schedule(self.store,self.app.config['MEDIA_DIR']),0)

    def test_api_failure_and_temporarily_missing_video_retry_without_losing_event(self):
        self.deliver()
        with patch.object(self.importer,'api',side_effect=ValueError('quotaExceeded')):push.process(self.importer)
        self.assertEqual(self.event()['done'],0);self.assertGreater(self.event()['retry_at'],time.time())
        with self.store.connect() as db:db.execute('UPDATE youtube_push_events SET retry_at=0')
        with patch.object(self.importer,'api',return_value={'items':[]}):push.process(self.importer)
        self.assertEqual(self.event()['attempts'],2)
        with self.store.connect() as db:db.execute('UPDATE youtube_push_events SET retry_at=0')
        recreated=Importer(self.app)
        with patch.object(recreated,'api',return_value={'items':[self.video()]}):push.process(recreated)
        self.assertEqual(self.event()['done'],1)

    def test_feed_details_process_without_public_callback_origin(self):
        self.deliver();self.store.set('public_origin','')
        with patch.object(self.importer,'api',return_value={'items':[self.video()]}):push.process(self.importer)
        self.assertEqual(self.event()['done'],1)
        self.assertEqual(self.store.get('youtube_push_running'),'0')

    def test_quota_transition_does_not_delay_unattempted_videos(self):
        with self.store.connect() as db:
            for i in range(5):
                db.execute('INSERT INTO youtube_push_events(id,channel,digest,received) VALUES(?,?,?,?)',(f'video{i:06d}',CHANNEL,str(i),i))
        def exhausted(*args,**kwargs):
            self.store.set('google_quota_retry_at',time.time()+3600)
            return {'items':[]}
        with patch.object(self.importer,'api',side_effect=exhausted):push.process(self.importer)
        with self.store.connect() as db:
            attempts=[r[0] for r in db.execute('SELECT attempts FROM youtube_push_events ORDER BY received')]
        self.assertEqual(attempts,[1,1,1,0,0])

    def test_pause_defers_processing_and_removed_channel_unsubscribes(self):
        self.deliver();self.store.set('youtube_sync_paused','true')
        with patch.object(self.importer,'api') as api:push.process(self.importer);api.assert_not_called()
        with self.store.connect() as db:db.execute('DELETE FROM channels')
        push.renew(self.store,self.requests.append)
        self.assertEqual(self.requests[-1]['hub.mode'],'unsubscribe')
        self.assertEqual(self.deliver().status_code,404);self.assertIsNone(self.event())
        r=self.client.get(self.path,base_url=ORIGIN,query_string={'hub.mode':'unsubscribe','hub.topic':push.topic(CHANNEL),'hub.challenge':'bye'})
        self.assertEqual(r.status_code,200)

    def test_new_notification_during_import_is_not_lost(self):
        self.deliver()
        def changed(*args,**kwargs):
            self.deliver(self.feed('Changed during import'));return {'items':[self.video()]}
        with patch.object(self.importer,'api',side_effect=changed):push.process(self.importer)
        self.assertEqual(self.event()['done'],0)

    def test_expired_lease_renews_and_failed_hub_is_backed_off(self):
        with self.store.connect() as db:db.execute('UPDATE youtube_push_subscriptions SET retry_at=0,expires=0')
        with patch('backend.youtube_push.send_subscription'):
            push.renew(self.store,self.requests.append)
        self.assertEqual(len(self.requests),2)
        push.renew(self.store,self.requests.append);self.assertEqual(len(self.requests),2)
        with self.store.connect() as db:db.execute('UPDATE youtube_push_subscriptions SET retry_at=0')
        def fail(fields):raise OSError('offline')
        push.renew(self.store,fail)
        self.assertIn('retry',self.store.get('youtube_push_status'))
        push.renew(self.store,self.requests.append);self.assertEqual(len(self.requests),2)

    def test_public_origin_change_rotates_callback_and_rejects_old_delivery(self):
        self.store.set('public_origin','https://new.example.com')
        push.renew(self.store,self.requests.append)
        self.assertNotEqual(urlsplit(self.requests[-1]['hub.callback']).path,self.path)
        self.assertEqual(self.client.post(self.path,base_url='https://new.example.com').status_code,404)

    def test_api_cannot_substitute_an_unsubscribed_channel(self):
        self.deliver();video=self.video();video['snippet']['channelId']='UC'+'b'*22
        with patch.object(self.importer,'api',return_value={'items':[video]}):push.process(self.importer)
        self.assertEqual(self.event()['done'],0)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM videos').fetchone()[0],0)

    def test_emby_refresh_only_after_runner_finishes_successfully(self):
        self.importer.save_videos([self.video()]);self.store.set('emby_auto_refresh','true')
        downloads.enqueue(self.store,{'id':VID,'title':'Video','state':'published'},'1080')
        sequence=[]
        def run(*args):sequence.append('finished')
        with patch('backend.emby.queue_refresh',side_effect=lambda *args:sequence.append('refresh')):
            downloads.tick(self.store,self.app.config['MEDIA_DIR'],threading.Event(),runner=run)
        self.assertEqual(sequence,['finished','refresh'])
        downloads.enqueue(self.store,{'id':VID,'title':'Video','state':'published'},'1080')
        with patch('backend.emby.queue_refresh') as refresh:
            downloads.tick(self.store,self.app.config['MEDIA_DIR'],threading.Event(),runner=lambda *a:(_ for _ in ()).throw(ValueError('failed')))
            refresh.assert_not_called()

if __name__=='__main__':unittest.main()
