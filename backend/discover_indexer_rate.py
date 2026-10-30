"""Shared indexer pacing and server-directed backoff across web and worker."""
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from .view_media.cache_lock import refresh_lock

INTERVAL=1.0


def limited_indexers(error):
    names=[]
    while error is not None:
        names.extend(getattr(error,'limited_indexers',()))
        error=error.__cause__
    return tuple(dict.fromkeys(names))


def limit_message(names):
    return 'NZB indexer request limit reached: '+', '.join(sorted(set(names) or {'name unavailable'}))+'.'


def retry_seconds(error,now):
    while error is not None:
        if isinstance(error,HTTPError) and error.code==429:
            value=error.headers.get('Retry-After','') if error.headers else ''
            try:return max(60,float(value))
            except (ValueError,TypeError):
                try:return max(60,parsedate_to_datetime(value).timestamp()-now)
                except (ValueError,TypeError,OverflowError):return 120
        error=error.__cause__
    return None


@contextmanager
def request_slot(store,indexer):
    from .discover_sources import DiscoverError,_provider_name
    name=_provider_name(indexer)
    key='indexer-rate:'+hashlib.sha256(json.dumps([indexer.get('url'),indexer.get('api_key')]).encode()).hexdigest()
    folder=Path(store.directory)/'indexer-rate';folder.mkdir(mode=0o700,exist_ok=True)
    path=folder/(key.split(':')[1]+'.json')
    with refresh_lock(store,key):
        try:state=json.loads(path.read_text())
        except (OSError,ValueError):state={}
        now=time.time()
        if state.get('blockedUntil',0)>now:
            error=DiscoverError(limit_message((name,)))
            error.failures=('http_429',);error.retry_at=state['blockedUntil']
            error.limited_indexers=(name,)
            raise error
        time.sleep(max(0,state.get('nextCall',0)-now))
        state['nextCall']=time.time()+INTERVAL
        try:
            yield
            state.pop('blockedUntil',None)
        except Exception as exc:
            delay=retry_seconds(exc,time.time())
            if delay is not None:
                state['blockedUntil']=time.time()+delay
                exc.retry_at=state['blockedUntil']
                exc.limited_indexers=(name,)
            raise
        finally:
            path.write_text(json.dumps(state));path.chmod(0o600)
