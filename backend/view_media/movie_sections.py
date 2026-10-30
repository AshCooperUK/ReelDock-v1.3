"""Fill movie batches with exact IMDb NZB matches, preserving retryable failures."""
import hashlib
import json
import logging
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from .providers import TMDB, initialise, movie_card
from .cache_lock import refresh_lock
from .. import discover_sources as sources
from ..discover_indexer_rate import limited_indexers,limit_message

SECTIONS = [('premiering','Premiering Movies'),('new','New Movies'),('trending','Trending Movies'),('top','Top Rated Movies'),('all','View All Movies')]
SORTS = [('title','A-Z'),('title_desc','Z-A'),('release','Latest'),('release_asc','Oldest'),('source','NZB Order'),('year','Year'),('rating','Rating')]
BATCH_TARGET=40
MAX_PAGES=5


def availability_key(imdb,account):
    return 'nzb:movie-availability:'+account+':'+str(int(imdb)).zfill(7)


def saved_availability(store,key):
    with store.connect() as db:return db.execute('SELECT body,updated FROM view_media_cache WHERE key=?',(key,)).fetchone()


def availability(app,store,imdb,account,compatible=()):
    key=availability_key(imdb,account)
    row=saved_availability(store,key)
    if row and row['updated']>time.time()-(3600 if json.loads(row['body']) else 300):return json.loads(row['body'])
    # Adding an indexer cannot invalidate a positive match from unchanged accounts.
    for previous in compatible:
        saved=saved_availability(store,availability_key(imdb,previous))
        if saved and saved['updated']>time.time()-3600 and json.loads(saved['body']):
            with store.connect() as db:db.execute('INSERT OR REPLACE INTO view_media_cache VALUES(?,?,?)',(key,saved['body'],saved['updated']))
            return json.loads(saved['body'])
    with refresh_lock(store,key):
        row=saved_availability(store,key)
        if row and row['updated']>time.time()-(3600 if json.loads(row['body']) else 300):return json.loads(row['body'])
        releases=sources.movie_availability(app,store,imdb)
        releases=[r for r in releases if 'sample' not in str(r.get('title') or '').casefold()]
        with store.connect() as db:db.execute('INSERT OR REPLACE INTO view_media_cache VALUES(?,?,?)',(key,json.dumps(releases),time.time()))
    return releases


def browse(app,store,args,indexers):
    initialise(store)
    section=args['collection']
    start=(args.get('cursor') or {}).get('movies',args['offset']*20)
    if section=='premiering':return upcoming(store,args,start)
    path={'all':'discover/movie','new':'discover/movie','trending':'trending/movie/week','top':'movie/top_rated'}[section]
    catalogue_only=bool(args.get('_catalogue_fallback'))
    today=datetime.now(ZoneInfo('Europe/London')).date();earliest=today-timedelta(days=90)
    account=hashlib.sha256(json.dumps(indexers,sort_keys=True).encode()).hexdigest()
    compatible=[hashlib.sha256(json.dumps(subset,sort_keys=True).encode()).hexdigest() for subset in sources.previous_indexer_sets(indexers)]
    query=args['q'].casefold();items={};failures={};checked=0;following=start;scanned=0
    circuit=Counter();lock=threading.Lock();reasons=Counter();retry_before=0;limited=set()
    deadline=time.monotonic()+35
    def note_failure(identity,stage,exc):
        nonlocal retry_before
        reason=sources.failure_reason(exc)
        with lock:
            circuit[stage]+=1;reasons[reason]+=1
            if stage=='NZB':limited.update(limited_indexers(exc))
            retry_before=max(retry_before,getattr(exc,'retry_at',0))
        logging.getLogger(__name__).warning('Movie catalogue check failed: tmdb=%s stage=%s reason=%s',identity,stage,reason)
    def load(row):
        if row.get('adult') or (query and query not in str(row.get('title') or '').casefold()):return None,None
        identity=str(row.get('id') or '')
        if not identity.isdigit():return None,None
        if catalogue_only:
            # Metadata lists remain browsable without an indexer/release. The
            # shared popup resolves exact identity and release choices on demand.
            if section=='new' and not earliest.isoformat()<=str(row.get('release_date') or '')<=today.isoformat():return None,None
            return dict(movie_card(row),live=True,metadataVerified=True),None
        with lock:
            blocked='metadata' if circuit['metadata']>=4 else None
        if blocked:return None,blocked
        stage='metadata'
        try:
            detail=TMDB(store).get('movie/'+identity,language='en-GB')
            if section=='new' and not earliest.isoformat()<=str(detail.get('release_date') or '')<=today.isoformat():return None,None
            imdb=str(detail.get('imdb_id') or '')
            if not re.fullmatch(r'tt\d{5,12}',imdb):return None,None
            stage='NZB'
            try:releases=availability(app,store,imdb[2:],account,compatible)
            except (ValueError,TypeError,KeyError) as exc:
                # A failed refresh must not erase a previously confirmed match.
                saved=saved_availability(store,availability_key(imdb[2:],account))
                if not saved or saved['updated']<time.time()-86400 or not json.loads(saved['body']):raise
                releases=json.loads(saved['body'])
                note_failure(identity,stage,exc)
                item=movie_card({**row,**detail})
                item.update(id='movie:'+imdb[2:],externalId=imdb[2:],live=True,releases=releases,metadataVerified=True)
                return item,stage
            if not releases:return None,None
            item=movie_card({**row,**detail})
            item.update(id='movie:'+imdb[2:],externalId=imdb[2:],live=True,releases=releases,
                        metadataVerified=True,provider=', '.join(dict.fromkeys(r.get('provider','NZB') for r in releases)))
            return item,None
        except (ValueError,TypeError,KeyError) as exc:
            note_failure(identity,stage,exc)
            return None,stage
    scopes={'all':'TMDB movie catalogue','new':'TMDB films released in the past 90 days','trending':'TMDB weekly trending','top':'TMDB top-rated movies'}
    def payload():
        counts=Counter(stage for row,stage in failures.values())
        warnings=[]
        if counts:
            summary=', '.join(str(n)+' '+('NZB availability' if stage=='NZB' else 'TMDB metadata')+' checks' for stage,n in sorted(counts.items()))
            warnings=[summary+' could not finish. Retrying automatically; confirmed matches are retained.']
            if limited:
                warnings=[limit_message(limited)+' Saved and verified matches are shown; checks will resume automatically.']
        return dict(items=list(items.values()),nextOffset=args['offset']+1 if following is not None else None,
            nextCursor=json.dumps({'movies':following}) if following is not None else None,
            label=dict(SECTIONS)[section],collection=section,metadataComplete=True,warnings=warnings,
            incomplete=bool(failures),retryAt=max(time.time()+60,retry_before) if failures else None,
            checked=checked,cataloguePages=scanned,failureReasons=dict(reasons) if failures else {},fetchedAt=datetime.now(timezone.utc).isoformat(),
            catalogueOnly=catalogue_only,
            scope=scopes[section]+('. Download availability is checked separately.' if catalogue_only else '. Only exact IMDb matches with NZB releases are shown.')+' Sort applies to loaded titles.')
    with ThreadPoolExecutor(max_workers=2) as pool:
        while following is not None and len(items)<(20 if args.get('_one_page') else BATCH_TARGET) and scanned<(1 if args.get('_one_page') else MAX_PAGES):
            params=dict(page=following//20+1,language='en-GB')
            if section=='new':params.update(include_adult='false',include_video='false',sort_by='popularity.desc',
                **{'primary_release_date.gte':earliest.isoformat(),'primary_release_date.lte':today.isoformat()})
            result=TMDB(store,cache_seconds=300).get(path,**params)
            rows=result.get('results')
            if not isinstance(rows,list):raise ValueError('This movie list is temporarily unavailable.')
            total=min(int(result.get('total_pages') or 1),500)
            following=following+20 if rows and params['page']<total else None
            scanned+=1;checked+=len(rows)
            pending={pool.submit(load,row):row for row in rows}
            for task in as_completed(pending):
                row=pending[task];item,error=task.result()
                if item:items[item['id']]=item
                if error:failures[str(row['id'])]=(row,error)
                if args.get('_partial') and items:args['_partial'](payload())
            if any(n>=4 for n in circuit.values()) or time.monotonic()>=deadline:break
        # Retry only failed titles once; do not repeat successful provider work.
        if failures and retry_before<=time.time():
            pending=list(failures.values());failures={};circuit.clear()
            for (row,stage),(item,error) in zip(pending,pool.map(load,[row for row,stage in pending])):
                if item:items[item['id']]=item
                if error:failures[str(row['id'])]=(row,error)
    return payload()


def unreleased(item,today=None):
    today=today or datetime.now(ZoneInfo('Europe/London')).date()
    try:return datetime.fromisoformat(str(item.get('releaseDate') or '')).date()>today
    except (ValueError,TypeError):return False


def upcoming(store,args,start):
    """Original future releases only; regional re-releases are not premieres."""
    from . import public_movies
    warnings=[]
    try:
        tomorrow=datetime.now(ZoneInfo('Europe/London')).date()+timedelta(days=1)
        result=TMDB(store,cache_seconds=300).get('discover/movie',page=start//20+1,language='en-GB',
            include_adult='false',include_video='false',sort_by='popularity.desc',
            **{'primary_release_date.gte':tomorrow.isoformat()})
        rows=result.get('results')
        if not isinstance(rows,list):raise ValueError('Invalid upcoming catalogue.')
        items=[movie_card(row) for row in rows if not row.get('adult')]
        following=start+20 if rows and start//20+1<min(int(result.get('total_pages') or 1),500) else None
    except (ValueError,TypeError,KeyError):
        result=public_movies.movies(store,offset=start,upcoming=True)
        items=result['items'];following=result['nextOffset'];warnings=result.get('warnings',[])
    items=[dict(item,live=True,metadataVerified=True) for item in items if unreleased(item) and args['q'].casefold() in item['title'].casefold()]
    return dict(items=items,nextOffset=args['offset']+1 if following is not None else None,
        nextCursor=json.dumps({'movies':following}) if following is not None else None,
        label='Premiering Movies',collection='premiering',catalogueOnly=True,metadataComplete=True,warnings=warnings,
        fetchedAt=datetime.now(timezone.utc).isoformat(),
        scope='TMDB original releases after today. Released films, regional re-releases and unknown dates are excluded. Download availability is checked separately.')
