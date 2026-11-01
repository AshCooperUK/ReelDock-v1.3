import json
import tempfile
import threading
import unittest
import sys
from unittest.mock import patch
from pathlib import Path
from backend.app import create_app
from backend import downloads, download_options

ORIGIN = 'https://dev.example.com'
VIDEO = {'id': 'testvideo01', 'title': 'Test video', 'channel': 'channel', 'state': 'published', 'date':'2026-09-26'}


def completed_media(folder,job,raw):
    """The fake extractor publishes the same identity-bound facts as yt-dlp."""
    (folder/downloads.MANUAL_DETAILS_FILE).write_text(json.dumps(dict(id=job['id'],width=1920,height=1080,duration=900,live_status='not_live')))
    return (folder/'video.mp4').write_bytes(raw)


class DownloadTests(unittest.TestCase):
    def test_cancel_all_stops_active_and_queue_but_preserves_finished_records(self):
        states=['queued','downloading','complete','failed','cancelled','skipped']
        with self.store.connect() as db:
            db.executemany("INSERT INTO downloads(id,title,state,created,updated,quality) VALUES(?,?,?,0,0,'720')",
                           [(f'job{i:08d}','Video',state) for i,state in enumerate(states)])
        self.assertEqual(self.post({'action':'cancel_all'},csrf='wrong').status_code,403)
        self.assertIn(self.app.test_client().post('/api/downloads',base_url=ORIGIN,json={'action':'cancel_all'}).status_code,(401,403))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM downloads WHERE id='job00000000'").fetchone()[0],'queued')
        self.assertEqual(self.post({'action':'cancel_all'}).status_code,200)
        self.assertEqual(self.post({'action':'cancel_all'}).status_code,200)
        with self.store.connect() as db:
            result=[r[0] for r in db.execute('SELECT state FROM downloads ORDER BY id')]
        self.assertEqual(result,['cancelled','cancelling','complete','failed','cancelled','skipped'])

    def test_completed_history_keeps_latest_fifteen_without_losing_queue_or_records(self):
        with self.store.connect() as db:
            db.executemany("INSERT INTO downloads(id,title,state,created,updated,quality) VALUES(?,?,?,?,?,'720')",
                           [(f'old{i:08d}', 'Done', 'complete', i, i) for i in range(25)] +
                           [('pending0001', 'Pending', 'queued', 0, 0), ('failure0001', 'Failed', 'failed', 0, 0)])
        data = self.client.get('/api/downloads', base_url=ORIGIN).json
        completed = [r['id'] for r in data['items'] if r['state'] == 'complete']
        self.assertEqual(completed, [f'old{i:08d}' for i in range(24, 9, -1)])
        self.assertEqual(data['shown'], 17)
        self.assertEqual(data['total'], 27)
        self.assertIn('pending0001', [r['id'] for r in data['items']])
        self.assertIn('failure0001', [r['id'] for r in data['items']])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM downloads').fetchone()[0], 27)

    def test_live_feed_bounds_failed_history_but_keeps_every_active_row(self):
        with self.store.connect() as db:
            db.executemany("INSERT INTO downloads(id,title,state,created,updated,quality) VALUES(?,?,?,?,?,'720')",
                           [(f'fail{i:07d}','Failed',('failed','cancelled','skipped')[i%3],i,i) for i in range(40)] +
                           [(f'wait{i:07d}','Queued','queued',100+i,100+i) for i in range(30)] +
                           [('running0001','Running','downloading',50,50)])
        data=self.client.get('/api/downloads',base_url=ORIGIN).json
        ids=[r['id'] for r in data['items']]
        self.assertEqual(ids[0],'running0001')
        self.assertEqual(ids[1:31],[f'wait{i:07d}' for i in range(30)])
        # Only the newest fifteen finished-but-not-complete rows are polled.
        self.assertEqual(ids[31:],[f'fail{i:07d}' for i in range(39,24,-1)])
        self.assertEqual(data['total'],71)
        self.assertEqual(data['shown'],46)
        # Older rows are not lost: the paged Activity history still lists them.
        history=self.client.get('/api/download-history?limit=50&offset=50',base_url=ORIGIN).json
        self.assertIn('fail0000000',[row['item']['id'] for row in history['items'] if row['kind']=='youtube'])

    def test_live_feed_reports_file_sizes_with_one_lookup(self):
        from backend import media_library
        (self.media/(VIDEO['id']+'.mp4')).write_bytes(b'12345')
        with self.store.connect() as db:
            db.execute("INSERT INTO downloads(id,title,state,created,updated,quality) VALUES(?,?,'complete',1,1,'720')",(VIDEO['id'],'Saved'))
            db.execute("INSERT INTO downloads(id,title,state,created,updated,quality) VALUES('missing0001','Gone','complete',2,2,'720')")
        self.assertEqual(media_library.file_sizes(self.store,self.media,[VIDEO['id'],'missing0001','../etc/pass']),{VIDEO['id']:5})
        items={r['id']:r for r in self.client.get('/api/downloads',base_url=ORIGIN).json['items']}
        self.assertEqual(items[VIDEO['id']]['bytes'],5)
        self.assertTrue(items[VIDEO['id']]['video']['downloaded'])
        self.assertEqual(items['missing0001']['bytes'],0)

    def test_single_video_status_auth_queue_progress_and_missing_video(self):
        path='/api/downloads/'+VIDEO['id']
        self.assertEqual(self.app.test_client().get(path,base_url=ORIGIN).status_code,401)
        self.assertEqual(self.client.get('/api/downloads/unknown0001',base_url=ORIGIN).status_code,404)
        initial=self.client.get(path,base_url=ORIGIN)
        self.assertEqual(initial.status_code,200)
        self.assertIsNone(initial.json['job'])
        self.assertEqual(initial.headers['Cache-Control'],'no-store')
        self.assertEqual(self.post({'id':VIDEO['id'],'action':'queue','quality':'720'}).status_code,200)
        self.assertEqual(self.client.get(path,base_url=ORIGIN).json['job']['state'],'queued')
        downloads.update(self.store,VIDEO['id'],'downloading',42,'Downloading')
        result=self.client.get(path,base_url=ORIGIN).json
        self.assertEqual(result['job'],{'state':'downloading','progress':42,'message':'Downloading'})
        self.assertNotIn('items',result)
        self.assertEqual(self.post({'id':VIDEO['id'],'action':'cancel'}).status_code,200)
        self.assertEqual(self.client.get(path,base_url=ORIGIN).json['job']['state'],'cancelling')

    def test_channel_uploads_include_unsaved_and_saved_and_paginate(self):
        with self.store.connect() as db:
            for i in range(18):
                video={**VIDEO,'id':f'upload{i:05d}','title':f'Upload {i}',
                       'state':'upcoming' if i==17 else 'published'}
                db.execute('INSERT INTO videos VALUES(?,?,?,?,?,?)',
                           (video['id'],'channel','2026-09-26','2026-09-26',json.dumps(video),0))
        self.media.joinpath('upload00000.mp4').write_bytes(b'fixture')
        with self.store.connect() as db:
            db.execute("INSERT INTO downloads(id,title,state,created,updated,quality) VALUES('upload00000','Saved','complete',0,0,'720')")
        first=self.client.get('/api/guide/channel/channel',base_url=ORIGIN).json
        second=self.client.get('/api/guide/channel/channel?offset=15',base_url=ORIGIN).json
        self.assertTrue(first['more'])
        self.assertFalse(second['more'])
        items=first['items']+second['items']
        self.assertEqual(len(items),19)
        self.assertEqual(len({item['id'] for item in items}),19)
        self.assertIn('upcoming',{item['state'] for item in items})
        states=self.client.get('/api/download-status?ids=upload00000,upload00001',base_url=ORIGIN).json
        self.assertEqual(states['downloaded'],{'upload00000':True,'upload00001':False})

    def test_batch_status_reports_queue_failure_and_missing_completed_file(self):
        ids=['batch000001','batch000002','batch000003']
        with self.store.connect() as db:
            for video,state in zip(ids,['queued','failed','complete']):
                db.execute("INSERT INTO downloads(id,title,state,created,updated,quality) VALUES(?,?,?,0,0,'720')",(video,video,state))
        path='/api/download-status?ids='+','.join(ids)
        self.assertEqual(self.app.test_client().get(path,base_url=ORIGIN).status_code,401)
        result=self.client.get(path,base_url=ORIGIN).json
        self.assertEqual(result['jobs'],dict(zip(ids,['queued','failed','complete'])))
        self.assertEqual(result['downloaded'],dict.fromkeys(ids,False))
        self.media.joinpath(ids[-1]+'.mp4').write_bytes(b'fixture')
        self.assertTrue(self.client.get(path,base_url=ORIGIN).json['downloaded'][ids[-1]])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.media = Path(self.tmp.name) / 'media'
        self.media.mkdir()
        self.app = create_app({'TESTING': True, 'DATA_DIR': self.tmp.name, 'MEDIA_DIR': str(self.media)})
        self.store = self.app.extensions['store']
        self.store.set('public_origin', ORIGIN)
        self.store.set('setup_complete', '1')
        with self.store.connect() as db:
            db.execute("INSERT INTO users VALUES(1,'owner','unused')")
            db.execute('INSERT INTO channels VALUES(?,?,1)', ('channel', json.dumps({'id':'channel','name':'Test'})))
            db.execute('INSERT INTO videos VALUES(?,?,?,?,?,?)', (VIDEO['id'],'channel','2026-09-26','2026-09-26',json.dumps(VIDEO),0))
        self.client = self.app.test_client()
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session['user_id'] = 1
            session['csrf'] = 'token'
        # These tests cover the download pipeline, not the minimum-length rule
        # (covered below and in test_channel_rules_everywhere), so no type probe runs.
        download_options.save(self.store,{'minimum_video_minutes':0})

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, data, csrf='token'):
        return self.client.post('/api/downloads',base_url=ORIGIN,json=data,
                                headers={'Origin':ORIGIN,'X-CSRF-Token':csrf})

    def row(self):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM downloads').fetchone())

    def test_automatic_download_rechecks_cached_kind_and_minimum_before_transfer(self):
        from backend import channel_downloads, download_options
        from types import SimpleNamespace
        channel_downloads.save(self.store,'channel',{'videos':True,'shorts':False,'live':True,'range_days':0})
        video={**VIDEO,'kind':'videos'}
        with self.store.connect() as db:
            db.execute('UPDATE videos SET data=? WHERE id=?',(json.dumps(video),VIDEO['id']))
        for info,minimum,message in [
            ({'width':1080,'height':1920,'duration':120},5,'turned off for this channel'),
            ({'width':1920,'height':1080,'duration':120},5,'5-minute minimum'),
            ({'width':1920,'height':1080,'duration':299},5,'5-minute minimum'),
            ({'width':1920,'height':1080,'duration':300},5,'passed inspection'),
            ({'width':1920,'height':1080,'duration':120},0,'passed inspection'),
            ({'was_live':True,'duration':120},5,'passed inspection'),
        ]:
            with self.subTest(info=info,minimum=minimum):
                with self.store.connect() as db:
                    db.execute('UPDATE videos SET data=? WHERE id=?',(json.dumps(video),VIDEO['id']))
                download_options.save(self.store,{'minimum_video_minutes':minimum})
                def probe(args,stdout,**kwargs):
                    stdout.write(json.dumps(info).encode())
                    return SimpleNamespace(returncode=0)
                with patch('backend.downloads.subprocess.run',side_effect=probe), patch('backend.downloads.shutil.disk_usage',return_value=SimpleNamespace(free=2*1024**3)), patch('backend.downloads.library.reserve',side_effect=ValueError('passed inspection')):
                    with self.assertRaisesRegex(ValueError,message):
                        downloads.run_download(self.store,self.media,{**VIDEO,'quality':'720','automatic':True},threading.Event())
        for invalid in (-1,121,True,1.5,'5'):
            with self.assertRaises(ValueError):download_options.save(self.store,{'minimum_video_minutes':invalid})

    def test_unknown_type_is_probed_before_any_media_download_and_cannot_bypass_filters(self):
        from backend import channel_downloads
        from types import SimpleNamespace
        channel_downloads.save(self.store,'channel',{'videos':True,'shorts':False,'live':False,'range_days':0})
        unknown={k:v for k,v in VIDEO.items() if k!='kind'}
        for info,expected in [({'width':1080,'height':1920,'duration':40},'turned off for this channel'),
                              ({'was_live':True,'duration':3600},'turned off for this channel'),
                              ({'width':1920,'height':1080,'duration':900},'passed inspection')]:
            with self.subTest(info=info):
                with self.store.connect() as db:
                    db.execute('UPDATE videos SET data=? WHERE id=?',(json.dumps(unknown),VIDEO['id']))
                calls=[]
                def probe(args,stdout,**kwargs):
                    calls.append(args);self.assertIn('--skip-download',args)
                    stdout.write(json.dumps(info).encode());return SimpleNamespace(returncode=0)
                with patch('backend.downloads.subprocess.run',side_effect=probe), \
                     patch('backend.downloads.shutil.disk_usage',return_value=SimpleNamespace(free=2*1024**3)), \
                     patch('backend.downloads.run_process',side_effect=AssertionError('media download started')) as transfer, \
                     patch('backend.downloads.library.reserve',side_effect=ValueError('passed inspection')):
                    with self.assertRaisesRegex(ValueError,expected):
                        downloads.run_download(self.store,self.media,{**VIDEO,'quality':'720','automatic':True},threading.Event())
                self.assertEqual(len(calls),1)  # The type probe ran first ...
                transfer.assert_not_called()      # ... and no media transfer started.
                with self.store.connect() as db:
                    saved=json.loads(db.execute('SELECT data FROM videos WHERE id=?',(VIDEO['id'],)).fetchone()[0])
                self.assertIn(saved['kind'],('shorts','live','videos'))

    def test_short_detection_uses_aspect_ratio_and_duration(self):
        self.assertEqual(downloads.classify_video({'aspect_ratio':0.56,'duration':180},'2026-09-01'),'shorts')
        self.assertEqual(downloads.classify_video({'width':1080,'height':1080,'duration':180},'2026-09-01'),'shorts')
        self.assertEqual(downloads.classify_video({'aspect_ratio':0.56,'duration':181},'2026-09-01'),'videos')
        self.assertEqual(downloads.classify_video({'aspect_ratio':1.78,'duration':60},'2026-09-01'),'videos')
        with self.assertRaises(downloads.DownloadSkipped):downloads.classify_video({'duration':60},'2026-09-01')

    def test_activity_contains_active_video_and_channel_without_full_queue(self):
        downloads.enqueue(self.store,VIDEO,'720')
        downloads.update(self.store,VIDEO['id'],'downloading',42)
        with self.store.connect() as db:
            db.execute("UPDATE downloads SET processing_info='Speed 2.8x',message='Hardware encoding' WHERE id=?",(VIDEO['id'],))
        response=self.client.get('/api/activity',base_url=ORIGIN)
        self.assertEqual(response.status_code,200)
        item=response.get_json()['download']
        self.assertEqual(item['id'],VIDEO['id'])
        self.assertEqual(item['video']['id'],VIDEO['id'])
        self.assertEqual(item['channel'],'channel')
        self.assertEqual(item['progress'],42)
        self.assertEqual(item['quality'],'720')
        self.assertEqual(item['processing_info'],'Speed 2.8x')
        self.assertEqual(item['message'],'Hardware encoding')

    def test_full_queue_matches_worker_order_with_active_first(self):
        with self.store.connect() as db:
            db.executemany("INSERT INTO downloads(id,title,quality,state,created,updated) VALUES (?,?,'720',?,?,?)",[
                (f'queue{i:06d}','Queued','queued',i+1,i+1) for i in range(205)
            ]+[("active00001","Active","downloading",300,300),
               ("done0000001","Done","complete",400,400)])
        data=self.client.get('/api/downloads',base_url=ORIGIN).get_json()
        self.assertEqual(len(data['items']),207)
        self.assertEqual(data['items'][0]['id'],'active00001')
        self.assertEqual([r['id'] for r in data['items'][1:-1]],[f'queue{i:06d}' for i in range(205)])
        self.assertEqual(data['items'][-1]['id'],'done0000001')

    def test_type_probe_excludes_large_metadata_and_preserves_classification_fields(self):
        from yt_dlp import YoutubeDL
        with YoutubeDL({'quiet': True}) as ydl:
            for fields in (
                {'width':1920,'height':1080,'duration':1200,'is_live':False,'was_live':False,'live_status':'not_live'},
                {'width':1080,'height':1920,'duration':60},
                {'is_live':True,'live_status':'is_live'},
                {'was_live':True,'live_status':'was_live'},
                {},
            ):
                with self.subTest(fields=fields):
                    info={**fields,'formats':[{'fragments':'x'*5_000_001}], 'description':'unused'}
                    raw=ydl.evaluate_outtmpl(downloads.TYPE_PROBE_TEMPLATE,info)
                    self.assertLess(len(raw),1024)
                    self.assertEqual(json.loads(raw),fields)

    def test_ffmpeg_progress_reads_complete_reports_without_exposing_raw_output(self):
        path=Path(self.tmp.name)/'progress'
        path.write_text('frame=25\nout_time=00:01:23.500000\nspeed=0.25x\nprogress=continue\nframe=26\n',encoding='utf-8')
        result=downloads.ffmpeg_progress(path)
        self.assertEqual(result[:2],('00:01:23','0.25x'))
        path.write_text('out_time=private-url\nspeed=cookie\nprogress=end\n',encoding='utf-8')
        self.assertIsNone(downloads.ffmpeg_progress(path))

    def test_processing_phases_replace_transfer_percentage(self):
        self.post({'id':VIDEO['id']})
        downloads.update(self.store,VIDEO['id'],'downloading')
        downloads.process_line(self.store,VIDEO['id'],'ReelDock:50:100')
        self.assertEqual(self.row()['progress'],50)
        self.assertEqual(self.row()['phase'],'downloading')
        for line,phase in [('[Merger] Merging formats into private-path','merging'),
                           ('[ModifyChapters] Re-encoding private-path with appropriate keyframes','encoding'),
                           ('[ModifyChapters] Removing chapters from private-path','sponsors'),
                           ('[Metadata] Adding metadata to private-path','metadata')]:
            downloads.process_line(self.store,VIDEO['id'],line)
            self.assertEqual(self.row()['phase'],phase)
            self.assertNotIn('private-path',self.row()['message'])
        downloads.update(self.store,VIDEO['id'],'cancelled')
        downloads.process_line(self.store,VIDEO['id'],'ReelDock:99:100')
        self.assertEqual(self.row()['state'],'cancelled')

    def test_api_queue_duplicate_quality_and_security(self):
        self.assertEqual(self.post({'id':VIDEO['id']},'bad').status_code,403)
        self.assertEqual(self.post({'id':'../outside'}).status_code,404)
        self.assertEqual(self.post({'id':VIDEO['id'],'quality':'unlimited'}).status_code,400)
        self.assertEqual(self.post({'action':'quality','quality':'720'}).status_code,200)
        for _ in range(2):
            response = self.post({'id':VIDEO['id']})
            self.assertEqual(response.status_code,200)
            self.assertEqual(len(response.json['items']),1)
        self.assertEqual(self.row()['quality'],'720')
        self.client.get('/logout',base_url=ORIGIN)
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session.clear()
        self.assertEqual(self.client.get('/api/downloads',base_url=ORIGIN).status_code,401)

    def test_cancel_retry_and_restart(self):
        self.post({'id':VIDEO['id']})
        self.post({'action':'cancel','id':VIDEO['id']})
        self.assertEqual(self.row()['state'],'cancelled')
        self.post({'action':'retry','id':VIDEO['id']})
        downloads.update(self.store,VIDEO['id'],'downloading',50)
        downloads.recover(self.store)
        self.assertEqual(self.row()['state'],'queued')
        self.assertEqual(self.row()['progress'],0)

    def test_completed_file_is_reported_saved_and_has_no_playback_route(self):
        self.post({'id':VIDEO['id']})
        def runner(store, media, job, stop):
            (Path(media)/(job['id']+'.mp4')).write_bytes(b'0123456789')
        downloads.tick(self.store,self.media,threading.Event(),runner)
        self.assertEqual(self.row()['state'],'complete')
        status=self.client.get('/api/downloads/'+VIDEO['id'],base_url=ORIGIN).json
        self.assertTrue(status['saved']);self.assertEqual(status['job']['state'],'complete')
        for removed in ('/api/playback/'+VIDEO['id'],'/media/video/'+VIDEO['id']):
            self.assertEqual(self.client.get(removed,base_url=ORIGIN).status_code,404)

    def test_failure_sanitised_and_retry(self):
        self.post({'id':VIDEO['id']})
        def fail(*args):
            raise OSError('private cookie content')
        downloads.tick(self.store,self.media,threading.Event(),fail)
        self.assertEqual(self.row()['state'],'queued')
        self.assertEqual(self.row()['attempt_count'],1)
        self.assertNotIn('private cookie',self.row()['message'])
        self.assertNotIn('private cookie',self.row()['retry_error'])
        self.post({'action':'retry','id':VIDEO['id']})
        self.assertEqual(self.row()['state'],'queued')

    def test_command_only_fixed_youtube_url_and_private_output(self):
        args=downloads.command(VIDEO['id'],'720',self.media)
        self.assertIn('--ignore-config',args)
        self.assertEqual(args[-1],'https://www.youtube.com/watch?v='+VIDEO['id'])
        self.assertIn(str(self.media/'video.%(ext)s'),args)
        with self.assertRaises(ValueError):
            downloads.enqueue(self.store,{**VIDEO,'state':'live'},'720')

    def test_fresh_defaults_only_transcode_when_needed(self):
        from backend import download_options
        options=download_options.for_channel(self.store,'channel')
        self.assertFalse(options['force_transcode'])
        self.assertTrue(options['convert_fallback'])
        self.assertEqual(options['sponsorblock'],'off')
        self.post({'id':VIDEO['id']});calls=[]
        def process(store,job,stop,args,log,root):
            calls.append(args[:]);completed_media(log.parent,job,b'compatible');return 0,''
        with patch('backend.downloads.run_process',side_effect=process):downloads.tick(self.store,self.media,threading.Event())
        self.assertEqual(len(calls),1)
        self.assertNotEqual(calls[0][0],'ffmpeg')
        self.assertEqual(self.row()['state'],'complete')

    def test_channel_processing_override_inheritance_validation_and_persistence(self):
        from backend import channel_downloads, download_options
        download_options.save(self.store,{'sponsorblock':'remove','force_transcode':True})
        self.assertEqual(download_options.for_channel(self.store,'channel')['sponsorblock'],'remove')
        channel_downloads.save(self.store,'channel',{'sponsorblock':'off','force_transcode':'off'})
        channel_downloads.save(self.store,'channel',{'videos':True})
        self.assertEqual(download_options.for_channel(self.store,'channel')['sponsorblock'],'off')
        self.assertFalse(download_options.for_channel(self.store,'channel')['force_transcode'])
        self.assertEqual(download_options.read(self.store)['sponsorblock'],'remove')
        self.assertTrue(download_options.for_channel(self.store,'another')['force_transcode'])
        for values in ({'force_transcode':True},{'sponsorblock':'bad'}):
            with self.assertRaises(ValueError):channel_downloads.save(self.store,'channel',values)
        channel_downloads.save(self.store,'channel',{'sponsorblock':'default','force_transcode':'default'})
        self.assertTrue(download_options.for_channel(self.store,'channel')['force_transcode'])
        channel_downloads.initialise(self.store)
        self.assertEqual(channel_downloads.preferences(self.store,'channel')['sponsorblock'],'default')

    def test_channel_override_is_used_by_manual_download_and_forces_ffmpeg(self):
        from backend import channel_downloads
        self.post({'action':'options','options':{'sponsorblock':'remove','hardware_encoding':False}})
        channel_downloads.save(self.store,'channel',{'sponsorblock':'off','force_transcode':'on'})
        self.post({'id':VIDEO['id']});calls=[]
        def process(store,job,stop,args,log,root):
            calls.append(args[:])
            if args[0]=='ffmpeg':Path(args[-1]).write_bytes(b'reencoded')
            else:completed_media(log.parent,job,b'source')
            return 0,''
        with patch('backend.downloads.run_process',side_effect=process):downloads.tick(self.store,self.media,threading.Event())
        self.assertNotIn('--sponsorblock-remove',calls[0])
        self.assertEqual(calls[-1][0],'ffmpeg')
        self.assertIn('libx264',calls[-1])
        self.assertEqual(downloads.library.local_file(self.store,self.media,VIDEO['id']).read_bytes(),b'reencoded')

    def test_channel_override_can_remove_sponsors_when_global_is_off(self):
        from backend import channel_downloads
        channel_downloads.save(self.store,'channel',{'sponsorblock':'remove'})
        self.post({'id':VIDEO['id']});calls=[]
        def process(store,job,stop,args,log,root):
            calls.append(args[:]);completed_media(log.parent,job,b'cut');return 0,''
        with patch('backend.downloads.run_process',side_effect=process):downloads.tick(self.store,self.media,threading.Event())
        self.assertIn('--sponsorblock-remove',calls[0])
        self.assertIn('--force-keyframes-at-cuts',calls[0])

    def test_forced_conversion_failure_keeps_existing_download(self):
        from backend import channel_downloads
        channel_downloads.save(self.store,'channel',{'force_transcode':'on'})
        self.post({'id':VIDEO['id']})
        target=self.media/(VIDEO['id']+'.mp4');target.write_bytes(b'existing file')
        with self.store.connect() as db:db.execute('UPDATE downloads SET replace_existing=1')
        def process(store,job,stop,args,log,root):
            if args[0]=='ffmpeg':return 1,'conversion failed'
            completed_media(log.parent,job,b'source');return 0,''
        with patch('backend.downloads.run_process',side_effect=process):downloads.tick(self.store,self.media,threading.Event())
        self.assertEqual(self.row()['state'],'queued')
        self.assertIn('conversion failed',self.row()['retry_error'])
        self.assertEqual(target.read_bytes(),b'existing file')

    def test_expired_cookies_and_api_quota_do_not_block_known_download(self):
        self.store.set('google_quota_retry_at',4102444800)
        (self.store.directory/'youtube-cookies.txt').write_text('# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1\tSID\tfixture\n')
        self.post({'id':VIDEO['id']})
        def process(store,job,stop,args,log,root):
            self.assertNotIn('--cookies',args)
            completed_media(log.parent,job,b'complete');return 0,''
        with patch('backend.downloads.run_process',side_effect=process):downloads.tick(self.store,self.media,threading.Event())
        self.assertEqual(self.row()['state'],'complete')

    def test_automatic_probe_retries_without_cookies_and_preserves_type_rules(self):
        from backend import channel_downloads
        from types import SimpleNamespace
        channel_downloads.save(self.store,'channel',{'videos':True,'shorts':False,'range_days':0})
        (self.store.directory/'youtube-cookies.txt').write_text('# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t4102444800\tSID\tfixture\n')
        self.post({'action':'options','options':{'authenticated_retry':True}})
        calls=[]
        def probe(args,stdout,stderr,**kwargs):
            calls.append(args[:])
            if '--cookies' in args:
                stderr.write(b'cookies have expired');return SimpleNamespace(returncode=1)
            stdout.write(json.dumps({'width':1080,'height':1920,'duration':120}).encode())
            return SimpleNamespace(returncode=0)
        with patch('backend.downloads.subprocess.run',side_effect=probe), patch('backend.downloads.shutil.disk_usage',return_value=SimpleNamespace(free=2*1024**3)):
            with self.assertRaisesRegex(downloads.DownloadSkipped,'turned off for this channel'):
                downloads.run_download(self.store,self.media,{**VIDEO,'quality':'720','automatic':True},threading.Event())
        self.assertEqual(len(calls),2);self.assertNotIn('--cookies',calls[1])

    def test_cookie_retry_and_sponsor_options(self):
        self.post({'action':'options','options':{'authenticated_retry':True,'sponsorblock':'mark'}})
        (self.store.directory/'youtube-cookies.txt').write_text('# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t4102444800\tSID\tfixture\n')
        self.post({'id':VIDEO['id']})
        calls=[]
        def process(store,job,stop,args,log,root):
            calls.append(args[:])
            if '--cookies' in args:return 1,'Sign in to confirm your age'
            completed_media(log.parent,job,b'complete')
            return 0,''
        with patch('backend.downloads.run_process',side_effect=process):downloads.tick(self.store,self.media,threading.Event())
        self.assertEqual(['--cookies' in args for args in calls],[True,True,False])
        self.assertNotIn('--force-keyframes-at-cuts',calls[0])
        self.assertIn('--sponsorblock-mark',calls[0]);self.assertEqual(self.row()['state'],'complete')

    def test_sponsor_removal_uses_safe_cuts_before_publishing(self):
        self.post({'action':'options','options':{'sponsorblock':'remove'}})
        self.post({'id':VIDEO['id']})
        calls=[]
        def process(store,job,stop,args,log,root):
            calls.append(args[:])
            self.assertFalse(downloads.library.local_file(store,root,job['id']))
            completed_media(log.parent,job,b'safely cut')
            return 0,''
        with patch('backend.downloads.run_process',side_effect=process):
            downloads.tick(self.store,self.media,threading.Event())
        self.assertIn('--sponsorblock-remove',calls[0])
        self.assertIn('--force-keyframes-at-cuts',calls[0])
        self.assertLess(calls[0].index('--force-keyframes-at-cuts'),calls[0].index('--'))
        self.assertEqual(self.row()['state'],'complete')
        self.assertEqual(downloads.library.local_file(self.store,self.media,VIDEO['id']).read_bytes(),b'safely cut')

    def test_failed_sponsor_reencode_preserves_existing_file(self):
        self.post({'action':'options','options':{'sponsorblock':'remove'}})
        self.post({'id':VIDEO['id']})
        target=self.media/(VIDEO['id']+'.mp4');target.write_bytes(b'existing file')
        with self.store.connect() as db:
            db.execute('UPDATE downloads SET replace_existing=1')
        with patch('backend.downloads.run_process',return_value=(1,'FFmpeg processing failed')):
            downloads.tick(self.store,self.media,threading.Event())
        self.assertEqual(self.row()['state'],'queued')
        self.assertEqual(self.row()['attempt_count'],1)
        self.assertEqual(target.read_bytes(),b'existing file')

    def test_gpu_conversion_failure_retries_cpu_before_publishing(self):
        self.post({'action':'options','options':{'convert_fallback':True}})
        self.post({'id':VIDEO['id']});calls=[]
        def process(store,job,stop,args,log,root):
            calls.append(args[:])
            if len(calls)==1:return 1,'Requested format is not available'
            if args[0]!='ffmpeg':completed_media(log.parent,job,b'source');return 0,''
            if 'h264_vaapi' in args:return 1,'GPU unavailable'
            Path(args[-1]).write_bytes(b'cpu fallback');return 0,''
        with patch('backend.hardware_encoding.available_device',return_value='/dev/dri/renderD128'),patch('backend.downloads.run_process',side_effect=process):
            downloads.tick(self.store,self.media,threading.Event())
        self.assertIn('h264_vaapi',calls[-2]);self.assertIn('libx264',calls[-1])
        self.assertEqual(self.row()['state'],'complete')
        self.assertEqual(downloads.library.local_file(self.store,self.media,VIDEO['id']).read_bytes(),b'cpu fallback')

    def test_unavailable_format_converts_before_publish(self):
        self.post({'action':'options','options':{'convert_fallback':True}})
        self.post({'id':VIDEO['id']});calls=[]
        def process(store,job,stop,args,log,root):
            calls.append(args[:])
            self.assertFalse(downloads.library.local_file(store,root,job['id']))
            if len(calls)==1:return 1,'Requested format is not available'
            if args[0]=='ffmpeg':Path(args[-1]).write_bytes(b'converted')
            else:completed_media(log.parent,job,b'source')
            return 0,''
        with patch('backend.downloads.run_process',side_effect=process):downloads.tick(self.store,self.media,threading.Event())
        self.assertEqual(calls[-1][0],'ffmpeg')
        self.assertEqual(downloads.library.local_file(self.store,self.media,VIDEO['id']).read_bytes(),b'converted')

    def test_default_quality_and_legacy_disk_usage(self):
        from backend import channel_downloads
        channel_downloads.save(self.store,'channel',{'videos':True})
        self.post({'action':'quality','quality':'720'})
        self.post({'id':VIDEO['id']});self.assertEqual(self.row()['quality'],'720')
        self.post({'action':'cancel','id':VIDEO['id']})
        channel_downloads.save(self.store,'channel',{'quality':'480'})
        self.post({'action':'quality','quality':'1080'})
        self.post({'id':VIDEO['id']});self.assertEqual(self.row()['quality'],'480')
        (self.media/(VIDEO['id']+'.mp4')).write_bytes(b'1234567890')
        downloads.update(self.store,VIDEO['id'],'complete',100)
        self.assertEqual(downloads.library.disk_usage(self.store,self.media,['channel']),{'channel':10})
        data=self.client.get('/api/downloads',base_url=ORIGIN).json
        self.assertEqual(data['items'][0]['bytes'],10)
        self.assertEqual(data['items'][0]['video']['title'],VIDEO['title'])

    def test_queue_pause_does_not_start_download(self):
        self.post({'action':'options','options':{'pause_downloads':True}})
        self.post({'id':VIDEO['id']})
        with patch('backend.downloads.run_download') as runner:
            downloads.tick(self.store,self.media,threading.Event(),runner)
            runner.assert_not_called()
        self.assertEqual(self.row()['state'],'queued')

    def test_partial_media_never_selected(self):
        (self.media/(VIDEO['id']+'.mp4.part')).write_bytes(b'partial')
        self.assertFalse(self.client.get('/api/downloads/'+VIDEO['id'],base_url=ORIGIN).json['saved'])

    def test_process_publishes_only_finished_media(self):
        self.post({'id':VIDEO['id']})
        def fake_command(video_id, quality, folder, cookie):
            script="from pathlib import Path; import json; Path("+repr(str(folder/'video.mp4'))+").write_bytes(b'completed'); Path("+repr(str(folder/downloads.MANUAL_DETAILS_FILE))+").write_text("+repr(json.dumps(dict(id=video_id,width=1920,height=1080,duration=900,live_status='not_live')))+")"
            return [sys.executable, '-c',script,'-f','best','--','https://www.youtube.com/watch?v='+video_id]
        with patch('backend.downloads.command',side_effect=fake_command):
            downloads.tick(self.store,self.media,threading.Event())
        self.assertEqual(self.row()['state'],'complete',self.row()['message'])
        self.assertEqual((self.media/'Test/Season 2026/s2026.e092601 - Test video.mp4').read_bytes(),b'completed')
        self.assertFalse(list(self.media.glob('.reeldock-download-*')))

    def test_running_process_can_be_cancelled(self):
        self.post({'id':VIDEO['id']})
        def fake_command(video_id, quality, folder, cookie):
            downloads.update(self.store,video_id,'cancelling')
            return [sys.executable, '-c', 'import time; time.sleep(30)','-f','best','--','https://www.youtube.com/watch?v='+video_id]
        with patch('backend.downloads.command',side_effect=fake_command):
            downloads.tick(self.store,self.media,threading.Event())
        self.assertEqual(self.row()['state'],'cancelled')
        self.assertFalse(list(self.media.iterdir()))

    def test_downloads_without_google_connection_for_existing_install(self):
        self.store.set('last_sync','123456789')
        self.store.set('setup_complete','')
        self.store.set('google_tokens','')
        self.assertEqual(self.post({'id':VIDEO['id']}).status_code,200)
        self.assertEqual(self.post({'action':'quality','quality':'720'}).status_code,200)
        self.assertEqual(self.post({'action':'cancel','id':VIDEO['id']}).status_code,200)
        self.assertEqual(self.post({'action':'retry','id':VIDEO['id']},csrf='bad').status_code,403)
        self.store.set('last_sync','')
        self.assertEqual(self.post({'id':VIDEO['id']}).status_code,409)

    def test_downloaded_status_requires_nonempty_file_and_updates_guide(self):
        self.post({'id':VIDEO['id']})
        downloads.update(self.store,VIDEO['id'],'complete',100)
        url='/api/download-status?ids='+VIDEO['id']
        self.assertFalse(self.client.get(url,base_url=ORIGIN).json['downloaded'][VIDEO['id']])
        path=self.media/(VIDEO['id']+'.mp4');path.write_bytes(b'video')
        self.assertTrue(self.client.get(url,base_url=ORIGIN).json['downloaded'][VIDEO['id']])
        response=self.client.get('/api/guide/channel/channel',base_url=ORIGIN)
        self.assertTrue(response.json['items'][0]['downloaded'])
        path.unlink()
        self.assertFalse(self.client.get(url,base_url=ORIGIN).json['downloaded'][VIDEO['id']])
        self.assertEqual(self.client.get('/api/download-status?ids=../secret',base_url=ORIGIN).status_code,400)
