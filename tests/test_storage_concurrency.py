import sqlite3
from contextlib import nullcontext
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch
from backend.storage import Store
from backend.worker import run_tick
from backend.artwork import cache_loop


class StorageConcurrencyTests(unittest.TestCase):
    def test_existing_database_migrates_without_losing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            with sqlite3.connect(Path(directory)/'reeldock.sqlite3') as db:
                db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                db.execute("INSERT INTO settings VALUES ('fixture','saved')")
                self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0],'delete')
            db.close()
            store=Store(directory)
            self.assertEqual(store.get('fixture'),'saved')
            with store.connect() as db:
                self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0],'wal')

    def test_readers_continue_while_another_connection_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            store.set('fixture','before')
            with ThreadPoolExecutor(max_workers=1) as pool:
                with store.connect() as writer:
                    writer.execute('BEGIN EXCLUSIVE')
                    writer.execute("UPDATE settings SET value='after' WHERE key='fixture'")
                    # In rollback mode this read blocks until the writer exits.
                    read=pool.submit(store.get,'fixture')
                    self.assertEqual(read.result(timeout=2),'before')
            self.assertEqual(store.get('fixture'),'after')

    def test_multiple_writers_and_guide_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            store.set('public_origin','https://example.com')
            start=threading.Barrier(6)
            def writer(n):
                start.wait(timeout=5)
                for i in range(50): store.set('writer-'+str(n),i)
            def reader():
                start.wait(timeout=5)
                for _ in range(100):
                    self.assertEqual(store.get('public_origin'),'https://example.com')
            with ThreadPoolExecutor(max_workers=6) as pool:
                jobs=[pool.submit(writer,n) for n in range(4)]+[pool.submit(reader) for _ in range(2)]
                for job in jobs: job.result(timeout=15)
            for n in range(4): self.assertEqual(store.get('writer-'+str(n)),'49')

    def test_worker_survives_failure_to_record_database_error(self):
        importer=Mock()
        importer.store.background_operation.side_effect=nullcontext
        importer.store.reset_pending.return_value=False
        importer.store.set.side_effect=sqlite3.OperationalError('database is locked')
        with patch('backend.worker.tick',side_effect=sqlite3.OperationalError('database is locked')), self.assertLogs(level='WARNING'):
            run_tick(importer)
            run_tick(importer)
        self.assertEqual(importer.store.set.call_count,2)

    def test_artwork_loop_survives_failure_to_record_database_error(self):
        store=Mock()
        store.background_operation.side_effect=nullcontext
        store.reset_pending.return_value=False
        store.set.side_effect=sqlite3.OperationalError('database is locked')
        stop=Mock()
        stop.is_set.return_value=False
        stop.wait.side_effect=[False,True]
        with self.assertLogs(level='WARNING'):
            cache_loop(store,stop)
        self.assertEqual(stop.wait.call_count,2)


class ConnectionReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name)

    def tearDown(self):
        self.store.end_reuse()
        self.tmp.cleanup()

    def test_scope_reuses_one_connection_and_nested_calls_stay_separate(self):
        with self.store.connect() as a:pass
        with self.store.connect() as b:pass
        self.assertIsNot(a,b)  # outside a scope every call is independent
        with self.store.reuse():
            with self.store.connect() as first:
                with self.store.connect() as nested:
                    self.assertIsNot(first,nested)
            # One of them is kept idle and handed out again; no new connection.
            with patch('backend.storage.sqlite3.connect',side_effect=AssertionError('reopened')):
                with self.store.connect() as again:
                    self.assertIn(again,(first,nested))
                self.store.set('key','value');self.assertEqual(self.store.get('key'),'value')
        # Ending the scope closes the idle connection.
        with self.assertRaises(sqlite3.ProgrammingError):again.execute('SELECT 1')

    def test_failed_transaction_rolls_back_and_connection_stays_usable(self):
        with self.store.reuse():
            with self.assertRaises(RuntimeError):
                with self.store.connect() as db:
                    db.execute("INSERT INTO settings VALUES ('lost','x')")
                    raise RuntimeError('fixture')
            with self.store.connect() as again:
                self.assertIs(db,again)
                self.assertFalse(again.in_transaction)
                self.assertIsNone(again.execute("SELECT value FROM settings WHERE key='lost'").fetchone())
        self.assertEqual(self.store.get('lost'),'')

    def test_each_web_request_opens_a_single_connection(self):
        from backend.app import create_app
        app=create_app({'TESTING':True,'DATA_DIR':self.tmp.name})
        store=app.extensions['store']
        store.set('public_origin','https://dev.example.com');store.set('setup_complete','1')
        with store.connect() as db:db.execute("INSERT INTO users VALUES(1,'owner','unused')")
        client=app.test_client()
        with client.session_transaction(base_url='https://dev.example.com') as session:
            session['user_id']=1;session['csrf']='token'
        real=sqlite3.connect;opened=[]
        def counting(*args,**kwargs):
            opened.append(1);return real(*args,**kwargs)
        with patch('backend.storage.sqlite3.connect',counting):
            self.assertEqual(client.get('/api/downloads',base_url='https://dev.example.com').status_code,200)
        self.assertEqual(len(opened),1)


class IndexMigrationTests(unittest.TestCase):
    def test_existing_downloads_table_gains_indexes_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            with sqlite3.connect(Path(directory)/'reeldock.sqlite3') as db:
                db.execute('CREATE TABLE downloads(id TEXT PRIMARY KEY,title TEXT NOT NULL,quality TEXT NOT NULL,state TEXT NOT NULL,progress REAL NOT NULL DEFAULT 0,message TEXT NOT NULL DEFAULT \'\',created REAL NOT NULL,updated REAL NOT NULL)')
                db.execute("INSERT INTO downloads(id,title,quality,state,created,updated) VALUES('old00000001','Saved','720','complete',1,1)")
            db.close()
            from backend.app import create_app
            store=create_app({'TESTING':True,'DATA_DIR':directory}).extensions['store']
            with store.connect() as db:
                names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
                self.assertTrue({'downloads_state_updated','downloads_created','m3u_catalog_checked'}<=names)
                self.assertEqual(db.execute('SELECT title FROM downloads').fetchone()[0],'Saved')
