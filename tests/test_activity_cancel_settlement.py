"""Activity Clear settles only after its exact native owner is demonstrably gone."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend import activity_clear, discover_acquire, discover_store, media_holds
from backend.storage import Store

REASON = 'Cleared from Activity.'


class ActivityCancelSettlementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'data')
        discover_store.initialise(self.store)
        activity_clear.initialise(self.store)
        self.app = SimpleNamespace(extensions={})
        for factory in ('backend.nzb._engine', 'backend.torrents._engine'):
            context = patch(factory, side_effect=AssertionError('Settlement must never open a native engine'))
            context.start()
            self.addCleanup(context.stop)
        self.provider = patch.object(discover_acquire, 'submit_nzb', side_effect=AssertionError('Settlement cannot request a provider'))
        self.provider.start()
        self.addCleanup(self.provider.stop)

    def request(self, name='fixture', *, kind='nzb', held=True, state='cancelling', message=REASON, identity=None):
        identity = identity or ('a' * (32 if kind == 'nzb' else 40))
        row, _ = discover_store.insert_request(self.store, dict(id=name, idempotency=name,
            kind='movie', title='Fixture', state=state, quality='1080', fallback=False,
            engine=kind, engine_id=identity, episode_key='movie:' + name, message=message, body={}))
        if held:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                media_holds.hold(db, media_holds.request_keys(row), [('discover', name)])
        return self.current(name)

    def current(self, identity='fixture'):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM discover_requests WHERE id=?', (identity,)).fetchone())

    def checkpoint(self, row):
        return self.store.directory / (row['engine'] + '-engine') / ('jobs' if row['engine'] == 'nzb' else 'torrents') / (row['engine_id'] + '.json')

    def sync(self):
        discover_acquire.sync_jobs(self.app, self.store)

    def test_absent_nzb_and_torrent_settle_without_engine_network_or_hold_release(self):
        for kind in ('nzb', 'torrent'):
            row = self.request(kind, kind=kind)
            self.sync()
            settled = self.current(kind)
            self.assertEqual((settled['state'], settled['message']), ('cancelled', REASON))
            self.assertEqual(settled['engine_id'], row['engine_id'])
            self.assertEqual(settled['stop_generation'], row['stop_generation'])
            self.assertTrue(media_holds.blocked(self.store, media_holds.request_keys(row)))
        self.assertEqual(self.app.extensions, {})
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM discover_attempts').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM activity_archive').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM discover_requests').fetchone()[0], 2)

    def test_any_existing_native_checkpoint_keeps_cancelling(self):
        for kind in ('nzb', 'torrent'):
            row = self.request(kind, kind=kind)
            target = self.checkpoint(row)
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps(dict(id=row['engine_id'], phase='download')))
            self.sync()
            self.assertEqual(self.current(kind), row)
            target.unlink()
            self.sync()
            self.assertEqual(self.current(kind)['state'], 'cancelled')

    def test_live_or_stopping_native_row_prevents_settlement_even_without_checkpoint(self):
        for index, state in enumerate(('downloading', 'paused', 'cancelling', 'stopped', 'failed')):
            row = self.request('job' + str(index), identity=f'{index:032x}')
            engine = SimpleNamespace(jobs=Mock(return_value=[dict(id=row['engine_id'], state=state)]))
            self.app.extensions['nzb_engine'] = engine
            discover_acquire.settle_activity_cancellations(self.app, self.store)
            self.assertEqual(self.current(row['id']), row)
            engine.jobs.assert_called()

    def test_removed_job_with_live_private_owner_is_not_treated_as_finished(self):
        row = self.request()
        job = SimpleNamespace(removed=True, running=True)
        self.app.extensions['nzb_engine'] = SimpleNamespace(_jobs={row['engine_id']: job}, jobs=Mock(return_value=[]))
        self.sync()
        self.assertEqual(self.current(), row)

    def worker_metadata(self, row):
        engine = SimpleNamespace(_jobs={row['engine_id']: SimpleNamespace(phase='queued', running=False)},
            jobs=Mock(return_value=[dict(id=row['engine_id'], state='queued')]))
        self.app.extensions[row['engine'] + '_engine'] = engine
        self.app.config = {'PROCESS_ROLE': 'worker'}
        return engine

    def test_worker_only_retained_nzb_metadata_does_not_block_absent_checkpoint(self):
        row = self.request()
        engine = self.worker_metadata(row)
        with patch.dict(discover_acquire.os.environ, {'REELDOCK_DOWNLOADS': 'worker'}):
            self.sync()
        current = self.current()
        self.assertEqual((current['state'], current['message']), ('cancelled', REASON))
        self.assertEqual(current['stop_generation'], row['stop_generation'])
        self.assertTrue(media_holds.blocked(self.store, media_holds.request_keys(row)))
        engine.jobs.assert_not_called()

    def test_worker_nzb_metadata_exception_still_requires_absent_checkpoint(self):
        row = self.request()
        self.worker_metadata(row)
        target = self.checkpoint(row)
        target.parent.mkdir(parents=True)
        target.write_text('Still owned by the web downloader')
        with patch.dict(discover_acquire.os.environ, {'REELDOCK_DOWNLOADS': 'worker'}):
            self.sync()
        self.assertEqual(self.current(), row)
        target.unlink()
        target.symlink_to(self.root / 'missing-native-file')
        with patch.dict(discover_acquire.os.environ, {'REELDOCK_DOWNLOADS': 'worker'}):
            self.sync()
        self.assertEqual(self.current(), row)

    def test_one_worker_role_flag_alone_does_not_ignore_nzb_owner(self):
        row = self.request()
        engine = self.worker_metadata(row)
        for environment, config in (('worker', 'web'), ('web', 'worker'), ('worker', None), ('web', 'web')):
            with self.subTest(environment=environment, config=config):
                self.app.config = {'PROCESS_ROLE': config} if config else {}
                with patch.dict(discover_acquire.os.environ, {'REELDOCK_DOWNLOADS': environment}):
                    self.sync()
                self.assertEqual(self.current(), row)
        engine.jobs.assert_not_called()  # The retained private owner blocks first.

    def test_torrent_worker_owner_is_never_treated_as_nzb_metadata(self):
        row = self.request(kind='torrent')
        self.worker_metadata(row)
        with patch.dict(discover_acquire.os.environ, {'REELDOCK_DOWNLOADS': 'worker'}):
            self.sync()
        self.assertEqual(self.current(), row)

    def test_live_engine_inspection_errors_fail_closed(self):
        row = self.request()
        self.app.extensions['nzb_engine'] = SimpleNamespace(jobs=Mock(side_effect=RuntimeError('Fixture engine stopping')))
        self.sync()
        self.assertEqual(self.current(), row)
        for malformed in (None, [{}], [None]):
            with self.subTest(malformed=malformed):
                self.app.extensions['nzb_engine'] = SimpleNamespace(jobs=lambda: malformed)
                self.sync()
                self.assertEqual(self.current(), row)

    def test_unheld_request_direct_cancel_and_other_engines_are_unchanged(self):
        rows = [self.request('unheld', held=False, identity='a' * 32),
            self.request('direct', message='Cancelled by the owner.', identity='b' * 32),
            self.request('other-engine', kind='vod', identity='opaque-vod')]
        self.sync()
        for row in rows:
            self.assertEqual(self.current(row['id']), row)

    def test_symbolic_checkpoint_and_parent_never_prove_absence(self):
        row = self.request()
        target = self.checkpoint(row)
        target.parent.mkdir(parents=True)
        target.symlink_to(self.root / 'missing-file')
        self.sync()
        self.assertEqual(self.current(), row)
        target.unlink()
        target.parent.rmdir()
        target.parent.symlink_to(self.root / 'missing-folder')
        self.sync()
        self.assertEqual(self.current(), row)

    def test_unreadable_checkpoint_path_fails_closed(self):
        row = self.request()
        with patch.object(discover_acquire.os, 'open', side_effect=PermissionError('Fixture inaccessible directory')):
            self.sync()
        self.assertEqual(self.current(), row)

    def test_malformed_native_identity_never_walks_outside_job_directory(self):
        row = self.request(identity='../outside')
        with patch.object(discover_acquire.os, 'open', side_effect=AssertionError('Malformed identity touched filesystem')):
            self.sync()
        self.assertEqual(self.current(), row)

    def during_absence_check(self, action):
        original = discover_acquire._native_checkpoint_absent
        called = []
        def check(*args):
            answer = original(*args)
            if not called:
                called.append(True)
                action()
            return answer
        return patch.object(discover_acquire, '_native_checkpoint_absent', side_effect=check)

    def test_explicit_replacement_wins_before_writer_fence(self):
        row = self.request()
        def replace():
            media_holds.resume(self.store, ['request:' + row['id']])
            with self.store.connect() as db:
                db.execute("UPDATE discover_requests SET state='downloading',message='Replacement active',engine_id=?,stop_generation=stop_generation+1 WHERE id=?", ('b' * 32, row['id']))
        with self.during_absence_check(replace):
            self.sync()
        current = self.current()
        self.assertEqual((current['state'], current['engine_id'], current['message']), ('downloading', 'b' * 32, 'Replacement active'))
        self.assertEqual(current['stop_generation'], row['stop_generation'] + 1)
        self.assertFalse(media_holds.blocked(self.store, ['request:' + row['id']]))

    def test_same_state_new_generation_is_not_overwritten(self):
        row = self.request()
        def advance():
            with self.store.connect() as db:
                db.execute('UPDATE discover_requests SET stop_generation=stop_generation+1 WHERE id=?', (row['id'],))
        with self.during_absence_check(advance):
            self.sync()
        self.assertEqual(self.current()['state'], 'cancelling')
        self.assertEqual(self.current()['stop_generation'], row['stop_generation'] + 1)

    def test_hold_removed_before_writer_fence_prevents_settlement(self):
        row = self.request()
        with self.during_absence_check(lambda: media_holds.resume(self.store, ['request:' + row['id']])):
            self.sync()
        self.assertEqual(self.current(), row)

    def test_checkpoint_appearing_before_writer_fence_prevents_settlement(self):
        row = self.request()
        target = self.checkpoint(row)
        def appear():
            target.parent.mkdir(parents=True)
            target.write_text('Fixture checkpoint exists')
        with self.during_absence_check(appear):
            self.sync()
        self.assertEqual(self.current(), row)


if __name__ == '__main__':
    unittest.main()
