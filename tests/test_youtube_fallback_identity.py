import tempfile
import unittest
from unittest.mock import patch
from backend import discover_store, discovery_engine as engine
from backend.storage import Store


class YoutubeFallbackIdentityTests(unittest.TestCase):
    item = dict(id='tvmaze:show:71810', title='Big Brother', year='2023', externalId='440642')
    edition = dict(title='Big Brother', year='2023', country='GB', countryName='United Kingdom')
    episode = dict(season=4, number=15)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)

    def request(self, body):
        # Exercise the real per-request handoff lock with disposable request state.
        return discover_store.insert_request(self.store, dict(id='test',
            idempotency='youtube-fallback-identity', kind='episode', title='Big Brother',
            quality='1080', fallback=False, body=body))[0]

    def matches(self, title, description='', edition=None):
        return engine.youtube_title_matches(title, self.item, self.episode, self.edition if edition is None else edition, description)

    def test_rejects_other_series_country_reboot_and_episode(self):
        for title in ('NewsRadio S04E15 Big Brother', 'big brother canada s04e15',
                      'Big Brother Canada 2023 S04E15', 'Big Brother UK 2000 S04E15',
                      'Big Brother UK 2023 S04E14', 'Big Brother UK 2023 S04E15-E16',
                      'Big Brother UK 2023 S04E15 recap'):
            with self.subTest(title=title):self.assertFalse(self.matches(title))

    def test_requires_positive_edition_evidence(self):
        self.assertFalse(self.matches('Big Brother S04E15'))
        self.assertFalse(self.matches('Big Brother UK S04E15'))
        self.assertFalse(self.matches('Big Brother 2023 S04E15'))
        self.assertFalse(self.matches('Big Brother UK S04E15','Uploaded in 2023.'))
        self.assertFalse(self.matches('Big Brother UK 2023 S04E15', edition={}))
        self.assertTrue(self.matches('Big Brother UK 2023 S04E15 Full Episode'))
        self.assertTrue(self.matches('Big Brother S04E15 Full Episode', 'Big Brother United Kingdom, the 2023 series.'))

    def test_movie_requires_title_boundary_and_one_year(self):
        item=dict(title='It',year='2017')
        for title in ('Little 2017 Full Movie','It 1990 Full Movie','It 1990 Full Movie 2017','It 2017 Trailer'):
            self.assertFalse(engine.youtube_title_matches(title,item))
        self.assertTrue(engine.youtube_title_matches('It (2017) Full Movie',item))

    def test_search_results_and_authoritative_video_are_both_checked(self):
        body=dict(item=self.item,episode=self.episode,edition=self.edition)
        row=self.request(body)
        candidates=[dict(title='NewsRadio S04E15 Big Brother',videoId='wrong-series'),
                    dict(title='Big Brother Canada 2023 S04E15',videoId='wrong-country'),
                    dict(title='Big Brother UK 2023 S04E15',videoId='abcdefghijk')]
        with patch('backend.youtube_discover._search',return_value={'items':candidates}) as search, \
             patch('backend.catalogue.Importer') as importer, \
             patch('backend.discover_youtube.register_video') as register, \
             patch('backend.downloads.enqueue') as enqueue:
            importer.return_value.api.return_value={'items':[dict(id='abcdefghijk',snippet=dict(title='Big Brother Canada S04E15',description='2016'),contentDetails=dict(duration='PT45M'))]}
            self.assertFalse(engine.fallback_youtube(None,self.store,row,body))
            self.assertIn('2023 United Kingdom S04E15',search.call_args.args[0])
            importer.return_value.api.assert_called_once()
            register.assert_not_called();enqueue.assert_not_called()

    def test_unknown_country_does_not_start_automatic_search(self):
        body=dict(item=self.item,episode=self.episode,edition=dict(title='Big Brother',year='2023'))
        row=self.request(body)
        with patch('backend.youtube_discover._search') as search:
            self.assertFalse(engine.fallback_youtube(None,self.store,row,body))
        search.assert_not_called()
