import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from backend.app import create_app
from backend.nzb import stop
from backend.view_media import adapters, local

ORIGIN='https://dev.example.com'

class ViewMediaTests(unittest.TestCase):
    def setUp(self):
        from backend import youtube_discover
        youtube_discover._CACHE.clear()
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)/'media';self.root.mkdir()
        self.app=create_app({'TESTING':True,'DATA_DIR':self.tmp.name,'MEDIA_DIR':str(self.root)})
        self.store=self.app.extensions['store']
        self.store.set('public_origin',ORIGIN);self.store.set('setup_complete','1')
        with self.store.connect() as db:db.execute("INSERT INTO users VALUES(1,'owner','unused')")
        self.client=self.app.test_client()
        with self.client.session_transaction(base_url=ORIGIN) as session:session['user_id']=1;session['csrf']='token'
    def tearDown(self):
        stop(self.app);local._CACHE.pop(str(self.root),None);self.tmp.cleanup()
    def get(self,path):return self.client.get(path,base_url=ORIGIN)
    def test_registry_and_auth(self):
        self.assertEqual({item['id'] for item in self.get('/api/view-media/sources').json['items']},{'all','discover','vod','local','subscriptions'})
        self.assertNotIn('browse',str(self.get('/api/view-media/sources').json))
        self.assertNotEqual(self.app.test_client().get('/api/view-media/sources',base_url=ORIGIN).status_code,200)
    def test_only_selected_adapter_runs_and_filters_are_validated(self):
        mocks={key:Mock(return_value={'items':[],'nextOffset':None}) for key in adapters.REGISTRY}
        registry={key:{**value,'browse':mocks[key]} for key,value in adapters.REGISTRY.items()}
        with patch.dict(adapters.REGISTRY,registry):
            self.assertEqual(self.get('/api/view-media?source=vod').status_code,200)
            mocks['vod'].assert_called_once()
            for key in ('discover','local','subscriptions'):mocks[key].assert_not_called()
            self.assertEqual(self.get('/api/view-media?source=vod&media=youtube').status_code,400)
            self.assertEqual(self.get('/api/view-media?source=unknown').status_code,400)
            self.assertEqual(self.get('/api/view-media?offset=-1').status_code,400)
    def test_empty_local_sources_work_without_provider_requests(self):
        for source in ('vod','subscriptions'):
            response=self.get('/api/view-media?source='+source)
            self.assertEqual(response.status_code,200,response.json)
            self.assertEqual(response.json['items'],[])
    def test_inventory_excludes_samples_temp_and_links_and_serves_ranges(self):
        folder=self.root/'Film';folder.mkdir()
        (folder/'Film.mp4').write_bytes(b'0123456789')
        (folder/'Film.nfo').write_text('<movie><title>Actual title</title><year>2026</year></movie>')
        (folder/'Film.sample.mp4').write_bytes(b'sample')
        (self.root/'temp').mkdir();(self.root/'temp'/'partial.mp4').write_bytes(b'partial')
        outside=Path(self.tmp.name)/'private.mp4';outside.write_bytes(b'private')
        (folder/'linked.mp4').symlink_to(outside)
        state={};local._scan(self.root,state);local._CACHE[str(self.root)]=state
        self.assertEqual([i['title'] for i in state['items']],['Actual title'])
        identity=state['items'][0]['action']['id']
        self.assertEqual(local.media_file(self.root,identity,'file').read_bytes()[2:5],b'234')
        self.assertEqual(self.get('/api/view-media/local/'+identity+'/file').status_code,404,'media files are never served to the browser')
        self.assertEqual(self.client.get('/api/view-media/local/'+identity+'/file',base_url=ORIGIN,headers={'Range':'bytes=2-4'}).status_code,404)
        (folder/'Film.mp4').unlink();(folder/'Film.mp4').symlink_to(outside)
        with self.assertRaises(ValueError):local.media_file(self.root,identity,'file')
        self.assertEqual(self.get('/api/view-media/local/unknown/file').status_code,404)
    def test_rename_retarget_supersedes_an_inflight_scan_publication(self):
        import threading
        old=self.root/'Movies'/'Old (2026)'/'Old.mkv';old.parent.mkdir(parents=True);old.write_bytes(b'original')
        old_art=old.parent/'poster.jpg';old_art.write_bytes(b'poster')
        state={};local._scan(self.root,state)
        original=local._entry;reached=threading.Event();release=threading.Event();finished=threading.Event()
        def delayed(root,path):
            result=original(root,path)
            if path==old:
                reached.set();self.assertTrue(release.wait(5))
            return result
        real_scan=local._scan
        def scan(*args):
            try:real_scan(*args)
            finally:finished.set()
        with patch.object(local,'_entry',side_effect=delayed),patch.object(local,'_scan',side_effect=scan):
            local.refresh(self.root);self.assertTrue(reached.wait(5))
            destination=self.root/'Movies'/'New (2026)'/'New.mkv';old.parent.rename(destination.parent)
            (destination.parent/'Old.mkv').rename(destination)
            mapping=local.retarget(self.root,{old:destination,old_art:destination.parent/'poster.jpg'})
            new=mapping[0]['to'];self.assertEqual(local.media_file(self.root,new,'file'),destination)
            self.assertEqual(local.media_file(self.root,new,'art').read_bytes(),b'poster')
            release.set();self.assertTrue(finished.wait(5))
        self.assertEqual(local.media_file(self.root,new,'file'),destination,'older scan cannot replace the trusted renamed state')
        self.assertNotIn(mapping[0]['from'],local.inventory(self.root)['files'],'no old-hash alias survives')

    def test_rename_retarget_merges_previous_leaves_with_new_empty_scan(self):
        old=self.root/'Movies'/'Old (2026)'/'Old.mkv';old.parent.mkdir(parents=True);old.write_bytes(b'renamed')
        other=self.root/'Other.mp4';other.write_bytes(b'unrelated')
        state={};local._scan(self.root,state);previous=local.cached_snapshot(self.root)
        other_id=next(key for key,entry in state['files'].items() if entry['path']==other)
        destination=old.with_name('New.mkv');old.rename(destination)
        # Ownership invalidation followed by a concurrent GET has started an
        # empty new scan; it must not erase the saved, unrelated known leaves.
        local._CACHE[str(self.root)]=dict(items=[],files={},scanning=True,checked=0,warning='',_scan_token=object())
        changes=local.retarget(self.root,{old:destination},previous=previous)
        self.assertEqual(local.media_file(self.root,changes[0]['to'],'file'),destination)
        self.assertEqual(local.media_file(self.root,other_id,'file'),other)
        self.assertNotIn(changes[0]['from'],local.inventory(self.root)['files'])

    def test_nzb_source_registry_preserves_other_sources(self):
        items={row['id']:row for row in self.get('/api/view-media/sources').json['items']}
        self.assertEqual(items['discover']['label'],'NZB')
        self.assertEqual(items['discover']['media'],['movies','shows'])
        self.assertEqual(items['subscriptions']['label'],'YouTube')
        self.assertEqual(items['subscriptions']['media'],['youtube'])
        self.assertEqual(items['vod']['label'],'VOD')
        self.assertEqual(self.get('/api/view-media?media=youtube').status_code,400)

    def test_nzb_only_browse_search_and_independent_cursor(self):
        import json
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch('backend.discover_sources.indexers_for',return_value=[{}]))
            search=stack.enter_context(patch('backend.discover_search.search',return_value={
                'items':[{'id':'movie:1','title':'Film','kind':'movie'}],'nextOffset':100}))
            excluded=[stack.enter_context(patch(path)) for path in (
                'backend.view_media.providers.movies','backend.view_media.providers.shows',
                'backend.view_media.public_movies.movies','backend.view_media.download_sources.catalogue',
                'backend.view_media.local.inventory','backend.discover_sources.search_youtube')]
            for media in ('movies','shows'):
                result=self.get('/api/view-media?source=discover&media='+media).json
                self.assertEqual(len(result['items']),1)
                self.assertEqual(result['label'],'View All Movies' if media=='movies' else 'View All TV Shows')
                self.assertEqual(json.loads(result['nextCursor']),{'nzb':100})
                search.assert_called_with(self.app,self.store,media,'',0,browse=True)
                response=self.client.get('/api/view-media',base_url=ORIGIN,query_string={
                    'source':'discover','media':media,'q':'example','offset':1,'cursor':result['nextCursor']})
                self.assertEqual(response.status_code,200,response.json)
                search.assert_called_with(self.app,self.store,media,'example',100,browse=False)
            for mock in excluded:mock.assert_not_called()

    def test_nzb_source_without_indexers_has_honest_empty_state(self):
        with patch('backend.discover_sources.indexers_for',side_effect=ValueError()),patch('backend.discover_search.search') as search:
            response=self.get('/api/view-media')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['items'],[])
        self.assertIn('TMDB fallback',response.json['warnings'][0])
        search.assert_not_called()

    def test_nzb_rejects_old_mixed_cursors_before_search(self):
        import json
        with patch('backend.discover_search.search') as search:
            for cursor in ({'vod':24},{'local':24},{'catalogue':20},{'nzb':100,'vod':24}):
                response=self.client.get('/api/view-media',base_url=ORIGIN,query_string={'cursor':json.dumps(cursor)})
                self.assertEqual(response.status_code,400)
        search.assert_not_called()

    def test_nzb_failure_does_not_fall_back_to_vod_or_expose_secrets(self):
        with patch('backend.discover_sources.indexers_for',return_value=[{}]),patch('backend.discover_search.search',side_effect=RuntimeError('private detail')),patch('backend.view_media.download_sources.catalogue') as vod:
            response=self.get('/api/view-media?q=example')
        self.assertEqual(response.status_code,502)
        self.assertNotIn('private detail',str(response.json))
        vod.assert_not_called()

    def test_sorting_unknown_last_and_before_pagination(self):
        from backend.view_media.ordering import ordered
        items=[{'id':'a','kind':'movie','title':'Zulu','year':''},{'id':'b','kind':'movie','title':'Beta','year':'1990'},{'id':'c','kind':'movie','title':'Alpha','year':'2026'}]
        self.assertEqual([i['id'] for i in ordered(items,'year')],['c','b','a'])
        self.assertEqual([i['id'] for i in ordered(items,'year_asc')],['b','c','a'])
        with patch('backend.discover_sources.indexers_for',return_value=[{}]),patch('backend.discover_search.search',return_value={'items':items}):
            response=self.get('/api/view-media?sort=title')
            self.assertEqual([i['title'] for i in response.json['items']],['Alpha','Beta','Zulu'])
        self.assertEqual(self.get('/api/view-media?sort=top').status_code,400)
    def test_nzb_release_date_sort_uses_movie_year_when_exact_date_is_unknown(self):
        rows=[dict(id='movie:1',kind='movie',title='Old film',year='1999'),
              dict(id='movie:2',kind='movie',title='New film',year='2026'),
              dict(id='movie:3',kind='movie',title='Known date',year='2025',releaseDate='2025-11-01')]
        with patch('backend.discover_sources.indexers_for',return_value=[{}]),patch('backend.discover_search.search',return_value=dict(items=rows)):
            page=self.get('/api/view-media?sort=release').json
        self.assertEqual([item['title'] for item in page['items']],['New film','Known date','Old film'])

    def test_nzb_upload_sort_uses_latest_release_timestamp(self):
        rows=[dict(id='movie:1',kind='movie',title='New film',year='2026',releases=[dict(published='Wed, 14 Oct 2026 12:00:00 +0000')]),
              dict(id='movie:2',kind='movie',title='Old film uploaded today',year='1999',releases=[dict(published='Thu, 15 Oct 2026 12:00:00 +0000')])]
        with patch('backend.discover_sources.indexers_for',return_value=[{}]),patch('backend.discover_search.search',return_value=dict(items=rows)):
            page=self.get('/api/view-media?sort=new').json
        self.assertEqual(page['items'][0]['title'],'Old film uploaded today')
        self.assertTrue(page['items'][0]['added'])

    def test_movie_release_names_are_cleaned_without_changing_canonical_numeric_titles(self):
        from backend.discover_sources import _search_details
        for release,title,year in [('Holy Smoke 1999 1080p BluRay REMUX AVC DTS-HD-MA 5 1-UnKn0wn','Holy Smoke','1999'),
            ('Protector.2025.1080p.BluRay.REMUX','Protector','2025'),
            ('Police Academy Mission to Moscow 1994 BluRay 1080p FLAC 2 0 AVC REMUX','Police Academy Mission to Moscow','1994')]:
            result=_search_details({},release,'movie')
            self.assertEqual(result[:2],(title,year))
        self.assertEqual(_search_details(dict(imdbtitle='Blade Runner 2049',imdbyear='2017'),'unused','movie')[:2],('Blade Runner 2049','2017'))

    def test_movie_metadata_uses_imdb_identity_and_retains_nzb_releases(self):
        from backend.view_media.nzb_metadata import movies
        self.store.set('m3u_tmdb_token','test-token')
        item=dict(id='movie:123',kind='movie',externalId='00000123',title='Release name',releases=[dict(id='release')])
        with patch('backend.view_media.nzb_metadata.TMDB') as client:
            client.return_value.entity.return_value=dict(id=42,title='Canonical title',release_date='2026-10-01',poster_path='/poster.jpg')
            movies(self.store,[item])
        client.return_value.entity.assert_called_once_with(dict(kind='movies',provider='imdb',identity='tt0000123'))
        self.assertEqual(item['id'],'movie:123')
        self.assertEqual(item['releases'],[dict(id='release')])
        self.assertEqual(item['title'],'Canonical title')
        self.assertEqual(item['releaseDate'],'2026-10-01')
        self.assertTrue(item['image'])

    def test_invalid_cursor_rejected_before_search(self):
        for cursor in ('[]','{"youtube":1}','{"movies":-100}','{"private":0}'):
            response=self.client.get('/api/view-media',base_url=ORIGIN,query_string={'q':'test','cursor':cursor})
            self.assertEqual(response.status_code,400)

    def test_tv_schedule_paginates_unique_shows(self):
        from backend.view_media import providers
        episodes=[dict(airstamp='2026-01-01T12:00:00+00:00',show=dict(id=i,name='Show '+str(i),externals={})) for i in range(1,61)]
        episodes.append(dict(airstamp='2099-01-01T12:00:00+00:00',show=dict(id=999,name='Future')))
        with patch.object(providers,'maze',return_value=episodes):
            pages=[providers.shows(self.store,offset=n) for n in (0,24,48)]
        self.assertEqual([len(p['items']) for p in pages],[24,24,12])
        self.assertEqual([p['nextOffset'] for p in pages],[24,48,None])
        self.assertEqual(len({i['id'] for p in pages for i in p['items']}),60)
    def test_tmdb_trending_search_sort_and_details(self):
        from backend.view_media import providers
        with patch.object(providers,'TMDB') as client:
            client.return_value.get.return_value={'results':[{'id':12,'title':'Movie'}],'total_pages':3}
            self.assertEqual(providers.movies(self.store)['nextOffset'],20)
            client.return_value.get.assert_called_with('trending/movie/week',page=1,language='en-GB')
            providers.movies(self.store,query='Test',offset=20)
            self.assertEqual(client.return_value.get.call_args.args[0],'search/movie')
            providers.movies(self.store,sort='title')
            self.assertEqual(client.return_value.get.call_args.kwargs['sort_by'],'title.asc')
            client.return_value.get.return_value={'id':12,'title':'Movie','imdb_id':'tt12345'}
            self.assertEqual(providers.details(self.store,'tmdb:movie:12')['externalId'],'12345')
        self.assertEqual(self.get('/api/view-media/details?id=arbitrary:12').status_code,400)
    def _local_page(self, media, q=''):
        state={};local._scan(self.root,state);local._CACHE[str(self.root)]=state
        return adapters.REGISTRY['local']['browse'](self.app,self.store,dict(q=q,media=media,sort='title',offset=0))
    def _episode(self,folder,name,show,season,number,title='Episode'):
        folder.mkdir(parents=True,exist_ok=True)
        (folder/name).write_bytes(b'video')
        (folder/(name.rsplit('.',1)[0]+'.nfo')).write_text(
            f'<episodedetails><title>{title}</title><showtitle>{show}</showtitle><season>{season}</season><episode>{number}</episode><aired>2026-01-0{number}</aired></episodedetails>')
    def test_local_groups_show_episodes_under_one_parent_card(self):
        show=self.root/'TV Shows'/'VisionQuest (2026)'
        self._episode(show/'Season 1','e1.mkv','VisionQuest',1,2,'Theseus')
        self._episode(show/'Season 1','e2.mkv','VisionQuest',1,1,'No Strings')
        self._episode(self.root/'TV Shows'/'Other (2020)','o.mkv','Other',3,1)
        page=self._local_page('shows')
        parents={i['title']:i for i in page['items']}
        self.assertEqual(len(parents),2)
        quest=parents['VisionQuest']
        self.assertEqual(quest['action']['type'],'local-group');self.assertEqual(quest['count'],2)
        self.assertEqual([c['title'] for c in quest['children']],
            ['VisionQuest S01E01 - No Strings','VisionQuest S01E02 - Theseus'])
        self.assertEqual(parents['Other']['count'],1)
        # The parent card is not a saved file.
        gid=quest['action']['id']
        with self.assertRaises(ValueError):local.media_file(self.root,gid,'file')
    def test_local_groups_youtube_channel_videos_and_movie_parts(self):
        chan=self.root/'YouTube Downloads'/'Tracy Chapman';chan.mkdir(parents=True)
        (chan/'Baby Can I Hold You.mp4').write_bytes(b'v');(chan/'Fast Car.mp4').write_bytes(b'v')
        film=self.root/'Movies'/'Film (2020)';film.mkdir(parents=True)
        (film/'Film (2020)-cd2.mkv').write_bytes(b'v');(film/'Film (2020)-cd1.mkv').write_bytes(b'v')
        single=self.root/'Movies'/'Solo (2021)';single.mkdir(parents=True)
        (single/'Solo (2021).mkv').write_bytes(b'v')
        yt=self._local_page('youtube')
        self.assertEqual(len(yt['items']),1)
        self.assertEqual(yt['items'][0]['title'],'Tracy Chapman');self.assertEqual(yt['items'][0]['count'],2)
        movies=self._local_page('movies')
        by_title={i['title']:i for i in movies['items']}
        self.assertEqual(by_title['Film (2020)']['count'],2)
        self.assertEqual([c['title'] for c in by_title['Film (2020)']['children']],['Film (2020)-cd1','Film (2020)-cd2'])
        # A one-file movie folder stays a plain card.
        self.assertEqual(by_title['Solo (2021)']['action']['type'],'local')
    def _saved_upload(self, identity, channel, title, episode=1):
        path=self.root/'YouTube Downloads'/'Timeline'/'Season 2026'/(title+'.mp4')
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'unchanged saved video')
        path.with_suffix('.nfo').write_text('<episodedetails><title>'+title+'</title></episodedetails>')
        path.with_suffix('.jpg').write_bytes(b'local thumbnail')
        with self.store.connect() as db:
            db.execute("INSERT INTO media_files(id,channel,path,day,episode,profile,bytes) VALUES(?,?,?,?,?,?,?)",
                (identity,channel,str(path.relative_to(self.root)),'2026-04-29',episode,'emby',path.stat().st_size))
            db.execute("INSERT INTO downloads(id,title,quality,state,progress,created,updated,automatic) VALUES(?,?,1080,'complete',100,1,12345,0)",
                (identity,title))
        return path
    def test_single_saved_manual_youtube_video_visible_without_channel_membership(self):
        channel='UC'+'a'*22
        path=self._saved_upload('C0oPGcM8kYQ',channel,'Owner selected one-off')
        before=(path.read_bytes(),path.stat().st_ino,path.stat().st_mtime_ns)
        for active in (None,0,1):
            with self.subTest(channel_active=active):
                with self.store.connect() as db:
                    if active is not None:
                        db.execute('INSERT OR REPLACE INTO channels(id,data,active) VALUES(?,?,?)',(channel,'{"name":"Timeline"}',active))
                self._local_page('youtube')
                response=self.get('/api/view-media?source=local&media=youtube&sort=new')
                self.assertEqual(response.status_code,200,response.json)
                self.assertEqual(len(response.json['items']),1)
                card=response.json['items'][0]
                self.assertEqual((card['title'],card['kind'],card['channel'],card['channelId']),
                    ('Owner selected one-off','youtube','Timeline',channel))
                self.assertEqual(card['action']['type'],'local')
                self.assertNotIn('children',card)
                self.assertEqual(card['added'],12345)
                self.assertEqual(local.media_file(self.root,card['action']['id'],'file'),path)
                self.assertEqual(local.media_file(self.root,card['action']['id'],'art').read_bytes(),b'local thumbnail')
        self.assertEqual((path.read_bytes(),path.stat().st_ino,path.stat().st_mtime_ns),before)
        self.assertNotEqual(self.app.test_client().get('/api/view-media?source=local&media=youtube',base_url=ORIGIN).status_code,200)
    def test_multiple_manual_uploads_keep_saved_children_and_explicit_channel_identity(self):
        channel='UC'+'a'*22
        first=self._saved_upload('C0oPGcM8kYQ',channel,'Saved first',1)
        second=self._saved_upload('SzrRaA3Jn9E',channel,'Saved second',2)
        # This is a saved-file view, so an unavailable/failed un-published
        # download and arbitrary catalogue uploads never become group children.
        with self.store.connect() as db:
            db.execute("INSERT INTO downloads(id,title,quality,state,created,updated) VALUES('failedVideo1','Failed unpublished',1080,'failed',2,2)")
        page=self._local_page('youtube')
        self.assertEqual(len(page['items']),1)
        card=page['items'][0]
        self.assertEqual((card['title'],card['count'],card['channelId']),('Timeline',2,channel))
        self.assertEqual(card['action']['type'],'local-group')
        self.assertEqual([child['title'] for child in card['children']],['Saved first','Saved second'])
        self.assertTrue(all(child['action']['type']=='local' and child['channelId']==channel for child in card['children']))
        self.assertEqual({local.media_file(self.root,child['action']['id'],'file') for child in card['children']},{first,second})
        response=self.get('/api/view-media/local/'+card['action']['id']+'/parent')
        self.assertEqual(response.status_code,200,response.json)
        self.assertIsNone(response.json['target'],'a Library group must never redirect to the full channel catalogue')
        self.assertNotIn(str(self.root),response.get_data(as_text=True))
    def test_local_group_art_prefers_the_title_folder(self):
        show=self.root/'TV Shows'/'Rick and Morty (2013)'
        self._episode(show/'Season 9','e.mkv','Rick and Morty',9,8)
        (show/'poster.jpg').write_bytes(b'poster')
        page=self._local_page('shows')
        card=page['items'][0]
        gid=card['action']['id']
        self.assertEqual(card['image'],'/api/view-media/local/'+gid+'/art')
        self.assertEqual(local.media_file(self.root,gid,'art').read_bytes(),b'poster')
        # A folder without artwork falls back to a child's artwork URL.
        self._episode(self.root/'TV Shows'/'Bare (2021)','b.mkv','Bare',1,1)
        page=self._local_page('shows')
        bare=next(i for i in page['items'] if i['title']=='Bare')
        self.assertEqual(bare['image'],bare['children'][0]['image'])
    def test_local_search_returns_files_not_groups(self):
        show=self.root/'TV Shows'/'VisionQuest (2026)'
        self._episode(show/'Season 1','e1.mkv','VisionQuest',1,2,'Theseus')
        self._episode(show/'Season 1','e2.mkv','VisionQuest',1,1,'No Strings')
        page=self._local_page('movies',q='VisionQuest')
        self.assertEqual(len(page['items']),2)
        self.assertTrue(all(i['action']['type']=='local' for i in page['items']))
    def test_local_show_without_nfo_groups_by_folder(self):
        season=self.root/'TV Shows'/'Traffic Cops (2016)'/'Season 13';season.mkdir(parents=True)
        (season/'Traffic Cops S13E15 - Episode 15.mkv').write_bytes(b'v')
        (season/'Traffic Cops S13E16 - Episode 16.mkv').write_bytes(b'v')
        page=self._local_page('shows')
        self.assertEqual(len(page['items']),1)
        card=page['items'][0]
        self.assertEqual(card['title'],'Traffic Cops');self.assertEqual(card['count'],2)
        self.assertEqual(card['children'][0]['title'],'Traffic Cops S13E15 - Episode 15')
    def test_local_season_only_dir_never_becomes_a_category_card(self):
        # TV Shows/Season 1/ has no title folder — the files keep their real
        # show identity and must not merge into a bogus 'TV Shows' card.
        self._episode(self.root/'TV Shows'/'Season 1','a.mkv','Show A',1,1)
        self._episode(self.root/'TV Shows'/'Season 1','b.mkv','Show B',1,1)
        page=self._local_page('shows')
        self.assertEqual(sorted(i['title'] for i in page['items']),['Show A','Show B'])
        self.assertTrue(all(i['action']['type']=='local-group' for i in page['items']))
        # The same orphaning under a YouTube category leaves the file flat.
        sub=self.root/'YouTube Subscriptions'/'Season 2026';sub.mkdir(parents=True)
        (sub/'orphan.mkv').write_bytes(b'v')
        yt=self._local_page('youtube')
        self.assertEqual([i['title'] for i in yt['items']],['orphan'])
        self.assertEqual(yt['items'][0]['action']['type'],'local')
    def test_local_category_art_never_leaks_to_loose_files(self):
        # A poster saved at the category root must not dress every file in it.
        (self.root/'Movies').mkdir(exist_ok=True)
        (self.root/'Movies'/'poster.jpg').write_bytes(b'poster')
        (self.root/'Movies'/'fanart.jpg').write_bytes(b'fan')
        (self.root/'Movies'/'Loose (2020).mkv').write_bytes(b'v')
        page=self._local_page('movies')
        self.assertEqual(page['items'][0]['image'],'')
        self.assertEqual(page['items'][0]['backdrop'],'')
        # A file sidecar still wins.
        (self.root/'Movies'/'Loose (2020).jpg').write_bytes(b'jpg')
        page=self._local_page('movies')
        self.assertTrue(page['items'][0]['image'].endswith('/art'))
    def test_local_youtube_subdirectory_keeps_channel_identity_and_art(self):
        chan=self.root/'YouTube Downloads'/'ChanA';(chan/'2024').mkdir(parents=True)
        (chan/'poster.jpg').write_bytes(b'poster');(chan/'fanart.jpg').write_bytes(b'fan')
        (chan/'2024'/'old.mkv').write_bytes(b'v')
        (chan/'new.mkv').write_bytes(b'v')
        # SxxEyy in a YouTube filename stays a video, not a TV episode.
        (chan/'Clip S01E02.mkv').write_bytes(b'v')
        page=self._local_page('youtube')
        self.assertEqual(len(page['items']),1)
        card=page['items'][0]
        self.assertEqual(card['title'],'ChanA');self.assertEqual(card['count'],3)
        for child in card['children']:
            self.assertEqual(child['kind'],'youtube')
            self.assertEqual(child['channel'],'ChanA')
            self.assertTrue(child['image'].endswith('/art'))
    def test_local_youtube_thumb_sidecar_beats_channel_art(self):
        # Each upload's own yt-dlp -thumb.jpg is its image; the channel
        # poster/fanart belongs to the group card, not every child.
        chan=self.root/'YouTube Downloads'/'ChanB';chan.mkdir(parents=True)
        (chan/'poster.jpg').write_bytes(b'poster');(chan/'fanart.jpg').write_bytes(b'fan')
        (chan/'One.mkv').write_bytes(b'v');(chan/'One-thumb.jpg').write_bytes(b'one')
        (chan/'Two.mkv').write_bytes(b'v');(chan/'Two-thumb.png').write_bytes(b'two')
        (chan/'Three.mkv').write_bytes(b'v')
        page=self._local_page('youtube')
        card=page['items'][0]
        self.assertEqual(card['title'],'ChanB');self.assertEqual(card['count'],3)
        # Group card keeps the channel folder's poster/fanart.
        gid=card['action']['id']
        self.assertEqual(local.media_file(self.root,gid,'art').read_bytes(),b'poster')
        self.assertEqual(local.media_file(self.root,gid,'backdrop').read_bytes(),b'fan')
        children={c['title']:c for c in card['children']}
        self.assertEqual(local.media_file(self.root,children['One']['action']['id'],'art').read_bytes(),b'one')
        self.assertEqual(local.media_file(self.root,children['Two']['action']['id'],'art').read_bytes(),b'two')
        # No sidecar of its own — falls back to the channel poster, not the fanart.
        self.assertEqual(local.media_file(self.root,children['Three']['action']['id'],'art').read_bytes(),b'poster')
        # Same-stem .jpg still outranks the -thumb sidecar and folder art.
        (chan/'Two.jpg').write_bytes(b'stem')
        page=self._local_page('youtube')
        two=next(c for c in page['items'][0]['children'] if c['title']=='Two')
        self.assertEqual(local.media_file(self.root,two['action']['id'],'art').read_bytes(),b'stem')
    def test_library_added_is_not_file_modification_time(self):
        path=self.root/'Movie.mp4';path.write_bytes(b'video')
        state={};local._scan(self.root,state)
        self.assertIsNone(local.library_dates(self.store,state)[0]['added'])
        with self.store.connect() as db:
            db.execute("INSERT INTO m3u_downloads(id,destination,state,updated) VALUES('test',?,'complete',12345)",(str(path),))
        self.assertEqual(local.library_dates(self.store,state)[0]['added'],12345)

    def test_movie_artwork_host_is_allowed_by_page_policy(self):
        from backend.view_media.providers import movie_card
        from backend.discover_store import _image
        item=movie_card({'id':123,'poster_path':'/poster.jpg','backdrop_path':'/backdrop.jpg'})
        self.assertEqual(_image(item['image']),item['image'])
        self.assertIn('https://image.tmdb.org', self.get('/app/').headers['Content-Security-Policy'].split('img-src ')[1].split(';')[0])

    def test_vod_federation_available_sources_identity_choices_and_samples(self):
        import json
        from backend.view_media import download_sources as ds
        with self.store.connect() as db:
            for source,enabled in [('one',True),('two',True),('off',False)]:
                db.execute("INSERT INTO m3u_sources(id,name,url,options,folder) VALUES(?,?,?,?,?)",(source,source,'https://private.invalid/secret',json.dumps({'enabled':enabled}),source))
                for key,title,imdb,path in [('film','Film','tt1234567','Film.strm'),('wrong','Film','tt9999999','Wrong.strm'),('sample','Film','tt1234567','Sample.strm')]:
                    db.execute("INSERT INTO m3u_files VALUES(?,?,?,?,?,?)",(source,path,title,'movies','',''))
                    db.execute("INSERT OR IGNORE INTO m3u_catalog_titles(key,info,year,data) VALUES(?,?,?,?)",(key,json.dumps({'name':title,'provider':'imdb','identity':imdb}),2026,'{}'))
                    db.execute("INSERT INTO m3u_catalog_entries VALUES(?,?,?,?,?)",(source,path,key,0,''))
            db.execute("INSERT INTO m3u_removals VALUES('off','queued')")
        with patch('backend.discover_sources.indexers_for',return_value=[{}]):
            options=ds.choices(self.app,self.store,dict(id='tmdb:movie:3',kind='movie',title='Film',year='2026',externalId='1234567'))
        self.assertEqual([s['id'] for s in options['sources']],['nzb','one','two'])
        self.assertEqual([e['path'] for s in options['sources'] for e in s['entries']],['Film.strm','Film.strm'])
        self.assertNotIn('private.invalid',str(options));self.assertNotIn('secret',str(options))
        catalog=ds.catalogue(self.store,'movies')
        self.assertEqual(len(catalog['items']),2) # conflicting IMDb identities stay distinct
        self.assertEqual(ds.catalogue(self.store,'shows')['items'],[])
        response=self.get('/api/view-media/download-sources?kind=movie&id=tmdb:movie:3&externalId=1234567')
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.get('/api/view-media/download-sources?kind=video').status_code,400)

    def test_video_channel_resolution_uses_channel_name_not_video_title(self):
        from backend.view_media.download_sources import channel_item
        channel='UC'+'a'*22
        with patch('backend.catalogue.Importer') as importer:
            importer.return_value.api.return_value={'items':[{'id':'abcdefghijk','snippet':{'channelId':channel,'channelTitle':'Real channel'}}]}
            result=channel_item(self.app,self.store,{'kind':'video','videoId':'abcdefghijk','title':'Video title'})
        self.assertEqual(result['title'],'Real channel');self.assertEqual(result['channelId'],channel)
        with self.assertRaises(ValueError):channel_item(self.app,self.store,{'videoId':'invalid'})

    def test_video_subscription_explicit_action_does_not_toggle(self):
        channel='UC'+'b'*22
        with patch('backend.view_media.download_sources.channel_item',return_value={'kind':'channel','channelId':channel,'title':'Channel'}),patch('backend.youtube_account.subscribe',return_value={'subscribed':True}) as subscribe,patch('backend.youtube_account.toggle') as toggle:
            response=self.client.post('/api/discover/subscribe',base_url=ORIGIN,headers={'Origin':ORIGIN,'X-CSRF-Token':'token'},json={'item':{'kind':'video','videoId':'abcdefghijk'},'action':'subscribe'})
            self.assertEqual(response.status_code,200,response.json)
            subscribe.assert_called_once();toggle.assert_not_called()

class MovieIdentityTests(unittest.TestCase):
    def test_padded_indexer_ids_merge_and_private_release_lists_stay_server_side(self):
        from backend.view_media.adapters import card
        from backend.view_media.identity import unique_cards
        items=[dict(id='movie:00305224',kind='movie',title='Anger Management',externalId='00305224',year='2003',
                    rating='8',added=1,releases=[dict(id='a')]),
               dict(id='movie:0305224',kind='movie',title='Anger Management',externalId='0305224',year='2003',
                    rating='6.2',metadataVerified=True,added=2,releases=[dict(id='b')]),
               dict(id='movie:12345',kind='movie',title='Anger Management',externalId='12345',year='2026')]
        cards=unique_cards([card(item,dict(type='discover',item=item)) for item in items])
        self.assertEqual(len(cards),2,'Merge verified IMDb identity, never title alone')
        self.assertEqual(cards[0]['id'],'movie:305224')
        self.assertEqual(cards[0]['rating'],'6.2')
        self.assertEqual(cards[0]['releaseCount'],2)
        self.assertEqual(cards[0]['added'],2)
        self.assertNotIn('releases',cards[0]['action']['item'])
        self.assertEqual(cards[0]['action']['item']['externalId'],'0305224')
