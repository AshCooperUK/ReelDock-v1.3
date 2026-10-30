"""Parse a deliberately small WireGuard dialect. Uploaded scripts never run."""
import base64
import configparser
import ipaddress
import re


def parse(text):
    if not isinstance(text, str) or len(text.encode()) > 16000:
        raise ValueError('Upload a WireGuard .conf file under 16 KB.')
    parser = configparser.ConfigParser(interpolation=None, strict=True, inline_comment_prefixes=('#', ';'))
    try:
        parser.read_string(text)
        if parser.sections() != ['Interface', 'Peer'] or parser.defaults():
            raise ValueError()
        interface, peer = parser['Interface'], parser['Peer']
        if set(interface) - {'privatekey', 'address', 'dns', 'mtu'} or set(peer) - {'publickey', 'presharedkey', 'allowedips', 'endpoint', 'persistentkeepalive'}:
            raise ValueError()
        def key(value):
            if len(base64.b64decode(value, validate=True)) != 32: raise ValueError()
            return value
        private = key(interface['privatekey'].strip())
        public = key(peer['publickey'].strip())
        psk = key(peer['presharedkey'].strip()) if peer.get('presharedkey') else ''
        addresses = [str(ipaddress.ip_interface(x.strip())) for x in interface['address'].split(',')]
        if not any(ipaddress.ip_interface(x).version == 4 for x in addresses): raise ValueError()
        dns = [str(ipaddress.ip_address(x.strip())) for x in interface['dns'].split(',')]
        if not dns or any(ipaddress.ip_address(x).version != 4 for x in dns): raise ValueError()
        allowed = [ipaddress.ip_network(x.strip()) for x in peer['allowedips'].split(',')]
        if ipaddress.ip_network('0.0.0.0/0') not in allowed: raise ValueError()
        endpoint = peer['endpoint'].strip()
        host, port = endpoint.rsplit(':', 1)
        if not re.fullmatch(r'[a-zA-Z0-9.-]{1,253}', host) or not 1 <= int(port) <= 65535: raise ValueError()
        try:
            if not ipaddress.ip_address(host).is_global: raise ValueError()
        except ValueError:
            if not re.fullmatch(r'(?=.{1,253}$)[a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z]{2,}', host): raise ValueError()
        mtu = int(interface.get('mtu', '1420'))
        keepalive = int(peer.get('persistentkeepalive', '25'))
        if not 1280 <= mtu <= 1500 or not 0 <= keepalive <= 65535: raise ValueError()
        return dict(private_key=private, public_key=public, preshared_key=psk, addresses=addresses,
                    dns=dns, endpoint=endpoint, mtu=mtu, keepalive=keepalive)
    except (ValueError, KeyError, configparser.Error):
        raise ValueError('Use a full-tunnel WireGuard profile with IPv4 Address, DNS, one Peer and AllowedIPs 0.0.0.0/0. Hooks and extra directives are not accepted.') from None


def wireguard_text(config, endpoint):
    text = '[Interface]\nPrivateKey = ' + config['private_key'] + '\nFwMark = 51821\n\n[Peer]\nPublicKey = ' + config['public_key']
    if config['preshared_key']: text += '\nPresharedKey = ' + config['preshared_key']
    return text + '\nAllowedIPs = 0.0.0.0/0\nEndpoint = ' + endpoint + '\nPersistentKeepalive = ' + str(config['keepalive']) + '\n'
