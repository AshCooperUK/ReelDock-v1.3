"""Indexer catalogue membership first; metadata and acquisition stay separate."""
import hashlib
import copy
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlencode
from .providers import initialise
from .cache_lock import refresh_lock
from .. import discover_sources as sources
from ..discover_indexer_rate import request_slot, limited_indexers, limit_message

POLICY='indexer-first-v2'
FEEDS={
    'movies':{'trending':'trending_movies','new':'new_movies','top':'geek_rated_movies','premiering':'upcoming_movies'},
    'shows':{'trending':'trending_shows','top':'geek_rated_tv','endorsed':'endorsed_shows',
             'new':'new_shows','recent':'recently_aired_shows','premiering':'premiering_shows'},
}
RELEASE=re.compile(r'(?i)(?:\bS\d{1,4}(?:[. _-]*E\d{1,4})?\b|\b\d{3,4}[pi]\b|\b(?:WEB[. -]?DL|WEBRip|HDTV|DVD9|BluRay)\b)')


def key(indexer,feed):
    account=hashlib.sha256(json.dumps(indexer,sort_keys=True).encode()).hexdigest()
    return 'nzb:catalogue:'+POLICY+':'+feed+':'+account


def parse(raw,kind,provider):
    root=sources._rss_root(raw);rows=root.findall('./channel/item')
    # Unknown functions sometimes return ordinary NZB uploads with HTTP200.
    if any(RELEASE.search(r.findtext('title') or '') or any('nzb' in (e.get('type') or '').lower() for e in r.findall('enclosure')) for r in rows):
        raise ValueError('Unsupported catalogue.')
    items={};bad=0
    for start in range(0,len(rows),1000):
        rss=ET.Element('rss');channel=ET.SubElement(rss,'channel')
        for original in rows[start:start+1000]:
            attrs=sources._attrs(original)
            field='imdbid' if kind=='movie' else 'tvdbid'
            external=(original.findtext(field) or attrs.get('imdbid' if kind=='movie' else 'tvdbid') or attrs.get('imdb' if kind=='movie' else 'tvdb') or '').strip()
            if kind=='movie':external=external.removeprefix('tt')
            if not re.fullmatch(r'[0-9]{1,12}',external) or int(external)==0 or not (original.findtext('title') or '').strip():
                bad+=1;continue
            row=copy.deepcopy(original)
            for e in list(row):
                if e.tag in ('imdbid','tvdbid'):row.remove(e)
            ET.SubElement(row,field).text=external
            channel.append(row)
        parsed=sources.parse_feed(ET.tostring(rss),'trending_movies' if kind=='movie' else 'trending_shows',provider)
        for item in parsed:
            external=str(item.get('externalId') or '')
            if not re.fullmatch(r'[0-9]{1,12}',external) or int(external)==0:continue
            item.update(id=kind+':'+str(int(external)),externalId=str(int(external)))
            items[item['id']]=item
    # Duplicates are legitimate. Unidentified rows make completeness unknown.
    response=next((e for e in root.iter() if str(e.tag).split('}')[-1]=='response'),None)
    attrs=dict(response.attrib) if response is not None else {}
    return list(items.values()),rows,bad,attrs


def fetch_page(store,indexer,feed,offset,kind):
    from ..downloaders.nzb.indexers import api_base
    if not indexer.get('url') or not indexer.get('api_key'):raise ValueError('Indexer catalogue unavailable.')
    base=api_base(indexer['url'])
    # RSS is the indexer's advertised catalogue interface. Never downgrade a
    # named list to generic uploads when its specialised API ignores t=<name>.
    endpoints=[base.rsplit('/api',1)[0]+'/rss?'+urlencode(dict(t=feed,r=indexer['api_key'],limit=1000,offset=offset)),
               base+'?'+urlencode(dict(t=feed,apikey=indexer['api_key'],limit=1000,offset=offset,extended=1))]
    last=None
    for url in endpoints:
        try:
            with request_slot(store,indexer):raw=sources.fetch(url,limit=8_000_000,timeout=12)
            return parse(raw,kind,sources._provider_name(indexer))
        except (ValueError,TypeError,KeyError) as exc:
            if limited_indexers(exc):raise
            last=exc
    raise ValueError('Indexer catalogue unavailable.') from last


def provider(store,indexer,feed,kind):
    initialise(store);cache=key(indexer,feed);now=time.time()
    def read():
        with store.connect() as db:row=db.execute('SELECT body FROM view_media_cache WHERE key=?',(cache,)).fetchone()
        try:return json.loads(row[0]) if row else {}
        except (ValueError,TypeError):return {}
    previous=read()
    if previous.get('retryAt',0)>now:return previous
    with refresh_lock(store,cache):
        previous=read()
        if previous.get('retryAt',0)>time.time():return previous
        # Resume a declared, unfinished page; a normal fresh snapshot starts at0.
        resume=previous.get('cursor')
        items={i['id']:i for i in previous.get('items',[])} if resume is not None else {}
        offset=resume or 0;complete=False;reason='';limited=();retry_at=0;deadline=time.monotonic()+25;seen=set()
        while True:
            try:
                cards,rows,bad,attrs=fetch_page(store,indexer,feed,offset,kind)
                # The catalogue's verified titles remain useful even if the
                # response lies about its offset/total. Keep them before deciding
                # whether completeness can be established (NZBgeek New Movies).
                items.update((i['id'],i) for i in cards)
                returned=attrs.get('offset')
                if returned is not None and (not returned.isdigit() or int(returned)!=offset):raise ValueError('Catalogue page did not advance.')
                stamp=tuple(i['id'] for i in cards)
                if offset and stamp in seen:raise ValueError('Catalogue page did not advance.')
                seen.add(stamp)
                total=attrs.get('total');following=offset+len(rows)
                if bad:reason='Some indexer catalogue identities are unavailable.';resume=None;break
                if total is not None:
                    if not total.isdigit():raise ValueError('Invalid catalogue total.')
                    if following>int(total):raise ValueError('Inconsistent catalogue total.')
                    if following>=int(total):complete=bool(items);resume=None;break
                    if not rows:raise ValueError('Catalogue page did not advance.')
                    resume=following
                else:reason='The indexer did not confirm the end of its catalogue.';resume=None;break
                if time.monotonic()>deadline:reason='The indexer catalogue is still being collected.';break
                offset=resume
            except (ValueError,TypeError,KeyError) as exc:
                limited=limited_indexers(exc)
                retry_at=getattr(exc,'retry_at',0) or 0
                reason=limit_message(limited) if limited else 'This indexer did not provide a complete catalogue.'
                # Keep known titles on failures, never a credential-bearing error.
                items={**{i['id']:i for i in previous.get('items',[])},**items}
                resume=offset if offset else None
                break
        result=dict(items=list(items.values()),complete=complete,cursor=resume,reason=reason,
                    retryAt=max(time.time()+(900 if complete else 300),retry_at),
                    provider=sources._provider_name(indexer),limited=list(limited),fetchedAt=time.time())
        with store.connect() as db:db.execute('INSERT OR REPLACE INTO view_media_cache VALUES(?,?,?)',(cache,json.dumps(result),time.time()))
        return result


def known_metadata(store,items):
    """Reuse exact saved identities; missing metadata never hides a catalogue title."""
    if not items:return items
    ids={i['id'] for i in items};known={}
    with store.connect() as db:
        # Existing cards already contain safe, checked metadata. No provider call
        # per poster, and no filename/name/year matching or new monitoring entry.
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='saved_media_catalogue'").fetchone():
            for row in db.execute("SELECT body FROM saved_media_catalogue WHERE origin IN ('NZB','Catalogue') ORDER BY updated"):
                card=json.loads(row[0]);raw=(card.get('action') or {}).get('item') or {}
                external=str(raw.get('externalId') or '')
                canonical=raw.get('kind','')+':'+str(int(external)) if external.isdigit() and int(external)>0 else ''
                if canonical in ids:known[canonical]={**raw,**{k:card[k] for k in ('releaseDate','lastEpisodeReleased','image','backdrop','rating','description','title','year') if card.get(k)}}
    for item in items:
        saved=known.get(item['id'],{})
        item.update({k:saved[k] for k in ('releaseDate','lastEpisodeReleased','image','backdrop','rating','description','title','year','genre') if saved.get(k)})
        item.pop('feed',None)
    return items


def browse(app,store,args,indexers,fallback):
    media=args['media'];section=args['collection'];feed=FEEDS[media][section];kind='movie' if media=='movies' else 'show'
    responses=[provider(store,indexer,feed,kind) for indexer in indexers]
    items={}
    for response in responses:
        for item in response['items']:items.setdefault(item['id'],item)
    rows=known_metadata(store,list(items.values()))
    if section=='premiering':
        from .movie_sections import unreleased
        filtered=[i for i in rows if unreleased(i)]
        # A premiere needs a verified future date, not an inferred date from year.
        if len(filtered)!=len(rows):responses=[dict(r,complete=False) for r in responses]
        rows=filtered
    complete=any(r['complete'] and r['items'] for r in responses)
    providers=list(dict.fromkeys(r['provider'] for r in responses if r['items']))
    warnings=list(dict.fromkeys(limit_message(r['limited']) for r in responses if r.get('limited')))
    if media=='movies':from .movie_sections import SECTIONS
    else:from .show_sections import SECTIONS
    result=dict(items=rows,label=dict(SECTIONS)[section],nextOffset=None,nextCursor=None,metadataComplete=True,incomplete=False,warnings=warnings,
                listPolicy=POLICY,indexerComplete=complete,catalogueOnly=True,
                fetchedAt=datetime.now(timezone.utc).isoformat(),catalogueSource='NZB indexer catalogue')
    if not complete:
        try:
            extra=fallback(dict(args,_catalogue_fallback=True,_partial=None))
            seen={i['id'] for i in rows}
            rows.extend(i for i in extra['items'] if i['id'] not in seen)
            result.update({k:v for k,v in extra.items() if k not in ('items','scope','warnings')})
            result.update(items=rows,warnings=[*warnings,*extra.get('warnings',[])],listPolicy=POLICY,
                          indexerComplete=False,catalogueOnly=True,catalogueSource='NZB indexer catalogue + TMDB fallback' if providers else 'TMDB fallback')
        except (ValueError,TypeError,KeyError):
            result.update(incomplete=True,retryAt=time.time()+60,catalogueSource='TMDB fallback' if not rows else result['catalogueSource'],
                          warnings=[*warnings,'TMDB fallback is unavailable. Known indexer titles are retained.' if rows else 'No NZB catalogue is available and the TMDB fallback could not be loaded.'])
    reason='No NZB indexers enabled.' if not indexers else 'No complete indexer catalogue is available.'
    result['scope']=('Indexer catalogue: '+', '.join(providers)+'. ' if providers else '')+(
        'Complete indexer list. ' if complete else reason+' TMDB list fallback. ')+ 'Download availability is checked separately when a title is opened.'
    retries=[r['retryAt'] for r in responses if not r['complete']]
    if not complete and retries:result['indexerRetryAt']=max(time.time()+60,min(retries))
    if args.get('q'):result['items']=[i for i in result['items'] if args['q'].casefold() in i['title'].casefold()]
    if args.get('_partial'):args['_partial'](result)
    return result


def all_fallback(app,store,args,start,indexers):
    """One metadata page per existing100-step archive cursor; no skipped pages."""
    if args['media']=='movies':from .movie_sections import browse as metadata
    else:from .show_sections import browse as metadata
    field='movies' if args['media']=='movies' else 'shows'
    selected=dict(args,collection='all',offset=0,cursor={field:(start//100)*20},_catalogue_fallback=True,_one_page=True)
    try:result=metadata(app,store,selected,indexers)
    except ValueError:
        result=dict(items=[],nextCursor=None,incomplete=True,metadataComplete=True,retryAt=time.time()+60,
                    warnings=['No NZB catalogue is available and the TMDB fallback could not be loaded.'])
    following=start+100 if result.get('nextCursor') else None
    result.update(nextOffset=following,nextCursor=None,catalogueOnly=True,catalogueSource='TMDB fallback',listPolicy=POLICY,
                  label='View All Movies' if args['media']=='movies' else 'View All TV Shows',metadataComplete=True,
                  scope='No complete NZB release page is available. TMDB catalogue fallback. Download availability is checked separately when a title is opened.')
    return result
