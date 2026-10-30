"""A superseded failure may be dismissed without clearing its replacement."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app import create_app
from backend import activity_clear, activity_items, discover_store, download_history, media_holds
from backend import source_alternatives

ORIGIN = 'https://fixture.example.test'


class NativeAttempts:
    def __init__(self):
        self.items = {}
        self.removed = []

    def add(self, identity, state='failed', generation=7):
        self.items[identity] = dict(id=identity, name='Exact fixture', state=state,
            progress=0, added=10, activity_generation=generation,
            error='Incomplete articles.' if state == 'failed' else '')

    def jobs(self):
        return [dict(item) for item in self.items.values()]

    def activity_key(self, identity):
        return 'native:nzb:' + identity

    def remove(self, identity, timeout):
        self.removed.append(identity)


class ActivityAttemptClearTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / 'media'
        self.media.mkdir()
        self.app = create_app(dict(TESTING=True, DATA_DIR=str(self.root / 'data'), MEDIA_DIR=str(self.media)))
        self.store = self.app.extensions['store']
        self.store.set('public_origin', ORIGIN)
        self.store.set('setup_complete', '1')
        with self.store.connect() as db:
            db.execute("INSERT INTO users VALUES(1,'fixture','unused-test-hash')")
        self.native = NativeAttempts()
        self.app.extensions['nzb_engine'] = self.native
        self.addCleanup(self.app.extensions.pop, 'nzb_engine', None)
        self.account = patch('backend.discover_sources.indexers_for', return_value=[dict(url='https://fixture.invalid/api', api_key='TEST_ONLY')])
        self.account.start()
        self.addCleanup(self.account.stop)
        self.tasks = []
        self.pool = patch.object(source_alternatives._POOL, 'submit', side_effect=self.tasks.append)
        self.pool.start()
        self.addCleanup(self.pool.stop)
        self.addCleanup(self.clear_pending)

    def clear_pending(self):
        with source_alternatives._LOCK:
            source_alternatives._PENDING.difference_update(key for key in tuple(source_alternatives._PENDING) if key[0] == str(self.store.directory))

    def client(self):
        client = self.app.test_client()
        with client.session_transaction(base_url=ORIGIN) as session:
            session.update(user_id=1, csrf='fixture-csrf')
        return client

    def post_clear(self, kind, identity, observed):
        return self.client().post('/api/activity/clear-item', base_url=ORIGIN,
            json=dict(kind=kind, id=identity, observed=observed),
            headers={'Origin': ORIGIN, 'X-CSRF-Token': 'fixture-csrf'})

    def observe(self, kind, identity):
        from backend.activity_observation import capture
        if kind in ('torrent', 'nzb'):
            return capture(kind, self.native.items[identity])
        with self.store.connect() as db:
            row = activity_items._row(db, kind, identity)
            return capture(kind, row['item'], generation=activity_clear.generation(db, kind, identity))

    def current(self, identity):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM discover_requests WHERE id=?', (identity,)).fetchone())

    def dump(self):
        with self.store.connect() as db:
            return tuple(db.iterdump())

    def archive(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM activity_archive ORDER BY id')]

    def failed_source(self, link='binding'):
        self.native.add('old-attempt')
        original = dict(id='old-release', source='nzb', kind='movie', externalId='12345', quality='1080p',
            title='Exact.Fixture.2026.1080p-OLD', size=5_000_000_000, grab='https://fixture.invalid/private-old')
        replacement = dict(original, id='replacement-release', title='Exact.Fixture.2026.1080p-NEW', size=4_800_000_000)
        discover_store.remember_candidates(self.store, [original, replacement])
        request, _ = discover_store.insert_request(self.store, dict(idempotency='fixture-failed', kind='movie',
            title='Exact fixture', state='failed', quality='1080', fallback=False, manual=True, engine='nzb',
            engine_id='old-attempt', episode_key='movie:12345',
            body=dict(externalId='12345', managedReleaseId='old-release')))
        discover_store.add_attempt(self.store, request['id'], 'old-release', 'nzb', '1080p', 'failed', 'Incomplete articles.')
        with self.store.connect() as db:
            db.execute("INSERT INTO managed_media(key,parent,kind,body,updated) VALUES('movie:12345','','movie','{}',1)")
            if link == 'binding':
                db.executemany('INSERT INTO media_request_keys VALUES(?,?)', [(request['id'], 'native:nzb-job:old-attempt'), (request['id'], 'native:nzb:old-attempt')])
            elif link == 'policy':
                db.execute('INSERT INTO movie_download_policy VALUES(?,?,?)', ('nzb', 'old-attempt', json.dumps(dict(requestId=request['id']))))
        source_alternatives.search(self.app, self.store, request['id'])
        with patch.object(source_alternatives, '_search_candidates', return_value=[replacement]):
            self.tasks.pop(0)()
        frame = source_alternatives.status(self.store, request['id'])
        self.assertEqual(frame['state'], 'ready')
        self.assertEqual([row['id'] for row in frame['items']], ['replacement-release'])
        return request['id'], frame['revision']

    def select(self, identity, revision):
        with patch('backend.discovery_engine.schedule', return_value=False):
            return source_alternatives.select(self.app, self.store, identity, 'replacement-release', revision)

    def start_replacement(self, identity):
        self.native.add('new-attempt', 'downloading', 8)
        discover_store.update_request(self.store, identity, state='starting')
        row = self.current(identity)
        self.assertTrue(media_holds.bind_request(self.store, identity, row['stop_generation'],
            ['native:nzb-job:new-attempt', 'native:nzb:new-attempt']))
        discover_store.update_request(self.store, identity, state='downloading', engine='nzb', engine_id='new-attempt')

    def assert_replacement_untouched(self, identity, before):
        self.assertEqual(self.current(identity), before)
        self.assertEqual(self.native.removed, [])
        self.assertFalse(media_holds.blocked(self.store,
            ['request:' + identity, 'managed:movie:12345', 'native:nzb-job:new-attempt', 'native:nzb:new-attempt']))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT hold FROM managed_media WHERE key='movie:12345'").fetchone()[0], 0)

    def test_old_failure_with_only_historical_binding_dismisses_exact_attempt(self):
        identity, revision = self.failed_source('binding')
        observed = self.observe('nzb', 'old-attempt')
        self.select(identity, revision)
        self.start_replacement(identity)
        before = self.current(identity)
        result = self.post_clear('nzb', 'old-attempt', observed)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual((result.json['held'], result.json['stopping'], result.json['cleared']), (False, False, 1))
        self.assert_replacement_untouched(identity, before)
        archived = self.archive()
        self.assertEqual([(row['kind'], row['identity'], row['generation']) for row in archived], [('nzb', 'old-attempt', 7)])
        self.assertEqual(json.loads(archived[0]['data'])['item']['state'], 'failed')
        export = download_history.page(self.store, self.media, 50, plain=True, include_archived=True)
        old = [row for row in export['items'] if row['item'].get('originalId') == 'old-attempt']
        self.assertEqual(len(old), 1)
        self.assertEqual(old[0]['item']['state'], 'failed')
        self.assertTrue(any(row['kind'] == 'discover' and row['item']['id'] == identity and row['item']['state'] == 'downloading' for row in export['items']))
        repeated = self.post_clear('nzb', 'old-attempt', observed)
        self.assertEqual(repeated.status_code, 200, repeated.json)
        self.assertEqual((repeated.json['held'], repeated.json['stopping'], repeated.json['cleared']), (False, False, 0))
        self.assertEqual(len(self.archive()), 1)
        self.assert_replacement_untouched(identity, before)

    def test_old_failure_with_only_old_policy_link_does_not_stop_replacement(self):
        identity, revision = self.failed_source('policy')
        observed = self.observe('nzb', 'old-attempt')
        self.select(identity, revision)
        self.start_replacement(identity)
        before = self.current(identity)
        result = self.post_clear('nzb', 'old-attempt', observed)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertFalse(result.json['held'])
        self.assertFalse(result.json['stopping'])
        self.assert_replacement_untouched(identity, before)
        self.assertEqual([row['identity'] for row in self.archive()], ['old-attempt'])

    def test_old_failure_cannot_hold_selected_replacement_waiting_for_dispatch(self):
        identity, revision = self.failed_source()
        observed = self.observe('nzb', 'old-attempt')
        self.select(identity, revision)
        before = self.current(identity)
        self.assertEqual((before['state'], before['engine_id']), ('waiting', ''))
        result = self.post_clear('nzb', 'old-attempt', observed)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual((result.json['held'], result.json['stopping']), (False, False))
        self.assert_replacement_untouched(identity, before)
        self.assertEqual([row['identity'] for row in self.archive()], ['old-attempt'])

    def test_archived_failure_replay_is_safe_after_same_id_retry_or_job_removal(self):
        identity, revision = self.failed_source()
        observed = self.observe('nzb', 'old-attempt')
        self.select(identity, revision)
        self.start_replacement(identity)
        result = self.post_clear('nzb', 'old-attempt', observed)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertFalse(result.json['held'])
        for present in (True, False):
            with self.subTest(current_job_present=present):
                if present:
                    self.native.add('old-attempt', 'downloading', 8)
                else:
                    self.native.items.pop('old-attempt')
                before = self.dump()
                result = self.post_clear('nzb', 'old-attempt', observed)
                self.assertEqual(result.status_code, 200, result.json)
                self.assertEqual((result.json['held'], result.json['stopping'], result.json['cleared']), (False, False, 0))
                self.assertEqual(self.dump(), before)
                self.assertEqual(self.native.removed, [])
                self.assertFalse(media_holds.blocked(self.store, ['request:' + identity, 'native:nzb-job:old-attempt']))

    def test_stale_same_parent_error_snapshot_after_real_selection_is_409_no_changes(self):
        identity, revision = self.failed_source()
        observed = self.observe('discover', identity)
        self.select(identity, revision)
        self.start_replacement(identity)
        before = self.dump()
        result = self.post_clear('discover', identity, observed)
        self.assertEqual(result.status_code, 409, result.json)
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.native.removed, [])

    def test_fresh_active_clear_still_holds_and_stops_related_work(self):
        identity, revision = self.failed_source()
        self.select(identity, revision)
        self.start_replacement(identity)
        result = self.post_clear('nzb', 'new-attempt', self.observe('nzb', 'new-attempt'))
        self.assertEqual(result.status_code, 200, result.json)
        self.assertTrue(result.json['held'])
        self.assertEqual(self.current(identity)['state'], 'cancelling')
        self.assertIn('new-attempt', self.native.removed)
        self.assertTrue(media_holds.blocked(self.store, ['managed:movie:12345']))
        self.assertTrue(media_holds.blocked(self.store, ['native:nzb-job:new-attempt']))
        self.assertTrue(media_holds.blocked(self.store, ['request:' + identity]))
        self.assertFalse(media_holds.bind_request(self.store, identity, self.current(identity)['stop_generation'], ['native:nzb:automatic-retry']))

    def test_lagging_active_parent_pointing_at_exact_failed_target_is_not_new_replacement(self):
        identity, _revision = self.failed_source()
        discover_store.update_request(self.store, identity, state='downloading', engine='nzb', engine_id='old-attempt')
        result = self.post_clear('nzb', 'old-attempt', self.observe('nzb', 'old-attempt'))
        self.assertEqual(result.status_code, 200, result.json)
        self.assertTrue(result.json['held'])
        self.assertTrue(result.json['stopping'])
        self.assertEqual(self.current(identity)['state'], 'cancelling')
        self.assertEqual(self.native.removed, ['old-attempt'])

    def test_native_same_id_explicit_retry_invalidates_old_failure_observation(self):
        self.native.add('same-native', 'failed', 12)
        observed = self.observe('nzb', 'same-native')
        self.native.add('same-native', 'downloading', 13)
        before = self.dump()
        result = self.post_clear('nzb', 'same-native', observed)
        self.assertEqual(result.status_code, 409, result.json)
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.native.removed, [])
        self.native.add('same-native', 'failed', 13)
        before = self.dump()
        result = self.post_clear('nzb', 'same-native', observed)
        self.assertEqual(result.status_code, 409, result.json)
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.native.removed, [])

    def test_native_retry_while_clear_waits_for_sql_writer_is_revalidated(self):
        self.native.add('same-native', 'failed', 12)
        observed = self.observe('nzb', 'same-native')
        waiting = threading.Event()
        answers = {}
        opened = self.store._open
        def connection():
            db = opened()
            def trace(statement):
                if statement.strip().upper().startswith('BEGIN IMMEDIATE'):
                    waiting.set()
            db.set_trace_callback(trace)
            return db
        def clear():
            try:
                answers['result'] = activity_items.clear_one(self.app, self.store, 'nzb', 'same-native', observed=observed)
            except BaseException as exc:
                answers['error'] = exc
        with self.store.connect() as writer:
            writer.execute('BEGIN IMMEDIATE')
            with patch.object(self.store, '_open', side_effect=connection):
                clearing = threading.Thread(target=clear)
                clearing.start()
                entered = waiting.wait(2)
                # The native engine may save a new lifecycle generation while
                # a SQL writer is busy; validating a pre-transaction snapshot
                # would wrongly clear this fresh attempt after the lock opens.
                self.native.add('same-native', 'downloading', 13)
        clearing.join(4)
        self.assertTrue(entered, answers)
        self.assertFalse(clearing.is_alive())
        self.assertNotIn('result', answers)
        self.assertEqual(getattr(answers.get('error'), 'status', None), 409, answers)
        self.assertEqual(self.native.removed, [])
        self.assertFalse(media_holds.blocked(self.store, ['native:nzb-job:same-native', 'native:nzb:same-native']))
        self.assertEqual(self.archive(), [])

    def test_sql_youtube_and_vod_same_id_new_attempt_reject_old_failure_generation(self):
        for kind in ('youtube', 'm3u'):
            with self.subTest(kind=kind):
                identity = 'abcdefghijk' if kind == 'youtube' else 'fixture-vod'
                with self.store.connect() as db:
                    if kind == 'youtube':
                        db.execute("INSERT INTO downloads(id,title,quality,state,message,created,updated) VALUES(?,?,?,'failed','Original failure',1,1)", (identity, 'Fixture', '1080'))
                        table = 'downloads'
                        active = 'downloading'
                    else:
                        db.execute('INSERT INTO m3u_downloads VALUES(?,?,?,?,?,?,?,?,?,?,?)', (identity, 'fixture-source', 'fixture-path', 'Fixture', 'movie', str(self.media / 'fixture.mkv'), 'failed', 0, 'Original failure', 1, 1))
                        table = 'm3u_downloads'
                        active = 'running'
                observed = self.observe(kind, identity)
                with self.store.connect() as db:
                    db.execute('UPDATE ' + table + ' SET state=?,message=? WHERE id=?', (active, 'Explicit retry', identity))
                before = self.dump()
                result = self.post_clear(kind, identity, observed)
                self.assertEqual(result.status_code, 409, result.json)
                self.assertEqual(self.dump(), before)
                with self.store.connect() as db:
                    db.execute('UPDATE ' + table + " SET state='failed',message='Replacement failure' WHERE id=?", (identity,))
                new_observed = self.observe(kind, identity)
                self.assertEqual(new_observed['state'], observed['state'])
                self.assertGreater(new_observed['generation'], observed['generation'])
                before = self.dump()
                result = self.post_clear(kind, identity, observed)
                self.assertEqual(result.status_code, 409, result.json)
                self.assertEqual(self.dump(), before)

    def test_passive_metadata_progress_does_not_invalidate_observation(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO downloads(id,title,quality,state,message,created,updated) VALUES('abcdefghijk','Fixture','1080','downloading','Initial',1,1)")
        observed = self.observe('youtube', 'abcdefghijk')
        with self.store.connect() as db:
            db.execute("UPDATE downloads SET message='Progress updated',updated=99 WHERE id='abcdefghijk'")
        result = self.post_clear('youtube', 'abcdefghijk', observed)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertTrue(result.json['held'])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM downloads WHERE id='abcdefghijk'").fetchone()[0], 'cancelling')

    def test_selection_writer_wins_before_stale_clear_snapshot_is_validated(self):
        identity, revision = self.failed_source()
        observed = self.observe('discover', identity)
        writer = threading.Event()
        release = threading.Event()
        entered_clear = threading.Event()
        results = {}
        errors = []
        consume = source_alternatives.consume_approval
        clear = activity_items._clear_one
        def hold_selection(*args, **kwargs):
            approved = consume(*args, **kwargs)
            writer.set()
            if not release.wait(3):
                raise AssertionError('Clear did not reach the writer race')
            return approved
        def enter_clear(*args, **kwargs):
            entered_clear.set()
            return clear(*args, **kwargs)
        def run(name, action):
            try:
                results[name] = action()
            except BaseException as exc:
                errors.append(exc)
        with patch.object(source_alternatives, 'consume_approval', side_effect=hold_selection), patch.object(activity_items, '_clear_one', side_effect=enter_clear):
            select = threading.Thread(target=run, args=('select', lambda: self.select(identity, revision)))
            select.start()
            self.assertTrue(writer.wait(2))
            clearing = threading.Thread(target=run, args=('clear', lambda: self.post_clear('discover', identity, observed)))
            clearing.start()
            try:
                self.assertTrue(entered_clear.wait(2))
            finally:
                release.set()
                select.join(4)
                clearing.join(4)
        self.assertFalse(select.is_alive() or clearing.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(results['select']['ok'])
        self.assertEqual(results['clear'].status_code, 409, results['clear'].json)
        self.assertEqual(self.current(identity)['state'], 'waiting')
        self.assertFalse(media_holds.blocked(self.store, ['request:' + identity, 'managed:movie:12345']))

    def test_clear_writer_wins_and_prevents_preapproved_source_selection(self):
        identity, revision = self.failed_source()
        observed = self.observe('discover', identity)
        writer = threading.Event()
        release = threading.Event()
        selecting = threading.Event()
        results = {}
        errors = []
        commit = activity_items._commit
        def held_commit(*args, **kwargs):
            result = commit(*args, **kwargs)
            writer.set()
            if not release.wait(3):
                raise AssertionError('Selection did not enter the writer race')
            return result
        def clear():
            try:
                results['clear'] = self.post_clear('discover', identity, observed)
            except BaseException as exc:
                errors.append(exc)
        def select():
            selecting.set()
            try:
                results['select'] = self.select(identity, revision)
            except ValueError as exc:
                results['refused'] = str(exc)
            except BaseException as exc:
                errors.append(exc)
        with patch.object(activity_items, '_commit', side_effect=held_commit):
            clearing = threading.Thread(target=clear)
            clearing.start()
            self.assertTrue(writer.wait(2))
            selection = threading.Thread(target=select)
            selection.start()
            try:
                self.assertTrue(selecting.wait(2))
            finally:
                release.set()
                clearing.join(4)
                selection.join(4)
        self.assertFalse(clearing.is_alive() or selection.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results['clear'].status_code, 200, results['clear'].json)
        self.assertIn('refused', results)
        self.assertNotIn('select', results)
        self.assertTrue(media_holds.blocked(self.store, ['request:' + identity, 'managed:movie:12345']))


if __name__ == '__main__':
    unittest.main()
