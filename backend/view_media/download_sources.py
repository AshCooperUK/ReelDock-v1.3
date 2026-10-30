"""Public catalogue identities and download choices; never expose upstream URLs."""
import json
import re
from urllib.parse import urlencode


def vod_rows(store, item=None):
    where="";params=[]
    if item is not None:
        # Restrict in SQLite before materialising files from large playlists.
        key=item.get('id',''); parts=key.split(':')
        tmdb=parts[-1] if key.startswith('tmdb:') else ''
        external=str(item.get('externalId') or '').removeprefix('tt').lstrip('0')
        imdb=str(item.get('imdbId') or '').removeprefix('tt').lstrip('0')
        where=""" AND (c.key=? OR (?!='' AND CAST(json_extract(t.data,'$.tmdb_id') AS TEXT)=?)
          OR (?!='' AND json_extract(t.info,'$.provider')='tmdb' AND json_extract(t.info,'$.identity')=?)
          OR (?!='' AND json_extract(t.info,'$.provider')=? AND ltrim(replace(json_extract(t.info,'$.identity'),'tt',''),'0')=?)
          OR (?!='' AND json_extract(t.info,'$.provider')='imdb' AND ltrim(replace(json_extract(t.info,'$.identity'),'tt',''),'0')=?)
          OR (?!='' AND ltrim(replace(CAST(json_extract(t.data,?) AS TEXT),'tt',''),'0')=?)
          OR (lower(coalesce(json_extract(t.data,'$.title'),json_extract(t.info,'$.name')))=lower(?) AND t.year=?))
          AND f.kind """ + ("='movies'" if item.get('kind')=='movie' else "IN ('series','tv')")
        params=[key.removeprefix('vod:'),tmdb,tmdb,tmdb,tmdb,external,'imdb' if item.get('kind')=='movie' else 'tvdb',external,imdb,imdb,external,'$.external_ids.'+('imdb_id' if item.get('kind')=='movie' else 'tvdb_id'),external,item.get('title',''),item.get('year','')]
    with store.connect() as db:
        return [dict(r) for r in db.execute("""SELECT c.key,f.source,f.path,f.title,f.kind,s.name,
            t.info,t.data,t.year FROM m3u_files f JOIN m3u_sources s ON s.id=f.source
            JOIN m3u_catalog_entries c ON c.source=f.source AND c.path=f.path
            JOIN m3u_catalog_titles t ON t.key=c.key
            WHERE coalesce(json_extract(s.options,'$.enabled'),1)=1
            AND f.kind IN ('movies','series','tv')
            AND lower(f.path) NOT LIKE '%sample%' AND lower(f.title) NOT LIKE '%sample%'
            """+where+" ORDER BY s.name,f.path",params)]


def identity(row):
    data=json.loads(row['data'] or '{}');info=json.loads(row['info'] or '{}')
    kind='movie' if row['kind']=='movies' else 'show'
    if data.get('tmdb_id'):return f"tmdb:{kind}:{data['tmdb_id']}"
    provider=info.get('provider');key=str(info.get('identity') or '')
    if provider=='tmdb' and key:return f'tmdb:{kind}:{key}'
    if provider in ('imdb','tvdb') and key:return f'{provider}:{kind}:{key.removeprefix("tt").lstrip("0")}'
    return 'vod:'+row['key']


def catalogue(store,media,query='',offset=0,limit=24,include_entry=False):
    if type(limit) is not int or not 1<=limit<=1000:raise ValueError('Invalid catalogue batch.')
    kind='movies' if media=='movies' else 'series'
    pattern='%'+query.replace('!','!!').replace('%','!%').replace('_','!_')+'%'
    with store.connect() as db:
        rows=db.execute("""SELECT t.*,min(f.source) AS source,min(f.path) AS path,
          min(f.title) AS file_title,min(f.kind) AS kind,min(f.source||char(0)||f.path) AS chosen
          FROM m3u_catalog_titles t JOIN m3u_catalog_entries c ON c.key=t.key
          JOIN m3u_files f ON f.source=c.source AND f.path=c.path
          JOIN m3u_sources s ON s.id=f.source
          WHERE coalesce(json_extract(s.options,'$.enabled'),1)=1
          AND (f.kind=? OR (?='series' AND f.kind='tv'))
          AND coalesce(json_extract(t.data,'$.title'),json_extract(t.info,'$.name'),f.title) LIKE ? ESCAPE '!'
          AND lower(f.path) NOT LIKE '%sample%' AND lower(f.title) NOT LIKE '%sample%'
          GROUP BY t.key ORDER BY coalesce(json_extract(t.data,'$.title'),json_extract(t.info,'$.name'),f.title) COLLATE NOCASE,t.key
          LIMIT ? OFFSET ?""",(kind,kind,pattern,limit+1,offset)).fetchall()
    items=[]
    for raw in rows[:limit]:
        row=dict(raw);data=json.loads(row['data'] or '{}');info=json.loads(row['info'] or '{}')
        media_kind='movie' if row['kind']=='movies' else 'show'
        linked=str((data.get('external_ids') or {}).get('imdb_id' if media_kind=='movie' else 'tvdb_id') or '').removeprefix('tt').lstrip('0')
        item=dict(id=identity(row),kind=media_kind,title=data.get('title') or info.get('name') or row['file_title'],
            year=str(row['year'] or ''),provider='VOD',image=data.get('artwork') or '',
            description=data.get('description') or '',rating=str(data.get('rating') or ''),
            imdbId=str(info.get('identity') or '') if info.get('provider')=='imdb' else '',
            externalId=str(info.get('identity') or '').removeprefix('tt') if info.get('provider')==('imdb' if media_kind=='movie' else 'tvdb') else linked if linked.isdigit() else '')
        if include_entry:
            source,path=row['chosen'].split('\0',1);item['_entry']=dict(source=source,path=path,kind=row['kind'])
        items.append(item)
    return dict(items=items,nextOffset=offset+limit if len(rows)>limit else None)


def matches(row,item):
    info=json.loads(row['info'] or '{}');data=json.loads(row['data'] or '{}')
    kind='movie' if row['kind']=='movies' else 'show'
    if kind!=item.get('kind'):return ''
    provider='imdb' if kind=='movie' else 'tvdb'
    external=str(item.get('externalId') or '').removeprefix('tt').lstrip('0')
    supplied=str(info.get('identity') or '').removeprefix('tt').lstrip('0')
    imdb=str(item.get('imdbId') or '').removeprefix('tt').lstrip('0')
    linked=str((data.get('external_ids') or {}).get(provider+'_id') or '').removeprefix('tt').lstrip('0')
    if supplied and ((info.get('provider')==provider and external and supplied!=external) or
                     (info.get('provider')=='imdb' and imdb and supplied!=imdb)):return ''
    if linked and external:
        if linked!=external:return ''
        return 'identity'
    if identity(row)==item.get('id'):return 'identity'
    if supplied and ((info.get('provider')==provider and supplied==external) or (info.get('provider')=='imdb' and supplied==imdb)):return 'identity'
    if supplied and ((info.get('provider')==provider and external) or (info.get('provider')=='imdb' and imdb)):return ''
    title=data.get('title') or info.get('name') or ''
    if title.casefold()==str(item.get('title') or '').casefold() and row['year'] and str(row['year'])==str(item.get('year')):return 'title'
    return ''


def choices(app,store,item):
    from ..discover_sources import indexers_for
    groups={}
    for row in vod_rows(store,item):
        match=matches(row,item)
        if not match:continue
        group=groups.setdefault(row['source'],dict(id=row['source'],label=row['name'],type='vod',entries=[]))
        marker=re.search(r'(?i)S(\d+)E(\d+)',row['path'])
        group['entries'].append(dict(source=row['source'],path=row['path'],title=row['title'],match=match,
            season=int(marker[1]) if marker else None,number=int(marker[2]) if marker else None))
    sources=list(groups.values())
    try: available=bool(indexers_for(app,store))
    except ValueError:available=False
    if available and item.get('externalId'):
        sources.insert(0,dict(id='nzb',label='NZB · search active indexers',type='nzb',entries=[]))
    if available and not item.get('externalId') and re.fullmatch(r'tvmaze:show:\d{1,12}',item.get('id','')):
        sources.insert(0,dict(id='nzb-manual',label='NZB · choose episode releases manually',type='manual',entries=[]))
    return dict(sources=sources)


def channel_item(app,store,item):
    """Resolve chart videos that do not carry a channel ID before subscribing."""
    video=str(item.get('videoId') or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}',video):raise ValueError('Choose a valid YouTube video.')
    channel=str(item.get('channelId') or '')
    name=item.get('channel') or ''
    if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}',channel):
        from ..catalogue import Importer
        result=Importer(app).api('videos',part='snippet',id=video)
        rows=result.get('items') or []
        row=next((r for r in rows if r.get('id')==video),{})
        snippet=row.get('snippet') or {}
        channel=snippet.get('channelId') or '';name=snippet.get('channelTitle') or ''
    if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}',channel):raise ValueError('The channel could not be resolved. Try again when YouTube is available.')
    return dict(kind='channel',channelId=channel,title=name or 'YouTube',channel=name)
