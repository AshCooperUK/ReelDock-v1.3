"""Complete Activity reads keep one WAL frame while ordinary writers progress."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from backend.app import create_app
from backend import activity_clear, activity_observation, download_history

ORIGIN = 'http://127.0.0.1:8790'


class ActivityHistorySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(dict(TESTING=True, DATA_DIR=self.temp.name+'/data',
                                  MEDIA_DIR=self.temp.name+'/media'))
        self.store = self.app.extensions['store']
        self.media = self.app.config['MEDIA_DIR']
        self.store.set('public_origin', ORIGIN)
        self.store.set('setup_complete', '1')
        with self.store.connect() as db:
            db.execute("INSERT INTO users VALUES(1,'fixture','unused')")
        self.client = self.app.test_client()

    def login(self):
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session.update(user_id=1, csrf='fixture',
                           auth_revision=self.store.get('auth_revision','0'))

    def seed(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO downloads(id,title,quality,state,message,created,updated) VALUES('abcdefghijk','Saved video','1080','complete','Saved',1,1)")
            db.execute("INSERT INTO discover_requests(id,idempotency,kind,title,state,quality,fallback,body,message,progress,updated,created) VALUES('queued','queued','episode','Programme S01E01','waiting','1080',0,?,'Waiting',0,2,2)",
                       (json.dumps({'externalId':'81580','description':'Saved overview'}),))

    def dump(self):
        with self.store.connect() as db:
            return '\n'.join(db.iterdump())

    def test_complete_authenticated_cached_read_has_exact_rows_observations_and_no_writes(self):
        self.seed()
        self.assertEqual(self.client.get('/api/download-history/snapshot',base_url=ORIGIN).status_code,401)
        self.login()
        before = self.dump()
        with patch('backend.discover_sources.show_media',side_effect=AssertionError('No provider read')) as provider, \
             patch('backend.download_history.local_file',return_value=Path('/fixture/saved.mp4')) as local:
            response = self.client.get('/api/download-history/snapshot',base_url=ORIGIN)
        self.assertEqual(response.status_code,200)
        result = response.json
        self.assertEqual(result['total'],2)
        self.assertFalse(result['more'])
        self.assertEqual([row['item']['id'] for row in result['items']],['queued','abcdefghijk'])
        self.assertEqual([row['item']['id'] for row in result['active']],['queued'])
        self.assertEqual(result['items'][1]['item']['destination'],'/fixture/saved.mp4')
        self.assertEqual(result['items'][0]['item']['description'],'Saved overview')
        self.assertNotIn('posterChecked',result['items'][0]['item'])
        self.assertNotIn('externalId',result['items'][0]['item'])
        for row in result['items']:
            self.assertTrue(activity_observation.valid(row['item']['activity_observation']))
            self.assertEqual(row['item']['activity_observation']['state'],row['item']['state'])
        provider.assert_not_called()
        local.assert_called_once_with(self.store,self.media,'abcdefghijk')
        self.assertEqual(self.dump(),before)

    def test_insert_clear_state_generation_and_stop_hold_committed_mid_read_wait_for_next_frame(self):
        self.seed()
        with self.store.connect() as db:
            old_generation = activity_clear.generation(db,'discover','queued')
        build_row = download_history._history_row
        changed = False

        def writer():
            # This is a real Clear writer on its own connection while the GET
            # reader remains open. WAL permits it to commit without waiting.
            ack = activity_clear.clear(self.store,self.media,'completed')
            with self.store.connect() as db:
                db.execute("UPDATE discover_requests SET state='downloading',engine='nzb',engine_id='new-job',stop_generation=1 WHERE id='queued'")
                db.execute("INSERT INTO discover_requests(id,idempotency,kind,title,state,quality,fallback,body,message,progress,updated,created) VALUES('new','new','episode','Programme S01E02','waiting','1080',0,'{}','Waiting',0,3,3)")
                db.execute("INSERT INTO settings VALUES('acquisition_hold','1') ON CONFLICT(key) DO UPDATE SET value='1'")
            return ack

        def interleaved(db,key,enabled):
            nonlocal changed
            if not changed:
                changed = True
                with ThreadPoolExecutor(max_workers=1) as pool:
                    acknowledgement = pool.submit(writer).result(timeout=5)
                self.assertEqual(acknowledgement['cleared'],1)
                self.assertEqual(acknowledgement['activityRevision'],1)
            return build_row(db,key,enabled)

        with patch('backend.download_history._history_row',side_effect=interleaved), \
             patch('backend.download_history.local_file',return_value=None):
            before = download_history.snapshot(self.store,self.media)
        self.assertTrue(changed)
        self.assertEqual(before['total'],2)
        self.assertEqual({row['item']['id'] for row in before['items']},{'queued','abcdefghijk'})
        self.assertEqual(before['activityRevision'],0)
        self.assertFalse(before['acquisitionHold'])
        old = before['active'][0]['item']
        self.assertEqual(old['state'],'waiting')
        self.assertEqual(old['engine_id'],'')
        self.assertEqual(old['activity_observation']['generation'],old_generation)
        self.assertEqual(old['activity_observation']['state'],'waiting')
        after = download_history.snapshot(self.store,self.media)
        self.assertEqual(after['total'],2)
        self.assertEqual({row['item']['id'] for row in after['items']},{'queued','new'})
        self.assertEqual(after['activityRevision'],1)
        self.assertTrue(after['acquisitionHold'])
        current = next(row['item'] for row in after['items'] if row['item']['id']=='queued')
        self.assertEqual(current['state'],'downloading')
        self.assertEqual(current['engine_id'],'new-job')
        self.assertGreater(current['activity_observation']['generation'],old_generation)
        self.assertNotEqual(current['activity_observation']['attempt'],old['activity_observation']['attempt'])
        self.assertEqual(len(after['active']),2)

    def test_media_hold_visibility_and_native_clear_generations_share_the_same_snapshot(self):
        self.seed()
        build_row = download_history._history_row
        changed = False

        def interleaved(db,key,enabled):
            nonlocal changed
            if not changed:
                changed = True
                with self.store.connect() as writer:
                    writer.execute("INSERT INTO media_clear_holds(key,group_id,created) VALUES('fixture-key','fixture-group',5)")
                    writer.execute("INSERT INTO media_clear_rows(kind,identity,group_id) VALUES('discover','queued','fixture-group')")
                    writer.execute("INSERT INTO activity_hidden(kind,identity,generation,cleared) VALUES('nzb','native-job',4,5)")
                    writer.execute('UPDATE activity_clear_revision SET revision=revision+1 WHERE id=1')
            return build_row(db,key,enabled)

        with patch('backend.download_history._history_row',side_effect=interleaved):
            before = download_history.snapshot(self.store,self.media)
        self.assertEqual(before['suppressedActivityIds'],{})
        self.assertEqual(before['clearedEngineGenerations']['nzb'],{})
        self.assertEqual(before['activityRevision'],0)
        self.assertEqual(len(before['active']),1)
        after = download_history.snapshot(self.store,self.media)
        self.assertEqual(after['suppressedActivityIds'],{'discover':['queued']})
        self.assertEqual(after['clearedEngineGenerations']['nzb'],{'native-job':4})
        self.assertEqual(after['activityRevision'],1)
        self.assertEqual(after['total'],1)
        self.assertEqual(after['active'],[])

    def test_five_thousand_is_complete_and_over_cap_is_explicit_without_serialization(self):
        with self.store.connect() as db:
            db.executemany("INSERT INTO activity_events(created,category,title,state,message) VALUES(?,'Fixture','Event','complete','Saved')",[(i,) for i in range(5_000)])
        result = download_history.snapshot(self.store,self.media)
        self.assertEqual(result['total'],5_000)
        self.assertEqual(len(result['items']),5_000)
        self.assertFalse(result['more'])
        with self.store.connect() as db:
            db.execute("INSERT INTO activity_events(created,category,title,state,message) VALUES(5000,'Fixture','Event','complete','Saved')")
        self.login()
        with patch('backend.download_history._history_row',side_effect=AssertionError('Over-cap reads must not serialize a truncated list')):
            response = self.client.get('/api/download-history/snapshot',base_url=ORIGIN)
        self.assertEqual(response.status_code,400)
        self.assertIn('more than 5,000',response.json['error'])
        self.assertNotIn('items',response.json)


if __name__ == '__main__':
    unittest.main()
