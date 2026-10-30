import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
from backend.app import create_app
from backend.nzb import stop
from backend import discover_logic, discover_store, discover_monitor, discover_acquire, downloads
from backend.discover_sources import parse_feed, parse_search, save_chart, shows_from_browse, merge_rss, episode_releases, _download_allowed, _matched_show, _norm_title
from backend.discover_youtube import adjust_command, claim_path

ORIGIN = 'https://dev.example.com'
FEED = b'''<?xml version="1.0"?><rss><channel>
<item><title>Known (2024)</title><imdbid>123</imdbid><coverurl>https://api.nzbgeek.info/covers/a.jpg</coverurl></item>
<item><title>No identity</title></item>
</channel></rss>'''
SEARCH = b'''<?xml version="1.0"?><rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
<channel><newznab:response offset="0" total="1"/>
<item><title>Example.Show.S02E01.1080p</title><enclosure url="https://api.nzbgeek.info/get?id=1&amp;apikey=secret-key"/>
<newznab:attr name="tvdbid" value="77"/><newznab:attr name="tvtitle" value="Example Show"/>
<newznab:attr name="size" value="1000"/></item>
<item><title>Remake.S02E01.1080p</title><enclosure url="https://api.nzbgeek.info/get?id=2&amp;apikey=secret-key"/>
<newznab:attr name="tvtitle" value="Remake"/></item>
</channel></rss>'''


class LogicTests(unittest.TestCase):
    def test_nzb_doctype_is_a_download_not_an_error_page(self):
        self.assertTrue(_download_allowed(b'<?xml version="1.0"?><!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN" "http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd"><nzb></nzb>'))
        self.assertTrue(_download_allowed(b'<!DOCTYPE rss><rss></rss>'))
        self.assertFalse(_download_allowed(b'<!DOCTYPE html><body>sign in</body>'))
        self.assertFalse(_download_allowed(b'<!DOCTYPE rss [<!ENTITY x "y">]><rss></rss>'))

    def test_episode_search_keeps_a_release_without_tvdbid(self):
        data = b'''<?xml version="1.0"?><rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel>
        <item><title>Abbott.Elementary.S01E01.1080p</title><enclosure url="https://api.nzb.life/get?id=1&amp;apikey=secret"/>
        <newznab:attr name="season" value="S01"/><newznab:attr name="episode" value="E01"/></item>
        <item><title>Abbott.Elementary.S01E02.720p</title><enclosure url="https://api.nzb.life/get?id=2&amp;apikey=secret"/>
        <newznab:attr name="season" value="S01"/><newznab:attr name="episode" value="E02"/></item>
        <item><title>Other.Show.S01E01.1080p</title><enclosure url="https://api.nzb.life/get?id=3&amp;apikey=secret"/>
        <newznab:attr name="tvdbid" value="999"/><newznab:attr name="season" value="S01"/><newznab:attr name="episode" value="E01"/></item>
        </channel></rss>'''
        public, secret = episode_releases(data, '402910', 1, 1)
        self.assertEqual(len(public), 1)
        self.assertEqual(public[0]['externalId'], '402910')
        self.assertEqual(public[0]['quality'], '1080p')
        self.assertNotIn('secret', json.dumps(public))
        self.assertTrue(secret[0]['grab'].startswith('https://'))

    def test_tv_search_pages_are_joined(self):
        merged = merge_rss([
            b'<rss><channel><item><title>One</title></item></channel></rss>',
            b'<rss><channel><item><title>Two</title></item></channel></rss>',
        ])
        titles = [item.findtext('title') for item in ET.fromstring(merged).findall('./channel/item')]
        self.assertEqual(titles, ['One', 'Two'])

    def test_show_match_accepts_a_possessive_or_subtitle(self):
        self.assertEqual(_norm_title("Mary Berry's"), _norm_title('Mary Berrys'))
        possessive = _matched_show("Mary Berrys Absolute Favourites", [
            {'show': {'name': "Mary Berry's Absolute Favourites", 'externals': {'thetvdb': 12}}},
        ])
        self.assertEqual(possessive['externals']['thetvdb'], 12)
        subtitle = _matched_show('Tougen Anki', [
            {'show': {'name': 'Tougen Anki: Legend of the Cursed Blood', 'externals': {'thetvdb': 34}}},
        ])
        self.assertEqual(subtitle['externals']['thetvdb'], 34)
        self.assertIsNone(_matched_show('Blue Ridge The Series', [
            {'show': {'name': 'Blue Ridge', 'externals': {'thetvdb': 56}}},
        ]))

    def test_strict_quality_rejects_unknown_and_lower(self):
        self.assertFalse(discover_logic.acceptable('Unspecified', '1080', False))
        self.assertFalse(discover_logic.acceptable('720p', '1080', False))
        self.assertTrue(discover_logic.acceptable('720p', '1080', True))
        self.assertFalse(discover_logic.acceptable('2160p', '1080', True))
        self.assertEqual([item['id'] for item in discover_logic.rank_releases([
            {'id': 'low', 'quality': '720p', 'size': 9},
            {'id': 'exact', 'quality': '1080p', 'size': 1},
            {'id': 'unknown', 'quality': 'Unspecified', 'size': 50},
        ], '1080', True)], ['exact', 'low'])

    def test_boundary_ignores_specials_and_future_airdates(self):
        episodes = [
            {'season': 0, 'airdate': '2020-01-01'},
            {'season': 1, 'airdate': '2024-01-01'},
            {'season': 2, 'airdate': '2026-12-01'},
            {'season': 3, 'number': 1},
        ]
        self.assertEqual(discover_logic.boundary_season(episodes, '2026-10-09'), 2)
        self.assertEqual(discover_logic.boundary_season([], '2026-10-09'), 1)

    def test_release_match_requires_codes_not_titles(self):
        self.assertTrue(discover_logic.release_covers('Show.S02E01.1080p', 2, 1))
        self.assertFalse(discover_logic.release_covers('Show.S02E01.1080p', 2, 2))
        self.assertTrue(discover_logic.release_covers('Show.S02.Season.Pack.1080p', 2, 3))
        self.assertFalse(discover_logic.release_covers('Example Show', 2, 1))

    def test_paging_does_not_skip_or_pass_the_cap(self):
        self.assertEqual(discover_logic.next_offset(0, 100, 250), 100)
        self.assertEqual(discover_logic.next_offset(200, 50, 250), None)
        self.assertEqual(discover_logic.next_offset(900, 100, 5000), 1000)
        with self.assertRaises(ValueError):
            discover_logic.next_offset(50, 100, 250)

    def test_feed_does_not_pad_and_search_hides_grab_urls(self):
        items = parse_feed(FEED, 'trending_movies')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['externalId'], '123')
        typed = b'''<?xml version="1.0"?><!DOCTYPE rss PUBLIC "-//Netscape Communications//DTD RSS 0.91//EN" "http://my.netscape.com/publish/formats/rss-0.91.dtd"><rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel>
        <item><title>Known.2024.1080p</title><newznab:attr name="imdb" value="tt55"/>
        <newznab:attr name="imdbtitle" value="Known"/><newznab:attr name="imdbyear" value="2024"/>
        <newznab:attr name="imdbscore" value="7.4"/><newznab:attr name="genre" value="Drama, Crime"/>
        <newznab:attr name="imdbplot" value="A known story."/>
        <newznab:attr name="coverurl" value="https://api.nzb.life/covers/movies/55-cover.jpg"/></item>
        <item><title>Show.S01E01.1080p</title><newznab:attr name="imdb" value="99"/></item>
        </channel></rss>'''
        life = parse_feed(typed, 'trending_movies', 'NZB.life')
        self.assertEqual(life[0]['title'], 'Known')
        self.assertEqual(life[0]['year'], '2024')
        self.assertEqual(life[0]['rating'], '7.4')
        self.assertEqual(life[0]['genre'], 'Drama, Crime')
        self.assertEqual(life[0]['description'], 'A known story.')
        self.assertEqual(len(life), 1)
        self.assertEqual(life[0]['externalId'], '55')
        self.assertEqual(life[0]['image'], 'https://api.nzb.life/covers/movies/55-cover.jpg')
        with self.assertRaises(ValueError):
            parse_feed(b'<!DOCTYPE rss [<!ENTITY x "y">]><rss></rss>', 'trending_movies')
        movie = b'''<?xml version="1.0"?><rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel>
        <item><title>The.Mask.1994.1080p</title><enclosure url="https://api.nzb.life/get?id=9&amp;apikey=secret-key"/>
        <newznab:attr name="imdb" value="0110475"/><newznab:attr name="imdbtitle" value="The Mask"/>
        <newznab:attr name="imdbyear" value="1994"/><newznab:attr name="imdbscore" value="6.9"/>
        <newznab:attr name="genre" value="Comedy, Fantasy"/><newznab:attr name="imdbplot" value="A mask."/>
        <newznab:attr name="coverurl" value="https://api.nzb.life/covers/movies/0110475-cover.jpg"/></item>
        </channel></rss>'''
        films = parse_search(movie, 'movies', 0)
        self.assertEqual(films['items'][0]['title'], 'The Mask')
        self.assertEqual(films['items'][0]['year'], '1994')
        self.assertEqual(films['items'][0]['genre'], 'Comedy, Fantasy')
        self.assertEqual(films['items'][0]['rating'], '6.9')
        self.assertEqual(films['items'][0]['description'], 'A mask.')
        self.assertEqual(films['items'][0]['image'], 'https://api.nzb.life/covers/movies/0110475-cover.jpg')
        parsed = parse_search(SEARCH, 'shows', 0)
        public = json.dumps({key: value for key, value in parsed.items() if key != 'secret'})
        self.assertNotIn('secret-key', public)
        self.assertNotIn('apikey', public)
        self.assertEqual(len(parsed['items']), 1)
        self.assertEqual(parsed['items'][0]['externalId'], '77')
        self.assertTrue(parsed['secret'][0]['grab'].startswith('https://'))

    def test_existing_youtube_command_still_allows_lower_heights(self):
        args = downloads.command('abcdefghijk', '1080', Path('/tmp'))
        self.assertIn('height<=1080', args[args.index('-f') + 1])
        strict = adjust_command(args, '1080', False)
        self.assertIn('height=1080', strict[strict.index('-f') + 1])
        self.assertNotIn('height<=1080', strict[strict.index('-f') + 1])
        self.assertIn('--no-playlist', strict)

    def test_disposition_keeps_unicode_names(self):
        header = discover_logic.content_disposition('Justin Bieber - Baby ft. Ludacris.mkv')
        self.assertIn("filename*=UTF-8''Justin%20Bieber%20-%20Baby%20ft.%20Ludacris.mkv", header)
        self.assertNotIn('secret', discover_logic.redact('failed https://api.example/x?apikey=secret'))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.media = Path(self.tmp.name) / 'media'
        self.media.mkdir()
        self.app = create_app({'TESTING': True, 'DATA_DIR': self.tmp.name, 'MEDIA_DIR': str(self.media)})
        self.store = self.app.extensions['store']
        self.store.set('public_origin', ORIGIN)
        self.store.set('setup_complete', '1')
        self.store.set('storage_base', self.tmp.name)
        with self.store.connect() as db:
            db.execute("INSERT INTO users VALUES(1,'owner','unused')")
        self.client = self.app.test_client()
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session['user_id'] = 1
            session['csrf'] = 'token'

    def tearDown(self):
        stop(self.app)
        self.tmp.cleanup()

    def post(self, path, body, csrf='token'):
        return self.client.post(path, base_url=ORIGIN, json=body, headers={'Origin': ORIGIN, 'X-CSRF-Token': csrf})

    def test_special_routes_require_csrf_and_explicit_candidate(self):
        with patch('backend.discover_specials.search', return_value={'items': [], 'warnings': [], 'query': 'Example'}) as search:
            body = {'tvdb': '482384', 'episodeId': 55}
            self.assertEqual(self.post('/api/discover/specials/search', body, csrf='bad').status_code, 403)
            search.assert_not_called()
            self.assertEqual(self.post('/api/discover/specials/search', body).status_code, 200)
        with patch('backend.discover_specials.queue') as queue:
            self.assertEqual(self.post('/api/discover/specials/acquire', body, csrf='bad').status_code, 403)
            queue.assert_not_called()

    def test_compact_show_acquisition_fits_existing_form_limit(self):
        body={'item':{'id':'show:12345','kind':'show','title':'Fixture','externalId':'12345'},
              'episodes':[{'id':1,'season':24,'number':1,'name':'Episode'}],
              'quality':'1080','fallback':True,'source':'NZB first','requestId':'compact-request-fixture'}
        with patch('backend.discover.process',side_effect=lambda app,store,row: row):
            response=self.post('/api/discover/acquire',body)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(len(response.json['requests']),1)

    def test_youtube_source_is_saved_but_requires_an_explicit_video_choice(self):
        saved = self.post('/api/discover/preferences', {'key': 'source', 'value': 'YouTube'})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(self.client.get('/api/discover/state', base_url=ORIGIN).json['preferences']['source'], 'YouTube')
        body = {'item': {'id': 'movie:123', 'kind': 'movie', 'title': 'Example', 'externalId': '123'},
                'quality': '720', 'fallback': True, 'source': 'YouTube', 'requestId': 'youtube-source-choice-fixture'}
        with patch('backend.downloads.enqueue') as enqueue, patch('backend.discover.process') as process:
            self.assertEqual(self.post('/api/discover/acquire', body).status_code, 400)
            process.assert_not_called()
            enqueue.assert_not_called()
            body['item'] = {'id': 'youtube:video:abcdefghijk', 'kind': 'video', 'videoId': 'abcdefghijk',
                            'title': 'Chosen upload', 'channelId': 'UC' + 'a' * 22, 'channel': 'Uploader'}
            self.assertEqual(self.post('/api/discover/acquire', body, csrf='no').status_code, 403)
            enqueue.assert_not_called()
            response = self.post('/api/discover/acquire', body)
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.json['requests'][0]['videoId'], 'abcdefghijk')
            self.assertEqual(enqueue.call_args.args[2], '720')
            self.assertFalse(enqueue.call_args.kwargs['automatic'])
            self.assertTrue(discover_store.quality_policy(self.store, 'abcdefghijk'))

    def test_routes_require_sign_in_and_preferences_round_trip(self):
        self.assertEqual(self.app.test_client().get('/api/discover/state', base_url=ORIGIN).status_code, 401)
        self.assertEqual(self.post('/api/discover/preferences', {'key': 'quality', 'value': '2160'}, csrf='no').status_code, 403)
        saved = self.post('/api/discover/preferences', {'key': 'quality', 'value': '2160'})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json['preferences']['quality'], '2160')
        again = self.client.get('/api/discover/state', base_url=ORIGIN)
        self.assertEqual(again.json['preferences']['quality'], '2160')
        self.assertFalse(again.json['capabilities']['torrent'])

    def test_future_rule_locks_boundary_and_scope_one_is_not_widened(self):
        episodes = [{'season': 1, 'number': 1, 'airdate': '2024-01-01', 'name': 'One', 'id': 1}]
        rule = discover_monitor.save_rule(self.store, {
            'showId': '77', 'title': 'Example', 'enabled': True, 'quality': '1080', 'fallback': False,
            'optIn': True, 'item': {'id': 'show:77', 'kind': 'show', 'title': 'Example', 'externalId': '77'},
        }, episodes)
        self.assertEqual((rule['season'], rule['scope']), (2, 'future'))
        changed = discover_monitor.save_rule(self.store, {
            'showId': '77', 'title': 'Example', 'enabled': True, 'quality': '720', 'fallback': True,
            'optIn': False, 'item': rule['item'],
        }, [{'season': 5, 'number': 1, 'airdate': '2020-01-01', 'id': 9}])
        self.assertEqual(changed['season'], 2)
        self.assertEqual(changed['quality'], '720')
        with self.store.connect() as db:
            db.execute("UPDATE discover_rules SET scope='one', season=4 WHERE id=?", (rule['id'],))
        kept = discover_monitor.save_rule(self.store, {
            'showId': '77', 'title': 'Example', 'enabled': True, 'quality': '1080', 'fallback': False,
            'optIn': False, 'item': rule['item'],
        }, episodes)
        self.assertEqual(kept['scope'], 'one')
        self.assertEqual(kept['season'], 4)

    def test_monitor_queues_a_future_episode_once_across_restart_and_pause(self):
        discover_monitor.save_rule(self.store, {
            'showId': '77', 'title': 'Example Show', 'enabled': True, 'quality': '1080', 'fallback': False,
            'optIn': True, 'item': {'id': 'show:77', 'kind': 'show', 'title': 'Example Show', 'externalId': '77'},
        }, [{'season': 1, 'number': 1, 'airdate': '2024-01-01', 'id': 1}])
        episodes = [
            {'id': 1, 'season': 1, 'number': 1, 'airdate': '2024-01-01', 'name': 'Old'},
            {'id': 2, 'season': 2, 'number': 1, 'airdate': '2020-02-01', 'name': 'New'},
        ]
        calls = []

        def fake_search(app, store, tvdb, season, number):
            return [
                {'id': 'rel:1', 'title': 'Example.Show.S02E01.1080p', 'quality': '1080p', 'size': 10, 'externalId': '77'},
                {'id': 'rel:2', 'title': 'Other.Show.S02E01.720p', 'quality': '720p', 'size': 10, 'externalId': '88'},
            ]

        def fake_submit(app, store, release, request_row=None, generation=None):
            calls.append(release['id'])
            return {'engine': 'nzb', 'id': 'job1', 'state': 'queued'}

        with patch('backend.discover_sources.search_episode', fake_search), patch('backend.discover_acquire.submit_nzb', fake_submit), patch('backend.discover_acquire.sync_jobs'):
            discover_monitor.tick(self.app, self.store, fetch_episodes=lambda show_id: episodes)
            discover_monitor.tick(self.app, self.store, fetch_episodes=lambda show_id: episodes)
        self.assertEqual(calls, ['rel:1'])
        with self.store.connect() as db:
            count = db.execute("SELECT count(*) FROM discover_requests WHERE episode_key='show:77:2:1'").fetchone()[0]
        self.assertEqual(count, 1)
        discover_monitor.set_enabled(self.store, '77', False)
        with patch('backend.discover_sources.search_episode', fake_search), patch('backend.discover_acquire.submit_nzb', fake_submit), patch('backend.discover_acquire.sync_jobs'):
            discover_monitor.tick(self.app, self.store, fetch_episodes=lambda show_id: episodes + [
                {'id': 3, 'season': 2, 'number': 2, 'airdate': '2026-03-01', 'name': 'Next'}])
        self.assertEqual(calls, ['rel:1'])

    def test_readable_names_do_not_overwrite(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO media_files(id,channel,path,day,episode,profile) VALUES ('abcdefghijk','channel','old.mp4','2026-01-01',1,'emby')")
        with self.store.connect() as db:
            db.execute("INSERT INTO media_channels VALUES ('channel','Creator')")
        folder = self.media / 'Discover/Creator'
        folder.mkdir(parents=True)
        (folder / 'Baby.mkv').write_bytes(b'kept')
        relative = claim_path(self.store, self.media, 'abcdefghijk', 'Baby')
        self.assertEqual(relative, 'Discover/Creator/Baby (2).mkv')
        self.assertEqual((folder / 'Baby.mkv').read_bytes(), b'kept')

    def test_subscribe_joins_reeldock_and_waits_to_sync_youtube(self):
        from backend import youtube_account
        channel = 'UC' + 'c' * 22
        item = {'id': 'youtube:channel:' + channel, 'kind': 'channel', 'title': 'Example channel', 'channelId': channel}
        saved = self.post('/api/discover/subscribe', {'item': item})
        self.assertEqual(saved.status_code, 200, saved.json)
        self.assertTrue(saved.json['subscribed'])
        self.assertEqual(saved.json['youtube'], 'waiting')
        self.assertIn(channel, saved.json['subscriptions'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT active FROM channels WHERE id=?', (channel,)).fetchone()[0], 1)
            self.assertIsNotNone(db.execute('SELECT 1 FROM youtube_manual_channels WHERE id=?', (channel,)).fetchone())
        calls = []
        with patch.object(youtube_account, 'google_ready', return_value=True), patch.object(youtube_account, '_google', side_effect=lambda *args: calls.append(args[-1])):
            self.assertEqual(youtube_account.flush(self.app, self.store), 1)
        self.assertEqual(calls, ['subscribe'])
        removed = self.post('/api/discover/subscribe', {'item': item})
        self.assertEqual(removed.status_code, 200, removed.json)
        self.assertFalse(removed.json['subscribed'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT active FROM channels WHERE id=?', (channel,)).fetchone()[0], 0)
            self.assertIsNotNone(db.execute('SELECT 1 FROM removed_channels WHERE id=?', (channel,)).fetchone())

    def test_latest_uploads_come_from_youtube_subscriptions(self):
        subscribed = 'UC' + 'a' * 22
        inactive = 'UC' + 'b' * 22
        video = {'id': 'abcdefghijk', 'title': 'From a subscription', 'description': 'Saved from the guide.'}
        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES(?,?,1)', (subscribed, json.dumps({'id': subscribed, 'name': 'Subscribed channel'})))
            db.execute('INSERT INTO channels VALUES(?,?,0)', (inactive, json.dumps({'id': inactive, 'name': 'Removed channel'})))
            db.execute('INSERT INTO videos VALUES(?,?,?,?,?,0)', (
                video['id'], subscribed, '2026-10-01', '2026-10-01T12:00:00+01:00', json.dumps(video)))
            db.execute('INSERT INTO videos VALUES(?,?,?,?,?,0)', (
                'zzzzzzzzzzz', inactive, '2026-10-02', '2026-10-02T12:00:00+01:00', json.dumps({'id': 'zzzzzzzzzzz', 'title': 'Hidden'})))
        response = self.client.get('/api/discover/uploads', base_url=ORIGIN)
        self.assertEqual(response.status_code, 200)
        titles = [item['title'] for item in response.json['items']]
        self.assertEqual(titles, ['From a subscription'])
        self.assertEqual(response.json['items'][0]['channel'], 'Subscribed channel')
        self.assertEqual(response.json['source'], 'subscriptions')
        self.assertGreaterEqual(response.json['totalChannels'], 1)

    def test_activity_row_uses_the_show_poster_instead_of_an_m3u_label(self):
        from backend.download_history import page
        discover_store.insert_request(self.store, {
            'idempotency': 'activity-token-000001', 'kind': 'episode', 'title': 'Example Show · S01E01',
            'quality': '1080', 'fallback': False, 'episode_key': 'show:77:1:1', 'state': 'waiting',
            'body': {'externalId': '77'}, 'message': 'Waiting for a release.',
        })
        with patch('backend.discover_sources.show_media', return_value={'image': 'https://static.tvmaze.com/uploads/images/example.jpg', 'summary': 'A short summary.'}):
            data = page(self.store, str(self.media), 15)
        row = next(item for item in data['items'] if item['kind'] == 'discover')
        self.assertEqual(row['item']['subscription'], 'Discover')
        self.assertEqual(row['item']['mediaKind'], 'show')
        self.assertEqual(row['item']['artwork'], 'https://static.tvmaze.com/uploads/images/example.jpg')
        self.assertEqual(row['item']['description'], 'A short summary.')

    def test_chart_snapshot_stores_the_error_column(self):
        save_chart(self.store, 'all-time', {'title': 'All-time', 'items': []})
        with self.store.connect() as db:
            row = db.execute('SELECT error FROM discover_charts WHERE name=?', ('all-time',)).fetchone()
        self.assertEqual(row['error'], '')

    def test_show_browse_keeps_one_exact_tvmaze_name(self):
        data = b'''<?xml version="1.0"?><rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel>
        <item><title>Example.Show.S01E01</title><newznab:attr name="tvtitle" value="Pilot"/>
        <newznab:attr name="tvairdate" value="2024-01-02"/></item>
        <item><title>Example.Show.S01E02</title><newznab:attr name="tvtitle" value="Next"/></item>
        <item><title>Ambiguous.Show.S01E01</title><newznab:attr name="tvtitle" value="Start"/></item>
        </channel></rss>'''

        def fake_fetch(url, limit=1, timeout=25):
            self.assertNotIn('Pilot', url)
            if 'Ambiguous' in url:
                return b'[{"show":{"name":"Ambiguous Show","externals":{"thetvdb":1}}},{"show":{"name":"Ambiguous Show","externals":{"thetvdb":2}}}]'
            return b'[{"show":{"name":"Example Show","externals":{"thetvdb":77},"genres":["Drama"],"image":{"medium":"https://static.tvmaze.com/uploads/images/example.jpg"},"summary":"A short summary."}}]'

        with patch('backend.discover_sources.fetch', fake_fetch):
            items, finished = shows_from_browse(self.store, data, 'trending_shows', 'NZB.life')
        self.assertTrue(finished)
        self.assertEqual([item['externalId'] for item in items], ['77'])
        self.assertEqual(items[0]['year'], '2024')
        self.assertEqual(items[0]['image'], 'https://static.tvmaze.com/uploads/images/example.jpg')
        self.assertEqual(items[0]['genre'], 'Drama')
        self.assertEqual(items[0]['description'], 'A short summary.')

    def test_movie_download_searches_by_imdb_id(self):
        from backend.discover_sources import search_movie
        data = b'''<?xml version="1.0"?><rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel>
        <item><title>Mask.1985.DC.720p.BluRay.x264</title><enclosure url="https://api.nzb.life/get?id=1"/>
        <newznab:attr name="imdb" value="0089560"/></item>
        <item><title>Mask.1985.DC.1080p.BluRay.x264</title><enclosure url="https://api.nzb.life/get?id=2"/>
        <newznab:attr name="imdb" value="0089560"/></item>
        <item><title>The.Mask.1994.720p</title><enclosure url="https://api.nzb.life/get?id=3"/>
        <newznab:attr name="imdb" value="0110475"/></item>
        </channel></rss>'''
        seen = {}

        def fake_query(indexer, params, **kwargs):
            seen.update(params)
            import xml.etree.ElementTree as ET
            return [('NZB.life', ET.fromstring(data))], []

        with patch('backend.discover_sources.indexers_for', return_value=[{'name': 'NZB.life', 'url': 'https://api.nzb.life/api', 'api_key': 'secret'}]), patch('backend.discover_search.query_all', fake_query):
            releases = search_movie(self.app, self.store, '0089560')
        self.assertEqual(seen['t'], 'movie')
        self.assertEqual(seen['imdbid'], '0089560')
        self.assertEqual(seen['extended'], 1)
        self.assertEqual([item['quality'] for item in releases], ['720p', '1080p'])
        self.assertTrue(all(item['externalId'] == '0089560' for item in releases))

    def test_saved_nzb_job_is_still_downloading(self):
        job_dir = Path(self.tmp.name) / 'nzb-engine' / 'jobs'
        job_dir.mkdir(parents=True)
        (job_dir / 'abc.json').write_text(json.dumps({
            'id': 'abc', 'phase': 'download', 'error': '', 'done_parts': 0, 'total_parts': 10, 'published': {},
        }), encoding='utf-8')
        discover_store.insert_request(self.store, {
            'idempotency': 'mask-live-000000000001', 'kind': 'movie', 'title': 'Mask (1985)',
            'quality': '1080', 'fallback': True, 'state': 'failed', 'engine': 'nzb', 'engine_id': 'abc',
            'episode_key': 'movie:89560', 'message': 'The NZB job is no longer in the downloader.',
            'progress': 6, 'body': {},
        })

        class App:
            extensions = {}

        discover_acquire.sync_jobs(App(), self.store)
        with self.store.connect() as db:
            row = db.execute('SELECT state, message, progress FROM discover_requests WHERE engine_id=?', ('abc',)).fetchone()
        self.assertEqual(row['state'], 'downloading')
        self.assertEqual(row['message'], 'Downloading from Usenet.')
        self.assertEqual(row['progress'], 6)

    def test_finished_library_path_completes_the_request(self):
        library = Path(self.tmp.name) / 'library'
        folder = library / 'Movies' / 'Teen Wolf (1985)'
        folder.mkdir(parents=True)
        (folder / 'Teen Wolf (1985).mkv').write_bytes(b'video')
        self.store.set('storage_base', str(library))
        job_dir = Path(self.tmp.name) / 'nzb-engine' / 'jobs'
        job_dir.mkdir(parents=True)
        (job_dir / 'wolf.json').write_text(json.dumps({
            'id': 'wolf', 'phase': 'stopped', 'error': '', 'done_parts': 10, 'total_parts': 10,
            'published': {'0': 'Movies/Teen Wolf (1985)/Teen Wolf (1985).mkv'},
        }), encoding='utf-8')
        discover_store.insert_request(self.store, {
            'idempotency': 'teen-wolf-00000000001', 'kind': 'movie', 'title': 'Teen Wolf (1985)',
            'quality': '1080', 'fallback': True, 'state': 'queued', 'engine': 'nzb', 'engine_id': 'wolf',
            'episode_key': 'movie:90142', 'message': 'Queued with the NZB downloader.',
            'progress': 100, 'body': {},
        })

        class App:
            extensions = {}

        discover_acquire.sync_jobs(App(), self.store)
        with self.store.connect() as db:
            row = db.execute('SELECT state, message FROM discover_requests WHERE engine_id=?', ('wolf',)).fetchone()
            owned = db.execute('SELECT path FROM discover_owned WHERE key=?', ('movie:90142',)).fetchone()
        self.assertEqual(row['state'], 'complete')
        self.assertEqual(row['message'], 'Saved in the library.')
        self.assertEqual(owned['path'], str(folder / 'Teen Wolf (1985).mkv'))

    def test_saved_nzb_status_honors_explicit_pause_only_for_active_phases(self):
        path = Path(self.tmp.name) / 'paused-status.json'
        expected = {'queued': 'paused', 'download': 'paused', 'paused': 'paused',
                    'failed': 'failed', 'stopped': 'stopped', 'choose': 'choose'}
        for phase, state in expected.items():
            with self.subTest(phase=phase):
                original = json.dumps({'phase': phase, 'user_paused': True, 'done_parts': 2, 'total_parts': 10}).encode()
                path.write_bytes(original)
                status = discover_acquire.status_from_job_file(path)
                self.assertEqual(status['state'], state)
                self.assertEqual(status['phase'], phase)
                self.assertEqual(path.read_bytes(), original)
        for paused in (False, 'false', 1, None):
            with self.subTest(user_paused=paused):
                path.write_text(json.dumps({'phase': 'queued', 'user_paused': paused}))
                self.assertEqual(discover_acquire.status_from_job_file(path)['state'], 'queued')

    def finished_native(self, published, *, kind='episode', state='queued'):
        job_dir = Path(self.tmp.name) / 'nzb-engine' / 'jobs'
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / 'finished.json').write_text(json.dumps({
            'id': 'finished', 'phase': 'stopped', 'progress_stage': 'complete', 'error': '',
            'done_parts': 10, 'total_parts': 10, 'published': dict(enumerate(published)),
        }), encoding='utf-8')
        row, _ = discover_store.insert_request(self.store, {
            'idempotency': 'finished-native-00000001', 'kind': kind, 'title': 'Example episode',
            'quality': '1080', 'fallback': False, 'state': state, 'engine': 'nzb', 'engine_id': 'finished',
            'episode_key': 'show:77:1:2', 'message': 'Queued with the NZB downloader.',
            'body': {'episodeId': 88, 'qualitySource': 'show'},
        })
        return row

    def test_native_ownership_uses_video_after_nonempty_companions_and_soundtrack(self):
        folder = Path(self.tmp.name) / 'TV Shows' / 'Example'
        folder.mkdir(parents=True)
        for name in ('Episode.nfo', 'Episode.srt', 'Episode.jpg', 'Soundtrack.flac', 'Episode.mkv'):
            (folder / name).write_bytes(b'published content')
        before = self.finished_native([str(folder / name) for name in (
            'Episode.nfo', 'Episode.srt', 'Episode.jpg', 'Soundtrack.flac', 'Episode.mkv')])
        with patch('backend.discover_acquire._retry_candidate', side_effect=AssertionError('No retry')):
            discover_acquire.sync_jobs(type('App', (), {'extensions': {}})(), self.store)
        with self.store.connect() as db:
            after = dict(db.execute('SELECT * FROM discover_requests WHERE id=?', (before['id'],)).fetchone())
            owned = dict(db.execute('SELECT * FROM discover_owned WHERE key=?', (before['episode_key'],)).fetchone())
        self.assertEqual(after['state'], 'complete')
        self.assertEqual(owned['path'], str(folder / 'Episode.mkv'))
        self.assertEqual(owned['request_id'], before['id'])
        for key in ('id', 'engine', 'engine_id', 'stop_generation', 'body', 'quality', 'fallback', 'episode_key'):
            self.assertEqual(after[key], before[key], key)

    def test_playable_saved_paths_respect_request_kind_and_library_boundaries(self):
        root = Path(self.tmp.name)
        folder = root / 'library'
        folder.mkdir()
        self.store.set('storage_base', str(folder))
        for name in ('Episode.mkv', 'Soundtrack.flac', 'Episode.nfo', 'Episode.srt', 'sample.mkv', 'zero.mkv'):
            (folder / name).write_bytes(b'' if name == 'zero.mkv' else b'content')
        outside = root / 'outside.mkv'
        outside.write_bytes(b'outside')
        (folder / 'link.mkv').symlink_to(folder / 'Episode.mkv')
        (folder / 'linked').symlink_to(folder, target_is_directory=True)
        (folder / 'directory.mkv').mkdir()
        paths = ['Episode.nfo', 'Soundtrack.flac', 'Episode.mkv', 'Episode.srt', 'sample.mkv', 'zero.mkv',
                 'missing.mkv', str(outside), '../outside.mkv', 'link.mkv', 'linked/Episode.mkv', 'directory.mkv']
        video = str(folder / 'Episode.mkv')
        audio = str(folder / 'Soundtrack.flac')
        for kind in ('movie', 'episode', 'show', 'video'):
            with self.subTest(kind=kind):
                self.assertEqual(discover_acquire.saved_paths(self.store, paths, kind=kind), [video])
        self.assertEqual(discover_acquire.saved_paths(self.store, paths, kind='audio'), [audio])
        self.assertEqual(discover_acquire.saved_paths(self.store, paths), [video, audio])
        self.assertEqual(discover_acquire.saved_paths(self.store, paths, kind='unknown'), [])

    def test_native_companions_or_audio_cannot_complete_video_request(self):
        root = Path(self.tmp.name)
        for name in ('Episode.nfo', 'Soundtrack.flac', 'zero.mkv'):
            (root / name).write_bytes(b'' if name == 'zero.mkv' else b'content')
        before = self.finished_native(['Episode.nfo', 'Soundtrack.flac', 'zero.mkv', 'missing.mkv'], kind='movie')
        with patch('backend.discover_acquire._retry_candidate', side_effect=AssertionError('No retry')):
            discover_acquire.sync_jobs(type('App', (), {'extensions': {}})(), self.store)
        with self.store.connect() as db:
            after = db.execute('SELECT state FROM discover_requests WHERE id=?', (before['id'],)).fetchone()
            self.assertIsNone(db.execute('SELECT * FROM discover_owned WHERE key=?', (before['episode_key'],)).fetchone())
        self.assertNotEqual(after['state'], 'complete')

    def test_native_completed_file_does_not_override_clear_or_cancellation(self):
        from backend import media_holds
        (Path(self.tmp.name) / 'Episode.mkv').write_bytes(b'video')
        before = self.finished_native(['Episode.mkv'])
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            media_holds.hold(db, media_holds.request_keys(before), [])
        with patch('backend.discover_acquire.mark_owned', side_effect=AssertionError('Held ownership')):
            discover_acquire.sync_jobs(type('App', (), {'extensions': {}})(), self.store)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM discover_requests WHERE id=?', (before['id'],)).fetchone()[0], 'queued')
            db.execute("UPDATE discover_requests SET state='cancelled' WHERE id=?", (before['id'],))
        with patch('backend.discover_acquire.mark_owned', side_effect=AssertionError('Cancelled ownership')):
            discover_acquire.sync_jobs(type('App', (), {'extensions': {}})(), self.store)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM discover_requests WHERE id=?', (before['id'],)).fetchone()[0], 'cancelled')
            self.assertIsNone(db.execute('SELECT * FROM discover_owned WHERE key=?', (before['episode_key'],)).fetchone())

    def test_native_cancellation_during_checkpoint_read_cannot_create_ownership(self):
        (Path(self.tmp.name) / 'Episode.mkv').write_bytes(b'video')
        before = self.finished_native(['Episode.mkv'])
        original = discover_acquire.status_from_job_file

        def cancel_then_read(path):
            with self.store.connect() as db:
                db.execute("UPDATE discover_requests SET state='cancelled' WHERE id=?", (before['id'],))
            return original(path)

        with patch('backend.discover_acquire.status_from_job_file', side_effect=cancel_then_read), \
                patch('backend.discover_acquire.mark_owned', side_effect=AssertionError('Cancelled ownership')), \
                patch('backend.activity.record', side_effect=AssertionError('Cancelled completion')):
            discover_acquire.sync_jobs(type('App', (), {'extensions': {}})(), self.store)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM discover_requests WHERE id=?', (before['id'],)).fetchone()[0], 'cancelled')
            self.assertIsNone(db.execute('SELECT * FROM discover_owned WHERE key=?', (before['episode_key'],)).fetchone())

    def test_manual_and_automatic_requests_share_one_episode(self):
        first, created = discover_store.insert_request(self.store, {
            'idempotency': 'manual-token-00000001', 'kind': 'episode', 'title': 'Example', 'quality': '1080',
            'fallback': False, 'episode_key': 'show:77:2:1', 'state': 'queued', 'body': {},
        })
        second, again = discover_store.insert_request(self.store, {
            'idempotency': 'other-token-0000000002', 'kind': 'episode', 'title': 'Example', 'quality': '720',
            'fallback': True, 'episode_key': 'show:77:2:1', 'state': 'searching', 'body': {},
        })
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(first['id'], second['id'])


if __name__ == '__main__':
    unittest.main()
