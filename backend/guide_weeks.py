"""Demand-driven weekly guide imports; cached reads never call Google."""
import json
import re
import secrets
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

LONDON = ZoneInfo('Europe/London')
SCAN_INTERVAL = 6 * 3600
VIEW_TTL = 300
LEASE = 120


def current_week():
    today = datetime.now(LONDON).date()
    return today - timedelta(days=today.weekday())


def initialise(store):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("""CREATE TABLE IF NOT EXISTS guide_week_jobs(
          channel TEXT, week TEXT, cursor TEXT DEFAULT '', state TEXT DEFAULT 'queued',
          updated REAL DEFAULT 0, requested REAL DEFAULT 0, retry_at REAL DEFAULT 0,
          error TEXT DEFAULT '', trail TEXT DEFAULT '[]', PRIMARY KEY(channel,week))""")
        columns={row[1] for row in db.execute('PRAGMA table_info(guide_week_jobs)')}
        for name,kind in [('claim',"TEXT NOT NULL DEFAULT ''"),('lease_until','REAL NOT NULL DEFAULT 0')]:
            if name not in columns:db.execute('ALTER TABLE guide_week_jobs ADD COLUMN '+name+' '+kind)


def request_week(store, start, *, foreground=False):
    """Deduplicate tabs/repeated selections; never automatically request other weeks."""
    start = date.fromisoformat(start)
    if start.weekday() != 0:
        raise ValueError('Choose a week beginning on Monday.')
    if start > current_week():
        if foreground:
            with store.connect() as db:db.execute("DELETE FROM settings WHERE key='guide_view_demand'")
        return  # Future scheduled videos arrive through recent scans/push, not history.
    now = time.time()
    freshness = SCAN_INTERVAL if start == current_week() else 7 * 86400
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        channels = db.execute("""SELECT c.id FROM channels c LEFT JOIN channel_preferences p ON p.id=c.id
            WHERE c.active=1 AND COALESCE(p.shown,1)=1""").fetchall()
        for channel in channels:
            # Reuse the earlier history import while that coverage is still fresh.
            history=db.execute('''SELECT h.state,h.updated,h.cursor,MIN(v.day) AS oldest FROM history_jobs h
              LEFT JOIN videos v ON v.channel=h.channel WHERE h.channel=? GROUP BY h.channel''',(channel['id'],)).fetchone()
            covered=history and history['state']=='complete' and history['updated']>now-freshness and (not history['cursor'] or history['oldest'] and history['oldest']<=start.isoformat())
            if covered:
                db.execute("INSERT OR IGNORE INTO guide_week_jobs(channel,week,state,updated,requested) VALUES(?,?,'complete',?,?)",(channel['id'],start.isoformat(),history['updated'],now))
            db.execute("""INSERT INTO guide_week_jobs(channel,week,requested) VALUES(?,?,?)
                ON CONFLICT(channel,week) DO UPDATE SET cursor='',state='queued',requested=excluded.requested,
                retry_at=0,error='',trail='[]' WHERE guide_week_jobs.state='complete' AND guide_week_jobs.updated<?""",
                (channel['id'], start.isoformat(), now, now-freshness))
        db.execute("DELETE FROM guide_week_jobs WHERE channel NOT IN (SELECT id FROM channels WHERE active=1)")
        db.execute("DELETE FROM guide_week_jobs WHERE state='complete' AND requested<?", (now-28*86400,))
        if foreground:
            db.execute("INSERT OR REPLACE INTO settings VALUES('guide_view_demand',?)",(json.dumps(dict(week=start.isoformat(),requested=now,expires=now+VIEW_TTL)),))


def page_dates(listing):
    days=[]
    for item in listing.get('items', []):
        try:days.append(datetime.fromisoformat(item['contentDetails']['videoPublishedAt'].replace('Z','+00:00')).astimezone(LONDON).date())
        except (KeyError,ValueError,TypeError):days.append(None)
    return days


def finished(listing, start):
    days=page_dates(listing)
    return not listing.get('nextPageToken') or (bool(days) and all(day is not None and day < start for day in days))


def seed_recent(store, channel, listing):
    """Reuse the recent scan's first page; no duplicate first-page API call."""
    start=current_week();now=time.time()
    state='complete' if finished(listing,start) else 'queued'
    with store.connect() as db:
        db.execute("""INSERT INTO guide_week_jobs(channel,week,cursor,state,updated,requested) VALUES(?,?,?,?,?,?)
          ON CONFLICT(channel,week) DO UPDATE SET cursor=excluded.cursor,state=excluded.state,
          updated=excluded.updated,requested=excluded.requested,error='',retry_at=0,trail='[]'
          WHERE guide_week_jobs.state='complete' OR guide_week_jobs.cursor=''""",
          (channel['id'],start.isoformat(),listing.get('nextPageToken',''),state,now,now))


def status(store, start):
    with store.connect() as db:
        rows=db.execute("""SELECT j.state,j.error FROM guide_week_jobs j JOIN channels c ON c.id=j.channel
          WHERE j.week=? AND c.active=1 AND NOT EXISTS(SELECT 1 FROM channel_preferences p WHERE p.id=c.id AND p.shown=0)""",(start,)).fetchall()
    pending=sum(row['state']!='complete' for row in rows)
    if not pending:return ''
    from .youtube_source import fallback
    source=' Using upload feeds and yt-dlp.' if fallback(store) else ''
    if store.get('youtube_sync_paused')=='true':return 'YouTube updates are paused. Showing saved videos for this week.'
    if any(row['error'] for row in rows):return 'Some channels could not update yet. Showing saved videos; retries are queued.'
    return f'Updating this week: {pending} channels remaining. Showing saved videos as they arrive.'+source


def _paused(db):
    return bool(db.execute("SELECT 1 FROM settings WHERE key IN ('youtube_sync_paused','reset_in_progress') AND value IN ('true','1') LIMIT 1").fetchone())


def _demand(db):
    row=db.execute("SELECT value FROM settings WHERE key='guide_view_demand'").fetchone()
    try:
        value=json.loads(row[0]) if row else {}
        return value if isinstance(value,dict) and type(value.get('expires')) in (int,float) and value['expires']>time.time() else {}
    except (TypeError,ValueError):return {}


def _claim(store, foreground, stop):
    if stop is not None and stop.is_set():return None
    if foreground and not store.get('setup_complete'):return None
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if _paused(db):return None
        demand=_demand(db)
        if foreground and not demand:return None
        compatible="(j.cursor='' OR (substr(j.cursor,1,3)='fb:' AND length(j.cursor) BETWEEN 4 AND 10 AND substr(j.cursor,4) NOT GLOB '*[^0-9]*'))"
        clause=' AND j.week=? AND '+compatible if foreground else ' AND (j.week!=? OR NOT '+compatible+')' if demand else ''
        values=[time.time(),time.time(),*([demand['week']] if demand else [])]
        job=db.execute("""SELECT j.*,c.data FROM guide_week_jobs j JOIN channels c ON c.id=j.channel
            LEFT JOIN channel_favourites f ON f.id=c.id WHERE j.state='queued' AND c.active=1
            AND j.retry_at<=? AND j.lease_until<=?
            AND NOT EXISTS(SELECT 1 FROM channel_preferences p WHERE p.id=c.id AND p.shown=0)"""+clause+"""
            ORDER BY (f.id IS NOT NULL) DESC,j.requested DESC,j.updated,j.channel LIMIT 1""",values).fetchone()
        if not job:return None
        token=secrets.token_hex(16)
        db.execute('UPDATE guide_week_jobs SET claim=?,lease_until=? WHERE channel=? AND week=?',(token,time.time()+LEASE,job['channel'],job['week']))
        return dict(job,claim=token)


def _valid(db, job, foreground=False, stop=None):
    if stop is not None and stop.is_set() or _paused(db):return False
    if foreground and _demand(db).get('week')!=job['week']:return False
    return bool(db.execute("""SELECT 1 FROM guide_week_jobs j JOIN channels c ON c.id=j.channel
        WHERE j.channel=? AND j.week=? AND j.claim=? AND j.cursor=? AND j.state='queued' AND c.active=1
        AND NOT EXISTS(SELECT 1 FROM channel_preferences p WHERE p.id=c.id AND p.shown=0)""",(job['channel'],job['week'],job['claim'],job['cursor'])).fetchone())


def _public_page(importer, job, timeout):
    from .youtube_fallback import extract,stamp
    if job['cursor'] and not re.fullmatch(r'fb:\d{1,7}',job['cursor']):raise ValueError('Unsupported Guide page token.')
    offset=int(job['cursor'][3:]) if re.fullmatch(r'fb:\d{1,7}',job['cursor']) else 0
    data=extract(importer,'https://www.youtube.com/playlist?list=UU'+job['channel'][2:],'playlist',offset+1,offset+10,public=True,foreground=True,guide=True,timeout=timeout)
    if data.get('channel_id') and data['channel_id']!=job['channel']:raise ValueError('Unexpected Guide channel.')
    entries=data.get('entries')
    if not isinstance(entries,list) or len(entries)>10:raise ValueError('Invalid Guide page.')
    items=[]
    for entry in entries:
        if not isinstance(entry,dict):raise ValueError('Invalid Guide video.')
        if entry.get('channel_id') and entry['channel_id']!=job['channel']:raise ValueError('Unexpected Guide video.')
        if re.fullmatch(r'[A-Za-z0-9_-]{11}',str(entry.get('id') or '')):
            items.append(dict(contentDetails=dict(videoId=entry['id'],videoPublishedAt=stamp(entry))))
    return dict(items=items,nextPageToken='fb:'+str(offset+10) if len(entries)>=10 else '')


def batch(importer, limit=2, *, foreground=False, stop=None):
    store=importer.store
    attempted=False
    for _ in range(limit):
        job=_claim(store,foreground,stop)
        if not job:return attempted
        attempted=True
        deadline=time.monotonic()+30
        def allowed():
            with store.connect() as db:return _valid(db,job,foreground,stop)
        try:
            channel=json.loads(job['data']);start=date.fromisoformat(job['week']);end=start+timedelta(days=6)
            if not channel.get('playlist'):listing={'items':[]}
            elif foreground:
                from .metadata_control import work
                with work(allowed):listing=_public_page(importer,job,max(1,deadline-time.monotonic()))
            else:listing=importer.api('playlistItems',part='contentDetails',playlistId=channel['playlist'],maxResults=50,pageToken=job['cursor'])
            cursor=listing.get('nextPageToken','');trail=json.loads(job['trail'])
            if cursor and cursor in [job['cursor'],*trail]:raise ValueError('Repeated guide page.')
            ids=[item['contentDetails']['videoId'] for item,day in zip(listing.get('items',[]),page_dates(listing)) if day is None or start<=day<=end]
            with store.connect() as db:
                cached={r['id'] for r in db.execute("SELECT id FROM videos WHERE COALESCE(json_extract(data,'$.metadata_pending'),0)=0 AND (json_extract(data,'$.state')='published' OR checked>?)",(time.time()-1800,))}
            ids=[identity for identity in ids if identity not in cached]
            with store.connect() as db:
                if not _valid(db,job,foreground,stop):continue
            if foreground:
                from .youtube_fallback import queue_details
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    if not _valid(db,job,True,stop):continue
                    queue_details(store,[dict(id=identity,snippet=dict(channelId=job['channel'])) for identity in ids],transaction=db)
                ids=ids[:1]  # One rich detail per turn; normal hydration keeps the rest.
                if deadline-time.monotonic()<1:ids=[]
            if ids:
                if foreground:
                    from .youtube_fallback import extract,video_item
                    from .metadata_control import work
                    with work(allowed):info=extract(importer,'https://www.youtube.com/watch?v='+ids[0],public=True,foreground=True,guide=True,timeout=max(1,deadline-time.monotonic()))
                    if info.get('channel_id')!=job['channel']:raise ValueError('Unexpected Guide video channel.')
                    result=dict(items=[video_item(info,ids[0])])
                else:result=importer.api('videos',part='snippet,contentDetails,liveStreamingDetails,statistics',id=','.join(ids),maxResults=50)
                importer.save_videos(result.get('items',[]),guard=lambda db:_valid(db,job,foreground,stop))
            from .youtube_fallback import fill_dates
            fill_dates(store,listing)
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if not _valid(db,job,foreground,stop):continue
                db.execute("""UPDATE guide_week_jobs SET cursor=?,state=?,updated=?,retry_at=0,error='',trail=? WHERE channel=? AND week=?""",
                  (cursor,'complete' if finished(listing,start) else 'queued',time.time(),json.dumps([*trail,job['cursor']]),job['channel'],job['week']))
        except Exception as exc:
            from .metadata_control import Cancelled
            if isinstance(exc,Cancelled):return attempted
            with store.connect() as db:
                if _valid(db,job,foreground,stop):
                    db.execute("UPDATE guide_week_jobs SET updated=?,retry_at=?,error=? WHERE channel=? AND week=? AND claim=?",
                      (time.time(),time.time()+300,'Guide request failed; retry pending.',job['channel'],job['week'],job['claim']))
            return attempted
        finally:
            with store.connect() as db:
                db.execute("UPDATE guide_week_jobs SET claim='',lease_until=0 WHERE channel=? AND week=? AND claim=?",(job['channel'],job['week'],job['claim']))
    return attempted


def recover(store):
    with store.connect() as db:db.execute("UPDATE guide_week_jobs SET claim='',lease_until=0 WHERE claim!='' OR lease_until!=0")


def loop(app,store,stop):
    from .catalogue import Importer
    importer=Importer(app)
    while not stop.is_set():
        with store.background_operation():
            try:batch(importer,1,foreground=True,stop=stop)
            except Exception:app.logger.warning('Viewed Guide will retry.')
        stop.wait(1)
