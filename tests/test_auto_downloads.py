import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from backend.app import create_app
import xml.etree.ElementTree as ET

from backend.auto_downloads import destination, save_folder, scan, validate_watch, write_youtube_sidecars, youtube_card, youtube_id
from backend.download_options import save

LOCAL = 'http://127.0.0.1:8790'


class AutoDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = self.root / 'ReelDock'
        self.base.mkdir()
        self.app = create_app(dict(
            TESTING=True, DATA_DIR=str(self.root / 'data'), MEDIA_DIR=str(self.root / 'youtube'),
            M3U_DIR=str(self.root / 'links'), REELDOCK_STORAGE_ROOTS=json.dumps([str(self.base)]),
            LOCAL_SETUP_URL=LOCAL, APP_PORT='8790',
        ))
        self.store = self.app.extensions['store']
        self.store.set('storage_base', str(self.base))
        self.store.set('setup_complete', '1')
        for name in ('YouTube', 'M3U', 'Movies', 'TV Shows', 'Downloads'):
            (self.base / name).mkdir()
        self.watch = self.base / 'Auto'
        self.watch.mkdir()
        save_folder(self.app, self.store, str(self.watch))
        save(self.store, {'auto_downloads': True})
        self.stop = threading.Event()

    def tearDown(self):
        from backend import torrents, nzb
        try:
            # Invalid intake can still have opened a native engine. Join its
            # scheduler before deleting the SQLite directory it reads.
            torrents.stop(self.app)
            nzb.stop(self.app)
        finally:
            self.tmp.cleanup()

    def place(self, name, body):
        path = self.watch / name
        path.write_bytes(body if isinstance(body, bytes) else body.encode())
        os.utime(path, (time.time() - 30, time.time() - 30))
        return path

    def test_watch_folder_stays_outside_the_library(self):
        for folder in (self.base, self.base / 'Movies', self.base / 'Downloads', self.base / 'TV Shows'):
            with self.assertRaises(ValueError):
                validate_watch(self.app, self.store, str(folder))
        self.assertEqual(validate_watch(self.app, self.store, str(self.watch)), self.watch)

    def test_strm_uses_an_existing_show_folder_and_nzb_waits(self):
        show = self.base / 'TV Shows' / 'Cupertino (2026)'
        show.mkdir(parents=True)
        strm = self.place('Cupertino.S01E01.1080p.strm', 'https://example.com/video.mp4\n')
        nzb = self.place('show.nzb', b'<nzb></nzb>')
        calls = []

        def fetch(url):
            calls.append(url)
            return '.mp4', 5, [b'video']

        scan(self.app, self.store, self.stop, {'strm', 'nzb'}, fetch=fetch)
        saved = list(show.rglob('*.mp4'))
        self.assertEqual(calls, ['https://example.com/video.mp4'])
        self.assertEqual(len(saved), 1)
        self.assertIn('s01e01', saved[0].name)
        self.assertEqual(saved[0].read_bytes(), b'video')
        self.assertFalse(strm.exists())
        self.assertTrue((self.watch / '.reeldock-accepted' / strm.name).is_file())
        self.assertTrue(nzb.is_file())
        scan(self.app, self.store, self.stop, {'strm', 'nzb'}, fetch=fetch)
        self.assertEqual(calls, ['https://example.com/video.mp4'])

    def test_torrent_starts_once_and_a_paused_switch_does_nothing(self):
        torrent = self.place('Show.S02E03.mkv.torrent', b'd8:info')
        seen = []

        def starter(_app, _store, data, name):
            seen.append((name, data))

        scan(self.app, self.store, self.stop, {'torrent'}, torrent=starter)
        self.assertEqual(seen, [('Show.S02E03.mkv.torrent', b'd8:info')])
        self.assertTrue((self.watch / '.reeldock-accepted' / torrent.name).is_file())
        scan(self.app, self.store, self.stop, {'torrent'}, torrent=starter)
        self.assertEqual(len(seen), 1)
        save(self.store, {'pause_downloads': True})
        self.place('Other.S01E01.mkv.torrent', b'd8:more')
        scan(self.app, self.store, self.stop, {'torrent'}, torrent=starter)
        self.assertEqual(len(seen), 1)

    def test_cancelled_torrent_readded_with_original_mtime_starts_again(self):
        from backend.torrents import _engine, stop
        from tests.torrentutil import build_torrent
        payload=build_torrent([('Fixture.S01E02.mkv',b'x'*4096)])
        path=self.place('Fixture.torrent',payload)
        original_mtime=path.stat().st_mtime_ns
        engine=_engine(self.app,self.store)
        calls=[]
        def starter(app,store,data,name):
            preview=engine.preview_bytes(data,name)
            calls.append(engine.start(preview['id'],[0])['id'])
        try:
            scan(self.app,self.store,self.stop,{'torrent'},torrent=starter)
            self.assertEqual(len(engine.jobs()),1)
            engine.remove(calls[0])
            self.assertEqual(engine.jobs(),[])
            archived=self.watch/'.reeldock-accepted'/path.name
            archived.rename(path)
            self.assertEqual(path.stat().st_mtime_ns,original_mtime)
            scan(self.app,self.store,self.stop,{'torrent'},torrent=starter)
            self.assertEqual(len(calls),2)
            self.assertEqual(len(engine.jobs()),1)
            # Another deliberate arrival while active must not duplicate the job.
            (self.watch/'.reeldock-accepted'/path.name).rename(path)
            scan(self.app,self.store,self.stop,{'torrent'},torrent=starter)
            self.assertEqual(len(calls),3)
            self.assertEqual(len(engine.jobs()),1)
            scan(self.app,self.store,self.stop,{'torrent'},torrent=starter)
            self.assertEqual(len(calls),3)
        finally:stop(self.app)

    def test_failed_unchanged_file_keeps_backoff_but_new_copy_can_retry(self):
        import shutil
        path=self.place('Fixture.torrent',b'fixture')
        original=self.root/'original.torrent'
        shutil.copy2(path,original)
        seen=[]
        def fail(*args):
            seen.append(1)
            raise ValueError('Fixture rejection')
        scan(self.app,self.store,self.stop,{'torrent'},torrent=fail)
        scan(self.app,self.store,self.stop,{'torrent'},torrent=fail)
        self.assertEqual(len(seen),1)
        path.unlink()
        shutil.copy2(original,path)
        self.assertEqual(path.stat().st_mtime_ns,original.stat().st_mtime_ns)
        scan(self.app,self.store,self.stop,{'torrent'},torrent=fail)
        self.assertEqual(len(seen),2)

    def test_youtube_strm_is_queued_instead_of_fetched_as_a_file(self):
        strm = self.place('bbb.strm', 'https://www.youtube.com/watch?v=aqz-KE-bpKQ\n')
        seen = []

        def youtube(_app, _store, video_id, name, _stop):
            seen.append((video_id, name))

        def fetch(_url):
            raise AssertionError('A YouTube strm is not fetched as a plain video file.')

        scan(self.app, self.store, self.stop, {'strm'}, fetch=fetch, youtube=youtube)
        self.assertEqual(seen, [('aqz-KE-bpKQ', 'bbb.strm')])
        self.assertFalse(strm.exists())
        self.assertEqual(youtube_id('https://youtu.be/aqz-KE-bpKQ'), 'aqz-KE-bpKQ')
        self.assertEqual(youtube_id('https://example.com/video.mp4'), '')

    def test_youtube_video_gets_show_artwork_and_nfo(self):
        media = self.base / 'YouTube'
        video = media / 'Blender' / 'Season 2008' / 's2008.e052901 - Big Buck Bunny.mp4'
        video.parent.mkdir(parents=True)
        video.write_bytes(b'video')
        saved = []

        def save_image(_store, url, target):
            saved.append((url, target.name))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'jpeg')
            return True

        note = write_youtube_sidecars(self.store, media, video, 'abcdefghijk', {
            'title': 'Big Buck Bunny', 'channel': 'Blender', 'upload_date': '20080529',
            'description': 'A short film', 'thumbnail': 'https://i.ytimg.com/thumb.jpg', 'channel_id': 'UCtestchannelid00000000',
        }, channel={'avatar': 'https://yt3.ggpht.com/avatar', 'banner': 'https://yt3.ggpht.com/banner', 'description': 'Films'}, save_image=save_image)
        self.assertEqual(note, '')
        show = ET.parse(video.parent.parent / 'tvshow.nfo').getroot()
        self.assertEqual(show.tag, 'tvshow')
        self.assertEqual(show.findtext('title'), 'Blender')
        episode = ET.parse(video.with_suffix('.nfo')).getroot()
        self.assertEqual(episode.tag, 'episodedetails')
        self.assertEqual(episode.findtext('episode'), '052901')
        self.assertEqual({name for _url, name in saved}, {'s2008.e052901 - Big Buck Bunny-thumb.jpg', 'poster.jpg', 'fanart.jpg'})
        self.assertTrue((video.parent.parent / 'poster.jpg').is_file())
        self.assertTrue((video.parent.parent / 'fanart.jpg').is_file())

    def test_youtube_hover_uses_fanart_then_the_video_picture(self):
        def fetch(url):
            self.assertTrue(url.startswith('https://yt3.ggpht.com/'))
            return b'\xff\xd8\xfffanart'

        card = youtube_card(self.store, {
            'title': 'Big Buck Bunny', 'channel': 'Blender', 'description': 'A short film',
            'thumbnail': 'https://i.ytimg.com/vi/example/hqdefault.jpg',
        }, {'banner': 'https://yt3.ggpht.com/banner'}, fetch)
        self.assertEqual(card['title'], 'Big Buck Bunny')
        self.assertEqual(card['overview'], 'A short film')
        self.assertRegex(card['image'], r'^/artwork/[a-f0-9]{64}$')
        self.assertNotIn('ytimg', json.dumps(card))
        self.assertNotIn('ggpht', json.dumps(card))
        saved = self.store.directory / 'artwork' / card['image'].rsplit('/', 1)[-1]
        self.assertEqual(saved.read_bytes(), b'\xff\xd8\xfffanart')

    def test_suggestion_uses_the_matched_show_name(self):
        (self.base / 'TV Shows' / 'Cupertino (2026)').mkdir(parents=True)
        path = destination(self.store, 'Cupertino.S01E01.mkv.strm')
        self.assertEqual(path.parent.parent.name, 'Cupertino (2026)')
        self.assertEqual(path.parent.name, 'Season 1')


if __name__ == '__main__':
    unittest.main()

class WatchedRoutingSettingsTests(unittest.TestCase):
    setUp = AutoDownloadTests.setUp
    tearDown = AutoDownloadTests.tearDown
    place = AutoDownloadTests.place

    def test_disabled_type_is_left_unclaimed_and_resumes_when_enabled(self):
        from backend import settings
        path=self.place('Movie.torrent',b'fixture')
        seen=[]
        settings.save(self.store,'auto-download',{'torrent':False})
        scan(self.app,self.store,self.stop,{'torrent'},torrent=lambda *args:seen.append(args))
        self.assertTrue(path.exists())
        self.assertEqual(seen,[])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auto_downloads').fetchone()[0],0)
        settings.save(self.store,'auto-download',{'torrent':True})
        scan(self.app,self.store,self.stop,{'torrent'},torrent=lambda *args:seen.append(args))
        self.assertEqual(len(seen),1)
        self.assertFalse(path.exists())

    def test_disabling_intake_during_batch_leaves_remaining_inputs(self):
        from backend import settings
        self.place('One.torrent',b'one')
        self.place('Two.torrent',b'two')
        seen=[]
        def accept(*args):
            seen.append(args)
            settings.save(self.store,'auto-download',{'enabled':False})
        scan(self.app,self.store,self.stop,{'torrent'},torrent=accept)
        self.assertEqual(len(seen),1)
        self.assertEqual(len(list(self.watch.glob('*.torrent'))),1)
