"""Bounded, combined download history, independent of the full worker queue."""
import json
import time
from urllib.parse import urlencode
from .media_library import local_file


def _observed_row(db,kind,item,generation_enabled=True):
    from .activity_observation import capture
    from .activity_clear import generation
    item['activity_observation']=capture(kind,item,generation(db,kind,item['id']) if generation_enabled else 0)
    return dict(kind=kind,item=item)


def _history_union(db, include_archived=False):
    from .activity_clear import visibility_sql
    enabled=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='activity_hidden'").fetchone()
    parts=[]
    for kind,table,stamp in [('youtube','downloads','created'),('m3u','m3u_downloads','created'),('m3u_sync','m3u_runs','finished'),('event','activity_events','created'),('discover','discover_requests','created')]:
        visible=visibility_sql(kind,'s') if enabled else '1'
        if kind=='discover':visible+=" AND s.kind!='video'"
        parts.append(f"SELECT s.id,'{kind}' AS kind,s.{stamp} AS created FROM {table} s WHERE {visible}")
    if enabled and include_archived:parts.append("SELECT id,'archive' AS kind,created FROM activity_archive")
    union=' UNION ALL '.join(parts)
    return union, bool(enabled)


def _history_row(db, key, enabled):
    from .activity_clear import archived_row
    if key['kind']=='archive':
        archived=archived_row(db,key['id'])
        return archived
    if key['kind']=='youtube':
        row=db.execute('SELECT d.*,v.data AS video_data,c.data AS channel_data FROM downloads d LEFT JOIN videos v ON v.id=d.id LEFT JOIN channels c ON c.id=v.channel WHERE d.id=?',(key['id'],)).fetchone()
        item=dict(row);video=json.loads(item.pop('video_data') or '{}');channel=json.loads(item.pop('channel_data') or '{}')
        item.update(video=video,channel=channel.get('id',''),channel_name=channel.get('name','Unknown channel'),avatar=channel.get('avatar',''),banner=channel.get('banner',''))
    elif key['kind']=='event':
        item=dict(db.execute('SELECT * FROM activity_events WHERE id=?',(key['id'],)).fetchone())
        item.update(finished=item['created'],subscription=item.pop('category'),stats={})
    elif key['kind']=='m3u_sync':
        row=db.execute('SELECT r.*,s.name FROM m3u_runs r LEFT JOIN m3u_sources s ON s.id=r.source WHERE r.id=?',(key['id'],)).fetchone()
        item=dict(row);item['stats']=json.loads(item['stats'])
        item.update(created=item['finished'],subscription=item.pop('name') or 'M3U',title='M3U library sync')
        # Older failed runs could carry the previous successful run's totals.
        if item['state']!='complete':item['stats']={}
    elif key['kind']=='discover':
        row=db.execute('SELECT * FROM discover_requests WHERE id=?',(key['id'],)).fetchone()
        body=json.loads(row['body'] or '{}')
        item=dict(row)
        from .activity_media import request_item
        parent=request_item(row)
        if parent:item['mediaItem']=parent
        from .source_choice import public as source_choice
        choice=source_choice(db,row)
        if choice:item['sourceChoice']=choice
        item.update(title=row['title'],state=row['state'],message=row['message'],subscription='Discover',
            mediaKind='movie' if row['kind']=='movie' else 'show',artwork=str(body.get('image') or ''),
            year=str(body.get('year') or '')[:4],genre=str(body.get('genre') or '')[:80],
            rating=str(body.get('rating') or '')[:8],description=str(body.get('description') or '')[:400],
            externalId=str(body.get('externalId') or ''),posterChecked=float(body.get('posterChecked') or 0),destination='')
    else:
        row=db.execute('SELECT * FROM m3u_downloads WHERE id=?',(key['id'],)).fetchone();item=dict(row)
        from .strm import public_metadata
        manual=public_metadata(db,row['source'])
        if manual:item.update(manual)
        else:
            meta=db.execute('SELECT t.data,s.name FROM m3u_sources s LEFT JOIN m3u_catalog_entries c ON c.source=s.id AND c.path=? LEFT JOIN m3u_catalog_titles t ON t.key=c.key WHERE s.id=?',(row['path'],row['source'])).fetchone()
            data=json.loads(meta['data'] or '{}') if meta else {}
            item.update(artwork=data.get('artwork') or '/api/m3u/'+row['source']+'/artwork?'+urlencode({'path':row['path']}),subscription=meta['name'] if meta else 'M3U',description=data.get('description',''))
    return _observed_row(db,key['kind'],item,enabled)


MAX_ACTIVITY_HISTORY = 5_000
_ACTIVE_STATES = {
    'youtube': {'queued','downloading','cancelling'},
    'm3u': {'queued','running','cancelling'},
    'discover': {'searching','starting','waiting','queued','downloading','verifying','paused','cancelling'},
}


def snapshot(store, media):
    """One complete bounded read frame while ordinary acquisition keeps writing.

    BEGIN is explicit: sqlite3's connection context alone does not hold a read
    transaction across SELECT statements. Visibility and observed generations
    must be read on the same connection as keys and saved row state.
    """
    from .activity_clear import metadata_from_db
    with store.connect() as db:
        db.execute('BEGIN')
        union, enabled = _history_union(db)
        total = db.execute('SELECT COUNT(*) FROM ('+union+')').fetchone()[0]
        if total > MAX_ACTIVITY_HISTORY:
            raise ValueError('Activity has more than 5,000 entries. No partial list was shown. Export the log to view the full history.')
        keys = db.execute(union+' ORDER BY created DESC,kind,id').fetchall()
        items = [_history_row(db,key,enabled) for key in keys]
        active = [row for row in items if row['item']['state'] in _ACTIVE_STATES.get(row['kind'], ())]
        hold = db.execute("SELECT value FROM settings WHERE key='acquisition_hold'").fetchone()
        result = dict(items=items,total=total,more=False,active=active,
                      acquisitionHold=bool(hold and hold[0]=='1'),**metadata_from_db(db))
    # Only checked local paths are resolved after releasing the SQL read
    # transaction. This does not refresh providers or modify saved facts.
    for row in items:
        if row['kind']=='youtube':
            path=local_file(store,media,row['item']['id'])
            row['item']['destination']=str(path) if path else ''
        elif row['kind']=='discover':
            row['item'].pop('posterChecked',None)
            row['item'].pop('externalId',None)
    return result


def page(store, media, limit=15, offset=0, *, plain=False, include_archived=False, cached=False):
    plain=plain or include_archived
    if type(limit) is not int or not 1<=limit<=50:raise ValueError('Choose between 1 and 50 downloads per page.')
    if type(offset) is not int or offset<0:raise ValueError('Choose a valid activity page.')
    with store.connect() as db:
        union, enabled = _history_union(db, include_archived)
        keys=db.execute(union+' ORDER BY created DESC,kind,id LIMIT ? OFFSET ?',(limit,offset)).fetchall()
        total=db.execute('SELECT COUNT(*) FROM ('+union+')').fetchone()[0]
        items=[]
        for key in keys[:limit]:
            row=_history_row(db,key,enabled)
            if row:items.append(row)
    # Plain export reads use saved facts only: no provider refresh, filesystem
    # resolution or incidental metadata writes. Ordinary Activity keeps its
    # existing enrichment and containment checks.
    for row in items:
        if row['kind']=='youtube':
            path=local_file(store,media,row['item']['id']) if not plain else None
            row['item']['destination']=str(path) if path else ''
        elif not plain and not cached and row['kind']=='discover' and row['item'].get('mediaKind')=='show' and not (row['item'].get('artwork') and row['item'].get('genre')) and time.time()-float(row['item'].get('posterChecked') or 0)>=3600:
            from .discover_sources import show_media
            info=show_media(row['item'].get('externalId'))
            artwork=row['item'].get('artwork') or info.get('image') or ''
            description=row['item'].get('description') or info.get('summary') or ''
            with store.connect() as db:
                saved=db.execute('SELECT body FROM discover_requests WHERE id=?',(row['item']['id'],)).fetchone()
                body=json.loads(saved['body'] or '{}') if saved else {}
                body['posterChecked']=time.time()
                if artwork:body['image']=artwork
                if description and not body.get('description'):body['description']=description[:800]
                for field in ('genre','rating','year'):
                    if info.get(field) and not body.get(field):body[field]=info[field]
                db.execute('UPDATE discover_requests SET body=? WHERE id=?',(json.dumps(body),row['item']['id']))
            row['item']['artwork']=artwork
            row['item']['description']=description[:400]
            for field in ('genre','rating','year'):
                if info.get(field) and not row['item'].get(field):row['item'][field]=info[field]
        if row['kind']=='discover':
            row['item'].pop('posterChecked', None)
            row['item'].pop('externalId', None)
    active=[]
    with store.connect() as db:
        active_keys=[]
        active_keys += [('youtube', row['id']) for row in db.execute("SELECT id FROM downloads WHERE state IN ('queued','downloading','cancelling')")]
        try:
            active_keys += [('m3u', row['id']) for row in db.execute("SELECT id FROM m3u_downloads WHERE state IN ('queued','running','cancelling')")]
        except Exception:
            pass
        try:
            active_keys += [('discover', row['id']) for row in db.execute("SELECT id FROM discover_requests WHERE kind!='video' AND state IN ('searching','starting','waiting','queued','downloading','verifying','paused','cancelling')")]
        except Exception:
            pass
    seen=set()
    for kind, identity in active_keys:
        if (kind, identity) in seen:
            continue
        seen.add((kind, identity))
        match=next((row for row in items if row['kind']==kind and str(row['item'].get('id'))==str(identity)), None)
        if match:
            active.append(match)
            continue
        built=_active_row(store, kind, identity)
        if built:
            active.append(built)
    from .activity_clear import metadata
    return dict(items=items,total=total,more=offset+len(items)<total,acquisitionHold=store.get('acquisition_hold')=='1',active=active,**metadata(store))


def _active_row(store, kind, identity):
    """A job that is still running, even when it is older than the loaded history page."""
    with store.connect() as db:
        enabled=bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='activity_generations'").fetchone())
        if kind=='youtube':
            row=db.execute('SELECT d.*,v.data AS video_data,c.data AS channel_data FROM downloads d LEFT JOIN videos v ON v.id=d.id LEFT JOIN channels c ON c.id=v.channel WHERE d.id=?',(identity,)).fetchone()
            if not row:
                return None
            item=dict(row)
            video=json.loads(item.pop('video_data') or '{}')
            channel=json.loads(item.pop('channel_data') or '{}')
            item.update(video=video,channel=channel.get('id',''),channel_name=channel.get('name','Unknown channel'),avatar=channel.get('avatar',''),banner=channel.get('banner',''),destination='')
            return _observed_row(db,'youtube',item,enabled)
        if kind=='m3u':
            row=db.execute('SELECT * FROM m3u_downloads WHERE id=?',(identity,)).fetchone()
            if not row:
                return None
            item=dict(row)
            from .strm import public_metadata
            item.update(public_metadata(db,row['source']) or dict(artwork='',subscription='M3U',description=''))
            return _observed_row(db,'m3u',item,enabled)
        row=db.execute('SELECT * FROM discover_requests WHERE id=?',(identity,)).fetchone()
        if not row:
            return None
        body=json.loads(row['body'] or '{}')
        item=dict(row)
        from .activity_media import request_item
        parent=request_item(row)
        if parent:item['mediaItem']=parent
        from .source_choice import public as source_choice
        choice=source_choice(db,row)
        if choice:item['sourceChoice']=choice
        item.update(subscription='Discover',mediaKind='movie' if row['kind']=='movie' else 'show',artwork=str(body.get('image') or ''),
            year=str(body.get('year') or '')[:4],genre=str(body.get('genre') or '')[:80],rating=str(body.get('rating') or '')[:8],
            description=str(body.get('description') or '')[:400],destination='')
        return _observed_row(db,'discover',item,enabled)
