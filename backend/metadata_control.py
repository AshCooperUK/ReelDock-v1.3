"""Cooperative cancellation boundaries shared by metadata workers only."""
from contextlib import contextmanager
from contextvars import ContextVar
from .vpn.transport import metadata

_guard = ContextVar('metadata_guard', default=None)


class Cancelled(Exception):
    pass


def checkpoint():
    guard = _guard.get()
    if guard is not None and not guard():
        raise Cancelled('Metadata work cancelled in Settings.')


@contextmanager
def work(guard):
    token = _guard.set(guard)
    try:
        checkpoint()
        yield
        checkpoint()
    finally:
        _guard.reset(token)


@metadata
def vod_task(store, action):
    generation = store.get('m3u_metadata_generation', '0')
    def enabled():
        return (store.get('m3u_share_metadata', 'true') == 'true' and
                store.get('m3u_metadata_generation', '0') == generation)
    try:
        with work(enabled):
            return action()
    except Cancelled:
        return False


@metadata
def youtube_task(store, action, default=''):
    generation = store.get('youtube_metadata_generation', '0')
    def enabled():
        return (store.get('youtube_share_metadata', 'true') == 'true' and
                store.get('youtube_metadata_generation', '0') == generation)
    try:
        with work(enabled):
            return action()
    except Cancelled:
        return default
