import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import m3u, m3u_metadata
from backend.app import create_app

ORIGIN = 'https://dev.example.com'


def playlist(*titles):
    return '#EXTM3U\n' + '\n'.join(f'#EXTINF:-1 group-title="Movie VOD",{title}\nhttps://example.com/movie/{i}.mp4' for i, title in enumerate(titles)) + '\n'


class M3UTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'library'
        self.app = create_app({'TESTING': True, 'DATA_DIR': self.temp.name, 'M3U_DIR': str(self.root)})
        self.store = self.app.extensions['store']
        self.store.set('public_origin', ORIGIN); self.store.set('setup_complete', '1')
        with self.store.connect() as db: db.execute("INSERT INTO users VALUES(1,'admin','unused')")
        self.client = self.app.test_client()
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session['user_id'] = 1; session['csrf'] = 'test'
        self.payload = dict(name='Test library', url='https://example.com/private/token/playlist', existing_library=True)
        self.source = m3u.save(self.store, self.payload)

    def tearDown(self): self.temp.cleanup()

    def test_sync_changes_by_type_and_failed_run_does_not_repeat_counts(self):
        result=self.run_import(playlist('First film (2024)','Second film (2025)'))
        self.assertEqual(result['stats']['changes']['movies'],dict(added=2,updated=0,removed=0))
        result=self.run_import(playlist('First film (2024)'))
        self.assertEqual(result['stats']['changes']['movies'],dict(added=0,updated=0,removed=1))
        result=self.run_import(playlist('First film (2024)'))
        self.assertEqual(result['stats']['changes'],{})
        m3u.request_sync(self.store,self.source)
        def fail(url):raise ValueError('Provider unavailable')
        m3u.tick(self.store,self.root,fetch=fail)
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM m3u_runs ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(row['state'],'error')
        self.assertEqual(json.loads(row['stats']),{})

    def test_permission_error_names_folder_without_leaking_stream_url(self):
        import errno
        blocked = self.root / 'Movie_VOD' / 'Example' / '.reeldock-test.tmp'
        with patch.object(m3u, '_export', side_effect=PermissionError(errno.EACCES, 'denied', str(blocked))):
            with self.assertRaisesRegex(ValueError, 'Permission denied') as caught:
                m3u.export(self.store, self.root, {}, [], 0)
        self.assertIn('Example', str(caught.exception))
        self.assertNotIn('.tmp', str(caught.exception))
        self.assertNotIn(str(self.root), str(caught.exception))

    def post(self, payload, token='test'):
        return self.client.post('/api/m3u', base_url=ORIGIN, json=payload, headers={'Origin': ORIGIN, 'X-CSRF-Token': token})

    def run_import(self, text):
        m3u.request_sync(self.store, self.source)
        m3u.tick(self.store, self.root, fetch=lambda url: text)
        return m3u.subscriptions(self.store, self.root)[0]

    def test_classification_and_scrub_preserve_emby_layout(self):
        text = '''#EXTM3U
#EXTINF:-1 tvg-id="" group-title="Movie VOD",HD : Film, The (2024)
https://example.com/movie/1.mp4
#EXTINF:-1 group-title="TV VOD",Example Show S02E07
https://example.com/series/2.mp4
#EXTINF:-1 group-title="TV VOD",Other Show 2x7
https://example.com/series/3.mp4
#EXTINF:0 group-title="TV VOD",Daily Show 2026 09 23 Guest HDTV
https://example.com/series/4.mp4
#EXTINF:-1 tvg-id="news" group-title="News, UK",News
#EXTVLCOPT:http-user-agent=Example
https://example.com/live/5.ts
#EXTINF:0 group-title="Unknown",Mystery
https://example.com/6.mp4
'''
        entries, _ = m3u.parse(text, dict(m3u.DEFAULTS, scrub=['HD :', 'SD :', r'\,']))
        self.assertEqual(entries[0]['path'], 'Movie_VOD/Film, The (2024)/Film, The (2024).strm')
        self.assertEqual(entries[1]['path'], 'TV_VOD/Example Show/Season 02/Example Show S02E07.strm')
        self.assertEqual(entries[2]['path'], 'TV_VOD/Other Show/Season 02/Other Show S02E07.strm')
        self.assertEqual(entries[3]['path'], 'TV_VOD/Daily Show/Daily Show 2026 09 23 Guest.strm')
        self.assertEqual(entries[4]['kind'], 'live')
        self.assertIn('#EXTVLCOPT:', entries[4]['content'])
        self.assertEqual(entries[5]['kind'], 'unsorted')

    def test_takeover_adopts_same_link_without_newline_and_keeps_artwork(self):
        file = self.root / 'Movie_VOD/Film (2024)/Film (2024).strm'
        file.parent.mkdir(parents=True)
        file.write_text('https://example.com/movie/0.mp4', encoding='utf-8')
        artwork = file.with_suffix('.nfo'); artwork.write_text('Emby metadata')
        result = self.run_import(playlist('Film (2024)'))
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['stats']['adopted'], 1)
        self.assertEqual(file.read_text(), 'https://example.com/movie/0.mp4')
        second = self.run_import(playlist('Film (2024)'))
        self.assertEqual(second['stats']['updated'], 0)
        self.assertEqual(file.read_text(), 'https://example.com/movie/0.mp4')
        result = self.run_import(playlist('New Film (2025)'))
        self.assertFalse(file.exists()); self.assertTrue(artwork.exists())
        self.assertEqual(result['stats']['removed'], 1)

    def test_emby_ids_reuse_legacy_file_and_rotated_links_keep_location(self):
        original = self.root / 'Movie_VOD/Example (2024)/Example (2024).strm'
        original.parent.mkdir(parents=True)
        original.write_text('https://example.com/movie/original', encoding='utf-8')
        artwork = original.parent / 'poster.jpg'; artwork.write_bytes(b'artwork')
        m3u.save(self.store, dict(self.payload, id=self.source, naming='emby'))
        text = '#EXTM3U\n#EXTINF:0 tvg-type="movie" tvg-id="tt123",Example (2024)\nhttps://example.com/movie/original\n'
        result = self.run_import(text)
        self.assertEqual(result['stats']['added'], 1) # Live playlist only.
        self.assertEqual(result['stats']['adopted'], 1)
        self.assertEqual(list(self.root.rglob('*.strm')), [original])
        result = self.run_import(text.replace('/original', '/replacement'))
        self.assertEqual(result['stats']['added'], 0)
        self.assertEqual(result['stats']['updated'], 1)
        self.assertIn('/replacement', original.read_text())
        self.assertEqual(artwork.read_bytes(), b'artwork')
        self.assertEqual(list(self.root.rglob('*.strm')), [original])

    def test_repeated_stream_with_different_quality_title_is_one_output(self):
        text = '#EXTM3U\n#EXTINF:0 tvg-type="movie",Example (2024)\nhttps://example.com/movie/one\n#EXTINF:0 tvg-type="movie",Example (2024) [1080p]\nhttps://example.com/movie/one\n'
        entries, _ = m3u.parse(text, dict(m3u.DEFAULTS, naming='emby'))
        self.assertEqual(len(entries), 1)

    def test_root_takeover_does_not_adopt_files_from_another_subscription(self):
        other = m3u.save(self.store, dict(self.payload, name='Other', existing_library=False))
        path = self.root / other / 'Movie_VOD/Film (2024)/Film (2024).strm'
        path.parent.mkdir(parents=True); path.write_text('https://example.com/movie/0.mp4')
        self.run_import(playlist('Film (2024)'))
        result = next(r for r in m3u.subscriptions(self.store,self.root) if r['id']==self.source)
        self.assertEqual(result['stats']['adopted'], 0)
        self.assertTrue((self.root / 'Movie_VOD/Film (2024)/Film (2024).strm').exists())
        self.assertTrue(path.exists())

    def test_dry_run_reuses_original_without_changing_files_or_aliases(self):
        original = self.root / 'Movie_VOD/Example (2024)/Example (2024).strm'
        original.parent.mkdir(parents=True); original.write_text('https://example.com/movie/one')
        m3u.save(self.store, dict(self.payload, id=self.source, naming='emby'))
        text = '#EXTM3U\n#EXTINF:0 tvg-type="movie" tvg-id="tt123",Example (2024)\nhttps://example.com/movie/one\n'
        with self.store.connect() as db: source=dict(db.execute('SELECT * FROM m3u_sources WHERE id=?',(self.source,)).fetchone())
        entries, excluded=m3u.parse(text,json.loads(source['options']))
        report=m3u.export(self.store,self.root,source,entries,excluded,dry_run=True)
        self.assertEqual(report['adopted'],1); self.assertEqual(report['added'],1) # Live playlist only.
        self.assertEqual(list(self.root.rglob('*.strm')),[original])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM m3u_file_aliases').fetchone()[0],0)

    def test_failed_and_empty_import_preserve_existing_library(self):
        self.run_import(playlist('Film (2024)'))
        file = self.root / 'Movie_VOD/Film (2024)/Film (2024).strm'
        for text in ('<html>Expired</html>', '#EXTM3U\n', playlist('New (2024)')+'#EXTINF:-1,Incomplete'):
            result = self.run_import(text)
            self.assertEqual(result['state'], 'error'); self.assertTrue(file.exists())
        m3u.request_sync(self.store, self.source)
        with patch('backend.m3u.export') as export:
            m3u.tick(self.store, self.root, fetch=lambda url: (_ for _ in ()).throw(ValueError('Provider unavailable.')))
            export.assert_not_called()

    def test_changed_and_untracked_files_are_never_overwritten_or_deleted(self):
        self.run_import(playlist('Film (2024)'))
        file = self.root / 'Movie_VOD/Film (2024)/Film (2024).strm'
        file.write_text('Manually changed')
        result = self.run_import(playlist('Film (2024)'))
        self.assertEqual(result['stats']['conflicts'], 1)
        result = self.run_import(playlist('New (2024)'))
        self.assertEqual(file.read_text(), 'Manually changed')
        other = self.root / 'Movie_VOD/Untracked (2024)/Untracked (2024).strm'
        other.parent.mkdir(parents=True); other.write_text('https://other.example/private')
        self.run_import(playlist('Untracked (2024)'))
        self.assertEqual(other.read_text(), 'https://other.example/private')

    def test_disabled_cleanup_and_large_drop_removes_all_missing_owned_files(self):
        self.run_import(playlist(*(f'Film {i} (2024)' for i in range(205))))
        result = self.run_import(playlist('Film 0 (2024)'))
        self.assertFalse(result['stats']['cleanup_held'])
        self.assertEqual(result['stats']['removed'], 204)
        m3u.save(self.store, dict(self.payload, id=self.source, clean_sync=False))
        result = self.run_import(playlist('Different (2025)'))
        self.assertEqual(result['stats']['removed'], 0)

    def test_duplicates_filters_and_traversal(self):
        entries, _ = m3u.parse(playlist('Same (2024)', 'Same (2024)', '../../Escape (2024)'), m3u.DEFAULTS)
        self.assertEqual(len({i['path'] for i in entries}), 3)
        self.assertNotIn('..', entries[2]['path'])
        with self.assertRaises(ValueError): m3u.contained(self.root, '../outside')
        with self.assertRaises(ValueError): m3u.contained(self.root, str(Path(self.temp.name) / 'outside'))
        with self.assertRaises(ValueError): m3u.parse(playlist('Film (2024)'), dict(m3u.DEFAULTS, exclude=['movie']))

    def test_private_urls_auth_csrf_paging_and_file_access(self):
        self.assertEqual(self.client.get('/guide/files/m3u.js', base_url=ORIGIN).status_code, 404)  # prototype removed
        response = self.client.get('/api/m3u', base_url=ORIGIN)
        self.assertNotIn('private/token', response.get_data(as_text=True))
        self.assertEqual(self.post(dict(self.payload, id=self.source), token='bad').status_code, 403)
        self.assertEqual(self.app.test_client().get('/api/m3u', base_url=ORIGIN).status_code, 401)
        self.run_import(playlist(*(f'Film {i} (2024)' for i in range(65))))
        page = self.client.get('/api/m3u/'+self.source+'/entries?kind=movies', base_url=ORIGIN).json
        self.assertEqual(len(page['items']), 50); self.assertTrue(page['more'])
        page2 = self.client.get('/api/m3u/'+self.source+'/entries?kind=movies&offset=50', base_url=ORIGIN).json
        self.assertEqual(len(page2['items']), 15); self.assertFalse(page2['more'])
        response = self.client.get('/api/m3u/'+self.source+'/file', query_string={'path': page['items'][0]['path']}, base_url=ORIGIN)
        self.assertEqual(response.status_code, 200); response.close()
        self.assertEqual(self.client.get('/api/m3u/'+self.source+'/file?path=../outside', base_url=ORIGIN).status_code, 404)

    def test_schedule_concurrency_and_remove_keeps_files(self):
        with patch('backend.m3u.fetch_playlist') as fetch:
            m3u.tick(self.store, self.root, fetch=fetch); fetch.assert_not_called()
        self.run_import(playlist('Film (2024)'))
        with self.store.connect() as db: db.execute("UPDATE m3u_sources SET state='running' WHERE id=?", (self.source,))
        self.assertEqual(self.post(dict(self.payload, id=self.source)).status_code, 400)
        self.assertEqual(self.post(dict(action='remove', id=self.source)).status_code, 400)
        with self.store.connect() as db: db.execute("UPDATE m3u_sources SET state='complete' WHERE id=?", (self.source,))
        self.assertEqual(self.post(dict(action='remove', id=self.source)).status_code, 200)
        self.assertTrue((self.root/'Movie_VOD/Film (2024)/Film (2024).strm').exists())

    def test_remove_owned_files_in_worker_preserves_changed_files_and_artwork(self):
        self.run_import(playlist('Film (2024)','Other (2024)'))
        files=list(self.root.rglob('*.strm'))
        self.assertEqual(len(files),2)
        changed=files[0];changed.write_text('https://example.com/changed',encoding='utf-8')
        artwork=files[1].parent/'poster.jpg';artwork.write_bytes(b'art')
        unknown=self.root/'unrecognised.strm';unknown.write_text('https://example.com/unknown',encoding='utf-8')
        result=self.post(dict(action='remove',id=self.source,delete_files=True))
        self.assertEqual(result.status_code,200);self.assertTrue(result.json['removing'])
        self.assertTrue(files[1].exists())
        with patch('backend.m3u.fetch_playlist') as fetch:
            m3u.tick(self.store,self.root,fetch=fetch);fetch.assert_not_called()
        self.assertFalse(files[1].exists())
        self.assertTrue(changed.exists());self.assertTrue(artwork.exists());self.assertTrue(unknown.exists())
        with self.store.connect() as db:
            self.assertIsNone(db.execute('SELECT id FROM m3u_sources WHERE id=?',(self.source,)).fetchone())

    def test_removal_permission_failure_can_resume(self):
        self.run_import(playlist('Film (2024)'))
        path=next(self.root.rglob('*.strm'))
        self.post(dict(action='remove',id=self.source,delete_files=True))
        with patch('backend.m3u_removal.Path.unlink',side_effect=PermissionError):m3u.tick(self.store,self.root)
        self.assertTrue(path.exists())
        with self.store.connect() as db:
            source=db.execute('SELECT * FROM m3u_sources WHERE id=?',(self.source,)).fetchone()
        self.assertEqual(source['state'],'error');self.assertFalse(json.loads(source['options'])['enabled'])
        self.assertEqual(self.post(dict(action='remove',id=self.source,delete_files=True)).status_code,200)
        m3u.tick(self.store,self.root)
        self.assertFalse(path.exists())

    def test_removal_keeps_files_shared_with_another_subscription(self):
        self.run_import(playlist('Film (2024)'))
        path=next(self.root.rglob('*.strm'))
        other=m3u.save(self.store,dict(name='Other',url='https://example.com/other'))
        with self.store.connect() as db:
            folder=path.parent.relative_to(self.root).as_posix()
            db.execute('UPDATE m3u_sources SET folder=? WHERE id=?',(folder,other))
            db.execute('INSERT INTO m3u_files VALUES(?,?,?,?,?,?)',(other,path.name,'Film','movies','',m3u.digest_file(path)))
        self.post(dict(action='remove',id=self.source,delete_files=True));m3u.tick(self.store,self.root)
        self.assertTrue(path.exists())

    def test_url_validation_and_public_network_checks(self):
        for url in ('file:///etc/passwd', 'https://a.example/\nsecret', 'https://user:pass@example.com/list'):
            with self.assertRaises(ValueError): m3u.validate_url(url)
        with patch('backend.m3u.public_addresses', side_effect=ValueError('Not public.')), patch('backend.m3u.socket.create_connection') as connect:
            with self.assertRaises(ValueError): m3u.fetch_playlist('https://localhost/playlist')
            connect.assert_not_called()

    def test_settings_validation_and_existing_root_ownership(self):
        self.assertEqual(self.post(self.payload).status_code, 400)
        for changes in ({'hours': -1}, {'hours': 169}, {'hours': float('nan')}, {'hours': float('inf')}, {'enabled': 'true'}, {'cleaners': ['bad']}, {'exclude': 'bad'}):
            self.assertEqual(self.post(dict(self.payload, id=self.source, **changes)).status_code, 400)
        result = self.post(dict(self.payload, id=self.source, url='', name='Renamed'))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(m3u.subscriptions(self.store, self.root)[0]['name'], 'Renamed')

    def test_fractional_refresh_and_zero_disable(self):
        import time
        now = time.time()
        for hours in (0.1, 0.5, 1.2, 0.01):
            with self.subTest(hours=hours), patch('backend.m3u.time.time', return_value=now):
                self.assertEqual(self.post(dict(self.payload, id=self.source, hours=hours)).status_code, 200)
                with self.store.connect() as db:
                    row = db.execute('SELECT options,next_run FROM m3u_sources WHERE id=?', (self.source,)).fetchone()
                self.assertTrue(json.loads(row['options'])['enabled'])
                self.assertEqual(json.loads(row['options'])['hours'], hours)
                self.assertAlmostEqual(row['next_run'], now + hours * 3600)
                fetch = unittest.mock.Mock(return_value=playlist('Example (2025)'))
                with patch('backend.m3u.time.time', return_value=now - 1 + hours * 3600):
                    m3u.tick(self.store, self.root, fetch=fetch)
                fetch.assert_not_called()
                with patch('backend.m3u.time.time', return_value=now + 1 + hours * 3600):
                    m3u.tick(self.store, self.root, fetch=fetch)
                fetch.assert_called_once()
        self.assertEqual(self.post(dict(self.payload, id=self.source, hours=0)).status_code, 200)
        options = m3u.subscriptions(self.store, self.root)[0]['options']
        self.assertEqual(options['hours'], 0)
        self.assertFalse(options['enabled'])
        with self.store.connect() as db: db.execute('UPDATE m3u_sources SET next_run=0')
        fetch = unittest.mock.Mock(return_value=playlist('Example (2025)'))
        m3u.tick(self.store, self.root, fetch=fetch)
        fetch.assert_not_called()
        m3u.request_sync(self.store, self.source)
        m3u.tick(self.store, self.root, fetch=fetch)
        fetch.assert_called_once()

    def test_dry_run_predicts_real_import_without_writing_library_or_schedule(self):
        self.run_import(playlist('Old Film (2023)', 'Keep Film (2024)'))
        with self.store.connect() as db:
            before = [tuple(r) for r in db.execute('SELECT * FROM m3u_files')]
            source_before = tuple(db.execute('SELECT * FROM m3u_sources').fetchone())
            history_before = db.execute('SELECT COUNT(*) FROM m3u_runs').fetchone()[0]
        files_before = {str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        text = playlist('Keep Film (2024)', 'New Film (2025)')
        self.assertEqual(self.post(dict(action='dry-run', id=self.source)).status_code, 200)
        self.assertEqual(self.post(dict(action='sync', id=self.source)).status_code, 400)
        self.assertEqual(self.post(dict(action='remove', id=self.source)).status_code, 400)
        with patch('backend.emby.queue_refresh') as scan:
            m3u.tick(self.store, self.root, fetch=lambda url:text)
            scan.assert_not_called()
        report = self.client.get('/api/m3u/'+self.source+'/dry-run',base_url=ORIGIN).json
        self.assertEqual(report['state'], 'complete')
        self.assertEqual(len(report['report']['steps']), 5)
        self.assertNotIn('https://', json.dumps(report))
        self.assertLessEqual(len(report['report']['samples']), 12)
        self.assertEqual(files_before, {str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        with self.store.connect() as db:
            self.assertEqual(before, [tuple(r) for r in db.execute('SELECT * FROM m3u_files')])
            self.assertEqual(source_before, tuple(db.execute('SELECT * FROM m3u_sources').fetchone()))
            self.assertEqual(history_before, db.execute('SELECT COUNT(*) FROM m3u_runs').fetchone()[0])
        actual = self.run_import(text)['stats']
        for key in ('added', 'updated', 'removed', 'adopted', 'conflicts'):
            self.assertEqual(report['report'][key], actual[key])

    def test_dry_run_creates_no_output_folder_and_failures_stay_read_only(self):
        m3u.request_dry_run(self.store, self.source)
        m3u.tick(self.store, self.root, fetch=lambda url:playlist('New (2024)'))
        self.assertFalse(self.root.exists())
        m3u.request_dry_run(self.store, self.source)
        m3u.tick(self.store, self.root, fetch=lambda url:'<html>Not a playlist</html>')
        result = self.client.get('/api/m3u/'+self.source+'/dry-run',base_url=ORIGIN).json
        self.assertEqual(result['state'], 'error'); self.assertFalse(self.root.exists())
        self.assertEqual(self.app.test_client().get('/api/m3u/'+self.source+'/dry-run',base_url=ORIGIN).status_code,401)
        self.assertEqual(self.post(dict(action='dry-run',id=self.source),token='bad').status_code,403)

    def test_server_presets_and_date_cleanup(self):
        for preset in ('emby', 'jellyfin', 'plex'):
            options = m3u.options_from(dict(naming=preset))
            entries, _ = m3u.parse(playlist('Example.Show S02E07 [720p]',
                'The Price Is Right 2026 09 25 1080p HEVC x265-MeGusta [1080p] [x265] [HEVC]',
                'Film.Name (2024) [1080p]'), options)
            self.assertEqual(entries[0]['path'], 'TV_VOD/Example Show/Season 02/Example Show - S02E07.strm')
            self.assertEqual(entries[1]['path'], 'TV_VOD/The Price Is Right/The Price Is Right - 2026-09-25.strm')
            self.assertEqual(entries[2]['path'], 'Movie_VOD/Film Name (2024)/Film Name (2024).strm')
            self.assertEqual(entries[1]['content'], 'https://example.com/movie/1.mp4\n')
        self.assertEqual(m3u.clean_terms('Daily Show [720p] [WEBRip] [HDTV]', ['720p', 'WEB', 'HDTV']), 'Daily Show')

    def test_confirmed_episode_matches_are_playlist_specific(self):
        options = m3u.options_from(dict(naming='emby', episode_maps=[
            dict(show='Example Daily Show', date='2026-09-25', season=55, episode=5)]))
        text = playlist('Example Daily Show 2026 09 25 [720p]')
        entries, _ = m3u.parse(text, options)
        self.assertEqual(entries[0]['path'], 'TV_VOD/Example Daily Show/Season 55/Example Daily Show - S55E05.strm')
        other, _ = m3u.parse(text, m3u.options_from(dict(naming='jellyfin')))
        self.assertEqual(other[0]['kind'], 'tv')
        invalid, _ = m3u.parse(playlist('Example Daily Show 2026 02 30'), options)
        self.assertEqual(invalid[0]['kind'], 'unsorted')

    def test_episode_mapping_validation(self):
        match = dict(show='Example Show', date='2026-09-25', season=1, episode=1)
        for maps in ('bad', [None], [dict(match, date='2026-02-30')],
                     [dict(match, season=True)], [dict(match, episode=0)], [match, match]):
            with self.assertRaises(ValueError): m3u.options_from(dict(episode_maps=maps))
        with self.assertRaises(ValueError): m3u.options_from(dict(naming='unknown'))

    def test_preview_is_unsaved_and_cannot_write_or_fetch(self):
        with self.store.connect() as db:
            before = tuple(db.execute('SELECT * FROM m3u_sources').fetchone())
        body = dict(action='preview-naming', naming='emby', titles=['Example S01E01'])
        with patch('backend.m3u.fetch_playlist') as fetch, patch('backend.m3u.export') as export:
            result = self.post(body)
            self.assertEqual(result.status_code, 200)
            self.assertIn(' - S01E01.strm', result.json['items'][0]['path'])
            fetch.assert_not_called(); export.assert_not_called()
        self.assertFalse(self.root.exists())
        with self.store.connect() as db:
            self.assertEqual(before, tuple(db.execute('SELECT * FROM m3u_sources').fetchone()))
        self.assertEqual(self.post(body, token='bad').status_code, 403)
        self.assertEqual(self.post(dict(body, titles=['a']*21)).status_code, 400)

    def test_naming_change_pauses_only_that_playlist_and_invalidates_preview(self):
        other_id = m3u.save(self.store, dict(name='Other', url='https://example.com/other'))
        m3u.request_dry_run(self.store, self.source)
        m3u.tick(self.store, self.root, fetch=lambda url:playlist('Example S01E01'))
        m3u.request_sync(self.store, self.source)
        m3u.save(self.store, dict(self.payload, id=self.source, naming='emby'))
        sources = {s['id']:s for s in m3u.subscriptions(self.store, self.root)}
        self.assertFalse(sources[self.source]['options']['enabled'])
        self.assertEqual(sources[self.source]['state'], 'idle')
        self.assertTrue(sources[other_id]['options']['enabled'])
        self.assertEqual(sources[other_id]['options']['naming'], 'legacy')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM m3u_dry_runs').fetchone()[0], 0)
        # Explicitly turning refresh back on with the reviewed rules works.
        m3u.save(self.store, dict(self.payload, id=self.source, naming='emby', enabled=True))
        self.assertTrue({s['id']:s for s in m3u.subscriptions(self.store, self.root)}[self.source]['options']['enabled'])

    def test_provider_fields_classify_yearless_movies_and_canonical_series_names(self):
        text = '''#EXTM3U
#EXTINF:-1 tvg-type="movie" tvg-id="tt38773547" group-title="Movie VOD",Worth More Than Gold [1080p] [WEBRip] [WEB]
https://example.com/1
#EXTINF:-1 tvg-type="series" tvg-id="59941" group-title="The Tonight Show Starring Jimmy Fallon",Jimmy Fallon 2026 09 24 Guest 1080p HEVC x265-MeGusta [1080p]
https://example.com/2
#EXTINF:-1 tvg-type="live" group-title="News",News 2026
https://example.com/3
#EXTINF:-1 tvg-type="series" group-title="Annual Show",Annual Show S2023E02 [720p]
https://example.com/4
'''
        options = m3u.options_from(dict(naming='emby', numeric_ids='tmdb'))
        entries, _ = m3u.parse(text, options)
        self.assertEqual(entries[0]['path'], 'Movie_VOD/Worth More Than Gold [imdbid=tt38773547]/Worth More Than Gold [imdbid=tt38773547].strm')
        self.assertEqual(entries[1]['path'], 'TV_VOD/The Tonight Show Starring Jimmy Fallon [tmdbid=59941]/The Tonight Show Starring Jimmy Fallon - 2026-09-24.strm')
        self.assertEqual(entries[2]['kind'], 'live')
        self.assertIn('Season 2023/Annual Show - S2023E02.strm', entries[3]['path'])
        self.assertEqual(entries[0]['content'], 'https://example.com/1\n')
        for preset, tag in [('jellyfin','[imdbid-tt38773547]'),('plex','{imdb-tt38773547}')]:
            result, _ = m3u.parse(text, dict(options, naming=preset))
            self.assertIn(tag, result[0]['path'])

    def test_series_group_is_only_used_when_it_extends_the_episode_title(self):
        from backend.m3u_naming import describe
        options = m3u.options_from(dict(naming='emby'))
        category = describe('The Show S01E01', '0', 'Drama', '', options, {'tvg-type': 'series'})
        self.assertEqual(category['name'], 'The Show')
        shortened = describe('Jimmy Fallon 2026 09 24 Guest', '0', 'The Tonight Show Starring Jimmy Fallon', '', options, {'tvg-type': 'series'})
        self.assertEqual(shortened['name'], 'The Tonight Show Starring Jimmy Fallon')
        same = describe('Annual Show S2023E02', '0', 'Annual Show', '', options, {'tvg-type': 'series'})
        self.assertEqual(same['name'], 'Annual Show')
        text = '''#EXTM3U
#EXTINF:0 tvg-type="series" group-title="Drama",The Show S01E01
https://example.com/series/1
'''
        entries, _ = m3u.parse(text, options)
        self.assertIn('TV_VOD/The Show/', entries[0]['path'])
        self.assertNotIn('/Drama/', entries[0]['path'])

    def test_cleanup_preserves_real_title_text_and_handles_movie_titles_containing_years(self):
        text = '''#EXTM3U
#EXTINF:0 tvg-type="movie",2001 A Space Odyssey (1968) [1080p] [WEB]
https://example.com/1
#EXTINF:0 tvg-type="movie",1917 [1080p]
https://example.com/2
#EXTINF:0 tvg-type="movie",Web of Lies [The Alphabet] [720p] [HEVC]
https://example.com/3
#EXTINF:0 tvg-type="series",Example S01E02E03 [720p]
https://example.com/4
'''
        entries, _ = m3u.parse(text, m3u.options_from(dict(naming='emby')))
        self.assertEqual(entries[0]['title'], '2001 A Space Odyssey (1968)')
        self.assertEqual(entries[1]['title'], '1917')
        self.assertIsNone(entries[1]['info']['year'])
        self.assertEqual(entries[2]['title'], 'Web of Lies [The Alphabet]')
        self.assertIn('S01E02E03', entries[3]['path'])

    def test_auto_numeric_ids_verify_title_year_and_media_type(self):
        client = m3u_metadata.TMDB(self.store)
        info = dict(kind='movies', provider='auto', identity='42', name='Example Film', year=2025)
        for item in ({'id':42,'title':'Unrelated Film','release_date':'2025-01-01'},
                     {'id':42,'title':'Example Film','release_date':'1999-01-01'},
                     {'id':42,'name':'Example Film','first_air_date':'2025-01-01'}):
            with patch.object(client, 'get', return_value=item):
                self.assertEqual(client.detect(info), {})
        correct = {'id':42,'title':'EXAMPLE: Film','release_date':'2025-01-01'}
        with patch.object(client, 'get', return_value=correct) as fetch:
            self.assertEqual(client.detect(info), correct)
            fetch.assert_called_once_with('movie/42')

    def test_auto_tv_ids_reject_ambiguous_sources(self):
        client = m3u_metadata.TMDB(self.store)
        info = dict(kind='series', provider='auto', identity='42', name='Example Show', year=None)
        direct = {'id':42,'name':'Example Show'}
        external = {'id':88,'name':'Example Show'}
        with patch.object(client, 'get', side_effect=[direct, {'tv_results':[external]}]):
            self.assertEqual(client.detect(info), {})
        with patch.object(client, 'get', side_effect=[{'id':42,'name':'Wrong Show'}, {'tv_results':[external]}, external]):
            self.assertEqual(client.detect(info)['id'], 88)

    def test_auto_ids_stay_out_of_filenames_until_verified(self):
        from backend.m3u_naming import apply_match, match_key, paths
        options = m3u.options_from(dict(naming='emby', numeric_ids='auto'))
        text = '#EXTM3U\n#EXTINF:0 tvg-type="movie" tvg-id="42",Example Film (2025)\nhttps://example.com/movie/1\n'
        entries, _ = m3u.parse(text, options)
        info = entries[0]['info']
        self.assertNotIn('auto', entries[0]['path'])
        self.assertNotIn('tmdbid', entries[0]['path'])
        key = match_key(info)
        other = dict(info, name='Unrelated Film')
        self.assertNotEqual(key, match_key(other))
        resolved = apply_match(info, {key:dict(provider='tmdb', identity='42')})
        self.assertIn('[tmdbid=42]', paths(resolved, 'emby')[2])
        self.assertEqual(apply_match(other, {key:dict(provider='tmdb', identity='42')})['provider'], 'auto')
        m3u.save(self.store, dict(self.payload, id=self.source, naming='emby', numeric_ids='auto'))
        self.store.set('m3u_tmdb_token', 'fixture-token-for-unit-tests')
        m3u_metadata.request(self.store, self.source)
        with patch.object(m3u_metadata.TMDB, 'get', return_value={'id':42,'title':'Example Film','release_date':'2025-01-01'}):
            m3u_metadata.tick(self.store, lambda url:text)
        matches = m3u_metadata.matches(self.store, self.source)
        self.assertEqual(matches[key]['provider'], 'tmdb')
        self.assertEqual(matches[key]['identity'], '42')
        self.assertFalse(self.root.exists())

    def test_repeated_auto_matching_can_fill_newly_available_episode(self):
        from backend.m3u_naming import match_key
        options = dict(self.payload, id=self.source, naming='emby', numeric_ids='auto')
        m3u.save(self.store, options)
        self.store.set('m3u_tmdb_token', 'fixture-token-for-unit-tests')
        text = '#EXTM3U\n#EXTINF:0 tvg-type="series" tvg-id="42",Example Show 2026 09 25\nhttps://example.com/series/1\n'
        raw, _ = m3u.parse(text, m3u.options_from(options))
        key = match_key(raw[0]['info'])
        for episodes in ({}, {'2026-09-25':dict(season=2, episode=5)}):
            m3u_metadata.request(self.store, self.source)
            with patch.object(m3u_metadata.TMDB, 'detect', return_value={'id':88,'name':'Example Show'}), patch.object(m3u_metadata.TMDB, 'episodes', return_value=episodes):
                m3u_metadata.tick(self.store, lambda url:text)
        stored = m3u_metadata.matches(self.store, self.source)
        self.assertEqual(list(stored), [key])
        self.assertEqual(stored[key]['episode'], 5)
        entries, _ = m3u.parse(text, m3u.options_from(options), stored)
        self.assertIn('S02E05', entries[0]['path'])
        self.assertIn('[tmdbid=88]', entries[0]['path'])
        self.assertFalse(self.root.exists())

    def metadata_fixture(self):
        m3u.save(self.store, dict(self.payload, id=self.source, naming='emby', numeric_ids='tmdb'))
        self.store.set('m3u_tmdb_token', 'fixture-token-for-unit-tests')
        return '''#EXTM3U
#EXTINF:0 tvg-type="movie" tvg-id="tt12345",Missing Year [1080p]
https://example.com/movie/1
#EXTINF:0 tvg-type="series" tvg-id="2051" group-title="Daily Show",Daily Show 2026 09 25 [HEVC]
https://example.com/series/2
'''

    def test_optional_metadata_task_is_read_only_and_results_feed_dry_run(self):
        text = self.metadata_fixture()
        m3u_metadata.request(self.store, self.source)
        for action in ('sync', 'dry-run', 'remove'):
            self.assertEqual(self.post(dict(action=action,id=self.source)).status_code, 400)
        with patch.object(m3u_metadata.TMDB, 'movie', return_value={'year':2024}), patch.object(m3u_metadata.TMDB, 'episodes', return_value={'2026-09-25':{'season':55,'episode':5}}), patch('backend.emby.queue_refresh') as scan:
            m3u.tick(self.store, self.root, fetch=lambda url:text)
            scan.assert_not_called()
        self.assertFalse(self.root.exists())
        result = self.client.get('/api/m3u/'+self.source+'/metadata-status',base_url=ORIGIN).json
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['report']['matched'], 2)
        self.assertNotIn('https://', json.dumps(result))
        m3u.request_dry_run(self.store, self.source)
        with patch('requests.get') as network:
            m3u.tick(self.store, self.root, fetch=lambda url:text)
            network.assert_not_called()
        self.assertFalse(self.root.exists())
        report = self.client.get('/api/m3u/'+self.source+'/dry-run',base_url=ORIGIN).json['report']
        self.assertEqual(report['metadata_matched'],2)
        paths = [s['path'] for s in report['samples']]
        self.assertTrue(any('Missing Year (2024)' in p for p in paths))
        self.assertTrue(any('Season 55/Daily Show - S55E05' in p for p in paths))
        self.assertFalse(m3u.subscriptions(self.store,self.root)[0]['options']['enabled'])

    def test_tmdb_credentials_are_private_and_task_failures_preserve_files(self):
        token='a'*32
        self.assertEqual(self.post(dict(action='metadata-settings',token=token),token='bad').status_code,403)
        self.assertEqual(self.post(dict(action='metadata-settings',token=token)).status_code,200)
        self.assertNotIn(token,self.client.get('/api/m3u',base_url=ORIGIN).get_data(as_text=True))
        self.run_import(playlist('Existing (2024)'))
        before={str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        text=self.metadata_fixture();m3u_metadata.request(self.store,self.source)
        with patch.object(m3u_metadata.TMDB,'movie',side_effect=ValueError('TMDB rejected the API credential.')):
            m3u.tick(self.store,self.root,fetch=lambda url:text)
        result=self.client.get('/api/m3u/'+self.source+'/metadata-status',base_url=ORIGIN).json
        self.assertEqual(result['state'],'error')
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertEqual(self.app.test_client().get('/api/m3u/'+self.source+'/metadata-status',base_url=ORIGIN).status_code,401)
        self.assertEqual(self.post(dict(action='metadata-settings',clear=True)).status_code,200)
        self.assertFalse(self.store.get('m3u_tmdb_token'))

    def test_tmdb_uses_exact_ids_and_rejects_ambiguous_air_dates(self):
        self.metadata_fixture();client=m3u_metadata.TMDB(self.store)
        def fake(path, **params):
            if path=='find/tt12345':
                self.assertEqual(params,{'external_source':'imdb_id'})
                return {'movie_results':[{'release_date':'2024-03-12'}]}
            if path=='tv/2051':return {'id':2051,'seasons':[{'season_number':1},{'season_number':0}]}
            if path=='tv/2051/season/1':return {'episodes':[{'air_date':'2026-09-25','episode_number':5},{'air_date':'2026-09-26','episode_number':6}]}
            if path=='tv/2051/season/0':return {'episodes':[{'air_date':'2026-09-25','episode_number':1}]}
            self.fail(path)
        with patch.object(client,'get',side_effect=fake):
            self.assertEqual(client.movie(dict(kind='movies',provider='imdb',identity='tt12345')),{'year':2024})
            result=client.episodes(dict(kind='tv',provider='tmdb',identity='2051'))
            self.assertNotIn('2026-09-25',result)
            self.assertEqual(result['2026-09-26'],dict(season=1,episode=6))

    def test_large_removal_dry_run_matches_real_cleanup_and_preserves_artwork(self):
        self.run_import(playlist(*(f'Film {i} (2024)' for i in range(205))))
        artwork=self.root/'Movie_VOD/Film 1 (2024)/poster.jpg';artwork.write_bytes(b'artwork')
        untracked=self.root/'untracked.strm';untracked.write_text('private stream')
        m3u.request_dry_run(self.store,self.source)
        m3u.tick(self.store,self.root,fetch=lambda url:playlist('Film 0 (2024)'))
        report=self.client.get('/api/m3u/'+self.source+'/dry-run',base_url=ORIGIN).json['report']
        self.assertEqual(report['removed'],204)
        self.assertEqual(len(list(self.root.rglob('*.strm'))),206)
        result=self.run_import(playlist('Film 0 (2024)'))
        self.assertEqual(result['stats']['removed'],204)
        self.assertTrue(artwork.exists());self.assertTrue(untracked.exists())


if __name__ == '__main__': unittest.main()
