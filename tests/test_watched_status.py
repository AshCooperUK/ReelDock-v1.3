import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
import requests
from backend import watched_status as watched


class WatchedStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.cookies=Path(self.tmp.name)/'cookies.txt'
        self.cookies.write_text('# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t4102444800\tSAPISID\tprivate-fixture-token\n')

    def tearDown(self):self.tmp.cleanup()

    def session(self,response=None,error=None):
        session=MagicMock()
        if error:session.__enter__.return_value.get.side_effect=error
        else:session.__enter__.return_value.get.return_value=response
        return patch('backend.watched_status.requests.Session',return_value=session)

    def test_session_explicit_confirmation_and_consent(self):
        response=MagicMock(url='https://www.youtube.com/feed/history',text='ytcfg.set({"LOGGED_IN":true});')
        with self.session(response):self.assertEqual(watched.check_cookie_session(self.cookies)[0],'active')
        response.url='https://consent.youtube.com/'
        with self.session(response):
            state,message=watched.check_cookie_session(self.cookies)
        self.assertEqual(state,'auth_unverified');self.assertIn('consent',message)

    def test_network_and_rate_limit_are_distinct(self):
        with self.session(error=requests.ConnectionError('private URL secret')):
            state,message=watched.check_cookie_session(self.cookies)
        self.assertEqual(state,'network_error');self.assertNotIn('secret',message)
        response=MagicMock(url='https://www.youtube.com/feed/history')
        response.raise_for_status.side_effect=requests.HTTPError(response=SimpleNamespace(status_code=429))
        with self.session(response):self.assertEqual(watched.check_cookie_session(self.cookies)[0],'rate_limited')

    def test_sender_enables_node_and_does_not_require_downloadable_formats(self):
        result=SimpleNamespace(returncode=0,stdout='[youtube] Marking watched\n[youtube] Marking fully watched',stderr='WARNING: unrelated thumbnail warning')
        with patch.object(watched,'check_cookie_session',return_value=('active','ok')),patch.object(watched.subprocess,'run',return_value=result) as run:
            state,message=watched.send_watched_request('video000001',self.cookies)
        self.assertEqual(state,'sent')
        command=run.call_args.args[0]
        self.assertIn('--simulate',command);self.assertIn('--ignore-no-formats-error',command)
        self.assertEqual(command[command.index('--js-runtimes')+1],'node')
        self.assertNotEqual(command[command.index('--cookies')+1],str(self.cookies))
        self.assertIn('private-fixture-token',self.cookies.read_text())

    def test_cookie_failure_never_invokes_sender(self):
        with patch.object(watched,'check_cookie_session',return_value=('auth_unverified','No request sent')),patch.object(watched.subprocess,'run') as run:
            self.assertEqual(watched.send_watched_request('video000001',self.cookies)[0],'auth_unverified')
        run.assert_not_called()

    def test_error_categories_do_not_expose_extractor_output(self):
        for output,expected in [('ERROR: Sign in to confirm you’re not a bot','bot/sign-in'),('ERROR: Private video','private'),('ERROR: getaddrinfo failed','DNS'),('ERROR: HTTP Error 429','rate-limiting'),('ERROR: no supported JavaScript runtime','JavaScript')]:
            state,message=watched.interpret_result(1,output+' https://secret.example/token?cookie=private-fixture-token')
            self.assertEqual(state,'extractor_failed');self.assertIn(expected,message)
            self.assertNotIn('secret.example',message);self.assertNotIn('private-fixture-token',message)
        state,message=watched.interpret_result(1,'WARNING: JavaScript runtime unavailable\nERROR: Private video')
        self.assertIn('private',message)

    def test_partial_request_and_timeout_stay_uncertain(self):
        self.assertEqual(watched.interpret_result(0,'Marking fully watched\nWARNING: Unable to mark watched')[0],'unverified')
        self.assertEqual(watched.interpret_result(1,'Marking watched\nERROR: network failed')[0],'unverified')
        with patch.object(watched,'check_cookie_session',return_value=('active','ok')),patch.object(watched.subprocess,'run',side_effect=subprocess.TimeoutExpired('yt-dlp',90)):
            self.assertEqual(watched.send_watched_request('video000001',self.cookies)[0],'unverified')
