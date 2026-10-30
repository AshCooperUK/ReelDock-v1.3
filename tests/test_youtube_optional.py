import hashlib
import hmac
import json
import time
import unittest
from datetime import datetime,timezone,timedelta
from unittest.mock import Mock,patch
import test_guide as fixture
CHANNEL,ORIGIN=fixture.CHANNEL,fixture.ORIGIN
from backend import youtube_fallback as fb, youtube_source as source, guide_weeks, youtube_push, channel_downloads, history

VID='newvideo001'
OTHER='newvideo002'
class OptionalYouTubeTests(unittest.TestCase):
    tearDown=fixture.GuideTests.tearDown
    google=fixture.GuideTests.google
    def setUp(self):
        fixture.GuideTests.setUp(self)
        self.store.set('youtube_use_google_api','false')
        self.store.set('next_sync',time.time()+21600)
        self.extracts=[]
        self.app.config['YOUTUBE_EXTRACT']=self.extract
        self.app.config['YOUTUBE_FEED']=self.feed
        self.sleep=patch('backend.youtube_fallback.time.sleep');self.sleep.start();self.addCleanup(self.sleep.stop)
        channel=dict(id=CHANNEL,name='Saved channel',playlist='UU'+CHANNEL[2:],avatar='',description='')
        with self.store.connect() as db:db.execute('INSERT OR REPLACE INTO channels VALUES(?,?,1)',(CHANNEL,json.dumps(channel)))
        channel_downloads.save(self.store,CHANNEL,{'videos':True,'range_days':7})
    def post(self,url,body):
        return self.client.post(url,base_url=ORIGIN,json=body,headers={'Origin':ORIGIN,'X-CSRF-Token':'csrf-fixture'})
    def extract(self,url,mode,start,end):
        self.extracts.append((url,mode,start,end))
        if mode=='channel':return dict(id=CHANNEL,channel_id=CHANNEL,channel='Direct channel',entries=[],thumbnails=[])
        if mode=='playlist':return {'entries':[dict(id=VID,timestamp=time.time())]}
        return dict(id=url.split('v=')[1],channel_id=CHANNEL,title='New upload',timestamp=time.time(),duration=601,description='Details',live_status='not_live')
    def feed(self,channel):
        return self.xml()
    def xml(self):
        return f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"><entry><yt:videoId>{VID}</yt:videoId><yt:channelId>{CHANNEL}</yt:channelId><title>New upload</title><published>{datetime.now(timezone.utc).isoformat()}</published></entry></feed>'''.encode()
    def test_off_makes_zero_google_calls_and_finishes_metadata_before_auto_download(self):
        google=Mock(side_effect=AssertionError('Google must not be contacted'));self.importer.request=google
        self.importer.run()
        with self.store.connect() as db:video=json.loads(db.execute('SELECT data FROM videos WHERE id=?',(VID,)).fetchone()[0])
        self.assertTrue(video['metadata_pending'])
        self.assertEqual(channel_downloads.schedule(self.store,self.app.config['MEDIA_DIR']),0)
        youtube_push.process(self.importer)
        with self.store.connect() as db:
            video=json.loads(db.execute('SELECT data FROM videos WHERE id=?',(VID,)).fetchone()[0])
            self.assertEqual(db.execute('SELECT done FROM youtube_push_events WHERE id=?',(VID,)).fetchone()[0],1)
        self.assertFalse(video['metadata_pending']);self.assertEqual(video['duration'],'10:01')
        self.assertEqual(channel_downloads.schedule(self.store,self.app.config['MEDIA_DIR']),1)
        google.assert_not_called()
    def test_known_channel_discovery_with_expired_cookies_and_no_api(self):
        (self.store.directory/'youtube-cookies.txt').write_text('# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1\tSID\tfixture\n')
        self.test_off_makes_zero_google_calls_and_finishes_metadata_before_auto_download()

    def test_real_extractor_retries_without_rejected_cookies(self):
        import subprocess
        self.app.config.pop('YOUTUBE_EXTRACT');self.app.testing=False
        (self.store.directory/'youtube-cookies.txt').write_text('# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t4102444800\tSID\tfixture\n')
        calls=[]
        def run(args, input, stdout, **kwargs):
            request=json.loads(input);calls.append(request)
            if 'cookie' in request:raise subprocess.CalledProcessError(1,args)
            stdout.write(json.dumps({'id':VID}).encode())
        with patch('backend.youtube_fallback.run_extractor',side_effect=run):
            self.assertEqual(fb.extract(self.importer,'https://www.youtube.com/watch?v='+VID),{'id':VID})
        self.assertEqual(len(calls),2);self.assertIn('cookie',calls[0]);self.assertNotIn('cookie',calls[1])

    def test_extractor_skips_expired_cookies_and_does_not_loop_on_failure(self):
        import subprocess
        self.app.config.pop('YOUTUBE_EXTRACT');self.app.testing=False
        (self.store.directory/'youtube-cookies.txt').write_text('# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1\tSID\tfixture\n')
        def run(args,input,**kwargs):
            self.assertNotIn('cookie',json.loads(input))
            raise subprocess.CalledProcessError(1,args)
        with patch('backend.youtube_fallback.run_extractor',side_effect=run) as runner:
            with self.assertRaisesRegex(ValueError,'without signing in'):
                fb.extract(self.importer,'https://www.youtube.com/watch?v='+VID)
        self.assertEqual(runner.call_count,1)

    def test_api_preferred_quota_falls_back_and_reset_returns_to_google(self):
        self.store.set('youtube_use_google_api','true')
        self.store.set('google_tokens',json.dumps(dict(access_token='fixture',received_at=time.time(),expires_in=3600)))
        google=Mock(return_value={'items':[]});self.importer.request=google
        self.importer.api('videos',id=VID);self.assertEqual(len(self.extracts),0)
        google.side_effect=ValueError('YouTube API quotaExceeded')
        result=self.importer.api('videos',id=VID);self.assertEqual(result['items'][0]['id'],VID)
        self.assertTrue(source.fallback(self.store));self.assertEqual(google.call_count,2)
        self.importer.api('videos',id=VID);self.assertEqual(google.call_count,2)
        self.store.set('google_quota_retry_at',0);google.side_effect=None
        self.importer.api('videos',id=VID);self.assertEqual(google.call_count,3)
    def test_setting_auth_csrf_preserves_credentials_and_channels(self):
        before=self.store.get('google_tokens')
        self.assertEqual(self.client.post('/api/youtube/mode',base_url=ORIGIN,json={'enabled':True},headers={'Origin':ORIGIN}).status_code,403)
        self.assertEqual(self.post('/api/youtube/mode',{'enabled':'false'}).status_code,400)
        self.assertEqual(self.post('/api/youtube/mode',{'enabled':False}).status_code,200)
        self.assertEqual(self.store.get('google_tokens'),before)
        self.assertEqual(self.client.get('/api/subscriptions',base_url=ORIGIN).json['total'],1)
        self.assertFalse(self.client.get('/api/subscriptions',base_url=ORIGIN).json['items'][0]['can_manage_youtube'])
        self.assertEqual(self.post('/api/youtube/authorise',{}).status_code,409)
        self.assertEqual(self.post('/api/channel-downloads/'+CHANNEL,{'action':'unsubscribe_youtube','confirm':True}).status_code,409)
    def test_setup_finishes_without_google_and_keeps_security(self):
        self.store.set('setup_complete','');self.store.set('google_tokens','')
        self.assertEqual(self.post('/api/setup/youtube-mode',{'enabled':False}).status_code,200)
        state=self.client.get('/api/setup/state',base_url=ORIGIN).json
        self.assertFalse(state['use_google_api']);self.assertFalse(state['youtube_connected'])
        self.assertEqual(self.post('/api/setup/finish',{}).status_code,200)
        self.assertEqual(self.store.get('setup_complete'),'1');self.assertEqual(self.calls,[])
    def test_signed_notification_immediately_adds_basic_entry_without_google(self):
        with self.store.connect() as db:db.execute('INSERT INTO youtube_push_subscriptions(channel,token,secret,origin,expires) VALUES(?,?,?,?,?)',(CHANNEL,'fixture','secret',ORIGIN,time.time()+600))
        body=self.xml();signature='sha256='+hmac.new(b'secret',body,'sha256').hexdigest()
        response=self.client.post('/hooks/youtube/fixture',base_url=ORIGIN,data=body,headers={'X-Hub-Signature':signature})
        self.assertEqual(response.status_code,204)
        with self.store.connect() as db:self.assertTrue(json.loads(db.execute('SELECT data FROM videos WHERE id=?',(VID,)).fetchone()[0])['metadata_pending'])
        self.assertEqual(self.calls,[])
        youtube_push.process(self.importer)
        self.client.post('/hooks/youtube/fixture',base_url=ORIGIN,data=body,headers={'X-Hub-Signature':signature})
        with self.store.connect() as db:self.assertFalse(json.loads(db.execute('SELECT data FROM videos WHERE id=?',(VID,)).fetchone()[0])['metadata_pending'])
    def test_channel_import_csv_validation_and_worker_resolution(self):
        csv='Channel Id,Channel Url,Channel Title\n'+CHANNEL+',https://www.youtube.com/channel/'+CHANNEL+',Fixture\n'
        self.assertEqual(self.post('/api/youtube/channels',{'channels':csv}).status_code,200)
        fb.add_batch(self.importer)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM youtube_channel_requests').fetchone()[0],'complete')
            self.assertEqual(db.execute('SELECT id FROM youtube_manual_channels').fetchone()[0],CHANNEL)
        for value in ('http://127.0.0.1/','https://youtube.com.evil.test/@hello','https://www.youtube.com/watch?v='+VID):
            self.assertEqual(self.post('/api/youtube/channels',{'channels':value}).status_code,400)
        self.assertEqual(self.calls,[])
    def test_channel_import_rebuilds_forecasts_once_per_batch(self):
        ids=[CHANNEL[:-1]+c for c in 'ABC']
        identity=lambda url:next(c for c in ids if c in url)
        self.app.config['YOUTUBE_EXTRACT']=lambda url,mode,start,end:dict(id=identity(url),channel_id=identity(url),channel='Imported',entries=[],thumbnails=[])
        csv='Channel Id,Channel Url,Channel Title\n'+''.join(f'{c},https://www.youtube.com/channel/{c},Fixture\n' for c in ids)
        self.assertEqual(self.post('/api/youtube/channels',{'channels':csv}).status_code,200)
        with patch('backend.forecasts.rebuild') as rebuild:
            fb.add_batch(self.importer);fb.add_batch(self.importer)
            rebuild.assert_not_called()
            self.assertEqual(self.store.get('forecasts_pending'),'1')
            fb.add_batch(self.importer)
            rebuild.assert_called_once()
            fb.add_batch(self.importer)
            rebuild.assert_called_once()
        self.assertEqual(self.store.get('forecasts_pending'),'0')
        with self.store.connect() as db:
            self.assertEqual(sorted(r[0] for r in db.execute('SELECT id FROM youtube_manual_channels')),sorted(ids))

    def test_guide_and_explicit_history_work_off_and_across_cursor_changes(self):
        guide_weeks.request_week(self.store,guide_weeks.current_week().isoformat())
        guide_weeks.batch(self.importer)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT state FROM guide_week_jobs').fetchone()[0],'complete')
        channel_downloads.save(self.store,CHANNEL,{'videos':False})
        channel_downloads.perform(self.store,self.app.config['MEDIA_DIR'],CHANNEL,'deep_scan')
        self.store.set('next_sync',time.time()+21600)
        history.batch(self.importer)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT state FROM history_jobs').fetchone()[0],'complete')
        self.importer.api('playlistItems',playlistId='UU'+CHANNEL[2:],pageToken='opaque-google-cursor')
        self.assertEqual(self.extracts[-1][2],1)
        self.store.set('youtube_use_google_api','true')
        self.importer.api('playlistItems',playlistId='UU'+CHANNEL[2:],pageToken='fb:10')
        self.assertEqual(self.extracts[-1][2],11);self.assertEqual(self.calls,[])
    def test_failed_video_does_not_block_next_and_pause_stops_both_sources(self):
        def extract(url,mode,start,end):
            if url.endswith(VID):raise ValueError('Unavailable')
            return self.extract(url,mode,start,end)
        self.app.config['YOUTUBE_EXTRACT']=extract
        result=self.importer.api('videos',id=VID+','+OTHER)
        self.assertEqual([v['id'] for v in result['items']],[OTHER])
        self.store.set('youtube_sync_paused','true')
        count=len(self.extracts)
        with self.assertRaises(ValueError):self.importer.api('videos',id=OTHER)
        self.assertEqual(len(self.extracts),count)
    def test_large_csv_and_invalid_feed_dates(self):
        csv='Channel Id,Channel Url,Channel Title\n'+(CHANNEL+',https://www.youtube.com/channel/'+CHANNEL+',Fixture\n')*500
        self.assertGreater(len(csv),16384)
        self.assertEqual(self.post('/api/youtube/channels',{'channels':csv}).status_code,200)
        self.assertEqual(self.post('/api/youtube/channels',{'channels':'x'*600001}).status_code,413)
        body=self.xml()
        invalid=body.replace(body.split(b'<published>')[1].split(b'</published>')[0],b'invalid')
        self.app.config['YOUTUBE_FEED']=lambda _:invalid
        self.assertEqual(fb.feed(self.importer,CHANNEL),[])
    def test_quota_transition_bounds_metadata_batch(self):
        self.store.set('youtube_use_google_api','true')
        self.importer.request=Mock(side_effect=ValueError('YouTube API quotaExceeded'))
        result=self.importer.api('videos',id=VID+','+OTHER,_fallback_limit=1)
        self.assertEqual(len(result['items']),1)
        self.assertEqual(len(self.extracts),1)
    def test_no_google_tokens_needed_for_push_lease_and_app_sections(self):
        self.store.set('google_tokens','')
        self.assertTrue(youtube_push.available(self.store))
        for url in ('/api/guide/sync','/api/activity','/api/subscriptions','/api/downloads','/api/m3u'):
            self.assertEqual(self.client.get(url,base_url=ORIGIN).status_code,200,url)
        self.assertEqual(self.calls,[])

if __name__=='__main__':unittest.main()
