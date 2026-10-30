import json
import unittest
from unittest.mock import patch
import test_view_media as vm
from backend import youtube_discover as engine, discover_sources as sources

class YouTubeDiscoverTests(unittest.TestCase):
    setUp=vm.ViewMediaTests.setUp
    tearDown=vm.ViewMediaTests.tearDown
    get=vm.ViewMediaTests.get

    def test_failed_momentum_does_not_block_other_charts_and_keeps_snapshot(self):
        sources.save_chart(self.store,'rising',{'items':[{'id':'old'}]})
        def channels(growth=False):
            if growth:raise ValueError('Channel growth snapshot mismatch.')
            return {'items':[{'id':'channel'}]}
        with patch.object(sources,'_chart_popular',return_value={'items':[{'id':'fresh'}]}),patch.object(sources,'_chart_all_time',return_value={'items':[{'id':'all'}]}),patch.object(sources,'_chart_channels',side_effect=channels):
            result=sources.refresh_charts(self.store)
        self.assertEqual(set(result),{'popular','all-time','channels'})
        self.assertEqual(sources.load_chart(self.store,'popular')['items'][0]['id'],'fresh')
        self.assertNotIn('stale',sources.load_chart(self.store,'popular'))
        self.assertTrue(sources.load_chart(self.store,'rising')['stale'])
        self.assertEqual(sources.load_chart(self.store,'rising')['items'][0]['id'],'old')
        with patch.object(sources,'_chart_popular') as popular:
            sources.refresh_charts(self.store)
            popular.assert_not_called()

    def test_chart_staleness_reaches_youtube_api(self):
        with patch.object(sources,'load_chart',return_value={'items':[],'stale':True,'source':'Kworb','fetchedAt':'2026-10-13T12:00:00Z'}):
            result=self.get('/api/youtube-discover?feed=popular').json
        self.assertTrue(result['freshness']['stale']);self.assertTrue(result['warnings'])
        self.assertEqual(len(result['feeds']),7)

    def test_topic_search_pagination_and_cache(self):
        with patch.object(sources,'search_youtube',return_value={'items':[], 'nextOffset':20}) as search:
            params={'feed':'topics','category':'science'}
            first=self.client.get('/api/youtube-discover',base_url=vm.ORIGIN,query_string=params)
            self.assertEqual(first.status_code,200,first.json)
            params.update(offset=first.json['nextOffset'])
            second=self.client.get('/api/youtube-discover',base_url=vm.ORIGIN,query_string=params)
            self.assertEqual(second.status_code,200,second.json)
            engine.browse(self.store,feed='topics',category='science')
            self.assertEqual([call.args for call in search.call_args_list],[('Science',0),('Science',20)])

    def test_recommendations_exclude_followed_channels_and_explain_three_to_one_mix(self):
        followed='UC'+'a'*22
        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES(?,?,1)',(followed,json.dumps({'name':'Technology tutorials'})))
        chart=[dict(id='youtube:channel:'+identity,channelId=identity,title='Channel '+str(i),kind='channel') for i,identity in enumerate([followed,*['UC'+str(i)*22 for i in range(6)]])]
        videos=[dict(channelId='UC'+str(i)*22,channel='Tech '+str(i)) for i in range(4)]
        videos.append(dict(channelId=followed,channel='Followed'))
        with patch.object(sources,'load_chart',return_value={'items':chart}),patch.object(engine,'_search',return_value={'items':videos}) as search:
            result=engine.browse(self.store,feed='recommended')
        self.assertNotIn(followed,[item['channelId'] for item in result['items']])
        self.assertEqual(len({item['id'] for item in result['items']}),len(result['items']))
        self.assertTrue(all(item['description'].startswith('Suggested for') for item in result['items'][:3]))
        self.assertTrue(result['items'][3]['description'].startswith('For variety'))
        self.assertIn('Technology',[call.args[0] for call in search.call_args_list][0])

    def test_recommendation_cold_start_and_search_failure_are_honest(self):
        with patch.object(sources,'load_chart',return_value={'items':[]}):
            result=engine.browse(self.store,feed='recommended')
            self.assertIn('Not enough topic information',result['warnings'][0])
            with patch.object(engine,'_search',side_effect=ValueError('unavailable')):
                result=engine.browse(self.store,feed='recommended',category='science')
            self.assertIn('temporarily unavailable',result['warnings'][0])

    def test_reusable_api_auth_and_validation(self):
        self.assertNotEqual(self.app.test_client().get('/api/youtube-discover',base_url=vm.ORIGIN).status_code,200)
        for suffix in ('feed=invalid','category=private','offset=-1','feed=topics&category=science&offset=24'):
            self.assertEqual(self.get('/api/youtube-discover?'+suffix).status_code,400)
        response=self.get('/api/youtube-discover?feed=topics')
        self.assertEqual(response.status_code,200);self.assertIn('Choose a topic',response.json['warnings'][0])

    def test_search_dates_use_one_flat_request_and_mark_uncertainty(self):
        from types import SimpleNamespace
        entries=[dict(id='abcdefghijk',title='Uploaded video',timestamp=1792051200),
                 dict(id='lmnopqrstuv',title='Scheduled',timestamp=1792051200,live_status='is_upcoming'),
                 dict(id='12345678901',title='Unknown date',timestamp='invalid')]
        def completed(command,**kwargs):
            self.assertIn('--flat-playlist',command)
            self.assertIn('youtubetab:approximate_date',command)
            return SimpleNamespace(returncode=0,stdout=json.dumps(dict(entries=entries)))
        with patch('subprocess.run',side_effect=completed) as run:
            result=sources.search_youtube('date test',dates=True)
        run.assert_called_once()
        self.assertEqual(result['items'][0]['published'][:10],'2026-10-15')
        self.assertTrue(result['items'][0]['publishedApproximate'])
        for item in result['items'][1:]:
            self.assertEqual(item['published'],'');self.assertFalse(item['publishedApproximate'])

    def test_global_date_search_does_not_change_normal_youtube_search_cache(self):
        engine._CACHE.clear()
        with patch.object(sources,'search_youtube',return_value=dict(items=[],nextOffset=None)) as search:
            engine.browse(self.store,query='dated query')
            engine.browse(self.store,query='dated query',search_dates=True)
            engine.browse(self.store,query='dated query',search_dates=True)
        self.assertEqual([call.kwargs for call in search.call_args_list],[{},dict(dates=True)])
