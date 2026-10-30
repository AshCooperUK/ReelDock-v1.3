"""Open stored RAR archives and ZIP files that arrived inside an NZB.

Video posted to Usenet is normally stored, not compressed. Compressed RAR is
refused with a clear error rather than handed to another program.
"""

from __future__ import annotations

import re
import struct
import zipfile
import zlib
from pathlib import Path


class ArchiveError(ValueError):
    pass


def _vint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift <= 63:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ArchiveError("This RAR archive is incomplete.")


def _write_vint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def extract(paths: list[Path], destination: Path, password: str = "") -> list[Path]:
    """Extract every archive in paths. Return the files that were written."""
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    groups: dict[str, list[Path]] = {}
    plain: list[Path] = []
    for path in paths:
        key = volume_key(path)
        if key is None:
            if path.suffix.lower() == ".zip":
                written.extend(_zip(path, destination))
            continue
        groups.setdefault(key, []).append(path)
    for volumes in groups.values():
        volumes = _unique_volumes(volumes)
        written.extend(_rar_volumes(volumes, destination, password))
    written.extend(plain)
    return written


def volume_key(path: Path) -> str | None:
    """Recover the identity of obfuscated RAR4 volumes from their file header.

    Keep ordinary archive sets separate. Hash-named volumes retain their .rNN
    ordering but not a shared basename; only split members may join this way.
    """
    fallback = _volume_key(path.name)
    stem = re.sub(r"^\d{4}-", "", path.stem.lower())
    if fallback is None or not re.fullmatch(r"[0-9a-f]{32,64}", stem):
        return fallback
    with path.open("rb") as handle:
        data = handle.read(65536)
    if not data.startswith(b"Rar!\x1a\x07\x00"):
        return fallback
    offset = 7
    while offset + 7 <= len(data):
        _crc, kind, flags, size = struct.unpack_from("<HBHH", data, offset)
        if size < 7 or offset + size > len(data):
            break
        header = data[offset:offset + size]
        if kind == 0x74:
            if not flags & 3:
                return fallback
            _pack, unpacked, _method, name = _rar4_file(header, flags)
            return "split-member:" + repr((name, unpacked))
        if flags & 0x8000:
            break
        offset += size
    return fallback


def _unused_target(destination: Path, leaf: str) -> Path:
    target = destination / leaf
    counter = 1
    while target.exists():
        target = destination / f"{Path(leaf).stem}-{counter}{Path(leaf).suffix}"
        counter += 1
    return target


def _volume_key(name: str) -> str | None:
    import re

    lower = re.sub(r"^\d{4}-", "", name.lower())
    if lower.endswith(".par2"):
        return None
    part = re.sub(r"\.part\d+\.rar$", "", lower)
    if part != lower:
        return part
    old = re.sub(r"\.r\d{2}$", "", lower)
    if old != lower:
        return old
    if lower.endswith(".rar"):
        return lower[:-4]
    return None


def _unique_volumes(volumes: list[Path]) -> list[Path]:
    """Keep one file per volume number. A repeated assemble must not append the part twice."""
    import re

    chosen: dict[int, Path] = {}
    for path in volumes:
        number, _lower = _volume_order(path.name)
        current = chosen.get(number)
        prefixed = bool(re.match(r"^\d{4}-", path.name))
        if current is None or (re.match(r"^\d{4}-", current.name) and not prefixed):
            chosen[number] = path
    return [chosen[number] for number in sorted(chosen)]


def _volume_order(name: str) -> tuple[int, str]:
    import re

    lower = name.lower()
    part = re.search(r"\.part(\d+)\.rar$", lower)
    if part:
        return int(part.group(1)), lower
    old = re.search(r"\.r(\d{2})$", lower)
    if old:
        return 2 + int(old.group(1)), lower
    if lower.endswith(".rar"):
        return 1, lower
    return 0, lower


def _zip(path: Path, destination: Path) -> list[Path]:
    written = []
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                leaf = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
                if not leaf or leaf in {".", ".."}:
                    continue
                target = _unused_target(destination, leaf)
                target.write_bytes(archive.read(info))
                written.append(target)
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise ArchiveError(path.name + " is not a readable ZIP archive.") from exc
    return written


def _rar_volumes(volumes: list[Path], destination: Path, password: str = "") -> list[Path]:
    """Join split volumes into files. One volume is written out before the next is read."""
    handles: dict[str, tuple[Path, object]] = {}
    written: list[Path] = []
    try:
        for volume in volumes:
            data = volume.read_bytes()
            if data.startswith(b"Rar!\x1a\x07\x01\x00"):
                blocks = _rar5(data, password)
            elif data.startswith(b"Rar!\x1a\x07\x00"):
                blocks = _rar4(data, password)
            else:
                raise ArchiveError(volume.name + " is not a RAR archive ReelDock can read.")
            del data
            for name, chunk, _continued in blocks:
                leaf = name.replace("\\", "/").rsplit("/", 1)[-1]
                if not leaf or leaf in {".", ".."}:
                    raise ArchiveError("A file inside the RAR archive has an unsafe name.")
                if leaf not in handles:
                    target = _unused_target(destination, leaf)
                    handle = target.open("wb")
                    handles[leaf] = (target, handle)
                    written.append(target)
                handles[leaf][1].write(chunk)
            del blocks
    finally:
        for _target, handle in handles.values():
            handle.close()
    return written


def _rar4(data: bytes, password: str = "") -> list[tuple[str, bytes, bool]]:
    del password
    offset = 7
    found = []
    while offset + 7 <= len(data):
        crc, kind, flags, head_size = struct.unpack_from("<HBHH", data, offset)
        del crc
        if head_size < 7 or offset + head_size > len(data):
            raise ArchiveError("This RAR archive is incomplete.")
        header = data[offset : offset + head_size]
        add_size = 0
        if flags & 0x8000 and len(header) >= 11:
            add_size = struct.unpack_from("<I", header, 7)[0]
        if kind == 0x7B:
            return found
        if kind == 0x74:
            if flags & 0x0004:
                raise ArchiveError("This RAR archive uses older password protection. ReelDock opens RAR5 passwords stored in the NZB.")
            pack, unpacked, method, name = _rar4_file(header, flags)
            if method != 0x30:
                raise ArchiveError("This RAR archive is compressed. ReelDock extracts stored video archives, which is how video is usually posted.")
            payload = data[offset + head_size : offset + head_size + pack]
            if len(payload) != pack or (unpacked and pack != unpacked and not (flags & 0x0003)):
                raise ArchiveError("This RAR archive is incomplete.")
            found.append((name, payload, bool(flags & 0x0003)))
            offset += head_size + pack
            continue
        offset += head_size + (add_size if flags & 0x8000 and kind != 0x74 else 0)
    return found


def _rar5(data: bytes, password: str = "") -> list[tuple[str, bytes, bool]]:
    offset = 8
    found = []
    while offset + 6 <= len(data):
        if offset + 4 > len(data):
            break
        _crc = data[offset : offset + 4]
        offset += 4
        header_size, offset = _vint(data, offset)
        start = offset
        if header_size <= 0 or start + header_size > len(data):
            raise ArchiveError("This RAR archive is incomplete.")
        header = data[start : start + header_size]
        kind, cursor = _vint(header, 0)
        flags, cursor = _vint(header, cursor)
        extra = 0
        data_size = 0
        if flags & 0x0001:
            extra, cursor = _vint(header, cursor)
        if flags & 0x0002:
            data_size, cursor = _vint(header, cursor)
        if kind == 5:
            return found
        if kind == 2:
            file_flags, cursor = _vint(header, cursor)
            unpacked, cursor = _vint(header, cursor)
            _attributes, cursor = _vint(header, cursor)
            if file_flags & 0x0004:
                cursor += 4
            if file_flags & 0x0002:
                cursor += 4
            compression, cursor = _vint(header, cursor)
            _host, cursor = _vint(header, cursor)
            name_length, cursor = _vint(header, cursor)
            name = header[cursor : cursor + name_length].decode("utf-8", "replace")
            cursor += name_length
            if compression & 0x3F:
                raise ArchiveError("This RAR archive is compressed. ReelDock extracts stored video archives, which is how video is usually posted.")
            if file_flags & 0x0001:
                offset = start + header_size + data_size
                continue
            payload = data[start + header_size : start + header_size + data_size]
            if len(payload) != data_size:
                raise ArchiveError("This RAR archive is incomplete.")
            crypt = _rar5_crypt(header[len(header) - extra :] if extra else b"")
            if crypt:
                payload = _decrypt_rar5(password, crypt, payload)
                if unpacked:
                    payload = payload[:unpacked]
            elif unpacked and data_size != unpacked and not (flags & 0x0018):
                raise ArchiveError("This RAR archive is incomplete.")
            found.append((name, payload, bool(flags & 0x0018)))
        offset = start + header_size + data_size
        del extra, _crc
    return found


def _rar5_crypt(extra: bytes) -> dict | None:
    cursor = 0
    while cursor < len(extra):
        try:
            size, cursor = _vint(extra, cursor)
        except ArchiveError:
            return None
        record = extra[cursor : cursor + size]
        cursor += size
        if len(record) < size or not record:
            break
        kind, pos = _vint(record, 0)
        if kind != 1:
            continue
        _version, pos = _vint(record, pos)
        _flags, pos = _vint(record, pos)
        if pos >= len(record):
            raise ArchiveError("The archive password data is incomplete.")
        kdf = record[pos]
        salt = record[pos + 1 : pos + 17]
        iv = record[pos + 17 : pos + 33]
        if len(salt) != 16 or len(iv) != 16:
            raise ArchiveError("The archive password data is incomplete.")
        return {"kdf": kdf, "salt": salt, "iv": iv}
    return None


def _decrypt_rar5(password: str, crypt: dict, payload: bytes) -> bytes:
    if not password:
        raise ArchiveError("This archive is password protected. ReelDock uses the password stored in the NZB.")
    from .rar_aes import CryptoError, aes256_cbc_decrypt, rar5_key

    try:
        key = rar5_key(password, crypt["salt"], int(crypt["kdf"]))
        return aes256_cbc_decrypt(key, crypt["iv"], payload)
    except CryptoError as exc:
        raise ArchiveError(str(exc)) from exc


def _rar4_file(header: bytes, flags: int) -> tuple[int, int, int, str]:
    """Read one RAR4 file header.

    The long-block flag means file bytes follow the header. Newer stored
    archives put that length in its own field and then the file fields. Older
    scene archives, including split discs, store it only as the packed size,
    with the file fields starting at the same place. Use the layout whose
    method and filename are real.
    """
    starts = (11, 7) if flags & 0x8000 else (7,)
    for start in starts:
        parsed = _rar4_fields(header, flags, start)
        if parsed is not None:
            return parsed
    raise ArchiveError("This RAR archive is incomplete.")


def _rar4_fields(header: bytes, flags: int, start: int) -> tuple[int, int, int, str] | None:
    if start + 25 > len(header):
        return None
    pack, unpacked, _host, _file_crc, _time, _version, method, name_size, _attr = struct.unpack_from("<IIBIIBBHI", header, start)
    cursor = start + 25
    if flags & 0x0100:
        if cursor + 8 > len(header):
            return None
        high_pack, high_unpacked = struct.unpack_from("<II", header, cursor)
        cursor += 8
        pack |= high_pack << 32
        unpacked |= high_unpacked << 32
    if method not in range(0x30, 0x36) or name_size < 1 or cursor + name_size > len(header):
        return None
    name = header[cursor : cursor + name_size].decode("utf-8", "replace")
    if not name or any(ord(ch) < 32 for ch in name):
        return None
    return pack, unpacked, method, name


def pack_rar4(name: str, payload: bytes) -> bytes:
    """Build a stored RAR4 archive. Tests use this; downloads only read archives."""
    name_b = name.encode("utf-8")
    fields = struct.pack("<IIBIIBBHI", len(payload), len(payload), 0, zlib.crc32(payload) & 0xFFFFFFFF, 0, 20, 0x30, len(name_b), 0x20) + name_b
    add = struct.pack("<I", len(payload))
    head_size = 2 + 1 + 2 + 2 + 4 + len(fields)
    header_after_crc = bytes([0x74]) + struct.pack("<HH", 0x8000, head_size) + add + fields
    crc = struct.pack("<H", zlib.crc32(header_after_crc) & 0xFFFF)
    file_header = crc + header_after_crc + payload
    archive_body = bytes([0x73]) + struct.pack("<HH", 0, 13) + b"\x00" * 6
    archive_crc = struct.pack("<H", zlib.crc32(archive_body) & 0xFFFF)
    return b"Rar!\x1a\x07\x00" + archive_crc + archive_body + file_header


def pack_rar5(name: str, payload: bytes, password: str = "") -> bytes:
    name_b = name.encode("utf-8")
    stored = payload
    extra = b""
    if password:
        from .rar_aes import aes256_cbc_encrypt, rar5_key

        salt = b"0123456789abcdef"
        iv = b"fedcba9876543210"
        padded = payload + b"\x00" * ((16 - len(payload) % 16) % 16)
        stored = aes256_cbc_encrypt(rar5_key(password, salt, 1), iv, padded or b"\x00" * 16)
        record = _write_vint(1) + _write_vint(0) + _write_vint(0) + bytes([1]) + salt + iv
        extra = _write_vint(len(record)) + record
    file_fields = b"".join(
        [
            _write_vint(0x0004),
            _write_vint(len(payload)),
            _write_vint(0),
            struct.pack("<I", zlib.crc32(payload) & 0xFFFFFFFF),
            _write_vint(0),
            _write_vint(0),
            _write_vint(len(name_b)),
            name_b,
        ]
    )
    flags = 0x0003 if extra else 0x0002
    sizes = (_write_vint(len(extra)) if extra else b"") + _write_vint(len(stored))
    file_body = _write_vint(2) + _write_vint(flags) + sizes + file_fields + extra
    file_size = _write_vint(len(file_body))
    file_crc = struct.pack("<I", zlib.crc32(file_size + file_body) & 0xFFFFFFFF)
    main_body = _write_vint(1) + _write_vint(0)
    main_size = _write_vint(len(main_body))
    main_crc = struct.pack("<I", zlib.crc32(main_size + main_body) & 0xFFFFFFFF)
    end_body = _write_vint(5) + _write_vint(0)
    end_size = _write_vint(len(end_body))
    end_crc = struct.pack("<I", zlib.crc32(end_size + end_body) & 0xFFFFFFFF)
    return b"Rar!\x1a\x07\x01\x00" + main_crc + main_size + main_body + file_crc + file_size + file_body + stored + end_crc + end_size + end_body
