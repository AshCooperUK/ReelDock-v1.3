"""Compose catalogue, policies, real requests and library files without synthetic states."""
import json
import re
import time
import uuid
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from . import store as records
from .. import discover_store as ds, discover_sources as sources
from ..discover_logic import same_external_id,episode_identity,movie_identity,acceptable,release_pairs,single_episode

def today():return datetime.now(ZoneInfo('Europe/London')).date().isoformat()
def body(row):return json.loads(row['body'])

def enrol(app,store,item,episode=None,*,refresh=True):
    item=ds.clean_item(item)
    if item['kind']=='show':
        if episode is not None:
            if not item.get('externalId') or type(episode.get('season')) is not int or type(episode.get('number')) is not int:
                raise ValueError('A verified show identity and episode numbers are required.')
            key=episode_identity(item['externalId'],episode['season'],episode['number'])
            key=records.remember(store,key,item['id'],'episode',dict(item=item,episode=episode,date=episode.get('airdate',''),dateLabel='Air date',automatic=True))
            return listing(app,store,key=key)
        tvmaze=item['id'].split(':')[-1] if item['id'].startswith('tvmaze:show:') and not item.get('externalId') else None
        data=sources.episodes(item.get('externalId',''),store,tvmaze=tvmaze)
        from ..discovery_engine import series_edition
        edition=series_edition(data)
        for e in data['episodes']:
            numbered=type(e.get('number')) is int and type(e.get('season')) is int
            key=episode_identity(item['externalId'],e['season'],e['number']) if item.get('externalId') and numbered else 'tvmaze:'+str(data['showId'])+':episode:'+str(e['id'])
            records.remember(store,key,item['id'],'episode',dict(item=item,episode=e,date=e.get('airdate',''),dateLabel='Air date',automatic=bool(item.get('externalId') and numbered),tvmaze=data['showId'],edition=edition))
    elif item['kind']=='movie':
        from ..view_media.providers import details
        # Refresh metadata on enrolment; unknown digital availability stays undated.
        detail=details(store,item['id']) if refresh and item['id'].startswith('tmdb:movie:') else item
        if refresh and item['id'].startswith('tmdb:movie:'):
            from ..view_media.providers import TMDB
            try:
                dates=TMDB(store).get('movie/'+item['id'].split(':')[-1]+'/release_dates').get('results',[])
                region=next((r for r in dates if r.get('iso_3166_1')=='GB'),None) or next((r for r in dates if r.get('iso_3166_1')=='US'),{})
                digital=sorted(str(r['release_date'])[:10] for r in region.get('release_dates',[]) if r.get('type')==4 and r.get('release_date'))
                detail['digitalReleaseDate']=digital[0] if digital else ''
            except Exception:pass # Optional dates never block existing title details.
        item=ds.clean_item({**item,**detail})
        key=movie_identity(item['externalId']) if item.get('externalId') else item['id']
        records.remember(store,key,item['id'],'movie',dict(item=item,date=detail.get('digitalReleaseDate',''),cinemaDate=detail.get('releaseDate',''),dateLabel='Digital release',automatic=bool(item.get('externalId'))))
    elif item['kind']=='video':
        from ..discover_youtube import register_video
        with store.connect() as db:saved=db.execute('SELECT data FROM videos WHERE id=?',(item['videoId'],)).fetchone()
        video=json.loads(saved['data']) if saved else register_video(store,item)
        item['videoId']=video['id']
        item['channelId']=video.get('channel') or item.get('channelId','')
        item['image']=video.get('thumbnail') or item.get('image','')
        with store.connect() as db:channel=db.execute('SELECT data FROM channels WHERE id=?',(item['channelId'],)).fetchone()
        if channel:item['channel']=json.loads(channel['data']).get('name') or item.get('channel','')
        records.remember(store,'video:'+video['id'],item['id'],'video',dict(item=item,date=str(video.get('published') or video.get('date') or '')[:10],dateLabel='Published',automatic=video.get('state')=='published'))
    else:raise ValueError('Choose a movie, episode or video.')
    return listing(app,store,parent=item['id'])

def request_for(store,key):
    with store.connect() as db:
        r=db.execute('SELECT * FROM discover_requests WHERE episode_key=? ORDER BY created DESC LIMIT 1',(key,)).fetchone()
    return dict(r) if r else None

def _video_file(app,store,video):
    # local_file resolves paths before returning them. Keep the original
    # reservation here so even an internal symlink cannot hide a linked source.
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}',str(video)):return None
    with store.connect() as db:row=db.execute('SELECT path FROM media_files WHERE id=?',(video,)).fetchone()
    root=Path(app.config['MEDIA_DIR']).absolute()
    from ..media_library import contained
    for value in ([row['path']] if row else [])+[video+suffix for suffix in ('.mp4','.webm','.mkv')]:
        if not isinstance(value,str) or not value:continue
        try:
            p=root/value
            relative=p.relative_to(root)
            if '..' in p.parts or any(part.is_symlink() for part in (p,*p.parents)):continue
            if contained(root,str(relative))!=p:continue
            if p.is_file() and p.stat().st_size:return p
        except (OSError,ValueError):pass
    return None

def library_file(app,store,r,*,heal=True):
    if r['kind']=='video':
        video=_video_file(app,store,body(r)['item'].get('videoId',''))
        if video:return video
    q=request_for(store,r['key'])
    owned=ds.owned(store,r['key'])
    if q and q.get('engine')=='youtube' and not (owned and owned.get('state')=='complete' and owned.get('path')):
        video=_video_file(app,store,json.loads(q['body']).get('videoId',''))
        if video:return video
    paths=[owned['path']] if owned and owned.get('state')=='complete' and owned.get('path') else []
    # Publication can finish before the worker records ownership. Read the
    # settled job so the popup does not keep offering Cancel for a saved movie.
    if not paths and q and not json.loads(q.get('body') or '{}').get('replaces') and q.get('engine')=='nzb' and q.get('engine_id') and q['state'] not in ('cancelled','failed'):
        from ..discover_acquire import status_for_job
        job=status_for_job(app,store,q['engine_id']) or {}
        if job.get('phase')=='stopped' and job.get('progress_stage')=='complete' and not job.get('error'):
            paths=job.get('published') or []
    # A rename crash can leave ownership pointing at the old path; the journaled
    # destination goes through the same confinement checks and heals records.
    b=body(r)
    if b.get('localPath'):paths.append(b['localPath'])
    journal=b.get('renameJournal') or {}
    journal_new=journal.get('new') if isinstance(journal,dict) else None
    if journal_new:paths.append(journal_new)
    from ..storage_layout import download_base
    from ..downloaders.torrent.media import VIDEO
    if not download_base(store):return None
    root=Path(download_base(store)).absolute()
    # Reject every symlink component, including intermediate directories.
    for path in paths:
        if not isinstance(path,str) or not path:continue
        p=Path(path)
        if not p.is_absolute():p=root/p
        if p.suffix.lower() not in VIDEO:continue
        try:
            relative=p.relative_to(root)
            cursor=root;linked=False
            for part in relative.parts:
                cursor=cursor/part
                if cursor.is_symlink():linked=True;break
            from .files import checked
            if not linked and checked(store,p).is_file() and p.stat().st_size:
                if heal and journal_new and str(p)==str(journal_new):heal_rename(store,r,journal,p)
                return p
        except (ValueError,OSError):pass
    return None


def heal_rename(store,r,journal,p):
    """Adopt a journaled rename destination when ownership missed the update."""
    from ..discover_store import mark_owned,owned
    from ..storage_layout import download_base
    mark_owned(store,r['key'],(owned(store,r['key']) or {}).get('request_id',''),str(p))
    root=Path(download_base(store) or '')
    old=str(journal.get('old',''))
    with store.connect() as db:
        candidates=[old]
        try:candidates.append(str(Path(old).relative_to(root)))
        except (ValueError,OSError):pass
        try:db.execute('UPDATE media_files SET path=? WHERE path IN (?,?)',(str(p.relative_to(root)),*candidates))
        except (ValueError,OSError):pass
    records.patch_body(store,r['key'],renameJournal=None,localPath=None)

def effective_monitor(store,r):
    if r['removed'] or r['hold']:return False
    if body(r).get('directTitleKey'):return False
    series=records.series_for_key(store,r['key'],r) if r['kind']=='episode' else None
    if series and series['monitoring']=='off':return False
    if r['monitor']!=-1:return bool(r['monitor'])
    if series and series['monitoring']=='all':return body(r).get('episode',{}).get('season',0)>0
    b=body(r)
    if r['kind']=='episode' and b['item'].get('externalId'):
        e=b['episode'];season=e.get('season')
        return any(rule['enabled'] and rule['showId']==b['item']['externalId'] and season and (season>=rule['season'] if rule['scope']=='future' else season==rule['season']) for rule in ds.rules(store))
    if r['kind']=='video':
        from ..channel_downloads import preferences,eligible
        with store.connect() as db:v=db.execute('SELECT data FROM videos WHERE id=?',(b['item'].get('videoId',''),)).fetchone()
        if v:
            video=json.loads(v['data']);p=preferences(store,video.get('channel',''))
            return bool(eligible(video,p) and not p.get('subscription_paused'))
    return False

def effective_policy(store,r):
    if r['kind']=='movie':
        from ..movie_options import policy
        selected=policy(store,body(r)['item'])
        if selected:return dict(r,quality=selected['quality'],fallback=int(selected['fallback']))
    if r['monitor']!=-1:return r
    result=dict(r);b=body(r);prefs=ds.preferences(store)
    result.update(quality=prefs['quality'],fallback=int(prefs['fallback']))
    if r['kind']=='video':
        from ..channel_downloads import preferences
        result['quality']=preferences(store,b['item'].get('channelId',''))['quality']
    elif r['kind']=='episode':
        season=b.get('episode',{}).get('season')
        matching=[rule for rule in ds.rules(store) if rule['enabled'] and rule['showId']==b['item'].get('externalId') and season is not None and (season>=rule['season'] if rule['scope']=='future' else season==rule['season'])]
        if matching:result.update(quality=matching[0]['quality'],fallback=int(matching[0]['fallback']))
    series=records.series_for_key(store,r['key'],r) if r['kind']=='episode' else None
    if series:result.update(quality=series['quality'],fallback=series['fallback'])
    if r['kind']=='episode':
        from ..show_options import find,policy
        option=find(store,b['item'])
        if option and (option['quality'] is not None or (not series and not matching)):
            selected=policy(store,b['item'])
            result.update(quality=selected['quality'],fallback=int(selected['fallback']))
    return result


def compose(app,store,r,probe=True):
    r=effective_policy(store,r)
    b=body(r);item=b['item'];e=b.get('episode',{});q=request_for(store,r['key']);p=library_file(app,store,r)
    if r['kind']=='video':
        with store.connect() as db:d=db.execute('SELECT * FROM downloads WHERE id=?',(item.get('videoId',''),)).fetchone()
        if d:q=dict(d)
    active=q and q['state'] in ('searching','starting','queued','downloading','verifying','paused','cancelling','importing')
    estimated=0;progress=(q or {}).get('progress',0)
    if active and q.get('engine') in ('','nzb'):
        with store.connect() as db:
            candidate=db.execute("SELECT c.size FROM discover_attempts a JOIN discover_candidates c ON c.id=a.candidate_id WHERE a.request_id=? AND a.state='queued' ORDER BY a.id DESC LIMIT 1",(q['id'],)).fetchone()
        if candidate:estimated=max(0,candidate['size'])
    monitor=effective_monitor(store,r);day=b.get('date','');future=bool(day and day>today())
    state='Upcoming' if future else 'Date TBA' if not day else 'Waiting for release' if monitor else 'Not monitored'
    if r['hold']:state='Acquisition paused'
    if q and q['state']=='failed':state='Needs attention'
    if active:
        state={'starting':'Searching','verifying':'Processing','queued':'Queued','downloading':'Downloading','searching':'Searching','paused':'Paused','cancelling':'Cancelling','importing':'Importing'}.get(q['state'],q['state'])
        if q.get('engine')=='nzb' and q.get('engine_id'):
            from ..discover_acquire import status_for_job
            j=status_for_job(app,store,q['engine_id']) or {}
            estimated=j.get('estimated_bytes') or estimated
            if j.get('transfer_progress') is not None:
                measured=j['transfer_progress']*100
                progress=max(progress,measured) if j.get('stale_progress') else measured
            if q['state']!='cancelling':
                state={'assembling':'Assembling','repairing':'Repairing','unpacking':'Extracting','extracting':'Extracting','publishing':'Publishing library','processing':'Processing','choose':'Needs attention','paused':'Paused','failed':'Needs attention'}.get(j.get('phase') if j.get('phase') in ('choose','paused','failed') else j.get('progress_stage') or j.get('phase') or j.get('state'),state)
            if q['state']!='cancelling' and j.get('phase')=='stopped' and j.get('progress_stage')=='complete' and not j.get('error') and not p:
                state='Saved file unavailable'
    if r['kind']=='video' and active and q.get('phase'):
        state={'processing':'Processing','transcoding':'Processing','merging':'Processing','encoding':'Processing','sponsors':'Processing','metadata':'Publishing metadata'}.get(q['phase'],state)
    from .files import metadata_ready,quality as file_quality
    metadata=True
    if p and r['kind']!='video':metadata=metadata_ready(store,p)
    replacing=bool(q and json.loads(q.get('body') or '{}').get('replaces'))
    if p and not (active and replacing):state='In library' if metadata else 'Saved · metadata incomplete'
    if r['removed']:state='Removed from management'
    from ..media_duration import fields as duration_fields
    duration=duration_fields(minutes=e.get('runtime')) if e else duration_fields(seconds=item.get('durationSeconds'),minutes=item.get('runtimeMinutes'),clock=item.get('duration'))
    return dict(key=r['key'],kind=r['kind'],title=b.get('displayTitle') or e.get('name') or item['title'],series=item['title'] if e else '',image=e.get('image') or item.get('image',''),**duration,
        season=e.get('season'),number=e.get('number'),date=day,dateLabel=b.get('dateLabel'),cinemaDate=b.get('cinemaDate',''),status=state,
        monitored=monitor,policy='Inherited' if r['monitor']==-1 else 'Custom',hold=bool(r['hold']),removed=bool(r['removed']),automatic=b.get('automatic',False),
        quality=r['quality'],fallback=bool(r['fallback']),fileQuality=file_quality(store,r,p,probe=probe),
        file=bool(p),filename=p.name if p else '',bytes=_safe_size(p),estimatedBytes=estimated,metadataReady=metadata,
        active=bool(active),requestId=(q or {}).get('id'),progress=progress,
        message=r['message'] or (q or {}).get('message',''),lastCheck=r['last_check'],nextCheck=r['next_check'],
        videoId=item.get('videoId',''),channelId=item.get('channelId',''),channel=item.get('channel',''))

def _safe_size(p):
    if not p: return 0
    try: return p.stat().st_size
    except OSError: return 0


def listing(app,store,parent=None,kind=None,key=None):
    filters=[];values=[]
    for field,value in (('parent',parent),('kind',kind),('key',key)):
        if value is not None:filters.append(field+'=?');values.append(value)
    with store.connect() as db:
        rows=db.execute('SELECT * FROM managed_media'+(' WHERE '+' AND '.join(filters) if filters else '')+' ORDER BY updated DESC LIMIT 2000',values).fetchall()
    result=dict(items=[compose(app,store,dict(r),probe=False) for r in rows])
    if parent and rows and rows[0]['kind']=='episode':
        policy=records.series_for_key(store,rows[0]['key'],dict(rows[0]))
        if not policy:
            effective=effective_policy(store,dict(rows[0]))
            policy=dict(quality=effective['quality'],fallback=bool(effective['fallback']),monitoring='inherit')
        result['seriesSettings']=dict(quality=policy['quality'],fallback=bool(policy['fallback']),monitoring=policy['monitoring'])
    return result

def candidates(app,store,r):
    r=effective_policy(store,r)
    b=body(r);item=b['item'];e=b.get('episode')
    if r['key'].startswith('local:') and not b.get('automatic'):
        return dict(items=[],message='This local file has no verified '+r['kind']+' identity in its metadata, so release search is unavailable.')
    if r['kind']=='video':return dict(items=[],message='YouTube downloads use the existing video quality controls, not indexer releases.')
    if not b.get('automatic') and not e:return dict(items=[],message='No verified movie identity is available. Use the existing source selector to choose a provider file.')
    warnings=[]
    if b.get('directTitleKey'):
        from ..discover_title_releases import releases as title_releases
        releases=title_releases(app,store,item)
    elif not b.get('automatic'):
        from .. import discover_manual_episodes,discover_specials
        result=discover_manual_episodes.search(app,store,b['tvmaze'],e['id']) if not item.get('externalId') else discover_specials.search(app,store,item['externalId'],e['id'])
        releases=result['items'];warnings=result.get('warnings',[])
    else:releases=sources.search_episode(app,store,item['externalId'],e['season'],e['number']) if e else sources.search_movie(app,store,item['externalId'])
    result=[]
    for c in releases:
        reason=''
        if 'sample' in c['title'].lower():reason='Sample content excluded'
        elif b.get('automatic') and not same_external_id(c.get('externalId'),item.get('externalId')):reason='Different catalogue identity'
        elif e and b.get('automatic') and not single_episode(c['title'],e['season'],e['number']):reason='Different episode or unverified pack coverage'
        elif not acceptable(c.get('quality'),r['quality'],r['fallback']):reason='Outside the selected quality policy'
        if not reason:
            from ..settings.nzb import selection_limits
            minimum,maximum=selection_limits(store)
            if c.get('size') and not minimum<=int(c['size'])<=maximum:reason='Outside the configured NZB size range'
        result.append(dict(id=c['id'],title=c['title'],quality=c.get('quality','Unknown'),size=c.get('size',0),source=c.get('provider') or c.get('source') or 'NZB',reason=reason))
    from .files import quality as file_quality
    return dict(items=result,fileQuality=file_quality(store,r,library_file(app,store,r)),
                message=' '.join(warnings) if result else 'No matching releases. Waiting for release. '+' '.join(warnings))

def queue(app,store,r,release_id=None,*,replace=False,automatic=False):
    r=effective_policy(store,r)
    b=body(r);item=b['item'];e=b.get('episode')
    if r['removed']:raise ValueError('Restore this entry first.')
    if automatic and (replace or release_id or not b.get('automatic')):
        raise ValueError('Automatic acquisition requires a verified catalogue identity.')
    if replace:return queue_replacement(app,store,r,release_id)
    if library_file(app,store,r):raise ValueError('Already saved in the library.')
    if r['key'].startswith('local:') and not b.get('automatic'):raise ValueError('This local file has no verified catalogue identity, so release search is unavailable.')
    if r['kind']=='video':
        if automatic:
            from ..downloads import enqueue
            with store.connect() as db:video=db.execute('SELECT data FROM videos WHERE id=?',(item.get('videoId',''),)).fetchone()
            if video:enqueue(store,json.loads(video[0]),r['quality'],automatic=True)
            return []
        from ..discover import _queue_youtube
        return _queue_youtube(store,item,dict(quality=r['quality'],fallback=bool(r['fallback']),requestId=str(uuid.uuid4())))
    if b.get('directTitleKey'):
        from ..discover_title_releases import queue as queue_title
        return queue_title(app,store,r,release_id)
    if not b.get('automatic'):
        if not release_id:raise ValueError('This title requires manual release selection.')
        from .. import discover_manual_episodes,discover_specials
        return discover_manual_episodes.queue(app,store,b['tvmaze'],e['id'],release_id,str(uuid.uuid4())) if not item.get('externalId') else discover_specials.queue(app,store,item['externalId'],e['id'],release_id,str(uuid.uuid4()))
    from ..discover_acquire import queue_manual,process,resolve_release
    selected=None
    if release_id:
        matches=candidates(app,store,r)['items'];choice=next((c for c in matches if c['id']==release_id),None)
        if not choice or choice['reason']:raise ValueError('Release is unavailable or no longer eligible. Search again.')
        selected=ds.candidate(store,release_id)
        if not selected or time.time()-selected['created']>86400:raise ValueError('Release expired.')
        selected=dict(selected,externalId=selected['external_id'])
    # Replace only an idle waiting request, preserving its attempt history.
    if not automatic:
        with store.connect() as db:
            db.execute("UPDATE discover_requests SET state='cancelled',message='Superseded by explicit search.' WHERE episode_key=? AND state='waiting'",(r['key'],))
    rows=queue_manual(store,item,[e] if e else [],r['quality'],bool(r['fallback']),'NZB first',str(uuid.uuid4()),**({'automatic':True} if automatic else {}))
    for request in rows:
        if not request.get('blocked'):
            if selected:
                payload=json.loads(request['body']);payload['managedReleaseId']=release_id
                with store.connect() as db:db.execute('UPDATE discover_requests SET body=? WHERE id=?',(json.dumps(payload),request['id']))
                request['body']=json.dumps(payload)
                resolve_release(app,store,request,[selected])
            else:process(app,store,request)
    return rows


def queue_replacement(app,store,r,release_id):
    """Explicit manual selection keeps ordinary queue/ownership guards intact."""
    from . import files
    from ..acquisition import ensure_open
    from ..discover_acquire import _catalogue_body,resolve_release
    ensure_open(store)
    if r['kind']=='video':raise ValueError('YouTube videos cannot be replaced here.')
    if r['hold']:raise ValueError('Resume this managed title before replacing its file.')
    files.idle(store,r)
    p=files.saved(app,store,r)
    original=files.snapshot(store,p)
    if len(files.videos(store,p))!=1 or re.search(r'-cd[1-9]$',p.stem,re.I):
        raise ValueError('Split movies cannot be replaced atomically. The saved parts were kept.')
    choice=next((c for c in candidates(app,store,r)['items'] if c['id']==release_id and not c['reason']),None)
    if not choice:raise ValueError('Release is unavailable or no longer eligible. Search again.')
    actual=files.quality(store,r,p)
    offered=re.sub(r'[pi]$','',str(choice.get('quality','')))
    if actual!='Not verified' and (not offered.isdigit() or int(offered)<=int(actual)):
        raise ValueError('Choose a release above the saved file quality.')
    b=body(r);item=b['item'];e=b.get('episode') or {}
    old=request_for(store,r['key'])
    previous=json.loads(old['body']) if old else {}
    # Carry catalogue identity, never a previous swap journal or another
    # acquisition engine's automation/fallback controls into this request.
    retained=('externalId','source','itemId','image','year','genre','rating','description',
              'episode','scope','directTitleKey','manualTvmaze','imdbId',
              'catalogueEpisode','manualReleaseId','releaseEpisode','showTitle')
    payload={k:previous[k] for k in retained if k in previous}
    selected=ds.candidate(store,release_id)
    if not selected or time.time()-selected['created']>86400:raise ValueError('Release expired.')
    selected=dict(selected,externalId=selected['external_id'])
    # Reuse special episode validation and its exact release-number mapping.
    if not b.get('automatic') and not b.get('directTitleKey'):
        from .. import discover_manual_episodes,discover_specials
        module=discover_specials if item.get('externalId') else discover_manual_episodes
        identity=item.get('externalId') or b['tvmaze']
        selected=module.selected(store,identity,e['id'],release_id)
        season,number=release_pairs(selected['title'])[0]
        payload.update(manualReleaseId=release_id,releaseEpisode=dict(season=season,number=number),
                       catalogueEpisode=e,showTitle=item['title'])
        if not item.get('externalId'):
            show,_=module.catalogue(store,identity,e['id'])
            payload.update(manualTvmaze=str(identity),imdbId=show.get('externals',{}).get('imdb',''))
    payload=_catalogue_body(item,{**payload,'item':item,'itemId':item['id'],
        'externalId':item.get('externalId') or payload.get('externalId',''),
        'source':'NZB first','managedReleaseId':release_id,'replaces':r['key']})
    if e and b.get('automatic'):payload['episode']=e
    if b.get('directTitleKey'):payload['directTitleKey']=b['directTitleKey']
    token=uuid.uuid4().hex
    # A unique engine publication name isolates both video and sidecars. Final
    # publication copies to a private leaf beside p before the atomic swap.
    payload['replacementName']='ReelDock replacement '+token
    with store.exclusive_operation():
        files.idle(store,r)
        current=files.saved(app,store,r)
        if current!=p:raise ValueError('Saved file changed; refresh before replacing.')
        state=files.snapshot(store,p)
        files.same(original,state)
        if (original.st_size,original.st_mtime_ns)!=(state.st_size,state.st_mtime_ns):
            raise ValueError('Saved file changed; refresh before replacing.')
        payload['replacementTarget']=dict(path=str(p),device=state.st_dev,inode=state.st_ino,
                                           size=state.st_size,mtime=state.st_mtime_ns)
        request=records.insert_replacement(store,r,payload)
        records.history(store,r['key'],f'Replacement queued {p} -> release {release_id} (request {request["id"]}); saved file retained until verification.')
    resolve_release(app,store,request,[selected])
    return [request]
