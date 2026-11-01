"""VOD work identities and grouped browsing; file choices remain explicit."""
import hashlib
import json
import re
from urllib.parse import quote,urlencode
from ..media_duration import fields as duration_fields
from ..m3u_availability import AVAILABLE_SQL


def decoded(raw):
    try:
        value=json.loads(raw or '{}')
        return value if isinstance(value,dict) else {}
    except (ValueError,TypeError):return {}


def facts(kind,title,group='',path=''):
    from ..m3u_naming import describe
    from ..settings.m3u import DEFAULTS
    attrs={'tvg-type':'movie' if kind=='movies' else 'series'}
    info=describe(title,'0',group,'',dict(DEFAULTS,cleaners=[]),attrs)
    # Imported paths sometimes retain the episode marker after a display-title
    # override. They are a fallback only, never an identity for another work.
    if info.get('season') is None:
        marker=re.search(r'(?i)(?:^|[^a-z0-9])S(\d{1,4})[ ._-]*E(\d{1,4})(?:[^0-9]|$)',path)
        if marker:info.update(season=int(marker[1]),episode=int(marker[2]))
    return info


def conflicting(info,data):
    provider=info.get('provider');supplied=str(info.get('identity') or '').removeprefix('tt').lstrip('0')
    linked=str(data.get('tmdb_id') if provider=='tmdb' else (data.get('external_ids') or {}).get(str(provider)+'_id') or '').removeprefix('tt').lstrip('0')
    return bool(supplied and linked and supplied!=linked)


def work_id(kind,title,group,path,source,key,info_raw,data_raw):
    info=decoded(info_raw);data=decoded(data_raw);media='movie' if kind=='movies' else 'show'
    provider=info.get('provider');identity=str(info.get('identity') or '')
    if conflicting(info,data):identity=['conflicting',media,key or [source,path]]
    elif data.get('tmdb_id'):identity=['tmdb',media,str(data['tmdb_id'])]
    elif provider in ('tmdb','imdb','tvdb') and identity:identity=[provider,media,identity.removeprefix('tt').lstrip('0')]
    elif key:identity=['catalogue',media,key]
    else:
        parsed=facts(kind,title,group,path)
        # Unknown movie years stay file-scoped: equal names alone cannot prove
        # that two films are the same work. Unindexed shows stay source-scoped.
        identity=['unindexed',media,source,parsed['name'].casefold(),parsed.get('year')]
        if media=='movie' and not parsed.get('year'):identity.append(path)
    return 'vodgroup:'+hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:40]


def register(db):
    db.create_function('reeldock_vod_work',8,work_id,deterministic=True)
    db.create_function('reeldock_vod_name',4,lambda kind,title,group,path:facts(kind,title,group,path)['name'],deterministic=True)
    db.create_function('reeldock_vod_year',4,lambda kind,title,group,path:facts(kind,title,group,path).get('year'),deterministic=True)


BASE="""SELECT f.source,f.path,f.title,f.kind,f.group_name,s.name,
    c.key,c.added,c.release_date AS playlist_release,t.info,t.data,t.year,i.data AS file_info,
    reeldock_vod_work(f.kind,f.title,f.group_name,f.path,f.source,c.key,t.info,t.data) AS work,
    reeldock_vod_work(f.kind,f.title,f.group_name,f.path,f.source,c.key,t.info,'{}') AS prior_work
    FROM m3u_files f JOIN m3u_sources s ON s.id=f.source
    LEFT JOIN m3u_catalog_entries c ON c.source=f.source AND c.path=f.path
    LEFT JOIN m3u_catalog_titles t ON t.key=c.key
    LEFT JOIN m3u_file_info i ON i.source=f.source AND i.path=f.path
    WHERE """+AVAILABLE_SQL+"""
    AND f.kind IN ('movies','series','tv')
    AND lower(f.path) NOT LIKE '%sample%' AND lower(f.title) NOT LIKE '%sample%'"""


def browse(store,media,query='',offset=0,limit=24,source='',sort='title'):
    if limit is not None and (type(limit) is not int or not 1<=limit<=1000):raise ValueError('Invalid catalogue batch.')
    pattern='%'+query.replace('!','!!').replace('%','!%').replace('_','!_')+'%'
    where='';params=[]
    if source:where+=' AND f.source=?';params.append(source)
    if not query:where+=' AND '+("f.kind='movies'" if media=='movies' else "f.kind IN ('series','tv')")
    elif media in ('movies','shows'):
        where+=' AND '+("f.kind='movies'" if media=='movies' else "f.kind IN ('series','tv')")
    # Match raw filenames and catalogue titles. Group first, so searching one
    # edition still opens all files/episodes of that work rather than hiding them.
    match="(title LIKE ? ESCAPE '!' OR path LIKE ? ESCAPE '!' OR coalesce(json_extract(data,'$.title'),json_extract(info,'$.name'),'') LIKE ? ESCAPE '!')"
    title="coalesce(nullif(json_extract(data,'$.title'),''),nullif(json_extract(info,'$.name'),''),reeldock_vod_name(kind,title,group_name,path))"
    year="coalesce(nullif(year,0),reeldock_vod_year(kind,title,group_name,path))"
    # Release sorting shares the catalogue's year fallback, without inventing
    # a release day in the card. Use text so ISO dates and integer years compare
    # chronologically instead of SQLite sorting them into separate type groups.
    release="coalesce(nullif(json_extract(data,'$.release_date'),''),nullif(playlist_release,''),CAST("+year+" AS TEXT))"
    field,direction={
        'title':(title+' COLLATE NOCASE','ASC'),'title_desc':(title+' COLLATE NOCASE','DESC'),'source':(title+' COLLATE NOCASE','ASC'),
        'new':('latest','DESC'),'oldest':('latest','ASC'),'episode_added':('latest','DESC'),'episode_added_asc':('latest','ASC'),
        'year':(year,'DESC'),'year_asc':(year,'ASC'),
        'rating':("CAST(nullif(json_extract(data,'$.rating'),'') AS REAL)",'DESC'),
        'rating_asc':("CAST(nullif(json_extract(data,'$.rating'),'') AS REAL)",'ASC'),
        'release':(release,'DESC'),'release_asc':(release,'ASC'),
    }[sort]
    sql=("WITH files AS ("+BASE+where+"), selected AS (SELECT work FROM files WHERE "+match+
        " GROUP BY work), ranked AS (SELECT files.*,count(*) OVER(PARTITION BY work) AS count,max(added) OVER(PARTITION BY work) AS latest,"
        "row_number() OVER(PARTITION BY work ORDER BY (coalesce(data,'{}')!='{}') DESC,source,path) AS n FROM files JOIN selected USING(work)), "
        "grouped AS (SELECT * FROM ranked WHERE n=1) SELECT * FROM grouped ORDER BY ("+field+") IS NULL,"+field+' '+direction+',work')
    params.extend((pattern,pattern,pattern))
    if limit is not None:sql+=' LIMIT ? OFFSET ?';params.extend((limit+1,offset))
    elif offset:sql+=' LIMIT -1 OFFSET ?';params.append(offset)
    with store.connect() as db:
        register(db)
        rows=db.execute(sql,params).fetchall()
    items=[]
    for row in rows if limit is None else rows[:limit]:
        data=decoded(row['data']);info=decoded(row['info']);parsed=facts(row['kind'],row['title'],row['group_name'],row['path'])
        conflict=conflicting(info,data)
        if conflict:data={} # Retain the file without attaching another work's facts/art.
        kind='movie' if row['kind']=='movies' else 'show'
        title=data.get('title') or info.get('name') or parsed['name']
        year=str(row['year'] or parsed.get('year') or '')
        external=str((data.get('external_ids') or {}).get('imdb_id' if kind=='movie' else 'tvdb_id') or '')
        if info.get('provider')==('imdb' if kind=='movie' else 'tvdb'):external=str(info.get('identity') or '')
        external=external.removeprefix('tt').lstrip('0')
        image=data.get('artwork') or '/api/m3u/'+quote(row['source'],safe='')+'/artwork?'+urlencode(dict(path=row['path']))
        collection_id=kind+':'+external if external.isdigit() else ('tmdb:'+kind+':'+str(data['tmdb_id']) if data.get('tmdb_id') else 'tmdb:'+kind+':'+str(info['identity']) if info.get('provider')=='tmdb' and info.get('identity') else 'vod:'+row['key'] if row['key'] else row['work'])
        item=dict(id=row['work'],kind=kind,title=title,year=year,provider='VOD',image=image,
            description=data.get('description') or '',rating=str(data.get('rating') or ''),
            externalId=external if external.isdigit() else '',imdbId=str(info.get('identity') or '') if info.get('provider')=='imdb' else '',
            releaseDate=data.get('release_date') or row['playlist_release'] or '',added=row['latest'],count=row['count'],_entry=dict(source=row['source'],path=row['path'],kind=row['kind']))
        if kind=='movie':
            file_info=decoded(row['file_info'])
            item.update(duration_fields(minutes=data.get('runtimeMinutes')))
            item.update(duration_fields(seconds=file_info.get('durationSeconds'),minutes=file_info.get('runtimeMinutes')))
        item['_collectionId']=None if conflict else collection_id
        if conflict:item.update(externalId='',imdbId='')
        items.append(item)
    return dict(items=items,nextOffset=offset+limit if limit is not None and len(rows)>limit else None)


def cards(page):
    from .adapters import card
    result=[]
    for item in page['items']:
        raw={key:value for key,value in item.items() if key not in ('count','added','_entry','_collectionId')}
        action=dict(type='vod-group',item=raw,target=dict(raw,id=item['_collectionId']) if item['_collectionId'] else None)
        result.append(dict(card(item,action),count=item['count'],_entry=item['_entry']))
    return result


def rows(store,identity):
    with store.connect() as db:
        register(db)
        # Metadata can arrive after a fixed search snapshot was displayed.
        # Resolve its catalogue/provider identity to the current verified work
        # before enumerating versions, without trusting browser-supplied paths.
        sql='WITH files AS ('+BASE+') SELECT * FROM files WHERE work IN (SELECT work FROM files WHERE work=? OR prior_work=?) ORDER BY name,path'
        return [dict(row) for row in db.execute(sql,(identity,identity))]
