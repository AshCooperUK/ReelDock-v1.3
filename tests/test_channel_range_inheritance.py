"""Global range inheritance, explicit choices, and queue reconciliation."""
import unittest
import test_channel_rules_everywhere as fixtures
from backend import channel_downloads, downloads
from backend.settings.youtube import after_write


class ChannelRangeInheritanceTests(unittest.TestCase):
    setUp=fixtures.ChannelRulesEverywhereTests.setUp
    tearDown=fixtures.ChannelRulesEverywhereTests.tearDown
    video=fixtures.ChannelRulesEverywhereTests.video
    row=fixtures.ChannelRulesEverywhereTests.row
    # Reuse the isolated store fixture without inheriting the original test cases.
    def test_inherited_range_tracks_global_without_overwriting_explicit_all(self):
        self.store.set('download_default_range',7)
        self.assertEqual(channel_downloads.preferences(self.store,'channel')['range_days'],7)
        result=channel_downloads.save(self.store,'channel',{'videos':True})
        self.assertEqual(result['range_override'],0)
        self.store.set('download_default_range',30)
        self.assertEqual(channel_downloads.preferences(self.store,'channel')['range_days'],30)
        channel_downloads.save(self.store,'channel',{'range_days':0})
        self.store.set('download_default_range',1)
        self.assertEqual(channel_downloads.preferences(self.store,'channel')['range_days'],0)
        result=channel_downloads.save(self.store,'channel',{'range_days':'default'})
        self.assertEqual((result['range_override'],result['range_days']),(0,1))

    def test_global_change_reconciles_inherited_queue_and_reschedules(self):
        channel_downloads.save(self.store,'channel',{'range_days':'default','videos':True})
        video=self.video(1,20)
        downloads.enqueue(self.store,video,'1080',automatic=True)
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('download_default_range','7')")
            after_write(db,{'default_range':7})
        self.assertEqual(self.row(video['id'])['state'],'skipped')
        self.assertEqual(self.store.get('next_auto_check'),'0')
        channel_downloads.save(self.store,'channel',{'range_days':30})
        downloads.enqueue(self.store,video,'1080',automatic=True)
        with self.store.connect() as db:after_write(db,{'default_range':1})
        self.assertEqual(self.row(video['id'])['state'],'queued')

    def test_scheduler_uses_latest_global_range_and_all_uploads(self):
        channel_downloads.save(self.store,'channel',{'videos':True,'range_days':'default'})
        old=self.video(2,20)
        self.store.set('download_default_range',7)
        self.assertEqual(channel_downloads.schedule(self.store,self.media),0)
        self.store.set('download_default_range',0)
        self.assertEqual(channel_downloads.schedule(self.store,self.media),1)
        self.assertEqual(self.row(old['id'])['state'],'queued')

    def test_legacy_rows_remain_explicit(self):
        with self.store.connect() as db:
            db.execute("ALTER TABLE download_rules DROP COLUMN range_override")
            db.execute("INSERT INTO download_rules(channel,range_days) VALUES('channel',30)")
        self.store.set('download_default_range',7)
        channel_downloads.initialise(self.store)
        result=channel_downloads.preferences(self.store,'channel')
        self.assertEqual((result['range_override'],result['range_days']),(1,30))

    def test_inherited_all_uploads_disables_retention_and_manual_files_stay(self):
        channel_downloads.save(self.store,'channel',{'range_days':'default','videos':True})
        video=self.video(3,20)
        downloads.enqueue(self.store,video,'1080',automatic=True)
        downloads.update(self.store,video['id'],'complete',100,'Saved')
        self.store.set('download_default_range',0)
        self.assertEqual(channel_downloads.expire_downloads(self.store,self.media),0)
        self.store.set('download_default_range',7)
        with self.store.connect() as db:db.execute('UPDATE downloads SET automatic=0')
        self.assertEqual(channel_downloads.expire_downloads(self.store,self.media),0)
        with self.store.connect() as db:db.execute('UPDATE downloads SET automatic=1')
        self.assertEqual(channel_downloads.expire_downloads(self.store,self.media),1)
