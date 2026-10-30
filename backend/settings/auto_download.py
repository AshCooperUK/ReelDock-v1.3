"""Watched-file intake preferences; destinations remain Compose-owned."""
from .schema import Field

ENABLED = Field('download_auto_downloads', False, message='Choose on or off.')
FIELDS = {
    'release_check_minutes': Field('discover_engine_interval',15,minimum=5,maximum=1440,message='Choose a release check interval from 5 to 1440 minutes.'),
    'enabled': ENABLED,
    **{kind: Field('auto_download_' + kind, True, message='Choose on or off.')
       for kind in ('torrent', 'nzb', 'strm')},
}


def accepts(store, kind):
    return kind in ('torrent', 'nzb', 'strm') and FIELDS[kind].read(store)


def check(store, values):
    pass


def after_write(db, values):
    pass
