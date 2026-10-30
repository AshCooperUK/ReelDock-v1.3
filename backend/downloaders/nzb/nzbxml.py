"""Read an NZB file into files and article ids. Nothing is downloaded."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

_QUOTED = re.compile(r'"([^"\\]{1,240})"')
_MESSAGE = re.compile(r"^[A-Za-z0-9$%&'*+/=?^_`{|}~.@!-]{1,250}$")
# Obfuscated posts leave the yEnc quotes empty and put the real name in [brackets].
_BRACKETED = re.compile(
    r"\[((?:[^\[\]]|\[[^\]]*\])*\.(?:mkv|mp4|m4v|avi|mov|ts|mpg|mpeg|rar|zip|7z|par2|nfo|srt|sub|idx|jpg|jpeg|png|r\d{2}))\]",
    re.IGNORECASE,
)


class NzbError(ValueError):
    pass


@dataclass(frozen=True)
class Segment:
    number: int
    size: int
    message_id: str


@dataclass(frozen=True)
class NzbFile:
    index: int
    subject: str
    name: str
    groups: tuple[str, ...]
    segments: tuple[Segment, ...]
    date: int

    @property
    def size(self) -> int:
        return sum(item.size for item in self.segments)


@dataclass(frozen=True)
class ParsedNzb:
    name: str
    files: tuple[NzbFile, ...]
    password: str = ""
    category: str = ""
    inner_name: str = ""

    def __repr__(self) -> str:
        return f"ParsedNzb(name={self.name!r}, files={len(self.files)}, category={self.category!r})"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _leaf(value: str) -> str:
    base = value.replace("\\", "/").rsplit("/", 1)[-1].strip().strip(".")
    if not base or base in {".", ".."} or "/" in base or "\\" in base:
        return ""
    return base[:180]


def _name_from_subject(subject: str, index: int) -> str:
    quoted = _QUOTED.findall(subject)
    for candidate in reversed(quoted):
        base = _leaf(candidate)
        if base and "." in base:
            return base
    for candidate in reversed(_BRACKETED.findall(subject)):
        base = _leaf(candidate)
        if base:
            return base
    if quoted:
        base = _leaf(quoted[-1])
        if base:
            return base
    return f"file-{index:04d}.bin"


def _message_id(value: str) -> str:
    text = value.strip().strip("<>").strip()
    if not _MESSAGE.fullmatch(text):
        raise NzbError("An article id in this NZB is not valid.")
    return text


def parse(data: bytes, name: str = "") -> ParsedNzb:
    if not data or len(data) > 20_000_000:
        raise NzbError("Choose an NZB file smaller than 20 MB.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NzbError("That file is not a valid NZB.") from exc
    if _local(root.tag) != "nzb":
        raise NzbError("That file is not a valid NZB.")
    release = inner = password = category = ""
    files: list[NzbFile] = []
    for element in root.iter():
        kind = _local(element.tag)
        if kind == "meta":
            meta = (element.attrib.get("type") or "").lower()
            text = (element.text or "").strip()
            if meta == "name" and text:
                release = text[:180]
            elif meta == "title" and text:
                inner = text[:180]
            elif meta == "password":
                password = text[:200]
            elif meta == "category" and text:
                category = text[:80]
        elif kind == "file":
            files.append(_file(element, len(files)))
            if len(files) > 20000:
                raise NzbError("This NZB lists too many files.")
    if not files:
        raise NzbError("This NZB has no files.")
    label = release or name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if label.lower().endswith(".nzb"):
        label = label[:-4]
    if not label:
        label = inner
    return ParsedNzb(label[:180] or "NZB", tuple(files), password, category, inner)


def _file(element: ET.Element, index: int) -> NzbFile:
    subject = (element.attrib.get("subject") or "").strip()
    groups = tuple(
        (child.text or "").strip()
        for child in element
        if _local(child.tag) == "groups"
        for child in child
        if _local(child.tag) == "group" and (child.text or "").strip()
    )
    segments: list[Segment] = []
    for child in element:
        if _local(child.tag) != "segments":
            continue
        for segment in child:
            if _local(segment.tag) != "segment":
                continue
            try:
                number = int(segment.attrib.get("number") or "0")
                size = int(segment.attrib.get("bytes") or "0")
            except ValueError as exc:
                raise NzbError("A segment in this NZB has a bad size.") from exc
            if number < 1 or size < 0 or size > 50_000_000:
                raise NzbError("A segment in this NZB has a bad size.")
            segments.append(Segment(number, size, _message_id(segment.text or "")))
            if len(segments) > 2_000_000:
                raise NzbError("This NZB lists too many articles.")
    if not segments:
        raise NzbError("A file in this NZB has no articles.")
    try:
        posted = int(element.attrib.get("date") or "0")
    except ValueError:
        posted = 0
    segments.sort(key=lambda item: item.number)
    return NzbFile(index, subject[:500], _name_from_subject(subject, index), groups[:8], tuple(segments), posted)
