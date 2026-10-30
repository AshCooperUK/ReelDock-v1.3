"""Queue a bounded manual refresh through the existing background workers."""
import json
import time


def _status(db, settings, now, work_only=False):
    receipt=json.loads(settings.get('sync_all_receipt') or '{}')
    if not receipt:return dict(busy=False,message='')
    if receipt.get('complete'):return dict(busy=False,message='Sync All finished. Check Activity for any errors.')
    pending=[]
    if receipt.get('youtube') and settings.get('youtube_sync_paused')!='true' and (settings.get('sync_requested')=='1' or settings.get('sync_running')=='1' or float(settings.get('recent_scan_started') or 0)>0):pending.append('YouTube')
    for source in receipt.get('m3u',[]):
        row=db.execute('SELECT state FROM m3u_sources WHERE id=?',(source,)).fetchone()
        if row and row[0] in ('queued','running'):pending.append('M3U');break
    for table,key,states in [('downloads','downloads',('queued','downloading')),('m3u_downloads','server_downloads',('queued','running'))]:
        if table=='downloads' and settings.get('download_pause_downloads')=='true':continue
        for identity in receipt.get(key,[]):
            row=db.execute('SELECT state FROM '+table+' WHERE id=?',(identity,)).fetchone()
            if row and row[0] in states:pending.append('Downloads');break
    if receipt.get('emby'):
        if db.execute("SELECT 1 FROM emby_refresh WHERE channel='sync-all'").fetchone() and not settings.get('emby_refresh_status','').startswith('Library scan failed:'):pending.append('Emby')
    busy=bool(pending) or now-float(receipt['requested'])<60 or (not work_only and not receipt.get('finishing'))
    message=('Waiting for '+', '.join(dict.fromkeys(pending))+'.') if pending else 'Refresh requests sent. Check Activity for results.'
    if receipt.get('paused'):message+=' Paused: '+', '.join(receipt['paused'])+'.'
    return dict(busy=busy,message=message)


def status(store):
    with store.connect() as db:
        settings=dict(db.execute('SELECT key,value FROM settings'))
        return _status(db,settings,time.time())


def queue(store):
    from .media_holds import blocked_in,vod_key
    now=time.time()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        settings=dict(db.execute('SELECT key,value FROM settings'))
        current=_status(db,settings,now)
        if current['busy']:return current
        changes={}
        receipt=dict(requested=now,youtube=False,m3u=[],downloads=[],server_downloads=[],emby=False,paused=[])
        if settings.get('youtube_sync_paused')!='true':
            receipt['youtube']=True
            if settings.get('sync_running')!='1':changes['sync_requested']='1'
            changes.update(next_history='0',next_auto_check='0',last_manual_sync=str(now))
            db.execute("UPDATE guide_week_jobs SET retry_at=0 WHERE state='queued'")
            db.execute('UPDATE youtube_push_events SET retry_at=0 WHERE done=0')
        else:receipt['paused'].append('YouTube checks')
        sources=db.execute("SELECT id,options,state FROM m3u_sources WHERE id NOT IN (SELECT source FROM m3u_removals)").fetchall()
        for source in sources:
            if not json.loads(source['options']).get('enabled',True):continue
            receipt['m3u'].append(source['id'])
            if source['state'] not in ('running','queued'):
                db.execute("UPDATE m3u_sources SET state='queued',message='Sync All requested. Waiting for the background worker.',next_run=0 WHERE id=?",(source['id'],))
        if settings.get('download_pause_downloads')!='true':
            receipt['downloads']=[r[0] for r in db.execute("SELECT id FROM downloads WHERE state IN ('failed','queued','downloading')")
                                  if not blocked_in(db,['video:'+r[0],'managed:video:'+r[0]])]
            for identity in receipt['downloads']:
                db.execute("UPDATE downloads SET state='queued',progress=0,phase='',message='Retry requested by Sync All.',updated=? WHERE id=? AND state='failed'",(now,identity))
        else:receipt['paused'].append('YouTube downloads')
        receipt['server_downloads']=[r[0] for r in db.execute("SELECT d.id,d.source,d.path FROM m3u_downloads d JOIN m3u_sources s ON s.id=d.source WHERE d.state IN ('failed','queued','running') AND s.id NOT IN (SELECT source FROM m3u_removals) AND coalesce(json_extract(s.options,'$.enabled'),1)=1")
                                    if not blocked_in(db,vod_key(r['source'],r['path']))]
        for identity in receipt['server_downloads']:
            db.execute("UPDATE m3u_downloads SET state='queued',progress=0,message='Retry requested by Sync All.',updated=? WHERE id=? AND state='failed'",(now,identity))
        if settings.get('emby_url') and settings.get('emby_key'):
            receipt['emby']=True
            db.execute("INSERT OR REPLACE INTO emby_refresh VALUES ('sync-all',?)",(now,))
            changes.update(emby_refresh_after='0',emby_refresh_status='Library scan queued by Sync All.',emby_discover_after='0',emby_favourites_after='0',emby_next_check='0')
        changes['sync_all_receipt']=json.dumps(receipt)
        db.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)',changes.items())
        message='YouTube checks, enabled playlists and failed download retries requested.'
        if receipt['emby']:message+=' Emby library refresh requested.'
        if receipt['paused']:message+=' Paused: '+', '.join(receipt['paused'])+'.'
        db.execute("INSERT OR REPLACE INTO activity_tasks VALUES ('sync-all','Sync All','complete',?,NULL,?)",(message,now))
    return status(store)


def tick(store):
    """Scan Emby again after the requested imports/downloads finish."""
    now=time.time()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        settings=dict(db.execute('SELECT key,value FROM settings'))
        receipt=json.loads(settings.get('sync_all_receipt') or '{}')
        if not receipt or receipt.get('complete'):return
        if not receipt.get('finishing'):
            for table,key in [('downloads','downloads'),('m3u_downloads','server_downloads')]:
                new=[r[0] for r in db.execute('SELECT id FROM '+table+" WHERE created>=? AND state IN ('queued','downloading','running')",(receipt['requested'],))]
                receipt[key]=list(set(receipt[key]+new))
            core=dict(receipt,emby=False)
            if _status(db,{**settings,'sync_all_receipt':json.dumps(core)},now,work_only=True)['busy']:
                db.execute("INSERT OR REPLACE INTO settings VALUES ('sync_all_receipt',?)",(json.dumps(receipt),));return
            receipt['finishing']=True
            if receipt.get('emby') and settings.get('emby_url') and settings.get('emby_key'):
                db.execute("INSERT OR REPLACE INTO emby_refresh VALUES ('sync-all',?)",(now,))
                db.execute("INSERT OR REPLACE INTO settings VALUES ('emby_refresh_status','Final Sync All library scan queued.')")
        elif not _status(db,settings,now)['busy']:
            receipt['complete']=True
            db.execute("INSERT OR REPLACE INTO activity_tasks VALUES ('sync-all','Sync All','complete','Requested jobs have finished. Check individual activities for any errors.',NULL,?)",(now,))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('sync_all_receipt',?)",(json.dumps(receipt),))
