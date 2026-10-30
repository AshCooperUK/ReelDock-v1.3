"""PAR2 verification and repair.

The field is GF(2^16) with the PAR2 polynomial 0x1100B. Recovery slices use
the same Reed-Solomon construction as the PAR2 specification.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

MAGIC = b"PAR2\x00PKT"
_POLY = 0x1100B


class Par2Error(ValueError):
    pass


def _tables() -> tuple[list[int], list[int]]:
    exp = [0] * 65536
    log = [0] * 65536
    value = 1
    for index in range(65535):
        exp[index] = value
        log[value] = index
        value <<= 1
        if value & 0x10000:
            value ^= _POLY
    exp[65535] = 1
    return exp, log


_EXP, _LOG = _tables()


def mul(left: int, right: int) -> int:
    if left == 0 or right == 0:
        return 0
    return _EXP[(_LOG[left] + _LOG[right]) % 65535]


def _pow2(exponent: int) -> int:
    return _EXP[exponent % 65535]


def _packets(data: bytes) -> list[tuple[bytes, bytes, bytes]]:
    found = []
    offset = 0
    while offset + 64 <= len(data):
        if data[offset : offset + 8] != MAGIC:
            offset += 1
            continue
        length = struct.unpack_from("<Q", data, offset + 8)[0]
        if length < 64 or length > len(data) - offset or length % 4:
            break
        remainder = data[offset + 32 : offset + length]
        digest = data[offset + 16 : offset + 32]
        if hashlib.md5(remainder).digest() != digest:
            raise Par2Error("A PAR2 packet failed its checksum.")
        set_id, kind, body = remainder[:16], remainder[16:32], remainder[32:]
        found.append((kind, body, set_id))
        offset += length
    return found


def _packet(set_id: bytes, kind: bytes, body: bytes) -> bytes:
    padded = body + b"\x00" * ((4 - len(body) % 4) % 4)
    remainder = set_id + kind.ljust(16, b"\x00")[:16] + padded
    digest = hashlib.md5(remainder).digest()
    length = 32 + len(remainder)
    return MAGIC + struct.pack("<Q", length) + digest + remainder


def _words(raw: bytes) -> list[int]:
    padded = raw + b"\x00" * ((2 - len(raw) % 2) % 2)
    return list(struct.unpack("<" + "H" * (len(padded) // 2), padded))


def _pack(words: list[int], size: int) -> bytes:
    return struct.pack("<" + "H" * len(words), *words)[:size]


def load(paths: list[Path]) -> dict:
    """Read PAR2 packets from the recovery files in one download."""
    packets: list[tuple[bytes, bytes, bytes]] = []
    for path in paths:
        try:
            packets.extend(_packets(path.read_bytes()))
        except OSError as exc:
            raise Par2Error("A PAR2 file could not be read.") from exc
    if not packets:
        raise Par2Error("No PAR2 packets were found.")
    main = next((body for kind, body, _set_id in packets if kind.startswith(b"PAR 2.0\x00Main")), None)
    if main is None or len(main) < 28:
        raise Par2Error("The PAR2 data has no main packet.")
    slice_size = struct.unpack_from("<Q", main, 0)[0]
    count = struct.unpack_from("<I", main, 8)[0]
    ids = [main[12 + index * 16 : 28 + index * 16] for index in range(count)]
    if slice_size <= 0 or slice_size > 64 * 1024 * 1024 or len(ids) != count:
        raise Par2Error("The PAR2 slice size is not usable.")
    files = {}
    checksums: dict[bytes, list[tuple[bytes, int]]] = {}
    recovery: dict[int, bytes] = {}
    for kind, body, _set_id in packets:
        if kind.startswith(b"PAR 2.0\x00FileDesc") and len(body) >= 56:
            file_id = body[:16]
            files[file_id] = {
                "name": body[56:].split(b"\x00", 1)[0].decode("utf-8", "replace"),
                "length": struct.unpack_from("<Q", body, 48)[0],
                "md5": body[16:32],
            }
        elif kind.startswith(b"PAR 2.0\x00IFSC") and len(body) >= 16:
            file_id = body[:16]
            pairs = []
            cursor = 16
            while cursor + 20 <= len(body):
                pairs.append((body[cursor : cursor + 16], struct.unpack_from("<I", body, cursor + 16)[0]))
                cursor += 20
            checksums[file_id] = pairs
        elif kind.startswith(b"PAR 2.0\x00RecvSlic") and len(body) >= 4:
            exponent = struct.unpack_from("<I", body, 0)[0]
            recovery[exponent] = body[4 : 4 + slice_size]
    return {"slice": slice_size, "ids": ids, "files": files, "checksums": checksums, "recovery": recovery}


def _large_file_needs_repair(info: dict, ordered: list, slice_size: int) -> bool:
    """Check a large file one slice at a time. The whole video does not fit in memory."""
    for described, path in ordered:
        length = int(described["length"])
        if length <= 256 * 1024 * 1024:
            continue
        file_id = next(key for key, value in info["files"].items() if value is described)
        file_sums = info["checksums"].get(file_id, [])
        count = max(1, (length + slice_size - 1) // slice_size) if length else 0
        handle = path.open("rb") if path is not None and path.is_file() else None
        try:
            for index in range(count):
                start = index * slice_size
                size = min(slice_size, max(0, length - start))
                chunk = b""
                if handle is not None:
                    handle.seek(start)
                    chunk = handle.read(size)
                expected = file_sums[index][1] if index < len(file_sums) else None
                if len(chunk) < size or (expected is not None and _slice_crc(chunk) != expected):
                    return True
        finally:
            if handle is not None:
                handle.close()
    return False


def _slice_crc(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def repair(directory: Path, par_files: list[Path]) -> list[str]:
    """Repair files in directory. Return the names that were rewritten."""
    info = load(par_files)
    slice_size = info["slice"]
    present = {path.name: path for path in directory.iterdir() if path.is_file() and not path.name.endswith(".par2")}
    ordered: list[tuple[dict, Path | None]] = []
    for file_id in info["ids"]:
        described = info["files"].get(file_id)
        if described is None:
            continue
        ordered.append((described, present.get(Path(described["name"]).name)))
    slices: list[bytes | None] = []
    owners: list[tuple[Path | None, int, int]] = []
    largest = max((int(described["length"]) for described, _path in ordered), default=0)
    if largest > 256 * 1024 * 1024:
        if _large_file_needs_repair(info, ordered, slice_size):
            raise Par2Error("This download is too large to repair in memory.")
        return []
    for described, path in ordered:
        length = int(described["length"])
        raw = path.read_bytes() if path is not None and path.is_file() else b""
        count = max(1, (length + slice_size - 1) // slice_size) if length else 0
        file_id = next(key for key, value in info["files"].items() if value is described)
        file_sums = info["checksums"].get(file_id, [])
        for index in range(count):
            start = index * slice_size
            chunk = raw[start : start + slice_size]
            expected = file_sums[index][1] if index < len(file_sums) else None
            if len(chunk) < min(slice_size, max(0, length - start)) or (expected is not None and _slice_crc(chunk) != expected):
                slices.append(None)
            else:
                slices.append(chunk.ljust(slice_size, b"\x00"))
            owners.append((path, start, min(slice_size, max(0, length - start))))
    missing = [index for index, chunk in enumerate(slices) if chunk is None]
    if not missing:
        return []
    exponents = sorted(info["recovery"])
    if len(exponents) < len(missing):
        raise Par2Error("Not enough PAR2 recovery data to repair the missing articles.")
    chosen = exponents[: len(missing)]
    matrix = [[_pow2(exponent * column) for column in missing] for exponent in chosen]
    inverse = _invert(matrix)
    recovered: dict[int, bytearray] = {index: bytearray(slice_size) for index in missing}
    known = [(index, slices[index]) for index in range(len(slices)) if slices[index] is not None]
    width = slice_size // 2
    for word in range(width):
        syndrome = []
        for exponent in chosen:
            total = struct.unpack_from("<H", info["recovery"][exponent], word * 2)[0]
            factor_base = exponent
            for index, chunk in known:
                if chunk is None:
                    continue
                value = struct.unpack_from("<H", chunk, word * 2)[0]
                total ^= mul(value, _pow2(factor_base * index))
            syndrome.append(total)
        for row, index in enumerate(missing):
            total = 0
            for column, value in enumerate(syndrome):
                total ^= mul(inverse[row][column], value)
            struct.pack_into("<H", recovered[index], word * 2, total)
    written = []
    by_path: dict[Path, bytearray] = {}
    for index in missing:
        path, start, size = owners[index]
        if path is None:
            raise Par2Error("PAR2 refers to a file that was not downloaded.")
        target = by_path.setdefault(path, bytearray(path.read_bytes() if path.is_file() else b""))
        if len(target) < start + size:
            target.extend(b"\x00" * (start + size - len(target)))
        target[start : start + size] = recovered[index][:size]
    for path, data in by_path.items():
        described = next((item for item, location in ordered if location == path), None)
        if described is not None:
            data = data[: int(described["length"])]
            if hashlib.md5(data).digest() != described["md5"]:
                raise Par2Error("PAR2 repair did not restore " + path.name + ".")
        path.write_bytes(data)
        written.append(path.name)
    return written


def _invert(matrix: list[list[int]]) -> list[list[int]]:
    size = len(matrix)
    working = [row[:] + [1 if column == index else 0 for column in range(size)] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot = next((row for row in range(column, size) if working[row][column]), None)
        if pivot is None:
            raise Par2Error("PAR2 recovery data is not independent.")
        working[column], working[pivot] = working[pivot], working[column]
        scale = _inverse(working[column][column])
        working[column] = [mul(value, scale) for value in working[column]]
        for row in range(size):
            if row == column or working[row][column] == 0:
                continue
            factor = working[row][column]
            working[row] = [value ^ mul(factor, working[column][index]) for index, value in enumerate(working[row])]
    return [row[size:] for row in working]


def _inverse(value: int) -> int:
    if value == 0:
        raise Par2Error("PAR2 recovery data is not independent.")
    return _EXP[(65535 - _LOG[value]) % 65535]


def recovery_slice(slices: list[bytes], exponent: int) -> bytes:
    """Build one recovery slice. Tests use this to prove repair against the same field."""
    width = len(slices[0]) // 2
    out = bytearray(len(slices[0]))
    for word in range(width):
        total = 0
        for index, chunk in enumerate(slices):
            value = struct.unpack_from("<H", chunk, word * 2)[0]
            total ^= mul(value, _pow2(exponent * index))
        struct.pack_into("<H", out, word * 2, total)
    return bytes(out)
