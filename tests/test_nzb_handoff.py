"""Internal NZB create-if-absent handoffs; all state is disposable and offline."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.downloaders.nzb.engine import EngineError, NzbEngine, _Item, _Meta, handoff_checkpoint

RAW = b'<nzb><file subject="&quot;Fixture.2026.mkv&quot; yEnc (1/2)"><groups><group>fixture</group></groups><segments><segment bytes="8" number="1">first@invalid</segment><segment bytes="8" number="2">second@invalid</segment></segments></file></nzb>'
TOKEN, JOB_ID = 'a' * 32, 'b' * 32


class NzbHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'data' / 'nzb-engine'
        spawn = patch.object(NzbEngine, '_spawn')
        spawn.start()
        self.addCleanup(spawn.stop)
        self.engine = self.new_engine()

    def new_engine(self):
        engine = NzbEngine(self.root / 'media', self.data)
        engine._servers = lambda: [{'host': 'fixture.invalid'}]
        self.addCleanup(engine.shutdown)
        return engine

    def preview(self, engine=None, raw=RAW):
        return (engine or self.engine).preview_bytes(raw, 'fixture.nzb', check_availability=False)

    def start(self, engine=None, **changes):
        engine = engine or self.engine
        options = dict(handoff_token=TOKEN, job_id=JOB_ID)
        options.update(changes)
        return engine.start(self.preview(engine)['id'], [0], 'Saved Film (2026)', **options)

    @property
    def path(self):
        return self.data / 'jobs' / (JOB_ID + '.json')

    def key(self):
        return self.engine.preview_activity_key(self.preview()['id'], [0])

    def read(self):
        return handoff_checkpoint(self.data, JOB_ID, TOKEN, self.key())

    def binding(self, stage='prepared', request_engine='', request_job='', request_state='queued', generation=0, acquisition_generation=0):
        path = self.data.parent / 'reeldock.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS discover_requests(id INTEGER PRIMARY KEY,engine TEXT,engine_id TEXT,state TEXT,stop_generation INTEGER)')
            db.execute('CREATE TABLE IF NOT EXISTS discover_handoffs(token TEXT,job_id TEXT,engine TEXT,stage TEXT,request_id INTEGER,stop_generation INTEGER,acquisition_generation INTEGER)')
            db.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT)')
            db.execute('DELETE FROM discover_handoffs')
            db.execute('DELETE FROM discover_requests')
            db.execute('INSERT INTO discover_requests VALUES(1,?,?,?,?)', (request_engine, request_job, request_state, generation))
            db.execute('INSERT INTO discover_handoffs VALUES(?,?,?,?,1,0,0)', (TOKEN, JOB_ID, 'nzb', stage))
            db.execute("INSERT OR REPLACE INTO settings VALUES('acquisition_generation',?)", (str(acquisition_generation),))

    def test_optional_pair_is_strict_and_legacy_start_still_works(self):
        preview = self.preview()
        for options in [dict(handoff_token=TOKEN), dict(job_id=JOB_ID),
                        dict(handoff_token=None, job_id=JOB_ID), dict(handoff_token=TOKEN, job_id='../escape'),
                        dict(handoff_token='A' * 32, job_id=JOB_ID), dict(handoff_token=TOKEN, job_id='b' * 31)]:
            with self.subTest(options=options), self.assertRaises(EngineError):
                self.engine.start(preview['id'], [0], **options)
        self.assertFalse(self.engine._jobs)
        self.assertFalse(list(self.data.glob('jobs/*.json')))
        with patch.object(self.engine, '_launch') as launch:
            legacy = self.engine.start(preview['id'], [0])
        self.assertNotEqual(legacy['id'], JOB_ID)
        self.assertEqual(self.engine._jobs[legacy['id']].handoff_token, '')
        launch.assert_called_once()

    def test_checkpoint_exists_before_registration_and_launch(self):
        original = self.engine._save_job
        stages = []

        def save(job):
            self.assertNotIn(job.id, self.engine._jobs)
            self.assertNotIn(job, self.engine._order)
            stages.append('save')
            original(job)

        def launch(job):
            self.assertIs(self.engine._jobs[job.id], job)
            self.assertEqual(json.loads(self.path.read_text())['handoff_token'], TOKEN)
            stages.append('launch')

        with patch.object(self.engine, '_save_job', side_effect=save), patch.object(self.engine, '_launch', side_effect=launch):
            result = self.start()
        self.assertEqual(result['id'], JOB_ID)
        self.assertEqual(stages, ['save', 'launch'])
        self.assertEqual(self.read()['activity_key'], self.key())
        self.assertEqual(len(self.engine._order), 1)

    def test_memory_replay_preserves_active_paused_terminal_and_metadata(self):
        self.start()
        job = self.engine._jobs[JOB_ID]
        job.metadata_choice = {0: {'mode': 'manual', 'data': {'title': 'Keep exact metadata'}}}
        job.metadata_done = {0}
        job.metadata_note = 'Keep note'
        job.categories = {0: 'movie'}
        job.lookup_provider, job.lookup_identity, job.minimum_minutes = 'imdb', 'tt1234567', 17
        job.done_parts, job.total_parts = 1, 2
        job.ready = ['/fixture/prepared.mkv']
        job.storage.published = {0: 'Movies/Saved Film (2026)/Saved Film.mkv'}
        for phase, paused in [('queued', False), ('download', False), ('paused', True), ('choose', False), ('failed', False), ('stopped', False)]:
            with self.subTest(phase=phase):
                job.phase, job.user_paused = phase, paused
                self.engine._save_job(job)
                before = self.path.read_bytes()
                with patch.object(self.engine, '_save_job', side_effect=AssertionError('replay saved state')), patch.object(self.engine, '_launch', side_effect=AssertionError('replay relaunched')):
                    replay = self.start(lookup_provider='changed', lookup_identity='changed', minimum_minutes=9999)
                self.assertEqual(replay['id'], JOB_ID)
                self.assertIs(self.engine._jobs[JOB_ID], job)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual((job.phase, job.user_paused, job.done_parts), (phase, paused, 1))
                self.assertEqual((job.lookup_provider, job.lookup_identity, job.minimum_minutes), ('imdb', 'tt1234567', 17))
                self.assertEqual(job.metadata_choice[0]['data']['title'], 'Keep exact metadata')
                self.assertEqual(job.metadata_done, {0})
                self.assertEqual(job.metadata_note, 'Keep note')
                self.assertEqual(len(self.engine._order), 1)

    def test_persisted_replay_attaches_without_saving_launching_or_reapplying(self):
        other = self.new_engine()  # It has not seen the checkpoint yet.
        self.start()
        job = self.engine._jobs[JOB_ID]
        job.phase, job.user_paused, job.done_parts = 'stopped', True, 2
        job.metadata_choice = {'0': {'mode': 'manual', 'data': {'title': 'Saved custom metadata'}}}
        job.metadata_done, job.metadata_note = {0}, 'Saved note'
        job.lookup_provider, job.lookup_identity, job.minimum_minutes = 'tvdb', '1234', 19
        job.categories, job.ready = {0: 'series'}, ['/fixture/prepared.mkv']
        job.meta = _Meta('Kept', [_Item(0, 'Kept.mkv', 5)])
        job.storage.published = {0: 'TV Shows/Kept/Kept.mkv'}
        self.engine._save_job(job)
        before = self.path.read_bytes()
        media = self.root / 'media'
        media.mkdir()
        note, art = media / 'Kept.nfo', media / 'Kept.jpg'
        note.write_bytes(b'<title>Saved metadata</title>')
        art.write_bytes(b'existing artwork')
        files_before = [(path.stat().st_ino, path.read_bytes()) for path in (note, art)]
        with patch.object(other, '_save_job', side_effect=AssertionError('replay saved state')), patch.object(other, '_launch', side_effect=AssertionError('replay relaunched')):
            result = self.start(other, lookup_provider='changed', lookup_identity='changed', minimum_minutes=0)
        loaded = other._jobs[JOB_ID]
        self.assertEqual((result['id'], loaded.phase, loaded.user_paused, loaded.done_parts), (JOB_ID, 'stopped', True, 2))
        self.assertEqual((loaded.lookup_provider, loaded.lookup_identity, loaded.minimum_minutes), ('tvdb', '1234', 19))
        self.assertEqual(loaded.metadata_choice, job.metadata_choice)
        self.assertEqual(loaded.metadata_done, job.metadata_done)
        self.assertEqual(loaded.metadata_note, job.metadata_note)
        self.assertEqual(loaded.categories, job.categories)
        self.assertEqual(loaded.ready, job.ready)
        self.assertEqual(loaded.storage.published, job.storage.published)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([(path.stat().st_ino, path.read_bytes()) for path in (note, art)], files_before)

    def test_public_pause_state_preserves_queued_intent_and_terminal_outcomes(self):
        self.start()
        job = self.engine._jobs[JOB_ID]
        for phase in ('queued', 'download', 'paused', 'choose', 'failed', 'stopped'):
            for paused in (False, True):
                with self.subTest(phase=phase, paused=paused):
                    job.phase, job.user_paused = phase, paused
                    self.engine._save_job(job)
                    before = self.path.read_bytes()
                    expected = ('paused' if paused and phase in {'queued', 'download', 'paused'} else
                                'downloading' if phase == 'download' else phase)
                    public = self.engine.job(JOB_ID)
                    self.assertEqual(public['state'], expected)
                    self.assertEqual(public['pause_reason'], 'user' if expected == 'paused' and paused else '')
                    self.assertEqual((job.phase, job.user_paused), (phase, paused))
                    self.assertEqual(self.path.read_bytes(), before)
        # An engine restart must show the existing queued pause without launching it.
        job.phase, job.user_paused = 'queued', True
        self.engine._save_job(job)
        before = self.path.read_bytes()
        with patch.object(NzbEngine, '_launch', side_effect=AssertionError('No implicit resume')):
            restored = self.new_engine()
            self.assertEqual(restored.job(JOB_ID)['state'], 'paused')
            self.assertTrue(restored._jobs[JOB_ID].user_paused)
            self.assertEqual(restored._jobs[JOB_ID].phase, 'queued')
            self.assertEqual(self.path.read_bytes(), before)

    def test_replay_does_not_require_new_server_pause_or_size_policy(self):
        self.start()
        before = self.path.read_bytes()
        self.engine._servers = lambda: []
        self.engine.stop.set()
        self.engine.config['min_download_bytes'] = 10**9
        with patch.object(self.engine, '_launch', side_effect=AssertionError('replay relaunched')):
            self.assertEqual(self.start()['id'], JOB_ID)
        self.assertEqual(self.path.read_bytes(), before)

    def test_mismatch_corruption_and_symlink_fail_closed_without_writes(self):
        self.assertIsNone(self.read())
        self.start()
        before = self.path.read_bytes()
        for token, raw in [('c' * 32, RAW), (TOKEN, RAW.replace(b'first@invalid', b'other@invalid'))]:
            with self.subTest(token=token), self.assertRaises(EngineError):
                self.engine.start(self.preview(raw=raw)['id'], [0], handoff_token=token, job_id=JOB_ID)
            self.assertEqual(self.path.read_bytes(), before)
        self.path.write_text('{broken')
        with self.assertRaises(EngineError):
            self.read()
        self.assertEqual(self.path.read_text(), '{broken')
        malformed = json.loads(before)
        malformed['files'] = ['invalid file shape']
        self.path.write_text(json.dumps(malformed))
        with self.assertRaises(EngineError):
            self.read()
        self.path.unlink()
        elsewhere = self.root / 'foreign.json'
        elsewhere.write_bytes(before)
        self.path.symlink_to(elsewhere)
        with self.assertRaises(EngineError):
            self.read()
        self.assertEqual(elsewhere.read_bytes(), before)

    def test_same_token_cannot_be_used_for_a_different_job_id(self):
        self.start()
        before = self.path.read_bytes()
        for engine in (self.engine, self.new_engine()):
            with self.assertRaises(EngineError):
                self.start(engine, job_id='c' * 32)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(list(self.data.glob('jobs/*.json'))), 1)

    def test_malformed_present_token_never_restores_or_adopts_as_legacy(self):
        self.start()
        saved = json.loads(self.path.read_text())
        for token in ('', None, False, 123, 'short', 'A' * 32, 'a' * 31):
            with self.subTest(token=token):
                self.path.write_text(json.dumps({**saved, 'handoff_token': token}))
                with self.assertRaises(EngineError) as rejected:
                    self.read()
                self.assertEqual(rejected.exception.status, 409)
                with patch.object(NzbEngine, '_launch') as launch:
                    restored = self.new_engine()
                    self.assertFalse(restored._jobs)
                    self.assertFalse(restored._order)
                    restored._adopt()
                    self.assertFalse(restored._jobs)
                    self.assertFalse(restored._order)
                launch.assert_not_called()
        saved.pop('handoff_token')
        self.path.write_text(json.dumps(saved))
        legacy = self.new_engine()
        self.assertIn(JOB_ID, legacy._jobs, 'missing token keeps the existing legacy restore behavior')
        self.assertEqual(legacy._jobs[JOB_ID].handoff_token, '')

    def test_failed_checkpoint_never_registers_or_launches(self):
        with patch.object(self.engine, '_save_job', side_effect=OSError('disk failed')), patch.object(self.engine, '_launch') as launch:
            with self.assertRaises(OSError):
                self.start()
        self.assertFalse(self.engine._jobs)
        self.assertFalse(self.engine._order)
        self.assertIsNone(self.read())
        launch.assert_not_called()
        self.assertEqual(self.start()['id'], JOB_ID)

    def test_crash_after_checkpoint_before_registration_replays_existing(self):
        other = self.new_engine()
        with patch('backend.downloaders.nzb.engine.os.fsync', side_effect=SystemExit('crash after atomic checkpoint')):
            with self.assertRaises(SystemExit):
                self.start()
        self.assertFalse(self.engine._jobs)
        before = self.path.read_bytes()
        self.assertEqual(self.read()['id'], JOB_ID)
        with patch.object(other, '_save_job', side_effect=AssertionError('replay saved state')), patch.object(other, '_launch', side_effect=AssertionError('replay relaunched')):
            self.assertEqual(self.start(other)['id'], JOB_ID)
        self.assertEqual(self.path.read_bytes(), before)

    def test_two_threads_and_engines_create_once(self):
        other = self.new_engine()
        barrier = threading.Barrier(2)
        results, errors, saves = [], [], []
        original = NzbEngine._save_job

        def save(engine, job):
            saves.append(job.id)
            original(engine, job)

        def run(engine):
            try:
                preview = self.preview(engine)
                barrier.wait(timeout=5)
                results.append(engine.start(preview['id'], [0], handoff_token=TOKEN, job_id=JOB_ID))
            except BaseException as exc:
                errors.append(exc)

        with patch.object(NzbEngine, '_save_job', new=save):
            threads = [threading.Thread(target=run, args=(engine,)) for engine in (self.engine, other)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([row['id'] for row in results], [JOB_ID, JOB_ID])
        self.assertEqual(saves, [JOB_ID])
        self.assertEqual(len(self.engine._order), 1)
        self.assertEqual(len(other._order), 1)

    def test_two_processes_create_one_checkpoint(self):
        script = '''import json,os,sys
from pathlib import Path
from backend.downloaders.nzb.engine import NzbEngine
root=Path(sys.argv[1]);raw=bytes.fromhex(sys.argv[2]);token=sys.argv[3];job_id=sys.argv[4]
engine=NzbEngine(root/'media',root/'data'/'nzb-engine');engine._servers=lambda:[{}]
preview=engine.preview_bytes(raw,'fixture.nzb',check_availability=False)
original=engine._save_job
def save(job):
 with (root/'creates.txt').open('a') as stream:stream.write(job.id+'\\n')
 original(job)
engine._save_job=save
print('READY',flush=True);sys.stdin.readline()
result=engine.start(preview['id'],[0],handoff_token=token,job_id=job_id)
print(json.dumps(result['id']),flush=True)
engine.shutdown()
'''
        environment = dict(os.environ, REELDOCK_DOWNLOADS='worker', PYTHONDONTWRITEBYTECODE='1')
        processes = [subprocess.Popen([sys.executable, '-c', script, str(self.root), RAW.hex(), TOKEN, JOB_ID],
                                      cwd=Path(__file__).resolve().parents[1], env=environment,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        try:
            for process in processes:
                self.assertEqual(process.stdout.readline().strip(), 'READY')
            for process in processes:
                process.stdin.write('\n')
                process.stdin.flush()
            for process in processes:
                output, error = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, error)
                self.assertEqual(json.loads(output.strip()), JOB_ID)
            self.assertEqual((self.root / 'creates.txt').read_text().splitlines(), [JOB_ID])
            self.assertEqual(self.read()['id'], JOB_ID)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_pending_binding_defers_transfer_publication_and_metadata_without_mutation(self):
        self.start()
        job = self.engine._jobs[JOB_ID]
        self.binding()
        job.meta = _Meta('Saved', [_Item(0, 'Saved.mkv', 5)])
        job.storage.published = {0: 'Movies/Saved.mkv'}
        job.progress_stage = 'publishing'
        self.engine._save_job(job)
        self.engine.metadata = Mock()
        self.engine.config['share_metadata'] = True
        before = self.path.read_bytes()
        with patch.object(self.engine, '_save_job', side_effect=AssertionError('pending job was rewritten')), patch.object(self.engine, '_spawn') as spawn, patch.object(self.engine, '_claim_download') as claim:
            self.engine._launch(job)
            self.engine._launch_publication(job)
            self.engine._publish(job)
            self.engine._resume_metadata()
        spawn.assert_not_called()
        claim.assert_not_called()
        self.engine.metadata.assert_not_called()
        self.assertFalse(job.user_paused)
        self.assertEqual(job.error, '')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.engine._handoff_metadata_pending, {JOB_ID})
        for stage, engine, identity in [('prepared', 'nzb', JOB_ID), ('committed', '', JOB_ID), ('committed', 'torrent', JOB_ID), ('committed', 'nzb', 'c' * 32)]:
            self.binding(stage, engine, identity)
            self.assertFalse(self.engine._handoff_ready(job))
        self.binding('committed', 'nzb', JOB_ID)
        self.assertTrue(self.engine._handoff_ready(job))
        self.engine._resume_metadata()
        self.engine.metadata.assert_called_once()
        job.progress_stage = ''
        with patch.object(self.engine, '_claim_download', return_value=True), patch.object(self.engine, '_spawn') as spawn:
            self.engine._launch(job)
        spawn.assert_called_once()
        self.assertTrue(job.running)

    def test_cancelled_or_stale_binding_blocks_but_completed_metadata_remains_ready(self):
        self.start()
        job = self.engine._jobs[JOB_ID]
        for state, generation in [('cancelling', 0), ('cancelled', 0), ('queued', 1), ('complete', 1)]:
            with self.subTest(state=state, generation=generation):
                self.binding('committed', 'nzb', JOB_ID, state, generation)
                self.assertFalse(self.engine._handoff_ready(job))
        self.binding('committed', 'nzb', JOB_ID, 'complete', 0)
        self.assertTrue(self.engine._handoff_ready(job))

    def test_stop_all_generation_change_blocks_even_after_global_hold_released(self):
        self.start()
        job = self.engine._jobs[JOB_ID]
        self.binding('committed', 'nzb', JOB_ID)
        self.assertTrue(self.engine._handoff_ready(job))
        for generation in (1, 'malformed', ''):
            self.binding('committed', 'nzb', JOB_ID, acquisition_generation=generation)
            self.assertFalse(self.engine._handoff_ready(job))
        self.binding('committed', 'nzb', JOB_ID)
        self.assertTrue(self.engine._handoff_ready(job))

    def test_pending_handoff_metadata_does_not_repeat_unrelated_catchup(self):
        self.start()
        handoff = self.engine._jobs[JOB_ID]
        legacy = self.engine.start(self.preview()['id'], [0])['id']
        self.binding()
        for job in (handoff, self.engine._jobs[legacy]):
            job.meta = _Meta('Saved', [_Item(0, 'Saved.mkv', 5)])
            job.storage.published = {0: 'Movies/Saved.mkv'}
        self.engine.metadata = Mock()
        self.engine.config['share_metadata'] = True
        self.engine._resume_metadata()
        self.engine._resume_metadata()
        self.engine._resume_metadata()
        self.assertEqual(self.engine.metadata.call_count, 1)
        self.assertEqual(self.engine.metadata.call_args.args[0].id, legacy)
        self.binding('committed', 'nzb', JOB_ID)
        self.engine._resume_metadata()
        self.engine._resume_metadata()
        self.assertEqual(self.engine.metadata.call_count, 2)
        self.assertEqual(self.engine.metadata.call_args.args[0].id, JOB_ID)

    def test_missing_receipt_database_or_table_defers_handoff_only(self):
        self.start()
        job = self.engine._jobs[JOB_ID]
        path = self.data.parent / 'reeldock.sqlite3'
        self.assertFalse(path.exists())
        self.assertFalse(self.engine._handoff_ready(job))
        self.assertFalse(path.exists(), 'readiness never creates an owner database')
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE unrelated(value TEXT)')
        self.assertFalse(self.engine._handoff_ready(job))
        job.handoff_token = ''
        self.assertTrue(self.engine._handoff_ready(job))


if __name__ == '__main__':
    unittest.main()
