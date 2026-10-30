"""Bencode (BEP 3) encode and decode."""

from __future__ import annotations


class BencodeError(ValueError):
    pass


_MAX_DEPTH = 32


def bdecode(data: bytes, index: int = 0, depth: int = 0) -> tuple[object, int]:
    if depth > _MAX_DEPTH:
        raise BencodeError("nesting too deep")
    if index >= len(data):
        raise BencodeError("truncated")
    kind = data[index]
    if kind == ord("i"):
        try:
            end = data.index(b"e", index)
        except ValueError as exc:
            raise BencodeError("truncated int") from exc
        raw = data[index + 1 : end]
        if not raw or raw == b"-" or raw.startswith(b"-0") or (len(raw) > 1 and raw.startswith(b"0")):
            raise BencodeError("bad int")
        try:
            value = int(raw)
        except ValueError as exc:
            raise BencodeError("bad int") from exc
        return value, end + 1
    if kind == ord("l"):
        index += 1
        items: list[object] = []
        while index < len(data) and data[index] != ord("e"):
            item, index = bdecode(data, index, depth + 1)
            items.append(item)
        if index >= len(data) or data[index] != ord("e"):
            raise BencodeError("truncated list")
        return items, index + 1
    if kind == ord("d"):
        index += 1
        items: dict[bytes, object] = {}
        while index < len(data) and data[index] != ord("e"):
            key, index = bdecode(data, index, depth + 1)
            if not isinstance(key, bytes):
                raise BencodeError("dict key must be a string")
            value, index = bdecode(data, index, depth + 1)
            items[key] = value
        if index >= len(data) or data[index] != ord("e"):
            raise BencodeError("truncated dict")
        return items, index + 1
    if 48 <= kind <= 57:
        try:
            colon = data.index(b":", index)
        except ValueError as exc:
            raise BencodeError("truncated string") from exc
        try:
            length = int(data[index:colon])
        except ValueError as exc:
            raise BencodeError("bad string length") from exc
        if length < 0 or data[index:colon].startswith(b"0") and colon - index > 1:
            raise BencodeError("bad string length")
        start = colon + 1
        end = start + length
        if end > len(data):
            raise BencodeError("string overrun")
        return data[start:end], end
    raise BencodeError(f"unknown type {kind}")


def bdecode_all(data: bytes) -> object:
    value, index = bdecode(data, 0)
    if index != len(data):
        raise BencodeError("trailing data")
    return value


def raw_value(data: bytes, key: bytes) -> bytes:
    """Return the exact encoded value of a top-level dict key."""
    if not data.startswith(b"d"):
        raise BencodeError("expected dict")
    index = 1
    while index < len(data) and data[index] != ord("e"):
        raw_key, index = bdecode(data, index)
        start = index
        _, index = bdecode(data, index)
        if raw_key == key:
            return data[start:index]
    raise KeyError(key)


def bencode(value: object) -> bytes:
    if isinstance(value, bool) or not isinstance(value, (int, bytes, str, list, dict)):
        raise BencodeError(f"cannot encode {type(value).__name__}")
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii") + b"e"
    if isinstance(value, str):
        value = value.encode("utf-8")
    if isinstance(value, bytes):
        return str(len(value)).encode("ascii") + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    items = []
    for key, item in value.items():
        if isinstance(key, str):
            key_b = key.encode("utf-8")
        elif isinstance(key, bytes):
            key_b = key
        else:
            raise BencodeError("dict key must be a string")
        items.append((key_b, item))
    items.sort(key=lambda pair: pair[0])
    body = b"".join(bencode(key) + bencode(item) for key, item in items)
    return b"d" + body + b"e"
