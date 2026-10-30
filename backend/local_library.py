"""Read saved download metadata and page over files that still exist."""
import base64
import json
import math
from . import media_library


ENTRY_SQL="""SELECT d.*,v.data AS video_data,c.data AS channel_data,
            coalesce(v.channel,m.channel,'') AS channel_id,m.day,mc.folder,
            EXISTS(SELECT 1 FROM channel_favourites f WHERE f.id=c.id) AS favourite
            FROM downloads d LEFT JOIN videos v ON v.id=d.id LEFT JOIN media_files m ON m.id=d.id
            LEFT JOIN channels c ON c.id=coalesce(v.channel,m.channel)
            LEFT JOIN media_channels mc ON mc.id=coalesce(v.channel,m.channel)"""
PAGE_SIZE=15
BATCH=100
# Upper bound on download rows inspected by one request, so a history full of
# failed or deleted downloads cannot make a single page unbounded.
SCAN_LIMIT=5000


def compose(row,video_id,size):
    channel=json.loads(row['channel_data']) if row['channel_data'] else dict(id=row['channel_id'],name=row['folder'] or 'Saved channel',initials='YT',color='#9ee6cf')
    channel['favourite']=bool(row['favourite'])
    video=json.loads(row['video_data']) if row['video_data'] else dict(id=video_id,title=row['title'],channel=row['channel_id'],date=row['day'] or '',time='',state='published',description='')
    video['downloaded']=bool(size)
    return dict(id=video_id,title=video['title'],video=video,channel_info=channel,channel=channel['id'],channel_name=channel['name'],avatar=channel.get('avatar',''),favourite=channel['favourite'],bytes=size,description=video.get('description',''),created=row['created'],updated=row['updated'])


def entry(store,media,video_id):
    with store.connect() as db:
        row=db.execute(ENTRY_SQL+' WHERE d.id=?',(video_id,)).fetchone()
    if not row:return None
    path=media_library.local_file(store,media,video_id)
    try:size=path.stat().st_size if path else 0
    except OSError:size=0
    return compose(row,video_id,size)


def page(store,media,cursor=''):
    after=None
    if cursor:
        try:
            after=json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if len(after)!=2 or type(after[0]) not in (int,float) or not math.isfinite(after[0]) or not isinstance(after[1],str):raise ValueError()
        except Exception:raise ValueError('Invalid library page.') from None
    items=[];last=after;scanned=0;more=False
    # One connection per request: fetch joined rows in batches and check their
    # files together until a full page of saved videos is found.
    with store.connect() as db:
        while len(items)<PAGE_SIZE and scanned<SCAN_LIMIT:
            rows=db.execute(ENTRY_SQL+(' WHERE (d.created,d.id)<(?,?)' if last else '')+' ORDER BY d.created DESC,d.id DESC LIMIT ?',(*(last or ()),BATCH)).fetchall()
            if not rows:break
            sizes=media_library.file_sizes(store,media,[row['id'] for row in rows],db)
            for row in rows:
                last=[row['created'],row['id']];scanned+=1
                if sizes.get(row['id']):items.append(compose(row,row['id'],sizes[row['id']]))
                if len(items)==PAGE_SIZE:break
            if len(rows)<BATCH and last==[rows[-1]['created'],rows[-1]['id']]:break
        if last and last!=after:
            more=bool(db.execute('SELECT 1 FROM downloads WHERE (created,id)<(?,?) LIMIT 1',last).fetchone())
    next_cursor=base64.urlsafe_b64encode(json.dumps(last).encode()).decode() if more else ''
    return dict(items=items,more=more,cursor=next_cursor)
