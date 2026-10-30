"""Future-season checks run in the worker, not in the browser.

The boundary recorded on first activation is kept across polls, pauses and
restarts. A legacy scope of one season is not widened unless the future
checkbox is turned on.
"""
import json
import time
from datetime import date

from .discover_acquire import process
from .discover_logic import boundary_season, classify_episodes, episode_identity
from .discover_store import initialise, insert_request, open_request_for, owned, rules, update_request

INTERVAL = 15 * 60
LEASE = 180


def today():
    return date.today().isoformat()


def save_rule(store, payload, episodes):
    """Autosave the single future-season checkbox and the shared quality settings."""
    initialise(store)
    show_id = str(payload.get('showId') or '')
    if not show_id.isdigit():
        raise ValueError('This show has no TVDB identity.')
    quality = str(payload.get('quality') or '')
    if quality not in ('720', '1080', '2160'):
        raise ValueError('Choose 720p, 1080p or 2160p.')
    if type(payload.get('fallback')) is not bool or type(payload.get('enabled')) is not bool:
        raise ValueError('Choose whether future seasons should download.')
    item = payload.get('item') if isinstance(payload.get('item'), dict) else {'id': 'show:' + show_id, 'kind': 'show', 'title': payload.get('title') or 'TV show', 'externalId': show_id}
    from .discover_store import clean_item
    item = clean_item({**item, 'kind': 'show', 'externalId': show_id, 'id': item.get('id') or 'show:' + show_id})
    enabled = payload['enabled']
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT * FROM discover_rules WHERE show_id=?', (show_id,)).fetchone()
        if not enabled:
            if not existing:
                return None
            db.execute('UPDATE discover_rules SET enabled=0, quality=?, fallback=?, updated=? WHERE id=?',
                       (quality, int(payload['fallback']), time.time(), existing['id']))
        else:
            if existing and existing['boundary_locked'] and existing['scope'] == 'future':
                season = existing['season']
                scope = 'future'
                locked = 1
            elif existing and existing['scope'] == 'one' and not payload.get('optIn'):
                season = existing['season']
                scope = 'one'
                locked = existing['boundary_locked']
            else:
                if not episodes:
                    raise ValueError('Episode information is unavailable, so the future-season boundary was not saved.')
                season = boundary_season(episodes, today())
                scope = 'future'
                locked = 1
            rule_id = existing['id'] if existing else show_id
            db.execute('''INSERT INTO discover_rules(id,show_id,scope,season,quality,fallback,enabled,boundary_locked,next_check,body,updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                scope=excluded.scope, season=excluded.season, quality=excluded.quality, fallback=excluded.fallback,
                enabled=excluded.enabled, boundary_locked=excluded.boundary_locked, body=excluded.body, updated=excluded.updated,
                next_check=0''',
                       (rule_id, show_id, scope, season, quality, int(payload['fallback']), 1, locked, 0, json.dumps(item), time.time()))
    from .watchlist import sync_auto_titles
    sync_auto_titles(store)
    saved = next((rule for rule in rules(store) if rule['showId'] == show_id), None)
    return saved


def set_enabled(store, rule_id, enabled):
    with store.connect() as db:
        db.execute('UPDATE discover_rules SET enabled=?, updated=?, next_check=CASE WHEN ?=1 THEN 0 ELSE next_check END WHERE id=?',
                   (int(bool(enabled)), time.time(), int(bool(enabled)), rule_id))
    from .watchlist import sync_auto_titles
    sync_auto_titles(store)


def remove_rule(store, rule_id):
    """Remove the policy only. Completed files and manual requests stay."""
    with store.connect() as db:
        db.execute('DELETE FROM discover_rules WHERE id=?', (rule_id,))


def _claim(store, now, limit=2):
    claimed = []
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute('''SELECT * FROM discover_rules WHERE enabled=1 AND next_check<=? AND lease_until<?
            ORDER BY next_check, id LIMIT ?''', (now, now, limit)).fetchall()
        for row in rows:
            cursor = db.execute('UPDATE discover_rules SET lease_until=? WHERE id=? AND lease_until<?', (now + LEASE, row['id'], now))
            if cursor.rowcount:
                claimed.append(dict(row))
    return claimed


def _release(store, rule_id, delay, error=''):
    with store.connect() as db:
        db.execute('UPDATE discover_rules SET lease_until=0, last_check=?, next_check=?, last_error=?, updated=? WHERE id=?',
                   (time.time(), time.time() + delay, error[:500], time.time(), rule_id))


def consider(store, rule, episodes, now=None):
    """Queue each due episode at most once. Waiting episodes are not marked complete."""
    now = now if now is not None else time.time()
    current_day = today()
    due, waiting = classify_episodes(episodes, rule, current_day)
    show_id = rule.get('show_id') or rule.get('showId')
    queued = []
    for episode in due:
        key = episode_identity(show_id, episode['season'], episode['number'])
        from .media_management.store import overrides_monitor
        if overrides_monitor(store,key):continue
        have = owned(store, key)
        if have and have.get('state') == 'complete':
            continue
        if open_request_for(store, key):
            continue
        # Re-read the rule immediately before enqueue so a pause or quality change wins the race.
        fresh = next((item for item in rules(store) if item['id'] == rule['id']), None)
        if not fresh or not fresh['enabled']:
            break
        label = f"S{int(episode['season']):02d}E{int(episode['number']):02d}"
        row, created = insert_request(store, {
            'idempotency': f"rule:{rule['id']}:{int(episode['season'])}:{int(episode['number'])}",
            'kind': 'episode', 'title': (fresh['item'].get('title') or 'Show') + ' · ' + label,
            'quality': fresh['quality'], 'fallback': fresh['fallback'], 'manual': False, 'rule_id': rule['id'],
            'episode_key': key, 'state': 'searching',
            'body': {'externalId': show_id, 'source': 'NZB first', 'itemId': fresh['item'].get('id'),
                     'episode': {'id': episode.get('id'), 'season': int(episode['season']), 'number': int(episode['number']),
                                 'name': str(episode.get('name') or '')[:180]},
                     'scope': label},
        })
        if created:
            queued.append(row)
    if waiting and not due:
        update_note = f"Waiting for a release. {len(waiting)} episode{'s' if len(waiting) != 1 else ''} ha{'ve' if len(waiting) != 1 else 's'} not aired."
    else:
        update_note = ''
    return queued, update_note


def tick(app, store, fetch_episodes=None, now=None):
    initialise(store)
    if store.reset_pending() or not store.get('setup_complete'):
        return []
    now = now if now is not None else time.time()
    fetch_episodes = fetch_episodes or (lambda show_id: __import__('backend.discover_sources', fromlist=['episodes']).episodes(show_id, store).get('episodes') or [])
    from .acquisition import held
    if held(store):
        from .discover_acquire import sync_jobs
        sync_jobs(app, store)
        return []
    started = []
    for rule in _claim(store, now):
        try:
            fresh = next((item for item in rules(store) if item['id'] == rule['id']), None)
            if not fresh or not fresh['enabled']:
                _release(store, rule['id'], INTERVAL)
                continue
            episodes = fetch_episodes(fresh['showId'])
            queued, note = consider(store, fresh, episodes, now)
            for request_row in queued:
                if held(store):
                    break
                current = next((item for item in rules(store) if item['id'] == rule['id']), None)
                if not current or not current['enabled']:
                    break
                process(app, store, request_row)
                started.append(request_row['id'])
            _release(store, rule['id'], INTERVAL, note)
        except Exception as exc:
            from .discover_logic import redact
            _release(store, rule['id'], min(INTERVAL * 4, 6 * 3600), redact(exc))
    from .discover_acquire import sync_jobs
    sync_jobs(app, store)
    return started
