"""Disposable queue fixtures: retry budgets, live preferences and private errors."""
import json
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend import channel_downloads, downloads
from backend import auto_downloads
from backend.cookie_recovery import DOWNLOAD_MESSAGE, resume
from tests import test_downloads as fixtures
from tests import test_auto_downloads as auto_fixtures

VIDEO = fixtures.VIDEO
completed_media = fixtures.completed_media


class YouTubeRetryTests(unittest.TestCase):
    setUp = fixtures.DownloadTests.setUp
    tearDown = fixtures.DownloadTests.tearDown
    post = fixtures.DownloadTests.post
    row = fixtures.DownloadTests.row

    def due(self):
        with self.store.connect() as db:db.execute('UPDATE downloads SET retry_at=0')

    def test_initial_plus_five_delayed_retries_are_durable_and_then_final(self):
        self.post({'id': VIDEO['id']})
        runner=Mock(side_effect=downloads.YouTubeFailure('YouTube reports a country/region restriction.'))
        for attempt in range(1, 7):
            self.due()
            downloads.tick(self.store,self.media,threading.Event(),runner)
            row=self.row()
            self.assertEqual(row['attempt_count'],attempt)
            self.assertEqual(row['state'],'queued' if attempt<6 else 'failed')
            self.assertNotIn('country/region',row['message'] if attempt<6 else '')
            self.assertIn('country/region',row['retry_error'])
            if attempt<6:
                self.assertIn(f'retry {attempt} of 5',row['message'])
                self.assertGreater(row['retry_at'],0)
                calls=runner.call_count
                downloads.recover(self.store)
                downloads.tick(self.store,self.media,threading.Event(),runner)
                self.assertEqual(runner.call_count,calls)
                self.assertEqual(self.row()['attempt_count'],attempt)
        downloads.tick(self.store,self.media,threading.Event(),runner)
        self.assertEqual(runner.call_count,6)
        self.assertIn('country/region',self.row()['message'])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM downloads').fetchone()[0],1)
        downloads.enqueue(self.store,VIDEO,'1080',automatic=True)
        self.assertEqual(self.row()['state'],'failed')
        self.assertEqual(self.post({'action':'retry','id':VIDEO['id']}).status_code,200)
        self.assertEqual(self.row()['attempt_count'],0)

    def test_success_stops_retry_and_keeps_one_saved_file(self):
        self.post({'id':VIDEO['id']})
        first=Mock(side_effect=OSError('private cookie=https://secret.invalid'))
        downloads.tick(self.store,self.media,threading.Event(),first)
        self.assertNotIn('secret',self.row()['retry_error'])
        self.due()
        def succeed(store,media,job,stop):
            (Path(media)/(job['id']+'.mp4')).write_bytes(b'disposable completed fixture')
        runner=Mock(side_effect=succeed)
        downloads.tick(self.store,self.media,threading.Event(),runner)
        downloads.tick(self.store,self.media,threading.Event(),runner)
        self.assertEqual(runner.call_count,1)
        self.assertEqual(self.row()['state'],'complete')
        self.assertEqual(self.row()['retry_at'],0)
        self.assertEqual(self.row()['retry_error'],'')
        self.assertEqual(len(list(self.media.rglob('*.mp4'))),1)

    def test_retry_after_seconds_and_http_date_are_respected_and_never_echoed(self):
        from email.utils import formatdate
        now=1_800_000_000
        for value in ('7200',formatdate(now+7200,usegmt=True)):
            with self.subTest(value=value):
                self.assertEqual(downloads.provider_retry_at('Retry-After: '+value,now),now+7200)
        self.assertEqual(downloads.provider_retry_at('Retry-After: secret private token',now),0)
        self.post({'id':VIDEO['id']})
        deadline=downloads.time.time()+7200
        downloads.tick(self.store,self.media,threading.Event(),Mock(side_effect=downloads.YouTubeFailure('Rate limited.',deadline)))
        self.assertGreaterEqual(self.row()['retry_at'],deadline)

    def test_cancellation_hold_wins_over_failure_and_no_new_attempt_starts(self):
        self.post({'id':VIDEO['id']})
        def cancelled(*args):
            self.post({'action':'cancel','id':VIDEO['id']})
            raise downloads.YouTubeFailure('Region restriction.')
        downloads.tick(self.store,self.media,threading.Event(),cancelled)
        self.assertEqual(self.row()['state'],'cancelled')
        runner=Mock()
        downloads.tick(self.store,self.media,threading.Event(),runner)
        runner.assert_not_called()
        self.assertEqual(self.row()['attempt_count'],1)

    def test_pause_or_current_vpn_block_does_not_use_retry_budget(self):
        self.post({'id':VIDEO['id']})
        with patch('backend.vpn.downloads_blocked',return_value=True):
            runner=Mock();downloads.tick(self.store,self.media,threading.Event(),runner)
        runner.assert_not_called();self.assertEqual(self.row()['attempt_count'],0)
        downloads.tick(self.store,self.media,threading.Event(),Mock(side_effect=downloads.DownloadPaused('Waiting for VPN.')))
        self.assertEqual(self.row()['state'],'queued')
        self.assertEqual(self.row()['attempt_count'],0)
        self.assertEqual(self.row()['message'],'Waiting for VPN.')

    def test_restart_during_sixth_attempt_cannot_start_seventh(self):
        self.post({'id':VIDEO['id']})
        with self.store.connect() as db:
            db.execute("UPDATE downloads SET state='downloading',attempt_count=6,retry_error='YouTube remains unavailable.'")
        downloads.recover(self.store)
        runner=Mock();downloads.tick(self.store,self.media,threading.Event(),runner)
        runner.assert_not_called()
        self.assertEqual(self.row()['state'],'failed')
        self.assertIn('unavailable',self.row()['message'])

    def test_restart_after_final_atomic_save_recovers_completion_without_network(self):
        self.post({'id':VIDEO['id']})
        (self.media/(VIDEO['id']+'.mp4')).write_bytes(b'completed fixture')
        with self.store.connect() as db:
            db.execute("UPDATE downloads SET state='downloading',attempt_count=6")
        downloads.recover(self.store)
        runner=Mock();downloads.tick(self.store,self.media,threading.Event(),runner)
        runner.assert_not_called()
        self.assertEqual(self.row()['state'],'complete')

    def test_cookie_recovery_cannot_reset_exhausted_budget(self):
        self.post({'id':VIDEO['id']})
        with self.store.connect() as db:
            db.execute("UPDATE downloads SET state='failed',attempt_count=6,message=?",(DOWNLOAD_MESSAGE,))
        resume(self.store)
        self.assertEqual(self.row()['state'],'failed')
        self.assertEqual(self.row()['attempt_count'],6)

    def test_manual_retry_refreshes_inherited_quality_and_keeps_explicit_quality(self):
        for explicit in (False,True):
            with self.subTest(explicit=explicit):
                channel_downloads.save(self.store,'channel',{'quality':'720'})
                self.post({'id':VIDEO['id'],**({'quality':'480'} if explicit else {})})
                with self.store.connect() as db:db.execute("UPDATE downloads SET state='failed',attempt_count=6")
                channel_downloads.save(self.store,'channel',{'quality':'1080'})
                self.post({'action':'retry','id':VIDEO['id']})
                self.assertEqual(self.row()['quality'],'480' if explicit else '1080')
                self.assertEqual(self.row()['attempt_count'],0)
                downloads.update(self.store,VIDEO['id'],'failed')

    def test_explicit_retry_preserves_automatic_intent_and_rules(self):
        downloads.enqueue(self.store,VIDEO,'720',automatic=True,quality_inherited=True)
        with self.store.connect() as db:db.execute("UPDATE downloads SET state='failed',attempt_count=6")
        self.assertEqual(self.post({'action':'retry','id':VIDEO['id']}).status_code,200)
        self.assertEqual(self.row()['automatic'],1)
        self.assertEqual(self.row()['attempt_count'],0)

    def test_options_cookies_quality_and_child_route_refresh_between_attempts(self):
        channel_downloads.save(self.store,'channel',{'quality':'720'})
        self.post({'id':VIDEO['id']})
        self.post({'action':'options','options':{'authenticated_retry':True,'sponsorblock':'mark'}})
        cookie=self.store.directory/'youtube-cookies.txt'
        def save_cookie(value):
            cookie.write_text('# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t4102444800\tSID\t'+value+'\n')
        save_cookie('first-private-fixture')
        observed=[]
        def process(store,job,stop,args,log,root):
            snapshot=Path(args[args.index('--cookies')+1]).read_text() if '--cookies' in args else ''
            observed.append((job['quality'],'--sponsorblock-mark' in args,'--sponsorblock-remove' in args,snapshot))
            if len(observed)==1:raise downloads.YouTubeFailure('Temporary connection failure.')
            completed_media(log.parent,job,b'fixture');return 0,''
        with patch('backend.downloads.run_process',side_effect=process):
            downloads.tick(self.store,self.media,threading.Event())
            save_cookie('second-private-fixture')
            self.post({'action':'options','options':{'sponsorblock':'remove'}})
            channel_downloads.save(self.store,'channel',{'quality':'1080'})
            self.due();downloads.tick(self.store,self.media,threading.Event())
        self.assertEqual(observed[0][:3],('720',True,False))
        self.assertIn('first-private-fixture',observed[0][3])
        self.assertEqual(observed[1][:3],('1080',False,True))
        self.assertIn('second-private-fixture',observed[1][3])
        self.assertEqual(self.row()['state'],'complete')

    def test_type_probe_preserves_authenticated_geo_reason_and_current_youtube_route(self):
        channel_downloads.save(self.store,'channel',{'videos':True,'range_days':0})
        downloads.enqueue(self.store,VIDEO,'720',automatic=True)
        self.post({'action':'options','options':{'authenticated_retry':True}})
        (self.store.directory/'youtube-cookies.txt').write_text('# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t4102444800\tSID\tfixture\n')
        environments=[]
        def probe(args,stdout,stderr,**kwargs):
            environments.append(kwargs['env']['REELDOCK_TRAFFIC'])
            stderr.write(b'ERROR: The uploader has not made this video available in your country. https://secret.invalid' if '--cookies' in args else b'ERROR: Sign in to confirm you are not a bot.')
            return subprocess.CompletedProcess(args,1)
        # The real attempt is resumed each time; no provider or media is touched.
        with patch('backend.downloads.subprocess.run',side_effect=probe),patch('backend.downloads.run_process') as transfer:
            for _ in range(6):
                self.due();downloads.tick(self.store,self.media,threading.Event())
        transfer.assert_not_called()
        self.assertEqual(environments,['youtube']*12)
        self.assertEqual(self.row()['state'],'failed')
        self.assertIn('country/region restriction',self.row()['message'])
        self.assertNotIn('secret',json.dumps(self.row()))

    def test_retry_after_header_stops_child_before_extractor_can_retry(self):
        import contextlib
        import io
        from yt_dlp import YoutubeDL
        from yt_dlp.networking.exceptions import HTTPError
        from backend.youtube_download import install_retry_after, retry_after
        error=HTTPError(Mock(status=429,reason='Too many requests',headers={'Retry-After':'120'}))
        output=io.StringIO()
        with patch.object(YoutubeDL,'urlopen',side_effect=error) as original:
            install_retry_after()
            with contextlib.redirect_stderr(output),self.assertRaises(SystemExit) as raised:
                YoutubeDL.urlopen(None,'https://private.invalid/?token=secret')
        self.assertEqual(raised.exception.code,75)
        self.assertEqual(original.call_count,1)
        self.assertIn('ReelDock:Retry-After: 120',output.getvalue())
        self.assertNotIn('private',output.getvalue())
        self.assertEqual(retry_after('invalid private header'),0)

    def test_retry_after_prevents_anonymous_and_format_fallback_requests(self):
        self.post({'id':VIDEO['id']})
        process=Mock(return_value=(75,'ReelDock:Retry-After: 900\nERROR: HTTP Error 429'))
        with patch('backend.downloads.run_process',process):
            downloads.tick(self.store,self.media,threading.Event())
        self.assertEqual(process.call_count,1)
        self.assertEqual(self.row()['state'],'queued')
        self.assertGreater(self.row()['retry_at'],downloads.time.time()+890)

    def test_active_retry_progress_keeps_visible_retry_count(self):
        self.post({'id':VIDEO['id']})
        with self.store.connect() as db:db.execute("UPDATE downloads SET state='downloading',attempt_count=3")
        downloads.process_line(self.store,VIDEO['id'],'ReelDock:5:10')
        self.assertIn('retry 2 of 5',self.row()['message'])
        downloads.update_progress(self.store,VIDEO['id'],'Merging.','merging')
        self.assertIn('retry 2 of 5',self.row()['message'])


class YouTubeDropRetryTests(unittest.TestCase):
    setUp=auto_fixtures.AutoDownloadTests.setUp
    tearDown=auto_fixtures.AutoDownloadTests.tearDown
    place=auto_fixtures.AutoDownloadTests.place

    def rows(self):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM auto_downloads').fetchone()),dict(db.execute('SELECT * FROM youtube_drop_retries').fetchone())

    def due(self):
        with self.store.connect() as db:
            db.execute('UPDATE youtube_drop_retries SET retry_at=0')
            db.execute('UPDATE auto_downloads SET updated=0')

    def stop_and_resume(self):
        from backend import acquisition
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name in ('_stop_discover','_stop_downloads','_stop_m3u','_stop_nzb','_stop_torrents'):
                stack.enter_context(patch.object(acquisition,name,return_value={'stopped':0,'pending':0,'errors':[]}))
            acquisition.stop_all(self.app,self.store)
        acquisition.resume(self.store)

    def prepare_receipt(self,info=None):
        self.place('Movie.strm','https://www.youtube.com/watch?v=testvideo01')
        auto_downloads.scan(self.app,self.store,self.stop,{'strm'},youtube=Mock(side_effect=downloads.YouTubeFailure('Temporary failure.')))
        job,_=self.rows()
        media=self.base/'YouTube'
        target=media/'Channel'/'Season 2026'/'fixture.mp4';target.parent.mkdir(parents=True)
        output=self.root/'output.mp4';output.write_bytes(b'identity-bound completed fixture')
        details=info or dict(id='testvideo01',title='Fixture',upload_date='20261031',channel='Fixture')
        token=auto_downloads._youtube_arrival.set({'key':job['key'],'recovery':False})
        try:auto_downloads._youtube_receipt(self.store,media,details,output,target)
        finally:auto_downloads._youtube_arrival.reset(token)
        output.rename(target)
        self.due()
        return target

    def test_drop_has_six_durable_attempts_and_no_failed_alert_until_exhausted(self):
        path=self.place('Movie.strm','https://www.youtube.com/watch?v=testvideo01')
        starter=Mock(side_effect=downloads.YouTubeFailure('YouTube reports a country/region restriction.'))
        for count in range(1,7):
            self.due() if count>1 else None
            auto_downloads.scan(self.app,self.store,self.stop,{'strm'},youtube=starter)
            job,retry=self.rows()
            self.assertEqual(retry['attempt_count'],count)
            self.assertEqual(job['state'],'retry' if count<6 else 'failed')
            with self.store.connect() as db:
                task=db.execute('SELECT state,message FROM activity_tasks WHERE id=?',('auto-'+job['key'][:20],)).fetchone()
            self.assertEqual(task['state'],'queued' if count<6 else 'failed')
            self.assertNotIn('country/region',task['message'] if count<6 else '')
            calls=starter.call_count
            auto_downloads.scan(self.app,self.store,self.stop,{'strm'},youtube=starter)
            self.assertEqual(starter.call_count,calls)
        self.due();auto_downloads.scan(self.app,self.store,self.stop,{'strm'},youtube=starter)
        self.assertEqual(starter.call_count,6)
        self.assertTrue(path.exists())
        self.assertIn('country/region',self.rows()[0]['message'])

    def test_drop_success_ends_retry_and_retains_one_arrival(self):
        path=self.place('Movie.strm','https://www.youtube.com/watch?v=testvideo01')
        starter=Mock(side_effect=[downloads.YouTubeFailure('Temporary failure.'),None])
        auto_downloads.scan(self.app,self.store,self.stop,{'strm'},youtube=starter)
        self.due();auto_downloads.scan(self.app,self.store,self.stop,{'strm'},youtube=starter)
        auto_downloads.scan(self.app,self.store,self.stop,{'strm'},youtube=starter)
        self.assertEqual(starter.call_count,2)
        self.assertEqual(self.rows()[0]['state'],'done')
        self.assertFalse(path.exists())
        self.assertTrue((self.watch/'.reeldock-accepted'/path.name).is_file())

    def test_drop_pause_does_not_spend_budget_and_current_vpn_block_prevents_claim(self):
        self.place('Movie.strm','https://www.youtube.com/watch?v=testvideo01')
        starter=Mock(side_effect=downloads.DownloadPaused('Waiting for VPN.'))
        with patch('backend.vpn.downloads_blocked',return_value=True):
            auto_downloads.scan(self.app,self.store,self.stop,{'strm'},youtube=starter)
        starter.assert_not_called()
        auto_downloads.scan(self.app,self.store,self.stop,{'strm'},youtube=starter)
        self.assertEqual(self.rows()[1]['attempt_count'],0)
        self.assertEqual(self.rows()[0]['state'],'retry')

    def test_prepared_publish_receipt_recovers_without_provider_or_duplicate_file(self):
        auto_downloads.initialise(self.store)
        with self.store.connect() as db:
            db.execute("INSERT INTO youtube_drop_retries(key,attempt_count) VALUES('fixture',6)")
        media=self.base/'YouTube'
        target=media/'Channel'/'Season 2026'/'fixture.mp4';target.parent.mkdir(parents=True)
        output=self.root/'output.mp4';output.write_bytes(b'identity-bound completed fixture')
        info=dict(id='testvideo01',title='Fixture',upload_date='20261031',channel='Fixture')
        token=auto_downloads._youtube_arrival.set({'key':'fixture','recovery':False})
        try:
            auto_downloads._youtube_receipt(self.store,media,info,output,target)
            output.rename(target)
            auto_downloads._youtube_arrival.set({'key':'fixture','recovery':True})
            with patch.object(auto_downloads,'_probe_youtube') as probe,patch.object(auto_downloads,'write_youtube_sidecars',return_value=''),patch('backend.activity.record'):
                auto_downloads._save_youtube(self.app,self.store,'testvideo01',self.stop,'task')
            probe.assert_not_called()
            self.assertEqual(len(list(media.rglob('*.mp4'))),1)
            target.write_bytes(b'different content')
            with self.assertRaisesRegex(ValueError,'cannot be verified'):
                auto_downloads._youtube_receipt(self.store,media)
        finally:auto_downloads._youtube_arrival.reset(token)

    def test_drop_cannot_reset_exhausted_normal_queue(self):
        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES(?,?,0)',('channel',json.dumps({'id':'channel','name':'Fixture'})))
            db.execute('INSERT INTO videos VALUES(?,?,?,?,?,?)',(VIDEO['id'],'channel','2026-10-31','2026-10-31',json.dumps(VIDEO),0))
        downloads.enqueue(self.store,VIDEO,'720')
        with self.store.connect() as db:db.execute("UPDATE downloads SET state='failed',attempt_count=6")
        with self.assertRaisesRegex(auto_downloads._HeldSource,'Five automatic retries'):
            auto_downloads.start_youtube(self.app,self.store,VIDEO['id'],'fixture.strm',self.stop)
        with self.store.connect() as db:
            row=db.execute('SELECT state,attempt_count FROM downloads').fetchone()
        self.assertEqual(tuple(row),('failed',6))

    def test_real_legacy_publication_and_metadata_retry_reuse_completed_receipt(self):
        from types import SimpleNamespace
        path=self.place('Movie.strm','https://www.youtube.com/watch?v=testvideo01')
        info=dict(id='testvideo01',title='Fixture',upload_date='20261031',channel='Fixture',channel_id='UC'+'a'*22)
        def transfer(args,stdout,**kwargs):
            folder=Path(args[args.index('-o')+1]).parent
            (folder/'video.mp4').write_bytes(b'completed disposable media fixture')
            stdout.write('ReelDock:32:32\n');stdout.flush()
            return SimpleNamespace(poll=lambda:0,wait=lambda **kwargs:0)
        with patch.object(auto_downloads,'_probe_youtube',return_value=info) as probe,patch.object(auto_downloads,'_channel_images',return_value={}), \
             patch.object(auto_downloads,'youtube_card',return_value={}),patch.object(auto_downloads,'write_youtube_sidecars',side_effect=[ValueError('Metadata temporarily unavailable.'),'']), \
             patch('backend.auto_downloads.subprocess.Popen',side_effect=transfer) as process:
            auto_downloads.scan(self.app,self.store,self.stop,{'strm'})
            self.assertEqual(self.rows()[0]['state'],'retry')
            self.assertEqual(self.rows()[1]['stage'],'published')
            self.due();auto_downloads.scan(self.app,self.store,self.stop,{'strm'})
        self.assertEqual(probe.call_count,1)
        self.assertEqual(process.call_count,1)
        self.assertEqual(len(list((self.base/'YouTube').rglob('*.mp4'))),1)
        self.assertEqual(self.rows()[0]['state'],'done')
        self.assertFalse(path.exists())

    def test_final_receipt_recovery_pause_cannot_refund_an_exhausted_budget(self):
        auto_downloads.initialise(self.store)
        with self.store.connect() as db:
            db.execute("INSERT INTO youtube_drop_retries(key,attempt_count) VALUES('fixture',6)")
            db.execute("INSERT INTO auto_downloads VALUES('fixture','Fixture','strm','running','',0)")
        token=auto_downloads._youtube_arrival.set({'key':'fixture','recovery':True})
        try:auto_downloads._youtube_failure(self.store,'fixture',downloads.DownloadPaused('Paused.'),paused=True)
        finally:auto_downloads._youtube_arrival.reset(token)
        self.assertEqual(self.rows()[1]['attempt_count'],6)

    def test_fresh_legacy_probe_must_match_requested_video_before_transfer(self):
        path=self.place('Movie.strm','https://www.youtube.com/watch?v=testvideo01')
        details=dict(id='othervideo1',title='Different',upload_date='20261031',channel='Fixture')
        with patch.object(auto_downloads,'_extract_youtube',return_value=details),patch('backend.auto_downloads.subprocess.Popen') as process:
            auto_downloads.scan(self.app,self.store,self.stop,{'strm'})
        process.assert_not_called()
        self.assertNotEqual(self.rows()[0]['state'],'done')
        self.assertIn('did not match',self.rows()[1]['error'])
        self.assertTrue(path.exists())
        self.assertFalse(list((self.base/'YouTube').rglob('*.mp4')))

    def test_recovered_receipt_identity_must_match_requested_video(self):
        target=self.prepare_receipt(dict(id='othervideo1',title='Different',upload_date='20261031'))
        with patch.object(auto_downloads,'_probe_youtube') as probe,patch.object(auto_downloads,'write_youtube_sidecars') as sidecars:
            auto_downloads.scan(self.app,self.store,self.stop,{'strm'})
        probe.assert_not_called();sidecars.assert_not_called()
        self.assertNotEqual(self.rows()[0]['state'],'done')
        self.assertIn('does not match',self.rows()[1]['error'])
        self.assertEqual(target.read_bytes(),b'identity-bound completed fixture')

    def test_stop_during_receipt_hash_prevents_sidecars_and_completion_after_resume(self):
        target=self.prepare_receipt()
        original=auto_downloads.hashlib.file_digest
        def verify(*args,**kwargs):
            result=original(*args,**kwargs);self.stop_and_resume();return result
        with patch.object(auto_downloads.hashlib,'file_digest',side_effect=verify),patch.object(auto_downloads,'write_youtube_sidecars') as sidecars:
            auto_downloads.scan(self.app,self.store,self.stop,{'strm'})
        sidecars.assert_not_called()
        self.assertEqual(self.rows()[0]['state'],'held')
        self.assertEqual(target.read_bytes(),b'identity-bound completed fixture')

    def test_stop_during_recovery_sidecars_prevents_completed_arrival(self):
        self.prepare_receipt()
        def sidecars(*args,**kwargs):self.stop_and_resume();return ''
        with patch.object(auto_downloads,'write_youtube_sidecars',side_effect=sidecars):
            auto_downloads.scan(self.app,self.store,self.stop,{'strm'})
        self.assertEqual(self.rows()[0]['state'],'held')
        self.assertTrue((self.watch/'Movie.strm').exists())

    def test_delayed_legacy_retry_stays_stopped_after_stop_all_and_resume(self):
        self.place('Movie.strm','https://www.youtube.com/watch?v=testvideo01')
        starter=Mock(side_effect=downloads.YouTubeFailure('Temporary failure.'))
        auto_downloads.scan(self.app,self.store,self.stop,{'strm'},youtube=starter)
        self.assertEqual(self.rows()[0]['state'],'retry')
        self.stop_and_resume();self.due()
        auto_downloads.scan(self.app,self.store,self.stop,{'strm'},youtube=starter)
        self.assertEqual(starter.call_count,1)
        self.assertEqual(self.rows()[0]['state'],'held')
        self.assertEqual(self.rows()[1]['attempt_count'],1)

    def test_active_legacy_transfer_stays_stopped_after_stop_all_and_resume(self):
        from types import SimpleNamespace
        self.place('Movie.strm','https://www.youtube.com/watch?v=testvideo01')
        info=dict(id='testvideo01',title='Fixture',upload_date='20261031',channel='Fixture')
        def transfer(args,stdout,**kwargs):
            (Path(args[args.index('-o')+1]).parent/'video.mp4').write_bytes(b'partial fixture')
            self.stop_and_resume()
            return SimpleNamespace(poll=lambda:0,wait=lambda **kwargs:0)
        with patch.object(auto_downloads,'_probe_youtube',return_value=info),patch.object(auto_downloads,'_channel_images',return_value={}), \
             patch.object(auto_downloads,'youtube_card',return_value={}),patch('backend.auto_downloads.subprocess.Popen',side_effect=transfer) as process:
            auto_downloads.scan(self.app,self.store,self.stop,{'strm'})
            self.due();auto_downloads.scan(self.app,self.store,self.stop,{'strm'})
        self.assertEqual(process.call_count,1)
        self.assertEqual(self.rows()[0]['state'],'held')
        self.assertEqual(self.rows()[1]['attempt_count'],1)
        self.assertFalse(list((self.base/'YouTube').rglob('*.mp4')))

    def test_legacy_preflight_auth_option_refreshes_between_retries(self):
        self.place('Movie.strm','https://www.youtube.com/watch?v=testvideo01')
        (self.store.directory/'youtube-cookies.txt').write_text('# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t4102444800\tSID\tprivate-fixture\n')
        self.store.set('download_authenticated_retry','true')
        snapshots=[]
        def extract(args,input,**kwargs):
            request=json.loads(input);snapshots.append('cookie' in request)
            return subprocess.CompletedProcess(args,1,b'',b'ERROR: Video unavailable.')
        with patch('backend.auto_downloads.subprocess.run',side_effect=extract):
            auto_downloads.scan(self.app,self.store,self.stop,{'strm'})
            self.store.set('download_authenticated_retry','false')
            self.due();auto_downloads.scan(self.app,self.store,self.stop,{'strm'})
        self.assertEqual(snapshots,[True,False])
        self.assertEqual(self.rows()[1]['attempt_count'],2)


if __name__=='__main__':unittest.main()
