import tempfile
import unittest
from unittest.mock import patch
from backend.storage import Store
from backend import discover_acquire as acquire, discover_store as requests

class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name);requests.initialise(self.store)
        self.item=dict(kind='show',externalId='482384',title='The Wargame')
    def queue(self,episodes):
        return acquire.queue_manual(self.store,self.item,episodes,'1080',True,'NZB first','batch-test-request-token')
    def test_invalid_last_episode_creates_no_orphans(self):
        for number in (None,True,'2',0,-1):
            with self.assertRaises(ValueError):
                self.queue([dict(id=1,season=1,number=1),dict(id=2,season=1,number=number)])
            with self.store.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM discover_requests').fetchone()[0],0)
    def test_batch_duplicates_are_blocked(self):
        episode=dict(id=1,season=1,number=1)
        rows=self.queue([episode,episode])
        self.assertEqual(rows[0]['id'],rows[1]['id']);self.assertTrue(rows[1]['blocked'])
    def test_single_episode_does_not_select_season_pack(self):
        releases=[dict(id=str(i),title=title,externalId='482384',quality='1080p') for i,title in enumerate(['The.Wargame.S01.1080p','The.Wargame.S01E02.1080p','The.Wargame.S01E03.1080p'])]
        self.assertEqual([r['id'] for r in acquire.choose(releases,'1080',True,'482384',dict(season=1,number=2))],['1'])
    def test_search_failure_after_cancel_does_not_revive_request(self):
        row=self.queue([dict(id=1,season=1,number=1)])[0]
        def fail(*args):
            requests.cancel_request(self.store,row['id'])
            raise RuntimeError('Provider unavailable')
        with patch.object(acquire,'search_for',side_effect=fail):
            acquire.process(None,self.store,row)
        with self.store.connect() as db:
            state=db.execute('SELECT state FROM discover_requests WHERE id=?',(row['id'],)).fetchone()[0]
        self.assertIn(state,('cancelled','cancelling'))

    def test_late_duplicate_cannot_start_an_already_queued_job(self):
        row=self.queue([dict(id=1,season=1,number=1)])[0]
        generation=requests.claim_start(self.store,row['id'])
        self.assertIsNotNone(generation)
        self.assertTrue(requests.commit_engine(self.store,row['id'],generation,'nzb','existing-job','Queued'))
        self.assertIsNone(requests.claim_start(self.store,row['id']))
