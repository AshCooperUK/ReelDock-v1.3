import tempfile
import unittest
from backend.storage import Store
from backend.event_log import record,started
from backend.activity import record as task

class EventLogTests(unittest.TestCase):
    def test_versions_and_task_transitions_are_persistent(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(root)
            started(store,'v1');started(store,'v1','worker');started(store,'v2');started(store,'v2','worker')
            task(store,'test','Artwork','running','Checking')
            task(store,'test','Artwork','running','Checking next',20)
            task(store,'test','Artwork','complete','Checked 2 images')
            task(store,'test','Artwork','complete','Checked 2 images')
            record(store,'Signed in','Account signed in successfully.','Account')
            with Store(root).connect() as db:rows=[dict(r) for r in db.execute('SELECT * FROM activity_events')]
            self.assertEqual(sum(r['title']=='ReelDock updated' for r in rows),1)
            self.assertEqual(sum(r['title']=='Artwork' for r in rows),1)
            self.assertEqual(len(rows),7)
            self.assertEqual(rows[-1]['category'],'Account')

    def test_sign_in_events_do_not_contain_credentials(self):
        from backend.app import create_app
        from werkzeug.security import generate_password_hash
        with tempfile.TemporaryDirectory() as root:
            app=create_app({'TESTING':True,'DATA_DIR':root,'MEDIA_DIR':root+'/media'})
            store=app.extensions['store'];origin='https://events.example.com'
            store.set('public_origin',origin);store.set('setup_complete','1')
            with store.connect() as db:db.execute('INSERT INTO users VALUES(1,?,?)',('admin',generate_password_hash('private-password')))
            client=app.test_client()
            with client.session_transaction(base_url=origin) as session:session['csrf']='test'
            response=client.post('/login',base_url=origin,data={'csrf':'test','username':'admin','password':'private-password'},headers={'Origin':origin})
            self.assertEqual(response.status_code,302)
            with store.connect() as db:rows=[dict(r) for r in db.execute('SELECT * FROM activity_events')]
            self.assertEqual(rows[-1]['title'],'Signed in')
            self.assertNotIn('private-password',str(rows))
            self.assertNotIn('admin',str(rows))

    def test_artwork_batches_share_a_summary_and_failures_remain_separate(self):
        from backend.event_log import artwork_batch
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            store=Store(root)
            with patch('backend.event_log.time.time',return_value=1000):artwork_batch(store,3)
            with patch('backend.event_log.time.time',return_value=1100):artwork_batch(store,4,1)
            task(store,'youtube-artwork','YouTube artwork','complete','Finished processing 5 images.')
            with store.connect() as db:rows=[dict(r) for r in db.execute('SELECT * FROM activity_events ORDER BY id')]
            self.assertEqual(len(rows),2)
            self.assertEqual(rows[0]['message'],'7 images processed.')
            self.assertEqual(rows[0]['created'],1000)
            self.assertEqual(rows[1]['state'],'failed')
            store=Store(root) # survives reopening, including trigger migration
            with patch('backend.event_log.time.time',return_value=1200):artwork_batch(store,2)
            with patch('backend.event_log.time.time',return_value=1600):artwork_batch(store,1)
            with store.connect() as db:rows=[dict(r) for r in db.execute('SELECT * FROM activity_events ORDER BY id')]
            self.assertEqual(len(rows),3)
            self.assertEqual(rows[0]['message'],'9 images processed.')
            self.assertEqual(rows[-1]['message'],'1 image processed.')

    def test_old_artwork_successes_merge_without_losing_failures(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(root)
            with store.connect() as db:
                db.execute("DELETE FROM settings WHERE key='artwork_activity_merged'")
                for stamp,state,message in [(1000,'complete','Finished processing 3 images.'),(1100,'failed','Retrying.'),(1200,'complete','Finished processing 2 images.'),(1600,'complete','Finished processing 1 images.')]:
                    db.execute("INSERT INTO activity_events(created,category,title,state,message) VALUES(?,'Background work','YouTube artwork',?,?)",(stamp,state,message))
            Store(root)
            with store.connect() as db:rows=[dict(r) for r in db.execute('SELECT * FROM activity_events ORDER BY created')]
            self.assertEqual(len(rows),3)
            self.assertEqual(rows[0]['message'],'5 images processed.')
            self.assertEqual(rows[1]['state'],'failed')
            self.assertEqual(rows[2]['message'],'1 image processed.')

    def test_metadata_batches_and_historical_totals(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            store=Store(root)
            for stamp in (1000,1100):
                with patch('backend.event_log.time.time',return_value=stamp):
                    task(store,'m3u-exports','Emby metadata & artwork','running','Processing')
                    task(store,'m3u-exports','Emby metadata & artwork','complete','Processed 8 titles for local metadata sharing.')
            task(store,'m3u-exports','Emby metadata & artwork','error','Failed')
            with store.connect() as db:
                rows=[dict(r) for r in db.execute('SELECT * FROM activity_events ORDER BY id')]
                self.assertEqual(len(rows),2)
                self.assertEqual(rows[0]['message'],'16 metadata items processed.')
                self.assertEqual(rows[1]['state'],'error')
                db.execute("DELETE FROM settings WHERE key='metadata_activity_merged'")
                for stamp in (2000,2100):db.execute("INSERT INTO activity_events(created,category,title,state,message) VALUES(?,'Background work','Emby metadata & artwork','complete','Processed 8 titles for local metadata sharing.')",(stamp,))
            Store(root)
            with store.connect() as db:rows=[dict(r) for r in db.execute('SELECT * FROM activity_events ORDER BY id')]
            self.assertEqual(len(rows),3)
            self.assertEqual(rows[-1]['message'],'16 metadata items processed.')
