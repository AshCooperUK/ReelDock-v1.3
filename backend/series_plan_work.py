"""Durable, bounded enrollment of an explicitly enabled all-seasons plan.

Only startup creates this ledger. Exact keys, rather than catalogue offsets,
survive both process loss and episode information arriving out of order.
"""
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

BATCH_SIZE = 10
RUN_SECONDS = 5
LEASE_SECONDS = 120


class PlanDeferred(ValueError):
    """The snapshot lost its ownership or eligibility; retry from saved state."""


def initialise(store):
    with store.connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS discover_plan_work(
            parent TEXT PRIMARY KEY,revision INTEGER NOT NULL,
            all_before REAL NOT NULL,
            lease_token TEXT NOT NULL DEFAULT '',lease_until REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS discover_plan_entries(
            parent TEXT NOT NULL,revision INTEGER NOT NULL,key TEXT NOT NULL,
            PRIMARY KEY(parent,revision,key));
        CREATE INDEX IF NOT EXISTS discover_plan_request_episode ON discover_requests(episode_key);
        CREATE TRIGGER IF NOT EXISTS discover_plan_insert AFTER INSERT ON discover_series_plan
        BEGIN
            INSERT INTO discover_plan_work(parent,revision,all_before)
                VALUES(NEW.parent,1,CAST(strftime('%s','now') AS REAL))
            ON CONFLICT(parent) DO UPDATE SET revision=revision+1,
                all_before=excluded.all_before,lease_token='',lease_until=0;
        END;
        CREATE TRIGGER IF NOT EXISTS discover_plan_update
        AFTER UPDATE OF item,download_all,download_future,future_from ON discover_series_plan
        BEGIN
            INSERT INTO discover_plan_work(parent,revision,all_before)
                VALUES(NEW.parent,1,CAST(strftime('%s','now') AS REAL))
            ON CONFLICT(parent) DO UPDATE SET revision=revision+1,
                all_before=CASE WHEN OLD.download_all=0 AND NEW.download_all=1
                    THEN excluded.all_before ELSE all_before END,
                lease_token='',lease_until=0;
        END;
        CREATE TRIGGER IF NOT EXISTS discover_plan_delete AFTER DELETE ON discover_series_plan
        BEGIN
            UPDATE discover_plan_work SET revision=revision+1,lease_token='',lease_until=0
            WHERE parent=OLD.parent;
        END;
        ''')
        db.execute('BEGIN IMMEDIATE')
        # The old future refresh also stamped checked; it cannot prove that ALL
        # finished. Preserve the latest evidenced exact ALL pass where available.
        # Otherwise adoption time is the only recoverable historical boundary.
        db.execute('''INSERT OR IGNORE INTO discover_plan_work(parent,revision,all_before)
            SELECT p.parent,1,min(coalesce((SELECT max(r.created) FROM discover_requests r
                WHERE json_extract(r.body,'$.planParent')=p.parent
                AND json_extract(r.body,'$.plan')='all'),?),?)
            FROM discover_series_plan p''',(time.time(),time.time()))


def _all_day(timestamp):
    return datetime.fromtimestamp(timestamp,ZoneInfo('Europe/London')).date().isoformat()


@contextmanager
def _connect(store):
    with store.connect() as db:
        db.create_function('reeldock_all_day',1,_all_day,deterministic=True)
        yield db


def _eligible(alias='m'):
    day=f"json_extract({alias}.body,'$.date')"
    return f'''{alias}.parent=p.parent AND {alias}.kind='episode' AND {alias}.removed=0
        AND coalesce(json_extract({alias}.body,'$.directTitleKey'),'')=''
        AND {day} IS NOT NULL AND {day}!=''
        AND json_extract({alias}.body,'$.episode.season') IS NOT NULL
        AND ((p.download_all=1 AND {day}<=reeldock_all_day(w.all_before))
             OR (p.download_future=1 AND {day}>=p.future_from))'''


def _pending():
    return f'''EXISTS(SELECT 1 FROM managed_media m WHERE {_eligible()}
        AND NOT EXISTS(SELECT 1 FROM discover_plan_entries e
            WHERE e.parent=p.parent AND e.revision=w.revision AND e.key=m.key))'''


def pending_parent(store, today, parent=None, *, include_leased=False):
    """A read also finds episodes enrolled after an earlier completed pass."""
    with _connect(store) as db:
        row=db.execute(f'''SELECT p.* FROM discover_series_plan p
            JOIN discover_plan_work w ON w.parent=p.parent
            WHERE p.download_all=1 AND (p.checked=0 OR {_pending()})
            AND (? OR w.lease_until<=?) {"AND p.parent=?" if parent else ""}
            ORDER BY p.checked,p.parent LIMIT 1''',
            (int(include_leased),time.time(),parent) if parent else (int(include_leased),time.time())).fetchone()
    return dict(row) if row else None


def _settings(db):
    values=dict(db.execute("SELECT key,value FROM settings WHERE key IN ('acquisition_generation','acquisition_hold','reset_in_progress')"))
    return str(values.get('acquisition_generation') or '0'),values.get('acquisition_hold')=='1' or values.get('reset_in_progress')=='1'


def claim(store, parent):
    from .acquisition import held
    if held(store) or store.reset_pending():return None
    now=time.time();token=uuid.uuid4().hex
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('''SELECT p.*,w.revision,w.lease_until,w.all_before FROM discover_series_plan p
            JOIN discover_plan_work w ON w.parent=p.parent WHERE p.parent=?''',(parent,)).fetchone()
        generation,blocked=_settings(db)
        if not row or blocked or not (row['download_all'] or row['download_future']) or row['lease_until']>now:return None
        db.execute('UPDATE discover_plan_work SET lease_token=?,lease_until=? WHERE parent=? AND revision=?',
                   (token,now+LEASE_SECONDS,parent,row['revision']))
    return {**dict(row),'token':token,'generation':generation}


def valid_in(db, claim, key=None, scope=None, today=None):
    row=db.execute('''SELECT p.*,w.revision,w.lease_token,w.lease_until,w.all_before FROM discover_series_plan p
        JOIN discover_plan_work w ON w.parent=p.parent WHERE p.parent=?''',(claim['parent'],)).fetchone()
    generation,blocked=_settings(db)
    if (not row or blocked or generation!=claim['generation'] or row['revision']!=claim['revision']
            or row['lease_token']!=claim['token'] or row['lease_until']<=time.time()):return False
    if key is not None:
        media=db.execute('SELECT body,removed FROM managed_media WHERE key=? AND parent=?',(key,claim['parent'])).fetchone()
        if not media or media['removed']:return False
        import json
        body=json.loads(media['body']);day=body.get('date','')
        if not day or body.get('episode',{}).get('season') is None or body.get('directTitleKey'):return False
        actual='future' if row['download_future'] and day>=row['future_from'] else 'all' if row['download_all'] and day<=_all_day(row['all_before']) else ''
        if actual!=scope:return False
    return True


def alive(store, claim):
    from .acquisition import held
    if held(store) or store.reset_pending():return False
    with _connect(store) as db:
        db.execute('BEGIN IMMEDIATE')
        if not valid_in(db,claim):return False
        db.execute('UPDATE discover_plan_work SET lease_until=? WHERE parent=? AND lease_token=?',
                   (time.time()+LEASE_SECONDS,claim['parent'],claim['token']))
    return True


def batch(store, claim, today):
    with _connect(store) as db:
        db.execute('BEGIN IMMEDIATE')
        if not valid_in(db,claim):return []
        # A restarted initial pass must not re-attempt an earlier failed or
        # cancelled ALL request. Its explicit Retry/Download action owns that.
        db.execute(f'''INSERT OR IGNORE INTO discover_plan_entries(parent,revision,key)
            SELECT p.parent,w.revision,m.key FROM discover_series_plan p
            JOIN discover_plan_work w ON w.parent=p.parent
            JOIN managed_media m ON m.parent=p.parent
            WHERE p.parent=? AND {_eligible()} AND EXISTS(
                SELECT 1 FROM discover_requests r WHERE r.episode_key=m.key
                AND json_extract(r.body,'$.planParent')=p.parent
                AND json_extract(r.body,'$.plan') IN ('all','future'))''',(claim['parent'],))
        rows=db.execute(f'''SELECT m.* FROM discover_series_plan p
            JOIN discover_plan_work w ON w.parent=p.parent
            JOIN managed_media m ON m.parent=p.parent
            WHERE p.parent=? AND {_eligible()} AND NOT EXISTS(
                SELECT 1 FROM discover_plan_entries e WHERE e.parent=p.parent
                AND e.revision=w.revision AND e.key=m.key)
            ORDER BY m.key LIMIT ?''',(claim['parent'],BATCH_SIZE)).fetchall()
    return [dict(row) for row in rows]


def consumed(store, claim, key):
    with _connect(store) as db:
        db.execute('BEGIN IMMEDIATE')
        if not valid_in(db,claim):return False
        db.execute('INSERT OR IGNORE INTO discover_plan_entries VALUES(?,?,?)',(claim['parent'],claim['revision'],key))
    return True


def finish(store, claim, today, *, complete, all_work=True):
    with _connect(store) as db:
        db.execute('BEGIN IMMEDIATE')
        if not valid_in(db,claim):return False
        if complete and all_work:
            # Recheck the live catalogue under the writer lock; enrollment may
            # have added keys since the last batch was read.
            pending=db.execute(f'''SELECT {_pending()} FROM discover_series_plan p
                JOIN discover_plan_work w ON w.parent=p.parent WHERE p.parent=?''',(claim['parent'],)).fetchone()[0]
            if pending:complete=False
        if complete:db.execute('UPDATE discover_series_plan SET checked=? WHERE parent=?',(time.time(),claim['parent']))
        db.execute("UPDATE discover_plan_work SET lease_token='',lease_until=0 WHERE parent=? AND revision=? AND lease_token=?",
                   (claim['parent'],claim['revision'],claim['token']))
    return complete


def release(store, claim):
    """An old owner can release only its own lease, even after a plan edit."""
    with store.connect() as db:
        db.execute("UPDATE discover_plan_work SET lease_token='',lease_until=0 WHERE parent=? AND revision=? AND lease_token=?",
                   (claim['parent'],claim['revision'],claim['token']))
