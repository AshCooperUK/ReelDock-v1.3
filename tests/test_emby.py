import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from backend.app import create_app
from backend import emby


class EmbyTests(unittest.TestCase):
    def test_sent_history_limit_preserves_pending_and_deduplication_records(self):
        import hashlib
        scope = hashlib.sha256(b'|').hexdigest()
        with self.store.connect() as db:
            for i in range(25):
                db.execute('INSERT INTO emby_watched VALUES(?,?,?,?)', (scope, f'video{i}', 'sent', 'Sent'))
                db.execute('INSERT INTO emby_attempts VALUES(?,?,?,?,?)', (scope, f'video{i}', i, 1, 0))
            db.execute('INSERT INTO emby_watched VALUES(?,?,?,?)', (scope, 'pending', 'retry', 'Retry'))
            db.execute('INSERT INTO emby_watched VALUES(?,?,?,?)', (scope, 'failed', 'auth_expired', 'Expired'))
        data = emby.settings(self.store)
        self.assertEqual([r['id'] for r in data['attempts'] if r['state'] == 'sent'], [f'video{i}' for i in range(24, 9, -1)])
        self.assertEqual(len(data['attempts']), 17)
        self.assertEqual(data['counts']['sent'], 25)

    def test_live_preferences_preserve_connection_and_do_not_scan(self):
        for key,value in {'emby_url':'http://emby','emby_key':'private','emby_user':'u','emby_users':'[{"id":"u","name":"User"}]','emby_refresh_after':'123'}.items():
            self.store.set(key,value)
        with patch('backend.emby.api') as remote:
            emby.save_preferences(self.store,{'favourites':True})
            remote.assert_not_called()
        self.assertEqual(self.store.get('emby_key'),'private')
        self.assertEqual(self.store.get('emby_refresh_after'),'123')
        self.assertTrue(emby.settings(self.store)['favourites'])
        for bad in ({'url':'http://other'},{'auto_refresh':'yes'},{'user':'unknown'},{'watched':True}):
            with self.assertRaises(ValueError):emby.save_preferences(self.store,bad)
        self.assertEqual(self.store.get('emby_url'),'http://emby')
        self.assertFalse(emby.settings(self.store)['watched'])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=create_app({'TESTING':True,'DATA_DIR':self.tmp.name})
        self.store=self.app.extensions['store']

    def tearDown(self):self.tmp.cleanup()

    def favourite_fixture(self):
        for key,value in {'emby_url':'http://emby','emby_user':'u','emby_favourites':'true'}.items():self.store.set(key,value)
        with self.store.connect() as db:db.execute('INSERT INTO channels VALUES (?,?,1)',('channel',json.dumps({'id':'channel','name':'Test'})))
        return {'Id':'series1','ProviderIds':{'YouTube':'channel'},'UserData':{'IsFavorite':False}}

    def test_favourites_merge_then_propagate_removals_and_additions_both_ways(self):
        item=self.favourite_fixture()
        def request(store,path,method='GET',**kwargs):
            if method=='GET':return {'Items':[item],'TotalRecordCount':1}
            item['UserData']['IsFavorite']=method=='POST'
            return item['UserData'].copy()
        def local(value):
            with self.store.connect() as db:
                if value:db.execute("INSERT OR IGNORE INTO channel_favourites VALUES ('channel')")
                else:db.execute("DELETE FROM channel_favourites WHERE id='channel'")
        def current():
            with self.store.connect() as db:return bool(db.execute('SELECT 1 FROM channel_favourites').fetchone())
        with patch('backend.emby.api',side_effect=request):
            local(True);emby.match_favourites(self.store,threading.Event())
            self.assertTrue(item['UserData']['IsFavorite'])
            item['UserData']['IsFavorite']=False;emby.match_favourites(self.store,threading.Event())
            self.assertFalse(current())
            item['UserData']['IsFavorite']=True;emby.match_favourites(self.store,threading.Event())
            self.assertTrue(current())
            local(False);emby.match_favourites(self.store,threading.Event())
            self.assertFalse(item['UserData']['IsFavorite'])
            local(True);emby.match_favourites(self.store,threading.Event())
            self.assertTrue(item['UserData']['IsFavorite'])

    def test_favourites_first_sync_imports_emby_and_scopes_new_user(self):
        item=self.favourite_fixture();item['UserData']['IsFavorite']=True
        with patch('backend.emby.api',return_value={'Items':[item]}) as api:
            emby.match_favourites(self.store,threading.Event())
            self.assertEqual(api.call_count,1)
        with self.store.connect() as db:self.assertTrue(db.execute('SELECT 1 FROM channel_favourites').fetchone())
        self.store.set('emby_user','other')
        with patch('backend.emby.api',return_value={'Items':[item]}):emby.match_favourites(self.store,threading.Event())
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM emby_favourites').fetchone()[0],2)

    def test_favourites_failures_duplicates_and_disabled_do_not_change_baseline(self):
        item=self.favourite_fixture()
        with self.store.connect() as db:db.execute("INSERT INTO channel_favourites VALUES ('channel')")
        with patch('backend.emby.api',side_effect=[{'Items':[item]},ValueError('offline')]):
            with self.assertRaises(ValueError):emby.match_favourites(self.store,threading.Event())
        with self.store.connect() as db:self.assertFalse(db.execute('SELECT 1 FROM emby_favourites').fetchone())
        with patch('backend.emby.api',return_value={'Items':[item,item]}) as api:
            emby.match_favourites(self.store,threading.Event());self.assertEqual(api.call_count,1)
        self.store.set('emby_favourites','false')
        with patch('backend.emby.api') as api:
            emby.match_favourites(self.store,threading.Event());api.assert_not_called()

    def test_favourites_path_match_pagination_and_unknown_series(self):
        item=self.favourite_fixture();item.pop('ProviderIds');item['Path']='/tv/Test';item['UserData']['IsFavorite']=True
        self.store.set('emby_root','/tv')
        with self.store.connect() as db:db.execute("INSERT INTO media_channels VALUES ('channel','Test')")
        with patch('backend.emby.api',side_effect=[{'Items':[{'Id':'unrelated','Name':'Test','UserData':{'IsFavorite':True}}],'TotalRecordCount':2},{'Items':[item],'TotalRecordCount':2}]) as api:
            emby.match_favourites(self.store,threading.Event())
            self.assertEqual(api.call_args.kwargs['params']['StartIndex'],1)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT id FROM channel_favourites').fetchall()[0][0],'channel')

    def test_favourites_preserves_local_change_during_remote_write(self):
        item=self.favourite_fixture()
        with self.store.connect() as db:db.execute("INSERT INTO channel_favourites VALUES ('channel')")
        def request(store,path,method='GET',**kwargs):
            if method=='GET':return {'Items':[item]}
            with self.store.connect() as db:db.execute("DELETE FROM channel_favourites WHERE id='channel'")
            return {'IsFavorite':True}
        with patch('backend.emby.api',side_effect=request):emby.match_favourites(self.store,threading.Event())
        with self.store.connect() as db:
            self.assertFalse(db.execute('SELECT 1 FROM channel_favourites').fetchone())
            self.assertEqual(db.execute('SELECT value FROM emby_favourites').fetchone()[0],1)

    def test_favourites_requires_user_but_not_youtube_cookies(self):
        with self.assertRaises(ValueError):emby.save(self.store,dict(url='http://emby',key='key',favourites=True))
        with patch('backend.emby.users',return_value=[{'id':'u','name':'Owner'}]):
            emby.connect(self.store,dict(url='http://emby',key='key',favourites=True))
        self.assertTrue(emby.settings(self.store)['favourites'])
        self.assertEqual(emby.settings(self.store)['user'],'u')

    def test_settings_secret_is_not_returned_and_bad_url_rejected(self):
        emby.save(self.store,dict(url='http://emby:8096',key='secret',root='/media/tv_shows',user='user',watched=False,auto_refresh=True))
        data=emby.settings(self.store)
        self.assertTrue(data['key_saved']);self.assertNotIn('secret',json.dumps(data))
        for url in ('file:///data','http://user:password@emby','http://emby?key=secret'):
            with self.assertRaises(ValueError):emby.save(self.store,dict(url=url,key='key'))
        with self.assertRaises(ValueError):emby.save(self.store,dict(url='http://other:8096'))

    def test_refresh_scans_even_without_indexed_channel_or_matched_path(self):
        with patch('backend.emby.api',return_value={}) as api:
            emby.refresh(self.store,'new-channel')
        self.assertEqual(api.call_args.args[1:],('Library/Refresh','POST'))
        self.assertIn('accepted',self.store.get('emby_refresh_status'))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM emby_refresh').fetchone()[0],0)

    def test_refresh_coalesces_downloads_and_preserves_concurrent_completion(self):
        emby.queue_refresh(self.store,'one');emby.queue_refresh(self.store,'two')
        def scan(*args,**kwargs):
            with self.store.connect() as db:db.execute("UPDATE emby_refresh SET updated=updated+1 WHERE channel='one'")
            return {}
        with patch('backend.emby.api',side_effect=scan) as api:
            emby.process_refreshes(self.store);emby.process_refreshes(self.store)
            api.assert_called_once()
        with self.store.connect() as db:self.assertEqual([r[0] for r in db.execute('SELECT channel FROM emby_refresh')],['one'])
        self.store.set('emby_refresh_after','0')
        with patch('backend.emby.api',return_value={}):emby.process_refreshes(self.store)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM emby_refresh').fetchone()[0],0)

    def test_failed_scan_stays_queued_and_backs_off(self):
        emby.queue_refresh(self.store,'one')
        with patch('backend.emby.api',side_effect=ValueError('Emby rejected the request.')) as api:
            emby.process_refreshes(self.store);emby.process_refreshes(self.store)
            api.assert_called_once()
        self.assertIn('Library scan failed:',self.store.get('emby_refresh_status'))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM emby_refresh').fetchone()[0],1)

    def test_watched_only_matches_known_complete_videos_and_deduplicates(self):
        self.store.set('emby_url','http://emby');self.store.set('emby_user','chosen');self.store.set('emby_watched','true')
        with self.store.connect() as db:
            db.execute("INSERT INTO media_files VALUES ('video000001','channel','tv_shows/Example/video.mp4','2026-09-26',1,'emby',100)")
            db.execute("INSERT INTO downloads(id,title,quality,state,created,updated) VALUES ('video000001','Title','1080','complete',0,0)")
        response={'Items':[{'ProviderIds':{'YouTube':'video000001'}},{'ProviderIds':{'YouTube':'unknown0000'}}],'TotalRecordCount':2}
        with patch('backend.emby.api',return_value=response) as api,patch('backend.watched_status.send_watched_request',return_value=('sent','Unverified receipt')) as sender:
            emby.match_watched(self.store,threading.Event());emby.match_watched(self.store,threading.Event())
        sender.assert_called_once()
        self.assertEqual(sender.call_args.args[0],'video000001')
        self.assertEqual(api.call_args.args[1],'Users/chosen/Items')

    def test_watched_requires_cookies_and_selected_user(self):
        with self.assertRaises(ValueError):emby.save(self.store,dict(url='http://emby',key='key',watched=True))

    def test_connect_uses_new_credentials_and_selects_only_user(self):
        self.store.set('emby_url','http://old');self.store.set('emby_key','oldkey')
        def response(connection):
            self.assertEqual(connection.get('emby_url'),'http://new')
            self.assertEqual(connection.get('emby_key'),'newkey')
            return [{'id':'one','name':'Fixture User'}]
        with patch('backend.emby.users',side_effect=response):
            emby.connect(self.store,dict(url='http://new',key='newkey',auto_refresh=True))
        self.assertEqual(emby.settings(self.store)['users'],[{'id':'one','name':'Fixture User'}])
        self.assertEqual(self.store.get('emby_user'),'one')
        with patch('backend.emby.users',side_effect=ValueError('Unavailable')):
            with self.assertRaises(ValueError):emby.connect(self.store,dict(url='http://broken',key='wrong'))
        self.assertEqual(self.store.get('emby_url'),'http://new')

    def test_multiple_users_require_choice_and_keep_saved_names(self):
        available=[{'id':'one','name':'First'},{'id':'two','name':'Second'}]
        with patch('backend.emby.users',return_value=available):
            emby.connect(self.store,dict(url='http://emby',key='key'))
            self.assertEqual(self.store.get('emby_user'),'')
            emby.connect(self.store,dict(url='http://emby',user='two'))
        self.assertEqual(emby.settings(self.store)['users'],available)
        self.assertEqual(self.store.get('emby_user'),'two')

    def test_library_matching_uses_known_identity_and_relative_file(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO media_files VALUES ('video000001','channel','tv_shows/Example/Season 2026/Episode.mp4','2026-09-26',1,'emby',100)")
            db.execute("INSERT INTO downloads(id,title,quality,state,created,updated) VALUES ('video000001','Title','1080','complete',0,0)")
        replies=[{'Items':[{'Locations':['/emby/media']}]},{'Items':[{'ProviderIds':{'YouTube':'video000001'},'Path':'/emby/media/tv_shows/Example/Season 2026/Episode.mp4'}],'TotalRecordCount':1}]
        with patch('backend.emby.api',side_effect=replies):emby.discover_library(self.store)
        self.assertEqual(self.store.get('emby_root'),'/emby/media/tv_shows')

    def test_library_matching_without_extra_tv_folder(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO media_files VALUES ('video000001','channel','Example/Season 2026/Episode.mp4','2026-09-26',1,'emby',100)")
            db.execute("INSERT INTO downloads(id,title,quality,state,created,updated) VALUES ('video000001','Title','1080','complete',0,0)")
        replies=[{'Items':[{'Locations':['/emby/media']}]},{'Items':[{'ProviderIds':{'YouTube':'video000001'},'Path':'/emby/media/Example/Season 2026/Episode.mp4'}],'TotalRecordCount':1}]
        with patch('backend.emby.api',side_effect=replies):emby.discover_library(self.store)
        self.assertEqual(self.store.get('emby_root'),'/emby/media')

    def test_library_names_alone_never_trigger_a_match(self):
        with patch('backend.emby.api',return_value={'Items':[{'Name':'YouTube','Locations':['/unrelated/tv_shows']}]}) as api:
            emby.discover_library(self.store)
        self.assertEqual(self.store.get('emby_root'),'')
        api.assert_called_once()

    def test_status_shows_saved_failure_for_current_user_only(self):
        import hashlib
        self.store.set('emby_url','http://emby');self.store.set('emby_user','one')
        scope=hashlib.sha256(b'http://emby|one').hexdigest()
        with self.store.connect() as db:
            db.execute("INSERT INTO emby_watched VALUES (?,?,?,?)",(scope,'video000001','auth_unverified','No watched request was sent.'))
            db.execute("INSERT INTO emby_watched VALUES ('other','video000002','sent','Other account')")
        data=emby.settings(self.store)
        self.assertEqual(data['counts'],{'auth_unverified':1})
        self.assertEqual(data['watched_status'],'No watched request was sent.')

    def test_failed_session_retries_then_pauses_without_resending_uncertain_requests(self):
        self.store.set('emby_url','http://emby');self.store.set('emby_user','chosen');self.store.set('emby_watched','true')
        with self.store.connect() as db:
            db.execute("INSERT INTO media_files VALUES ('video000001','channel','tv_shows/Example/video.mp4','2026-09-26',1,'emby',100)")
            db.execute("INSERT INTO downloads(id,title,quality,state,created,updated) VALUES ('video000001','Title','1080','complete',0,0)")
        response={'Items':[{'ProviderIds':{'YouTube':'video000001'}}],'TotalRecordCount':1}
        with patch('backend.emby.api',return_value=response),patch('backend.watched_status.send_watched_request',return_value=('auth_unverified','No request sent')) as sender:
            emby.match_watched(self.store,threading.Event())
            emby.match_watched(self.store,threading.Event())
            self.assertEqual(sender.call_count,1)
            for _ in range(2):
                with self.store.connect() as db:db.execute('UPDATE emby_attempts SET retry_after=0')
                emby.match_watched(self.store,threading.Event())
        self.assertEqual(sender.call_count,3)
        self.assertEqual(self.store.get('emby_watched'),'true')
        self.assertIn('Paused:',self.store.get('emby_watched_paused'))
        self.store.set('emby_watched','true')
        emby.retry_watched(self.store,'video000001')
        with patch('backend.emby.api',return_value=response),patch('backend.watched_status.send_watched_request',return_value=('unverified','May have reached YouTube')) as sender:
            emby.match_watched(self.store,threading.Event());emby.match_watched(self.store,threading.Event())
        sender.assert_called_once()
        self.assertEqual(emby.settings(self.store)['attempts'][0]['state'],'unverified')

    def test_manual_retry_does_not_cross_users_or_retry_sent_requests(self):
        import hashlib
        self.store.set('emby_url','http://emby');self.store.set('emby_user','one');self.store.set('emby_watched','true')
        scope=hashlib.sha256(b'http://emby|one').hexdigest()
        with self.store.connect() as db:
            db.execute("INSERT INTO emby_watched VALUES (?,?,?,?)",(scope,'video000001','sent','sent'))
            db.execute("INSERT INTO emby_watched VALUES ('other','video000002','failed','failed')")
        for video in ('video000001','video000002'):
            with self.assertRaises(ValueError):emby.retry_watched(self.store,video)

    def test_cookie_check_is_queued_requires_csrf_and_does_not_send_watched_request(self):
        origin='https://example.test'
        self.store.set('public_origin',origin);self.store.set('setup_complete','1')
        with self.store.connect() as db:db.execute("INSERT INTO users VALUES (1,'owner','unused')")
        client=self.app.test_client()
        with client.session_transaction(base_url=origin) as session:
            session['user_id']=1;session['csrf']='token'
        with patch('backend.watched_status.check_cookie_session') as check:
            self.assertEqual(client.post('/api/emby',base_url=origin,json={'action':'check_cookies'}).status_code,403)
            response=client.post('/api/emby',base_url=origin,json={'action':'check_cookies'},headers={'Origin':origin,'X-CSRF-Token':'token'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.store.get('emby_cookie_check'),'queued')
        check.assert_not_called()
