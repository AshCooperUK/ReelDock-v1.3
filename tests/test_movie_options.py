"""Movie preferences use disposable data; source/acquisition work is explicitly mocked."""
import json
import subprocess
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import test_view_media as vm
from backend import movie_options as options,discovery_engine as engine,discover_store as ds
from backend.media_management import store as records


class MovieOptionsTests(unittest.TestCase):
    def setUp(self):
        vm.ViewMediaTests.setUp(self)
        self.scheduler=patch.object(engine,'schedule');self.scheduler.start();self.addCleanup(self.scheduler.stop)
        self.item=dict(id='movie:1234567',kind='movie',externalId='1234567',title='A Movie',year='2020',releaseDate='2020-01-01')
    tearDown=vm.ViewMediaTests.tearDown
    def post(self,settings=None,item=None,csrf=True):
        body=dict(item=item or self.item)
        if settings is not None:body['settings']=settings
        return self.client.post('/api/discovery-engine/movie-options',base_url=vm.ORIGIN,json=body,headers={'Origin':vm.ORIGIN,**({'X-CSRF-Token':'token'} if csrf else {})})
    def row(self):return options.find(self.store,self.item)
    def request(self):
        with self.store.connect() as db:return dict(db.execute('SELECT * FROM discover_requests ORDER BY created DESC LIMIT 1').fetchone())
    def enable(self):
        options.save(self.app,self.store,self.item,dict(automatic=True))
        with patch.object(options,'refresh'):options.check_automatic(self.app,self.store,options.managed(self.store,self.item))

    def test_read_does_not_show_or_download_any_movie(self):
        result=self.post();self.assertEqual(result.status_code,200,result.json)
        self.assertFalse(result.json['shown']);self.assertFalse(result.json['automatic']);self.assertFalse(result.json['favourite'])
        self.assertEqual(result.json['quality'],'default');self.assertIsNone(result.json['minimum'])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM discover_requests').fetchone()[0],0)

    def test_auth_csrf_types_and_client_dates_are_rejected(self):
        self.assertIn(self.app.test_client().post('/api/discovery-engine/movie-options',base_url=vm.ORIGIN,json={}).status_code,(401,403))
        self.assertEqual(self.post(csrf=False).status_code,403)
        for settings in ({'shown':'yes'},{'automatic':1},{'minimum':True},{'minimum':-1},{'minimum':1441},{'minimum':1.2},{'quality':'999'},{'release_date':'2026-01-01'},{}):
            self.assertEqual(self.post(settings).status_code,400,settings)
        self.assertEqual(self.post(item=dict(self.item,externalId='7654321')).status_code,400)
        self.assertEqual(self.post(item=dict(self.item,kind='show')).status_code,400)

    def test_values_persist_and_global_inheritance_remains_dynamic(self):
        self.post({'shown':True,'dates':False,'favourite':True,'quality':'720','minimum':90})
        result=self.post().json
        self.assertTrue(result['shown']);self.assertTrue(result['favourite']);self.assertFalse(result['dates']);self.assertEqual(result['minimum'],90)
        self.assertEqual(options.policy(self.store,self.item)['quality'],'720')
        self.post({'quality':'default','minimum':None});self.store.set('discover_engine_quality','2160')
        self.assertEqual(options.policy(self.store,self.item)['quality'],'2160')
        self.assertIsNone(self.post().json['minimum'])

    def test_padded_imdb_identity_shares_the_same_options(self):
        self.post({'favourite':True})
        other=dict(self.item,id='movie:01234567',externalId='01234567')
        self.assertTrue(self.post(item=other).json['favourite'])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM movie_options').fetchone()[0],1)

    def test_guide_has_known_date_events_only_and_one_switch_hides_all(self):
        self.post({'shown':True,'favourite':True})
        self.assertEqual(options.guide(self.store,'2020-01-01','2030-12-31')[0]['events'],[],'client release date does not become a Guide fact')
        with self.store.connect() as db:db.execute('UPDATE movie_options SET release_date=?,premiere_date=?',('2026-10-20','2026-10-19'))
        options.observe(self.store,[self.item],'NZB',time.time())
        rows=options.guide(self.store,'2026-01-01','2026-12-31')
        self.assertTrue(rows[0]['favourite']);self.assertEqual([e['label'] for e in rows[0]['events']],['Release date','Premiere date','Found in sources'])
        self.assertTrue(all('time' not in event for event in rows[0]['events']))
        self.post({'dates':False});self.assertEqual(options.guide(self.store,'2026-01-01','2026-12-31')[0]['events'],[])
        self.post({'shown':False});self.assertEqual(options.guide(self.store,'2026-01-01','2026-12-31'),[])

    def test_source_first_found_date_is_stable_and_records_multiple_sources(self):
        self.post();options.observe(self.store,[self.item],'NZB',1000);options.observe(self.store,[self.item],'VOD',2000)
        options.observe(self.store,[dict(self.item,id='movie:7654321',externalId='7654321')],'Local library',500)
        self.assertEqual(self.row()['found_at'],1000);self.assertEqual(json.loads(self.row()['found_sources']),['NZB','VOD'])

    def test_guide_api_includes_movies_without_changing_youtube_data(self):
        self.post({'shown':True})
        result=self.client.get('/api/guide?start=2026-10-19&end=2026-10-25',base_url=vm.ORIGIN)
        self.assertEqual(result.status_code,200,result.json);self.assertEqual(result.json['movies'][0]['item']['id'],self.item['id'])
        self.assertIn('uploads',result.json);self.assertIn('channels',result.json)

    def test_automatic_movie_uses_existing_engine_and_does_not_duplicate(self):
        self.post({'quality':'720','minimum':85});self.enable()
        first=self.request();self.assertEqual(first['quality'],'720');self.assertEqual(first['manual'],0)
        self.assertTrue(json.loads(first['body'])['movieAutomatic']);self.assertEqual(options.minimum_for_request(self.store,first),85)
        with patch.object(options,'refresh'):options.check_automatic(self.app,self.store,options.managed(self.store,self.item))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM discover_requests').fetchone()[0],1)

    def test_cancel_holds_automatic_work_until_explicitly_enabled_again(self):
        self.enable();first=self.request()
        from backend.acquisition import cancel_discover
        cancel_discover(self.app,self.store,first['id'])
        self.assertFalse(self.post().json['automatic'])
        self.post({'quality':'720'})
        with patch.object(options,'refresh'):options.check_automatic(self.app,self.store,options.managed(self.store,self.item))
        self.assertEqual(self.request()['id'],first['id']);self.assertEqual(self.request()['state'],'cancelled')
        self.enable();self.assertNotEqual(self.request()['id'],first['id']);self.assertEqual(self.request()['state'],'searching')

    def test_disabling_cancels_pending_automatic_work_but_preserves_manual_request(self):
        self.enable();self.post({'automatic':False});self.assertEqual(self.request()['state'],'cancelled')
        engine.queue(self.app,self.store,options.managed(self.store,self.item)['key'])
        manual=self.request();self.assertTrue(manual['manual'])
        self.post({'automatic':False});self.assertEqual(self.request()['id'],manual['id']);self.assertEqual(self.request()['state'],'searching')

    def test_disabling_leaves_running_download_to_finish(self):
        self.enable();row=self.request();ds.update_request(self.store,row['id'],state='downloading',engine='nzb',engine_id='saved-job')
        self.post({'automatic':False});self.assertEqual(self.request()['state'],'downloading')

    def test_off_policy_prevents_stale_resolution_from_starting_sources(self):
        self.enable();row=self.request();self.post({'automatic':False})
        with patch.object(engine,'fallback_vod') as vod,patch.object(engine,'fallback_youtube') as youtube:
            engine.resolve(self.app,self.store,row)
        vod.assert_not_called();youtube.assert_not_called()

    def test_actual_staged_file_minimum_and_quality(self):
        path=Path(self.tmp.name)/'movie-check.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=black:s=1280x720:r=1','-t','2','-c:v','libx264','-preset','ultrafast',str(path)],check=True,timeout=20)
        options.verify_file(path,dict(minimum=0,quality='720',fallback=False))
        with self.assertRaisesRegex(ValueError,'minimum length'):options.verify_file(path,dict(minimum=1,quality='720',fallback=False))
        with self.assertRaisesRegex(ValueError,'quality'):options.verify_file(path,dict(minimum=0,quality='1080',fallback=False))
        options.verify_file(path,dict(minimum=0,quality='1080',fallback=True))
        self.assertTrue(path.is_file(),'validation never deletes an existing/staged file itself')

    def test_unreadable_duration_is_not_published(self):
        with patch.object(options.subprocess,'run',return_value=SimpleNamespace(stdout='{"format":{"duration":"NaN"}}')):
            with self.assertRaisesRegex(ValueError,'Could not verify'):options.verify_file('fixture',dict(minimum=1))

    def test_nzb_minimum_override_does_not_change_global_or_allow_samples(self):
        from backend.downloaders.nzb.engine import NzbEngine,EngineError
        engine_object=object.__new__(NzbEngine);engine_object.config={'min_video_minutes':60}
        folder=Path(self.tmp.name);movie=folder/'Movie.mp4';sample=folder/'Movie.sample.mp4'
        for path in (movie,sample):path.write_bytes(b'fixture')
        self.assertEqual(engine_object._filter_videos([movie,sample],0),[movie]);self.assertEqual(engine_object.config['min_video_minutes'],60)
        with patch('backend.downloaders.nzb.engine.subprocess.run',return_value=SimpleNamespace(stdout='{"format":{"duration":"3000"}}')):
            with self.assertRaises(EngineError):engine_object._filter_videos([movie])
            self.assertEqual(engine_object._filter_videos([movie],40),[movie])

    def test_nzb_job_minimum_survives_restart_and_old_jobs_inherit(self):
        from backend.downloaders.nzb.engine import _job_from
        self.assertEqual(_job_from(dict(id='test',minimum_minutes=80)).minimum_minutes,80)
        self.assertIsNone(_job_from(dict(id='old')).minimum_minutes)
        self.assertIsNone(_job_from(dict(id='invalid',minimum_minutes=True)).minimum_minutes)

    def test_metadata_refresh_does_not_guess_a_premiere_or_repeat_failed_checks(self):
        self.post()
        with patch('backend.view_media.movie_info.details',return_value={'guideDates':{'releaseDate':'2026-10-20','premiereDate':''}}),patch.object(options,'known_sources'):
            options.refresh(self.app,self.store,self.item)
        self.assertEqual(self.row()['release_date'],'2026-10-20');self.assertEqual(self.row()['premiere_date'],'')
        with patch('backend.view_media.movie_info.details',side_effect=ValueError('Unavailable')),patch.object(options,'known_sources'):
            options.refresh(self.app,self.store,self.item)
        self.assertEqual(self.row()['release_date'],'2026-10-20','provider failure preserves the last known date')
        with patch.object(engine,'schedule') as schedule:options.schedule_refresh(self.app,self.store,self.row())
        schedule.assert_not_called()

    def _vod_job(self):
        from backend import m3u_downloads as vod
        self.post({'minimum':0,'quality':'720'});self.enable();request=self.request();generation=ds.claim_start(self.store,request['id'])
        selected=dict(options.policy(self.store,self.item),requestId=request['id'])
        with patch.object(vod,'lookup',return_value=({'title':'A Movie','kind':'movies','folder':''},'https://example.test/private')),patch.object(vod,'settings',return_value={'folders':{'movies':str(self.root)}}),patch.object(vod,'safe_folder',return_value=self.root),patch('backend.discover_publication.planned_path',return_value='Movies/A Movie (2020)/A Movie (2020).mkv'):
            identity=vod.enqueue(self.app,self.store,'fixture-source','Movies/A Movie.strm',movie_policy=selected)
        self.assertTrue(ds.commit_engine(self.store,request['id'],generation,'vod',identity,'Queued'))
        with self.store.connect() as db:job=dict(db.execute('SELECT * FROM m3u_downloads WHERE id=?',(identity,)).fetchone())
        self.assertEqual(options.job_policy(self.store,'vod',identity),selected)
        self.assertEqual(Path(job['destination']),self.root/'Movies/A Movie (2020)/A Movie (2020)')
        return job

    def _vod_transfer(self,job,rejection=False):
        import io,threading
        from unittest.mock import Mock
        from backend import m3u_downloads as vod
        body=b'\x00\x00\x00\x20ftypisom'+b'x'*6000
        response=Mock();response.status=200;response.read=io.BytesIO(body).read
        response.getheader.side_effect=lambda key,default='':{'Content-Type':'application/octet-stream','Content-Length':str(len(body))}.get(key,default)
        def metadata(store,root,record,request,**kwargs):
            self.assertEqual(kwargs,{'source':''});target=Path(root)/record['path'];self.assertFalse(target.exists())
            for path in (target.with_suffix('.nfo'),target.parent/'poster.jpg',target.parent/'fanart.jpg'):path.write_bytes(b'fixture-sidecar')
        with patch.object(vod,'roots',return_value=[self.root]),patch.object(vod,'safe_folder',return_value=self.root),patch.object(vod,'lookup',return_value=({},'https://example.test/private')),patch('backend.m3u_player.upstream',return_value=(Mock(),response,'https://example.test/movie.mp4')),patch.object(options,'verify_file',side_effect=ValueError('Below minimum length') if rejection else None),patch('backend.discover_publication.write_metadata',side_effect=metadata) as write:
            vod.transfer(self.app,self.store,job,threading.Event())
        return write

    def test_vod_movie_policy_rejects_short_file_before_publication(self):
        job=self._vod_job();write=self._vod_transfer(job,True);write.assert_not_called()
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT state FROM m3u_downloads').fetchone()[0],'failed')
        self.assertFalse(list(self.root.rglob('*.mp4')));self.assertFalse(list(self.root.rglob('*.part')))

    def test_vod_movie_has_identity_folder_and_sidecars_before_completion(self):
        job=self._vod_job();self._vod_transfer(job)
        with self.store.connect() as db:row=db.execute('SELECT * FROM m3u_downloads').fetchone()
        self.assertEqual(row['state'],'complete');target=Path(row['destination'])
        self.assertTrue(target.is_file());self.assertTrue(target.with_suffix('.nfo').is_file());self.assertTrue((target.parent/'poster.jpg').is_file());self.assertTrue((target.parent/'fanart.jpg').is_file())

    def test_local_source_observation_requires_matching_title_and_year(self):
        self.post();options.observe_local(self.store,[dict(kind='movies',title=self.item['title'],year='1999')]);self.assertFalse(self.row()['found_at'])
        options.observe_local(self.store,[dict(kind='movies',title=self.item['title'],year='2020')]);self.assertTrue(self.row()['found_at'])

    def test_legacy_display_only_request_does_not_gain_a_movie_policy(self):
        self.post()
        legacy=dict(kind='movie',body=json.dumps({'item':{'title':'Legacy display title'}}))
        self.assertIsNone(options.minimum_for_request(self.store,legacy))
        options.observe(self.store,[dict(kind='movie',title='Legacy display title')],'NZB')
        self.assertFalse(self.row()['found_at'])
