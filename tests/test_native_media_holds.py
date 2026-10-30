"""Durable per-content Clear gates real native previews, restore and publication.

All files, SQLite state and saved engine jobs live in TemporaryDirectory. Native
network/thread starters are disabled, not the identity/hold/storage code tested.
"""
import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend import auto_downloads, media_holds
from backend.downloaders.torrent.engine import TorrentEngine, EngineError as TorrentError
from backend.downloaders.nzb.engine import NzbEngine, EngineError as NzbError
from tests import test_engine_settings as fixture
from tests.test_engine_settings import ORIGIN
from tests.torrentutil import build_torrent


def nzb_bytes(message='article@fixture', title='Film.2026.mkv', second=False):
    extra = ('<file subject="&quot;Other.2026.mkv&quot; yEnc (1/1)"><groups><group>fixture</group></groups>'
             '<segments><segment bytes="12" number="1">other@fixture</segment></segments></file>') if second else ''
    return (f'<nzb><file subject="&quot;{title}&quot; yEnc (1/1)"><groups><group>fixture</group></groups>'
            f'<segments><segment bytes="12" number="1">{message}</segment></segments></file>{extra}</nzb>').encode()


class NativeMediaHoldTests(unittest.TestCase):
    setUp = fixture.EngineSettingsTests.setUp

    def engine(self, kind):
        root = Path(self.tmp.name)
        if kind == 'torrent':
            for method in ('_bind', '_spawn'):
                patcher = patch.object(TorrentEngine, method);patcher.start();self.addCleanup(patcher.stop)
            engine = TorrentEngine(root/'media', self.store.directory/'torrent', settings={
                'enable_dht':False, 'enable_lsd':False, 'enable_upnp':False, 'enable_natpmp':False})
        else:
            patcher=patch.object(NzbEngine, '_spawn');patcher.start();self.addCleanup(patcher.stop)
            engine = NzbEngine(root/'media', self.store.directory/'nzb')
            patcher=patch.object(engine, '_availability',return_value='');patcher.start();self.addCleanup(patcher.stop)
            patcher=patch.object(engine, '_servers', return_value=[{'host':'fixture.invalid'}]);patcher.start();self.addCleanup(patcher.stop)
            # Actual start persists the parsed job; payload workers stay offline.
            patcher=patch.object(engine, '_launch');patcher.start();self.addCleanup(patcher.stop)
        self.addCleanup(engine.shutdown)
        return engine

    def preview(self, engine, kind, *, different=False, filename='renamed'):
        payload=(build_torrent([('Film.2026.mkv', b'other' if different else b'payload')],name='Film')
                 if kind=='torrent' else nzb_bytes('other-id@fixture' if different else 'article@fixture'))
        return engine.preview_bytes(payload, filename+'.'+('torrent' if kind=='torrent' else 'nzb'))

    def hold(self, kind, key, job_id=None):
        media_holds.initialise(self.store)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            media_holds.hold(db,[key]+(['native:'+kind+'-job:'+job_id] if job_id else []),[(kind,job_id or 'fixture')])

    def post(self, path, body, csrf='test-token', client=None):
        return (client or self.client).post(path,base_url=ORIGIN,json=body,
            headers={'Origin':ORIGIN,'X-CSRF-Token':csrf})

    def test_content_identity_survives_preview_name_and_job_uuid_but_not_changed_payload(self):
        for kind in ('torrent','nzb'):
            with self.subTest(kind=kind):
                engine=self.engine(kind);first=self.preview(engine,kind);again=self.preview(engine,kind,filename='completely-different-title')
                other=self.preview(engine,kind,different=True)
                key=engine.preview_activity_key(first['id'],[0])
                self.assertEqual(key,engine.preview_activity_key(again['id'],[0]))
                self.assertNotEqual(key,engine.preview_activity_key(other['id'],[0]))
                self.assertNotIn('Film',key);self.assertNotIn('article@fixture',key)
                one=engine.start(first['id'],[0]);self.assertEqual(engine.activity_key(one['id']),key)
                if kind=='nzb':
                    two=engine.start(again['id'],[0]);self.assertNotEqual(one['id'],two['id'])
                    self.assertEqual(engine.activity_key(two['id']),key)

    def test_nzb_selected_articles_not_title_or_file_order_define_hold(self):
        engine=self.engine('nzb');one=engine.preview_bytes(nzb_bytes(second=True),'release.nzb')
        selected=engine.preview_activity_key(one['id'],[0]);other=engine.preview_activity_key(one['id'],[1])
        both=engine.preview_activity_key(one['id'],[0,1])
        self.assertNotEqual(selected,other);self.assertNotEqual(selected,both)
        renamed=engine.preview_bytes(nzb_bytes(title='Another.title.2026.mkv'),'other.nzb')
        self.assertEqual(selected,engine.preview_activity_key(renamed['id'],[0]))
        self.hold('nzb',selected)
        with self.assertRaises(NzbError):engine.start(one['id'],[0])
        allowed=engine.start(one['id'],[1]);self.assertEqual(engine.activity_key(allowed['id']),other)

    def test_nzb_restore_identity_uses_original_articles_not_extracted_output_indexes(self):
        engine=self.engine('nzb');preview=engine.preview_bytes(nzb_bytes(second=True),'archive.nzb')
        key=engine.preview_activity_key(preview['id'],[1]);started=engine.start(preview['id'],[1]);job=engine._jobs[started['id']]
        # An archive's extracted video list is renumbered independently of its
        # selected original article-file indexes. The private saved files retain
        # precisely those original selected articles.
        job.selected={0};engine._save_job(job)
        self.hold('nzb',key,job.id);restored=self.engine('nzb')
        self.assertEqual(restored.activity_key(job.id),key);self.assertTrue(restored._jobs[job.id].user_paused)
        from backend.downloaders.nzb.engine import checkpoint_activity_key
        payload=json.loads((engine.state_dir/(job.id+'.json')).read_text());before=json.dumps(payload,sort_keys=True)
        with patch('backend.downloaders.nzb.engine.NzbEngine',side_effect=AssertionError('identity created engine')):
            self.assertEqual(checkpoint_activity_key(payload),key)
        self.assertEqual(json.dumps(payload,sort_keys=True),before)
        with self.assertRaises(NzbError):checkpoint_activity_key({'id':'bad','files':[]})

    def test_torrent_publication_rechecks_hold_before_each_ready_file(self):
        engine=self.engine('torrent');payload=build_torrent([('First.2026.mkv',b'aaa'),('Second.2026.mkv',b'bbb')],name='Two movies')
        preview=engine.preview_bytes(payload,'two.torrent');started=engine.start(preview['id'],[0,1]);job=engine._jobs[started['id']]
        key=engine.activity_key(job.id);storage=job.storage;storage.verified=[True]*job.meta.piece_count
        placed=[]
        def place(item,relative,**kwargs):
            placed.append(item.index)
            self.hold('torrent',key,job.id)
            return relative
        with patch.object(storage,'_place',side_effect=place):job.publish()
        self.assertEqual(placed,[0]);self.assertEqual(list(storage.published),[0])

    def test_torrent_mid_copy_clear_removes_only_owned_unpublished_output(self):
        engine=self.engine('torrent');body=b'v'*(2*1024*1024+37)
        preview=engine.preview_bytes(build_torrent([('Film.2026.mkv',body)],name='Film',piece_length=1024*1024),'film.torrent')
        started=engine.start(preview['id'],[0]);job=engine._jobs[started['id']];storage=job.storage
        for index in range(job.meta.piece_count):self.assertTrue(storage.write_piece(index,body[index*1024*1024:(index+1)*1024*1024]))
        kept=engine.media_base/'Movies'/'Existing.2025.mkv';kept.parent.mkdir(parents=True,exist_ok=True);kept.write_bytes(b'kept')
        key=engine.activity_key(job.id);real_write=os.write;copied=[]
        def write(descriptor,data):
            size=real_write(descriptor,data);copied.append(size)
            self.assertGreater(job.writers,0)
            self.hold('torrent',key,job.id)
            return size
        with patch('backend.downloaders.torrent.storage.os.link',side_effect=OSError('force cross-device copy')),patch('backend.downloaders.torrent.storage.os.write',side_effect=write):job.publish()
        self.assertEqual(len(copied),1);self.assertEqual(storage.published,{})
        self.assertEqual(kept.read_bytes(),b'kept');self.assertEqual(job.writers,0);self.assertEqual(job.error,'')
        self.assertFalse(any(path.name=='Film (2026).mkv' for path in (engine.media_base/'Movies').rglob('*.mkv')))

    def test_nzb_mid_copy_clear_does_not_replace_or_publish_video(self):
        from backend.downloaders.nzb.engine import _place
        engine=self.engine('nzb');preview=self.preview(engine,'nzb');started=engine.start(preview['id'],[0]);job=engine._jobs[started['id']]
        key=engine.activity_key(job.id);root=Path(self.tmp.name);source=root/'ready.mkv';source.write_bytes(b'v'*(2*1024*1024+7))
        target=engine.media_base/'Movies'/'Film (2026)'/'Film (2026).mkv';target.parent.mkdir(parents=True)
        kept=target.parent/'Existing.mkv';kept.write_bytes(b'kept');real_open=Path.open;reads=[]
        class Incoming:
            def __init__(self,handle):self.handle=handle
            def __enter__(self):return self
            def __exit__(self,*args):self.handle.close()
            def read(inner,limit):
                chunk=inner.handle.read(limit);reads.append(len(chunk));self.hold('nzb',key,job.id);return chunk
        def opened(path,*args,**kwargs):
            handle=real_open(path,*args,**kwargs)
            return Incoming(handle) if path==source and args==('rb',) else handle
        def allowed(*,fresh=False):return not engine._activity_held(job,fresh=fresh)
        with patch.object(Path,'open',opened):result=_place(engine.media_base,source,target.relative_to(engine.media_base).as_posix(),allowed=allowed)
        self.assertIsNone(result);self.assertEqual(reads,[1024*1024]);self.assertFalse(target.exists())
        self.assertFalse(list(target.parent.glob('.*reeldock-partial')));self.assertEqual(kept.read_bytes(),b'kept')
        self.assertEqual(source.stat().st_size,2*1024*1024+7)

    def test_held_torrent_can_be_explicitly_resumed_after_metadata_only_restore(self):
        engine=self.engine('torrent');preview=self.preview(engine,'torrent');started=engine.start(preview['id'],[0]);job=engine._jobs[started['id']]
        job.categories={0:'movie'};job.download_limit_bps=3456;job.metadata_note='Keep saved note';engine._save_job(job)
        key=engine.activity_key(job.id);self.hold('torrent',key,job.id);restored=self.engine('torrent')
        self.assertIsNone(restored._jobs[job.id].storage)
        with patch('backend.torrents._engine',return_value=restored):
            response=self.post('/api/torrents/'+job.id,{'action':'resume'})
        self.assertEqual(response.status_code,200,response.json);active=restored._jobs[job.id]
        self.assertIsNotNone(active.storage);self.assertFalse(active.user_paused)
        self.assertEqual(active.categories,{0:'movie'});self.assertEqual(active.download_limit_bps,3456)
        self.assertEqual(active.metadata_note,'Keep saved note');self.assertEqual(len(restored.jobs()),1)
        self.assertFalse(media_holds.blocked(self.store,[key,'native:torrent-job:'+job.id]))

    def test_held_start_has_no_payload_storage_or_job_and_unrelated_start_continues(self):
        for kind,error in (('torrent',TorrentError),('nzb',NzbError)):
            with self.subTest(kind=kind):
                engine=self.engine(kind);preview=self.preview(engine,kind);key=engine.preview_activity_key(preview['id'],[0]);self.hold(kind,key)
                before=set((Path(self.tmp.name)/'media').rglob('*'))
                with self.assertRaises(error):engine.start(preview['id'],[0])
                self.assertEqual(engine.jobs(),[]);self.assertEqual(set((Path(self.tmp.name)/'media').rglob('*')),before)
                other=self.preview(engine,kind,different=True);job=engine.start(other['id'],[0])
                self.assertEqual(len(engine.jobs()),1);self.assertNotEqual(engine.activity_key(job['id']),key)
                self.assertTrue(media_holds.blocked(self.store,key))

    def test_held_restore_retains_identity_without_storage_prepare_or_transfer(self):
        for kind in ('torrent','nzb'):
            with self.subTest(kind=kind):
                engine=self.engine(kind);preview=self.preview(engine,kind);started=engine.start(preview['id'],[0]);job=engine._jobs[started['id']]
                job.user_paused=True;job.downloaded=42;job.categories={0:'movie'};job.added=1_700_000_001
                if kind=='torrent':job.download_limit_bps=1234;job.metadata_note='Keep metadata'
                engine._save_job(job);key=engine.activity_key(job.id)
                # Before factory creation only the exact job-ID hold is known.
                self.hold(kind,'native:'+kind+'-job:'+job.id,job.id)
                saved={p:p.read_bytes() for p in engine.data_dir.rglob('*') if p.is_file()}
                storage_before={p:p.read_bytes() for p in engine.media_base.rglob('*') if p.is_file()}
                with patch('backend.downloaders.torrent.engine.Storage.prepare',side_effect=AssertionError('held restore prepared media')):
                    restored=self.engine(kind)
                self.assertEqual(restored.activity_key(job.id),key);restored_job=restored._jobs[job.id]
                self.assertTrue(restored_job.user_paused);self.assertEqual(restored_job.categories,{0:'movie'})
                self.assertEqual(restored_job.added,1_700_000_001)
                if kind=='torrent':
                    self.assertIsNone(restored_job.storage);self.assertEqual(restored_job.download_limit_bps,1234)
                    self.assertEqual(restored_job.metadata_note,'Keep metadata');self.assertFalse(restored_job.io)
                else:self.assertTrue(restored._halted(restored_job));self.assertFalse(restored_job.running)
                self.assertEqual({p:p.read_bytes() for p in engine.data_dir.rglob('*') if p.is_file()},saved)
                self.assertEqual({p:p.read_bytes() for p in engine.media_base.rglob('*') if p.is_file()},storage_before)

    def test_ongoing_and_publication_honor_content_and_job_holds(self):
        for kind,error in (('torrent',TorrentError),('nzb',NzbError)):
            with self.subTest(kind=kind):
                engine=self.engine(kind);preview=self.preview(engine,kind);started=engine.start(preview['id'],[0]);job=engine._jobs[started['id']]
                key=engine.activity_key(job.id);self.hold(kind,key,job.id)
                with self.assertRaises(error):engine.update(job.id,{'paused':False})
                if kind=='torrent':
                    job.io=True;self.assertFalse(job.io);self.assertFalse(job.wait_for_resume())
                    with patch.object(job.storage,'publish_ready') as publish:job.publish();publish.assert_not_called()
                else:
                    self.assertTrue(engine._halted(job))
                    with patch('backend.downloaders.nzb.engine._place') as place:engine._publish(job);place.assert_not_called()
                self.assertTrue(media_holds.blocked(self.store,key))

    def test_only_authenticated_csrf_exact_resume_releases_native_hold(self):
        for kind in ('torrent','nzb'):
            with self.subTest(kind=kind):
                engine=self.engine(kind);preview=self.preview(engine,kind);started=engine.start(preview['id'],[0]);job_id=started['id'];key=engine.activity_key(job_id)
                self.hold(kind,key,job_id);unrelated='native:'+kind+':unrelated';self.hold(kind,unrelated,'other')
                path=('/api/torrents/' if kind=='torrent' else '/api/nzb/')+job_id
                with patch('backend.'+('torrents' if kind=='torrent' else 'nzb')+'._engine',return_value=engine):
                    self.assertEqual(self.post(path,{'action':'resume'},client=self.app.test_client()).status_code,401)
                    self.assertEqual(self.post(path,{'action':'resume'},csrf='wrong').status_code,403)
                    self.assertTrue(media_holds.blocked(self.store,key))
                    self.assertEqual(self.post(path,{'action':'pause'}).status_code,200);self.assertTrue(media_holds.blocked(self.store,key))
                    result=self.post(path,{'action':'resume'});self.assertEqual(result.status_code,200,result.json)
                self.assertFalse(media_holds.blocked(self.store,[key,'native:'+kind+'-job:'+job_id]))
                self.assertTrue(media_holds.blocked(self.store,unrelated));self.assertFalse(engine._jobs[job_id].user_paused)

    def test_explicit_start_releases_only_valid_selected_preview(self):
        for kind in ('torrent','nzb'):
            with self.subTest(kind=kind):
                engine=self.engine(kind);preview=self.preview(engine,kind);key=engine.preview_activity_key(preview['id'],[0]);self.hold(kind,key)
                path='/api/torrents' if kind=='torrent' else '/api/nzb'
                with patch('backend.'+('torrents' if kind=='torrent' else 'nzb')+'._engine',return_value=engine):
                    body={'action':'start','preview_id':preview['id'],'files':[0]}
                    self.assertEqual(self.post(path,body,csrf='wrong').status_code,403)
                    self.assertEqual(self.post(path,{**body,'files':[999]}).status_code,400)
                    self.assertTrue(media_holds.blocked(self.store,key))
                    response=self.post(path,body);self.assertEqual(response.status_code,200,response.json)
                self.assertFalse(media_holds.blocked(self.store,key));self.assertEqual(engine.activity_key(response.json['id']),key)

    def test_watched_native_hold_precedes_optional_metadata_and_never_releases(self):
        for kind in ('torrent','nzb'):
            with self.subTest(kind=kind):
                engine=self.engine(kind);preview=self.preview(engine,kind);self.hold(kind,engine.preview_activity_key(preview['id'],[0]))
                payload=build_torrent([('Film.2026.mkv',b'payload')],name='Film') if kind=='torrent' else nzb_bytes()
                module='backend.'+('torrents' if kind=='torrent' else 'nzb')
                with patch(module+'._engine',return_value=engine),patch(module+'._with_found_title') as lookup,patch.object(engine,'start') as start:
                    if kind=='nzb':engine._availability.reset_mock()
                    with self.assertRaises(auto_downloads._HeldSource):
                        getattr(auto_downloads,'start_'+kind)(self.app,self.store,payload,'renamed.'+kind)
                    lookup.assert_not_called();start.assert_not_called()
                    if kind=='nzb':engine._availability.assert_not_called()
                self.assertEqual(engine.jobs(),[])

    def test_watched_native_late_hold_remains_held_instead_of_failed_retry(self):
        for kind in ('torrent','nzb'):
            with self.subTest(kind=kind):
                engine=self.engine(kind);preview=self.preview(engine,kind);key=engine.preview_activity_key(preview['id'],[0])
                payload=build_torrent([('Film.2026.mkv',b'payload')],name='Film') if kind=='torrent' else nzb_bytes()
                def metadata(store,engine,preview):
                    self.hold(kind,key)
                    return preview
                module='backend.'+('torrents' if kind=='torrent' else 'nzb')
                with patch(module+'._engine',return_value=engine),patch(module+'._with_found_title',side_effect=metadata):
                    with self.assertRaises(auto_downloads._HeldSource):getattr(auto_downloads,'start_'+kind)(self.app,self.store,payload,'renamed.'+kind)
                self.assertEqual(engine.jobs(),[]);self.assertTrue(media_holds.blocked(self.store,key))

    def test_watched_youtube_hold_prevents_metadata_or_enqueue_or_activity(self):
        video_id='abc12345678'
        for key in ('video:'+video_id,'managed:video:'+video_id):
            self.hold('youtube',key)
            with patch('backend.downloads.enqueue') as enqueue,patch('backend.auto_downloads._save_youtube') as save:
                with self.assertRaises(auto_downloads._HeldSource):
                    auto_downloads.start_youtube(self.app,self.store,video_id,'renamed.strm',threading.Event())
                enqueue.assert_not_called();save.assert_not_called()
            with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM activity_tasks').fetchone()[0],0)
            self.assertTrue(media_holds.blocked(self.store,key));media_holds.resume(self.store,key)

    def test_watch_held_receipt_is_not_timed_retry_or_new_activity_failure(self):
        root=Path(self.tmp.name);watch=root/'watch';watch.mkdir();self.store.set('auto_download_folder',str(watch));self.store.set('storage_base',str(root/'media'))
        self.store.set('auto_downloads','true')
        from backend.download_options import save
        save(self.store,{'auto_downloads':True})
        for name in ('held.nzb','unrelated.nzb'):
            path=watch/name;path.write_bytes(nzb_bytes());os.utime(path,(time.time()-30,)*2)
        calls=[]
        def intake(app,store,data,name):
            calls.append(name)
            if name=='held.nzb':raise auto_downloads._HeldSource('Cleared media requires explicit resume.')
        with patch('backend.auto_downloads.watch_folder',return_value=watch):
            auto_downloads.scan(self.app,self.store,threading.Event(),{'nzb'},nzb=intake)
            with self.store.connect() as db:db.execute("UPDATE auto_downloads SET updated=0")
            auto_downloads.scan(self.app,self.store,threading.Event(),{'nzb'},nzb=intake)
        self.assertEqual(sorted(calls),['held.nzb','unrelated.nzb']);self.assertTrue((watch/'held.nzb').is_file())
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM auto_downloads WHERE name='held.nzb'").fetchone()[0],'held')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM activity_tasks WHERE state='failed'").fetchone()[0],0)


if __name__=='__main__':unittest.main()
