"""Read-only per-channel history and forecast views for the channel popup."""
import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .discover_logic import redact

CHANNEL = re.compile(r'UC[A-Za-z0-9_-]{22}')
HISTORY_CTE = '''WITH events AS (
 SELECT 'download:'||d.id AS id,'download' AS kind,d.title AS title,d.state AS state,d.message AS message,d.updated AS created
 FROM downloads d JOIN videos v ON v.id=d.id WHERE v.channel=?
 UNION ALL
 SELECT 'management:'||h.id AS id,'management' AS kind,COALESCE(json_extract(v.data,'$.title'),'Video') AS title,'recorded' AS state,h.message AS message,h.created AS created
 FROM managed_history h JOIN videos v ON h.media_key='video:'||v.id OR h.media_key='youtube:video:'||v.id WHERE v.channel=?
 UNION ALL
 SELECT 'channel:'||t.channel AS id,'channel-task' AS kind,t.action AS title,t.state AS state,t.message AS message,t.created AS created
 FROM channel_tasks t WHERE t.channel=?
)'''
FORECASTS = ('SELECT f.data FROM forecasts f JOIN channels c ON c.id=f.channel WHERE c.active=1 AND f.channel=? '
             'AND NOT EXISTS(SELECT 1 FROM channel_preferences p WHERE p.id=c.id AND p.shown=0) '
             'AND NOT EXISTS(SELECT 1 FROM channel_forecast_preferences fp WHERE fp.id=c.id AND fp.enabled=0) '
             'AND f.day BETWEEN ? AND ? ORDER BY f.day,f.id')
VIDEOS = ('SELECT v.data FROM videos v JOIN channels c ON c.id=v.channel WHERE c.active=1 AND v.channel=? '
          'AND v.day BETWEEN ? AND ? ORDER BY v.published DESC,v.id')
CLOCK = re.compile(r'([01][0-9]|2[0-3]):([0-5][0-9])')
FORECAST_FIELDS = ('id', 'channel', 'date', 'time', 'title', 'state', 'description', 'confidence')


def valid_channel(value):
    return isinstance(value, str) and bool(CHANNEL.fullmatch(value))


def history_page(store, channel, limit=20, offset=0):
    params = (channel, channel, channel)
    with store.connect() as db:
        rows = db.execute(HISTORY_CTE + ' SELECT id,kind,title,state,message,created FROM events ORDER BY created DESC,id DESC LIMIT ? OFFSET ?',
                          params + (limit, offset)).fetchall()
        total = db.execute(HISTORY_CTE + ' SELECT COUNT(*) FROM events', params).fetchone()[0]
    items = []
    for row in rows:
        item = dict(row)
        item['message'] = redact(str(item.get('message') or ''))
        items.append(item)
    return dict(items=items, total=total, more=offset + len(items) < total)


def _minutes(clock):
    match = CLOCK.fullmatch(clock) if isinstance(clock, str) else None
    return int(match[1]) * 60 + int(match[2]) if match else None


def _aware(value):
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else None


def forecasts(store, channel, now=None):
    """Display window only (today to +31 days); expired, stale or suppressed rows are dropped."""
    now = now or datetime.now(ZoneInfo('Europe/London'))
    start = now.date()
    end = start + timedelta(days=31)
    if now.timestamp() - float(store.get('forecasts_built', '0') or 0) > 48 * 3600:
        return dict(items=[])
    with store.connect() as db:
        rows = [row[0] for row in db.execute(FORECASTS, (channel, start.isoformat(), end.isoformat()))]
        videos = [row[0] for row in db.execute(VIDEOS, (channel, start.isoformat(), end.isoformat()))]
    real = []
    for raw in videos:
        try:
            video = json.loads(raw)
        except (TypeError, ValueError):
            continue
        minutes = _minutes(video.get('time')) if isinstance(video, dict) else None
        if minutes is not None and video.get('channel') == channel:
            real.append((video.get('date'), minutes))
    items = []
    for raw in rows:
        try:
            item = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(item, dict) or item.get('channel') != channel or item.get('state') != 'expected':
            continue
        minutes = _minutes(item.get('time'))
        ends = _aware(item.get('window_end'))
        if minutes is None or ends is None or ends.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
            continue
        if any(day == item.get('date') and abs(at - minutes) <= 180 for day, at in real):
            continue
        safe = {key: item.get(key, '') for key in FORECAST_FIELDS}
        safe['description'] = redact(str(safe['description'] or ''))
        items.append(safe)
    return dict(items=items)
