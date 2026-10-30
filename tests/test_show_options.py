"""TV options exercise the existing plans and policies with disposable data."""
import json
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
import test_view_media as vm
from backend import show_options as options,discovery_engine as engine,discover_store as ds
from backend.media_management import store as records,service


class ShowOptionsTests(unittest.TestCase):
    def setUp(self):
        vm.ViewMediaTests.setUp(self)
        self.scheduler=patch.object(engine,'schedule');self.schedule=self.scheduler.start();self.addCleanup(self.scheduler.stop)
        self.item=dict(id='show:77',kind='show',externalId='77',title='A Show',year='2020')
        for season,number,day in [(1,1,'2020-01-01'),(2,1,'2020-02-01'),(2,2,'2099-01-01'),(0,1,'2020-02-02')]:
            records.remember(self.store,f'show:77:{season}:{number}',self.item['id'],'episode',dict(item=self.item,episode=dict(id=season*10+number,season=season,number=number,name='Episode'),date=day,automatic=True))
    tearDown=vm.ViewMediaTests.tearDown
    def post(self,settings=None,item=None,csrf=True):
        payload=dict(item=item or self.item)
        if settings is not None:payload['settings']=settings
        return self.client.post('/api/discovery-engine/show-options',base_url=vm.ORIGIN,json=payload,headers={'Origin':vm.ORIGIN,**({'X-CSRF-Token':'token'} if csrf else {})})
    def plan(self):
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM discover_series_plan WHERE parent=?',(self.item['id'],)).fetchone()
        return dict(row) if row else None
    def requests(self):
        with self.store.connect() as db:return [dict(r) for r in db.execute('SELECT * FROM discover_requests ORDER BY created')]
    def apply(self):engine.apply_plan(self.app,self.store,self.plan())

    def test_open_and_unrelated_saves_never_opt_in_or_rewrite_existing_policies(self):
        with self.store.connect() as db:db.execute("INSERT INTO managed_series VALUES('show:77','77','720',1,'inherit')")
        result=self.post();self.assertEqual(result.status_code,200,result.json)
        self.assertFalse(result.json['shown']);self.assertFalse(result.json['favourite']);self.assertFalse(result.json['downloadAll']);self.assertFalse(result.json['downloadFuture'])
        self.assertEqual(result.json['quality'],'720');self.assertIsNone(result.json['minimum'])
        for field,value in [('shown',True),('favourite',True),('minimum',5),('quality','1080')]:self.assertEqual(self.post({field:value}).status_code,200)
        self.assertIsNone(self.plan());self.assertEqual(self.requests(),[])
        with self.store.connect() as db:self.assertEqual(tuple(db.execute('SELECT quality,fallback,monitoring FROM managed_series').fetchone()),('720',1,'inherit'))

    def test_auth_csrf_and_invalid_fields(self):
        self.assertIn(self.app.test_client().post('/api/discovery-engine/show-options',base_url=vm.ORIGIN,json={}).status_code,(401,403))
        self.assertEqual(self.post(csrf=False).status_code,403)
        for value in [{'dates':True},{'shown':1},{'downloadFuture':'true'},{'quality':'999'},{'quality':[]},{'minimum':True},{'minimum':-1},{'minimum':1441},{'minimum':1.5},{}, {'shown':True,'downloadAll':True}]:
            self.assertEqual(self.post(value).status_code,400,value)
        self.assertEqual(self.post(item=dict(self.item,externalId='88')).status_code,400)
        self.assertEqual(self.post(item=dict(self.item,kind='movie')).status_code,400)

    def test_existing_future_boundary_and_both_plan_flags_survive_open_and_quality(self):
        engine.save_plan(self.app,self.store,self.item['id'],'future',True)
        engine.save_plan(self.app,self.store,self.item['id'],'all',True)
        with self.store.connect() as db:db.execute("UPDATE discover_series_plan SET future_from='2023-01-02'")
        before=self.plan();result=self.post().json
        self.assertTrue(result['downloadAll']);self.assertTrue(result['downloadFuture'])
        self.post({'quality':'720'});self.post({'minimum':0});self.post({'favourite':True})
        self.assertEqual(self.plan(),before);self.assertEqual(self.requests(),[])

    def test_all_released_seasons_future_dates_owned_and_duplicate_protection(self):
        self.store.set('storage_base',str(self.root));owned=self.root/'existing.mkv';owned.write_bytes(b'existing video')
        ds.mark_owned(self.store,'show:77:1:1','owned',str(owned),'complete')
        self.post({'downloadAll':True});self.apply();self.apply()
        self.assertEqual({r['episode_key'] for r in self.requests()},{'show:77:2:1','show:77:0:1'})
        self.assertTrue(all(json.loads(r['body'])['plan']=='all' for r in self.requests()))

    def test_future_only_stays_separate_from_all_and_is_not_failed(self):
        self.post({'downloadFuture':True});self.apply()
        self.assertEqual([r['episode_key'] for r in self.requests()],['show:77:2:2'])
        self.assertEqual(self.requests()[0]['state'],'waiting')
        self.assertFalse(self.post().json['downloadAll'])

    def test_cancelled_episode_stays_held_after_each_option_and_automation_reenable(self):
        self.post({'downloadAll':True});self.apply();row=self.requests()[0]
        from backend.acquisition import cancel_discover
        cancel_discover(self.app,self.store,row['id'])
        for field,value in [('quality','720'),('minimum',5),('shown',True),('favourite',True),('downloadFuture',True),('downloadAll',False),('downloadAll',True)]:
            self.assertEqual(self.post({field:value}).status_code,200);self.apply()
        self.assertEqual([r['state'] for r in self.requests() if r['episode_key']==row['episode_key']],['cancelled'])
        self.assertEqual(records.row(self.store,row['episode_key'])['hold'],1)

    def test_off_cancels_only_pending_plan_scope_preserves_manual_and_running(self):
        self.post({'downloadAll':True});self.apply();rows=self.requests()
        ds.update_request(self.store,rows[0]['id'],state='downloading',engine='nzb',engine_id='running')
        engine.queue(self.app,self.store,'show:77:2:2')
        manual=self.requests()[-1]
        self.post({'downloadAll':False})
        states={r['id']:r['state'] for r in self.requests()}
        self.assertEqual(states[rows[0]['id']],'downloading');self.assertEqual(states[rows[1]['id']],'cancelled');self.assertEqual(states[manual['id']],'waiting')

    def test_disabled_plan_is_rechecked_inside_request_insert(self):
        self.post({'downloadAll':True})
        real_insert=ds.insert_request
        def raced(store,payload):
            self.post({'downloadAll':False})
            return real_insert(store,payload)
        with patch.object(ds,'insert_request',side_effect=raced):self.apply()
        self.assertEqual(self.requests(),[])

    def test_show_policies_persist_independently_and_globals_are_dynamic(self):
        self.post({'quality':'720'});self.post({'minimum':5})
        other=dict(self.item,id='show:88',externalId='88')
        self.assertEqual(self.post(item=other).json['quality'],'default')
        self.assertEqual(options.policy(self.store,self.item)['minimum'],5)
        self.assertEqual(service.effective_policy(self.store,records.row(self.store,'show:77:2:1'))['quality'],'720')
        self.post({'quality':'default'});self.post({'minimum':None});self.store.set('discover_engine_quality','2160')
        directory=Path(self.tmp.name)/'nzb-engine';directory.mkdir(exist_ok=True);(directory/'settings.json').write_text(json.dumps(dict(min_video_minutes=12)))
        self.assertEqual(options.policy(self.store,self.item)['quality'],'2160');self.assertEqual(options.policy(self.store,self.item)['minimum'],12)
        self.assertEqual(self.post().json['quality'],'default');self.assertIsNone(self.post().json['minimum'])

    def test_legacy_series_and_explicit_episode_choices_are_preserved(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO managed_series VALUES('show:77','77','720',1,'inherit')")
            db.execute("UPDATE managed_media SET monitor=0,quality='2160' WHERE key='show:77:1:1'")
        self.post();self.assertEqual(self.post().json['quality'],'720')
        self.post({'quality':'1080'})
        self.assertTrue(options.policy(self.store,self.item)['fallback'],'existing series lower-quality choice remains authoritative')
        self.assertEqual(service.effective_policy(self.store,records.row(self.store,'show:77:1:1'))['quality'],'2160')
        self.assertEqual(service.effective_policy(self.store,records.row(self.store,'show:77:2:1'))['quality'],'1080')

    def test_plan_requests_use_show_quality_and_minimum_in_existing_nzb_engine(self):
        self.post({'quality':'720'});self.post({'minimum':5});self.post({'downloadAll':True});self.apply()
        row=self.requests()[0];self.assertEqual(row['quality'],'720');self.assertEqual(options.minimum_for_request(self.store,row),5)
        generation=ds.claim_start(self.store,row['id'])
        fake=Mock();fake.preview_bytes.return_value=dict(id='preview',accepted=True,on_server='available')
        fake.start.side_effect=lambda *args,**kwargs:dict(id=kwargs['job_id'],name='job')
        fake.preview_activity_key.return_value='native:nzb:'+'c'*64
        with patch('backend.discover_acquire.candidate',return_value={'grab':'https://example.test/private'}),patch('backend.discover_sources.fetch',return_value=b'<nzb></nzb>'),patch('backend.nzb._engine',return_value=fake):
            from backend.discover_acquire import submit_nzb
            submit_nzb(self.app,self.store,dict(id='release',title='A.Show.S02E01.720p'),row,generation)
        self.assertEqual(fake.start.call_args.kwargs['minimum_minutes'],5)
        self.assertEqual(len(fake.start.call_args.kwargs['handoff_token']),32)
        self.assertEqual(len(fake.start.call_args.kwargs['job_id']),32)
        fake.preview_activity_key.assert_called_once_with('preview',None)

    def test_guide_only_known_air_dates_and_favourite_api(self):
        self.post({'shown':True});self.post({'favourite':True})
        with self.store.connect() as db:
            db.execute("UPDATE managed_media SET body=json_set(body,'$.date','unknown') WHERE key='show:77:0:1'")
        result=self.client.get('/api/guide?start=2020-01-01&end=2020-01-31',base_url=vm.ORIGIN)
        self.assertEqual(result.status_code,200,result.json);self.assertIn('movies',result.json);self.assertIn('channels',result.json)
        show=result.json['shows'][0];self.assertTrue(show['favourite']);self.assertEqual(len(show['events']),1)
        self.assertEqual(show['events'][0]['title'],'S01E01 · Episode');self.assertNotIn('time',show['events'][0])
        self.post({'shown':False});self.assertEqual(options.guide(self.app,self.store,'2020-01-01','2099-12-31'),[])

    def test_unnumbered_titles_are_separate_and_never_inherit_umbrella_automation(self):
        self.post({'downloadAll':True})
        first=dict(self.item,title='One.Documentary.2020.1080p.WEB');other=dict(self.item,title='Other.Documentary.2020.1080p.WEB')
        self.post({'favourite':True},item=first)
        self.assertFalse(self.post(item=first).json['automaticSupported']);self.assertFalse(self.post(item=first).json['downloadAll'])
        self.assertFalse(self.post(item=other).json['favourite']);self.assertEqual(self.post({'downloadFuture':True},item=first).status_code,400)
        self.assertTrue(self.plan()['download_all']);self.assertEqual(self.requests(),[])
        self.schedule.reset_mock();self.post({'shown':True},item=first)
        self.schedule.assert_not_called()

    def test_missing_episode_metadata_is_honestly_manual_only(self):
        item=dict(self.item,id='show:88',externalId='88')
        result=self.post(item=item).json
        self.assertFalse(result['automaticSupported']);self.assertTrue(result['automaticMessage'])
        self.assertEqual(self.post({'downloadAll':True},item=item).status_code,400);self.assertEqual(self.requests(),[])

    def test_manual_vod_selection_checks_the_exact_episode_and_passes_its_policy(self):
        self.post({'minimum':5});self.post({'quality':'720'})
        entry=dict(source='fixture',path='S02E01.strm',season=2,number=1)
        body=dict(key='show:77:2:1',vod={k:entry[k] for k in ('source','path')})
        headers={'Origin':vm.ORIGIN,'X-CSRF-Token':'token'}
        with patch('backend.view_media.download_sources.choices',return_value={'sources':[dict(type='vod',entries=[entry])]}),patch('backend.m3u_downloads.lookup',return_value=(dict(title='Episode',kind='tv',folder='fixture'),'https://fixture.invalid/video.mp4')),patch('backend.m3u_downloads.enqueue_resolved',return_value='job') as enqueue:
            wrong=self.client.post('/api/discovery-engine/download',base_url=vm.ORIGIN,json=dict(body,key='show:77:1:1'),headers=headers)
            self.assertEqual(wrong.status_code,400);enqueue.assert_not_called()
            result=self.client.post('/api/discovery-engine/download',base_url=vm.ORIGIN,json=body,headers=headers)
            self.assertEqual(result.status_code,202,result.json);selected=enqueue.call_args.kwargs['episode_policy'];self.assertEqual(selected['minimum'],5);self.assertEqual(selected['quality'],'720')
            self.assertIsNotNone(enqueue.call_args.kwargs['transaction'])
        self.assertEqual(len(self.requests()),1);self.assertEqual(self.requests()[0]['engine'],'vod')

    def vod_job(self):
        from backend import m3u_downloads as vod
        self.app.config['MEDIA_DIR']=str(self.root)
        self.store.set('storage_base',str(self.root));self.post({'minimum':0});self.post({'quality':'720'})
        engine.queue(self.app,self.store,'show:77:2:1');row=self.requests()[0];generation=ds.claim_start(self.store,row['id'])
        selected=dict(options.policy(self.store,self.item),requestId=row['id'])
        # Match the Compose shared mount after legacy request setup adjusts its
        # YouTube-only MEDIA_DIR. Production fixed storage keeps the shared root.
        self.app.config['MEDIA_DIR']=str(self.root)
        with patch.object(vod,'lookup',return_value=({'title':'A Show S02E01','kind':'series','folder':''},'https://example.test/private')),patch.object(vod,'settings',return_value={'folders':{'series':str(self.root)}}),patch.object(vod,'safe_folder',return_value=self.root):
            identity=vod.enqueue(self.app,self.store,'fixture','TV/A Show/S02E01.strm',episode_policy=selected)
        self.assertTrue(ds.commit_engine(self.store,row['id'],generation,'vod',identity,'Queued'))
        self.assertEqual(options.job_policy(self.store,'vod',identity),selected)
        with self.store.connect() as db:job=dict(db.execute('SELECT * FROM m3u_downloads WHERE id=?',(identity,)).fetchone())
        return job

    def vod_transfer(self,job,reject=False):
        import io,threading
        from backend import m3u_downloads as vod,movie_options
        content=b'\x00\x00\x00\x20ftypisom'+b'x'*6000
        response=Mock();response.status=200;response.read=io.BytesIO(content).read
        response.getheader.side_effect=lambda key,default='':{'Content-Type':'application/octet-stream','Content-Length':str(len(content))}.get(key,default)
        def metadata(store,root,record,request,**kwargs):
            self.assertEqual(request['kind'],'episode');target=Path(root)/record['path'];self.assertFalse(target.exists())
            for p in (target.with_suffix('.nfo'),target.parent/'poster.jpg',target.parent/'fanart.jpg'):p.write_bytes(b'fixture-sidecar')
        with patch.object(vod,'roots',return_value=[self.root]),patch.object(vod,'safe_folder',return_value=self.root),patch.object(vod,'lookup',return_value=({},'https://example.test/private')),patch('backend.m3u_player.upstream',return_value=(Mock(),response,'https://example.test/episode.mp4')),patch.object(movie_options,'verify_file',side_effect=ValueError('Episode below minimum length') if reject else None) as verify,patch('backend.discover_publication.write_metadata',side_effect=metadata) as write:
            vod.transfer(self.app,self.store,job,threading.Event())
        self.assertEqual(verify.call_args.args[1]['kind'],'episode')
        return write

    def test_vod_episode_minimum_is_persisted_and_rejects_before_publication(self):
        job=self.vod_job();write=self.vod_transfer(job,reject=True);write.assert_not_called()
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT state FROM m3u_downloads').fetchone()[0],'failed')
        self.assertFalse(list(self.root.rglob('*.mp4')));self.assertFalse(list(self.root.rglob('*.part')))

    def test_vod_episode_publishes_sidecars_and_preserves_other_episodes(self):
        job=self.vod_job();folder=Path(job['destination']).parent;folder.mkdir(parents=True,exist_ok=True)
        sibling=folder/'Existing S01E01.mkv';sibling.write_bytes(b'existing episode')
        self.vod_transfer(job)
        with self.store.connect() as db:row=db.execute('SELECT * FROM m3u_downloads').fetchone()
        self.assertEqual(row['state'],'complete');target=Path(row['destination']);self.assertTrue(target.is_file());self.assertTrue(target.with_suffix('.nfo').is_file());self.assertTrue((folder/'poster.jpg').is_file());self.assertTrue((folder/'fanart.jpg').is_file());self.assertEqual(sibling.read_bytes(),b'existing episode')
