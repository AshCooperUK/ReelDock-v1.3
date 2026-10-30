"""Protect publication from release samples and videos below the saved limit."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from backend.downloaders.nzb.engine import NzbEngine, EngineError, _Job

class DurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = NzbEngine(Path(self.tmp.name)/'library', Path(self.tmp.name)/'data')
        self.addCleanup(self.engine.shutdown)

    def files(self, *names):
        paths = [Path(self.tmp.name)/name for name in names]
        for path in paths: path.write_bytes(b'fixture')
        return paths

    def test_release_sample_is_not_a_second_disc(self):
        paths = self.files('episode.sample-group.mkv', 'episode-group.mkv')
        job = _Job(id='fixture', name='Fixture', files=(), selected=set())
        self.engine._describe_media(job, paths)
        self.assertEqual(job.ready, [str(paths[1])])
        self.assertEqual([(item.index,item.path) for item in job.meta.files], [(0, paths[1].name)])
        self.assertTrue(paths[0].exists())

    def test_boundary_and_short_video(self):
        self.engine.update_settings({'min_video_minutes':2})
        paths = self.files('short.mkv', 'full.mkv')
        with patch('backend.downloaders.nzb.engine.subprocess.run', side_effect=[SimpleNamespace(stdout=json.dumps({'format':{'duration':seconds}})) for seconds in [119.9,120]]):
            self.assertEqual(self.engine._filter_videos(paths), [paths[1]])

    def test_unreadable_or_no_qualifying_video_fails(self):
        self.engine.update_settings({'min_video_minutes':2})
        paths = self.files('episode.mkv')
        for response in ['{}', '{"format":{"duration":"NaN"}}', '{"format":{"duration":"60"}}']:
            with patch('backend.downloaders.nzb.engine.subprocess.run', return_value=SimpleNamespace(stdout=response)):
                with self.assertRaises(EngineError): self.engine._filter_videos(paths)
        with patch('backend.downloaders.nzb.engine.subprocess.run', side_effect=subprocess.TimeoutExpired('ffprobe',30)):
            with self.assertRaises(EngineError): self.engine._filter_videos(paths)

    def test_disabled_duration_does_not_probe(self):
        paths = self.files('episode.mkv')
        with patch('backend.downloaders.nzb.engine.subprocess.run') as probe:
            self.assertEqual(self.engine._filter_videos(paths), paths)
            probe.assert_not_called()

    def test_validated_and_persisted_setting(self):
        self.engine.update_settings({'min_video_minutes':2})
        for value in [-1, True, 1.5, 1441]:
            with self.assertRaises(EngineError): self.engine.update_settings({'min_video_minutes':value})
        saved = json.loads((self.engine.data_dir/'settings.json').read_text())
        self.assertEqual(saved['min_video_minutes'],2)

    def test_sample_rule_applies_before_nzb_and_torrent_transfer(self):
        from backend.downloaders.torrent.media import gate
        from backend.downloaders.nzb.files import classify
        names = ['SAMPLE.mkv', 'film.Sample-group.mp4', 'sample.rar', 'samples/clip.mkv', 'sample.par2', 'sample.nfo']
        rows = [(0, 'film.mkv', 1000)] + [(i+1,name,100) for i,name in enumerate(names)]
        for result in [gate(rows).files, classify(rows)[2]]:
            self.assertTrue(result[0].selected)
            for item in result[1:]:
                self.assertFalse(item.selectable)
                self.assertFalse(item.selected)
                self.assertIn('Sample', item.reason)
        self.assertFalse(gate(rows[1:]).accepted)
        self.assertFalse(classify(rows[1:])[0])

    def test_extracted_sample_is_removed_not_published(self):
        from zipfile import ZipFile
        root = self.engine.work_dir/'fixture'
        root.mkdir()
        archive = root/'release.zip'
        with ZipFile(archive,'w') as out:
            out.writestr('sample.mkv', b'sample')
            out.writestr('episode.mkv', b'full episode')
        job = _Job(id='fixture', name='Fixture', files=(), selected=set())
        ready = self.engine._unpack(job, [archive])
        self.assertEqual([p.name for p in ready], ['episode.mkv'])
        self.assertFalse(any(p.name == 'sample.mkv' for p in root.rglob('*')))
