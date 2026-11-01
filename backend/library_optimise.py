"""Explicit, single-file library conversions; originals are never modified."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path, PurePosixPath

KINDS = {'tv': 'REELDOCK_TV_LIBRARY', 'movies': 'REELDOCK_MOVIE_LIBRARY'}
PROFILES = {'hevc': ('libx265', 24), 'h264': ('libx264', 22)}
MEDIA = {'.mkv', '.mp4', '.m4v', '.mov', '.avi', '.ts', '.m2ts', '.mpg', '.mpeg', '.webm'}
ACTIVE = ('preparing', 'encoding', 'validating')
MAX_SCAN = 1000
MAX_SIZE_ENTRIES = 100000
MAX_SIZE_SECONDS = 8.0
MAX_SIZE_DEPTH = 64
MAX_QUEUE = 20
PROGRESS_INTERVAL = 1.5
PROGRESS_COLUMNS = ('started', 'phase_started', 'duration_seconds', 'processed_seconds',
                    'progress_percent', 'fps', 'speed', 'progress_updated')


class OptimiseError(ValueError):
    pass


class Cancelled(Exception):
    pass


def _relative(value, *, empty=False):
    if not isinstance(value, str) or len(value) > 2048 or '\\' in value or any(ord(c) < 32 for c in value):
        raise OptimiseError('Choose a valid library path.')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ('.', '..') or part.startswith('.') for part in path.parts):
        raise OptimiseError('Choose a file inside the configured library.')
    if not value and empty:
        return ''
    if not value or str(path) != value:
        raise OptimiseError('Choose a valid library path.')
    return value


def _signature(info):
    return ':'.join(str(value) for value in (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))


def _absolute(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise OptimiseError('Choose an absolute path without parent-directory references.')
    return Path('/' + str(path).lstrip('/'))


def _open_absolute(path, *, create=False, allow_missing=False):
    """Anchor absolute directories at /; never follow any symlink component."""
    path = _absolute(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open('/', flags)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if allow_missing:
                    os.close(descriptor)
                    return None
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError:
        os.close(descriptor)
        raise OptimiseError('A configured directory is unavailable or contains a symbolic link.') from None


def _check_absolute(path, *, allow_missing=False):
    descriptor = _open_absolute(path, allow_missing=allow_missing)
    if descriptor is not None:
        os.close(descriptor)


def _source_root(app, kind):
    if not isinstance(kind, str) or kind not in KINDS:
        raise OptimiseError('Choose TV Shows or Movies.')
    raw = app.config.get(KINDS[kind], os.environ.get(KINDS[kind], ''))
    if not isinstance(raw, str) or not raw or not Path(raw).is_absolute():
        raise OptimiseError('This library path has not been configured.')
    root = _absolute(raw)
    try:
        _check_absolute(root)
    except OptimiseError:
        raise OptimiseError('This library is not available. Check its path mapping.')
    return root


@contextmanager
def _open_source(app, kind, relative, *, directory=False):
    """Every path component is opened without following symlinks."""
    root = _source_root(app, kind)
    relative = _relative(relative, empty=directory)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = _open_absolute(root)
    try:
        for index, part in enumerate(PurePosixPath(relative).parts):
            child = os.open(part, flags | (os.O_DIRECTORY if directory or index < len(PurePosixPath(relative).parts) - 1 else 0), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise OptimiseError('Choose a regular library file.')
        if not directory and (Path(relative).suffix.lower() not in MEDIA or info.st_size <= 0):
            raise OptimiseError('Choose a supported non-empty video file.')
        yield descriptor, info, root
    except OSError:
        raise OptimiseError('The selected path is unavailable or contains a symbolic link.') from None
    finally:
        os.close(descriptor)


def _database(store):
    db = sqlite3.connect(store.directory / 'library-optimisation.sqlite3', timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, path TEXT NOT NULL, root TEXT NOT NULL,
        signature TEXT NOT NULL, fingerprint TEXT NOT NULL, source_bytes INTEGER NOT NULL,
        source_hash TEXT NOT NULL DEFAULT '', profile TEXT NOT NULL, stereo INTEGER NOT NULL DEFAULT 0,
        limit1080 INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL,
        error TEXT NOT NULL DEFAULT '', output TEXT NOT NULL DEFAULT '', output_bytes INTEGER NOT NULL DEFAULT 0,
        temporary_root TEXT NOT NULL DEFAULT '', temporary TEXT NOT NULL DEFAULT '',
        cancel INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL,
        UNIQUE(fingerprint,profile))''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
    additions = {'temporary_root': "TEXT NOT NULL DEFAULT ''", 'temporary': "TEXT NOT NULL DEFAULT ''",
                 'stereo': 'INTEGER NOT NULL DEFAULT 0', 'limit1080': 'INTEGER NOT NULL DEFAULT 0',
                 'destination_mode': "TEXT NOT NULL DEFAULT 'appdata'", 'destination_name': "TEXT NOT NULL DEFAULT ''",
                 'validated_output_hash': "TEXT NOT NULL DEFAULT ''",
                 **{column: 'REAL DEFAULT NULL' for column in PROGRESS_COLUMNS}}
    if not additions.keys() <= columns:
        db.execute('BEGIN IMMEDIATE')
        columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
        for column, declaration in additions.items():
            if column not in columns:
                db.execute('ALTER TABLE jobs ADD COLUMN ' + column + ' ' + declaration)
    from .library_publication import initialise as initialise_publication
    initialise_publication(db)
    db.commit()
    return db


@contextmanager
def _db(store):
    db = _database(store)
    try:
        with db:
            yield db
    finally:
        db.close()


def _enabled(store):
    return store.get('library_optimise_enabled') == 'true'


def _output_root(app, store):
    raw = app.config.get('REELDOCK_OPTIMISE_OUTPUT', os.environ.get('REELDOCK_OPTIMISE_OUTPUT', ''))
    root = _absolute(raw if raw else store.directory / 'library-optimisation' / 'outputs')
    _check_absolute(root, allow_missing=True)
    for kind in KINDS:
        try:
            source = _source_root(app, kind)
        except OptimiseError:
            continue
        if root == source or source in root.parents:
            raise OptimiseError('The output folder must be outside the source libraries.')
    return root


def status(app, store):
    from . import library_publication as publication
    roots = []
    for kind in KINDS:
        try:
            root = str(_source_root(app, kind))
            roots.append(dict(kind=kind, available=True, path=root, message=''))
        except OptimiseError as exc:
            roots.append(dict(kind=kind, available=False, path='', message=str(exc)))
        roots[-1].update(publication.output_status(app, kind))
    with _db(store) as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 50')]
        requests = {row['job_id']: dict(row) for row in db.execute('SELECT * FROM publication_requests WHERE job_id IN (' + ','.join('?' for _ in rows) + ')', [row['id'] for row in rows])} if rows else {}
        jobs = []
        for row in rows:
            job = {key: row[key] for key in ('id', 'kind', 'path', 'profile', 'stereo', 'limit1080', 'state', 'error',
                                             'source_bytes', 'output_bytes', 'output', 'created', 'updated')}
            job.update(_telemetry_dto(app, store, row))
            job.update(publication.dto(app, store, row, requests.get(row['id']), available=next(library['output_available'] for library in roots if library['kind'] == row['kind'])))
            for option in ('stereo', 'limit1080'):
                job[option] = bool(job[option])
            jobs.append(job)
    try:
        output = str(_output_root(app, store))
        output_error = ''
    except OptimiseError as exc:
        output, output_error = '', str(exc)
    return dict(enabled=_enabled(store), libraries=roots, jobs=jobs, output_root=output,
                output_error=output_error, tools_available=bool(shutil.which('ffmpeg') and shutil.which('ffprobe')))


def _number(value, *, maximum=None):
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 and (maximum is None or value <= maximum) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _owned_temporary(job):
    return (job['kind'] in KINDS and isinstance(job['id'], str) and re.fullmatch(r'[0-9a-f]{32}', job['id'])
            and isinstance(job['temporary'], str) and re.fullmatch(
                re.escape(job['kind'] + '/' + job['id']) + r'-[0-9a-f]{8}/\.encoding\.mkv', job['temporary']))


def _live_output_bytes(app, store, job):
    """Read directory/stat metadata only; status never opens a media payload."""
    if job['state'] not in ACTIVE or not _owned_temporary(job):
        return None
    descriptors = []
    try:
        root = _output_root(app, store)
        if str(root) != job['temporary_root']:
            return None
        descriptors.append(_open_absolute(root))
        for part in PurePosixPath(job['temporary']).parts[:-1]:
            descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                       dir_fd=descriptors[-1]))
        info = os.stat('.encoding.mkv', dir_fd=descriptors[-1], follow_symlinks=False)
        return info.st_size if stat.S_ISREG(info.st_mode) else None
    except (OSError, OptimiseError):
        return None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _telemetry_dto(app, store, job):
    now = time.time()
    active = job['state'] in ACTIVE
    started = _number(job.get('started'))
    phase_started = _number(job.get('phase_started'))
    end = now if active or job['state'] == 'queued' else job['updated']
    baseline = started if started is not None else _number(job['created'])
    result = {key: _number(job.get(key)) for key in ('duration_seconds', 'processed_seconds', 'progress_percent', 'fps', 'speed', 'progress_updated')}
    result.update(elapsed_seconds=max(0, end - baseline) if baseline is not None else None,
                  elapsed_basis='started' if started is not None else 'created' if baseline is not None else None,
                  phase_elapsed_seconds=max(0, end - phase_started) if phase_started is not None else None,
                  eta_seconds=None, live_output_bytes=_live_output_bytes(app, store, job))
    if active:
        result['progress_percent'] = min(99.9, result['progress_percent']) if result['progress_percent'] is not None else None
    elif job['state'] == 'complete':
        result['progress_percent'] = 100.0
    if (job['state'] in ('encoding', 'validating') and result['progress_updated'] is not None
            and 0 <= now - result['progress_updated'] <= 5 and result['speed']
            and result['duration_seconds'] is not None and result['processed_seconds'] is not None):
        result['eta_seconds'] = _number(max(0, result['duration_seconds'] - result['processed_seconds']) / result['speed'])
    return result


class _SizeBudget:
    def __init__(self):
        self.remaining = MAX_SIZE_ENTRIES
        self.deadline = time.monotonic() + MAX_SIZE_SECONDS

    def available(self):
        return self.remaining > 0 and time.monotonic() < self.deadline

    def take(self):
        if not self.available():
            return False
        self.remaining -= 1
        return True


def _folder_size(parent, name, budget, depth=0):
    """Sum regular-file lengths through anchored directory fds, never file reads."""
    if not budget.available():
        return dict(bytes=None, complete=False, reason='limit')
    if depth >= MAX_SIZE_DEPTH:
        return dict(bytes=None, complete=False, reason='depth')
    descriptor = None
    total, measured, reason = 0, False, ''
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if not budget.take():
                    reason = reason or 'limit'
                    break
                try:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode):
                        total += info.st_size
                        measured = True
                    elif stat.S_ISDIR(info.st_mode):
                        child = _folder_size(descriptor, entry.name, budget, depth + 1)
                        if child['bytes'] is not None:
                            total += child['bytes']
                            measured = True
                        if not child['complete']:
                            reason = 'unreadable' if child['reason'] == 'unreadable' else reason or child['reason']
                except OSError:
                    reason = 'unreadable'
    except OSError:
        reason = 'unreadable'
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return dict(bytes=total if measured or not reason else None, complete=not bool(reason), reason=reason)


def scan(app, store, kind, folder=''):
    """An explicit bounded listing with stat-only, bounded descendant size checks."""
    folder = _relative(folder, empty=True)
    files, folders, sizes, truncated, unreadable = [], [], {}, False, 0
    with _open_source(app, kind, folder, directory=True) as (descriptor, _info, _root):
        with os.scandir(descriptor) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_SCAN:
                    truncated = True
                    break
                if entry.name.startswith('.'):
                    continue
                relative = '/'.join(part for part in (folder, entry.name) if part)
                try:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        folders.append(relative)
                    elif stat.S_ISREG(info.st_mode) and Path(entry.name).suffix.lower() in MEDIA and info.st_size > 0:
                        files.append(dict(path=relative, bytes=info.st_size))
                except OSError:
                    unreadable += 1
        budget = _SizeBudget()
        for relative in sorted(folders, key=str.casefold):
            sizes[relative] = _folder_size(descriptor, PurePosixPath(relative).name, budget)
    return dict(kind=kind, folder=folder, files=sorted(files, key=lambda row: row['path'].casefold()),
                folders=sorted(folders), folderSizes=sizes, truncated=truncated, unreadableEntries=unreadable)


def enqueue(app, store, kind, path, profile, *, stereo=False, limit1080=False):
    from . import library_publication as publication
    if not _enabled(store):
        raise OptimiseError('Enable library optimisation before queuing a file.')
    if not isinstance(profile, str) or profile not in PROFILES:
        raise OptimiseError('Choose a supported video profile.')
    if type(stereo) is not bool or type(limit1080) is not bool:
        raise OptimiseError('Choose on or off for stereo and the 1080p limit.')
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise OptimiseError('FFmpeg and FFprobe are required for library optimisation.')
    _output_root(app, store)
    path = _relative(path)
    planned = publication.plan(app, kind, path, profile)
    with _open_source(app, kind, path) as (_descriptor, info, root):
        signature = _signature(info)
    identity = str(root) + '\0' + path + '\0' + signature
    if stereo or limit1080:
        identity += '\0' + json.dumps(dict(stereo=stereo, limit1080=limit1080), sort_keys=True, separators=(',', ':'))
    legacy_fingerprint = hashlib.sha256(identity.encode()).hexdigest()
    fingerprint = hashlib.sha256((identity + '\0adjacent\0' + json.dumps(planned, sort_keys=True)).encode()).hexdigest()
    with _db(store) as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute("SELECT * FROM jobs WHERE (fingerprint=? OR (fingerprint=? AND destination_mode='appdata')) AND profile=? ORDER BY created LIMIT 1", (fingerprint, legacy_fingerprint, profile)).fetchone()
        if previous:
            if (not previous['cancel'] and previous['state'] in ('queued', *ACTIVE, 'complete')
                    and not db.execute('SELECT 1 FROM publication_requests WHERE job_id=?', (previous['id'],)).fetchone()):
                publication._insert(db, previous['id'], planned, 'queued' if previous['state'] == 'complete' else 'waiting')
            return previous['id']
        if db.execute("SELECT count(*) FROM jobs WHERE state='queued' OR state IN ('preparing','encoding','validating')").fetchone()[0] >= MAX_QUEUE:
            raise OptimiseError('The optimisation queue is full. Wait for existing jobs to finish.')
        job_id, now = uuid.uuid4().hex, time.time()
        db.execute('''INSERT INTO jobs(id,kind,path,root,signature,fingerprint,source_bytes,profile,stereo,limit1080,state,created,updated,destination_mode,destination_name)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (job_id, kind, path, str(root), signature, fingerprint, info.st_size, profile, int(stereo), int(limit1080), 'queued', now, now, 'adjacent', planned['name']))
        publication._insert(db, job_id, planned, 'waiting')
    return job_id


def cancel(store, job_id):
    if not isinstance(job_id, str):
        raise OptimiseError('Choose an optimisation job.')
    with _db(store) as db:
        row = db.execute('SELECT state FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise OptimiseError('That optimisation job no longer exists.')
        if row['state'] == 'queued':
            db.execute("UPDATE jobs SET state='cancelled',cancel=1,updated=? WHERE id=?", (time.time(), job_id))
        elif row['state'] in ACTIVE:
            db.execute('UPDATE jobs SET cancel=1,updated=? WHERE id=?', (time.time(), job_id))


def retry(app, store, job_id):
    if not _enabled(store):
        raise OptimiseError('Enable library optimisation before retrying a file.')
    if not isinstance(job_id, str):
        raise OptimiseError('Choose an optimisation job.')
    with _db(store) as db:
        db.execute('BEGIN IMMEDIATE')
        job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not job or job['state'] not in ('failed', 'cancelled'):
            raise OptimiseError('Only failed or cancelled jobs can be retried.')
        with _open_source(app, job['kind'], job['path']) as (_descriptor, info, root):
            if str(root) != job['root'] or _signature(info) != job['signature']:
                raise OptimiseError('The source changed. Scan and queue the current file instead.')
        if db.execute("SELECT count(*) FROM jobs WHERE state='queued' OR state IN ('preparing','encoding','validating')").fetchone()[0] >= MAX_QUEUE:
            raise OptimiseError('The optimisation queue is full.')
        db.execute("UPDATE jobs SET state='queued',error='',cancel=0," + ','.join(column + '=NULL' for column in PROGRESS_COLUMNS)
                   + ',updated=? WHERE id=?', (time.time(), job_id))
        db.execute("UPDATE publication_requests SET state='waiting',error='',updated=? WHERE job_id=? AND cancel=0 AND state='cancelled'", (time.time(), job_id))


def configure(store, enabled):
    if type(enabled) is not bool:
        raise OptimiseError('Choose whether library optimisation is enabled.')
    store.set('library_optimise_enabled', 'true' if enabled else 'false')
    if not enabled:
        with _db(store) as db:
            db.execute("UPDATE jobs SET state='cancelled',cancel=1,updated=? WHERE state='queued'", (time.time(),))
            db.execute("UPDATE jobs SET cancel=1,updated=? WHERE state IN ('preparing','encoding','validating')", (time.time(),))


class _ProgressParser:
    """Accept only bounded numeric machine progress; never retain tool messages."""
    def __init__(self, report):
        self.report = report
        self.pending = b''
        self.values = {}

    def feed(self, chunk):
        self.pending += chunk
        while b'\n' in self.pending:
            line, self.pending = self.pending.split(b'\n', 1)
            if len(line) > 512:
                raise OptimiseError('The media progress response exceeded its safe limit. The original was kept.')
            key, _, raw = line.partition(b'=')
            raw = raw.rstrip(b'\r')
            if key == b'out_time_us':
                value = _number(raw)
                if value is not None:
                    self.values['processed_seconds'] = value / 1_000_000
            elif key in (b'fps', b'speed'):
                self.values[key.decode('ascii')] = _number(raw.rstrip(b'x'), maximum=1_000_000)
            elif key == b'progress':
                if raw in (b'continue', b'end'):
                    self.report(dict(self.values, finished=raw == b'end'))
                self.values = {}
        if len(self.pending) > 512:
            raise OptimiseError('The media progress response exceeded its safe limit. The original was kept.')


class _ProgressReporter:
    def __init__(self, store, job_id, duration):
        self.store, self.job_id, self.duration = store, job_id, duration
        self.last = None

    def start(self, phase):
        self.last = None
        _update(self.store, self.job_id, state=phase, phase_started=time.time(), duration_seconds=self.duration,
                progress_percent=None, processed_seconds=None, fps=None, speed=None, progress_updated=None)

    def report(self, values):
        now = time.monotonic()
        if self.last is not None and now - self.last < PROGRESS_INTERVAL and not values.get('finished'):
            return
        processed = values.get('processed_seconds')
        if processed is None:
            return
        _update(self.store, self.job_id, processed_seconds=processed,
                progress_percent=min(99.9, processed / self.duration * 100),
                fps=None if values.get('finished') else values.get('fps'),
                speed=None if values.get('finished') else values.get('speed'), progress_updated=time.time())
        self.last = now


def _run(command, checkpoint, timeout, descriptors=(), *, capture=False, progress=None):
    """Bounded output and process-group cancellation; never save raw tool errors."""
    checkpoint()
    read_fd = write_fd = None
    if progress is not None:
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        # The child writes blocking bounded progress to its private inherited
        # pipe. Only the parent reader is nonblocking; neither stdout nor logs
        # can contain the progress stream or media/source paths.
        os.set_blocking(write_fd, True)
        command = [command[0], '-nostats', '-stats_period', '1', '-progress', 'pipe:' + str(write_fd), *command[1:]]
        descriptors = (*descriptors, write_fd)
    try:
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output if capture else subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, pass_fds=descriptors, start_new_session=True)
            if write_fd is not None:
                os.close(write_fd)
                write_fd = None
            parser = _ProgressParser(progress) if progress is not None else None
            def read_progress():
                if parser is None:
                    return
                for _ in range(16):
                    try:
                        chunk = os.read(read_fd, 4096)
                    except BlockingIOError:
                        return
                    if not chunk:
                        return
                    parser.feed(chunk)
                raise OptimiseError('The media progress response exceeded its safe limit. The original was kept.')
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    checkpoint()
                    read_progress()
                    if time.monotonic() >= deadline or output.tell() > 4 * 1024 * 1024:
                        raise OptimiseError('The media tool exceeded its time or response limit. The original was kept.')
                    time.sleep(.2)
                checkpoint()
                read_progress()
                if process.returncode:
                    raise OptimiseError('The media tool could not process this file safely. The original was kept.')
                if capture:
                    output.seek(0)
                    data = output.read(4 * 1024 * 1024 + 1)
                    if len(data) > 4 * 1024 * 1024:
                        raise OptimiseError('The media information is too large to verify safely.')
                    return data
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=3)
    finally:
        for descriptor in (read_fd, write_fd):
            if descriptor is not None:
                os.close(descriptor)


def _probe(path, checkpoint, descriptors=()):
    try:
        return json.loads(_run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-show_streams', '-show_format', '-show_chapters', '-of', 'json', str(path)],
                               checkpoint, 45, descriptors, capture=True))
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        raise OptimiseError('The media information could not be read safely.') from None


def _inspect(probe, profile, stereo=False, limit1080=False):
    streams = probe.get('streams')
    if not isinstance(streams, list) or not 1 <= len(streams) <= 64:
        raise OptimiseError('The file has an unsupported number of tracks.')
    main = [index for index, stream in enumerate(streams) if stream.get('codec_type') == 'video' and not stream.get('disposition', {}).get('attached_pic')]
    if len(main) != 1:
        raise OptimiseError('Choose a file with one main video track. No tracks were removed.')
    index = main[0]
    video = streams[index]
    if any(type(video.get(key)) is not int or not 0 < video[key] <= 8192 for key in ('width', 'height')):
        raise OptimiseError('The video dimensions cannot be processed safely.')
    if video.get('pix_fmt') not in ('yuv420p', 'yuv420p10le') or profile == 'h264' and video.get('pix_fmt') != 'yuv420p':
        raise OptimiseError('This video colour format is unsupported by the selected profile. The original was kept.')
    if video.get('color_transfer') in ('smpte2084', 'arib-std-b67') or any(
            any(word in str(side.get('side_data_type', '')).lower() for word in ('dovi', 'mastering display', 'content light'))
            or float(side.get('rotation') or 0) != 0 for side in video.get('side_data_list', [])):
        raise OptimiseError('HDR, Dolby Vision and rotated videos are kept unchanged to preserve their presentation.')
    audio = {'aac', 'ac3', 'eac3', 'dts', 'flac', 'mp3', 'mp2', 'opus', 'vorbis', 'truehd', 'alac', 'pcm_s16le', 'pcm_s24le', 'pcm_s32le', 'pcm_f32le', 'pcm_f64le'}
    subtitles = {'subrip', 'ass', 'ssa', 'webvtt', 'dvd_subtitle', 'hdmv_pgs_subtitle'}
    for stream in streams:
        kind = stream.get('codec_type')
        if kind == 'audio' and stream.get('codec_name') in audio:
            if stereo:
                try:
                    rate = int(stream.get('sample_rate') or 0)
                    if rate not in {7350, 8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 64000, 88200, 96000}:
                        raise ValueError()
                    if type(stream.get('channels')) is not int or not 1 <= stream['channels'] <= 64:
                        raise ValueError()
                except (TypeError, ValueError):
                    raise OptimiseError('This audio format cannot be converted to stereo while retaining its sample rate. The original was kept.') from None
            continue
        if kind == 'subtitle' and stream.get('codec_name') in subtitles:
            continue
        if kind == 'video' or kind == 'attachment':
            continue
        raise OptimiseError('This file contains a track that cannot be preserved safely in MKV. No tracks were removed.')
    try:
        duration = float(probe['format']['duration'])
        if not math.isfinite(duration) or not 0 < duration <= 24 * 3600:
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise OptimiseError('The video duration could not be verified safely.') from None
    if limit1080:
        _geometry(video)
    return index, duration


def _aspect(value):
    try:
        result = Fraction(str(value).replace(':', '/'))
        return result if result > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def _geometry(video):
    width, height = video['width'], video['height']
    factor = min(Fraction(1), Fraction(1920, width), Fraction(1080, height))
    target_width = (width * factor.numerator // factor.denominator) // 2 * 2
    target_height = (height * factor.numerator // factor.denominator) // 2 * 2
    if target_width < 2 or target_height < 2:
        raise OptimiseError('This video cannot fit the 1080p limit without changing its presentation. The original was kept.')
    source_sar = _aspect(video.get('sample_aspect_ratio'))
    supplied_dar = _aspect(video.get('display_aspect_ratio'))
    if source_sar is None:
        if supplied_dar is None:
            raise OptimiseError('The source aspect ratio is unavailable. The original was kept.')
        source_sar = supplied_dar * Fraction(height, width)
    source_dar = Fraction(width, height) * source_sar
    if supplied_dar is not None and abs(float(source_dar / supplied_dar) - 1) > .005:
        raise OptimiseError('The source aspect-ratio metadata conflicts. The original was kept.')
    target_sar = source_dar * Fraction(target_height, target_width)
    return target_width, target_height, target_sar, source_dar


def _tags(value):
    ignored = {'encoder', 'major_brand', 'minor_version', 'compatible_brands', 'duration', 'bps', 'number_of_frames', 'number_of_bytes'}
    return {key.casefold(): str(item) for key, item in (value or {}).items()
            if key.casefold() not in ignored and not key.casefold().startswith('_statistics')}


def _validate(before, after, main, duration, profile, stereo=False, limit1080=False):
    streams = before['streams']
    result = after.get('streams', [])
    if len(streams) != len(result):
        raise OptimiseError('Output track validation failed. The original was kept.')
    for index, (source, output) in enumerate(zip(streams, result)):
        is_audio = source.get('codec_type') == 'audio'
        fields = ('codec_type',) + (() if index == main or is_audio and stereo else ('codec_name',))
        if is_audio:
            fields += ('sample_rate',) if stereo else ('channels', 'sample_rate')
        if any(source.get(key) != output.get(key) for key in fields):
            raise OptimiseError('An output track did not preserve its format. The original was kept.')
        if index == main and output.get('codec_name') != ('hevc' if profile == 'hevc' else 'h264'):
            raise OptimiseError('The output video did not use the selected codec. The original was kept.')
        if is_audio and stereo and (output.get('codec_name') != 'aac' or output.get('channels') != 2):
            raise OptimiseError('The output did not retain every audio track as two-channel AAC stereo. The original was kept.')
        for key, value in _tags(source.get('tags')).items():
            if _tags(output.get('tags')).get(key) != value:
                raise OptimiseError('Output track metadata validation failed. The original was kept.')
        if any(bool(value) != bool(output.get('disposition', {}).get(key)) for key, value in source.get('disposition', {}).items()):
            raise OptimiseError('Output track flags changed. The original was kept.')
        if index == main:
            if limit1080:
                width, height, expected_sar, expected_dar = _geometry(source)
                output_sar = _aspect(output.get('sample_aspect_ratio'))
                output_dar = _aspect(output.get('display_aspect_ratio'))
                if (output.get('width'), output.get('height')) != (width, height):
                    raise OptimiseError('The output did not fit the saved 1080p dimensions. The original was kept.')
                if (output_sar is None or abs(float(output_sar / expected_sar) - 1) > .001
                        or abs(float(Fraction(width, height) * output_sar / expected_dar) - 1) > .005
                        or output_dar is not None and abs(float(output_dar / expected_dar) - 1) > .005):
                    raise OptimiseError('The output aspect ratio changed. The original was kept.')
            elif (source.get('width'), source.get('height')) != (output.get('width'), output.get('height')):
                raise OptimiseError('Output video dimensions changed. The original was kept.')
            for key in ('color_range', 'color_space', 'color_transfer', 'color_primaries') + (() if limit1080 else ('sample_aspect_ratio',)):
                if source.get(key) not in (None, 'unknown', '0:1') and source.get(key) != output.get(key):
                    raise OptimiseError('Output video colour or aspect metadata changed. The original was kept.')
    try:
        actual = float(after['format']['duration'])
        if not math.isfinite(actual) or abs(actual - duration) > max(.5, duration * .005):
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise OptimiseError('Output duration validation failed. The original was kept.') from None
    for key, value in _tags(before.get('format', {}).get('tags')).items():
        if _tags(after.get('format', {}).get('tags')).get(key) != value:
            raise OptimiseError('Output metadata validation failed. The original was kept.')
    original_chapters, chapters = before.get('chapters', []), after.get('chapters', [])
    if len(original_chapters) != len(chapters):
        raise OptimiseError('Output chapters changed. The original was kept.')
    for source, output in zip(original_chapters, chapters):
        if (_tags(source.get('tags')) != _tags(output.get('tags')) or
                any(abs(float(source.get(key, 0)) - float(output.get(key, 0))) > .1 for key in ('start_time', 'end_time'))):
            raise OptimiseError('Output chapter validation failed. The original was kept.')


def _hash(descriptor, checkpoint):
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        checkpoint()
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            os.lseek(descriptor, 0, os.SEEK_SET)
            return digest.hexdigest()
        digest.update(chunk)


def _update(store, job_id, **values):
    values['updated'] = time.time()
    with _db(store) as db:
        db.execute('UPDATE jobs SET ' + ','.join(key + '=?' for key in values) + ' WHERE id=?', (*values.values(), job_id))


def _remove_staged(directory, descriptor=None):
    """Unlink only the exact regular staging entry, never a symlink target."""
    try:
        info = os.stat('.encoding.mkv', dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return True
    if not stat.S_ISREG(info.st_mode):
        return False
    if descriptor is not None:
        expected = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino):
            return False
    os.unlink('.encoding.mkv', dir_fd=directory)
    os.fsync(directory)
    return True


def _remove_uncommitted(directory, source_path):
    """An unpublished final name is ours only when it is the staging inode."""
    leaf = Path(_relative(source_path)).stem + '.optimised.mkv'
    try:
        final = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return True
    try:
        temporary = os.stat('.encoding.mkv', dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if (not stat.S_ISREG(final.st_mode) or not stat.S_ISREG(temporary.st_mode) or
            (final.st_dev, final.st_ino) != (temporary.st_dev, temporary.st_ino)):
        return False
    os.unlink(leaf, dir_fd=directory)
    os.fsync(directory)
    return True


def _cleanup_temporary(app, store, job):
    """Recover only recorded job-owned staging files while the global claim is held."""
    if not job['temporary']:
        return True
    if not _owned_temporary(job):
        return False
    descriptors = []
    try:
        root = _output_root(app, store)
        if str(root) != job['temporary_root']:
            return False
        root_fd = _open_absolute(root, allow_missing=True)
        if root_fd is None:
            return True
        descriptors.append(root_fd)
        for part in PurePosixPath(job['temporary']).parts[:-1]:
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptors[-1])
            descriptors.append(descriptor)
        if job['state'] in ACTIVE and not _remove_uncommitted(descriptors[-1], job['path']):
            return False
        cleaned = _remove_staged(descriptors[-1])
        if cleaned:
            try:
                os.rmdir(PurePosixPath(job['temporary']).parts[-2], dir_fd=descriptors[-2])
            except OSError:
                pass  # A validated final copy may share the job directory.
        return cleaned
    except FileNotFoundError:
        return True
    except (OSError, OptimiseError):
        return False
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _recover_temporary(app, store):
    with _db(store) as db:
        jobs = [dict(row) for row in db.execute("SELECT * FROM jobs WHERE temporary<>'' OR state IN ('preparing','encoding','validating')")]
    safe = True
    for job in jobs:
        cleaned = _cleanup_temporary(app, store, job)
        values = {}
        if job['state'] in ACTIVE:
            values.update(state='cancelled' if job['cancel'] else 'failed',
                          error='' if job['cancel'] and cleaned else 'Interrupted before validation finished. The original was kept.')
        if cleaned:
            values.update(temporary='', temporary_root='')
        else:
            safe = False
            values['error'] = 'The temporary copy could not be removed safely. Check the output mapping before retrying. The original was kept.'
        if values:
            _update(store, job['id'], **values)
    return safe


def run_once(app, store, stop):
    """One OS claim covers conversion, validation and recovery after interruption."""
    claim = os.open(store.directory / 'library-optimisation.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(claim, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        # An orphan ffmpeg retains this inherited claim. Only after it exits
        # may recovery remove its recorded temporary file or run another job.
        if not _recover_temporary(app, store):
            return False
        from .library_publication import process as publish
        if publish(app, store, stop, claim):
            return True
        with _db(store) as db:
            db.execute('BEGIN IMMEDIATE')
            if stop.is_set() or not _enabled(store):
                return False
            job = db.execute("SELECT * FROM jobs WHERE state='queued' AND cancel=0 ORDER BY created LIMIT 1").fetchone()
            if not job:
                return False
            job = dict(job)
            started = time.time()
            db.execute("UPDATE jobs SET state='preparing',started=?,phase_started=?," + ','.join(
                column + '=NULL' for column in PROGRESS_COLUMNS if column not in ('started', 'phase_started'))
                + ',updated=? WHERE id=?', (started, started, started, job['id']))
        output_root_fd = kind_fd = output_directory = temporary_fd = None
        publication_committed, cleanup_safe = False, True
        def terminal(state, error=''):
            nonlocal cleanup_safe
            # Roll back an exclusive final link before marking an attempt
            # terminal. A crash during rollback still leaves an ACTIVE row
            # for the same inode-checked recovery path above.
            if output_directory is not None and not publication_committed:
                cleanup_safe = False
                cleanup_safe = _remove_uncommitted(output_directory, job['path'])
            _update(store, job['id'], state=state, error=error)
        def checkpoint():
            if stop.is_set() or store.reset_pending() or not _enabled(store):
                raise Cancelled()
            with _db(store) as db:
                current = db.execute('SELECT cancel FROM jobs WHERE id=?', (job['id'],)).fetchone()
            if not current or current['cancel']:
                raise Cancelled()
            if temporary_fd is not None and os.fstat(temporary_fd).st_size > job['source_bytes'] * 1.1 + 1024 * 1024:
                raise OptimiseError('The copy exceeded the source size. The original was kept.')
        try:
            output_root = _output_root(app, store)
            with _open_source(app, job['kind'], job['path']) as (source, info, root):
                if str(root) != job['root'] or _signature(info) != job['signature']:
                    raise OptimiseError('The source file or library mapping changed. The original was kept.')
                descriptors = (claim, source)
                source_path = '/proc/self/fd/' + str(source)
                before = _probe(source_path, checkpoint, descriptors)
                main, duration = _inspect(before, job['profile'], bool(job['stereo']), bool(job['limit1080']))
                _update(store, job['id'], duration_seconds=duration)
                reporter = _ProgressReporter(store, job['id'], duration)
                source_hash = _hash(source, checkpoint)
                _update(store, job['id'], source_hash=source_hash)
                with _db(store) as db:
                    previous = db.execute("SELECT id FROM jobs WHERE source_hash=? AND profile=? AND stereo=? AND limit1080=? AND state='complete' LIMIT 1",
                                          (source_hash, job['profile'], job['stereo'], job['limit1080'])).fetchone()
                if previous:
                    _update(store, job['id'], state='skipped', error='An identical source already has a validated optimised copy. The original was kept.')
                    return True
                output_root_fd = _open_absolute(output_root, create=True)
                if shutil.disk_usage('/proc/self/fd/' + str(output_root_fd)).free < max(512 * 1024 * 1024, int(job['source_bytes'] * 1.1) + 64 * 1024 * 1024):
                    raise OptimiseError('The output folder needs more free space for a safe temporary copy. The original was kept.')
                try:
                    os.mkdir(job['kind'], 0o700, dir_fd=output_root_fd)
                except FileExistsError:
                    pass
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                kind_fd = os.open(job['kind'], flags, dir_fd=output_root_fd)
                folder_name = job['id'] + '-' + uuid.uuid4().hex[:8]
                os.mkdir(folder_name, 0o700, dir_fd=kind_fd)
                output_directory = os.open(folder_name, flags, dir_fd=kind_fd)
                leaf = Path(job['path']).stem + '.optimised.mkv'
                temporary_relative = job['kind'] + '/' + folder_name + '/.encoding.mkv'
                _update(store, job['id'], temporary_root=str(output_root), temporary=temporary_relative)
                temporary_fd = os.open('.encoding.mkv', os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=output_directory)
                temporary_path = '/proc/self/fd/' + str(temporary_fd)
                descriptors = (claim, source, temporary_fd)
                codec, crf = PROFILES[job['profile']]
                video_index = sum(stream.get('codec_type') == 'video' for stream in before['streams'][:main])
                command = ['ffmpeg', '-v', 'error', '-nostdin', '-y', '-noautorotate', '-protocol_whitelist', 'file,pipe', '-i', source_path,
                           '-map', '0', '-map_metadata', '0', '-map_chapters', '0', '-c', 'copy',
                           f'-c:v:{video_index}', codec, f'-pix_fmt:v:{video_index}', before['streams'][main]['pix_fmt'],
                           f'-crf:v:{video_index}', str(crf), f'-preset:v:{video_index}', 'medium', '-threads', '2']
                if job['profile'] == 'hevc':
                    command += ['-x265-params', 'pools=2:frame-threads=2']
                if job['limit1080']:
                    width, height, sar, _dar = _geometry(before['streams'][main])
                    command += [f'-filter:v:{video_index}', f'scale={width}:{height},setsar=sar={sar.numerator}/{sar.denominator}:max=100000']
                if job['stereo']:
                    command += ['-c:a', 'aac', '-b:a', '160k', '-ac:a', '2']
                    audio_index = 0
                    for stream in before['streams']:
                        if stream.get('codec_type') == 'audio':
                            command += [f'-ar:a:{audio_index}', str(stream['sample_rate'])]
                            audio_index += 1
                for index, stream in enumerate(before['streams']):
                    flags = '+'.join(key for key, value in stream.get('disposition', {}).items() if value) or '0'
                    command += [f'-disposition:{index}', flags]
                command += ['-f', 'matroska', temporary_path]
                reporter.start('encoding')
                _run(command, checkpoint, min(86400, max(600, duration * 30)), descriptors, progress=reporter.report)
                reporter.start('validating')
                after = _probe(temporary_path, checkpoint, (claim, temporary_fd))
                _validate(before, after, main, duration, job['profile'], bool(job['stereo']), bool(job['limit1080']))
                _run(['ffmpeg', '-v', 'error', '-nostdin', '-xerror', '-err_detect', 'explode', '-protocol_whitelist', 'file,pipe', '-i', temporary_path,
                      '-map', '0:v?', '-map', '0:a?', '-f', 'null', '-'], checkpoint, min(86400, max(600, duration * 4)),
                     (claim, temporary_fd), progress=reporter.report)
                if _signature(os.fstat(source)) != job['signature'] or _hash(source, checkpoint) != source_hash:
                    raise OptimiseError('The source changed during conversion. The original was kept.')
                with _open_source(app, job['kind'], job['path']) as (_current, current_info, current_root):
                    if str(current_root) != job['root'] or _signature(current_info) != job['signature']:
                        raise OptimiseError('The source path changed during conversion. The original was kept.')
                size = os.fstat(temporary_fd).st_size
                if not 0 < size <= job['source_bytes'] * .95:
                    _update(store, job['id'], state='skipped', output_bytes=size,
                            error='The validated copy saved less than 5%. The original was kept and the copy discarded.')
                    return True
                checkpoint()
                validated_output_hash = _hash(temporary_fd, checkpoint)
                os.fsync(temporary_fd)
                current_root = _open_absolute(output_root)
                try:
                    current_directory = _open_absolute(output_root / job['kind'] / folder_name)
                    try:
                        if ((os.fstat(current_root).st_dev, os.fstat(current_root).st_ino) != (os.fstat(output_root_fd).st_dev, os.fstat(output_root_fd).st_ino)
                                or (os.fstat(current_directory).st_dev, os.fstat(current_directory).st_ino) != (os.fstat(output_directory).st_dev, os.fstat(output_directory).st_ino)):
                            raise OptimiseError('The output directory changed during conversion. The original was kept.')
                    finally:
                        os.close(current_directory)
                finally:
                    os.close(current_root)
                with _db(store) as db:
                    db.execute('BEGIN IMMEDIATE')
                    current = db.execute('SELECT cancel FROM jobs WHERE id=?', (job['id'],)).fetchone()
                    if not current or current['cancel'] or stop.is_set() or not _enabled(store) or store.reset_pending():
                        raise Cancelled()
                    # Cancellation and publication serialize on the job row.
                    os.link(temporary_path, leaf, dst_dir_fd=output_directory, follow_symlinks=True)
                    os.fsync(output_directory)
                    db.execute("UPDATE jobs SET state='complete',output_bytes=?,output=?,validated_output_hash=?,progress_percent=100,error='',updated=? WHERE id=?",
                               (size, job['kind'] + '/' + folder_name + '/' + leaf, validated_output_hash, time.time(), job['id']))
                publication_committed = True
        except Cancelled:
            terminal('cancelled')
        except OptimiseError as exc:
            terminal('failed', str(exc))
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            terminal('failed', 'Optimisation could not finish safely. The original was kept.')
        finally:
            try:
                if cleanup_safe and output_directory is not None and _remove_staged(output_directory, temporary_fd):
                    _update(store, job['id'], temporary='', temporary_root='')
                    try:
                        os.rmdir(folder_name, dir_fd=kind_fd)
                    except OSError:
                        pass
            finally:
                for descriptor in (temporary_fd, output_directory, kind_fd, output_root_fd):
                    if descriptor is not None:
                        os.close(descriptor)
        return True
    finally:
        os.close(claim)


def loop(app, store, stop):
    while not stop.wait(2):
        with store.background_operation():
            if store.reset_pending():
                continue
            try:
                run_once(app, store, stop)
            except (OSError, sqlite3.Error, ValueError):
                # A database/storage outage must not terminate all later work.
                continue


def register(app, store, access, csrf):
    from flask import jsonify, request

    @app.route('/api/library-optimise', methods=['GET', 'POST'])
    def library_optimise():
        rejected = access(local_action=True)
        if rejected:
            return rejected
        if request.method == 'GET':
            return jsonify(status(app, store))
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), csrf()):
            return jsonify(error='Reload the page.'), 403
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error='Choose an optimisation action.'), 400
        try:
            action = body.get('action')
            if action == 'settings' and set(body) <= {'action', 'enabled'}:
                configure(store, body.get('enabled'))
            elif action == 'scan' and set(body) <= {'action', 'kind', 'folder'}:
                return jsonify(scan(app, store, body.get('kind'), body.get('folder', '')))
            elif action == 'enqueue' and set(body) <= {'action', 'kind', 'path', 'profile', 'stereo', 'limit1080'}:
                enqueue(app, store, body.get('kind'), body.get('path'), body.get('profile'),
                        stereo=body.get('stereo', False), limit1080=body.get('limit1080', False))
            elif action == 'cancel' and set(body) <= {'action', 'id'}:
                cancel(store, body.get('id'))
            elif action == 'retry' and set(body) <= {'action', 'id'}:
                retry(app, store, body.get('id'))
            elif action in ('request_publish', 'cancel_publish') and set(body) <= {'action', 'id'}:
                from . import library_publication as publication
                if action == 'request_publish':
                    publication.request(app, store, body.get('id'))
                else:
                    publication.cancel(store, body.get('id'))
            else:
                raise OptimiseError('Choose a supported optimisation action.')
            return jsonify(status(app, store))
        except (OptimiseError, OSError) as exc:
            return jsonify(error=str(exc) if isinstance(exc, OptimiseError) else 'The library storage is unavailable.'), 400
