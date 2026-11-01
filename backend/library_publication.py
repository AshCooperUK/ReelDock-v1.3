"""Publish validated optimiser copies through separate, explicitly mapped RW views."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

OUTPUTS = {'tv': 'REELDOCK_TV_OUTPUT', 'movies': 'REELDOCK_MOVIE_OUTPUT'}
PENDING = ('waiting', 'queued', 'copying')


def initialise(db):
    db.execute('''CREATE TABLE IF NOT EXISTS publication_requests (
      job_id TEXT PRIMARY KEY, root TEXT NOT NULL, root_device TEXT NOT NULL, root_inode TEXT NOT NULL,
      folder TEXT NOT NULL, name TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'waiting', cancel INTEGER NOT NULL DEFAULT 0,
      created REAL NOT NULL, updated REAL NOT NULL, temporary TEXT NOT NULL DEFAULT '',
      temporary_device TEXT, temporary_inode TEXT, final_name TEXT NOT NULL DEFAULT '',
      bytes INTEGER NOT NULL DEFAULT 0, total_bytes INTEGER, output_hash TEXT NOT NULL DEFAULT '',
      appdata_device TEXT, appdata_inode TEXT, appdata_signature TEXT NOT NULL DEFAULT '',
      cleanup_state TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '')''')


def filename(path, profile):
    """Use the actual file title, never a potentially multi-film parent folder."""
    from . import library_optimise as opt
    stem = Path(opt._relative(path)).stem
    if ' ' not in stem:
        stem = stem.replace('.', ' ').replace('_', ' ')
    # Only a release marker following title text starts a technical suffix.
    marker = re.search(r'(?i)(?<![A-Za-z0-9])(?:\d{3,4}[pi]|[xh][ .]?26[45]|HEVC|AV1|WEB[ .-]?DL|WEBRip|Blu[ .-]?Ray|BDRemux|HDTV|REMUX)(?![A-Za-z0-9])', stem)
    if marker and stem[:marker.start()].strip(' ._-([{'):
        stem = stem[:marker.start()].rstrip(' ._-([{')
    # Keep a film's title/year and a TV episode's marker and episode title.
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', stem)
    stem = re.sub(r'\s+', ' ', stem).strip(' ._-')
    if not re.search(r'(?i)\bS\d{1,4}[ ._-]*E\d{1,4}\b', stem):
        stem = re.sub(r'(?<=\S)\s+((?:19|20)\d{2})$', r' (\1)', stem)
    if not stem or profile not in ('hevc', 'h264'):
        raise opt.OptimiseError('The output filename could not be determined safely.')
    stem = stem.encode('utf-8')[:220].decode('utf-8', errors='ignore').rstrip(' .')
    return stem + (' HEVC.mkv' if profile == 'hevc' else ' H264.mkv')


def _identity(info):
    return str(info.st_dev), str(info.st_ino)


@contextmanager
def output_root(app, kind):
    from . import library_optimise as opt
    if not isinstance(kind, str) or kind not in OUTPUTS:
        raise opt.OptimiseError('Choose TV Shows or Movies.')
    raw = app.config.get(OUTPUTS[kind], os.environ.get(OUTPUTS[kind], ''))
    if not isinstance(raw, str) or not raw:
        raise opt.OptimiseError('A separate writable output view has not been configured for this library.')
    root = opt._absolute(raw)
    descriptor = opt._open_absolute(root)
    try:
        source = opt._open_absolute(opt._source_root(app, kind))
        try:
            if _identity(os.fstat(source)) != _identity(os.fstat(descriptor)):
                raise opt.OptimiseError('The output view must map the same physical library as the read-only input.')
        finally:
            os.close(source)
        if os.fstatvfs(descriptor).f_flag & os.ST_RDONLY or not os.access('/proc/self/fd/' + str(descriptor), os.W_OK):
            raise opt.OptimiseError('The separate library output view is not writable.')
        yield root, descriptor
    finally:
        os.close(descriptor)


def output_status(app, kind):
    from . import library_optimise as opt
    try:
        with output_root(app, kind) as (root, _descriptor):
            return dict(output_available=True, output_path=str(root), output_message='Validated copies are saved beside their originals.')
    except (OSError, opt.OptimiseError) as exc:
        return dict(output_available=False, output_path='', output_message=str(exc) if isinstance(exc, opt.OptimiseError) else 'The library output view is unavailable.')


def plan(app, kind, path, profile):
    with output_root(app, kind) as (root, descriptor):
        info = os.fstat(descriptor)
        return dict(root=str(root), root_device=str(info.st_dev), root_inode=str(info.st_ino),
                    folder='' if PurePosixPath(path).parent == PurePosixPath('.') else str(PurePosixPath(path).parent),
                    name=filename(path, profile))


def _insert(db, job_id, planned, state):
    now = time.time()
    db.execute('''INSERT INTO publication_requests(job_id,root,root_device,root_inode,folder,name,state,created,updated)
                  VALUES(?,?,?,?,?,?,?,?,?)''', (job_id, planned['root'], planned['root_device'], planned['root_inode'],
                  planned['folder'], planned['name'], state, now, now))


def request(app, store, job_id):
    from . import library_optimise as opt
    if not isinstance(job_id, str) or not opt._enabled(store):
        raise opt.OptimiseError('Enable library optimisation before requesting a saved copy.')
    with opt._db(store) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row or row['cancel'] or row['state'] not in ('queued', *opt.ACTIVE, 'complete'):
            raise opt.OptimiseError('Only an uncancelled current or validated job can be saved beside its original.')
        planned = plan(app, row['kind'], row['path'], row['profile'])
        old = db.execute('SELECT * FROM publication_requests WHERE job_id=?', (job_id,)).fetchone()
        state = 'queued' if row['state'] == 'complete' else 'waiting'
        if not old:
            _insert(db, job_id, planned, state)
        elif old['state'] in ('failed', 'cancelled'):
            if any(old[key] != planned[key] for key in ('root', 'root_device', 'root_inode', 'folder', 'name')):
                raise opt.OptimiseError('The saved publication plan changed. Restore its output mapping before retrying.')
            db.execute("UPDATE publication_requests SET state=?,cancel=0,error='',updated=? WHERE job_id=?", (state, time.time(), job_id))


def cancel(store, job_id):
    from . import library_optimise as opt
    if not isinstance(job_id, str):
        raise opt.OptimiseError('Choose a publication request.')
    with opt._db(store) as db:
        row = db.execute('SELECT state FROM publication_requests WHERE job_id=?', (job_id,)).fetchone()
        if not row:
            raise opt.OptimiseError('This job has no publication request.')
        if row['state'] in PENDING:
            db.execute('UPDATE publication_requests SET cancel=1,updated=? WHERE job_id=?', (time.time(), job_id))


def dto(app, store, job, row, *, available=None):
    if available is None:
        available = output_status(app, job['kind'])['output_available']
    eligible = (available and opt_enabled(store) and not job['cancel'] and job['state'] in ('queued', 'preparing', 'encoding', 'validating', 'complete'))
    result = dict(destination_mode=job.get('destination_mode') or 'appdata', destination_name=job.get('destination_name') or '',
                  publication_state=None, publication_path='', publication_error='', publication_bytes=None,
                  publication_total_bytes=None, publication_percent=None, can_publish=bool(eligible))
    if row:
        result.update(publication_state=row['state'], destination_name=row['name'], publication_error=row['error'],
                      publication_bytes=row['bytes'], publication_total_bytes=row['total_bytes'],
                      can_publish=bool(eligible and row['state'] in ('failed', 'cancelled')))
        if row['state'] == 'complete':
            result['publication_path'] = str(PurePosixPath(row['folder']) / row['final_name'])
            result['publication_percent'] = 100.0
        elif row['state'] == 'copying' and row['total_bytes']:
            result['publication_percent'] = min(99.9, row['bytes'] / row['total_bytes'] * 100)
    return result


def opt_enabled(store):
    return store.get('library_optimise_enabled') == 'true'


@contextmanager
def _parent(app, job, publication):
    from . import library_optimise as opt
    descriptors = []
    expected_folder = '' if PurePosixPath(job['path']).parent == PurePosixPath('.') else str(PurePosixPath(job['path']).parent)
    if publication['folder'] != expected_folder:
        raise opt.OptimiseError('The publication folder is not the original file parent.')
    with output_root(app, job['kind']) as (root, output):
        if str(root) != publication['root'] or _identity(os.fstat(output)) != (publication['root_device'], publication['root_inode']):
            raise opt.OptimiseError('The publication output mapping changed. The validated copy was retained.')
        descriptors.append(os.dup(output))
        try:
            for part in PurePosixPath(opt._relative(publication['folder'], empty=True)).parts:
                descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptors[-1]))
            # The exact original, through both views, must still be the same inode.
            original = os.open(PurePosixPath(job['path']).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptors[-1])
            try:
                if opt._signature(os.fstat(original)) != job['signature']:
                    raise opt.OptimiseError('The original or its output mapping changed. The validated copy was retained.')
            finally:
                os.close(original)
            yield descriptors[-1]
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


@contextmanager
def _appdata(app, store, job):
    from . import library_optimise as opt
    relative = opt._relative(job['output'])
    pattern = re.escape(job['kind'] + '/' + job['id']) + r'-[0-9a-f]{8}/' + re.escape(Path(job['path']).stem + '.optimised.mkv')
    if not re.fullmatch(pattern, relative):
        raise opt.OptimiseError('The validated AppData output receipt is unavailable.')
    descriptors = [opt._open_absolute(opt._output_root(app, store))]
    try:
        for part in PurePosixPath(relative).parts[:-1]:
            descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptors[-1]))
        descriptor = os.open(PurePosixPath(relative).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptors[-1])
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size != job['output_bytes'] or not 0 < info.st_size <= job['source_bytes'] * .95:
                raise opt.OptimiseError('The validated output size changed. The original was retained.')
            yield descriptor, descriptors[-1], PurePosixPath(relative).name, info
        finally:
            os.close(descriptor)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _update(store, job_id, **values):
    from . import library_optimise as opt
    values['updated'] = time.time()
    with opt._db(store) as db:
        db.execute('UPDATE publication_requests SET ' + ','.join(key + '=?' for key in values) + ' WHERE job_id=?', (*values.values(), job_id))


def _owned_stat(parent, name, publication):
    if not name:
        return None
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or _identity(info) != (publication['temporary_device'], publication['temporary_inode'])):
        from .library_optimise import OptimiseError
        raise OptimiseError('A publication file no longer matches its owned inode. No existing file was removed.')
    return info


def _cleanup(parent, publication, *, committed=False):
    """A crash can leave a final hardlink only to our persisted staging inode."""
    from . import library_optimise as opt
    temporary = publication['temporary']
    if not temporary:
        return
    if not re.fullmatch(r'\.reeldock-optimise-' + re.escape(publication['job_id']) + r'-[0-9a-f]{8}\.part', temporary):
        raise opt.OptimiseError('The publication temporary name is not owned by this job.')
    if (PurePosixPath(publication['name']).name != publication['name'] or publication['name'].startswith('.')
            or publication['final_name'] and publication['final_name'] not in {_candidate(publication['name'], number) for number in range(1, 101)}):
        raise opt.OptimiseError('The publication final name is not owned by this job.')
    if not committed and publication['final_name']:
        try:
            final = os.stat(publication['final_name'], dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            # A no-clobber collision belongs to somebody else. Only an exact
            # inode match can be our uncommitted link after a crash.
            if stat.S_ISREG(final.st_mode) and _identity(final) == (publication['temporary_device'], publication['temporary_inode']):
                os.unlink(publication['final_name'], dir_fd=parent)
    try:
        _owned_stat(parent, temporary, publication)
    except FileNotFoundError:
        pass
    else:
        os.unlink(temporary, dir_fd=parent)
    os.fsync(parent)


def _candidate(name, counter):
    return name if counter == 1 else Path(name).stem + f' ({counter}).mkv'


def process(app, store, stop, claim):
    """Called only with the optimiser's inherited global claim already held."""
    from . import library_optimise as opt
    with opt._db(store) as db:
        rows = [dict(row) for row in db.execute("SELECT p.* FROM publication_requests p JOIN jobs j ON j.id=p.job_id WHERE p.state IN ('waiting','queued','copying') OR (p.state='complete' AND p.cleanup_state='pending') ORDER BY p.created LIMIT 20")]
    if stop.is_set() or not opt._enabled(store):
        return False
    for publication in rows:
        with opt._db(store) as db:
            found = db.execute('SELECT * FROM jobs WHERE id=?', (publication['job_id'],)).fetchone()
        if not found:
            continue
        job = dict(found)
        if publication['cancel'] or job['cancel'] or job['state'] in ('failed', 'cancelled', 'skipped'):
            try:
                with _parent(app, job, publication) as parent:
                    _cleanup(parent, publication, committed=publication['state'] == 'complete')
                _update(store, job['id'], state='cancelled', temporary='')
            except (OSError, ValueError, TypeError, KeyError):
                _update(store, job['id'], state='cancelled', error='Cancelled; a temporary copy was retained because its ownership could not be verified safely.')
            return True
        if job['state'] != 'complete':
            continue
        return _publish(app, store, stop, claim, job, publication)
    return False


def _publish(app, store, stop, claim, job, publication):
    from . import library_optimise as opt
    parent = None
    committed = publication['state'] == 'complete'
    last_check = last_write = 0.0
    def checkpoint():
        nonlocal last_check
        if stop.is_set() or store.reset_pending() or not opt._enabled(store):
            raise opt.Cancelled()
        now = time.monotonic()
        if now - last_check < .2:
            return
        with opt._db(store) as db:
            current = db.execute('SELECT p.cancel,j.cancel AS job_cancel FROM publication_requests p JOIN jobs j ON j.id=p.job_id WHERE p.job_id=?', (job['id'],)).fetchone()
        if not current or current['cancel'] or current['job_cancel']:
            raise opt.Cancelled()
        last_check = now
    try:
        with opt._open_source(app, job['kind'], job['path']) as (source, original, root), _parent(app, job, publication) as parent:
            if str(root) != job['root'] or opt._signature(original) != job['signature']:
                raise opt.OptimiseError('The original changed. The validated copy was retained.')
            if committed:
                _cleanup(parent, publication, committed=True)
                return _finish_move(app, store, job, publication)
            _cleanup(parent, publication)
            _update(store, job['id'], state='copying', bytes=0, temporary='', final_name='', error='')
            with _appdata(app, store, job) as (encoded, _directory, _leaf, encoded_info):
                checkpoint()
                # Legacy completed jobs lack a validated-output hash receipt.
                # Recheck their tracks/full decode against the exact original.
                known_hash = job.get('validated_output_hash') or ''
                if not known_hash:
                    before = opt._probe('/proc/self/fd/' + str(source), checkpoint, (claim, source))
                    main, duration = opt._inspect(before, job['profile'], bool(job['stereo']), bool(job['limit1080']))
                    after = opt._probe('/proc/self/fd/' + str(encoded), checkpoint, (claim, encoded))
                    opt._validate(before, after, main, duration, job['profile'], bool(job['stereo']), bool(job['limit1080']))
                    opt._run(['ffmpeg', '-v', 'error', '-nostdin', '-xerror', '-err_detect', 'explode', '-protocol_whitelist', 'file,pipe', '-i', '/proc/self/fd/' + str(encoded), '-map', '0:v?', '-map', '0:a?', '-f', 'null', '-'], checkpoint, min(86400, max(600, duration * 4)), (claim, encoded))
                if not job['source_hash'] or opt._hash(source, checkpoint) != job['source_hash']:
                    raise opt.OptimiseError('The original content changed. The validated copy was retained.')
                if shutil.disk_usage('/proc/self/fd/' + str(parent)).free < encoded_info.st_size + 64 * 1024 * 1024:
                    raise opt.OptimiseError('The original folder needs more free space for the separate copy.')
                temporary = '.reeldock-optimise-' + job['id'] + '-' + uuid.uuid4().hex[:8] + '.part'
                _update(store, job['id'], temporary=temporary, temporary_device=None, temporary_inode=None,
                        total_bytes=encoded_info.st_size, appdata_device=str(encoded_info.st_dev),
                        appdata_inode=str(encoded_info.st_ino), appdata_signature=opt._signature(encoded_info))
                publication.update(temporary=temporary, temporary_device=None, temporary_inode=None, final_name='')
                target = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
                try:
                    target_info = os.fstat(target)
                    publication.update(temporary_device=str(target_info.st_dev), temporary_inode=str(target_info.st_ino))
                    _update(store, job['id'], temporary_device=str(target_info.st_dev), temporary_inode=str(target_info.st_ino))
                    copied, digest = 0, hashlib.sha256()
                    os.lseek(encoded, 0, os.SEEK_SET)
                    while True:
                        checkpoint()
                        chunk = os.read(encoded, 1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(target, view)
                            if written <= 0:
                                raise opt.OptimiseError('The copy could not be written safely.')
                            view = view[written:]
                        copied += len(chunk)
                        now = time.monotonic()
                        if now - last_write >= opt.PROGRESS_INTERVAL:
                            _update(store, job['id'], bytes=copied)
                            last_write = now
                    os.fsync(target)
                    output_hash = digest.hexdigest()
                    if (copied != encoded_info.st_size or known_hash and output_hash != known_hash
                            or opt._signature(os.fstat(encoded)) != opt._signature(encoded_info)
                            or opt._hash(target, checkpoint) != output_hash):
                        raise opt.OptimiseError('The copied output did not match its validated bytes.')
                    if opt._signature(os.fstat(source)) != job['signature'] or opt._hash(source, checkpoint) != job['source_hash']:
                        raise opt.OptimiseError('The original changed during publication. Its copy was retained in AppData.')
                    # Reopen paths before final publication so a replaced parent
                    # cannot redirect a saved copy out of its original folder.
                    with _parent(app, job, publication) as current_parent:
                        if _identity(os.fstat(current_parent)) != _identity(os.fstat(parent)):
                            raise opt.OptimiseError('The original folder changed during publication.')
                    source_mode = stat.S_IMODE(original.st_mode)
                    if source_mode & 0o040 and os.fstat(target).st_gid != original.st_gid:
                        if original.st_gid in {*os.getgroups(), os.getgid()}:
                            os.fchown(target, -1, original.st_gid)
                        elif not source_mode & 0o004:
                            raise opt.OptimiseError('The original read group is unavailable for the saved copy. AppData was retained.')
                    os.fchmod(target, 0o600 | (source_mode & 0o044))
                    os.fsync(target)
                    checkpoint()
                    for counter in range(1, 101):
                        final = _candidate(publication['name'], counter)
                        # Persist the intended final name BEFORE its hardlink.
                        # A crash before commit then leaves an inode-checkable
                        # receipt instead of an untracked visible copy.
                        _update(store, job['id'], final_name=final)
                        publication['final_name'] = final
                        try:
                            with opt._db(store) as db:
                                db.execute('BEGIN IMMEDIATE')
                                current = db.execute('SELECT p.cancel,j.cancel AS job_cancel FROM publication_requests p JOIN jobs j ON j.id=p.job_id WHERE p.job_id=?', (job['id'],)).fetchone()
                                if not current or current['cancel'] or current['job_cancel'] or stop.is_set() or not opt._enabled(store) or store.reset_pending():
                                    raise opt.Cancelled()
                                _owned_stat(parent, temporary, publication)
                                os.link('/proc/self/fd/' + str(target), final, dst_dir_fd=parent, follow_symlinks=True)
                                _owned_stat(parent, final, publication)
                                os.fsync(parent)
                                db.execute("UPDATE publication_requests SET state='complete',bytes=?,output_hash=?,final_name=?,cleanup_state='pending',updated=? WHERE job_id=?", (copied, output_hash, final, time.time(), job['id']))
                        except FileExistsError:
                            _update(store, job['id'], final_name='')
                            publication['final_name'] = ''
                            continue
                        break
                    else:
                        raise opt.OptimiseError('All safe output names already exist. No existing file was overwritten.')
                    committed = True
                    publication.update(state='complete', bytes=copied, output_hash=output_hash,
                                       appdata_device=str(encoded_info.st_dev), appdata_inode=str(encoded_info.st_ino),
                                       appdata_signature=opt._signature(encoded_info))
                finally:
                    os.close(target)
            _cleanup(parent, publication, committed=True)
            _finish_move(app, store, job, publication)
    except opt.Cancelled:
        _update(store, job['id'], state='cancelled', error='')
    except (OSError, ValueError, TypeError, KeyError) as exc:
        _update(store, job['id'], state='complete' if committed else 'failed',
                error=str(exc) if isinstance(exc, opt.OptimiseError) else 'Publication could not finish safely. The original and validated AppData copy were retained.',
                cleanup_state='error' if committed else '')
    finally:
        if not committed:
            try:
                with _parent(app, job, publication) as current_parent:
                    _cleanup(current_parent, publication)
                _update(store, job['id'], temporary='')
            except (OSError, ValueError, TypeError, KeyError):
                pass  # Retain an uncertain receipt/file; never guess ownership.
    return True


def _finish_move(app, store, job, publication):
    """Only after a durable final receipt may the generated AppData inode go."""
    from . import library_optimise as opt
    with _parent(app, job, publication) as parent:
        _owned_stat(parent, publication['final_name'], publication)
        final = os.open(publication['final_name'], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        try:
            final_info = os.fstat(final)
            if (_identity(final_info) != (publication['temporary_device'], publication['temporary_inode'])
                    or opt._hash(final, lambda: None) != publication['output_hash']):
                raise opt.OptimiseError('The saved adjacent copy changed. Its AppData source was retained.')
            final_signature = opt._signature(final_info)
            if opt._signature(os.fstat(final)) != final_signature:
                raise opt.OptimiseError('The saved adjacent copy changed during verification. AppData was retained.')
            try:
                with _appdata(app, store, job) as (descriptor, directory, leaf, info):
                    if (_identity(info) != (publication['appdata_device'], publication['appdata_inode'])
                            or opt._signature(info) != publication['appdata_signature']
                            or opt._hash(descriptor, lambda: None) != publication['output_hash']):
                        raise opt.OptimiseError('Saved beside the original; the changed AppData copy was retained for review.')
                    current = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
                    if opt._signature(current) != opt._signature(info):
                        raise opt.OptimiseError('Saved beside the original; the AppData path changed and was retained.')
                    with _parent(app, job, publication) as current_parent:
                        current_final = _owned_stat(current_parent, publication['final_name'], publication)
                        if (_identity(os.fstat(current_parent)) != _identity(os.fstat(parent))
                                or opt._signature(current_final) != final_signature
                                or opt._signature(os.fstat(final)) != final_signature):
                            raise opt.OptimiseError('The saved adjacent copy or parent changed. AppData was retained.')
                    os.unlink(leaf, dir_fd=directory)
                    os.fsync(directory)
            except FileNotFoundError:
                # Only an already-missing AppData path can finish cleanup. A
                # missing/replaced adjacent destination must retain AppData.
                with _parent(app, job, publication) as current_parent:
                    _owned_stat(current_parent, publication['final_name'], publication)
                with opt._db(store) as db:
                    if db.execute('SELECT 1 FROM jobs WHERE id=?', (job['id'],)).fetchone() is None:
                        raise opt.OptimiseError('The publication job changed during cleanup.')
        finally:
            os.close(final)
    _update(store, job['id'], cleanup_state='done', temporary='', error='')
    return True
