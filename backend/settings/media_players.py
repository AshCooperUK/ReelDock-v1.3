"""Non-secret, independent automation preferences for each new media player."""
import time
from .schema import Field, write
PLAYERS=('jellyfin','plex','kodi')
FIELDS={}
for player in PLAYERS:
    for option in ('sync','auto_scan'):
        name=player+'_'+option
        FIELDS[name]=Field('player_'+name,False,message='Choose on or off.')
    name=player+'_minutes'
    FIELDS[name]=Field('player_'+name,15,(5,15,30,60,180,360),message='Choose a supported interval.')


def check(store, values):
    for name,value in values.items():
        if value is True and not store.get('player_'+name.split('_')[0]+'_connection'):
            raise ValueError('Connect that media player first.')
    if values.get('jellyfin_sync'):
        import json
        if not json.loads(store.get('player_jellyfin_connection') or '{}').get('user'):
            raise ValueError('Choose a Jellyfin user first.')


def after_write(db, values):
    for player in PLAYERS:
        if any(name.startswith(player+'_') for name in values):
            changes={'player_'+player+'_next':'0','player_'+player+'_generation':str(time.time_ns())}
            for field,pending in (('sync','pending_sync'),('auto_scan','pending_scan')):
                if values.get(player+'_'+field) is False:
                    changes['player_'+player+'_'+pending]=''
                    changes['player_'+player+'_status']='Automation preferences saved.'
            write(db,changes)
