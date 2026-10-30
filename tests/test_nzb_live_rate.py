"""Displayed native speed expires even when no further article is received."""
import unittest
from unittest.mock import patch

from backend.downloaders.nzb.engine import _Job


class NzbLiveRateTests(unittest.TestCase):
    def test_last_successful_article_cannot_advertise_speed_after_idle_window(self):
        job=_Job(id='isolated',name='Fixture',files=[],selected=set())
        job.pullers=20
        with patch('backend.downloaders.nzb.engine.time.monotonic',return_value=100):job.note(700_000)
        with patch('backend.downloaders.nzb.engine.time.monotonic',return_value=100.2):job.note(700_000)
        with patch('backend.downloaders.nzb.engine.time.monotonic',return_value=102):self.assertGreater(job.down_rate,0)
        # Polling does not call note(). A stalled/finished provider must not keep
        # the last nonzero rate indefinitely while all connections remain open.
        with patch('backend.downloaders.nzb.engine.time.monotonic',return_value=106):self.assertEqual(job.down_rate,0)
        with patch('backend.downloaders.nzb.engine.time.monotonic',return_value=700):self.assertEqual(job.down_rate,0)

    def test_idle_filter_does_not_discard_a_new_completion_or_mutate_samples(self):
        job=_Job(id='isolated',name='Fixture',files=[],selected=set())
        job.samples=[(10,900_000),(100,700_000)]
        with patch('backend.downloaders.nzb.engine.time.monotonic',return_value=101):
            self.assertEqual(job.down_rate,700_000/.2)
        self.assertEqual(job.samples,[(10,900_000),(100,700_000)])

    def test_no_completed_articles_means_zero_payload_rate(self):
        job=_Job(id='isolated',name='Fixture',files=[],selected=set())
        self.assertEqual(job.down_rate,0)

    def test_exact_window_boundary_and_later_expiry(self):
        job=_Job(id='isolated',name='Fixture',files=[],selected=set());job.samples=[(100,500)]
        with patch('backend.downloaders.nzb.engine.time.monotonic',return_value=105):self.assertGreater(job.down_rate,0)
        with patch('backend.downloaders.nzb.engine.time.monotonic',return_value=105.001):self.assertEqual(job.down_rate,0)

    def test_new_completion_resumes_rate_after_idle_without_reviving_old_bytes(self):
        job=_Job(id='isolated',name='Fixture',files=[],selected=set());job.samples=[(1,90_000_000)]
        with patch('backend.downloaders.nzb.engine.time.monotonic',return_value=100):
            self.assertEqual(job.down_rate,0);job.note(700_000)
            self.assertEqual(job.down_rate,700_000/.2)

    def test_restart_recovers_real_completed_parts_when_stage_counter_is_stale(self):
        import tempfile
        import json
        from pathlib import Path
        from backend.downloaders.nzb.engine import NzbEngine
        raw=b'''<nzb><file subject="&quot;Fixture.2026.mkv&quot; yEnc (1/2)">
        <groups><group>fixture</group></groups><segments>
        <segment bytes="3" number="1">first@fixture</segment>
        <segment bytes="3" number="2">second@fixture</segment></segments></file></nzb>'''
        with tempfile.TemporaryDirectory() as temporary,patch.object(NzbEngine,'_spawn'):
            root=Path(temporary);engine=NzbEngine(root/'media',root/'data')
            try:
                preview=engine.preview_bytes(raw,'fixture.nzb',check_availability=False)
                with patch.object(engine,'_servers',return_value=[{}]),patch.object(engine,'_launch'):
                    started=engine.start(preview['id'],[0])
                job=engine._jobs[started['id']];segment=job.files[0].segments[0]
                engine._write_part(job,segment,b'abc','Fixture.2026.mkv',0)
                checkpoint=engine.state_dir/(job.id+'.json')
                self.assertEqual(json.loads(checkpoint.read_text())['done_parts'],0)
                retained={path:path.read_bytes() for path in (engine.work_dir/job.id).rglob('*') if path.is_file()}
                engine.shutdown()
                restored=NzbEngine(root/'media',root/'data')
                try:
                    self.assertEqual(restored._jobs[job.id].done_parts,1)
                    self.assertEqual(restored.job(job.id)['progress'],.5)
                    self.assertEqual({path:path.read_bytes() for path in (engine.work_dir/job.id).rglob('*') if path.is_file()},retained)
                finally:restored.shutdown()
            finally:engine.shutdown()


if __name__=='__main__':unittest.main()
