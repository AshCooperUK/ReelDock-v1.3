"""New Discover orchestration uses disposable databases and mocked provider engines."""
import json
import threading
import time
import unittest
from unittest.mock import Mock,patch
import test_view_media as vm
from backend import discovery_engine as engine,discover_store as ds
from backend.media_management import store as records
from backend.view_media.public_movies import Cards,Detail
from backend import settings
class DiscoveryTests(unittest.TestCase):
    setUp=vm.ViewMediaTests.setUp
    tearDown=vm.ViewMediaTests.tearDown
    def post(self,path,body,csrf=True):
        return self.client.post('/api/discovery-engine/'+path,base_url=vm.ORIGIN,json=body,headers={'Origin':vm.ORIGIN,**({'X-CSRF-Token':'token'} if csrf else {})})
    def movie(self,day='2099-01-01'):
        item=dict(id='test:movie:12',kind='movie',title='Test Film',year='2099',externalId='12',releaseDate=day)
        engine.prepare(self.app,self.store,item);return item
    def test_auth_csrf_and_invalid_item(self):
        self.assertNotEqual(self.app.test_client().post('/api/discovery-engine/title',base_url=vm.ORIGIN,json={}).status_code,200)
        self.assertEqual(self.post('title',{},False).status_code,403)
        self.assertEqual(self.post('title',{}).status_code,400)
    def test_future_movie_persists_without_starting_a_download(self):
        item=self.movie()
        with patch.object(engine,'schedule'):
            self.assertEqual(self.post('download',dict(key='movie:12')).status_code,202)
        row=engine.title_data(self.app,self.store,item)['items'][0]
        self.assertTrue(row['requested']);self.assertEqual(row['date'],'2099-01-01')
        with self.store.connect() as db:r=dict(db.execute('SELECT * FROM discover_requests').fetchone())
        with patch.object(engine,'fallback_vod') as vod,patch.object(engine,'fallback_youtube') as yt:
            engine.resolve(self.app,self.store,r)
        vod.assert_not_called();yt.assert_not_called()
        self.assertEqual(ds.public_requests(self.store)[0]['state'],'waiting')
    def test_duplicate_request_is_idempotent(self):
        self.movie()
        with patch.object(engine,'schedule'):
            engine.queue(self.app,self.store,'movie:12');engine.queue(self.app,self.store,'movie:12')
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM discover_requests').fetchone()[0],1)
    def test_movie_title_poll_reports_current_transfer_without_mutating_request(self):
        item=self.movie('2020-01-01')
        ds.insert_request(self.store,dict(idempotency='popup-live',kind='movie',title='Test Film',quality='1080',fallback=False,episode_key='movie:12',state='downloading',engine='nzb',engine_id='popup-live',progress=0,body={}))
        live=Mock(stop=threading.Event())
        live.job.return_value=dict(id='popup-live',state='downloading',progress=.35,progress_stage='downloading',total_bytes=1700000000,published=[],error='')
        with patch.dict(self.app.extensions,nzb_engine=live):
            first=self.post('title',dict(item=item)).json['items'][0]
            live.job.return_value={**live.job.return_value,'progress':.52}
            second=self.post('title',dict(item=item)).json['items'][0]
        self.assertEqual((first['progress'],second['progress']),(35,52))
        self.assertEqual(first['estimatedBytes'],1700000000)
        self.assertTrue(first['requested']);self.assertEqual(first['requestState'],'downloading')
        with self.store.connect() as db:
            self.assertEqual(tuple(db.execute("SELECT state,progress FROM discover_requests WHERE engine_id='popup-live'").fetchone()),('downloading',0))
    def test_background_warmup_honours_saved_catalogue_without_stopping_monitor_loop(self):
        self.store.set('catalogue_pre_download','true')
        with patch.object(engine,'cached_browse') as browse:
            engine.warm_first_pages(self.app,self.store)
        browse.assert_not_called()
        self.store.set('catalogue_pre_download','false')
        with patch.object(engine,'cached_browse') as browse:
            engine.warm_first_pages(self.app,self.store)
        self.assertEqual([call.args[2]['media'] for call in browse.call_args_list],['movies','shows'])
    def test_no_nzb_falls_back_to_vod_first(self):
        item=self.movie('2020-01-01')
        with patch.object(engine,'schedule'):engine.queue(self.app,self.store,'movie:12')
        with self.store.connect() as db:r=dict(db.execute('SELECT * FROM discover_requests').fetchone())
        with patch.object(engine.sources,'indexers_for',side_effect=ValueError()),patch.object(engine,'fallback_vod',return_value=True) as vod,patch.object(engine,'fallback_youtube') as yt:
            engine.resolve(self.app,self.store,r)
        vod.assert_called_once();yt.assert_not_called()
    def test_no_release_waits_and_global_interval_controls_retry(self):
        self.movie('2020-01-01')
        with patch.object(engine,'schedule'):engine.queue(self.app,self.store,'movie:12')
        with self.store.connect() as db:r=dict(db.execute('SELECT * FROM discover_requests').fetchone())
        with patch.object(engine.sources,'indexers_for',side_effect=ValueError()),patch.object(engine,'fallback_vod',return_value=False),patch.object(engine,'fallback_youtube',return_value=False):
            engine.resolve(self.app,self.store,r)
        self.assertEqual(ds.public_requests(self.store)[0]['state'],'waiting')
        self.store.set('discover_engine_interval','60')
        with self.store.connect() as db:db.execute('UPDATE discover_requests SET updated=?',(time.time()-1200,))
        with patch.object(engine,'resolve') as resolve:engine.tick(self.app,self.store)
        resolve.assert_not_called()
    def test_cancelled_request_cannot_restart(self):
        self.movie('2020-01-01')
        with patch.object(engine,'schedule'):engine.queue(self.app,self.store,'movie:12')
        with self.store.connect() as db:r=dict(db.execute('SELECT * FROM discover_requests').fetchone())
        ds.cancel_request(self.store,r['id'])
        with patch.object(engine,'fallback_vod') as vod:engine.resolve(self.app,self.store,r)
        vod.assert_not_called()
    def test_vod_cancel_uses_existing_job(self):
        self.movie('2020-01-01')
        with patch.object(engine,'schedule'):engine.queue(self.app,self.store,'movie:12')
        with self.store.connect() as db:
            r=dict(db.execute('SELECT * FROM discover_requests').fetchone())
            db.execute("INSERT INTO m3u_downloads VALUES('job','source','file','Test','movies','/tmp/no-file','queued',0,'',0,0)")
        ds.update_request(self.store,r['id'],engine='vod',engine_id='job',state='queued')
        from backend.acquisition import cancel_discover
        cancel_discover(self.app,self.store,r['id'])
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT state FROM m3u_downloads WHERE id='job'").fetchone()[0],'cancelled')
    def test_public_html_catalogue_needs_no_api_and_preserves_future_date(self):
        p=Cards();p.feed('<div data-object-id="x"><a href="/movie/12-film"><img class="poster" alt="Film" src="https://media.themoviedb.org/t/p/w500/a.jpg"></a><span class="release_date">21 October 2027</span></div>')
        self.assertEqual(p.items[0]['releaseDate'],'2027-10-21')
        self.assertEqual(p.items[0]['id'],'tmdb:movie:12');self.assertNotIn('rating',p.items[0])
    def test_cached_browse_returns_immediately_while_provider_is_slow(self):
        event=threading.Event()
        def browse(*args):event.wait(1);return dict(items=[],nextOffset=None)
        args=dict(media='movies',q='',sort='source',offset=0,collection='',category='all',cursor=None)
        with patch('backend.view_media.adapters.REGISTRY',{'discover':{'browse':browse}}):
            start=time.monotonic();result=engine.cached_browse(self.app,self.store,args)
            self.assertLess(time.monotonic()-start,.2);self.assertTrue(result['loading'])
            event.set()
            deadline=time.monotonic()+2
            while engine._PENDING and time.monotonic()<deadline:time.sleep(.02)
    def test_sort_changes_share_prepared_page_and_never_query_again(self):
        args=engine.default_browse_args()
        reply=dict(items=[dict(id='1',title='Zulu',year='2026'),dict(id='2',title='Alpha',year='1999')],nextOffset=None)
        with patch('backend.view_media.adapters.REGISTRY',{'discover':{'browse':lambda *a:reply}}),patch.object(engine,'schedule',side_effect=lambda app,store,key,fn:fn()) as scheduled:
            engine.cached_browse(self.app,self.store,args)
            latest=engine.cached_browse(self.app,self.store,args)
            alpha=engine.cached_browse(self.app,self.store,dict(args,sort='title'))
        self.assertEqual(scheduled.call_count,1)
        self.assertEqual([r['id'] for r in latest['items']],['1','2'])
        self.assertEqual([r['id'] for r in alpha['items']],['2','1'])

    def test_two_batches_prepare_ahead_even_after_partial_provider_failure(self):
        seen=[]
        def browse(app,store,args):
            n=args['offset'];seen.append(n)
            return dict(items=[dict(id=str(n),title='Film '+str(n))],nextOffset=n+1,nextCursor=json.dumps({'movies':(n+1)*20}),incomplete=n==0,retryAt=time.time()+60 if n==0 else None)
        with patch('backend.view_media.adapters.REGISTRY',{'discover':{'browse':browse}}),patch.object(engine,'schedule',side_effect=lambda app,store,key,fn:fn()):
            engine.cached_browse(self.app,self.store,engine.default_browse_args())
        self.assertEqual(seen,[0,1,2],'Lookahead must be bounded and continue past one incomplete page')

    def test_legacy_movie_gets_missing_release_date_without_resetting_policy(self):
        item=self.movie('')
        with self.store.connect() as db:db.execute("UPDATE managed_media SET hold=1,quality='2160',monitor=0 WHERE key='movie:12'")
        result=engine.title_data(self.app,self.store,dict(item,releaseDate='2010-07-15'))
        self.assertEqual(result['items'][0]['date'],'2010-07-15')
        row=records.row(self.store,'movie:12')
        self.assertEqual((row['hold'],row['quality'],row['monitor']),(1,'2160',0))
        engine.title_data(self.app,self.store,dict(item,releaseDate='2012-01-01'))
        self.assertEqual(json.loads(records.row(self.store,'movie:12')['body'])['date'],'2010-07-15')

    def test_slow_preloading_does_not_block_opening_a_movie(self):
        release=threading.Event();opened=threading.Event()
        try:
            for n in range(4):engine.schedule(self.app,self.store,'engine:nzb-browse:slow:'+str(n),lambda:release.wait(3))
            engine.schedule(self.app,self.store,'title:immediate',opened.set)
            self.assertTrue(opened.wait(.5),'Movie preparation must not wait behind catalogue preload')
        finally:
            release.set()
            deadline=time.monotonic()+3
            while any(key[0]==str(self.store.directory) for key in engine._PENDING) and time.monotonic()<deadline:time.sleep(.01)

    def test_partial_snapshot_from_other_process_keeps_polling_for_metadata(self):
        from backend.view_media.providers import initialise
        initialise(self.store)
        args=engine.default_browse_args()
        with self.store.connect() as db:
            db.execute('INSERT INTO view_media_cache VALUES(?,?,?)',
                (engine.browse_cache_key(args,self.store),json.dumps(dict(items=[dict(id='1',title='Preparing')],nextOffset=1,nextCursor='{"nzb":100}',preparing=True)),time.time()))
        with patch.object(engine,'schedule') as schedule:
            result=engine.cached_browse(self.app,self.store,args)
        self.assertTrue(result['refreshing']);self.assertFalse(result['loading'])
        self.assertEqual(result['items'][0]['title'],'Preparing')
        schedule.assert_not_called()

    def test_incomplete_refresh_retains_confirmed_cards_and_retries_after_one_minute(self):
        from backend.view_media.providers import initialise
        initialise(self.store)
        args=engine.default_browse_args();key=engine.browse_cache_key(args,self.store)
        with self.store.connect() as db:
            db.execute('INSERT INTO view_media_cache VALUES(?,?,?)',(key,json.dumps(dict(items=[dict(id='known',title='Known movie')],nextOffset=None)),time.time()-301))
        incomplete=dict(items=[],nextOffset=None,incomplete=True,retryAt=time.time()+60)
        with patch('backend.view_media.adapters.REGISTRY',{'discover':{'browse':lambda *a:incomplete}}),patch.object(engine,'schedule',side_effect=lambda app,store,key,fn:fn()):
            engine.cached_browse(self.app,self.store,args)
        with patch.object(engine,'schedule') as schedule:page=engine.cached_browse(self.app,self.store,args)
        self.assertEqual(page['items'][0]['id'],'known');self.assertTrue(page['incomplete']);schedule.assert_not_called()
        with self.store.connect() as db:
            body=json.loads(db.execute('SELECT body FROM view_media_cache WHERE key=?',(key,)).fetchone()[0]);body['retryAt']=time.time()-1
            db.execute('UPDATE view_media_cache SET body=? WHERE key=?',(json.dumps(body),key))
        with patch.object(engine,'schedule') as schedule:engine.cached_browse(self.app,self.store,args)
        schedule.assert_called_once()

    def test_nzb_cache_does_not_reuse_old_mixed_discover_pages(self):
        from backend.view_media.providers import initialise
        initialise(self.store)
        args=engine.default_browse_args()
        old='engine:browse:'+json.dumps(args,sort_keys=True,separators=(',',':'))
        with self.store.connect() as db:
            db.execute('INSERT INTO view_media_cache VALUES(?,?,?)',
                       (old,json.dumps(dict(items=[dict(id='vod:old')],nextOffset=None)),time.time()))
        with patch.object(engine,'schedule'):
            result=engine.cached_browse(self.app,self.store,args)
        self.assertEqual(result['items'],[])
        self.assertNotEqual(engine.browse_cache_key(args,self.store),old)

    def test_nzb_cache_changes_when_enabled_accounts_change(self):
        args=engine.default_browse_args()
        with patch.object(engine.sources,'indexers_for',return_value=[dict(api_key='private-one')]):
            first=engine.browse_cache_key(args,self.store)
        with patch.object(engine.sources,'indexers_for',return_value=[dict(api_key='private-two')]):
            second=engine.browse_cache_key(args,self.store)
        self.assertNotEqual(first,second)
        self.assertNotIn('private',first+second)

    def test_added_indexer_keeps_snapshot_from_still_enabled_accounts(self):
        from backend.view_media.providers import initialise
        initialise(self.store);args=engine.default_browse_args()
        before=[dict(id='one',api_key='private-one')]
        with patch.object(engine.sources,'indexers_for',return_value=before):key=engine.browse_cache_key(args,self.store)
        with self.store.connect() as db:db.execute('INSERT INTO view_media_cache VALUES(?,?,?)',(key,json.dumps(dict(items=[dict(id='known',title='Known')],nextOffset=None)),time.time()))
        with patch.object(engine.sources,'indexers_for',return_value=[*before,dict(id='two',api_key='private-two')]),patch.object(engine,'schedule') as scheduled:
            page=engine.cached_browse(self.app,self.store,args)
        self.assertEqual(page['items'][0]['id'],'known');self.assertFalse(page['loading'])
        scheduled.assert_called_once()

    def test_expired_nzb_page_queries_api_and_publishes_new_titles(self):
        args=dict(engine.default_browse_args(),collection='all')
        def immediate(app,store,key,fn):fn()
        replies=[dict(items=[dict(id='movie:1',title='First',kind='movie')]),
                 dict(items=[dict(id='movie:2',title='New release',kind='movie')])]
        with patch.object(engine.sources,'indexers_for',return_value=[{}]),patch('backend.discover_search.search',side_effect=replies) as api,patch.object(engine,'schedule',side_effect=immediate):
            engine.cached_browse(self.app,self.store,args)
            self.assertEqual(engine.cached_browse(self.app,self.store,args)['items'][0]['title'],'First')
            with self.store.connect() as db:
                db.execute('UPDATE view_media_cache SET updated=?',(time.time()-301,))
            engine.cached_browse(self.app,self.store,args)
            page=engine.cached_browse(self.app,self.store,args)
        self.assertEqual(page['items'][0]['title'],'New release')
        self.assertEqual(api.call_count,2)

    def test_vod_size_preferences_validate_both_bounds(self):
        settings.save(self.store,'m3u',dict(min_download_bytes=1_000_000,max_download_bytes=2_000_000))
        self.assertEqual(settings.read(self.store,'m3u')['max_download_bytes'],2_000_000)
        with self.assertRaises(ValueError):settings.save(self.store,'m3u',dict(min_download_bytes=3_000_000))

    def test_future_request_starts_existing_sources_after_release(self):
        self.movie()
        with patch.object(engine,'schedule'):engine.queue(self.app,self.store,'movie:12')
        with self.store.connect() as db:r=dict(db.execute('SELECT * FROM discover_requests').fetchone())
        with patch.object(engine.service,'today',return_value='2100-01-01'),patch.object(engine.sources,'indexers_for',side_effect=ValueError()),patch.object(engine,'fallback_vod',return_value=True) as vod:
            engine.resolve(self.app,self.store,r)
        vod.assert_called_once()
    def test_removed_or_held_entry_does_not_automatically_reacquire(self):
        self.movie('2020-01-01')
        with patch.object(engine,'schedule'):engine.queue(self.app,self.store,'movie:12')
        with self.store.connect() as db:
            r=dict(db.execute('SELECT * FROM discover_requests').fetchone());db.execute("UPDATE managed_media SET hold=1 WHERE key='movie:12'")
        with patch.object(engine,'fallback_vod') as vod:engine.resolve(self.app,self.store,r)
        vod.assert_not_called()
    def test_youtube_trailer_is_not_a_movie_fallback(self):
        row=dict(kind='movie',id='request',title='Film')
        b=dict(item=dict(title='Film',year='2026'))
        with patch('backend.youtube_discover._search',return_value={'items':[dict(title='Film 2026 Official Trailer',videoId='abcdefghijk')]}),patch('backend.discover_youtube.register_video') as register:
            self.assertFalse(engine.fallback_youtube(self.app,self.store,row,b))
        register.assert_not_called()
    def series(self):
        item=dict(id='test:show:77',kind='show',title='Show',externalId='77')
        for number in range(1,46):
            records.remember(self.store,'show:77:2:'+str(number),item['id'],'episode',
                dict(item=item,episode=dict(id=number,season=2,number=number,name='Episode '+str(number),summary='Episode summary'),
                     date='2020-02-%02d'%(number%28+1),dateLabel='Air date',automatic=True))
        records.remember(self.store,'show:77:1:1',item['id'],'episode',dict(item=item,episode=dict(id=99,season=1,number=1,name='Pilot'),date='2020-01-01',automatic=True))
        return item
    def test_episode_pages_search_and_date_order(self):
        item=self.series()
        first=engine.title_data(self.app,self.store,item)
        second=engine.title_data(self.app,self.store,item,offset=first['nextOffset'])
        self.assertEqual(len(first['items']),30);self.assertEqual(len(second['items']),16)
        self.assertFalse(set(r['key'] for r in first['items'])&set(r['key'] for r in second['items']))
        self.assertEqual(first['items'][0]['description'],'Episode summary')
        result=engine.title_data(self.app,self.store,item,query='S02E03')
        self.assertEqual([r['number'] for r in result['items']],[3])
        self.assertEqual(engine.title_data(self.app,self.store,item,query='Season 1')['items'][0]['number'],1)
    def test_season_filter_keeps_search_paging_and_complete_season_choices(self):
        item=self.series()
        first=engine.title_data(self.app,self.store,item,season=2)
        second=engine.title_data(self.app,self.store,item,offset=first['nextOffset'],season=2)
        self.assertEqual(first['seasons'],[1,2]);self.assertEqual(len(first['items']),30);self.assertEqual(len(second['items']),15)
        self.assertTrue(all(r['season']==2 for r in first['items']+second['items']))
        pilot=engine.title_data(self.app,self.store,item,query='Pilot',season=1)
        self.assertEqual([r['number'] for r in pilot['items']],[1]);self.assertEqual(pilot['seasons'],[1,2])
        self.assertEqual(engine.title_data(self.app,self.store,item,query='Pilot',season=2)['items'],[])
        self.assertEqual(self.post('title',dict(item=item,season=True)).status_code,400)
        self.assertEqual(self.post('title',dict(item=item,season=-1)).status_code,400)
        self.assertEqual(self.post('title',dict(item=item,season=1)).json['items'][0]['title'],'Pilot')

    def test_all_episodes_queues_released_and_future_scope_persists(self):
        item=self.series()
        with patch.object(engine,'schedule'):
            engine.save_plan(self.app,self.store,item['id'],'all',True)
        with self.store.connect() as db:plan=dict(db.execute('SELECT * FROM discover_series_plan').fetchone())
        with patch.object(engine,'queue') as queue:engine.apply_plan(self.app,self.store,plan)
        self.assertEqual(queue.call_count,46)
        with patch.object(engine,'schedule'):engine.save_plan(self.app,self.store,item['id'],'future',True)
        self.assertTrue(engine.title_data(self.app,self.store,item)['downloadFuture'])
    def test_future_scope_finds_newly_announced_episode(self):
        item=self.series()
        with patch.object(engine,'schedule'):engine.save_plan(self.app,self.store,item['id'],'future',True)
        records.remember(self.store,'show:77:3:1',item['id'],'episode',dict(item=item,episode=dict(id=100,season=3,number=1,name='New episode'),date='2099-01-01',automatic=True))
        with self.store.connect() as db:plan=dict(db.execute('SELECT * FROM discover_series_plan').fetchone())
        with patch.object(engine,'prepare'),patch.object(engine,'queue') as queue:engine.apply_plan(self.app,self.store,plan,refresh=True)
        queue.assert_called_once()
        self.assertEqual(queue.call_args.args,(self.app,self.store,'show:77:3:1','future'))
        claim=queue.call_args.kwargs['plan_claim']
        self.assertEqual(claim['parent'],item['id']);self.assertFalse(claim['all_work'])
    def test_release_interval_saves_in_auto_download_only(self):
        settings.save(self.store,'auto-download',dict(release_check_minutes=45))
        self.assertEqual(engine.preferences(self.store)['release_check_minutes'],45)
        self.store.set('storage_base',str(self.root))
        with self.app.app_context():self.assertNotIn('release_check_minutes',settings.read(self.store,'nzb'))
