"""Additive managed-media records; legacy rules/requests/ownership remain authoritative."""
import json
import re
import time

def initialise(store):
    with store.connect() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS managed_media (
            key TEXT PRIMARY KEY, parent TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
            monitor INTEGER NOT NULL DEFAULT -1, removed INTEGER NOT NULL DEFAULT 0,
            hold INTEGER NOT NULL DEFAULT 0, quality TEXT NOT NULL DEFAULT '1080',
            fallback INTEGER NOT NULL DEFAULT 0, last_check REAL NOT NULL DEFAULT 0,
            next_check REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '', updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS managed_series (
            parent TEXT PRIMARY KEY, show_id TEXT NOT NULL, quality TEXT NOT NULL,
            fallback INTEGER NOT NULL, monitoring TEXT NOT NULL DEFAULT 'inherit');
        CREATE TABLE IF NOT EXISTS managed_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, media_key TEXT NOT NULL, message TEXT NOT NULL, created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS managed_parent ON managed_media(parent);
        ''')
    reconcile(store)

LOCAL_FIELDS=('displayTitle','fileQuality','localPath','renameJournal')

def _survivor(rows):
    """Explicit user state wins over freshness: removal, then a hold, then a
    deliberate monitor override, then the original (most-linked) record."""
    return sorted(rows,key=lambda r:(not r['removed'],not r['hold'],r['monitor']==-1,r['updated'],r['key']))[0]

def _ordered(rows):
    return sorted(rows,key=lambda r:(not r['removed'],not r['hold'],r['monitor']==-1,r['updated'],r['key']))

def _alias_rows(db,key,parent,kind,body):
    """Existing records whose verified catalogue identity is the same title.
    Titles/years alone never qualify; local records have no catalogue identity."""
    if key.startswith('local:'):return []
    if kind=='movie':
        item=body.get('item') or {}
        external=str(item.get('externalId') or '')
        forms={str(parent),str(item.get('id') or '')}
        if external:forms|={'movie:'+external,'imdb:movie:'+external}
        forms.discard('')
        marks=','.join('?' for _ in forms)
        rows=[dict(r) for r in db.execute(
            f"SELECT * FROM managed_media WHERE kind='movie' AND key!=? AND (parent IN ({marks}) OR json_extract(body,'$.item.id') IN ({marks}))",
            (key,*forms,*forms)).fetchall()]
        if external:
            from ..discover_logic import same_external_id
            for r in db.execute("SELECT * FROM managed_media WHERE kind='movie' AND key!=? AND json_extract(body,'$.item.externalId') IS NOT NULL",(key,)).fetchall():
                other=json.loads(r['body']).get('item',{}).get('externalId')
                if same_external_id(external,other):
                    if r['key'] not in {found['key'] for found in rows}:rows.append(dict(r))
        return _ordered(rows)
    if kind=='episode':
        episode=body.get('episode') or {}
        if type(episode.get('season')) is not int or type(episode.get('number')) is not int:return []
        return _ordered(dict(r) for r in db.execute(
            "SELECT * FROM managed_media WHERE kind='episode' AND key!=? AND parent=? AND json_extract(body,'$.episode.season')=? AND json_extract(body,'$.episode.number')=?",
            (key,parent,episode['season'],episode['number'])).fetchall())
    return []

def _merged_body(survivor,losers,incoming):
    """The freshest record body wins; file-local fields are unioned, preferring
    the surviving record so a rename/cache saved on it is never lost."""
    base=dict(incoming) if incoming is not None else dict(json.loads(max([survivor,*losers],key=lambda r:r['updated'])['body']))
    for name in LOCAL_FIELDS:
        chosen=None
        for r in [survivor,*sorted(losers,key=lambda r:-r['updated'])]:
            value=json.loads(r['body']).get(name)
            if value is not None:chosen=value;break
        if chosen is None:base.pop(name,None)
        else:base[name]=chosen
    return base

def _repoint(db,survivor,loser):
    """Move key references from a folded record to its survivor. Open requests
    never duplicate: the unique open-request index on episode_key is honoured
    by superseding the extra request instead of repointing it."""
    survivor_key,loser_key=survivor['key'],loser['key']
    db.execute('UPDATE managed_history SET media_key=? WHERE media_key=?',(survivor_key,loser_key))
    db.execute('UPDATE OR IGNORE discover_owned SET key=? WHERE key=?',(survivor_key,loser_key))
    db.execute('DELETE FROM discover_owned WHERE key=?',(loser_key,))
    for table in ('discover_watchlist','movie_options'):
        if db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():
            column='id' if table=='discover_watchlist' else 'key'
            db.execute(f'UPDATE OR IGNORE {table} SET {column}=? WHERE {column}=?',(survivor_key,loser_key))
            db.execute(f'DELETE FROM {table} WHERE {column}=?',(loser_key,))
    open_loser=db.execute("SELECT 1 FROM discover_requests WHERE episode_key=? AND state NOT IN ('failed','cancelled')",(loser_key,)).fetchone()
    survivor_open=db.execute("SELECT 1 FROM discover_requests WHERE episode_key=? AND state NOT IN ('failed','cancelled')",(survivor_key,)).fetchone()
    if open_loser and (survivor_open or survivor['hold'] or survivor['removed']):
        db.execute("UPDATE discover_requests SET state='cancelled',message='Superseded by management reconciliation; saved file retained.' WHERE episode_key=? AND state NOT IN ('failed','cancelled')",(loser_key,))
    else:
        db.execute("UPDATE discover_requests SET episode_key=? WHERE episode_key=? AND state NOT IN ('failed','cancelled')",(survivor_key,loser_key))
    db.execute("UPDATE discover_requests SET episode_key=? WHERE episode_key=? AND state IN ('failed','cancelled','complete')",(survivor_key,loser_key))
    db.execute('DELETE FROM managed_media WHERE key=?',(loser_key,))

def _merge(db,survivor,losers,incoming=None):
    body=_merged_body(survivor,losers,incoming)
    for loser in losers:_repoint(db,survivor,loser)
    db.execute('UPDATE managed_media SET body=?,updated=? WHERE key=?',(json.dumps(body),time.time(),survivor['key']))
    return survivor['key']

def remember(store,key,parent,kind,body):
    """Insert or refresh a managed record and return the effective key.
    When the same verified title was already enrolled under another key
    (metadata enrichment changes movie:/tmdb: forms), fold this record into the
    existing one so one movie keeps one entry, one hold and one history."""
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        # Preserve local fields in the upsert itself, so concurrent enrolment
        # cannot overwrite a rename/cache saved after a preliminary read.
        exists=db.execute('SELECT 1 FROM managed_media WHERE key=?',(key,)).fetchone()
        if exists:
            db.execute('''UPDATE managed_media SET
                body=json_set(?,'$.displayTitle',json_extract(managed_media.body,'$.displayTitle'),
                              '$.fileQuality',json_extract(managed_media.body,'$.fileQuality'),
                              '$.localPath',COALESCE(json_extract(managed_media.body,'$.localPath'),json_extract(?,'$.localPath'))),
                parent=?,updated=? WHERE key=?''',
                (json.dumps(body),json.dumps(body),parent,time.time(),key))
            return key
        aliases=_alias_rows(db,key,parent,kind,body)
        if aliases:
            survivor=_survivor(aliases)
            return _merge(db,survivor,[r for r in aliases if r['key']!=survivor['key']],incoming=body)
        db.execute('INSERT INTO managed_media(key,parent,kind,body,updated) VALUES(?,?,?,?,?)',
            (key,parent,kind,json.dumps(body),time.time()))
        return key

def reconcile(store):
    """Repair pre-existing duplicate records that predate alias-aware enrolment.
    Groups by verified catalogue identity only; idempotent under web/worker."""
    merged=0
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        movies=[dict(r) for r in db.execute("SELECT * FROM managed_media WHERE kind='movie' AND key NOT LIKE 'local:%'").fetchall()]
        groups=[]
        for r in movies:
            b=json.loads(r['body']);item=b.get('item') or {}
            identities={str(r['parent']),str(item.get('id') or '')}
            external=str(item.get('externalId') or '')
            if external:identities|={external,'movie:'+external,'imdb:movie:'+external}
            match=re.fullmatch(r'(?:movie:|imdb:movie:)(\d{1,12})',r['key'])
            if match:identities|={match[1],'movie:'+match[1]}
            identities.discard('')
            found=[g for g in groups if identities & g['identities']]
            if found:
                g=found[0]
                for extra in found[1:]:
                    g['rows']+=extra['rows'];g['identities']|=extra['identities'];groups.remove(extra)
                g['rows'].append(r);g['identities']|=identities
            else:groups.append(dict(identities=identities,rows=[r]))
        for g in groups:
            if len(g['rows'])<2:continue
            survivor=_survivor(g['rows'])
            losers=[r for r in g['rows'] if r['key']!=survivor['key']]
            _merge(db,survivor,losers);merged+=len(losers)
        episodes={}
        for r in db.execute("SELECT * FROM managed_media WHERE kind='episode'").fetchall():
            e=json.loads(r['body']).get('episode') or {}
            if type(e.get('season')) is not int or type(e.get('number')) is not int:continue
            episodes.setdefault((r['parent'],e['season'],e['number']),[]).append(dict(r))
        for rows in episodes.values():
            if len(rows)<2:continue
            survivor=_survivor(rows)
            losers=[r for r in rows if r['key']!=survivor['key']]
            _merge(db,survivor,losers);merged+=len(losers)
    return merged

def patch_body(store,key,**values):
    """Merge additive JSON fields without overwriting a concurrent policy/cache save."""
    with store.connect() as db:
        for name,value in values.items():
            if name not in ('displayTitle','fileQuality','renameJournal','localPath'):raise ValueError('Unknown managed file field.')
            if value is None:
                db.execute('UPDATE managed_media SET body=json_remove(body,?) WHERE key=?',('$.'+name,key))
            else:
                db.execute('UPDATE managed_media SET body=json_set(body,?,json(?)) WHERE key=?',
                           ('$.'+name,json.dumps(value),key))

def insert_replacement(store,r,payload):
    """A saved ownership row is retained; only this explicit request bypasses it."""
    import uuid
    from ..acquisition import ACTIVE_DISCOVER,ensure_open
    ensure_open(store)
    now=time.time();identity=str(uuid.uuid4())
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current=db.execute('SELECT hold,removed FROM managed_media WHERE key=?',(r['key'],)).fetchone()
        if not current or current['hold'] or current['removed']:raise ValueError('Resume this managed title before replacing its file.')
        pending=db.execute('SELECT state FROM discover_requests WHERE episode_key=?',(r['key'],)).fetchall()
        if any(q['state'] in ACTIVE_DISCOVER or q['state']=='importing' for q in pending):
            raise ValueError('An acquisition is already active.')
        # The existing unique open-request index includes completed requests.
        # Retire their queue slot, retaining ownership, attempts and saved files.
        db.execute("UPDATE discover_requests SET state='cancelled',message='Superseded by explicit replacement; saved file retained.' WHERE episode_key=? AND state IN ('complete','waiting')",(r['key'],))
        db.execute('''INSERT INTO discover_requests
            (id,idempotency,kind,title,state,quality,fallback,body,manual,episode_key,updated,created)
            VALUES(?,?,?,?,'searching',?,?,?,1,?,?,?)''',
            (identity,identity,r['kind'],payload['item']['title'],r['quality'],r['fallback'],json.dumps(payload),r['key'],now,now))
        return dict(db.execute('SELECT * FROM discover_requests WHERE id=?',(identity,)).fetchone())

def row(store,key):
    with store.connect() as db:
        r=db.execute('SELECT * FROM managed_media WHERE key=?',(key,)).fetchone()
    return dict(r) if r else None

def locate(store,key,parent,kind,body):
    """Resolve a record whose key may have been folded into an alias survivor."""
    r=row(store,key)
    if r:return r
    with store.connect() as db:
        candidates=_alias_rows(db,key,parent,kind,body)
    return candidates[0] if candidates else None

def policy_blocks(store,key,automatic=True):
    # Called from established acquisition paths too; no schema work in engine hot paths.
    from ..media_holds import blocked
    if blocked(store,'managed:'+key):return True
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='managed_media'").fetchone():return False
        r=db.execute('SELECT monitor,hold,removed,parent FROM managed_media WHERE key=?',(key,)).fetchone()
    policy=series_for_key(store,key,r)
    return bool((automatic and policy and policy['monitoring']=='off') or (r and (r['removed'] or r['hold'] or (automatic and r['monitor']==0))))

def history(store,key,message):
    with store.connect() as db:db.execute('INSERT INTO managed_history(media_key,message,created) VALUES(?,?,?)',(key,message,time.time()))


def overrides_monitor(store,key):
    """Explicit per-item policies are scheduled by management, not the parent rule."""
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='managed_media'").fetchone():return False
        r=db.execute('SELECT monitor,hold,removed,parent FROM managed_media WHERE key=?',(key,)).fetchone()
    return bool(series_for_key(store,key,r) or (r and (r['monitor']!=-1 or r['hold'] or r['removed'])))


def series_for_key(store,key,r=None):
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='managed_series'").fetchone():return None
        result=db.execute('SELECT * FROM managed_series WHERE parent=?',(r['parent'],)).fetchone() if r else None
        if result is None and key.startswith('show:'):
            result=db.execute('SELECT * FROM managed_series WHERE show_id=?',(key.split(':')[1],)).fetchone()
    return dict(result) if result else None
