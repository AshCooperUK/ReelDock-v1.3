"""Opening local information coexists with workers; file mutations stay fenced."""
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import patch
import test_view_media as vm
from backend.media_management import local, service, store as records


class ForegroundManagementTests(unittest.TestCase):
    setUp = vm.ViewMediaTests.setUp
    tearDown = vm.ViewMediaTests.tearDown

    def post(self, body, csrf=True):
        return self.client.post('/api/media-management', base_url=vm.ORIGIN,
            json=body, headers={'Origin': vm.ORIGIN,
                **({'X-CSRF-Token': 'token'} if csrf else {})})

    def identity(self, path):
        self.store.media_root = str(self.root)
        state = {}; vm.local._scan(self.root, state)
        vm.local._CACHE[str(self.root)] = state
        return next(key for key, value in state['files'].items() if value['path'] == path)

    @contextmanager
    def worker_running(self):
        entered, release = threading.Event(), threading.Event()
        def work():
            with self.store.background_operation():
                entered.set(); release.wait(5)
        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(entered.wait(2))
        try:
            yield
            self.assertTrue(thread.is_alive(), 'information completed before the worker released its lock')
        finally:
            release.set(); thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_generic_information_loads_while_background_work_holds_shared_lock(self):
        path = self.root/'Episode.mp4'; path.write_bytes(b'original media')
        path.with_suffix('.nfo').write_text('<episodedetails><title>Episode 2</title><showtitle>Local show</showtitle><season>28</season><episode>2</episode></episodedetails>')
        identity = self.identity(path); before = path.stat()
        with self.store.connect() as db:
            db.execute("INSERT INTO downloads(id,title,quality,state,created,updated) VALUES('abcdefghijk','Active','1080','downloading',1,2)")
            original = tuple(db.execute("SELECT * FROM downloads WHERE id='abcdefghijk'").fetchone())
        with self.worker_running(), patch('backend.view_media.providers.details') as provider:
            response = self.post(dict(action='enrol-local', value=dict(id=identity)))
        self.assertEqual(response.status_code, 200, response.json)
        provider.assert_not_called()
        self.assertEqual(response.json['entry']['title'], 'Episode 2')
        self.assertTrue(response.json['entry']['file'])
        after = path.stat()
        self.assertEqual((before.st_ino,before.st_size,before.st_mtime_ns), (after.st_ino,after.st_size,after.st_mtime_ns))
        self.assertEqual(path.read_bytes(), b'original media')
        with self.store.connect() as db:
            self.assertEqual(tuple(db.execute("SELECT * FROM downloads WHERE id='abcdefghijk'").fetchone()), original)

    def test_existing_identity_and_hold_are_retained_under_worker_lock(self):
        path = self.root/'Film.mp4'; path.write_bytes(b'film')
        identity = self.identity(path)
        first = self.post(dict(action='enrol-local', value=dict(id=identity)))
        key = first.json['key']
        with self.store.connect() as db:
            db.execute('UPDATE managed_media SET hold=1,removed=1,monitor=0 WHERE key=?', (key,))
        with self.worker_running():
            again = self.post(dict(action='enrol-local', value=dict(id=identity)))
        self.assertEqual(again.status_code, 200, again.json)
        self.assertEqual(again.json['key'], key)
        self.assertTrue(again.json['entry']['hold'])
        self.assertTrue(again.json['entry']['removed'])
        self.assertFalse(again.json['entry']['monitored'])

    def test_file_replacement_during_optional_lookup_is_rejected(self):
        path = self.root/'Film.mp4'; path.write_bytes(b'original')
        identity = self.identity(path)
        original = local.catalogue
        def changed(*args):
            result = original(*args)
            replacement = self.root/'replacement.mp4'; replacement.write_bytes(b'new file')
            replacement.replace(path)
            return result
        with patch.object(local,'catalogue',side_effect=changed):
            response = self.post(dict(action='enrol-local',value=dict(id=identity)))
        self.assertEqual(response.status_code, 400, response.json)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM managed_media').fetchone()[0],0)
        self.assertEqual(path.read_bytes(), b'new file')

    def test_auth_and_exclusive_mutation_fence_remain(self):
        path = self.root/'Film.mp4'; path.write_bytes(b'film')
        identity = self.identity(path)
        self.assertEqual(self.post(dict(action='enrol-local', value=dict(id=identity)), csrf=False).status_code,403)
        response = self.post(dict(action='enrol-local', value=dict(id=identity)))
        self.assertEqual(response.status_code,200,response.json)
        with self.worker_running():
            renamed = self.post(dict(action='rename',key=response.json['key'],value='Renamed'))
        self.assertEqual(renamed.status_code,400,renamed.json)
        self.assertIn('busy',renamed.json['error'])
        self.assertEqual(path.read_bytes(),b'film')
        self.assertEqual(service.body(records.row(self.store,response.json['key']))['localPath'],str(path))
