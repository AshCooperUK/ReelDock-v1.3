"""A YouTube fallback keeps the Discover movie/episode's library identity."""
import io
import json
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

from . import media_library as library
from .vpn.transport import metadata as vpn_metadata


def request_for_video(store, video_id):
    with store.connect() as db:
        try:
            row=db.execute("""SELECT * FROM discover_requests WHERE kind IN ('movie','episode')
                AND json_extract(body,'$.discoveryEngine')=1 AND json_extract(body,'$.videoId')=?
                AND engine IN ('','youtube') AND state NOT IN ('failed','cancelled','cancelling')
                ORDER BY created DESC LIMIT 1""",(video_id,)).fetchone()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):return None
            raise
    return dict(row) if row else None


def planned_path(store, media, request, suffix='.mkv'):
    from .discover_acquire import library_name
    from .downloaders.torrent.rename import library_path
    from .storage_layout import names, download_base
    base=download_base(store)
    if base and Path(base).resolve()!=Path(media).resolve():
        raise ValueError('Discover fallback needs the shared ReelDock media mount.')
    if suffix not in ('.mkv','.mp4','.webm'):raise ValueError('Unsupported video format.')
    folders=names(store)
    relative=library_path('', 'video'+suffix, 'video', 'series' if request['kind']=='episode' else 'movie',
                          'emby',dict(series=folders['series'],movies=folders['movies']),
                          library_name(request,''),flat_episodes=request['kind']=='episode')
    if not relative:raise ValueError('The Discover library destination could not be determined.')
    library.contained(media,relative)
    return relative


def claim(store, media, video_id, request, suffix='.mkv'):
    relative=planned_path(store,media,request,suffix)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        taken=db.execute('SELECT id FROM media_files WHERE path=? COLLATE NOCASE AND id!=?',(relative,video_id)).fetchone()
        if taken or library.contained(media,relative).exists():
            raise ValueError('This episode or movie already has a saved file. Existing media was kept.')
        db.execute('UPDATE media_files SET path=? WHERE id=?',(relative,video_id))
    return relative


@vpn_metadata
def write_metadata(store, media, record, request, *, source='youtube', check=lambda:None):
    """Write catalogue NFOs/art, never the upload date as a TV season."""
    from .discover_acquire import _catalogue_identity
    from .m3u_sidecars import initialise, owned_write
    from .m3u_player import cached_image_path
    from PIL import Image
    root=Path(media).resolve();target=library.contained(root,record['path'])
    if record['path']!=planned_path(store,media,request,target.suffix):
        raise ValueError('This fallback has not been assigned its Discover library destination.')
    body=json.loads(request['body']);item=body['item'];episode=body.get('episode') or {}
    provider,identity=_catalogue_identity(request)
    if not identity and re.fullmatch(r'tmdb:movie:\d{1,12}',item.get('id','')):
        provider,identity='tmdb',item['id'].split(':')[-1]
    if not identity and re.fullmatch(r'tt\d{7,12}',str(item.get('imdbId') or '')):
        provider,identity='imdb',item['imdbId']
    initialise(store)
    def nfo(path,tag,fields):
        check()
        doc=ET.Element(tag)
        for key,value in fields.items():
            if value not in ('',None):ET.SubElement(doc,key).text=re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]','',str(value))
        if identity:ET.SubElement(doc,'uniqueid',type=provider,default='true').text=str(identity)
        if source:ET.SubElement(doc,'uniqueid',type=source,default='false').text=record['id']
        ET.indent(doc);owned_write(store,root,path,ET.tostring(doc,encoding='utf-8',xml_declaration=True))
    nfo(target.with_suffix('.nfo'),'episodedetails' if episode else 'movie',
        dict(title=episode.get('name') or item['title'],showtitle=item['title'] if episode else '',
             season=episode.get('season'),episode=episode.get('number'),year=item.get('year',''),
             aired=body.get('date','') if episode else '',plot=episode.get('summary') or item.get('description','')))
    if episode:nfo(target.parent/'tvshow.nfo','tvshow',dict(title=item['title'],year=item.get('year',''),plot=item.get('description','')))
    def art(name,url):
        check()
        path=library.contained(root,str((target.parent/name).relative_to(root)))
        if path.is_file():return
        if not url:raise ValueError('Catalogue artwork is unavailable. Retry when metadata is available.')
        source=cached_image_path(store,url)
        with Image.open(source) as image:
            image.thumbnail((1920,1920));output=io.BytesIO();image.convert('RGB').save(output,format='JPEG',quality=90)
        check()
        owned_write(store,root,path,output.getvalue())
    art('poster.jpg',item.get('image'))
    art('fanart.jpg',item.get('backdrop') or item.get('image'))
    if not all(path.is_file() for path in (target.with_suffix('.nfo'),target.parent/'poster.jpg',target.parent/'fanart.jpg')):
        raise ValueError('Discover library metadata could not be saved.')
    return ''


def publish(store, media, record, request, produced):
    def check(db=None):
        from .download_control import paused
        from .downloads import DownloadPaused
        if paused(store, 'youtube'):raise DownloadPaused('Downloads are paused.')
        if db is None:
            with store.connect() as connection:return check(connection)
        from .media_holds import blocked_in,request_keys
        current=db.execute('SELECT * FROM discover_requests WHERE id=?',(request['id'],)).fetchone()
        job=db.execute('SELECT state FROM downloads WHERE id=?',(record['id'],)).fetchone()
        if blocked_in(db,request_keys(request)+['video:'+record['id'],'managed:video:'+record['id']]):
            raise InterruptedError('Download cleared. Nothing was published.')
        if not current or current['state'] in ('cancelled','cancelling','failed') or (job and job['state'] not in ('queued','downloading')):
            raise InterruptedError('Download cancelled. Nothing was published.')
    check()
    if request['kind']=='movie':
        from .movie_options import policy,verify_file
        verify_file(produced,policy(store,json.loads(request['body'])['item']))
    elif request['kind']=='episode':
        from .show_options import policy
        from .movie_options import verify_file
        selected=policy(store,json.loads(request['body'])['item'])
        if selected:
            selected.update(quality=request['quality'],fallback=bool(request['fallback']))
            verify_file(produced,selected)
    write_metadata(store,media,record,request,check=check)
    check()
    target=library.contained(media,record['path']);target.parent.mkdir(parents=True,exist_ok=True)
    # Download staging uses the same media filesystem; link refuses overwrite.
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        check(db)
        os.link(produced,target)
        db.execute('UPDATE media_files SET bytes=? WHERE id=?',(target.stat().st_size,record['id']))
        db.execute('''INSERT INTO discover_owned(key,request_id,path,state) VALUES (?,?,?,'complete')
            ON CONFLICT(key) DO UPDATE SET request_id=excluded.request_id,path=excluded.path,state=excluded.state''',
            (request['episode_key'],request['id'],record['path']))
    produced.unlink()
    from .view_media.local import invalidate
    invalidate(media)
    return ''
