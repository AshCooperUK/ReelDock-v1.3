"""Export cached TMDB metadata as Emby-readable sidecars; preserve custom files."""
from .metadata_control import checkpoint, vod_task
from .storage_layout import source_base
import hashlib
import json
import re
import time
import secrets
import xml.etree.ElementTree as ET
from pathlib import Path
from . import m3u
from .activity import record
from .m3u_metadata import TMDB


def initialise(store):
    with store.connect() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS m3u_sidecars(path TEXT PRIMARY KEY,digest TEXT);
        CREATE TABLE IF NOT EXISTS m3u_exported(source TEXT,path TEXT,target TEXT,checked REAL,PRIMARY KEY(source,path,target));''')

def owned_write(store,base,path,raw):
    checkpoint()
    path=m3u.contained(base,path.relative_to(base));digest=hashlib.sha256(raw).hexdigest()
    with store.connect() as db:old=db.execute('SELECT digest FROM m3u_sidecars WHERE path=?',(str(path),)).fetchone()
    if path.exists():
        if not path.is_file() or path.stat().st_size>12*1024*1024:return False
        existing=hashlib.sha256(path.read_bytes()).hexdigest()
        if existing==digest:return False
        if not old or existing!=old['digest']:return False
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name('.reeldock-'+secrets.token_hex(12)+'.tmp')
    try:
        checkpoint()
        temporary.write_bytes(raw)
        if path.exists():temporary.replace(path)
        else:
            import os
            os.link(temporary,path);temporary.unlink()
    finally:temporary.unlink(missing_ok=True)
    with store.connect() as db:db.execute('INSERT OR REPLACE INTO m3u_sidecars VALUES(?,?)',(str(path),digest))
    return True

def cleanup_removed(store,base,removed):
    """Delete unchanged owned sidecars only, within this playlist's export tree."""
    base=Path(base);candidates=set();directories=set()
    for relative in removed:
        target=m3u.contained(base,relative)
        candidates.add(target.with_suffix('.nfo'))
        for suffix in ('-poster','-fanart','-thumb'):
            for ext in ('.jpg','.png'):candidates.add(target.with_name(target.stem+suffix+ext))
        parent=target.parent
        while parent!=base and parent.is_relative_to(base) and not (getattr(store, 'media_root', '') and parent.parent==base):
            directories.add(parent);parent=parent.parent
    for folder in sorted(directories,key=lambda p:len(p.parts),reverse=True):
        # Show-level files are shared by every remaining episode and season.
        if not any(p.suffix.lower() in ('.strm','.mp4','.mkv','.avi','.mov','.webm','.ts','.m4v') for p in folder.rglob('*')):
            candidates.update(folder/name for name in ('tvshow.nfo','poster.jpg','poster.png','fanart.jpg','fanart.png'))
    for candidate in candidates:
        path=m3u.contained(base,candidate.relative_to(base))
        with store.connect() as db:owned=db.execute('SELECT digest FROM m3u_sidecars WHERE path=?',(str(path),)).fetchone()
        if not owned:continue
        if path.exists():
            if not path.is_file() or path.stat().st_size>12*1024*1024:continue
            if hashlib.sha256(path.read_bytes()).hexdigest()!=owned['digest']:continue
            path.unlink()
        with store.connect() as db:db.execute('DELETE FROM m3u_sidecars WHERE path=?',(str(path),))
    for folder in sorted(directories,key=lambda p:len(p.parts),reverse=True):
        folder=m3u.contained(base,folder.relative_to(base))
        try:folder.rmdir()
        except OSError:pass  # Never remove a directory that still contains files.


def xml(tag,data,season=None,episode=None,show=''):
    root=ET.Element(tag)
    mapping={'title':'title','year':'year','overview':'plot','release_date':'premiered','first_air_date':'premiered','air_date':'aired','runtime':'runtime','vote_average':'rating','original_language':'language'}
    data=dict(data)
    if not data.get('title'):data['title']=data.get('name','')
    if not data.get('year'):
        premiered=str(data.get('release_date') or data.get('first_air_date') or data.get('air_date') or '')
        if len(premiered)>=4 and premiered[:4].isdigit():data['year']=premiered[:4]
    for key,field in mapping.items():
        if data.get(key) is not None and data.get(key)!='':ET.SubElement(root,field).text=str(data[key])
    if data.get('id'):ET.SubElement(root,'uniqueid',type='tmdb',default='true').text=str(data['id'])
    imdb=data.get('imdb_id') or (data.get('external_ids') or {}).get('imdb_id')
    if isinstance(imdb,str) and re.fullmatch(r'tt\d{7,8}',imdb):ET.SubElement(root,'uniqueid',type='imdb').text=imdb
    for genre in data.get('genres',[]):ET.SubElement(root,'genre').text=genre['name']
    for person in data.get('credits',{}).get('cast',[])[:30]:
        actor=ET.SubElement(root,'actor');ET.SubElement(actor,'name').text=person.get('name','');ET.SubElement(actor,'role').text=person.get('character','')
    if season is not None:ET.SubElement(root,'season').text=str(season)
    if episode is not None:ET.SubElement(root,'episode').text=str(episode)
    if show:ET.SubElement(root,'showtitle').text=show
    return ET.tostring(root,encoding='utf-8',xml_declaration=True)

def artwork(store,base,path,remote):
    checkpoint()
    if not isinstance(remote,str) or not re.fullmatch(r'/[A-Za-z0-9_-]+\.(jpg|png)',remote):return False
    from .m3u_player import cached_image_path
    cached=cached_image_path(store,'https://image.tmdb.org/t/p/w780'+remote)
    # Keep the image's real format in its file name.
    return owned_write(store,base,path.with_suffix(Path(remote).suffix),cached.read_bytes())

def manual_entry(store,row,base,target,summary):
    """Write the user's own details as NFO and poster files Emby can read."""
    from .m3u_overrides import poster_path
    data=dict(title=summary.get('title',''),year=summary.get('year'),overview=summary.get('description',''),
              vote_average=summary.get('rating'),genres=[{'name':g} for g in summary.get('genres',[])],
              credits={'cast':[{'name':n,'character':''} for n in summary.get('cast',[])]})
    poster=poster_path(store,row['key']) if row.get('key') else None
    raw=poster.read_bytes() if poster and poster.is_file() else b''
    suffix='.png' if raw.startswith(b'\x89PNG') else '.jpg'
    changed=False
    if (row.get('kind') or json.loads(row['info']).get('kind'))=='movies':
        changed=owned_write(store,base,target.with_suffix('.nfo'),xml('movie',data)) or changed
        if raw:changed=owned_write(store,base,target.with_name(target.stem+'-poster'+suffix),raw) or changed
    else:
        folder=target.parent.parent if target.parent.name.lower().startswith('season') else target.parent
        changed=owned_write(store,base,folder/'tvshow.nfo',xml('tvshow',data)) or changed
        if raw:changed=owned_write(store,base,folder/('poster'+suffix),raw) or changed
    return changed

def export_entry(store,row,base,target):
    summary=json.loads(row['metadata']);info=json.loads(row['info'])
    if summary.get('manual'):return manual_entry(store,row,base,target,summary)
    identity=summary.get('tmdb_id')
    if not identity:return False
    client=TMDB(store);kind='movie' if row['kind']=='movies' else 'tv'
    # Reuses the same request cache as the catalogue and media player.
    entity=client.get(f'{kind}/{identity}')
    if not entity:return False
    changed=False
    def write(path,tag,data,**extra):
        nonlocal changed
        changed=owned_write(store,base,path,xml(tag,data,**extra)) or changed
    def art(path,remote):
        nonlocal changed
        changed=artwork(store,base,path,remote) or changed
    if kind=='movie':
        write(target.with_suffix('.nfo'),'movie',entity)
        art(target.with_name(target.stem+'-poster.jpg'),entity.get('poster_path'))
        art(target.with_name(target.stem+'-fanart.jpg'),entity.get('backdrop_path'))
    else:
        season=info.get('season');episode=info.get('episode')
        folder=target.parent.parent if target.parent.name.lower().startswith('season') else target.parent
        write(folder/'tvshow.nfo','tvshow',entity)
        art(folder/'poster.jpg',entity.get('poster_path'));art(folder/'fanart.jpg',entity.get('backdrop_path'))
        if season is not None and episode is not None:
            data=client.get(f'tv/{identity}/season/{int(season)}')
            ep=next((e for e in data.get('episodes',[]) if e.get('episode_number')==int(episode)),None)
            if ep:
                write(target.with_suffix('.nfo'),'episodedetails',ep,season=season,episode=episode,show=entity.get('name',''))
                art(target.with_name(target.stem+'-thumb.jpg'),ep.get('still_path'))
    return changed

def batch(app,store,limit=8):
    from .storage_layout import apply
    apply(app,store)
    with m3u.SIDECAR_LOCK:
        return vod_task(store, lambda: _batch(app,store,limit))


def _batch(app,store,limit=8):
    if store.get('m3u_share_metadata','true')!='true':return 0
    if not store.get('m3u_tmdb_token'):
        with store.connect() as db:
            if not db.execute("SELECT 1 FROM m3u_title_overrides WHERE mode='manual' LIMIT 1").fetchone():return 0
    with store.connect() as db:
        rows=db.execute('''SELECT f.*,s.folder,t.key,t.info,t.data AS metadata,t.checked FROM m3u_files f
        JOIN m3u_sources s ON s.id=f.source JOIN m3u_catalog_entries c ON c.source=f.source AND c.path=f.path
        JOIN m3u_catalog_titles t ON t.key=c.key LEFT JOIN m3u_exported e ON e.source=f.source AND e.path=f.path AND e.target='strm'
        WHERE s.state NOT IN ('running','queued') AND (json_extract(t.data,'$.tmdb_id') IS NOT NULL OR json_extract(t.data,'$.manual')=1)
        AND (e.checked IS NULL OR e.checked<t.checked) ORDER BY coalesce(e.checked,0) LIMIT ?''',(limit,)).fetchall()
        downloads=db.execute('''SELECT d.*,t.key,t.info,t.data AS metadata,s.folder,t.checked FROM m3u_downloads d
        JOIN m3u_sources s ON s.id=d.source JOIN m3u_catalog_entries c ON c.source=d.source AND c.path=d.path
        JOIN m3u_catalog_titles t ON t.key=c.key LEFT JOIN m3u_exported e ON e.source=d.source AND e.path=d.path AND e.target=d.destination
        WHERE d.state='complete' AND (json_extract(t.data,'$.tmdb_id') IS NOT NULL OR json_extract(t.data,'$.manual')=1)
        AND (e.checked IS NULL OR e.checked<t.checked OR e.checked<d.updated) LIMIT ?''',(limit,)).fetchall()
    jobs=[(dict(r),'strm') for r in rows]+[(dict(r),r['destination']) for r in downloads]
    changed=False
    for row,destination in jobs:
        checkpoint()
        record(store,'m3u-exports','Emby metadata & artwork','running','Saving local metadata for '+row['title']+'.')
        if destination=='strm':
            base=source_base(store, app.config['M3U_DIR'],row['folder']);target=m3u.contained(base,row['path'])
        else:
            from .m3u_downloads import roots
            target=Path(destination);base=next(r for r in roots(app) if target.is_relative_to(r));target=m3u.contained(base,target.relative_to(base))
        if target.is_file():changed=export_entry(store,row,base,target) or changed
        checkpoint()
        with store.connect() as db:db.execute('INSERT OR REPLACE INTO m3u_exported VALUES(?,?,?,?)',(row['source'],row['path'],destination,time.time()))
    if jobs:record(store,'m3u-exports','Emby metadata & artwork','complete',f'Processed {len(jobs)} titles for local metadata sharing.')
    if (changed or downloads) and store.get('m3u_export_refresh')=='true':
        from .emby import queue_refresh
        queue_refresh(store,'m3u-sidecars')
    return len(jobs)

def loop(app,store,stop):
    while not stop.is_set():
        paused = True
        with store.background_operation():
            if store.reset_pending():
                paused = True
            else:
                paused = False
                try:batch(app,store)
                except Exception:record(store,'m3u-exports','Emby metadata & artwork','error','Metadata export could not finish. Check folder permissions and the TMDB connection; retrying automatically.')
        if paused:
            stop.wait(1)
            continue
        stop.wait(10)
