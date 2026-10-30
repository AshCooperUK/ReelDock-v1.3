"""Permanent Activity clearing preserves immutable exports and operational jobs."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app import create_app
from backend import activity, activity_clear, download_history, event_log

ORIGIN='https://dev.example.com'


class ActivityClearTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.media=self.root/'media';self.media.mkdir()
        self.app=create_app(dict(TESTING=True,DATA_DIR=str(self.root/'data'),MEDIA_DIR=str(self.media)))
        self.store=self.app.extensions['store'];self.store.set('public_origin',ORIGIN);self.store.set('setup_complete','1')
        with self.store.connect() as db:db.execute("INSERT INTO users VALUES(1,'owner','unused')")
        self.client=self.app.test_client()
        with self.client.session_transaction(base_url=ORIGIN) as session:session.update(user_id=1,csrf='token')

    def post(self,section='completed',**kwargs):
        return self.client.post('/api/activity/clear',base_url=ORIGIN,json={'section':section},headers={'X-CSRF-Token':'token','Origin':ORIGIN},**kwargs)

    def seed(self,identity='abcdefghijk',state='complete',message='Original outcome'):
        with self.store.connect() as db:
            db.execute('INSERT INTO downloads(id,title,quality,state,progress,message,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                       (identity,'Saved '+identity,'1080',state,100 if state=='complete' else 0,message,100,200))

    def history(self,archive=False,limit=50,offset=0):
        return download_history.page(self.store,str(self.media),limit,offset,plain=True,include_archived=archive)

    def test_auth_origin_csrf_and_body_are_required_without_changes(self):
        self.seed()
        self.assertNotEqual(self.app.test_client().post('/api/activity/clear',base_url=ORIGIN,json={'section':'completed'}).status_code,200)
        self.assertEqual(self.client.post('/api/activity/clear',base_url=ORIGIN,json={'section':'completed'},headers={'Origin':ORIGIN}).status_code,403)
        self.assertEqual(self.client.post('/api/activity/clear',base_url=ORIGIN,json={'section':'completed'},headers={'X-CSRF-Token':'token','Origin':'https://other.example'}).status_code,403)
        for body in ({},{'section':'all'},{'section':'queued'},{'section':'attention','ids':['x']},{'section':True}):
            self.assertEqual(self.client.post('/api/activity/clear',base_url=ORIGIN,json=body,headers={'X-CSRF-Token':'token','Origin':ORIGIN}).status_code,400)
        self.assertEqual(self.history()['total'],1)

    def test_completed_is_permanent_across_store_restart_but_files_jobs_holds_remain(self):
        self.seed();self.seed('BBBBBBBBBBB','queued');self.seed('CCCCCCCCCCC','failed')
        video=self.media/'saved.mp4';video.write_bytes(b'owner media');self.store.set('acquisition_hold','1')
        before=video.stat();ack=self.post().get_json();self.assertEqual(ack['cleared'],1)
        self.assertEqual(self.history()['total'],2)
        from backend.storage import Store
        reopened=Store(self.store.directory);activity_clear.initialise(reopened)
        self.assertEqual(download_history.page(reopened,str(self.media),plain=True)['total'],2)
        self.assertEqual(video.read_bytes(),b'owner media');self.assertEqual(video.stat().st_ino,before.st_ino)
        self.assertEqual(self.store.get('acquisition_hold'),'1')
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM downloads').fetchone()[0],3)
        exported=self.history(True);self.assertEqual(exported['total'],3)
        archived=next(row for row in exported['items'] if row['item'].get('archived'))
        self.assertEqual(archived['item']['originalId'],'abcdefghijk');self.assertEqual(archived['item']['message'],'Original outcome')
        self.assertTrue(archived['item']['id'].startswith('archive:'))

    def test_attention_and_completed_match_terminal_sections_never_clear_active_waiting(self):
        states=['failed','cancelled','expired','skipped','complete','queued','downloading','cancelling']
        for n,state in enumerate(states):self.seed(str(n)*11,state)
        self.assertEqual(self.post('attention').get_json()['cleared'],3)
        self.assertEqual(self.post('completed').get_json()['cleared'],2)
        self.assertEqual({r['item']['state'] for r in self.history()['items']},{'queued','downloading','cancelling'})
        self.assertEqual(len(self.history()['active']),3)

    def test_retention_expiry_clears_with_completed_and_preserves_raw_archive_outcome(self):
        expected={
            'retention01':'Removed automatically: outside this channel’s download range.',
            'retention02':"Removed automatically: outside this channel's download range.",
        }
        for identity,message in expected.items():self.seed(identity,'expired',message)
        self.seed('failed00001','failed',expected['retention01'])
        self.seed('cancelled01','cancelled',expected['retention01'])
        self.seed('unknown0001','expired','An unknown expiry needs investigation.')
        # The familiar cleanup text cannot classify another engine as success.
        for kind in ('m3u','discover','event','nzb','torrent'):
            self.assertEqual(activity_clear.section(kind,dict(state='expired',message=expected['retention01'])),'attention')
        self.assertEqual(self.post('attention').get_json()['cleared'],3)
        self.assertEqual({row['item']['id'] for row in self.history()['items']},set(expected))
        self.assertEqual(self.post('completed').get_json()['cleared'],2)
        self.assertEqual(self.history()['total'],0)
        archived={row['item']['originalId']:row['item'] for row in self.history(True)['items']}
        self.assertEqual(len(archived),5)
        for identity,message in expected.items():
            self.assertEqual((archived[identity]['state'],archived[identity]['message']),('expired',message))
        with self.store.connect() as db:
            saved={row['id']:(row['state'],row['message']) for row in db.execute('SELECT id,state,message FROM downloads')}
        for identity,message in expected.items():self.assertEqual(saved[identity],('expired',message))

    def test_same_id_new_attempt_without_activity_poll_is_visible_and_old_snapshots_are_immutable(self):
        self.seed(state='failed',message='Attempt one');self.post('attention')
        with self.store.connect() as db:
            db.execute("UPDATE downloads SET state='queued' WHERE id='abcdefghijk'")
            db.execute("UPDATE downloads SET state='downloading' WHERE id='abcdefghijk'")
            db.execute("UPDATE downloads SET state='failed',message='Attempt two' WHERE id='abcdefghijk'")
        self.assertEqual(self.history()['total'],1);self.post('attention')
        exported=self.history(True)['items'];self.assertEqual(len(exported),2)
        self.assertEqual({r['item']['message'] for r in exported},{'Attempt one','Attempt two'})
        self.assertEqual(len({r['item']['id'] for r in exported}),2)

    def test_passive_updated_message_metadata_and_restart_never_resurrect_cleared_version(self):
        self.seed();self.post()
        with self.store.connect() as db:db.execute("UPDATE downloads SET updated=9999,message='Metadata repaired',processing_info='read refresh' WHERE id='abcdefghijk'")
        activity_clear.initialise(self.store)
        self.assertEqual(self.history()['total'],0)
        self.assertEqual(self.history(True)['items'][0]['item']['message'],'Original outcome')

    def test_archive_pagination_totals_and_duplicate_clear_are_exact(self):
        for n in range(18):self.seed(f'{n:011d}')
        first=self.post().get_json();second=self.post().get_json()
        self.assertEqual((first['cleared'],second['cleared']),(18,0))
        self.assertEqual(first['activityRevision'],second['activityRevision'])
        self.assertEqual(self.history()['total'],0)
        one=self.history(True,15);two=self.history(True,15,15)
        self.assertEqual((one['total'],len(one['items']),one['more']),(18,15,True));self.assertEqual(len(two['items']),3)
        self.assertEqual(len({r['item']['id'] for r in one['items']+two['items']}),18)

    def test_cleared_event_batches_start_new_ids_instead_of_mutating_exported_outcome(self):
        event_log.artwork_batch(self.store,2);event_log.metadata_batch(self.store,'m3u-exports',3)
        self.post();event_log.artwork_batch(self.store,1);event_log.metadata_batch(self.store,'m3u-exports',1)
        current=self.history()['items'];self.assertEqual(len(current),2)
        self.assertEqual({r['item']['message'] for r in current},{'1 image processed.','1 metadata item processed.'})
        exported=self.history(True)['items'];self.assertEqual(len(exported),4)
        self.assertTrue(any(r['item']['message']=='2 images processed.' for r in exported))

    def test_archive_strips_private_body_headers_trackers_and_access_urls(self):
        native=[dict(kind='torrent',item=dict(id='native',name='Safe title',state='stopped',progress=1,added=5,
            error='Failed https://host/?token=PRIVATE_URL Cookie: session=PRIVATE_COOKIE',trackers=['PRIVATE_TRACKER'],
            body='PRIVATE_BODY',activity_generation=0))]
        ack=activity_clear.clear(self.store,str(self.media),'completed',native)
        self.assertEqual(ack['clearedEngineGenerations']['torrent'],{'native':0})
        archive=self.history(True)['items'][0];raw=json.dumps(archive)
        for secret in ('PRIVATE_URL','PRIVATE_COOKIE','PRIVATE_TRACKER','PRIVATE_BODY'):self.assertNotIn(secret,raw)
        self.assertEqual(archive['kind'],'torrent');self.assertTrue(archive['item']['archived'])

    def test_live_native_controls_and_new_generation_are_not_cleared(self):
        native=[dict(kind='torrent',item=dict(id='active',state='seeding',progress=1)),dict(kind='nzb',item=dict(id='pending',state='choose',progress=1)),dict(kind='nzb',item=dict(id='failed',state='failed',progress=.2,activity_generation=4))]
        ack=activity_clear.clear(self.store,str(self.media),'attention',native)
        self.assertEqual(ack['cleared'],1);self.assertEqual(ack['clearedEngineGenerations'],{'torrent':{},'nzb':{'failed':4}})
        with self.store.connect() as db:
            self.assertTrue(activity_clear.is_hidden(db,'nzb','failed',4));self.assertFalse(activity_clear.is_hidden(db,'nzb','failed',5))

    def test_alerts_and_recent_task_snapshot_obey_clear_without_dismissing_new_attempt(self):
        self.seed(state='failed');activity.record(self.store,'task-one','Work','complete','Finished')
        activity_clear.clear(self.store,str(self.media),'attention');activity_clear.clear(self.store,str(self.media),'completed')
        snapshot=activity.snapshot(self.store)
        self.assertFalse(any(a['id']=='download-abcdefghijk' for a in snapshot['alerts']))
        self.assertFalse(any(t['id']=='task-one' for t in snapshot['recent']))
        with self.store.connect() as db:
            db.execute("UPDATE downloads SET state='queued' WHERE id='abcdefghijk'")
            db.execute("UPDATE downloads SET state='failed',updated=99999999999 WHERE id='abcdefghijk'")
        self.assertTrue(any(a['id']=='download-abcdefghijk' for a in activity.snapshot(self.store)['alerts']))

    def test_concurrent_duplicate_clears_archive_each_existing_record_exactly_once(self):
        for n in range(5):self.seed(f'{n:011d}')
        gate=threading.Barrier(2);results=[];errors=[]
        def run():
            try:gate.wait(2);results.append(activity_clear.clear(self.store,str(self.media),'completed'))
            except Exception as exc:errors.append(exc)
        threads=[threading.Thread(target=run) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(5);self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[]);self.assertEqual(sorted(r['cleared'] for r in results),[0,5])
        self.assertEqual(self.history(True)['total'],5)

    def test_terminal_event_committed_after_atomic_snapshot_is_not_cleared(self):
        self.seed();captured=threading.Event();release=threading.Event();started=threading.Event();errors=[]
        original=download_history.page
        def paused_page(*args,**kwargs):
            result=original(*args,**kwargs);captured.set();self.assertTrue(release.wait(3));return result
        def clearing():
            try:
                with patch('backend.download_history.page',side_effect=paused_page):activity_clear.clear(self.store,str(self.media),'completed')
            except Exception as exc:errors.append(exc)
        def late_event():
            started.set();event_log.record(self.store,'New completion','Committed after the clear began')
        first=threading.Thread(target=clearing);first.start();self.assertTrue(captured.wait(2))
        second=threading.Thread(target=late_event);second.start();self.assertTrue(started.wait(2));release.set()
        for thread in (first,second):thread.join(5);self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[]);current=self.history()['items']
        self.assertEqual(len(current),1);self.assertEqual(current[0]['item']['title'],'New completion')

    def test_clear_and_archive_reads_do_not_enrich_or_mutate_saved_facts(self):
        self.seed()
        with patch('backend.download_history.local_file',side_effect=AssertionError('No filesystem resolution')),patch('backend.discover_sources.show_media',side_effect=AssertionError('No provider calls')):
            activity_clear.clear(self.store,str(self.media),'completed')
            with self.store.connect() as db:before=list(db.iterdump())
            self.history(True);activity.snapshot(self.store)
            with self.store.connect() as db:self.assertEqual(list(db.iterdump()),before)

    def test_terminal_task_without_history_is_archived_counted_and_advances_revision(self):
        activity.record(self.store,'only-task','Standalone background work','complete','Saved task facts')
        with self.store.connect() as db:db.execute('DELETE FROM activity_events')
        ack=activity_clear.clear(self.store,str(self.media),'completed')
        self.assertEqual(ack['cleared'],1);self.assertEqual(ack['activityRevision'],1)
        self.assertEqual(self.history()['total'],0);archive=self.history(True)['items']
        self.assertEqual(len(archive),1);self.assertEqual(archive[0]['kind'],'event')
        self.assertEqual(archive[0]['item']['title'],'Standalone background work')
        self.assertEqual(archive[0]['item']['message'],'Saved task facts')
        self.assertEqual(activity_clear.clear(self.store,str(self.media),'completed')['cleared'],0)

    def test_all_sql_history_sources_filter_before_pagination_and_keep_operational_rows(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO m3u_downloads(id,source,path,title,state,created,updated,message) VALUES('server','missing','media.strm','Saved stream','failed',2,2,'Server failed')")
            db.execute("INSERT INTO m3u_runs(source,finished,state,message,stats) VALUES('missing',3,'error','Sync failed','{}')")
            db.execute("INSERT INTO activity_events(created,category,title,state,message) VALUES(4,'Server','Old failure','failed','Old event')")
            for identity,state in [('failed-request','failed'),('waiting-request','waiting')]:
                db.execute("INSERT INTO discover_requests(id,idempotency,kind,title,state,quality,fallback,body,message,progress,updated,created) VALUES(?,?, 'movie','Saved movie',?,'1080',0,?,'Movie outcome',0,5,5)",
                           (identity,identity,state,json.dumps({'grab':'https://provider/?token=PRIVATE_GRAB'})))
        self.assertEqual(activity_clear.clear(self.store,str(self.media),'attention')['cleared'],4)
        current=self.history(limit=1);self.assertEqual((current['total'],len(current['items']),current['more']),(1,1,False))
        self.assertEqual(current['items'][0]['item']['id'],'waiting-request');self.assertEqual(len(current['active']),1)
        exported=self.history(True);self.assertEqual(exported['total'],5)
        for row in exported['items']:
            if row['item'].get('archived'):self.assertNotIn('PRIVATE_GRAB',json.dumps(row))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM discover_requests').fetchone()[0],2)
            db.execute("UPDATE discover_requests SET body='{}',updated=99 WHERE id='failed-request'")
        self.assertEqual(self.history()['total'],1)

    def test_auxiliary_source_tasks_preserve_exact_public_names_without_copying_configuration(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO m3u_sources(id,name,url,options,folder,state,last_run) VALUES('source','Archive playlist','https://provider/?token=PRIVATE_CONFIG','{}','source','complete',10)")
            db.execute("INSERT INTO m3u_metadata_runs(source,state,message,updated) VALUES('source','complete','Local metadata finished',11)")
        ack=activity_clear.clear(self.store,str(self.media),'completed');self.assertEqual(ack['cleared'],2)
        records=self.history(True)['items'];self.assertEqual({r['item']['title'] for r in records},{'Archive playlist · Playlist','Archive playlist · Metadata'})
        self.assertTrue(all(r['kind']=='event' for r in records));self.assertNotIn('PRIVATE_CONFIG',json.dumps(records))

    def test_activity_cached_get_flag_reaches_no_provider_path_and_retains_file_facts(self):
        self.seed()
        with self.store.connect() as db:
            db.execute("INSERT INTO discover_requests(id,idempotency,kind,title,state,quality,fallback,body,message,progress,updated,created) VALUES('show','show','season','Saved show','failed','1080',0,?,'Saved outcome',0,100,100)",(json.dumps({'externalId':'public-show','description':'Saved details'}),))
        with patch('backend.discover_sources.show_media',side_effect=AssertionError('No provider work from cached Activity GET')),patch('backend.download_history.local_file',return_value=Path('/fixture/saved.mp4')):
            response=self.client.get('/api/download-history?limit=50&cached=1',base_url=ORIGIN)
        self.assertEqual(response.status_code,200)
        youtube=next(row for row in response.get_json()['items'] if row['kind']=='youtube')
        self.assertEqual(youtube['item']['destination'],'/fixture/saved.mp4')


    def test_clear_logs_removes_finished_and_previously_archived_exports_only(self):
        self.seed('old-complete','complete')
        self.assertEqual(self.post('completed').status_code,200)
        self.seed('failed-item','failed')
        self.seed('cancelled-item','cancelled')
        self.seed('active-item','downloading')
        self.seed('queued-item','queued')
        self.seed('paused-item','paused')
        self.store.set('owner-preference','keep-this')
        self.store.set('youtube_cookie_issue','auth_expired')
        with self.store.connect() as db:
            db.execute("INSERT INTO discover_requests(id,idempotency,kind,title,state,quality,fallback,body,message,progress,updated,created) VALUES('future-show','future-show','episode','Future episode','waiting','1080',0,'{}','Waiting',0,100,100)")
            before_downloads=[tuple(row) for row in db.execute('SELECT * FROM downloads ORDER BY id')]
            before_requests=[tuple(row) for row in db.execute('SELECT * FROM discover_requests ORDER BY id')]
            before_holds=[tuple(row) for row in db.execute('SELECT * FROM media_clear_holds')]
            before_settings=[tuple(row) for row in db.execute('SELECT * FROM settings ORDER BY key')]
        result=self.post('logs');self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(result.json['section'],'logs');self.assertEqual(result.json['cleared'],3)
        exported=self.history(True)
        self.assertEqual({str(row['item']['id']) for row in exported['items']},{'active-item','queued-item','paused-item','future-show'})
        self.assertFalse(any(row['kind']=='archive' for row in exported['items']))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM activity_archive').fetchone()[0],0)
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM downloads ORDER BY id')],before_downloads)
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM discover_requests ORDER BY id')],before_requests)
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM media_clear_holds')],before_holds)
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM settings ORDER BY key')],before_settings)
        duplicate=self.post('logs').json;self.assertEqual(duplicate['cleared'],0);self.assertEqual(duplicate['activityRevision'],result.json['activityRevision'])
        self.seed('new-complete','complete')
        self.assertIn('new-complete',{str(row['item']['id']) for row in self.history(True)['items']})

    def test_clear_logs_native_terminal_generation_and_original_owned_files_are_preserved(self):
        owned=self.media/'owned.mkv';owned.write_bytes(b'owned-video')
        native=[dict(kind='nzb',item=dict(id='finished-native',name='Finished NZB',state='complete',activity_generation=7,published=['owned.mkv'])),dict(kind='nzb',item=dict(id='active-native',name='Active NZB',state='downloading',activity_generation=9))]
        result=activity_clear.clear(self.store,str(self.media),'logs',native)
        self.assertEqual(result['cleared'],1);self.assertEqual(result['clearedEngineGenerations']['nzb'],{'finished-native':7})
        self.assertEqual(native[0]['item']['state'],'complete');self.assertEqual(native[1]['item']['state'],'downloading')
        self.assertEqual(owned.read_bytes(),b'owned-video')
        self.assertEqual(self.history(True)['items'],[])
        with self.store.connect() as db:self.assertFalse(activity_clear.is_hidden(db,'nzb','finished-native',8),'a later real attempt is not hidden')

    def test_clear_logs_auth_and_incomplete_snapshot_do_not_erase_archived_entries(self):
        self.seed('saved-log','complete');self.post('completed')
        original=self.history(True)
        self.assertNotEqual(self.app.test_client().post('/api/activity/clear',base_url=ORIGIN,json={'section':'logs'}).status_code,200)
        self.assertEqual(self.client.post('/api/activity/clear',base_url=ORIGIN,json={'section':'logs'},headers={'Origin':ORIGIN}).status_code,403)
        with patch('backend.download_history.page',return_value={'items':[],'more':True}):
            response=self.post('logs')
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.history(True),original)


if __name__=='__main__':unittest.main()
