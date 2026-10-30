"""On-demand public upload metadata pages, independent of automation/history."""
import hashlib
import json
import math
import re
import secrets
import time

from .channel_refresh import CHANNEL, _paused

PAGE = 25
TTL = 300
RETRY = 60
LEASE = 120


def initialise(store):
    with store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS channel_upload_pages(
            channel TEXT NOT NULL,offset INTEGER NOT NULL,state TEXT NOT NULL DEFAULT 'idle',
            ids TEXT NOT NULL DEFAULT '[]',has_more INTEGER NOT NULL DEFAULT 1,
            requested REAL NOT NULL DEFAULT 0,expires REAL NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL DEFAULT 0,claim TEXT NOT NULL DEFAULT '',
            lease_until REAL NOT NULL DEFAULT 0,fingerprint TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(channel,offset))''')
        db.execute('''CREATE TRIGGER IF NOT EXISTS channel_upload_pages_removed AFTER DELETE ON channels
            BEGIN DELETE FROM channel_upload_pages WHERE channel=OLD.id; END''')


def offset_of(cursor):
    if not isinstance(cursor, str) or not re.fullmatch(r'0|[1-9][0-9]{0,6}', cursor):
        raise ValueError('Choose a valid channel upload page.')
    offset = int(cursor)
    if offset % PAGE:
        raise ValueError('Choose a valid channel upload page.')
    return offset


def status(store, channel, cursor='0'):
    offset = offset_of(cursor)
    now = time.time()
    with store.connect() as db:
        owner = db.execute('SELECT 1 FROM channels WHERE id=? AND active=1', (channel,)).fetchone()
        row = db.execute('SELECT * FROM channel_upload_pages WHERE channel=? AND offset=?', (channel, offset)).fetchone()
        paused = _paused(db)
        items = []
        if row and row['state']=='complete' and owner:
            ids = json.loads(row['ids'])
            if ids:
                saved = {record['id']:json.loads(record['data']) for record in db.execute('SELECT id,data FROM videos WHERE channel=? AND id IN ('+','.join('?' for _ in ids)+')', [channel,*ids])}
                items = [saved[identity] for identity in ids if identity in saved]
    result = dict(cursor=cursor,state='idle',nextCursor=cursor,hasMore=bool(owner),refreshAfterMs=0,notice='')
    if not owner:
        result.update(state='complete',nextCursor=None)
    elif row and row['state']=='complete':
        result.update(state='idle' if row['has_more'] else 'complete',nextCursor=str(offset+PAGE) if row['has_more'] else None,hasMore=bool(row['has_more']),page=dict(cursor=cursor,items=items))
    elif paused:
        result.update(state='paused',notice='YouTube updates are paused. Saved uploads remain available.')
    elif row and row['lease_until']>now:
        result.update(state='running',notice='Loading older uploads…',refreshAfterMs=2000)
    elif row and row['retry_at']>now:
        result.update(state='retry',notice='The public upload page could not update yet. Retrying shortly.',refreshAfterMs=min(5000,max(1000,int((row['retry_at']-now)*1000))),retryAt=row['retry_at'])
    elif row and row['expires']>now and row['state'] in ('queued','running','retry'):
        result.update(state='queued',notice='Loading older uploads…',refreshAfterMs=2000)
    return result


def request_page(store, channel, cursor):
    offset = offset_of(cursor)
    if not isinstance(channel,str) or not CHANNEL.fullmatch(channel):
        raise ValueError('Choose a valid YouTube channel.')
    now = time.time()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM channels WHERE id=? AND active=1',(channel,)).fetchone():
            raise LookupError('Add this channel to ReelDock before browsing its uploads.')
        row = db.execute('SELECT * FROM channel_upload_pages WHERE channel=? AND offset=?',(channel,offset)).fetchone()
        if not row and offset:
            previous = db.execute("SELECT has_more FROM channel_upload_pages WHERE channel=? AND offset=? AND state='complete'",(channel,offset-PAGE)).fetchone()
            if not previous or not previous['has_more']:
                raise ValueError('Load the previous public upload page first.')
        if not _paused(db) and not (row and row['state']=='complete'):
            db.execute('INSERT OR IGNORE INTO channel_upload_pages(channel,offset) VALUES(?,?)',(channel,offset))
            db.execute("UPDATE channel_upload_pages SET requested=?,expires=?,state=CASE WHEN lease_until>? THEN 'running' WHEN retry_at>? THEN 'retry' ELSE 'queued' END WHERE channel=? AND offset=?",(now,now+TTL,now,now,channel,offset))
    return status(store,channel,cursor)


def _basic(entry, channel):
    identity = entry.get('id')
    if not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}',identity):
        return None
    if entry.get('channel_id') and entry['channel_id']!=channel:
        return None
    title = entry.get('title')
    if not isinstance(title,str) or not title.strip() or title.strip().lower() in ('[deleted video]','[private video]','deleted video','private video'):
        return None
    from .youtube_fallback import stamp
    published=stamp(entry)
    day,clock='',''
    if published:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        try:
            date=datetime.fromisoformat(published).astimezone(ZoneInfo('Europe/London'))
            day,clock=date.date().isoformat(),date.strftime('%H:%M')
        except (TypeError,ValueError,OverflowError):published=''
    duration=''
    seconds=entry.get('duration')
    if isinstance(seconds,(int,float)) and not isinstance(seconds,bool) and math.isfinite(seconds) and seconds>=0:
        value=int(seconds);duration=f'{value//3600}:{value//60%60:02}:{value%60:02}' if value>=3600 else f'{value//60}:{value%60:02}'
    body=dict(id=identity,channel=channel,title=title.strip()[:500],date=day,time=clock,state='unknown',duration=duration,
              description='',metadata_pending=True,browse_only=True,forecast_eligible=False,kind='unknown',views=None)
    return body,published


def tick(importer, stop=None):
    store=importer.store
    if stop is not None and stop.is_set() or not store.get('setup_complete') or store.reset_pending() or store.get('youtube_sync_paused')=='true':return False
    now=time.time();token=secrets.token_hex(16)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM channel_upload_pages WHERE channel NOT IN (SELECT id FROM channels WHERE active=1)')
        row=db.execute('''SELECT p.* FROM channel_upload_pages p JOIN channels c ON c.id=p.channel WHERE c.active=1
            AND p.state IN ('queued','running','retry') AND p.expires>? AND p.retry_at<=? AND p.lease_until<=?
            ORDER BY p.requested DESC,p.channel,p.offset LIMIT 1''',(now,now,now)).fetchone()
        if not row or _paused(db):return False
        db.execute("UPDATE channel_upload_pages SET state='running',claim=?,lease_until=? WHERE channel=? AND offset=?",(token,now+LEASE,row['channel'],row['offset']))
    channel,offset=row['channel'],row['offset']
    try:
        from .youtube_fallback import extract,queue_details
        data=extract(importer,'https://www.youtube.com/playlist?list=UU'+channel[2:],'playlist',offset+1,offset+PAGE+1,public=True,timeout=30,foreground=True)
        if data.get('channel_id') and data['channel_id']!=channel:raise ValueError('Unexpected public channel.')
        entries=data.get('entries')
        if not isinstance(entries,list) or len(entries)>PAGE+1:raise ValueError('Invalid public upload page.')
        raw=entries[:PAGE];ids=[];basics=[]
        for entry in raw:
            if isinstance(entry,dict) and entry.get('channel_id') and entry['channel_id']!=channel:raise ValueError('Unexpected public channel.')
            value=_basic(entry,channel) if isinstance(entry,dict) else None
            if value and value[0]['id'] not in ids:
                ids.append(value[0]['id']);basics.append(value)
        fingerprint=hashlib.sha256('\0'.join(sorted(ids)).encode()).hexdigest() if ids else ''
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if stop is not None and stop.is_set() or _paused(db) or not db.execute('''SELECT 1 FROM channel_upload_pages p JOIN channels c ON c.id=p.channel
                WHERE p.channel=? AND p.offset=? AND p.claim=? AND c.active=1''',(channel,offset,token)).fetchone():return True
            if fingerprint and db.execute("SELECT 1 FROM channel_upload_pages WHERE channel=? AND offset!=? AND state='complete' AND fingerprint=?",(channel,offset,fingerprint)).fetchone():raise ValueError('Repeated public upload page.')
            for body,published in basics:
                # Existing rich/normal observations always win over flat rows.
                body['thumbnail']='/artwork/'+hashlib.sha256(('https://i.ytimg.com/vi/'+body['id']+'/hqdefault.jpg').encode()).hexdigest()
                db.execute('INSERT OR IGNORE INTO artwork(id,url) VALUES(?,?)',(body['thumbnail'].rsplit('/',1)[-1],'https://i.ytimg.com/vi/'+body['id']+'/hqdefault.jpg'))
                db.execute('INSERT OR IGNORE INTO videos VALUES(?,?,?,?,?,?)',(body['id'],channel,body['date'],published,json.dumps(body),time.time()))
                if body['duration']:
                    # A flat listing knows duration even when RSS does not.
                    # Fill that one missing fact without changing provenance,
                    # known publication dates or richer metadata.
                    db.execute("UPDATE videos SET data=json_set(data,'$.duration',?) WHERE id=? AND channel=? AND json_extract(data,'$.metadata_pending')=1 AND COALESCE(json_extract(data,'$.duration'),'')=''",(body['duration'],body['id'],channel))
            queue_details(store,[dict(id=identity,snippet={'channelId':channel}) for identity in ids],transaction=db)
            db.execute("UPDATE channel_upload_pages SET state='complete',ids=?,has_more=?,fingerprint=?,retry_at=0,claim='',lease_until=0 WHERE channel=? AND offset=? AND claim=?",(json.dumps(ids),int(len(entries)>PAGE),fingerprint,channel,offset,token))
        return True
    except Exception:
        with store.connect() as db:
            db.execute("UPDATE channel_upload_pages SET state='retry',retry_at=?,claim='',lease_until=0 WHERE channel=? AND offset=? AND claim=?",(time.time()+RETRY,channel,offset,token))
        return True
    finally:
        with store.connect() as db:
            db.execute("UPDATE channel_upload_pages SET state=CASE WHEN expires>? THEN 'queued' ELSE 'idle' END,claim='',lease_until=0 WHERE channel=? AND offset=? AND claim=?",(time.time(),channel,offset,token))


def recover(store):
    with store.connect() as db:
        db.execute('DELETE FROM channel_upload_pages WHERE channel NOT IN (SELECT id FROM channels WHERE active=1)')
        db.execute("UPDATE channel_upload_pages SET state=CASE WHEN expires>? THEN 'queued' ELSE 'idle' END,claim='',lease_until=0 WHERE state='running'",(time.time(),))


def loop(app,store,stop):
    from .catalogue import Importer
    importer=Importer(app)
    while not stop.is_set():
        with store.background_operation():
            try:tick(importer,stop)
            except Exception:app.logger.warning('Viewed channel uploads will retry.')
        stop.wait(1)
