"""Checked TV identity, premiere and latest *aired* episode metadata."""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from .providers import TMDB,maze,image,initialise
from .cache_lock import refresh_lock


def day(value,aired=False):
    value=str(value or '')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):return ''
    try:parsed=datetime.strptime(value,'%Y-%m-%d').date()
    except ValueError:return ''
    return '' if aired and parsed>datetime.now(ZoneInfo('Europe/London')).date() else value


def tmdb_card(row):
    external=row.get('external_ids') or {}
    tvdb=str(external.get('tvdb_id') or '')
    if not re.fullmatch(r'[1-9][0-9]{0,11}',tvdb):raise ValueError('This series has no verified TVDB identity.')
    premiered=day(row.get('first_air_date'))
    # next_episode_to_air and last_air_date can refer to future catalogue entries.
    episode=row.get('last_episode_to_air') or {}
    aired=day(episode.get('air_date'),aired=True)
    return dict(id='show:'+tvdb,kind='show',externalId=tvdb,imdbId=external.get('imdb_id') or '',
        title=row.get('name') or row.get('original_name') or '',year=premiered[:4],releaseDate=premiered,
        lastEpisodeReleased=aired,image=image(row.get('poster_path')),backdrop=image(row.get('backdrop_path'),'w1280'),
        description=row.get('overview') or '',genre=', '.join(g['name'] for g in row.get('genres',[]) if g.get('name')),
        rating=str(row.get('vote_average') or ''),metadataVerified=True)


def remember_hints(store,tvdb,result):
    if result.get('title') and result.get('year'):
        from .. import discover_sources as sources
        current=sources._load_shows(store,[tvdb]).get(tvdb,{})
        sources._remember_show(store,tvdb,{**current,'identityTitle':result['title'],'identityYear':result['year']})


def details(store,tvdb):
    tvdb=str(tvdb or '')
    if not re.fullmatch(r'[1-9][0-9]{0,11}',tvdb):raise ValueError('Choose a verified TVDB identity.')
    initialise(store);key='nzb:show-metadata:v1:'+tvdb
    def saved():
        with store.connect() as db:return db.execute('SELECT body,updated FROM view_media_cache WHERE key=?',(key,)).fetchone()
    previous=saved()
    if previous and previous['updated']>time.time()-900:
        result=json.loads(previous['body']);remember_hints(store,tvdb,result);return result
    with refresh_lock(store,key):
        previous=saved()
        if previous and previous['updated']>time.time()-900:
            result=json.loads(previous['body']);remember_hints(store,tvdb,result);return result
        try:
            client=TMDB(store,cache_seconds=300)
            found=client.get('find/'+tvdb,external_source='tvdb_id').get('tv_results',[])
            if len(found)!=1 or type(found[0].get('id')) is not int:raise ValueError('No exact TMDB TV match.')
            row=client.get('tv/'+str(found[0]['id']),language='en-GB',append_to_response='external_ids')
            result=tmdb_card(row)
            if result['externalId']!=tvdb:raise ValueError('TV identities do not agree.')
        except (ValueError,TypeError,KeyError):
            row=maze(store,'lookup/shows',thetvdb=tvdb)
            if str((row.get('externals') or {}).get('thetvdb') or '')!=tvdb:raise ValueError('TV identities do not agree.')
            full=maze(store,'shows/'+str(row['id']),embed='previousepisode')
            from ..discover_sources import _show_card
            result=dict(_show_card(full),metadataVerified=True,lastEpisodeReleased='')
            episode=(full.get('_embedded') or {}).get('previousepisode') or {}
            stamp=episode.get('airstamp')
            try:aired=datetime.fromisoformat(stamp.replace('Z','+00:00')).timestamp()<=time.time()
            except (AttributeError,ValueError):aired=False
            if aired:result['lastEpisodeReleased']=day(episode.get('airdate'),aired=True)
        with store.connect() as db:db.execute('INSERT OR REPLACE INTO view_media_cache VALUES(?,?,?)',(key,json.dumps(result),time.time()))
        remember_hints(store,tvdb,result)
        return result


def enrich(store,items,on_progress=None,on_failure=None):
    failures=0
    with ThreadPoolExecutor(max_workers=3) as pool:
        pending={pool.submit(details,store,str(item.get('externalId') or '')):item for item in items if item.get('externalId')}
        for task in as_completed(pending):
            item=pending[task]
            try:
                result=task.result()
                item.update({k:v for k,v in result.items() if k not in ('id','kind','externalId') and v not in ('',None)})
                # An unknown/future last episode must not retain a stale guessed date.
                item['lastEpisodeReleased']=result.get('lastEpisodeReleased','')
            except (ValueError,TypeError,KeyError):
                failures+=1
                if on_failure:on_failure(item)
            if on_progress:on_progress()
    return failures
