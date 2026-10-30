"""Discover YouTube downloads use the existing queue with their own quality rule.

Other channel downloads keep height<=quality. A Discover request for 1080p with
lower quality turned off asks yt-dlp for height=1080 and does not widen that
selector when the exact format is missing.
"""
import json
import os
import re
import sys
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from . import media_library as library
from .vpn.transport import youtube, metadata
from .discover_logic import QUALITIES, redact
from .discover_store import quality_policy, readable_name
from .media_library import contained, safe_name

PLACEHOLDER_CHANNEL = 'UCdiscoverreeldock000000'


def adjust_command(args, quality, allow_lower):
    args = list(args)
    operator = '<=' if allow_lower else '='
    selector = f'bv[height{operator}{quality}]+ba/b[height{operator}{quality}]'
    args[args.index('-f') + 1] = selector
    merge = args.index('--merge-output-format')
    args[merge + 1] = 'mkv'
    if '--no-playlist' not in args:
        args.insert(args.index('--'), '--no-playlist')
    return args


def is_discover(store, video_id):
    return quality_policy(store, video_id) is not None


def register_video(store, item, allow_lower=None):
    """Insert a published catalogue row without turning the channel into a subscription."""
    video_id = str(item.get('videoId') or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise ValueError('Invalid YouTube video ID.')
    if item.get('quality') not in QUALITIES and item.get('quality') is not None:
        raise ValueError('Choose 720p, 1080p or 2160p.')
    channel_id = item.get('channelId') if re.fullmatch(r'UC[A-Za-z0-9_-]{22}', str(item.get('channelId') or '')) else PLACEHOLDER_CHANNEL
    channel_name = str(item.get('channel') or 'YouTube')[:180]
    published = str(item.get('published') or '')[:10]
    approximate = bool(item.get('publishedApproximate')) or not bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', published))
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', published):
        published = date.today().isoformat()
    video = {
        'id': video_id, 'title': str(item.get('title') or video_id)[:300], 'channel': channel_id,
        'state': 'published', 'date': published, 'kind': 'videos', 'discover': True,
        'publishedApproximate': approximate,
        'description': str(item.get('description') or '')[:800],
        'thumbnail': item.get('image') if str(item.get('image') or '').startswith('https://i.ytimg.com/') else '',
    }
    channel = {'id': channel_id, 'name': channel_name, 'avatar': '', 'description': 'Public Discover channel. This is not a Google subscription.'}
    from . import media_library
    from .channel_downloads import initialise as initialise_rules
    media_library.initialise(store)
    initialise_rules(store)
    with store.connect() as db:
        existing_channel = db.execute('SELECT data,active FROM channels WHERE id=?', (channel_id,)).fetchone()
        if not existing_channel:
            db.execute('INSERT INTO channels(id,data,active) VALUES (?,?,0)', (channel_id, json.dumps(channel)))
            db.execute('''INSERT OR IGNORE INTO download_rules(channel,quality,videos,shorts,live,range_days,enabled_at,quality_override)
                VALUES (?,?,0,0,0,0,?,0)''', (channel_id, '1080', time.time()))
        existing_video = db.execute('SELECT data FROM videos WHERE id=?', (video_id,)).fetchone()
        if existing_video:
            current = json.loads(existing_video['data'])
            if not current.get('discover'):
                video = current
        else:
            db.execute('INSERT INTO videos(id,channel,day,published,data,checked) VALUES (?,?,?,?,?,?)',
                       (video_id, channel_id, published, published, json.dumps(video), time.time()))
    video['id'] = video_id
    if video.get('state') != 'published':
        video['state'] = 'published'
    return video


@metadata
def enrich_metadata(store, video_id, info):
    """Keep full public metadata and subscription-compatible cached artwork references."""
    from .auto_downloads import _channel_images
    from .catalogue import thumbnail
    channel_id = str(info.get('channel_id') or '')
    uploaded = str(info.get('upload_date') or '')
    if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', channel_id) or not re.fullmatch(r'\d{8}', uploaded):
        raise ValueError('YouTube did not provide the channel and upload date needed for library metadata.')
    if info.get('id') != video_id:
        raise ValueError('YouTube metadata did not match the requested video.')
    day = date(int(uploaded[:4]), int(uploaded[4:6]), int(uploaded[6:8])).isoformat()
    images = _channel_images(store, channel_id)
    def cached(url):
        return thumbnail(store, {'thumbnails': {'medium': {'url': url}}}) if url else ''
    picture = cached(info.get('thumbnail'))
    avatar, banner = cached(images.get('avatar')), cached(images.get('banner'))
    with store.connect() as db:
        row = db.execute('SELECT data FROM videos WHERE id=?', (video_id,)).fetchone()
        if not row:
            raise ValueError('Saved video details are unavailable.')
        video = json.loads(row[0])
        video.update(title=info.get('title') or video['title'], channel=channel_id, date=day, publishedApproximate=False,
                     description=info.get('description') or video.get('description', ''),
                     thumbnail=picture or video.get('thumbnail', ''))
        seconds = max(0, int(info.get('duration') or 0))
        if seconds:
            hours, minutes = seconds // 3600, seconds // 60 % 60
            video['duration'] = f'{hours}:{minutes:02d}:{seconds % 60:02d}' if hours else f'{minutes}:{seconds % 60:02d}'
            video['duration_seconds'] = seconds
        if info.get('timestamp') or info.get('release_timestamp'):
            from .youtube_fallback import stamp
            published = stamp(info)
            if published:
                video['time'] = datetime.fromisoformat(published).astimezone(ZoneInfo('Europe/London')).strftime('%H:%M')
        if info.get('view_count') is not None:
            video['views'] = info['view_count']
        row = db.execute('SELECT data FROM channels WHERE id=?', (channel_id,)).fetchone()
        channel = json.loads(row[0]) if row else {'id': channel_id}
        channel['name'] = info.get('channel') or info.get('uploader') or channel.get('name', 'YouTube')
        for key, value in [('avatar', avatar), ('banner', banner), ('description', images.get('description'))]:
            if value:
                channel[key] = value
        db.execute('INSERT INTO channels(id,data,active) VALUES (?,?,0) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                   (channel_id, json.dumps(channel)))
        db.execute('UPDATE videos SET channel=?,day=?,published=?,data=?,checked=? WHERE id=?',
                   (channel_id, day, day, json.dumps(video), time.time(), video_id))
    return video


def claim_path(store, media, video_id, title, suffix='.mkv'):
    """Save manual videos directly in their channel folder with title-only filenames."""
    from .storage_layout import fixed, FIXED_NAMES
    record = library.reserve(store, media, video_id)
    from .discover_publication import request_for_video, claim
    request=request_for_video(store,video_id)
    if request:return claim(store,media,video_id,request,suffix)
    with store.connect() as db:
        saved = db.execute('SELECT folder FROM media_channels WHERE id=?', (record['channel'],)).fetchone()
        if not saved:
            raise ValueError('Saved channel folder is unavailable.')
        channel = Path(saved[0]).name
        stem = safe_name(title)
        from .auto_downloads import _restricted
        stem = _restricted(store, stem)
        base = FIXED_NAMES['youtube_downloads'] if fixed(store) else 'Discover'
        for number in range(1, 100):
            name = stem + ('' if number == 1 else f' ({number})') + '.mkv'
            relative = f'{base}/{channel}/{name}'
            taken = db.execute('SELECT id FROM media_files WHERE path=? COLLATE NOCASE AND id!=?', (relative, video_id)).fetchone()
            path = contained(media, relative)
            if taken or path.exists():
                continue
            db.execute('UPDATE media_files SET path=? WHERE id=?', (relative, video_id))
            return relative
    raise ValueError('Could not choose a free filename for this video.')


@youtube
def run(store, media, job, stop):
    from .downloads import command, run_process
    from .channel_downloads import preferences
    allow_lower = quality_policy(store, job['id'])
    if allow_lower is None:
        raise ValueError('This is not a Discover download.')
    root = Path(media).resolve()
    root.mkdir(parents=True, exist_ok=True)
    import shutil
    if shutil.disk_usage(root).free < 1024 ** 3:
        raise ValueError('Less than 1 GB free in the media folder. Free space, then retry.')
    with store.connect() as db:
        saved = db.execute('SELECT data FROM videos WHERE id=?', (job['id'],)).fetchone()
    video = json.loads(saved['data']) if saved else {'id': job['id'], 'title': job['title'], 'discover': True, 'date': date.today().isoformat(), 'kind': 'videos', 'channel': PLACEHOLDER_CHANNEL}
    from .downloads import check_rules
    check_rules(store, job, False, video, preferences(store, video.get('channel', '')), None, 'start')
    import tempfile
    with tempfile.TemporaryDirectory(prefix='.reeldock-discover-', dir=root) as directory:
        folder = Path(directory)
        cookie = store.directory / 'youtube-cookies.txt'
        from .cookies import allow_downloads
        snapshot = folder / 'cookies.txt' if allow_downloads(store) and cookie.is_file() else None
        if snapshot:
            snapshot.write_bytes(cookie.read_bytes())
            os.chmod(snapshot, 0o600)
        args = adjust_command(command(job['id'], job['quality'], folder, snapshot), job['quality'], allow_lower)
        args.insert(args.index('--'), '--write-info-json')
        code, output = run_process(store, job, stop, args, folder / 'progress.log', root)
        produced = next((path for path in folder.glob('video.*') if path.suffix in ('.mkv', '.mp4', '.webm') and path.stat().st_size), None)
        if code or not produced:
            detail = redact(output)
            quality_missing = 'requested format is not available' in detail.lower()
            if not allow_lower and quality_missing:
                raise ValueError('The requested quality is not available, and a lower quality was not allowed.')
            raise ValueError(detail or 'No completed media file was produced.')
        info = json.loads((folder / 'video.info.json').read_text())
        from .discover_publication import request_for_video, publish
        request=request_for_video(store,job['id'])
        if request:
            from .discovery_engine import youtube_title_matches
            body=json.loads(request['body'])
            if not youtube_title_matches(info.get('title',''),body['item'],body.get('episode'),body.get('edition'),info.get('description','')):
                raise ValueError('YouTube metadata does not match the requested Discover title and edition. Nothing was published.')
        video = enrich_metadata(store, job['id'], info)
        if stop.is_set():
            raise InterruptedError('Download cancelled.')
        from .downloads import recheck_before_save
        recheck_before_save(store, job, False, info.get('duration'))
        record = library.reserve(store, media, job['id'])
        relative = claim_path(store, media, job['id'], video.get('title') or job['title'],produced.suffix)
        record['path'] = relative
        if request:return publish(store,media,record,request,produced)
        target = contained(root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(produced, target)
        notice = library.write_metadata(store, media, record)
    return notice
