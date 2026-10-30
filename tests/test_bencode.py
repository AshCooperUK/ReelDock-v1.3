import hashlib
import unittest

from backend.downloaders.torrent.bencode import BencodeError, bdecode_all, bencode, raw_value
from backend.downloaders.torrent.metainfo import parse_torrent

from tests.torrentutil import build_torrent


class BencodeTests(unittest.TestCase):
    def test_round_trip_and_sorted_keys(self) -> None:
        encoded = bencode({b"b": 1, b"a": [b"x", 2], b"c": {b"z": b"q", b"y": -3}})
        self.assertEqual(encoded, b"d1:al1:xi2ee1:bi1e1:cd1:yi-3e1:z1:qee")
        self.assertEqual(bdecode_all(encoded), {b"b": 1, b"a": [b"x", 2], b"c": {b"z": b"q", b"y": -3}})

    def test_rejects_bad_ints(self) -> None:
        for raw in (b"i03e", b"i-0e", b"ie", b"i-e"):
            with self.assertRaises(BencodeError):
                bdecode_all(raw)

    def test_info_hash_uses_raw_info_bytes(self) -> None:
        blob = build_torrent([("clip.mp4", b"v" * 50)])
        info = raw_value(blob, b"info")
        meta = parse_torrent(blob)
        self.assertEqual(meta.info_hash, hashlib.sha1(info).digest())
        self.assertEqual(meta.files[0].path, "clip.mp4")
        self.assertEqual(meta.total_length, 50)

    def test_trackers_and_web_seeds(self) -> None:
        blob = build_torrent(
            [("a.mp4", b"a"), ("b.srt", b"b")],
            name="Show",
            trackers=["http://tracker.example/announce", "udp://tracker.example:1337", "wss://example/ws"],
            url_list=["https://mirror.example/files/"],
        )
        meta = parse_torrent(blob)
        self.assertTrue(meta.multi)
        self.assertEqual(meta.files[1].path, "b.srt")
        self.assertIn("wss://example/ws", meta.trackers)
        self.assertEqual(meta.url_list, ["https://mirror.example/files/"])


if __name__ == "__main__":
    unittest.main()
