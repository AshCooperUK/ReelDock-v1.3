import unittest
from unittest.mock import patch
import test_view_media as vm
from backend.view_media import movie_info as info


class MovieInfoTests(unittest.TestCase):
    def setUp(self):
        vm.ViewMediaTests.setUp(self)
        self.store.set('m3u_tmdb_token','synthetic-configured-test-token')
    tearDown=vm.ViewMediaTests.tearDown
    def row(self):
        return dict(id=12,imdb_id='tt1234567',runtime=98,release_date='2026-02-20',vote_average=7.26,
                    original_language='en',spoken_languages=[dict(iso_639_1='en',english_name='English')],genres=[dict(name='Thriller')],
                    credits=dict(cast=[dict(id=1,name='Actor One',character='Nikki',profile_path='/portrait.jpg')],crew=[dict(id=2,name='Director One',job='Director')]),
                    videos=dict(results=[dict(site='YouTube',type='Trailer',key='abcdefghijk',official=True,name='Official trailer',iso_639_1='en')]))

    def test_exact_identity_facts_cast_and_trailer_use_separate_get(self):
        with patch.object(info,'TMDB') as client:
            client.return_value.entity.return_value={'id':12};client.return_value.get.return_value=self.row()
            response=self.client.get('/api/view-media/movie-info?id=movie:1234567',base_url=vm.ORIGIN)
        self.assertEqual(response.status_code,200,response.json)
        result=response.json
        self.assertEqual(result['facts']['Runtime'],'98 min');self.assertEqual(result['facts']['Director'],'Director One')
        self.assertEqual(result['facts']['Rating'],'7.3 / 10');self.assertEqual(result['facts']['Language'],'English')
        self.assertEqual(result['cast'][0]['role'],'Nikki');self.assertTrue(result['cast'][0]['image'].startswith('/api/m3u-artwork/'))
        self.assertEqual(result['trailer']['url'],'https://www.youtube.com/watch?v=abcdefghijk')
        client.return_value.entity.assert_called_once_with(dict(kind='movies',provider='imdb',identity='tt1234567'))
        client.return_value.get.assert_called_once_with('movie/12',language='en-GB',append_to_response='credits,videos,release_dates')

    def test_auth_and_invalid_or_conflicting_identities(self):
        self.assertEqual(self.app.test_client().get('/api/view-media/movie-info?id=movie:1234567',base_url=vm.ORIGIN).status_code,401)
        with patch.object(info,'TMDB') as client:
            for query in ('id=show:1234567','id=movie:1234567&imdb=7654321','id=tmdb:movie:12&imdb=https://private.invalid'):
                self.assertEqual(self.client.get('/api/view-media/movie-info?'+query,base_url=vm.ORIGIN).status_code,400)
        client.assert_not_called()

    def test_tmdb_identity_is_checked_against_known_imdb(self):
        with patch.object(info,'TMDB') as client:
            client.return_value.get.return_value=self.row()
            with self.assertRaisesRegex(ValueError,'do not agree'):info.details(self.store,'tmdb:movie:12','7654321')

    def test_only_safe_youtube_trailers_and_prefer_official(self):
        videos=[dict(site='YouTube',type='Trailer',key='abcdefghijk',official=False),
                dict(site='YouTube',type='Trailer',key='ABCDEFGHIJK',official=True),
                dict(site='YouTube',type='Trailer',key='https://private.invalid',official=True),
                dict(site='YouTube',type='Clip',key='12345678901',official=True),
                dict(site='Other',type='Trailer',key='12345678901',official=True)]
        self.assertEqual(info.trailer(videos)['url'],'https://www.youtube.com/watch?v=ABCDEFGHIJK')
        self.assertIsNone(info.trailer(videos[2:]))

    def test_missing_optional_trailer_does_not_hide_cast_or_facts(self):
        row=self.row();row['videos']={'results':[]}
        with patch.object(info,'TMDB') as client:
            client.return_value.get.side_effect=[row,ValueError('temporarily unavailable')]
            result=info.details(self.store,'tmdb:movie:12')
        self.assertIsNone(result['trailer']);self.assertTrue(result['cast']);self.assertTrue(result['facts'])

    def test_cast_is_bounded_and_missing_values_are_not_invented(self):
        row=self.row();row.update(runtime=None,vote_average=0,genres=[],spoken_languages=[],original_language=None)
        row['credits']['cast']=[dict(id=n,name='Actor '+str(n),character='Character',profile_path='https://private.invalid') for n in range(1,15)]
        with patch.object(info,'TMDB') as client:
            client.return_value.get.return_value=row;result=info.details(self.store,'tmdb:movie:12')
        self.assertEqual(len(result['cast']),6);self.assertFalse(any(p['image'] for p in result['cast']))
        self.assertNotIn('Runtime',result['facts']);self.assertNotIn('Rating',result['facts']);self.assertNotIn('Language',result['facts'])

    def test_guide_premiere_uses_actual_type_and_known_region_fallback(self):
        row=self.row();row['release_dates']={'results':[
            dict(iso_3166_1='GB',release_dates=[dict(type=3,release_date='2026-02-20T00:00:00Z')]),
            dict(iso_3166_1='US',release_dates=[dict(type=1,release_date='2026-02-01T00:00:00Z')])]}
        with patch.object(info,'TMDB') as client:
            client.return_value.get.return_value=row
            result=info.details(self.store,'tmdb:movie:12')
        self.assertEqual(result['guideDates'],dict(releaseDate='2026-02-20',premiereDate='2026-02-01'))
        row['release_dates']['results'][1]['release_dates'][0]['type']=4
        with patch.object(info,'TMDB') as client:
            client.return_value.get.return_value=row
            result=info.details(self.store,'tmdb:movie:12')
        self.assertEqual(result['guideDates']['premiereDate'],'','digital dates never stand in for a missing premiere')
