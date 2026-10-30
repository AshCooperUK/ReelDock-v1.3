"""Keep request counters with the OAuth client that incurred them."""
import hashlib
import json

KEYS = ('quota_day', 'quota_used', 'history_quota_day', 'history_quota_used')


def bind(db, client_id):
    identity = hashlib.sha256(client_id.encode()).hexdigest()
    row = db.execute("SELECT value FROM settings WHERE key='quota_client'").fetchone()
    previous = row[0] if row else ''
    if previous == identity:
        return
    if previous:
        values = dict(db.execute("SELECT key,value FROM settings WHERE key IN (?,?,?,?)", KEYS))
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('quota_archive:'+previous,json.dumps(values)))
        saved = db.execute('SELECT value FROM settings WHERE key=?', ('quota_archive:'+identity,)).fetchone()
        restored = json.loads(saved[0]) if saved else {}
        db.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)', [(key,restored.get(key,'')) for key in KEYS])
    elif db.execute("SELECT 1 FROM settings WHERE key='quota_used'").fetchone():
        db.execute("INSERT OR REPLACE INTO settings VALUES ('quota_legacy_unattributed','1')")
    # Existing installations retain their counters on first migration.
    db.execute("INSERT OR REPLACE INTO settings VALUES ('quota_client',?)", (identity,))


def client_id(app, store):
    return app.config['GOOGLE_CLIENT_ID'] or store.get('google_client_id')


def clear_legacy_pause(store):
    """A removed local ceiling must not leave an upgraded installation paused."""
    with store.connect() as db:
        for key in ('sync_error','sync_message','history_message'):
            row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
            if row and ('Daily import allowance reached' in row[0] or 'History allowance reached' in row[0]):
                db.execute('UPDATE settings SET value=? WHERE key=?',('',key))
                schedule='next_history' if key=='history_message' else 'next_sync'
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(schedule,'0'))


def next_reset():
    """YouTube daily quota resets at midnight Pacific, including DST changes."""
    from datetime import datetime, timedelta, time
    from zoneinfo import ZoneInfo
    zone=ZoneInfo('America/Los_Angeles')
    tomorrow=datetime.now(zone).date()+timedelta(days=1)
    return datetime.combine(tomorrow,time(0,5),zone).timestamp()


def status(store):
    """Local status only: rendering the footer must not spend API quota."""
    import time
    now=time.time()
    retry=float(store.get('google_quota_retry_at','0') or 0)
    state=('off' if store.get('youtube_use_google_api')=='false' else
           'disconnected' if not store.get('google_tokens') else
           'limited' if retry>now else 'ready')
    # Google resets at midnight; ReelDock allows five minutes before retrying.
    reset=retry-300 if retry>now else next_reset()-300
    return dict(state=state,reset_at=reset,retry_at=retry if retry>now else 0,server_time=now)
