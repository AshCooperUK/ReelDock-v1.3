"""Build tiny .torrent fixtures without a third-party torrent library."""

from __future__ import annotations

import hashlib

from backend.downloaders.torrent.bencode import bencode


def build_torrent(
    files: list[tuple[str, bytes]],
    *,
    name: str = "Sample",
    piece_length: int = 1024,
    announce: str = "http://127.0.0.1:9/announce",
    url_list: list[str] | None = None,
    trackers: list[str] | None = None,
    private: bool = False,
) -> bytes:
    blob = b"".join(data for _path, data in files)
    if not blob:
        pieces = hashlib.sha1(b"").digest()
    else:
        pieces = b"".join(
            hashlib.sha1(blob[offset : offset + piece_length]).digest()
            for offset in range(0, len(blob), piece_length)
        )
    single = len(files) == 1 and "/" not in files[0][0]
    if single:
        info: dict = {
            "name": files[0][0],
            "piece length": piece_length,
            "pieces": pieces,
            "length": len(files[0][1]),
        }
    else:
        info = {
            "name": name,
            "piece length": piece_length,
            "pieces": pieces,
            "files": [
                {"length": len(data), "path": path.split("/")}
                for path, data in files
            ],
        }
    if private:
        info["private"] = 1
    root: dict = {"announce": announce, "info": info}
    if trackers:
        root["announce-list"] = [[item] for item in trackers]
    if url_list is not None:
        root["url-list"] = url_list
    return bencode(root)


QUIET_SETTINGS = {
    "download_limit_bps": 0,
    "upload_limit_bps": 0,
    "enable_dht": False,
    "enable_pex": True,
    "enable_lsd": False,
    "encryption": "disabled",
    "enable_upnp": False,
    "enable_natpmp": False,
    "enable_web_seeds": True,
    "max_active_downloads": 2,
    "max_active_seeds": 2,
    "share_ratio_limit": 2,
    "listen_port": 0,
    "theme": "light",
}
