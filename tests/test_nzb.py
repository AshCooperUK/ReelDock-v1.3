import os
"""ReelDock reads NZBs and downloads them over NNTP without another program."""
import json
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

from backend.app import create_app
import gzip
import io
import zipfile

from backend.downloaders.nzb.archive import ArchiveError, extract, pack_rar4, pack_rar5
from backend.downloaders.nzb.engine import NzbEngine, _place, articles_on_server
from backend.downloaders.nzb.finish import prepare, unwrap_manifest
from backend.downloaders.nzb.seven import _Cursor, extract_volumes, pack_7z
from backend.downloaders.nzb.indexers import api_base
from backend.downloaders.nzb.nzbxml import parse
from backend.downloaders.nzb.yenc import decode, encode
from backend.nzb import stop

ORIGIN = 'https://dev.example.com'


def _stored_volume(name: str, payload: bytes, unpacked: int, flags: int) -> bytes:
    """A stored RAR5 volume. flags mark whether the file continues across volumes."""
    from backend.downloaders.nzb.archive import _write_vint
    import zlib
    name_b = name.encode('utf-8')
    file_fields = b''.join([
        _write_vint(0x0004),
        _write_vint(unpacked),
        _write_vint(0),
        struct.pack('<I', zlib.crc32(payload) & 0xFFFFFFFF),
        _write_vint(0),
        _write_vint(0),
        _write_vint(len(name_b)),
        name_b,
    ])
    file_body = _write_vint(2) + _write_vint(flags) + _write_vint(len(payload)) + file_fields
    file_size = _write_vint(len(file_body))
    file_crc = struct.pack('<I', zlib.crc32(file_size + file_body) & 0xFFFFFFFF)
    main_body = _write_vint(1) + _write_vint(0)
    main_size = _write_vint(len(main_body))
    main_crc = struct.pack('<I', zlib.crc32(main_size + main_body) & 0xFFFFFFFF)
    end_body = _write_vint(5) + _write_vint(0)
    end_size = _write_vint(len(end_body))
    end_crc = struct.pack('<I', zlib.crc32(end_size + end_body) & 0xFFFFFFFF)
    return b'Rar!\x1a\x07\x01\x00' + main_crc + main_size + main_body + file_crc + file_size + file_body + payload + end_crc + end_size + end_body


def _old_stored_rar4(name: str, payload: bytes, unpacked: int, flags: int, method: int = 0x30) -> bytes:
    """RAR4 volume whose long-block flag does not insert a separate size field."""
    import zlib
    name_b = name.encode('utf-8')
    fields = struct.pack('<IIBIIBBHI', len(payload), unpacked, 0, zlib.crc32(payload) & 0xFFFFFFFF, 0, 20, method, len(name_b), 0x20) + name_b
    header_after_crc = bytes([0x74]) + struct.pack('<HH', flags | 0x8000, 7 + len(fields)) + fields
    crc = struct.pack('<H', zlib.crc32(header_after_crc) & 0xFFFF)
    archive_body = bytes([0x73]) + struct.pack('<HH', 0, 13) + b'\x00' * 6
    archive_crc = struct.pack('<H', zlib.crc32(archive_body) & 0xFFFF)
    return b'Rar!\x1a\x07\x00' + archive_crc + archive_body + crc + header_after_crc + payload


SAMPLE = Path(os.environ.get('REELDOCK_TEST_NZB_SAMPLE', str(Path(__file__).with_name('fixtures') / 'sample.nzb')))


class NzbFormatTests(unittest.TestCase):
    def test_a_full_account_waits_instead_of_rejecting_the_password(self):
        from backend.downloaders.nzb.nntp import auth_failure
        from backend.downloaders.nzb.engine import _ConnectionBudget
        self.assertIn('connections', auth_failure(502, 'Too many connections'))
        self.assertIn('password', auth_failure(481, 'Authentication failed'))
        budget = _ConnectionBudget()
        budget.set_limit(1)
        stop = threading.Event()
        self.assertTrue(budget.acquire(stop))
        started = threading.Event()
        def waiter():
            started.set()
            self.assertTrue(budget.acquire(stop))
            budget.release()
        thread = threading.Thread(target=waiter)
        thread.start()
        self.assertTrue(started.wait(1))
        time.sleep(0.05)
        self.assertTrue(thread.is_alive())
        budget.release()
        thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_yenc_round_trip_includes_escaped_bytes(self):
        original = bytes(range(256))
        decoded, name, offset = decode(encode(original, 'Film.1999.mkv'))
        self.assertEqual(decoded, original)
        self.assertEqual((name, offset), ('Film.1999.mkv', 0))

    def test_release_name_beats_an_obfuscated_inner_title(self):
        raw = b'''<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"><head>
        <meta type="name">Example.1999.1080p</meta>
        <meta type="title">abcdef0123456789.mkv</meta>
        <meta type="category">Movies &gt; HD</meta>
        <meta type="password">not-shown</meta></head>
        <file subject="&quot;Example.part01.rar&quot; yEnc (1/1)">
        <groups><group>alt.binaries.test</group></groups>
        <segments><segment bytes="10" number="1">part@test</segment></segments>
        </file></nzb>'''
        parsed = parse(raw, 'ignored.nzb')
        self.assertEqual(parsed.name, 'Example.1999.1080p')
        self.assertEqual(parsed.category, 'Movies > HD')
        self.assertEqual(parsed.password, 'not-shown')
        self.assertNotIn('not-shown', repr(parsed))
        self.assertEqual(parsed.files[0].name, 'Example.part01.rar')

    def test_an_obfuscated_episode_is_suggested_from_the_release_name(self):
        raw = b'''<nzb><head><meta type="name">Judy.Justice.S04E110.1080p.HEVC.x265-MeGusta</meta></head>
        <file subject="&quot;2VyKNP2062U1QFOXzgerWXUPhioWQc9.mkv&quot; yEnc (1/1)">
        <groups><group>alt.binaries.test</group></groups>
        <segments><segment bytes="10" number="1">part@test</segment></segments></file></nzb>'''
        folder = Path(tempfile.mkdtemp())
        engine = NzbEngine(folder / 'library', folder / 'data')
        try:
            preview = engine.preview_bytes(raw, 'Judy.Justice.S04E110.1080p.HEVC.x265-MeGusta.nzb')
        finally:
            engine.shutdown()
        self.assertEqual(preview['suggested_name'], 'Judy Justice S04E110')
        self.assertTrue(preview['accepted'])

    def test_bracketed_subject_keeps_the_real_filename(self):
        raw = b'''<nzb><file subject="[PRiVATE]-[EnCrYpTnZb]-[Film.[2026].1080p.mkv]-[1/2] - &quot;&quot; yEnc (1/1)">
        <groups><group>alt.binaries.test</group></groups>
        <segments><segment bytes="10" number="1">part@test</segment></segments>
        </file></nzb>'''
        parsed = parse(raw, 'Film.2026.nzb')
        self.assertEqual(parsed.files[0].name, 'Film.[2026].1080p.mkv')

    def test_local_sample_uses_the_release_name(self):
        if not SAMPLE.is_file():
            self.skipTest('The local sample NZB is not on this machine.')
        parsed = parse(SAMPLE.read_bytes(), SAMPLE.name)
        self.assertTrue(parsed.name.startswith('Supergirl.2026'))
        self.assertEqual(len(parsed.files), 46)
        self.assertTrue(parsed.category.startswith('Movies'))
        self.assertTrue(parsed.password)
        self.assertNotIn(parsed.password, repr(parsed))
        self.assertNotIn(parsed.inner_name, parsed.name)
        folder = Path(tempfile.mkdtemp())
        engine = NzbEngine(folder / 'library', folder / 'data')
        try:
            preview = engine.preview_bytes(SAMPLE.read_bytes(), SAMPLE.name)
        finally:
            engine.shutdown()
        shown = json.dumps(preview)
        self.assertTrue(str(preview['suggested_name']).startswith('Supergirl'))
        self.assertNotIn(parsed.inner_name, str(preview['suggested_name']))
        self.assertNotIn(parsed.password, shown)
        self.assertNotIn('archive_password', shown)
        self.assertEqual(sum(1 for item in parsed.files if item.name.endswith('.rar')), 37)

    def test_old_split_rar4_discs_are_joined(self):
        folder = Path(tempfile.mkdtemp())
        payload = b'abcdefghij'
        first = folder / 'ils-maskdc-cd1.rar'
        second = folder / 'ils-maskdc-cd1.r00'
        first.write_bytes(_old_stored_rar4('ils-maskdc-cd1.avi', payload[:4], len(payload), 0x0002))
        second.write_bytes(_old_stored_rar4('ils-maskdc-cd1.avi', payload[4:], len(payload), 0x0003))
        saved = extract([second, first], folder / 'out')
        self.assertEqual(saved[0].name, 'ils-maskdc-cd1.avi')
        self.assertEqual(saved[0].read_bytes(), payload)
        packed = folder / 'compressed.rar'
        packed.write_bytes(_old_stored_rar4('ils-maskdc-cd1.avi', b'not-video', 40, 0, method=0x33))
        with self.assertRaises(ArchiveError) as caught:
            extract([packed], folder / 'nope')
        self.assertIn('compressed', str(caught.exception))

    def test_hash_named_rar4_volumes_join_by_split_member(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            payload = b'one-complete-video'
            paths = [folder / ('a' * 64 + '.rar'), folder / ('b' * 64 + '.r00'), folder / ('c' * 64 + '.r01')]
            for path, chunk, flags in zip(paths, [payload[:4], payload[4:8], payload[8:]], [2, 3, 1]):
                path.write_bytes(_old_stored_rar4('Movie.mkv', chunk, len(payload), flags))
            saved = prepare(list(reversed(paths)), folder / 'out')
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].read_bytes(), payload)
            again = prepare(paths, folder / 'out')
            self.assertNotEqual(saved[0], again[0])
            self.assertEqual(again[0].read_bytes(), payload)
            self.assertEqual(saved[0].read_bytes(), payload)

    def test_distinct_archives_same_member_never_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            paths = []
            for index in range(4):
                path = folder / f'set{index}.rar'
                path.write_bytes(pack_rar4('Movie.mkv', bytes([index])))
                paths.append(path)
            saved = prepare(paths, folder / 'out')
            self.assertEqual(len(set(saved)), 4)
            self.assertEqual([p.read_bytes() for p in saved], [bytes([i]) for i in range(4)])

    def test_stored_rar_round_trip(self):
        folder = Path(tempfile.mkdtemp())
        archive = folder / 'film.rar'
        archive.write_bytes(pack_rar4('Film.1999.mkv', b'picture'))
        saved = extract([archive], folder / 'out')
        self.assertEqual(saved[0].read_bytes(), b'picture')
        locked = folder / 'locked.rar'
        locked.write_bytes(pack_rar5('Film.1999.mkv', b'secret-picture', 'archive-secret'))
        opened = extract([locked], folder / 'opened', 'archive-secret')
        self.assertEqual(opened[0].read_bytes(), b'secret-picture')
        with self.assertRaises(ArchiveError):
            extract([locked], folder / 'closed')

    def test_padding_after_the_rar_end_marker_is_not_a_broken_archive(self):
        folder = Path(tempfile.mkdtemp())
        archive = folder / 'Film.part01.rar'
        archive.write_bytes(pack_rar5('Film.1999.mkv', b'stored-video') + b'\x00' * 7)
        saved = extract([archive], folder / 'out')
        self.assertEqual(saved[0].read_bytes(), b'stored-video')

    def test_split_rar_volumes_are_joined_in_part_order(self):
        folder = Path(tempfile.mkdtemp())
        payload = b'abcdefghij'
        first = folder / 'Show.mkv.part01.rar'
        second = folder / 'Show.mkv.part02.rar'
        last = folder / 'Show.mkv.part10.rar'
        first.write_bytes(_stored_volume('Show.mkv', payload[:4], len(payload), 0x0012) + b'\x00' * 7)
        second.write_bytes(_stored_volume('Show.mkv', payload[4:8], len(payload), 0x001A))
        last.write_bytes(_stored_volume('Show.mkv', payload[8:], len(payload), 0x000A))
        saved = extract([last, second, first], folder / 'out')
        self.assertEqual(saved[0].read_bytes(), payload)

    def test_a_repeated_assemble_prefix_does_not_split_the_archive(self):
        folder = Path(tempfile.mkdtemp())
        payload = b'abcdefghij'
        (folder / '0001-Show.mkv.part01.rar').write_bytes(_stored_volume('Show.mkv', payload[:4], len(payload), 0x0012))
        (folder / '0002-Show.mkv.part02.rar').write_bytes(_stored_volume('Show.mkv', payload[4:], len(payload), 0x000A))
        (folder / 'Show.mkv.part01.rar').write_bytes(_stored_volume('Show.mkv', payload[:4], len(payload), 0x0012))
        saved = extract(sorted(folder.glob('*.rar')), folder / 'out')
        self.assertEqual(saved[0].read_bytes(), payload)

    def test_seven_zip_copy_lzma2_and_password(self):
        self.assertEqual(_Cursor(bytes.fromhex('f1e01ea584')).number(), 6520381152)
        folder = Path(tempfile.mkdtemp())
        copy = folder / 'film.7z'
        copy.write_bytes(pack_7z('Film.1999.mkv', b'picture'))
        saved = extract_volumes([copy], folder / 'copy')
        self.assertEqual(saved[0].name, 'Film.1999.mkv')
        self.assertEqual(saved[0].read_bytes(), b'picture')
        compressed = folder / 'packed.7z'
        compressed.write_bytes(pack_7z('Film.1999.mkv', b'picture' * 40, method='lzma2'))
        opened = extract_volumes([compressed], folder / 'lzma')
        self.assertEqual(opened[0].read_bytes(), b'picture' * 40)
        locked = folder / 'locked.7z'
        locked.write_bytes(pack_7z('Film.1999.mkv', b'secret-picture', 'archive-secret'))
        with self.assertRaises(ArchiveError) as missing:
            extract_volumes([locked], folder / 'closed')
        self.assertIn('password', str(missing.exception).lower())
        with self.assertRaises(ArchiveError) as wrong:
            extract_volumes([locked], folder / 'wrong', 'nope')
        self.assertIn('password', str(wrong.exception).lower())
        unlocked = extract_volumes([locked], folder / 'opened', 'archive-secret')
        self.assertEqual(unlocked[0].read_bytes(), b'secret-picture')

    def test_split_seven_zip_and_raw_pieces_and_nested_archives(self):
        folder = Path(tempfile.mkdtemp())
        blob = pack_7z('Film.1999.mkv', b'joined-picture')
        (folder / 'set.7z.002').write_bytes(blob[20:])
        (folder / 'set.7z.001').write_bytes(blob[:20])
        saved = prepare(list(folder.glob('set.7z.*')), folder / 'out')
        self.assertEqual(saved[0].read_bytes(), b'joined-picture')
        pieces = folder / 'pieces'
        pieces.mkdir()
        (pieces / 'movie.mkv.002').write_bytes(b'cdef')
        (pieces / 'movie.mkv.001').write_bytes(b'\x1aE\xdf\xa3ab')
        (pieces / 'release.par2').write_bytes(b'PAR2')
        joined = prepare(list(pieces.iterdir()), folder / 'joined')
        self.assertEqual(len(joined), 1)
        self.assertEqual(joined[0].name, 'movie.mkv')
        self.assertEqual(joined[0].read_bytes(), b'\x1aE\xdf\xa3abcdef')
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, 'w') as archive:
            archive.writestr('Nested.mkv', b'nested-picture')
        wrapped = folder / 'wrapped.7z'
        wrapped.write_bytes(pack_7z('inside.zip', inner.getvalue()))
        nested = prepare([wrapped], folder / 'nested')
        self.assertEqual(nested[0].read_bytes(), b'nested-picture')
        manifest = b'<nzb><head><meta type="name">Example</meta></head></nzb>'
        opened, name = unwrap_manifest(gzip.compress(manifest), 'Example.nzb.gz')
        self.assertEqual(opened, manifest)
        self.assertEqual(name, 'Example.nzb')

    def test_new_download_starts_without_scanning_parts(self):
        folder = Path(tempfile.mkdtemp())
        engine = NzbEngine(folder / 'library', folder / 'data')
        self.addCleanup(engine.shutdown)
        job = type('Job', (), {})()
        job.id = 'fresh'
        job.files = [type('File', (), {'index': 0, 'segments': [object(), object()]})()]
        self.assertEqual(len(engine._pending_segments(job)), 2)
        self.assertFalse((engine.work_dir / 'fresh' / 'parts').exists())

    def test_indexer_address_gains_an_api_path(self):
        self.assertEqual(api_base('http://127.0.0.1:9/'), 'http://127.0.0.1:9/api')
        self.assertEqual(api_base('http://127.0.0.1:9/api'), 'http://127.0.0.1:9/api')


class NzbDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / 'library'
        self.base.mkdir()
        self.engine = NzbEngine(self.base, Path(self.tmp.name) / 'data')
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.articles = {'abc@test': encode(b'video-bytes', 'Example.1999.mkv')}
        self.drops = {}
        threading.Thread(target=self._serve, daemon=True).start()

    def tearDown(self):
        self.engine.shutdown()
        self.sock.close()
        self.tmp.cleanup()

    def _serve(self):
        while True:
            try:
                connection, _addr = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(connection,), daemon=True).start()

    def _client(self, connection):
        reader = connection.makefile('rb')
        connection.sendall(b'200 test\r\n')
        try:
            while True:
                line = reader.readline()
                if not line:
                    break
                text = line.decode().strip()
                upper = text.upper()
                if upper.startswith('AUTHINFO USER'):
                    connection.sendall(b'381 password\r\n')
                elif upper.startswith('AUTHINFO PASS'):
                    connection.sendall(b'281 ok\r\n')
                elif upper.startswith('STAT'):
                    msgid = text.split()[-1].strip('<>')
                    connection.sendall(b'223 0 <id>\r\n' if msgid in self.articles else b'430 No Such Article\r\n')
                elif upper.startswith('BODY'):
                    msgid = text.split()[-1].strip('<>')
                    if self.drops.get(msgid, 0):
                        self.drops[msgid] -= 1
                        connection.close()
                        return
                    body = self.articles.get(msgid)
                    if body is None:
                        connection.sendall(b'430 missing\r\n')
                        continue
                    connection.sendall(b'222 0 <id>\r\n')
                    for item in body:
                        if item.startswith(b'.'):
                            item = b'.' + item
                        connection.sendall(item + b'\r\n')
                    connection.sendall(b'.\r\n')
                elif text.upper().startswith('QUIT'):
                    connection.sendall(b'205 bye\r\n')
                    break
                else:
                    connection.sendall(b'500 unsupported\r\n')
        finally:
            connection.close()

    def test_a_movie_nzb_is_saved_under_movies(self):
        metadata_calls = []
        self.engine.metadata = lambda job, created: metadata_calls.append(created)
        self.engine.update_settings({'servers': [{'name': 'local', 'host': '127.0.0.1', 'port': self.port, 'ssl': False, 'username': 'news', 'password': 'secret', 'connections': 2, 'enabled': True}]})
        shown = self.engine.settings()
        self.assertTrue(shown['servers'][0]['password_saved'])
        self.assertNotIn('password', shown['servers'][0])
        self.assertNotIn('secret', str(shown))
        preview = self.engine.preview_bytes(b'''<nzb><head><meta type="name">Example.1999.1080p</meta></head>
            <file subject="&quot;Example.1999.mkv&quot; yEnc (1/1)"><groups><group>alt.binaries.test</group></groups>
            <segments><segment bytes="10" number="1">abc@test</segment></segments></file></nzb>''', 'Example.1999.nzb')
        self.assertTrue(preview['accepted'])
        self.assertEqual(preview['suggested_name'], 'Example (1999)')
        started = self.engine.start(preview['id'], None, preview['suggested_name'])
        deadline = time.time() + 5
        current = started
        while time.time() < deadline and current['state'] not in {'stopped', 'failed', 'choose'}:
            time.sleep(0.05)
            current = self.engine.job(started['id'])
        self.assertEqual(current['state'], 'stopped', current)
        saved = self.base / 'Movies' / 'Example (1999)' / 'Example (1999).mkv'
        self.assertEqual(saved.read_bytes(), b'video-bytes')
        # Publication exposes "stopped" before its metadata callback runs.
        # Wait for this job's complete worker lifecycle, including the callback,
        # before checking either enabled metadata or its deliberate absence.
        worker = self.engine._jobs[started['id']].worker
        self.assertIsNotNone(worker)
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), 'The download worker did not finish its metadata stage')
        self.assertEqual(bool(metadata_calls), self.engine.config['share_metadata'])

    def test_metadata_can_be_disabled_without_preventing_video_completion(self):
        self.engine.update_settings({'share_metadata': False})
        self.test_a_movie_nzb_is_saved_under_movies()

    def test_a_dropped_article_is_fetched_again(self):
        self.drops['flake@test'] = 1
        self.articles['flake@test'] = encode(b'video-bytes', 'hash-without-suffix')
        self.engine.update_settings({'servers': [{'name': 'local', 'host': '127.0.0.1', 'port': self.port, 'ssl': False, 'username': 'news', 'password': 'secret', 'connections': 1, 'enabled': True}]})
        preview = self.engine.preview_bytes(b'''<nzb><file subject="&quot;Film.mkv&quot; yEnc (1/1)"><groups><group>alt.binaries.test</group></groups>
            <segments><segment bytes="10" number="1">flake@test</segment></segments></file></nzb>''', 'Film.nzb')
        started = self.engine.start(preview['id'], None, 'Film (1999)')
        deadline = time.time() + 5
        current = started
        while time.time() < deadline and current['state'] not in {'stopped', 'failed', 'choose'}:
            time.sleep(0.05)
            current = self.engine.job(started['id'])
        self.assertEqual(current['state'], 'stopped', current)
        self.assertEqual((self.base / 'Movies' / 'Film (1999)' / 'Film (1999).mkv').read_bytes(), b'video-bytes')

    def test_parts_without_byte_offsets_are_joined_in_order(self):
        from backend.downloaders.nzb.engine import _File, _Job, _Segment
        segments = [_Segment(1, 4, 'a@test', ()), _Segment(2, 4, 'b@test', ())]
        for segment in segments:
            segment.file_index = 0
        job = _Job(id='join', name='Film', files=[_File(0, 'Film.mkv', (), segments)], selected={0})
        folder = self.engine.work_dir / job.id / 'parts' / '0'
        folder.mkdir(parents=True)
        for number, payload in ((1, b'aaaa'), (2, b'bbbb')):
            (folder / f'{number:06d}.bin').write_bytes(payload)
            (folder / f'{number:06d}.json').write_text(json.dumps({'offset': 0, 'name': 'hash', 'size': 4}), encoding='utf-8')
        written, missing = self.engine._assemble(job)
        self.assertFalse(missing)
        self.assertEqual(written[0].read_bytes(), b'aaaabbbb')
        self.assertTrue(written[0].name.endswith('.mkv'))

    def test_save_as_names_the_video_when_the_article_name_has_no_extension(self):
        hidden = '5c29b1e25b8980e2425a34ea89fa642533967e47b0519c189c1909f1'
        self.articles['hash@test'] = encode(b'\x1aE\xdf\xa3video', hidden)
        self.engine.update_settings({'servers': [{'name': 'local', 'host': '127.0.0.1', 'port': self.port, 'ssl': False, 'username': 'news', 'password': 'secret', 'connections': 2, 'enabled': True}]})
        preview = self.engine.preview_bytes(b'''<nzb><head><meta type="name">Insidious.2026</meta></head>
            <file subject="&quot;tuQEmkFSXWv3ol1ZI6zv5LqVTiyiLV14.mkv&quot; yEnc (1/1)"><groups><group>alt.binaries.test</group></groups>
            <segments><segment bytes="10" number="1">hash@test</segment></segments></file></nzb>''', 'Insidious.nzb')
        started = self.engine.start(preview['id'], None, 'Insidious (2026)')
        deadline = time.time() + 5
        current = started
        while time.time() < deadline and current['state'] not in {'stopped', 'failed', 'choose'}:
            time.sleep(0.05)
            current = self.engine.job(started['id'])
        self.assertEqual(current['state'], 'stopped', current)
        saved = self.base / 'Movies' / 'Insidious (2026)' / 'Insidious (2026).mkv'
        self.assertTrue(saved.is_file(), current)
        self.assertTrue(saved.read_bytes().startswith(b'\x1aE\xdf\xa3'))
        self.assertFalse(any(hidden in path.name for path in self.base.rglob('*')))

    def test_a_server_without_the_articles_fails_without_walking_every_segment(self):
        self.engine.update_settings({'servers': [{'name': 'local', 'host': '127.0.0.1', 'port': self.port, 'ssl': False, 'username': 'news', 'password': 'secret', 'connections': 4, 'enabled': True}]})
        segments = ''.join(f'<segment bytes="10" number="{index}">missing-{index}@test</segment>' for index in range(1, 61))
        preview = self.engine.preview_bytes(f'''<nzb><file subject="&quot;Film.mkv&quot; yEnc (1/60)"><groups><group>alt.binaries.test</group></groups>
            <segments>{segments}</segments></file></nzb>'''.encode(), 'Film.nzb')
        self.assertEqual(preview['on_server'], 'missing')
        from backend.downloaders.nzb.engine import EngineError
        started = time.time()
        with self.assertRaises(EngineError) as caught:
            self.engine.start(preview['id'], None, 'Film (2026)')
        self.assertIn('does not have these articles', str(caught.exception))
        self.assertLess(time.time() - started, 4)

    def test_scanning_an_nzb_warns_when_the_server_has_no_articles(self):
        from backend.downloaders.nzb.engine import EngineError
        servers = [{'host': '127.0.0.1', 'port': self.port, 'ssl': False, 'username': 'news', 'password': 'secret'}]
        present = b'''<nzb><file subject="&quot;Example.1999.mkv&quot; yEnc (1/1)"><groups><group>alt.binaries.test</group></groups>
            <segments><segment bytes="10" number="1">abc@test</segment></segments></file></nzb>'''
        mixed = b'''<nzb><file subject="&quot;Example.1999.mkv&quot; yEnc (1/2)"><groups><group>alt.binaries.test</group></groups>
            <segments><segment bytes="10" number="1">abc@test</segment><segment bytes="10" number="2">gone@test</segment></segments></file></nzb>'''
        self.assertEqual(articles_on_server(servers, present), 'available')
        self.assertEqual(articles_on_server(servers, mixed), 'partial')
        self.engine.update_settings({'servers': [{'name': 'local', 'host': '127.0.0.1', 'port': self.port, 'ssl': False, 'username': 'news', 'password': 'secret', 'connections': 2, 'enabled': True}]})
        preview = self.engine.preview_bytes(b'''<nzb><file subject="&quot;Film.mkv&quot; yEnc (1/1)"><groups><group>alt.binaries.test</group></groups>
            <segments><segment bytes="10" number="1">gone@test</segment></segments></file></nzb>''', 'Film.nzb')
        self.assertEqual(preview['on_server'], 'missing')
        with self.assertRaises(EngineError) as caught:
            self.engine.start(preview['id'], None, 'Film (2026)')
        self.assertIn('does not have these articles', str(caught.exception))


class NzbPlaceTests(unittest.TestCase):
    def test_an_interrupted_save_is_replaced_by_the_complete_file(self):
        folder = Path(tempfile.mkdtemp())
        source = folder / 'source.mkv'
        source.write_bytes(b'complete-movie-bytes')
        library = folder / 'library'
        movie = library / 'Movies'
        movie.mkdir(parents=True)
        torn = movie / 'Film (2026).mkv'
        torn.write_bytes(b'complete')
        placed = _place(library, source, 'Movies/Film (2026).mkv')
        self.assertEqual(placed, 'Movies/Film (2026).mkv')
        self.assertEqual((movie / 'Film (2026).mkv').read_bytes(), b'complete-movie-bytes')
        self.assertEqual(list(movie.glob('*(2)*')), [])
        self.assertEqual(list(movie.glob('*.reeldock-partial')), [])

    def test_a_different_file_keeps_its_own_name(self):
        folder = Path(tempfile.mkdtemp())
        source = folder / 'source.mkv'
        source.write_bytes(b'new-movie')
        library = folder / 'library'
        movie = library / 'Movies'
        movie.mkdir(parents=True)
        (movie / 'Film (2026).mkv').write_bytes(b'other')
        placed = _place(library, source, 'Movies/Film (2026).mkv')
        self.assertEqual(placed, 'Movies/Film (2026) (2).mkv')
        self.assertEqual((movie / 'Film (2026).mkv').read_bytes(), b'other')
        self.assertEqual((movie / 'Film (2026) (2).mkv').read_bytes(), b'new-movie')


class NzbRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / 'library'
        self.base.mkdir()
        self.app = create_app({'TESTING': True, 'DATA_DIR': self.tmp.name, 'MEDIA_DIR': self.tmp.name + '/youtube'})
        self.store = self.app.extensions['store']
        self.store.set('public_origin', ORIGIN)
        self.store.set('setup_complete', '1')
        self.store.set('storage_base', str(self.base))
        with self.store.connect() as db:
            db.execute("INSERT INTO users VALUES(1,'owner','unused')")
        self.client = self.app.test_client()
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session['user_id'] = 1
            session['csrf'] = 'token'

    def tearDown(self):
        stop(self.app)
        self.tmp.cleanup()

    def test_lookup_uses_the_save_as_name_before_the_video_exists(self):
        from backend.downloaders.nzb.engine import _Job
        self.client.get('/api/nzb', base_url=ORIGIN)
        engine = self.app.extensions['nzb_engine']
        job = _Job(id='matchbox', name='tuQEmkFSXWv3ol1ZI6zv5LqVTiyiLV14.mkv', files=[], selected=set(), save_as='Matchbox the Movie (2026)')
        engine._jobs[job.id] = job
        response = self.client.get('/api/nzb/matchbox/lookup', base_url=ORIGIN)
        self.assertEqual(response.status_code, 200, response.get_json())
        card = response.get_json()
        self.assertEqual(card['title'], 'Matchbox the Movie')
        self.assertEqual(card['year'], '2026')
        self.assertEqual(card['media_type'], 'movie')
        self.assertNotIn('Unknown NZB', str(card))

    def test_routes_require_sign_in(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get('/api/nzb', base_url=ORIGIN).status_code, 401)
        denied = self.client.post('/api/nzb/preview', base_url=ORIGIN, json={'url': 'https://example.com/film.nzb'}, headers={'Origin': ORIGIN, 'X-CSRF-Token': 'wrong'})
        self.assertEqual(denied.status_code, 403)
