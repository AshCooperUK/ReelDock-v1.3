"""Bounded ReelDock network history for the web and worker processes.

Each process samples its own interfaces, excluding loopback. The dashboard
adds the two fresh series together. No host mounts or privileged access, so
other containers on the machine are not included.
"""
import json
import logging
import time
from pathlib import Path

INTERVAL = 5
MAX_POINTS = 181


def counters(path='/proc/net/dev'):
    result = {}
    for line in Path(path).read_text().splitlines():
        if ':' not in line:
            continue
        name, values = line.split(':', 1)
        name = name.strip()
        fields = values.split()
        if name == 'lo':
            continue
        if len(fields) < 16:
            raise ValueError('Incomplete network counters')
        received, sent = int(fields[0]), int(fields[8])
        if received < 0 or sent < 0:
            raise ValueError('Invalid network counters')
        result[name] = (received, sent)
    if not result:
        raise ValueError('No network interfaces')
    return result


class Sampler:
    def __init__(self):
        self.previous = None

    def sample(self, values, monotonic, timestamp):
        previous = self.previous
        self.previous = (values, monotonic)
        if previous is None or values.keys() != previous[0].keys():
            return None
        elapsed = monotonic - previous[1]
        if elapsed <= 0 or elapsed > INTERVAL * 3:
            return None
        changes = [(values[key][0] - old[0], values[key][1] - old[1]) for key, old in previous[0].items()]
        if any(received < 0 or sent < 0 for received, sent in changes):
            return None
        return {'time': timestamp, 'download': sum(v[0] for v in changes) / elapsed,
                'upload': sum(v[1] for v in changes) / elapsed}


def _series(raw, now):
    """Return ('bad'|'stale'|state, points) for one process."""
    try:
        data = json.loads(raw or '{}')
        if not isinstance(data, dict):
            raise ValueError('network history must be an object')
        updated = float(data.get('updated', 0))
        fresh = 0 <= now - updated <= INTERVAL * 4
    except (ValueError, TypeError, AttributeError):
        return 'bad', []
    if not fresh:
        return 'stale', []
    points = data.get('points', [])
    if not isinstance(points, list):
        return 'bad', []
    return data.get('state', 'waiting'), points[-MAX_POINTS:]


def _merge(series):
    """Add download and upload rates that fall in the same 5 second slot."""
    buckets = {}
    for points in series:
        seen = {}
        for point in points:
            if not isinstance(point, dict):
                continue
            try:
                stamp = float(point['time'])
                download = float(point['download'])
                upload = float(point['upload'])
            except (KeyError, TypeError, ValueError):
                continue
            slot = int(stamp // INTERVAL) * INTERVAL
            seen[slot] = {'time': stamp, 'download': download, 'upload': upload}
        for slot, point in seen.items():
            current = buckets.get(slot)
            if current is None:
                buckets[slot] = dict(point)
            else:
                current['download'] += point['download']
                current['upload'] += point['upload']
                current['time'] = max(current['time'], point['time'])
    return [buckets[slot] for slot in sorted(buckets)][-MAX_POINTS:]


def snapshot(store, now=None):
    now = time.time() if now is None else now
    loaded = [_series(store.get(key, '{}'), now) for key in ('worker_network', 'web_network')]
    fresh = [(state, points) for state, points in loaded if state not in {'bad', 'stale'}]
    if not fresh:
        state = 'unavailable' if any(item[0] == 'bad' for item in loaded) else 'waiting'
        return {'state': state, 'scope': 'reeldock', 'points': [], 'interval': INTERVAL}
    states = [state for state, _points in fresh]
    overall = 'live' if 'live' in states else 'warming' if 'warming' in states else states[0]
    points = fresh[0][1] if len(fresh) == 1 else _merge(item[1] for item in fresh)
    return {'state': overall, 'scope': 'reeldock', 'points': points, 'interval': INTERVAL}


def loop(store, stop, key='worker_network'):
    sampler = Sampler()
    points = []
    revision = store.get('auth_revision')
    while not stop.is_set():
        paused = True
        with store.background_operation():
            if store.reset_pending():
                paused = True
            else:
                paused = False
                current_revision = store.get('auth_revision')
                if current_revision != revision:
                    sampler, points, revision = Sampler(), [], current_revision
                now = time.time()
                try:
                    point = sampler.sample(counters(), time.monotonic(), now)
                    if point is None:
                        points = []
                    else:
                        points = (points + [point])[-MAX_POINTS:]
                    data = {'updated': now, 'state': 'live' if point else 'warming', 'points': points}
                except (OSError, ValueError):
                    sampler = Sampler()
                    points = []
                    data = {'updated': now, 'state': 'unavailable', 'points': []}
                try:
                    store.set(key, json.dumps(data, separators=(',', ':')))
                except Exception:
                    logging.warning('Could not save network activity; retrying shortly')
        if paused:
            stop.wait(1)
            continue
        stop.wait(INTERVAL)
