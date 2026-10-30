"""Turn assembled NZB files into the media they contain.

The download has already decoded the articles. This step verifies the set,
joins raw split files, and extracts archives. An archive can contain another
archive, so extraction repeats until the files are plain media. The format is
read from the file itself, because a .001 volume can be a raw piece or the
first part of a 7-Zip archive.
"""

from __future__ import annotations

import bz2
import gzip
import io
import lzma
import re
import shutil
import tarfile
import zipfile
from pathlib import Path

from .archive import ArchiveError, extract, volume_key
from .files import is_recovery
from .seven import extract_volumes

_LAYERS = 4
_CHUNK = 1 << 20
_NUMBER = re.compile(r"^(?P<stem>.*)\.(?P<part>\d{3})$", re.IGNORECASE)
_RAR_OLD = re.compile(r"^(?P<stem>.*)\.r(?P<part>\d{2})$", re.IGNORECASE)
_RAR_PART = re.compile(r"^(?P<stem>.*)\.part(?P<part>\d+)\.rar$", re.IGNORECASE)
_ZIP_PART = re.compile(r"^(?P<stem>.*)\.z(?P<part>\d{2})$", re.IGNORECASE)


def prepare(paths: list[Path], destination: Path, password: str = "") -> list[Path]:
    """Join and extract until the remaining files are not archives."""
    destination.mkdir(parents=True, exist_ok=True)
    pending = [path for path in paths if path.is_file() and not _supporting(path.name)]
    ready: list[Path] = []
    for layer in range(_LAYERS):
        if not pending:
            break
        extracted: list[Path] = []
        for kind, group in _groups(pending):
            if kind == "plain":
                ready.extend(group)
            elif kind == "split":
                ready.append(_join(group, destination))
            elif kind == "rar":
                extracted.extend(extract(_rar_order(group), destination / f"layer-{layer}", password))
            elif kind == "zip":
                extracted.extend(_zip_set(group, destination / f"layer-{layer}"))
            elif kind == "7z":
                extracted.extend(extract_volumes(_seven_order(group), destination / f"layer-{layer}", password))
            elif kind == "gzip":
                extracted.append(_decompress(group[0], destination / f"layer-{layer}", gzip.open))
            elif kind == "bz2":
                extracted.append(_decompress(group[0], destination / f"layer-{layer}", bz2.open))
            elif kind == "xz":
                extracted.append(_decompress(group[0], destination / f"layer-{layer}", lzma.open))
            elif kind == "tar":
                extracted.extend(_untar(group[0], destination / f"layer-{layer}"))
        pending = [path for path in extracted if _container(path)]
        ready.extend(path for path in extracted if path.is_file() and not _container(path))
    if pending:
        raise ArchiveError("This download contains too many nested archives.")
    return ready


def unwrap_manifest(data: bytes, name: str) -> tuple[bytes, str]:
    """An NZB is sometimes posted inside a ZIP or gzip file. Return the manifest."""
    if data.startswith(b"\x1f\x8b"):
        try:
            opened = gzip.decompress(data)
        except (OSError, EOFError) as exc:
            raise ArchiveError("That gzip file did not contain an NZB.") from exc
        if len(opened) > 20_000_000:
            raise ArchiveError("Choose an NZB file smaller than 20 MB.")
        return opened, name[:-3] if name.lower().endswith(".gz") else name
    if data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = [item.filename for item in archive.infolist() if not item.is_dir()]
                chosen = next((item for item in names if item.lower().endswith(".nzb")), names[0] if len(names) == 1 else "")
                if not chosen:
                    raise ArchiveError("That ZIP file did not contain an NZB.")
                opened = archive.read(chosen)
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            raise ArchiveError("That ZIP file did not contain an NZB.") from exc
        if len(opened) > 20_000_000:
            raise ArchiveError("Choose an NZB file smaller than 20 MB.")
        leaf = chosen.replace("\\", "/").rsplit("/", 1)[-1]
        return opened, leaf or name
    return data, name


def _supporting(name: str) -> bool:
    lower = name.lower()
    return is_recovery(lower) or lower.endswith(".sfv")


def _magic(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(512)


def _container(path: Path) -> bool:
    if not path.is_file():
        return False
    kind = _magic_kind(_magic(path), path.name)
    return kind in {"rar", "zip", "7z", "gzip", "bz2", "xz", "tar"}


def _magic_kind(magic: bytes, name: str) -> str:
    if magic.startswith(b"\x37\x7a\xbc\xaf\x27\x1c"):
        return "7z"
    if magic.startswith(b"Rar!\x1a\x07"):
        return "rar"
    if magic.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "zip"
    if magic.startswith(b"\x1f\x8b"):
        return "gzip"
    if magic.startswith(b"BZh"):
        return "bz2"
    if magic.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    if len(magic) >= 262 and magic[257:262] == b"ustar":
        return "tar"
    lower = _bare(name)
    if lower.endswith((".tar", ".tar.gz", ".tgz")):
        return "tar"
    return ""


def _bare(name: str) -> str:
    return re.sub(r"^\d{4}-", "", name.lower())


def _groups(paths: list[Path]) -> list[tuple[str, list[Path]]]:
    buckets: dict[tuple[str, str], list[Path]] = {}
    order: list[tuple[str, str]] = []
    alone: list[Path] = []
    for path in paths:
        key = _group_key(path.name)
        if key is not None and key[0] == "rar":
            key = ("rar", volume_key(path) or key[1])
        if key is None:
            alone.append(path)
            continue
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(path)
    found: list[tuple[str, list[Path]]] = []
    for key in order:
        group = buckets[key]
        found.append((_kind(key[0], group), group))
    for path in alone:
        found.append((_single(path), [path]))
    return found


def _group_key(name: str) -> tuple[str, str] | None:
    lower = _bare(name)
    part = _RAR_PART.match(lower)
    if part:
        return ("rar", part.group("stem"))
    old = _RAR_OLD.match(lower)
    if old:
        return ("rar", old.group("stem"))
    if lower.endswith(".rar"):
        return ("rar", lower[:-4])
    if ".7z." in lower and _NUMBER.match(lower) and lower.rsplit(".", 1)[0].endswith(".7z"):
        return ("7z", lower.rsplit(".", 1)[0])
    if lower.endswith(".7z"):
        return ("7z", lower)
    zip_part = _ZIP_PART.match(lower)
    if zip_part:
        return ("zip", zip_part.group("stem"))
    if lower.endswith(".zip"):
        return ("zip", lower[:-4])
    number = _NUMBER.match(lower)
    if number:
        return ("numbered", number.group("stem"))
    return None


def _kind(label: str, group: list[Path]) -> str:
    first = _ordered(group)[0]
    magic = _magic_kind(_magic(first), first.name)
    if label == "numbered":
        return magic if magic in {"7z", "rar", "zip"} else "split"
    if magic in {"7z", "rar", "zip"}:
        return magic
    return label


def _single(path: Path) -> str:
    magic = _magic_kind(_magic(path), path.name)
    if magic:
        return magic
    lower = _bare(path.name)
    if lower.endswith(".tar"):
        return "tar"
    return "plain"


def _ordered(group: list[Path]) -> list[Path]:
    def key(path: Path) -> tuple[int, str]:
        lower = _bare(path.name)
        for pattern in (_NUMBER, _RAR_OLD, _ZIP_PART, _RAR_PART):
            found = pattern.match(lower)
            if found:
                return int(found.group("part")), lower
        return 10_000, lower

    return sorted(group, key=key)


def _seven_order(group: list[Path]) -> list[Path]:
    return _ordered(group)


def _rar_order(group: list[Path]) -> list[Path]:
    def key(path: Path) -> tuple[int, str]:
        lower = _bare(path.name)
        part = _RAR_PART.match(lower)
        if part:
            return int(part.group("part")), lower
        old = _RAR_OLD.match(lower)
        if old:
            return 2 + int(old.group("part")), lower
        if lower.endswith(".rar"):
            return 1, lower
        return 0, lower

    return sorted(group, key=key)


def _join(group: list[Path], destination: Path) -> Path:
    ordered = _ordered(group)
    leaf = _output_name(ordered[0].name)
    target = _unique(destination, leaf)
    with target.open("wb") as handle:
        for path in ordered:
            with path.open("rb") as source:
                shutil.copyfileobj(source, handle, _CHUNK)
    return target


def _output_name(name: str) -> str:
    bare = re.sub(r"^\d{4}-", "", Path(name).name)
    number = _NUMBER.match(bare)
    if number:
        return number.group("stem")[:180] or "joined"
    return bare[:180] or "joined"


def _unique(folder: Path, leaf: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / leaf
    if not target.exists():
        return target
    stem = Path(leaf).stem
    suffix = Path(leaf).suffix
    index = 2
    while True:
        candidate = folder / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _zip_set(group: list[Path], destination: Path) -> list[Path]:
    ordered = _ordered(group)
    if len(ordered) == 1 and _bare(ordered[0].name).endswith(".zip"):
        return extract(ordered, destination)
    combined = destination / ".spanned.zip"
    destination.mkdir(parents=True, exist_ok=True)
    with combined.open("wb") as handle:
        for path in ordered:
            if _bare(path.name).endswith(".zip"):
                continue
            with path.open("rb") as source:
                shutil.copyfileobj(source, handle, _CHUNK)
        last = next((path for path in ordered if _bare(path.name).endswith(".zip")), None)
        if last is None:
            raise ArchiveError("This ZIP archive is missing a volume.")
        with last.open("rb") as source:
            shutil.copyfileobj(source, handle, _CHUNK)
    try:
        return extract([combined], destination)
    finally:
        combined.unlink(missing_ok=True)


def _decompress(path: Path, destination: Path, opener) -> Path:
    leaf = path.name
    for ending in (".gz", ".bz2", ".xz", ".tgz"):
        if leaf.lower().endswith(ending):
            leaf = leaf[: -len(ending)] or leaf
            break
    if leaf.lower().endswith(".tgz"):
        leaf = leaf[:-4] + ".tar"
    target = _unique(destination, leaf[:180])
    try:
        with opener(path, "rb") as source, target.open("wb") as handle:
            shutil.copyfileobj(source, handle, _CHUNK)
    except (OSError, EOFError, lzma.LZMAError) as exc:
        raise ArchiveError(path.name + " could not be unpacked.") from exc
    return target


def _untar(path: Path, destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    written = []
    try:
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                leaf = member.name.replace("\\", "/").rsplit("/", 1)[-1]
                if not leaf or leaf in {".", ".."}:
                    raise ArchiveError("A file inside the archive has an unsafe name.")
                source = archive.extractfile(member)
                if source is None:
                    continue
                target = _unique(destination, leaf[:180])
                with source, target.open("wb") as handle:
                    shutil.copyfileobj(source, handle, _CHUNK)
                written.append(target)
    except (tarfile.TarError, OSError) as exc:
        raise ArchiveError(path.name + " could not be unpacked.") from exc
    return written
