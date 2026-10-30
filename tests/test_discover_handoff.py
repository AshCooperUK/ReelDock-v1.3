"""Real process-loss boundaries and cancellation fences for durable handoffs."""
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from backend import acquisition, discover_handoff as handoff, discover_store as ds, media_holds
from backend.downloaders.nzb.engine import NzbEngine
import test_engine_settings as fixture


MANIFEST = b'<nzb><file subject="&quot;Fixture.2026.mkv&quot; yEnc (1/1)"><groups><group>fixture</group></groups><segments><segment bytes="12" number="1">handoff@fixture.invalid</segment></segments></file></nzb>'


class DiscoverHandoffTests(unittest.TestCase):
    setUp = fixture.EngineSettingsTests.setUp

    def request(self):
        return ds.insert_request(self.store, dict(idempotency='handoff-fixture',
            kind='movie', title='Exact saved title', quality='2160', fallback=False,
            episode_key='movie:345', body=dict(discoveryEngine=1, externalId='345',
            item=dict(id='imdb:movie:345', kind='movie', title='Exact saved title', year='1999'),
            savedMetadata=dict(choice='manual', text='Unchanged metadata'))))[0]

    def current(self, identity):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM discover_requests WHERE id=?', (identity,)).fetchone())

    def receipt(self, identity):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM discover_handoffs WHERE request_id=?', (identity,)).fetchone())

    def native(self):
        spawn = patch.object(NzbEngine, '_spawn'); spawn.start(); self.addCleanup(spawn.stop)
        engine = NzbEngine(Path(self.tmp.name) / 'media', self.store.directory / 'nzb-engine')
        self.addCleanup(engine.shutdown)
        servers = patch.object(engine, '_servers', return_value=[{}]); servers.start(); self.addCleanup(servers.stop)
        preview = engine.preview_bytes(MANIFEST, 'fixture.nzb', check_availability=False)
        return engine, preview

    def lose_process(self, point):
        row = self.request()
        engine, preview = self.native()
        identity = engine.preview_activity_key(preview['id'], None)
        pid = os.fork()
        if pid == 0:
            try:
                with handoff.locked(self.store, row['id'], blocking=True):
                    generation = ds.claim_start(self.store, row['id'])
                    if point == 'claim': os._exit(73)
                    receipt = handoff.prepare_nzb(self.store, row['id'], generation, identity, 'fixture-release')
                    if point == 'prepared': os._exit(73)
                    job = engine.start(preview['id'], None, 'Exact saved title (1999)',
                        lookup_provider='imdb', lookup_identity='tt0000345',
                        handoff_token=receipt['token'], job_id=receipt['job_id'])
                    if point == 'checkpoint': os._exit(73)
                    if not ds.commit_engine(self.store, row['id'], generation, 'nzb', job['id'], 'Queued'):
                        os._exit(75)
                    os._exit(73)
            except BaseException:
                os._exit(74)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 73, 'fixture child reached the chosen crash point')
        receipt = self.receipt(row['id'])
        checkpoint = engine.state_dir / (receipt['job_id'] + '.json')
        saved = checkpoint.read_bytes() if checkpoint.is_file() else None
        result = handoff.recover(self.app, self.store)
        current = self.current(row['id'])
        self.assertEqual(current['body'], row['body'])
        self.assertEqual((current['title'], current['quality'], current['fallback']),
                         (row['title'], row['quality'], row['fallback']))
        if point in ('claim', 'prepared'):
            self.assertEqual((current['state'], result['requeued']), ('searching', 1))
            self.assertEqual(list(engine.state_dir.glob('*.json')), [])
        else:
            self.assertEqual((current['state'], current['engine_id']), ('queued', receipt['job_id']))
            self.assertEqual(self.receipt(row['id'])['stage'], 'committed')
            self.assertEqual(checkpoint.read_bytes(), saved)
            self.assertEqual(len(list(engine.state_dir.glob('*.json'))), 1)
            self.assertEqual(handoff.recover(self.app, self.store)['attached'], 0, 'recovery replay leaves the same job')

    def test_process_loss_after_claim_before_source_preparation(self):
        self.lose_process('claim')

    def test_process_loss_after_receipt_before_native_checkpoint(self):
        self.lose_process('prepared')

    def test_process_loss_after_checkpoint_before_binding_reuses_exact_job(self):
        self.lose_process('checkpoint')

    def test_process_loss_after_binding_does_not_requeue_or_create_another_job(self):
        self.lose_process('bound')

    def test_slow_live_creator_is_not_recovered_by_another_thread(self):
        row = self.request(); ready = threading.Event(); release = threading.Event()
        def creator():
            with handoff.locked(self.store, row['id'], blocking=True):
                ds.claim_start(self.store, row['id']); ready.set(); release.wait(5)
        thread = threading.Thread(target=creator)
        thread.start()
        self.addCleanup(lambda: (release.set(), thread.join(6)))
        self.assertTrue(ready.wait(5))
        self.assertEqual(handoff.recover(self.app, self.store)['deferred'], 1)
        self.assertEqual(self.current(row['id'])['state'], 'starting')
        release.set(); thread.join(5)
        self.assertEqual(handoff.recover(self.app, self.store)['requeued'], 1)

    def test_slow_creator_cross_process_lock_released_only_after_process_exit(self):
        row = self.request(); reader, writer = os.pipe(); stop_reader, stop_writer = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(reader); os.close(stop_writer)
            with handoff.locked(self.store, row['id'], blocking=True):
                ds.claim_start(self.store, row['id'])
                os.write(writer, b'1'); os.close(writer)
                os.read(stop_reader, 1)
                os._exit(0)
        os.close(writer); os.close(stop_reader)
        try:
            self.assertEqual(os.read(reader, 1), b'1')
            self.assertEqual(handoff.recover(self.app, self.store)['deferred'], 1)
            self.assertEqual(self.current(row['id'])['state'], 'starting')
        finally:
            os.close(reader); os.write(stop_writer, b'1'); os.close(stop_writer)
            os.waitpid(pid, 0)
        self.assertEqual(handoff.recover(self.app, self.store)['requeued'], 1)

    def test_clear_and_stop_all_generations_fence_saved_handoff(self):
        for action in ('cancel', 'hold', 'stop-resume'):
            with self.subTest(action=action):
                with self.store.connect() as db:
                    db.execute('DELETE FROM discover_requests'); db.execute('DELETE FROM discover_handoffs')
                row = self.request(); generation = ds.claim_start(self.store, row['id'])
                if action == 'cancel': ds.cancel_request(self.store, row['id'])
                elif action == 'hold':
                    with self.store.connect() as db:
                        media_holds.hold(db, 'request:' + row['id'], [('discover', row['id'])])
                else:
                    acquisition.begin_hold(self.store); acquisition.resume(self.store)
                self.assertFalse(ds.commit_engine(self.store, row['id'], generation, 'youtube', 'fixture0001', 'Queued'))
                self.assertEqual(handoff.recover(self.app, self.store)['attached'], 0)
                self.assertIn(self.current(row['id'])['state'], ('cancelled','cancelling'))
                self.assertEqual(self.current(row['id'])['body'], row['body'])

    def test_late_bookkeeping_error_cannot_turn_a_bound_download_into_a_new_search(self):
        from backend import discovery_engine as engine, discover_acquire as acquire
        row = self.request()
        directory=self.store.directory/'nzb-engine';directory.mkdir(exist_ok=True)
        (directory/'settings.json').write_text(json.dumps(dict(servers=[dict(enabled=True)])))
        release=dict(id='fixture-release',externalId='345',quality='2160p',title='Fixture.2160p',size=12)
        def submit(app,store,candidate,request,generation):
            self.assertTrue(ds.commit_engine(store,request['id'],generation,'nzb','a'*32,'Queued'))
        def bookkeeping(*args):
            if args[5]=='queued':raise OSError('Fixture bookkeeping interrupted after binding')
        with patch.object(engine.sources,'indexers_for',return_value=['fixture']), \
             patch.object(acquire,'search_for',return_value=[release]), \
             patch.object(acquire,'choose',return_value=[release]), \
             patch('backend.settings.nzb.selection_limits',return_value=(0,100000)), \
             patch.object(acquire,'submit_nzb',side_effect=submit), \
             patch.object(acquire,'add_attempt',side_effect=bookkeeping):
            engine._resolve(self.app,self.store,row)
        saved=self.current(row['id']);receipt=self.receipt(row['id'])
        self.assertEqual((saved['state'],saved['engine_id']),('queued','a'*32))
        self.assertEqual(receipt['stage'],'committed')
        self.assertIsNone(ds.claim_start(self.store,row['id']))
        self.assertEqual(self.receipt(row['id'])['token'],receipt['token'])

    def test_legacy_unverifiable_start_requires_attention_without_metadata_change(self):
        row = self.request()
        with self.store.connect() as db:
            db.execute("UPDATE discover_requests SET state='starting',updated=? WHERE id=?", (time.time() - 700, row['id']))
        self.assertEqual(handoff.recover(self.app, self.store)['attention'], 1)
        current = self.current(row['id'])
        self.assertEqual(current['state'], 'failed'); self.assertIn('retry explicitly', current['message'])
        self.assertEqual(current['body'], row['body'])

    def test_malformed_checkpoint_is_attention_not_absent_job_retry(self):
        row = self.request(); engine, preview = self.native()
        generation = ds.claim_start(self.store, row['id'])
        receipt = handoff.prepare_nzb(self.store, row['id'], generation,
            engine.preview_activity_key(preview['id'], None), 'fixture')
        path = engine.state_dir / (receipt['job_id'] + '.json'); path.write_bytes(b'{broken')
        self.assertEqual(handoff.recover(self.app, self.store)['attention'], 1)
        self.assertEqual(path.read_bytes(), b'{broken')
        self.assertEqual(self.current(row['id'])['state'], 'failed')


if __name__ == '__main__':
    unittest.main()
