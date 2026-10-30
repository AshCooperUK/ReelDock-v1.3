"""Explicit Download All survives interrupted web work, using disposable state."""
import json
import threading
import time
import unittest
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch

import test_view_media as vm
from backend import acquisition,discover_store as ds,discovery_engine as engine
from backend import series_plan_work as work,watchlist_policy
from backend.media_management import service,store as records
from backend.storage import Store


class SeriesPlanResumeTests(unittest.TestCase):
    def setUp(self):
        vm.ViewMediaTests.setUp(self)
        self.item=dict(id='tvdb:show:81580',kind='show',externalId='81580',title='Disposable series')
        self.scheduler=patch.object(engine,'schedule');self.schedule=self.scheduler.start();self.addCleanup(self.scheduler.stop)
        self.composer=patch.object(service,'compose',return_value={'file':False});self.composer.start();self.addCleanup(self.composer.stop)
    tearDown=vm.ViewMediaTests.tearDown

    def episodes(self,count=1,*,season=2005,start=1,day='2020-01-01'):
        values=[]
        for number in range(start,start+count):
            key=f'{self.item["id"]}:{season}:{number}'
            body=dict(item=self.item,episode=dict(season=season,number=number,name=f'Episode {number}'),date=day,automatic=True)
            values.append((key,self.item['id'],'episode',json.dumps(body),time.time()))
        with self.store.connect() as db:db.executemany('INSERT INTO managed_media(key,parent,kind,body,updated) VALUES(?,?,?,?,?)',values)
        return [value[0] for value in values]

    def enable(self,field='all',value=True):
        engine.save_plan(self.app,self.store,self.item['id'],field,value)
        return self.plan()

    def plan(self):
        with self.store.connect() as db:return dict(db.execute('SELECT * FROM discover_series_plan WHERE parent=?',(self.item['id'],)).fetchone())

    def requests(self):
        with self.store.connect() as db:return [dict(row) for row in db.execute('SELECT * FROM discover_requests ORDER BY id')]

    def entries(self):
        with self.store.connect() as db:return db.execute('SELECT count(*) FROM discover_plan_entries').fetchone()[0]

    def run_plan(self,*,refresh=False):engine.apply_plan(self.app,self.store,self.plan(),refresh=refresh)

    def test_interrupted_legacy_eighty_any_states_and_five_completed_survive_reopen(self):
        keys=self.episodes(95)
        self.enable()
        before=[]
        for index,key in enumerate(keys[:80]):
            state=('failed','cancelled','complete','waiting','searching')[index%5]
            row,_=ds.insert_request(self.store,dict(id=f'original-{index}',idempotency=f'original-{index}',kind='episode',title='Original',quality='720',
                episode_key=key,state=state,body=dict(item=self.item,plan='all',planParent=self.item['id'])))
            before.append(row)
        for index,key in enumerate(keys[80:85]):
            ds.insert_request(self.store,dict(id=f'owned-{index}',idempotency=f'owned-{index}',kind='episode',title='Saved',quality='1080',episode_key=key,state='complete',body=dict(item=self.item)))
        with self.store.connect() as db:
            for trigger in ('discover_plan_insert','discover_plan_update','discover_plan_delete'):db.execute('DROP TRIGGER '+trigger)
            db.execute('DELETE FROM discover_plan_work')
            db.execute('UPDATE discover_series_plan SET checked=?',(time.time(),))
        self.store=Store(self.tmp.name)
        work.initialise(self.store)
        with patch.object(engine,'prepare') as prepare:self.run_plan(refresh=True)
        prepare.assert_not_called()
        self.assertEqual(len(self.requests()),95)
        after={row['id']:row for row in self.requests()}
        for row in before:self.assertEqual(after[row['id']],row)
        self.assertGreater(self.plan()['checked'],0)
        self.run_plan();self.assertEqual(len(self.requests()),95)

    def test_process_loss_after_insert_before_ledger_does_not_duplicate_failed_request(self):
        key=self.episodes()[0];self.enable()
        with patch.object(work,'consumed',side_effect=RuntimeError('simulated process loss')):
            with self.assertRaises(RuntimeError):self.run_plan()
        original=self.requests()[0];ds.update_request(self.store,original['id'],state='failed')
        before=self.requests()
        self.assertEqual(self.entries(),0)
        self.store=Store(self.tmp.name);self.run_plan(refresh=True)
        self.assertEqual(self.requests(),before);self.assertEqual(self.entries(),1)
        self.assertEqual(self.requests()[0]['episode_key'],key)

    def test_time_budget_returns_then_store_reopen_continues_more_than_one_batch(self):
        self.episodes(55);self.enable()
        real=engine.queue;clock=[0.0]
        def slow(*args,**kwargs):
            result=real(*args,**kwargs);clock[0]+=2
            return result
        with patch.object(work.time,'monotonic',side_effect=lambda:clock[0]),patch.object(engine,'queue',side_effect=slow):self.run_plan()
        self.assertEqual(len(self.requests()),3);self.assertEqual(self.plan()['checked'],0)
        self.store=Store(self.tmp.name);self.run_plan(refresh=True)
        self.assertEqual(len(self.requests()),55);self.assertGreater(self.plan()['checked'],0)

    def test_fast_large_cached_catalogue_does_not_stop_after_ten(self):
        self.episodes(1865);self.enable()
        with patch.object(engine,'queue') as queue:self.run_plan()
        self.assertGreater(queue.call_count,10)
        while work.pending_parent(self.store,service.today()):self.run_plan()
        self.assertEqual(self.entries(),1865);self.assertGreater(self.plan()['checked'],0)

    def test_legacy_checked_does_not_consume_unattempted_keys_and_reopen_retains_progress(self):
        old=self.episodes(3)
        # Recreate the pre-upgrade startup state, retaining the six-column plan.
        with self.store.connect() as db:
            for trigger in ('discover_plan_insert','discover_plan_update','discover_plan_delete'):db.execute('DROP TRIGGER '+trigger)
            db.execute('DELETE FROM discover_plan_work')
            db.execute('INSERT INTO discover_series_plan VALUES(?,?,1,0,?,?)',(self.item['id'],json.dumps(self.item),service.today(),time.time()))
        work.initialise(self.store)
        self.assertIsNotNone(work.pending_parent(self.store,service.today()));self.assertEqual(self.entries(),0)
        self.assertEqual(self.requests(),[])
        self.run_plan();before=self.requests();self.assertEqual(len(before),3)
        new=self.episodes(1,season=2004)[0]
        self.store=Store(self.tmp.name);work.initialise(self.store)
        self.assertIsNotNone(work.pending_parent(self.store,service.today()))
        self.run_plan(refresh=True)
        self.assertEqual({row['episode_key'] for row in self.requests()},set(old+[new]));self.assertEqual(self.entries(),4)
        after={row['id']:row for row in self.requests()}
        for row in before:self.assertEqual(after[row['id']],row)

    def test_all_cutoff_survives_future_edits_and_only_future_allows_newly_aired_keys(self):
        yesterday=datetime.now(ZoneInfo('Europe/London'))-timedelta(days=1)
        cutoff=yesterday.date().isoformat();earlier=(yesterday-timedelta(days=1)).date().isoformat()
        old=self.episodes(day=cutoff)[0];self.enable()
        with self.store.connect() as db:db.execute('UPDATE discover_plan_work SET all_before=?',(yesterday.timestamp(),))
        self.run_plan()
        fresh=self.episodes(season=2026,day=service.today())[0]
        late=self.episodes(season=2004,day=earlier)[0]
        self.store=Store(self.tmp.name);work.initialise(self.store);self.run_plan(refresh=True)
        self.assertEqual({row['episode_key'] for row in self.requests()},{old,late})
        self.enable('future');self.run_plan()
        self.assertEqual({row['episode_key'] for row in self.requests()},{old,late,fresh})
        self.enable('future',False)
        with self.store.connect() as db:self.assertEqual(work._all_day(db.execute('SELECT all_before FROM discover_plan_work').fetchone()[0]),cutoff)
        another=self.episodes(season=2026,start=2,day=service.today())[0]
        self.run_plan()
        self.assertNotIn(another,{row['episode_key'] for row in self.requests()})

    def test_all_reenable_renews_cutoff_but_metadata_edits_do_not(self):
        yesterday=datetime.now(ZoneInfo('Europe/London'))-timedelta(days=1)
        self.episodes(day=service.today());self.enable()
        with self.store.connect() as db:
            db.execute('UPDATE discover_plan_work SET all_before=?',(yesterday.timestamp(),))
            db.execute("UPDATE discover_series_plan SET item=json_set(item,'$.title','Updated title')")
        self.run_plan();self.assertEqual(self.requests(),[])
        self.enable('all',False);self.enable('all',True);self.run_plan()
        self.assertEqual(len(self.requests()),1)

    def test_legacy_cutoff_uses_latest_exact_all_date_caps_future_and_fallback_is_once(self):
        keys=self.episodes(2);self.enable()
        old=datetime(2020,1,1,tzinfo=ZoneInfo('Europe/London')).timestamp()
        newer=datetime(2021,2,3,tzinfo=ZoneInfo('Europe/London')).timestamp()
        ids=[]
        for index,key in enumerate(keys):
            row,_=ds.insert_request(self.store,dict(idempotency=f'old-{index}',kind='episode',title='Prior',quality='1080',episode_key=key,state='failed',
                body=dict(item=self.item,plan='all',planParent=self.item['id'])))
            ids.append(row['id'])
        def adopt():
            with self.store.connect() as db:db.execute('DELETE FROM discover_plan_work')
            work.initialise(self.store)
            with self.store.connect() as db:return db.execute('SELECT all_before FROM discover_plan_work').fetchone()[0]
        with self.store.connect() as db:
            db.execute('UPDATE discover_requests SET created=? WHERE id=?',(old,ids[0]))
            db.execute('UPDATE discover_requests SET created=? WHERE id=?',(newer,ids[1]))
        self.assertEqual(work._all_day(adopt()),'2021-02-03')
        with self.store.connect() as db:db.execute('UPDATE discover_requests SET created=? WHERE id=?',(time.time()+86400*100,ids[1]))
        self.assertEqual(work._all_day(adopt()),service.today())
        with self.store.connect() as db:db.execute('DELETE FROM discover_requests')
        fallback=adopt();self.assertEqual(work._all_day(fallback),service.today())
        with patch.object(work.time,'time',return_value=fallback+86400*100):work.initialise(self.store)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT all_before FROM discover_plan_work').fetchone()[0],fallback)

    def test_only_released_dated_numbered_rows_unless_future_is_separately_enabled(self):
        past=self.episodes()[0];future=self.episodes(season=2027,day='2099-01-01')[0]
        blank=self.episodes(season=2028,day='')[0];removed=self.episodes(season=2029)[0]
        direct=self.episodes(season=2030)[0]
        with self.store.connect() as db:
            db.execute('UPDATE managed_media SET removed=1 WHERE key=?',(removed,))
            db.execute("UPDATE managed_media SET body=json_set(body,'$.directTitleKey','') WHERE key=?",(past,))
            db.execute("UPDATE managed_media SET body=json_set(body,'$.directTitleKey','real:direct') WHERE key=?",(direct,))
        self.enable();self.run_plan()
        self.assertEqual([row['episode_key'] for row in self.requests()],[past])
        self.enable('future');self.run_plan()
        rows={row['episode_key']:row for row in self.requests()}
        self.assertEqual(set(rows),{past,future});self.assertEqual(rows[future]['state'],'waiting')
        self.assertNotIn(blank,rows)

    def test_same_plan_claim_coalesces_web_worker_and_does_not_prepare(self):
        self.episodes(12);self.enable();claim=work.claim(self.store,self.item['id'])
        other=Store(self.tmp.name)
        with patch.object(engine,'prepare') as prepare:engine.apply_plan(self.app,other,self.plan(),refresh=True)
        prepare.assert_not_called();self.assertEqual(self.requests(),[])
        work.release(other,claim);self.run_plan();self.assertEqual(len(self.requests()),12)

    def test_actual_competing_threads_only_one_owner_enqueues(self):
        self.episodes(14);self.enable();entered=threading.Event();release=threading.Event();errors=[];real=engine.queue
        def paused(*args,**kwargs):
            if not entered.is_set():entered.set();self.assertTrue(release.wait(5))
            return real(*args,**kwargs)
        def worker():
            try:
                with self.app.app_context():engine.apply_plan(self.app,Store(self.tmp.name),self.plan(),refresh=True)
            except BaseException as exc:errors.append(exc)
        with patch.object(engine,'queue',side_effect=paused),patch.object(engine,'prepare') as prepare:
            first=threading.Thread(target=worker);first.start();self.assertTrue(entered.wait(5))
            second=threading.Thread(target=worker);second.start();second.join(5);self.assertFalse(second.is_alive())
            release.set();first.join(10);self.assertFalse(first.is_alive())
        self.assertEqual(errors,[]);prepare.assert_not_called();self.assertEqual(len(self.requests()),14)

    def test_disable_reenable_inside_insert_fences_old_pass_and_old_completion(self):
        self.episodes(2);self.enable();real=ds.insert_request;once=[False]
        def raced(store,payload):
            if not once[0]:
                once[0]=True;self.enable('all',False);self.enable('all',True)
            return real(store,payload)
        with patch.object(ds,'insert_request',side_effect=raced):self.run_plan()
        self.assertEqual(self.requests(),[]);self.assertEqual(self.entries(),0);self.assertEqual(self.plan()['checked'],0)
        self.run_plan();self.assertEqual(len(self.requests()),2)

    def test_watchlist_writers_invalidate_existing_all_lease(self):
        self.episodes();self.enable();claim=work.claim(self.store,self.item['id'])
        with self.store.connect() as db:watchlist_policy.enable(db,self.item['id'],self.item)
        with self.store.connect() as db:self.assertFalse(work.valid_in(db,claim))
        self.assertFalse(self.plan()['download_all']);self.assertTrue(self.plan()['download_future'])
        replacement=work.claim(self.store,self.item['id']);self.assertIsNotNone(replacement)
        work.release(self.store,claim)
        with self.store.connect() as db:self.assertTrue(work.valid_in(db,replacement))
        with self.store.connect() as db:watchlist_policy.disable(db,self.item['id'])
        with self.store.connect() as db:self.assertFalse(work.valid_in(db,replacement))

    def test_global_stop_then_resume_inside_writer_does_not_enqueue_old_pass(self):
        self.episodes(2);self.enable();real=ds.insert_request;once=[False]
        def raced(store,payload):
            if not once[0]:once[0]=True;acquisition.begin_hold(store);acquisition.resume(store)
            return real(store,payload)
        with patch.object(ds,'insert_request',side_effect=raced):self.run_plan()
        self.assertEqual(self.requests(),[]);self.assertEqual(self.entries(),0);self.assertEqual(self.plan()['checked'],0)
        self.run_plan();self.assertEqual(len(self.requests()),2)

    def test_file_only_stop_and_reset_leave_work_pending(self):
        self.episodes();self.enable()
        acquisition._write_file(self.store,True,1)
        self.run_plan();self.assertEqual(self.requests(),[]);self.assertEqual(self.entries(),0)
        acquisition._write_file(self.store,False,1);self.store.set('reset_in_progress','1')
        self.run_plan();self.assertEqual(self.requests(),[]);self.assertEqual(self.entries(),0)
        self.store.set('reset_in_progress','0');self.run_plan();self.assertEqual(len(self.requests()),1)

    def test_file_hold_arriving_inside_insert_is_deferred_without_consuming(self):
        self.episodes();self.enable();real=ds.insert_request
        def raced(store,payload):
            acquisition._write_file(store,True,1)
            return real(store,payload)
        with patch.object(ds,'insert_request',side_effect=raced):self.run_plan()
        self.assertEqual(self.requests(),[]);self.assertEqual(self.entries(),0);self.assertEqual(self.plan()['checked'],0)
        acquisition._write_file(self.store,False,1);self.run_plan();self.assertEqual(len(self.requests()),1)

    def test_date_change_rejected_inside_writer_is_not_consumed(self):
        key=self.episodes()[0];self.enable();real=ds.insert_request
        def raced(store,payload):
            with store.connect() as db:db.execute("UPDATE managed_media SET body=json_set(body,'$.date','2099-01-01') WHERE key=?",(key,))
            return real(store,payload)
        with patch.object(ds,'insert_request',side_effect=raced):self.run_plan()
        self.assertEqual(self.requests(),[]);self.assertEqual(self.entries(),0);self.assertEqual(self.plan()['checked'],0)
        with self.store.connect() as db:db.execute("UPDATE managed_media SET body=json_set(body,'$.date','2020-01-01') WHERE key=?",(key,))
        self.run_plan();self.assertEqual(len(self.requests()),1)

    def test_clear_hold_is_consumed_without_releasing_it_or_requesting_media(self):
        from backend.media_holds import hold,blocked
        key=self.episodes()[0];self.enable()
        with self.store.connect() as db:db.execute('UPDATE managed_media SET hold=1 WHERE key=?',(key,))
        # The per-media durable hold is independent of section clearing.
        with self.store.connect() as db:hold(db,['managed:'+key],[])
        with patch('backend.media_holds.resume') as resume:self.run_plan()
        resume.assert_not_called();self.assertTrue(blocked(self.store,['managed:'+key]))
        self.assertEqual(self.requests(),[]);self.assertEqual(self.entries(),1);self.assertGreater(self.plan()['checked'],0)

    def test_expired_lease_recovers_but_stale_owner_cannot_insert_or_complete(self):
        key=self.episodes()[0];self.enable();old=work.claim(self.store,self.item['id']);old['all_work']=True
        with self.store.connect() as db:db.execute('UPDATE discover_plan_work SET lease_until=0')
        new=work.claim(self.store,self.item['id']);self.assertIsNotNone(new)
        with self.assertRaises(work.PlanDeferred):engine.queue(self.app,self.store,key,'all',plan_claim=old)
        self.assertFalse(work.finish(self.store,old,service.today(),complete=True));self.assertEqual(self.plan()['checked'],0)
        work.release(self.store,old)
        with self.store.connect() as db:self.assertTrue(work.valid_in(db,new))
        work.release(self.store,new);self.run_plan();self.assertEqual(len(self.requests()),1)

    def test_worker_tick_recovers_cache_without_provider_and_new_keys_after_completion(self):
        self.episodes();self.enable();tasks=[]
        self.schedule.side_effect=lambda app,store,key,fn:tasks.append((key,fn))
        with patch.object(engine,'sync_fallbacks'),patch.object(engine,'prepare') as prepare:
            engine.tick(self.app,self.store)
            [fn() for key,fn in tasks if key.startswith('plan:')]
        prepare.assert_not_called();self.assertEqual(len(self.requests()),1)
        original=self.requests()[0];ds.update_request(self.store,original['id'],state='failed');before=self.requests()[0]
        self.episodes(season=2004);tasks.clear()
        self.store=Store(self.tmp.name);work.initialise(self.store)
        with patch.object(engine,'sync_fallbacks'),patch.object(engine,'prepare') as prepare:
            engine.tick(self.app,self.store)
            [fn() for key,fn in tasks if key.startswith('plan:')]
        prepare.assert_not_called();self.assertEqual(len(self.requests()),2)
        self.assertEqual({row['id']:row for row in self.requests()}[before['id']],before)


if __name__=='__main__':unittest.main()
