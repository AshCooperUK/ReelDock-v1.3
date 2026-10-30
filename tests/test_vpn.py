"""VPN parser, security contract and fail-closed policy regressions."""
import base64
import importlib.util
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from backend.app import create_app
from backend import vpn
from backend.vpn.config import parse, wireguard_text
from backend.vpn.transport import DOWNLOAD, METADATA, traffic_class, downloads

KEY = base64.b64encode(bytes(range(32))).decode()
PROFILE = f'[Interface]\nPrivateKey = {KEY}\nAddress = 100.64.0.2/32\nDNS = 198.18.0.1,198.18.0.2\n[Peer]\nPublicKey = {KEY}\nAllowedIPs = 0.0.0.0/0\nEndpoint = 1.1.1.1:51820\n'
ORIGIN = 'https://vpn.example.test'
STATE = dict(available=True, connected=False, configured=False, state='Disconnected', external_ip='', message='', values=dict(downloads=False, metadata=False))


class ProfileTests(unittest.TestCase):
    def test_full_tunnel_and_no_secret_shell_directives(self):
        result = parse(PROFILE)
        self.assertEqual(result['dns'], ['198.18.0.1', '198.18.0.2'])
        clean = wireguard_text(result, result['endpoint'])
        self.assertNotIn('Address', clean)
        self.assertNotIn('DNS', clean)
        self.assertIn('FwMark = 51821', clean)
        for extra in ('PostUp = touch /tmp/unsafe', 'PreDown = anything', 'Table = auto', 'SaveConfig = true'):
            with self.assertRaises(ValueError): parse(PROFILE.replace('[Peer]', extra + '\n[Peer]'))

    def test_rejects_ambiguous_and_partial_profiles(self):
        for text in (PROFILE.replace('0.0.0.0/0', '10.0.0.0/8'), PROFILE.replace(KEY, 'invalid', 1),
                     PROFILE.replace('1.1.1.1', '127.0.0.1'), PROFILE + '[Peer]\nPublicKey=x',
                     PROFILE.replace('DNS = 198.18.0.1,198.18.0.2\n', ''), PROFILE.replace(':51820', ':65536'),
                     PROFILE.replace('Address =', 'PrivateKey ='), '[DEFAULT]\nx = y\n' + PROFILE,
                     PROFILE + 'x' * 20000):
            with self.assertRaises(ValueError): parse(text)

    def test_unknown_traffic_defaults_to_download_and_scopes_restore(self):
        self.assertEqual(traffic_class(), DOWNLOAD)
        namespace = {'__name__': 'backend.artwork', 'traffic_class': traffic_class}
        exec('def metadata(): return traffic_class()', namespace)
        self.assertEqual(namespace['metadata'](), METADATA)
        self.assertEqual(downloads(namespace['metadata'])(), DOWNLOAD)
        self.assertEqual(namespace['metadata'](), METADATA)


class VpnApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.app = create_app(dict(TESTING=True, DATA_DIR=self.tmp.name, MEDIA_DIR=self.tmp.name + '/media'))
        self.store = self.app.extensions['store']; self.store.set('public_origin', ORIGIN); self.store.set('setup_complete', '1')
        with self.store.connect() as db: db.execute("INSERT INTO users VALUES(1,'owner','unused')")
        self.client = self.app.test_client()
        with self.client.session_transaction(base_url=ORIGIN) as session: session.update(user_id=1, csrf='token')

    def post(self, body, endpoint='/api/vpn', csrf='token', origin=ORIGIN):
        return self.client.post(endpoint, base_url=ORIGIN, headers={'Origin': origin, 'X-CSRF-Token': csrf}, json=body)

    def test_auth_origin_and_csrf_are_required(self):
        self.assertEqual(self.app.test_client().get('/api/vpn', base_url=ORIGIN).status_code, 401)
        self.assertEqual(self.post({'action': 'disconnect'}, csrf='wrong').status_code, 403)
        self.assertEqual(self.post({'action': 'disconnect'}, origin='https://elsewhere.test').status_code, 403)

    def test_profile_validation_precedes_gateway_call_and_response_is_public(self):
        with patch('backend.vpn.call', return_value=STATE) as call:
            result = self.post({'action': 'wireguard', 'config': PROFILE})
            self.assertEqual(result.status_code, 200, result.json)
            self.assertNotIn(KEY, result.text)
            self.assertEqual(result.headers['Cache-Control'], 'no-store')
            self.assertEqual(call.call_count, 1)
            self.assertEqual(self.post({'action': 'wireguard', 'config': 'unsafe'}).status_code, 400)
            self.assertEqual(call.call_count, 1)

    def test_policy_uses_existing_engine_and_rejects_unavailable_gateway(self):
        with patch('backend.settings.vpn.call', return_value={**STATE, 'values': dict(downloads=True, metadata=False)}):
            result = self.post({'values': {'downloads': True}}, '/api/settings/vpn')
            self.assertEqual(result.status_code, 200, result.json)
            self.assertTrue(result.json['values']['downloads'])
        with patch('backend.settings.vpn.call', side_effect=ValueError('VPN unavailable.')):
            result = self.post({'values': {'downloads': True}}, '/api/settings/vpn')
            self.assertEqual(result.status_code, 409)
        for value in ({'downloads': 'true'}, {'metadata': 1}, {'password': 'test'}, {}):
            self.assertEqual(self.post({'values': value}, '/api/settings/vpn').status_code, 400)

    def test_disconnect_does_not_disable_switches(self):
        with patch('backend.vpn.call', return_value={**STATE, 'values': dict(downloads=True, metadata=True)}) as call:
            result = self.post({'action': 'disconnect'})
            self.assertTrue(result.json['values']['downloads']); self.assertTrue(result.json['values']['metadata'])
            call.assert_called_once_with('disconnect')

    def test_country_enrichment_is_authenticated_get_only_and_failure_keeps_connection(self):
        connected = {**STATE, 'connected': True, 'state': 'Connected', 'external_ip': '91.148.237.191'}
        with patch('backend.vpn.status', return_value=connected), patch('backend.vpn.exit_country', return_value='GB') as country:
            self.assertEqual(self.app.test_client().get('/api/vpn', base_url=ORIGIN).status_code, 401)
            country.assert_not_called()
            result = self.client.get('/api/vpn', base_url=ORIGIN)
            self.assertEqual(result.json, {**connected, 'country_code': 'GB'})
            country.assert_called_once_with('91.148.237.191')
            self.assertNotIn('country_code', connected)
        with patch('backend.vpn.status', return_value=connected), patch('backend.vpn.exit_country', side_effect=OSError('offline')):
            self.assertEqual(self.client.get('/api/vpn', base_url=ORIGIN).json, {**connected, 'country_code': ''})
        with patch('backend.vpn.status', return_value=STATE), patch('backend.vpn.exit_country') as country:
            self.assertEqual(self.client.get('/api/vpn', base_url=ORIGIN).json['country_code'], '')
            country.assert_not_called()
            vpn.status(); country.assert_not_called()
        with patch('backend.vpn.call', return_value=connected), patch('backend.vpn.exit_country') as country:
            self.assertEqual(self.post({'action': 'connect'}).status_code, 200)
            country.assert_not_called()

    def test_legacy_policy_reads_inherit_all_engines_without_persistence(self):
        with self.store.connect() as db:
            before = dict(db.execute('SELECT key,value FROM settings'))
        for legacy in (False, True):
            with patch('backend.vpn.call', return_value={**STATE, 'values': {'downloads': legacy, 'metadata': False}}) as call:
                response = self.client.get('/api/settings/vpn', base_url=ORIGIN)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json['values'], {**dict.fromkeys(vpn.ENGINE_KEYS, legacy), 'downloads': legacy, 'metadata': False})
                call.assert_called_once_with('status')
        with self.store.connect() as db:
            self.assertEqual(dict(db.execute('SELECT key,value FROM settings')), before)

    def test_engine_policy_changes_are_typed_partial_writes_with_legacy_alias(self):
        values = dict(youtube=False, nzb=True, torrent=True, vod=True, metadata=True, downloads=True)
        with patch('backend.settings.vpn.call', return_value={**STATE, 'values': values}) as call:
            result = self.post({'values': {'youtube': False}}, '/api/settings/vpn')
            self.assertEqual(result.status_code, 200, result.json)
            self.assertEqual(result.json['values'], values)
            call.assert_called_once_with('policy', values={'youtube': False})
        with patch('backend.settings.vpn.call', return_value={**STATE, 'values': {'downloads': False, 'metadata': True}}) as call:
            result = self.post({'values': {'downloads': False}}, '/api/settings/vpn')
            self.assertEqual(result.json['values'], {**dict.fromkeys(vpn.ENGINE_KEYS, False), 'downloads': False, 'metadata': True})
            call.assert_called_once_with('policy', values={'downloads': False})
        for body in ({'youtube': 1}, {'nzb': 'true'}, {'torrent': None}, {'vod': []},
                     {'youtube': False, 'password': 'PRIVATE'}, {'downloads': True, 'youtube': False}):
            with self.subTest(body=body), patch('backend.settings.vpn.call') as call:
                self.assertEqual(self.post({'values': body}, '/api/settings/vpn').status_code, 400)
                call.assert_not_called()
        with patch('backend.settings.vpn.call') as call:
            self.assertEqual(self.post({'values': {'nzb': False}}, '/api/settings/vpn', csrf='wrong').status_code, 403)
            self.assertEqual(self.post({'values': {'vod': False}}, '/api/settings/vpn', origin='https://elsewhere.test').status_code, 403)
            call.assert_not_called()

    def test_old_gateway_engine_write_refusal_never_changes_global_policy(self):
        with self.store.connect() as db:
            before = dict(db.execute('SELECT key,value FROM settings'))
        with patch('backend.settings.vpn.call', side_effect=ValueError('Unsupported policy.')) as call:
            response = self.post({'values': {'youtube': False}}, '/api/settings/vpn')
            self.assertEqual(response.status_code, 409)
            call.assert_called_once_with('policy', values={'youtube': False})
        with self.store.connect() as db:
            self.assertEqual(dict(db.execute('SELECT key,value FROM settings')), before)


class PolicyValuesTests(unittest.TestCase):
    def test_explicit_engines_are_independent_and_aggregate_is_derived(self):
        raw = dict(downloads=True, youtube=False, nzb=True, torrent=False, vod=False, metadata=True)
        before = dict(raw)
        self.assertEqual(vpn.policy_values(raw), raw)
        self.assertEqual(raw, before)
        self.assertEqual(vpn.policy_values(dict.fromkeys(vpn.ENGINE_KEYS, False)),
                         {**dict.fromkeys(vpn.ENGINE_KEYS, False), 'metadata': False, 'downloads': False})
        self.assertTrue(vpn.policy_values({'downloads': False, 'youtube': True})['downloads'])

    def test_malformed_present_flags_are_conservative(self):
        self.assertTrue(all(vpn.policy_values({'downloads': 'false'})[key] for key in vpn.ENGINE_KEYS))
        for invalid in ('false', 0, 1, None):
            with self.subTest(invalid=invalid):
                values = vpn.policy_values({'downloads': False, 'youtube': invalid, 'metadata': invalid})
                self.assertTrue(values['youtube']); self.assertTrue(values['metadata']); self.assertTrue(values['downloads'])
                self.assertFalse(values['nzb'])

    def test_partial_policy_without_legacy_flag_does_not_claim_unknown_engines_are_off(self):
        for raw in ({'nzb': True}, {'nzb': False}, {}, None):
            with self.subTest(raw=raw):
                values = vpn.policy_values(raw)
                self.assertTrue(values['youtube']); self.assertTrue(values['torrent']); self.assertTrue(values['vod'])
                self.assertTrue(values['downloads'])
        self.assertFalse(vpn.policy_values({'nzb': False})['nzb'])
        self.assertEqual(vpn.policy_values(dict.fromkeys(vpn.ENGINE_KEYS, False)),
                         {**dict.fromkeys(vpn.ENGINE_KEYS, False), 'downloads': False, 'metadata': False})


class ExitCountryTests(unittest.TestCase):
    def setUp(self):
        with vpn._country_lock:
            vpn._country_cache.clear()

    def test_fixed_https_request_is_metadata_routed_bounded_and_validates_response(self):
        class Socket:
            def settimeout(self, value):
                self.timeout = value
                self_test.assertGreater(value, 0); self_test.assertLessEqual(value, 2)
            def connect(self, address): self_test.assertEqual(address, ('8.8.8.8', 443))
            def close(self): pass
        class Response:
            status = 200
            def __init__(self, body): self.body = body
            def read1(self, count):
                part, self.body = self.body[:count], self.body[count:]
                return part
        self_test = self
        raw = Socket()
        def addresses(host, port, **kwargs):
            self.assertEqual((host, port), ('api.country.is', 443))
            self.assertEqual(traffic_class(), METADATA)
            return [(2, 1, 6, '', ('8.8.8.8', 443))]
        with patch('backend.vpn.socket.getaddrinfo', side_effect=addresses), patch('backend.vpn.socket.socket', return_value=raw), \
                patch('backend.vpn.ssl.create_default_context') as context, patch('backend.vpn.http.client.HTTPSConnection') as https:
            context.return_value.wrap_socket.return_value = raw
            cases = [({'ip': '91.148.237.191', 'country': 'GB'}, 'GB'),
                     ({'ip': '8.8.8.8', 'country': 'GB'}, ''),
                     ({'ip': '91.148.237.191', 'country': 'ZZ'}, ''),
                     ({'ip': '91.148.237.191', 'country': 'gb'}, ''),
                     ({'ip': '91.148.237.191', 'country': '<script>'}, '')]
            for data, expected in cases:
                with self.subTest(data=data):
                    https.return_value.getresponse.return_value = Response(json.dumps(data).encode())
                    self.assertEqual(vpn._country_request('91.148.237.191'), expected)
                    https.return_value.request.assert_called_with('GET', '/91.148.237.191', headers={'Accept': 'application/json'})
            for body in (b'x' * 4097, b'<html>invalid</html>'):
                https.return_value.getresponse.return_value = Response(body)
                self.assertEqual(vpn._country_request('91.148.237.191'), '')
            response = Response(b''); response.status = 302
            https.return_value.getresponse.return_value = response
            https.return_value.request.reset_mock()
            self.assertEqual(vpn._country_request('91.148.237.191'), '')
            self.assertEqual(https.return_value.request.call_count, 1, 'Redirects must never be followed')
            context.return_value.wrap_socket.assert_called_with(raw, server_hostname='api.country.is')
        self.assertEqual(traffic_class(), DOWNLOAD)

    def test_public_ip_validation_and_ipv6_cache_identity(self):
        with patch('backend.vpn._country_request', return_value='GB') as lookup:
            for address in ('', None, 'https://elsewhere.test/', '127.0.0.1', '10.0.0.1', '169.254.169.254', '224.0.0.1',
                            '::1', 'fe80::1', '2001:4860::1%eth0', '91.148.237.191/path'):
                self.assertEqual(vpn.exit_country(address), '', address)
            lookup.assert_not_called()
            self.assertEqual(vpn.exit_country('2001:4860:4860:0:0:0:0:8888'), 'GB')
            self.assertEqual(vpn.exit_country('2001:4860:4860::8888'), 'GB')
            lookup.assert_called_once_with('2001:4860:4860::8888')
        with patch('backend.vpn.socket.getaddrinfo', return_value=[(2,1,6,'',('127.0.0.1',443))]), patch('backend.vpn.socket.socket') as sock:
            self.assertEqual(vpn._country_request('91.148.237.191'), '')
            sock.assert_not_called()

    def test_cache_new_ip_failure_ttl_and_bounded_size(self):
        with patch('backend.vpn._country_request', side_effect=['GB', 'US', '', 'GB']) as lookup:
            self.assertEqual(vpn.exit_country('91.148.237.191'), 'GB')
            self.assertEqual(vpn.exit_country('91.148.237.191'), 'GB')
            self.assertEqual(vpn.exit_country('8.8.8.8'), 'US')
            self.assertEqual(vpn.exit_country('9.9.9.9'), '')
            self.assertEqual(vpn.exit_country('9.9.9.9'), '')
            with vpn._country_lock:
                # Completion callbacks run on the producer; wait for their cache publication.
                future = vpn._country_cache['9.9.9.9']
            if isinstance(future, vpn.Future): vpn._country_completed('9.9.9.9', future)
            with vpn._country_lock:
                expiry, _ = vpn._country_cache['9.9.9.9']
                self.assertGreater(expiry, time.monotonic() + 55)
                self.assertLess(expiry, time.monotonic() + 65)
                vpn._country_cache['9.9.9.9'] = (-1, '')
            self.assertEqual(vpn.exit_country('9.9.9.9'), 'GB')
            self.assertEqual(lookup.call_count, 4)
        with vpn._country_lock:
            vpn._country_cache.clear()
            for number in range(1, 17): vpn._country_cache['8.8.8.'+str(number)] = (time.monotonic()+3600, 'US')
        with patch('backend.vpn._country_request', return_value='GB'):
            self.assertEqual(vpn.exit_country('91.148.237.191'), 'GB')
        with vpn._country_lock:
            self.assertEqual(len(vpn._country_cache), 16)
            self.assertNotIn('8.8.8.1', vpn._country_cache)

    def test_concurrent_reads_coalesce_and_settings_wait_has_deadline(self):
        entered = threading.Event(); release = threading.Event()
        def blocked(_address):
            entered.set(); release.wait(2)
            return 'GB'
        with patch('backend.vpn._country_request', side_effect=blocked) as lookup, ThreadPoolExecutor(max_workers=4) as readers:
            results = [readers.submit(vpn.exit_country, '91.148.237.191') for _ in range(4)]
            self.assertTrue(entered.wait(1)); self.assertEqual(lookup.call_count, 1)
            release.set()
            self.assertEqual([result.result(1) for result in results], ['GB']*4)
        entered.clear(); release.clear()
        with patch('backend.vpn._country_request', side_effect=blocked), patch('backend.vpn.COUNTRY_TIMEOUT', 0.05):
            started = time.monotonic()
            try:
                self.assertEqual(vpn.exit_country('8.8.8.8'), '')
                self.assertLess(time.monotonic()-started, 0.25)
            finally: release.set()


class FirewallTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('rd_gateway', Path(__file__).resolve().parents[1] / 'runtime/vpn/gateway.py')
        self.gateway = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.gateway)

    def test_protected_connections_have_no_established_bypass(self):
        g = self.gateway
        g.CONFIG['values'] = dict(downloads=True, metadata=False)
        with patch.object(g, 'command') as cmd: g.firewall(False)
        rules = next(call.kwargs['data'] for call in cmd.call_args_list if 'data' in call.kwargs)
        self.assertIn('--mark 3329 -j REJECT', rules)
        self.assertNotIn('--mark 3330 -j MARK', rules)
        self.assertIn('--mark 3329 -j REJECT', rules)
        self.assertIn('--mark 3329 -j DROP', rules)
        self.assertNotIn('ESTABLISHED', rules)
        self.assertNotIn('--mark 3329 -o rdwg0 -j ACCEPT', rules)
        with patch.object(g, 'command') as cmd: g.firewall(True)
        self.assertIn('--mark 3329 -o rdwg0 -j ACCEPT', next(call.kwargs['data'] for call in cmd.call_args_list if 'data' in call.kwargs))

    def test_ipv6_protected_classes_are_rejected(self):
        g = self.gateway; g.CONFIG['values'] = dict(downloads=True, metadata=True)
        with patch.object(g, 'command') as cmd: g.ipv6_firewall()
        rules = cmd.call_args.kwargs['data']
        for mark in (DOWNLOAD, METADATA): self.assertIn(f'--mark {mark} -j REJECT', rules)
        self.assertIn('--hl-eq 255', rules)

    def test_ipv6_firewall_falls_back_without_hop_limit_match(self):
        g = self.gateway; g.CONFIG['values'] = dict(downloads=True, metadata=True)
        def restore(*args, data=None, optional=False):
            if '--hl-eq' in data: raise ValueError('missing hl match')
        with patch.object(g, 'HOP_LIMIT', True), patch.object(g, 'command', side_effect=restore) as cmd:
            g.ipv6_firewall(); g.ipv6_firewall()
            self.assertFalse(g.HOP_LIMIT)
        self.assertEqual(cmd.call_count, 3)
        rules = cmd.call_args.kwargs['data']
        self.assertNotIn('--hl-eq', rules)
        self.assertIn('--icmpv6-type 135 -j ACCEPT', rules)
        for mark in (DOWNLOAD, METADATA): self.assertIn(f'--mark {mark} -j REJECT', rules)

    def test_disconnect_keeps_blocking_policy(self):
        g = self.gateway; g.CONFIG['values'] = dict(downloads=True, metadata=True)
        with patch.object(g, 'command'), patch.object(g, 'save'):
            g.handle({'action': 'disconnect'})
        self.assertEqual(g.CONFIG['values'], dict(downloads=True, metadata=True))
        self.assertFalse(g.CONFIG['connect'])

    def test_zero_mark_never_becomes_a_catch_all_route(self):
        g = self.gateway; g.CONFIG['values'] = dict(downloads=True, metadata=False)
        with patch.object(g, 'command', return_value='') as cmd: g.routing_policy()
        routes = [call.args for call in cmd.call_args_list if 'add' in call.args]
        self.assertTrue(any('0/0xffffffff' in args for args in routes))
        self.assertFalse(any('fwmark' in args and args[args.index('fwmark') + 1] == '0' for args in routes))


class VpnPauseTests(unittest.TestCase):
    def test_engine_gate_uses_its_own_policy_and_unknown_traffic_uses_any(self):
        values = dict(youtube=False, nzb=True, torrent=True, vod=False, metadata=True, downloads=True)
        with tempfile.TemporaryDirectory() as folder, patch('backend.vpn.RUN', Path(folder)), patch.dict('os.environ', REELDOCK_VPN_TRANSPORT='1'):
            file = Path(folder) / 'status.json'
            file.write_text(json.dumps(dict(values=values, connected=False, checked_at=time.time())))
            self.assertFalse(vpn.downloads_blocked('youtube'))
            self.assertFalse(vpn.downloads_blocked('vod'))
            for engine in ('nzb', 'torrent', None, 'unknown'):
                self.assertTrue(vpn.downloads_blocked(engine), engine)
            file.write_text(json.dumps(dict(values=values, connected=True, checked_at=time.time())))
            for engine in (*vpn.ENGINE_KEYS, None, 'unknown'):
                self.assertFalse(vpn.downloads_blocked(engine), engine)
            file.write_text(json.dumps(dict(values=values, connected=True, checked_at=time.time()-60)))
            self.assertTrue(vpn.downloads_blocked('nzb'))
            self.assertFalse(vpn.downloads_blocked('youtube'))
            for legacy in (False, True):
                file.write_text(json.dumps(dict(values=dict(downloads=legacy, metadata=False), connected=False, checked_at=time.time())))
                for engine in (*vpn.ENGINE_KEYS, None, 'unknown'):
                    self.assertEqual(vpn.downloads_blocked(engine), legacy, engine)

    def test_unavailable_or_malformed_policy_never_becomes_an_unprotected_bypass(self):
        with tempfile.TemporaryDirectory() as folder, patch('backend.vpn.RUN', Path(folder)), patch.dict('os.environ', REELDOCK_VPN_TRANSPORT='1'):
            file = Path(folder) / 'status.json'
            for saved in ({}, [], {'values': {}},
                          dict(values=dict(downloads='false'), connected=False),
                          dict(values=dict(downloads=False, youtube=0), connected=False),
                          dict(values=dict(youtube=False), connected=False),
                          dict(values=dict(downloads=False), connected='true'),
                          dict(values=dict(downloads=False), connected=True, available='true'),
                          dict(values=dict(downloads=False), connected=False, available=False)):
                with self.subTest(saved=saved):
                    file.write_text(json.dumps(saved))
                    for engine in (*vpn.ENGINE_KEYS, None, 'unknown'):
                        self.assertTrue(vpn.downloads_blocked(engine), engine)
            file.write_text('{')
            self.assertTrue(vpn.downloads_blocked('youtube'))
            for checked in (True, 'fresh', float('inf'), float('nan'), 10**1000):
                file.write_text(json.dumps(dict(values=dict(downloads=True), connected=True, checked_at=checked)))
                self.assertTrue(vpn.downloads_blocked('youtube'))

    def test_missing_stale_and_disconnected_status_hold_downloads(self):
        from backend.vpn import downloads_blocked
        import time
        with tempfile.TemporaryDirectory() as folder, patch('backend.vpn.RUN', Path(folder)), patch.dict('os.environ', REELDOCK_VPN_TRANSPORT='1'):
            file = Path(folder) / 'status.json'
            self.assertTrue(downloads_blocked())
            file.write_text(json.dumps(dict(values=dict(downloads=True), connected=False, checked_at=time.time())))
            self.assertTrue(downloads_blocked())
            file.write_text(json.dumps(dict(values=dict(downloads=True), connected=True, checked_at=time.time() - 60)))
            self.assertTrue(downloads_blocked())
            file.write_text(json.dumps(dict(values=dict(downloads=True), connected=True, checked_at=time.time())))
            self.assertFalse(downloads_blocked())
            file.write_text(json.dumps(dict(values=dict(downloads=False), connected=False)))
            self.assertFalse(downloads_blocked())


class CachedVpnTests(unittest.TestCase):
    def test_stale_public_state_keeps_configuration_but_loses_connected_claim(self):
        from backend.vpn import cached_status
        import time
        with tempfile.TemporaryDirectory() as folder, patch('backend.vpn.RUN', Path(folder)):
            file=Path(folder)/'status.json'
            self.assertEqual(cached_status(), {})
            file.write_text(json.dumps(dict(configured=True, state='Connected', connected=True, checked_at=time.time()-60)))
            value=cached_status()
            self.assertTrue(value['configured'])
            self.assertFalse(value['connected'])
            self.assertEqual(value['state'],'Unavailable')
