"""Read-only parent media links from authoritative saved/provider identities.

No filename matching, provider requests, engine construction or state changes.
"""
import json
import re


def parent_item(value):
    if not isinstance(value,dict) or value.get('kind') not in ('movie','show'):
        return None
    identity=value.get('id')
    if not isinstance(identity,str):return None
    patterns={'movie':r'(movie|imdb:movie|tmdb:movie):([0-9]{1,12})',
              'show':r'(show|tvdb:show|tmdb:show|imdb:show|tvmaze:show):([0-9]{1,12})'}
    matched=re.fullmatch(patterns[value['kind']],identity)
    if not matched or int(matched[2])==0 or (matched[2].startswith('0') and not matched[1].startswith('imdb:')):return None
    from .discover_store import clean_item
    try:item=clean_item(value)
    except (ValueError,TypeError):return None
    # This field means TVDB/IMDb to acquisition. A TMDB ID must keep its own
    # namespace until the normal detail lookup establishes another provider ID.
    if matched[1].startswith('tmdb:') or matched[1] in ('imdb:show','tvmaze:show'):item.pop('externalId',None)
    elif matched[1] in ('movie','imdb:movie','show','tvdb:show') and item.get('externalId') and int(item['externalId'])!=int(matched[2]):
        return None
    allowed={'id','kind','title','image','backdrop','year','releaseDate','description','genre','rating','externalId','imdbId','provider','durationSeconds','runtimeMinutes'}
    return {key:content for key,content in item.items() if key in allowed}


def request_item(row):
    try:body=json.loads(row['body'] or '{}')
    except (ValueError,TypeError,KeyError):return None
    item=parent_item(body.get('item')) if isinstance(body,dict) else None
    if not item:return None
    expected='movie' if row['kind']=='movie' else 'show' if row['kind'] in ('episode','show') else None
    return item if item['kind']==expected else None


def tmdb_item(identity,kind,card):
    if type(identity) not in (int,str) or not re.fullmatch(r'[1-9][0-9]{0,11}',str(identity)):
        return None
    media='show' if kind=='tv' else 'movie'
    return parent_item(dict(id='tmdb:'+media+':'+str(identity),kind=media,title=card.get('title') or 'Untitled',
        year=card.get('year') or '',image=card.get('poster') or '',description=card.get('overview') or '',provider='TMDB'))


def native_items(store,engine,items):
    """Attach saved request parents only to that request's exact native job."""
    items=[dict(item) for item in items]
    if engine not in ('nzb','torrent') or not items:return items
    with store.connect() as db:
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'discover_requests' not in tables:return items
        for item in items:
            identity=item.get('id')
            if not isinstance(identity,str) or not identity:continue
            owners=set()
            for table in ('movie_download_policy','show_download_policy'):
                if table not in tables:continue
                policy=db.execute('SELECT body FROM '+table+' WHERE engine=? AND job=?',(engine,identity)).fetchone()
                try:body=json.loads(policy['body']) if policy else {}
                except (ValueError,TypeError):body={}
                if isinstance(body,dict) and isinstance(body.get('requestId'),str) and body['requestId']:owners.add(body['requestId'])
            if len(owners)>1:continue
            rows=db.execute('SELECT id,kind,body FROM discover_requests WHERE engine=? AND engine_id=? ORDER BY updated DESC,id',(engine,identity)).fetchall()
            if owners:rows=[row for row in rows if row['id'] in owners]
            found=[parent for row in rows if (parent:=request_item(row)) is not None]
            if found and len({parent['id'] for parent in found})==1:item['mediaItem']=found[0]
    return items


def lookup_item(store,engine,identity,result):
    parent=native_items(store,engine,[dict(id=identity)])[0].get('mediaItem')
    if not parent:return result
    if (result.get('mediaItem') or {}).get('id')==parent['id']:
        return dict(result,mediaItem=parent)
    # A filename search may identify another title. Keep presentation and
    # actions on the same saved parent without claiming refreshed metadata.
    from .media_duration import positive
    return dict(result,mediaItem=parent,matched=False,title=parent.get('title') or '',year=parent.get('year') or '',
        genres=[part.strip() for part in str(parent.get('genre') or '').split(',') if part.strip()],
        rating=positive(parent.get('rating'),10),overview=parent.get('description') or '',cast=[],poster=parent.get('image') or '',
        media_type='tv' if parent['kind']=='show' else 'movie',message='Showing saved programme details.')
