"""Explicit NZB choices for unnumbered TV releases with unavailable episode metadata.

An umbrella TVDB ID alone is insufficient: bind the release's full title and year
as well. Never guess an episode number or enable series monitoring here.
"""
import hashlib
import json
import re
import time
import uuid
from urllib.parse import parse_qs, urlsplit
from . import discover_sources as sources, discover_store as ds
from .media_management import store as records


def title_identity(title):
    text=re.sub(r'[._]+',' ',str(title or ''))
    if re.search(r'(?i)\b(?:S[0-9]{1,4}(?:E[0-9]{1,4})?|season\s*\d+|\d{4}[ -]\d{2}[ -]\d{2})\b',text):return None
    found=re.search(r'\b((?:19|20)\d{2})\s+(?=(?:2160|1080|720|480)[pi]\b|HDTV\b|WEB\b|DVDRIP\b|BLURAY\b)',text,re.I)
    if not found:return None
    name=text[:found.start()].strip(' -()')
    return (re.sub(r'[^a-z0-9]+','',name.casefold()),found[1],name) if name else None


def title_key(item):
    identity=title_identity(item.get('title')) if item.get('kind')=='show' else None
    return hashlib.sha256(json.dumps(identity[:2]).encode()).hexdigest()[:24] if identity else ''


def releases(app,store,item):
    identity=title_identity(item.get('title'))
    if item.get('kind')!='show' or not title_key(item) or not str(item.get('externalId') or '').isdigit():return []
    try:indexers=sources.indexers_for(app,store)
    except ValueError:return []
    with store.connect() as db:
        rows=db.execute("SELECT * FROM discover_candidates WHERE kind='show' AND external_id=? AND created>? ORDER BY created DESC,id",(item['externalId'],time.time()-86400)).fetchall()
    result=[]
    for row in rows:
        row=dict(row);other=title_identity(row['title'])
        if not other or other[:2]!=identity[:2] or 'sample' in row['title'].casefold():continue
        grab=urlsplit(row['grab']);params=parse_qs(grab.query)
        # A cached choice must still belong to a currently enabled account.
        account=next((i for i in indexers if grab.scheme=='https' and grab.hostname==urlsplit(i['url']).hostname
                      and i['api_key'] in [*params.get('apikey',[]),*params.get('r',[])]),None)
        if not account:continue
        result.append(dict(row,externalId=row['external_id'],provider=sources._provider_name(account)))
    return result


def remember(app,store,item):
    if not releases(app,store,item):return False
    identity=title_identity(item['title']);key=title_key(item)
    records.remember(store,'title-release:'+item['externalId']+':'+key,item['id'],'episode',dict(
        item=dict(item,year=identity[1]),episode=dict(name=identity[2],season=None,number=None),date='',dateLabel='',
        automatic=False,directTitleKey=key))
    return True


def queue(app,store,r,release_id):
    from .acquisition import ensure_open
    from .discover_acquire import resolve_release
    from .media_management import service
    ensure_open(store)
    b=service.body(r);item=b['item']
    choice=next((c for c in service.candidates(app,store,r)['items'] if c['id']==release_id and not c['reason']),None)
    selected=next((c for c in releases(app,store,item) if c['id']==release_id),None)
    if not choice or not selected:raise ValueError('Release is unavailable or no longer eligible. Search again.')
    from .media_holds import resume
    resume(store,'managed:'+r['key'])
    # A cancelled title resumes only through this explicit, validated selection.
    with store.connect() as db:
        db.execute("UPDATE discover_requests SET state='cancelled',message='Superseded by explicit selection.' WHERE episode_key=? AND state='waiting'",(r['key'],))
    row,created=ds.insert_request(store,dict(idempotency=str(uuid.uuid4()),kind='episode',title=b['episode']['name'],
        quality=r['quality'],fallback=bool(r['fallback']),manual=True,episode_key=r['key'],body=dict(
            item=item,itemId=item['id'],externalId=item['externalId'],year=title_identity(item['title'])[1],
            source='NZB first',managedReleaseId=release_id,directTitleKey=b['directTitleKey'])))
    if created:resolve_release(app,store,row,[selected])
    return [row]
