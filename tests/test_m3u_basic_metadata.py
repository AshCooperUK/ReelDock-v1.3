"""Completed ordinary VOD/STRM files need no TMDB login for local facts."""
import json
import threading
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import test_m3u_downloads as vod_fixture
import test_strm as strm_fixture
from backend import m3u_catalogue, m3u_downloads

MEDIA=b'\x00\x00\x00\x20ftypisom'+b'x'*6000


class BasicVodMetadataTests(unittest.TestCase):
    setUp=vod_fixture.ServerDownloadTests.setUp
    tearDown=vod_fixture.ServerDownloadTests.tearDown
    enqueue=vod_fixture.ServerDownloadTests.enqueue
    response=vod_fixture.ServerDownloadTests.response

    def transfer(self,job):
        with patch('backend.m3u_player.upstream',return_value=self.response(MEDIA)),patch('backend.m3u_metadata.TMDB.get',side_effect=AssertionError('Basic metadata never requests a provider')):
            m3u_downloads.transfer(self.app,self.store,job,threading.Event())
        with self.store.connect() as db:return dict(db.execute('SELECT * FROM m3u_downloads WHERE id=?',(job['id'],)).fetchone())

    def test_no_key_movie_writes_local_title_year_only_after_exact_media_publication(self):
        job=self.transfer(self.enqueue());target=Path(job['destination'])
        self.assertEqual(job['state'],'complete');self.assertEqual(job['progress'],100)
        self.assertEqual(target.read_bytes(),MEDIA)
        nfo=ET.parse(target.with_suffix('.nfo')).getroot()
        self.assertEqual(nfo.tag,'movie');self.assertEqual(nfo.findtext('title'),'Movie');self.assertEqual(nfo.findtext('year'),'2024')
        for field in ('uniqueid','plot','actor','thumb'):self.assertIsNone(nfo.find(field))
        self.assertNotIn('https://',target.with_suffix('.nfo').read_text())

    def test_saved_catalogue_runtime_is_explicit_minutes_and_private_fields_are_ignored(self):
        m3u_catalogue.index(self.store);job=self.enqueue()
        with self.store.connect() as db:
            row=db.execute('SELECT t.key,t.info FROM m3u_catalog_entries e JOIN m3u_catalog_titles t ON t.key=e.key WHERE e.source=? AND e.path=?',(job['source'],job['path'])).fetchone()
            info=json.loads(row['info']);info['private']='https://private.invalid/?token=secret'
            db.execute('UPDATE m3u_catalog_titles SET info=?,data=? WHERE key=?',(json.dumps(info),json.dumps(dict(runtimeMinutes=125,duration=999,description='Not generic plot',tmdb_id=444,url='https://private.invalid/secret')),row['key']))
        job=self.transfer(job);raw=Path(job['destination']).with_suffix('.nfo').read_text();nfo=ET.fromstring(raw)
        self.assertEqual(nfo.findtext('runtime'),'125')
        for text in ('private','secret','Not generic plot','444','999'):self.assertNotIn(text,raw)

    def test_disabled_sharing_never_invokes_the_basic_writer(self):
        self.store.set('m3u_share_metadata','false')
        with patch('backend.downloaders.torrent.metadata.write_basic',side_effect=AssertionError('Metadata is disabled')) as writer:
            job=self.transfer(self.enqueue())
        writer.assert_not_called();self.assertEqual(job['state'],'complete')
        self.assertFalse(Path(job['destination']).with_suffix('.nfo').exists())

    def test_configured_tmdb_keeps_existing_rich_metadata_path(self):
        self.store.set('m3u_tmdb_token','configured-test-only')
        with patch('backend.downloaders.torrent.metadata.write_basic',side_effect=AssertionError('Configured rich path must remain unchanged')) as writer:
            job=self.transfer(self.enqueue())
        writer.assert_not_called();self.assertEqual(job['state'],'complete')
        self.assertFalse(Path(job['destination']).with_suffix('.nfo').exists())

    def test_custom_rich_nfo_is_preserved_byte_for_byte(self):
        job=self.enqueue();target=Path(job['destination']+'.mp4');target.parent.mkdir(parents=True,exist_ok=True)
        raw=b'<?xml version="1.0"?><movie><title>Owner title</title><plot>Owner plot</plot></movie>'
        target.with_suffix('.nfo').write_bytes(raw)
        result=self.transfer(job)
        self.assertEqual(result['state'],'complete');self.assertEqual(target.with_suffix('.nfo').read_bytes(),raw)

    def test_metadata_failure_keeps_completed_media_and_sanitizes_visible_note(self):
        with patch('backend.downloaders.torrent.metadata.write_basic',side_effect=ValueError('https://private.invalid/?token=secret')):
            job=self.transfer(self.enqueue())
        self.assertEqual(job['state'],'complete');self.assertEqual(job['progress'],100)
        self.assertEqual(Path(job['destination']).read_bytes(),MEDIA)
        self.assertEqual(job['message'],'Saved on the server. Metadata incomplete; use Repair metadata to try again.')
        self.assertNotIn('secret',job['message']);self.assertFalse(list(self.movies.rglob('*.part')))

    def test_sidecar_symlink_is_not_followed_and_download_remains_complete(self):
        job=self.enqueue();target=Path(job['destination']+'.mp4');target.parent.mkdir(parents=True,exist_ok=True)
        outside=self.base/'outside.nfo';outside.write_bytes(b'owner sidecar')
        target.with_suffix('.nfo').symlink_to(outside)
        result=self.transfer(job)
        self.assertEqual(result['state'],'complete');self.assertEqual(target.read_bytes(),MEDIA)
        self.assertIn('Metadata incomplete',result['message'])
        self.assertEqual(outside.read_bytes(),b'owner sidecar');self.assertTrue(target.with_suffix('.nfo').is_symlink())

    def test_only_existing_enabled_refresh_preference_requests_emby_after_generic(self):
        for enabled in (False,True):
            self.store.set('m3u_export_refresh','true' if enabled else 'false')
            with patch('backend.emby.queue_refresh') as refresh:
                job=self.transfer(self.enqueue())
            if enabled:refresh.assert_called_once_with(self.store,'m3u-downloads')
            else:refresh.assert_not_called()
            self.assertEqual(self.store.get('m3u_export_refresh'),'true' if enabled else 'false')
            target=Path(job['destination']);target.unlink();target.with_suffix('.nfo').unlink()

    def test_configured_library_refresh_error_does_not_fail_published_media(self):
        self.store.set('m3u_export_refresh','true')
        with patch('backend.emby.queue_refresh',side_effect=RuntimeError('private refresh credential')) as refresh:
            job=self.transfer(self.enqueue())
        refresh.assert_called_once_with(self.store,'m3u-downloads')
        self.assertEqual(job['state'],'complete');self.assertEqual(job['progress'],100)
        self.assertEqual(Path(job['destination']).read_bytes(),MEDIA)
        self.assertTrue(Path(job['destination']).with_suffix('.nfo').is_file())
        self.assertEqual(job['message'],'Saved on the server. Library refresh pending.')
        self.assertNotIn('private',job['message']);self.assertFalse(list(self.movies.rglob('*.part')))


class BasicStrmMetadataTests(unittest.TestCase):
    setUp=strm_fixture.StrmTests.setUp
    tearDown=strm_fixture.StrmTests.tearDown
    post=strm_fixture.StrmTests.post
    preview=strm_fixture.StrmTests.preview
    start=strm_fixture.StrmTests.start
    rows=strm_fixture.StrmTests.rows
    job=strm_fixture.StrmTests.job
    response=strm_fixture.StrmTests.response

    def finish(self):
        with patch('backend.m3u_player.upstream',return_value=self.response(MEDIA)),patch('backend.m3u_metadata.TMDB.get',side_effect=AssertionError('No metadata provider/login is needed')):
            m3u_downloads.transfer(self.app,self.store,self.job(),threading.Event())
        self.assertEqual(self.job()['state'],'complete')
        return Path(self.job()['destination'])

    def test_private_manual_movie_gets_safe_local_nfo_without_playlist_or_url(self):
        self.start(title='Confirmed movie (2020)');target=self.finish();raw=target.with_suffix('.nfo').read_text();nfo=ET.fromstring(raw)
        self.assertEqual(nfo.tag,'movie');self.assertEqual(nfo.findtext('title'),'Confirmed movie');self.assertEqual(nfo.findtext('year'),'2020')
        for text in ('private-token','media.example.com','strm:','http','uniqueid','plot','actor'):self.assertNotIn(text,raw)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM m3u_sources').fetchone()[0],0)

    def test_confirmed_series_episode_uses_actual_season_episode_and_local_title(self):
        self.start(title='Programme S03E02 - Safe & sound',kind='series');target=self.finish()
        nfo=ET.parse(target.with_suffix('.nfo')).getroot()
        self.assertEqual(nfo.tag,'episodedetails');self.assertEqual(nfo.findtext('showtitle'),'Programme')
        self.assertEqual(nfo.findtext('season'),'3');self.assertEqual(nfo.findtext('episode'),'2');self.assertEqual(nfo.findtext('title'),'Safe & sound')
        show=ET.parse(target.parent.parent/'tvshow.nfo').getroot();self.assertEqual(show.findtext('title'),'Programme')
        self.assertEqual(target.read_bytes(),MEDIA)

    def test_cancellation_never_publishes_media_or_generic_sidecars(self):
        self.start()
        with self.store.connect() as db:db.execute("UPDATE m3u_downloads SET state='cancelling'")
        with patch('backend.m3u_player.upstream',return_value=self.response(MEDIA)),patch('backend.downloaders.torrent.metadata.write_basic') as writer:
            m3u_downloads.transfer(self.app,self.store,self.job(),threading.Event())
        writer.assert_not_called();self.assertEqual(self.job()['state'],'cancelled')
        self.assertFalse(list(self.movies.rglob('*.mp4')));self.assertFalse(list(self.movies.rglob('*.nfo')))


if __name__=='__main__':unittest.main()
