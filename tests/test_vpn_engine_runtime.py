"""Per-engine gateway policy and DNS contracts; no privileged commands/network."""
import base64
import importlib.util
import ipaddress
from pathlib import Path
import socket
import struct
import unittest
from unittest.mock import MagicMock, patch


class Peer:
    def __init__(self, body, uid=10001):
        self.body, self.uid, self.output = body, uid, b''

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def settimeout(self, _):
        pass

    def getsockopt(self, *_):
        return struct.pack('3i', 10, self.uid, 10001)

    def recv(self, size):
        part, self.body = self.body[:size], self.body[size:]
        return part

    def sendall(self, value):
        self.output += value

    def sendmsg(self, buffers, ancillary):
        self.output += b''.join(buffers)
        self.ancillary = ancillary


class EngineGatewayTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('vpn_engine_runtime_fixture',
            Path(__file__).resolve().parents[1] / 'runtime/vpn/gateway.py')
        self.g = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.g)

    def test_privado_missing_client_key_fails_before_network_and_preserves_state(self):
        g = self.g
        before = g.json.dumps([g.CONFIG, g.STATUS], sort_keys=True)
        for value in (None, '', '   '):
            environment = {} if value is None else {'REELDOCK_PRIVADO_API_KEY': value}
            with self.subTest(value=value), patch.dict(g.os.environ, environment, clear=True), \
                    patch.object(g, 'privado_request') as request, \
                    patch.object(g, 'resolve_host') as resolve, \
                    patch.object(g, 'command') as command, patch.object(g, 'save') as save:
                with self.assertRaisesRegex(ValueError, 'REELDOCK_PRIVADO_API_KEY'):
                    g.privado('fixture-user', 'fixture-password', 'United Kingdom')
                request.assert_not_called()
                resolve.assert_not_called()
                command.assert_not_called()
                save.assert_not_called()
            self.assertEqual(g.json.dumps([g.CONFIG, g.STATUS], sort_keys=True), before)

    def test_privado_configured_client_key_preserves_login_and_profile_flow(self):
        g = self.g
        key = base64.b64encode(bytes(range(32))).decode()
        before = g.json.dumps([g.CONFIG, g.STATUS], sort_keys=True)
        replies = [
            {'access_token': 'fixture-session-token'},
            [{'maintenance': False, 'name': 'fixture.privadovpn.com',
              'country': 'GB', 'city': 'Fixture', 'country_code': 'gb'}],
            {'WGPrivateKey': key, 'WGIPAddress': '10.0.0.2/32',
             'ServerPublicKey': key, 'ServerIPAddress': '1.1.1.1',
             'ServerListeningPort': 51820},
        ]
        with patch.dict(g.os.environ, {'REELDOCK_PRIVADO_API_KEY': '  fixture-client-key  '}), \
                patch.object(g, 'privado_request', side_effect=replies) as request, \
                patch.object(g, 'resolve_host', return_value='1.1.1.1') as resolve, \
                patch.object(g, 'command') as command, patch.object(g, 'save') as save:
            profile = g.privado('fixture-user', 'fixture-password', 'United Kingdom')
        self.assertEqual(request.call_count, 3)
        self.assertEqual(request.call_args_list[0].args,
            ('client-api.privado.io', '/v1/login', {'api_key': 'fixture-client-key',
             'username': 'fixture-user', 'password': 'fixture-password'}))
        self.assertEqual(request.call_args_list[1].kwargs, {'token': 'fixture-session-token'})
        self.assertEqual(request.call_args_list[2].args,
            ('fixture.privadovpn.com', '/api/1.0/login',
             {'Username': 'fixture-user', 'Password': 'fixture-password'}))
        self.assertEqual(request.call_args_list[2].kwargs, {'port': 44121})
        resolve.assert_called_once_with('fixture.privadovpn.com', False)
        self.assertEqual(profile['endpoint'], '1.1.1.1:51820')
        command.assert_not_called()
        save.assert_not_called()
        self.assertEqual(g.json.dumps([g.CONFIG, g.STATUS], sort_keys=True), before)

    def test_privado_configured_provider_failure_does_not_echo_credentials(self):
        g = self.g
        before = g.json.dumps([g.CONFIG, g.STATUS], sort_keys=True)
        with patch.dict(g.os.environ, {'REELDOCK_PRIVADO_API_KEY': 'fixture-client-key'}), \
                patch.object(g, 'privado_request',
                    side_effect=RuntimeError('fixture-client-key fixture-password')) as request, \
                patch.object(g, 'resolve_host') as resolve, patch.object(g, 'save') as save:
            with self.assertRaises(ValueError) as caught:
                g.privado('fixture-user', 'fixture-password', 'GB')
        self.assertIn('PrivadoVPN login or profile retrieval failed.', str(caught.exception))
        self.assertNotIn('fixture-client-key', str(caught.exception))
        self.assertNotIn('fixture-password', str(caught.exception))
        self.assertEqual(request.call_count, 1)
        resolve.assert_not_called()
        save.assert_not_called()
        self.assertEqual(g.json.dumps([g.CONFIG, g.STATUS], sort_keys=True), before)

    def test_legacy_switch_migrates_all_four_engines_and_unknown_stays_conservative(self):
        g = self.g
        for protected in (False, True):
            with self.subTest(protected=protected):
                values = g.policy_values(dict(downloads=protected, metadata=False))
                self.assertEqual({key: values[key] for key in g.ENGINE_MARKS}, dict.fromkeys(g.ENGINE_MARKS, protected))
                self.assertEqual(values['downloads'], protected)
                g.CONFIG['values'] = dict(downloads=protected, metadata=False)
                self.assertEqual(g.DOWNLOAD in g.protected_marks(), protected)
                self.assertEqual(0 in g.protected_marks(), protected)
        for engine in g.ENGINE_MARKS:
            g.CONFIG['values'] = {**dict.fromkeys(g.ENGINE_MARKS, False), engine: True, 'metadata': False}
            self.assertEqual(set(g.protected_marks()), {g.ENGINE_MARKS[engine], g.DOWNLOAD, 0})
        for partial in ({}, {'youtube': False}, {'nzb': True}):
            self.assertTrue(g.policy_values(partial)['downloads'])
        for invalid in (None, [], dict(downloads='true'), dict(downloads=True, nzb=1), dict(unknown=True)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                g.policy_values(invalid)

    def test_partial_policy_is_guarded_before_routes_and_legacy_write_remains_all_engines(self):
        g = self.g
        g.CONFIG['values'] = dict(downloads=True, metadata=False)
        g.STATUS['connected'] = True
        with patch.object(g, 'command', return_value='') as command, patch.object(g, 'save'):
            result = g.handle(dict(action='policy', values=dict(youtube=False)))
        self.assertFalse(result['values']['youtube'])
        self.assertTrue(all(result['values'][key] for key in ('nzb', 'torrent', 'vod', 'downloads')))
        restores = [value.kwargs['data'] for value in command.call_args_list if value.args[0] == 'iptables-restore']
        self.assertEqual(len(restores), 2)
        self.assertIn(f'--mark {g.ENGINE_MARKS["youtube"]} -j REJECT', restores[0])
        self.assertNotIn(f'--mark {g.ENGINE_MARKS["youtube"]} ', restores[1])
        self.assertIn(f'--mark {g.ENGINE_MARKS["nzb"]} -o rdwg0 -j ACCEPT', restores[1])
        self.assertNotIn('ESTABLISHED', '\n'.join(restores))
        routes = [value.args for value in command.call_args_list if value.args[:4] == ('ip', '-4', 'rule', 'add')]
        self.assertTrue(any('3346/0xffffffff' in value for value in routes))
        v6 = [value.kwargs['data'] for value in command.call_args_list if value.args[0] == 'ip6tables-restore'][-1]
        self.assertNotIn(f'--mark {g.ENGINE_MARKS["youtube"]} ', v6)
        self.assertIn(f'--mark {g.ENGINE_MARKS["nzb"]} -j REJECT', v6)
        self.assertIn(f'--mark {g.ENGINE_MARKS["nzb"]} -j DROP', v6)
        with patch.object(g, 'command', return_value=''), patch.object(g, 'save'):
            result = g.handle(dict(action='policy', values=dict(downloads=False)))
        self.assertFalse(any(result['values'].values()))

    def test_invalid_partial_and_failed_apply_never_acknowledge_a_weaker_policy(self):
        g = self.g
        before = g.policy_values(dict(downloads=True, metadata=True))
        g.CONFIG['values'] = before.copy()
        for values in ({}, dict(nzb='false'), dict(downloads=False, youtube=True), dict(secret=False)):
            with self.subTest(values=values), patch.object(g, 'command') as command, self.assertRaises(ValueError):
                g.handle(dict(action='policy', values=values))
            command.assert_not_called()
            self.assertEqual(g.CONFIG['values'], before)
        calls = []
        def firewall(healthy):
            calls.append((healthy, g.CONFIG['values'].copy()))
            if len(calls) == 2:
                raise ValueError('Synthetic atomic restore failure')
        with patch.object(g, 'firewall', side_effect=firewall), patch.object(g, 'ipv6_firewall'), patch.object(g, 'save') as save:
            with self.assertRaises(ValueError):
                g.handle(dict(action='policy', values=dict(youtube=False)))
        self.assertEqual(g.CONFIG['values'], before)
        self.assertEqual(calls[-1], (False, before))
        save.assert_not_called()

    def test_each_socket_class_preserves_mark_and_rejects_forged_classes_or_users(self):
        g = self.g
        for mark in g.SOCKET_MARKS:
            for family in (socket.AF_INET, socket.AF_INET6):
                peer = Peer(struct.pack('4i', family, socket.SOCK_STREAM, 0, mark))
                routed = MagicMock(); routed.__enter__.return_value = routed; routed.fileno.return_value = 55
                with patch.object(g.socket, 'socket', return_value=routed):
                    g.socket_broker(peer)
                self.assertEqual(peer.output, b'1')
                routed.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_MARK, mark)
        for mark, uid in ((g.PROBE, 10001), (g.TUNNEL, 10001), (g.DOWNLOAD, 99999)):
            peer = Peer(struct.pack('4i', socket.AF_INET, socket.SOCK_STREAM, 0, mark), uid=uid)
            with patch.object(g.socket, 'socket') as routed:
                g.socket_broker(peer)
            self.assertEqual(peer.output, b'0')
            routed.assert_not_called()

    def test_native_dns_uses_selected_engine_and_profile_without_direct_failover(self):
        from backend.vpn import dns
        g = self.g
        g.DNS = ['198.18.0.1', '198.18.0.2']
        g.CONFIG['values'] = {**dict.fromkeys(g.ENGINE_MARKS, True), 'youtube': False, 'metadata': False}
        seen = []
        def resolve(name, qtype, factory, deadline, *, resolvers):
            routed = factory(socket.AF_INET, socket.SOCK_DGRAM)
            seen.append((name, qtype, tuple(resolvers)))
            routed.close()
            return (['203.0.113.10'] if qtype == 1 else [], name)
        for connected in (False, True):
            g.STATUS['connected'] = connected
            for engine, mark in g.ENGINE_MARKS.items():
                with self.subTest(connected=connected, engine=engine):
                    raw = MagicMock()
                    with patch.object(dns, '_resolve', side_effect=resolve), patch.object(g.socket, 'socket', return_value=raw):
                        if engine != 'youtube' and not connected:
                            with self.assertRaises(OSError):
                                g.native_addresses('provider.example.test', socket.AF_INET, 0, mark)
                            raw.setsockopt.assert_not_called()
                        else:
                            self.assertEqual(g.native_addresses('provider.example.test', socket.AF_INET, 0, mark)[0], ['203.0.113.10'])
                            self.assertEqual(seen[-1][2], tuple(g.DNS if engine != 'youtube' else dns.RESOLVERS))
                            raw.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_MARK, mark)
        g.STATUS['connected'] = True
        with patch.object(dns, '_resolve', side_effect=OSError('Synthetic DNS failure')) as resolve:
            with self.assertRaises(OSError):
                g.native_addresses('provider.example.test', socket.AF_INET, 0, g.ENGINE_MARKS['nzb'])
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(resolve.call_args.kwargs['resolvers'], tuple(g.DNS))

    def test_native_dns_protocol_is_bounded_authenticated_and_has_fixed_errors(self):
        g = self.g; host = b'provider.example.test'
        header = struct.pack('4i', g.ENGINE_MARKS['youtube'], socket.AF_UNSPEC, socket.AI_CANONNAME, len(host))
        peer = Peer(header + host)
        with patch.object(g, 'native_addresses', return_value=(['203.0.113.10', '2001:db8::1'], 'canonical.example.test')) as resolve:
            g.native_dns_broker(peer)
        self.assertEqual(struct.unpack('3i', peer.output[:12]), (0, 2, 22))
        self.assertEqual(peer.output[16:20], ipaddress.ip_address('203.0.113.10').packed)
        self.assertEqual(resolve.call_args.args, (host.decode(), socket.AF_UNSPEC, socket.AI_CANONNAME, g.ENGINE_MARKS['youtube']))
        self.assertIn('deadline', resolve.call_args.kwargs)
        invalid = [(b'broken', 10001), (header + host, 99999),
                   (struct.pack('4i', g.PROBE, socket.AF_INET, 0, len(host)) + host, 10001),
                   (struct.pack('4i', g.DOWNLOAD, socket.AF_INET, 1 << 20, len(host)) + host, 10001),
                   (struct.pack('4i', g.DOWNLOAD, socket.AF_INET, 0, 1025), 10001)]
        for body, uid in invalid:
            with self.subTest(uid=uid, body=body[:16]):
                peer = Peer(body, uid)
                with patch.object(g, 'native_addresses') as resolve:
                    g.native_dns_broker(peer)
                self.assertEqual(peer.output, struct.pack('3i', 2, 0, 0))
                resolve.assert_not_called()

    def test_native_dns_uses_real_bounded_parser_with_same_class_tcp_retry(self):
        from backend.vpn import dns
        g = self.g
        g.CONFIG['values'] = {**dict.fromkeys(g.ENGINE_MARKS, True), 'youtube': False, 'metadata': False}
        g.STATUS['connected'] = True
        g.DNS = ['198.18.0.1']
        sockets = []

        class Upstream:
            def __init__(self, family, kind):
                self.family, self.kind, self.mark, self.buffer = family, kind, None, b''
                sockets.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

            def close(self):
                pass

            def settimeout(self, value):
                assert 0 < value <= dns.ATTEMPT_TIMEOUT

            def setsockopt(self, level, key, value):
                assert (level, key) == (socket.SOL_SOCKET, socket.SO_MARK)
                self.mark = value

            def connect(self, address):
                self.address = address

            def send(self, query):
                self.query = query

            def sendall(self, data):
                self.query = data[2:]
                answer = self.response(False)
                self.buffer = struct.pack('!H', len(answer)) + answer

            def response(self, truncated):
                _, offset = dns._name(self.query, 12)
                qtype = struct.unpack('!H', self.query[offset:offset + 2])[0]
                flags = 0x8380 if truncated else 0x8180
                count = 0 if truncated else 1
                header = self.query[:2] + struct.pack('!5H', flags, 1, count, 0, 0)
                if truncated:
                    return header + self.query[12:]
                address = ipaddress.ip_address('203.0.113.10' if qtype == 1 else '2001:db8::10').packed
                return header + self.query[12:] + b'\xc0\x0c' + struct.pack('!HHIH', qtype, 1, 60, len(address)) + address

            def recvfrom(self, _):
                return self.response(True), self.address

            def recv(self, size):
                value, self.buffer = self.buffer[:size], self.buffer[size:]
                return value

        for engine, expected_server in (('youtube', dns.RESOLVERS[0]), ('nzb', g.DNS[0])):
            with self.subTest(engine=engine):
                sockets.clear()
                with patch.object(g.socket, 'socket', side_effect=Upstream):
                    values, canonical = g.native_addresses('provider.example.test', socket.AF_UNSPEC, 0, g.ENGINE_MARKS[engine])
                self.assertEqual(values, ['203.0.113.10', '2001:db8::10'])
                self.assertEqual(canonical, 'provider.example.test')
                self.assertEqual([value.kind for value in sockets], [socket.SOCK_DGRAM, socket.SOCK_STREAM] * 2)
                self.assertTrue(all(value.mark == g.ENGINE_MARKS[engine] for value in sockets))
                self.assertTrue(all(value.address == (expected_server, 53) for value in sockets))
        peer = Peer(b'')
        with patch.object(g, 'native_addresses') as resolve:
            g.native_dns_broker(peer, deadline=0)
        self.assertEqual(peer.output, struct.pack('3i', 2, 0, 0))
        resolve.assert_not_called()


if __name__ == '__main__':
    unittest.main()
