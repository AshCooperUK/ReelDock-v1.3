"""Payload bytes stay off disk until a media scan has passed and the user starts."""

from __future__ import annotations

import hashlib
import shutil
import socket
import struct
import time
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from backend.downloaders.torrent.engine import TorrentEngine
from backend.operations import Operations

from tests.torrentutil import QUIET_SETTINGS, build_torrent


class RangeServer(ThreadingHTTPServer):
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.hits = {key: 0 for key in files}
        super().__init__(("127.0.0.1", 0), _Handler)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        path = urllib.parse.unquote(self.path.split("?", 1)[0])
        files = self.server.files
        if path not in files:
            self.send_error(404)
            return
        self.server.hits[path] = self.server.hits.get(path, 0) + 1
        payload = files[path]
        start, end = 0, len(payload) - 1
        header = self.headers.get("Range")
        status = 200
        if header and header.startswith("bytes="):
            spec = header.split("=", 1)[1]
            left, _, right = spec.partition("-")
            start = int(left or 0)
            end = int(right) if right else len(payload) - 1
            end = min(end, len(payload) - 1)
            status = 206
        chunk = payload[start : end + 1]
        self.send_response(status)
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Content-Type", "application/octet-stream")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
        self.end_headers()
        self.wfile.write(chunk)


class PayloadPeer:
    """Independent local seed fixture; ReelDock itself remains download-only."""
    def __init__(self, meta, payload):
        self.stop=threading.Event();self.requests=0;self.error=None
        self.sock=socket.socket();self.sock.bind(('127.0.0.1',0));self.sock.listen();self.sock.settimeout(.2)
        self.port=self.sock.getsockname()[1]
        def serve():
            from backend.downloaders.torrent.wire import open_inbound, pack_message, bitfield_bytes, WireError
            try:
                while not self.stop.is_set():
                    try:connection,_=self.sock.accept()
                    except socket.timeout:continue
                    with connection:
                        stream,_,_=open_inbound(connection,self.stop,b'fixture-peer-0000001','disabled',lambda identity:True if identity==meta.info_hash else None)
                        stream.send(pack_message(5,bitfield_bytes([True]*meta.piece_count,meta.piece_count)))
                        stream.send(pack_message(1))
                        while not self.stop.is_set():
                            message=stream.read_message(time.monotonic()+10)
                            if not message:continue
                            kind,body=message
                            if kind==6:
                                index,begin,length=struct.unpack('>III',body)
                                offset=index*meta.piece_length+begin
                                stream.send(pack_message(7,struct.pack('>II',index,begin)+payload[offset:offset+length]))
                                self.requests+=1
            except (OSError,WireError) as exc:
                if not self.stop.is_set():self.error=str(exc)
        self.thread=threading.Thread(target=serve,daemon=True);self.thread.start()

    def close(self):
        self.stop.set();self.sock.close();self.thread.join(2)


class MetadataBeforePayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.id().split(".")[-1] + "-data")
        if self.tmp.exists():
            shutil.rmtree(self.tmp)
        self.media = self.tmp / "media"
        self.data = self.tmp / "app"
        self.media.mkdir(parents=True)
        self.engine = TorrentEngine(
            self.media,
            self.data,
            Operations(str(self.data / "operations.lock")),
            dict(QUIET_SETTINGS),
        )

    def tearDown(self) -> None:
        self.engine.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rejected_torrent_never_creates_payload(self) -> None:
        blob = build_torrent([("readme.pdf", b"doc"), ("run.exe", b"MZ" * 40), ("disk.iso", b"cd")])
        preview = self.engine.preview_bytes(blob)
        self.assertEqual(preview["status"], "rejected")
        self.assertFalse(preview["accepted"])
        with self.assertRaises(Exception):
            self.engine.start(preview["id"], [0])
        self.assertEqual(list(self.media.rglob("*")), [])

    def test_preview_fetches_metadata_only_then_downloads_media(self) -> None:
        video = b"v" * 2048
        subtitle = b"1\n00:00:00,000 --> 00:00:01,000\nHi\n"
        poster = b"\xff\xd8" + b"p" * 40
        executable = b"e" * 1024
        files = {
            "note.exe": executable,
            "Big Buck Bunny.mp4": video,
            "Big Buck Bunny.en.srt": subtitle,
            "poster.jpg": poster,
        }
        # The executable occupies its own piece so a media download never asks for it.
        ordered = [
            ("note.exe", executable),
            ("Big Buck Bunny 2008.mp4", video),
            ("Big Buck Bunny.en.srt", subtitle),
            ("poster.jpg", poster),
        ]
        server = RangeServer({})
        port = server.server_address[1]
        base = f"http://127.0.0.1:{port}/torrents/"
        blob = build_torrent(
            ordered,
            name="Big Buck Bunny",
            piece_length=1024,
            url_list=[base],
            trackers=["udp://127.0.0.1:9/announce", "wss://tracker.example/socket"],
        )
        served = {
            "/sample.torrent": blob,
            "/torrents/Big Buck Bunny/note.exe": executable,
            "/torrents/Big Buck Bunny/Big Buck Bunny 2008.mp4": video,
            "/torrents/Big Buck Bunny/Big Buck Bunny.en.srt": subtitle,
            "/torrents/Big Buck Bunny/poster.jpg": poster,
        }
        server.files = served
        server.hits = {key: 0 for key in served}
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            preview = self.engine.preview(f"http://127.0.0.1:{port}/sample.torrent")
            self.assertEqual(preview["status"], "ready")
            self.assertTrue(preview["accepted"])
            self.assertIn("wss://tracker.example/socket", preview["skipped_trackers"])
            selected = [item["path"] for item in preview["files"] if item["selected"]]
            self.assertIn("Big Buck Bunny 2008.mp4", selected)
            self.assertNotIn("note.exe", selected)
            self.assertEqual(server.hits["/sample.torrent"], 1)
            self.assertEqual(server.hits["/torrents/Big Buck Bunny/Big Buck Bunny 2008.mp4"], 0)
            self.assertEqual(server.hits["/torrents/Big Buck Bunny/note.exe"], 0)
            self.assertFalse(any(path.name == "note.exe" for path in self.media.rglob("*")))

            job = self.engine.start(preview["id"], None)
            done = self.engine.wait_job(job["id"], timeout=8)
        finally:
            server.shutdown()
        self.assertGreaterEqual(done["progress"], 1)
        self.assertEqual(server.hits["/torrents/Big Buck Bunny/note.exe"], 0)
        self.assertGreater(server.hits["/torrents/Big Buck Bunny/Big Buck Bunny 2008.mp4"], 0)
        library = self.media / "Movies" / "Big Buck Bunny (2008)" / "Big Buck Bunny (2008).mp4"
        self.assertTrue(library.is_file())
        self.assertFalse(library.is_symlink())
        self.assertEqual(library.read_bytes(), video)
        self.assertEqual(hashlib.sha1(library.read_bytes()).digest(), hashlib.sha1(video).digest())
        self.assertTrue((self.media / "Movies" / "Big Buck Bunny (2008)" / "Big Buck Bunny (2008).en.srt").is_file())
        self.assertTrue((self.media / "Movies" / "Big Buck Bunny (2008)" / "poster.jpg").is_file())
        self.assertFalse(any(path.name == "note.exe" for path in self.media.rglob("*")))
        self.assertFalse((self.media / "Downloads" / "YouTube").exists())

    def test_peer_transfer_and_magnet_metadata_do_not_download_early(self) -> None:
        payload = b"p" * 3000
        blob = build_torrent(
            [("Clip.mp4", payload)],
            piece_length=1024,
            url_list=["http://127.0.0.1:9/missing/"],
        )
        from backend.downloaders.torrent.metainfo import parse_torrent

        meta = parse_torrent(blob)
        server = RangeServer({"/clip": payload})
        # Serve the single-file web seed from a URL that does not end in a slash.
        web = f"http://127.0.0.1:{server.server_address[1]}/clip"
        blob = build_torrent([("Clip.mp4", payload)], piece_length=1024, url_list=[web])
        meta = parse_torrent(blob)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        other_media = self.tmp / "other-media"
        other_data = self.tmp / "other-app"
        other = TorrentEngine(other_media, other_data, None, dict(QUIET_SETTINGS))
        peer = PayloadPeer(meta,payload)
        try:
            preview = self.engine.preview_bytes(blob)
            job = self.engine.start(preview["id"], None)
            self.engine.wait_job(job["id"], timeout=8)
            self.assertGreaterEqual(self.engine.job(job["id"])["uploaded"], 0)

            magnet = f"magnet:?xt=urn:btih:{meta.info_hash.hex()}&dn=Clip"
            pending = other.preview(magnet)
            self.assertEqual(pending["status"], "reading-metadata")
            other.inject_peer(pending["id"], "127.0.0.1", self.engine.port)
            ready = other.wait_preview(pending["id"], timeout=5)
            self.assertEqual(ready["status"], "ready", ready.get("reason"))
            seeder = self.engine.job(job["id"])
            self.assertEqual(self._requests(job["id"]), 0)
            self.assertFalse(other_media.exists() and any(other_media.rglob("*")))
            started = other.start(pending["id"], None)
            other.inject_peer(started["id"], "127.0.0.1", peer.port)
            finished = other.wait_job(started["id"], timeout=8)
            self.assertGreaterEqual(finished["progress"], 1)
            self.assertGreater(peer.requests, 0)
            self.assertEqual(self._requests(job["id"]), 0)
            self.assertEqual(self.engine.job(job["id"])["uploaded"], 0)
            copies = [path for path in other_media.rglob("Clip.mp4") if path.is_file()]
            self.assertEqual(len(copies), 1)
            self.assertEqual(copies[0].read_bytes(), payload)
        finally:
            other.shutdown()
            peer.close()
            server.shutdown()

    def _requests(self, job_id: str) -> int:
        return self.engine._jobs[job_id].payload_requests_received

    def test_halt_stops_work_and_leaves_library_files(self) -> None:
        folder = self.media / "Downloads" / "Movies"
        folder.mkdir(parents=True)
        kept = folder / "already.mp4"
        kept.write_bytes(b"keep")
        self.engine.operations.halt()
        self.assertTrue(kept.read_bytes() == b"keep")
        self.assertTrue(self.engine.stop.is_set())


if __name__ == "__main__":
    unittest.main()
