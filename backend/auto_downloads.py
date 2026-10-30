"""Route accepted files from the watched folder into their download engines."""
import hashlib
import logging
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import m3u
from .download_options import read
from .storage_layout import names, download_base
from .settings.auto_download import accepts
from .vpn.transport import youtube as youtube_traffic, vod, subprocess_env

LIMITS = {'.strm': 16_384, '.torrent': 2_000_000, '.nzb': 20_000_000}
KINDS = {'.strm': 'strm', '.torrent': 'torrent', '.nzb': 'nzb'}
class _HeldSource(ValueError):
    pass


VIDEO_SUFFIXES = {'.mkv', '.mp4', '.avi', '.m4v', '.mov', '.webm', '.ts', '.mpg', '.mpeg'}


def initialise(store):
    with store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS auto_downloads (
            key TEXT PRIMARY KEY, name TEXT, kind TEXT, state TEXT, message TEXT, updated REAL)''')


def enabled(store, engine=None):
    options = read(store, engine) if engine is not None else read(store)
    return bool(options['auto_downloads']) and not options['pause_downloads']


def _scan_enabled(store):
    # An unrelated protected engine must not stop allowed arrivals. Check the
    # global user controls here and the selected engine before claiming a file.
    from .download_options import FIELDS
    return bool(FIELDS['auto_downloads'].read(store)) and not FIELDS['pause_downloads'].read(store)


def _source_engine(path, kind):
    if kind != 'strm':
        return kind
    try:
        url = _stream_url(_read_file(path, LIMITS['.strm']).decode('utf-8-sig'))
        return 'youtube' if youtube_id(url) else 'vod'
    except (OSError, UnicodeError, ValueError):
        # Malformed input still follows the existing claimed-file failure path.
        return 'vod'


def library_dirs(store, app=None):
    """Folders that already receive finished media. The watch folder must stay outside them."""
    base = download_base(store)
    if not store.get('storage_base') and app is not None:
        import json
        folders = json.loads(store.get('m3u_download_folders', '{}'))
        roots = [Path(app.config[key]) for key in ('MEDIA_DIR', 'M3U_DIR') if app.config.get(key)]
        roots.extend(Path(value) for value in folders.values() if value)
        if base:
            roots.extend(Path(base) / name for name in (*names(store).values(), 'Downloads'))
        return roots
    if not base:
        return []
    root = Path(base)
    values = names(store)
    return [root / values['youtube'], root / values['m3u'], root / values['movies'], root / values['series'], root / values['unsorted'], root / 'Downloads']


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except (ValueError, OSError):
        try:
            child.absolute().relative_to(parent.absolute())
        except ValueError:
            return False
    return child != parent


def validate_watch(app, store, value, create=False):
    """A writable drop folder that is not the library itself."""
    from .storage_locations import resolve, host_path
    path, _location = resolve(app, value)
    if not path.exists():
        if not create or not path.parent.is_dir() or path.parent.is_symlink():
            raise ValueError('Choose an existing folder, or a new folder inside one.')
        path.mkdir()
    if path.is_symlink() or not path.is_dir():
        raise ValueError('Choose a folder, not a file or link.')
    if not os.access(path, os.W_OK | os.X_OK):
        raise ValueError('ReelDock cannot write in that folder.')
    base = download_base(store)
    if base and path.resolve() == Path(base).resolve():
        raise ValueError('Choose a folder inside your library location, not the whole ReelDock directory.')
    for root in library_dirs(store, app):
        # Bind-mount aliases have different container paths but the same host path.
        host = Path(host_path(app, path))
        library = Path(host_path(app, root))
        if (path.resolve() == root.resolve() or _inside(path, root) or _inside(root, path)
                or host == library or host.is_relative_to(library) or library.is_relative_to(host)):
            raise ValueError('Choose a folder outside Movies, TV Shows, YouTube, M3U and Downloads.')
    return path


def save_folder(app, store, value):
    from .storage_layout import fixed, LOCKED
    if fixed(store): raise ValueError(LOCKED)
    if value in ('', None):
        store.set('auto_download_folder', '')
        return
    if not isinstance(value, str):
        raise ValueError('Choose an auto-download folder.')
    store.set('auto_download_folder', str(validate_watch(app, store, value, create=True)))


def watch_folder(app, store):
    from .storage_layout import fixed, fixed_folders
    if fixed(store):
        folder = Path(fixed_folders(store)['auto'])
        if not folder.is_dir(): raise ValueError('The Compose auto-download folder is unavailable.')
        return folder
    saved = store.get('auto_download_folder') or ''
    if not saved:
        return None
    try:
        return validate_watch(app, store, saved, create=False)
    except ValueError:
        return None


def _key(name, size, mtime_ns, ctime_ns, device, inode):
    # Linux ctime changes on re-entry even when a move/copy preserves mtime.
    # Device/inode also distinguish a replacement from the previously handled file.
    return hashlib.sha256(f'v2\0{name}\0{size}\0{mtime_ns}\0{ctime_ns}\0{device}\0{inode}'.encode()).hexdigest()


def _claim(store, key, name, kind):
    """One process owns a file arrival; repeated scans do not claim it again."""
    now = time.time()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT state, updated FROM auto_downloads WHERE key=?', (key,)).fetchone()
        if row and row['state'] in {'done','held'}:
            return False
        if row and row['state'] == 'running' and now - row['updated'] < 1800:
            return False
        if row and row['state'] == 'failed' and now - row['updated'] < 900:
            return False
        db.execute('INSERT OR REPLACE INTO auto_downloads VALUES(?,?,?,?,?,?)', (key, name[:180], kind, 'running', 'Starting.', now))
    return True


def _finish(store, key, state, message):
    with store.connect() as db:
        db.execute('UPDATE auto_downloads SET state=?, message=?, updated=? WHERE key=?', (state, message[:200], time.time(), key))


def _video_name(filename):
    stem = filename[:-5] if filename.lower().endswith('.strm') else filename
    if Path(stem).suffix.lower() not in VIDEO_SUFFIXES:
        stem += '.mkv'
    return stem


def destination(store, filename):
    """The movie or episode path this drop should use, before the real extension is known."""
    from types import SimpleNamespace
    from .downloaders.torrent.metadata import matched_save_as, scan_lookup
    from .downloaders.torrent.rename import align_existing_folder, library_path, suggest_download
    from .torrents import _library_layout

    base = download_base(store)
    if not base:
        raise ValueError('Configure a ReelDock download directory first.')
    root = Path(base)
    video = _video_name(filename)
    item = SimpleNamespace(path=video, kind='video', selectable=True, size=1, index=0)
    layout = _library_layout(store)
    suggestion = suggest_download([item], root, layout)
    title = suggestion['name']
    if not suggestion['folder']:
        try:
            found = matched_save_as(scan_lookup(store, video))
        except (OSError, ValueError):
            found = ''
        if found:
            title = found
    relative = library_path(video, video, 'video', profile=layout.get('profile', 'emby'), folders=layout, title=title or None)
    if not relative:
        raise ValueError('ReelDock could not tell whether this is a movie or a TV show. Add a year or an episode number to the file name.')
    relative = align_existing_folder(root, relative)
    return m3u.contained(root, relative)


def _stream_url(text):
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith('#')]
    if len(lines) != 1:
        raise ValueError('This is not a single video stream.')
    url = m3u.validate_url(lines[0])
    if urlsplit(url).path.lower().endswith('.m3u8'):
        raise ValueError('Auto download supports a video file, not a live stream or playlist.')
    return url


def youtube_id(url: str) -> str:
    """Return one YouTube video id, or an empty string for any other address."""
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    ident = ''
    if host == 'youtu.be':
        ident = parsed.path.strip('/').split('/')[0]
    elif host in {'youtube.com', 'm.youtube.com', 'music.youtube.com'}:
        if parsed.path == '/watch':
            ident = (parse_qs(parsed.query).get('v') or [''])[0]
        else:
            parts = [part for part in parsed.path.split('/') if part]
            if parts and parts[0] in {'shorts', 'embed', 'live', 'v'} and len(parts) > 1:
                ident = parts[1]
    return ident if re.fullmatch(r'[A-Za-z0-9_-]{11}', ident or '') else ''


def _read_file(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Choose a regular file.')
        if info.st_size > limit:
            raise ValueError('That file is too large for an auto download.')
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise ValueError('That file is too large for an auto download.')
        return data
    finally:
        os.close(fd)


def _accept(folder: Path, path: Path):
    done = folder / '.reeldock-accepted'
    done.mkdir(exist_ok=True)
    target = done / path.name
    if target.exists():
        target = done / f'{path.stem}-{time.time_ns()}{path.suffix}'
    os.replace(path, target)


def _open_target(store, source_name, suffix, total):
    import shutil
    target = destination(store, source_name).with_suffix(suffix)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError('A file already exists at the destination; it was kept.')
    if shutil.disk_usage(target.parent).free < max(total, 1024 ** 3) + 256 * 1024 ** 2:
        raise ValueError('There is not enough free space for this download.')
    temporary = target.parent / ('.reeldock-auto-' + hashlib.sha256(source_name.encode()).hexdigest()[:16] + '.part')
    return target, temporary


def _publish(temporary, target, total, sent):
    if total and sent != total:
        raise ValueError('The provider ended the download early.')
    if not sent:
        raise ValueError('The provider returned an empty file.')
    os.link(temporary, target)


@vod
def _save_live(store, source_name, url, stop):
    from .m3u_player import upstream, video_suffix
    connection = None
    temporary = None
    try:
        connection, response, resolved = upstream(url)
        if response.status != 200:
            raise ValueError('The provider could not supply this video. Try again later.')
        prefix = response.read(4096)
        mime = response.getheader('Content-Type', '').split(';')[0].lower()
        suffix = video_suffix(resolved, mime, prefix)
        if suffix == '.bin' or prefix.lstrip().startswith(b'#EXTM3U'):
            raise ValueError('This source is not a supported video file.')
        total = int(response.getheader('Content-Length', '0') or '0')
        target, temporary = _open_target(store, source_name, suffix, total)
        sent = 0
        with temporary.open('wb') as handle:
            handle.write(prefix)
            sent += len(prefix)
            while True:
                if stop.is_set():
                    raise ValueError('Auto download stopped.')
                block = response.read(256 * 1024)
                if not block:
                    break
                handle.write(block)
                sent += len(block)
            handle.flush()
            os.fsync(handle.fileno())
        _publish(temporary, target, total, sent)
        temporary.unlink()
        temporary = None
        return target
    finally:
        if connection:
            connection.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@vod
def _save_stream(store, source_name, url, fetch, stop):
    if fetch is None:
        return _save_live(store, source_name, url, stop)
    suffix, total, chunks = fetch(url)
    body = b''.join(chunks)
    target, temporary = _open_target(store, source_name, suffix, total or len(body))
    try:
        if stop.is_set():
            raise ValueError('Auto download stopped.')
        temporary.write_bytes(body)
        _publish(temporary, target, total, len(body))
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _extract_youtube(store, url: str, mode: str) -> dict:
    """Read public YouTube details. The address is not included in errors."""
    import json
    if mode == 'channel':
        from .youtube_fallback import channel_url
        url = channel_url(url) + '/videos'
    elif mode != 'video':
        raise ValueError('Could not read this YouTube video.')
    request = {'url': url, 'mode': mode, 'start': 1, 'end': 1}
    cookie = store.directory / 'youtube-cookies.txt'
    temporary = None
    try:
        from .cookies import allow_downloads
        if (mode != 'video' or allow_downloads(store)) and cookie.is_file():
            temporary = tempfile.NamedTemporaryFile(prefix='youtube-cookies-', dir=store.directory, delete=False)
            temporary.write(cookie.read_bytes())
            temporary.close()
            os.chmod(temporary.name, 0o600)
            request['cookie'] = temporary.name
        args = [sys.executable, '-m', 'backend.youtube_extract']
        if mode == 'channel':
            from .youtube_fallback import run_extractor
            for attempt in range(2):
                try:
                    with tempfile.TemporaryFile() as output:
                        run_extractor(args, input=json.dumps(request).encode(), stdout=output, store=store, timeout=90)
                        output.seek(0)
                        result = subprocess.CompletedProcess(args, 0, output.read(8_000_001))
                    break
                except subprocess.SubprocessError:
                    if attempt:
                        raise
                    # Channel artwork is public. Retry transient failures without
                    # a saved session, which can itself prevent public extraction.
                    request.pop('cookie', None)
        else:
            result = subprocess.run(args, input=json.dumps(request).encode(), capture_output=True, timeout=90, env=subprocess_env('youtube'))
    except subprocess.SubprocessError:
        raise ValueError('Could not read these YouTube details. Try again in a moment.') from None
    finally:
        if temporary is not None:
            os.unlink(temporary.name)
    if result.returncode or len(result.stdout) > 8_000_000:
        raise ValueError('Could not read this YouTube video. Check that it is available, then try again.')
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ValueError('Could not read this YouTube video. Check that it is available, then try again.') from None
    if not isinstance(data, dict):
        raise ValueError('Could not read this YouTube video. Check that it is available, then try again.')
    return data


def _probe_youtube(store, video_id: str) -> dict:
    data = _extract_youtube(store, 'https://www.youtube.com/watch?v=' + video_id, 'video')
    if not data.get('title') or not re.fullmatch(r'\d{8}', str(data.get('upload_date') or '')):
        raise ValueError('Could not read this YouTube video. Check that it is available, then try again.')
    data['title'] = str(data['title'])[:180]
    data['channel'] = str(data.get('channel') or data.get('uploader') or 'YouTube')[:120]
    data['description'] = str(data.get('description') or '')[:4000]
    return data


def _restricted(store, stem: str) -> str:
    if store.get('download_restrict_filenames') != 'true':
        return stem
    import unicodedata
    stem = unicodedata.normalize('NFKD', stem).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^A-Za-z0-9._-]+', '_', stem).strip('._') or 'video'


def _youtube_target(store, media: Path, info: dict) -> Path:
    from .media_library import contained, safe_name
    from .storage_layout import fixed, FIXED_NAMES
    if fixed(store): media = contained(media, FIXED_NAMES['youtube_downloads'])
    title = safe_name(info.get('title') or 'Video')
    uploaded = info.get('upload_date') or ''
    if not re.fullmatch(r'\d{8}', uploaded):
        return contained(media, f'Unsorted/{_restricted(store, title)}.mp4')
    day = date(int(uploaded[:4]), int(uploaded[4:6]), int(uploaded[6:8]))
    folder = safe_name(info.get('channel') or 'YouTube', 80)
    season = contained(media, f'{folder}/Season {day.year}')
    used = {path.name for path in season.glob(f's{day.year}.e{day:%m%d}*.mp4')} if season.is_dir() else set()
    episode = next((number for number in range(1, 100) if not any(name.startswith(f's{day.year}.e{day:%m%d}{number:02d}') for name in used)), None)
    if episode is None:
        raise ValueError('This channel already has 99 videos saved for that day.')
    stem = _restricted(store, f's{day.year}.e{day:%m%d}{episode:02d} - {title}')
    return contained(media, f'{folder}/Season {day.year}/{stem}.mp4')


def _save_artwork(store, url: str, target: Path) -> bool:
    if not url or target.exists():
        return bool(url) and target.exists()
    from .catalogue import thumbnail
    from .media_library import artwork
    return artwork(store, thumbnail(store, {'thumbnails': {'medium': {'url': url}}}), target)


def _cached_artwork(store, url: str, fetch=None) -> str:
    from .metadata_control import youtube_task
    return youtube_task(store, lambda: _cached_artwork_active(store, url, fetch))


def _cached_artwork_active(store, url: str, fetch=None) -> str:
    """Save a YouTube picture locally so the hover card can show it immediately."""
    url = str(url or '')
    if url.startswith('//'):
        url = 'https:' + url
    if not url.startswith('https://'):
        return ''
    from .artwork import fetch_image
    from .catalogue import thumbnail
    path = thumbnail(store, {'thumbnails': {'medium': {'url': url}}})
    key = path.rsplit('/', 1)[-1]
    if not re.fullmatch(r'[a-f0-9]{64}', key):
        return ''
    folder = store.directory / 'artwork'
    folder.mkdir(exist_ok=True, mode=0o700)
    target = folder / key
    if target.is_file():
        return path
    try:
        data = (fetch or fetch_image)(url)
    except (OSError, ValueError):
        return ''
    from .metadata_control import checkpoint
    checkpoint()
    temporary = folder / (key + '.part')
    temporary.write_bytes(data)
    os.replace(temporary, target)
    return path


def youtube_card(store, info: dict, channel: dict | None = None, fetch=None) -> dict:
    """Fanart when the channel has one, otherwise the video picture, plus the description."""
    channel = channel or {}
    return {
        'title': str(info.get('title') or '')[:180],
        'channel': str(info.get('channel') or '')[:120],
        'overview': str(info.get('description') or '')[:500],
        'image': _cached_artwork(store, str(channel.get('banner') or info.get('thumbnail') or ''), fetch),
    }


def _channel_images(store, channel_id: str) -> dict:
    from .metadata_control import youtube_task
    return youtube_task(store, lambda: _channel_images_active(store, channel_id), {})


def _channel_images_active(store, channel_id: str) -> dict:
    from .youtube_fallback import channel_banner
    try:
        data = _extract_youtube(store, channel_id, 'channel')
    except ValueError:
        return {}
    images = [item for item in data.get('thumbnails') or [] if isinstance(item, dict) and item.get('url')]
    avatar = next((item['url'] for item in images if 'avatar' in str(item.get('id', '')).lower()), '')
    if not avatar:
        avatar = next((item['url'] for item in images if item.get('width') and item.get('width') == item.get('height')), '')
    return {'avatar': avatar, 'banner': channel_banner(data), 'description': str(data.get('description') or '')}


def write_youtube_sidecars(store, media: Path, video_path: Path, video_id: str, info: dict, channel: dict | None = None, save_image=None) -> str:
    from .metadata_control import youtube_task
    return youtube_task(store, lambda: _write_youtube_sidecars(store, media, video_path, video_id, info, channel, save_image))


def _write_youtube_sidecars(store, media: Path, video_path: Path, video_id: str, info: dict, channel: dict | None = None, save_image=None) -> str:
    """Write the episode .nfo, tvshow.nfo, poster, fanart and thumbnail beside a saved video."""
    from .media_library import contained, xml_file
    save = save_image or _save_artwork
    root = media.resolve()
    video_path = video_path.resolve()
    uploaded = str(info.get('upload_date') or '')
    if not re.fullmatch(r'\d{8}', uploaded):
        raise ValueError('Could not date this YouTube video, so its show information was not written.')
    day = date(int(uploaded[:4]), int(uploaded[4:6]), int(uploaded[6:8]))
    found = re.search(r's\d{4}\.e\d{4}(\d{2})', video_path.name)
    episode = int(found.group(1)) if found else int(info.get('episode') or 1)
    show = video_path.parent.parent
    title = str(info.get('title') or video_path.stem)
    channel_name = str(info.get('channel') or 'YouTube')
    if channel is None and info.get('channel_id'):
        channel = _channel_images(store, str(info['channel_id']))
    channel = channel or {}
    xml_file(contained(root, video_path.with_suffix('.nfo').relative_to(root)), 'episodedetails', {
        'title': title, 'showtitle': channel_name, 'plot': info.get('description') or '',
        'season': day.year, 'episode': f'{day:%m%d}{episode:02d}', 'aired': day.isoformat(),
        'premiered': day.isoformat(), 'year': day.year,
    }, video_id)
    missing = []
    thumb = contained(root, video_path.with_name(video_path.stem + '-thumb.jpg').relative_to(root))
    if show != root:
        xml_file(contained(root, (show / 'tvshow.nfo').relative_to(root)), 'tvshow', {
            'title': channel_name, 'plot': channel.get('description') or '', 'studio': 'YouTube',
        }, str(info.get('channel_id') or video_id))
        poster = contained(root, (show / 'poster.jpg').relative_to(root))
        fanart = contained(root, (show / 'fanart.jpg').relative_to(root))
        if not save(store, str(channel.get('avatar') or ''), poster):
            missing.append(poster.name)
        if not save(store, str(channel.get('banner') or ''), fanart):
            missing.append(fanart.name)
    if not save(store, str(info.get('thumbnail') or ''), thumb):
        missing.append(thumb.name)
    return 'Artwork unavailable; the show information was saved.' if missing else ''


@youtube_traffic
def _save_youtube(app, store, video_id: str, stop, task) -> None:
    from .activity import record
    from .downloads import QUALITIES, command
    from .storage_layout import path as media_path
    info = _probe_youtube(store, video_id)
    channel = _channel_images(store, str(info['channel_id'])) if info.get('channel_id') else {}
    title = info['title'] or 'YouTube video'
    record(store, task, title, 'running', 'Downloading from YouTube.', 0, youtube_card(store, info, channel))
    media = Path(media_path(store, 'youtube', app.config['MEDIA_DIR']))
    media.mkdir(parents=True, exist_ok=True)
    target = _youtube_target(store, media, info)
    if target.exists():
        raise ValueError('A file already exists for this YouTube video; it was kept.')
    quality = store.get('download_quality', '1080')
    if quality not in QUALITIES:
        quality = '1080'
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.reeldock-auto-', dir=media) as directory:
        folder = Path(directory)
        cookie = store.directory / 'youtube-cookies.txt'
        snapshot = None
        from .cookies import allow_downloads
        if allow_downloads(store) and read(store, 'youtube')['authenticated_retry'] and cookie.is_file():
            snapshot = folder / 'cookies.txt'
            snapshot.write_bytes(cookie.read_bytes())
            os.chmod(snapshot, 0o600)
        args = command(video_id, quality, folder, snapshot)
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=subprocess_env('youtube'))
        last = 0.0
        try:
            while True:
                if stop.is_set():
                    process.terminate()
                    raise ValueError('Auto download stopped.')
                line = process.stdout.readline() if process.stdout is not None else ''
                if not line:
                    if process.poll() is not None:
                        break
                    continue
                if line.startswith('download:ReelDock:') and time.monotonic() - last >= 2:
                    parts = line.strip().split(':')
                    done = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
                    total = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
                    record(store, task, title, 'running', 'Downloading from YouTube.', min(99, done / total * 100) if total else None)
                    last = time.monotonic()
            code = process.wait(timeout=20)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        if code:
            raise ValueError('YouTube download failed. Check that the video is available, then try again.')
        output = folder / 'video.mp4'
        if not output.is_file() or output.stat().st_size == 0:
            raise ValueError('No playable video was produced.')
        try:
            os.link(output, target)
        except FileExistsError:
            raise ValueError('A file already exists for this YouTube video; it was kept.')
        except OSError:
            os.replace(output, target)
        else:
            output.unlink()
    note = write_youtube_sidecars(store, media, target, video_id, info, channel)
    record(store, task, title, 'complete', note or 'Saved in the YouTube folder.', 100)


def start_youtube(app, store, video_id: str, source_name: str, stop) -> None:
    """Queue a guide video, or download a YouTube link that is not in the guide yet."""
    import json
    from .activity import record
    from .downloads import QUALITIES, enqueue
    task = 'auto-youtube-' + video_id
    from .media_holds import blocked
    if blocked(store,['video:'+video_id,'managed:video:'+video_id]):
        raise _HeldSource('This video was cleared. Start or resume this video explicitly to download it again.')
    with store.connect() as db:
        row = db.execute('SELECT data FROM videos WHERE id=?', (video_id,)).fetchone()
    if row:
        video = json.loads(row['data'])
        if video.get('state') == 'published':
            quality = store.get('download_quality', '1080')
            enqueue(store, {**video, 'id': video_id}, quality if quality in QUALITIES else '1080')
            record(store, task, video.get('title') or source_name, 'complete', 'Queued with your other downloads.')
            return
    record(store, task, source_name, 'running', 'Downloading from YouTube.', 0)
    _save_youtube(app, store, video_id, stop, task)


def start_torrent(app, store, data, name):
    from .torrents import _engine, _with_found_title
    engine = _engine(app, store)
    preview = engine.preview_bytes(data, name)
    if preview.get('status') != 'ready' or not preview.get('accepted'):
        raise ValueError(preview.get('reason') or 'This torrent is not media. Nothing was downloaded.')
    chosen = [item['index'] for item in preview.get('files') or [] if item.get('selected') and item.get('selectable')]
    if not chosen:
        raise ValueError('This torrent has no media to download.')
    from .media_holds import blocked
    key=engine.preview_activity_key(preview['id'],chosen)
    if blocked(store,key):
        raise _HeldSource('This media was cleared. Start or resume this file explicitly to download it again.')
    try:
        preview = _with_found_title(store,engine,preview)
        engine.start(preview['id'], chosen, preview.get('suggested_name') or '')
    except ValueError:
        if blocked(store,key):
            raise _HeldSource('This media was cleared. Start or resume this file explicitly to download it again.') from None
        raise


def start_nzb(app, store, data, name):
    from .nzb import _engine, _with_found_title
    engine = _engine(app, store)
    preview = engine.preview_bytes(data,name,check_availability=False)
    if preview.get('status') != 'ready' or not preview.get('accepted'):
        raise ValueError(preview.get('reason') or 'This NZB is not media. Nothing was downloaded.')
    chosen = [item['index'] for item in preview.get('files') or [] if item.get('selected') and item.get('selectable')]
    if not chosen:
        raise ValueError('This NZB has no media to download.')
    from .media_holds import blocked
    key=engine.preview_activity_key(preview['id'],chosen)
    if blocked(store,key):
        raise _HeldSource('This media was cleared. Start or resume this file explicitly to download it again.')
    try:
        preview = _with_found_title(store,engine,preview)
        engine.start(preview['id'], chosen, preview.get('suggested_name') or '')
    except ValueError:
        if blocked(store,key):
            raise _HeldSource('This media was cleared. Start or resume this file explicitly to download it again.') from None
        raise


def _handle(app, store, folder, path: Path, kind, key, stop, torrent, fetch, youtube=None, nzb=None):
    from .activity import record as task
    from .event_log import record
    if kind == 'nzb':
        data = _read_file(path, LIMITS['.nzb'])
        (nzb or start_nzb)(app, store, data, path.name)
        _accept(folder, path)
        _finish(store, key, 'done', 'NZB download started.')
        record(store, 'NZB auto download started', 'A .nzb file from the auto-download folder started with the usual movie or episode name.', 'Downloads')
        return
    if kind == 'strm':
        url = _stream_url(_read_file(path, LIMITS['.strm']).decode('utf-8-sig'))
        video = youtube_id(url)
        if video:
            (youtube or start_youtube)(app, store, video, path.name, stop)
            note = 'A YouTube link from the auto-download folder was queued.'
        else:
            task(store, 'auto-' + key[:20], path.name, 'running', 'Downloading this stream.', 0)
            _save_stream(store, path.name, url, fetch, stop)
            task(store, 'auto-' + key[:20], path.name, 'complete', 'Saved with the usual movie or episode name.', 100)
            note = 'A .strm file from the auto-download folder was saved.'
        _accept(folder, path)
        _finish(store, key, 'done', 'Downloaded.')
        record(store, 'Stream download finished', note, 'Downloads')
        return
    data = _read_file(path, LIMITS['.torrent'])
    (torrent or start_torrent)(app, store, data, path.name)
    _accept(folder, path)
    _finish(store, key, 'done', 'Torrent download started.')
    record(store, 'Torrent auto download started', 'A .torrent file from the auto-download folder started with the usual movie or episode name.', 'Downloads')


def scan(app, store, stop, kinds, torrent=None, fetch=None, youtube=None, nzb=None):
    from .acquisition import held
    if held(store):
        return
    """Start downloads for new files. Each kind is handled by one process."""
    if stop.is_set() or not _scan_enabled(store) or store.reset_pending():
        return
    folder = watch_folder(app, store)
    if folder is None:
        return
    initialise(store)
    found = []
    from .download_control import paused
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if len(found) >= 8:
                    break
                suffix = Path(entry.name).suffix.lower()
                kind = KINDS.get(suffix)
                if entry.name.startswith('.') or kind not in kinds or not accepts(store, kind) or not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if time.time() - info.st_mtime < 2 or info.st_size <= 0:
                    continue
                if paused(store, _source_engine(Path(entry.path), kind)):
                    continue
                found.append((Path(entry.path), kind, info))
    except OSError:
        logging.exception('Auto-download folder could not be read')
        return
    for path, kind, info in found:
        if stop.is_set() or not _scan_enabled(store) or store.reset_pending() or held(store):
            return
        if not accepts(store, kind):
            continue
        if paused(store, _source_engine(path, kind)):
            continue
        key = _key(path.name, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino)
        if not _claim(store, key, path.name, kind):
            continue
        try:
            _handle(app, store, folder, path, kind, key, stop, torrent, fetch, youtube, nzb)
        except _HeldSource as exc:
            # Keep the arrival and durable hold; ordinary scans never release it.
            _finish(store,key,'held',str(exc))
        except Exception as exc:
            logging.exception('Auto download failed')
            message = str(exc) if isinstance(exc, ValueError) else 'Auto download failed. The file was left in the folder.'
            _finish(store, key, 'failed', message)
            from .activity import record as task
            task(store, 'auto-' + key[:20], path.name, 'failed', message)


def loop(app, store, stop, kinds):
    while not stop.is_set():
        with store.background_operation():
            if not store.reset_pending():
                try:
                    scan(app, store, stop, kinds)
                except Exception:
                    logging.exception('Auto-download check paused; retrying')
        stop.wait(3)
