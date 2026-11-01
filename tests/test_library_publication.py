"""Disposable adjacent-copy fixtures; never touch installed libraries or jobs."""
import hashlib
import os
import shutil
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import library_optimise as opt, library_publication as pub
import test_library_optimise as fixture


class LibraryPublicationTests(unittest.TestCase):
    setUp = fixture.LibraryOptimiseTests.setUp
    job = fixture.LibraryOptimiseTests.job
    run_fixture = fixture.LibraryOptimiseTests.run_fixture

    def enqueue(self, path='Fixture.mkv', profile='hevc', stereo=False, limit1080=False):
        opt.configure(self.store, True)
        with patch.object(opt.shutil, 'which', return_value='/fixture/tool'):
            return opt.enqueue(self.app, self.store, 'movies', path, profile, stereo=stereo, limit1080=limit1080)

    def publication(self, job_id):
        with opt._db(self.store) as db:
            row = db.execute('SELECT * FROM publication_requests WHERE job_id=?', (job_id,)).fetchone()
            return dict(row) if row else None

    def appdata(self, job_id):
        return opt._output_root(self.app, self.store) / self.job(job_id)['output']

    def complete(self):
        job_id = self.enqueue()
        self.assertTrue(self.run_fixture())
        self.assertEqual(self.job(job_id)['state'], 'complete')
        self.assertEqual(self.publication(job_id)['state'], 'waiting')
        return job_id

    def destination(self, job_id):
        row = self.publication(job_id)
        return self.movies / row['folder'] / (row['final_name'] or row['name'])

    def test_new_enqueue_requires_separate_writable_same_physical_view(self):
        self.app.config.pop('REELDOCK_MOVIE_OUTPUT')
        with self.assertRaises(opt.OptimiseError):
            self.enqueue()
        self.app.config['REELDOCK_MOVIE_OUTPUT'] = str(self.tv)
        with self.assertRaisesRegex(opt.OptimiseError, 'same physical'):
            self.enqueue()
        alias = self.base / 'output-alias'
        alias.symlink_to(self.movies, target_is_directory=True)
        self.app.config['REELDOCK_MOVIE_OUTPUT'] = str(alias)
        with self.assertRaises(opt.OptimiseError):
            self.enqueue()
        self.app.config['REELDOCK_MOVIE_OUTPUT'] = str(self.movies)
        with patch.object(pub.os, 'access', return_value=False), self.assertRaisesRegex(opt.OptimiseError, 'not writable'):
            self.enqueue()
        job_id = self.enqueue()
        self.assertEqual(self.job(job_id)['destination_mode'], 'adjacent')
        self.assertEqual(self.job(job_id)['destination_name'], 'Fixture HEVC.mkv')
        self.assertEqual(self.enqueue(), job_id)
        self.assertEqual(opt.status(self.app, self.store)['libraries'][1]['output_available'], True)

    def test_names_clean_only_file_release_suffix_preserving_year_episode_and_collection_identity(self):
        self.assertEqual(pub.filename('Terminator Collection/Terminator.2.-.Judgment.Day.1991.2160p.BluRay.x265.mkv', 'hevc'), 'Terminator 2 - Judgment Day (1991) HEVC.mkv')
        self.assertEqual(pub.filename('Terminator Collection/Terminator 3 (2003) [BluRay-1080p][DTS].mkv', 'h264'), 'Terminator 3 (2003) H264.mkv')
        self.assertEqual(pub.filename('Show/Season 2/Show - S02E03 - A Real Episode Title 1080p WEB-DL x264.mkv', 'hevc'), 'Show - S02E03 - A Real Episode Title HEVC.mkv')
        self.assertEqual(pub.filename('Web Cam (2020).mkv', 'h264'), 'Web Cam (2020) H264.mkv')

    def test_validated_copy_moves_only_generated_appdata_after_adjacent_receipt(self):
        job_id = self.complete()
        original_info = opt._signature(self.source.stat())
        generated = self.appdata(job_id)
        generated_inode = generated.stat().st_ino
        expected = generated.read_bytes()
        with patch.object(opt, '_run', side_effect=AssertionError('A validated new output must not reencode or decode again')):
            self.assertTrue(opt.run_once(self.app, self.store, self.stop))
        row = self.publication(job_id)
        self.assertEqual((row['state'], row['cleanup_state']), ('complete', 'done'))
        self.assertEqual(self.destination(job_id).read_bytes(), expected)
        self.assertNotEqual(self.destination(job_id).stat().st_ino, generated_inode, 'publication copies bytes rather than cross-filesystem linking AppData')
        self.assertFalse(generated.exists())
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(opt._signature(self.source.stat()), original_info)
        self.assertFalse(list(self.movies.glob('.reeldock-optimise-*')))
        public = opt.status(self.app, self.store)['jobs'][0]
        self.assertEqual(public['publication_percent'], 100)
        self.assertEqual(public['publication_path'], 'Fixture HEVC.mkv')
        self.assertFalse(public['can_publish'])
        self.assertFalse(opt.run_once(self.app, self.store, self.stop))

    def test_collisions_never_overwrite_and_all_collision_cleanup_preserves_foreign_files(self):
        job_id = self.complete()
        name = self.publication(job_id)['name']
        (self.movies / name).write_bytes(b'foreign published file')
        self.assertTrue(opt.run_once(self.app, self.store, self.stop))
        self.assertEqual((self.movies / name).read_bytes(), b'foreign published file')
        self.assertEqual(self.publication(job_id)['final_name'], 'Fixture HEVC (2).mkv')
        # An independent original fixture with every candidate already occupied.
        (self.movies / 'Other.mkv').write_bytes(b'other disposable original' * 1000)
        other = self.enqueue('Other.mkv')
        self.run_fixture()
        for number in range(1, 101):
            (self.movies / pub._candidate('Other HEVC.mkv', number)).write_bytes(b'foreign')
        self.assertTrue(opt.run_once(self.app, self.store, self.stop))
        self.assertEqual(self.publication(other)['state'], 'failed')
        self.assertTrue(self.appdata(other).exists())
        self.assertFalse(list(self.movies.glob('.reeldock-optimise-*')))
        self.assertTrue(all((self.movies / pub._candidate('Other HEVC.mkv', n)).read_bytes() == b'foreign' for n in range(1, 101)))

    def test_cancel_save_during_copy_preserves_encoding_result_and_original(self):
        job_id = self.complete()
        write = os.write
        cancelled = False
        def cancelling_write(descriptor, data):
            nonlocal cancelled
            result = write(descriptor, data)
            if not cancelled:
                cancelled = True
                pub.cancel(self.store, job_id)
            return result
        with patch.object(pub.os, 'write', side_effect=cancelling_write):
            opt.run_once(self.app, self.store, self.stop)
        self.assertEqual(self.publication(job_id)['state'], 'cancelled')
        self.assertEqual((self.job(job_id)['state'], self.job(job_id)['cancel']), ('complete', 0))
        self.assertTrue(self.appdata(job_id).exists())
        self.assertFalse(self.destination(job_id).exists())
        self.assertFalse(list(self.movies.glob('.reeldock-optimise-*')))
        pub.request(self.app, self.store, job_id)
        self.assertTrue(opt.run_once(self.app, self.store, self.stop))
        self.assertEqual(self.publication(job_id)['state'], 'complete')

    def test_public_read_source_does_not_require_membership_of_its_original_group(self):
        job_id = self.complete()
        open_source = opt._open_source
        @contextmanager
        def source_group(app, kind, relative, **kwargs):
            with open_source(app, kind, relative, **kwargs) as (descriptor, info, root):
                fields = {key: getattr(info, key) for key in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}
                yield descriptor, SimpleNamespace(**fields, st_mode=0o100644, st_gid=-12345), root
        with patch.object(opt, '_open_source', source_group), patch.object(pub.os, 'fchown', side_effect=AssertionError('Public read needs no unavailable group change')):
            opt.run_once(self.app, self.store, self.stop)
        self.assertEqual(self.publication(job_id)['state'], 'complete', self.publication(job_id)['error'])
        self.assertEqual(self.destination(job_id).stat().st_mode & 0o777, 0o644)

    def crash_receipt(self, job_id, committed=False):
        row = self.publication(job_id)
        generated = self.appdata(job_id)
        temporary = self.movies / ('.reeldock-optimise-' + job_id + '-aabbccdd.part')
        temporary.write_bytes(generated.read_bytes())
        final = self.movies / row['name']
        os.link(temporary, final)
        info, original = temporary.stat(), generated.stat()
        pub._update(self.store, job_id, state='complete' if committed else 'copying', temporary=temporary.name,
                    temporary_device=str(info.st_dev), temporary_inode=str(info.st_ino), final_name=final.name,
                    output_hash=hashlib.sha256(generated.read_bytes()).hexdigest(), bytes=info.st_size, total_bytes=info.st_size,
                    appdata_device=str(original.st_dev), appdata_inode=str(original.st_ino), appdata_signature=opt._signature(original),
                    cleanup_state='pending' if committed else '')
        return temporary, final

    def test_link_before_commit_crash_recovery_reuses_copy_without_encoding_or_duplicate_final(self):
        job_id = self.complete()
        temporary, final = self.crash_receipt(job_id)
        with patch.object(opt, '_run', side_effect=AssertionError('No repeated encoding')):
            self.assertTrue(opt.run_once(self.app, self.store, self.stop))
        self.assertEqual(self.publication(job_id)['state'], 'complete')
        self.assertEqual(self.publication(job_id)['final_name'], final.name)
        self.assertFalse(temporary.exists())
        self.assertEqual(len(list(self.movies.glob('*HEVC*.mkv'))), 1)

    def test_committed_crash_receipt_keeps_final_and_finishes_generated_appdata_cleanup(self):
        job_id = self.complete()
        temporary, final = self.crash_receipt(job_id, committed=True)
        expected = final.read_bytes()
        self.assertTrue(opt.run_once(self.app, self.store, self.stop))
        self.assertEqual(final.read_bytes(), expected)
        self.assertFalse(temporary.exists())
        self.assertFalse(self.appdata(job_id).exists())
        self.assertEqual(self.publication(job_id)['cleanup_state'], 'done')

    def test_foreign_temporary_symlink_or_inode_is_retained_never_deleted(self):
        job_id = self.complete()
        temporary, final = self.crash_receipt(job_id)
        final.unlink()
        temporary.unlink()
        temporary.symlink_to(self.source)
        self.assertTrue(opt.run_once(self.app, self.store, self.stop))
        self.assertEqual(self.publication(job_id)['state'], 'failed')
        self.assertTrue(temporary.is_symlink())
        self.assertTrue(self.appdata(job_id).exists())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_temp_path_replacement_before_final_link_cannot_publish_foreign_bytes(self):
        job_id = self.complete()
        update = pub._update
        replaced = False
        def replacing_update(store, identity, **values):
            nonlocal replaced
            result = update(store, identity, **values)
            if values.get('final_name') and not replaced:
                replaced = True
                row = self.publication(job_id)
                temporary = self.movies / row['temporary']
                temporary.unlink()
                temporary.write_bytes(b'foreign replacement')
            return result
        with patch.object(pub, '_update', side_effect=replacing_update):
            opt.run_once(self.app, self.store, self.stop)
        self.assertEqual(self.publication(job_id)['state'], 'failed')
        self.assertTrue(self.appdata(job_id).exists())
        self.assertFalse(self.destination(job_id).exists())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_adjacent_removed_during_appdata_hash_prevents_appdata_deletion(self):
        job_id = self.complete()
        generated = self.appdata(job_id)
        original_hash = opt._hash
        attacked = False
        def replacing_final(descriptor, checkpoint):
            nonlocal attacked
            row = self.publication(job_id)
            if not attacked and row['state'] == 'complete' and os.fstat(descriptor).st_ino == generated.stat().st_ino:
                attacked = True
                self.destination(job_id).unlink()
                self.destination(job_id).write_bytes(b'foreign replacement')
            return original_hash(descriptor, checkpoint)
        with patch.object(opt, '_hash', side_effect=replacing_final):
            opt.run_once(self.app, self.store, self.stop)
        self.assertTrue(attacked)
        self.assertTrue(generated.exists())
        self.assertEqual(self.destination(job_id).read_bytes(), b'foreign replacement')
        self.assertEqual(self.publication(job_id)['cleanup_state'], 'error')
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_legacy_active_request_is_additive_and_cancelled_source_never_revived(self):
        job_id = fixture.LibraryOptimiseTests.enqueue(self)
        before = self.job(job_id)
        pub.request(self.app, self.store, job_id)
        self.assertEqual(self.job(job_id), before)
        self.assertEqual(self.publication(job_id)['state'], 'waiting')
        opt.cancel(self.store, job_id)
        with self.assertRaises(opt.OptimiseError):
            pub.request(self.app, self.store, job_id)
        opt.run_once(self.app, self.store, self.stop)
        self.assertEqual(self.publication(job_id)['state'], 'cancelled')
        self.assertEqual(self.job(job_id)['state'], 'cancelled')
        opt.retry(self.app, self.store, job_id)
        self.assertEqual(self.job(job_id)['destination_mode'], 'appdata')
        self.assertEqual(self.publication(job_id)['state'], 'waiting')

    def test_legacy_completed_output_is_revalidated_and_reuses_no_encode(self):
        job_id = fixture.LibraryOptimiseTests.enqueue(self)
        self.run_fixture()
        opt._update(self.store, job_id, validated_output_hash='')
        before = self.job(job_id)
        pub.request(self.app, self.store, job_id)
        self.assertEqual(self.job(job_id), before)
        with patch.object(opt, '_probe', side_effect=[self.before, self.after]), patch.object(opt, '_run') as decode:
            opt.run_once(self.app, self.store, self.stop)
        self.assertEqual(decode.call_count, 1)
        self.assertEqual(decode.call_args.args[0][-3:], ['-f', 'null', '-'])
        self.assertEqual(self.publication(job_id)['state'], 'complete')
        self.assertFalse(self.appdata(job_id).exists())

    def test_routes_require_csrf_exact_action_keys_and_do_not_publish_on_get(self):
        job_id = self.complete()
        opt.register(self.app, self.store, lambda **_kwargs: None, lambda: 'fixture')
        client = self.app.test_client()
        with patch.object(pub, 'process', side_effect=AssertionError('GET must not publish')):
            self.assertEqual(client.get('/api/library-optimise').status_code, 200)
        for action in ('request_publish', 'cancel_publish'):
            self.assertEqual(client.post('/api/library-optimise', json=dict(action=action, id=job_id)).status_code, 403)
            self.assertEqual(client.post('/api/library-optimise', headers={'X-CSRF-Token': 'fixture'}, json=dict(action=action, id=job_id, path='other')).status_code, 400)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Real publication needs isolated deployment-image FFmpeg.')
class RealLibraryPublicationTests(unittest.TestCase):
    setUp = fixture.LibraryOptimiseTests.setUp
    job = fixture.LibraryOptimiseTests.job
    enqueue = LibraryPublicationTests.enqueue
    publication = LibraryPublicationTests.publication
    appdata = LibraryPublicationTests.appdata
    destination = LibraryPublicationTests.destination

    def test_real_new_and_legacy_validated_copies_move_beside_untouched_source(self):
        import subprocess
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=12:duration=3',
                        '-c:v', 'ffv1', '-pix_fmt', 'yuv420p', str(self.source)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
        source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        for profile, legacy in (('hevc', False), ('h264', True)):
            with self.subTest(profile=profile, legacy=legacy):
                job_id = self.enqueue(profile=profile)
                self.assertTrue(opt.run_once(self.app, self.store, self.stop))
                self.assertEqual(self.job(job_id)['state'], 'complete', self.job(job_id)['error'])
                if legacy:
                    with opt._db(self.store) as db:
                        db.execute("UPDATE jobs SET destination_mode='appdata',destination_name='',validated_output_hash='' WHERE id=?", (job_id,))
                        db.execute('DELETE FROM publication_requests WHERE job_id=?', (job_id,))
                    before = self.job(job_id)
                    pub.request(self.app, self.store, job_id)
                    self.assertEqual(self.job(job_id), before)
                with patch.object(opt, '_run', wraps=opt._run) as tools:
                    self.assertTrue(opt.run_once(self.app, self.store, self.stop))
                ffmpeg = [call for call in tools.call_args_list if call.args[0][0] == 'ffmpeg']
                self.assertEqual(len(ffmpeg), int(legacy), 'legacy outputs get one full decode; neither copy is reencoded')
                self.assertTrue(all(call.args[0][-3:] == ['-f', 'null', '-'] for call in ffmpeg))
                self.assertEqual(self.publication(job_id)['state'], 'complete', self.publication(job_id)['error'])
                result = opt._probe(self.destination(job_id), lambda: None)
                self.assertEqual(result['streams'][0]['codec_name'], profile)
                self.assertFalse(self.appdata(job_id).exists())
                self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), source_hash)
