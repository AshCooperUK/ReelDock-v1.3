import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.app import create_app
from backend import acquisition, download_history, m3u_downloads, strm


ORIGIN = 'https://strm-tests.example.com'
POINTER = b'https://media.example.com/private-film.mp4?access=private-token'


class StrmTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.movies = self.base / 'Movies'
        self.series = self.base / 'TV Shows'
        self.movies.mkdir()
        self.series.mkdir()
        self.app = create_app(dict(TESTING=True, DATA_DIR=str(self.base/'data'),
            M3U_DIR=str(self.base/'links'), MEDIA_DIR=str(self.base/'youtube'),
            M3U_DOWNLOAD_ROOTS=json.dumps([str(self.movies), str(self.series)])))
        self.store = self.app.extensions['store']
        self.store.set('public_origin', ORIGIN)
        self.store.set('setup_complete', '1')
        with self.store.connect() as db:
            db.execute("INSERT INTO users VALUES(1,'admin','unused')")
        self.client = self.app.test_client()
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session['user_id'] = 1
            session['csrf'] = 'token'

    def tearDown(self):
        self.temp.cleanup()

    def post(self, route, raw=POINTER, name='Film.strm', client=None, csrf='token', fields=None):
        return (client or self.client).post(route, base_url=ORIGIN,
            headers={'Origin': ORIGIN, 'X-CSRF-Token': csrf},
            data={'strm': (io.BytesIO(raw), name), **(fields or {})})

    def preview(self, raw=POINTER, name='Film.strm'):
        response = self.post('/api/strm/preview', raw, name)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def start(self, raw=POINTER, name='Film.strm', title='Film', kind='movies'):
        preview = self.preview(raw, name)
        return self.post('/api/strm-downloads', raw, name,
            fields=dict(preview_id=preview['id'], title=title, kind=kind))

    def rows(self, table):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM '+table)]

    def dump(self):
        with self.store.connect() as db:
            return '\n'.join(db.iterdump())

    def job(self):
        return self.rows('m3u_downloads')[0]

    def assert_private(self, value):
        text = json.dumps(value)
        for secret in ('https://media.example.com', 'private-token', 'private-film.mp4'):
            self.assertNotIn(secret, text)

    def test_preview_is_offline_stateless_and_returns_only_safe_fields(self):
        before = self.dump()
        with patch('socket.getaddrinfo', side_effect=AssertionError('DNS is forbidden')), \
                patch('socket.create_connection', side_effect=AssertionError('Network is forbidden')), \
                patch('backend.m3u_player.upstream', side_effect=AssertionError('Transfer is forbidden')):
            result = self.preview(b'\xef\xbb\xbf'+POINTER+b'\r\n', 'Programme.S01E02.strm')
        self.assertEqual(self.dump(), before)
        self.assertEqual(set(result), {'id', 'name', 'title', 'kind'})
        self.assertEqual(result['name'], 'Programme.S01E02.strm')
        self.assertEqual(result['kind'], 'series')
        self.assertRegex(result['id'], r'^[a-f0-9]{64}$')
        self.assert_private(result)
        self.assertFalse((self.base/'links').exists())
        self.assertEqual(list(self.movies.iterdir()), [])

    def test_auth_origin_csrf_and_revision_gate_both_actions(self):
        for route in ('/api/strm/preview', '/api/strm-downloads'):
            self.assertEqual(self.post(route, client=self.app.test_client()).status_code, 401)
            self.assertEqual(self.post(route, csrf='wrong').status_code, 403)
            self.assertEqual(self.client.post(route, base_url=ORIGIN).status_code, 403)
        self.store.set('auth_revision', 'changed')
        self.assertEqual(self.post('/api/strm/preview').status_code, 401)
        self.assertEqual(self.rows('strm_sources'), [])

    def test_invalid_files_never_echo_private_input_or_persist(self):
        invalid = [b'', POINTER+b'\nhttps://example.com/other.mp4', b'#EXTM3U\n'+POINTER,
            b'\xff', b'x'*(strm.MAX_BYTES+1), b'plugin://secret-token', b'file:///etc/passwd',
            b'javascript:alert(1)', b'https://user:private-token@example.com/a.mp4',
            b'https://youtu.be/private-token', b'https://www.youtube.com/watch?v=private-token',
            b'https://youtube-nocookie.com/embed/private-token', b'https://example.com/private-token.m3u8',
            b'https://example.com/private-token.mpd', b'http://127.0.0.1/private-token.mp4',
            b'http://[::1]/private-token.mp4', b'http://localhost/private-token.mp4']
        invalid += [POINTER+b'|User-Agent=private-token', b'https://example.com/private-token.m3u']
        before = self.dump()
        for raw in invalid:
            with self.subTest(raw=raw[:40]):
                result = self.post('/api/strm/preview', raw)
                self.assertEqual(result.status_code, 400)
                self.assert_private(result.json)
        self.assertEqual(self.post('/api/strm/preview', name='film.txt').status_code, 400)
        self.assertEqual(self.dump(), before)

    def test_exact_file_limit_accounts_for_multipart_and_bounds_oversized_requests(self):
        raw = POINTER+b'\n'+b' '*(strm.MAX_BYTES-len(POINTER)-1)
        self.assertEqual(len(raw), strm.MAX_BYTES)
        self.assertEqual(self.post('/api/strm/preview', raw).status_code, 200)
        self.assertEqual(self.post('/api/strm/preview', raw+b' ').status_code, 400)
        huge = self.post('/api/strm/preview', b'x'*(strm.MAX_BYTES*3))
        self.assertEqual(huge.status_code, 413)
        self.assertIn('16 KiB', huge.json['error'])
        self.assertEqual(self.rows('strm_sources'), [])

    def test_start_requires_exact_file_name_session_and_preview(self):
        preview = self.preview()
        fields = dict(preview_id=preview['id'], title='Film', kind='movies')
        for raw, name, token in [(POINTER+b'\n', 'Film.strm', preview['id']),
                                  (POINTER, 'Other.strm', preview['id']), (POINTER, 'Film.strm', '0'*64)]:
            result = self.post('/api/strm-downloads', raw, name,
                               fields={**fields, 'preview_id': token})
            self.assertEqual(result.status_code, 400)
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session['csrf'] = 'new-token'
        self.assertEqual(self.post('/api/strm-downloads', csrf='new-token', fields=fields).status_code, 400)
        self.assertEqual(self.rows('strm_sources'), [])
        self.assertEqual(self.rows('m3u_downloads'), [])

    def test_start_atomic_private_source_no_subscription_or_pointer_export(self):
        with patch('backend.m3u_player.upstream', side_effect=AssertionError('Not yet started')):
            result = self.start(title='Confirmed title', kind='series')
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json['state'], 'queued')
        self.assertFalse(result.json['duplicate'])
        source = self.rows('strm_sources')[0]
        job = self.job()
        self.assertEqual(source['url'], POINTER.decode())
        self.assertEqual(source['kind'], 'series')
        self.assertEqual(job['source'], source['id'])
        self.assertEqual(job['path'], 'media.strm')
        self.assertEqual(Path(job['destination']), self.series/'Confirmed title')
        self.assertEqual(self.rows('m3u_sources'), [])
        self.assertEqual(self.rows('m3u_files'), [])
        self.assertFalse((self.base/'links').exists())
        self.assert_private(result.json)

    def test_invalid_destination_rolls_back_source_and_job(self):
        self.store.set('m3u_download_folders', json.dumps({'movies': str(self.base/'missing')}))
        result = self.start()
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.rows('strm_sources'), [])
        self.assertEqual(self.rows('m3u_downloads'), [])

    def test_duplicate_start_does_not_change_active_identity_title_or_type(self):
        first = self.start()
        duplicate = self.start(title='Different title', kind='series')
        self.assertEqual(duplicate.status_code, 200, duplicate.json)
        self.assertTrue(duplicate.json['duplicate'])
        self.assertEqual(duplicate.json['id'], first.json['id'])
        self.assertEqual(len(self.rows('strm_sources')), 1)
        self.assertEqual(self.job()['title'], 'Film')
        self.assertEqual(self.rows('strm_sources')[0]['kind'], 'movies')

    def test_list_history_and_older_active_metadata_never_fabricate_playlist(self):
        self.assertEqual(self.start().status_code, 200)
        item = self.client.get('/api/m3u-server-downloads', base_url=ORIGIN).json['items'][0]
        history = download_history.page(self.store, self.app.config['MEDIA_DIR'])
        older_active = download_history._active_row(self.store, 'm3u', item['id'])['item']
        for row in (item, history['items'][0]['item'], history['active'][0]['item'], older_active):
            self.assertEqual(row['sourceType'], 'strm')
            self.assertEqual(row['sourceName'], 'Film.strm')
            self.assertEqual(row['subscription'], 'STRM')
            self.assertEqual(row['artwork'], '')
            self.assert_private(row)

    def test_cancel_retry_and_hold_use_shared_lifecycle(self):
        self.start()
        original = self.job()
        response = self.client.post('/api/m3u-server-downloads', base_url=ORIGIN,
            headers={'Origin': ORIGIN, 'X-CSRF-Token': 'token'},
            json=dict(action='cancel', id=original['id']))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.job()['state'], 'cancelled')
        identity = m3u_downloads.enqueue(self.app, self.store, original['source'], original['path'])
        self.assertEqual(identity, original['id'])
        self.assertEqual(self.job()['state'], 'queued')
        acquisition.begin_hold(self.store)
        acquisition.settle_after_restart(self.store)
        self.assertEqual(self.job()['state'], 'cancelled')
        with self.assertRaises(acquisition.AcquisitionHeld):
            m3u_downloads.enqueue(self.app, self.store, original['source'], original['path'])
        self.assertEqual(self.start(raw=POINTER+b'\n').status_code, 400)
        self.assertEqual(len(self.rows('strm_sources')), 1)
        acquisition.resume(self.store)
        self.assertEqual(m3u_downloads.enqueue(self.app, self.store, original['source'], original['path']), identity)

    def response(self, data):
        response = Mock(status=200)
        response.read = io.BytesIO(data).read
        response.getheader.side_effect = lambda key, default='': {
            'Content-Type': 'video/mp4', 'Content-Length': str(len(data))}.get(key, default)
        return Mock(), response, POINTER.decode()

    def test_real_shared_transfer_resolves_private_source_and_publishes_exact_bytes(self):
        self.start()
        data = b'\x00\x00\x00\x20ftypisom'+b'x'*6000
        with patch('backend.m3u_player.upstream', return_value=self.response(data)) as upstream:
            m3u_downloads.transfer(self.app, self.store, self.job(), threading.Event())
        upstream.assert_called_once_with(POINTER.decode())
        job = self.job()
        self.assertEqual(job['state'], 'complete')
        self.assertEqual(Path(job['destination']), self.movies/'Film.mp4')
        self.assertEqual(Path(job['destination']).read_bytes(), data)
        self.assertFalse(list(self.movies.rglob('*.part')))

    def test_cancel_and_shutdown_never_publish_and_saved_file_is_kept(self):
        self.start()
        data = b'\x00\x00\x00\x20ftypisom'+b'x'*6000
        with self.store.connect() as db:
            db.execute("UPDATE m3u_downloads SET state='cancelling'")
        with patch('backend.m3u_player.upstream', return_value=self.response(data)):
            m3u_downloads.transfer(self.app, self.store, self.job(), threading.Event())
        self.assertEqual(self.job()['state'], 'cancelled')
        self.assertFalse(list(self.movies.rglob('*.mp4')))
        m3u_downloads.enqueue(self.app, self.store, self.job()['source'], self.job()['path'])
        stop = threading.Event()
        stop.set()
        with patch('backend.m3u_player.upstream', return_value=self.response(data)):
            m3u_downloads.transfer(self.app, self.store, self.job(), stop)
        self.assertEqual(self.job()['state'], 'queued')
        self.assertFalse(list(self.movies.rglob('*.part')))
        saved = self.movies/'Film.mp4'
        saved.write_bytes(b'owner media')
        with patch('backend.m3u_player.upstream', return_value=self.response(data)):
            m3u_downloads.transfer(self.app, self.store, self.job(), threading.Event())
        self.assertEqual(self.job()['state'], 'failed')
        self.assertEqual(saved.read_bytes(), b'owner media')

    def test_manual_path_cannot_be_substituted_and_symlink_target_rejected(self):
        self.start()
        job = self.job()
        with self.assertRaises(ValueError):
            m3u_downloads.enqueue(self.app, self.store, job['source'], '../outside.strm')
        outside = self.base/'outside'
        outside.mkdir()
        (self.movies/'Film.mp4').symlink_to(outside/'saved.mp4')
        data = b'\x00\x00\x00\x20ftypisom'+b'x'*6000
        with patch('backend.m3u_player.upstream', return_value=self.response(data)):
            m3u_downloads.transfer(self.app, self.store, job, threading.Event())
        self.assertEqual(self.job()['state'], 'failed')
        self.assertEqual(list(outside.iterdir()), [])

    def test_episode_identity_organises_only_a_confirmed_season(self):
        result = self.start(name='Programme.S03E02.strm', title='Programme S03E02', kind='series')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(Path(self.job()['destination']), self.series/'Programme'/'Season 03'/'Programme S03E02')

    def test_html_json_and_manifest_response_never_published_even_with_mp4_url(self):
        self.start()
        for mime, data in [('text/html', b'<html>private-token</html>'),
                           ('application/octet-stream', b'<!DOCTYPE html><html>private-token</html>'),
                           ('application/dash+xml', b'<MPD>private-token</MPD>'),
                           ('application/json', b'{"private-token":"error"}'),
                           ('video/mp4', b'#EXTM3U\nprivate-token')]:
            with self.subTest(mime=mime):
                response = self.response(data)
                response[1].getheader.side_effect = lambda key, default='': {
                    'Content-Type': mime, 'Content-Length': str(len(data))}.get(key, default)
                with patch('backend.m3u_player.upstream', return_value=response):
                    m3u_downloads.transfer(self.app, self.store, self.job(), threading.Event())
                self.assertEqual(self.job()['state'], 'failed')
                self.assert_private(self.job())
                self.assertFalse(list(self.movies.iterdir()))

    def test_provider_value_error_is_private_and_global_pause_prevents_network(self):
        self.start()
        with patch('backend.m3u_player.upstream', side_effect=ValueError(POINTER.decode())):
            m3u_downloads.transfer(self.app, self.store, self.job(), threading.Event())
        self.assertEqual(self.job()['state'], 'failed')
        self.assert_private(self.job())
        m3u_downloads.enqueue(self.app, self.store, self.job()['source'], self.job()['path'])
        self.store.set('download_pause_downloads', 'true')
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        with patch('backend.m3u_player.upstream', side_effect=AssertionError('Paused')) as upstream:
            m3u_downloads.loop(self.app, self.store, stop)
        upstream.assert_not_called()
        self.assertEqual(self.job()['state'], 'queued')


if __name__ == '__main__':
    unittest.main()
