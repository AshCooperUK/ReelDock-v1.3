from .schema import Field, Conflict
from ..vpn import ENGINE_KEYS, call, policy_values, status

FIELDS = {name: Field('vpn_' + name, False) for name in (*ENGINE_KEYS, 'downloads', 'metadata')}


def read_values(store):
    return policy_values(status()['values'])


def save_values(store, values):
    if 'downloads' in values and any(key in values for key in ENGINE_KEYS):
        raise ValueError('Choose individual engines or the all-downloads setting in one change.')
    try:
        return policy_values(call('policy', values=values)['values'])
    except ValueError as exc:
        raise Conflict(str(exc)) from None
