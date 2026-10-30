"""Compose-managed layout: real temporary files, fake provider responses only."""
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from backend.app import create_app
from backend import storage_layout as layout, m3u, m3u_downloads, media_library, discover_youtube
from backend import auto_downloads, reset, torrents, nzb, m3u_removal

ORIGIN = 'https://dev.example.com'

class FixedStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'media'
        self.root.mkdir()
        self.app = create_app(dict(TESTING=True, DATA_DIR=str(Path(self.tmp.name)/'data'),
                                  REELDOCK_MEDIA_ROOT=str(self.root), REELDOCK_EMBY_ROOT='/emby/ReelDock'))
        self.store = self.app.extensions['store']
        self.store.set('public_origin', ORIGIN)
        self.store.set('setup_complete', '1')
        with self.store.connect() as db: db.execute("INSERT INTO users VALUES(1,'owner','unused')")
        self.client = self.app.test_client()
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session['user_id'] = 1
            session['csrf'] = 'token'

    def post(self, url, body, csrf=True):
        return self.client.post(url, json=body, base_url=ORIGIN,
                                headers={'Origin':ORIGIN, **({'X-CSRF-Token':'token'} if csrf else {})})

    def source(self, name='Provider'):
        identity = m3u.save(self.store, dict(name=name, url='https://example.com/list.m3u', clean_sync=False))
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM m3u_sources WHERE id=?',(identity,)).fetchone())

    def export(self, source, kind='movies'):
        relative = 'Movie_VOD/Film (2020)/Film (2020).strm' if kind=='movies' else 'TV_VOD/Show/Season 01/Show S01E01.strm'
        m3u.export(self.store, str(self.root), source,
                   [dict(path=relative,title='Film' if kind=='movies' else 'Show',kind=kind,group='',content='https://example.com/video.mp4\n')],0)
        return layout.export_path(self.store, relative, source['folder'])

    def test_mount_overrides_obsolete_saved_paths_and_reset_stays_scoped(self):
        for k,v in [('storage_base','/old'),('storage_names','{"movies":"Wrong"}'),('auto_download_folder','/old/watch'),('m3u_download_folders','{"movies":"/old/movies"}')]: self.store.set(k,v)
        layout.apply(self.app,self.store)
        self.assertEqual(set(p.name for p in self.root.iterdir()), set(layout.FIXED_NAMES.values()))
        self.assertEqual(layout.download_base(self.store),str(self.root))
        self.assertEqual(auto_downloads.watch_folder(self.app,self.store), self.root/'Auto-Download')
        self.assertEqual(m3u_downloads.settings(self.app,self.store)['folders']['movies'],str(self.root/'Movies'))
        self.assertEqual(torrents._library_layout(self.store)['movies'],'Movies')
        self.assertEqual(nzb._library_layout(self.store)['series'],'TV Shows')
        extra=self.root/'unmanaged';extra.mkdir();(extra/'keep').write_text('keep')
        _,targets=reset.targets(self.app,self.store)
        self.assertNotIn(self.root,targets);self.assertNotIn(extra,targets)
        self.assertEqual(len(targets),7)

    def test_directory_mutations_rejected_and_csrf_retained(self):
        for body in ({'base':'/tmp/escape'},{'auto_download_folder':'/tmp/escape'},{'names':{'movies':'Other'}}):
            self.assertEqual(self.post('/api/storage-settings',body).status_code,400)
        self.assertEqual(self.post('/api/storage-settings',{'base':'/tmp/escape'},False).status_code,403)
        self.assertEqual(self.post('/api/m3u-download-settings',{'folders':{'movies':'/tmp/escape'}}).status_code,400)
        self.assertEqual(self.client.get('/api/storage-folders',base_url=ORIGIN).status_code,409)
        self.assertEqual(self.client.get('/api/m3u-folders',base_url=ORIGIN).status_code,409)
        self.assertEqual(self.post('/api/m3u-download-settings',{'share_metadata':False}).status_code,200)
        state=self.client.get('/api/storage-settings',base_url=ORIGIN).json
        self.assertTrue(state['ready']);self.assertEqual(len(state['folders']),7)

    def test_exports_downloads_and_source_removal(self):
        a=self.source();b=self.source('Other')
        for source in (a,b):
            movie=self.export(source)
            self.export(source,'series')
            self.assertTrue((self.root/movie).is_file())
        movie=layout.export_path(self.store,'Movie_VOD/Film (2020)/Film (2020).strm',a['folder'])
        self.assertEqual(movie,'VOD Movies/Provider/Film (2020)/Film (2020).strm')
        row,url=m3u_downloads.lookup(self.app,self.store,a['id'],movie)
        self.assertEqual(url,'https://example.com/video.mp4')
        identity=m3u_downloads.enqueue(self.app,self.store,a['id'],movie)
        with self.store.connect() as db: job=dict(db.execute('SELECT * FROM m3u_downloads WHERE id=?',(identity,)).fetchone())
        self.assertEqual(job['destination'],str(self.root/'Movies/Film (2020)/Film (2020)'))
        content=b'\x00\x00\x00\x20ftypisom'+b'x'*50
        response=Mock(status=200);response.read=io.BytesIO(content).read
        response.getheader=lambda name,default='':{'Content-Length':str(len(content)),'Content-Type':'video/mp4'}.get(name,default)
        with patch('backend.m3u_player.upstream',return_value=(Mock(),response,url)):
            m3u_downloads.transfer(self.app,self.store,job,threading.Event())
        target=self.root/'Movies/Film (2020)/Film (2020).mp4'
        self.assertEqual(target.read_bytes(),content)
        saved=self.client.get('/api/m3u/'+a['id']+'/download',query_string={'path':movie},base_url=ORIGIN)
        self.assertEqual(saved.status_code,200);self.assertEqual(saved.data,content)
        self.assertTrue(saved.headers['Content-Disposition'].startswith('attachment;'));saved.close()
        self.assertEqual(self.client.get('/api/m3u-local/'+identity,base_url=ORIGIN).status_code,404)
        with self.store.connect() as db:m3u_removal.queue(db,a['id'])
        self.assertTrue(m3u_removal.tick(self.store,str(self.root)))
        self.assertFalse((self.root/movie).exists())
        self.assertTrue((self.root/'VOD Movies/Other/Film (2020)/Film (2020).strm').is_file())
        self.assertTrue(target.is_file())

    def test_last_source_cleanup_keeps_fixed_mount_folders(self):
        source=self.source()
        self.export(source)
        with self.store.connect() as db:m3u_removal.queue(db,source['id'])
        m3u_removal.tick(self.store,str(self.root))
        for name in layout.FIXED_NAMES.values():self.assertTrue((self.root/name).is_dir(),name)

    def test_tv_download_and_metadata_destination(self):
        source=self.source();relative=self.export(source,'series')
        identity=m3u_downloads.enqueue(self.app,self.store,source['id'],relative)
        with self.store.connect() as db:job=dict(db.execute('SELECT * FROM m3u_downloads WHERE id=?',(identity,)).fetchone())
        self.assertEqual(job['destination'],str(self.root/'TV Shows/Show/Season 01/Show S01E01'))
        from backend import m3u_emby, emby
        self.assertEqual(emby.library_root(self.store),'/emby/ReelDock')
        self.assertEqual(m3u_emby.settings(self.store)['emby_folders']['movies'],'/emby/ReelDock/Movies')
        from backend.downloaders.torrent.rename import library_path
        for engine in (torrents,nzb):
            movie=library_path('Film (2020).mkv','Film (2020).mkv','video',folders=engine._library_layout(self.store))
            episode=library_path('Show S01E01.mkv','Show S01E01.mkv','video',folders=engine._library_layout(self.store))
            self.assertTrue(movie.startswith('Movies/'),movie)
            self.assertTrue(episode.startswith('TV Shows/'),episode)

    def test_youtube_subscription_and_discover_paths(self):
        channel='UC'+'a'*22;video='a'*11
        with self.store.connect() as db:
            db.execute('INSERT INTO channels(id,data,active) VALUES(?,?,1)',(channel,json.dumps({'id':channel,'name':'Channel'})))
            db.execute('INSERT INTO videos(id,channel,day,published,data,checked) VALUES(?,?,?,?,?,0)',
                       (video,channel,'2026-10-10','2026-10-10',json.dumps({'id':video,'channel':channel,'date':'2026-10-10','title':'Episode'})))
        record=media_library.reserve(self.store,str(self.root),video)
        self.assertTrue(record['path'].startswith('YouTube Subscriptions/Channel/Season 2026/'))
        path=discover_youtube.claim_path(self.store,str(self.root),video,'Individual video')
        self.assertEqual(path,'YouTube Downloads/Channel/Individual video.mkv')
        target=self.root/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'video')
        self.assertEqual(media_library.local_file(self.store,str(self.root),video),target)
        auto=auto_downloads._youtube_target(self.store,self.root,dict(title='Auto video',channel='Channel',upload_date='20261010'))
        self.assertTrue(auto.is_relative_to(self.root/'YouTube Downloads'))

    def test_channel_artwork_retries_public_and_preserves_saved_cookies(self):
        import subprocess
        cookie = self.store.directory / 'youtube-cookies.txt'
        cookie.write_text('saved session')
        calls = []
        def extract(args, **kwargs):
            request = json.loads(kwargs['input'])
            calls.append(request)
            if len(calls) == 1:
                self.assertNotEqual(request['cookie'], str(cookie))
                raise subprocess.CalledProcessError(1, args)
            self.assertNotIn('cookie', request)
            kwargs['stdout'].write(json.dumps({'thumbnails': [
                {'id': 'avatar', 'url': 'https://yt3.googleusercontent.com/avatar', 'width': 900, 'height': 900},
                {'id': 'banner', 'url': 'https://yt3.googleusercontent.com/banner', 'width': 1060, 'height': 175},
            ]}).encode())
        with patch('backend.youtube_fallback.run_extractor', side_effect=extract):
            images = auto_downloads._channel_images(self.store, 'UC' + 'a' * 22)
        self.assertTrue(images['avatar'])
        self.assertTrue(images['banner'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(cookie.read_text(), 'saved session')
        self.assertFalse(Path(calls[0]['cookie']).exists())

    def test_channel_artwork_retry_is_bounded_and_cancellable(self):
        import subprocess
        from backend.metadata_control import Cancelled
        with patch('backend.youtube_fallback.run_extractor', side_effect=subprocess.TimeoutExpired('extract', 90)) as run:
            self.assertEqual(auto_downloads._channel_images(self.store, 'UC' + 'a' * 22), {})
            self.assertEqual(run.call_count, 2)
        with patch('backend.youtube_fallback.run_extractor', side_effect=Cancelled()) as run:
            self.assertEqual(auto_downloads._channel_images(self.store, 'UC' + 'a' * 22), {})
            self.assertEqual(run.call_count, 1)

    def test_discover_channel_artwork_failure_keeps_video_metadata(self):
        import subprocess
        video='c'*11
        channel='UC'+'c'*22
        discover_youtube.register_video(self.store,dict(videoId=video,title='Song',channelId=channel,channel='Artist'))
        info=dict(id=video,title='Song',channel_id=channel,channel='Artist',upload_date='20100101',duration=210,
                  description='Video description',thumbnail='https://i.ytimg.com/vi/'+video+'/hqdefault.jpg')
        with patch('backend.youtube_fallback.run_extractor',side_effect=subprocess.CalledProcessError(1,['extractor'])):
            saved=discover_youtube.enrich_metadata(self.store,video,info)
        self.assertEqual(saved['duration_seconds'],210)
        self.assertEqual(saved['date'],'2010-01-01')
        self.assertEqual(saved['description'],'Video description')
        self.assertTrue(saved['thumbnail'].startswith('/artwork/'))

    def test_discover_download_writes_full_subscription_metadata_and_artwork(self):
        import xml.etree.ElementTree as ET
        from PIL import Image
        video = 'b' * 11
        channel = 'UC' + 'b' * 22
        discover_youtube.register_video(self.store, {'videoId':video, 'title':'Search result', 'channelId':channel, 'channel':'Creator'})
        info = dict(id=video, title='Actual title', channel_id=channel, channel='Creator',
                    upload_date='20240305', duration=3600, description='Full description. ' * 600,
                    thumbnail='https://i.ytimg.com/vi/' + video + '/hqdefault.jpg')
        images = dict(avatar='https://yt3.ggpht.com/avatar', banner='https://yt3.ggpht.com/banner', description='Channel description')
        picture = io.BytesIO()
        Image.new('RGB', (32, 20), 'blue').save(picture, 'JPEG')
        def download(store, job, stop, args, log, root):
            self.assertIn('--write-info-json', args)
            self.assertIn('bv[height=1080]+ba/b[height=1080]', args)
            folder = log.parent
            (folder/'video.mkv').write_bytes(b'mocked completed media')
            (folder/'video.info.json').write_text(json.dumps(info))
            return 0, ''
        with patch('backend.discover_youtube.quality_policy', return_value=False), patch('backend.downloads.check_rules'), \
             patch('backend.downloads.run_process', side_effect=download), patch('backend.auto_downloads._channel_images', return_value=images), \
             patch('backend.artwork.fetch_image', return_value=picture.getvalue()):
            notice = discover_youtube.run(self.store, str(self.root), dict(id=video, title='Search result', quality='1080'), threading.Event())
        self.assertEqual(notice, '')
        target = media_library.local_file(self.store, self.root, video)
        self.assertEqual(target.relative_to(self.root).as_posix(), 'YouTube Downloads/Creator/Actual title.mkv')
        episode = ET.parse(target.with_name('Actual title.nfo')).getroot()
        self.assertEqual(episode.tag, 'episodedetails')
        self.assertEqual(episode.findtext('plot'), info['description'])
        self.assertEqual(episode.findtext('aired'), '2024-03-05')
        self.assertEqual(episode.findtext('runtime'), '60')
        self.assertEqual(episode.findtext('uniqueid'), video)
        show = target.parent
        self.assertEqual(ET.parse(show/'tvshow.nfo').findtext('uniqueid'), channel)
        for file in [show/'poster.jpg', show/'fanart.jpg', target.with_name('Actual title-thumb.jpg')]:
            with Image.open(file) as image: self.assertEqual(image.format, 'JPEG')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT active FROM channels WHERE id=?',(channel,)).fetchone()[0], 0)
        self.assertFalse(any(show.glob('Season *')))
        self.assertTrue(target.with_suffix('.nfo').exists())
        self.assertTrue(media_library.organise(self.store, self.root, video))
        self.assertFalse(any(self.root.glob('.reeldock-discover-*')))
        self.assertFalse(any(self.root.rglob('*.info.json')))
        from backend.channel_downloads import perform
        perform(self.store, self.root, channel, 'delete_media')
        self.assertFalse(target.exists())
        self.assertFalse((show/'Actual title.nfo').exists())
        self.assertFalse((show/'Actual title-thumb.jpg').exists())
        self.assertTrue((show/'poster.jpg').exists())

    def test_symlinks_cannot_redirect_fixed_folders(self):
        (self.root/'Auto-Download').rmdir()
        (self.root/'Auto-Download').symlink_to(self.store.directory,target_is_directory=True)
        with self.assertRaises(ValueError):auto_downloads.watch_folder(self.app,self.store)
        with self.assertRaises(ValueError):layout.configure(self.app,self.store)
        with self.assertRaises(ValueError):reset.targets(self.app,self.store)

if __name__=='__main__':unittest.main()
