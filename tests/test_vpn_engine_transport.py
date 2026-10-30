"""Engine intent, DNS and pause isolation; all network traffic is emulated."""
import http.client
import os
import socket
import time
import unittest
from contextlib import ExitStack
from unittest.mock import patch
from backend import download_control
from backend.vpn import dns,transport
from test_vpn_dns import Network,answer,response
import test_vpn_dns as fixtures

class EngineTransportTests(unittest.TestCase):
    install=fixtures.MetadataTransportTests.install

    @staticmethod
    def provider_get():
        connection=http.client.HTTPConnection('provider.example.test',timeout=2)
        try:
            connection.request('GET','/download')
            reply=connection.getresponse();return reply.status,reply.read()
        finally:connection.close()

    def test_every_engine_dns_tcp_and_download_follow_own_policy(self):
        for role,mark in transport.ENGINE_MARKS.items():
            for protected in (False,True):
                for connected in (False,True):
                    with self.subTest(role=role,protected=protected,connected=connected),ExitStack() as stack:
                        # Every other engine is protected; this one can still
                        # resolve/connect directly when its own switch is off.
                        flags={value:True for value in transport.ENGINE_MARKS.values()};flags[mark]=protected
                        network=Network(lambda query,tcp,server:answer(query) if tcp else response(query,flags=0x8380),downloads=True,metadata=True,connected=connected,engine_flags=flags)
                        stack.enter_context(patch.dict(os.environ,{'REELDOCK_VPN_TRANSPORT':'1','REELDOCK_TRAFFIC':''}))
                        stack.enter_context(patch.object(socket,'socket',network.Socket))
                        stack.enter_context(patch.object(socket,'getaddrinfo',network.resolver))
                        stack.enter_context(patch.object(socket,'_reeldock_vpn',False,create=True));transport.install()
                        request=getattr(transport,role)(self.provider_get)
                        if protected and not connected:
                            with self.assertRaises(OSError):request()
                            self.assertFalse(network.requests)
                        else:
                            self.assertEqual(request(),(200,b'OK'))
                            self.assertEqual(network.requests[0][0],mark)
                            self.assertTrue(any(s.type==socket.SOCK_STREAM and s.address[1]==53 for s in network.sockets if s.family!=socket.AF_UNIX))
                        self.assertTrue(all(s.mark==mark for s in network.sockets if s.family!=socket.AF_UNIX))
                        self.assertTrue(all(call[-1]&socket.AI_NUMERICHOST for call in network.resolver_calls))
                        self.assertIsNone(transport._kind.get());self.assertIsNone(transport._dns_deadline.get())

    def test_scopes_restore_after_exception_and_nested_metadata_stays_independent(self):
        for role,mark in transport.ENGINE_MARKS.items():
            @getattr(transport,role)
            def run():
                self.assertEqual(transport.traffic_class(),mark)
                self.assertEqual(transport.downloads(transport.traffic_class)(),mark)
                self.assertEqual(transport.metadata(transport.traffic_class)(),transport.METADATA)
                self.assertEqual(transport.traffic_class(),mark)
                raise RuntimeError('fixture')
            with self.assertRaises(RuntimeError):run()
            self.assertIsNone(transport._kind.get())

    def test_child_environment_is_copied_and_explicit_role_wins_shared_extractor(self):
        parent={'LD_PRELOAD':'/app/runtime/libsocket_mark.so','KEEP':'fixture'}
        for role,mark in transport.ENGINE_MARKS.items():
            child=transport.subprocess_env(role,parent)
            self.assertEqual(child,dict(parent,REELDOCK_TRAFFIC=role));self.assertNotIn('REELDOCK_TRAFFIC',parent)
            namespace={'__name__':'backend.youtube_extract','traffic_class':transport.traffic_class}
            exec('def call(): return traffic_class()',namespace)
            with patch.dict(os.environ,child,clear=True):
                self.assertEqual(namespace['call'](),mark)
                self.assertEqual(transport.metadata(namespace['call'])(),transport.METADATA)
            self.assertEqual(getattr(transport,role)(transport.subprocess_env)()['REELDOCK_TRAFFIC'],role)
        self.assertEqual(transport.subprocess_env('unknown',parent)['REELDOCK_TRAFFIC'],'downloads')
        self.assertEqual(transport.subprocess_env('metadata',parent)['REELDOCK_TRAFFIC'],'metadata')

    def test_legacy_thread_module_fallback_and_shared_metadata_exceptions(self):
        for module,wanted in [('downloaders.nzb.nntp',transport.NZB),('downloaders.torrent.dht',transport.TORRENT),('downloaders.torrent.metadata',transport.METADATA),('downloaders.nzb.indexers',transport.METADATA),('m3u_downloads',transport.VOD),('downloads',transport.YOUTUBE),('future_engine',transport.DOWNLOAD)]:
            namespace={'__name__':'backend.'+module,'traffic_class':transport.traffic_class};exec('def call(): return traffic_class()',namespace)
            with patch.dict(os.environ,{'REELDOCK_TRAFFIC':''}):self.assertEqual(namespace['call'](),wanted)

    def test_selected_profile_resolvers_pass_through_strict_dns_parser(self):
        seen=[]
        def exchange(name,qtype,factory,deadline,*,resolvers=None):
            seen.append((name,qtype,resolvers));return ['198.51.100.2'],name
        with patch.object(dns,'_exchange',side_effect=exchange):
            self.assertEqual(dns._resolve('fixture.test',1,None,time.monotonic()+1,resolvers=['10.8.0.1']),(['198.51.100.2'],'fixture.test'))
        self.assertEqual(seen,[('fixture.test',1,['10.8.0.1'])])

    def test_pause_readers_keep_global_pause_holds_and_scope_only_vpn(self):
        class Store:
            def get(self,key,default=''):return default
        store=Store()
        with patch('backend.acquisition.held',return_value=False),patch('backend.vpn.downloads_blocked',side_effect=lambda engine=None:engine!='youtube') as gate:
            self.assertFalse(download_control.paused(store,'youtube'))
            self.assertTrue(download_control.paused(store,'nzb'))
            self.assertFalse(download_control.reader(store,'youtube').snapshot()['paused'])
            self.assertTrue(download_control.reader(store,'nzb').snapshot()['paused'])
            self.assertTrue(download_control.paused(store))
            self.assertIn(unittest.mock.call('youtube'),gate.call_args_list)
            self.assertIn(unittest.mock.call('nzb'),gate.call_args_list)
            self.assertIn(unittest.mock.call(),gate.call_args_list)
        with patch('backend.acquisition.held',return_value=True),patch('backend.vpn.downloads_blocked') as gate:
            self.assertEqual(download_control.pause_status(store,'youtube'),dict(paused=True,reason='hold'));gate.assert_not_called()
