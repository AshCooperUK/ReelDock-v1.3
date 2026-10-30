import json
import unittest
from unittest.mock import patch, MagicMock
from backend.network import google_request


class GoogleErrorTests(unittest.TestCase):
    def failure(self, host, data, status=403):
        connection=MagicMock()
        connection.getresponse.return_value.status=status
        connection.getresponse.return_value.read.return_value=json.dumps(data).encode()
        with patch('backend.network.http.client.HTTPSConnection',return_value=connection):
            with self.assertRaises(ValueError) as caught:
                google_request(host,'/test')
        return str(caught.exception)

    def test_quota_identified_without_provider_message(self):
        message=self.failure('www.googleapis.com',{'error':{'errors':[{'reason':'quotaExceeded'}],'message':'SECRET'}})
        self.assertIn('YouTube API check',message)
        self.assertIn('quotaExceeded',message)
        self.assertNotIn('SECRET',message)

    def test_token_error_identifies_sign_in(self):
        message=self.failure('oauth2.googleapis.com',{'error':'invalid_client','error_description':'SECRET'},401)
        self.assertIn('Google sign-in failed (HTTP 401, invalid_client)',message)
        self.assertNotIn('SECRET',message)

    def test_unknown_and_malformed_errors_remain_private(self):
        for data in [{'error':'SECRET'}, {'error':{'errors':None}}, ['SECRET'], {'error':{'errors':[{'reason':[]}]} }]:
            message=self.failure('www.googleapis.com',data)
            self.assertIn('HTTP 403',message)
            self.assertNotIn('SECRET',message)
