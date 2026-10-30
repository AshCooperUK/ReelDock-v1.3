"""Independent 7z fixtures for folders with intermediate coder outputs.

These small archives use the 7z wire format directly, rather than pack_7z,
whose single payload coder could not expose a doubled decoded file size.
"""

import lzma
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from backend.downloaders.nzb.archive import ArchiveError
from backend.downloaders.nzb.finish import prepare
from backend.downloaders.nzb.rar_aes import aes256_cbc_encrypt, seven_zip_key
from backend.downloaders.nzb.seven import extract_volumes


PASSWORD = "fixture-password"


def _number(value):
    """Encode the small unsigned integers needed by these fixtures."""
    if value < 0x80:
        return bytes([value])
    if value < 0x4000:
        return bytes([0x80 | (value >> 8), value & 0xFF])
    raise ValueError("The fixture integer is too large.")


def _crc(data):
    return struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF)


def _signature(packed, header):
    start = struct.pack("<QQ", len(packed), len(header)) + _crc(header)
    return b"7z\xbc\xaf\x27\x1c\x00\x04" + _crc(start) + start + packed + header


def _pack_info(position, size):
    return b"\x06" + _number(position) + b"\x01\x09" + _number(size) + b"\x00"


def _unpack_info(coders, bind_pair, sizes, payload, folder_crc=True):
    folder = _number(len(coders)) + b"".join(coders)
    folder += _number(bind_pair[0]) + _number(bind_pair[1])
    info = b"\x07\x0b\x01\x00" + folder
    info += b"\x0c" + b"".join(_number(size) for size in sizes)
    if folder_crc:
        info += b"\x0a\x01" + _crc(payload)
    return info + b"\x00"


def _fixture(members, method="copy", reverse=False, substreams=False, encoded_header=False):
    payload = b"".join(data for _name, data in members)
    copy = b"\x01\x00"
    bind_pair = (1, 0)
    if method == "copy":
        packed = payload
        coders = [copy, copy]
        sizes = [len(payload), len(payload)]
    else:
        if method == "aes-lzma2":
            compressed = lzma.compress(
                payload, format=lzma.FORMAT_RAW,
                filters=[{"id": lzma.FILTER_LZMA2, "dict_size": 1 << 16}],
            )
            following = b"\x21\x21\x01\x08"
        else:
            compressed = payload
            following = copy
        salt, iv = b"0123456789abcdef", b"fedcba9876543210"
        cycles = 4
        props = bytes([cycles | 0xC0, 0xFF]) + salt + iv
        aes = b"\x24\x06\xf1\x07\x01" + _number(len(props)) + props
        padded = compressed + bytes(-len(compressed) % 16)
        packed = aes256_cbc_encrypt(seven_zip_key(PASSWORD, salt, cycles), iv, padded)
        coders, sizes = [aes, following], [len(compressed), len(payload)]
        if reverse:
            coders.reverse()
            sizes.reverse()
            bind_pair = (0, 1)
    multi = len(members) > 1
    streams = _pack_info(0, len(packed))
    streams += _unpack_info(coders, bind_pair, sizes, payload, folder_crc=not multi)
    if multi:
        streams += b"\x08\x0d" + _number(len(members))
        streams += b"\x09" + b"".join(_number(len(data)) for _name, data in members[:-1])
        streams += b"\x0a\x01" + b"".join(_crc(data) for _name, data in members) + b"\x00"
    elif substreams:
        # The default one substream has no explicit size property.
        streams += b"\x08\x00"
    names = b"\x00" + b"".join(name.encode("utf-16le") + b"\x00\x00" for name, _data in members)
    files = b"\x05" + _number(len(members)) + b"\x11" + _number(len(names)) + names + b"\x00"
    header = b"\x01\x04" + streams + b"\x00" + files + b"\x00"
    if encoded_header:
        descriptor = b"\x17" + _pack_info(len(packed), len(header))
        descriptor += _unpack_info([copy, copy], (1, 0), [len(header)] * 2, header) + b"\x00"
        return _signature(packed + header, descriptor)
    return _signature(packed, header)


class ChainedSevenZipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _extract(self, blob, password="", name="archive.7z"):
        source = self.root / name
        source.write_bytes(blob)
        return extract_volumes([source], self.root / (name + ".out"), password)

    def test_copy_chain_uses_final_size_without_substreams_info(self):
        payload = b"picture-with-two-copy-stages"
        files = self._extract(_fixture([("Film.mkv", payload)]))
        self.assertEqual([(path.name, path.read_bytes()) for path in files], [("Film.mkv", payload)])

    def test_copy_chain_uses_default_single_substream_size(self):
        payload = b"picture-with-default-substream"
        files = self._extract(_fixture([("Film.mkv", payload)], substreams=True))
        self.assertEqual(files[0].read_bytes(), payload)

    def test_encrypted_stored_payload_excludes_aes_block_padding(self):
        payload = b"movie whose size is not a multiple of sixteen"
        self.assertNotEqual(len(payload) % 16, 0)
        files = self._extract(_fixture([("Film.mkv", payload)], method="aes-copy"), PASSWORD)
        self.assertEqual(files[0].read_bytes(), payload)
        self.assertEqual(files[0].stat().st_size, len(payload))

    def test_final_output_need_not_be_last_coder(self):
        payload = b"compressible-movie-content\n" * 20
        files = self._extract(_fixture([("Film.mkv", payload)], method="aes-lzma2", reverse=True), PASSWORD)
        self.assertEqual(files[0].read_bytes(), payload)

    def test_solid_substream_remainder_uses_final_output_size(self):
        members = [("Film.mkv", b"solid-movie" * 5), ("Film.srt", b"subtitles\n" * 3)]
        files = self._extract(_fixture(members))
        self.assertEqual([(path.name, path.read_bytes()) for path in files], members)

    def test_chained_coders_also_work_in_encoded_header(self):
        payload = b"movie-with-encoded-header"
        files = self._extract(_fixture([("Film.mkv", payload)], encoded_header=True))
        self.assertEqual(files[0].read_bytes(), payload)

    def test_split_encrypted_archive_passes_full_prepare_without_padding(self):
        payload = b"split-encrypted-movie" * 7
        blob = _fixture([("Film.mkv", payload)], method="aes-copy")
        first, second, third = [self.root / ("release.7z." + suffix) for suffix in ("001", "002", "003")]
        first.write_bytes(blob[:41])
        second.write_bytes(blob[41:97])
        third.write_bytes(blob[97:])
        files = prepare([third, first, second], self.root / "prepared", PASSWORD)
        self.assertEqual([(path.name, path.read_bytes()) for path in files], [("Film.mkv", payload)])

    def test_corrupt_chained_payload_still_fails_crc(self):
        blob = bytearray(_fixture([("Film.mkv", b"a-video-with-a-checksum")]))
        blob[32] ^= 0x80
        with self.assertRaisesRegex(ArchiveError, "incomplete"):
            self._extract(bytes(blob))

    def test_truncated_chained_archive_still_fails(self):
        blob = _fixture([("Film.mkv", b"a-truncated-video")])
        with self.assertRaisesRegex(ArchiveError, "incomplete"):
            self._extract(blob[:-1])

    def test_wrong_encrypted_payload_password_still_fails(self):
        blob = _fixture([("Film.mkv", b"a-video-with-password")], method="aes-copy")
        with self.assertRaises(ArchiveError):
            self._extract(blob, "wrong-fixture-password")


if __name__ == "__main__":
    unittest.main()
