"""Header Download uses saved policy and preserves separate future automation."""
import json
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import patch
import test_view_media as vm
from backend import acquisition,channel_downloads,discover_store as records,discovery_engine,media_holds,movie_options,show_options
from backend.media_management import store as managed,service


class QuickDownloadTests(unittest.TestCase):
    setUpBase=vm.ViewMediaTests.setUp
    tearDown=vm.ViewMediaTests.tearDown

    def setUp(self):
        self.setUpBase()
        self.scheduler=patch.object(discovery_engine,'schedule',return_value=True)
        self.schedule=self.scheduler.start();self.addCleanup(self.scheduler.stop)
        self.movie=dict(id='movie:1234567',kind='movie',externalId='1234567',title='Known movie',year='2020')

    def post(self,item,token='token',client=None,extra=None):
        return (client or self.client).post('/api/quick-download',base_url=vm.ORIGIN,json=dict(item=item,**(extra or {})),headers={'Origin':vm.ORIGIN,'X-CSRF-Token':token})

    def jobs(self,table='discover_requests'):
        with self.store.connect() as db:return [dict(row) for row in db.execute('SELECT * FROM '+table)]

    def hold(self,key,kind='discover',identity='fixture'):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');media_holds.hold(db,[key],[(kind,identity)])

    def video(self,state='published',**changes):
        video=dict(id='abcdefghijk',title='Saved upload',channel='UC'+'a'*22,date='2020-01-01',state=state,kind='shorts',**changes)
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO channels VALUES(?,?,1)',(video['channel'],json.dumps(dict(id=video['channel'],name='Known channel'))))
            db.execute('INSERT OR REPLACE INTO videos VALUES(?,?,?,?,?,0)',(video['id'],video['channel'],video['date'],video['date'],json.dumps(video)))
        return video,dict(id='video:'+video['id'],videoId=video['id'],kind='video',title=video['title'],channelId=video['channel'])

    def test_auth_origin_csrf_and_exact_body_before_any_work(self):
        self.assertIn(self.post(self.movie,client=self.app.test_client()).status_code,(401,403))
        self.assertEqual(self.post(self.movie,token='wrong').status_code,403)
        self.assertEqual(self.post(self.movie,extra=dict(quality='2160')).status_code,400)
        self.assertEqual(self.client.post('/api/quick-download',base_url=vm.ORIGIN,json=dict(item=self.movie),headers={'Origin':'https://other.test','X-CSRF-Token':'token'}).status_code,403)
        self.assertEqual(self.jobs(),[]);self.schedule.assert_not_called()

    def test_movie_uses_saved_quality_fallback_and_minimum_settings(self):
        movie_options.save(self.app,self.store,self.movie,dict(quality='720',minimum=42,automatic=False))
        self.store.set('discover_engine_fallback','true')
        response=self.post(dict(self.movie,quality='2160',minimum=0))
        self.assertEqual(response.status_code,202,response.json);self.assertEqual(response.json['state'],'searching')
        queued=self.jobs();self.assertEqual(len(queued),1);self.assertEqual(queued[0]['quality'],'720');self.assertTrue(queued[0]['fallback'])
        self.assertEqual(movie_options.minimum_for_request(self.store,queued[0]),42)
        self.assertFalse(movie_options.find(self.store,self.movie)['automatic'])
        self.assertEqual(self.schedule.call_count,1)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM discover_series_plan').fetchone()[0],0)

    def test_duplicate_movie_clicks_use_one_request_without_enabling_monitoring(self):
        first=self.post(self.movie);second=self.post(self.movie)
        self.assertEqual(first.status_code,202,first.json);self.assertEqual(second.status_code,202,second.json)
        self.assertEqual(len(self.jobs()),1);self.assertEqual(self.schedule.call_count,1)
        self.assertNotEqual(managed.row(self.store,'movie:1234567')['monitor'],1)

    def test_unprepared_show_and_unverified_movie_require_details_without_empty_plan(self):
        for item in (dict(id='show:123',kind='show',externalId='123',title='Known series'),dict(id='tmdb:movie:42',kind='movie',title='Unverified movie')):
            response=self.post(item);self.assertEqual(response.status_code,202,response.json);self.assertEqual(response.json['state'],'choice_required')
        self.assertEqual(self.jobs(),[]);self.schedule.assert_not_called()
        self.assertEqual(self.jobs('discover_series_plan'),[])

    def series(self,episodes=None):
        item=dict(id='show:77',kind='show',externalId='77',title='Disposable complete series',year='2020')
        for season,number,day in episodes or [(1,1,'2020-01-01'),(2,1,'2020-01-02'),(0,1,'2020-01-03'),(2,2,'2099-01-01')]:
            managed.remember(self.store,f'show:77:{season}:{number}',item['id'],'episode',dict(item=item,episode=dict(season=season,number=number,name='Episode'),date=day,automatic=True))
        return item

    def apply_series(self):
        with patch.object(service,'compose',side_effect=lambda _app,_store,row:dict(file=bool(records.owned(self.store,row['key'])))):
            discovery_engine.apply_plan(self.app,self.store,self.jobs('discover_series_plan')[0])

    def test_show_requests_entire_catalogue_all_seasons_and_specials_with_saved_global_nzb_settings(self):
        episodes=[(season,number,'2020-01-01') for season in (1,2,3) for number in range(1,18)]+[(0,1,'2020-01-02'),(4,1,'2099-01-01')]
        item=self.series(episodes)
        self.store.set('discover_engine_quality','2160');self.store.set('discover_engine_fallback','true')
        config=Path(self.store.directory)/'nzb-engine'/'settings.json';config.parent.mkdir(exist_ok=True)
        config.write_text(json.dumps(dict(min_video_minutes=12,min_download_bytes=1_000_000,max_download_bytes=9_000_000_000)))
        before=config.read_bytes()
        with patch.object(discovery_engine.sources,'episodes',side_effect=AssertionError('No provider catalogue request in header POST')):
            result=self.post(dict(item,quality='720',minimum=0))
            self.assertEqual(result.status_code,202,result.json);self.assertEqual(result.json['state'],'searching')
            self.assertTrue(result.json['downloadAll']);self.assertFalse(result.json['downloadFuture'])
            self.apply_series()
        rows=self.jobs();self.assertEqual(len(rows),52)
        self.assertEqual({r['episode_key'] for r in rows},{f'show:77:{s}:{n}' for s,n,_ in episodes if s!=4})
        self.assertTrue(all(r['quality']=='2160' and r['fallback'] for r in rows))
        self.assertTrue(all(show_options.minimum_for_request(self.store,r)==12 for r in rows))
        self.assertTrue(all(json.loads(r['body'])['plan']=='all' and json.loads(r['body'])['planParent']==item['id'] for r in rows))
        self.assertEqual(config.read_bytes(),before)
        self.assertEqual(self.jobs('discover_series_plan')[0]['download_future'],0)

    def test_show_preserves_saved_show_and_episode_quality_overrides(self):
        item=self.series([(1,1,'2020-01-01'),(2,1,'2020-01-02')])
        show_options.save(self.app,self.store,item,dict(quality='720'))
        show_options.save(self.app,self.store,item,dict(minimum=23))
        with self.store.connect() as db:db.execute("UPDATE managed_media SET monitor=0,quality='2160',fallback=1 WHERE key='show:77:1:1'")
        self.store.set('discover_engine_quality','1080')
        self.assertEqual(self.post(item).status_code,202);self.apply_series()
        rows={r['episode_key']:r for r in self.jobs()}
        self.assertEqual((rows['show:77:1:1']['quality'],rows['show:77:1:1']['fallback']),('2160',1))
        self.assertEqual(rows['show:77:2:1']['quality'],'720')
        self.assertTrue(all(show_options.minimum_for_request(self.store,r)==23 for r in rows.values()))

    def test_show_keeps_existing_future_opt_in_boundary_and_other_options(self):
        item=self.series();show_options.save(self.app,self.store,item,dict(favourite=True))
        discovery_engine.save_plan(self.app,self.store,item['id'],'future',True)
        with self.store.connect() as db:db.execute("UPDATE discover_series_plan SET future_from='2022-03-04' WHERE parent=?",(item['id'],))
        before=show_options.find(self.store,item)
        result=self.post(item);self.assertEqual(result.status_code,202,result.json);self.assertTrue(result.json['downloadFuture'])
        self.apply_series()
        self.assertEqual(self.jobs('discover_series_plan')[0]['future_from'],'2022-03-04')
        self.assertEqual(show_options.find(self.store,item),before)
        future=next(r for r in self.jobs() if r['episode_key']=='show:77:2:2')
        self.assertEqual(future['state'],'waiting');self.assertEqual(json.loads(future['body'])['plan'],'future')

    def test_show_all_keeps_owned_active_cancelled_and_failed_plan_requests_and_holds(self):
        item=self.series([(1,n,'2020-01-01') for n in range(1,7)])
        path=self.root/'owned.mkv';path.write_bytes(b'already saved');self.store.set('storage_base',str(self.root))
        records.mark_owned(self.store,'show:77:1:1','owned',str(path),'complete')
        originals=[]
        for number,state in [(2,'downloading'),(3,'cancelled'),(4,'failed')]:
            row,_=records.insert_request(self.store,dict(idempotency='original-'+str(number),kind='episode',title='Original',state=state,quality='720',episode_key=f'show:77:1:{number}',body=dict(item=item,plan='all',planParent=item['id'])))
            originals.append(row)
        self.hold('managed:show:77:1:3','discover',originals[1]['id'])
        self.hold('managed:show:77:1:5')
        self.assertEqual(self.post(item).status_code,202);self.apply_series();self.apply_series()
        after={r['id']:r for r in self.jobs()}
        for row in originals:self.assertEqual(after[row['id']],row)
        self.assertEqual({r['episode_key'] for r in after.values()},{f'show:77:1:{n}' for n in (2,3,4,6)})
        self.assertTrue(media_holds.blocked(self.store,'managed:show:77:1:3'));self.assertTrue(media_holds.blocked(self.store,'managed:show:77:1:5'))
        self.assertEqual(path.read_bytes(),b'already saved')

    def test_duplicate_show_clicks_keep_same_revision_lease_and_single_request_per_episode(self):
        item=self.series();self.assertEqual(self.post(item).status_code,202)
        plan=self.jobs('discover_series_plan');work=self.jobs('discover_plan_work')
        self.assertEqual(self.post(item).status_code,202)
        self.assertEqual(self.jobs('discover_series_plan'),plan);self.assertEqual(self.jobs('discover_plan_work'),work)
        self.apply_series();before=self.jobs()
        self.assertEqual(self.post(item).status_code,202);self.apply_series()
        self.assertEqual(self.jobs(),before);self.assertEqual(len(before),3)

    def test_later_show_click_renews_all_boundary_for_newly_released_episode_only(self):
        yesterday=datetime.now(ZoneInfo('Europe/London'))-timedelta(days=1)
        item=self.series([(1,1,yesterday.date().isoformat())])
        self.assertEqual(self.post(item).status_code,202)
        with self.store.connect() as db:db.execute('UPDATE discover_plan_work SET all_before=?',(yesterday.timestamp(),))
        self.apply_series();before=self.jobs()
        self.series([(2,1,service.today())]);self.apply_series()
        self.assertEqual(self.jobs(),before,'Original ALL cutoff excludes newly aired episodes until explicit request')
        result=self.post(item);self.assertEqual(result.status_code,202,result.json);self.apply_series()
        self.assertEqual({r['episode_key'] for r in self.jobs()},{'show:77:1:1','show:77:2:1'})
        self.assertEqual(next(r for r in self.jobs() if r['id']==before[0]['id']),before[0])
        self.assertFalse(self.jobs('discover_series_plan')[0]['download_future'])

    def test_show_identity_direct_title_and_global_hold_do_not_enable_bulk_plan(self):
        item=self.series()
        self.assertEqual(self.post(dict(item,externalId='88')).status_code,409)
        result=self.post(dict(item,title='Disposable.Documentary.2020.1080p.WEB-DL'))
        self.assertEqual(result.status_code,202,result.json);self.assertEqual(result.json['state'],'choice_required')
        acquisition.begin_hold(self.store)
        self.assertEqual(self.post(item).status_code,409)
        self.assertEqual(self.jobs('discover_series_plan'),[]);self.assertEqual(self.jobs(),[]);self.schedule.assert_not_called()

    def test_youtube_uses_saved_channel_quality_and_explicit_manual_scope(self):
        video,item=self.video()
        channel_downloads.save(self.store,video['channel'],dict(quality='480',videos=False,shorts=False,live=False,range_days=7,minimum_video_minutes=120))
        before=channel_downloads.preferences(self.store,video['channel'])
        response=self.post(item);self.assertEqual(response.status_code,202,response.json)
        job=self.jobs('downloads')[0];self.assertEqual(job['quality'],'480');self.assertEqual(job['state'],'queued');self.assertEqual(job['automatic'],0)
        self.assertEqual(channel_downloads.preferences(self.store,video['channel']),before)

    def test_selected_youtube_releases_only_its_hold_but_not_future_automation(self):
        video,item=self.video();self.hold('video:'+video['id'],'youtube',video['id']);self.hold('video:otherhold01','youtube','otherhold01')
        response=self.post(item);self.assertEqual(response.status_code,202,response.json)
        self.assertFalse(media_holds.blocked(self.store,'video:'+video['id']));self.assertTrue(media_holds.blocked(self.store,'video:otherhold01'))
        self.assertFalse(channel_downloads.preferences(self.store,video['channel'])['videos'])

    def test_queued_and_stopping_youtube_are_idempotent_and_keep_clear_fence(self):
        video,item=self.video()
        for state in ('queued','downloading','cancelling'):
            with self.subTest(state=state):
                with self.store.connect() as db:db.execute("INSERT OR REPLACE INTO downloads(id,title,quality,state,created,updated) VALUES(?,'Saved','720',?,1,1)",(video['id'],state))
                self.hold('video:'+video['id'],'youtube',video['id'])
                response=self.post(item);self.assertEqual(response.status_code,202,response.json)
                self.assertEqual(self.jobs('downloads')[0]['state'],state);self.assertTrue(media_holds.blocked(self.store,'video:'+video['id']))
        self.assertEqual(len(self.jobs('downloads')),1)

    def test_completed_youtube_file_keeps_bytes_and_hold(self):
        video,item=self.video();path=self.root/(video['id']+'.mp4');path.write_bytes(b'saved media');before=path.stat()
        self.hold('video:'+video['id'],'youtube',video['id'])
        response=self.post(item);self.assertEqual(response.status_code,202,response.json);self.assertEqual(response.json['state'],'complete')
        self.assertTrue(media_holds.blocked(self.store,'video:'+video['id']));self.assertEqual(self.jobs('downloads'),[])
        self.assertEqual((path.stat().st_ino,path.stat().st_mtime_ns,path.read_bytes()),(before.st_ino,before.st_mtime_ns,b'saved media'))

    def test_future_or_unverified_browse_video_does_not_release_clear(self):
        for state,changes in [('upcoming',{}),('unknown',dict(browse_only=True,metadata_pending=True))]:
            with self.subTest(state=state):
                video,item=self.video(state,**changes);self.hold('video:'+video['id'],'youtube',video['id'])
                self.assertEqual(self.post(item).status_code,409)
                self.assertTrue(media_holds.blocked(self.store,'video:'+video['id']));self.assertEqual(self.jobs('downloads'),[])

    def test_global_stop_never_releases_selected_hold_or_enables_work(self):
        video,item=self.video();self.hold('video:'+video['id'],'youtube',video['id']);acquisition.begin_hold(self.store)
        self.assertEqual(self.post(item).status_code,409)
        self.assertTrue(media_holds.blocked(self.store,'video:'+video['id']));self.assertTrue(acquisition.held(self.store));self.assertEqual(self.jobs('downloads'),[])

    def test_stopping_movie_keeps_hold_and_existing_request(self):
        managed.remember(self.store,'movie:1234567',self.movie['id'],'movie',dict(item=self.movie,automatic=True,date='2020-01-01'))
        row,_=records.insert_request(self.store,dict(idempotency='stopping',kind='movie',title='Known movie',state='cancelling',quality='1080',fallback=False,episode_key='movie:1234567',body=dict(item=self.movie,externalId='1234567')))
        self.hold('managed:movie:1234567','discover',row['id'])
        response=self.post(self.movie);self.assertIn(response.status_code,(202,409),response.json)
        self.assertTrue(media_holds.blocked(self.store,'managed:movie:1234567'))
        self.assertEqual(len(self.jobs()),1);self.assertEqual(self.jobs()[0]['state'],'cancelling');self.schedule.assert_not_called()

    def test_stopped_selected_movie_releases_only_its_hold_without_future_monitoring(self):
        key='movie:1234567';managed.remember(self.store,key,self.movie['id'],'movie',dict(item=self.movie,automatic=True,date='2020-01-01'))
        row,_=records.insert_request(self.store,dict(idempotency='stopped',kind='movie',title='Known movie',state='cancelled',quality='1080',fallback=False,episode_key=key,body=dict(item=self.movie,externalId='1234567')))
        self.hold('managed:'+key,'discover',row['id']);self.hold('managed:movie:7654321','discover','other')
        response=self.post(self.movie);self.assertEqual(response.status_code,202,response.json)
        self.assertFalse(media_holds.blocked(self.store,'managed:'+key));self.assertTrue(media_holds.blocked(self.store,'managed:movie:7654321'))
        self.assertEqual(sorted(job['state'] for job in self.jobs()),['cancelled','searching'])
        self.assertNotEqual(managed.row(self.store,key)['monitor'],1);self.assertEqual(self.schedule.call_count,1)

    def test_failed_selected_movie_requires_alternatives_without_repeating_that_source(self):
        records.remember_candidates(self.store,[dict(id='old-source',kind='movie',externalId='1234567',title='Known.Movie.1080p',quality='1080p',size=5_000_000_000,grab='https://fixture.invalid/api?key=synthetic')])
        row,_=records.insert_request(self.store,dict(idempotency='failed-selected',kind='movie',title='Known movie',state='failed',quality='1080',fallback=False,episode_key='movie:1234567',body=dict(item=self.movie,externalId='1234567',managedReleaseId='old-source')))
        records.add_attempt(self.store,row['id'],'old-source','nzb','1080p','failed','This Usenet server does not have these articles.')
        response=self.post(self.movie);self.assertEqual(response.status_code,202,response.json);self.assertEqual(response.json['state'],'choice_required')
        self.assertEqual(self.jobs()[0]['state'],'failed');self.assertEqual(len(self.jobs()),1);self.schedule.assert_not_called()

    def test_already_saved_movie_does_not_release_hold_or_queue_new_attempt(self):
        self.store.set('storage_base',str(self.root));key='movie:1234567'
        managed.remember(self.store,key,self.movie['id'],'movie',dict(item=self.movie,automatic=True,date='2020-01-01'))
        path=self.root/'Movies'/'Known movie.mkv';path.parent.mkdir();path.write_bytes(b'saved movie')
        records.mark_owned(self.store,key,'saved',str(path));self.hold('managed:'+key)
        response=self.post(self.movie);self.assertEqual(response.status_code,202,response.json);self.assertEqual(response.json['state'],'complete')
        self.assertTrue(media_holds.blocked(self.store,'managed:'+key));self.assertEqual(path.read_bytes(),b'saved movie')
        self.assertEqual(self.jobs(),[]);self.schedule.assert_not_called()


class QuickVodTests(unittest.TestCase):
    def setUp(self):
        from test_m3u_downloads import ServerDownloadTests
        self.fixture=ServerDownloadTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)

    def post(self,item):
        return self.fixture.client.post('/api/quick-download',base_url=vm.ORIGIN,json=dict(item=item),headers={'Origin':vm.ORIGIN,'X-CSRF-Token':'token'})

    def test_vod_keeps_exact_saved_source_path_and_duplicate_clicks_single_job(self):
        fixture=self.fixture;entry=next(row for row in fixture.entries if row['kind']=='movies')
        item=dict(kind='vod',source=fixture.source,path=entry['path'])
        key=media_holds.vod_key(item['source'],item['path'])
        with fixture.store.connect() as db:media_holds.hold(db,[key],[('m3u','old')]);media_holds.hold(db,[media_holds.vod_key('other',item['path'])],[])
        for _ in range(2):self.assertEqual(self.post(item).status_code,202)
        with fixture.store.connect() as db:jobs=[dict(row) for row in db.execute('SELECT * FROM m3u_downloads')]
        self.assertEqual(len(jobs),1);self.assertEqual((jobs[0]['source'],jobs[0]['path']),(item['source'],item['path']))
        self.assertFalse(media_holds.blocked(fixture.store,key));self.assertTrue(media_holds.blocked(fixture.store,media_holds.vod_key('other',item['path'])))

    def test_stopping_vod_rejects_quick_retry_without_releasing_hold(self):
        fixture=self.fixture;job=fixture.enqueue();key=media_holds.vod_key(job['source'],job['path'])
        with fixture.store.connect() as db:db.execute("UPDATE m3u_downloads SET state='cancelling'");media_holds.hold(db,[key],[('m3u',job['id'])])
        self.assertEqual(self.post(dict(kind='vod',source=job['source'],path=job['path'])).status_code,409)
        self.assertTrue(media_holds.blocked(fixture.store,key))
        with fixture.store.connect() as db:self.assertEqual(db.execute('SELECT state FROM m3u_downloads').fetchone()[0],'cancelling')


if __name__=='__main__':unittest.main()
