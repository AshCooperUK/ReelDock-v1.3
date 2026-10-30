"""A failed remote health check blocks traffic without discarding a valid tunnel.

Every network, WireGuard/firewall command and publication seam is mocked. These
fault tests never use a real profile, interface, provider, route or firewall.
"""
from contextlib import contextmanager, ExitStack
import hashlib
import importlib.util
import json
from pathlib import Path
import ssl
import unittest
from unittest.mock import Mock, patch, call


class VpnMonitorRecoveryTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('vpn_monitor_recovery_fixture',
            Path(__file__).resolve().parents[1] / 'runtime/vpn/gateway.py')
        self.g = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.g)
        self.profile = {'endpoint': 'fixture.invalid:51820', 'public_key': 'P'*43+'=',
                        'addresses': ['10.20.30.2/32'], 'fixture': True}
        self.identity = hashlib.sha256(json.dumps(self.profile, sort_keys=True).encode()).hexdigest()
        self.g.CONFIG.update(connect=True, profile=self.profile)
        self.g.ACTIVE = self.identity
        self.g.STATUS.update(connected=True, state='Connected', external_ip='8.8.8.8')

    @contextmanager
    def seams(self):
        g = self.g
        raw, secure, connection, response = Mock(), Mock(), Mock(), Mock()
        response.status, response.read.return_value = 200, b'8.8.8.8'
        connection.getresponse.return_value = response
        tls = Mock()
        wrapped = Mock()
        wrapped.__enter__ = Mock(return_value=secure)
        wrapped.__exit__ = Mock(return_value=False)
        tls.wrap_socket.return_value = wrapped
        def command(*args, **kwargs):
            if args == ('wg', 'show', 'rdwg0', 'public-key'):
                self.assertEqual(kwargs, {'optional': True})
                return 'F'*43+'=\n'
            if args == ('wg', 'show', 'rdwg0', 'peers'):
                return self.profile['public_key']+'\n'
            if args == ('wg', 'show', 'rdwg0', 'allowed-ips'):
                return self.profile['public_key']+'\t0.0.0.0/0\n'
            if args == ('ip', '-j', 'link', 'show', 'dev', 'rdwg0'):
                return json.dumps([{'ifname': 'rdwg0', 'flags': ['UP', 'POINTOPOINT'], 'operstate': 'UNKNOWN'}])
            if args == ('ip', '-j', '-4', 'addr', 'show', 'dev', 'rdwg0'):
                return json.dumps([{'ifname': 'rdwg0', 'addr_info': [{'family': 'inet', 'local': '10.20.30.2', 'prefixlen': 32}]}])
            if args == ('ip', '-j', '-4', 'route', 'show', 'table', '51820', 'default'):
                return json.dumps([{'dst': 'default', 'dev': 'rdwg0', 'metric': 10},
                                   {'dst': 'default', 'type': 'blackhole', 'metric': 32767}])
            if args == ('wg', 'show', 'rdwg0', 'latest-handshakes'):
                return 'fixture-peer 999\n'
            self.fail('Unexpected privileged command: '+repr(args))
        with ExitStack() as stack:
            mocks = {
                'resolve': stack.enter_context(patch.object(g, 'resolve_host', return_value='8.8.8.8')),
                'socket': stack.enter_context(patch.object(g.socket, 'socket', return_value=raw)),
                'tls': stack.enter_context(patch.object(g.ssl, 'create_default_context', return_value=tls)),
                'http': stack.enter_context(patch.object(g.http.client, 'HTTPConnection', return_value=connection)),
                'command': stack.enter_context(patch.object(g, 'command', side_effect=command)),
                'firewall': stack.enter_context(patch.object(g, 'firewall')),
                'publish': stack.enter_context(patch.object(g, 'publish')),
                'start': stack.enter_context(patch.object(g, 'start_tunnel')),
            }
            stack.enter_context(patch.object(g.time, 'time', return_value=1000))
            yield dict(mocks, raw=raw, secure=secure, connection=connection, response=response, context=tls)

    def blocked(self, stage, category):
        g = self.g
        self.assertFalse(g.STATUS['connected'])
        self.assertEqual(g.STATUS['state'], 'Blocked')
        self.assertEqual(g.STATUS['external_ip'], '')
        self.assertEqual(g.STATUS['check_stage'], stage)
        self.assertEqual(g.STATUS['check_error'], category)
        self.assertIn('Protected traffic is blocked.', g.STATUS['message'])
        self.assertEqual(g.ACTIVE, self.identity)
        public = json.dumps(g.snapshot())
        for secret in ('PRIVATE_EXCEPTION', 'PRIVATE_KEY', 'credential.invalid'):
            self.assertNotIn(secret, public)

    def test_dns_failure_blocks_and_preserves_profile_without_opening_a_socket(self):
        with self.seams() as s:
            s['resolve'].side_effect = OSError('PRIVATE_EXCEPTION https://credential.invalid/?PRIVATE_KEY=secret')
            self.g.probe()
            self.blocked('dns', 'unavailable')
            s['firewall'].assert_called_once_with(False)
            s['socket'].assert_not_called()
            s['start'].assert_not_called()

    def test_connection_timeout_blocks_and_closes_its_probe_socket(self):
        with self.seams() as s:
            s['raw'].connect.side_effect = TimeoutError('PRIVATE_EXCEPTION')
            self.g.probe()
            self.blocked('connect', 'timeout')
            s['firewall'].assert_called_once_with(False)
            s['raw'].close.assert_called_once()

    def test_tls_failure_blocks_without_http_or_tunnel_restart(self):
        with self.seams() as s:
            s['context'].wrap_socket.side_effect = ssl.SSLError('PRIVATE_EXCEPTION PRIVATE_KEY')
            self.g.probe()
            self.blocked('tls', 'tls')
            s['http'].assert_not_called()
            s['firewall'].assert_called_once_with(False)
            s['start'].assert_not_called()

    def test_http_errors_and_non_global_or_invalid_ip_responses_stay_blocked(self):
        for status, body in [(503, b'8.8.8.8'), (200, b'PRIVATE_EXCEPTION'), (200, b'10.0.0.1')]:
            with self.subTest(status=status, body=body), self.seams() as s:
                s['response'].status, s['response'].read.return_value = status, body
                self.g.probe()
                self.blocked('public-address', 'invalid-response')
                s['firewall'].assert_called_once_with(False)

    def test_http_read_timeout_is_reported_without_recreating_the_interface(self):
        with self.seams() as s:
            s['response'].read.side_effect = TimeoutError('PRIVATE_EXCEPTION')
            self.g.probe()
            self.blocked('public-address', 'timeout')
            s['firewall'].assert_called_once_with(False)
            s['start'].assert_not_called()

    def test_missing_stale_or_malformed_handshake_never_marks_connected(self):
        for handshake in ('', 'fixture-peer 0', 'fixture-peer 800', 'fixture-peer invalid'):
            with self.subTest(handshake=handshake), self.seams() as s:
                s['command'].side_effect = None
                s['command'].return_value = handshake
                self.g.probe()
                self.blocked('handshake', 'invalid-response')
                s['firewall'].assert_called_once_with(False)

    def test_firewall_setup_failure_rolls_back_to_blocked(self):
        with self.seams() as s:
            s['firewall'].side_effect = [ValueError('PRIVATE_EXCEPTION'), None]
            self.g.probe()
            self.blocked('firewall', 'invalid-response')
            self.assertEqual(s['firewall'].call_args_list, [call(True), call(False)])

    def test_success_requires_every_original_check_and_clears_old_diagnostics(self):
        self.g.STATUS.update(connected=False, state='Blocked', check_stage='dns', check_error='timeout')
        with self.seams() as s:
            def firewall(healthy):
                self.assertTrue(healthy)
                self.assertFalse(self.g.STATUS['connected'], 'Connected follows successful firewall protection')
            s['firewall'].side_effect = firewall
            self.g.probe()
            self.assertTrue(self.g.STATUS['connected'])
            self.assertEqual(self.g.STATUS['state'], 'Connected')
            self.assertEqual(self.g.STATUS['external_ip'], '8.8.8.8')
            self.assertEqual(self.g.STATUS['check_stage'], '')
            self.assertEqual(self.g.STATUS['check_error'], '')
            s['resolve'].assert_called_once_with('api.ipify.org', True)
            s['raw'].setsockopt.assert_called_once_with(self.g.socket.SOL_SOCKET, self.g.socket.SO_MARK, self.g.PROBE)
            s['command'].assert_called_once_with('wg', 'show', 'rdwg0', 'latest-handshakes')
            s['firewall'].assert_called_once_with(True)

    def test_remote_failure_then_success_reuses_the_same_local_interface(self):
        with self.seams() as s:
            s['resolve'].side_effect = [OSError('PRIVATE_EXCEPTION'), '8.8.8.8']
            self.g.monitor_once()
            self.blocked('dns', 'unavailable')
            self.g.monitor_once()
            self.assertTrue(self.g.STATUS['connected'])
            self.assertEqual(self.g.ACTIVE, self.identity)
            s['start'].assert_not_called()
            self.assertEqual(s['firewall'].call_args_list, [call(False), call(True)])
            self.assertEqual(s['publish'].call_count, 2)

    def test_repeated_remote_failure_never_gets_a_grace_allowance_or_tunnel_restart(self):
        with self.seams() as s:
            s['resolve'].side_effect = TimeoutError('PRIVATE_EXCEPTION')
            for _ in range(3):
                self.g.monitor_once()
                self.blocked('dns', 'timeout')
            self.assertEqual(s['firewall'].call_args_list, [call(False)]*3)
            s['start'].assert_not_called()
            self.assertEqual(s['publish'].call_count, 3)

    def test_absent_local_interface_is_recreated_before_probing(self):
        with self.seams() as s:
            s['command'].side_effect = None
            s['command'].return_value = ''
            with patch.object(self.g, 'probe') as probe:
                self.g.monitor_once()
            s['start'].assert_called_once_with(self.profile)
            probe.assert_called_once()
            s['publish'].assert_called_once()

    def test_changed_profile_is_recreated_even_when_remote_check_previously_failed(self):
        self.g.ACTIVE = 'old-profile-identity'
        with self.seams() as s, patch.object(self.g, 'probe') as probe:
            self.g.monitor_once()
            s['start'].assert_called_once_with(self.profile)
            s['command'].assert_not_called()
            probe.assert_called_once()
            s['publish'].assert_called_once()

    def test_failed_interface_recreation_still_blocks_and_publishes_fixed_diagnostics(self):
        with self.seams() as s, patch.object(self.g, 'probe') as probe:
            s['command'].side_effect = None
            s['command'].return_value = ''
            s['start'].side_effect = OSError('PRIVATE_EXCEPTION PRIVATE_KEY')
            self.g.monitor_once()
            self.blocked('setup', 'unavailable')
            s['firewall'].assert_called_once_with(False)
            s['publish'].assert_called_once()
            probe.assert_not_called()

    def test_local_interface_check_errors_are_treated_as_unavailable(self):
        with self.seams() as s, patch.object(self.g, 'probe') as probe:
            s['command'].side_effect = ValueError('PRIVATE_EXCEPTION')
            self.g.monitor_once()
            s['start'].assert_called_once_with(self.profile)
            probe.assert_called_once()

    def test_unconfigured_or_malformed_local_interface_identity_is_recreated(self):
        for identity in ('(none)', 'malformed', ''):
            with self.subTest(identity=identity), self.seams() as s, patch.object(self.g, 'probe') as probe:
                s['command'].side_effect = None
                s['command'].return_value = identity
                self.g.monitor_once()
                s['start'].assert_called_once_with(self.profile)
                probe.assert_called_once()

    def assert_local_damage_recreated(self, broken_args, output):
        with self.seams() as s, patch.object(self.g, 'probe') as probe:
            original = s['command'].side_effect
            s['command'].side_effect = lambda *args, **kwargs: output if args == broken_args else original(*args, **kwargs)
            self.g.monitor_once()
            s['start'].assert_called_once_with(self.profile)
            probe.assert_called_once()
            s['publish'].assert_called_once()

    def test_valid_wireguard_key_with_link_down_is_recreated(self):
        self.assert_local_damage_recreated(('ip', '-j', 'link', 'show', 'dev', 'rdwg0'),
            json.dumps([{'ifname': 'rdwg0', 'flags': ['POINTOPOINT'], 'operstate': 'DOWN'}]))

    def test_missing_wrong_or_shadowed_protected_default_route_is_recreated(self):
        for routes in ([], [{'dst': 'default', 'dev': 'eth0', 'metric': 10}],
                       [{'dst': 'default', 'dev': 'rdwg0', 'metric': 20}],
                       [{'dst': 'default', 'dev': 'rdwg0', 'metric': 10}, {'dst': 'default', 'dev': 'eth0'}],
                       [{'dst': 'default', 'type': 'blackhole', 'metric': 10}]):
            with self.subTest(routes=routes):
                self.assert_local_damage_recreated(('ip', '-j', '-4', 'route', 'show', 'table', '51820', 'default'), json.dumps(routes))

    def test_missing_wrong_or_extra_peer_is_recreated_without_printing_keys(self):
        for peers in ('', 'W'*43+'=', self.profile['public_key']+'\n'+'W'*43+'='):
            with self.subTest(peer_count=len(peers.split())):
                self.assert_local_damage_recreated(('wg', 'show', 'rdwg0', 'peers'), peers)

    def test_missing_full_tunnel_allowed_ips_is_recreated(self):
        self.assert_local_damage_recreated(('wg', 'show', 'rdwg0', 'allowed-ips'), self.profile['public_key']+'\t10.0.0.0/8\n')

    def test_missing_or_changed_interface_address_is_recreated(self):
        for info in ([], [{'family': 'inet', 'local': '10.20.30.9', 'prefixlen': 32}]):
            with self.subTest(info=info):
                self.assert_local_damage_recreated(('ip', '-j', '-4', 'addr', 'show', 'dev', 'rdwg0'),
                    json.dumps([{'ifname': 'rdwg0', 'addr_info': info}]))

    def test_disconnect_or_unconfigured_monitor_does_not_probe_or_recreate(self):
        for values in ({'connect': False}, {'profile': None}):
            with self.subTest(values=values), self.seams() as s, patch.dict(self.g.CONFIG, values):
                self.g.monitor_once()
                s['resolve'].assert_not_called()
                s['command'].assert_not_called()
                s['start'].assert_not_called()
                s['publish'].assert_not_called()


if __name__ == '__main__':
    unittest.main()
