"""Watchlist opts into future automation; workers own acquisition and saved files."""
import json
import unittest
from urllib.parse import urlencode
from unittest.mock import patch
import test_view_media as vm
import test_favourites as fav_tests
from backend import watchlist, discover_store as ds, movie_options, discovery_engine as engine
from backend.media_management import store as records


class WatchlistTests(unittest.TestCase):
    setUp=vm.ViewMediaTests.setUp
    tearDown=vm.ViewMediaTests.tearDown
    get=vm.ViewMediaTests.get
    post=fav_tests.FavouritesTests.post
    video=fav_tests.FavouritesTests.video
    vod=fav_tests.FavouritesTests.vod

    def item(self,kind='movie',identity='77',**extra):
        return dict(id=kind+':'+identity,kind=kind,externalId=identity,title='A title',**extra)

    def status(self,item):return self.get('/api/watchlist/item?'+urlencode(dict(item=json.dumps(item))))
    def save(self,item,selected=True):return self.post('/api/watchlist/item',dict(item=item,watchlisted=selected))

    def protected(self):
        with self.store.connect() as db:
            names=['users','settings','channels','channel_favourites','video_favourites','episode_favourites','movie_options','show_options','discover_preferences','discover_follows','discover_rules','discover_series_plan','managed_media','managed_series','discover_requests','downloads','download_rules']
            return {name:[tuple(r) for r in db.execute('SELECT * FROM '+name+' ORDER BY 1')] for name in names}

    def test_auth_csrf_strict_boolean_and_identity_validation(self):
        item=self.item()
        for url in ['/api/watchlist','/api/watchlist/item?'+urlencode(dict(item=json.dumps(item))),'/api/watchlist/videos']:
            self.assertNotEqual(self.app.test_client().get(url,base_url=vm.ORIGIN).status_code,200)
        self.assertEqual(self.post('/api/watchlist/item',dict(item=item,watchlisted=True),csrf=False).status_code,403)
        for body in [None,{},dict(item=item,watchlisted=1),dict(item=item,watchlisted='true'),dict(item=item,watchlisted=True,automatic=True),dict(key='movie:77',watchlisted=True)]:
            self.assertEqual(self.post('/api/watchlist/item',body).status_code,400)
        for bad in [{},dict(item,externalId='88'),dict(id='show:77',kind='show',externalId='88'),dict(id='youtube:video:abcdefghijk',kind='video',videoId='01234567890'),dict(id='bad',kind='video')]:
            self.assertEqual(self.save(bad).status_code,400)
        self.assertEqual(self.get('/api/watchlist/item?item=bad').status_code,400)
        self.assertEqual(self.get('/api/watchlist/videos?offset=-1').status_code,400)

    def test_membership_is_idempotent_and_removal_only_disables_automation(self):
        video,_=self.video();movie=self.item();show=self.item('show')
        records.remember(self.store,'show:77:1:1',show['id'],'episode',dict(item=show,episode=dict(season=1,number=1),date='2099-01-01',automatic=True))
        with patch.object(engine,'schedule'):engine.save_plan(self.app,self.store,show['id'],'future',True)
        with self.store.connect() as db:db.execute('UPDATE managed_media SET hold=1')
        before=self.protected()
        with patch.object(engine,'queue') as queue,patch('backend.downloads.enqueue') as download,patch('backend.discover_sources.episodes') as metadata:
            for item in [movie,show,dict(id=video,kind='video',videoId=video,title='Video')]:
                self.assertEqual(self.status(item).json['watchlisted'],item==show)
                self.assertTrue(self.save(item).json['watchlisted'])
                self.assertTrue(self.save(item).json['watchlisted'])
            result=self.get('/api/watchlist').json
        self.assertEqual([len(result[k]) for k in ('movies','shows','videos','channels')],[1,1,1,0])
        self.assertTrue(result['shows'][0]['downloadFuture'])
        current=self.protected()
        for table in before.keys()-{'movie_options','managed_media'}:self.assertEqual(current[table],before[table],table)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT automatic FROM movie_options').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT hold FROM managed_media WHERE kind='episode'").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM managed_media WHERE kind IN ('movie','video') AND monitor=1").fetchone()[0],2)
        queue.assert_not_called();download.assert_not_called();metadata.assert_not_called()
        for item in [movie,show,dict(id=video,kind='video',title='Video')]:self.assertFalse(self.save(item,False).json['watchlisted'])
        after=self.protected()
        with self.store.connect() as db:self.assertEqual(tuple(db.execute('SELECT download_all,download_future FROM discover_series_plan').fetchone()),(0,0))
        after['discover_series_plan']=before['discover_series_plan']
        after['settings']=[r for r in after['settings'] if r[0]!=watchlist.AUTO_TITLES_SEEN]
        before['settings']=[r for r in before['settings'] if r[0]!=watchlist.AUTO_TITLES_SEEN]
        for table in before.keys()-{'movie_options','managed_media'}:self.assertEqual(after[table],before[table],table)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT automatic FROM movie_options').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM managed_media WHERE kind IN ('movie','video') AND monitor=0").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT hold FROM managed_media WHERE kind='episode'").fetchone()[0],1)

    def test_movie_remains_when_release_date_passes_and_file_is_downloaded(self):
        item=self.item(releaseDate='2099-01-01');self.save(item)
        self.assertEqual(self.get('/api/watchlist').json['movies'][0]['watchStatus'],'Upcoming')
        with patch('backend.media_management.service.today',return_value='2100-01-01'):
            self.assertEqual(self.get('/api/watchlist').json['movies'][0]['watchStatus'],'Released')
        records.remember(self.store,'movie:77',item['id'],'movie',dict(item=item,date='2099-01-01',automatic=True))
        with patch('backend.media_management.service.compose',return_value=dict(file=True,active=False,status='Downloaded')):
            result=self.get('/api/watchlist').json['movies']
        self.assertEqual(len(result),1);self.assertEqual(result[0]['watchStatus'],'Downloaded')
        self.assertTrue(self.status(item).json['watchlisted'])
        self.assertFalse(self.save(item,False).json['watchlisted'])

    def test_videos_remain_for_scheduled_live_released_complete_and_missing_catalogue(self):
        video,channel=self.video();item=dict(id=video,kind='video',videoId=video,title='Snapshot')
        self.save(item)
        for state,when,label in [('scheduled','2099-01-01','Upcoming'),('live','2026-01-01','Live'),('published','2026-01-01','Released')]:
            with self.store.connect() as db:db.execute('UPDATE videos SET data=? WHERE id=?',(json.dumps(dict(id=video,title='Updated title',channel=channel,state=state,date=when)),video))
            entry=self.get('/api/watchlist').json['videos'][0]
            self.assertEqual(entry['title'],'Updated title');self.assertEqual(entry['watchStatus'],label)
        with patch('backend.media_library.local_file',return_value=self.root/'file.mp4'):
            self.assertEqual(self.get('/api/watchlist').json['videos'][0]['watchStatus'],'Downloaded')
        with self.store.connect() as db:db.execute('DELETE FROM videos WHERE id=?',(video,))
        self.assertEqual(len(self.get('/api/watchlist').json['videos']),1)
        self.assertTrue(self.status(item).json['watchlisted'])

    def test_public_snapshot_without_saved_video_does_not_register_or_subscribe(self):
        item=dict(id='youtube:video:abcdefghijk',videoId='abcdefghijk',kind='video',title='Upcoming premiere',published='2099-01-01',genres=['Technology'],avatar='https://yt3.ggpht.com/avatar',credential='DO_NOT_EXPORT',url='https://private.invalid/SECRET',image='https://private.invalid/SECRET',releases=[dict(grab='SECRET')])
        before=self.protected();self.save(item)
        result=self.get('/api/watchlist').json
        self.assertEqual(result['videos'][0]['watchStatus'],'Upcoming')
        self.assertEqual(result['videos'][0]['genres'],['Technology'])
        self.assertNotIn('SECRET',json.dumps(result));self.assertNotIn('credential',json.dumps(result))
        after=self.protected()
        for table in before.keys()-{'managed_media'}:self.assertEqual(after[table],before[table],table)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT monitor FROM managed_media').fetchone()[0],1)
        self.assertEqual(self.get('/api/watchlist/videos').json['items'],[])

    def test_legacy_aliases_are_read_without_migration_and_removed_together(self):
        movie=self.item();alias=dict(movie,id='imdb:movie:77')
        with self.store.connect() as db:
            for key,item in [('imdb:movie:77',alias),('movie:77',movie)]:db.execute('INSERT INTO discover_watchlist VALUES(?,?,0)',(key,json.dumps(item)))
        self.assertTrue(self.status(alias).json['watchlisted']);self.assertEqual(len(self.get('/api/watchlist').json['movies']),1)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM discover_watchlist').fetchone()[0],2)
        self.assertFalse(self.post('/api/watchlist/item',dict(key='imdb:movie:77',watchlisted=False)).json['watchlisted'])
        self.assertEqual(self.get('/api/watchlist').json['movies'],[])
        self.assertTrue(ds.toggle_watch(self.store,alias)['saved']);self.assertFalse(ds.toggle_watch(self.store,movie)['saved'])

    def test_vod_and_nzb_share_verified_identity_without_fetching_stream(self):
        body=self.vod(kind='movies',provider='imdb',identity='77')
        item=self.get('/api/favourites/vod-title?'+urlencode(body)).json['item']
        self.assertEqual(self.save(item).json['key'],'movie:77')
        self.assertTrue(self.status(self.item()).json['watchlisted'])
        self.assertEqual(len(self.get('/api/watchlist').json['movies']),1)
        self.save(self.item(),False);self.assertEqual(self.get('/api/watchlist').json['movies'],[])

    def test_picker_scroll_pages_search_and_scheduled_flags(self):
        for n in range(63):self.video(n)
        with self.store.connect() as db:
            row=json.loads(db.execute('SELECT data FROM videos WHERE id=?',('00000000000',)).fetchone()[0]);row['state']='scheduled'
            db.execute('UPDATE videos SET data=? WHERE id=?',(json.dumps(row),'00000000000'))
        self.save(dict(id='00000000000',kind='video',title='Video'))
        first=self.get('/api/watchlist/videos').json;second=self.get('/api/watchlist/videos?offset=60').json
        self.assertEqual(len(first['items']),60);self.assertEqual(first['nextOffset'],60)
        self.assertTrue(first['items'][0]['watchlisted']);self.assertEqual(len(second['items']),3);self.assertIsNone(second['nextOffset'])
        self.assertEqual(len(self.get('/api/watchlist/videos?q=Video%2062').json['items']),1)
        self.assertEqual(self.get('/api/watchlist/videos?q=%25').json['items'],[])

    def test_date_tba_and_future_show_episode_without_policy_creation(self):
        movie=self.item();show=self.item('show')
        self.save(movie);self.save(show)
        records.remember(self.store,'show:77:1:1',show['id'],'episode',dict(item=show,date='2099-01-01',episode=dict(season=1,number=1),automatic=True))
        before=self.protected();result=self.get('/api/watchlist').json
        self.assertEqual(result['movies'][0]['watchStatus'],'Date TBA')
        self.assertEqual(result['shows'][0]['watchStatus'],'Next episode');self.assertEqual(result['shows'][0]['watchDate'],'2099-01-01')
        self.assertEqual(self.protected(),before)

    def test_enabled_channels_import_once_without_policy_or_favourite_changes(self):
        _,channel=self.video()
        with self.store.connect() as db:
            db.execute('UPDATE channels SET active=1,data=? WHERE id=?',(json.dumps(dict(name='Technology channel',description='Computers',avatar='/artwork/avatar.jpg',banner='/artwork/banner.jpg',credential='DO_NOT_EXPORT')),channel))
            db.execute('INSERT INTO download_rules(channel,videos) VALUES(?,1)',(channel,))
        before=self.protected();watchlist.sync_auto_channels(self.store)
        after=self.protected();self.assertEqual(after['download_rules'],before['download_rules'])
        for table in before:
            if table!='settings':self.assertEqual(after[table],before[table],table)
        self.assertEqual({key for key,value in after['settings']} - {key for key,value in before['settings']},{watchlist.AUTO_CHANNELS_SEEN})
        channel_entry=self.get('/api/watchlist').json['channels'][0]
        self.assertEqual(channel_entry['id'],channel);self.assertEqual(channel_entry['watchStatus'],'Automatic downloads on')
        self.assertEqual(channel_entry['image'],'/artwork/avatar.jpg');self.assertEqual(channel_entry['genres'],['Technology'])
        self.assertEqual(channel_entry['indexedVideos'],1);self.assertNotIn('DO_NOT_EXPORT',str(channel_entry))
        selected=self.protected();watchlist.sync_auto_channels(self.store);self.get('/api/watchlist');self.assertEqual(self.protected(),selected)

    def test_automatic_channel_stays_after_policy_off_and_removal_survives_restart(self):
        from backend import channel_downloads
        _,channel=self.video()
        with self.store.connect() as db:db.execute('UPDATE channels SET active=1 WHERE id=?',(channel,))
        channel_downloads.save(self.store,channel,dict(shorts=True))
        self.assertEqual(len(self.get('/api/watchlist').json['channels']),1)
        channel_downloads.save(self.store,channel,dict(shorts=False))
        self.assertEqual(self.get('/api/watchlist').json['channels'][0]['watchStatus'],'Automatic downloads off')
        channel_downloads.save(self.store,channel,dict(live=True))
        self.assertFalse(self.post('/api/watchlist/item',dict(key=channel,watchlisted=False)).json['watchlisted'])
        channel_downloads.save(self.store,channel,dict(quality='720'))
        app=vm.create_app({'TESTING':True,'DATA_DIR':self.tmp.name,'MEDIA_DIR':str(self.root)})
        self.assertEqual(watchlist.listing(app,app.extensions['store'])['channels'],[])
        self.assertTrue(self.save(dict(id=channel,kind='channel',title='Creator')).json['watchlisted'])

    def test_worker_includes_new_enabled_channels_and_skips_inactive_and_manual_only(self):
        from unittest.mock import Mock
        from backend.worker import tick
        _,channel=self.video()
        other='UC'+'b'*22;manual='UC'+'c'*22
        with self.store.connect() as db:
            db.execute('UPDATE channels SET active=1 WHERE id=?',(channel,))
            for key,active,enabled in [(channel,1,1),(other,0,1),(manual,1,0)]:
                db.execute('INSERT OR IGNORE INTO channels VALUES(?,?,?)',(key,json.dumps(dict(name='Creator')),active))
                db.execute('INSERT INTO download_rules(channel,live,subscription_paused) VALUES(?,?,1)',(key,enabled))
        self.store.set('youtube_sync_paused','true')
        importer=Mock(store=self.store);tick(importer,download_artwork=False);importer.run.assert_not_called()
        entries=self.get('/api/watchlist').json['channels']
        self.assertEqual([r['id'] for r in entries],[channel]);self.assertEqual(entries[0]['watchStatus'],'Automatic downloads paused')
        self.assertEqual(self.get('/api/watchlist').json['videos'],[])

    def plan(self,item,field='future',value=True):
        records.remember(self.store,item['id']+':1:1',item['id'],'episode',dict(item=item,episode=dict(season=1,number=1),date='2099-01-01',automatic=True))
        with patch.object(engine,'schedule'):return engine.save_plan(self.app,self.store,item['id'],field,value)

    def test_movie_and_both_tv_plan_switches_add_immediately_and_deduplicate(self):
        movie=self.item();future=self.item('show','88');all_seasons=self.item('show','99')
        movie_options.save(self.app,self.store,movie,dict(automatic=True))
        self.plan(future);self.plan(all_seasons,'all');self.plan(all_seasons,'future')
        result=self.get('/api/watchlist').json
        self.assertEqual([v['watchlistKey'] for v in result['movies']],['movie:77'])
        self.assertEqual({v['watchlistKey'] for v in result['shows']},{'show:88','show:99'})
        self.assertTrue(next(v for v in result['shows'] if v['id']=='show:99')['downloadAll'])
        self.assertTrue(next(v for v in result['shows'] if v['id']=='show:88')['downloadFuture'])
        before=self.protected();watchlist.sync_automatic(self.store);self.get('/api/watchlist')
        self.assertEqual(self.protected(),before)

    def test_startup_imports_existing_automation_without_touching_policies_or_jobs(self):
        movie=self.item();show=self.item('show','88')
        movie_options.ensure(self.store,movie)
        with self.store.connect() as db:
            db.execute('UPDATE movie_options SET automatic=1')
            db.execute('INSERT INTO discover_series_plan VALUES(?,?,1,0,?,0)',(show['id'],json.dumps(show),'2026-01-01'))
        before=self.protected()
        with patch('backend.downloads.enqueue') as download,patch.object(engine,'queue') as queue,patch('backend.discover_sources.episodes') as provider:
            app=vm.create_app({'TESTING':True,'DATA_DIR':self.tmp.name,'MEDIA_DIR':str(self.root)})
        result=watchlist.listing(app,app.extensions['store'])
        self.assertEqual([len(result[k]) for k in ('movies','shows','videos','channels')],[1,1,0,0])
        after=self.protected();after['settings']=[r for r in after['settings'] if r[0]!=watchlist.AUTO_TITLES_SEEN]
        self.assertEqual(after,before)
        download.assert_not_called();queue.assert_not_called();provider.assert_not_called()

    def test_titles_stay_when_disabled_and_explicit_removal_survives_policy_saves_and_restart(self):
        movie=self.item();show=self.item('show','88')
        movie_options.save(self.app,self.store,movie,dict(automatic=True));self.plan(show,'all')
        movie_options.save(self.app,self.store,movie,dict(automatic=False));self.plan(show,'all',False)
        result=self.get('/api/watchlist').json
        self.assertEqual([len(result[k]) for k in ('movies','shows')],[1,1])
        self.assertFalse(result['movies'][0]['automatic']);self.assertFalse(result['shows'][0]['downloadAll'])
        self.save(movie,False);self.post('/api/watchlist/item',dict(key='show:88',watchlisted=False))
        movie_options.save(self.app,self.store,movie,dict(quality='720'))
        app=vm.create_app({'TESTING':True,'DATA_DIR':self.tmp.name,'MEDIA_DIR':str(self.root)})
        result=watchlist.listing(app,app.extensions['store'])
        self.assertEqual(result['movies'],[]);self.assertEqual(result['shows'],[])
        movie_options.save(self.app,self.store,movie,dict(automatic=True));self.plan(show,'future')
        self.assertTrue(self.status(movie).json['watchlisted']);self.assertTrue(self.status(show).json['watchlisted'])

    def test_legacy_enabled_rules_and_tvmaze_plans_keep_their_actual_identity(self):
        from backend import discover_monitor
        show=self.item('show');other=dict(id='tvmaze:show:88',kind='show',title='Known show')
        discover_monitor.save_rule(self.store,dict(showId='77',quality='1080',fallback=False,enabled=True,item=show),[dict(season=1,number=1,airdate='2026-01-01')])
        self.plan(other)
        self.assertEqual({v['watchlistKey'] for v in self.get('/api/watchlist').json['shows']},{'show:77','tvmaze:show:88'})
        discover_monitor.set_enabled(self.store,'77',False);watchlist.sync_automatic(self.store)
        self.assertEqual(len(self.get('/api/watchlist').json['shows']),2)

    def test_explicit_monitor_and_all_series_policy_add_but_default_catalogue_and_holds_do_not(self):
        for number in range(1,7):
            item=self.item('show',str(number))
            records.remember(self.store,'show:'+str(number)+':1:1',item['id'],'episode',dict(item=item,episode=dict(season=1,number=1),automatic=True))
        with self.store.connect() as db:
            db.execute("UPDATE managed_media SET monitor=1 WHERE parent IN ('show:2','show:3','show:4','show:5')")
            db.execute("UPDATE managed_media SET hold=1 WHERE parent='show:3'")
            db.execute("UPDATE managed_media SET removed=1 WHERE parent='show:4'")
            db.execute("INSERT INTO managed_series VALUES('show:5','5','1080',0,'off')")
            db.execute("INSERT INTO managed_series VALUES('show:6','6','1080',0,'all')")
        before=self.protected();watchlist.sync_auto_titles(self.store)
        self.assertEqual({v['watchlistKey'] for v in self.get('/api/watchlist').json['shows']},{'show:2','show:6'})
        after=self.protected();after['settings']=[r for r in after['settings'] if r[0]!=watchlist.AUTO_TITLES_SEEN]
        self.assertEqual(after,before)

    def test_management_policy_saves_include_titles_immediately(self):
        show=self.item('show');key='show:77:1:1'
        records.remember(self.store,key,show['id'],'episode',dict(item=show,episode=dict(season=1,number=1),automatic=True))
        response=self.post('/api/media-management',dict(action='series-settings',parent=show['id'],field='monitoring',value='all'))
        self.assertEqual(response.status_code,200,response.json)
        self.assertTrue(self.status(show).json['watchlisted'])
        movie=self.item('movie','88')
        records.remember(self.store,'movie:88',movie['id'],'movie',dict(item=movie,automatic=True))
        response=self.post('/api/media-management',dict(action='monitor',key='movie:88',value=True))
        self.assertEqual(response.status_code,200,response.json)
        self.assertTrue(self.status(movie).json['watchlisted'])

    def test_worker_imports_movies_and_shows_even_when_youtube_sync_is_paused(self):
        from unittest.mock import Mock
        from backend.worker import tick
        movie=self.item();show=self.item('show')
        movie_options.ensure(self.store,movie)
        with self.store.connect() as db:
            db.execute('UPDATE movie_options SET automatic=1')
            db.execute('INSERT INTO discover_series_plan VALUES(?,?,0,1,?,0)',(show['id'],json.dumps(show),'2026-01-01'))
        self.store.set('youtube_sync_paused','true')
        importer=Mock(store=self.store);tick(importer,download_artwork=False)
        result=self.get('/api/watchlist').json
        self.assertEqual([len(result[k]) for k in ('movies','shows')],[1,1]);importer.run.assert_not_called()

    def test_channel_identity_alias_and_mismatch(self):
        _,channel=self.video();item=dict(id='youtube:channel:'+channel,channelId=channel,kind='channel',title='Creator')
        with self.store.connect() as db:db.execute('UPDATE channels SET active=1 WHERE id=?',(channel,))
        self.assertTrue(self.save(item).json['watchlisted'])
        self.assertTrue(self.status(dict(item,id=channel)).json['watchlisted'])
        self.assertEqual(self.get('/api/watchlist').json['channels'][0]['id'],channel)
        self.assertEqual(self.save(dict(item,channelId='UC'+'b'*22)).status_code,400)

    def test_remove_tv_disables_both_plans_legacy_and_explicit_automation_atomically(self):
        from backend import discover_monitor
        show=self.item('show');other=self.item('show','88')
        self.plan(show,'all');self.plan(show,'future');self.plan(other,'future')
        discover_monitor.save_rule(self.store,dict(showId='77',quality='720',fallback=True,enabled=True,item=show),[dict(season=1,number=1,airdate='2026-01-01')])
        with self.store.connect() as db:
            db.execute("INSERT INTO managed_series VALUES('show:77','77','720',1,'all')")
            db.execute("UPDATE managed_media SET monitor=1,hold=1,quality='2160',fallback=1 WHERE parent='show:77'")
        self.assertFalse(self.post('/api/watchlist/item',dict(key='show:77',watchlisted=False)).json['watchlisted'])
        with self.store.connect() as db:
            self.assertEqual(tuple(db.execute("SELECT download_all,download_future FROM discover_series_plan WHERE parent='show:77'").fetchone()),(0,0))
            self.assertEqual(db.execute('SELECT enabled FROM discover_rules').fetchone()[0],0)
            self.assertEqual(tuple(db.execute('SELECT quality,fallback,monitoring FROM managed_series').fetchone()),('720',1,'inherit'))
            self.assertEqual(tuple(db.execute("SELECT monitor,hold,quality,fallback FROM managed_media WHERE parent='show:77'").fetchone()),(-1,1,'2160',1))
            self.assertEqual(db.execute("SELECT download_future FROM discover_series_plan WHERE parent='show:88'").fetchone()[0],1)
        self.assertTrue(self.status(other).json['watchlisted'])
        watchlist.sync_automatic(self.store);self.assertFalse(self.status(show).json['watchlisted'])
        # Explicit re-add now enables future downloads, retaining holds and quality.
        self.save(show)
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT download_future FROM discover_series_plan WHERE parent='show:77'").fetchone()[0],1)
        self.plan(show,'future');self.assertTrue(self.get('/api/watchlist').json['shows'][0]['downloadFuture'])

    def test_remove_withdraws_only_pre_engine_automatic_requests_and_fences_stale_start(self):
        show=self.item('show');self.plan(show)
        pending=[]
        for number,state,manual,engine_id,plan in [(1,'waiting',1,'','future'),(2,'searching',0,'',''),(3,'starting',0,'',''),(4,'downloading',0,'active','future'),(5,'waiting',1,'','')]:
            key='show:77:1:'+str(number)
            records.remember(self.store,key,show['id'],'episode',dict(item=show,episode=dict(season=1,number=number),automatic=True))
            row,_=ds.insert_request(self.store,dict(idempotency=str(number),kind='episode',title='Episode',quality='1080',fallback=False,episode_key=key,state=state,manual=manual,engine_id=engine_id,body=dict(item=show,plan=plan,planParent=show['id'])))
            pending.append(row)
        self.save(show,False)
        with self.store.connect() as db:
            states=[db.execute('SELECT state FROM discover_requests WHERE id=?',(r['id'],)).fetchone()[0] for r in pending]
            self.assertEqual(states,['cancelled','cancelled','cancelled','downloading','waiting'])
        self.assertFalse(ds.commit_engine(self.store,pending[2]['id'],0,'nzb','stale','stale'))
        row,created=ds.insert_request(self.store,dict(idempotency='stale-plan',kind='episode',title='Episode',quality='1080',fallback=False,episode_key='show:77:1:6',automatic=True,body=dict(item=show,plan='future',planParent=show['id'])))
        self.assertFalse(created)

    def test_remove_movie_disables_automatic_policy_and_rejects_stale_automatic_insert(self):
        movie=self.item();movie_options.save(self.app,self.store,movie,dict(automatic=True,quality='720',minimum=5,favourite=True))
        self.save(movie,False)
        row=movie_options.find(self.store,movie)
        self.assertFalse(row['automatic']);self.assertEqual(row['quality'],'720');self.assertEqual(row['minimum'],5);self.assertTrue(row['favourite'])
        stale,created=ds.insert_request(self.store,dict(idempotency='stale-movie',kind='movie',title='Movie',quality='1080',fallback=False,episode_key='movie:77',automatic=True,body=dict(item=movie,itemId=movie['id'],movieAutomatic=True)))
        self.assertFalse(created)
        movie_options.save(self.app,self.store,movie,dict(automatic=True))
        self.assertTrue(movie_options.snapshot(self.store,movie_options.find(self.store,movie))['automatic'])

    def test_channel_removal_disables_types_keeps_subscription_favourites_quality_and_running_jobs(self):
        from backend import channel_downloads
        first,channel=self.video();second,_=self.video(1);third,_=self.video(2)
        with self.store.connect() as db:
            db.execute('UPDATE channels SET active=1 WHERE id=?',(channel,))
            db.execute('INSERT INTO channel_favourites VALUES(?)',(channel,))
        channel_downloads.save(self.store,channel,dict(videos=True,shorts=True,live=True,quality='720'))
        with self.store.connect() as db:
            for video,state,automatic in [(first,'queued',1),(second,'downloading',1),(third,'queued',0)]:
                db.execute('INSERT INTO downloads(id,title,quality,state,created,updated,automatic) VALUES(?,?,?,?,0,0,?)',(video,'Video','720',state,automatic))
        self.post('/api/watchlist/item',dict(key=channel,watchlisted=False))
        with self.store.connect() as db:
            self.assertEqual(tuple(db.execute('SELECT videos,shorts,live,quality FROM download_rules WHERE channel=?',(channel,)).fetchone()),(0,0,0,'720'))
            self.assertEqual(db.execute('SELECT active FROM channels WHERE id=?',(channel,)).fetchone()[0],1)
            self.assertTrue(db.execute('SELECT 1 FROM channel_favourites WHERE id=?',(channel,)).fetchone())
            self.assertEqual([db.execute('SELECT state FROM downloads WHERE id=?',(v,)).fetchone()[0] for v in [first,second,third]],['skipped','downloading','queued'])

    def test_channel_watchlist_on_restores_type_choices_across_restart(self):
        from backend import channel_downloads
        _,channel=self.video()
        with self.store.connect() as db:db.execute('UPDATE channels SET active=1 WHERE id=?',(channel,))
        channel_downloads.save(self.store,channel,dict(videos=True,shorts=False,live=True,quality='720',subscription_paused=True))
        self.post('/api/watchlist/item',dict(key=channel,watchlisted=False))
        app=vm.create_app({'TESTING':True,'DATA_DIR':self.tmp.name,'MEDIA_DIR':str(self.root)})
        watchlist.set_item(app.extensions['store'],dict(id=channel,kind='channel',title='Creator'),True)
        with self.store.connect() as db:
            self.assertEqual(tuple(db.execute('SELECT videos,shorts,live,quality,subscription_paused FROM download_rules WHERE channel=?',(channel,)).fetchone()),(1,0,1,'720',1))
        # Another off/on must keep the same choices, not broaden to every type.
        self.save(dict(id=channel,kind='channel',title='Creator'),False)
        self.save(dict(id=channel,kind='channel',title='Creator'))
        self.assertEqual(channel_downloads.preferences(self.store,channel)['shorts'],0)
        self.assertEqual(self.get('/api/watchlist').json['channels'][0]['watchStatus'],'Automatic downloads paused')

    def test_channel_watchlist_on_without_saved_choices_defaults_to_normal_videos(self):
        from backend import channel_downloads
        _,channel=self.video()
        with self.store.connect() as db:db.execute('UPDATE channels SET active=1 WHERE id=?',(channel,))
        channel_downloads.save(self.store,channel,dict(quality='720'))
        self.save(dict(id=channel,kind='channel',title='Creator'))
        rule=channel_downloads.preferences(self.store,channel)
        self.assertEqual((rule['videos'],rule['shorts'],rule['live'],rule['quality']),(1,0,0,'720'))

    def test_removal_and_policy_changes_roll_back_together_on_failure(self):
        show=self.item('show');self.plan(show)
        with patch('backend.watchlist.remember_titles',side_effect=ValueError('fixture write failure')):
            self.assertEqual(self.save(show,False).status_code,400)
        self.assertTrue(self.status(show).json['watchlisted'])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT download_future FROM discover_series_plan').fetchone()[0],1)

    def test_unknown_remove_key_cannot_poison_automatic_membership_history(self):
        before=self.protected()
        self.assertEqual(self.post('/api/watchlist/item',dict(key='not a saved identity',watchlisted=False)).status_code,200)
        watchlist.sync_automatic(self.store)
        self.assertEqual(self.protected(),before)
        self.assertEqual(self.get('/api/watchlist').status_code,200)

    def test_explicit_add_enables_movie_and_future_tv_without_all_seasons_or_clearing_holds(self):
        from backend import show_options
        movie=self.item();show=self.item('show','88')
        movie_options.save(self.app,self.store,movie,dict(favourite=True))
        movie_options.save(self.app,self.store,movie,dict(quality='720'))
        records.remember(self.store,'movie:77',movie['id'],'movie',dict(item=movie,automatic=True,date='2099-01-01'))
        with self.store.connect() as db:db.execute("UPDATE managed_media SET hold=1,quality='2160',fallback=1")
        with patch.object(engine,'schedule') as scheduler,patch('backend.downloads.enqueue') as download:
            self.assertTrue(self.save(movie).json['watchlisted']);self.assertTrue(self.save(show).json['watchlisted'])
        scheduler.assert_not_called();download.assert_not_called()
        option=movie_options.find(self.store,movie)
        self.assertEqual((option['automatic'],option['quality'],option['favourite']),(1,'720',1))
        with self.store.connect() as db:
            self.assertEqual(tuple(db.execute("SELECT monitor,hold,quality,fallback FROM managed_media WHERE key='movie:77'").fetchone()),(1,1,'2160',1))
            plan=dict(db.execute("SELECT * FROM discover_series_plan WHERE parent='show:88'").fetchone())
        self.assertEqual((plan['download_all'],plan['download_future']),(0,1))
        self.assertTrue(show_options.snapshot(self.store,show_options.ensure(self.store,show))['downloadFuture'])
        self.save(show);self.save(movie)
        with self.store.connect() as db:self.assertEqual(dict(db.execute("SELECT * FROM discover_series_plan WHERE parent='show:88'").fetchone()),plan)
        self.assertEqual(movie_options.find(self.store,movie)['enabled_at'],option['enabled_at'])

    def test_new_tv_watchlist_opt_in_replaces_older_all_seasons_choice_with_future_only(self):
        from backend import show_options
        for future in (False,True):
            with self.subTest(previous_future=future):
                show=self.item('show',str(88+int(future)))
                other=self.item('show','90')
                self.plan(show,'all');self.plan(show,'future',future);self.plan(other,'all')
                show_options.save(self.app,self.store,show,dict(quality='720'))
                records.remember(self.store,show['id']+':1:1',show['id'],'episode',dict(item=show,episode=dict(season=1,number=1),date='2099-01-01',automatic=True))
                with self.store.connect() as db:
                    db.execute("DELETE FROM discover_watchlist WHERE id=?",(show['id'],))
                    db.execute("UPDATE discover_series_plan SET future_from='2025-01-01' WHERE parent=?",(show['id'],))
                    db.execute('UPDATE managed_media SET hold=1 WHERE parent=?',(show['id'],))
                    before=dict(db.execute('SELECT * FROM discover_series_plan WHERE parent=?',(other['id'],)).fetchone())
                with patch('backend.media_management.service.today',return_value='2026-10-16'),patch.object(engine,'schedule') as scheduler,patch.object(engine,'queue') as queue:
                    self.assertTrue(self.save(show).json['watchlisted'])
                scheduler.assert_not_called();queue.assert_not_called()
                with self.store.connect() as db:
                    plan=db.execute('SELECT download_all,download_future,future_from FROM discover_series_plan WHERE parent=?',(show['id'],)).fetchone()
                    self.assertEqual(tuple(plan),(0,1,'2025-01-01' if future else '2026-10-16'))
                    self.assertEqual(dict(db.execute('SELECT * FROM discover_series_plan WHERE parent=?',(other['id'],)).fetchone()),before)
                    self.assertEqual(db.execute('SELECT hold FROM managed_media WHERE parent=?',(show['id'],)).fetchone()[0],1)
                self.assertEqual(show_options.find(self.store,show)['quality'],'720')
                # A repeated save is idempotent: a later deliberate All Seasons
                # choice in Options remains independent of the selected bookmark.
                self.plan(show,'all');self.save(show)
                with self.store.connect() as db:self.assertEqual(tuple(db.execute('SELECT download_all,download_future FROM discover_series_plan WHERE parent=?',(show['id'],)).fetchone()),(1,1))

    def test_individual_video_waits_then_worker_queues_automatic_and_removal_withdraws_it(self):
        from backend.media_management import tick
        from backend import downloads
        video,channel=self.video();item=dict(id=video,videoId=video,kind='video',title='A scheduled upload')
        with self.store.connect() as db:
            value=json.loads(db.execute('SELECT data FROM videos WHERE id=?',(video,)).fetchone()[0]);value.update(state='scheduled',date='2099-01-01')
            db.execute('UPDATE videos SET data=? WHERE id=?',(json.dumps(value),video))
        self.save(item)
        with patch('backend.media_management.service.today',return_value='2026-01-01'):tick(self.app,self.store)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM downloads').fetchone()[0],0)
        value['state']='published'
        with self.store.connect() as db:
            db.execute('UPDATE videos SET data=? WHERE id=?',(json.dumps(value),video));db.execute('UPDATE managed_media SET next_check=0,lease_until=0')
        with patch('backend.media_management.service.today',return_value='2100-01-01'):tick(self.app,self.store)
        with self.store.connect() as db:self.assertEqual(tuple(db.execute('SELECT state,automatic FROM downloads').fetchone()),('queued',1))
        self.save(item,False)
        with self.store.connect() as db:self.assertEqual(tuple(db.execute('SELECT state,automatic FROM downloads').fetchone()),('skipped',1))
        downloads.enqueue(self.store,value,'1080',automatic=True)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT state FROM downloads').fetchone()[0],'skipped')
        self.assertFalse(self.status(item).json['watchlisted'])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT active FROM channels WHERE id=?',(channel,)).fetchone()[0],0)


if __name__=='__main__':unittest.main()
