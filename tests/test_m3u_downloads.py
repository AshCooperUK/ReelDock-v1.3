import io
import json
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock, patch
from backend.app import create_app
from backend import m3u, m3u_downloads as downloads, m3u_sidecars as sidecars
ORIGIN='https://dev.example.com'
class ServerDownloadTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
  self.movies=self.base/'Movies';self.shows=self.base/'TV Shows'
  self.movies.mkdir();self.shows.mkdir()
  self.app=create_app({'TESTING':True,'DATA_DIR':str(self.base/'data'),'M3U_DIR':str(self.base/'links'),'MEDIA_DIR':str(self.base/'youtube'),'M3U_DOWNLOAD_ROOTS':json.dumps([str(self.movies),str(self.shows)])})
  self.store=self.app.extensions['store'];self.store.set('public_origin',ORIGIN);self.store.set('setup_complete','1')
  with self.store.connect() as db:db.execute("INSERT INTO users VALUES(1,'admin','unused')")
  self.client=self.app.test_client()
  with self.client.session_transaction(base_url=ORIGIN) as session:session['user_id']=1;session['csrf']='token'
  self.source=m3u.save(self.store,dict(name='Test',url='https://example.com/list',naming='emby'))
  text='#EXTM3U\n#EXTINF:0 tvg-type="movie",Movie (2024)\nhttps://example.com/opaque\n#EXTINF:0 group-title="TV VOD",Show S01E01\nhttps://example.com/episode.mp4\n'
  m3u.request_sync(self.store,self.source);m3u.tick(self.store,self.app.config['M3U_DIR'],fetch=lambda url:text)
  with self.store.connect() as db:self.entries=[dict(r) for r in db.execute("SELECT * FROM m3u_files WHERE kind!='playlist' ORDER BY kind")]
 def tearDown(self):self.temp.cleanup()
 def enqueue(self):
  movie=next(r for r in self.entries if r['kind']=='movies')
  identity=downloads.enqueue(self.app,self.store,self.source,movie['path'])
  with self.store.connect() as db:return dict(db.execute('SELECT * FROM m3u_downloads WHERE id=?',(identity,)).fetchone())
 def response(self,body,total=None):
  response=Mock();response.status=200;response.read=io.BytesIO(body).read
  response.getheader.side_effect=lambda key,default='':{'Content-Type':'application/octet-stream','Content-Length':str(len(body) if total is None else total)}.get(key,default)
  return Mock(),response,'https://example.com/opaque'
 def test_roots_are_separate_and_strm_stays_unchanged(self):
  for row in self.entries:downloads.enqueue(self.app,self.store,self.source,row['path'])
  with self.store.connect() as db:rows=db.execute('SELECT * FROM m3u_downloads').fetchall()
  self.assertEqual(len(rows),2)
  for row in rows:self.assertTrue(Path(row['destination']).is_relative_to(self.movies if row['kind']=='movies' else self.shows))
  self.assertEqual(len(list((self.base/'links').rglob('*.strm'))),2)
 def test_duplicate_enqueue_is_idempotent(self):
  first=self.enqueue();second=self.enqueue();self.assertEqual(first['id'],second['id'])
 def test_paths_cannot_escape_or_enter_strm_tree(self):
  for path in [self.base,self.base/'links',self.movies/'..'/'links']:
   with self.assertRaises(ValueError):downloads.safe_folder(self.app,str(path))
  with self.assertRaises(ValueError):downloads.safe_folder(self.app,str(self.movies/'missing'))
 def test_auth_csrf_folder_and_conflicting_categories(self):
  r=self.client.post('/api/m3u-server-downloads',json={'action':'download'},base_url=ORIGIN);self.assertEqual(r.status_code,403)
  r=self.client.post('/api/m3u-download-settings',json={'folders':{'movies':str(self.movies),'series':str(self.movies)}},headers={'X-CSRF-Token':'token','Origin':ORIGIN},base_url=ORIGIN);self.assertEqual(r.status_code,400)
  r=self.client.post('/api/m3u-folders',query_string={'path':str(self.movies)},json={'name':'../escape'},headers={'X-CSRF-Token':'token','Origin':ORIGIN},base_url=ORIGIN);self.assertEqual(r.status_code,400)
  r=self.client.post('/api/m3u-folders',query_string={'path':str(self.movies)},json={'name':'New folder'},headers={'X-CSRF-Token':'token','Origin':ORIGIN},base_url=ORIGIN);self.assertEqual(r.status_code,200);self.assertTrue((self.movies/'New folder').is_dir())
  self.assertNotIn('https://example.com',self.client.get('/api/m3u-server-downloads',base_url=ORIGIN).text)
  self.assertNotEqual(self.app.test_client().get('/api/m3u-folders',query_string={'path':str(self.movies)},base_url=ORIGIN).status_code,200)
 def test_emby_option_requires_csrf_and_downloads_include_artwork(self):
  paths=dict(streams='/media/ReelDock/M3U',movies='/media/ReelDock/Movies',series='/media/ReelDock/TV Shows')
  body=dict(auto_libraries=True,emby_folders=paths)
  self.assertEqual(self.client.post('/api/m3u-download-settings',json=body,base_url=ORIGIN).status_code,403)
  result=self.client.post('/api/m3u-download-settings',json=body,headers={'X-CSRF-Token':'token','Origin':ORIGIN},base_url=ORIGIN)
  self.assertEqual(result.status_code,200);self.assertTrue(result.json['auto_libraries']);self.assertEqual(result.json['emby_folders'],paths)
  self.enqueue();result=self.client.get('/api/m3u-server-downloads',base_url=ORIGIN)
  self.assertEqual(result.json['items'][0]['subscription'],'Test');self.assertTrue(result.json['items'][0]['artwork'].startswith('/api/m3u/'))
  self.assertNotIn('https://example.com',result.text)
 def test_complete_opaque_mp4_is_exact_and_published_atomically(self):
  job=self.enqueue();body=b'\x00\x00\x00\x20ftypisom'+b'x'*6000
  with patch('backend.m3u_player.upstream',return_value=self.response(body)):
   downloads.transfer(self.app,self.store,job,threading.Event())
  with self.store.connect() as db:result=db.execute('SELECT * FROM m3u_downloads').fetchone()
  self.assertEqual(result['state'],'complete');self.assertEqual(Path(result['destination']).suffix,'.mp4');self.assertEqual(Path(result['destination']).read_bytes(),body)
  self.assertFalse(list(self.movies.rglob('*.part')))
 def test_truncated_transfer_is_not_published(self):
  job=self.enqueue();body=b'\x00\x00\x00\x20ftypisom'+b'x'*10
  with patch('backend.m3u_player.upstream',return_value=self.response(body,9000)):
   downloads.transfer(self.app,self.store,job,threading.Event())
  with self.store.connect() as db:self.assertEqual(db.execute('SELECT state FROM m3u_downloads').fetchone()[0],'failed')
  self.assertFalse(list(self.movies.rglob('*.mp4')));self.assertFalse(list(self.movies.rglob('*.part')))
 def test_cancel_prevents_completed_file(self):
  job=self.enqueue()
  with self.store.connect() as db:db.execute("UPDATE m3u_downloads SET state='cancelling'")
  with patch('backend.m3u_player.upstream',return_value=self.response(b'\x00\x00\x00\x20ftypisom'+b'x'*10)):
   downloads.transfer(self.app,self.store,job,threading.Event())
  with self.store.connect() as db:self.assertEqual(db.execute('SELECT state FROM m3u_downloads').fetchone()[0],'cancelled')
  self.assertFalse(list(self.movies.rglob('*.mp4')))
 def test_existing_destination_kept(self):
  job=self.enqueue();target=Path(job['destination']+'.mp4');target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'custom')
  with patch('backend.m3u_player.upstream',return_value=self.response(b'\x00\x00\x00\x20ftypisom')):downloads.transfer(self.app,self.store,job,threading.Event())
  self.assertEqual(target.read_bytes(),b'custom')
 def test_sidecar_ownership_and_xml_escaping(self):
  path=self.movies/'Movie.nfo';raw=sidecars.xml('movie',{'title':'A & B','overview':'<description>','id':42})
  self.assertTrue(sidecars.owned_write(self.store,self.movies,path,raw));self.assertEqual(ET.fromstring(path.read_bytes()).findtext('plot'),'<description>')
  self.assertFalse(sidecars.owned_write(self.store,self.movies,path,raw))
  path.write_text('custom');self.assertFalse(sidecars.owned_write(self.store,self.movies,path,b'new'));self.assertEqual(path.read_text(),'custom')
 def test_episode_export_uses_episode_id_and_preserves_show_metadata(self):
  target=self.shows/'Show'/'Season 01'/'Show S01E01.strm';target.parent.mkdir(parents=True);target.write_text('https://example.com')
  row={'metadata':json.dumps({'tmdb_id':42}),'info':json.dumps({'season':1,'episode':1}),'kind':'series'}
  def get(_,path):return {'id':42,'name':'Show','overview':'Show description'} if path=='tv/42' else {'episodes':[{'id':99,'name':'Pilot','episode_number':1,'overview':'Episode plot','air_date':'2024-01-01'}]}
  with patch('backend.m3u_sidecars.TMDB.get',autospec=True,side_effect=get):self.assertTrue(sidecars.export_entry(self.store,row,self.shows,target))
  episode=ET.fromstring(target.with_suffix('.nfo').read_bytes());self.assertEqual(episode.findtext('uniqueid'),'99');self.assertEqual(episode.findtext('plot'),'Episode plot');self.assertEqual(episode.findtext('season'),'1')
  self.assertTrue((self.shows/'Show'/'tvshow.nfo').exists())
 def test_backfill_exports_existing_catalogue_and_does_not_repeat(self):
  from backend import m3u_catalogue
  m3u_catalogue.index(self.store);self.store.set('m3u_tmdb_token','test-token')
  row=next(r for r in self.entries if r['kind']=='movies')
  with self.store.connect() as db:
   db.execute("UPDATE m3u_catalog_titles SET data=?,checked=1 WHERE key=(SELECT key FROM m3u_catalog_entries WHERE source=? AND path=?)",(json.dumps({'tmdb_id':42}),self.source,row['path']))
  with patch('backend.m3u_sidecars.TMDB.get',return_value={'id':42,'title':'Movie','overview':'Shared plot'}) as get:
   self.assertEqual(sidecars.batch(self.app,self.store),1);self.assertEqual(sidecars.batch(self.app,self.store),0);self.assertEqual(get.call_count,1)
  self.assertTrue(list((self.base/'links').rglob('*.nfo')))
 def test_playlist_cleanup_removes_owned_movie_art_and_keeps_full_download(self):
  row=next(r for r in self.entries if r['kind']=='movies')
  with self.store.connect() as db:folder=db.execute('SELECT folder FROM m3u_sources WHERE id=?',(self.source,)).fetchone()[0]
  base=self.base/'links'/folder;target=base/row['path']
  for file in [target.with_suffix('.nfo'),target.with_name(target.stem+'-poster.jpg'),target.with_name(target.stem+'-fanart.png')]:sidecars.owned_write(self.store,base,file,b'owned')
  saved=self.movies/'Movie.mp4';saved.write_bytes(b'full-video')
  text='#EXTM3U\n#EXTINF:0 group-title="TV VOD",Show S01E01\nhttps://example.com/episode.mp4\n'
  m3u.request_sync(self.store,self.source);m3u.tick(self.store,self.app.config['M3U_DIR'],fetch=lambda url:text)
  self.assertFalse(target.parent.exists());self.assertEqual(saved.read_bytes(),b'full-video')
  with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM m3u_sidecars').fetchone()[0],0)
 def test_cleanup_keeps_shared_show_art_until_last_episode_is_removed(self):
  base=self.base/'links';show=base/'TV_VOD'/'Show';season=show/'Season 01';season.mkdir(parents=True)
  first=season/'Show S01E01.strm';second=season/'Show S01E02.strm'
  for target in (first,second):
   target.write_text('stream');sidecars.owned_write(self.store,base,target.with_suffix('.nfo'),b'episode');sidecars.owned_write(self.store,base,target.with_name(target.stem+'-thumb.jpg'),b'image')
  for name in ('tvshow.nfo','poster.jpg','fanart.png'):sidecars.owned_write(self.store,base,show/name,b'shared')
  first.unlink();sidecars.cleanup_removed(self.store,base,[first.relative_to(base)])
  self.assertTrue((show/'poster.jpg').exists());self.assertTrue(second.with_suffix('.nfo').exists());self.assertFalse(first.with_suffix('.nfo').exists())
  second.unlink();sidecars.cleanup_removed(self.store,base,[second.relative_to(base)])
  self.assertFalse(show.exists())
 def test_cleanup_preserves_modified_and_unowned_sidecars(self):
  base=self.base/'links';target=base/'Movie'/'Movie.strm';target.parent.mkdir(parents=True)
  nfo=target.with_suffix('.nfo');sidecars.owned_write(self.store,base,nfo,b'original');nfo.write_bytes(b'custom')
  art=target.with_name(target.stem+'-poster.jpg');art.write_bytes(b'unowned')
  sidecars.cleanup_removed(self.store,base,[target.relative_to(base)])
  self.assertEqual(nfo.read_bytes(),b'custom');self.assertEqual(art.read_bytes(),b'unowned')
 def test_symlink_cannot_escape_download_or_export_roots(self):
  outside=self.base/'outside';outside.mkdir();link=self.movies/'link'
  try:link.symlink_to(outside,target_is_directory=True)
  except OSError:self.skipTest('Symlinks unavailable')
  with self.assertRaises(ValueError):downloads.safe_folder(self.app,str(link))
  with self.assertRaises(ValueError):sidecars.owned_write(self.store,self.movies,link/'file.nfo',b'unsafe')
  self.assertFalse(list(outside.iterdir()))
 def test_activity_includes_server_download_and_failure_alert(self):
  from backend.activity import snapshot
  job=self.enqueue()
  self.assertTrue(any(t['id']=='server-download-'+job['id'] for t in snapshot(self.store)['tasks']))
  downloads.update(self.store,job,'failed','Provider unavailable.')
  self.assertTrue(any(a['title'].startswith('Server download') for a in snapshot(self.store)['alerts']))

 def test_size_limits_reject_known_and_unknown_length_before_publication(self):
  body=b'\x00\x00\x00\x20ftypisom'+b'x'*6000
  self.store.set('m3u_max_download_bytes','100')
  for total in (len(body),0):
   job=self.enqueue()
   with patch('backend.m3u_player.upstream',return_value=self.response(body,total)):
    downloads.transfer(self.app,self.store,job,threading.Event())
   with self.store.connect() as db:self.assertEqual(db.execute('SELECT state FROM m3u_downloads').fetchone()[0],'failed')
   self.assertFalse(list(self.movies.rglob('*.mp4')));self.assertFalse(list(self.movies.rglob('*.part')))

if __name__=='__main__':unittest.main()
