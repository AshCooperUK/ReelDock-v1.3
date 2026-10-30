import json
import tempfile
import unittest
from unittest.mock import patch
from backend.app import create_app
from backend import discover_metadata as metadata, discover_sources as sources

SHOW = dict(id=92726, name='The Wargame', premiered='2026-09-21', externals={'thetvdb':None},
            image={'medium':'https://static.tvmaze.com/uploads/images/medium_portrait/642/1605168.jpg'},
            summary='<p>Verified synopsis.</p>', genres=['Drama'])


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.app=create_app({'TESTING':True,'DATA_DIR':self.tmp.name,'MEDIA_DIR':self.tmp.name+'/media'})
        self.store=self.app.extensions['store'];sources.initialise(self.store);sources.EPISODE_CACHE.clear()

    def test_exact_title_year_can_supply_a_missing_external_link(self):
        self.assertEqual(metadata.title_match([{'show':SHOW}], '482384', 'The Wargame', '2026')['id'],92726)
        for title,year in [('Wargame','2026'),('The Wargame','2025'),('The Wargame','')]:
            self.assertIsNone(metadata.title_match([{'show':SHOW}], '482384',title,year))

    def test_conflicting_and_ambiguous_matches_rejected(self):
        conflict={**SHOW,'externals':{'thetvdb':123}}
        self.assertIsNone(metadata.title_match([{'show':conflict}], '482384','The Wargame','2026'))
        self.assertIsNone(metadata.title_match([{'show':SHOW},{'show':{**SHOW,'id':999}}], '482384','The Wargame','2026'))

    def test_timeout_does_not_trigger_name_fallback(self):
        with patch.object(sources,'fetch',side_effect=sources.DiscoverError('timeout')) as fetch:
            with self.assertRaises(sources.DiscoverError):metadata.resolve('482384','The Wargame','2026')
        self.assertEqual(fetch.call_count,1)

    def test_enrichment_and_episode_catalogue_survive_new_process(self):
        def fetch(url,**kwargs):
            if '/lookup/' in url: raise sources.DiscoverError('The provider returned HTTP 404.')
            if '/search/' in url:return json.dumps([{'show':SHOW}]).encode()
            if '/episodes?' in url:return json.dumps([dict(id=1,name='Episode 1',season=1,number=1,airdate='2026-09-21'),dict(id=2,name='Aftermath',season=1,number=None,type='significant_special')]).encode()
            if '/seasons' in url:return json.dumps([dict(id=10,number=1)]).encode()
            return json.dumps(SHOW).encode()
        groups={'482384':dict(title='The Wargame', image='')}
        known={'the wargame':[dict(identity='482384',title='The Wargame',year='2026')]}
        with patch.object(sources,'fetch',side_effect=fetch):
            metadata.enrich(self.store,groups,known)
            self.assertEqual(groups['482384']['description'],'Verified synopsis.')
            self.assertTrue(groups['482384']['image'].startswith('https://static.tvmaze.com/'))
            sources.EPISODE_CACHE.clear()
            catalogue=sources.episodes('482384',self.store)
        self.assertEqual(catalogue['showId'],92726)
        self.assertEqual(catalogue['episodes'][0]['number'],1)
        self.assertIsNone(catalogue['episodes'][1]['number'])

    def test_failed_enrichment_preserves_results_and_retry_hints(self):
        groups={'482384':dict(title='The Wargame',image='')}
        known={'the wargame':[dict(identity='482384',title='The Wargame',year='2026')]}
        with patch.object(sources,'fetch',side_effect=sources.DiscoverError('offline')):
            metadata.enrich(self.store,groups,known)
        self.assertEqual(groups['482384']['title'],'The Wargame')
        card=sources._load_shows(self.store,['482384'])['482384']
        self.assertEqual(card['identityYear'],'2026');self.assertNotIn('tvmazeId',card)

    def test_changed_cached_identity_is_not_reused(self):
        metadata.save(self.store,'482384',SHOW,'The Wargame','2026')
        conflict={**SHOW,'externals':{'thetvdb':123}}
        with patch.object(sources,'fetch',return_value=json.dumps(conflict).encode()):
            with self.assertRaises(sources.DiscoverError):metadata.episode_show(self.store,'482384')
