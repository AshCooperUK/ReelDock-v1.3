"""Lazy, bounded local-media inventory. Never follows links or leaves the media root."""
import hashlib
import os
import re
import threading
import stat
import time
from pathlib import Path
import xml.etree.ElementTree as ET
from ..downloaders.torrent.media import is_sample, VIDEO

_LOCK = threading.Lock()
_CACHE = {}
EXTENSIONS = VIDEO

def invalidate(root):
    with _LOCK:_CACHE.pop(str(Path(root).resolve()),None)


def refresh(root):
    """Rescan in the background while the last inventory keeps serving."""
    root = Path(root).resolve()
    with _LOCK:
        state = _CACHE.get(str(root))
        if state is None:
            state = dict(items=[], files={}, scanning=False, checked=0, warning='')
            _CACHE[str(root)] = state
        if state['scanning']:return
        state['scanning'] = True
        state['checked'] = time.time()
        state['_scan_token'] = token = object()
        threading.Thread(target=_scan, args=(root, state, token), daemon=True, name='view-media-local').start()


def inventory(root):
    root = Path(root).resolve()
    key = str(root)
    with _LOCK:
        state = _CACHE.get(key)
        if state is None or (not state['scanning'] and time.time() - state['checked'] > 300):
            state = dict(items=state['items'] if state else [], files=state['files'] if state else {}, scanning=True, checked=time.time(), warning='')
            _CACHE[key] = state
            state['_scan_token'] = token = object()
            threading.Thread(target=_scan, args=(root, state, token), daemon=True, name='view-media-local').start()
    return state


def _entry(root, path):
    """Compose one already-discovered leaf without scanning sibling folders."""
    if path.name.startswith('.') or path.suffix.lower() not in EXTENSIONS or path.is_symlink() or is_sample(str(path.relative_to(root))):
        return None
    name = path.name
    relative = str(path.relative_to(root))
    if any(p.lower() in ('temp', 'tmp', 'work', 'incomplete', 'downloads-in-progress', 'auto-download', 'staging') or p.lower().startswith('reeldock replacement') for p in path.relative_to(root).parts[:-1]): return None
    identity = hashlib.sha256(relative.encode()).hexdigest()[:32]
    meta = None
    nfo = path.with_suffix('.nfo')
    if nfo.is_file() and not nfo.is_symlink() and nfo.stat().st_size < 100000:
        try: meta = ET.fromstring(nfo.read_bytes())
        except (OSError, ET.ParseError): pass
    def value(key): return (meta.findtext(key) or '') if meta is not None else ''
    se_match = re.search(r'(?i)S(\d{1,4})E(\d{1,4})', name)
    kind = 'youtube' if any(p.lower().startswith('youtube') for p in path.relative_to(root).parts[:-1]) else 'shows' if (meta is not None and meta.tag == 'episodedetails') or se_match else 'movies'
    title_parts, nested = _group_parts(path, root)
    show = value('showtitle')
    if kind=='shows' and not show and se_match:
        show = re.sub(r'[._]+',' ',name[:se_match.start()]).strip(' -')[:200]
    # Dir art only comes from a real title folder — never a bare
    # category root, whose poster would leak onto every file in it.
    title_dir = _title_folder(path, root, show, kind)
    art_dirs = [path.parent] if path.parent != Path(root) and (nested or len(title_parts) >= 2 or title_dir is not None) else []
    if title_dir is not None and title_dir != path.parent: art_dirs.append(title_dir)
    # Per-file art first (same-stem image or yt-dlp -thumb sidecar),
    # then the title folder's poster — never the category root's.
    art = next((p for p in (path.with_suffix('.jpg'), *(path.parent/(path.stem+'-thumb'+ext) for ext in ('.jpg','.jpeg','.png','.webp')), *(c for d in art_dirs for c in (d/'poster.jpg', d/'folder.jpg'))) if p.is_file() and not p.is_symlink()), None)
    backdrop = next((c for d in art_dirs for c in (d/'fanart.jpg',) if c.is_file() and not c.is_symlink()), None)
    entry = dict(path=path, art=art, backdrop=backdrop or art)
    base = '/api/view-media/local/' + identity
    title=value('title') or path.stem
    season = int(value('season')) if value('season').isdigit() else (int(se_match[1]) if se_match else None)
    number = int(value('episode')) if value('episode').isdigit() else (int(se_match[2]) if se_match else None)
    if kind=='shows' and show and season is not None and number is not None and not re.search(r'(?i)S\d{1,4}E\d{1,4}', title):
        title=f"{show} S{season:02d}E{number:02d} - {title}"
    item = dict(id='local:'+identity, title=title, kind=kind,
        image=base+'/art' if art else '', backdrop=base+'/backdrop' if entry['backdrop'] else '',
        year=value('year'),releaseDate=value('aired') or value('premiered'), description=value('plot'), date=value('aired') or value('premiered'),
        rating=value('rating'), added=None, show=show, season=season, number=number,
        channel=_channel_name(title_parts) if kind=='youtube' else '',
        action=dict(type='local', id=identity))
    # NFO runtime is explicitly minutes. Never probe every file on a page read.
    runtime = value('runtime').strip()
    if re.fullmatch(r'\d{1,6}(?:\.\d{1,3})?', runtime) and 0<float(runtime)<=525600:
        item['runtimeMinutes'] = float(runtime)
    return identity, item, entry


def _scan(root, state, token=None):
    items, files, count = [], {}, 0
    warning = ''
    try:
        for folder, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not d.lower().startswith('reeldock replacement') and not (Path(folder)/d).is_symlink())
            for name in sorted(names):
                count += 1
                if count > 300000:
                    warning = 'Local scan reached its file limit. Some files may not be listed.'
                    break
                path = Path(folder)/name
                if name.startswith('.') or path.suffix.lower() not in EXTENSIONS or path.is_symlink() or is_sample(str(path.relative_to(root))): continue
                result = _entry(root, path)
                if result:
                    identity, item, entry = result
                    files[identity] = entry
                    items.append(item)
            if count > 300000: break
    except OSError:
        warning = 'Some local folders could not be read.'
    except Exception:
        warning = 'The local scan stopped unexpectedly.'
    finally:
        # One atomic publish: dict.update is GIL-atomic for Python readers, so
        # nobody sees new items paired with old files, and scanning always resets.
        with _LOCK:
            if token is None or (_CACHE.get(str(root)) is state and state.get('_scan_token') is token):
                state.update(dict(items=items, files=files, checked=time.time(), warning=warning, scanning=False))
                _CACHE[str(root)] = state


def cached_snapshot(root, limit=None, kinds=None, group_shows=False):
    """Preserve known leaves across ownership's ordinary cache invalidation."""
    with _LOCK:
        state = _CACHE.get(str(Path(root).resolve()))
        if state is None:return None
        limited = False
        if group_shows and limit is not None:
            # The saved landing page limits title cards, not episode files.
            # Preserve every child of a selected show without letting a large
            # series consume the whole title window before grouping happens.
            items, selected = [], set()
            for item in state['items']:
                if kinds and item['kind'] not in kinds:continue
                group = _group_for(item, state, Path(root)) if item['kind']=='shows' else None
                key = ('show', group[0]) if group else ('file', item['action']['id'])
                if key not in selected and len(selected)>=limit:
                    limited = True
                    continue
                selected.add(key)
                items.append(item)
        elif kinds:
            from itertools import islice
            rows=(item for item in state['items'] if item['kind'] in kinds)
            items=list(islice(rows,limit)) if limit is not None else list(rows)
        else:items=state['items'][:limit] if limit is not None else state['items']
        keys={item['action']['id'] for item in items} if limit is not None else state['files']
        return dict(state, items=[dict(item) for item in items],
                    **({'groupLimited':limited} if group_shows else {}),
                    files={key:dict(state['files'][key]) for key in keys if key in state['files']})


def known_snapshot(root, paths, limit=1000, kinds=None, group_shows=False):
    """Register only bounded, already-recorded completed leaves; never start a scan."""
    root=Path(root).absolute();observed=[]
    if root.is_symlink() or root.resolve()!=root:return None
    for value in paths[:250]:
        path=Path(value);path=path if path.is_absolute() else root/path
        try:
            relative=path.relative_to(root)
            if '..' in relative.parts or any(part.startswith('.') for part in relative.parts) or path.suffix.lower() not in EXTENSIONS:continue
            if any(parent.is_symlink() for parent in (path,*path.parents)):continue
            descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            try:
                before=os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode):continue
                result=_entry(root,path)
                after=path.stat(follow_symlinks=False)
                if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino) or path.resolve()!=path:continue
                if result:observed.append(result)
            finally:os.close(descriptor)
        except (ValueError,OSError):continue
    if observed:
        with _LOCK:
            state=_CACHE.get(str(root))
            if state is None:
                state=dict(items=[],files={},scanning=False,checked=0,warning='')
                _CACHE[str(root)]=state
            previous=state.setdefault('_known_metadata',{})
            changed={identity for identity,item,entry in observed if state['files'].get(identity)!=entry or previous.get(identity)!=item}
            if changed:
                state['items']=[item for item in state['items'] if item['action']['id'] not in changed]
                for identity,item,entry in observed:
                    if identity in changed:state['items'].append(item);state['files'][identity]=entry;previous[identity]=item
    return cached_snapshot(root,limit,kinds,group_shows)


def register_group_art(root, snapshot, cards):
    """Publish generated parent artwork only while its exact leaves are current.

    Saved browsing groups a copied snapshot; its artwork routes still need the
    checked group entry in the canonical in-memory cache. A concurrent rename
    or replacement must never let an older snapshot restore stale leaf aliases.
    """
    with _LOCK:
        current = _CACHE.get(str(Path(root).resolve()))
        if current is None:return
        for card in cards:
            action = card.get('action') or {}
            if action.get('type')!='local-group':continue
            identity = action.get('id')
            entry = snapshot['files'].get(identity)
            children = card.get('children') or []
            if not entry or entry.get('path') is not None or not children:continue
            valid = True
            for child in children:
                key = (child.get('action') or {}).get('id')
                before = snapshot['files'].get(key)
                after = current['files'].get(key)
                if not before or not after or before.get('path')!=after.get('path'):
                    valid = False
                    break
            if valid:current['files'][identity] = dict(entry)


def retarget(root, changes, previous=None):
    """Publish exact renamed leaves immediately; old IDs receive no aliases."""
    root = Path(root).resolve()
    changes = {Path(old): Path(new) for old, new in changes.items()}
    leaves = [(old, new) for old, new in changes.items() if old.suffix.lower() in EXTENSIONS]
    old_ids = {hashlib.sha256(str(old.relative_to(root)).encode()).hexdigest()[:32] for old, _ in leaves}
    # Replace the state object first, invalidating any outstanding scan token.
    with _LOCK:
        prior = previous or dict(items=[], files={}, warning='')
        current = _CACHE.get(str(root)) or {}
        known_files = {**prior.get('files', {}), **current.get('files', {})}
        known_items = {item['action']['id']: item for source in (prior, current) for item in source.get('items', [])}
        state = dict(prior, **{key:value for key,value in current.items() if key not in ('items','files')})
        state.update(items=[dict(item) for item in known_items.values()],
                     files={key: dict(value) for key, value in known_files.items()},
                     scanning=False, checked=time.time())
        state.pop('_scan_token', None)
        _CACHE[str(root)] = state
        for key in old_ids:
            state['files'].pop(key, None)
        state['items'] = [item for item in state['items'] if item.get('action', {}).get('id') not in old_ids]
        for entry in state['files'].values():
            for part in ('path', 'art', 'backdrop'):
                if entry.get(part) in changes:
                    entry[part] = changes[entry[part]]
        for _, new in leaves:
            result = _entry(root, new)
            if result:
                identity, item, entry = result
                state['files'][identity] = entry
                state['items'] = [old for old in state['items'] if old.get('action', {}).get('id') != identity]
                state['items'].append(item)
    return [{'from': hashlib.sha256(str(old.relative_to(root)).encode()).hexdigest()[:32],
             'to': hashlib.sha256(str(new.relative_to(root)).encode()).hexdigest()[:32]} for old, new in leaves]


def media_file(root, identity, part):
    state = inventory(root)
    entry = state['files'].get(identity)
    path = entry.get({'file':'path', 'art':'art', 'backdrop':'backdrop'}.get(part, '')) if entry else None
    if not path or path.is_symlink() or not path.resolve().is_relative_to(Path(root).resolve()) or not path.is_file():
        raise ValueError('Local media is unavailable.')
    return path


def library_dates(store, state, root=None):
    """Use recorded completed imports, never filesystem modification times."""
    import json
    from ..media_duration import fields as duration_fields
    with store.connect() as db:
        rows = list(db.execute("SELECT o.path,r.updated FROM discover_owned o JOIN discover_requests r ON r.id=o.request_id WHERE o.state='complete'"))
        rows += list(db.execute("SELECT destination AS path,updated FROM m3u_downloads WHERE state='complete'"))
        saved = list(db.execute("SELECT m.path,m.channel,d.updated,v.data AS video_data FROM media_files m JOIN downloads d ON d.id=m.id LEFT JOIN videos v ON v.id=m.id WHERE d.state='complete'"))
        rows += saved
    dates = {str(Path(root or '.') / r['path']):r['updated'] for r in rows if r['path']}
    channels = {str(Path(root or '.') / row['path']):row['channel'] for row in saved
                if row['path'] and re.fullmatch(r'UC[A-Za-z0-9_-]{22}', row['channel'] or '')}
    durations={}
    for row in saved:
        try:video=json.loads(row['video_data'] or '{}')
        except (TypeError,ValueError):continue
        durations[str(Path(root or '.')/row['path'])]=duration_fields(seconds=video.get('duration_seconds'),clock=video.get('duration'))
    result=[]
    for item in state['items']:
        path=state['files'][item['action']['id']]['path']
        added=dates.get(str(path))
        if added is None:
            added=next((dates[str(p)] for p in path.parents if str(p) in dates),None)
        result.append(dict(item,added=added,**durations.get(str(path),{}),**({'channelId':channels[str(path)]} if item['kind']=='youtube' and str(path) in channels else {})))
    return result


_SEASON_DIR = re.compile(r'(?i)^(season[\s_.-]*\d+|specials|extras)$')

def _group_parts(path, root):
    """Containing folder minus trailing season/extras levels — the title folder."""
    raw = path.relative_to(root).parent.parts
    parts = list(raw)
    while parts and _SEASON_DIR.match(parts[-1]): parts.pop()
    return parts, len(parts) != len(raw)


def _title_folder(path, root, show='', kind=''):
    """The item's title folder, or None when the only container is a bare
    category root (`Movies`, `TV Shows`, `YouTube …`) or the media root
    itself — those never own per-title art or a group card."""
    parts, stripped = _group_parts(path, root)
    if kind == 'youtube':
        for i, part in enumerate(parts):
            if part.lower().startswith('youtube') and i + 1 < len(parts):
                return Path(root, *parts[:i + 2])
        return None
    if len(parts) > 1: return Path(root, *parts)
    if len(parts) == 1 and stripped and show and parts[0].casefold() == show.casefold():
        return Path(root, *parts)
    return None


def _channel_name(parts):
    """Channel folder = component directly under the `youtube*` category dir."""
    for i, part in enumerate(parts):
        if part.lower().startswith('youtube'):
            return parts[i + 1] if i + 1 < len(parts) else ''
    return ''


def _group_for(item, state, root):
    """(group key, title, folder path or None) for a nested title folder.
    Category-root files (no title folder) never group for TV/YouTube; loose
    files may still join a named show group from NFO/filename identity."""
    entry = state['files'].get(item['action']['id'])
    if not entry or not entry.get('path'): return None
    parts, stripped = _group_parts(entry['path'], root)
    folder = '/'.join(parts)
    kind, show = item['kind'], (item.get('show') or '').strip()
    if kind == 'shows':
        if len(parts) > 1 or (len(parts) == 1 and stripped and show and parts[0].casefold() == show.casefold()):
            return ('dir:'+folder.casefold(), show or parts[-1], folder)
        if show: return ('show:'+show.casefold(), show, None)
        return None
    if kind == 'youtube':
        idx = next((i for i, p in enumerate(parts) if p.lower().startswith('youtube')), -1)
        if idx >= 0 and idx + 1 < len(parts):
            folder = '/'.join(parts[:idx + 2])
            return ('dir:'+folder.casefold(), parts[idx + 1], folder)
        return None
    if kind == 'movies' and len(parts) >= 2:
        return ('dir:'+folder.casefold(), parts[-1], folder)
    return None


def _child_rank(state):
    def rank(item):
        entry = state['files'].get(item['action']['id']) or {}
        filename = str(entry.get('path') and entry['path'].name or '').casefold()
        season, number = item.get('season'), item.get('number')
        return (season is None, season if type(season) is int else 0,
                number is None, number if type(number) is int else 0,
                str(item.get('title') or '').casefold(), filename)
    return rank


def _parent_card(store_state, root, key, folder, title, kind, children):
    """One card representing a whole nested folder; art prefers the title
    folder's own poster/fanart and falls back to the first child's art."""
    gid = 'group-' + hashlib.sha256(key.encode()).hexdigest()[:24]
    art = backdrop = None
    if folder:
        base_dir = Path(root)/folder
        art = next((p for p in (base_dir/'poster.jpg', base_dir/'folder.jpg', base_dir/'poster.png') if p.is_file() and not p.is_symlink()), None)
        candidate = base_dir/'fanart.jpg'
        backdrop = candidate if candidate.is_file() and not candidate.is_symlink() else None
    if art or backdrop:
        store_state['files'][gid] = dict(path=None, art=art, backdrop=backdrop or art)
        image, banner = '/api/view-media/local/'+gid+'/art' if art else '', '/api/view-media/local/'+gid+'/backdrop' if backdrop else ''
    else:
        image, banner = children[0].get('image') or '', children[0].get('backdrop') or children[0].get('image') or ''
    years = sorted(str(c['year'])[:4] for c in children if str(c.get('year') or '')[:4].isdigit())
    dates = sorted(str(c['releaseDate']) for c in children if c.get('releaseDate'))
    added = [c['added'] for c in children if c.get('added')]
    channel_ids = {child['channelId'] for child in children if child.get('channelId')}
    return dict(id='local-group:'+gid, title=title, kind=kind, image=image, backdrop=banner,
                channel=title if kind=='youtube' else '',
                **({'channelId':next(iter(channel_ids))} if kind=='youtube' and len(channel_ids)==1 else {}),
                year=years[0] if years else '', releaseDate=dates[-1] if dates else '',
                description='', rating='', added=max(added) if added else None,
                count=len(children), children=children,
                action=dict(type='local-group', id=gid))


def grouped(items, state, root):
    """Collapse nested shows/YouTube folders (and multi-file movie folders)
    into one parent card per title folder; ordering is applied afterwards."""
    groups, sequence = {}, []
    for item in items:
        found = _group_for(item, state, root)
        if not found: sequence.append(('item', item)); continue
        key = found[0]
        if key not in groups:
            groups[key] = dict(title=found[1], folder=found[2], kind=item['kind'], items=[])
            sequence.append(('group', key))
        groups[key]['items'].append(item)
    result = []
    for kind, payload in sequence:
        if kind == 'item': result.append(payload); continue
        group = groups[payload]
        children = sorted(group['items'], key=_child_rank(state))
        # One saved movie or YouTube upload keeps its own file card. Channel
        # membership never hides a one-off manual download behind its catalogue.
        if len(children) == 1 and group['kind'] in ('movies','youtube'): result.append(children[0]); continue
        result.append(_parent_card(state, root, payload, group['folder'], group['title'], group['kind'], children))
    return result
