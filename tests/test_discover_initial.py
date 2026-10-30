import json
import time
import threading
import unittest
from unittest.mock import patch

from backend.discover_initial import snapshot
from backend.discover_sources import save_chart
import test_dashboard as dashboard

LOCAL = dashboard.LOCAL


class DiscoverInitialTests(unittest.TestCase):
    def setUp(self):
        self.fixture = dashboard.DashboardTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store = self.fixture.store
        items = [dict(id=str(i), kind='movie', title='Cached title %d' % i,
                      externalId=str(i), grab='private-download-url') for i in range(60)]
        save_chart(self.store, 'all-time', dict(title='Cached chart', items=items, secret='private-token'))
        with self.store.connect() as db:
            db.execute('INSERT INTO discover_feeds(name,body,fetched) VALUES (?,?,?)',
                       ('trending_movies', json.dumps(dict(items=items, scope='Cached catalogue')), time.time()))

    def test_preview_is_small_public_and_local_without_truncating_cache(self):
        with patch('socket.create_connection', side_effect=AssertionError('Preview must not contact providers')):
            preview = snapshot(self.store)
        self.assertEqual(len(preview['charts']['all-time']['items']), 30)
        self.assertEqual(len(preview['feeds']['trending_movies']['items']), 24)
        self.assertTrue(preview['charts']['all-time']['partial'])
        self.assertTrue(preview['feeds']['trending_movies']['preloaded'])
        self.assertNotIn('private-', json.dumps(preview))
        self.assertEqual(preview['preferences']['layout'], 'grid')
        with self.store.connect() as db:
            cached = json.loads(db.execute('SELECT body FROM discover_feeds').fetchone()[0])
        self.assertEqual(len(cached['items']), 60)

    def test_stale_or_corrupt_feed_is_honest(self):
        with self.store.connect() as db:
            db.execute('UPDATE discover_feeds SET fetched=?', (time.time()-1000,))
            db.execute('INSERT INTO discover_feeds VALUES (?,?,?)', ('new_movies', 'broken-json', time.time()))
        preview = snapshot(self.store)
        self.assertTrue(preview['feeds']['trending_movies']['stale'])
        self.assertIn('cached', preview['feeds']['trending_movies']['warning'])
        self.assertNotIn('new_movies', preview['feeds'])

    def test_dashboard_only_exposes_preview_to_signed_in_completed_account(self):
        self.assertNotIn('discover_initial', self.fixture.get().json)
        self.fixture.admin()
        self.assertNotIn('discover_initial', self.fixture.get().json)
        self.store.set('setup_complete', 'true')
        self.assertEqual(len(self.fixture.get().json['discover_initial']['charts']['all-time']['items']), 30)
        anonymous = self.fixture.app.test_client().get('/api/dashboard', base_url=LOCAL)
        self.assertNotIn('discover_initial', anonymous.json)

    def test_chart_preview_limit_preserves_full_browsing_and_access(self):
        self.fixture.admin()
        self.store.set('setup_complete', 'true')
        def chart(query):
            return self.fixture.client.get('/api/discover/youtube?name=all-time' + query, base_url=LOCAL)
        self.assertEqual(len(chart('&limit=30').json['items']), 30)
        self.assertTrue(chart('&limit=30').json['partial'])
        self.assertEqual(len(chart('').json['items']), 60)
        for value in ('0', '-1', '1001', 'bad'):
            self.assertEqual(chart('&limit='+value).status_code, 400)
        self.assertEqual(self.fixture.app.test_client().get('/api/discover/youtube?name=all-time&limit=30', base_url=LOCAL).status_code, 401)

    def test_view_media_preview_uses_existing_cache_without_provider_wait(self):
        from backend.discovery_engine import browse_cache_key, default_browse_args
        from backend.view_media.providers import initialise
        initialise(self.store)
        page=dict(items=[dict(id='movie:1',title='Cached movie',kind='movie',action=dict(type='discover',item=dict(id='movie:1',title='Cached movie',kind='movie')))],nextOffset=None)
        with self.store.connect() as db:
            db.execute('INSERT INTO view_media_cache VALUES (?,?,?)',
                       (browse_cache_key(default_browse_args(),self.store),json.dumps(page),time.time()-600))
        with patch('backend.view_media.adapters.REGISTRY') as providers:
            result=snapshot(self.store)['viewMedia']
        self.assertEqual(result['items'][0]['title'],'Cached movie')
        self.assertTrue(result['refreshing']);self.assertFalse(result['loading'])
        providers.assert_not_called()

    def test_next_page_is_prefetched_once_off_request_thread(self):
        from backend import discovery_engine as engine
        from backend.view_media.providers import initialise
        initialise(self.store)
        args=engine.default_browse_args()
        page=dict(items=[dict(id='first')],nextOffset=1,nextCursor='{"nzb":100}')
        with self.store.connect() as db:
            db.execute('INSERT INTO view_media_cache VALUES (?,?,?)',
                       (engine.browse_cache_key(args,self.store),json.dumps(page),time.time()))
        started=threading.Event();release=threading.Event()
        def browse(app,store,following):
            self.assertEqual(following['cursor'],dict(nzb=100))
            self.assertEqual(following['offset'],1)
            started.set();release.wait(3)
            return dict(items=[dict(id='second')],nextOffset=2,nextCursor='{"nzb":200}')
        with patch('backend.view_media.adapters.REGISTRY',{'discover':{'browse':browse}}):
            begin=time.monotonic()
            result=engine.cached_browse(self.fixture.app,self.store,args)
            self.assertLess(time.monotonic()-begin,.2)
            self.assertEqual(result['items'][0]['id'],'first')
            self.assertTrue(started.wait(1))
            release.set()
            deadline=time.monotonic()+3
            while any(key[0]==str(self.store.directory) for key in engine._PENDING) and time.monotonic()<deadline:time.sleep(.01)
        with self.store.connect() as db:
            rows=db.execute("SELECT body FROM view_media_cache WHERE key LIKE 'engine:nzb-browse:%'").fetchall()
        self.assertEqual(len(rows),2,'Prefetch must not recursively fetch the entire catalogue')
