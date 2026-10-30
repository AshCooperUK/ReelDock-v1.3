import json
import time
from datetime import datetime, timezone
import tempfile
import threading
import unittest
from unittest.mock import patch
from backend.storage import Store
from backend.catalogue import initialise
from backend.artwork import cache_batch, cache_loop, request_artwork, queue_counts, QUEUE, queue_times


class ArtworkQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name)
        initialise(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, key):
        with self.store.connect() as db:
            db.execute('INSERT INTO artwork(id,url) VALUES (?,?)',(key,key))

    def test_existing_queue_prioritises_logos_then_newest_across_channels(self):
        for key in ['old','new','logo','banner']:
            self.add(key)
        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES (?,?,1)',('c',json.dumps(dict(avatar='/artwork/logo',banner='/artwork/banner'))))
            for key,stamp in [('old','2020-01-01T00:00:00Z'),('new',datetime.now(timezone.utc).isoformat())]:
                db.execute('INSERT INTO videos VALUES (?,?,?,?,?,0)',(key,key,stamp[:10],stamp,json.dumps(dict(thumbnail='/artwork/'+key))))
        seen=[]
        def fetch(url):
            seen.append(url)
            return b'fixture'
        cache_batch(self.store,fetch=fetch,workers=1)
        self.assertEqual(seen,['logo','new'])
        self.assertEqual(queue_counts(self.store),(0,2))
        request_artwork(self.store,'old')
        request_artwork(self.store,'banner')
        self.assertEqual(queue_counts(self.store),(2,0))
        cache_batch(self.store,fetch=fetch,workers=1)
        self.assertEqual(seen[:2],['logo','new'])
        self.assertCountEqual(seen[2:],['old','banner'])
        cache_batch(self.store,fetch=fetch,workers=1)
        self.assertEqual(len(seen),4,'Fresh artwork is not downloaded again')

    def test_large_queue_uses_bounded_database_work(self):
        # Limit SQLite instruction steps, rather than relying on machine speed.
        # The former nested join exceeds this budget with 30,000 images.
        with self.store.connect() as db:
            rows=[(f'{i:064x}',str(i)) for i in range(30000)]
            db.executemany('INSERT INTO artwork(id,url) VALUES (?,?)',rows)
            db.executemany('INSERT INTO videos VALUES (?,?,?,?,?,0)',
                [(key,'channel','2099-01-01','2099-01-01T00:00:00Z',json.dumps(dict(thumbnail='/artwork/'+key))) for key,_ in rows])
        with self.store.connect() as db:
            steps=0
            def budget():
                nonlocal steps
                steps+=1
                return int(steps>5000)
            db.set_progress_handler(budget,1000)
            self.assertEqual(db.execute(QUEUE+'SELECT COUNT(*) FROM eligible WHERE checked=0',queue_times()).fetchone()[0],30000)
            self.assertEqual(len(db.execute(QUEUE+'SELECT id FROM eligible WHERE checked<:stale ORDER BY priority,stamp DESC LIMIT 30',queue_times()).fetchall()),30)

    def test_downloads_are_parallel_but_bounded(self):
        for i in range(8):
            self.add(str(i))
            request_artwork(self.store,str(i))
        barrier=threading.Barrier(4)
        lock=threading.Lock()
        active=peak=0
        def fetch(url):
            nonlocal active,peak
            with lock:
                active+=1
                peak=max(peak,active)
            barrier.wait(timeout=5)
            with lock: active-=1
            return b'fixture'
        cache_batch(self.store,fetch=fetch)
        self.assertEqual(peak,4)

    def test_pruned_image_is_not_published_after_fetch(self):
        self.add('removed')
        request_artwork(self.store,'removed')
        def fetch(url):
            with self.store.connect() as db: db.execute('DELETE FROM artwork')
            return b'fixture'
        cache_batch(self.store,fetch=fetch)
        self.assertFalse((self.store.directory/'artwork'/'removed').exists())
        self.assertFalse(list((self.store.directory/'artwork').glob('*.tmp')))

    def test_loop_downloads_while_scan_flag_is_running(self):
        self.store.set('setup_complete','1')
        self.store.set('sync_running','1')
        stop=threading.Event()
        def download(store):
            self.assertEqual(store.get('sync_running'),'1')
            stop.set()
            return 1
        with patch('backend.artwork.cache_batch', side_effect=download) as batch:
            cache_loop(self.store,stop)
            batch.assert_called_once()
        self.assertTrue(self.store.get('artwork_heartbeat'))
