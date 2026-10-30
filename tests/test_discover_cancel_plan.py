"""Reproduce the episode cross with a parent plan, retries and a reopened store."""
import json
import unittest
from unittest.mock import Mock, patch

import test_view_media as vm
from backend import discovery_engine as engine, discover_store as ds
from backend.media_management import store as records
from backend.storage import Store


class CancelPlanTests(unittest.TestCase):
    setUp = vm.ViewMediaTests.setUp
    tearDown = vm.ViewMediaTests.tearDown

    def prepare(self):
        item = dict(id='show:453660', kind='show', title='The Celebrity Traitors', externalId='453660')
        for number in (5, 6):
            records.remember(self.store, f'show:453660:2:{number}', item['id'], 'episode',
                             dict(item=item, episode=dict(id=number, season=2, number=number, name=f'Episode {number}'),
                                  date='2099-01-01', automatic=True))
        with patch.object(engine, 'schedule'):
            engine.save_plan(self.app, self.store, item['id'], 'future', True)
            engine.queue(self.app, self.store, 'show:453660:2:5', 'future')
        with self.store.connect() as db:
            plan = dict(db.execute('SELECT * FROM discover_series_plan').fetchone())
            request = dict(db.execute('SELECT * FROM discover_requests').fetchone())
        return item, plan, request

    def cross(self, request):
        return self.client.post('/api/discover/requests/cancel', base_url=vm.ORIGIN,
                                json={'id': request['id']}, headers={'Origin': vm.ORIGIN, 'X-CSRF-Token': 'token'})

    def test_cross_stays_cancelled_through_plan_refresh_and_restart(self):
        item, plan, request = self.prepare()
        self.assertEqual(self.cross(request).status_code, 200)
        self.assertEqual(records.row(self.store, request['episode_key'])['hold'], 1)
        self.store = Store(self.tmp.name)
        with patch.object(engine, 'prepare'), patch.object(engine, 'schedule'):
            for _ in range(3):
                engine.apply_plan(self.app, self.store, plan, refresh=True)
        with self.store.connect() as db:
            states = list(db.execute('SELECT state FROM discover_requests WHERE episode_key=?', (request['episode_key'],)))
            self.assertEqual([r[0] for r in states], ['cancelled'])
            self.assertEqual(db.execute('SELECT download_future FROM discover_series_plan').fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM discover_requests WHERE episode_key='show:453660:2:6'").fetchone()[0], 1)
        entry = next(r for r in engine.title_data(self.app, self.store, item)['items'] if r['number'] == 5)
        self.assertFalse(entry['requested'])
        with patch.object(engine, 'schedule'):
            engine.queue(self.app, self.store, request['episode_key'])
        self.assertEqual(records.row(self.store, request['episode_key'])['hold'], 0)
        self.assertEqual(len([r for r in ds.public_requests(self.store) if r['episodeKey'] == request['episode_key']]), 2)

    def test_pre_fix_cancellation_without_hold_is_not_recreated(self):
        _, plan, request = self.prepare()
        self.cross(request)
        with self.store.connect() as db:
            db.execute('UPDATE managed_media SET hold=0 WHERE key=?', (request['episode_key'],))
        with patch.object(engine, 'prepare'), patch.object(engine, 'schedule'):
            engine.apply_plan(self.app, self.store, plan, refresh=True)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM discover_requests WHERE episode_key=?', (request['episode_key'],)).fetchone()[0], 1)

    def test_cross_wins_against_a_plan_already_preparing_an_insert(self):
        _, _, request = self.prepare()
        def stale_read(store, key):
            self.assertEqual(self.cross(request).status_code, 200)
            return None
        with patch.object(ds, 'open_request_for', side_effect=stale_read), patch.object(engine, 'schedule') as schedule:
            engine.queue(self.app, self.store, request['episode_key'], 'future')
        schedule.assert_not_called()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM discover_requests').fetchone()[0], 1)

    def test_cross_removes_only_its_engine_job_and_keeps_csrf(self):
        _, _, request = self.prepare()
        ds.update_request(self.store, request['id'], state='queued', engine='nzb', engine_id='owned-job')
        downloader = Mock()
        with patch('backend.acquisition._engine', return_value=downloader):
            self.assertEqual(self.cross(request).status_code, 200)
        downloader.remove.assert_called_once_with('owned-job', timeout=20)
        self.assertEqual(self.client.post('/api/discover/requests/cancel', base_url=vm.ORIGIN, json={'id': request['id']}).status_code, 403)
