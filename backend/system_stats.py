"""Small, read-only overview without walking the media library."""
import platform
import json
import os
import re
import shutil
import time
from yt_dlp.version import __version__ as yt_dlp_version
from pathlib import Path


# One threshold for every "worker online/offline" signal (Activity chip,
# offline alert, download status and YouTube settings). The worker refreshes
# its heartbeat every cycle (5 s) and during long extractions.
WORKER_OFFLINE_SECONDS = 120
MOUNTINFO = Path('/proc/self/mountinfo')
MAX_MOUNT_BYTES = 1_048_576
MAX_STORAGE_PATHS = 128
MAX_STORAGE_CONFIG_BYTES = 32768


def _mount_points():
    """Read this container's mount table, without walking any library folders."""
    try:
        with MOUNTINFO.open(encoding='utf-8') as source:
            text = source.read(MAX_MOUNT_BYTES + 1)
    except (OSError, UnicodeError):
        return [], True
    if len(text) > MAX_MOUNT_BYTES:
        return [], True
    result = []
    for line in text.splitlines():
        fields = line.partition(' - ')[0].split()
        if ' - ' not in line or len(fields) < 6:
            continue
        value = re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), fields[4])
        if value.startswith('/') and '\0' not in value:
            result.append(Path(value))
    return result, False


def storage_snapshot(store, media, app=None):
    """Capacity for configured paths and nested mounts, once per filesystem."""
    roots = [('Application data', str(store.directory)), ('Media', media)]
    incomplete = False
    if app is not None:
        from .storage_locations import roots as allowed_roots, configuration_warnings
        roots.extend(('Media', value) for value in allowed_roots(app))
        roots.extend([('Media', app.config.get('M3U_DIR', '')),
                      ('Downloads', app.config.get('REELDOCK_DOWNLOAD_BASE', ''))])
        try:
            raw = app.config.get('M3U_DOWNLOAD_ROOTS') or '[]'
            if not isinstance(raw, str) or len(raw.encode()) > MAX_STORAGE_CONFIG_BYTES:
                raise ValueError('Invalid download storage paths.')
            values = json.loads(raw)
            if not isinstance(values, list) or len(values) > MAX_STORAGE_PATHS:
                raise ValueError('Invalid download storage paths.')
            roots.extend(('Downloads', value) for value in values)
        except (ValueError, TypeError, RecursionError):
            incomplete = True
    configured = {}
    for label, value in roots:
        if value == '':
            continue
        if (not isinstance(value, str) or not Path(value).is_absolute()
                or '..' in Path(value).parts or any(ord(character) < 32 for character in value)):
            incomplete = True
            continue
        try:
            path = Path(value).resolve()
        except (OSError, RuntimeError, ValueError):
            incomplete = True
            continue
        configured.setdefault(path, []).append(label)
    mounts, mount_error = _mount_points()
    incomplete |= mount_error
    # Always sample explicit roots before their optional nested mount aliases.
    candidates = {root: list(labels) for root, labels in configured.items()}
    for root, labels in configured.items():
        # A configured root of / reports its capacity, never all container mounts.
        if root == Path('/'):
            continue
        for mount in mounts:
            if mount.is_relative_to(root):
                candidates.setdefault(mount, []).extend(labels)
    if len(candidates) > MAX_STORAGE_PATHS:
        incomplete = True
    rows, devices = [], {}
    for path, labels in list(candidates.items())[:MAX_STORAGE_PATHS]:
        display = str(path)
        if app is not None:
            try:
                from .storage_locations import host_path
                display = host_path(app, path)
            except (ValueError, TypeError, KeyError, AttributeError):
                pass
        try:
            device = os.stat(path).st_dev
            if device in devices:
                row = devices[device]
            else:
                row = dict(id='filesystem:' + str(device), paths=[], roles=[],
                           total=None, used=None, free=None)
                devices[device] = row
                rows.append(row)
            if row['total'] is None:
                try:
                    usage = shutil.disk_usage(path)
                    row.update(total=usage.total, used=usage.used, free=usage.free)
                except OSError:
                    pass
        except OSError:
            row = dict(id='unavailable:' + str(path), paths=[], roles=[], total=None, used=None, free=None)
            rows.append(row)
        for label in labels:
            if label not in row['roles']:
                row['roles'].append(label)
        if display not in row['paths']:
            row['paths'].append(display)
    for row in rows:
        row['label'] = ' · '.join(row.pop('roles'))
    warnings = configuration_warnings(app) if app is not None else []
    incomplete |= bool(warnings)
    return dict(storage_disks=rows, storage_disks_incomplete=incomplete,
                storage_configuration_warnings=warnings)


def worker_online(heartbeat, now=None):
    try:
        heartbeat = float(heartbeat or 0)
    except (TypeError, ValueError):
        return False
    return heartbeat > (time.time() if now is None else now) - WORKER_OFFLINE_SECONDS


def number(path):
    try:
        value=Path(path).read_text().strip()
        return int(value) if value!='max' else None
    except (OSError,ValueError):return None


def snapshot(store,media,version,started,*,app=None):
    def disk(path):
        try:
            value=shutil.disk_usage(path)
            return dict(total=value.total,used=value.used,free=value.free)
        except OSError:return None
    with store.connect() as db:
        channels=db.execute('SELECT count(*) FROM channels WHERE active=1').fetchone()[0]
        videos=db.execute('SELECT count(*) FROM videos').fetchone()[0]
        favourites=db.execute('SELECT count(*) FROM channel_favourites f JOIN channels c ON c.id=f.id WHERE c.active=1').fetchone()[0]
        downloads=dict(db.execute('SELECT state,count(*) FROM downloads GROUP BY state'))
        indexed_bytes=db.execute('SELECT coalesce(sum(bytes),0) FROM media_files').fetchone()[0]
        m3u_counts=dict(db.execute('SELECT f.kind,count(*) FROM m3u_files f JOIN m3u_sources s ON s.id=f.source GROUP BY f.kind'))
        shows=db.execute("""SELECT DISTINCT f.source,
            json_extract(i.data,'$.info.name') AS name,
            json_extract(i.data,'$.info.provider') AS provider,
            json_extract(i.data,'$.info.identity') AS identity
            FROM m3u_files f JOIN m3u_sources s ON s.id=f.source
            LEFT JOIN m3u_file_info i ON i.source=f.source AND i.path=f.path
            WHERE f.kind IN ('series','tv')""").fetchall()
        m3u=dict(playlists=db.execute('SELECT count(*) FROM m3u_sources').fetchone()[0],
            movies=m3u_counts.get('movies',0),episodes=m3u_counts.get('series',0),
            dated_tv=m3u_counts.get('tv',0),unsorted=m3u_counts.get('unsorted',0),
            live_playlists=m3u_counts.get('playlist',0),
            shows=len(shows) if all(row['name'] for row in shows) else None)
    from .quota import status as quota_status
    return dict(google_api=quota_status(store),version=version,yt_dlp_version=yt_dlp_version,worker_version=store.get('worker_version') or 'Not reported',
        worker_online=worker_online(store.get('worker_heartbeat','0')),
        uptime_seconds=max(0,int(time.monotonic()-started)),platform=platform.system(),architecture=platform.machine(),
        channels=channels,videos=videos,favourites=favourites,downloads=downloads,indexed_video_bytes=indexed_bytes,m3u=m3u,
        media_disk=disk(media),data_disk=disk(store.directory),database_bytes=store.path.stat().st_size,
        **storage_snapshot(store,media,app),
        container_memory_bytes=number('/sys/fs/cgroup/memory.current'),container_memory_limit=number('/sys/fs/cgroup/memory.max'),
        last_sync=store.get('last_sync'),youtube_sync_paused=store.get('youtube_sync_paused')=='true',
        downloads_paused=store.get('download_pause_downloads')=='true')
