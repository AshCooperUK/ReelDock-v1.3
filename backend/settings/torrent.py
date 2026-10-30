"""Public torrent preferences backed by the existing engine settings file."""
from .schema import Conflict, Field

FIELDS = {
    'share_metadata': Field('share_metadata', True),
    'download_limit_bps': Field('download_limit_bps', 0, minimum=0),
    'max_active_downloads': Field('max_active_downloads', 3, minimum=0, maximum=999),
    'listen_port': Field('listen_port', 6881, minimum=0, maximum=65535),
    'encryption': Field('encryption', 'prefer', choices=('disabled', 'prefer', 'required')),
    **{key: Field(key, True) for key in ('enable_dht', 'enable_pex', 'enable_lsd', 'enable_upnp', 'enable_natpmp', 'enable_web_seeds')},
}


def _engine(store):
    from flask import current_app
    from ..torrents import _engine as open_engine
    from ..operations import Halted
    from ..downloaders.torrent.engine import EngineError
    try:
        return open_engine(current_app, store)
    except (EngineError, Halted) as exc:
        raise Conflict(str(exc)) from exc


def read_values(store):
    values = _engine(store).settings()
    return {name: values[name] for name in FIELDS}


def save_values(store, values):
    from ..downloaders.torrent.engine import EngineError
    try:
        result = _engine(store).update_settings(values)
    except EngineError as exc:
        raise ValueError(str(exc)) from exc
    return {name: result[name] for name in FIELDS}
