"""Cancellation fencing and the Stop all downloads hold."""
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.storage import Store
from backend import discover_store, discover_acquire, media_holds
from backend.acquisition import begin_hold, ensure_open, held, resume, stop_all, AcquisitionHeld
from backend.downloaders.nzb.engine import NzbEngine, _Job


class CancelFenceTests(unittest.TestCase):
    def test_cancelled_request_is_not_restarted(self):
        with TemporaryDirectory() as directory:
            store = Store(directory)
            discover_store.initialise(store)
            row, _created = discover_store.insert_request(store, dict(
                idempotency='audit-cancel-race', kind='movie', title='Audit fixture',
                quality='1080', fallback=False, episode_key='movie:123',
                body={'externalId': '123', 'source': 'NZB first'}))
            discover_store.update_request(store, row['id'], state='cancelled', message='Cancelled by user')
            calls = []

            def submit(*_args, **_kwargs):
                calls.append('ENGINE_STARTED')
                return {'engine': 'nzb', 'id': 'fixture-only'}

            with patch.object(discover_acquire, 'submit_nzb', submit):
                discover_acquire.resolve_release(None, store, row, [dict(
                    id='candidate', externalId='123', quality='1080p', title='Audit.1080p', size=100)])
            with store.connect() as db:
                state = db.execute('SELECT state FROM discover_requests WHERE id=?', (row['id'],)).fetchone()['state']
            self.assertEqual(calls, [])
            self.assertEqual(state, 'cancelled')

    def test_cancel_between_start_and_commit_stops_the_job(self):
        with TemporaryDirectory() as directory:
            store = Store(directory)
            discover_store.initialise(store)
            media_holds.initialise(store)
            row, _created = discover_store.insert_request(store, dict(
                idempotency='audit-cancel-mid', kind='movie', title='Audit fixture',
                quality='1080', fallback=False, episode_key='movie:456',
                body={'externalId': '456', 'source': 'NZB first'}))
            removed = []
            created = []

            class Engine:
                def __init__(self):
                    self._jobs = {}

                def preview_bytes(self, _data, _name):
                    return {'accepted': True, 'on_server': 'present', 'id': 'preview'}

                def preview_activity_key(self, preview_id, indexes=None):
                    assert preview_id == 'preview' and indexes is None
                    return 'native:nzb:'+'a'*64

                def start(self, _preview, _indexes, _title, *, lookup_provider="", lookup_identity="", handoff_token="", job_id=""):
                    assert len(handoff_token)==len(job_id)==32
                    created.append(job_id)
                    discover_store.cancel_request(store, row['id'])
                    return {'id': job_id, 'name': 'Audit fixture', 'state': 'queued'}

                def remove(self, job_id, timeout=20):
                    removed.append(job_id)

            with patch('backend.discover_sources.fetch', return_value=b'<nzb></nzb>'), \
                    patch('backend.nzb._engine', return_value=Engine()), \
                    patch('backend.discover_acquire.candidate', return_value={'grab': 'https://example.test/nzb'}):
                discover_acquire.resolve_release(None, store, row, [dict(
                    id='candidate', externalId='456', quality='1080p', title='Audit.1080p', size=100, grab='https://example.test/nzb')])
            with store.connect() as db:
                saved = db.execute('SELECT state, engine_id FROM discover_requests WHERE id=?', (row['id'],)).fetchone()
            self.assertEqual(len(created),1)
            self.assertEqual(removed, created)
            self.assertEqual(saved['state'], 'cancelling')
            self.assertEqual(saved['engine_id'], '')


class HoldTests(unittest.TestCase):
    def test_hold_blocks_a_new_enqueue_until_resume(self):
        with TemporaryDirectory() as directory:
            store = Store(directory)
            begin_hold(store)
            self.assertTrue(held(store))
            with self.assertRaises(AcquisitionHeld):
                ensure_open(store)
            resume(store)
            self.assertFalse(held(store))
            ensure_open(store)

    def test_stop_all_cancels_a_searching_request_and_stays_held(self):
        with TemporaryDirectory() as directory:
            store = Store(directory)
            media = Path(directory) / 'media'
            media.mkdir()
            store.set('storage_base', str(media))
            discover_store.initialise(store)
            row, _created = discover_store.insert_request(store, dict(
                idempotency='stop-all-1', kind='movie', title='Queued film',
                quality='1080', fallback=False, episode_key='movie:789',
                body={'externalId': '789'}))

            class App:
                testing = True
                extensions = {}
                config = {}

            app = App()
            report = stop_all(app, store)
            self.addCleanup(lambda: [engine.shutdown() for engine in app.extensions.values() if hasattr(engine, 'shutdown')])
            self.assertTrue(report['held'])
            self.assertGreaterEqual(report['sources']['discover']['stopped'], 1)
            with store.connect() as db:
                state = db.execute('SELECT state FROM discover_requests WHERE id=?', (row['id'],)).fetchone()['state']
            self.assertEqual(state, 'cancelled')
            self.assertTrue(held(store))


class NzbRemoveTests(unittest.TestCase):
    def test_remove_joins_the_worker_before_deleting_partials(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            engine = NzbEngine(folder / 'library', folder / 'data')
            self.addCleanup(engine.shutdown)
            job = _Job(id='job', name='Film', files=[], selected=set())
            job.phase = 'download'
            job.running = True
            job.engine = engine
            engine._jobs[job.id] = job
            engine._order.append(job)
            work = engine.work_dir / job.id
            work.mkdir()
            (work / 'partial.bin').write_bytes(b'x')
            started = threading.Event()
            release = threading.Event()

            def block():
                started.set()
                release.wait(5)
                work.mkdir(parents=True, exist_ok=True)
                (work / 'again.bin').write_bytes(b'y')

            job.worker = threading.Thread(target=block)
            job.worker.start()
            started.wait(2)
            threading.Thread(target=release.set, daemon=True).start()
            engine.remove(job.id, timeout=2)
            self.assertFalse(job.worker.is_alive())
            self.assertFalse(work.exists())

    def test_remove_leaves_partials_when_the_worker_does_not_stop(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            engine = NzbEngine(folder / 'library', folder / 'data')
            self.addCleanup(engine.shutdown)
            job = _Job(id='stuck', name='Film', files=[], selected=set())
            job.phase = 'download'
            job.running = True
            engine._jobs[job.id] = job
            engine._order.append(job)
            work = engine.work_dir / job.id
            work.mkdir()
            (work / 'partial.bin').write_bytes(b'x')
            started = threading.Event()

            def block():
                started.set()
                time.sleep(5)

            job.worker = threading.Thread(target=block, daemon=True)
            job.worker.start()
            started.wait(2)
            from backend.downloaders.nzb.engine import EngineError
            with self.assertRaises(EngineError):
                engine.remove(job.id, timeout=0.2)
            self.assertTrue((work / 'partial.bin').is_file())


if __name__ == '__main__':
    unittest.main()
