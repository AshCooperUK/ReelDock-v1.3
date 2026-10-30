import unittest
from unittest.mock import patch
import test_view_media as vm
from backend.view_media import show_info as info


class ShowInfoTests(unittest.TestCase):
    def setUp(self):
        vm.ViewMediaTests.setUp(self)
        self.store.set('m3u_tmdb_token','synthetic-configured-test-token')
    tearDown=vm.ViewMediaTests.tearDown

    def row(self):
        return dict(id=12,external_ids=dict(tvdb_id=77,imdb_id='tt1234567'),first_air_date='2020-02-20',vote_average=7.26,
                    original_language='en',spoken_languages=[dict(iso_639_1='en',english_name='English')],genres=[dict(name='Drama')],
                    created_by=[dict(name='Creator One')],episode_run_time=[45],number_of_seasons=3,status='Returning Series',
                    credits=dict(cast=[dict(id=1,name='Actor One',character='Lead',profile_path='/portrait.jpg')]),
                    videos=dict(results=[dict(site='YouTube',type='Trailer',key='abcdefghijk',official=True,name='Official trailer')]))

    def test_tvdb_exact_match_returns_facts_cast_and_trailer(self):
        with patch.object(info,'TMDB') as client:
            client.return_value.entity.return_value={'id':12};client.return_value.get.return_value=self.row()
            response=self.client.get('/api/view-media/show-info?id=show:77&tvdb=77',base_url=vm.ORIGIN)
        self.assertEqual(response.status_code,200,response.json)
        result=response.json
        self.assertEqual(result['facts']['Creator'],'Creator One');self.assertEqual(result['facts']['Episode runtime'],'45 min')
        self.assertEqual(result['facts']['Seasons'],'3');self.assertEqual(result['cast'][0]['role'],'Lead')
        self.assertTrue(result['cast'][0]['image'].startswith('/api/m3u-artwork/'))
        self.assertEqual(result['trailer']['url'],'https://www.youtube.com/watch?v=abcdefghijk')
        client.return_value.entity.assert_called_once_with(dict(kind='series',provider='tvdb',identity='77'))
        client.return_value.get.assert_called_once_with('tv/12',language='en-GB',append_to_response='credits,videos,external_ids')

    def test_tvdb_show_alias_matches_exact_show_identity(self):
        with patch.object(info,'TMDB') as client:
            client.return_value.entity.return_value={'id':12};client.return_value.get.return_value=self.row()
            alias=self.client.get('/api/view-media/show-info?id=tvdb:show:77&tvdb=77',base_url=vm.ORIGIN)
            exact=self.client.get('/api/view-media/show-info?id=show:77&tvdb=77',base_url=vm.ORIGIN)
            bare=self.client.get('/api/view-media/show-info?id=tvdb:show:77',base_url=vm.ORIGIN)
        for response in (alias,bare):
            self.assertEqual(response.status_code,200,response.json)
            self.assertEqual(response.json,exact.json)
        self.assertEqual(alias.json['facts']['Creator'],'Creator One');self.assertEqual(alias.json['cast'][0]['role'],'Lead')
        self.assertEqual(alias.json['trailer']['url'],'https://www.youtube.com/watch?v=abcdefghijk')
        client.return_value.entity.assert_called_with(dict(kind='series',provider='tvdb',identity='77'))

    def test_tvdb_show_alias_rejects_conflicting_tvdb_before_any_provider_call(self):
        with patch.object(info,'TMDB') as client:
            for query in ('id=tvdb:show:77&tvdb=99','id=tvdb:show:0','id=tvdb:movie:77'):
                self.assertEqual(self.client.get('/api/view-media/show-info?'+query,base_url=vm.ORIGIN).status_code,400)
        client.assert_not_called()

    def test_auth_and_invalid_or_conflicting_identity(self):
        self.assertEqual(self.app.test_client().get('/api/view-media/show-info?id=show:77',base_url=vm.ORIGIN).status_code,401)
        with patch.object(info,'TMDB') as client:
            for query in ('id=movie:77','id=show:77&tvdb=99','id=imdb:show:1234567&imdb=7654321','id=show:77&tvdb=https://private.invalid'):
                self.assertEqual(self.client.get('/api/view-media/show-info?'+query,base_url=vm.ORIGIN).status_code,400)
        client.assert_not_called()

    def test_tmdb_external_identity_must_agree(self):
        with patch.object(info,'TMDB') as client:
            client.return_value.get.return_value=self.row()
            with self.assertRaisesRegex(ValueError,'do not agree'):info.details(self.store,'tmdb:show:12','99')
            with self.assertRaisesRegex(ValueError,'do not agree'):info.details(self.store,'tmdb:show:12','','7654321')

    def test_tvmaze_uses_external_identity_and_rejects_conflict(self):
        with patch.object(info,'maze',return_value=dict(externals=dict(thetvdb=77,imdb='tt1234567'))),patch.object(info,'TMDB') as client:
            client.return_value.entity.return_value={'id':12};client.return_value.get.return_value=self.row()
            self.assertTrue(info.details(self.store,'tvmaze:show:55')['cast'])
            with self.assertRaisesRegex(ValueError,'do not agree'):info.details(self.store,'tvmaze:show:55','99')
            client.return_value.entity.assert_called_once_with(dict(kind='series',provider='tvdb',identity='77'))

    def test_missing_optional_fields_do_not_hide_valid_facts(self):
        row=self.row();row.update(episode_run_time=[],created_by=[],number_of_seasons=None,videos={})
        with patch.object(info,'TMDB') as client:
            client.return_value.get.side_effect=[row,ValueError('unavailable')]
            result=info.details(self.store,'tmdb:show:12')
        self.assertIsNone(result['trailer']);self.assertEqual(result['facts']['Premiered'],'2020-02-20')
        self.assertNotIn('Episode runtime',result['facts']);self.assertNotIn('Creator',result['facts']);self.assertNotIn('Seasons',result['facts'])

    def test_unknown_tvmaze_identity_never_guesses_by_title(self):
        with patch.object(info,'maze',return_value=dict(externals={})),patch.object(info,'TMDB') as client:
            result=info.details(self.store,'tvmaze:show:55')
            self.assertTrue(result['unavailable'])
            self.assertEqual(result['facts'],{})
            self.assertEqual(result['cast'],[])
            client.return_value.entity.assert_not_called()
