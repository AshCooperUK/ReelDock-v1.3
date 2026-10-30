"""Optional Emby refresh and selected-user watched matching."""
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json
import os
import re
import time
from urllib.parse import urlsplit, quote
import requests


def initialise(store):
    with store.connect() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS emby_favourites(scope TEXT,channel TEXT,item TEXT,value INTEGER,PRIMARY KEY(scope,channel));
        CREATE TABLE IF NOT EXISTS emby_refresh(channel TEXT PRIMARY KEY, updated REAL);
        CREATE TABLE IF NOT EXISTS emby_watched(scope TEXT,id TEXT,state TEXT,message TEXT,PRIMARY KEY(scope,id));
        CREATE TABLE IF NOT EXISTS emby_attempts(scope TEXT,id TEXT,updated REAL,attempts INTEGER,retry_after REAL,PRIMARY KEY(scope,id));''')


def settings(store):
    from .youtube_matching import source_scope
    scope=source_scope(store)
    with store.connect() as db:
        rows=db.execute('''SELECT w.id,w.state,w.message,coalesce(a.updated,0) AS updated,
            coalesce(a.attempts,1) AS attempts,coalesce(a.retry_after,0) AS retry_after,
            json_extract(v.data,'$.title') AS title FROM emby_watched w
            LEFT JOIN emby_attempts a ON a.scope=w.scope AND a.id=w.id LEFT JOIN videos v ON v.id=w.id
            WHERE w.scope=? AND (w.state!='sent' OR w.id IN (
                SELECT h.id FROM emby_watched h LEFT JOIN emby_attempts ha ON ha.scope=h.scope AND ha.id=h.id
                WHERE h.scope=? AND h.state='sent' ORDER BY coalesce(ha.updated,0) DESC,h.rowid DESC LIMIT 15
            )) ORDER BY coalesce(a.updated,0) DESC,w.rowid DESC''',(scope,scope)).fetchall()
        latest=rows[0] if rows else None
        counts=dict(db.execute('SELECT state,count(*) FROM emby_watched WHERE scope=? GROUP BY state',(scope,)))
    from yt_dlp.version import __version__
    return dict(favourites=store.get('emby_favourites')=='true',favourites_status=store.get('emby_favourites_status','Favourites sync has not run yet.'),refresh_status=store.get('emby_refresh_status','No library scan requested yet.'),attempts=[dict(r) for r in rows],yt_dlp_version=__version__,cookie_check=store.get('emby_cookie_status',''),counts=counts,url=store.get('emby_url'),key_saved=bool(store.get('emby_key')),user=store.get('emby_user'),
                root=library_root(store),auto_refresh=store.get('emby_auto_refresh')=='true',
                watched=store.get('emby_watched')=='true',status=store.get('emby_status','Not connected.'),
                users=json.loads(store.get('emby_users','[]')),library_status=store.get('emby_library_status','Library matching runs automatically after connecting.'),
                watched_status=store.get('emby_watched_paused') or store.get('emby_watched_status') or (latest['message'] if latest else 'Watched matching has not run yet.'))


def validated(store, body):
    if not isinstance(body,dict):raise ValueError('Invalid Emby settings.')
    url=str(body.get('url','')).strip().rstrip('/')
    parsed=urlsplit(url)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Enter an HTTP or HTTPS Emby server address.')
    try:parsed.port
    except ValueError:raise ValueError('Invalid server port.')
    root=str(body.get('root','')).strip().replace('\\','/').rstrip('/')
    from .storage_layout import fixed, LOCKED
    if fixed(store) and root != library_root(store): raise ValueError(LOCKED)
    if root and (not root.startswith('/') or '..' in root.split('/')):raise ValueError('Enter the absolute TV library folder as Emby sees it.')
    user=str(body.get('user','')).strip()
    if user and not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',user):raise ValueError('Choose a valid Emby user.')
    for key in ('auto_refresh','watched','favourites'):
        if type(body.get(key,False)) is not bool:raise ValueError('Choose on or off.')
    if body.get('favourites') and not user:raise ValueError('Choose an Emby user before enabling favourites sync.')
    if body.get('watched') and store.get('youtube_watched_source','emby')=='emby' and (not user or not (store.directory/'youtube-cookies.txt').exists()):
        raise ValueError('Choose an Emby user and upload signed-in YouTube cookies before enabling watched matching.')
    key=str(body.get('key','')).strip()
    if '\r' in key or '\n' in key or len(key)>4096:raise ValueError('Invalid Emby API key.')
    if url!=store.get('emby_url') and not key:raise ValueError('Enter the API key for the new Emby server.')
    values={'emby_url':url,'emby_root':root,'emby_user':user,'emby_auto_refresh':str(body.get('auto_refresh',False)).lower(),
            'emby_watched':str(body.get('watched',False)).lower(),'emby_next_check':'0','emby_watched_paused':'','emby_favourites':str(body.get('favourites',False)).lower(),'emby_favourites_after':'0'}
    if key:values['emby_key']=key
    return values


def save(store, body):
    values=validated(store,body)
    with store.connect() as db:db.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)',values.items())


def save_preferences(store, values):
    allowed={'user','root','auto_refresh','watched','favourites'}
    if not isinstance(values,dict) or not values or set(values)-allowed:
        raise ValueError('Choose a supported Emby preference.')
    current=settings(store)
    if not current['key_saved'] or not current['url']:
        raise ValueError('Connect Emby first.')
    if 'user' in values and values['user'] and values['user'] not in {u['id'] for u in current['users']}:
        raise ValueError('Choose an available Emby user.')
    save(store,dict(current,**values))


def connect(store, body):
    # Test the submitted credentials before replacing a working connection.
    candidate=dict(body)
    candidate['watched']=False
    candidate['favourites']=False
    values=validated(store,candidate)
    class Connection:
        def get(self,key,default=''):
            return values.get(key,store.get(key,default))
    available=users(Connection())
    selected=body.get('user','')
    if len(available)==1:selected=available[0]['id']
    elif selected not in {u['id'] for u in available}:selected=''
    values=validated(store,dict(body,user=selected))
    values['emby_users']=json.dumps(available)
    values['emby_status']='Connected to Emby.'
    if body.get('url','').strip().rstrip('/')!=store.get('emby_url'):
        values['emby_watched_status']='Watched matching has not run for this server yet.'
    with store.connect() as db:db.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)',values.items())
    store.set('emby_discover_after','0')
    store.set('emby_refresh_after','0')
    store.set('emby_library_status','Library matching is queued and will run automatically.')
    return available


def discover_library(store):
    """Use exact host paths or downloaded video identities, never a library name guess."""
    if library_root(store):
        store.set('emby_library_status','Using Emby folder: '+library_root(store))
        return
    folders=api(store,'Library/VirtualFolders/Query')
    folders=folders.get('Items',[]) if isinstance(folders,dict) else folders
    locations=[str(path).replace('\\','/').rstrip('/') for folder in folders for path in folder.get('Locations',[])]
    from .storage_layout import fixed
    if fixed(store):
        store.set('emby_library_status', 'Set REELDOCK_EMBY_ROOT in Docker Compose to match the Emby media mount.')
        return
    host=os.environ.get('MEDIA_HOST_DIR','').replace('\\','/').rstrip('/')
    if store.get('storage_base') and os.environ.get('REELDOCK_HOST_ROOT'):
        from .storage_layout import names
        base=Path(store.get('storage_base'))
        if base.is_relative_to('/reeldock'):
            host=str(Path(os.environ['REELDOCK_HOST_ROOT'])/base.relative_to('/reeldock')/names(store)['youtube'])
    if store.get('storage_host_base'):
        from .storage_layout import names
        host=str(Path(store.get('storage_host_base'))/names(store)['youtube'])
    target=host
    if target and any(target==path or target.startswith(path+'/') for path in locations):
        store.set('emby_root',target)
        store.set('emby_library_status','Automatically matched Emby folder: '+target)
        return
    with store.connect() as db:
        known={r['id']:r['path'].removeprefix('tv_shows/') for r in db.execute("SELECT m.id,m.path FROM media_files m JOIN downloads d ON d.id=m.id WHERE d.state='complete'")}
    roots=set();offset=0
    if known:
        while offset<10000:
            data=api(store,'Items',params={'Recursive':'true','IncludeItemTypes':'Episode,Video','Fields':'ProviderIds,Path','StartIndex':offset,'Limit':200})
            items=data.get('Items',[])
            for item in items:
                path=str(item.get('Path','')).replace('\\','/')
                ids=[str(v) for k,v in (item.get('ProviderIds') or {}).items() if k.lower()=='youtube']
                for video in ids:
                    suffix='/'+known[video] if video in known else ''
                    if suffix and path.endswith(suffix):roots.add(path[:-len(suffix)])
            offset+=len(items)
            if not items or offset>=data.get('TotalRecordCount',offset):break
        else:
            store.set('emby_library_status','Automatic matching reached its scan limit. Enter the Emby folder under Manual library path.')
            return
    if len(roots)==1:
        root=roots.pop();store.set('emby_root',root)
        store.set('emby_library_status','Automatically matched downloaded videos in: '+root)
    else:
        store.set('emby_library_status','No unique library match yet. Emby must index a ReelDock download first, or enter its folder under Manual library path. Matching will retry automatically.')



def api(store,path,method='GET',params=None,body=None):
    base=store.get('emby_url');key=store.get('emby_key')
    if not base or not key:raise ValueError('Configure the Emby server and API key first.')
    url=base+('' if base.lower().endswith('/emby') else '/emby')+'/'+path
    try:
        # LAN hosts are intentional; never forward the private key on redirects.
        with requests.request(method,url,params=params,json=body,headers={'X-Emby-Token':key},timeout=(5,15),allow_redirects=False,stream=True) as response:
            if response.status_code>=300:raise ValueError('Emby rejected the request. Check the server address, API key and permissions.')
            content=b''
            for chunk in response.iter_content(65536):
                content+=chunk
                if len(content)>5_000_000:raise ValueError('Emby response was too large.')
            return json.loads(content) if content else {}
    except (requests.RequestException,json.JSONDecodeError):
        raise ValueError('Could not read Emby. Check its address, connection and API key.') from None


def users(store):
    return [dict(id=item['Id'],name=item.get('Name','User')) for item in api(store,'Users')]


def queue_refresh(store,channel):
    with store.connect() as db:db.execute('INSERT OR REPLACE INTO emby_refresh VALUES (?,?)',(channel,time.time()))


def refresh(store,channel):
    # Refreshing item metadata does not reliably discover new season folders.
    # The library scan also works before the first channel has been indexed.
    queue_refresh(store,channel)
    process_refreshes(store)


def process_refreshes(store):
    with store.connect() as db:
        rows=db.execute('SELECT channel,updated FROM emby_refresh ORDER BY updated').fetchall()
    if not rows:return
    now=time.time()
    if now<float(store.get('emby_refresh_after','0')):
        if not store.get('emby_refresh_status').startswith('Library scan failed:'):
            store.set('emby_refresh_status','Library scan queued; nearby downloads share a scan (up to five minutes).')
        return
    # Apply the cooldown to errors too, so a rejected request cannot hammer Emby.
    store.set('emby_refresh_after',now+300)
    try:
        api(store,'Library/Refresh','POST')
    except ValueError as exc:
        store.set('emby_refresh_status','Library scan failed: '+str(exc)+' Retrying in five minutes.')
        from .event_log import record
        record(store,'Emby library scan failed','Emby did not accept the scan request. Retrying in five minutes.','Emby','failed')
        return
    with store.connect() as db:
        # A completion arriving during the request must remain queued.
        db.executemany('DELETE FROM emby_refresh WHERE channel=? AND updated=?',
                       [(r['channel'],r['updated']) for r in rows])
    store.set('emby_refresh_status','Emby accepted a library scan request at '+datetime.now(ZoneInfo('Europe/London')).strftime('%d.%m.%Y %H:%M')+'. New files appear when Emby finishes indexing.')
    store.set('emby_discover_after','0')
    from .event_log import record
    record(store,'Emby library scan requested','Emby accepted the scan request. Files appear when indexing finishes.','Emby')


def retry_watched(store, video_id):
    from .youtube_matching import retry_watched as retry
    return retry(store, video_id)


def match_watched(store, stop):
    from .youtube_matching import match_watched as match
    return match(store, stop)


def match_favourites(store, stop):
    """Match series by YouTube channel ID or exact library path, never by title."""
    if store.get('emby_favourites')!='true':return
    user=store.get('emby_user');server=store.get('emby_url');root=library_root(store)
    if not user:raise ValueError('Choose an Emby user.')
    credentials={'emby_url':server,'emby_key':store.get('emby_key')}
    class Connection:
        def get(self,key,default=''):return credentials.get(key,default)
    connection=Connection()
    scope=hashlib.sha256((server+'|'+user).encode()).hexdigest()
    with store.connect() as db:
        known={r[0] for r in db.execute('SELECT id FROM channels WHERE active=1')}
        paths={root+'/'+r['folder']:r['id'] for r in db.execute('SELECT * FROM media_channels') if root and r['id'] in known}
    matched={};offset=0
    while not stop.is_set():
        data=api(connection,'Users/'+quote(user,safe='')+'/Items',params={'Recursive':'true','IncludeItemTypes':'Series','Fields':'ProviderIds,Path','EnableUserData':'true','StartIndex':offset,'Limit':200})
        items=data.get('Items',[])
        for item in items:
            ids={str(v) for k,v in (item.get('ProviderIds') or {}).items() if k.lower()=='youtube'} & known
            path=paths.get(str(item.get('Path','')).replace(chr(92),'/').rstrip('/'))
            if path:ids.add(path)
            if len(ids)==1 and item.get('Id') and type((item.get('UserData') or {}).get('IsFavorite')) is bool:
                matched.setdefault(ids.pop(),[]).append(item)
        offset+=len(items)
        if not items or offset>=data.get('TotalRecordCount',offset):break
        if offset>=100000:raise ValueError('Emby favourites scan exceeded its item limit.')
    count=0
    for channel,items in matched.items():
        if stop.is_set() or store.get('emby_favourites')!='true' or store.get('emby_user')!=user or store.get('emby_url')!=server:return
        if len(items)!=1:continue  # Duplicate series require a unique match before writing.
        item=items[0];remote=item['UserData']['IsFavorite'];item_id=str(item['Id'])
        with store.connect() as db:
            local=bool(db.execute('SELECT 1 FROM channel_favourites WHERE id=?',(channel,)).fetchone())
            previous=db.execute('SELECT item,value FROM emby_favourites WHERE scope=? AND channel=?',(scope,channel)).fetchone()
        # First contact merges favourites; later changes use the shared baseline.
        target=(local or remote) if not previous or previous['item']!=item_id else (local if local!=bool(previous['value']) else remote)
        if target!=remote:
            result=api(connection,'Users/'+quote(user,safe='')+'/FavoriteItems/'+quote(item_id,safe=''),'POST' if target else 'DELETE')
            if result.get('IsFavorite') is not target:raise ValueError('Emby did not confirm the favourite change.')
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Preserve a local heart changed while the network request was running.
            current=bool(db.execute('SELECT 1 FROM channel_favourites WHERE id=?',(channel,)).fetchone())
            if store.get('emby_favourites')!='true' or store.get('emby_user')!=user or store.get('emby_url')!=server:return
            if current==local:
                if target:db.execute('INSERT OR IGNORE INTO channel_favourites VALUES (?)',(channel,))
                else:db.execute('DELETE FROM channel_favourites WHERE id=?',(channel,))
            db.execute('INSERT OR REPLACE INTO emby_favourites VALUES (?,?,?,?)',(scope,channel,item_id,int(target)))
        count+=1
    store.set('emby_favourites_status',f'Favourites checked for {count} matched channels. Channels must be indexed as series in Emby; unmatched or duplicate series are skipped.')


def loop(store,stop):
    with store.connect() as db:
        db.execute("UPDATE emby_watched SET state='unverified',message='Interrupted. Check YouTube before retrying.' WHERE state='sending'")
    if store.get('emby_cookie_check')=='checking':store.set('emby_cookie_check','queued')
    while not stop.is_set():
        paused = True
        with store.background_operation():
            if store.reset_pending():
                paused = True
            else:
                paused = False
                try:
                    if store.get('emby_cookie_check')=='queued':
                        from .watched_status import check_cookie_session
                        store.set('emby_cookie_check','checking')
                        store.set('emby_cookie_status','Checking the YouTube session from the NAS…')
                        try:
                            cookie_path=store.directory/'youtube-cookies.txt'
                            before=cookie_path.stat().st_mtime_ns if cookie_path.exists() else None
                            state,message=check_cookie_session(cookie_path)
                            after=cookie_path.stat().st_mtime_ns if cookie_path.exists() else None
                            if before!=after:
                                store.set('emby_cookie_check','queued')
                                continue
                            store.set('emby_cookie_status',message)
                            from .cookie_recovery import record, resume
                            record(store,state)
                            if state=='active':resume(store)
                        except Exception:store.set('emby_cookie_status','The cookie check could not finish. Check NAS connectivity and retry.')
                        finally:
                            if store.get('emby_cookie_check')!='queued':store.set('emby_cookie_check','complete')
                    from .m3u_emby import tick as m3u_library_tick
                    m3u_library_tick(store)
                    from .sync_all import tick as sync_all_tick
                    sync_all_tick(store)
                    if store.get('emby_url') and store.get('emby_key'):
                        if float(store.get('emby_discover_after','0'))<=time.time():
                            store.set('emby_discover_after',time.time()+600)
                            try:discover_library(store)
                            except ValueError:store.set('emby_library_status','Could not read Emby libraries. Automatic matching will retry; a manual folder can also be entered.')
                        process_refreshes(store)
                        if store.get('emby_favourites')=='true' and float(store.get('emby_favourites_after','0'))<=time.time():
                            store.set('emby_favourites_after',time.time()+60)
                            try:match_favourites(store,stop)
                            except Exception:store.set('emby_favourites_status','Favourites sync failed. Check the Emby connection and permissions; retrying in one minute.')
                    if store.get('youtube_sync_paused')!='true' and store.get('emby_watched')=='true' and float(store.get('emby_next_check','0'))<=time.time():
                        from .youtube_matching import tick
                        tick(store,stop)
                except Exception:
                    store.set('emby_status','Emby task could not finish. Check the server connection and library folder.')
        if paused:
            stop.wait(1)
            continue
        stop.wait(30)


def library_root(store):
    from .storage_layout import fixed
    return getattr(store, 'emby_media_root', '') if fixed(store) else store.get('emby_root')
