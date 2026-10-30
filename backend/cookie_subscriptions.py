"""Add subscriptions discovered with cookies without deleting existing channels."""
import json
import time
from .youtube_fallback import CHANNEL, extract
from .cookies import allow_channel_import
from .watched_status import cookie_session_file_status


def sync(importer):
    store=importer.store
    if not allow_channel_import(store):return
    cookie=store.directory/'youtube-cookies.txt'
    if not cookie.is_file() or float(store.get('youtube_cookie_subscriptions_next','0'))>time.time():return
    store.set('youtube_cookie_subscriptions_next',time.time()+6*3600)
    if cookie_session_file_status(cookie)[0]!='unchecked':
        store.set('youtube_cookie_subscriptions_status','Cookie import needs fresh signed-in cookies. Known channels still use public feeds.')
        return
    try:
        result=extract(importer,'https://www.youtube.com/feed/channels','subscriptions',1,1001)
        if not allow_channel_import(store):return
        entries=result.get('entries',[])
        if not entries or len(entries)>1000 or any(not CHANNEL.fullmatch(e.get('channel_id') or e.get('id') or '') for e in entries):
            raise ValueError('YouTube did not return a usable subscription list. Import a subscriptions CSV or add channel links instead.')
        from .catalogue import thumbnail
        from .download_options import read
        defaults=read(store);added=0
        for entry in entries:
            if not allow_channel_import(store):return
            identity=entry.get('channel_id') or entry['id']
            with store.connect() as db:
                if db.execute('SELECT 1 FROM removed_channels WHERE id=?',(identity,)).fetchone():continue
                if db.execute('SELECT 1 FROM channels WHERE id=?',(identity,)).fetchone():continue
            name=entry.get('channel') or entry.get('title') or identity
            images=[i for i in entry.get('thumbnails',[]) if i.get('url')]
            avatar=thumbnail(store,{'thumbnails':{'medium':{'url':images[-1]['url']}}}) if images else ''
            if not allow_channel_import(store):return
            channel=dict(id=identity,name=name,description=entry.get('description',''),avatar=avatar,
                         playlist='UU'+identity[2:],initials=''.join(w[0] for w in name.split()[:2]),
                         color='#9ee6cf',art='landscape',coverage='Public upload check queued')
            with store.connect() as db:
                db.execute('INSERT OR IGNORE INTO channels VALUES(?,?,1)',(identity,json.dumps(channel)))
                # Keep imported channels if Google is connected to another account later.
                db.execute('INSERT OR IGNORE INTO youtube_manual_channels VALUES(?)',(identity,))
                db.execute('INSERT OR IGNORE INTO download_rules(channel,quality,videos,shorts,live,range_days,enabled_at,quality_override,range_override) VALUES(?,?,?,0,0,?,?,0,0)',
                           (identity,store.get('download_quality','1080'),int(defaults['new_channel_videos']),defaults['default_range'],time.time()))
            added+=1
        store.set('youtube_cookie_subscriptions_status',f'Cookie subscription import checked {len(entries)} channels; {added} added.')
    except Exception as exc:
        from .sync_controls import SyncPaused
        if isinstance(exc,SyncPaused):raise
        store.set('youtube_cookie_subscriptions_status','Cookie subscription import could not finish. Refresh your cookies, import a subscriptions CSV or add channel links. Existing channels are unchanged.')
        store.set('youtube_cookie_subscriptions_next',time.time()+1800)
