"""Independent race regressions for the viewed-channel and ordinary feed lanes."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app import create_app
from backend.catalogue import Importer
from backend import channel_refresh as refresh


A = 'UC' + 'a' * 22
B = 'UC' + 'b' * 22
VIDEO = 'feedVideo01'


def feed_xml(channel=A, video=VIDEO):
    return (f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">'
            f'<entry><yt:videoId>{video}</yt:videoId><yt:channelId>{channel}</yt:channelId>'
            '<title>RSS title</title><published>2026-10-24T10:00:00Z</published></entry></feed>').encode()


class ChannelRefreshConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.threads, self.releases = [], []
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(dict(TESTING=True, DATA_DIR=self.temp.name,
            MEDIA_DIR=str(Path(self.temp.name)/'media'), YOUTUBE_FEED=feed_xml))
        self.store = self.app.extensions['store']
        self.store.set('setup_complete', '1')
        self.store.set('youtube_sync_paused', 'false')
        self.importer = Importer(self.app)
        self.other = Importer(self.app)
        for channel in (A, B):
            self.seed_channel(channel)

    def tearDown(self):
        for event in self.releases: event.set()
        for thread in self.threads: thread.join(5)
        self.temp.cleanup()

    def seed_channel(self, channel):
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO channels VALUES(?,?,1)',
                (channel, json.dumps(dict(id=channel, name='Original channel', playlist='original-playlist',
                    description='Original description', coverage={'days': 30}, last_checked=0))))

    def row(self, table, identity, key='id'):
        with self.store.connect() as db:
            result = db.execute('SELECT * FROM '+table+' WHERE '+key+'=?', (identity,)).fetchone()
        return dict(result) if result else None

    def threaded(self, function):
        result, errors = [], []
        def run():
            try: result.append(function())
            except BaseException as error: errors.append(error)
        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        def finish():
            thread.join(5)
            self.assertFalse(thread.is_alive(), 'the feed lane must finish after release')
            if errors: raise errors[0]
            return result[0] if result else None
        return finish

    def blocked(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        self.addCleanup(release.set)
        def wait():
            entered.set()
            if not release.wait(5): raise AssertionError('The test did not release the blocked feed.')
        return entered, release, wait

    def competing_lanes(self, first_priority):
        entered, release, wait = self.blocked()
        calls = []
        def feed(channel):
            calls.append(channel)
            wait()
            return feed_xml(channel)
        self.app.config['YOUTUBE_FEED'] = feed
        finish = self.threaded(lambda: refresh.refresh(self.importer, A, priority=first_priority))
        try:
            self.assertTrue(entered.wait(3))
            first = self.row('channel_refresh_requests', A, 'channel')
            self.assertTrue(first['claim'])
            refresh.request_recent(self.store, A)
            second = refresh.refresh(self.other, A, priority=not first_priority)
            self.assertIsNone(second, 'bulk and viewed checks share the same durable per-channel claim')
            claimed = self.row('channel_refresh_requests', A, 'channel')
            self.assertEqual(claimed['claim'], first['claim'])
            self.assertEqual(refresh.status(self.store, A)['state'], 'running')
        finally:
            release.set()
        self.assertTrue(finish()['success'])
        self.assertEqual(calls, [A], 'competing importers issue only one provider feed read')
        self.assertEqual(refresh.status(self.store, A)['state'], 'fresh')
        self.assertIsNone(refresh.refresh(self.other, A, priority=True), 'a completed viewed check is reused immediately')

    def test_bulk_claim_coalesces_simultaneous_viewed_request(self):
        self.competing_lanes(False)

    def test_viewed_claim_coalesces_simultaneous_bulk_request(self):
        self.competing_lanes(True)

    def test_slow_feed_commit_preserves_concurrent_channel_identity_and_settings(self):
        entered, release, wait = self.blocked()
        self.app.config['YOUTUBE_FEED'] = lambda channel: (wait(), feed_xml(channel))[1]
        finish = self.threaded(lambda: refresh.refresh(self.importer, A, priority=True))
        try:
            self.assertTrue(entered.wait(3))
            updated = dict(id=A, name='Updated authoritative name', avatar='/new-avatar', banner='/new-banner',
                description='A concurrent edit must survive', playlist='new-playlist', coverage={'days': 365},
                indexed_videos=780, local_custom={'retained': True}, last_checked=0, uploads_error='Old failure')
            with self.store.connect() as db:
                db.execute('UPDATE channels SET data=? WHERE id=?', (json.dumps(updated), A))
        finally:
            release.set()
        self.assertTrue(finish()['success'])
        saved = json.loads(self.row('channels', A)['data'])
        for key, value in updated.items():
            if key not in ('last_checked', 'uploads_error'): self.assertEqual(saved[key], value, key)
        self.assertGreater(saved['last_checked'], 0)
        self.assertNotIn('uploads_error', saved)

    def test_rich_metadata_saved_after_placeholder_lookup_wins_the_write_race(self):
        entered, release, wait = self.blocked()
        ordinary_save = self.importer.save_videos
        def stale_placeholder(items):
            wait()  # placeholder has already checked that this ID was unknown.
            return ordinary_save(items)
        self.importer.save_videos = stale_placeholder
        finish = self.threaded(lambda: refresh.refresh(self.importer, A, priority=True))
        try:
            self.assertTrue(entered.wait(3))
            self.other.save_videos([dict(id=VIDEO, snippet=dict(channelId=A, title='Rich authoritative title',
                description='Full authoritative description', publishedAt='2026-10-24T10:00:00Z'),
                contentDetails={'duration': 'PT1H2M3S'}, statistics={'viewCount': '12345'})])
            rich = self.row('videos', VIDEO)
        finally:
            release.set()
        self.assertTrue(finish()['success'])
        self.assertEqual(self.row('videos', VIDEO), rich, 'RSS cannot replace any richer video columns or JSON')
        data = json.loads(rich['data'])
        self.assertEqual(data['duration'], '1:02:03')
        self.assertEqual(data['views'], '12345')
        self.assertFalse(data['metadata_pending'])

    def test_channel_removed_between_publish_gate_and_placeholder_write_cannot_resurrect_video(self):
        entered, release, wait = self.blocked()
        ordinary_save = self.importer.save_videos
        def stale_placeholder(items):
            wait()
            return ordinary_save(items)
        self.importer.save_videos = stale_placeholder
        finish = self.threaded(lambda: refresh.refresh(self.importer, A, priority=True))
        try:
            self.assertTrue(entered.wait(3))
            with self.store.connect() as db:
                db.execute('DELETE FROM channels WHERE id=?', (A,))
        finally:
            release.set()
        self.assertIsNone(finish())
        self.assertIsNone(self.row('channels', A))
        self.assertIsNone(self.row('videos', VIDEO))
        self.assertIsNone(self.row('channel_refresh_requests', A, 'channel'))
        self.assertIsNone(self.row('youtube_push_events', VIDEO))

    def test_pause_between_publish_gate_and_placeholder_write_preserves_saved_state(self):
        entered, release, wait = self.blocked()
        ordinary_save = self.importer.save_videos
        self.importer.save_videos = lambda items: (wait(), ordinary_save(items))[1]
        finish = self.threaded(lambda: refresh.refresh(self.importer, A, priority=True))
        try:
            self.assertTrue(entered.wait(3))
            self.store.set('youtube_sync_paused', 'true')
        finally:
            release.set()
        self.assertIsNone(finish())
        self.assertIsNone(self.row('videos', VIDEO))
        self.assertIsNone(self.row('youtube_push_events', VIDEO))
        self.assertEqual(json.loads(self.row('channels', A)['data'])['last_checked'], 0)
        self.assertEqual(self.row('channel_refresh_requests', A, 'channel')['lease_until'], 0)

    def test_repeated_views_preserve_failure_backoff_for_bulk_and_priority_lanes(self):
        with patch('backend.channel_refresh.time.time', return_value=10000):
            refresh.request_recent(self.store, A)
            with patch('backend.youtube_fallback.feed', side_effect=ValueError('Private provider failure')) as feed:
                self.assertFalse(refresh.refresh(self.importer, A, priority=True)['success'])
                self.assertEqual(feed.call_count, 1)
        retry = self.row('channel_refresh_requests', A, 'channel')['retry_at']
        self.assertEqual(retry, 10000 + refresh.RETRY_SECONDS)
        with patch('backend.channel_refresh.time.time', return_value=10001):
            for _ in range(3): self.assertEqual(refresh.request_recent(self.store, A)['state'], 'retry')
            with patch('backend.youtube_fallback.feed', side_effect=AssertionError('Backoff must not be bypassed')) as feed:
                self.assertIsNone(refresh.refresh(self.other, A, priority=True))
                self.assertIsNone(refresh.refresh(self.other, A, since=9990))
                self.assertIsNone(refresh.tick(self.other))
                feed.assert_not_called()
        self.assertEqual(self.row('channel_refresh_requests', A, 'channel')['retry_at'], retry)
        self.assertNotIn('Private provider failure', self.row('channels', A)['data'])

    def test_viewed_lane_keeps_bulk_fifo_history_policies_and_acquisition_hold(self):
        with self.store.connect() as db:
            for channel in (A, B):
                db.execute('INSERT INTO youtube_channel_requests(url,state,message,updated) VALUES(?,?,?,?)',
                    ('https://www.youtube.com/channel/'+channel, 'queued', 'Waiting', 1))
                db.execute('INSERT INTO youtube_history_requests(channel) VALUES(?)', (channel,))
        self.store.set('acquisition_hold', '1')
        tables = ('youtube_channel_requests', 'youtube_history_requests', 'history_jobs', 'download_rules', 'downloads', 'discover_requests')
        def snapshot():
            with self.store.connect() as db: return {table: [tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY 1')] for table in tables}
        before = snapshot()
        calls = []
        self.app.config['YOUTUBE_FEED'] = lambda channel: (calls.append(channel), feed_xml(channel))[1]
        with patch('backend.channel_refresh.time.time', return_value=10000): refresh.request_recent(self.store, A)
        with patch('backend.channel_refresh.time.time', return_value=10001): refresh.request_recent(self.store, B)
        with patch('backend.channel_refresh.time.time', return_value=10002):
            self.assertTrue(refresh.tick(self.importer)['success'])
            self.assertTrue(refresh.tick(self.importer)['success'])
        self.assertEqual(calls, [B, A], 'only the separate viewed demand lane prioritises the latest selection')
        self.assertEqual(snapshot(), before, 'viewing metadata must not reorder import FIFO, seed deep history or change download policies')
        self.assertEqual(self.store.get('acquisition_hold'), '1')


if __name__ == '__main__':
    unittest.main()
