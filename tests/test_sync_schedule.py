import tempfile,time,unittest
from unittest.mock import Mock,patch
from backend.app import create_app
from backend.sync_schedule import read,save
from backend.worker import tick

class ScheduleTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.app=create_app({'TESTING':True,'DATA_DIR':self.tmp.name});self.store=self.app.extensions['store'];self.store.set('setup_complete','1');self.store.set('next_history',time.time()+86400)
 def tearDown(self):self.tmp.cleanup()
 def test_saved_interval_controls_unattended_scan_and_pause(self):
  save(self.store,30);importer=Mock(store=self.store)
  with patch('backend.youtube_fallback.add_batch'):
   tick(importer,False)
   importer.run.assert_called_once()
   self.assertAlmostEqual(float(self.store.get('next_sync')),time.time()+1800,delta=5)
   tick(importer,False);importer.run.assert_called_once()
   self.store.set('next_sync',0);self.store.set('youtube_sync_paused','true');tick(importer,False);importer.run.assert_called_once()
 def test_batch_pacing_and_validation(self):
  save(self.store,15);self.store.set('youtube_fallback_remaining',20)
  with patch('backend.youtube_fallback.add_batch'):tick(Mock(store=self.store),False)
  self.assertAlmostEqual(float(self.store.get('next_sync')),time.time()+5,delta=2)
  for value in (0,1,True,'30',1500):
   with self.assertRaises(ValueError):save(self.store,value)
  self.assertEqual(read(self.store)['minutes'],15)

 def test_route_login_csrf_and_shared_saved_preference(self):
  origin='https://schedule.example';self.store.set('public_origin',origin)
  with self.store.connect() as db:db.execute("INSERT INTO users VALUES(1,'admin','unused')")
  client=self.app.test_client()
  self.assertEqual(client.get('/api/youtube-schedule',base_url=origin).status_code,401)
  with client.session_transaction(base_url=origin) as session:session.update(user_id=1,csrf='test')
  self.assertEqual(client.post('/api/youtube-schedule',base_url=origin,headers={'Origin':origin},json={'minutes':30}).status_code,403)
  headers={'Origin':origin,'X-CSRF-Token':'test'}
  self.assertEqual(client.post('/api/youtube-schedule',base_url=origin,headers=headers,json=[]).status_code,400)
  self.assertEqual(client.post('/api/youtube-schedule',base_url=origin,headers=headers,json={'minutes':30}).json['minutes'],30)
  other=self.app.test_client()
  with other.session_transaction(base_url=origin) as session:session['user_id']=1
  self.assertEqual(other.get('/api/youtube-schedule',base_url=origin).json['minutes'],30)
