"""Saved collection over existing title/channel policies; never an acquisition engine."""
import hashlib
import hmac
import json
import re
import time
from flask import jsonify, request
from . import discover_store as ds


def initialise(store):
    with store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS video_favourites (
            id TEXT PRIMARY KEY, item TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS episode_favourites (
            key TEXT PRIMARY KEY, item TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)''')


def video_item(store, identity):
    with store.connect() as db:
        row=db.execute('SELECT v.data,c.data AS creator FROM videos v LEFT JOIN channels c ON c.id=v.channel WHERE v.id=?',(identity,)).fetchone()
    if not row:raise ValueError('Open a saved YouTube video first.')
    v=json.loads(row['data']);c=json.loads(row['creator'] or '{}')
    return dict(id='youtube:video:'+identity,kind='video',videoId=identity,title=str(v.get('title') or 'YouTube video')[:300],
        channelId=v.get('channel',''),channel=c.get('name',''),image=v.get('thumbnail',''),avatar=c.get('avatar',''),
        description=str(v.get('description') or '')[:800],published=v.get('published') or v.get('date',''),duration=v.get('duration',''),genres=channel_genres(c))


def title_item(store, row):
    item=json.loads(row['item']);extra={}
    with store.connect() as db:
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='saved_media_catalogue'").fetchone()
        cached=db.execute('SELECT body FROM saved_media_catalogue WHERE id=? ORDER BY updated DESC',(item['id'],)).fetchall() if exists else []
    for r in cached:
        body=json.loads(r['body']);raw=body.get('action',{}).get('item',{})
        for key in ('image','backdrop','year','genre','rating','description'):
            if not extra.get(key):extra[key]=body.get(key) or raw.get(key)
    return {**extra,**{k:v for k,v in item.items() if v not in ('',None)}}


def channel_genres(channel):
    """Conservative local categories, inferred only from saved channel information."""
    from .youtube_discover import TOPICS
    text=' '.join(str(channel.get(key) or '')[:16000] for key in ('name','description'))
    text=re.sub(r'https?://\S+|\S+@\S+', ' ', text.lower())
    words=set(re.findall(r'[a-z]+',text))
    # Shared topic labels, with narrower signals than discovery recommendations:
    # e.g. an electrician's "training" alone is not evidence of a fitness channel.
    terms=dict(
        music='music songs singer concert guitar piano musician musicians',
        gaming='gaming gamer gameplay minecraft playstation xbox nintendo',
        technology='technology tech computer computers computing linux software coding programming hardware electronics networking',
        science='science physics astronomy biology chemistry scientific',
        education='education educational tutorial tutorials teaching mathematics',
        travel='travel travelling traveling camping hiking tourism overland roadtrip',
        food='cooking recipe recipes chef baking kitchen cuisine',
        sports='sports football soccer basketball tennis cricket rugby',
        fitness='fitness workout workouts yoga exercise bodybuilding',
        film='cinema filmmaking movies filmmaker filmmakers',
        comedy='comedy comedian comedians humour humor',
        crafts='woodworking woodwork crafts diy restoration maker makers sewing welding metalwork fabrication',
        nature='nature wildlife animals gardening birds conservation',
        news='news journalism politics journalist journalists')
    labels=[TOPICS[key][0] for key,signals in terms.items() if words.intersection(signals.split())]
    extra={
        'Cars & motoring':'car cars automotive motoring mechanic mechanics motorcycle motorcycles motorbike motorbikes turbocharged restomodding',
        'Crime & justice':'crime criminal criminals courtroom justice policing',
        'Documentaries':'documentary documentaries',
        'Engineering & inventions':'engineering engineer engineers invention inventions inventor inventors robotics',
        'Trades & construction':'electrician electricians electrical locksmith locksmiths plumbing plumber plumbers carpentry construction'}
    labels.extend(label for label,signals in extra.items() if words.intersection(signals.split()))
    if 'do it yourself' in text and TOPICS['crafts'][0] not in labels:labels.append(TOPICS['crafts'][0])
    return sorted(labels,key=str.casefold)


def episode_item(app,store,key):
    from .media_management import service,store as records
    row=records.row(store,key)
    if not row or row['kind']!='episode' or row['removed']:raise ValueError('Open a saved TV episode first.')
    body=service.body(row);raw=body.get('item',{});episode=body.get('episode')
    if raw.get('kind')!='show' or not isinstance(episode,dict):raise ValueError('Choose a TV episode.')
    show={k:v for k,v in ds.clean_item(raw).items() if k in ('id','kind','title','year','image','backdrop','externalId','imdbId','description','genre','rating','provider')}
    state=service.compose(app,store,row);request=service.request_for(store,key)
    entry={k:state.get(k) for k in ('key','title','season','number','date','dateLabel','status','file','image','requestId')}
    entry.update(requested=bool(request and request['state'] not in ('cancelled','failed','complete')),
                 requestState=request['state'] if request else '',description=str(episode.get('summary') or '')[:4000])
    label=('S'+str(entry['season']).zfill(2)+'E'+str(entry['number']).zfill(2)) if entry['season'] is not None and entry['number'] is not None else 'Unnumbered episode'
    return dict(id='episode:'+key,kind='show',title=show['title']+' · '+label+' · '+entry['title'],
                image=show.get('image') or entry['image'] or '',year=show.get('year',''),genre=show.get('genre',''),
                episodeKey=key,episodeLabel=label,episode=entry,show=show)


def listing(store,app):
    from . import movie_options, show_options
    result=dict(movies=[],shows=[],channels=[],videos=[],episodes=[])
    with store.connect() as db:
        movies=[dict(r) for r in db.execute('SELECT * FROM movie_options WHERE favourite=1 ORDER BY key')]
        shows=[dict(r) for r in db.execute('SELECT * FROM show_options WHERE favourite=1 ORDER BY key')]
        channels=db.execute('''SELECT c.id,c.data,(SELECT count(*) FROM videos v WHERE v.channel=c.id) AS indexed,
            (SELECT max(day) FROM videos v WHERE v.channel=c.id AND json_extract(v.data,'$.state')='published') AS latest
            FROM channels c JOIN channel_favourites f ON f.id=c.id''').fetchall()
        videos=db.execute('SELECT id,item FROM video_favourites ORDER BY id').fetchall()
        episodes=db.execute('SELECT key,item FROM episode_favourites ORDER BY key').fetchall()
    for rows,options,name in ((movies,movie_options,'movies'),(shows,show_options,'shows')):
        for row in rows:
            state=options.snapshot(store,row)
            result[name].append(dict(title_item(store,row),collectionKey=row['key'],
                automatic=state.get('automatic',False),downloadFuture=state.get('downloadFuture',False),
                downloadAll=state.get('downloadAll',False),automaticSupported=state.get('automaticSupported',True)))
    for row in channels:
        c=json.loads(row['data'])
        result['channels'].append(dict(id=row['id'],kind='channel',channelId=row['id'],title=c.get('name') or 'YouTube channel',
            image=c.get('avatar') or '',avatar=c.get('avatar') or '',banner=c.get('banner') or '',
            description=c.get('description') or '',genres=channel_genres(c),indexedVideos=row['indexed'],latestUpload=row['latest'] or ''))
    for row in videos:
        try:item=video_item(store,row['id'])
        except ValueError:item=json.loads(row['item'])
        result['videos'].append(item)
    for row in episodes:
        try:item=episode_item(app,store,row['key'])
        except ValueError:item=json.loads(row['item'])
        result['episodes'].append(item)
    return result


def picker(store, query, offset):
    pattern='%'+query.replace('!','!!').replace('%','!%').replace('_','!_')+'%'
    with store.connect() as db:
        rows=db.execute('''SELECT v.id FROM videos v LEFT JOIN channels c ON c.id=v.channel
            WHERE json_extract(v.data,'$.state')='published'
            AND (json_extract(v.data,'$.title') LIKE ? ESCAPE '!' OR json_extract(c.data,'$.name') LIKE ? ESCAPE '!')
            ORDER BY v.published DESC,v.id LIMIT 61 OFFSET ?''',(pattern,pattern,offset)).fetchall()
        selected={r[0] for r in db.execute('SELECT id FROM video_favourites')}
    return dict(items=[dict(video_item(store,r['id']),favourite=r['id'] in selected) for r in rows[:60]],
        nextOffset=offset+60 if len(rows)>60 else None)


def vod_title(store, source, path, *, refresh=True):
    """Resolve a stored VOD title by authoritative IDs, without fetching its stream."""
    from .view_media.providers import TMDB, image, initialise as initialise_cache
    if refresh:initialise_cache(store)
    with store.connect() as db:
        row=db.execute('''SELECT c.key,f.title,f.kind,t.info,t.data,t.year FROM m3u_files f
            JOIN m3u_catalog_entries c ON c.source=f.source AND c.path=f.path
            JOIN m3u_catalog_titles t ON t.key=c.key JOIN m3u_sources s ON s.id=f.source
            WHERE f.source=? AND f.path=? AND f.kind IN ('movies','series','tv')
            AND lower(f.path) NOT LIKE '%sample%'
            AND coalesce(json_extract(s.options,'$.enabled'),1)=1''',(source,path)).fetchone()
    if not row:raise ValueError('This VOD title is not catalogued yet. Sync its metadata first.')
    data=json.loads(row['data'] or '{}');info=json.loads(row['info'] or '{}')
    kind='movie' if row['kind']=='movies' else 'show';provider='imdb' if kind=='movie' else 'tvdb'
    external=str(info.get('identity') or '').removeprefix('tt') if info.get('provider')==provider else ''
    if not re.fullmatch('[1-9][0-9]{0,11}',external.lstrip('0')):external=''
    linked=str((data.get('external_ids') or {}).get(provider+'_id') or '').removeprefix('tt').lstrip('0')
    if linked and external and linked!=external.lstrip('0'):raise ValueError('VOD metadata identities do not agree. Fix this title match first.')
    if re.fullmatch('[1-9][0-9]{0,11}',linked):external=external or linked
    tmdb=str(data.get('tmdb_id') or (info.get('identity') if info.get('provider')=='tmdb' else '') or '')
    cache_key='favourites:vod:'+hashlib.sha256(json.dumps([row['key'],info,tmdb],sort_keys=True).encode()).hexdigest()
    resolved={}
    with store.connect() as db:
        cache_exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='view_media_cache'").fetchone()
        cached=db.execute('SELECT body,updated FROM view_media_cache WHERE key=?',(cache_key,)).fetchone() if cache_exists else None
    if cached and cached['updated']>time.time()-86400:resolved=json.loads(cached['body'])
    elif refresh and re.fullmatch('[1-9][0-9]{0,11}',tmdb) and not external:
        try:
            found=TMDB(store).get(('movie/' if kind=='movie' else 'tv/')+tmdb,append_to_response='external_ids')
            if str(found.get('id'))!=tmdb:raise ValueError('VOD metadata identities do not agree.')
            ids=found.get('external_ids') or {}
            value=str((found.get('imdb_id') or ids.get('imdb_id')) if kind=='movie' else ids.get('tvdb_id') or '').removeprefix('tt')
            if re.fullmatch('[1-9][0-9]{0,11}',value.lstrip('0')):resolved['externalId']=str(int(value))
            resolved.update(image=image(found.get('poster_path')),backdrop=image(found.get('backdrop_path'),'w1280'))
            with store.connect() as db:db.execute('INSERT OR REPLACE INTO view_media_cache VALUES(?,?,?)',(cache_key,json.dumps(resolved),time.time()))
        except ValueError:pass # An unresolved title can be favourited without guessing an external ID.
    external=str(int(external)) if external else resolved.get('externalId','')
    if refresh and external and re.fullmatch('[1-9][0-9]{0,11}',tmdb):
        # Retain the verified cross-provider link in catalogue metadata, so the
        # existing acquisition engine can reject conflicting editions explicitly.
        linked=dict(data.get('external_ids') or {})
        linked[provider+'_id']=('tt'+external.zfill(7)) if provider=='imdb' else int(external)
        enriched=dict(data,external_ids=linked)
        with store.connect() as db:db.execute('UPDATE m3u_catalog_titles SET data=? WHERE key=? AND data=?',(json.dumps(enriched),row['key'],row['data']))
    identity=kind+':'+external if external else 'tmdb:'+kind+':'+tmdb if re.fullmatch('[1-9][0-9]{0,11}',tmdb) else 'vod:'+row['key']
    art=data.get('artwork') or ''
    if art.startswith('/api/m3u-artwork/'):art='https://image.tmdb.org/t/p/w500/'+art.split('/')[-1]
    return ds.clean_item(dict(id=identity,kind=kind,externalId=external,title=data.get('title') or info.get('name') or row['title'],
        year=str(row['year'] or ''),image=resolved.get('image') or art,backdrop=resolved.get('backdrop') or '',
        description=data.get('description') or '',rating=data.get('rating') or '',provider='VOD'))


def _recorded_local_item(row):
    """Use saved provider identities only; never enrich or enrol during this GET."""
    managed = json.loads(row['managed_body'] or '{}')
    request = json.loads(row['request_body'] or '{}')
    kind = row['managed_kind'] or row['request_kind'] or ('episode' if row['key'].startswith('show:') else 'movie')
    if kind not in ('movie', 'episode'):
        return None
    if row['managed_kind'] and row['request_kind'] and row['managed_kind'] != row['request_kind']:
        raise ValueError('This file has conflicting saved identities. Check its metadata first.')
    expected = 'show' if kind == 'episode' else 'movie'
    raw = [value for value in (managed.get('item'), request.get('item')) if isinstance(value, dict)]
    if not isinstance(request.get('item'), dict) and row['request_body']:
        raw.append(dict(request, id=request.get('itemId') or row['episode_key'] or row['key'],
                        kind=expected, title=row['request_title'] or 'Saved title'))
    ids = dict(external=set(), tmdb=set(), tvmaze=set())
    external = ''
    def add(field, value):
        nonlocal external
        value = str(value or '').removeprefix('tt') if expected == 'movie' else str(value or '')
        if re.fullmatch(r'[0-9]{1,12}', value) and int(value):
            ids[field].add(str(int(value)))
            if field == 'external' and not external:external = value
    def identity(value):
        match = re.fullmatch(r'(tmdb|imdb|tvdb|tvmaze):'+expected+r':([0-9]{1,12})', str(value or ''))
        if match:
            if match[1] == 'tmdb':add('tmdb', match[2])
            elif match[1] == 'tvmaze' and expected == 'show':add('tvmaze', match[2])
            elif match[1] == ('imdb' if expected == 'movie' else 'tvdb'):add('external', match[2])
            else:raise ValueError('This file has conflicting saved identities. Check its metadata first.')
            return bool(int(match[2]))
        match = re.fullmatch(expected+r':([0-9]{1,12})'+(r':[0-9]{1,4}:[0-9]{1,4}' if kind == 'episode' else ''),str(value or ''))
        if match:add('external', match[1]);return bool(int(match[1]))
        # A show item uses show:<TVDB>, while episode ownership has numbering.
        if kind == 'episode':
            match = re.fullmatch(r'show:([0-9]{1,12})', str(value or ''))
            if match:add('external', match[1]);return bool(int(match[1]))
        if kind == 'episode':
            match = re.fullmatch(r'tvmaze:([0-9]{1,12}):episode:([0-9]{1,12})', str(value or ''))
            if match and int(match[1]) and int(match[2]):add('tvmaze', match[1]);return True
        if str(value or '').startswith(('tmdb:', 'imdb:', 'tvdb:', 'tvmaze:', 'movie:', 'show:')):
            raise ValueError('This file has conflicting saved identities. Check its metadata first.')
        return False
    chosen = None
    for item in raw:
        if item.get('kind') != expected:
            raise ValueError('This file has conflicting saved identities. Check its metadata first.')
        known = identity(item.get('id'))
        add('external', item.get('externalId'))
        if expected == 'movie':add('external', item.get('imdbId'))
        if chosen is None and (known or ids['external']):chosen = item
    identity(row['key'])
    identity(row['episode_key'])
    if any(len(values)>1 for values in ids.values()):
        raise ValueError('This file has conflicting saved identities. Check its metadata first.')
    if not any(ids.values()):
        return None
    chosen = dict(chosen or {}, kind=expected)
    chosen_id = re.fullmatch(r'(?:tmdb:|imdb:|tvdb:|tvmaze:)?'+expected+r':([0-9]{1,12})', str(chosen.get('id') or ''))
    if not chosen_id or not int(chosen_id[1]):
        provider='tmdb' if ids['tmdb'] else 'tvmaze'
        chosen['id'] = expected+':'+external if external else provider+':'+expected+':'+next(iter(ids[provider]))
    if external:chosen['externalId'] = external
    if expected == 'movie' and external:chosen['imdbId'] = 'tt'+external
    chosen.setdefault('title', row['request_title'] or 'Saved title')
    item = ds.clean_item(chosen)
    if kind == 'movie':return item
    episodes = [value for value in (managed.get('episode'), request.get('episode')) if isinstance(value, dict)]
    episode = episodes[0] if episodes else {}
    match = re.fullmatch(r'show:[0-9]{1,12}:([0-9]{1,4}):([0-9]{1,4})', row['key'])
    season, number = (int(match[1]), int(match[2])) if match else (episode.get('season'), episode.get('number'))
    maze_key = re.fullmatch(r'tvmaze:([0-9]{1,12}):episode:([0-9]{1,12})', row['key'])
    if not maze_key and (type(season) is not int or type(number) is not int):return None
    if maze_key and any(value is not None and (type(value) is not int or not 0<=value<=9999) for value in (season,number)):return None
    for saved in episodes:
        if any(field in saved and (saved[field] != value or saved[field] is not None and type(saved[field]) is not int) for field,value in (('season',season),('number',number))):
            raise ValueError('This file has conflicting saved identities. Check its metadata first.')
    request_key = re.fullmatch(r'show:[0-9]{1,12}:([0-9]{1,4}):([0-9]{1,4})', str(row['episode_key'] or ''))
    if request_key and (int(request_key[1]),int(request_key[2])) != (season,number):
        raise ValueError('This file has conflicting saved identities. Check its metadata first.')
    if maze_key:
        if row['episode_key'] and row['episode_key']!=row['key']:
            raise ValueError('This file has conflicting saved identities. Check its metadata first.')
        if any('tvmaze' in saved and str(saved['tvmaze'])!=maze_key[1] for saved in (managed,request)):
            raise ValueError('This file has conflicting saved identities. Check its metadata first.')
        if any('id' in saved and str(saved['id'])!=maze_key[2] for saved in episodes):
            raise ValueError('This file has conflicting saved identities. Check its metadata first.')
    title = managed.get('displayTitle') or episode.get('name') or item['title']
    entry = dict(key=row['key'],title=title,season=season,number=number,date=managed.get('date') or episode.get('airdate',''),
                 dateLabel=managed.get('dateLabel') or 'Air date',status='In library',file=True,image=episode.get('image') or item.get('image',''),
                 requestId=row['request_id'] or '',requested=bool(row['request_state'] and row['request_state'] not in ('cancelled','failed','complete')),requestState=row['request_state'] or '',description=str(episode.get('summary') or '')[:4000])
    label=('S'+str(season).zfill(2)+'E'+str(number).zfill(2)) if season is not None and number is not None else 'Unnumbered episode'
    return dict(id='episode:'+row['key'],kind='show',title=item['title']+' · '+label+' · '+title,
                image=item.get('image') or entry['image'],year=item.get('year',''),genre=item.get('genre',''),
                episodeKey=row['key'],episodeLabel=label,episode=entry,show=item)


def local_title(app,store,identity):
    """Resolve recorded ownership only; a filename is never a catalogue identity."""
    from pathlib import Path
    from .view_media.local import media_file
    if not re.fullmatch(r'[a-f0-9]{32}',identity):raise ValueError('Choose a local title.')
    root=Path(app.config['MEDIA_DIR']).resolve();file=media_file(root,identity,'file')
    def matches(value,folder=False):
        if not value:return False
        path=(root/value).resolve()
        return path.is_relative_to(root) and (path==file or (folder and path!=root and path.is_dir() and path==file.parent))
    with store.connect() as db:
        rows=db.execute("""SELECT o.key,o.path,o.request_id,r.body AS request_body,r.kind AS request_kind,
            r.episode_key,r.title AS request_title,r.state AS request_state,m.body AS managed_body,m.kind AS managed_kind
            FROM discover_owned o LEFT JOIN discover_requests r ON r.id=o.request_id
            LEFT JOIN managed_media m ON m.key=o.key WHERE o.state='complete'""").fetchall()
        videos=db.execute('SELECT id,path FROM media_files').fetchall()
        vod=db.execute("SELECT source,path,destination FROM m3u_downloads WHERE state='complete'").fetchall()
    found=[]
    for row in rows:
        if not matches(row['path'],(row['managed_kind'] or row['request_kind'])=='movie'):continue
        item=_recorded_local_item(row)
        if item:found.append(item)
    for row in videos:
        if matches(row['path']):found.append(video_item(store,row['id']))
    if found:
        keys={(v.get('episodeKey') or ('movie:'+str(int(v['externalId'])) if v.get('kind')=='movie' and v.get('externalId') else v['id'])) for v in found}
        tmdb={item['id'] for item in found if item.get('kind')=='movie' and item.get('id','').startswith('tmdb:movie:')}
        if len(keys)!=1 or len(tmdb)>1:raise ValueError('This file has conflicting saved identities. Check its metadata first.')
        return found[0]
    for row in vod:
        if matches(row['destination']):return vod_title(store,row['source'],row['path'],refresh=False)
    raise ValueError('This local file has no recorded title identity. Match its metadata before saving collection choices.')


def register(app,store,access,csrf):
    initialise(store)
    def guard(write=False):
        denied=access(local_action=True)
        if denied:return denied
        if write and not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return jsonify(error='Reload the page.'),403
        from .discover import _limited
        if not _limited(app,'favourites_hits',180):return jsonify(error='Too many favourites requests. Try again shortly.'),429

    @app.get('/api/favourites')
    def favourites_list():
        denied=guard()
        if denied:return denied
        return jsonify(**listing(store,app))

    @app.get('/api/favourites/local-title/<identity>')
    def favourite_local_title(identity):
        denied=guard()
        if denied:return denied
        try:return jsonify(item=local_title(app,store,identity),csrf=csrf())
        except (ValueError,TypeError) as exc:return jsonify(error=str(exc)),400

    @app.route('/api/favourites/episode',methods=['GET','POST'])
    def favourite_episode():
        denied=guard(request.method=='POST')
        if denied:return denied
        body=dict(request.args) if request.method=='GET' else request.get_json(silent=True)
        allowed={'key'} if request.method=='GET' else {'key','favourite'}
        if not isinstance(body,dict) or set(body)!=allowed or not isinstance(body.get('key'),str) or not 1<=len(body['key'])<=250 or any(ord(c)<32 for c in body['key']):return jsonify(error='Choose a TV episode.'),400
        key=body['key']
        if request.method=='POST':
            if type(body['favourite']) is not bool:return jsonify(error='Choose on or off.'),400
            try:item=episode_item(app,store,key) if body['favourite'] else None
            except ValueError as exc:return jsonify(error=str(exc)),404
            now=time.time()
            with store.connect() as db:
                if item:db.execute('INSERT INTO episode_favourites VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET item=excluded.item,updated=excluded.updated',(key,json.dumps(item),now,now))
                else:db.execute('DELETE FROM episode_favourites WHERE key=?',(key,))
        with store.connect() as db:saved=bool(db.execute('SELECT 1 FROM episode_favourites WHERE key=?',(key,)).fetchone())
        return jsonify(key=key,favourite=saved,csrf=csrf())

    @app.get('/api/favourites/videos')
    def favourite_video_picker():
        denied=guard()
        if denied:return denied
        try:
            offset=int(request.args.get('offset','0'))
            if not 0<=offset<=250000:raise ValueError('Invalid video page.')
            return jsonify(picker(store,request.args.get('q','').strip()[:160],offset))
        except ValueError as exc:return jsonify(error=str(exc)),400

    @app.route('/api/favourites/video/<identity>',methods=['GET','POST'])
    def favourite_video(identity):
        denied=guard(request.method=='POST')
        if denied:return denied
        if not re.fullmatch('[A-Za-z0-9_-]{11}',identity):return jsonify(error='Choose a YouTube video.'),400
        if request.method=='POST':
            body=request.get_json(silent=True)
            if not isinstance(body,dict) or set(body)!={'favourite'} or type(body['favourite']) is not bool:return jsonify(error='Choose on or off.'),400
            try:item=video_item(store,identity) if body['favourite'] else None
            except ValueError as exc:return jsonify(error=str(exc)),404
            now=time.time()
            with store.connect() as db:
                if item:db.execute('INSERT INTO video_favourites VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET item=excluded.item,updated=excluded.updated',(identity,json.dumps(item),now,now))
                else:db.execute('DELETE FROM video_favourites WHERE id=?',(identity,))
        with store.connect() as db:saved=bool(db.execute('SELECT 1 FROM video_favourites WHERE id=?',(identity,)).fetchone())
        return jsonify(id=identity,favourite=saved,csrf=csrf())

    @app.route('/api/favourites/vod-title',methods=['GET','POST'])
    def favourite_vod_title():
        denied=guard(request.method=='POST')
        if denied:return denied
        body=request.args if request.method=='GET' else request.get_json(silent=True)
        if request.method=='GET':body=dict(body)
        if not isinstance(body,dict) or not isinstance(body.get('source'),str) or not isinstance(body.get('path'),str):return jsonify(error='Choose a VOD title.'),400
        try:return jsonify(item=vod_title(store,body['source'][:100],body['path'][:4096]),csrf=csrf())
        except (ValueError,TypeError) as exc:return jsonify(error=str(exc)),400
