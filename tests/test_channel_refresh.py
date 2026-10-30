"""Viewed channels use bounded RSS work without waiting for the bulk importer."""
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch

import test_youtube_optional as fixture
from backend import channel_refresh as refresh, youtube_fallback, youtube_push
from backend.catalogue import Importer

CHANNEL, VID = fixture.CHANNEL, fixture.VID
OTHER = 'UC' + 'b' * 22
ORIGIN = fixture.ORIGIN


class ChannelRefreshTests(unittest.TestCase):
    setUp = fixture.OptionalYouTubeTests.setUp
    tearDown = fixture.OptionalYouTubeTests.tearDown
    google = fixture.OptionalYouTubeTests.google
    extract = fixture.OptionalYouTubeTests.extract
    feed = fixture.OptionalYouTubeTests.feed
    xml = fixture.OptionalYouTubeTests.xml

    def post(self, channel=CHANNEL, csrf='csrf-fixture', body=None, origin=ORIGIN):
        return self.client.post('/api/guide/channel/' + channel, base_url=ORIGIN,
                                json=body or {'action': 'prioritise_recent'},
                                headers={'Origin': origin, 'X-CSRF-Token': csrf})

    def protected(self):
        tables = ['downloads', 'channel_tasks', 'download_rules', 'youtube_history_requests', 'history_jobs', 'guide_week_jobs', 'channel_favourites', 'watchlist_items']
        with self.store.connect() as db:
            known = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)] for table in tables if table in known}

    def channel(self, channel=CHANNEL):
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM channels WHERE id=?', (channel,)).fetchone()
        return json.loads(row[0]) if row else None

    def test_post_only_records_coalesced_demand_get_stays_read_only(self):
        protected = self.protected()
        with patch.object(youtube_fallback, 'feed', side_effect=AssertionError('No provider in the web request')):
            first = self.post()
            second = self.post()
            self.assertEqual(first.status_code, 202)
            self.assertEqual(first.json['refresh']['state'], 'queued')
            self.assertEqual(second.status_code, 202)
            with self.store.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM channel_refresh_requests').fetchone()[0], 1)
                before = '\n'.join(db.iterdump())
            response = self.client.get('/api/guide/channel/' + CHANNEL, base_url=ORIGIN)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['items'], [])
            self.assertEqual(response.json['total'], 0)
            self.assertEqual(response.json['refresh']['refreshAfterMs'], 2000)
            with self.store.connect() as db:
                self.assertEqual('\n'.join(db.iterdump()), before)
        self.assertEqual(self.protected(), protected)

    def test_cached_upload_total_tracks_older_imports_and_active_channel_scope_without_writes(self):
        def insert(db, ident, owner, published):
            item = {'id': ident, 'channel': owner, 'title': ident, 'date': published[:10], 'state': 'published'}
            db.execute('INSERT INTO videos VALUES(?,?,?,?,?,?)', (ident, owner, published[:10], published, json.dumps(item), time.time()))

        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES(?,?,1)', (OTHER, json.dumps({'id': OTHER, 'name': 'Other'})))
            for index in range(35):
                insert(db, f'saved{index:06}', CHANNEL, f'2026-10-24T12:00:{index:02}Z')
            for index in range(7):
                insert(db, f'other{index:06}', OTHER, f'2026-10-24T12:00:{index:02}Z')
            before = '\n'.join(db.iterdump())
            plan = db.execute('EXPLAIN QUERY PLAN SELECT COUNT(*) FROM videos v JOIN channels c ON c.id=v.channel WHERE c.active=1 AND v.channel=?', (CHANNEL,)).fetchall()
            self.assertIn('videos_channel_date', ' '.join(str(tuple(row)) for row in plan))
        with patch.object(youtube_fallback, 'feed', side_effect=AssertionError('Saved count must not schedule a provider read')):
            head = self.client.get('/api/guide/channel/' + CHANNEL, base_url=ORIGIN).json
            end = self.client.get('/api/guide/channel/' + CHANNEL + '?offset=30', base_url=ORIGIN).json
            self.assertEqual((head['total'], len(head['items']), head['more']), (35, 15, True))
            self.assertEqual((end['total'], len(end['items']), end['more']), (35, 5, False))
            with self.store.connect() as db:
                self.assertEqual('\n'.join(db.iterdump()), before)
                for index in range(15):
                    insert(db, f'older{index:06}', CHANNEL, f'2026-10-23T12:00:{index:02}Z')
                after_import = '\n'.join(db.iterdump())
            growing = self.client.get('/api/guide/channel/' + CHANNEL, base_url=ORIGIN).json
            tail = self.client.get('/api/guide/channel/' + CHANNEL + '?offset=35', base_url=ORIGIN).json
            self.assertEqual(growing['items'], head['items'], 'older imports leave the visible first page unchanged')
            self.assertEqual(growing['total'], 50)
            self.assertEqual((tail['total'], len(tail['items']), tail['more']), (50, 15, False))
            self.assertTrue(all(item['id'].startswith('older') for item in tail['items']))
            with self.store.connect() as db:
                self.assertEqual('\n'.join(db.iterdump()), after_import)
                db.execute('UPDATE channels SET active=0 WHERE id=?', (CHANNEL,))
            inactive = self.client.get('/api/guide/channel/' + CHANNEL, base_url=ORIGIN).json
            missing = self.client.get('/api/guide/channel/missing', base_url=ORIGIN).json
            self.assertEqual((inactive['total'], inactive['items'], inactive['more']), (0, [], False))
            self.assertEqual((missing['total'], missing['items'], missing['more']), (0, [], False))
            self.assertEqual(self.client.get('/api/guide/channel/' + OTHER, base_url=ORIGIN).json['total'], 7)

    def test_uploads_return_effective_minimum_without_filtering_or_mutating_saved_pages(self):
        videos = []
        for index, (kind, duration) in enumerate([('unknown', '1:24'), ('unknown', '1:12'), ('unknown', '1:31'),
                                                ('shorts', '0:40'), ('videos', '2:00'), ('live', '1:00'), ('unknown', '')]):
            videos.append(dict(id=f'saved{index:06}', channel=CHANNEL, title=f'Saved upload {index}',
                               date='2026-10-24', kind=kind, duration=duration, state='published'))
        videos += [dict(id=f'saved{index:06}', channel=CHANNEL, title=f'Older upload {index}',
                        date='2026-10-23', kind='unknown', duration='12:34', state='published') for index in range(7,18)]
        with self.store.connect() as db:
            for index, video in enumerate(videos):
                db.execute('INSERT INTO videos VALUES(?,?,?,?,?,?)',
                           (video['id'], CHANNEL, video['date'], f"{video['date']}T12:00:{index:02}Z", json.dumps(video), time.time()))
            db.execute("INSERT INTO channel_upload_pages(channel,offset,state,ids,has_more) VALUES(?,0,'complete',?,1)",
                       (CHANNEL,json.dumps([video['id'] for video in videos[:7]])))
        def read(expected):
            with self.store.connect() as db:before='\n'.join(db.iterdump())
            with patch.object(youtube_fallback,'feed',side_effect=AssertionError('GET must not request providers')), \
                 patch.object(youtube_fallback,'extract',side_effect=AssertionError('GET must not classify or refresh videos')):
                response=self.client.get('/api/guide/channel/'+CHANNEL,base_url=ORIGIN)
            self.assertEqual(response.status_code,200)
            body=response.json
            self.assertEqual(body['minimumVideoMinutes'],expected)
            self.assertIs(type(body['minimumVideoMinutes']),int)
            self.assertEqual((body['total'],len(body['items']),body['more']),(18,15,True))
            self.assertEqual({video['id'] for video in body['items']}, {video['id'] for video in sorted(videos,key=lambda value:(value['date'],value['id']),reverse=True)[:15]})
            self.assertEqual(body['browse']['page']['items'],videos[:7])
            with self.store.connect() as db:self.assertEqual('\n'.join(db.iterdump()),before)
            return body
        default=read(5)
        self.store.set('download_minimum_video_minutes','7')
        self.assertEqual(read(7)['items'],default['items'])
        with self.store.connect() as db:db.execute('UPDATE download_rules SET minimum_video_minutes=0 WHERE channel=?',(CHANNEL,))
        read(0)
        self.store.set('download_minimum_video_minutes','9')
        read(0)
        with self.store.connect() as db:db.execute('UPDATE download_rules SET minimum_video_minutes=10 WHERE channel=?',(CHANNEL,))
        read(10)
        with self.store.connect() as db:db.execute('UPDATE download_rules SET minimum_video_minutes=121 WHERE channel=?',(CHANNEL,))
        read(9)
        with self.store.connect() as db:db.execute('UPDATE download_rules SET minimum_video_minutes=-1 WHERE channel=?',(CHANNEL,))
        read(9)
        missing=self.client.get('/api/guide/channel/not-a-channel',base_url=ORIGIN)
        self.assertEqual(missing.status_code,200)
        self.assertEqual((missing.json['minimumVideoMinutes'],missing.json['items'],missing.json['total']),(9,[],0))
        self.assertEqual(self.app.test_client().get('/api/guide/channel/'+CHANNEL,base_url=ORIGIN).status_code,401)

    def test_post_requires_active_channel_origin_csrf_and_exact_action(self):
        self.assertEqual(self.post(csrf='wrong').status_code, 403)
        self.assertEqual(self.post(origin='https://wrong.example').status_code, 403)
        self.assertEqual(self.post(channel='not-a-channel').status_code, 400)
        self.assertEqual(self.post(channel=OTHER).status_code, 404)
        self.assertEqual(self.post(body={'action': 'deep_scan'}).status_code, 400)
        self.assertEqual(self.post(body={'action': 'prioritise_recent', 'download': True}).status_code, 400)
        with self.store.connect() as db:
            db.execute('UPDATE channels SET active=0 WHERE id=?', (CHANNEL,))
        self.assertEqual(self.post().status_code, 404)
        self.assertEqual(self.app.test_client().post('/api/guide/channel/' + CHANNEL, base_url=ORIGIN, json={'action': 'prioritise_recent'}, headers={'Origin': ORIGIN}).status_code, 401)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM channel_refresh_requests').fetchone()[0], 0)

    def test_rss_lane_publishes_basic_information_without_google_cookies_or_download_work(self):
        before = self.protected()
        self.importer.request = Mock(side_effect=AssertionError('No Google request'))
        refresh.request_recent(self.store, CHANNEL)
        with patch.object(youtube_fallback, 'extract', side_effect=AssertionError('No yt-dlp/cookie lookup')):
            result = refresh.tick(self.importer)
        self.assertTrue(result['success'])
        with self.store.connect() as db:
            video = json.loads(db.execute('SELECT data FROM videos WHERE id=?', (VID,)).fetchone()[0])
            self.assertEqual(db.execute('SELECT count(*) FROM youtube_push_events WHERE channel=?', (CHANNEL,)).fetchone()[0], 1)
        self.assertEqual(video['title'], 'New upload')
        self.assertTrue(video['metadata_pending'])
        self.assertTrue(video['thumbnail'].startswith('/artwork/'))
        self.assertEqual(refresh.status(self.store, CHANNEL)['state'], 'fresh')
        self.assertEqual(self.protected(), before)
        self.importer.request.assert_not_called()

    def test_fresh_cache_returns_immediately_and_repeat_view_does_not_rescan(self):
        refresh.request_recent(self.store, CHANNEL)
        refresh.tick(self.importer)
        with patch.object(youtube_fallback, 'feed', side_effect=AssertionError('Fresh cache must win')):
            response=self.post()
            self.assertEqual(response.status_code, 202,'missing channel facts can queue separately from the fresh RSS cache')
            self.assertEqual(response.json['metadata']['state'],'queued')
            self.assertEqual(self.post().json['refresh']['state'], 'fresh')
            self.assertIsNone(refresh.tick(self.importer))
            self.assertIsNone(refresh.refresh(self.importer, CHANNEL, priority=True))

    def test_empty_success_does_not_display_the_other_channels_bulk_import_notice(self):
        self.store.set('sync_running', '1')
        refresh.request_recent(self.store, CHANNEL)
        with patch.object(youtube_fallback, 'feed', return_value=[]):
            self.assertTrue(refresh.tick(self.importer)['success'])
        result = self.client.get('/api/guide/channel/' + CHANNEL, base_url=ORIGIN).json
        self.assertEqual(result['refresh']['state'], 'fresh')
        self.assertIn('No recent public uploads', result['notice'])
        self.assertNotIn('Importing', result['notice'])

    def test_failed_feed_has_fixed_error_and_clicks_cannot_clear_retry_cooldown(self):
        refresh.request_recent(self.store, CHANNEL)
        with patch.object(youtube_fallback, 'feed', side_effect=ValueError('PRIVATE_COOKIE https://private.example/token')) as feed:
            self.assertFalse(refresh.tick(self.importer)['success'])
            with self.store.connect() as db:
                deadline = db.execute('SELECT retry_at FROM channel_refresh_requests').fetchone()[0]
            for _ in range(3):
                response = self.post()
                self.assertEqual(response.json['refresh']['state'], 'retry')
                self.assertIsNone(refresh.tick(self.importer))
                self.assertNotIn('PRIVATE', response.text)
            self.assertEqual(feed.call_count, 1)
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM channel_refresh_requests').fetchone()
            self.assertEqual(row['retry_at'], deadline)
            self.assertGreater(deadline, time.time() + 50)
            self.assertEqual(row['claim'], '')
        self.assertNotIn('PRIVATE', json.dumps(self.channel()))

    def test_pause_reset_expiry_and_stop_do_not_start_provider_work(self):
        refresh.request_recent(self.store, CHANNEL)
        with patch.object(youtube_fallback, 'feed', side_effect=AssertionError('No provider work')):
            self.store.set('youtube_sync_paused', 'true')
            self.assertEqual(self.post().json['refresh']['state'], 'paused')
            self.assertIsNone(refresh.tick(self.importer))
            self.store.set('youtube_sync_paused', 'false')
            self.store.set('reset_in_progress', '1')
            self.assertIsNone(refresh.tick(self.importer))
            self.store.set('reset_in_progress', '0')
            with self.store.connect() as db:
                db.execute('UPDATE channel_refresh_requests SET expires=0')
            self.assertIsNone(refresh.tick(self.importer))
            stopped = threading.Event()
            stopped.set()
            self.assertIsNone(refresh.tick(self.importer, stopped))

    def test_newest_view_is_selected_first_and_expired_requests_are_ignored(self):
        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES(?,?,1)', (OTHER, json.dumps({'id': OTHER, 'name': 'Other'})))
        refresh.request_recent(self.store, CHANNEL)
        refresh.request_recent(self.store, OTHER)
        with patch.object(refresh, 'refresh', return_value={'success': True}) as run:
            refresh.tick(self.importer)
            self.assertEqual(run.call_args.args[1], OTHER)
            with self.store.connect() as db:
                db.execute('UPDATE channel_refresh_requests SET expires=0 WHERE channel=?', (OTHER,))
            refresh.tick(self.importer)
            self.assertEqual(run.call_args.args[1], CHANNEL)

    def test_recovery_and_removal_do_not_resurrect_expired_work(self):
        refresh.request_recent(self.store, CHANNEL)
        with self.store.connect() as db:
            db.execute("UPDATE channel_refresh_requests SET state='running',claim='stale',lease_until=?", (time.time() + 999,))
        refresh.recover(self.store)
        self.assertEqual(refresh.status(self.store, CHANNEL)['state'], 'queued')
        with self.store.connect() as db:
            db.execute('UPDATE channel_refresh_requests SET expires=0')
        refresh.recover(self.store)
        self.assertEqual(refresh.status(self.store, CHANNEL)['state'], 'idle')
        with self.store.connect() as db:
            db.execute('DELETE FROM channels WHERE id=?', (CHANNEL,))
            self.assertEqual(db.execute('SELECT count(*) FROM channel_refresh_requests').fetchone()[0], 0)

    def test_priority_metadata_keeps_fifo_slots_and_does_not_reset_event_backoff(self):
        now = time.time()
        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES(?,?,1)', (OTHER, json.dumps({'id': OTHER, 'name': 'Bulk'})))
            for index, (ident, owner, retry) in enumerate([('oldvideo001', OTHER, 0), ('oldvideo002', OTHER, 0), ('oldvideo003', OTHER, 0), (VID, CHANNEL, 0), ('backoff0001', CHANNEL, now + 100)]):
                db.execute('INSERT INTO youtube_push_events(id,channel,digest,received,retry_at,attempts) VALUES(?,?,?,?,?,?)', (ident, owner, ident, now-100+index, retry, 4 if retry else 0))
        refresh.request_recent(self.store, CHANNEL)
        with patch.object(self.importer, 'api', return_value={'items': []}) as api:
            youtube_push.process(self.importer)
        ids = api.call_args.kwargs['id'].split(',')
        self.assertEqual(ids, [VID, 'oldvideo001', 'oldvideo002'])
        with self.store.connect() as db:
            row = db.execute("SELECT retry_at,attempts FROM youtube_push_events WHERE id='backoff0001'").fetchone()
        self.assertEqual(row['retry_at'], now + 100)
        self.assertEqual(row['attempts'], 4)

    def test_dedicated_tick_works_while_the_bulk_import_is_blocked(self):
        from backend.worker import run_tick
        started, release = threading.Event(), threading.Event()
        def blocked():
            started.set()
            release.wait(3)
        self.store.set('next_sync', '0')
        self.store.set('next_history', time.time()+3600)
        with patch.object(self.importer, 'run', side_effect=blocked), patch.object(youtube_fallback, 'add_batch'), patch.object(youtube_fallback, 'backfill_channel_art'):
            thread = threading.Thread(target=run_tick, args=(self.importer,))
            thread.start()
            try:
                self.assertTrue(started.wait(1))
                refresh.request_recent(self.store, CHANNEL)
                self.assertTrue(refresh.tick(self.importer)['success'])
                self.assertFalse(release.is_set())
            finally:
                release.set()
                thread.join(3)

    def test_bulk_run_does_not_refetch_a_just_viewed_channel_or_clobber_channel_facts(self):
        refresh.request_recent(self.store, CHANNEL)
        refresh.tick(self.importer)
        with self.store.connect() as db:
            db.execute("UPDATE channels SET data=json_set(data,'$.banner','/artwork/current','$.name','Owner name') WHERE id=?", (CHANNEL,))
        with patch.object(youtube_fallback, 'feed', side_effect=AssertionError('Recent viewed result is shared')):
            youtube_fallback.run(self.importer)
        self.assertEqual(self.channel()['name'], 'Owner name')
        self.assertEqual(self.channel()['banner'], '/artwork/current')


if __name__ == '__main__':
    unittest.main()
