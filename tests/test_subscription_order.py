"""Channel ordering applies to the full saved catalogue before pagination."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import test_guide as fixture


class SubscriptionOrderTests(unittest.TestCase):
    setUp = fixture.GuideTests.setUp
    tearDown = fixture.GuideTests.tearDown
    google = fixture.GuideTests.google

    def seed(self):
        self.ids = ['UC' + f'{n:022d}' for n in range(40)]
        with self.store.connect() as db:
            for n, identity in enumerate(self.ids):
                data = dict(id=identity, name=f'Channel {n:02d}', subscribers=str(n * 100))
                db.execute('INSERT INTO channels VALUES(?,?,1)', (identity, json.dumps(data)))
                published = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=n)).isoformat()
                video = dict(id=f'vid{n:08d}', channel=identity, state='published', date=published[:10])
                db.execute('INSERT INTO videos VALUES(?,?,?,?,?,0)',
                           (video['id'], identity, published[:10], published, json.dumps(video)))
                if n % 2 == 0:
                    db.execute('INSERT INTO channel_favourites VALUES(?)', (identity,))
            # A later scheduled programme must not become "latest upload".
            db.execute('INSERT INTO videos VALUES(?,?,?,?,?,0)',
                       ('scheduled01', self.ids[0], '2099-01-01', '2099-01-01T00:00:00Z',
                        json.dumps(dict(state='scheduled'))))
        self.app.config['YOUTUBE_FEED'] = Mock(side_effect=AssertionError('No provider read'))

    def page(self, query=''):
        return self.client.get('/api/subscriptions?' + query, base_url=fixture.ORIGIN)

    def test_latest_upload_fetches_the_global_first_page_without_scrolling(self):
        self.seed()
        default = self.page().json
        latest = self.page('sort=latest').json
        self.assertNotIn(self.ids[39], [item['id'] for item in default['items']])
        self.assertEqual([item['id'] for item in latest['items']], self.ids[:24:-1])
        self.assertTrue(latest['more'])
        self.app.config['YOUTUBE_FEED'].assert_not_called()

    def test_sort_pages_keep_one_global_order_without_duplicates(self):
        self.seed()
        pages = [self.page(f'sort=latest&offset={offset}').json for offset in (0, 15, 30)]
        result = [item['id'] for page in pages for item in page['items']]
        self.assertEqual(result, list(reversed(self.ids)))
        self.assertEqual(len(set(result)), 40)
        self.assertFalse(pages[-1]['more'])

    def test_favourites_filter_applies_before_global_order_and_page_limit(self):
        self.seed()
        pages = [self.page(f'sort=latest&favourites=1&offset={offset}').json for offset in (0, 15)]
        result = [item['id'] for page in pages for item in page['items']]
        self.assertEqual(result, list(reversed(self.ids[::2])))
        self.assertTrue(pages[0]['more'])
        self.assertFalse(pages[1]['more'])

    def test_all_supported_orders_and_search_keep_the_same_scope(self):
        self.seed()
        for sort, expected in [('az', self.ids[:15]), ('za', self.ids[:24:-1]),
                               ('subscribers', self.ids[:24:-1])]:
            with self.subTest(sort=sort):
                self.assertEqual([item['id'] for item in self.page('sort=' + sort).json['items']], expected)
        scoped = self.page('sort=latest&q=Channel%200').json
        self.assertEqual([item['id'] for item in scoped['items']], self.ids[9::-1])
        self.assertEqual(self.page('sort=latest&channel=' + self.ids[0]).json['items'][0]['id'], self.ids[0])

    def test_unknown_sort_or_filter_does_not_modify_saved_state(self):
        self.seed()
        with self.store.connect() as db:
            before = list(db.execute('SELECT id,data,active FROM channels ORDER BY id'))
        for query in ('sort=unknown', 'sort=latest%3BDROP%20TABLE%20channels', 'favourites=true'):
            self.assertEqual(self.page(query).status_code, 400)
        with self.store.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT id,data,active FROM channels ORDER BY id')],
                             [tuple(row) for row in before])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM downloads').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM history_jobs').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
