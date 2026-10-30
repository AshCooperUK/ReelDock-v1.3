import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.app import create_app
from backend.at_a_glance import snapshot


class AtAGlanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = create_app(dict(TESTING=True, DATA_DIR=str(self.root / 'data'),
                                   MEDIA_DIR=str(self.root / 'media'), M3U_DIR=str(self.root / 'vod')))
        self.store = self.app.extensions['store']
        self.overview = dict(downloads_paused=False, worker_online=True, channels=2, favourites=1,
                             downloads={'complete': 3, 'queued': 1},
                             m3u=dict(playlists=1, movies=100, shows=None, episodes=200))

    def tearDown(self):
        self.tmp.cleanup()

    def result(self, **kwargs):
        return snapshot(self.app, self.store, self.overview, **kwargs)

    def test_dismissed_discover_failure_no_longer_flags_attention(self):
        from backend import discover_store, activity
        discover_store.initialise(self.store)
        row, _ = discover_store.insert_request(self.store, dict(
            idempotency='dismiss-fixture', kind='movie', title='Fixture',
            state='failed', quality='1080', body={}))
        self.assertEqual(self.result()['rows'][-1]['attention'], 1)
        key = activity.alert_id('discover-'+row['id'], 'The request failed.')
        with self.store.connect() as db:
            db.execute('INSERT INTO dismissed_alerts(id,dismissed) VALUES(?,?)', (key, 1))
        result = self.result()['rows'][-1]
        self.assertEqual(result['attention'], 0)
        self.assertNotIn('failed', result['detail'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM discover_requests WHERE id=?',(row['id'],)).fetchone()[0], 'failed')
        discover_store.update_request(self.store, row['id'], message='A new failure')
        self.assertEqual(self.result()['rows'][-1]['attention'], 1)

    def test_read_only_and_private(self):
        self.store.set('player_plex_connection', json.dumps(dict(url='http://private', secret='DO-NOT-EXPOSE')))
        with patch('backend.nzb._engine', side_effect=AssertionError('Engine startup')), patch('backend.torrents._engine', side_effect=AssertionError('Engine startup')):
            with self.store.connect() as db:
                before = list(db.execute('SELECT * FROM settings'))
            result = self.result()
            with self.store.connect() as db:
                self.assertEqual(before, list(db.execute('SELECT * FROM settings')))
        text = json.dumps(result)
        self.assertNotIn('DO-NOT-EXPOSE', text)
        self.assertNotIn('http://private', text)
        self.assertEqual(len(result['rows']), 8)
        self.assertIn('Unknown shows', result['rows'][1]['detail'])
        self.assertEqual(result['rows'][6]['status'], '1 configured')
        self.assertEqual(result['completed_today'], 0)  # Catalogue is not downloaded media.

    def test_live_engine_states_and_attention(self):
        jobs = [dict(state='downloading', peers=0, down_rate=0), dict(state='queued'),
                dict(state='choose'), dict(state='stopped', progress=1), dict(state='failed', error='private tracker')]
        self.app.extensions['torrent_engine'] = SimpleNamespace(stop=threading.Event(), jobs=lambda: jobs)
        row = self.result()['rows'][3]
        self.assertEqual(row['status'], 'Live')
        for label in ('1 waiting for transfer', '1 queued', '1 need placement', '1 completed', '1 failed'):
            self.assertIn(label, row['detail'])
        self.assertEqual(row['attention'], 2)
        self.assertNotIn('private tracker', json.dumps(row))

    def test_replaced_discover_failure_is_history_not_attention(self):
        from backend import discover_store
        discover_store.initialise(self.store)
        row, _ = discover_store.insert_request(self.store, dict(
            idempotency='replacement', kind='movie', title='Fixture (1994)',
            state='downloading', quality='1080', engine='nzb', engine_id='new',
            body={'externalId':'0111257'}))
        folder=Path(self.store.directory)/'nzb-engine'/'jobs'
        folder.mkdir(parents=True)
        data={'id':'old','name':'discover','save_as':'Fixture (1994)','phase':'failed',
              'lookup_identity':'tt0111257','error':'Missing article'}
        path=folder/'old.json';path.write_text(json.dumps(data))
        self.assertEqual(self.result()['rows'][2]['attention'],0)
        self.assertTrue(path.exists())
        self.app.extensions['nzb_engine']=SimpleNamespace(stop=threading.Event(),jobs=lambda:[dict(id='old',state='failed',error='Missing article')])
        self.assertEqual(self.result()['rows'][2]['attention'],0)
        data['name']='Manually added';path.write_text(json.dumps(data))
        self.assertEqual(self.result()['rows'][2]['attention'],1)
        data['name']='discover';path.write_text(json.dumps(data))
        discover_store.update_request(self.store,row['id'],state='failed')
        self.assertEqual(self.result()['rows'][2]['attention'],1)

    def test_saved_state_and_corruption_are_not_fake_zeroes(self):
        folder = self.root / 'data' / 'torrent-engine' / 'torrents'
        folder.mkdir(parents=True)
        state = folder / 'test.json'
        state.write_text(json.dumps(dict(phase='download', trackers=['secret'])))
        row = self.result()['rows'][3]
        self.assertEqual(row['status'], 'Saved state')
        self.assertIn('1 saved pending', row['detail'])
        self.store.set('acquisition_hold', '1')
        self.assertIn('1 paused', self.result()['rows'][3]['detail'])
        state.write_text('{')
        result = self.result()
        self.assertEqual(result['rows'][3]['status'], 'Unavailable')
        self.assertNotEqual(result['rows'][0]['status'], 'Unavailable')

    def test_today_uses_uk_midnight_not_rolling_24_hours(self):
        now = datetime(2026, 10, 10, 1, 30, tzinfo=ZoneInfo('Europe/London')).timestamp()
        midnight = now - 90 * 60
        with self.store.connect() as db:
            for index, (stamp, state) in enumerate([(midnight - 1, 'complete'), (midnight, 'complete'), (now, 'failed'), (now + 1, 'complete')]):
                db.execute('INSERT INTO downloads(id,title,quality,state,created,updated) VALUES(?,?,?,?,?,?)',
                           (str(index), 'Fixture', '1080', state, stamp, stamp))
        self.assertEqual(self.result(now=now)['completed_today'], 1)

    def test_disabled_checks_are_not_next_action(self):
        self.store.set('next_sync', '1900000000')
        self.assertEqual(self.result()['next']['at'], 1900000000)
        self.store.set('youtube_sync_paused', 'true')
        self.assertIsNone(self.result()['next'])

    def test_auto_attention_only_counts_current_failed_arrival(self):
        from backend.auto_downloads import _key, initialise
        initialise(self.store)
        folder = self.root / 'intake'
        folder.mkdir()
        path = folder / 'fixture.torrent'
        path.write_bytes(b'fixture')
        info = path.stat()
        key = _key(path.name, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino)
        with self.store.connect() as db:
            db.execute("INSERT INTO auto_downloads VALUES('old','old.torrent','torrent','failed','old failure',0)")
        with patch('backend.auto_downloads.watch_folder', return_value=folder):
            self.assertEqual(self.result()['rows'][4]['attention'], 0)
            with self.store.connect() as db:
                db.execute("INSERT INTO auto_downloads VALUES(?,?,'torrent','failed','fixture',0)", (key, path.name))
            self.assertEqual(self.result()['rows'][4]['attention'], 1)
            path.unlink()
            self.assertEqual(self.result()['rows'][4]['attention'], 0)

    def test_disabled_metadata_and_player_failure(self):
        self.store.set('m3u_share_metadata', 'false')
        self.store.set('player_plex_connection', '{"url":"http://private"}')
        self.store.set('player_plex_status', 'Could not finish. Check the connection, user and permissions; retry the action.')
        rows = self.result()['rows']
        self.assertIn('VOD checks off', rows[5]['detail'])
        self.assertNotIn('due for checks', rows[5]['detail'])
        self.assertEqual(rows[6]['attention'], 1)
        self.assertEqual(rows[2]['status'], 'Not configured')

    def test_public_dashboard_does_not_expose_summaries(self):
        result = self.app.test_client().get('/api/dashboard')
        self.assertNotIn('glance', result.json)

    def test_vod_latest_completed_update_counts(self):
        def line():return self.result()['rows'][1]['brief']
        self.assertIn('No completed update yet',line())
        with self.store.connect() as db:
            db.execute("INSERT INTO m3u_sources(id,name,url,options,folder) VALUES('source','Test','private','{}','test')")
            for finished,state,added,removed in [(1,'complete',100,3),(2,'complete',7,2),(3,'error',999,999)]:
                db.execute("INSERT INTO m3u_runs(source,finished,state,message,stats) VALUES('source',?,?,?,?)",
                           (finished,state,'private provider details',json.dumps(dict(added=added,removed=removed))))
        self.assertIn('\nLatest update: 7 added · 2 removed',line())
        self.assertNotIn('private',line())
        with self.store.connect() as db:
            db.execute("UPDATE m3u_runs SET stats=? WHERE finished=2",(json.dumps(dict(added=0,removed=0)),))
        self.assertIn('0 added · 0 removed',line())
        with self.store.connect() as db:db.execute("UPDATE m3u_runs SET stats='{}' WHERE finished=2")
        self.assertIn('counts unavailable',line())


    def test_vpn_row_requires_a_configured_profile_and_exposes_only_status(self):
        for value in ({}, {'configured':False, 'state':'Disconnected'}):
            with patch('backend.vpn.cached_status', return_value=value):
                self.assertFalse(any(row['id']=='vpn' for row in self.result()['rows']))
        value=dict(configured=True, connected=True, state='Connected', values=dict(downloads=True, metadata=False),
                   external_ip='PRIVATE', endpoint='PRIVATE', private_key='PRIVATE', username='PRIVATE')
        with patch('backend.vpn.cached_status', return_value=value):
            row=self.result()['rows'][-1]
        self.assertEqual(row, dict(id='vpn', title='VPN', target='settings:VPN', status='Connected', detail='', brief='', attention=0))
        self.assertNotIn('PRIVATE', json.dumps(row))
        value.update(connected=False, state='Blocked')
        with patch('backend.vpn.cached_status', return_value=value):
            self.assertEqual(self.result()['rows'][-1]['attention'], 1)
        value.update(state='Disconnected', values=dict(downloads=False, metadata=False))
        with patch('backend.vpn.cached_status', return_value=value):
            self.assertEqual(self.result()['rows'][-1]['attention'], 0)
