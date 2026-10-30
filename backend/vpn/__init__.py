"""Unprivileged application RPC. The gateway owns secrets, routes and firewall."""
import json
import math
import os
import socket
import time
import http.client
import ipaddress
import ssl
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Lock

from .transport import metadata as vpn_metadata

RUN = Path(os.environ.get('REELDOCK_VPN_RUN', '/vpn-run'))
ENGINE_KEYS = ('youtube', 'nzb', 'torrent', 'vod')
COUNTRY_TIMEOUT = 2.0
COUNTRY_HOST = 'api.country.is'
COUNTRY_CODES = frozenset(('AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ '
    'CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR '
    'GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP '
    'KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ '
    'NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ '
    'TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW').split())
_country_lock = Lock()
_country_cache = OrderedDict()
_country_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='vpn-country-display')


def policy_values(raw):
    """Public compatibility view; reading a legacy policy never writes it."""
    raw = raw if isinstance(raw, dict) else {}
    legacy = raw.get('downloads', not all(raw.get(key) is False for key in ENGINE_KEYS))
    legacy = legacy if type(legacy) is bool else True
    values = {}
    for key in ENGINE_KEYS:
        value = raw.get(key, legacy)
        values[key] = value if type(value) is bool else True
    metadata = raw.get('metadata', False)
    values['metadata'] = metadata if type(metadata) is bool else True
    values['downloads'] = any(values[key] for key in ENGINE_KEYS)
    return values


def _public_ip(value):
    if not isinstance(value, str) or len(value) > 64 or '%' in value:
        raise ValueError('Invalid public IP.')
    address = ipaddress.ip_address(value)
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise ValueError('Invalid public IP.')
    return str(address)


@vpn_metadata
def _country_request(exit_ip):
    """Fixed HTTPS metadata request; no redirects, proxy, credentials or state writes."""
    deadline = time.monotonic() + COUNTRY_TIMEOUT
    connection = None
    try:
        # Check the fixed host's resolved addresses before opening its TLS socket.
        # The marked metadata resolver and socket retain the existing VPN policy.
        addresses = socket.getaddrinfo(COUNTRY_HOST, 443, type=socket.SOCK_STREAM)
        if not addresses or any(_public_ip(row[4][0]) != row[4][0] for row in addresses):
            return ''
        for family, kind, protocol, _, address in addresses:
            raw = None
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return ''
                raw = socket.socket(family, kind, protocol)
                raw.settimeout(remaining)
                raw.connect(address)
                raw.settimeout(max(0.001, deadline - time.monotonic()))
                secure = ssl.create_default_context().wrap_socket(raw, server_hostname=COUNTRY_HOST)
                raw = None
                connection = http.client.HTTPSConnection(COUNTRY_HOST)
                connection.sock = secure
                secure.settimeout(max(0.001, deadline - time.monotonic()))
                connection.request('GET', '/' + exit_ip, headers={'Accept': 'application/json'})
                secure.settimeout(max(0.001, deadline - time.monotonic()))
                response = connection.getresponse()
                if response.status != 200:
                    return ''  # In particular, never follow a redirect.
                body = bytearray()
                while len(body) <= 4096:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return ''
                    secure.settimeout(remaining)
                    part = response.read1(4097 - len(body))
                    if not part:
                        break
                    body.extend(part)
                if len(body) > 4096:
                    return ''
                result = json.loads(body)
                if not isinstance(result, dict) or _public_ip(result.get('ip')) != exit_ip:
                    return ''
                code = result.get('country')
                return code if isinstance(code, str) and code in COUNTRY_CODES else ''
            except (OSError, ValueError, http.client.HTTPException):
                if time.monotonic() >= deadline:
                    return ''
            finally:
                if raw is not None:
                    raw.close()
                if connection is not None:
                    connection.close()
                    connection = None
    except (OSError, ValueError, http.client.HTTPException):
        return ''
    return ''


def _country_completed(exit_ip, future):
    try:
        code = future.result()
    except Exception:
        code = ''
    with _country_lock:
        if _country_cache.get(exit_ip) is future:
            _country_cache[exit_ip] = (time.monotonic() + (3600 if code else 60), code)


def exit_country(exit_ip):
    """Coalesce UI reads; cache at most 16 IPs and bound added display latency."""
    try:
        exit_ip = _public_ip(exit_ip)
    except ValueError:
        return ''
    started = time.monotonic()
    created = False
    with _country_lock:
        entry = _country_cache.get(exit_ip)
        if isinstance(entry, tuple) and entry[0] > started:
            _country_cache.move_to_end(exit_ip)
            return entry[1]
        if isinstance(entry, tuple):
            del _country_cache[exit_ip]
            entry = None
        if entry is None:
            if len(_country_cache) >= 16:
                removable = next((key for key, value in _country_cache.items() if not isinstance(value, Future)), None)
                if removable is None:
                    return ''
                del _country_cache[removable]
            entry = _country_pool.submit(_country_request, exit_ip)
            _country_cache[exit_ip] = entry
            created = True
    if created:
        entry.add_done_callback(lambda future: _country_completed(exit_ip, future))
    try:
        return entry.result(timeout=max(0, COUNTRY_TIMEOUT - (time.monotonic() - started)))
    except Exception:
        return ''


def display_status():
    """Only authenticated VPN Settings reads enrich the gateway's public status."""
    value = status()
    try:
        code = exit_country(value.get('external_ip')) if value.get('connected') is True else ''
    except Exception:
        code = ''  # A display lookup must never hide the connection status.
    return {**value, 'country_code': code}


def call(action, **values):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(40)
            client.connect(str(RUN / 'control.sock'))
            client.sendall(json.dumps(dict(action=action, **values)).encode() + b'\n')
            raw = bytearray()
            while not raw.endswith(b'\n') and len(raw) < 65536:
                part = client.recv(65536)
                if not part: break
                raw.extend(part)
        result = json.loads(raw)
    except (OSError, ValueError):
        raise ValueError('VPN service is unavailable. Protected traffic remains blocked; check the ReelDock VPN container.') from None
    if 'error' in result: raise ValueError(result['error'])
    return result


def status():
    try:
        value = call('status')
        return {**value, 'values': policy_values(value.get('values'))}
    except ValueError:
        try: saved = json.loads((RUN / 'status.json').read_text())
        except (OSError, ValueError): saved = {}
        if not isinstance(saved, dict): saved = {}
        return {**saved, 'available': False, 'connected': False, 'external_ip': '',
                'state': 'Unavailable', 'message': 'VPN service unavailable.',
                'values': policy_values(saved.get('values'))}


def cached_status():
    """Read public state for overview widgets without blocking on gateway RPC."""
    try:
        file = RUN / 'status.json'
        if file.is_symlink() or file.stat().st_size > 16384: return {}
        saved = json.loads(file.read_text())
        if not isinstance(saved, dict): return {}
        if time.time() - float(saved.get('checked_at', 0)) > 25:
            saved.update(available=False, connected=False, state='Unavailable')
        saved['values'] = policy_values(saved.get('values'))
        return saved
    except (OSError, ValueError, TypeError):
        return {}


def downloads_blocked(engine=None):
    """Cheap worker checkpoint; socket/firewall enforcement is independent."""
    if os.environ.get('REELDOCK_VPN_TRANSPORT') != '1': return False
    try:
        saved = json.loads((RUN / 'status.json').read_text())
        values = saved['values']
        if (not isinstance(values, dict) or any(type(value) is not bool for value in values.values())
                or not (type(values.get('downloads')) is bool or all(type(values.get(key)) is bool for key in ENGINE_KEYS))
                or type(saved['connected']) is not bool or saved.get('available') is False
                or 'available' in saved and type(saved['available']) is not bool):
            return True
        public = policy_values(values)
        if not public[engine if engine in ENGINE_KEYS else 'downloads']: return False
        checked = saved['checked_at']
        if type(checked) not in (int, float) or not math.isfinite(checked): return True
        return not saved['connected'] or not -5 <= time.time() - checked <= 25
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return True


def register(app, access):
    import hmac
    from flask import jsonify, request, session
    @app.route('/api/vpn', methods=['GET', 'POST'])
    def vpn_api():
        denied = access(local_action=True)
        if denied: return denied
        if request.method == 'GET': return jsonify(display_status())
        if not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), session.get('csrf') or ''):
            return jsonify(error='Reload the page before changing VPN settings.'), 403
        body = request.get_json(silent=True)
        if not isinstance(body, dict): return jsonify(error='Choose a VPN connection.'), 400
        try:
            if body.get('action') == 'wireguard' and set(body) == {'action', 'config'}:
                from .config import parse
                parse(body['config'])
                return jsonify(call('wireguard', config=body['config']))
            if body.get('action') == 'privado' and set(body) == {'action', 'username', 'password', 'server'}:
                return jsonify(call('privado', username=body['username'], password=body['password'], server=body['server']))
            if body == {'action': 'disconnect'}: return jsonify(call('disconnect'))
            if body == {'action': 'connect'}: return jsonify(call('connect'))
            raise ValueError('Choose a supported VPN action.')
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
