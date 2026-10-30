"""Independent manual-download intent, output verification and retry guards."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app import create_app
from backend import channel_downloads, download_options, downloads

ID = 'abcdefghijk'
CHANNEL = 'UC'+'a'*22
MEDIA = b'\x00\x00\x00\x20ftypisom' + b'fixture media bytes'


class ManualYoutubeFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root/'media';self.media.mkdir()
        self.app = create_app(dict(TESTING=True, DATA_DIR=str(self.root/'data'), MEDIA_DIR=str(self.media)))
        self.store = self.app.extensions['store']
        self.store.set('setup_complete', '1')
        self.video = dict(id=ID, title='Explicit selected video', channel=CHANNEL, state='published', kind='videos', date='2020-01-01')
        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES(?,?,1)', (CHANNEL, json.dumps({'id':CHANNEL,'name':'Fixture channel'})))
            db.execute('INSERT INTO videos VALUES(?,?,?,?,?,0)', (ID, CHANNEL, self.video['date'], self.video['date'], json.dumps(self.video)))
        channel_downloads.save(self.store, CHANNEL, dict(videos=False, shorts=True, live=False, range_days=1, minimum_video_minutes=120))
        download_options.save(self.store, dict(convert_fallback=False, force_transcode=False, hardware_encoding=False, authenticated_retry=True, sponsorblock='off'))
        downloads.enqueue(self.store, self.video, '720')
        downloads.update(self.store, ID, 'downloading')
        with self.store.connect() as db: self.job = dict(db.execute('SELECT * FROM downloads WHERE id=?',(ID,)).fetchone())
        self.stop = threading.Event();self.calls = []

    def information(self, **changes):
        return dict(id=ID, duration=45, width=1080, height=1920, is_live=False, live_status='not_live', **changes)

    def produce(self, folder, info=None, extension='mp4'):
        (folder/('video.'+extension)).write_bytes(MEDIA)
        (folder/downloads.MANUAL_DETAILS_FILE).write_text(json.dumps(self.information() if info is None else info))

    def invoke(self, handler=None, *, cookie=False):
        if cookie: (self.store.directory/'youtube-cookies.txt').write_text('synthetic cookie snapshot')
        def process(store, job, stop, args, log, root):
            self.calls.append(list(args))
            if handler: return handler(args, log.parent)
            self.produce(log.parent)
            return 0, ''
        with patch('backend.downloads.run_process',side_effect=process), \
             patch('backend.downloads.subprocess.run',side_effect=AssertionError('Manual downloads must not depend on a preliminary probe')), \
             patch('backend.downloads.library.write_metadata',return_value='Saved metadata'), \
             patch('backend.cookies.allow_downloads',return_value=cookie), \
             patch('backend.watched_status.cookie_session_file_status',return_value=('unchecked','')):
            return downloads.run_download(self.store,self.media,self.job,self.stop)

    def published(self):
        return list(self.media.rglob('*.mp4'))

    def test_manual_published_video_bypasses_automation_filters_and_preliminary_probe(self):
        self.assertEqual(self.invoke(), 'Saved metadata')
        self.assertEqual(len(self.calls),1)
        self.assertNotIn('--write-info-json', self.calls[0])
        self.assertIn('--no-simulate', self.calls[0])
        index = self.calls[0].index('--print-to-file')
        self.assertEqual(self.calls[0][index+1], downloads.MANUAL_DETAILS_TEMPLATE)
        self.assertEqual(Path(self.calls[0][index+2]).name, downloads.MANUAL_DETAILS_FILE)
        self.assertIn('--match-filter', self.calls[0])
        self.assertIn('is_upcoming', self.calls[0][self.calls[0].index('--match-filter')+1])
        self.assertEqual(len(self.published()),1)
        self.assertEqual(self.published()[0].read_bytes(),MEDIA)
        self.assertFalse(list(self.media.glob('.reeldock-download-*')))
        self.assertEqual(channel_downloads.preferences(self.store,CHANNEL)['minimum_video_minutes'],120)

    def test_later_channel_automation_changes_do_not_withdraw_manual_intent(self):
        def transfer(args, folder):
            channel_downloads.save(self.store,CHANNEL,dict(videos=False,shorts=False,live=True,range_days=1,minimum_video_minutes=120))
            self.produce(folder, dict(id=ID,duration=25,width=1920,height=1080,live_status='not_live'))
            return 0,''
        self.invoke(transfer)
        self.assertEqual(len(self.published()),1)
        self.assertEqual(self.published()[0].read_bytes(),MEDIA)

    def test_unknown_scheduled_and_current_live_cache_records_never_start_payload(self):
        for state in ('unknown','scheduled','live'):
            with self.subTest(state=state):
                with self.store.connect() as db:db.execute('UPDATE videos SET data=? WHERE id=?',(json.dumps(dict(self.video,state=state,metadata_pending=True)),ID))
                self.calls.clear()
                with self.assertRaises(ValueError):self.invoke()
                self.assertEqual(self.calls,[])
                self.assertEqual(self.published(),[])

    def test_actual_live_or_upcoming_info_never_publishes_even_after_successful_transfer(self):
        for facts in ({'is_live':True},{'is_upcoming':True},{'live_status':'is_live'},{'live_status':'is_upcoming'}):
            with self.subTest(facts=facts):
                def transfer(args,folder):self.produce(folder,dict(self.information(),**facts));return 0,''
                with self.assertRaises(downloads.DownloadSkipped):self.invoke(transfer)
                self.assertEqual(self.published(),[])

    def test_missing_mismatched_malformed_oversize_nonfinite_and_symlink_info_are_rejected(self):
        outside=self.root/'outside-info.json';outside.write_text('private synthetic source must not be read')
        cases=['missing','wrong-id','malformed','oversize','nan','bool','negative','symlink']
        for case in cases:
            with self.subTest(case=case):
                def transfer(args,folder):
                    (folder/'video.mp4').write_bytes(MEDIA);path=folder/downloads.MANUAL_DETAILS_FILE
                    if case=='wrong-id':path.write_text(json.dumps({'id':'BBBBBBBBBBB'}))
                    elif case=='malformed':path.write_bytes(b'{')
                    elif case=='oversize':path.write_bytes(b' ' * (downloads.MANUAL_DETAILS_LIMIT+1))
                    elif case=='nan':path.write_text(json.dumps({'id':ID,'duration':float('nan')}))
                    elif case=='bool':path.write_text(json.dumps({'id':ID,'width':True}))
                    elif case=='negative':path.write_text(json.dumps({'id':ID,'height':-1}))
                    elif case=='symlink':path.symlink_to(outside)
                    return 0,''
                with self.assertRaisesRegex(ValueError,'could not be verified'):self.invoke(transfer)
                self.assertEqual(self.published(),[])
                self.assertEqual(outside.read_text(),'private synthetic source must not be read')

    def test_each_failed_attempt_discards_its_appended_details_before_fresh_fallback(self):
        def process(args, folder):
            if args[0]=='ffmpeg':
                Path(args[-1]).write_bytes(MEDIA)
                return 0,''
            path=folder/downloads.MANUAL_DETAILS_FILE
            self.assertFalse(path.exists(), 'Every transfer must begin without stale print-to-file facts')
            if sum(call[0]!='ffmpeg' for call in self.calls)==1:
                path.write_text(json.dumps(dict(self.information(),id='BBBBBBBBBBB'))+'\n')
                return 1,'ERROR: Requested format is not available'
            (folder/'video.webm').write_bytes(MEDIA)
            with path.open('a') as target:target.write(json.dumps(self.information())+'\n')
            return 0,''
        self.invoke(process)
        self.assertEqual(sum(call[0]!='ffmpeg' for call in self.calls),2)
        self.assertEqual(self.published()[0].read_bytes(),MEDIA)

    def test_success_without_new_details_cannot_reuse_failed_attempt_identity(self):
        def process(args, folder):
            path=folder/downloads.MANUAL_DETAILS_FILE
            if len(self.calls)==1:
                path.write_text(json.dumps(self.information()))
                return 1,'ERROR: Requested format is not available'
            self.assertFalse(path.exists())
            (folder/'video.mp4').write_bytes(MEDIA)
            return 0,''
        with self.assertRaisesRegex(ValueError,'could not be verified'):self.invoke(process)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.published(),[])

    def test_manual_format_fallback_converts_with_old_preference_off_and_keeps_requested_height(self):
        def process(args,folder):
            if args[0]=='ffmpeg':
                self.assertIn('720',args[args.index('-vf')+1])
                Path(args[-1]).write_bytes(MEDIA+b' converted')
                return 0,''
            if sum(args[0]!='ffmpeg' for args in self.calls)==1:return 1,'ERROR: Requested format is not available'
            self.produce(folder,extension='webm')
            return 0,''
        self.invoke(process)
        transfers=[args for args in self.calls if args[0]!='ffmpeg']
        self.assertEqual(len(transfers),2)
        self.assertNotEqual(transfers[0][transfers[0].index('-f')+1],transfers[1][transfers[1].index('-f')+1])
        self.assertEqual(sum(args[0]=='ffmpeg' for args in self.calls),1)
        self.assertEqual(self.published()[0].read_bytes(),MEDIA+b' converted')
        self.assertFalse(download_options.read(self.store)['convert_fallback'],'manual fallback does not mutate the saved automation preference')

    def test_cookie_and_public_format_pairs_are_bounded_to_four_unique_attempts(self):
        with self.assertRaises(ValueError):self.invoke(lambda args,folder:(1,'ERROR: Requested format is not available'),cookie=True)
        self.assertEqual(len(self.calls),4)
        modes=[('--cookies' in args,args[args.index('-f')+1]) for args in self.calls]
        self.assertEqual(len(set(modes)),4)
        self.assertEqual([mode[0] for mode in modes],[True,True,False,False])
        self.assertEqual(self.published(),[])

    def test_stop_or_cancellation_prevents_another_attempt_and_post_success_publication(self):
        for gate,code in [('stop',1),('stop',0),('cancel',1),('cancel',0)]:
            with self.subTest(gate=gate,code=code):
                self.stop.clear();self.calls.clear();downloads.update(self.store,ID,'downloading')
                def process(args,folder):
                    if code==0:self.produce(folder)
                    if gate=='stop':self.stop.set()
                    else:downloads.update(self.store,ID,'cancelling')
                    return code,'ERROR: Requested format is not available'
                with self.assertRaises((InterruptedError,ValueError)):self.invoke(process)
                self.assertEqual(len(self.calls),1)
                self.assertEqual(self.published(),[])

    def test_hold_and_global_pause_prevent_a_manual_payload(self):
        for key in ('acquisition_hold','download_pause_downloads'):
            with self.subTest(gate=key):
                self.store.set(key,'1' if key=='acquisition_hold' else 'true');self.calls.clear()
                try:
                    with self.assertRaises(downloads.DownloadPaused):self.invoke()
                    self.assertEqual(self.calls,[])
                finally:self.store.set(key,'0' if key=='acquisition_hold' else 'false')

    def test_stop_after_failed_hardware_conversion_never_launches_cpu_fallback(self):
        download_options.save(self.store,{'hardware_encoding':True})
        def process(args,folder):
            if args[0]=='ffmpeg':
                if 'h264_vaapi' in args:
                    self.stop.set()
                    return 1,'Hardware conversion failed'
                Path(args[-1]).write_bytes(MEDIA)
                return 0,''
            if sum(args[0]!='ffmpeg' for args in self.calls)==1:return 1,'ERROR: Requested format is not available'
            self.produce(folder,extension='webm')
            return 0,''
        with patch('backend.hardware_encoding.available_device',return_value='/dev/dri/fixture'):
            with self.assertRaises(InterruptedError):self.invoke(process)
        conversions=[args for args in self.calls if args[0]=='ffmpeg']
        self.assertEqual(len(conversions),1,'cancellation cannot start another conversion attempt')
        self.assertIn('h264_vaapi',conversions[0])
        self.assertEqual(self.published(),[])

    def test_automatic_requests_still_obey_channel_automation_filters(self):
        self.job['automatic']=1
        with self.assertRaises(downloads.DownloadSkipped):self.invoke()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.published(),[])


if __name__=='__main__':unittest.main()
