"""User-controlled pause at provider request boundaries."""
class SyncPaused(ValueError):
    pass


def check(store):
    if store.get('youtube_sync_paused')=='true':
        raise SyncPaused('YouTube sync is paused in Settings > Google & YouTube.')
