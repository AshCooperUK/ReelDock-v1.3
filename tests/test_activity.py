import json
import tempfile
import time
import unittest
from backend.app import create_app
from backend import activity, m3u

class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=create_app({'TESTING':True,'DATA_DIR':self.temp.name,'M3U_DIR':self.temp.name+'/media'})
        self.store=self.app.extensions['store']
        self.store.set('public_origin','https://test.example');self.store.set('setup_complete','1')
        with self.store.connect() as db:db.execute("INSERT INTO users VALUES(1,'admin','unused')")
        self.source=m3u.save(self.store,dict(name='Playlist one',url='https://example.com/private?credential=secret',existing_library=True,naming='emby'))
    def tearDown(self):self.temp.cleanup()
    def test_discover_engine_progress_uses_dedicated_tiles_only(self):
        from backend import discover_store
        discover_store.initialise(self.store)
        for engine in ('nzb', 'torrent'):
            for state in ('queued', 'downloading', 'verifying', 'paused'):
                key = engine + '-' + state
                row, _ = discover_store.insert_request(self.store, dict(
                    idempotency=key, kind='movie', title=key, state=state,
                    quality='1080', engine=engine, engine_id=key, body={}))
                self.assertNotIn('discover-'+row['id'], {t['id'] for t in activity.snapshot(self.store)['tasks']})
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM discover_requests').fetchone()[0], 8)

    def test_discover_search_retry_and_failure_remain_visible(self):
        from backend import discover_store
        discover_store.initialise(self.store)
        for state in ('searching', 'waiting', 'cancelling', 'failed'):
            row, _ = discover_store.insert_request(self.store, dict(
                idempotency=state, kind='movie', title=state, state=state,
                quality='1080', engine='nzb', engine_id='previous-job', body={}))
            data = activity.snapshot(self.store)
            entries = data['alerts'] if state == 'failed' else data['tasks']
            self.assertTrue(any(t['id'].startswith('discover-'+row['id']) for t in entries))

    def test_operational_discover_tasks_expose_exact_transport_links_without_writes_or_engines(self):
        from backend import discover_store
        discover_store.initialise(self.store)
        records=[]
        for engine,state in [('youtube','downloading'),('vod','queued'),('nzb','cancelling'),('youtube','searching'),('vod','starting'),('nzb','waiting')]:
            row,_=discover_store.insert_request(self.store,dict(idempotency=engine+'-'+state,kind='movie',title=state,
                state=state,quality='1080',engine=engine,engine_id='exact-'+engine,body={'private_source':'PRIVATE_SOURCE'}))
            records.append((engine,state,row['id']))
        with self.store.connect() as db:before=list(db.iterdump())
        data=activity.snapshot(self.store)
        tasks={task['id']:task for task in data['tasks']}
        for engine,state,identity in records:
            task=tasks['discover-'+identity]
            if state in ('queued','downloading','cancelling'):
                self.assertEqual((task['engine'],task['engine_id']),(engine,'exact-'+engine))
            else:
                self.assertNotIn('engine',task);self.assertNotIn('engine_id',task)
            if state=='waiting':self.assertEqual(task['waiting'],1)
        self.assertNotIn('PRIVATE_SOURCE',json.dumps(data))
        self.assertNotIn('nzb_engine',self.app.extensions);self.assertNotIn('torrent_engine',self.app.extensions)
        with self.store.connect() as db:self.assertEqual(list(db.iterdump()),before)

    def test_waiting_discover_backlog_collapses_to_one_entry(self):
        from backend import discover_store
        discover_store.initialise(self.store)
        for index in range(3):
            discover_store.insert_request(self.store, dict(
                idempotency='wait-'+str(index), kind='movie', title='Title '+str(index), state='waiting',
                quality='1080', body={}))
        tasks=activity.snapshot(self.store)['tasks']
        discover=[t for t in tasks if t['id'].startswith('discover-')]
        self.assertEqual([t['id'] for t in discover],['discover-waiting'])
        self.assertIn('3',discover[0]['message'])
        self.assertEqual(discover[0]['waiting'],3)
        with self.store.connect() as db:db.execute("DELETE FROM discover_requests WHERE idempotency IN ('wait-1','wait-2')")
        discover=[t for t in activity.snapshot(self.store)['tasks'] if t['id'].startswith('discover-')]
        self.assertEqual(len(discover),1);self.assertNotEqual(discover[0]['id'],'discover-waiting')
        self.assertEqual(discover[0]['waiting'],1)

    def test_discover_youtube_does_not_repeat_manual_video_job(self):
        from backend import discover_store
        discover_store.initialise(self.store)
        row, _ = discover_store.insert_request(self.store, dict(
            idempotency='youtube-manual', kind='video', title='Video', state='queued',
            quality='1080', body={'videoId':'abcdefghijk'}))
        with self.store.connect() as db:
            db.execute("INSERT INTO downloads(id,title,quality,state,created,updated,automatic) VALUES('abcdefghijk','Video','1080','queued',?,?,0)",(time.time(),time.time()))
        self.assertNotIn('discover-'+row['id'], {t['id'] for t in activity.snapshot(self.store)['tasks']})

    def test_concurrent_work_is_not_hidden_by_another_job(self):
        self.store.set('sync_running','1');self.store.set('sync_message','Checking subscriptions')
        self.store.set('history_running','1');self.store.set('history_message','Loading older uploads')
        with self.store.connect() as db:
            db.execute("UPDATE m3u_sources SET state='running',message='Fetching playlist' WHERE id=?",(self.source,))
            db.execute("INSERT INTO m3u_metadata_runs VALUES(?,'running','Matching metadata','{}',?)",(self.source,time.time()))
            db.execute("INSERT INTO emby_refresh VALUES('channel',?)",(time.time(),))
        activity.record(self.store,'art','Artwork','running','Downloading images')
        activity.record(self.store,'browser','Browser download','running','Sending video',25)
        data=activity.snapshot(self.store)
        ids={t['id'] for t in data['tasks']}
        self.assertTrue({'youtube-sync','youtube-history','m3u-'+self.source,'m3u_metadata_runs-'+self.source,'emby-refresh','art','browser'}<=ids)
        self.assertNotIn('credential',json.dumps(data));self.assertNotIn('secret',json.dumps(data))
        self.assertEqual(next(t for t in data['tasks'] if t['id']=='browser')['progress'],25)
    def test_errors_are_separate_alerts_and_changed_messages_get_new_ids(self):
        activity.record(self.store,'art','Artwork','error','Artwork unavailable')
        activity.record(self.store,'import','Import','running','Working')
        first=activity.snapshot(self.store)
        self.assertEqual(len(first['alerts']),1);self.assertEqual(len(first['tasks']),1)
        self.assertEqual(first['alerts'],activity.snapshot(self.store)['alerts'])
        activity.record(self.store,'art','Artwork','error','Artwork retry failed')
        self.assertNotEqual(first['alerts'][0]['id'],activity.snapshot(self.store)['alerts'][0]['id'])
        activity.record(self.store,'art','Artwork','complete','Images saved')
        self.assertFalse(activity.snapshot(self.store)['alerts'])
        self.assertEqual(activity.snapshot(self.store)['recent'][0]['id'],'art')
    def test_youtube_hover_stays_while_the_download_progresses(self):
        image = '/artwork/' + ('ab' * 32)
        activity.record(self.store, 'auto-youtube-abcdefghijk', 'Big Buck Bunny', 'running', 'Downloading from YouTube.', 0, preview={
            'title': 'Big Buck Bunny', 'channel': 'Blender', 'overview': 'A short film from the channel.',
            'image': image, 'watch': 'https://www.youtube.com/watch?v=secret',
        })
        activity.record(self.store, 'auto-youtube-abcdefghijk', 'Big Buck Bunny', 'running', 'Downloading from YouTube.', 40)
        task = next(item for item in activity.snapshot(self.store)['tasks'] if item['id'] == 'auto-youtube-abcdefghijk')
        self.assertEqual(task['progress'], 40)
        self.assertEqual(task['preview'], {'title': 'Big Buck Bunny', 'channel': 'Blender', 'overview': 'A short film from the channel.', 'image': image})
        self.assertNotIn('youtube.com', json.dumps(task))
        self.assertNotIn('secret', json.dumps(task))
        activity.record(self.store, 'auto-youtube-abcdefghijk', 'Big Buck Bunny', 'running', 'Downloading from YouTube.', 10, preview={'title': 'Nope', 'image': 'https://evil.example/pic.jpg', 'overview': 'Kept'})
        again = next(item for item in activity.snapshot(self.store)['tasks'] if item['id'] == 'auto-youtube-abcdefghijk')
        self.assertEqual(again['preview']['image'], '')
        self.assertEqual(again['preview']['overview'], 'Kept')

    def test_stale_running_work_is_not_reported_as_live(self):
        activity.record(self.store,'stale','Old job','running','Working')
        activity.record(self.store,'done','Completed job','complete','Finished')
        with self.store.connect() as db:db.execute('UPDATE activity_tasks SET updated=?',(time.time()-1000,))
        data=activity.snapshot(self.store)
        self.assertFalse(data['tasks']);self.assertFalse(data['recent'])
    def test_long_import_is_recent_when_it_finishes(self):
        with self.store.connect() as db:
            db.execute("UPDATE m3u_sources SET state='complete',last_run=?,message='Import finished' WHERE id=?",(time.time()-3600,self.source))
            db.execute("INSERT INTO m3u_runs(source,finished,state,message,stats) VALUES(?,?,'complete','Import finished','{}')",(self.source,time.time()))
        self.assertEqual(activity.snapshot(self.store)['recent'][0]['id'],'m3u-'+self.source)

    def test_activity_requires_login_and_returns_all_jobs(self):
        self.store.set('sync_running','1')
        client=self.app.test_client()
        self.assertNotEqual(client.get('/api/activity',base_url='https://test.example').status_code,200)
        with client.session_transaction(base_url='https://test.example') as session:session['user_id']=1
        data=client.get('/api/activity',base_url='https://test.example').json
        self.assertIn('tasks',data);self.assertIn('alerts',data);self.assertIn('recent',data)
        self.assertEqual(data['tasks'][0]['id'],'youtube-sync')

    def test_dismissed_download_survives_changed_error_and_session(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO downloads(id,title,quality,state,created,updated,message) VALUES('abcdefghijk','Video','720','failed',?,?, 'Not available in your country')",(time.time(),time.time()))
        identity=activity.snapshot(self.store)['alerts'][0]['id']
        client=self.app.test_client()
        self.assertNotEqual(client.post('/api/activity/dismiss',json={'id':identity},base_url='https://test.example').status_code,200)
        with client.session_transaction(base_url='https://test.example') as session:session.update(user_id=1,csrf='test')
        self.assertEqual(client.post('/api/activity/dismiss',json={'id':identity},base_url='https://test.example').status_code,403)
        self.assertEqual(client.post('/api/activity/dismiss',json={'id':identity},headers={'X-CSRF-Token':'test','Origin':'https://test.example'},base_url='https://test.example').status_code,200)
        with self.store.connect() as db:db.execute("UPDATE downloads SET message='Provider failed again',updated=?",(time.time(),))
        self.assertFalse(activity.snapshot(self.store)['alerts'])
        other=self.app.test_client()
        with other.session_transaction(base_url='https://test.example') as session:session['user_id']=1
        self.assertFalse(other.get('/api/activity',base_url='https://test.example').json['alerts'])
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT state FROM downloads").fetchone()[0],'failed')

    def test_cookie_warning_survives_legacy_dismissal_until_session_recovers(self):
        from backend.cookie_recovery import record
        self.store.set('download_quality','720');self.store.set('youtube_cookie_downloads','false')
        record(self.store,'auth_expired')
        with self.store.connect() as db:
            db.execute("INSERT INTO downloads(id,title,quality,state,created,updated,message) VALUES('abcdefghijk','Video','720','failed',?,?,'Other download failure')",(time.time(),time.time()))
            before=dict(db.execute('SELECT * FROM downloads').fetchone())
        client=self.app.test_client()
        self.assertNotEqual(client.get('/api/activity',base_url='https://test.example').status_code,200)
        with client.session_transaction(base_url='https://test.example') as session:session.update(user_id=1,csrf='test')
        first=client.get('/api/activity',base_url='https://test.example').json['cookie_warning']
        self.assertIn('expired',first['message']);self.assertNotIn('secret',json.dumps(first))
        self.assertEqual(client.post('/api/activity/dismiss',json={'id':first['id']},headers={'Origin':'https://test.example'},base_url='https://test.example').status_code,403)
        headers={'X-CSRF-Token':'test','Origin':'https://test.example'}
        self.assertEqual(client.post('/api/activity/dismiss',json={'id':first['id']},headers=headers,base_url='https://test.example').status_code,200)
        with self.store.connect() as db:self.assertIsNotNone(db.execute('SELECT id FROM dismissed_alerts WHERE id=?',(first['id'],)).fetchone())
        self.assertEqual(client.get('/api/activity',base_url='https://test.example').json['cookie_warning'],first,'An old Dismiss cannot hide an unresolved cookie issue')
        other=activity.snapshot(self.store)['alerts'][0]['id']
        self.assertEqual(client.post('/api/activity/dismiss',json={'id':other},headers=headers,base_url='https://test.example').status_code,200)
        later=self.app.test_client()
        with later.session_transaction(base_url='https://test.example') as session:session['user_id']=1
        data=later.get('/api/activity',base_url='https://test.example').json
        self.assertEqual(data['cookie_warning'],first);self.assertFalse(data['alerts'],'Other alert dismissals keep their existing behavior')
        record(self.store,'active')
        self.assertIsNone(later.get('/api/activity',base_url='https://test.example').json['cookie_warning'],'Confirmed recovery removes the warning')
        with self.store.connect() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM downloads').fetchone()),before)
            self.assertEqual(db.execute('SELECT count(*) FROM dismissed_alerts').fetchone()[0],2)
        self.assertEqual(self.store.get('download_quality'),'720');self.assertEqual(self.store.get('youtube_cookie_downloads'),'false')

    def test_group_dismissal_keeps_history_and_allows_new_failures(self):
        def add(identity):
            with self.store.connect() as db:db.execute("INSERT INTO downloads(id,title,quality,state,created,updated,message) VALUES(?,'Video','720','failed',?,?,'Cookies need attention')",(identity,time.time(),time.time()))
        add('aaaaaaaaaaa');add('bbbbbbbbbbb')
        alert=activity.snapshot(self.store)['alerts'][0]
        self.assertIn('2 YouTube',alert['title'])
        client=self.app.test_client()
        with client.session_transaction(base_url='https://test.example') as session:session.update(user_id=1,csrf='test')
        response=client.post('/api/activity/dismiss',json={'id':alert['id']},headers={'X-CSRF-Token':'test','Origin':'https://test.example'},base_url='https://test.example')
        self.assertEqual(response.status_code,200)
        self.assertFalse(activity.snapshot(self.store)['alerts'])
        add('ccccccccccc')
        self.assertEqual(activity.snapshot(self.store)['alerts'][0]['id'],'download-ccccccccccc')
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM downloads').fetchone()[0],3)
