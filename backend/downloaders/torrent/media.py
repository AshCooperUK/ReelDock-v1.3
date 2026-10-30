"""Decide which torrent paths are allowed before any payload is stored."""

from __future__ import annotations

from dataclasses import dataclass

VIDEO = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".flv", ".webm", ".ts",
    ".m2ts", ".mpg", ".mpeg", ".ogv", ".3gp", ".vob", ".m2v", ".divx",
}
AUDIO = {
    ".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".alac",
    ".aiff", ".aif", ".wma", ".mka",
}
SUBTITLE = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup"}
ARTWORK = {".jpg", ".jpeg", ".png", ".webp"}
INFO = {".nfo"}
MEDIA_KINDS = {"video", "audio"}
COMPANION_KINDS = {"subtitle", "artwork", "nfo"}


@dataclass(frozen=True)
class ClassifiedFile:
    index: int
    path: str
    size: int
    kind: str
    selected: bool
    selectable: bool
    reason: str


@dataclass(frozen=True)
class GateResult:
    accepted: bool
    reason: str
    files: tuple[ClassifiedFile, ...]


def _suffix(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    dot = base.rfind(".")
    if dot <= 0:
        return ""
    return base[dot:].lower()


def path_is_safe(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        return False
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    lowered = path.lower()
    if "_____padding_file" in lowered or lowered.endswith(".pad"):
        return False
    return True


def is_sample(path: str) -> bool:
    """User rule: sample-named files and sample directories are never payload."""
    return "sample" in path.lower()


def classify_path(path: str) -> tuple[str, str]:
    if not path_is_safe(path):
        return "rejected", "Unsafe path"
    if is_sample(path):
        return "rejected", "Sample file excluded"
    suffix = _suffix(path)
    if suffix in VIDEO:
        return "video", ""
    if suffix in AUDIO:
        return "audio", ""
    if suffix in SUBTITLE:
        return "subtitle", ""
    if suffix in ARTWORK:
        return "artwork", ""
    if suffix in INFO:
        return "nfo", ""
    if not suffix:
        return "rejected", "No file extension"
    return "rejected", f"Not media ({suffix})"


def gate(files: list[tuple[int, str, int]]) -> GateResult:
    """Classify files. Accepted only when the torrent contains video or audio.

    Subtitles, artwork, and .nfo files are selected only as companions of that media.
    Everything else stays off and cannot be turned on.
    """
    classified: list[ClassifiedFile] = []
    has_media = False
    for index, path, size in files:
        kind, reason = classify_path(path)
        if kind in MEDIA_KINDS:
            has_media = True
        classified.append(
            ClassifiedFile(index, path, size, kind, False, False, reason)
        )
    if not has_media:
        finalized = []
        for item in classified:
            reason = item.reason or "No video or audio in this torrent"
            if item.kind in COMPANION_KINDS:
                reason = "Subtitles, artwork, and .nfo files need a video or audio file"
            finalized.append(
                ClassifiedFile(
                    item.index, item.path, item.size, item.kind, False, False, reason
                )
            )
        return GateResult(
            False,
            "No video or audio files. Nothing will be downloaded.",
            tuple(finalized),
        )
    finalized = []
    for item in classified:
        if item.kind in MEDIA_KINDS or item.kind in COMPANION_KINDS:
            finalized.append(
                ClassifiedFile(
                    item.index, item.path, item.size, item.kind, True, True, ""
                )
            )
        else:
            finalized.append(item)
    return GateResult(True, "", tuple(finalized))
