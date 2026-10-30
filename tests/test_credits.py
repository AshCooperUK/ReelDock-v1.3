import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import credits
from backend.app import create_app


class CreditsTests(unittest.TestCase):
    def test_installed_python_notices_include_copyright(self):
        entry = next(e for e in credits.summaries() if e['name'].lower() == 'flask')
        self.assertNotIn('files', entry)
        self.assertIn('BSD', entry['licence'])
        self.assertIn('Copyright', credits.notice(entry['id']))
        self.assertIsNone(credits.notice('../../data/settings'))

    def test_public_credits_only_expose_catalogued_notices(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app({'TESTING': True, 'DATA_DIR': directory})
            app.extensions['store'].set('public_origin', 'https://dev.example.com')
            client = app.test_client()
            response = client.get('/api/credits', base_url='https://dev.example.com')
            self.assertEqual(response.status_code, 200)
            item = next(e for e in response.json['items'] if e['name'].lower() == 'flask')
            notice = client.get('/licenses/' + item['id'], base_url='https://dev.example.com')
            self.assertEqual(notice.status_code, 200)
            self.assertEqual(notice.mimetype, 'text/plain')
            self.assertEqual(client.get('/licenses/unknown', base_url='https://dev.example.com').status_code, 404)

    def test_debian_notice_includes_referenced_common_license(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'copyright'
            path.write_text('Copyright Example\nSee /usr/share/common-licenses/GPL-2')
            entry = dict(name='Example', version='1', licence='GPL-2', url='https://example.com', files=[path])
            real_read = credits.read
            with patch.object(credits, 'catalog', return_value={'example': entry}), patch.object(credits, 'read', side_effect=lambda p: 'Full GPL text' if str(p).replace('\\', '/').endswith('/common-licenses/GPL-2') else real_read(p)):
                self.assertIn('Full GPL text', credits.notice('example'))
