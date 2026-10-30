"""Remember subscription identities so local removals require explicit restoration."""
import json
import time


def initialise(store):
    with store.connect() as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS youtube_subscription_seen(
            channel TEXT PRIMARY KEY, subscription TEXT NOT NULL, present INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS youtube_subscription_returns(
            channel TEXT PRIMARY KEY, subscription TEXT NOT NULL, data TEXT NOT NULL,
            state TEXT NOT NULL, detected REAL NOT NULL);""")


def observe(store, items):
    # Called only after every page of an authenticated subscription listing succeeds.
    with store.connect() as db:
        removed={r[0] for r in db.execute('SELECT id FROM removed_channels')}
        old={r['channel']:dict(r) for r in db.execute('SELECT * FROM youtube_subscription_seen')}
        db.execute('UPDATE youtube_subscription_seen SET present=0')
        for item in items:
            sn=item['snippet'];channel=sn['resourceId']['channelId']
            identity=item.get('id') or sn.get('publishedAt') or channel
            previous=old.get(channel)
            if channel in removed and (not previous or not previous['present'] or previous['subscription']!=identity):
                data=dict(id=channel,name=sn.get('title') or channel,avatar='',playlist='UU'+channel[2:],description=sn.get('description',''))
                saved=db.execute('SELECT data FROM channels WHERE id=?',(channel,)).fetchone()
                if saved:data={**data,**json.loads(saved[0])}
                db.execute("INSERT INTO youtube_subscription_returns VALUES(?,?,?,'pending',?) ON CONFLICT(channel) DO UPDATE SET subscription=excluded.subscription,data=excluded.data,state='pending',detected=excluded.detected",(channel,identity,json.dumps(data),time.time()))
                db.execute('INSERT OR IGNORE INTO channels VALUES(?,?,0)',(channel,json.dumps(data)))
            db.execute('INSERT OR REPLACE INTO youtube_subscription_seen VALUES(?,?,1)',(channel,identity))
        db.execute("DELETE FROM youtube_subscription_returns WHERE channel NOT IN (SELECT channel FROM youtube_subscription_seen WHERE present=1)")


def decide(store, channel, subscription, accept):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute("SELECT * FROM youtube_subscription_returns WHERE channel=? AND subscription=? AND state='pending'",(channel,subscription)).fetchone()
        if not row:raise ValueError('This subscription alert changed. Refresh and try again.')
        db.execute("UPDATE youtube_subscription_returns SET state=? WHERE channel=?",('accepted' if accept else 'dismissed',channel))
        if accept:
            db.execute('DELETE FROM removed_channels WHERE id=?',(channel,))
            db.execute('INSERT OR REPLACE INTO channels VALUES(?,?,1)',(channel,row['data']))
            db.execute('INSERT OR IGNORE INTO youtube_manual_channels VALUES(?)',(channel,))
            db.execute("INSERT OR REPLACE INTO settings VALUES('next_sync','0')")
            db.execute("INSERT OR REPLACE INTO settings VALUES('recent_scan_started','0')")
