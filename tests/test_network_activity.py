import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from backend import network_activity as net
from backend.app import create_app

LOCAL = 'http://127.0.0.1:8790'

class NetworkActivityTests(unittest.TestCase):
    def test_counter_parser_excludes_loopback_and_rejects_partial_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder)/'dev'
            file.write_text('Inter-| Receive | Transmit\n lo: 99 0 0 0 0 0 0 0 99 0 0 0 0 0 0 0\n eth0: 100 0 0 0 0 0 0 0 25 0 0 0 0 0 0 0\n')
            self.assertEqual(net.counters(file), {'eth0':(100,25)})
            file.write_text('eth0: 123')
            with self.assertRaises(ValueError): net.counters(file)

    def test_rates_use_elapsed_time_and_reset_on_counter_or_interface_changes(self):
        sampler = net.Sampler()
        self.assertIsNone(sampler.sample({'eth0':(100,50)},10,100))
        self.assertEqual(sampler.sample({'eth0':(1100,300)},15,105), {'time':105,'download':200,'upload':50})
        self.assertIsNone(sampler.sample({'eth0':(1,1)},20,110))
        self.assertEqual(sampler.sample({'eth0':(1,1)},25,115)['download'],0)
        self.assertIsNone(sampler.sample({'eth1':(999999,999999)},30,120))
        self.assertIsNone(sampler.sample({'eth1':(9999999,9999999)},90,180))

    def test_api_authentication_freshness_and_bounded_history(self):
        with tempfile.TemporaryDirectory() as folder:
            app=create_app({'TESTING':True,'DATA_DIR':folder,'LOCAL_SETUP_URL':LOCAL})
            store=app.extensions['store'];client=app.test_client()
            response=client.get('/api/network-activity',base_url=LOCAL)
            self.assertNotEqual(response.status_code,200)
            store.set('setup_complete','true')
            with store.connect() as db:db.execute("INSERT INTO users VALUES (1,'owner','unused')")
            with client.session_transaction(base_url=LOCAL) as session:session['user_id']=1
            points=[{'time':i,'download':i,'upload':0} for i in range(300)]
            store.set('worker_network',json.dumps({'updated':1000,'state':'live','points':points}))
            with patch('backend.network_activity.snapshot',return_value=net.snapshot(store,1005)):
                response=client.get('/api/network-activity',base_url=LOCAL)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json['state'],'live')
            self.assertEqual(len(response.json['points']),181)
            self.assertEqual(response.headers['Cache-Control'],'no-store')
            self.assertEqual(net.snapshot(store,1021)['state'],'waiting')
            self.assertEqual(net.snapshot(store,1021)['points'],[])
            store.set('worker_network','bad json')
            self.assertEqual(net.snapshot(store,1005)['state'],'unavailable')
            self.assertEqual(client.get('/api/network-activity',base_url='http://evil.example').status_code,403)

    def test_snapshot_adds_web_and_worker_traffic(self):
        class Store:
            def __init__(self):
                self.values = {}
            def get(self, key, default=None):
                return self.values.get(key, default)
            def set(self, key, value):
                self.values[key] = value
        store = Store()
        store.set('worker_network', json.dumps({'updated':1000,'state':'live','points':[{'time':1000,'download':200,'upload':10}]}))
        store.set('web_network', json.dumps({'updated':1002,'state':'live','points':[{'time':1001,'download':9_600_000,'upload':0}]}))
        result = net.snapshot(store, 1005)
        self.assertEqual(result['state'], 'live')
        self.assertEqual(result['scope'], 'reeldock')
        self.assertEqual(len(result['points']), 1)
        self.assertEqual(result['points'][0]['download'], 9_600_200)
        self.assertEqual(result['points'][0]['upload'], 10)
        store.set('web_network', 'bad json')
        self.assertEqual(net.snapshot(store, 1005)['points'][0]['download'], 200)

if __name__=='__main__':unittest.main()
