"""Real native save/restore paths retain clear identity without polling Activity."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend import activity_clear
from backend.storage import Store
from backend.downloaders.torrent.engine import TorrentEngine, Job, _restore_metadata
from backend.downloaders.nzb.engine import NzbEngine, _Job, _job_from


class NativeActivityGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.store=Store(self.root/'data');activity_clear.initialise(self.store)

    def fixture(self,kind,added=100):
        if kind=='torrent':
            engine=object.__new__(TorrentEngine);engine.data_dir=self.root/'torrent';engine.data_dir.mkdir(exist_ok=True)
            engine.global_paused=lambda:False
            job=Job(engine,b'a'*20);job.added=added;job.phase='stopped'
            job.meta=SimpleNamespace(name='Fixture torrent',raw=b'private test torrent bytes',files=[],url_list=[])
            path=engine.data_dir/'torrents'/(job.id+'.json')
        else:
            engine=object.__new__(NzbEngine);engine.state_dir=self.root/'nzb';engine.state_dir.mkdir(exist_ok=True)
            engine.global_paused=lambda:False;engine.config={'servers':[],'download_limit_bps':0,'max_active_downloads':1}
            job=_Job('native-id','Fixture NZB',[],set(),added=added,phase='stopped')
            path=engine.state_dir/(job.id+'.json')
        return engine,job,path

    def restore(self,kind,engine,payload):
        if kind=='nzb':return _job_from(payload)
        clone=Job(engine,b'a'*20);clone.phase=payload['phase'];clone.added=payload['added']
        clone.meta=SimpleNamespace(name='Fixture torrent',raw=b'private test torrent bytes',files=[],url_list=[])
        _restore_metadata(clone,payload);return clone

    def test_real_save_terminal_retry_terminal_advances_once_without_activity_poll(self):
        for kind in ('torrent','nzb'):
            with self.subTest(kind=kind):
                engine,job,path=self.fixture(kind);engine._save_job(job);first=json.loads(path.read_text())['activity_generation']
                self.assertGreater(first,0);self.assertLessEqual(first,9007199254740991)
                with self.store.connect() as db:db.execute('INSERT INTO activity_hidden VALUES(?,?,?,1)',(kind,job.id,first))
                job.phase='queued';engine._save_job(job)
                job.phase='download';engine._save_job(job)
                job.phase='stopped';engine._save_job(job)
                saved=json.loads(path.read_text());self.assertEqual(saved['activity_generation'],first+1)
                with self.store.connect() as db:
                    self.assertTrue(activity_clear.is_hidden(db,kind,job.id,first))
                    self.assertFalse(activity_clear.is_hidden(db,kind,job.id,saved['activity_generation']))
                self.assertEqual(engine._job_dict(job)['activity_generation'],first+1)

    def test_passive_saves_public_reads_and_restore_do_not_advance_generation(self):
        for kind in ('torrent','nzb'):
            with self.subTest(kind=kind):
                engine,job,path=self.fixture(kind);engine._save_job(job);first=job.activity_generation
                for n in range(3):
                    job.metadata_note='Updated local metadata '+str(n);engine._job_dict(job);engine._save_job(job)
                    self.assertEqual(job.activity_generation,first)
                restored=self.restore(kind,engine,json.loads(path.read_text()));engine._save_job(restored)
                self.assertEqual(json.loads(path.read_text())['activity_generation'],first)

    def test_legacy_saved_jobs_default_to_zero_and_stay_cleared_after_restore(self):
        for kind in ('torrent','nzb'):
            with self.subTest(kind=kind):
                engine,job,path=self.fixture(kind);engine._save_job(job);legacy=json.loads(path.read_text());legacy.pop('activity_generation')
                with self.store.connect() as db:db.execute('INSERT INTO activity_hidden VALUES(?,?,0,1)',(kind,job.id))
                restored=self.restore(kind,engine,legacy);engine._save_job(restored)
                self.assertEqual(restored.activity_generation,0)
                self.assertEqual(engine._job_dict(restored)['activity_generation'],0)
                with self.store.connect() as db:self.assertTrue(activity_clear.is_hidden(db,kind,job.id,restored.activity_generation))

    def test_readding_same_native_id_after_removal_has_distinct_incarnation(self):
        for kind in ('torrent','nzb'):
            with self.subTest(kind=kind):
                engine,first,path=self.fixture(kind,100);engine._save_job(first)
                with self.store.connect() as db:db.execute('INSERT INTO activity_hidden VALUES(?,?,?,1)',(kind,first.id,first.activity_generation))
                path.unlink();engine,second,path=self.fixture(kind,200);engine._save_job(second)
                self.assertEqual(first.id,second.id);self.assertNotEqual(first.activity_generation,second.activity_generation)
                with self.store.connect() as db:self.assertFalse(activity_clear.is_hidden(db,kind,second.id,second.activity_generation))


if __name__=='__main__':unittest.main()
