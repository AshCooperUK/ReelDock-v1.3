"""Explicit server downloads. Owned media is independent of playlist exports."""
from .storage_layout import source_base
from contextlib import nullcontext
import hmac
import json
import os
import secrets
import shutil
import time
from pathlib import Path
from flask import jsonify, request, session
from . import m3u
from .vpn.transport import vod

KINDS = {'movies':'Movies','series':'TV Shows','tv':'TV Shows','unsorted':'Unsorted'}

def initialise(store):
    with store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS m3u_downloads (
            id TEXT PRIMARY KEY,source TEXT,path TEXT,title TEXT,kind TEXT,destination TEXT,
            state TEXT,progress REAL,message TEXT,updated REAL,created REAL,UNIQUE(source,path))''')
        # Private uploaded pointers are not playlist subscriptions or library exports.
        db.execute('''CREATE TABLE IF NOT EXISTS strm_sources (
            id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL UNIQUE,name TEXT NOT NULL,
            title TEXT NOT NULL,kind TEXT NOT NULL,url TEXT NOT NULL,created REAL NOT NULL)''')

def roots(app):
    from .storage_layout import apply
    if 'store' in app.extensions:apply(app,app.extensions['store'])
    return [Path(p).absolute() for p in json.loads(app.config.get('M3U_DOWNLOAD_ROOTS') or '[]')]

def safe_folder(app, value):
    if not isinstance(value,str) or not value:raise ValueError('Choose a server folder first.')
    path=Path(value).absolute()
    for root in roots(app):
        if path.is_relative_to(root):
            path=m3u.contained(root,path.relative_to(root))
            from .storage_layout import fixed, fixed_folders
            store = app.extensions.get('store')
            reserved_paths = ([app.config.get('DATA_DIR'), *[v for k,v in fixed_folders(store).items() if k not in ('movies','series')]]
                              if fixed(store) else (app.config.get('M3U_DIR'),app.config.get('MEDIA_DIR'),app.config.get('DATA_DIR')))
            for reserved in reserved_paths:
                if reserved:
                    other=Path(reserved).resolve()
                    if path.resolve().is_relative_to(other) or other.is_relative_to(path.resolve()):
                        raise ValueError('Downloads must be separate from stream links, YouTube media and app data.')
            if not path.is_dir() or not os.access(path,os.W_OK):raise ValueError('Choose a writable server folder.')
            return path
    raise ValueError('Choose a folder from the available server locations.')

def settings(app,store):
    from .m3u_emby import settings as emby_settings
    from .storage_layout import fixed, fixed_folders
    if fixed(store):
        return dict(folders={k:fixed_folders(store)[k] for k in ('movies','series')},
                    roots=[str(p) for p in roots(app)], locked=True, share_metadata=store.get('m3u_share_metadata','true')=='true',
                    refresh_emby=store.get('m3u_export_refresh','false')=='true', **emby_settings(store))
    defaults={key:str(root) for key,label in [('movies','Movies'),('series','TV Shows')] for root in roots(app) if root.name==label}
    return dict(folders=json.loads(store.get('m3u_download_folders',json.dumps(defaults))),
        roots=[str(p) for p in roots(app)],share_metadata=store.get('m3u_share_metadata','true')=='true',
        refresh_emby=store.get('m3u_export_refresh','false')=='true',**emby_settings(store))

def lookup(app,store,source,relative):
    from .storage_layout import apply
    apply(app,store)
    if not isinstance(source,str) or not isinstance(relative,str):raise ValueError('Choose a valid title.')
    if source.startswith('strm:'):
        from .strm import lookup as manual_lookup
        return manual_lookup(store,source,relative)
    with store.connect() as db:
        row=db.execute('SELECT f.*,s.folder FROM m3u_files f JOIN m3u_sources s ON s.id=f.source WHERE f.source=? AND f.path=?',(source,relative)).fetchone()
    if not row or row['kind'] not in KINDS or not relative.endswith('.strm'):raise ValueError('Choose a movie or episode.')
    base=source_base(store, app.config['M3U_DIR'],row['folder']);path=m3u.contained(base,relative)
    if not path.is_file() or path.stat().st_size>16384:raise ValueError('The stream link is unavailable.')
    lines=[s.strip() for s in path.read_text(encoding='utf-8-sig').splitlines() if s.strip() and not s.startswith('#')]
    if len(lines)!=1:raise ValueError('This is not a single video stream.')
    url=m3u.validate_url(lines[0])
    from urllib.parse import urlsplit
    if urlsplit(url).path.lower().endswith('.m3u8') or 'youtube.com' in urlsplit(url).hostname or urlsplit(url).hostname=='youtu.be':
        raise ValueError('Server downloads currently support video files, not live streams or streaming playlists.')
    return dict(row),url

CLEARED_MESSAGE='Cleared from Activity. Automatic retries are blocked until explicit Download or Retry.'


class _TransferStopped(Exception):
    def __init__(self,state,message):self.state=state;self.message=message


def activity_keys(db,source,path,job_id=None,selected=None):
    """Exact source/path and stored request identities; never display titles."""
    from .media_holds import vod_key,request_keys
    keys=[vod_key(source,path)];requests=set()
    has_requests=db.execute("SELECT 1 FROM sqlite_master WHERE name='discover_requests'").fetchone() is not None
    if selected is not None and selected.get('requestId'):requests.add(str(selected['requestId']))
    if job_id:
        if has_requests:requests.update(str(row[0]) for row in db.execute("SELECT id FROM discover_requests WHERE engine='vod' AND engine_id=?",(job_id,)))
        for table in ('movie_download_policy','show_download_policy'):
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone():continue
            row=db.execute('SELECT body FROM '+table+" WHERE engine='vod' AND job=?",(job_id,)).fetchone()
            if row:
                try:body=json.loads(row[0])
                except (TypeError,ValueError):body={}
                if isinstance(body,dict) and body.get('requestId'):requests.add(str(body['requestId']))
    for identity in requests if has_requests else []:
        row=db.execute('SELECT * FROM discover_requests WHERE id=?',(identity,)).fetchone()
        if row:keys.extend(request_keys(row))
    return list(dict.fromkeys(keys))


def _resume_selected(store,source,path):
    """Called only after authenticated/CSRF explicit selected Download/Start."""
    from .media_holds import resume
    with store.connect() as db:
        old=db.execute('SELECT id,state FROM m3u_downloads WHERE source=? AND path=?',(source,path)).fetchone()
        if old and old['state']=='cancelling':raise ValueError('This download is still stopping. Retry once it has stopped.')
        keys=activity_keys(db,source,path,old['id'] if old else None)
    return resume(store,keys)


def _checkpoint(store,job,keys,stop,db=None):
    from .media_holds import blocked_in
    with (store.connect() if db is None else nullcontext(db)) as current:
        row=current.execute('SELECT state FROM m3u_downloads WHERE id=?',(job['id'],)).fetchone()
        if blocked_in(current,keys):raise _TransferStopped('cancelled',CLEARED_MESSAGE)
        if not row or row[0] in ('cancelling','cancelled'):raise _TransferStopped('cancelled','Cancelled.')
    if stop.is_set():raise _TransferStopped('queued','Waiting to restart download.')


def recover(store):
    """Restart only unheld transfers; a per-item fence is never a retry request."""
    from .media_holds import blocked_in
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for row in db.execute("SELECT * FROM m3u_downloads WHERE state IN ('queued','running','cancelling')").fetchall():
            blocked=blocked_in(db,activity_keys(db,row['source'],row['path'],row['id']))
            state='cancelled' if blocked or row['state']=='cancelling' else 'queued'
            db.execute('UPDATE m3u_downloads SET state=?,message=CASE WHEN ? THEN ? ELSE message END WHERE id=?',(state,int(blocked),CLEARED_MESSAGE,row['id']))


def claim(store):
    """Skip held items while unrelated queued media can continue immediately."""
    from .media_holds import blocked_in
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for row in db.execute("SELECT * FROM m3u_downloads WHERE state='queued' ORDER BY created LIMIT 128").fetchall():
            if blocked_in(db,activity_keys(db,row['source'],row['path'],row['id'])):
                db.execute("UPDATE m3u_downloads SET state='cancelled',message=?,updated=? WHERE id=?",(CLEARED_MESSAGE,time.time(),row['id']))
                continue
            db.execute("UPDATE m3u_downloads SET state='running',message='Connecting to video provider.',updated=? WHERE id=?",(time.time(),row['id']))
            return dict(row)
    return None


def enqueue(app,store,source,path,*,movie_policy=None,episode_policy=None):
    from .media_holds import blocked,vod_key
    if blocked(store,vod_key(source,path)):raise ValueError(CLEARED_MESSAGE)
    row,_=lookup(app,store,source,path)
    return enqueue_resolved(app,store,source,path,row,movie_policy=movie_policy,episode_policy=episode_policy)

def enqueue_resolved(app,store,source,path,row,*,movie_policy=None,episode_policy=None,transaction=None):
    """Queue a trusted resolved source; manual Start supplies its atomic transaction."""
    manual=row.get('sourceType')=='strm'
    if manual:
        from .acquisition import ensure_open
        ensure_open(store)
    if 'sample' in (path+' '+row['title']).lower():raise ValueError('Sample videos are excluded.')
    category='series' if row['kind']=='tv' else row['kind']
    from .storage_layout import fixed
    if fixed(store) and category not in ('movies','series'):
        raise ValueError('Classify this title as a movie or TV show before downloading.')
    folder=safe_folder(app,settings(app,store)['folders'].get(category,''))
    # Preserve show/season structure but never reuse the .strm tree.
    parts=Path(path).parts
    offset=2 if fixed(store) else 1
    relative=Path(*parts[offset:]) if len(parts)>offset else Path(path)
    if manual:
        from .strm import destination
        relative=destination(row['title'],category)
    else:relative=Path(*(m3u.filename(part) for part in relative.with_suffix('').parts))
    if not manual and not fixed(store) and store.get('storage_base'):relative=Path(row['folder'])/relative
    target=m3u.contained(folder,relative)
    selected=movie_policy if movie_policy is not None else episode_policy
    if selected is not None:
        with store.connect() as db:owner=db.execute('SELECT * FROM discover_requests WHERE id=? AND kind=?',(selected['requestId'],'movie' if movie_policy is not None else 'episode')).fetchone()
        if not owner or owner['state']!='starting':raise ValueError('The media request is no longer starting.')
        from .discover_publication import planned_path
        target=m3u.contained(Path(app.config['MEDIA_DIR']),planned_path(store,app.config['MEDIA_DIR'],dict(owner))).with_suffix('')
        from .view_media.local import EXTENSIONS
        if movie_policy is not None and target.parent.exists() and any(p.suffix.lower() in EXTENSIONS for p in target.parent.iterdir()):raise ValueError('This movie folder already contains a video. Existing media was kept.')
    with (store.connect() if transaction is None else nullcontext(transaction)) as db:
        if transaction is None:db.execute('BEGIN IMMEDIATE')
        from .media_holds import blocked_in
        old=db.execute('SELECT * FROM m3u_downloads WHERE source=? AND path=?',(source,path)).fetchone()
        if blocked_in(db,activity_keys(db,source,path,old['id'] if old else None,selected)):raise ValueError(CLEARED_MESSAGE)
        if manual:
            # Check after acquiring the write lock, so Stop all cannot be bypassed.
            gate=db.execute("SELECT value FROM settings WHERE key='acquisition_hold'").fetchone()
            from .acquisition import file_held
            if (gate and gate[0]=='1') or file_held(store.directory):
                from .acquisition import AcquisitionHeld
                raise AcquisitionHeld('Downloads are stopped. Resume downloads in Activity before starting another.')
        if old and old['state'] in ('queued','running','cancelling'):
            if selected is not None:raise ValueError('This source file already has a download in progress.')
            return old['id']
        if old and old['state']=='complete' and Path(old['destination']).is_file():raise ValueError('This video is already saved on the server.')
        identity=old['id'] if old else secrets.token_hex(16)
        db.execute('INSERT OR REPLACE INTO m3u_downloads VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (identity,source,path,row['title'],category,str(target),'queued',0,'Waiting to download.',time.time(),time.time()))
        if movie_policy is not None:
            db.execute('INSERT OR REPLACE INTO movie_download_policy VALUES(?,?,?)',('vod',identity,json.dumps(movie_policy)))
        elif db.execute("SELECT 1 FROM sqlite_master WHERE name='movie_download_policy'").fetchone():
            db.execute("DELETE FROM movie_download_policy WHERE engine='vod' AND job=?",(identity,))
        if episode_policy is not None:
            db.execute('INSERT OR REPLACE INTO show_download_policy VALUES(?,?,?)',('vod',identity,json.dumps(episode_policy)))
        elif db.execute("SELECT 1 FROM sqlite_master WHERE name='show_download_policy'").fetchone():
            db.execute("DELETE FROM show_download_policy WHERE engine='vod' AND job=?",(identity,))
    return identity

def update(store,job,state,message,progress=None,destination=None):
    # Settle under the same write fence as Clear. A late progress/error callback
    # cannot resurrect an already cancelled item or downgrade saved media.
    from .media_holds import blocked_in
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM m3u_downloads WHERE id=?',(job['id'],)).fetchone()
        if not row:return
        blocked=blocked_in(db,activity_keys(db,row['source'],row['path'],row['id']))
        if row['state']=='complete' and (state!='complete' or blocked):return
        if blocked:
            state,message,progress='cancelled',CLEARED_MESSAGE,row['progress']
        elif row['state'] in ('cancelling','cancelled') and state in ('running','queued','failed','complete'):
            return
        db.execute('UPDATE m3u_downloads SET state=?,message=?,progress=?,updated=?,destination=coalesce(?,destination) WHERE id=?',
                   (state,message,progress,time.time(),destination,job['id']))


def basic_metadata(store,root,target,job,*,allowed=None):
    """Write local facts for ordinary downloads without requiring a TMDB key."""
    if store.get('m3u_share_metadata','true')!='true' or store.get('m3u_tmdb_token') or job['kind'] not in ('movies','series','tv'):
        return False
    from .downloaders.torrent.rename import parse_save_as
    from .downloaders.torrent.metadata import write_basic
    from .metadata_control import vod_task
    info=parse_save_as(job['title'])
    if not str(job['source']).startswith('strm:'):
        with store.connect() as db:
            row=db.execute('''SELECT t.info,t.data FROM m3u_catalog_entries e
                JOIN m3u_catalog_titles t ON t.key=e.key WHERE e.source=? AND e.path=?''',(job['source'],job['path'])).fetchone()
        if row:
            try:known,summary=json.loads(row['info']),json.loads(row['data'])
            except (TypeError,ValueError):known,summary={},{}
            if not isinstance(known,dict):known={}
            if not isinstance(summary,dict):summary={}
            # Only known local identity/length facts may enter generic NFOs.
            # Provider URLs, source bodies, plots, credits and IDs stay out.
            for field in ('year','season','episode'):
                if info.get(field) is None and type(known.get(field)) is int:
                    info[field]=known[field]
            if job['kind'] in ('series','tv') and info.get('episode') is not None and isinstance(known.get('name'),str) and known['name'].strip():
                info['name']=known['name'].strip()
            runtime=summary.get('runtimeMinutes')
            if type(runtime) in (int,float) and 0<runtime<=525600:
                info['runtime']=runtime
    # The common writer preserves valid custom/rich XML and confines every
    # sidecar to this same validated media root. Generation cancellation stays
    # independent of the completed media file.
    def write():
        from .metadata_control import work
        with (work(allowed) if allowed is not None else nullcontext()):
            return write_basic(store,root,str(target.relative_to(root)),'movie' if job['kind']=='movies' else 'series',info)
    vod_task(store,write)
    return True

@vod
def transfer(app,store,job,stop):
    from .m3u_player import upstream,video_suffix
    from .download_control import paused
    connection=None;temporary=None;keys=[]
    try:
        with store.connect() as db:keys=activity_keys(db,job['source'],job['path'],job['id'])
        _checkpoint(store,job,keys,stop)
        source_row,url=lookup(app,store,job['source'],job['path'])
        target=Path(job['destination']);safe_folder(app,str(next(r for r in roots(app) if target.is_relative_to(r))))
        # Revalidate all components before writing and reject any new symlink.
        root=next(r for r in roots(app) if target.is_relative_to(r));target=m3u.contained(root,target.relative_to(root))
        target.parent.mkdir(parents=True,exist_ok=True)
        _checkpoint(store,job,keys,stop)
        connection,response,resolved=upstream(url)
        _checkpoint(store,job,keys,stop)
        if response.status!=200:raise ValueError('The provider could not supply this video. Try again later.')
        prefix=response.read(4096);mime=response.getheader('Content-Type','').split(';')[0].lower()
        if source_row.get('sourceType')=='strm':
            from .strm import validate_video_response
            validate_video_response(mime,prefix)
        suffix=video_suffix(resolved,mime,prefix)
        if suffix=='.bin' or prefix.lstrip().startswith(b'#EXTM3U'):raise ValueError('This source is not a supported video file.')
        target=target.with_name(target.name+suffix)
        if target.exists():raise ValueError('A file already exists at the destination; it was kept.')
        total=int(response.getheader('Content-Length','0') or '0')
        from .settings.m3u import FIELDS as preferences
        minimum=preferences['min_download_bytes'].read(store)
        maximum=preferences['max_download_bytes'].read(store)
        if total and not minimum<=total<=maximum:raise ValueError('This video is outside the VOD download size range.')
        if shutil.disk_usage(target.parent).free<max(total,1024**3)+256*1024**2:raise ValueError('There is not enough free space for this download.')
        temporary=target.parent/('.reeldock-'+job['id']+'.part')
        if temporary.is_symlink():raise ValueError('The temporary download path is unavailable.')
        sent=0;last=0
        with temporary.open('wb') as file:
            chunk=prefix
            while chunk:
                _checkpoint(store,job,keys,stop)
                if paused(store, 'vod'):
                    update(store,job,'running','Paused by the global download control.',min(99,sent/total*100) if total else None)
                    stop.wait(0.2)
                    continue
                if sent+len(chunk)>maximum:raise ValueError('This video exceeds the VOD maximum download size.')
                file.write(chunk);sent+=len(chunk)
                if time.monotonic()-last>=1:
                    update(store,job,'running',f'{sent/1024**2:.1f} MB downloaded.',min(99,sent/total*100) if total else None);last=time.monotonic()
                chunk=response.read(256*1024)
            _checkpoint(store,job,keys,stop)
            file.flush();os.fsync(file.fileno())
        if total and sent!=total:raise ValueError('The provider ended the download early. Retry the download.')
        if not sent:raise ValueError('The provider returned an empty file.')
        if sent<minimum:raise ValueError('This video is below the VOD minimum download size.')
        _checkpoint(store,job,keys,stop)
        from .movie_options import job_policy,verify_file
        selected=job_policy(store,'vod',job['id'])
        movie_selected=selected is not None
        if selected is None:
            from .show_options import job_policy as show_policy
            selected=show_policy(store,'vod',job['id'])
        verify_file(temporary,selected)
        _checkpoint(store,job,keys,stop)
        if selected:
            from .view_media.local import EXTENSIONS
            if movie_selected and any(p!=temporary and p.suffix.lower() in EXTENSIONS for p in target.parent.iterdir()):raise ValueError('This movie folder already contains a video. Existing media was kept.')
            with store.connect() as db:owner=db.execute('SELECT * FROM discover_requests WHERE id=?',(selected['requestId'],)).fetchone()
            if not owner or owner['state'] in ('cancelled','cancelling','failed'):raise ValueError('The movie request was cancelled. Nothing was published.')
            from .discover_publication import write_metadata
            from .metadata_control import work
            from .media_holds import blocked
            with work(lambda:not blocked(store,keys)):
                write_metadata(store,app.config['MEDIA_DIR'],dict(id=job['id'],path=str(target.relative_to(app.config['MEDIA_DIR']))),dict(owner),source='')
            with store.connect() as db:
                state=db.execute('SELECT state FROM discover_requests WHERE id=?',(owner['id'],)).fetchone()
                cancelled=db.execute('SELECT state FROM m3u_downloads WHERE id=?',(job['id'],)).fetchone()
            if not state or state[0] in ('cancelled','cancelling','failed') or not cancelled or cancelled[0]=='cancelling':raise ValueError('The movie request was cancelled. Nothing was published.')
        # Publish without overwriting any existing file, even if another process created it.
        # Clear uses this same SQLite write fence. Only a fast no-overwrite
        # link runs under it; provider reads and metadata stay outside SQL.
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _checkpoint(store,job,keys,stop,db)
            os.link(temporary,target);temporary.unlink();temporary=None
            db.execute('UPDATE m3u_downloads SET destination=? WHERE id=?',(str(target),job['id']))
        _checkpoint(store,job,keys,stop)
        generic=False;message='Saved on the server.'
        if not selected:
            try:
                from .media_holds import blocked
                generic=basic_metadata(store,root,target,job,allowed=lambda:not blocked(store,keys))
            except Exception:
                generic=True
                message='Saved on the server. Metadata incomplete; use Repair metadata to try again.'
        _checkpoint(store,job,keys,stop)
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _checkpoint(store,job,keys,stop,db)
            db.execute("UPDATE m3u_downloads SET state='complete',message=?,progress=100,updated=?,destination=? WHERE id=?",(message,time.time(),str(target),job['id']))
        if store.get('m3u_export_refresh')=='true' and (generic or store.get('m3u_share_metadata','true')!='true'):
            from .emby import queue_refresh
            try:queue_refresh(store,'m3u-downloads')
            except Exception:
                # A refresh acknowledgement cannot turn an already published
                # media file into a failed/re-downloadable acquisition.
                update(store,job,'complete',message+' Library refresh pending.',100,str(target))
    except _TransferStopped as exc:
        update(store,job,exc.state,exc.message)
    except Exception as exc:
        try:
            if keys:_checkpoint(store,job,keys,stop)
        except _TransferStopped as stopped:
            update(store,job,stopped.state,stopped.message)
            return
        if str(job['source']).startswith('strm:') and isinstance(exc,ValueError):
            from .strm import transfer_error
            message=transfer_error(exc)
        else:message=str(exc) if isinstance(exc,ValueError) else 'Download failed. Check the source, destination permissions and free space, then retry.'
        update(store,job,'failed',message)
    finally:
        if connection:connection.close()
        if temporary:temporary.unlink(missing_ok=True)

def loop(app,store,stop):
    from .acquisition import held, settle_after_restart
    if not settle_after_restart(store):
        recover(store)
    while not stop.is_set():
        row = None
        with store.background_operation():
            row = None
            from .download_control import paused
            if not store.reset_pending() and not held(store) and not paused(store, 'vod'):
                row=claim(store)
                if row:transfer(app,store,dict(row),stop)
        if not row:stop.wait(2)


def register(app,store,access):
    initialise(store)
    @app.route('/api/m3u-download-settings',methods=['GET','POST'])
    def preferences():
        denied=access(local_action=True)
        if denied:return denied
        if request.method=='POST':
            if not session.get('csrf') or not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),session['csrf']):return jsonify(error='Reload the page.'),403
            body=request.get_json(silent=True)
            if not isinstance(body,dict):return jsonify(error='Choose valid settings.'),400
            # Public preference-only callers use the shared engine service.
            from . import settings as engine_settings
            if body and not set(body)-engine_settings.m3u.FIELDS.keys():
                try:engine_settings.save(store,'m3u',body)
                except ValueError as exc:return jsonify(error=str(exc)),400
                return jsonify(settings(app,store))
            try:
                if set(body)-{'folders','emby_folders',*engine_settings.m3u.FIELDS}:raise ValueError('Choose a supported setting.')
                from .storage_layout import fixed, LOCKED
                if fixed(store) and any(k in body for k in ('folders','emby_folders')): raise ValueError(LOCKED)
                folders=body.get('folders',settings(app,store)['folders'])
                if not isinstance(folders,dict) or set(folders)-{'movies','series','unsorted'}:raise ValueError('Choose valid download categories.')
                chosen={k:str(safe_folder(app,v)) for k,v in folders.items() if v}
                values=list(map(Path,chosen.values()))
                if any(a.is_relative_to(b) or b.is_relative_to(a) for i,a in enumerate(values) for b in values[i+1:]):raise ValueError('Choose separate folders for each download type.')
                from .settings.schema import validate, write
                public={k:body[k] for k in engine_settings.m3u.FIELDS if k in body}
                writes=validate(engine_settings.m3u.FIELDS,public) if public else {}
                from .m3u_emby import validate_paths
                emby_paths=body.get('emby_folders',settings(app,store)['emby_folders'])
                if not isinstance(emby_paths,dict):raise ValueError('Enter valid Emby folders.')
                if not fixed(store) and (body.get('auto_libraries',settings(app,store)['auto_libraries']) or any(emby_paths.values())):validate_paths(emby_paths)
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    write(db, {**writes,'m3u_emby_folders':json.dumps(emby_paths),'m3u_download_folders':json.dumps(chosen)})
                    engine_settings.m3u.after_write(db,public)
            except (ValueError,TypeError) as exc:return jsonify(error=str(exc) if isinstance(exc,ValueError) else 'Choose valid settings.'),400
        return jsonify(settings(app,store))

    @app.route('/api/m3u-folders',methods=['GET','POST'])
    def folders():
        denied=access(local_action=True)
        if denied:return denied
        from .storage_layout import fixed, LOCKED
        if fixed(store): return jsonify(error=LOCKED),409
        try:
            folder=safe_folder(app,request.args.get('path',''))
            if request.method=='POST':
                if not session.get('csrf') or not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),session['csrf']):return jsonify(error='Reload the page.'),403
                name=(request.get_json(silent=True) or {}).get('name','')
                if not isinstance(name,str) or name in ('','.','..') or any(c in name for c in '/\\:') or name.startswith('.'):raise ValueError('Enter a folder name.')
                m3u.contained(folder,name).mkdir()
            children=[]
            for child in sorted(folder.iterdir(),key=lambda p:p.name.casefold()):
                if child.is_dir() and not child.is_symlink() and not child.name.startswith('.'):
                    try:safe_folder(app,str(child));children.append(dict(name=child.name,path=str(child)))
                    except ValueError:pass
            parent=str(folder.parent) if any(folder!=r and folder.parent.is_relative_to(r) for r in roots(app)) else ''
            return jsonify(path=str(folder),parent=parent,items=children)
        except (OSError,ValueError):return jsonify(error='This folder is unavailable. Choose a mounted, writable server location.'),400

    @app.route('/api/m3u-server-downloads',methods=['GET','POST'])
    def jobs():
        denied=access(local_action=True)
        if denied:return denied
        if request.method=='POST':
            if not session.get('csrf') or not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),session['csrf']):return jsonify(error='Reload the page.'),403
            body=request.get_json(silent=True) or {}
            try:
                if body.get('action')=='download':
                    source,path=body.get('source',''),body.get('path','')
                    row,_=lookup(app,store,source,path)
                    _resume_selected(store,source,path)
                    enqueue_resolved(app,store,source,path,row)
                elif body.get('action')=='cancel':
                    with store.connect() as db:db.execute("UPDATE m3u_downloads SET state=CASE WHEN state='queued' THEN 'cancelled' ELSE 'cancelling' END,updated=? WHERE id=? AND state IN ('queued','running')",(time.time(),body.get('id','')))
                else:raise ValueError('Choose a download action.')
            except (ValueError,OSError) as exc:return jsonify(error=str(exc) if isinstance(exc,ValueError) else 'The destination is unavailable.'),400
        with store.connect() as db:
            rows=db.execute("SELECT * FROM m3u_downloads WHERE state IN ('queued','running','cancelling') OR id IN (SELECT id FROM m3u_downloads ORDER BY updated DESC LIMIT 30) ORDER BY created DESC").fetchall()
            items=[]
            from urllib.parse import urlencode
            for row in rows:
                item=dict(row)
                from .strm import public_metadata
                manual=public_metadata(db,row['source'])
                if manual:item.update(manual)
                else:
                    meta=db.execute('SELECT t.data,s.name FROM m3u_sources s LEFT JOIN m3u_catalog_entries c ON c.source=s.id AND c.path=? LEFT JOIN m3u_catalog_titles t ON t.key=c.key WHERE s.id=?',(row['path'],row['source'])).fetchone()
                    data=json.loads(meta['data'] or '{}') if meta else {}
                    item.update(artwork=data.get('artwork') or '/api/m3u/'+row['source']+'/artwork?'+urlencode({'path':row['path']}),subscription=meta['name'] if meta else 'M3U',description=data.get('description',''))
                items.append(item)
        return jsonify(items=items)
