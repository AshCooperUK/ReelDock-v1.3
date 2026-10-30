"""Durable Activity visibility, separate from operational jobs and export history."""
import json
import re
import time

TERMINAL = ('complete', 'skipped', 'failed', 'error', 'cancelled', 'expired', 'stopped')
TABLES = {'youtube': 'downloads', 'm3u': 'm3u_downloads', 'm3u_sync': 'm3u_runs',
          'event': 'activity_events', 'discover': 'discover_requests', 'task': 'activity_tasks',
          'm3u_source':'m3u_sources','m3u_metadata_task':'m3u_metadata_runs','m3u_dry_task':'m3u_dry_runs'}
PUBLIC_FIELDS = {'id','title','name','state','phase','progress','created','updated','finished','added',
    'message','subscription','sourceType','sourceName','kind','mediaKind','channel','channel_name',
    'avatar','artwork','banner','description','destination','metadata_note','error','stop_reason',
    'published','down_rate','up_rate','peers','connections','stats','year','genre','rating','activity_generation'}


def initialise(store):
    """Only startup migrates; ordinary reads never create tables or generations."""
    with store.connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS activity_generations(kind TEXT NOT NULL,identity TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(kind,identity));
        CREATE TABLE IF NOT EXISTS activity_hidden(kind TEXT NOT NULL,identity TEXT NOT NULL,
            generation INTEGER NOT NULL,cleared REAL NOT NULL,PRIMARY KEY(kind,identity,generation));
        CREATE TABLE IF NOT EXISTS activity_archive(id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,identity TEXT NOT NULL,generation INTEGER NOT NULL,
            created REAL NOT NULL,cleared REAL NOT NULL,data TEXT NOT NULL,
            UNIQUE(kind,identity,generation));
        CREATE TABLE IF NOT EXISTS activity_clear_revision(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL);
        INSERT OR IGNORE INTO activity_clear_revision VALUES(1,0);
        ''')
        existing={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for kind, table in TABLES.items():
            if table not in existing:continue
            key='source' if kind in ('m3u_metadata_task','m3u_dry_task') else 'id'
            db.execute(f"INSERT OR IGNORE INTO activity_generations SELECT ?,CAST({key} AS TEXT),0 FROM {table}",(kind,))
            db.executescript(f'''
            CREATE TRIGGER IF NOT EXISTS activity_generation_{kind}_insert AFTER INSERT ON {table}
            BEGIN INSERT INTO activity_generations VALUES('{kind}',CAST(NEW.{key} AS TEXT),0)
              ON CONFLICT(kind,identity) DO UPDATE SET generation=generation+1; END;
            CREATE TRIGGER IF NOT EXISTS activity_generation_{kind}_update AFTER UPDATE OF state ON {table}
            WHEN OLD.state!=NEW.state
            BEGIN INSERT INTO activity_generations VALUES('{kind}',CAST(NEW.{key} AS TEXT),1)
              ON CONFLICT(kind,identity) DO UPDATE SET generation=generation+1; END;
            ''')
    from .media_holds import initialise as initialise_holds
    initialise_holds(store)


def visibility_sql(kind, alias):
    """Whitelisted SQL fragment; enrichment timestamps are deliberately absent."""
    assert kind in TABLES
    from .media_holds import suppressed_sql
    return f"NOT EXISTS(SELECT 1 FROM activity_hidden ah WHERE ah.kind='{kind}' AND ah.identity=CAST({alias}.id AS TEXT) AND ah.generation=COALESCE((SELECT generation FROM activity_generations ag WHERE ag.kind='{kind}' AND ag.identity=CAST({alias}.id AS TEXT)),0)) AND "+suppressed_sql(kind,alias)


def generation(db, kind, identity):
    r=db.execute('SELECT generation FROM activity_generations WHERE kind=? AND identity=?',(kind,str(identity))).fetchone()
    return r[0] if r else 0


def is_hidden(db, kind, identity, version=None):
    version=generation(db,kind,identity) if version is None else version
    return db.execute('SELECT 1 FROM activity_hidden WHERE kind=? AND identity=? AND generation=?',(kind,str(identity),version)).fetchone() is not None


def metadata(store):
    with store.connect() as db:
        return metadata_from_db(db)


def metadata_from_db(db):
    """Read visibility on the caller's existing SQLite snapshot."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='activity_hidden'").fetchone():
        return dict(activityRevision=0,clearedEngineIds={'torrent':[],'nzb':[]},clearedEngineGenerations={'torrent':{},'nzb':{}})
    revision=db.execute('SELECT revision FROM activity_clear_revision WHERE id=1').fetchone()[0]
    versions={'torrent':{},'nzb':{}}
    for r in db.execute("SELECT kind,identity,MAX(generation) AS generation FROM activity_hidden WHERE kind IN ('torrent','nzb') GROUP BY kind,identity"):
        versions[r['kind']][r['identity']]=r['generation']
    from .media_holds import suppressed_ids
    suppressed=suppressed_ids(db)
    return dict(activityRevision=revision,clearedEngineIds={k:list(v) for k,v in versions.items()},clearedEngineGenerations=versions,suppressedActivityIds=suppressed)


def section(kind, item):
    state=item.get('state')
    # Retention is successful policy cleanup; keep its raw expired state for
    # scheduling and export history while clearing it with completed outcomes.
    if kind=='youtube' and state=='expired' and str(item.get('message') or '') in (
        'Removed automatically: outside this channel’s download range.',
        "Removed automatically: outside this channel's download range.",
    ):return 'completed'
    if kind=='torrent':
        # A seeding/downloading/paused/placement job still owns live controls.
        if state in ('queued','downloading','seeding','paused','choose'):return None
        if item.get('progress',0)>=1:return 'completed'
    elif kind=='nzb':
        if state in ('queued','downloading','paused','choose'):return None
        if item.get('progress',0)>=1 and state=='stopped':return 'completed'
    if state in ('complete','skipped'):return 'completed'
    if state in ('failed','error','cancelled','expired','stopped'):return 'attention'
    return None


def native_generation(job, terminal):
    """Called only on a native engine's existing save path, never on GET."""
    old=getattr(job,'_activity_terminal',None)
    version=getattr(job,'activity_generation',None)
    if version is None:
        added=getattr(job,'added',0)
        candidate=int(added*1_000_000) if type(added) in (int,float) and 0<=added<9_007_199_254 else 0
        version=candidate if candidate<9_007_199_254_740_991 else 0
    if old is True and not terminal:version+=1
    job.activity_generation=version
    job._activity_terminal=terminal
    return version


def restore_native(job, saved, terminal):
    version=saved.get('activity_generation',0)
    job.activity_generation=version if type(version) is int and 0<=version<=9_007_199_254_740_991 else 0
    job._activity_terminal=terminal


def native_jobs(app):
    """Snapshot already-open engines only; never construct engines or start work."""
    result=[]
    for kind in ('torrent','nzb'):
        engine=app.extensions.get(kind+'_engine')
        if engine is not None:
            result.extend(dict(kind=kind,item=item) for item in engine.jobs())
    return result


def snapshot_visible(store, identity):
    source=('task',identity)
    for prefix,kind in [('server-download-','m3u'),('download-','youtube'),('discover-','discover'),
                        ('m3u_metadata_runs-','m3u_metadata_task'),('m3u_dry_runs-','m3u_dry_task'),('m3u-','m3u_source')]:
        if identity.startswith(prefix):source=(kind,identity[len(prefix):]);break
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='activity_hidden'").fetchone():return True
        from .media_holds import suppressed_ids
        return not is_hidden(db,*source) and source[1] not in suppressed_ids(db).get(source[0],[])


def _public(row):
    item={k:v for k,v in row['item'].items() if k in PUBLIC_FIELDS}
    # The archive never copies raw Discover bodies, URLs, credentials, headers,
    # provider options, native trackers, NNTP servers or uploaded file contents.
    for field in ('avatar','artwork','banner'):
        item.pop(field,None)
    def safe(value):
        if isinstance(value,str):
            value=re.sub(r'(?im)(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key)\s*:[^\n]*','[header omitted]',value)
            value=re.sub(r'(?i)\b[a-z][a-z0-9+.-]*://[^\s<>"\']+|//[^\s<>"\']+|\b(?:file|magnet|data|plugin):[^\s<>"\']+','[URL omitted]',value)
            return re.sub(r'(?i)\b(?:[\w-]*token|password|passwd|secret|api[_-]?key|passkey|authkey|session|credential|signature)\s*[:=]\s*[^\s,;&]+','[credential omitted]',value)
        if isinstance(value,list):return [safe(v) for v in value]
        if isinstance(value,dict):return {k:safe(v) for k,v in value.items()}
        return value
    item=safe(item)
    if row['kind'] in ('torrent','nzb'):
        item['files']=[dict(selected=bool(f.get('selected')),size=f.get('size',0)) for f in row['item'].get('files',[]) if isinstance(f,dict)]
    return dict(kind=row['kind'],item=item)


def archive_row(db, row, now=None):
    """Snapshot one exact source generation; used by explicit per-item Clear."""
    now=time.time() if now is None else now
    kind,item=row['kind'],row['item'];identity=str(item['id'])
    version=item.get('activity_generation',0) if kind in ('torrent','nzb') else generation(db,kind,identity)
    if is_hidden(db,kind,identity,version):return 0
    safe=_public(row);created=item.get('created') or item.get('added') or item.get('finished') or now
    db.execute('INSERT OR IGNORE INTO activity_archive(kind,identity,generation,created,cleared,data) VALUES(?,?,?,?,?,?)',
        (kind,identity,version,created,now,json.dumps(safe)))
    db.execute('INSERT OR IGNORE INTO activity_hidden VALUES(?,?,?,?)',(kind,identity,version,now))
    return 1


def clear(store, media, wanted, native=()):
    if wanted not in ('attention','completed','logs'):raise ValueError('Choose Needs attention, Completed or Clear Logs.')
    def matches(kind,item):
        bucket=section(kind,item)
        return bucket in ('attention','completed') if wanted=='logs' else bucket==wanted
    from .download_history import page
    now=time.time();cleared=0
    with store.connect() as db:
        # SQLite source rows cannot cross this server snapshot while it is made.
        # Native snapshots carry their own persisted attempt generation.
        db.execute('BEGIN IMMEDIATE')
        rows=[];offset=0
        while True:
            batch=page(store,media,50,offset,plain=True)
            rows.extend(row for row in batch['items'] if matches(row['kind'],row['item']))
            offset+=len(batch['items'])
            if not batch['more']:break
            if not batch['items'] or offset>100000:raise ValueError('Activity is too large to clear safely. No activity was cleared.')
        rows.extend(row for row in native if matches(row['kind'],row['item']))
        for row in rows:
            kind, item=row['kind'],row['item'];identity=str(item['id'])
            version=item.get('activity_generation',0) if kind in ('torrent','nzb') else generation(db,kind,identity)
            if is_hidden(db,kind,identity,version):continue
            safe=_public(row);created=item.get('created') or item.get('added') or item.get('finished') or now
            db.execute('INSERT OR IGNORE INTO activity_archive(kind,identity,generation,created,cleared,data) VALUES(?,?,?,?,?,?)',
                       (kind,identity,version,created,now,json.dumps(safe)))
            db.execute('INSERT OR IGNORE INTO activity_hidden VALUES(?,?,?,?)',(kind,identity,version,now));cleared+=1
        for kind in ('task','m3u_source','m3u_metadata_task','m3u_dry_task'):
            key='source' if kind in ('m3u_metadata_task','m3u_dry_task') else 'id'
            for task in db.execute('SELECT * FROM '+TABLES[kind]).fetchall():
                if matches(kind,dict(task)):
                    identity=str(task[key]);version=generation(db,kind,identity)
                    if is_hidden(db,kind,identity,version):continue
                    item=dict(task);stamp=item.get('updated') or item.get('last_run') or 0
                    name=item.get('name') or ''
                    if kind in ('m3u_metadata_task','m3u_dry_task'):
                        source=db.execute('SELECT name FROM m3u_sources WHERE id=?',(identity,)).fetchone()
                        name=source['name'] if source else 'M3U'
                    label={'m3u_source':'Playlist','m3u_metadata_task':'Metadata','m3u_dry_task':'Dry run'}.get(kind)
                    item.update(id=identity,title=item.get('title') or ((name+' · '+label) if label else 'Background work'),
                                created=stamp,finished=stamp,subscription='M3U'+(' · '+name if name else '') if kind.startswith('m3u') else 'Background work')
                    safe=_public(dict(kind='event',item=item))
                    db.execute('INSERT OR IGNORE INTO activity_archive(kind,identity,generation,created,cleared,data) VALUES(?,?,?,?,?,?)',
                               (kind,identity,version,stamp,now,json.dumps(safe)))
                    db.execute('INSERT OR IGNORE INTO activity_hidden VALUES(?,?,?,?)',(kind,identity,version,now));cleared+=1
        if wanted=='logs':
            # Operational rows and their attempt/ownership records stay intact.
            # Existing visibility fences hide the finished source rows; remove
            # only their public log copies so TXT exports are cleared as well.
            archived=db.execute('SELECT COUNT(*) FROM activity_archive').fetchone()[0]
            db.execute('DELETE FROM activity_archive')
            cleared=max(cleared,archived)
        if cleared:db.execute('UPDATE activity_clear_revision SET revision=revision+1 WHERE id=1')
        revision=db.execute('SELECT revision FROM activity_clear_revision WHERE id=1').fetchone()[0]
    return dict(ok=True,section=wanted,cleared=cleared,activityRevision=revision,clearedBefore=now,**{k:v for k,v in metadata(store).items() if k!='activityRevision'})


def archived_row(db, identity):
    row=db.execute('SELECT * FROM activity_archive WHERE id=?',(identity,)).fetchone()
    if not row:return None
    result=json.loads(row['data']);item=result['item']
    item.update(originalId=item['id'],id='archive:'+str(row['id']),archived=True,clearedAt=row['cleared'])
    return result
