"""Copy a managed library before atomically switching its saved paths."""
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from . import storage_layout
from .storage_locations import validate_choice, host_path


def move(app, store, value):
    # Caller holds the operation lock exclusively. Workers reread saved paths
    # between cycles; no media writer may run while this snapshot is copied.
    old = store.get('storage_base')
    if not old:
        raise ValueError('Choose a base directory in setup before moving the library.')
    source = Path(old)
    destination = validate_choice(app, store, value)
    if destination == source:
        return
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError('Choose a separate directory outside the current ReelDock folder.')
    if destination.exists() and destination.samefile(source):
        return
    if any(p.exists() and p.samefile(source) for p in destination.parents) or (destination.exists() and any(p.samefile(destination) for p in source.parents)):
        raise ValueError('Choose a separate directory outside the current ReelDock folder.')
    if not source.is_dir():
        raise ValueError('The current library folder is unavailable. Restore its mount before moving it.')
    size = 0
    for directory, dirs, files in os.walk(source, followlinks=False):
        for name in dirs + files:
            item = Path(directory) / name
            info = item.lstat()
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or os.path.ismount(item):
                raise ValueError('The library contains a symbolic link, special file or nested mount. Keep it in its current location until that is resolved.')
            if stat.S_ISREG(info.st_mode): size += info.st_size
    destination.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(destination.parent).free < size + 64 * 1024**2:
        raise ValueError('The destination needs enough free space for a full copy of the library.')
    stage = Path(tempfile.mkdtemp(prefix='.reeldock-move-', dir=destination.parent))
    installed = []
    committed = False
    try:
        shutil.copytree(source, stage, dirs_exist_ok=True, symlinks=True)
        # Reject links introduced while copying; never switch to a partial tree.
        for directory, dirs, files in os.walk(stage, followlinks=False):
            for name in dirs + files:
                if (Path(directory)/name).is_symlink():
                    raise ValueError('The library changed during copying. Its original location has been kept.')
        destination = validate_choice(app, store, str(destination))
        destination.mkdir(exist_ok=True)
        if any(destination.iterdir()):
            raise ValueError('The destination is no longer empty. Existing files have been kept.')
        for item in stage.iterdir():
            target = destination/item.name
            item.rename(target)
            installed.append(target)
        aliases={old:str(destination)}
        defaults=app.extensions.get('storage_defaults',{})
        roots=[defaults.get('MEDIA_DIR'),defaults.get('M3U_DIR'),*json.loads(defaults.get('M3U_DOWNLOAD_ROOTS') or '[]')]
        candidates=[source,*[p for p in source.iterdir() if p.is_dir()]]
        if (source/'Downloads').is_dir():candidates.extend(p for p in (source/'Downloads').iterdir() if p.is_dir())
        for root in roots:
            if root and Path(root).exists():
                for candidate in candidates:
                    if Path(root).samefile(candidate):aliases[root]=str(destination/candidate.relative_to(source))
        def rewrite(value, before=old, after=str(destination)):
            if before==old and after==str(destination):
                for prefix,target in sorted(aliases.items(),key=lambda item:len(item[0]),reverse=True):
                    if value and Path(value).is_absolute() and Path(value).is_relative_to(prefix):
                        return str(Path(target)/Path(value).relative_to(prefix))
            if value and Path(value).is_absolute() and Path(value).is_relative_to(before):
                return str(Path(after)/Path(value).relative_to(before))
            return value
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for table, column in [('m3u_downloads','destination'),('m3u_sidecars','path'),('m3u_exported','target')]:
                for row in db.execute(f'SELECT rowid,{column} FROM {table}').fetchall():
                    updated = rewrite(row[1])
                    if updated != row[1]:db.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?',(updated,row[0]))
            folders=json.loads(store.get('m3u_download_folders','{}'))
            values={'storage_base':str(destination), 'storage_host_base':host_path(app,destination), 'm3u_download_folders':json.dumps({k:rewrite(v) for k,v in folders.items()})}
            # Keep media-server mappings accurate; original files remain available
            # until the administrator updates the external library's own paths.
            if store.get('emby_root'):
                values['emby_root']=rewrite(store.get('emby_root'),host_path(app,source),host_path(app,destination))
            if store.get('m3u_emby_folders'):
                values['m3u_emby_folders']=json.dumps({k:rewrite(v,host_path(app,source),host_path(app,destination)) for k,v in json.loads(store.get('m3u_emby_folders')).items()})
            values['emby_library_status']='ReelDock directory changed. Check the folder paths in your media server.'
            db.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)',values.items())
        committed = True
        storage_layout.apply(app, store)
    finally:
        if not committed:
            for item in installed:
                if item.is_dir():shutil.rmtree(item)
                else:item.unlink(missing_ok=True)
        shutil.rmtree(stage, ignore_errors=True)
