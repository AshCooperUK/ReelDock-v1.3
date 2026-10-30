"""Search pages must be slices of one sorted window, never provider-arrival order."""
import json
import time
import unittest
from unittest.mock import patch

import test_view_media as vm
from backend import discovery_engine as engine
from backend.view_media import search_snapshot as snapshot


class SearchSnapshotTests(unittest.TestCase):
    setUp = vm.ViewMediaTests.setUp
    tearDown = vm.ViewMediaTests.tearDown

    def args(self, **values):
        return dict(dict(media='all', q='Window', sort='release', offset=0,
                         collection='', category='all', cursor=None, **{'global': True}), **values)

    def card(self, identity, year, kind='movie', **fields):
        return dict(id=identity, kind=kind, title=identity, year=str(year),
                    action=dict(type='local', id=identity), **fields)

    def fetch(self, args):
        # Exercise real SQLite caches and locking; make only the scheduler synchronous.
        with patch.object(engine, 'schedule', side_effect=lambda app, store, key, fn: fn()):
            result = engine.cached_browse(self.app, self.store, args)
        return engine.cached_browse(self.app, self.store, args) if result.get('loading') else result

    def drain(self, first, args):
        items = list(first['items'])
        while first['nextOffset'] is not None:
            first = self.fetch(dict(args, offset=first['nextOffset'], cursor=json.loads(first['nextCursor'])))
            items.extend(first['items'])
        return items

    def test_later_provider_pages_sorted_before_first_page_is_exposed(self):
        calls = []
        def browse(app, store, args):
            batch = args['offset']; calls.append(args)
            # Newer items deliberately arrive in later provider batches.
            return dict(items=[self.card(f'{batch}:{n}', 2000 + batch, 'movie' if n < 26 else 'show') for n in range(30)],
                        nextCursor=json.dumps({'youtube': (batch + 1) * 20}), incomplete=False)
        with patch.object(snapshot.global_search, 'browse', side_effect=browse):
            args = self.args()
            first = self.fetch(args)
            self.assertEqual([call['cursor'] for call in calls], [None, {'youtube': 20}, {'youtube': 40}])
            self.assertTrue(all(call['_partial'] is None for call in calls))
            self.assertTrue(all(item['year'] == '2002' for item in first['items']))
            rows = self.drain(first, args)
            self.assertEqual(len(calls), snapshot.BATCHES, 'scrolling must never make provider calls')
        self.assertEqual(len(rows), 90)
        self.assertEqual(len({r['id'] for r in rows}), 90)
        for kind in ('movie', 'show'):
            years = [r['year'] for r in rows if r['kind'] == kind]
            self.assertEqual(years, sorted(years, reverse=True))
        self.assertEqual([r['kind'] for r in rows], sorted([r['kind'] for r in rows]))
        self.assertIn('Fixed search window', first['freshness']['scope'])

    def test_every_selected_sort_and_unknowns_are_consistent_across_pages(self):
        from backend.view_media.ordering import ordered
        cards = [self.card(str(n), 2000 + n % 5, rating=n % 10, added=n) for n in range(55)]
        cards.append(self.card('unknown', '', rating='', added=None))
        with patch.object(snapshot.global_search, 'browse', return_value=dict(items=cards, nextCursor=None)) as provider:
            for order in ('title', 'title_desc', 'release', 'release_asc', 'year', 'rating', 'source'):
                args = self.args(sort=order)
                rows = self.drain(self.fetch(args), args)
                expected = ordered(cards, 'new' if order == 'source' else order)
                self.assertEqual([r['id'] for r in rows], [r['id'] for r in expected], order)
            provider.assert_called_once()

    def test_cursor_pins_snapshot_after_cache_refresh_and_store_reopen(self):
        from backend.storage import Store
        args = self.args()
        with patch.object(snapshot.global_search, 'browse', return_value=dict(items=[self.card(str(n), 2000) for n in range(50)], nextCursor=None)):
            first = self.fetch(args)
        key = engine.browse_cache_key(dict(args, sort='source'), self.store)
        with self.store.connect() as db:
            db.execute('UPDATE view_media_cache SET updated=0 WHERE key=?', (key,))
        with patch.object(snapshot.global_search, 'browse', return_value=dict(items=[self.card('new', 2026)], nextCursor=None)):
            new = self.fetch(args)
        self.assertNotEqual(first['searchSnapshot'], new['searchSnapshot'])
        self.store = Store(self.tmp.name)
        rows = self.drain(first, args)
        self.assertEqual(len(rows), 50)
        self.assertNotIn('new', [r['id'] for r in rows])
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.fetch(self.args(q='Different', offset=first['nextOffset'], cursor=json.loads(first['nextCursor'])))

    def test_failure_keeps_healthy_results_and_scanning_does_not_publish_partial_order(self):
        args = self.args()
        page = dict(items=[self.card('healthy', 2026)], nextCursor=None, incomplete=True,
                    warnings=['YouTube search could not finish. Retrying automatically.'])
        with patch.object(snapshot.global_search, 'browse', return_value=page):
            first = self.fetch(args)
        self.assertEqual(first['items'][0]['id'], 'healthy')
        self.assertIn('Search again to retry', first['warnings'][0])
        with patch.object(snapshot.global_search, 'browse', return_value=dict(page, scanning=True)):
            scanning = self.fetch(self.args(q='Scanning'))
        self.assertTrue(scanning['loading']); self.assertEqual(scanning['items'], [])

    def test_cursor_validation_auth_and_expiry(self):
        cursor = dict(snapshot='a' * 32, order='release')
        for value, offset, order in [(cursor, 0, 'release'), (cursor, 1, 'release'),
                                     (cursor, 24, 'title'), ({'snapshot': 1}, 24, 'release'),
                                     ([], 24, 'release')]:
            response = self.client.get('/api/view-media/search', base_url=vm.ORIGIN,
                                       query_string=dict(q='Window', sort=order, offset=offset, cursor=json.dumps(value)))
            self.assertEqual(response.status_code, 400)
        response = self.client.get('/api/view-media/search', base_url=vm.ORIGIN,
                                   query_string=dict(q='Window', offset=24, cursor=json.dumps(cursor)))
        self.assertEqual(response.status_code, 400); self.assertIn('expired', response.json['error'])
        self.assertEqual(self.app.test_client().get('/api/view-media/search?q=Window', base_url=vm.ORIGIN).status_code, 401)

    def test_grouped_search_supplies_the_sorted_window_without_scroll_cursors(self):
        cards=[self.card(str(n),2000+n%5,kind) for kind in ('movie','show','video') for n in range(30)]
        # Distinct identities per type retain every result through deduplication.
        cards=[dict(card,id=card['kind']+card['id']) for card in cards]
        with patch.object(snapshot.global_search,'browse',return_value=dict(items=cards,nextCursor=None)) as provider:
            result=self.fetch(self.args(grouped=True))
            self.assertEqual(len(result['items']),90)
            self.assertIsNone(result['nextOffset']);self.assertIsNone(result['nextCursor'])
            legacy=self.fetch(self.args())
            self.assertEqual(len(legacy['items']),24)
            self.assertEqual(result['searchSnapshot'],legacy['searchSnapshot'])
            provider.assert_called_once()
        for kind in ('movie','show','video'):
            years=[row['year'] for row in result['items'] if row['kind']==kind]
            self.assertEqual(years,sorted(years,reverse=True))

    def test_grouped_endpoint_validates_mode_and_has_no_continuation(self):
        with patch.object(snapshot.global_search,'browse',return_value=dict(items=[self.card(str(n),2026) for n in range(55)],nextCursor=None)),patch.object(engine,'schedule',side_effect=lambda app,store,key,fn:fn()):
            url='/api/view-media/search?q=Window&grouped=1'
            self.client.get(url,base_url=vm.ORIGIN)
            response=self.client.get(url,base_url=vm.ORIGIN)
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.json['items']),55)
        self.assertIsNone(response.json['nextOffset'])
        for suffix in ('&grouped=true','&grouped=1&offset=24'):
            self.assertEqual(self.client.get('/api/view-media/search?q=Window'+suffix,base_url=vm.ORIGIN).status_code,400)
