import unittest
from unittest.mock import MagicMock, patch

from backend.m3u import fetch_playlist


class PlaylistFetchTests(unittest.TestCase):
    def fetch(self, chunks, cancel=lambda: None):
        response = MagicMock(status=200)
        response.read.side_effect = chunks
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch('backend.m3u.public_addresses', return_value=['93.184.216.34']), \
             patch('backend.m3u.socket.create_connection'), \
             patch('backend.m3u.http.client.HTTPConnection', return_value=connection), \
             patch('backend.m3u.time.monotonic', side_effect=[0, 301, 602, 903]):
            try:
                return fetch_playlist('http://example.com/playlist', check_cancel=cancel)
            finally:
                connection.close.assert_called_once()

    def test_playlist_larger_than_previous_128_mib_limit(self):
        chunk = b'a' * 65536
        result = self.fetch([chunk] * 2049 + [b''])
        self.assertEqual(len(result), 2049 * len(chunk))

    def test_progressing_fetch_can_exceed_five_minutes(self):
        self.assertEqual(self.fetch([b'#EXTM3U\n', b'# completed\n', b'']), '#EXTM3U\n# completed\n')

    def test_cancellation_still_interrupts_and_closes_connection(self):
        def cancel():
            raise RuntimeError('cancelled')
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.fetch([b'#EXTM3U\n'], cancel)

    def test_stalled_connection_still_fails_without_exposing_url(self):
        with self.assertRaisesRegex(ValueError, 'Could not read the playlist'):
            self.fetch([TimeoutError('private provider details')])
