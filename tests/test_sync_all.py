import json
import tempfile
import unittest
from unittest.mock import patch
from backend.app import create_app
from backend.sync_all import queue, status, tick

class SyncAllTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=create_app({'TESTING':True,'DATA_DIR':self.tmp.name,'MEDIA_DIR':self.tmp.name+'/media'})
        self.store=self.app.extensions['store']
        with self.store.connect() as db:db.execute("INSERT INTO users VALUES (1,'fixture','unused')")
        self.store.set('public_origin','https://dev.example.com');self.store.set('setup_complete','1')
        self.client=self.app.test_client()
        with self.client.session_transaction(base_url='https://dev.example.com') as session:session['user_id']=1;session['csrf']='csrf'
    def tearDown(self):self.tmp.cleanup()
    def jobs(self):
        with self.store.connect() as db:
            for i,state in enumerate(['failed','complete','cancelled','downloading','skipped']):
                db.execute("INSERT INTO downloads(id,title,quality,state,created,updated) VALUES (?,?,'720',?,0,0)",(str(i),'Video',state))
            for key,enabled,state in [('on',True,'complete'),('off',False,'complete'),('busy',True,'running')]:
                db.execute('INSERT INTO m3u_sources(id,name,url,options,folder,state) VALUES (?,?,?,?,?,?)',(key,key,'https://example.com/list',json.dumps({'enabled':enabled}),key,state))
            db.execute("INSERT INTO m3u_downloads(id,source,path,state,created,updated) VALUES ('server','on','x.strm','failed',0,0)")
    def test_queues_configured_work_and_retry_only_failures(self):
        self.jobs();self.store.set('emby_url','https://emby.example');self.store.set('emby_key','fixture')
        result=queue(self.store);self.assertTrue(result['busy'])
        with self.store.connect() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT state FROM downloads ORDER BY id')],['queued','complete','cancelled','downloading','skipped'])
            self.assertEqual(dict(db.execute('SELECT id,state FROM m3u_sources')),{'on':'queued','off':'complete','busy':'running'})
            self.assertEqual(db.execute("SELECT state FROM m3u_downloads WHERE id='server'").fetchone()[0],'queued')
            self.assertTrue(db.execute("SELECT 1 FROM emby_refresh WHERE channel='sync-all'").fetchone())
        self.assertEqual(self.store.get('sync_requested'),'1')
        receipt=self.store.get('sync_all_receipt');queue(self.store);self.assertEqual(self.store.get('sync_all_receipt'),receipt)
    def test_pauses_and_cancellation_preserved(self):
        self.jobs();self.store.set('youtube_sync_paused','true');self.store.set('download_pause_downloads','true')
        queue(self.store)
        self.assertEqual(self.store.get('sync_requested'),'')
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT state FROM downloads WHERE id='0'").fetchone()[0],'failed')
        self.assertIn('Paused:',status(self.store)['message'])
    def test_auth_and_csrf(self):
        url='/api/sync-all';origin='https://dev.example.com'
        self.assertEqual(self.app.test_client().post(url,base_url=origin,headers={'Origin':origin}).status_code,401)
        self.assertEqual(self.client.post(url,base_url=origin,headers={'Origin':origin,'X-CSRF-Token':'bad'}).status_code,403)
        self.assertEqual(self.client.post(url,base_url=origin,headers={'Origin':origin,'X-CSRF-Token':'csrf'}).status_code,202)
    def test_stays_busy_for_fallback_batches_then_finishes(self):
        with patch('backend.sync_all.time.time',return_value=1000):queue(self.store)
        self.store.set('sync_requested','0');self.store.set('recent_scan_started','1000')
        with patch('backend.sync_all.time.time',return_value=1100):
            tick(self.store);self.assertTrue(status(self.store)['busy'])
            self.store.set('recent_scan_started','0');tick(self.store);tick(self.store)
            self.assertFalse(status(self.store)['busy'])
    def test_final_emby_scan_after_new_downloads(self):
        self.store.set('emby_url','https://emby.example');self.store.set('emby_key','fixture')
        with patch('backend.sync_all.time.time',return_value=1000):queue(self.store)
        self.store.set('sync_requested','0')
        with self.store.connect() as db:
            db.execute('DELETE FROM emby_refresh')
            db.execute("INSERT INTO downloads(id,title,quality,state,created,updated) VALUES ('new','New','720','downloading',1001,1001)")
        with patch('backend.sync_all.time.time',return_value=1100):
            tick(self.store);self.assertTrue(status(self.store)['busy'])
            with self.store.connect() as db:db.execute("UPDATE downloads SET state='complete'")
            tick(self.store)
            with self.store.connect() as db:self.assertTrue(db.execute("SELECT 1 FROM emby_refresh WHERE channel='sync-all'").fetchone())
