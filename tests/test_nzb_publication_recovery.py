"""Recover prepared NZB output without NNTP, extraction or stale intent writes."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from backend import acquisition, media_holds
from backend.storage import Store
from backend.downloaders.nzb.engine import NzbEngine, _PublicationRecovery
from backend.downloaders.torrent.rename import companion_path

RAW = b'<nzb><file subject="&quot;Fixture.2026.mkv&quot; yEnc (1/1)"><groups><group>fixture</group></groups><segments><segment bytes="5" number="1">fixture@invalid</segment></segments></file></nzb>'


class NzbPublicationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'data')
        media_holds.initialise(self.store)
        spawn = patch.object(NzbEngine, '_spawn')
        spawn.start()
        self.addCleanup(spawn.stop)
        self.engine = self.new_engine()
        self.engine.media_base.mkdir()
        preview = self.engine.preview_bytes(RAW, 'fixture.nzb', check_availability=False)
        with patch.object(self.engine, '_servers', return_value=[{}]), patch.object(self.engine, '_launch'):
            started = self.engine.start(preview['id'], [0])
        self.job = self.engine._jobs[started['id']]
        self.prepared = self.engine.work_dir / self.job.id / 'media'
        self.prepared.mkdir(parents=True)
        self.video = self.prepared / 'Fixture.2026.mkv'
        self.video.write_bytes(b'prepared-video' * 300)
        self.note = self.prepared / 'Fixture.2026.nfo'
        self.note.write_bytes(b'Original release note')
        self.engine._describe_media(self.job, [self.video, self.note])
        self.job.done_parts = self.job.total_parts
        self.job.phase = 'download'
        self.job.progress_stage = 'publishing'
        self.job.metadata_choice = {0: {'mode': 'manual', 'data': {'title': 'Saved custom choice'}}}
        self.job.metadata_note = 'Original metadata note'
        layout = self.engine.library_layout()
        self.relative = self.engine._primary_relative(self.job, layout, {'movies': layout['movies'], 'series': layout['series']})
        self.target = self.engine.media_base / self.relative
        self.companion_relative = companion_path(self.relative, self.note.name)
        self.companion = self.engine.media_base / self.companion_relative
        self.companion.parent.mkdir(parents=True)
        self.companion.write_bytes(self.note.read_bytes())
        self.job.storage.published[1] = self.companion_relative
        self.engine._save_job(self.job)
        self.checkpoint = self.engine.state_dir / (self.job.id + '.json')
        self.edit(lambda saved: saved.update(custom_saved_metadata={'retain': True}))
        for method in ('_fetch', '_assemble', '_repair', '_unpack', '_describe_media'):
            context = patch.object(NzbEngine, method, side_effect=AssertionError('Publication recovery entered ' + method))
            context.start()
            self.addCleanup(context.stop)

    def new_engine(self):
        engine = NzbEngine(self.root / 'media', self.store.directory / 'nzb-engine')
        engine.config.update(min_video_minutes=0, share_metadata=False)
        self.addCleanup(engine.shutdown)
        return engine

    def edit(self, action):
        value = json.loads(self.checkpoint.read_text())
        action(value)
        self.checkpoint.write_text(json.dumps(value))
        return value

    def recover(self, engine=None, job=None):
        engine = engine or self.engine
        job = job or self.job
        with patch.object(engine, '_spawn', side_effect=lambda action, name: action()):
            engine._launch_publication(job)

    def hold(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            media_holds.hold(db, [self.engine.activity_key(self.job.id), 'native:nzb-job:' + self.job.id], [('nzb', self.job.id)])

    def fingerprint(self, path):
        state = path.stat()
        return state.st_ino, state.st_size, path.read_bytes()

    def assert_complete(self, job=None):
        job = job or self.job
        saved = json.loads(self.checkpoint.read_text())
        self.assertEqual((saved['phase'], saved['progress_stage'], saved['error']), ('stopped', 'complete', ''))
        self.assertEqual(saved['published']['0'], self.relative)
        self.assertEqual(self.target.read_bytes(), self.video.read_bytes())
        self.assertFalse(job.running)
        self.assertIsNone(getattr(job, '_download_lock', None))

    def test_restart_recovers_only_prepared_publication_preserving_companion_and_metadata(self):
        source = self.fingerprint(self.video)
        companion = self.fingerprint(self.companion)
        before = json.loads(self.checkpoint.read_text())
        restored = self.new_engine()
        job = restored._jobs[self.job.id]
        self.assertEqual(job.phase, 'queued')
        with patch.object(restored, '_spawn', side_effect=lambda action, name: action()):
            restored._launch(job)
        self.assert_complete(job)
        self.assertEqual(self.fingerprint(self.video), source)
        self.assertEqual(self.fingerprint(self.companion), companion)
        after = json.loads(self.checkpoint.read_text())
        for field in ('metadata_choice', 'metadata_note', 'custom_saved_metadata', 'files', 'ready', 'media', 'activity_generation'):
            self.assertEqual(after[field], before[field])

    def test_live_orphan_recovery_accepts_nested_prepared_files(self):
        nested = self.prepared / 'disc' / self.video.name
        nested.parent.mkdir()
        self.video.replace(nested)
        self.video = nested
        self.job.ready[0] = str(nested)
        self.engine._save_job(self.job)
        self.recover()
        self.assert_complete()

    def test_restart_recovers_unpublished_empty_nfo_after_video(self):
        self.note.write_bytes(b'')
        self.job.meta.files[1].length = 0
        self.job.storage.published.clear()
        self.companion.unlink()
        self.engine._save_job(self.job)
        sources = self.fingerprint(self.video), self.fingerprint(self.note)
        restored = self.new_engine()
        with patch.object(restored, '_spawn', side_effect=lambda action, name: action()):
            restored._launch(restored._jobs[self.job.id])
        self.assert_complete(restored._jobs[self.job.id])
        saved = json.loads(self.checkpoint.read_text())
        self.assertEqual(saved['published']['1'], self.companion_relative)
        self.assertEqual(self.companion.read_bytes(), b'')
        self.assertEqual((self.fingerprint(self.video), self.fingerprint(self.note)), sources)

    def test_restart_keeps_already_published_empty_nfo_before_video(self):
        self.note.write_bytes(b'')
        self.companion.write_bytes(b'')
        video, note = self.job.meta.files
        video.index, note.index, note.length = 1, 0, 0
        self.job.meta.files = [note, video]
        self.job.ready = [str(self.note), str(self.video)]
        self.job.storage.published = {0: self.companion_relative}
        self.engine._save_job(self.job)
        sources = self.fingerprint(self.video), self.fingerprint(self.note), self.fingerprint(self.companion)
        restored = self.new_engine()
        job = restored._jobs[self.job.id]
        with patch.object(restored, '_spawn', side_effect=lambda action, name: action()):
            restored._launch(job)
        saved = json.loads(self.checkpoint.read_text())
        self.assertEqual((saved['phase'], saved['progress_stage'], saved['error']), ('stopped', 'complete', ''))
        self.assertEqual(saved['published'], {'0': self.companion_relative, '1': self.relative})
        self.assertEqual(self.target.read_bytes(), self.video.read_bytes())
        self.assertEqual((self.fingerprint(self.video), self.fingerprint(self.note), self.fingerprint(self.companion)), sources)
        self.assertFalse(job.running)
        self.assertIsNone(getattr(job, '_download_lock', None))

    def test_zero_length_requires_supported_companion_and_an_actual_integer(self):
        item_type = type(self.job.meta.files[1])
        self.job.storage.published.clear()
        for name, length, accepted in (
                ('Fixture.2026.nfo', 0, True), ('Fixture.2026.srt', 0, True), ('Fixture.2026.jpg', 0, True),
                ('Empty.2026.mkv', 0, False), ('Empty.2026.mp3', 0, False), ('Other.2026.txt', 0, False),
                ('Fixture.2026.nfo', -1, False), ('Fixture.2026.nfo', False, False), ('Fixture.2026.nfo', True, False),
                ('sample.nfo', 0, False), ('../Unsafe.nfo', 0, False)):
            with self.subTest(name=name, length=length, length_type=type(length).__name__):
                source = self.prepared / Path(name).name
                source.write_bytes(b'')
                self.job.meta.files[1] = item_type(1, name, length)
                self.job.ready[1] = str(source)
                payload = json.loads(self.checkpoint.read_text())
                payload.update(ready=self.job.ready, published={}, media=[{'index': item.index, 'path': item.path, 'length': item.length} for item in self.job.meta.files])
                recovery = _PublicationRecovery(self.engine, self.job, payload)
                if accepted:
                    recovery.validate(payload)
                    self.assertTrue(recovery.validated)
                else:
                    with self.assertRaises(ValueError):recovery.validate(payload)
                self.assertFalse(self.target.exists(), 'Validation alone never publishes a destination')

    def test_restart_rejects_coerced_boolean_or_float_companion_lengths(self):
        self.job.storage.published.clear()
        self.companion.unlink()
        for value in (False, True, 0.0):
            with self.subTest(length=value, length_type=type(value).__name__):
                self.note.write_bytes(b'x' if value is True else b'')
                self.job.meta.files[1].length = self.note.stat().st_size
                self.engine._save_job(self.job)
                self.edit(lambda saved: saved['media'][1].update(length=value))
                restored = self.new_engine()
                with patch.object(restored, '_spawn', side_effect=lambda action, name: action()):
                    restored._launch(restored._jobs[self.job.id])
                self.assertEqual(json.loads(self.checkpoint.read_text())['phase'], 'failed')
                self.assertFalse(self.target.exists())
                self.assertFalse(self.companion.exists())

    def test_scheduler_picks_up_a_live_orphan_without_reentering_download(self):
        with patch.object(self.engine.stop, 'wait', side_effect=[False, True]), patch.object(self.engine, '_spawn', side_effect=lambda action, name: action()):
            self.engine._schedule()
        self.assert_complete()

    def test_missing_changed_size_symlink_and_escape_sources_fail_without_publication(self):
        mutations = ('missing', 'size', 'symlink', 'escape')
        original = self.video.read_bytes()
        initial = self.checkpoint.read_bytes()
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.checkpoint.write_bytes(initial)
                self.job.phase = 'download'
                self.job.error = ''
                self.job._publication_after = 0
                self.job.ready[0] = str(self.video)
                self.video.unlink(missing_ok=True)
                self.video.write_bytes(original)
                if mutation == 'missing':
                    self.video.unlink()
                elif mutation == 'size':
                    self.video.write_bytes(b'changed-size')
                else:
                    outside = self.root / 'outside.mkv'
                    outside.write_bytes(original)
                    self.video.unlink()
                    if mutation == 'symlink':
                        self.video.symlink_to(outside)
                    else:
                        escaped = self.prepared / '..' / '..' / '..' / '..' / 'outside.mkv'
                        self.job.ready[0] = str(escaped)
                        self.edit(lambda saved: saved['ready'].__setitem__(0, str(escaped)))
                self.recover()
                self.assertEqual(json.loads(self.checkpoint.read_text())['phase'], 'failed')
                self.assertFalse(self.target.exists())
                self.assertIsNone(getattr(self.job, '_download_lock', None))
        self.video.unlink(missing_ok=True)
        self.video.write_bytes(original)

    def test_mismatched_meta_index_or_published_companion_size_is_not_recovered(self):
        before = self.checkpoint.read_bytes()
        self.job.meta.files[0].index = 9
        self.edit(lambda saved: saved['media'][0].update(index=9))
        self.recover()
        self.assertEqual(json.loads(self.checkpoint.read_text())['phase'], 'failed')
        self.assertFalse(self.target.exists())
        self.job.meta.files[0].index = 0
        self.job.phase = 'download'
        self.job.error = ''
        self.job._publication_after = 0
        self.checkpoint.write_bytes(before)
        self.companion.write_bytes(b'Unexpected edited companion size')
        self.recover()
        self.assertEqual(json.loads(self.checkpoint.read_text())['phase'], 'failed')
        self.assertEqual(self.companion.read_bytes(), b'Unexpected edited companion size')
        self.assertFalse(self.target.exists())

    def test_paused_held_worker_and_active_jobs_never_launch_recovery(self):
        before = self.checkpoint.read_bytes()
        for flag in ('pause', 'running', 'worker', 'hold'):
            with self.subTest(flag=flag):
                self.job.user_paused = flag == 'pause'
                self.job.running = flag == 'running'
                if flag == 'hold':
                    self.hold()
                with patch.dict(os.environ, {'REELDOCK_DOWNLOADS': 'worker' if flag == 'worker' else 'web'}), patch.object(self.engine, '_spawn') as spawn:
                    self.engine._launch_publication(self.job)
                    spawn.assert_not_called()
                self.assertEqual(self.checkpoint.read_bytes(), before)
                self.assertFalse(self.target.exists())
        self.job.running = False

    def test_changed_checkpoint_generation_is_not_rewritten_or_launched(self):
        current = self.edit(lambda saved: saved.update(activity_generation=saved.get('activity_generation', 0) + 1))
        before = self.checkpoint.read_bytes()
        self.recover()
        self.assertEqual(self.checkpoint.read_bytes(), before)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.job.running)
        self.assertIsNone(getattr(self.job, '_download_lock', None))
        self.assertGreater(current['activity_generation'], self.job.activity_generation)

    def test_existing_destination_is_never_replaced_even_when_same_size(self):
        existing = b'x' * self.video.stat().st_size
        self.target.write_bytes(existing)
        before = self.fingerprint(self.target)
        self.recover()
        self.assertEqual(self.fingerprint(self.target), before)
        self.assertEqual(json.loads(self.checkpoint.read_text())['phase'], 'failed')
        self.assertIn('already exists', self.job.error)
        self.assertTrue(self.video.is_file())

    def test_foreign_process_claim_blocks_recovery_until_it_exits(self):
        code = "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX); print('claimed',flush=True); sys.stdin.read(1)"
        child = subprocess.Popen([sys.executable, '-c', code, str(self.engine.state_dir / (self.job.id + '.lock'))], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'claimed')
            before = self.checkpoint.read_bytes()
            self.recover()
            self.assertEqual(self.checkpoint.read_bytes(), before)
            self.assertFalse(self.target.exists())
        finally:
            child.communicate('x', timeout=3)
        self.recover()
        self.assert_complete()

    def test_two_launchers_have_only_one_publication_owner(self):
        other = self.new_engine()
        entered = threading.Event()
        release = threading.Event()
        original = self.engine._publish
        workers = []
        def publish(*args, **kwargs):
            entered.set()
            release.wait(3)
            return original(*args, **kwargs)
        def spawn(action, name):
            thread = threading.Thread(target=action)
            workers.append(thread)
            thread.start()
            return thread
        with patch.object(self.engine, '_publish', side_effect=publish) as publishes, patch.object(self.engine, '_spawn', side_effect=spawn):
            self.engine._launch_publication(self.job)
            self.assertTrue(entered.wait(2))
            try:
                with patch.object(other, '_spawn') as second:
                    other._launch_publication(other._jobs[self.job.id])
                    second.assert_not_called()
            finally:
                release.set()
                for thread in workers:
                    thread.join(3)
            self.assertEqual(publishes.call_count, 1)
        self.assert_complete()

    def test_acquisition_stop_during_copy_keeps_prepared_files_and_no_destination(self):
        self.video.write_bytes(b'v' * (2 * 1024 * 1024 + 7))
        self.job.meta.files[0].length = self.video.stat().st_size
        self.engine._save_job(self.job)
        original = Path.open
        reads = []
        class Incoming:
            def __init__(self, handle):
                self.handle = handle
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.handle.close()
            def read(inner, size):
                data = inner.handle.read(size)
                reads.append(len(data))
                acquisition.begin_hold(self.store)
                return data
        def opening(path, *args, **kwargs):
            handle = original(path, *args, **kwargs)
            return Incoming(handle) if path == self.video else handle
        before = self.checkpoint.read_bytes()
        with patch.object(Path, 'open', opening):
            self.recover()
        self.assertEqual(reads, [1024 * 1024])
        self.assertEqual(self.checkpoint.read_bytes(), before)
        self.assertFalse(self.target.exists())
        self.assertTrue(self.video.exists())

    def test_clear_winning_final_action_fence_creates_no_destination(self):
        original = _PublicationRecovery.publish
        before = self.checkpoint.read_bytes()
        def clear(recovery, *args):
            with media_holds.explicit_action(self.store):
                self.hold()
            return original(recovery, *args)
        with patch.object(_PublicationRecovery, 'publish', clear):
            self.recover()
        self.assertEqual(self.checkpoint.read_bytes(), before)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.glob('*.partial')), [])

    def test_new_checkpoint_generation_before_link_is_never_overwritten(self):
        original = _PublicationRecovery.publish
        changed = []
        def replace(recovery, *args):
            self.edit(lambda saved: saved.update(activity_generation=saved.get('activity_generation', 0) + 1, phase='paused', user_paused=True))
            changed.append(self.checkpoint.read_bytes())
            return original(recovery, *args)
        with patch.object(_PublicationRecovery, 'publish', replace):
            self.recover()
        self.assertEqual(self.checkpoint.read_bytes(), changed[0])
        self.assertFalse(self.target.exists())
        self.assertTrue(self.video.exists())

    def test_metadata_enrichment_during_copy_is_preserved_by_checkpoint_commit(self):
        original = _PublicationRecovery.publish
        def enrich(recovery, *args):
            self.edit(lambda saved: saved.update(metadata_note='Another writer enriched metadata', metadata_done=[1]))
            return original(recovery, *args)
        with patch.object(_PublicationRecovery, 'publish', enrich):
            self.recover()
        self.assert_complete()
        saved = json.loads(self.checkpoint.read_text())
        self.assertEqual(saved['metadata_note'], 'Another writer enriched metadata')
        self.assertEqual(saved['metadata_done'], [1])
        self.assertEqual(saved['custom_saved_metadata'], {'retain': True})

    def test_link_winner_records_publication_before_later_clear_can_acknowledge(self):
        original = os.link
        started = threading.Event()
        acknowledged = threading.Event()
        errors = []
        threads = []
        def clear():
            try:
                started.set()
                with media_holds.explicit_action(self.store):
                    saved = json.loads(self.checkpoint.read_text())
                    self.assertEqual(saved['published']['0'], self.relative)
                    self.assertEqual(self.target.read_bytes(), self.video.read_bytes())
                    self.hold()
                    acknowledged.set()
            except BaseException as exc:
                errors.append(exc)
        def link(*args, **kwargs):
            result = original(*args, **kwargs)
            if Path(args[1]) == self.target:
                worker = threading.Thread(target=clear)
                threads.append(worker)
                worker.start()
                self.assertTrue(started.wait(1))
                self.assertFalse(acknowledged.wait(.05), 'Clear crossed the exclusive publication record fence')
            return result
        with patch.object(os, 'link', side_effect=link):
            self.recover()
            for worker in threads:
                worker.join(3)
        self.assertEqual(errors, [])
        self.assertTrue(acknowledged.is_set())
        self.assertEqual(self.target.read_bytes(), self.video.read_bytes())

    def test_clear_holding_action_can_join_publication_without_a_lock_wait_cycle(self):
        ready = threading.Event()
        attempt = threading.Event()
        waiting = threading.Event()
        errors = []
        workers = []
        original_publish = _PublicationRecovery.publish
        original_action = media_holds.explicit_action
        companion = self.fingerprint(self.companion)
        def publish(recovery, *args):
            ready.set()
            if not attempt.wait(2):
                raise AssertionError('Clear did not take the final action fence')
            return original_publish(recovery, *args)
        @contextmanager
        def action(*args, **kwargs):
            with original_action(*args, **kwargs) as acquired:
                if kwargs.get('blocking') is False and not acquired:
                    waiting.set()
                yield acquired
        def spawn(callback, name):
            def run():
                try:
                    callback()
                except BaseException as exc:
                    errors.append(exc)
            worker = threading.Thread(target=run)
            workers.append(worker)
            worker.start()
            return worker
        with patch.object(_PublicationRecovery, 'publish', publish), patch.object(media_holds, 'explicit_action', action), patch.object(self.engine, '_spawn', side_effect=spawn):
            self.engine._launch_publication(self.job)
            try:
                self.assertTrue(ready.wait(2))
                with original_action(self.store):
                    self.hold()
                    attempt.set()
                    self.assertTrue(waiting.wait(2))
                    # A blocking Action acquisition would hold this worker
                    # alive until remove() times out while still owning Action.
                    self.engine.remove(self.job.id, timeout=.3)
                    self.assertFalse(self.job.worker.is_alive())
            finally:
                attempt.set()
                for worker in workers:
                    worker.join(3)
        self.assertEqual(errors, [])
        self.assertFalse(self.target.exists())
        self.assertFalse(self.checkpoint.exists())
        self.assertEqual(self.fingerprint(self.companion), companion)

    def test_repeated_interruption_has_a_bounded_copy_attempt_budget(self):
        clock = [100.0]
        with patch('backend.downloaders.nzb.engine.time.monotonic', side_effect=lambda: clock[0]), patch.object(self.engine, '_publish', return_value=None) as publish:
            for _ in range(4):
                self.recover()
                clock[0] += 31
        self.assertEqual(publish.call_count, 3)
        self.assertEqual(self.job.phase, 'failed')
        self.assertIn('Prepared files were kept', self.job.error)
        self.assertTrue(self.video.is_file())
        self.assertFalse(self.target.exists())

    def test_metadata_scheduling_error_cannot_hide_completed_recovery(self):
        self.engine.config['share_metadata'] = True
        self.engine.metadata = Mock(side_effect=RuntimeError('Fixture scheduling failure'))
        self.recover()
        self.assert_complete()
        self.engine.metadata.assert_called_once()


if __name__ == '__main__':
    unittest.main()
