"""Google preference and quota-aware source selection."""
import time

def enabled(store):
    return store.get('youtube_use_google_api') != 'false'

def fallback(store):
    return not enabled(store) or not store.get('google_tokens') or float(store.get('google_quota_retry_at','0')) > time.time() or float(store.get('youtube_api_unavailable_until','0')) > time.time()

def description(store):
    if not enabled(store): return 'Google API is off. Using upload feeds and yt-dlp.'
    if not store.get('google_tokens'): return 'Google is not connected. Using upload feeds and yt-dlp.'
    if float(store.get('youtube_api_unavailable_until','0')) > time.time(): return 'Google API is temporarily unavailable. Using upload feeds and yt-dlp.'
    if fallback(store): return 'Google allowance reached. Using upload feeds and yt-dlp until it resets.'
    return 'Using public upload feeds. Google API is reserved for subscriptions and missing video details.'
