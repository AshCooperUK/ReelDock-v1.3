"""Forecasts use synthetic histories, including held-out weeks and clock changes."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo
from backend.storage import Store
from backend.catalogue import initialise
from backend.forecasts import rebuild

ZONE=ZoneInfo('Europe/London')

class ForecastTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name)
        initialise(self.store)
        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES (?,?,1)',('channel',json.dumps({'id':'channel'})))
        self.now=datetime(2026,9,25,12,tzinfo=ZONE)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self,moment,state='published',eligible=True,index=None):
        key=str(index or moment.isoformat())
        item={'id':key,'channel':'channel','date':moment.date().isoformat(),'time':moment.strftime('%H:%M'),
              'state':state,'forecast_eligible':eligible}
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO videos VALUES (?,?,?,?,?,0)',(key,'channel',item['date'],moment.isoformat(),json.dumps(item)))

    def weekly(self,count=22,skip=(),hour=20):
        last=datetime(2026,9,24,hour,tzinfo=ZONE)
        for i in range(count):
            if i not in skip:self.add(last-timedelta(weeks=i))

    def events(self):
        rebuild(self.store,self.now)
        with self.store.connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT data FROM forecasts ORDER BY day')]

    def test_twenty_two_thursdays_predict_next_thursday_at_eight(self):
        self.weekly()
        events=self.events()
        self.assertTrue(events)
        self.assertEqual(events[0]['date'],'2026-10-01')
        self.assertEqual(events[0]['time'],'20:00')
        self.assertEqual(events[0]['confidence'],'High')
        self.assertIn('22 matching uploads',events[0]['description'])
        self.assertEqual(events[0]['state'],'expected')
        self.assertEqual(events[0]['thumbnail'],'')
        self.assertNotIn('video_id',events[0])

    def test_sparse_and_irregular_histories_do_not_invent_schedule(self):
        self.weekly(count=5)
        self.assertEqual(self.events(),[])
        with self.store.connect() as db:db.execute('DELETE FROM videos')
        for i in range(22):self.add(self.now-timedelta(days=i*5+1,hours=(i*7)%24))
        self.assertEqual(self.events(),[])

    def test_missing_recent_weeks_suppress_old_pattern(self):
        self.weekly(skip=(0,1,2))
        self.assertEqual(self.events(),[])

    def test_real_scheduled_video_replaces_expected_slot(self):
        self.weekly()
        self.add(datetime(2026,10,1,20,tzinfo=ZONE),'scheduled')
        self.assertFalse(any(e['date']=='2026-10-01' for e in self.events()))

    def test_live_history_is_not_used_to_predict_regular_uploads(self):
        for i in range(22):self.add(datetime(2026,9,24,20,tzinfo=ZONE)-timedelta(weeks=i),eligible=False)
        self.assertEqual(self.events(),[])

    def test_recent_time_shift_invalidates_eight_pm(self):
        self.weekly(skip=(0,1,2,3))
        for i in range(4):self.add(datetime(2026,9,24,10,tzinfo=ZONE)-timedelta(weeks=i))
        self.assertFalse(any(e['time']=='20:00' for e in self.events()))

    def test_local_clock_time_survives_autumn_dst_change(self):
        self.now=datetime(2026,10,23,12,tzinfo=ZONE)
        for i in range(22):self.add(datetime(2026,10,22,20,tzinfo=ZONE)-timedelta(weeks=i))
        event=self.events()[0]
        self.assertEqual(event['date'],'2026-10-29')
        self.assertEqual(event['time'],'20:00')
        self.assertTrue(event['event_at'].endswith('+00:00'))

    def test_rebuilding_is_idempotent(self):
        self.weekly()
        first=self.events()
        self.assertEqual(first,self.events())

    def invalid_dates(self):
        """Unknown upload placeholders and damaged legacy dates stay unknown."""
        with self.store.connect() as db:
            for index,published in enumerate(('', 'not-a-date', '2026-02-30T20:00:00Z', '2026-13-01T20:00:00+00:00')):
                key='undated-'+str(index)
                # Flat playlist entries intentionally have no publication date.
                # Also cover a damaged older row marked eligible by its importer.
                item=dict(id=key,channel='channel',title='Unverified upload',date='',time='',state='unknown' if index==0 else 'published',
                          metadata_pending=index==0,browse_only=index==0,forecast_eligible=index!=0)
                db.execute('INSERT INTO videos VALUES(?,?,?,?,?,0)',(key,'channel','',published,json.dumps(item)))

    def saved_video_rows(self):
        with self.store.connect() as db:
            return [tuple(row) for row in db.execute('SELECT * FROM videos ORDER BY id')]

    def test_unknown_and_malformed_dates_preserve_valid_forecast_without_fabricating_dates(self):
        self.weekly()
        expected=self.events()
        self.assertTrue(expected)
        self.invalid_dates()
        before=self.saved_video_rows()
        with patch('backend.youtube_fallback.extract',side_effect=AssertionError('Forecast rebuild must remain a saved-data calculation')):
            self.assertEqual(self.events(),expected)
        self.assertEqual(self.saved_video_rows(),before,'Forecasting must not stamp unknown dates or modify saved history')
        self.assertIn('22 matching uploads',expected[0]['description'],'Only valid dated history supports the cadence')

    def test_valid_scheduled_video_still_suppresses_its_forecast_among_undated_rows(self):
        self.weekly()
        self.add(datetime(2026,10,1,20,tzinfo=ZONE),'scheduled')
        expected=self.events()
        self.assertFalse(any(event['date']=='2026-10-01' for event in expected))
        self.assertTrue(any(event['date']=='2026-10-08' for event in expected))
        self.invalid_dates()
        before=self.saved_video_rows()
        self.assertEqual(self.events(),expected)
        self.assertEqual(self.saved_video_rows(),before)

    def test_import_batch_finishes_pending_forecasts_with_mixed_saved_dates(self):
        from backend.youtube_fallback import rebuild_forecasts_once
        self.weekly()
        expected=self.events()
        self.invalid_dates()
        before=self.saved_video_rows()
        self.store.set('forecasts_pending','1')
        # Keep the controller's actual rebuild path, using a fixed test clock.
        with patch('backend.forecasts.rebuild',side_effect=lambda store:rebuild(store,self.now)), \
             patch('backend.youtube_fallback.extract',side_effect=AssertionError('Finishing forecasts must not make a provider request')):
            rebuild_forecasts_once(self.store)
        self.assertEqual(self.store.get('forecasts_pending'),'0')
        with self.store.connect() as db:
            actual=[json.loads(row[0]) for row in db.execute('SELECT data FROM forecasts ORDER BY day')]
        self.assertEqual(actual,expected)
        self.assertEqual(self.saved_video_rows(),before)
