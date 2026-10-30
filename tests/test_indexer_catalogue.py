"""Discover uses complete indexer lists; fallback never changes acquisition policy."""
import json
import time
import unittest
from contextlib import ExitStack
from unittest.mock import Mock,patch
from urllib.parse import parse_qs,urlsplit
from backend.view_media import indexer_catalogue as catalogue,adapters,movie_sections,show_sections,saved_catalogue
from backend import discover_sources as sources
import test_view_media as vm

ACCOUNT=dict(name='Indexer A',url='https://indexer.invalid/api',api_key='PRIVATE_KEY')

def rss(ids,kind='movie',total=None,offset=0):
    response='' if total is None else f'<n:response total="{total}" offset="{offset}"/>'
    rows=''.join(f'<item><title>Title {n} (2026)</title><{"imdbid" if kind=="movie" else "tvdbid"}>{n}</{"imdbid" if kind=="movie" else "tvdbid"}><coverurl>https://api.nzbgeek.info/covers/{n}.jpg</coverurl></item>' for n in ids)
    return ('<rss xmlns:n="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel>'+response+rows+'</channel></rss>').encode()


class IndexerCatalogueTests(unittest.TestCase):
    def setUp(self):
        vm.ViewMediaTests.setUp(self)
        self.pacing=patch('backend.discover_indexer_rate.INTERVAL',0);self.pacing.start();self.addCleanup(self.pacing.stop)
        self.dns=patch('backend.downloaders.nzb.indexers._assert_host');self.dns.start();self.addCleanup(self.dns.stop)
    tearDown=vm.ViewMediaTests.tearDown

    def args(self,media='movies',section='trending',**kw):return dict(media=media,collection=section,q='',sort='source',offset=0,cursor=None,**kw)

    def test_all_500_indexer_movies_survive_missing_metadata_and_no_release_probe(self):
        fallback=Mock(side_effect=AssertionError('A full primary feed must not load a TMDB list.'))
        with patch.object(sources,'fetch',return_value=rss(range(1000001,1000501),total=500)) as fetch,patch.object(movie_sections,'availability') as probe:
            page=catalogue.browse(self.app,self.store,self.args(),[ACCOUNT],fallback)
        self.assertEqual(len(page['items']),500);self.assertTrue(page['indexerComplete']);self.assertIsNone(page['nextCursor'])
        self.assertEqual(fetch.call_count,1);probe.assert_not_called();fallback.assert_not_called()
        self.assertFalse(any(i.get('releases') for i in page['items']));self.assertNotIn('PRIVATE_KEY',json.dumps(page))
        self.assertIn('/rss?',fetch.call_args.args[0])

    def test_full_tv_feed_keeps_titles_without_optional_metadata(self):
        fallback=Mock()
        with patch.object(sources,'fetch',return_value=rss(range(1,251),'show',250)),patch.object(show_sections,'details') as details:
            result=catalogue.browse(self.app,self.store,self.args('shows'),[ACCOUNT],fallback)
        self.assertEqual(len(result['items']),250);self.assertTrue(result['indexerComplete'])
        fallback.assert_not_called();details.assert_not_called()

    def test_movie_and_tv_ids_never_cross_when_feed_has_both_identifiers(self):
        raw=b'<rss><channel><item><title>A series (2026)</title><imdbid>tt7654321</imdbid><tvdbid>101</tvdbid></item></channel></rss>'
        self.assertEqual(catalogue.parse(raw,'show','Indexer')[0][0]['id'],'show:101')
        self.assertEqual(catalogue.parse(raw,'movie','Indexer')[0][0]['id'],'movie:7654321')

    def test_declared_pages_are_followed_without_three_batch_title_cap(self):
        def fetch(url,**kw):
            offset=int(parse_qs(urlsplit(url).query)['offset'][0]);return rss(range(1000000+offset,1000000+offset+2),total=8,offset=offset)
        with patch.object(sources,'fetch',side_effect=fetch) as request:
            result=catalogue.browse(self.app,self.store,self.args(),[ACCOUNT],Mock())
        self.assertEqual(len(result['items']),8);self.assertTrue(result['indexerComplete']);self.assertEqual(request.call_count,4)

    def test_ignored_offset_keeps_first_page_and_falls_back_instead_of_looping(self):
        fallback=Mock(return_value=dict(items=[dict(id='movie:999',title='Fallback',kind='movie')],nextCursor=None))
        with patch.object(sources,'fetch',return_value=rss([1000001,1000002],total=10)) as fetch:
            page=catalogue.browse(self.app,self.store,self.args(),[ACCOUNT],fallback)
        self.assertEqual(len(page['items']),3);self.assertFalse(page['indexerComplete']);fallback.assert_called_once()
        self.assertLess(fetch.call_count,5)

    def test_inconsistent_first_offset_retains_all_valid_primary_titles_before_fallback(self):
        fallback=Mock(return_value=dict(items=[dict(id='movie:999',title='Fallback',kind='movie')],nextCursor=None))
        with patch.object(sources,'fetch',return_value=rss([1000001,1000002],total=2,offset=105)):
            page=catalogue.browse(self.app,self.store,self.args(section='new'),[ACCOUNT],fallback)
        self.assertEqual([i['id'] for i in page['items']],['movie:1000001','movie:1000002','movie:999'])
        self.assertFalse(page['indexerComplete']);self.assertIn('Indexer catalogue + TMDB',page['catalogueSource'].replace('indexer','Indexer'))
        fallback.assert_called_once()

    def test_partial_error_resumes_declared_cursor_and_retains_known_titles(self):
        def fetch(url,**kw):
            if int(parse_qs(urlsplit(url).query)['offset'][0])==0:return rss([1000001,1000002],total=3)
            raise sources.DiscoverError('https://private.invalid/?apikey=PRIVATE_KEY')
        with patch.object(sources,'fetch',side_effect=fetch):first=catalogue.provider(self.store,ACCOUNT,'trending_movies','movie')
        self.assertEqual(first['cursor'],2);self.assertEqual(len(first['items']),2);self.assertFalse(first['complete'])
        self.assertNotIn('PRIVATE_KEY',json.dumps(first))
        with self.store.connect() as db:db.execute("UPDATE view_media_cache SET body=json_set(body,'$.retryAt',0)")
        with patch.object(sources,'fetch',return_value=rss([1000003],total=3,offset=2)) as fetch:
            second=catalogue.provider(self.store,ACCOUNT,'trending_movies','movie')
        self.assertEqual(len(second['items']),3);self.assertTrue(second['complete']);self.assertIn('offset=2',fetch.call_args.args[0])

    def test_generic_release_feed_cannot_masquerade_as_trending(self):
        raw=b'<rss><channel><item><title>Film.2026.1080p.WEB-DL</title><imdbid>1000001</imdbid><enclosure type="application/x-nzb" url="https://private.invalid/SECRET"/></item></channel></rss>'
        fallback=Mock(return_value=dict(items=[],nextCursor=None))
        with patch.object(sources,'fetch',return_value=raw):page=catalogue.browse(self.app,self.store,self.args(),[ACCOUNT],fallback)
        self.assertFalse(page['indexerComplete']);self.assertEqual(page['items'],[]);fallback.assert_called_once()
        self.assertNotIn('SECRET',json.dumps(page))

    def test_rss_failure_can_use_a_genuine_named_api_catalogue(self):
        def fetch(url,**kw):
            if '/rss?' in url:raise sources.DiscoverError('Unavailable')
            return rss([1000001],total=1)
        with patch.object(sources,'fetch',side_effect=fetch):result=catalogue.provider(self.store,ACCOUNT,'trending_movies','movie')
        self.assertTrue(result['complete']);self.assertEqual(result['items'][0]['id'],'movie:1000001')

    def test_no_indexer_calls_tmdb_fallback_without_availability_filter(self):
        with patch.object(sources,'indexers_for',return_value=[]),patch.object(movie_sections,'TMDB') as tmdb,patch.object(movie_sections,'availability') as release:
            tmdb.return_value.get.return_value=dict(results=[dict(id=8,title='No files yet',release_date='2026-01-01')],total_pages=1)
            result=adapters.discover(self.app,self.store,self.args())
        self.assertEqual(len(result['items']),1);self.assertEqual(result['freshness']['source'],'TMDB fallback')
        self.assertFalse(result['items'][0]['available']);release.assert_not_called()

    def test_no_indexer_tv_fallback_retains_verified_tv_identity_without_files(self):
        from test_show_sections import ShowSectionsTests
        def get(path,**kw):return ShowSectionsTests.row(self,1) if path=='tv/1' else dict(results=[dict(id=1)],total_pages=1)
        with patch.object(sources,'indexers_for',return_value=[]),patch.object(show_sections,'TMDB') as tmdb,patch.object(show_sections,'availability') as release:
            tmdb.return_value.get.side_effect=get
            result=adapters.discover(self.app,self.store,self.args('shows'))
        self.assertEqual([i['id'] for i in result['items']],['show:101']);self.assertFalse(result['items'][0]['available']);release.assert_not_called()

    def test_full_primary_from_one_indexer_is_not_replaced_by_unsupported_other(self):
        def fetch(url,**kw):
            if 'other.invalid' in url:raise sources.DiscoverError('Unsupported')
            return rss([1000001],total=1)
        fallback=Mock()
        with patch.object(sources,'fetch',side_effect=fetch):result=catalogue.browse(self.app,self.store,self.args(),[ACCOUNT,dict(ACCOUNT,url='https://other.invalid/api')],fallback)
        self.assertTrue(result['indexerComplete']);self.assertEqual(len(result['items']),1);fallback.assert_not_called()

    def test_healthy_cache_is_scoped_to_account_and_avoids_repeat_calls(self):
        with patch.object(sources,'fetch',return_value=rss([1000001],total=1)) as fetch:
            for account in [ACCOUNT,ACCOUNT,dict(ACCOUNT,api_key='DIFFERENT')]:catalogue.provider(self.store,account,'trending_movies','movie')
        self.assertEqual(fetch.call_count,2)

    def test_failed_refresh_retains_previous_feed_and_safe_fallback_scope(self):
        with patch.object(sources,'fetch',return_value=rss([1000001],total=1)):catalogue.provider(self.store,ACCOUNT,'trending_movies','movie')
        with self.store.connect() as db:db.execute("UPDATE view_media_cache SET body=json_set(body,'$.retryAt',0)")
        with patch.object(sources,'fetch',side_effect=ValueError('PRIVATE_KEY')):result=catalogue.browse(self.app,self.store,self.args(),[ACCOUNT],Mock(return_value=dict(items=[],nextCursor=None)))
        self.assertEqual(len(result['items']),1);self.assertFalse(result['indexerComplete']);self.assertIn('TMDB list fallback',result['scope']);self.assertNotIn('PRIVATE_KEY',json.dumps(result))

    def test_missing_or_zero_ids_trigger_fallback_without_guessed_title_matching(self):
        raw=rss([1000001,0],total=2)
        fallback=Mock(return_value=dict(items=[],nextCursor=None))
        with patch.object(sources,'fetch',return_value=raw):result=catalogue.browse(self.app,self.store,self.args(),[ACCOUNT],fallback)
        self.assertEqual([i['id'] for i in result['items']],['movie:1000001']);self.assertFalse(result['indexerComplete']);fallback.assert_called_once()

    def test_full_feed_deduplicates_padded_ids_without_merging_remakes(self):
        with patch.object(sources,'fetch',return_value=rss(['01000001','1000001','1000002'],total=3)):
            page=catalogue.browse(self.app,self.store,self.args(),[ACCOUNT],Mock())
        self.assertEqual([i['id'] for i in page['items']],['movie:1000001','movie:1000002']);self.assertTrue(page['indexerComplete'])

    def test_unconfirmed_feed_limit_requires_fallback_instead_of_claiming_complete(self):
        fallback=Mock(return_value=dict(items=[],nextCursor=None))
        with patch.object(sources,'fetch',return_value=rss(range(1000001,1001001))):result=catalogue.browse(self.app,self.store,self.args(),[ACCOUNT],fallback)
        self.assertEqual(len(result['items']),1000);self.assertFalse(result['indexerComplete']);fallback.assert_called_once()

    def test_short_feed_without_total_does_not_prove_the_whole_list_was_returned(self):
        with patch.object(sources,'fetch',return_value=rss([1000001])):
            result=catalogue.provider(self.store,ACCOUNT,'trending_movies','movie')
        self.assertEqual(len(result['items']),1);self.assertFalse(result['complete'])

    def test_all_metadata_fallback_cursor_does_not_skip_pages(self):
        args=self.args(section='all');args['cursor']={'nzb':100}
        with patch.object(sources,'indexers_for',return_value=[]),patch.object(movie_sections,'TMDB') as tmdb:
            tmdb.return_value.get.return_value=dict(results=[dict(id=30,title='Second page')],total_pages=3)
            result=adapters.discover(self.app,self.store,args)
        tmdb.return_value.get.assert_called_once_with('discover/movie',page=2,language='en-GB')
        self.assertEqual(json.loads(result['nextCursor']),{'nzb':200});self.assertIn('TMDB catalogue fallback',result['freshness']['scope'])

    def test_saved_catalogue_publishes_entire_primary_feed_in_one_refresh(self):
        self.store.set('catalogue_pre_download','true');saved_catalogue.initialise(self.store)
        with patch.object(sources,'indexers_for',return_value=[ACCOUNT]),patch.object(sources,'fetch',return_value=rss(range(1000001,1000501),total=500)):
            sig=saved_catalogue.signature(self.store)
            saved_catalogue.refresh_list(self.app,self.store,sig,'0','movies','trending')
            args=self.args();page=saved_catalogue.browse(self.app,self.store,args);seen=list(page['items'])
            while page['nextOffset'] is not None:
                page=saved_catalogue.browse(self.app,self.store,dict(args,offset=page['nextOffset']));seen+=page['items']
        self.assertEqual(len(seen),500);self.assertIsNone(page['nextCursor']);self.assertTrue(page['completeSet']);self.assertIn('Complete indexer list',page['freshness']['scope'])

    def test_feed_browsing_does_not_change_policies_favourites_jobs_or_accounts(self):
        from test_watchlist import WatchlistTests
        before=WatchlistTests.protected(self)
        with patch.object(sources,'fetch',return_value=rss([1000001],total=1)):catalogue.browse(self.app,self.store,self.args(),[ACCOUNT],Mock())
        self.assertEqual(WatchlistTests.protected(self),before)


if __name__=='__main__':unittest.main()
