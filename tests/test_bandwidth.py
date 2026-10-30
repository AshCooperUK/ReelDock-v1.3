import threading
import unittest

from backend.downloaders.torrent.bandwidth import SlotJob, TokenBucket, choose_active, ratio_reached


class BandwidthTests(unittest.TestCase):
    def test_bucket_waits_until_the_clock_moves(self) -> None:
        clock = {"now": 0.0}

        def fake_clock() -> float:
            return clock["now"]

        def fake_sleep(seconds: float) -> None:
            clock["now"] += seconds

        bucket = TokenBucket(1000, clock=fake_clock, sleep=fake_sleep)
        stop = threading.Event()
        self.assertTrue(bucket.consume(1000, stop))
        self.assertTrue(bucket.consume(500, stop))
        self.assertGreaterEqual(clock["now"], 0.49)

    def test_queue_and_ratio(self) -> None:
        jobs = [
            SlotJob("a", "download", False),
            SlotJob("b", "download", False),
            SlotJob("c", "seed", False),
            SlotJob("d", "download", True),
        ]
        self.assertEqual(choose_active(jobs, 1, 1), {"a", "c"})
        self.assertFalse(ratio_reached(10, 0, 1))
        self.assertFalse(ratio_reached(10, 10, 0))
        self.assertTrue(ratio_reached(20, 10, 2))
        self.assertFalse(ratio_reached(19, 10, 2))


if __name__ == "__main__":
    unittest.main()
