"""Activity read DTOs preserve the raw attempt observed by an explicit Clear."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app import create_app
from backend import activity_clear, activity_observation, download_history

LOCAL='http://127.0.0.1:8790'


class ActivityObservationDtoTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.app=create_app({'TESTING':True,'DATA_DIR':str(root/'data'),'MEDIA_DIR':str(root/'media'),
            'M3U_DIR':str(root/'m3u'),'LOCAL_SETUP_URL':LOCAL,'CANDIDATE_URL':''})
        self.store=self.app.extensions['store'];self.media=self.app.config['MEDIA_DIR']
        self.store.set('setup_complete','1')
        with self.store.connect() as db:
            db.execute("INSERT INTO users VALUES(1,'fixture','unused-fixture-hash')")
            db.execute("INSERT INTO downloads(id,title,quality,state,message,created,updated) VALUES('abcdefghijk','Video','1080','queued','Original',1,1)")
            db.execute("INSERT INTO m3u_downloads(id,source,path,title,state,created,updated,message,destination) VALUES('vod-one','missing','one.strm','Server title','running',2,2,'Original','')")
            db.execute("INSERT INTO activity_events(id,created,category,title,state,message) VALUES(501,3,'Fixture','Event','failed','Original')")
            db.execute("INSERT INTO m3u_runs(source,finished,state,message,stats) VALUES('missing',4,'failed','Original','{}')")
            db.execute("INSERT INTO discover_requests(id,idempotency,kind,title,state,quality,fallback,body,engine,engine_id,stop_generation,updated,created) VALUES('request-one','request-one','movie','Movie','downloading','1080',0,'{}','nzb','old-native',3,5,5)")
        self.client=self.app.test_client()
        with self.client.session_transaction(base_url=LOCAL) as session:
            session.update(user_id=1,csrf='fixture-csrf',auth_revision=self.store.get('auth_revision','0'))

    def page(self,**kwargs):return download_history.page(self.store,self.media,plain=True,**kwargs)

    def test_normal_authenticated_history_has_exact_raw_states_and_sql_generations(self):
        with self.client.get('/api/download-history?limit=50&cached=1',base_url=LOCAL) as response:
            self.assertEqual(response.status_code,200);rows=response.json['items']
        self.assertEqual({row['kind'] for row in rows},{'youtube','m3u','event','m3u_sync','discover'})
        with self.store.connect() as db:
            for row in rows:
                item=row['item'];stamp=item['activity_observation']
                self.assertTrue(activity_observation.valid(stamp))
                self.assertEqual(stamp['state'],item['state'])
                self.assertEqual(stamp['generation'],activity_clear.generation(db,row['kind'],item['id']))
                self.assertEqual(len(stamp['attempt']),64)
                self.assertEqual(set(stamp),{'state','generation','attempt'})

    def test_off_page_active_rows_have_observations_without_loading_history_tail(self):
        data=self.page(limit=1)
        self.assertEqual(len(data['items']),1)
        self.assertEqual({row['kind'] for row in data['active']},{'youtube','m3u','discover'})
        for row in data['active']:
            self.assertTrue(activity_observation.valid(row['item']['activity_observation']))
            self.assertEqual(row['item']['activity_observation']['state'],row['item']['state'])

    def test_progress_and_artwork_do_not_change_observation_but_new_native_attempt_does(self):
        def observed():return next(row['item']['activity_observation'] for row in self.page()['items'] if row['kind']=='discover')
        before=observed()
        with self.store.connect() as db:
            db.execute("UPDATE discover_requests SET progress=53,updated=200,body=? WHERE id='request-one'",(json.dumps({'image':'/artwork/fixture','description':'Enriched'}),))
        self.assertEqual(observed(),before)
        with self.store.connect() as db:db.execute("UPDATE discover_requests SET engine_id='replacement-native',stop_generation=4 WHERE id='request-one'")
        after=observed()
        self.assertEqual(after['state'],before['state']);self.assertEqual(after['generation'],before['generation'])
        self.assertNotEqual(after['attempt'],before['attempt'])

    def test_archived_export_records_do_not_gain_actionable_observation(self):
        row=next(row for row in self.page()['items'] if row['kind']=='event')
        with self.store.connect() as db:activity_clear.archive_row(db,row,100)
        data=self.page(include_archived=True)
        archive=next(row for row in data['items'] if row['item'].get('archived'))
        self.assertNotIn('activity_observation',archive['item'])
        for row in data['items']:
            if not row['item'].get('archived'):self.assertTrue(activity_observation.valid(row['item']['activity_observation']))

    def test_native_dtos_stamp_actual_state_and_existing_generation_without_engine_startup(self):
        from backend.downloaders.nzb.engine import NzbEngine,_Job
        from backend.downloaders.torrent.engine import TorrentEngine,Job
        nzb=object.__new__(NzbEngine);nzb.global_paused=lambda:False
        nzb.config={'download_limit_bps':0,'max_active_downloads':2};nzb._servers=lambda:[]
        nzb_job=_Job('nzb-fixture','Native title',[],set(),phase='download',total_parts=10)
        nzb_job.activity_generation=89
        torrent=object.__new__(TorrentEngine);torrent.global_paused=lambda:False;torrent._activity_held=lambda *a,**k:False
        torrent_job=Job(torrent,b't'*20);torrent_job.activity_generation=90;torrent_job.io=True
        for kind,engine,job in [('nzb',nzb,nzb_job),('torrent',torrent,torrent_job)]:
            with self.subTest(kind=kind):
                original=engine._job_dict(job)
                self.assertEqual(original['state'],'downloading')
                stamp=original['activity_observation']
                self.assertTrue(activity_observation.valid(stamp));self.assertEqual(stamp['state'],original['state'])
                self.assertEqual(stamp['generation'],job.activity_generation)
                job.downloaded=200;job.done_parts=2
                self.assertEqual(engine._job_dict(job)['activity_observation'],stamp)
                job.user_paused=True
                if kind=='nzb':job.phase='paused'  # Existing NZB pause action updates both fields.
                self.assertEqual(engine._job_dict(job)['activity_observation']['state'],'paused')
                self.assertNotEqual(engine._job_dict(job)['activity_observation']['attempt'],stamp['attempt'])

    def test_api_requires_valid_observation_before_any_clear(self):
        headers={'Origin':LOCAL,'X-CSRF-Token':'fixture-csrf'}
        with patch('backend.activity_items.clear_one',side_effect=AssertionError('Invalid observation reached Clear')):
            for body in ({'kind':'youtube','id':'abcdefghijk'},
                         {'kind':'youtube','id':'abcdefghijk','observed':{}},
                         {'kind':'youtube','id':'abcdefghijk','observed':{'state':'queued','generation':-1,'attempt':'a'*64}}):
                with self.subTest(body=body),self.client.post('/api/activity/clear-item',base_url=LOCAL,json=body,headers=headers) as response:
                    self.assertEqual(response.status_code,400);self.assertIn('Refresh',response.json['error'])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM activity_archive').fetchone()[0],0)

    def test_saved_native_paused_and_seed_observations_match_actual_torrent_dto(self):
        from backend import activity_items
        from backend.downloaders.torrent.engine import TorrentEngine,Job
        from backend.downloaders.torrent.metainfo import parse_torrent
        from torrentutil import build_torrent
        raw=build_torrent([('Fixture.mkv',b'isolated-video')]);meta=parse_torrent(raw)
        folder=self.store.directory/'torrent-engine'/'torrents';folder.mkdir(parents=True)
        (folder/(meta.info_hash.hex()+'.torrent')).write_bytes(raw)
        engine=object.__new__(TorrentEngine);engine.global_paused=lambda:False;engine._activity_held=lambda *a,**k:False
        for phase,paused,expected in [('download',True,'paused'),('seed',False,'seeding')]:
            with self.subTest(phase=phase,paused=paused):
                payload={'selected':[0],'phase':phase,'user_paused':paused,'activity_generation':90}
                (folder/(meta.info_hash.hex()+'.json')).write_text(json.dumps(payload))
                job=Job(engine,meta.info_hash);job.phase=phase;job.user_paused=paused;job.activity_generation=90;job.io=True
                actual=engine._job_dict(job)
                with patch('backend.torrents._engine',side_effect=AssertionError('Saved observation opened an engine')):
                    saved=activity_items._saved_native(self.store,'torrent',job.id)
                self.assertIsNotNone(saved)
                self.assertEqual(saved['item']['state'],expected)
                self.assertEqual(activity_observation.capture('torrent',saved['item']),actual['activity_observation'])

    def test_api_preserves_conflict_status_and_exact_observed_payload(self):
        class Conflict(ValueError):status=409
        row=next(row for row in self.page()['items'] if row['kind']=='youtube');observed=row['item']['activity_observation']
        with patch('backend.activity_items.clear_one',side_effect=Conflict('Activity changed; refresh first.')) as clear:
            with self.client.post('/api/activity/clear-item',base_url=LOCAL,headers={'Origin':LOCAL,'X-CSRF-Token':'fixture-csrf'},json={'kind':'youtube','id':'abcdefghijk','observed':observed}) as response:
                self.assertEqual(response.status_code,409)
            self.assertEqual(clear.call_args.args,(self.app,self.store,'youtube','abcdefghijk'))
            self.assertEqual(clear.call_args.kwargs,{'observed':observed})


if __name__=='__main__':unittest.main()
