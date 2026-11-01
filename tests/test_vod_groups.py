"""Disposable playlist fixtures: work grouping never discards selectable files."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from backend.app import create_app
from backend.nzb import stop
from backend.view_media import adapters,download_sources,global_search,saved_catalogue
from backend.view_media import vod_groups


class VodGroupsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.app=create_app(dict(TESTING=True,DATA_DIR=str(self.root),MEDIA_DIR=str(self.root/'media')))
        self.store=self.app.extensions['store']
        with self.store.connect() as db:
            for source,enabled in [('one',True),('two',True),('off',False)]:
                db.execute('INSERT INTO m3u_sources(id,name,url,options,folder) VALUES(?,?,?,?,?)',(source,source,'https://fixture.invalid/private-token',json.dumps(dict(enabled=enabled)),source))
            db.execute("INSERT INTO m3u_removals VALUES('off','queued')")
    def tearDown(self):stop(self.app);self.tmp.cleanup()
    def entry(self,source,path,title,kind='movies',key='',provider='',identity='',name='',year=2024):
        with self.store.connect() as db:
            db.execute('INSERT INTO m3u_files VALUES(?,?,?,?,?,?)',(source,path,title,kind,'',''))
            if key:
                db.execute('INSERT OR IGNORE INTO m3u_catalog_titles(key,info,year,data) VALUES(?,?,?,?)',(key,json.dumps(dict(name=name or title,provider=provider,identity=identity)),year,'{}'))
                db.execute('INSERT INTO m3u_catalog_entries VALUES(?,?,?,?,?)',(source,path,key,10,''))
    def test_movie_versions_group_by_identity_and_conflicting_works_stay_distinct(self):
        for source,path in [('one','Film1080.strm'),('two','Film2160.strm'),('off','FilmHidden.strm')]:
            self.entry(source,path,'Film (2024) 1080p',key='same-'+source,provider='imdb',identity='tt123',name='Film')
        self.entry('one','Other.strm','Film (2024)',key='other',provider='imdb',identity='tt999',name='Film')
        page=vod_groups.browse(self.store,'movies')
        self.assertEqual(len(page['items']),2)
        film=next(item for item in page['items'] if item['externalId']=='123')
        with patch('backend.discover_sources.indexers_for',return_value=[]):choices=download_sources.choices(self.app,self.store,film)
        self.assertEqual([(entry['source'],entry['path']) for source in choices['sources'] for entry in source['entries']],[('one','Film1080.strm'),('two','Film2160.strm')])
        self.assertNotIn('private-token',str(choices));self.assertEqual(film['count'],2)
        self.assertEqual(vod_groups.cards(dict(items=[film]))[0]['action']['target']['id'],'movie:123')
    def test_series_card_contains_every_season_episode_and_version(self):
        for source,path,title in [('one','Show/S01E01-1080.strm','Show S01E01 1080p'),('two','Show/S01E01-2160.strm','Show S01E01 2160p'),('one','Show/S02E02.strm','Show S02E02')]:
            self.entry(source,path,title,'series','show-'+source,'tvdb','321','Show')
        page=vod_groups.browse(self.store,'shows');self.assertEqual(len(page['items']),1)
        with patch('backend.discover_sources.indexers_for',return_value=[]):choices=download_sources.choices(self.app,self.store,page['items'][0])
        entries=[entry for source in choices['sources'] for entry in source['entries']]
        self.assertEqual(len(entries),3);self.assertEqual({(e['season'],e['number']) for e in entries},{(1,1),(2,2)})
        self.assertEqual(vod_groups.cards(page)[0]['action']['type'],'vod-group')
        self.assertEqual(vod_groups.cards(page)[0]['action']['target']['id'],'show:321')
    def test_uncatalogued_and_raw_filename_search_opens_all_versions(self):
        self.entry('one','Film/Film (2024) EditionTag 1080p.strm','Film (2024) 1080p',key='film',provider='imdb',identity='tt123',name='Film')
        self.entry('two','Film/Film (2024) 2160p.strm','Film (2024) 2160p',key='film2',provider='imdb',identity='tt123',name='Film')
        self.entry('one','Raw/Uncatalogued (2025).strm','Uncatalogued (2025)')
        page=download_sources.catalogue(self.store,'movies','EditionTag')
        self.assertEqual(len(page['items']),1);self.assertEqual(page['items'][0]['count'],2)
        self.assertEqual(download_sources.catalogue(self.store,'movies','Uncatalogued')['items'][0]['title'],'Uncatalogued')
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM m3u_downloads').fetchone()[0],0)
    def test_group_paging_never_splits_one_series_into_episode_cards(self):
        for number in range(28):self.entry('one',f'Show{number}/S01E01.strm',f'Show {number} S01E01','series')
        for number in range(1,30):self.entry('one',f'Show0/S01E{number+1:02}.strm',f'Show 0 S01E{number+1:02}','series')
        first=vod_groups.browse(self.store,'shows');second=vod_groups.browse(self.store,'shows',offset=first['nextOffset'])
        cards=first['items']+second['items'];self.assertEqual(len(cards),28);self.assertEqual(len({c['id'] for c in cards}),28)
        self.assertIsNone(second['nextOffset']);self.assertEqual(next(c for c in cards if c['title']=='Show 0')['count'],30)
    def test_release_sort_uses_known_year_among_full_dates_without_fabricating_a_day(self):
        rows=[('old','Old dated (2011)',2011),('new','New year only (2026)',2026),('metadata','Metadata dated (2025)',2025),('playlist','Playlist dated (2023)',2023),('unknown','Unknown release',0)]
        for number,(key,title,year) in enumerate(rows):
            self.entry('one',key+'.strm',title,key=key,provider='imdb',identity='tt'+str(1000+number),name=title,year=year)
        with self.store.connect() as db:
            db.execute('UPDATE m3u_catalog_titles SET data=? WHERE key=?',(json.dumps(dict(release_date='2011-07-14')),'old'))
            db.execute('UPDATE m3u_catalog_titles SET data=? WHERE key=?',(json.dumps(dict(release_date='2025-04-03')),'metadata'))
            db.execute('UPDATE m3u_catalog_entries SET release_date=? WHERE key=?',('2023-08-12','playlist'))
            before='\n'.join(db.iterdump())
        with patch('backend.m3u_metadata.TMDB.get',side_effect=AssertionError('Sorting never fetches metadata')):
            newest=vod_groups.browse(self.store,'movies',source='one',sort='release')['items']
            oldest=vod_groups.browse(self.store,'movies',source='one',sort='release_asc')['items']
        self.assertEqual([item['_entry']['path'] for item in newest],['new.strm','metadata.strm','playlist.strm','old.strm','unknown.strm'])
        self.assertEqual([item['_entry']['path'] for item in oldest],['old.strm','playlist.strm','metadata.strm','new.strm','unknown.strm'])
        by_path={item['_entry']['path']:item for item in newest}
        self.assertEqual((by_path['new.strm']['year'],by_path['new.strm']['releaseDate']),('2026',''))
        self.assertEqual(by_path['playlist.strm']['releaseDate'],'2023-08-12')
        self.assertEqual(by_path['unknown.strm']['releaseDate'],'')
        self.assertEqual(vod_groups.cards(dict(items=[by_path['new.strm']]))[0]['action']['item']['releaseDate'],'')
        with self.store.connect() as db:self.assertEqual('\n'.join(db.iterdump()),before,'sorting is read-only')
    def test_undated_manual_source_release_pages_keep_year_order_groups_and_exact_versions(self):
        with self.store.connect() as db:db.execute('UPDATE m3u_sources SET options=? WHERE id=?',(json.dumps(dict(enabled=False,hours=0)),'one'))
        for number in range(30):
            for quality in ('1080p','2160p'):
                self.entry('one',f'Newest {number}-{quality}.strm',f'Newest {number} (2026) {quality}',key='new-'+str(number),provider='imdb',identity='tt'+str(2000+number),name='Newest '+str(number),year=2026)
        for number,year in enumerate((2011,2017,2007,2015,2024,1949)):
            self.entry('one',f'Older{number}.strm',f'Older {number} ({year})',key='old-'+str(number),provider='imdb',identity='tt'+str(3000+number),year=year)
        self.entry('one','Unknown.strm','Unknown release',year=0)
        self.entry('two','Other source.strm','Other source (2030)',key='other-source',provider='imdb',identity='tt4000',year=2030)
        self.entry('off','Removing.strm','Removing source (2099)',key='removing',provider='imdb',identity='tt5000',year=2099)
        first=vod_groups.browse(self.store,'movies',source='one',sort='release')
        self.assertEqual(len(first['items']),24);self.assertEqual({item['year'] for item in first['items']},{'2026'})
        self.assertTrue(all(item['releaseDate']=='' and item['count']==2 for item in first['items']))
        for order in ('release','release_asc'):
            start=vod_groups.browse(self.store,'movies',source='one',sort=order)
            following=vod_groups.browse(self.store,'movies',source='one',sort=order,offset=start['nextOffset'])
            items=start['items']+following['items'];self.assertEqual(len(items),37);self.assertEqual(len({item['id'] for item in items}),37)
            self.assertIsNone(following['nextOffset']);self.assertEqual(items[-1]['year'],'')
            self.assertEqual([item['id'] for item in items],[item['id'] for item in vod_groups.browse(self.store,'movies',source='one',sort=order,limit=None)['items']])
            self.assertEqual([item['id'] for item in start['items']],[item['id'] for item in vod_groups.browse(self.store,'movies',source='one',sort=order)['items']])
        action=vod_groups.cards(first)[0]['action']
        with patch('backend.discover_sources.indexers_for',return_value=[]):choices=download_sources.choices(self.app,self.store,action['item'])
        entries=[entry for source in choices['sources'] for entry in source['entries']]
        self.assertEqual(len(entries),2);self.assertEqual({entry['quality'] for entry in entries},{'1080p','2160p'})
        self.assertEqual({entry['source'] for entry in entries},{'one'})
        global_page=vod_groups.browse(self.store,'movies',sort='release')['items']
        self.assertEqual(global_page[0]['year'],'2030');self.assertFalse(any(item['_entry']['source']=='off' for item in global_page))
        with self.store.connect() as db:self.assertEqual(json.loads(db.execute('SELECT options FROM m3u_sources WHERE id=?',('one',)).fetchone()[0]),dict(enabled=False,hours=0))
    def test_saved_merged_movie_retains_vod_source_membership(self):
        saved_catalogue.initialise(self.store)
        item=adapters.card(dict(id='movie:123',kind='movie',title='Film',externalId='123'),dict(type='discover',item={}))
        item.update(origin='NZB',origins=['NZB','VOD'])
        saved_catalogue.remember_search(self.store,'fixture',[item],'Film')
        restored=saved_catalogue._cards(self.store,'fixture')[0]
        self.assertEqual(restored['origins'],['NZB','VOD'])
        duplicate=dict(item);duplicate.pop('origins')
        self.assertEqual(global_search.merge([duplicate,restored])[0]['origins'],['NZB','VOD'])
    def test_saved_search_finds_uncatalogued_vod_without_worker_or_provider(self):
        from backend.view_media import saved_search
        self.entry('one','Raw/Uncatalogued (2025).strm','Uncatalogued (2025)')
        args=dict(media='all',q='Uncatalogued',sort='title',offset=0,cursor=None,collection='',category='all',fresh=True,grouped=True)
        with patch('backend.discovery_engine.schedule',return_value=False),patch('backend.discover_sources.indexers_for',return_value=[]):page=saved_search.search(self.app,self.store,args)
        self.assertEqual(len(page['items']),1);self.assertEqual(page['items'][0]['action']['type'],'vod-group')
    def test_global_vod_lane_retains_full_series_instead_of_choosing_first_file(self):
        self.entry('one','Show/S01E01.strm','Show S01E01','series')
        self.entry('one','Show/S02E02.strm','Show S02E02','series')
        args=dict(q='Show',media='all',offset=0,cursor=None,sort='source',collection='',category='all')
        with patch('backend.discover_sources.indexers_for',return_value=[]),patch('backend.youtube_discover.browse',return_value=dict(items=[],nextOffset=None)),patch.dict(adapters.REGISTRY,local=dict(browse=lambda *args:dict(items=[],nextOffset=None))):
            page=global_search.browse(self.app,self.store,args)
        self.assertEqual(len(page['items']),1)
        action=page['items'][0]['action'];self.assertEqual(action['type'],'vod-group')
        self.assertEqual(len(download_sources.vod_rows(self.store,action['item'])),2)
        self.assertEqual(page['warnings'],[])
    def test_metadata_enrichment_does_not_strand_a_displayed_group_identity(self):
        self.entry('one','Film.strm','Film (2024)',key='film',provider='imdb',identity='tt123',name='Film')
        before=vod_groups.browse(self.store,'movies')['items'][0]
        with self.store.connect() as db:db.execute('UPDATE m3u_catalog_titles SET data=? WHERE key=?',(json.dumps(dict(tmdb_id=5,title='Film')),'film'))
        after=vod_groups.browse(self.store,'movies')['items'][0]
        self.assertNotEqual(before['id'],after['id'])
        self.assertEqual([(row['source'],row['path']) for row in download_sources.vod_rows(self.store,before)],[('one','Film.strm')])
    def test_conflicting_metadata_never_merges_or_supplies_collection_identity(self):
        self.entry('one','Conflict.strm','Film (2024)',key='conflict',provider='imdb',identity='tt123',name='Film')
        self.entry('two','Other.strm','Other (2024)',key='other',provider='imdb',identity='tt999',name='Other')
        with self.store.connect() as db:db.execute('UPDATE m3u_catalog_titles SET data=?',(json.dumps(dict(tmdb_id=5,title='Other',external_ids=dict(imdb_id='tt999'),artwork='https://image.tmdb.org/t/p/w500/other.jpg')),))
        page=vod_groups.cards(vod_groups.browse(self.store,'movies'))
        self.assertEqual(len(page),2)
        conflict=next(card for card in page if card['_entry']['path']=='Conflict.strm')
        self.assertIsNone(conflict['action']['target']);self.assertFalse(conflict['action']['item']['externalId'])
        self.assertEqual(conflict['title'],'Film');self.assertNotIn('other.jpg',conflict['image'])
