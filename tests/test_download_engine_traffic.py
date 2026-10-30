"""Engine routing at real download boundaries, using only disposable fixtures."""
import subprocess
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend import auto_downloads, download_options, downloads, metadata_control
from backend.vpn import transport
from tests import test_auto_downloads as fixture
from tests import test_m3u_downloads as vod_fixture


class DownloadEngineTrafficTests(unittest.TestCase):
    setUp = fixture.AutoDownloadTests.setUp
    tearDown = fixture.AutoDownloadTests.tearDown
    place = fixture.AutoDownloadTests.place

    def test_selected_engine_options_do_not_use_the_conservative_overview_gate(self):
        with patch('backend.vpn.downloads_blocked', side_effect=lambda engine=None: engine != 'youtube') as gate:
            self.assertTrue(download_options.read(self.store)['pause_downloads'])
            self.assertFalse(download_options.read(self.store, 'youtube')['pause_downloads'])
            self.assertFalse(download_options.for_channel(self.store, '', 'youtube')['pause_downloads'])
            self.assertTrue(download_options.read(self.store, 'nzb')['pause_downloads'])
        self.assertEqual([call.args for call in gate.call_args_list], [(), ('youtube',), ('youtube',), ('nzb',)])
        self.assertEqual(self.store.get('download_pause_downloads', 'false'), 'false')

    def test_youtube_attempt_ignores_an_unrelated_engine_gate_but_obeys_its_own(self):
        with patch('backend.vpn.downloads_blocked', side_effect=lambda engine=None: engine != 'youtube'):
            downloads.check_attempt(self.store, {'id': 'abcdefghijk'}, self.stop)
        with patch('backend.vpn.downloads_blocked', return_value=True) as gate:
            with self.assertRaises(downloads.DownloadPaused):
                downloads.check_attempt(self.store, {'id': 'abcdefghijk'}, self.stop)
            gate.assert_called_once_with('youtube')

    def test_youtube_child_and_ffmpeg_keep_the_role_and_existing_environment(self):
        process = Mock(returncode=0)
        process.poll.return_value = 0
        seen = []
        def launch(args, **kwargs):
            seen.append((transport.traffic_class(), kwargs['env']))
            return process
        with patch.dict('os.environ', {'LD_PRELOAD': '/fixed/interposer.so', 'REELDOCK_TRAFFIC': 'metadata'}), \
             patch('backend.downloads.check_attempt'), patch('backend.downloads.subprocess.Popen', side_effect=launch), \
             patch('backend.downloads.update_progress'):
            for index, command in enumerate((['python', '-m', 'yt_dlp', '--', 'fixture'], ['ffmpeg', '-i', 'fixture', 'output'])):
                self.assertEqual(downloads.run_process(self.store, {'id': 'abcdefghijk'}, self.stop, command,
                                                       self.root / f'process-{index}.log', self.root), (0, ''))
        self.assertEqual([role for role, _ in seen], [transport.YOUTUBE, transport.YOUTUBE])
        self.assertTrue(all(env['REELDOCK_TRAFFIC'] == 'youtube' and env['LD_PRELOAD'] == '/fixed/interposer.so' for _, env in seen))

    def test_manual_video_probe_and_catalogue_search_have_distinct_child_roles(self):
        from backend.discover_sources import search_youtube
        seen = []
        def run(args, **kwargs):
            seen.append(kwargs['env']['REELDOCK_TRAFFIC'])
            return subprocess.CompletedProcess(args, 0, b'{}' if kwargs.get('capture_output') and not kwargs.get('text') else '{"entries": []}')
        with patch('subprocess.run', side_effect=run):
            self.assertEqual(auto_downloads._extract_youtube(self.store, 'https://youtu.be/abcdefghijk', 'video'), {})
            search_youtube('fixture search')
        self.assertEqual(seen, ['youtube', 'metadata'])

    def test_auto_stream_transfer_and_nested_metadata_restore_separate_roles(self):
        seen = []
        def fetch(_url):
            seen.append(transport.traffic_class())
            for action in (metadata_control.vod_task, metadata_control.youtube_task):
                action(self.store, lambda: seen.append(transport.traffic_class()))
            seen.append(transport.traffic_class())
            return '.mp4', 5, [b'video']
        path = auto_downloads._save_stream(self.store, 'Fixture (2026).mp4', 'https://provider.example/video', fetch, self.stop)
        self.assertEqual(path.read_bytes(), b'video')
        self.assertEqual(seen, [transport.VOD, transport.METADATA, transport.METADATA, transport.VOD])

    def test_artwork_remains_metadata_inside_a_vod_transfer(self):
        from backend.m3u_player import cached_image_path
        seen = []
        response = Mock(status=200)
        response.read.return_value = b'\x89PNGfixture'
        def upstream(_url):
            seen.append(transport.traffic_class())
            return Mock(), response, 'https://artwork.example/image'
        with patch('backend.m3u_player.upstream', side_effect=upstream):
            saved = transport.vod(cached_image_path)(self.store, 'https://artwork.example/image')
        self.assertEqual(saved.read_bytes(), b'\x89PNGfixture')
        self.assertEqual(seen, [transport.METADATA])

    def test_auto_intake_checks_each_engine_before_claiming_or_accepting(self):
        paths = {
            'youtube': self.place('Youtube.strm', 'https://youtu.be/abcdefghijk'),
            'vod': self.place('Vod.strm', 'https://provider.example/video.mp4'),
            'nzb': self.place('Fixture.nzb', '<nzb/>'),
            'torrent': self.place('Fixture.torrent', b'd8:info'),
        }
        seen = []
        def youtube(_app, _store, ident, name, _stop):
            seen.append((ident, name))
        with patch('backend.vpn.downloads_blocked', side_effect=lambda engine=None: engine != 'youtube') as gate:
            auto_downloads.scan(self.app, self.store, self.stop, {'strm', 'nzb', 'torrent'}, youtube=youtube,
                                torrent=Mock(side_effect=AssertionError('Blocked torrent started')),
                                nzb=Mock(side_effect=AssertionError('Blocked NZB started')),
                                fetch=Mock(side_effect=AssertionError('Blocked VOD started')))
        self.assertEqual(seen, [('abcdefghijk', 'Youtube.strm')])
        self.assertEqual({call.args[0] for call in gate.call_args_list}, {'youtube', 'vod', 'nzb', 'torrent'})
        self.assertFalse(paths['youtube'].exists())
        self.assertTrue(all(paths[engine].is_file() for engine in ('vod', 'nzb', 'torrent')))
        with self.store.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT name,state FROM auto_downloads')], [('Youtube.strm', 'done')])

    def test_global_pause_still_stops_every_auto_intake_before_engine_checks(self):
        self.place('Youtube.strm', 'https://youtu.be/abcdefghijk')
        self.store.set('download_pause_downloads', 'true')
        with patch('backend.vpn.downloads_blocked', return_value=False) as gate, patch('backend.auto_downloads._claim') as claim:
            auto_downloads.scan(self.app, self.store, self.stop, {'strm'})
        gate.assert_not_called()
        claim.assert_not_called()

    def test_large_blocked_prefix_does_not_fill_the_eight_eligible_arrival_batch(self):
        for number in range(128):self.place(f'a-{number:03d}.nzb', '<nzb/>')
        allowed = self.place('z-youtube.strm', 'https://youtu.be/abcdefghijk')
        original = auto_downloads.os.scandir
        class OrderedEntries:
            def __enter__(self):
                with original(self_folder) as entries:self.entries=sorted(entries,key=lambda entry:entry.name)
                return iter(self.entries)
            def __exit__(self,*_):pass
        self_folder=self.watch
        seen=[]
        with patch('backend.auto_downloads.os.scandir', side_effect=lambda _folder:OrderedEntries()), \
             patch('backend.vpn.downloads_blocked', side_effect=lambda engine=None:engine!='youtube'), \
             patch('backend.auto_downloads.start_nzb', side_effect=AssertionError('Blocked NZB started')):
            auto_downloads.scan(self.app,self.store,self.stop,{'strm','nzb'},
                                youtube=lambda _app,_store,identity,name,_stop:seen.append((identity,name)))
            auto_downloads.scan(self.app,self.store,self.stop,{'strm','nzb'},
                                youtube=lambda _app,_store,identity,name,_stop:seen.append((identity,name)))
        self.assertEqual(seen,[('abcdefghijk','z-youtube.strm')])
        self.assertFalse(allowed.exists())
        self.assertTrue(all((self.watch/f'a-{number:03d}.nzb').is_file() for number in range(128)))
        with self.store.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT name,state FROM auto_downloads')],[('z-youtube.strm','done')])

    def test_candidate_filter_retains_fresh_policy_check_before_claim(self):
        self.place('Youtube.strm','https://youtu.be/abcdefghijk')
        with patch('backend.vpn.downloads_blocked', side_effect=[False,True]) as gate, \
             patch('backend.auto_downloads._claim', side_effect=AssertionError('Policy changed before claim')):
            auto_downloads.scan(self.app,self.store,self.stop,{'strm'})
        self.assertEqual([call.args for call in gate.call_args_list],[('youtube',),('youtube',)])

    def test_candidate_filter_keeps_the_eight_download_batch_bound(self):
        for number in range(9):self.place(f'Youtube-{number}.strm','https://youtu.be/abcdefghijk')
        seen=[]
        with patch('backend.vpn.downloads_blocked', return_value=False):
            auto_downloads.scan(self.app,self.store,self.stop,{'strm'},
                                youtube=lambda _app,_store,_identity,name,_stop:seen.append(name))
            self.assertEqual(len(seen),8)
            self.assertEqual(len(list(self.watch.glob('*.strm'))),1)
            auto_downloads.scan(self.app,self.store,self.stop,{'strm'},
                                youtube=lambda _app,_store,_identity,name,_stop:seen.append(name))
        self.assertEqual(len(seen),9)
        self.assertEqual(len(set(seen)),9)


class VodEngineGateTests(unittest.TestCase):
    setUp = vod_fixture.ServerDownloadTests.setUp
    tearDown = vod_fixture.ServerDownloadTests.tearDown
    enqueue = vod_fixture.ServerDownloadTests.enqueue
    response = vod_fixture.ServerDownloadTests.response

    def test_allowed_vod_transfer_finishes_while_other_protected_engines_are_blocked(self):
        from backend import m3u_downloads
        job = self.enqueue()
        payload = b'\x00\x00\x00\x20ftypisom' + b'x' * 6000
        roles = []
        def upstream(*args):
            roles.append(transport.traffic_class())
            return self.response(payload)
        with patch('backend.vpn.downloads_blocked', side_effect=lambda engine=None: engine != 'vod') as gate, \
             patch('backend.m3u_player.upstream', side_effect=upstream):
            m3u_downloads.transfer(self.app, self.store, job, threading.Event())
        self.assertEqual(roles, [transport.VOD])
        self.assertTrue(gate.called)
        self.assertEqual({call.args for call in gate.call_args_list}, {('vod',)})
        with self.store.connect() as db:
            result = db.execute('SELECT state,destination FROM m3u_downloads WHERE id=?', (job['id'],)).fetchone()
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(Path(result['destination']).read_bytes(), payload)


class NativeBoundaryTrafficTests(unittest.TestCase):
    def test_native_engine_spawn_enters_its_role_in_a_new_thread(self):
        from backend.downloaders.nzb.engine import NzbEngine
        from backend.downloaders.torrent.engine import TorrentEngine
        seen = []
        for engine, mark in ((NzbEngine, transport.NZB), (TorrentEngine, transport.TORRENT)):
            instance = object.__new__(engine)
            instance._threads = []
            transport.metadata(instance._spawn)(lambda: seen.append(transport.traffic_class()), 'synthetic-role')
            instance._threads[0].join(2)
            self.assertFalse(instance._threads[0].is_alive())
            self.assertEqual(seen[-1], mark)

    def test_nzb_direct_puller_threads_also_enter_the_nzb_role(self):
        from backend.downloaders.nzb.engine import NzbEngine
        engine = object.__new__(NzbEngine)
        engine._slots = Mock()
        seen = []
        engine._pull = lambda *args: seen.append(transport.traffic_class())
        transport.metadata(engine._fetch_server)(Mock(), {'connections': 2}, [Mock(), Mock()], SimpleNamespace())
        self.assertEqual(seen, [transport.NZB, transport.NZB])

    def test_native_manifest_acquisition_uses_engine_role_instead_of_metadata(self):
        from backend.downloaders.nzb.engine import NzbEngine
        from backend.downloaders.torrent.engine import TorrentEngine
        seen = []
        def boundary(*args, **kwargs):
            seen.append(transport.traffic_class())
            raise OSError('Synthetic acquisition boundary')
        opener = Mock()
        opener.open.side_effect = boundary
        with patch('backend.downloaders.nzb.engine.indexers._get', side_effect=boundary), \
             patch('backend.downloaders.torrent.engine.urllib.request.build_opener', return_value=opener):
            for engine, method in ((NzbEngine, 'preview'), (TorrentEngine, '_fetch_torrent')):
                with self.assertRaises(ValueError if engine is TorrentEngine else OSError):
                    transport.metadata(getattr(object.__new__(engine), method))('https://provider.example/release')
        self.assertEqual(seen, [transport.NZB, transport.TORRENT])

    def test_nntp_socket_creation_has_nzb_role_even_inside_metadata(self):
        from backend.downloaders.nzb.nntp import NntpClient
        seen = []
        def connect(*args, **kwargs):
            seen.append(transport.traffic_class())
            raise OSError('Synthetic socket boundary')
        with patch('backend.downloaders.nzb.nntp.socket.create_connection', side_effect=connect):
            with self.assertRaisesRegex(OSError, 'Synthetic'):
                transport.metadata(NntpClient('server.example', 563, True, '', '').connect)()
        self.assertEqual(seen, [transport.NZB])

    def test_independent_dht_thread_enters_torrent_scope(self):
        from backend.downloaders.torrent.dht import DHTNode
        seen, targets = [], []
        real_thread = threading.Thread
        def socket(*args, **kwargs):
            seen.append(transport.traffic_class())
            result = Mock()
            result.getsockname.return_value = ('0.0.0.0', 12345)
            return result
        def thread(*args, **kwargs):
            targets.append(kwargs['target'])
            return Mock()
        with patch('backend.downloaders.torrent.dht.socket.socket', side_effect=socket), \
             patch('backend.downloaders.torrent.dht.threading.Thread', side_effect=thread), \
             patch.object(DHTNode, '_loop', lambda self: seen.append(transport.traffic_class())):
            node = transport.metadata(DHTNode)()
            worker = real_thread(target=targets[0])
            worker.start(); worker.join(2)
            self.assertFalse(worker.is_alive())
            node.close()
        self.assertEqual(seen, [transport.TORRENT, transport.TORRENT])

    def test_shared_native_sidecar_writer_is_metadata_for_both_engines(self):
        from backend.downloaders.torrent.metadata import _save
        seen = []
        with patch('backend.downloaders.torrent.metadata._save_active', side_effect=lambda *args: seen.append(transport.traffic_class())):
            for role in (transport.nzb, transport.torrent):
                role(_save)(None, Path('/unused'), SimpleNamespace(engine=SimpleNamespace(source_type='fixture')), 0, 'fixture', 'movie', {})
        self.assertEqual(seen, [transport.METADATA, transport.METADATA])

    def test_replacement_gate_uses_current_request_engine_and_retains_cancellation(self):
        from backend.media_management.files import replacement_allowed
        request = dict(id='fixture', episode_key='episode:fixture', engine='nzb', stop_generation=4)
        current = dict(state='running', stop_generation=4, engine='vod')
        store = Mock()
        store.connect.return_value.__enter__ = Mock(return_value=store)
        store.connect.return_value.__exit__ = Mock(return_value=False)
        store.execute.return_value.fetchone.side_effect = lambda: current
        with patch('backend.media_management.files.records.row', return_value=dict(hold=False, removed=False)), \
             patch('backend.download_control.paused', return_value=False) as paused:
            self.assertTrue(replacement_allowed(store, request))
            paused.assert_called_once_with(store, 'vod')
            current['state'] = 'cancelled'
            self.assertFalse(replacement_allowed(store, request))
            self.assertEqual(paused.call_count, 1)


if __name__ == '__main__':
    unittest.main()
