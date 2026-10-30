"""Optional enrichment never requires a credential or weakens exact identity."""
import json
import unittest
from unittest.mock import patch

import test_view_media as fixture
import test_movie_info as movie_fixture
import test_show_info as show_fixture
from backend import discover_sources, m3u_metadata
from backend.view_media import movie_info, show_info, providers


class OptionalTitleInformationTests(unittest.TestCase):
    setUp=fixture.ViewMediaTests.setUp
    tearDown=fixture.ViewMediaTests.tearDown

    def get(self,query):return self.client.get('/api/view-media/'+query,base_url=fixture.ORIGIN)

    def cache(self,path,data,**params):
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO m3u_tmdb_cache VALUES(?,?,0)',(json.dumps([path,params],sort_keys=True),json.dumps(data)))

    def snapshot(self):
        with self.store.connect() as db:return '\n'.join(db.iterdump())

    def test_no_key_or_cache_is_successful_optional_absence_with_no_provider_or_db_writes(self):
        before=self.snapshot()
        with patch.object(movie_info,'TMDB',side_effect=AssertionError('No credentialed movie client')),patch.object(show_info,'TMDB',side_effect=AssertionError('No credentialed show client')),patch.object(show_info,'maze',side_effect=AssertionError('No new TVmaze request')),patch('backend.m3u_metadata.requests.get',side_effect=AssertionError('No provider request')):
            for query in ('movie-info?id=movie:1234567','show-info?id=show:77&tvdb=77','show-info?id=tvmaze:show:55'):
                response=self.get(query)
                self.assertEqual(response.status_code,200,response.json)
                self.assertEqual(response.json['facts'],{})
                self.assertEqual(response.json['cast'],[])
                self.assertTrue(response.json['unavailable'])
                self.assertNotIn('credential',str(response.json).lower())
        self.assertEqual(self.snapshot(),before)

    def test_keyless_exact_cached_movie_facts_survive_even_stale_cache_without_provider(self):
        self.cache('find/tt1234567',dict(movie_results=[dict(id=12)]),external_source='imdb_id')
        self.cache('movie/12',movie_fixture.MovieInfoTests.row(self),language='en-GB',append_to_response='credits,videos,release_dates')
        before=self.snapshot()
        with patch.object(movie_info,'TMDB',side_effect=AssertionError('Saved-only path')),patch('backend.m3u_metadata.requests.get',side_effect=AssertionError('No provider request')):
            response=self.get('movie-info?id=movie:1234567')
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['facts']['Runtime'],'98 min')
        self.assertEqual(response.json['cast'][0]['name'],'Actor One')
        self.assertEqual(response.json['source'],'Saved TMDB')
        self.assertEqual(self.snapshot(),before)

    def test_keyless_cached_show_uses_exact_external_link_and_rich_response(self):
        self.cache('find/77',dict(tv_results=[dict(id=12)]),external_source='tvdb_id')
        self.cache('tv/12',show_fixture.ShowInfoTests.row(self),language='en-GB',append_to_response='credits,videos,external_ids')
        with patch.object(show_info,'TMDB',side_effect=AssertionError('Saved-only path')),patch.object(show_info,'maze',side_effect=AssertionError('No fuzzy fallback')):
            response=self.get('show-info?id=show:77&tvdb=77')
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['facts']['Creator'],'Creator One')
        self.assertEqual(response.json['source'],'Saved TMDB')

    def test_exact_saved_tvdb_card_is_used_without_inventing_cast_or_fetching_tmdb(self):
        discover_sources._remember_show(self.store,'77',dict(id='show:77',title='MobLand',releaseDate='2025-03-30',genre='Crime',description='Saved exact show description.'))
        with patch.object(show_info,'TMDB',side_effect=AssertionError('No credentialed client')),patch.object(show_info,'maze',side_effect=AssertionError('No new TVmaze query')):
            response=self.get('show-info?id=show:77&tvdb=77')
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['title'],'MobLand')
        self.assertEqual(response.json['facts'],dict(Genres='Crime',Premiered='2025-03-30'))
        self.assertEqual(response.json['cast'],[])
        self.assertNotIn('Creator',response.json['facts'])

    def test_exact_saved_tvmaze_id_supplies_only_its_own_known_basic_information(self):
        providers.initialise(self.store)
        row=dict(id=55,name='Exact cached show',externals=dict(thetvdb=77,imdb='tt1234567'),premiered='2020-02-20',genres=['Drama'],summary='<p>Known summary.</p>')
        with self.store.connect() as db:db.execute('INSERT INTO view_media_cache VALUES(?,?,0)',('shows/55?',json.dumps(row)))
        with patch.object(show_info,'maze',side_effect=AssertionError('Cache miss must not trigger a provider')),patch.object(show_info,'TMDB',side_effect=AssertionError('No key')):
            response=self.get('show-info?id=tvmaze:show:55&tvdb=77')
            conflict=self.get('show-info?id=tvmaze:show:55&tvdb=99')
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['title'],'Exact cached show')
        self.assertEqual(response.json['description'],'Known summary.')
        self.assertEqual(conflict.status_code,400)

    def test_invalid_credential_or_provider_failure_stays_optional_and_never_echoes_provider_errors(self):
        self.store.set('m3u_tmdb_token','synthetic-invalid-configured-token')
        for failure in ('TMDB rejected the API credential.','Private provider https://private.invalid/token=secret'):
            with self.subTest(failure=failure),patch.object(movie_info,'TMDB') as client:
                client.return_value.get.side_effect=ValueError(failure)
                response=self.get('movie-info?id=tmdb:movie:12')
            self.assertEqual(response.status_code,200,response.json)
            self.assertTrue(response.json['unavailable'])
            self.assertNotIn('secret',str(response.json))
            self.assertNotIn('credential',str(response.json))

    def test_provider_failure_retains_exact_cached_show_facts(self):
        self.store.set('m3u_tmdb_token','synthetic-configured-token')
        self.cache('tv/12',show_fixture.ShowInfoTests.row(self),language='en-GB',append_to_response='credits,videos,external_ids')
        with patch.object(show_info,'TMDB') as client:
            client.return_value.get.side_effect=ValueError('Provider unavailable')
            response=self.get('show-info?id=tmdb:show:12&tvdb=77')
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['facts']['Creator'],'Creator One')
        self.assertEqual(response.json['source'],'Saved TMDB')

    def test_auth_input_and_cached_identity_conflicts_remain_errors_before_wrong_metadata(self):
        self.assertEqual(self.app.test_client().get('/api/view-media/show-info?id=show:77',base_url=fixture.ORIGIN).status_code,401)
        with patch.object(show_info,'TMDB',side_effect=AssertionError('No provider for invalid identity')):
            for query in ('show-info?id=movie:77','show-info?id=show:77&tvdb=99','show-info?id=show:77&tvdb=https://private.invalid'):
                self.assertEqual(self.get(query).status_code,400)
        wrong=movie_fixture.MovieInfoTests.row(self);wrong['imdb_id']='tt7654321'
        self.cache('movie/12',wrong,language='en-GB',append_to_response='credits,videos,release_dates')
        self.assertEqual(self.get('movie-info?id=tmdb:movie:12&imdb=1234567').status_code,400)

    def test_explicit_metadata_matching_still_requires_actionable_credential_configuration(self):
        with self.assertRaisesRegex(ValueError,'Save your TMDB API credential'):
            m3u_metadata.request(self.store,'nonexistent-source')


if __name__=='__main__':unittest.main()
