"""Small, authenticated first-screen preview using server-local catalogue caches.

Never queries a provider or opens a download engine. Full browsing endpoints
remain available; this only removes the initial blank screen after navigation.
"""
import json
import sqlite3
import time
from .discover_sources import load_chart
from .discover_store import preferences

FEEDS = ('trending_movies', 'new_movies', 'trending_shows')
ITEM_FIELDS = frozenset(('id', 'kind', 'title', 'image', 'backdrop', 'releaseDate', 'year', 'provider',
    'externalId', 'imdbId', 'videoId', 'channelId', 'channel', 'description', 'genre', 'rating',
    'url', 'public', 'live', 'rank', 'views', 'dailyViews', 'subscribers', 'growth', 'growthDays',
    'growthChart', 'country', 'published', 'duration'))
GROUP_FIELDS = frozenset(('title', 'scope', 'source', 'sourceUrl', 'updatedAt', 'fetchedAt', 'warning', 'error', 'seed', 'stale'))


def first_page(payload, limit):
    if not isinstance(payload, dict) or not isinstance(payload.get('items'), list): return None
    group = {key: value for key, value in payload.items() if key in GROUP_FIELDS}
    group['items'] = [{key: value for key, value in item.items() if key in ITEM_FIELDS}
                      for item in payload['items'][:limit] if isinstance(item, dict)]
    group.update(partial=len(payload['items']) > limit, preloaded=True)
    return group


def snapshot(store):
    charts, feeds = {}, {}
    with store.connect() as db:
        chart = db.execute("SELECT body,error FROM discover_charts WHERE name='all-time'").fetchone()
        rows = db.execute("SELECT name,body,fetched FROM discover_feeds WHERE name IN (?,?,?)", FEEDS).fetchall()
        prefs = db.execute('SELECT layout,density,quality,fallback,source FROM discover_preferences WHERE id=1').fetchone()
    try:
        payload = json.loads(chart['body']) if chart else load_chart(store, 'all-time')
        if chart and chart['error']:payload.update(error=chart['error'],stale=True)
        group = first_page(payload, 30)
        if group: charts['all-time'] = group
    except (ValueError, OSError): pass
    for row in rows:
        try:
            group = first_page(json.loads(row['body']), 24)
            if group:
                if time.time() - row['fetched'] > 900:
                    group.update(stale=True, warning='Showing cached titles while this collection updates.')
                feeds[row['name']] = group
        except (ValueError, TypeError): continue
    # The View Media testing page has its own NZB indexer cache.
    # Reuse it without requesting providers on the dashboard request thread.
    view_media = None
    from .discovery_engine import default_browse_args, browse_cache_key
    try:
        with store.connect() as db:
            row = db.execute('SELECT body,updated FROM view_media_cache WHERE key=?',
                             (browse_cache_key(default_browse_args(),store),)).fetchone()
        if row:
            data = json.loads(row['body'])
            if isinstance(data.get('items'), list):
                from .view_media.ordering import ordered
                data['items']=ordered(data['items'],'release')
                view_media = dict(data, source='discover', media='movies', loading=False,
                                  refreshing=row['updated'] < time.time()-300)
    except (sqlite3.OperationalError, ValueError, TypeError): pass
    prefs = dict(prefs, fallback=bool(prefs['fallback'])) if prefs else preferences(store)
    return dict(charts=charts, feeds=feeds, preferences=prefs, viewMedia=view_media)
