import json
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import test_view_media as vm
from backend import discover_store as ds, discovery_engine as engine, discover_title_releases as direct, discover_acquire as acquire
from backend.media_management import service, store as records


class TitleReleaseTests(unittest.TestCase):
    setUp=vm.ViewMediaTests.setUp
    tearDown=vm.ViewMediaTests.tearDown

    def fixture(self):
        self.item=dict(id='show:123931',kind='show',externalId='123931',title='Invasion of the E Bikes Scourge of Our Streets 2026 480p HDTV x264-RMTeam')
        folder=Path(self.store.directory)/'nzb-engine';folder.mkdir(exist_ok=True)
        (folder/'settings.json').write_text(json.dumps(dict(indexers=[dict(url='https://index.example',api_key='secret',name='My indexer') ])))
        for id,title,external,key in [('720','Invasion.of.the.E.Bikes.Scourge.of.Our.Streets.2026.720p.HDTV.H264-JFF','123931','secret'),
            ('1080','Invasion.of.the.E.Bikes.Scourge.of.Our.Streets.2026.1080p.HDTV.H264-GROUP','123931','secret'),
            ('other','Other.Documentary.2026.1080p.HDTV-GROUP','123931','secret'),
            ('year','Invasion.of.the.E.Bikes.Scourge.of.Our.Streets.2025.1080p.HDTV-GROUP','123931','secret'),
            ('account','Invasion.of.the.E.Bikes.Scourge.of.Our.Streets.2026.1080p.HDTV-GROUP','123931','oldsecret'),
            ('sample','Invasion.of.the.E.Bikes.Scourge.of.Our.Streets.2026.1080p.HDTV.sample','123931','secret'),
            ('wrongid','Invasion.of.the.E.Bikes.Scourge.of.Our.Streets.2026.1080p.HDTV-GROUP','12','secret')]:
            ds.remember_candidates(self.store,[dict(id=id,title=title,externalId=external,kind='show',quality='720p' if id=='720' else '1080p',size=1_000_000_000,grab='https://index.example/api?t=get&id='+id+'&apikey='+key)])
        with patch.object(engine.sources,'episodes',side_effect=AssertionError('Episode metadata must not block exact releases')):
            engine.prepare(self.app,self.store,self.item)
        self.key='title-release:123931:'+direct.title_key(self.item)
        return records.row(self.store,self.key)

    def post(self,path,body,csrf=True):
        return self.client.post(path,base_url=vm.ORIGIN,json=body,headers={'Origin':vm.ORIGIN,**({'X-CSRF-Token':'token'} if csrf else {})})

    def test_exact_saved_releases_work_without_metadata_or_auto_request(self):
        r=self.fixture()
        data=engine.title_data(self.app,self.store,self.item)
        self.assertTrue(data['releaseFallback']);self.assertFalse(data['loading']);self.assertFalse(data['error'])
        self.assertEqual(len(data['items']),1);self.assertIsNone(data['items'][0]['number']);self.assertIsNone(data['items'][0]['season'])
        self.assertFalse(data['downloadAll']);self.assertFalse(data['downloadFuture'])
        releases=service.candidates(self.app,self.store,r)['items']
        self.assertEqual({c['id'] for c in releases},{'720','1080'})
        self.assertEqual({c['source'] for c in releases},{'My indexer'})
        self.assertNotIn('secret',json.dumps(data)+json.dumps(releases));self.assertNotIn('grab',json.dumps(releases))
        self.assertEqual(ds.public_requests(self.store),[])
        with self.assertRaises(ValueError):engine.queue(self.app,self.store,self.key)
        with self.assertRaises(ValueError):engine.save_plan(self.app,self.store,self.item['id'],'all',True)

    def test_selected_file_reaches_existing_engine_contract_and_is_idempotent(self):
        self.fixture()
        fake=Mock();fake.preview_bytes.return_value=dict(id='preview',accepted=True)
        fake.start.side_effect=lambda *args,**kwargs:dict(id=kwargs['job_id'],state='queued')
        fake.preview_activity_key.return_value='native:nzb:'+'b'*64
        with patch('backend.nzb._engine',return_value=fake),patch.object(engine.sources,'fetch',return_value=b'<nzb/>'):
            response=self.post('/api/media-management',dict(action='search',key=self.key,value='1080'))
            self.assertEqual(response.status_code,200,response.json)
            second=self.post('/api/media-management',dict(action='search',key=self.key,value='1080'))
            self.assertEqual(second.status_code,400)
        fake.start.assert_called_once()
        fake.preview_activity_key.assert_called_once_with('preview',None)
        args,kwargs=fake.start.call_args
        self.assertEqual(args[2],'Invasion of the E Bikes Scourge of Our Streets (2026)')
        self.assertEqual(len(kwargs.pop('handoff_token')),32)
        self.assertEqual(len(kwargs.pop('job_id')),32)
        self.assertEqual(kwargs,dict(lookup_provider='tvdb',lookup_identity='123931'))
        requests=ds.public_requests(self.store);self.assertEqual(len(requests),1);self.assertEqual(requests[0]['state'],'queued')
        self.assertEqual(requests[0]['attempts'][0]['candidateId'],'1080')

    def test_policy_account_expiry_and_cancel_fences(self):
        r=self.fixture()
        for id in ('720','other','year','account','sample','wrongid'):
            self.assertEqual(self.post('/api/media-management',dict(action='search',key=self.key,value=id)).status_code,400)
        with patch.object(acquire,'submit_nzb') as submit:
            response=self.post('/api/media-management',dict(action='search',key=self.key,value='1080'))
            self.assertEqual(response.status_code,200,response.json)
            submit.assert_called_once()
            row=submit.call_args.args[3]
            ds.cancel_request(self.store,row['id'])
            submit.reset_mock();acquire.resolve_release(self.app,self.store,row,direct.releases(self.app,self.store,self.item));submit.assert_not_called()
        with self.store.connect() as db:db.execute('UPDATE discover_candidates SET created=?',(time.time()-90000,))
        self.assertEqual(direct.releases(self.app,self.store,self.item),[])
        self.assertEqual(acquire.search_for(self.app,self.store,row),[])

    def test_two_titles_sharing_tvdb_remain_separate(self):
        self.fixture()
        other={**self.item,'title':'Other Documentary 2026 1080p HDTV-GROUP'}
        engine.prepare(self.app,self.store,other)
        self.assertEqual(len(engine.title_data(self.app,self.store,self.item)['items']),1)
        self.assertEqual(engine.title_data(self.app,self.store,other)['items'][0]['title'],'Other Documentary')

    def test_numbered_series_and_unknown_titles_keep_existing_path(self):
        self.fixture()
        for title in ('Test Show S01E01','Test Show Season 1 2026 1080p HDTV','Test Show 2026 10 16 HDTV','Test Show'):
            item={**self.item,'title':title,'id':'show:12','externalId':'12'}
            self.assertFalse(direct.remember(self.app,self.store,item))
            with patch.object(engine.sources,'episodes',side_effect=ValueError('No metadata')):
                with self.assertRaises(ValueError):engine.prepare(self.app,self.store,item)

    def test_auth_csrf_and_disabled_account(self):
        self.fixture()
        payload=dict(action='releases',key=self.key)
        self.assertEqual(self.post('/api/media-management',payload,False).status_code,403)
        self.assertNotEqual(self.app.test_client().post('/api/media-management',base_url=vm.ORIGIN,json=payload).status_code,200)
        config=Path(self.store.directory)/'nzb-engine/settings.json';config.write_text('{}')
        self.assertEqual(self.post('/api/media-management',payload).json['items'],[])
        self.assertEqual(self.post('/api/media-management',dict(action='search',key=self.key,value='1080')).status_code,400)
