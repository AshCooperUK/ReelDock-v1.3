"""User-selected TVmaze episode releases when no TVDB identity is available."""
import re
import time
from . import discover_sources as sources, discover_store as storage
from .discover_logic import opaque_id, release_pairs


def catalogue(store, tvmaze, episode_id):
    data=sources.episodes('',store,tvmaze=tvmaze)
    episode=next((e for e in data['episodes'] if str(e['id'])==str(episode_id)),None)
    if not episode:raise ValueError('Choose an episode listed for this show.')
    if data['externals'].get('thetvdb'):raise ValueError('This show has a TVDB identity. Reopen it to use exact matching.')
    return data,episode


def search(app,store,tvmaze,episode_id):
    from .discover_search import query_all
    show,episode=catalogue(store,tvmaze,episode_id)
    numbered=type(episode.get('season')) is int and type(episode.get('number')) is int
    suffix=f"S{episode['season']:02d}E{episode['number']:02d}" if numbered else episode['name']
    query=show['name']+' '+suffix
    params={'t':'search','cat':'5000','q':query,'limit':100,'offset':0}
    imdb=show['externals'].get('imdb') or ''
    # Title/episode search remains available to indexers without IMDb TV search.
    pages,warnings=query_all(sources.indexers_for(app,store),params,store=store)
    words=set(re.findall(r'[a-z0-9]+',(show['name']+(' '+suffix if not numbered else '')).lower()))-{'the','a','an'}
    results=[];secrets=[];seen=set()
    for provider,root in pages:
        for row in root.findall('./channel/item')[:100]:
            title=row.findtext('title') or '';attrs=sources._attrs(row);grab=sources._grab(row);pairs=release_pairs(title)
            supplied=str(attrs.get('imdbid') or attrs.get('imdb') or '').removeprefix('tt').lstrip('0')
            if supplied and imdb and supplied!=imdb.removeprefix('tt').lstrip('0'):continue
            if 'sample' in title.lower() or not grab or len(pairs)!=1:continue
            if numbered and pairs[0]!=(episode['season'],episode['number']):continue
            if not words or not words<=set(re.findall(r'[a-z0-9]+',title.lower())):continue
            identity='tvmaze:'+str(tvmaze);key=opaque_id(identity+':'+str(episode_id)+':'+grab)
            if key in seen:continue
            seen.add(key);quality=re.search(r'(?i)(2160|1080|720|480)[pi]',title);size=str(attrs.get('size') or '0')
            item=dict(id=key,title=title,quality=quality.group(0).lower() if quality else 'Unspecified',size=int(size) if size.isdigit() else 0,provider=provider,releaseSeason=pairs[0][0],releaseNumber=pairs[0][1])
            results.append(item);secrets.append(dict(item,kind='manual-episode:'+str(episode_id),externalId=identity,grab=grab))
    storage.remember_candidates(store,secrets)
    return dict(items=results,warnings=warnings+['Verify the title and episode before downloading. TVDB matching is unavailable.'],query=query)


def selected(store,tvmaze,episode_id,release_id):
    if not isinstance(release_id,str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,100}',release_id):raise ValueError('Choose a release from this search.')
    row=storage.candidate(store,release_id)
    if not row or row['kind']!='manual-episode:'+str(episode_id) or row['external_id']!='tvmaze:'+str(tvmaze):raise ValueError('Search this episode and choose one of its releases.')
    if time.time()-row['created']>86400:raise ValueError('Release choice expired. Search again.')
    return dict(row,externalId=row['external_id'])


def queue(app,store,tvmaze,episode_id,release_id,token):
    from .acquisition import ensure_open
    from .discover_acquire import _blocked,_catalogue_body,re_token,resolve_release
    ensure_open(store)
    if not isinstance(token,str) or not re_token(token):raise ValueError('Invalid request identity.')
    show,episode=catalogue(store,tvmaze,episode_id);release=selected(store,tvmaze,episode_id,release_id)
    quality=release['quality'].rstrip('pi')
    if quality not in ('720','1080','2160'):raise ValueError('Choose a 720p, 1080p or 2160p release.')
    key=f'tvmaze:{tvmaze}:episode:{episode_id}'
    blocked=_blocked(store,key)
    if blocked:return blocked
    from .media_holds import resume
    resume(store,'managed:'+key)
    season,number=release_pairs(release['title'])[0]
    body=_catalogue_body(dict(image=show['image'],year=show['premiered'][:4],description=show['summary']),dict(
        externalId='tvmaze:'+str(tvmaze),manualTvmaze=str(tvmaze),imdbId=show['externals'].get('imdb') or '',
        source='NZB first',itemId='tvmaze:show:'+str(tvmaze),catalogueEpisode=episode,manualReleaseId=release_id,
        releaseEpisode=dict(season=season,number=number),scope=episode['name'],showTitle=show['name']))
    row,created=storage.insert_request(store,dict(idempotency=token+':manual',kind='episode',title=show['name']+' · '+episode['name'],quality=quality,fallback=False,episode_key=key,body=body))
    if created:resolve_release(app,store,row,[release])
    with store.connect() as db:return dict(db.execute('SELECT id,state,message FROM discover_requests WHERE id=?',(row['id'],)).fetchone())
