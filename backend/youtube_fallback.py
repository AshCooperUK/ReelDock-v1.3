"""Routine public upload feeds and bounded extraction without Google quota."""
import csv
import os
import signal
import io
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import http.client
from contextlib import contextmanager, nullcontext
from datetime import datetime,timezone,timedelta
from pathlib import Path
from xml.etree import ElementTree
from .sync_controls import check
from .vpn.transport import subprocess_env

CHANNEL=re.compile(r'UC[A-Za-z0-9_-]{22}')
VIDEO=re.compile(r'[A-Za-z0-9_-]{11}')
ATOM='{http://www.w3.org/2005/Atom}'
YT='{http://www.youtube.com/xml/schemas/2015}'
_lock=threading.Lock()
_foreground_lock=threading.Lock()


def initialise(store):
    with store.connect() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS youtube_manual_channels(id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS youtube_history_requests(channel TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS youtube_channel_requests(url TEXT PRIMARY KEY,state TEXT DEFAULT 'queued',message TEXT DEFAULT '',updated REAL DEFAULT 0);''')


def channel_url(value):
    value=value.strip()
    if CHANNEL.fullmatch(value):return 'https://www.youtube.com/channel/'+value
    if re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',value):value='@'+value
    if re.fullmatch(r'@[\w.-]{1,100}',value):return 'https://www.youtube.com/'+value
    match=re.fullmatch(r'https://(?:www\.|m\.)?youtube\.com/(channel/UC[A-Za-z0-9_-]{22}|@[\w.-]{1,100})(?:/(?:videos|shorts|streams))?/?',value)
    if not match:raise ValueError('Use a YouTube channel URL, @handle or channel ID.')
    return 'https://www.youtube.com/'+match[1]


def queue_channels(store,text):
    if not isinstance(text,str) or not text.strip() or len(text)>500000:raise ValueError('Enter channels or a subscriptions CSV (up to 1,000 channels).')
    rows=list(csv.reader(io.StringIO(text.lstrip('\ufeff'))))
    values=[]
    for row in rows:
        if not row or not any(x.strip() for x in row):continue
        if row[0].strip().lower() in ('channel id','channel_id'):continue
        value=next((x.strip() for x in row if CHANNEL.fullmatch(x.strip())),row[0].strip())
        values.append(channel_url(value))
    values=list(dict.fromkeys(values))
    if not values or len(values)>1000:raise ValueError('Enter between 1 and 1,000 channels.')
    with store.connect() as db:
        for url in values:
            db.execute("INSERT INTO youtube_channel_requests(url) VALUES(?) ON CONFLICT(url) DO UPDATE SET state='queued',message='',updated=0",(url,))
    return len(values)


@contextmanager
def _extract_slot(store, public, timeout, foreground=False):
    if not public:
        with _lock:yield None
        return
    deadline=time.monotonic()+min(30,max(1,timeout))
    lock=_foreground_lock if foreground else _lock
    acquired=False
    try:
        while not acquired:
            from .metadata_control import checkpoint
            checkpoint()
            check(store)
            if store.reset_pending():raise ValueError('ReelDock is resetting.')
            remaining=deadline-time.monotonic()
            if remaining<=0:raise ValueError('Public upload page is waiting for another upload page.' if foreground else 'Public channel information is waiting for another metadata request.')
            acquired=lock.acquire(timeout=min(.5,remaining))
        yield deadline
    finally:
        if acquired:lock.release()


def extract(importer,url,mode='video',start=1,end=1,*,public=False,timeout=90,foreground=False,guide=False):
    check(importer.store)
    if mode=='video':
        if not re.fullmatch(r'https://www.youtube.com/watch\?v=[A-Za-z0-9_-]{11}',url):raise ValueError('Invalid video address.')
    elif mode=='playlist':
        if not re.fullmatch(r'https://www.youtube.com/playlist\?list=UU[A-Za-z0-9_-]{22}',url):raise ValueError('Invalid uploads address.')
    elif mode=='subscriptions':
        if url!='https://www.youtube.com/feed/channels':raise ValueError('Invalid subscriptions address.')
    elif mode=='channel':
        url=channel_url(url)+'/videos'
    else:raise ValueError('Invalid metadata request.')
    if guide and (not foreground or not public or mode not in ('video','playlist')):raise ValueError('Invalid foreground Guide request.')
    bounded_video=guide and mode=='video' and type(start) is int and type(end) is int and start==end==1
    if foreground and (not public or not bounded_video and (mode!='playlist' or type(start) is not int or type(end) is not int or start<1 or end<start or end-start+1>26)):
        raise ValueError('Foreground reads support one bounded public upload page only.')
    # An explicitly viewed public upload page has one independent bounded slot.
    # Guide's single public video shares that same slot. Ordinary rich-detail,
    # banner, history and cookie work retain their original lock.
    with _extract_slot(importer.store,public,timeout,foreground) as deadline:
        from .metadata_control import checkpoint
        checkpoint()
        check(importer.store)
        if importer.store.reset_pending():raise ValueError('ReelDock is resetting.')
        pacing='youtube_upload_browse_next' if foreground else 'youtube_fallback_next'
        delay=float(importer.store.get(pacing,'0'))-time.time()
        if delay>0:time.sleep(min(delay,3,max(0,deadline-time.monotonic()) if deadline else 3))
        if deadline and time.monotonic()>=deadline:raise ValueError('Public channel information timed out.')
        checkpoint()
        check(importer.store)
        if importer.store.reset_pending():raise ValueError('ReelDock is resetting.')
        importer.store.set('worker_heartbeat',time.time())
        importer.store.set(pacing,time.time()+3)
        hook=importer.app.config.get('YOUTUBE_EXTRACT')
        if hook:return hook(url,mode,start,end)
        if importer.app.testing:raise ValueError('No test metadata extractor configured.')
        with tempfile.TemporaryDirectory(prefix='youtube-metadata-',dir=importer.store.directory) as folder:
            root=Path(folder); cookie=importer.store.directory/'youtube-cookies.txt'
            request=dict(url=url,mode=mode,start=start,end=end)
            from .watched_status import cookie_session_file_status
            if not public and cookie_session_file_status(cookie)[0] == 'unchecked':
                copy=root/'cookies.txt';copy.write_bytes(cookie.read_bytes());copy.chmod(0o600);request['cookie']=str(copy)
            if mode=='subscriptions' and 'cookie' not in request:
                raise ValueError('Upload valid signed-in YouTube cookies to import subscriptions.')
            output=root/'metadata.json'
            for attempt in range(2):
                check(importer.store)
                try:
                    with output.open('wb') as out:
                        run_extractor([sys.executable,'-m','backend.youtube_extract'],store=importer.store,input=json.dumps(request).encode(),stdout=out,
                                       stderr=subprocess.DEVNULL,timeout=max(.5,deadline-time.monotonic()) if deadline else timeout,check=True)
                    if output.stat().st_size>8_000_000:raise ValueError('Metadata response too large.')
                    return json.loads(output.read_text(encoding='utf-8'))
                except (subprocess.SubprocessError,OSError,ValueError):
                    if mode!='subscriptions' and request.pop('cookie',None):continue
                    raise ValueError('YouTube details could not be read without signing in. Retrying later; check video availability and saved cookies.') from None



def run_extractor(args, *, input, stdout, store, timeout=90, **_options):
    # Poll instead of blocking in subprocess.run so reset can interrupt a
    # metadata scan and clean up yt-dlp and its child processes promptly.
    process=subprocess.Popen(args,env=subprocess_env('metadata'),stdin=subprocess.PIPE,stdout=stdout,stderr=subprocess.DEVNULL,start_new_session=True)
    deadline=time.monotonic()+timeout
    first=True
    try:
        while True:
            from .metadata_control import checkpoint
            checkpoint()
            if store.reset_pending():raise ValueError('ReelDock is resetting.')
            remaining=deadline-time.monotonic()
            if remaining<=0:raise subprocess.TimeoutExpired(args,timeout)
            try:
                process.communicate(input=input if first else None,timeout=min(.5,remaining))
                break
            except subprocess.TimeoutExpired:
                first=False
        if process.returncode:raise subprocess.CalledProcessError(process.returncode,args)
    finally:
        if process.poll() is None:
            os.killpg(process.pid,signal.SIGTERM)
            try:process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL);process.wait()


def stamp(info):
    timestamp=info.get('release_timestamp') or info.get('timestamp')
    try:
        if timestamp:return datetime.fromtimestamp(float(timestamp),timezone.utc).isoformat()
        day=info.get('release_date') or info.get('upload_date')
        return datetime.strptime(day,'%Y%m%d').replace(tzinfo=timezone.utc).isoformat()
    except (ValueError,TypeError,OverflowError,OSError):return ''


def video_item(info,expected):
    if info.get('id')!=expected or not CHANNEL.fullmatch(info.get('channel_id') or ''):raise ValueError('YouTube returned incomplete video details.')
    published=stamp(info)
    if not published:raise ValueError('YouTube has not supplied an upload date yet.')
    live=info.get('live_status')
    seconds=max(0,int(info.get('duration') or 0))
    item=dict(id=expected,snippet=dict(channelId=info['channel_id'],title=info.get('title') or expected,
        description=info.get('description') or '',publishedAt=published,
        liveBroadcastContent={'is_upcoming':'upcoming','is_live':'live'}.get(live,'none'),
        thumbnails={'medium':{'url':info.get('thumbnail') or 'https://i.ytimg.com/vi/'+expected+'/hqdefault.jpg'}}),
        contentDetails={'duration':f'PT{seconds//3600}H{seconds//60%60}M{seconds%60}S'},statistics={'viewCount':info.get('view_count')})
    if live in ('is_upcoming','is_live','was_live','post_live'):item['liveStreamingDetails']={'scheduledStartTime':published}
    return item


def api(importer,resource,**params):
    if resource=='videos':
        items=[];failed=False
        ids=params.get('id','').split(',')
        if params.get('_limit'):ids=ids[:params['_limit']]
        for vid in ids:
            if not VIDEO.fullmatch(vid):raise ValueError('Invalid video ID.')
            try:
                info=extract(importer,'https://www.youtube.com/watch?v='+vid)
                items.append(video_item(info,vid))
            except ValueError:
                failed=True
                importer.store.set('youtube_fallback_error','Some video details are unavailable. Retrying later; check saved cookies if this continues.')
        if not failed:importer.store.set('youtube_fallback_error','')
        return {'items':items}
    if resource=='playlistItems':
        playlist=params.get('playlistId','')
        if not re.fullmatch(r'UU[A-Za-z0-9_-]{22}',playlist):raise ValueError('Channel uploads are not available yet.')
        cursor=params.get('pageToken','')
        offset=int(cursor[3:]) if re.fullmatch(r'fb:\d{1,7}',cursor) else 0
        size=10
        data=extract(importer,'https://www.youtube.com/playlist?list='+playlist,'playlist',offset+1,offset+size)
        entries=data.get('entries',[]);items=[]
        for entry in entries:
            if VIDEO.fullmatch(entry.get('id','')):
                items.append({'contentDetails':{'videoId':entry['id'],'videoPublishedAt':stamp(entry)}})
        queue_details(importer.store,[dict(id=i['contentDetails']['videoId'],snippet={'channelId':'UC'+playlist[2:]}) for i in items])
        return dict(items=items,nextPageToken=f'fb:{offset+size}' if len(entries)>=size else '')
    raise ValueError('This account action needs an enabled Google API connection with available allowance.')


def feed(importer,channel):
    check(importer.store)
    if not CHANNEL.fullmatch(channel):raise ValueError('Invalid channel.')
    hook=importer.app.config.get('YOUTUBE_FEED')
    if hook:body=hook(channel)
    else:
        if importer.app.testing:raise ValueError('No test feed configured.')
        connection=http.client.HTTPSConnection('www.youtube.com',timeout=15)
        try:
            connection.request('GET','/feeds/videos.xml?channel_id='+channel)
            response=connection.getresponse()
            if response.status!=200:raise ValueError('Upload feed is temporarily unavailable.')
            body=response.read(256001)
        finally:connection.close()
    if len(body)>256000 or b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():raise ValueError('Invalid upload feed.')
    root=ElementTree.fromstring(body)
    if root.tag!=ATOM+'feed':raise ValueError('Invalid upload feed.')
    items=[]
    for entry in root.findall(ATOM+'entry'):
        vid=entry.findtext(YT+'videoId','');owner=entry.findtext(YT+'channelId','')
        if not VIDEO.fullmatch(vid) or owner!=channel:continue
        published=entry.findtext(ATOM+'published') or ''
        try:
            parsed=datetime.fromisoformat(published.replace('Z','+00:00'))
            if parsed.tzinfo is None:continue
            published=parsed.astimezone(timezone.utc).isoformat()
        except ValueError:continue
        items.append(dict(id=vid,snippet=dict(channelId=owner,title=entry.findtext(ATOM+'title') or vid,
            publishedAt=published,thumbnails={'medium':{'url':'https://i.ytimg.com/vi/'+vid+'/hqdefault.jpg'}}),_pending=True))
    return items


def promote_observed(db,ids,channel=None):
    """A normal RSS/history observation restores ordinary catalogue semantics."""
    for start in range(0,len(ids),500):
        observed=ids[start:start+500]
        owner=' AND channel=?' if channel is not None else ''
        db.execute("UPDATE videos SET data=json_set(json_remove(data,'$.browse_only'),'$.forecast_eligible',json(CASE WHEN json_extract(data,'$.state')='published' AND COALESCE(json_extract(data,'$.metadata_pending'),0)=0 AND COALESCE(json_extract(data,'$.kind'),'unknown')!='live' THEN 'true' ELSE 'false' END)) WHERE id IN ("+','.join('?' for _ in observed)+")"+owner+" AND json_extract(data,'$.browse_only')=1 AND channel IN (SELECT id FROM channels WHERE active=1) AND NOT EXISTS(SELECT 1 FROM settings WHERE key IN ('youtube_sync_paused','reset_in_progress') AND value IN ('true','1'))",[*observed,*([channel] if channel is not None else [])])


def placeholder(importer,items):
    # Never replace complete metadata with an incomplete notification/feed entry.
    ids=list(dict.fromkeys(item['id'] for item in items));known=set()
    with importer.store.connect() as db:
        for start in range(0,len(ids),500):
            batch=ids[start:start+500]
            known.update(r[0] for r in db.execute('SELECT id FROM videos WHERE id IN ('+','.join('?' for _ in batch)+')',batch))
        promote_observed(db,ids)
    importer.save_videos([item for item in items if item['id'] not in known])


def queue_details(store,items,*,transaction=None):
    import hashlib
    now=time.time()
    with (store.connect() if transaction is None else nullcontext(transaction)) as db:
        if transaction is None:db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM settings WHERE key IN ('youtube_sync_paused','reset_in_progress') AND value IN ('true','1') LIMIT 1").fetchone():return
        for item in items:
            if not db.execute('SELECT 1 FROM channels WHERE id=? AND active=1',(item['snippet']['channelId'],)).fetchone():continue
            saved=db.execute('SELECT data,checked FROM videos WHERE id=?',(item['id'],)).fetchone()
            if saved and not json.loads(saved['data']).get('metadata_pending') and json.loads(saved['data']).get('state') == 'published':continue
            if saved and not json.loads(saved['data']).get('metadata_pending') and saved['checked']>now-1800:continue
            digest=hashlib.sha256(('feed:'+item['id']).encode()).hexdigest()
            db.execute('INSERT OR IGNORE INTO youtube_push_events(id,channel,digest,received) VALUES(?,?,?,?)',
                       (item['id'],item['snippet']['channelId'],digest,now))
            db.execute('UPDATE youtube_push_events SET done=0 WHERE id=? AND done=1',(item['id'],))


def channel_requests_queued(store):
    with store.connect() as db:
        return bool(db.execute("SELECT 1 FROM youtube_channel_requests WHERE state='queued' LIMIT 1").fetchone())


def rebuild_forecasts_once(store):
    """Rebuild upload forecasts after a channel import batch, not per channel."""
    from .forecasts import rebuild
    rebuild(store);store.set('forecasts_pending','0')


def channel_banner(data):
    """Pick a header-sized banner from yt-dlp channel thumbnails.

    yt-dlp lists banners as wide images with numeric ids plus a huge
    'banner_uncropped'. Prefer the smallest wide image at least 1000px across."""
    images=[x for x in data.get('thumbnails') or [] if isinstance(x,dict) and isinstance(x.get('url'),str) and x['url']]
    def size(x):
        try:return int(x.get('width') or 0),int(x.get('height') or 0)
        except (TypeError,ValueError):return 0,0
    wide=sorted((x for x in images if size(x)[1] and size(x)[0]>=2.5*size(x)[1]),key=lambda x:size(x)[0])
    pick=next((x for x in wide if size(x)[0]>=1000),wide[-1] if wide else None)
    if pick:return pick['url']
    return next((x['url'] for x in images if 'banner' in str(x.get('id','')).lower()),'')


def channel_followers(data):
    value=data.get('channel_follower_count')
    return str(value) if isinstance(value,int) and not isinstance(value,bool) and value>=0 else None


CHANNEL_ART_GAP=10  # seconds between background channel-detail reads
CHANNEL_ART_RECHECK=30*86400


def backfill_channel_art(importer):
    """Without the Google API, fill banners and subscriber counts one channel at a time.

    Cookie subscription imports only list avatars. Each read is one small yt-dlp
    channel request (the same one used when adding a channel), spaced out and
    skipped while imports are queued, then repeated monthly."""
    from .youtube_source import fallback
    from .sync_controls import SyncPaused
    from .catalogue import thumbnail
    store=importer.store
    if not fallback(store) or channel_requests_queued(store):return
    now=time.time()
    if float(store.get('channel_art_next','0') or 0)>now:return
    with store.connect() as db:
        row=db.execute("""SELECT id FROM channels WHERE active=1
            AND COALESCE(CAST(json_extract(data,'$.art_checked') AS REAL),0)<?
            ORDER BY COALESCE(CAST(json_extract(data,'$.art_checked') AS REAL),0),id LIMIT 1""",(now-CHANNEL_ART_RECHECK,)).fetchone()
    if not row:return
    store.set('channel_art_next',now+CHANNEL_ART_GAP)
    identity=row[0];updates={}
    try:
        data=extract(importer,identity,'channel')
        if (data.get('channel_id') or data.get('id'))!=identity:raise ValueError('Unexpected channel.')
    except SyncPaused:return
    except Exception:
        updates['art_checked']=now-CHANNEL_ART_RECHECK+86400  # retry tomorrow
    else:
        updates['art_checked']=now
        banner=channel_banner(data)
        if banner:updates['banner']=thumbnail(store,{'thumbnails':{'medium':{'url':banner}}})
        followers=channel_followers(data)
        if followers is not None:updates['subscribers']=followers
    paths=','.join("'$.%s',?"%k for k in updates)
    with store.connect() as db:
        db.execute('UPDATE channels SET data=json_set(data,'+paths+') WHERE id=?',(*updates.values(),identity))


def add_batch(importer):
    store=importer.store
    with store.connect() as db:
        row=db.execute("SELECT * FROM youtube_channel_requests WHERE state='queued' OR (state='failed' AND updated<?) ORDER BY updated LIMIT 1",(time.time()-3600,)).fetchone()
    if not row:
        if store.get('forecasts_pending')=='1':rebuild_forecasts_once(store)
        return
    try:
        data=extract(importer,row['url'],'channel');identity=data.get('channel_id') or data.get('id')
        if not CHANNEL.fullmatch(identity or ''):raise ValueError('Could not identify this YouTube channel.')
        from .catalogue import thumbnail
        name=data.get('channel') or data.get('uploader') or data.get('title') or identity
        images=[x for x in data.get('thumbnails',[]) if x.get('url')]
        avatar=next((x['url'] for x in images if 'avatar' in str(x.get('id','')).lower()),'')
        if not avatar:avatar=next((x['url'] for x in images if x.get('width') and x.get('width')==x.get('height')),'')
        banner=channel_banner(data)
        art=lambda url:thumbnail(store,{'thumbnails':{'medium':{'url':url}}}) if url else ''
        with store.connect() as db:
            old=db.execute('SELECT data FROM channels WHERE id=?',(identity,)).fetchone()
            channel=json.loads(old[0]) if old else dict(id=identity,initials=''.join(w[0] for w in name.split()[:2]),color='#9ee6cf',art='landscape')
            channel.update(name=name,description=data.get('description') or channel.get('description',''),playlist='UU'+identity[2:],coverage='Current week queued',metadata_checked=time.time())
            followers=channel_followers(data)
            if followers is not None:channel['subscribers']=followers
        channel['avatar']=art(avatar) or channel.get('avatar','');channel['banner']=art(banner) or channel.get('banner','')
        from .download_options import read
        options=read(store)
        with store.connect() as db:
            db.execute('INSERT OR REPLACE INTO channels VALUES(?,?,1)',(identity,json.dumps(channel)))
            db.execute('INSERT OR IGNORE INTO youtube_manual_channels VALUES(?)',(identity,))
            db.execute('DELETE FROM removed_channels WHERE id=?',(identity,))
            db.execute('INSERT OR IGNORE INTO download_rules(channel,quality,videos,shorts,live,range_days,enabled_at,quality_override,range_override) VALUES(?,?,?,0,0,?,?,0,0)',
                       (identity,store.get('download_quality','1080'),int(options['new_channel_videos']),options['default_range'],time.time()))
            db.execute("UPDATE youtube_channel_requests SET state='complete',message='Channel added.',updated=? WHERE url=?",(time.time(),row['url']))
        from .guide_weeks import request_week,current_week
        request_week(store,current_week().isoformat())
        # A CSV import can queue hundreds of channels; each forecast rebuild
        # reads every saved video, so rebuild once when the queue is empty.
        store.set('forecasts_pending','1');store.set('next_sync',0)
    except Exception:
        with store.connect() as db:db.execute("UPDATE youtube_channel_requests SET state='failed',message='Could not read this channel. Check its address or saved cookies. Retrying later.',updated=? WHERE url=?",(time.time(),row['url']))
    if store.get('forecasts_pending')=='1' and not channel_requests_queued(store):rebuild_forecasts_once(store)


def run(importer):
    store=importer.store
    started=float(store.get('recent_scan_started','0')) or time.time();store.set('recent_scan_started',started)
    with store.connect() as db:rows=[json.loads(r[0]) for r in db.execute('SELECT data FROM channels WHERE active=1')]
    pending=[c for c in rows if float(c.get('last_checked') or 0)<started]
    from .guide_weeks import current_week,request_week
    from .channel_downloads import preferences
    errors=0
    for index,channel in enumerate(pending[:5],start=len(rows)-len(pending)):
        importer.stage('videos',index,len(rows))
        check(store)
        importer.progress('Checking public channel upload feeds.')
        try:
            from .channel_refresh import refresh
            result=refresh(importer,channel['id'],since=started)
            if result is None:continue  # A viewed-channel read already owns/freshened it.
            if not result['success']:
                errors+=1
                continue
            feed_items=result['items']
            # Keep the normal import's existing guide/history policy. A viewed
            # refresh itself only adds recent observations and metadata work.
            if feed_items and any(i['snippet']['publishedAt'][:10]<current_week().isoformat() for i in feed_items):
                with store.connect() as db:
                    db.execute("INSERT INTO guide_week_jobs(channel,week,state,updated,requested) VALUES(?,?,'complete',?,?) ON CONFLICT(channel,week) DO UPDATE SET state='complete',updated=excluded.updated,error='',retry_at=0",(channel['id'],current_week().isoformat(),time.time(),time.time()))
            rule=preferences(store,channel['id'])
            if any(rule[k] for k in ('videos','shorts','live')):
                from .history import seed
                with store.connect() as db:owner=db.execute('SELECT data FROM channels WHERE id=? AND active=1',(channel['id'],)).fetchone()
                if owner:seed(store,json.loads(owner['data']),{'nextPageToken':'fb:0'})
        except Exception as exc:
            from .sync_controls import SyncPaused
            if isinstance(exc,SyncPaused):raise
            errors+=1
    with store.connect() as db:latest=[json.loads(r[0]) for r in db.execute('SELECT data FROM channels WHERE active=1')]
    remaining=sum(float(channel.get('last_checked') or 0)<started for channel in latest)
    store.set('youtube_fallback_remaining',remaining)
    if not remaining:
        request_week(store,current_week().isoformat())
        if channel_requests_queued(store):store.set('forecasts_pending','1')  # add_batch rebuilds when the import ends
        else:rebuild_forecasts_once(store)
        importer.prune_artwork()
        store.set('recent_scan_started',0);store.set('last_sync',time.time())
        from .sync_schedule import minutes
        store.set('next_sync',time.time()+minutes(store)*60)
        importer.stage('complete',len(rows),len(rows))
    account_error=store.get('youtube_subscriptions_error') if store.get('youtube_use_google_api')!='false' else ''
    message='Some upload feeds could not be checked. Saved videos are available; checks will retry.' if errors else ''
    if account_error:message+=' Subscription list could not refresh. Known channels still use public feeds. '+account_error
    store.set('sync_error',message.strip())
    importer.progress(f'Checking public upload feeds: {remaining} remaining.' if remaining else 'Upload feeds checked. Missing video details are being collected.')

def fill_dates(store,listing):
    """Flat yt-dlp lists may lack dates; use the details just saved to stop paging."""
    with store.connect() as db:
        for item in listing.get('items',[]):
            details=item['contentDetails']
            if not details.get('videoPublishedAt'):
                row=db.execute("SELECT published FROM videos WHERE id=? AND COALESCE(json_extract(data,'$.metadata_pending'),0)=0",(details['videoId'],)).fetchone()
                if row:details['videoPublishedAt']=row[0]
