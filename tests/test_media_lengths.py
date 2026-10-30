"""Preserve known duration units without adding provider reads or file probes."""
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_guide as fixture
from backend import discover_store, m3u_catalogue
from backend.view_media import adapters, local, providers


class MediaLengthTests(unittest.TestCase):
    def test_shared_cards_and_saved_discovery_keep_explicit_units(self):
        item = dict(id='movie:1', kind='movie', title='Film', runtimeMinutes=148)
        result = adapters.card(item, dict(type='discover', item=item))
        self.assertEqual(result['runtimeMinutes'], 148)
        self.assertEqual(result['action']['item']['runtimeMinutes'], 148)
        clip = adapters.card(dict(id='video:1', kind='video', title='Clip',
                                  duration='18:42', durationSeconds=1122, metadata_pending=True),
                             dict(type='youtube', id='abcdefghijk'))
        self.assertEqual(clip['duration'], '18:42')
        self.assertEqual(clip['durationSeconds'], 1122)
        self.assertTrue(clip['metadata_pending'])

    def test_unknown_invalid_or_unbounded_lengths_are_not_invented(self):
        for value in (None, '', '90', True, -5, 0, float('inf'), float('nan'), 10**20):
            item = dict(id='movie:1', kind='movie', title='Film', runtimeMinutes=value, durationSeconds=value)
            with self.subTest(value=repr(value)):
                for result in (discover_store.clean_item(item), adapters.card(item, dict(type='local', id='file'))):
                    self.assertNotIn('runtimeMinutes', result)
                    self.assertNotIn('durationSeconds', result)

    def test_local_nfo_runtime_is_minutes_and_never_runs_ffprobe(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'Film.mp4'
            path.write_bytes(b'fixture')
            path.with_suffix('.nfo').write_text('<movie><title>Film</title><runtime>95.5</runtime></movie>')
            with patch('subprocess.run', side_effect=AssertionError('No file probe')) as probe:
                identity, item, entry = local._entry(root, path)
                self.assertEqual(item['runtimeMinutes'], 95.5)
                self.assertEqual(item['action']['id'], identity)
                self.assertEqual(entry['path'], path)
                probe.assert_not_called()
            path.with_suffix('.nfo').write_text('<movie><runtime>unknown</runtime></movie>')
            self.assertNotIn('runtimeMinutes', local._entry(root, path)[1])

    def test_tmdb_movie_and_vod_movie_keep_known_runtime(self):
        self.assertEqual(providers.movie_card(dict(id=1, title='Film', runtime=148))['runtimeMinutes'], 148)
        client = Mock()
        client.entity.return_value = dict(id=1)
        client.get.return_value = dict(id=1, title='Film', runtime=148)
        movie = m3u_catalogue.resolve(client, dict(identity=1, kind='movies'))
        self.assertEqual(movie['runtimeMinutes'], 148)
        show = m3u_catalogue.resolve(client, dict(identity=1, kind='series'))
        self.assertNotIn('runtimeMinutes', show, 'a show summary is not the exact length of every episode')
        row = dict(source='source', path='film.strm', title='Film', kind='movies', metadata=json.dumps(movie))
        with tempfile.TemporaryDirectory() as folder:
            db=sqlite3.connect(Path(folder)/'catalogue.sqlite3')
            db.row_factory=sqlite3.Row
            try:
                db.execute('CREATE TABLE m3u_file_info(source TEXT,path TEXT,data TEXT NOT NULL,PRIMARY KEY(source,path))')
                db.execute('INSERT INTO m3u_file_info VALUES(?,?,?)',('source','film.strm',json.dumps(dict(durationSeconds=8880))))
                statements=[];db.set_trace_callback(statements.append)
                decorated=m3u_catalogue.decorate(db,[row])[0]
                self.assertEqual(decorated['runtimeMinutes'],148)
                self.assertEqual(decorated['durationSeconds'],8880,'known per-file facts use the actual batched database lookup')
                self.assertEqual(len([sql for sql in statements if sql.startswith('SELECT source,path,data FROM m3u_file_info')]),1)
            finally:db.close()


class SubscriptionLengthTests(unittest.TestCase):
    setUp = fixture.GuideTests.setUp
    tearDown = fixture.GuideTests.tearDown
    google = fixture.GuideTests.google

    def test_subscription_adapter_retains_existing_length_and_pending_state(self):
        channel = fixture.CHANNEL
        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES(?,?,1)', (channel, json.dumps(dict(id=channel, name='Channel'))))
            video = dict(id='abcdefghijk', title='Clip', channel=channel, state='published', duration='18:42', metadata_pending=True)
            db.execute('INSERT INTO videos VALUES(?,?,?,?,?,0)',
                       (video['id'], channel, '2026-10-01', '2026-10-01T12:00:00Z', json.dumps(video)))
        self.app.config['YOUTUBE_FEED'] = Mock(side_effect=AssertionError('No provider read'))
        result = adapters.subscriptions(self.app, self.store, dict(q='', sort='new', offset=0))
        self.assertEqual(result['items'][0]['duration'], '18:42')
        self.assertTrue(result['items'][0]['metadata_pending'])
        self.app.config['YOUTUBE_FEED'].assert_not_called()


if __name__ == '__main__':
    unittest.main()
