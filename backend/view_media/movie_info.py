"""Optional cached movie facts, cast and verified YouTube trailer links."""
import json
import re
from contextlib import nullcontext
from .providers import TMDB
from ..m3u_metadata import TMDB as _TMDBBase
from .cache_lock import refresh_lock
from ..m3u_player import people


def trailer(videos):
    rows=[row for row in videos if row.get('site')=='YouTube' and row.get('type')=='Trailer'
          and re.fullmatch(r'[A-Za-z0-9_-]{11}',str(row.get('key') or ''))]
    if not rows:return None
    row=max(rows,key=lambda r:(r.get('official') is True,r.get('iso_639_1')=='en',str(r.get('published_at') or '')))
    return dict(url='https://www.youtube.com/watch?v='+row['key'],name=str(row.get('name') or 'Movie trailer')[:200],official=row.get('official') is True)


class IdentityError(ValueError):
    """A conflicting identity must never become an optional metadata fallback."""


class CachedTMDB(_TMDBBase):
    """Only exact saved API response keys; no network, credential or cache writes."""
    def __init__(self,store):self.store=store

    def get(self,path,**params):
        variants=[params]
        if re.fullmatch(r'(?:movie|tv)/[1-9][0-9]{0,11}',path):
            variants += [dict(language='en-GB',append_to_response='credits,videos,release_dates' if path.startswith('movie/') else 'credits,videos,external_ids'),dict(append_to_response='credits'),dict(append_to_response='external_ids'),dict(language='en-GB',append_to_response='external_ids'),{}]
        keys=list(dict.fromkeys(json.dumps([path,value],sort_keys=True) for value in variants))
        with self.store.connect() as db:
            rows=db.execute('SELECT key,data FROM m3u_tmdb_cache WHERE key IN ('+','.join('?' for _ in keys)+')',keys).fetchall()
        saved={row['key']:row['data'] for row in rows}
        for key in keys:
            try:value=json.loads(saved.get(key,'{}'))
            except (TypeError,ValueError):continue
            if isinstance(value,dict) and value:return value
        return {}


def unavailable():
    return dict(facts={},cast=[],trailer=None,source='',unavailable=True)


def _details(store,identity,imdb='',*,client=None):
    tmdb=re.fullmatch(r'tmdb:movie:([1-9][0-9]{0,11})',identity)
    matched=re.fullmatch(r'(?:movie:|imdb:movie:)([0-9]{5,12})',identity)
    imdb=str(imdb or '').removeprefix('tt')
    if imdb and not re.fullmatch(r'[0-9]{5,12}',imdb):raise IdentityError('Choose a valid movie identity.')
    if matched:
        if imdb and int(imdb)!=int(matched[1]):raise IdentityError('Movie identities do not agree.')
        imdb=matched[1]
    if not tmdb and not imdb:raise IdentityError('Choose a valid movie identity.')
    with (nullcontext() if isinstance(client,CachedTMDB) else refresh_lock(store,'movie-info:'+identity+':'+imdb)):
        client=client or TMDB(store)
        entity={'id':int(tmdb[1])} if tmdb else client.entity(dict(kind='movies',provider='imdb',identity='tt'+str(int(imdb)).zfill(7)))
        key=entity.get('id')
        if type(key) is not int or key<=0:raise ValueError('No exact movie metadata match is available.')
        row=client.get('movie/'+str(key),language='en-GB',append_to_response='credits,videos,release_dates')
        if not row:raise ValueError('Movie information is currently unavailable.')
        if row.get('id') is not None and (type(row['id']) is not int or row['id']!=key):raise IdentityError('Movie identities do not agree.')
        found=str(row.get('imdb_id') or '').removeprefix('tt')
        if imdb and found.isdigit() and int(imdb)!=int(found):raise IdentityError('Movie identities do not agree.')
        credits=row.get('credits') or {}
        directors=list(dict.fromkeys(p['name'] for p in credits.get('crew',[]) if p.get('job')=='Director' and p.get('name')))
        language=next((p.get('english_name') or p.get('name') for p in row.get('spoken_languages',[]) if p.get('iso_639_1')==row.get('original_language')),row.get('original_language'))
        facts={'Genres':', '.join(g['name'] for g in row.get('genres',[]) if g.get('name')),
               'Runtime':str(row['runtime'])+' min' if type(row.get('runtime')) is int and row['runtime']>0 else '',
               'Released':row.get('release_date'),
               'Rating':f"{float(row['vote_average']):.1f} / 10" if row.get('vote_average') else '',
               'Director':', '.join(directors[:3]),'Language':language}
        chosen=trailer((row.get('videos') or {}).get('results',[]))
        if not chosen:
            try:chosen=trailer(client.get('movie/'+str(key)+'/videos',language='en-US').get('results',[]))
            except ValueError:pass # Missing optional trailer must not hide facts/cast.
        regions=(row.get('release_dates') or {}).get('results',[])
        # TMDB release type 1 is a premiere, never a theatrical/digital date.
        def premieres_in(country=None):
            return sorted(str(r['release_date'])[:10] for region in regions if country is None or region.get('iso_3166_1')==country
                          for r in region.get('release_dates',[]) if r.get('type')==1 and r.get('release_date'))
        premieres=premieres_in('GB') or premieres_in('US') or premieres_in()
        return dict(facts={k:str(v)[:500] for k,v in facts.items() if v},cast=people({'cast':credits.get('cast',[])})[:6],
                    trailer=chosen,source='TMDB',guideDates=dict(releaseDate=row.get('release_date') or '',premiereDate=premieres[0] if premieres else ''))


def details(store,identity,imdb=''):
    identity=str(identity or '');imdb=str(imdb or '')
    if store.get('m3u_tmdb_token'):
        try:return _details(store,identity,imdb)
        except IdentityError:raise
        except (ValueError,KeyError,TypeError):pass
    try:
        result=_details(store,identity,imdb,client=CachedTMDB(store))
        result['source']='Saved TMDB'
        return result
    except IdentityError:raise
    except (ValueError,KeyError,TypeError):return unavailable()
