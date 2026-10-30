import json
import time
import unittest
from datetime import timedelta
from unittest.mock import patch
from test_guide import GuideTests, ORIGIN, CHANNEL
from backend import guide_weeks


class WeekTests(unittest.TestCase):
    tearDown=GuideTests.tearDown
    google=GuideTests.google
    guide_setup=GuideTests.setUp
    def setUp(self):
        self.guide_setup()
        self.week=guide_weeks.current_week()-timedelta(days=14)
        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES(?,?,1)',(CHANNEL,json.dumps(dict(id=CHANNEL,playlist='uploads',name='Channel'))))
        self.store.set('next_sync',time.time()+21600)

    def test_week_request_is_authenticated_csrf_protected_deduplicated_and_read_is_free(self):
        url='/api/guide/week';body={'start':self.week.isoformat()}
        self.assertEqual(self.app.test_client().post(url,base_url=ORIGIN,json=body,headers={'Origin':ORIGIN}).status_code,401)
        self.assertEqual(self.client.post(url,base_url=ORIGIN,json=body).status_code,403)
        headers={'Origin':ORIGIN,'X-CSRF-Token':'csrf-fixture'}
        for _ in range(2):self.assertEqual(self.client.post(url,base_url=ORIGIN,json=body,headers=headers).status_code,200)
        self.assertEqual(self.client.post(url,base_url=ORIGIN,json={'start':(self.week+timedelta(days=1)).isoformat()},headers=headers).status_code,400)
        response=self.client.get('/api/guide',base_url=ORIGIN,query_string={'start':body['start'],'end':(self.week+timedelta(days=6)).isoformat()})
        self.assertIn('Updating this week',response.json['notice']);self.assertEqual(self.calls,[])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM guide_week_jobs').fetchone()[0],1)

    def test_worker_fetches_only_requested_week_details_and_resumes_pages(self):
        guide_weeks.request_week(self.store,self.week.isoformat())
        first={'items':[{'contentDetails':{'videoId':'outside0001','videoPublishedAt':(self.week+timedelta(days=8)).isoformat()+'T12:00:00Z'}}],'nextPageToken':'page2'}
        second={'items':[{'contentDetails':{'videoId':'inside00001','videoPublishedAt':self.week.isoformat()+'T12:00:00Z'}}],'nextPageToken':'page3'}
        third={'items':[{'contentDetails':{'videoId':'older000001','videoPublishedAt':(self.week-timedelta(days=1)).isoformat()+'T12:00:00Z'}}],'nextPageToken':'page4'}
        with patch.object(self.importer,'api',side_effect=[first,second,{'items':[]},third]) as api:
            guide_weeks.batch(self.importer,limit=1)
            guide_weeks.batch(self.importer,limit=2)
        calls=api.call_args_list
        self.assertEqual([c.args[0] for c in calls],['playlistItems','playlistItems','videos','playlistItems'])
        self.assertEqual(calls[2].kwargs['id'],'inside00001')
        self.assertEqual(calls[1].kwargs['pageToken'],'page2')
        self.assertEqual(guide_weeks.status(self.store,self.week.isoformat()),'')
        guide_weeks.request_week(self.store,self.week.isoformat())
        with patch.object(self.importer,'api') as api:guide_weeks.batch(self.importer);api.assert_not_called()

    def test_future_weeks_and_quota_pause_make_no_provider_calls(self):
        guide_weeks.request_week(self.store,(guide_weeks.current_week()+timedelta(days=7)).isoformat())
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM guide_week_jobs').fetchone()[0],0)
        guide_weeks.request_week(self.store,self.week.isoformat())
        self.store.set('google_quota_retry_at',time.time()+3600)
        with patch.object(self.importer,'api',side_effect=ValueError('Fallback unavailable')) as api:
            guide_weeks.batch(self.importer);api.assert_called_once()
        self.assertIn('retries',guide_weeks.status(self.store,self.week.isoformat()))

    def test_failure_preserves_cursor_and_does_not_repeat_immediately(self):
        guide_weeks.request_week(self.store,self.week.isoformat())
        with patch.object(self.importer,'api',side_effect=ValueError('Failure')) as api:
            guide_weeks.batch(self.importer);guide_weeks.batch(self.importer)
            self.assertEqual(api.call_count,1)
        with self.store.connect() as db:
            job=db.execute('SELECT * FROM guide_week_jobs').fetchone()
            self.assertEqual(job['cursor'],'');self.assertGreater(job['retry_at'],time.time())

    def test_disabled_channels_do_not_seed_background_history(self):
        from backend.history import seed,batch
        seed(self.store,dict(id=CHANNEL,playlist='uploads'),{'nextPageToken':'page2'})
        with patch.object(self.importer,'api') as api:batch(self.importer);api.assert_not_called()

    def test_recent_page_is_reused_for_current_week_only(self):
        guide_weeks.seed_recent(self.store,dict(id=CHANNEL),{'items':[{'contentDetails':{'videoPublishedAt':(guide_weeks.current_week()-timedelta(days=1)).isoformat()+'T12:00:00Z'}}],'nextPageToken':'old'})
        with self.store.connect() as db:
            rows=db.execute('SELECT week,state FROM guide_week_jobs').fetchall()
            self.assertEqual([tuple(row) for row in rows],[(guide_weeks.current_week().isoformat(),'complete')])
        with patch.object(self.importer,'api') as api:guide_weeks.batch(self.importer);api.assert_not_called()

    def test_fallback_scan_is_not_repeated_after_half_an_hour(self):
        from backend.worker import tick
        self.store.set('next_sync','0');self.store.set('next_history',time.time()+86400)
        with patch.object(self.importer,'run') as run:
            tick(self.importer,download_artwork=False)
            self.assertGreater(float(self.store.get('next_sync')),time.time()+5*3600)
            tick(self.importer,download_artwork=False)
            self.assertEqual(run.call_count,1)

    def test_week_boundary_uses_london_time(self):
        listing={'items':[{'contentDetails':{'videoPublishedAt':'2026-09-27T23:30:00Z'}}]}
        self.assertEqual(guide_weeks.page_dates(listing)[0].isoformat(),'2026-09-28')

    def test_usage_ledger_counts_real_attempts_and_stops_during_quota_pause(self):
        self.store.set('google_tokens',json.dumps({'access_token':'test','received_at':time.time(),'expires_in':3600}))
        with patch.object(self.importer,'request',side_effect=ValueError('quotaExceeded')):
            for _ in range(2):
                with self.assertRaises(ValueError):self.importer.api('subscriptions')
        with self.store.connect() as db:
            row=db.execute('SELECT requests,units FROM youtube_api_usage').fetchone()
            self.assertEqual(tuple(row),(1,1))


del GuideTests
