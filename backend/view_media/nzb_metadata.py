"""Enrich only indexer-backed titles; never add metadata-catalogue entries."""
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from datetime import timezone
import threading
from .providers import movie_card
from ..m3u_metadata import TMDB


def publication(item):
    stamps=[]
    for release in item.get('releases',[]):
        try:
            day=parsedate_to_datetime(release.get('published',''))
            if day.tzinfo is None:day=day.replace(tzinfo=timezone.utc)
            stamps.append(day)
        except (ValueError,TypeError,OverflowError):pass
    if stamps:
        latest=max(stamps)
        item.update(published=latest.isoformat(),added=latest.timestamp())
    return item


def movies(store,items,on_progress=None):
    if not store.get('m3u_tmdb_token'):return
    failures=0;lock=threading.Lock()
    def load(item):
        nonlocal failures
        with lock:
            if failures>=4:return
        try:
            identity=str(item.get('externalId') or '').removeprefix('tt')
            if not identity.isdigit():return
            row=TMDB(store).entity(dict(kind='movies',provider='imdb',identity='tt'+str(int(identity)).zfill(7)))
            if not row:return
            metadata=movie_card(row)
            # Keep NZB identity, source choices and private candidate references.
            item.update({key:metadata[key] for key in ('title','year','releaseDate','image','backdrop','description','rating','runtimeMinutes') if metadata.get(key)})
            item['metadataVerified']=True
            if on_progress:on_progress()
        except (ValueError,KeyError,TypeError):
            with lock:failures+=1
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(load,items))
