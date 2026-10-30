"""TV collections from checked metadata, restricted to confirmed NZB matches."""
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from .providers import TMDB,initialise
from .show_metadata import details,tmdb_card,day
from .cache_lock import refresh_lock
from .. import discover_sources as sources
from ..discover_indexer_rate import limited_indexers,limit_message

SECTIONS=[('new','New TV Shows'),('recent','Recently Aired TV Shows'),('premiering','Premiering TV Shows'),('trending','Trending TV Shows'),('top','Top Rated TV Shows'),('endorsed','Endorsed TV Shows'),('all','View All TV Shows')]
SORTS=[('title','A-Z'),('title_desc','Z-A'),('release','Latest'),('episode_released','Latest Episode'),('release_asc','Oldest'),('episode_released_asc','Oldest Episode'),('source','NZB Order'),('year','Year'),('rating','Rating')]


def account_key(indexers):return hashlib.sha256(json.dumps(indexers,sort_keys=True).encode()).hexdigest()
def availability_key(account,tvdb):return 'nzb:show-availability:'+account+':'+str(int(tvdb))
def saved(store,key):
    with store.connect() as db:return db.execute('SELECT body,updated FROM view_media_cache WHERE key=?',(key,)).fetchone()
def save(store,key,value):
    with store.connect() as db:db.execute('INSERT OR REPLACE INTO view_media_cache VALUES(?,?,?)',(key,json.dumps(value),time.time()))


def remember_available(store,indexers,items):
    initialise(store);account=account_key(indexers)
    for item in items:
        if item.get('releases') and str(item.get('externalId') or '').isdigit():save(store,availability_key(account,item['externalId']),item['releases'])


def probe(app,store,item,indexers):
    from ..discover_search import query_all
    tvdb=str(item['externalId']);failures=[];limited=[];retry_at=0
    for indexer in indexers:
        pages,warnings=query_all([indexer],dict(t='tvsearch',tvdbid=tvdb,cat='5000',limit=5,offset=0,extended=1),timeout=5,response_limit=512_000,store=store)
        failures.extend(getattr(warnings,'failures',()));retry_at=max(retry_at,getattr(warnings,'retry_at',0))
        limited.extend(getattr(warnings,'limited_indexers',()))
        result=[]
        for provider,root in pages:
            for row in root.findall('./channel/item'):
                attrs=sources._attrs(row);title=row.findtext('title') or '';grab=sources._grab(row)
                if not grab or 'sample' in title.casefold():continue
                external=attrs.get('tvdbid') or attrs.get('tvdb') or ''
                if external:
                    if not external.isdigit() or int(external)!=int(tvdb):continue
                else:
                    # Scoped APIs can omit IDs; confirm name and edition rather than
                    # accepting arbitrary releases from an unsupported ID filter.
                    name=sources._show_name(title)
                    names=sources._name_candidates(name)
                    if sources._norm_title(item['title']) not in {sources._norm_title(n) for n in names}:continue
                    edition=re.search(r'\b((?:19|20)\d{2})$',name)
                    if edition and item.get('year') and edition[1]!=item['year']:continue
                    if not re.search(r'(?i)(?:S\d{1,4}|\d{4}[. _-]\d{2}[. _-]\d{2})',title):continue
                size=attrs.get('size') or '0'
                result.append(dict(id=sources.opaque_id(grab),title=title,size=int(size) if size.isdigit() else 0,
                    published=row.findtext('pubDate') or '',externalId=tvdb,kind='show',provider=provider))
        if result:return result
    if failures:
        error=sources.DiscoverError('TV availability search is incomplete.');error.failures=tuple(failures);error.retry_at=retry_at;error.limited_indexers=tuple(dict.fromkeys(limited));raise error
    return []


def availability(app,store,item,indexers):
    initialise(store)
    key=availability_key(account_key(indexers),item['externalId'])
    row=saved(store,key)
    if row and row['updated']>time.time()-(3600 if json.loads(row['body']) else 300):return json.loads(row['body'])
    for subset in sources.previous_indexer_sets(indexers):
        previous=saved(store,availability_key(account_key(subset),item['externalId']))
        if previous and previous['updated']>time.time()-3600 and json.loads(previous['body']):
            save(store,key,json.loads(previous['body']));return json.loads(previous['body'])
    with refresh_lock(store,key):
        row=saved(store,key)
        if row and row['updated']>time.time()-(3600 if json.loads(row['body']) else 300):return json.loads(row['body'])
        releases=probe(app,store,item,indexers);save(store,key,releases);return releases


def named_collection(store,indexers,name):
    """Accept catalogue cards, never an unsupported request's generic releases."""
    if name not in ('endorsed_shows','premiering_shows'):raise ValueError('Unknown TV catalogue.')
    initialise(store)
    key='nzb:tv-catalogue:v2:'+name+':'+account_key(indexers)
    row=saved(store,key)
    if row and row['updated']>time.time()-3600:return json.loads(row['body'])
    from ..discover_indexer_rate import request_slot
    items={};supported=[]
    for indexer in indexers:
        try:
            with request_slot(store,indexer):raw=sources._named_catalogue(indexer,name)
            rows=sources._rss_root(raw).findall('./channel/item')
            # Some indexers ignore t=<unknown> and return recent NZB uploads.
            # Such a response does not establish editorial catalogue membership.
            release_name=re.compile(r'(?i)(?:\bS\d{1,4}(?:[. _-]*E\d{1,4})?\b|\b\d{3,4}[pi]\b|\b(?:WEB[. -]?DL|WEBRip|HDTV|DVD9|BluRay)\b)')
            if any(release_name.search(r.findtext('title') or '') or any('nzb' in (e.get('type') or '').lower() for e in r.findall('enclosure')) for r in rows):continue
            cards=sources.parse_feed(raw,'trending_shows',sources._provider_name(indexer))
            if cards:
                supported.append(sources._provider_name(indexer))
                for item in cards:items[item['id']]=dict(item,feed=name)
        except (ValueError,TypeError,KeyError):continue
    result=dict(items=list(items.values()),providers=supported);save(store,key,result);return result


def endorsements(store,indexers):return named_collection(store,indexers,'endorsed_shows')


def catalogue(store,section,start,today,indexers,metadata_only=False):
    client=TMDB(store,cache_seconds=300);page=start//20+1
    params=dict(language='en-GB',page=page)
    if section=='premiering' and not metadata_only:
        feed=named_collection(store,indexers,'premiering_shows')
        if feed['items']:
            return feed['items'][start:start+20],start+20 if start+20<len(feed['items']) else None,'Indexer Premiering catalogue: '+', '.join(feed['providers'])
    if section=='endorsed':
        feed=endorsements(store,indexers) if not metadata_only else dict(items=[],providers=[])
        if feed['items']:
            return feed['items'][start:start+20],start+20 if start+20<len(feed['items']) else None,'Indexer endorsements: '+', '.join(feed['providers'])
        seeds=client.get('tv/top_rated',language='en-GB',page=1).get('results',[])[:3]
        if not seeds:return [],None,'TMDB recommendations fallback; no indexer endorsement feed available'
        seed=seeds[(page-1)%len(seeds)];remote=(page-1)//len(seeds)+1
        result=client.get('tv/'+str(seed['id'])+'/recommendations',language='en-GB',page=remote)
        following=start+20 if (page-1)%len(seeds)<len(seeds)-1 or remote<min(int(result.get('total_pages') or 1),500) else None
        return result.get('results',[]),following,'TMDB recommendations from highly rated shows (no indexer endorsement feed available)'
    paths={'all':'discover/tv','new':'discover/tv','recent':'discover/tv','premiering':'discover/tv','trending':'trending/tv/week','top':'tv/top_rated'}
    scopes={'all':'TMDB TV catalogue','new':'TMDB series premiered in the past 90 days','recent':'TMDB series with an episode aired in the past seven days','premiering':'TMDB series premiering in the next 90 days (indexer Premiering catalogue feed unavailable)','trending':'TMDB weekly TV trending','top':'TMDB top-rated TV shows'}
    if section in ('new','recent','premiering'):
        params.update(include_adult='false',include_null_first_air_dates='false',sort_by='popularity.desc',timezone='Europe/London')
        field='air_date' if section=='recent' else 'first_air_date'
        earliest=today-timedelta(days=7 if section=='recent' else 90) if section!='premiering' else today+timedelta(days=1)
        latest=today+timedelta(days=90) if section=='premiering' else today
        params.update({field+'.gte':earliest.isoformat(),field+'.lte':latest.isoformat()})
    result=client.get(paths[section],**params)
    return result.get('results',[]),start+20 if page<min(int(result.get('total_pages') or 1),500) else None,scopes[section]


def browse(app,store,args,indexers):
    initialise(store);section=args['collection'];today=datetime.now(ZoneInfo('Europe/London')).date()
    following=(args.get('cursor') or {}).get('shows',args['offset']*20)
    items={};failures=0;limited=set();retry_at=0;scanned=0;scope='';deadline=time.monotonic()+35
    def payload():
        return dict(items=list(items.values()),nextOffset=args['offset']+1 if following is not None else None,
            nextCursor=json.dumps({'shows':following}) if following is not None else None,label=dict(SECTIONS)[section],collection=section,
            metadataComplete=True,catalogueOnly=section=='premiering' or bool(args.get('_catalogue_fallback')),incomplete=bool(failures),retryAt=max(time.time()+60,retry_at) if failures else None,
            warnings=[limit_message(limited)+' Saved matches are retained and checks will resume automatically.'] if limited else ['Some TV metadata or NZB checks could not finish. Saved matches are retained and checks will resume automatically.'] if failures else [],
            fetchedAt=datetime.now(timezone.utc).isoformat(),scope=scope+('. Download availability is checked separately.' if args.get('_catalogue_fallback') else '. Upcoming catalogue; download availability is checked separately.' if section=='premiering' else '. Only verified TV identities with NZB releases are shown.')+' Sort applies to loaded titles.')
    def load(row):
        if row.get('adult'):return None
        if row.get('externalId'):item={**row,**details(store,row['externalId'])}
        else:
            key=str(row.get('id') or '')
            if not key.isdigit():return None
            detail=TMDB(store,cache_seconds=300).get('tv/'+key,language='en-GB',append_to_response='external_ids')
            # A valid catalogue title without a TVDB mapping is unsupported,
            # rather than a failed request that should retry indefinitely.
            if not (detail.get('external_ids') or {}).get('tvdb_id'):return None
            item=tmdb_card(detail)
            # Persist the same metadata used by All and episode-lookup recovery.
            if item.get('externalId'):save(store,'nzb:show-metadata:v1:'+item['externalId'],item)
        if args['q'] and args['q'].casefold() not in item['title'].casefold():return None
        first=day(item.get('releaseDate'));aired=day(item.get('lastEpisodeReleased'),aired=True)
        if section=='new' and not (today-timedelta(days=90)).isoformat()<=first<=today.isoformat():return None
        if section=='recent' and not (today-timedelta(days=7)).isoformat()<=aired<=today.isoformat():return None
        if section=='premiering':
            # Premiering means a known first premiere after today, even when
            # an indexer catalogue includes older series or repeat airings.
            if not today.isoformat()<first:return None
            cached=saved(store,availability_key(account_key(indexers),item['externalId']))
            releases=json.loads(cached['body']) if cached and cached['updated']>time.time()-3600 else []
            return dict(item,live=True,releases=releases,provider=row.get('provider') or 'TMDB')
        if args.get('_catalogue_fallback'):return dict(item,live=True,releases=[],provider='TMDB')
        releases=availability(app,store,item,indexers)
        if not releases:return None
        return dict(item,live=True,releases=releases,provider=', '.join(dict.fromkeys(r.get('provider','NZB') for r in releases)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        while following is not None and len(items)<(20 if args.get('_one_page') else 24) and scanned<(1 if args.get('_one_page') else 3):
            options=dict(metadata_only=True) if args.get('_catalogue_fallback') else {}
            rows,following,scope=catalogue(store,section,following,today,indexers,**options);scanned+=1
            pending=[pool.submit(load,row) for row in rows]
            for task in as_completed(pending):
                try:
                    item=task.result()
                    if item:items[item['id']]=item
                except (ValueError,TypeError,KeyError) as exc:
                    failures+=1;retry_at=max(retry_at,getattr(exc,'retry_at',0))
                    limited.update(limited_indexers(exc))
                if args.get('_partial') and items:args['_partial'](payload())
            if failures>=4 or time.monotonic()>=deadline:break
    return payload()
