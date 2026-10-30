"""Shared, persistent pause gate; individual engine pauses remain independent."""
import time
import threading


def pause_status(store, engine=None):
    """One read of the existing pause gate, with a public presentation reason."""
    from .acquisition import held
    from .vpn import downloads_blocked
    if store.get('download_pause_downloads', 'false') == 'true':
        return dict(paused=True, reason='downloads')
    if held(store):
        return dict(paused=True, reason='hold')
    blocked = downloads_blocked(engine) if engine is not None else downloads_blocked()
    return dict(paused=blocked, reason='vpn' if blocked else '')


def paused(store, engine=None):
    return (pause_status(store, engine) if engine is not None else pause_status(store))['paused']


def reader(store, engine=None):
    """Bound frequent socket checkpoints without caching across processes forever."""
    checked = None
    value = (True, '')
    refreshing = threading.Lock()
    def current():
        nonlocal checked, value
        now = time.monotonic()
        if (checked is None or now - checked >= 0.2) and refreshing.acquire(blocking=False):
            try:
                # Hundreds of NNTP/socket checkpoints share this reader. Only
                # one may refresh, and slow reads do not immediately expire.
                try:
                    result = pause_status(store, engine) if engine is not None else pause_status(store)
                    if type(result.get('paused')) is not bool or result.get('reason') not in ('', 'downloads', 'hold', 'vpn'):
                        raise ValueError('Unknown download pause decision')
                    value = (result['paused'], result['reason'])
                except Exception:
                    # A failed read must never permit transfer or invent a
                    # particular cause. The next bounded checkpoint retries.
                    value = (True, '')
                checked = time.monotonic()
            finally:
                refreshing.release()
        return value
    def read():
        return current()[0]
    def snapshot():
        paused, reason = current()
        return dict(paused=paused, reason=reason)
    read.snapshot = snapshot
    return read
