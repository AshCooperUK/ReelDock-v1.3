"""Manual published selections override automation filters; automatic work retains them."""
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from backend.app import create_app
from backend import channel_downloads, download_options, downloads

ORIGIN = 'https://dev.example.com'


def days_ago(days):
    return (datetime.now(ZoneInfo('Europe/London')).date()-timedelta(days=days)).isoformat()


class ChannelRulesEverywhereTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.media = Path(self.tmp.name)/'media'
        self.media.mkdir()
        self.app = create_app({'TESTING': True, 'DATA_DIR': self.tmp.name, 'MEDIA_DIR': str(self.media)})
        self.store = self.app.extensions['store']
        self.store.set('public_origin', ORIGIN)
        self.store.set('setup_complete', '1')
        with self.store.connect() as db:
            db.execute("INSERT INTO users VALUES(1,'owner','unused')")
            db.execute('INSERT INTO channels VALUES(?,?,1)', ('channel', json.dumps({'id':'channel','name':'Test'})))
        self.client = self.app.test_client()
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session['user_id'] = 1
            session['csrf'] = 'token'

    def tearDown(self):
        self.tmp.cleanup()

    def video(self, n, age, kind='videos'):
        video = {'id': f'rulevid{n:04d}', 'title': f'Video {n}', 'channel': 'channel', 'state': 'published', 'date': days_ago(age)}
        if kind:video['kind'] = kind
        with self.store.connect() as db:
            db.execute('INSERT INTO videos VALUES(?,?,?,?,?,0)', (video['id'], 'channel', video['date'], video['date'], json.dumps(video)))
        return video

    def post(self, data):
        return self.client.post('/api/downloads', base_url=ORIGIN, json=data, headers={'Origin': ORIGIN, 'X-CSRF-Token': 'token'})

    def row(self, video_id):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM downloads WHERE id=?', (video_id,)).fetchone())

    def automate(self, **settings):
        channel_downloads.save(self.store, 'channel', {'videos': True, 'shorts': False, 'live': False, 'range_days': 30, **settings})

    @staticmethod
    def probe_returning(info, calls=None):
        def probe(args, stdout, **kwargs):
            if calls is not None:calls.append(args)
            assert '--skip-download' in args
            stdout.write(json.dumps(info).encode())
            return SimpleNamespace(returncode=0)
        return probe

    @staticmethod
    def actual_details(job, log, info):
        if not job.get('automatic'):
            payload={'id':job['id'],'width':1920,'height':1080,'duration':900,'live_status':'was_live' if info.get('was_live') else 'not_live',**info}
            (log.parent/downloads.MANUAL_DETAILS_FILE).write_text(json.dumps(payload))

    def run_tick(self, info, transfer):
        calls=[]
        def actual_transfer(store,job,stop,args,log,root):
            self.actual_details(job,log,info)
            return transfer(store,job,stop,args,log,root)
        with patch('backend.downloads.subprocess.run', side_effect=self.probe_returning(info,calls)), \
             patch('backend.downloads.run_process', side_effect=actual_transfer):
            downloads.tick(self.store, self.media, threading.Event())
        return calls

    def automatic(self,video):
        downloads.enqueue(self.store,video,'1080',automatic=True)

    # Manual requests -----------------------------------------------------

    def test_explicit_published_manual_requests_override_filters_but_keep_auth_and_csrf(self):
        self.automate()
        videos=[self.video(1,200),self.video(2,3,'shorts'),self.video(3,3,'live'),self.video(4,3),self.video(5,3,None)]
        endpoint='/api/downloads';body={'id':videos[0]['id']}
        self.assertEqual(self.app.test_client().post(endpoint,base_url=ORIGIN,json=body,headers={'Origin':ORIGIN,'X-CSRF-Token':'token'}).status_code,401)
        self.assertEqual(self.client.post(endpoint,base_url=ORIGIN,json=body,headers={'Origin':ORIGIN,'X-CSRF-Token':'wrong'}).status_code,403)
        self.assertEqual(self.client.post(endpoint,base_url=ORIGIN,json=body,headers={'Origin':'https://wrong.example','X-CSRF-Token':'token'}).status_code,403)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM downloads').fetchone()[0],0)
        for video in videos:
            self.assertEqual(self.post({'id':video['id']}).status_code,200)
            self.assertEqual(self.row(video['id'])['automatic'],0)
        self.assertEqual(self.post({'id':videos[0]['id'],'action':'retry'}).status_code,200)
        rule=channel_downloads.preferences(self.store,'channel')
        for video,reason in [(videos[0],'published before'),(videos[1],'Shorts are turned off'),(videos[2],'live streams are turned off')]:
            self.assertEqual(channel_downloads.exclusion(video,rule,False),'')
            self.assertIn(reason,channel_downloads.exclusion(video,rule,True))

    def test_channels_without_automation_allow_manual_old_and_short_videos_without_probe(self):
        old=self.video(1,900);short=self.video(2,3,'shorts')
        self.assertEqual(self.post({'id':old['id']}).status_code,200)
        self.assertEqual(self.post({'id':short['id']}).status_code,200)
        def transfer(store,job,stop,args,log,root):
            (log.parent/'video.mp4').write_bytes(b'saved');return 0,''
        for _ in range(2):self.assertEqual(self.run_tick({'width':1080,'height':1920,'duration':40},transfer),[])
        self.assertEqual(self.row(old['id'])['state'],'complete');self.assertEqual(self.row(short['id'])['state'],'complete')

    def test_manual_older_than_seven_days_keeps_range_and_survives_queue_reconciliation(self):
        self.automate(range_days=7);video=self.video(92,40)
        self.assertEqual(self.post({'id':video['id']}).status_code,200)
        rule=channel_downloads.preferences(self.store,'channel')
        self.assertEqual(rule['range_days'],7);self.assertFalse(channel_downloads.eligible(video,rule))
        self.assertEqual(channel_downloads.schedule(self.store,self.media),0)
        with self.store.connect() as db:self.assertEqual(channel_downloads.drop_unmatched(db,'channel',rule),(0,0))
        self.assertEqual((self.row(video['id'])['state'],self.row(video['id'])['automatic']),('queued',0))
        self.assertEqual(channel_downloads.preferences(self.store,'channel')['range_days'],7)

    def test_manual_download_uses_actual_details_but_does_not_inherit_minimum_length(self):
        download_options.save(self.store,{'minimum_video_minutes':5});video=self.video(1,3,None)
        self.assertEqual(self.post({'id':video['id']}).status_code,200)
        def transfer(store,job,stop,args,log,root):
            (log.parent/'video.mp4').write_bytes(b'saved');return 0,''
        self.assertEqual(self.run_tick({'width':1920,'height':1080,'duration':120},transfer),[])
        self.assertEqual(self.row(video['id'])['state'],'complete')

    def test_no_probe_when_no_setting_depends_on_type_or_length(self):
        download_options.save(self.store, {'minimum_video_minutes': 0})
        video = self.video(1, 3)
        self.assertEqual(self.post({'id': video['id']}).status_code, 200)
        calls = []

        def transfer(store, job, stop, args, log, root):
            self.actual_details(job,log,{'width':1920,'height':1080,'duration':900})
            (log.parent/'video.mp4').write_bytes(b'saved');return 0, ''
        with patch('backend.downloads.subprocess.run', side_effect=self.probe_returning({}, calls)), \
             patch('backend.downloads.run_process', side_effect=transfer):
            downloads.tick(self.store, self.media, threading.Event())
        self.assertEqual(calls, [])
        self.assertEqual(self.row(video['id'])['state'], 'complete')

    # Before the transfer ------------------------------------------------

    def test_automatic_download_type_and_minimum_length_are_checked_before_transfer(self):
        self.automate()
        download_options.save(self.store, {'minimum_video_minutes': 5})
        for n, info, message in [
            (1, {'width': 1080, 'height': 1920, 'duration': 40}, 'Not downloaded: Shorts are turned off for this channel.'),
            (2, {'was_live': True, 'duration': 3600}, 'Not downloaded: live streams are turned off for this channel.'),
            (3, {'width': 1920, 'height': 1080, 'duration': 120}, 'Not downloaded: shorter than the 5-minute minimum length.'),
            (4, {'width': 1920, 'height': 1080}, "Could not identify this video type, so this channel's download settings could not be checked."),
        ]:
            with self.subTest(info=info):
                video = self.video(n, 3, None)
                self.automatic(video)
                self.run_tick(info, AssertionError('media transfer started'))
                row = self.row(video['id'])
                self.assertEqual((row['state'], row['message']), ('skipped', message))
                self.assertFalse(downloads.library.local_file(self.store, self.media, video['id']))

    def test_eligible_manual_download_uses_payload_details_without_probe(self):
        self.automate()
        video = self.video(1, 3, None)
        self.post({'id': video['id']})

        def transfer(store, job, stop, args, log, root):
            (log.parent/'video.mp4').write_bytes(b'saved');return 0, ''
        self.assertEqual(self.run_tick({'width': 1920, 'height': 1080, 'duration': 900}, transfer),[])
        self.assertEqual(self.row(video['id'])['state'], 'complete')
        with self.store.connect() as db:
            saved = json.loads(db.execute('SELECT data FROM videos WHERE id=?', (video['id'],)).fetchone()[0])
        self.assertEqual(saved['kind'], 'videos')

    def test_automatic_request_queued_before_rules_changed_is_refused_by_worker(self):
        video = self.video(1, 100)
        self.automatic(video)
        # Settings are written directly, so the queue is not cleaned up: the
        # worker's own check must still refuse the download.
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO download_rules(channel,quality,videos,shorts,live,range_days,enabled_at) VALUES('channel','1080',1,0,0,30,0)")
        self.run_tick({'width': 1920, 'height': 1080, 'duration': 900}, AssertionError('media transfer started'))
        row = self.row(video['id'])
        self.assertEqual(row['state'], 'skipped')
        self.assertIn("published before this channel's download range", row['message'])

    # During the transfer and before saving ------------------------------

    def test_saving_settings_that_exclude_a_running_automatic_download_cancels_it(self):
        self.automate(range_days=0)
        video = self.video(1, 20)
        self.automatic(video)

        def transfer(store, job, stop, args, log, root):
            (log.parent/'video.mp4').write_bytes(b'finished')
            response = self.client.post('/api/channel-downloads/channel', base_url=ORIGIN, json={'settings': {'range_days': 7}},
                                        headers={'Origin': ORIGIN, 'X-CSRF-Token': 'token'})
            assert response.status_code == 200
            assert self.row(job['id'])['state'] == 'cancelling'
            return 0, ''
        self.run_tick({'width': 1920, 'height': 1080, 'duration': 900}, transfer)
        row = self.row(video['id'])
        self.assertEqual((row['state'], row['message']), ('cancelled', "Cancelled: published before this channel's download range (last 7 days)."))
        self.assertFalse(downloads.library.local_file(self.store, self.media, video['id']))
        self.assertFalse(list(self.media.rglob('*.mp4')))

    def test_automatic_finished_file_is_not_saved_when_rules_changed_during_transfer(self):
        self.automate(range_days=0)
        download_options.save(self.store, {'minimum_video_minutes': 5})
        for n, change, message in [
            (1, "UPDATE download_rules SET range_days=7", "Not saved: published before this channel's download range (last 7 days)."),
            (2, "UPDATE download_rules SET range_days=0,videos=0,shorts=1", 'Not saved: normal videos are turned off for this channel.'),
            (3, "INSERT OR REPLACE INTO settings VALUES('download_minimum_video_minutes','20')", 'Not saved: shorter than the 20-minute minimum length.'),
        ]:
            with self.subTest(change=change):
                self.automate(range_days=0)
                download_options.save(self.store, {'minimum_video_minutes': 5})
                video = self.video(n, 20)
                self.automatic(video)

                def transfer(store, job, stop, args, log, root):
                    (log.parent/'video.mp4').write_bytes(b'finished')
                    with store.connect() as db:db.execute(change)
                    return 0, ''
                self.run_tick({'width': 1920, 'height': 1080, 'duration': 900}, transfer)
                row = self.row(video['id'])
                self.assertEqual((row['state'], row['message']), ('skipped', message))
                self.assertFalse(downloads.library.local_file(self.store, self.media, video['id']))

    def test_queued_and_running_manual_choice_survives_channel_filter_changes(self):
        self.automate(range_days=0);video=self.video(90,100)
        self.assertEqual(self.post({'id':video['id']}).status_code,200)
        channel_downloads.save(self.store,'channel',{'range_days':1,'videos':False,'shorts':True,'minimum_video_minutes':20})
        self.assertEqual(self.row(video['id'])['state'],'queued')
        def transfer(store,job,stop,args,log,root):
            (log.parent/'video.mp4').write_bytes(b'explicit manual choice')
            response=self.client.post('/api/channel-downloads/channel',base_url=ORIGIN,json={'settings':{'range_days':7,'shorts':False,'live':True}},headers={'Origin':ORIGIN,'X-CSRF-Token':'token'})
            self.assertEqual(response.status_code,200);self.assertNotEqual(self.row(job['id'])['state'],'cancelling')
            return 0,''
        self.assertEqual(self.run_tick({'width':1920,'height':1080,'duration':90},transfer),[])
        self.assertEqual(self.row(video['id'])['state'],'complete')

    def test_manual_replacement_ignores_automation_changes_and_saves_exact_choice(self):
        self.automate(range_days=0);video=self.video(91,100,'shorts');target=self.queue_replacement(video)
        def transfer(store,job,stop,args,log,root):
            (log.parent/'video.mp4').write_bytes(b'explicit replacement')
            channel_downloads.save(store,'channel',{'range_days':1,'videos':True,'shorts':False,'minimum_video_minutes':20})
            return 0,''
        self.assertEqual(self.run_tick({'width':1080,'height':1920,'duration':40},transfer),[])
        self.assertEqual(self.row(video['id'])['state'],'complete')
        self.assertEqual(downloads.library.local_file(self.store,self.media,video['id']).read_bytes(),b'explicit replacement')

    # Replacements --------------------------------------------------------

    def queue_replacement(self, video, automatic=False):
        target = self.media/(video['id']+'.mp4')
        target.write_bytes(b'existing file')
        with self.store.connect() as db:
            db.execute("INSERT INTO downloads(id,title,quality,state,created,updated,automatic,replace_existing) VALUES(?,?,'1080','queued',1,1,?,1)",
                       (video['id'], video['title'],int(automatic)))
        return target

    def test_automatic_replacement_cannot_bypass_rules_or_overwrite_after_becoming_ineligible(self):
        self.automate(range_days=0)
        video = self.video(1, 20)
        target = self.queue_replacement(video,automatic=True)

        def transfer(store, job, stop, args, log, root):
            (log.parent/'video.mp4').write_bytes(b'replacement')
            with store.connect() as db:db.execute('UPDATE download_rules SET range_days=7')
            return 0, ''
        self.run_tick({'width': 1920, 'height': 1080, 'duration': 900}, transfer)
        self.assertEqual(target.read_bytes(), b'existing file')
        row = self.row(video['id'])
        self.assertEqual((row['state'], row['replace_existing']), ('complete', 0))
        self.assertIn('Replacement stopped', row['message'])
        self.assertIn('The saved file was kept', row['message'])

    def test_automatic_replacement_already_outside_the_rules_never_starts_a_transfer(self):
        self.automate()
        video = self.video(1, 3, 'shorts')
        target = self.queue_replacement(video,automatic=True)
        self.run_tick({'width': 1080, 'height': 1920, 'duration': 40}, AssertionError('media transfer started'))
        self.assertEqual(target.read_bytes(), b'existing file')
        self.assertEqual(self.row(video['id'])['state'], 'complete')

    def test_automatic_settings_cancelling_a_running_replacement_keeps_the_saved_file(self):
        self.automate(range_days=0)
        video = self.video(1, 20)
        target = self.queue_replacement(video,automatic=True)

        def transfer(store, job, stop, args, log, root):
            (log.parent/'video.mp4').write_bytes(b'replacement')
            channel_downloads.save(store, 'channel', {'range_days': 7})
            return 0, ''
        self.run_tick({'width': 1920, 'height': 1080, 'duration': 900}, transfer)
        self.assertEqual(target.read_bytes(), b'existing file')
        row = self.row(video['id'])
        self.assertEqual((row['state'], row['replace_existing']), ('complete', 0))
        self.assertIn('published before', row['message'])

    def test_eligible_replacement_still_replaces_the_file(self):
        self.automate(range_days=0)
        video = self.video(1, 20)
        self.queue_replacement(video)

        def transfer(store, job, stop, args, log, root):
            (log.parent/'video.mp4').write_bytes(b'replacement');return 0, ''
        self.run_tick({'width': 1920, 'height': 1080, 'duration': 900}, transfer)
        self.assertEqual(self.row(video['id'])['state'], 'complete')
        self.assertEqual(downloads.library.local_file(self.store, self.media, video['id']).read_bytes(), b'replacement')


if __name__ == '__main__':
    unittest.main()
