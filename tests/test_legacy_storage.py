"""Upgraded NAS installations retain separate media mounts and no storage_base."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app import create_app
from backend import auto_downloads, setup_storage, storage_layout
from backend.download_options import save


class LegacyStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root / 'library'
        self.base.mkdir()
        self.youtube = self.root / 'media-alias'
        self.youtube.mkdir()
        self.links = self.root / 'm3u-alias'
        self.links.mkdir()
        self.app = create_app(dict(TESTING=True, DATA_DIR=str(self.root / 'data'),
            MEDIA_DIR=str(self.youtube), M3U_DIR=str(self.links),
            REELDOCK_DOWNLOAD_BASE=str(self.base),
            REELDOCK_STORAGE_ROOTS=json.dumps([str(self.base), str(self.youtube), str(self.links)]),
            REELDOCK_STORAGE_LOCATIONS=json.dumps([
                dict(path=str(self.base), host_path='/nas/ReelDock'),
                dict(path=str(self.youtube), host_path='/nas/ReelDock/YouTube'),
                dict(path=str(self.links), host_path='/nas/ReelDock/M3U')]),
            LOCAL_SETUP_URL='http://127.0.0.1:8790', APP_PORT='8790'))
        self.store = self.app.extensions['store']
        self.store.set('setup_complete', '1')

    def test_reports_real_storage_without_adopting_library(self):
        state = setup_storage.state(self.app, self.store)
        self.assertFalse(state['configured'])
        self.assertTrue(state['writable'])
        self.assertIsInstance(state['free_gb'], float)
        self.assertEqual(state['storage_path'], '/nas/ReelDock')
        self.assertTrue(state['can_choose_auto_folder'])
        storage_layout.apply(self.app, self.store)
        self.assertEqual(self.app.config['MEDIA_DIR'], str(self.youtube))
        self.assertEqual(self.app.config['M3U_DIR'], str(self.links))
        self.assertFalse(self.store.get('storage_base'))

    def test_original_legacy_install_reports_media_disk_not_working_directory(self):
        self.store.legacy_download_base = ''
        self.app.config['REELDOCK_STORAGE_ROOTS'] = '[]'
        with patch('backend.setup_storage.probe', wraps=setup_storage.probe) as probe:
            state = setup_storage.state(self.app, self.store)
        probe.assert_called_once_with(str(self.youtube))
        self.assertTrue(state['writable'])
        self.assertFalse(state['can_choose_auto_folder'])
        self.assertFalse(setup_storage.probe('')['writable'])

    def test_drop_folder_saved_and_library_aliases_rejected(self):
        auto_downloads.save_folder(self.app, self.store, '/nas/ReelDock/Auto Downloads')
        watch = self.base / 'Auto Downloads'
        self.assertEqual(auto_downloads.watch_folder(self.app, self.store), watch)
        for name in ('YouTube', 'M3U', 'Movies', 'TV Shows', 'Downloads'):
            folder = self.base / name
            folder.mkdir()
            (folder / 'child').mkdir()
            for path in (folder, folder / 'child'):
                with self.assertRaises(ValueError):
                    auto_downloads.validate_watch(self.app, self.store, str(path))
        for path in (self.base, self.youtube, self.links, self.root / 'data'):
            with self.assertRaises(ValueError):
                auto_downloads.validate_watch(self.app, self.store, str(path))
        self.assertFalse(self.store.get('storage_base'))

    def test_legacy_strm_drop_downloads_to_existing_movie_folder(self):
        import os, time
        auto_downloads.save_folder(self.app, self.store, '/nas/ReelDock/Auto Downloads')
        save(self.store, {'auto_downloads': True})
        movie = self.base / 'Movies' / 'Example (2025)'
        movie.mkdir(parents=True)
        source = self.base / 'Auto Downloads' / 'Example.2025.strm'
        source.write_text('https://example.com/movie.mp4\n')
        os.utime(source, (time.time() - 30, time.time() - 30))
        auto_downloads.scan(self.app, self.store, threading.Event(), {'strm'},
                            fetch=lambda url: ('.mp4', 5, [b'video']))
        self.assertEqual(len(list(movie.rglob('*.mp4'))), 1)
        self.assertFalse(source.exists())
        self.assertFalse(self.store.get('storage_base'))

    def test_both_engines_use_explicit_download_mount(self):
        from backend import torrents, nzb
        for module, cls in ((torrents, 'TorrentEngine'), (nzb, 'NzbEngine')):
            with self.subTest(engine=cls), patch.object(module, 'Operations'), patch.object(module, cls) as engine, patch.object(module, '_attach', side_effect=lambda e, *_: e):
                module._engine(self.app, self.store)
                self.assertEqual(engine.call_args.args[0], self.base)
        self.assertFalse(self.store.get('storage_base'))

    def test_discover_recognises_completed_legacy_download(self):
        from backend.discover_acquire import saved_paths
        target = self.base / 'Movies' / 'Example.mp4'
        target.parent.mkdir()
        target.write_bytes(b'video')
        self.assertEqual(saved_paths(self.store, ['Movies/Example.mp4']), [str(target)])
