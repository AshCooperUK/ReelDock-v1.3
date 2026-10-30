"""Fix match: TMDB re-linking and the user's own details, locked against automatic changes."""
import json
import unittest
from unittest.mock import patch

from backend import m3u_catalogue as catalogue, m3u_overrides as overrides, m3u_sidecars
from tests import test_m3u_player as player
PlayerTests, ORIGIN = player.PlayerTests, player.ORIGIN

PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 64
MOVIES = {
    'search/movie': {'results': [{'id': 999, 'title': 'The Right Film', 'release_date': '2001-05-04', 'overview': 'x' * 300, 'poster_path': '/right.jpg'},
                                 {'id': 'bad'}]},
    'movie/999': {'id': 999, 'title': 'The Right Film', 'release_date': '2001-05-04', 'overview': 'Correct plot.', 'vote_average': 7.5, 'poster_path': '/right.jpg', 'credits': {'cast': []}},
    'movie/123': {'id': 123, 'title': 'Wrong Film', 'release_date': '2024-01-01', 'overview': 'Wrong plot.', 'poster_path': '/wrong.jpg'},
}


def fake_get(self, path, **params):
    if path.startswith('find/'): return {'movie_results': [MOVIES['movie/123']]}
    return dict(MOVIES.get(path, {}))


class FixMatchTests(unittest.TestCase):
    setUp = PlayerTests.setUp
    tearDown = PlayerTests.tearDown
    get = PlayerTests.get

    def post(self, body, csrf='token'):
        with self.client.session_transaction(base_url=ORIGIN) as session: session['csrf'] = 'token'
        return self.client.post('/api/m3u/'+self.source+'/match', base_url=ORIGIN, json=dict(body, path=self.row['path']),
                                headers={'Origin': ORIGIN, 'X-CSRF-Token': csrf})

    def title(self):
        with self.store.connect() as db:
            return dict(db.execute('SELECT t.* FROM m3u_catalog_titles t JOIN m3u_catalog_entries c ON c.key=t.key WHERE c.source=? AND c.path=?',
                                   (self.source, self.row['path'])).fetchone())

    def setUpTmdb(self):
        self.store.set('m3u_tmdb_token', 'a'*32)
        catalogue.index(self.store)

    def test_security_auth_and_csrf(self):
        self.setUpTmdb()
        self.assertEqual(self.app.test_client().get('/api/m3u/'+self.source+'/match', query_string={'path': self.row['path']}, base_url=ORIGIN).status_code, 401)
        self.assertEqual(self.post({'action': 'search', 'query': 'x', 'media_type': 'movie'}, csrf='wrong').status_code, 403)
        state = self.get('match', path=self.row['path']).json
        self.assertFalse(state['locked']); self.assertEqual(state['media_type'], 'movie')

    def test_search_then_choose_locks_against_automatic_matching_until_reverted(self):
        self.setUpTmdb()
        with patch('backend.m3u_metadata.TMDB.get', fake_get):
            results = self.post({'action': 'search', 'query': 'Right', 'year': '2001', 'media_type': 'movie'}).json['results']
            self.assertEqual([r['id'] for r in results], [999])
            self.assertEqual(results[0]['year'], '2001'); self.assertTrue(results[0]['overview'].endswith('…'))
            self.assertEqual(results[0]['poster'], '/api/m3u-artwork/right.jpg?size=w185')
            state = self.post({'action': 'tmdb', 'media_type': 'movie', 'tmdb_id': 999}).json
            self.assertTrue(state['locked']); self.assertEqual(state['mode'], 'tmdb')
            self.assertEqual(self.title()['locked'], 1)
            # Background matching skips the locked title.
            with self.store.connect() as db: db.execute('UPDATE m3u_catalog_titles SET checked=0')
            while catalogue.enrich(self.store): pass
            data = json.loads(self.title()['data'])
            self.assertEqual((data['tmdb_id'], data['title']), (999, 'The Right Film'))
            # Opening the title uses the chosen match and does not rewrite it.
            details = self.get('details', path=self.row['path']).json
            self.assertEqual(details['description'], 'Correct plot.')
            self.assertIn('locked', details['message'])
            self.assertEqual(json.loads(self.title()['data'])['tmdb_id'], 999)
            item = next(i for i in self.get('entries').json['items'] if i['path'] == self.row['path'])
            self.assertEqual(item['title'], 'The Right Film')
            # A new import that drops and re-adds the title keeps the lock.
            with self.store.connect() as db:
                db.execute('DELETE FROM m3u_catalog_entries'); db.execute('DELETE FROM m3u_catalog_titles')
            catalogue.index(self.store)
            self.assertEqual(json.loads(self.title()['data'])['tmdb_id'], 999)
            self.assertFalse(self.post({'action': 'revert'}).json['locked'])
            self.assertEqual((self.title()['locked'], self.title()['checked']), (0, 0))
            while catalogue.enrich(self.store): pass
            self.assertEqual(json.loads(self.title()['data']).get('tmdb_id'), 123)

    def test_own_details_with_poster_are_locked_shown_and_exported(self):
        fields = dict(title='My Title', year='1999', plot='My plot.', poster_url='https://images.example.org/p.png',
                      genres='Drama, Comedy', rating='8.4', cast='A Person, B Person')
        with patch('backend.m3u_overrides.fetch_poster', return_value=PNG) as fetch:
            state = self.post({'action': 'manual', 'fields': fields}).json
        fetch.assert_called_once_with('https://images.example.org/p.png')
        self.assertTrue(state['locked']); self.assertEqual(state['mode'], 'manual')
        self.assertEqual(state['data']['genres'], ['Drama', 'Comedy']); self.assertEqual(state['data']['rating'], 8.4)
        with patch('backend.m3u_metadata.TMDB.get', side_effect=AssertionError('TMDB must not be used')):
            self.store.set('m3u_tmdb_token', 'a'*32)
            details = self.get('details', path=self.row['path']).json
            self.assertEqual((details['title'], details['description']), ('My Title', 'My plot.'))
            self.assertEqual(details['facts']['Cast'], 'A Person, B Person')
            self.assertEqual(self.get('artwork', path=self.row['path']).data, PNG)
            with self.store.connect() as db: db.execute('UPDATE m3u_catalog_titles SET checked=0,locked=1 WHERE locked=0')
            self.assertFalse(catalogue.enrich(self.store))  # Locked titles are never picked for matching.
            with self.store.connect() as db: db.execute("UPDATE m3u_catalog_titles SET locked=0 WHERE data NOT LIKE '%\"manual\"%'")
            self.assertEqual(json.loads(self.title()['data'])['title'], 'My Title')
            m3u_sidecars.batch(self.app, self.store)
        folder = (self.root/self.row['path']).parent
        nfo = (self.root/self.row['path']).with_suffix('.nfo').read_text()
        self.assertIn('<title>My Title</title>', nfo); self.assertIn('<year>1999</year>', nfo); self.assertIn('<genre>Drama</genre>', nfo)
        self.assertEqual((folder/(self.root/self.row['path']).stem).with_name((self.root/self.row['path']).stem+'-poster.png').read_bytes(), PNG)
        art = self.client.get(state['data']['artwork'], base_url=ORIGIN)
        self.assertEqual(art.data, PNG); self.assertEqual(art.mimetype, 'image/png')
        self.post({'action': 'revert'})
        self.assertFalse(overrides.poster_path(self.store, self.title()['key']).exists())

    def test_own_details_validation(self):
        for fields, message in [({'title': ''}, 'Enter a title.'), ({'title': 'X', 'year': '99'}, 'four-digit year'),
                                ({'title': 'X', 'rating': '11'}, 'rating from 0 to 10'), ({'title': 'X', 'genres': ','.join(['g']*21)}, 'Use up to 20')]:
            with self.subTest(fields=fields):
                response = self.post({'action': 'manual', 'fields': fields})
                self.assertEqual(response.status_code, 400); self.assertIn(message, response.json['error'])
        self.assertFalse(self.get('match', path=self.row['path']).json['locked'])

    def test_poster_fetch_is_https_public_and_image_only(self):
        for url, message in [('http://images.example.org/a.jpg', 'HTTPS'), ('https://user:pw@images.example.org/a.jpg', 'HTTPS'),
                             ('https://images.example.org:8443/a.jpg', 'HTTPS'), ('https://10.0.0.5/a.jpg', 'IP address'),
                             ('https://[::1]/a.jpg', 'IP address')]:
            with self.subTest(url=url), self.assertRaises(ValueError) as caught: overrides.fetch_poster(url)
            self.assertIn(message, str(caught.exception))
        with patch('backend.m3u_overrides.public_addresses', side_effect=ValueError('private')), self.assertRaises(ValueError) as caught:
            overrides.fetch_poster('https://intranet.example.org/a.jpg')
        self.assertIn('public internet host', str(caught.exception))

        class Response:
            def __init__(self, status, kind, body, location=''): self.status, self.kind, self.body, self.location = status, kind, body, location
            def getheader(self, name, default=''): return {'Content-Type': self.kind, 'Location': self.location}.get(name, default)
            def read(self, limit): return self.body[:limit]
        responses = []
        class Connection:
            def __init__(self, *a, **k): pass
            def request(self, *a, **k): pass
            def getresponse(self): return responses.pop(0)
            def close(self): pass
        with patch('backend.m3u_overrides.public_addresses', return_value=['93.184.216.34']), \
             patch('backend.m3u_overrides.socket.create_connection'), patch('backend.m3u_overrides.ssl.create_default_context'), \
             patch('backend.m3u_overrides.http.client.HTTPSConnection', Connection):
            responses[:] = [Response(200, 'text/html', b'<html>')]
            with self.assertRaises(ValueError): overrides.fetch_poster('https://images.example.org/a.jpg')
            responses[:] = [Response(200, 'image/png', b'not an image')]
            with self.assertRaises(ValueError): overrides.fetch_poster('https://images.example.org/a.jpg')
            responses[:] = [Response(200, 'image/png', PNG + b'0' * overrides.POSTER_LIMIT)]
            with self.assertRaisesRegex(ValueError, '8 MB'): overrides.fetch_poster('https://images.example.org/a.jpg')
            responses[:] = [Response(302, '', b'', 'http://images.example.org/b.png')]
            with self.assertRaisesRegex(ValueError, 'HTTPS'): overrides.fetch_poster('https://images.example.org/a.jpg')
            responses[:] = [Response(302, '', b'', '/b.png'), Response(200, 'image/png; charset=binary', PNG)]
            self.assertEqual(overrides.fetch_poster('https://images.example.org/a.jpg'), PNG)


del PlayerTests

if __name__ == "__main__":
    unittest.main()
