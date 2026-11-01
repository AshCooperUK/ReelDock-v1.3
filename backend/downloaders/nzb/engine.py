"""Download NZB releases over NNTP and place the video with the torrent naming rules."""

from __future__ import annotations

from ...vpn.transport import nzb, subprocess_env

import fcntl
import hashlib
import json
import os
import re
import queue
import shutil
import sqlite3
import subprocess
import math
import stat
import threading
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from contextlib import contextmanager
from pathlib import Path

from ...settings.nzb import ENGINE_FIELDS as PUBLIC_FIELDS
from ..base import DownloadEngine
from ..torrent.bandwidth import TokenBucket
from ..torrent.media import COMPANION_KINDS, classify_path, is_sample
from ..torrent.rename import category_for, companion_path, library_path, parse_save_as, release_save_as, stack_split_movies, suggest_download
from . import archive, indexers, nzbxml, yenc
from .files import classify, is_archive, is_recovery
from .finish import prepare, unwrap_manifest
from .nntp import NntpClient, NntpError, NntpAuthenticationError, NntpConnectionBusy
from .par2 import Par2Error, repair


class EngineError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


_HANDOFF_LOCK_GUARD = threading.Lock()
_HANDOFF_LOCKS: dict[str, threading.Lock] = {}


@contextmanager
def _handoff_creation(directory: Path, token: str, job_id: str):
    """Serialize internal create-if-absent across threads and processes."""
    from ...m3u import contained
    contained(directory.parent, directory.name)
    acquired = []
    try:
        for name in sorted(('job-' + job_id, 'token-' + token)):
            path = directory / ('.handoff-' + name + '.lock')
            with _HANDOFF_LOCK_GUARD:
                lock = _HANDOFF_LOCKS.setdefault(str(path.absolute()), threading.Lock())
            lock.acquire()
            descriptor = None
            try:
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise OSError('The NZB handoff lock is unavailable.')
                os.fchmod(descriptor, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except BaseException:
                if descriptor is not None:
                    os.close(descriptor)
                lock.release()
                raise
            acquired.append((lock, descriptor))
        yield
    finally:
        for lock, descriptor in reversed(acquired):
            os.close(descriptor)
            lock.release()


DEFAULT_SETTINGS = {
    **{name: field.default for name, field in PUBLIC_FIELDS.items()},
    "servers": [],
    "indexers": [],
}


def _leaf(name: str) -> str:
    text = name.replace("\\", "/").rsplit("/", 1)[-1].replace("\x00", "").strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text:
        return ""
    return text[:180]


def _recognized(name: str) -> bool:
    leaf = _leaf(name)
    if not leaf:
        return False
    kind, _reason = classify_path(leaf)
    return kind != "rejected" or is_archive(leaf) or is_recovery(leaf)


def _preferred_name(posted: str, listed: str) -> str:
    """Keep a usable suffix when the article name is an extensionless hash."""
    article = _leaf(posted)
    nzb = _leaf(listed)
    if _recognized(article):
        return article
    if _recognized(nzb):
        return nzb
    return article or nzb


def _sniff_suffix(header: bytes) -> str:
    if header.startswith(b"\x1aE\xdf\xa3"):
        return ".webm" if b"webm" in header[:64].lower() else ".mkv"
    if header.startswith(b"PAR2\x00PKT"):
        return ".par2"
    if header.startswith(b"Rar!\x1a\x07"):
        return ".rar"
    if header.startswith(b"PK\x03\x04"):
        return ".zip"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return ".mp4"
    if header.startswith(b"ID3"):
        return ".mp3"
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"AVI ":
        return ".avi"
    return ""


def _apply_suffix(path: Path) -> Path:
    if _recognized(path.name) or not path.is_file():
        return path
    with path.open("rb") as handle:
        suffix = _sniff_suffix(handle.read(64))
    if not suffix:
        return path
    stem = _leaf(path.name)[:160]
    target = path.with_name(stem + suffix)
    if target.exists():
        target = path.with_name(f"{stem}-{path.parent.name[:8]}{suffix}")
    path.replace(target)
    return target


class _PullState:
    def __init__(self) -> None:
        self.hits = 0
        self.misses = 0
        self.stop = False
        self.inflight = 0
        self.group = None
        self.attempts: dict[tuple[int, int], int] = {}
        self.lock = threading.Lock()


@dataclass
class _Segment:
    number: int
    size: int
    message_id: str
    groups: tuple[str, ...]
    file_index: int = 0


@dataclass
class _File:
    index: int
    name: str
    groups: tuple[str, ...]
    segments: list[_Segment]


@dataclass
class _Preview:
    id: str
    name: str
    accepted: bool
    reason: str
    files: tuple
    parsed_files: tuple[_File, ...]
    found_name: str = ""
    found_checked: bool = False
    password: str = ""
    category: str = ""
    on_server: str = ""


class _Item:
    def __init__(self, index: int, path: str, length: int) -> None:
        self.index = index
        self.path = path
        self.length = length


class _Meta:
    def __init__(self, name: str, files: list[_Item]) -> None:
        self.name = name
        self.files = files


class _Storage:
    def __init__(self) -> None:
        self.published: dict[int, str] = {}


def _activity_identity(files,selected):
    """Content identity survives renamed NZBs, random job IDs and file order."""
    rows=[]
    for item in files:
        if item.index not in selected:continue
        rows.append(sorted((int(part.number),int(part.size),str(part.message_id).strip().strip('<>')) for part in item.segments))
    canonical=json.dumps(sorted(rows),ensure_ascii=False,separators=(',',':'))
    return 'native:nzb:'+hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class _Job:
    id: str
    name: str
    files: list[_File]
    selected: set[int]
    save_as: str = ""
    handoff_token: str = ""
    added: float = 0
    phase: str = "queued"
    progress_stage: str = ""
    error: str = ""
    stop_reason: str = ""
    user_paused: bool = False
    removed: bool = False
    running: bool = False
    categories: dict = field(default_factory=dict)
    metadata_choice: dict = field(default_factory=dict)
    metadata_done: set = field(default_factory=set)
    metadata_started: set = field(default_factory=set)
    metadata_note: str = ""
    lookup_provider: str = ""
    lookup_identity: str = ""
    minimum_minutes: int | None = None
    archive_password: str = ""
    category: str = ""
    ready: list[str] = field(default_factory=list)
    samples: list = field(default_factory=list)
    done_parts: int = 0
    total_parts: int = 0
    fetch_error: str = ""
    pullers: int = 0
    clients: list = field(default_factory=list)
    _clients: threading.Lock = field(default_factory=threading.Lock)
    worker: threading.Thread | None = None
    meta: _Meta | None = None
    storage: _Storage = field(default_factory=_Storage)
    engine: object = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def note(self, amount: int) -> None:
        now = time.monotonic()
        self.samples.append((now, amount))
        self.samples = [(stamp, size) for stamp, size in self.samples if now - stamp <= 5]

    @property
    def down_rate(self) -> float:
        # A provider may stop completing articles while its connections stay
        # open. Polling must expire samples even when note() is never called.
        # Keep a local snapshot so reads cannot erase a concurrent completion.
        now=time.monotonic()
        samples=[(stamp,size) for stamp,size in tuple(self.samples) if now-stamp<=5]
        if not samples:
            return 0
        span = max(0.2,samples[-1][0]-samples[0][0])
        return sum(size for _stamp,size in samples)/span


class _ConnectionBudget:
    """Share physical connections, retaining base leases between articles.

    Groups register once per download/server pass.  A new group changes the
    quotas immediately; workers above quota finish their current article, close
    their socket and yield.  Remainder leases rotate on article completion, so
    even a one-connection account makes progress on every active download.
    """

    def __init__(self) -> None:
        self._limit = 1
        self._used = 0
        self._anonymous = 0
        self._groups = {}
        self._leases = {}
        self._turn = 0
        self._provider_turn = {}
        self._cond = threading.Condition()

    @staticmethod
    def _share(groups, caps, total, turn):
        """Integer water filling, with a rotating remainder and capped demand."""
        quotas = {group: 0 for group in groups}
        remaining = max(0, total)
        while remaining:
            eligible = [group for group in groups if quotas[group] < caps[group]]
            if not eligible:
                break
            level = remaining // len(eligible)
            if not level:
                base = dict(quotas)
                ordered = groups[turn % len(groups):] + groups[:turn % len(groups)]
                for group in ordered:
                    if group in eligible and remaining:
                        quotas[group] += 1
                        remaining -= 1
                return quotas, base
            step = min(level, min(caps[group] - quotas[group] for group in eligible))
            for group in eligible:
                quotas[group] += step
            remaining -= step * len(eligible)
        return quotas, dict(quotas)

    def _quotas(self):
        # Demand callbacks are RAM-only, never cancellation/SQLite callbacks.
        groups = [group for group, row in self._groups.items() if row['demand']() > 0]
        caps = {group: min(self._groups[group]['ceiling'], self._groups[group]['demand']()) for group in groups}
        provider_base = {}
        providers = {}
        for group in groups:
            providers.setdefault(self._groups[group]['provider'], []).append(group)
        for provider, members in providers.items():
            ceiling = min(self._groups[group]['ceiling'] for group in members)
            shares, base = self._share(members, caps, ceiling - self._anonymous,
                                       self._provider_turn.get(provider, 0))
            caps.update(shares)
            provider_base.update(base)
        quotas, base = self._share(groups, caps, self._limit - self._anonymous, self._turn)
        return groups, providers, quotas, base, provider_base

    def register(self, group, server, demand):
        # Account/endpoint identity stays private in RAM; no credentials enter
        # public jobs or diagnostics.  Different jobs share the same account cap.
        provider = (server['host'], int(server['port']), bool(server['ssl']), server.get('username') or '')
        with self._cond:
            self._groups[group] = {'provider': provider, 'ceiling': int(server['connections']),
                                   'demand': demand, 'leases': []}
            self._cond.notify_all()

    def unregister(self, group):
        with self._cond:
            row = self._groups.pop(group, None)
            if row and not any(other['provider'] == row['provider'] for other in self._groups.values()):
                self._provider_turn.pop(row['provider'], None)
            self._cond.notify_all()

    def acquire_group(self, stop, group, *, allowed):
        while True:
            # _halted may wait for pause or read the hold database.  It must
            # never run under the allocator condition or reserve a socket.
            if stop.is_set() or not allowed():
                return None
            with self._cond:
                row = self._groups.get(group)
                if row is None:
                    return None
                _groups, _providers, quotas, _base, _provider_base = self._quotas()
                provider_used = sum(1 for owner in self._leases.values()
                                    if self._groups[owner]['provider'] == row['provider'])
                if (self._used < self._limit and len(row['leases']) < quotas.get(group, 0)
                        and provider_used + self._anonymous < row['ceiling']):
                    token = object()
                    self._leases[token] = group
                    row['leases'].append(token)
                    self._used += 1
                    return token
                self._cond.wait(.2)

    def retain(self, token):
        with self._cond:
            group = self._leases[token]
            _groups, _providers, quotas, _base, _provider_base = self._quotas()
            return self._groups[group]['leases'].index(token) < quotas.get(group, 0)

    def article_done(self, token):
        with self._cond:
            group = self._leases[token]
            groups, providers, quotas, base, provider_base = self._quotas()
            if group not in quotas:
                self._cond.notify_all()
                return
            row = self._groups[group]
            position = row['leases'].index(token)
            if base[group] <= position < quotas[group]:
                self._turn = groups.index(group) + 1
            members = providers[row['provider']]
            if provider_base[group] <= position < quotas[group]:
                self._provider_turn[row['provider']] = members.index(group) + 1
            self._cond.notify_all()

    def release_group(self, token):
        with self._cond:
            group = self._leases.pop(token)
            self._groups[group]['leases'].remove(token)
            self._used -= 1
            self._cond.notify_all()

    def set_limit(self, limit: int) -> None:
        with self._cond:
            self._limit = max(1, int(limit))
            self._cond.notify_all()

    def acquire(self,stop: threading.Event,*,timeout=None,ceiling=None,allowed=None) -> bool:
        deadline=time.monotonic()+timeout if timeout is not None else None
        while True:
            if stop.is_set() or allowed is not None and not allowed():return False
            with self._cond:
                limit=min(self._limit,max(1,int(ceiling))) if ceiling is not None else self._limit
                if self._used<limit:
                    self._used+=1
                    self._anonymous+=1
                    return True
                remaining=deadline-time.monotonic() if deadline is not None else None
                if remaining is not None and remaining<=0:return False
                self._cond.wait(timeout=min(.2,remaining) if remaining is not None else .2)

    def release(self) -> None:
        with self._cond:
            self._anonymous = max(0, self._anonymous - 1)
            self._used = max(0, self._used - 1)
            self._cond.notify()


def _regular_open(root, path):
    """Open a contained regular file without following any path component."""
    from ...m3u import contained
    root = Path(root).absolute()
    path = Path(path).absolute()
    relative = path.relative_to(root)
    if not relative.parts or '..' in relative.parts:
        raise ValueError('Prepared publication paths must stay inside their own directory.')
    contained(root, '')
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        for part in relative.parts[:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = following
        descriptor = os.open(relative.parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Prepared publication requires regular files.')
        result, descriptor = descriptor, None
        return result, info
    finally:
        os.close(directory)
        if descriptor is not None:
            os.close(descriptor)


def _publication_stamp(payload):
    # Metadata workers may enrich their own fields while a large copy runs.
    # They cannot change the acquisition identity, intent or prepared files.
    return {key: value for key, value in payload.items()
            if key not in {'metadata_note', 'metadata_done', 'metadata_started'}}


class _PublicationRecovery:
    def __init__(self, engine, job, payload):
        self.engine, self.job = engine, job
        self.version = payload.get('activity_generation', 0)
        self.expected = _publication_stamp(payload)
        self.files = []
        self.validated = False

    def validate(self, payload):
        job, engine = self.job, self.engine
        media = [{'index': item.index, 'path': item.path, 'length': item.length} for item in job.meta.files] if job.meta else []
        if (not media or len(media) > 10000 or payload.get('ready') != job.ready or payload.get('media') != media
                or any(type(item['length']) is not int for item in payload['media'])
                or len(job.ready) != len(media) or {item['index'] for item in media} != set(range(len(media)))
                or job.selected != set(range(len(media))) or payload.get('name') != job.name
                or payload.get('save_as', '') != job.save_as
                or payload.get('published', {}) != {str(key): value for key, value in job.storage.published.items()}
                or checkpoint_activity_key(payload) != engine.activity_key(job.id)):
            raise ValueError('Prepared NZB publication details changed. Existing files were kept.')
        prepared = engine.work_dir / job.id / 'media'
        for item in media:
            path = Path(job.ready[item['index']])
            # The normal writer retains empty NFO/subtitle/artwork sidecars.
            # Playable media still needs bytes, and every file remains bound
            # to its exact regular prepared path and recorded length.
            if (not path.is_absolute() or path.name != Path(item['path']).name
                    or type(item['length']) is not int or item['length'] < 0
                    or item['length'] == 0 and classify_path(item['path'])[0] not in COMPANION_KINDS):
                raise ValueError('Prepared NZB publication files are unavailable. Existing files were kept.')
            self.remember(prepared, path, item['length'])
        for index, relative in job.storage.published.items():
            if index not in range(len(media)) or not isinstance(relative, str) or Path(relative).is_absolute():
                raise ValueError('Saved NZB publication paths changed. Existing files were kept.')
            self.remember(engine.media_base, engine.media_base / relative, media[index]['length'])
        self.validated = True

    def remember(self, root, path, length):
        descriptor, info = _regular_open(root, path)
        os.close(descriptor)
        if info.st_size != length:
            raise ValueError('Prepared NZB publication size changed. Existing files were kept.')
        self.files.append((root, path, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)))

    def files_current(self):
        for root, path, expected in self.files:
            descriptor, info = _regular_open(root, path)
            os.close(descriptor)
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != expected:
                return False
        return True

    def allowed(self, fresh=False):
        job, engine = self.job, self.engine
        if (job.removed or job.user_paused or engine.stop.is_set() or engine.global_paused() or engine._acquisition_held()
                or getattr(job, 'activity_generation', 0) != self.version or engine._activity_held(job, fresh=fresh)
                or fresh and not engine._handoff_ready(job)):
            return False
        if not fresh:
            return True
        try:
            return _publication_stamp(engine._publication_state(job)) == self.expected and self.files_current()
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def publish(self, temporary, target, index, relative):
        from types import SimpleNamespace
        from ...media_holds import explicit_action
        # Clear/Resume use the same lock. Copying happens outside it; only the
        # last checks, exclusive link and its durable ownership record belong
        # here, before a later Clear can acknowledge stopping this job.
        deadline = time.monotonic() + 5
        while not self.job.removed and not self.job.user_paused and not self.engine.stop.is_set():
            with explicit_action(SimpleNamespace(directory=self.engine.data_dir.parent), blocking=False) as acquired:
                if acquired:
                    return self._publish_locked(temporary, target, index, relative)
            if time.monotonic() >= deadline:
                return False
            # Clear may be holding Action while joining this worker. Never
            # wait on that lock after its in-memory stop flags become visible.
            self.engine.stop.wait(.05)
        return False

    def _publish_locked(self, temporary, target, index, relative):
        if not self.allowed(fresh=True):
            return False
        os.link(temporary, target)
        linked = os.stat(target, follow_symlinks=False)
        previous = dict(self.job.storage.published)
        count = len(self.files)
        self.job.storage.published[index] = relative
        committed = False
        try:
            self.remember(self.engine.media_base, target, self.job.meta.files[index].length)
            committed = self.commit()
            if committed:
                return True
        finally:
            if not committed:
                # Remove only this newly linked inode, never a different
                # writer's destination. Prepared sources stay untouched.
                self.job.storage.published = previous
                del self.files[count:]
                try:
                    current = os.stat(target, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == (linked.st_dev, linked.st_ino):
                        target.unlink()
                except FileNotFoundError:
                    pass
        return False

    def commit(self, *, validate=True, **changes):
        """Preserve metadata and refuse stale checkpoint resurrection under its writer lock."""
        from ..job_state import _locked, _write
        job, engine = self.job, self.engine
        path = engine.state_dir / (job.id + '.json')
        try:
            with _locked(path):
                current = engine._publication_state(job)
                if (_publication_stamp(current) != self.expected or not self.allowed()
                        or validate and not self.files_current()):
                    return False
                current.update(changes)
                if self.validated:
                    current['published'] = {str(key): value for key, value in job.storage.published.items()}
                _write(path, current)
                self.expected = _publication_stamp(current)
            for key, value in changes.items():
                setattr(job, key, value)
            if 'phase' in changes:
                from ...activity_clear import native_generation
                native_generation(job, job.phase in {'stopped', 'failed'})
            job.metadata_note = str(current.get('metadata_note') or '')
            done = current.get('metadata_done')
            if isinstance(done, list):
                job.metadata_done.update(value for value in done if type(value) is int)
            return True
        except (OSError, ValueError, TypeError, KeyError):
            return False


class NzbEngine(DownloadEngine):
    source_type = "nzb"

    def __init__(self, media_base: Path, data_dir: Path, operations=None, global_paused=None) -> None:
        self.global_paused = global_paused or (lambda: False)
        self.media_base = Path(media_base)
        self.data_dir = Path(data_dir)
        self.state_dir = self.data_dir / "jobs"
        self.work_dir = self.data_dir / "work"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.operations = operations
        if operations is not None:
            operations.register(self)
        self.config = dict(DEFAULT_SETTINGS)
        self.config.update(self._read_settings())
        self.bucket = TokenBucket(int(self.config["download_limit_bps"]))
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}
        self._order: list[_Job] = []
        self._previews: dict[str, _Preview] = {}
        self._threads: list[threading.Thread] = []
        self._slots = _ConnectionBudget()
        self.layout = None
        self.metadata = None
        self._metadata_caught = False
        self._resume()
        if os.environ.get("REELDOCK_DOWNLOADS") != "worker":
            self._spawn(self._schedule, "reeldock-nzb")

    def shutdown(self) -> None:
        self.stop.set()
        for job in list(self._order):
            job.removed = True
            job.user_paused = True
        for thread in list(self._threads):
            thread.join(timeout=2)

    @nzb
    def preview(self, source: str) -> dict:
        text = source.strip()
        if not text.startswith(("http://", "https://")):
            raise EngineError("Paste an https address for an NZB file.")
        data = indexers._get(text, None)
        try:
            data, source_name = unwrap_manifest(data, text.rsplit("/", 1)[-1])
        except archive.ArchiveError as exc:
            raise EngineError(str(exc)) from exc
        if not data.startswith(b"<") and b"<nzb" not in data[:200].lower():
            raise EngineError("That address did not return an NZB file.")
        return self.preview_bytes(data, source_name)

    def preview_bytes(self, data: bytes, source: str = "nzb", *, check_availability: bool = True) -> dict:
        try:
            data, source = unwrap_manifest(data, source)
        except archive.ArchiveError as exc:
            raise EngineError(str(exc)) from exc
        try:
            parsed = nzbxml.parse(data, source)
        except nzbxml.NzbError as exc:
            raise EngineError(str(exc)) from exc
        rows = [(item.index, item.name, item.size) for item in parsed.files]
        accepted, reason, classified = classify(rows)
        files = tuple(
            _File(
                item.index,
                item.name,
                item.groups,
                [_Segment(segment.number, segment.size, segment.message_id, item.groups, item.index) for segment in item.segments],
            )
            for item in parsed.files
        )
        preview = _Preview(
            uuid.uuid4().hex, parsed.name, accepted, reason, classified, files,
            password=parsed.password, category=parsed.category, on_server=self._availability(data) if check_availability else "unchecked",
        )
        self._previews[preview.id] = preview
        return self._preview_dict(preview)

    def _availability(self, data: bytes) -> str:
        servers = self._servers()
        if not servers:
            return "no-server"
        try:
            return articles_on_server(servers,data,budget=self._slots,stop=self.stop)
        except (OSError, ValueError):
            return "unchecked"

    def preview_status(self, preview_id: str) -> dict:
        preview = self._previews.get(preview_id)
        if preview is None:
            raise EngineError("Unknown preview", 404)
        return self._preview_dict(preview)

    def preview_video(self, preview_id: str) -> tuple[str, str]:
        preview = self._previews.get(preview_id)
        if preview is None:
            raise EngineError("Unknown preview", 404)
        suggestion = self._suggestion(preview)
        path = self._naming_path(preview) or suggestion["name"]
        name = preview.found_name if preview.found_name and not suggestion["folder"] else suggestion["name"]
        return path or preview.name, name

    def start(self, preview_id: str, file_indexes: list[int] | None, title: str | None = None, *, lookup_provider: str = "", lookup_identity: str = "", minimum_minutes: int | None = None, handoff_token: str = "", job_id: str = "") -> dict:
        if handoff_token == "" and job_id == "":
            return self._start_new(preview_id, file_indexes, title, lookup_provider=lookup_provider,
                                   lookup_identity=lookup_identity, minimum_minutes=minimum_minutes)
        _handoff_arguments(job_id, handoff_token)
        content_key = self.preview_activity_key(preview_id, file_indexes)
        with _handoff_creation(self.state_dir, handoff_token, job_id):
            saved = _handoff_saved(self.data_dir, job_id, handoff_token, content_key)
            with self._lock:
                existing = self._jobs.get(job_id)
                if existing is not None:
                    if (existing.removed or existing.handoff_token != handoff_token
                            or self.activity_key(job_id) != content_key or saved is None):
                        raise EngineError('The saved NZB handoff identity changed.', 409)
                    return self._job_dict(existing)
                if saved is not None:
                    # Attach the checkpoint exactly as saved. Replay never
                    # resets counters, metadata, pause or terminal state, and
                    # does not itself launch a transfer or publication.
                    saved.engine = self
                    self._jobs[job_id] = saved
                    self._order.append(saved)
                    return self._job_dict(saved)
                if any(job.handoff_token == handoff_token for job in self._jobs.values()):
                    raise EngineError('This NZB handoff already belongs to another job.', 409)
            for path in self.state_dir.glob('*.json'):
                try:
                    payload = _read_handoff_payload(self.data_dir, path.stem)
                except (OSError, ValueError, TypeError, EngineError):
                    continue
                if payload is not None and payload.get('handoff_token') == handoff_token:
                    raise EngineError('This NZB handoff already belongs to another job.', 409)
            return self._start_new(preview_id, file_indexes, title, lookup_provider=lookup_provider,
                                   lookup_identity=lookup_identity, minimum_minutes=minimum_minutes,
                                   handoff_token=handoff_token, job_id=job_id)

    def _start_new(self, preview_id: str, file_indexes: list[int] | None, title: str | None = None, *, lookup_provider: str = "", lookup_identity: str = "", minimum_minutes: int | None = None, handoff_token: str = "", job_id: str = "") -> dict:
        if minimum_minutes is not None and (type(minimum_minutes) is not int or not 0<=minimum_minutes<=1440):
            raise EngineError('Choose a minimum length from 0 to 1440 minutes.')
        if self.stop.is_set() or self._acquisition_held():
            raise EngineError("Downloads are stopped. Resume downloads in Activity before starting another.", 409)
        if not self._servers():
            raise EngineError("Add a Usenet server in Settings → Downloads → NZB Downloads.", 409)
        preview = self._previews.get(preview_id)
        if preview is None:
            raise EngineError("Unknown preview", 404)
        if not preview.accepted:
            raise EngineError(preview.reason or "This NZB was rejected.")
        if preview.on_server == "missing":
            raise EngineError("This Usenet server does not have these articles.")
        chosen = self._selection(preview, file_indexes)
        from ...media_holds import native_blocked
        if native_blocked(self.data_dir,_activity_identity(preview.parsed_files,chosen),fresh=True):
            raise EngineError('This NZB was cleared. Use its explicit Start or Resume action to download it again.',409)
        size=sum(int(segment.size or 0) for item in preview.parsed_files if item.index in chosen for segment in item.segments)
        reason=self._size_refusal(size)
        if reason:raise EngineError(reason)
        save_as = release_save_as((title or "").strip() or self._suggestion(preview)["name"])
        job = _Job(
            id=job_id or uuid.uuid4().hex,
            handoff_token=handoff_token,
            name=preview.name or "NZB",
            files=[item for item in preview.parsed_files if item.index in chosen],
            selected=set(chosen),
            save_as=save_as[:180],
            lookup_provider=lookup_provider,
            lookup_identity=lookup_identity,
            minimum_minutes=minimum_minutes,
            added=time.time(),
            total_parts=sum(len(item.segments) for item in preview.parsed_files if item.index in chosen),
            archive_password=preview.password,
            category=preview.category,
        )
        job.engine = self
        with self._lock:
            # A recoverer may infer "no native side effect" from an absent
            # checkpoint. Publish it before any scheduler can see this job.
            self._save_job(job)
            if handoff_token:
                descriptor, _info = _regular_open(self.data_dir, self.state_dir / (job.id + '.json'))
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                directory = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            self._jobs[job.id] = job
            self._order.append(job)
        self._launch(job)
        return self._job_dict(job)

    def jobs(self) -> list[dict]:
        return [self._job_dict(job) for job in list(self._order) if not job.removed]

    def activity_key(self,job_id: str) -> str:
        job=self._require(job_id)
        return getattr(job,'_activity_key',None) or _activity_identity(job.files,{item.index for item in job.files})

    def preview_activity_key(self,preview_id: str,file_indexes=None) -> str:
        preview=self._previews.get(preview_id)
        if preview is None:raise EngineError('Unknown preview',404)
        if not preview.accepted:raise EngineError(preview.reason or 'This NZB was rejected.')
        return _activity_identity(preview.parsed_files,self._selection(preview,file_indexes))

    def _activity_held(self,job: _Job,*,fresh=False) -> bool:
        from ...media_holds import native_blocked
        key=getattr(job,'_activity_key',None)
        if key is None:key=job._activity_key=_activity_identity(job.files,{item.index for item in job.files})
        return native_blocked(self.data_dir,[key,'native:nzb-job:'+job.id],fresh=fresh)

    def job(self, job_id: str) -> dict:
        return self._job_dict(self._require(job_id))

    def update(self, job_id: str, patch: dict) -> dict:
        job = self._require(job_id)
        if "paused" in patch:
            if not patch['paused'] and self._activity_held(job,fresh=True):raise EngineError('This NZB was cleared. Use its explicit Resume action to download it again.',409)
            job.user_paused = bool(patch["paused"])
            if job.user_paused:
                job.phase = "paused"
            elif job.phase == "paused":
                job.phase = "queued"
                self._launch(job)
        if "placement" in patch:
            placement = patch["placement"]
            if not isinstance(placement, dict):
                raise EngineError("Choose a movie or a TV show.")
            try:
                index = int(placement.get("index"))
            except (TypeError, ValueError) as exc:
                raise EngineError("Choose a movie or a TV show.") from exc
            value = placement.get("value")
            if value not in {"movie", "series"}:
                raise EngineError("Choose a movie or a TV show.")
            if job.meta is None or index not in {item.index for item in job.meta.files}:
                raise EngineError("That file is not part of this download.")
            job.categories[index] = value
            self._publish(job)
        self._save_job(job)
        return self._job_dict(job)

    def remove(self, job_id: str, timeout: float = 20) -> None:
        job = self._require(job_id)
        job.removed = True
        job.user_paused = True
        with job._clients:
            clients = list(job.clients)
        for client in clients:
            client.abort()
        worker = job.worker
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout)
            if worker.is_alive():
                raise EngineError("This NZB is still stopping.", 409)
        self._drop_unfinished(job)

    def _drop_unfinished(self, job: _Job) -> None:
        """Remove this job's temporary files after its worker has stopped. Library files stay."""
        shutil.rmtree(self.work_dir / job.id, ignore_errors=True)
        (self.state_dir / f"{job.id}.json").unlink(missing_ok=True)
        (self.state_dir / f"{job.id}.lock").unlink(missing_ok=True)
        with self._lock:
            self._jobs.pop(job.id, None)
            if job in self._order:
                self._order.remove(job)

    def _acquisition_held(self) -> bool:
        from ...acquisition import file_held
        return file_held(self.data_dir)

    def _halted(self, job: _Job) -> bool:
        if self._activity_held(job):return True
        while self.global_paused() and not (job.removed or job.user_paused or self.stop.is_set()):
            self.stop.wait(0.2)
        return job.removed or job.user_paused or self.stop.is_set() or self._activity_held(job)

    def settings(self) -> dict:
        return {
            **{name: self.config[name] for name in PUBLIC_FIELDS},
            "servers": [_public_server(item) for item in self.config["servers"]],
            "indexers": [_public_indexer(item) for item in self.config["indexers"]],
        }

    def update_settings(self, patch: dict) -> dict:
        with self._lock:
            updated = dict(self.config)
            for name, field in PUBLIC_FIELDS.items():
                if name in patch:
                    number = patch[name] if isinstance(field.default, bool) or name in ("min_download_bytes","max_download_bytes","min_video_minutes") else _nonnegative(patch[name])
                    try:
                        field.validate(number)
                    except ValueError as exc:
                        raise EngineError(str(exc)) from exc
                    updated[name] = number
            if updated['min_download_bytes']>updated['max_download_bytes']:
                raise EngineError('Minimum download size must not exceed maximum download size.')
            if "servers" in patch:
                updated["servers"] = _merge_servers(self.config["servers"], patch["servers"])
            if "indexers" in patch:
                updated["indexers"] = _merge_indexers(self.config["indexers"], patch["indexers"])
            if "share_metadata" in patch:
                updated["metadata_generation"] = uuid.uuid4().hex
            # Commit the private file before publishing new runtime values. An interrupted
            # save must not truncate credentials or acknowledge unpersisted preferences.
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.data_dir,
                                                 prefix=".settings-", delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump(updated, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.data_dir / "settings.json")
            except OSError as exc:
                raise EngineError("Could not save NZB settings. Check the data directory.") from exc
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            self.config = updated
            if "share_metadata" in patch:
                self._metadata_caught = False
            self.bucket.set_rate(int(updated["download_limit_bps"]))
            return self.settings()

    def library_layout(self) -> dict:
        layout = self.layout() if callable(self.layout) else {}
        if not isinstance(layout, dict):
            layout = {}
        profile = layout.get("profile") if layout.get("profile") in {"emby", "plex", "kodi"} else "emby"
        return {"profile": profile, "movies": layout.get("movies") or "Movies", "series": layout.get("series") or "TV Shows"}

    def _size_refusal(self, size: int) -> str:
        minimum,maximum=self.config['min_download_bytes'],self.config['max_download_bytes']
        if minimum<=size<=maximum:return ''
        return f'NZB payload size ({size / 1_000_000:,.0f} MB) is outside the allowed range ({minimum / 1_000_000:,.0f}–{maximum / 1_000_000:,.0f} MB). Change NZB Settings to allow it.'

    def _handoff_ready(self, job: _Job) -> bool:
        """A durable grab cannot transfer before its exact Discover binding."""
        if not job.handoff_token:
            return True
        path = (self.data_dir.parent / 'reeldock.sqlite3').absolute()
        try:
            db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
            try:
                row = db.execute('''SELECT h.acquisition_generation,
                    (SELECT value FROM settings WHERE key='acquisition_generation')
                    FROM discover_handoffs h
                    JOIN discover_requests r ON r.id=h.request_id
                    WHERE h.token=? AND h.job_id=? AND h.engine='nzb'
                    AND h.stage='committed' AND r.engine='nzb' AND r.engine_id=?
                    AND h.stop_generation=IFNULL(r.stop_generation,0)
                    AND r.state NOT IN ('cancelled','cancelling') LIMIT 1''',
                                 (job.handoff_token, job.id, job.id)).fetchone()
                return row is not None and int(row[0]) == (0 if row[1] is None else int(row[1]))
            finally:
                db.close()
        except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
            return False

    def _launch(self, job: _Job) -> None:
        if not self._handoff_ready(job):
            return
        if job.progress_stage == 'publishing':
            self._launch_publication(job)
            return
        if self._activity_held(job,fresh=True):
            job.user_paused=True
            self._save_job(job)
            return
        # The worker records a grab. The web process is the one that connects.
        if os.environ.get("REELDOCK_DOWNLOADS") == "worker":
            with self._lock:
                job.running = False
                job.phase = "queued"
            self._save_job(job)
            return
        with self._lock:
            if job.running or job.removed or job.user_paused or self.stop.is_set():
                return
            reason=self._size_refusal(sum(int(segment.size or 0) for item in job.files for segment in item.segments))
            if reason:
                job.phase='failed'
                job.error=reason
                self._save_job(job)
                return
            if self._acquisition_held() or self.global_paused():
                job.phase = "queued"
                job.running = False
                return
            active = sum(1 for item in self._order if item.running)
            maximum = int(self.config["max_active_downloads"])
            if maximum > 0 and active >= maximum:
                job.phase = "queued"
                return
            if not self._claim_download(job):
                job.phase = "queued"
                return
            job.running = True
            job.phase = "download"
        self._save_job(job)
        job.worker = self._spawn(lambda: self._download(job), "reeldock-nzb-job")

    def _claim_download(self, job: _Job) -> bool:
        """One process downloads a job. A second copy would use another set of connections."""
        handle = (self.state_dir / f"{job.id}.lock").open("a")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        job._download_lock = handle
        return True

    def _release_download(self, job: _Job) -> None:
        handle = getattr(job, "_download_lock", None)
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        except OSError:
            pass
        job._download_lock = None

    def _schedule(self) -> None:
        while not self.stop.wait(0.4):
            self._adopt()
            self._resume_metadata()
            for job in list(self._order):
                if job.phase == 'download' and job.progress_stage == 'publishing' and not job.running:
                    self._launch_publication(job)
                if job.phase == "queued" and not job.user_paused and not job.removed:
                    self._launch(job)

    def _publication_state(self, job):
        if not re.fullmatch(r'[0-9a-f]{32}', job.id):
            raise ValueError('Invalid saved NZB publication identity.')
        descriptor, info = _regular_open(self.data_dir, self.state_dir / (job.id + '.json'))
        with os.fdopen(descriptor, 'rb') as handle:
            if info.st_size > 32 * 1024 * 1024:
                raise ValueError('Saved NZB publication details are too large.')
            raw = handle.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError('Saved NZB publication details are too large.')
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get('id') != job.id:
            raise ValueError('Saved NZB publication identity changed.')
        return payload

    def _launch_publication(self, job):
        """Resume prepared output only; never re-enter article acquisition."""
        if (not re.fullmatch(r'[0-9a-f]{32}', job.id)
                or not self._handoff_ready(job)
                or os.environ.get('REELDOCK_DOWNLOADS') == 'worker' or self.stop.is_set() or job.removed
                or job.user_paused or job.error or job.phase not in {'download', 'queued'}
                or job.progress_stage != 'publishing' or self._activity_held(job, fresh=True)
                or self._acquisition_held() or self.global_paused()
                or time.monotonic() < getattr(job, '_publication_after', 0)):
            return
        from ...m3u import contained
        try:
            contained(self.data_dir, 'jobs/' + job.id + '.lock')
        except (OSError, ValueError):
            return
        with self._lock:
            maximum = int(self.config['max_active_downloads'])
            if job.running or maximum > 0 and sum(item.running for item in self._order) >= maximum:
                return
            if not self._claim_download(job):
                return
            job.running = True
        recovery = None
        handed_off = False
        job._publication_after = time.monotonic() + 30
        try:
            payload = self._publication_state(job)
            if (payload.get('phase') not in {'download', 'queued'} or payload.get('progress_stage') != 'publishing'
                    or payload.get('user_paused') or payload.get('error')
                    or payload.get('activity_generation', 0) != getattr(job, 'activity_generation', 0)):
                return
            recovery = _PublicationRecovery(self, job, payload)
            recovery.validate(payload)
            if not recovery.allowed(fresh=True):
                return
            attempts = getattr(job, '_publication_attempts', 0)
            if attempts >= 3:
                recovery.commit(phase='failed', error='Publication could not finish safely. Prepared files were kept.', validate=False)
                return
            job._publication_attempts = attempts + 1
            job._publication_after = time.monotonic() + 30
            job.worker = self._spawn(lambda: self._recover_publication(job, recovery), 'reeldock-nzb-publication')
            handed_off = True
        except (OSError, ValueError, TypeError, KeyError, IndexError, RuntimeError):
            if recovery is not None:
                recovery.commit(phase='failed', error='Prepared publication could not be verified. Existing and staged files were kept.', validate=False)
        finally:
            if not handed_off:
                job.running = False
                self._release_download(job)

    def _recover_publication(self, job, recovery):
        try:
            self._publish(job, recovery=recovery)
        except (EngineError, OSError, ValueError) as exc:
            recovery.commit(phase='failed', error=str(exc)[:300] or 'Publication failed. Prepared files were kept.', validate=False)
        finally:
            job.running = False
            self._release_download(job)
            if job.removed and not self.stop.is_set():
                try:
                    current = self._publication_state(job)
                    if current.get('activity_generation', 0) == recovery.version:
                        self._drop_unfinished(job)
                except (OSError, ValueError, TypeError, KeyError):
                    pass

    def _resume_metadata(self) -> None:
        readiness=getattr(self,'metadata_ready',None)
        if callable(readiness):
            ready=bool(readiness());previous=getattr(self,'_metadata_ready_snapshot',None)
            self._metadata_ready_snapshot=ready
            if ready and previous is False:self._metadata_caught=False
        if not self.config["share_metadata"] or not callable(self.metadata):
            return
        jobs = list(self._jobs.values())
        if self._metadata_caught:
            pending = getattr(self, '_handoff_metadata_pending', set())
            jobs = [job for job in jobs if job.id in pending]
        self._metadata_caught = True
        self._handoff_metadata_pending = set()
        for job in jobs:
            if not self._handoff_ready(job):
                self._handoff_metadata_pending.add(job.id)
                continue
            if job.removed or job.meta is None or self._activity_held(job):
                continue
            rows = [(index, relative, classify_path(next((item.path for item in job.meta.files if item.index == index), ""))[0])
                    for index, relative in job.storage.published.items()]
            if rows:
                self.metadata(job, rows)

    def _adopt(self) -> None:
        """Pick up a grab the worker saved, without opening a second downloader."""
        known = set(self._jobs)
        for path in self.state_dir.glob("*.json"):
            if path.stem in known or not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            job = _job_from(payload)
            if job is None or job.phase != "queued" or job.user_paused:
                continue
            if self._activity_held(job,fresh=True):job.user_paused=True
            job.engine = self
            with self._lock:
                if job.id in self._jobs:
                    continue
                self._jobs[job.id] = job
                self._order.append(job)

    def _download(self, job: _Job) -> None:
        try:
            if any(is_sample(item.name) for item in job.files):
                job.files = [item for item in job.files if not is_sample(item.name)]
                job.total_parts = sum(len(item.segments) for item in job.files)
                if not job.files:
                    raise EngineError("No downloadable files remain after excluding samples.")
            self._set_stage(job, "downloading")
            self._fetch(job)
            if self._halted(job):
                if job.user_paused and not job.removed:
                    job.phase = "paused"
                return
            self._set_stage(job, "assembling")
            assembled, incomplete = self._assemble(job)
            if self._halted(job):
                return
            if incomplete:
                self._set_stage(job, "repairing")
                self._repair(job, assembled)
            if self._halted(job):
                return
            self._set_stage(job, "extracting")
            ready = self._unpack(job, assembled)
            if self._halted(job):
                return
            job.ready = [str(path) for path in ready]
            self._set_stage(job, "processing")
            self._describe_media(job, ready)
            if self._halted(job):
                return
            self._set_stage(job, "publishing")
            self._publish(job)
        except (EngineError, archive.ArchiveError, Par2Error, OSError, ValueError) as exc:
            message = str(exc)[:300] or "The NZB download failed."
            if message.startswith("The Usenet account is using all"):
                job.phase = "queued"
                job.error = ""
            else:
                job.phase = "failed"
                job.error = message
        finally:
            job.running = False
            self._release_download(job)
            if job.removed:
                self._drop_unfinished(job)
            else:
                self._save_job(job)

    def _fetch(self, job: _Job) -> None:
        # A new download has no parts yet. Checking every article first keeps the
        # job at 0% while another download holds the interpreter.
        pending = self._pending_segments(job)
        job.done_parts = job.total_parts - len(pending)
        job.fetch_error = ""
        saw_articles = False
        missing_message = ""
        auth_error = ""
        for server in self._servers():
            if (self.work_dir / job.id / "parts").is_dir():
                pending = [segment for segment in pending if not self._part_path(job, segment.file_index, segment.number).with_suffix(".json").is_file()]
            if not pending or self._halted(job):
                return
            job.fetch_error = ""
            state = _PullState()
            self._fetch_server(job, server, pending, state)
            if job.fetch_error:
                auth_error = job.fetch_error
            if state.hits:
                saw_articles = True
                missing_message = ""
                auth_error = ""
            elif state.stop:
                missing_message = "This Usenet server does not have these articles."
        pending = [segment for segment in pending if not self._part_path(job, segment.file_index, segment.number).with_suffix(".json").is_file()]
        if missing_message and not saw_articles and pending:
            raise EngineError(missing_message)
        if auth_error and pending and not saw_articles:
            raise EngineError(auth_error)
        if job.fetch_error and pending:
            raise EngineError(job.fetch_error)

    def _fetch_server(self, job: _Job, server: dict, pending: list, state: "_PullState") -> None:
        work: queue.Queue = queue.Queue()
        for segment in pending:
            work.put(segment)
        self._slots.set_limit(int(server["connections"]))
        state.group = object()
        def demand():
            if self.stop.is_set() or job.removed or job.user_paused or job.fetch_error or state.stop:
                return 0
            return work.qsize() + state.inflight
        self._slots.register(state.group, server, demand)
        workers = max(1,min(int(server["connections"]),len(pending)))
        threads = [threading.Thread(target=nzb(self._pull), args=(job, server, work, state), daemon=True) for _index in range(workers)]
        started = []
        try:
            for thread in threads:
                thread.start()
                started.append(thread)
        finally:
            for thread in started:
                thread.join()
            self._slots.unregister(state.group)

    def _pull(self, job: _Job, server: dict, work: queue.Queue, state: "_PullState") -> None:
        token = None
        client = None
        def close():
            nonlocal token, client
            if client is not None:
                try:
                    client.close()
                except OSError:
                    pass
                finally:
                    with job._clients:
                        if client in job.clients:
                            job.clients.remove(client)
                    with job._lock:
                        job.pullers = max(0, job.pullers - 1)
                    client = None
            # Do not offer the lease to another worker while its old socket is
            # still open, including the NNTP authentication/error paths.
            if token is not None:
                self._slots.release_group(token)
                token = None
        def allowed():
            return not state.stop and not job.fetch_error and not work.empty() and not self._halted(job)
        try:
            while allowed():
                if token is not None and not self._slots.retain(token):
                    close()
                if client is None:
                    token = self._slots.acquire_group(self.stop, state.group, allowed=allowed)
                    if token is None:
                        return
                    client = NntpClient(server["host"], int(server["port"]), bool(server["ssl"]), server.get("username") or "", server.get("password") or "")
                    client.checkpoint = lambda: not self._halted(job)
                    with job._clients:
                        job.clients.append(client)
                    with job._lock:
                        job.pullers += 1
                try:
                    segment = work.get_nowait()
                except queue.Empty:
                    return
                with state.lock:
                    state.inflight += 1
                reconnect = False
                try:
                    lines = client.body(segment.message_id, segment.groups)
                    data, name, offset = yenc.decode(lines)
                    if not self.bucket.consume(len(data), self.stop):
                        return
                    self._write_part(job, segment, data, name, offset)
                    job.note(len(data))
                    job.done_parts += 1
                    with state.lock:
                        state.hits += 1
                except (NntpError, yenc.YencError, OSError) as exc:
                    if isinstance(exc, NntpError) and (isinstance(exc, (NntpAuthenticationError, NntpConnectionBusy))
                            or exc.code in {481, 482, 502} or "rejected" in str(exc).lower()):
                        job.fetch_error = str(exc)
                        return
                    if isinstance(exc, NntpError) and exc.code in {420, 423, 430}:
                        with state.lock:
                            state.misses += 1
                            if state.hits == 0 and state.misses >= 40:
                                state.stop = True
                        continue
                    key = (getattr(segment, "file_index", 0), segment.number)
                    with state.lock:
                        state.attempts[key] = state.attempts.get(key, 0) + 1
                        again = state.attempts[key] < 4
                    if again:
                        work.put(segment)
                    reconnect = True
                finally:
                    with state.lock:
                        state.inflight -= 1
                    self._slots.article_done(token)
                if reconnect:
                    close()
        finally:
            close()

    def _write_part(self, job: _Job, segment, data: bytes, name: str, offset: int) -> None:
        if job.removed or self.stop.is_set():
            return
        folder = self.work_dir / job.id / "parts" / str(segment.file_index)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{segment.number:06d}.bin"
        temporary = target.with_suffix(".partial")
        temporary.write_bytes(data)
        os.replace(temporary, target)
        (folder / f"{segment.number:06d}.json").write_text(
            json.dumps({"offset": offset, "name": _leaf(name), "size": len(data)}),
            encoding="utf-8",
        )

    def _assemble(self, job: _Job) -> tuple[list[Path], bool]:
        if self._halted(job):
            return [], False
        folder = self.work_dir / job.id / "files"
        folder.mkdir(parents=True, exist_ok=True)
        written = []
        missing = False
        used_names: set[str] = set()
        for item in job.files:
            if self._halted(job):
                return written, missing
            parts = []
            for segment in item.segments:
                meta_path = self._part_path(job, item.index, segment.number).with_suffix(".json")
                blob = meta_path.with_suffix(".bin")
                if not meta_path.is_file() or not blob.is_file():
                    missing = True
                    continue
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                parts.append((int(meta.get("offset") or 0), segment.number, blob, _leaf(str(meta.get("name") or ""))))
            if not parts:
                continue
            offsets = [row[0] for row in parts]
            positioned = len(set(offsets)) == len(parts) and max(offsets) > 0
            parts.sort(key=lambda row: (row[0], row[1]) if positioned else (row[1], row[0]))
            posted = next((label for _offset, _number, _path, label in parts if label), "")
            canonical = _preferred_name(posted, item.name) or f"file-{item.index:04d}.bin"
            leaf = canonical if canonical not in used_names else f"{item.index:04d}-{canonical}"
            used_names.add(leaf)
            target = folder / leaf
            temporary = folder / f".{leaf}.reeldock-partial"
            cursor = 0
            with temporary.open("wb") as handle:
                for offset, _number, blob, _label in parts:
                    data = blob.read_bytes()
                    if positioned:
                        if offset > cursor:
                            missing = True
                            handle.write(b"\x00" * (offset - cursor))
                        handle.seek(offset)
                        handle.write(data)
                        cursor = max(cursor, offset + len(data))
                    else:
                        handle.write(data)
                        cursor += len(data)
            os.replace(temporary, target)
            if leaf == canonical:
                sibling = folder / f"{item.index:04d}-{canonical}"
                if sibling.is_file():
                    sibling.unlink()
            target = _apply_suffix(target)
            written.append(target)
        if missing and not any(is_recovery(path.name) or path.suffix.lower() == ".par2" for path in written):
            raise EngineError("An article is missing from the Usenet servers you added.")
        return written, missing

    def _repair(self, job: _Job, assembled: list[Path]) -> None:
        if self._halted(job):
            return
        par = [path for path in assembled if path.name.lower().endswith(".par2") and ".vol" not in path.name.lower() or path.name.lower().endswith(".par2")]
        par = [path for path in assembled if path.name.lower().endswith(".par2")]
        if not par:
            return
        try:
            repair(par[0].parent, par)
        except Par2Error:
            incomplete = [path for path in assembled if path.stat().st_size == 0]
            if incomplete:
                raise

    def _unpack(self, job: _Job, assembled: list[Path]) -> list[Path]:
        if self._halted(job):
            return []
        destination = self.work_dir / job.id / "media"
        destination.mkdir(parents=True, exist_ok=True)
        ready = []
        for path in prepare(assembled, destination, job.archive_password):
            if self._halted(job):
                return ready
            if is_sample(str(path.relative_to(self.work_dir / job.id))):
                # Only dispose of extracted files inside this job's work directory.
                if (self.work_dir / job.id).resolve() in path.resolve().parents:
                    path.unlink(missing_ok=True)
                continue
            leaf = _leaf(path.name)
            if not leaf:
                continue
            target = destination / leaf
            inside = destination in path.resolve().parents or path.resolve().parent == destination.resolve()
            if path.resolve() == target.resolve():
                ready.append(path)
                continue
            if target.exists():
                target = destination / f"{path.stem}-{job.id[:6]}{path.suffix}"
            if inside:
                path.replace(target)
            else:
                shutil.copyfile(path, target)
            ready.append(target)
        useful = [path for path in ready if classify_path(path.name)[0] in {"video", "audio", "subtitle", "artwork", "nfo"}]
        if not useful:
            raise EngineError("The NZB downloaded, but it did not contain a video or audio file.")
        return useful

    def _filter_videos(self, ready: list[Path], minimum_minutes: int | None = None) -> list[Path]:
        minimum = (self.config["min_video_minutes"] if minimum_minutes is None else minimum_minutes) * 60
        ready = [path for path in ready if not is_sample(path.name)]
        if not any(classify_path(path.name)[0] in {"video", "audio"} for path in ready):
            raise EngineError("No video or audio remains after excluding sample files.")
        videos = [path for path in ready if classify_path(path.name)[0] == "video"]
        accepted = []
        for path in videos:
            if minimum:
                try:
                    result = subprocess.run(
                        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                         "-of", "json", str(path)], capture_output=True, text=True,
                        timeout=30, check=True, env=subprocess_env('nzb'))
                    seconds = float(json.loads(result.stdout)["format"]["duration"])
                    if not math.isfinite(seconds) or seconds <= 0:
                        raise ValueError("Invalid duration")
                except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
                    raise EngineError("Could not verify video length; nothing was saved to the library.") from exc
                if seconds < minimum:
                    continue
            accepted.append(path)
        if videos and not accepted:
            raise EngineError("No video meets the minimum video length or all videos are release samples.")
        return [path for path in ready if path not in videos or path in accepted]

    def _describe_media(self, job: _Job, ready: list[Path]) -> None:
        ready = self._filter_videos(ready,job.minimum_minutes)
        items = []
        for index, path in enumerate(ready):
            items.append(_Item(index, path.name, path.stat().st_size))
        job.meta = _Meta(job.name, items)
        job.selected = {item.index for item in items}
        job.ready = [str(path) for path in ready]

    def _publish(self, job: _Job, *, recovery=None) -> None:
        def allowed(fresh=False):
            if recovery is not None:
                return recovery.allowed(fresh=fresh)
            return (not job.removed and not self.stop.is_set() and not self._activity_held(job, fresh=fresh)
                    and (not fresh or self._handoff_ready(job)))
        if not allowed(fresh=True) or self._halted(job) or job.meta is None:
            return
        allowed_paths = set(self._filter_videos([Path(path) for path in job.ready]))
        questions = self._questions(job)
        if questions:
            if recovery is not None:
                recovery.commit(phase='choose', stop_reason='')
                return
            job.phase = "choose"
            job.stop_reason = ""
            self._save_job(job)
            return
        created = []
        layout = self.library_layout()
        folders = {"movies": layout["movies"], "series": layout["series"]}
        primary = self._primary_relative(job, layout, folders)
        planned = []
        for item in job.meta.files:
            if item.index in job.storage.published:
                continue
            source = Path(job.ready[item.index])
            if source not in allowed_paths:
                continue
            kind = classify_path(item.path)[0]
            if kind == "rejected":
                continue
            if kind == "audio":
                relative = f"Downloads/NZB/Audio/{_leaf(item.path) or 'audio'}"
            elif kind == "video":
                relative = library_path(job.name, _name_for_library(job.name, item.path), kind, job.categories.get(item.index), layout["profile"], folders, release_save_as(job.save_as) or None, flat_episodes=job.lookup_provider == "tvdb" and layout["profile"] == "emby")
            elif primary:
                relative = companion_path(primary, item.path)
            else:
                continue
            planned.append((item, source, kind, relative))
        stacked = stack_split_movies([(item.index, item.path, relative) for item, _source, kind, relative in planned if kind == "video" and relative])
        for item, source, kind, relative in planned:
            if not allowed(fresh=True) or self._halted(job):
                return
            if item.index in stacked:
                relative = stacked[item.index]
            if not relative:
                continue
            publish = (lambda temporary, target: recovery.publish(temporary, target, item.index, relative)) if recovery is not None else None
            placed = _place(self.media_base, source, relative, allowed=allowed, exclusive=recovery is not None, publish=publish)
            if placed is None:return
            job.storage.published[item.index] = placed
            created.append((item.index, placed, kind))
        if recovery is not None:
            if not recovery.commit(phase='stopped', progress_stage='complete', stop_reason='Download finished', error=''):
                return
            # Optional metadata scheduling cannot hide a safely published file.
            if created and self.config['share_metadata'] and callable(self.metadata):
                try:
                    self.metadata(job, created)
                except Exception:
                    pass
            return
        job.phase = "stopped"
        job.stop_reason = "Download finished"
        job.error = ""
        self._save_job(job)
        hook = self.metadata
        if created and self.config["share_metadata"] and callable(hook):
            hook(job, created)
        self._set_stage(job, "complete")

    def _questions(self, job: _Job) -> list[dict]:
        pending = []
        chosen = parse_save_as(release_save_as(job.save_as)) if job.save_as else {}
        if chosen.get("year") or chosen.get("season"):
            return []
        for item in job.meta.files:
            if classify_path(item.path)[0] != "video" or job.categories.get(item.index) in {"movie", "series"}:
                continue
            if category_for(item.path) == "unsure":
                pending.append({"index": item.index, "name": item.path, "category": "unsure"})
        return pending

    def _primary_relative(self, job: _Job, layout: dict, folders: dict) -> str | None:
        videos = [item for item in job.meta.files if classify_path(item.path)[0] == "video"]
        if not videos:
            return None
        primary = max(videos, key=lambda item: item.length)
        return library_path(job.name, _name_for_library(job.name, primary.path), "video", job.categories.get(primary.index), layout["profile"], folders, release_save_as(job.save_as) or None, flat_episodes=job.lookup_provider == "tvdb" and layout["profile"] == "emby")

    def _completed_parts(self, job: _Job) -> int:
        found = 0
        for item in job.files:
            for segment in item.segments:
                if self._part_path(job, item.index, segment.number).with_suffix(".json").is_file():
                    found += 1
        return found

    def _pending_segments(self, job: _Job) -> list:
        if not (self.work_dir / job.id / "parts").is_dir():
            return [segment for item in job.files for segment in item.segments]
        return [
            segment for item in job.files for segment in item.segments
            if not self._part_path(job, item.index, segment.number).with_suffix(".json").is_file()
        ]

    def _part_path(self, job: _Job, index: int, number: int) -> Path:
        return self.work_dir / job.id / "parts" / str(index) / f"{number:06d}.bin"

    def _servers(self) -> list[dict]:
        rows = [item for item in self.config["servers"] if item.get("enabled", True) and item.get("host")]
        return sorted(rows, key=lambda item: (int(item.get("priority") or 0), str(item.get("name") or "")))

    def _selection(self, preview: _Preview, file_indexes: list[int] | None) -> set[int]:
        allowed = {item.index: item for item in preview.files}
        if file_indexes is None:
            return {index for index, item in allowed.items() if item.selected}
        chosen = set()
        for index in file_indexes:
            item = allowed.get(int(index))
            if item is None or not item.selectable:
                raise EngineError("Choose a video, archive, subtitle, or artwork file from the scan.")
            chosen.add(item.index)
        if not chosen:
            raise EngineError("Choose at least one file.")
        return chosen

    def _naming_path(self, preview: _Preview) -> str:
        """Prefer a release name that contains the episode when the video name is obfuscated."""
        from ..torrent.rename import _describe

        videos = [item for item in preview.files if item.kind == "video" and item.selectable]
        video = max(videos, key=lambda item: item.size).path if videos else ""
        release = preview.name or ""
        if video and _described(video):
            return video
        if release and _described(release):
            return release
        return video or release

    def _suggestion(self, preview: _Preview) -> dict:
        path = self._naming_path(preview)
        if not path:
            return {"name": "", "folder": ""}
        fake = type("File", (), {"kind": "video", "selectable": True, "size": 1, "path": path})()
        suggestion = suggest_download([fake], self.media_base, self.library_layout())
        videos = [item for item in preview.files if item.kind == "video" and item.selectable]
        video = max(videos, key=lambda item: item.size).path if videos else ""
        if not video or not _described(video):
            suggestion = dict(suggestion)
            suggestion["name"] = _with_episode(suggestion.get("name") or "", path)
        return suggestion

    def _preview_dict(self, preview: _Preview) -> dict:
        suggestion = self._suggestion(preview)
        return {
            "id": preview.id,
            "status": "ready",
            "accepted": preview.accepted,
            "name": preview.name,
            "reason": preview.reason,
            "suggested_name": preview.found_name or suggestion["name"],
            "suggested_folder": suggestion["folder"],
            "on_server": preview.on_server,
            "files": [
                {"index": item.index, "path": item.path, "size": item.size, "kind": item.kind, "selected": item.selected, "selectable": item.selectable, "reason": item.reason}
                for item in preview.files
            ],
        }

    def _job_dict(self, job: _Job) -> dict:
        progress = 1.0 if job.phase == "stopped" and not job.error else (job.done_parts / job.total_parts if job.total_parts else 0)
        # The public pause state and explanation use one cached gate sample.
        # Plain legacy callables keep their existing boolean-only behavior.
        snapshot = getattr(self.global_paused, 'snapshot', None)
        try:
            decision = snapshot() if callable(snapshot) else None
            if not isinstance(decision, dict) or type(decision.get('paused')) is not bool or decision.get('reason') not in ('', 'downloads', 'hold', 'vpn'):
                decision = dict(paused=bool(self.global_paused()), reason='')
        except Exception:
            decision = dict(paused=True, reason='')
        global_pause = decision['paused'] and job.phase in {'queued', 'download'}
        state = 'paused' if global_pause else _state(job)
        pause_reason = ('user' if job.user_paused else decision['reason'] if global_pause else '') if state == 'paused' else ''
        files = []
        if job.meta is not None:
            files = [
                {"index": item.index, "path": item.path, "size": item.length, "kind": classify_path(item.path)[0], "selected": True, "selectable": True, "reason": ""}
                for item in job.meta.files
            ]
        result = {
            "id": job.id,
            "activity_generation": getattr(job, 'activity_generation', 0),
            "name": release_save_as(job.save_as) or job.save_as or job.name,
            "state": state,
            "pause_reason": pause_reason,
            "progress": progress,
            "total_bytes": sum(segment.size for item in job.files for segment in item.segments),
            "error": job.error,
            "stop_reason": job.stop_reason,
            "down_rate": job.down_rate,
            "up_rate": 0,
            "peers": 0,
            "connections": job.pullers if job.phase == "download" else 0,
            "servers": [server.get("name") or server.get("host") for server in self._servers()],
            "files": files,
            "placements": self._questions(job) if job.phase == "choose" and job.meta is not None else [],
            "metadata_note": job.metadata_note,
            "progress_stage": job.progress_stage,
            "added": job.added,
            "published": list(job.storage.published.values()),
            "limits": {"download_limit_bps": int(self.config["download_limit_bps"]), "max_active_downloads": int(self.config["max_active_downloads"])},
        }
        from ...activity_observation import capture
        result['activity_observation']=capture('nzb',result)
        return result

    def _require(self, job_id: str) -> _Job:
        job = self._jobs.get(job_id)
        if job is None or job.removed:
            raise EngineError("Unknown NZB", 404)
        return job

    def _set_stage(self, job: _Job, stage: str) -> None:
        job.progress_stage = stage
        self._save_job(job)

    def _save_job(self, job: _Job) -> None:
        if job.removed:
            return
        from ...activity_clear import native_generation
        activity_generation = native_generation(job, job.phase in {'stopped', 'failed'})
        cleaned = release_save_as(job.save_as)
        if cleaned and cleaned != job.save_as:
            job.save_as = cleaned
        payload = {
            "activity_generation": activity_generation,
            "id": job.id,
            "name": job.name,
            "save_as": job.save_as,
            "added": job.added,
            "phase": job.phase,
            "progress_stage": job.progress_stage,
            "error": job.error,
            "stop_reason": job.stop_reason,
            "user_paused": job.user_paused,
            "selected": sorted(job.selected),
            "categories": {str(key): value for key, value in job.categories.items()},
            "metadata_choice": job.metadata_choice,
            "metadata_done": sorted(job.metadata_done),
            "metadata_note": job.metadata_note,
            "lookup_provider": job.lookup_provider,
            "lookup_identity": job.lookup_identity,
            "minimum_minutes": job.minimum_minutes,
            "archive_password": job.archive_password,
            "category": job.category,
            "ready": job.ready,
            "published": {str(key): value for key, value in job.storage.published.items()},
            "done_parts": job.done_parts,
            "total_parts": job.total_parts,
            "files": [
                {
                    "index": item.index,
                    "name": item.name,
                    "groups": list(item.groups),
                    "segments": [
                        {"number": segment.number, "size": segment.size, "message_id": segment.message_id, "groups": list(segment.groups), "file_index": item.index}
                        for segment in item.segments
                    ],
                }
                for item in job.files
            ],
        }
        if job.meta is not None:
            payload["media"] = [{"index": item.index, "path": item.path, "length": item.length} for item in job.meta.files]
        if job.handoff_token:
            payload['handoff_token'] = job.handoff_token
        path = self.state_dir / f"{job.id}.json"
        from ..job_state import save
        save(path,payload)

    def _resume(self) -> None:
        for path in sorted(self.state_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            job = _job_from(payload)
            if job is None:
                continue
            if self._activity_held(job,fresh=True):job.user_paused=True
            job.engine = self
            self._jobs[job.id] = job
            self._order.append(job)
            found = self._completed_parts(job)
            if found:
                job.done_parts = found
            if os.environ.get("REELDOCK_DOWNLOADS") != "worker" and job.phase in {"download", "queued"} and not job.user_paused:
                job.phase = "queued"

    def _read_settings(self) -> dict:
        path = self.data_dir / "settings.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _spawn(self, target, name: str) -> threading.Thread:
        thread = threading.Thread(target=nzb(target), name=name, daemon=True)
        self._threads.append(thread)
        thread.start()
        return thread


def _handoff_arguments(job_id, token):
    if not all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value) for value in (job_id, token)):
        raise EngineError('NZB handoff requires a paired saved job identity and token.')


def _read_handoff_payload(data_dir, job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r'[0-9a-f]{32}', job_id):
        raise EngineError('The saved NZB handoff identity is invalid.', 409)
    data_dir = Path(data_dir)
    try:
        descriptor, info = _regular_open(data_dir, data_dir / 'jobs' / (job_id + '.json'))
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, 'rb') as handle:
        if info.st_size > 32 * 1024 * 1024:
            raise ValueError('The saved NZB handoff checkpoint is too large.')
        raw = handle.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError('The saved NZB handoff checkpoint is too large.')
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError('The saved NZB handoff checkpoint is invalid.')
    return payload


def _handoff_saved(data_dir, job_id, token, content_key):
    _handoff_arguments(job_id, token)
    if not isinstance(content_key, str) or not re.fullmatch(r'native:nzb:[0-9a-f]{64}', content_key):
        raise EngineError('The saved NZB handoff content identity is invalid.', 409)
    try:
        payload = _read_handoff_payload(data_dir, job_id)
        if payload is None:
            return None
        if (payload.get('id') != job_id or payload.get('handoff_token') != token
                or payload.get('phase') not in {'queued', 'download', 'paused', 'choose', 'failed', 'stopped'}):
            raise ValueError('The saved NZB handoff identity changed.')
        job = _job_from(payload)
        if (job is None or not job.files or not all(item.segments for item in job.files)
                or _activity_identity(job.files, {item.index for item in job.files}) != content_key):
            raise ValueError('The saved NZB handoff content identity changed.')
        return job
    except (OSError, ValueError, TypeError, KeyError, OverflowError, AttributeError, IndexError) as exc:
        raise EngineError('The saved NZB handoff checkpoint could not be verified.', 409) from exc


def handoff_checkpoint(data_dir, job_id, token, content_key):
    """Read one exact internal handoff without constructing an engine or writing.

    Absence alone returns None. Corruption, symlinks and identity mismatches
    fail closed, so a recoverer cannot mistake uncertainty for a fresh start.
    No saved article IDs, archive password or custom metadata leave this read.
    """
    job = _handoff_saved(data_dir, job_id, token, content_key)
    if job is None:
        return None
    return dict(id=job.id, handoff_token=job.handoff_token, activity_key=content_key,
                name=release_save_as(job.save_as) or job.save_as or job.name,
                phase=job.phase, state='paused' if job.user_paused else _state(job),
                progress=1.0 if job.phase == 'stopped' and not job.error else (job.done_parts / job.total_parts if job.total_parts else 0),
                user_paused=job.user_paused, activity_generation=getattr(job, 'activity_generation', 0))


def checkpoint_activity_key(payload: dict) -> str:
    """Content identity from an already bounded, checked private checkpoint.

    Never constructs an engine or touches files, SQLite, settings or providers.
    The legacy restore parser annotates segment dictionaries, so its input is
    defensively copied to keep this helper a pure read.
    """
    from copy import deepcopy
    job=_job_from(deepcopy(payload))
    if job is None or not job.files or not all(item.segments for item in job.files):
        raise EngineError('The saved NZB identity is unavailable.')
    return _activity_identity(job.files,{item.index for item in job.files})


def _job_from(payload: dict) -> _Job | None:
    if not isinstance(payload, dict) or not payload.get("id"):
        return None
    if 'handoff_token' in payload and (not isinstance(payload['handoff_token'], str)
                                     or not re.fullmatch(r'[0-9a-f]{32}', payload['handoff_token'])):
        # A damaged internal receipt must never become an ordinary unguarded
        # job. Missing tokens alone identify legacy/user-created checkpoints.
        return None
    files = []
    for item in payload.get("files") or []:
        segments = [
            _Segment(int(segment["number"]), int(segment.get("size") or 0), str(segment["message_id"]), tuple(segment.get("groups") or item.get("groups") or []))
            for segment in item.get("segments") or []
        ]
        for segment in item.get("segments") or []:
            segment["file_index"] = item["index"]
        files.append(_File(int(item["index"]), str(item.get("name") or "file.bin"), tuple(item.get("groups") or []), segments))
    # Attach file_index onto segments for resume fetches.
    flat = []
    for item in files:
        for segment in item.segments:
            flat.append(segment)
            segment_holder = segment
            del segment_holder
    job = _Job(
        id=str(payload["id"]),
        name=str(payload.get("name") or "NZB"),
        files=files,
        selected={int(item) for item in payload.get("selected") or []},
        save_as=str(payload.get("save_as") or ""),
        handoff_token=payload.get('handoff_token', ''),
        added=float(payload.get("added") or 0),
        phase=str(payload.get("phase") or "queued"),
        progress_stage=str(payload.get("progress_stage") or ""),
        error=str(payload.get("error") or ""),
        stop_reason=str(payload.get("stop_reason") or ""),
        user_paused=bool(payload.get("user_paused")),
        metadata_choice=payload.get("metadata_choice") if isinstance(payload.get("metadata_choice"), dict) else {},
        metadata_done={int(item) for item in payload.get("metadata_done") or []},
        metadata_note=str(payload.get("metadata_note") or ""),
        lookup_provider=str(payload.get("lookup_provider") or ""),
        lookup_identity=str(payload.get("lookup_identity") or ""),
        minimum_minutes=payload.get('minimum_minutes') if type(payload.get('minimum_minutes')) is int and 0<=payload['minimum_minutes']<=1440 else None,
        archive_password=str(payload.get("archive_password") or ""),
        category=str(payload.get("category") or ""),
        ready=[str(item) for item in payload.get("ready") or []],
        done_parts=int(payload.get("done_parts") or 0),
        total_parts=int(payload.get("total_parts") or 0),
    )
    from ...activity_clear import restore_native
    restore_native(job, payload, job.phase in {'stopped', 'failed'})
    categories = {}
    for key, value in (payload.get("categories") or {}).items():
        try:
            categories[int(key)] = value
        except (TypeError, ValueError):
            continue
    job.categories = categories
    published = {}
    for key, value in (payload.get("published") or {}).items():
        try:
            published[int(key)] = str(value)
        except (TypeError, ValueError):
            continue
    job.storage.published = published
    media = payload.get("media")
    if isinstance(media, list) and media:
        job.meta = _Meta(job.name, [_Item(int(item["index"]), str(item["path"]), int(item["length"])) for item in media])
    # Segments need file_index for _fetch after resume.
    class _Bound:
        def __init__(self, file_index: int, segment: _Segment) -> None:
            self.file_index = file_index
            self.number = segment.number
            self.size = segment.size
            self.message_id = segment.message_id
            self.groups = segment.groups

    for item in job.files:
        item.segments = [_Bound(item.index, segment) for segment in item.segments]
    del flat
    return job


def _state(job: _Job) -> str:
    if job.user_paused and job.phase in {"queued", "download", "paused"}:
        return "paused"
    if job.phase == "download":
        return "downloading"
    if job.phase == "paused":
        return "paused"
    if job.phase == "choose":
        return "choose"
    if job.phase == "failed":
        return "failed"
    if job.phase == "queued":
        return "queued"
    return "stopped"


def _described(path: str) -> bool:
    from ..torrent.rename import _describe

    info = _describe(path)
    return bool(info.get("season") or info.get("episode") or info.get("year") or info.get("day"))


def _with_episode(title: str, path: str) -> str:
    from ..torrent.rename import _describe

    info = _describe(path)
    if not info.get("season") or not info.get("episode"):
        return title
    code = f"S{int(info['season']):02d}E{int(info['episode']):02d}{info.get('extra') or ''}"
    if code.casefold() in str(title or "").casefold():
        return title
    trimmed = re.sub(r"(?i)\s+Season\s+\d{1,4}$", "", str(title or "")).strip()
    return f"{trimmed} {code}".strip()


def _name_for_library(release: str, file_path: str) -> str:
    """Use the NZB release name when the saved video name does not say what it is."""
    if _described(file_path):
        return file_path
    if not release or not _described(release):
        return file_path
    suffix = Path(file_path).suffix
    leaf = _leaf(release) or "video"
    if suffix and not leaf.lower().endswith(suffix.lower()):
        leaf = _leaf(f"{leaf}{suffix}") or leaf
    return leaf


def articles_on_server(servers: list[dict],raw: bytes,*,budget=None,stop=None) -> str:
    """Ask the Usenet servers whether they have a few articles from this NZB."""
    samples = _article_samples(nzbxml.parse(raw, "result.nzb"))
    if not samples or not servers:
        return "unchecked"
    partial = False
    answered = False
    stop=stop if stop is not None else threading.Event()
    for server in servers:
        if stop.is_set():break
        # A preview must not open an extra socket beyond active downloads, or
        # block the interface waiting for an entire job to release its slots.
        if budget is not None and not budget.acquire(stop,timeout=0,ceiling=int(server.get('connections') or 1)):continue
        client=None
        try:
            client=NntpClient(server['host'],int(server['port']),bool(server['ssl']),
                server.get('username') or '',server.get('password') or '',timeout=8)
            client.checkpoint=lambda:not stop.is_set()
            found=sum(1 for groups,message_id in samples if client.stat(message_id,groups))
        except (NntpError,OSError):
            continue
        finally:
            if client is not None:client.close()
            if budget is not None:budget.release()
        answered = True
        if found == len(samples):
            return "available"
        if found:
            partial = True
    if partial:
        return "partial"
    return "missing" if answered else "unchecked"


def _article_samples(parsed) -> list[tuple[tuple[str, ...], str]]:
    rows = []
    for item in parsed.files:
        for segment in item.segments:
            if segment.message_id:
                rows.append((item.groups, segment.message_id))
    if not rows:
        return []
    count = len(rows)
    picks = {0, count // 2, count - 1}
    if count > 6:
        picks.update({count // 4, (3 * count) // 4})
    chosen = []
    seen = set()
    for index in sorted(picks):
        groups, message_id = rows[index]
        if message_id in seen:
            continue
        seen.add(message_id)
        chosen.append((groups, message_id))
    return chosen


def _release_name(name: str) -> str:
    leaf = _leaf(name) or "download"
    if Path(leaf).suffix.lower() in {".mkv", ".mp4", ".avi", ".m4v"}:
        return leaf
    return leaf + ".mkv"


def _place(base: Path, source: Path, relative: str, *, allowed=None, exclusive=False, publish=None) -> str | None:
    from ... import m3u

    if source.is_symlink() or not source.is_file():
        raise EngineError("A downloaded file could not be saved.")
    parts = tuple(relative.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise EngineError("The save location is not valid.")
    directory = m3u.contained(base, "/".join(parts[:-1]))
    directory.mkdir(parents=True, exist_ok=True)
    leaf = parts[-1]
    stem, dot, ext = leaf.rpartition(".")
    number = 2
    while True:
        candidate = directory / leaf
        if not candidate.exists():
            break
        if exclusive:
            raise EngineError('The publication destination already exists. Existing and prepared files were kept.')
        if not candidate.is_file() or candidate.is_symlink():
            raise EngineError("The save location is not valid.")
        if candidate.stat().st_size == source.stat().st_size:
            return "/".join((*parts[:-1], leaf))
        if _is_file_prefix(candidate, source):
            break
        base_name = stem if dot else leaf
        suffix = f".{ext}" if dot else ""
        leaf = f"{base_name} ({number}){suffix}"
        number += 1
    if not _copy_into_place(directory,leaf,source,allowed=allowed,exclusive=exclusive,publish=publish):return None
    return "/".join((*parts[:-1],leaf))


def _is_file_prefix(candidate: Path, source: Path) -> bool:
    """A shorter file is the start of this download when a save was interrupted."""
    candidate_size = candidate.stat().st_size
    if candidate_size <= 0 or candidate_size >= source.stat().st_size:
        return False
    remaining = candidate_size
    with candidate.open("rb") as left, source.open("rb") as right:
        while remaining:
            chunk = min(1024 * 1024, remaining)
            if left.read(chunk) != right.read(chunk):
                return False
            remaining -= chunk
    return True


def _copy_into_place(directory: Path, leaf: str, source: Path, *, allowed=None, exclusive=False, publish=None) -> bool:
    """Write a private partial; Clear can stop copying before final publication."""
    if allowed is not None and not allowed(fresh=True):return False
    temporary=directory/(f'.{leaf}.reeldock-{uuid.uuid4().hex}.partial' if exclusive else f'.{leaf}.reeldock-partial')
    temporary.unlink(missing_ok=True)
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL
    if hasattr(os,"O_NOFOLLOW"):flags|=os.O_NOFOLLOW
    descriptor=os.open(temporary,flags,0o644)
    try:
        with source.open("rb") as incoming,os.fdopen(descriptor,"wb") as outgoing:
            descriptor=-1
            while True:
                if allowed is not None and not allowed():return False
                chunk=incoming.read(1024*1024)
                if not chunk:break
                outgoing.write(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if allowed is not None and not allowed(fresh=True):return False
        if publish is not None:
            return publish(temporary, directory / leaf)
        if exclusive:
            os.link(temporary, directory / leaf)
        else:
            os.replace(temporary,directory/leaf)
        return True
    finally:
        if descriptor>=0:os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _public_server(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "name": item.get("name") or "",
        "host": item.get("host") or "",
        "port": int(item.get("port") or 119),
        "ssl": bool(item.get("ssl")),
        "username": item.get("username") or "",
        "password_saved": bool(item.get("password")),
        "connections": int(item.get("connections") or 4),
        "priority": int(item.get("priority") or 0),
        "enabled": bool(item.get("enabled", True)),
    }


def _public_indexer(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "name": item.get("name") or "",
        "url": item.get("url") or "",
        "api_key_saved": bool(item.get("api_key")),
        "enabled": bool(item.get("enabled", True)),
    }


def _merge_servers(saved: list, incoming) -> list[dict]:
    if not isinstance(incoming, list) or len(incoming) > 20:
        raise EngineError("Keep the Usenet server list to 20 or fewer.")
    previous = {item.get("id"): item for item in saved if isinstance(item, dict)}
    merged = []
    for raw in incoming:
        if not isinstance(raw, dict):
            raise EngineError("Check the Usenet server details.")
        prior = previous.get(str(raw.get("id") or "")) or {}
        host = str(raw.get("host") or "").strip().lower()
        if not host or len(host) > 253 or any(mark in host for mark in " /@\\"):
            raise EngineError("Enter the Usenet server address.")
        try:
            port = int(raw.get("port") or (563 if raw.get("ssl") else 119))
        except (TypeError, ValueError) as exc:
            raise EngineError("Enter a Usenet port.") from exc
        if not 1 <= port <= 65535:
            raise EngineError("Enter a Usenet port.")
        connections = _bounded(raw.get("connections") or 4, 1, 60, "Use 1 to 60 connections for a server.")
        password = str(raw.get("password") or "")
        if not password:
            password = str(prior.get("password") or "")
        if len(password) > 200 or len(str(raw.get("username") or "")) > 200:
            raise EngineError("The Usenet username or password is too long.")
        merged.append(
            {
                "id": str(raw.get("id") or "") or uuid.uuid4().hex,
                "name": str(raw.get("name") or host)[:80],
                "host": host,
                "port": port,
                "ssl": bool(raw.get("ssl")),
                "username": str(raw.get("username") or "")[:200],
                "password": password,
                "connections": connections,
                "priority": _bounded(raw.get("priority") or 0, 0, 99, "Use a priority from 0 to 99."),
                "enabled": bool(raw.get("enabled", True)),
            }
        )
    return merged


def _merge_indexers(saved: list, incoming) -> list[dict]:
    if not isinstance(incoming, list) or len(incoming) > 30:
        raise EngineError("Keep the indexer list to 30 or fewer.")
    previous = {item.get("id"): item for item in saved if isinstance(item, dict)}
    merged = []
    for raw in incoming:
        if not isinstance(raw, dict):
            raise EngineError("Check the indexer details.")
        prior = previous.get(str(raw.get("id") or "")) or {}
        try:
            url = indexers.api_base(str(raw.get("url") or ""))
        except indexers.IndexerError as exc:
            raise EngineError(str(exc)) from exc
        key = str(raw.get("api_key") or "")
        if not key:
            key = str(prior.get("api_key") or "")
        if not key:
            raise EngineError("Enter the indexer API key.")
        if len(key) > 200:
            raise EngineError("That API key is too long.")
        merged.append(
            {
                "id": str(raw.get("id") or "") or uuid.uuid4().hex,
                "name": str(raw.get("name") or urllib_host(url))[:80],
                "url": url,
                "api_key": key,
                "enabled": bool(raw.get("enabled", True)),
            }
        )
    return merged


def urllib_host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).hostname or "Indexer"


def _nonnegative(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise EngineError("Enter a number that is zero or more.") from exc
    if number < 0 or number > 10_000_000_000:
        raise EngineError("Enter a smaller number.")
    return number


def _bounded(value, low: int, high: int, message: str) -> int:
    number = _nonnegative(value)
    if number < low or number > high:
        raise EngineError(message)
    return number
