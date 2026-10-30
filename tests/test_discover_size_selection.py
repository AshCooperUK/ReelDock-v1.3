"""Catalogue selection regressions; all submissions are mocked, no provider traffic."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import discover_acquire as acquire, discover_store
from backend.storage import Store
from backend.settings.nzb import selection_limits

GB = 1_000_000_000


class DiscoverSizeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        discover_store.initialise(self.store)
        self.settings = Path(self.temp.name) / 'nzb-engine' / 'settings.json'
        self.settings.parent.mkdir()
        self.settings.write_text(json.dumps({'min_download_bytes': GB, 'max_download_bytes': 8 * GB}))
        self.row, _ = discover_store.insert_request(self.store, dict(
            idempotency='size-selection-fixture', kind='movie', title='Fixture',
            quality='1080', fallback=False, episode_key='movie:1234567',
            body={'externalId': '1234567', 'source': 'NZB first'}))

    def release(self, name, size, **extra):
        return dict(id=name, externalId='1234567', quality='1080p', title=name, size=size, **extra)

    def resolve(self, releases, submit):
        with patch.object(acquire, 'submit_nzb', side_effect=submit):
            acquire.resolve_release(None, self.store, self.row, releases)

    def state(self):
        with self.store.connect() as db:
            return db.execute('SELECT state FROM discover_requests WHERE id=?', (self.row['id'],)).fetchone()[0]

    def test_oversized_top_five_do_not_hide_smaller_release(self):
        calls = []
        releases = [self.release(str(i), (40-i)*GB) for i in range(5)]
        releases += [self.release('eligible', 6*GB)]
        self.resolve(releases, lambda _app, _store, release, *_: calls.append(release['id']))
        self.assertEqual(calls, ['eligible'])
        with self.store.connect() as db:
            states = [r[0] for r in db.execute('SELECT state FROM discover_attempts ORDER BY id')]
        self.assertEqual(states, ['size-filtered']*5 + ['queued'])

    def test_manifest_rejections_continue_beyond_fifth_candidate(self):
        calls = []
        def submit(_app, _store, release, *_):
            calls.append(release['id'])
            if release['id'] != '6':
                raise acquire.CandidateError('Fixture manifest rejected')
        self.resolve([self.release(str(i), 7*GB-i) for i in range(7)], submit)
        self.assertEqual(calls, [str(i) for i in range(7)])

    def test_boundaries_unknown_sizes_and_quality_identity_filters(self):
        releases = [self.release('small', GB-1), self.release('large', 8*GB+1),
                    self.release('minimum', GB), self.release('maximum', 8*GB),
                    self.release('unknown', 0)]
        releases += [dict(self.release('wrong-id', 4*GB), externalId='999'),
                     dict(self.release('wrong-quality', 4*GB), quality='2160p')]
        calls = []
        def submit(_app, _store, release, *_):
            calls.append(release['id'])
            raise acquire.CandidateError('Fixture rejected')
        self.resolve(releases, submit)
        self.assertEqual(calls, ['maximum', 'minimum', 'unknown'])

    def test_rate_limit_stops_further_requests(self):
        calls = []
        def submit(_app, _store, release, *_):
            calls.append(release['id'])
            raise RuntimeError('HTTP 429')
        self.resolve([self.release(str(i), 6*GB) for i in range(8)], submit)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.state(), 'waiting')

    def test_cancellation_stops_remaining_candidates(self):
        calls = []
        def submit(_app, _store, release, *_):
            calls.append(release['id'])
            discover_store.cancel_request(self.store, self.row['id'])
            raise acquire.CandidateError('Fixture rejected')
        self.resolve([self.release(str(i), 6*GB) for i in range(8)], submit)
        self.assertEqual(len(calls), 1)
        self.assertIn(self.state(), ('cancelled', 'cancelling'))

    def test_limits_read_fresh_and_legacy_defaults(self):
        self.assertEqual(selection_limits(self.store), (GB, 8*GB))
        self.settings.write_text('{}')
        self.assertEqual(selection_limits(self.store), (0, 100*GB))
        self.settings.write_text('{"min_download_bytes": 10, "max_download_bytes": 2}')
        with self.assertRaises(ValueError):
            selection_limits(self.store)

    def test_transfer_retries_have_no_five_attempt_cap_and_skip_old_grabs(self):
        for i in range(7):
            discover_store.add_attempt(self.store, self.row['id'], str(i), 'nzb', '1080p', 'failed', 'Incomplete articles')
        discover_store.add_attempt(self.store, self.row['id'], 'old-grab', 'nzb', '1080p', 'queued', 'Accepted')
        self.assertTrue(acquire._retry_candidate(self.store, self.row, 'Incomplete articles'))
        calls = []
        self.resolve([self.release('old-grab', 7*GB), self.release('fresh', 6*GB)],
                     lambda _app, _store, release, *_: calls.append(release['id']))
        self.assertEqual(calls, ['fresh'])


if __name__ == '__main__':
    unittest.main()
