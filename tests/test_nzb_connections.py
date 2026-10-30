"""Configured native NNTP capacity is shared across downloads and previews."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

from backend.downloaders.nzb.engine import NzbEngine,_Job,_PullState,_ConnectionBudget,articles_on_server
from backend.downloaders.nzb.nntp import NntpError
from backend.downloaders.nzb.yenc import encode


RAW=b'''<nzb><file subject="&quot;Fixture.2026.mkv&quot; yEnc (1/1)">
<groups><group>fixture</group></groups><segments>
<segment bytes="5" number="1">article@fixture</segment></segments></file></nzb>'''
SERVER={'host':'fixture.invalid','port':563,'ssl':True,'username':'fixture','password':'not-real','connections':60}


def segments(count):
    return [SimpleNamespace(number=index+1,size=5,message_id='article'+str(index)+'@fixture',groups=('fixture',),file_index=0) for index in range(count)]


class NzbConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        spawn=patch.object(NzbEngine,'_spawn');spawn.start();self.addCleanup(spawn.stop)
        self.engine=NzbEngine(Path(self.temp.name)/'media',Path(self.temp.name)/'data')
        self.addCleanup(self.engine.shutdown)

    def test_one_job_starts_all_sixty_configured_pullers(self):
        arrived=threading.Event();release=threading.Event();lock=threading.Lock();count=0
        def pull(*args):
            nonlocal count
            with lock:
                count+=1
                if count==60:arrived.set()
            release.wait(3)
        job=_Job('fixture','Fixture',[],set())
        with patch.object(self.engine,'_pull',side_effect=pull):
            worker=threading.Thread(target=self.engine._fetch_server,args=(job,SERVER,segments(120),_PullState()));worker.start()
            try:self.assertTrue(arrived.wait(2),'Sixty configured pullers did not start');self.assertEqual(count,60)
            finally:release.set();worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_fewer_pending_articles_do_not_create_unused_pullers(self):
        with patch.object(self.engine,'_pull') as pull:
            self.engine._fetch_server(_Job('fixture','Fixture',[],set()),SERVER,segments(7),_PullState())
        self.assertEqual(pull.call_count,7)

    def test_two_jobs_never_exceed_the_shared_sixty_connection_allowance(self):
        lock=threading.Lock();full=threading.Event();release=threading.Event();active=0;peak=0;opened=0;errors=[]
        class Client:
            def __init__(inner,*args,**kwargs):inner.open=False;inner.checkpoint=None
            def body(inner,*args):
                nonlocal active,peak,opened
                if not inner.open:
                    with lock:
                        inner.open=True;active+=1;opened+=1;peak=max(peak,active)
                        if active==60:full.set()
                release.wait(4)
                return encode(b'video','Fixture.2026.mkv')
            def close(inner):
                nonlocal active
                if inner.open:
                    with lock:inner.open=False;active-=1
        def fetch(job):
            try:self.engine._fetch_server(job,SERVER,segments(90),_PullState())
            except BaseException as exc:errors.append(exc)
        jobs=[_Job('job'+str(index),'Fixture',[],set()) for index in range(2)]
        with patch('backend.downloaders.nzb.engine.NntpClient',Client),patch.object(self.engine,'_write_part'):
            threads=[threading.Thread(target=fetch,args=(job,)) for job in jobs]
            for thread in threads:thread.start()
            try:
                self.assertTrue(full.wait(3));time.sleep(.05)
                self.assertEqual(active,60);self.assertEqual(self.engine._slots._used,60)
            finally:
                release.set()
                for thread in threads:thread.join(5)
        self.assertFalse(errors);self.assertEqual(peak,60);self.assertGreaterEqual(opened,60)
        self.assertEqual(active,0);self.assertEqual(self.engine._slots._used,0)
        self.assertFalse(any(thread.is_alive() for thread in threads))

    def test_cancelled_waiting_job_exits_before_another_job_releases_slot(self):
        self.engine._slots.set_limit(1);self.assertTrue(self.engine._slots.acquire(self.engine.stop))
        job=_Job('waiting','Fixture',[],set());done=threading.Event()
        def fetch():
            try:self.engine._fetch_server(job,dict(SERVER,connections=1),segments(2),_PullState())
            finally:done.set()
        with patch('backend.downloaders.nzb.engine.NntpClient') as client:
            worker=threading.Thread(target=fetch);worker.start()
            try:
                self.assertFalse(done.wait(.05));job.removed=True
                self.assertTrue(done.wait(1));client.assert_not_called();self.assertEqual(self.engine._slots._used,1)
            finally:self.engine._slots.release();worker.join(2)
        self.assertEqual(self.engine._slots._used,0)

    def test_global_pause_does_not_reserve_a_slot_or_prevent_cancel(self):
        job=_Job('paused','Fixture',[],set());self.engine.global_paused=lambda:True
        with patch('backend.downloaders.nzb.engine.NntpClient') as client:
            worker=threading.Thread(target=self.engine._fetch_server,args=(job,dict(SERVER,connections=2),segments(4),_PullState()));worker.start()
            try:
                time.sleep(.05);self.assertEqual(self.engine._slots._used,0);client.assert_not_called()
                job.removed=True;worker.join(1);self.assertFalse(worker.is_alive())
            finally:self.engine.stop.set();worker.join(2)
        self.assertEqual(self.engine._slots._used,0)

    def test_shutdown_wakes_waiters_without_leaking_reserved_capacity(self):
        budget=_ConnectionBudget();budget.set_limit(1);stop=threading.Event();self.assertTrue(budget.acquire(stop));result=[]
        worker=threading.Thread(target=lambda:result.append(budget.acquire(stop)));worker.start()
        try:
            time.sleep(.05);stop.set();worker.join(1);self.assertEqual(result,[False])
            self.assertEqual(budget._used,1)
        finally:stop.set();budget.release();worker.join(2)
        self.assertEqual(budget._used,0)

    def test_full_provider_budget_makes_preview_unchecked_without_extra_socket(self):
        budget=self.engine._slots;budget.set_limit(60)
        for _ in range(60):self.assertTrue(budget.acquire(self.engine.stop))
        with patch('backend.downloaders.nzb.engine.NntpClient') as client:
            before=time.monotonic();result=articles_on_server([SERVER],RAW,budget=budget,stop=self.engine.stop)
            self.assertLess(time.monotonic()-before,.5);self.assertEqual(result,'unchecked');client.assert_not_called()
        self.assertEqual(budget._used,60)
        for _ in range(60):budget.release()

    def test_preview_releases_its_slot_for_success_missing_and_network_failure(self):
        self.engine._slots.set_limit(60)
        for response,expected in ((True,'available'),(False,'missing'),(NntpError(0,'fixture failure'),'unchecked')):
            with self.subTest(response=expected),patch('backend.downloaders.nzb.engine.NntpClient') as factory:
                client=factory.return_value
                if isinstance(response,Exception):client.stat.side_effect=response
                else:client.stat.return_value=response
                self.assertEqual(articles_on_server([SERVER],RAW,budget=self.engine._slots,stop=self.engine.stop),expected)
                client.close.assert_called_once();self.assertEqual(self.engine._slots._used,0)
                self.assertEqual(factory.call_args.args[:3],('fixture.invalid',563,True))
                self.assertEqual(factory.call_args.kwargs['timeout'],8)

    def test_lower_provider_ceiling_cannot_borrow_a_larger_current_global_limit(self):
        budget=self.engine._slots;budget.set_limit(60);self.assertTrue(budget.acquire(self.engine.stop))
        with patch('backend.downloaders.nzb.engine.NntpClient') as client:
            self.assertEqual(articles_on_server([dict(SERVER,connections=1)],RAW,budget=budget,stop=self.engine.stop),'unchecked')
            client.assert_not_called()
        budget.release();self.assertEqual(budget._used,0)


if __name__=='__main__':unittest.main()
