"""Public engine preferences, atomic validation and legacy-worker compatibility."""
import tempfile
import unittest
from pathlib import Path
from backend.app import create_app
from backend import download_options, channel_downloads, settings, sync_schedule

ORIGIN = 'https://settings.example.test'

class EngineSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app(dict(TESTING=True, DATA_DIR=str(Path(self.tmp.name)/'data'), MEDIA_DIR=str(Path(self.tmp.name)/'media')))
        self.store = self.app.extensions['store']
        self.store.set('public_origin', ORIGIN)
        self.store.set('setup_complete', '1')
        with self.store.connect() as db: db.execute("INSERT INTO users VALUES(1,'owner','unused')")
        self.client = self.app.test_client()
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session['user_id']=1; session['csrf']='test-token'

    def get(self, path='/api/settings/youtube'):
        return self.client.get(path, base_url=ORIGIN)

    def post(self, values, token='test-token', origin=ORIGIN):
        return self.client.post('/api/settings/youtube',base_url=ORIGIN,
            headers={'Origin':origin,'X-CSRF-Token':token},json={'values':values})

    def test_existing_values_and_defaults_without_exposing_private_settings(self):
        self.store.set('download_quality','720')
        self.store.set('google_tokens','PRIVATE')
        self.store.set('cookie_secret','PRIVATE')
        self.store.set('metadata_type','kodi')
        response=self.get()
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        values=response.json['values']
        self.assertEqual(values['quality'],'720')
        self.assertTrue(values['convert_fallback'])
        self.assertFalse(values['force_transcode'])
        self.assertNotIn('PRIVATE',response.text)
        for key in ('metadata_type','pause_downloads','auto_downloads','google_tokens','storage_base'):
            self.assertNotIn(key,values)

    def test_save_persists_for_workers_and_legacy_endpoints(self):
        response=self.post({'quality':'720','sponsorblock':'mark','sleep_interval':25,'check_minutes':30})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(download_options.read(self.store)['sponsorblock'],'mark')
        self.assertEqual(channel_downloads.preferences(self.store,'channel')['quality'],'720')
        self.assertEqual(sync_schedule.minutes(self.store),30)
        download_options.save(self.store,{'sleep_interval':12})
        self.assertEqual(self.get().json['values']['sleep_interval'],12)
        other=self.app.test_client()
        with other.session_transaction(base_url=ORIGIN) as session:session['user_id']=1
        self.assertEqual(other.get('/api/settings/youtube',base_url=ORIGIN).json['values']['quality'],'720')

    def test_validation_is_atomic_and_cannot_write_other_engines_or_secrets(self):
        before=self.get().json
        for invalid in ({'quality':'720','sleep_interval':True}, {'quality':'720','sponsorblock':'wrong'},
                        {'quality':'720','metadata_type':'plex'}, {'quality':'720','google_tokens':'changed'},
                        {'check_minutes':0}, {'default_range':3}, {'minimum_video_minutes':121},
                        {'hardware_encoding':'false'}, {}, [], None):
            self.assertEqual(self.post(invalid).status_code,400,invalid)
            self.assertEqual(self.get().json,before)
        self.assertEqual(self.get('/api/settings/unregistered').status_code,404)

    def test_auth_csrf_and_origin_are_required(self):
        self.assertEqual(self.app.test_client().get('/api/settings/youtube',base_url=ORIGIN).status_code,401)
        self.assertEqual(self.post({'quality':'720'},token='wrong').status_code,403)
        self.assertEqual(self.post({'quality':'720'},origin='https://elsewhere.example').status_code,403)
        self.assertEqual(self.get().json['values']['quality'],'1080')

    def test_mode_guard_and_scheduling_side_effects_are_preserved(self):
        self.store.set('local_only','1')
        response=self.post({'quality':'720','use_google_api':True})
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.get().json['values']['quality'],'1080')
        self.store.set('next_sync','9999999999')
        self.assertEqual(self.post({'use_google_api':False}).status_code,200)
        self.assertEqual(self.store.get('next_sync'),'0')
        self.store.set('next_sync','123')
        self.assertEqual(self.post({'sync_paused':True}).status_code,200)
        self.assertEqual(self.store.get('next_sync'),'123')
        self.assertEqual(self.post({'sync_paused':False}).status_code,200)
        self.assertEqual(self.store.get('next_sync'),'0')

    def test_invalid_stored_values_use_safe_defaults_without_rewriting_database(self):
        self.store.set('download_sleep_interval','bad')
        self.store.set('download_default_range','42')
        self.store.set('download_force_transcode','surprise')
        values=settings.read(self.store,'youtube')
        self.assertEqual(values['sleep_interval'],0)
        self.assertEqual(values['default_range'],180)
        self.assertFalse(values['force_transcode'])
        self.assertEqual(self.store.get('download_sleep_interval'),'bad')

class M3uEngineSettingsTests(unittest.TestCase):
    """Same security contract, independent public preference namespace."""
    setUp = EngineSettingsTests.setUp
    def test_m3u_preferences_do_not_change_sources_credentials_or_youtube(self):
        from backend import m3u, xtream
        playlist=m3u.save(self.store,{'name':'Fixture playlist','url':'https://example.com/list.m3u','hours':0.5})
        login=xtream.save(self.store,{'name':'Fixture Xtream','server':'https://example.com','username':'fixture-user','password':'fixture-private','enabled':False})
        with self.store.connect() as db:
            sources=[tuple(r) for r in db.execute('SELECT * FROM m3u_sources ORDER BY id')]
            accounts=[tuple(r) for r in db.execute('SELECT * FROM xtream_accounts ORDER BY source')]
        before=settings.read(self.store,'youtube')
        response=self.client.post('/api/settings/m3u',base_url=ORIGIN,headers={'Origin':ORIGIN,'X-CSRF-Token':'test-token'},json={'values':{'share_metadata':False,'refresh_emby':True}})
        self.assertEqual(response.status_code,200,response.json)
        self.assertNotIn('fixture-private',response.text)
        self.assertEqual(response.json['values'],{'share_metadata':False,'refresh_emby':True,'auto_libraries':False,'min_download_bytes':0,'max_download_bytes':100_000_000_000})
        self.assertEqual(self.store.get('m3u_share_metadata'),'false')
        self.assertEqual(settings.read(self.store,'youtube'),before)
        with self.store.connect() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM m3u_sources ORDER BY id')],sources)
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM xtream_accounts ORDER BY source')],accounts)
        legacy=self.client.get('/api/m3u-download-settings',base_url=ORIGIN)
        self.assertFalse(legacy.json['share_metadata'])
        legacy=self.client.post('/api/m3u-download-settings',base_url=ORIGIN,headers={'Origin':ORIGIN,'X-CSRF-Token':'test-token'},json={'share_metadata':True})
        self.assertEqual(legacy.status_code,200,legacy.json)
        self.assertTrue(settings.read(self.store,'m3u')['share_metadata'])

    def test_m3u_batch_scope_csrf_and_secret_rejection(self):
        before=settings.read(self.store,'m3u')
        for values in ({'share_metadata':False,'quality':'720'},{'share_metadata':False,'auto_libraries':'yes'}, {'url':'https://example.com/private'}, {'password':'private'}, {'folders':{}}, {'hours':24}):
            response=self.client.post('/api/settings/m3u',base_url=ORIGIN,headers={'Origin':ORIGIN,'X-CSRF-Token':'test-token'},json={'values':values})
            self.assertEqual(response.status_code,400,values)
            self.assertEqual(settings.read(self.store,'m3u'),before)
        self.assertEqual(self.client.post('/api/settings/m3u',base_url=ORIGIN,headers={'Origin':ORIGIN},json={'values':{'share_metadata':False}}).status_code,403)
        self.assertEqual(self.app.test_client().get('/api/settings/m3u',base_url=ORIGIN).status_code,401)

    def test_source_options_stay_per_source_and_keep_fractional_refresh(self):
        from backend import m3u
        from backend.settings.m3u import source_options
        self.assertEqual(source_options({'hours':0.5})['hours'],0.5)
        self.assertEqual(m3u.options_from({'hours':0})['enabled'],False)
        self.assertEqual(source_options({'naming':'emby'})['naming'],'emby')
        for bad in ({'hours':True},{'hours':float('nan')},{'hours':169},{'numeric_ids':'invalid'},{'clean_sync':'false'}):
            with self.assertRaises(ValueError):source_options(bad)

class NzbEngineSettingsTests(unittest.TestCase):
    def setUp(self):
        EngineSettingsTests.setUp(self)
        from backend.nzb import stop
        self.addCleanup(stop, self.app)
        media = Path(self.tmp.name) / 'media'
        media.mkdir(exist_ok=True)
        self.store.set('storage_base', str(media))

    def post(self, values, path='/api/settings/nzb', token='test-token', origin=ORIGIN):
        return self.client.post(path, base_url=ORIGIN, headers={'Origin':origin,'X-CSRF-Token':token},
                                json={'values':values} if path=='/api/settings/nzb' else values)

    def get(self):
        return self.client.get('/api/settings/nzb', base_url=ORIGIN)

    def test_public_limits_keep_existing_credentials_and_engine_storage(self):
        import json
        secret = 'fixture-private-value'
        legacy = self.post({'servers':[{'host':'news.example.test','ssl':True,'password':secret}],
                            'indexers':[{'url':'http://127.0.0.1','api_key':secret}]}, '/api/nzb/settings')
        self.assertEqual(legacy.status_code,200,legacy.json)
        response = self.post({'max_active_downloads':7,'download_limit_bps':2048})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(set(response.json['values']), {'max_active_downloads','download_limit_bps','share_metadata','min_download_bytes','max_download_bytes','min_video_minutes','preferred_quality','allow_lower_quality'})
        self.assertNotIn(secret,response.text)
        engine = self.app.extensions['nzb_engine']
        self.assertEqual(engine.config['max_active_downloads'],7)
        self.assertEqual(engine.config['servers'][0]['password'],secret)
        path=engine.data_dir/'settings.json'
        self.assertEqual(json.loads(path.read_text())['indexers'][0]['api_key'],secret)
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertEqual(engine._read_settings()['download_limit_bps'],2048)
        self.assertEqual(self.post({'max_active_downloads':2},'/api/nzb/settings').status_code,200)
        self.assertEqual(self.get().json['values']['max_active_downloads'],2)
        self.assertEqual(self.get().headers['Cache-Control'],'no-store')

    def test_public_validation_and_access_are_atomic(self):
        before=self.get().json
        for values in ({'max_active_downloads':1000}, {'max_active_downloads':True},
                       {'share_metadata':'false'}, {'download_limit_bps':-1}, {'download_limit_bps':1.5},
                       {'max_active_downloads':4,'servers':[]}, {'password':'private'},
                       {'quality':'720'}, {}, None):
            self.assertEqual(self.post(values).status_code,400,values)
            self.assertEqual(self.get().json,before)
        self.assertEqual(self.post({'max_active_downloads':4},token='bad').status_code,403)
        self.assertEqual(self.post({'max_active_downloads':4},origin='https://elsewhere.test').status_code,403)
        self.assertEqual(self.app.test_client().get('/api/settings/nzb',base_url=ORIGIN).status_code,401)
        # Zero is accepted as unlimited concurrency.
        self.assertEqual(self.post({'max_active_downloads':0}).status_code,200)
        self.store.set('reset_in_progress','1')
        self.assertEqual(self.get().status_code,503)

    def test_failed_disk_save_leaves_runtime_and_private_file_unchanged(self):
        from unittest.mock import patch
        self.assertEqual(self.post({'max_active_downloads':5}).status_code,200)
        engine=self.app.extensions['nzb_engine']
        path=engine.data_dir/'settings.json'
        before=path.read_bytes()
        with patch('backend.downloaders.nzb.engine.os.replace',side_effect=OSError('fixture disk error')):
            self.assertEqual(self.post({'max_active_downloads':8}).status_code,400)
        self.assertEqual(engine.settings()['max_active_downloads'],5)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(list(engine.data_dir.glob('.settings-*')),[])

class TorrentEngineSettingsTests(unittest.TestCase):
    def setUp(self):
        EngineSettingsTests.setUp(self)
        from backend.torrents import stop
        self.addCleanup(stop, self.app)
        media=Path(self.tmp.name)/'media'
        media.mkdir(exist_ok=True)
        self.store.set('storage_base',str(media))

    def post(self, values, token='test-token', origin=ORIGIN):
        return self.client.post('/api/settings/torrent',base_url=ORIGIN,
            headers={'Origin':origin,'X-CSRF-Token':token},json={'values':values})

    def get(self):
        return self.client.get('/api/settings/torrent',base_url=ORIGIN)

    def test_saved_limits_and_metadata_are_shared_with_legacy_engine(self):
        import json
        response=self.post({'download_limit_bps':4096,'max_active_downloads':0,'share_metadata':False})
        self.assertEqual(response.status_code,200,response.json)
        engine=self.app.extensions['torrent_engine']
        self.assertEqual(engine.config['download_limit_bps'],4096)
        self.assertFalse(engine.config['share_metadata'])
        path=engine.data_dir/'settings.json'
        self.assertFalse(json.loads(path.read_text())['share_metadata'])
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertNotIn('metadata_generation',response.json['values'])
        self.assertNotIn('theme',response.json['values'])
        self.assertEqual(self.get().headers['Cache-Control'],'no-store')
        generation=engine.config['metadata_generation']
        self.assertEqual(self.post({'share_metadata':True}).status_code,200)
        self.assertNotEqual(engine.config['metadata_generation'],generation)

    def test_invalid_values_and_access_do_not_mutate_settings(self):
        before=self.get().json
        for patch in ({'listen_port':65536},{'enable_dht':'false'},{'download_limit_bps':-1},
                      {'max_active_downloads':True},{'encryption':'bogus'},
                      {'max_active_downloads':4,'theme':'dark'},{'share_metadata':1}):
            self.assertEqual(self.post(patch).status_code,400,patch)
            self.assertEqual(self.get().json,before)
        self.assertEqual(self.post({'enable_pex':False},token='bad').status_code,403)
        self.assertEqual(self.post({'enable_pex':False},origin='https://wrong.test').status_code,403)
        self.assertEqual(self.app.test_client().get('/api/settings/torrent',base_url=ORIGIN).status_code,401)

    def test_disk_failure_preserves_runtime_and_file(self):
        from unittest.mock import patch
        self.assertEqual(self.post({'max_active_downloads':5}).status_code,200)
        engine=self.app.extensions['torrent_engine']
        path=engine.data_dir/'settings.json'
        before=path.read_bytes()
        with patch('backend.downloaders.torrent.engine.os.replace',side_effect=OSError('fixture')):
            self.assertEqual(self.post({'max_active_downloads':6}).status_code,400)
        self.assertEqual(engine.config['max_active_downloads'],5)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(list(engine.data_dir.glob('.settings-*')),[])

class AutoDownloadEngineSettingsTests(unittest.TestCase):
    setUp = EngineSettingsTests.setUp

    def post(self, values, token='test-token', origin=ORIGIN):
        return self.client.post('/api/settings/auto-download',base_url=ORIGIN,
            headers={'Origin':origin,'X-CSRF-Token':token},json={'values':values})

    def get(self):
        return self.client.get('/api/settings/auto-download',base_url=ORIGIN)

    def test_legacy_switch_and_scoped_settings_share_storage(self):
        download_options.save(self.store,{'auto_downloads':True})
        self.assertEqual(self.get().json['values'],dict(enabled=True,torrent=True,nzb=True,strm=True,release_check_minutes=15))
        self.assertEqual(self.post({'enabled':False,'torrent':False}).status_code,200)
        self.assertFalse(download_options.read(self.store)['auto_downloads'])
        self.assertTrue(self.get().json['values']['nzb'])
        self.assertEqual(self.get().headers['Cache-Control'],'no-store')

    def test_atomic_validation_and_access(self):
        before=self.get().json
        for values in ({'enabled':'false'},{'enabled':True,'directory':'/tmp'}, {'nzb':1}, {}):
            self.assertEqual(self.post(values).status_code,400)
            self.assertEqual(self.get().json,before)
        self.assertEqual(self.post({'enabled':True},token='wrong').status_code,403)
        self.assertEqual(self.post({'enabled':True},origin='https://wrong.test').status_code,403)
        self.assertEqual(self.app.test_client().get('/api/settings/auto-download',base_url=ORIGIN).status_code,401)
