"""Managed files only: descriptor-walked mutations and staged metadata/replacements."""
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import store as records
from ..media_library import contained
from ..storage_layout import download_base, names
from ..downloaders.torrent.media import VIDEO, ARTWORK, INFO, SUBTITLE
from ..vpn.transport import metadata as vpn_metadata

ARTWORK_UNAVAILABLE = 'Artwork unavailable; use Refresh Media to retry.'
# A movie folder is only removable when nothing but metadata/artwork remains.
REMOVABLE = ARTWORK | INFO | SUBTITLE | {'.edl', '.tbn'}
QUALITY_RANK = {'480': 0, '720': 1, '1080': 2, '2160': 3}
# ffprobe format_name tokens accepted for each saved extension.
CONTAINERS = {'.mp4': {'mov', 'mp4'}, '.m4v': {'mov', 'mp4'}, '.mov': {'mov', 'mp4'},
              '.3gp': {'mov', 'mp4'}, '.mkv': {'matroska'}, '.webm': {'webm', 'matroska'},
              '.ts': {'mpegts'}, '.m2ts': {'mpegts'}, '.mts': {'mpegts'},
              '.avi': {'avi'}, '.divx': {'avi'}, '.wmv': {'asf'}, '.flv': {'flv'},
              '.mpg': {'mpeg', 'mpegps'}, '.mpeg': {'mpeg', 'mpegps'}, '.vob': {'mpegps'},
              '.m2v': {'mpeg', 'mpegps'}, '.ogv': {'ogg'}}


def checked(store, path, *, allow_root=False):
    """Containment alone resolves links; reject them before resolving anything."""
    base = str(download_base(store) or '')
    if not base or not Path(base).is_absolute():
        raise ValueError('The ReelDock media folder is unavailable.')
    root = Path(base)
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    if '..' in p.parts or '..' in root.parts:
        raise ValueError('Media path must stay within the media folder.')
    p.relative_to(root)
    cursor = Path('/')
    for part in p.parts[1:]:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError('Symbolic links are not managed media paths.')
    if allow_root and p == root:
        if root.resolve()!=root:raise ValueError('Symbolic links are not managed media paths.')
        return root
    resolved=contained(root,str(p.relative_to(root)))
    if resolved!=p:raise ValueError('Symbolic links are not managed media paths.')
    return p  # Keep the lexical path for the later O_NOFOLLOW descriptor walk.


@contextmanager
def directory(store, path):
    path = checked(store, path, allow_root=True)
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        # Detect a directory substituted while walking; subsequent work uses fd.
        same(path.stat(follow_symlinks=False), os.fstat(fd))
        yield fd
    finally:
        os.close(fd)


def same(before, current):
    if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
        raise ValueError('File changed; refresh before changing saved media.')


def snapshot(store, p):
    p = checked(store, p)
    result = p.stat(follow_symlinks=False)
    if not stat.S_ISREG(result.st_mode):
        raise ValueError('Only regular saved files can be changed.')
    return result


def verify(fd, name, before):
    current = os.stat(name, dir_fd=fd, follow_symlinks=False)
    same(before, current)
    if not stat.S_ISREG(current.st_mode):
        raise ValueError('Only regular saved files can be changed.')


def absent(fd, name):
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise ValueError('The destination already exists.')


def move(fd, old, new, before):
    verify(fd, old, before)
    absent(fd, new)
    rename_noreplace(fd, old, new)


def unlink_verified(fd, name, before):
    """Quarantine the leaf under a hidden name, then unlink only that inode.

    A check-then-unlink race could delete a file substituted after verify();
    renaming first is atomic, so a swapped entry is moved back untouched.
    """
    quarantine = '.reeldock-unlink-' + uuid.uuid4().hex
    rename_noreplace(fd, name, quarantine)
    try:
        verify(fd, quarantine, before)
    except BaseException:
        try:
            rename_noreplace(fd, quarantine, name)
        except OSError:
            pass
        raise
    os.unlink(quarantine, dir_fd=fd)


def rename_noreplace(fd, old, new):
    # Linux renameat2 closes the destination-exists race for files and folders.
    # Fail safely on a filesystem without this primitive; never clobber a leaf.
    import ctypes
    libc=ctypes.CDLL(None,use_errno=True)
    renameat2=getattr(libc,'renameat2',None)
    if renameat2 is None:raise OSError('Atomic rename without replacement is unavailable.')
    renameat2.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
    renameat2.restype=ctypes.c_int
    if renameat2(fd,os.fsencode(old),fd,os.fsencode(new),1):
        number=ctypes.get_errno()
        raise OSError(number,os.strerror(number))


def invalidate():
    """Rescan every watched local root while previous results stay visible."""
    from ..view_media import local
    for root in list(local._CACHE):
        local.refresh(root)


def drop_saved_local(store, paths, root):
    """Forget saved Local Library cards whose files were just moved or deleted."""
    try:
        base = Path(root).resolve()
    except OSError:
        return
    ids = []
    for p in paths:
        try:
            ids.append(hashlib.sha256(str(Path(p).resolve().relative_to(base)).encode()).hexdigest()[:32])
        except (OSError, ValueError):
            continue
    if not ids:
        return
    from ..view_media.saved_catalogue import signature, initialise
    initialise(store)
    with store.connect() as db:
        db.execute("DELETE FROM saved_media_catalogue WHERE signature=? AND origin='Local Library' AND json_extract(body,'$.action.id') IN ("+','.join('?' for _ in ids)+")",
                   (signature(store), *ids))


def idle(store, r):
    from ..acquisition import ACTIVE_DISCOVER
    with store.connect() as db:
        rows = db.execute('SELECT state FROM discover_requests WHERE episode_key=?', (r['key'],)).fetchall()
        if any(q['state'] in ACTIVE_DISCOVER or q['state'] == 'importing' for q in rows):
            raise ValueError('Cancel the active acquisition before changing this item.')
        if r['kind'] == 'video':
            video = json.loads(r['body'])['item'].get('videoId','')
            q = db.execute('SELECT state FROM downloads WHERE id=?', (video,)).fetchone()
            if q and q['state'] in ('queued', 'downloading', 'cancelling'):
                raise ValueError('Cancel the active acquisition before changing this item.')


def saved(app, store, r):
    from .service import library_file
    p = library_file(app, store, r)
    if not p:
        raise ValueError('No saved file is available.')
    if p.suffix.lower() not in VIDEO:
        raise ValueError('No saved video file is available.')
    return checked(store, p)


def videos(store, p):
    """Only the exact leaf and Emby cd siblings belong to this managed movie."""
    base = re.sub(r'-cd[1-9]$', '', p.stem, flags=re.I)
    result = [p]
    if base != p.stem:
        for child in p.parent.iterdir():
            if child != p and child.suffix.lower() in VIDEO and re.fullmatch(re.escape(base)+r'-cd[1-9]', child.stem, re.I):
                snapshot(store, child)
                result.append(child)
    return sorted(result)


def movie_folder(store, p):
    # p stays lexical (checked() returns the joined path), so compare lexically.
    root = Path(download_base(store))
    protected = {root, *(root / name for name in names(store).values())}
    if p.parent in protected:
        raise ValueError('This is a shared media folder.')


def rename(app, store, r, title):
    from ..downloaders.torrent.rename import library_path, parse_save_as
    if r['kind'] == 'video':
        raise ValueError('YouTube videos cannot be renamed here.')
    if not isinstance(title, str) or not title.strip() or len(title) > 180 or any(c in title for c in '/\\') or any(ord(c) < 32 for c in title):
        raise ValueError('Enter a display title without path separators.')
    title = title.strip()
    if title.strip(' .') == '':
        raise ValueError('Enter a display title.')
    with store.exclusive_operation():
        idle(store, r)
        p = saved(app, store, r)
        before = snapshot(store, p)
        b = json.loads(r['body']); item = b['item']; episode = b.get('episode') or {}
        if r['kind'] == 'movie':
            parsed = parse_save_as(title)
            year = str(item.get('year') or parsed.get('year') or '')
            label = parsed['name'] + (' ('+year+')' if re.fullmatch(r'\d{4}', year) else '')
        else:
            year = str(item.get('year') or '')
            label = item['title'] + (' ('+year+')' if re.fullmatch(r'\d{4}', year) else '')
            if type(episode.get('season')) is int and type(episode.get('number')) is int:
                label += f" S{episode['season']:02d}E{episode['number']:02d} - {title}"
            else:
                label = title
        planned = library_path('', p.name, 'video', 'movie' if r['kind'] == 'movie' else 'series',
                               store.get('metadata_type', 'emby'), names(store), label,
                               flat_episodes=r['kind'] == 'episode' and not p.parent.name.lower().startswith('season'))
        if not planned:
            raise ValueError('The corrected name could not be determined.')
        if r['kind']=='episode' and episode.get('season')==0 and type(episode.get('number')) is int:
            from ..m3u import filename
            planned=str(Path(planned).with_name(filename(f"{item['title']} s00e{episode['number']:02d} - {title}")+p.suffix))
        destination = checked(store, p.parent.parent / Path(planned).parent.name / Path(planned).name) if r['kind'] == 'movie' else checked(store, p.with_name(Path(planned).name))
        if r['kind'] == 'movie':
            movie_folder(store, p)
            old_base = re.sub(r'-cd[1-9]$', '', p.stem, flags=re.I)
            new_base = Path(planned).stem
            members = videos(store, p)
            changes = {}
            for child in p.parent.iterdir():
                state = snapshot(store, child)  # No directory/link is carried into a new folder.
                if child.suffix.lower() in VIDEO and child not in members:
                    raise ValueError('This movie folder contains another video.')
                name = child.name
                if name.startswith(old_base) and (len(name) == len(old_base) or not name[len(old_base)].isalnum()):
                    name = new_base + name[len(old_base):]
                elif name.startswith(p.parent.name) and (len(name)==len(p.parent.name) or not name[len(p.parent.name)].isalnum()):
                    name = new_base + name[len(p.parent.name):]
                changes[child.name] = (name, state)
            new_leaf = changes[p.name][0]
            destination = destination.with_name(new_leaf)
        else:
            changes = {p.name: (destination.name, before)}
            # An episode never moves shared artwork, subtitles or its show/season directory.
            nfo = checked(store, p.with_suffix('.nfo'))
            if nfo.exists():
                changes[nfo.name] = (destination.with_suffix('.nfo').name, snapshot(store, nfo))
        if destination == p:
            raise ValueError('The saved file already has this name.')
        if 'sample' in str(destination.relative_to(Path(download_base(store)))).lower():
            raise ValueError('Sample content excluded.')
        target_relative=str(destination.relative_to(Path(download_base(store))))
        with store.connect() as db:
            reserved=db.execute('SELECT 1 FROM media_files WHERE path COLLATE NOCASE IN (?,?) AND path COLLATE NOCASE NOT IN (?,?)',
                                (str(destination),target_relative,str(p),str(p.relative_to(Path(download_base(store)))))).fetchone()
            owned=db.execute("SELECT 1 FROM discover_owned WHERE key!=? AND state='complete' AND path IN (?,?)",
                             (r['key'],str(destination),target_relative)).fetchone()
        if reserved or owned:raise ValueError('The destination is reserved by another saved item.')
        # A crash between the filesystem move and the record update is healed
        # by library_file(): the journal destination is verified then adopted.
        records.patch_body(store, r['key'], renameJournal=dict(old=str(p), new=str(destination)))
        with directory(store, p.parent) as fd:
            for old, (new, state) in changes.items():
                verify(fd, old, state)
                if new != old:
                    absent(fd, new)
            verify(fd, p.name, before)
            if r['kind'] == 'movie' and destination.parent != p.parent:
                folder_before = p.parent.stat(follow_symlinks=False)
                with directory(store, p.parent.parent) as parent_fd:
                    absent(parent_fd, destination.parent.name)
                    same(folder_before, os.stat(p.parent.name, dir_fd=parent_fd, follow_symlinks=False))
                    verify(fd, p.name, before)
                    rename_noreplace(parent_fd,p.parent.name,destination.parent.name)
                    moved = []
                    try:
                        for old, (new, state) in changes.items():
                            if old != new:
                                move(fd, old, new, state); moved.append((old, new, state))
                    except BaseException:
                        for old, new, state in reversed(moved):
                            move(fd, new, old, state)
                        absent(parent_fd, p.parent.name)
                        same(folder_before, os.stat(destination.parent.name, dir_fd=parent_fd, follow_symlinks=False))
                        rename_noreplace(parent_fd,destination.parent.name,p.parent.name)
                        raise
            else:
                moved = []
                try:
                    for old, (new, state) in changes.items():
                        if old != new:
                            move(fd, old, new, state); moved.append((old, new, state))
                except BaseException:
                    for old, new, state in reversed(moved):
                        move(fd, new, old, state)
                    raise
        from ..view_media import local
        scan_root = Path(app.config['MEDIA_DIR']).resolve()
        if not p.is_relative_to(scan_root):scan_root = Path(download_base(store)).resolve()
        known = local.cached_snapshot(scan_root)
        from ..discover_store import mark_owned, owned
        have = owned(store, r['key']) or {}
        mark_owned(store, r['key'], have.get('request_id', ''), str(destination))
        with store.connect() as db:
            for old, (new, _) in changes.items():
                source_name=str(p.parent/old);target_name=str(destination.parent/new)
                if source_name!=target_name:
                    db.execute('DELETE FROM m3u_sidecars WHERE path=?',(target_name,))
                    db.execute('INSERT INTO m3u_sidecars(path,digest) SELECT ?,digest FROM m3u_sidecars WHERE path=?',(target_name,source_name))
                    db.execute('DELETE FROM m3u_sidecars WHERE path=?',(source_name,))
            # Catalogue YouTube fallback files also have an inventory reservation.
            db.execute('UPDATE media_files SET path=? WHERE path IN (?,?)',
                       (str(destination.relative_to(Path(download_base(store)))), str(p), str(p.relative_to(Path(download_base(store))))))
        records.patch_body(store, r['key'], displayTitle=title, fileQuality=None, renameJournal=None, localPath=None)
        records.history(store, r['key'], f'Renamed {p} -> {destination}; catalogue identity retained.')
        drop_saved_local(store, [p.parent / old for old in changes if Path(old).suffix.lower() in VIDEO], app.config['MEDIA_DIR'])
        local_changes = local.retarget(scan_root,
            {p.parent / old: destination.parent / new for old, (new, _) in changes.items()}, previous=known)
        invalidate()
        return local_changes


def delete(app, store, r, remove_folder=False):
    with store.exclusive_operation():
        idle(store, r)
        p = saved(app, store, r)
        members = videos(store, p) if r['kind'] == 'movie' else [p]
        states = {child.name: snapshot(store, child) for child in members}
        with directory(store, p.parent) as fd:
            for name, before in states.items():
                verify(fd, name, before)
            with store.connect() as db:
                db.execute('UPDATE managed_media SET hold=1 WHERE key=?', (r['key'],))
            for name, before in states.items():
                unlink_verified(fd, name, before)
        with store.connect() as db:
            db.execute("UPDATE discover_owned SET state='deleted' WHERE key=?", (r['key'],))
            db.execute("UPDATE discover_requests SET state='cancelled',message='File deleted; automatic acquisition paused.' WHERE episode_key=? AND state='complete'", (r['key'],))
            if r['kind'] == 'video':
                vid = json.loads(r['body'])['item'].get('videoId','')
                db.execute("UPDATE downloads SET state='cancelled',message='File deleted; automatic acquisition paused.' WHERE id=?", (vid,))
                db.execute('UPDATE media_files SET bytes=0 WHERE id=?', (vid,))
        removed = False
        if remove_folder and r['kind'] == 'movie':
            try:
                movie_folder(store, p)
                folder_before = p.parent.stat(follow_symlinks=False)
                with directory(store, p.parent) as fd:
                    entries = [(name, os.stat(name, dir_fd=fd, follow_symlinks=False)) for name in os.listdir(fd)]
                    # Only known metadata/artwork/sidecar leaves are disposable; any
                    # directory, symlink, video or unrecognised file keeps the folder.
                    if all(stat.S_ISREG(s.st_mode) and Path(name).suffix.lower() in REMOVABLE for name, s in entries):
                        for name, before in entries:
                            checked(store, p.parent/name)
                            unlink_verified(fd, name, before)
                        with directory(store, p.parent.parent) as parent_fd:
                            same(folder_before, os.stat(p.parent.name, dir_fd=parent_fd, follow_symlinks=False))
                            os.rmdir(p.parent.name, dir_fd=parent_fd)
                            removed = True
            except (OSError, ValueError):
                pass  # The requested video deletion is still successful.
        records.patch_body(store, r['key'], fileQuality=None, localPath=None)
        records.history(store, r['key'], f'Deleted {", ".join(str(c) for c in members)} -> deleted; acquisition paused; movie folder '+('removed.' if removed else 'kept.'))
        drop_saved_local(store, members, app.config['MEDIA_DIR'])
        invalidate()


def probe_media(fd, name, before):
    """ffprobe a descriptor-pinned video; returns (quality label, format tokens)."""
    video_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        if before is not None:
            same(before, os.fstat(video_fd))
        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                                'stream=codec_type,width,height:format=format_name', '-of', 'json',
                                '/proc/self/fd/'+str(video_fd)],
                               capture_output=True, text=True, timeout=30, check=True, pass_fds=(video_fd,))
        data = json.loads(probe.stdout)
        stream = next(v for v in data.get('streams', []) if v.get('codec_type') == 'video')
        width = max(float(stream['width']), float(stream['height'])*16/9)
        result = '2160' if width >= 3000 else '1080' if width >= 1600 else '720' if width >= 1000 else '480'
        if not (math.isfinite(width) and width > 0):
            result = None
        formats = set(str(data.get('format', {}).get('format_name') or '').split(',')) - {''}
        return result, formats
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, StopIteration):
        return None, set()
    finally:
        os.close(video_fd)


def quality(store, r, p, probe=True):
    if not p:
        return 'Not verified'
    try:
        before = snapshot(store, p)
        signature = [str(p), before.st_size, before.st_mtime_ns]
        current = records.row(store, r['key']) or r
        cached = json.loads(current['body']).get('fileQuality') or {}
        if cached.get('signature') == signature:
            return cached.get('quality', 'Not verified')
        if not probe:
            # Listing refreshes every few seconds; probing belongs to explicit actions.
            return 'Not verified'
        result = 'Not verified'
        with directory(store, p.parent) as fd:
            verify(fd, p.name, before)
            measured, _formats = probe_media(fd, p.name, before)
            if measured:
                result = measured
            verify(fd, p.name, before)
        after = snapshot(store, p)
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            return 'Not verified'
        records.patch_body(store, r['key'], fileQuality=dict(signature=signature, quality=result))
        return result
    except (OSError, ValueError):
        return 'Not verified'


@contextmanager
def workspace(store, parent):
    """A flat, private metadata workspace; explicit regular-file cleanup only."""
    name = '.reeldock-management-'+uuid.uuid4().hex
    with directory(store, parent) as fd:
        os.mkdir(name, mode=0o700, dir_fd=fd)
        path = checked(store, parent/name)
        before = path.stat(follow_symlinks=False)
        try:
            yield path
        finally:
            with directory(store, path) as scratch:
                for leaf in os.listdir(scratch):
                    state = os.stat(leaf, dir_fd=scratch, follow_symlinks=False)
                    if stat.S_ISREG(state.st_mode):
                        verify(scratch, leaf, state)
                        os.unlink(leaf, dir_fd=scratch)
            same(before, os.stat(name, dir_fd=fd, follow_symlinks=False))
            try:
                os.rmdir(name, dir_fd=fd)
            except OSError:
                pass
            with store.connect() as db:
                db.execute('DELETE FROM m3u_sidecars WHERE substr(path,1,?)=?', (len(str(path)+'/'), str(path)+'/'))


def atomic_sidecar(store, path, raw):
    path = checked(store, path)
    before = snapshot(store, path) if path.exists() else None
    with directory(store, path.parent) as fd:
        leaf = '.reeldock-sidecar-'+uuid.uuid4().hex
        out = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        temporary = os.fstat(out)
        try:
            with os.fdopen(out, 'wb') as stream:
                os.fchmod(stream.fileno(),before.st_mode & 0o777 if before else 0o644)
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            if before is None:
                absent(fd, path.name)
            else:
                verify(fd, path.name, before)
            verify(fd, leaf, temporary)
            os.replace(leaf, path.name, src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            try:
                verify(fd, leaf, temporary); os.unlink(leaf, dir_fd=fd)
            except FileNotFoundError:
                pass
    with store.connect() as db:
        db.execute('INSERT OR REPLACE INTO m3u_sidecars(path,digest) VALUES(?,?)', (str(path), hashlib.sha256(raw).hexdigest()))


def identity_info(store, r):
    from .service import request_for
    from ..discover_acquire import _catalogue_identity
    b = json.loads(r['body']); item = b['item']; episode = b.get('episode') or {}
    q = request_for(store, r['key']) or dict(kind=r['kind'], body='{}')
    provider, identity = _catalogue_identity(q)
    if not identity:
        provider, identity = _catalogue_identity(dict(kind=r['kind'], body=json.dumps(dict(externalId=item.get('externalId', '')))))
    if not identity and re.fullmatch(r'tmdb:(?:movie|show|tv):\d+', str(item.get('id') or '')):
        provider, identity = 'tmdb', item['id'].split(':')[-1]
    if not identity and re.fullmatch(r'[1-9]\d{0,11}',str(item.get('tmdbId') or item.get('tmdb_id') or '')):
        provider,identity='tmdb',str(item.get('tmdbId') or item['tmdb_id'])
    if not identity and re.fullmatch(r'tt\d{7,12}', str(item.get('imdbId') or '')):
        provider, identity = 'imdb', item['imdbId']
    if not identity:
        raise ValueError('No stored catalogue identity is available. The filename cannot identify this title.')
    return dict(provider=provider, identity=identity, name=item['title'], year=item.get('year'),
                season=episode.get('season'), episode=episode.get('number'))


@vpn_metadata
def repair_at(store, r, p):
    from ..downloaders.torrent.metadata import fetch,TMDB
    from ..m3u_sidecars import initialise
    info = identity_info(store, r)
    before = snapshot(store, p)
    initialise(store)
    def present(path):
        try:return snapshot(store,path).st_size>0
        except FileNotFoundError:return False
    poster=present(p.parent/'poster.jpg');fanart=present(p.parent/'fanart.jpg')
    thumb=present(p.with_name(p.stem+'-thumb.jpg'))
    client=TMDB(store)
    class MissingArtworkOnly:
        def get(self,*args,**kwargs):
            data=dict(client.get(*args,**kwargs) or {})
            if poster:data['poster_path']=None
            if fanart:data['backdrop_path']=None
            if thumb and 'episodes' in data:
                data['episodes']=[dict(e,still_path=None) if e.get('episode_number')==info.get('episode') else e for e in data['episodes']]
            return data
    with workspace(store, p.parent) as scratch:
        # The established writer needs an existing leaf, not its video contents.
        dummy = scratch/p.name
        dummy.write_bytes(b'metadata target')
        fetch(store, scratch, p.name, 'movie' if r['kind'] == 'movie' else 'series', info,client=MissingArtworkOnly())
        generated = scratch/p.with_suffix('.nfo').name
        if not generated.is_file():
            raise ValueError('Metadata details were not available for this saved file.')
        snapshot(store, generated)
        with directory(store, p.parent) as fd:
            verify(fd, p.name, before)
        for source in scratch.iterdir():
            if source == dummy:
                continue
            snapshot(store, source)
            target = checked(store, p.parent/source.name)
            raw = source.read_bytes()
            if source.suffix.lower()=='.png':
                # The required Emby poster/fanart paths are JPEG, even if TMDB
                # supplied a PNG. Use the already-installed image decoder.
                import io
                from PIL import Image
                try:
                    with Image.open(source) as image:
                        output=io.BytesIO(); image.convert('RGB').save(output,format='JPEG',quality=90)
                except (OSError,ValueError):continue
                raw=output.getvalue();target=checked(store,target.with_suffix('.jpg'))
            # Rewrite NFOs, but retain existing nonempty artwork.
            if source.suffix.lower() in ('.jpg', '.png') and target.exists() and snapshot(store, target).st_size:
                continue
            atomic_sidecar(store, target, raw)
    return '' if metadata_ready(store,p) and (r['kind']!='episode' or present(p.with_name(p.stem+'-thumb.jpg'))) else ARTWORK_UNAVAILABLE


def metadata_ready(store, p):
    try:
        return all(snapshot(store, path).st_size > 0 for path in (p.with_suffix('.nfo'), p.parent/'poster.jpg', p.parent/'fanart.jpg'))
    except (OSError, ValueError):
        return False


def repair(app, store, r):
    if r['kind'] == 'video':
        raise ValueError('Use the existing YouTube metadata controls for this video.')
    with store.exclusive_operation():
        idle(store, r)
        p = saved(app, store, r)
        message = repair_at(store, r, p)
        records.history(store, r['key'], f'Repaired metadata {p} -> {p.with_suffix(".nfo")}. '+message)
        invalidate()
        return message


def replacement_allowed(store, request):
    from ..download_control import paused
    current = records.row(store, request['episode_key'])
    with store.connect() as db:
        q = db.execute('SELECT state,stop_generation,engine FROM discover_requests WHERE id=?', (request['id'],)).fetchone()
    return bool(current and not current['hold'] and not current['removed'] and q
                and q['state'] not in ('cancelled', 'cancelling', 'failed')
                and q['stop_generation'] == request.get('stop_generation', 0) and not paused(store, q['engine']))


def cleanup_replacement(store, source, token):
    """Only the unique publication directory is disposable; never recurse."""
    folder=source.parent
    while token in str(folder.name) or (folder.name.startswith('Season ') and token in folder.parent.name):
        try:
            before=folder.stat(follow_symlinks=False)
            with directory(store,folder) as fd:
                entries=[(leaf,os.stat(leaf,dir_fd=fd,follow_symlinks=False)) for leaf in os.listdir(fd)]
                if any(not stat.S_ISREG(s.st_mode) for _,s in entries):return
                for leaf,state in entries:
                    checked(store,folder/leaf);verify(fd,leaf,state)
                    os.unlink(leaf,dir_fd=fd)
                    with store.connect() as db:db.execute('DELETE FROM m3u_sidecars WHERE path=?',(str(folder/leaf),))
            with directory(store,folder.parent) as parent_fd:
                same(before,os.stat(folder.name,dir_fd=parent_fd,follow_symlinks=False))
                os.rmdir(folder.name,dir_fd=parent_fd)
            folder=folder.parent
        except (OSError,ValueError):return


def finish_replacement(app, store, request, published):
    """Called by the owning acquisition reconciler, before it records ownership.

    The engine publishes under a unique name. A verified copy is staged beside
    the original so the only destructive video operation is one os.replace.
    A request JSON journal makes a restart just after that syscall recoverable.
    """
    import shutil
    from ..discover_store import mark_owned
    from ..movie_options import verify_file, minimum_for_request
    from ..downloaders.torrent.rename import library_path
    from ..discover_acquire import library_name,_catalogue_identity
    with store.exclusive_operation():
        with store.connect() as db:
            latest = db.execute('SELECT * FROM discover_requests WHERE id=?', (request['id'],)).fetchone()
        if not latest:
            return None
        request = dict(latest)
        payload = json.loads(request['body'])
        if payload.get('replaces') != request['episode_key']:
            raise ValueError('Replacement identity changed. The saved file was kept.')
        if not replacement_allowed(store, request):
            return None
        r = records.row(store, request['episode_key'])
        marker = payload['replacementTarget']
        target = checked(store, marker['path'])
        old = snapshot(store, target)
        signature = dict(device=old.st_dev, inode=old.st_ino, size=old.st_size, mtime=old.st_mtime_ns)
        committed = payload.get('replacementSwap') == signature
        # A crash between staging and the swap leaves a hidden leaf; clear it
        # before staging another verified copy beside the saved file.
        with directory(store, target.parent) as sweep_fd:
            for leaf_name in os.listdir(sweep_fd):
                if leaf_name.startswith('.reeldock-replacement-'):
                    leaf_state = os.stat(leaf_name, dir_fd=sweep_fd, follow_symlinks=False)
                    if stat.S_ISREG(leaf_state.st_mode):
                        os.unlink(leaf_name, dir_fd=sweep_fd)
        if not committed and any(signature[k] != marker[k] for k in signature):
            raise ValueError('Saved file changed; replacement stopped. The saved file was kept.')
        sources = [checked(store, p) for p in published if Path(p).suffix.lower() in VIDEO]
        if not committed and len(sources) != 1:
            raise ValueError('A replacement must contain one video. The saved file was kept.')
        source = sources[0] if sources else None
        if source:
            profile=store.get('metadata_type','emby') or 'emby'
            expected = library_path('', 'video'+source.suffix, 'video', 'series' if r['kind'] == 'episode' else 'movie',
                                    profile, names(store), library_name(request, ''),
                                    flat_episodes=_catalogue_identity(request)[0]=='tvdb' and profile=='emby')
            relative=source.relative_to(Path(download_base(store)))
            parts=list(Path(expected).parts) if expected else []
            actual=list(relative.parts)
            # A source file may carry an existing IMDb tag into the unique show
            # directory; that tag never supplies the catalogue identity.
            if len(parts)>1 and len(actual)==len(parts) and actual[1].startswith(parts[1]+' '):
                suffix=actual[1][len(parts[1]):]
                if re.fullmatch(r' (?:\[(?:imdb|tmdb|tvdb)id[=-][A-Za-z0-9]+\]|\{(?:imdb|tmdb|tvdb)-[A-Za-z0-9]+\})',suffix):
                    actual[1]=parts[1]
            if not expected or actual!=parts or 'sample' in str(relative).lower():
                raise ValueError('Replacement publication path changed. The saved file was kept.')
        if not committed:
            before = snapshot(store, source)
            import xml.etree.ElementTree as ET
            try:
                nfo=checked(store,source.with_suffix('.nfo'))
                snapshot(store,nfo)
                valid_nfo=ET.fromstring(nfo.read_bytes()).tag in ('movie','episodedetails')
            except (OSError,ValueError,ET.ParseError):valid_nfo=False
            # Artwork can remain partial, but release-text NFOs cannot qualify.
            if not valid_nfo:repair_at(store, r, source)
            nfo = checked(store, source.with_suffix('.nfo'))
            if not snapshot(store, nfo).st_size:
                raise ValueError('Replacement metadata is unavailable. The saved file was kept.')
            minimum = minimum_for_request(store, request)
            if r['kind'] == 'episode':
                from ..show_options import minimum_for_request as episode_minimum
                minimum = episode_minimum(store, request)
            selected = dict(kind=r['kind'], minimum=minimum or 0, quality=request['quality'], fallback=bool(request['fallback']))
            with directory(store, source.parent) as source_fd, directory(store, target.parent) as target_fd:
                verify(source_fd, source.name, before)
                verify(target_fd, target.name, old)
                saved_quality, _saved_formats = probe_media(target_fd, target.name, old)
                leaf = '.reeldock-replacement-'+uuid.uuid4().hex+source.suffix
                outgoing = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=target_fd)
                staged = os.fstat(outgoing)
                try:
                    with os.fdopen(outgoing, 'wb') as dst:
                        os.fchmod(dst.fileno(),old.st_mode & 0o777)
                        incoming = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_fd)
                        with os.fdopen(incoming, 'rb') as src:
                            same(before, os.fstat(incoming))
                            shutil.copyfileobj(src, dst, 1024*1024)
                        dst.flush(); os.fsync(dst.fileno())
                    temporary = checked(store, target.parent/leaf)
                    verify_file(temporary, selected)
                    staged_quality, staged_formats = probe_media(target_fd, leaf, staged)
                    if saved_quality and staged_quality and QUALITY_RANK.get(staged_quality, -1) <= QUALITY_RANK.get(saved_quality, -1):
                        raise ValueError('The staged file is not a higher quality than the saved file. The saved file was kept.')
                    allowed = CONTAINERS.get(target.suffix.lower())
                    if staged_formats and allowed and not staged_formats & allowed:
                        raise ValueError('The staged file uses a different container than the saved file. The saved file was kept.')
                    verify(source_fd, source.name, before)
                    if (source.stat().st_size, source.stat().st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                        raise ValueError('Replacement file changed. The saved file was kept.')
                    verify(target_fd, target.name, old)
                    if not replacement_allowed(store, request):
                        return None
                    verify(target_fd, leaf, staged)
                    final_stat = os.stat(leaf, dir_fd=target_fd, follow_symlinks=False)
                    payload['replacementSwap'] = dict(device=final_stat.st_dev, inode=final_stat.st_ino,
                                                       size=final_stat.st_size, mtime=final_stat.st_mtime_ns)
                    verify(target_fd,target.name,old)
                    current=os.stat(target.name,dir_fd=target_fd,follow_symlinks=False)
                    if (current.st_size,current.st_mtime_ns)!=(old.st_size,old.st_mtime_ns):
                        raise ValueError('Saved file changed; replacement stopped. The saved file was kept.')
                    # BEGIN IMMEDIATE makes the final cancellation check and the
                    # swap one critical section: a cancel UPDATE waits on the
                    # write lock until commit instead of slipping between them.
                    with store.connect() as db:
                        db.execute('BEGIN IMMEDIATE')
                        q = db.execute('SELECT state,stop_generation FROM discover_requests WHERE id=?', (request['id'],)).fetchone()
                        if not q or q['state'] in ('cancelled','cancelling','failed') or q['stop_generation'] != request.get('stop_generation',0):
                            db.rollback()
                            return None
                        db.execute('UPDATE discover_requests SET body=? WHERE id=?', (json.dumps(payload), request['id']))
                        verify(target_fd,leaf,staged)
                        # The path itself stays exactly the one library_file resolves.
                        os.replace(leaf, target.name, src_dir_fd=target_fd, dst_dir_fd=target_fd)
                        try:os.fsync(target_fd)
                        except OSError:pass  # Swap already happened; reconcile its journal.
                finally:
                    try:
                        verify(target_fd, leaf, staged); os.unlink(leaf, dir_fd=target_fd)
                    except FileNotFoundError:
                        pass
        # A post-swap metadata failure cannot turn a successful swap into a failed
        # download or point ownership at the staging name. It remains repairable.
        message = ''
        if source and source.exists():
            try:
                for candidate in source.parent.iterdir():
                    if candidate.suffix.lower() not in ('.nfo', '.jpg', '.png'):
                        continue
                    snapshot(store, candidate)
                    name = candidate.name
                    if name.startswith(source.stem):
                        name = target.stem + name[len(source.stem):]
                    destination = checked(store, target.parent/name)
                    if candidate.suffix.lower() in ('.jpg', '.png') and destination.exists():
                        snapshot(store, destination)
                        continue
                    atomic_sidecar(store, destination, candidate.read_bytes())
            except (OSError, ValueError):
                message = 'Saved · metadata incomplete. Repair metadata to retry.'
        if not metadata_ready(store, target):
            message = message or ARTWORK_UNAVAILABLE
        mark_owned(store, r['key'], request['id'], str(target), 'complete')
        records.patch_body(store, r['key'], fileQuality=None)
        with store.connect() as db:
            db.execute('UPDATE managed_media SET message=? WHERE key=?', (message, r['key']))
            if not payload.get('replacementRecorded'):
                db.execute('INSERT INTO managed_history(media_key,message,created) VALUES(?,?,?)',
                           (r['key'], f'Replaced {marker["path"]} -> {target} from release {payload["managedReleaseId"]} (request {request["id"]}). '+message, time.time()))
                payload['replacementRecorded'] = True
                db.execute('UPDATE discover_requests SET body=? WHERE id=?', (json.dumps(payload), request['id']))
        invalidate()
        if source and payload.get('replacementName'):
            cleanup_replacement(store,source,payload['replacementName'])
        return str(target)
