"""Discover catalogue and download coordinator, using existing acquisition engines."""
import hmac
import hashlib
from datetime import datetime
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from flask import jsonify, request, session
from . import discover_store as ds
from . import discover_sources as sources
from .media_management import service, store as records
from .discover_handoff import serialised

_POOL=ThreadPoolExecutor(max_workers=4,thread_name_prefix='reeldock-discover')
# Long catalogue refreshes must not delay opening titles or acquisition work.
_BROWSE_POOL=ThreadPoolExecutor(max_workers=2,thread_name_prefix='reeldock-catalogue')
_LOCK=threading.Lock()
_PENDING={}
_ERRORS={}
_RETRY={}
def default_browse_args(media='movies'):
    return dict(media=media,q='',sort='release',offset=0,collection='trending',category='all',cursor=None)

def browse_cache_key(args,store,indexers=None):
    if indexers is None:
        try:indexers=sources.indexers_for(None,store)
        except ValueError:indexers=[]
    accounts=[indexers,bool(store.get('m3u_tmdb_token'))]
    if args.get('global'):
        with store.connect() as db:
            accounts.append([tuple(row) for row in db.execute("SELECT id,coalesce(json_extract(options,'$.enabled'),1) FROM m3u_sources ORDER BY id")])
    account=hashlib.sha256(json.dumps(accounts,sort_keys=True).encode()).hexdigest()
    prefix='engine:global-search:v5:' if args.get('global') else 'engine:nzb-browse:v11:' if args.get('collection') not in ('',None,'all') else 'engine:nzb-browse:v7:'
    return prefix+account+':'+json.dumps(dict(args,sort='source'),sort_keys=True,separators=(',',':'))

def warm_first_pages(app,store):
    """Prepare the default catalogue before navigation, on the existing pool."""
    from .view_media.saved_catalogue import enabled
    if enabled(store):return
    for media in ('movies','shows'):
        cached_browse(app,store,default_browse_args(media))
    # Unopened named lists must not consume quota or occupy the catalogue pool.
    # Their saved snapshots and shared title availability are reused on navigation.


def preferences(store):
    from .settings.nzb import DISCOVERY_FIELDS
    from .settings.auto_download import FIELDS
    return {**{key:field.read(store) for key,field in DISCOVERY_FIELDS.items()},'release_check_minutes':FIELDS['release_check_minutes'].read(store)}

def schedule(app,store,key,fn):
    identity=(str(store.directory),key)
    with _LOCK:
        if identity in _PENDING or _RETRY.get(identity,0)>time.time():return False
        if len(_PENDING)>=32:return False
        _PENDING[identity]=True
        _ERRORS.pop(identity,None)
    def run():
        try:
            with app.app_context(),store.background_operation():
                if not store.reset_pending():fn()
            with _LOCK:_ERRORS.pop(identity,None)
        except Exception:
            with _LOCK:
                _ERRORS[identity]='This source is temporarily unavailable. Try again shortly.'
                _RETRY[identity]=time.time()+60
        finally:
            with _LOCK:_PENDING.pop(identity,None)
    (_BROWSE_POOL if key.startswith(('engine:nzb-browse:', 'engine:global-search:')) else _POOL).submit(run)
    return True

def cached_browse(app,store,args,prefetch=True,ahead=2):
    if args.get('global'):
        from .view_media.search_snapshot import cached_browse as search
        return search(app,store,args)
    from .view_media.providers import initialise
    from .view_media.adapters import REGISTRY
    from .view_media.ordering import ordered
    initialise(store)
    key=browse_cache_key(args,store)
    with store.connect() as db:row=db.execute('SELECT body,updated FROM view_media_cache WHERE key=?',(key,)).fetchone()
    if not row and not args.get('global'):
        try:indexers=sources.indexers_for(app,store)
        except ValueError:indexers=[]
        for subset in sources.previous_indexer_sets(indexers):
            with store.connect() as db:
                previous=db.execute('SELECT body,updated FROM view_media_cache WHERE key=?',(browse_cache_key(args,store,subset),)).fetchone()
            if previous and previous['updated']>time.time()-3600:
                saved=json.loads(previous['body'])
                if saved.get('items') and not saved.get('preparing'):
                    saved.update(warnings=[],incomplete=False,retryAt=None)
                    with store.connect() as db:
                        db.execute('INSERT OR IGNORE INTO view_media_cache VALUES(?,?,?)',(key,json.dumps(saved),0))
                        row=db.execute('SELECT body,updated FROM view_media_cache WHERE key=?',(key,)).fetchone()
                    break
    identity=(str(store.directory),key)
    def load():
        from .view_media.cache_lock import refresh_lock
        with refresh_lock(store,key):
            # Another process may have finished while this refresh waited.
            with store.connect() as db:latest=db.execute('SELECT body,updated FROM view_media_cache WHERE key=?',(key,)).fetchone()
            saved=json.loads(latest['body']) if latest else {}
            due=(saved.get('retryAt') or latest['updated']+300) if latest else 0
            if latest and not saved.get('preparing') and due>time.time():return
            def retain(result):
                if saved.get('items') and (result.get('incomplete') or result.get('preparing')):
                    ids={item['id'] for item in result['items']}
                    result=dict(result,items=[*result['items'],*(item for item in saved['items'] if item['id'] not in ids)])
                return result
            def partial(result):
                if result['items']:
                    snapshot=retain(dict(result,preparing=True))
                    with store.connect() as db:db.execute('INSERT OR REPLACE INTO view_media_cache VALUES(?,?,?)',(key,json.dumps(snapshot),time.time()))
            loader=REGISTRY['discover']['browse']
            if args.get('global'):
                from .view_media.global_search import browse
                loader=browse
            result=retain(loader(app,store,{**args,'sort':'source','keyless':True,'_partial':partial}))
            checked=result.get('freshness',{}).get('fetchedAt')
            updated=datetime.fromisoformat(checked).timestamp() if checked else time.time()
            with store.connect() as db:
                db.execute('INSERT OR REPLACE INTO view_media_cache VALUES(?,?,?)',(key,json.dumps(result),updated))
        if prefetch:prefetch_next(result)
    def prefetch_next(result):
        if ahead>0 and not args['q'] and result.get('nextOffset') is not None and result.get('nextCursor'):
            try:cursor=json.loads(result['nextCursor'])
            except ValueError:return
            following=dict(args,offset=result['nextOffset'],cursor=cursor)
            cached_browse(app,store,following,prefetch=ahead>1,ahead=ahead-1)
    saved=json.loads(row['body']) if row else {}
    due=(saved.get('retryAt') or row['updated']+300) if row else 0
    if due<=time.time():schedule(app,store,key,load)
    with _LOCK:pending=identity in _PENDING;error=_ERRORS.get(identity,'')
    result=json.loads(row['body']) if row else dict(items=[],nextOffset=None)
    preparing=bool(result.pop('preparing',False) and row and row['updated']>=time.time()-300)
    if prefetch and row and not pending and not preparing:prefetch_next(result)
    result['items']=ordered(result['items'],args['sort'])
    if result.get('freshness'):
        result['freshness']['stale']=bool(row and row['updated']<time.time()-300)
    if args.get('collection')=='premiering':
        from .view_media.movie_sections import unreleased
        result['items']=[item for item in result['items'] if unreleased(item)]
    return dict(result,loading=pending and not row,refreshing=pending or preparing,preparing=preparing,error=error)

def prepare(app,store,item):
    raw=dict(item);item=ds.clean_item(item)
    if item['kind']=='movie':
        if item['id'].startswith('tmdb:movie:'):
            from .view_media.public_movies import details
            try:raw=details(store,raw);item=ds.clean_item(raw)
            except ValueError:pass # Catalogue title remains usable while details refresh.
        day=str(raw.get('releaseDate') or '')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day):day=''
        key='movie:'+item['externalId'] if item.get('externalId') else item['id']
        records.remember(store,key,item['id'],'movie',dict(item=item,date=day,dateLabel='Release date',automatic=True))
    elif item['kind']=='show':
        from .discover_title_releases import remember
        if remember(app,store,item):return item
        tvmaze=item['id'].split(':')[-1] if item['id'].startswith('tvmaze:show:') and not item.get('externalId') else None
        try:catalogue=sources.episodes(item.get('externalId',''),store,tvmaze=tvmaze)
        except sources.DiscoverError as exc:
            if not item.get('externalId') or not str(exc).startswith(('No TVmaze','No unique')):raise
            from .view_media.show_metadata import details
            verified=details(store,item['externalId'])
            item=ds.clean_item({**item,**{k:v for k,v in verified.items() if k not in ('id','externalId','kind') and v}})
            catalogue=sources.episodes(item['externalId'],store)
        item=ds.clean_item(dict(item,description=item.get('description') or catalogue.get('summary',''),image=item.get('image') or catalogue.get('image','')))
        service.enrol(app,store,item)
    else:raise ValueError('Choose a movie or series.')
    return item

def title_data(app,store,item,offset=0,query='',limit=30,season=None):
    item=ds.clean_item(item);parent=item['id']
    from .discover_title_releases import title_key
    title_filter="parent=? AND removed=0 AND coalesce(json_extract(body,'$.directTitleKey'),'') IN ('',?)"
    title_values=(parent,title_key(item))
    if type(offset) is not int or not 0<=offset<=250000 or type(limit) is not int or not 1<=limit<=2000:raise ValueError('Invalid episode page.')
    if season is not None and (type(season) is not int or not 0<=season<=9999):raise ValueError('Choose a valid season.')
    with store.connect() as db:
        meta=db.execute('SELECT * FROM managed_media WHERE '+title_filter+' LIMIT 1',title_values).fetchone()
        season_count=db.execute("SELECT count(DISTINCT json_extract(body,'$.episode.season')) FROM managed_media WHERE parent=? AND json_extract(body,'$.episode.season')>0",(parent,)).fetchone()[0]
        seasons=[r[0] for r in db.execute("SELECT DISTINCT json_extract(body,'$.episode.season') FROM managed_media WHERE parent=? AND removed=0 AND kind='episode' AND json_type(body,'$.episode.season')='integer' ORDER BY 1",(parent,))]
        plan=db.execute('SELECT * FROM discover_series_plan WHERE parent=?',(parent,)).fetchone()
        where=title_filter;values=list(title_values)
        if season is not None:
            where+=" AND json_extract(body,'$.episode.season')=?";values.append(season)
        if query:
            marker=re.fullmatch(r'(?:s|season\s*)0*(\d+)(?:\s*e0*(\d+))?',query.strip(),re.I)
            if marker:
                where+=" AND json_extract(body,'$.episode.season')=?";values.append(int(marker[1]))
                if marker[2]:where+=" AND json_extract(body,'$.episode.number')=?";values.append(int(marker[2]))
            else:
                where+=" AND json_extract(body,'$.episode.name') LIKE ? ESCAPE '!'"
                values.append('%'+query.replace('!','!!').replace('%','!%').replace('_','!_')+'%')
        rows=db.execute("SELECT * FROM managed_media WHERE "+where+" ORDER BY CASE WHEN json_extract(body,'$.episode.season')=0 THEN 1 ELSE 0 END,json_extract(body,'$.date') DESC,key LIMIT ? OFFSET ?",(*values,limit+1,offset)).fetchall()
    if meta and item['kind']=='movie' and item.get('releaseDate'):
        saved=service.body(dict(meta))
        if not saved.get('date'):
            saved.update(date=item['releaseDate'],dateLabel='Release date')
            saved['item']={**saved['item'],'releaseDate':item['releaseDate']}
            with store.connect() as db:
                db.execute('UPDATE managed_media SET body=? WHERE key=? AND body=?',
                           (json.dumps(saved),meta['key'],meta['body']))
            # Compose from the refreshed record in this response too.
            meta=dict(meta,body=json.dumps(saved))
            rows=[dict(row,body=json.dumps(saved)) if row['key']==meta['key'] else row for row in rows]
    identity=(str(store.directory),'title:'+parent)
    with _LOCK:pending=identity in _PENDING;error=_ERRORS.get(identity,'')
    if not meta and not pending and _RETRY.get(identity,0)<=time.time():
        schedule(app,store,'title:'+parent,lambda:prepare(app,store,item));pending=True
    result=[]
    for r in rows[:limit]:
        entry=service.compose(app,store,dict(r));q=service.request_for(store,r['key'])
        entry['requested']=bool(q and q['state'] not in ('cancelled','failed','complete'))
        entry['requestState']=q['state'] if q else ''
        entry['description']=service.body(dict(r)).get('episode',{}).get('summary','')
        result.append(entry)
    direct=bool(meta and service.body(dict(meta)).get('directTitleKey'))
    return dict(item=service.body(dict(meta))['item'] if meta else item,items=result,loading=pending,error=error if not direct else '',releaseFallback=direct,
                seasonCount=season_count,seasons=seasons,nextOffset=offset+limit if len(rows)>limit else None,
                downloadAll=bool(plan and plan['download_all']),downloadFuture=bool(plan and plan['download_future']))

def queue(app,store,key,plan=None,*,automatic=False,vod=None,plan_claim=None):
    from .acquisition import ensure_open
    ensure_open(store)
    r=records.row(store,key)
    if not r or r['removed']:raise ValueError('Open this title again.')
    state=service.compose(app,store,r)
    if state['file']:return state
    current=ds.open_request_for(store,key)
    if current:return state
    if not automatic and not plan:
        from .media_holds import resume
        resume(store,['managed:'+key]+([key] if key.startswith('video:') else []))
        r=records.row(store,key)
    if r['hold'] and (plan or automatic):raise ValueError('Automatic acquisition is paused for this title.')
    if r['hold']:
        with store.connect() as db:db.execute('UPDATE managed_media SET hold=0 WHERE key=?',(key,))
    current=ds.open_request_for(store,key)
    if current:return service.compose(app,store,r)
    b=service.body(r);prefs=preferences(store);item=b['item'];episode=b.get('episode')
    if b.get('directTitleKey'):raise ValueError('Choose an available release explicitly.')
    if vod is not None:
        if r['kind']!='episode' or not episode or not isinstance(vod,dict):raise ValueError('Choose an episode source.')
        from .view_media.download_sources import choices
        matches=[e for source in choices(app,store,item)['sources'] if source['type']=='vod' for e in source['entries']
                 if e.get('source')==vod.get('source') and e.get('path')==vod.get('path')
                 and e.get('season')==episode.get('season') and e.get('number')==episode.get('number') and episode.get('number') is not None]
        if not matches:raise ValueError('That source does not match this episode.')
    if r['kind']=='movie':
        from .movie_options import policy,snapshot,find
        selected=policy(store,item)
        if selected:prefs.update(preferred_quality=selected['quality'],allow_lower_quality=selected['fallback'])
        if automatic and (not find(store,item) or not snapshot(store,find(store,item))['automatic']):return service.compose(app,store,r)
    elif r['kind']=='episode':
        from .show_options import find
        if find(store,item):
            selected=service.effective_policy(store,r)
            prefs.update(preferred_quality=selected['quality'],allow_lower_quality=bool(selected['fallback']))
    payload=dict(discoveryEngine=True,item=item,externalId=item.get('externalId',''),itemId=item['id'],year=item.get('year',''),date=b.get('date',''),source='NZB first')
    if b.get('edition'):payload['edition']=b['edition']
    if episode:payload['episode']=episode
    if plan:payload['plan']=plan;payload['planParent']=item['id']
    if automatic:payload['movieAutomatic']=True
    identity='discover:'+uuid.uuid4().hex
    if plan_claim and plan_claim.get('all_work'):
        identity='series-plan:'+hashlib.sha256(json.dumps([plan_claim['parent'],plan_claim['revision'],key]).encode()).hexdigest()
    row,created=ds.insert_request(store,dict(idempotency=identity,kind=r['kind'],
        title=item['title']+(' · S%02dE%02d'%(episode['season'],episode['number']) if episode and type(episode.get('number')) is int else ''),
        quality=prefs['preferred_quality'],fallback=prefs['allow_lower_quality'],manual=not automatic,
        episode_key=key,automatic=bool(plan) or automatic,state='waiting' if b.get('date','')>service.today() else 'searching',body=payload,
        **({'_planClaim':plan_claim} if plan_claim else {})))
    if created:
        if vod is not None:
            if not start_vod(app,store,row,item,vod):raise ValueError('The episode request could not start. Reopen this episode to check its state.')
        else:schedule(app,store,'request:'+row['id'],lambda:resolve(app,store,row))
    return service.compose(app,store,r)

def same_title(left,right):
    def norm(v):return re.sub(r'[^a-z0-9]+','',str(v).casefold())
    return norm(left)==norm(right)

def resolve(app,store,row):
    now=time.time()
    with store.connect() as db:
        claimed=db.execute('UPDATE managed_media SET lease_until=? WHERE key=? AND lease_until<?',(now+300,row['episode_key'],now)).rowcount
    if not claimed:return
    try:_resolve(app,store,row)
    finally:
        with store.connect() as db:db.execute('UPDATE managed_media SET lease_until=0 WHERE key=?',(row['episode_key'],))

def _resolve(app,store,row):
    from .acquisition import held
    from . import discover_acquire as acquire
    if held(store) or store.reset_pending() or records.policy_blocks(store,row.get('episode_key',''),automatic=False):return
    with store.connect() as db:fresh=db.execute('SELECT * FROM discover_requests WHERE id=?',(row['id'],)).fetchone()
    if not fresh or fresh['state'] not in ('waiting','searching'):return
    row=dict(fresh);b=json.loads(row['body']);item=b['item'];day=b.get('date','')
    from .media_holds import blocked,request_keys
    if blocked(store,request_keys(row)):return
    if b.get('movieAutomatic'):
        from .movie_options import find,snapshot
        option=find(store,item)
        if not option or not snapshot(store,option)['automatic']:return
    if b.get('plan'):
        with store.connect() as db:plan=db.execute('SELECT * FROM discover_series_plan WHERE parent=?',(b['planParent'],)).fetchone()
        if not plan or not plan['download_'+b['plan']]:return
    if (day and day>service.today()) or (not day and str(item.get('year',''))>service.today()[:4]):
        ds.update_request(store,row['id'],only_unbound=True,state='waiting',message='Waiting for release.');return
    prefs=preferences(store)
    if row['kind']=='movie':
        from .movie_options import policy
        selected=policy(store,item)
        if selected:prefs.update(preferred_quality=selected['quality'],allow_lower_quality=selected['fallback'])
    elif row['kind']=='episode':
        from .show_options import find
        record=records.row(store,row['episode_key'])
        if record and find(store,item):
            selected=service.effective_policy(store,record)
            prefs.update(preferred_quality=selected['quality'],allow_lower_quality=bool(selected['fallback']))
    row.update(quality=prefs['preferred_quality'],fallback=prefs['allow_lower_quality'])
    ds.update_request(store,row['id'],only_unbound=True,state='searching',quality=row['quality'],fallback=row['fallback'],message='Searching available sources.')
    try:
        configured=bool(sources.indexers_for(app,store))
        from pathlib import Path
        saved=json.loads((Path(store.directory)/'nzb-engine'/'settings.json').read_text())
        configured=configured and any(server.get('enabled',True) for server in saved.get('servers',[]))
    except (ValueError,OSError):configured=False
    try:
        if configured:
            external=b.get('externalId')
            if not external and row['kind']=='movie':
                result=sources.search_nzb(app,store,'movies',item['title'],0)
                match=next((m for m in result.get('items',[]) if same_title(m.get('title'),item['title']) and m.get('year')==item.get('year') and m.get('externalId')),None)
                if match:
                    b['externalId']=match['externalId'];row['body']=json.dumps(b)
                    existing=records.row(store,row['episode_key'])
                    if existing:
                        saved=service.library_file(app,store,dict(existing,key='movie:'+match['externalId']))
                        if saved:
                            ds.mark_owned(store,row['episode_key'],row['id'],str(saved))
                            ds.update_request(store,row['id'],state='complete',message='Already in the library.');return
                    with store.connect() as db:db.execute('UPDATE discover_requests SET body=? WHERE id=?',(row['body'],row['id']))
            if b.get('externalId'):
                releases=acquire.search_for(app,store,row)
                acquire.resolve_release(app,store,row,releases)
                with store.connect() as db:current=db.execute('SELECT * FROM discover_requests WHERE id=?',(row['id'],)).fetchone()
                if current['state'] in ('queued','downloading','verifying','failed','cancelled','cancelling'):return
                row=dict(current)
        if fallback_vod(app,store,row,b):return
        if fallback_youtube(app,store,row,b):return
        ds.update_request(store,row['id'],only_unbound=True,state='waiting',message='Waiting for release.')
    except Exception as exc:
        # Missing/unusable releases never become failed transfers.
        from .discover_logic import redact
        ds.update_request(store,row['id'],only_unbound=True,state='waiting',message='Waiting for release. '+redact(str(exc))[:160])

class _FallbackHandoffCancelled(Exception):
    """Reject a stale binding by rolling back its queue and policy writes."""


@serialised
def start_vod(app,store,row,item,entry):
    from .m3u_downloads import lookup,enqueue_resolved
    from .discover_store import claim_start,fence_start,commit_engine
    generation=claim_start(store,row['id'])
    if generation is None or not fence_start(store,row['id'],generation):return False
    try:
        from .movie_options import policy
        selected=policy(store,item) if row['kind']=='movie' else None
        if row['kind']=='episode':
            from .show_options import policy as show_policy
            selected=show_policy(store,item)
            if selected:selected.update(quality=row['quality'],fallback=bool(row['fallback']))
        from .media_holds import bind_request,vod_key
        if not bind_request(store,row['id'],generation,[vod_key(entry['source'],entry['path'])]):return False
        resolved,_url=lookup(app,store,entry['source'],entry['path'])
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not fence_start(store,row['id'],generation,transaction=db):raise _FallbackHandoffCancelled()
            job=enqueue_resolved(app,store,entry['source'],entry['path'],resolved,transaction=db,**({'movie_policy' if row['kind']=='movie' else 'episode_policy':dict(selected,requestId=row['id'])} if selected else {}))
            if not commit_engine(store,row['id'],generation,'vod',job,'Queued with the VOD downloader.',transaction=db):raise _FallbackHandoffCancelled()
    except _FallbackHandoffCancelled:
        return False
    except ValueError:
        ds.update_request(store,row['id'],state='waiting',message='Waiting for release.');return False
    return True


def fallback_vod(app,store,row,b):
    from .view_media.download_sources import choices
    item=b['item'];episode=b.get('episode') or {}
    entries=[e for s in choices(app,store,item)['sources'] if s['type']=='vod' for e in s['entries']]
    entries=[e for e in entries if row['kind']=='movie' or (e.get('season')==episode.get('season') and e.get('number')==episode.get('number') and episode.get('number') is not None)]
    for entry in entries:
        if start_vod(app,store,row,item,entry):return True
    return False

COUNTRY_ALIASES = {'GB': ('uk', 'united kingdom', 'britain', 'british'),
    'US': ('us', 'usa', 'united states', 'america', 'american'),
    'CA': ('ca', 'canada', 'canadian'), 'AU': ('au', 'australia', 'australian'),
    'NZ': ('nz', 'new zealand'), 'IN': ('india', 'indian')}


def series_edition(catalogue):
    country=catalogue.get('country') or {}
    return dict(title=catalogue.get('name') or '', year=str(catalogue.get('premiered') or '')[:4],
                country=country.get('code') or '', countryName=country.get('name') or '')


def youtube_title_matches(title, item, episode=None, edition=None, description=''):
    """Automatic fallback needs positive edition evidence, not a title substring."""
    from .discover_logic import single_episode
    def norm(value):return re.sub(r'[^a-z0-9]+',' ',str(value).casefold()).strip()
    text=norm(title);wanted=norm((edition or {}).get('title') or item.get('title'))
    if not wanted or not (text==wanted or text.startswith(wanted+' ')):return False
    if re.search(r'\b(trailer|teaser|review|reaction|clip|recap|sample|highlights)\b',text):return False
    if not episode:
        years=re.findall(r'\b(?:19|20)\d{2}\b',text[len(wanted):])
        return bool(item.get('year') and years and all(year==str(item['year']) for year in years) and re.search(r'\bfull\s+(movie|film)\b',text))
    if not single_episode(title,episode.get('season'),episode.get('number')):return False
    edition=edition or {};year=str(edition.get('year') or '')
    country=edition.get('country') or ''
    aliases=COUNTRY_ALIASES.get(country,())+(norm(edition.get('countryName')),)
    evidence=text+' '+norm(description)
    if not re.fullmatch(r'\d{4}',year) or not country:return False
    prefix=re.split(r'(?i)\bs\d+\s*e\d+\b',title,maxsplit=1)[0]
    # An upload date elsewhere in the text is not a series premiere year.
    year_in_name=re.search(r'\b'+year+r'\b',norm(prefix))
    described_name=re.search(r'\b'+re.escape(wanted)+r'\b[^.\n]{0,80}\b'+year+r'\b',str(description).casefold())
    if not year_in_name and not described_name:return False
    if not any(alias and re.search(r'\b'+re.escape(alias)+r'\b',evidence) for alias in aliases):return False
    # Before the episode code, only the series name, edition year and region
    # may appear. Reject another series, foreign editions and reboot years.
    remainder=norm(prefix)[len(wanted):].strip()
    for alias in sorted((a for a in aliases if a),key=len,reverse=True):
        remainder=re.sub(r'\b'+re.escape(alias)+r'\b',' ',remainder)
    remainder=re.sub(r'\b'+year+r'\b',' ',remainder)
    return not remainder.strip()


@serialised
def fallback_youtube(app,store,row,b):
    from .youtube_discover import _search
    from .discover_youtube import register_video
    from .discover_store import claim_start,fence_start,commit_engine
    from .downloads import enqueue
    item=b['item'];episode=b.get('episode') or {}
    marker='S%02dE%02d'%(episode['season'],episode['number']) if type(episode.get('season')) is int and type(episode.get('number')) is int else ''
    if row['kind']=='episode' and not marker:return False
    edition=b.get('edition')
    if marker and not edition:
        try:
            tvmaze=item['id'].split(':')[-1] if item.get('id','').startswith('tvmaze:show:') else None
            edition=series_edition(sources.episodes(item.get('externalId',''),store,tvmaze=tvmaze))
        except ValueError:return False
    if marker and (not edition.get('year') or not edition.get('country')):return False
    query=' '.join(filter(None,[item['title'],(edition or {}).get('year') or item.get('year',''),(edition or {}).get('countryName') or (edition or {}).get('country',''),marker,'full episode' if marker else 'full movie']))
    for candidate in _search(query[:160]).get('items',[]):
        if not youtube_title_matches(candidate.get('title',''),item,episode if marker else None,edition,candidate.get('description','')):continue
        from .catalogue import Importer
        result=Importer(app).api('videos',part='snippet,contentDetails',id=candidate['videoId'])
        public=next((v for v in result.get('items',[]) if v.get('id')==candidate['videoId']),{})
        snippet=public.get('snippet') or {}
        if snippet.get('liveBroadcastContent','none')!='none':continue
        if not youtube_title_matches(snippet.get('title',''),item,episode if marker else None,edition,snippet.get('description','')):continue
        parts=re.fullmatch(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?',(public.get('contentDetails') or {}).get('duration',''))
        seconds=sum(int(v or 0)*unit for v,unit in zip(parts.groups(),(3600,60,1))) if parts else 0
        from .movie_options import minimum_for_request
        minimum=minimum_for_request(store,row) if row['kind']=='movie' else None
        if row['kind']=='episode':
            from .show_options import minimum_for_request as show_minimum
            minimum=show_minimum(store,row)
        if seconds<max(600 if marker else 2400,(minimum or 0)*60):continue
        candidate=dict(candidate,title=snippet['title'],channelId=snippet.get('channelId',''),channel=snippet.get('channelTitle',''),published=snippet.get('publishedAt',''))
        if row['kind']=='movie':
            from .movie_options import observe
            observe(store,[item],'YouTube')
        video=register_video(store,candidate)
        generation=claim_start(store,row['id'])
        if generation is None or not fence_start(store,row['id'],generation):return False
        b.update(videoId=video['id'])
        if edition:b['edition']=edition
        from .media_holds import bind_request
        if not bind_request(store,row['id'],generation,['video:'+video['id'],'managed:video:'+video['id']]):return False
        from .discover_store import set_quality_policy
        try:
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if not fence_start(store,row['id'],generation,transaction=db):raise _FallbackHandoffCancelled()
                db.execute('UPDATE discover_requests SET body=? WHERE id=?',(json.dumps(b),row['id']))
                set_quality_policy(store,video['id'],bool(row['fallback']),transaction=db)
                enqueue(store,video,row['quality'],automatic=False,transaction=db)
                if not commit_engine(store,row['id'],generation,'youtube',video['id'],'Queued with the YouTube downloader.',transaction=db):raise _FallbackHandoffCancelled()
        except _FallbackHandoffCancelled:
            return False
        return True
    return False

def tick(app,store):
    from .discover_handoff import recover
    recover(app,store)
    from .acquisition import held
    if held(store):return
    cutoff=time.time()-preferences(store)['release_check_minutes']*60
    with store.connect() as db:
        rows=db.execute("SELECT * FROM discover_requests WHERE json_extract(body,'$.discoveryEngine')=1 AND (state='searching' OR (state='waiting' AND updated<?)) ORDER BY updated LIMIT 3",(cutoff,)).fetchall()
        plans=db.execute('SELECT * FROM discover_series_plan WHERE download_future=1 AND checked<? ORDER BY checked LIMIT 1',(cutoff,)).fetchall()
    from . import series_plan_work as work
    unfinished=work.pending_parent(store,service.today())
    if unfinished:plans=[unfinished]
    for row in rows:schedule(app,store,'request:'+row['id'],lambda r=dict(row):resolve(app,store,r))
    for plan in plans:schedule(app,store,'plan:'+plan['parent'],lambda p=dict(plan):apply_plan(app,store,p,refresh=True))
    sync_fallbacks(app,store)

def sync_fallbacks(app,store):
    with store.connect() as db:
        rows=db.execute("SELECT * FROM discover_requests WHERE json_extract(body,'$.discoveryEngine')=1 AND engine IN ('vod','youtube') AND state IN ('queued','downloading','verifying','cancelling')").fetchall()
    for row in rows:
        table='m3u_downloads' if row['engine']=='vod' else 'downloads'
        with store.connect() as db:job=db.execute('SELECT * FROM '+table+' WHERE id=?',(row['engine_id'],)).fetchone()
        if not job:continue
        mapped={'queued':'queued','running':'downloading','downloading':'downloading','complete':'complete','failed':'failed','cancelled':'cancelled','cancelling':'cancelling'}.get(job['state'])
        if mapped:
            ds.update_request(store,row['id'],state=mapped,message=job['message'],progress=float(job['progress'] or 0))
            if mapped=='complete' and row['engine']=='vod':ds.mark_owned(store,row['episode_key'],row['id'],job['destination'])

def apply_plan(app,store,plan,refresh=False):
    from . import series_plan_work as work
    claim=work.claim(store,plan['parent'])
    if not claim:return
    try:
        unfinished=work.pending_parent(store,service.today(),plan['parent'],include_leased=True)
        # An interrupted ALL pass needs only the saved episode catalogue.
        # Claim first so a competing or disabled pass cannot refresh providers.
        if refresh and not unfinished:prepare(app,store,json.loads(claim['item']))
        all_work=bool(claim['download_all'] and (unfinished or not refresh))
        claim['all_work']=all_work
        deadline=time.monotonic()+work.RUN_SECONDS
        if not all_work:
            with store.connect() as db:rows=db.execute('SELECT * FROM managed_media WHERE parent=? AND removed=0',(claim['parent'],)).fetchall()
            for row in rows:
                if not work.alive(store,claim):return
                b=service.body(dict(row));day=b.get('date','')
                if day and b.get('episode',{}).get('season') is not None and claim['download_future'] and day>=claim['future_from']:
                    try:queue(app,store,row['key'],'future',plan_claim=claim)
                    except work.PlanDeferred:return
                    except ValueError:
                        if not work.alive(store,claim):return
            work.finish(store,claim,service.today(),complete=True,all_work=False)
            return
        while time.monotonic()<deadline and work.alive(store,claim):
            rows=work.batch(store,claim,service.today())
            if not rows:
                work.finish(store,claim,service.today(),complete=True)
                return
            for row in rows:
                if time.monotonic()>=deadline or not work.alive(store,claim):return
                b=service.body(row);day=b.get('date','')
                scope='future' if claim['download_future'] and day>=claim['future_from'] else 'all'
                try:queue(app,store,row['key'],scope,plan_claim=claim)
                except work.PlanDeferred:return
                except ValueError:
                    if not work.alive(store,claim):return
                    from .media_holds import blocked
                    current=records.row(store,row['key'])
                    if current and not (current['hold'] or current['removed'] or blocked(store,['managed:'+row['key']])):raise
                if not work.consumed(store,claim,row['key']):return
    finally:work.release(store,claim)

def save_plan(app,store,parent,field,value,*,renew_all=False):
    if not isinstance(parent,str) or field not in ('all','future') or type(value) is not bool:raise ValueError('Choose a series download option.')
    if renew_all and (field!='all' or not value):raise ValueError('Choose all released episodes.')
    with store.connect() as db:
        row=db.execute("SELECT * FROM managed_media WHERE parent=? AND kind='episode' AND json_extract(body,'$.directTitleKey') IS NULL LIMIT 1",(parent,)).fetchone()
        old=db.execute('SELECT * FROM discover_series_plan WHERE parent=?',(parent,)).fetchone()
        if not row and not old:raise ValueError('Open this series first.')
        plan=dict(old) if old else dict(parent=parent,item=json.dumps(service.body(dict(row))['item']),download_all=0,download_future=0,future_from=service.today(),checked=0)
        if field=='future' and value and not plan['download_future']:plan['future_from']=service.today()
        plan['download_'+field]=int(value)
        db.execute('''INSERT INTO discover_series_plan VALUES(?,?,?,?,?,?) ON CONFLICT(parent) DO UPDATE SET
            item=excluded.item,download_all=excluded.download_all,download_future=excluded.download_future,
            future_from=excluded.future_from,checked=0''',(parent,plan['item'],plan['download_all'],plan['download_future'],plan['future_from'],0))
        if renew_all:
            db.execute('UPDATE discover_plan_work SET all_before=? WHERE parent=?',(time.time(),parent))
        pending=db.execute("SELECT id FROM discover_requests WHERE json_extract(body,'$.planParent')=? AND json_extract(body,'$.plan')=? AND state IN ('waiting','searching','starting')",(parent,field)).fetchall() if not value else []
    from .watchlist import sync_auto_titles
    sync_auto_titles(store)
    if not value:
        from .acquisition import cancel_discover
        for row in pending:cancel_discover(app,store,row['id'])
    else:schedule(app,store,'plan:'+parent,lambda:apply_plan(app,store,plan))
    return dict(downloadAll=bool(plan['download_all']),downloadFuture=bool(plan['download_future']))

def register(app,store,access,csrf):
    records.initialise(store)
    from . import movie_options
    movie_options.initialise(store)
    @app.post('/api/discovery-engine/movie-options')
    def movie_settings():
        denied=access(local_action=True)
        if denied:return denied
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return jsonify(error='Reload the page.'),403
        from .discover import _limited
        if not _limited(app,'movie_options_hits',120):return jsonify(error='Too many movie option requests. Try again shortly.'),429
        payload=request.get_json(silent=True)
        if not isinstance(payload,dict):return jsonify(error='Choose a movie option.'),400
        try:
            item=movie_options.clean(payload.get('item'))
            option=movie_options.save(app,store,item,payload['settings']) if 'settings' in payload else movie_options.ensure(store,item)
            movie_options.schedule_refresh(app,store,option)
            return jsonify(movie_options.snapshot(store,option))
        except (ValueError,TypeError) as exc:return jsonify(error=str(exc)),400
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS discover_series_plan(parent TEXT PRIMARY KEY,item TEXT NOT NULL,download_all INTEGER NOT NULL,download_future INTEGER NOT NULL,future_from TEXT NOT NULL,checked REAL NOT NULL)')
    from . import series_plan_work
    series_plan_work.initialise(store)
    from . import show_options
    show_options.initialise(store)
    @app.post('/api/discovery-engine/show-options')
    def show_settings():
        denied=access(local_action=True)
        if denied:return denied
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return jsonify(error='Reload the page.'),403
        from .discover import _limited
        if not _limited(app,'show_options_hits',120):return jsonify(error='Too many TV show option requests. Try again shortly.'),429
        payload=request.get_json(silent=True)
        if not isinstance(payload,dict):return jsonify(error='Choose a TV show option.'),400
        try:
            item=show_options.clean(payload.get('item'))
            option=show_options.save(app,store,item,payload['settings']) if 'settings' in payload else show_options.ensure(store,item)
            show_options.schedule_refresh(app,store,option)
            return jsonify(show_options.snapshot(store,option))
        except (ValueError,TypeError) as exc:return jsonify(error=str(exc)),400
    @app.post('/api/discovery-engine/series')
    def series():
        denied=access(local_action=True)
        if denied:return denied
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return jsonify(error='Reload the page.'),403
        b=request.get_json(silent=True)
        if not isinstance(b,dict):return jsonify(error='Choose a series download option.'),400
        try:return jsonify(save_plan(app,store,b.get('parent'),b.get('field'),b.get('value')))
        except ValueError as exc:return jsonify(error=str(exc)),400
    @app.post('/api/discovery-engine/title')
    def title():
        denied=access(local_action=True)
        if denied:return denied
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return jsonify(error='Reload the page.'),403
        payload=request.get_json(silent=True)
        if not isinstance(payload,dict):return jsonify(error='Choose a valid title.'),400
        try:return jsonify(title_data(app,store,payload.get('item'),payload.get('offset',0),str(payload.get('query') or '')[:120],payload.get('limit',30),payload.get('season')))
        except (ValueError,TypeError):return jsonify(error='Choose a valid title.'),400
    @app.post('/api/discovery-engine/download')
    def download():
        denied=access(local_action=True)
        if denied:return denied
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return jsonify(error='Reload the page.'),403
        from .discover import _limited
        if not _limited(app,'discovery_download_hits',30):return jsonify(error='Try again shortly.'),429
        payload=request.get_json(silent=True)
        if not isinstance(payload,dict):return jsonify(error='Choose a valid title.'),400
        try:return jsonify(item=queue(app,store,str(payload.get('key','')),vod=payload.get('vod'))),202
        except (ValueError,TypeError) as exc:return jsonify(error=str(exc)),400
