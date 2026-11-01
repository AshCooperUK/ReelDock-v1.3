"""Fast saved landing and provider progress preserve existing media contracts."""
import json
import time
import unittest
from unittest.mock import patch
import test_view_media as vm
from backend import discovery_engine as engine
from backend.view_media import saved_catalogue as saved, saved_search, search_snapshot, adapters
from backend import discover_sources


class SavedDiscoverLandingTests(unittest.TestCase):
    setUp=vm.ViewMediaTests.setUp
    tearDown=vm.ViewMediaTests.tearDown
    get=vm.ViewMediaTests.get

    def index(self):
        self.store.set('catalogue_pre_download','true')
        saved.initialise(self.store)
        return saved.signature(self.store)

    def card(self,n,kind='movie',origin='NZB',title=None):
        raw=dict(id=kind+':'+str(n),kind=kind,title=title or 'Fast '+str(n).zfill(4),externalId=str(n),year='2026')
        action=dict(type='discover',item=raw) if origin=='NZB' else dict(type='youtube',id='video'+str(n)) if origin=='YouTube' else dict(type='local',id='local'+str(n)) if origin=='Local Library' else dict(type='vod',source='playlist',item=dict(title=raw['title'],path='folder/'+str(n),kind='series'))
        return dict(adapters.card(raw,action),origin=origin)

    def args(self,**extra):
        return dict(dict(media='all',q='Fast',sort='title',offset=0,cursor=None,grouped=True),**extra)

    def cached(self,items,**overrides):
        saved.initialise_cache(self.store)
        args=dict(media='shows',q='',sort='source',offset=0,collection='all',category='all',cursor=None)
        args.update(overrides);key=engine.browse_cache_key(args,self.store)
        with self.store.connect() as db:db.execute('INSERT OR REPLACE INTO view_media_cache VALUES(?,?,?)',(key,json.dumps(dict(items=items)),time.time()))
        return key

    def dump(self):
        with self.store.connect() as db:return list(db.iterdump())

    def local_file(self,relative,nfo=None,record=False):
        """An isolated published fixture, never a provider or video download."""
        path=self.root/relative;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'synthetic saved video')
        if nfo:path.with_suffix('.nfo').write_text(nfo)
        if record:
            import hashlib
            identity=hashlib.sha256(relative.encode()).hexdigest()[:16]
            with self.store.connect() as db:
                db.execute("INSERT INTO downloads(id,title,quality,state,created,updated) VALUES(?,'Fixture','1080','complete',1,2)",(identity,))
                number=db.execute('SELECT count(*) FROM media_files').fetchone()[0]+1
                db.execute('INSERT INTO media_files(id,channel,path,day,episode,profile,bytes) VALUES(?,?,?,?,?,?,?)',
                           (identity,'',relative,'2026-01-01',number,'emby',path.stat().st_size))
        return path

    def episode(self,folder,season,number,record=False,show='Shared series'):
        return self.local_file(folder+f'/Season {season:02d}/Fixture S{season:02d}E{number:04d}.mkv',
            f'<episodedetails><title>Episode {number}</title><showtitle>{show}</showtitle><season>{season}</season><episode>{number}</episode><year>2026</year></episodedetails>',record)

    def test_new_or_disabled_catalogue_is_read_only_and_never_schedules_sources(self):
        with patch.object(engine,'schedule') as schedule,patch.object(adapters,'discover') as providers:
            result=self.get('/api/view-media?source=all')
            self.assertEqual(result.status_code,200,result.json)
            self.assertEqual(result.json['items'],[])
            self.assertFalse(result.json['loading'])
            with self.store.connect() as db:
                self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='saved_media_catalogue'").fetchone())
            schedule.assert_not_called();providers.assert_not_called()
        self.assertNotEqual(self.app.test_client().get('/api/view-media?source=all',base_url=vm.ORIGIN).status_code,200)

    def test_saved_cards_paginate_all_origins_and_preserve_actions_without_provider_calls(self):
        cards=[self.card(n) for n in range(1,30)]
        cards += [self.card(31,'movie','Local Library'),self.card(32,'video','YouTube')]
        # Current playlist rows contribute one grouped series even when no
        # background catalogue worker has run; stale per-file observations do not.
        with self.store.connect() as db:
            db.execute("INSERT INTO m3u_sources(id,name,url,options,folder) VALUES('vod','Fixture playlist','https://private.test/source','{}','vod')")
            db.execute("INSERT INTO m3u_files VALUES('vod','Fixture/S01E01.strm','Fixture S01E01','series','','hash')")
        sig=self.index()
        for origin in ('NZB','VOD','Local Library','YouTube'):
            saved.remember(self.store,sig,origin,[item for item in cards if item['origin']==origin])
        with patch.object(engine,'schedule') as schedule,patch.object(adapters,'discover') as providers:
            first=self.get('/api/view-media?source=all&sort=title').json
            self.assertEqual(len(first['items']),24);self.assertFalse(first['completeSet']);self.assertEqual(first['nextOffset'],24)
            second=self.get('/api/view-media?source=all&sort=title&offset=24').json
            self.assertTrue(second['completeSet']);self.assertIsNone(second['nextOffset'])
            result=first['items']+second['items']
            self.assertEqual(len(result),32);self.assertEqual(len({row['id'] for row in result}),32)
            self.assertEqual({row['origin'] for row in result},{'NZB','VOD','Local Library','YouTube'})
            self.assertEqual({row['action']['type'] for row in result},{'discover','vod-group','local','youtube'})
            self.assertFalse(first['windowLimited']);self.assertIn('No provider requests',first['freshness']['scope'])
            self.assertEqual([item['kind'] for item in self.get('/api/view-media?source=all&media=youtube').json['items']],['video'])
            schedule.assert_not_called();providers.assert_not_called()
        for params in ('media=bad','sort=release','offset=-1','q=Fast','cursor={}'):self.assertEqual(self.get('/api/view-media?source=all&'+params).status_code,400)

    def test_window_limit_is_explicit_and_old_signature_is_not_visible(self):
        sig=self.index()
        saved.remember(self.store,sig,'NZB',[self.card(n) for n in range(1,1005)])
        result=self.get('/api/view-media?source=all').json
        self.assertTrue(result['windowLimited']);self.assertIn('Latest 1000 saved observations',result['freshness']['scope'])
        self.store.set('m3u_tmdb_token','different-account')
        self.assertEqual(self.get('/api/view-media?source=all').json['items'],[])

    def test_provider_completion_reports_new_matches_without_reordering_and_refresh_does_not_restart(self):
        sig=self.index();saved.remember(self.store,sig,'NZB',[self.card(2,title='Fast B')]);tasks=[]
        with patch.object(engine,'schedule',side_effect=lambda app,store,key,fn:tasks.append(fn)),patch.object(search_snapshot,'collect',return_value=dict(items=[self.card(1,title='Fast A')],warnings=[])) as collect:
            first=saved.search(self.app,self.store,self.args())
            self.assertTrue(first['providerChecksPending']);self.assertEqual(first['refreshAfterMs'],2000)
            self.assertFalse(first['searchUpdatesAvailable']);self.assertFalse(first['loading']);collect.assert_not_called()
            tasks[0]()
            complete=saved.search(self.app,self.store,self.args())
            self.assertEqual(first['items'],complete['items']);self.assertEqual(first['searchSnapshot'],complete['searchSnapshot'])
            self.assertFalse(complete['providerChecksPending']);self.assertTrue(complete['providerChecksComplete']);self.assertEqual(complete['refreshAfterMs'],0)
            self.assertTrue(complete['searchUpdatesAvailable'])
            refreshed=saved.search(self.app,self.store,self.args(fresh=True))
            self.assertEqual([item['title'] for item in refreshed['items']],['Fast A','Fast B'])
            self.assertFalse(refreshed['searchUpdatesAvailable']);self.assertEqual(len(tasks),1);collect.assert_called_once()

    def test_provider_completion_with_existing_matches_does_not_offer_false_new_results(self):
        sig=self.index();item=self.card(2);saved.remember(self.store,sig,'NZB',[item]);tasks=[]
        with patch.object(engine,'schedule',side_effect=lambda app,store,key,fn:tasks.append(fn)),patch.object(search_snapshot,'collect',return_value=dict(items=[item],warnings=[])):
            first=saved.search(self.app,self.store,self.args());tasks[0]()
            completed=saved.search(self.app,self.store,self.args())
            self.assertEqual(first['searchSnapshot'],completed['searchSnapshot']);self.assertTrue(completed['providerChecksComplete'])
            self.assertFalse(completed['searchUpdatesAvailable'])

    def test_default_off_reuses_nzb_browse_without_provider_scan_or_database_writes(self):
        self.cached([self.card(99,'show',title='Already browsed series')]);before=self.dump()
        with patch.object(engine,'schedule',side_effect=AssertionError('No provider scheduling')),patch.object(adapters,'discover',side_effect=AssertionError('No provider')),patch.object(adapters.local,'inventory',side_effect=AssertionError('No new scan')):
            response=self.get('/api/view-media?source=all&media=all');self.assertEqual(response.status_code,200,response.json)
        self.assertEqual([row['title'] for row in response.json['items']],['Already browsed series'])
        self.assertEqual(response.json['items'][0]['action']['item']['externalId'],'99')
        self.assertEqual(response.json['observationState'],'ready');self.assertEqual(response.json['refreshAfterMs'],10000)
        self.assertNotEqual(self.store.get('catalogue_pre_download'),'true');self.assertEqual(self.dump(),before)

    def test_exact_current_indexer_credentials_are_required_for_cached_observations(self):
        with patch.object(discover_sources,'indexers_for',return_value=[dict(url='https://fixture.test/api',api_key='PRIVATE_ONE')]) as indexers:
            self.cached([self.card(99,'show')]);self.assertTrue(self.get('/api/view-media?source=all').json['items'])
            indexers.return_value=[dict(url='https://fixture.test/api',api_key='PRIVATE_TWO')]
            changed=self.get('/api/view-media?source=all').json
        self.assertEqual(changed['items'],[]);self.assertEqual(changed['observationState'],'unindexed')
        self.assertNotIn('PRIVATE',json.dumps(changed))

    def test_global_cached_frames_reuse_public_nzb_but_not_old_private_source_cards(self):
        self.cached([self.card(7,'show'),self.card(8,'show','VOD',title='PRIVATE old playlist')],media='all',q='saved query',collection='',**{'global':True})
        result=self.get('/api/view-media?source=all').json
        self.assertEqual([row['id'] for row in result['items']],['show:7']);self.assertNotIn('PRIVATE',json.dumps(result))

    def test_current_youtube_vod_and_completed_files_work_without_worker_index(self):
        channel='UC'+'a'*22;identity='abcdefghijk'
        folder=self.root/'Movies'/'Known movie';folder.mkdir(parents=True);video=folder/'Known movie.mkv';video.write_bytes(b'published video')
        video.with_suffix('.nfo').write_text('<movie><title>Known local movie</title><runtime>91</runtime></movie>')
        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES(?,?,1)',(channel,json.dumps(dict(id=channel,name='Known channel'))))
            db.execute('INSERT INTO videos VALUES(?,?,?,?,?,0)',(identity,channel,'2026-01-01','2026-01-01T00:00:00+00:00',json.dumps(dict(id=identity,title='Known upload',state='published',duration_seconds=45))))
            db.execute("INSERT INTO downloads(id,title,quality,state,created,updated) VALUES('localrecord','Local','1080','complete',1,2)")
            db.execute('INSERT INTO media_files(id,channel,path,day,episode,profile,bytes) VALUES(?,?,?,?,?,?,?)',('localrecord','',str(video.relative_to(self.root)),'2026-01-01',1,'emby',video.stat().st_size))
            db.execute("INSERT INTO m3u_sources(id,name,url,options,folder) VALUES('vod','Known playlist','https://private.test/source','{}','vod')")
            db.execute("INSERT INTO m3u_files VALUES('vod','movie.strm','Known VOD','movies','','hash')")
            db.execute("INSERT INTO m3u_file_info VALUES('vod','movie.strm',?)",(json.dumps(dict(durationSeconds=3600)),))
        before=self.dump()
        with patch.object(adapters.local,'inventory',side_effect=AssertionError('No full scan')),patch.object(adapters.local,'_scan',side_effect=AssertionError('No background scan')):
            result=self.get('/api/view-media?source=all').json
        origins={row['origin']:row for row in result['items']};self.assertEqual(set(origins),{'VOD','YouTube','Local Library'})
        self.assertEqual(origins['YouTube']['action'],dict(type='youtube',id=identity));self.assertEqual(origins['YouTube']['durationSeconds'],45)
        self.assertEqual(origins['VOD']['action']['type'],'vod-group');self.assertEqual(origins['VOD']['_entry']['source'],'vod');self.assertEqual(origins['VOD']['_entry']['path'],'movie.strm');self.assertEqual(origins['VOD']['durationSeconds'],3600)
        local=origins['Local Library'];self.assertEqual(local['runtimeMinutes'],91);self.assertEqual(local['action']['type'],'local')
        self.assertEqual(adapters.local._CACHE[str(self.root)]['files'][local['action']['id']]['path'],video)
        self.assertEqual(self.dump(),before)
        with self.store.connect() as db:db.execute("UPDATE m3u_sources SET options='{\"enabled\":false}' WHERE id='vod'")
        self.assertTrue(any(row['origin']=='VOD' for row in self.get('/api/view-media?source=all').json['items']),'pausing automatic refresh keeps imported files available')
        from backend import m3u_removal
        with self.store.connect() as db:m3u_removal.queue(db,'vod')
        self.assertFalse(any(row['origin']=='VOD' for row in self.get('/api/view-media?source=all').json['items']))

    def test_known_completed_local_paths_reject_links_and_outside_mount(self):
        outside=self.root.parent/'outside.mkv';outside.write_bytes(b'private')
        link=self.root/'linked.mkv';link.symlink_to(outside)
        with self.store.connect() as db:
            for number,(identity,path) in enumerate([('outside',outside),('link',link)],1):
                db.execute("INSERT INTO downloads(id,title,quality,state,created,updated) VALUES(?,'Local','1080','complete',1,2)",(identity,))
                db.execute('INSERT INTO media_files(id,channel,path,day,episode,profile,bytes) VALUES(?,?,?,?,?,?,?)',(identity,'',str(path),'2026-01-01',number,'emby',7))
        with patch.object(adapters.local,'_entry',side_effect=AssertionError('Unsafe paths cannot be inspected')):
            result=self.get('/api/view-media?source=all').json
        self.assertEqual(result['items'],[]);self.assertNotIn('private',json.dumps(result))

    def test_empty_unindexed_state_refreshes_when_browsed_source_arrives(self):
        first=self.get('/api/view-media?source=all').json;self.assertEqual(first['observationState'],'unindexed')
        self.cached([self.card(7,'show')]);second=self.get('/api/view-media?source=all').json
        self.assertEqual(second['observationState'],'ready');self.assertNotEqual(first['observationRevision'],second['observationRevision'])
        self.assertEqual(second['observationIds'],['show:7']);self.assertGreater(first['refreshAfterMs'],0)

    def test_fresh_source_frame_enters_bounded_window_ahead_of_old_saved_rows(self):
        sig=self.index();saved.remember(self.store,sig,'NZB',[self.card(n,'show') for n in range(1,1002)])
        self.cached([self.card(9000,'show',title='Newly browsed result')])
        result=self.get('/api/view-media?source=all').json
        self.assertTrue(result['windowLimited']);self.assertEqual(len(result['observationIds']),1000)
        self.assertEqual(result['items'][0]['title'],'Newly browsed result')

    def test_changed_signature_during_observation_read_never_publishes_old_account_frame(self):
        self.cached([self.card(7,'show')]);old=saved.signature(self.store)
        with patch.object(saved,'signature',side_effect=[old,'new-account']):result=self.get('/api/view-media?source=all').json
        self.assertEqual(result['items'],[]);self.assertEqual(result['observationState'],'warming')
        self.assertEqual(result['observationSignature'],'new-account:0');self.assertEqual(result['refreshAfterMs'],2000)

    def test_all_media_groups_current_show_episodes_without_old_leaf_duplicates_or_writes(self):
        sig=self.index()
        paths=[self.episode('TV/Shared (2000)',2,4,True),self.episode('TV/Shared (2000)',1,2,True),
               self.episode('TV/Shared (2000)',1,1,True),self.episode('TV/Shared (2026)',1,1,True)]
        movie=self.local_file('Movies/One film/One film.mkv','<movie><title>One saved film</title></movie>',True)
        upload=self.local_file('YouTube/One creator/One upload.mp4',record=True)
        leaves=[]
        for path in paths:
            _,item,_=adapters.local._entry(self.root,path)
            leaves.append(dict(item,kind='show'))
        saved.remember(self.store,sig,'Local Library',leaves)
        before=self.dump()
        with patch.object(engine,'schedule',side_effect=AssertionError('No source scheduling')),\
             patch.object(adapters,'discover',side_effect=AssertionError('No provider calls')),\
             patch.object(adapters.local,'inventory',side_effect=AssertionError('No full inventory')),\
             patch.object(adapters.local,'_scan',side_effect=AssertionError('No background scan')):
            response=self.get('/api/view-media?source=all&media=all&sort=title')
            self.assertEqual(response.status_code,200,response.json)
            result=response.json
            shows=self.get('/api/view-media?source=all&media=shows&sort=title').json
        self.assertEqual(self.dump(),before,'opening the landing page cannot rewrite stored search leaves')
        self.assertEqual(len(result['items']),4,'two show folders, one movie and one upload')
        groups=[row for row in result['items'] if row['action']['type']=='local-group']
        self.assertEqual(len(groups),2,'same-name shows in different folders keep separate identities')
        self.assertEqual({row['title'] for row in groups},{'Shared series'})
        self.assertEqual(sorted(row['count'] for row in groups),[1,3],'even a single episode has a show parent')
        self.assertEqual(len({row['id'] for row in groups}),2)
        self.assertEqual(result['observationCounts']['Local Library'],4)
        self.assertEqual(set(result['observationIds']),{row['id'] for row in result['items']})
        self.assertEqual({row['id'] for row in shows['items']},{row['id'] for row in groups})
        self.assertEqual(shows['observationCounts']['Local Library'],2)
        cache=adapters.local._CACHE[str(self.root)]
        children=[child for group in groups for child in group['children']]
        self.assertEqual({child['id'] for child in children},{leaf['id'] for leaf in leaves})
        self.assertTrue(all(child['action']['type']=='local' for child in children))
        self.assertTrue(all(set(child['action'])=={'type','id'} and len(child['action']['id'])==32 for child in children))
        self.assertTrue(all('path' not in child and 'files' not in child and '_entry' not in child for child in children))
        self.assertEqual({cache['files'][child['action']['id']]['path'] for child in children},set(paths))
        three=next(row for row in groups if row['count']==3)
        self.assertEqual([(row['season'],row['number']) for row in three['children']],[(1,1),(1,2),(2,4)])
        self.assertEqual({cache['files'][row['action']['id']]['path'] for row in result['items'] if row['action']['type']=='local'},{movie,upload})
        self.assertNotIn(str(self.root),json.dumps(result),'absolute private filesystem paths do not enter the public cards')
        self.assertEqual({row['id'] for row in saved._cards(self.store,sig,origin='Local Library',kind='show')},{leaf['id'] for leaf in leaves},'persisted search observations stay per episode')
        with patch.object(adapters.local,'inventory',return_value=cache):
            searched=self.get('/api/view-media?source=local&media=shows&q=Episode').json['items']
        self.assertEqual({row['id'] for row in searched},{leaf['id'] for leaf in leaves})
        self.assertTrue(all(row['action']['type']=='local' for row in searched),'an explicit episode search retains its exact file actions')

    def test_grouped_local_parent_art_is_registered_against_the_original_safe_children(self):
        first=self.episode('TV/Art series',1,1,True,show='Art series')
        self.episode('TV/Art series',1,2,True,show='Art series')
        first.with_suffix('.jpg').write_bytes(b'episode-specific artwork')
        poster=self.root/'TV/Art series/poster.jpg';poster.write_bytes(b'show poster')
        banner=poster.with_name('fanart.jpg');banner.write_bytes(b'show fanart')
        before=self.dump()
        with patch.object(adapters.local,'inventory',side_effect=AssertionError('No full inventory')),\
             patch.object(adapters.local,'_scan',side_effect=AssertionError('No background scan')):
            result=self.get('/api/view-media?source=all&media=shows').json
        self.assertEqual(self.dump(),before)
        self.assertEqual(len(result['items']),1)
        group=result['items'][0];identity=group['action']['id'];cache=adapters.local._CACHE[str(self.root)]
        self.assertEqual(group['image'],'/api/view-media/local/'+identity+'/art')
        self.assertEqual(group['backdrop'],'/api/view-media/local/'+identity+'/backdrop')
        self.assertEqual(cache['files'][identity]['art'],poster)
        self.assertEqual(cache['files'][identity]['backdrop'],banner)
        with patch.object(adapters.local,'inventory',return_value=cache):
            for route,expected in [(group['image'],b'show poster'),(group['backdrop'],b'show fanart')]:
                picture=self.get(route)
                try:self.assertEqual(picture.data,expected)
                finally:picture.close()

    def test_local_group_public_dto_sanitizes_children_and_rejects_nested_or_remote_actions(self):
        identity='a'*32
        child=dict(id='local:'+identity,title='Safe episode',kind='shows',season=1,number=2,
                   path='PRIVATE raw path',files={'PRIVATE':'raw mapping'},_entry=dict(source='PRIVATE',path='PRIVATE',kind='series'),
                   action=dict(type='local',id=identity,path='PRIVATE action path'))
        groupid='group-'+'b'*24
        group=dict(id='local-group:'+groupid,title='Safe show',kind='shows',count=999,
                   action=dict(type='local-group',id=groupid),_entry=dict(source='PRIVATE',path='PRIVATE',kind='series'),
                   children=[child,dict(child,title='PRIVATE nested group',action=dict(type='local-group',id=groupid)),
                             dict(child,title='PRIVATE remote action',action=dict(type='vod',source='PRIVATE',item={})),
                             dict(child,title='PRIVATE unsafe id',action=dict(type='local',id='../PRIVATE'))])
        records=list(saved._records('fixture','Local Library',[group]))
        self.assertEqual(len(records),1)
        public=json.loads(records[0][5])
        self.assertEqual(public['count'],1)
        self.assertEqual(public['children'][0]['action'],dict(type='local',id=identity))
        self.assertEqual((public['children'][0]['season'],public['children'][0]['number']),(1,2))
        self.assertNotIn('PRIVATE',json.dumps(public))
        self.assertEqual(list(saved._records('fixture','NZB',[group])),[])

    def test_all_media_pages_count_show_parents_before_the_page_boundary(self):
        for number in range(26):
            for episode in (1,2):self.episode(f'TV/Series {number:02d}',1,episode,show=f'Series {number:02d}')
        state={};adapters.local._scan(self.root,state)
        before=self.dump()
        with patch.object(adapters.local,'inventory',side_effect=AssertionError('No new inventory')),\
             patch.object(adapters.local,'_scan',side_effect=AssertionError('No new scan')),\
             patch.object(engine,'schedule',side_effect=AssertionError('No provider scheduling')):
            first=self.get('/api/view-media?source=all&media=shows&sort=title').json
            second=self.get('/api/view-media?source=all&media=shows&sort=title&offset=24').json
        self.assertEqual(self.dump(),before)
        self.assertEqual((len(first['items']),first['nextOffset'],first['completeSet']),(24,24,False))
        self.assertEqual((len(second['items']),second['nextOffset'],second['completeSet']),(2,None,True))
        cards=first['items']+second['items']
        self.assertEqual(len({row['id'] for row in cards}),26)
        self.assertEqual(first['observationIds'],second['observationIds'])
        self.assertEqual(len(first['observationIds']),26)
        self.assertEqual(first['observationCounts']['Local Library'],26)
        self.assertTrue(all(row['action']['type']=='local-group' and row['count']==2 for row in cards))

    def test_large_cached_episode_run_does_not_fill_the_saved_title_window(self):
        # A saved series can exceed the 1,000-observation window by itself.
        # Another title must remain visible after that series is collapsed.
        for number in range(1,1003):
            self.local_file(f'TV/Packed series/Season 01/Packed series S01E{number:04d}.mkv')
        self.episode('TV/Following series',1,1,show='Following series')
        state={};adapters.local._scan(self.root,state)
        before=self.dump()
        with patch.object(adapters.local,'inventory',side_effect=AssertionError('No new inventory')),\
             patch.object(adapters.local,'_scan',side_effect=AssertionError('No new scan')),\
             patch.object(engine,'schedule',side_effect=AssertionError('No provider scheduling')):
            result=self.get('/api/view-media?source=all&media=shows&sort=title').json
        self.assertEqual(self.dump(),before)
        self.assertEqual(len(result['items']),2)
        self.assertEqual({row['title'] for row in result['items']},{'Packed series','Following series'})
        self.assertEqual(sorted(row['count'] for row in result['items']),[1,1002])
        self.assertEqual(len(result['observationIds']),2)
        self.assertEqual(result['observationCounts']['Local Library'],2)
        self.assertFalse(result['windowLimited'])


if __name__=='__main__':unittest.main()
