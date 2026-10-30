import hashlib
import tempfile
import unittest
from pathlib import Path

from backend.artwork import cache_batch
from backend.catalogue import initialise, thumbnail
from backend.storage import Store


class RelativeArtworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'data')
        initialise(self.store)
        self.store.set('setup_complete', '1')

    def tearDown(self):
        self.tmp.cleanup()

    def test_protocol_relative_avatar_is_saved_as_https(self):
        path = thumbnail(self.store, {'thumbnails': {'medium': {'url': '//yt3.googleusercontent.com/abc=s176'}}})
        with self.store.connect() as db:
            url = db.execute('SELECT url FROM artwork WHERE id=?', (path[9:],)).fetchone()[0]
        self.assertEqual(url, 'https://yt3.googleusercontent.com/abc=s176')

    def test_existing_relative_rows_are_repaired_and_downloaded(self):
        key = hashlib.sha256(b'//yt3.googleusercontent.com/old').hexdigest()
        with self.store.connect() as db:
            db.execute('INSERT INTO artwork(id,url,checked) VALUES(?,?,?)', (key, '//yt3.googleusercontent.com/old', 1.0))
            db.execute("INSERT INTO channels VALUES('UCx', ?, 1)", ('{"id":"UCx","avatar":"/artwork/%s"}' % key,))
        store = Store(Path(self.tmp.name) / 'data')
        initialise(store)  # restart runs the repair
        fetched = []
        cache_batch(store, fetch=lambda url: fetched.append(url) or b'\x89PNG\r\n\x1a\n')
        self.assertEqual(fetched, ['https://yt3.googleusercontent.com/old'])
        self.assertTrue((Path(self.tmp.name) / 'data' / 'artwork' / key).is_file())


if __name__ == '__main__':
    unittest.main()
