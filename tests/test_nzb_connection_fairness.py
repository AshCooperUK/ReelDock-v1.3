"""Article-boundary fairness uses real pull loops and no provider connections."""
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.downloaders.nzb.engine import NzbEngine, _ConnectionBudget, _Job, _PullState, articles_on_server
from backend.downloaders.nzb.nntp import NntpError
from backend.downloaders.nzb.yenc import encode


SERVER = dict(host='fixture.invalid', port=563, ssl=True, username='fixture', password='test-only', connections=60)
RAW = b'<nzb><file subject="Fixture.mkv"><groups><group>fixture</group></groups><segments><segment bytes="5" number="1">one@fixture</segment></segments></file></nzb>'


class PullHarness:
    """A BODY call waits for its own article, rather than a timing-based sleep."""
    def __init__(self, engine):
        self.engine = engine
        self.cond = threading.Condition()
        self.clients = set()
        self.calls = {}
        self.jobs = {}
        self.threads = []
        self.errors = []
        self.writes = []
        self.peak = 0
        self.created = []
        harness = self

        class Client:
            def __init__(self, *args, **kwargs):
                self.owner = None
                self.checkpoint = None
                with harness.cond:
                    harness.created.append((args, kwargs))

            def body(self, message_id, groups):
                owner = message_id.split(':', 1)[0]
                done = threading.Event()
                with harness.cond:
                    self.owner = owner
                    harness.clients.add(self)
                    harness.calls[done] = owner
                    harness.peak = max(harness.peak, len(harness.clients))
                    harness.cond.notify_all()
                try:
                    while not done.wait(.02):
                        if harness.engine.stop.is_set() or harness.jobs[owner].removed or harness.jobs[owner].user_paused:
                            raise NntpError(0, 'Download stopped.')
                    return encode(b'video', 'Fixture.mkv')
                finally:
                    with harness.cond:
                        harness.calls.pop(done, None)
                        harness.cond.notify_all()

            def close(self):
                with harness.cond:
                    harness.clients.discard(self)
                    harness.cond.notify_all()

        self.client_patch = patch('backend.downloaders.nzb.engine.NntpClient', Client)
        self.write_patch = patch.object(engine, '_write_part', side_effect=self.write)

    def write(self, job, segment, *_args):
        with self.cond:
            self.writes.append((job.id, segment.number))

    def __enter__(self):
        self.client_patch.start()
        self.write_patch.start()
        return self

    def __exit__(self, *_args):
        self.engine.stop.set()
        self.release()
        for thread in self.threads:
            thread.join(4)
        self.write_patch.stop()
        self.client_patch.stop()

    def start(self, name, count=400, limit=60, server=None):
        job = _Job(name, 'Fixture', [], set())
        self.jobs[name] = job
        state = _PullState()
        pending = [SimpleNamespace(number=n, size=5, message_id=f'{name}:{n}', groups=('fixture',), file_index=0) for n in range(1, count + 1)]
        def fetch():
            try:
                self.engine._fetch_server(job, dict(server or SERVER, connections=limit), pending, state)
            except BaseException as exc:
                self.errors.append(exc)
        thread = threading.Thread(target=fetch)
        self.threads.append(thread)
        thread.start()
        return job, state, thread

    def counts(self):
        with self.cond:
            return dict(Counter(client.owner for client in self.clients))

    def wait(self, predicate, message, timeout=4):
        deadline = time.monotonic() + timeout
        with self.cond:
            while not predicate():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f'{message}: {self.counts()}, errors={self.errors}')
                self.cond.wait(min(.05, remaining))

    def release(self, owner=None):
        with self.cond:
            current = [event for event, name in self.calls.items() if owner is None or owner == name]
        for event in current:
            event.set()


class NzbConnectionFairnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        spawn = patch.object(NzbEngine, '_spawn')
        spawn.start()
        self.addCleanup(spawn.stop)
        self.engine = NzbEngine(Path(self.temp.name) / 'media', Path(self.temp.name) / 'data')
        self.addCleanup(self.engine.shutdown)

    def assert_released(self, harness):
        self.assertFalse(harness.errors)
        self.assertFalse(any(thread.is_alive() for thread in harness.threads))
        self.assertEqual(self.engine._slots._used, 0)
        self.assertEqual(self.engine._slots._groups, {})
        self.assertEqual(self.engine._slots._leases, {})
        self.assertEqual(harness.counts(), {})

    def test_late_second_download_splits_sixty_and_completion_restores_sixty(self):
        with PullHarness(self.engine) as h:
            first, _, _ = h.start('first')
            h.wait(lambda: h.counts() == {'first': 60}, 'first download did not fill its allowance')
            original = set(h.clients)
            second, _, thread = h.start('second', count=30)
            h.wait(lambda: len(self.engine._slots._groups) == 2, 'second group did not register')
            self.assertEqual(h.counts(), {'first': 60}, 'in-flight sockets must not be abandoned')
            h.release('first')
            h.wait(lambda: h.counts() == {'first': 30, 'second': 30}, 'late job never received half the sockets')
            self.assertEqual(first.done_parts, 60)
            self.assertEqual(len(original.intersection(h.clients)), 30, 'in-quota clients should stay connected')
            # No spare socket may be opened for a preview while all 60 are busy.
            self.assertEqual(articles_on_server([SERVER], RAW, budget=self.engine._slots, stop=self.engine.stop), 'unchecked')
            h.release('second')
            thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertEqual(second.done_parts, 30)
            h.wait(lambda: h.counts() == {'first': 60}, 'completed job retained capacity')
            self.assertEqual(h.peak, 60)
            self.assertEqual(len(h.writes), len(set(h.writes)), 'rebalance repeated an article')
            # Endpoint, TLS and account details are preserved on every new lease.
            self.assertTrue(all(args == ('fixture.invalid', 563, True, 'fixture', 'test-only') for args, _ in h.created))
        self.assert_released(h)

    def test_three_downloads_receive_twenty_each_and_cancel_rebalances(self):
        with PullHarness(self.engine) as h:
            h.start('first')
            h.wait(lambda: h.counts() == {'first': 60}, 'first fill')
            h.start('second')
            h.wait(lambda: len(self.engine._slots._groups) == 2, 'second registration')
            h.release('first')
            h.wait(lambda: h.counts() == {'first': 30, 'second': 30}, 'two-way share')
            third, _, thread = h.start('third')
            h.wait(lambda: len(self.engine._slots._groups) == 3, 'third registration')
            h.release()
            h.wait(lambda: h.counts() == {'first': 20, 'second': 20, 'third': 20}, 'three-way share')
            third.removed = True
            thread.join(3)
            self.assertFalse(thread.is_alive())
            h.wait(lambda: h.counts() == {'first': 30, 'second': 30}, 'cancelled group retained slots')
            self.assertEqual(h.peak, 60)
        self.assert_released(h)

    def test_one_socket_rotates_between_jobs_at_article_boundaries(self):
        with PullHarness(self.engine) as h:
            a, _, _ = h.start('first', limit=1)
            h.wait(lambda: h.counts() == {'first': 1}, 'first single socket')
            b, _, _ = h.start('second', limit=1)
            h.wait(lambda: len(self.engine._slots._groups) == 2, 'second registration')
            for owner, next_owner in [('first', 'second'), ('second', 'first')] * 3:
                h.release(owner)
                h.wait(lambda: h.counts() == {next_owner: 1}, 'single socket did not rotate')
            self.assertEqual((a.done_parts, b.done_parts), (3, 3))
            self.assertEqual(h.peak, 1)
            self.assertEqual(len(h.writes), 6)
        self.assert_released(h)

    def test_fewer_sockets_than_jobs_and_odd_remainders_do_not_starve(self):
        for limit in (2, 5):
            with self.subTest(limit=limit):
                self.engine.stop.clear()
                with PullHarness(self.engine) as h:
                    for owner in ('first', 'second', 'third'):
                        h.start(owner, limit=limit)
                    h.wait(lambda: len(self.engine._slots._groups) == 3 and sum(h.counts().values()) == limit, 'initial limited pool')
                    for _ in range(12):
                        h.release()
                        # Every wave is held at its next BODY, avoiding sleeps
                        # that would let the fastest thread monopolise a test.
                        h.wait(lambda: len(h.calls) == limit and sum(job.done_parts for job in h.jobs.values()) > 0, 'next wave')
                        if all(job.done_parts >= 2 for job in h.jobs.values()):
                            break
                        time.sleep(.02)
                    self.assertTrue(all(job.done_parts >= 2 for job in h.jobs.values()), {key: job.done_parts for key, job in h.jobs.items()})
                    self.assertLessEqual(h.peak, limit)
                    if limit == 5:
                        self.assertTrue(all(count >= 1 for count in h.counts().values()))
                self.assert_released(h)

    def test_paused_and_failed_groups_free_their_share_without_new_articles(self):
        for mode in ('pause', 'failure'):
            with self.subTest(mode=mode):
                self.engine.stop.clear()
                with PullHarness(self.engine) as h:
                    h.start('first', limit=4)
                    h.wait(lambda: h.counts() == {'first': 4}, 'first fill')
                    b, state, thread = h.start('second', limit=4)
                    h.wait(lambda: len(self.engine._slots._groups) == 2, 'second registration')
                    h.release('first')
                    h.wait(lambda: h.counts() == {'first': 2, 'second': 2}, 'two shares')
                    if mode == 'pause':
                        b.user_paused = True
                    else:
                        b.fetch_error = 'Fixture authentication refused.'
                        h.release('second')
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
                    h.wait(lambda: h.counts() == {'first': 4}, 'inactive group retained quota')
                    self.assertEqual(state.inflight, 0)
                self.assert_released(h)

    def test_reduced_limit_yields_after_current_articles_without_retries(self):
        with PullHarness(self.engine) as h:
            a, state, _ = h.start('first', limit=6)
            h.wait(lambda: h.counts() == {'first': 6}, 'initial six')
            self.engine._slots.set_limit(2)
            self.assertEqual(h.counts(), {'first': 6})
            h.release()
            h.wait(lambda: h.counts() == {'first': 2}, 'lower total was not enforced at boundaries')
            self.assertEqual(a.done_parts, 6)
            self.assertEqual(state.attempts, {})
        self.assert_released(h)

    def test_cancellation_callback_is_never_called_inside_allocator_condition(self):
        budget = _ConnectionBudget()
        budget.set_limit(1)
        group = object()
        budget.register(group, dict(SERVER, connections=1), lambda: 2)
        checked = []
        def allowed():
            acquired = threading.Event()
            def inspect():
                with budget._cond:
                    acquired.set()
            thread = threading.Thread(target=inspect)
            thread.start()
            checked.append(acquired.wait(.5))
            thread.join(.5)
            return True
        token = budget.acquire_group(threading.Event(), group, allowed=allowed)
        self.assertEqual(checked, [True])
        budget.release_group(token)
        budget.unregister(group)
        self.assertEqual(budget._used, 0)

    def test_separate_providers_obey_both_account_and_shared_global_limits(self):
        with PullHarness(self.engine) as h:
            h.start('first', limit=2)
            h.wait(lambda: h.counts() == {'first': 2}, 'first account fill')
            h.start('second', limit=2)
            h.wait(lambda: len(self.engine._slots._groups) == 2, 'same-account registration')
            h.release()
            h.wait(lambda: h.counts() == {'first': 1, 'second': 1}, 'same account should share two')
            h.start('other', limit=6, server=dict(SERVER, host='other.fixture.invalid'))
            h.wait(lambda: h.counts() == {'first': 1, 'second': 1, 'other': 4}, 'second account should use remaining global capacity')
            self.assertEqual(h.peak, 6)
        self.assert_released(h)

    def test_cancelled_zero_quota_job_unregisters_without_opening_socket(self):
        with PullHarness(self.engine) as h:
            h.start('first', limit=1)
            h.wait(lambda: h.counts() == {'first': 1}, 'first single socket')
            waiting, _, thread = h.start('waiting', limit=1)
            h.wait(lambda: len(self.engine._slots._groups) == 2, 'waiter registration')
            waiting.removed = True
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(h.created), 1)
            self.assertEqual(len(self.engine._slots._groups), 1)
            self.assertEqual(h.counts(), {'first': 1})
            # Its departure must not leave the remainder assigned to a ghost.
            h.release('first')
            h.wait(lambda: h.jobs['first'].done_parts == 1 and len(h.calls) == 1, 'remaining job stalled behind cancelled group')
            self.assertEqual(len(h.created), 1, 'sole remaining socket need not reconnect')
        self.assert_released(h)

    def test_shutdown_releases_live_and_waiting_groups(self):
        with PullHarness(self.engine) as h:
            h.start('first', limit=1)
            h.wait(lambda: h.counts() == {'first': 1}, 'initial socket')
            h.start('waiting', limit=1)
            h.wait(lambda: len(self.engine._slots._groups) == 2, 'waiter registration')
            self.engine.stop.set()
            for thread in h.threads:
                thread.join(2)
            self.assertEqual(len(h.created), 1)
        self.assert_released(h)

    def test_authentication_and_missing_article_stop_release_all_group_leases(self):
        for code in (481, 430):
            with self.subTest(code=code):
                self.engine.stop.clear()
                created = []
                closed = []
                class Client:
                    def __init__(self, *args, **kwargs):
                        self.checkpoint = None
                        created.append(self)
                    def body(self, *_args):
                        raise NntpError(code, 'Fixture authentication rejected.' if code == 481 else 'Article not available.')
                    def close(self):
                        closed.append(self)
                job = _Job('failed', 'Fixture', [], set())
                state = _PullState()
                pending = [SimpleNamespace(number=n, message_id=f'failed:{n}', groups=('fixture',), file_index=0) for n in range(100)]
                with patch('backend.downloaders.nzb.engine.NntpClient', Client):
                    self.engine._fetch_server(job, SERVER, pending, state)
                self.assertEqual(Counter(created), Counter(closed))
                self.assertEqual(self.engine._slots._used, 0)
                self.assertEqual(self.engine._slots._groups, {})
                self.assertEqual(self.engine._slots._leases, {})
                self.assertEqual(state.inflight, 0)
                if code == 481:
                    self.assertTrue(job.fetch_error)
                else:
                    self.assertTrue(state.stop)
                    self.assertGreaterEqual(state.misses, 40)


if __name__ == '__main__':
    unittest.main()
