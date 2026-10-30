import tempfile
import unittest
from backend.storage import Store
from backend.quota import bind


class QuotaTests(unittest.TestCase):
    def test_client_switch_restores_counts_and_same_client_keeps_them(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            store.set('quota_day','2026-09-26');store.set('quota_used','8500')
            store.set('history_quota_day','2026-09-26');store.set('history_quota_used','1500')
            with store.connect() as db:bind(db,'first-client')
            with store.connect() as db:bind(db,'first-client')
            self.assertEqual(store.get('quota_used'),'8500')
            with store.connect() as db:bind(db,'second-client')
            self.assertEqual(store.get('quota_used'),'')
            self.assertEqual(store.get('history_quota_used'),'')
            store.set('quota_used','25')
            with store.connect() as db:bind(db,'first-client')
            self.assertEqual(store.get('quota_used'),'8500')
            self.assertEqual(store.get('history_quota_used'),'1500')
            with store.connect() as db:bind(db,'second-client')
            self.assertEqual(store.get('quota_used'),'25')
