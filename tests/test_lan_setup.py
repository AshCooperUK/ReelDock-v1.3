import json
import os
import re
import socket
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import lan_setup
from backend.app import create_app


def interfaces():
    return [dict(ifname='docker0', flags=['UP'], addr_info=[dict(local='172.18.0.1')]),
            dict(ifname='eth0', flags=['UP'], addr_info=[dict(local='192.168.50.10'), dict(local='fd12::10')]),
            dict(ifname='eth1', flags=[], addr_info=[dict(local='10.99.0.1')])]


@contextmanager
def root_file_metadata():
    # CI runs unprivileged: change only the fixture's reported owner. Actual
    # descriptors, mode bits, hardlinks, symlinks, bounds and timestamps remain real.
    original = os.fstat
    def owned(descriptor):
        value = original(descriptor)
        fields = {name: getattr(value, name) for name in dir(value) if name.startswith('st_')}
        return SimpleNamespace(**{**fields, 'st_uid': 0})
    with patch('backend.lan_setup.os.fstat', side_effect=owned):
        yield


class TrustedLanFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o755)
        self.path = self.root / 'lan-hosts.json'
        self.payload = lan_setup.collect_hosts(interfaces(), 'fixture-server',
                                              '127.0.1.1 fixture-server fixture-server.test\n192.168.90.2 other-machine.test')
        self.path.write_text(json.dumps(self.payload))
        self.path.chmod(0o644)

    def test_collector_uses_only_up_local_addresses_and_linked_aliases(self):
        hosts, default = lan_setup._snapshot(self.payload)
        self.assertEqual(default, '192.168.50.10')
        self.assertTrue({'localhost', '127.0.0.1', '::1', 'fixture-server.test', 'fd12::10', '172.18.0.1'} <= hosts)
        self.assertNotIn('other-machine.test', hosts)
        self.assertNotIn('10.99.0.1', hosts)
        rows = interfaces() + [dict(ifname='eth2', flags=['UP'], addr_info=[dict(local='1.1.1.1'), dict(local='192.168.80.2', flags=['tentative'])])]
        self.assertNotIn('1.1.1.1', lan_setup.collect_hosts(rows)['hosts'])
        self.assertNotIn('192.168.80.2', lan_setup.collect_hosts(rows)['hosts'])
        with self.assertRaises(ValueError):
            lan_setup.collect_hosts([])

    def test_loopback_aliases_require_an_exact_machine_identity_token(self):
        rows = '\n'.join(('127.0.0.1 localhost ads.attacker.test',
                          '127.0.1.1 fixture-server fixture-server.home.test',
                          '127.0.1.2 fixture-server.attacker.test',
                          '192.168.50.10 explicit-local-nic.test',
                          '192.168.50.99 other-machine.test'))
        hosts = lan_setup.collect_hosts(interfaces(), 'fixture-server.home.test', rows)['hosts']
        self.assertIn('fixture-server', hosts)
        self.assertIn('fixture-server.home.test', hosts)
        self.assertIn('explicit-local-nic.test', hosts)
        self.assertIn('localhost', hosts)
        self.assertNotIn('ads.attacker.test', hosts)
        self.assertNotIn('fixture-server.attacker.test', hosts)
        self.assertNotIn('other-machine.test', hosts)

    def test_validated_file_is_read_only_and_rejects_missing_unsafe_and_noncanonical_data(self):
        before = self.path.read_bytes()
        with root_file_metadata():
            self.assertEqual(lan_setup.read_hosts(self.path), lan_setup._snapshot(self.payload))
        self.assertEqual(self.path.read_bytes(), before)
        variants = [dict(self.payload, version=True), dict(self.payload, default_host='1.1.1.1'),
                    dict(self.payload, hosts=['*.test']), dict(self.payload, hosts=['fixture-server.test:8821']),
                    dict(self.payload, hosts=self.payload['hosts'] * 2)]
        for value in variants:
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value))
                with root_file_metadata(), self.assertRaises(ValueError):
                    lan_setup.read_hosts(self.path)
        self.path.write_text('{"version":1,"version":1}')
        with root_file_metadata(), self.assertRaises(ValueError):
            lan_setup.read_hosts(self.path)
        self.path.write_bytes(b' ' * (lan_setup.MAX_BYTES + 1))
        with root_file_metadata(), self.assertRaises(ValueError):
            lan_setup.read_hosts(self.path)
        self.path.unlink()
        with root_file_metadata(), self.assertRaises(ValueError):
            lan_setup.read_hosts(self.path)

    def test_real_symlinks_hardlinks_write_permissions_and_nonroot_owner_are_rejected(self):
        target = self.root / 'target'
        target.write_bytes(self.path.read_bytes())
        self.path.unlink()
        self.path.symlink_to(target)
        with root_file_metadata(), self.assertRaises(ValueError):
            lan_setup.read_hosts(self.path)
        self.path.unlink()
        os.link(target, self.path)
        with root_file_metadata(), self.assertRaises(ValueError):
            lan_setup.read_hosts(self.path)
        target.unlink()
        for mode in (0o664, 0o666):
            self.path.chmod(mode)
            with root_file_metadata(), self.assertRaises(ValueError):
                lan_setup.read_hosts(self.path)
        self.path.chmod(0o644)
        self.root.chmod(0o777)
        with root_file_metadata(), self.assertRaises(ValueError):
            lan_setup.read_hosts(self.path)
        self.root.chmod(0o755)
        if os.getuid() != 0:
            with self.assertRaises(ValueError):
                lan_setup.read_hosts(self.path)
        directory_link = self.root / 'linked-directory'
        directory_link.symlink_to(self.root, target_is_directory=True)
        with root_file_metadata(), self.assertRaises(ValueError):
            lan_setup.read_hosts(directory_link / self.path.name)

    def test_atomic_writer_and_cli_use_local_metadata_only(self):
        self.path.unlink()
        with root_file_metadata(), patch('backend.lan_setup.os.geteuid', return_value=0), \
                patch('backend.lan_setup.os.fsync', wraps=os.fsync) as sync:
            lan_setup.write_hosts(self.payload, self.path)
            self.assertEqual(sync.call_count, 2)
            self.assertEqual(lan_setup.read_hosts(self.path), lan_setup._snapshot(self.payload))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(sorted(item.name for item in self.root.iterdir()), ['lan-hosts.json'])
        with patch('backend.lan_setup.os.geteuid', return_value=10001), self.assertRaises(ValueError):
            lan_setup.write_hosts(self.payload, self.path)
        self.path.unlink()
        etc = self.root / 'etc'
        etc.mkdir()
        for name, value in [('hostname', 'fixture-server'), ('hosts', '127.0.1.1 fixture-server.test'), ('resolv.conf', '')]:
            (etc / name).write_text(value)
        with root_file_metadata(), patch('backend.lan_setup.os.geteuid', return_value=0), \
                patch.object(lan_setup, 'HOSTS_PATH', self.path), patch.object(lan_setup, 'HOST_ETC', etc), \
                patch('backend.lan_setup.subprocess.run', return_value=SimpleNamespace(stdout=json.dumps(interfaces()))) as command, \
                patch.object(socket, 'getaddrinfo', side_effect=AssertionError('No DNS lookup')):
            lan_setup.main()
        self.assertEqual(command.call_args.args[0], ['ip', '-j', 'address', 'show', 'up'])
        self.assertEqual(command.call_args.kwargs['timeout'], 5)

    def test_host_authority_parser_preserves_exact_origin_and_rejects_confusion(self):
        hosts, _ = lan_setup._snapshot(self.payload)
        self.assertEqual(lan_setup.origin_for('FIXTURE-SERVER.TEST:8821', 'http', 8821, hosts),
                         'http://fixture-server.test:8821')
        for authority in ('fixture-server.test', 'fixture-server.test:8790', 'fixture-server.test:8821/path',
                          'fixture-server.test:8821?x=1', 'fixture-server.test:8821#x',
                          'user@fixture-server.test:8821', '@fixture-server.test:8821', 'fixture-server.test:8821\t',
                          '*.test:8821', '[fd12::10%eth0]:8821'):
            self.assertIsNone(lan_setup.origin_for(authority, 'http', 8821, hosts), authority)


class AutomaticLanAppTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'lan-hosts.json'
        self.path.write_text(json.dumps(lan_setup.collect_hosts(interfaces(), 'fixture-server', '127.0.1.1 fixture-server fixture-server.test')))
        self.path.chmod(0o644)
        self.root.chmod(0o755)
        self.media = self.root / 'media'
        self.media.mkdir()
        self.config = dict(TESTING=True, DATA_DIR=str(self.root / 'data'), MEDIA_DIR=str(self.media / 'YouTube'),
                           M3U_DIR=str(self.media / 'M3U'), REELDOCK_STORAGE_ROOTS=json.dumps([str(self.media)]),
                           APP_PORT='8821', LOCAL_SETUP_URL='', AUTO_LAN_SETUP=True,
                           GOOGLE_CLIENT_ID='', GOOGLE_CLIENT_SECRET='')
        with patch.object(lan_setup, 'HOSTS_PATH', self.path), root_file_metadata():
            self.app = create_app(self.config)
        self.client = self.app.test_client()
        self.store = self.app.extensions['store']
        self.local = 'http://192.168.50.10:8821'

    def state(self, origin=None, **kwargs):
        return self.client.get('/api/setup/state', base_url=origin or self.local, **kwargs)

    def admin(self):
        csrf = self.state().json['csrf']
        result = self.client.post('/api/setup/admin', base_url=self.local,
                                  json=dict(username='fixture', password='long-fixture-password', confirm='long-fixture-password'),
                                  headers={'Origin': self.local, 'X-CSRF-Token': csrf})
        self.assertEqual(result.status_code, 200)
        return result.json['csrf']

    def test_automatic_lan_ip_alias_ipv6_and_cookie_contract(self):
        before = self.path.read_bytes()
        for origin in (self.local, 'http://fixture-server.test:8821', 'http://[fd12::10]:8821', 'http://localhost:8821'):
            self.assertEqual(self.state(origin, environ_overrides={'REMOTE_ADDR': '192.168.50.20'}).status_code, 200, origin)
        self.admin()
        cookie = self.client.get_cookie('reeldock_dev_local_8821', domain='192.168.50.10')
        self.assertIsNotNone(cookie)
        self.assertFalse(cookie.secure)
        self.assertTrue(cookie.http_only)
        self.assertEqual(self.path.read_bytes(), before)

    def test_wrong_host_port_scheme_proxy_public_peer_and_origin_cannot_create_account(self):
        for origin in ('http://evil.test:8821', 'http://192.168.50.11:8821', 'http://fixture-server.test:8790', 'https://fixture-server.test:8821'):
            self.assertEqual(self.state(origin).status_code, 403, origin)
        for headers in ({'X-Forwarded-Host': 'fixture-server.test:8821'}, {'Forwarded': 'for=192.168.50.20'}, {'X-Real-IP': '192.168.50.20'}):
            self.assertEqual(self.state(headers=headers).status_code, 403)
        self.assertEqual(self.state(environ_overrides={'REMOTE_ADDR': '1.1.1.1'}).status_code, 403)
        csrf = self.state().json['csrf']
        body = dict(username='fixture', password='long-fixture-password', confirm='long-fixture-password')
        for headers in ({'Origin': 'http://fixture-server.test:8821', 'X-CSRF-Token': csrf},
                        {'Origin': self.local}, {'Origin': self.local, 'X-CSRF-Token': 'wrong'}):
            self.assertEqual(self.client.post('/api/setup/admin', base_url=self.local, json=body, headers=headers).status_code, 403)
        self.assertIsNone(self.store.user())

    def test_snapshot_is_not_updated_from_requests_or_later_file_changes(self):
        payload = json.loads(self.path.read_text())
        payload['hosts'].append('new-untrusted.test')
        self.path.write_text(json.dumps(payload))
        before = self.path.read_bytes()
        self.assertEqual(self.state('http://new-untrusted.test:8821').status_code, 403)
        self.assertEqual(self.state().status_code, 200)
        self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_browser_alias_help_links_to_discovered_ip_without_trusting_it(self):
        response = self.client.get('/setup', base_url='http://unknown-browser-alias.test:8821')
        self.assertEqual(response.status_code, 403)
        data = json.loads(re.search(r'<script id="reeldock-bootstrap" type="application/json">(.*?)</script>',
                                    response.text).group(1))
        self.assertEqual(data['continue_url'], self.local + '/setup')
        self.assertIn(self.local + '/setup', data['message'])
        self.assertNotIn('unknown-browser-alias.test', data['continue_url'])
        self.assertIsNone(self.store.user())

    def test_public_route_is_separate_from_every_trusted_alias_and_keeps_secure_cookies(self):
        csrf = self.admin()
        with patch.dict(self.app.config, ROUTE_VERIFIER=lambda *_: None):
            rejected = self.client.post('/api/setup/verify-route', base_url=self.local,
                                        json={'url': 'https://fixture-server.test'},
                                        headers={'Origin': self.local, 'X-CSRF-Token': csrf})
            self.assertEqual(rejected.status_code, 400)
            self.assertEqual(self.store.get('public_origin'), '')
            accepted = self.client.post('/api/setup/verify-route', base_url=self.local,
                                        json={'url': 'https://public.test'},
                                        headers={'Origin': self.local, 'X-CSRF-Token': csrf})
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json['continue_url'], 'https://public.test/setup')
        self.assertEqual(self.client.get('/login', base_url='https://public.test').status_code, 200)
        public_cookie = self.client.get_cookie('reeldock_dev_session', domain='public.test')
        self.assertIsNotNone(public_cookie)
        self.assertTrue(public_cookie.secure)
        self.assertTrue(public_cookie.http_only)

    def test_explicit_url_has_priority_and_auto_requires_optin_and_safe_file(self):
        self.path.unlink()
        with patch.object(lan_setup, 'HOSTS_PATH', self.path):
            explicit = create_app({**self.config, 'LOCAL_SETUP_URL': 'http://explicit.test:8821'})
            self.assertEqual(explicit.test_client().get('/api/setup/state', base_url='http://explicit.test:8821').status_code, 200)
            self.assertEqual(explicit.test_client().get('/api/setup/state', base_url=self.local).status_code, 403)
            with self.assertRaises(ValueError):
                create_app(self.config)
            with self.assertRaises(ValueError):
                create_app({**self.config, 'AUTO_LAN_SETUP': False})

    def finish_local_setup(self):
        csrf = self.admin()
        for route, body in [('skip-route', {}), ('storage', {'base': str(self.media)}), ('source-ready', {}), ('finish', {})]:
            result = self.client.post('/api/setup/' + route, base_url=self.local, json=body,
                                      headers={'Origin': self.local, 'X-CSRF-Token': csrf})
            self.assertEqual(result.status_code, 200, route)
        self.assertEqual(self.state().status_code, 409)
        return csrf

    def test_local_mode_and_reset_use_trusted_request_origin(self):
        csrf = self.finish_local_setup()
        result = self.client.post('/api/account/reset', base_url=self.local,
                                  json=dict(password='long-fixture-password', confirmed=True),
                                  headers={'Origin': self.local, 'X-CSRF-Token': csrf})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['url'], self.local + '/setup')
        self.assertIsNone(self.store.user())

    def test_reset_from_verified_public_origin_returns_discovered_local_address(self):
        self.finish_local_setup()
        public = 'https://public.test'
        self.store.set('public_origin', public)
        with self.client.session_transaction(base_url=public) as session:
            session.update(user_id=1, auth_revision='0', csrf='reset-fixture')
        result = self.client.post('/api/account/reset', base_url=public,
                                  json=dict(password='long-fixture-password', confirmed=True),
                                  headers={'Origin': public, 'X-CSRF-Token': 'reset-fixture'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['url'], self.local + '/setup')
        self.assertEqual(self.store.get('public_origin'), '')
        self.assertIsNone(self.store.user())


if __name__ == '__main__':
    unittest.main()
