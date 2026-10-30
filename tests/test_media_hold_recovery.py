"""Selected-media Clear survives automatic recovery without touching saved files."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.app import create_app
from backend import channel_downloads, cookie_recovery, downloads, media_holds, sync_all
from backend.storage import Store

ID='heldvideo01'
OTHER='othervideo1'
CHANNEL='fixture-channel'


class MediaHoldRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.media=self.root/'media';self.media.mkdir()
        self.app=create_app(dict(TESTING=True,DATA_DIR=str(self.root/'data'),MEDIA_DIR=str(self.media)))
        self.store=self.app.extensions['store']
        self.store.set('setup_complete','1')
        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES(?,?,1)',(CHANNEL,json.dumps(dict(id=CHANNEL,name='Fixture'))))
        self.video(ID);self.video(OTHER)

    def video(self,identity):
        value=dict(id=identity,channel=CHANNEL,title=identity,date='2026-10-24',state='published',kind='videos')
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO videos VALUES(?,?,?,?,?,0)',(identity,CHANNEL,value['date'],value['date'],json.dumps(value)))
        return value

    def job(self,identity=ID,state='failed',message='',replace=False,created=1):
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO downloads(id,title,quality,state,message,created,updated,replace_existing) VALUES(?,?,'720',?,?,?,1,?)",(identity,identity,state,message,created,int(replace)))
            return dict(db.execute('SELECT * FROM downloads WHERE id=?',(identity,)).fetchone())

    def hold(self,keys=None):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            media_holds.hold(db,keys or ['video:'+ID],[('youtube',ID)])

    def row(self,identity=ID):
        with self.store.connect() as db:return dict(db.execute('SELECT * FROM downloads WHERE id=?',(identity,)).fetchone())

    def test_restart_recovers_unrelated_transfer_but_cancels_held_queued_and_running(self):
        self.job(state='downloading');self.job(OTHER,'downloading');self.job('queuedheld1','queued')
        self.hold(['video:'+ID,'managed:video:queuedheld1'])
        reopened=Store(self.store.directory)
        downloads.recover(reopened);downloads.recover(reopened)
        self.assertEqual(self.row()['state'],'cancelled');self.assertEqual(self.row('queuedheld1')['state'],'cancelled')
        self.assertEqual(self.row(OTHER)['state'],'queued')
        self.assertTrue(media_holds.blocked(reopened,'video:'+ID))

    def test_cookie_recovery_does_not_requeue_cleared_video_or_watched_receipt(self):
        self.job(message=cookie_recovery.DOWNLOAD_MESSAGE);self.job(OTHER,message=cookie_recovery.DOWNLOAD_MESSAGE)
        from backend.youtube_matching import source_scope
        scope=source_scope(self.store)
        with self.store.connect() as db:
            for identity in (ID,OTHER):db.execute("INSERT INTO emby_watched VALUES(?,?,'auth_expired','')",(scope,identity))
        self.hold();cookie_recovery.resume(self.store)
        self.assertEqual(self.row()['state'],'failed');self.assertEqual(self.row(OTHER)['state'],'queued')
        with self.store.connect() as db:
            self.assertEqual(dict(db.execute('SELECT id,state FROM emby_watched')), {ID:'auth_expired',OTHER:'retry'})
            self.assertEqual([r[0] for r in db.execute('SELECT id FROM emby_attempts')],[OTHER])
        self.assertTrue(media_holds.blocked(self.store,'video:'+ID))

    def test_sync_all_excludes_exact_held_youtube_and_vod_not_other_source_or_native(self):
        self.job();self.job(OTHER)
        with self.store.connect() as db:
            for source in ('one','two'):
                db.execute("INSERT INTO m3u_sources(id,name,url,options,folder) VALUES(?,?,?,'{}',?)",(source,source,'https://fixture.invalid/list',source))
                db.execute("INSERT INTO m3u_downloads(id,source,path,state,created,updated) VALUES(?,?,'same.strm','failed',1,1)",(source,source))
        native=self.store.directory/'nzb-engine'/'jobs';native.mkdir(parents=True)
        checkpoint=native/'fixture.json';checkpoint.write_bytes(b'unchanged native checkpoint')
        self.hold(['video:'+ID,media_holds.vod_key('one','same.strm')]);sync_all.queue(self.store)
        self.assertEqual(self.row()['state'],'failed');self.assertEqual(self.row(OTHER)['state'],'queued')
        with self.store.connect() as db:self.assertEqual(dict(db.execute('SELECT id,state FROM m3u_downloads')),{'one':'failed','two':'queued'})
        receipt=json.loads(self.store.get('sync_all_receipt'))
        self.assertEqual(receipt['downloads'],[OTHER]);self.assertEqual(receipt['server_downloads'],['two'])
        self.assertEqual(checkpoint.read_bytes(),b'unchanged native checkpoint')
        self.assertTrue(media_holds.blocked(self.store,['video:'+ID,media_holds.vod_key('one','same.strm')]))

    def test_bulk_channel_replacement_preserves_held_completed_file_and_preferences(self):
        self.job(state='complete');self.job(OTHER,'complete')
        saved=self.media/(ID+'.mp4');saved.write_bytes(b'existing saved bytes');before=saved.stat()
        channel_downloads.save(self.store,CHANNEL,dict(videos=True,range_days=0))
        rule=channel_downloads.preferences(self.store,CHANNEL);self.hold()
        result=channel_downloads.perform(self.store,self.media,CHANNEL,'redownload_existing')
        self.assertEqual(self.row()['state'],'complete');self.assertFalse(self.row()['replace_existing'])
        self.assertEqual(self.row(OTHER)['state'],'queued');self.assertIn('1 cleared video',result)
        self.assertEqual((saved.stat().st_ino,saved.stat().st_mtime_ns,saved.read_bytes()),(before.st_ino,before.st_mtime_ns,b'existing saved bytes'))
        self.assertEqual(channel_downloads.preferences(self.store,CHANNEL),rule)

    def test_automatic_channel_schedule_cannot_count_or_resurrect_held_expired_video(self):
        self.job(state='expired');self.hold();channel_downloads.save(self.store,CHANNEL,dict(videos=True,range_days=0))
        # Only the held ID is eligible in this fixture.
        with self.store.connect() as db:db.execute('DELETE FROM videos WHERE id=?',(OTHER,))
        self.assertEqual(channel_downloads.schedule(self.store,self.media,CHANNEL),0)
        self.assertEqual(self.row()['state'],'expired');self.assertTrue(media_holds.blocked(self.store,'video:'+ID))

    def test_claim_does_not_start_a_held_legacy_queued_row(self):
        self.job(state='queued');self.hold();runner=Mock()
        downloads.tick(self.store,self.media,threading.Event(),runner)
        runner.assert_not_called();self.assertEqual(self.row()['state'],'cancelled')

    def test_claim_skips_held_backlog_and_runs_unrelated_video_in_same_tick(self):
        self.job(state='queued');self.job(OTHER,'queued',created=2);self.hold();seen=[]
        def runner(store,media,job,stop):seen.append(job['id'])
        downloads.tick(self.store,self.media,threading.Event(),runner)
        self.assertEqual(seen,[OTHER]);self.assertEqual(self.row()['state'],'cancelled')
        self.assertEqual(self.row(OTHER)['state'],'complete');self.assertTrue(media_holds.blocked(self.store,'video:'+ID))

    def test_held_transfer_entry_does_not_inspect_cookies_or_start_provider(self):
        job=self.job(state='downloading');self.hold()
        with patch.object(downloads.subprocess,'run') as probe,patch.object(downloads,'run_process') as transfer,patch('backend.watched_status.cookie_session_file_status') as cookies:
            with self.assertRaisesRegex(ValueError,'Cancelled'):
                downloads.run_download(self.store,self.media,job,threading.Event())
        probe.assert_not_called();transfer.assert_not_called();cookies.assert_not_called()
        self.assertEqual(list(self.media.iterdir()),[])

    def test_clear_during_automatic_probe_prevents_public_retry_and_payload(self):
        channel_downloads.save(self.store,CHANNEL,dict(videos=True,range_days=0,minimum_video_minutes=0))
        job=self.job(state='downloading');job['automatic']=1
        from backend import download_options
        download_options.save(self.store,dict(authenticated_retry=True))
        (self.store.directory/'youtube-cookies.txt').write_bytes(b'synthetic fixture')
        def finish_probe(*args,**kwargs):self.hold();return Mock(returncode=1)
        with patch.object(downloads.subprocess,'run',side_effect=finish_probe) as probe,patch.object(downloads,'run_process') as transfer,patch('backend.cookies.allow_downloads',return_value=True),patch('backend.watched_status.cookie_session_file_status',return_value=('unchecked','')):
            with self.assertRaisesRegex(ValueError,'Cancelled'):
                downloads.run_download(self.store,self.media,job,threading.Event())
        self.assertEqual(probe.call_count,1);self.assertIn('--cookies',probe.call_args.args[0])
        transfer.assert_not_called();self.assertEqual(list(self.media.iterdir()),[])

    def test_late_hold_after_transfer_blocks_complete_and_stays_cancelled_after_restart(self):
        self.job(state='queued')
        def runner(*args):self.hold();return ''
        downloads.tick(self.store,self.media,threading.Event(),runner)
        self.assertEqual(self.row()['state'],'cancelled');downloads.recover(self.store)
        self.assertEqual(self.row()['state'],'cancelled')

    def test_all_worker_exit_paths_preserve_clear_instead_of_retry_or_failure(self):
        for error in (InterruptedError(),downloads.DownloadPaused(),downloads.DownloadSkipped('Excluded'),ValueError('Network unavailable')):
            with self.subTest(error=type(error).__name__):
                media_holds.resume(self.store,'video:'+ID);self.job(state='queued')
                def runner(*args):self.hold();raise error
                downloads.tick(self.store,self.media,threading.Event(),runner)
                self.assertEqual(self.row()['state'],'cancelled')
                self.assertTrue(media_holds.blocked(self.store,'video:'+ID))

    def test_cancelled_replacement_keeps_saved_bytes_complete_and_never_requeues(self):
        self.job(state='queued',replace=True);saved=self.media/(ID+'.mp4');saved.write_bytes(b'saved')
        def runner(*args):self.hold();raise InterruptedError()
        downloads.tick(self.store,self.media,threading.Event(),runner)
        self.assertEqual(self.row()['state'],'complete');self.assertEqual(saved.read_bytes(),b'saved')
        downloads.recover(self.store);self.assertEqual(self.row()['state'],'complete')
        self.assertFalse(self.row()['replace_existing']);self.assertTrue(media_holds.blocked(self.store,'video:'+ID))

    def test_running_process_notices_durable_hold_and_terminates_without_another_attempt(self):
        job=self.job(state='downloading');process=Mock();process.pid=123;process.poll.return_value=None
        process.wait.return_value=0
        owner=self
        class Turn:
            def is_set(self):return False
            def wait(self,seconds):owner.hold();return False
        with patch.object(downloads.subprocess,'Popen',return_value=process) as spawn,patch.object(downloads.os,'killpg') as signal:
            with self.assertRaisesRegex(ValueError,'Cancelled'):
                downloads.run_process(self.store,job,Turn(),['fixture-command'],self.root/'progress.log',self.media)
        self.assertEqual(spawn.call_count,1);self.assertEqual(signal.call_count,1);process.wait.assert_called_once()

    def test_hold_arriving_after_final_precheck_prevents_publication_and_metadata(self):
        job=self.job(state='downloading');real_contained=downloads.library.contained
        def transfer(store,job,stop,args,log,root):
            (log.parent/'video.mp4').write_bytes(b'new completed bytes')
            (log.parent/downloads.MANUAL_DETAILS_FILE).write_text(json.dumps(dict(id=ID,duration=90,live_status='not_live')))
            return 0,''
        def target(root,relative):
            result=real_contained(root,relative)
            if relative.endswith('.mp4') and str(relative).startswith('Fixture/'):
                self.hold()
            return result
        # Reserve first so the injected hold occurs at the final target lookup,
        # after check_attempt, rather than during harmless path reservation.
        downloads.library.reserve(self.store,self.media,ID)
        with patch.object(downloads,'run_process',side_effect=transfer),patch.object(downloads.library,'contained',side_effect=target),patch.object(downloads.library,'write_metadata') as metadata,patch('backend.cookies.allow_downloads',return_value=False):
            with self.assertRaisesRegex(ValueError,'Cancelled'):
                downloads.run_download(self.store,self.media,job,threading.Event())
        metadata.assert_not_called();self.assertEqual(list(self.media.rglob('*.mp4')),[])

    def test_only_explicit_selected_authenticated_retry_releases_exact_hold(self):
        self.job();self.job(OTHER);self.hold();self.store.set('public_origin','https://fixture.test')
        with self.store.connect() as db:db.execute("INSERT INTO users VALUES(1,'fixture','unused')")
        client=self.app.test_client()
        with client.session_transaction(base_url='https://fixture.test') as session:session['user_id']=1;session['csrf']='token'
        args=dict(base_url='https://fixture.test',json=dict(id=ID,action='retry'))
        self.assertEqual(client.post('/api/downloads',headers={'Origin':'https://fixture.test','X-CSRF-Token':'wrong'},**args).status_code,403)
        self.assertTrue(media_holds.blocked(self.store,'video:'+ID))
        self.assertEqual(client.post('/api/downloads',headers={'Origin':'https://fixture.test','X-CSRF-Token':'token'},**args).status_code,200)
        self.assertFalse(media_holds.blocked(self.store,'video:'+ID));self.assertEqual(self.row()['state'],'queued')
        self.assertEqual(self.row(OTHER)['state'],'failed')

    def managed_movie(self):
        from backend.media_management import store as records
        key='movie:12345';item=dict(id=key,kind='movie',externalId='12345',title='Known movie',year='2020')
        records.remember(self.store,key,key,'movie',dict(item=item,date='2020-01-01',automatic=True))
        with self.store.connect() as db:db.execute('UPDATE managed_media SET monitor=1 WHERE key=?',(key,))
        return records.row(self.store,key)

    def test_automatic_managed_queue_never_releases_clear_arriving_after_policy_check(self):
        from backend.media_management import service
        from backend import discover_acquire as acquire
        record=self.managed_movie();original=acquire.queue_manual
        def clear_before_insert(*args,**kwargs):
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                media_holds.hold(db,['managed:'+record['key']],[])
                db.execute('UPDATE managed_media SET hold=1 WHERE key=?',(record['key'],))
            return original(*args,**kwargs)
        with patch.object(acquire,'queue_manual',side_effect=clear_before_insert) as queue,patch.object(acquire,'process',side_effect=AssertionError('Held worker must not search')):
            self.assertEqual(service.queue(self.app,self.store,record,automatic=True),[])
        self.assertTrue(queue.call_args.kwargs['automatic'])
        self.assertTrue(media_holds.blocked(self.store,'managed:'+record['key']))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM discover_requests').fetchone()[0],0)

    def test_actual_monitor_tick_cannot_turn_late_clear_into_explicit_manual_resume(self):
        from backend import media_management,discover_acquire as acquire
        from backend.media_management import service,store as records
        record=self.managed_movie();original=service.queue
        self.store.set('managed_alias_reconcile',time.time())
        def late_clear(app,store,row,*args,**kwargs):
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                media_holds.hold(db,['managed:'+row['key']],[])
                db.execute('UPDATE managed_media SET hold=1 WHERE key=?',(row['key'],))
            return original(app,store,row,*args,**kwargs)
        with patch.object(service,'enrol'),patch.object(service,'compose',return_value=dict(monitored=True,file=False,active=False)),patch.object(service,'queue',side_effect=late_clear) as queue,patch.object(acquire,'process',side_effect=AssertionError('No provider after Clear')):
            media_management.tick(self.app,self.store)
        self.assertEqual(queue.call_count,1);self.assertTrue(queue.call_args.kwargs['automatic'])
        self.assertTrue(media_holds.blocked(self.store,'managed:'+record['key']))
        self.assertTrue(records.row(self.store,record['key'])['hold'])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM discover_requests').fetchone()[0],0)

    def test_automatic_request_keeps_automatic_provenance_but_explicit_selection_can_resume(self):
        from backend import discover_acquire as acquire
        from backend.media_management import service
        record=self.managed_movie()
        with patch.object(acquire,'process'):
            result=service.queue(self.app,self.store,record,automatic=True)
        self.assertEqual(result[0]['manual'],0)
        with self.store.connect() as db:
            db.execute("UPDATE discover_requests SET state='cancelled'")
            media_holds.hold(db,['managed:'+record['key']],[('discover',result[0]['id'])])
            media_holds.hold(db,['managed:movie:67890'],[])
            db.execute('UPDATE managed_media SET hold=1 WHERE key=?',(record['key'],))
        with patch.object(acquire,'process'):
            resumed=service.queue(self.app,self.store,record)
        self.assertEqual(resumed[0]['manual'],1)
        self.assertFalse(media_holds.blocked(self.store,'managed:'+record['key']))
        self.assertTrue(media_holds.blocked(self.store,'managed:movie:67890'))

    def test_explicit_movie_and_episode_still_stopping_keep_clear_fence(self):
        from backend import discover_acquire as acquire,discover_store as ds
        for item,episodes,key in [(dict(kind='movie',externalId='12345',title='Known movie'),[],'movie:12345'),
                                  (dict(kind='show',externalId='12345',title='Known show'),[dict(id=1,season=1,number=1)],'show:12345:1:1')]:
            with self.subTest(kind=item['kind']):
                row,_=ds.insert_request(self.store,dict(idempotency='stopping-'+item['kind'],kind='movie' if item['kind']=='movie' else 'episode',title='Selected',quality='1080',fallback=False,episode_key=key,body={}))
                ds.cancel_request(self.store,row['id'])
                with self.store.connect() as db:media_holds.hold(db,['managed:'+key],[('discover',row['id'])])
                result=acquire.queue_manual(self.store,item,episodes,'1080',False,'NZB first','explicit-selected-'+item['kind'])
                self.assertEqual(result[0]['state'],'cancelling');self.assertTrue(result[0]['blocked'])
                self.assertTrue(media_holds.blocked(self.store,'managed:'+key))


if __name__=='__main__':unittest.main()
