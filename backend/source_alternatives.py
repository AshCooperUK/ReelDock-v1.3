"""Explicit, saved-status alternatives for one failed, verified NZB selection."""
import hashlib
import hmac
import json
import math
import re
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

from flask import jsonify, request
from . import discover_store as records, media_holds
from .discover_logic import acceptable, parse_height, same_external_id, single_episode

TTL=600
LEASE=90
LIMIT=10
_POOL=ThreadPoolExecutor(max_workers=2,thread_name_prefix='source-alternatives')
_LOCK=threading.Lock()
_PENDING=set()


class Unavailable(ValueError):
    pass


def initialise(store):
    with store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS source_alternative_frames(
            request_id TEXT PRIMARY KEY,attempt_key TEXT NOT NULL,account TEXT NOT NULL,
            revision TEXT NOT NULL,state TEXT NOT NULL,body TEXT NOT NULL,updated REAL NOT NULL)''')


def _json(value):
    try:result=json.loads(value or '{}')
    except (TypeError,ValueError):return {}
    return result if isinstance(result,dict) else {}


def _text(value,fallback=''):
    value=str(value or '')
    if re.search(r'(?i)(?:authorization|cookie|api[_-]?key|password|passkey|auth[_-]?token)\s*[:=]',value):return fallback
    value=re.sub(r'(?i)[a-z][a-z0-9+.-]*://\S+|//\S+','[link omitted]',value)
    return ' '.join(value.split())[:300] or fallback


def _name(value):return re.sub(r'[^a-z0-9]+','',str(value or '').casefold())


def _size(value):
    return int(value) if type(value) in (int,float) and math.isfinite(value) and 0<value<=2**53-1 else None


def public_release(candidate):
    return dict(id=str(candidate['id']),title=_text(candidate.get('title'),'Release'),
                quality=_text(candidate.get('quality'),'Unspecified'),size=_size(candidate.get('size')),
                source=_text(candidate.get('provider') or candidate.get('source'),'NZB'))


def rank_candidates(releases,*,wanted,allow_lower,original,failed_ids,failed_titles,minimum,maximum):
    """Quality is authoritative; byte-distance only ranks eligible alternatives."""
    failed_names={_name(title) for title in [*failed_titles,original.get('title','')] if title}
    seen_ids=set(failed_ids);seen_names=set();ranked=[];original_size=_size(original.get('size'))
    selected_height=parse_height(original.get('quality')) or int(wanted)
    for candidate in releases:
        identity=candidate.get('id');title=str(candidate.get('title') or '')
        name=_name(title);size=_size(candidate.get('size'));quality=candidate.get('quality')
        if not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,100}',identity):continue
        if identity in seen_ids or not name or name in failed_names or 'sample' in title.casefold():continue
        if not acceptable(quality,wanted,allow_lower) or size is not None and not minimum<=size<=maximum:continue
        row=public_release(candidate);distance=abs(size-original_size)/original_size if size and original_size else None
        row.update(sizeKnown=size is not None,differencePercent=round((size-original_size)/original_size*100,2) if distance is not None else None,
                   recommended=False,availability='unchecked')
        ranked.append(((0 if parse_height(quality)==selected_height else 1,0 if size else 1,
                        distance if distance is not None else float('inf'),int(wanted)-(parse_height(quality) or 0),name,identity),row,name))
        seen_ids.add(identity)
    result=[]
    for _,row,name in sorted(ranked,key=lambda value:value[0]):
        if name in seen_names:continue
        seen_names.add(name);result.append(row)
        if len(result)>=LIMIT:break
    if result and result[0]['sizeKnown'] and result[0]['differencePercent'] is not None:
        result[0]['recommended']=True
    return result


def _fingerprint(candidate):
    return hashlib.sha256(json.dumps({key:candidate.get(key) for key in ('id','source','kind','external_id','quality','size','title','grab')},sort_keys=True).encode()).hexdigest()


def _exact(context,candidate):
    """Recheck the saved mapping without a title-only provider lookup."""
    body=context['body'];external=candidate.get('externalId') or candidate.get('external_id')
    if body.get('directTitleKey'):
        from .discover_title_releases import title_identity
        original=title_identity((body.get('item') or {}).get('title'));other=title_identity(candidate.get('title'))
        expected=body.get('externalId') or (body.get('item') or {}).get('externalId')
        return bool(original and other and original[:2]==other[:2] and (not expected or same_external_id(external,expected)))
    if body.get('manualTvmaze'):
        episode=body.get('catalogueEpisode') or {}
        return candidate.get('kind')=='manual-episode:'+str(episode.get('id')) and external=='tvmaze:'+str(body['manualTvmaze'])
    if body.get('manualReleaseId'):
        episode=body.get('catalogueEpisode') or {}
        return candidate.get('kind')=='special:'+str(episode.get('id')) and same_external_id(external,body.get('externalId'))
    expected=body.get('externalId') or (body.get('item') or {}).get('externalId')
    if not re.fullmatch(r'[0-9]{1,12}',str(expected or '')) or int(expected)==0 or not same_external_id(external,expected):return False
    episode=body.get('episode')
    return not episode or single_episode(candidate.get('title',''),episode.get('season'),episode.get('number'))


def _account(store):
    from .discover_sources import indexers_for
    from .settings.nzb import selection_limits
    configured=indexers_for(None,store)
    return hashlib.sha256(json.dumps([[(value.get('url'),value.get('api_key')) for value in configured],selection_limits(store)],sort_keys=True).encode()).hexdigest()


def _scope(store,context):
    return hashlib.sha256((_account(store)+context['requestSignature']).encode()).hexdigest()


def _context(store,identity,db=None):
    if not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,100}',identity):raise Unavailable('Choose a saved failed download.')
    if db is None:
        with store.connect() as connection:return _context(store,identity,connection)
    found=db.execute('SELECT * FROM discover_requests WHERE id=?',(identity,)).fetchone()
    if not found:raise Unavailable('This download request is unavailable.')
    row=dict(found);body=_json(row['body'])
    if media_holds.blocked_in(db,media_holds.request_keys_in(db,row)):raise Unavailable('This media was cleared. Use its explicit Download or Retry action first.')
    from .source_choice import eligibility
    marker=eligibility(db,row)
    if not marker:raise Unavailable('This request is no longer waiting for a replacement source.')
    attempts=[dict(value) for value in db.execute('SELECT * FROM discover_attempts WHERE request_id=? ORDER BY id DESC',(identity,))]
    selected=body.get('managedReleaseId') or body.get('manualReleaseId')
    queued=next((value for value in attempts if value['state']=='queued' and value['candidate_id']),None)
    selected=selected or (queued or {}).get('candidate_id') or next((value['candidate_id'] for value in attempts if value['candidate_id']),None)
    original=db.execute('SELECT * FROM discover_candidates WHERE id=?',(selected,)).fetchone() if selected else None
    if not original:raise Unavailable('The original source facts are unavailable. Reopen the title to choose a release.')
    original=dict(original)
    signature_body={key:value for key,value in body.items() if key not in ('sourceChoice','sourceChoiceHistory')}
    signature=hashlib.sha256(json.dumps([identity,row['episode_key'],row['quality'],row['fallback'],row.get('stop_generation'),
        (attempts[0]['id'] if attempts else 0),_fingerprint(original),signature_body],sort_keys=True).encode()).hexdigest()
    failed_ids={value['candidate_id'] for value in attempts if value['candidate_id']};failed_ids.add(original['id'])
    failed_titles={original['title']}
    if failed_ids:
        for value in db.execute('SELECT title FROM discover_candidates WHERE id IN ('+','.join('?' for _ in failed_ids)+')',tuple(failed_ids)):failed_titles.add(value[0])
    return dict(row=row,body=body,attemptKey=marker['attemptKey'],requestSignature=signature,original=public_release(original),failed_ids=failed_ids,failed_titles=failed_titles)


def _base(context):
    return dict(requestId=context['row']['id'],attemptKey=context['attemptKey'],title=_text(context['row']['title'],'Media'),
                revision='',state='idle',loading=False,original=context['original'],items=[],error='',
                message='Search for other releases of this exact title. Indexer results do not guarantee available articles.',refreshAfterMs=0)


def status(store,identity):
    context=_context(store,identity);result=_base(context);account=_scope(store,context)
    with store.connect() as db:frame=db.execute('SELECT * FROM source_alternative_frames WHERE request_id=?',(identity,)).fetchone()
    if not frame or frame['attempt_key']!=context['attemptKey'] or frame['account']!=account or frame['updated']<time.time()-TTL:return result
    saved=_json(frame['body']);result.update({key:saved[key] for key in ('items','message','error') if key in saved})
    state=frame['state']
    if state in ('selecting','consumed'):
        state='failed';result.update(items=[],error='The replacement is being confirmed. Check Activity before trying again.',message='')
    if state=='searching' and frame['updated']<time.time()-LEASE:
        state='failed';result.update(items=[],error='The source search did not finish. Search again.',message='')
    result.update(revision=frame['revision'],state=state,loading=state=='searching',refreshAfterMs=1500 if state=='searching' else 0)
    return result


def _search_candidates(app,store,context):
    from . import discover_acquire as acquire
    row=context['row'];body=dict(context['body'])
    if body.get('directTitleKey'):
        from .discover_title_releases import releases,title_identity
        from .discover_sources import search_nzb
        identity=title_identity(body.get('item',{}).get('title'))
        if not identity:return []
        search_nzb(app,store,'shows',identity[2]+' '+identity[1],0)
        return releases(app,store,body['item'])
    if body.get('manualTvmaze'):
        from . import discover_manual_episodes as helper
        episode=(body.get('catalogueEpisode') or {}).get('id')
        found=helper.search(app,store,body['manualTvmaze'],episode)
        return [dict(helper.selected(store,body['manualTvmaze'],episode,value['id']),provider=value.get('provider')) for value in found['items']]
    if body.get('manualReleaseId'):
        from . import discover_specials as helper
        episode=(body.get('catalogueEpisode') or {}).get('id')
        found=helper.search(app,store,body.get('externalId'),episode)
        return [dict(helper.selected(store,body.get('externalId'),episode,value['id']),provider=value.get('provider')) for value in found['items']]
    body.pop('managedReleaseId',None)
    releases=acquire.search_for(app,store,dict(row,body=json.dumps(body)))
    return [value for value in releases if _exact(context,value)]


def _search(app,store,context,account,revision):
    try:
        if store.reset_pending():return
        current=_context(store,context['row']['id'])
        if current['attemptKey']!=context['attemptKey'] or _scope(store,current)!=account:return
        releases=_search_candidates(app,store,context)
        from .settings.nzb import selection_limits
        minimum,maximum=selection_limits(store)
        items=rank_candidates(releases,wanted=context['row']['quality'],allow_lower=bool(context['row']['fallback']),
            original=context['original'],failed_ids=context['failed_ids'],failed_titles=context['failed_titles'],minimum=minimum,maximum=maximum)
        approval={}
        for item in items:
            candidate=records.candidate(store,item['id'])
            if candidate:approval[item['id']]=_fingerprint(candidate)
        items=[item for item in items if item['id'] in approval]
        body=dict(items=items,approval=approval,error='',message='Choose a replacement. Article availability has not been checked.' if items else 'No different eligible releases were found within the current quality and size settings.')
        state='ready'
    except Exception:
        body=dict(items=[],approval={},error='The source search could not finish. Check the indexer connection and try again.',message='');state='failed'
    try:
        current=_context(store,context['row']['id'])
        if current['attemptKey']!=context['attemptKey'] or _scope(store,current)!=account or store.reset_pending():return
        with store.connect() as db:
            db.execute('UPDATE source_alternative_frames SET state=?,body=?,updated=? WHERE request_id=? AND revision=?',
                (state,json.dumps(body),time.time(),context['row']['id'],revision))
    except Unavailable:pass


def search(app,store,identity):
    context=_context(store,identity);account=_scope(store,context);identity_key=(str(store.directory),identity)
    revision=uuid.uuid4().hex
    with _LOCK:
        if identity_key in _PENDING:return status(store,identity)
        if len(_PENDING)>=8:raise Unavailable('Other source searches are finishing. Try this search again shortly.')
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE');current=_context(store,identity,db)
            if current['attemptKey']!=context['attemptKey']:raise Unavailable('The failed selection changed. Open it again.')
            previous=db.execute('SELECT * FROM source_alternative_frames WHERE request_id=?',(identity,)).fetchone()
            if (previous and previous['state']=='searching' and previous['attempt_key']==context['attemptKey'] and
                    previous['account']==account and previous['updated']>=time.time()-LEASE):
                return status(store,identity)
            db.execute('INSERT OR REPLACE INTO source_alternative_frames VALUES(?,?,?,?,?,?,?)',
                (identity,context['attemptKey'],account,revision,'searching',json.dumps(dict(items=[],approval={},error='',message='Searching alternative releases…')),time.time()))
        _PENDING.add(identity_key)
    try:
        def run():
            try:
                with app.app_context(),store.background_operation():_search(app,store,context,account,revision)
            finally:
                with _LOCK:_PENDING.discard(identity_key)
        _POOL.submit(run)
    except Exception:
        with _LOCK:_PENDING.discard(identity_key)
        raise
    return status(store,identity)


def select(app,store,identity,candidate_id,revision):
    context=_context(store,identity)
    if not isinstance(revision,str) or not re.fullmatch(r'[0-9a-f]{32}',revision):raise Unavailable('Search again before choosing a source.')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');context=_context(store,identity,db)
        frame=db.execute('SELECT * FROM source_alternative_frames WHERE request_id=?',(identity,)).fetchone()
        if not frame or frame['state']!='ready' or frame['revision']!=revision or frame['attempt_key']!=context['attemptKey'] or frame['updated']<time.time()-TTL or frame['account']!=_scope(store,context):
            raise Unavailable('These release choices changed or expired. Search again.')
        saved=_json(frame['body']);approved=saved.get('approval',{}).get(candidate_id) if isinstance(candidate_id,str) else None
        candidate=db.execute('SELECT * FROM discover_candidates WHERE id=?',(candidate_id,)).fetchone() if approved else None
        if not candidate or not hmac.compare_digest(approved,_fingerprint(dict(candidate))):raise Unavailable('That source changed or was not in this search. Search again.')
        db.execute("UPDATE source_alternative_frames SET state='selecting' WHERE request_id=? AND revision=?",(identity,revision))
    from .discover_acquire import replace_failed_source
    try:replacement=replace_failed_source(app,store,context['row'],dict(candidate),context['attemptKey'])
    except Exception:
        with store.connect() as db:
            db.execute("UPDATE source_alternative_frames SET state='failed',body=?,updated=? WHERE request_id=? AND revision=?",
                (json.dumps(dict(items=[],approval={},error='The replacement could not be confirmed. Check Activity before trying again.',message='')),time.time(),identity,revision))
        raise Unavailable('The replacement could not be confirmed. Check Activity before trying again.') from None
    return dict(ok=True,requestId=identity,replacement=dict(id=replacement['id'],state=replacement['state']))


def consume_approval(db,request_id,candidate_id,attempt_key):
    """Called inside the ordinary replacement writer transaction, never a GET."""
    filename=next((row[2] for row in db.execute('PRAGMA database_list') if row[1]=='main'),'')
    if not filename:raise Unavailable('The saved source approval is unavailable. Search again.')
    store=SimpleNamespace(directory=Path(filename).parent)
    context=_context(store,request_id,db)
    frame=db.execute('SELECT * FROM source_alternative_frames WHERE request_id=?',(request_id,)).fetchone()
    if (not frame or frame['state']!='selecting' or frame['attempt_key']!=attempt_key or
            context['attemptKey']!=attempt_key or frame['updated']<time.time()-TTL or frame['account']!=_scope(store,context)):
        raise Unavailable('The failed selection changed. Search again before choosing a source.')
    approved=_json(frame['body']).get('approval',{}).get(candidate_id)
    found=db.execute('SELECT * FROM discover_candidates WHERE id=?',(candidate_id,)).fetchone() if approved else None
    candidate=dict(found) if found else None
    if not candidate or not hmac.compare_digest(approved,_fingerprint(candidate)) or not _exact(context,candidate):
        raise Unavailable('That release is no longer an approved exact match. Search again.')
    from .settings.nzb import selection_limits
    minimum,maximum=selection_limits(store)
    if not rank_candidates([candidate],wanted=context['row']['quality'],allow_lower=bool(context['row']['fallback']),
                           original=context['original'],failed_ids=context['failed_ids'],failed_titles=context['failed_titles'],minimum=minimum,maximum=maximum):
        raise Unavailable('That release no longer meets the selected quality and size settings.')
    db.execute("UPDATE source_alternative_frames SET state='consumed' WHERE request_id=? AND state='selecting'",(request_id,))
    return candidate


def register(app,store,access,csrf):
    initialise(store)
    @app.route('/api/source-alternatives',methods=['GET','POST'])
    def alternatives():
        denied=access(local_action=True)
        if denied:return denied
        if request.method=='POST' and (not csrf() or not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf())):
            return jsonify(error='Reload the page.'),403
        try:
            if request.method=='GET':return jsonify(status(store,request.args.get('request','')))
            body=request.get_json(silent=True)
            if not isinstance(body,dict):raise Unavailable('Choose a source action.')
            action=body.get('action');identity=body.get('requestId')
            allowed={'action','requestId'} if action=='search' else {'action','requestId','candidateId','revision'}
            if set(body)!=allowed:raise Unavailable('Choose a supported source action.')
            if action=='search':return jsonify(search(app,store,identity)),202
            if action=='select':return jsonify(select(app,store,identity,body['candidateId'],body['revision'])),202
            raise Unavailable('Choose Search or a replacement source.')
        except (Unavailable,ValueError) as exc:return jsonify(error=str(exc) if isinstance(exc,Unavailable) else 'The source selection could not be completed. Reopen the title and try again.'),409
