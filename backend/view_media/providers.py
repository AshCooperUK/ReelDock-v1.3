"""Discovery metadata only. Indexers are consulted later by acquisition."""
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
from ..m3u_metadata import TMDB
from .. import discover_sources as sources
from .ordering import ordered

_lock=threading.Lock()
_last_call=0.0


def initialise(store):
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS view_media_cache(key TEXT PRIMARY KEY,body TEXT NOT NULL,updated REAL NOT NULL)')


def maze(store,path,**params):
    global _last_call
    initialise(store)
    key=path+'?'+urlencode(params)
    with store.connect() as db:row=db.execute('SELECT body,updated FROM view_media_cache WHERE key=?',(key,)).fetchone()
    if row and row['updated']>time.time()-21600:return json.loads(row['body'])
    # Below TVmaze's documented 20 requests / 10 seconds; bounded response/time.
    with _lock:
        time.sleep(max(0,.55-(time.monotonic()-_last_call)))
        _last_call=time.monotonic()
    data=json.loads(sources.fetch('https://api.tvmaze.com/'+key,limit=4_000_000,timeout=8))
    with store.connect() as db:
        db.execute('INSERT OR REPLACE INTO view_media_cache VALUES(?,?,?)',(key,json.dumps(data),time.time()))
        db.execute('DELETE FROM view_media_cache WHERE updated<?',(time.time()-7*86400,))
    return data


def image(path,size='w500'):
    return 'https://image.tmdb.org/t/p/'+size+path if isinstance(path,str) and re.fullmatch(r'/[A-Za-z0-9_.-]+',path) else ''


def movie_card(row):
    identity=str(row.get('id') or '')
    if not re.fullmatch(r'\d{1,12}',identity):raise ValueError('TMDB returned an invalid movie identity.')
    imdb=str(row.get('imdb_id') or '')
    external=imdb[2:] if re.fullmatch(r'tt\d{1,12}',imdb) else ''
    return dict(id='tmdb:movie:'+identity,kind='movie',title=row.get('title') or row.get('original_title') or 'Untitled',
                externalId=external,provider='TMDB',image=image(row.get('poster_path')),backdrop=image(row.get('backdrop_path'),'w1280'),
                year=str(row.get('release_date') or '')[:4],releaseDate=row.get('release_date') or '',
                rating=str(row.get('vote_average') or ''),description=row.get('overview') or '',
                **({'runtimeMinutes':row['runtime']} if type(row.get('runtime')) is int and 0<row['runtime']<=525600 else {}))


def movies(store,query='',offset=0,sort='source'):
    if offset%20 or not 0<=offset<10000:raise ValueError('Invalid TMDB page.')
    client=TMDB(store);page=offset//20+1
    params=dict(page=page,language='en-GB')
    if query:result=client.get('search/movie',query=query,include_adult='false',**params)
    elif sort=='source':result=client.get('trending/movie/week',**params)
    else:
        order={'title':'title.asc','title_desc':'title.desc','release':'primary_release_date.desc','release_asc':'primary_release_date.asc',
               'year':'primary_release_date.desc','year_asc':'primary_release_date.asc','rating':'vote_average.desc','rating_asc':'vote_average.asc'}[sort]
        params.update(sort_by=order,include_adult='false',include_video='false')
        if sort.startswith(('release','year')):params['primary_release_date.lte']=datetime.now(ZoneInfo('Europe/London')).date().isoformat()
        if sort.startswith('rating'):params['vote_count.gte']=50
        result=client.get('discover/movie',**params)
    return dict(items=[movie_card(r) for r in result.get('results',[]) if not r.get('adult')],
                nextOffset=offset+20 if page<min(int(result.get('total_pages') or 0),500) else None)


def show_card(store,row,episode=None):
    identity=str(row.get('id') or '')
    if not re.fullmatch(r'\d{1,12}',identity):raise ValueError('TVmaze returned an invalid show identity.')
    external=str((row.get('externals') or {}).get('thetvdb') or '')
    result=dict(sources._show_card(row),id='tvmaze:show:'+identity,kind='show',externalId=external,provider='TVmaze',
                imdbId=str((row.get('externals') or {}).get('imdb') or ''),releaseDate=row.get('premiered') or '',lastEpisodeReleased=(episode or {}).get('airstamp') or (episode or {}).get('airdate') or '')
    if external:
        from ..discover_metadata import save
        save(store,external,row,result['title'],result['year'])
    return result


def shows(store,query='',offset=0,sort='source'):
    if query:
        rows=maze(store,'search/shows',q=query)
        cards=[show_card(store,r['show']) for r in rows if r.get('show')]
        return dict(items=cards,nextOffset=None)
    # Recent broadcast and streaming episodes, not an invented TV popularity list.
    today=datetime.now(ZoneInfo('Europe/London')).date();now=time.time();known={};failures=0
    calls=[]
    for days in range(7):
        day=(today-timedelta(days=days)).isoformat()
        calls.extend([('schedule',dict(country='GB',date=day)),('schedule',dict(country='US',date=day)),('schedule/web',dict(date=day))])
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks=[pool.submit(maze,store,path,**params) for path,params in calls]
        for task in tasks:
            try:episodes=task.result()
            except Exception:failures+=1;continue
            for episode in episodes:
                stamp=episode.get('airstamp')
                try:aired=datetime.fromisoformat(stamp.replace('Z','+00:00')).timestamp()
                except (ValueError,AttributeError):continue
                if aired>now:continue
                show=episode.get('show') or (episode.get('_embedded') or {}).get('show')
                if not show:continue
                key=show['id']
                if key not in known or aired>known[key][0]:known[key]=(aired,show,episode)
    if not known and failures:raise ValueError('TVmaze is temporarily unavailable. Try again shortly.')
    cards=[show_card(store,show,ep) for _,show,ep in sorted(known.values(),key=lambda v:-v[0])]
    cards=ordered(cards,sort)
    return dict(items=cards[offset:offset+24],nextOffset=offset+24 if offset+24<len(cards) else None,
                warnings=['Some TVmaze schedules could not be read. Showing available recent episodes.'] if failures else [],
                scope='TVmaze: episodes aired in the past seven days (UK, US and streaming).')


def details(store,identity):
    match=re.fullmatch(r'(tmdb:movie|tmdb:show|imdb:show|tvmaze:show):(\d{1,12})',identity)
    if not match:raise ValueError('Choose a valid catalogue title.')
    provider,key=match.groups()
    if provider in ('tmdb:show','imdb:show'):
        if provider=='imdb:show':row=maze(store,'lookup/shows',imdb='tt'+key.zfill(7))
        else:
            external=TMDB(store).get('tv/'+key+'/external_ids')
            tvdb=external.get('tvdb_id')
            if not tvdb:raise ValueError('No TVDB match is available for this series.')
            row=maze(store,'lookup/shows',thetvdb=tvdb)
        return show_card(store,row)
    if provider=='tmdb:movie':
        row=TMDB(store).get('movie/'+key,language='en-GB')
        if not row:raise ValueError('This movie is no longer available from TMDB.')
        return movie_card(row)
    row=maze(store,'shows/'+key,embed='previousepisode')
    return show_card(store,row,(row.get('_embedded') or {}).get('previousepisode'))
