import json
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch
import test_view_media as vm
from backend import discover_sources as sources, discover_manual_episodes as manual, youtube_discover as youtube
from backend.discover_acquire import _catalogue_identity,library_name

class IdentityFallbackTests(unittest.TestCase):
    setUp=vm.ViewMediaTests.setUp
    tearDown=vm.ViewMediaTests.tearDown
    get=vm.ViewMediaTests.get
    def catalogue(self):
        return dict(showId=60963,name='Bridge of Lies',externals={'imdb':'tt15852904','thetvdb':None},premiered='2022-03-14',image='',summary='Quiz show',episodes=[dict(id=1,name='Episode 1',season=1,number=1)],seasons=[{'number':1}])
    def post(self,path,body,csrf=True):
        return self.client.post(path,base_url=vm.ORIGIN,json=body,headers={'Origin':vm.ORIGIN,**({'X-CSRF-Token':'token'} if csrf else {})})
    def test_tvmaze_episode_identity_without_tvdb(self):
        with patch.object(sources,'fetch',side_effect=[json.dumps(dict(id=60963,name='Bridge of Lies',externals={'imdb':'tt15852904'})).encode(),b'[{"id":1,"name":"Pilot","season":1,"number":1}]',b'[{"number":1}]']) as fetch:
            response=self.get('/api/discover/episodes?tvmaze=60963')
            self.assertEqual(response.status_code,200,response.json)
            self.assertEqual(response.json['episodes'][0]['id'],1)
            self.assertEqual(fetch.call_args_list[0].args[0],'https://api.tvmaze.com/shows/60963')
        with patch.object(sources,'fetch') as fetch:
            self.assertNotEqual(self.get('/api/discover/episodes?tvmaze=../private').status_code,200)
            fetch.assert_not_called()
    def test_manual_source_is_offered_without_tvdb(self):
        from backend.view_media.download_sources import choices
        with patch.object(sources,'indexers_for',return_value=[{}]):
            result=choices(self.app,self.store,dict(id='tvmaze:show:60963',kind='show',title='Bridge of Lies',year='2022'))
        self.assertEqual(result['sources'][0]['type'],'manual')
    def test_manual_search_filters_samples_conflicting_ids_and_wrong_episodes(self):
        rows=[]
        for i,(title,imdb) in enumerate([('Bridge.of.Lies.S01E01.1080p','15852904'),('Bridge.of.Lies.S01E02.1080p','15852904'),('Bridge.of.Lies.S01E01.sample.1080p','15852904'),('Bridge.of.Lies.S01E01.1080p','9999999')]):
            rows.append(f'<item><title>{title}</title><enclosure url="https://indexer.example/{i}"/><attr name="imdb" value="{imdb}"/></item>')
        root=ET.fromstring('<rss><channel>'+''.join(rows)+'</channel></rss>')
        with patch.object(sources,'episodes',return_value=self.catalogue()),patch.object(sources,'indexers_for',return_value=[{}]),patch('backend.discover_search.query_all',return_value=([('Fixture',root)],[])):
            response=self.post('/api/discover/manual-episodes/search',{'tvmaze':'60963','episodeId':1})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(len(response.json['items']),1)
        self.assertNotIn('indexer.example',json.dumps(response.json))
        release=response.json['items'][0]
        with self.assertRaises(ValueError):manual.selected(self.store,'60963',2,release['id'])
        with self.assertRaises(ValueError):manual.selected(self.store,'60964',1,release['id'])
        with patch.object(sources,'episodes',return_value=self.catalogue()),patch('backend.discover_acquire.resolve_release') as resolve:
            manual.queue(self.app,self.store,'60963',1,release['id'],'request-token-identity-1234')
            row=resolve.call_args.args[2]
            self.assertEqual(_catalogue_identity(row),('imdb','tt15852904'))
            self.assertIn('S01E01',library_name(row,release['title']))
    def test_manual_route_requires_csrf_and_catalogue_episode(self):
        self.assertEqual(self.post('/api/discover/manual-episodes/search',{},csrf=False).status_code,403)
        with patch.object(sources,'episodes',return_value=self.catalogue()):
            self.assertEqual(self.post('/api/discover/manual-episodes/search',{'tvmaze':60963,'episodeId':99}).status_code,400)
    def test_public_channel_metadata_and_uploads_do_not_subscribe(self):
        channel='UC'+'z'*22;youtube._PUBLIC_CACHE.clear()
        html=b'<meta property="og:title" content="Public channel"><meta property="og:image" content="https://yt3.ggpht.com/example"><meta property="og:description" content="Public details">'
        with patch.object(sources,'load_chart',return_value={'items':[]}),patch.object(sources,'fetch',return_value=html):
            response=self.get('/api/guide/channel/'+channel+'/info')
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['channel']['avatar'],'https://yt3.ggpht.com/example')
        self.assertTrue(response.json['channel']['public'])
        with self.store.connect() as db:self.assertIsNone(db.execute('SELECT 1 FROM channels WHERE id=?',(channel,)).fetchone())
        with patch.object(sources,'uploads',return_value={'items':[{'videoId':'abcdefghijk'}]}):
            self.assertEqual(self.get('/api/youtube-discover/channel/'+channel+'/uploads').json['items'][0]['videoId'],'abcdefghijk')
    def test_youtube_related_feed_is_available(self):
        with patch.object(youtube,'related_videos',return_value={'items':[],'nextOffset':None}) as recommend:
            response=self.get('/api/youtube-discover?feed=related')
            self.assertEqual(response.status_code,200,response.json)
            self.assertEqual(response.json['feed'],'related');recommend.assert_called_once()
