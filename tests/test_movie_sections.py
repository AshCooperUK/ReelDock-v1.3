"""Movie lists retain metadata identity and require real NZB release candidates."""
import json
import unittest
from unittest.mock import patch
import test_view_media as vm
from backend.view_media import movie_sections as sections

class MovieSectionsTests(unittest.TestCase):
    setUp=vm.ViewMediaTests.setUp
    tearDown=vm.ViewMediaTests.tearDown
    get=vm.ViewMediaTests.get

    def result(self,path,**params):
        if path.startswith('movie/') and path.rsplit('/',1)[-1].isdigit():
            n=int(path.rsplit('/',1)[-1])
            return dict(id=n,imdb_id='tt'+str(n).zfill(7),title='Film '+str(n),release_date='2026-10-01',poster_path='/poster.jpg',vote_average=8)
        return dict(results=[dict(id=n,title='Film '+str(n),release_date='2026-10-01',poster_path='/poster.jpg') for n in (1,2)],total_pages=2)

    def test_lists_and_sort_choices_are_exact(self):
        self.assertEqual([label for _,label in sections.SECTIONS],['Premiering Movies','New Movies','Trending Movies','Top Rated Movies','View All Movies'])
        self.assertEqual([label for _,label in sections.SORTS],['A-Z','Z-A','Latest','Oldest','NZB Order','Year','Rating'])

    def test_unavailable_indexer_lists_use_explicit_tmdb_catalogue_fallback(self):
        with patch.object(sections,'TMDB') as client,patch('backend.discover_sources.indexers_for',return_value=[{}]),patch.object(sections.sources,'movie_availability',side_effect=lambda app,store,imdb:[dict(id='release',title='Film',published='Thu, 15 Oct 2026 12:00:00 +0000',provider='Indexer')] if imdb=='0000001' else []):
            client.return_value.get.side_effect=self.result
            for section,path in [('new','discover/movie'),('trending','trending/movie/week'),('top','movie/top_rated')]:
                response=self.get('/api/view-media?collection='+section)
                self.assertEqual(response.status_code,200,response.json)
                page=response.json
                self.assertEqual({item['id'] for item in page['items']},{'tmdb:movie:1','tmdb:movie:2'})
                self.assertNotIn('externalId',page['items'][0]['action']['item'])
                self.assertFalse(page['items'][0]['available'])
                self.assertEqual(page['freshness']['source'],'TMDB fallback')
                self.assertNotIn('releases',page['items'][0]['action']['item'])
                self.assertTrue(page['items'][0]['image'])
                self.assertIsNone(page['nextCursor'])
                self.assertEqual(page['cataloguePages'],2)
                self.assertEqual(page['collection'],section)
                self.assertTrue(any(call.args[0]==path for call in client.return_value.get.call_args_list))
                if section=='new':
                    call=next(call for call in client.return_value.get.call_args_list if call.args[0]==path)
                    self.assertIn('primary_release_date.gte',call.kwargs)
                    self.assertIn('primary_release_date.lte',call.kwargs)

    def test_upcoming_is_tmdb_catalogue_without_claiming_nzb_availability(self):
        with patch.object(sections,'TMDB') as client,patch('backend.discover_sources.indexers_for',return_value=[]),patch.object(sections.sources,'movie_availability') as availability:
            client.return_value.get.return_value=dict(results=[dict(id=12,title='Coming Soon',release_date='2099-01-01'),dict(id=13,title='Adult',adult=True)],total_pages=2)
            response=self.get('/api/view-media?collection=premiering')
        self.assertEqual(response.status_code,200,response.json)
        page=response.json;self.assertTrue(page['catalogueOnly']);self.assertEqual(page['collection'],'premiering')
        self.assertEqual([i['title'] for i in page['items']],['Coming Soon'])
        self.assertFalse(page['items'][0]['available']);self.assertEqual(page['items'][0]['releaseDate'],'2099-01-01')
        self.assertEqual(page['items'][0]['action']['item']['id'],'tmdb:movie:12')
        self.assertEqual(json.loads(page['nextCursor']),dict(movies=20))
        self.assertIn('checked separately',page['freshness']['scope']);self.assertEqual(page['freshness']['source'],'TMDB fallback')
        availability.assert_not_called()
        call=client.return_value.get.call_args;self.assertEqual(call.args[0],'discover/movie');self.assertIn('primary_release_date.gte',call.kwargs)

    def test_upcoming_public_fallback_retains_membership_and_saved_warning(self):
        from backend.view_media import public_movies
        args=dict(collection='premiering',q='soon',offset=1,cursor=dict(movies=20))
        with patch.object(sections,'TMDB') as client,patch.object(public_movies,'movies',return_value=dict(items=[dict(id='tmdb:movie:12',kind='movie',title='Coming Soon',releaseDate='2099-01-01')],nextOffset=None,warnings=['Saved catalogue'])) as public:
            client.return_value.get.side_effect=ValueError('No credential')
            page=sections.browse(self.app,self.store,args,[])
        self.assertEqual(page['warnings'],['Saved catalogue']);self.assertEqual(len(page['items']),1);self.assertIsNone(page['nextCursor'])
        public.assert_called_once_with(self.store,offset=20,upcoming=True)

    def test_premiering_excludes_old_today_unknown_and_invalid_primary_dates(self):
        today=sections.datetime.now(sections.ZoneInfo('Europe/London')).date().isoformat()
        with patch.object(sections,'TMDB') as client:
            client.return_value.get.return_value=dict(results=[dict(id=n,title=str(n),release_date=day) for n,day in enumerate(['2014-01-01',today,'','not a date','2099-01-01'],1)],total_pages=1)
            page=sections.upcoming(self.store,dict(q='',offset=0),0)
        self.assertEqual([item['title'] for item in page['items']],['5'])

    def test_public_upcoming_uses_requested_page_not_popularity_or_search(self):
        from backend.view_media import public_movies
        html='<div data-object-id="12"><a href="/movie/12-coming"><img class="poster" alt="Coming Soon" src="/poster.jpg"></a></div>'
        with patch.object(public_movies,'fetch',return_value=html.encode()) as fetch,patch.object(public_movies,'details',side_effect=lambda store,item:item):
            page=public_movies.movies(self.store,offset=20,sort='release',upcoming=True)
        self.assertIn('/movie/upcoming?page=2',fetch.call_args.args[0]);self.assertNotIn('sort_by',fetch.call_args.args[0])
        self.assertEqual(page['items'][0]['title'],'Coming Soon')

    def test_availability_cache_avoids_repeat_search_and_changes_with_accounts(self):
        from backend.view_media.providers import initialise
        initialise(self.store)
        with patch.object(sections.sources,'movie_availability',return_value=[dict(id='r',title='Film')]) as search:
            sections.availability(self.app,self.store,'0000001','one')
            sections.availability(self.app,self.store,'0000001','one')
            self.assertEqual(search.call_count,1)
            sections.availability(self.app,self.store,'0000001','two')
            self.assertEqual(search.call_count,2)
            with self.store.connect() as db:db.execute('UPDATE view_media_cache SET updated=0')
            sections.availability(self.app,self.store,'0000001','two')
            self.assertEqual(search.call_count,3)

    def test_samples_never_count_as_availability(self):
        from backend.view_media.providers import initialise
        initialise(self.store)
        with patch.object(sections.sources,'movie_availability',return_value=[dict(id='sample',title='Film.Sample.mkv')]):
            self.assertEqual(sections.availability(self.app,self.store,'0000001','one'),[])

    def test_movie_cursor_belongs_to_selected_list(self):
        with patch.object(sections,'TMDB') as client,patch('backend.discover_sources.indexers_for',return_value=[{}]),patch.object(sections.sources,'movie_availability',return_value=[]):
            client.return_value.get.side_effect=self.result
            response=self.client.get('/api/view-media',base_url=vm.ORIGIN,query_string=dict(collection='top',offset=1,cursor='{"movies":20}'))
            self.assertEqual(response.status_code,200,response.json)
            client.return_value.get.assert_any_call('movie/top_rated',page=2,language='en-GB')
            for params in (dict(collection='all',cursor='{"movies":20}'),dict(collection='top',cursor='{"nzb":100}'),dict(collection='invalid')):
                self.assertEqual(self.client.get('/api/view-media',base_url=vm.ORIGIN,query_string=params).status_code,400)

    def test_missing_imdb_and_failed_provider_are_not_catalogue_only_cards(self):
        with patch.object(sections,'TMDB') as client,patch('backend.discover_sources.indexers_for',return_value=[{}]),patch.object(sections.sources,'movie_availability',side_effect=ValueError('private URL')):
            client.return_value.get.side_effect=self.result
            page=sections.browse(self.app,self.store,dict(collection='trending',q='',offset=0,cursor=None),[{}])
            self.assertEqual(page['items'],[])
            self.assertTrue(page['warnings'])
            self.assertNotIn('private URL',str(page))

    def test_sparse_catalogue_pages_fill_a_batch_and_advance_by_consumed_pages(self):
        def result(path,**params):
            if path.startswith('movie/'):
                return self.result(path,**params)
            start=(params['page']-1)*20+1
            return dict(results=[dict(id=n,title='Film '+str(n)) for n in range(start,start+20)],total_pages=10)
        def release(app,store,imdb):
            return [dict(id=imdb,title='Film')] if int(imdb)%2 else []
        with patch.object(sections,'TMDB') as client,patch.object(sections.sources,'movie_availability',side_effect=release):
            client.return_value.get.side_effect=result
            page=sections.browse(self.app,self.store,dict(collection='trending',q='',offset=0,cursor=None),[{}])
        self.assertEqual(len(page['items']),40)
        self.assertEqual(page['cataloguePages'],4)
        self.assertEqual(json.loads(page['nextCursor']),dict(movies=80))
        self.assertFalse(page['incomplete'])

    def test_temporary_failure_retries_only_failed_movies(self):
        attempts={}
        def search(app,store,imdb):
            attempts[imdb]=attempts.get(imdb,0)+1
            if imdb=='0000001' and attempts[imdb]==1:raise ValueError('private provider URL')
            return [dict(id=imdb,title='Film')]
        with patch.object(sections,'TMDB') as client,patch.object(sections.sources,'movie_availability',side_effect=search):
            client.return_value.get.side_effect=self.result
            page=sections.browse(self.app,self.store,dict(collection='top',q='',offset=0,cursor=None),[{}])
        self.assertEqual(attempts['0000001'],2);self.assertEqual(attempts['0000002'],1)
        self.assertEqual(len(page['items']),2)
        self.assertFalse(page['warnings']);self.assertFalse(page['incomplete'])

    def test_failed_refresh_keeps_known_availability_and_schedules_retry(self):
        from backend.view_media.providers import initialise
        import hashlib,time
        initialise(self.store)
        account=hashlib.sha256(json.dumps([{}],sort_keys=True).encode()).hexdigest()
        with self.store.connect() as db:
            db.execute('INSERT INTO view_media_cache VALUES(?,?,?)',(sections.availability_key('0000001',account),json.dumps([dict(id='known',title='Film')]),time.time()-7200))
        with patch.object(sections,'TMDB') as client,patch.object(sections.sources,'movie_availability',side_effect=ValueError('private URL')):
            client.return_value.get.side_effect=self.result
            page=sections.browse(self.app,self.store,dict(collection='top',q='',offset=0,cursor=None),[{}])
        self.assertEqual(len(page['items']),1)
        self.assertTrue(page['incomplete']);self.assertGreater(page['retryAt'],time.time())
        self.assertNotIn('private URL',str(page))

    def test_concurrent_availability_checks_share_one_provider_request(self):
        from backend.view_media.providers import initialise
        from concurrent.futures import ThreadPoolExecutor
        import time
        initialise(self.store)
        def search(*args):time.sleep(.05);return [dict(id='release',title='Film')]
        with patch.object(sections.sources,'movie_availability',side_effect=search) as provider,ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(sections.availability,self.app,self.store,'0000001','same') for _ in range(2)]
            self.assertTrue(all(f.result() for f in futures))
        self.assertEqual(provider.call_count,1)

    def test_added_indexer_reuses_positive_but_rechecks_negative_availability(self):
        from backend.view_media.providers import initialise
        import time
        initialise(self.store)
        with self.store.connect() as db:
            db.execute('INSERT INTO view_media_cache VALUES(?,?,?)',(sections.availability_key('0000001','old'),json.dumps([dict(id='known',title='Film')]),time.time()))
            db.execute('INSERT INTO view_media_cache VALUES(?,?,?)',(sections.availability_key('0000002','old'),'[]',time.time()))
        with patch.object(sections.sources,'movie_availability',return_value=[]) as query:
            self.assertEqual(sections.availability(self.app,self.store,'0000001','new',('old',))[0]['id'],'known')
            self.assertEqual(sections.availability(self.app,self.store,'0000002','new',('old',)),[])
        self.assertEqual(query.call_count,1)

    def test_rate_limit_respects_retry_time_without_hiding_later_cached_matches(self):
        import time
        deadline=time.time()+300
        def search(app,store,imdb):
            if int(imdb)>4:return [dict(id=imdb,title='Film')]
            error=sections.sources.DiscoverError('Limited');error.failures=('http_429',);error.retry_at=deadline;error.limited_indexers=('NZBgeek','NZB.life')
            raise error
        def result(path,**params):
            if path.startswith('movie/'):return self.result(path,**params)
            return dict(results=[dict(id=n,title='Film '+str(n)) for n in range(1,7)],total_pages=1)
        with patch.object(sections,'TMDB') as client,patch.object(sections.sources,'movie_availability',side_effect=search) as query:
            client.return_value.get.side_effect=result
            page=sections.browse(self.app,self.store,dict(collection='trending',q='',offset=0,cursor=None),[{}])
        self.assertEqual(query.call_count,6,'No immediate retries during the server cooldown')
        self.assertEqual(len(page['items']),2)
        self.assertGreaterEqual(page['retryAt'],deadline)
        self.assertIn('request limit',page['warnings'][0])
        self.assertIn('NZBgeek',page['warnings'][0]);self.assertIn('NZB.life',page['warnings'][0])

    def test_confirmed_cards_publish_before_slow_neighbour_finishes(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        first=threading.Event();release=threading.Event()
        def search(app,store,imdb):
            if imdb=='0000002':release.wait(2)
            return [dict(id=imdb,title='Film')]
        with patch.object(sections,'TMDB') as client,patch.object(sections.sources,'movie_availability',side_effect=search),ThreadPoolExecutor(max_workers=1) as pool:
            client.return_value.get.side_effect=self.result
            task=pool.submit(sections.browse,self.app,self.store,dict(collection='top',q='',offset=0,cursor=None,_partial=lambda p:first.set()),[{}])
            try:
                self.assertTrue(first.wait(1),'First confirmed movie should not wait for the slow neighbour')
                self.assertFalse(task.done())
            finally:release.set()
            self.assertEqual(len(task.result()['items']),2)
