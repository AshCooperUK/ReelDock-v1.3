import threading
import time
import unittest
from unittest.mock import patch
from backend.download_control import reader


class PauseReaderContentionTests(unittest.TestCase):
    def test_parallel_checkpoints_do_not_repeat_slow_database_reads(self):
        entered=threading.Event();release=threading.Event()
        def slow(store):entered.set();release.wait(2);return dict(paused=False,reason='')
        with patch('backend.download_control.pause_status',side_effect=slow) as probe:
            gate=reader(object());first=threading.Thread(target=gate);first.start()
            self.assertTrue(entered.wait(1))
            self.assertTrue(all(gate() for _ in range(100)), 'Uninitialised readers must hold work while the first check runs')
            self.assertEqual(probe.call_count,1)
            time.sleep(.25);release.set();first.join(1)
            self.assertFalse(first.is_alive())
            self.assertFalse(gate());self.assertEqual(probe.call_count,1,'Cache starts at completion of a slow check')

    def test_pause_refresh_remains_bounded_and_current(self):
        with patch('backend.download_control.pause_status',side_effect=[dict(paused=False,reason=''),dict(paused=True,reason='vpn')]) as probe:
            gate=reader(object());self.assertFalse(gate())
            time.sleep(.22);self.assertTrue(gate());self.assertEqual(probe.call_count,2)

    def test_reason_and_boolean_share_one_immutable_cached_decision(self):
        with patch('backend.download_control.pause_status',return_value=dict(paused=True,reason='vpn')) as probe:
            gate=reader(object())
            self.assertTrue(gate())
            state=gate.snapshot();self.assertEqual(state,dict(paused=True,reason='vpn'))
            state.update(paused=False,reason='downloads')
            self.assertEqual(gate.snapshot(),dict(paused=True,reason='vpn'))
            self.assertTrue(gate());self.assertEqual(probe.call_count,1)

    def test_failed_reason_read_still_holds_work_without_inventing_a_cause(self):
        with patch('backend.download_control.pause_status',side_effect=OSError('fixture unavailable')) as probe:
            gate=reader(object())
            self.assertTrue(gate());self.assertEqual(gate.snapshot(),dict(paused=True,reason=''))
            self.assertEqual(probe.call_count,1)
