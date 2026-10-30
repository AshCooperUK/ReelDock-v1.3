import json
import time
import unittest
from unittest.mock import patch
import test_guide as fixture
from backend import youtube_fallback as fb

CHANNEL = fixture.CHANNEL
BANNER = [
    dict(id='0', url='https://yt3.googleusercontent.com/b=w1060', width=1060, height=175),
    dict(id='3', url='https://yt3.googleusercontent.com/b=w2120', width=2120, height=351),
    dict(id='banner_uncropped', url='https://yt3.googleusercontent.com/b=uncropped'),
    dict(id='7', url='https://yt3.googleusercontent.com/a=s900', width=900, height=900),
    dict(id='avatar_uncropped', url='https://yt3.googleusercontent.com/a=uncropped'),
]


class ChannelArtBackfillTests(unittest.TestCase):
    tearDown = fixture.GuideTests.tearDown
    google = fixture.GuideTests.google

    def setUp(self):
        fixture.GuideTests.setUp(self)
        self.store.set('youtube_use_google_api', 'false')
        self.calls = []
        self.reply = dict(id=CHANNEL, channel_id=CHANNEL, thumbnails=BANNER, channel_follower_count=16900000, entries=[])
        self.app.config['YOUTUBE_EXTRACT'] = self.extract
        self.sleep = patch('backend.youtube_fallback.time.sleep'); self.sleep.start(); self.addCleanup(self.sleep.stop)
        channel = dict(id=CHANNEL, name='Cookie channel', playlist='UU' + CHANNEL[2:], avatar='/artwork/x')
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO channels VALUES(?,?,1)', (CHANNEL, json.dumps(channel)))

    def extract(self, url, mode, start, end):
        self.calls.append((url, mode))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def channel(self):
        with self.store.connect() as db:
            return json.loads(db.execute('SELECT data FROM channels WHERE id=?', (CHANNEL,)).fetchone()[0])

    def test_picks_header_sized_banner_not_avatar_or_uncropped(self):
        self.assertEqual(fb.channel_banner(dict(thumbnails=BANNER)), 'https://yt3.googleusercontent.com/b=w1060')
        self.assertEqual(fb.channel_banner(dict(thumbnails=[BANNER[3]])), '')
        self.assertEqual(fb.channel_banner(dict(thumbnails=[BANNER[2]])), 'https://yt3.googleusercontent.com/b=uncropped')

    def test_backfills_banner_and_subscribers_once_then_waits(self):
        fb.backfill_channel_art(self.importer)
        data = self.channel()
        self.assertEqual(data['subscribers'], '16900000')
        self.assertEqual(data['avatar'], '/artwork/x')
        with self.store.connect() as db:
            url = db.execute('SELECT url FROM artwork WHERE id=?', (data['banner'][9:],)).fetchone()[0]
        self.assertEqual(url, 'https://yt3.googleusercontent.com/b=w1060')
        self.store.set('channel_art_next', 0)
        fb.backfill_channel_art(self.importer)
        self.assertEqual(len(self.calls), 1)  # checked channels are not read again this month

    def test_protocol_relative_banner_is_saved_as_https(self):
        self.reply = dict(self.reply, thumbnails=[dict(id='0', url='//yt3.googleusercontent.com/rel', width=1060, height=175)])
        fb.backfill_channel_art(self.importer)
        with self.store.connect() as db:
            url = db.execute('SELECT url FROM artwork WHERE id=?', (self.channel()['banner'][9:],)).fetchone()[0]
        self.assertEqual(url, 'https://yt3.googleusercontent.com/rel')

    def test_spaced_out_and_failures_retry_later(self):
        self.reply = ValueError('blocked')
        fb.backfill_channel_art(self.importer)
        fb.backfill_channel_art(self.importer)  # inside the gap: no second request
        self.assertEqual(len(self.calls), 1)
        data = self.channel()
        self.assertNotIn('banner', data)
        self.assertLess(float(data['art_checked']), time.time())

    def test_skipped_when_google_api_supplies_details(self):
        self.store.set('youtube_use_google_api', 'true')
        self.store.set('google_tokens', 'present')
        fb.backfill_channel_art(self.importer)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
