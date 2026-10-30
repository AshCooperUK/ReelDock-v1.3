import json
import unittest
from datetime import datetime,timedelta
from unittest.mock import patch
from xml.etree.ElementTree import fromstring
from zoneinfo import ZoneInfo
import test_view_media as vm
from backend.view_media import show_sections as sections,show_metadata as metadata
from backend.view_media.ordering import ordered
from backend import discovery_engine as engine,discover_sources as sources


class ShowSectionsTests(unittest.TestCase):
    setUp=vm.ViewMediaTests.setUp
    tearDown=vm.ViewMediaTests.tearDown

    def row(self,key=1,premiere='2022-03-14',aired=None):
        today=datetime.now(ZoneInfo('Europe/London')).date()
        return dict(id=key,name='Show '+str(key),first_air_date=premiere,vote_average=8.1,
            external_ids=dict(tvdb_id=100+key),last_episode_to_air=dict(air_date=aired or (today-timedelta(days=1)).isoformat()),
            next_episode_to_air=dict(air_date=(today+timedelta(days=1)).isoformat()))

    def test_exact_choices_defaults_and_reject_old_tv_sort_fields(self):
        self.assertEqual([v for k,v in sections.SECTIONS],['New TV Shows','Recently Aired TV Shows','Premiering TV Shows','Trending TV Shows','Top Rated TV Shows','Endorsed TV Shows','View All TV Shows'])
        self.assertEqual([v for k,v in sections.SORTS],['A-Z','Z-A','Latest','Latest Episode','Oldest','Oldest Episode','NZB Order','Year','Rating'])
        self.assertEqual(engine.default_browse_args('shows')['sort'],'release')
        with patch('backend.discover_sources.indexers_for',return_value=[{}]),patch.object(sections,'browse',return_value=dict(items=[],nextOffset=None)) as browse:
            response=self.client.get('/api/view-media?source=discover&media=shows&collection=recent',base_url=vm.ORIGIN)
            self.assertEqual(response.status_code,200,response.json);self.assertEqual(browse.call_args.args[2]['sort'],'release')
            for query in ('collection=other','sort=new','sort=year_asc','sort=rating_asc','sort=episode_added'):
                self.assertEqual(self.client.get('/api/view-media?source=discover&media=shows&'+query,base_url=vm.ORIGIN).status_code,400)

    def test_named_cursor_is_separate_from_all_indexer_cursor(self):
        with patch('backend.discover_sources.indexers_for',return_value=[{}]),patch.object(sections,'browse',return_value=dict(items=[],nextOffset=None)):
            for cursor,status in [({'shows':20},200),({'movies':20},400),({'nzb':100},400),({'shows':1},400)]:
                response=self.client.get('/api/view-media',base_url=vm.ORIGIN,query_string=dict(source='discover',media='shows',collection='trending',cursor=json.dumps(cursor)))
                self.assertEqual(response.status_code,status,response.json)

    def test_real_premiere_and_aired_dates_have_distinct_sort_orders(self):
        old=metadata.tmdb_card(self.row(1,'2013-01-01','2026-10-14'))
        new=metadata.tmdb_card(self.row(2,'2026-01-01','2026-10-01'))
        unknown=dict(id='unknown',title='Unknown')
        self.assertEqual([i['id'] for i in ordered([old,new,unknown],'release')],['show:102','show:101','unknown'])
        self.assertEqual([i['id'] for i in ordered([old,new,unknown],'episode_released')],['show:101','show:102','unknown'])
        self.assertEqual([i['id'] for i in ordered([old,new,unknown],'episode_released_asc')],['show:102','show:101','unknown'])

    def test_future_or_invalid_episode_dates_never_sort_as_aired(self):
        future=(datetime.now(ZoneInfo('Europe/London')).date()+timedelta(days=2)).isoformat()
        row=self.row(aired=future);row['last_air_date']=future
        self.assertEqual(metadata.tmdb_card(row)['lastEpisodeReleased'],'')
        self.assertEqual(metadata.day('2026-02-31'),'')
        self.assertEqual(metadata.tmdb_card(self.row())['lastEpisodeReleased'],self.row()['last_episode_to_air']['air_date'])

    def test_collection_queries_use_distinct_date_windows(self):
        today=datetime.now(ZoneInfo('Europe/London')).date()
        with patch.object(sections,'TMDB') as client,patch.object(sections,'named_collection',return_value=dict(items=[],providers=[])):
            client.return_value.get.return_value=dict(results=[],total_pages=1)
            for section,field,days in [('new','first_air_date',90),('recent','air_date',7),('premiering','first_air_date',-1)]:
                sections.catalogue(self.store,section,0,today,[{}]);args=client.return_value.get.call_args
                self.assertEqual(args.args[0],'discover/tv')
                self.assertEqual(args.kwargs[field+'.gte'],(today-timedelta(days=days)).isoformat())
                self.assertEqual(args.kwargs[field+'.lte'],(today+timedelta(days=90) if section=='premiering' else today).isoformat())

    def test_recent_includes_old_series_with_a_new_episode_and_excludes_future(self):
        today=datetime.now(ZoneInfo('Europe/London')).date()
        rows={1:self.row(1,'2013-01-01'),2:self.row(2,aired=(today+timedelta(days=1)).isoformat())}
        with patch.object(sections,'catalogue',return_value=([dict(id=1),dict(id=2)],None,'Checked schedule')),patch.object(sections,'TMDB') as client,patch.object(sections,'availability',return_value=[dict(id='release',title='Show.S01E01')]) as available:
            client.return_value.get.side_effect=lambda path,**kw:rows[int(path.split('/')[1])]
            result=sections.browse(self.app,self.store,dict(collection='recent',offset=0,cursor=None,q=''),[{}])
        self.assertEqual([i['externalId'] for i in result['items']],['101']);self.assertEqual(available.call_count,1)

    def test_only_positive_indexer_matches_are_displayed(self):
        with patch.object(sections,'catalogue',return_value=([dict(id=1)],None,'TMDB trending')),patch.object(sections,'TMDB') as client,patch.object(sections,'availability',return_value=[]):
            client.return_value.get.return_value=self.row()
            result=sections.browse(self.app,self.store,dict(collection='trending',offset=0,cursor=None,q=''),[{}])
        self.assertEqual(result['items'],[])

    def test_premiering_includes_future_catalogue_before_files_exist(self):
        today=datetime.now(ZoneInfo('Europe/London')).date()
        rows={1:self.row(1,(today+timedelta(days=10)).isoformat()),2:self.row(2,(today-timedelta(days=1)).isoformat())}
        with patch.object(sections,'catalogue',return_value=([dict(id=1),dict(id=2)],None,'TMDB upcoming')),patch.object(sections,'TMDB') as client,patch.object(sections,'availability') as available:
            client.return_value.get.side_effect=lambda path,**kw:rows[int(path.split('/')[1])]
            result=sections.browse(self.app,self.store,dict(collection='premiering',offset=0,cursor=None,q=''),[{}])
        self.assertEqual([i['externalId'] for i in result['items']],['101'])
        self.assertEqual(result['items'][0]['releases'],[]);self.assertFalse(result['incomplete'])
        self.assertIn('availability is checked separately',result['scope']);available.assert_not_called()

    def test_premiering_api_does_not_claim_unavailable_files_are_verified(self):
        item=dict(id='show:101',externalId='101',title='Future series',kind='show',releases=[])
        with patch.object(sources,'indexers_for',return_value=[{}]),patch.object(sections,'browse',return_value=dict(items=[item],nextOffset=None,catalogueOnly=True)):
            response=self.client.get('/api/view-media?source=discover&media=shows&collection=premiering',base_url=vm.ORIGIN)
        self.assertEqual(response.status_code,200,response.json);result=response.json
        self.assertEqual(result['freshness']['source'],'TMDB fallback')
        self.assertFalse(result['items'][0]['available']);self.assertEqual(result['items'][0]['releaseCount'],0)
        self.assertEqual(result['items'][0]['action']['item']['externalId'],'101')

    def test_indexer_premiering_membership_cannot_admit_already_premiered_series(self):
        item=dict(id='show:101',externalId='101',title='Indexer premiere',kind='show',provider='Indexer A',feed='premiering_shows')
        with patch.object(sections,'catalogue',return_value=([item],None,'Indexer Premiering catalogue')),patch.object(sections,'details',return_value=metadata.tmdb_card(self.row())),patch.object(sections,'availability') as available:
            result=sections.browse(self.app,self.store,dict(collection='premiering',offset=0,cursor=None,q=''),[{}])
        self.assertEqual(len(result['items']),0);available.assert_not_called()

    def test_named_premiering_feed_precedes_metadata_fallback(self):
        with patch.object(sections,'named_collection',return_value=dict(items=[dict(id='show:101')],providers=['Indexer A'])) as feed,patch.object(sections,'TMDB') as client:
            rows,following,scope=sections.catalogue(self.store,'premiering',0,datetime.now(ZoneInfo('Europe/London')).date(),[{}])
        self.assertEqual(rows[0]['id'],'show:101');self.assertIn('Indexer A',scope);client.return_value.get.assert_not_called()
        feed.assert_called_once_with(self.store,[{}],'premiering_shows')

    def test_ignored_named_function_cannot_masquerade_as_curated_catalogue(self):
        raw=b'<rss><channel><item><title>Some.Show.S01E01.1080p.WEB</title><tvdbid>101</tvdbid><enclosure type="application/x-nzb" url="https://indexer.invalid/file.nzb"/></item></channel></rss>'
        with patch.object(sources,'_named_catalogue',return_value=raw):
            for name in ('premiering_shows','endorsed_shows'):
                result=sections.named_collection(self.store,[dict(name='Indexer A')],name)
                self.assertEqual(result,dict(items=[],providers=[]))

    def test_real_catalogue_is_cached_without_publishing_private_urls(self):
        raw=b'<rss><channel><item><title>Future Series (2026)</title><tvdbid>101</tvdbid><link>https://indexer.invalid/?apikey=private-value</link></item></channel></rss>'
        with patch.object(sources,'_named_catalogue',return_value=raw) as fetch:
            first=sections.named_collection(self.store,[dict(name='Indexer A')],'premiering_shows')
            second=sections.named_collection(self.store,[dict(name='Indexer A')],'premiering_shows')
        self.assertEqual(fetch.call_count,1);self.assertEqual(first,second)
        self.assertEqual(first['items'][0]['feed'],'premiering_shows');self.assertNotIn('private-value',json.dumps(first))

    def test_unmapped_catalogue_title_is_not_reported_as_a_provider_failure(self):
        with patch.object(sections,'catalogue',return_value=([dict(id=1)],None,'TMDB trending')),patch.object(sections,'TMDB') as client,patch.object(sections,'availability') as available:
            client.return_value.get.return_value=dict(name='Unmapped show',external_ids={})
            result=sections.browse(self.app,self.store,dict(collection='trending',offset=0,cursor=None,q=''),[{}])
        self.assertEqual(result['items'],[]);self.assertFalse(result['incomplete']);available.assert_not_called()

    def test_indexer_endorsements_take_precedence_and_fallback_is_labelled(self):
        today=datetime.now(ZoneInfo('Europe/London')).date()
        with patch.object(sections,'endorsements',return_value=dict(items=[dict(id='show:101')],providers=['Indexer A'])),patch.object(sections,'TMDB') as client:
            rows,following,scope=sections.catalogue(self.store,'endorsed',0,today,[{}])
            client.return_value.get.assert_not_called();self.assertEqual(rows[0]['id'],'show:101');self.assertIn('Indexer A',scope)
        with patch.object(sections,'endorsements',return_value=dict(items=[],providers=[])),patch.object(sections,'TMDB') as client:
            client.return_value.get.side_effect=[dict(results=[dict(id=1)]),dict(results=[dict(id=2)],total_pages=1)]
            rows,following,scope=sections.catalogue(self.store,'endorsed',0,today,[{}])
            self.assertIn('TMDB recommendations',scope);self.assertIn('no indexer',scope)
            self.assertEqual(client.return_value.get.call_args.args[0],'tv/1/recommendations')

    def test_availability_is_cached_and_private_grab_addresses_stay_private(self):
        root=fromstring('<rss xmlns:n="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel><item><title>Show.1.S01E01.1080p</title><link>https://indexer.invalid/api?t=get&amp;apikey=private-value</link><n:attr name="tvdbid" value="101"/></item></channel></rss>')
        item=dict(externalId='101',title='Show 1',year='2022');indexers=[dict(url='https://indexer.invalid/api',api_key='private-value')]
        with patch('backend.discover_search.query_all',return_value=([('Indexer',root)],[])) as query:
            first=sections.availability(self.app,self.store,item,indexers);second=sections.availability(self.app,self.store,item,indexers)
        self.assertEqual(first,second);self.assertEqual(query.call_count,1);self.assertNotIn('private-value',json.dumps(first))
        self.assertNotIn('grab',first[0]);self.assertEqual(first[0]['externalId'],'101')

    def test_probe_rejects_conflicting_identity_and_sample(self):
        root=fromstring('<rss xmlns:n="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel><item><title>Wrong.S01E01</title><link>https://indexer.invalid/a.nzb</link><n:attr name="tvdbid" value="102"/></item><item><title>Show.1.S01E01.Sample</title><link>https://indexer.invalid/b.nzb</link><n:attr name="tvdbid" value="101"/></item></channel></rss>')
        with patch('backend.discover_search.query_all',return_value=([('Indexer',root)],[])):
            self.assertEqual(sections.probe(self.app,self.store,dict(externalId='101',title='Show 1'),[{}]),[])

    def test_bridge_missing_tvdb_link_recovers_using_verified_metadata(self):
        item=dict(id='show:417792',kind='show',externalId='417792',title='Bridge Of Lies')
        with patch.object(sources,'episodes',side_effect=[sources.DiscoverError('No TVmaze show matches this TVDB ID.'),dict(summary='Actual summary',image='')]) as episodes,patch.object(metadata,'details',return_value=dict(title='Bridge of Lies',year='2022')),patch('backend.media_management.service.enrol') as enrol:
            result=engine.prepare(self.app,self.store,item)
        self.assertEqual(result['year'],'2022');self.assertEqual(episodes.call_count,2);self.assertEqual(enrol.call_args.args[2]['title'],'Bridge of Lies')

    def test_cached_verified_metadata_retains_hints_for_missing_links(self):
        with patch.object(metadata,'TMDB') as client:
            client.return_value.get.side_effect=[dict(tv_results=[dict(id=1)]),self.row()]
            first=metadata.details(self.store,'101');second=metadata.details(self.store,'101')
        self.assertEqual(first,second);self.assertEqual(client.return_value.get.call_count,2)
        self.assertEqual(sources._load_shows(self.store,['101'])['101']['identityYear'],'2022')
