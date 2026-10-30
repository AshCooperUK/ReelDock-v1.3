import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
from backend.app import create_app
from backend import discover_search as search, discover_sources as sources


def feed(*items, total=None):
    root=ET.Element('rss');channel=ET.SubElement(root,'channel')
    if total is not None: ET.SubElement(channel,'response',total=str(total))
    for n,(title,identity) in enumerate(items):
        row=ET.SubElement(channel,'item');ET.SubElement(row,'title').text=title
        ET.SubElement(row,'enclosure',url='https://index.example/get?id='+str(n)+'&apikey=private')
        if identity: ET.SubElement(row,'attr',name='tvdbid',value=identity)
    return root


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app=create_app({'TESTING':True,'DATA_DIR':self.temp.name,'MEDIA_DIR':self.temp.name+'/media'})
        self.store=self.app.extensions['store']
        sources.initialise(self.store);sources.SEARCH_CACHE.clear()
        self.enrichment = patch('backend.discover_metadata.enrich');self.enrichment.start();self.addCleanup(self.enrichment.stop)

    def test_wargame_provider_id_overrides_negative_metadata_cache(self):
        sources._remember_tvdb(self.store,'the wargame','')
        a=feed(('The.Wargame.S01.1080p.WEB-DL',''))
        b=feed(('The.Wargame.2026.S01E01.720p','482384'))
        # Different provider downloads must keep their own private candidates.
        b.find('./channel/item/enclosure').set('url','https://second.example/get?apikey=private')
        with patch.object(sources,'_resolve_shows',side_effect=AssertionError('unnecessary lookup')):
            result=search.television(self.store,[('Life',a),('Geek',b)],0)
        self.assertEqual(len(result['items']),1)
        self.assertEqual(result['items'][0]['externalId'],'482384')
        self.assertEqual(len(result['items'][0]['releases']),2)
        self.assertEqual({r['provider'] for r in result['items'][0]['releases']},{'Life','Geek'})
        self.assertEqual(len(result.pop('secret')),2)
        self.assertNotIn('private',json.dumps(result));self.assertEqual(result['unidentified'],[])

    def test_zero_imdb_is_an_unknown_placeholder_not_a_shared_movie_identity(self):
        root=feed(('Unrelated.Film.2026.1080p',''),('Another.Film.2026.720p',''),('Real.Film.2026.1080p',''))
        for row,identity in zip(root.findall('./channel/item'),('0','0000000','1234567')):ET.SubElement(row,'attr',name='imdb',value=identity)
        result=sources.parse_search(ET.tostring(root),'movies',0,'Indexer')
        self.assertEqual([item['externalId'] for item in result['items']],['1234567'])
        self.assertEqual(len(result['secret']),1)

    def test_conflicting_ids_and_years_are_not_guessed(self):
        root=feed(('Example.2020.S01E01','11'),('Example.2026.S01E01','22'),('Example.S01E02',''),('Example.2020.S01E02',''))
        r=search.television(self.store,[('One',root)],0)
        self.assertEqual(len(r['items']),2);self.assertEqual(len(r['unidentified']),1)
        self.assertEqual(len(next(x for x in r['items'] if x['externalId']=='11')['releases']),2)

    def test_missing_identity_stays_visible_without_candidate(self):
        with patch.object(sources,'_resolve_shows',return_value={}):
            r=search.television(self.store,[('One',feed(('Unknown.S01E01','')))],0)
        self.assertEqual(r['items'],[]);self.assertEqual(len(r['unidentified']),1);self.assertEqual(r['secret'],[])

    def test_failure_does_not_hide_other_indexer_or_leak_exception(self):
        indexes=[dict(name='One',url='https://one.example/api',api_key='private'),dict(name='Two',url='https://two.example/api',api_key='private')]
        def fetch(url,**kwargs):
            if 'one.example' in url: raise sources.DiscoverError(url)
            return ET.tostring(feed(('Example.S01E01','11')))
        with patch.object(sources,'fetch',side_effect=fetch), patch('backend.downloaders.nzb.indexers.api_base',side_effect=lambda url:url):
            good,warnings=search.query_all(indexes,{'q':'Example'})
        self.assertEqual(len(good),1);self.assertEqual(warnings,['One could not complete this search.'])
        self.assertNotIn('private',str(warnings))

    def test_availability_uses_small_probe_without_saving_download_candidates(self):
        root=feed(('Film.2026.1080p',''))
        ET.SubElement(root.find('./channel/item'),'attr',name='imdb',value='1234567')
        with patch.object(sources,'indexers_for',return_value=[{}]),patch.object(search,'query_all',return_value=([('One',root)],[])) as query,patch('backend.discover_store.remember_candidates') as remember:
            rows=sources.movie_availability(self.app,self.store,'1234567')
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['provider'],'One')
        self.assertEqual(query.call_args.args[1]['limit'],5)
        self.assertEqual(query.call_args.kwargs,dict(timeout=5,response_limit=512_000,store=self.store))
        remember.assert_not_called()

    def test_sample_probe_checks_remaining_releases_before_declaring_unavailable(self):
        root=feed(('Film.Sample.2026',''))
        ET.SubElement(root.find('./channel/item'),'attr',name='imdb',value='1234567')
        with patch.object(sources,'indexers_for',return_value=[{}]),patch.object(search,'query_all',return_value=([('One',root)],[])),patch.object(sources,'search_movie',return_value=[dict(id='full',title='Film.2026')]) as full:
            self.assertEqual(sources.movie_availability(self.app,self.store,'1234567')[0]['id'],'full')
        full.assert_called_once()

    def test_availability_stops_after_one_provider_confirms_a_movie(self):
        root=feed(('Film.2026',''));ET.SubElement(root.find('./channel/item'),'attr',name='imdb',value='1234567')
        with patch.object(sources,'indexers_for',return_value=[dict(name='First'),dict(name='Second')]),patch.object(search,'query_all',return_value=([('First',root)],[])) as query:
            self.assertTrue(sources.movie_availability(self.app,self.store,'1234567'))
        query.assert_called_once()

    def test_rate_limited_indexer_does_not_hide_another_indexers_match(self):
        root=feed(('Film.2026',''));ET.SubElement(root.find('./channel/item'),'attr',name='imdb',value='1234567')
        failure=sources.DiscoverError('Limited');failure.failures=('http_429',);failure.retry_at=999999
        with patch.object(sources,'indexers_for',return_value=[{},{}]),patch.object(search,'query_all',side_effect=[failure,([('Second',root)],[])]) as query:
            self.assertEqual(sources.movie_availability(self.app,self.store,'1234567')[0]['provider'],'Second')
        self.assertEqual(query.call_count,2)

    def test_availability_reports_safe_failure_codes_and_does_not_cache_partial_absence(self):
        from urllib.error import HTTPError,URLError
        with patch('backend.downloaders.nzb.indexers.api_base',return_value='https://index.example/api'),patch.object(sources,'fetch',side_effect=HTTPError('https://private.invalid/secret',429,'busy',{},None)):
            with self.assertRaises(sources.DiscoverError) as failure:search.query_all([dict(url='https://index.example',api_key='private')],dict(t='movie'))
        self.assertEqual(sources.failure_reason(failure.exception),'http_429')
        self.assertNotIn('private',sources.failure_reason(URLError('https://private.invalid/secret')))
        warnings=search.IndexerWarnings(['One could not complete this search.']);warnings.failures=('timeout',)
        with patch.object(sources,'indexers_for',return_value=[{}]),patch.object(search,'query_all',return_value=([('Two',feed())],warnings)):
            with self.assertRaises(sources.DiscoverError) as partial:sources.movie_availability(self.app,self.store,'1234567')
        self.assertEqual(sources.failure_reason(partial.exception),'timeout')

    def test_browse_queries_latest_category_releases_and_refreshes_after_five_minutes(self):
        indexes=[dict(name='One',url='https://one.example/api',api_key='private')]
        with patch.object(sources,'indexers_for',return_value=indexes),patch.object(search,'query_all',return_value=([('One',feed())],[])) as query,patch.object(search.time,'time',return_value=1000):
            first=search.search(self.app,self.store,'movies','',0,browse=True)
            self.assertEqual(query.call_args.args[1],dict(t='search',cat='2000',limit=100,offset=0,extended=1))
            search.search(self.app,self.store,'movies','',0,browse=True)
            self.assertEqual(query.call_count,1)
        with patch.object(sources,'indexers_for',return_value=indexes),patch.object(search,'query_all',return_value=([('One',feed())],[])) as query,patch.object(search.time,'time',return_value=1301):
            second=search.search(self.app,self.store,'movies','',0,browse=True)
            query.assert_called_once()
        self.assertIn('fetchedAt',first)
        self.assertIn('fetchedAt',second)
        with patch.object(sources,'indexers_for',return_value=indexes),patch.object(search,'query_all',return_value=([('One',feed())],[])) as query:
            search.search(self.app,self.store,'shows','',0,browse=True)
            self.assertEqual(query.call_args.args[1]['t'],'search')
            self.assertEqual(query.call_args.args[1]['cat'],'5000')
            search.search(self.app,self.store,'movies','Film',0)
            self.assertEqual(query.call_args.args[1]['t'],'search')
            self.assertEqual(query.call_args.args[1]['q'],'Film')

    def test_movie_keywords_search_release_names_in_movie_category(self):
        root=feed(('Guy.Martin.The.Worlds.Fastest.Electric.Car.2021.1080p',''),('Unidentified.Fast.Car.2026.1080p',''))
        ET.SubElement(root.find('./channel/item'),'attr',name='imdb',value='1234567')
        with patch.object(sources,'indexers_for',return_value=[dict(name='One')]),patch.object(search,'query_all',return_value=([('One',root)],[])) as query:
            result=search.search(self.app,self.store,'movies','  Fast  Car  ',100)
        self.assertEqual(query.call_args.args[1],dict(t='search',q='Fast Car',cat='2000',limit=100,offset=100,extended=1))
        self.assertEqual([row['externalId'] for row in result['items']],['1234567'])
        self.assertEqual(len(result['unidentified']),1,'unverified releases never become movie cards')
        self.assertNotIn('private',json.dumps(result))

    def test_disabled_indexers_are_excluded(self):
        p=Path(self.temp.name)/'nzb-engine';p.mkdir(exist_ok=True)
        (p/'settings.json').write_text(json.dumps({'indexers':[dict(url='https://one.example',api_key='x',enabled=False),dict(url='https://two.example',api_key='y',enabled=True)]}))
        self.assertEqual(len(sources.indexers_for(self.app,self.store)),1)

    def test_pagination_keeps_remaining_indexer_pages(self):
        r=search.page_info([('One',feed(('A.S01','1'),total=1)),('Two',feed(*[('B.S01','2')]*100,total=130))],0)
        self.assertEqual(r['total'],131);self.assertEqual(r['releaseCount'],101);self.assertEqual(r['nextOffset'],100)
        self.assertIsNone(search.page_info([('One',feed(total=1)),('Two',feed(*[('B.S01','2')]*30,total=130))],100)['nextOffset'])

    def test_metadata_failure_keeps_identified_releases(self):
        with patch.object(sources, '_resolve_shows', side_effect=ValueError('bad metadata')):
            result = search.television(self.store, [('One', feed(('Known.S01E01', '11'), ('Unknown.S01E01', '')))], 0)
        self.assertEqual(result['items'][0]['externalId'], '11')
        self.assertEqual(len(result['unidentified']), 1)

    def test_episode_search_uses_second_indexer_and_rejects_other_episodes(self):
        second=feed(('The.Wargame.2026.S01E01.1080p','482384'),
                    ('The.Wargame.2026.S01E02.1080p','482384'),
                    ('Other.S01E01.1080p','999'))
        with patch.object(sources,'indexers_for',return_value=[{},{}]), patch.object(search,'query_all',return_value=([('Life',feed()),('Geek',second)],[])) as query:
            releases=sources.search_episode(self.app,self.store,'482384',1,1)
        self.assertEqual(len(releases),1)
        self.assertEqual(releases[0]['provider'],'Geek')
        self.assertEqual(query.call_args.args[1]['tvdbid'],'482384')
        self.assertEqual(query.call_args.args[1]['extended'],1)
        from backend.discover_acquire import choose
        self.assertEqual(len(choose(releases,'1080',True,'482384',{'season':1,'number':1})),1)
        from backend.discover_store import candidate
        self.assertTrue(candidate(self.store,releases[0]['id'])['grab'])
        self.assertNotIn('private',json.dumps(releases))

    def test_episode_partial_outage_is_not_reported_as_no_match(self):
        with patch.object(sources,'indexers_for',return_value=[{},{}]), patch.object(search,'query_all',return_value=([('Life',feed())],['Geek could not complete this search.'])):
            with self.assertRaisesRegex(sources.DiscoverError,'incomplete'):
                sources.search_episode(self.app,self.store,'482384',1,1)

    def test_movie_acquisition_keeps_second_provider_matches(self):
        root=feed(('Fixture.2026.1080p',''))
        ET.SubElement(root.find('./channel/item'),'attr',name='imdb',value='1234567')
        with patch.object(sources,'indexers_for',return_value=[{},{}]), patch.object(search,'query_all',return_value=([('First',feed()),('Second',root)],[])):
            releases=sources.search_movie(self.app,self.store,'1234567')
        self.assertEqual(len(releases),1)
        self.assertEqual(releases[0]['provider'],'Second')
        self.assertEqual(releases[0]['externalId'],'1234567')

    def test_movie_lookup_requests_identity_metadata(self):
        def response(indexers,params,**kwargs):
            root=feed(('Insidious.Out.of.the.Further.2026.1080p.WEB-DL',''))
            if params.get('extended') == 1:
                ET.SubElement(root.find('./channel/item'),'attr',name='imdb',value='32393988')
            return [('Indexer',root)],[]
        with patch.object(sources,'indexers_for',return_value=[{}]),patch.object(search,'query_all',side_effect=response):
            releases=sources.search_movie(self.app,self.store,'32393988')
        from backend.discover_acquire import choose
        self.assertEqual(len(choose(releases,'1080',True,'32393988')),1)

    def test_movie_lookup_normalizes_padding_without_losing_request_identity(self):
        def response(indexers,params,**kwargs):
            self.assertEqual(params['imdbid'],'0305224')
            root=feed(('Anger.Management.2003.1080p.BluRay',''))
            ET.SubElement(root.find('./channel/item'),'attr',name='imdb',value='0305224')
            return [('Indexer',root)],[]
        with patch.object(sources,'indexers_for',return_value=[{}]),patch.object(search,'query_all',side_effect=response):
            releases=sources.search_movie(self.app,self.store,'00305224')
        self.assertEqual(len(releases),1)
        self.assertEqual(releases[0]['externalId'],'00305224')
