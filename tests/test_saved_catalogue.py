"""Local catalogue reads are immediate; only background jobs contact providers."""
import json
import time
import unittest
from unittest.mock import patch
import test_view_media as vm
from backend import discovery_engine as engine,settings
from backend.view_media import saved_catalogue as saved,adapters,search_snapshot

class SavedCatalogueTests(unittest.TestCase):
    setUp=vm.ViewMediaTests.setUp
    tearDown=vm.ViewMediaTests.tearDown
    get=vm.ViewMediaTests.get
    def setup_index(self):
        self.store.set('catalogue_pre_download','true');saved.initialise(self.store);return saved.signature(self.store)
    def item(self,n=1,kind='movie',title=None,day='2099-01-01'):
        raw=dict(id=kind+':'+str(n),kind=kind,title=title or 'Fast car '+str(n),externalId=str(n),releaseDate=day)
        return adapters.card(raw,dict(type='discover',item=raw))
    def args(self,**extra):return dict(media='all',q='fast',sort='title',offset=0,cursor=None,grouped=True,**extra)

    def test_settings_auth_csrf_atomic_interval_and_no_side_effect_on_existing_preferences(self):
        self.assertEqual(self.get('/api/settings/catalogue').json['values'],dict(enabled=False,minutes=60))
        self.assertNotEqual(self.app.test_client().get('/api/view-media/catalogue-status',base_url=vm.ORIGIN).status_code,200)
        for values in [dict(enabled=True,minutes=1),dict(enabled='true'),dict(minutes=60.0),dict(enabled=True,google_tokens='bad')]:
            response=self.client.post('/api/settings/catalogue',base_url=vm.ORIGIN,headers={'Origin':vm.ORIGIN,'X-CSRF-Token':'token'},json=dict(values=values))
            self.assertEqual(response.status_code,400);self.assertFalse(saved.enabled(self.store))
        response=self.client.post('/api/settings/catalogue',base_url=vm.ORIGIN,headers={'Origin':vm.ORIGIN},json=dict(values=dict(enabled=True)))
        self.assertEqual(response.status_code,403)
        self.store.set('download_quality','720');self.store.set('google_tokens','PRIVATE')
        settings.save(self.store,'catalogue',dict(enabled=True,minutes=180))
        self.assertEqual(saved.interval(self.store),10800);self.assertEqual(self.store.get('download_quality'),'720')
        self.assertNotIn('PRIVATE',json.dumps(saved.status(self.store)))

    def test_movie_and_tv_browse_return_whole_saved_list_without_live_calls(self):
        sig=self.setup_index()
        with patch.object(adapters,'discover') as remote:
            for media,kind in [('movies','movie'),('shows','show')]:
                items=[self.item(n,kind) for n in range(1,76)]
                saved.list_result(self.store,sig,media,'trending',dict(items=items,label='Trending'))
                seen=[];offset=0
                while True:
                    result=self.get('/api/view-media?engine=1&media='+media+'&collection=trending&offset='+str(offset)).json
                    self.assertIsNone(result['nextCursor']);seen+=result['items']
                    self.assertEqual(result['completeSet'],result['nextOffset'] is None)
                    if result['nextOffset'] is None:break
                    offset=result['nextOffset']
                self.assertTrue(result['completeSet']);self.assertEqual(len(seen),75)
        remote.assert_not_called()

    def test_view_all_uses_every_saved_source_title_not_a_named_list_or_search_window(self):
        sig=self.setup_index();items=[self.item(n+1,title='Film '+str(n).zfill(4)) for n in range(1105)]
        saved.remember(self.store,sig,'NZB',items)
        saved.remember(self.store,sig,'Catalogue',[self.item(9999,title='Future metadata only')])
        saved.remember(self.store,sig,'VOD',[self.item(8888,title='Other source')])
        saved.list_result(self.store,sig,'movies','all',dict(items=items[:3],label='Old limited snapshot'))
        with patch.object(adapters,'discover') as remote:
            seen=[];offset=0
            while True:
                result=self.get('/api/view-media?engine=1&media=movies&collection=all&sort=title_desc&offset='+str(offset)).json
                seen+=result['items']
                if result['nextOffset'] is None:break
                offset=result['nextOffset']
        remote.assert_not_called()
        self.assertEqual(len(seen),1105);self.assertEqual(seen[0]['title'],'Film 1104')
        self.assertEqual(seen[-1]['title'],'Film 0000');self.assertTrue(result['completeSet'])
        self.assertIsNone(result['nextCursor'])
        self.assertIn('load as you scroll',result['freshness']['scope'])
        # The explicitly selected search window remains independent.
        self.assertEqual(len(saved._cards(self.store,sig,origin='NZB',kind='movie')),1000)
        saved.remember(self.store,sig,'Local Library',items)
        local=saved.browse(self.app,self.store,dict(media='movies',collection='',sort='title'),'local')
        self.assertEqual(len(local['items']),24);self.assertEqual(local['nextOffset'],24)

    def test_all_crawl_resumes_past_three_batches_until_provider_end(self):
        sig=self.setup_index();offsets=[]
        def remote(app,store,args):
            start=(args['cursor'] or {}).get('nzb',0);offsets.append(start)
            return dict(items=[self.item(start+1)],nextCursor=json.dumps(dict(nzb=start+100)) if start<700 else None)
        with patch.object(adapters,'discover',side_effect=remote):
            for _ in range(3):saved.refresh_list(self.app,self.store,sig,'0','movies','all')
        self.assertEqual(offsets,list(range(0,800,100)))
        self.assertTrue(saved.state(self.store,saved.crawl_key(sig,'0','movies'))[0]['done'])
        result=saved.browse(self.app,self.store,dict(media='movies',collection='all',sort='title'))
        self.assertEqual(len(result['items']),8);self.assertIn('reached the end',result['freshness']['scope'])

    def test_all_crawl_retries_partial_page_without_skipping_or_erasing_titles(self):
        sig=self.setup_index();calls=[]
        def remote(app,store,args):
            start=(args['cursor'] or {}).get('nzb',0);calls.append(start)
            if len(calls)==2:return dict(items=[self.item(2)],nextCursor=json.dumps(dict(nzb=200)),warnings=['NZB indexer request limit reached: Indexer A.'])
            return dict(items=[self.item(start+1)],nextCursor=json.dumps(dict(nzb=100)) if start==0 else None)
        with patch.object(adapters,'discover',side_effect=remote):
            saved.refresh_all(self.app,self.store,sig,'0','movies')
            state=saved.state(self.store,saved.crawl_key(sig,'0','movies'))[0]
            self.assertEqual(state['cursor'],dict(nzb=100));self.assertFalse(state['done'])
            result=saved.browse(self.app,self.store,dict(media='movies',collection='all',sort='title'))
            self.assertIn('Indexer A',result['warnings'][0]);self.assertEqual(len(result['items']),2)
            with patch.object(saved,'seed'),patch.object(saved,'refresh_local',return_value=True),patch.object(saved,'targets',return_value=[('movies','all')]):
                saved.tick(self.app,self.store)
            self.assertEqual(calls,[0,100],'cooldown does not issue another request')
            saved.refresh_all(self.app,self.store,sig,'0','movies')
        self.assertEqual(calls,[0,100,100]);self.assertEqual(len(saved._cards(self.store,sig)),3)

    def test_missing_optional_tv_metadata_does_not_block_catalogue_pages(self):
        sig=self.setup_index();offsets=[]
        def releases(app,store,media,query,offset,**kwargs):
            offsets.append(offset);item=self.item(offset+1,'show',day='2020-01-01')['action']['item']
            return dict(items=[item],nextOffset=offset+100 if offset<700 else None,warnings=[])
        with patch.object(saved,'signature',return_value=sig),patch('backend.discover_sources.indexers_for',return_value=[{}]),patch('backend.discover_search.search',side_effect=releases),patch('backend.view_media.show_metadata.details',side_effect=ValueError('No exact mapping')):
            for _ in range(3):saved.refresh_all(self.app,self.store,sig,'0','shows')
        self.assertEqual(offsets,list(range(0,800,100)))
        progress=saved.state(self.store,saved.crawl_key(sig,'0','shows'))[0]
        self.assertTrue(progress['done']);self.assertEqual(progress['warnings'],[])
        page=saved.browse(self.app,self.store,dict(media='shows',collection='all',sort='release'))
        self.assertEqual(len(page['items']),8);self.assertEqual(page['warnings'],[])
        self.assertIn('Metadata retries automatically',page['freshness']['scope'])
        self.assertTrue(all(row['releaseDate']=='2020-01-01' for row in page['items']),'known dates survive lookup failures')

    def test_optional_tv_retry_is_independent_backed_off_and_clears_on_success(self):
        sig=self.setup_index();saved.remember(self.store,sig,'NZB',[self.item(1,'show',day='2020-01-01')])
        saved.queue_tv_metadata(self.store,sig,'0',['1']);prefix=saved.tv_retry_prefix(sig,'0')
        with patch('backend.view_media.show_metadata.details') as details:
            saved.retry_tv_metadata(self.store,sig,'0');details.assert_not_called()
        with self.store.connect() as db:db.execute("UPDATE view_media_cache SET body=json_set(body,'$.retryAt',0) WHERE key=?",(prefix+'1',))
        with patch('backend.view_media.show_metadata.details',side_effect=ValueError('Unavailable')) as details:
            saved.retry_tv_metadata(self.store,sig,'0');saved.retry_tv_metadata(self.store,sig,'0');self.assertEqual(details.call_count,1)
        pending=saved.state(self.store,'tv-metadata:'+sig+':0:1')[0];self.assertGreater(pending['retryAt'],time.time()+800)
        with self.store.connect() as db:db.execute("UPDATE view_media_cache SET body=json_set(body,'$.retryAt',0) WHERE key=?",(prefix+'1',))
        with patch('backend.view_media.show_metadata.details',return_value=dict(id='show:1',externalId='1',kind='show',title='Verified show',releaseDate='2021-03-04',metadataVerified=True,lastEpisodeReleased='')):
            saved.retry_tv_metadata(self.store,sig,'0')
        page=saved.browse(self.app,self.store,dict(media='shows',collection='all',sort='release'))
        self.assertEqual(page['items'][0]['releaseDate'],'2021-03-04');self.assertEqual(page['items'][0]['action']['item']['title'],'Verified show')
        self.assertNotIn('Metadata retries',page['freshness']['scope']);self.assertEqual(saved.state(self.store,'tv-metadata:'+sig+':0:1')[0],{})

    def test_tv_source_failure_still_blocks_cursor_even_with_optional_metadata_failure(self):
        sig=self.setup_index();item=self.item(1,'show')['action']['item']
        with patch.object(saved,'signature',return_value=sig),patch('backend.discover_sources.indexers_for',return_value=[{}]),patch('backend.discover_search.search',return_value=dict(items=[item],nextOffset=100,warnings=['Indexer A could not complete this search.'])),patch('backend.view_media.show_metadata.details',side_effect=ValueError('Missing')):
            saved.refresh_all(self.app,self.store,sig,'0','shows')
        state=saved.state(self.store,saved.crawl_key(sig,'0','shows'))[0]
        self.assertFalse(state['done']);self.assertEqual(state['offset'],0)
        self.assertEqual(state['warnings'],['Indexer A could not complete this search.'])

    def test_optional_tv_retry_completion_after_disable_cannot_change_saved_cards(self):
        sig=self.setup_index();saved.remember(self.store,sig,'NZB',[self.item(1,'show')]);saved.queue_tv_metadata(self.store,sig,'0',['1'])
        with self.store.connect() as db:db.execute("UPDATE view_media_cache SET body=json_set(body,'$.retryAt',0) WHERE key=?",(saved.tv_retry_prefix(sig,'0')+'1',))
        def finish(*args):
            settings.save(self.store,'catalogue',dict(enabled=False));return dict(title='Obsolete result',releaseDate='2020-01-01')
        with patch('backend.view_media.show_metadata.details',side_effect=finish):saved.retry_tv_metadata(self.store,sig,'0')
        self.assertEqual(saved._cards(self.store,sig)[0]['title'],'Fast car 1')

    def test_all_crawl_is_fair_between_media_and_fenced_after_disable(self):
        sig=self.setup_index();calls=[]
        def remote(app,store,args):
            calls.append(args['media']);start=(args['cursor'] or {}).get('nzb',0)
            return dict(items=[self.item(start+1)],nextCursor=json.dumps(dict(nzb=start+100)))
        with patch.object(saved,'seed'),patch.object(saved,'refresh_local',return_value=True),patch.object(saved,'targets',return_value=[('movies','all'),('shows','all'),('movies','top')]),patch.object(adapters,'discover',side_effect=remote):
            for _ in range(4):saved.tick(self.app,self.store)
        self.assertIn('shows',calls);self.assertGreater(calls.count('movies'),3)
        with patch.object(adapters,'discover',side_effect=lambda *a:(settings.save(self.store,'catalogue',dict(enabled=False)) or dict(items=[self.item(999)],nextCursor=None))):
            saved.refresh_all(self.app,self.store,sig,'0','movies')
        self.assertFalse(any(row['id']=='movie:999' for row in saved._cards(self.store,sig)))

    def test_saved_vod_has_no_file_cap_and_retains_native_actions_and_legacy_paging(self):
        self.setup_index()
        with self.store.connect() as db:
            db.execute('INSERT INTO m3u_sources(id,name,url,options,folder) VALUES(?,?,?,?,?)',('source','Source','https://private.invalid/secret','{}','source'))
            db.executemany('INSERT INTO m3u_files VALUES(?,?,?,?,?,?)',[('source','Movies/'+str(n)+'.strm','Film '+str(n),'movies','','') for n in range(1105)])
        args=dict(media='movies',collection='source',sort='title',q='',offset=0)
        with patch('backend.m3u_catalogue.decorate',side_effect=lambda db,rows:[dict(row) for row in rows]):
            result=saved.browse(self.app,self.store,args,'vod')
            self.assertEqual(len(result['items']),24);self.assertEqual(result['nextOffset'],24);self.assertFalse(result['completeSet'])
            seen=list(result['items']);offset=result['nextOffset']
            while offset is not None:
                page=saved.browse(self.app,self.store,dict(args,offset=offset),'vod')
                seen+=page['items'];offset=page['nextOffset']
            self.assertTrue(page['completeSet']);self.assertEqual(len(seen),1105)
            self.assertTrue(all(item['action']['type']=='vod-group' and item['_entry']['source']=='source' and item['_entry']['path'].endswith('.strm') for item in seen))
            legacy=adapters.vod(self.app,self.store,args)
            self.assertEqual(len(legacy['items']),24);self.assertEqual(legacy['nextOffset'],24)

    def test_premiering_filters_saved_dates_on_every_read_for_movies_and_tv(self):
        sig=self.setup_index()
        for media,kind in [('movies','movie'),('shows','show')]:
            items=[self.item(1,kind,day='2000-01-01'),self.item(2,kind),self.item(3,kind,day='')]
            saved.list_result(self.store,sig,media,'premiering',dict(items=items,label='Premiering'))
            result=saved.browse(self.app,self.store,dict(media=media,collection='premiering',sort='release'))
            self.assertEqual([item['action']['item']['externalId'] for item in result['items']],['2'])
            # A once-future saved list must not keep films after release day.
            with patch.object(saved,'unreleased',return_value=False):self.assertEqual(saved.browse(self.app,self.store,dict(media=media,collection='premiering',sort='release'))['items'],[])

    def test_local_search_is_immediate_frozen_until_explicit_repeat_and_account_scoped(self):
        sig=self.setup_index();saved.remember(self.store,sig,'NZB',[self.item(1,title='Fast B')]);tasks=[]
        with patch.object(engine,'schedule',side_effect=lambda app,store,key,fn:tasks.append(fn)),patch.object(search_snapshot,'collect',return_value=dict(items=[dict(self.item(2,title='Fast A'),origin='NZB')],warnings=[])) as collect:
            first=saved.search(self.app,self.store,self.args(fresh=True))
            collect.assert_not_called();self.assertEqual([i['title'] for i in first['items']],['Fast B'])
            tasks[0]();self.assertEqual(collect.call_count,1)
            repeated=saved.search(self.app,self.store,self.args())
            self.assertEqual(first['searchSnapshot'],repeated['searchSnapshot']);self.assertEqual(repeated['items'],first['items'])
            refreshed=saved.search(self.app,self.store,self.args(fresh=True))
            self.assertEqual([i['title'] for i in refreshed['items']],['Fast A','Fast B']);self.assertNotEqual(first['searchSnapshot'],refreshed['searchSnapshot'])
            self.store.set('m3u_tmdb_token','new-account-token')
            self.assertEqual(saved.search(self.app,self.store,self.args(fresh=True))['items'],[])

    def test_first_empty_search_waits_and_publishes_provider_matches_without_resubmission(self):
        sig=self.setup_index();tasks=[]
        item=dict(self.item(123931,'show',title='Invasion of the E Bikes Scourge of Our Streets'),origin='NZB')
        with patch.object(engine,'schedule',side_effect=lambda app,store,key,fn:tasks.append(fn)),patch.object(search_snapshot,'collect',return_value=dict(items=[item],warnings=[])):
            first=saved.search(self.app,self.store,dict(self.args(fresh=True),q='Invasion of the E Bikes'))
            self.assertTrue(first['loading']);self.assertEqual(first['items'],[])
            before=saved.search(self.app,self.store,dict(self.args(),q='Invasion of the E Bikes'))
            self.assertEqual(before['searchSnapshot'],first['searchSnapshot']);self.assertEqual(len(tasks),1)
            tasks[0]()
            complete=saved.search(self.app,self.store,dict(self.args(),q='Invasion of the E Bikes'))
            self.assertFalse(complete['loading']);self.assertEqual(len(complete['items']),1)
            self.assertNotEqual(first['searchSnapshot'],complete['searchSnapshot'])
            self.assertEqual(complete['items'][0]['title'],item['title'])
            self.assertEqual(saved.search(self.app,self.store,dict(self.args(),q='Invasion of the E Bikes'))['searchSnapshot'],complete['searchSnapshot'])

    def test_completed_empty_search_and_failure_do_not_spin_forever_or_wait_an_hour_to_retry(self):
        self.setup_index();tasks=[]
        with patch.object(engine,'schedule',side_effect=lambda app,store,key,fn:tasks.append(fn)),patch.object(search_snapshot,'collect',return_value=dict(items=[],warnings=[],incomplete=False)) as collect:
            saved.search(self.app,self.store,self.args(fresh=True));tasks[0]()
            result=saved.search(self.app,self.store,self.args())
            self.assertFalse(result['loading']);self.assertEqual(result['items'],[])
            # Search cache uses five minutes, independently of the hourly sweep.
            with self.store.connect() as db:
                row=db.execute("SELECT key,body FROM view_media_cache WHERE key LIKE 'saved:state:saved:query:v3:%'").fetchone()
                state=json.loads(row['body']);self.assertLessEqual(state['retryAt']-time.time(),300)
                db.execute("UPDATE view_media_cache SET body=json_set(body,'$.retryAt',0) WHERE key=?",(row['key'],))
            saved.search(self.app,self.store,self.args(fresh=True));self.assertEqual(len(tasks),2)
        collect.side_effect=ValueError('PRIVATE provider URL')
        tasks[1]()
        result=saved.search(self.app,self.store,self.args());self.assertFalse(result['loading'])
        self.assertNotIn('PRIVATE',json.dumps(result));self.assertIn('could not finish',result['warnings'][0])

    def test_search_names_all_enabled_indexers_and_does_not_leak_secrets(self):
        sig=self.setup_index();indexes=[dict(name='NZBgeek',url='https://geek.invalid/api',api_key='PRIVATE'),dict(name='AltHub',url='https://alt.invalid/api',api_key='PRIVATE')]
        with patch.object(saved,'signature',return_value=sig),patch('backend.discover_sources.indexers_for',return_value=indexes),patch.object(engine,'schedule',return_value=False):
            result=saved.search(self.app,self.store,self.args(fresh=True))
        self.assertIn('NZBgeek, AltHub',result['freshness']['scope']);self.assertNotIn('PRIVATE',json.dumps(result));self.assertTrue(result['loading'])

    def test_zero_identity_is_not_indexed_and_old_zero_rows_do_not_create_false_search_matches(self):
        sig=self.setup_index();bad=self.item(0,title='Unrelated movie')
        saved.remember_search(self.store,sig,[dict(bad,origin='NZB')],'invasion')
        self.assertEqual(saved._cards(self.store,sig),[])
        # Preserve historical data on disk while excluding the old bad card.
        with self.store.connect() as db:db.execute('INSERT INTO saved_media_catalogue VALUES(?,?,?,?,?,?,?)',(sig,'NZB','movie:0','movie','invasion unrelated',json.dumps(bad),time.time()))
        with patch.object(engine,'schedule',return_value=False):result=saved.search(self.app,self.store,dict(self.args(fresh=True),q='invasion'))
        self.assertEqual(result['items'],[])
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM saved_media_catalogue WHERE id='movie:0'").fetchone()[0],1)

    def test_background_completion_after_disable_cannot_commit(self):
        sig=self.setup_index();tasks=[]
        def finish(*args):
            settings.save(self.store,'catalogue',dict(enabled=False));return dict(items=[dict(self.item(),origin='NZB')],warnings=[])
        with patch.object(engine,'schedule',side_effect=lambda app,store,key,fn:tasks.append(fn)),patch.object(search_snapshot,'collect',side_effect=finish):
            saved.search(self.app,self.store,self.args());tasks[0]()
        self.assertEqual(saved._cards(self.store,sig),[])

    def test_observed_provider_query_survives_canonical_title_cleanup(self):
        sig=self.setup_index();item=dict(self.item(title='Canonical film title'),origin='NZB')
        saved.remember_search(self.store,sig,[item],'fast car')
        self.assertEqual(len(saved._cards(self.store,sig,'fast car')),1)
        saved.remember(self.store,sig,'NZB',[self.item(title='Canonical film title')])
        self.assertEqual(len(saved._cards(self.store,sig,'fast car')),1,'ordinary refresh retains the observed query alias')
        self.assertNotIn('fast car',saved._cards(self.store,sig,'fast car')[0]['title'])

    def test_only_current_account_caches_are_seeded_without_provider_calls(self):
        sig=self.setup_index();args=engine.default_browse_args();key=engine.browse_cache_key(args,self.store)
        with self.store.connect() as db:
            db.execute('INSERT INTO view_media_cache VALUES(?,?,?)',(key,json.dumps(dict(items=[self.item()],label='Saved')),time.time()))
            db.execute('INSERT INTO view_media_cache VALUES(?,?,?)',('engine:nzb-browse:v7:old-account:'+key.split(':{',1)[1].join(['{','']),json.dumps(dict(items=[self.item(2,title='Private old account')])),time.time()))
        with patch.object(adapters,'discover') as remote:saved.seed(self.store,sig)
        remote.assert_not_called();self.assertEqual([item['title'] for item in saved._cards(self.store,sig)],['Fast car 1'])

    def test_refresh_collects_three_batches_then_serves_all_without_paging(self):
        sig=self.setup_index();calls=[]
        def remote(app,store,args):
            calls.append(args);n=len(calls)
            return dict(items=[self.item(n)],nextCursor=json.dumps(dict(movies=n*20)),nextOffset=n,label='Movies')
        with patch.object(adapters,'discover',side_effect=remote):saved.refresh_list(self.app,self.store,sig,'0','movies','trending')
        self.assertEqual(len(calls),3)
        result=saved.browse(self.app,self.store,dict(media='movies',collection='trending',sort='title'))
        self.assertEqual(len(result['items']),3);self.assertIsNone(result['nextOffset'])
        settings.save(self.store,'catalogue',dict(enabled=False))
        with patch.object(adapters,'discover') as remote:saved.refresh_list(self.app,self.store,sig,'0','movies','top')
        remote.assert_not_called()

    def test_failed_refresh_keeps_saved_titles_and_safe_warnings(self):
        sig=self.setup_index();saved.list_result(self.store,sig,'movies','trending',dict(items=[self.item()],label='Movies'))
        with patch.object(adapters,'discover',return_value=dict(items=[],incomplete=True,warnings=['NZB indexer request limit reached: Indexer A.'],nextOffset=None)):
            saved.refresh_list(self.app,self.store,sig,'0','movies','trending')
        result=saved.browse(self.app,self.store,dict(media='movies',collection='trending',sort='title'))
        self.assertEqual(len(result['items']),1);self.assertIn('Indexer A',result['warnings'][0])

    def test_index_does_not_store_private_provider_payloads_or_clobber_vod_entry(self):
        sig=self.setup_index();item=self.item();item['grab']='PRIVATE';item['action']['item']['apikey']='PRIVATE';item['action']['secret']='PRIVATE';item['_entry']=dict(source='vod',path='Movies/A.strm',kind='movies')
        saved.remember(self.store,sig,'VOD',[item]);item.pop('_entry');saved.remember(self.store,sig,'VOD',[item])
        rows=saved._cards(self.store,sig);self.assertNotIn('PRIVATE',json.dumps(rows));self.assertEqual(rows[0]['_entry']['source'],'vod')

    def test_schedule_uses_saved_interval_and_off_is_idle(self):
        sig=self.setup_index()
        with patch.object(saved,'seed'),patch.object(saved,'refresh_local',return_value=True),patch.object(saved,'refresh_list') as refresh:
            saved.tick(self.app,self.store);self.assertEqual(refresh.call_count,1)
            saved.tick(self.app,self.store);self.assertEqual(refresh.call_count,2,'next list warms independently')
            settings.save(self.store,'catalogue',dict(enabled=False));saved.tick(self.app,self.store);self.assertEqual(refresh.call_count,2)

    def test_server_records_are_indexed_without_live_metadata_or_bad_vod_pairs(self):
        channel='UC'+'a'*22
        with self.store.connect() as db:
            db.execute('INSERT INTO channels(id,data,active) VALUES(?,?,1)',(channel,json.dumps(dict(id=channel,name='A channel'))))
            db.execute('INSERT INTO videos(id,channel,published,day,data,checked) VALUES(?,?,?,?,?,0)',('abcdefghijk',channel,'2026-01-01','2026-01-01',json.dumps(dict(title='Fast video',thumbnail=''))))
            for source in ['z-source','a-source']:
                db.execute('INSERT INTO m3u_sources(id,name,url,options,folder) VALUES(?,?,?,?,?)',(source,source,'https://private.invalid/secret','{}',source))
            db.execute('INSERT INTO m3u_catalog_titles(key,info,year,data) VALUES(?,?,?,?)',('film',json.dumps(dict(name='Fast film',provider='imdb',identity='tt1234567')),2026,'{}'))
            for source,path in [('a-source','z-file.strm'),('z-source','a-file.strm')]:
                db.execute('INSERT INTO m3u_files VALUES(?,?,?,?,?,?)',(source,path,'Fast film','movies','',''))
                db.execute('INSERT INTO m3u_catalog_entries VALUES(?,?,?,?,?)',(source,path,'film',0,''))
        sig=self.setup_index()
        with patch.object(adapters.local,'inventory',return_value=dict(items=[],files={},scanning=False)),patch('backend.view_media.providers.TMDB.get') as network:
            self.assertTrue(saved.refresh_local(self.app,self.store,sig,'0'))
        network.assert_not_called()
        rows=saved._cards(self.store,sig);self.assertEqual(len(rows),2);self.assertNotIn('private.invalid',json.dumps(rows))
        video=next(row for row in rows if row['kind']=='video');self.assertEqual(video['channelId'],channel)
        film=next(row for row in rows if row['kind']=='movie');self.assertEqual(film['_entry']['source'],'a-source');self.assertEqual(film['_entry']['path'],'z-file.strm')
        page=saved.browse(self.app,self.store,dict(media='movies',collection='a-source',sort='title'),'vod')
        self.assertEqual(page['items'][0]['action']['type'],'vod-group');self.assertEqual(page['items'][0]['_entry']['path'],'z-file.strm')
        page=saved.browse(self.app,self.store,dict(media='youtube',collection='',sort='title'),'subscriptions')
        self.assertEqual(len(page['items']),1)
        with self.store.connect() as db:db.execute('UPDATE channels SET active=0')
        self.assertEqual(saved.browse(self.app,self.store,dict(media='youtube',collection='',sort='title'),'subscriptions')['items'],[])
