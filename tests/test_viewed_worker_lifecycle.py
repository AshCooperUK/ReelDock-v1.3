"""Viewed upload pages have a worker lifecycle independent of banner work."""
import fcntl
import os
import threading
import time
import unittest
from unittest.mock import patch

import test_youtube_optional as fixture
from backend import channel_refresh, channel_metadata_refresh as metadata, channel_upload_browse as browse, worker


class ViewedWorkerLifecycleTests(unittest.TestCase):
    setUp=fixture.OptionalYouTubeTests.setUp
    tearDown=fixture.OptionalYouTubeTests.tearDown
    google=fixture.OptionalYouTubeTests.google
    extract=fixture.OptionalYouTubeTests.extract
    feed=fixture.OptionalYouTubeTests.feed
    xml=fixture.OptionalYouTubeTests.xml

    def test_startup_recovers_all_viewed_claims_before_any_thread_and_joins_each_lane(self):
        channel=fixture.CHANNEL
        channel_refresh.request_recent(self.store,channel)
        browse.request_page(self.store,channel,'0')
        metadata.request(self.store,channel)
        tables=['channel_refresh_requests','channel_upload_pages','channel_metadata_refresh_requests']
        with self.store.connect() as db:
            for table in tables:db.execute("UPDATE "+table+" SET state='running',claim='previous-worker',lease_until=?",(time.time()+120,))
        created=[];owner=self
        class Thread:
            def __init__(self,*,target,args,daemon,name):
                self.target,self.args,self.daemon,self.name=target,args,daemon,name
                self.started=False;self.joined=None;created.append(self)
            def start(self):
                with owner.store.connect() as db:
                    for table in tables:
                        row=db.execute('SELECT state,claim,lease_until FROM '+table).fetchone()
                        owner.assertEqual(tuple(row),('queued','',0),'claims recover before the first worker thread starts')
                with (owner.store.directory/'worker.lock').open('a') as probe:
                    with owner.assertRaises(BlockingIOError):fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.started=True
            def join(self,timeout):self.joined=timeout
        class EndTest(Exception):pass
        with patch.dict(os.environ,{}),patch.object(worker,'create_app',return_value=self.app),patch.object(worker.threading,'Thread',Thread),patch.object(worker,'run_tick',side_effect=EndTest):
            with self.assertRaises(EndTest):worker.main()
        lanes={thread.name:thread for thread in created}
        pages,information=lanes['viewed-channel-pages'],lanes['viewed-channel-information']
        self.assertIs(pages.target,browse.loop)
        self.assertIs(information.target,metadata.loop)
        self.assertIsNot(pages.target,information.target)
        self.assertIs(pages.args[2],information.args[2])
        self.assertTrue(pages.args[2].is_set(),'shutdown reaches both independent loops')
        for lane in [pages,information]:
            self.assertTrue(lane.started)
            self.assertTrue(lane.daemon)
            self.assertEqual(lane.joined,35)

    def test_upload_loop_never_runs_banner_work(self):
        stop=threading.Event();attempts=[]
        def page(importer,signal):
            attempts.append(importer.store);signal.set();return True
        with patch.object(browse,'tick',side_effect=page),patch.object(metadata,'tick',side_effect=AssertionError('Banner work cannot block the upload-page loop')):
            browse.loop(self.app,self.store,stop)
        self.assertEqual(len(attempts),1)
        self.assertEqual(attempts[0].directory,self.store.directory)

    def test_upload_queue_serves_newest_view_first_without_changing_other_work(self):
        channel=fixture.CHANNEL;other='UC'+'z'*22
        with self.store.connect() as db:
            row=db.execute('SELECT data FROM channels WHERE id=?',(channel,)).fetchone()
            db.execute('INSERT INTO channels VALUES(?,?,1)',(other,row[0]))
        browse.request_page(self.store,channel,'0');browse.request_page(self.store,other,'0')
        with self.store.connect() as db:
            db.execute('UPDATE channel_upload_pages SET requested=? WHERE channel=?',(time.time()-1,channel))
            before={table:[tuple(row) for row in db.execute('SELECT * FROM '+table)] for table in ['downloads','download_rules','history_jobs']}
        urls=[]
        def extract(importer,url,*args,**options):urls.append(url);return {'entries':[]}
        with patch('backend.youtube_fallback.extract',side_effect=extract):
            self.assertTrue(browse.tick(self.importer));self.assertTrue(browse.tick(self.importer))
        self.assertEqual(urls,['https://www.youtube.com/playlist?list=UU'+identity[2:] for identity in [other,channel]])
        with self.store.connect() as db:self.assertEqual({table:[tuple(row) for row in db.execute('SELECT * FROM '+table)] for table in before},before)


if __name__=='__main__':unittest.main()
