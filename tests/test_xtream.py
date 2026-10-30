"""Xtream logins import into the same library as M3U playlists. All provider calls are mocked."""
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from backend import m3u, xtream, m3u_catalogue as catalogue
from backend.app import create_app

ORIGIN = 'https://dev.example.com'
SECRET = 'p@ss/word'

API = {
    None: {'user_info': {'auth': 1, 'status': 'Active', 'exp_date': '1893456000', 'max_connections': '2'}},
    'get_live_categories': [{'category_id': '1', 'category_name': 'News'}],
    'get_live_streams': [{'stream_id': 11, 'name': 'News One', 'stream_icon': 'https://img.example.org/n.png', 'category_id': '1', 'epg_channel_id': 'news.one'}],
    'get_vod_categories': [{'category_id': '2', 'category_name': 'Films'}],
    'get_vod_streams': [{'stream_id': 21, 'name': 'A "Quoted" Film', 'year': '2019', 'container_extension': 'mkv', 'stream_icon': 'https://img.example.org/f.jpg',
                         'rating': '7.1', 'category_id': '2'},
                        {'stream_id': 'x', 'name': 'Invalid id'}],
    'get_series_categories': [{'category_id': '3', 'category_name': 'Drama'}],
    'get_series': [{'series_id': 31, 'name': 'The Show', 'cover': 'https://img.example.org/s.jpg', 'plot': 'Provider plot.',
                    'cast': 'Actor One, Actor Two', 'genre': 'Drama', 'rating': '8', 'releaseDate': '2020-01-01', 'last_modified': '100', 'category_id': '3'}],
    'get_series_info': {'episodes': {'1': [{'id': '41', 'episode_num': 1, 'season': 1, 'container_extension': 'mp4', 'info': {'plot': 'Ep'}},
                                           {'id': '42', 'episode_num': 2, 'season': 1, 'container_extension': 'mp4'}]}},
    'get_vod_info': {'info': {'plot': 'Film plot from provider.', 'cast': 'Lead Actor', 'genre': 'Thriller', 'rating': '7.1'}},
}


class Provider:
    def __init__(self, api=API): self.api = api; self.calls = []

    def __call__(self, url):
        parsed = urlsplit(url)
        assert parsed.path.endswith('/player_api.php'), url
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.calls.append(query.get('action'))
        assert query['username'] == 'user' and query['password'] == SECRET
        return json.dumps(self.api[query.get('action')])


class XtreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)/'library'
        self.app = create_app({'TESTING': True, 'DATA_DIR': self.temp.name, 'M3U_DIR': str(self.root)})
        self.store = self.app.extensions['store']
        self.store.set('public_origin', ORIGIN); self.store.set('setup_complete', '1')
        with self.store.connect() as db: db.execute("INSERT INTO users VALUES(1,'admin','unused')")
        self.client = self.app.test_client()
        with self.client.session_transaction(base_url=ORIGIN) as session: session['user_id'] = 1; session['csrf'] = 'token'

    def tearDown(self): self.temp.cleanup()

    def post(self, body, csrf='token'):
        return self.client.post('/api/xtream', base_url=ORIGIN, json=body, headers={'Origin': ORIGIN, 'X-CSRF-Token': csrf})

    def add(self, **extra):
        response = self.post(dict(name='Provider', server='http://tv.example.net:8080/player_api.php', username='user', password=SECRET, **extra))
        self.assertEqual(response.status_code, 200, response.json)
        return response.json['id']

    def run_import(self, provider=None):
        provider = provider or Provider()
        with patch('backend.m3u.fetch_playlist', provider):
            m3u.tick(self.store, self.root)
        return provider

    def test_login_saved_without_exposing_password_and_requires_csrf(self):
        self.assertEqual(self.post({'name': 'x'}, csrf='bad').status_code, 403)
        self.assertEqual(self.app.test_client().get('/api/xtream', base_url=ORIGIN).status_code, 401)
        source = self.add()
        # A new enabled login queues its first import; editing waits for it.
        self.assertIn('Wait for the current import', self.post({'id': source, 'name': 'Renamed'}).json['error'])
        with self.store.connect() as db: db.execute("UPDATE m3u_sources SET state='idle'")
        listing = self.client.get('/api/xtream', base_url=ORIGIN)
        self.assertNotIn(SECRET, listing.get_data(as_text=True))
        item = listing.json['items'][0]
        self.assertEqual((item['server'], item['username'], item['password_saved'], item['enabled']), ('http://tv.example.net:8080', 'user', True, True))
        m3u_list = self.client.get('/api/m3u', base_url=ORIGIN)
        self.assertNotIn(SECRET, m3u_list.get_data(as_text=True))
        self.assertEqual(m3u_list.json['items'][0]['kind'], 'xtream'); self.assertEqual(m3u_list.json['items'][0]['host'], 'tv.example.net')
        # Editing without a password keeps the saved one; editing M3U options keeps the login.
        self.assertEqual(self.post({'id': source, 'name': 'Renamed', 'enabled': False}).status_code, 200)
        self.assertEqual(xtream.account(self.store, source)['password'], SECRET)
        self.assertEqual(self.client.get('/api/xtream', base_url=ORIGIN).json['items'][0]['enabled'], False)
        m3u.save(self.store, {'id': source, 'name': 'Renamed', 'hours': 6})
        with self.store.connect() as db: self.assertEqual(db.execute('SELECT url FROM m3u_sources').fetchone()[0], 'xtream:'+source)
        for bad, message in [({'server': 'ftp://x'}, 'server address'), ({'server': 'http://u:p@x.example'}, 'server address'), ({'username': ''}, 'username')]:
            body = dict(name='Bad', server='http://tv.example.net', username='user', password='pw'); body.update(bad)
            response = self.post(body)
            self.assertEqual(response.status_code, 400); self.assertIn(message, response.json['error'])

    def test_connection_test_reports_account_and_rejection_without_credentials(self):
        source = self.add()
        with patch('backend.m3u.fetch_playlist', Provider()):
            result = self.post({'action': 'test', 'id': source}).json
        self.assertIn('Connected. Account active, expires 2030-01-01, 2 connections allowed.', result['message'])
        rejected = Provider({**API, None: {'user_info': {'auth': 0}}})
        with patch('backend.m3u.fetch_playlist', rejected):
            response = self.post({'action': 'test', 'id': source})
        self.assertEqual(response.status_code, 400); self.assertIn('rejected this login', response.json['error'])
        with patch('backend.m3u.fetch_playlist', side_effect=ValueError('http://tv.example.net/player_api.php?password='+SECRET)):
            response = self.post({'action': 'test', 'id': source})
        self.assertNotIn(SECRET, response.get_data(as_text=True)); self.assertNotIn('player_api', response.json['error'])

    def test_private_servers_are_refused_like_m3u_sources(self):
        with patch('backend.m3u.public_addresses', side_effect=ValueError('private')), self.assertRaises(ValueError) as caught:
            xtream.authenticate(xtream.Client('http://192.168.1.10:8080', 'user', SECRET))
        self.assertNotIn(SECRET, str(caught.exception))

    def test_import_builds_standard_stream_urls_and_library_entries(self):
        source = self.add()
        self.run_import()
        with self.store.connect() as db:
            state = db.execute('SELECT state,message FROM m3u_sources').fetchone()
            files = {r['path']: dict(r) for r in db.execute('SELECT * FROM m3u_files')}
        self.assertEqual(state['state'], 'complete', state['message'])
        self.assertNotIn(SECRET, state['message'])
        base = self.root/source
        film = next(p for p in files if p.startswith('Movie_VOD'))
        self.assertEqual(files[film]['kind'], 'movies'); self.assertIn('(2019)', film)
        self.assertEqual((base/film).read_text().strip(), 'http://tv.example.net:8080/movie/user/p%40ss%2Fword/21.mkv')
        episodes = sorted(p for p in files if files[p]['kind'] == 'series')
        self.assertEqual(len(episodes), 2)
        self.assertEqual((base/episodes[0]).read_text().strip(), 'http://tv.example.net:8080/series/user/p%40ss%2Fword/41.mp4')
        live = (base/'Live_TV/livetv.m3u').read_text()
        self.assertIn('http://tv.example.net:8080/live/user/p%40ss%2Fword/11.ts', live); self.assertIn('group-title="News"', live)
        # Provider metadata is used first in the library.
        catalogue.index(self.store)
        items = self.client.get('/api/m3u/'+source+'/entries', base_url=ORIGIN, query_string={'kind': 'series'}).json['items']
        self.assertEqual(items[0]['description'], 'Provider plot.'); self.assertEqual(items[0]['rating'], 8.0)
        with self.store.connect() as db:
            names = {json.loads(r['info']).get('name') for r in db.execute(
                "SELECT t.info FROM m3u_catalog_titles t JOIN m3u_catalog_entries c ON c.key=t.key JOIN m3u_files f ON f.source=c.source AND f.path=c.path WHERE f.kind='series'")}
        self.assertEqual(names, {'The Show (2020)'})
        # Series details are cached until the provider reports a change.
        again = self.run_import(Provider())
        self.assertNotIn('get_series_info', again.calls)

    def test_film_details_use_get_vod_info_and_provider_first(self):
        source = self.add(); self.run_import()
        with self.store.connect() as db: film = db.execute("SELECT path FROM m3u_files WHERE kind='movies'").fetchone()[0]
        provider = Provider()
        with patch('backend.m3u.fetch_playlist', provider):
            details = self.client.get('/api/m3u/'+source+'/details', base_url=ORIGIN, query_string={'path': film}).json
            self.client.get('/api/m3u/'+source+'/details', base_url=ORIGIN, query_string={'path': film})
        self.assertEqual(details['description'], 'Film plot from provider.')
        self.assertEqual((details['facts']['Cast'], details['facts']['Genres']), ('Lead Actor', 'Thriller'))
        self.assertEqual(provider.calls.count('get_vod_info'), 1)

    def test_tmdb_fills_gaps_but_provider_metadata_stays_first(self):
        found = {'tmdb_id': 5, 'media_type': 'tv', 'title': 'The Show', 'description': 'TMDB plot', 'rating': 6.0, 'artwork': '/api/m3u-artwork/a.jpg'}
        merged = catalogue.merge_provider(found, catalogue.provider_summary({'x-plot': 'Provider plot.', 'x-rating': '8'}, 'tv'))
        self.assertEqual((merged['description'], merged['rating'], merged['tmdb_id'], merged['artwork']), ('Provider plot.', 8.0, 5, '/api/m3u-artwork/a.jpg'))
        self.assertEqual(catalogue.merge_provider(found, {}), found)

    def test_fix_match_works_for_xtream_items(self):
        source = self.add(); self.run_import(); catalogue.index(self.store)
        with self.store.connect() as db: film = db.execute("SELECT path FROM m3u_files WHERE kind='movies'").fetchone()[0]
        response = self.client.post('/api/m3u/'+source+'/match', base_url=ORIGIN, json={'path': film, 'action': 'manual', 'fields': {'title': 'Corrected', 'plot': 'Mine.'}},
                                    headers={'Origin': ORIGIN, 'X-CSRF-Token': 'token'})
        self.assertTrue(response.json['locked'])
        with patch('backend.m3u.fetch_playlist', side_effect=AssertionError('no provider call for locked own details')):
            details = self.client.get('/api/m3u/'+source+'/details', base_url=ORIGIN, query_string={'path': film}).json
        self.assertEqual((details['title'], details['description']), ('Corrected', 'Mine.'))

    def test_removal_deletes_the_login(self):
        source = self.add()
        response = self.client.post('/api/m3u', base_url=ORIGIN, json={'action': 'remove', 'id': source}, headers={'Origin': ORIGIN, 'X-CSRF-Token': 'token'})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(xtream.account(self.store, source))


if __name__ == '__main__':
    unittest.main()
