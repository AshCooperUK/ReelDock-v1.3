import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch
from backend.storage import Store
from backend import discover_specials as specials, discover_store as requests, discover_acquire as acquire

class SpecialTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(self.temp.name);requests.initialise(self.store)
        self.show=dict(title='The Wargame',year='2026')
        self.episode=dict(id=55,season=1,number=None,name='The Wargame: Aftermath')
        self.catalogue=patch.object(specials,'catalogue',return_value=(self.show,self.episode));self.catalogue.start();self.addCleanup(self.catalogue.stop)
    def search(self):
        root=ET.fromstring('''<rss><channel>
          <item><title>The.Wargame.S01E05.Wargame.Aftermath.The.1080p</title><enclosure url="https://example.com/get?apikey=secret"/><attr name="tvdbid" value="482384"/></item>
          <item><title>The.Wargame.S01E05.Wargame.Aftermath.The.720p</title><enclosure url="https://example.com/other"/><attr name="tvdbid" value="999"/></item>
          <item><title>The.Wargame.S01.1080p</title><enclosure url="https://example.com/pack"/></item>
        </channel></rss>''')
        with patch('backend.discover_search.query_all',return_value=([('Geek',root)],[])),patch.object(specials.sources,'indexers_for',return_value=[{}]):
            return specials.search(None,self.store,'482384',55)
    def test_search_keeps_public_choices_private_and_rejects_conflicts(self):
        result=self.search()
        self.assertEqual(len(result['items']),1)
        self.assertNotIn('secret',json.dumps(result))
        self.assertEqual(result['items'][0]['releaseNumber'],5)
        self.assertIn('secret',requests.candidate(self.store,result['items'][0]['id'])['grab'])
    def test_choice_is_bound_to_show_and_special(self):
        release=self.search()['items'][0]
        for tvdb,episode in [('999',55),('482384',66)]:
            with self.assertRaises(ValueError): specials.selected(self.store,tvdb,episode,release['id'])
        with self.assertRaises(ValueError): specials.selected(self.store,'482384',55,{'url':'bad'})
    def test_manual_queue_preserves_metadata_and_downloads_only_selected_release(self):
        release=self.search()['items'][0]
        with patch.object(acquire,'resolve_release') as resolve:
            result=specials.queue(None,self.store,'482384',55,release['id'],'special-request-token')
            self.assertEqual(resolve.call_count,1)
            row=resolve.call_args.args[2];body=json.loads(row['body'])
            self.assertIsNone(body['catalogueEpisode']['number'])
            self.assertEqual(body['releaseEpisode']['number'],5)
            self.assertEqual(row['episode_key'],'show:482384:special:55')
            self.assertIn('S01E05',acquire.library_name(row,release['title']))
            self.assertEqual([r['id'] for r in acquire.search_for(None,self.store,row)],[release['id']])
            specials.queue(None,self.store,'482384',55,release['id'],'another-request-token')
            self.assertEqual(resolve.call_count,1)
            self.assertFalse(acquire._retry_candidate(self.store,row,'Incomplete articles'))
    def test_expired_choice_requires_search_again(self):
        release=self.search()['items'][0]
        with self.store.connect() as db: db.execute('UPDATE discover_candidates SET created=0')
        with self.assertRaisesRegex(ValueError,'expired'): specials.selected(self.store,'482384',55,release['id'])
