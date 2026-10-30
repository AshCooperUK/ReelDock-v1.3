"""Immediate local search; finish an empty frame when provider checks complete."""
import hashlib
import json
import time
import uuid
from datetime import datetime,timezone
from . import saved_catalogue as catalogue,global_search
from .search_snapshot import PREFIX,TTL,_page
from .identity import movie_identity,valid_card
from .ordering import ordered
from .adapters import PAGE

MAX_WAIT=900
SAVED_WINDOW=1000
ALL_SOURCE=dict(id='all',label='All sources',media=['all','movies','shows','youtube'],
    sorts=['new','oldest','title','title_desc','year','year_asc','rating','rating_asc'])

def browse_saved(app,store,args):
    """A bounded saved-observations landing page; never queue provider work."""
    sig=catalogue.signature(store)
    observed=catalogue.observations(app,store,sig,args['media'],SAVED_WINDOW)
    current=catalogue.signature(store)
    if current!=sig:
        sig=current;observed.update(items=[],warming=True,counts={},indexed=False,updated=0)
    limited=observed['limited'];cards=observed['items']
    items=ordered(cards,args['sort']);offset=args['offset'];following=offset+PAGE if offset+PAGE<len(items) else None
    scope=('Latest '+str(SAVED_WINDOW)+' saved observations. ' if limited else str(len(items))+' saved titles. ')
    scope+='No provider requests are made by this view. Reuses saved source results and known server media; new observations appear automatically. '
    if not items:scope+='No titles have been observed in these filters yet. Choose a source to browse its catalogue or use Search. '
    if not observed['backgroundEnabled']:scope+='Background catalogue pre-download is off; browsing sources still contributes saved results.'
    state='warming' if observed['warming'] else 'ready' if items else 'empty' if observed['indexed'] or observed['localKnown'] else 'unindexed'
    keys=[_identity(item) for item in items]
    signature=sig+':'+store.get('catalogue_generation','0')
    revision=hashlib.sha256(json.dumps(items,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return dict(items=items[offset:offset+PAGE],label='Saved across your sources',collection='',nextOffset=following,nextCursor=None,
        completeSet=following is None,windowLimited=limited,savedCatalogue=True,loading=bool(observed['warming'] and not items),refreshing=bool(observed['warming'] and items),
        observationState=state,observationSignature=signature,observationRevision=revision,observationIds=keys,
        observationCounts=observed['counts'],refreshAfterMs=2000 if observed['warming'] else 10000,
        freshness=dict(source='Saved source observations',fetchedAt=datetime.fromtimestamp(observed['updated'],timezone.utc).isoformat() if observed['updated'] else None,scope=scope))

def _identity(item):
    return movie_identity(item['action']['item']) if item['kind']=='movie' and item['action']['type']=='discover' else item['id']

def _checks(page,saved,current):
    pending=current.get('status') in ('queued','checking','busy')
    known={_identity(item) for item in saved.get('items',[])}
    added=(current.get('status')=='complete' and current.get('checkedAt',0)>saved.get('checkedAt',0)
        and bool(set(current.get('resultKeys',[]))-known))
    return dict(page,providerChecksPending=pending,providerChecksComplete=current.get('status')=='complete',
        searchUpdatesAvailable=added,refreshAfterMs=2000 if pending else 0,
        warnings=list(dict.fromkeys([*page.get('warnings',[]),*current.get('warnings',[])])))

def search(app,store,args):
    from .. import discovery_engine as engine,discover_sources as sources
    from .search_snapshot import collect
    catalogue.initialise(store);sig=catalogue.signature(store)
    generation=store.get('catalogue_generation','0')
    canonical=dict(args,offset=0,cursor=None,sort='source');canonical.pop('fresh',None)
    key='saved:query:v2:'+sig+':'+generation+':'+hashlib.sha256(args['q'].casefold().encode()).hexdigest()
    if args.get('cursor'):
        with store.connect() as db:row=db.execute('SELECT body,updated FROM view_media_cache WHERE key=?',(PREFIX+args['cursor']['snapshot'],)).fetchone()
        if not row or row['updated']<time.time()-86400:raise ValueError('This search has expired. Search again.')
        saved=json.loads(row['body'])
        if saved.get('searchKey')!=key:raise ValueError('This search has changed. Search again.')
        return _checks(dict(_page(saved,args['cursor']['snapshot'],args),completeSet=True),saved,catalogue.state(store,key)[0])
    job_key='engine:global-search:saved:'+key;job=(str(store.directory),job_key)
    def update():
        current,_=catalogue.state(store,key)
        if current.get('retryAt',0)>time.time() or not catalogue.valid(store,sig,generation):return
        catalogue.save_state(store,key,dict(status='checking',startedAt=time.time(),warnings=current.get('warnings',[])))
        try:result=collect(app,store,dict(canonical,_checkpoint=lambda:catalogue.valid(store,sig,generation)))
        except Exception:
            if catalogue.valid(store,sig,generation):catalogue.save_state(store,key,dict(status='failed',retryAt=time.time()+60,
                warnings=['Provider search could not finish. Saved matches are retained; search again to retry.']))
            return
        if catalogue.valid(store,sig,generation):
            catalogue.remember_search(store,sig,result['items'],args['q'])
            catalogue.save_state(store,key,dict(status='complete',checkedAt=time.time(),retryAt=time.time()+(60 if result.get('incomplete') else TTL),
                resultKeys=list(dict.fromkeys(_identity(item) for item in result['items'])),warnings=result.get('warnings',[])))
    current,stamp=catalogue.state(store,key)
    with engine._LOCK:running=job in engine._PENDING
    if not running and current.get('retryAt',0)<=time.time() and (current.get('status') not in ('queued','checking') or stamp+5<=time.time()):
        catalogue.save_state(store,key,dict(status='queued',startedAt=time.time(),warnings=current.get('warnings',[])))
        accepted=engine.schedule(app,store,job_key,update)
        if accepted is False:
            with engine._LOCK:running=job in engine._PENDING
            if not running:catalogue.save_state(store,key,dict(status='busy',retryAt=time.time()+5,warnings=['Search is busy. Retrying shortly.']))
    current,_=catalogue.state(store,key)
    waiting=current.get('status') in ('queued','checking','busy')
    snapshot_key=key+':'+args['sort']
    with store.connect() as db:previous=db.execute('SELECT body,updated FROM view_media_cache WHERE key=?',(snapshot_key,)).fetchone()
    prior=json.loads(previous['body']) if previous else None
    # Once there are visible matches, polling must never reorder the frame.
    # An empty provisional frame can be completed once checks have finished.
    if prior and not args.get('fresh') and previous['updated']>time.time()-TTL:
        if not prior.get('awaitingProviders') or waiting and time.time()-prior['awaitingSince']<MAX_WAIT:
            return _checks(dict(_page(prior,prior['snapshot'],args),completeSet=True,loading=bool(prior.get('awaitingProviders'))),prior,current)
    items=global_search.merge([item for kind in ('movie','show','video','channel') for item in catalogue._cards(store,sig,args['q'],kind=kind)])
    awaiting=not items and waiting
    warnings=list(current.get('warnings',[]))
    if prior and prior.get('awaitingProviders') and time.time()-prior['awaitingSince']>=MAX_WAIT:
        awaiting=False;warnings.append('Provider checks are taking longer than expected. Search again to retry.')
    try:names=[sources._provider_name(indexer) for indexer in sources.indexers_for(app,store)]
    except ValueError:names=[]
    coverage=' NZB indexers: '+(', '.join(names) if names else 'none enabled')+'.'
    scope=str(len(items))+' saved matches (up to 1,000 per media type).'+coverage
    scope+=(' Checking providers for this search…' if awaiting else ' Background checks save additional matches; repeat Search to include them.' if items else ' Provider checks finished. No saved matches were found.' if current.get('status')=='complete' else ' Provider checks are incomplete; see source alerts.')
    identity=uuid.uuid4().hex
    since=prior['awaitingSince'] if prior and prior.get('awaitingProviders') and not args.get('fresh') else time.time()
    saved=dict(items=items,warnings=warnings,searchKey=key,snapshot=identity,awaitingProviders=awaiting,awaitingSince=since,checkedAt=current.get('checkedAt',0),
        freshness=dict(source='Saved server catalogue',fetchedAt=datetime.now(timezone.utc).isoformat(),scope=scope))
    with store.connect() as db:
        db.executemany('INSERT OR REPLACE INTO view_media_cache VALUES(?,?,?)',[(snapshot_key,json.dumps(saved),time.time()),(PREFIX+identity,json.dumps(saved),time.time())])
        db.execute('DELETE FROM view_media_cache WHERE key LIKE ? AND updated<?',(PREFIX+'%',time.time()-86400))
    return _checks(dict(_page(saved,identity,args),completeSet=True,loading=awaiting),saved,current)
