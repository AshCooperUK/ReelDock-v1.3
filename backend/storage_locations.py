"""Authenticated directory selection within installation-provided mounts."""
import json
import hashlib
import logging
import os
from pathlib import Path
from . import m3u

MAX_CONFIG_BYTES = 32768
MAX_LOCATIONS = 64


def _path(value):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or value.startswith('//') or not Path(value).is_absolute()
            or '..' in Path(value).parts or Path(value) == Path('/')):
        raise ValueError('Invalid configured storage path.')
    return str(Path(value))


def _array(app, key):
    raw = app.config.get(key)
    if raw is None or raw == '':
        return [], True
    try:
        if isinstance(raw, str):
            if len(raw.encode()) > MAX_CONFIG_BYTES:
                return [], False
            value = json.loads(raw)
        else:
            value = raw
        if not isinstance(value, list) or len(value) > MAX_LOCATIONS:
            return [], False
        if len(json.dumps(value).encode()) > MAX_CONFIG_BYTES:
            return [], False
        return value, True
    except (ValueError, TypeError, RecursionError):
        return [], False


def _warning(app, key, message=''):
    extensions = getattr(app, 'extensions', {})
    warnings = extensions.setdefault('storage_configuration_warnings', {})
    if not message:
        warnings.pop(key, None)
        return
    fingerprint = hashlib.sha256(repr(app.config.get(key)).encode()).hexdigest()
    if warnings.get(key, (None,))[0] != fingerprint:
        logging.getLogger(__name__).warning('%s', message)
    warnings[key] = (fingerprint, message)


def configuration_warnings(app):
    """Active, sanitized diagnostics; never expose the malformed environment value."""
    return [value[1] for value in getattr(app, 'extensions', {}).get('storage_configuration_warnings', {}).values()]


def roots(app):
    """Invalid extra mounts grant no access; an explicit fixed mount remains exact."""
    fixed = app.config.get('REELDOCK_MEDIA_ROOT')
    try:
        canonical = _path(fixed) if fixed else ''
    except ValueError:
        _warning(app, 'REELDOCK_MEDIA_ROOT', 'REELDOCK_MEDIA_ROOT is invalid. Storage selection is disabled; correct the dedicated media mount.')
        return []
    _warning(app, 'REELDOCK_MEDIA_ROOT')
    values, valid = _array(app, 'REELDOCK_STORAGE_ROOTS')
    try:
        parsed = list(dict.fromkeys(_path(value) for value in values)) if valid else []
    except ValueError:
        valid = False
        parsed = []
    if valid:
        _warning(app, 'REELDOCK_STORAGE_ROOTS')
    else:
        suffix = 'Only the explicitly configured media mount is retained.' if canonical else 'Storage selection is disabled until this setting is corrected.'
        _warning(app, 'REELDOCK_STORAGE_ROOTS', 'REELDOCK_STORAGE_ROOTS must be a JSON array of dedicated absolute mount paths. Invalid additional locations were disabled. ' + suffix)
    if canonical and canonical not in parsed:
        parsed.insert(0, canonical)
    return parsed


def _metadata(app):
    values, valid = _array(app, 'REELDOCK_STORAGE_LOCATIONS')
    result = []
    if valid:
        try:
            for value in values:
                if not isinstance(value, dict) or not set(value) <= {'path', 'host_path', 'label', 'select_root'}:
                    raise ValueError()
                item = dict(value, path=_path(value.get('path')))
                if 'host_path' in item and item['host_path'] != '':
                    item['host_path'] = _path(item['host_path'])
                if 'label' in item and (not isinstance(item['label'], str) or len(item['label']) > 180):
                    raise ValueError()
                if 'select_root' in item and type(item['select_root']) is not bool:
                    raise ValueError()
                result.append(item)
        except ValueError:
            valid = False
    if not valid:
        _warning(app, 'REELDOCK_STORAGE_LOCATIONS', 'REELDOCK_STORAGE_LOCATIONS must be a JSON array of valid location mappings. Invalid mappings and whole-mount selection were disabled; correct this setting before selecting a mount root.')
        return None
    _warning(app, 'REELDOCK_STORAGE_LOCATIONS')
    return result


def locations(app):
    configured=_metadata(app)
    allowed=roots(app)
    result=[]
    for root in allowed:
        item=next((v for v in configured or [] if v.get('path')==root),{})
        host=item.get('host_path') or (app.config.get('REELDOCK_HOST_ROOT') if root=='/reeldock' else '') or root
        result.append(dict(path=root,host_path=host,label=item.get('label') or host,
                           select_root=False if configured is None else item.get('select_root',True)))
    return result


def resolve(app, value):
    if not isinstance(value,str) or not Path(value).is_absolute() or '..' in Path(value).parts:
        raise ValueError('Choose an absolute folder path from an available storage location.')
    candidate=Path(value)
    # Host paths are what the user sees. Translate the most specific mapping.
    options=sorted(locations(app),key=lambda v:len(Path(v['host_path']).parts),reverse=True)
    match=next((v for v in options if candidate.is_relative_to(v['host_path'])),None)
    if match:candidate=Path(match['path'])/candidate.relative_to(match['host_path'])
    options=sorted(locations(app),key=lambda v:len(Path(v['path']).parts),reverse=True)
    match=next((v for v in options if candidate.is_relative_to(v['path'])),None)
    if not match or not Path(match['path']).is_dir():
        raise ValueError('This location is not available to ReelDock. Choose a folder using Browse folders.')
    if any('ytsd' in part.lower() for part in candidate.parts):raise ValueError('Choose a dedicated ReelDock folder, separate from YTSD.')
    root=Path(match['path'])
    candidate=m3u.contained(root,candidate.relative_to(root))
    data=Path(app.config['DATA_DIR']).resolve()
    if candidate.resolve().is_relative_to(data) or data.is_relative_to(candidate.resolve()):
        raise ValueError('Keep application data separate from the media directory.')
    protected=[data,Path(__file__).resolve().parent.parent]
    for ancestor in (candidate,*candidate.parents):
        if ancestor.exists() and any(p.exists() and ancestor.samefile(p) for p in protected):
            raise ValueError('Keep media separate from application data and source files.')
    return candidate,match


def host_path(app,value):
    path=Path(value)
    for location in sorted(locations(app),key=lambda v:len(Path(v['path']).parts),reverse=True):
        if path.is_relative_to(location['path']):return str(Path(location['host_path'])/path.relative_to(location['path']))
    return str(path)


def validate_choice(app,store,value):
    path,location=resolve(app,value)
    if path==Path(location['path']) and not location['select_root']:
        raise ValueError('Choose a dedicated ReelDock folder inside this location, not the whole location.')
    current=store.get('storage_base')
    # A general host mount must never let reset adopt an unrelated populated folder.
    if str(path)!=current and path.exists() and any(path.iterdir()) and not empty_scaffold(path,location):
        raise ValueError('Choose an empty folder. Existing files will not be merged or overwritten.')
    return path


def empty_scaffold(path,location):
    # Reset preserves the empty directories used by dedicated bind mounts.
    if path!=Path(location['path']) or not location['select_root']:return False
    allowed={'YouTube','M3U','Downloads','Movies','TV Shows','Unsorted'}
    for directory,dirs,files in os.walk(path,followlinks=False):
        if files or any(name not in allowed or (Path(directory)/name).is_symlink() for name in dirs):return False
    return True


def browse(app,store,value=''):
    available=locations(app)
    if not value:return dict(locations=available,host_path='',path='',parent='',folders=[])
    path,location=resolve(app,value)
    if not path.is_dir():raise ValueError('This folder does not exist. Choose its parent and enter a new folder name.')
    rows=[]
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if len(rows)>=1000:raise ValueError('This folder has too many entries. Enter a more specific path.')
                if entry.name.startswith('.') or 'ytsd' in entry.name.lower() or not entry.is_dir(follow_symlinks=False):continue
                child=Path(entry.path)
                try:resolve(app,str(child))
                except ValueError:continue
                rows.append(dict(name=entry.name,path=str(child),host_path=host_path(app,child)))
    except PermissionError:raise ValueError('ReelDock does not have permission to browse this folder.') from None
    try:
        validate_choice(app,store,str(path)); selectable=os.access(path,os.W_OK|os.X_OK)
    except ValueError:selectable=False
    return dict(locations=available,path=str(path),host_path=host_path(app,path),
                parent=str(path.parent) if path!=Path(location['path']) else '',
                writable=os.access(path,os.W_OK|os.X_OK),
                selectable=selectable,
                folders=sorted(rows,key=lambda row:row['name'].casefold()))
