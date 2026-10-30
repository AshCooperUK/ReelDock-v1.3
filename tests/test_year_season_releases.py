"""Year-numbered TV seasons use the same identity checks as ordinary seasons.

The Panorama fixture reflects the mixed numbered/date-based results returned by
the exact episode query. No account, provider transfer or live data is used.
"""
import json
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch
import test_view_media as vm
from backend import discover_logic as logic, discover_sources as sources
from backend import discover_acquire as acquire, discover_store as ds
from backend import discover_search as search
from backend.discover_title_releases import title_identity
from backend.media_management import service, store as records


NAME = 'Panorama.S2026E20.The.Starmer.Fires.The.Russian.Connection'
EPISODE = dict(id=4000020, season=2026, number=20,
               name='The Starmer Fires- The Russian Connection')
ITEM = dict(id='show:80748', kind='show', title='Panorama', year='1953', externalId='80748')


def feed(rows):
    root = ET.Element('rss'); channel = ET.SubElement(root, 'channel')
    for n, (title, attrs) in enumerate(rows):
        row = ET.SubElement(channel, 'item'); ET.SubElement(row, 'title').text = title
        ET.SubElement(row, 'enclosure', url=f'https://index.example/get?id={n}&apikey=private')
        for key, value in attrs.items():
            ET.SubElement(row, 'attr', name=key, value=value)
    return root


def panorama_feed():
    return feed([
        (NAME + '.720p.iP.WEB-DL', dict(tvdbid='80748', season='S2026', episode='E20')),
        (NAME + '.1080p.iP.WEB-DL', dict(tvdbid='80748', season='S2026', episode='E20')),
        ('Panorama.2026.07.20.Theres.a.Gangster.in.My.Flat.1080p',
         dict(tvdbid='80748', season='2026', episode='07/20')),
        ('Panorama.2026.04.20.Antisemitism.1080p',
         dict(tvdbid='80748', season='2026', episode='04/20')),
        ('Panorama.S2026E21.Other.1080p', dict(tvdbid='80748', season='S2026', episode='E21')),
        ('Other.S2026E20.1080p', dict(tvdbid='999', season='S2026', episode='E20')),
    ])


class NumberingTests(unittest.TestCase):
    def test_year_seasons_work_without_changing_ordinary_or_special_numbering(self):
        for season, number in ((0, 1), (1, 20), (100, 123), (2026, 20), (9999, 9999)):
            with self.subTest(season=season, number=number):
                title = f'Show.S{season:02d}E{number:02d}.1080p'
                self.assertEqual(logic.release_pairs(title), [(season, number)])
                self.assertTrue(logic.single_episode(title, season, number))
                self.assertTrue(logic.release_covers(title, season, number))
                self.assertFalse(logic.single_episode(title, season, number + 1))

    def test_year_multi_episode_releases_and_packs_cannot_be_single_downloads(self):
        for suffix in ('E21', '-21', '-E21', '.E21', '.S2026E21'):
            with self.subTest(suffix=suffix):
                self.assertFalse(logic.single_episode('Show.S2026E20' + suffix + '.1080p', 2026, 20))
        for title in ('Show.S2026.1080p', 'Show.Season 2026.1080p'):
            self.assertTrue(logic.release_covers(title, 2026, 20))
            self.assertFalse(logic.single_episode(title, 2026, 20))
        episodes = [dict(id=1, season=2026, number=20), dict(id=2, season=2025, number=20)]
        self.assertEqual(logic.covered_episode_ids(NAME, episodes), [1])

    def test_overlong_or_date_codes_are_never_truncated_to_an_episode(self):
        for title in ('Show.S20260E20.1080p', 'Show.S2026E20000.1080p',
                      'Show.Season 20260.1080p', 'Show.2026.06.15.1080p'):
            with self.subTest(title=title):
                self.assertFalse(logic.release_covers(title, 2026, 20))

    def test_year_season_names_are_grouped_as_shows_not_unnumbered_films(self):
        self.assertEqual(sources._show_name(NAME + '.1080p'), 'Panorama')
        self.assertIsNone(title_identity('Panorama.S2026E20.2026.1080p'))

    def test_exact_provider_response_keeps_only_the_requested_identity_and_number(self):
        public, private = sources.episode_releases(ET.tostring(panorama_feed()), '80748', 2026, 20)
        self.assertEqual([r['quality'] for r in public], ['720p', '1080p'])
        self.assertEqual(len(private), 2)
        self.assertNotIn('private', json.dumps(public))

    def test_numbered_filename_fallback_handles_missing_attributes(self):
        root = feed([(NAME + '.1080p', dict(tvdbid='80748'))])
        self.assertEqual(len(sources.episode_releases(ET.tostring(root), '80748', 2026, 20)[0]), 1)

    def test_attributes_accept_padding_but_reject_partial_numbers_and_dates(self):
        for season, number, expected in [('S2026', 'E20', 1), ('2026', '020', 1),
                                         ('20260', '20', 0), ('2026', '20000', 0),
                                         ('2026', '20/07', 0), ('2026', 'E20-E21', 0)]:
            with self.subTest(season=season, number=number):
                root = feed([('Panorama.Unnumbered.1080p', dict(tvdbid='80748', season=season, episode=number))])
                self.assertEqual(len(sources.episode_releases(ET.tostring(root), '80748', 2026, 20)[0]), expected)


class EpisodePathTests(unittest.TestCase):
    setUp = vm.ViewMediaTests.setUp
    tearDown = vm.ViewMediaTests.tearDown

    def remember(self):
        key = logic.episode_identity('80748', 2026, 20)
        records.remember(self.store, key, ITEM['id'], 'episode', dict(
            item=ITEM, episode=EPISODE, automatic=True, date='2026-06-15'))
        return records.row(self.store, key)

    def test_federated_lookup_popup_and_download_choice_share_year_numbering(self):
        self.remember()
        with patch.object(sources, 'indexers_for', return_value=[{}, {}]), patch.object(
                search, 'query_all', return_value=([('Empty', feed([])), ('NZBgeek', panorama_feed())], [])) as query:
            response = self.client.post('/api/media-management', base_url=vm.ORIGIN,
                json=dict(action='releases', key='show:80748:2026:20'),
                headers={'Origin': vm.ORIGIN, 'X-CSRF-Token': 'token'})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(len(response.json['items']), 2)
        exact = next(r for r in response.json['items'] if r['quality'] == '1080p')
        self.assertEqual(exact['reason'], '')
        self.assertEqual(exact['source'], 'NZBgeek')
        saved = ds.candidate(self.store, exact['id'])
        self.assertIn('private', saved['grab'])
        selected = dict(saved, externalId=saved['external_id'])
        self.assertEqual(acquire.choose([selected], '1080', False, '80748', EPISODE), [selected])
        self.assertEqual(query.call_args.args[1], dict(t='tvsearch', tvdbid='80748', season=2026,
            ep=20, cat='5000', limit=100, offset=0, extended=1))
        self.assertNotIn('private', json.dumps(response.json))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM discover_requests').fetchone()[0], 0)

    def test_source_comparison_keeps_identity_pack_sample_and_quality_guards(self):
        row = self.remember()
        releases = [dict(id=str(n), title=title, quality=quality, externalId=identity)
            for n, (title, quality, identity) in enumerate([
                (NAME + '.1080p', '1080p', '80748'),
                (NAME + '.1080p', '1080p', '999'),
                ('Panorama.S2026E20E21.1080p', '1080p', '80748'),
                (NAME + '.sample.1080p', '1080p', '80748'),
                (NAME + '.720p', '720p', '80748')])]
        with patch.object(sources, 'search_episode', return_value=releases):
            result = service.candidates(self.app, self.store, row)
        self.assertEqual([r['reason'] for r in result['items']], ['', 'Different catalogue identity',
            'Different episode or unverified pack coverage', 'Sample content excluded',
            'Outside the selected quality policy'])

    def test_request_and_library_name_retain_the_complete_year_season(self):
        request = acquire.queue_manual(self.store, ITEM, [EPISODE], '1080', False,
                                       'NZB first', 'year-season-regression-request')[0]
        self.assertEqual(request['episode_key'], 'show:80748:2026:20')
        name = acquire.library_name(request, NAME + '.1080p')
        self.assertIn('S2026E20', name)
        from backend.m3u_naming import describe
        from backend.settings.m3u import DEFAULTS
        parsed = describe(name, '-1', 'TV', 'https://provider.example/episode.mp4', DEFAULTS)
        self.assertEqual((parsed['season'], parsed['episode']), (2026, 20))

    def test_selected_year_release_reaches_the_existing_downloader(self):
        row = self.remember()
        with patch.object(sources, 'indexers_for', return_value=[{}]), patch.object(
                search, 'query_all', return_value=([('NZBgeek', panorama_feed())], [])), patch.object(
                acquire, 'submit_nzb') as submit:
            choice = next(r for r in service.candidates(self.app, self.store, row)['items']
                          if r['quality'] == '1080p')
            service.queue(self.app, self.store, row, choice['id'])
        submit.assert_called_once()
        self.assertEqual(submit.call_args.args[2]['id'], choice['id'])
        self.assertEqual(json.loads(submit.call_args.args[3]['body'])['episode']['season'], 2026)

    def test_preview_does_not_clear_an_existing_cancellation_hold(self):
        row = self.remember()
        with self.store.connect() as db:
            db.execute('UPDATE managed_media SET hold=1 WHERE key=?', (row['key'],))
        with patch.object(sources, 'indexers_for', return_value=[{}]), patch.object(
                search, 'query_all', return_value=([('NZBgeek', panorama_feed())], [])):
            service.candidates(self.app, self.store, records.row(self.store, row['key']))
        self.assertTrue(records.row(self.store, row['key'])['hold'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM discover_requests').fetchone()[0], 0)
