"""Collect a bounded global search once, then page its immutable sorted results."""
import json
import re
import time
import uuid
from datetime import datetime, timezone

from . import global_search
from .adapters import PAGE
from .cache_lock import refresh_lock
from .ordering import ordered
from .providers import initialise

BATCHES = 3
TTL = 300
RETENTION = 86400
PREFIX = 'engine:search-snapshot:v1:'
SCOPE = ('Fixed search window: up to 300 NZB releases per indexer and media type, '
         '72 VOD titles per media type, 72 local files and 60 YouTube results. '
         'Sorting covers the whole window. Narrow your search for other matches.')


def validate_cursor(raw, offset, order):
    if not raw:
        if offset != 0:
            raise ValueError('Invalid search page. Search again.')
        return None
    try:
        cursor = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError('Invalid search page.') from None
    if (not isinstance(cursor, dict) or set(cursor) != {'snapshot', 'order'}
            or not isinstance(cursor['snapshot'], str)
            or not re.fullmatch(r'[a-f0-9]{32}', cursor['snapshot'])
            or cursor['order'] != order or offset <= 0 or offset % PAGE):
        raise ValueError('Invalid search page. Search again.')
    return cursor


def collect(app, store, args):
    items, warnings = [], []
    cursor = None
    incomplete = scanning = False
    for batch in range(BATCHES):
        if args.get('_checkpoint') and not args['_checkpoint']():break
        page = global_search.browse(app, store, dict(args, offset=batch, cursor=cursor, _partial=None))
        items.extend(page['items'])
        warnings.extend(w.replace('Retrying automatically.', 'Search again to retry.') for w in page.get('warnings', []))
        incomplete |= bool(page.get('incomplete'))
        scanning |= bool(page.get('scanning'))
        cursor = json.loads(page['nextCursor']) if page.get('nextCursor') else None
        if not cursor:
            break
    return dict(items=global_search.merge(items), warnings=list(dict.fromkeys(warnings)),
                incomplete=incomplete, scanning=scanning, windowLimited=bool(cursor),
                freshness=dict(source='Global search', fetchedAt=datetime.now(timezone.utc).isoformat(), scope=SCOPE))


def _page(saved, identity, args):
    # Sort the complete window, then group, then slice. No later provider batch
    # can insert a card before a page the user has already read.
    items = ordered(saved['items'], 'new' if args['sort'] == 'source' else args['sort'])
    def group(item):
        if item['kind'] in ('video', 'youtube', 'channel') or item['action']['type'] == 'youtube':
            return 2
        return 1 if item['kind'] in ('show', 'shows', 'episode', 'series', 'tv') else 0
    items.sort(key=group)
    offset = args['offset']
    following = offset + PAGE if not args.get('grouped') and offset + PAGE < len(items) else None
    return dict(items=items if args.get('grouped') else items[offset:offset + PAGE], nextOffset=following,
                nextCursor=json.dumps(dict(snapshot=identity, order=args['sort'])) if following is not None else None,
                searchSnapshot=identity, label='Search results', collection='',
                warnings=saved.get('warnings', []), incomplete=saved.get('incomplete', False),
                freshness=saved['freshness'], loading=False, refreshing=False)


def cached_browse(app, store, args):
    from .. import discovery_engine as engine
    initialise(store)
    canonical = dict({key:value for key,value in args.items() if key!='grouped'}, offset=0, cursor=None, sort='source')
    key = engine.browse_cache_key(canonical, store)
    cursor = args.get('cursor')
    now = time.time()
    if cursor:
        with store.connect() as db:
            row = db.execute('SELECT body,updated FROM view_media_cache WHERE key=?', (PREFIX + cursor['snapshot'],)).fetchone()
        if not row or row['updated'] < now - RETENTION:
            raise ValueError('This search has expired. Search again.')
        saved = json.loads(row['body'])
        if saved['searchKey'] != key:
            raise ValueError('This search has changed. Search again.')
        return _page(saved, cursor['snapshot'], args)

    with store.connect() as db:
        row = db.execute('SELECT body,updated FROM view_media_cache WHERE key=?', (key,)).fetchone()
    saved = json.loads(row['body']) if row else {}
    due = saved.get('retryAt', row['updated'] + TTL if row else 0)
    if row and due > now and not saved.get('scanning'):
        return _page(saved, saved['snapshot'], args)

    def load():
        with refresh_lock(store, key):
            with store.connect() as db:
                latest = db.execute('SELECT body,updated FROM view_media_cache WHERE key=?', (key,)).fetchone()
            prior = json.loads(latest['body']) if latest else {}
            if latest and prior.get('retryAt', latest['updated'] + TTL) > time.time():
                return
            result = collect(app, store, canonical)
            stamp = time.time()
            identity = uuid.uuid4().hex
            result.update(searchKey=key, snapshot=identity)
            if result['scanning']:
                result['retryAt'] = stamp + 2
            elif result['incomplete']:
                result['retryAt'] = stamp + 60
            body = json.dumps(result)
            with store.connect() as db:
                db.execute('INSERT OR REPLACE INTO view_media_cache VALUES(?,?,?)', (key, body, stamp))
                if not result['scanning']:
                    db.execute('INSERT INTO view_media_cache VALUES(?,?,?)', (PREFIX + identity, body, stamp))
                db.execute('DELETE FROM view_media_cache WHERE key LIKE ? AND updated<?', (PREFIX + '%', stamp - RETENTION))

    if due <= now:
        engine.schedule(app, store, key, load)
    with engine._LOCK:
        error = engine._ERRORS.get((str(store.directory), key), '')
    return dict(items=[], nextOffset=None, label='Search results', collection='',
                loading=not bool(error), refreshing=False, error=error,
                freshness=dict(source='Global search', scope=SCOPE))
