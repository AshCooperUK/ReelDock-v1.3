"""V1.3 development templates isolate storage, ports and Docker namespaces."""
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from backend.app import create_app, VERSION

try:
    import yaml
except ImportError:  # CI installs PyYAML; the runtime image does not need it.
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
INSTALL_TEMPLATE = '${REELDOCK_INSTALL_DIR:-/DATA/AppData/reeldock-v13}'
PRIVADO_TEMPLATE = '${REELDOCK_PRIVADO_API_KEY:-}'
LOCAL = Path('/DATA/AppData/reeldock-v13')
CASAOS_MEDIA = Path('/DATA/Media/reeldock-v13/data')
LOCAL_IMAGE = 'reeldock:1.3-development'
RELEASE_IMAGE = 'ghcr.io/ashcooperuk/reeldock-v1.3:' + VERSION.removeprefix('v')
# Paths and names used by the other ReelDock installation on the same NAS.
OTHER_DATA = '/DATA/AppData/ytsd'
OTHER_MEDIA = '/DATA/Media/YTSD/'
OTHER_NAMES = {'ytsd', 'ytsd-web', 'ytsd-worker'}


@unittest.skipIf(yaml is None, 'PyYAML is not installed')
class DeploymentConfigTests(unittest.TestCase):
    def load(self, name, install=LOCAL):
        # Resolve only the two public-template variables, without reading local
        # environment credentials or requiring Docker for pure YAML assertions.
        text = (ROOT / name).read_text().replace(INSTALL_TEMPLATE, str(install))
        return yaml.safe_load(text.replace(PRIVADO_TEMPLATE, ''))

    def host_paths(self, service):
        return [volume.split(':', 1)[0] for volume in service.get('volumes', [])]

    def check_isolated(self, config, image=LOCAL_IMAGE, media=LOCAL / 'media'):
        self.assertEqual(config['name'], 'reeldock-v13')
        services = config['services']
        for role in ('vpn','web','worker'):
            self.assertEqual(services[role]['container_name'], 'reeldock-v13-'+role)
        names = {config['name']} | {s.get('container_name') for s in services.values()}
        self.assertFalse(names & OTHER_NAMES)
        allowed={'vpn-run','vpn-state',str(media),*(str(LOCAL / child) for child in ('data','media/YouTube','media/M3U','vpn-run','vpn-state'))}
        for name, service in services.items():
            self.assertEqual(service['image'],image)
            for path in self.host_paths(service):
                with self.subTest(service=name, path=path):
                    self.assertIn(path,allowed)
                    self.assertNotEqual(path, OTHER_DATA)
                    self.assertFalse(path.startswith(OTHER_MEDIA))
            self.assertNotIn('ashjohn', json.dumps(service))
        self.assertIn(config['x-casaos']['title']['en_US'], ('ReelDock V1.3',))
        self.assertEqual(config['x-casaos']['port_map'], '8821')
        self.assertEqual(services['vpn']['ports'],['127.0.0.1:8821:8790'])
        self.assertEqual(services['web']['environment']['REELDOCK_HOST_ROOT'],str(media))
        self.assertEqual(services['web']['environment'],services['worker']['environment'])
        return services

    def test_casaos_compose_uses_fresh_fixed_media_layout(self):
        config = self.load('compose.casaos.yaml')
        raw = yaml.safe_load((ROOT / 'compose.casaos.yaml').read_text())
        services = self.check_isolated(config, RELEASE_IMAGE, CASAOS_MEDIA)
        env = services['web']['environment']
        self.assertEqual(services['worker']['environment'], env)
        self.assertEqual(services['vpn']['ports'], ['127.0.0.1:8821:8790'])
        self.assertEqual(env['APP_URL'], '')
        self.assertEqual(env['APP_PORT'], '8821')
        self.assertEqual(env['LOCAL_SETUP_URL'], 'http://127.0.0.1:8821')
        self.assertEqual(env['REELDOCK_MEDIA_ROOT'], '/reeldock')
        self.assertEqual(json.loads(env['REELDOCK_STORAGE_ROOTS']), ['/reeldock'])
        self.assertEqual(env['REELDOCK_HOST_ROOT'], str(CASAOS_MEDIA))
        for legacy in ('MEDIA_DIR', 'M3U_DIR', 'M3U_DOWNLOAD_ROOTS', 'MEDIA_HOST_DIR',
                       'REELDOCK_DOWNLOAD_BASE', 'REELDOCK_STORAGE_LOCATIONS'):
            self.assertNotIn(legacy, env)
        for role in ('web', 'worker'):
            self.assertEqual(services[role]['volumes'], [str(LOCAL / 'vpn-run')+':/vpn-run:ro',
                             str(CASAOS_MEDIA)+':/reeldock', str(LOCAL / 'data')+':/data'])
            self.assertEqual(raw['services'][role]['volumes'], services[role]['volumes'])
            self.assertNotIn('/vpn-state', [volume.split(':')[1] for volume in services[role]['volumes']])
        for role, service in raw['services'].items():
            for volume in service['volumes']:
                # CasaOS edits host/container/permission fields independently.
                source, target, *mode = volume.split(':')
                self.assertTrue(source and Path(source).is_absolute(), (role, volume))
                self.assertTrue(target.startswith('/'), (role, volume))
                self.assertNotIn('${', source, 'CasaOS mounts must not require interpolation')
                self.assertIn(mode, ([], ['ro']))
                edited = dict(source=source, target=target, read_only=mode == ['ro'])
                restored = edited['source']+':'+edited['target']+(':ro' if edited['read_only'] else '')
                self.assertEqual(restored, volume, 'Host/container/permission editor roundtrip keeps the complete mount')
        self.assertEqual(services['vpn']['volumes'], [str(LOCAL / 'vpn-state')+':/vpn-state',
                                                     str(LOCAL / 'vpn-run')+':/vpn-run'])
        self.assertEqual(services['vpn']['depends_on']['prepare-storage']['condition'], 'service_completed_successfully')
        self.assertEqual(raw['services']['web']['environment']['REELDOCK_HOST_ROOT'], str(CASAOS_MEDIA))
        self.assertEqual(self.load('compose.casaos.yaml', Path('/srv/reeldock-fixture')), config,
                         'The CasaOS template keeps explicit host paths independent of source-build overrides')
        prepare = services['prepare-storage']
        self.assertEqual(prepare['command'], ["import os; [os.chown(p,10001,10001) for p in ('/data','/reeldock')]; os.chmod('/data',0o700); [os.chown(p,0,0) for p in ('/vpn-run','/vpn-state')]; os.chmod('/vpn-run',0o755); os.chmod('/vpn-state',0o700)"])
        self.assertEqual(prepare['volumes'], [str(LOCAL / 'vpn-run')+':/vpn-run',
                         str(LOCAL / 'vpn-state')+':/vpn-state', str(CASAOS_MEDIA)+':/reeldock',
                         str(LOCAL / 'data')+':/data'])
        self.assertEqual(prepare['cap_add'], ['CHOWN', 'FOWNER'])
        self.assertEqual(prepare['user'], '0:0')
        self.assertEqual(prepare['restart'], 'no')
        self.assertTrue(prepare['healthcheck']['disable'])
        for role, memory in (('web', '1GB'), ('worker', '2GB')):
            self.assertEqual(services[role]['deploy']['resources']['limits'], {'memory': memory, 'cpus': '1.00'})
        icon = urlsplit(config['x-casaos']['icon'])
        self.assertEqual(icon.netloc, urlsplit(env['LOCAL_SETUP_URL']).netloc)
        self.assertEqual(icon.path, '/preview/assets/reeldock-brand.svg')
        self.assertTrue((ROOT / 'frontend/public/assets/reeldock-brand.svg').is_file())

    def test_casaos_environment_is_accepted_by_the_app(self):
        env = self.load('compose.casaos.yaml')['services']['web']['environment']
        with tempfile.TemporaryDirectory() as folder:
            media = Path(folder) / 'media'
            media.mkdir()
            app = create_app({**env, 'TESTING': True, 'DATA_DIR': str(Path(folder) / 'data'),
                              'REELDOCK_MEDIA_ROOT': str(media), 'REELDOCK_STORAGE_ROOTS': json.dumps([str(media)]),
                              'CANDIDATE_URL': env['APP_URL']})
            client = app.test_client()
            store = app.extensions['store']
            self.assertEqual(client.get('/setup', base_url=env['LOCAL_SETUP_URL']).status_code, 200)
            self.assertEqual(client.get('/setup', base_url='http://127.0.0.1:8822').status_code, 403)
            self.assertEqual(client.get('/preview/assets/reeldock-brand.svg', base_url=env['LOCAL_SETUP_URL']).status_code, 200)
            state = client.get('/api/setup/state', base_url=env['LOCAL_SETUP_URL']).json
            self.assertTrue(state['storage']['locked'])
            self.assertTrue(state['storage']['ready'])
            self.assertEqual(state['storage']['host_path'], str(CASAOS_MEDIA))
            self.assertEqual(app.config['MEDIA_DIR'], str(media))
            self.assertEqual(app.config['M3U_DIR'], str(media))
            self.assertEqual(json.loads(app.config['M3U_DOWNLOAD_ROOTS']), [str(media / name) for name in ('Movies', 'TV Shows')])
            self.assertEqual(set(p.name for p in media.iterdir()), {'Auto-Download', 'TV Shows', 'Movies',
                             'VOD TV Shows', 'VOD Movies', 'YouTube Subscriptions', 'YouTube Downloads'})

            def post(action, body):
                return client.post('/api/setup/' + action, json=body, base_url=env['LOCAL_SETUP_URL'],
                                   headers={'Origin': env['LOCAL_SETUP_URL'], 'X-CSRF-Token': state['csrf']})

            response = post('admin', dict(username='owner', password='long-test-password', confirm='long-test-password'))
            self.assertEqual(response.status_code, 200, response.json)
            state['csrf'] = response.json['csrf']
            for action in ('skip-route', 'storage', 'source-ready', 'finish'):
                response = post(action, {})
                self.assertEqual(response.status_code, 200, (action, response.json))
            self.assertEqual(store.get('setup_complete'), '1')
            self.assertEqual(client.get('/app/', base_url=env['LOCAL_SETUP_URL']).status_code, 200)

    def test_release_image_and_workflows_remain_repository_scoped(self):
        services = self.load('compose.casaos.yaml')['services']
        self.assertEqual({s['image'] for s in services.values()}, {RELEASE_IMAGE})
        self.assertIn('org.opencontainers.image.source="https://github.com/AshCooperUK/ReelDock-v1.3"',
                      (ROOT / 'Dockerfile').read_text())
        for name in ('release.yml', 'export-image.yml'):
            workflow = (ROOT / '.github/workflows' / name).read_text()
            with self.subTest(workflow=name):
                self.assertIn('IMAGE="ghcr.io/${GITHUB_REPOSITORY,,}"' if name == 'export-image.yml'
                              else 'image="ghcr.io/${GITHUB_REPOSITORY,,}"', workflow)
                self.assertNotIn('ghcr.io/ashcooperuk', workflow.lower())
                self.assertNotIn('.md', workflow)
        release = (ROOT / '.github/workflows/release.yml').read_text()
        self.assertRegex(release, r'push:\s+tags:')
        self.assertIn('--notes "$notes"', release)
        self.assertNotIn('--notes-file', release)

    def test_local_redesign_compose_is_isolated(self):
        config = self.load('compose.yaml')
        self.assertEqual(config['name'], 'reeldock-v13')
        services = config['services']
        self.assertEqual(services['vpn']['ports'], ['127.0.0.1:8821:8790'])
        self.assertEqual(services['web']['network_mode'], 'service:vpn')
        self.assertEqual(services['worker']['network_mode'], 'service:vpn')
        self.assertEqual(services['vpn']['cap_add'], ['NET_ADMIN', 'NET_RAW', 'NET_BIND_SERVICE', 'CHOWN', 'DAC_OVERRIDE'])
        self.assertNotIn('NET_ADMIN', services['web'].get('cap_add', []))
        self.assertEqual(services['web']['environment']['LOCAL_SETUP_URL'], 'http://127.0.0.1:8821')
        self.assertEqual(services['web']['environment'], services['worker']['environment'])
        for service in services.values():
            self.assertEqual(service['image'], LOCAL_IMAGE)
            for path in self.host_paths(service):
                self.assertTrue(path in ('vpn-run', 'vpn-state') or Path(path).is_relative_to(LOCAL))
            self.assertEqual(service['restart'], 'no' if service is services['prepare-storage'] else 'unless-stopped')

    def test_all_yaml_preserves_vpn_worker_storage_and_security_contract(self):
        for name in ('compose.yaml','compose.casaos.yaml','compose.nas-source.yaml'):
            with self.subTest(compose=name):
                config=self.load(name);services=self.check_isolated(config, RELEASE_IMAGE if name=='compose.casaos.yaml' else LOCAL_IMAGE, CASAOS_MEDIA if name=='compose.casaos.yaml' else LOCAL / 'media');meta=config['x-casaos']
                text=(ROOT/name).read_text()
                variables={PRIVADO_TEMPLATE} if name=='compose.casaos.yaml' else {INSTALL_TEMPLATE,PRIVADO_TEMPLATE}
                self.assertEqual(set(re.findall(r'\$\{[^}]+\}',text)),variables,'Only source builds interpolate the installation root; the optional provider key remains VPN-only')
                self.assertNotIn('/home/',text,'Published templates contain no developer home paths')
                alternate=self.load(name,Path('/srv/reeldock-fixture'))['services']
                alternate_root=str(LOCAL) if name=='compose.casaos.yaml' else '/srv/reeldock-fixture'
                self.assertEqual(alternate['web']['environment']['REELDOCK_HOST_ROOT'],str(CASAOS_MEDIA) if name=='compose.casaos.yaml' else alternate_root+'/media')
                self.assertTrue(all(not host.startswith('/') or host.startswith(alternate_root+'/') or (name=='compose.casaos.yaml' and host==str(CASAOS_MEDIA)) for service in alternate.values() for host in self.host_paths(service)))
                self.assertEqual(services['vpn']['environment'],{'REELDOCK_PRIVADO_API_KEY':''})
                for role in ('web','worker','prepare-storage'):
                    self.assertNotIn('REELDOCK_PRIVADO_API_KEY',services[role].get('environment',{}),'The VPN provider key is not shared with application services')
                self.assertEqual(meta['version'],VERSION);self.assertEqual(meta['main'],'web')
                self.assertEqual(meta['architectures'],['amd64']);self.assertIsInstance(meta['port_map'],str)
                self.assertTrue(meta['icon'].endswith('/preview/assets/reeldock-brand.svg'))
                self.assertEqual(set(config.get('volumes',{})),set() if name=='compose.casaos.yaml' else {'vpn-run','vpn-state'})
                self.assertEqual(services['vpn']['dns'],['127.0.0.1'])
                self.assertIn('NET_ADMIN',services['vpn']['cap_add'])
                for service in services.values():
                    self.assertEqual(service['cap_drop'],['ALL'])
                    self.assertIn('no-new-privileges:true',service['security_opt'])
                    self.assertTrue(all(isinstance(port,str) for port in service.get('ports',[])))
                    self.assertTrue(all(isinstance(device,str) for device in service.get('devices',[])))
                for role in ('web','worker'):
                    app=services[role]
                    self.assertEqual(app['network_mode'],'service:vpn');self.assertNotIn('ports',app)
                    self.assertEqual(app['depends_on']['vpn']['condition'],'service_healthy')
                    self.assertEqual(app['depends_on']['prepare-storage']['condition'],'service_completed_successfully')
                    self.assertIn((str(LOCAL / 'vpn-run') if name=='compose.casaos.yaml' else 'vpn-run')+':/vpn-run:ro',app['volumes'])
                    self.assertEqual(app['environment']['REELDOCK_VPN_TRANSPORT'],'1')
                    self.assertEqual(app['environment']['LD_PRELOAD'],'/usr/local/lib/reeldock_socket_mark.so')
                self.assertEqual(services['worker']['depends_on']['web']['condition'],'service_healthy')
                if name in ('compose.yaml', 'compose.casaos.yaml'):
                    self.assertEqual(services['worker']['healthcheck']['test'][0], 'CMD')
                    self.assertNotIn('disable', services['worker']['healthcheck'])
                    self.assertEqual(services['worker']['healthcheck']['test'],
                                     self.load('compose.yaml')['services']['worker']['healthcheck']['test'])
                else:
                    self.assertTrue(services['worker']['healthcheck']['disable'])

    def test_local_worker_healthcheck_requires_recent_heartbeat_without_creating_data(self):
        from backend.system_stats import WORKER_OFFLINE_SECONDS
        probe = self.load('compose.yaml')['services']['worker']['healthcheck']['test']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'reeldock.sqlite3'

            def check():
                return subprocess.run(
                    [sys.executable, *probe[2:]],
                    env={**os.environ, 'DATA_DIR': directory},
                    capture_output=True, text=True, timeout=5,
                )

            self.assertNotEqual(check().returncode, 0)
            self.assertFalse(path.exists(), 'A healthcheck must not create an empty database')
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            cases = (
                (None, False),
                (str(time.time() - WORKER_OFFLINE_SECONDS - 5), False),
                ('invalid', False),
                (str(time.time()), True),
            )
            for heartbeat, healthy in cases:
                with self.subTest(heartbeat=heartbeat):
                    with sqlite3.connect(path) as db:
                        db.execute('DELETE FROM settings')
                        if heartbeat is not None:
                            db.execute('INSERT INTO settings VALUES (?, ?)', ('worker_heartbeat', heartbeat))
                    result = check()
                    self.assertEqual(result.returncode == 0, healthy)

    def test_source_compose_uses_its_own_names_port_and_folders(self):
        services = self.check_isolated(self.load('compose.nas-source.yaml'))
        self.assertEqual(services['vpn']['ports'], ['127.0.0.1:8821:8790'])
        self.assertEqual({s['image'] for s in services.values()}, {LOCAL_IMAGE})
        env = services['web']['environment']
        self.assertEqual(env['APP_PORT'], '8821')
        self.assertEqual(env['LOCAL_SETUP_URL'], 'http://127.0.0.1:8821')
        self.assertEqual(env['MEDIA_HOST_DIR'],str(LOCAL / 'media/YouTube'))


if __name__ == '__main__':
    unittest.main()
