"""Viewed Guide demand shares the one public foreground lane, not backfill."""
import json
import threading
import time
import unittest
from datetime import timedelta
from unittest.mock import Mock,patch

import test_youtube_optional as fixture
from backend import guide_weeks as guide, youtube_fallback as fallback

CHANNEL=fixture.CHANNEL
OTHER='UC'+'b'*22
VIDEO='abcdefghijk'


class ForegroundGuideTests(unittest.TestCase):
    setUp=fixture.OptionalYouTubeTests.setUp
    tearDown=fixture.OptionalYouTubeTests.tearDown
    google=fixture.OptionalYouTubeTests.google
    extract=fixture.OptionalYouTubeTests.extract
    feed=fixture.OptionalYouTubeTests.feed
    xml=fixture.OptionalYouTubeTests.xml

    def setup_week(self):
        self.week=guide.current_week()-timedelta(days=14)
        with self.store.connect() as db:
            data=json.dumps(dict(id=CHANNEL,name='Channel',playlist='UU'+CHANNEL[2:]))
            db.execute('UPDATE channels SET data=? WHERE id=?',(data,CHANNEL))
        self.store.set('youtube_use_google_api','false')
        guide.request_week(self.store,self.week.isoformat(),foreground=True)

    def post(self,week=None,csrf='csrf-fixture'):
        return self.client.post('/api/guide/week',base_url=fixture.ORIGIN,json=dict(start=(week or self.week).isoformat()),headers={'Origin':fixture.ORIGIN,'X-CSRF-Token':csrf})

    def job(self,channel=CHANNEL,week=None):
        with self.store.connect() as db:return dict(db.execute('SELECT * FROM guide_week_jobs WHERE channel=? AND week=?',(channel,(week or self.week).isoformat())).fetchone())

    def facts(self,identity=VIDEO,channel=CHANNEL):
        return dict(id=identity,channel_id=channel,title='Exact Guide video',upload_date=self.week.strftime('%Y%m%d'),duration=47,width=1920,height=1080,live_status='not_live')

    def protected(self):
        with self.store.connect() as db:
            return {table:[tuple(row) for row in db.execute('SELECT * FROM '+table)] for table in ('downloads','download_rules','history_jobs','channel_tasks','discover_requests')}

    def test_explicit_view_intent_is_guarded_deduplicated_and_cached_get_makes_no_demand(self):
        self.setup_week()
        with patch.object(fallback,'extract',side_effect=AssertionError('No provider in a web request')):
            self.assertEqual(self.post(csrf='wrong').status_code,403)
            for _ in range(2):self.assertEqual(self.post().status_code,200)
            self.assertEqual(self.app.test_client().post('/api/guide/week',base_url=fixture.ORIGIN,json=dict(start=self.week.isoformat()),headers={'Origin':fixture.ORIGIN}).status_code,401)
            before=self.store.get('guide_view_demand')
            self.assertEqual(self.client.get('/api/guide',base_url=fixture.ORIGIN,query_string=dict(start=self.week.isoformat(),end=(self.week+timedelta(days=6)).isoformat())).status_code,200)
            self.assertEqual(self.store.get('guide_view_demand'),before)
        demand=json.loads(before)
        self.assertEqual(demand['week'],self.week.isoformat())
        self.assertLessEqual(demand['expires']-demand['requested'],300)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM guide_week_jobs').fetchone()[0],1)

    def test_actual_public_guide_extracts_while_normal_background_lock_is_held(self):
        self.setup_week();seen=[];completed=threading.Event();errors=[]
        before=self.protected()
        def hook(url,mode,start,end):
            seen.append((url,mode,start,end))
            return dict(entries=[self.facts()]) if mode=='playlist' else self.facts()
        self.app.config['YOUTUBE_EXTRACT']=hook
        def run():
            try:guide.batch(self.importer,1,foreground=True)
            except BaseException as exc:errors.append(exc)
            finally:completed.set()
        fallback._lock.acquire()
        try:
            with patch.object(fallback.time,'sleep',return_value=None):
                worker=threading.Thread(target=run,daemon=True);worker.start()
                self.assertTrue(completed.wait(3),'viewed Guide must not queue behind the normal extractor lock')
                worker.join(1)
            self.assertFalse(errors,errors)
            self.assertTrue(fallback._lock.locked())
        finally:fallback._lock.release()
        self.assertEqual([row[1] for row in seen],['playlist','video'])
        self.assertLessEqual(seen[0][3]-seen[0][2]+1,10)
        self.assertEqual(self.job()['state'],'complete')
        with self.store.connect() as db:
            row=json.loads(db.execute('SELECT data FROM videos WHERE id=?',(VIDEO,)).fetchone()[0])
        self.assertEqual(row['title'],'Exact Guide video')
        self.assertEqual(row['duration'],'0:47')
        self.assertEqual(row['date'],self.week.isoformat())
        self.assertEqual(self.protected(),before,'Guide viewing never queues downloads or changes automation/history')

    def test_one_rich_detail_per_turn_and_other_ids_stay_on_existing_hydration_queue(self):
        self.setup_week();ids=['guidevid001','guidevid002','guidevid003'];calls=[]
        def extract(importer,url,mode='video',*args,**kwargs):
            calls.append((url,mode,kwargs))
            return dict(entries=[self.facts(identity) for identity in ids]) if mode=='playlist' else self.facts(ids[0])
        with patch.object(fallback,'extract',side_effect=extract):self.assertTrue(guide.batch(self.importer,1,foreground=True))
        self.assertEqual([row[1] for row in calls],['playlist','video'])
        self.assertTrue(all(row[2]['foreground'] and row[2]['public'] and row[2]['guide'] for row in calls))
        with self.store.connect() as db:
            self.assertEqual([row[0] for row in db.execute('SELECT id FROM videos ORDER BY id')],ids[:1])
            self.assertEqual([row[0] for row in db.execute('SELECT id FROM youtube_push_events ORDER BY id')],ids)

    def test_latest_view_week_and_favourites_go_first_without_requeuing_fresh_jobs(self):
        self.setup_week()
        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES(?,?,1)',(OTHER,json.dumps(dict(id=OTHER,playlist='UU'+OTHER[2:]))))
            db.execute('INSERT INTO channel_favourites VALUES(?)',(OTHER,))
        latest=self.week+timedelta(days=7)
        guide.request_week(self.store,latest.isoformat(),foreground=True)
        urls=[]
        with patch.object(fallback,'extract',side_effect=lambda importer,url,*args,**kwargs:(urls.append(url),dict(entries=[]))[1]):
            guide.batch(self.importer,1,foreground=True)
        self.assertEqual(urls,['https://www.youtube.com/playlist?list=UU'+OTHER[2:]])
        self.assertEqual(self.job(OTHER,latest)['state'],'complete')
        self.assertEqual(self.job(CHANNEL,self.week)['state'],'queued')
        before=self.job(OTHER,latest)
        guide.request_week(self.store,latest.isoformat(),foreground=True)
        self.assertEqual(self.job(OTHER,latest),before,'fresh complete jobs retain state, cursor and checked time')
        guide.request_week(self.store,(guide.current_week()+timedelta(days=7)).isoformat(),foreground=True)
        self.assertEqual(self.store.get('guide_view_demand'),'','viewing the future retires the prior week demand')

    def test_failed_favourite_keeps_cooldown_but_does_not_block_other_channels(self):
        self.setup_week()
        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES(?,?,1)',(OTHER,json.dumps(dict(id=OTHER,playlist='UU'+OTHER[2:]))))
            db.execute('INSERT INTO channel_favourites VALUES(?)',(CHANNEL,))
        guide.request_week(self.store,self.week.isoformat(),foreground=True)
        with patch.object(fallback,'extract',side_effect=ValueError('private https://secret.invalid/token')):guide.batch(self.importer,1,foreground=True)
        deadline=self.job()['retry_at'];self.assertGreater(deadline,time.time())
        guide.request_week(self.store,self.week.isoformat(),foreground=True)
        self.assertEqual(self.job()['retry_at'],deadline)
        seen=[]
        with patch.object(fallback,'extract',side_effect=lambda importer,url,*args,**kwargs:(seen.append(url),dict(entries=[]))[1]):guide.batch(self.importer,1,foreground=True)
        self.assertEqual(seen,['https://www.youtube.com/playlist?list=UU'+OTHER[2:]])
        self.assertNotIn('secret',self.job()['error'])

    def test_normal_and_viewed_pages_share_fb_cursor_and_claim(self):
        self.setup_week()
        with self.store.connect() as db:db.execute("UPDATE guide_week_jobs SET cursor='fb:10'")
        with patch.object(fallback,'extract',return_value=dict(entries=[])) as extract:
            guide.batch(self.importer,1,foreground=True)
        self.assertEqual(extract.call_args.args[3:5],(11,20))
        with self.store.connect() as db:
            db.execute("UPDATE guide_week_jobs SET state='queued',cursor='fb:20'")
            db.execute("DELETE FROM settings WHERE key='guide_view_demand'")
        with patch.object(self.importer,'api',return_value=dict(items=[])) as api:guide.batch(self.importer,1)
        self.assertEqual(api.call_args.kwargs['pageToken'],'fb:20')
        self.assertEqual(self.job()['state'],'complete')

    def test_legacy_opaque_cursor_keeps_normal_lane_without_foreground_backoff(self):
        self.setup_week()
        with self.store.connect() as db:db.execute("UPDATE guide_week_jobs SET cursor='opaque-old-page'")
        with patch.object(fallback,'extract',side_effect=AssertionError('Opaque API progress must not be reset by public paging')):
            self.assertFalse(guide.batch(self.importer,1,foreground=True))
        self.assertEqual((self.job()['cursor'],self.job()['retry_at']),('opaque-old-page',0))
        with patch.object(self.importer,'api',return_value=dict(items=[])) as api:guide.batch(self.importer,1)
        self.assertEqual(api.call_args.kwargs['pageToken'],'opaque-old-page')
        self.assertEqual(self.job()['state'],'complete')

    def test_expired_or_superseded_claim_cannot_commit_or_publish_stale_page(self):
        self.setup_week();started=threading.Event();release=threading.Event();errors=[]
        def extract(*args,**kwargs):
            started.set();self.assertTrue(release.wait(3));return dict(entries=[self.facts()])
        def run():
            try:guide.batch(self.importer,1,foreground=True)
            except BaseException as exc:errors.append(exc)
        with patch.object(fallback,'extract',side_effect=extract):
            thread=threading.Thread(target=run,daemon=True);thread.start();self.assertTrue(started.wait(2))
            with patch.object(self.importer,'api',side_effect=AssertionError('A claimed Guide job cannot run twice')):
                self.assertFalse(guide.batch(self.importer,1))
            with self.store.connect() as db:
                db.execute("UPDATE guide_week_jobs SET cursor='fb:40',claim='new-worker',lease_until=?",(time.time()+120,))
            release.set();thread.join(3)
        self.assertFalse(thread.is_alive());self.assertFalse(errors,errors)
        self.assertEqual((self.job()['cursor'],self.job()['claim']),('fb:40','new-worker'))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM videos').fetchone()[0],0)

    def test_recent_scan_completing_same_cursor_supersedes_held_foreground_claim(self):
        self.setup_week()
        def extract(*args,**kwargs):
            with self.store.connect() as db:db.execute("UPDATE guide_week_jobs SET state='complete',updated=123 WHERE channel=?",(CHANNEL,))
            return dict(entries=[self.facts()])
        with patch.object(fallback,'extract',side_effect=extract) as call:guide.batch(self.importer,1,foreground=True)
        self.assertEqual(call.call_count,1)
        self.assertEqual((self.job()['state'],self.job()['updated']),('complete',123))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM videos').fetchone()[0],0)

    def test_expired_view_and_incomplete_setup_do_not_start_a_priority_read(self):
        self.setup_week()
        with self.store.connect() as db:db.execute("UPDATE settings SET value=? WHERE key='guide_view_demand'",(json.dumps(dict(week=self.week.isoformat(),expires=0)),))
        with patch.object(fallback,'extract',side_effect=AssertionError('Expired Guide demand must wait for ordinary background work')):
            self.assertFalse(guide.batch(self.importer,1,foreground=True))
        guide.request_week(self.store,self.week.isoformat(),foreground=True)
        self.store.set('setup_complete','')
        with patch.object(fallback,'extract',side_effect=AssertionError('Setup is not complete')):
            self.assertFalse(guide.batch(self.importer,1,foreground=True))

    def test_atomic_publication_guard_rechecks_channel_visibility(self):
        self.setup_week()
        with self.store.connect() as db:before_art=[tuple(row) for row in db.execute('SELECT * FROM artwork')]
        real=self.importer.save_videos
        def save(items,**options):
            with self.store.connect() as db:db.execute('INSERT OR REPLACE INTO channel_preferences VALUES(?,0)',(CHANNEL,))
            real(items,**options)
        with patch.object(fallback,'extract',side_effect=[dict(entries=[self.facts()]),self.facts()]),patch.object(self.importer,'save_videos',side_effect=save):
            guide.batch(self.importer,1,foreground=True)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM videos').fetchone()[0],0)
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM artwork')],before_art,'cancelled guarded publication cannot insert late artwork either')
        self.assertEqual(self.job()['state'],'queued')
        self.assertEqual(self.job()['retry_at'],0,'a cancelled visibility change is not a provider failure')

    def test_stop_pause_and_reset_after_waiting_for_foreground_slot_prevent_extractor(self):
        self.setup_week()
        for flag in ('stop','pause','reset'):
            with self.subTest(flag=flag):
                stop=threading.Event();checked=threading.Event();errors=[]
                original=guide._valid
                def valid(*args,**kwargs):checked.set();return original(*args,**kwargs)
                def run():
                    try:guide.batch(self.importer,1,foreground=True,stop=stop)
                    except BaseException as exc:errors.append(exc)
                fallback._foreground_lock.acquire()
                try:
                    with patch.object(guide,'_valid',side_effect=valid),patch.dict(self.app.config,{'YOUTUBE_EXTRACT':Mock(side_effect=AssertionError('Cancelled extraction must not start'))}):
                        thread=threading.Thread(target=run,daemon=True);thread.start();self.assertTrue(checked.wait(2))
                        if flag=='stop':stop.set()
                        else:self.store.set('youtube_sync_paused' if flag=='pause' else 'reset_in_progress','true' if flag=='pause' else '1')
                        fallback._foreground_lock.release();thread.join(3)
                        self.assertFalse(thread.is_alive());self.app.config['YOUTUBE_EXTRACT'].assert_not_called()
                finally:
                    if fallback._foreground_lock.locked():fallback._foreground_lock.release()
                    self.store.set('youtube_sync_paused','false');self.store.set('reset_in_progress','0')
                self.assertFalse(errors,errors)
                self.assertEqual(self.job()['retry_at'],0)

    def test_thirty_second_turn_does_not_start_another_video_after_budget_is_spent(self):
        self.setup_week();clock=[100.0]
        def extract(*args,**kwargs):clock[0]=129.5;return dict(entries=[self.facts()])
        with patch.object(guide.time,'monotonic',side_effect=lambda:clock[0]),patch.object(fallback,'extract',side_effect=extract) as call:
            guide.batch(self.importer,1,foreground=True)
        self.assertEqual(call.call_count,1)
        self.assertLessEqual(call.call_args.kwargs['timeout'],30)


if __name__=='__main__':unittest.main()
