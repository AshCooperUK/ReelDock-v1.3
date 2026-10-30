import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app import create_app
from backend.catalogue import Importer
from backend import channel_metadata_refresh as metadata

CHANNEL = 'UC' + 'a' * 22
BANNER = 'https://yt3.googleusercontent.com/public-channel-banner'


class ChannelMetadataRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(dict(TESTING=True, DATA_DIR=self.temp.name, MEDIA_DIR=str(Path(self.temp.name)/'media')))
        self.store = self.app.extensions['store']
        self.store.set('setup_complete', '1')
        self.store.set('youtube_sync_paused', 'false')
        self.importer = Importer(self.app)
        metadata.initialise(self.store)
        self.original = dict(id=CHANNEL, name='Saved name', avatar='/saved-avatar', playlist='saved-playlist',
            last_checked=12345, coverage={'days': 365}, indexed_videos=70, description='Old description', custom='retained')
        self.set_channel(self.original)

    def tearDown(self):
        self.temp.cleanup()

    def set_channel(self, data):
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO channels VALUES(?,?,1)', (CHANNEL, json.dumps(data)))

    def channel(self):
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM channels WHERE id=?', (CHANNEL,)).fetchone()
        return json.loads(row[0]) if row else None

    def row(self):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM channel_metadata_refresh_requests WHERE channel=?', (CHANNEL,)).fetchone()
        return dict(row) if row else None

    def extracted(self, **changes):
        return dict(channel_id=CHANNEL, id=CHANNEL, title='Provider name must not replace saved name',
            description='Public channel description', channel_follower_count=12000,
            thumbnails=[dict(id='avatar', url='https://yt3.googleusercontent.com/avatar', width=320, height=320),
                        dict(id='banner', url=BANNER, width=1280, height=212)], **changes)

    def dump(self):
        with self.store.connect() as db: return '\n'.join(db.iterdump())

    def test_status_is_read_only_and_request_requires_known_active_channel(self):
        before = self.dump()
        self.assertEqual(metadata.status(self.store, CHANNEL)['state'], 'idle')
        self.assertEqual(self.dump(), before)
        with self.assertRaises(ValueError): metadata.request(self.store, 'invalid')
        with self.assertRaises(LookupError): metadata.request(self.store, 'UC'+'z'*22)
        self.store.set('youtube_sync_paused', 'true')
        self.assertEqual(metadata.request(self.store, CHANNEL)['state'], 'paused')
        self.assertIsNone(self.row())

    def test_public_bounded_extract_caches_only_banner_and_merges_display_fields(self):
        self.store.set('acquisition_hold', '1')
        before = {key: self.store.get(key) for key in ('acquisition_hold', 'sync_requested', 'next_history')}
        with self.store.connect() as db:
            policies = {name: [tuple(row) for row in db.execute('SELECT * FROM '+name)] for name in ('download_rules','history_jobs','youtube_channel_requests','downloads')}
        self.assertEqual(metadata.request(self.store, CHANNEL)['state'], 'queued')
        with patch('backend.youtube_fallback.extract', return_value=self.extracted()) as extract:
            self.assertTrue(metadata.tick(self.importer))
        extract.assert_called_once_with(self.importer, CHANNEL, 'channel', public=True, timeout=30)
        saved = self.channel()
        for key, value in self.original.items():
            if key != 'description': self.assertEqual(saved[key], value, key)
        self.assertEqual(saved['description'], 'Public channel description')
        self.assertEqual(saved['subscribers'], '12000')
        self.assertEqual(saved['banner'], '/artwork/'+hashlib.sha256(BANNER.encode()).hexdigest())
        self.assertGreater(saved['metadataChecked'], 0)
        self.assertEqual(saved['art_checked'], saved['metadataChecked'])
        with self.store.connect() as db:
            self.assertEqual([row['url'] for row in db.execute('SELECT url FROM artwork')], [BANNER])
            self.assertEqual([row['id'] for row in db.execute('SELECT id FROM artwork_requests')], [hashlib.sha256(BANNER.encode()).hexdigest()])
            self.assertEqual({name: [tuple(row) for row in db.execute('SELECT * FROM '+name)] for name in policies}, policies)
        self.assertEqual({key: self.store.get(key) for key in before}, before)
        self.assertEqual(metadata.status(self.store, CHANNEL)['state'], 'fresh')

    def test_video_thumbnail_is_never_used_as_a_channel_banner_and_partial_cache_is_daily(self):
        result = dict(channel_id=CHANNEL, thumbnails=[dict(id='0', url='https://i.ytimg.com/vi/video/hqdefault.jpg', width=640, height=360)])
        now = time.time()
        with patch('backend.channel_metadata_refresh.time.time', return_value=now):
            metadata.request(self.store, CHANNEL)
            with patch('backend.youtube_fallback.extract', return_value=result): self.assertTrue(metadata.tick(self.importer))
        self.assertNotIn('banner', self.channel())
        self.assertNotIn('subscribers', self.channel())
        self.assertEqual(self.channel()['description'], 'Old description')
        with self.store.connect() as db: self.assertEqual(db.execute('SELECT count(*) FROM artwork').fetchone()[0], 0)
        with patch('backend.channel_metadata_refresh.time.time', return_value=now+3600):
            self.assertEqual(metadata.request(self.store, CHANNEL)['state'], 'fresh')
            with patch('backend.youtube_fallback.extract') as extract:
                self.assertFalse(metadata.tick(self.importer)); extract.assert_not_called()
        with patch('backend.channel_metadata_refresh.time.time', return_value=now+2*86400):
            self.assertEqual(metadata.request(self.store, CHANNEL)['state'], 'queued')

    def test_complete_information_is_fresh_for_thirty_days(self):
        now = time.time()
        self.set_channel(dict(self.original, banner='/saved-banner', subscribers='0', art_checked=now))
        with patch('backend.channel_metadata_refresh.time.time', return_value=now+2*86400):
            self.assertEqual(metadata.request(self.store, CHANNEL)['state'], 'fresh')
            with patch('backend.youtube_fallback.extract') as extract:
                self.assertFalse(metadata.tick(self.importer)); extract.assert_not_called()
        with patch('backend.channel_metadata_refresh.time.time', return_value=now+31*86400):
            self.assertEqual(metadata.request(self.store, CHANNEL)['state'], 'queued')

    def test_failures_have_fixed_backoff_do_not_copy_private_text_and_expire(self):
        now = time.time()
        with patch('backend.channel_metadata_refresh.time.time', return_value=now):
            metadata.request(self.store, CHANNEL)
            with patch('backend.youtube_fallback.extract', side_effect=ValueError('https://private.invalid/?secret=PRIVATE')):
                self.assertTrue(metadata.tick(self.importer), 'a failed attempt still consumes this worker iteration')
        self.assertEqual(self.channel(), self.original)
        retry = self.row()['retry_at']
        self.assertEqual(retry, now+metadata.RETRY_SECONDS)
        with patch('backend.channel_metadata_refresh.time.time', return_value=now+1):
            for _ in range(3): self.assertEqual(metadata.request(self.store, CHANNEL)['state'], 'retry')
            with patch('backend.youtube_fallback.extract') as extract:
                self.assertFalse(metadata.tick(self.importer)); extract.assert_not_called()
            self.assertEqual(self.row()['retry_at'], retry)
            self.assertNotIn('PRIVATE', json.dumps(metadata.status(self.store, CHANNEL)))
        with patch('backend.channel_metadata_refresh.time.time', return_value=now+metadata.REQUEST_SECONDS+2):
            with patch('backend.youtube_fallback.extract') as extract:
                self.assertFalse(metadata.tick(self.importer)); extract.assert_not_called()

    def test_wrong_channel_cannot_publish_metadata_or_artwork(self):
        metadata.request(self.store, CHANNEL)
        with patch('backend.youtube_fallback.extract', return_value={'id':'UC'+'z'*22, 'description':'Wrong owner', 'thumbnails':[{'id':'banner','url':BANNER}]}):
            self.assertTrue(metadata.tick(self.importer))
        self.assertEqual(self.channel(), self.original)
        with self.store.connect() as db: self.assertEqual(db.execute('SELECT count(*) FROM artwork').fetchone()[0], 0)
        self.assertEqual(self.row()['state'], 'retry')

    def test_pause_checkpoint_releases_claim_without_treating_pause_as_provider_failure(self):
        from backend.sync_controls import SyncPaused
        metadata.request(self.store, CHANNEL)
        with patch('backend.youtube_fallback.extract', side_effect=SyncPaused('Paused')):
            self.assertTrue(metadata.tick(self.importer))
        self.assertEqual(self.row()['claim'], '')
        self.assertEqual(self.row()['lease_until'], 0)
        self.assertEqual(self.row()['retry_at'], 0)
        self.assertEqual(self.channel(), self.original)

    def test_inflight_request_coalesces_and_merges_concurrent_channel_edits(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        def extract(*args, **kwargs):
            entered.set()
            if not release.wait(5): raise AssertionError('Test did not release extraction')
            return self.extracted()
        def run():
            try: metadata.tick(self.importer)
            except BaseException as error: errors.append(error)
        metadata.request(self.store, CHANNEL)
        thread = threading.Thread(target=run)
        with patch('backend.youtube_fallback.extract', side_effect=extract) as read:
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                original_claim = self.row()['claim']
                self.assertEqual(metadata.request(self.store, CHANNEL)['state'], 'running')
                self.assertFalse(metadata.tick(Importer(self.app)))
                self.assertEqual(self.row()['claim'], original_claim)
                self.set_channel(dict(self.original, name='Concurrent name', avatar='/concurrent-avatar', custom='new-value'))
            finally:
                release.set(); thread.join(5)
            self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
            self.assertEqual(read.call_count, 1)
        saved = self.channel()
        self.assertEqual(saved['name'], 'Concurrent name')
        self.assertEqual(saved['avatar'], '/concurrent-avatar')
        self.assertEqual(saved['custom'], 'new-value')
        self.assertTrue(saved['banner'])

    def test_removal_pause_stop_or_changed_claim_discard_a_held_result(self):
        for action in ('remove', 'pause', 'stop', 'claim'):
            with self.subTest(action=action):
                self.set_channel(self.original)
                self.store.set('youtube_sync_paused', 'false')
                with self.store.connect() as db:
                    db.execute('DELETE FROM channel_metadata_refresh_requests')
                    db.execute('DELETE FROM artwork')
                    db.execute('DELETE FROM artwork_requests')
                metadata.request(self.store, CHANNEL)
                stop = threading.Event()
                def extract(*args, **kwargs):
                    if action == 'remove':
                        with self.store.connect() as db: db.execute('DELETE FROM channels WHERE id=?', (CHANNEL,))
                    elif action == 'pause': self.store.set('youtube_sync_paused', 'true')
                    elif action == 'stop': stop.set()
                    else:
                        with self.store.connect() as db: db.execute("UPDATE channel_metadata_refresh_requests SET claim='new-owner',lease_until=?", (time.time()+120,))
                    return self.extracted()
                with patch('backend.youtube_fallback.extract', side_effect=extract):
                    self.assertTrue(metadata.tick(self.importer, stop))
                self.assertEqual(self.channel(), None if action == 'remove' else self.original)
                with self.store.connect() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM artwork').fetchone()[0], 0)
                    self.assertEqual(db.execute('SELECT count(*) FROM artwork_requests').fetchone()[0], 0)
                if action == 'claim': self.assertEqual(self.row()['claim'], 'new-owner')
                elif self.row(): self.assertEqual(self.row()['lease_until'], 0)

    def test_recover_clears_abandoned_claim_and_drops_inactive_channel(self):
        metadata.request(self.store, CHANNEL)
        with self.store.connect() as db:
            db.execute("UPDATE channel_metadata_refresh_requests SET state='running',claim='abandoned',lease_until=?", (time.time()+120,))
        metadata.recover(self.store)
        self.assertEqual(self.row()['state'], 'queued')
        self.assertEqual(self.row()['claim'], '')
        with self.store.connect() as db: db.execute('UPDATE channels SET active=0 WHERE id=?', (CHANNEL,))
        metadata.recover(self.store)
        self.assertIsNone(self.row())


if __name__ == '__main__': unittest.main()
