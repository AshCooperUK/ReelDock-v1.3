import importlib.util
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import discover_store, downloads, media_library
from backend.storage import Store
from backend.view_media import local, local_parent

ORIGIN = 'https://dev.example.com'
CHANNEL = 'UC' + 'a' * 22
OTHER = 'UC' + 'b' * 22


class Fixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = (Path(self.tmp.name) / 'media').resolve()
        self.root.mkdir()
        self.store = Store(Path(self.tmp.name) / 'data')
        self.store.media_root = str(self.root)
        discover_store.initialise(self.store)
        downloads.initialise(self.store)
        media_library.initialise(self.store)
        self.app = SimpleNamespace(config={'MEDIA_DIR': str(self.root)})

    def tearDown(self):
        local._CACHE.pop(str(self.root), None)
        self.tmp.cleanup()

    def video(self, *parts):
        path = self.root.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x')
        return path

    def show(self, name='The Simpsons'):
        return [self.video('TV Shows', name, 'Season 1', f'{name} S01E0{n}.mkv') for n in (1, 2)]

    def scan(self):
        state = dict(items=[], files={}, scanning=False, checked=0, warning='')
        local._scan(self.root, state)
        local._CACHE[str(self.root)] = state
        return local.grouped(state['items'], state, self.root)[0]['action']['id']

    def own(self, key, path):
        with self.store.connect() as db:
            db.execute("INSERT INTO discover_owned(key,request_id,path,state) VALUES(?,?,?,'complete')", (key, 'r-' + key, str(path)))

    def target(self):
        result = local_parent.resolve(self.app, self.store, self.scan())
        return result['target']


class LocalParentTests(Fixture, unittest.TestCase):
    def test_owned_show_by_relative_and_absolute_path(self):
        first, second = self.show()
        self.own('show:71663:1:1', first.relative_to(self.root))
        self.own('show:71663:1:2', second)
        with patch('backend.view_media.providers.details') as details:
            target = self.target()
            details.assert_not_called()
        self.assertEqual(target['type'], 'discover')
        item = target['item']
        self.assertEqual((item['id'], item['kind'], item['externalId'], item['title']), ('tvdb:show:71663', 'show', '71663', 'The Simpsons'))
        for key in ('year', 'releaseDate', 'initialEpisode', 'episode'):
            self.assertNotIn(key, item)

    def test_no_identity_is_null_and_never_searches(self):
        self.show()
        with patch('backend.view_media.providers.details') as details:
            self.assertIsNone(self.target())
            details.assert_not_called()

    def test_conflicting_owned_shows_are_null(self):
        first, second = self.show()
        self.own('show:1:1:1', first)
        self.own('show:2:1:2', second)
        self.assertIsNone(self.target())

    def test_incomplete_or_malformed_owned_keys_are_ignored(self):
        first, second = self.show()
        self.own('movie:5', first)
        self.own('show:0:1:1', second)
        self.assertIsNone(self.target())

    def nfo(self, text, name='tvshow.nfo'):
        path = self.root / 'TV Shows' / 'The Simpsons' / name
        path.write_text(text) if isinstance(text, str) else path.write_bytes(text)
        return path

    def test_tvshow_nfo_tvdb_identity(self):
        self.show()
        self.nfo('<tvshow><title>The Simpsons</title><uniqueid type="tvdb">71663</uniqueid></tvshow>')
        item = self.target()['item']
        self.assertEqual((item['id'], item['externalId']), ('tvdb:show:71663', '71663'))

    def test_tvdbid_element_wins_over_tmdb(self):
        self.show()
        self.nfo('<tvshow><tmdbid>456</tmdbid><tvdbid>71663</tvdbid></tvshow>')
        with patch('backend.view_media.providers.details') as details:
            self.assertEqual(self.target()['item']['externalId'], '71663')
            details.assert_not_called()

    def test_episode_nfo_ids_never_identify_the_show(self):
        first, _ = self.show()
        first.with_suffix('.nfo').write_text('<episodedetails><uniqueid type="tvdb">999</uniqueid><tvdbid>999</tvdbid></episodedetails>')
        self.nfo('<episodedetails><uniqueid type="tvdb">999</uniqueid></episodedetails>')
        self.assertIsNone(self.target())

    def test_tmdb_show_uses_provider_details_only_for_verified_id(self):
        self.show()
        self.nfo('<tvshow><uniqueid type="tmdb">456</uniqueid></tvshow>')
        detail = dict(id='tvmaze:show:9', kind='show', externalId='71663', title='Provider Title', year='1989')
        with patch('backend.view_media.providers.details', return_value=detail) as details:
            item = self.target()['item']
        details.assert_called_once_with(self.store, 'tmdb:show:456')
        self.assertEqual((item['id'], item['externalId'], item['title']), ('tvmaze:show:9', '71663', 'The Simpsons'))

    def test_tmdb_without_tvdb_or_failure_is_null(self):
        self.show()
        self.nfo('<tvshow><uniqueid type="tmdb">456</uniqueid></tvshow>')
        with patch('backend.view_media.providers.details', return_value=dict(id='tvmaze:show:9', externalId='')):
            self.assertIsNone(self.target())
        with patch('backend.view_media.providers.details', side_effect=ValueError('offline')):
            self.assertIsNone(self.target())

    def test_symlinked_tvshow_nfo_is_rejected(self):
        self.show()
        outside = Path(self.tmp.name) / 'outside.nfo'
        outside.write_text('<tvshow><uniqueid type="tvdb">71663</uniqueid></tvshow>')
        (self.root / 'TV Shows' / 'The Simpsons' / 'tvshow.nfo').symlink_to(outside)
        self.assertIsNone(self.target())

    def test_unsafe_nfo_content_is_null(self):
        self.show()
        for raw in ('<tvshow><tvdbid>1', b'<tvshow>' + b' ' * 100000 + b'<tvdbid>1</tvdbid></tvshow>',
                    '<!DOCTYPE tvshow [<!ENTITY a "1">]><tvshow><tvdbid>&a;</tvdbid></tvshow>', '<tvshow><tvdbid>0</tvdbid></tvshow>',
                    '<tvshow><tvdbid>1234567890123</tvdbid></tvshow>'):
            self.nfo(raw)
            self.assertIsNone(self.target(), raw[:40])

    def test_child_swapped_for_symlink_after_scan_is_null(self):
        first, second = self.show()
        self.own('show:71663:1:1', first)
        group = self.scan()
        second.unlink()
        second.symlink_to(first)
        self.assertEqual(local_parent.resolve(self.app, self.store, group), dict(target=None))

    def test_youtube_group_stays_saved_files_even_with_exact_channel_identity(self):
        paths = [self.video('YouTube', 'Some Channel', f'v{n}.mp4') for n in (1, 2)]
        for n, path in enumerate(paths):
            self.add_file(f'vid{n}', CHANNEL, path.relative_to(self.root) if n else path, n)
        self.assertIsNone(self.target())
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM media_files').fetchone()[0], 2)

    def add_file(self, identity, channel, path, episode):
        with self.store.connect() as db:
            db.execute("INSERT INTO media_files(id,channel,path,day,episode,profile,bytes) VALUES(?,?,?,?,?,?,1)",
                       (identity, channel, str(path), '2026-01-01', episode, 'p'))

    def test_youtube_missing_invalid_or_conflicting_channel_is_null(self):
        paths = [self.video('YouTube', 'Some Channel', f'v{n}.mp4') for n in (1, 2)]
        self.assertIsNone(self.target())
        self.add_file('a', 'Some Channel', paths[0], 1)
        self.assertIsNone(self.target())
        self.add_file('b', CHANNEL, paths[1], 2)
        self.assertIsNone(self.target())
        with self.store.connect() as db:
            db.execute('UPDATE media_files SET channel=? WHERE id=?', (OTHER, 'a'))
        self.assertIsNone(self.target())

    def test_movie_groups_have_no_target(self):
        self.video('Movies', 'Trilogy', 'a.mkv'); self.video('Movies', 'Trilogy', 'b.mkv')
        self.assertIsNone(self.target())



class LocalParentEndpointTests(Fixture, unittest.TestCase):
    """Route-level checks; need the real Flask app (skipped where Flask is absent)."""
    def setUp(self):
        if importlib.util.find_spec('flask') is None:
            self.skipTest('Flask is not installed')
        from backend.app import create_app
        from backend.nzb import stop
        self.tmp = tempfile.TemporaryDirectory()
        self.root = (Path(self.tmp.name) / 'media').resolve()
        self.root.mkdir()
        self.app = create_app({'TESTING': True, 'DATA_DIR': self.tmp.name, 'MEDIA_DIR': str(self.root)})
        self.stop = stop
        self.store = self.app.extensions['store']
        self.store.media_root = str(self.root)
        self.store.set('public_origin', ORIGIN)
        self.store.set('setup_complete', '1')
        with self.store.connect() as db:
            db.execute("INSERT INTO users VALUES(1,'owner','unused')")
        self.client = self.app.test_client()
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session['user_id'] = 1
            session['csrf'] = 'token'

    def tearDown(self):
        self.stop(self.app)
        super().tearDown()

    def test_endpoint_auth_stale_and_no_path_leak(self):
        first, second = self.show()
        self.own('show:71663:1:1', first)
        group = self.scan()
        url = f'/api/view-media/local/{group}/parent'
        self.assertNotEqual(self.app.test_client().get(url, base_url=ORIGIN).status_code, 200)
        response = self.client.get(url, base_url=ORIGIN)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['target']['item']['id'], 'tvdb:show:71663')
        self.assertNotIn(str(self.root), response.get_data(as_text=True))
        for bad in ('group-' + '0' * 24, 'not-a-group', 'x' * 40):
            self.assertEqual(self.client.get(f'/api/view-media/local/{bad}/parent', base_url=ORIGIN).status_code, 404)
        first.unlink()
        second.unlink()
        local.invalidate(self.root)
        stale = self.client.get(url, base_url=ORIGIN)
        self.assertEqual(stale.status_code, 404)


if __name__ == '__main__':
    unittest.main()
