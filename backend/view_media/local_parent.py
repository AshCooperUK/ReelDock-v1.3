"""Click-time exact identity for a Local Library parent card (read-only)."""
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from . import local
from ..media_management import files

SHOW_KEY = re.compile(r'show:([1-9][0-9]{0,11}):[0-9]{1,4}:[0-9]{1,4}')
GROUP = re.compile(r'group-[0-9a-f]{24}')
NUMBER = re.compile(r'[1-9][0-9]{0,11}')


def _group(app, identity):
    if not GROUP.fullmatch(identity or ''):
        return None
    root = Path(app.config['MEDIA_DIR']).resolve()
    state = local.inventory(root)
    cards = local.grouped(state['items'], state, root)
    card = next((c for c in cards if c.get('action', {}).get('type') == 'local-group' and c['action']['id'] == identity), None)
    return (root, state, card) if card else None


def _children(store, root, state, card):
    """Checked (absolute, relative) paths for every child, or None if any is unsafe."""
    result = []
    for child in card.get('children') or []:
        entry = state['files'].get((child.get('action') or {}).get('id'))
        path = entry.get('path') if entry else None
        if path is None:
            return None
        try:
            path = files.checked(store, path)
            relative = path.relative_to(root)
        except (ValueError, OSError):
            return None
        result.append((path, str(relative)))
    return result or None


def _rows(store, sql, children):
    with store.connect() as db:
        return [row[0] for path, relative in children for row in db.execute(sql, (str(path), relative))]


def _tvshow_nfo(store, folder):
    try:
        before = files.snapshot(store, folder / 'tvshow.nfo')
        if not 0 < before.st_size < 100000:
            return None
        with files.directory(store, folder) as fd:
            source = os.open('tvshow.nfo', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(source, 'rb') as stream:
                files.same(before, os.fstat(stream.fileno()))
                raw = stream.read(100000)
            files.verify(fd, 'tvshow.nfo', before)
        if len(raw) >= 100000 or b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
            return None
        meta = ET.fromstring(raw)
        return meta if meta.tag == 'tvshow' else None
    except (OSError, ValueError, ET.ParseError):
        return None


def _nfo_identity(meta):
    def valid(value):
        value = (value or '').strip()
        return value if NUMBER.fullmatch(value) else ''
    ids = {name: valid(meta.findtext(name + 'id')) for name in ('tvdb', 'tmdb')}
    for node in meta.findall('uniqueid'):
        kind = str(node.get('type') or '').lower()
        if kind in ids and not ids[kind]:
            ids[kind] = valid(node.text)
    return ('tvdb', ids['tvdb']) if ids['tvdb'] else ('tmdb', ids['tmdb']) if ids['tmdb'] else None


def _show_folders(root, children):
    folders = {}
    for path, _ in children:
        folder = path.parent
        if local._SEASON_DIR.match(folder.name):
            folder = folder.parent
        if folder == root or root not in folder.parents:
            continue
        folders[folder] = True
    return list(folders)


def _show(app, store, root, children, card):
    keys = _rows(store, "SELECT key FROM discover_owned WHERE state='complete' AND path IN (?,?)", children)
    owned = {m.group(1) for m in (SHOW_KEY.fullmatch(key or '') for key in keys) if m}
    if len(owned) > 1:
        return None
    base = dict(kind='show', title=card['title'], image=card.get('image') or '', backdrop=card.get('backdrop') or '')
    if owned:
        show = next(iter(owned))
        return dict(base, id='tvdb:show:' + show, externalId=show)
    found = set()
    for folder in _show_folders(root, children):
        meta = _tvshow_nfo(store, folder)
        identity = _nfo_identity(meta) if meta is not None else None
        if identity:
            found.add(identity)
    if len(found) != 1:
        return None
    provider, show = next(iter(found))
    if provider == 'tvdb':
        return dict(base, id='tvdb:show:' + show, externalId=show)
    from .providers import details
    try:
        detail = details(store, 'tmdb:show:' + show)
    except Exception:
        return None
    external = str(detail.get('externalId') or '')
    if not NUMBER.fullmatch(external) or not detail.get('id'):
        return None
    return {**detail, **{k: v for k, v in base.items() if v}, 'id': detail['id'], 'externalId': external, 'kind': 'show'}


def resolve(app, store, identity):
    """None for an invalid/stale group; otherwise {'target': ...|None}."""
    found = _group(app, identity)
    if found is None:
        return None
    root, state, card = found
    if card.get('kind') not in ('shows', 'youtube'):
        return dict(target=None)
    children = _children(store, root, state, card)
    if children is None:
        return dict(target=None)
    if card['kind'] == 'youtube':
        # Library groups contain saved files, not the channel's public uploads.
        # The explicit channel identity link remains a separate navigation action.
        return dict(target=None)
    item = _show(app, store, root, children, card)
    return dict(target=dict(type='discover', item=item) if item else None)
