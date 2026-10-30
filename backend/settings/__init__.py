"""Engine-scoped public settings, shared validation and atomic persistence.

Never register secret storage keys here. Credentials and uploads keep their
existing specialised endpoints. Modules are an explicit allowlist, not imports
chosen by an HTTP caller.
"""
from . import youtube, m3u, nzb, torrent, auto_download, media_players, vpn, catalogue
from .schema import Conflict, validate, write

MODULES = {'youtube': youtube, 'm3u': m3u, 'nzb': nzb, 'torrent': torrent, 'auto-download': auto_download, 'media-players': media_players, 'vpn': vpn, 'catalogue':catalogue}


def module(name):
    if name not in MODULES:
        raise KeyError('Unknown settings engine.')
    return MODULES[name]


def read(store, name):
    engine = module(name)
    if hasattr(engine, 'read_values'):
        return engine.read_values(store)
    return {key: field.read(store) for key, field in engine.FIELDS.items()}


def save(store, name, values):
    engine = module(name)
    writes = validate(engine.FIELDS, values)
    if hasattr(engine, 'save_values'):
        return engine.save_values(store, values)
    engine.check(store, values)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        write(db, writes)
        engine.after_write(db, values)
    return read(store, name)
