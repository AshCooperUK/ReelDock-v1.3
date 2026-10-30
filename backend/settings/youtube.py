"""YouTube owns its public preferences; existing storage keys remain compatible."""
import time
from .schema import Conflict, Field, write


def toggle(name, default=False):
    return Field('download_' + name, default, message='Choose on or off.')


DOWNLOAD_FIELDS = {
    'hardware_encoding': toggle('hardware_encoding', True),
    'authenticated_retry': toggle('authenticated_retry'),
    'sponsorblock': Field('download_sponsorblock', 'off', ('off', 'mark', 'remove'), message='Choose a SponsorBlock mode.'),
    'convert_fallback': toggle('convert_fallback', True),
    'force_transcode': toggle('force_transcode'),
    'restrict_filenames': toggle('restrict_filenames'),
    'sleep_interval': Field('download_sleep_interval', 0, minimum=0, maximum=86400, message='Choose a sleep interval from 0 to 86400 seconds.'),
    'new_channel_videos': toggle('new_channel_videos'),
    'default_range': Field('download_default_range', 180, (0,1,2,7,14,30,90,180,365,730), message='Choose a supported default download range.'),
    'minimum_video_minutes': Field('download_minimum_video_minutes', 5, minimum=0, maximum=120, message='Choose a minimum video duration from 0 to 120 minutes.'),
}
FIELDS = {
    **DOWNLOAD_FIELDS,
    'share_metadata': Field('youtube_share_metadata', True, message='Choose on or off.'),
    'quality': Field('download_quality', '1080', ('480','720','1080','2160'), message='Choose a supported quality.'),
    'use_google_api': Field('youtube_use_google_api', True, message='Choose on or off.'),
    'sync_paused': Field('youtube_sync_paused', False, message='Choose on or off.'),
    'cookie_import_channels': Field('youtube_cookie_import_channels', False, message='Choose on or off.'),
    'cookie_downloads': Field('youtube_cookie_downloads', True, message='Choose on or off.'),
    'check_minutes': Field('youtube_check_minutes', 360, (15,30,60,180,360,720,1440), message='Choose a supported upload check interval.'),
}


def check(store, values):
    if values.get('use_google_api') and store.get('local_only') == '1':
        raise Conflict('Google API requires a verified public HTTPS address.')


def after_write(db, values):
    """Keep existing scheduling side effects in the same transaction as the preference."""
    if 'default_range' in values:
        from ..channel_downloads import drop_unmatched
        days=values['default_range']
        for row in db.execute('SELECT * FROM download_rules WHERE range_override=0').fetchall():
            rule=dict(row);rule['range_days']=days
            db.execute("DELETE FROM downloads WHERE state='skipped' AND id IN (SELECT id FROM videos WHERE channel=?)",(rule['channel'],))
            drop_unmatched(db,rule['channel'],rule)
        if days==0 or days>365:
            db.execute("UPDATE history_jobs SET state='indexing',updated=0 WHERE cursor!='' AND channel IN (SELECT channel FROM download_rules WHERE range_override=0)")
        write(db, dict.fromkeys(('next_auto_check','next_retention_check','next_history'), '0'))
    if 'share_metadata' in values:
        write(db, {'youtube_metadata_generation': str(time.time_ns()),
                   'youtube_metadata_pending': '1' if values['share_metadata'] else '0',
                   'youtube_metadata_cursor': ''})
        if not values['share_metadata']:
            db.execute("UPDATE activity_tasks SET state='cancelled',message='YouTube metadata disabled in Settings.',updated=? WHERE id IN ('youtube-artwork','youtube-metadata')", (time.time(),))
    if 'check_minutes' in values:
        row = db.execute("SELECT value FROM settings WHERE key='next_sync'").fetchone()
        due = float(row[0] or 0) if row else 0
        write(db, {'next_sync': str(min(due, time.time() + values['check_minutes'] * 60))})
    if 'cookie_import_channels' in values:
        if values['cookie_import_channels']:
            write(db, {'youtube_cookie_subscriptions_next': '0', 'next_sync': '0'})
        else:
            write(db, {'youtube_cookie_subscriptions_status': ''})
    if values.get('sync_paused') is False:
        write(db, dict.fromkeys(('next_sync','next_history','emby_next_check'), '0'))
    if 'use_google_api' in values:
        write(db, {'next_sync':'0', 'next_history':'0', 'recent_scan_started':'0', 'sync_error':'', 'youtube_fallback_error':''})
        db.execute('UPDATE youtube_push_events SET retry_at=0 WHERE done=0')
        db.execute("UPDATE guide_week_jobs SET retry_at=0 WHERE state='queued'")
