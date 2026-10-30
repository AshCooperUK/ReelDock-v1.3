"""Saved pause and transfer policy apply before any restored torrent is scheduled."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.downloaders.torrent.engine import TorrentEngine
from tests.torrentutil import QUIET_SETTINGS, build_torrent


class TorrentRestartTests(unittest.TestCase):
    def check_restart(self, paused):
        with tempfile.TemporaryDirectory() as temporary, patch.object(TorrentEngine, '_bind'), patch.object(TorrentEngine, '_spawn'):
            root = Path(temporary)
            first = TorrentEngine(root / 'media', root / 'data', settings=QUIET_SETTINGS)
            self.addCleanup(first.shutdown)
            payload = b'a' * 1024 + b'b' * 1024
            preview = first.preview_bytes(build_torrent([('Film.2026.mkv', payload)], piece_length=1024), 'fixture.torrent')
            started = first.start(preview['id'], [0])
            job = first._jobs[started['id']]
            self.assertTrue(job.storage.write_piece(0, payload[:1024]))
            job.user_paused = paused
            job.download_limit_bps = 1234
            job.enable_pex = False
            job.trackers = ['https://fixture.invalid/announce']
            job.categories = {0: 'movie'}
            job.metadata_choice = {0: {'mode': 'manual', 'media_type': 'movie', 'data': {'title': 'Saved choice'}}}
            first._save_job(job)
            checkpoint = root / 'data' / 'torrents' / (job.id + '.json')
            saved = checkpoint.read_bytes()
            first.shutdown()

            scheduled = []
            original = TorrentEngine._schedule_once

            def schedule(engine):
                original(engine)
                restored = engine._jobs.get(job.id)
                if restored is not None:
                    # This runs at the first scheduling opportunity, before
                    # _resume returns or starts the per-job network loops.
                    scheduled.append((restored.user_paused, restored.io))
                    self.assertEqual(restored.user_paused, paused)
                    self.assertEqual(restored.download_limit_bps, 1234)
                    self.assertFalse(restored.enable_pex)
                    self.assertEqual(restored.trackers, job.trackers)
                    self.assertEqual(restored.categories, job.categories)
                    self.assertEqual(restored.metadata_choice, job.metadata_choice)

            with patch.object(TorrentEngine, '_schedule_once', schedule):
                reopened = TorrentEngine(root / 'media', root / 'data', settings=QUIET_SETTINGS)
            self.addCleanup(reopened.shutdown)
            restored = reopened._jobs[job.id]
            self.assertEqual(scheduled, [(paused, not paused)])
            self.assertEqual(restored.storage.have, [True, False])
            self.assertEqual(restored.storage.read_unchecked(0), payload[:1024])
            self.assertEqual(checkpoint.read_bytes(), saved)

    def test_paused_transfer_keeps_pause_and_policy_before_first_schedule(self):
        self.check_restart(True)

    def test_active_transfer_recovers_verified_piece_and_saved_policy(self):
        self.check_restart(False)


if __name__ == '__main__':
    unittest.main()
