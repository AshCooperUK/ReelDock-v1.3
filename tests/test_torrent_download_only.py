"""Hostile or old peers cannot obtain media pieces from a download-only job."""
import struct
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from backend.downloaders.torrent.wire import run_peer
from backend.downloaders.torrent.engine import Job
from backend.settings.torrent import FIELDS

class DownloadOnlyTests(unittest.TestCase):
    def test_interested_peer_and_unsolicited_request_never_receive_a_piece(self):
        stop=threading.Event();stream=Mock();storage=Mock();storage.have=[True]
        job=SimpleNamespace(info_bytes=None,listen_port=0,meta=SimpleNamespace(piece_count=1),
            storage=storage,picker=None,metadata_only=False,removed=False,io=True)
        messages=iter([(2,b''),(6,struct.pack('>III',0,0,16384))])
        def read(deadline):
            try:return next(messages)
            except StopIteration:stop.set();return None
        stream.read_message.side_effect=read
        run_peer(stream,job,'fixture',stop,b'\0'*68)
        storage.read_piece.assert_not_called()
        ids=[call.args[0][4] for call in stream.send.call_args_list]
        self.assertNotIn(7,ids) # no piece payload
        self.assertNotIn(1,ids) # no unchoke
        self.assertIn(0,ids)

    def test_old_upload_settings_cannot_enable_uploads(self):
        job=Job(SimpleNamespace(),b'x'*20)
        job.upload_limit_bps=999999
        self.assertFalse(job.take_upload(16384))
        self.assertFalse(set(FIELDS)&{'upload_limit_bps','max_active_seeds','share_ratio_limit'})
