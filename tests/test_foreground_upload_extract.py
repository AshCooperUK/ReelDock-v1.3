"""Independent real-extractor locking, public-window and budget regressions."""
import json
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.storage import Store
from backend import youtube_fallback as fallback

CHANNEL = 'UC' + 'a' * 22
UPLOADS = 'https://www.youtube.com/playlist?list=UU' + CHANNEL[2:]
VIDEO = 'https://www.youtube.com/watch?v=abcdefghijk'


class ForegroundUploadExtractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.set('youtube_sync_paused', 'false')
        self.app = SimpleNamespace(config={}, testing=True)
        self.importer = SimpleNamespace(store=self.store, app=self.app)
        self.threads, self.release_events = [], []

    def tearDown(self):
        for event in self.release_events: event.set()
        for thread in self.threads: thread.join(5)
        self.temp.cleanup()

    def thread(self, function):
        result, errors = [], []
        def run():
            try: result.append(function())
            except BaseException as error: errors.append(error)
        task = threading.Thread(target=run, daemon=True)
        self.threads.append(task)
        task.start()
        def finish():
            task.join(5)
            self.assertFalse(task.is_alive(), 'the bounded request must finish after release')
            if errors: raise errors[0]
            return result[0]
        return task, finish

    def foreground(self, **options):
        return fallback.extract(self.importer, UPLOADS, 'playlist', 1, 26,
                                public=True, foreground=True, timeout=30, **options)

    def test_actual_foreground_hook_runs_while_the_normal_background_lock_is_held(self):
        called = threading.Event()
        self.app.config['YOUTUBE_EXTRACT'] = lambda *args: (called.set(), {'entries': []})[1]
        normal_deadline = str(time.time()+3600)
        self.store.set('youtube_fallback_next', normal_deadline)
        fallback._lock.acquire()
        try:
            task, finish = self.thread(self.foreground)
            self.assertTrue(called.wait(2), 'a normal importer must not hold up the viewed upload hook')
            self.assertEqual(finish(), {'entries': []})
            self.assertTrue(fallback._lock.locked(), 'foreground work never releases or replaces the original lock')
        finally:
            fallback._lock.release()
        self.assertEqual(self.store.get('youtube_fallback_next'), normal_deadline)
        self.assertGreater(float(self.store.get('youtube_upload_browse_next')), time.time())

    def test_two_actual_foreground_requests_never_enter_the_hook_together(self):
        entered, release, second_started, second_attempted = threading.Event(), threading.Event(), threading.Event(), threading.Event()
        self.release_events.append(release)
        guard = threading.Lock()
        active = peak = calls = 0
        def hook(*args):
            nonlocal active, peak, calls
            with guard:
                calls += 1
                ordinal = calls
                active += 1
                peak = max(peak, active)
            try:
                if ordinal == 1:
                    entered.set()
                    if not release.wait(5): raise AssertionError('Test did not release the first extraction')
                else: second_started.set()
                return {'entries': []}
            finally:
                with guard: active -= 1
        self.app.config['YOUTUBE_EXTRACT'] = hook
        with patch.object(fallback.time, 'sleep', return_value=None):
            _, first = self.thread(self.foreground)
            self.assertTrue(entered.wait(2))
            def request_second():
                second_attempted.set()
                return self.foreground()
            _, second = self.thread(request_second)
            self.assertTrue(second_attempted.wait(2))
            self.assertFalse(second_started.wait(.15), 'the second foreground request must wait for its own single slot')
            release.set()
            self.assertEqual(first(), {'entries': []})
            self.assertEqual(second(), {'entries': []})
        self.assertEqual(calls, 2)
        self.assertEqual(peak, 1)
        self.assertFalse(fallback._foreground_lock.locked())

    def test_banner_and_ordinary_video_calls_still_use_the_original_lock(self):
        for url, mode, public in [(CHANNEL, 'channel', True), (VIDEO, 'video', False)]:
            with self.subTest(mode=mode):
                called = threading.Event()
                self.app.config['YOUTUBE_EXTRACT'] = lambda *args: (called.set(), {})[1]
                self.store.set('youtube_fallback_next', '0')
                fallback._lock.acquire()
                try:
                    _, finish = self.thread(lambda: fallback.extract(self.importer, url, mode, public=public, timeout=30))
                    self.assertFalse(called.wait(.15), 'ordinary/channel work must not bypass the original lock')
                finally:
                    fallback._lock.release()
                self.assertEqual(finish(), {})
                self.assertTrue(called.is_set())

    def test_foreground_whitelist_rejects_cookie_modes_arbitrary_lists_and_large_windows_before_hook(self):
        hook = Mock(return_value={})
        self.app.config['YOUTUBE_EXTRACT'] = hook
        cases = [(UPLOADS, 'playlist', 1, 26, False), (VIDEO, 'video', 1, 1, True),
                 (CHANNEL, 'channel', 1, 1, True), ('https://www.youtube.com/feed/channels', 'subscriptions', 1, 1, True),
                 ('https://www.youtube.com/playlist?list=PL'+'a'*22, 'playlist', 1, 26, True),
                 ('https://other.invalid/playlist?list=UU'+'a'*22, 'playlist', 1, 26, True),
                 (UPLOADS, 'playlist', 0, 25, True), (UPLOADS, 'playlist', 2, 1, True),
                 (UPLOADS, 'playlist', 1, 27, True), (UPLOADS, 'playlist', True, 26, True),
                 (UPLOADS, 'playlist', 1, 26.0, True)]
        for url, mode, start, end, public in cases:
            with self.subTest(mode=mode, start=start, end=end, public=public):
                with self.assertRaises(ValueError):
                    fallback.extract(self.importer, url, mode, start, end, public=public, foreground=True)
        hook.assert_not_called()
        self.assertEqual(self.store.get('youtube_upload_browse_next'), '')

    def test_real_public_runner_is_cookie_free_even_with_a_saved_cookie_file(self):
        self.app.testing = False
        cookie = self.store.directory/'youtube-cookies.txt'
        cookie.write_text('# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t4102444800\tSID\tfixture-only\n')
        before = cookie.read_bytes()
        seen = []
        def runner(args, *, input, stdout, **options):
            seen.append((json.loads(input), options['timeout']))
            stdout.write(b'{"entries":[]}')
        with patch('backend.watched_status.cookie_session_file_status', side_effect=AssertionError('Public work must not inspect cookies')), \
             patch.object(fallback, 'run_extractor', side_effect=runner):
            self.assertEqual(self.foreground(), {'entries': []})
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], {'url': UPLOADS, 'mode': 'playlist', 'start': 1, 'end': 26})
        self.assertNotIn('cookie', seen[0][0])
        self.assertGreater(seen[0][1], 0)
        self.assertLessEqual(seen[0][1], 30)
        self.assertEqual(cookie.read_bytes(), before)

    def test_pause_and_reset_prevent_foreground_hook_without_advancing_pacing(self):
        hook = Mock(return_value={})
        self.app.config['YOUTUBE_EXTRACT'] = hook
        for key, value in [('youtube_sync_paused', 'true'), ('reset_in_progress', '1')]:
            with self.subTest(gate=key):
                self.store.set(key, value)
                try:
                    with self.assertRaises(ValueError): self.foreground()
                    self.assertEqual(self.store.get('youtube_upload_browse_next'), '')
                    self.assertFalse(fallback._foreground_lock.locked())
                finally:
                    self.store.set(key, 'false' if key == 'youtube_sync_paused' else '0')
        hook.assert_not_called()

    def test_thirty_second_total_budget_includes_lock_wait_before_runner(self):
        self.app.testing = False
        slot = Mock()
        slot.acquire.return_value = True
        seen = []
        def runner(args, *, input, stdout, **options):
            seen.append(options['timeout'])
            stdout.write(b'{"entries":[]}')
        with patch.object(fallback, '_foreground_lock', slot), \
             patch.object(fallback.time, 'monotonic', side_effect=[100, 129, 129.25, 129.5]), \
             patch.object(fallback, 'run_extractor', side_effect=runner):
            self.assertEqual(fallback.extract(self.importer, UPLOADS, 'playlist', 1, 26,
                                             public=True, foreground=True, timeout=900), {'entries': []})
        self.assertEqual(seen, [.5], 'waiting for the slot does not create a fresh subprocess timeout')
        slot.acquire.assert_called_once_with(timeout=.5)
        slot.release.assert_called_once()

    def test_exhausted_slot_or_pacing_budget_never_enters_the_extractor_hook(self):
        hook = Mock(return_value={})
        self.app.config['YOUTUBE_EXTRACT'] = hook
        slot = Mock()
        slot.acquire.return_value = False
        with patch.object(fallback, '_foreground_lock', slot), \
             patch.object(fallback.time, 'monotonic', side_effect=[100, 100, 130]):
            with self.assertRaises(ValueError): self.foreground()
        hook.assert_not_called()
        slot.release.assert_not_called()
        slot.acquire.return_value = True
        self.store.set('youtube_upload_browse_next', '1100')
        with patch.object(fallback, '_foreground_lock', slot), \
             patch.object(fallback.time, 'time', return_value=1000), \
             patch.object(fallback.time, 'monotonic', side_effect=[100, 100, 129, 130]), \
             patch.object(fallback.time, 'sleep') as sleep:
            with self.assertRaises(ValueError): self.foreground()
        sleep.assert_called_once_with(1)
        hook.assert_not_called()


if __name__ == '__main__': unittest.main()
