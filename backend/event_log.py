"""Persistent user-facing events. Callers supply safe descriptions, never request data."""
import time
import logging
import sqlite3


def record(store, title, message, category='Server', state='complete'):
    try:
        with store.connect() as db:
            db.execute('INSERT INTO activity_events(created,category,title,state,message) VALUES(?,?,?,?,?)',
                       (time.time(),category,title,state,message))
    except (sqlite3.Error,OSError):
        logging.warning('Could not record activity event.')


def started(store, version, role='web'):
    # Version detection is atomic across web and worker startup.
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute("SELECT value FROM settings WHERE key='activity_release'").fetchone()
        if old and old['value']!=version:
            db.execute('INSERT INTO activity_events(created,category,title,state,message) VALUES(?,?,?,?,?)',
                       (time.time(),'Server','ReelDock updated','complete',f"Version changed from {old['value']} to {version}."))
        db.execute("INSERT OR REPLACE INTO settings VALUES('activity_release',?)",(version,))
        db.execute('INSERT INTO activity_events(created,category,title,state,message) VALUES(?,?,?,?,?)',
                   (time.time(),'Server','Background worker started' if role=='worker' else 'Web server started','complete',f'ReelDock {version} started.'))


def artwork_batch(store, count, failed=0):
    """One persistent summary per continuous run; five idle minutes starts a new run."""
    now=time.time()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for key,amount,title,state,word in (
            ('youtube-artwork',count,'YouTube artwork updated','complete','processed'),
            ('youtube-artwork-failed',failed,'YouTube artwork failed','failed','could not be downloaded')):
            if not amount:continue
            previous=db.execute('SELECT b.* FROM activity_batches b JOIN activity_events e ON e.id=b.event_id WHERE b.key=?',(key,)).fetchone()
            from .activity_clear import is_hidden
            reuse=bool(previous and now-previous['updated']<300)
            if reuse and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='activity_hidden'").fetchone():
                reuse=not is_hidden(db,'event',previous['event_id'])
            total=amount+(previous['count'] if reuse else 0)
            message=f"{total:,} {'image' if total==1 else 'images'} {word}."
            if state=='failed':message+=' These images will retry automatically.'
            if reuse:
                event_id=previous['event_id']
                db.execute('UPDATE activity_events SET message=? WHERE id=?',(message,event_id))
            else:
                event_id=db.execute('INSERT INTO activity_events(created,category,title,state,message) VALUES(?,?,?,?,?)',
                    (now,'YouTube',title,state,message)).lastrowid
            db.execute('INSERT OR REPLACE INTO activity_batches VALUES(?,?,?,?)',(key,event_id,total,now))


def merge_old_artwork(store):
    """Consolidate only the old, recognised success messages; keep all failures."""
    import re
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM settings WHERE key='artwork_activity_merged'").fetchone():return
        rows=db.execute("SELECT * FROM activity_events WHERE title='YouTube artwork' AND state='complete' ORDER BY created,id").fetchall()
        group=None
        for row in rows:
            match=re.fullmatch(r'Finished processing (\d+) images\.',row['message'])
            if not match:continue
            count=int(match[1])
            if group and row['created']-group['updated']<300:
                group['count']+=count;group['updated']=row['created']
                db.execute('DELETE FROM activity_events WHERE id=?',(row['id'],))
            else:group=dict(id=row['id'],count=count,updated=row['created'])
            total=group['count']
            db.execute("UPDATE activity_events SET title='YouTube artwork updated',category='YouTube',message=? WHERE id=?",
                       (f"{total:,} {'image' if total==1 else 'images'} processed.",group['id']))
        db.execute("INSERT INTO settings VALUES('artwork_activity_merged','1')")


def metadata_batch(store, key, count):
    now=time.time()
    title='Emby metadata & artwork' if key=='m3u-exports' else 'M3U metadata & artwork'
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT b.* FROM activity_batches b JOIN activity_events e ON e.id=b.event_id WHERE b.key=?',(key,)).fetchone()
        from .activity_clear import is_hidden
        reuse=bool(old and now-old['updated']<300)
        if reuse and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='activity_hidden'").fetchone():
            reuse=not is_hidden(db,'event',old['event_id'])
        total=count+(old['count'] if reuse else 0)
        message=f"{total:,} metadata {'item' if total==1 else 'items'} processed."
        if reuse:
            identity=old['event_id'];db.execute('UPDATE activity_events SET message=? WHERE id=?',(message,identity))
        else:
            identity=db.execute('INSERT INTO activity_events(created,category,title,state,message) VALUES(?,?,?,?,?)',
                (now,'Emby' if key=='m3u-exports' else 'M3U',title,'complete',message)).lastrowid
        db.execute('INSERT OR REPLACE INTO activity_batches VALUES(?,?,?,?)',(key,identity,total,now))


def merge_old_metadata(store):
    import re
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM settings WHERE key='metadata_activity_merged'").fetchone():return
        for title,pattern,category in [('Emby metadata & artwork',r'Processed (\d+) titles for local metadata sharing\.','Emby'),('M3U metadata & artwork',r'Checked .+\.','M3U')]:
            group=None
            for row in db.execute("SELECT * FROM activity_events WHERE title=? AND state='complete' ORDER BY created,id",(title,)).fetchall():
                match=re.fullmatch(pattern,row['message'])
                if not match:continue
                count=int(match[1]) if category=='Emby' else 1
                if group and row['created']-group['updated']<300:
                    group['count']+=count;group['updated']=row['created']
                    db.execute('DELETE FROM activity_events WHERE id=?',(row['id'],))
                else:group=dict(id=row['id'],count=count,updated=row['created'])
                total=group['count']
                db.execute('UPDATE activity_events SET category=?,message=? WHERE id=?',(category,f"{total:,} metadata {'item' if total==1 else 'items'} processed.",group['id']))
        db.execute("INSERT INTO settings VALUES('metadata_activity_merged','1')")
