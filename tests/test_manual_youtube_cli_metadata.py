"""Verify manual download facts using the pinned downloader's real JSON output.

The extractor fixture contains a large, legitimate fragment list.  The CLI reads
it through --load-info-json and downloads only a tiny local MP4; a child-process
audit hook forbids DNS/socket access, so this never contacts a provider.
"""
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app import create_app
from backend import download_options, downloads


ID = 'abcdefghijk'
CHANNEL = 'UC' + 'a' * 22
MEDIA = b'\x00\x00\x00\x20ftypisom' + b'offline media fixture' * 100
OFFLINE_CLI = """\
import sys
def deny_network(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo'):
        raise AssertionError('The offline metadata fixture attempted network access')
sys.addaudithook(deny_network)
from backend.youtube_download import main
main()
"""


class ManualYoutubeCliMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / 'media'
        self.media.mkdir()
        self.app = create_app(dict(TESTING=True, DATA_DIR=str(self.root / 'data'), MEDIA_DIR=str(self.media)))
        self.store = self.app.extensions['store']
        self.store.set('setup_complete', '1')
        video = dict(id=ID, title='Offline long video', channel=CHANNEL, state='published', kind='videos', date='2020-01-01')
        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES(?,?,1)', (CHANNEL, json.dumps(dict(id=CHANNEL, name='Offline fixture channel'))))
            db.execute('INSERT INTO videos VALUES(?,?,?,?,?,0)', (ID, CHANNEL, video['date'], video['date'], json.dumps(video)))
        download_options.save(self.store, dict(force_transcode=False, hardware_encoding=False, sponsorblock='off', authenticated_retry=False))
        downloads.enqueue(self.store, video, '720')
        downloads.update(self.store, ID, 'downloading')
        with self.store.connect() as db:
            self.job = dict(db.execute('SELECT * FROM downloads WHERE id=?', (ID,)).fetchone())
        self.source = self.root / 'source.mp4'
        self.source.write_bytes(MEDIA)
        self.fixture = self.root / 'extractor.json'
        self.fixture.write_text(json.dumps(dict(
            id=ID, title='Offline long video', extractor='generic', extractor_key='Generic',
            webpage_url='https://example.invalid/watch?v=' + ID,
            duration=55_000, width=1280, height=720, is_live=False, live_status='not_live',
            formats=[
                dict(url=self.source.as_uri(), ext='mp4', format_id='18', vcodec='avc1', acodec='mp4a', width=1280, height=720, protocol='http'),
                dict(url='https://example.invalid/manifest', ext='webm', format_id='249', vcodec='none', acodec='opus', protocol='http_dash_segments',
                     fragments=[dict(url='https://example.invalid/fragment/' + str(i) + 'x' * 450, duration=5.0) for i in range(11_000)]),
            ],
        )))
        self.assertGreater(self.fixture.stat().st_size, 5_000_000)

    def cli(self, args):
        """Keep all product options; replace only the external extractor input."""
        self.assertEqual(args[1:3], ['-m', 'backend.youtube_download'])
        separator = args.index('--')
        options = args[3:separator] + ['--enable-file-urls', '--load-info-json', str(self.fixture)]
        return subprocess.run([sys.executable, '-c', OFFLINE_CLI, *options], capture_output=True, text=True, timeout=20)

    def test_real_cli_can_produce_large_full_info_for_a_successful_local_transfer(self):
        """Prove the size comes from yt-dlp serialization, not a mocked writer."""
        output = self.root / 'full-info'
        output.mkdir()
        args = downloads.command(ID, '720', output)
        args[args.index('--'):args.index('--')] = ['--write-info-json', '--match-filter', downloads.MANUAL_LIVE_FILTER]
        result = self.cli(args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((output / 'video.mp4').read_bytes(), MEDIA)
        info = output / 'video.info.json'
        self.assertGreater(info.stat().st_size, 5_000_000)
        self.assertEqual(json.loads(info.read_text())['id'], ID)

    def test_real_cli_manual_download_uses_small_actual_facts_and_publishes(self):
        attempts = []
        generated = {}

        def offline_transfer(store, job, stop, args, log, media):
            attempts.append(args[:])
            result = self.cli(args)
            self.assertEqual(result.returncode, 0, result.stderr)
            generated.update({path.name: path.read_bytes() for path in log.parent.glob('*.json')})
            return result.returncode, result.stderr

        with patch('backend.downloads.run_process', side_effect=offline_transfer), \
             patch('backend.downloads.library.write_metadata', return_value='Saved offline metadata'):
            result = downloads.run_download(self.store, self.media, self.job, threading.Event())

        self.assertEqual(result, 'Saved offline metadata')
        self.assertEqual(len(attempts), 1)
        self.assertNotIn('--write-info-json', attempts[0], 'Full formats/fragments must not become the bounded identity record')
        self.assertNotIn('video.info.json', generated)
        self.assertEqual(len(generated), 1)
        raw = next(iter(generated.values()))
        self.assertLess(len(raw), 4096)
        facts = json.loads(raw)
        self.assertEqual(facts['id'], ID)
        self.assertEqual(facts['duration'], 55_000)
        self.assertEqual(facts['live_status'], 'not_live')
        self.assertNotIn('formats', facts)
        self.assertNotIn('url', facts)
        published = list(self.media.rglob('*.mp4'))
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].read_bytes(), MEDIA)
        self.assertEqual(self.source.read_bytes(), MEDIA)
        self.assertFalse(list(self.media.glob('.reeldock-download-*')))


if __name__ == '__main__':
    unittest.main()
