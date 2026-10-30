"""Link the selected title's future automation to its Watchlist write transaction."""
import json
import re
import time

CHANNEL_RESUME='watchlist_channel_resume'
TYPES=('videos','shorts','live')


def channel_choices(db):
    row=db.execute('SELECT value FROM settings WHERE key=?',(CHANNEL_RESUME,)).fetchone()
    choices=json.loads(row[0]) if row else {}
    if not isinstance(choices,dict) or any(not re.fullmatch(r'UC[A-Za-z0-9_-]{22}',key) or not isinstance(value,dict) or set(value)!=set(TYPES) or any(type(v) is not bool for v in value.values()) for key,value in choices.items()):
        raise ValueError('Could not read the saved channel download choices.')
    return choices


def resume_channel(db,key):
    row=db.execute('SELECT r.* FROM download_rules r JOIN channels c ON c.id=r.channel WHERE r.channel=? AND c.active=1',(key,)).fetchone()
    if not row:
        if not db.execute('SELECT 1 FROM channels WHERE id=? AND active=1',(key,)).fetchone():raise ValueError('Add this channel to your ReelDock channel list before enabling future downloads.')
        quality=db.execute("SELECT value FROM settings WHERE key='download_quality'").fetchone()
        db.execute('INSERT INTO download_rules(channel,quality,videos,shorts,live,quality_override,range_override) VALUES(?,?,0,0,0,0,0)',(key,str(quality[0]) if quality else '1080'))
        row=db.execute('SELECT * FROM download_rules WHERE channel=?',(key,)).fetchone()
    if any(row[field] for field in TYPES):return
    choices=channel_choices(db).get(key) or dict(videos=True,shorts=False,live=False)
    db.execute('UPDATE download_rules SET videos=?,shorts=?,live=?,enabled_at=? WHERE channel=?',(*(int(choices[field]) for field in TYPES),time.time(),key))
    db.execute("INSERT INTO settings VALUES('next_auto_check','0') ON CONFLICT(key) DO UPDATE SET value='0'")


def enable(db,key,item):
    """Explicit bookmark opt-in; existing workers own availability and acquisition."""
    from .media_management.service import today
    from .discover_title_releases import title_key
    now=time.time();kind=item['kind']
    if kind=='channel':return resume_channel(db,key)
    if kind=='show':
        # Unnumbered documentary releases must never enable umbrella-series plans.
        if title_key(item):return
        # A new Watchlist opt-in is future-only, including an older saved plan.
        # Download All Seasons remains a separate, explicit Options choice.
        db.execute('''INSERT INTO discover_series_plan VALUES(?,?,0,1,?,0)
            ON CONFLICT(parent) DO UPDATE SET download_all=0,download_future=1,
            future_from=CASE WHEN download_future=0 THEN excluded.future_from ELSE future_from END,checked=0''',
            (item['id'],json.dumps(item),today()))
        return
    if kind=='movie':
        db.execute('''INSERT INTO movie_options(key,item,automatic,enabled_at,created,updated) VALUES(?,?,1,?,?,?)
            ON CONFLICT(key) DO UPDATE SET automatic=1,
            enabled_at=CASE WHEN automatic=0 THEN excluded.enabled_at ELSE enabled_at END,updated=excluded.updated''',
            (key,json.dumps(item),now,now,now))
        media_key='movie:'+str(int(item['externalId'])) if item.get('externalId') else item['id']
    else:media_key='video:'+item['videoId']
    row=db.execute('SELECT key,body FROM managed_media WHERE key=? OR (parent=? AND kind=?) ORDER BY key=? DESC LIMIT 1',(media_key,item['id'],kind,media_key)).fetchone()
    if row:
        # Explicit cancellation/removal holds survive a bookmark opt-in.
        db.execute('UPDATE managed_media SET monitor=1,next_check=0 WHERE key=?',(row['key'],))
        if kind=='movie':
            body=json.loads(row['body']);body['automatic']=True
            db.execute('UPDATE managed_media SET body=? WHERE key=?',(json.dumps(body),row['key']))
    else:
        body=dict(item=item,date=item.get('releaseDate') or str(item.get('published') or '')[:10],dateLabel='Published' if kind=='video' else 'Release date',automatic=True)
        saved=db.execute("SELECT value FROM settings WHERE key='download_quality'").fetchone() if kind=='video' else None
        quality=str(saved[0]) if saved and str(saved[0]) in ('720','1080','2160') else '1080'
        db.execute('INSERT INTO managed_media(key,parent,kind,body,monitor,quality,updated) VALUES(?,?,?,?,1,?,?)',(media_key,item['id'],kind,json.dumps(body),quality,now))


def disable(db,key):
    from .watchlist import identity
    def matches(raw):
        try:return identity(raw)[0]==key
        except (ValueError,TypeError):return False
    now=time.time();parents=set();media=set();rules=set()
    for row in db.execute('SELECT key,item FROM movie_options').fetchall():
        raw=json.loads(row['item'])
        if matches(raw):
            parents.add(raw['id'])
            db.execute('UPDATE movie_options SET automatic=0,updated=? WHERE key=? AND automatic!=0',(now,row['key']))
    for row in db.execute('SELECT parent,item FROM discover_series_plan').fetchall():
        if matches(json.loads(row['item'])):
            parents.add(row['parent'])
            db.execute('UPDATE discover_series_plan SET download_all=0,download_future=0 WHERE parent=?',(row['parent'],))
    for row in db.execute('SELECT id,body FROM discover_rules').fetchall():
        raw=json.loads(row['body'])
        if matches(raw):
            parents.add(raw['id']);rules.add(row['id'])
            db.execute('UPDATE discover_rules SET enabled=0,updated=? WHERE id=? AND enabled!=0',(now,row['id']))
    # Use the bookmark's verified identity, including documentary title keys.
    for row in db.execute("SELECT key,parent,kind,body,monitor FROM managed_media WHERE monitor=1 OR parent IN (SELECT parent FROM managed_series WHERE monitoring='all')").fetchall():
        if matches(json.loads(row['body']).get('item')):
            parents.add(row['parent']);media.add(row['key'])
            if row['monitor']==1:
                # Restore series inheritance so a later explicit future-plan
                # activation works. Existing off/hold/quality choices remain.
                db.execute('UPDATE managed_media SET monitor=? WHERE key=?',(-1 if row['kind']=='episode' else 0,row['key']))
    if parents:
        marks=','.join('?' for _ in parents)
        for row in db.execute('SELECT key,body FROM managed_media WHERE parent IN ('+marks+')',tuple(parents)):
            if matches(json.loads(row['body']).get('item')):media.add(row['key'])
        db.execute("UPDATE managed_series SET monitoring='inherit' WHERE monitoring='all' AND parent IN ("+marks+')',tuple(parents))
    if re.fullmatch(r'UC[A-Za-z0-9_-]{22}',key):
        row=db.execute('SELECT videos,shorts,live FROM download_rules WHERE channel=?',(key,)).fetchone()
        if row and any(row[field] for field in TYPES):
            choices=channel_choices(db);choices[key]={field:bool(row[field]) for field in TYPES}
            db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(CHANNEL_RESUME,json.dumps(choices,sort_keys=True)))
        db.execute('UPDATE download_rules SET videos=0,shorts=0,live=0 WHERE channel=?',(key,))
        # Withdraw unstarted automatic uploads, preserving manual requests,
        # running jobs and saved files, including queued replacements.
        db.execute("""UPDATE downloads SET state=CASE WHEN replace_existing=1 THEN 'complete' ELSE 'skipped' END,
            progress=CASE WHEN replace_existing=1 THEN 100 ELSE 0 END,replace_existing=0,
            message='Automatic downloads off: removed from Watchlist.',updated=?
            WHERE automatic=1 AND state='queued' AND id IN (SELECT id FROM videos WHERE channel=?)""",(now,key))
    if key.startswith('youtube:video:'):
        db.execute("""UPDATE downloads SET state=CASE WHEN replace_existing=1 THEN 'complete' ELSE 'skipped' END,
            progress=CASE WHEN replace_existing=1 THEN 100 ELSE 0 END,replace_existing=0,
            message='Automatic downloads off: removed from Watchlist.',updated=?
            WHERE id=? AND automatic=1 AND state='queued'""",(now,key.removeprefix('youtube:video:')))
    # Reuse the existing start-generation fence for pre-engine automatic work.
    # A competing commit either already owns a job, or loses that fence and
    # follows the engine's established cancellation cleanup.
    for row in db.execute("SELECT id,episode_key,rule_id,manual,body FROM discover_requests WHERE state IN ('waiting','searching','starting') AND engine_id=''").fetchall():
        body=json.loads(row['body'])
        automatic=not row['manual'] or body.get('plan') in ('all','future') or body.get('movieAutomatic') or row['rule_id']
        selected=row['episode_key'] in media or row['rule_id'] in rules or matches(body.get('item'))
        if automatic and selected:
            db.execute("UPDATE discover_requests SET state='cancelled',stop_generation=IFNULL(stop_generation,0)+1,message='Automatic downloads off: removed from Watchlist.',updated=? WHERE id=?",(now,row['id']))
