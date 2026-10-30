import json
import tempfile
import unittest
from pathlib import Path
from flask import Flask

from backend import reset, storage_locations
from backend.app import create_app


class StorageConfigurationTests(unittest.TestCase):
    def app(self, **config):
        app = Flask(__name__)
        app.config.update(REELDOCK_MEDIA_ROOT='', REELDOCK_STORAGE_ROOTS='[]',
                          REELDOCK_STORAGE_LOCATIONS='[]', REELDOCK_HOST_ROOT='', **config)
        return app

    def test_exact_fixed_mount_and_valid_extra_mounts_preserve_host_mapping(self):
        app = self.app()
        app.config.update(REELDOCK_MEDIA_ROOT='/reeldock', REELDOCK_HOST_ROOT='/host/media',
                          REELDOCK_STORAGE_ROOTS='["/extra", "/reeldock", "/extra"]',
                          REELDOCK_STORAGE_LOCATIONS='[{"path":"/extra","host_path":"/host/extra","label":"Extra","select_root":false}]')
        self.assertEqual(storage_locations.roots(app), ['/extra', '/reeldock'])
        rows = storage_locations.locations(app)
        self.assertEqual(rows[0], dict(path='/extra', host_path='/host/extra', label='Extra', select_root=False))
        self.assertEqual(storage_locations.host_path(app, '/reeldock/Auto-Download'), '/host/media/Auto-Download')
        self.assertEqual(storage_locations.configuration_warnings(app), [])
        app.config['REELDOCK_STORAGE_ROOTS'] = ''
        self.assertEqual(storage_locations.roots(app), ['/reeldock'])

    def test_malformed_or_invalid_roots_disable_all_extra_paths_without_echoing_values(self):
        app = self.app()
        app.config['REELDOCK_MEDIA_ROOT'] = '/reeldock'
        cases = ['[/reeldock]', '/reeldock', '{}', 'null', '123', '"/extra"',
                 '["/extra", 2]', '["/extra", "/"]', '["//"]', '["///"]', '["/private-imported-invalid/../escape"]',
                 json.dumps(['/extra'] * (storage_locations.MAX_LOCATIONS + 1)),
                 'x' * (storage_locations.MAX_CONFIG_BYTES + 1)]
        for raw in cases:
            with self.subTest(length=len(raw)):
                app.config['REELDOCK_STORAGE_ROOTS'] = raw
                with self.assertLogs('backend.storage_locations', level='WARNING') as logs:
                    self.assertEqual(storage_locations.roots(app), ['/reeldock'])
                self.assertEqual(len(logs.output), 1)
                self.assertIn('REELDOCK_STORAGE_ROOTS', logs.output[0])
                self.assertNotIn('/private-imported-invalid', logs.output[0])
                self.assertTrue(storage_locations.configuration_warnings(app))

    def test_invalid_roots_without_fixed_mount_authorize_no_location(self):
        app = self.app()
        app.config.update(REELDOCK_STORAGE_ROOTS='[/untrusted-extra]',
                          REELDOCK_STORAGE_LOCATIONS='[{"path":"/untrusted-extra","host_path":"/host/extra"}]')
        with self.assertLogs('backend.storage_locations', level='WARNING'):
            self.assertEqual(storage_locations.roots(app), [])
        self.assertEqual(storage_locations.locations(app), [])
        with self.assertRaisesRegex(ValueError, 'not available'):
            storage_locations.resolve(app, '/host/extra/file')
        for fixed in ('/', '//', '///'):
            app.config.update(REELDOCK_MEDIA_ROOT=fixed, REELDOCK_STORAGE_ROOTS='["/extra"]')
            with self.assertLogs('backend.storage_locations', level='WARNING'):
                self.assertEqual(storage_locations.roots(app), [])

    def test_malformed_location_metadata_is_not_an_access_allowlist(self):
        app = self.app()
        app.config.update(REELDOCK_MEDIA_ROOT='/reeldock', REELDOCK_HOST_ROOT='/host/media')
        for raw in ('[/private-mapping]', '{}', '[null]', '[{"path":"/reeldock","host_path":"../escape"}]',
                    '[{"path":"/reeldock","select_root":"false"}]', '[{"path":"/reeldock","host_path":null}]'):
            app.config['REELDOCK_STORAGE_LOCATIONS'] = raw
            with self.assertLogs('backend.storage_locations', level='WARNING'):
                self.assertEqual(storage_locations.locations(app), [dict(path='/reeldock', host_path='/host/media', label='/host/media', select_root=False)])
        app.config['REELDOCK_STORAGE_LOCATIONS'] = '[{"path":"/outside","host_path":"/host/outside"}]'
        self.assertEqual([row['path'] for row in storage_locations.locations(app)], ['/reeldock'])

    def test_invalid_mapping_cannot_drop_a_whole_mount_selection_restriction(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            media, data = root / 'media', root / 'data'
            media.mkdir()
            data.mkdir()
            app = self.app()
            app.config.update(DATA_DIR=str(data), REELDOCK_STORAGE_ROOTS=json.dumps([str(media)]),
                              REELDOCK_STORAGE_LOCATIONS='[malformed]')
            with self.assertLogs('backend.storage_locations', level='WARNING'):
                locations = storage_locations.locations(app)
            self.assertFalse(locations[0]['select_root'])
            with self.assertRaisesRegex(ValueError, 'inside this location'):
                storage_locations.validate_choice(app, None, str(media))
            app.config['REELDOCK_STORAGE_LOCATIONS'] = '[]'
            self.assertTrue(storage_locations.locations(app)[0]['select_root'])
            self.assertEqual(storage_locations.configuration_warnings(app), [])

    def test_warnings_coalesce_and_clear_after_configuration_is_corrected(self):
        app = self.app()
        app.config['REELDOCK_STORAGE_ROOTS'] = '[/bad]'
        with self.assertLogs('backend.storage_locations', level='WARNING') as logs:
            for _ in range(4):
                storage_locations.roots(app)
        self.assertEqual(len(logs.output), 1)
        app.config['REELDOCK_STORAGE_ROOTS'] = '[]'
        self.assertEqual(storage_locations.roots(app), [])
        self.assertEqual(storage_locations.configuration_warnings(app), [])
        app.config['REELDOCK_STORAGE_ROOTS'] = '[/bad]'
        with self.assertLogs('backend.storage_locations', level='WARNING'):
            storage_locations.roots(app)

    def test_valid_mount_translation_retains_symlink_and_data_boundaries(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            media, outside, data = (root / name for name in ('media', 'outside', 'data'))
            for path in (media, outside, data):
                path.mkdir()
            (media / 'alias').symlink_to(outside, target_is_directory=True)
            app = self.app()
            app.config.update(DATA_DIR=str(data), REELDOCK_STORAGE_ROOTS=json.dumps([str(media)]),
                              REELDOCK_STORAGE_LOCATIONS=json.dumps([dict(path=str(media), host_path='/host/media')]))
            self.assertEqual(storage_locations.resolve(app, '/host/media/new-folder')[0], media / 'new-folder')
            with self.assertRaises(ValueError):
                storage_locations.resolve(app, '/host/media/alias')
            with self.assertRaises(ValueError):
                storage_locations.resolve(app, str(data))


class MalformedCasaOsRouteTests(unittest.TestCase):
    def test_fixed_fresh_general_and_activity_remain_json_with_imported_malformed_roots(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            media = root / 'media'
            media.mkdir()
            app = create_app(dict(TESTING=True, DATA_DIR=str(root / 'data'), REELDOCK_MEDIA_ROOT=str(media),
                                  REELDOCK_HOST_ROOT='/host/media', REELDOCK_STORAGE_ROOTS='[' + str(media) + ']',
                                  LOCAL_SETUP_URL='http://127.0.0.1:8821', APP_PORT='8821'))
            store = app.extensions['store']
            with store.connect() as db:
                db.execute('INSERT INTO users VALUES(1,?,?)', ('fixture', 'unused-fixture-hash'))
            store.set('setup_complete', '1')
            store.set('public_origin', 'https://reeldock.example.test')
            client = app.test_client()
            with client.session_transaction(base_url='https://reeldock.example.test') as session:
                session.update(user_id=1, auth_revision='0')
            with self.assertLogs('backend.storage_locations', level='WARNING') as logs:
                response = client.get('/api/downloads', base_url='https://reeldock.example.test')
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.is_json)
            self.assertIn('pause_downloads', response.json['options'])
            self.assertEqual(response.json['items'], [])
            self.assertNotIn(str(media), '\n'.join(logs.output))
            self.assertEqual(client.get('/api/activity', base_url='https://reeldock.example.test').status_code, 200)
            info = media.stat()
            self.assertIn((info.st_dev, info.st_ino), reset.mount_inodes(app))
            self.assertEqual(storage_locations.roots(app), [str(media)])

    def test_nonfixed_reset_cannot_adopt_a_saved_base_when_its_allowlist_is_invalid(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            media = root / 'media'
            media.mkdir()
            app = create_app(dict(TESTING=True, DATA_DIR=str(root / 'data'), MEDIA_DIR=str(media / 'youtube'),
                                  M3U_DIR=str(media / 'links'), REELDOCK_STORAGE_ROOTS='[/invalid]',
                                  LOCAL_SETUP_URL='http://127.0.0.1:8821', APP_PORT='8821'))
            store = app.extensions['store']
            store.set('storage_base', str(media))
            with self.assertLogs('backend.storage_locations', level='WARNING'), self.assertRaisesRegex(ValueError, 'outside'):
                reset.targets(app, store)
            self.assertTrue(media.is_dir())


if __name__ == '__main__':
    unittest.main()
