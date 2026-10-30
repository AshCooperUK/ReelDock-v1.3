"""Shared, exact-identity player-to-YouTube matching.

Legacy emby_watched storage is retained to preserve receipts and retry safety.
It now belongs to this service, not to the Emby connection UI.
"""
import hashlib
import json
import re
import time
from urllib.parse import quote

PLAYERS = ('emby', 'jellyfin', 'plex', 'kodi')


def source(store):
    return store.get('youtube_watched_source', 'emby')


def config(store):
    if source(store) == 'emby':
        return {key: store.get('emby_'+key) for key in ('url','key','user')}
    from .media_players import connection
    return connection(store, source(store))


def source_scope(store):
    player=source(store); value=config(store)
    identity=value.get('url','')+'|'+value.get('user','')
    if player!='emby':identity=player+'|'+identity+'|'+value.get('username','')+'|'+value.get('library','')
    return hashlib.sha256(identity.encode()).hexdigest()


def fingerprint(store):
    return (source(store), json.dumps(config(store),sort_keys=True), store.get('youtube_watched_generation'), store.get('player_'+source(store)+'_generation'))


def ready(store, player):
    from .media_players import connection
    if player=='emby':return bool(store.get('emby_url') and store.get('emby_key') and store.get('emby_user'))
    value=connection(store,player)
    return bool(value.get('url') and value.get('secret') and (player!='jellyfin' or value.get('user')))


def settings(store):
    from . import emby
    current=emby.settings(store)
    return dict(source=source(store),enabled=current['watched'],status=current['watched_status'],
                cookie_check=current['cookie_check'],attempts=current['attempts'],
                sources=[dict(id=p,ready=ready(store,p)) for p in PLAYERS])


def save(store, values):
    if not isinstance(values,dict) or not values or set(values)-{'source','enabled'}:raise ValueError('Choose supported watched matching preferences.')
    player=values.get('source',source(store));enabled=values.get('enabled',store.get('emby_watched')=='true')
    if player not in PLAYERS or type(enabled) is not bool:raise ValueError('Choose a source player and on or off.')
    if enabled and not ready(store,player):raise ValueError('Connect the source player and select its user or library in Media Players first.')
    if enabled and not (store.directory/'youtube-cookies.txt').exists():raise ValueError('Upload signed-in YouTube cookies before enabling watched matching.')
    with store.connect() as db:
        db.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)',{
            'youtube_watched_source':player,'emby_watched':str(enabled).lower(),
            'youtube_watched_generation':str(time.time_ns()),'emby_next_check':'0',
            'emby_watched_paused':'','emby_watched_status':'Matching queued.' if enabled else 'Watched matching is off.'}.items())


def items(store, checkpoint):
    from . import emby
    from .media_players.clients import Client
    player=source(store);value=config(store)
    if player!='emby':
        yield from Client(player,value).items(checkpoint)
        return
    class Connection:
        def get(self,key,default=''):return value.get(key.removeprefix('emby_'),default)
    offset=0
    while offset<100000:
        checkpoint()
        data=emby.api(Connection(),'Users/'+quote(value['user'],safe='')+'/Items',params={'Recursive':'true','IncludeItemTypes':'Movie,Episode,Video','IsPlayed':'true','Fields':'ProviderIds,Path','StartIndex':offset,'Limit':200})
        rows=data.get('Items',[])
        for item in rows:
            yield dict(watched=True,provider_ids=item.get('ProviderIds') or {},paths=[item.get('Path','')])
        offset+=len(rows)
        if not rows or offset>=data.get('TotalRecordCount',offset):return
    raise ValueError('The watched library exceeded its item limit.')


class Cancelled(Exception):pass


def tick(store,stop):
    store.set('emby_next_check',time.time()+300)
    try:
        count=match_watched(store,stop)
        if not count and not store.get('emby_watched_paused') and store.get('emby_watched')=='true':
            store.set('emby_watched_status','No downloaded YouTube videos match the source player’s watched items.')
    except Cancelled:pass
    except Exception:store.set('emby_watched_status','Watched matching could not finish. Check the selected player connection and permissions.')


def retry_watched(store,video_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}',str(video_id)):raise ValueError('Choose a valid watched request.')
    if store.get('emby_watched')!='true':raise ValueError('Enable and save watched matching before retrying.')
    scope=source_scope(store)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT state FROM emby_watched WHERE scope=? AND id=?',(scope,video_id)).fetchone()
        if not row or row[0] in ('sent','sending','retry'):raise ValueError('This request cannot be retried now.')
        db.execute("UPDATE emby_watched SET state='retry',message='Retry queued; the video must still be watched in the selected player.' WHERE scope=? AND id=?",(scope,video_id))
        db.execute('INSERT OR REPLACE INTO emby_attempts VALUES (?,?,?,?,0)',(scope,video_id,time.time(),0))
    store.set('emby_watched_paused','')
    store.set('emby_next_check','0')


def match_watched(store, stop):
    from .watched_status import send_watched_request
    if store.get('youtube_sync_paused')=='true' or store.get('emby_watched_paused'):return 0
    from .emby import library_root
    root=library_root(store)
    scope=source_scope(store);generation=fingerprint(store)
    def checkpoint():
        if stop.is_set() or store.get('emby_watched')!='true' or store.get('youtube_sync_paused')=='true' or fingerprint(store)!=generation:raise Cancelled()
    checkpoint()
    with store.connect() as db:
        files=[dict(r) for r in db.execute("SELECT m.id,m.path FROM media_files m JOIN downloads d ON d.id=m.id WHERE d.state='complete'")]
        previous=db.execute('''SELECT w.id,w.state,coalesce(a.attempts,1) AS attempts,coalesce(a.retry_after,0) AS retry_after
            FROM emby_watched w LEFT JOIN emby_attempts a ON a.scope=w.scope AND a.id=w.id WHERE w.scope=?''',(scope,)).fetchall()
        handled={r['id'] for r in previous if r['state']!='retry' and not
                 (r['state'] in ('auth_unverified','network_error','rate_limited') and r['attempts']<3 and r['retry_after']<=time.time())}
    with store.connect() as db:
        handled.update(r[0] for r in db.execute("SELECT id FROM emby_watched WHERE scope!=? AND state IN ('sent','sending','unverified')",(scope,)))
    known={r['id'] for r in files}
    paths={}
    def add_path(path, video):paths.setdefault(path,set()).add(video)
    if root:
        for r in files:add_path(root+'/'+r['path'].removeprefix('tv_shows/'),r['id'])
    # Exact full paths cover both subscriptions and manual YouTube downloads.
    base=getattr(store,'media_root','')
    if base:
        for r in files:add_path(base.rstrip('/')+'/'+r['path'],r['id'])
    matched=set()
    for item in items(store,checkpoint):
        checkpoint()
        if not item.get('watched'):continue
        ids={str(v) for k,v in item.get('provider_ids',{}).items() if k.lower()=='youtube'}
        for path in item.get('paths',[]):ids.update(paths.get(path.replace(chr(92),'/'),set()))
        if len(ids)==1 and ids <= known:matched.update(ids) # Conflicting identities must never mark either video.
    for video_id in sorted(matched-handled)[:20]:
        checkpoint()
        with store.connect() as db:
            db.execute("INSERT INTO emby_watched VALUES (?,?,'sending','Checking YouTube session and sending watched update.') ON CONFLICT(scope,id) DO UPDATE SET state='sending',message=excluded.message",(scope,video_id))
            db.execute('INSERT INTO emby_attempts VALUES (?,?,?,1,0) ON CONFLICT(scope,id) DO UPDATE SET updated=excluded.updated,attempts=attempts+1,retry_after=0',(scope,video_id,time.time()))
            attempts=db.execute('SELECT attempts FROM emby_attempts WHERE scope=? AND id=?',(scope,video_id)).fetchone()[0]
        try:state,message=send_watched_request(video_id,store.directory/'youtube-cookies.txt')
        except Exception:state,message='unverified','The watched check stopped unexpectedly. Completion is unknown; check YouTube history before retrying.'
        from .cookie_recovery import record
        record(store,state)
        safe_retry=state in ('auth_unverified','network_error','rate_limited')
        retry_after=time.time()+(3600 if state=='rate_limited' else 300*2**(attempts-1)) if safe_retry and attempts<3 else 0
        if safe_retry and attempts>=3:message+=' Watched matching paused after three unsuccessful session checks. Upload fresh cookies to resume automatically, or fix the connection and choose Check YouTube cookies.'
        with store.connect() as db:
            db.execute('UPDATE emby_watched SET state=?,message=? WHERE scope=? AND id=?',(state,message,scope,video_id))
            db.execute('UPDATE emby_attempts SET updated=?,retry_after=? WHERE scope=? AND id=?',(time.time(),retry_after,scope,video_id))
        checkpoint() # Keep the receipt, but never overwrite a new source's status.
        store.set('emby_watched_status',message)
        if state in ('auth_required','auth_expired') or (safe_retry and attempts>=3):store.set('emby_watched_paused','Paused: '+message+' Fix the cookies or connection, then choose Check watched status now.')
        if retry_after:store.set('emby_next_check',retry_after)
        if state!='sent':break
    return len(matched)
