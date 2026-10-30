"""TV preferences around the existing series plans, episode policies and Guide."""
import json
import re
import time
from . import discover_store as ds


def initialise(store):
    with store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS show_options (
            key TEXT PRIMARY KEY, item TEXT NOT NULL, shown INTEGER NOT NULL DEFAULT 0,
            favourite INTEGER NOT NULL DEFAULT 0, quality TEXT, minimum INTEGER,
            checked REAL NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL)''')
        db.execute('CREATE TABLE IF NOT EXISTS show_download_policy(engine TEXT NOT NULL,job TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(engine,job))')


def clean(item):
    item=ds.clean_item(item)
    if item['kind']!='show':raise ValueError('Choose a TV show.')
    match=re.fullmatch(r'show:(\d{1,12})',item['id'])
    if match:
        if item.get('externalId') and int(item['externalId'])!=int(match[1]):raise ValueError('TV show identities do not agree.')
        item['externalId']=match[1]
    return item


def key_for(item):
    from .discover_title_releases import title_key
    item=clean(item);direct=title_key(item)
    # Documentary releases sharing an umbrella TVDB ID must keep separate options.
    return item['id']+':title:'+direct if direct else item['id']


def find(store,item):
    try:key=key_for(item)
    except (ValueError,TypeError):return None
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='show_options'").fetchone():return None
        row=db.execute('SELECT * FROM show_options WHERE key=?',(key,)).fetchone()
    return dict(row) if row else None


def ensure(store,item):
    item=clean(item);row=find(store,item)
    if row:return row
    now=time.time()
    # NULL quality means an untouched inherited policy; opening the panel must
    # not overwrite an existing season, series or individual episode policy.
    with store.connect() as db:
        db.execute('INSERT OR IGNORE INTO show_options(key,item,created,updated) VALUES(?,?,?,?)',
                   (key_for(item),json.dumps(item),now,now))
    return find(store,item)


def global_minimum(store):
    from .movie_options import global_minimum as read
    return read(store)


def series_policy(store,item):
    from .discover_title_releases import title_key
    if title_key(item):return None
    with store.connect() as db:row=db.execute('SELECT * FROM managed_series WHERE parent=?',(item['id'],)).fetchone()
    return dict(row) if row else None


def policy(store,item):
    row=find(store,item)
    if not row:return None
    from .discovery_engine import preferences
    defaults=preferences(store);legacy=series_policy(store,item)
    return dict(kind='episode',minimum=global_minimum(store) if row['minimum'] is None else row['minimum'],
                quality=legacy['quality'] if row['quality'] is None and legacy else defaults['preferred_quality'] if row['quality'] in (None,'default') else row['quality'],
                fallback=bool(legacy['fallback']) if legacy else defaults['allow_lower_quality'])


def snapshot(store,row):
    from .discover_title_releases import title_key
    item=json.loads(row['item']);legacy=series_policy(store,item)
    with store.connect() as db:
        plan=db.execute('SELECT * FROM discover_series_plan WHERE parent=?',(item['id'],)).fetchone()
        numbered=db.execute("SELECT 1 FROM managed_media WHERE parent=? AND removed=0 AND kind='episode' AND json_extract(body,'$.automatic')=1 AND json_type(body,'$.episode.number')='integer' LIMIT 1",(item['id'],)).fetchone()
    supported=bool(numbered and not title_key(item))
    return dict(key=row['key'],item=item,shown=bool(row['shown']),favourite=bool(row['favourite']),
                downloadAll=bool(not title_key(item) and plan and plan['download_all']),downloadFuture=bool(not title_key(item) and plan and plan['download_future']),
                automaticSupported=supported,automaticMessage='' if supported else 'Automatic matching is unavailable. Keep using the episode or saved release picker.',
                quality=row['quality'] or (legacy['quality'] if legacy else 'default'),minimum=row['minimum'],globalMinimum=global_minimum(store))


def save(app,store,item,patch):
    if not isinstance(patch,dict) or not patch or len(patch)!=1 or set(patch)-{'shown','favourite','downloadAll','downloadFuture','quality','minimum'}:
        raise ValueError('Choose a valid TV show option.')
    field,value=next(iter(patch.items()))
    if field in ('shown','favourite','downloadAll','downloadFuture') and type(value) is not bool:raise ValueError('Choose on or off.')
    if field=='quality' and value not in ('default','720','1080','2160'):raise ValueError('Choose a supported TV show quality.')
    if field=='minimum' and value is not None and (type(value) is not int or not 0<=value<=1440):raise ValueError('Choose a minimum length from 0 to 1440 minutes.')
    row=ensure(store,item);item=json.loads(row['item'])
    if field in ('downloadAll','downloadFuture'):
        if value and not snapshot(store,row)['automaticSupported']:raise ValueError('Automatic matching is unavailable. Choose a release manually.')
        from .discovery_engine import save_plan
        save_plan(app,store,item['id'],'all' if field=='downloadAll' else 'future',value)
    else:
        with store.connect() as db:db.execute('UPDATE show_options SET '+field+'=?,updated=? WHERE key=?',(value,time.time(),row['key']))
    return find(store,item)


def minimum_for_request(store,request):
    if request.get('kind')!='episode':return None
    body=json.loads(request['body']) if isinstance(request['body'],str) else request['body']
    item=body.get('item')
    if not item:
        external=str(body.get('externalId') or '')
        if not external:return None
        item=dict(kind='show',id=body.get('itemId') or 'show:'+external,externalId=external,title=request.get('title',''))
    selected=policy(store,item)
    return selected['minimum'] if selected else None


def job_policy(store,engine,job):
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='show_download_policy'").fetchone():return None
        row=db.execute('SELECT body FROM show_download_policy WHERE engine=? AND job=?',(engine,job)).fetchone()
    return json.loads(row[0]) if row else None


def schedule_refresh(app,store,row):
    if not row['shown'] or row['checked']>time.time()-900:return
    from .discover_title_releases import title_key
    if title_key(json.loads(row['item'])):return
    from .discovery_engine import schedule,prepare
    def refresh():
        prepare(app,store,json.loads(row['item']))
        with store.connect() as db:db.execute('UPDATE show_options SET checked=? WHERE key=?',(time.time(),row['key']))
    schedule(app,store,'show-options:'+row['key'],refresh)


def guide(app,store,start,end):
    from .movie_options import valid_day
    with store.connect() as db:rows=db.execute('SELECT * FROM show_options WHERE shown=1 ORDER BY key').fetchall()
    result=[]
    for saved in rows:
        row=dict(saved);item=json.loads(row['item']);events=[]
        schedule_refresh(app,store,row)
        from .discover_title_releases import title_key
        if not title_key(item):
            with store.connect() as db:episodes=db.execute("SELECT key,body FROM managed_media WHERE parent=? AND removed=0 AND json_extract(body,'$.directTitleKey') IS NULL",(item['id'],)).fetchall()
            for episode in episodes:
                body=json.loads(episode['body']);day=valid_day(body.get('date'));e=body.get('episode') or {}
                if day and start<=day<=end:
                    code='S%02dE%02d'%(e['season'],e['number']) if type(e.get('season')) is int and type(e.get('number')) is int else ''
                    events.append(dict(id=episode['key'],date=day,label='Air date',title=' · '.join(filter(None,[code,e.get('name') or item['title']])),note='Air date; time not announced',image=e.get('image') or item.get('image',''),description=e.get('summary') or ''))
        result.append(dict(key=row['key'],item=item,favourite=bool(row['favourite']),events=events))
    return result
