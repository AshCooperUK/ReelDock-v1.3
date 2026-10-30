"""HTTP and FTP web seeds (BEP 19)."""

from __future__ import annotations

import ftplib
import threading
import urllib.error
import urllib.parse
import urllib.request

from .metainfo import Metainfo, TorrentFile


class WebSeedError(ConnectionError):
    pass


def file_urls(meta: Metainfo, torrent_file: TorrentFile) -> list[str]:
    urls: list[str] = []
    for base in meta.url_list:
        parsed = urllib.parse.urlparse(base)
        if parsed.scheme not in {"http", "https", "ftp"}:
            continue
        if meta.multi:
            segments = [meta.name, *torrent_file.parts]
            suffix = "/".join(urllib.parse.quote(part, safe="") for part in segments)
            root = base if base.endswith("/") else base.rstrip("/") + "/"
            urls.append(root + suffix)
        elif base.endswith("/"):
            urls.append(base + urllib.parse.quote(meta.name, safe=""))
        else:
            urls.append(base)
    return urls


def fetch_range(
    url: str,
    start: int,
    length: int,
    consume,
    stop: threading.Event,
) -> bytes:
    if length <= 0:
        return b""
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme in {"http", "https"}:
        return _http(url, start, length, consume, stop)
    if scheme == "ftp":
        return _ftp(url, start, length, consume, stop)
    raise WebSeedError(f"unsupported web seed {scheme}")


def _take(consume, stop: threading.Event, data: bytes) -> None:
    if stop.is_set() or not consume(len(data), stop):
        raise WebSeedError("stopped")


def _http(url: str, start: int, length: int, consume, stop: threading.Event) -> bytes:
    end = start + length - 1
    request = urllib.request.Request(
        url,
        headers={"Range": f"bytes={start}-{end}", "User-Agent": "ReelDock/0.1"},
    )
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except (OSError, urllib.error.URLError) as exc:
        raise WebSeedError(str(exc)) from exc
    try:
        status = getattr(response, "status", 200)
        if status == 206 or (status == 200 and start == 0):
            return _read_exact(response, length, consume, stop)
        raise WebSeedError(f"web seed HTTP {status}")
    finally:
        response.close()


def _read_exact(response, length: int, consume, stop: threading.Event) -> bytes:
    chunks = []
    remaining = length
    while remaining:
        if stop.is_set():
            raise WebSeedError("stopped")
        try:
            chunk = response.read(min(16 * 1024, remaining))
        except OSError as exc:
            raise WebSeedError(str(exc)) from exc
        if not chunk:
            break
        _take(consume, stop, chunk)
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) != length:
        raise WebSeedError("short web seed response")
    return data


def _ftp(url: str, start: int, length: int, consume, stop: threading.Event) -> bytes:
    parsed = urllib.parse.urlparse(url)
    remote = urllib.parse.unquote(parsed.path)
    ftp = ftplib.FTP()
    try:
        ftp.connect(parsed.hostname or "", parsed.port or 21, timeout=20)
        ftp.login(urllib.parse.unquote(parsed.username or "anonymous"), urllib.parse.unquote(parsed.password or "reeldock@local"))
        ftp.voidcmd("TYPE I")
        if start:
            ftp.voidcmd(f"REST {start}")
        chunks: list[bytes] = []

        def callback(chunk: bytes) -> None:
            if stop.is_set():
                raise WebSeedError("stopped")
            need = length - sum(len(part) for part in chunks)
            if need <= 0:
                raise WebSeedError("done")
            piece = chunk[:need]
            _take(consume, stop, piece)
            chunks.append(piece)
            if sum(len(part) for part in chunks) >= length:
                raise WebSeedError("done")

        try:
            ftp.retrbinary(f"RETR {remote}", callback, blocksize=16 * 1024)
        except WebSeedError as exc:
            if str(exc) != "done":
                raise
        data = b"".join(chunks)
        if len(data) != length:
            raise WebSeedError("short FTP response")
        return data
    except ftplib.all_errors as exc:
        raise WebSeedError(str(exc)) from exc
    finally:
        try:
            ftp.close()
        except Exception:
            pass
