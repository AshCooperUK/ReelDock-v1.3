import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET
from PIL import Image
import test_dashboard as dashboard
from backend import discover_store as ds, discover_publication as publication, discovery_engine as engine
from backend.discover_youtube import register_video, claim_path, is_discover
from backend.media_library import reserve
from backend.view_media import local


class DiscoverPublicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=dashboard.DashboardTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.store=self.fixture.store;self.media=Path(self.fixture.tmp.name)/'media';self.media.mkdir(exist_ok=True)
        self.item=dict(id='tvmaze:show:71810',kind='show',title='Big Brother',year='2023',externalId='440642',image='https://static.tvmaze.com/uploads/images/a.jpg')
        self.episode=dict(season=4,number=15,name='Episode 15')
        self.edition=dict(title='Big Brother',year='2023',country='GB',countryName='United Kingdom')
        self.video=register_video(self.store,dict(videoId='abcdefghijk',title='Big Brother UK 2023 S04E15',published='2026-10-14'))
        self.body=dict(discoveryEngine=True,item=self.item,externalId='440642',year='2023',episode=self.episode,edition=self.edition,videoId=self.video['id'],date='2026-10-13')
        self.row,_=ds.insert_request(self.store,dict(idempotency='test-request-publication',kind='episode',title='Big Brother · S04E15',quality='1080',fallback=False,manual=True,state='searching',episode_key='show:440642:4:15',body=self.body))
        self.art=Path(self.fixture.tmp.name)/'art.jpg';Image.new('RGB',(20,20),'purple').save(self.art)

    def test_youtube_fallback_publishes_in_series_folder_with_catalogue_metadata(self):
        record=reserve(self.store,self.media,self.video['id'])
        record['path']=claim_path(self.store,self.media,self.video['id'],self.video['title'])
        self.assertEqual(Path(record['path']).parent,Path('TV Shows/Big Brother (2023)'))
        self.assertIn('s04e15',record['path']);self.assertNotIn('2026.e',record['path'])
        produced=self.media/'staged.mkv';produced.write_bytes(b'completed-test-video')
        with patch('backend.m3u_player.cached_image_path',return_value=self.art):publication.publish(self.store,self.media,record,self.row,produced)
        target=self.media/record['path'];self.assertEqual(target.read_bytes(),b'completed-test-video');self.assertFalse(produced.exists())
        nfo=ET.parse(target.with_suffix('.nfo')).getroot()
        self.assertEqual(nfo.findtext('showtitle'),'Big Brother');self.assertEqual(nfo.findtext('season'),'4');self.assertEqual(nfo.findtext('episode'),'15')
        self.assertEqual(nfo.find("uniqueid[@type='tvdb']").text,'440642')
        for name in ('poster.jpg','fanart.jpg','tvshow.nfo'):self.assertTrue((target.parent/name).is_file())
        self.assertEqual(ds.owned(self.store,self.row['episode_key'])['path'],record['path'])
        state=dict(items=[],files={},scanning=True,checked=0,warning='');local._scan(self.media,state)
        self.assertTrue(any('Big Brother S04E15' in i['title'] and i['kind']=='shows' for i in state['items']))

    def test_existing_destination_and_symlink_boundary_are_preserved(self):
        reserve(self.store,self.media,self.video['id'])
        relative=publication.planned_path(self.store,self.media,self.row)
        existing=self.media/relative;existing.parent.mkdir(parents=True);existing.write_bytes(b'keep-existing')
        with self.assertRaisesRegex(ValueError,'already has'):claim_path(self.store,self.media,self.video['id'],self.video['title'])
        self.assertEqual(existing.read_bytes(),b'keep-existing')
        outside=Path(self.fixture.tmp.name)/'outside';outside.mkdir()
        other=dict(self.row,kind='movie',title='Movie',body=json.dumps(dict(item=dict(title='Movie',year='2020'),year='2020')))
        (self.media/'Movies').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):publication.planned_path(self.store,self.media,other)

    def test_missing_art_does_not_publish_a_bare_video(self):
        record=reserve(self.store,self.media,self.video['id']);record['path']=claim_path(self.store,self.media,self.video['id'],self.video['title'])
        produced=self.media/'staged.mkv';produced.write_bytes(b'keep-staging')
        with patch('backend.m3u_player.cached_image_path',side_effect=ValueError('Artwork unavailable')):
            with self.assertRaises(ValueError):publication.publish(self.store,self.media,record,self.row,produced)
        self.assertTrue(produced.exists());self.assertFalse((self.media/record['path']).exists())

    def test_fallback_reserves_identity_and_selected_quality_before_enqueue(self):
        queued=[]
        def enqueue(store,video,quality,automatic=False,*,transaction=None):
            self.assertIsNotNone(transaction)
            self.assertTrue(transaction.in_transaction)
            self.assertEqual(transaction.execute('SELECT allow_lower FROM discover_quality_policy WHERE id=?',(video['id'],)).fetchone()[0],0)
            queued.append(quality);self.assertIsNotNone(publication.request_for_video(store,video['id']))
        public=dict(id=self.video['id'],snippet=dict(title=self.video['title'],channelId='UC'+'z'*22,channelTitle='Official',publishedAt='2026-10-14'),contentDetails=dict(duration='PT45M'))
        with patch('backend.youtube_discover._search',return_value={'items':[dict(videoId=self.video['id'],title=self.video['title'])]}),patch('backend.catalogue.Importer') as importer,patch('backend.downloads.enqueue',side_effect=enqueue):
            importer.return_value.api.return_value={'items':[public]}
            self.assertTrue(engine.fallback_youtube(self.fixture.app,self.store,self.row,self.body))
        self.assertEqual(queued,['1080']);self.assertEqual(publication.request_for_video(self.store,self.video['id'])['engine'],'youtube');self.assertTrue(is_discover(self.store,self.video['id']))

    def test_normal_manual_youtube_download_keeps_its_channel_destination(self):
        other=register_video(self.store,dict(videoId='klmnopqrstu',title='Ordinary upload',published='2026-10-14'))
        relative=claim_path(self.store,self.media,other['id'],other['title'])
        self.assertTrue(relative.startswith('Discover/'));self.assertNotIn('TV Shows/',relative)

    def test_cancellation_during_metadata_keeps_video_out_of_library(self):
        record=reserve(self.store,self.media,self.video['id']);record['path']=claim_path(self.store,self.media,self.video['id'],self.video['title'])
        produced=self.media/'staged.mkv';produced.write_bytes(b'keep-staging')
        def cancel_then_art(*args):ds.cancel_request(self.store,self.row['id']);return self.art
        with patch('backend.m3u_player.cached_image_path',side_effect=cancel_then_art):
            with self.assertRaises(InterruptedError):publication.publish(self.store,self.media,record,self.row,produced)
        self.assertTrue(produced.exists());self.assertFalse((self.media/record['path']).exists())
