"""Public optimistic-concurrency stamps for explicit Activity item actions.

These hashes identify a rendered attempt, not an authorization capability.
Authentication, Origin and CSRF checks remain with the route. Progress and
metadata updates do not invalidate a user's click; state/attempt changes do.
"""
import hashlib
import json
import re


def capture(kind, item, generation=None):
    version = item.get('activity_generation', 0) if generation is None else generation
    version = version if type(version) is int and 0 <= version <= 9_007_199_254_740_991 else 0
    state = str(item.get('state') or '')
    attempt = [str(kind), str(item.get('id')), state, version]
    if kind == 'discover':
        attempt.extend([item.get('stop_generation', 0), item.get('engine') or '', item.get('engine_id') or ''])
    digest = hashlib.sha256(json.dumps(attempt, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
    return dict(state=state, generation=version, attempt=digest)


def valid(value):
    return (isinstance(value, dict) and set(value) == {'state', 'generation', 'attempt'}
            and isinstance(value['state'], str) and 0 < len(value['state']) <= 40
            and type(value['generation']) is int and 0 <= value['generation'] <= 9_007_199_254_740_991
            and isinstance(value['attempt'], str) and re.fullmatch(r'[0-9a-f]{64}', value['attempt']) is not None)
