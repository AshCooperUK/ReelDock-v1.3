"""Account refusals are terminal without echoing private NNTP responses."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.downloaders.nzb.engine import NzbEngine, _File, _Job, _Segment
from backend.downloaders.nzb.nntp import (
    NntpAuthenticationError, NntpClient, NntpConnectionBusy, auth_failure,
)
from backend.downloaders.nzb.yenc import encode


SERVER = dict(host='fixture.invalid', port=563, ssl=True,
              username='fixture-user', password='fixture-secret', connections=8)
AUTH_ERROR = 'The Usenet server rejected authentication. Check the account username and password.'


class NntpAuthenticationTests(unittest.TestCase):
    def test_502_needs_explicit_connection_limit_text_to_mean_busy(self):
        for text in ('Authentication Failed', 'Access denied', '',
                     'Too many failed authentication attempts for fixture-user',
                     'Too many authentication failures on this connection'):
            with self.subTest(text=text):
                self.assertEqual(auth_failure(502, text), AUTH_ERROR)
        for code, text in ((502, 'Too many connections'),
                           (400, 'Too many simultaneous connections'),
                           (481, 'Account connection limit reached'),
                           (502, 'Maximum number of connections reached')):
            with self.subTest(code=code, text=text):
                self.assertIn('all of its connections', auth_failure(code, text))

    def test_username_and_password_refusals_keep_a_sanitized_typed_reason(self):
        for username_reply, password_reply in (
                ((502, 'Authentication Failed fixture-user fixture-secret'), None),
                ((381, 'Password required'), (502, 'Authentication Failed fixture-secret')),
                ((381, 'Password required'), (400, 'Account access refused fixture-secret'))):
            with self.subTest(username_reply=username_reply, password_reply=password_reply):
                client = NntpClient(**{key: SERVER[key] for key in ('host', 'port', 'username', 'password')}, use_ssl=True)
                replies = [username_reply] + ([password_reply] if password_reply else [])
                client._command = Mock(side_effect=replies)
                with self.assertRaises(NntpAuthenticationError) as caught:
                    client._authenticate()
                self.assertEqual(str(caught.exception), AUTH_ERROR)
                self.assertEqual(client._command.call_count, len(replies))

    def test_connection_limits_on_either_auth_command_stay_retryable(self):
        for replies in ([(400, 'Too many connections')],
                        [(381, 'Password required'), (502, 'Connection limit reached')]):
            with self.subTest(replies=replies):
                client = NntpClient('fixture.invalid', 563, True, 'fixture-user', 'fixture-secret')
                client._command = Mock(side_effect=replies)
                with self.assertRaises(NntpConnectionBusy) as caught:
                    client._authenticate()
                self.assertIn('all of its connections', str(caught.exception))

    def test_article_access_refusals_use_the_same_sanitized_account_reason(self):
        for method in ('body', 'stat'):
            with self.subTest(method=method):
                client = NntpClient('fixture.invalid', 563, True, 'fixture-user', 'fixture-secret')
                client._article = Mock(return_value=(502, 'Access denied for fixture-user fixture-secret'))
                with self.assertRaises(NntpAuthenticationError) as caught:
                    getattr(client, method)('fixture-article')
                self.assertEqual(str(caught.exception), AUTH_ERROR)


class NzbAuthenticationHandlingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.spawn = patch.object(NzbEngine, '_spawn')
        self.spawn.start()
        self.addCleanup(self.spawn.stop)
        self.engine = NzbEngine(Path(self.temp.name) / 'media', Path(self.temp.name) / 'data')
        self.addCleanup(self.engine.shutdown)
        self.engine.config['servers'] = [SERVER]

    def job(self, count=100):
        segments = [_Segment(index, 5, f'fixture-{index}', ('fixture',), 0)
                    for index in range(1, count + 1)]
        job = _Job('a' * 32, 'Fixture movie', [_File(0, 'Fixture.mkv', ('fixture',), segments)], {0},
                   phase='download', running=True, total_parts=count)
        self.engine._jobs[job.id] = job
        self.engine._order.append(job)
        return job

    def refused_client(self, replies):
        created, closed = [], []

        class Client(NntpClient):
            def __init__(inner, *args, **kwargs):
                super().__init__(*args, **kwargs)
                inner._command = Mock(side_effect=list(replies))
                created.append(inner)

            def body(inner, *_args):
                inner._authenticate()
                raise AssertionError('Fixture authentication unexpectedly succeeded')

            def close(inner):
                closed.append(inner)

        return Client, created, closed

    def test_authentication_failure_is_bounded_saved_and_not_requeued_after_restart(self):
        job = self.job()
        first = job.files[0].segments[0]
        self.engine._write_part(job, first, b'cached', 'Fixture.mkv', 0)
        cached = self.engine._part_path(job, 0, first.number)
        client, created, closed = self.refused_client([(381, 'Password required'), (502, 'Authentication Failed fixture-secret')])
        with patch('backend.downloaders.nzb.engine.NntpClient', client):
            self.engine._download(job)
        self.assertEqual(job.phase, 'failed')
        self.assertEqual(self.engine._job_dict(job)['state'], 'failed')
        self.assertEqual(job.error, AUTH_ERROR)
        self.assertEqual(job.done_parts, 1)
        self.assertEqual(cached.read_bytes(), b'cached')
        self.assertGreaterEqual(len(created), 1)
        self.assertLessEqual(len(created), SERVER['connections'])
        self.assertCountEqual(created, closed)
        self.assertEqual(self.engine._slots._used, 0)
        self.assertFalse(job.running)
        saved = json.loads((self.engine.state_dir / (job.id + '.json')).read_text())
        self.assertEqual(saved['phase'], 'failed')
        self.assertEqual(saved['error'], AUTH_ERROR)
        restarted = NzbEngine(self.engine.media_base, self.engine.data_dir)
        self.addCleanup(restarted.shutdown)
        self.assertEqual(restarted._jobs[job.id].phase, 'failed')
        self.assertEqual(restarted._jobs[job.id].error, AUTH_ERROR)

    def test_authentication_with_an_unusual_status_does_not_retry_every_article(self):
        job = self.job()
        client, created, closed = self.refused_client([(400, 'Account access refused fixture-secret')])
        with patch('backend.downloaders.nzb.engine.NntpClient', client):
            self.engine._download(job)
        self.assertEqual(job.phase, 'failed')
        self.assertEqual(job.error, AUTH_ERROR)
        self.assertLessEqual(len(created), SERVER['connections'])
        self.assertTrue(all(item._command.call_count == 1 for item in created))
        self.assertCountEqual(created, closed)
        self.assertEqual(self.engine._slots._used, 0)

    def test_explicit_connection_exhaustion_still_queues_instead_of_failing(self):
        job = self.job()
        client, created, closed = self.refused_client([(400, 'Too many connections')])
        with patch('backend.downloaders.nzb.engine.NntpClient', client):
            self.engine._download(job)
        self.assertEqual(job.phase, 'queued')
        self.assertEqual(job.error, '')
        self.assertCountEqual(created, closed)
        self.assertEqual(self.engine._slots._used, 0)

    def test_another_configured_server_can_still_supply_the_articles(self):
        job = self.job(count=3)
        second = dict(SERVER, host='second-fixture.invalid')
        self.engine.config['servers'] = [SERVER, second]
        hosts = []

        class Client(NntpClient):
            def body(inner, *_args):
                hosts.append(inner.host)
                if inner.host == SERVER['host']:
                    raise NntpAuthenticationError(502, AUTH_ERROR)
                return encode(b'video', 'Fixture.mkv')

        with patch('backend.downloaders.nzb.engine.NntpClient', Client):
            self.engine._fetch(job)
        self.assertEqual(job.done_parts, 3)
        self.assertEqual(job.fetch_error, '')
        self.assertIn(SERVER['host'], hosts)
        self.assertIn(second['host'], hosts)
        self.assertEqual(self.engine._slots._used, 0)


if __name__ == '__main__':
    unittest.main()
