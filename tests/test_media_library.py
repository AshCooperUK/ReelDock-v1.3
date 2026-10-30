import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from PIL import Image
from backend.app import create_app
from backend import media_library as library, channel_downloads as channels, downloads


class MediaLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.media=Path(self.tmp.name)/'media'
        self.media.mkdir()
        self.app=create_app({'TESTING':True,'DATA_DIR':self.tmp.name,'MEDIA_DIR':str(self.media)})
        self.store=self.app.extensions['store']
        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES (?,?,1)',('channel',json.dumps({'id':'channel','name':'Jake Trains'})))

    def tearDown(self):
        self.tmp.cleanup()

    def video(self,n=1,kind='videos'):
        video=dict(id=f'video{n:06d}',channel='channel',title='Pattys & <Day>',date='2021-03-17',state='published',kind=kind)
        with self.store.connect() as db:
            db.execute('INSERT INTO videos VALUES (?,?,?,?,?,0)',(video['id'],'channel',video['date'],video['date'],json.dumps(video)))
        return video

    def test_flatten_existing_channel_preserves_playback_and_artwork(self):
        from backend.flatten_youtube import migrate
        video=self.video(); record=library.reserve(self.store,self.media,video['id'])
        legacy='tv_shows/'+record['path']
        old=self.media/legacy; old.parent.mkdir(parents=True); old.write_bytes(b'media')
        artwork=old.parent.parent/'poster.jpg'; artwork.write_bytes(b'art')
        with self.store.connect() as db:db.execute('UPDATE media_files SET path=? WHERE id=?',(legacy,video['id']))
        self.store.set('emby_root','/nas/YouTube/tv_shows')
        self.assertEqual(migrate(self.store,self.media)['moved_channels'],1)
        self.assertEqual(library.local_file(self.store,self.media,video['id']),self.media/record['path'])
        self.assertEqual((self.media/'Jake Trains/poster.jpg').read_bytes(),b'art')
        self.assertEqual(self.store.get('emby_root'),'/nas/YouTube')
        self.assertFalse((self.media/'tv_shows').exists())
        self.assertEqual(migrate(self.store,self.media)['updated_paths'],0)

    def test_flatten_refuses_existing_destination_without_changes(self):
        from backend.flatten_youtube import migrate
        video=self.video(); record=library.reserve(self.store,self.media,video['id'])
        legacy='tv_shows/'+record['path']
        old=self.media/legacy; old.parent.mkdir(parents=True); old.write_bytes(b'media')
        (self.media/'Jake Trains').mkdir()
        with self.store.connect() as db:db.execute('UPDATE media_files SET path=? WHERE id=?',(legacy,video['id']))
        with self.assertRaises(ValueError):migrate(self.store,self.media)
        self.assertEqual(old.read_bytes(),b'media')
        self.assertEqual(library.local_file(self.store,self.media,video['id']),old)

    def test_stable_same_day_paths_and_xml(self):
        one=self.video();two=self.video(2)
        first=library.reserve(self.store,self.media,one['id'])
        second=library.reserve(self.store,self.media,two['id'])
        self.assertEqual(first['path'],'Jake Trains/Season 2021/s2021.e031701 - Pattys & -Day-.mp4')
        self.assertIn('e031702',second['path'])
        self.assertEqual(library.reserve(self.store,self.media,one['id'])['path'],first['path'])
        target=library.contained(self.media,first['path']);target.parent.mkdir(parents=True);target.write_bytes(b'video')
        library.write_metadata(self.store,self.media,first)
        root=ET.parse(target.with_suffix('.nfo')).getroot()
        self.assertEqual(root.findtext('title'),one['title'])
        self.assertEqual(root.findtext('episode'),'031701')
        self.assertTrue((target.parent.parent/'tvshow.nfo').exists())

    def test_legacy_file_migration_and_playback(self):
        video=self.video()
        old=self.media/(video['id']+'.mp4');old.write_bytes(b'existing')
        self.assertEqual(library.local_file(self.store,self.media,video['id']),old)
        self.assertTrue(library.organise(self.store,self.media,video['id']))
        self.assertFalse(old.exists())
        self.assertEqual(library.local_file(self.store,self.media,video['id']).read_bytes(),b'existing')

    def test_path_escape_rejected(self):
        for path in ('../outside.mp4',str(self.media.parent/'outside.mp4'),'.'):
            with self.assertRaises(ValueError):library.contained(self.media,path)
        self.assertNotIn('/',library.safe_name('../../bad/name'))

    def test_cached_png_becomes_real_jpeg(self):
        cache=Path(self.tmp.name)/'artwork';cache.mkdir()
        data=io.BytesIO();Image.new('RGBA',(10,10)).save(data,format='PNG')
        key='a'*64;(cache/key).write_bytes(data.getvalue())
        target=self.media/'poster.jpg'
        self.assertTrue(library.artwork(self.store,'/artwork/'+key,target))
        with Image.open(target) as image:self.assertEqual(image.format,'JPEG')

    def test_auto_off_types_and_no_repeat_queue(self):
        self.video(1,'shorts');self.video(2,'videos')
        self.assertEqual(channels.schedule(self.store,self.media),0)
        channels.save(self.store,'channel',dict(videos=True,range_days=0))
        self.assertEqual(channels.schedule(self.store,self.media),1)
        self.assertEqual(channels.schedule(self.store,self.media),0)
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM downloads').fetchone()
            self.assertEqual(row['id'],'video000002');self.assertEqual(row['automatic'],1)

    def queue(self,n,days_ago,kind='videos',automatic=1,state='queued',replace=0):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        day=(datetime.now(ZoneInfo('Europe/London')).date()-timedelta(days=days_ago)).isoformat()
        video=dict(id=f'queued{n:05d}',channel='channel',title=f'Video {n}',date=day,state='published')
        if kind:video['kind']=kind
        with self.store.connect() as db:
            db.execute('INSERT INTO videos VALUES (?,?,?,?,?,0)',(video['id'],'channel',day,day,json.dumps(video)))
            db.execute("INSERT INTO downloads(id,title,quality,state,created,updated,automatic,replace_existing) VALUES(?,?,'1080',?,?,?,?,?)",
                       (video['id'],video['title'],state,n,n,automatic,replace))
        return video['id']

    def states(self):
        with self.store.connect() as db:
            return {r['id']:(r['state'],r['message']) for r in db.execute('SELECT id,state,message FROM downloads')}

    def test_saving_a_shorter_range_drops_queued_videos_outside_it(self):
        # Regression: after choosing "last 30 days", July/August videos stayed queued.
        channels.save(self.store,'channel',dict(videos=True,range_days=0))
        recent=self.queue(1,5);july=self.queue(2,70);august=self.queue(3,40);edge=self.queue(4,29)
        self.store.set('public_origin','https://test.example');self.store.set('setup_complete','1')
        with self.store.connect() as db:db.execute("INSERT INTO users VALUES(1,'owner','unused')")
        client=self.app.test_client()
        with client.session_transaction(base_url='https://test.example') as session:
            session['user_id']=1;session['csrf']='token'
        response=client.post('/api/channel-downloads/channel',base_url='https://test.example',json={'settings':{'range_days':30}},
                             headers={'Origin':'https://test.example','X-CSRF-Token':'token'})
        self.assertEqual(response.status_code,200)
        states=self.states()
        self.assertEqual(states[recent][0],'queued');self.assertEqual(states[edge][0],'queued')
        for old in (july,august):
            self.assertEqual(states[old][0],'skipped')
            self.assertIn('download range (last 30 days)',states[old][1])
        with self.store.connect() as db:
            task=db.execute("SELECT * FROM activity_tasks WHERE id='download-rules-channel'").fetchone()
        self.assertEqual(task['title'],'Jake Trains · Download settings')
        self.assertIn('Removed 2 queued downloads',task['message'])
        from backend.activity import snapshot
        self.assertIn('download-rules-channel',json.dumps(snapshot(self.store)))
        # The skipped rows stay skipped: the scheduler does not queue them again.
        self.assertEqual(channels.schedule(self.store,self.media),0)
        self.assertEqual(self.states()[july][0],'skipped')

    def test_turning_off_shorts_and_live_drops_them_but_keeps_videos_and_unknown_types(self):
        channels.save(self.store,'channel',dict(videos=True,shorts=True,live=True,range_days=0))
        video=self.queue(1,3,'videos');short=self.queue(2,3,'shorts');live=self.queue(3,3,'live');unknown=self.queue(4,3,None)
        channels.save(self.store,'channel',dict(shorts=False,live=False))
        states=self.states()
        self.assertEqual(states[video][0],'queued')
        self.assertEqual(states[unknown][0],'queued')  # The worker probes its type before any media download.
        self.assertEqual(states[short],('skipped','Removed from the queue: Shorts are turned off for this channel.'))
        self.assertEqual(states[live],('skipped','Removed from the queue: live streams are turned off for this channel.'))
        channels.save(self.store,'channel',dict(videos=False))
        states=self.states()
        for item in (video,unknown):
            self.assertEqual(states[item],('skipped','Removed from the queue: automatic downloads were turned off for this channel.'))

    def test_settings_changes_preserve_manual_selections_and_still_stop_automatic_work(self):
        channels.save(self.store,'channel',dict(videos=True,shorts=True,live=True,range_days=0))
        manual=self.queue(1,200,'shorts',automatic=0)
        replacement=self.queue(2,200,'shorts',replace=1)
        running=self.queue(3,200,'shorts',state='downloading',automatic=0)
        cancelling=self.queue(4,200,'shorts',state='cancelling')
        promoted=self.queue(5,200,'shorts')
        allowed_manual=self.queue(6,3,'videos',automatic=0)
        allowed_running=self.queue(7,3,'videos',state='downloading',automatic=0)
        automatic_running=self.queue(8,200,'shorts',state='downloading',automatic=1)
        # Asking for an automatically queued video by hand makes it a manual download.
        with self.store.connect() as db:data=json.loads(db.execute('SELECT data FROM videos WHERE id=?',(promoted,)).fetchone()[0])
        downloads.enqueue(self.store,data,'720')
        channels.save(self.store,'channel',dict(videos=True,shorts=False,live=False,range_days=30))
        states=self.states()
        self.assertEqual(states[manual],('queued',''))
        self.assertEqual(states[promoted][0],'queued')
        # A withdrawn replacement goes back to its completed state; nothing is overwritten.
        self.assertEqual(states[replacement][0],'complete')
        self.assertIn('The saved file was kept',states[replacement][1])
        # The running download is stopped through the normal cancellation path.
        self.assertEqual(states[running],('downloading',''))
        self.assertEqual(states[automatic_running],('cancelling',"Cancelled: published before this channel's download range (last 30 days)."))
        self.assertEqual(states[cancelling][0],'cancelling')
        self.assertEqual(states[allowed_manual][0],'queued')
        self.assertEqual(states[allowed_running][0],'downloading')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT replace_existing FROM downloads WHERE id=?',(replacement,)).fetchone()[0],0)
            task=db.execute("SELECT message FROM activity_tasks WHERE id='download-rules-channel'").fetchone()[0]
        self.assertIn('Removed 1 queued download',task)
        self.assertIn('Stopping 1 running download',task)

    def test_turning_automation_off_keeps_all_explicit_manual_selections_but_stops_automatic_ones(self):
        channels.save(self.store,'channel',dict(videos=True,shorts=True,live=True,range_days=30))
        manual=self.queue(1,3,'shorts',automatic=0)
        running_manual=self.queue(2,3,'shorts',state='downloading',automatic=0)
        old_manual=self.queue(5,200,'videos',automatic=0)
        automatic=self.queue(3,3,'videos')
        running_auto=self.queue(4,3,'videos',state='downloading')
        # Channel automation changes do not withdraw explicit manual selections,
        # including an older video selected outside the automatic date range.
        channels.save(self.store,'channel',dict(videos=False,shorts=False,live=False))
        states=self.states()
        self.assertEqual(states[manual][0],'queued')
        self.assertEqual(states[running_manual][0],'downloading')
        self.assertEqual(states[old_manual],('queued',''))
        self.assertEqual(states[automatic][0],'skipped')
        self.assertEqual(states[running_auto],('cancelling','Cancelled: automatic downloads were turned off for this channel.'))

    def test_replace_downloads_action_skips_videos_the_settings_exclude(self):
        old=self.queue(1,200,'videos',automatic=0,state='complete')
        recent=self.queue(2,3,'videos',automatic=0,state='complete')
        short=self.queue(3,3,'shorts',automatic=0,state='complete')
        channels.save(self.store,'channel',dict(videos=True,shorts=False,live=False,range_days=30))
        message=channels.perform(self.store,self.media,'channel','redownload_existing')
        self.assertEqual(message,"1 completed download queued for replacement. 2 not replaced because this channel's download settings exclude them.")
        states=self.states()
        self.assertEqual(states[recent][0],'queued')
        self.assertEqual((states[old][0],states[short][0]),('complete','complete'))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT replace_existing FROM downloads WHERE id=?',(old,)).fetchone()[0] or 0,0)

    def test_delete_disables_auto_and_keeps_other_files(self):
        video=self.video();old=self.media/(video['id']+'.mp4');old.write_bytes(b'existing')
        other=self.media/'unmanaged.mp4';other.write_bytes(b'keep')
        channels.save(self.store,'channel',dict(videos=True,range_days=0))
        channels.perform(self.store,self.media,'channel','delete_media')
        self.assertFalse(old.exists());self.assertTrue(other.exists())
        self.assertFalse(channels.preferences(self.store,'channel')['videos'])

    def test_replacement_intent_survives_restart(self):
        video=self.video();downloads.enqueue(self.store,video,'1080')
        downloads.update(self.store,video['id'],'complete',100)
        # The 2021 fixture is outside the default 180-day range, which now
        # applies to every channel; allow all dates for this restart test.
        channels.save(self.store,'channel',dict(range_days=0))
        channels.perform(self.store,self.media,'channel','redownload_existing')
        downloads.update(self.store,video['id'],'downloading')
        downloads.recover(self.store)
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM downloads').fetchone()
            self.assertEqual(row['state'],'queued');self.assertEqual(row['replace_existing'],1)

    def test_channel_actions_require_confirmation_and_csrf(self):
        self.store.set('public_origin','https://test.example');self.store.set('setup_complete','1')
        with self.store.connect() as db:db.execute("INSERT INTO users VALUES(1,'owner','unused')")
        client=self.app.test_client()
        with client.session_transaction(base_url='https://test.example') as session:
            session['user_id']=1;session['csrf']='token'
        def post(body,token='token'):
            return client.post('/api/channel-downloads/channel',base_url='https://test.example',json=body,headers={'Origin':'https://test.example','X-CSRF-Token':token})
        self.assertEqual(post({'action':'delete_media'},'bad').status_code,403)
        self.assertEqual(post({'action':'delete_media'}).status_code,400)
        self.assertEqual(post({'action':'delete_media','confirm':True}).status_code,200)
