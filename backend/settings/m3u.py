"""M3U/Xtream public preferences and per-source import option validation.

Connection credentials and source lifecycle operations remain in their guarded
M3U/Xtream routes; source options are never promoted into global preferences.
"""
import re
import time
from datetime import date
from .schema import Field, write

FIELDS = {
    'min_download_bytes': Field('m3u_min_download_bytes', 0, minimum=0, maximum=100_000_000_000),
    'max_download_bytes': Field('m3u_max_download_bytes', 100_000_000_000, minimum=0, maximum=100_000_000_000),
    'share_metadata': Field('m3u_share_metadata', True, message='Choose on or off.'),
    'refresh_emby': Field('m3u_export_refresh', False, message='Choose on or off.'),
    'auto_libraries': Field('m3u_emby_auto', False, message='Choose on or off.'),
}


def check(store, values):
    minimum=values.get('min_download_bytes',FIELDS['min_download_bytes'].read(store))
    maximum=values.get('max_download_bytes',FIELDS['max_download_bytes'].read(store))
    if minimum>maximum: raise ValueError('VOD minimum size exceeds maximum size.')
    # Same deployment validation as the existing download-settings endpoint.
    if values.get('auto_libraries'):
        from ..storage_layout import fixed
        if not fixed(store):
            from ..m3u_emby import settings, validate_paths
            validate_paths(settings(store)['emby_folders'])


def after_write(db, values):
    write(db, {'m3u_emby_check': '0'})
    if 'share_metadata' in values:
        write(db, {'m3u_metadata_generation': str(time.time_ns())})
        if not values['share_metadata']:
            db.execute("UPDATE activity_tasks SET state='cancelled',message='Metadata disabled in Settings.',updated=? WHERE id IN ('m3u-catalogue','m3u-exports')", (time.time(),))
            db.execute("UPDATE m3u_metadata_runs SET state='cancelled',message='Metadata disabled in Settings.',updated=? WHERE state IN ('queued','running')", (time.time(),))
        else:
            db.execute("UPDATE m3u_metadata_runs SET state='queued',message='Metadata enabled; waiting to resume.',updated=? WHERE state='cancelled'", (time.time(),))

    if 'auto_libraries' in values:
        write(db, {'m3u_emby_status': 'Library setup queued.' if values['auto_libraries'] else 'Automatic library setup is off. Existing libraries are kept.'})


DEFAULTS = dict(hours=1, enabled=True, clean_sync=True, live_tv=True, unsorted=True,
                scrub=['HD :', 'SD :'], exclude=[], remove=['720p', 'WEB', 'h264', 'H264', 'HDTV', 'x264'],
                replace={'1/2': '½', '/': '-'}, cleaners=['tv'], refresh_emby=False,
                naming='legacy', episode_maps=[], numeric_ids='auto')
NAMING = ('legacy', 'emby', 'jellyfin', 'plex')

def source_options(payload):
    from ..m3u import clean_show
    if not isinstance(payload, dict):
        raise ValueError('Choose valid playlist settings.')
    options = dict(DEFAULTS)
    for key in ('enabled', 'clean_sync', 'live_tv', 'unsorted', 'refresh_emby'):
        if not isinstance(payload.get(key, options[key]), bool):
            raise ValueError('Choose on or off for each option.')
        options[key] = payload.get(key, options[key])
    hours = payload.get('hours', 1)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 <= hours <= 168:
        raise ValueError('Choose a refresh interval from 0 to 168 hours; 0 disables automatic refresh.')
    options['enabled'] = hours > 0 if 'hours' in payload else options['enabled']
    options['hours'] = hours if options['enabled'] else 0
    for key in ('scrub', 'exclude', 'remove', 'cleaners'):
        values = payload.get(key, options[key])
        if not isinstance(values, list) or len(values) > 50 or any(not isinstance(v, str) or not v or len(v) > 100 for v in values):
            raise ValueError('Use up to 50 non-empty terms, one per line.')
        options[key] = values
    if any(v not in ('movies', 'series', 'tv', 'unsorted') for v in options['cleaners']):
        raise ValueError('Choose recognised title cleaning categories.')
    replacements = payload.get('replace', options['replace'])
    if (not isinstance(replacements, dict) or len(replacements) > 50
            or any(not k or len(k) > 100 or not isinstance(v, str) or len(v) > 100 for k, v in replacements.items())):
        raise ValueError('Enter replacements as original=replacement, one per line.')
    options['replace'] = replacements
    options['naming'] = payload.get('naming', 'legacy')
    if options['naming'] not in NAMING:
        raise ValueError('Choose a recognised media-server naming preset.')
    maps = payload.get('episode_maps', [])
    if not isinstance(maps, list) or len(maps) > 2000:
        raise ValueError('Use up to 2,000 dated episode matches per playlist.')
    seen = set()
    for item in maps:
        if not isinstance(item, dict):
            raise ValueError('Each episode match needs a show, date, season and episode.')
        show = item.get('show'); day = item.get('date')
        if not isinstance(show, str) or not show.strip() or len(show) > 140:
            raise ValueError('Enter a show name for each episode match.')
        try:
            if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day): raise ValueError()
            date.fromisoformat(day)
        except ValueError:
            raise ValueError('Use a valid YYYY-MM-DD air date for each episode match.') from None
        for key, minimum in (('season', 0), ('episode', 1)):
            value = item.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 999:
                raise ValueError('Use season 0–999 and episode 1–999 for episode matches.')
        identity = (clean_show(show).casefold(), day)
        if identity in seen:
            raise ValueError('Only one episode match is allowed for a show and date.')
        seen.add(identity)
    options['episode_maps'] = maps
    options['numeric_ids'] = payload.get('numeric_ids', 'auto')
    if options['numeric_ids'] not in ('none', 'auto', 'tmdb', 'tvdb'):
        raise ValueError('Choose what numeric playlist IDs represent.')
    return options
