"""A manual imported catalogue stays usable without automatic provider refresh."""
import json
import hashlib
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import Mock, patch

import test_view_media as fixture
from backend import discovery_engine as engine, favourites, m3u, m3u_downloads, m3u_removal
from backend.view_media import adapters, download_sources, global_search, saved_catalogue, saved_search, search_snapshot, vod_groups


PLAYLIST='''#EXTM3U
#EXTINF:5400 group-title="Movies" tvg-type="movie" imdb-id="tt1234567",Manual Film EditionTag 1080p (2024)
https://fixture.invalid/private-token/film1080.mp4
#EXTINF:5400 group-title="Movies" tvg-type="movie" imdb-id="tt1234567",Manual Film 2160p (2024)
https://fixture.invalid/private-token/film2160.mp4
#EXTINF:2400 group-title="TV VOD" tvdb-id="76543",Manual Show 1080p S01E01
https://fixture.invalid/private-token/episode1080.mp4
#EXTINF:2400 group-title="TV VOD" tvdb-id="76543",Manual Show 2160p S01E01
https://fixture.invalid/private-token/episode2160.mp4
#EXTINF:2400 group-title="TV VOD" tvdb-id="76543",Manual Show 1080p S02E02
https://fixture.invalid/private-token/episode202.mp4
#EXTINF:1800 group-title="TV VOD" tvdb-id="76544",Manual Daily 2026-10-31
https://fixture.invalid/private-token/daily.mp4
#EXTINF:-1 group-title="News",Live channel
https://fixture.invalid/private-token/channel.ts
'''


class VodAvailabilityTests(unittest.TestCase):
    def setUp(self):
        # Seed and read saved observations under one synthetic account scope.
        # A method-local patch alone changes the signature after seeding when
        # another fixture has supplied an ambient indexer account.
        indexers=patch('backend.discover_sources.indexers_for',return_value=[])
        indexers.start();self.addCleanup(indexers.stop)
        # The production image exports M3U_DIR=/m3u. An integration fixture must
        # use its disposable media tree even inside that read-only image.
        with patch.dict('os.environ',dict(M3U_DIR='')):
            fixture.ViewMediaTests.setUp(self)
        self.assertTrue(Path(self.app.config['M3U_DIR']).is_relative_to(self.root))
    tearDown=fixture.ViewMediaTests.tearDown
    get=fixture.ViewMediaTests.get

    def imported(self):
        # Matches the Settings form's default: Import now is explicit, while
        # the automatic-refresh interval is zero.
        source=m3u.save(self.store,dict(name='Manual source',url='https://fixture.invalid/private-playlist-token',hours=0,enabled=False))
        m3u.request_sync(self.store,source)
        fetch=Mock(return_value=PLAYLIST)
        m3u.tick(self.store,Path(self.app.config['M3U_DIR']),fetch=fetch)
        fetch.assert_called_once()
        with self.store.connect() as db:
            row=db.execute('SELECT state,options,message FROM m3u_sources WHERE id=?',(source,)).fetchone()
        self.assertEqual(row['state'],'complete',row['message'])
        self.assertEqual((json.loads(row['options'])['enabled'],json.loads(row['options'])['hours']),(False,0))
        return source,row['options']

    def args(self,**overrides):
        return dict(dict(media='all',q='Manual',sort='title',offset=0,cursor=None,collection='',category='all'),**overrides)

    def entries(self,item):
        with patch('backend.discover_sources.indexers_for',return_value=[]):
            return [entry for group in download_sources.choices(self.app,self.store,item)['sources'] for entry in group['entries']]

    def test_default_manual_import_browses_groups_and_keeps_every_exact_version_without_auto_sync(self):
        source,options=self.imported()
        no_fetch=Mock(side_effect=AssertionError('Automatic refresh stays off'))
        m3u.tick(self.store,Path(self.app.config['M3U_DIR']),fetch=no_fetch);no_fetch.assert_not_called()
        for background in (False,True):
            self.store.set('catalogue_pre_download','true' if background else 'false')
            movie=self.get('/api/view-media?'+urlencode(dict(source='vod',media='movies',collection=source))).json
            self.assertEqual(len(movie['items']),1)
            self.assertEqual(movie['collections'],[dict(id=source,name='Manual source')])
            card=movie['items'][0];self.assertEqual(card['count'],2)
            self.assertEqual(card['action']['type'],'vod-group')
            self.assertEqual(card['action']['target']['id'],'movie:1234567')
            versions=self.entries(card['action']['item'])
            self.assertEqual(len(versions),2)
            self.assertEqual({entry['source'] for entry in versions},{source})
            self.assertEqual({entry['quality'] for entry in versions},{'1080p','2160p'})
            # Opening one exact choice resolves its own file, never a sibling
            # stream or an implicit request to fetch from the provider.
            with self.store.connect() as db:
                files={row['path']:row['title'] for row in db.execute('SELECT path,title FROM m3u_files WHERE source=? AND kind=\'movies\'',(source,))}
            self.assertEqual({entry['path']:entry['title'] for entry in versions},files)
            for entry in versions:
                resolved,url=m3u_downloads.lookup(self.app,self.store,entry['source'],entry['path'])
                self.assertEqual((resolved['source'],resolved['path']),(source,entry['path']))
                self.assertTrue(url.endswith('film1080.mp4' if '1080p' in entry['quality'] else 'film2160.mp4'))
            shows=self.get('/api/view-media?'+urlencode(dict(source='vod',media='shows',collection=source))).json['items']
            series=next(item for item in shows if item['action']['target']['id']=='show:76543')
            episodes=self.entries(series['action']['item'])
            self.assertEqual(len(episodes),3)
            self.assertEqual({(entry['season'],entry['number']) for entry in episodes},{(1,1),(2,2)})
            self.assertEqual(sum(entry['season']==1 for entry in episodes),2)
            daily=next(item for item in shows if item['action']['target']['id']=='show:76544')
            self.assertEqual(self.entries(daily['action']['item'])[0]['kind'],'tv')
            self.assertFalse(any('Live channel' in item['title'] or item['kind']=='playlist' for item in shows))
            self.assertNotIn('private-token',json.dumps([movie,shows,versions,episodes]))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT options FROM m3u_sources WHERE id=?',(source,)).fetchone()[0],options)
            self.assertEqual(db.execute('SELECT count(*) FROM m3u_downloads').fetchone()[0],0)

    def test_global_and_saved_search_reload_find_raw_filename_and_all_series_versions(self):
        source,_=self.imported()
        with patch('backend.discover_sources.indexers_for',return_value=[]),patch('backend.youtube_discover.browse',return_value=dict(items=[],nextOffset=None)),patch.dict(adapters.REGISTRY,local=dict(browse=lambda *args:dict(items=[],nextOffset=None))),patch('backend.discovery_engine.schedule',return_value=False):
            page=global_search.browse(self.app,self.store,self.args(q='EditionTag'))
            self.assertEqual(len(page['items']),1)
            self.assertEqual(page['warnings'],[])
            film=page['items'][0];self.assertEqual(film['origin'],'VOD')
            self.assertEqual(len(self.entries(film['action']['item'])),2)
            saved_catalogue.initialise(self.store)
            saved_catalogue.remember_search(self.store,saved_catalogue.signature(self.store),page['items'],'EditionTag')
            for _ in range(2):
                result=saved_search.search(self.app,self.store,self.args(q='EditionTag',fresh=True,grouped=True))
                self.assertEqual(len(result['items']),1)
                self.assertEqual(result['items'][0]['origin'],'VOD')
                self.assertEqual({entry['source'] for entry in self.entries(result['items'][0]['action']['item'])},{source})
                self.assertEqual(len(self.entries(result['items'][0]['action']['item'])),2)
            series=saved_search.search(self.app,self.store,self.args(q='Manual Show',fresh=True,grouped=True))['items']
            self.assertEqual(len(series),1)
            self.assertEqual(len(self.entries(series[0]['action']['item'])),3)

    def test_failed_refresh_and_naming_review_pause_keep_last_import_available(self):
        source,_=self.imported()
        before=vod_groups.browse(self.store,'movies')['items'][0]
        m3u.request_sync(self.store,source)
        fetch=Mock(side_effect=ValueError('Synthetic provider unavailable'))
        m3u.tick(self.store,Path(self.app.config['M3U_DIR']),fetch=fetch)
        self.assertEqual(vod_groups.browse(self.store,'movies')['items'][0]['id'],before['id'])
        self.assertEqual(len(self.entries(before)),2)
        # Editing naming pauses refresh for a dry-run review; already imported
        # files still have their old safe paths until an explicit new import.
        m3u.save(self.store,dict(id=source,name='Manual source',hours=12,naming='emby'))
        with self.store.connect() as db:
            row=db.execute('SELECT state,options,message FROM m3u_sources WHERE id=?',(source,)).fetchone()
        self.assertEqual((json.loads(row['options'])['enabled'],json.loads(row['options'])['hours']),(False,0))
        self.assertIn('review',row['message'])
        self.assertEqual(len(self.entries(before)),2)
        no_fetch=Mock();m3u.tick(self.store,Path(self.app.config['M3U_DIR']),fetch=no_fetch);no_fetch.assert_not_called()

    def test_removal_intent_all_states_and_missing_sources_revoke_browse_choices_and_lookup(self):
        source,_=self.imported()
        film=vod_groups.browse(self.store,'movies')['items'][0]
        exact=self.entries(film)[0]
        self.assertEqual(favourites.vod_title(self.store,source,exact['path'],refresh=False)['id'],'movie:1234567')
        with self.store.connect() as db:m3u_removal.queue(db,source)
        for state in ('queued','running','error'):
            with self.store.connect() as db:db.execute('UPDATE m3u_removals SET state=? WHERE source=?',(state,source))
            self.assertEqual(vod_groups.browse(self.store,'movies')['items'],[])
            self.assertEqual(self.entries(film),[])
            self.assertEqual(download_sources.vod_rows(self.store,dict(id='movie:1234567',kind='movie',externalId='1234567')),[])
            self.assertEqual(adapters.vod(self.app,self.store,dict(media='movies',q='',offset=0,sort='title',collection=''))['collections'],[])
            with self.assertRaises(ValueError):favourites.vod_title(self.store,source,exact['path'],refresh=False)
            with patch.object(global_search,'catalogue',side_effect=AssertionError('Removed source must not run a VOD lane')),patch('backend.discover_sources.indexers_for',return_value=[]):
                self.assertEqual(global_search.browse(self.app,self.store,self.args(cursor=dict(vod_movies=0)))['items'],[])
        with self.store.connect() as db:db.execute('DELETE FROM m3u_sources WHERE id=?',(source,));db.execute('DELETE FROM m3u_removals WHERE source=?',(source,))
        self.assertEqual(self.entries(film),[],'orphan catalogue rows cannot offer expired choices')

    def test_removal_invalidates_paused_source_search_frame_and_stale_saved_group(self):
        source,_=self.imported()
        saved_catalogue.initialise(self.store)
        sig=saved_catalogue.signature(self.store)
        raw=dict(id='movie:9999999',kind='movie',title='Retained NZB',externalId='9999999')
        nzb=dict(adapters.card(raw,dict(type='discover',item=raw)),origin='NZB')
        saved_catalogue.list_result(self.store,sig,'movies','trending',dict(items=[nzb]))
        saved_catalogue.remember_search(self.store,sig,[nzb],'Manual')
        self.store.set('catalogue_pre_download','false')
        with patch('backend.discovery_engine.schedule',return_value=False),patch('backend.discover_sources.indexers_for',return_value=[]):
            first=saved_search.search(self.app,self.store,self.args(fresh=True,grouped=True))
            saved_catalogue.remember_search(self.store,saved_catalogue.signature(self.store),first['items'],'Manual')
            prior=saved_catalogue.signature(self.store)
            with self.store.connect() as db:m3u_removal.queue(db,source)
            self.assertEqual(saved_catalogue.signature(self.store),prior,'a removal intent must not hide unrelated saved observations')
            # Even if a stale remembered card has the current signature, VOD is
            # rebuilt from current rows rather than reviving its old _entry.
            saved_catalogue.remember_search(self.store,saved_catalogue.signature(self.store),first['items'],'Manual')
            retained=saved_search.search(self.app,self.store,self.args(fresh=True,grouped=True))['items']
            self.assertEqual([(item['origin'],item['title']) for item in retained],[('NZB',nzb['title'])])
            page=saved_catalogue.browse(self.app,self.store,dict(self.args(media='movies',q=''),collection='trending'))
            self.assertEqual([item['title'] for item in page['items']],[nzb['title']])
            with self.assertRaisesRegex(ValueError,'changed'):
                saved_search.search(self.app,self.store,self.args(offset=24,cursor=dict(snapshot=first['searchSnapshot'],order='title')))

    def test_removal_during_provider_collection_fences_old_scope_publication(self):
        source,_=self.imported()
        self.store.set('catalogue_pre_download','true')
        tasks=[]
        def collect(app,store,args):
            self.assertTrue(args['_checkpoint']())
            old=dict(vod_groups.cards(vod_groups.browse(store,'movies'))[0],origin='VOD')
            with store.connect() as db:m3u_removal.queue(db,source)
            self.assertFalse(args['_checkpoint']())
            return dict(items=[old],warnings=[])
        with patch('backend.discovery_engine.schedule',side_effect=lambda app,store,key,fn:tasks.append(fn)),patch('backend.discover_sources.indexers_for',return_value=[]),patch.object(search_snapshot,'collect',side_effect=collect):
            saved_search.search(self.app,self.store,self.args(fresh=True,grouped=True))
            tasks[0]()
            self.assertEqual(saved_catalogue._cards(self.store,saved_catalogue.signature(self.store),origin='VOD'),[])
            self.assertEqual(saved_search.search(self.app,self.store,self.args(fresh=True,grouped=True))['items'],[])

    def test_pre_fix_empty_global_search_frame_cannot_mask_manual_catalogue(self):
        self.imported()
        search_snapshot.initialise(self.store)
        args=self.args();args['global']=True
        key=engine.browse_cache_key(args,self.store)
        self.assertTrue(key.startswith('engine:global-search:v6:'))
        old=key.replace('engine:global-search:v6:','engine:global-search:v5:',1)
        with self.store.connect() as db:
            db.execute('INSERT INTO view_media_cache VALUES(?,?,?)',(old,json.dumps(dict(items=[],retryAt=time.time()+3600,snapshot='a'*32,searchKey=old)),time.time()))
        def run(app,store,key,fn):fn();return True
        with patch('backend.discovery_engine.schedule',side_effect=run),patch('backend.discover_sources.indexers_for',return_value=[]),patch('backend.youtube_discover.browse',return_value=dict(items=[],nextOffset=None)),patch.dict(adapters.REGISTRY,local=dict(browse=lambda *args:dict(items=[],nextOffset=None))):
            search_snapshot.cached_browse(self.app,self.store,args)
            result=search_snapshot.cached_browse(self.app,self.store,args)
        self.assertEqual(len(result['items']),3)
        self.assertTrue(all(item['origin']=='VOD' for item in result['items']))

    def test_search_policy_upgrade_preserves_existing_account_saved_aliases_and_lists(self):
        self.imported()
        saved_catalogue.initialise(self.store)
        args=self.args(q='',sort='source');args['global']=True
        transient=engine.browse_cache_key(args,self.store)
        old_signature=hashlib.sha256(transient.replace('engine:global-search:v6:','engine:global-search:v5:',1).encode()).hexdigest()
        self.assertEqual(saved_catalogue.signature(self.store),old_signature,'result-policy upgrades retain the established account identity')
        raw=dict(id='movie:9999999',kind='movie',title='Retained movie',externalId='9999999')
        card=dict(adapters.card(raw,dict(type='discover',item=raw)),origin='NZB')
        saved_catalogue.list_result(self.store,old_signature,'movies','trending',dict(items=[card]))
        saved_catalogue.remember_search(self.store,old_signature,[card],'PriorAlias')
        old_key='saved:query:v2:'+old_signature+':'+self.store.get('catalogue_generation','0')+':'+hashlib.sha256(b'prioralias').hexdigest()
        empty=dict(items=[],snapshot='a'*32,searchKey=old_key,freshness={})
        with self.store.connect() as db:db.execute('INSERT INTO view_media_cache VALUES(?,?,?)',(old_key+':title',json.dumps(empty),time.time()))
        self.store.set('catalogue_pre_download','false')
        with patch('backend.discovery_engine.schedule',return_value=False),patch('backend.discover_sources.indexers_for',return_value=[]):
            result=saved_search.search(self.app,self.store,self.args(q='PriorAlias',grouped=True))
            self.assertEqual([item['title'] for item in result['items']],['Retained movie'])
            page=saved_catalogue.browse(self.app,self.store,dict(self.args(media='movies',q=''),collection='trending'))
            self.assertEqual([item['title'] for item in page['items']],['Retained movie'])
        self.assertEqual(result['items'][0]['origin'],'NZB')

    def test_old_global_nzb_observations_survive_policy_upgrade_without_old_vod_leaves(self):
        self.imported()
        search_snapshot.initialise(self.store)
        args=self.args(q='LegacyAlias',sort='source');args['global']=True
        legacy=engine.browse_cache_key(args,self.store,catalogue_identity=True)
        raw=dict(id='movie:8888888',kind='movie',title='Legacy NZB observation',externalId='8888888')
        nzb=dict(adapters.card(raw,dict(type='discover',item=raw)),origin='NZB')
        old_vod=dict(vod_groups.cards(vod_groups.browse(self.store,'movies'))[0],title='Stale VOD file leaf',origin='VOD')
        with self.store.connect() as db:db.execute('INSERT INTO view_media_cache VALUES(?,?,?)',(legacy,json.dumps(dict(items=[nzb,old_vod])),time.time()))
        with patch('backend.discovery_engine.schedule',side_effect=AssertionError('Saved landing never schedules providers')),patch('backend.discover_sources.indexers_for',return_value=[]):
            page=saved_search.browse_saved(self.app,self.store,self.args(q=''))
        self.assertIn(nzb['title'],[item['title'] for item in page['items']])
        self.assertNotIn(old_vod['title'],[item['title'] for item in page['items']])
        self.assertEqual(len([item for item in page['items'] if item['origin']=='VOD']),3)


if __name__=='__main__':unittest.main()
