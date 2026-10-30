"""Optional exact-identity TV facts, cast and trailer; never acquire media."""
import json
import re
from contextlib import nullcontext
from .providers import TMDB, maze, image
from .cache_lock import refresh_lock
from .movie_info import trailer, CachedTMDB, IdentityError, unavailable
from ..m3u_player import people


def _saved_maze(store,key):
    keys=['shows/'+key+'?','shows/'+key+'?embed=previousepisode']
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='view_media_cache'").fetchone():return {}
        rows=db.execute('SELECT body FROM view_media_cache WHERE key IN (?,?) ORDER BY updated DESC',keys).fetchall()
    for row in rows:
        try:value=json.loads(row['body'])
        except (ValueError,TypeError):continue
        if isinstance(value,dict) and value:return value
    return {}


def _details(store,identity,tvdb='',imdb='',*,client=None,cache_only=False):
    matched=re.fullmatch(r'(show|tvdb:show|tvmaze:show|tmdb:show|imdb:show):([1-9][0-9]{0,11})',identity)
    tvdb=str(tvdb or '');imdb=str(imdb or '').removeprefix('tt')
    if not matched or (tvdb and not re.fullmatch(r'[1-9][0-9]{0,11}',tvdb)) or (imdb and not re.fullmatch(r'[0-9]{5,12}',imdb)):
        raise IdentityError('Choose a valid TV show identity.')
    provider,key=matched.groups()
    if provider in ('show','tvdb:show'):
        if tvdb and int(tvdb)!=int(key):raise IdentityError('TV show identities do not agree.')
        tvdb=key
    if provider=='imdb:show':
        if imdb and int(imdb)!=int(key):raise IdentityError('TV show identities do not agree.')
        imdb=key
    with (nullcontext() if cache_only else refresh_lock(store,'show-info:'+identity+':'+tvdb+':'+imdb)):
        if provider=='tvmaze:show':
            row=_saved_maze(store,key) if cache_only else maze(store,'shows/'+key)
            if not row:raise ValueError('No exact saved TV show metadata is available.')
            if row.get('id') is not None and str(row['id'])!=key:raise IdentityError('TV show identities do not agree.')
            external=row.get('externals') or {}
            found=str(external.get('thetvdb') or '')
            found_imdb=str(external.get('imdb') or '').removeprefix('tt')
            if tvdb and found and int(tvdb)!=int(found):raise IdentityError('TV show identities do not agree.')
            if imdb and found_imdb and int(imdb)!=int(found_imdb):raise IdentityError('TV show identities do not agree.')
            tvdb=found or tvdb;imdb=found_imdb or imdb
        client=client or TMDB(store)
        if provider=='tmdb:show':entity={'id':int(key)}
        elif tvdb:entity=client.entity(dict(kind='series',provider='tvdb',identity=tvdb))
        elif imdb:entity=client.entity(dict(kind='series',provider='imdb',identity='tt'+imdb.zfill(7)))
        else:raise ValueError('No exact TV show metadata identity is available.')
        key=entity.get('id')
        if type(key) is not int or key<=0:raise ValueError('No exact TV show metadata match is available.')
        row=client.get('tv/'+str(key),language='en-GB',append_to_response='credits,videos,external_ids')
        if not row:raise ValueError('TV show information is currently unavailable.')
        if row.get('id') is not None and (type(row['id']) is not int or row['id']!=key):raise IdentityError('TV show identities do not agree.')
        external=row.get('external_ids') or {}
        found=str(external.get('tvdb_id') or '');found_imdb=str(external.get('imdb_id') or '').removeprefix('tt')
        if tvdb and found and int(tvdb)!=int(found):raise IdentityError('TV show identities do not agree.')
        if imdb and found_imdb and int(imdb)!=int(found_imdb):raise IdentityError('TV show identities do not agree.')
        runtimes=[n for n in row.get('episode_run_time',[]) if type(n) is int and n>0]
        language=next((p.get('english_name') or p.get('name') for p in row.get('spoken_languages',[]) if p.get('iso_639_1')==row.get('original_language')),row.get('original_language'))
        facts={'Creator':', '.join(p['name'] for p in row.get('created_by',[]) if p.get('name')),
               'Genres':', '.join(p['name'] for p in row.get('genres',[]) if p.get('name')),
               'Language':language,'Rating':f"{float(row['vote_average']):.1f} / 10" if row.get('vote_average') else '',
               'Premiered':row.get('first_air_date'),'Episode runtime':str(runtimes[0])+' min' if runtimes else '',
               'Seasons':str(row['number_of_seasons']) if type(row.get('number_of_seasons')) is int and row['number_of_seasons']>0 else '',
               'Status':row.get('status')}
        chosen=trailer((row.get('videos') or {}).get('results',[]))
        if not chosen:
            try:chosen=trailer(client.get('tv/'+str(key)+'/videos',language='en-US').get('results',[]))
            except ValueError:pass
        return dict(facts={k:str(v)[:500] for k,v in facts.items() if v},cast=people({'cast':(row.get('credits') or {}).get('cast',[])})[:6],trailer=chosen,source='TMDB',title=row.get('name') or '',year=str(row.get('first_air_date') or '')[:4],description=row.get('overview') or '',image=image(row.get('poster_path')),backdrop=image(row.get('backdrop_path'),'w1280'))


def _basic(store,identity,tvdb,imdb):
    provider,key=identity.rsplit(':',1)
    if provider in ('show','tvdb:show'):tvdb=key
    raw=_saved_maze(store,key) if provider=='tvmaze:show' else {}
    if raw:
        external=raw.get('externals') or {}
        found=str(external.get('thetvdb') or '')
        found_imdb=str(external.get('imdb') or '').removeprefix('tt')
        if raw.get('id') is not None and str(raw['id'])!=key:raise IdentityError('TV show identities do not agree.')
        if found and (not re.fullmatch(r'[1-9][0-9]{0,11}',found) or tvdb and int(tvdb)!=int(found)):raise IdentityError('TV show identities do not agree.')
        if found_imdb and (not re.fullmatch(r'[0-9]{5,12}',found_imdb) or imdb and int(str(imdb).removeprefix('tt'))!=int(found_imdb)):raise IdentityError('TV show identities do not agree.')
        from ..discover_sources import _show_card
        card=_show_card(raw)
    elif tvdb:
        with store.connect() as db:
            row=db.execute('SELECT body FROM discover_show_media WHERE tvdb=?',(tvdb,)).fetchone()
        try:card=json.loads(row['body']) if row else {}
        except (ValueError,TypeError):card={}
        if not isinstance(card,dict):card={}
        if card.get('externalId') and str(card['externalId'])!=tvdb:raise IdentityError('TV show identities do not agree.')
        if card.get('id') and card['id'] not in ('show:'+tvdb,'tvdb:show:'+tvdb):return unavailable()
        found_imdb=str(card.get('imdbId') or '').removeprefix('tt')
        if found_imdb and (not re.fullmatch(r'[0-9]{5,12}',found_imdb) or imdb and int(str(imdb).removeprefix('tt'))!=int(found_imdb)):raise IdentityError('TV show identities do not agree.')
    else:card={}
    if not card:return unavailable()
    facts={name:str(value)[:500] for name,value in dict(Genres=card.get('genre'),Premiered=card.get('releaseDate'),Rating=card.get('rating')).items() if value}
    return dict(facts=facts,cast=[],trailer=None,source='Saved TV information',title=card.get('title') or '',year=card.get('year') or '',description=card.get('description') or '',image=card.get('image') or '',backdrop=card.get('backdrop') or '')


def details(store,identity,tvdb='',imdb=''):
    identity=str(identity or '');tvdb=str(tvdb or '');imdb=str(imdb or '')
    if store.get('m3u_tmdb_token'):
        try:return _details(store,identity,tvdb,imdb)
        except IdentityError:raise
        except (ValueError,KeyError,TypeError):pass
    try:
        result=_details(store,identity,tvdb,imdb,client=CachedTMDB(store),cache_only=True)
        result['source']='Saved TMDB'
        return result
    except IdentityError:raise
    except (ValueError,KeyError,TypeError):return _basic(store,identity,tvdb,imdb)
