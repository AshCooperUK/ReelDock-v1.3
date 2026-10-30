"""Stable media paths and local TV metadata, shared by downloads and library management."""
import io
import json
import os
import re
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

PROFILES = ('emby', 'plex', 'kodi')


def disk_usage(store, media, channels):
    """Stat only known downloads for the visible page, including legacy MP4s."""
    if not channels:return {}
    with store.connect() as db:
        rows=db.execute('''SELECT m.id,m.channel,m.path FROM media_files m WHERE m.channel IN ('''+','.join('?' for _ in channels)+''')
            UNION SELECT v.id,v.channel,m.path FROM videos v LEFT JOIN media_files m ON m.id=v.id
            WHERE v.channel IN ('''+','.join('?' for _ in channels)+''') AND
            (EXISTS(SELECT 1 FROM downloads d WHERE d.id=v.id AND d.state='complete')
             OR EXISTS(SELECT 1 FROM media_files m WHERE m.id=v.id))''',[*channels,*channels]).fetchall()
    result={}
    for row in rows:
        size=0
        for relative in ([row['path']] if row['path'] else [])+[row['id']+'.mp4',row['id']+'.webm',row['id']+'.mkv']:
            try:
                path=contained(media,relative)
                if path.is_file():size=path.stat().st_size;break
            except (ValueError,OSError):pass
        result[row['channel']]=result.get(row['channel'],0)+size
    return result


def initialise(store):
    with store.connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS media_channels(id TEXT PRIMARY KEY, folder TEXT UNIQUE COLLATE NOCASE);
        CREATE TABLE IF NOT EXISTS media_files(id TEXT PRIMARY KEY, channel TEXT NOT NULL, path TEXT UNIQUE COLLATE NOCASE,
            day TEXT NOT NULL, episode INTEGER NOT NULL, profile TEXT NOT NULL, bytes INTEGER DEFAULT 0,
            UNIQUE(channel,day,episode));
        CREATE TABLE IF NOT EXISTS download_rules(channel TEXT PRIMARY KEY, quality TEXT DEFAULT '1080',
            videos INTEGER DEFAULT 0, shorts INTEGER DEFAULT 0, live INTEGER DEFAULT 0,
            range_days INTEGER DEFAULT 180, enabled_at REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS removed_channels(id TEXT PRIMARY KEY);
        ''')
        columns={r[1] for r in db.execute('PRAGMA table_info(downloads)')}
        if 'automatic' not in columns:
            db.execute('ALTER TABLE downloads ADD COLUMN automatic INTEGER DEFAULT 0')
        if 'replace_existing' not in columns:
            db.execute('ALTER TABLE downloads ADD COLUMN replace_existing INTEGER DEFAULT 0')


def safe_name(value, limit=120):
    from .plain_names import plain_text
    text=plain_text(value)
    text=re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', text)
    text=re.sub(r'\s+', ' ', text).strip(' .')
    text=text.encode('utf-8')[:limit].decode('utf-8','ignore').rstrip(' .') or 'Untitled'
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)',text,re.I):
        text='_'+text
    return text


def contained(root, relative):
    root=Path(root).resolve()
    candidate=(root/relative).resolve()
    if candidate == root or not candidate.is_relative_to(root):
        raise ValueError('Media path must stay within the media folder.')
    return candidate


def reserve(store, media, video_id):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        existing=db.execute('SELECT * FROM media_files WHERE id=?',(video_id,)).fetchone()
        if existing:
            return dict(existing)
        row=db.execute('SELECT v.data,c.data FROM videos v JOIN channels c ON c.id=v.channel WHERE v.id=?',(video_id,)).fetchone()
        if not row:
            raise ValueError('Saved video details are unavailable.')
        video,channel=map(json.loads,row)
        day=date.fromisoformat(video['date'])
        saved=db.execute('SELECT folder FROM media_channels WHERE id=?',(video['channel'],)).fetchone()
        if saved:
            folder=saved[0]
        else:
            folder=safe_name(channel['name'],80)
            from .storage_layout import fixed, FIXED_NAMES
            if fixed(store): folder=FIXED_NAMES['youtube']+'/'+folder
            if db.execute('SELECT 1 FROM media_channels WHERE folder=? COLLATE NOCASE',(folder,)).fetchone() or (contained(media,folder).exists() or contained(media,'tv_shows/'+folder).exists()):
                folder += ' ['+safe_name(video['channel'],40)+']'
            db.execute('INSERT INTO media_channels VALUES (?,?)',(video['channel'],folder))
        # Never renumber earlier downloads when older uploads are discovered later.
        used={r[0] for r in db.execute('SELECT episode FROM media_files WHERE channel=? AND day=?',(video['channel'],day.isoformat()))}
        episode=next((n for n in range(1,100) if n not in used),None)
        if episode is None:
            raise ValueError('This channel already has 99 assigned episodes on that day.')
        stem=f's{day.year}.e{day:%m%d}{episode:02d} - {safe_name(video["title"])}'
        if store.get('download_restrict_filenames')=='true':
            stem=unicodedata.normalize('NFKD',stem).encode('ascii','ignore').decode('ascii')
            stem=re.sub(r'[^A-Za-z0-9._-]+','_',stem).strip('._')
        relative=f'{folder}/Season {day.year}/{stem}.mp4'
        if contained(media,relative).exists():
            raise ValueError('An unmanaged file already exists at the planned media path.')
        profile=store.get('metadata_type','emby')
        db.execute('INSERT INTO media_files(id,channel,path,day,episode,profile) VALUES (?,?,?,?,?,?)',
                   (video_id,video['channel'],relative,day.isoformat(),episode,profile))
        return dict(id=video_id,channel=video['channel'],path=relative,day=day.isoformat(),episode=episode,profile=profile)


def local_file(store, media, video_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}',video_id):
        return None
    with store.connect() as db:
        row=db.execute('SELECT path FROM media_files WHERE id=?',(video_id,)).fetchone()
    for relative in ([row[0]] if row else [])+[video_id+'.mp4',video_id+'.webm',video_id+'.mkv']:
        try:
            path=contained(media,relative)
            if path.is_file() and path.stat().st_size:
                return path
        except (ValueError,OSError):
            pass
    return None


def file_sizes(store, media, ids, db=None):
    """Return {video id: bytes} for saved files, with one query per 500 ids.

    Uses the same containment and candidate order as local_file, so callers can
    check a whole page of rows without a connection and query per video.
    """
    ids=[video for video in dict.fromkeys(ids) if isinstance(video,str) and re.fullmatch(r'[A-Za-z0-9_-]{11}',video)]
    paths={}
    def lookup(connection):
        for offset in range(0,len(ids),500):
            batch=ids[offset:offset+500]
            paths.update((row[0],row[1]) for row in connection.execute('SELECT id,path FROM media_files WHERE id IN ('+','.join('?' for _ in batch)+')',batch))
    if ids:
        if db is None:
            with store.connect() as connection:lookup(connection)
        else:lookup(db)
    result={}
    for video in ids:
        for relative in ([paths[video]] if paths.get(video) else [])+[video+'.mp4',video+'.webm',video+'.mkv']:
            try:
                path=contained(media,relative)
                if path.is_file():
                    size=path.stat().st_size
                    if size:result[video]=size;break
            except (ValueError,OSError):
                pass
    return result


def atomic_bytes(path, content):
    from .metadata_control import checkpoint
    checkpoint()
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.metadata-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as output:
            output.write(content)
        os.chmod(name,0o644)
        checkpoint()
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)


def xml_file(path, tag, fields, identity):
    root=ET.Element(tag)
    for key,value in fields.items():
        ET.SubElement(root,key).text=re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]','',str(value or ''))
    ET.SubElement(root,'uniqueid',{'type':'youtube','default':'true'}).text=identity
    ET.indent(root)
    atomic_bytes(path,ET.tostring(root,encoding='utf-8',xml_declaration=True))


def artwork(store, reference, target):
    if not reference or not re.fullmatch(r'/artwork/[a-f0-9]{64}',reference):
        return False
    key=reference.rsplit('/',1)[1]
    cached=store.directory/'artwork'/key
    try:
        if cached.is_file():
            content=cached.read_bytes()
        else:
            with store.connect() as db:
                row=db.execute('SELECT url FROM artwork WHERE id=?',(key,)).fetchone()
            if not row:return False
            from .artwork import fetch_image
            content=fetch_image(row[0])
        from PIL import Image
        with Image.open(io.BytesIO(content)) as image:
            image.thumbnail((1920,1920))
            output=io.BytesIO()
            image.convert('RGB').save(output,format='JPEG',quality=90)
        atomic_bytes(target,output.getvalue())
        return True
    except Exception as exc:
        from .metadata_control import Cancelled
        if isinstance(exc, Cancelled): raise
        return False


def metadata_paths(media, record):
    """Keep sidecars beside their video and manual downloads directly under the channel."""
    from .storage_layout import FIXED_NAMES
    path = contained(media, record['path'])
    parts = Path(record['path']).parts
    manual = len(parts) == 3 and parts[0] in (FIXED_NAMES['youtube_downloads'], 'Discover')
    show = path.parent if manual else path.parent.parent
    stem = path.stem
    return show, path.with_name(stem + '.nfo'), path.with_name(stem + '-thumb.jpg')


def write_metadata(store, media, record):
    from .discover_publication import request_for_video, write_metadata as catalogue_metadata
    request=request_for_video(store,record['id'])
    if request:return catalogue_metadata(store,media,record,request)
    from .metadata_control import youtube_task
    return youtube_task(store, lambda: _write_metadata(store, media, record))


def _write_metadata(store, media, record):
    path=contained(media,record['path'])
    with store.connect() as db:
        row=db.execute('SELECT v.data,c.data FROM videos v JOIN channels c ON c.id=v.channel WHERE v.id=?',(record['id'],)).fetchone()
    if not row: raise ValueError('Saved metadata is unavailable.')
    video,channel=map(json.loads,row)
    show,nfo,thumb=metadata_paths(media,record)
    day=date.fromisoformat(record['day'])
    xml_file(contained(media,str(nfo.relative_to(Path(media).resolve()))),'episodedetails',
             {'title':video['title'],'showtitle':channel['name'],'plot':video.get('description',''),
              'season':day.year,'episode':f'{day:%m%d}{record["episode"]:02d}', 'aired':day.isoformat(),
              'premiered':day.isoformat(),'year':day.year,
              **({'runtime':max(1,round(video['duration_seconds']/60))} if video.get('duration_seconds') else {})},record['id'])
    xml_file(contained(media,str((show/'tvshow.nfo').relative_to(Path(media).resolve()))),'tvshow',
             {'title':channel['name'],'plot':channel.get('description',''),'studio':'YouTube'},video['channel'])
    missing=[]
    for reference,target in [(video.get('thumbnail'),thumb),
                             (channel.get('avatar'),show/'poster.jpg'),(channel.get('banner'),show/'fanart.jpg')]:
        target=contained(media,str(target.relative_to(Path(media).resolve())))
        if not target.exists() and not artwork(store,reference,target):missing.append(target.name)
    with store.connect() as db:
        db.execute('UPDATE media_files SET bytes=?,profile=? WHERE id=?',
                   (path.stat().st_size if path.exists() else 0,store.get('metadata_type','emby'),record['id']))
    return 'Artwork unavailable; use Refresh Media to retry.' if missing else ''


def organise(store, media, video_id):
    source=local_file(store,media,video_id)
    if not source:return False
    if source.suffix not in ('.mp4','.mkv','.webm'):return False
    record=reserve(store,media,video_id)
    target=contained(media,record['path'])
    target.parent.mkdir(parents=True,exist_ok=True)
    if source!=target:
        if target.exists():raise ValueError('The destination already exists.')
        os.replace(source,target)
    write_metadata(store,media,record)
    return True


def download_status(store, media, ids):
    """Confirm saved files in batches without opening a connection per video."""
    ids=list(dict.fromkeys(ids));result={video:False for video in ids}
    with store.connect() as db:
        for offset in range(0,len(ids),500):
            batch=ids[offset:offset+500]
            marks=','.join('?' for _ in batch)
            rows=db.execute("SELECT id,path FROM media_files WHERE id IN ("+marks+") UNION SELECT id,NULL AS path FROM downloads WHERE state='complete' AND id IN ("+marks+")",[*batch,*batch]).fetchall()
            paths={row['id']:row['path'] for row in rows if row['path']}
            known={row['id'] for row in rows}
            for video in batch:
                if video not in known:continue
                if not re.fullmatch(r'[A-Za-z0-9_-]{11}',video):continue
                for relative in ([paths[video]] if video in paths else [])+[video+'.mp4',video+'.webm',video+'.mkv']:
                    try:
                        path=contained(media,relative)
                        if path.is_file() and path.stat().st_size:result[video]=True;break
                    except (ValueError,OSError):pass
    return result


def metadata_batch(store, media, limit=3):
    """Resume unfinished saved-library sidecars in small batches after enabling."""
    from .activity import record as activity_record
    from .metadata_control import checkpoint, youtube_task
    def run():
        if store.get('youtube_metadata_pending') != '1': return
        generation = store.get('youtube_metadata_generation', '0')
        with store.connect() as db:
            rows = db.execute('SELECT * FROM media_files WHERE id>? ORDER BY id LIMIT ?',
                              (store.get('youtube_metadata_cursor', ''), limit)).fetchall()
        for row in rows:
            checkpoint()
            if contained(media, row['path']).is_file():
                activity_record(store, 'youtube-metadata', 'YouTube metadata & artwork', 'running', 'Updating saved video metadata and artwork.')
                with store.connect() as db:
                    channel_row = db.execute('SELECT c.id,c.data FROM channels c JOIN videos v ON v.channel=c.id WHERE v.id=?', (row['id'],)).fetchone()
                if channel_row and re.fullmatch(r'UC[A-Za-z0-9_-]{22}', channel_row['id']):
                    channel = json.loads(channel_row['data'])
                    if not channel.get('avatar') or not channel.get('banner'):
                        from .auto_downloads import _channel_images
                        from .catalogue import thumbnail
                        images = _channel_images(store, channel_row['id'])
                        checkpoint()
                        for key, remote in [('avatar', images.get('avatar')), ('banner', images.get('banner'))]:
                            if remote: channel[key] = thumbnail(store, {'thumbnails': {'medium': {'url': remote}}})
                        if images.get('description'): channel['description'] = images['description']
                        with store.connect() as db:
                            db.execute('UPDATE channels SET data=? WHERE id=?', (json.dumps(channel), channel_row['id']))
                _write_metadata(store, media, dict(row))
            checkpoint()
            with store.connect() as db:
                db.execute("UPDATE settings SET value=? WHERE key='youtube_metadata_cursor' AND (SELECT value FROM settings WHERE key='youtube_metadata_generation')=?", (row['id'], generation))
        if len(rows) < limit:
            checkpoint()
            with store.connect() as db:
                db.execute("UPDATE settings SET value='0' WHERE key='youtube_metadata_pending' AND (SELECT value FROM settings WHERE key='youtube_metadata_generation')=?", (generation,))
            activity_record(store, 'youtube-metadata', 'YouTube metadata & artwork', 'complete', 'Saved video metadata pass finished.')
    return youtube_task(store, run)
