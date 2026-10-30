import hashlib
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.app import create_app
from backend.downloaders.torrent import metadata


class BasicMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.app=create_app({'TESTING':True,'DATA_DIR':self.tmp.name,'LOCAL_SETUP_URL':'http://127.0.0.1:8790'})
        self.store=self.app.extensions['store'];self.base=Path(self.tmp.name)/'media';self.base.mkdir()

    def video(self,path):
        target=self.base/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'kept video');return target

    def test_no_login_movie_metadata_local_art_and_release_note_are_idempotent(self):
        from PIL import Image
        target=self.video('Movies/Example (2026)/Example (2026).mkv');note=b'Plain release note, not XML.'
        target.with_suffix('.nfo').write_bytes(note);Image.new('RGB',(40,30),'blue').save(target.with_suffix('.jpg'))
        info={'name':'Example','year':2026,'runtime':41,'url':'https://private.invalid/SECRET','description':'Do not invent this','identity':'999'}
        with patch.object(metadata,'TMDB',side_effect=AssertionError('No network or account')):
            self.assertTrue(metadata.write_basic(self.store,self.base,str(target.relative_to(self.base)),'movie',info))
            self.assertFalse(metadata.write_basic(self.store,self.base,str(target.relative_to(self.base)),'movie',info))
        root=ET.fromstring(target.with_suffix('.nfo').read_bytes());self.assertEqual(root.tag,'movie');self.assertEqual(root.findtext('title'),'Example');self.assertEqual(root.findtext('year'),'2026');self.assertEqual(root.findtext('runtime'),'41')
        for field in ('plot','actor','uniqueid'):self.assertIsNone(root.find(field))
        self.assertNotIn(b'SECRET',target.with_suffix('.nfo').read_bytes());self.assertEqual(target.read_bytes(),b'kept video')
        copies=list(target.parent.glob('*.release-note-*.txt'));self.assertEqual(len(copies),1);self.assertEqual(copies[0].read_bytes(),note)
        for path in (target.parent/'poster.jpg',target.with_name(target.stem+'-thumb.jpg')):
            with Image.open(path) as image:self.assertEqual(image.size,(40,30))

    def test_series_fields_are_explicit_and_rich_custom_xml_is_never_downgraded(self):
        target=self.video('TV Shows/Example/Season 28/Example s28e02.mkv');relative=str(target.relative_to(self.base))
        self.assertTrue(metadata.write_basic(self.store,self.base,relative,'series',{'name':'Example','season':28,'episode':2,'episode_title':'Known local title'}))
        show=target.parent.parent/'tvshow.nfo';episode=target.with_suffix('.nfo');root=ET.fromstring(episode.read_bytes())
        self.assertEqual((root.findtext('title'),root.findtext('showtitle'),root.findtext('season'),root.findtext('episode')),('Known local title','Example','28','2'))
        rich=b'<tvshow><title>Custom series</title><plot>Keep this</plot><uniqueid type="tmdb">7</uniqueid></tvshow>';show.write_bytes(rich)
        self.assertFalse(metadata.write_basic(self.store,self.base,relative,'series',{'name':'Wrong replacement','season':28,'episode':2}))
        self.assertEqual(show.read_bytes(),rich);self.assertEqual(ET.fromstring(episode.read_bytes()).findtext('title'),'Known local title')

    def test_generic_owned_metadata_can_later_upgrade_to_external_metadata(self):
        from test_torrent_metadata import Client
        target=self.video('Movies/Coyote vs Acme (2026)/Coyote vs Acme (2026).mkv');relative=str(target.relative_to(self.base))
        metadata.write_basic(self.store,self.base,relative,'movie',{'name':'Coyote vs Acme','year':2026})
        self.assertIsNone(ET.fromstring(target.with_suffix('.nfo').read_bytes()).find('uniqueid'))
        self.assertTrue(metadata.fetch(self.store,self.base,relative,'movie',{'name':'Coyote vs Acme','year':2026},Client()))
        self.assertEqual(ET.fromstring(target.with_suffix('.nfo').read_bytes()).findtext('uniqueid'),'7')

    def test_symlink_nfo_art_and_backup_are_never_read_or_overwritten(self):
        target=self.video('Movies/Example/Example.mkv');relative=str(target.relative_to(self.base));outside=Path(self.tmp.name)/'outside';outside.write_bytes(b'outside private data')
        target.with_suffix('.nfo').symlink_to(outside)
        with self.assertRaises(ValueError):metadata.write_basic(self.store,self.base,relative,'movie',{'name':'Example'})
        self.assertEqual(outside.read_bytes(),b'outside private data');target.with_suffix('.nfo').unlink();target.with_suffix('.jpg').symlink_to(outside)
        self.assertTrue(metadata.write_basic(self.store,self.base,relative,'movie',{'name':'Example'}));self.assertFalse((target.parent/'poster.jpg').exists())
        target.with_suffix('.nfo').write_bytes(b'release note');backup=target.with_name(target.stem+'.release-note-'+hashlib.sha256(b'release note').hexdigest()[:12]+'.txt');backup.symlink_to(outside)
        with self.assertRaises(ValueError):metadata.write_basic(self.store,self.base,relative,'movie',{'name':'Example'})
        self.assertEqual(target.with_suffix('.nfo').read_bytes(),b'release note');self.assertEqual(outside.read_bytes(),b'outside private data')

    def test_symbolic_managed_root_is_rejected_before_resolving_or_writing(self):
        outside=Path(self.tmp.name)/'outside-media';outside.mkdir();(outside/'Example.mkv').write_bytes(b'kept video')
        link=Path(self.tmp.name)/'managed-link';link.symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):metadata.write_basic(self.store,link,'Example.mkv','movie',{'name':'Example'})
        self.assertFalse((outside/'Example.nfo').exists());self.assertEqual((outside/'Example.mkv').read_bytes(),b'kept video')

    def test_other_process_shared_show_creation_does_not_abort_episode_metadata(self):
        from backend import m3u_sidecars
        target=self.video('TV Shows/Example/Season 1/Example s01e02.mkv');relative=str(target.relative_to(self.base))
        show=target.parent.parent/'tvshow.nfo';custom=b'<tvshow><title>Concurrent author</title></tvshow>';original=m3u_sidecars.owned_write
        def publish(store,base,path,raw):
            if path==show and not path.exists():
                path.write_bytes(custom)
                raise FileExistsError('Another process won the shared show link')
            return original(store,base,path,raw)
        with patch.object(m3u_sidecars,'owned_write',side_effect=publish):
            self.assertTrue(metadata.write_basic(self.store,self.base,relative,'series',{'name':'Example','season':1,'episode':2}))
        self.assertEqual(show.read_bytes(),custom)
        self.assertEqual(ET.fromstring(target.with_suffix('.nfo').read_bytes()).findtext('episode'),'2')
        self.assertEqual(target.read_bytes(),b'kept video')

    def test_conflicting_backup_preserves_original_note_and_cancellation_stops_writes(self):
        from backend.metadata_control import work,Cancelled
        target=self.video('Movies/Example/Example.mkv');relative=str(target.relative_to(self.base));note=b'release note';target.with_suffix('.nfo').write_bytes(note)
        backup=target.with_name(target.stem+'.release-note-'+hashlib.sha256(note).hexdigest()[:12]+'.txt');backup.write_bytes(b'custom backup')
        with self.assertRaises(ValueError):metadata.write_basic(self.store,self.base,relative,'movie',{'name':'Example'})
        self.assertEqual(backup.read_bytes(),b'custom backup');self.assertEqual(target.with_suffix('.nfo').read_bytes(),note)
        with self.assertRaises(Cancelled):
            with work(lambda:False):metadata.write_basic(self.store,self.base,relative,'movie',{'name':'Example'})

    def test_new_credential_readiness_reopens_catchup_once_for_both_native_engines(self):
        job=SimpleNamespace(removed=False,storage=SimpleNamespace(published={0:'Movies/Example/Example.mkv'}))
        engine=SimpleNamespace(config={'share_metadata':True},_metadata_caught=True,_jobs={'fixture':job},data_dir=Path(self.tmp.name))
        with patch.object(metadata,'schedule') as schedule,patch.object(metadata,'_kind',return_value='video'):
            metadata.catch_up(self.store,self.base,engine);self.assertEqual(schedule.call_count,0)
            self.store.set('m3u_tmdb_token','test-only-token');metadata.catch_up(self.store,self.base,engine);metadata.catch_up(self.store,self.base,engine);self.assertEqual(schedule.call_count,1)
        from backend.downloaders.nzb.engine import NzbEngine
        ready=[False];callback=Mock();job.meta=SimpleNamespace(files=[SimpleNamespace(index=0,path='Example.mkv')])
        hold_check=Mock(return_value=False)
        job.id='fixture'
        engine=SimpleNamespace(_metadata_caught=True,config={'share_metadata':True},metadata=callback,metadata_ready=lambda:ready[0],_jobs={'fixture':job},_activity_held=hold_check,_handoff_ready=lambda current:True)
        NzbEngine._resume_metadata(engine);self.assertEqual(callback.call_count,0);ready[0]=True;NzbEngine._resume_metadata(engine);NzbEngine._resume_metadata(engine);self.assertEqual(callback.call_count,1)
        hold_check.assert_called_once_with(job)

    def test_restart_adopts_exact_legacy_path_without_republishing_or_moving_video(self):
        from backend.downloaders.torrent.rename import library_path,previous_library_path,named_info
        from backend.downloaders.torrent.storage import Storage
        from backend.downloaders.torrent.metainfo import parse_torrent
        from tests.torrentutil import build_torrent
        source='law.and.order.svu.s28e02.1080p.web.h264-cakes.mkv';title='law and order svu - S28-E02';payload=b'kept video'
        meta=parse_torrent(build_torrent([(source,payload)],piece_length=16));current=library_path(meta.name,source,'video',title=title);previous=previous_library_path(meta.name,source,'video',title=title)
        self.assertNotEqual(current,previous);old=self.video(previous)
        storage=Storage(self.base,meta,{0});storage.save_as=title;storage.note_existing()
        self.assertEqual(storage.published,{0:previous});self.assertFalse((self.base/current).exists());self.assertEqual(old.read_bytes(),payload)
        engine=SimpleNamespace(media_base=self.base,layout=None)
        self.assertEqual(metadata._saved_rows(engine,meta,{0},{},title),[(0,previous,'video')])
        self.assertTrue(metadata.write_basic(self.store,self.base,previous,'series',named_info(source,title)))
        self.assertEqual(ET.fromstring((old.parent.parent/'tvshow.nfo').read_bytes()).findtext('title'),'law and order svu')
        self.assertEqual(old.read_bytes(),payload);self.assertFalse((self.base/current).exists())
        old.write_bytes(b'wrong');again=Storage(self.base,meta,{0});again.save_as=title;again.note_existing();self.assertEqual(again.published,{})
        old.write_bytes(payload);new=self.base/current;new.parent.mkdir(parents=True);new.write_bytes(b'wrong');again.note_existing();self.assertEqual(again.published,{},'a present wrong-sized new destination never falls back to another file')
        new.unlink();old.unlink();outside=Path(self.tmp.name)/'outside-video';outside.write_bytes(payload);old.symlink_to(outside);again.note_existing();self.assertEqual(again.published,{});self.assertEqual(outside.read_bytes(),payload)
