"""Global search over configured catalogues, retaining existing source actions."""
import json
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timezone
from .adapters import card,REGISTRY
from .download_sources import catalogue
from .identity import movie_identity,valid_card
from .. import discover_sources as sources

STEPS={'nzb_movies':100,'nzb_shows':100,'vod_movies':24,'vod_shows':24,'local':24,'youtube':20}


def merge(items):
    result={}
    for item in items:
        if not valid_card(item):continue
        key=movie_identity(item['action']['item']) if item['action']['type']=='discover' and item['kind']=='movie' else item['id']
        old=result.get(key)
        if old:
            # NZB identity drives one movie popup; it also offers matching VOD files.
            chosen=item if item.get('origin')=='NZB' and old.get('origin')!='NZB' else old
            origins=list(dict.fromkeys([*old.get('origins',[old.get('origin','')]),*item.get('origins',[item.get('origin','')])]))
            result[key]=dict(chosen,origins=origins)
        else:result[key]=item
    return list(result.values())


def browse(app,store,args):
    query=args['q'];cursor=args.get('cursor');tasks={};batches={};warnings=[];following={};failed=False;scanning=False
    def nzb(media,offset):
        return REGISTRY['discover']['browse'](app,store,dict(args,media=media,collection='all' if media=='movies' else '',offset=0,cursor={'nzb':offset},sort='source',_partial=None))
    def vod(media,offset):
        from .vod_groups import cards
        page=catalogue(store,media,query,offset)
        return dict(page,items=cards(page))

    def youtube(offset):
        from ..youtube_discover import browse
        page=browse(store,query=query,offset=offset,search_dates=True);cards=[]
        for item in page['items']:
            video=item.get('videoId') or item['id'].split(':')[-1]
            row=card(item,dict(type='youtube',id=video));row['id']='youtube:'+video
            row['releaseDate']=str(item.get('published') or '')[:10];row['dateApproximate']=bool(item.get('publishedApproximate'));cards.append(row)
        # Exact dates already indexed locally take precedence without per-video HTTP calls.
        if cards:
            with store.connect() as db:
                known={record['id']:record['published'] for record in db.execute('SELECT id,published,data FROM videos WHERE id IN ('+','.join('?' for _ in cards)+')',[row['action']['id'] for row in cards]) if not json.loads(record['data'] or '{}').get('publishedApproximate')}
            for row in cards:
                published=str(known.get(row['action']['id']) or '')
                if published:
                    row.update(date=published,releaseDate=published[:10],dateApproximate=False)
        return dict(items=cards,nextOffset=page.get('nextOffset'))
    def local(offset):
        page=REGISTRY['local']['browse'](app,store,dict(args,media='movies',offset=offset,sort='title'))
        page['items']=[dict(item,kind={'movies':'movie','shows':'show','youtube':'video'}.get(item['kind'],item['kind'])) for item in page['items']]
        return page
    try:nzb_enabled=bool(sources.indexers_for(app,store))
    except ValueError:nzb_enabled=False
    with store.connect() as db:
        from ..m3u_availability import AVAILABLE_SQL
        vod_enabled=bool(db.execute("SELECT 1 FROM m3u_sources s WHERE "+AVAILABLE_SQL+" LIMIT 1").fetchone())
    lanes={'local':('Local Library',local),'youtube':('YouTube',youtube)}
    if nzb_enabled:lanes.update(nzb_movies=('NZB',lambda offset:nzb('movies',offset)),nzb_shows=('NZB',lambda offset:nzb('shows',offset)))
    if vod_enabled:lanes.update(vod_movies=('VOD',lambda offset:vod('movies',offset)),vod_shows=('VOD',lambda offset:vod('shows',offset)))
    def snapshot():
        # Completion timing must not decide which equal-ranked card/action wins.
        items=[item for lane in STEPS for item in batches.get(lane,[])]
        return dict(items=merge(items),nextOffset=args['offset']+1 if following else None,
            nextCursor=json.dumps(following) if following else None,label='Search results',collection='',
            warnings=list(dict.fromkeys(warnings)),scanning=scanning,incomplete=failed or scanning,retryAt=datetime.now().timestamp()+(2 if scanning else 60) if failed or scanning else None,
            freshness=dict(source='Global search',fetchedAt=datetime.now(timezone.utc).isoformat(),scope='Search across NZB, VOD, Local Library and YouTube.'))
    with ThreadPoolExecutor(max_workers=4) as pool:
        for lane,(origin,loader) in lanes.items():
            if cursor is None or lane in cursor:tasks[pool.submit(loader,0 if cursor is None else cursor[lane])]=(lane,origin)
        for future in as_completed(tasks):
            lane,origin=tasks[future]
            try:
                page=future.result()
                batches[lane]=[dict(item,origin=origin) for item in page['items']]
                warnings.extend(page.get('warnings',[]));scanning|=bool(page.get('scanning'))
                failed|=bool(page.get('incomplete'))
                next_offset=page.get('nextOffset')
                if lane.startswith('nzb_') and page.get('nextCursor'):next_offset=json.loads(page['nextCursor'])['nzb']
                if next_offset is not None:following[lane]=next_offset
            except Exception as exc:
                # Exceptions may carry provider URLs/tokens; only fixed labels leave here.
                from ..discover_indexer_rate import limited_indexers,limit_message
                names=limited_indexers(exc)
                warnings.append(limit_message(names)+' Retrying automatically.' if names else origin+' search could not finish. Retrying automatically.');failed=True
            if args.get('_partial') and batches:args['_partial'](snapshot())
    return snapshot()
