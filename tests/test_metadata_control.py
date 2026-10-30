"""Metadata switches cancel in-flight work, leave progress pending and resume safely."""
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests import test_m3u_player as fixture
from backend import activity, m3u_catalogue, m3u_metadata, settings
from backend.metadata_control import checkpoint


class VodCancellationTests(unittest.TestCase):
    setUp = fixture.PlayerTests.setUp
    tearDown = fixture.PlayerTests.tearDown

    def test_disabled_metadata_neither_fetches_nor_advertises_pending_work(self):
        self.store.set('m3u_tmdb_token', 'fixture-token')
        activity.record(self.store, 'm3u-catalogue', 'M3U metadata & artwork', 'running', 'Matching')
        settings.save(self.store, 'm3u', {'share_metadata':False})
        with patch('backend.m3u_catalogue.resolve') as fetch:
            self.assertFalse(m3u_catalogue.enrich(self.store))
            fetch.assert_not_called()
        # A late worker update must not turn cancellation into running/queued activity.
        activity.record(self.store, 'm3u-catalogue', 'M3U metadata & artwork', 'complete', 'Checked title.')
        self.assertFalse(any(t['id']=='m3u-catalogue' for t in activity.snapshot(self.store)['tasks']))
        with self.assertRaisesRegex(ValueError, 'Enable VOD metadata'):
            m3u_metadata.request(self.store, 'unused')

    def test_disable_during_fetch_does_not_commit_and_enable_resumes(self):
        self.store.set('m3u_tmdb_token', 'fixture-token')
        with self.store.connect() as db:
            before=[tuple(r) for r in db.execute('SELECT key,data,checked FROM m3u_catalog_titles ORDER BY key')]
        def interrupted(*args):
            settings.save(self.store, 'm3u', {'share_metadata':False})
            # A quick on/off/on must not revive the old worker generation.
            settings.save(self.store, 'm3u', {'share_metadata':True})
            return {'tmdb_id':123,'title':'fixture'}
        with patch('backend.m3u_catalogue.resolve',side_effect=interrupted):
            self.assertFalse(m3u_catalogue.enrich(self.store))
        with self.store.connect() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT key,data,checked FROM m3u_catalog_titles ORDER BY key')],before)
        with patch('backend.m3u_catalogue.resolve',return_value={'tmdb_id':123,'title':'fixture'}):
            self.assertTrue(m3u_catalogue.enrich(self.store))

    def test_sidecar_write_stops_at_cancellation_boundary(self):
        from backend import m3u_sidecars
        from backend.metadata_control import vod_task
        target=self.store.directory/'should-not-exist.nfo'
        def export():
            settings.save(self.store,'m3u',{'share_metadata':False})
            m3u_sidecars.owned_write(self.store,self.store.directory,target,b'<movie/>')
        self.assertFalse(vod_task(self.store,export))
        self.assertFalse(target.exists())


class NzbCancellationTests(unittest.TestCase):
    def test_active_metadata_is_retryable_after_cancellation(self):
        from pathlib import Path
        import tempfile
        from backend.downloaders.torrent import metadata
        with tempfile.TemporaryDirectory() as folder:
            engine=SimpleNamespace(source_type='nzb',config={'share_metadata':True,'metadata_generation':'one'},
                stop=threading.Event(),state_dir=Path(folder),_metadata_caught=True)
            job=SimpleNamespace(engine=engine,id='fixture',removed=False,_lock=threading.Lock(),
                metadata_done=set(),metadata_started={0},metadata_note='',metadata_choice={})
            def fetch(*args):
                engine.config={'share_metadata':False,'metadata_generation':'two'}
                checkpoint()
            with patch('backend.downloaders.torrent.metadata._save_active',side_effect=fetch):
                metadata._save(None,Path(folder),job,0,'video.mkv','movie',{})
            self.assertEqual(job.metadata_done,set())
            self.assertEqual(job.metadata_started,set())
            self.assertFalse(engine._metadata_caught)
            self.assertIn('cancelled',job.metadata_note)

    def test_reenabled_engine_reschedules_published_unfinished_metadata(self):
        from backend.downloaders.nzb.engine import NzbEngine
        calls=[]
        job=SimpleNamespace(id='test',handoff_token='',removed=False,meta=SimpleNamespace(files=[SimpleNamespace(index=0,path='Movie.mkv')]),
                            storage=SimpleNamespace(published={0:'Movies/Movie.mkv'}))
        hold_check=Mock(return_value=False)
        engine=SimpleNamespace(config={'share_metadata':False},_metadata_caught=False,_jobs={'test':job},
                               metadata=lambda job,rows:calls.append(rows),_activity_held=hold_check)
        # Model a legacy published job through the real readiness gate. Durable
        # Discover handoff receipts are covered by test_nzb_handoff.
        engine._handoff_ready=NzbEngine._handoff_ready.__get__(engine)
        NzbEngine._resume_metadata(engine)
        self.assertEqual(calls,[])
        engine.config['share_metadata']=True
        NzbEngine._resume_metadata(engine)
        self.assertEqual(calls,[[(0,'Movies/Movie.mkv','video')]])
        NzbEngine._resume_metadata(engine)
        self.assertEqual(len(calls),1)
        hold_check.assert_called_once_with(job)

    def test_cleared_native_metadata_is_not_restarted_when_metadata_is_enabled(self):
        from backend.downloaders.nzb.engine import NzbEngine
        job=SimpleNamespace(id='test',handoff_token='',removed=False,meta=SimpleNamespace(files=[SimpleNamespace(index=0,path='Movie.mkv')]),
                            storage=SimpleNamespace(published={0:'Movies/Movie.mkv'}))
        hold_check=Mock(return_value=True);callback=Mock()
        engine=SimpleNamespace(config={'share_metadata':True},_metadata_caught=False,_jobs={'test':job},
                               metadata=callback,_activity_held=hold_check)
        engine._handoff_ready=NzbEngine._handoff_ready.__get__(engine)
        NzbEngine._resume_metadata(engine)
        hold_check.assert_called_once_with(job);callback.assert_not_called()
        self.assertEqual(job.storage.published,{0:'Movies/Movie.mkv'})

from tests import test_media_library as library_fixture


class YouTubeCancellationTests(unittest.TestCase):
    setUp = library_fixture.MediaLibraryTests.setUp
    tearDown = library_fixture.MediaLibraryTests.tearDown
    video = library_fixture.MediaLibraryTests.video

    def test_disabled_sidecars_keep_video_and_enabling_resumes_saved_library(self):
        from backend import media_library as library
        video=self.video()
        record=library.reserve(self.store,self.media,video['id'])
        target=self.media/record['path']
        target.parent.mkdir(parents=True)
        target.write_bytes(b'video')
        settings.save(self.store,'youtube',{'share_metadata':False})
        self.assertEqual(library.write_metadata(self.store,self.media,record),'')
        self.assertFalse(target.with_suffix('.nfo').exists())
        self.assertEqual(target.read_bytes(),b'video')
        settings.save(self.store,'youtube',{'share_metadata':True})
        library.metadata_batch(self.store,self.media)
        self.assertTrue(target.with_suffix('.nfo').exists())
        self.assertEqual(self.store.get('youtube_metadata_pending'),'0')
        settings.save(self.store,'youtube',{'share_metadata':False})
        self.assertTrue(target.with_suffix('.nfo').exists())

    def test_cancelled_youtube_artwork_is_not_written(self):
        from backend import media_library as library, auto_downloads
        from backend.metadata_control import youtube_task
        target=self.media/'poster.jpg'
        def write():
            settings.save(self.store,'youtube',{'share_metadata':False})
            library.atomic_bytes(target,b'fixture')
        youtube_task(self.store,write)
        self.assertFalse(target.exists())
        with patch('backend.auto_downloads._extract_youtube') as extractor:
            self.assertEqual(auto_downloads._channel_images(self.store,'UC'+'a'*22),{})
            extractor.assert_not_called()

    def test_disabled_artwork_queue_does_not_fetch(self):
        from backend import artwork
        settings.save(self.store,'youtube',{'share_metadata':False})
        with patch('backend.artwork.fetch_image') as fetch:
            self.assertEqual(artwork.cache_batch(self.store,fetch=fetch),0)
            fetch.assert_not_called()


class TorrentCancellationTests(unittest.TestCase):
    def test_active_metadata_is_retryable_after_cancellation(self):
        from pathlib import Path
        import tempfile
        from backend.downloaders.torrent import metadata
        with tempfile.TemporaryDirectory() as folder:
            engine=SimpleNamespace(source_type='torrent',config={'share_metadata':True,'metadata_generation':'one'},
                stop=threading.Event(),state_dir=Path(folder),_metadata_caught=True, _save_job=lambda job: None)
            job=SimpleNamespace(engine=engine,id='fixture',removed=False,_lock=threading.Lock(),
                metadata_done=set(),metadata_started={0},metadata_note='',metadata_choice={})
            def fetch(*args):
                engine.config={'share_metadata':False,'metadata_generation':'two'}
                checkpoint()
            with patch('backend.downloaders.torrent.metadata._save_active',side_effect=fetch):
                metadata._save(None,Path(folder),job,0,'video.mkv','movie',{})
            self.assertEqual(job.metadata_done,set())
            self.assertEqual(job.metadata_started,set())
            self.assertFalse(engine._metadata_caught)
            self.assertIn('cancelled',job.metadata_note)

    def test_disabled_catch_up_and_reenable(self):
        from backend.downloaders.torrent import metadata
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            job=SimpleNamespace(removed=False,storage=SimpleNamespace(published={0:'Movies/Fixture.mkv'}),meta=SimpleNamespace(files=[SimpleNamespace(index=0,path='Fixture.mkv')]))
            engine=SimpleNamespace(config={'share_metadata':False},_metadata_caught=False,_jobs={'fixture':job},data_dir=Path(folder))
            with patch.object(metadata,'schedule') as schedule:
                metadata.catch_up(None,Path(folder),engine)
                schedule.assert_not_called()
                engine.config['share_metadata']=True
                metadata.catch_up(None,Path(folder),engine)
                schedule.assert_called_once_with(None,Path(folder),job,[(0,'Movies/Fixture.mkv','video')])
                metadata.catch_up(None,Path(folder),engine)
                self.assertEqual(schedule.call_count,1)

    def test_disabled_manual_rewrite_does_not_write_sidecars(self):
        from backend.downloaders.torrent import metadata
        from pathlib import Path
        job=SimpleNamespace(engine=SimpleNamespace(config={'share_metadata':False}),removed=False)
        with patch.object(metadata,'_rewrite_active') as rewrite:
            metadata._rewrite(None,Path('/unused'),job,0,'Fixture.mkv')
            rewrite.assert_not_called()
