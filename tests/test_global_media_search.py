"""All-source search is independent of browse filters and keeps source actions."""
import json
import unittest
from unittest.mock import patch
import test_view_media as vm
from backend.view_media import global_search as search
from backend import discovery_engine as engine

class GlobalSearchTests(unittest.TestCase):
    setUp=vm.ViewMediaTests.setUp
    tearDown=vm.ViewMediaTests.tearDown
    get=vm.ViewMediaTests.get
    def args(self,**values):
        return dict(dict(media='all',q='Python',sort='source',offset=0,collection='',category='all',cursor=None),**values)
    def add_vod(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO m3u_sources(id,name,url,options,folder) VALUES('vod','VOD','https://private.invalid/secret','{}','vod')")

    def test_open_public_youtube_result_registers_before_download_without_subscribing(self):
        identity='abcdefghijk';channel='UC'+'r'*22
        self.assertEqual(self.get('/api/downloads/'+identity).status_code,404)
        item=dict(videoId=identity,title='Public search video',channelId=channel,channel='Creator',published='2026-10-01',publishedApproximate=True)
        response=self.client.post('/api/discover/youtube/register',base_url=vm.ORIGIN,json=dict(item=item),headers={'Origin':vm.ORIGIN,'X-CSRF-Token':'token'})
        self.assertEqual(response.status_code,200,response.json)
        self.assertFalse(self.get('/api/downloads/'+identity).json['saved'])
        self.assertEqual(self.get('/api/downloads/'+identity).status_code,200)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT active FROM channels WHERE id=?',(channel,)).fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM downloads').fetchone()[0],0)
            self.assertTrue(json.loads(db.execute('SELECT data FROM videos WHERE id=?',(identity,)).fetchone()[0])['publishedApproximate'])
        denied=self.client.post('/api/discover/youtube/register',base_url=vm.ORIGIN,json=dict(item=item))
        self.assertEqual(denied.status_code,403,'registration retains CSRF protection')

    def test_opening_search_keeps_an_existing_subscription_and_saved_video(self):
        identity='abcdefghijk';channel='UC'+'r'*22
        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES(?,?,1)',(channel,json.dumps(dict(id=channel,name='Subscribed creator'))))
            saved=dict(id=identity,title='Saved title',channel=channel,state='published',date='2020-01-01')
            db.execute('INSERT INTO videos(id,channel,day,published,data,checked) VALUES(?,?,?,?,?,?)',(identity,channel,'2020-01-01','2020-01-01',json.dumps(saved),1))
        from backend.discover_youtube import register_video
        register_video(self.store,dict(videoId=identity,title='Different search title',channelId=channel,published='2026-10-01',publishedApproximate=True))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT active FROM channels WHERE id=?',(channel,)).fetchone()[0],1)
            self.assertEqual(json.loads(db.execute('SELECT data FROM videos WHERE id=?',(identity,)).fetchone()[0]),saved)

    def test_registered_estimated_date_is_not_promoted_to_an_exact_search_date(self):
        from backend.discover_youtube import register_video
        identity='abcdefghijk'
        register_video(self.store,dict(videoId=identity,title='Clip',published='2026-10-01',publishedApproximate=True))
        video=dict(id='youtube:'+identity,videoId=identity,kind='video',title='Clip',published='2026-09-30',publishedApproximate=True)
        with patch('backend.youtube_discover.browse',return_value=dict(items=[video],nextOffset=None)):
            row=search.browse(self.app,self.store,self.args(cursor={'youtube':0}))['items'][0]
        self.assertTrue(row['dateApproximate']);self.assertEqual(row['date'],'2026-09-30')
    def test_every_provider_and_type_searched_with_independent_cursors(self):
        self.add_vod()
        seen=[]
        def nzb(app,store,args):
            self.assertIsNone(args.get('_partial'),'Only merged global snapshots may be published')
            seen.append((args['media'],args['collection'],args['cursor']['nzb']))
            kind='movie' if args['media']=='movies' else 'show'
            item=dict(id=kind+':1234567',kind=kind,externalId='1234567',title='Python '+kind)
            return dict(items=[dict(id=item['id'],kind=kind,title=item['title'],action=dict(type='discover',item=item))],nextOffset=1,nextCursor='{"nzb":100}')
        vod_seen=[]
        def vod(store,media,query,offset):
            vod_seen.append((media,query,offset))
            return dict(items=[dict(id='vodgroup:'+'a'*40,externalId='1234567',kind='movie',title='Python movie',count=2,_collectionId='movie:1234567',_entry=dict(source='vod',path='Movies/Python.strm',kind='movies'))],nextOffset=24 if offset==0 else None) if media=='movies' else dict(items=[],nextOffset=None)
        local=dict(id='local:file',kind='movies',title='Local Python',action=dict(type='local',id='file'))
        video=dict(id='youtube:abcdefghijk',videoId='abcdefghijk',kind='video',title='Python tutorial')
        with patch.object(search.sources,'indexers_for',return_value=[{}]),patch.dict(search.REGISTRY,{'discover':{'browse':nzb},'local':{'browse':lambda *a:dict(items=[local],nextOffset=None)}}),patch.object(search,'catalogue',side_effect=vod),patch('backend.youtube_discover.browse',return_value=dict(items=[video],nextOffset=20)) as youtube:
            result=search.browse(self.app,self.store,self.args(_partial=lambda result:None))
            continuation=search.browse(self.app,self.store,self.args(offset=1,cursor={'vod_movies':24}))
        self.assertEqual({i['kind'] for i in result['items']},{'movie','show','video'})
        self.assertEqual(len(result['items']),4,'Same verified movie from VOD and NZB shares one result')
        movie=next(i for i in result['items'] if i['kind']=='movie' and i['action']['type']=='discover')
        self.assertEqual(set(movie['origins']),{'NZB','VOD'})
        self.assertEqual(result['warnings'],[],'every mocked provider must complete with its real grouped-card contract')
        self.assertCountEqual(seen,[('movies','all',0),('shows','',0)])
        self.assertEqual(json.loads(result['nextCursor']),dict(nzb_movies=100,nzb_shows=100,youtube=20,vod_movies=24))
        self.assertCountEqual(vod_seen,[('movies','Python',0),('shows','Python',0),('movies','Python',24)])
        self.assertEqual(continuation['items'][0]['action']['type'],'vod-group')
        self.assertEqual(continuation['items'][0]['action']['target']['id'],'movie:1234567')
        self.assertIsNone(continuation['nextCursor'])
        youtube.assert_called_once_with(self.store,query='Python',offset=0,search_dates=True)
    def test_continuation_does_not_restart_exhausted_providers(self):
        with patch.object(search.sources,'indexers_for',return_value=[{}]),patch.dict(search.REGISTRY,{'local':{'browse':lambda *a:self.fail('Local results are exhausted')}}),patch('backend.youtube_discover.browse',return_value=dict(items=[],nextOffset=None)) as youtube:
            result=search.browse(self.app,self.store,self.args(offset=1,cursor={'youtube':20}))
        youtube.assert_called_once_with(self.store,query='Python',offset=20,search_dates=True)
        self.assertIsNone(result['nextCursor'])

    def test_youtube_upload_dates_preserve_approximation_and_prefer_known_dates(self):
        videos=[dict(id='youtube:'+identity,videoId=identity,kind='video',title='Video',published='2026-10-01T00:00:00Z',publishedApproximate=True) for identity in ('abcdefghijk','lmnopqrstuv')]
        with self.store.connect() as db:
            db.execute("INSERT INTO channels VALUES('channel','{}',1)")
            db.execute('INSERT INTO videos(id,channel,day,published,data,checked) VALUES(?,?,?,?,?,?)',('abcdefghijk','channel','2026-09-29','2026-09-29T12:00:00Z','{}',0))
        with patch('backend.youtube_discover.browse',return_value=dict(items=videos,nextOffset=None)):
            result=search.browse(self.app,self.store,self.args(cursor={'youtube':0}))
        known,estimated=result['items']
        self.assertEqual(known['date'],'2026-09-29T12:00:00Z');self.assertFalse(known['dateApproximate'])
        self.assertEqual(estimated['date'],'2026-10-01T00:00:00Z');self.assertTrue(estimated['dateApproximate'])
    def test_provider_failure_keeps_other_results_and_redacts_urls(self):
        item=dict(id='local:1',kind='movies',title='Python',action=dict(type='local',id='1'))
        with patch.object(search.sources,'indexers_for',side_effect=ValueError()),patch.dict(search.REGISTRY,{'local':{'browse':lambda *a:dict(items=[item],nextOffset=None)}}),patch('backend.youtube_discover.browse',side_effect=ValueError('https://private.invalid/secret')):
            result=search.browse(self.app,self.store,self.args())
        self.assertEqual(len(result['items']),1);self.assertTrue(result['incomplete'])
        self.assertNotIn('private',str(result));self.assertIn('YouTube',result['warnings'][0])
    def test_search_endpoint_ignores_browse_source_media_and_collection(self):
        def cached(app,store,args,**kwargs):
            self.assertTrue(args['global']);self.assertEqual(args['media'],'all');self.assertEqual(args['collection'],'')
            return dict(items=[],nextOffset=None)
        with patch.object(engine,'cached_browse',side_effect=cached):
            response=self.client.get('/api/view-media/search?q=Python&source=vod&media=shows&collection=top',base_url=vm.ORIGIN)
        self.assertEqual(response.status_code,200,response.json);self.assertEqual(response.json['source'],'global')
        for cursor in ('{"youtube":1}','{"unknown":0}','{}','{"local":true}'):
            response=self.client.get('/api/view-media/search',query_string=dict(q='Python',cursor=cursor),base_url=vm.ORIGIN)
            self.assertEqual(response.status_code,400)
        self.assertEqual(self.app.test_client().get('/api/view-media/search?q=Python',base_url=vm.ORIGIN).status_code,401)

    def test_library_scan_keeps_search_refreshing_until_local_results_are_ready(self):
        with patch.object(search.sources,'indexers_for',side_effect=ValueError()),patch.dict(search.REGISTRY,{'local':{'browse':lambda *a:dict(items=[],scanning=True,nextOffset=None)}}),patch('backend.youtube_discover.browse',return_value=dict(items=[],nextOffset=None)):
            result=search.browse(self.app,self.store,self.args())
        self.assertTrue(result['scanning']);self.assertTrue(result['incomplete'])
        self.assertTrue(result['retryAt'])
