"""Concurrent native checkpoints share reads without weakening Clear fences."""
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from backend import media_holds


class NativeHoldReadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.data=self.root/'nzb-engine';self.data.mkdir()
        self.path=self.root/'reeldock.sqlite3'
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE media_clear_holds(key TEXT PRIMARY KEY,group_id TEXT,created REAL)')
        for name in ('_NATIVE_CACHE','_NATIVE_REFRESH_LOCKS'):
            context=patch.dict(getattr(media_holds,name),{},clear=True)
            context.start();self.addCleanup(context.stop)

    def test_twenty_four_same_job_readers_refresh_expired_cache_once(self):
        ready=threading.Barrier(25);entered=threading.Event();release=threading.Event()
        connection=Mock();count=[]
        def lookup(db,keys):
            count.append(tuple(keys));entered.set();release.wait(2);return False
        def read():
            ready.wait();return media_holds.native_blocked(self.data,['native:nzb:fixture'])
        with patch.object(media_holds.sqlite3,'connect',return_value=connection) as connect,patch.object(media_holds,'blocked_in',side_effect=lookup):
            with ThreadPoolExecutor(max_workers=24) as pool:
                futures=[pool.submit(read) for _ in range(24)]
                try:
                    ready.wait(timeout=2);self.assertTrue(entered.wait(1))
                    time.sleep(.04)
                finally:release.set()
                self.assertEqual([future.result(timeout=2) for future in futures],[False]*24)
            self.assertEqual(connect.call_count,1)
            self.assertEqual(count,[('native:nzb:fixture',)])
            connection.close.assert_called_once()
            self.assertIn('mode=ro',connect.call_args.args[0])
            self.assertTrue(connect.call_args.kwargs['uri'])

    def test_different_jobs_refresh_independently_without_global_database_lock(self):
        entered=threading.Barrier(2)
        def lookup(db,keys):entered.wait(timeout=1);return False
        with patch.object(media_holds.sqlite3,'connect',side_effect=lambda *a,**k:Mock()) as connect,patch.object(media_holds,'blocked_in',side_effect=lookup):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(media_holds.native_blocked,self.data,[key]) for key in ('first','second')]
                self.assertEqual([future.result(timeout=2) for future in futures],[False,False])
            self.assertEqual(connect.call_count,2)

    def test_explicit_fresh_check_observes_new_hold_inside_cache_lifetime(self):
        self.assertFalse(media_holds.native_blocked(self.data,['fixture']))
        with sqlite3.connect(self.path) as db:db.execute("INSERT INTO media_clear_holds VALUES('fixture','group',0)")
        self.assertFalse(media_holds.native_blocked(self.data,['fixture']))
        self.assertTrue(media_holds.native_blocked(self.data,['fixture'],fresh=True))
        self.assertTrue(media_holds.native_blocked(self.data,['fixture']))

    def test_ordinary_cache_expires_after_two_hundred_milliseconds(self):
        clock=[20.0]
        with patch.object(media_holds.time,'monotonic',side_effect=lambda:clock[0]),patch.object(media_holds,'blocked_in',side_effect=[False,True]) as lookup:
            self.assertFalse(media_holds.native_blocked(self.data,['fixture']))
            clock[0]=20.199;self.assertFalse(media_holds.native_blocked(self.data,['fixture']))
            clock[0]=20.201;self.assertTrue(media_holds.native_blocked(self.data,['fixture']))
            self.assertEqual(lookup.call_count,2)

    def test_unreadable_database_stays_held_and_fresh_checks_still_retry_read(self):
        with patch.object(media_holds.sqlite3,'connect',side_effect=sqlite3.OperationalError('isolated read failure')) as connect:
            self.assertTrue(media_holds.native_blocked(self.data,['fixture']))
            self.assertTrue(media_holds.native_blocked(self.data,['fixture']))
            self.assertEqual(connect.call_count,1)
            self.assertTrue(media_holds.native_blocked(self.data,['fixture'],fresh=True))
            self.assertEqual(connect.call_count,2)

    def test_fresh_reader_queued_behind_cached_reader_makes_own_authoritative_read(self):
        entered=threading.Event();release=threading.Event();fresh_started=threading.Event();answers=iter((False,True))
        def lookup(db,keys):
            answer=next(answers)
            if answer is False:entered.set();release.wait(2)
            return answer
        def fresh():fresh_started.set();return media_holds.native_blocked(self.data,['fixture'],fresh=True)
        with patch.object(media_holds,'blocked_in',side_effect=lookup) as read:
            with ThreadPoolExecutor(max_workers=2) as pool:
                ordinary=pool.submit(media_holds.native_blocked,self.data,['fixture'])
                try:
                    self.assertTrue(entered.wait(1));authoritative=pool.submit(fresh)
                    self.assertTrue(fresh_started.wait(1))
                finally:release.set()
                self.assertFalse(ordinary.result(timeout=2));self.assertTrue(authoritative.result(timeout=2))
            self.assertEqual(read.call_count,2)


if __name__=='__main__':unittest.main()
