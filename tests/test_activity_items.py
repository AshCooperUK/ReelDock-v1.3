"""Per-item Clear fences exact linked work, preserving files and export facts."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app import create_app
from backend import activity_clear, activity_items, download_history, media_holds

ORIGIN = 'https://dev.example.com'


class Native:
    def __init__(self, store, kind, identity, *, state='downloading', stopping=False):
        self.store, self.kind, self.identity = store, kind, identity
        self.state, self.stopping = state, stopping
        self.calls = []

    def jobs(self):
        return [dict(id=self.identity, name='Native title', state=self.state,
                     progress=1 if self.state == 'stopped' else 0, added=10,
                     activity_generation=7)]

    def activity_key(self, identity):
        assert identity == self.identity
        return 'native:' + self.kind + ':content'

    def remove(self, identity, timeout):
        assert media_holds.blocked(self.store, 'native:' + self.kind + '-job:' + identity)
        assert media_holds.blocked(self.store, 'native:' + self.kind + ':content')
        with self.store.connect() as db:
            assert db.execute('SELECT 1 FROM activity_archive WHERE kind=? AND identity=?',
                              (self.kind, identity)).fetchone()
        self.calls.append((identity, timeout))
        if self.stopping:
            from backend.downloaders.nzb.engine import EngineError
            raise EngineError('This NZB is still stopping.', 409)


class DuplicateNative:
    def __init__(self, store, identities):
        self.store, self.identities = store, identities; self.calls = []

    def jobs(self):
        return [dict(id=identity, name='Same display title', state='queued', activity_generation=0)
                for identity in [*self.identities, 'unrelated-native']]

    def activity_key(self, identity):
        return 'native:nzb:' + ('same-content' if identity in self.identities else 'unrelated-content')

    def remove(self, identity, timeout):
        with self.store.connect() as db:
            for target in self.identities:
                assert media_holds.blocked_in(db, 'native:nzb-job:' + target)
                assert db.execute("SELECT 1 FROM activity_archive WHERE kind='nzb' AND identity=?", (target,)).fetchone()
        self.calls.append(identity)


class ActivityItemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.media = self.root / 'media'; self.media.mkdir()
        self.app = create_app(dict(TESTING=True, DATA_DIR=str(self.root / 'data'), MEDIA_DIR=str(self.media)))
        self.store = self.app.extensions['store']
        self.store.set('public_origin', ORIGIN); self.store.set('setup_complete', '1')
        with self.store.connect() as db:
            db.execute("INSERT INTO users VALUES(1,'owner','unused')")
        self.client = self.app.test_client()
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session.update(user_id=1, csrf='token')

    def post(self, kind, identity, **kwargs):
        from backend.activity_observation import capture
        if kind in ('torrent','nzb'):
            target=activity_items._native(self.app,self.store,kind,str(identity))
            observed=capture(kind,target['item'] if target else {'id':identity,'state':'unknown'})
        else:
            with self.store.connect() as db:
                target=activity_items._row(db,kind,str(identity))
                observed=capture(kind,target['item'] if target else {'id':identity,'state':'unknown'},
                                 activity_clear.generation(db,kind,identity))
        return self.client.post('/api/activity/clear-item', base_url=ORIGIN,
            json=dict(kind=kind, id=identity,observed=observed), headers={'Origin': ORIGIN, 'X-CSRF-Token': 'token'}, **kwargs)

    def video(self, identity='abcdefghijk', state='queued'):
        with self.store.connect() as db:
            db.execute('INSERT INTO downloads(id,title,quality,state,message,created,updated) VALUES(?,?,?,?,?,?,?)',
                       (identity, 'Saved title', '1080', state, 'Before clear', 10, 20))

    def vod(self, identity='vod-one', state='running', source='source-one', path='movie-one'):
        with self.store.connect() as db:
            db.execute('INSERT INTO m3u_downloads VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (identity, source, path, 'Saved title', 'movie', str(self.media / (identity + '.mkv')), state, 50, 'Original outcome', 20, 10))

    def request(self, identity='request-one', *, state='downloading', key='episode:1:1:1', kind='episode', engine='', job='', body=None):
        with self.store.connect() as db:
            db.execute('''INSERT INTO discover_requests(id,idempotency,kind,title,state,quality,fallback,body,
                engine,engine_id,episode_key,updated,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (identity, identity, kind, 'Same displayed title', state, '1080', 0, json.dumps(body or {}), engine, job, key, 20, 10))

    def state(self, table, identity):
        with self.store.connect() as db:
            return db.execute('SELECT state FROM ' + table + ' WHERE id=?', (identity,)).fetchone()[0]

    def archive(self):
        with self.store.connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT data FROM activity_archive ORDER BY id')]

    def test_route_requires_auth_origin_csrf_exact_body_and_scalar_identity(self):
        self.video()
        from backend.activity_observation import capture
        with self.store.connect() as db:
            observed=capture('youtube',activity_items._row(db,'youtube','abcdefghijk')['item'],
                             activity_clear.generation(db,'youtube','abcdefghijk'))
        self.assertNotEqual(self.app.test_client().post('/api/activity/clear-item', base_url=ORIGIN,
            json=dict(kind='youtube', id='abcdefghijk',observed=observed)).status_code, 200)
        self.assertEqual(self.client.post('/api/activity/clear-item', base_url=ORIGIN,
            json=dict(kind='youtube', id='abcdefghijk',observed=observed), headers={'Origin': ORIGIN}).status_code, 403)
        self.assertEqual(self.client.post('/api/activity/clear-item', base_url=ORIGIN,
            json=dict(kind='youtube', id='abcdefghijk',observed=observed), headers={'Origin': 'https://other.example', 'X-CSRF-Token': 'token'}).status_code, 403)
        for body in ({}, {'kind': 'all', 'id': 'x'}, {'kind': 'youtube', 'id': True},
                     {'kind': 'youtube', 'id': ''}, {'kind': 'youtube', 'id': []},
                     {'kind': 'youtube', 'id': 'x', 'mediaKey': 'forged'}):
            self.assertEqual(self.client.post('/api/activity/clear-item', base_url=ORIGIN, json={**body,'observed':observed},
                headers={'Origin': ORIGIN, 'X-CSRF-Token': 'token'}).status_code, 400)
        self.assertEqual(self.state('downloads', 'abcdefghijk'), 'queued')
        self.assertEqual(self.archive(), [])

    def test_youtube_clear_archives_before_cancel_and_does_not_hold_whole_channel(self):
        self.video(); self.video('BBBBBBBBBBB')
        with self.store.connect() as db:
            db.execute("INSERT INTO managed_media(key,parent,kind,body,updated) VALUES('video:abcdefghijk','channel','video','{}',1)")
        ack = self.post('youtube', 'abcdefghijk').get_json()
        self.assertEqual((ack['held'], ack['stopping'], ack['cleared']), (True, False, 1))
        self.assertEqual(self.state('downloads', 'abcdefghijk'), 'cancelled')
        self.assertEqual(self.state('downloads', 'BBBBBBBBBBB'), 'queued')
        self.assertTrue(media_holds.blocked(self.store, 'video:abcdefghijk'))
        self.assertFalse(media_holds.blocked(self.store, 'video:BBBBBBBBBBB'))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT hold FROM managed_media WHERE key='video:abcdefghijk'").fetchone()[0], 1)
        self.assertEqual(self.archive()[0]['item']['state'], 'queued')
        self.assertEqual(ack['suppressedActivityIds'], {'youtube': ['abcdefghijk']})
        with self.store.connect() as db:
            db.execute("UPDATE downloads SET state='complete' WHERE id='abcdefghijk'")
        self.assertEqual(download_history.page(self.store, self.media, plain=True)['total'], 1)

    def test_running_video_and_linked_discover_are_suppressed_and_fenced(self):
        self.video(state='downloading')
        self.request(kind='video', key='', body={'videoId': 'abcdefghijk'})
        ack = self.post('youtube', 'abcdefghijk').get_json()
        self.assertTrue(ack['stopping']); self.assertEqual(ack['cleared'], 2)
        self.assertEqual(self.state('downloads', 'abcdefghijk'), 'cancelling')
        self.assertEqual(self.state('discover_requests', 'request-one'), 'cancelling')
        self.assertEqual(set(ack['suppressedActivityIds']), {'youtube', 'discover'})

    def test_completed_video_and_sidecars_remain_byte_and_inode_identical(self):
        self.video(state='complete')
        paths = [self.media / 'saved.mkv', self.media / 'saved.nfo', self.media / 'saved-thumb.jpg']
        for path in paths: path.write_bytes(('owner ' + path.name).encode())
        before = [(path.read_bytes(), path.stat().st_ino) for path in paths]
        self.assertFalse(self.post('youtube', 'abcdefghijk').get_json()['stopping'])
        self.assertEqual(self.state('downloads', 'abcdefghijk'), 'complete')
        self.assertEqual(before, [(path.read_bytes(), path.stat().st_ino) for path in paths])

    def test_vod_key_exact_source_and_path_preserves_other_playlist_items(self):
        self.vod(); self.vod('vod-two', source='other-source'); self.vod('vod-three', path='other-path')
        ack = self.post('m3u', 'vod-one').get_json()
        self.assertTrue(ack['stopping'])
        self.assertEqual(self.state('m3u_downloads', 'vod-one'), 'cancelling')
        self.assertEqual(self.state('m3u_downloads', 'vod-two'), 'running')
        self.assertEqual(self.state('m3u_downloads', 'vod-three'), 'running')
        self.assertTrue(media_holds.blocked(self.store, media_holds.vod_key('source-one', 'movie-one')))
        self.assertFalse(media_holds.blocked(self.store, media_holds.vod_key('other-source', 'movie-one')))

    def test_link_graph_follows_episode_and_policy_ids_without_matching_same_title(self):
        self.request('old-request', state='failed')
        self.request('request-one', engine='nzb', job='native-job')
        self.request('unrelated', key='episode:1:1:2')
        self.vod('linked-vod', state='queued')
        with self.store.connect() as db:
            db.execute("INSERT INTO managed_media(key,parent,kind,body,updated) VALUES('episode:1:1:1','show:1','episode','{}',1)")
            db.execute('INSERT INTO movie_download_policy VALUES(?,?,?)', ('vod', 'linked-vod', json.dumps({'requestId': 'old-request'})))
            db.execute('INSERT INTO show_download_policy VALUES(?,?,?)', ('nzb', 'native-job', json.dumps({'requestId': 'request-one'})))
        engine = Native(self.store, 'nzb', 'native-job'); self.app.extensions['nzb_engine'] = engine
        ack = self.post('m3u', 'linked-vod').get_json()
        self.assertEqual(ack['cleared'], 4)
        self.assertEqual(self.state('m3u_downloads', 'linked-vod'), 'cancelled')
        self.assertEqual(self.state('discover_requests', 'request-one'), 'cancelling')
        self.assertEqual(self.state('discover_requests', 'old-request'), 'failed')
        self.assertEqual(self.state('discover_requests', 'unrelated'), 'downloading')
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT hold FROM managed_media WHERE key='episode:1:1:1'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT stop_generation FROM discover_requests WHERE id='request-one'").fetchone()[0], 1)
        self.assertEqual(engine.calls, [('native-job', .5)])
        self.assertTrue(media_holds.blocked(self.store, ['managed:episode:1:1:1']))
        self.assertNotIn('unrelated', ack['suppressedActivityIds'].get('discover', []))
        self.assertEqual({row['kind'] for row in self.archive()}, {'m3u', 'discover', 'nzb'})

    def test_native_clear_commits_hold_and_archive_before_remove_and_reports_stopping(self):
        engine = Native(self.store, 'torrent', 'native-job', stopping=True)
        self.app.extensions['torrent_engine'] = engine
        ack = self.post('torrent', 'native-job').get_json()
        self.assertEqual((ack['held'], ack['stopping'], ack['cleared']), (True, True, 1))
        self.assertEqual(engine.calls, [('native-job', .5)])
        self.assertEqual(ack['suppressedActivityIds']['torrent'], ['native-job'])

    def test_direct_native_clear_follows_reverse_discover_engine_link_without_policy(self):
        self.request(engine='torrent', job='native-job')
        self.request('unrelated', key='episode:1:1:2', engine='torrent', job='other-native')
        engine = Native(self.store, 'torrent', 'native-job')
        self.app.extensions['torrent_engine'] = engine
        ack = self.post('torrent', 'native-job').get_json()
        self.assertEqual(ack['cleared'], 2)
        self.assertEqual(self.state('discover_requests', 'request-one'), 'cancelling')
        self.assertEqual(self.state('discover_requests', 'unrelated'), 'downloading')
        self.assertTrue(media_holds.blocked(self.store, 'managed:episode:1:1:1'))

    def test_native_hold_is_persisted_before_factory_restore(self):
        identity = 'a' * 32; folder = self.store.directory / 'nzb-engine' / 'jobs'; folder.mkdir(parents=True)
        payload = dict(id=identity, name='Saved native', selected=[0], phase='queued', files=[dict(index=0,
            name='video.mkv', groups=[], segments=[dict(number=1, size=10, message_id='saved-part')])])
        (folder / (identity + '.json')).write_text(json.dumps(payload))
        engine = Native(self.store, 'nzb', identity)
        def factory(app, store):
            self.assertTrue(media_holds.blocked(store, 'native:nzb-job:' + identity))
            from backend.downloaders.nzb.engine import checkpoint_activity_key
            self.assertTrue(media_holds.blocked(store, checkpoint_activity_key(payload)))
            self.assertEqual(len(self.archive()), 1)
            return engine
        with patch('backend.nzb._engine', side_effect=factory) as opening:
            ack = self.post('nzb', identity).get_json()
        self.assertTrue(ack['held']); opening.assert_called_once()
        self.assertEqual(engine.calls, [(identity, .5)])

    def test_saved_nzb_pause_is_visible_without_changing_intent_or_terminal_state(self):
        identity = 'd' * 32
        folder = self.store.directory / 'nzb-engine' / 'jobs'
        folder.mkdir(parents=True)
        path = folder / (identity + '.json')
        for phase in ('queued', 'download', 'paused', 'choose', 'failed', 'stopped'):
            for paused in (False, True, 'false', 1, None):
                with self.subTest(phase=phase, paused=paused):
                    payload = dict(id=identity, name='Saved native', selected=[0], phase=phase,
                        user_paused=paused, files=[dict(index=0, name='video.mkv', groups=[],
                            segments=[dict(number=1, size=10, message_id='saved-part')])])
                    path.write_text(json.dumps(payload))
                    before = path.read_bytes()
                    with patch('backend.nzb._engine', side_effect=AssertionError('Read must not construct engine')):
                        item = activity_items._saved_native(self.store, 'nzb', identity)['item']
                    expected = ('paused' if paused is True and phase in {'queued', 'download', 'paused'} else
                                'downloading' if phase == 'download' else phase)
                    self.assertEqual((item['state'], item['phase']), (expected, phase))
                    self.assertEqual(path.read_bytes(), before)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM media_clear_holds').fetchone()[0], 0)

    def test_duplicate_native_content_expands_exact_linked_requests_but_not_same_name(self):
        engine = DuplicateNative(self.store, ['native-one', 'native-two'])
        self.app.extensions['nzb_engine'] = engine
        self.request(engine='nzb', job='native-one')
        self.request('second-request', key='episode:2:1:1', engine='nzb', job='native-two')
        self.request('unrelated', key='episode:3:1:1', engine='nzb', job='unrelated-native')
        ack = self.post('nzb', 'native-one').get_json()
        self.assertEqual(ack['cleared'], 4)
        self.assertEqual(set(engine.calls), {'native-one', 'native-two'})
        self.assertEqual(set(ack['suppressedActivityIds']['nzb']), {'native-one', 'native-two'})
        self.assertEqual(self.state('discover_requests', 'request-one'), 'cancelling')
        self.assertEqual(self.state('discover_requests', 'second-request'), 'cancelling')
        self.assertEqual(self.state('discover_requests', 'unrelated'), 'downloading')

    def test_factory_revealed_duplicate_native_is_archived_and_held_before_either_remove(self):
        identity = 'a' * 32; other = 'b' * 32
        folder = self.store.directory / 'nzb-engine' / 'jobs'; folder.mkdir(parents=True)
        payload = dict(id=identity, name='Native', selected=[0], phase='queued', files=[dict(index=0,
            name='video.mkv', segments=[dict(number=1, size=10, message_id='saved-part')])])
        (folder / (identity + '.json')).write_text(json.dumps(payload))
        engine = DuplicateNative(self.store, [identity, other])
        from backend.downloaders.nzb.engine import checkpoint_activity_key
        content_key = checkpoint_activity_key(payload)
        original_key = engine.activity_key
        engine.activity_key = lambda target: content_key if target in engine.identities else original_key(target)
        def factory(app, store):
            self.assertTrue(media_holds.blocked(store, content_key))
            app.extensions['nzb_engine'] = engine
            return engine
        with patch('backend.nzb._engine', side_effect=factory):
            ack = self.post('nzb', identity).get_json()
        self.assertEqual(ack['cleared'], 2)
        self.assertEqual(set(engine.calls), {identity, other})
        self.assertEqual(set(ack['suppressedActivityIds']['nzb']), {identity, other})

    def test_unknown_forged_malformed_and_symlink_native_ids_never_open_engine_or_hold(self):
        folder = self.store.directory / 'nzb-engine' / 'jobs'; folder.mkdir(parents=True)
        (folder / ('a' * 32 + '.json')).write_text(json.dumps({'id': 'b' * 32, 'selected': [], 'files': []}))
        outside = self.root / 'outside.json'; outside.write_text(json.dumps({'id': 'c' * 32, 'selected': [], 'files': []}))
        (folder / ('c' * 32 + '.json')).symlink_to(outside)
        with patch('backend.nzb._engine') as opening:
            for identity in ('unknown', '../outside', 'a' * 32, 'c' * 32):
                response = self.post('nzb', identity)
                self.assertEqual(response.status_code,409)
                self.assertIn('changed',response.get_json()['error'])
            opening.assert_not_called()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM media_clear_holds').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM activity_archive').fetchone()[0],0)
        self.assertEqual(outside.read_text(), json.dumps({'id': 'c' * 32, 'selected': [], 'files': []}))

    def test_log_only_event_and_sync_archive_without_holding_or_stopping(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO activity_events VALUES(123,10,'Background work','System event','failed','Original event')")
            db.execute("INSERT INTO m3u_runs(source,finished,state,message,stats) VALUES('source',11,'failed','Original sync','{}')")
            sync = db.execute('SELECT id FROM m3u_runs').fetchone()[0]
        for kind, identity in [('event', 123), ('m3u_sync', sync)]:
            ack = self.post(kind, identity).get_json()
            self.assertEqual((ack['held'], ack['stopping'], ack['cleared']), (False, False, 1))
            self.assertEqual(self.post(kind, identity).get_json()['cleared'], 0)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM media_clear_holds').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM m3u_runs').fetchone()[0], 1)
        self.assertEqual(len(self.archive()), 2)

    def test_repeat_clear_is_idempotent_and_restart_does_not_release_hold(self):
        self.video(state='downloading')
        first = self.post('youtube', 'abcdefghijk').get_json()
        second = self.post('youtube', 'abcdefghijk').get_json()
        self.assertEqual(second['cleared'], 0)
        self.assertEqual(second['activityRevision'], first['activityRevision'])
        self.assertEqual(len(self.archive()), 1)
        from backend.storage import Store
        reopened = Store(self.store.directory); activity_clear.initialise(reopened)
        self.assertTrue(media_holds.blocked(reopened, 'video:abcdefghijk'))
        with reopened.connect() as db:
            db.execute("UPDATE downloads SET state='failed',message='Worker stopped' WHERE id='abcdefghijk'")
        self.assertEqual(download_history.page(reopened, self.media, plain=True)['total'], 0)
        self.assertTrue(media_holds.resume(reopened, 'video:abcdefghijk'))
        self.assertFalse(media_holds.blocked(reopened, 'video:abcdefghijk'))
        self.assertEqual(download_history.page(reopened, self.media, plain=True)['total'], 1)

    def test_archive_uses_safe_original_facts_not_private_discover_body(self):
        self.request(state='failed', body={'url': 'https://private.example/?token=PRIVATE', 'Cookie': 'PRIVATE'})
        self.post('discover', 'request-one')
        encoded = json.dumps(self.archive())
        self.assertNotIn('PRIVATE', encoded); self.assertNotIn('body', encoded)
        self.assertIn('failed', encoded)

    def test_legacy_request_without_managed_key_is_still_held_by_its_exact_request_id(self):
        self.request(kind='movie', key='', state='searching')
        self.request('other-request', kind='movie', key='', state='searching')
        ack = self.post('discover', 'request-one').get_json()
        self.assertTrue(ack['held'])
        self.assertTrue(media_holds.blocked(self.store, 'request:request-one'))
        self.assertFalse(media_holds.blocked(self.store, 'request:other-request'))
        self.assertEqual(self.state('discover_requests', 'other-request'), 'searching')

    def test_simultaneous_clear_calls_commit_once_and_preserve_unrelated_rows(self):
        self.video(); self.video('BBBBBBBBBBB')
        results = []; barrier = threading.Barrier(2)
        def clear():
            barrier.wait(); results.append(activity_items.clear_one(self.app, self.store, 'youtube', 'abcdefghijk'))
        threads = [threading.Thread(target=clear) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5); self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(row['cleared'] for row in results), [0, 1])
        self.assertEqual(len({row['activityRevision'] for row in results}), 1)
        self.assertEqual(self.state('downloads', 'BBBBBBBBBBB'), 'queued')


if __name__ == '__main__':
    unittest.main()
