"""Discover's database queues and their request binding commit as one unit."""
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from backend import discover_store as ds, discovery_engine as engine, downloads
from backend.app import create_app


class ProcessLost(BaseException):
    """An injected process loss must escape normal fallback error handling."""


class DiscoverFallbackHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.media = root / 'media'
        self.media.mkdir()
        self.app = create_app(dict(TESTING=True, DATA_DIR=str(root / 'data'),
            MEDIA_DIR=str(self.media), REELDOCK_MEDIA_ROOT=str(self.media)))
        self.store = self.app.extensions['store']
        self.store.set('setup_complete', '1')
        self.item = dict(id='imdb:movie:12', externalId='12', kind='movie',
                         title='Fixture Film', year='2026')
        self.video = dict(id='fixture0001', title='Fixture Film 2026 Full Movie',
                         state='published', channel='fixture', date='2026-01-01')
        self.row, _ = ds.insert_request(self.store, dict(idempotency='fallback-fixture',
            kind='movie', title='Fixture Film', episode_key='movie:12', quality='1080',
            fallback=False, state='searching', body=dict(item=self.item, year='2026', discoveryEngine=1)))
        self.body = json.loads(self.row['body'])
        self.entry = dict(source='fixture-source', path='Movie_VOD/Fixture.strm')

    def current(self):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM discover_requests WHERE id=?', (self.row['id'],)).fetchone())

    def count(self, table):
        with self.store.connect() as db:
            return db.execute('SELECT count(*) FROM ' + table).fetchone()[0]

    def receipt(self):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM discover_handoffs WHERE request_id=?', (self.row['id'],)).fetchone())

    def invoke(self, kind):
        with ExitStack() as stack:
            if kind == 'vod':
                stack.enter_context(patch('backend.movie_options.policy', return_value=dict(
                    minimum=42, quality='1080', fallback=False)))
                stack.enter_context(patch('backend.m3u_downloads.lookup', return_value=(
                    dict(title='Fixture source', kind='movies', folder='fixture'), 'https://fixture.invalid/video.mp4')))
                return engine.start_vod(self.app, self.store, self.row, self.item, self.entry)
            stack.enter_context(patch('backend.youtube_discover._search', return_value=dict(items=[dict(
                videoId=self.video['id'], title=self.video['title'])])))
            stack.enter_context(patch('backend.catalogue.Importer.api', return_value=dict(items=[dict(
                id=self.video['id'], snippet=dict(title=self.video['title'], liveBroadcastContent='none'),
                contentDetails=dict(duration='PT1H30M'))])))
            return engine.fallback_youtube(self.app, self.store, self.row, self.body)

    def queue_table(self, kind):
        return 'm3u_downloads' if kind == 'vod' else 'downloads'

    def assert_private_pending_writes(self, kind, transaction):
        self.assertIsNotNone(transaction)
        self.assertTrue(transaction.in_transaction)
        table = self.queue_table(kind)
        self.assertEqual(transaction.execute('SELECT count(*) FROM ' + table).fetchone()[0], 1)
        # Another connection cannot see a queue entry before its Discover binding.
        self.assertEqual(self.count(table), 0)
        self.assertEqual(self.current()['state'], 'starting')
        self.assertEqual(self.receipt()['stage'], 'claimed')
        if kind == 'youtube':
            self.assertEqual(transaction.execute('SELECT allow_lower FROM discover_quality_policy WHERE id=?',
                (self.video['id'],)).fetchone()[0], 0)
            writer = transaction.execute('SELECT body FROM discover_requests WHERE id=?', (self.row['id'],)).fetchone()[0]
            self.assertEqual(json.loads(writer)['videoId'], self.video['id'])
            self.assertNotIn('videoId', json.loads(self.current()['body']))
        else:
            policy = json.loads(transaction.execute("SELECT body FROM movie_download_policy WHERE engine='vod'").fetchone()[0])
            self.assertEqual(policy, dict(minimum=42, quality='1080', fallback=False, requestId=self.row['id']))
            target = transaction.execute('SELECT destination FROM m3u_downloads').fetchone()[0]
            self.assertTrue(Path(target).is_relative_to(self.media / 'Movies'))

    def check_atomic_success(self, kind):
        commit = ds.commit_engine

        def observe(*args, transaction=None, **kwargs):
            self.assert_private_pending_writes(kind, transaction)
            result = commit(*args, transaction=transaction, **kwargs)
            self.assertTrue(result)
            # Even request binding/receipt completion stay invisible until the
            # queue writer's transaction exits successfully.
            self.assertEqual(self.current()['state'], 'starting')
            self.assertEqual(self.receipt()['stage'], 'claimed')
            self.assertEqual(transaction.execute('SELECT stage FROM discover_handoffs WHERE request_id=?',
                (self.row['id'],)).fetchone()[0], 'committed')
            return result

        with patch.object(ds, 'commit_engine', side_effect=observe):
            self.assertTrue(self.invoke(kind))
        row = self.current()
        self.assertEqual((row['state'], row['engine']), ('queued', kind))
        receipt = self.receipt()
        self.assertEqual((receipt['stage'], receipt['engine'], receipt['job_id']), ('committed', kind, row['engine_id']))
        with self.store.connect() as db:
            queued = db.execute('SELECT * FROM ' + self.queue_table(kind)).fetchone()
            self.assertEqual(queued['id'], row['engine_id'])
        if kind == 'youtube':
            self.assertEqual(json.loads(row['body'])['videoId'], self.video['id'])
            self.assertFalse(ds.quality_policy(self.store, self.video['id']))

    def test_youtube_queue_policy_body_and_binding_are_atomic(self):
        self.check_atomic_success('youtube')

    def test_vod_queue_policy_destination_and_binding_are_atomic(self):
        self.check_atomic_success('vod')

    def check_process_loss(self, kind):
        commit = ds.commit_engine

        def lost(*args, transaction=None, **kwargs):
            self.assert_private_pending_writes(kind, transaction)
            self.assertTrue(commit(*args, transaction=transaction, **kwargs))
            raise ProcessLost()

        with patch.object(ds, 'commit_engine', side_effect=lost), self.assertRaises(ProcessLost):
            self.invoke(kind)
        self.assertEqual(self.count(self.queue_table(kind)), 0)
        row = self.current()
        self.assertEqual((row['state'], row['engine'], row['engine_id']), ('starting', '', ''))
        self.assertNotIn('videoId', json.loads(row['body']))
        self.assertEqual(self.count('discover_quality_policy'), 0)
        self.assertEqual(self.count('movie_download_policy'), 0)
        self.assertEqual((self.receipt()['stage'], self.receipt()['engine'], self.receipt()['job_id']), ('claimed', '', ''))

    def test_process_loss_rolls_back_youtube_queue_policy_and_binding(self):
        self.check_process_loss('youtube')

    def test_process_loss_rolls_back_vod_queue_policy_and_binding(self):
        self.check_process_loss('vod')

    def check_rejected_binding(self, kind):
        def reject(*args, transaction=None, **kwargs):
            self.assert_private_pending_writes(kind, transaction)
            return False

        with patch.object(ds, 'commit_engine', side_effect=reject):
            self.assertFalse(self.invoke(kind))
        self.assertEqual(self.count(self.queue_table(kind)), 0)
        self.assertEqual(self.count('discover_quality_policy'), 0)
        self.assertEqual(self.count('movie_download_policy'), 0)
        self.assertEqual(self.current()['state'], 'starting')
        self.assertEqual(self.receipt()['stage'], 'claimed')

    def test_rejected_youtube_generation_does_not_leave_or_cancel_an_unbound_queue(self):
        self.check_rejected_binding('youtube')

    def test_rejected_vod_generation_does_not_leave_or_cancel_an_unbound_queue(self):
        self.check_rejected_binding('vod')

    def test_existing_completed_youtube_request_is_not_requeued_on_reentry(self):
        self.assertTrue(self.invoke('youtube'))
        with self.store.connect() as db:
            db.execute("UPDATE downloads SET state='complete',progress=100,message='Saved'")
            db.execute("UPDATE discover_requests SET state='complete',progress=100")
            before = dict(db.execute('SELECT * FROM downloads').fetchone())
        self.assertFalse(self.invoke('youtube'))
        with self.store.connect() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM downloads').fetchone()), before)

    def check_stop_then_resume_during_preparation(self, kind):
        from backend import acquisition, media_holds
        bind = media_holds.bind_request

        def interrupted(*args, **kwargs):
            result = bind(*args, **kwargs)
            self.assertTrue(result)
            # Clearing the global hold does not authorise the old attempt:
            # its persisted acquisition generation must still be rejected.
            acquisition.begin_hold(self.store)
            acquisition.resume(self.store)
            return result

        with patch.object(media_holds, 'bind_request', side_effect=interrupted):
            self.assertFalse(self.invoke(kind))
        self.assertEqual(self.count(self.queue_table(kind)), 0)
        self.assertEqual(self.receipt()['stage'], 'claimed')
        self.assertEqual(self.count('discover_quality_policy'), 0)
        self.assertEqual(self.count('movie_download_policy'), 0)

    def test_youtube_preparation_cannot_survive_stop_then_resume_generation(self):
        self.check_stop_then_resume_during_preparation('youtube')

    def test_vod_preparation_cannot_survive_stop_then_resume_generation(self):
        self.check_stop_then_resume_during_preparation('vod')

    def test_supplied_enqueue_transaction_rolls_back_without_committing(self):
        with self.assertRaises(ProcessLost):
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                downloads.enqueue(self.store, self.video, '720', transaction=db)
                self.assertEqual(self.count('downloads'), 0)
                self.assertEqual(db.execute('SELECT quality FROM downloads').fetchone()[0], '720')
                raise ProcessLost()
        self.assertEqual(self.count('downloads'), 0)


if __name__ == '__main__':
    unittest.main()
