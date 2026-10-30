"""Management integration tests use disposable storage and mocked providers only."""
import json
import time
import unittest
import threading
from pathlib import Path
from unittest.mock import Mock, patch
import test_view_media as vm
from backend.media_management import change,tick
from backend.media_management import store as records,service,files
from backend import discover_store as ds,discover_acquire as acquire

class ManagementTests(unittest.TestCase):
    setUp=vm.ViewMediaTests.setUp
    tearDown=vm.ViewMediaTests.tearDown
    get=vm.ViewMediaTests.get
    def post(self,body,csrf=True):
        return self.client.post('/api/media-management',base_url=vm.ORIGIN,json=body,headers={'Origin':vm.ORIGIN,**({'X-CSRF-Token':'token'} if csrf else {})})
    def scan_identity(self,path):
        self.store.media_root=str(self.root)
        state={};vm.local._scan(self.root,state);vm.local._CACHE[str(self.root)]=state
        return next(identity for identity,entry in state['files'].items() if entry['path']==path)
    def enrol_local(self,identity):
        return self.post(dict(action='enrol-local',value=dict(id=identity)))
    def test_enrol_local_owned_path_reuses_key_and_preserves_policy(self):
        r,p=self.movie();identity=self.scan_identity(p)
        with self.store.connect() as db:
            db.execute('UPDATE managed_media SET monitor=0,hold=1,removed=1 WHERE key=?',(r['key'],))
        p.with_suffix('.nfo').write_text('<movie><tmdbid>999</tmdbid></movie>')
        with patch('backend.view_media.providers.details') as details:
            response=self.enrol_local(identity)
        self.assertEqual(response.status_code,200,response.json);details.assert_not_called()
        self.assertEqual(response.json['key'],r['key']);self.assertEqual(response.json['entry']['key'],r['key'])
        self.assertTrue(response.json['entry']['file']);self.assertTrue(response.json['entry']['hold'])
        self.assertTrue(response.json['entry']['removed']);self.assertFalse(response.json['entry']['monitored'])
        self.assertEqual(service.body(records.row(self.store,r['key']))['localPath'],str(p))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM managed_media').fetchone()[0],1)

    def test_enrol_local_owned_path_without_managed_row_keeps_existing_key(self):
        r,p=self.movie()
        with self.store.connect() as db:db.execute('DELETE FROM managed_media WHERE key=?',(r['key'],))
        response=self.enrol_local(self.scan_identity(p))
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['key'],r['key']);self.assertTrue(response.json['entry']['automatic'])
        self.assertTrue(response.json['entry']['file'])

    def test_enrol_local_youtube_reservation_uses_video_key(self):
        video='abcdefghijk';p=self.root/'YouTube'/'Channel'/'Video.mp4'
        p.parent.mkdir(parents=True);p.write_bytes(b'video')
        with self.store.connect() as db:
            db.execute('INSERT INTO media_files(id,channel,path,day,episode,profile,bytes) VALUES(?,?,?,?,?,?,?)',
                       (video,'channel',str(p.relative_to(self.root)),'2026-01-02',1,'emby',5))
            db.execute("INSERT INTO downloads(id,title,quality,state,created,updated) VALUES(?,?,?,'complete',0,0)",(video,'Saved video','1080'))
        identity=self.scan_identity(p)
        with patch('backend.view_media.providers.details') as details:
            response=self.enrol_local(identity)
            again=self.enrol_local(identity)
        self.assertEqual(response.status_code,200,response.json);self.assertEqual(again.status_code,200,again.json)
        details.assert_not_called();self.assertEqual(response.json['key'],'video:'+video)
        self.assertEqual(response.json['entry']['videoId'],video);self.assertTrue(response.json['entry']['file'])
        self.assertEqual(service.body(records.row(self.store,'video:'+video))['localPath'],str(p))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM managed_media').fetchone()[0],1)

    def test_enrol_local_tmdb_movie_uses_canonical_imdb_key(self):
        p=self.root/'Movies'/'Film'/'Film.mkv';p.parent.mkdir(parents=True);p.write_bytes(b'film')
        p.with_suffix('.nfo').write_text('<movie><title>NFO Film</title><tmdbid>42</tmdbid><year>2026</year><premiered>2026-01-02</premiered></movie>')
        with patch('backend.view_media.providers.details',return_value=dict(id='tmdb:movie:42',kind='movie',title='Catalogue Film',externalId='0123456',year='2026')) as details:
            response=self.enrol_local(self.scan_identity(p))
        self.assertEqual(response.status_code,200,response.json);details.assert_called_once_with(self.store,'tmdb:movie:42')
        self.assertEqual(response.json['key'],'movie:0123456');self.assertTrue(response.json['entry']['automatic'])
        r=records.row(self.store,'movie:0123456');b=service.body(r)
        self.assertEqual(r['parent'],'tmdb:movie:42');self.assertEqual(b['item']['externalId'],'0123456')
        self.assertEqual(b['localPath'],str(p));self.assertTrue(response.json['entry']['file'])
        service.enrol(self.app,self.store,b['item'],refresh=False)
        self.assertEqual(service.library_file(self.app,self.store,records.row(self.store,r['key'])),p)

    def test_enrol_local_keeps_tmdb_identity_when_metadata_is_unavailable(self):
        p=self.root/'Film.mp4';p.write_bytes(b'film')
        p.with_suffix('.nfo').write_text('<movie><title>NFO Film</title><tmdbid>0</tmdbid><uniqueid type="tmdb">42</uniqueid></movie>')
        with patch('backend.view_media.providers.details',side_effect=ValueError('Metadata unavailable')):
            response=self.enrol_local(self.scan_identity(p))
        self.assertEqual(response.status_code,200,response.json);self.assertEqual(response.json['key'],'tmdb:movie:42')
        self.assertTrue(response.json['entry']['file']);self.assertFalse(response.json['entry']['automatic'])
        info=files.identity_info(self.store,records.row(self.store,response.json['key']))
        self.assertEqual((info['provider'],info['identity']),('tmdb','42'))

    def test_enrol_local_uniqueid_movie_and_folder_nfo_without_provider(self):
        p=self.root/'Movies'/'Film'/'Film.mp4';p.parent.mkdir(parents=True);p.write_bytes(b'film')
        (p.parent/'movie.nfo').write_text('<movie><title>Known Film</title><uniqueid type="imdb">tt0123456</uniqueid><premiered>2025-02-03</premiered></movie>')
        with patch('backend.view_media.providers.details') as details:
            response=self.enrol_local(self.scan_identity(p))
        self.assertEqual(response.status_code,200,response.json);details.assert_not_called()
        self.assertEqual(response.json['key'],'movie:0123456');self.assertTrue(response.json['entry']['automatic'])
        self.assertEqual(service.body(records.row(self.store,response.json['key']))['item']['year'],'2025')

    def test_enrol_local_episode_uses_tvdb_and_nfo_numbers_without_enrolling_series(self):
        p=self.root/'TV Shows'/'Show'/'Episode.mp4';p.parent.mkdir(parents=True);p.write_bytes(b'episode')
        (p.parent/'episodedetails.nfo').write_text('<episodedetails><title>Local Episode</title><showtitle>Known Show</showtitle><uniqueid type="tvdb">77</uniqueid><season>2026</season><episode>20</episode><aired>2026-01-03</aired></episodedetails>')
        with patch.object(service.sources,'episodes') as episodes:
            response=self.enrol_local(self.scan_identity(p))
        self.assertEqual(response.status_code,200,response.json);episodes.assert_not_called()
        self.assertEqual(response.json['key'],'show:77:2026:20');self.assertTrue(response.json['entry']['automatic'])
        self.assertEqual(response.json['entry']['series'],'Known Show');self.assertEqual(response.json['entry']['title'],'Local Episode')
        self.assertEqual(response.json['entry']['date'],'2026-01-03')
        with patch.object(service.sources,'search_episode',return_value=[]) as search:
            self.post(dict(action='releases',key=response.json['key']))
        search.assert_called_once_with(self.app,self.store,'77',2026,20)

    def test_enrol_local_without_nfo_can_rename_delete_and_report_no_identity(self):
        p=self.root/'Movies'/'Unknown (2024)'/'Unknown (2024).mkv';p.parent.mkdir(parents=True);p.write_bytes(b'film')
        identity=self.scan_identity(p);response=self.enrol_local(identity)
        self.assertEqual(response.status_code,200,response.json);key=response.json['key']
        self.assertEqual(key,'local:'+identity);self.assertTrue(response.json['entry']['file'])
        r=records.row(self.store,key);self.assertEqual(r['parent'],key);self.assertFalse(response.json['entry']['automatic'])
        self.assertEqual(service.body(r)['item']['year'],'2024')
        with patch.object(service.sources,'search_movie') as search:
            releases=self.post(dict(action='releases',key=key))
        self.assertEqual(releases.status_code,200,releases.json);self.assertEqual(releases.json['items'],[])
        self.assertIn('no verified movie identity',releases.json['message']);search.assert_not_called()
        self.assertEqual(self.post(dict(action='repair-metadata',key=key)).status_code,400)
        renamed=self.post(dict(action='rename',key=key,value='Corrected'))
        self.assertEqual(renamed.status_code,200,renamed.json);self.assertFalse(p.exists())
        destination=self.root/'Movies'/'Corrected (2024)'/'Corrected (2024).mkv'
        self.assertEqual(destination.read_bytes(),b'film');self.assertTrue(renamed.json['item']['file'])
        r=records.row(self.store,key);self.assertNotIn('localPath',service.body(r))
        self.assertEqual(service.library_file(self.app,self.store,r),destination)
        deleted=self.post(dict(action='delete',key=key,confirm=True,value=False))
        self.assertEqual(deleted.status_code,200,deleted.json);self.assertFalse(destination.exists())
        self.assertTrue(deleted.json['item']['hold']);self.assertFalse(deleted.json['item']['file'])
        history=self.post(dict(action='history',key=key)).json['items']
        self.assertTrue(any('Renamed '+str(p)+' -> '+str(destination) in h['message'] for h in history))
        self.assertTrue(any('Deleted '+str(destination) in h['message'] for h in history))

    def test_enrol_local_minimal_show_and_video_preserve_scan_kind(self):
        for folder,name,kind in (('TV Shows','Unknown.S01E01.mp4','episode'),('YouTube','Unknown.mp4','video')):
            with self.subTest(kind=kind):
                p=self.root/folder/name;p.parent.mkdir(exist_ok=True);p.write_bytes(b'video')
                response=self.enrol_local(self.scan_identity(p))
                self.assertEqual(response.status_code,200,response.json);key=response.json['key']
                self.assertEqual(response.json['entry']['kind'],kind);self.assertTrue(response.json['entry']['file'])
                self.assertFalse(response.json['entry']['automatic'])
                releases=self.post(dict(action='releases',key=key))
                self.assertEqual(releases.status_code,200,releases.json);self.assertIn('no verified '+kind+' identity',releases.json['message'])
                deleted=self.post(dict(action='delete',key=key,confirm=True))
                self.assertEqual(deleted.status_code,200,deleted.json);self.assertFalse(p.exists())

    def test_enrol_local_rejects_non_file_identity_and_invalid_values(self):
        p=self.root/'Film.mp4';p.write_bytes(b'video');identity=self.scan_identity(p)
        p.unlink();p.mkdir()
        for value in (dict(id=identity),dict(id='unknown'),dict(id=[]),{},None):
            with self.subTest(value=value):
                response=self.post(dict(action='enrol-local',value=value))
                self.assertEqual(response.status_code,400,response.json);self.assertIn('file',response.json['error'])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM managed_media').fetchone()[0],0)

    def test_key_scoped_listing_returns_only_requested_entry(self):
        first=self.remember();self.remember('show:77:1:2',number=2)
        state=self.get('/api/media-management?key='+first['key'])
        self.assertEqual(state.status_code,200,state.json);self.assertEqual([r['key'] for r in state.json['items']],[first['key']])
        self.assertEqual(service.listing(self.app,self.store,parent=first['parent'],kind='movie',key=first['key'])['items'],[])
        self.assertEqual(self.get('/api/media-management?key=unknown').json['items'],[])

    def test_local_path_candidates_reject_escape_links_empty_and_non_video(self):
        self.store.set('storage_base',str(self.root))
        key='local:test';records.remember(self.store,key,key,'movie',dict(item=dict(id=key,kind='movie',title='Local'),automatic=False))
        outside=Path(self.tmp.name)/'outside.mp4';outside.write_bytes(b'private')
        real=self.root/'Real';real.mkdir();video=real/'Video.mp4';video.write_bytes(b'video')
        alias=self.root/'Alias';alias.symlink_to(real,target_is_directory=True)
        linked=self.root/'Link.mp4';linked.symlink_to(video)
        empty=self.root/'Empty.mp4';empty.touch()
        text=self.root/'Text.txt';text.write_bytes(b'text')
        for path in (outside,alias/video.name,linked,empty,text,self.root/'..'/'outside.mp4'):
            with self.subTest(path=path):
                records.patch_body(self.store,key,localPath=str(path))
                self.assertIsNone(service.library_file(self.app,self.store,records.row(self.store,key)))
                self.assertEqual(self.post(dict(action='delete',key=key,confirm=True)).status_code,400)
        records.patch_body(self.store,key,localPath=str(video))
        self.assertEqual(service.library_file(self.app,self.store,records.row(self.store,key)),video)
        self.assertEqual(outside.read_bytes(),b'private');self.assertEqual(video.read_bytes(),b'video')

    def test_owned_path_takes_precedence_over_local_path(self):
        r,p=self.movie();other=p.with_name('Other.mp4');other.write_bytes(b'other')
        records.patch_body(self.store,r['key'],localPath=str(other))
        self.assertEqual(service.library_file(self.app,self.store,records.row(self.store,r['key'])),p)

    def test_enrol_local_never_reads_a_symlink_nfo(self):
        p=self.root/'Film.mp4';p.write_bytes(b'film')
        outside=Path(self.tmp.name)/'private.nfo';outside.write_text('<movie><imdbid>tt0123456</imdbid></movie>')
        p.with_suffix('.nfo').symlink_to(outside)
        response=self.enrol_local(self.scan_identity(p))
        self.assertEqual(response.status_code,200,response.json);self.assertTrue(response.json['key'].startswith('local:'))
        self.assertFalse(response.json['entry']['automatic'])

    def test_enrol_local_is_rate_limited_and_requires_csrf(self):
        p=self.root/'Film.mp4';p.write_bytes(b'film');identity=self.scan_identity(p)
        self.assertEqual(self.post(dict(action='enrol-local',value=dict(id=identity)),csrf=False).status_code,403)
        with patch('backend.discover._limited',return_value=False) as limited:
            response=self.enrol_local(identity)
        self.assertEqual(response.status_code,429);limited.assert_called_once_with(self.app,'managed_media_search',30)
    def remember(self,key='show:77:1:1',kind='episode',day='2026-01-01',number=1):
        records.remember(self.store,key,'test:show:77',kind,dict(item=dict(id='test:show:77',kind='show',title='Test Show',externalId='77'),episode=dict(id=1,name='Pilot',season=1,number=number),date=day,automatic=True))
        return records.row(self.store,key)
    def file(self):
        self.store.set('storage_base',str(self.root))
        p=self.root/'Test'/'Pilot.mp4';p.parent.mkdir();p.write_bytes(b'test video')
        ds.mark_owned(self.store,'show:77:1:1','test-request',str(p))
        return p
    def test_auth_and_csrf(self):
        self.assertNotEqual(self.app.test_client().get('/api/media-management',base_url=vm.ORIGIN).status_code,200)
        self.assertEqual(self.post(dict(action='monitor'),False).status_code,403)
        self.assertEqual(self.post([]).status_code,400)
    def test_owned_and_upcoming_appear_together_without_jobs(self):
        self.remember();self.file()
        self.remember('show:77:1:2',day='2099-01-01',number=2)
        result=service.listing(self.app,self.store)['items']
        self.assertEqual({r['status'] for r in result},{'Upcoming','Saved · metadata incomplete'})
        self.assertEqual(sum(r['file'] for r in result),1)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM discover_requests').fetchone()[0],0)
    def test_monitor_and_remove_block_reacquisition_restore_keeps_pause(self):
        r=self.remember();change(self.app,self.store,r['key'],'monitor',True)
        self.assertFalse(records.policy_blocks(self.store,r['key']))
        change(self.app,self.store,r['key'],'remove',confirm=True)
        self.assertTrue(records.policy_blocks(self.store,r['key']))
        state=change(self.app,self.store,r['key'],'restore')
        self.assertTrue(state['hold']);self.assertFalse(state['monitored'])
    def test_remove_keeps_video_and_delete_requires_confirmation(self):
        r=self.remember();p=self.file()
        response=self.post(dict(action='delete',key=r['key']))
        self.assertEqual(response.status_code,400);self.assertTrue(p.exists())
        change(self.app,self.store,r['key'],'remove',confirm=True)
        self.assertTrue(p.exists())
    def test_delete_only_video_and_pause(self):
        r=self.remember();p=self.file();side=p.with_suffix('.nfo');side.write_text('<episodedetails/>')
        other=p.parent/'Other.mp4';other.write_bytes(b'keep')
        state=change(self.app,self.store,r['key'],'delete',confirm=True)
        self.assertFalse(p.exists());self.assertTrue(other.exists());self.assertTrue(side.exists())
        self.assertTrue(state['hold']);self.assertFalse(state['file'])
        self.assertEqual(ds.owned(self.store,r['key'])['state'],'deleted')
    def test_symlink_file_is_not_exposed_or_deleted(self):
        r=self.remember();p=self.file();p.unlink()
        outside=Path(self.tmp.name)/'outside.mp4';outside.write_bytes(b'keep');p.symlink_to(outside)
        self.assertIsNone(service.library_file(self.app,self.store,r))
        self.assertEqual(self.post(dict(action='delete',key=r['key'],confirm=True)).status_code,400)
        self.assertEqual(outside.read_bytes(),b'keep')
    def test_managed_file_download_route_is_removed(self):
        r=self.remember();self.file()
        for query in (r['key'],'/etc/passwd'):
            self.assertEqual(self.get('/api/media-management/file?key='+query).status_code,404)
        self.assertEqual(self.client.get('/api/media-management/file?key='+r['key'],base_url=vm.ORIGIN,headers={'Range':'bytes=0-3'}).status_code,404)
        self.assertTrue(service.library_file(self.app,self.store,records.row(self.store,r['key'])))
    def test_enrol_keeps_monitor_policy_and_specials(self):
        data=dict(showId=12,episodes=[dict(id=1,name='Pilot',season=1,number=1,airdate='2026-01-01'),dict(id=2,name='Special',season=0,number=1,airdate='2026-01-02')])
        item=dict(id='test:show:77',kind='show',title='Test Show',externalId='77')
        with patch.object(service.sources,'episodes',return_value=data):
            result=service.enrol(self.app,self.store,item)
            key=result['items'][0]['key'];change(self.app,self.store,key,'monitor',True)
            again=service.enrol(self.app,self.store,item)
        self.assertEqual({r['season'] for r in again['items']},{0,1})
        self.assertTrue(next(r for r in again['items'] if r['key']==key)['monitored'])
    def test_identity_enrichment_folds_into_existing_entry_and_keeps_hold(self):
        item=dict(id='tmdb:movie:755679',kind='movie',title='Fast Forever',year='2028',releaseDate='2028-03-15')
        records.remember(self.store,'tmdb:movie:755679',item['id'],'movie',dict(item=item,date='2028-03-15',dateLabel='Release date',automatic=True))
        change(self.app,self.store,'tmdb:movie:755679','monitor',True)
        q,_=ds.insert_request(self.store,dict(idempotency='ff-request',kind='movie',title='Fast Forever',quality='1080',
            fallback=False,episode_key='tmdb:movie:755679',state='searching',body={}))
        ds.cancel_request(self.store,q['id'],'Cancelled. Saved files were kept.')
        with self.store.connect() as db:db.execute("UPDATE discover_requests SET state='cancelled' WHERE id=?",(q['id'],))
        old=records.row(self.store,'tmdb:movie:755679')
        self.assertTrue(old['hold']);self.assertEqual(old['monitor'],1)
        service.enrol(self.app,self.store,{**item,'externalId':'27550022'},refresh=False)
        with self.store.connect() as db:rows=[dict(r) for r in db.execute("SELECT * FROM managed_media WHERE kind='movie'").fetchall()]
        self.assertEqual([r['key'] for r in rows],['tmdb:movie:755679'])
        sole=records.row(self.store,'tmdb:movie:755679')
        self.assertTrue(sole['hold']);self.assertEqual(sole['monitor'],1)
        self.assertEqual(service.body(sole)['item']['externalId'],'27550022')
        self.assertEqual(service.request_for(self.store,'tmdb:movie:755679')['id'],q['id'])
        items=service.listing(self.app,self.store,parent='tmdb:movie:755679')['items']
        self.assertEqual(len(items),1);self.assertTrue(items[0]['hold'])
    def test_reconcile_merges_preexisting_duplicate_and_repoints_references(self):
        old_item=dict(id='tmdb:movie:755679',kind='movie',title='Fast Forever',year='2028',releaseDate='2028-03-15')
        new_item={**old_item,'externalId':'27550022'}
        records.remember(self.store,'tmdb:movie:755679','tmdb:movie:755679','movie',dict(item=old_item,date='2028-03-15',dateLabel='Release date',automatic=True))
        with self.store.connect() as db:
            db.execute('UPDATE managed_media SET monitor=1,hold=1 WHERE key=?',('tmdb:movie:755679',))
            db.execute("INSERT INTO discover_watchlist(id,body,updated) VALUES('tmdb:movie:755679','{}',0)")
        q,_=ds.insert_request(self.store,dict(idempotency='ff-req',kind='movie',title='Fast Forever',quality='1080',
            fallback=False,episode_key='tmdb:movie:755679',state='cancelled',body={}))
        # Simulate a duplicate written before alias-aware enrolment existed.
        with self.store.connect() as db:
            db.execute('INSERT INTO managed_media(key,parent,kind,body,monitor,updated) VALUES(?,?,?,?,?,?)',
                ('movie:27550022','tmdb:movie:755679','movie',json.dumps(dict(item=new_item,date='',dateLabel='Digital release',cinemaDate='2028-03-15',automatic=True)),-1,time.time()))
            db.execute('INSERT INTO managed_history(media_key,message,created) VALUES(?,?,0)',('movie:27550022','Enrolled with catalogue identity.'))
            self.assertEqual(db.execute("SELECT count(*) FROM managed_media WHERE kind='movie'").fetchone()[0],2)
        self.assertEqual(records.reconcile(self.store),1)
        sole=records.row(self.store,'tmdb:movie:755679')
        self.assertIsNotNone(sole);self.assertIsNone(records.row(self.store,'movie:27550022'))
        self.assertTrue(sole['hold']);self.assertEqual(sole['monitor'],1)
        merged=service.body(sole)
        self.assertEqual(merged['item']['externalId'],'27550022');self.assertEqual(merged['cinemaDate'],'2028-03-15')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM managed_history WHERE media_key=?',('tmdb:movie:755679',)).fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM discover_watchlist WHERE id='tmdb:movie:755679'").fetchone()[0],1)
        self.assertEqual(service.request_for(self.store,'tmdb:movie:755679')['id'],q['id'])
        self.assertEqual(records.reconcile(self.store),0)
    def test_reconcile_repoints_open_request_and_never_resumes_held_survivor(self):
        records.remember(self.store,'tmdb:movie:9','tmdb:movie:9','movie',dict(item=dict(id='tmdb:movie:9',kind='movie',title='Film',year='2028'),date='2028-01-01',automatic=True))
        with self.store.connect() as db:
            db.execute('UPDATE managed_media SET monitor=1,hold=1 WHERE key=?',('tmdb:movie:9',))
            db.execute('INSERT INTO managed_media(key,parent,kind,body,monitor,updated) VALUES(?,?,?,?,?,?)',
                ('movie:88','tmdb:movie:9','movie',json.dumps(dict(item=dict(id='tmdb:movie:9',kind='movie',title='Film',year='2028',externalId='88'),automatic=True)),-1,1))
        q,_=ds.insert_request(self.store,dict(idempotency='open-on-loser',kind='movie',title='Film',quality='1080',fallback=False,episode_key='movie:88',state='searching',body={}))
        records.reconcile(self.store)
        self.assertEqual(service.request_for(self.store,'tmdb:movie:9')['state'],'cancelled')
        self.assertIsNone(ds.open_request_for(self.store,'tmdb:movie:9'))
        self.assertTrue(records.row(self.store,'tmdb:movie:9')['hold'])
    def test_reconcile_moves_open_request_when_survivor_is_free(self):
        records.remember(self.store,'tmdb:movie:9','tmdb:movie:9','movie',dict(item=dict(id='tmdb:movie:9',kind='movie',title='Film',year='2028'),automatic=True))
        with self.store.connect() as db:
            db.execute('UPDATE managed_media SET monitor=1 WHERE key=?',('tmdb:movie:9',))
            db.execute('INSERT INTO managed_media(key,parent,kind,body,monitor,updated) VALUES(?,?,?,?,?,?)',
                ('movie:88','tmdb:movie:9','movie',json.dumps(dict(item=dict(id='tmdb:movie:9',kind='movie',title='Film',year='2028',externalId='88'),automatic=True)),-1,1))
        q,_=ds.insert_request(self.store,dict(idempotency='open-moves',kind='movie',title='Film',quality='1080',fallback=False,episode_key='movie:88',state='downloading',body={}))
        records.reconcile(self.store)
        self.assertEqual(ds.open_request_for(self.store,'tmdb:movie:9')['id'],q['id'])
        self.assertIsNone(ds.open_request_for(self.store,'movie:88'))
    def test_alias_matching_never_combines_different_movies_sharing_a_title(self):
        for identity,external in (('tmdb:movie:1',''),('tmdb:movie:2','55'),('imdb:movie:66','66')):
            item=dict(id=identity,kind='movie',title='Same Title',year='2028',externalId=external)
            service.enrol(self.app,self.store,item,refresh=False)
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM managed_media WHERE kind='movie'").fetchone()[0],3)
        self.assertEqual(records.reconcile(self.store),0)
    def test_future_episode_not_queued(self):
        r=self.remember(day='2099-01-01');change(self.app,self.store,r['key'],'monitor',True)
        with patch.object(service,'enrol'),patch.object(service,'queue') as queue:
            tick(self.app,self.store);queue.assert_not_called()
    def test_manual_release_selection_can_download_before_metadata_airdate(self):
        r=self.remember(day='2099-01-01')
        release=dict(id='early-file',title='Test.Show.S01E01.1080p',quality='1080p',externalId='77',size=123,grab='https://example.invalid/release.nzb')
        ds.remember_candidates(self.store,[release])
        with patch.object(service,'candidates',return_value=dict(items=[dict(id='early-file',reason='')])),patch.object(acquire,'resolve_release') as resolve,patch.object(acquire,'process') as automatic:
            result=service.queue(self.app,self.store,r,'early-file')
        automatic.assert_not_called();resolve.assert_called_once()
        self.assertEqual(resolve.call_args.args[3][0]['id'],'early-file')
        self.assertEqual(json.loads(result[0]['body'])['managedReleaseId'],'early-file')
    def test_due_episode_uses_existing_engine_once(self):
        r=self.remember();change(self.app,self.store,r['key'],'monitor',True)
        with patch.object(service,'enrol'),patch.object(service,'queue') as queue:
            tick(self.app,self.store);tick(self.app,self.store);queue.assert_called_once()
    def test_comparison_rejection_reasons(self):
        r=self.remember()
        releases=[dict(id='a',title='Test.S01E01.1080p.sample',quality='1080p',externalId='77'),dict(id='b',title='Test.S01E02.1080p',quality='1080p',externalId='77'),dict(id='c',title='Test.S01E01.720p',quality='720p',externalId='77')]
        with patch.object(service.sources,'search_episode',return_value=releases):result=service.candidates(self.app,self.store,r)
        self.assertEqual([c['reason'] for c in result['items']],['Sample content excluded','Different episode or unverified pack coverage','Outside the selected quality policy'])
    def test_no_suitable_release_waits(self):
        row,_=ds.insert_request(self.store,dict(idempotency='no-match',kind='episode',title='Test',quality='1080',fallback=False,episode_key='test',state='searching',body=dict(externalId='77',episode=dict(season=1,number=1))))
        acquire.resolve_release(self.app,self.store,row,[])
        self.assertEqual(service.request_for(self.store,'test')['state'],'waiting')
    def test_active_request_cannot_be_removed(self):
        r=self.remember()
        ds.insert_request(self.store,dict(idempotency='active',kind='episode',title='Test',quality='1080',fallback=False,episode_key=r['key'],state='downloading',body={}))
        self.assertEqual(self.post(dict(action='remove',key=r['key'],confirm=True)).status_code,400)
    def test_youtube_remove_blocks_automatic_but_not_manual_download(self):
        from backend import downloads
        item=dict(id='youtube:video:abcdefghijk',kind='video',videoId='abcdefghijk',title='Test video')
        service.enrol(self.app,self.store,item)
        key='video:abcdefghijk'
        change(self.app,self.store,key,'remove',confirm=True)
        video=dict(id='abcdefghijk',title='Test video',state='published')
        downloads.enqueue(self.store,video,'1080',automatic=True)
        with self.store.connect() as db:self.assertIsNone(db.execute('SELECT id FROM downloads WHERE id=?',(video['id'],)).fetchone())
        downloads.enqueue(self.store,video,'1080',automatic=False)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT state FROM downloads WHERE id=?',(video['id'],)).fetchone()['state'],'queued')
    def test_selected_release_stays_pinned_on_retry(self):
        ds.remember_candidates(self.store,[dict(id='chosen',kind='movie',externalId='123',quality='1080p',size=123,title='Test Film',grab='https://example.com/nzb')])
        request=dict(kind='movie',title='Test',body=json.dumps(dict(externalId='123',managedReleaseId='chosen')))
        result=acquire.search_for(self.app,self.store,request)
        self.assertEqual([r['id'] for r in result],['chosen'])
        with self.store.connect() as db:db.execute("UPDATE discover_candidates SET created=0 WHERE id='chosen'")
        self.assertEqual(acquire.search_for(self.app,self.store,request),[])
    def test_manual_search_can_replace_waiting_request(self):
        r=self.remember()
        old,_=ds.insert_request(self.store,dict(idempotency='waiting',kind='episode',title='Test',quality='1080',fallback=False,episode_key=r['key'],state='waiting',body={}))
        with patch.object(acquire,'process') as process:
            result=service.queue(self.app,self.store,r)
        process.assert_called_once();self.assertNotEqual(result[0]['id'],old['id'])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT state FROM discover_requests WHERE id=?',(old['id'],)).fetchone()['state'],'cancelled')
    def test_packs_are_rejected_in_single_episode_comparison(self):
        r=self.remember()
        release=dict(id='pack',title='Test.S01E01E02.1080p',quality='1080p',externalId='77')
        with patch.object(service.sources,'search_episode',return_value=[release]):
            self.assertTrue(service.candidates(self.app,self.store,r)['items'][0]['reason'])
    def test_custom_monitor_is_not_rescheduled_by_parent_rule(self):
        r=self.remember();self.assertFalse(records.overrides_monitor(self.store,r['key']))
        change(self.app,self.store,r['key'],'monitor',True)
        self.assertTrue(records.overrides_monitor(self.store,r['key']))
    def test_processing_stage_is_visible_without_completing_download(self):
        r=self.remember()
        folder=Path(self.store.directory)/'nzb-engine'/'jobs';folder.mkdir(parents=True,exist_ok=True)
        (folder/'stage.json').write_text(json.dumps(dict(phase='download',progress_stage='extracting',published={},done_parts=10,total_parts=10)))
        ds.insert_request(self.store,dict(idempotency='stage',kind='episode',title='Test',quality='1080',fallback=False,episode_key=r['key'],state='downloading',engine='nzb',engine_id='stage',body={}))
        self.assertEqual(service.compose(self.app,self.store,r)['status'],'Extracting')
        self.assertFalse(service.compose(self.app,self.store,r)['file'])

    def test_transfer_estimate_comes_from_saved_nzb_not_final_file(self):
        r=self.remember()
        folder=Path(self.store.directory)/'nzb-engine'/'jobs';folder.mkdir(parents=True,exist_ok=True)
        path=folder/'size.json'
        path.write_text(json.dumps(dict(phase='download',done_parts=1,total_parts=4,files=[dict(segments=[dict(size=200),dict(size='300'),dict(size=-1),dict(size='invalid')])],archive_password='private',published={})))
        ds.insert_request(self.store,dict(idempotency='size',kind='episode',title='Test',quality='1080',fallback=False,episode_key=r['key'],state='downloading',engine='nzb',engine_id='size',progress=.25,body={}))
        result=service.compose(self.app,self.store,r)
        self.assertEqual(result['estimatedBytes'],500);self.assertEqual(result['progress'],25);self.assertEqual(result['bytes'],0)
        self.assertFalse(result['file']);self.assertNotIn('private',json.dumps(result));self.assertNotIn('archive_password',json.dumps(result))
        path.write_text('{partial')
        self.assertEqual(service.compose(self.app,self.store,r)['estimatedBytes'],0)
        path.unlink();self.assertEqual(service.compose(self.app,self.store,r)['estimatedBytes'],0)

    def test_popup_prefers_advancing_live_nzb_status_to_zero_checkpoint(self):
        r=self.remember()
        folder=Path(self.store.directory)/'nzb-engine'/'jobs';folder.mkdir(parents=True,exist_ok=True)
        (folder/'live.json').write_text(json.dumps(dict(phase='download',progress_stage='downloading',done_parts=0,total_parts=100,published={})))
        ds.insert_request(self.store,dict(idempotency='live',kind='episode',title='Test',quality='1080',fallback=False,episode_key=r['key'],state='downloading',engine='nzb',engine_id='live',progress=0,body={}))
        live=Mock(stop=threading.Event())
        live.job.return_value=dict(id='live',state='downloading',progress=.35,progress_stage='downloading',total_bytes=1700000000,published=[],error='',servers=['private-server'])
        with patch.dict(self.app.extensions,nzb_engine=live):
            first=service.compose(self.app,self.store,r)
            live.job.return_value={**live.job.return_value,'progress':.63}
            second=service.compose(self.app,self.store,r)
        self.assertEqual((first['progress'],second['progress']),(35,63))
        self.assertEqual(first['estimatedBytes'],1700000000)
        self.assertEqual(first['status'],'Downloading');self.assertFalse(first['file'])
        self.assertNotIn('private-server',json.dumps(first))
        with self.store.connect() as db:
            self.assertEqual(tuple(db.execute("SELECT state,progress FROM discover_requests WHERE engine_id='live'").fetchone()),('downloading',0))

    def test_saved_checkpoint_cannot_regress_known_transfer_progress(self):
        r=self.remember()
        folder=Path(self.store.directory)/'nzb-engine'/'jobs';folder.mkdir(parents=True,exist_ok=True)
        (folder/'stale.json').write_text(json.dumps(dict(phase='download',done_parts=0,total_parts=100,published={})))
        ds.insert_request(self.store,dict(idempotency='stale',kind='episode',title='Test',quality='1080',fallback=False,episode_key=r['key'],state='downloading',engine='nzb',engine_id='stale',progress=47,body={}))
        self.assertEqual(service.compose(self.app,self.store,r)['progress'],47)

    def test_live_pause_and_processing_status_keep_cancellation_authoritative(self):
        r=self.remember()
        ds.insert_request(self.store,dict(idempotency='phase-live',kind='episode',title='Test',quality='1080',fallback=False,episode_key=r['key'],state='downloading',engine='nzb',engine_id='phase-live',body={}))
        live=Mock(stop=threading.Event())
        live.job.return_value=dict(state='paused',progress=.4,progress_stage='downloading',total_bytes=200,published=[],error='')
        with patch.dict(self.app.extensions,nzb_engine=live):
            self.assertEqual(service.compose(self.app,self.store,r)['status'],'Paused')
            live.job.return_value={**live.job.return_value,'state':'downloading','progress':1,'progress_stage':'extracting'}
            result=service.compose(self.app,self.store,r)
            self.assertEqual(result['status'],'Extracting');self.assertEqual(result['progress'],100);self.assertFalse(result['file'])
            with self.store.connect() as db:db.execute("UPDATE discover_requests SET state='cancelling' WHERE engine_id='phase-live'")
            self.assertEqual(service.compose(self.app,self.store,r)['status'],'Cancelling')

    def test_live_job_unavailable_uses_saved_state_without_starting_an_engine(self):
        from backend.downloaders.nzb.engine import EngineError
        r=self.remember()
        folder=Path(self.store.directory)/'nzb-engine'/'jobs';folder.mkdir(parents=True,exist_ok=True)
        (folder/'fallback.json').write_text(json.dumps(dict(phase='download',done_parts=3,total_parts=4,published={})))
        ds.insert_request(self.store,dict(idempotency='fallback',kind='episode',title='Test',quality='1080',fallback=False,episode_key=r['key'],state='downloading',engine='nzb',engine_id='fallback',body={}))
        live=Mock(stop=threading.Event());live.job.side_effect=EngineError('Unknown NZB',404)
        with patch('backend.nzb._engine') as start,patch.dict(self.app.extensions,nzb_engine=live):
            self.assertEqual(service.compose(self.app,self.store,r)['progress'],75)
            live.stop.set();live.job.reset_mock()
            self.assertEqual(service.compose(self.app,self.store,r)['progress'],75)
            live.job.assert_not_called();start.assert_not_called()

    def test_finished_job_with_missing_file_is_not_reported_as_downloaded_or_queued(self):
        r=self.remember()
        folder=Path(self.store.directory)/'nzb-engine'/'jobs';folder.mkdir(parents=True,exist_ok=True)
        (folder/'missing.json').write_text(json.dumps(dict(phase='stopped',progress_stage='complete',done_parts=4,total_parts=4,published={'0':'Movies/Missing.mkv'})))
        ds.insert_request(self.store,dict(idempotency='missing',kind='episode',title='Test',quality='1080',fallback=False,episode_key=r['key'],state='queued',engine='nzb',engine_id='missing',body={}))
        result=service.compose(self.app,self.store,r)
        self.assertEqual(result['status'],'Saved file unavailable');self.assertEqual(result['progress'],100);self.assertFalse(result['file'])
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT state FROM discover_requests WHERE idempotency='missing'").fetchone()[0],'queued')

    def test_completed_size_is_actual_owned_file_size(self):
        r=self.remember();p=self.file();result=service.compose(self.app,self.store,r)
        self.assertTrue(result['file']);self.assertEqual(result['bytes'],p.stat().st_size);self.assertEqual(result['filename'],p.name)
        self.assertEqual(result['estimatedBytes'],0)

    def test_completed_job_is_saved_before_worker_ownership_sync(self):
        r=self.remember();self.store.media_root=str(self.root)
        p=self.root/'Movies'/'Saved.mkv';p.parent.mkdir();p.write_bytes(b'saved movie')
        folder=Path(self.store.directory)/'nzb-engine'/'jobs';folder.mkdir(parents=True,exist_ok=True)
        path=folder/'finished.json'
        path.write_text(json.dumps(dict(phase='stopped',progress_stage='complete',done_parts=4,total_parts=4,published={'0':'Movies/Saved.mkv'})))
        ds.insert_request(self.store,dict(idempotency='finished',kind='episode',title='Test',quality='1080',fallback=False,episode_key=r['key'],state='queued',engine='nzb',engine_id='finished',body={}))
        result=service.compose(self.app,self.store,r)
        self.assertTrue(result['file']);self.assertEqual(result['bytes'],p.stat().st_size);self.assertEqual(result['filename'],p.name)
        self.assertIsNone(ds.owned(self.store,r['key']),'display reconciliation never mutates ownership')
        payload=json.loads(path.read_text());payload['progress_stage']='publishing';path.write_text(json.dumps(payload))
        self.assertFalse(service.compose(self.app,self.store,r)['file'],'publication must finish before playback')
        payload['progress_stage']='complete';path.write_text(json.dumps(payload))
        p.unlink();outside=Path(self.tmp.name)/'outside.mkv';outside.write_bytes(b'outside');p.symlink_to(outside)
        self.assertFalse(service.compose(self.app,self.store,r)['file'],'completed jobs cannot bypass symlink boundaries')
        p.unlink();payload['published']={'0':str(outside)};path.write_text(json.dumps(payload))
        self.assertFalse(service.compose(self.app,self.store,r)['file'],'absolute paths outside the library remain rejected')
    def test_global_quality_inherited_until_customised(self):
        r=self.remember()
        ds.save_preference(self.store,'quality','720')
        self.assertEqual(service.compose(self.app,self.store,r)['quality'],'720')
        change(self.app,self.store,r['key'],'monitor',True)
        ds.save_preference(self.store,'quality','2160')
        self.assertEqual(service.compose(self.app,self.store,records.row(self.store,r['key']))['quality'],'720')


    def test_series_defaults_inherit_and_off_blocks_new_episodes(self):
        r=self.remember()
        for field,value in [('quality','2160'),('fallback',True),('monitoring','all')]:
            response=self.post(dict(action='series-settings',parent=r['parent'],field=field,value=value))
            self.assertEqual(response.status_code,200,response.json)
        state=service.compose(self.app,self.store,records.row(self.store,r['key']))
        self.assertEqual(state['quality'],'2160');self.assertTrue(state['monitored'])
        change(self.app,self.store,r['key'],'quality','720')
        self.assertEqual(service.compose(self.app,self.store,records.row(self.store,r['key']))['quality'],'720')
        change(self.app,self.store,r['key'],'inherit')
        self.assertEqual(service.compose(self.app,self.store,records.row(self.store,r['key']))['quality'],'2160')
        self.post(dict(action='series-settings',parent=r['parent'],field='monitoring',value='off'))
        self.assertFalse(service.effective_monitor(self.store,records.row(self.store,r['key'])))
        self.assertTrue(records.policy_blocks(self.store,'show:77:9:1'))

    def test_series_inherited_monitor_claims_due_episode(self):
        r=self.remember()
        self.post(dict(action='series-settings',parent=r['parent'],field='monitoring',value='all'))
        with patch.object(service,'enrol'),patch.object(service,'queue') as queued:
            tick(self.app,self.store)
        queued.assert_called_once()

    def test_movie_popup_offers_all_indexers_and_queues_only_chosen_release(self):
        from backend.discovery_engine import prepare
        item=dict(id='movie:00305224',kind='movie',externalId='00305224',title='Anger Management',year='2003')
        prepare(self.app,self.store,item)
        releases=[dict(id=key,kind='movie',externalId='00305224',quality='1080p',size=4_000_000_000,
                       title='Anger.Management.2003.1080p.BluRay',grab='https://example.com/nzb',provider=provider)
                  for key,provider in [('indexer-a-release','Indexer A'),('indexer-b-release','Indexer B')]]
        ds.remember_candidates(self.store,[dict(r,externalId='0305224') for r in releases])
        with patch.object(service.sources,'search_movie',return_value=releases),patch.object(acquire,'resolve_release') as resolve:
            response=self.post(dict(action='releases',key='movie:00305224'))
            self.assertEqual(response.status_code,200,response.json)
            self.assertEqual([r['source'] for r in response.json['items']],['Indexer A','Indexer B'])
            self.assertNotIn('https://',str(response.json))
            response=self.post(dict(action='search',key='movie:00305224',value='indexer-b-release'))
            self.assertEqual(response.status_code,200,response.json)
        resolve.assert_called_once()
        self.assertEqual(resolve.call_args.args[-1][0]['id'],'indexer-b-release')
        self.assertEqual(len(acquire.choose(resolve.call_args.args[-1],'1080',False,'00305224')),1)
        self.assertEqual(acquire.choose(resolve.call_args.args[-1],'1080',False,'0305225'),[])
        self.assertEqual(json.loads(resolve.call_args.args[-2]['body'])['managedReleaseId'],'indexer-b-release')

    def movie(self):
        self.store.set('storage_base',str(self.root))
        item=dict(id='movie:123',kind='movie',externalId='123',title='Old Film',year='2026')
        records.remember(self.store,'movie:123',item['id'],'movie',dict(item=item,automatic=True,date='2026-01-01'))
        r=records.row(self.store,'movie:123')
        p=self.root/'Movies'/'Old Film (2026)'/'Old Film (2026).mkv'
        p.parent.mkdir(parents=True);p.write_bytes(b'original movie')
        q,_=ds.insert_request(self.store,dict(idempotency='previous-movie',kind='movie',title='Old Film',quality='1080',
            fallback=False,episode_key=r['key'],state='complete',body=dict(item=item,externalId='123',year='2026')))
        ds.mark_owned(self.store,r['key'],q['id'],str(p))
        return r,p

    def replacement(self):
        r,p=self.movie()
        candidate=dict(id='better',kind='movie',externalId='123',quality='1080p',size=1234,
                       title='Old.Film.2026.1080p',grab='https://example.invalid/release.nzb')
        ds.remember_candidates(self.store,[candidate])
        with patch.object(service,'candidates',return_value=dict(items=[dict(id='better',quality='1080p',reason='')])),\
             patch.object(files,'quality',return_value='720'),patch.object(acquire,'resolve_release'):
            q=service.queue(self.app,self.store,r,'better',replace=True)[0]
        q=ds.update_request(self.store,q['id'],state='queued',engine='nzb',engine_id='replacement-job')
        from backend.downloaders.torrent.rename import library_path
        relative=library_path('','video.mkv','video','movie','emby',title=acquire.library_name(q,''))
        source=self.root/relative;source.parent.mkdir(parents=True);source.write_bytes(b'new verified movie')
        source.with_suffix('.nfo').write_text('<movie><title>Old Film</title></movie>')
        (source.parent/'poster.jpg').write_bytes(b'poster');(source.parent/'fanart.jpg').write_bytes(b'fanart')
        return r,p,q,source

    def test_rename_movie_moves_folder_leaf_companions_and_ownership(self):
        r,p=self.movie()
        for name in ('Old Film (2026).nfo','Old Film (2026).en.srt','Old Film (2026)-poster.jpg','poster.jpg','fanart.jpg'):
            (p.parent/name).write_bytes(name.encode())
        with self.store.connect() as db:db.execute('UPDATE managed_media SET hold=1 WHERE key=?',(r['key'],))
        response=self.post(dict(action='rename',key=r['key'],value='Correct Film'))
        self.assertEqual(response.status_code,200,response.json)
        destination=self.root/'Movies'/'Correct Film (2026)'/'Correct Film (2026).mkv'
        self.assertEqual(destination.read_bytes(),b'original movie');self.assertFalse(p.parent.exists())
        for name in ('Correct Film (2026).nfo','Correct Film (2026).en.srt','Correct Film (2026)-poster.jpg','poster.jpg','fanart.jpg'):
            self.assertTrue((destination.parent/name).is_file(),name)
        self.assertEqual(Path(ds.owned(self.store,r['key'])['path']),destination)
        self.assertEqual(service.library_file(self.app,self.store,r),destination)
        self.assertTrue(response.json['item']['hold']);self.assertEqual(response.json['item']['title'],'Correct Film')
        self.assertEqual(service.body(records.row(self.store,r['key']))['item']['externalId'],'123')
        history=self.post(dict(action='history',key=r['key'])).json['items']
        self.assertTrue(any(str(p)+' -> '+str(destination) in h['message'] for h in history))

    def test_rename_returns_scan_mapping_and_serves_new_file_and_art_immediately(self):
        import hashlib
        r,p=self.movie()
        (p.parent/'poster.jpg').write_bytes(b'poster')
        (p.parent/'fanart.jpg').write_bytes(b'fanart')
        old=self.scan_identity(p)
        with self.store.connect() as db:db.execute('UPDATE managed_media SET hold=1 WHERE key=?',(r['key'],))
        with patch.object(files,'invalidate'):
            result=self.post(dict(action='rename',key=r['key'],value='Correct Film'))
        self.assertEqual(result.status_code,200,result.json)
        destination=self.root/'Movies'/'Correct Film (2026)'/'Correct Film (2026).mkv'
        new=hashlib.sha256(str(destination.relative_to(self.root)).encode()).hexdigest()[:32]
        self.assertEqual(result.json['item']['localChanges'],[{'from':old,'to':new}])
        self.assertTrue(result.json['item']['hold'])
        self.assertEqual(vm.local.media_file(self.root,new,'file'),destination)
        self.assertEqual(vm.local.media_file(self.root,new,'art').read_bytes(),b'poster')
        self.assertEqual(vm.local.media_file(self.root,new,'backdrop').read_bytes(),b'fanart')
        self.assertEqual(self.get('/api/view-media/local/'+old+'/file').status_code,404)
        self.assertEqual(self.get('/api/view-media/local/'+new+'/file').status_code,404,'renamed files remain library files but are never streamed')
        self.assertEqual(vm.local.media_file(self.root,new,'file').read_bytes(),b'original movie')
        listing=self.get('/api/media-management?key='+r['key']).json['items'][0]
        self.assertNotIn('localChanges',listing,'mapping is an operation result, not persistent title state')

    def test_rename_without_prior_scan_serves_shared_video_extension(self):
        r,p=self.movie();other=p.with_suffix('.m2ts');p.rename(other)
        ds.mark_owned(self.store,r['key'],'previous',str(other))
        self.assertNotIn(str(self.root),vm.local._CACHE)
        with patch.object(files,'invalidate'):
            result=change(self.app,self.store,r['key'],'rename','Correct Film')
        self.assertEqual(len(result['localChanges']),1)
        identity=result['localChanges'][0]['to']
        self.assertEqual(vm.local.media_file(self.root,identity,'file').suffix,'.m2ts')
        state={};vm.local._scan(self.root,state)
        self.assertIn(identity,state['files'],'all existing shared VIDEO formats remain in subsequent inventories')

    def test_rename_maps_every_movie_part_and_episode_leaf(self):
        r,p=self.movie();first=p.with_name(p.stem+'-cd1.mkv');p.rename(first)
        second=first.with_name('Old Film (2026)-cd2.mkv');second.write_bytes(b'part two')
        ds.mark_owned(self.store,r['key'],'previous',str(first))
        old_first=self.scan_identity(first);old_second=self.scan_identity(second)
        with patch.object(files,'invalidate'):
            result=change(self.app,self.store,r['key'],'rename','Correct Film')
        mapping={item['from']:item['to'] for item in result['localChanges']}
        self.assertEqual(set(mapping),{old_first,old_second})
        self.assertEqual(vm.local.media_file(self.root,mapping[old_first],'file').read_bytes(),b'original movie')
        self.assertEqual(vm.local.media_file(self.root,mapping[old_second],'file').read_bytes(),b'part two')
        episode=self.remember();path=self.file();parent=path.parent;path.with_suffix('.nfo').write_text('<episodedetails/>')
        old=self.scan_identity(path)
        with patch.object(files,'invalidate'):
            result=change(self.app,self.store,episode['key'],'rename','Renamed episode')
        self.assertEqual(len(result['localChanges']),1);self.assertEqual(result['localChanges'][0]['from'],old)
        self.assertIn('Renamed episode',vm.local.media_file(self.root,result['localChanges'][0]['to'],'file').name)
        self.assertEqual(vm.local.media_file(self.root,result['localChanges'][0]['to'],'file').parent,parent)

    def test_rename_split_movie_keeps_cd_parts_and_companions_together(self):
        r,p=self.movie();first=p.with_name(p.stem+'-cd1.mkv');p.rename(first)
        second=first.with_name('Old Film (2026)-cd2.mkv');second.write_bytes(b'part two')
        second.with_suffix('.nfo').write_text('<movie/>')
        ds.mark_owned(self.store,r['key'],'previous',str(first))
        change(self.app,self.store,r['key'],'rename','Correct Film')
        folder=self.root/'Movies'/'Correct Film (2026)'
        self.assertTrue((folder/'Correct Film (2026)-cd1.mkv').is_file())
        self.assertEqual((folder/'Correct Film (2026)-cd2.mkv').read_bytes(),b'part two')
        self.assertTrue((folder/'Correct Film (2026)-cd2.nfo').is_file())

    def test_rename_episode_changes_only_leaf_and_nfo(self):
        r=self.remember();p=self.file();p.with_suffix('.nfo').write_text('<episodedetails/>')
        thumb=p.with_name(p.stem+'-thumb.jpg');thumb.write_bytes(b'keep artwork')
        other=p.parent/'Other.mp4';other.write_bytes(b'other episode')
        old_folder=p.parent
        result=change(self.app,self.store,r['key'],'rename','Revised Pilot')
        destination=service.library_file(self.app,self.store,records.row(self.store,r['key']))
        self.assertEqual(destination.parent,old_folder);self.assertFalse(p.exists())
        self.assertIn('s01e01',destination.name);self.assertIn('Revised Pilot',destination.name)
        self.assertTrue(destination.with_suffix('.nfo').is_file());self.assertFalse(p.with_suffix('.nfo').exists())
        self.assertTrue(other.exists());self.assertTrue(thumb.exists());self.assertEqual(result['title'],'Revised Pilot')

    def test_rename_refused_while_active_and_destination_exists(self):
        r,p=self.movie()
        target=p.parent.parent/'Correct Film (2026)';target.mkdir()
        self.assertEqual(self.post(dict(action='rename',key=r['key'],value='Correct Film')).status_code,400)
        self.assertTrue(p.exists());self.assertEqual(list(target.iterdir()),[])
        target.rmdir()
        with self.store.connect() as db:db.execute("UPDATE discover_requests SET state='downloading' WHERE episode_key=?",(r['key'],))
        self.assertEqual(self.post(dict(action='rename',key=r['key'],value='Correct Film')).status_code,400)
        self.assertTrue(p.exists());self.assertFalse(target.exists())

    def test_rename_rejects_episode_nfo_collision_and_path_input(self):
        r=self.remember();p=self.file()
        for title in ('../escape','bad/name','bad\\name',''):
            self.assertEqual(self.post(dict(action='rename',key=r['key'],value=title)).status_code,400)
        destination=p.with_name('Test Show - s01e01 - Revised Pilot.mp4')
        destination.with_suffix('.nfo').write_bytes(b'existing companion')
        p.with_suffix('.nfo').write_bytes(b'original companion')
        self.assertEqual(self.post(dict(action='rename',key=r['key'],value='Revised Pilot')).status_code,400)
        self.assertTrue(p.is_file());self.assertFalse(destination.exists())

    def test_all_file_mutations_reject_intermediate_symlink_escape(self):
        r,p=self.movie();folder=p.parent
        outside=Path(self.tmp.name)/'outside';folder.rename(outside);folder.symlink_to(outside,target_is_directory=True)
        for action,value in (('rename','Correct Film'),('delete',{'folder':True}),('repair-metadata',None),('replace','better')):
            response=self.post(dict(action=action,key=r['key'],value=value,confirm=True))
            self.assertEqual(response.status_code,400,(action,response.json))
        self.assertEqual((outside/p.name).read_bytes(),b'original movie')

    def test_inode_substitution_refuses_rename_and_delete(self):
        r,p=self.movie();real_verify=files.verify
        for action,value in (('rename','Correct Film'),('delete',None)):
            injected=False
            def substitute(fd,name,before):
                nonlocal injected
                if name==p.name and not injected:
                    injected=True
                    temporary=p.with_name('injected.mkv');temporary.write_bytes(b'changed inode')
                    temporary.replace(p)
                return real_verify(fd,name,before)
            with patch.object(files,'quality',return_value='Not verified'),patch.object(files,'verify',side_effect=substitute):
                response=self.post(dict(action=action,key=r['key'],value=value,confirm=True))
            self.assertEqual(response.status_code,400,response.json)
            self.assertEqual(p.read_bytes(),b'changed inode')
            self.assertFalse((p.parent.parent/'Correct Film (2026)').exists())

    def test_repair_metadata_uses_stored_identity_and_reports_artwork_partial_success(self):
        r,p=self.movie();p.with_suffix('.nfo').write_text('<movie><title>Obsolete</title></movie>')
        provider=Mock()
        provider.get.side_effect=[dict(movie_results=[dict(id=42,title='Old Film')]),
            dict(id=42,title='Canonical Film',poster_path='/poster.jpg',backdrop_path='/backdrop.jpg')]
        with patch('backend.downloaders.torrent.metadata.TMDB',return_value=provider),\
             patch('backend.m3u_sidecars.artwork',return_value=False) as art:
            response=self.post(dict(action='repair-metadata',key=r['key']))
        self.assertEqual(response.status_code,200,response.json)
        provider.get.assert_any_call('find/tt0000123',external_source='imdb_id')
        self.assertIn('Canonical Film',p.with_suffix('.nfo').read_text());self.assertTrue(art.called)
        self.assertEqual(response.json['message'],files.ARTWORK_UNAVAILABLE)
        self.assertFalse(response.json['item']['metadataReady'])
        self.assertEqual(response.json['item']['status'],'Saved · metadata incomplete')
        self.assertFalse(any(p.parent.glob('.reeldock-management-*')))

    def test_repair_metadata_preserves_existing_artwork_and_completes_status(self):
        r,p=self.movie()
        for name in ('poster.jpg','fanart.jpg'):(p.parent/name).write_bytes(b'custom artwork')
        provider=Mock();provider.get.side_effect=[dict(movie_results=[dict(id=42)]),
            dict(id=42,title='Canonical Film',poster_path='/poster.jpg',backdrop_path='/backdrop.jpg')]
        with patch('backend.downloaders.torrent.metadata.TMDB',return_value=provider),\
             patch('backend.m3u_player.cached_image_path') as fetch_art:
            response=self.post(dict(action='repair-metadata',key=r['key']))
        self.assertEqual(response.status_code,200,response.json);fetch_art.assert_not_called()
        self.assertTrue(response.json['item']['metadataReady']);self.assertEqual(response.json['item']['status'],'In library')
        self.assertEqual((p.parent/'poster.jpg').read_bytes(),b'custom artwork')

    def test_repair_never_infers_identity_from_filename(self):
        r,p=self.movie();b=service.body(r);b['item']=dict(kind='movie',id='local:unknown',title='Unknown')
        with self.store.connect() as db:
            db.execute('UPDATE managed_media SET body=? WHERE key=?',(json.dumps(b),r['key']))
            db.execute('UPDATE discover_requests SET body=? WHERE episode_key=?',('{}',r['key']))
        with patch('backend.downloaders.torrent.metadata.TMDB') as provider:
            response=self.post(dict(action='repair-metadata',key=r['key']))
        self.assertEqual(response.status_code,400,response.json);provider.assert_not_called()
        self.assertFalse(p.with_suffix('.nfo').exists())

    def test_repair_metadata_fetches_missing_artwork_beside_existing_file(self):
        r,p=self.movie();provider=Mock()
        provider.get.side_effect=[dict(movie_results=[dict(id=42)]),
            dict(id=42,title='Canonical Film',poster_path='/poster.jpg',backdrop_path='/backdrop.jpg')]
        def artwork(store,base,path,remote):
            if not remote:return False
            path.write_bytes(b'fetched artwork');return True
        with patch('backend.downloaders.torrent.metadata.TMDB',return_value=provider),patch('backend.m3u_sidecars.artwork',side_effect=artwork):
            response=self.post(dict(action='repair-metadata',key=r['key']))
        self.assertEqual(response.status_code,200,response.json);self.assertTrue(response.json['item']['metadataReady'])
        self.assertEqual(response.json['message'],'')
        self.assertEqual((p.parent/'poster.jpg').read_bytes(),b'fetched artwork')
        self.assertEqual((p.parent/'fanart.jpg').read_bytes(),b'fetched artwork')

    def test_delete_folder_removes_only_movie_folder_without_remaining_video(self):
        r,p=self.movie()
        for name in ('poster.jpg','fanart.jpg','Old Film (2026).nfo'):(p.parent/name).write_bytes(b'sidecar')
        response=self.post(dict(action='delete',key=r['key'],value={'folder':True},confirm=True))
        self.assertEqual(response.status_code,200,response.json);self.assertFalse(p.parent.exists())
        self.assertTrue(response.json['item']['hold']);self.assertEqual(ds.owned(self.store,r['key'])['state'],'deleted')
        self.assertTrue(p.parent.parent.is_dir())

    def test_delete_folder_keeps_remaining_movie_video_and_all_episode_folders(self):
        r,p=self.movie();other=p.parent/'Other.mkv';other.write_bytes(b'other');poster=p.parent/'poster.jpg';poster.write_bytes(b'art')
        change(self.app,self.store,r['key'],'delete',{'folder':True},True)
        self.assertTrue(other.is_file());self.assertTrue(poster.is_file())
        r=self.remember();episode=self.file();side=episode.with_suffix('.nfo');side.write_bytes(b'keep')
        change(self.app,self.store,r['key'],'delete',True,True)
        self.assertTrue(episode.parent.is_dir());self.assertTrue(side.exists());self.assertFalse(episode.exists())

    def test_delete_folder_never_follows_remaining_symlinks(self):
        r,p=self.movie();outside=Path(self.tmp.name)/'outside.jpg';outside.write_bytes(b'keep')
        link=p.parent/'poster.jpg';link.symlink_to(outside)
        change(self.app,self.store,r['key'],'delete',True,True)
        self.assertFalse(p.exists());self.assertTrue(link.is_symlink());self.assertEqual(outside.read_bytes(),b'keep')

    def test_delete_folder_failure_is_best_effort(self):
        r,p=self.movie()
        with patch.object(files.os,'rmdir',side_effect=PermissionError('unavailable')):
            response=self.post(dict(action='delete',key=r['key'],value=True,confirm=True))
        self.assertEqual(response.status_code,200,response.json);self.assertFalse(p.exists());self.assertTrue(p.parent.exists())

    def test_video_delete_never_removes_folder_and_internal_symlinks_are_rejected(self):
        self.store.set('storage_base',str(self.root))
        from backend import storage_layout;storage_layout.apply(self.app,self.store)
        video='abcdefghijk';key='video:'+video
        records.remember(self.store,key,'youtube:video:'+video,'video',dict(item=dict(id='youtube:video:'+video,kind='video',videoId=video,title='Video')))
        youtube=self.root/'YouTube';folder=youtube/'Channel';folder.mkdir(parents=True);p=folder/'Video.mp4';p.write_bytes(b'video')
        with self.store.connect() as db:
            db.execute('INSERT INTO media_files(id,channel,path,day,episode,profile,bytes) VALUES(?,?,?,?,?,?,?)',
                       (video,'channel','Channel/Video.mp4','2026-01-01',1,'emby',5))
        side=folder/'poster.jpg';side.write_bytes(b'keep')
        alias=youtube/'Alias';alias.symlink_to(folder,target_is_directory=True)
        with self.store.connect() as db:db.execute('UPDATE media_files SET path=? WHERE id=?',('Alias/Video.mp4',video))
        self.assertIsNone(service.library_file(self.app,self.store,records.row(self.store,key)))
        self.assertEqual(self.post(dict(action='delete',key=key,value=True,confirm=True)).status_code,400)
        with self.store.connect() as db:db.execute('UPDATE media_files SET path=? WHERE id=?',('Channel/Video.mp4',video))
        self.assertEqual(self.post(dict(action='rename',key=key,value='Rename video')).status_code,400)
        response=self.post(dict(action='delete',key=key,value=True,confirm=True))
        self.assertEqual(response.status_code,200,response.json);self.assertFalse(p.exists());self.assertTrue(side.exists())

    def test_file_quality_probe_is_cached_by_path_size_and_mtime_in_body(self):
        r,p=self.movie()
        probe=Mock(stdout=json.dumps(dict(streams=[dict(codec_type='video',width=1920,height=800)])))
        with patch.object(files.subprocess,'run',return_value=probe) as ffprobe:
            self.assertEqual(service.compose(self.app,self.store,r)['fileQuality'],'1080')
            self.assertEqual(service.compose(self.app,self.store,r)['fileQuality'],'1080')
            self.assertEqual(service.listing(self.app,self.store)['items'][0]['fileQuality'],'1080')
            self.assertEqual(ffprobe.call_count,1)
            p.write_bytes(b'a different movie size')
            self.assertEqual(service.compose(self.app,self.store,r)['fileQuality'],'1080');self.assertEqual(ffprobe.call_count,2)
            state=p.stat();files.os.utime(p,ns=(state.st_atime_ns,state.st_mtime_ns+1000000))
            service.compose(self.app,self.store,r);self.assertEqual(ffprobe.call_count,3)
        cached=service.body(records.row(self.store,r['key']))['fileQuality']
        self.assertEqual(cached['signature'][0],str(p));self.assertEqual(cached['quality'],'1080')
        records.remember(self.store,r['key'],r['parent'],r['kind'],service.body(r))
        self.assertEqual(service.body(records.row(self.store,r['key']))['fileQuality'],cached)

    def test_file_quality_probe_failure_is_cached_and_width_mapping_matches_verifier(self):
        r,p=self.movie()
        with patch.object(files.subprocess,'run',side_effect=OSError('ffprobe missing')) as probe:
            self.assertEqual(service.compose(self.app,self.store,r)['fileQuality'],'Not verified')
            self.assertEqual(service.compose(self.app,self.store,r)['fileQuality'],'Not verified');probe.assert_called_once()
        for width,expected in ((3840,'2160'),(1920,'1080'),(1280,'720'),(640,'480')):
            records.patch_body(self.store,r['key'],fileQuality=None)
            with patch.object(files.subprocess,'run',return_value=Mock(stdout=json.dumps(dict(streams=[dict(codec_type='video',width=width,height=1)])))):
                self.assertEqual(service.compose(self.app,self.store,r)['fileQuality'],expected)

    def test_replace_queues_past_saved_guard_and_keeps_ownership_and_identity(self):
        r,p,q,source=self.replacement()
        self.assertEqual(json.loads(q['body'])['replaces'],r['key'])
        self.assertEqual(json.loads(q['body'])['managedReleaseId'],'better')
        self.assertEqual(service.library_file(self.app,self.store,r),p)
        self.assertEqual(p.read_bytes(),b'original movie')
        self.assertNotEqual(source,p);self.assertIn('ReelDock replacement',str(source))
        with self.assertRaisesRegex(ValueError,'Already saved'):service.queue(self.app,self.store,r,'better')
        state=service.compose(self.app,self.store,r)
        self.assertTrue(state['active']);self.assertEqual(state['status'],'Queued');self.assertFalse(state['hold'])

    def test_replace_rejects_active_held_ineligible_and_not_better_release(self):
        r,p=self.movie()
        with self.store.connect() as db:db.execute('UPDATE managed_media SET hold=1 WHERE key=?',(r['key'],))
        self.assertEqual(self.post(dict(action='replace',key=r['key'],value='better')).status_code,400)
        with self.store.connect() as db:db.execute('UPDATE managed_media SET hold=0 WHERE key=?',(r['key'],))
        with patch.object(service,'candidates',return_value=dict(items=[dict(id='bad',quality='1080p',reason='Sample content excluded')])):
            self.assertEqual(self.post(dict(action='replace',key=r['key'],value='bad')).status_code,400)
        with patch.object(files,'quality',return_value='1080'),patch.object(service,'candidates',return_value=dict(items=[dict(id='bad',quality='1080p',reason='')])):
            self.assertEqual(self.post(dict(action='replace',key=r['key'],value='bad')).status_code,400)
        with self.store.connect() as db:db.execute("UPDATE discover_requests SET state='paused' WHERE episode_key=?",(r['key'],))
        self.assertEqual(self.post(dict(action='replace',key=r['key'],value='better')).status_code,400)
        self.assertEqual(p.read_bytes(),b'original movie')

    def test_replace_refuses_split_movies_without_creating_a_request(self):
        r,p=self.movie();part=p.with_name(p.stem+'-cd1.mkv');p.rename(part)
        ds.mark_owned(self.store,r['key'],'previous',str(part))
        with patch.object(service,'candidates') as candidates:
            response=self.post(dict(action='replace',key=r['key'],value='better'))
        self.assertEqual(response.status_code,400,response.json);self.assertIn('Split movies',response.json['error'])
        candidates.assert_not_called();self.assertEqual(part.read_bytes(),b'original movie')
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM discover_requests').fetchone()[0],1)

    def test_replace_swaps_verified_copy_at_exact_old_path_and_rewrites_sidecars(self):
        r,p,q,source=self.replacement();p.with_suffix('.nfo').write_text('<movie><title>Old NFO</title></movie>')
        def verified(temporary,policy):
            self.assertEqual(temporary.parent,p.parent);self.assertEqual(p.read_bytes(),b'original movie')
            self.assertEqual(temporary.read_bytes(),b'new verified movie');self.assertEqual(policy['quality'],'1080')
        with patch('backend.download_control.paused',return_value=False),patch('backend.movie_options.verify_file',side_effect=verified) as verifier:
            result=files.finish_replacement(self.app,self.store,q,[str(source)])
        verifier.assert_called_once();self.assertEqual(result,str(p));self.assertEqual(p.read_bytes(),b'new verified movie')
        self.assertEqual(service.library_file(self.app,self.store,r),p)
        self.assertEqual(Path(ds.owned(self.store,r['key'])['path']),p)
        self.assertIn('Old Film',p.with_suffix('.nfo').read_text());self.assertFalse(source.parent.exists())
        with self.store.connect() as db:
            history=[h[0] for h in db.execute('SELECT message FROM managed_history WHERE media_key=?',(r['key'],))]
        self.assertTrue(any('Replaced '+str(p)+' -> '+str(p) in h for h in history))

    def test_replace_verification_failure_preserves_old_file_nfo_and_ownership(self):
        r,p,q,source=self.replacement();p.with_suffix('.nfo').write_bytes(b'old metadata')
        before=ds.owned(self.store,r['key'])
        with patch('backend.download_control.paused',return_value=False),patch('backend.movie_options.verify_file',side_effect=ValueError('Not verified')):
            with self.assertRaises(ValueError):files.finish_replacement(self.app,self.store,q,[str(source)])
        self.assertEqual(p.read_bytes(),b'original movie');self.assertEqual(p.with_suffix('.nfo').read_bytes(),b'old metadata')
        self.assertEqual(ds.owned(self.store,r['key']),before)
        self.assertFalse(any(p.parent.glob('.reeldock-replacement-*')))

    def test_replace_holds_and_changed_old_inode_prevent_swap(self):
        r,p,q,source=self.replacement()
        with patch('backend.download_control.paused',return_value=True),patch('backend.movie_options.verify_file') as verifier:
            self.assertIsNone(files.finish_replacement(self.app,self.store,q,[str(source)]));verifier.assert_not_called()
        with self.store.connect() as db:db.execute('UPDATE managed_media SET hold=1 WHERE key=?',(r['key'],))
        with patch('backend.download_control.paused',return_value=False):
            self.assertIsNone(files.finish_replacement(self.app,self.store,q,[str(source)]))
        with self.store.connect() as db:db.execute('UPDATE managed_media SET hold=0 WHERE key=?',(r['key'],))
        injected=p.with_name('changed.mkv');injected.write_bytes(b'changed file');injected.replace(p)
        with patch('backend.download_control.paused',return_value=False):
            with self.assertRaisesRegex(ValueError,'Saved file changed'):files.finish_replacement(self.app,self.store,q,[str(source)])
        self.assertEqual(p.read_bytes(),b'changed file')

    def test_replacement_reconciler_records_final_path_and_never_exposes_staging(self):
        r,p,q,source=self.replacement()
        folder=Path(self.store.directory)/'nzb-engine'/'jobs';folder.mkdir(parents=True,exist_ok=True)
        path=folder/'replacement-job.json'
        path.write_text(json.dumps(dict(phase='stopped',progress_stage='publishing',published={'0':str(source)})))
        with patch('backend.download_control.paused',return_value=False),patch('backend.movie_options.verify_file') as verifier:
            acquire.sync_jobs(self.app,self.store)
            self.assertEqual(service.library_file(self.app,self.store,r),p);verifier.assert_not_called()
            path.write_text(json.dumps(dict(phase='stopped',progress_stage='complete',published={'0':str(source)})))
            acquire.sync_jobs(self.app,self.store)
        self.assertEqual(service.request_for(self.store,r['key'])['state'],'complete')
        self.assertEqual(p.read_bytes(),b'new verified movie');self.assertEqual(Path(ds.owned(self.store,r['key'])['path']),p)

    def test_replacement_swap_journal_recovers_restart_without_double_history(self):
        r,p,q,source=self.replacement()
        with patch('backend.download_control.paused',return_value=False),patch('backend.movie_options.verify_file'):
            files.finish_replacement(self.app,self.store,q,[str(source)])
            # The reconciler can restart between the swap and its complete update.
            files.finish_replacement(self.app,self.store,q,[str(source)])
        self.assertEqual(p.read_bytes(),b'new verified movie')
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM managed_history WHERE message LIKE 'Replaced %'").fetchone()[0],1)

    def test_replacement_cancellation_after_verification_prevents_swap(self):
        r,p,q,source=self.replacement()
        def cancel(*args):
            with self.store.connect() as db:
                db.execute('UPDATE managed_media SET hold=1 WHERE key=?',(r['key'],))
                db.execute("UPDATE discover_requests SET state='cancelled',stop_generation=stop_generation+1 WHERE id=?",(q['id'],))
        with patch('backend.download_control.paused',return_value=False),patch('backend.movie_options.verify_file',side_effect=cancel):
            self.assertIsNone(files.finish_replacement(self.app,self.store,q,[str(source)]))
        self.assertEqual(p.read_bytes(),b'original movie');self.assertTrue(records.row(self.store,r['key'])['hold'])
        self.assertFalse(any(p.parent.glob('.reeldock-replacement-*')))

    def test_replacement_rejects_symlink_publication_without_touching_old_file(self):
        r,p,q,source=self.replacement();source.unlink()
        outside=Path(self.tmp.name)/'outside.mkv';outside.write_bytes(b'outside');source.symlink_to(outside)
        with patch('backend.download_control.paused',return_value=False),patch('backend.movie_options.verify_file') as verifier:
            with self.assertRaises(ValueError):files.finish_replacement(self.app,self.store,q,[str(source)])
        verifier.assert_not_called();self.assertEqual(p.read_bytes(),b'original movie');self.assertEqual(outside.read_bytes(),b'outside')

    def test_new_replacement_does_not_inherit_previous_swap_or_automation_marker(self):
        r,p,q,source=self.replacement()
        with patch('backend.download_control.paused',return_value=False),patch('backend.movie_options.verify_file'):
            files.finish_replacement(self.app,self.store,q,[str(source)])
        q=ds.update_request(self.store,q['id'],state='complete')
        payload=json.loads(q['body']);payload['discoveryEngine']=1;payload['movieAutomatic']=True
        with self.store.connect() as db:db.execute('UPDATE discover_requests SET body=? WHERE id=?',(json.dumps(payload),q['id']))
        with patch.object(service,'candidates',return_value=dict(items=[dict(id='better',quality='1080p',reason='')])),\
             patch.object(files,'quality',return_value='720'),patch.object(acquire,'resolve_release'):
            new=service.queue(self.app,self.store,r,'better',replace=True)[0]
        b=json.loads(new['body'])
        for field in ('replacementSwap','replacementRecorded','discoveryEngine','movieAutomatic'):self.assertNotIn(field,b)
        self.assertNotEqual(b['replacementName'],payload['replacementName'])

    def test_delete_folder_retains_unrecognised_files(self):
        r,p=self.movie()
        (p.parent/'poster.jpg').write_bytes(b'art');doc=p.parent/'notes.docx';doc.write_bytes(b'doc')
        archive=p.parent/'extras.zip';archive.write_bytes(b'zip')
        response=self.post(dict(action='delete',key=r['key'],value={'folder':True},confirm=True))
        self.assertEqual(response.status_code,200,response.json);self.assertFalse(p.exists())
        self.assertTrue(p.parent.is_dir());self.assertTrue(doc.is_file());self.assertTrue(archive.is_file())
        self.assertTrue((p.parent/'poster.jpg').is_file())
        with self.store.connect() as db:
            message=db.execute("SELECT message FROM managed_history WHERE media_key=? ORDER BY created DESC LIMIT 1",(r['key'],)).fetchone()[0]
        self.assertIn('movie folder kept',message)

    def test_listing_uses_cached_quality_without_probing_every_row(self):
        r,p=self.movie()
        with patch.object(files.subprocess,'run') as ffprobe:
            item=next(i for i in service.listing(self.app,self.store)['items'] if i['key']==r['key'])
        ffprobe.assert_not_called();self.assertEqual(item['fileQuality'],'Not verified')
        probe=Mock(stdout=json.dumps(dict(streams=[dict(codec_type='video',width=1920,height=800)],format=dict(format_name='matroska'))))
        with patch.object(files.subprocess,'run',return_value=probe):
            self.assertEqual(service.compose(self.app,self.store,r)['fileQuality'],'1080')
        with patch.object(files.subprocess,'run') as ffprobe:
            item=next(i for i in service.listing(self.app,self.store)['items'] if i['key']==r['key'])
        ffprobe.assert_not_called();self.assertEqual(item['fileQuality'],'1080')

    def test_replacement_rejects_measured_downgrade(self):
        r,p,q,source=self.replacement()
        def measured(fd,name,before):return ('1080',{'matroska'}) if name==p.name else ('720',{'matroska'})
        with patch('backend.download_control.paused',return_value=False),patch('backend.movie_options.verify_file'),\
             patch.object(files,'probe_media',side_effect=measured):
            with self.assertRaises(ValueError):files.finish_replacement(self.app,self.store,q,[str(source)])
        self.assertEqual(p.read_bytes(),b'original movie');self.assertFalse(any(p.parent.glob('.reeldock-replacement-*')))

    def test_replacement_rejects_container_mismatch(self):
        r,p,q,source=self.replacement()
        def measured(fd,name,before):return ('720',{'matroska'}) if name==p.name else ('2160',{'mov','mp4'})
        with patch('backend.download_control.paused',return_value=False),patch('backend.movie_options.verify_file'),\
             patch.object(files,'probe_media',side_effect=measured):
            with self.assertRaises(ValueError):files.finish_replacement(self.app,self.store,q,[str(source)])
        self.assertEqual(p.read_bytes(),b'original movie')

    def test_rename_journal_heals_ownership_after_interrupted_rename(self):
        r,p=self.movie()
        new=p.with_name('New Film (2026).mkv');p.rename(new)
        records.patch_body(self.store,r['key'],renameJournal=dict(old=str(p),new=str(new)),localPath=str(p))
        self.assertEqual(service.library_file(self.app,self.store,records.row(self.store,r['key'])),new)
        self.assertEqual(Path(ds.owned(self.store,r['key'])['path']),new)
        self.assertNotIn('renameJournal',service.body(records.row(self.store,r['key'])))
        self.assertNotIn('localPath',service.body(records.row(self.store,r['key'])))
        self.assertEqual(service.library_file(self.app,self.store,records.row(self.store,r['key'])),new)

    def test_local_scan_never_lists_replacement_staging(self):
        staging=self.root/'Movies'/'ReelDock replacement abc123';staging.mkdir(parents=True)
        (staging/'ReelDock replacement abc123.mkv').write_bytes(b'new')
        (self.root/'Movies'/'.reeldock-replacement-deadbeef.mkv').write_bytes(b'staged')
        film=self.root/'Movies'/'Film';film.mkdir();(film/'Film.mp4').write_bytes(b'0123456789')
        state={};vm.local._scan(self.root,state)
        self.assertEqual([i['title'] for i in state['items']],['Film'])

    def test_replacement_failure_cleans_publication_staging(self):
        r,p,q,source=self.replacement()
        folder=Path(self.store.directory)/'nzb-engine'/'jobs';folder.mkdir(parents=True,exist_ok=True)
        (folder/'replacement-job.json').write_text(json.dumps(dict(phase='stopped',progress_stage='complete',published={'0':str(source)})))
        with patch('backend.download_control.paused',return_value=False),\
             patch.object(files,'finish_replacement',side_effect=ValueError('boom')):
            acquire.sync_jobs(self.app,self.store)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM discover_requests WHERE id=?',(q['id'],)).fetchone()['state'],'failed')
        self.assertFalse(source.parent.exists());self.assertEqual(p.read_bytes(),b'original movie')
