"""Trusted startup addresses for opt-in, direct LAN setup; never request discovery."""
import ipaddress
import json
import os
import re
import stat
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit

HOSTS_PATH = Path('/vpn-run/lan-hosts.json')
HOST_ETC = Path('/host/etc')
MAX_BYTES = 32768
MAX_HOSTS = 64
MAX_INPUT = 262144
NETWORKS = tuple(ipaddress.ip_network(value) for value in
                 ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7'))


def canonical_host(value):
    if not isinstance(value, str) or not value or len(value) > 253 or not value.isascii():
        raise ValueError('Invalid trusted LAN host.')
    if '%' in value:
        raise ValueError('Scoped LAN addresses are not supported.')
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        value = value.lower().removesuffix('.')
        if not value or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
                                for part in value.split('.')):
            raise ValueError('Invalid trusted LAN host.')
        return value


def _lan(address):
    return any(address in network for network in NETWORKS)


def _snapshot(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'hosts', 'default_host'}
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['hosts'], list) or not 1 <= len(value['hosts']) <= MAX_HOSTS):
        raise ValueError('Invalid trusted LAN setup metadata.')
    hosts = value['hosts']
    if any(canonical_host(host) != host for host in hosts) or len(set(hosts)) != len(hosts):
        raise ValueError('Invalid trusted LAN setup hosts.')
    default = value['default_host']
    try:
        address = ipaddress.ip_address(default)
    except (TypeError, ValueError):
        raise ValueError('Trusted LAN setup needs a local address.') from None
    if not _lan(address) or default not in hosts or address.compressed != default:
        raise ValueError('Trusted LAN setup needs a local address.')
    return frozenset(hosts), default


def _directory(path):
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(descriptor)
    if info.st_uid != 0 or info.st_mode & 0o022:
        os.close(descriptor)
        raise ValueError('Trusted LAN setup directory must be root-owned and protected.')
    return descriptor


def _file(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
            or info.st_mode & 0o022 or not 0 < info.st_size <= MAX_BYTES):
        raise ValueError('Trusted LAN setup file must be root-owned, protected and bounded.')


def read_hosts(path=None):
    path = HOSTS_PATH if path is None else Path(path)
    try:
        directory = _directory(path)
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(descriptor, 'rb') as source:
                before = os.fstat(source.fileno())
                _file(before)
                payload = source.read(MAX_BYTES + 1)
                after = os.fstat(source.fileno())
                _file(after)
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise ValueError('Trusted LAN setup metadata changed while reading.')
        finally:
            os.close(directory)
        if len(payload) > MAX_BYTES:
            raise ValueError('Trusted LAN setup metadata is too large.')
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError('Duplicate trusted LAN setup field.')
                value[key] = item
            return value
        return _snapshot(json.loads(payload, object_pairs_hook=unique))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError('Trusted LAN setup addresses are unavailable. Run storage preparation again.') from error


def origin_for(host_header, scheme, port, hosts):
    if (scheme != 'http' or not isinstance(host_header, str)
            or any(ord(character) <= 32 or ord(character) == 127 for character in host_header)):
        return None
    try:
        parsed = urlsplit('http://' + host_header)
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.path or parsed.query
                or parsed.fragment or parsed.port != port or canonical_host(parsed.hostname) not in hosts):
            return None
    except ValueError:
        return None
    # Preserve the browser's exact authority for the Origin comparison and cookie scope.
    return 'http://' + parsed.netloc.lower()


def default_origin(host, port):
    return 'http://' + ('[' + host + ']' if ':' in host else host) + ':' + str(port)


def collect_hosts(interfaces, hostname='', hosts_text=''):
    if not isinstance(interfaces, list):
        raise ValueError('Could not read local LAN interfaces.')
    addresses = set()
    candidates = []
    for interface in interfaces:
        if not isinstance(interface, dict) or 'UP' not in interface.get('flags', []):
            continue
        name = str(interface.get('ifname', ''))
        for row in interface.get('addr_info', []):
            if not isinstance(row, dict) or set(row.get('flags', [])) & {'tentative', 'deprecated', 'dadfailed'}:
                continue
            try:
                address = ipaddress.ip_address(row.get('local', ''))
            except ValueError:
                continue
            if not _lan(address):
                continue
            addresses.add(address.compressed)
            virtual = name.startswith(('lo', 'docker', 'br-', 'veth', 'virbr'))
            candidates.append((virtual, address.version != 4, name, address.packed, address.compressed))
    if not candidates:
        raise ValueError('No active private LAN address was found for setup.')
    default = min(candidates)[-1]
    aliases = {'localhost'}
    machine = hostname.strip().splitlines()
    machine = machine[0].strip() if machine else ''
    try:
        machine = canonical_host(machine)
        try:
            ipaddress.ip_address(machine)
        except ValueError:
            aliases.add(machine)
        else:
            machine = ''
    except ValueError:
        machine = ''
    identities = {machine, machine.split('.')[0]} if machine else set()
    for line in hosts_text.splitlines():
        fields = line.split('#', 1)[0].split()
        if len(fields) < 2:
            continue
        try:
            address = ipaddress.ip_address(fields[0])
        except ValueError:
            continue
        if not address.is_loopback and address.compressed not in addresses:
            continue
        row_aliases = set()
        for field in fields[1:]:
            try:
                alias = canonical_host(field)
                try:
                    ipaddress.ip_address(alias)
                except ValueError:
                    row_aliases.add(alias)
            except ValueError:
                pass
        # Loopback also contains adblocking entries. Only a machine identity
        # row, not a matching first label of an unrelated FQDN, binds aliases.
        if not address.is_loopback or row_aliases & identities:
            aliases.update(row_aliases)
    mandatory = {default, '127.0.0.1', '::1', 'localhost'}
    ordered = sorted(mandatory) + sorted((addresses | aliases) - mandatory)
    payload = dict(version=1, hosts=ordered[:MAX_HOSTS], default_host=default)
    _snapshot(payload)
    return payload


def write_hosts(payload, path=None):
    if os.geteuid() != 0:
        raise ValueError('Trusted LAN setup preparation must run as root.')
    _snapshot(payload)
    encoded = (json.dumps(payload, separators=(',', ':')) + '\n').encode()
    if len(encoded) > MAX_BYTES:
        raise ValueError('Trusted LAN setup metadata is too large.')
    path = HOSTS_PATH if path is None else Path(path)
    directory = _directory(path)
    temporary = '.lan-hosts-' + uuid.uuid4().hex + '.tmp'
    try:
        try:
            old = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _file(old)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(encoded)
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            os.fsync(output.fileno())
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def main():
    result = subprocess.run(['ip', '-j', 'address', 'show', 'up'], capture_output=True, text=True,
                            check=True, timeout=5)
    if len(result.stdout.encode()) > MAX_INPUT:
        raise ValueError('Local interface metadata is too large.')
    def text(name):
        with (HOST_ETC / name).open(encoding='utf-8') as source:
            value = source.read(MAX_INPUT + 1)
        if len(value) > MAX_INPUT:
            raise ValueError('Local host metadata is too large.')
        return value
    write_hosts(collect_hosts(json.loads(result.stdout), text('hostname'), text('hosts')))
    print('Prepared trusted LAN setup addresses.')


if __name__ == '__main__':
    main()
