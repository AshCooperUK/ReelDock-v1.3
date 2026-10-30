"""A single media base directory, constrained to administrator-configured mounts."""
import json
from pathlib import Path
from . import m3u

DEFAULTS = dict(youtube='YouTube', m3u='M3U', movies='Movies', series='TV Shows', unsorted='Unsorted')

# Internal names are fixed. Compose supplies the host mount and container root.
FIXED_NAMES = dict(auto='Auto-Download', series='TV Shows', movies='Movies',
                   vod_series='VOD TV Shows', vod_movies='VOD Movies',
                   youtube='YouTube Subscriptions', youtube_downloads='YouTube Downloads')
LOCKED = 'Media directories are managed by Docker Compose.'

def fixed(store):
    return bool(getattr(store, 'media_root', ''))

def configure(app, store):
    value = app.config.get('REELDOCK_MEDIA_ROOT', '')
    store.media_root = value
    store.emby_media_root = app.config.get('REELDOCK_EMBY_ROOT', '').rstrip('/')
    if store.emby_media_root and (not Path(store.emby_media_root).is_absolute() or '..' in Path(store.emby_media_root).parts or store.emby_media_root=='/'):
        raise ValueError('Configure an absolute dedicated Emby media root.')
    if not value:
        return
    root = Path(value)
    data = Path(store.directory).resolve()
    if not root.is_absolute() or '..' in root.parts or root == Path('/'):
        raise ValueError('Configure an absolute dedicated ReelDock media mount.')
    from .reset import checked_directory
    root = checked_directory(root)
    root = m3u.contained(root, '')
    if root.resolve().is_relative_to(data) or data.is_relative_to(root.resolve()):
        raise ValueError('Keep application data and media mounts separate.')
    if not root.is_dir():
        raise ValueError('The Compose media mount is unavailable.')
    for name in FIXED_NAMES.values():
        m3u.contained(root, name).mkdir(exist_ok=True)

def fixed_folders(store):
    return {key: str(m3u.contained(store.media_root, name)) for key, name in FIXED_NAMES.items()}

def source_base(store, root, folder):
    return m3u.contained(root, '' if fixed(store) else folder)

def source_streams(store, root, folder):
    """Only scan this source's exports, never the whole shared media mount."""
    from .m3u_existing import scan
    result = {}
    for name in ('VOD Movies', 'VOD TV Shows'):
        relative = Path(name) / folder
        base = m3u.contained(root, relative)
        for key, paths in scan(base).items():
            result.setdefault(key, []).extend(str(relative / path) for path in paths)
    return result

def names(store):
    if fixed(store): return {**DEFAULTS, **FIXED_NAMES}
    return {**DEFAULTS, **json.loads(store.get('storage_names', '{}'))}

def download_base(store):
    """Use an explicit download mount on upgrades without adopting the old library."""
    return getattr(store, 'media_root', '') or store.get('storage_base') or getattr(store, 'legacy_download_base', '')

def path(store, kind, fallback):
    if fixed(store):
        return store.media_root if kind in ('youtube', 'm3u') else fixed_folders(store)[kind]
    base=store.get('storage_base')
    return str(Path(base)/names(store)[kind]) if base else fallback

def apply(app, store):
    if fixed(store):
        app.config.update(MEDIA_DIR=store.media_root, M3U_DIR=store.media_root,
                          M3U_DOWNLOAD_ROOTS=json.dumps([fixed_folders(store)[k] for k in ('movies', 'series')]))
        return
    if not store.get('storage_base'):
        if app.extensions.pop('storage_base_applied', False):
            app.config.update(app.extensions.get('storage_defaults', {}))
        return
    app.extensions['storage_base_applied'] = True
    app.config['MEDIA_DIR']=path(store,'youtube',app.config['MEDIA_DIR'])
    app.config['M3U_DIR']=path(store,'m3u',app.config['M3U_DIR'])
    app.config['M3U_DOWNLOAD_ROOTS']=json.dumps([str(Path(store.get('storage_base'))/'Downloads')])

def validate_name(value):
    if not isinstance(value,str) or not value.strip() or len(value)>80 or value.startswith('.') or any(c in value for c in '/\\:') or any(ord(c)<32 for c in value):
        raise ValueError('Use a folder name without slashes, dots at the start or control characters.')
    return value.strip()

def busy_or_used(store):
    with store.connect() as db:
        return any(db.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone() for table in ('media_files','m3u_sources','m3u_downloads','downloads'))

def select(app,store,value):
    if fixed(store): raise ValueError(LOCKED)
    from .storage_locations import validate_choice
    candidate=validate_choice(app,store,value)
    old=store.get('storage_base')
    if old!=str(candidate) and busy_or_used(store): raise ValueError('Existing library paths are in use. They have been kept; moving a populated library requires a separate migration.')
    candidate.mkdir(parents=True,exist_ok=True)
    values=names(store)
    for name in (values['youtube'],values['m3u'],'Downloads'):
        m3u.contained(candidate,name).mkdir(exist_ok=True)
    for key in ('movies','series','unsorted'):
        m3u.contained(candidate/'Downloads',values[key]).mkdir(exist_ok=True)
    from .setup_storage import probe
    if not probe(candidate)['writable'] or not all(probe(candidate/name)['writable'] for name in (values['youtube'],values['m3u'],'Downloads')): raise ValueError('The base directory must be writable by ReelDock.')
    store.set('storage_base',str(candidate))
    from .storage_locations import host_path
    store.set('storage_host_base',host_path(app,candidate))
    store.set('m3u_download_folders',json.dumps({k:str(candidate/'Downloads'/values[k]) for k in ('movies','series','unsorted')}))
    apply(app,store)

def save_names(app,store,body):
    if fixed(store): raise ValueError(LOCKED)
    if not store.get('storage_base'): raise ValueError('Choose the base ReelDock directory in setup first.')
    if not isinstance(body,dict) or set(body)!=set(DEFAULTS): raise ValueError('Choose valid folder names.')
    values={k:validate_name(v) for k,v in body.items()}
    if len({values[k].casefold() for k in ('youtube','m3u') }|{'downloads'})!=3 or len({values[k].casefold() for k in ('movies','series','unsorted')})!=3: raise ValueError('Use separate names for each folder.')
    if values!=names(store) and busy_or_used(store): raise ValueError('Folder names are already used by your library. Existing files are kept; rename them through a separate migration.')
    base=Path(store.get('storage_base'))
    for key in ('youtube','m3u'):
        m3u.contained(base,values[key]).mkdir(exist_ok=True)
    for key in ('movies','series','unsorted'):
        m3u.contained(base/'Downloads',values[key]).mkdir(exist_ok=True)
    store.set('storage_names',json.dumps(values))
    store.set('m3u_download_folders',json.dumps({k:str(base/'Downloads'/values[k]) for k in ('movies','series','unsorted')}))
    apply(app,store)

def source_folder(store,db,name,identity):
    if not fixed(store) and not store.get('storage_base'): return identity
    folder=m3u.filename(name)[:80]
    if folder.startswith('.'): folder='Source '+identity[:8]
    if db.execute('SELECT 1 FROM m3u_sources WHERE folder=? COLLATE NOCASE',(folder,)).fetchone():folder+=' ['+identity[:8]+']'
    return folder

def export_path(store,value,folder=''):
    if fixed(store):
        parts = Path(value).parts
        if not parts or parts[0] not in ('Movie_VOD','TV_VOD','Unsorted_VOD','Live_TV'):
            raise ValueError('Unknown stream library category.')
        name = 'VOD TV Shows' if parts[0] in ('TV_VOD','Live_TV') else 'VOD Movies'
        extra = (parts[0],) if parts[0] in ('Unsorted_VOD','Live_TV') else ()
        return str(Path(name, folder, *extra, *parts[1:]))
    if not store.get('storage_base'):return value
    parts=list(Path(value).parts)
    mapping={'Movie_VOD':'movies','TV_VOD':'series','Unsorted_VOD':'unsorted'}
    if parts and parts[0] in mapping:parts[0]=names(store)[mapping[parts[0]]]
    return str(Path(*parts))
