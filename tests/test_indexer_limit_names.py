"""Rate-limit notices identify only the affected indexers, with no private URLs."""
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import test_view_media as vm
from backend import discover_search as search, discover_sources as sources
from backend.discover_indexer_rate import limited_indexers
from backend.view_media import global_search, movie_sections, show_sections


class IndexerLimitNamesTests(unittest.TestCase):
    setUp=vm.ViewMediaTests.setUp
    tearDown=vm.ViewMediaTests.tearDown

    def test_partial_and_total_search_failures_carry_only_limited_indexer_names(self):
        indexers=[dict(name=name,url='https://'+name.lower()+'.example/api',api_key='private-secret') for name in ('Geek','Life','Healthy')]
        def fetch(url,**kwargs):
            if 'geek.example' in url:raise HTTPError('https://private.invalid/secret',429,'private',{},None)
            if 'life.example' in url:raise TimeoutError('private-secret')
            return b'<rss><channel/></rss>'
        with patch('backend.downloaders.nzb.indexers.api_base',side_effect=lambda url:url),patch.object(sources,'fetch',side_effect=fetch):
            good,warnings=search.query_all(indexers,{})
            self.assertEqual([name for name,_ in good],['Healthy'])
            self.assertEqual(warnings.limited_indexers,('Geek',))
            self.assertIn('request limit reached: Geek',warnings[0])
            with self.assertRaises(sources.DiscoverError) as caught:
                search.query_all(indexers[:2],{})
        self.assertEqual(limited_indexers(caught.exception),('Geek',))
        self.assertIn('Geek',str(caught.exception))
        self.assertNotIn('private',str(caught.exception))
        self.assertNotIn('private',str(warnings))

    def test_availability_preserves_multiple_named_limits(self):
        indexers=[dict(name=name,url='https://'+name.lower()+'.example',api_key='private-secret') for name in ('Geek','Life')]
        with patch('backend.downloaders.nzb.indexers.api_base',side_effect=lambda url:url),patch.object(sources,'indexers_for',return_value=indexers),patch.object(sources,'fetch',side_effect=HTTPError('https://private.invalid/secret',429,'private',{},None)):
            with self.assertRaises(sources.DiscoverError) as caught:
                sources.movie_availability(self.app,self.store,'1234567')
        self.assertEqual(limited_indexers(caught.exception),('Geek','Life'))

    def test_tv_catalogue_and_global_search_show_the_limited_name(self):
        error=sources.DiscoverError('private-secret');error.failures=('http_429',);error.limited_indexers=('NZBgeek',)
        row=dict(id='show:77',externalId='77',title='Show',year='2020',releaseDate='2020-01-01')
        with patch.object(show_sections,'catalogue',return_value=([row],None,'TV catalogue')),patch.object(show_sections,'details',return_value=row),patch.object(show_sections,'availability',side_effect=error):
            page=show_sections.browse(self.app,self.store,dict(collection='trending',q='',offset=0,cursor=None),[{}])
        self.assertIn('request limit reached: NZBgeek',page['warnings'][0]);self.assertNotIn('private',str(page))
        with patch.object(sources,'indexers_for',return_value=[{}]),patch.dict(global_search.REGISTRY,{'discover':{'browse':lambda *args:(_ for _ in ()).throw(error)}}):
            page=global_search.browse(self.app,self.store,dict(q='Show',cursor={'nzb_shows':0},offset=0))
        self.assertIn('request limit reached: NZBgeek',page['warnings'][0]);self.assertNotIn('private',str(page))

    def test_metadata_rate_limit_does_not_blame_an_indexer(self):
        error=ValueError('Metadata unavailable');error.__cause__=HTTPError('https://private.invalid',429,'busy',{},None)
        def get(path,**kwargs):
            if path.startswith('movie/'):raise error
            return dict(results=[dict(id=1,title='Film')],total_pages=1)
        with patch.object(movie_sections,'TMDB') as client:
            client.return_value.get.side_effect=get
            page=movie_sections.browse(self.app,self.store,dict(collection='trending',q='',offset=0,cursor=None),[{}])
        self.assertIn('TMDB metadata',page['warnings'][0]);self.assertNotIn('indexer request limit',str(page['warnings']))

    def test_provider_label_never_exposes_url_query_or_api_key(self):
        for name in ('HTTPS://private.invalid/api?apikey=private-secret','Label private-secret','Label ?api_key=private-secret'):
            self.assertEqual(sources._provider_name(dict(name=name,url='https://geek.example/api?apikey=private-secret',api_key='private-secret')),'geek.example')

    def test_retry_notice_uses_named_provider(self):
        from backend.discover_acquire import indexer_busy_message
        self.assertIn('request limit reached: NZBgeek',indexer_busy_message(provider='NZBgeek'))
