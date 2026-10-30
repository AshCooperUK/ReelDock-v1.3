"""One read-only view of concurrent worker jobs and actionable alerts."""
import json
import hashlib
import re
import sqlite3
import time

_POLICY_SKIP = re.compile(
    r'(?:Not downloaded|Not saved|Removed from the queue): (?:'
    r'automatic downloads were turned off for this channel|'
    r'auto download settings no longer include this video|'
    r'(?:normal videos|Shorts|live streams) are turned off for this channel|'
    r"published before this channel's download range \(last [1-9][0-9]{0,3} days?\)|"
    r'shorter than the [1-9][0-9]{0,3}-minute minimum length)\.'
)


def _expected_automatic_skip(row):
    """Keep expected policy outcomes in history without treating them as errors.

    Unknown metadata/access failures can also be stored as skipped. Suppress
    only a known policy reason on an explicitly automatic skipped row.
    """
    return row['state']=='skipped' and row['automatic']==1 and bool(_POLICY_SKIP.fullmatch(str(row['message'] or '').strip()))


def alert_id(identity, message):
    return identity if identity.startswith(('download-','server-download-')) else identity+'-'+hashlib.sha256(message.encode()).hexdigest()[:12]


def _preview(value):
    """Keep the hover card to title, channel, description and a local picture."""
    if not isinstance(value, dict):
        return None
    image = str(value.get('image') or '')
    if not re.fullmatch(r'/artwork/[a-f0-9]{64}', image):
        image = ''
    card = {
        'title': str(value.get('title') or '')[:180],
        'channel': str(value.get('channel') or '')[:120],
        'overview': str(value.get('overview') or '')[:500],
        'image': image,
    }
    return card if any(card.values()) else None


def record(store, identity, title, state, message, progress=None, preview=None):
    if identity in ('youtube-artwork','youtube-metadata') and store.get('youtube_share_metadata','true') != 'true':
        state, message, progress = 'cancelled', 'YouTube metadata disabled in Settings.', None
    if identity in ('m3u-exports','m3u-catalogue') and store.get('m3u_share_metadata','true') != 'true':
        state, message, progress = 'cancelled', 'Metadata disabled in Settings.', None
    if state=='complete' and identity in ('m3u-exports','m3u-catalogue'):
        from .event_log import metadata_batch
        match=re.fullmatch(r'Processed (\d+) titles for local metadata sharing\.',message)
        metadata_batch(store,identity,int(match[1]) if match else 1)
    with store.connect() as db:
        db.execute('INSERT INTO activity_tasks VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,state=excluded.state,message=excluded.message,progress=excluded.progress,updated=excluded.updated',
                   (identity,title,state,message,progress,time.time()))
        card = _preview(preview)
        if card:
            db.execute('INSERT INTO activity_previews(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data', (identity, json.dumps(card)))
        if state != 'running':
            db.execute("DELETE FROM activity_tasks WHERE state!='running' AND updated<?",(time.time()-86400,))
            db.execute("DELETE FROM activity_tasks WHERE state='running' AND updated<?",(time.time()-86400,))
            db.execute('DELETE FROM activity_previews WHERE id NOT IN (SELECT id FROM activity_tasks)')


def snapshot(store):
    now=time.time();tasks=[];recent=[];alerts=[]
    def task(identity,title,state,message,updated=0,progress=None,preview=None,waiting=0,engine='',engine_id=''):
        if state not in ('running','queued'):
            from .activity_clear import snapshot_visible
            if not snapshot_visible(store,identity):return
        item=dict(id=identity,title=title,state=state,message=message,progress=progress,updated=updated)
        if engine in ('youtube','vod','nzb','torrent') and engine_id:
            item.update(engine=engine,engine_id=str(engine_id))
        if preview:item['preview']=preview
        # Queued entries that are really held discover requests report how many
        # titles they represent so tile/widget/page waiting counts can agree.
        if waiting:item['waiting']=waiting
        if state in ('running','queued'):tasks.append(item)
        elif state=='complete' and updated>now-300:recent.append(item)
        elif state in ('error','failed'):alert(identity,title,message)
    def alert(identity,title,message,retry=None):
        if not message:return
        from .activity_clear import snapshot_visible
        if not snapshot_visible(store,identity):return
        # A download dismissal survives retries and changed provider messages.
        key=alert_id(identity, message)
        alerts.append(dict(id=key,title=title,message=message,**({"retry":retry} if retry else {})))
    with store.connect() as db:
        settings={r['key']:r['value'] for r in db.execute('SELECT key,value FROM settings')}
        for r in db.execute("SELECT * FROM youtube_subscription_returns WHERE state='pending'"):
            channel=json.loads(r['data'])
            alerts.append(dict(id='subscription-return-'+r['channel']+'-'+hashlib.sha256(r['subscription'].encode()).hexdigest()[:12],
                title='Restore YouTube channel?',message='You are subscribed to this channel on YouTube, but it was removed from ReelDock. Add it back?',
                channel=channel,subscription=r['subscription']))
        for r in db.execute('SELECT s.id,s.name,s.state,s.message,coalesce((SELECT max(finished) FROM m3u_runs WHERE source=s.id),s.last_run) AS last_run FROM m3u_sources s'):
            task('m3u-'+r['id'],r['name']+' · Playlist',r['state'],r['message'],r['last_run'])
        for table,label in [('m3u_metadata_runs','Metadata'),('m3u_dry_runs','Dry run')]:
            for r in db.execute(f'SELECT r.*,s.name FROM {table} r JOIN m3u_sources s ON s.id=r.source'):
                task(table+'-'+r['source'],r['name']+' · '+label,r['state'],r['message'],r['updated'])
        for r in db.execute("SELECT * FROM m3u_downloads WHERE state IN ('queued','running','cancelling') OR updated>?",(now-86400,)):
            task('server-download-'+r['id'],'Server download · '+r['title'],'running' if r['state']=='cancelling' else r['state'],r['message'],r['updated'],r['progress'])
        metadata_pending=db.execute('SELECT count(*) FROM m3u_catalog_titles WHERE checked<?',(now-7*86400,)).fetchone()[0]
        rows=list(db.execute('SELECT * FROM activity_tasks WHERE updated>?',(now-86400,)))
        saved={}
        if rows:
            marks=','.join('?'*len(rows))
            for item in db.execute(f'SELECT id,data FROM activity_previews WHERE id IN ({marks})',[r['id'] for r in rows]):
                try: parsed=json.loads(item['data'])
                except json.JSONDecodeError: continue
                card=_preview(parsed)
                if card: saved[item['id']]=card
        for r in rows:
            if r['id'] in ('youtube-artwork','youtube-metadata') and settings.get('youtube_share_metadata','true') != 'true':
                continue
            if r['id'] in ('m3u-catalogue','m3u-exports') and settings.get('m3u_share_metadata','true') != 'true':
                continue
            if r['state']=='running' and r['updated']<now-120:continue
            if r['id']=='m3u-catalogue' and r['state']=='complete' and metadata_pending and settings.get('m3u_tmdb_token'):
                task(r['id'],r['title'],'queued',f'{metadata_pending:,} titles awaiting metadata and artwork checks.')
            else:
                task(r['id'],r['title'],r['state'],r['message'],r['updated'],r['progress'],saved.get(r['id']))
        channel_requests=db.execute("SELECT count(*) FROM youtube_channel_requests WHERE state='queued'").fetchone()[0]
        if channel_requests:task('youtube-channels','Add YouTube channels','queued',f'{channel_requests} channels waiting to be checked.')
        from .youtube_source import description, fallback
        source=description(store)
        paused=settings.get('youtube_sync_paused')=='true'
        def waiting(rows,label):
            ready=sum(r['retry_at']<=now for r in rows)
            delayed=len(rows)-ready
            text=f'{len(rows):,} {label}: {ready:,} ready, {delayed:,} waiting to retry.'
            if delayed:
                from datetime import datetime
                from zoneinfo import ZoneInfo
                retry=min(r['retry_at'] for r in rows if r['retry_at']>now)
                text+=' Next retry: '+datetime.fromtimestamp(retry,ZoneInfo('Europe/London')).strftime('%d.%m.%Y %H:%M')+'.'
            return text+(' YouTube checks are paused in Settings.' if paused else ' '+source)
        week_rows=db.execute("SELECT j.retry_at,j.error FROM guide_week_jobs j JOIN channels c ON c.id=j.channel WHERE j.state='queued' AND c.active=1 AND NOT EXISTS(SELECT 1 FROM channel_preferences p WHERE p.id=c.id AND p.shown=0)").fetchall()
        if week_rows:
            message=waiting(week_rows,'channel/week checks')
            if settings.get('sync_running')=='1':message+=' Waiting for the current recent-upload scan to finish.'
            elif settings.get('guide_running')!='1':message+=' Checked in small batches between recent-upload scans.'
            reason=next((r['error'] for r in week_rows if r['error']),'')
            if reason:message+=' Last issue: '+reason
            task('guide-weeks','Selected guide weeks','running' if settings.get('guide_running')=='1' and not paused else 'queued',message)
        push_rows=db.execute('SELECT e.retry_at,e.attempts FROM youtube_push_events e JOIN channels c ON c.id=e.channel WHERE e.done=0 AND c.active=1').fetchall()
        if push_rows:
            running=float(settings.get('youtube_push_running') or 0)>now-600 and not paused
            message=waiting(push_rows,'uploads awaiting details')
            if fallback(store):message+=' Up to 3 videos per batch; public lookups share a rate-limited queue with guide and history checks.'
            if any(r['attempts'] for r in push_rows):message+=' '+(settings.get('youtube_push_status') or 'Unavailable details retry automatically.')
            task('youtube-push','YouTube notifications','running' if running else 'queued',message)
        refresh=db.execute('SELECT count(*) FROM emby_refresh').fetchone()[0]
        if refresh:
            text=settings.get('emby_refresh_status') or 'Waiting to refresh the Emby library.'
            task('emby-refresh','Emby library refresh','queued',text)
            if text.startswith('Library scan failed:'):alert('emby-refresh','Emby refresh',text)
        for r in db.execute("SELECT id,title,message,state,automatic FROM downloads WHERE state IN ('failed','skipped') AND updated>? ORDER BY updated DESC",(now-86400,)):
            if _expected_automatic_skip(r):continue
            alert('download-'+r['id'],'Download failed · '+r['title'],r['message'] or 'The download could not finish.',retry={'id':r['id']})
        try:
            discover_rows=db.execute("SELECT id,title,state,message,progress,updated,engine,engine_id,kind,body,manual,episode_key,stop_generation FROM discover_requests WHERE state IN ('searching','starting','queued','downloading','verifying','waiting','paused','cancelling') OR (state='failed' AND updated>?)",(now-86400,))
        except sqlite3.OperationalError:
            discover_rows=[]
        waiting=[]
        for r in discover_rows:
            if r['kind']=='video' and r['state'] in ('queued','downloading','failed'):
                try: video_id=json.loads(r['body'] or '{}').get('videoId')
                except (ValueError, TypeError): video_id=None
                if video_id and db.execute("SELECT 1 FROM downloads WHERE id=? AND automatic=0 AND state IN ('queued','downloading','cancelling','failed','skipped','complete')",(video_id,)).fetchone():
                    continue
            # Dedicated NZB/torrent tiles own progress after the engine handoff.
            # Search/retry/cancellation and failure alerts still need Activity.
            if r['engine'] in ('nzb', 'torrent') and r['engine_id'] and r['state'] in ('queued','downloading','verifying','paused'):
                continue
            if r['state']=='failed':
                from .source_choice import eligibility
                if eligibility(db,r):continue  # The source-choice dialog owns this decision.
                alert('discover-'+r['id'],'Discover · '+r['title'],r['message'] or 'The request failed.')
            elif r['state']=='waiting':
                waiting.append(r)
            else:
                mapped='running' if r['state'] in ('searching','downloading','verifying') else 'queued'
                # A linked transport owns its download count. Searching/starting
                # can refer to a previous failed source, so keep those attempts
                # independent instead of attaching the old transport identity.
                linked=r['state'] in ('queued','downloading','verifying','paused','cancelling')
                task('discover-'+r['id'],'Discover · '+r['title'],mapped,r['message'] or 'Working on this request.',r['updated'],r['progress'],
                     engine=r['engine'] if linked else '',engine_id=r['engine_id'] if linked else '')
        # Release-date and retry backlogs are one queued summary, not a row each.
        # A lone request keeps its own entry so it remains cancellable here.
        if len(waiting)==1:
            r=waiting[0]
            task('discover-'+r['id'],'Discover · '+r['title'],'queued',r['message'] or 'Working on this request.',r['updated'],r['progress'],waiting=1)
        elif waiting:
            task('discover-waiting','Discover requests','queued',f'{len(waiting):,} titles waiting for a release or indexer retry.',waiting=len(waiting))
    if settings.get('sync_running')=='1' or settings.get('sync_requested')=='1':
        task('youtube-sync','YouTube guide update','running' if settings.get('sync_running')=='1' else 'queued',settings.get('sync_message') or 'Checking subscriptions and uploads.')
    if settings.get('history_running')=='1':
        message=settings.get('history_message') or 'Loading older channel uploads.'
        if not message.startswith('No history checks'):
            task('youtube-history','YouTube channel history','running',message)
    else:
        with store.connect() as db:
            history=db.execute("SELECT count(*),min(h.updated+300) FROM history_jobs h JOIN channels c ON c.id=h.channel WHERE c.active=1 AND h.state='indexing' AND h.failures>0 AND h.updated+300>?",(now,)).fetchone()
        if history[0]:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            retry=datetime.fromtimestamp(history[1],ZoneInfo('Europe/London')).strftime('%d.%m.%Y %H:%M')
            task('youtube-history','YouTube channel history','queued',f'{history[0]} channels waiting after unsuccessful requests. Next retry: {retry}. '+source)
    for key,title in [('sync_error','YouTube updates'),('artwork_error','YouTube artwork'),('emby_watched_paused','YouTube watched status')]:
        message=settings.get(key,'')
        if key=='sync_error' and any(x in message for x in ('quotaExceeded','dailyLimitExceeded')):
            from .youtube_source import description
            message=description(store)
        alert(key,title,message)
    alert('youtube-fallback','YouTube details',settings.get('youtube_fallback_error',''))
    from .system_stats import worker_online
    heartbeat=settings.get('worker_heartbeat')
    try:beat=float(heartbeat) if heartbeat else 0
    except (TypeError,ValueError):beat=0
    if beat and not worker_online(heartbeat,now):alert('worker-offline','Worker offline','Automatic updates and downloads are waiting for the ReelDock worker to reconnect.')
    tasks.sort(key=lambda x:(x['state']!='running',x['title'].casefold()))
    recent.sort(key=lambda x:x['updated'],reverse=True)
    with store.connect() as db:dismissed={r[0] for r in db.execute('SELECT id FROM dismissed_alerts')}
    visible=[a for a in alerts if a['id'] not in dismissed]
    groups={};combined=[]
    for item in visible:
        source='YouTube' if item['id'].startswith('download-') else 'M3U' if item['id'].startswith('server-download-') else ''
        if not source:combined.append(item);continue
        groups.setdefault((source,item['message']),[]).append(item)
    for (source,message),members in groups.items():
        if len(members)==1:combined.extend(members);continue
        ids=sorted(a['id'] for a in members)
        identity='download-group-'+hashlib.sha256('\n'.join(ids).encode()).hexdigest()
        combined.append(dict(id=identity,title=f'{len(members):,} {source} downloads need attention',
            message=message+' Individual details are available in Activity.',members=ids))
    return dict(tasks=tasks,recent=recent[:5],alerts=combined)
