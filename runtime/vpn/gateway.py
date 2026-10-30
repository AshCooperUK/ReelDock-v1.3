"""ReelDock-owned WireGuard gateway. No wg-quick hooks, shell or proxy fallback.

Only this container has NET_ADMIN. App containers share its network namespace.
Traffic-class firewall changes are atomic, including for already-open sockets.
"""
import array
import concurrent.futures
import hashlib
import http.client
import ipaddress
import json
import os
import re
import secrets
import signal
import socket
import ssl
import struct
import subprocess
import threading
import time
from pathlib import Path
from backend.vpn.config import parse, wireguard_text

STATE = Path('/vpn-state')
RUN = Path('/vpn-run')
DOWNLOAD, METADATA, PROBE, TUNNEL = 0xD01, 0xD02, 0xD03, 0xD10
ENGINE_MARKS = {'youtube': 0xD11, 'nzb': 0xD12, 'torrent': 0xD13, 'vod': 0xD14}
SOCKET_MARKS = {DOWNLOAD, METADATA, *ENGINE_MARKS.values()}
LOCK = threading.RLock()
CONFIG = {'values': {'downloads': False, 'metadata': False}, 'profile': None, 'provider': '', 'connect': False}
STATUS = {'available': True, 'connected': False, 'state': 'Disconnected', 'external_ip': '', 'message': '',
          'check_stage': '', 'check_error': ''}
ACTIVE = ''
HOP_LIMIT = True
DNS =['1.1.1.1', '1.0.0.1']
READY = {key: threading.Event() for key in ('dns_udp', 'dns_tcp', 'dns_native', 'sockets')}


def policy_values(raw):
    """Old global protection becomes the default for every download engine."""
    if not isinstance(raw, dict) or set(raw) - {*ENGINE_MARKS, 'downloads', 'metadata'} or any(type(value) is not bool for value in raw.values()):
        raise ValueError('Invalid saved VPN policy; protection cannot be determined.')
    inherited = raw.get('downloads', not all(raw.get(engine) is False for engine in ENGINE_MARKS))
    result = {engine: raw.get(engine, inherited) for engine in ENGINE_MARKS}
    return {**result, 'downloads': any(result.values()), 'metadata': raw.get('metadata', False)}


def protected_marks():
    values = policy_values(CONFIG['values'])
    marks = [mark for engine, mark in ENGINE_MARKS.items() if values[engine]]
    if values['downloads']:
        marks += [DOWNLOAD, 0]  # Unknown/old clients never bypass another engine's protection.
    if values['metadata']:
        marks.append(METADATA)
    return marks


def command(*args, data=None, optional=False):
    result = subprocess.run(args, input=data, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    if result.returncode and not optional: raise ValueError('VPN network setup failed. Check NET_ADMIN and WireGuard support.')
    return result.stdout


def atomic(path, value, mode=0o600):
    temporary = path.with_name(path.name + '.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'w') as file:
        json.dump(value, file); file.flush(); os.fsync(file.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def snapshot():
    return {**STATUS, 'provider': CONFIG['provider'], 'configured': bool(CONFIG['profile']),
            'values': policy_values(CONFIG['values']), 'requested': CONFIG['connect'], 'endpoint': CONFIG['profile']['endpoint'] if CONFIG['profile'] else '',
            'checked_at': time.time()}


def publish():
    atomic(RUN / 'status.json', snapshot(), 0o644)


def firewall(healthy=False):
    # Loopback is only for the app, local DNS and health probes. No broad LAN or
    # ESTABLISHED exemption: existing direct download connections must be blocked
    # as soon as their switch is enabled. Only replies from the web port bypass.
    mangle = ['*mangle', ':RD_VPN_CLASS - [0:0]', '-F RD_VPN_CLASS',
              '-A RD_VPN_CLASS -p tcp --sport 8790 -m conntrack --ctdir REPLY -j RETURN',
              '-A RD_VPN_CLASS -j CONNMARK --save-mark']
    protected = protected_marks()
    mangle += ['COMMIT']
    rules = ['*filter', ':RD_VPN_OUT - [0:0]', '-F RD_VPN_OUT',
             '-A RD_VPN_OUT -o lo -j ACCEPT',
             '-A RD_VPN_OUT -p tcp --sport 8790 -m conntrack --ctdir REPLY -j ACCEPT']
    # Probe packets alone remain allowed while checking a blocked tunnel.
    if healthy:
        rules += [f'-A RD_VPN_OUT -m mark --mark {mark} -o rdwg0 -j ACCEPT' for mark in protected]
    inbound = [':RD_VPN_IN - [0:0]', '-F RD_VPN_IN', '-A RD_VPN_IN -i lo -j ACCEPT',
               '-A RD_VPN_IN -p tcp --dport 8790 -j ACCEPT']
    for mark in protected:
        if healthy: inbound.append(f'-A RD_VPN_IN -m connmark --mark {mark} -i rdwg0 -j ACCEPT')
        inbound.append(f'-A RD_VPN_IN -m connmark --mark {mark} -j DROP')
    rules += inbound
    rules += [f'-A RD_VPN_OUT -m mark --mark {PROBE} -o rdwg0 -j ACCEPT',
              *[f'-A RD_VPN_OUT -m mark --mark {mark} -j REJECT' for mark in protected],
              f'-A RD_VPN_OUT -m mark --mark {PROBE} -j REJECT', 'COMMIT']
    # Probes alone may use the protected route before the tunnel is verified.
    command('iptables-restore', '--noflush', data='\n'.join(mangle + rules) + '\n')
    routing_policy()


def routing_policy():
    # Route by the immutable socket class before selecting its source address.
    # Firewall goes first, so enabling has no unprotected transition window.
    current = command('ip', '-4', 'rule', 'show')
    values = policy_values(CONFIG['values'])
    classes = [(DOWNLOAD, 110, values['downloads']), (0, 111, values['downloads']),
               (METADATA, 112, values['metadata'])]
    classes += [(mark, 113 + index, values[engine]) for index, (engine, mark) in enumerate(ENGINE_MARKS.items())]
    for mark, priority, enabled in classes:
        existing = next((line for line in current.splitlines() if line.startswith(str(priority) + ':')), '')
        if enabled and 'fwmark' in existing and int(existing.split('fwmark ')[1].split()[0].split('/')[0], 0) == mark and 'lookup 51820' in existing: continue
        if not enabled and not existing: continue
        command('ip', '-4', 'rule', 'del', 'priority', str(priority), optional=True)
        if enabled: command('ip', '-4', 'rule', 'add', 'priority', str(priority), 'fwmark', str(mark) + '/0xffffffff', 'lookup', '51820')


def initialise_network():
    for table, chain in (('mangle', 'RD_VPN_CLASS'), ('filter', 'RD_VPN_OUT'), ('filter', 'RD_VPN_IN')):
        command('iptables', '-t', table, '-N', chain, optional=True)
        # A single namespace-scoped jump; never flush Docker or host rules.
        direction = 'INPUT' if chain == 'RD_VPN_IN' else 'OUTPUT'
        check = subprocess.run(['iptables', '-t', table, '-C', direction, '-j', chain], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if check.returncode: command('iptables', '-t', table, '-I', direction, '1', '-j', chain)
    command('ip', '-4', 'rule', 'del', 'priority', '50', optional=True)
    command('ip', '-4', 'rule', 'add', 'priority', '50', 'ipproto', 'tcp', 'sport', '8790', 'lookup', 'main')
    firewall(False)
    for mark, priority in ((TUNNEL, 100), (PROBE, 101)):
        command('ip', '-4', 'rule', 'del', 'priority', str(priority), optional=True)
        command('ip', '-4', 'rule', 'add', 'priority', str(priority), 'fwmark', str(mark) + '/0xffffffff', 'lookup', '51820')
    command('ip', '-4', 'route', 'replace', 'blackhole', 'default', 'metric', '32767', 'table', '51820')
    # No IPv6 tunnel is advertised: block protected IPv6, even on dual-stack hosts.
    for table, chain, direction in (('mangle', 'RD_VPN6_CLASS', 'OUTPUT'), ('filter', 'RD_VPN6', 'OUTPUT'), ('filter', 'RD_VPN6_IN', 'INPUT')):
        command('ip6tables', '-t', table, '-N', chain, optional=True)
        check = subprocess.run(['ip6tables', '-t', table, '-C', direction, '-j', chain], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if check.returncode: command('ip6tables', '-t', table, '-I', direction, '1', '-j', chain)
    ipv6_firewall()
    command('iptables', '-t', 'nat', '-N', 'RD_VPN_NAT', optional=True)
    check = subprocess.run(['iptables', '-t', 'nat', '-C', 'POSTROUTING', '-j', 'RD_VPN_NAT'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if check.returncode: command('iptables', '-t', 'nat', '-A', 'POSTROUTING', '-j', 'RD_VPN_NAT')
    command('iptables-restore', '--noflush', data='*nat\n:RD_VPN_NAT - [0:0]\n-F RD_VPN_NAT\n-A RD_VPN_NAT -o rdwg0 -j MASQUERADE\nCOMMIT\n')


def ipv6_firewall():
    global HOP_LIMIT
    if HOP_LIMIT:
        try: return command('ip6tables-restore', '--noflush', data=ipv6_rules(True))
        except ValueError: HOP_LIMIT = False
    # Some NAS kernels (ZimaOS 6.18) omit the hop-limit match; the fallback still
    # rejects every protected class and only loosens neighbour-discovery checks.
    command('ip6tables-restore', '--noflush', data=ipv6_rules(False))


def ipv6_rules(hop_limit):
    hl = ' -m hl --hl-eq 255' if hop_limit else ''
    lines = ['*mangle', ':RD_VPN6_CLASS - [0:0]', '-F RD_VPN6_CLASS',
             '-A RD_VPN6_CLASS -p tcp --sport 8790 -m conntrack --ctdir REPLY -j RETURN',
             '-A RD_VPN6_CLASS -j CONNMARK --save-mark', 'COMMIT',
             '*filter', ':RD_VPN6 - [0:0]', '-F RD_VPN6', '-A RD_VPN6 -o lo -j ACCEPT',
             '-A RD_VPN6 -p tcp --sport 8790 -m conntrack --ctdir REPLY -j ACCEPT',
             ':RD_VPN6_IN - [0:0]', '-F RD_VPN6_IN', '-A RD_VPN6_IN -i lo -j ACCEPT',
             '-A RD_VPN6_IN -p tcp --dport 8790 -j ACCEPT']
    # Local neighbour discovery carries no app payload; preserve direct metadata
    # networking while all protected IPv6 application sockets remain rejected.
    for kind in (133, 134, 135, 136):
        lines += [f'-A RD_VPN6 -p ipv6-icmp --icmpv6-type {kind}{hl} -j ACCEPT',
                  f'-A RD_VPN6_IN -p ipv6-icmp --icmpv6-type {kind}{hl} -j ACCEPT']
    for mark in protected_marks() + [PROBE, TUNNEL]:
        lines += [f'-A RD_VPN6 -m mark --mark {mark} -j REJECT',
                  f'-A RD_VPN6_IN -m connmark --mark {mark} -j DROP']
    lines += ['COMMIT']
    return '\n'.join(lines) + '\n'


def stop_tunnel():
    global ACTIVE
    firewall(False)
    command('ip', 'link', 'del', 'rdwg0', optional=True)
    ACTIVE = ''
    STATUS.update(connected=False, external_ip='', state='Disconnected', message='', check_stage='', check_error='')


def start_tunnel(profile):
    global ACTIVE, DNS
    stop_tunnel()
    host, port = profile['endpoint'].rsplit(':', 1)
    # Endpoint bootstrap is control-plane traffic, never a download exception.
    try: endpoint_ip = str(ipaddress.IPv4Address(host))
    except ValueError: endpoint_ip = resolve_host(host, False)
    if not ipaddress.ip_address(endpoint_ip).is_global: raise ValueError('The VPN endpoint must be a public IPv4 address.')
    keyfile = STATE / 'active.conf'
    fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as file: file.write(wireguard_text(profile, endpoint_ip + ':' + port))
    try:
        command('ip', 'link', 'add', 'rdwg0', 'type', 'wireguard')
        command('wg', 'setconf', 'rdwg0', str(keyfile))
        for address in profile['addresses']:
            if ipaddress.ip_interface(address).version == 4:
                command('ip', 'addr', 'add', address, 'dev', 'rdwg0')
        command('ip', 'link', 'set', 'rdwg0', 'mtu', str(profile['mtu']), 'up')
        command('ip', '-4', 'route', 'replace', 'default', 'dev', 'rdwg0', 'metric', '10', 'table', '51820')
        DNS = profile['dns']
        ACTIVE = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
        STATUS.update(state='Connecting', message='', check_stage='', check_error='')
    finally:
        keyfile.unlink(missing_ok=True)


def dns_exchange(query, protected, server=None):
    servers = [server] if server else (DNS if protected else ['1.1.1.1', '1.0.0.1'])
    for address in servers:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as upstream:
                upstream.settimeout(3)
                if protected: upstream.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, PROBE)
                upstream.connect((address, 53)); upstream.send(query)
                response = upstream.recv(65535)
                if response[:2] != query[:2]: continue
                # Support large DNSSEC/truncated replies using TCP, same route.
                if response[2] & 2:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
                        tcp.settimeout(3)
                        if protected: tcp.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, PROBE)
                        tcp.connect((address, 53)); tcp.sendall(struct.pack('!H', len(query)) + query)
                        size = struct.unpack('!H', receive(tcp, 2))[0]
                        response = receive(tcp, size)
                return response
        except OSError: continue
    return query[:2] + b'\x81\x82' + query[4:6] + b'\x00\x00\x00\x00\x00\x00' + query[12:]


def receive(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream.recv(size - len(data))
        if not chunk: raise OSError('Incomplete reply')
        data.extend(chunk)
    return bytes(data)


def resolve_host(host, protected):
    identity = secrets.token_bytes(2)
    labels = b''.join(bytes([len(x)]) + x.encode() for x in host.split('.')) + b'\x00'
    query = identity + b'\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00' + labels + b'\x00\x01\x00\x01'
    response = dns_exchange(query, protected)
    # Read DNS labels including compression pointers without following untrusted
    # pointers. Only A records in the answer section are used for the TLS probe.
    def skip(offset):
        for _ in range(128):
            length = response[offset]; offset += 1
            if length & 0xC0 == 0xC0: return offset + 1
            if length == 0: return offset
            offset += length
        raise ValueError()
    offset = skip(12) + 4
    for _ in range(struct.unpack('!H', response[6:8])[0]):
        offset = skip(offset)
        kind, cls, ttl, length = struct.unpack('!HHIH', response[offset:offset + 10]); offset += 10
        value = response[offset:offset + length]; offset += length
        if kind == 1 and cls == 1 and length == 4: return socket.inet_ntoa(value)
    raise ValueError('VPN DNS check failed.')


def _blocked(stage, error):
    """Expose fixed diagnostic labels; exceptions may contain private addresses."""
    labels = {'dns': 'VPN DNS check failed.', 'connect': 'VPN connection check failed.',
              'tls': 'VPN secure connection check failed.', 'public-address': 'VPN public address check failed.',
              'handshake': 'VPN handshake check failed.', 'firewall': 'VPN protection check failed.',
              'setup': 'VPN interface setup failed.'}
    if stage not in labels:stage = 'setup'
    category = ('timeout' if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)) else
                'tls' if isinstance(error, ssl.SSLError) else
                'invalid-response' if isinstance(error, (ValueError, UnicodeError)) else 'unavailable')
    STATUS.update(connected=False, external_ip='', state='Blocked',
                  message=labels[stage] + ' Protected traffic is blocked.', check_stage=stage, check_error=category)
    firewall(False)


def _tunnel_present():
    """Verify owned local tunnel setup without requiring a remote health check.

    A configured key alone is insufficient: a down link, missing peer/address
    or lost route needs local repair even when the profile hash is unchanged.
    WireGuard commonly reports operstate UNKNOWN while its UP flag is valid.
    """
    try:
        profile = CONFIG.get('profile') or {}
        key = command('wg', 'show', 'rdwg0', 'public-key', optional=True).strip()
        if not re.fullmatch(r'[A-Za-z0-9+/]{43}=', key):return False
        peer = profile.get('public_key', '')
        if not re.fullmatch(r'[A-Za-z0-9+/]{43}=', peer):return False
        if command('wg', 'show', 'rdwg0', 'peers', optional=True).split() != [peer]:return False
        allowed = command('wg', 'show', 'rdwg0', 'allowed-ips', optional=True).split()
        if not allowed or allowed[0] != peer or '0.0.0.0/0' not in allowed[1:]:return False
        links = json.loads(command('ip', '-j', 'link', 'show', 'dev', 'rdwg0', optional=True))
        if not isinstance(links, list) or len(links) != 1:return False
        if links[0].get('ifname') != 'rdwg0' or 'UP' not in links[0].get('flags', []):return False
        expected = {str(ipaddress.ip_interface(address)) for address in profile.get('addresses', [])
                    if ipaddress.ip_interface(address).version == 4}
        if not expected:return False
        addresses = json.loads(command('ip', '-j', '-4', 'addr', 'show', 'dev', 'rdwg0', optional=True))
        if not isinstance(addresses, list) or len(addresses) != 1:return False
        current = {str(ipaddress.ip_interface(str(value['local']) + '/' + str(value['prefixlen'])))
                   for value in addresses[0].get('addr_info', []) if value.get('family') == 'inet'}
        if current != expected:return False
        routes = json.loads(command('ip', '-j', '-4', 'route', 'show', 'table', '51820', 'default', optional=True))
        if not isinstance(routes, list):return False
        # start_tunnel owns metric 10; initialise_network also keeps a less
        # preferred blackhole default. A competing equal/better default is not
        # the verified local setup and must not strand failed checks forever.
        defaults = [route for route in routes if route.get('dst') in ('default', '0.0.0.0/0')]
        preferred = [route for route in defaults if int(route.get('metric', 0)) <= 10]
        return bool(preferred) and all(route.get('dev') == 'rdwg0' and int(route.get('metric', 0)) == 10
            and route.get('type', 'unicast') == 'unicast' and not route.get('gateway') and not route.get('nexthops')
            for route in preferred)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.TimeoutExpired):
        return False


def probe():
    stage = 'dns'
    try:
        address = resolve_host('api.ipify.org', True)
        stage = 'connect'
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, PROBE)
            raw.settimeout(6)
            raw.connect((address, 443))
            stage = 'tls'
            with ssl.create_default_context().wrap_socket(raw, server_hostname='api.ipify.org') as secure:
                stage = 'public-address'
                connection = http.client.HTTPConnection('api.ipify.org', timeout=6)
                connection.sock = secure
                connection.request('GET', '/', headers={'Host': 'api.ipify.org', 'Connection': 'close'})
                response = connection.getresponse()
                value = response.read(128).decode().strip()
                if response.status != 200 or not ipaddress.ip_address(value).is_global: raise ValueError()
        finally: raw.close()
        stage = 'handshake'
        handshakes = command('wg', 'show', 'rdwg0', 'latest-handshakes').split()
        if len(handshakes) < 2 or time.time() - int(handshakes[1]) > 180: raise ValueError()
        stage = 'firewall'
        firewall(True)
        STATUS.update(connected=True, external_ip=value, state='Connected', message='', check_stage='', check_error='')
    except Exception as exc:
        # Failed validation still blocks protected traffic immediately. Keep
        # the configured interface intact so a transient check failure does
        # not tear down every active connection on the next monitor pass.
        _blocked(stage, exc)


def privado_request(host, path, body=None, token=None, port=443):
    headers = {'Content-Type': 'application/json', 'User-Agent': 'ReelDock/1.2.1'}
    if token: headers['Authorization'] = 'Bearer ' + token
    connection = http.client.HTTPSConnection(host, port, timeout=12)
    try:
        address = resolve_host(host, False)
        if not ipaddress.ip_address(address).is_global: raise ValueError()
        raw = socket.create_connection((address, port), timeout=12)
        try: connection.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        except Exception: raw.close(); raise
        connection.request('POST' if body else 'GET', path, json.dumps(body) if body else None, headers)
        response = connection.getresponse(); data = response.read(2_000_001)
        if response.status != 200 or len(data) > 2_000_000: raise ValueError()
        result = json.loads(data)
        if isinstance(result, dict) and result.get('error'): raise ValueError()
        return result
    finally: connection.close()


def privado(username, password, server):
    if not all(isinstance(x, str) for x in (username, password, server)) or not username or not password or max(map(len, (username, password, server))) > 256:
        raise ValueError('Enter PrivadoVPN connection credentials and a server location.')
    try:
        api_key = os.environ['REELDOCK_PRIVADO_API_KEY'].strip()
    except KeyError:
        api_key = ''
    if not api_key:
        raise ValueError('Set REELDOCK_PRIVADO_API_KEY on the VPN service to use PrivadoVPN login, or upload a WireGuard profile.')
    try:
        response = privado_request('client-api.privado.io', '/v1/login',
            dict(api_key=api_key, username=username, password=password))
        token = response.get('access_token') or response.get('token')
        if not isinstance(token, str) or not token: raise ValueError()
        servers = privado_request('client-api.privado.io', '/v1/servers?nodes=all&includegeo=1', token=token)
        if isinstance(servers, dict): servers = servers['servers']
        query = server.strip().lower()
        country_codes = {'uk': 'gb', 'united kingdom': 'gb', 'great britain': 'gb', 'netherlands': 'nl',
                         'united states': 'us', 'germany': 'de', 'new zealand': 'nz', 'australia': 'au'}
        query = country_codes.get(query, query)
        candidates = [x for x in servers if isinstance(x, dict) and x.get('maintenance') is False
                      and (not query or query in ' '.join(str(x.get(k, '')).lower() for k in ('name', 'country', 'city', 'country_code')))]
        if not candidates: raise ValueError('No matching PrivadoVPN server. Try a country name.')
        host = candidates[0]['name']
        if not isinstance(host, str) or not host.endswith(('.privadovpn.com', '.vpn.privado.io')) or '/' in host or '@' in host: raise ValueError()
        address = resolve_host(host, False)
        if not ipaddress.ip_address(address).is_global: raise ValueError()
        result = privado_request(host, '/api/1.0/login', dict(Username=username, Password=password), port=44121)
        text = f"[Interface]\nPrivateKey = {result['WGPrivateKey']}\nAddress = {result['WGIPAddress'].split('/')[0]}/32\nDNS = 198.18.0.1,198.18.0.2\n[Peer]\nPublicKey = {result['ServerPublicKey']}\nAllowedIPs = 0.0.0.0/0\nEndpoint = {result['ServerIPAddress']}:{result.get('ServerListeningPort', 51820)}\n"
        return parse(text)
    except Exception:
        raise ValueError('PrivadoVPN login or profile retrieval failed. Check connection credentials, account access and location; or upload your WireGuard profile.') from None


def save():
    atomic(STATE / 'config.json', CONFIG)
    publish()


def handle(body):
    action = body.get('action')
    if action == 'status': return snapshot()
    if action == 'policy':
        values = body.get('values')
        if (not isinstance(values, dict) or not values or set(values) - {*ENGINE_MARKS, 'downloads', 'metadata'}
                or any(type(x) is not bool for x in values.values())
                or 'downloads' in values and set(values) & set(ENGINE_MARKS)):
            raise ValueError('Choose supported VPN switches.')
        old = policy_values(CONFIG['values'])
        updated = old.copy()
        if 'downloads' in values:
            updated.update({engine: values['downloads'] for engine in ENGINE_MARKS})
        updated.update({key: value for key, value in values.items() if key != 'downloads'})
        updated = policy_values(updated)
        try:
            # Block the union first. Neither an existing socket nor its replies
            # may use a direct route while a newly protected class is rerouted.
            CONFIG['values'] = {key: old[key] or updated[key] for key in old}
            firewall(False); ipv6_firewall()
            CONFIG['values'] = updated
            firewall(STATUS['connected']); ipv6_firewall(); save()
        except Exception:
            CONFIG['values'] = old
            firewall(False); ipv6_firewall()
            raise
    elif action in ('wireguard', 'privado'):
        profile = parse(body.get('config')) if action == 'wireguard' else privado(body.get('username'), body.get('password'), body.get('server'))
        CONFIG.update(profile=profile, provider='WireGuard' if action == 'wireguard' else 'PrivadoVPN', connect=True)
        save()
        try: start_tunnel(profile); probe()
        except Exception:
            stop_tunnel(); STATUS.update(state='Blocked', message='VPN could not connect. Protected traffic is blocked.')
        publish()
    elif action == 'disconnect':
        CONFIG['connect'] = False
        stop_tunnel(); save()
    elif action == 'connect':
        if not CONFIG['profile']: raise ValueError('Configure a VPN connection first.')
        CONFIG['connect'] = True; save()
        try: start_tunnel(CONFIG['profile']); probe()
        except Exception:
            stop_tunnel(); STATUS.update(state='Blocked', message='VPN could not connect. Protected traffic is blocked.')
        publish()
    else: raise ValueError('Choose a supported VPN action.')
    return snapshot()


def rpc(client):
    with client:
        client.settimeout(45)
        try:
            uid = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
            if uid not in (0, 10001): raise ValueError('VPN access denied.')
            data = bytearray()
            while not data.endswith(b'\n') and len(data) <= 20000:
                piece = client.recv(20001)
                if not piece: break
                data.extend(piece)
            if len(data) > 20000: raise ValueError('VPN request too large.')
            body = json.loads(data)
            if not isinstance(body, dict): raise ValueError('Invalid VPN request.')
            with LOCK: result = handle(body)
        except Exception as exc:
            result = {'error': str(exc) if isinstance(exc, ValueError) else 'VPN operation failed. Protected traffic remains blocked.'}
        try: client.sendall(json.dumps(result).encode() + b'\n')
        except OSError: pass


def socket_broker(client):
    with client:
        client.settimeout(3)
        try:
            uid = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
            if uid not in (0, 10001): raise ValueError()
            family, kind, proto, mark = struct.unpack('4i', receive(client, 16))
            if family not in (socket.AF_INET, socket.AF_INET6) or kind & 0xF not in (socket.SOCK_STREAM, socket.SOCK_DGRAM) or proto not in (0, 6, 17) or mark not in SOCKET_MARKS:
                raise ValueError()
            with socket.socket(family, kind, proto) as routed:
                routed.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, mark)
                client.sendmsg([b'1'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [routed.fileno()]))])
        except Exception:
            try: client.sendall(b'0')
            except OSError: pass


def sockets():
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path = RUN / 'socket.sock'; path.unlink(missing_ok=True)
    listener.bind(str(path)); os.chmod(path, 0o600); os.chown(path, 10001, 10001); listener.listen(64)
    READY['sockets'].set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        while True:
            client, _ = listener.accept(); pool.submit(socket_broker, client)


def dns_tcp():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', 53)); listener.listen(32)
    READY['dns_tcp'].set()
    def answer(client):
        with client:
            client.settimeout(10)
            try:
                size = struct.unpack('!H', receive(client, 2))[0]
                if not 12 <= size <= 4096: return
                query = receive(client, size)
                response = dns_exchange(query, bool(protected_marks()))
                client.sendall(struct.pack('!H', len(response)) + response)
            except OSError: pass
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        while True:
            client, _ = listener.accept(); pool.submit(answer, client)


def dns_udp():
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(('127.0.0.1', 53))
    READY['dns_udp'].set()
    def answer(query, address):
        if len(query) < 12: return
        listener.sendto(dns_exchange(query, bool(protected_marks())), address)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        while True:
            query, address = listener.recvfrom(4096)
            pool.submit(answer, query, address)


def native_addresses(host, family, flags, mark, *, deadline=None):
    """A native child's external DNS uses exactly its immutable socket class."""
    from backend.vpn import dns as resolver
    if mark not in SOCKET_MARKS:
        raise ValueError('Invalid native DNS class.')
    with LOCK:
        values = policy_values(CONFIG['values'])
        engine = next((engine for engine, value in ENGINE_MARKS.items() if value == mark), None)
        protected = values[engine] if engine else values['metadata'] if mark == METADATA else values['downloads']
        if protected and not STATUS['connected']:
            raise OSError('Protected DNS is unavailable.')
        servers = tuple(DNS if protected else resolver.RESOLVERS)
    name = resolver._hostname(host)
    # Leave IPC/encoding time inside the native child's five-second deadline.
    deadline = min(time.monotonic() + min(4.0, resolver.TIMEOUT), deadline if deadline is not None else float('inf'))
    queries = [1] if family == socket.AF_INET else [28] if family == socket.AF_INET6 else [1, 28]
    if family == socket.AF_INET6 and flags & getattr(socket, 'AI_V4MAPPED', 0):
        queries.append(1)

    def factory(*args):
        routed = socket.socket(*args)
        try:
            routed.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, mark)
            return routed
        except BaseException:
            routed.close()
            raise

    addresses = []; canonical = name; failed = False
    for query in queries:
        try:
            found, alias = resolver._resolve(name, query, factory, deadline, resolvers=servers)
            addresses.extend(found)
            if found:
                canonical = alias
        except (OSError, resolver.InvalidDNS):
            failed = True
    addresses = list(dict.fromkeys(addresses))
    if not addresses and failed:
        raise OSError('Native DNS is unavailable.')
    if len(addresses) > 128:
        raise OSError('Native DNS response is too large.')
    return addresses, canonical


def native_dns_broker(client, deadline=None):
    deadline = deadline if deadline is not None else time.monotonic() + 4.75
    with client:
        def budget():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Native DNS deadline exceeded.')
            return remaining
        def read(size):
            value = bytearray()
            while len(value) < size:
                client.settimeout(budget())
                part = client.recv(size - len(value))
                if not part:
                    raise OSError('Truncated native DNS request.')
                value.extend(part)
            return bytes(value)
        try:
            client.settimeout(budget())
            uid = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
            if uid not in (0, 10001):
                raise ValueError('DNS access denied.')
            mark, family, flags, length = struct.unpack('4i', read(16))
            if (mark not in SOCKET_MARKS or family not in (socket.AF_UNSPEC, socket.AF_INET, socket.AF_INET6)
                    or flags < 0 or flags & ~0x7ff or not 1 <= length <= 1024):
                raise ValueError('Invalid native DNS request.')
            host = read(length).decode('utf-8')
            addresses, canonical = native_addresses(host, family, flags, mark, deadline=deadline)
            encoded = canonical.encode('ascii')
            if len(encoded) > 253:
                raise ValueError('Native DNS name is too large.')
            records = []
            for address in addresses:
                value = ipaddress.ip_address(address)
                records.append(struct.pack('i16s', socket.AF_INET if value.version == 4 else socket.AF_INET6, value.packed))
            client.settimeout(budget())
            client.sendall(struct.pack('3i', 0 if records else 1, len(records), len(encoded)) + b''.join(records) + encoded)
        except Exception:
            try:
                client.settimeout(max(0.001, min(0.1, deadline - time.monotonic())))
                client.sendall(struct.pack('3i', 2, 0, 0))
            except OSError:
                pass


def dns_native():
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path = RUN / 'dns.sock'; path.unlink(missing_ok=True)
    listener.bind(str(path)); os.chmod(path, 0o600); os.chown(path, 10001, 10001); listener.listen(32)
    READY['dns_native'].set()
    capacity = threading.BoundedSemaphore(16)
    def answer(client, deadline):
        try:
            native_dns_broker(client, deadline)
        finally:
            capacity.release()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        while True:
            client, _ = listener.accept()
            if capacity.acquire(blocking=False):
                pool.submit(answer, client, time.monotonic() + 4.75)
            else:
                client.close()


def monitor_once():
    with LOCK:
        if CONFIG['connect'] and CONFIG['profile']:
            try:
                expected = hashlib.sha256(json.dumps(CONFIG['profile'], sort_keys=True).encode()).hexdigest()
                if expected != ACTIVE or not _tunnel_present():start_tunnel(CONFIG['profile'])
                probe()
            except Exception as exc:
                _blocked('setup', exc)
            publish()


def monitor():
    while True:
        time.sleep(10)
        monitor_once()


def main():
    original = socket.socket
    class ControlSocket(original):
        def __init__(self, family=-1, type=-1, proto=-1, fileno=None):
            super().__init__(family, type, proto, fileno)
            if fileno is None and self.family in (socket.AF_INET, socket.AF_INET6):
                self.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, 0xD04)
    socket.socket = ControlSocket
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    RUN.mkdir(parents=True, exist_ok=True)
    (RUN / 'ready').unlink(missing_ok=True)
    try:
        saved = json.loads((STATE / 'config.json').read_text())
        CONFIG.update(saved)
    except FileNotFoundError: pass
    CONFIG['values'] = policy_values(CONFIG['values'])
    # Invalid persistent state never silently disables protection.
    initialise_network()
    threading.Thread(target=dns_udp, daemon=True).start()
    threading.Thread(target=dns_tcp, daemon=True).start()
    threading.Thread(target=dns_native, daemon=True).start()
    threading.Thread(target=sockets, daemon=True).start()
    if not all(event.wait(5) for event in READY.values()):
        raise ValueError('VPN socket or DNS service could not start.')
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path = RUN / 'control.sock'; path.unlink(missing_ok=True)
    listener.bind(str(path)); os.chmod(path, 0o600); os.chown(path, 10001, 10001); listener.listen(8)
    publish(); (RUN / 'ready').touch()
    def shutdown(*_args):
        with LOCK:
            stop_tunnel(); publish()
        os._exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=monitor, daemon=True).start()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        while True:
            client, _ = listener.accept(); pool.submit(rpc, client)


if __name__ == '__main__': main()
