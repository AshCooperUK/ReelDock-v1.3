"""Explicit factory reset, restricted to this installation's managed storage."""
import json
import os
from pathlib import Path
import secrets
import stat


class ResetRequested(BaseException):
    """Unwind a worker cycle without treating cancellation as a provider failure."""


class ResetBusy(ValueError):
    pass


def checked_directory(value, data=False):
    path = Path(value).absolute()
    protected = (Path('/'), Path('/home'), Path('/tmp'), Path('/var'), Path('/data').parent,
                 Path.home(), Path(__file__).resolve().parent.parent)
    if any(p == path or p.is_relative_to(path) for p in protected):
        raise ValueError('Reset cannot clear a system or source directory. Check the ReelDock mounts.')
    if any('ytsd' in part.lower() for part in path.parts):
        raise ValueError('Reset cannot access YTSD storage.')
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Reset cannot use a storage directory reached through a symbolic link.')
    if path.exists() and not path.is_dir():
        raise ValueError('A ReelDock storage directory is not a folder.')
    if data and (path / 'ytsd-dev.sqlite3').exists():
        raise ValueError('Reset cannot access YTSD storage.')
    return path


def targets(app, store):
    data = checked_directory(store.directory, data=True)
    from .storage_layout import fixed, fixed_folders
    if fixed(store):
        media = [checked_directory(value) for value in fixed_folders(store).values()]
        for folder in media:
            if data.is_relative_to(folder) or folder.is_relative_to(data):
                raise ValueError('Keep application data and media separate.')
            if not folder.is_dir(): raise ValueError('A Compose media mount is unavailable.')
            for directory, dirs, _ in os.walk(folder, followlinks=False):
                if not os.access(directory, os.W_OK | os.X_OK): raise ValueError('A media folder is not writable.')
                if any(not (Path(directory)/name).is_symlink() and os.path.ismount(Path(directory)/name) for name in dirs):
                    raise ValueError('Reset cannot clear a nested mount.')
        return data, media
    media = [checked_directory(app.config[key]) for key in ('MEDIA_DIR', 'M3U_DIR')]
    base = store.get('storage_base')
    if base:
        candidate = checked_directory(base)
        roots = [checked_directory(p) for p in json.loads(app.config.get('REELDOCK_STORAGE_ROOTS') or '[]')]
        if not any(candidate.is_relative_to(root) for root in roots):
            raise ValueError('The saved ReelDock base is outside its configured storage mounts.')
        media.append(candidate)
    # Legacy installations may keep complete downloads outside the media base.
    folders = json.loads(store.get('m3u_download_folders', '{}'))
    roots = [checked_directory(p) for p in json.loads(app.config.get('M3U_DOWNLOAD_ROOTS') or '[]')]
    for value in folders.values():
        if not value:
            continue
        folder = checked_directory(value)
        if not any(folder.is_relative_to(root) for root in roots):
            raise ValueError('A download folder is outside its configured ReelDock mount.')
        media.append(folder)
    for folder in media:
        if data.is_relative_to(folder) or folder.is_relative_to(data):
            raise ValueError('Keep ReelDock app data and media directories separate before resetting.')
    # Keep mount roots themselves. Deduplicate ordinary nested paths.
    media = [p for p in set(media) if not any(p != q and p.is_relative_to(q) for q in media)]
    for root in [data, *media]:
        if not root.exists():
            raise ValueError('A ReelDock storage mount is unavailable. Restore it before resetting.')
        for directory, dirs, files in os.walk(root, followlinks=False):
            if not os.access(directory, os.W_OK | os.X_OK):
                raise ValueError('ReelDock cannot clear a storage folder. Check its permissions.')
            for name in dirs:
                child = Path(directory) / name
                if not child.is_symlink() and os.path.ismount(child):
                    raise ValueError('Move nested mounts out of ReelDock storage before resetting.')
    return data, media


def mount_inodes(app):
    # Bind aliases (/media and /reeldock/YouTube, for example) can refer to
    # the same directory without ismount() recognising the second spelling.
    paths = []
    for config in (app.config, app.extensions.get('storage_defaults', {})):
        paths.extend(config[key] for key in ('MEDIA_DIR', 'M3U_DIR') if config.get(key))
        paths.extend(json.loads(config.get('M3U_DOWNLOAD_ROOTS') or '[]'))
    paths.extend(json.loads(app.config.get('REELDOCK_STORAGE_ROOTS') or '[]'))
    result = set()
    for value in paths:
        try:
            info = Path(value).stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):result.add((info.st_dev, info.st_ino))
        except FileNotFoundError:
            pass
    return result


def clear_directory(root, keep=(), preserve_inodes=frozenset()):
    # Descriptor-relative traversal never follows a substituted symlink.
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    retained = set()
    try:
        for directory, dirs, files, fd in os.fwalk('.', topdown=False, follow_symlinks=False, dir_fd=root_fd):
            for name in files:
                if directory == '.' and name in keep:continue
                os.unlink(name, dir_fd=fd)
            for name in dirs:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                child = os.path.join(directory, name)
                if stat.S_ISLNK(info.st_mode):
                    os.unlink(name, dir_fd=fd)
                elif (info.st_dev, info.st_ino) in preserve_inodes or child in retained:
                    retained.add(directory)
                else:
                    os.rmdir(name, dir_fd=fd)
    finally:
        os.close(root_fd)


def reset(app, store):
    # The caller holds the exclusive operation lock: no web request or worker
    # cycle can repopulate the database or write media during deletion.
    data, media = targets(app, store)
    preserved = mount_inodes(app)
    if store.get('reset_in_progress') != '1':
        store.set('reset_in_progress', '1')
    try:
        for folder in media:
            clear_directory(folder, preserve_inodes=preserved)
        clear_directory(data, {'reeldock.sqlite3', 'reeldock.sqlite3-wal', 'reeldock.sqlite3-shm',
                               'operation.lock', 'worker.lock', 'activity-actions.lock', 'session-secret.txt', 'instance-id.txt'})
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('PRAGMA secure_delete=ON')
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            for table in tables:
                db.execute('DELETE FROM "' + table.replace('"', '""') + '"')
            # A fresh revision prevents pre-reset cookies becoming valid when
            # the replacement administrator is created, across all web processes.
            db.execute('INSERT INTO settings VALUES (?,?)', ('auth_revision', secrets.token_urlsafe(32)))
        with store.connect() as db:
            db.execute('VACUUM')
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    except OSError:
        # Keep the account and pause workers so a partial deletion can be retried.
        raise ValueError('Reset could not clear every file. Background work is paused. Check storage permissions and retry Reset ReelDock.') from None
