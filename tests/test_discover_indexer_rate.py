import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from backend import discover_indexer_rate as rate,discover_sources as sources


class IndexerRateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=SimpleNamespace(directory=self.tmp.name)
        self.indexer=dict(url='https://example.invalid',api_key='private-secret')
        self.now=1000.
        self.clock=patch.object(rate.time,'time',side_effect=lambda:self.now);self.clock.start();self.addCleanup(self.clock.stop)
        self.sleep=patch.object(rate.time,'sleep',side_effect=self.advance);self.sleep.start();self.addCleanup(self.sleep.stop)
    def advance(self,seconds):self.now+=seconds

    def test_separate_callers_share_pacing_without_storing_account_secrets(self):
        with rate.request_slot(self.store,self.indexer):pass
        with rate.request_slot(SimpleNamespace(directory=self.tmp.name),self.indexer):pass
        self.assertEqual(self.now,1001)
        for path in Path(self.tmp.name).rglob('*'):
            self.assertNotIn('private-secret',str(path))
            if path.is_file():self.assertNotIn('private-secret',path.read_text())

    def test_retry_after_blocks_other_process_without_more_network_calls(self):
        failure=HTTPError('https://example.invalid/private',429,'busy',{'Retry-After':'300'},None)
        with self.assertRaises(HTTPError):
            with rate.request_slot(self.store,self.indexer):raise failure
        self.assertEqual(failure.retry_at,1300)
        self.assertEqual(rate.limited_indexers(failure),('example.invalid',))
        with self.assertRaises(sources.DiscoverError) as blocked:
            with rate.request_slot(SimpleNamespace(directory=self.tmp.name),self.indexer):self.fail('Must not contact a cooling-down indexer')
        self.assertEqual(sources.failure_reason(blocked.exception),'http_429')
        self.assertEqual(blocked.exception.retry_at,1300)
        self.assertIn('example.invalid',str(blocked.exception))
        self.assertNotIn('private-secret',str(blocked.exception))
        self.now=1301
        with rate.request_slot(self.store,self.indexer):pass

    def test_configured_name_identifies_the_limited_account(self):
        self.indexer['name']='NZBgeek'
        failure=HTTPError('https://private.invalid/secret',429,'busy',{},None)
        with self.assertRaises(HTTPError):
            with rate.request_slot(self.store,self.indexer):raise failure
        with self.assertRaises(sources.DiscoverError) as blocked:
            with rate.request_slot(self.store,self.indexer):self.fail('Must honour the cooldown')
        self.assertEqual(rate.limited_indexers(blocked.exception),('NZBgeek',))
        self.assertIn('NZBgeek',str(blocked.exception))
        self.assertNotIn('private',str(blocked.exception))

    def test_http_date_and_missing_retry_after_are_handled(self):
        self.assertEqual(rate.retry_seconds(HTTPError('',429,'',{'Retry-After':'Thu, 01 Jan 1970 00:21:40 GMT'},None),1000),300)
        self.assertEqual(rate.retry_seconds(HTTPError('',429,'',{},None),1000),120)
        self.assertIsNone(rate.retry_seconds(ValueError('private'),1000))
