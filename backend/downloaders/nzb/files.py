"""Decide which NZB paths can be downloaded before any article is fetched."""

from __future__ import annotations

import re

from ..torrent.media import ClassifiedFile, classify_path, is_sample

_PART = re.compile(r"\.(?:r\d{2}|part\d+\.rar|z\d{2}|\d{3})$", re.IGNORECASE)
_ARCHIVE = {".rar", ".zip", ".7z", ".gz", ".bz2", ".xz", ".tar"}


def is_archive(path: str) -> bool:
    suffix = _suffix(path)
    return suffix in _ARCHIVE or bool(_PART.search(path))


def is_recovery(path: str) -> bool:
    return _suffix(path) == ".par2" or path.lower().endswith(".par2")


def _suffix(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return base[dot:].lower() if dot > 0 else ""


def classify(files: list[tuple[int, str, int]]) -> tuple[bool, str, tuple[ClassifiedFile, ...]]:
    """Accept a release that contains video, audio, or an archive of those."""
    rows = []
    has_media = False
    has_archive = False
    for index, path, size in files:
        kind, reason = classify_path(path)
        if is_sample(path):
            kind, reason = "rejected", "Sample file excluded"
        elif is_recovery(path):
            kind, reason = "archive", ""
        elif kind == "rejected" and is_archive(path):
            kind, reason = "archive", ""
        if kind in {"video", "audio"}:
            has_media = True
        if kind == "archive" and not is_recovery(path):
            has_archive = True
        rows.append((index, path, size, kind, reason))
    accepted = has_media or has_archive
    reason = "" if accepted else "No video, audio, or archive in this NZB. Nothing will be downloaded."
    classified = []
    for index, path, size, kind, item_reason in rows:
        selectable = accepted and kind in {"video", "audio", "subtitle", "artwork", "nfo", "archive"}
        if not accepted and kind in {"subtitle", "artwork", "nfo", "archive"}:
            item_reason = item_reason or "This file needs a video, audio, or archive file."
        classified.append(ClassifiedFile(index, path, size, kind, selectable, selectable, "" if selectable else item_reason))
    return accepted, reason, tuple(classified)
