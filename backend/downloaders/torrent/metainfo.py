"""Parse .torrent bytes and magnet links. Info-hash uses the raw info dict."""

from __future__ import annotations

import hashlib
import urllib.parse
from dataclasses import dataclass, field

from .bencode import BencodeError, bdecode_all, raw_value


class MetainfoError(ValueError):
    pass


@dataclass
class TorrentFile:
    index: int
    path: str
    parts: tuple[str, ...]
    length: int
    offset: int
    safe: bool


@dataclass
class Metainfo:
    info_hash: bytes
    info_bytes: bytes
    raw: bytes
    name: str
    piece_length: int
    pieces: bytes
    files: list[TorrentFile]
    private: bool
    trackers: list[str]
    url_list: list[str]
    multi: bool
    nodes: list[tuple[str, int]] = field(default_factory=list)
    comment: str = ""

    @property
    def total_length(self) -> int:
        return sum(item.length for item in self.files)

    @property
    def piece_count(self) -> int:
        return len(self.pieces) // 20


def _text(value: object, default: str = "") -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value
    return default


def _tracker_list(root: dict) -> list[str]:
    found: list[str] = []

    def add(value: object) -> None:
        text = _text(value).strip()
        if text and text not in found:
            found.append(text)

    tiers = root.get(b"announce-list")
    if isinstance(tiers, list):
        for tier in tiers:
            if isinstance(tier, list):
                for item in tier:
                    add(item)
            else:
                add(tier)
    add(root.get(b"announce"))
    return found


def _url_list(root: dict) -> list[str]:
    value = root.get(b"url-list")
    urls: list[str] = []
    if isinstance(value, list):
        for item in value:
            text = _text(item).strip()
            if text:
                urls.append(text)
    else:
        text = _text(value).strip()
        if text:
            urls.append(text)
    return urls


def _safe_parts(parts: list[str]) -> bool:
    if not parts:
        return False
    for part in parts:
        if part in {"", ".", ".."} or "/" in part or "\\" in part or "\x00" in part:
            return False
    return True


def parse_torrent(data: bytes) -> Metainfo:
    if len(data) > 2_000_000:
        raise MetainfoError("torrent file is too large")
    try:
        root = bdecode_all(data)
        info_bytes = raw_value(data, b"info")
    except (BencodeError, KeyError) as exc:
        raise MetainfoError("not a torrent file") from exc
    if not isinstance(root, dict) or not isinstance(root.get(b"info"), dict):
        raise MetainfoError("not a torrent file")
    info = root[b"info"]
    name = _text(info.get(b"name"), "download")
    try:
        piece_length = int(info[b"piece length"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MetainfoError("missing piece length") from exc
    pieces = info.get(b"pieces")
    if not isinstance(pieces, bytes) or len(pieces) % 20 != 0 or piece_length <= 0:
        raise MetainfoError("bad piece hashes")
    files: list[TorrentFile] = []
    multi = b"files" in info
    if multi:
        entries = info[b"files"]
        if not isinstance(entries, list) or not entries:
            raise MetainfoError("missing file list")
        offset = 0
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise MetainfoError("bad file entry")
            try:
                length = int(entry[b"length"])
            except (KeyError, TypeError, ValueError) as exc:
                raise MetainfoError("bad file length") from exc
            if length < 0:
                raise MetainfoError("bad file length")
            raw_parts = entry.get(b"path")
            if not isinstance(raw_parts, list) or not raw_parts:
                raise MetainfoError("bad file path")
            parts = tuple(_text(part) for part in raw_parts)
            path = "/".join(parts)
            files.append(
                TorrentFile(index, path, parts, length, offset, _safe_parts(list(parts)))
            )
            offset += length
    else:
        try:
            length = int(info[b"length"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MetainfoError("missing length") from exc
        if length < 0 or not name:
            raise MetainfoError("bad single-file torrent")
        files.append(TorrentFile(0, name, (name,), length, 0, _safe_parts([name])))
    total = sum(item.length for item in files)
    expected = 0 if total == 0 else (total + piece_length - 1) // piece_length
    if expected * 20 != len(pieces):
        raise MetainfoError("piece count does not match file sizes")
    nodes: list[tuple[str, int]] = []
    for node in root.get(b"nodes") or []:
        if isinstance(node, list) and len(node) == 2:
            try:
                nodes.append((_text(node[0]), int(node[1])))
            except (TypeError, ValueError):
                continue
    return Metainfo(
        info_hash=hashlib.sha1(info_bytes).digest(),
        info_bytes=info_bytes,
        raw=data,
        name=name,
        piece_length=piece_length,
        pieces=pieces,
        files=files,
        private=info.get(b"private") == 1,
        trackers=_tracker_list(root),
        url_list=_url_list(root),
        multi=multi,
        nodes=nodes,
        comment=_text(root.get(b"comment")),
    )


@dataclass
class Magnet:
    info_hash: bytes
    name: str
    trackers: list[str]


def parse_magnet(uri: str) -> Magnet:
    parsed = urllib.parse.urlparse(uri.strip())
    if parsed.scheme != "magnet":
        raise MetainfoError("not a magnet link")
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    xt = (params.get("xt") or [""])[0]
    if not xt.lower().startswith("urn:btih:"):
        raise MetainfoError("magnet is missing urn:btih")
    token = xt.split(":", 2)[2].strip()
    if len(token) == 40:
        try:
            info_hash = bytes.fromhex(token)
        except ValueError as exc:
            raise MetainfoError("bad info-hash") from exc
    elif len(token) == 32:
        import base64

        try:
            info_hash = base64.b32decode(token.upper())
        except Exception as exc:
            raise MetainfoError("bad info-hash") from exc
        if len(info_hash) != 20:
            raise MetainfoError("bad info-hash")
    else:
        raise MetainfoError("bad info-hash")
    name = (params.get("dn") or ["magnet"])[0] or "magnet"
    trackers = []
    for tracker in params.get("tr") or []:
        if tracker and tracker not in trackers:
            trackers.append(tracker)
    return Magnet(info_hash=info_hash, name=name, trackers=trackers)


def usable_trackers(trackers: list[str]) -> tuple[list[str], list[str]]:
    """Split trackers into ones this engine can announce to, and the rest."""
    ok: list[str] = []
    skipped: list[str] = []
    for tracker in trackers:
        scheme = urllib.parse.urlparse(tracker).scheme.lower()
        if scheme in {"http", "https", "udp"}:
            ok.append(tracker)
        else:
            skipped.append(tracker)
    return ok, skipped
