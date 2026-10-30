"""Source adapters. Each receives only its selected source request."""
import json
import time
from .ordering import ordered
from .identity import movie_identity,unique_cards
from . import local
from ..media_duration import fields as duration_fields, positive, clock_seconds

PAGE = 24


def card(item, action):
    if action['type']=='discover':
        from ..discover_store import clean_item
        action=dict(action,item=clean_item(item))
    result = dict(id=movie_identity(item), title=item.get('title') or 'Untitled', kind=item.get('kind') or 'video',
                image=item.get('image') or '', backdrop=item.get('backdrop') or '', year=item.get('year') or '',
                description=item.get('description') or '', rating=item.get('rating') or '', date=item.get('published') or '',added=item.get('added'),
                available=bool(item.get('releases')),releaseCount=len(item.get('releases',[])),metadataVerified=bool(item.get('metadataVerified')),
                releaseDate=item.get('releaseDate') or '',lastEpisodeReleased=item.get('lastEpisodeReleased') or '',channel=item.get('channel') or '', channelId=item.get('channelId') or '', action=action)
    # Public DTOs retain their strict numeric contract. Import adapters parse
    # provider strings in their known units before reaching this boundary.
    seconds=item.get('durationSeconds');minutes=item.get('runtimeMinutes')
    result.update(duration_fields(seconds=seconds if type(seconds) in (int,float) else None,
        minutes=minutes if type(minutes) in (int,float) else None,clock=item.get('duration'),pending=item.get('metadata_pending')))
    return result


def discover(app, store, args):
    from .. import discover_sources as sources
    media, query, offset = args['media'], args['q'], args['offset']
    if query and len(query)<2:raise ValueError('Enter at least two characters to search.')
    start=(args.get('cursor') or {}).get('nzb',offset)
    try:indexers=sources.indexers_for(app,store)
    except ValueError:indexers=[]
    if not indexers and query:
        return dict(items=[],nextOffset=None,nextCursor=None,label='NZB',
                    warnings=['Add an enabled NZB indexer in ReelDock Settings to browse NZB releases.'])
    section=args.get('collection') or 'all'
    from .nzb_metadata import publication,movies
    def page(result):
        items=[publication(dict(item)) for item in result['items']]
        cards=unique_cards([card(item,dict(type='discover',item=item)) for item in items])
        following=result.get('nextOffset') if section not in ('','all') else offset+1 if result.get('nextOffset') is not None else None
        return dict(result,items=ordered(cards,'new' if args['sort']=='source' else args['sort']),
            nextOffset=following,
            nextCursor=result.get('nextCursor') if section not in ('','all') else json.dumps({'nzb':result['nextOffset']}) if following is not None else None,
            label=result.get('label') or ('View All Movies' if media=='movies' else 'View All TV Shows'),collection=section,
            freshness=dict(source=result.get('catalogueSource') or (('TMDB upcoming movies' if media=='movies' else 'TV premiere catalogue') if result.get('catalogueOnly') else 'NZB indexer APIs'),fetchedAt=result.get('fetchedAt'),
                scope=result.get('scope') if section not in ('','all') or result.get('catalogueSource') else f'{len(indexers)} enabled indexer(s). {len(cards)} titles from {result.get("releaseCount",0)} releases. Metadata sorts apply to loaded titles. API refreshes every five minutes.'))
    if section!='all':
        if media=='movies':from .movie_sections import browse
        else:from .show_sections import browse
        def partial(result):
            if args.get('_partial'):args['_partial'](page(result))
        from .indexer_catalogue import browse as indexer_first
        result=indexer_first(app,store,{**args,'collection':section,'_partial':partial},indexers,
                             lambda selected:browse(app,store,selected,indexers))
    else:
        from ..discover_search import search
        try:
            if not indexers:raise ValueError('No NZB indexers enabled.')
            result=search(app,store,media,query,start,browse=not query)
        except ValueError:
            if query:raise
            from .indexer_catalogue import all_fallback
            return page(all_fallback(app,store,args,start,indexers))
        if not query and result.get('warnings'):
            from .indexer_catalogue import all_fallback
            try:
                extra=all_fallback(app,store,args,start,indexers)
                result.update(items=[*result['items'],*extra['items']],catalogueSource='NZB indexer releases + TMDB fallback',
                              scope='Available NZB releases retained. Some indexers could not finish; TMDB catalogue fallback supplies additional titles. Download availability is checked separately.',
                              sourceIncomplete=True,sourceWarnings=result['warnings'])
            except ValueError:pass # Known release matches still remain usable.
        result=dict(result,items=[publication(dict(item)) for item in result['items']])
        if media=='shows':
            from .show_metadata import enrich
            from .show_sections import remember_available
            remember_available(store,indexers,result['items'])
            if args.get('_partial'):args['_partial'](page(result))
            # Optional dates/artwork must not hold up the release-page cursor.
            # Keep the legacy warning while exposing separate acquisition and
            # metadata status for the saved-catalogue worker.
            result.update(sourceWarnings=list(result.get('warnings',[])),sourceIncomplete=bool(result.get('incomplete')),
                sourceRetryAt=getattr(result.get('warnings',[]),'retry_at',0),metadataPending=[])
            failures=enrich(store,result['items'],lambda:args['_partial'](page(result)) if args.get('_partial') else None,
                on_failure=lambda item:result['metadataPending'].append(str(item.get('externalId') or '')))
            if failures:result.update(incomplete=True,retryAt=time.time()+60,warnings=[*result.get('warnings',[]),'Some TV metadata could not be checked. Known dates are shown; unknown dates sort last.'])
        if media=='movies' and not result.get('metadataComplete'):
            if args.get('_partial'):args['_partial'](page(result))
            movies(store,result['items'])
    return page(result)



def _subscription_duration(seconds,clock,seconds_type):
    # SQLite exposes JSON booleans as 0/1; keep the pure duration validator's
    # distinction without parsing each complete video document in Python.
    return (positive(seconds) if seconds_type not in ('true','false') else None) or clock_seconds(clock)


def subscriptions(app, store, args):
    from ..settings.youtube import DOWNLOAD_FIELDS
    minimum=DOWNLOAD_FIELDS['minimum_video_minutes'].read(store)
    minimum_seconds=minimum*60
    query='%'+args['q'].replace('!','!!').replace('%','!%').replace('_','!_')+'%'
    order={'new':'v.published DESC','oldest':'v.published ASC','title':"json_extract(v.data,'$.title') COLLATE NOCASE ASC",'title_desc':"json_extract(v.data,'$.title') COLLATE NOCASE DESC"}[args['sort']]
    with store.connect() as db:
        db.create_function('reeldock_video_duration',3,_subscription_duration,deterministic=True)
        rows=db.execute("""SELECT v.id,v.channel,v.published,v.data,c.data AS channel_data
            FROM videos v JOIN channels c ON c.id=v.channel WHERE c.active=1
            AND (? OR (json_extract(v.data,'$.kind') IS NOT 'shorts'
                AND (?=0 OR coalesce(reeldock_video_duration(json_extract(v.data,'$.duration_seconds'),
                    json_extract(v.data,'$.duration'),json_type(v.data,'$.duration_seconds')),?)>=?)))
            AND (json_extract(v.data,'$.title') LIKE ? ESCAPE '!' OR json_extract(c.data,'$.name') LIKE ? ESCAPE '!')
            ORDER BY """+order+",v.id LIMIT ? OFFSET ?",(bool(args.get('show_shorts')),minimum_seconds,minimum_seconds,minimum_seconds,query,query,PAGE+1,args['offset'])).fetchall()
    items=[]
    for row in rows[:PAGE]:
        video=json.loads(row['data']);channel=json.loads(row['channel_data'])
        item=dict(id='youtube:'+row['id'],title=video.get('title'),image=video.get('thumbnail'),kind='youtube',
                  channel=channel.get('name'),channelId=row['channel'],description=video.get('description'),published=row['published'],
                  **duration_fields(seconds=video.get('duration_seconds'),clock=video.get('duration'),pending=video.get('metadata_pending')))
        items.append(card(item,dict(type='youtube',id=row['id'])))
    return dict(items=items,nextOffset=args['offset']+PAGE if len(rows)>PAGE else None,label='Latest from your subscriptions',minimumVideoMinutes=minimum)


def vod(app, store, args):
    limit=args.get('_limit',PAGE)
    from .. import m3u_catalogue
    with store.connect() as db:
        sources=[dict(r) for r in db.execute('SELECT id,name FROM m3u_sources ORDER BY name COLLATE NOCASE')]
        source=args['collection'] or (sources[0]['id'] if sources else '')
        if source and source not in {s['id'] for s in sources}: raise ValueError('Choose an available VOD source.')
        kinds=('movies','series','tv') if args['q'] else ('movies',) if args['media']=='movies' else ('series','tv')
        query='%'+args['q'].replace('!','!!').replace('%','!%').replace('_','!_')+'%'
        field,direction={'new':('c.added','DESC'),'oldest':('c.added','ASC'),'title':('f.title COLLATE NOCASE','ASC'),'title_desc':('f.title COLLATE NOCASE','DESC'),'year':('NULLIF(t.year,0)','DESC'),'year_asc':('NULLIF(t.year,0)','ASC'),'rating':("CAST(NULLIF(json_extract(t.data,'$.rating'),'') AS REAL)",'DESC'),'release':("NULLIF(COALESCE(NULLIF(c.release_date,''),json_extract(t.data,'$.release_date')),'')",'DESC'),'release_asc':("NULLIF(COALESCE(NULLIF(c.release_date,''),json_extract(t.data,'$.release_date')),'')",'ASC'),'episode_added':('c.added','DESC'),'episode_added_asc':('c.added','ASC'),'rating_asc':("CAST(NULLIF(json_extract(t.data,'$.rating'),'') AS REAL)",'ASC')}[args['sort']]
        order=f'({field}) IS NULL,{field} {direction},f.path'
        sql="""SELECT f.source,f.path,f.title,f.kind,f.group_name,t.data AS metadata,c.key AS catalog_key
            FROM m3u_files f LEFT JOIN m3u_catalog_entries c ON c.source=f.source AND c.path=f.path
            LEFT JOIN m3u_catalog_titles t ON t.key=c.key WHERE f.source=? AND f.kind IN ("""+','.join('?' for _ in kinds)+") AND f.title LIKE ? ESCAPE '!' ORDER BY "+order
        params=(source,*kinds,query)
        if limit is not None:sql+=' LIMIT ? OFFSET ?';params+= (limit+1,args['offset'])
        rows=db.execute(sql,params).fetchall()
        entries=m3u_catalogue.decorate(db,rows if limit is None else rows[:limit])
    items=[]
    for entry in entries:
        item=dict(id=entry['source']+':'+entry['path'],title=entry['title'],kind='movie' if entry['kind']=='movies' else 'show',image=entry.get('artwork'),description=entry.get('description'),rating=entry.get('rating'),**duration_fields(seconds=entry.get('durationSeconds'),minutes=entry.get('runtimeMinutes')))
        items.append(card(item,dict(type='vod',source=source,item=entry)))
    return dict(items=items,nextOffset=args['offset']+limit if limit is not None and len(rows)>limit else None,label='Your VOD catalogue',collections=sources,collection=source)


def library(app, store, args):
    state=local.inventory(app.config['MEDIA_DIR'])
    from ..movie_options import observe_local
    observe_local(store,state['items'])
    items=[r for r in local.library_dates(store,state,app.config['MEDIA_DIR']) if (args['q'] or r['kind']==args['media']) and args['q'].casefold() in r['title'].casefold()]
    # Browsing collapses nested shows/YouTube folders (and multi-part movies)
    # into one parent card per title folder; search stays per file.
    if not args['q']:items=local.grouped(items,state,app.config['MEDIA_DIR'])
    items=ordered(items,args['sort'])
    offset=args['offset'];page=items[offset:offset+PAGE]
    return dict(items=page,nextOffset=offset+PAGE if offset+PAGE<len(items) else None,label='Recently saved' if args['sort']=='new' else 'Your local library',
                scanning=state['scanning'],warnings=[state['warning']] if state['warning'] else [])


REGISTRY = {
    'discover': dict(label='NZB',media=['movies','shows'],sorts=['source','new','oldest','title','title_desc','release','release_asc','year','year_asc','rating','rating_asc','episode_released','episode_released_asc'],browse=discover),
    'vod': dict(label='VOD',media=['movies','shows'],sorts=['new','oldest','title','title_desc','release','release_asc','year','year_asc','rating','rating_asc','episode_added','episode_added_asc'],browse=vod),
    'local': dict(label='Local Library',media=['movies','shows','youtube'],sorts=['new','oldest','title','title_desc','release','release_asc','year','year_asc','rating','rating_asc','episode_added','episode_added_asc'],browse=library),
    'subscriptions': dict(label='YouTube',media=['youtube'],sorts=['new','oldest','title','title_desc'],browse=subscriptions),
}
