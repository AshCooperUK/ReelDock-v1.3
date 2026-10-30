"""Read-only per-channel history and forecast endpoints."""
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import test_guide as fixture
from backend import channel_details

ORIGIN = fixture.ORIGIN
A = 'UC' + 'a' * 22
B = 'UC' + 'b' * 22
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=ZoneInfo('Europe/London'))


class ChannelDetailTests(unittest.TestCase):
    setUp = fixture.GuideTests.setUp
    tearDown = fixture.GuideTests.tearDown
    google = fixture.GuideTests.google

    def get(self, path, client=None):
        return (client or self.client).get(path, base_url=ORIGIN)

    def seed_history(self):
        with self.store.connect() as db:
            for channel in (A, B):
                db.execute('INSERT OR REPLACE INTO channels VALUES(?,?,1)', (channel, json.dumps(dict(id=channel, name=channel))))
                for n in range(3):
                    vid = f'{channel[2:4]}video{n:04d}'.ljust(11, 'x')[:11]
                    db.execute('INSERT INTO videos VALUES(?,?,?,?,?,0)', (vid, channel, '2026-10-01', '2026-10-01T10:00:00Z', json.dumps(dict(id=vid, channel=channel, title='Same title'))))
                    db.execute('INSERT INTO downloads(id,title,quality,state,progress,message,created,updated) VALUES(?,?,?,?,?,?,?,?)', (vid, 'Same title', '1080', 'complete', 100, 'Saved apikey=SECRET', 1.0, 100.0 + n))
            va = 'aavideo0000'[:11]
            db.execute('INSERT INTO managed_history(media_key,message,created) VALUES(?,?,?)', ('video:' + va, 'Renamed https://example.com/x?token=1', 500.0))
            db.execute('INSERT INTO managed_history(media_key,message,created) VALUES(?,?,?)', ('youtube:video:' + va, 'Saved again', 400.0))
            db.execute('INSERT INTO managed_history(media_key,message,created) VALUES(?,?,?)', ('video:unrelated', 'Not this channel', 450.0))
            db.execute('INSERT OR REPLACE INTO channel_tasks(channel,action,state,message,created) VALUES(?,?,?,?,?)', (A, 'download_pending', 'complete', 'Queued 2 videos', 600.0))
            db.execute('INSERT OR REPLACE INTO channel_tasks(channel,action,state,message,created) VALUES(?,?,?,?,?)', (B, 'deep_scan', 'running', 'Other channel', 700.0))

    def tables(self):
        with self.store.connect() as db:
            return {name: db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0] for name in ('channels', 'videos', 'downloads', 'managed_history', 'channel_tasks', 'forecasts', 'download_rules', 'settings')}

    def test_history_is_scoped_ordered_paged_redacted_and_read_only(self):
        self.seed_history()
        before = self.tables()
        first = self.get(f'/api/guide/channel/{A}/history?limit=4&offset=0')
        self.assertEqual(first.status_code, 200, first.json)
        body = first.json
        self.assertEqual(body['total'], 6)
        self.assertEqual([i['kind'] for i in body['items']], ['channel-task', 'management', 'management', 'download'])
        self.assertEqual(body['items'][0]['id'], 'channel:' + A)
        self.assertTrue(body['more'])
        self.assertTrue(all(i['id'] for i in body['items']))
        second = self.get(f'/api/guide/channel/{A}/history?limit=4&offset=4').json
        self.assertEqual(len(second['items']), 2)
        self.assertFalse(second['more'])
        ids = [i['id'] for i in body['items'] + second['items']]
        self.assertEqual(len(ids), len(set(ids)), 'pages never repeat an event')
        text = json.dumps(body) + json.dumps(second)
        self.assertNotIn('SECRET', text)
        self.assertNotIn('example.com', text)
        self.assertNotIn('Not this channel', text)
        self.assertNotIn('Other channel', text)
        self.assertEqual(self.tables(), before, 'read-only')
        self.assertFalse(self.calls, 'no provider requests')

    def test_history_includes_download_and_both_management_key_forms(self):
        self.seed_history()
        items = self.get(f'/api/guide/channel/{A}/history?limit=50').json['items']
        self.assertEqual(sorted(i['kind'] for i in items), ['channel-task', 'download', 'download', 'download', 'management', 'management'])
        self.assertEqual(sum(1 for i in items if i['id'].startswith('management:')), 2)

    def test_history_other_channel_excluded_and_unknown_channel_is_empty(self):
        self.seed_history()
        other = self.get(f'/api/guide/channel/{B}/history').json
        self.assertTrue(all('Not this channel' not in i['message'] for i in other['items']))
        self.assertEqual(other['total'], 4)
        unknown = self.get('/api/guide/channel/UC' + 'z' * 22 + '/history').json
        self.assertEqual(unknown, dict(items=[], total=0, more=False))

    def test_history_validates_channel_limit_offset_and_authentication(self):
        for bad in ('bad', 'UC' + 'a' * 21, 'UC' + 'a' * 23, 'XX' + 'a' * 22):
            self.assertEqual(self.get(f'/api/guide/channel/{bad}/history').status_code, 400, bad)
            self.assertEqual(self.get(f'/api/guide/channel/{bad}/forecasts').status_code, 400, bad)
        for query in ('limit=0', 'limit=51', 'limit=x', 'offset=-1', 'offset=1000001', 'offset=x', 'limit=1.5'):
            self.assertEqual(self.get(f'/api/guide/channel/{A}/history?{query}').status_code, 400, query)
        anonymous = self.app.test_client()
        for suffix in ('history', 'forecasts'):
            self.assertIn(self.get(f'/api/guide/channel/{A}/{suffix}', anonymous).status_code, (401, 403))

    # Forecasts
    def forecast(self, ident='f1', channel=A, day=None, clock='15:00', state='expected', ends=None, **extra):
        day = day or (NOW.date() + timedelta(days=2)).isoformat()
        ends = ends or (NOW + timedelta(days=2, hours=3)).isoformat()
        row = dict(id=ident, channel=channel, date=day, time=clock, title='Expected upload', state=state, window_end=ends,
                   description='High confidence based on 5 matching uploads. https://example.com/hidden', confidence='High', duration='', thumbnail='', event_at=ends)
        row.update(extra)
        return day, row

    def put_forecast(self, ident='f1', **kwargs):
        day, row = self.forecast(ident, **kwargs)
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO channels VALUES(?,?,1)', (row['channel'], json.dumps(dict(id=row['channel'], name='C'))))
            db.execute('INSERT OR REPLACE INTO forecasts VALUES(?,?,?,?)', (ident, row['channel'], day, json.dumps(row)))
        return row

    def run_forecasts(self, channel=A, built=None):
        self.store.set('forecasts_built', str(built if built is not None else NOW.timestamp() - 3600))
        return channel_details.forecasts(self.store, channel, now=NOW)['items']

    def test_fresh_expected_forecast_is_listed_with_safe_fields_only(self):
        self.put_forecast()
        items = self.run_forecasts()
        self.assertEqual([i['id'] for i in items], ['f1'])
        self.assertEqual(set(items[0]), {'id', 'channel', 'date', 'time', 'title', 'state', 'description', 'confidence'})
        self.assertNotIn('example.com', json.dumps(items))
        self.assertNotIn('videoId', json.dumps(items))

    def test_endpoint_is_read_only_and_does_not_rebuild(self):
        self.put_forecast()
        self.store.set('forecasts_built', str(time.time()))
        before = self.tables()
        with patch('backend.forecasts.rebuild', Mock()) as rebuild:
            response = self.get(f'/api/guide/channel/{A}/forecasts')
        self.assertEqual(response.status_code, 200)
        rebuild.assert_not_called()
        self.assertEqual(self.tables(), before)
        self.assertFalse(self.calls)

    def test_stale_expired_disabled_hidden_and_inactive_forecasts_are_excluded(self):
        self.put_forecast()
        self.assertEqual(self.run_forecasts(built=NOW.timestamp() - 49 * 3600), [], 'older than 48h')
        self.assertEqual(len(self.run_forecasts()), 1)
        self.put_forecast('expired', ends=(NOW - timedelta(hours=1)).isoformat())
        self.assertEqual([i['id'] for i in self.run_forecasts()], ['f1'])
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO channel_forecast_preferences VALUES(?,0)', (A,))
        self.assertEqual(self.run_forecasts(), [])
        with self.store.connect() as db:
            db.execute('DELETE FROM channel_forecast_preferences')
            db.execute('INSERT OR REPLACE INTO channel_preferences VALUES(?,0)', (A,))
        self.assertEqual(self.run_forecasts(), [])
        with self.store.connect() as db:
            db.execute('DELETE FROM channel_preferences')
            db.execute('UPDATE channels SET active=0 WHERE id=?', (A,))
        self.assertEqual(self.run_forecasts(), [])

    def test_other_channels_wrong_state_malformed_and_out_of_window_rows_are_ignored(self):
        self.put_forecast('other', channel=B)
        self.put_forecast('wrong-state', state='published')
        self.put_forecast('bad-clock', clock='25:99')
        self.put_forecast('naive-end', ends='2027-01-01T00:00:00')
        self.put_forecast('bad-end', ends='nonsense')
        self.put_forecast('far', day=(NOW.date() + timedelta(days=40)).isoformat())
        self.put_forecast('wrong-channel-in-row', channel=A, **{})
        with self.store.connect() as db:
            db.execute('INSERT INTO forecasts VALUES(?,?,?,?)', ('junk', A, NOW.date().isoformat(), '{not json'))
            db.execute('INSERT INTO forecasts VALUES(?,?,?,?)', ('listrow', A, NOW.date().isoformat(), '[1,2]'))
            row = json.loads(db.execute("SELECT data FROM forecasts WHERE id='wrong-channel-in-row'").fetchone()[0]); row['channel'] = B
            db.execute("UPDATE forecasts SET data=? WHERE id='wrong-channel-in-row'", (json.dumps(row),))
        self.assertEqual([i['id'] for i in self.run_forecasts()], [])
        self.put_forecast('good')
        self.assertEqual([i['id'] for i in self.run_forecasts()], ['good'])

    def test_real_video_near_the_same_time_suppresses_the_estimate(self):
        day = (NOW.date() + timedelta(days=2)).isoformat()
        self.put_forecast(ident='near', clock='15:00')
        self.put_forecast(ident='far-in-time', clock='22:00')
        self.put_forecast(ident='other-day', day=(NOW.date() + timedelta(days=3)).isoformat(), clock='15:00')
        with self.store.connect() as db:
            for vid, channel, clock in (('real0000001', A, '17:59'), ('real0000002', B, '20:00')):
                db.execute('INSERT INTO videos VALUES(?,?,?,?,?,0)', (vid, channel, day, day + 'T00:00:00Z', json.dumps(dict(id=vid, channel=channel, date=day, time=clock, title='Real'))))
            db.execute('INSERT INTO videos VALUES(?,?,?,?,?,0)', ('badvideo0001', A, day, day + 'T00:00:01Z', '{broken'))
            db.execute('INSERT INTO videos VALUES(?,?,?,?,?,0)', ('badclock0001', A, day, day + 'T00:00:02Z', json.dumps(dict(id='badclock0001', channel=A, date=day, time='soon'))))
        ids = sorted(i['id'] for i in self.run_forecasts())
        self.assertEqual(ids, ['far-in-time', 'other-day'], 'only the real video within 180 minutes on the same date suppresses; other channels and bad rows never do')

    def test_unexpired_boundary_forecast_uses_aware_utc_comparison(self):
        self.put_forecast('edge', ends=(NOW.astimezone(timezone.utc) + timedelta(minutes=1)).isoformat())
        self.assertEqual([i['id'] for i in self.run_forecasts()], ['edge'])


if __name__ == '__main__':
    unittest.main()
