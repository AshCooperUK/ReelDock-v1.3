"""Small, read-only overview without walking the media library."""
import platform
import shutil
import time
from yt_dlp.version import __version__ as yt_dlp_version
from pathlib import Path


# One threshold for every "worker online/offline" signal (Activity chip,
# offline alert, download status and YouTube settings). The worker refreshes
# its heartbeat every cycle (5 s) and during long extractions.
WORKER_OFFLINE_SECONDS = 120


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


def snapshot(store,media,version,started):
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
        container_memory_bytes=number('/sys/fs/cgroup/memory.current'),container_memory_limit=number('/sys/fs/cgroup/memory.max'),
        last_sync=store.get('last_sync'),youtube_sync_paused=store.get('youtube_sync_paused')=='true',
        downloads_paused=store.get('download_pause_downloads')=='true')
