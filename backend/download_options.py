"""Compatibility facade for worker preferences and existing download APIs."""
from .settings.youtube import DOWNLOAD_FIELDS, toggle
from .settings.auto_download import ENABLED
from .settings.schema import validate, write

FIELDS = {**DOWNLOAD_FIELDS, 'pause_downloads': toggle('pause_downloads'), 'auto_downloads': ENABLED}
DEFAULTS = {key: field.default for key, field in FIELDS.items()}


def read(store, engine=None):
    result = {key: field.read(store) for key, field in FIELDS.items()}
    from .download_control import paused
    result['pause_downloads'] = paused(store, engine) if engine is not None else paused(store)
    return result


def save(store, values):
    writes = validate(FIELDS, values)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        write(db, writes)
        if 'default_range' in values:
            from .settings.youtube import after_write
            after_write(db, {'default_range': values['default_range']})
    if values.get('pause_downloads') is False:
        from .acquisition import resume
        resume(store)


def for_channel(store, channel, engine=None):
    """Resolve overrides when a job starts; never mutate the global preferences."""
    from .channel_downloads import preferences
    result = read(store, engine) if engine is not None else read(store)
    rule = preferences(store, channel)
    result['minimum_video_minutes']=rule['minimum_video_minutes']
    if rule['sponsorblock'] != 'default':
        result['sponsorblock'] = rule['sponsorblock']
    if rule['force_transcode'] != 'default':
        result['force_transcode'] = rule['force_transcode'] == 'on'
    return result
