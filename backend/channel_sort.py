"""Channel ranking values from the saved catalogue, independent of the visible dates."""
import time


def add_sort_metrics(store, channels):
    now = time.time()
    with store.connect() as db:
        rows = db.execute("""
            SELECT channel,
              MAX(CASE WHEN json_extract(data,'$.state')='published' AND unixepoch(published)<=?
                  THEN unixepoch(published) END) latest,
              SUM(CASE WHEN json_extract(data,'$.state')='published'
                  AND unixepoch(published) BETWEEN ? AND ? THEN 1 ELSE 0 END) activity,
              MIN(CASE WHEN json_extract(data,'$.state')='scheduled' AND unixepoch(published)>=?
                  THEN unixepoch(published) END) upcoming
            FROM videos GROUP BY channel
        """, (now, now-30*86400, now, now)).fetchall()
        metrics = {r['channel']:dict(latest=r['latest'], activity=r['activity'], upcoming=r['upcoming']) for r in rows}
        if now-float(store.get('forecasts_built', '0')) <= 48*3600:
            for row in db.execute("""
                SELECT f.channel, MIN(unixepoch(json_extract(f.data,'$.event_at'))) upcoming
                FROM forecasts f LEFT JOIN channel_forecast_preferences p ON p.id=f.channel
                WHERE COALESCE(p.enabled,1)=1 AND unixepoch(json_extract(f.data,'$.event_at'))>=?
                GROUP BY f.channel
            """, (now,)):
                values = metrics.setdefault(row['channel'],dict(latest=None, activity=0, upcoming=None))
                if values['upcoming'] is None or row['upcoming'] < values['upcoming']:
                    values['upcoming'] = row['upcoming']
    for channel in channels:
        values = metrics.get(channel['id'], dict(latest=None, activity=0, upcoming=None)).copy()
        # An unchecked channel's activity is unknown, rather than zero uploads.
        if not channel.get('last_checked'):
            values['activity'] = None
        channel['sort_metrics'] = values
    return channels
