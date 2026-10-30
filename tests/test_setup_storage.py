import json
import tempfile
import unittest
from pathlib import Path
from backend.app import create_app
from backend import m3u, m3u_downloads, storage_layout

LOCAL='http://127.0.0.1:8790'
class StorageSetupTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  for name in ('data','legacy-youtube','legacy-links','ReelDock'):(self.root/name).mkdir()
  self.base=self.root/'ReelDock'
  self.config=dict(TESTING=True,DATA_DIR=str(self.root/'data'),MEDIA_DIR=str(self.root/'legacy-youtube'),M3U_DIR=str(self.root/'legacy-links'),REELDOCK_STORAGE_ROOTS=json.dumps([str(self.base)]),LOCAL_SETUP_URL=LOCAL,APP_PORT='8790')
  self.app=create_app(self.config);self.store=self.app.extensions['store'];self.client=self.app.test_client()
  self.csrf=self.client.get('/api/setup/state',base_url=LOCAL).json['csrf']
 def tearDown(self):self.tmp.cleanup()
 def post(self,action,body):return self.client.post('/api/setup/'+action,json=body,base_url=LOCAL,headers={'Origin':LOCAL,'X-CSRF-Token':self.csrf})
 def admin(self):
  self.csrf=self.post('admin',dict(username='owner',password='long-test-password',confirm='long-test-password')).json['csrf']
  self.assertEqual(self.post('skip-route',{}).status_code,200)
 def choose(self):self.assertEqual(self.post('storage',{'base':str(self.base)}).status_code,200)
 def test_single_base_creates_default_tree_and_persists(self):
  self.admin();self.choose()
  for relative in ('YouTube','M3U','Downloads/Movies','Downloads/TV Shows','Downloads/Unsorted'):self.assertTrue((self.base/relative).is_dir())
  state=self.client.get('/api/setup/state',base_url=LOCAL).json['storage'];self.assertTrue(state['ready']);self.assertEqual(state['base'],str(self.base))
  restarted=create_app(self.config)
  self.assertEqual(restarted.config['MEDIA_DIR'],str(self.base/'YouTube'));self.assertEqual(restarted.config['M3U_DIR'],str(self.base/'M3U'))
  self.assertEqual(m3u_downloads.settings(restarted,restarted.extensions['store'])['folders']['series'],str(self.base/'Downloads/TV Shows'))
 def test_missing_base_selects_first_root_and_advances_setup(self):
  self.admin()
  self.assertFalse(self.client.get('/api/setup/state',base_url=LOCAL).json['storage']['ready'])
  response=self.post('storage',{})
  self.assertEqual(response.status_code,200,response.json)
  self.assertEqual(self.store.get('storage_base'),str(self.base))
  self.assertEqual(self.store.get('setup_storage_ready'),'1')
  self.assertTrue(self.client.get('/api/setup/state',base_url=LOCAL).json['storage']['ready'])
  for relative in ('YouTube','M3U','Downloads/Movies','Downloads/TV Shows','Downloads/Unsorted'):
   self.assertTrue((self.base/relative).is_dir())
  self.assertEqual(self.post('source-ready',{}).status_code,200)
  self.assertEqual(self.post('finish',{}).status_code,200)
  self.assertEqual(self.client.get('/app/',base_url=LOCAL).status_code,200)
 def test_missing_base_prefers_configured_download_base(self):
  other=self.root/'other';other.mkdir()
  self.config.update(REELDOCK_DOWNLOAD_BASE=str(self.base),REELDOCK_STORAGE_ROOTS=json.dumps([str(other),str(self.base)]))
  self.app=create_app(self.config);self.store=self.app.extensions['store'];self.client=self.app.test_client()
  self.csrf=self.client.get('/api/setup/state',base_url=LOCAL).json['csrf']
  self.admin()
  response=self.post('storage',{})
  self.assertEqual(response.status_code,200,response.json)
  self.assertEqual(self.store.get('storage_base'),str(self.base))
  self.assertFalse(any(other.iterdir()))
 def test_missing_base_keeps_saved_choice_and_existing_files(self):
  self.admin();chosen=self.base/'chosen'
  self.assertEqual(self.post('storage',{'base':str(chosen)}).status_code,200)
  saved=chosen/'YouTube/keep.mp4';saved.write_bytes(b'keep')
  self.store.legacy_download_base=str(self.base)
  for body in ({},{'base':None},{'base':''}):
   with self.subTest(body=body):
    response=self.post('storage',body)
    self.assertEqual(response.status_code,200,response.json)
    self.assertEqual(self.store.get('storage_base'),str(chosen))
    self.assertEqual(saved.read_bytes(),b'keep')
 def test_missing_base_retains_storage_safety_checks_and_gives_compose_guidance(self):
  self.admin()
  populated=self.root/'populated';populated.mkdir();(populated/'keep.mp4').write_bytes(b'keep')
  ytsd=self.root/'YTSD';ytsd.mkdir()
  alias=self.root/'alias';alias.symlink_to(self.root/'data',target_is_directory=True)
  unavailable=self.root/'unavailable'
  cases=(
   (str(populated),[str(populated),str(self.base)],'empty folder'),
   (str(ytsd),[str(ytsd)],'YTSD'),
   (str(alias),[str(alias)],'symbolic links'),
   (str(self.root/'data'),[str(self.root/'data')],'application data'),
   (str(self.root),[str(self.root)],'application data'),
   (str(unavailable),[str(unavailable)],'not available'),
   ('/etc',[str(self.base)],'not available'),
   (str(self.base/'../data'),[str(self.base)],'absolute folder'),
   ('',[],'absolute folder'),
  )
  for base,roots,message in cases:
   with self.subTest(base=base):
    self.store.legacy_download_base=base
    self.app.config['REELDOCK_STORAGE_ROOTS']=json.dumps(roots)
    response=self.post('storage',{})
    self.assertEqual(response.status_code,400,response.json)
    self.assertIn(message.lower(),response.json['error'].lower())
    self.assertIn('REELDOCK_MEDIA_ROOT',response.json['error'])
    self.assertIn('Docker Compose',response.json['error'])
    self.assertFalse(self.store.get('storage_base'))
    self.assertFalse(self.store.get('setup_storage_ready'))
    self.assertFalse(any(self.base.iterdir()))
  self.assertEqual((populated/'keep.mp4').read_bytes(),b'keep')
  self.assertFalse(any(ytsd.iterdir()))
 def test_missing_base_respects_nonselectable_root_and_unwritable_mount(self):
  from unittest.mock import patch
  self.admin()
  self.app.config['REELDOCK_STORAGE_LOCATIONS']=json.dumps([dict(path=str(self.base),select_root=False)])
  response=self.post('storage',{})
  self.assertEqual(response.status_code,400,response.json)
  self.assertIn('inside this location',response.json['error'])
  self.assertIn('REELDOCK_MEDIA_ROOT',response.json['error'])
  self.assertFalse(any(self.base.iterdir()))
  self.app.config['REELDOCK_STORAGE_LOCATIONS']='[]'
  with patch('backend.setup_storage.probe',return_value={'writable':False}):
   response=self.post('storage',{})
  self.assertEqual(response.status_code,400,response.json)
  self.assertIn('writable',response.json['error'])
  self.assertIn('REELDOCK_MEDIA_ROOT',response.json['error'])
  self.assertFalse(self.store.get('storage_base'))
  self.assertFalse(self.store.get('setup_storage_ready'))
 def test_missing_base_does_not_adopt_an_existing_legacy_library(self):
  self.admin()
  source=m3u.save(self.store,dict(name='Existing playlist',url='https://example.com/list.m3u'))
  response=self.post('storage',{})
  self.assertEqual(response.status_code,400,response.json)
  self.assertIn('Existing library paths are in use',response.json['error'])
  self.assertIn('REELDOCK_MEDIA_ROOT',response.json['error'])
  self.assertFalse(self.store.get('storage_base'))
  self.assertFalse(any(self.base.iterdir()))
  self.assertEqual(self.app.config['MEDIA_DIR'],self.config['MEDIA_DIR'])
  with self.store.connect() as db:
   self.assertTrue(db.execute('SELECT 1 FROM m3u_sources WHERE id=?',(source,)).fetchone())
 def test_reject_unmounted_symlink_data_and_traversal(self):
  self.admin();(self.base/'escape').symlink_to(self.root/'data',target_is_directory=True)
  for value in (str(self.root/'data'),str(self.base/'escape'),str(self.base/'../data'),'/etc',123):
   self.assertEqual(self.post('storage',{'base':value}).status_code,400)
  self.assertFalse(self.store.get('storage_base'))
 def test_settings_names_source_layout_and_existing_data_preserved(self):
  self.admin();self.choose();values={**storage_layout.DEFAULTS,'youtube':'Channels','series':'Television'}
  storage_layout.save_names(self.app,self.store,values)
  self.assertEqual(self.app.config['MEDIA_DIR'],str(self.base/'Channels'))
  source=m3u.save(self.store,dict(name='My playlist',url='https://example.com/list.m3u'))
  with self.store.connect() as db:row=dict(db.execute('SELECT * FROM m3u_sources WHERE id=?',(source,)).fetchone())
  self.assertEqual(row['folder'],'My playlist')
  entries=[dict(path='TV_VOD/Show/Season 01/Episode.strm',title='Episode',kind='series',group='',content='https://example.com/video.mp4\n')]
  m3u.export(self.store,self.app.config['M3U_DIR'],row,entries,0)
  target=self.base/'M3U/My playlist/Television/Show/Season 01/Episode.strm';self.assertTrue(target.is_file())
  with self.assertRaises(ValueError):storage_layout.save_names(self.app,self.store,{**values,'m3u':'Other'})
  self.assertTrue(target.is_file())
  other=self.base/'Other'
  self.assertEqual(self.post('storage',{'base':str(other)}).status_code,400)
 def test_auth_csrf_origin_and_settings_validation(self):
  self.assertEqual(self.post('storage',{'base':str(self.base)}).status_code,409);self.admin()
  self.assertEqual(self.client.post('/api/setup/storage',json={'base':str(self.base)},base_url=LOCAL,headers={'Origin':LOCAL}).status_code,403)
  self.assertEqual(self.client.post('/api/setup/storage',json={'base':str(self.base)},base_url=LOCAL,headers={'Origin':'http://evil.example','X-CSRF-Token':self.csrf}).status_code,403)
  self.choose()
  for key,value in [('youtube','../escape'),('m3u','Downloads'),('series','Movies')]:
   with self.assertRaises(ValueError):storage_layout.save_names(self.app,self.store,{**storage_layout.DEFAULTS,key:value})
 def test_browse_host_paths_and_select_dedicated_folder(self):
  from backend.storage_locations import resolve
  home=self.root/'home';home.mkdir();(home/'ReelDock').mkdir();(home/'unrelated').mkdir();(home/'unrelated'/'keep.txt').write_text('keep')
  self.app.config['REELDOCK_STORAGE_ROOTS']=json.dumps([str(home)])
  self.app.config['REELDOCK_STORAGE_LOCATIONS']=json.dumps([dict(path=str(home),host_path='/home',label='Home folders',select_root=False)])
  self.assertEqual(self.client.get('/api/storage-folders',base_url=LOCAL).status_code,401)
  self.admin()
  listing=self.client.get('/api/storage-folders?path=/home',base_url=LOCAL).json
  self.assertEqual(listing['host_path'],'/home');self.assertFalse(listing['selectable'])
  for value in ('/home','/home/unrelated'):
   self.assertEqual(self.post('storage',{'base':value}).status_code,400)
  self.assertEqual(self.post('storage',{'base':'/home/ReelDock'}).status_code,200)
  self.assertEqual(self.store.get('storage_base'),str(home/'ReelDock'))
  self.assertEqual((home/'unrelated'/'keep.txt').read_text(),'keep')
  (home/'alias').symlink_to(self.root/'data')
  with self.assertRaises(ValueError):resolve(self.app,'/home/alias')
 def test_reset_mount_scaffold_can_be_selected(self):
  self.admin()
  for name in ('YouTube','M3U','Downloads/Movies'):(self.base/name).mkdir(parents=True)
  self.choose()
 def test_move_copies_library_updates_paths_and_keeps_original(self):
  from backend.storage_move import move
  self.admin();self.choose()
  target=self.base/'Downloads/Movies/saved.mp4';target.write_bytes(b'media')
  with self.store.connect() as db:
   db.execute("INSERT INTO m3u_downloads(id,destination,state) VALUES('test',?,'complete')",(str(target),))
   db.execute('INSERT INTO m3u_sidecars VALUES(?,?)',(str(self.base/'M3U/file.nfo'),'digest'))
  destination=self.root/'next';destination.mkdir()
  self.app.config['REELDOCK_STORAGE_ROOTS']=json.dumps([str(self.base),str(destination)])
  with self.store.exclusive_operation():move(self.app,self.store,str(destination))
  self.assertEqual(self.store.get('storage_base'),str(destination));self.assertEqual(target.read_bytes(),b'media')
  self.assertEqual((destination/'Downloads/Movies/saved.mp4').read_bytes(),b'media')
  with self.store.connect() as db:
   self.assertEqual(db.execute("SELECT destination FROM m3u_downloads WHERE id='test'").fetchone()[0],str(destination/'Downloads/Movies/saved.mp4'))
  self.assertEqual(self.app.config['MEDIA_DIR'],str(destination/'YouTube'))
 def test_move_failure_keeps_original_settings_and_files(self):
  from backend.storage_move import move
  from unittest.mock import patch
  self.admin();self.choose();(self.base/'YouTube/video').write_text('original')
  destination=self.root/'next';destination.mkdir()
  self.app.config['REELDOCK_STORAGE_ROOTS']=json.dumps([str(self.base),str(destination)])
  with patch('backend.storage_move.shutil.copytree',side_effect=OSError('full')):
   with self.assertRaises(OSError),self.store.exclusive_operation():move(self.app,self.store,str(destination))
  self.assertEqual(self.store.get('storage_base'),str(self.base));self.assertFalse(any(destination.iterdir()))
  self.assertEqual((self.base/'YouTube/video').read_text(),'original')
  (self.base/'YouTube/link').symlink_to(self.root/'data')
  with self.assertRaises(ValueError),self.store.exclusive_operation():move(self.app,self.store,str(destination))
 def test_settings_move_requires_csrf_and_is_available_after_setup(self):
  self.admin();self.choose();self.store.set('setup_complete','1')
  self.app.config['REELDOCK_STORAGE_ROOTS']=json.dumps([str(self.base),str(self.root/'new')]);(self.root/'new').mkdir()
  body={'base':str(self.root/'new')}
  self.assertEqual(self.client.post('/api/storage-settings',json=body,base_url=LOCAL).status_code,403)
  response=self.client.post('/api/storage-settings',json=body,base_url=LOCAL,headers={'Origin':LOCAL,'X-CSRF-Token':self.csrf})
  self.assertEqual(response.status_code,200,response.json)
