"""Collections reuse title identities/policies and never acquire media on a favourite save."""
import json
import unittest
from unittest.mock import patch
import test_view_media as vm
from backend import favourites, movie_options, show_options, discovery_engine as engine
from backend.media_management import store as records


class FavouritesTests(unittest.TestCase):
    setUp=vm.ViewMediaTests.setUp
    tearDown=vm.ViewMediaTests.tearDown
    get=vm.ViewMediaTests.get
    def post(self,path,body,csrf=True):
        return self.client.post(path,base_url=vm.ORIGIN,json=body,headers={'Origin':vm.ORIGIN,**({'X-CSRF-Token':'token'} if csrf else {})})
    def video(self,number=0):
        channel='UC'+'a'*22;identity=f'{number:011d}'
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO channels VALUES(?,?,0)',(channel,json.dumps(dict(id=channel,name='Creator',avatar='',banner='',credential='DO_NOT_EXPORT'))))
            v=dict(id=identity,title='Video '+str(number),channel=channel,state='published',date='2026-01-01',duration='10:00')
            db.execute('INSERT INTO videos(id,channel,day,published,data,checked) VALUES(?,?,?,?,?,0)',(identity,channel,'2026-01-01','2026-01-01',json.dumps(v)))
        return identity,channel
    def vod(self,kind='series',provider='tvdb',identity='77',data=None,source='vod',key='catalogue',path='Show/S01E01.strm'):
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO m3u_sources(id,name,url,options,folder) VALUES(?,?,?,?,?)',(source,source,'https://private.invalid/SECRET','{}',source))
            db.execute('INSERT INTO m3u_files VALUES(?,?,?,?,?,?)',(source,path,'Same title',kind,'',''))
            db.execute('INSERT OR IGNORE INTO m3u_catalog_titles(key,info,year,data) VALUES(?,?,?,?)',(key,json.dumps(dict(provider=provider,identity=identity,name='Same title')),2026,json.dumps(data or {})))
            db.execute('INSERT INTO m3u_catalog_entries VALUES(?,?,?,?,?)',(source,path,key,0,''))
        return dict(source=source,path=path)

    def test_auth_csrf_validation_and_no_arbitrary_video_import(self):
        for url in ['/api/favourites','/api/favourites/videos','/api/favourites/video/00000000000']:
            self.assertNotEqual(self.app.test_client().get(url,base_url=vm.ORIGIN).status_code,200)
        path='/api/favourites/video/00000000000'
        self.assertEqual(self.post(path,dict(favourite=True),csrf=False).status_code,403)
        self.assertEqual(self.post(path,dict(favourite=True)).status_code,404)
        for body in [None,{},dict(favourite=1),dict(favourite='true'),dict(favourite=True,automatic=True)]:
            self.assertEqual(self.post(path,body).status_code,400)
        for url in ['/api/favourites/videos?offset=-1','/api/favourites/videos?offset=bad','/api/favourites/video/bad']:
            self.assertEqual(self.get(url).status_code,400)

    def test_video_favourite_persists_without_subscribe_download_or_policy_change(self):
        identity,channel=self.video()
        with patch('backend.downloads.enqueue') as acquire:
            self.assertEqual(self.post('/api/favourites/video/'+identity,dict(favourite=True)).status_code,200)
            favourites.initialise(self.store)
            rows=self.get('/api/favourites').json
            self.assertEqual([v['videoId'] for v in rows['videos']],[identity]);self.assertEqual(rows['channels'],[])
            self.assertNotIn('DO_NOT_EXPORT',str(rows))
            self.assertEqual(self.get('/api/favourites/video/'+identity).json['favourite'],True)
            self.assertEqual(self.post('/api/favourites/video/'+identity,dict(favourite=False)).status_code,200)
            self.assertEqual(self.get('/api/favourites').json['videos'],[])
        acquire.assert_not_called()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT active FROM channels WHERE id=?',(channel,)).fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM downloads').fetchone()[0],0)

    def test_video_genres_follow_saved_channel_information_without_channel_favourite(self):
        identity,channel=self.video()
        with self.store.connect() as db:
            db.execute('UPDATE channels SET data=? WHERE id=?',(json.dumps(dict(name='Creator',description='Computers and science',credential='DO_NOT_EXPORT')),channel))
        with patch('backend.discover_sources.search_youtube') as network:
            self.assertEqual(self.post('/api/favourites/video/'+identity,dict(favourite=True)).status_code,200)
            result=self.get('/api/favourites').json
        self.assertEqual(result['channels'],[])
        self.assertEqual(result['videos'][0]['genres'],['Science','Technology'])
        self.assertNotIn('DO_NOT_EXPORT',str(result));network.assert_not_called()
        with self.store.connect() as db:
            db.execute('UPDATE channels SET data=? WHERE id=?',(json.dumps(dict(name='Creator',description='Car restoration')),channel))
        self.assertEqual(self.get('/api/favourites').json['videos'][0]['genres'],['Cars & motoring','DIY & crafts'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM channel_favourites').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM downloads').fetchone()[0],0)

    def test_channel_favourites_never_populate_individual_videos(self):
        _,channel=self.video()
        with self.store.connect() as db:db.execute('INSERT INTO channel_favourites VALUES(?)',(channel,))
        result=self.get('/api/favourites').json
        self.assertEqual([c['id'] for c in result['channels']],[channel]);self.assertEqual(result['videos'],[])
        self.assertNotIn('DO_NOT_EXPORT',str(result))

    def test_automatic_channel_genres_use_saved_information_without_name_guesses(self):
        self.assertEqual(favourites.channel_genres(dict(name='Creator',description='Computers, networking and car restoration.')),
                         ['Cars & motoring','DIY & crafts','Technology'])
        self.assertEqual(favourites.channel_genres(dict(name='Electrical work',description='Electrician training and qualifications.')),
                         ['Trades & construction'])
        self.assertEqual(favourites.channel_genres(dict(name='Stories',description='Crime documentaries and the justice system.')),
                         ['Crime & justice','Documentaries'])
        for channel in [dict(name='Unknown',description=''),dict(name='Creator',description='https://example.com/gaming/music foo@technology.invalid')]:
            self.assertEqual(favourites.channel_genres(channel),[])
        self.assertEqual(favourites.channel_genres(dict(name='Workshop',description='Do it yourself projects.')),['DIY & crafts'])

    def test_channel_genres_are_read_only_and_never_fetch_or_change_favourites(self):
        _,channel=self.video()
        with self.store.connect() as db:
            db.execute('UPDATE channels SET data=? WHERE id=?',(json.dumps(dict(name='Computer workshop',description='Car restoration and computers',credential='DO_NOT_EXPORT')),channel))
            db.execute('INSERT INTO channel_favourites VALUES(?)',(channel,))
            before=tuple(db.execute('SELECT data,active FROM channels WHERE id=?',(channel,)).fetchone())
        with patch('backend.discover_sources.search_youtube') as youtube,patch('backend.view_media.providers.TMDB.get') as tmdb:
            result=self.get('/api/favourites').json
        self.assertEqual(result['channels'][0]['genres'],['Cars & motoring','DIY & crafts','Technology'])
        self.assertEqual(result['videos'],[]);self.assertNotIn('DO_NOT_EXPORT',str(result))
        youtube.assert_not_called();tmdb.assert_not_called()
        with self.store.connect() as db:
            self.assertEqual(tuple(db.execute('SELECT data,active FROM channels WHERE id=?',(channel,)).fetchone()),before)
            self.assertEqual(db.execute('SELECT count(*) FROM channel_favourites').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM downloads').fetchone()[0],0)

    def episode(self,season=2026,number=20):
        item=dict(id='show:77',kind='show',title='A show',externalId='77',year='2026',image='',credential='DO_NOT_EXPORT')
        key=f'show:77:{season}:{number}'
        records.remember(self.store,key,item['id'],'episode',dict(item=item,episode=dict(season=season,number=number,name='An episode',summary='Public synopsis'),date='2099-01-01',automatic=True))
        return key,item

    def test_episode_favourite_requires_known_episode_auth_csrf_and_strict_boolean(self):
        key,_=self.episode();path='/api/favourites/episode'
        self.assertNotEqual(self.app.test_client().get(path+'?key='+key,base_url=vm.ORIGIN).status_code,200)
        self.assertEqual(self.post(path,dict(key=key,favourite=True),csrf=False).status_code,403)
        for body in [None,{},dict(key=key,favourite=1),dict(key=key,favourite='true'),dict(key=key,favourite=True,item={})]:
            self.assertEqual(self.post(path,body).status_code,400)
        self.assertEqual(self.post(path,dict(key='unknown',favourite=True)).status_code,404)
        records.remember(self.store,'movie:1','movie:1','movie',dict(item=dict(id='movie:1',kind='movie',title='Movie')))
        self.assertEqual(self.post(path,dict(key='movie:1',favourite=True)).status_code,404)

    def test_episode_favourite_is_independent_of_show_plans_holds_and_acquisition(self):
        key,item=self.episode()
        # Seed the existing plan without starting its unrelated background scan.
        with patch.object(engine,'schedule'):engine.save_plan(self.app,self.store,item['id'],'future',True)
        with self.store.connect() as db:
            db.execute('UPDATE managed_media SET hold=1 WHERE key=?',(key,))
            before=tuple(db.execute('SELECT * FROM managed_media WHERE key=?',(key,)).fetchone())
            plan=tuple(db.execute('SELECT * FROM discover_series_plan WHERE parent=?',(item['id'],)).fetchone())
        with patch.object(engine,'queue') as queue,patch('backend.discover_sources.episodes') as metadata:
            self.assertEqual(self.post('/api/favourites/episode',dict(key=key,favourite=True)).status_code,200)
            result=self.get('/api/favourites').json
        self.assertEqual(result['shows'],[]);self.assertEqual(len(result['episodes']),1)
        entry=result['episodes'][0];self.assertEqual(entry['episodeKey'],key);self.assertEqual(entry['episodeLabel'],'S2026E20')
        self.assertEqual(entry['show']['id'],item['id']);self.assertEqual(entry['episode']['number'],20)
        self.assertNotIn('DO_NOT_EXPORT',str(result));self.assertNotIn('filename',entry['episode'])
        queue.assert_not_called();metadata.assert_not_called()
        with self.store.connect() as db:
            self.assertEqual(tuple(db.execute('SELECT * FROM managed_media WHERE key=?',(key,)).fetchone()),before)
            self.assertEqual(tuple(db.execute('SELECT * FROM discover_series_plan WHERE parent=?',(item['id'],)).fetchone()),plan)
            self.assertEqual(db.execute('SELECT count(*) FROM discover_requests').fetchone()[0],0)

    def test_episode_favourite_persists_separately_for_each_episode_and_retains_snapshot(self):
        one,_=self.episode(1,1);two,_=self.episode(1,2)
        for key in [one,two,one]:self.assertEqual(self.post('/api/favourites/episode',dict(key=key,favourite=True)).status_code,200)
        with self.store.connect() as db:db.execute('DELETE FROM managed_media WHERE key=?',(one,))
        self.assertEqual({e['episodeKey'] for e in self.get('/api/favourites').json['episodes']},{one,two})
        self.assertTrue(self.get('/api/favourites/episode?key='+one).json['favourite'])
        self.assertEqual(self.post('/api/favourites/episode',dict(key=one,favourite=False)).status_code,200)
        self.assertEqual([e['episodeKey'] for e in self.get('/api/favourites').json['episodes']],[two])

    def test_unmapped_episode_can_be_favourited_without_guessing_a_number(self):
        key='tvmaze:77:episode:88';item=dict(id='tvmaze:show:77',kind='show',title='A show')
        records.remember(self.store,key,item['id'],'episode',dict(item=item,episode=dict(season=None,number=None,name='Unnumbered'),automatic=False,date=''))
        self.assertEqual(self.post('/api/favourites/episode',dict(key=key,favourite=True)).status_code,200)
        entry=self.get('/api/favourites').json['episodes'][0]
        self.assertEqual(entry['episodeLabel'],'Unnumbered episode');self.assertIsNone(entry['episode']['number'])

    def test_video_picker_search_and_paging_include_saved_flags(self):
        for i in range(63):self.video(i)
        self.post('/api/favourites/video/00000000000',dict(favourite=True))
        first=self.get('/api/favourites/videos').json;last=self.get('/api/favourites/videos?offset=60').json
        self.assertEqual(len(first['items']),60);self.assertEqual(first['nextOffset'],60)
        self.assertEqual(len(last['items']),3);self.assertIsNone(last['nextOffset'])
        self.assertTrue(first['items'][0]['favourite'])
        self.assertEqual(len(self.get('/api/favourites/videos?q=Video%2062').json['items']),1)
        self.assertEqual(self.get('/api/favourites/videos?q=%25').json['items'],[])

    def test_snapshot_retains_video_after_index_row_is_removed(self):
        identity,_=self.video();self.post('/api/favourites/video/'+identity,dict(favourite=True))
        with self.store.connect() as db:db.execute('DELETE FROM videos WHERE id=?',(identity,))
        result=self.get('/api/favourites').json['videos'][0]
        self.assertEqual(result['videoId'],identity);self.assertEqual(result['title'],'Video 0')

    def test_vod_and_nzb_share_series_favourite_future_plan_and_cancel_hold(self):
        one=self.vod();two=self.vod(source='other')
        item=dict(id='show:77',kind='show',externalId='77',title='Same title',year='2026')
        records.remember(self.store,'show:77:1:1','show:77','episode',dict(item=item,episode=dict(season=1,number=1),date='2099-01-01',automatic=True))
        with patch.object(engine,'schedule'):engine.save_plan(self.app,self.store,'show:77','future',True)
        with self.store.connect() as db:db.execute("UPDATE managed_media SET hold=1 WHERE key='show:77:1:1'")
        with patch('backend.view_media.providers.TMDB.get') as network:
            for entry in (one,two):
                r=self.post('/api/favourites/vod-title',entry);self.assertEqual(r.status_code,200,r.json)
                self.assertEqual(r.json['item']['id'],'show:77')
                show_options.save(self.app,self.store,r.json['item'],dict(favourite=True))
            result=self.get('/api/favourites').json
        network.assert_not_called()
        self.assertEqual(len(result['shows']),1);self.assertTrue(result['shows'][0]['downloadFuture'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM discover_requests').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT hold FROM managed_media WHERE key='show:77:1:1'").fetchone()[0],1)
            self.assertEqual(db.execute('SELECT download_future FROM discover_series_plan').fetchone()[0],1)

    def test_vod_movie_reuses_imdb_movie_options_and_does_not_export_stream(self):
        entry=self.vod(kind='movies',provider='imdb',identity='tt1234567',path='Film.strm')
        result=self.post('/api/favourites/vod-title',entry)
        self.assertEqual(result.json['item']['id'],'movie:1234567')
        movie_options.save(self.app,self.store,result.json['item'],dict(favourite=True))
        state=self.get('/api/favourites').json
        self.assertFalse(state['movies'][0]['automatic']);self.assertNotIn('SECRET',str(state));self.assertNotIn('path',state['movies'][0])

    def test_tmdb_authoritative_external_link_is_cached_without_title_guessing(self):
        entry=self.vod(provider='tmdb',identity='123',data=dict(tmdb_id=123,title='Same title'))
        with patch('backend.view_media.providers.TMDB.get',return_value=dict(id=123,external_ids=dict(tvdb_id=77),poster_path='/poster.jpg')) as network:
            for _ in range(2):self.assertEqual(self.post('/api/favourites/vod-title',entry).json['item']['id'],'show:77')
        network.assert_called_once_with('tv/123',append_to_response='external_ids')

    def test_wrong_or_unavailable_metadata_never_guesses_show_identity(self):
        entry=self.vod(provider='tmdb',identity='123',data=dict(tmdb_id=123,title='Same title'))
        for response in [dict(id=999,external_ids=dict(tvdb_id=77)),dict(id=123,external_ids={})]:
            with patch('backend.view_media.providers.TMDB.get',return_value=response):
                item=self.post('/api/favourites/vod-title',entry).json['item']
            self.assertEqual(item['id'],'tmdb:show:123');self.assertFalse(item.get('externalId'))
        other=self.vod(provider='tvdb',identity='88',key='other',source='other')
        self.assertEqual(self.post('/api/favourites/vod-title',other).json['item']['id'],'show:88')

    def test_disabled_missing_and_sample_vod_entries_are_rejected(self):
        entry=self.vod()
        with self.store.connect() as db:db.execute("UPDATE m3u_sources SET options='{\"enabled\":false}'")
        self.assertEqual(self.post('/api/favourites/vod-title',entry).status_code,400)
        sample=self.vod(source='sample',key='sample',path='sample.strm')
        self.assertEqual(self.post('/api/favourites/vod-title',sample).status_code,400)
        self.assertEqual(self.post('/api/favourites/vod-title',dict(source='missing',path='Show.strm')).status_code,400)

    def test_verified_vod_links_drive_acquisition_and_reject_same_title_wrong_id(self):
        from backend.view_media.download_sources import vod_rows,matches
        entry=self.vod(provider='tmdb',identity='123',data=dict(tmdb_id=123,title='Different source wording'))
        with patch('backend.view_media.providers.TMDB.get',return_value=dict(id=123,external_ids=dict(tvdb_id=77))):
            self.post('/api/favourites/vod-title',entry)
        item=dict(id='show:77',kind='show',externalId='77',title='Catalogue title',year='2026')
        rows=vod_rows(self.store,item)
        self.assertEqual(len(rows),1);self.assertEqual(matches(rows[0],item),'identity')
        wrong=dict(item,id='show:88',externalId='88',title='Different source wording')
        self.assertEqual(matches(rows[0],wrong),'')

    def test_reading_collection_and_favourite_only_saves_never_opt_in(self):
        movie=dict(id='movie:1234567',kind='movie',externalId='1234567',title='Movie')
        show=dict(id='show:77',kind='show',externalId='77',title='Show')
        movie_options.save(self.app,self.store,movie,dict(favourite=True));show_options.save(self.app,self.store,show,dict(favourite=True))
        with patch.object(engine,'prepare') as prepare,patch.object(engine,'queue') as queue:
            result=self.get('/api/favourites').json
        prepare.assert_not_called();queue.assert_not_called()
        self.assertFalse(result['movies'][0]['automatic']);self.assertFalse(result['shows'][0]['downloadAll']);self.assertFalse(result['shows'][0]['downloadFuture'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM discover_requests').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM discover_series_plan').fetchone()[0],0)

    def test_local_tile_resolves_owned_movie_and_episode_without_path_or_secret_export(self):
        import hashlib
        from backend import discover_store as ds
        from backend.view_media import local
        movie=dict(id='movie:77',kind='movie',externalId='77',title='Recorded movie')
        folder=self.root/'Recorded movie';folder.mkdir();file=folder/'Recorded movie.mkv';file.write_bytes(b'owned')
        request,_=ds.insert_request(self.store,dict(idempotency='local-movie-fixture',kind='movie',title=movie['title'],quality='1080',fallback=False,state='complete',body=dict(item=movie,credential='DO_NOT_EXPORT'),episode_key='movie:77'))
        ds.mark_owned(self.store,'movie:77',request['id'],str(folder))
        local._scan(self.root,local._CACHE.setdefault(str(self.root),dict(items=[],files={},scanning=False,checked=0)))
        identity=hashlib.sha256(str(file.relative_to(self.root)).encode()).hexdigest()[:32]
        with patch('backend.discover_sources.episodes') as network:
            result=self.get('/api/favourites/local-title/'+identity)
        network.assert_not_called();self.assertEqual(result.status_code,200)
        self.assertEqual(result.json['item']['id'],'movie:77')
        self.assertNotIn(str(self.root),str(result.json));self.assertNotIn('DO_NOT_EXPORT',str(result.json))
        self.assertNotEqual(self.app.test_client().get('/api/favourites/local-title/'+identity,base_url=vm.ORIGIN).status_code,200)
        self.assertEqual(self.get('/api/favourites/local-title/not-an-id').status_code,400)
        file.unlink();file.symlink_to('/etc/passwd')
        self.assertEqual(self.get('/api/favourites/local-title/'+identity).status_code,400)
        show=dict(id='show:88',kind='show',externalId='88',title='Recorded show')
        episode=dict(id=2,season=1,number=2,name='Recorded episode')
        video=self.root/'Recorded S01E02.mkv';video.write_bytes(b'owned episode')
        key='show:88:1:2'
        records.remember(self.store,key,show['id'],'episode',dict(item=show,episode=episode,date='2026-01-01',automatic=True))
        row,_=ds.insert_request(self.store,dict(idempotency='local-episode-fixture',kind='episode',title='Recorded episode',quality='1080',fallback=False,state='complete',body=dict(item=show,episode=episode),episode_key=key))
        ds.mark_owned(self.store,key,row['id'],str(video))
        local._scan(self.root,local._CACHE[str(self.root)])
        identity=hashlib.sha256(video.name.encode()).hexdigest()[:32]
        result=self.get('/api/favourites/local-title/'+identity)
        self.assertEqual(result.status_code,200);self.assertEqual(result.json['item']['episodeKey'],key)
        self.assertEqual(result.json['item']['show']['id'],'show:88')
        self.assertNotIn(str(self.root),str(result.json))

    def recorded_local_movie(self,request='legacy',managed=True):
        import hashlib
        from backend import discover_store as ds
        from backend.view_media import local
        self.store.set('storage_base',str(self.root))
        self.store.media_root=str(self.root)
        file=self.root/'Movies'/'E T (1982)'/'E T (1982).mkv';file.parent.mkdir(parents=True);file.write_bytes(b'original video')
        item=dict(id='tmdb:movie:601',kind='movie',externalId='0083866',imdbId='tt0083866',title='E.T. the Extra-Terrestrial',year='1982')
        key='movie:0083866'
        if managed:records.remember(self.store,key,item['id'],'movie',dict(item=item,automatic=True))
        request_id=''
        if request:
            body=dict(item=item) if request=='modern' else dict(itemId=key,externalId='0083866',year='1982',credential='DO_NOT_EXPORT')
            row,_=ds.insert_request(self.store,dict(idempotency='legacy-local',kind='movie',title=item['title'],quality='1080',fallback=False,state='complete',body=body,episode_key=key))
            request_id=row['id']
        ds.mark_owned(self.store,key,request_id,str(file))
        state={};local._scan(self.root,state)
        identity=hashlib.sha256(str(file.relative_to(self.root)).encode()).hexdigest()[:32]
        return file,identity,item,key

    def test_local_legacy_owned_movie_uses_managed_identity_without_writing(self):
        file,identity,item,key=self.recorded_local_movie()
        connect=self.store.connect
        from contextlib import contextmanager
        @contextmanager
        def read_only():
            with connect() as db:
                db.execute('PRAGMA query_only=ON');yield db
        with patch.object(self.store,'connect',side_effect=read_only),patch('backend.view_media.providers.details') as provider:
            result=self.get('/api/favourites/local-title/'+identity)
        provider.assert_not_called();self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(result.json['item']['id'],item['id']);self.assertEqual(result.json['item']['externalId'],'0083866')
        self.assertNotIn(str(self.root),str(result.json));self.assertNotIn('DO_NOT_EXPORT',str(result.json))

    def test_local_legacy_movie_without_managed_row_and_without_request_row(self):
        file,identity,item,key=self.recorded_local_movie(managed=False)
        result=self.get('/api/favourites/local-title/'+identity)
        self.assertEqual(result.status_code,200,result.json);self.assertEqual(result.json['item']['id'],key)
        self.assertEqual(result.json['item']['externalId'],'0083866')
        records.remember(self.store,key,item['id'],'movie',dict(item=item,automatic=True))
        with self.store.connect() as db:db.execute('DELETE FROM discover_requests')
        result=self.get('/api/favourites/local-title/'+identity)
        self.assertEqual(result.status_code,200,result.json);self.assertEqual(result.json['item']['id'],item['id'])

    def test_local_modern_and_renamed_legacy_movie_keep_collection_identity(self):
        from backend import discover_store as ds
        from backend.media_management import change,files
        from backend.view_media import local
        file,identity,item,key=self.recorded_local_movie(request='modern')
        result=self.get('/api/favourites/local-title/'+identity)
        self.assertEqual(result.status_code,200,result.json);self.assertEqual(result.json['item']['id'],item['id'])
        with self.store.connect() as db:
            db.execute('UPDATE discover_requests SET body=? WHERE episode_key=?',(json.dumps(dict(itemId=key,externalId='0083866',year='1982')),key))
            db.execute('UPDATE managed_media SET hold=1,monitor=0 WHERE key=?',(key,))
        with patch.object(files,'invalidate'):
            renamed=change(self.app,self.store,key,'rename','ET (1982)')
        new=renamed['localChanges'][0]['to']
        self.assertEqual(renamed['localChanges'][0]['from'],identity)
        self.assertEqual(self.get('/api/view-media/local/'+identity+'/file').status_code,404)
        result=self.get('/api/favourites/local-title/'+new)
        self.assertEqual(result.status_code,200,result.json);self.assertEqual(result.json['item']['id'],item['id'])
        self.assertTrue(renamed['hold']);self.assertFalse(renamed['monitored'])
        self.assertEqual(local.media_file(self.root,new,'file').read_bytes(),b'original video')

    def test_local_recorded_identity_conflicts_are_rejected(self):
        file,identity,item,key=self.recorded_local_movie(request='modern')
        with self.store.connect() as db:
            db.execute('UPDATE discover_requests SET body=? WHERE episode_key=?',(json.dumps(dict(item=dict(item,externalId='999',imdbId='tt999'))),key))
        result=self.get('/api/favourites/local-title/'+identity)
        self.assertEqual(result.status_code,400);self.assertIn('conflicting',result.json['error'])

    def test_local_incompatible_provider_prefix_is_rejected(self):
        file,identity,item,key=self.recorded_local_movie(request='modern')
        for wrong in ('tvdb:movie:999','tmdb:show:999'):
            with self.subTest(identity=wrong):
                with self.store.connect() as db:
                    db.execute('UPDATE managed_media SET body=? WHERE key=?',(json.dumps(dict(item=dict(item,id=wrong),automatic=True)),key))
                result=self.get('/api/favourites/local-title/'+identity)
                self.assertEqual(result.status_code,400,result.json);self.assertIn('conflicting',result.json['error'])

    def test_local_episode_checks_both_records_and_get_stays_read_only(self):
        import hashlib
        from contextlib import contextmanager
        from backend import discover_store as ds
        from backend.view_media import local
        file=self.root/'Episode.mkv';file.write_bytes(b'episode')
        show=dict(id='show:88',kind='show',externalId='88',title='Saved show')
        episode=dict(season=1,number=2,name='Saved episode');key='show:88:1:2'
        records.remember(self.store,key,show['id'],'episode',dict(item=show,episode=episode,automatic=True,renameJournal={'old':'old','new':str(file)}))
        row,_=ds.insert_request(self.store,dict(idempotency='episode-readonly',kind='episode',title='Saved episode',quality='1080',fallback=False,state='complete',body=dict(item=show,episode=episode),episode_key=key))
        ds.mark_owned(self.store,key,row['id'],str(file));local._scan(self.root,{})
        identity=hashlib.sha256(file.name.encode()).hexdigest()[:32];connect=self.store.connect
        @contextmanager
        def readonly():
            with connect() as db:db.execute('PRAGMA query_only=ON');yield db
        with patch.object(self.store,'connect',side_effect=readonly),patch('backend.media_management.service.compose') as compose:
            result=self.get('/api/favourites/local-title/'+identity)
        self.assertEqual(result.status_code,200,result.json);compose.assert_not_called()
        self.assertEqual(result.json['item']['episodeKey'],key)
        for body,request_key in ((dict(item=show,episode=dict(season=2,number=3)),key),
                                 (dict(item=dict(show,externalId='99'),episode=episode),key),
                                 (dict(item=show,episode=episode),'show:88:2:3')):
            with self.subTest(body=body,request_key=request_key):
                with self.store.connect() as db:db.execute('UPDATE discover_requests SET body=?,episode_key=? WHERE id=?',(json.dumps(body),request_key,row['id']))
                result=self.get('/api/favourites/local-title/'+identity)
                self.assertEqual(result.status_code,400,result.json);self.assertIn('conflicting',result.json['error'])

    def test_local_multiple_owned_records_cannot_disagree_on_tmdb(self):
        from backend import discover_store as ds
        file,identity,item,key=self.recorded_local_movie(request='modern')
        other=dict(item,id='tmdb:movie:999')
        row,_=ds.insert_request(self.store,dict(idempotency='other-owner',kind='movie',title=other['title'],quality='1080',fallback=False,state='complete',body=dict(item=other),episode_key=other['id']))
        ds.mark_owned(self.store,other['id'],row['id'],str(file))
        from backend.view_media import local
        local._scan(self.root,{})
        result=self.get('/api/favourites/local-title/'+identity)
        self.assertEqual(result.status_code,400,result.json);self.assertIn('conflicting',result.json['error'])

    def test_local_vod_resolver_read_only_mode_never_enriches(self):
        from contextlib import contextmanager
        value=self.vod(kind='movies',provider='tmdb',identity='601',data=dict(tmdb_id='601',title='Stored movie'))
        with self.store.connect() as db:db.execute('DROP TABLE IF EXISTS view_media_cache')
        connect=self.store.connect
        @contextmanager
        def readonly():
            with connect() as db:db.execute('PRAGMA query_only=ON');yield db
        with patch.object(self.store,'connect',side_effect=readonly),patch('backend.view_media.providers.TMDB.get') as provider:
            item=favourites.vod_title(self.store,value['source'],value['path'],refresh=False)
        provider.assert_not_called();self.assertEqual(item['id'],'tmdb:movie:601')
        with self.store.connect() as db:self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='view_media_cache'").fetchone())

    def test_local_tvmaze_manual_unnumbered_identity_remains_read_only(self):
        import hashlib
        from contextlib import contextmanager
        from backend import discover_store as ds
        from backend.view_media import local
        from backend.media_management import files
        file=self.root/'Manual episode.mkv';file.write_bytes(b'episode')
        key='tvmaze:123:episode:456';show=dict(id='tvmaze:show:123',kind='show',title='Recorded manual show')
        episode=dict(id=456,season=None,number=None,name='Unnumbered episode')
        records.remember(self.store,key,show['id'],'episode',dict(item=show,episode=episode,automatic=False))
        row,_=ds.insert_request(self.store,dict(idempotency='tvmaze-manual',kind='episode',title='Unnumbered episode',quality='1080',fallback=False,state='complete',body=dict(item=show,episode=episode),episode_key=key))
        ds.mark_owned(self.store,key,row['id'],str(file));local._scan(self.root,{})
        identity=hashlib.sha256(file.name.encode()).hexdigest()[:32]
        with patch.object(files,'quality',return_value='Not verified'):before=favourites.episode_item(self.app,self.store,key)
        connect=self.store.connect
        @contextmanager
        def readonly():
            with connect() as db:db.execute('PRAGMA query_only=ON');yield db
        with patch.object(self.store,'connect',side_effect=readonly),patch('backend.view_media.providers.details') as provider:
            result=self.get('/api/favourites/local-title/'+identity)
        provider.assert_not_called();self.assertEqual(result.status_code,200,result.json)
        actual=result.json['item']
        self.assertEqual(actual['id'],before['id']);self.assertEqual(actual['episodeKey'],before['episodeKey'])
        self.assertEqual(actual['show']['id'],before['show']['id']);self.assertEqual(actual['episodeLabel'],before['episodeLabel'])
        self.assertIsNone(actual['episode']['season']);self.assertIsNone(actual['episode']['number'])
        with self.store.connect() as db:
            saved=db.execute('SELECT monitor,hold,body FROM managed_media WHERE key=?',(key,)).fetchone()
        self.assertEqual(saved['monitor'],-1);self.assertEqual(saved['hold'],0);self.assertFalse(json.loads(saved['body'])['automatic'])
        for item,ep,request_key in ((dict(show,id='tvmaze:show:999'),episode,key),
                                     (show,dict(episode,id=457),key),
                                     (show,dict(episode,number=1),key),
                                     (show,episode,'tvmaze:123:episode:457')):
            with self.subTest(item=item,episode=ep,key=request_key):
                with self.store.connect() as db:db.execute('UPDATE discover_requests SET body=?,episode_key=? WHERE id=?',(json.dumps(dict(item=item,episode=ep)),request_key,row['id']))
                result=self.get('/api/favourites/local-title/'+identity)
                self.assertEqual(result.status_code,400,result.json);self.assertIn('conflicting',result.json['error'])

    def test_local_tvmaze_key_preserves_canonical_show_and_partial_numbering(self):
        import hashlib
        from contextlib import contextmanager
        from backend import discover_store as ds
        from backend.view_media import local
        from backend.media_management import files
        file=self.root/'Canonical unnumbered episode.mkv';file.write_bytes(b'episode')
        key='tvmaze:123:episode:456';show=dict(id='tmdb:show:900',externalId='88',kind='show',title='Canonical show')
        episode=dict(id=456,season=1,number=None,name='Special episode')
        body=dict(item=show,episode=episode,automatic=False,tvmaze=123)
        records.remember(self.store,key,show['id'],'episode',body)
        row,_=ds.insert_request(self.store,dict(idempotency='canonical-unnumbered',kind='episode',title='Special episode',quality='1080',fallback=False,state='complete',body=body,episode_key=key))
        ds.mark_owned(self.store,key,row['id'],str(file));local._scan(self.root,{})
        identity=hashlib.sha256(file.name.encode()).hexdigest()[:32]
        with patch.object(files,'quality',return_value='Not verified'):before=favourites.episode_item(self.app,self.store,key)
        connect=self.store.connect
        @contextmanager
        def readonly():
            with connect() as db:db.execute('PRAGMA query_only=ON');yield db
        with patch.object(self.store,'connect',side_effect=readonly),patch('backend.view_media.providers.details') as provider:
            result=self.get('/api/favourites/local-title/'+identity)
        provider.assert_not_called();self.assertEqual(result.status_code,200,result.json)
        actual=result.json['item']
        self.assertEqual(actual['id'],before['id']);self.assertEqual(actual['episodeKey'],before['episodeKey'])
        self.assertEqual(actual['show']['id'],before['show']['id']);self.assertEqual(actual['show']['externalId'],'88')
        self.assertEqual(actual['episodeLabel'],before['episodeLabel']);self.assertEqual(actual['episodeLabel'],'Unnumbered episode')
        self.assertEqual(actual['episode']['season'],1);self.assertIsNone(actual['episode']['number'])
        for table,where,value in (('managed_media','key',key),('discover_requests','id',row['id'])):
            with self.subTest(conflicting=table):
                with self.store.connect() as db:db.execute('UPDATE '+table+' SET body=? WHERE '+where+'=?',(json.dumps(dict(body,tvmaze=999)),value))
                result=self.get('/api/favourites/local-title/'+identity)
                self.assertEqual(result.status_code,400,result.json);self.assertIn('conflicting',result.json['error'])
                with self.store.connect() as db:db.execute('UPDATE '+table+' SET body=? WHERE '+where+'=?',(json.dumps(body),value))
        with self.store.connect() as db:saved=db.execute('SELECT monitor,hold,body FROM managed_media WHERE key=?',(key,)).fetchone()
        self.assertEqual(saved['monitor'],-1);self.assertEqual(saved['hold'],0);self.assertFalse(json.loads(saved['body'])['automatic'])

    def test_local_unknown_owned_key_never_becomes_a_catalogue_identity(self):
        from backend import discover_store as ds
        from backend.view_media import local
        file=self.root/'Same title (1982).mkv';file.write_bytes(b'unknown')
        state={};local._scan(self.root,state);identity=state['items'][0]['action']['id']
        key='local:'+identity
        records.remember(self.store,key,key,'movie',dict(item=dict(id=key,kind='movie',title='Same title'),automatic=False))
        ds.mark_owned(self.store,key,'',str(file));local._scan(self.root,{})
        self.assertEqual(self.get('/api/favourites/local-title/'+identity).status_code,400)

    def test_unmatched_local_file_never_guesses_title_identity(self):
        import hashlib
        from backend.view_media import local
        file=self.root/'Same title 2026.mkv';file.write_bytes(b'unmatched')
        state=dict(items=[],files={},scanning=False,checked=0);local._CACHE[str(self.root)]=state;local._scan(self.root,state)
        identity=hashlib.sha256(file.name.encode()).hexdigest()[:32]
        before=[]
        with self.store.connect() as db:before=[tuple(r) for r in db.execute('SELECT * FROM movie_options')]
        self.assertEqual(self.get('/api/favourites/local-title/'+identity).status_code,400)
        with self.store.connect() as db:self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM movie_options')],before)

    def test_shared_media_root_is_not_a_movie_folder_identity(self):
        import hashlib
        from backend import discover_store as ds
        from backend.view_media import local
        file=self.root/'Unrelated movie.mkv';file.write_bytes(b'unrelated')
        item=dict(id='movie:77',kind='movie',externalId='77',title='Another movie')
        saved,_=ds.insert_request(self.store,dict(idempotency='bad-old-folder-fixture',kind='movie',title=item['title'],quality='1080',fallback=False,state='complete',body=dict(item=item),episode_key='movie:77'))
        ds.mark_owned(self.store,'movie:77',saved['id'],str(self.root))
        state=dict(items=[],files={},scanning=False,checked=0);local._CACHE[str(self.root)]=state;local._scan(self.root,state)
        identity=hashlib.sha256(file.name.encode()).hexdigest()[:32]
        self.assertEqual(self.get('/api/favourites/local-title/'+identity).status_code,400)
