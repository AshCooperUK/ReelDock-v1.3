"""ReelDock is a media manager/downloader: no route may stream or convert video for the browser."""
import unittest
from pathlib import Path
from unittest.mock import patch

import test_view_media as vm
from backend.view_media import local

FORBIDDEN_RULES = (
    '/api/playback/<video_id>', '/media/video/<video_id>', '/api/saved-playback', '/api/media-management/file',
    '/api/m3u-stream/<token>', '/api/m3u-local/<identity>', '/api/m3u/<source>/playback', '/api/m3u/<source>/convert',
    '/api/m3u-converted/<token>/<name>',
)


class NoPlaybackTests(unittest.TestCase):
    setUp = vm.ViewMediaTests.setUp
    tearDown = vm.ViewMediaTests.tearDown
    get = vm.ViewMediaTests.get

    def test_url_map_has_no_playback_or_conversion_route(self):
        rules = {rule.rule for rule in self.app.url_map.iter_rules()}
        for forbidden in FORBIDDEN_RULES:
            self.assertNotIn(forbidden, rules)
        for rule in rules:
            self.assertNotRegex(rule, r'playback|/convert|m3u-stream|m3u-converted|/media/video')

    def test_removed_routes_are_404_for_signed_in_users_and_never_spawn_a_process(self):
        urls = ('/api/playback/abcdefghijk', '/media/video/abcdefghijk', '/api/saved-playback?kind=local&id=x',
                '/api/media-management/file?key=x', '/api/m3u-stream/token', '/api/m3u-local/id',
                '/api/m3u/source/playback?path=x', '/api/m3u-converted/token/index.m3u8')
        with patch('subprocess.Popen') as spawn, patch('subprocess.run') as run:
            for url in urls:
                for method in ('get', 'head'):
                    response = getattr(self.client, method)(url, base_url=vm.ORIGIN)
                    self.assertEqual(response.status_code, 404, (method, url))
            for url in ('/api/m3u/source/convert', '/api/saved-playback'):
                response = self.client.post(url, base_url=vm.ORIGIN, json={'start': 0}, headers={'Origin': vm.ORIGIN, 'X-CSRF-Token': 'token'})
                self.assertEqual(response.status_code, 404, url)
            spawn.assert_not_called()
            run.assert_not_called()

    def test_local_files_are_never_served_but_artwork_still_is(self):
        folder = self.root / 'Movies' / 'Film (2020)'
        folder.mkdir(parents=True)
        (folder / 'Film (2020).mp4').write_bytes(b'0123456789')
        (folder / 'poster.jpg').write_bytes(b'poster')
        state = {}
        local._scan(self.root, state)
        local._CACHE[str(self.root)] = state
        identity = state['items'][0]['action']['id']
        for method in ('get', 'head'):
            self.assertEqual(getattr(self.client, method)('/api/view-media/local/' + identity + '/file', base_url=vm.ORIGIN).status_code, 404)
            self.assertEqual(getattr(self.client, method)('/api/view-media/local/' + identity + '/file', base_url=vm.ORIGIN, headers={'Range': 'bytes=0-3'}).status_code, 404)
        art = self.get('/api/view-media/local/' + identity + '/art')
        self.assertEqual(art.status_code, 200)
        self.assertEqual(art.data, b'poster')
        art.close()
        self.assertEqual(local.media_file(self.root, identity, 'file').read_bytes(), b'0123456789', 'internal lookup used by management is kept')

    def test_csp_forbids_frames_and_media_and_the_hls_runtime_is_gone(self):
        policy = self.get('/app/').headers['Content-Security-Policy']
        directives = {part.strip().split(' ')[0]: part.strip() for part in policy.split(';') if part.strip()}
        self.assertEqual(directives['frame-src'], "frame-src 'none'")
        self.assertEqual(directives['media-src'], "media-src 'none'")
        self.assertNotRegex(policy, r'youtube|blob:')
        for kept in ("default-src 'self'", "script-src 'self'", "connect-src 'self'", "frame-ancestors 'none'", "base-uri 'none'", "form-action 'self'"):
            self.assertIn(kept, policy)
        for path in ('/static/vendor/hls.min.js', '/static/vendor/hls.LICENSE', '/static/vendor/Apache-2.0.txt'):
            self.assertEqual(self.get(path).status_code, 404, path)
        self.assertFalse((Path(__file__).resolve().parents[1] / 'backend' / 'static' / 'vendor').exists())
        from backend import credits
        credits.catalog.cache_clear()
        self.assertNotIn('browser-hls', credits.catalog())
        self.assertTrue(any(key.startswith('python-') for key in credits.catalog()), 'remaining notices are kept')

    def test_no_backend_module_serves_inline_video(self):
        backend = Path(__file__).resolve().parents[1] / 'backend'
        for name in ('saved_playback.py', 'm3u_transcode.py'):
            self.assertFalse((backend / name).exists(), name)
        source = (backend / 'm3u_player.py').read_text()
        for needle in ('m3u-stream', 'application/vnd.apple.mpegurl', 'convert', 'LocalVideoResponse'):
            self.assertNotIn(needle, source)


if __name__ == '__main__':
    unittest.main()
