"""Subscription preferences using isolated databases and a local child process."""
import sys
import threading
import unittest
from unittest.mock import patch
import test_channel_rules_everywhere as fixtures
from backend import channel_downloads as rules, downloads, download_options


class SubscriptionPreferencesTests(unittest.TestCase):
    setUp=fixtures.ChannelRulesEverywhereTests.setUp
    tearDown=fixtures.ChannelRulesEverywhereTests.tearDown
    video=fixtures.ChannelRulesEverywhereTests.video
    row=fixtures.ChannelRulesEverywhereTests.row

    def test_discover_manual_selection_bypasses_subscription_filters(self):
        from backend.discover_store import set_quality_policy
        rule=rules.save(self.store,'channel',{'videos':True,'range_days':7,'minimum_video_minutes':0})
        video=self.video(1,100)
        set_quality_policy(self.store,video['id'],False)
        for stage in ('start','save'):
            downloads.check_rules(self.store,{'id':video['id']},False,video,rule,180,stage)
            with self.assertRaises(downloads.DownloadSkipped):
                downloads.check_rules(self.store,{'id':video['id']},True,video,rule,180,stage)
        self.assertEqual(rules.refusal(self.store,video),'')
        downloads.enqueue(self.store,video,'720',automatic=False)
        rules.save(self.store,'channel',{'range_days':1})
        self.assertEqual(self.row(video['id'])['state'],'queued')
        downloads.check_rules(self.store,{'id':video['id']},False,video,dict(rule,minimum_video_minutes=10),180,'save')
        downloads.check_rules(self.store,{'id':video['id']},False,dict(video,kind='shorts'),rule,30,'save')
        rules.save(self.store,'channel',{'shorts':True,'videos':False,'minimum_video_minutes':10})
        self.assertEqual(self.row(video['id'])['state'],'queued')
        ordinary=self.video(2,0)
        downloads.check_rules(self.store,{'id':ordinary['id']},False,ordinary,dict(rule,minimum_video_minutes=10),180,'save')
        with self.assertRaises(downloads.DownloadSkipped):
            downloads.check_rules(self.store,{'id':ordinary['id']},True,ordinary,dict(rule,minimum_video_minutes=10),180,'save')

    def test_minimum_inherits_and_explicit_zero_survives_other_edits(self):
        self.store.set('download_minimum_video_minutes',10)
        self.assertEqual(rules.preferences(self.store,'channel')['minimum_video_minutes'],10)
        rules.save(self.store,'channel',{'minimum_video_minutes':0})
        rules.save(self.store,'channel',{'range_days':'default','subscription_paused':True})
        self.assertEqual(download_options.for_channel(self.store,'channel')['minimum_video_minutes'],0)
        rules.save(self.store,'channel',{'minimum_video_minutes':'default'})
        self.store.set('download_minimum_video_minutes',3)
        self.assertEqual(rules.preferences(self.store,'channel')['minimum_video_minutes'],3)
        rules.save(self.store,'channel',{'quality':'default'})
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT minimum_video_minutes FROM download_rules').fetchone()[0],-1)

    def test_length_check_honours_override_and_short_exemption(self):
        video=self.video(1,0)
        rule=rules.save(self.store,'channel',{'videos':True,'shorts':True,'minimum_video_minutes':1})
        downloads.check_rules(self.store,{},True,video,rule,90,'start')
        rule=rules.save(self.store,'channel',{'minimum_video_minutes':2})
        with self.assertRaises(downloads.DownloadSkipped):
            downloads.check_rules(self.store,{},True,video,rule,90,'save')
        downloads.check_rules(self.store,{},True,{**video,'kind':'shorts'},rule,15,'save')
        for bad in (True,-1,121,1.5,'2',None):
            with self.assertRaises(ValueError):rules.save(self.store,'channel',{'minimum_video_minutes':bad})

    def test_pause_holds_auto_queue_but_manual_runs_and_resume_preserves_choices(self):
        rules.save(self.store,'channel',{'videos':True,'range_days':30,'minimum_video_minutes':2})
        automatic=self.video(1,0);manual=self.video(2,0)
        downloads.enqueue(self.store,automatic,'720',automatic=True)
        downloads.enqueue(self.store,manual,'720',automatic=False)
        rules.save(self.store,'channel',{'subscription_paused':True})
        self.assertEqual(rules.schedule(self.store,self.media),0)
        called=[]
        with patch('backend.downloads.library.local_file',return_value=None):
            downloads.tick(self.store,self.media,threading.Event(),runner=lambda s,m,j,stop:called.append(j['id']))
            self.assertEqual(called,[manual['id']])
            self.assertEqual(self.row(automatic['id'])['state'],'queued')
            rules.save(self.store,'channel',{'subscription_paused':False})
            downloads.tick(self.store,self.media,threading.Event(),runner=lambda s,m,j,stop:called.append(j['id']))
        self.assertEqual(called,[manual['id'],automatic['id']])
        result=rules.preferences(self.store,'channel')
        self.assertEqual((result['videos'],result['range_days'],result['minimum_video_minutes']),(1,30,2))

    def test_pause_stops_local_child_and_keeps_job_queued(self):
        rules.save(self.store,'channel',{'videos':True,'range_days':30})
        video=self.video(1,0)
        downloads.enqueue(self.store,video,'720',automatic=True)
        timer=threading.Timer(.3,lambda:rules.save(self.store,'channel',{'subscription_paused':True}))
        def runner(store,media,job,stop):
            timer.start()
            return downloads.run_process(store,job,stop,[sys.executable,'-c','import time; time.sleep(30)'],self.media/'fixture.log',self.media)
        try:
            downloads.tick(self.store,self.media,threading.Event(),runner=runner)
        finally:timer.cancel();timer.join(3)
        self.assertEqual(self.row(video['id'])['state'],'queued')
        self.assertIn('Subscription paused',self.row(video['id'])['message'])

    def test_pause_protects_retention(self):
        rules.save(self.store,'channel',{'range_days':7,'subscription_paused':True})
        video=self.video(1,20)
        downloads.enqueue(self.store,video,'720',automatic=True)
        downloads.update(self.store,video['id'],'complete',100,'Saved')
        self.assertEqual(rules.expire_downloads(self.store,self.media),0)
        rules.save(self.store,'channel',{'subscription_paused':False})
        self.assertEqual(rules.expire_downloads(self.store,self.media),1)

    def test_cancel_wins_over_pause_requeue(self):
        video=self.video(1,0)
        downloads.enqueue(self.store,video,'720',automatic=True)
        def runner(store,media,job,stop):
            with store.connect() as db:db.execute("UPDATE downloads SET state='cancelling' WHERE id=?",(job['id'],))
            raise downloads.DownloadPaused()
        downloads.tick(self.store,self.media,threading.Event(),runner=runner)
        self.assertEqual(self.row(video['id'])['state'],'cancelled')
