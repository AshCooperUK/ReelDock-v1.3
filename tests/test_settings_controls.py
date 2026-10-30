import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, Mock
from backend.app import create_app
from backend import channel_downloads, download_options, downloads, media_library, history, emby, worker
from backend.catalogue import Importer
from backend.sync_controls import SyncPaused

ORIGIN='https://dev.example.com'

class SettingsControlsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.media=Path(self.tmp.name)/'media';self.media.mkdir()
        self.app=create_app({'TESTING':True,'DATA_DIR':self.tmp.name,'MEDIA_DIR':str(self.media)})
        self.store=self.app.extensions['store']
        self.store.set('public_origin',ORIGIN);self.store.set('setup_complete','1')
        with self.store.connect() as db:
            db.execute("INSERT INTO users VALUES(1,'owner','unused')")
            for i in range(60):
                c={'id':f'channel{i:02d}','name':f'Channel {i:02d}'}
                db.execute('INSERT INTO channels VALUES(?,?,1)',(c['id'],json.dumps(c)))
        self.client=self.app.test_client()
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session['user_id']=1;session['csrf']='token'
    def tearDown(self):self.tmp.cleanup()
    def post(self,path,body,csrf='token'):
        return self.client.post(path,base_url=ORIGIN,json=body,headers={'Origin':ORIGIN,'X-CSRF-Token':csrf})
    def video(self,n=1):
        v=dict(id=f'video{n:06d}',channel='channel00',title='Caf\u00e9 & a sunny day',date='2026-09-27',state='published',kind='videos')
        with self.store.connect() as db:db.execute('INSERT INTO videos VALUES(?,?,?,?,?,0)',(v['id'],v['channel'],v['date'],v['date'],json.dumps(v)))
        return v
    def test_subscriptions_load_in_fifteen_channel_pages(self):
        ids=[]
        for offset in (0,15,30,45):
            data=self.client.get('/api/subscriptions?offset='+str(offset),base_url=ORIGIN).json
            self.assertEqual(len(data['items']),15)
            self.assertEqual(data['more'],offset<45)
            ids.extend(item['id'] for item in data['items'])
        self.assertEqual(len(set(ids)),60)

    def test_retention_failure_does_not_block_download_scheduling(self):
        with patch('backend.channel_downloads.expire_downloads',side_effect=OSError()),patch('backend.channel_downloads.schedule') as schedule:
            channel_downloads.tick(self.store,self.media)
            schedule.assert_called_once()
        self.assertIn('permissions',self.store.get('retention_status'))

    def test_retention_removes_expired_files_and_sidecars_but_keeps_boundary(self):
        old=self.video(1);recent=self.video(2)
        for video in (old,recent):
            downloads.enqueue(self.store,video,'480',automatic=True);downloads.update(self.store,video['id'],'complete')
            record=media_library.reserve(self.store,self.media,video['id'])
            path=media_library.contained(self.media,record['path']);path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(b'video');path.with_suffix('.nfo').write_text('metadata');path.with_name(path.stem+'-thumb.jpg').write_bytes(b'image')
        channel_downloads.save(self.store,'channel00',{'range_days':30})
        with self.store.connect() as db:
            db.execute("UPDATE media_files SET day='2026-08-27' WHERE id=?",(old['id'],))
            db.execute("UPDATE media_files SET day='2026-08-29' WHERE id=?",(recent['id'],))
            db.execute('DELETE FROM videos WHERE id=?',(old['id'],))
        with patch('backend.channel_downloads.range_cutoff',return_value='2026-08-29'):
            self.assertEqual(channel_downloads.expire_downloads(self.store,self.media),1)
            self.assertEqual(channel_downloads.expire_downloads(self.store,self.media),0)
        self.assertIsNone(media_library.local_file(self.store,self.media,old['id']))
        self.assertIsNotNone(media_library.local_file(self.store,self.media,recent['id']))
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM media_files WHERE id=?',(old['id'],)).fetchone()
            self.assertEqual(row['bytes'],0)
            self.assertEqual(db.execute('SELECT state FROM downloads WHERE id=?',(old['id'],)).fetchone()[0],'expired')
        path=media_library.contained(self.media,row['path'])
        self.assertFalse(path.with_suffix('.nfo').exists());self.assertFalse(path.with_name(path.stem+'-thumb.jpg').exists())

    def test_retention_protection_all_uploads_and_active_downloads(self):
        video=self.video();downloads.enqueue(self.store,video,'480',automatic=True);downloads.update(self.store,video['id'],'complete')
        path=self.media/(video['id']+'.mp4');path.write_bytes(b'keep')
        with patch('backend.channel_downloads.range_cutoff',return_value='2026-09-28'):
            for settings in ({'range_days':1,'keep_files':True},{'range_days':0,'keep_files':False}):
                channel_downloads.save(self.store,'channel00',settings)
                self.assertEqual(channel_downloads.expire_downloads(self.store,self.media),0);self.assertTrue(path.exists())
            channel_downloads.save(self.store,'channel00',{'range_days':1})
            downloads.update(self.store,video['id'],'downloading')
            self.assertEqual(channel_downloads.expire_downloads(self.store,self.media),0);self.assertTrue(path.exists())
            downloads.update(self.store,video['id'],'complete')
            self.assertEqual(channel_downloads.expire_downloads(self.store,self.media),1)

    def test_manual_downloads_and_sidecars_are_protected_from_retention(self):
        video=self.video();downloads.enqueue(self.store,video,'480')
        downloads.update(self.store,video['id'],'complete')
        record=media_library.reserve(self.store,self.media,video['id'])
        path=media_library.contained(self.media,record['path']);path.parent.mkdir(parents=True,exist_ok=True)
        files=(path,path.with_suffix('.nfo'),path.with_name(path.stem+'-thumb.jpg'))
        for file in files:file.write_bytes(b'keep')
        channel_downloads.save(self.store,'channel00',{'range_days':1,'keep_files':False})
        with patch('backend.channel_downloads.range_cutoff',return_value='2026-09-28'):
            self.assertEqual(channel_downloads.expire_downloads(self.store,self.media),0)
        self.assertTrue(all(file.exists() for file in files))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT state FROM downloads').fetchone()[0],'complete')

    def test_watched_checks_include_video_and_channel(self):
        video=self.video();downloads.enqueue(self.store,video,'480')
        with patch('backend.emby.settings',return_value={'attempts':[{'id':video['id'],'state':'failed','message':'Example','updated':123}]}):
            response=self.client.get('/api/emby',base_url=ORIGIN)
        attempt=response.get_json()['attempts'][0]
        self.assertEqual(attempt['channel_name'],'Channel 00')
        self.assertEqual(attempt['video']['id'],video['id'])
        self.assertEqual(attempt['updated'],123)
        self.assertEqual(attempt['state'],'failed')

    def test_google_delete_accepts_empty_204(self):
        from backend.network import google_request
        with patch('backend.network.http.client.HTTPSConnection') as connection:
            response=connection.return_value.getresponse.return_value
            response.status=204;response.read.return_value=b''
            self.assertEqual(google_request('www.googleapis.com','/youtube/v3/subscriptions?id=fixture',token='fixture',method='DELETE'),{})
            self.assertEqual(connection.return_value.request.call_args.args[0],'DELETE')

    def test_youtube_unsubscribe_permission_confirmation_and_failure(self):
        import time
        path='/api/channel-downloads/channel00'
        body={'action':'unsubscribe_youtube','confirm':True}
        self.assertEqual(self.post(path,{'action':'unsubscribe_youtube'}).status_code,400)
        self.assertTrue(self.post(path,body).get_json()['authorise'])
        self.store.set('google_tokens',json.dumps({'access_token':'fixture','expires_in':3600,'received_at':time.time(),'scope':'https://www.googleapis.com/auth/youtube'}))
        google=Mock(side_effect=[{'items':[{'id':'sub-123'}]},ValueError('quotaExceeded')])
        self.app.config['GOOGLE_REQUEST']=google
        self.assertEqual(self.post(path,body).status_code,400)
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT active FROM channels WHERE id='channel00'").fetchone()[0],1)
        self.store.set('google_quota_retry_at','0')
        google.side_effect=[{'items':[{'id':'sub-123'}]},{}]
        self.assertEqual(self.post(path,body).status_code,200)
        self.assertEqual(google.call_args.kwargs['method'],'DELETE')
        self.assertIn('id=sub-123',google.call_args.args[1])
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT active FROM channels WHERE id='channel00'").fetchone()[0],0)

    def test_subscription_management_authorisation_requires_csrf(self):
        from urllib.parse import urlsplit,parse_qs
        self.store.set('google_client_id','fixture');self.store.set('google_client_secret','fixture')
        self.assertEqual(self.post('/api/youtube/authorise',{},'bad').status_code,403)
        response=self.post('/api/youtube/authorise',{})
        self.assertEqual(response.status_code,200)
        self.assertEqual(parse_qs(urlsplit(response.get_json()['url']).query)['scope'],['https://www.googleapis.com/auth/youtube'])

    def test_password_change_revokes_sessions_and_logout_has_csrf(self):
        from werkzeug.security import generate_password_hash,check_password_hash
        with self.store.connect() as db:db.execute('UPDATE users SET password_hash=?',(generate_password_hash('old password 123'),))
        other=self.app.test_client()
        with other.session_transaction(base_url=ORIGIN) as session:session['user_id']=1
        body=dict(current='old password 123',password='new password 456',confirm='new password 456')
        self.assertEqual(self.post('/api/account/password',body,'bad').status_code,403)
        self.assertEqual(self.post('/api/account/password',{**body,'current':'wrong'}).status_code,400)
        self.assertEqual(self.post('/api/account/password',{**body,'confirm':'different'}).status_code,400)
        self.assertEqual(self.post('/api/account/password',body).status_code,200)
        self.assertTrue(check_password_hash(self.store.user()['password_hash'],'new password 456'))
        self.assertEqual(other.get('/api/local-library',base_url=ORIGIN).status_code,401)
        self.assertEqual(self.client.get('/api/local-library',base_url=ORIGIN).status_code,401)
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session['user_id']=1;session['auth_revision']=self.store.get('auth_revision');session['csrf']='token'
        self.assertEqual(self.client.post('/logout',base_url=ORIGIN,data={'csrf':'bad'},headers={'Origin':ORIGIN}).status_code,403)
        self.assertEqual(self.client.post('/logout',base_url=ORIGIN,data={'csrf':'token'},headers={'Origin':ORIGIN}).status_code,302)
        self.assertEqual(self.client.get('/api/local-library',base_url=ORIGIN).status_code,401)

    def test_command_line_password_reset_signs_out_existing_sessions(self):
        from werkzeug.security import check_password_hash
        from backend.manage import reset_password
        self.assertEqual(self.client.get('/api/local-library',base_url=ORIGIN).status_code,200)
        with self.store.connect() as db:db.execute("INSERT INTO attempts VALUES('login',5,9999999999)")
        with self.assertRaises(ValueError):reset_password(self.store,'short')
        reset_password(self.store,'recovered password 789')
        self.assertTrue(check_password_hash(self.store.user()['password_hash'],'recovered password 789'))
        # No restart needed: the next request from the old session is rejected.
        self.assertEqual(self.client.get('/api/local-library',base_url=ORIGIN).status_code,401)
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM attempts").fetchone()[0],0)

    def test_library_pages_physical_files_and_preserves_playback_without_catalogue(self):
        for n in range(135):
            video=self.video(n)
            downloads.enqueue(self.store,video,'480')
            if n<32:(self.media/(video['id']+'.mp4')).write_bytes(b'fixture')
        all_items=[];cursor='';counts=[]
        for _ in range(10):
            response=self.client.get('/api/local-library',query_string={'cursor':cursor},base_url=ORIGIN)
            self.assertEqual(response.status_code,200)
            page=response.get_json();counts.append(len(page['items']));all_items+=page['items']
            if not page['more']:break
            self.assertNotEqual(cursor,page['cursor']);cursor=page['cursor']
        self.assertEqual(len(all_items),32);self.assertEqual(len({v['id'] for v in all_items}),32)
        self.assertLessEqual(max(counts),15)
        self.assertEqual(self.client.get('/api/local-library?cursor=invalid',base_url=ORIGIN).status_code,400)
        with self.store.connect() as db:db.execute('DELETE FROM videos')
        response=self.client.get('/api/downloads/video000000',base_url=ORIGIN)
        self.assertEqual(response.status_code,200);self.assertTrue(response.get_json()['saved'])

    def test_library_page_skips_missing_files_in_one_request(self):
        from backend import local_library
        for n in range(260):
            video=self.video(n)
            downloads.enqueue(self.store,video,'480')
            # The newest 240 downloads have no file (failed or deleted).
            if n>=240:(self.media/(video['id']+'.mp4')).write_bytes(b'fixture')
        with self.store.connect() as db:db.execute('UPDATE downloads SET created=CAST(substr(id,6) AS INTEGER)*-1')
        connections=[]
        real=self.store.connect
        def counting():
            connections.append(1);return real()
        with patch.object(self.store,'connect',counting):
            page=local_library.page(self.store,self.media)
        self.assertEqual(len(page['items']),15)
        self.assertTrue(page['more'])
        self.assertEqual(len(connections),1)
        rest=local_library.page(self.store,self.media,page['cursor'])
        self.assertEqual(len(rest['items']),5);self.assertFalse(rest['more'])

    def test_favourite_pins_before_pagination_and_survives_repeated_save(self):
        path='/api/channel-favourite/channel59'
        self.assertEqual(self.post(path,{'favourite':True},'bad').status_code,403)
        for _ in range(2):self.assertEqual(self.post(path,{'favourite':True}).status_code,200)
        data=self.client.get('/api/subscriptions',base_url=ORIGIN).get_json()
        self.assertEqual(data['items'][0]['id'],'channel59');self.assertTrue(data['items'][0]['favourite'])
        self.assertTrue(data['more'])
        self.post(path,{'favourite':False})
        data=self.client.get('/api/subscriptions',base_url=ORIGIN).get_json()
        self.assertEqual(data['items'][0]['id'],'channel00')
    def test_sync_pause_blocks_imports_and_watched_but_not_downloads(self):
        self.assertEqual(self.post('/api/youtube-sync',{'paused':True}).status_code,200)
        importer=Importer(self.app)
        with patch.object(importer,'request') as request:
            with self.assertRaises(SyncPaused):importer.api('channels',id='channel00')
            with self.assertRaises(SyncPaused):history.request(importer,'channels',id='channel00')
            request.assert_not_called()
        with patch.object(importer,'run') as run,patch('backend.worker.history_batch') as batch:
            worker.tick(importer,download_artwork=False);run.assert_not_called();batch.assert_not_called()
        with patch('backend.emby.api') as request:
            self.assertEqual(emby.match_watched(self.store,threading.Event()),0);request.assert_not_called()
        video=self.video();downloads.enqueue(self.store,video,'480');runner=Mock(return_value='')
        downloads.tick(self.store,self.media,threading.Event(),runner=runner);runner.assert_called_once()
        self.assertFalse(self.store.get('quota_used'))
        self.post('/api/youtube-sync',{'paused':False})
        for key in ('next_sync','next_history','emby_next_check'):self.assertEqual(self.store.get(key),'0')
    def test_sleep_interval_and_zero_disable(self):
        video=self.video();downloads.enqueue(self.store,video,'480')
        self.store.set('download_finished_at',1000);download_options.save(self.store,{'sleep_interval':60})
        runner=Mock(return_value='')
        with patch('backend.downloads.time.time',return_value=1059):downloads.tick(self.store,self.media,threading.Event(),runner=runner)
        runner.assert_not_called()
        download_options.save(self.store,{'sleep_interval':0})
        with patch('backend.downloads.time.time',return_value=1059):downloads.tick(self.store,self.media,threading.Event(),runner=runner)
        runner.assert_called_once();self.assertEqual(self.store.get('download_finished_at'),'1059')
        for invalid in (-1,86401,True,1.5,'60'):
            with self.assertRaises(ValueError):download_options.save(self.store,{'sleep_interval':invalid})
    def test_restricted_new_names_and_stable_existing_names(self):
        first=self.video();old=media_library.reserve(self.store,self.media,first['id'])
        download_options.save(self.store,{'restrict_filenames':True})
        second=self.video(2);new=media_library.reserve(self.store,self.media,second['id'])
        name=Path(new['path']).name
        self.assertTrue(name.isascii());self.assertNotIn(' ',name);self.assertNotIn('&',name)
        self.assertEqual(media_library.reserve(self.store,self.media,first['id'])['path'],old['path'])
        self.assertNotEqual(new['path'],old['path'])
    def test_calendar_ranges_include_today(self):
        with patch('backend.channel_downloads.datetime') as clock:
            clock.now.return_value=datetime(2026,9,27,0,5)
            for days,expected in [(1,'2026-09-27'),(2,'2026-09-26'),(7,'2026-09-21'),(14,'2026-09-14')]:
                self.assertEqual(channel_downloads.range_cutoff(days),expected)
                channel_downloads.save(self.store,'channel00',{'range_days':days,'videos':True})
                rule=channel_downloads.preferences(self.store,'channel00')
                self.assertTrue(channel_downloads.eligible({'state':'published','kind':'videos','date':expected},rule))
                self.assertFalse(channel_downloads.eligible({'state':'published','kind':'videos','date':'2026-09-01'},rule))
    def test_overview_counts_and_protected_access(self):
        self.video();self.post('/api/channel-favourite/channel59',{'favourite':True})
        response=self.client.get('/api/overview',base_url=ORIGIN)
        self.assertEqual(response.status_code,200)
        data=response.get_json();self.assertEqual(data['channels'],60);self.assertEqual(data['videos'],1);self.assertEqual(data['favourites'],1)
        self.assertGreater(data['media_disk']['total'],0)
        self.assertNotIn('google_tokens',data)
        anonymous=self.app.test_client();self.assertNotEqual(anonymous.get('/api/overview',base_url=ORIGIN).status_code,200)

    def test_subscription_target_uses_identity_beyond_first_page(self):
        data=self.client.get('/api/subscriptions?channel=channel59&q=wrong-name',base_url=ORIGIN).get_json()
        self.assertEqual([c['id'] for c in data['items']],['channel59'])
        self.assertFalse(data['more'])
        data=self.client.get('/api/subscriptions?channel=unknown',base_url=ORIGIN).get_json()
        self.assertEqual(data['items'],[])

    def test_emby_scan_is_queued_only_after_successful_download_processing(self):
        video=self.video();downloads.enqueue(self.store,video,'480');self.store.set('emby_auto_refresh','true')
        def processing(store,media,job,stop):
            with store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM emby_refresh').fetchone()[0],0)
            return ''
        downloads.tick(self.store,self.media,threading.Event(),runner=processing)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT channel FROM emby_refresh').fetchone()[0],'channel00')
            db.execute('DELETE FROM emby_refresh')
        video=self.video(2);downloads.enqueue(self.store,video,'480')
        downloads.tick(self.store,self.media,threading.Event(),runner=Mock(side_effect=ValueError('Conversion failed')))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM emby_refresh').fetchone()[0],0)

    def import_channels(self,ids):
        self.store.set('google_tokens','{"refresh_token":"fixture"}')
        def api(resource,**params):
            if resource=='subscriptions':return {'items':[{'snippet':{'resourceId':{'channelId':i}}} for i in ids]}
            if resource=='channels':return {'items':[{'id':i,'snippet':{'title':i}} for i in params['id'].split(',')]}
            raise AssertionError(resource)
        self.store.set('youtube_subscriptions_next',0)
        with patch.object(Importer,'api',side_effect=api):Importer(self.app).run()

    def test_new_subscription_defaults_preserve_overrides_and_inherit_range(self):
        self.assertFalse(download_options.read(self.store)['new_channel_videos'])
        channel_downloads.save(self.store,'channel00',{'videos':False,'shorts':True,'range_days':30,'quality':'720'})
        response=self.post('/api/downloads',{'action':'options','options':{'new_channel_videos':True,'default_range':7}})
        self.assertEqual(response.status_code,200)
        self.store.set('download_quality','1080')
        self.import_channels(['channel00','channel01','brand-new'])
        new=channel_downloads.preferences(self.store,'brand-new')
        self.assertEqual((new['videos'],new['shorts'],new['live'],new['range_days'],new['quality_override']),(1,0,0,7,0))
        old=channel_downloads.preferences(self.store,'channel00')
        self.assertEqual((old['videos'],old['shorts'],old['range_days'],old['quality']),(0,1,30,'720'))
        self.assertFalse(channel_downloads.preferences(self.store,'channel01')['videos'])
        download_options.save(self.store,{'new_channel_videos':False,'default_range':14})
        self.import_channels(['channel00','brand-new','another-new'])
        self.assertEqual(channel_downloads.preferences(self.store,'brand-new')['range_days'],14)
        self.assertEqual(channel_downloads.preferences(self.store,'channel00')['range_days'],30)
        self.assertEqual(channel_downloads.preferences(self.store,'brand-new')['range_override'],0)
        self.assertTrue(channel_downloads.preferences(self.store,'brand-new')['videos'])
        newest=channel_downloads.preferences(self.store,'another-new')
        self.assertEqual((newest['videos'],newest['range_days']),(0,14))
        self.store.set('download_quality','480')
        self.assertEqual(channel_downloads.preferences(self.store,'brand-new')['quality'],'480')

    def test_invalid_new_channel_defaults_are_rejected(self):
        for value in (True,'7',-1,8):
            response=self.post('/api/downloads',{'action':'options','options':{'default_range':value}})
            self.assertEqual(response.status_code,400)
        response=self.post('/api/downloads',{'action':'options','options':{'new_channel_videos':'true'}})
        self.assertEqual(response.status_code,400)
