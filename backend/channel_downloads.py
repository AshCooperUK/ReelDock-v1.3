"""Per-channel automation and queued local library actions."""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from . import media_library as library

RANGES=(0,1,2,7,14,30,90,180,365,730)
ACTIONS=('refresh_emby','download_pending','redownload_existing','deep_scan','refresh_metadata','sync_files',
         'delete_media','remove_channel','remove_all')


def initialise(store):
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS channel_favourites(id TEXT PRIMARY KEY)')
        db.execute('''CREATE TABLE IF NOT EXISTS channel_tasks(channel TEXT PRIMARY KEY, action TEXT,
            state TEXT, message TEXT DEFAULT '', created REAL)''')
        columns={r[1] for r in db.execute('PRAGMA table_info(download_rules)')}
        for name in ('sponsorblock', 'force_transcode'):
            if name not in columns:
                db.execute(f"ALTER TABLE download_rules ADD COLUMN {name} TEXT NOT NULL DEFAULT 'default'")
        for name,default in (('minimum_video_minutes',-1),('subscription_paused',0)):
            if name not in columns:
                db.execute(f'ALTER TABLE download_rules ADD COLUMN {name} INTEGER NOT NULL DEFAULT {default}')
        if 'range_override' not in columns:
            # Legacy ranges have no provenance; preserve them as explicit choices.
            db.execute('ALTER TABLE download_rules ADD COLUMN range_override INTEGER NOT NULL DEFAULT 1')
        if 'quality_override' not in columns:
            # Existing choices are preserved; older versions did not record
            # whether saving another preference also copied the default.
            db.execute('ALTER TABLE download_rules ADD COLUMN quality_override INTEGER DEFAULT 1')
        if 'keep_files' not in columns:
            db.execute('ALTER TABLE download_rules ADD COLUMN keep_files INTEGER NOT NULL DEFAULT 0')
            # Existing subscriptions adopt retention, as requested for this release.
            db.execute("INSERT OR IGNORE INTO download_rules(channel,quality,quality_override,range_override) SELECT id,?,0,0 FROM channels WHERE active=1",(store.get('download_quality','1080'),))



def resolve_defaults(store, rule):
    result=dict(rule)
    if not result.get('range_override', 1):
        from .settings.youtube import DOWNLOAD_FIELDS
        result['range_days']=DOWNLOAD_FIELDS['default_range'].read(store)
    from .settings.youtube import DOWNLOAD_FIELDS
    result['minimum_override']=int(result.get('minimum_video_minutes',-1)!=-1)
    if not result['minimum_override']:
        result['minimum_video_minutes']=DOWNLOAD_FIELDS['minimum_video_minutes'].read(store)
    result.setdefault('subscription_paused',0)
    return result


def preferences(store, channel):
    with store.connect() as db:
        row=db.execute('SELECT * FROM download_rules WHERE channel=?',(channel,)).fetchone()
    result=dict(row) if row else dict(channel=channel,quality_override=0,videos=0,shorts=0,live=0,range_days=180,range_override=0,keep_files=0,sponsorblock='default',force_transcode='default')
    if not result['quality_override']:result['quality']=store.get('download_quality','1080')
    return resolve_defaults(store, result)


def save(store, channel, settings):
    from .downloads import QUALITIES
    current=preferences(store,channel)
    if not isinstance(settings,dict) or not settings or set(settings)-{'quality','videos','shorts','live','range_days','keep_files','sponsorblock','force_transcode','minimum_video_minutes','subscription_paused'}:
        raise ValueError('Choose a download preference.')
    for key,value in settings.items():
        if key in ('videos','shorts','live','keep_files','subscription_paused') and type(value) is not bool:raise ValueError('Choose on or off.')
        if key=='sponsorblock' and value not in ('default','off','mark','remove'):raise ValueError('Choose a SponsorBlock mode.')
        if key=='force_transcode' and value not in ('default','on','off'):raise ValueError('Choose a transcoding mode.')
        if key=='quality' and value not in (*QUALITIES,'default'):raise ValueError('Choose a supported quality.')
        if key=='range_days' and value!='default' and (type(value) is not int or value not in RANGES):raise ValueError('Choose a supported range.')
        if key=='minimum_video_minutes' and value!='default' and (type(value) is not int or not 0<=value<=120):raise ValueError('Choose a minimum length from 0 to 120 minutes.')
    current.update(settings)
    if 'minimum_video_minutes' in settings:
        current['minimum_override']=int(settings['minimum_video_minutes']!='default')
        if not current['minimum_override']:
            from .settings.youtube import DOWNLOAD_FIELDS
            current['minimum_video_minutes']=DOWNLOAD_FIELDS['minimum_video_minutes'].read(store)
    if 'quality' in settings:
        current['quality_override']=int(settings['quality']!='default')
        if not current['quality_override']:current['quality']=store.get('download_quality','1080')
    if 'range_days' in settings:
        current['range_override']=int(settings['range_days']!='default')
        if not current['range_override']:
            from .settings.youtube import DOWNLOAD_FIELDS
            current['range_days']=DOWNLOAD_FIELDS['default_range'].read(store)
    with store.connect() as db:
        # Serialise with the worker claiming the next queued job (downloads.tick).
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT OR REPLACE INTO download_rules(channel,quality,videos,shorts,live,range_days,enabled_at,quality_override,keep_files,sponsorblock,force_transcode,range_override,minimum_video_minutes,subscription_paused) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (channel,current['quality'],current['videos'],current['shorts'],current['live'],current['range_days'],time.time(),current['quality_override'],current['keep_files'],current['sponsorblock'],current['force_transcode'],current['range_override'],current['minimum_video_minutes'] if current['minimum_override'] else -1,current['subscription_paused']))
        if current['range_days']==0 or current['range_days']>365:
            db.execute("UPDATE history_jobs SET state='indexing',updated=0 WHERE channel=? AND cursor!=''",(channel,))
        db.execute("DELETE FROM downloads WHERE state='skipped' AND id IN (SELECT id FROM videos WHERE channel=?)",(channel,))
        removed,stopped=drop_unmatched(db,channel,current)
        name=db.execute('SELECT data FROM channels WHERE id=?',(channel,)).fetchone()
    if removed or stopped:
        from .activity import record
        title=(json.loads(name[0]).get('name') if name else '') or 'Channel'
        parts=[]
        if removed:
            parts.append(f"Removed {removed} queued download{'s' if removed!=1 else ''} that no longer match the new settings. "
                         'Each one is listed in download history with the reason.')
        if stopped:
            parts.append(f"Stopping {stopped} running download{'s' if stopped!=1 else ''} that the new settings exclude.")
        record(store,'download-rules-'+channel,title+' · Download settings','complete',' '.join(parts))
    store.set('next_retention_check',0)
    store.set('next_auto_check',0)
    from .watchlist import sync_auto_channels
    sync_auto_channels(store)
    return current


TYPE_LABELS={'videos':'normal videos','shorts':'Shorts','live':'live streams'}


def rules_apply(rule):
    """Channel selection settings govern automatic downloads."""
    return True


def manual_discover_rule(rule):
    return dict(rule, range_days=0, videos=0, shorts=0, live=0, minimum_video_minutes=0)


def request_rule(store, video, rule, automatic=False):
    """An explicit Discover selection bypasses automatic selection filters."""
    if not automatic:
        from .discover_store import quality_policy
        if quality_policy(store, video.get('id', '')) is not None:
            return manual_discover_rule(rule)
    return rule


def rule_conflict(video, rule):
    """Why this channel's date range or type settings exclude the video, or ''.

    An unknown type passes here: the worker probes it before any transfer.
    """
    days=rule.get('range_days')
    if days and (video.get('date') or '')<range_cutoff(days):
        return f"published before this channel's download range (last {days} day{'s' if days!=1 else ''})"
    kind=video.get('kind','unknown')
    # With every type off the channel simply has no automatic downloads; that
    # is not a type preference, so manual requests of any type are allowed.
    if kind in TYPE_LABELS and any(rule.get(k) for k in TYPE_LABELS) and not rule.get(kind):
        return f'{TYPE_LABELS[kind]} are turned off for this channel'
    return ''


def length_conflict(kind, duration, minimum):
    """Why a normal video fails the minimum length, or ''. Shorts and live streams are exempt."""
    if kind!='videos' or not minimum:return ''
    if not duration or duration<=0:return f'its length could not be checked against the {minimum}-minute minimum'
    if duration<minimum*60:return f'shorter than the {minimum}-minute minimum length'
    return ''


def exclusion(video, rule, automatic):
    """Why the channel settings exclude this download, or ''."""
    if not automatic:return ''  # Explicit manual selections do not change or inherit automation filters.
    if automatic and not any(rule.get(k) for k in ('videos','shorts','live')):return 'automatic downloads were turned off for this channel'
    return rule_conflict(video,rule)


def refusal(store, video):
    """Explicit published-video requests override automatic selection filters."""
    # Authentication, published state, holds and confined media paths are
    # enforced by their existing callers; channel filters select automation.
    return ''


def mismatch(video, rule, automatic=True):
    """Why a queued download no longer matches the channel rule, or ''.

    Only settings-related reasons: an unknown type stays queued while any type
    is enabled, because the worker probes the type before transferring media.
    """
    reason=exclusion(video,rule,automatic)
    return f'Removed from the queue: {reason}.' if reason else ''


def drop_unmatched(db, channel, rule):
    """Apply new channel settings to this channel's queued and running downloads.

    Settings apply to automatic downloads. Queued automatic downloads the
    rule excludes are skipped; a queued replacement is withdrawn and the saved
    file kept; a running download is stopped through the normal cancellation
    path (the worker sees 'cancelling' and stops before saving anything).
    Returns (queued removed, running stopped).
    """
    rows=db.execute("""SELECT d.id,d.state,d.automatic,COALESCE(d.replace_existing,0) AS replace_existing,v.data
        FROM downloads d JOIN videos v ON v.id=d.id
        WHERE v.channel=? AND d.state IN ('queued','downloading')""",(channel,)).fetchall()
    removed=stopped=0;now=time.time()
    has_discover=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='discover_quality_policy'").fetchone()
    discover_ids={r[0] for r in db.execute('SELECT id FROM discover_quality_policy')} if has_discover else set()
    for row in rows:
        try:video=json.loads(row['data'])
        except (TypeError,ValueError):continue
        effective=manual_discover_rule(rule) if not row['automatic'] and row['id'] in discover_ids else rule
        reason=exclusion(video,effective,bool(row['automatic']))
        if not reason:continue
        if row['state']=='downloading':
            stopped+=db.execute("UPDATE downloads SET state='cancelling',message=?,updated=? WHERE id=? AND state='downloading'",
                                (f'Cancelled: {reason}.',now,row['id'])).rowcount
        elif row['replace_existing']:
            # Replacements are only queued from completed downloads: restore that state.
            removed+=db.execute("UPDATE downloads SET state='complete',progress=100,replace_existing=0,message=?,updated=? WHERE id=? AND state='queued'",
                                (f'Replacement cancelled: {reason}. The saved file was kept.',now,row['id'])).rowcount
        else:
            removed+=db.execute("UPDATE downloads SET state='skipped',progress=0,message=?,updated=? WHERE id=? AND state='queued'",
                                (f'Removed from the queue: {reason}.',now,row['id'])).rowcount
    return removed,stopped


def range_cutoff(days):
    from zoneinfo import ZoneInfo
    today=datetime.now(ZoneInfo('Europe/London')).date()
    return (today-timedelta(days=max(0,days-1))).isoformat() if days else '0000-01-01'


def eligible(video, rule):
    if video.get('browse_only') or video.get('metadata_pending') or video.get('state')!='published':return False
    if rule['range_days']:
        cutoff=range_cutoff(rule['range_days'])
        if video.get('date','')<cutoff:return False
    kind=video.get('kind','unknown')
    return bool(rule.get(kind)) if kind in ('videos','shorts','live') else any(rule[k] for k in ('videos','shorts','live'))


def schedule(store, media, channel=None):
    from .downloads import enqueue
    from .media_holds import blocked_in
    with store.connect() as db:
        count=db.execute("SELECT count(*) FROM downloads WHERE state IN ('queued','downloading','cancelling')").fetchone()[0]
        if count>=100:return 0
        rules=[dict(r) for r in db.execute('SELECT r.* FROM download_rules r JOIN channels c ON c.id=r.channel WHERE c.active=1 AND (r.videos OR r.shorts OR r.live)')]
    added=0
    for rule in rules:
        rule=resolve_defaults(store,rule)
        if not rule['quality_override']:rule['quality']=store.get('download_quality','1080')
        if rule['subscription_paused']:continue
        if channel and rule['channel']!=channel:continue
        cutoff=range_cutoff(rule['range_days']) if rule['range_days'] else '0000-01-01'
        with store.connect() as db:
            rows=db.execute("""SELECT v.data FROM videos v LEFT JOIN downloads d ON d.id=v.id
                WHERE v.channel=? AND v.day>=? AND json_extract(v.data,'$.state')='published'
                AND COALESCE(json_extract(v.data,'$.browse_only'),0)=0
                AND (d.id IS NULL OR d.state='expired') AND (COALESCE(json_extract(v.data,'$.kind'),'unknown')='unknown'
                OR (json_extract(v.data,'$.kind')='videos' AND ?)
                OR (json_extract(v.data,'$.kind')='shorts' AND ?)
                OR (json_extract(v.data,'$.kind')='live' AND ?))
                ORDER BY v.published DESC,v.id LIMIT ?""",
                (rule['channel'],cutoff,rule['videos'],rule['shorts'],rule['live'],100-count)).fetchall()
        for row in rows:
            video=json.loads(row[0])
            if eligible(video,rule):
                with store.connect() as db:
                    if blocked_in(db,['video:'+video['id'],'managed:video:'+video['id']]):continue
                enqueue(store,video,rule['quality'],automatic=True)
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    if blocked_in(db,['video:'+video['id'],'managed:video:'+video['id']]):continue
                    queued=db.execute("SELECT 1 FROM downloads WHERE id=? AND state='queued'",(video['id'],)).fetchone()
                    if not queued:continue
                    if library.local_file(store,media,video['id']):
                        db.execute("UPDATE downloads SET state='complete',progress=100,message='Already saved on NAS',updated=? WHERE id=?",(time.time(),video['id']))
                    else:added+=1;count+=1
        if count>=100:break
    return added


def queue_action(store, channel, action):
    if action not in ACTIONS:raise ValueError('Unknown channel action.')
    with store.connect() as db:
        old=db.execute('SELECT state FROM channel_tasks WHERE channel=?',(channel,)).fetchone()
        if old and old[0] in ('queued','running'):raise ValueError('A channel action is already pending.')
        db.execute("INSERT OR REPLACE INTO channel_tasks VALUES (?,?,'queued','',?)",(channel,action,time.time()))


def perform(store, media, channel, action):
    from .downloads import enqueue
    if action=='refresh_emby':
        from .emby import refresh
        refresh(store,channel)
        return 'Emby library scan queued. Check Third Party Media Player for scan status.'
    with store.connect() as db:
        videos=[json.loads(r[0]) for r in db.execute('SELECT data FROM videos WHERE channel=?',(channel,))]
    if action=='deep_scan':
        with store.connect() as db:
            row=db.execute('SELECT data FROM channels WHERE id=?',(channel,)).fetchone()
            channel_data=json.loads(row[0])
            db.execute('INSERT OR IGNORE INTO youtube_history_requests VALUES(?)',(channel,))
            db.execute('INSERT OR REPLACE INTO history_jobs VALUES (?,?,?,?,?,?,?,?)',
                       (channel,channel_data.get('playlist',''),'','indexing',0,0,'','[]'))
        store.set('next_history',0)
        return 'History scan queued using the available YouTube connection.'
    if action=='download_pending':
        n=schedule(store,media,channel)
        return f'{n} videos queued using enabled types and range. More are added as the queue drains.'
    if action=='redownload_existing':
        # Replacements follow the channel settings too: never re-fetch, and so
        # never overwrite, a saved file the settings now exclude.
        from .media_holds import blocked_in
        rule=preferences(store,channel);queued=excluded=held=0
        for video in videos:
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if not db.execute("SELECT 1 FROM downloads WHERE id=? AND state='complete'",(video['id'],)).fetchone():continue
                if blocked_in(db,['video:'+video['id'],'managed:video:'+video['id']]):
                    held+=1;continue
                if rule_conflict(video,rule):
                    excluded+=1;continue
                queued+=db.execute("UPDATE downloads SET state='queued',replace_existing=1,message='Replace requested',updated=? WHERE id=? AND state='complete'",(time.time(),video['id'])).rowcount
        message=f"{queued} completed download{'s' if queued!=1 else ''} queued for replacement."
        if excluded:message+=f" {excluded} not replaced because this channel's download settings exclude {'them' if excluded!=1 else 'it'}."
        if held:message+=f" {held} cleared video{'s remain' if held!=1 else ' remains'} stopped until explicitly selected again."
        return message
    if action in ('refresh_metadata','sync_files'):
        done=0
        for video in videos:
            if library.organise(store,media,video['id']):done+=1
        return f'{done} saved videos organised and metadata refreshed.'
    # Disable automation before deletion, so removed files are not downloaded again.
    with store.connect() as db:
        db.execute('UPDATE download_rules SET videos=0,shorts=0,live=0 WHERE channel=?',(channel,))
        db.execute("UPDATE downloads SET state='cancelled' WHERE state='queued' AND id IN (SELECT id FROM videos WHERE channel=?)",(channel,))
    if action in ('delete_media','remove_all'):
        for video in videos:
            path=library.local_file(store,media,video['id'])
            if path:
                with store.connect() as db:
                    record=db.execute('SELECT * FROM media_files WHERE id=?',(video['id'],)).fetchone()
                sidecars=library.metadata_paths(media,dict(record))[1:] if record else (path.with_suffix('.nfo'),path.with_name(path.stem+'-thumb.jpg'))
                for target in [path,*sidecars]:
                    checked=library.contained(media,str(target.relative_to(Path(media).resolve())))
                    checked.unlink(missing_ok=True)
            with store.connect() as db:
                db.execute("UPDATE downloads SET state='cancelled',message='Media deleted' WHERE id=?",(video['id'],))
                db.execute('UPDATE media_files SET bytes=0 WHERE id=?',(video['id'],))
    if action in ('remove_channel','remove_all'):
        with store.connect() as db:
            db.execute('INSERT OR IGNORE INTO removed_channels VALUES (?)',(channel,))
            db.execute('UPDATE channels SET active=0 WHERE id=?',(channel,))
        return 'Channel removed locally. Your YouTube subscription is unchanged.'
    return 'Downloaded videos and episode sidecars deleted. Channel artwork and subscription retained. Auto downloads are off.'


def expire_downloads(store, media, limit=100):
    """Apply retention only to recognised episode files; never scan/delete folders."""
    removed=0;changed=set()
    with store.connect() as db:
        candidates=db.execute("""SELECT d.id,coalesce(m.channel,v.channel) AS channel,
            coalesce(m.day,v.day) AS day,m.path FROM downloads d
            LEFT JOIN media_files m ON m.id=d.id LEFT JOIN videos v ON v.id=d.id
            JOIN download_rules r ON r.channel=coalesce(m.channel,v.channel)
            JOIN channels c ON c.id=r.channel
            WHERE c.active=1 AND r.subscription_paused=0 AND r.keep_files=0 AND (r.range_override=0 OR r.range_days>0) AND d.automatic=1
            AND d.state NOT IN ('queued','downloading','cancelling','expired')
            ORDER BY coalesce(m.day,v.day),d.id""").fetchall()
    for item in candidates:
        if removed>=limit:break
        with store.connect() as db:
            # Serialise with settings changes and queue operations before unlinking.
            db.execute('BEGIN IMMEDIATE')
            rule=db.execute('SELECT * FROM download_rules WHERE channel=?',(item['channel'],)).fetchone()
            state=db.execute('SELECT state,automatic FROM downloads WHERE id=?',(item['id'],)).fetchone()
            if rule:rule=resolve_defaults(store,rule)
            if not rule or rule['subscription_paused'] or rule['keep_files'] or not rule['range_days'] or not state or state['automatic']!=1 or state[0] in ('queued','downloading','cancelling','expired'):continue
            try:
                from datetime import date
                day=date.fromisoformat(item['day']).isoformat()
            except (ValueError,TypeError):continue
            if day>=range_cutoff(rule['range_days']):continue
            relatives=([item['path']] if item['path'] else [])+[item['id']+'.mp4',item['id']+'.webm']
            targets=set()
            for relative in relatives:
                path=library.contained(media,relative)
                targets.update((path,path.with_suffix('.nfo'),path.with_name(path.stem+'-thumb.jpg')))
            # Resolve every target before deleting anything; reject paths outside media.
            checked=[library.contained(media,str(target.relative_to(Path(media).resolve()))) for target in targets]
            for target in checked:target.unlink(missing_ok=True)
            db.execute("UPDATE downloads SET state='expired',progress=0,message='Removed automatically: outside this channel’s download range.',updated=? WHERE id=?",(time.time(),item['id']))
            db.execute('UPDATE media_files SET bytes=0 WHERE id=?',(item['id'],))
            removed+=1;changed.add(item['channel'])
    if changed and store.get('emby_auto_refresh')=='true':
        from .emby import queue_refresh
        for channel in changed:queue_refresh(store,channel)
    return removed


def tick(store, media):
    if float(store.get('next_retention_check','0'))<=time.time():
        try:
            count=expire_downloads(store,media)
            store.set('retention_status','Automatic retention checked successfully.')
        except (OSError,ValueError):
            count=0
            store.set('retention_status','Automatic retention could not remove a file. Check media folder permissions; another attempt will run in five minutes.')
        store.set('next_retention_check',time.time()+(5 if count>=100 else 300))
    with store.connect() as db:
        task=db.execute("SELECT * FROM channel_tasks WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
        if task:db.execute("UPDATE channel_tasks SET state='running' WHERE channel=?",(task['channel'],))
    if task:
        try:
            message=perform(store,media,task['channel'],task['action']);state='complete'
        except Exception:
            message='Channel action failed. Check media folder access and retry.';state='failed'
        with store.connect() as db:db.execute('UPDATE channel_tasks SET state=?,message=? WHERE channel=?',(state,message,task['channel']))
    if float(store.get('next_auto_check','0'))<=time.time():
        schedule(store,media)
        store.set('next_auto_check',time.time()+30)
