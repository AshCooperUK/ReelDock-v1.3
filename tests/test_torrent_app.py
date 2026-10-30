"""Torrent scans and download controls stay inside the ReelDock app."""
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from backend.app import create_app
from backend.downloaders.torrent.rename import library_path
from backend.torrents import stop
from tests.torrentutil import build_torrent

ORIGIN = 'https://dev.example.com'


class TorrentAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / 'library'
        self.base.mkdir()
        self.app = create_app({'TESTING': True, 'DATA_DIR': self.tmp.name, 'MEDIA_DIR': self.tmp.name + '/youtube'})
        self.store = self.app.extensions['store']
        self.store.set('public_origin', ORIGIN)
        self.store.set('setup_complete', '1')
        self.store.set('storage_base', str(self.base))
        with self.store.connect() as db:
            db.execute("INSERT INTO users VALUES(1,'owner','unused')")
        self.client = self.app.test_client()
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session['user_id'] = 1
            session['csrf'] = 'token'
        self.assertNotIn('torrent_engine', self.app.extensions)

    def tearDown(self):
        stop(self.app)
        self.tmp.cleanup()

    def post(self, path, body, csrf='token'):
        return self.client.post(path, base_url=ORIGIN, json=body, headers={'Origin': ORIGIN, 'X-CSRF-Token': csrf})

    def test_rename_uses_downloads_folders(self):
        self.assertEqual(library_path('Show', 'Show.S01E02.mkv', 'video'), 'TV Shows/Show/Season 1/Show s01e02.mkv')
        self.assertIsNone(library_path('Film', 'Film.mp4', 'video'))
        self.assertEqual(library_path('Film', 'Film.1999.mkv', 'video'), 'Movies/Film (1999)/Film (1999).mkv')
        self.assertEqual(library_path('Song', 'song.mp3', 'audio'), 'Downloads/Torrents/Audio/song.mp3')

    def test_routes_require_sign_in_and_a_directory(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get('/api/torrents', base_url=ORIGIN).status_code, 401)
        self.assertEqual(self.post('/api/torrents/preview', {'url': 'https://example.com/a.torrent'}, csrf='wrong').status_code, 403)
        self.store.set('storage_base', '')
        self.assertEqual(self.client.get('/api/torrents', base_url=ORIGIN).json['torrents'], [])
        self.assertEqual(self.post('/api/torrents/preview', {'url': 'https://example.com/a.torrent'}).status_code, 409)

    def test_scan_rejects_non_media_before_any_download(self):
        payload = build_torrent([('notes.txt', b'not media')], name='Notes')
        url = self.serve({'/notes.torrent': payload})
        response = self.post('/api/torrents/preview', {'url': url})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(response.json['accepted'])
        self.assertFalse((self.base / 'Downloads' / '.reeldock-torrents').exists())
        self.assertEqual(self.post('/api/torrents', {'action': 'start', 'preview_id': response.json['id']}).status_code, 400)

    def test_scan_then_start_pause_and_remove(self):
        payload = build_torrent([('Film.mp4', b'1234')], name='Film')
        url = self.serve({'/film.torrent': payload})
        scanned = self.post('/api/torrents/preview', {'url': url})
        self.assertEqual(scanned.status_code, 200, scanned.json)
        self.assertTrue(scanned.json['accepted'])
        self.assertEqual(scanned.json['files'][0]['kind'], 'video')
        self.assertFalse((self.base / 'Downloads' / '.reeldock-torrents').exists())
        started = self.post('/api/torrents', {'action': 'start', 'preview_id': scanned.json['id'], 'files': [scanned.json['files'][0]['index']]})
        self.assertEqual(started.status_code, 200, started.json)
        job_id = started.json['id']
        self.assertIn(started.json['state'], {'queued', 'downloading', 'paused'})
        self.assertGreater(started.json['added'], 1_700_000_000)
        self.assertTrue((self.base / 'Downloads' / '.reeldock-torrents').is_dir())
        paused = self.post('/api/torrents/' + job_id, {'action': 'pause'})
        self.assertEqual(paused.status_code, 200, paused.json)
        self.assertEqual(paused.json['state'], 'paused')
        listed = self.client.get('/api/torrents', base_url=ORIGIN).json['torrents']
        self.assertEqual([job['id'] for job in listed], [job_id])
        self.assertEqual(self.post('/api/torrents/' + job_id, {'action': 'remove'}).status_code, 200)
        self.assertEqual(self.client.get('/api/torrents', base_url=ORIGIN).json['torrents'], [])

    def upload(self, payload, filename, csrf='token'):
        return self.client.post('/api/torrents/preview', data={'torrent': (io.BytesIO(payload), filename)}, content_type='multipart/form-data', base_url=ORIGIN, headers={'Origin': ORIGIN, 'X-CSRF-Token': csrf})

    def test_upload_scans_the_torrent_file_before_download(self):
        payload = build_torrent([('Film.mp4', b'1234')], name='Film')
        denied = self.upload(payload, 'Film.torrent', csrf='wrong')
        self.assertEqual(denied.status_code, 403)
        scanned = self.upload(payload, 'Film.torrent')
        self.assertEqual(scanned.status_code, 200, scanned.json)
        self.assertTrue(scanned.json['accepted'])
        self.assertEqual(scanned.json['files'][0]['kind'], 'video')
        self.assertFalse((self.base / 'Downloads' / '.reeldock-torrents').exists())
        rejected = self.upload(b'not a torrent', 'notes.txt')
        self.assertEqual(rejected.status_code, 400)
        empty = self.upload(b'', 'empty.torrent')
        self.assertEqual(empty.status_code, 400)
        oversized = self.upload(b'x' * (2_000_001), 'big.torrent')
        self.assertIn(oversized.status_code, {400, 413})

    def serve(self, files):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                data = files.get(self.path)
                if data is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        return 'http://127.0.0.1:%d%s' % (server.server_address[1], next(iter(files)))
