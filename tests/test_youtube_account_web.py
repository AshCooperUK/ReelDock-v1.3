import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from backend import youtube_account


class CookieSubscriptionTransportTests(unittest.TestCase):
    channel = 'UC' + 'a' * 22

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SimpleNamespace(directory=Path(self.directory.name))
        self.cookie = self.store.directory / 'youtube-cookies.txt'
        self.cookie.write_text('# Netscape HTTP Cookie File\n'
                               '.youtube.com\tTRUE\t/\tTRUE\t4102444800\tSAPISID\tfixture-sapisid\n')

    def test_cookie_authentication_is_preserved_without_a_bundled_api_key(self):
        for intent, status in [('subscribe', 200), ('unsubscribe', 204)]:
            with self.subTest(intent=intent), patch.object(youtube_account.requests, 'Session') as factory, \
                    patch.object(youtube_account.time, 'time', return_value=1800000000):
                session = factory.return_value.__enter__.return_value
                session.post.return_value.status_code = status
                youtube_account._cookie(self.store, self.channel, intent)
                session.post.assert_called_once()
                args, kwargs = session.post.call_args
                self.assertEqual(args, ('https://www.youtube.com/youtubei/v1/subscription/' + intent,))
                self.assertEqual(kwargs['params'], {'prettyPrint': 'false'})
                digest = hashlib.sha1(b'1800000000 fixture-sapisid https://www.youtube.com').hexdigest()
                self.assertEqual(kwargs['headers']['Authorization'], 'SAPISIDHASH 1800000000_' + digest)
                self.assertEqual(kwargs['headers']['Origin'], 'https://www.youtube.com')
                self.assertEqual(kwargs['json']['channelIds'], [self.channel])
                self.assertEqual(kwargs['json']['context']['client']['clientName'], 'WEB')
                self.assertEqual(kwargs['timeout'], (5, 20))
                jar = session.cookies.update.call_args.args[0]
                self.assertEqual([(cookie.name, cookie.value) for cookie in jar], [('SAPISID', 'fixture-sapisid')])

    def test_rejected_subscription_does_not_retry_or_disclose_cookies(self):
        with patch.object(youtube_account.requests, 'Session') as factory:
            session = factory.return_value.__enter__.return_value
            session.post.return_value.status_code = 403
            with self.assertRaisesRegex(ValueError, '^YouTube did not accept the subscription change from the saved cookies.$'):
                youtube_account._cookie(self.store, self.channel, 'subscribe')
            session.post.assert_called_once()

    def test_network_failure_does_not_retry_or_disclose_request_details(self):
        with patch.object(youtube_account.requests, 'Session') as factory:
            session = factory.return_value.__enter__.return_value
            session.post.side_effect = requests.RequestException('fixture-sapisid transport detail')
            with self.assertRaisesRegex(ValueError, '^YouTube could not be reached with the saved cookies.$'):
                youtube_account._cookie(self.store, self.channel, 'subscribe')
            session.post.assert_called_once()

    def test_missing_signed_in_cookie_prevents_a_write(self):
        self.cookie.write_text('# Netscape HTTP Cookie File\n'
                               '.youtube.com\tTRUE\t/\tTRUE\t4102444800\tSID\tfixture-sid\n')
        with patch.object(youtube_account.requests, 'Session') as factory:
            with self.assertRaisesRegex(ValueError, 'Signed-in YouTube cookies are missing'):
                youtube_account._cookie(self.store, self.channel, 'subscribe')
            factory.assert_not_called()
