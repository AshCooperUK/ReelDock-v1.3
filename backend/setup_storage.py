"""First-run base media location, independent of application data."""
import secrets
import shutil
from pathlib import Path
from . import storage_layout
from .storage_locations import roots as configured_roots

def probe(path):
    folder=Path(path)
    try:
        if not path or not folder.is_dir():raise OSError('Folder unavailable')
        marker=folder/('.reeldock-check-'+secrets.token_hex(8))
        try:
            with marker.open('x') as stream:stream.write('ok')
        finally:marker.unlink(missing_ok=True)
        return dict(path=str(folder),writable=True,free_gb=round(shutil.disk_usage(folder).free/1024**3,1))
    except OSError:return dict(path=str(folder),writable=False,free_gb=None)

def state(app,store):
    if storage_layout.fixed(store):
        folders = storage_layout.fixed_folders(store)
        checks = [probe(p) for p in folders.values()]
        return dict(locked=True, ready=all(p['writable'] for p in checks), configured=True,
                    base=store.media_root, host_path=app.config.get('REELDOCK_HOST_ROOT') or store.media_root,
                    folders=folders, names=storage_layout.names(store), roots=[store.media_root],
                    auto_download_folder=folders['auto'], auto_download_host=folders['auto'],
                    can_choose_auto_folder=False, **{k:v for k,v in probe(store.media_root).items() if k!='path'})
    roots=configured_roots(app)
    base=store.get('storage_base') or storage_layout.download_base(store) or (app.config.get('MEDIA_DIR', '') if store.get('setup_complete') else (roots[0] if roots else ''))
    from .storage_locations import host_path, locations
    folder=store.get('auto_download_folder') or ''
    return dict(can_choose_auto_folder=bool(locations(app)),storage_path=host_path(app,base) if base else '',host_path=host_path(app,base) if base else '',locations=locations(app),ready=bool(store.get('storage_base')) or bool(store.get('setup_complete')),base=base,configured=bool(store.get('storage_base')),roots=roots,names=storage_layout.names(store),auto_download_folder=folder,auto_download_host=host_path(app,folder) if folder else '',**{k:v for k,v in probe(base).items() if k!='path'})

def save(app,store,body):
    if storage_layout.fixed(store):
        if body: raise ValueError(storage_layout.LOCKED)
        if not state(app,store)['ready']: raise ValueError('Check the media mount permissions in Docker Compose.')
        return
    if not isinstance(body,dict):raise ValueError('Choose a base directory.')
    base=body.get('base')
    inferred=base is None or base==''
    try:
        if inferred:
            # The Compose-managed wizard sends {} even on old separate mounts.
            # Reuse a saved choice first; never try another mount if it is unsafe.
            roots=configured_roots(app)
            base=storage_layout.download_base(store) or (roots[0] if roots else '')
        storage_layout.select(app,store,base)
    except (ValueError,OSError) as exc:
        if not inferred:raise
        raise ValueError(f'{exc} Check REELDOCK_DOWNLOAD_BASE and REELDOCK_STORAGE_ROOTS in Docker Compose; for a fresh install, set REELDOCK_MEDIA_ROOT to a dedicated writable media mount, then restart ReelDock.') from None
    store.set('setup_storage_ready','1')
