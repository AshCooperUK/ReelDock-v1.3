"""Payload files under the media base. Symlinks are never followed."""

from __future__ import annotations

import hashlib
import os
import stat
import threading
from contextlib import nullcontext
from pathlib import Path

from ...operations import Operations
from .metainfo import Metainfo, TorrentFile
from .rename import align_existing_folder, category_for, companion_path, library_path, parse_save_as, stack_split_movies, previous_library_path, existing_saved_path


class UnsafePath(OSError):
    pass


def _lstat(dir_fd: int, name: str) -> os.stat_result | None:
    try:
        info = os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise UnsafePath(f"refusing to follow symlink {name}")
    return info


def open_directory(root: Path, parts: tuple[str, ...], create: bool) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open(root, flags)
    try:
        for part in parts:
            info = _lstat(current, part)
            if info is None:
                if not create:
                    raise FileNotFoundError(part)
                os.mkdir(part, mode=0o755, dir_fd=current)
            elif not stat.S_ISDIR(info.st_mode):
                raise UnsafePath(f"{part} is not a directory")
            nxt = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = nxt
        return current
    except Exception:
        os.close(current)
        raise


def remove_tree(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
        return
    if not path.exists():
        return
    if path.is_dir():
        for child in path.iterdir():
            remove_tree(child)
        path.rmdir()
        return
    path.unlink()


class Storage:
    def __init__(
        self,
        media_base: Path,
        meta: Metainfo,
        selected: set[int],
        operations: Operations | None = None,
    ) -> None:
        self.media_base = Path(media_base)
        self.meta = meta
        self.selected = set(selected)
        self.operations = operations
        digest = meta.info_hash.hex()
        self.work_parts = ("Downloads", ".reeldock-torrents", digest)
        self.verified = [False] * meta.piece_count
        self.have = [False] * meta.piece_count
        self.published: dict[int, str] = {}
        self.save_as = ""
        self._folder_cache: dict[str, list[str]] = {}
        self._lock = threading.Lock()
        if self.media_base.is_symlink():
            raise UnsafePath("media base is a symlink")
        self.media_base.mkdir(parents=True, exist_ok=True)

    def _section(self):
        if self.operations is None:
            return nullcontext()
        return self.operations.shared_section()

    def prepare(self) -> None:
        with self._section():
            directory = open_directory(self.media_base, self.work_parts, create=True)
            os.close(directory)

    def work_path(self) -> Path:
        return self.media_base.joinpath(*self.work_parts)

    def piece_size(self, index: int) -> int:
        start = index * self.meta.piece_length
        return min(self.meta.piece_length, self.meta.total_length - start)

    def _open_payload(self, torrent_file: TorrentFile, create: bool) -> int:
        if not torrent_file.safe or torrent_file.index not in self.selected:
            raise UnsafePath("file is not selected")
        directory = open_directory(
            self.media_base,
            self.work_parts + torrent_file.parts[:-1],
            create=create,
        )
        try:
            name = torrent_file.parts[-1]
            info = _lstat(directory, name)
            flags = os.O_RDWR | os.O_NOFOLLOW
            if info is None:
                if not create:
                    raise FileNotFoundError(name)
                flags |= os.O_CREAT
            elif not stat.S_ISREG(info.st_mode):
                raise UnsafePath("not a regular file")
            return os.open(name, flags, 0o644, dir_fd=directory)
        finally:
            os.close(directory)

    def write_piece(self, index: int, data: bytes) -> bool:
        if len(data) != self.piece_size(index):
            return False
        expected = self.meta.pieces[index * 20 : (index + 1) * 20]
        if hashlib.sha1(data).digest() != expected:
            return False
        start = index * self.meta.piece_length
        fully = True
        with self._lock:
            with self._section():
                for torrent_file in self.meta.files:
                    overlap_start = max(start, torrent_file.offset)
                    overlap_end = min(start + len(data), torrent_file.offset + torrent_file.length)
                    if overlap_start >= overlap_end:
                        continue
                    if torrent_file.index not in self.selected or not torrent_file.safe:
                        fully = False
                        continue
                    piece_off = overlap_start - start
                    chunk = data[piece_off : piece_off + (overlap_end - overlap_start)]
                    fd = self._open_payload(torrent_file, create=True)
                    try:
                        os.lseek(fd, overlap_start - torrent_file.offset, os.SEEK_SET)
                        os.write(fd, chunk)
                    finally:
                        os.close(fd)
            self.verified[index] = True
            self.have[index] = fully
        return True

    def read_piece(self, index: int) -> bytes | None:
        if index < 0 or index >= len(self.have) or not self.have[index]:
            return None
        return self.read_unchecked(index)

    def read_unchecked(self, index: int) -> bytes | None:
        if index < 0 or index >= self.meta.piece_count:
            return None
        start = index * self.meta.piece_length
        length = self.piece_size(index)
        out = bytearray(length)
        with self._lock:
            for torrent_file in self.meta.files:
                overlap_start = max(start, torrent_file.offset)
                overlap_end = min(start + length, torrent_file.offset + torrent_file.length)
                if overlap_start >= overlap_end:
                    continue
                if torrent_file.index not in self.selected or not torrent_file.safe:
                    return None
                try:
                    fd = self._open_payload(torrent_file, create=False)
                except (OSError, FileNotFoundError):
                    return None
                try:
                    os.lseek(fd, overlap_start - torrent_file.offset, os.SEEK_SET)
                    chunk = os.read(fd, overlap_end - overlap_start)
                finally:
                    os.close(fd)
                if len(chunk) != overlap_end - overlap_start:
                    return None
                out[overlap_start - start : overlap_end - start] = chunk
        return bytes(out)

    def file_complete(self, torrent_file: TorrentFile) -> bool:
        if torrent_file.length == 0:
            return torrent_file.index in self.selected
        first = torrent_file.offset // self.meta.piece_length
        last = (torrent_file.offset + torrent_file.length - 1) // self.meta.piece_length
        return all(self.verified[index] for index in range(first, last + 1))

    def selected_bytes_done(self) -> tuple[int, int]:
        done = 0
        total = 0
        for torrent_file in self.meta.files:
            if torrent_file.index not in self.selected:
                continue
            total += torrent_file.length
            if not torrent_file.length:
                continue
            first = torrent_file.offset // self.meta.piece_length
            last = (torrent_file.offset + torrent_file.length - 1) // self.meta.piece_length
            for index in range(first, last + 1):
                if not self.verified[index]:
                    continue
                start = index * self.meta.piece_length
                overlap_start = max(start, torrent_file.offset)
                overlap_end = min(
                    start + self.piece_size(index),
                    torrent_file.offset + torrent_file.length,
                )
                done += max(0, overlap_end - overlap_start)
        return done, total

    def questions(self, categories: dict | None = None) -> list[dict]:
        """Videos whose names do not say movie or TV show."""
        from .media import classify_path

        categories = categories or {}
        pending = []
        for torrent_file in self.meta.files:
            if torrent_file.index not in self.selected or torrent_file.index in self.published:
                continue
            if classify_path(torrent_file.path)[0] != "video":
                continue
            if categories.get(torrent_file.index) in {"movie", "series"}:
                continue
            chosen = parse_save_as(self.save_as) if self.save_as else {}
            if chosen.get("year") or chosen.get("season"):
                continue
            if category_for(torrent_file.path) == "unsure":
                pending.append({"index": torrent_file.index, "name": torrent_file.path, "category": "unsure"})
        return pending

    def publish_ready(self, layout: dict | None = None, categories: dict | None = None, *, allowed=None) -> list[tuple[int, str, str]]:
        from .media import classify_path

        layout = layout or {}
        categories = categories or {}
        profile = str(layout.get("profile") or "emby")
        folders = {"movies": layout.get("movies") or "Movies", "series": layout.get("series") or "TV Shows"}
        kinds = {item.index: classify_path(item.path)[0] for item in self.meta.files}
        primary = self._align(self._primary_relative(kinds, profile, folders, categories))
        created: list[tuple[int, str]] = []
        ready = []
        for torrent_file in self.meta.files:
            if torrent_file.index not in self.selected or torrent_file.index in self.published:
                continue
            if not self.file_complete(torrent_file):
                continue
            kind = kinds.get(torrent_file.index, "rejected")
            if kind == "rejected":
                continue
            relative = library_path(
                self.meta.name,
                torrent_file.path,
                kind,
                categories.get(torrent_file.index),
                profile,
                folders,
                self.save_as or None,
            )
            relative = self._align(relative)
            ready.append((torrent_file, kind, relative))
        stacked = stack_split_movies([(item.index, item.path, relative) for item, kind, relative in ready if kind == "video" and relative])
        with self._lock:
            for torrent_file, kind, relative in ready:
                if allowed is not None and not allowed(fresh=True):break
                if torrent_file.index in stacked:
                    relative = self._align(stacked[torrent_file.index])
                if relative is None:
                    if primary is None or kind in {"video", "audio"}:
                        continue
                    relative = companion_path(primary, torrent_file.path)
                placed = self._place(torrent_file, relative,allowed=allowed)
                if placed is None:break
                self.published[torrent_file.index] = placed
                created.append((torrent_file.index, placed, kind))
        return created

    def note_existing(self, layout: dict | None = None, categories: dict | None = None) -> None:
        """Remember videos already saved under their library name. Do not copy them."""
        from ... import m3u
        from .media import classify_path

        layout = layout or {}
        categories = categories or {}
        profile = str(layout.get("profile") or "emby")
        folders = {"movies": layout.get("movies") or "Movies", "series": layout.get("series") or "TV Shows"}
        kinds = {item.index: classify_path(item.path)[0] for item in self.meta.files}
        for torrent_file in self.meta.files:
            if torrent_file.index not in self.selected or torrent_file.index in self.published:
                continue
            kind = kinds.get(torrent_file.index, "rejected")
            if kind != "video":
                continue
            relative = self._align(library_path(self.meta.name, torrent_file.path, kind, categories.get(torrent_file.index), profile, folders, self.save_as or None))
            previous=self._align(previous_library_path(self.meta.name,torrent_file.path,kind,categories.get(torrent_file.index),profile,folders,self.save_as or None))
            adopted=existing_saved_path(self.media_base,relative,previous,torrent_file.length)
            if adopted:self.published[torrent_file.index]=adopted

    def _primary_relative(self, kinds: dict[int, str], profile: str, folders: dict, categories: dict) -> str | None:
        chosen = [
            item
            for item in self.meta.files
            if item.index in self.selected and kinds.get(item.index) in {"video", "audio"}
        ]
        if not chosen:
            return None
        videos = [item for item in chosen if kinds.get(item.index) == "video"]
        primary = max(videos or chosen, key=lambda item: item.length)
        return library_path(
            self.meta.name,
            primary.path,
            kinds[primary.index],
            categories.get(primary.index),
            profile,
            folders,
            self.save_as or None,
        )

    def _align(self, relative: str | None) -> str | None:
        if not relative:
            return relative
        return align_existing_folder(self.media_base, relative, self._folder_cache)

    def _place(self, torrent_file: TorrentFile, relative: str, *, allowed=None) -> str | None:
        parts = tuple(relative.split("/"))
        if any(part in {"", ".", ".."} for part in parts):
            raise UnsafePath(relative)
        with self._section():
            directory = open_directory(self.media_base, parts[:-1], create=True)
            try:
                leaf = parts[-1]
                candidate = leaf
                stem, dot, ext = leaf.rpartition(".")
                number = 2
                while True:
                    info = _lstat(directory, candidate)
                    if info is None:
                        break
                    source = self._open_payload(torrent_file, create=False)
                    try:
                        src_stat = os.fstat(source)
                    finally:
                        os.close(source)
                    same_file = info.st_ino == src_stat.st_ino and info.st_dev == src_stat.st_dev
                    if stat.S_ISREG(info.st_mode) and (same_file or info.st_size == torrent_file.length):
                        return "/".join((*parts[:-1], candidate))
                    base = stem if dot else leaf
                    suffix = f".{ext}" if dot else ""
                    candidate = f"{base} ({number}){suffix}"
                    number += 1
                source = self._open_payload(torrent_file, create=False)
                try:
                    if not self._link_or_copy(source,directory,candidate,allowed=allowed):return None
                finally:
                    os.close(source)
            finally:
                os.close(directory)
        return "/".join((*parts[:-1], candidate))

    def _link_or_copy(self, source: int, directory: int, name: str, *, allowed=None) -> bool:
        if allowed is not None and not allowed(fresh=True):return False
        try:
            os.link(f"/proc/self/fd/{source}",name,dst_dir_fd=directory)
        except OSError:
            pass
        else:
            if allowed is not None and not allowed(fresh=True):
                os.unlink(name,dir_fd=directory)
                return False
            return True
        dest=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644,dir_fd=directory)
        complete=False
        try:
            os.lseek(source,0,os.SEEK_SET)
            while True:
                if allowed is not None and not allowed():return False
                chunk=os.read(source,1024*1024)
                if not chunk:break
                view=memoryview(chunk)
                while view:
                    written=os.write(dest,view)
                    if written<=0:raise OSError('The downloaded file could not be saved.')
                    view=view[written:]
            if allowed is not None and not allowed(fresh=True):return False
            complete=True
            return True
        finally:
            os.close(dest)
            if not complete:os.unlink(name,dir_fd=directory)

    def remove_work(self) -> None:
        path = self.work_path()
        if path.exists() or path.is_symlink():
            remove_tree(path)
