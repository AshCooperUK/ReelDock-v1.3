"""Global controls with isolated databases, real child process and transfer fixtures."""
import sys
import time
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock
from tests import test_engine_settings as fixture, test_m3u_downloads as vod_fixture
from backend import downloads, download_options, download_control, acquisition, discover_store
from backend.downloaders.nzb.engine import NzbEngine, _Job
from backend.downloaders.nzb.nntp import _Reader, NntpError
from backend.downloaders.torrent.engine import TorrentEngine, Job


def until(check, timeout=6):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if check():return
        time.sleep(0.05)
    raise AssertionError('Timed out waiting for fixture state')


class GlobalControlTests(unittest.TestCase):
    setUp=fixture.EngineSettingsTests.setUp

    def test_pause_persists_and_resume_clears_cancel_hold(self):
        download_options.save(self.store, {'pause_downloads':True})
        self.assertTrue(download_control.reader(self.store)())
        acquisition.begin_hold(self.store)
        self.store.set('download_pause_downloads','false')
        self.assertTrue(download_options.read(self.store)['pause_downloads'])
        download_options.save(self.store, {'pause_downloads':False})
        self.assertFalse(download_control.paused(self.store))
        self.assertFalse(acquisition.held(self.store))

    def test_pause_reason_follows_the_existing_gate_precedence_without_extra_vpn_reads(self):
        with patch('backend.acquisition.held',return_value=True) as hold,patch('backend.vpn.downloads_blocked',return_value=True) as vpn:
            self.store.set('download_pause_downloads','true')
            self.assertEqual(download_control.pause_status(self.store),dict(paused=True,reason='downloads'))
            hold.assert_not_called();vpn.assert_not_called()
            self.store.set('download_pause_downloads','false')
            self.assertEqual(download_control.pause_status(self.store),dict(paused=True,reason='hold'))
            vpn.assert_not_called()
            hold.return_value=False
            self.assertEqual(download_control.pause_status(self.store),dict(paused=True,reason='vpn'))
            self.assertEqual(vpn.call_count,1)
            vpn.return_value=False
            self.assertEqual(download_control.pause_status(self.store),dict(paused=False,reason=''))
            self.assertFalse(download_control.paused(self.store))

    def test_vpn_reason_uses_only_the_existing_protected_download_gate(self):
        import json
        directory=Path(self.tmp.name)/'public-vpn-status';directory.mkdir()
        self.store.set('download_pause_downloads','false')
        with patch('backend.vpn.RUN',directory),patch.dict('os.environ',{'REELDOCK_VPN_TRANSPORT':'1'}),patch('backend.vpn.call',side_effect=AssertionError('No gateway RPC')):
            for protected,connected,age,reason in [(False,False,0,''),(True,False,0,'vpn'),(True,True,0,''),(True,True,30,'vpn')]:
                (directory/'status.json').write_text(json.dumps(dict(values=dict(downloads=protected,metadata=False),connected=connected,checked_at=time.time()-age)))
                self.assertEqual(download_control.pause_status(self.store),dict(paused=bool(reason),reason=reason))
            with patch.dict('os.environ',{'REELDOCK_VPN_TRANSPORT':'0'}):
                self.assertEqual(download_control.pause_status(self.store),dict(paused=False,reason=''))

    def test_nzb_public_pause_reason_uses_the_same_gate_sample_and_keeps_job_state_and_observation(self):
        root=Path(self.tmp.name)
        gate=download_control.reader(self.store)
        with patch.object(NzbEngine,'_spawn'):
            engine=NzbEngine(root/'media',root/'nzb',global_paused=gate)
        self.addCleanup(engine.shutdown)
        job=_Job(id='same-exact-job',name='Fixture',files=[],selected=set(),phase='download',done_parts=3,total_parts=6)
        job.storage=SimpleNamespace(published={});job.engine=engine
        original=(job.phase,job.user_paused,job.done_parts,job.total_parts,set(job.selected))
        with patch('backend.download_control.pause_status',side_effect=[dict(paused=True,reason='vpn'),dict(paused=False,reason='')]) as probe,patch.object(engine,'_save_job') as save:
            row=engine._job_dict(job)
            self.assertEqual((row['state'],row['pause_reason'],row['progress']),('paused','vpn',.5))
            self.assertEqual(row['activity_observation']['state'],'paused')
            self.assertEqual(gate.snapshot(),dict(paused=True,reason='vpn'))
            self.assertEqual(probe.call_count,1,'DTO reason never resamples the VPN after choosing its state')
            save.assert_not_called()
        self.assertEqual((job.phase,job.user_paused,job.done_parts,job.total_parts,set(job.selected)),original)
        gate.snapshot=lambda:dict(paused=True,reason='vpn')
        for phase,user,expected in [('paused',True,'user'),('download',True,'user'),('failed',False,''),('stopped',False,''),('choose',False,'')]:
            job.phase,job.user_paused=phase,user
            value=engine._job_dict(job)
            self.assertEqual(value['pause_reason'],expected)
            if phase in ('failed','stopped','choose'):self.assertEqual(value['state'],phase)
        job.phase,job.user_paused='download',False
        engine.global_paused=lambda:True
        legacy=engine._job_dict(job)
        self.assertEqual((legacy['state'],legacy['pause_reason']),('paused',''))
        engine.global_paused=Mock(return_value=False)
        self.assertEqual((engine._job_dict(job)['state'],engine._job_dict(job)['pause_reason']),('downloading',''))
        engine.global_paused.snapshot.side_effect=OSError('No readable status')
        failed=engine._job_dict(job)
        self.assertEqual((failed['state'],failed['pause_reason']),('paused',''))

    def test_nzb_route_preserves_pause_reason_without_an_extra_vpn_rpc_or_queue_write(self):
        root=Path(self.tmp.name);self.store.set('storage_base',str(root/'media'))
        with patch.object(NzbEngine,'_spawn'):
            engine=NzbEngine(root/'media',root/'nzb',global_paused=download_control.reader(self.store))
        self.addCleanup(engine.shutdown)
        job=_Job(id='exact-route-job',name='Fixture',files=[],selected=set(),phase='queued',done_parts=2,total_parts=5)
        job.storage=SimpleNamespace(published={});job.engine=engine;engine._order=[job]
        with self.store.connect() as db:before=list(db.iterdump())
        with patch('backend.nzb._engine',return_value=engine),patch('backend.download_control.pause_status',return_value=dict(paused=True,reason='vpn')) as probe,patch('backend.vpn.call',side_effect=AssertionError('No gateway RPC')):
            result=self.client.get('/api/nzb',base_url=fixture.ORIGIN)
        self.assertEqual(result.status_code,200,result.json)
        row=result.json['nzbs'][0]
        self.assertEqual((row['id'],row['state'],row['pause_reason'],row['progress']),('exact-route-job','paused','vpn',.4))
        self.assertEqual(probe.call_count,1)
        with self.store.connect() as db:self.assertEqual(list(db.iterdump()),before)

    def test_youtube_process_suspends_resumes_and_cancels_while_paused(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO downloads(id,title,quality,state,created,updated) VALUES ('pausevideo1','Fixture','720','downloading',0,0)")
        root=Path(self.tmp.name); log=root/'process.log'; done=threading.Event(); errors=[]
        def work():
            try:downloads.run_process(self.store,{'id':'pausevideo1'},threading.Event(),[sys.executable,'-u','-c','import time\nwhile True:\n print("tick",flush=True);time.sleep(.05)'],log,root)
            except ValueError as exc:errors.append(str(exc))
            finally:done.set()
        thread=threading.Thread(target=work);thread.start()
        def phase():
            with self.store.connect() as db:return db.execute("SELECT phase FROM downloads WHERE id='pausevideo1'").fetchone()[0]
        try:
            until(lambda:log.exists() and log.stat().st_size>0)
            download_options.save(self.store, {'pause_downloads':True})
            until(lambda:phase()=='paused')
            size=log.stat().st_size;time.sleep(.3);self.assertEqual(log.stat().st_size,size)
            download_options.save(self.store, {'pause_downloads':False})
            until(lambda:log.stat().st_size>size)
            download_options.save(self.store, {'pause_downloads':True})
            until(lambda:phase()=='paused')
            with self.store.connect() as db:db.execute("UPDATE downloads SET state='cancelling' WHERE id='pausevideo1'")
            self.assertTrue(done.wait(4));self.assertEqual(errors,['Cancelled.'])
        finally:
            with self.store.connect() as db:db.execute("UPDATE downloads SET state='cancelling' WHERE id='pausevideo1'")
            thread.join(5)

    def test_nzb_queue_and_article_gate_keep_manual_pause(self):
        engine=NzbEngine(Path(self.tmp.name)/'media',Path(self.tmp.name)/'nzb',global_paused=lambda:download_control.paused(self.store))
        self.addCleanup(engine.shutdown)
        job=_Job(id='fixture',name='Fixture',files=[],selected=set(),phase='queued');job.engine=engine
        download_options.save(self.store, {'pause_downloads':True})
        with patch.object(engine,'_claim_download') as claim:
            engine._launch(job);claim.assert_not_called();self.assertFalse(job.running)
        sock=Mock();sock.recv.return_value=b'article\n'
        reader=_Reader(sock,lambda:not engine._halted(job)); result=[]
        thread=threading.Thread(target=lambda:result.append(reader.line()));thread.start()
        time.sleep(.15);sock.recv.assert_not_called()
        download_options.save(self.store, {'pause_downloads':False})
        thread.join(2);self.assertEqual(result,[b'article'])
        job.user_paused=True
        self.assertTrue(engine._halted(job));self.assertTrue(job.user_paused)

    def test_torrent_active_io_blocks_then_resumes_without_unpausing_job(self):
        engine=SimpleNamespace(_activity_held=lambda *a,**k:False,global_paused=lambda:download_control.paused(self.store),stop=threading.Event(),global_down=Mock(),global_up=Mock())
        engine.global_down.consume.return_value=True
        job=Job(engine,b'x'*20);job.io=True
        download_options.save(self.store, {'pause_downloads':True})
        self.assertFalse(job.io)
        result=[];thread=threading.Thread(target=lambda:result.append(job.take_download(128)));thread.start()
        time.sleep(.15);engine.global_down.consume.assert_not_called()
        download_options.save(self.store, {'pause_downloads':False})
        thread.join(2);self.assertEqual(result,[True]);self.assertTrue(job.io)
        job.user_paused=True
        self.assertFalse(job.take_download(128));self.assertTrue(job.user_paused)

    def test_cancel_endpoint_covers_every_source_and_pending_discover(self):
        self.store.set('storage_base',str(Path(self.tmp.name)/'media'))
        engines={name:Mock() for name in ('nzb','torrent')}
        for engine in engines.values():
            engine.jobs.return_value=[{'id':'active','state':'downloading'},{'id':'pending','state':'queued'},{'id':'manual','state':'paused'},{'id':'saved','state':'stopped'}]
            engine._previews={}
        with self.store.connect() as db:
            db.execute("INSERT INTO downloads(id,title,quality,state,created,updated) VALUES ('queued1','Fixture','720','queued',0,0)")
            db.execute("INSERT INTO m3u_downloads(id,state,created,updated) VALUES ('vod1','queued',0,0)")
        discover_store.insert_request(self.store,dict(idempotency='global-stop',kind='movie',title='Fixture',quality='1080',fallback=False,episode_key='movie:123',body={'externalId':'123'}))
        with patch('backend.acquisition._engine',side_effect=lambda app,store,kind:engines[kind]):
            response=self.client.post('/api/downloads',base_url=fixture.ORIGIN,headers={'Origin':fixture.ORIGIN,'X-CSRF-Token':'test-token'},json={'action':'cancel_all'})
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json['acknowledged'],response.json)
        self.assertEqual(set(response.json['sources']),{'youtube','m3u','nzb','torrent','discover'})
        engines['nzb']._adopt.assert_called_once()
        for engine in engines.values():self.assertEqual([call.args[0] for call in engine.remove.call_args_list],['active','pending','manual'])
        with self.store.connect() as db:
            for table in ('downloads','m3u_downloads','discover_requests'):
                self.assertEqual(db.execute('SELECT state FROM '+table).fetchone()[0],'cancelled')
        self.assertTrue(acquisition.held(self.store))


class VodGlobalPauseTests(unittest.TestCase):
    setUp=vod_fixture.ServerDownloadTests.setUp
    tearDown=vod_fixture.ServerDownloadTests.tearDown
    enqueue=vod_fixture.ServerDownloadTests.enqueue
    response=vod_fixture.ServerDownloadTests.response

    def test_active_transfer_waits_and_resumes_or_cancels(self):
        from backend import m3u_downloads
        for cancel in (False,True):
            job=self.enqueue();payload=b'\x00\x00\x00\x20ftypisom'+b'x'*6000
            download_options.save(self.store,{'pause_downloads':True})
            with patch('backend.m3u_player.upstream',return_value=self.response(payload)):
                thread=threading.Thread(target=m3u_downloads.transfer,args=(self.app,self.store,job,threading.Event()));thread.start()
                def row():
                    with self.store.connect() as db:return dict(db.execute('SELECT * FROM m3u_downloads WHERE id=?',(job['id'],)).fetchone())
                until(lambda:'Paused' in row()['message'])
                partial=next(self.movies.rglob('*.part'));self.assertEqual(partial.stat().st_size,0)
                if cancel:
                    with self.store.connect() as db:db.execute("UPDATE m3u_downloads SET state='cancelling' WHERE id=?",(job['id'],))
                else:download_options.save(self.store,{'pause_downloads':False})
                thread.join(3);self.assertFalse(thread.is_alive());self.assertEqual(row()['state'],'cancelled' if cancel else 'complete')
                if not cancel:
                    Path(row()['destination']).unlink() # disposable fixture output only
