"""Saved upload-check cadence; bounded fallback batches keep their own pacing."""
import time
CHOICES=(15,30,60,180,360,720,1440)
def minutes(store):
    try:value=int(store.get('youtube_check_minutes','360'))
    except ValueError:value=360
    return value if value in CHOICES else 360

def read(store):
    return dict(minutes=minutes(store),last=float(store.get('last_sync','0') or 0),next=float(store.get('next_sync','0') or 0),paused=store.get('youtube_sync_paused')=='true',running=store.get('sync_running')=='1',remaining=int(store.get('youtube_fallback_remaining','0') or 0) if float(store.get('recent_scan_started','0') or 0)>0 else 0)

def save(store,value):
    from . import settings
    settings.save(store, 'youtube', {'check_minutes': value})
