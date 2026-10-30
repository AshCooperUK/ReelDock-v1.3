"""yEnc encode and decode (the encoding used inside Usenet articles)."""

from __future__ import annotations

import re
import zlib

_CRITICAL = {0, 9, 10, 13, 32, 61}
_SHIFT = bytes((index - 42) & 0xFF for index in range(256))
_BEGIN = re.compile(rb"=ybegin\s+(.+)$")
_PART = re.compile(rb"=ypart\s+(.+)$")
_END = re.compile(rb"=yend\s+(.+)$")
_FIELD = re.compile(rb"([a-zA-Z0-9]+)=(\S+)")


class YencError(ValueError):
    pass


def _fields(header: bytes) -> dict[str, str]:
    found = {}
    for key, value in _FIELD.findall(header):
        found[key.decode("ascii").lower()] = value.decode("latin-1")
    name_at = header.lower().find(b"name=")
    if name_at >= 0:
        name = header[name_at + 5 :].strip()
        if len(name) >= 2 and name[:1] == name[-1:] and name[:1] in {b'"', b"'"}:
            name = name[1:-1]
        found["name"] = name.decode("latin-1")
    return found


def _decode_line(line: bytes) -> bytes:
    if 61 not in line:
        return line.translate(_SHIFT)
    if line.endswith(b"="):
        raise YencError("A yEnc escape is incomplete.")
    parts = line.split(b"=")
    out = [parts[0].translate(_SHIFT)]
    for part in parts[1:]:
        if not part:
            raise YencError("A yEnc escape is incomplete.")
        out.append(bytes((((part[0] - 64) - 42) & 0xFF,)))
        if len(part) > 1:
            out.append(part[1:].translate(_SHIFT))
    return b"".join(out)


def decode(lines: list[bytes]) -> tuple[bytes, str, int]:
    """Return decoded bytes, the file name, and the absolute start offset (0-based)."""
    begin = None
    part = None
    payload: list[bytes] = []
    started = False
    carry = b""
    for line in lines:
        if not started:
            if line.startswith(b"=ybegin"):
                begin = _fields(line)
                started = True
            continue
        if line.startswith(b"=ypart"):
            part = _fields(line)
            continue
        if line.startswith(b"=yend"):
            if carry:
                raise YencError("A yEnc escape is incomplete.")
            end = _fields(line)
            data = b"".join(payload)
            expected = end.get("size")
            if expected and expected.isdigit() and int(expected) != len(data):
                raise YencError("The yEnc part size does not match its data.")
            crc = end.get("pcrc32") or end.get("crc32")
            if crc:
                try:
                    wanted = int(crc, 16)
                except ValueError as exc:
                    raise YencError("The yEnc checksum is not valid.") from exc
                if (zlib.crc32(data) & 0xFFFFFFFF) != wanted:
                    raise YencError("The yEnc checksum does not match.")
            if begin is None or "name" not in begin:
                raise YencError("The article has no yEnc file name.")
            offset = 0
            if part and part.get("begin", "").isdigit():
                offset = max(0, int(part["begin"]) - 1)
            return data, begin["name"], offset
        if carry:
            line = carry + line
            carry = b""
        if line.endswith(b"="):
            carry = b"="
            line = line[:-1]
        if line:
            payload.append(_decode_line(line))
    raise YencError("The article has no yEnc end marker.")


def encode(data: bytes, name: str, *, begin: int = 1, part: int = 1, total: int = 1, line_size: int = 128) -> list[bytes]:
    """Encode one part. Used by tests and by nothing on the download path."""
    full = len(data) if total == 1 else None
    header = f"=ybegin line={line_size} size={full if full is not None else len(data)} name={name}"
    lines = [header.encode("latin-1")]
    if total > 1:
        lines.append(f"=ypart begin={begin} end={begin + len(data) - 1}".encode("ascii"))
    encoded = bytearray()
    for byte in data:
        value = (byte + 42) & 0xFF
        if value in _CRITICAL:
            encoded.append(61)
            encoded.append((value + 64) & 0xFF)
        else:
            encoded.append(value)
    step = max(16, line_size)
    for start in range(0, len(encoded), step):
        chunk = encoded[start : start + step]
        if chunk[:1] == b"=":
            pass
        lines.append(bytes(chunk))
    crc = zlib.crc32(data) & 0xFFFFFFFF
    lines.append(f"=yend size={len(data)} part={part} pcrc32={crc:08x}".encode("ascii"))
    return lines
