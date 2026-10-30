"""Selected NZB payload range: no provider network calls."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from backend.downloaders.nzb.engine import NzbEngine, EngineError

RAW=b'''<nzb><file subject='"Film.mkv" yEnc (1/1)'><groups><group>alt.test</group></groups><segments><segment bytes="5000000" number="1">a@test</segment></segments></file></nzb>'''

class NzbSizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.engine=NzbEngine(Path(self.tmp.name)/'media',Path(self.tmp.name)/'data')
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.engine.shutdown)

    def test_zero_concurrency_is_unlimited_but_pause_still_holds(self):
        from types import SimpleNamespace
        from backend.downloaders.nzb.engine import _Job
        with patch.object(NzbEngine, '_spawn'), tempfile.TemporaryDirectory() as directory:
            engine = NzbEngine(Path(directory)/'media', Path(directory)/'data')
            self.addCleanup(engine.shutdown)
            engine._order = [SimpleNamespace(running=True, removed=False, user_paused=False)]
            job = _Job(id='fixture', name='Fixture', files=(), selected=set())
            with patch.object(engine, '_claim_download', return_value=True), patch.object(engine, '_save_job'), patch.object(engine, '_spawn') as spawn:
                engine.update_settings({'max_active_downloads':1})
                engine._launch(job)
                spawn.assert_not_called()
                engine.update_settings({'max_active_downloads':0})
                engine.global_paused = lambda: True
                engine._launch(job)
                spawn.assert_not_called()
                engine.global_paused = lambda: False
                engine._launch(job)
                spawn.assert_called_once()
                self.assertTrue(job.running)

    def test_bounds_and_atomic_validation(self):
        self.engine.update_settings({'min_download_bytes':5_000_000,'max_download_bytes':100_000_000_000})
        self.assertEqual(self.engine.settings()['max_download_bytes'],100_000_000_000)
        for patch_values in ({'min_download_bytes':100_000_000_001},{'max_download_bytes':4_000_000},{'min_download_bytes':-1},{'min_download_bytes':True},{'max_download_bytes':1.5}):
            with self.assertRaises(EngineError):self.engine.update_settings(patch_values)
        self.assertEqual(self.engine.settings()['min_download_bytes'],5_000_000)
        self.assertEqual(self.engine.settings()['max_download_bytes'],100_000_000_000)

    def test_start_rejects_outside_range_and_accepts_equal_boundary(self):
        with patch.object(self.engine,'_availability',return_value='unchecked'):
            preview=self.engine.preview_bytes(RAW,'Film.nzb')
        self.engine.update_settings({'min_download_bytes':6_000_000})
        with patch.object(self.engine,'_servers',return_value=[{'host':'fixture'}]),patch.object(self.engine,'_launch') as launch:
            with self.assertRaisesRegex(EngineError,'outside the allowed range'):
                self.engine.start(preview['id'],None)
            launch.assert_not_called()
            self.assertEqual(len(self.engine._jobs),0)
            self.engine.update_settings({'min_download_bytes':5_000_000,'max_download_bytes':5_000_000})
            self.engine.start(preview['id'],None)
            launch.assert_called_once()
            job = next(iter(self.engine._jobs.values()))
            self.assertEqual(self.engine._job_dict(job)['total_bytes'], 5_000_000)

    def test_queued_job_is_rechecked_before_transfer(self):
        with patch.object(self.engine,'_availability',return_value='unchecked'):
            preview=self.engine.preview_bytes(RAW,'Film.nzb')
        with patch.object(self.engine,'_servers',return_value=[{'host':'fixture'}]),patch.object(self.engine,'_launch'):
            result=self.engine.start(preview['id'],None)
        job=self.engine._jobs[result['id']]
        self.engine.update_settings({'max_download_bytes':4_000_000})
        with patch.object(self.engine,'_spawn') as spawn:
            self.engine._launch(job)
            spawn.assert_not_called()
        self.assertEqual(job.phase,'failed')
        self.assertIn('outside the allowed range',job.error)
