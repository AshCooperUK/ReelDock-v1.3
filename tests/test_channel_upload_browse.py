"""Metadata paging traverses public uploads without enabling history/downloads."""
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch

import test_youtube_optional as fixture
from backend import channel_upload_browse as browse, channel_downloads, youtube_fallback, youtube_push, downloads

CHANNEL, ORIGIN = fixture.CHANNEL, fixture.ORIGIN


class ChannelUploadBrowseTests(unittest.TestCase):
    setUp = fixture.OptionalYouTubeTests.setUp
    tearDown = fixture.OptionalYouTubeTests.tearDown
    google = fixture.OptionalYouTubeTests.google
    extract = fixture.OptionalYouTubeTests.extract
    feed = fixture.OptionalYouTubeTests.feed
    xml = fixture.OptionalYouTubeTests.xml

    def post(self, cursor='0', **extra):
        return self.client.post('/api/guide/channel/'+CHANNEL, base_url=ORIGIN,
                                json={'action':'browse_uploads','cursor':cursor,**extra},
                                headers={'Origin':ORIGIN,'X-CSRF-Token':'csrf-fixture'})

    def page(self, cursor='0'):
        return self.client.get('/api/guide/channel/'+CHANNEL+'?browse_cursor='+cursor,base_url=ORIGIN).json['browse']

    def rows(self, table):
        with self.store.connect() as db:return [tuple(row) for row in db.execute('SELECT * FROM '+table)]

    def protected(self):
        return {table:self.rows(table) for table in ['downloads','download_rules','history_jobs','guide_week_jobs','youtube_history_requests','channel_tasks']}

    @staticmethod
    def entries(start, end, count=53):
        return {'channel_id':CHANNEL,'entries':[dict(id=f'browse{index:05}',title=f'Public upload {index}',channel_id=CHANNEL,duration=600+index) for index in range(start-1,min(end,count))]}

    def test_web_request_is_queue_only_get_is_read_only_and_guards_are_retained(self):
        before=self.protected()
        with patch.object(youtube_fallback,'extract',side_effect=AssertionError('Web requests must not extract')):
            self.assertEqual(self.post().status_code,202)
            self.assertEqual(self.post().status_code,202)
            with self.store.connect() as db:dump='\n'.join(db.iterdump())
            self.assertEqual(self.page()['state'],'queued')
            with self.store.connect() as db:self.assertEqual('\n'.join(db.iterdump()),dump)
        self.assertEqual(len(self.rows('channel_upload_pages')),1)
        self.assertEqual(self.protected(),before)
        for cursor in ['-1','26','01','10000000','x']:
            self.assertEqual(self.post(cursor).status_code,400)
        self.assertEqual(self.post('25').status_code,400,'a caller cannot skip an uncached provider cursor')
        self.assertEqual(self.post(extra=True).status_code,400)
        denied=self.client.post('/api/guide/channel/'+CHANNEL,base_url=ORIGIN,json={'action':'browse_uploads','cursor':'0'},headers={'Origin':ORIGIN,'X-CSRF-Token':'bad'})
        self.assertEqual(denied.status_code,403)
        self.assertEqual(self.app.test_client().post('/api/guide/channel/'+CHANNEL,base_url=ORIGIN,json={'action':'browse_uploads','cursor':'0'},headers={'Origin':ORIGIN}).status_code,401)

    def test_three_exact_windows_and_reopen_replay_all_cached_pages(self):
        calls=[]
        def extract(importer,url,mode,start,end,**options):
            calls.append((url,mode,start,end,options))
            return self.entries(start,end)
        before=self.protected()
        with patch.object(youtube_fallback,'extract',side_effect=extract):
            all_ids=[]
            for cursor,next_cursor,length in [('0','25',25),('25','50',25),('50',None,3)]:
                self.assertEqual(self.post(cursor).status_code,202)
                self.assertTrue(browse.tick(self.importer))
                page=self.page(cursor)
                self.assertEqual(page['cursor'],cursor)
                self.assertEqual(page['nextCursor'],next_cursor)
                self.assertEqual(len(page['page']['items']),length)
                all_ids.extend(item['id'] for item in page['page']['items'])
            self.assertEqual(len(set(all_ids)),53)
            for cursor,next_cursor in [('0','25'),('25','50'),('50',None)]:
                cached=self.post(cursor)
                self.assertEqual(cached.status_code,200)
                self.assertEqual(cached.json['browse']['page']['cursor'],cursor)
                self.assertEqual(cached.json['browse']['nextCursor'],next_cursor)
            self.assertEqual(len(calls),3,'reopening replays page0,25,50 rather than jumping to highest or re-extracting')
        self.assertEqual([(call[2],call[3]) for call in calls],[(1,26),(26,51),(51,76)])
        for url,mode,start,end,options in calls:
            self.assertEqual(url,'https://www.youtube.com/playlist?list=UU'+CHANNEL[2:])
            self.assertEqual(mode,'playlist')
            self.assertEqual(options,{'public':True,'timeout':30,'foreground':True})
        self.assertEqual(self.protected(),before)

    def test_concurrent_same_cursor_requests_and_worker_claims_share_one_extraction(self):
        self.post()
        started,release=threading.Event(),threading.Event()
        def extract(*args,**kwargs):
            started.set();release.wait(3);return self.entries(1,26)
        with patch.object(youtube_fallback,'extract',side_effect=extract) as extractor:
            thread=threading.Thread(target=browse.tick,args=(self.importer,))
            thread.start()
            try:
                self.assertTrue(started.wait(1))
                self.assertEqual(self.post().json['browse']['state'],'running')
                self.assertFalse(browse.tick(self.importer))
                self.assertEqual(extractor.call_count,1)
            finally:
                release.set();thread.join(3)
        self.assertEqual(len(self.page()['page']['items']),25)

    def test_unknown_dates_are_honest_and_browse_only_survives_rich_hydration(self):
        self.post()
        with patch.object(youtube_fallback,'extract',return_value=self.entries(1,2,count=1)):
            browse.tick(self.importer)
        item=self.page()['page']['items'][0]
        self.assertEqual(item['date'],'')
        self.assertEqual(item['state'],'unknown')
        self.assertEqual(item['duration'],'10:00')
        self.assertTrue(item['metadata_pending'])
        self.assertTrue(item['browse_only'])
        rule=channel_downloads.preferences(self.store,CHANNEL)
        self.assertFalse(channel_downloads.eligible(item,rule))
        full=youtube_fallback.video_item(dict(id=item['id'],channel_id=CHANNEL,title='Hydrated title',timestamp=time.time(),duration=600,live_status='not_live'),item['id'])
        with patch.object(self.importer,'api',return_value={'items':[full]}):
            youtube_push.process(self.importer)
        hydrated=self.page()['page']['items'][0]
        self.assertEqual(hydrated['state'],'published')
        self.assertFalse(hydrated['metadata_pending'])
        self.assertTrue(hydrated['browse_only'])
        self.assertFalse(hydrated['forecast_eligible'])
        self.assertFalse(channel_downloads.eligible(hydrated,rule))
        self.assertEqual(channel_downloads.schedule(self.store,self.app.config['MEDIA_DIR']),0)
        self.assertEqual(self.rows('downloads'),[])
        self.assertEqual(channel_downloads.refusal(self.store,hydrated),'','manual requests retain the existing channel-policy contract')
        downloads.enqueue(self.store,hydrated,'1080',automatic=False)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT automatic FROM downloads').fetchone()[0],0)

    def test_pending_browse_manual_request_reports_checking_without_touching_queue(self):
        self.post()
        with patch.object(youtube_fallback,'extract',return_value=self.entries(1,2,count=1)):browse.tick(self.importer)
        before=self.protected()
        response=self.client.post('/api/downloads',base_url=ORIGIN,json={'id':'browse00000','action':'queue','quality':'1080'},headers={'Origin':ORIGIN,'X-CSRF-Token':'csrf-fixture'})
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.json['error'],'Video details are still being checked. Try again shortly.')
        self.assertEqual(self.protected(),before,'checking metadata cannot enqueue or change channel/history policies')
        # Existing RSS placeholders have a genuine publication date and keep
        # their established explicit-manual workflow while details are pending.
        observed=youtube_fallback.video_item(dict(id='rsspending1',channel_id=CHANNEL,title='Observed by RSS',timestamp=time.time(),duration=60),'rsspending1')
        observed['_pending']=True;self.importer.save_videos([observed])
        existing=self.client.post('/api/downloads',base_url=ORIGIN,json={'id':'rsspending1','action':'queue','quality':'1080'},headers={'Origin':ORIGIN,'X-CSRF-Token':'csrf-fixture'})
        self.assertEqual(existing.status_code,200,existing.json)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT id FROM downloads').fetchone()[0],'rsspending1')

    def test_normal_rss_and_normal_history_observations_clear_browse_provenance(self):
        self.post()
        with patch.object(youtube_fallback,'extract',return_value=self.entries(1,2,count=1)):browse.tick(self.importer)
        identity='browse00000'
        full=youtube_fallback.video_item(dict(id=identity,channel_id=CHANNEL,title='A real observed video',timestamp=time.time(),duration=600),identity)
        self.importer.save_videos([full],preserve_browse_only=True)
        youtube_fallback.placeholder(self.importer,[{**full,'_pending':True}])
        with self.store.connect() as db:item=json.loads(db.execute('SELECT data FROM videos WHERE id=?',(identity,)).fetchone()[0])
        self.assertNotIn('browse_only',item)
        self.assertEqual(item['title'],'A real observed video','normal RSS never replaces richer facts')
        with self.store.connect() as db:db.execute("UPDATE videos SET data=json_set(data,'$.browse_only',1) WHERE id=?",(identity,))
        self.importer.save_videos([full])
        with self.store.connect() as db:item=json.loads(db.execute('SELECT data FROM videos WHERE id=?',(identity,)).fetchone()[0])
        self.assertNotIn('browse_only',item,'an explicit normal history import remains authoritative')

    def test_normal_observation_promotes_forecast_only_for_rich_published_nonlive_items(self):
        from backend.youtube_fallback import promote_observed
        cases=[('published',False,'videos',True),('published',False,'live',False),('published',True,'unknown',False),('scheduled',False,'unknown',False)]
        with self.store.connect() as db:
            for index,(state,pending,kind,eligible) in enumerate(cases):
                identity=f'promote{index:04}'
                body=dict(id=identity,channel=CHANNEL,title='Keep title',state=state,metadata_pending=pending,kind=kind,browse_only=True,forecast_eligible=False)
                db.execute('INSERT INTO videos VALUES(?,?,?,?,?,?)',(identity,CHANNEL,'2026-10-24','2026-10-24T12:00:00Z',json.dumps(body),123))
            promote_observed(db,[f'promote{index:04}' for index in range(len(cases))],CHANNEL)
        with self.store.connect() as db:
            for index,(_,_,_,eligible) in enumerate(cases):
                row=dict(db.execute('SELECT * FROM videos WHERE id=?',(f'promote{index:04}',)).fetchone());body=json.loads(row['data'])
                self.assertNotIn('browse_only',body)
                self.assertIs(body['forecast_eligible'],eligible)
                self.assertEqual((row['day'],row['published'],row['checked']),('2026-10-24','2026-10-24T12:00:00Z',123))

    def test_existing_rich_video_and_old_oneoff_do_not_corrupt_provider_cursor(self):
        full=youtube_fallback.video_item(dict(id='browse00000',channel_id=CHANNEL,title='Keep rich title',timestamp=time.time()-86400*700,duration=999), 'browse00000')
        self.importer.save_videos([full])
        before=self.rows('videos')
        self.post()
        with patch.object(youtube_fallback,'extract',return_value=self.entries(1,26)):browse.tick(self.importer)
        page=self.page()
        self.assertEqual(page['nextCursor'],'25')
        self.assertEqual(page['page']['items'][0]['title'],'Keep rich title')
        with self.store.connect() as db:self.assertEqual(tuple(db.execute("SELECT * FROM videos WHERE id='browse00000'").fetchone()),before[0])

    def test_flat_page_fills_only_missing_duration_on_a_pending_rss_observation(self):
        identity='browse00000'
        normal=youtube_fallback.video_item(dict(id=identity,channel_id=CHANNEL,title='RSS title',timestamp=time.time(),duration=0),identity)
        normal.pop('contentDetails')
        normal['_pending']=True
        self.importer.save_videos([normal])
        with self.store.connect() as db:before=dict(db.execute('SELECT * FROM videos WHERE id=?',(identity,)).fetchone())
        self.post()
        with patch.object(youtube_fallback,'extract',return_value=self.entries(1,2,count=1)):browse.tick(self.importer)
        with self.store.connect() as db:after=dict(db.execute('SELECT * FROM videos WHERE id=?',(identity,)).fetchone())
        original=json.loads(before.pop('data'));changed=json.loads(after.pop('data'))
        self.assertEqual(before,after,'indexed date/checked/published identity remains untouched')
        self.assertEqual(changed.pop('duration'),'10:00')
        original.pop('duration')
        self.assertEqual(changed,original,'only missing duration is filled; RSS does not become browse-only')

    def test_failed_or_repeated_pages_keep_the_exact_cursor_and_retry_deadline(self):
        self.post()
        first=self.entries(1,26)
        with patch.object(youtube_fallback,'extract',return_value=first):browse.tick(self.importer)
        self.post('25')
        with patch.object(youtube_fallback,'extract',return_value=first):browse.tick(self.importer)
        page=self.page('25')
        self.assertEqual(page['state'],'retry')
        self.assertEqual(page['nextCursor'],'25')
        self.assertNotIn('page',page)
        with self.store.connect() as db:deadline=db.execute('SELECT retry_at FROM channel_upload_pages WHERE offset=25').fetchone()[0]
        with patch.object(youtube_fallback,'extract',side_effect=AssertionError('Cooldown is authoritative')):
            self.post('25');self.assertFalse(browse.tick(self.importer))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT retry_at FROM channel_upload_pages WHERE offset=25').fetchone()[0],deadline)
        self.assertEqual(len(self.rows('videos')),25)

    def test_membership_pause_and_stolen_claim_are_rechecked_after_provider_work(self):
        for change in ['inactive','paused','stolen']:
            with self.subTest(change=change):
                with self.store.connect() as db:
                    db.execute('DELETE FROM channel_upload_pages');db.execute('UPDATE channels SET active=1 WHERE id=?',(CHANNEL,))
                self.store.set('youtube_sync_paused','false')
                self.post()
                def extract(*args,**kwargs):
                    with self.store.connect() as db:
                        if change=='inactive':db.execute('UPDATE channels SET active=0 WHERE id=?',(CHANNEL,))
                        elif change=='paused':db.execute("INSERT OR REPLACE INTO settings VALUES('youtube_sync_paused','true')")
                        else:db.execute("UPDATE channel_upload_pages SET claim='new-owner'")
                    return self.entries(1,26)
                with patch.object(youtube_fallback,'extract',side_effect=extract):self.assertTrue(browse.tick(self.importer))
                self.assertEqual(self.rows('videos'),[])
                self.assertEqual(self.rows('youtube_push_events'),[])

    def test_empty_provider_page_completes_and_recovery_removal_are_scoped(self):
        self.post()
        with patch.object(youtube_fallback,'extract',return_value={'entries':[]}):browse.tick(self.importer)
        self.assertEqual(self.page()['state'],'complete')
        self.assertIsNone(self.page()['nextCursor'])
        self.assertEqual(self.page()['page']['items'],[])
        with self.store.connect() as db:
            db.execute("UPDATE channel_upload_pages SET state='running',claim='old',lease_until=?",(time.time()+999,))
        browse.recover(self.store)
        self.assertEqual(self.page()['state'],'queued')
        with self.store.connect() as db:
            db.execute('DELETE FROM channels WHERE id=?',(CHANNEL,))
        self.assertEqual(self.rows('channel_upload_pages'),[])

    def test_public_extract_never_reads_saved_cookies_and_has_a_single_total_budget(self):
        self.app.config.pop('YOUTUBE_EXTRACT',None)
        self.app.testing=False
        seen=[]
        def run(args,input,stdout,**kwargs):
            seen.append((json.loads(input),kwargs['timeout']))
            stdout.write(json.dumps({'entries':[]}).encode())
        with patch('backend.watched_status.cookie_session_file_status',side_effect=AssertionError('No saved cookie read')),patch.object(youtube_fallback,'run_extractor',side_effect=run):
            result=youtube_fallback.extract(self.importer,'https://www.youtube.com/playlist?list=UU'+CHANNEL[2:],'playlist',1,26,public=True,timeout=30)
        self.assertEqual(result,{'entries':[]})
        self.assertEqual(len(seen),1)
        self.assertNotIn('cookie',seen[0][0])
        self.assertGreater(seen[0][1],0)
        self.assertLessEqual(seen[0][1],30)

    def test_public_extract_lock_wait_is_bounded_without_starting_another_provider_request(self):
        lock=Mock();lock.acquire.return_value=False
        with patch.object(youtube_fallback,'_lock',lock),patch.object(youtube_fallback.time,'monotonic',side_effect=[0,0,31]),patch.object(youtube_fallback,'run_extractor',side_effect=AssertionError('No provider while the metadata slot is busy')):
            with self.assertRaisesRegex(ValueError,'waiting for another metadata request'):
                youtube_fallback.extract(self.importer,'https://www.youtube.com/playlist?list=UU'+CHANNEL[2:],'playlist',1,26,public=True,timeout=30)
        lock.acquire.assert_called_once_with(timeout=.5)
        lock.release.assert_not_called()


if __name__=='__main__':unittest.main()
