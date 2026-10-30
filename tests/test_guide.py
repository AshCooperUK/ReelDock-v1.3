import json
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
from backend.app import create_app
from backend.catalogue import Importer
from backend.artwork import cache_batch, fetch_image
from backend.worker import tick
from werkzeug.security import generate_password_hash

ORIGIN = 'https://dev.example.com'
CHANNEL = 'UC' + 'a'*22

class GuideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []
        self.app = create_app({'TESTING': True, 'DATA_DIR': self.tmp.name,
            'GOOGLE_CLIENT_ID': 'fixture.apps.googleusercontent.com', 'GOOGLE_CLIENT_SECRET': 'fixture-secret',
            'GOOGLE_REQUEST': self.google})
        self.app.config['YOUTUBE_FEED'] = lambda channel: ('<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">'+''.join(
            f'<entry><yt:videoId>video{i:06}</yt:videoId><yt:channelId>{channel}</yt:channelId><title>Video {i}</title><published>2026-09-26T{i:02}:00:00Z</published></entry>' for i in range(20))+'</feed>').encode()
        self.store = self.app.extensions['store']
        with self.store.connect() as db:
            db.execute('INSERT INTO users VALUES (1,?,?)', ('owner', generate_password_hash('test-password-long')))
        self.store.set('public_origin', ORIGIN)
        self.store.set('setup_complete', '1')
        self.store.set('google_tokens', json.dumps({'refresh_token':'refresh'}))
        self.importer = Importer(self.app)
        self.client = self.app.test_client()
        with self.client.session_transaction(base_url=ORIGIN) as cookie:
            cookie['user_id'] = 1
            cookie['csrf'] = 'csrf-fixture'

    def scan(self):
        # Feed discovery and the independent detail worker are separate stages.
        self.importer.run()
        from backend.youtube_push import process
        process(self.importer)

    def tearDown(self):
        self.tmp.cleanup()

    def google(self, host, path, **kwargs):
        self.calls.append((host, path))
        if host == 'oauth2.googleapis.com':
            return {'access_token':'access', 'expires_in':3600}
        resource = urlsplit(path).path.rsplit('/', 1)[-1]
        if resource == 'subscriptions':
            return {'items':[{'snippet':{'resourceId':{'channelId':CHANNEL}}}]}
        if resource == 'channels':
            return {'items':[{'id':CHANNEL,'snippet':{'title':'My channel', 'thumbnails':{'medium':{'url':'https://yt3.ggpht.com/test'}}},
                             'contentDetails':{'relatedPlaylists':{'uploads':'uploads'}}}]}
        if resource == 'playlistItems':
            return {'items':[{'contentDetails':{'videoId':f'video{i:06}'}} for i in range(20)], 'nextPageToken':'older'}
        if resource == 'videos':
            return {'items':[{'id':f'video{i:06}', 'snippet':{'channelId':CHANNEL,'title':f'Video {i}',
                'publishedAt':f'2026-09-26T{i:02}:00:00Z', 'liveBroadcastContent':'none', 'description':'Full text. ' * 400,
                'thumbnails':{'medium':{'url':'https://i.ytimg.com/test.jpg'}}},
                'contentDetails':{'duration':'PT12M3S'}} for i in range(20)]}
        raise AssertionError(resource)

    def test_unavailable_uploads_playlist_does_not_block_other_channels(self):
        unavailable='UC'+'b'*22
        def request(host,path,**kwargs):
            resource=urlsplit(path).path.rsplit('/',1)[-1]
            if resource=='subscriptions':
                return {'items':[{'snippet':{'resourceId':{'channelId':cid}}} for cid in [unavailable,CHANNEL]]}
            if resource=='channels':
                return {'items':[{'id':cid,'snippet':{'title':cid},'contentDetails':{'relatedPlaylists':{'uploads':cid}}} for cid in [unavailable,CHANNEL]]}
            if resource=='playlistItems' and parse_qs(urlsplit(path).query)['playlistId']==[unavailable]:
                raise ValueError('YouTube API check failed (HTTP 404).')
            return self.google(host,path,**kwargs)
        self.importer.request=request
        feed=self.app.config['YOUTUBE_FEED']
        def partial(channel):
            if channel==unavailable:raise ValueError('Feed unavailable')
            return feed(channel)
        self.app.config['YOUTUBE_FEED']=partial
        self.scan()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM videos WHERE channel=?',(CHANNEL,)).fetchone()[0],20)
        response=self.client.get('/api/guide/channel/'+unavailable,base_url=ORIGIN).json
        self.assertEqual(response['items'],[])
        self.assertIn('Upload feed unavailable',response['notice'])
        self.assertEqual(self.store.get('recent_scan_started'),'0')

    def test_database_busy_in_guard_returns_retryable_json(self):
        import sqlite3
        with patch.object(self.store,'get',side_effect=sqlite3.OperationalError('database is locked')), self.assertLogs(self.app.logger,level='ERROR'):
            response=self.client.get('/api/guide/sync',base_url=ORIGIN)
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.headers['Retry-After'],'10')
        self.assertTrue(response.is_json)
        self.assertIn('database is busy',response.json['error'])

    def test_import_progress_and_artwork_queue(self):
        stages=[]
        original=self.importer.stage
        def stage(name,done=0,total=None):
            stages.append((name,done,total))
            original(name,done,total)
        self.importer.stage=stage
        self.scan()
        self.assertIn(('subscriptions',1,None),stages)
        self.assertIn(('videos',0,1),stages)
        self.assertEqual(stages[-1],('complete',1,1))
        state=self.client.get('/api/guide/sync',base_url=ORIGIN).json
        self.assertEqual(state['progress']['stage'],'complete')
        self.assertGreater(state['artwork_pending'],0)
        self.store.set('sync_error','A previous error')
        self.scan()
        self.assertEqual(self.store.get('sync_error'),'')

    def test_per_channel_forecasts_persist_independently_of_visibility(self):
        self.scan()
        headers={'Origin':ORIGIN,'X-CSRF-Token':'csrf-fixture'}
        self.assertTrue(self.client.get('/api/subscriptions',base_url=ORIGIN).json['items'][0]['forecasts'])
        forecast={'id':'expected-test','channel':CHANNEL,'date':'2026-09-26','time':'23:59','window_end':'2099-01-01T00:00:00+00:00'}
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO forecasts VALUES (?,?,?,?)',('expected-test',CHANNEL,'2026-09-26',json.dumps(forecast)))
        self.store.set('forecasts_built',time.time())
        guide='/api/guide?start=2026-09-24&end=2026-09-30'
        self.assertEqual(len(self.client.get(guide,base_url=ORIGIN).json['forecasts']),1)
        self.assertEqual(self.client.post('/api/subscriptions',base_url=ORIGIN,headers=headers,json={'ids':[CHANNEL],'forecasts':False}).status_code,200)
        result=self.client.get(guide,base_url=ORIGIN).json
        self.assertEqual(result['forecasts'],[])
        self.assertEqual(len(result['uploads']),20)
        self.client.post('/api/subscriptions',base_url=ORIGIN,headers=headers,json={'all':True,'shown':True})
        self.scan()
        item=self.client.get('/api/subscriptions',base_url=ORIGIN).json['items'][0]
        self.assertTrue(item['shown'])
        self.assertFalse(item['forecasts'])
        self.assertEqual(self.client.post('/api/subscriptions',base_url=ORIGIN,headers=headers,json={'ids':[CHANNEL],'forecasts':'false'}).status_code,400)
        self.assertEqual(self.client.post('/api/subscriptions',base_url=ORIGIN,headers={'Origin':ORIGIN},json={'ids':[CHANNEL],'forecasts':True}).status_code,403)
        self.client.post('/api/subscriptions',base_url=ORIGIN,headers=headers,json={'ids':[CHANNEL],'forecasts':True})
        self.assertTrue(self.client.get('/api/subscriptions',base_url=ORIGIN).json['items'][0]['forecasts'])

    def test_info_popup_includes_hidden_subscriptions(self):
        self.scan()
        self.client.post('/api/subscriptions', base_url=ORIGIN, json={'all':True,'shown':False}, headers={'Origin':ORIGIN,'X-CSRF-Token':'csrf-fixture'})
        endpoint = '/api/guide/channel/' + CHANNEL + '/info'
        response = self.client.get(endpoint, base_url=ORIGIN)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['channel']['id'], CHANNEL)
        self.assertEqual(response.json['csrf'], 'csrf-fixture')
        self.assertEqual(self.app.test_client().get(endpoint, base_url=ORIGIN).status_code, 401)
        self.assertEqual(self.client.get('/api/guide/channel/missing/info',base_url=ORIGIN).status_code, 404)

    def test_removed_channel_info_stays_available_without_restoring_subscription(self):
        self.scan()
        with self.store.connect() as db:
            db.execute('UPDATE channels SET active=0 WHERE id=?', (CHANNEL,))
        endpoint = '/api/guide/channel/' + CHANNEL + '/info'
        response = self.client.get(endpoint, base_url=ORIGIN)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['channel']['id'], CHANNEL)
        self.assertEqual(response.json['csrf'], 'csrf-fixture')
        self.assertEqual(self.client.get('/api/subscriptions', base_url=ORIGIN).json['items'], [])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT active FROM channels WHERE id=?', (CHANNEL,)).fetchone()[0], 0)
        self.assertEqual(self.app.test_client().get(endpoint, base_url=ORIGIN).status_code, 401)

    def test_channel_statistics_and_banner_cache(self):
        original = self.importer.request
        def google(host, path, **kwargs):
            result = original(host, path, **kwargs)
            if '/channels?' in path:
                item = result['items'][0]
                item['statistics'] = {'subscriberCount':'131000','viewCount':'20000000','videoCount':'265'}
                item['snippet'].update(customUrl='@example', country='GB', defaultLanguage='en', publishedAt='2011-12-17T00:00:00Z')
                item['brandingSettings'] = {'image':{'bannerExternalUrl':'https://yt3.googleusercontent.com/banner'}}
            if '/videos?' in path:
                for item in result['items']:
                    item['statistics'] = {'viewCount':'1234'}
            return result
        self.importer.request = google
        self.scan()
        with self.store.connect() as db:
            channel = json.loads(db.execute('SELECT data FROM channels').fetchone()[0])
            self.assertEqual(channel['subscribers'], '131000')
            self.assertEqual(channel['handle'], '@example')
            self.assertEqual(channel['country'], 'GB')
            key = channel['banner'].rsplit('/',1)[-1]
            self.assertIsNotNone(db.execute('SELECT id FROM artwork WHERE id=?',(key,)).fetchone())
        metrics=self.client.get('/api/guide/channel/'+CHANNEL+'/info',base_url=ORIGIN).json['channel']
        self.assertEqual((metrics['recent_average'],metrics['recent_sample']),(1234.0,20))

    def test_private_cookies_upload_replace_remove(self):
        import io
        endpoint = '/api/youtube/cookies'
        headers = {'Origin': ORIGIN, 'X-CSRF-Token': 'csrf-fixture'}
        cookie = b'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret-fixture\n'
        other = b'.example.com\tTRUE\t/\tTRUE\t0\tother\tprivate-other\n'
        self.assertEqual(self.app.test_client().get(endpoint, base_url=ORIGIN).status_code, 401)
        self.assertFalse(self.client.get(endpoint, base_url=ORIGIN).json['saved'])
        denied = self.client.post(endpoint, base_url=ORIGIN, headers={'Origin':ORIGIN}, data={'cookies':(io.BytesIO(cookie),'cookies.txt')})
        self.assertEqual(denied.status_code, 403)
        response = self.client.post(endpoint, base_url=ORIGIN, headers=headers, data={'cookies':(io.BytesIO(cookie+other),'cookies.txt')})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {'saved':True,'import_status':''})
        target = self.store.directory / 'youtube-cookies.txt'
        saved = target.read_bytes()
        self.assertIn(b'secret-fixture', saved)
        self.assertNotIn(b'private-other', saved)
        invalid = self.client.post(endpoint, base_url=ORIGIN, headers=headers, data={'cookies':(io.BytesIO(b'not cookies'),'bad.txt')})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(target.read_bytes(), saved)
        self.assertEqual(self.client.post(endpoint, base_url=ORIGIN, headers=headers, data={'cookies':(io.BytesIO(cookie.replace(b'secret-fixture', b'replacement')),'new.txt')}).status_code, 200)
        self.assertIn(b'replacement',target.read_bytes())
        self.assertEqual(self.client.post(endpoint, base_url=ORIGIN, headers=headers, data={'action':'remove'}).json, {'saved':False,'import_status':''})
        self.assertFalse(target.exists())

    def test_download_status_reports_saved_file_and_playback_routes_are_gone(self):
        from pathlib import Path
        self.scan()
        root = Path(self.tmp.name) / 'media'
        root.mkdir()
        self.app.config['MEDIA_DIR'] = str(root)
        endpoint = '/api/downloads/video000000'
        self.assertFalse(self.client.get(endpoint, base_url=ORIGIN).json['saved'])
        video = root / 'video000000.mp4'
        video.write_bytes(b'0123456789')
        self.assertTrue(self.client.get(endpoint, base_url=ORIGIN).json['saved'])
        self.assertEqual(self.app.test_client().get(endpoint, base_url=ORIGIN).status_code, 401)
        for removed in ('/api/playback/video000000', '/media/video/video000000'):
            self.assertEqual(self.client.get(removed, base_url=ORIGIN).status_code, 404)
            self.assertEqual(self.app.test_client().get(removed, base_url=ORIGIN).status_code, 404)
        video.unlink()
        self.assertFalse(self.client.get(endpoint, base_url=ORIGIN).json['saved'])
        self.assertEqual(self.client.get('/api/downloads/unknown0000', base_url=ORIGIN).status_code, 404)
        (root / 'video000000.mp4.part').write_bytes(b'incomplete')
        self.assertFalse(self.client.get(endpoint, base_url=ORIGIN).json['saved'])

    def test_download_status_rejects_link_outside_media(self):
        from pathlib import Path
        from unittest.mock import patch
        self.scan()
        root = Path(self.tmp.name) / 'media'
        root.mkdir()
        outside = Path(self.tmp.name) / 'private.mp4'
        outside.write_bytes(b'private')
        self.app.config['MEDIA_DIR'] = str(root)
        original = Path.resolve
        def resolved(path, *args, **kwargs):
            if path.name == 'video000000.mp4':
                return outside
            return original(path, *args, **kwargs)
        with patch.object(Path, 'resolve', resolved):
            self.assertFalse(self.client.get('/api/downloads/video000000', base_url=ORIGIN).json['saved'])
            self.assertEqual(self.client.get('/media/video/video000000', base_url=ORIGIN).status_code, 404)

    def test_full_channel_description_and_settings_navigation(self):
        original = self.importer.request
        description = 'Full description. ' * 100 + 'https://example.com'
        def google(host, path, **kwargs):
            result = original(host, path, **kwargs)
            if '/channels?' in path:
                result['items'][0]['snippet']['description'] = description
            return result
        self.importer.request = google
        self.scan()
        result = self.client.get('/api/guide?start=2026-09-24&end=2026-09-30', base_url=ORIGIN)
        self.assertEqual(result.json['channels'][0]['description'], description)
        self.assertEqual(self.client.get('/guide/', base_url=ORIGIN).location, '/app/?view=TV+Guide')

    def test_import_refresh_token_and_idempotent_saved_range(self):
        self.scan()
        self.scan()
        tokens = json.loads(self.store.get('google_tokens'))
        self.assertEqual(tokens['refresh_token'], 'refresh')
        self.assertEqual(sum(host == 'oauth2.googleapis.com' for host, _ in self.calls),1)
        result = self.client.get('/api/guide?start=2026-09-24&end=2026-09-30',base_url=ORIGIN)
        self.assertEqual(result.status_code,200)
        self.assertEqual(len(result.json['channels']),1)
        self.assertEqual(len(result.json['uploads']),20)
        self.assertEqual(result.json['uploads'][0]['duration'],'12:03')
        self.assertEqual(result.json['uploads'][0]['description'], 'Full text. ' * 400)
        self.assertNotIn('refresh',result.get_data(as_text=True))
        self.assertTrue(result.json['uploads'][0]['thumbnail'].startswith('/artwork/'))
        first = self.client.get('/api/guide/channel/'+CHANNEL,base_url=ORIGIN).json
        second = self.client.get('/api/guide/channel/'+CHANNEL+'?offset=15',base_url=ORIGIN).json
        self.assertEqual(len(first['items']),15)
        self.assertEqual(first['total'],20)
        self.assertTrue(first['more'])
        self.assertEqual(len(second['items']),5)
        self.assertEqual(second['total'],20)
        self.assertFalse(second['more'])
        self.assertEqual(first['items'][0]['title'],'Video 19')

    def test_manual_refresh_authorised_queued_and_throttled(self):
        anon = self.app.test_client()
        self.assertEqual(anon.get('/api/guide/sync',base_url=ORIGIN).status_code,401)
        self.assertEqual(self.client.post('/api/guide/sync',base_url=ORIGIN,headers={'Origin':ORIGIN}).status_code,403)
        headers={'Origin':ORIGIN,'X-CSRF-Token':'csrf-fixture'}
        self.assertEqual(self.client.post('/api/guide/sync',base_url=ORIGIN,headers=headers).status_code,200)
        self.assertEqual(self.store.get('sync_requested'),'1')
        self.assertEqual(self.client.post('/api/guide/sync',base_url=ORIGIN,headers=headers).status_code,429)

    def test_worker_runs_without_browser_and_waits_thirty_minutes(self):
        with patch('backend.worker.cache_batch'):
            tick(self.importer)
            count=len(self.calls)
            tick(self.importer)
            self.assertEqual(len(self.calls),count)
            self.assertGreater(float(self.store.get('next_sync')),time.time()+1700)
            self.store.set('sync_requested','1')
            tick(self.importer)
            self.assertGreater(len(self.calls),count)

    def test_upgrades_and_restarts_preserve_saved_scan_schedule(self):
        from backend.app import VERSION
        due = time.time()+1700
        self.store.set('last_sync', time.time()-100)
        self.store.set('next_sync', due)
        self.store.set('worker_upgrade_refresh', 'dev.old')
        with patch('backend.worker.cache_batch'), patch('backend.worker.history_batch'), patch.object(self.importer, 'run') as run:
            tick(self.importer)
            # A newly constructed worker also uses the same persisted schedule.
            tick(Importer(self.app))
            run.assert_not_called()
            self.assertFalse(self.calls)
            self.assertEqual(float(self.store.get('next_sync')), due)
            self.store.set('next_sync', time.time()-1)
            tick(self.importer)
            run.assert_called_once()
        self.assertEqual(self.store.get('worker_version'), VERSION)
        state = self.client.get('/api/guide/sync', base_url=ORIGIN).json
        self.assertEqual(state['worker_warning'], '')
        self.assertTrue(float(state['last_sync']))
        self.store.set('worker_version', 'dev.old')
        self.assertIn('versions differ', self.client.get('/api/guide/sync',base_url=ORIGIN).json['worker_warning'])

    def test_upgrade_does_not_bypass_failed_scan_retry_delay(self):
        self.store.set('next_sync', time.time()+1700)
        self.store.set('sync_error', 'Google unavailable')
        with patch('backend.worker.cache_batch'), patch('backend.worker.history_batch'), patch.object(self.importer, 'run') as run:
            tick(self.importer)
            run.assert_not_called()
            self.store.set('sync_requested', '1')
            tick(self.importer)
            run.assert_called_once()

    def test_channel_info_receives_latest_channel_metadata(self):
        self.scan()
        with self.store.connect() as db:
            row = json.loads(db.execute('SELECT data FROM channels WHERE id=?',(CHANNEL,)).fetchone()[0])
            row.update(subscribers='999', metadata_checked=time.time())
            db.execute('UPDATE channels SET data=? WHERE id=?',(json.dumps(row),CHANNEL))
        response = self.client.get('/api/guide/channel/'+CHANNEL+'/info',base_url=ORIGIN).json
        self.assertEqual(response['channel']['subscribers'], '999')
        self.assertTrue(response['channel']['metadata_checked'])

    def test_failed_import_preserves_saved_data(self):
        self.scan()
        self.importer.request=lambda *args, **kwargs: (_ for _ in ()).throw(ValueError('Google unavailable'))
        self.store.set('next_sync','0')
        self.store.set('youtube_subscriptions_next','0')
        with patch('backend.worker.cache_batch'):
            tick(self.importer)
        self.assertEqual(self.store.get('sync_running'),'0')
        self.assertEqual(self.store.get('youtube_subscriptions_error'),'Google unavailable')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM videos').fetchone()[0],20)

    def test_repeat_scan_reuses_recent_published_metadata(self):
        self.scan()
        self.calls.clear()
        self.scan()
        resources=[urlsplit(path).path.rsplit('/',1)[-1] for _,path in self.calls]
        self.assertNotIn('playlistItems',resources)
        self.assertNotIn('videos',resources)
        with self.store.connect() as db:
            db.execute("UPDATE videos SET data=json_set(data,'$.state','live'),checked=? WHERE id='video000000'",(time.time()-1801,))
        self.calls.clear()
        self.scan()
        detail_calls=[parse_qs(urlsplit(path).query)['id'][0] for _,path in self.calls if urlsplit(path).path.endswith('/videos')]
        self.assertEqual(detail_calls,['video000000'],'Live video is refreshed after its polling interval')
        with self.store.connect() as db:
            db.execute('UPDATE videos SET checked=?', (time.time()-86401,))
        self.calls.clear()
        self.scan()
        self.assertFalse(any(urlsplit(path).path.endswith('/videos') for _,path in self.calls))

    def test_interrupted_scan_resumes_completed_channels(self):
        now=time.time()
        self.store.set('recent_scan_started',now-100)
        with self.store.connect() as db:
            channel=dict(id=CHANNEL,name='Saved',playlist='uploads',last_checked=now-50)
            db.execute('INSERT INTO channels VALUES (?,?,1)',(CHANNEL,json.dumps(channel)))
        restarted=Importer(self.app)
        restarted.run()
        self.assertFalse(any(urlsplit(path).path.endswith('/playlistItems') for _,path in self.calls))
        self.assertEqual(self.store.get('recent_scan_started'),'0')
        self.assertGreater(float(self.store.get('next_sync')),now+1700)

    def test_old_artwork_is_queued_only_when_requested(self):
        from backend.artwork import queue_counts
        self.scan()
        with self.store.connect() as db:
            db.execute("UPDATE videos SET published='2000-01-01T00:00:00Z'")
            url=json.loads(db.execute('SELECT data FROM videos LIMIT 1').fetchone()[0])['thumbnail']
        pending,deferred=queue_counts(self.store)
        self.assertGreater(deferred,0)
        self.assertEqual(self.app.test_client().get(url,base_url=ORIGIN).status_code,401)
        self.assertEqual(queue_counts(self.store),(pending,deferred))
        self.assertEqual(self.client.get(url,base_url=ORIGIN).status_code,204)
        self.assertEqual(queue_counts(self.store),(pending+1,deferred-1))
        cache_batch(self.store,fetch=lambda _:b'\xff\xd8\xfffixture')
        response=self.client.get(url,base_url=ORIGIN)
        self.assertEqual(response.status_code,200)
        response.close()
        self.client.get('/artwork/'+'f'*64,base_url=ORIGIN)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM artwork_requests WHERE id=?',('f'*64,)).fetchone()[0],0)

    def test_artwork_cached_and_only_served_authenticated(self):
        self.scan()
        payload=b'\xff\xd8\xfffixture'
        from backend.artwork import request_artwork
        with self.store.connect() as db:
            keys=[r[0] for r in db.execute('SELECT id FROM artwork')]
        for key in keys:request_artwork(self.store,key)
        cache_batch(self.store,fetch=lambda url:payload)
        result=self.client.get('/api/guide?start=2026-09-24&end=2026-09-30',base_url=ORIGIN).json
        url=result['uploads'][0]['thumbnail']
        self.assertTrue(url.startswith('/artwork/'))
        response=self.client.get(url,base_url=ORIGIN)
        self.assertEqual(response.data,payload)
        self.assertEqual(response.headers['Cache-Control'],'private, max-age=86400')
        response.close()
        self.assertEqual(self.app.test_client().get(url,base_url=ORIGIN).status_code,401)
        for url in ('http://i.ytimg.com/x','https://evil.test/x','https://ytimg.com.evil.test/x','https://i.ytimg.com:8443/x'):
            with self.assertRaises(ValueError):
                fetch_image(url)

    def test_subscription_removal_cleans_catalogue_and_artwork(self):
        self.scan()
        cache_batch(self.store, fetch=lambda url:b'\xff\xd8\xfffixture')
        original=self.importer.request
        self.importer.request=lambda host,path,**kwargs: {'items':[]} if '/subscriptions?' in path else original(host,path,**kwargs)
        self.store.set('youtube_subscriptions_next',0)
        self.scan()
        with self.store.connect() as db:
            for table in ('channels','videos','artwork'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    def test_reconnect_preserves_account_and_catalogue(self):
        self.scan()
        result=self.client.post('/google/reconnect',base_url=ORIGIN,headers={'Origin':ORIGIN},data={'csrf':'csrf-fixture'})
        self.assertEqual(result.location,'/setup')
        self.assertEqual(self.store.get('setup_complete'),'1')
        self.assertTrue(self.store.get('google_tokens'))
        self.assertEqual(self.store.get('reconnect_pending'),'1')
        self.assertIsNotNone(self.store.user())
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM videos').fetchone()[0],20)

    def test_daily_counter_does_not_stop_provider_requests(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        self.store.set('quota_day', datetime.now(ZoneInfo('America/Los_Angeles')).date().isoformat())
        self.store.set('quota_used',8500)
        self.importer.api('subscriptions',part='snippet',mine='true')
        self.assertTrue(self.calls)
        self.assertEqual(self.store.get('quota_used'),'8501')

    def test_google_quota_error_delays_retries_without_local_daily_ceiling(self):
        from unittest.mock import patch
        self.store.set('google_tokens',json.dumps({'access_token':'fixture','received_at':time.time(),'expires_in':3600}))
        with patch.object(self.importer,'request',side_effect=ValueError('YouTube API quotaExceeded')) as request:
            with self.assertRaisesRegex(ValueError,'quotaExceeded'):self.importer.api('subscriptions')
            with self.assertRaisesRegex(ValueError,'quotaExceeded'):self.importer.api('subscriptions')
            self.assertEqual(request.call_count,1)
        self.store.set('google_quota_retry_at','0')
        with patch.object(self.importer,'request',return_value={}) as request:
            self.importer.api('subscriptions');request.assert_called_once()

    def test_upgrade_clears_only_local_allowance_pause(self):
        from backend.quota import clear_legacy_pause
        self.store.set('sync_error','Daily import allowance reached')
        self.store.set('next_sync','9999999999')
        clear_legacy_pause(self.store)
        self.assertEqual(self.store.get('sync_error'),'')
        self.assertEqual(self.store.get('next_sync'),'0')
        self.store.set('sync_error','YouTube API quotaExceeded')
        clear_legacy_pause(self.store)
        self.assertEqual(self.store.get('sync_error'),'YouTube API quotaExceeded')

    def preferences(self, payload):
        return self.client.post('/api/subscriptions',base_url=ORIGIN,json=payload,
                                headers={'Origin':ORIGIN,'X-CSRF-Token':'csrf-fixture'})

    def test_visibility_survives_sync_and_filters_guide(self):
        self.scan()
        page=self.client.get('/api/subscriptions',base_url=ORIGIN).json
        self.assertEqual(page['total'],1)
        self.assertEqual(page['shown'],1)
        self.assertNotEqual(page['items'][0]['checked_label'],'Not checked yet')
        self.assertEqual(self.preferences({'ids':[CHANNEL],'shown':False}).status_code,200)
        self.scan()
        guide=self.client.get('/api/guide?start=2026-09-24&end=2026-09-30',base_url=ORIGIN).json
        self.assertEqual(guide['channels'],[])
        self.assertEqual(guide['uploads'],[])
        self.assertEqual(self.client.get('/api/subscriptions',base_url=ORIGIN).json['shown'],0)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM videos').fetchone()[0],20)
        self.preferences({'all':True,'shown':True})
        self.assertEqual(self.client.get('/api/subscriptions',base_url=ORIGIN).json['shown'],1)

    def test_visibility_validation_and_authentication(self):
        self.scan()
        self.assertEqual(self.app.test_client().get('/api/subscriptions',base_url=ORIGIN).status_code,401)
        self.assertEqual(self.client.post('/api/subscriptions',base_url=ORIGIN,json={'all':True,'shown':False},headers={'Origin':ORIGIN}).status_code,403)
        for payload in ({'ids':['missing'],'shown':False},{'ids':[],'shown':False},{'all':True,'shown':'false'}):
            self.assertEqual(self.preferences(payload).status_code,400)
        self.assertEqual(self.client.get('/api/subscriptions',base_url=ORIGIN).json['shown'],1)

    def test_subscription_search_paging_and_new_channel_defaults(self):
        self.scan()
        self.preferences({'all':True,'shown':False})
        with self.store.connect() as db:
            for i in range(55):
                item={'id':str(i),'name':f'Extra {i:02}','coverage':'Recent uploads pending'}
                db.execute('INSERT INTO channels VALUES (?,?,1)',(str(i),json.dumps(item)))
        first=self.client.get('/api/subscriptions?q=Extra',base_url=ORIGIN).json
        second=self.client.get('/api/subscriptions?q=Extra&offset=45',base_url=ORIGIN).json
        self.assertEqual(len(first['items']),15)
        self.assertTrue(first['more'])
        self.assertEqual(len(second['items']),10)
        self.assertFalse(second['more'])
        self.assertEqual(first['shown'],55)
        self.assertEqual(self.client.get('/api/subscriptions?q=%25',base_url=ORIGIN).json['items'],[])
        self.assertEqual(self.client.get('/subscriptions',base_url=ORIGIN).location,'/app/?view=YouTube')

    def test_live_page_has_no_sample_data_script(self):
        result=self.client.get('/guide/',base_url=ORIGIN)
        self.assertEqual(result.status_code,302)
        self.assertEqual(result.location,'/app/?view=TV+Guide')
        self.assertNotIn(b'src="sample-data.js"',result.data)
        self.assertEqual(self.client.get('/api/guide?start=bad&end=bad',base_url=ORIGIN).status_code,400)

    def test_saved_guide_access_while_reconnect_pending(self):
        self.scan()
        self.store.set('setup_complete', '')
        self.store.set('google_tokens', '')
        self.assertIn(b'"saved_guide": true', self.client.get('/setup', base_url=ORIGIN).data)
        page=self.client.get('/guide/',base_url=ORIGIN)
        self.assertEqual(page.location,'/app/?view=TV+Guide')
        self.assertEqual(self.client.get('/api/dashboard',base_url=ORIGIN).json['state'],'ready')
        self.assertEqual(self.client.get('/api/guide/sync',base_url=ORIGIN).status_code,200)
        response=self.client.post('/api/guide/sync',base_url=ORIGIN,headers={'Origin':ORIGIN,'X-CSRF-Token':'csrf-fixture'})
        self.assertEqual(response.status_code,409)
        with self.client.session_transaction(base_url=ORIGIN) as cookie:
            cookie.clear()
        self.assertEqual(self.client.get('/api/guide/sync',base_url=ORIGIN).status_code,401)

    def test_quota_after_oauth_retains_new_connection(self):
        self.scan()
        self.store.set('setup_complete','')
        self.store.set('google_tokens','')
        with self.client.session_transaction(base_url=ORIGIN) as cookie:
            cookie['oauth']={'state':'test-state','verifier':'test-verifier','expires':time.time()+600}
        def provider(host,path,**kwargs):
            if host=='oauth2.googleapis.com':
                return {'access_token':'new-access','refresh_token':'new-refresh','expires_in':3600}
            raise ValueError('YouTube API check failed (HTTP 403, quotaExceeded).')
        self.app.config['GOOGLE_REQUEST']=provider
        response=self.client.get('/oauth/google/callback?state=test-state&code=test',base_url=ORIGIN)
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.store.get('setup_complete'),'1')
        self.assertEqual(json.loads(self.store.get('google_tokens'))['refresh_token'],'new-refresh')
        state=self.client.get('/api/guide/sync',base_url=ORIGIN).json
        self.assertTrue(state['quota_paused'])
        self.assertTrue(state['youtube_connected'])

    def test_successful_reconnect_migrates_unattributed_allowance_once(self):
        self.store.set('quota_used','8500')
        self.store.set('sync_error','Daily import allowance reached')
        self.store.set('sync_message','Old quota warning')
        # The worker can touch the legacy counters before the user reconnects.
        from backend.quota import bind
        with self.store.connect() as db:bind(db,self.app.config['GOOGLE_CLIENT_ID'])
        def provider(host,path,**kwargs):
            return {'access_token':'new','refresh_token':'refresh','expires_in':3600} if host=='oauth2.googleapis.com' else {'items':[]}
        self.app.config['GOOGLE_REQUEST']=provider
        def reconnect():
            with self.client.session_transaction(base_url=ORIGIN) as cookie:
                cookie['oauth']={'state':'test','verifier':'test','expires':time.time()+600}
            return self.client.get('/oauth/google/callback?state=test&code=test',base_url=ORIGIN)
        self.assertEqual(reconnect().status_code,302)
        self.assertEqual(self.store.get('quota_used'),'')
        self.assertEqual(self.store.get('sync_error'),'')
        self.assertEqual(self.store.get('next_sync'),'0')
        self.store.set('quota_used','100')
        self.assertEqual(reconnect().status_code,302)
        self.assertEqual(self.store.get('quota_used'),'100')

    def test_status_distinguishes_local_allowance_from_google_quota(self):
        self.store.set('sync_error','Daily import allowance reached')
        state=self.client.get('/api/guide/sync',base_url=ORIGIN).json
        self.assertFalse(state['local_allowance_reached'])
        self.store.set('sync_error','YouTube API quotaExceeded')
        state=self.client.get('/api/guide/sync',base_url=ORIGIN).json
        self.assertTrue(state['quota_paused'])
        self.assertFalse(state['local_allowance_reached'])
