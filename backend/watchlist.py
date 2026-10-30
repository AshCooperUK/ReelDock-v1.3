"""Explicit Watchlist membership over the existing saved-title store.

Explicit additions enable future automation and removals disable it. Existing
workers own release checks/acquisition; dates and downloads never remove membership.
"""
import hmac
import json
import re
import time
from datetime import date
from flask import jsonify, request
from . import discover_store as ds, favourites, movie_options, show_options

FIELDS=('id','kind','title','externalId','imdbId','year','image','backdrop','genre',
        'rating','description','videoId','channelId','channel','published','releaseDate','duration',
        'durationSeconds','runtimeMinutes')
AUTO_CHANNELS_SEEN='watchlist_auto_channels_seen'
AUTO_TITLES_SEEN='watchlist_auto_titles_seen'


def seen_titles(db):
    row=db.execute('SELECT value FROM settings WHERE key=?',(AUTO_TITLES_SEEN,)).fetchone()
    value=json.loads(row[0]) if row else []
    if not isinstance(value,list) or any(not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,250}',v) for v in value):
        raise ValueError('Could not read automatic title Watchlist history.')
    return set(value)


def remember_titles(db,keys):
    db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(AUTO_TITLES_SEEN,json.dumps(sorted(keys))))


def automatic_titles(db):
    """Read configured automation, never default catalogue records or requests."""
    result={}
    def include(raw,**flags):
        try:key,item=identity(raw)
        except (ValueError,TypeError):return
        if item['kind'] not in ('movie','show','video'):return
        entry=result.setdefault(key,dict(item=item,flags=dict(automatic=True)))
        entry['flags'].update({k:True for k,v in flags.items() if v})
    for row in db.execute('SELECT item FROM movie_options WHERE automatic=1'):
        raw=json.loads(row['item'])
        try:key,item=identity(raw)
        except (ValueError,TypeError):continue
        record=db.execute("SELECT monitor,hold,removed FROM managed_media WHERE kind='movie' AND (parent=? OR key=? OR key=?) ORDER BY updated DESC LIMIT 1",(item['id'],key,'movie:'+item.get('externalId',''))).fetchone()
        if not record or not (record['hold'] or record['removed'] or record['monitor']==0):include(raw)
    for row in db.execute('SELECT * FROM discover_series_plan WHERE download_all=1 OR download_future=1'):
        include(json.loads(row['item']),downloadAll=row['download_all'],downloadFuture=row['download_future'])
    for row in db.execute('SELECT body,scope FROM discover_rules WHERE enabled=1'):
        include(json.loads(row['body']),downloadFuture=row['scope']=='future')
    for row in db.execute("SELECT parent FROM managed_series WHERE monitoring='all'"):
        saved=db.execute("SELECT body FROM managed_media WHERE parent=? AND kind='episode' AND json_extract(body,'$.directTitleKey') IS NULL ORDER BY updated DESC LIMIT 1",(row['parent'],)).fetchone()
        if saved:include(json.loads(saved['body']).get('item'),downloadAll=True)
    for row in db.execute("SELECT * FROM managed_media WHERE monitor=1 AND removed=0 AND hold=0 AND json_extract(body,'$.automatic')=1 AND json_extract(body,'$.directTitleKey') IS NULL"):
        body=json.loads(row['body']);raw=body.get('item')
        if row['kind']=='movie':
            try:key,item=identity(raw)
            except (ValueError,TypeError):continue
            # Movie Options owns the policy once it exists, including explicit off.
            if db.execute("SELECT 1 FROM movie_options WHERE key=? OR json_extract(item,'$.id')=?",(key,item['id'])).fetchone():continue
        if row['kind']=='episode' and db.execute("SELECT 1 FROM managed_series WHERE parent=? AND monitoring='off'",(row['parent'],)).fetchone():continue
        include(raw)
    return result


def sync_auto_titles(store):
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='discover_series_plan'").fetchone():return
        db.execute('BEGIN IMMEDIATE')
        seen=seen_titles(db);previous=set(seen);existing=set()
        for row in db.execute('SELECT id,body FROM discover_watchlist'):
            try:existing.add(identity(json.loads(row['body']))[0])
            except (ValueError,TypeError):existing.add(row['id'])
        for key,entry in automatic_titles(db).items():
            if key in seen:continue
            if key not in existing:
                if db.execute('SELECT count(*) FROM discover_watchlist').fetchone()[0]>=500:continue
                db.execute('INSERT INTO discover_watchlist VALUES(?,?,?)',(key,json.dumps(entry['item']),time.time()))
                existing.add(key)
            seen.add(key)
        if seen!=previous:remember_titles(db,seen)


def sync_automatic(store):
    sync_auto_channels(store)
    sync_auto_titles(store)


def channel_art(value):
    return str(value) if re.fullmatch(r'/artwork/[A-Za-z0-9_.-]{1,120}',str(value or '')) else ds._image(value)


def seen_channels(db):
    row=db.execute('SELECT value FROM settings WHERE key=?',(AUTO_CHANNELS_SEEN,)).fetchone()
    value=json.loads(row[0]) if row else []
    if not isinstance(value,list) or any(not isinstance(v,str) or not re.fullmatch(r'UC[A-Za-z0-9_-]{22}',v) for v in value):
        raise ValueError('Could not read automatic channel Watchlist history.')
    return set(value)


def remember_channels(db,channels):
    db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(AUTO_CHANNELS_SEEN,json.dumps(sorted(channels))))


def channel_item(db,key):
    row=db.execute('SELECT data,active FROM channels WHERE id=?',(key,)).fetchone()
    if not row:raise ValueError('Open a saved channel first.')
    c=json.loads(row['data']);rule=db.execute('SELECT * FROM download_rules WHERE channel=?',(key,)).fetchone()
    count,latest=db.execute('SELECT count(*),max(day) FROM videos WHERE channel=?',(key,)).fetchone()
    enabled=bool(rule and any(rule[k] for k in ('videos','shorts','live')))
    status='Not subscribed' if not row['active'] else 'Automatic downloads paused' if enabled and rule['subscription_paused'] else 'Automatic downloads on' if enabled else 'Automatic downloads off'
    return dict(id=key,kind='channel',channelId=key,title=str(c.get('name') or 'YouTube channel')[:300],
        image=channel_art(c.get('avatar')),avatar=channel_art(c.get('avatar')),banner=channel_art(c.get('banner')),
        description=str(c.get('description') or '')[:800],genres=favourites.channel_genres(c),
        indexedVideos=count,latestUpload=latest or '',watchStatus=status)


def sync_auto_channels(store):
    """Enrol newly seen enabled channels; an explicit removal always wins.

    Startup, rule saves and the worker call this. Collection GET routes remain
    read-only. The durable seen set prevents removed channels reappearing later.
    """
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='discover_watchlist'").fetchone():return
        db.execute('BEGIN IMMEDIATE')
        seen=seen_channels(db);previous=set(seen)
        channels=db.execute('SELECT c.id FROM channels c JOIN download_rules r ON r.channel=c.id WHERE c.active=1 AND (r.videos=1 OR r.shorts=1 OR r.live=1)').fetchall()
        for row in channels:
            key=row['id']
            if key in seen or not re.fullmatch(r'UC[A-Za-z0-9_-]{22}',key):continue
            if not db.execute('SELECT 1 FROM discover_watchlist WHERE id=?',(key,)).fetchone():
                if db.execute('SELECT count(*) FROM discover_watchlist').fetchone()[0]>=500:continue
                db.execute('INSERT INTO discover_watchlist VALUES(?,?,?)',(key,json.dumps(channel_item(db,key)),time.time()))
            seen.add(key)
        if seen!=previous:remember_channels(db,seen)


def identity(raw):
    item=ds.clean_item(raw)
    if item['kind']=='movie':
        item=movie_options.clean(item);key=movie_options.key_for(item)
    elif item['kind']=='show':
        item=show_options.clean(item);key=show_options.key_for(item)
    elif item['kind']=='video':
        video=item.get('videoId') or item['id'].removeprefix('youtube:video:').removeprefix('video:')
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}',video):raise ValueError('Choose a YouTube video.')
        if item['id'] not in (video,'video:'+video,'youtube:video:'+video):raise ValueError('Video identities do not agree.')
        item.update(videoId=video,id='youtube:video:'+video);key=item['id']
    else:
        key=item['id'].removeprefix('youtube:channel:')
        if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}',key):raise ValueError('Choose a channel.')
        if item.get('channelId') and item['channelId']!=key:raise ValueError('Channel identities do not agree.')
        item['channelId']=key
    item={k:v for k,v in item.items() if k in FIELDS}
    if isinstance(raw.get('genres'),list):
        item['genres']=[v.strip()[:80] for v in raw['genres'][:20] if isinstance(v,str) and v.strip()]
    avatar=ds._image(raw.get('avatar'))
    if avatar:item['avatar']=avatar
    if item['kind']=='channel':
        item.update(image=channel_art(raw.get('image')),avatar=channel_art(raw.get('avatar')),banner=channel_art(raw.get('banner')))
    return key,item


def rows(store):
    with store.connect() as db:return [dict(r) for r in db.execute('SELECT * FROM discover_watchlist ORDER BY updated DESC')]


def matching(store,key):
    result=[]
    for row in rows(store):
        try:other,_=identity(json.loads(row['body']))
        except (ValueError,TypeError):other=row['id']
        if other==key:result.append(row)
    return result


def saved(store,key):
    return bool(matching(store,key))


def set_item(store,raw,selected):
    if type(selected) is not bool:raise ValueError('Choose on or off.')
    key,item=identity(raw)
    if item['kind']=='video':
        try:item.update(favourites.video_item(store,item['videoId']))
        except ValueError:pass  # Public metadata snapshot; no provider fetch or registration.
        item['id']=key;_,item=identity(item)
    now=time.time()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        existing=[]
        for row in db.execute('SELECT id,body FROM discover_watchlist').fetchall():
            try:other,_=identity(json.loads(row['body']))
            except (ValueError,TypeError):other=row['id']
            if other==key:existing.append(row['id'])
        if selected and not existing and db.execute('SELECT count(*) FROM discover_watchlist').fetchone()[0]>=500:
            raise ValueError('The watchlist is full. Remove a title first.')
        if selected and not existing:
            from .watchlist_policy import enable
            enable(db,key,item)
        if not selected:
            from .watchlist_policy import disable
            disable(db,key)
        for old in existing:
            if not selected or old!=key:db.execute('DELETE FROM discover_watchlist WHERE id=?',(old,))
        if selected:db.execute('INSERT INTO discover_watchlist VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,updated=excluded.updated',(key,json.dumps(item),now))
        elif item['kind']=='channel':remember_channels(db,seen_channels(db)-{key})
        else:remember_titles(db,seen_titles(db)-{key})
    return key,selected


def remove(store,key):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        records=db.execute('SELECT id,body FROM discover_watchlist').fetchall()
        identities={}
        for row in records:
            try:identities[row['id']]=identity(json.loads(row['body']))[0]
            except (ValueError,TypeError):identities[row['id']]=row['id']
        canonical=identities.get(key,key)
        if canonical in identities.values():
            from .watchlist_policy import disable
            disable(db,canonical)
        for saved_key,logical_key in identities.items():
            if logical_key==canonical:db.execute('DELETE FROM discover_watchlist WHERE id=?',(saved_key,))
        if re.fullmatch(r'UC[A-Za-z0-9_-]{22}',canonical):remember_channels(db,seen_channels(db)-{canonical})
        elif re.fullmatch(r'[A-Za-z0-9:_-]{1,250}',canonical):remember_titles(db,seen_titles(db)-{canonical})
    return canonical


def toggle(store,raw):
    key,_=identity(raw);_,selected=set_item(store,raw,not saved(store,key))
    return dict(saved=selected,items=ds.watchlist(store),key=key,watchlisted=selected)


def day(value):
    try:return date.fromisoformat(str(value or '')[:10]).isoformat()
    except ValueError:return ''


def video_state(app,store,item):
    from .media_library import local_file
    identity=item['videoId']
    with store.connect() as db:
        video=db.execute('SELECT data FROM videos WHERE id=?',(identity,)).fetchone()
        job=db.execute('SELECT state FROM downloads WHERE id=?',(identity,)).fetchone()
    raw=json.loads(video['data']) if video else {}
    published=day(raw.get('published') or raw.get('date') or item.get('published'))
    if local_file(store,app.config['MEDIA_DIR'],identity):return 'Downloaded',''
    if job:
        state=job['state']
        if state in ('queued','downloading','cancelling'):return state.capitalize(),''
        if state=='failed':return 'Needs attention',''
        if state=='cancelled':return 'Cancelled',''
        if state=='complete':return 'Saved file unavailable',''
    from .media_management.service import today
    if raw.get('state')=='live':return 'Live',''
    if raw.get('state')=='scheduled' or published>today():return 'Upcoming',published
    return ('Released',published) if published else ('Date TBA','')


def title_state(app,store,item):
    from .media_management import service
    option=(movie_options if item['kind']=='movie' else show_options).find(store,item)
    flags={}
    if option:
        state=(movie_options if item['kind']=='movie' else show_options).snapshot(store,option)
        flags={k:state[k] for k in ('automatic','downloadFuture','downloadAll','automaticSupported') if k in state}
    elif item['kind']=='show':
        # Read the pre-Options series plan without creating or changing a policy.
        state=show_options.snapshot(store,dict(key=show_options.key_for(item),item=json.dumps(item),shown=0,favourite=0,quality=None,minimum=None))
        flags={k:state[k] for k in ('downloadFuture','downloadAll','automaticSupported')}
    else:
        record=movie_options.managed(store,item)
        flags['automatic']=bool(record and record['monitor']==1 and not record['hold'] and not record['removed'])
    if item['kind']=='show':
        with store.connect() as db:
            upcoming=db.execute("SELECT json_extract(body,'$.date') FROM managed_media WHERE parent=? AND kind='episode' AND removed=0 AND json_extract(body,'$.date')>? ORDER BY json_extract(body,'$.date') LIMIT 1",(item['id'],service.today())).fetchone()
        return flags,('Next episode',day(upcoming[0])) if upcoming else ('Following TV show','')
    record=movie_options.managed(store,item)
    if record:
        current=service.compose(app,store,record)
        if current['file']:return flags,('Downloaded','')
        if current['active'] or record['hold'] or current['status']=='Needs attention':return flags,(current['status'],'')
    released=day((option or {}).get('release_date') or item.get('releaseDate'))
    return flags,('Upcoming' if released>service.today() else 'Released',released) if released else ('Date TBA','')


def listing(app,store):
    result=dict(movies=[],shows=[],videos=[],channels=[])
    with store.connect() as db:automatic=automatic_titles(db)
    seen=set()
    for row in rows(store):
        raw=json.loads(row['body'])
        try:key,item=identity(raw)
        except (ValueError,TypeError):continue
        if key in seen:continue
        seen.add(key)
        if item['kind'] in ('movie','show'):
            item=favourites.title_item(store,dict(item=json.dumps(item)))
            flags,(status,when)=title_state(app,store,item)
            flags.update(automatic.get(key,{}).get('flags',{}))
            item.update(flags,watchStatus=status,watchDate=when)
        elif item['kind']=='video':
            try:item.update(favourites.video_item(store,item['videoId']))
            except ValueError:pass
            _,item=identity(item)
            status,when=video_state(app,store,item)
            item.update(watchStatus=status,watchDate=when)
        else:
            try:
                with store.connect() as db:item.update(channel_item(db,key))
            except ValueError:item.update(watchStatus='Channel unavailable')
            item['id']=key
        item.update(watchlistKey=row['id'],watchlisted=True)
        result[{'movie':'movies','show':'shows','video':'videos','channel':'channels'}[item['kind']]].append(item)
    return result


def picker(store,query,offset):
    pattern='%'+query.replace('!','!!').replace('%','!%').replace('_','!_')+'%'
    with store.connect() as db:
        videos=db.execute("""SELECT v.id FROM videos v LEFT JOIN channels c ON c.id=v.channel
            WHERE json_extract(v.data,'$.state') IN ('published','scheduled','live')
            AND (json_extract(v.data,'$.title') LIKE ? ESCAPE '!' OR json_extract(c.data,'$.name') LIKE ? ESCAPE '!')
            ORDER BY v.published DESC,v.id LIMIT 61 OFFSET ?""",(pattern,pattern,offset)).fetchall()
    selected=set()
    for row in rows(store):
        try:selected.add(identity(json.loads(row['body']))[0])
        except (ValueError,TypeError):pass
    return dict(items=[dict(favourites.video_item(store,r['id']),watchlisted='youtube:video:'+r['id'] in selected) for r in videos[:60]],nextOffset=offset+60 if len(videos)>60 else None)


def register(app,store,access,csrf):
    sync_automatic(store)
    def guard(write=False):
        denied=access(local_action=True)
        if denied:return denied
        if write and not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return jsonify(error='Reload the page.'),403
        from .discover import _limited
        if not _limited(app,'watchlist_hits',180):return jsonify(error='Too many Watchlist requests. Try again shortly.'),429

    @app.get('/api/watchlist')
    def watchlist():
        denied=guard()
        if denied:return denied
        return jsonify(**listing(app,store),csrf=csrf())

    @app.route('/api/watchlist/item',methods=['GET','POST'])
    def watchlist_item():
        denied=guard(request.method=='POST')
        if denied:return denied
        try:
            if request.method=='GET':
                if set(request.args)!={'item'}:raise ValueError('Choose a Watchlist item.')
                raw=json.loads(request.args['item']);key,_=identity(raw)
            else:
                body=request.get_json(silent=True)
                if not isinstance(body,dict) or type(body.get('watchlisted')) is not bool:raise ValueError('Choose on or off.')
                if set(body)=={'key','watchlisted'} and body['watchlisted'] is False:
                    key=body['key']
                    if not isinstance(key,str) or not 1<=len(key)<=250 or any(ord(c)<32 for c in key):raise ValueError('Choose a Watchlist item.')
                    key=remove(store,key)
                elif set(body)=={'item','watchlisted'}:key,_=set_item(store,body['item'],body['watchlisted'])
                else:raise ValueError('Choose a Watchlist item.')
            return jsonify(key=key,watchlisted=saved(store,key),csrf=csrf())
        except (ValueError,TypeError,KeyError):return jsonify(error='Choose a valid Watchlist item.'),400

    @app.get('/api/watchlist/videos')
    def watchlist_videos():
        denied=guard()
        if denied:return denied
        try:
            offset=int(request.args.get('offset','0'))
            if not 0<=offset<=250000:raise ValueError()
            return jsonify(picker(store,request.args.get('q','').strip()[:160],offset))
        except ValueError:return jsonify(error='Choose a valid video page.'),400
