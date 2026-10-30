"""Synthetic filesystem identities; no mounts, library walks or owner state."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import system_stats


class SystemStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / 'data'
        self.media = self.root / 'media'
        self.nested = self.media / 'second drive'
        self.alias = self.media / 'alias'
        self.other = self.root / 'media-other'
        for path in (self.data, self.media, self.nested, self.alias, self.other):
            path.mkdir(parents=True, exist_ok=True)
        self.store = SimpleNamespace(directory=self.data)
        self.app = SimpleNamespace(extensions={}, config=dict(REELDOCK_STORAGE_ROOTS=json.dumps([str(self.media)]),
            REELDOCK_STORAGE_LOCATIONS=json.dumps([dict(path=str(self.media), host_path='/Media Drive')])) )

    def sample(self, devices, mounts=(), failures=(), app=None):
        real_stat = os.stat
        self.probed = []
        self.measured = []
        def info(path, *args, **kwargs):
            path = Path(path)
            self.probed.append(path)
            if path in devices:
                if devices[path] is None:
                    raise FileNotFoundError(str(path))
                return SimpleNamespace(st_dev=devices[path])
            return real_stat(path, *args, **kwargs)
        def usage(path):
            path = Path(path)
            self.measured.append(path)
            self.assertIn(path, devices, 'Never measure an unrelated filesystem.')
            if path in failures:
                raise PermissionError(str(path))
            # Deliberately equal capacities across distinct filesystem IDs.
            return SimpleNamespace(total=2_000_000_000_000, used=500_000_000_000, free=1_500_000_000_000)
        with patch.object(system_stats, '_mount_points', return_value=(list(mounts), False)), \
                patch.object(system_stats.os, 'stat', side_effect=info), \
                patch.object(system_stats.shutil, 'disk_usage', side_effect=usage), \
                patch.object(os, 'walk', side_effect=AssertionError('No library walk')), \
                patch.object(os, 'scandir', side_effect=AssertionError('No directory scan')):
            return system_stats.storage_snapshot(self.store, str(self.media), app)

    def test_same_filesystem_merges_application_data_media_and_aliases(self):
        result = self.sample({self.data: 1, self.media: 1, self.alias: 1}, [self.alias])
        self.assertEqual(len(result['storage_disks']), 1)
        row = result['storage_disks'][0]
        self.assertEqual(row['label'], 'Application data · Media')
        self.assertEqual(row['paths'], [str(self.data), str(self.media), str(self.alias)])
        self.assertEqual(self.measured, [self.data])

    def test_nested_distinct_filesystem_is_included_and_bind_alias_not_duplicated(self):
        result = self.sample({self.data: 1, self.media: 1, self.nested: 2, self.alias: 2},
                             [self.nested, self.alias, self.other, Path('/etc/hosts')], app=self.app)
        rows = result['storage_disks']
        self.assertEqual(len(rows), 2, 'Equal capacity does not collapse two filesystems.')
        self.assertEqual(rows[1]['paths'], ['/Media Drive/second drive', '/Media Drive/alias'])
        self.assertEqual(rows[1]['label'], 'Media')
        self.assertEqual(self.measured, [self.data, self.nested])
        self.assertNotIn(self.other, self.probed)
        self.assertNotIn(Path('/etc/hosts'), self.probed)

    def test_separate_data_and_media_filesystems_have_independent_rows(self):
        rows = self.sample({self.data: 1, self.media: 2})['storage_disks']
        self.assertEqual([row['label'] for row in rows], ['Application data', 'Media'])
        self.assertEqual([row['id'] for row in rows], ['filesystem:1', 'filesystem:2'])

    def test_unavailable_path_does_not_hide_healthy_filesystem_or_duplicate_known_alias(self):
        rows = self.sample({self.data: 1, self.media: 2, self.alias: 2, self.nested: None},
                           [self.alias, self.nested], failures=[self.media])['storage_disks']
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]['paths'], [str(self.media), str(self.alias)])
        self.assertIsNotNone(rows[1]['total'], 'An accessible alias can report the shared filesystem.')
        self.assertIsNone(rows[2]['total'])
        self.assertIsNone(rows[2]['free'])

    def test_bad_config_and_candidate_limit_report_incomplete_instead_of_failing(self):
        app = SimpleNamespace(extensions={}, config={'REELDOCK_STORAGE_ROOTS': '{invalid'})
        result = self.sample({self.data: 1, self.media: 2}, app=app)
        self.assertTrue(result['storage_disks_incomplete'])
        self.assertEqual(len(result['storage_disks']), 2)
        app.config['M3U_DOWNLOAD_ROOTS'] = json.dumps(['/unsafe/../root', '/bad\0path', 7])
        self.assertEqual(len(self.sample({self.data: 1, self.media: 2}, app=app)['storage_disks']), 2)
        with patch.object(system_stats, 'MAX_STORAGE_PATHS', 2):
            result = self.sample({self.data: 1, self.media: 2}, [self.nested])
        self.assertTrue(result['storage_disks_incomplete'])
        self.assertNotIn(self.nested, self.probed)

    def test_mount_table_decodes_escaped_paths_and_is_bounded(self):
        table = self.root / 'mountinfo'
        table.write_text('1 0 8:1 / /media\\040drive rw - ext4 /dev/test rw\n'
                         '2 0 8:2 / /media\\134alias rw - ext4 /dev/test2 rw\ninvalid\n')
        with patch.object(system_stats, 'MOUNTINFO', table):
            self.assertEqual(system_stats._mount_points(), ([Path('/media drive'), Path('/media\\alias')], False))
            with patch.object(system_stats, 'MAX_MOUNT_BYTES', 8):
                self.assertEqual(system_stats._mount_points(), ([], True))
        with patch.object(system_stats, 'MOUNTINFO', self.root / 'missing'):
            self.assertEqual(system_stats._mount_points(), ([], True))


if __name__ == '__main__':
    unittest.main()
