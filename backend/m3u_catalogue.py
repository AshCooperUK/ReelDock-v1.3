"""Indexed M3U browsing metadata, shared between episodes and home shelves."""
from .metadata_control import Cancelled, checkpoint, vod_task
import hashlib
import json
import re
import time
from datetime import date
from urllib.parse import urlencode
from .m3u_metadata import TMDB
from .media_duration import fields as duration_fields

DATE_ATTRIBUTES = ('release-date', 'release_date', 'releasedate', 'premiered', 'first-air-date', 'air-date', 'air_date', 'date')

def normal_date(value):
    value = str(value or '').strip()[:10]
    try: return date.fromisoformat(value).isoformat()
    except ValueError: return ''

def initialise(store):
    with store.connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS m3u_catalog_entries(source TEXT,path TEXT,key TEXT,added REAL,release_date TEXT DEFAULT '',PRIMARY KEY(source,path));
        CREATE INDEX IF NOT EXISTS m3u_catalog_key ON m3u_catalog_entries(key);
        CREATE TABLE IF NOT EXISTS m3u_catalog_titles(key TEXT PRIMARY KEY,info TEXT,year INTEGER DEFAULT 0,priority INTEGER DEFAULT 0,checked REAL DEFAULT 0,data TEXT NOT NULL DEFAULT '{}');
        CREATE INDEX IF NOT EXISTS m3u_catalog_checked ON m3u_catalog_titles(checked);
        """)
    from .m3u_overrides import initialise as overrides_init
    overrides_init(store)

REVISION_KEY='m3u_catalogue_revision'
SHOW_IDENTITY='title-first'
SHELF_SECONDS=300
_shelf_cache={}


def identity_key(info, kind):
    media='movie' if kind=='movies' else 'tv'
    key_data=[media,info.get('provider'),info.get('identity')]
    if not info.get('identity') or info.get('provider')=='auto': key_data += [info.get('name','').casefold(),info.get('year')]
    return hashlib.sha256(json.dumps(key_data).encode()).hexdigest()


def entry_info(row, rebuild=False):
    from .m3u_naming import describe
    saved=json.loads(row['data'] or '{}');attrs=saved.get('attrs',{})
    saved_info=saved.get('info') or {}
    described=describe(row['title'],'0',row['group_name'],'',json.loads(row['options']),attrs)
    info=described if rebuild or not saved_info else saved_info
    if saved_info.get('identity') and saved_info.get('provider') not in ('', 'auto'):
        info=dict(info, provider=saved_info['provider'], identity=saved_info['identity'])
    return info, attrs, saved


def index(store):
    """Add new playlist files to the catalogue and drop removed ones.

    Runs in the worker (every minute and after each playlist import), not on
    every browsing request. Bumps the catalogue revision when rows change so
    cached Home shelves refresh straight away.
    """
    rebuild=store.get('m3u_show_identity')!=SHOW_IDENTITY
    with store.connect() as db:
        before=db.total_changes
        query="""SELECT f.*,i.data,s.last_run,s.options FROM m3u_files f
          JOIN m3u_sources s ON s.id=f.source LEFT JOIN m3u_file_info i ON i.source=f.source AND i.path=f.path"""
        if not rebuild: query+=" LEFT JOIN m3u_catalog_entries c ON c.source=f.source AND c.path=f.path WHERE c.path IS NULL"
        rows=db.execute(query).fetchall()
        entries=[];titles={};updates=[]
        for r in rows:
            info,attrs,saved=entry_info(r, rebuild)
            key=identity_key(info, r['kind'])
            release=next((normal_date(attrs.get(k)) for k in DATE_ATTRIBUTES if normal_date(attrs.get(k))), '')
            added=saved.get('added') or r['last_run'] or time.time()
            entries.append((r['source'],r['path'],key,added,release))
            if rebuild: updates.append((key, r['source'], r['path']))
            if r['kind'] in ('movies','series','tv'): titles[key]=(key,json.dumps(info),info.get('year') or 0,json.dumps(provider_summary(attrs,'movie' if r['kind']=='movies' else 'tv')))
        db.executemany('INSERT OR IGNORE INTO m3u_catalog_titles(key,info,year,data) VALUES(?,?,?,?)',titles.values())
        db.executemany('INSERT OR IGNORE INTO m3u_catalog_entries VALUES(?,?,?,?,?)',entries)
        if rebuild:
            db.executemany('UPDATE m3u_catalog_entries SET key=? WHERE source=? AND path=?', updates)
            db.executemany('UPDATE m3u_catalog_titles SET info=?,year=? WHERE key=? AND locked=0',
                           [(info_json, year, key) for key, info_json, year, _ in titles.values()])
            db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('m3u_show_identity',SHOW_IDENTITY))
        db.execute('DELETE FROM m3u_catalog_entries WHERE NOT EXISTS(SELECT 1 FROM m3u_files f WHERE f.source=m3u_catalog_entries.source AND f.path=m3u_catalog_entries.path)')
        db.execute('DELETE FROM m3u_catalog_titles WHERE NOT EXISTS(SELECT 1 FROM m3u_catalog_entries c WHERE c.key=m3u_catalog_titles.key)')
        from .m3u_overrides import reapply
        reapply(db)
        changed=db.total_changes!=before
        if changed:db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(REVISION_KEY,str(time.time_ns())))
    return changed


PROVIDER_FIELDS=('description','rating','genres','cast','release_date')

def provider_summary(attrs,kind):
    """Metadata supplied by an Xtream provider; used before TMDB fills any gaps."""
    if not any(attrs.get(k) for k in ('x-plot','x-rating','x-genre','x-cast')):return {}
    try:rating=round(float(attrs.get('x-rating') or ''),1)
    except ValueError:rating=None
    split=lambda v:[x.strip() for x in str(v or '').split(',') if x.strip()][:30]
    data=dict(provider=True,media_type=kind,description=attrs.get('x-plot',''),rating=rating if rating is None or 0<=rating<=10 else None,
              genres=split(attrs.get('x-genre')),cast=split(attrs.get('x-cast')),release_date=normal_date(attrs.get('release-date')))
    return {k:v for k,v in data.items() if v not in ('',None,[])}

def merge_provider(found,old):
    if not old.get('provider'):return found
    merged=dict(found)
    for key in PROVIDER_FIELDS:
        if old.get(key) not in (None,'',[]):merged[key]=old[key]
    merged['provider']=True;merged.setdefault('media_type',old.get('media_type'))
    return merged

def needs_index(db,source):
    """Cheap check for playlist files the worker has not catalogued yet."""
    files=db.execute('SELECT count(*) FROM m3u_files WHERE source=?',(source,)).fetchone()[0]
    entries=db.execute('SELECT count(*) FROM m3u_catalog_entries WHERE source=?',(source,)).fetchone()[0]
    return files>entries

def art(path):
    return '/api/m3u-artwork/'+path[1:] if isinstance(path,str) and re.fullmatch(r'/[A-Za-z0-9_-]+\.(?:jpg|png)',path) else ''

def resolve(client,info):
    kind='movie' if info.get('kind')=='movies' else 'tv'
    entity=client.entity(info) if info.get('identity') else {}
    if not entity.get('id') and not info.get('identity'):
        found=client.get('search/'+kind,query=info['name']).get('results',[])
        found=[x for x in found if (x.get('title') or x.get('name') or '').casefold()==info['name'].casefold() and (not info.get('year') or (x.get('release_date') or x.get('first_air_date') or '').startswith(str(info['year'])))]
        if len(found)==1:entity=found[0]
    if not entity.get('id'):return {}
    entity=client.get(f"{kind}/{entity['id']}",append_to_response='external_ids')
    if not entity.get('id'):return {}
    external=entity.get('external_ids') or {}
    external={k:v for k,v in dict(external,imdb_id=entity.get('imdb_id') or external.get('imdb_id')).items()
              if k in ('imdb_id','tvdb_id') and re.fullmatch(r'(?:tt)?[0-9]{1,12}',str(v or ''))}
    return dict(tmdb_id=entity['id'],external_ids=external,media_type=kind,title=entity.get('title') or entity.get('name'),
                release_date=normal_date(entity.get('release_date') or entity.get('first_air_date')),
                rating=entity.get('vote_average'),description=entity.get('overview',''),
                artwork=art(entity.get('backdrop_path') or entity.get('poster_path')),
                tmdb_url=f"https://www.themoviedb.org/{kind}/{entity['id']}",
                **({'runtimeMinutes':entity['runtime']} if kind=='movie' and type(entity.get('runtime')) is int and 0<entity['runtime']<=525600 else {}))

def enrich(store,kind='movies'):
    return vod_task(store, lambda: _enrich(store, kind))

def _enrich(store,kind='movies'):
    if not store.get('m3u_tmdb_token'):return False
    with store.connect() as db:
        row=db.execute("SELECT * FROM m3u_catalog_titles WHERE locked=0 AND checked<? ORDER BY priority DESC,CASE WHEN (json_extract(info,'$.kind')='movies')=(?='movies') THEN 0 ELSE 1 END,year DESC,CAST(json_extract(info,'$.identity') AS INTEGER) DESC,key LIMIT 1",(time.time()-7*86400,kind)).fetchone()
    if not row:return False
    from .activity import record
    info=json.loads(row['info']);name=info.get('name') or 'title'
    record(store,'m3u-catalogue','M3U metadata & artwork','running','Matching '+name+' with TMDB.')
    try:
        data=merge_provider(resolve(TMDB(store),info),json.loads(row['data'] or '{}'))
        checkpoint()
        # A user correction saved while TMDB was being read always wins.
        with store.connect() as db:db.execute('UPDATE m3u_catalog_titles SET data=?,checked=?,priority=0 WHERE key=? AND locked=0',(json.dumps(data),time.time(),row['key']))
    except Cancelled:
        raise
    except Exception:
        record(store,'m3u-catalogue','M3U metadata & artwork','error','TMDB matching could not finish. Retrying automatically.')
        raise
    record(store,'m3u-catalogue','M3U metadata & artwork','complete','Checked '+name+'.')
    return True

def loop(store,stop):
    indexed=0;movie=True
    while not stop.is_set():
        delay=1
        with store.background_operation():
            if not store.reset_pending():
                try:
                    if time.time()-indexed>60:index(store);indexed=time.time()
                    if not enrich(store,'movies' if movie else 'series'):delay=20
                    movie=not movie
                except Exception:
                    # Provider messages can contain secrets; never log the raw response.
                    delay=60
        stop.wait(delay)


def decorate(db,rows):
    result=[];keys=[]
    # One bounded database read per batch, never per-file/provider I/O. Episode
    # lengths belong to the individual playlist entry, not its series title.
    files={};identities=list({(row['source'],row['path']) for row in rows})
    for start in range(0,len(identities),400):
        batch=identities[start:start+400]
        for row in db.execute('SELECT source,path,data FROM m3u_file_info WHERE (source,path) IN (VALUES '+','.join('(?,?)' for _ in batch)+')',[value for identity in batch for value in identity]):
            try:
                value=json.loads(row['data'])
                if isinstance(value,dict):files[(row['source'],row['path'])]=value
            except (TypeError,ValueError):pass
    for row in rows:
        item=dict(row);data=json.loads(item.pop('metadata','{}') or '{}');key=item.pop('catalog_key',None)
        if data.get('locked') and data.get('title'):item['title']=data['title']
        item.update(artwork=data.get('artwork') or '/api/m3u/'+item['source']+'/artwork?'+urlencode({'path':item['path']}),rating=data.get('rating'),tmdb_url=data.get('tmdb_url'),description=data.get('description',''))
        file=files.get((item['source'],item['path']),{})
        item.update(duration_fields(minutes=data.get('runtimeMinutes') if item.get('kind')=='movies' else None))
        item.update(duration_fields(seconds=file.get('durationSeconds'),minutes=file.get('runtimeMinutes')))
        # Titles someone is browsing are matched with TMDB first.
        if key and (not data or set(data)<={'provider','media_type',*PROVIDER_FIELDS}):keys.append(key)
        result.append(item)
    if keys:db.execute('UPDATE m3u_catalog_titles SET priority=1 WHERE checked=0 AND priority=0 AND key IN ('+','.join('?' for _ in keys)+')',keys)
    return result

def shelves(store):
    with store.connect() as db:
        rows=db.execute("""SELECT f.source,f.path,f.title,f.kind,c.release_date AS playlist_release,t.data AS metadata,
            json_extract(t.data,'$.release_date') AS release_date,c.added AS episode_added,
            ROW_NUMBER() OVER(PARTITION BY json_extract(t.data,'$.media_type'),json_extract(t.data,'$.tmdb_id') ORDER BY c.added DESC,f.source,f.path) AS n
            FROM m3u_files f JOIN m3u_catalog_entries c ON c.source=f.source AND c.path=f.path
            JOIN m3u_catalog_titles t ON t.key=c.key WHERE json_extract(t.data,'$.tmdb_id') IS NOT NULL
            AND (f.kind!='movies' OR json_extract(t.data,'$.release_date')!='')
            ORDER BY CASE WHEN f.kind='movies' THEN json_extract(t.data,'$.release_date') END DESC,
            CASE WHEN f.kind!='movies' THEN c.added END DESC,f.title,f.source,f.path""").fetchall()
        result={'movies':[],'shows':[]}
        for row in rows:
            if row['n']!=1:continue
            group='movies' if row['kind']=='movies' else 'shows'
            if len(result[group])>=6:continue
            item=decorate(db,[row])[0];data=json.loads(row['metadata']);item['title']=data.get('title') or item['title'];result[group].append(item)
        result['pending']=db.execute('SELECT count(*) FROM m3u_catalog_titles WHERE checked=0').fetchone()[0]
        return result


def cached_shelves(store):
    """Home shelves, cached for a few minutes or until the catalogue changes.

    The shelf query walks the whole catalogue, while the Home page polls it.
    New TMDB matches therefore appear within SHELF_SECONDS; added or removed
    playlist files appear as soon as the worker indexes them. The pending
    count is always live.
    """
    revision=store.get(REVISION_KEY,'0');now=time.monotonic()
    hit=_shelf_cache.get(str(store.path))
    if hit and hit[0]==revision and hit[1]>now:
        result={k:list(v) for k,v in hit[2].items()}
    else:
        fresh=shelves(store)
        result={'movies':fresh['movies'],'shows':fresh['shows']}
        _shelf_cache[str(store.path)]=(revision,now+SHELF_SECONDS,{k:list(v) for k,v in result.items()})
    with store.connect() as db:
        result['pending']=db.execute('SELECT count(*) FROM m3u_catalog_titles WHERE checked=0').fetchone()[0]
    return result
