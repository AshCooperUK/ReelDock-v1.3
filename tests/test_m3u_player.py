import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend import m3u, m3u_player
from backend.app import create_app

ORIGIN = 'https://dev.example.com'


class PlayerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)/'library'
        self.app = create_app({'TESTING':True, 'DATA_DIR':self.temp.name, 'M3U_DIR':str(self.root)})
        self.store = self.app.extensions['store']
        self.store.set('public_origin', ORIGIN); self.store.set('setup_complete', '1')
        with self.store.connect() as db: db.execute("INSERT INTO users VALUES(1,'admin','unused')")
        self.client = self.app.test_client()
        with self.client.session_transaction(base_url=ORIGIN) as session: session['user_id']=1
        self.source = m3u.save(self.store, dict(name='Library',url='https://example.com/private/playlist',existing_library=True,naming='emby'))
        text = '#EXTM3U\n'+''.join(f'#EXTINF:0 tvg-type="movie" tvg-id="tt123" group-title="Movie VOD",Movie {i:03} (2024)\nhttps://example.com/private/secret/{i}.mp4\n' for i in range(65))
        m3u.request_sync(self.store,self.source)
        m3u.tick(self.store,self.root,fetch=lambda url:text)
        with self.store.connect() as db: self.row=dict(db.execute("SELECT * FROM m3u_files WHERE kind='movies' ORDER BY title LIMIT 1").fetchone())

    def tearDown(self): self.temp.cleanup()

    def get(self, endpoint, **query):
        return self.client.get('/api/m3u/'+self.source+'/'+endpoint,query_string=query,base_url=ORIGIN)

    def saved_video(self):
        folder=Path(self.temp.name)/'downloads';folder.mkdir()
        self.app.config['M3U_DOWNLOAD_ROOTS']=json.dumps([str(folder)])
        path=folder/'Movie.mp4';path.write_bytes(b'0123456789')
        with self.store.connect() as db:
            db.execute('INSERT INTO m3u_downloads VALUES(?,?,?,?,?,?,?,?,?,?,?)',('local',self.source,self.row['path'],'Movie','movies',str(path),'complete',100,'Saved',1,1))
        return path

    def test_saved_video_is_an_authenticated_attachment_after_playlist_removal(self):
        self.saved_video()
        with patch.object(m3u_player,'upstream',side_effect=AssertionError('Local video must not contact provider')):
            result=self.get('download-info',path=self.row['path']).json
            self.assertEqual(set(result),{'csrf','local','download','downloadAvailable','downloadReason'})
            self.assertTrue(result['local']);self.assertTrue(result['downloadAvailable']);self.assertEqual(result['downloadReason'],'')
            self.assertEqual(self.app.test_client().get(result['download'],base_url=ORIGIN).status_code,401)
            response=self.client.get(result['download'],base_url=ORIGIN,headers={'Range':'bytes=2-5'})
            self.assertEqual(response.status_code,206);self.assertEqual(response.data,b'2345')
            self.assertEqual(response.mimetype,'application/octet-stream')
            self.assertTrue(response.headers['Content-Disposition'].startswith('attachment;'));self.assertIn('Movie.mp4',response.headers['Content-Disposition']);response.close()
            full=self.client.get(result['download'],base_url=ORIGIN);self.assertEqual(full.status_code,200);self.assertEqual(full.data,b'0123456789');full.close()
            with self.store.connect() as db:db.execute('DELETE FROM m3u_files WHERE source=?',(self.source,))
            self.assertTrue(self.get('download-info',path=self.row['path']).json['local'])

    def test_missing_or_outside_saved_file_never_served(self):
        path=self.saved_video();path.unlink()
        self.assertFalse(self.get('download-info',path=self.row['path']).json['local'])
        outside=Path(self.temp.name)/'outside.mp4';outside.write_bytes(b'private')
        with self.store.connect() as db:db.execute('UPDATE m3u_downloads SET destination=?',(str(outside),))
        self.assertFalse(self.get('download-info',path=self.row['path']).json['local'])
        with patch.object(m3u_player,'upstream',side_effect=OSError('offline')):
            response=self.get('download',path=self.row['path'])
        self.assertNotEqual(response.data,b'private');response.close()

    def test_removed_playback_routes_are_gone(self):
        rules={rule.rule for rule in self.app.url_map.iter_rules()}
        for forbidden in ('/api/m3u-stream/<token>','/api/m3u-local/<identity>','/api/m3u/<source>/playback','/api/m3u/<source>/convert','/api/m3u-converted/<token>/<name>','/api/saved-playback','/api/playback/<video_id>','/media/video/<video_id>','/api/media-management/file'):
            self.assertNotIn(forbidden,rules)
        for method,url in (('get','/api/m3u-stream/token'),('get','/api/m3u-local/local'),('get','/api/m3u/'+self.source+'/playback'),('post','/api/m3u/'+self.source+'/convert'),('get','/api/m3u-converted/t/index.m3u8')):
            response=getattr(self.client,method)(url,base_url=ORIGIN,headers={'Origin':ORIGIN})
            self.assertEqual(response.status_code,404,url)
        with patch('subprocess.Popen') as spawn:
            self.client.post('/api/m3u/'+self.source+'/convert',base_url=ORIGIN,json={'start':0},headers={'Origin':ORIGIN})
            spawn.assert_not_called()

    def test_download_info_is_exact_opaque_and_authenticated(self):
        with patch.object(m3u_player,'upstream',side_effect=AssertionError('No provider contact')):
            result=self.get('download-info',path=self.row['path'])
        self.assertEqual(result.status_code,200)
        self.assertEqual(set(result.json),{'csrf','local','download','downloadAvailable','downloadReason'})
        self.assertFalse(result.json['local']);self.assertTrue(result.json['downloadAvailable'])
        self.assertTrue(result.json['download'].startswith('/api/m3u/'+self.source+'/download?'))
        for leaked in (b'secret',b'example.com',b'convert',b'token',b'.mp4'):self.assertNotIn(leaked,result.data)
        self.assertEqual(self.app.test_client().get('/api/m3u/'+self.source+'/download-info',query_string={'path':self.row['path']},base_url=ORIGIN).status_code,401)
        self.assertEqual(self.get('download-info',path='../outside.strm').status_code,404)

    def test_movies_paging_and_search(self):
        first=self.get('entries',kind='movies').json
        second=self.get('entries',kind='movies',offset=50).json
        self.assertEqual(len(first['items']),50); self.assertTrue(first['more'])
        self.assertEqual(len(second['items']),15); self.assertFalse(second['more'])
        self.assertFalse(set(x['path'] for x in first['items']) & set(x['path'] for x in second['items']))
        self.assertEqual(len(self.get('entries',kind='movies',q='064').json['items']),1)

    def test_sorting_applies_before_pagination_and_filters(self):
        first=self.get('entries',sort='title-desc',kind='movies').json
        second=self.get('entries',sort='title-desc',kind='movies',offset=50).json
        self.assertEqual(first['items'][0]['title'],'Movie 064 (2024)')
        self.assertEqual(second['items'][-1]['title'],'Movie 000 (2024)')
        self.assertEqual(len({r['path'] for r in first['items']+second['items']}),65)
        for sort in ('file','file-desc','group','type','year','year-desc'):
            self.assertEqual(self.get('entries',sort=sort,q='064').json['items'][0]['title'],'Movie 064 (2024)')
        self.assertEqual(self.get('entries',sort='title; DELETE FROM m3u_files').status_code,400)
        with self.store.connect() as db:
            db.execute("UPDATE m3u_file_info SET data=? WHERE path=?",(json.dumps({'info':{'year':1980}}),self.row['path']))
        self.assertEqual(self.get('entries',sort='year').json['items'][0]['path'],self.row['path'])
        self.assertNotEqual(self.get('entries',sort='year-desc').json['items'][0]['path'],self.row['path'])

    def test_cast_cards_and_biography_are_lazy_and_authenticated(self):
        self.store.set('m3u_tmdb_token','test-token')
        def fake_get(client,path,**params):
            if path=='find/tt123': return {'movie_results':[{'id':42}]}
            if path=='movie/42': return {'id':42,'credits':{'cast':[{'id':10,'name':'Actor','character':'Hero','profile_path':'/actor.jpg'}],'crew':[{'id':10,'name':'Actor','job':'Director'}]}}
            if path=='person/10': return {'name':'Actor','biography':'A biography.','birthday':'1970-01-01','profile_path':'/actor.jpg','place_of_birth':'London'}
            self.fail(path)
        with patch.object(m3u_player.TMDB,'get',autospec=True,side_effect=fake_get) as get:
            result=self.get('details',path=self.row['path']).json
            self.assertEqual(result['people'],[{'id':10,'name':'Actor','role':'Hero · Director','image':'/api/m3u-artwork/actor.jpg?size=w185'}])
            self.assertNotIn('Cast',result['facts'])
            self.assertFalse(any(c.args[1].startswith('person/') for c in get.call_args_list))
            response=self.client.get('/api/m3u-person/10',base_url=ORIGIN)
            self.assertEqual(response.json['biography'],'A biography.')
            self.assertEqual(response.json['facts']['Place of birth'],'London')
        self.assertEqual(self.app.test_client().get('/api/m3u-person/10',base_url=ORIGIN).status_code,401)
        self.assertEqual(self.client.get('/api/m3u-person/0',base_url=ORIGIN).status_code,404)
        self.assertEqual(m3u_player.portrait('/../unsafe.jpg'),'')

    def test_local_metadata_and_persisted_ids(self):
        path=self.root/self.row['path']
        path.with_suffix('.nfo').write_text('<movie><plot>Local plot</plot><runtime>92</runtime><genre>Drama</genre></movie>',encoding='utf-8')
        result=self.get('details',path=self.row['path']).json
        self.assertEqual(result['description'],'Local plot')
        self.assertEqual(result['facts']['Runtime (minutes)'],'92')
        with self.store.connect() as db:
            row=db.execute('SELECT data FROM m3u_file_info WHERE path=?',(self.row['path'],)).fetchone()
        self.assertEqual(json.loads(row['data'])['attrs']['tvg-id'],'tt123')

    def test_tmdb_id_details_artwork_and_credits(self):
        self.store.set('m3u_tmdb_token','test-token')
        def fake_get(client,path,**params):
            if path=='find/tt123': return {'movie_results':[{'id':42}]}
            if path=='movie/42': return {'id':42,'overview':'Matched plot','backdrop_path':'/art.jpg','runtime':95,'credits':{'cast':[{'name':'Actor'}]}}
            self.fail(path)
        with patch.object(m3u_player.TMDB,'get',fake_get):
            result=self.get('details',path=self.row['path']).json
        self.assertEqual(result['description'],'Matched plot')
        self.assertEqual(result['artwork'],'/api/m3u-artwork/art.jpg')
        self.assertEqual(result['facts']['Cast'],'Actor')

    def test_browser_download_streams_video_with_filename_and_range_support(self):
        download = self.get('download-info',path=self.row['path']).json['download']
        self.assertIn('/download?', download)
        connection=Mock(); response=Mock(status=206)
        response.getheader.side_effect=lambda key,default=None: {'Content-Type':'video/x-matroska','Content-Length':'3','Content-Range':'bytes 1-3/9','Accept-Ranges':'bytes'}.get(key,default)
        response.read=io.BytesIO(b'abc').read
        with patch.object(m3u_player,'upstream',return_value=(connection,response,'https://example.com/private/episode.mkv')) as fetch:
            result=self.client.get(download,base_url=ORIGIN,headers={'Range':'bytes=1-3'})
            self.assertEqual(result.status_code,206)
            self.assertEqual(result.data,b'abc')
            self.assertEqual(result.headers['Content-Range'],'bytes 1-3/9')
            self.assertIn('attachment;',result.headers['Content-Disposition'])
            self.assertIn('.mkv',result.headers['Content-Disposition'])
            self.assertNotIn('.strm',result.headers['Content-Disposition'])
            self.assertNotIn('private',str(result.headers))
            fetch.assert_called_once_with('https://example.com/private/secret/0.mp4','bytes=1-3')
            result.close();connection.close.assert_called()
        with patch.object(m3u_player,'upstream') as fetch:
            anonymous=self.app.test_client().get(download,base_url=ORIGIN)
            self.assertNotEqual(anonymous.status_code,200);fetch.assert_not_called()

    def test_opaque_download_detects_mp4_without_losing_header_bytes(self):
        path=self.root/self.row['path'];path.write_text('https://example.com/opaque',encoding='utf-8')
        raw=b'\x00\x00\x00\x20ftypisom'+b'x'*5000
        connection=Mock();response=Mock(status=200)
        response.getheader.side_effect=lambda key,default=None: {'Content-Type':'application/octet-stream','Content-Length':str(len(raw))}.get(key,default)
        response.read=io.BytesIO(raw).read
        with patch.object(m3u_player,'upstream',return_value=(connection,response,'https://example.com/opaque')):
            result=self.get('download',path=self.row['path'])
            self.assertEqual(result.data,raw)
            self.assertIn('.mp4',result.headers['Content-Disposition'])
            self.assertNotIn('.bin',result.headers['Content-Disposition'])
            self.assertEqual(int(result.headers['Content-Length']),len(raw))
            result.close();connection.close.assert_called()

    def test_resumed_opaque_download_keeps_the_same_container_filename(self):
        path=self.root/self.row['path'];path.write_text('https://example.com/opaque',encoding='utf-8')
        connection=Mock();response=Mock(status=206)
        response.getheader.side_effect=lambda key,default=None: {'Content-Type':'application/octet-stream','Content-Length':'3','Content-Range':'bytes 100-102/200'}.get(key,default)
        response.read=io.BytesIO(b'abc').read
        probe_connection=Mock();probe=Mock(status=206);probe.read.return_value=b'\x00\x00\x00\x20ftypisom'
        with patch.object(m3u_player,'upstream',side_effect=[(connection,response,'https://example.com/opaque'),(probe_connection,probe,'https://example.com/opaque')]) as fetch:
            result=self.client.get('/api/m3u/'+self.source+'/download',query_string={'path':self.row['path']},base_url=ORIGIN,headers={'Range':'bytes=100-102'})
            self.assertEqual(result.status_code,206);self.assertEqual(result.data,b'abc')
            self.assertIn('.mp4',result.headers['Content-Disposition'])
            self.assertEqual(result.headers['Content-Range'],'bytes 100-102/200')
            self.assertEqual(fetch.call_args_list[1].args[1],'bytes=0-4095')
            probe.read.assert_called_once_with(4096);probe_connection.close.assert_called()
            result.close();connection.close.assert_called()

    def test_provider_mime_names_an_opaque_range_download_without_a_second_request(self):
        path=self.root/self.row['path'];path.write_text('https://example.com/opaque',encoding='utf-8')
        connection=Mock();response=Mock(status=206)
        response.getheader.side_effect=lambda key,default=None: {'Content-Type':'video/quicktime','Content-Length':'3','Content-Range':'bytes 100-102/200'}.get(key,default)
        response.read=io.BytesIO(b'abc').read
        with patch.object(m3u_player,'upstream',return_value=(connection,response,'https://example.com/opaque')) as fetch:
            result=self.client.get('/api/m3u/'+self.source+'/download',query_string={'path':self.row['path']},base_url=ORIGIN,headers={'Range':'bytes=100-102'})
            self.assertEqual(result.status_code,206);self.assertEqual(result.data,b'abc')
            self.assertIn('.mov',result.headers['Content-Disposition']);self.assertNotIn('.bin',result.headers['Content-Disposition'])
            self.assertTrue(result.headers['Content-Disposition'].startswith('attachment;'))
            self.assertEqual(result.mimetype,'application/octet-stream','the browser still receives an opaque attachment')
            self.assertEqual(fetch.call_count,1,'the provider MIME avoids a signature probe')
            result.close();connection.close.assert_called()

    def test_container_signatures(self):
        for prefix,suffix in [(b'\x1a\x45\xdf\xa3matroska','.mkv'),(b'\x1a\x45\xdf\xa3webm','.webm'),(b'RIFF0000AVI ','.avi'),(b'OggS','.ogv')]:
            self.assertEqual(m3u_player.video_suffix('https://example.com/opaque','application/octet-stream',prefix),suffix)

    def test_browser_download_rejects_manifest_instead_of_saving_it_as_video(self):
        connection=Mock();response=Mock(status=200)
        response.getheader.side_effect=lambda key,default=None: 'application/vnd.apple.mpegurl' if key=='Content-Type' else default
        with patch.object(m3u_player,'upstream',return_value=(connection,response,'https://example.com/master.m3u8')):
            result=self.get('download',path=self.row['path'])
            self.assertEqual(result.status_code,409)
            self.assertNotIn('Content-Disposition',result.headers)
            response.read.assert_not_called();connection.close.assert_called()
        path=self.root/self.row['path'];path.write_text('https://example.com/master.m3u8',encoding='utf-8')
        info=self.get('download-info',path=self.row['path']).json
        self.assertEqual((info['download'],info['downloadAvailable']),('',False));self.assertTrue(info['downloadReason'])
        self.assertEqual(self.get('download',path='../outside.strm').status_code,404)

    def test_private_network_and_entities_rejected(self):
        with patch.object(m3u_player,'public_addresses',side_effect=ValueError('private')), patch.object(m3u_player.socket,'create_connection') as connect:
            with self.assertRaises(ValueError): m3u_player.upstream('http://127.0.0.1/secret')
            connect.assert_not_called()
        path=self.root/self.row['path']
        path.with_suffix('.nfo').write_text('<!DOCTYPE x [<!ENTITY x "bad">]><movie><plot>&x;</plot></movie>')
        self.assertEqual(m3u_player.nfo_data(self.root,path),{})

    def test_youtube_links_disable_downloads_only_for_real_youtube_ids(self):
        path=self.root/self.row['path']
        path.write_text('https://www.youtube.com/watch?v=abcdefghijk',encoding='utf-8')
        info=self.get('download-info',path=self.row['path']).json
        self.assertEqual((info['downloadAvailable'],info['download']),(False,''));self.assertTrue(info['downloadReason'])
        path.write_text('https://youtube.com.example.com/watch?v=abcdefghijk',encoding='utf-8')
        self.assertTrue(self.get('download-info',path=self.row['path']).json['downloadAvailable'])

    def test_playlist_artwork_is_private_bounded_and_cached(self):
        with self.store.connect() as db:
            db.execute('UPDATE m3u_file_info SET data=? WHERE source=? AND path=?',
                       (json.dumps({'attrs':{'tvg-logo':'https://example.com/secret/poster.jpg'}}),self.source,self.row['path']))
        result=self.get('details',path=self.row['path'])
        self.assertNotIn(b'secret',result.data)
        art=result.json['artwork']
        connection=Mock(); upstream=Mock(status=200)
        upstream.read=io.BytesIO(b'\xff\xd8sample-image').read
        with patch.object(m3u_player,'upstream',return_value=(connection,upstream,'https://example.com/secret/poster.jpg')) as fetch:
            first=self.client.get(art,base_url=ORIGIN)
            self.assertEqual(first.status_code,200); first.close()
            second=self.client.get(art,base_url=ORIGIN)
            self.assertEqual(second.mimetype,'image/jpeg'); second.close()
            self.assertEqual(fetch.call_count,1)
        connection.close.assert_called()
