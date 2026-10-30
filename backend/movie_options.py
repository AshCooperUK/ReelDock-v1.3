"""Per-movie preferences, known Guide dates and policies for existing acquisition."""
import json
import math
import re
import subprocess
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from . import discover_store as ds


def initialise(store):
    with store.connect() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS movie_options (
            key TEXT PRIMARY KEY, item TEXT NOT NULL, shown INTEGER NOT NULL DEFAULT 0,
            dates INTEGER NOT NULL DEFAULT 1, favourite INTEGER NOT NULL DEFAULT 0,
            automatic INTEGER NOT NULL DEFAULT 0, quality TEXT NOT NULL DEFAULT 'default',
            minimum INTEGER, release_date TEXT NOT NULL DEFAULT '', premiere_date TEXT NOT NULL DEFAULT '',
            found_at REAL NOT NULL DEFAULT 0, found_sources TEXT NOT NULL DEFAULT '[]',
            checked REAL NOT NULL DEFAULT 0, metadata_ok INTEGER NOT NULL DEFAULT 0, enabled_at REAL NOT NULL DEFAULT 0,
            created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS movie_download_policy (
            engine TEXT NOT NULL, job TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(engine,job));''')


def clean(item):
    item=ds.clean_item(item)
    if item['kind']!='movie':raise ValueError('Choose a movie.')
    match=re.fullmatch(r'(?:movie:|imdb:movie:)(\d{1,12})',item['id'])
    external=item.get('externalId') or item.get('imdbId','').removeprefix('tt')
    if match:
        if external and int(external)!=int(match[1]):raise ValueError('Movie identities do not agree.')
        external=match[1]
    if external:item['externalId']=external
    return item


def key_for(item):
    item=clean(item)
    return 'movie:'+str(int(item['externalId'])) if item.get('externalId') else item['id']


def find(store,item):
    try:key=key_for(item)
    except ValueError:return None  # Older request bodies may contain only a display title.
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='movie_options'").fetchone():return None
        row=db.execute("SELECT * FROM movie_options WHERE key=? OR json_extract(item,'$.id')=? ORDER BY key=? DESC LIMIT 1",(key,item['id'],key)).fetchone()
    return dict(row) if row else None


def managed(store,item):
    key=key_for(item)
    with store.connect() as db:
        # Explicit user state must outrank freshness while duplicate records
        # still exist; the reconcile pass removes them shortly after startup.
        row=db.execute("""SELECT * FROM managed_media WHERE kind='movie' AND (parent=? OR key=? OR key=?)
                       ORDER BY removed DESC,hold DESC,(monitor<>-1) DESC,updated ASC,key ASC LIMIT 1""",
                       (item['id'],key,'movie:'+item.get('externalId',''))).fetchone()
    return dict(row) if row else None


def global_minimum(store):
    try:config=json.loads((Path(store.directory)/'nzb-engine'/'settings.json').read_text())
    except FileNotFoundError:config={}
    value=config.get('min_video_minutes',0)
    if type(value) is not int or not 0<=value<=1440:raise ValueError('Could not read the global movie minimum length.')
    return value


def policy(store,item):
    row=find(store,item)
    if not row:return None
    from .discovery_engine import preferences
    defaults=preferences(store)
    return dict(minimum=global_minimum(store) if row['minimum'] is None else row['minimum'],
                quality=defaults['preferred_quality'] if row['quality']=='default' else row['quality'],
                fallback=defaults['allow_lower_quality'])


def ensure(store,item):
    item=clean(item);row=find(store,item)
    if row:return row
    existing=managed(store,item);enabled=bool(existing and existing['monitor']==1 and not existing['hold'] and not existing['removed'])
    now=time.time()
    with store.connect() as db:
        db.execute('INSERT OR IGNORE INTO movie_options(key,item,automatic,created,updated) VALUES(?,?,?,?,?)',
                   (key_for(item),json.dumps(item),int(enabled),now,now))
    return find(store,item)


def snapshot(store,row):
    item=json.loads(row['item']);record=managed(store,item)
    return dict(key=row['key'],item=item,shown=bool(row['shown']),dates=bool(row['dates']),favourite=bool(row['favourite']),
                automatic=bool(row['automatic'] and not (record and (record['hold'] or record['removed'] or record['monitor']==0))),
                quality=row['quality'],minimum=row['minimum'],globalMinimum=global_minimum(store))


def save(app,store,item,patch):
    if not isinstance(patch,dict) or not patch or set(patch)-{'shown','dates','favourite','automatic','quality','minimum'}:
        raise ValueError('Choose a valid movie option.')
    for field,value in patch.items():
        if field in ('shown','dates','favourite','automatic') and type(value) is not bool:raise ValueError('Choose on or off.')
        if field=='quality' and value not in ('default','720','1080','2160'):raise ValueError('Choose a supported movie quality.')
        if field=='minimum' and value is not None and (type(value) is not int or not 0<=value<=1440):raise ValueError('Choose a minimum length from 0 to 1440 minutes.')
    row=ensure(store,item);item=json.loads(row['item'])
    if patch.get('automatic') and not managed(store,item):
        from .media_management.store import remember
        key='movie:'+item['externalId'] if item.get('externalId') else item['id']
        remember(store,key,item['id'],'movie',dict(item=item,date=row['release_date'],dateLabel='Release date',automatic=True))
    record=managed(store,item);now=time.time();pending=[]
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        fields=','.join(field+'=?' for field in patch)
        db.execute('UPDATE movie_options SET '+fields+',updated=?'+(',enabled_at=?' if patch.get('automatic') else '')+' WHERE key=?',
                   (*patch.values(),now,*([now] if patch.get('automatic') else []),row['key']))
        if 'automatic' in patch and record:
            db.execute('UPDATE managed_media SET monitor=?,hold=CASE WHEN ? THEN 0 ELSE hold END,removed=CASE WHEN ? THEN 0 ELSE removed END,next_check=0 WHERE key=?',
                       (int(patch['automatic']),int(patch['automatic']),int(patch['automatic']),record['key']))
            if not patch['automatic']:
                pending=[r['id'] for r in db.execute("SELECT id FROM discover_requests WHERE episode_key=? AND json_extract(body,'$.movieAutomatic')=1 AND state IN ('waiting','searching','starting')",(record['key'],))]
    from .acquisition import cancel_discover
    for identity in pending:cancel_discover(app,store,identity)
    from .watchlist import sync_auto_titles
    sync_auto_titles(store)
    return find(store,item)


def observe(store,items,source,stamp=None):
    """Only server-observed availability creates a found event, never a chart/card date."""
    now=time.time();stamp=now if stamp is None else min(float(stamp),now)
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='movie_options'").fetchone():return
        for item in items:
            if item.get('kind')!='movie':continue
            try:key=key_for(item)
            except ValueError:continue
            row=db.execute("SELECT key,found_sources FROM movie_options WHERE key=? OR json_extract(item,'$.id')=? LIMIT 1",(key,item['id'])).fetchone()
            if not row:continue
            names=list(dict.fromkeys([*json.loads(row['found_sources']),source]))
            db.execute('UPDATE movie_options SET found_at=CASE WHEN found_at=0 THEN ? ELSE min(found_at,?) END,found_sources=? WHERE key=?',
                       (stamp,stamp,json.dumps(names),row['key']))


def known_sources(app,store,item):
    external=str(item.get('externalId') or '').lstrip('0')
    if external:
        with store.connect() as db:
            row=db.execute("SELECT min(created) FROM discover_candidates WHERE kind='movie' AND ltrim(external_id,'0')=?",(external,)).fetchone()
        if row and row[0]:observe(store,[item],'NZB',row[0])
    from .view_media.download_sources import vod_rows,matches
    if any(matches(row,item) for row in vod_rows(store,item)):observe(store,[item],'VOD')
    record=managed(store,item)
    if record:
        from .media_management.service import library_file
        if library_file(app,store,record):observe(store,[item],'Local library')
    from .view_media.local import inventory
    observe_local(store,inventory(app.config['MEDIA_DIR'])['items'])


def observe_local(store,items):
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='movie_options'").fetchone():return
        rows=db.execute('SELECT item FROM movie_options').fetchall()
    for row in rows:
        item=json.loads(row['item'])
        if item.get('year') and any(r.get('kind')=='movies' and r.get('year')==item['year'] and r.get('title','').casefold()==item['title'].casefold() for r in items):
            observe(store,[item],'Local library')


def refresh(app,store,item):
    from .view_media.movie_info import details
    row=find(store,item)
    if not row:return
    try:
        result=details(store,item['id'],item.get('externalId') or item.get('imdbId',''))
        dates=result.get('guideDates',{})
        with store.connect() as db:
            db.execute('UPDATE movie_options SET release_date=?,premiere_date=?,checked=?,metadata_ok=1 WHERE key=?',
                       (valid_day(dates.get('releaseDate')),valid_day(dates.get('premiereDate')),time.time(),row['key']))
    except ValueError:
        with store.connect() as db:db.execute('UPDATE movie_options SET checked=?,metadata_ok=0 WHERE key=?',(time.time(),row['key']))
    known_sources(app,store,item)


def valid_day(value):
    try:return date.fromisoformat(str(value)).isoformat()
    except ValueError:return ''


def schedule_refresh(app,store,row):
    if time.time()-row['checked']<(86400 if row['metadata_ok'] else 300):return
    from .discovery_engine import schedule
    schedule(app,store,'movie-options:'+row['key'],lambda:refresh(app,store,json.loads(row['item'])))


def guide(store,start,end):
    with store.connect() as db:rows=db.execute('SELECT * FROM movie_options WHERE shown=1 ORDER BY key').fetchall()
    result=[]
    for row in rows:
        item=json.loads(row['item']);events=[]
        if row['dates']:
            values=[('release',row['release_date'],'Release date'),('premiere',row['premiere_date'],'Premiere date')]
            if row['found_at']:
                values.append(('found',datetime.fromtimestamp(row['found_at'],ZoneInfo('Europe/London')).date().isoformat(),'Found in sources'))
            for kind,day,label in values:
                if day and start<=day<=end:
                    events.append(dict(id=row['key']+':'+kind,date=day,label=label,note=', '.join(json.loads(row['found_sources'])) if kind=='found' else 'Date only; time not announced'))
        result.append(dict(key=row['key'],item=item,favourite=bool(row['favourite']),events=events))
    return result


def check_automatic(app,store,record):
    """Called under the existing management worker lease, using the same request engine."""
    item=json.loads(record['body'])['item'];row=find(store,item)
    if not row or not snapshot(store,row)['automatic']:return
    from .media_management import service
    from . import discovery_engine as engine
    refresh(app,store,item)
    current=managed(store,item)
    if not current or not service.effective_monitor(store,current):return
    state=service.compose(app,store,current)
    if state['file'] or state['active'] or ds.open_request_for(store,current['key']):return
    # Only a verified metadata date is used to defer a request. An undated title
    # can still be found on a configured source; availability remains authoritative.
    latest=find(store,item);body=json.loads(current['body'])
    body['date']=latest['release_date'];body['item']['releaseDate']=latest['release_date']
    with store.connect() as db:db.execute('UPDATE managed_media SET body=? WHERE key=?',(json.dumps(body),current['key']))
    engine.queue(app,store,current['key'],automatic=True)


def minimum_for_request(store,request):
    if request.get('kind')!='movie':return None
    body=json.loads(request['body']) if isinstance(request['body'],str) else request['body']
    selected=policy(store,body.get('item') or dict(kind='movie',id='movie:'+body.get('externalId',''),externalId=body.get('externalId','')))
    return selected['minimum'] if selected else None


def verify_file(path,selected):
    """Check the staged file before publication; zero minimum still excludes samples elsewhere."""
    if not selected:return
    title='episode' if selected.get('kind')=='episode' else 'movie'
    try:
        result=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_type,width,height','-of','json',str(path)],capture_output=True,text=True,timeout=30,check=True)
        data=json.loads(result.stdout);seconds=float(data['format']['duration'])
        if not math.isfinite(seconds) or seconds<=0:raise ValueError()
        width=None
        if selected.get('quality'):
            stream=next(r for r in data.get('streams',[]) if r.get('codec_type')=='video')
            # Width handles the usual cinema crop in nominal 1080p/2160p releases.
            width=max(float(stream['width']),float(stream['height'])*16/9)
            if not math.isfinite(width) or width<=0:raise ValueError()
    except (OSError,subprocess.SubprocessError,KeyError,TypeError,ValueError,StopIteration) as exc:
        raise ValueError('Could not verify the '+title+' length and quality. Nothing was published.') from exc
    if seconds<selected['minimum']*60:raise ValueError('The '+title+' is below its minimum length. Nothing was published.')
    if width is not None:
        actual='2160' if width>=3000 else '1080' if width>=1600 else '720' if width>=1000 else '480'
        from .discover_logic import acceptable
        if not acceptable(actual,selected['quality'],selected['fallback']):raise ValueError('The '+title+' does not meet its quality setting. Nothing was published.')


def job_policy(store,engine,job):
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='movie_download_policy'").fetchone():return None
        row=db.execute('SELECT body FROM movie_download_policy WHERE engine=? AND job=?',(engine,job)).fetchone()
    return json.loads(row[0]) if row else None
