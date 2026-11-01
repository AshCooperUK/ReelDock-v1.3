"""Disposable library fixtures; no installed libraries or external services."""

import copy
import fcntl
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from backend import library_optimise as opt


class FixtureStore:
    def __init__(self, path):
        self.directory = Path(path)
        self.directory.mkdir()
        self.values = {}

    def get(self, key, default=''):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def reset_pending(self):
        return False

    def background_operation(self):
        return nullcontext()


def probe_fixture():
    return dict(streams=[dict(codec_type='video', codec_name='mpeg4', width=160, height=90, pix_fmt='yuv420p',
                              sample_aspect_ratio='1:1', display_aspect_ratio='16:9', disposition={'default': 0}, tags={'title': 'Fixture video'}),
                         dict(codec_type='audio', codec_name='aac', channels=2, sample_rate='48000',
                              disposition={'default': 1}, tags={'language': 'eng', 'title': 'Fixture audio'}),
                         dict(codec_type='subtitle', codec_name='subrip', disposition={'forced': 1}, tags={'language': 'eng'})],
                format=dict(duration='3.0', tags={'title': 'Fixture title'}),
                chapters=[dict(start_time='0.0', end_time='3.0', tags={'title': 'Fixture chapter'})])


class LibraryOptimiseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.movies = self.base / 'movies'
        self.tv = self.base / 'tv'
        self.movies.mkdir()
        self.tv.mkdir()
        self.store = FixtureStore(self.base / 'data')
        self.app = Flask(__name__)
        self.app.config.update(REELDOCK_TV_LIBRARY=str(self.tv), REELDOCK_MOVIE_LIBRARY=str(self.movies),
                               REELDOCK_TV_OUTPUT=str(self.tv), REELDOCK_MOVIE_OUTPUT=str(self.movies))
        self.source = self.movies / 'Fixture.mkv'
        self.source.write_bytes(b'disposable fixture input' * 1000)
        self.original = self.source.read_bytes()
        self.before = probe_fixture()
        self.after = copy.deepcopy(self.before)
        self.after['streams'][0]['codec_name'] = 'hevc'
        self.stop = threading.Event()

    def enqueue(self, path='Fixture.mkv', profile='hevc', stereo=False, limit1080=False):
        opt.configure(self.store, True)
        with patch.object(opt.shutil, 'which', return_value='/fixture/tool'):
            job_id = opt.enqueue(self.app, self.store, 'movies', path, profile, stereo=stereo, limit1080=limit1080)
        # These original engine tests exercise frozen, pre-adjacent AppData
        # plans. New default/destination/publication contracts have separate
        # tests; retain the old worker's exact persisted plan in this fixture.
        with opt._db(self.store) as db:
            job = dict(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
            identity = job['root'] + '\0' + job['path'] + '\0' + job['signature']
            if stereo or limit1080:
                import json
                identity += '\0' + json.dumps(dict(stereo=stereo, limit1080=limit1080), sort_keys=True, separators=(',', ':'))
            db.execute("UPDATE jobs SET destination_mode='appdata',destination_name='',fingerprint=? WHERE id=?", (hashlib.sha256(identity.encode()).hexdigest(), job_id))
            db.execute('DELETE FROM publication_requests WHERE job_id=?', (job_id,))
        return job_id

    def job(self, job_id):
        with opt._db(self.store) as db:
            return dict(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def run_fixture(self, encoder=None, after=None):
        def run(command, checkpoint, timeout, descriptors=(), capture=False, progress=None):
            checkpoint()
            if progress:
                progress(dict(processed_seconds=1.5, fps=12, speed=2))
            if command[0] == 'ffmpeg' and command[-1] != '-':
                if encoder:
                    encoder(command, checkpoint)
                else:
                    Path(command[-1]).write_bytes(b'disposable encoded fixture')
            checkpoint()
        with patch.object(opt, '_probe', side_effect=[self.before, after or self.after]), patch.object(opt, '_run', side_effect=run):
            return opt.run_once(self.app, self.store, self.stop)

    def test_default_is_disabled_and_listing_is_explicit_and_nonrecursive(self):
        (self.movies / 'Show').mkdir()
        (self.movies / 'Show' / 'Episode.mkv').write_bytes(b'fixture')
        with patch.object(opt, 'scan') as scan:
            status = opt.status(self.app, self.store)
        self.assertFalse(status['enabled'])
        scan.assert_not_called()
        with self.assertRaises(opt.OptimiseError):
            opt.enqueue(self.app, self.store, 'movies', 'Fixture.mkv', 'hevc')
        listing = opt.scan(self.app, self.store, 'movies')
        self.assertEqual(listing['folders'], ['Show'])
        self.assertEqual([item['path'] for item in listing['files']], ['Fixture.mkv'])

    def test_explicit_folder_sizes_sum_all_regular_descendants_without_payload_reads(self):
        show = self.movies / 'Show'
        season = show / 'Season 1'
        season.mkdir(parents=True)
        (show / 'poster.jpg').write_bytes(b'art fixture')
        (show / '.metadata').write_bytes(b'hidden fixture')
        (season / 'Episode.mkv').write_bytes(b'episode fixture')
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'Large.mkv').write_bytes(b'outside fixture' * 100)
        (show / 'file-link.mkv').symlink_to(outside / 'Large.mkv')
        (show / 'folder-link').symlink_to(outside, target_is_directory=True)
        (self.movies / 'Empty').mkdir()
        expected = len(b'art fixture') + len(b'hidden fixture') + len(b'episode fixture')
        with patch.object(opt.os, 'open', wraps=os.open) as opened:
            listing = opt.scan(self.app, self.store, 'movies')
        self.assertEqual(listing['folderSizes']['Show'], dict(bytes=expected, complete=True, reason=''))
        self.assertEqual(listing['folderSizes']['Empty'], dict(bytes=0, complete=True, reason=''))
        self.assertEqual(listing['files'][0]['bytes'], len(self.original))
        self.assertTrue(all(call.args[1] & os.O_DIRECTORY for call in opened.call_args_list), 'size listing may open directories, never media payloads')
        self.assertEqual(listing['folders'], ['Empty', 'Show'], 'existing folders string[] contract remains intact')

    def test_folder_size_entry_and_time_budgets_are_shared_and_mark_unknown_totals(self):
        for name in ('A', 'B'):
            (self.movies / name).mkdir()
            (self.movies / name / 'file.bin').write_bytes(b'fixture')
        with patch.object(opt, 'MAX_SIZE_ENTRIES', 1):
            listing = opt.scan(self.app, self.store, 'movies')
        self.assertEqual(listing['folderSizes']['A'], dict(bytes=7, complete=True, reason=''))
        self.assertEqual(listing['folderSizes']['B'], dict(bytes=None, complete=False, reason='limit'))
        (self.movies / 'A' / 'second.bin').write_bytes(b'second fixture')
        with patch.object(opt, 'MAX_SIZE_ENTRIES', 1):
            listing = opt.scan(self.app, self.store, 'movies')
        self.assertFalse(listing['folderSizes']['A']['complete'])
        self.assertGreater(listing['folderSizes']['A']['bytes'], 0)
        self.assertEqual(listing['folderSizes']['A']['reason'], 'limit')
        with patch.object(opt, 'MAX_SIZE_SECONDS', 0):
            listing = opt.scan(self.app, self.store, 'movies')
        self.assertTrue(all(size['bytes'] is None and not size['complete'] for size in listing['folderSizes'].values()))

    def test_unreadable_and_deep_folder_sizes_are_partial_instead_of_false_zero(self):
        show = self.movies / 'Show'
        blocked = show / 'blocked'
        blocked.mkdir(parents=True)
        (show / 'known.bin').write_bytes(b'fixture')
        (blocked / 'unknown.bin').write_bytes(b'not counted')
        real_open = os.open
        def guarded_open(path, *args, **kwargs):
            if path == 'blocked':
                raise PermissionError('Fixture permission failure')
            return real_open(path, *args, **kwargs)
        with patch.object(opt.os, 'open', side_effect=guarded_open):
            listing = opt.scan(self.app, self.store, 'movies')
        self.assertEqual(listing['folderSizes']['Show'], dict(bytes=7, complete=False, reason='unreadable'))
        with patch.object(opt, 'MAX_SIZE_DEPTH', 1):
            listing = opt.scan(self.app, self.store, 'movies')
        self.assertEqual(listing['folderSizes']['Show'], dict(bytes=7, complete=False, reason='depth'))
        (show / 'known.bin').unlink()
        with patch.object(opt.os, 'open', side_effect=guarded_open):
            listing = opt.scan(self.app, self.store, 'movies')
        self.assertEqual(listing['folderSizes']['Show'], dict(bytes=None, complete=False, reason='unreadable'))

    def test_unsafe_paths_and_symbolic_links_are_never_followed(self):
        outside = self.base / 'outside.mkv'
        outside.write_bytes(b'private fixture')
        (self.movies / 'link.mkv').symlink_to(outside)
        (self.movies / 'linkdir').symlink_to(self.tv, target_is_directory=True)
        for path in ('../outside.mkv', '/etc/passwd', '.hidden/movie.mkv', 'link.mkv', 'linkdir/file.mkv'):
            with self.subTest(path=path), self.assertRaises(opt.OptimiseError):
                self.enqueue(path)
        self.assertNotIn('linkdir', opt.scan(self.app, self.store, 'movies')['folders'])
        with self.assertRaises(opt.OptimiseError):
            opt.scan(self.app, self.store, 'movies', 'linkdir')

    def test_symbolic_links_in_absolute_root_ancestors_are_rejected(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.base, target_is_directory=True)
        self.app.config['REELDOCK_MOVIE_LIBRARY'] = str(alias / 'movies')
        with self.assertRaises(opt.OptimiseError):
            self.enqueue()
        self.app.config['REELDOCK_MOVIE_LIBRARY'] = str(self.movies)
        self.app.config['REELDOCK_OPTIMISE_OUTPUT'] = str(alias / 'output')
        with self.assertRaises(opt.OptimiseError):
            self.enqueue()
        self.assertFalse((self.base / 'output').exists())

    def test_scan_and_queue_are_bounded_and_duplicate_enqueues_keep_identity(self):
        self.assertEqual(self.enqueue(), self.enqueue())
        with patch.object(opt, 'MAX_SCAN', 1):
            (self.movies / 'Second.mkv').write_bytes(b'fixture')
            self.assertTrue(opt.scan(self.app, self.store, 'movies')['truncated'])
        with patch.object(opt, 'MAX_QUEUE', 1), self.assertRaises(opt.OptimiseError):
            self.enqueue('Second.mkv')

    def test_option_combinations_have_distinct_durable_identity_and_retry_choices(self):
        ids = {}
        for stereo in (False, True):
            for limit1080 in (False, True):
                job_id = self.enqueue(stereo=stereo, limit1080=limit1080)
                self.assertEqual(job_id, self.enqueue(stereo=stereo, limit1080=limit1080))
                ids[stereo, limit1080] = job_id
                job = self.job(job_id)
                self.assertEqual((job['stereo'], job['limit1080']), (int(stereo), int(limit1080)))
        self.assertEqual(len(set(ids.values())), 4)
        original = self.job(ids[False, False])
        legacy_identity = str(self.movies) + '\0Fixture.mkv\0' + original['signature']
        self.assertEqual(original['fingerprint'], hashlib.sha256(legacy_identity.encode()).hexdigest())
        opt.cancel(self.store, ids[True, True])
        opt.retry(self.app, self.store, ids[True, True])
        self.assertEqual((self.job(ids[True, True])['stereo'], self.job(ids[True, True])['limit1080']), (1, 1))
        public = {job['id']: job for job in opt.status(self.app, self.store)['jobs']}
        self.assertIs(public[ids[False, False]]['stereo'], False)
        self.assertIs(public[ids[True, True]]['limit1080'], True)

    def test_completed_source_dedup_matches_both_options(self):
        for stereo, limit1080 in ((False, False), (True, False), (False, True), (True, True)):
            job_id = self.enqueue(stereo=stereo, limit1080=limit1080)
            self.assertTrue(self.run_fixture())
            self.assertEqual(self.job(job_id)['state'], 'complete', self.job(job_id)['error'])
        self.assertEqual(len(list(opt._output_root(self.app, self.store).rglob('*.mkv'))), 4)

    def test_existing_completed_and_cancelled_jobs_migrate_with_options_off(self):
        completed = self.enqueue()
        self.run_fixture()
        (self.movies / 'Second.mkv').write_bytes(b'cancelled fixture')
        cancelled = self.enqueue('Second.mkv')
        opt.cancel(self.store, cancelled)
        with opt._db(self.store) as db:
            db.execute('ALTER TABLE jobs DROP COLUMN stereo')
            db.execute('ALTER TABLE jobs DROP COLUMN limit1080')
        self.assertEqual(self.job(completed)['state'], 'complete')
        self.assertEqual(self.job(cancelled)['state'], 'cancelled')
        for job_id in (completed, cancelled):
            self.assertEqual((self.job(job_id)['stereo'], self.job(job_id)['limit1080']), (0, 0))
        self.assertEqual(completed, self.enqueue())
        self.assertNotEqual(completed, self.enqueue(stereo=True))

    def test_stereo_converts_all_audio_tracks_but_keeps_their_identity_and_sample_rate(self):
        self.before['streams'][1].update(codec_name='pcm_s16le', channels=6)
        mono = copy.deepcopy(self.before['streams'][1])
        mono.update(channels=1, tags={'language': 'fra', 'title': 'Mono commentary'}, disposition={'default': 0})
        self.before['streams'].insert(2, mono)
        self.after = copy.deepcopy(self.before)
        self.after['streams'][0]['codec_name'] = 'hevc'
        for stream in self.after['streams']:
            if stream['codec_type'] == 'audio':
                stream.update(codec_name='aac', channels=2)
        commands = []
        def encoder(command, _checkpoint):
            commands.append(command)
            Path(command[-1]).write_bytes(b'encoded stereo fixture')
        job_id = self.enqueue(stereo=True)
        self.run_fixture(encoder=encoder)
        self.assertEqual(self.job(job_id)['state'], 'complete', self.job(job_id)['error'])
        command = commands[0]
        for flag, value in (('-c:a', 'aac'), ('-b:a', '160k'), ('-ac:a', '2'), ('-ar:a:0', '48000'), ('-ar:a:1', '48000')):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_stereo_and_dimension_exceptions_are_allowed_only_when_requested(self):
        before = probe_fixture()
        before['streams'][1].update(codec_name='pcm_s16le', channels=6)
        after = copy.deepcopy(before)
        after['streams'][0]['codec_name'] = 'hevc'
        after['streams'][1].update(codec_name='aac', channels=2)
        with self.assertRaises(opt.OptimiseError):
            opt._validate(before, after, 0, 3, 'hevc')
        opt._validate(before, after, 0, 3, 'hevc', stereo=True)
        for changed in ({'channels': 1}, {'codec_name': 'mp3'}, {'sample_rate': '44100'}):
            invalid = copy.deepcopy(after)
            invalid['streams'][1].update(changed)
            with self.subTest(changed=changed), self.assertRaises(opt.OptimiseError):
                opt._validate(before, invalid, 0, 3, 'hevc', stereo=True)
        before['streams'][1]['sample_rate'] = '192000'
        with self.assertRaises(opt.OptimiseError):
            opt._inspect(before, 'hevc', stereo=True)

    def test_1080_geometry_preserves_portrait_and_anamorphic_aspect_without_upscale(self):
        for width, height, sar in ((1920, 1200, '1:1'), (1080, 1920, '1:1'), (720, 576, '16:15'), (2048, 1152, '1:1')):
            before = probe_fixture()
            video = before['streams'][0]
            video.update(width=width, height=height, sample_aspect_ratio=sar, display_aspect_ratio=str(Fraction(width, height) * Fraction(sar.replace(':', '/'))))
            target_w, target_h, target_sar, dar = opt._geometry(video)
            self.assertLessEqual(target_w, min(width, 1920))
            self.assertLessEqual(target_h, min(height, 1080))
            self.assertEqual((target_w % 2, target_h % 2), (0, 0))
            self.assertEqual(Fraction(target_w, target_h) * target_sar, Fraction(width, height) * Fraction(sar.replace(':', '/')))
            after = copy.deepcopy(before)
            after['streams'][0].update(codec_name='hevc', width=target_w, height=target_h, sample_aspect_ratio=str(target_sar), display_aspect_ratio=str(dar))
            opt._validate(before, after, 0, 3, 'hevc', limit1080=True)
            if (width, height) != (target_w, target_h):
                with self.assertRaises(opt.OptimiseError):
                    opt._validate(before, after, 0, 3, 'hevc')
            after['streams'][0]['sample_aspect_ratio'] = '2:1'
            with self.assertRaises(opt.OptimiseError):
                opt._validate(before, after, 0, 3, 'hevc', limit1080=True)
        self.assertEqual(opt._geometry(dict(width=720, height=576, sample_aspect_ratio='16:15'))[:2], (720, 576))
        self.assertEqual(opt._geometry(dict(width=1080, height=1920, sample_aspect_ratio='1:1'))[:2], (606, 1080))

    def test_1080_geometry_uses_known_display_aspect_and_rejects_ambiguity(self):
        for sar in (None, '0:1', 'not-known'):
            video = dict(width=720, height=576, sample_aspect_ratio=sar, display_aspect_ratio='4:3')
            width, height, target_sar, dar = opt._geometry(video)
            self.assertEqual((width, height, target_sar, dar), (720, 576, Fraction(16, 15), Fraction(4, 3)))
        with self.assertRaises(opt.OptimiseError):
            opt._geometry(dict(width=720, height=576, sample_aspect_ratio='1:1', display_aspect_ratio='16:9'))
        with self.assertRaises(opt.OptimiseError):
            opt._geometry(dict(width=720, height=576))

    def test_validated_smaller_copy_retains_every_original_byte_and_avoids_repeat_work(self):
        job_id = self.enqueue()
        self.assertTrue(self.run_fixture())
        job = self.job(job_id)
        self.assertEqual(job['state'], 'complete')
        public = opt.status(self.app, self.store)['jobs'][0]
        self.assertEqual(public['progress_percent'], 100)
        self.assertEqual(public['duration_seconds'], 3)
        self.assertEqual(public['elapsed_basis'], 'started')
        with patch.object(opt.time, 'time', return_value=job['updated'] + 1000):
            self.assertEqual(opt.status(self.app, self.store)['jobs'][0]['elapsed_seconds'], public['elapsed_seconds'])
        self.assertEqual(self.source.read_bytes(), self.original)
        target = opt._output_root(self.app, self.store) / job['output']
        self.assertEqual(target.read_bytes(), b'disposable encoded fixture')
        self.assertFalse((target.parent / '.encoding.mkv').exists())
        self.assertEqual(job['source_hash'], hashlib.sha256(self.original).hexdigest())
        self.assertEqual(self.enqueue(), job_id)
        self.assertFalse(opt.run_once(self.app, self.store, self.stop))
        (self.movies / 'Copy.mkv').write_bytes(self.original)
        duplicate = self.enqueue('Copy.mkv')
        with patch.object(opt, '_probe', return_value=self.before), patch.object(opt, '_run') as encode:
            self.assertTrue(opt.run_once(self.app, self.store, self.stop))
        self.assertEqual(self.job(duplicate)['state'], 'skipped')
        encode.assert_not_called()

    def test_large_or_invalid_outputs_are_discarded_without_replacement(self):
        job_id = self.enqueue()
        self.run_fixture(encoder=lambda command, _checkpoint: Path(command[-1]).write_bytes(self.original))
        self.assertEqual(self.job(job_id)['state'], 'skipped')
        self.assertFalse(self.job(job_id)['output'])
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(list(opt._output_root(self.app, self.store).rglob('*.mkv')), [])

    def test_missing_track_metadata_or_decode_failure_prevents_publication(self):
        for defect in ('track', 'metadata', 'duration', 'chapter', 'codec', 'decode'):
            with self.subTest(defect=defect):
                job_id = self.enqueue()
                after = copy.deepcopy(self.after)
                if defect == 'track':
                    after['streams'].pop()
                elif defect == 'metadata':
                    after['streams'][1]['tags']['language'] = 'fra'
                elif defect == 'duration':
                    after['format']['duration'] = '7.0'
                elif defect == 'chapter':
                    after['chapters'][0]['tags']['title'] = 'Changed'
                elif defect == 'codec':
                    after['streams'][0]['codec_name'] = 'mpeg4'
                if defect == 'decode':
                    def run(command, checkpoint, *_args, **_kwargs):
                        checkpoint()
                        if command[-1] == '-':
                            raise opt.OptimiseError('Fixture decode failure.')
                        Path(command[-1]).write_bytes(b'fixture output')
                    with patch.object(opt, '_probe', side_effect=[self.before, after]), patch.object(opt, '_run', side_effect=run):
                        opt.run_once(self.app, self.store, self.stop)
                else:
                    self.run_fixture(after=after)
                self.assertEqual(self.job(job_id)['state'], 'failed')
                self.assertEqual(self.source.read_bytes(), self.original)
                self.assertFalse(self.job(job_id)['output'])
                self.source.touch()  # Each case explicitly queues a new source stat generation.

    def test_source_changes_and_new_root_mappings_prevent_processing(self):
        job_id = self.enqueue()
        self.source.write_bytes(self.original + b'changed fixture')
        with patch.object(opt, '_run') as encode:
            opt.run_once(self.app, self.store, self.stop)
        encode.assert_not_called()
        self.assertEqual(self.job(job_id)['state'], 'failed')
        next_id = self.enqueue()
        self.app.config['REELDOCK_MOVIE_LIBRARY'] = str(self.tv)
        (self.tv / 'Fixture.mkv').write_bytes(self.original)
        opt.run_once(self.app, self.store, self.stop)
        self.assertEqual(self.job(next_id)['state'], 'failed')

    def test_cancel_disable_and_manual_retry_do_not_repeat_completed_jobs(self):
        job_id = self.enqueue()
        opt.cancel(self.store, job_id)
        self.assertEqual(self.job(job_id)['state'], 'cancelled')
        self.assertFalse(opt.run_once(self.app, self.store, self.stop))
        opt.retry(self.app, self.store, job_id)
        def cancel_during_encode(command, checkpoint):
            Path(command[-1]).write_bytes(b'fixture output')
            opt.configure(self.store, False)
            checkpoint()
        self.run_fixture(encoder=cancel_during_encode)
        self.assertEqual(self.job(job_id)['state'], 'cancelled')
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse(self.job(job_id)['output'])
        opt.configure(self.store, True)
        opt.retry(self.app, self.store, job_id)
        self.run_fixture()
        self.assertEqual(self.job(job_id)['state'], 'complete')
        with self.assertRaises(opt.OptimiseError):
            opt.retry(self.app, self.store, job_id)

    def test_global_claim_blocks_a_second_worker_and_interrupted_work_is_not_replayed(self):
        job_id = self.enqueue()
        claim = os.open(self.store.directory / 'library-optimisation.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(claim, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse(opt.run_once(self.app, self.store, self.stop))
            self.assertEqual(self.job(job_id)['state'], 'queued')
        finally:
            os.close(claim)
        opt._update(self.store, job_id, state='encoding')
        with patch.object(opt, '_run') as encode:
            self.assertFalse(opt.run_once(self.app, self.store, self.stop))
        encode.assert_not_called()
        self.assertEqual(self.job(job_id)['state'], 'failed')

    def test_low_output_space_prevents_encoding_without_changing_the_original(self):
        job_id = self.enqueue()
        with patch.object(opt.shutil, 'disk_usage', return_value=SimpleNamespace(free=1024)), patch.object(opt, '_probe', return_value=self.before), patch.object(opt, '_run') as encode:
            opt.run_once(self.app, self.store, self.stop)
        encode.assert_not_called()
        self.assertEqual(self.job(job_id)['state'], 'failed')
        self.assertIn('free space', self.job(job_id)['error'])
        self.assertEqual(self.source.read_bytes(), self.original)

    def interrupted_copy(self, job_id, state='encoding'):
        root = opt._output_root(self.app, self.store)
        relative = f'movies/{job_id}-aabbccdd/.encoding.mkv'
        temporary = root / relative
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(b'disposable interrupted copy' * 1000)
        opt._update(self.store, job_id, state=state, temporary_root=str(root), temporary=relative)
        return temporary

    def test_interrupted_temporary_copy_is_cleaned_only_after_global_claim_is_free(self):
        job_id = self.enqueue()
        temporary = self.interrupted_copy(job_id)
        claim = os.open(self.store.directory / 'library-optimisation.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(claim, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse(opt.run_once(self.app, self.store, self.stop))
            self.assertTrue(temporary.exists())
            self.assertEqual(self.job(job_id)['state'], 'encoding')
        finally:
            os.close(claim)
        with patch.object(opt, '_run') as encode:
            self.assertFalse(opt.run_once(self.app, self.store, self.stop))
        encode.assert_not_called()
        self.assertFalse(temporary.exists())
        self.assertFalse(temporary.parent.exists())
        self.assertEqual(self.job(job_id)['state'], 'failed')
        self.assertEqual(self.job(job_id)['temporary'], '')
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_recovery_refuses_temporary_symlinks_and_unowned_paths(self):
        job_id = self.enqueue()
        temporary = self.interrupted_copy(job_id)
        temporary.unlink()
        temporary.symlink_to(self.source)
        self.assertFalse(opt.run_once(self.app, self.store, self.stop))
        self.assertTrue(temporary.is_symlink())
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertTrue(self.job(job_id)['temporary'])
        opt._update(self.store, job_id, temporary='../../movies/Fixture.mkv')
        self.assertFalse(opt.run_once(self.app, self.store, self.stop))
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertIn('could not be removed safely', self.job(job_id)['error'])

    def test_recovered_complete_copy_keeps_the_published_file(self):
        job_id = self.enqueue()
        self.run_fixture()
        job = self.job(job_id)
        root = opt._output_root(self.app, self.store)
        target = root / job['output']
        temporary = target.parent / '.encoding.mkv'
        os.link(target, temporary)
        opt._update(self.store, job_id, temporary_root=str(root), temporary=str(temporary.relative_to(root)))
        self.assertFalse(opt.run_once(self.app, self.store, self.stop))
        self.assertFalse(temporary.exists())
        self.assertEqual(target.read_bytes(), b'disposable encoded fixture')
        self.assertEqual(self.job(job_id)['state'], 'complete')
        self.assertEqual(self.job(job_id)['temporary'], '')

    def test_crash_after_final_link_before_commit_discards_only_its_uncommitted_copy(self):
        job_id = self.enqueue()
        temporary = self.interrupted_copy(job_id, state='validating')
        target = temporary.parent / 'Fixture.optimised.mkv'
        os.link(temporary, target)
        self.assertEqual(temporary.stat().st_ino, target.stat().st_ino)
        self.assertFalse(opt.run_once(self.app, self.store, self.stop))
        self.assertFalse(temporary.exists())
        self.assertFalse(target.exists())
        self.assertEqual(self.job(job_id)['state'], 'failed')
        self.assertEqual(self.job(job_id)['temporary'], '')
        opt.retry(self.app, self.store, job_id)
        self.run_fixture()
        self.assertEqual(self.job(job_id)['state'], 'complete')
        self.assertEqual(len(list(opt._output_root(self.app, self.store).rglob('*.mkv'))), 1)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_crash_recovery_does_not_remove_a_different_final_inode(self):
        job_id = self.enqueue()
        temporary = self.interrupted_copy(job_id, state='validating')
        target = temporary.parent / 'Fixture.optimised.mkv'
        target.write_bytes(b'foreign fixture copy')
        self.assertFalse(opt.run_once(self.app, self.store, self.stop))
        self.assertEqual(target.read_bytes(), b'foreign fixture copy')
        self.assertTrue(temporary.exists())
        self.assertTrue(self.job(job_id)['temporary'])
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_output_parent_replacement_during_encoding_cannot_redirect_writes(self):
        parent = self.base / 'output-parent'
        parent.mkdir()
        victim = self.base / 'untouched-output'
        victim.mkdir()
        self.app.config['REELDOCK_OPTIMISE_OUTPUT'] = str(parent / 'outputs')
        job_id = self.enqueue()
        def replace_parent(command, _checkpoint):
            Path(command[-1]).write_bytes(b'disposable encoded fixture')
            parent.rename(self.base / 'old-output-parent')
            parent.symlink_to(victim, target_is_directory=True)
        self.run_fixture(encoder=replace_parent)
        self.assertEqual(self.job(job_id)['state'], 'failed')
        self.assertEqual(list(victim.iterdir()), [])
        self.assertEqual(list((self.base / 'old-output-parent').rglob('.encoding.mkv')), [])
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_existing_job_database_gets_temporary_path_columns(self):
        self.enqueue()
        with opt._db(self.store) as db:
            db.execute('ALTER TABLE jobs DROP COLUMN temporary')
            db.execute('ALTER TABLE jobs DROP COLUMN temporary_root')
        with opt._db(self.store) as db:
            columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
        self.assertTrue({'temporary', 'temporary_root'} <= columns)

    def test_unsupported_tracks_hdr_and_output_inside_source_are_rejected(self):
        for defect in ('hdr', 'data', '10bit-h264'):
            probe = probe_fixture()
            profile = 'hevc'
            if defect == 'hdr':
                probe['streams'][0]['color_transfer'] = 'smpte2084'
            elif defect == 'data':
                probe['streams'].append({'codec_type': 'data', 'codec_name': 'bin_data'})
            else:
                probe['streams'][0]['pix_fmt'] = 'yuv420p10le'
                profile = 'h264'
            with self.subTest(defect=defect), self.assertRaises(opt.OptimiseError):
                opt._inspect(probe, profile)
        self.app.config['REELDOCK_OPTIMISE_OUTPUT'] = str(self.movies / 'outputs')
        with self.assertRaises(opt.OptimiseError):
            self.enqueue()

    def test_route_access_csrf_validation_and_get_do_not_start_work(self):
        opt.register(self.app, self.store, lambda **_kwargs: None, lambda: 'fixture-csrf')
        client = self.app.test_client()
        with patch.object(opt, 'scan') as scan, patch.object(opt, 'run_once') as run:
            self.assertEqual(client.get('/api/library-optimise').status_code, 200)
        scan.assert_not_called()
        run.assert_not_called()
        self.assertEqual(client.post('/api/library-optimise', json={'action': 'settings', 'enabled': True}).status_code, 403)
        self.assertEqual(client.post('/api/library-optimise', json={'action': 'settings', 'enabled': True}, headers={'X-CSRF-Token': 'fixture-csrf'}).status_code, 200)
        for body in ({'action': 'scan', 'kind': []}, {'action': 'enqueue', 'kind': 'movies', 'path': 'Fixture.mkv', 'profile': []}, {'action': 'scan', 'kind': 'movies', 'folder': '../'},
                     *({'action': 'enqueue', 'kind': 'movies', 'path': 'Fixture.mkv', 'profile': 'hevc', option: invalid}
                       for option in ('stereo', 'limit1080') for invalid in (None, 0, 1, 'true', [])),
                     {'action': 'enqueue', 'kind': 'movies', 'path': 'Fixture.mkv', 'profile': 'hevc', 'stronger': True}):
            response = client.post('/api/library-optimise', json=body, headers={'X-CSRF-Token': 'fixture-csrf'})
            self.assertEqual(response.status_code, 400)
        with patch.object(opt.shutil, 'which', return_value='/fixture/tool'):
            response = client.post('/api/library-optimise', json={'action': 'enqueue', 'kind': 'movies', 'path': 'Fixture.mkv', 'profile': 'hevc', 'stereo': True, 'limit1080': True}, headers={'X-CSRF-Token': 'fixture-csrf'})
        self.assertEqual(response.status_code, 200)
        queued = response.get_json()['jobs'][0]
        self.assertIs(queued['stereo'], True)
        self.assertIs(queued['limit1080'], True)
        self.assertEqual(client.post('/api/library-optimise', json={'action': 'retry', 'id': queued['id'], 'stereo': False}, headers={'X-CSRF-Token': 'fixture-csrf'}).status_code, 400)

    def test_subprocess_capture_and_cancellation_are_bounded(self):
        self.assertEqual(opt._run([sys.executable, '-c', 'print("fixture")'], lambda: None, 3, capture=True).strip(), b'fixture')
        calls = 0
        def checkpoint():
            nonlocal calls
            calls += 1
            if calls > 2:
                raise opt.Cancelled()
        with self.assertRaises(opt.Cancelled):
            opt._run([sys.executable, '-c', 'import time; time.sleep(10)'], checkpoint, 3)

    def test_progress_parser_accepts_fragmented_numeric_frames_without_retaining_messages(self):
        reports = []
        parser = opt._ProgressParser(reports.append)
        parser.feed(b'private_path=/private/fixture-secret\nout_time_')
        parser.feed(b'us=1500000\nfps=12.5\nspeed=0.75x\nprogress=continue\n')
        self.assertEqual(reports, [dict(processed_seconds=1.5, fps=12.5, speed=.75, finished=False)])
        parser.feed(b'out_time_us=-1\nfps=NaN\nspeed=infx\nprogress=end\n')
        self.assertEqual(reports[-1], dict(fps=None, speed=None, finished=True))
        self.assertNotIn('private', repr(reports))
        with self.assertRaises(opt.OptimiseError):
            parser.feed(b'x' * 513)

    def test_progress_is_throttled_phase_specific_and_never_complete_before_publication(self):
        job_id = self.enqueue()
        reporter = opt._ProgressReporter(self.store, job_id, 3)
        with patch.object(opt, '_update', wraps=opt._update) as update, patch.object(opt.time, 'monotonic', side_effect=[0, .4, 1.6, 1.7]):
            reporter.start('encoding')
            reporter.report(dict(processed_seconds=.75, fps=12, speed=2))
            reporter.report(dict(processed_seconds=1.5, fps=14, speed=2))
            self.assertEqual(self.job(job_id)['progress_percent'], 25)
            reporter.report(dict(processed_seconds=2.25, fps=15, speed=2))
            reporter.report(dict(processed_seconds=3, fps=16, speed=2, finished=True))
        self.assertEqual(update.call_count, 4, 'phase transition plus throttled numeric updates and final frame')
        self.assertEqual(self.job(job_id)['progress_percent'], 99.9)
        self.assertIsNone(opt.status(self.app, self.store)['jobs'][0]['eta_seconds'], 'finished media pass has unmeasured final checks, not a zero-second ETA')
        reporter.start('validating')
        self.assertIsNone(self.job(job_id)['processed_seconds'])
        self.assertIsNone(self.job(job_id)['progress_percent'])
        reporter.report(dict(processed_seconds=1.5, fps=100, speed=2))
        public = opt.status(self.app, self.store)['jobs'][0]
        self.assertEqual(public['progress_percent'], 50)
        self.assertAlmostEqual(public['eta_seconds'], .75)
        self.assertIsNotNone(public['phase_elapsed_seconds'])
        opt._update(self.store, job_id, progress_updated=1)
        self.assertIsNone(opt.status(self.app, self.store)['jobs'][0]['eta_seconds'], 'stale speed cannot promise an ETA')
        opt._update(self.store, job_id, progress_updated=opt.time.time(), speed=1e-320)
        self.assertIsNone(opt.status(self.app, self.store)['jobs'][0]['eta_seconds'], 'overflowing estimates must not produce non-finite JSON')
        opt._update(self.store, job_id, state='failed')
        self.assertLess(opt.status(self.app, self.store)['jobs'][0]['progress_percent'], 100)
        opt.retry(self.app, self.store, job_id)
        self.assertTrue(all(self.job(job_id)[column] is None for column in opt.PROGRESS_COLUMNS))

    def test_old_worker_active_row_migrates_without_claiming_work_or_reading_media(self):
        job_id = self.enqueue()
        temporary = self.interrupted_copy(job_id)
        with opt._db(self.store) as db:
            for column in opt.PROGRESS_COLUMNS:
                db.execute('ALTER TABLE jobs DROP COLUMN ' + column)
        old_worker = opt.sqlite3.connect(self.store.directory / 'library-optimisation.sqlite3')
        self.addCleanup(old_worker.close)
        old_worker.row_factory = opt.sqlite3.Row
        immutable = dict(old_worker.execute('SELECT signature,fingerprint,source_hash,stereo,limit1080,cancel,state FROM jobs WHERE id=?', (job_id,)).fetchone())
        with patch.object(opt, '_run', side_effect=AssertionError('Status must never run tools')), patch.object(opt, '_hash', side_effect=AssertionError('Status must not read media')), patch.object(opt, '_recover_temporary', side_effect=AssertionError('Status must not recover work')), patch.object(opt.os, 'open', wraps=os.open) as opened:
            public = opt.status(self.app, self.store)['jobs'][0]
        self.assertTrue(all(call.args[1] & os.O_DIRECTORY for call in opened.call_args_list))
        self.assertEqual(public['live_output_bytes'], temporary.stat().st_size)
        self.assertIsNone(public['progress_percent'])
        self.assertIsNone(public['eta_seconds'])
        self.assertIsNone(public['phase_elapsed_seconds'])
        self.assertEqual(public['elapsed_basis'], 'created')
        self.assertGreaterEqual(public['elapsed_seconds'], 0)
        self.assertEqual({key: self.job(job_id)[key] for key in immutable}, immutable)
        old_worker.execute("UPDATE jobs SET state='validating',updated=? WHERE id=?", (opt.time.time(), job_id))
        old_worker.commit()
        self.assertEqual(opt.status(self.app, self.store)['jobs'][0]['state'], 'validating')
        with temporary.open('ab') as output:
            output.write(b'new old-worker bytes')
        self.assertEqual(opt.status(self.app, self.store)['jobs'][0]['live_output_bytes'], temporary.stat().st_size)
        self.assertTrue(temporary.is_file(), 'status must leave the active legacy copy untouched')
        self.assertFalse(any(key in public for key in ('temporary', 'temporary_root', 'root', 'source_hash')))

    def test_live_output_stat_rejects_symlinks_foreign_paths_and_root_replacement(self):
        job_id = self.enqueue()
        temporary = self.interrupted_copy(job_id)
        temporary.unlink()
        temporary.symlink_to(self.source)
        self.assertIsNone(opt.status(self.app, self.store)['jobs'][0]['live_output_bytes'])
        temporary.unlink()
        temporary.write_bytes(b'fixture')
        opt._update(self.store, job_id, temporary='movies/foreign-aabbccdd/.encoding.mkv')
        self.assertIsNone(opt.status(self.app, self.store)['jobs'][0]['live_output_bytes'])
        opt._update(self.store, job_id, temporary=str(temporary.relative_to(opt._output_root(self.app, self.store))))
        root = opt._output_root(self.app, self.store)
        root.rename(root.with_name('old-outputs'))
        root.symlink_to(self.movies, target_is_directory=True)
        self.assertIsNone(opt.status(self.app, self.store)['jobs'][0]['live_output_bytes'])
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_private_progress_pipe_remains_bounded_and_cancellable(self):
        tool = self.base / 'progress-tool'
        tool.write_text('import os,sys,time\np=int(sys.argv[sys.argv.index("-progress")+1].split(":")[1])\nos.write(p,b"out_time_us=1500000\\nfps=20\\nspeed=2x\\nprogress=continue\\n")\ntime.sleep(10)\n')
        # The hardened deployment fixture has a noexec temporary filesystem.
        # Simulate an FFmpeg CLI through Python while retaining the exact
        # generated progress arguments, inherited fds and process-group logic.
        popen = opt.subprocess.Popen
        def python_tool(command, **options):
            self.assertEqual(command[0], str(tool))
            return popen([sys.executable, *command], **options)
        reports = []
        def checkpoint():
            if reports:
                raise opt.Cancelled()
        with patch.object(opt.subprocess, 'Popen', side_effect=python_tool), self.assertRaises(opt.Cancelled):
            opt._run([str(tool)], checkpoint, 3, progress=reports.append)
        self.assertEqual(reports[0]['processed_seconds'], 1.5)
        tool.write_text('import os,sys\np=int(sys.argv[sys.argv.index("-progress")+1].split(":")[1])\nos.write(p,b"x"*1024)\n')
        with patch.object(opt.subprocess, 'Popen', side_effect=python_tool), self.assertRaisesRegex(opt.OptimiseError, 'progress response'):
            opt._run([str(tool)], lambda: None, 3, progress=reports.append)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg sample validation needs the deployment image tools.')
class RealLibraryOptimiseTests(unittest.TestCase):
    setUp = LibraryOptimiseTests.setUp
    enqueue = LibraryOptimiseTests.enqueue
    job = LibraryOptimiseTests.job

    def test_real_ffmpeg_private_machine_progress_uses_numeric_media_time(self):
        reports = []
        opt._run(['ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=12:duration=2',
                  '-f', 'null', '-'], lambda: None, 15, progress=reports.append)
        self.assertTrue(reports)
        self.assertTrue(reports[-1]['finished'])
        self.assertAlmostEqual(reports[-1]['processed_seconds'], 2, delta=.1)
        self.assertTrue(all(set(report) <= {'processed_seconds', 'fps', 'speed', 'finished'} for report in reports))

    def test_real_ffmpeg_preserves_tracks_chapters_and_originals_for_both_profiles(self):
        subtitles = self.base / 'fixture.srt'
        subtitles.write_text('1\n00:00:00,000 --> 00:00:02,500\nDisposable subtitle fixture\n')
        metadata = self.base / 'fixture.ffmetadata'
        metadata.write_text(';FFMETADATA1\ntitle=Disposable fixture title\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=3000\ntitle=Disposable chapter\n')
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=12:duration=3',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=3', '-i', str(subtitles), '-f', 'ffmetadata', '-i', str(metadata),
                        '-map', '0:v', '-map', '1:a', '-map', '2:s', '-map_metadata', '3', '-map_chapters', '3',
                        '-c:v', 'ffv1', '-pix_fmt', 'yuv420p', '-c:a', 'pcm_s16le', '-c:s', 'srt',
                        '-metadata:s:a:0', 'language=eng', '-metadata:s:s:0', 'language=eng', '-disposition:a:0', 'default', '-disposition:s:0', 'forced',
                        '-t', '3', str(self.source)], check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
        original_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        for profile in ('hevc', 'h264'):
            with self.subTest(profile=profile):
                job_id = self.enqueue(profile=profile)
                self.assertTrue(opt.run_once(self.app, self.store, self.stop))
                job = self.job(job_id)
                self.assertEqual(job['state'], 'complete', job['error'])
                self.assertLess(job['output_bytes'], job['source_bytes'] * .95)
                self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), original_hash)
                output = opt._probe(opt._output_root(self.app, self.store) / job['output'], lambda: None)
                self.assertEqual([stream['codec_type'] for stream in output['streams']], ['video', 'audio', 'subtitle'])
                self.assertEqual(output['streams'][1]['codec_name'], 'pcm_s16le')
                self.assertEqual(output['streams'][2]['tags']['language'], 'eng')
                self.assertEqual(output['chapters'][0]['tags']['title'], 'Disposable chapter')

    def test_real_ffmpeg_stereo_and_1080_limit_preserve_tracks_and_originals(self):
        subtitles = self.base / 'surround.srt'
        subtitles.write_text('1\n00:00:00,000 --> 00:00:00,900\nDisposable surround subtitle\n')
        metadata = self.base / 'surround.ffmetadata'
        metadata.write_text(';FFMETADATA1\ntitle=Disposable surround title\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=Surround chapter\n')
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=1920x1200:rate=4:duration=1',
                        '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=5.1', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=1',
                        '-i', str(subtitles), '-f', 'ffmetadata', '-i', str(metadata),
                        '-map', '0:v', '-map', '1:a', '-map', '2:a', '-map', '3:s', '-map_metadata', '4', '-map_chapters', '4',
                        '-c:v', 'ffv1', '-pix_fmt', 'yuv420p', '-c:a', 'pcm_s16le', '-c:s', 'srt',
                        '-metadata:s:a:0', 'language=eng', '-metadata:s:a:0', 'title=Surround fixture', '-disposition:a:0', 'default',
                        '-metadata:s:a:1', 'language=fra', '-metadata:s:a:1', 'title=Mono commentary', '-disposition:a:1', '0',
                        '-metadata:s:s:0', 'language=eng', '-disposition:s:0', 'forced', '-t', '1', str(self.source)],
                       check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=45)
        original_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        original = opt._probe(self.source, lambda: None)
        self.assertEqual([stream.get('channels') for stream in original['streams'] if stream['codec_type'] == 'audio'], [6, 1])
        job_id = self.enqueue(profile='h264', stereo=True, limit1080=True)
        self.assertTrue(opt.run_once(self.app, self.store, self.stop))
        job = self.job(job_id)
        self.assertEqual(job['state'], 'complete', job['error'])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), original_hash)
        self.assertLess(job['output_bytes'], job['source_bytes'] * .95)
        output = opt._probe(opt._output_root(self.app, self.store) / job['output'], lambda: None)
        self.assertEqual([stream['codec_type'] for stream in output['streams']], ['video', 'audio', 'audio', 'subtitle'])
        self.assertEqual((output['streams'][0]['width'], output['streams'][0]['height']), (1728, 1080))
        self.assertEqual(Fraction(output['streams'][0]['display_aspect_ratio'].replace(':', '/')), Fraction(8, 5))
        for source, converted in zip(original['streams'][1:3], output['streams'][1:3]):
            self.assertEqual((converted['codec_name'], converted['channels'], converted['sample_rate']), ('aac', 2, '48000'))
            self.assertEqual(converted['tags']['language'], source['tags']['language'])
            self.assertEqual(converted['tags']['title'], source['tags']['title'])
            self.assertEqual(converted['disposition']['default'], source['disposition']['default'])
        self.assertEqual(output['streams'][3]['codec_name'], 'subrip')
        self.assertEqual(output['streams'][3]['disposition']['forced'], 1)
        self.assertEqual(output['chapters'][0]['tags']['title'], 'Surround chapter')


if __name__ == '__main__':
    unittest.main()
