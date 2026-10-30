"""Resumable, fair history indexing with provider-controlled quotas."""
import json
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HISTORY_DAYS = 0  # History is only fetched for explicitly enabled download ranges.


def seed(store, channel, listing):
    """Seed from the recent page; preserve an unfinished older-page cursor."""
    from .channel_downloads import preferences
    rule=preferences(store,channel['id'])
    if not any(rule[k] for k in ('videos','shorts','live')):return
    now = time.time()
    cursor = listing.get('nextPageToken', '')
    with store.connect() as db:
        old = db.execute('SELECT * FROM history_jobs WHERE channel=?', (channel['id'],)).fetchone()
        if old and old['playlist'] == channel['playlist'] and (old['state'] != 'complete' or now-old['updated'] < 20*86400):
            return
        db.execute('INSERT OR REPLACE INTO history_jobs VALUES (?,?,?,?,?,?,?,?)',
                   (channel['id'],channel['playlist'],cursor,'indexing' if cursor else 'complete',now,0,'',json.dumps([])))


def request(importer, resource, **params):
    store = importer.store
    from .sync_controls import check
    check(store)
    from .youtube_source import fallback
    if resource == 'playlistItems' or fallback(store) or str(params.get('pageToken','')).startswith('fb:'):return importer.api(resource,**params)
    day = datetime.now(ZoneInfo('America/Los_Angeles')).date().isoformat()
    from .quota import bind, client_id
    client = client_id(importer.app,store)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        bind(db,client)
        old = db.execute("SELECT value FROM settings WHERE key='history_quota_day'").fetchone()
        used = db.execute("SELECT value FROM settings WHERE key='history_quota_used'").fetchone()
        count = int(used[0]) if old and old[0] == day and used else 0
        db.execute("INSERT OR REPLACE INTO settings VALUES ('history_quota_day',?)", (day,))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('history_quota_used',?)", (str(count+1),))
    # This request also counts against the total request counter.
    return importer.api(resource, **params)


def coverage(store, items):
    """Add current history progress to channel responses in a single query."""
    with store.connect() as db:
        stats = {r['channel']:r for r in db.execute('SELECT h.channel,h.state,h.error,COUNT(v.id) AS count,MIN(v.day) AS oldest FROM history_jobs h LEFT JOIN videos v ON v.channel=h.channel GROUP BY h.channel')}
    for item in items:
        row = stats.get(item['id'])
        if row:
            status = 'History checked (selected range)' if row['state']=='complete' else 'Older history indexing'
            if row['error']:
                status = 'History paused; retry pending'
            item['coverage'] = f"{status} · {row['count']} saved videos" + (f" · oldest {row['oldest']}" if row['oldest'] else '')
    return items


def batch(importer, limit=2, now=None):
    store = importer.store
    current = now or datetime.now(timezone.utc)
    cutoff = current-timedelta(days=HISTORY_DAYS)
    changed = False
    if store.get('youtube_sync_paused')=='true':return False
    for _ in range(limit):
        # Yield as soon as a recent-upload refresh is requested or becomes due.
        if store.get('sync_requested') == '1' or float(store.get('next_sync','0')) <= time.time():
            break
        with store.connect() as db:
            job = db.execute("SELECT h.* FROM history_jobs h JOIN channels c ON c.id=h.channel JOIN download_rules d ON d.channel=h.channel WHERE c.active=1 AND (d.videos=1 OR d.shorts=1 OR d.live=1 OR EXISTS(SELECT 1 FROM youtube_history_requests x WHERE x.channel=h.channel)) AND h.state='indexing' AND (h.failures=0 OR h.updated<?) ORDER BY h.updated,h.channel LIMIT 1", (time.time()-300,)).fetchone()
        if not job:
            store.set('history_message','No history checks are running. Waiting for eligible channels or scheduled retries.')
            break
        store.set('history_message','Checking older uploads for one channel in its selected download range.')
        try:
            listing = request(importer,'playlistItems',part='contentDetails',playlistId=job['playlist'],maxResults=50,pageToken=job['cursor'])
            next_page = listing.get('nextPageToken','')
            trail = json.loads(job['trail'])
            if next_page and next_page in [job['cursor'], *trail]:
                raise ValueError('YouTube repeated a history page. The channel will retry from its newest page.')
            ids = [item['contentDetails']['videoId'] for item in listing.get('items',[])]
            with store.connect() as db:
                # A normal range/history observation is authoritative even when
                # richer metadata already exists and the detail fetch is skipped.
                # Browsing alone must never make that same promotion.
                from .youtube_fallback import promote_observed
                promote_observed(db,ids,job['channel'])
                complete={r[0] for r in db.execute("SELECT id FROM videos WHERE COALESCE(json_extract(data,'$.metadata_pending'),0)=0 AND (json_extract(data,'$.state')='published' OR checked>?)",(time.time()-1800,))}
            ids=[identity for identity in ids if identity not in complete]
            if ids:
                result = request(importer,'videos',part='snippet,contentDetails,liveStreamingDetails,statistics',id=','.join(ids),maxResults=50)
                importer.save_videos(result.get('items',[]))
            from .youtube_fallback import fill_dates
            fill_dates(store,listing)
            dates=[]
            for item in listing.get('items',[]):
                try:
                    dates.append(datetime.fromisoformat(item['contentDetails']['videoPublishedAt'].replace('Z','+00:00')))
                except (KeyError,ValueError):
                    pass
            from .channel_downloads import preferences
            rule=preferences(store,job['channel'])
            days=rule['range_days']
            cutoff=current-timedelta(days=max(HISTORY_DAYS,days))
            reached = days!=0 and bool(dates) and len(dates)==len(listing.get('items',[])) and max(dates) < cutoff
            complete = not next_page or reached
            with store.connect() as db:
                db.execute('UPDATE history_jobs SET cursor=?,state=?,updated=?,failures=0,error=?,trail=? WHERE channel=?',
                           (next_page,'complete' if complete else 'indexing',time.time(),'',json.dumps([*trail,job['cursor']]),job['channel']))
                if complete:db.execute('DELETE FROM youtube_history_requests WHERE channel=?',(job['channel'],))
            changed = True
            store.set('history_message','Indexing older uploads in the background (selected channel ranges).')
        except Exception as exc:
            from .sync_controls import SyncPaused
            if isinstance(exc,SyncPaused):
                store.set('history_message',str(exc))
                break
            message = str(exc) if isinstance(exc,ValueError) else 'History request failed; saved progress will be retried.'
            if any(code in message for code in ('quotaExceeded', 'dailyLimitExceeded')):
                store.set('history_message',message)
                break
            failures = job['failures']+1
            # A rejected/expired page token must not leave a channel stuck forever.
            reset = failures >= 3
            with store.connect() as db:
                db.execute('UPDATE history_jobs SET updated=?,failures=?,error=?,cursor=?,trail=? WHERE channel=?',
                           (time.time(),failures,message,'' if reset else job['cursor'],'[]' if reset else job['trail'],job['channel']))
            store.set('history_message',message)
    if changed:
        from .forecasts import rebuild
        rebuild(store)
    return changed
