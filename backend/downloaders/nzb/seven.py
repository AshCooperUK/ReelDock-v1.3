"""Open 7-Zip archives from NZB downloads.

Volumes such as name.7z.001 stay on disk. ReelDock reads the header, decrypts
it with the NZB password when the archive asks, and writes each file out as it
is decoded. LZMA and LZMA2 use Python's own compression library.
"""

from __future__ import annotations

import lzma
import struct
import zlib
from pathlib import Path

from .archive import ArchiveError

_COPY = b"\x00"
_LZMA = b"\x03\x01\x01"
_LZMA2 = b"\x21"
_AES = b"\x06\xf1\x07\x01"
_CHUNK = 1 << 20


class _Cursor:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise ArchiveError("This 7-Zip archive is incomplete.")
        value = self.data[self.pos]
        self.pos += 1
        return value

    def raw(self, size: int) -> bytes:
        end = self.pos + size
        if end > len(self.data):
            raise ArchiveError("This 7-Zip archive is incomplete.")
        value = self.data[self.pos:end]
        self.pos = end
        return value

    def number(self) -> int:
        first = self.byte()
        value = 0
        mask = 0x80
        for index in range(8):
            if (first & mask) == 0:
                return value + ((first & (mask - 1)) << (8 * index))
            value |= self.byte() << (8 * index)
            mask >>= 1
        return value

    def uint32(self) -> int:
        return struct.unpack("<I", self.raw(4))[0]

    def bits(self, count: int) -> list[int]:
        raw = self.raw((count + 7) // 8)
        return [(raw[index // 8] >> (index % 8)) & 1 for index in range(count)]


def _write_number(value: int) -> bytes:
    if value < 0x80:
        return bytes([value])
    for extra in range(1, 8):
        mask = 0x80 >> extra
        if value <= (((mask - 1) << (8 * extra)) | ((1 << (8 * extra)) - 1)):
            high = value >> (8 * extra)
            first = ((0x100 - (mask << 1)) & 0xFF) | high
            low = value & ((1 << (8 * extra)) - 1)
            return bytes([first]) + low.to_bytes(extra, "little")
    return bytes([0xFF]) + (value & ((1 << 64) - 1)).to_bytes(8, "little")


class _Coder:
    def __init__(self, method: bytes, props: bytes, num_in: int, num_out: int) -> None:
        self.method = method
        self.props = props
        self.num_in = num_in
        self.num_out = num_out


class _Folder:
    def __init__(self) -> None:
        self.coders: list[_Coder] = []
        self.bind_pairs: list[tuple[int, int]] = []
        self.packed_inputs: list[int] = []
        self.unpack_sizes: list[int] = []
        self.crc: int | None = None
        self.streams: list[tuple[int, int]] = []


class _File:
    def __init__(self, name: str) -> None:
        self.name = name
        self.size = 0
        self.crc: int | None = None
        self.has_stream = True
        self.folder = 0
        self.substream = 0


class _Volumes:
    def __init__(self, paths: list[Path]) -> None:
        self.paths = paths
        self.sizes = [path.stat().st_size for path in paths]
        self.total = sum(self.sizes)

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > self.total:
            raise ArchiveError("This 7-Zip archive is incomplete.")
        out = bytearray()
        cursor = 0
        for path, size in zip(self.paths, self.sizes):
            if cursor + size <= offset:
                cursor += size
                continue
            start = max(0, offset - cursor)
            take = min(length - len(out), size - start)
            with path.open("rb") as handle:
                handle.seek(start)
                out += handle.read(take)
            cursor += size
            if len(out) >= length:
                break
            offset = cursor
        if len(out) != length:
            raise ArchiveError("This 7-Zip archive is incomplete.")
        return bytes(out)


def extract_volumes(paths: list[Path], destination: Path, password: str = "") -> list[Path]:
    """Extract one 7-Zip archive split across paths. paths are already in volume order."""
    if not paths:
        return []
    source = _Volumes(paths)
    header, data_streams = _open(source, password)
    folders, files = _body(header)
    _attach(folders, data_streams)
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    used: set[str] = set()
    for folder in folders:
        members = [item for item in files if item.has_stream and item.folder == folders.index(folder)]
        if not members:
            continue
        reader = _folder_reader(folder, source, password)
        produced = 0
        expected = sum(item.size for item in members)
        for item in members:
            leaf = _leaf(item.name)
            if not leaf:
                raise ArchiveError("A file inside the 7-Zip archive has an unsafe name.")
            name = leaf if leaf not in used else f"{len(used)}-{leaf}"
            used.add(name)
            target = destination / name
            digest = 0
            remaining = item.size
            with target.open("wb") as handle:
                while remaining:
                    chunk = reader(min(_CHUNK, remaining))
                    if len(chunk) != min(_CHUNK, remaining) and len(chunk) != remaining:
                        if not chunk:
                            raise ArchiveError("This 7-Zip archive is incomplete.")
                    handle.write(chunk)
                    digest = zlib.crc32(chunk, digest) & 0xFFFFFFFF
                    remaining -= len(chunk)
                    produced += len(chunk)
            if item.crc is not None and digest != item.crc:
                raise ArchiveError("This 7-Zip archive is incomplete.")
            written.append(target)
        if produced != expected:
            raise ArchiveError("This 7-Zip archive is incomplete.")
    for item in files:
        if item.has_stream or not item.name or item.name.endswith("/"):
            continue
        leaf = _leaf(item.name)
        if leaf and leaf not in used:
            target = destination / leaf
            target.write_bytes(b"")
            written.append(target)
    return written


def _attach(folders: list[_Folder], data_streams: list[tuple[int, int]]) -> None:
    """Keep pack locations from the decoded header. Fill any folder that has none."""
    if not data_streams:
        return
    cursor = 0
    for folder in folders:
        if getattr(folder, "pack_streams", None):
            continue
        count = max(1, len(folder.packed_inputs))
        folder.pack_streams = data_streams[cursor:cursor + count]
        cursor += count


def _leaf(name: str) -> str:
    text = name.replace("\\", "/").rsplit("/", 1)[-1].replace("\x00", "").strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text:
        return ""
    return text[:180]


def _open(source: _Volumes, password: str) -> tuple[bytes, list[tuple[int, int]]]:
    if source.total < 32:
        raise ArchiveError("This 7-Zip archive is incomplete.")
    signature = source.read(0, 32)
    if signature[:6] != b"\x37\x7a\xbc\xaf\x27\x1c":
        raise ArchiveError(source.paths[0].name + " is not a 7-Zip archive ReelDock can read.")
    if (zlib.crc32(signature[12:32]) & 0xFFFFFFFF) != struct.unpack_from("<I", signature, 8)[0]:
        raise ArchiveError("This 7-Zip archive is incomplete.")
    next_offset, next_size, next_crc = struct.unpack_from("<QQI", signature, 12)
    start = 32 + next_offset
    if next_size > 8_000_000 or start + next_size > source.total:
        raise ArchiveError("This 7-Zip archive is incomplete.")
    raw = source.read(start, next_size)
    if (zlib.crc32(raw) & 0xFFFFFFFF) != next_crc:
        raise ArchiveError("This 7-Zip archive is incomplete.")
    cursor = _Cursor(raw)
    kind = cursor.byte()
    if kind == 0x01:
        return raw, []
    if kind != 0x17:
        raise ArchiveError("This 7-Zip archive uses a compression ReelDock cannot read.")
    folder, streams = _streams(cursor)
    if cursor.pos != len(raw) and cursor.data[cursor.pos:] != b"\x00":
        if cursor.byte() != 0x00 and cursor.pos < len(raw):
            raise ArchiveError("This 7-Zip archive is incomplete.")
    packed = _read_stream(source, streams[0])
    plain = _decode_bytes(folder, packed, password)
    if folder.crc is not None and (zlib.crc32(plain) & 0xFFFFFFFF) != folder.crc:
        raise ArchiveError("The 7-Zip password from the NZB did not open this archive.")
    if not plain.startswith(b"\x01"):
        raise ArchiveError("The 7-Zip password from the NZB did not open this archive.")
    return plain, streams[1:]


def _streams(cursor: _Cursor) -> tuple[_Folder, list[tuple[int, int]]]:
    """Read one StreamsInfo. The first byte (0x17 or 0x04) is already consumed when needed."""
    if cursor.data[cursor.pos] == 0x04:
        cursor.byte()
    folders: list[_Folder] = []
    streams: list[tuple[int, int]] = []
    while cursor.pos < len(cursor.data):
        kind = cursor.byte()
        if kind == 0x00:
            break
        if kind == 0x06:
            streams = _pack_info(cursor)
        elif kind == 0x07:
            folders = _unpack_info(cursor)
        elif kind == 0x08:
            _substreams(cursor, folders)
        else:
            raise ArchiveError("This 7-Zip archive uses a compression ReelDock cannot read.")
    if not folders:
        raise ArchiveError("This 7-Zip archive is incomplete.")
    _assign_streams(folders, streams)
    return folders[0], streams


def _pack_info(cursor: _Cursor) -> list[tuple[int, int]]:
    pack_pos = cursor.number()
    count = cursor.number()
    sizes: list[int] = []
    crcs: list[int | None] = [None] * count
    while True:
        kind = cursor.byte()
        if kind == 0x00:
            break
        if kind == 0x09:
            sizes = [cursor.number() for _ in range(count)]
        elif kind == 0x0A:
            crcs = _crcs(cursor, count)
        else:
            raise ArchiveError("This 7-Zip archive is incomplete.")
    if len(sizes) != count:
        raise ArchiveError("This 7-Zip archive is incomplete.")
    offset = 32 + pack_pos
    found = []
    for size, crc in zip(sizes, crcs):
        found.append((offset, size))
        if crc is not None:
            pass
        offset += size
    return found


def _unpack_info(cursor: _Cursor) -> list[_Folder]:
    if cursor.byte() != 0x0B:
        raise ArchiveError("This 7-Zip archive is incomplete.")
    count = cursor.number()
    if cursor.byte() != 0:
        raise ArchiveError("This 7-Zip archive uses a compression ReelDock cannot read.")
    folders = [_read_folder(cursor) for _ in range(count)]
    while True:
        kind = cursor.byte()
        if kind == 0x00:
            break
        if kind == 0x0C:
            for folder in folders:
                folder.unpack_sizes = [cursor.number() for _ in range(sum(coder.num_out for coder in folder.coders))]
        elif kind == 0x0A:
            crcs = _crcs(cursor, count)
            for folder, crc in zip(folders, crcs):
                folder.crc = crc
        else:
            raise ArchiveError("This 7-Zip archive is incomplete.")
    return folders


def _read_folder(cursor: _Cursor) -> _Folder:
    folder = _Folder()
    num_coders = cursor.number()
    in_total = 0
    out_total = 0
    for _ in range(num_coders):
        flag = cursor.byte()
        method = cursor.raw(flag & 0x0F)
        num_in = num_out = 1
        if flag & 0x10:
            num_in = cursor.number()
            num_out = cursor.number()
        props = cursor.raw(cursor.number()) if flag & 0x20 else b""
        if num_in != 1 or num_out != 1:
            raise ArchiveError("This 7-Zip archive uses a compression ReelDock cannot read.")
        folder.coders.append(_Coder(method, props, num_in, num_out))
        in_total += num_in
        out_total += num_out
    bind_count = out_total - 1
    folder.bind_pairs = [(cursor.number(), cursor.number()) for _ in range(bind_count)]
    packed = in_total - bind_count
    bound = {pair[0] for pair in folder.bind_pairs}
    if packed == 1:
        folder.packed_inputs = [index for index in range(in_total) if index not in bound]
    else:
        folder.packed_inputs = [cursor.number() for _ in range(packed)]
    return folder


def _unpack_size(folder: _Folder) -> int:
    """Only the unbound output is decoded data; other sizes are intermediate."""
    count = sum(coder.num_out for coder in folder.coders)
    if len(folder.unpack_sizes) != count:
        raise ArchiveError("This 7-Zip archive is incomplete.")
    bound = {out for _inn, out in folder.bind_pairs}
    if any(out < 0 or out >= count for out in bound):
        raise ArchiveError("This 7-Zip archive is incomplete.")
    final = [index for index in range(count) if index not in bound]
    if len(final) != 1:
        raise ArchiveError("This 7-Zip archive uses a compression ReelDock cannot read.")
    size = folder.unpack_sizes[final[0]]
    if size < 0:
        raise ArchiveError("This 7-Zip archive is incomplete.")
    return size


def _substreams(cursor: _Cursor, folders: list[_Folder]) -> None:
    counts = [1] * len(folders)
    while True:
        kind = cursor.byte()
        if kind == 0x00:
            break
        if kind == 0x0D:
            counts = [cursor.number() for _ in folders]
        elif kind == 0x09:
            for folder, count in zip(folders, counts):
                sizes = [cursor.number() for _ in range(max(0, count - 1))]
                if count:
                    remaining = _unpack_size(folder) - sum(sizes)
                    if remaining < 0:
                        raise ArchiveError("This 7-Zip archive is incomplete.")
                    sizes.append(remaining)
                folder.streams = [(size, None) for size in sizes]  # type: ignore[list-item]
        elif kind == 0x0A:
            crcs = _crcs(cursor, sum(counts))
            cursor_crc = 0
            for folder, count in zip(folders, counts):
                if not folder.streams:
                    folder.streams = [(_unpack_size(folder), None)] if count == 1 else []
                updated = []
                for size, _crc in folder.streams:
                    updated.append((size, crcs[cursor_crc]))
                    cursor_crc += 1
                folder.streams = updated
        else:
            raise ArchiveError("This 7-Zip archive is incomplete.")
    for folder, count in zip(folders, counts):
        if not folder.streams and count == 1:
            folder.streams = [(_unpack_size(folder), None)]
        elif not folder.streams and count:
            raise ArchiveError("This 7-Zip archive is incomplete.")


def _crcs(cursor: _Cursor, count: int) -> list[int | None]:
    defined = [1] * count if cursor.byte() else cursor.bits(count)
    found: list[int | None] = []
    for bit in defined:
        found.append(cursor.uint32() if bit else None)
    return found


def _assign_streams(folders: list[_Folder], streams: list[tuple[int, int]]) -> None:
    cursor = 0
    for folder in folders:
        count = len(folder.packed_inputs)
        folder.pack_streams = streams[cursor:cursor + count]  # type: ignore[attr-defined]
        cursor += count
        if not folder.streams:
            folder.streams = [(_unpack_size(folder), folder.crc)]


def _body(header: bytes) -> tuple[list[_Folder], list[_File]]:
    return _parse_header(header)


def _parse_header(header: bytes) -> tuple[list[_Folder], list[_File]]:
    cursor = _Cursor(header)
    if cursor.byte() != 0x01:
        raise ArchiveError("This 7-Zip archive is incomplete.")
    folders: list[_Folder] = []
    files: list[_File] = []
    while cursor.pos < len(cursor.data):
        kind = cursor.byte()
        if kind == 0x00:
            break
        if kind == 0x02:
            while True:
                prop = cursor.byte()
                if prop == 0x00:
                    break
                cursor.raw(cursor.number())
        elif kind == 0x04:
            folders = _main_streams(cursor)
        elif kind == 0x05:
            files = _files(cursor, folders)
        elif kind == 0x03:
            raise ArchiveError("This 7-Zip archive uses a compression ReelDock cannot read.")
        else:
            raise ArchiveError("This 7-Zip archive uses a compression ReelDock cannot read.")
    return folders, files


def _main_streams(cursor: _Cursor) -> list[_Folder]:
    folders: list[_Folder] = []
    streams: list[tuple[int, int]] = []
    while True:
        kind = cursor.byte()
        if kind == 0x00:
            break
        if kind == 0x06:
            streams = _pack_info(cursor)
        elif kind == 0x07:
            folders = _unpack_info(cursor)
        elif kind == 0x08:
            _substreams(cursor, folders)
        else:
            raise ArchiveError("This 7-Zip archive uses a compression ReelDock cannot read.")
    _assign_streams(folders, streams)
    return folders


def _files(cursor: _Cursor, folders: list[_Folder]) -> list[_File]:
    count = cursor.number()
    names = [f"file-{index}" for index in range(count)]
    empty = [0] * count
    while True:
        kind = cursor.byte()
        if kind == 0x00:
            break
        size = cursor.number()
        data = _Cursor(cursor.raw(size))
        if kind == 0x11 and data.byte() == 0:
            names = _names(data.raw(size - 1), count)
        elif kind == 0x0E:
            empty = data.bits(count)
    files = [_File(name) for name in names]
    stream = 0
    substreams = [(folder_index, sub, size, crc) for folder_index, folder in enumerate(folders) for sub, (size, crc) in enumerate(folder.streams)]
    for index, item in enumerate(files):
        if empty[index] or item.name.endswith("/"):
            item.has_stream = False
            item.size = 0
            continue
        if stream >= len(substreams):
            raise ArchiveError("This 7-Zip archive is incomplete.")
        folder_index, sub, size, crc = substreams[stream]
        item.folder = folder_index
        item.substream = sub
        item.size = size
        item.crc = crc
        stream += 1
    return files


def _names(data: bytes, count: int) -> list[str]:
    names = []
    chars: list[str] = []
    for index in range(0, len(data) - 1, 2):
        code = struct.unpack_from("<H", data, index)[0]
        if code == 0:
            names.append("".join(chars))
            chars = []
        else:
            chars.append(chr(code))
    if len(names) < count and chars:
        names.append("".join(chars))
    if len(names) != count:
        raise ArchiveError("This 7-Zip archive is incomplete.")
    return names


def _read_stream(source: _Volumes, stream: tuple[int, int]) -> bytes:
    offset, size = stream
    if size > 8_000_000:
        raise ArchiveError("This 7-Zip archive is incomplete.")
    return source.read(offset, size)


def _decode_bytes(folder: _Folder, packed: bytes, password: str) -> bytes:
    view = _Memory(packed)
    reader = _folder_reader(folder, view, password, base=0)
    out = bytearray()
    expected = sum(size for size, _crc in folder.streams) or _unpack_size(folder)
    while len(out) < expected:
        chunk = reader(min(_CHUNK, expected - len(out)))
        if not chunk:
            break
        out += chunk
    if len(out) < expected:
        raise ArchiveError("The 7-Zip password from the NZB did not open this archive.")
    return bytes(out[:expected])


class _Memory:
    """A stand-in volume for a header that is already in memory. Offsets are absolute."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.total = len(data)
        self.paths = [Path("header.7z")]

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > self.total:
            raise ArchiveError("This 7-Zip archive is incomplete.")
        return self.data[offset:offset + length]


def _folder_reader(folder: _Folder, source, password: str, base: int | None = None):
    if len(getattr(folder, "pack_streams", [])) != 1:
        raise ArchiveError("This 7-Zip archive uses a compression ReelDock cannot read.")
    offset, size = folder.pack_streams[0]
    if base is not None:
        offset = base
    remaining = size

    def packed(amount: int) -> bytes:
        nonlocal remaining
        amount = min(amount, remaining)
        if not amount:
            return b""
        data = source.read(offset + (size - remaining), amount)
        remaining -= len(data)
        return data

    return _chain(folder, packed, password, size)


def _chain(folder: _Folder, packed, password: str, packed_size: int):
    inputs: list[int] = []
    outputs: list[int] = []
    cursor_in = 0
    cursor_out = 0
    for coder in folder.coders:
        inputs.append(cursor_in)
        outputs.append(cursor_out)
        cursor_in += coder.num_in
        cursor_out += coder.num_out
    bound_out = {out for _inn, out in folder.bind_pairs}
    source_of = {inn: out for inn, out in folder.bind_pairs}
    final = [index for index in range(cursor_out) if index not in bound_out]
    if len(final) != 1 or folder.packed_inputs != [index for index in range(cursor_in) if index not in source_of]:
        if len(final) != 1:
            raise ArchiveError("This 7-Zip archive uses a compression ReelDock cannot read.")
    readers: dict[int, object] = {}

    def reader_for_out(out_index: int):
        if out_index in readers:
            return readers[out_index]
        coder_index = next(index for index, start in enumerate(outputs) if start == out_index)
        inn = inputs[coder_index]
        upstream = packed if inn not in source_of else reader_for_out(source_of[inn])
        if inn in source_of:
            upstream = reader_for_out(source_of[inn])
        else:
            upstream = packed if folder.coders[coder_index].method != _AES else _block_reader(packed, packed_size)
        made = _wrap(folder.coders[coder_index], upstream, password, packed_size if folder.coders[coder_index].method == _AES and inn not in source_of else None)
        readers[out_index] = made
        return made

    return reader_for_out(final[0])


def _block_reader(upstream, total: int):
    pending = bytearray()
    taken = 0

    def read(amount: int) -> bytes:
        nonlocal taken
        while len(pending) < amount and taken < total:
            want = min(_CHUNK, total - taken)
            if want % 16:
                want += 16 - (want % 16)
            want = min(want, total - taken)
            chunk = upstream(want)
            if not chunk:
                break
            pending.extend(chunk)
            taken += len(chunk)
        amount = min(amount, len(pending))
        out = bytes(pending[:amount])
        del pending[:amount]
        return out

    return read


def _wrap(coder: _Coder, upstream, password: str, aes_size: int | None):
    if coder.method == _COPY:
        return upstream
    if coder.method == _AES:
        return _aes_reader(coder, upstream, password, aes_size if aes_size is not None else 0)
    if coder.method == _LZMA2:
        return _lzma_reader(upstream, _lzma2_filters(coder.props))
    if coder.method == _LZMA:
        return _lzma_reader(upstream, _lzma_filters(coder.props))
    raise ArchiveError("This 7-Zip archive uses a compression ReelDock cannot read.")


def _aes_reader(coder: _Coder, upstream, password: str, total: int):
    if not password:
        raise ArchiveError("This archive is password protected. ReelDock uses the password stored in the NZB.")
    cycles, salt, iv = _aes_props(coder.props)
    from .rar_aes import AesCbcDecryptor, CryptoError, seven_zip_key

    try:
        decoder = AesCbcDecryptor(seven_zip_key(password, salt, cycles), iv)
    except CryptoError as exc:
        raise ArchiveError(str(exc)) from exc
    remaining = total
    pending = bytearray()

    def read(amount: int) -> bytes:
        nonlocal remaining
        while len(pending) < amount and remaining:
            want = min(_CHUNK, remaining)
            if want % 16:
                want += 16 - (want % 16)
            want = min(want, remaining)
            if want % 16:
                raise ArchiveError("This 7-Zip archive is incomplete.")
            chunk = _read_full(upstream, want)
            remaining -= len(chunk)
            try:
                pending.extend(decoder.update(chunk))
            except CryptoError as exc:
                raise ArchiveError(str(exc)) from exc
        amount = min(amount, len(pending))
        out = bytes(pending[:amount])
        del pending[:amount]
        return out

    return read


def _aes_props(props: bytes) -> tuple[int, bytes, bytes]:
    if not props:
        return 19, b"", bytes(16)
    first = props[0]
    cycles = first & 0x3F
    if (first & 0xC0) == 0:
        if len(props) != 1:
            raise ArchiveError("This 7-Zip archive is incomplete.")
        return cycles, b"", bytes(16)
    if len(props) < 2:
        raise ArchiveError("This 7-Zip archive is incomplete.")
    second = props[1]
    salt_size = ((first >> 7) & 1) + (second >> 4)
    iv_size = ((first >> 6) & 1) + (second & 0x0F)
    if len(props) != 2 + salt_size + iv_size:
        raise ArchiveError("This 7-Zip archive is incomplete.")
    salt = props[2:2 + salt_size]
    iv = props[2 + salt_size:] + bytes(16)
    return cycles, salt, iv[:16]


def _lzma2_filters(props: bytes) -> list[dict]:
    if len(props) != 1 or props[0] > 40:
        raise ArchiveError("This 7-Zip archive uses a compression ReelDock cannot read.")
    prop = props[0]
    if prop == 40:
        dictionary = 0xFFFFFFFF
    else:
        dictionary = (2 | (prop & 1)) << ((prop // 2) + 11)
    return [{"id": lzma.FILTER_LZMA2, "dict_size": dictionary}]


def _lzma_filters(props: bytes) -> list[dict]:
    if len(props) != 5:
        raise ArchiveError("This 7-Zip archive uses a compression ReelDock cannot read.")
    coded = props[0]
    if coded > 225:
        raise ArchiveError("This 7-Zip archive uses a compression ReelDock cannot read.")
    lc = coded % 9
    coded //= 9
    lp = coded % 5
    pb = coded // 5
    dictionary = struct.unpack("<I", props[1:])[0]
    return [{"id": lzma.FILTER_LZMA1, "dict_size": dictionary or (1 << 16), "lc": lc, "lp": lp, "pb": pb}]


def _lzma_reader(upstream, filters: list[dict]):
    decoder = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
    pending = bytearray()

    def read(amount: int) -> bytes:
        while len(pending) < amount and not decoder.eof:
            chunk = upstream(_CHUNK)
            if not chunk:
                break
            pending.extend(decoder.decompress(chunk))
        amount = min(amount, len(pending))
        out = bytes(pending[:amount])
        del pending[:amount]
        return out

    return read


def _read_full(upstream, amount: int) -> bytes:
    found = bytearray()
    while len(found) < amount:
        chunk = upstream(amount - len(found))
        if not chunk:
            break
        found += chunk
    if len(found) != amount:
        raise ArchiveError("This 7-Zip archive is incomplete.")
    return bytes(found)


def pack_7z(name: str, payload: bytes, password: str = "", method: str = "copy") -> bytes:
    """Build a small 7-Zip archive. Tests use this; downloads only read archives."""
    if method == "lzma2":
        filters = [{"id": lzma.FILTER_LZMA2, "preset": 0, "dict_size": 1 << 16}]
        packed = lzma.compress(payload, format=lzma.FORMAT_RAW, filters=filters)
        coder = bytes([0x01, 0x21, 0x21, 0x01, 0x08])
    else:
        packed = payload
        coder = bytes([0x01, 0x01, 0x00])
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    header = _raw_header(len(packed), len(payload), name, crc, coder)
    if not password:
        return _signature(packed, header)
    return _encrypted(packed, header, password)


def _raw_header(packed_size: int, unpacked_size: int, name: str, crc: int, coder: bytes) -> bytes:
    pack = bytes([0x06]) + _write_number(0) + _write_number(1) + bytes([0x09]) + _write_number(packed_size) + bytes([0x00])
    unpack = bytes([0x07, 0x0B]) + _write_number(1) + bytes([0x00]) + coder
    unpack += bytes([0x0C]) + _write_number(unpacked_size)
    unpack += bytes([0x0A, 0x01]) + struct.pack("<I", crc) + bytes([0x00])
    main = bytes([0x04]) + pack + unpack + bytes([0x00])
    encoded = name.encode("utf-16le") + b"\x00\x00"
    name_data = bytes([0x00]) + encoded
    files = bytes([0x05]) + _write_number(1) + bytes([0x11]) + _write_number(len(name_data)) + name_data + bytes([0x00])
    return bytes([0x01]) + main + files + bytes([0x00])


def _signature(packed: bytes, header: bytes) -> bytes:
    fields = struct.pack("<QQI", len(packed), len(header), zlib.crc32(header) & 0xFFFFFFFF)
    crc = struct.pack("<I", zlib.crc32(fields) & 0xFFFFFFFF)
    return b"\x37\x7a\xbc\xaf\x27\x1c" + b"\x00\x04" + crc + fields + packed + header


def _encrypted(packed: bytes, header: bytes, password: str) -> bytes:
    from .rar_aes import aes256_cbc_encrypt, seven_zip_key

    salt = b"0123456789abcdef"
    iv = b"fedcba9876543210"
    cycles = 4
    padded = header + bytes((-len(header)) % 16)
    cipher = aes256_cbc_encrypt(seven_zip_key(password, salt, cycles), iv, padded)
    props = bytes([cycles | 0xC0, ((len(salt) - 1) << 4) | (len(iv) - 1)]) + salt + iv
    coder = bytes([0x01, 0x24]) + _AES + _write_number(len(props)) + props
    descriptor = bytes([0x17, 0x06]) + _write_number(len(packed)) + _write_number(1)
    descriptor += bytes([0x09]) + _write_number(len(cipher)) + bytes([0x00])
    descriptor += bytes([0x07, 0x0B]) + _write_number(1) + bytes([0x00]) + coder
    descriptor += bytes([0x0C]) + _write_number(len(header))
    descriptor += bytes([0x0A, 0x01]) + struct.pack("<I", zlib.crc32(header) & 0xFFFFFFFF) + bytes([0x00, 0x00])
    return _signature(packed + cipher, descriptor)
