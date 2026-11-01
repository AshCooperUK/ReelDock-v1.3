"""Public catalogue identities and download choices; never expose upstream URLs."""
import json
import re
from urllib.parse import urlencode
from ..m3u_availability import AVAILABLE_SQL


def vod_rows(store, item=None):
    if item is not None and str(item.get('id') or '').startswith('vodgroup:'):
        from .vod_groups import rows
        return rows(store,item['id'])
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
        return [dict(r) for r in db.execute("""SELECT c.key,f.source,f.path,f.title,f.kind,f.group_name,s.name,
            t.info,t.data,t.year,i.data AS file_info FROM m3u_files f JOIN m3u_sources s ON s.id=f.source
            JOIN m3u_catalog_entries c ON c.source=f.source AND c.path=f.path
            JOIN m3u_catalog_titles t ON t.key=c.key
            LEFT JOIN m3u_file_info i ON i.source=f.source AND i.path=f.path
            WHERE """+AVAILABLE_SQL+"""
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
    from .vod_groups import browse
    return browse(store,media,query,offset,limit)


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
        match='identity' if str(item.get('id') or '').startswith('vodgroup:') else matches(row,item)
        if not match:continue
        group=groups.setdefault(row['source'],dict(id=row['source'],label=row['name'],type='vod',entries=[]))
        from .vod_groups import facts,decoded
        from ..media_duration import fields as duration_fields
        info=facts(row['kind'],row['title'],row.get('group_name',''),row['path'])
        file_info=decoded(row.get('file_info'));metadata=decoded(row.get('data'))
        duration=duration_fields(minutes=metadata.get('runtimeMinutes') if row['kind']=='movies' else None)
        duration.update(duration_fields(seconds=file_info.get('durationSeconds'),minutes=file_info.get('runtimeMinutes')))
        group['entries'].append(dict(source=row['source'],path=row['path'],title=row['title'],kind=row['kind'],match=match,
            season=info.get('season'),number=info.get('episode'),day=info.get('day') or '',
            quality=' · '.join(dict.fromkeys(re.findall(r'(?i)\b(?:2160p|1080p|720p|480p|4k|8k|HEVC|AV1|H[ .]?26[45]|x26[45]|HDR10?\+?|BluRay|WEB[ .-]?DL)\b',row['title']+' '+row['path']))),**duration))
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
