"""Public NZB limits backed by the existing downloader configuration, not SQLite.

Provider credentials remain exclusively on the specialised NZB settings route.
"""
from .schema import Conflict, Field, write
import json
from pathlib import Path

SIZE_CEILING = 100_000_000_000

FIELDS = {
    'min_video_minutes': Field('min_video_minutes', 0, minimum=0, maximum=1440, message='Choose a minimum video length from 0 to 1440 minutes.'),
    'min_download_bytes': Field('min_download_bytes', 0, minimum=0, maximum=SIZE_CEILING, message='Choose a minimum size from 0 MB to 100 GB.'),
    'max_download_bytes': Field('max_download_bytes', SIZE_CEILING, minimum=0, maximum=SIZE_CEILING, message='Choose a maximum size from 0 MB to 100 GB.'),
    'share_metadata': Field('share_metadata', True, message='Choose on or off.'),
    'download_limit_bps': Field('download_limit_bps', 0, minimum=0,
                                message='Enter a whole-number speed limit of zero or more.'),
    'max_active_downloads': Field('max_active_downloads', 3, minimum=0, maximum=999,
                                  message='Use 0 to 999 NZBs at once.'),
}

ENGINE_FIELDS = dict(FIELDS)
DISCOVERY_FIELDS = {
    'preferred_quality': Field('discover_engine_quality', '1080', ('720','1080','2160')),
    'allow_lower_quality': Field('discover_engine_fallback', False),

}
FIELDS.update(DISCOVERY_FIELDS)


def _engine(store):
    from flask import current_app
    from ..nzb import _engine as open_engine
    from ..operations import Halted
    from ..downloaders.nzb.engine import EngineError
    try:
        return open_engine(current_app, store)
    except (EngineError, Halted) as exc:
        raise Conflict(str(exc)) from exc


def read_values(store):
    values = _engine(store).settings()
    return {**{name: values[name] for name in ENGINE_FIELDS}, **{name: field.read(store) for name,field in DISCOVERY_FIELDS.items()}}


def selection_limits(store):
    """Read saved size limits without starting a downloader in the search worker."""
    path = Path(store.directory) / 'nzb-engine' / 'settings.json'
    try:
        values = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        values = {}
    if not isinstance(values, dict):
        raise ValueError('Could not read NZB size settings.')
    limits = tuple(values.get(name, FIELDS[name].default)
                   for name in ('min_download_bytes', 'max_download_bytes'))
    for name, value in zip(('min_download_bytes', 'max_download_bytes'), limits):
        FIELDS[name].validate(value)
    if limits[0] > limits[1]:
        raise ValueError('NZB minimum size exceeds maximum size.')
    return limits


def save_values(store, values):
    engine_values={name:value for name,value in values.items() if name in ENGINE_FIELDS}
    if engine_values: _engine(store).update_settings(engine_values)
    with store.connect() as db:
        write(db,{DISCOVERY_FIELDS[name].key:DISCOVERY_FIELDS[name].validate(value) for name,value in values.items() if name in DISCOVERY_FIELDS})
    return read_values(store)
