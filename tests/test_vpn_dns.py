"""Disposable DNS/socket fixtures: no live providers, VPN profiles or sockets."""
import array
import http.client
import io
import socket
import struct
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from backend.vpn import dns, transport

LIBC=socket.getaddrinfo
IP='198.51.100.7'
IP6='2001:db8::7'


def name(value):
    return b''.join(bytes([len(label)])+label.encode('ascii') for label in value.split('.'))+b'\0'


def record(owner,kind,data):
    return name(owner)+struct.pack('!HHIH',kind,1,60,len(data))+data


def response(query,records=(),flags=0x8180):
    identity=struct.unpack('!H',query[:2])[0]
    return struct.pack('!6H',identity,flags,1,len(records),0,0)+query[12:]+b''.join(records)


def answer(query,tcp=False,server='1.1.1.1'):
    host,offset=dns._name(query,12)
    kind=struct.unpack('!H',query[offset:offset+2])[0]
    address=socket.inet_pton(socket.AF_INET if kind==1 else socket.AF_INET6,IP if kind==1 else IP6)
    return response(query,[record(host,kind,address)])


class Network:
    """Emulate marked broker sockets and DNS/HTTP; never open a real socket."""
    def __init__(self,handler=answer,downloads=False,metadata=False,connected=False,engine_flags=None):
        self.handler=handler;self.downloads=downloads;self.metadata=metadata;self.connected=connected;self.engine_flags=engine_flags or {}
        self.sockets=[];self.requests=[];self.descriptors={};self.resolver_calls=[];network=self
        class Socket:
            def __init__(self,family=socket.AF_INET,type=socket.SOCK_STREAM,proto=0,fileno=None):
                self.family=family;self.type=type;self.proto=proto;self.mark=network.descriptors.get(fileno,transport.traffic_class())
                self.address=None;self.sent=b'';self.read=b'';self.timeout=None;network.sockets.append(self)
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
            def close(self):pass
            def settimeout(self,value):self.timeout=value
            def setsockopt(self,*args):pass
            def connect(self,address):
                self.address=address
                if self.family!=socket.AF_UNIX:
                    protected=network.metadata if self.mark==transport.METADATA else network.engine_flags.get(self.mark,network.downloads)
                    if protected and not network.connected:raise OSError('Protected traffic blocked')
            def send(self,data):self.sent=data;return len(data)
            def sendall(self,data):
                self.sent=data
                if self.family==socket.AF_UNIX:return
                if self.address[1]==53:
                    query=data[2:];reply=network.handler(query,True,self.address[0])
                    self.read=struct.pack('!H',len(reply))+reply
                else:
                    network.requests.append((self.mark,data))
                    self.read=b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK'
            def recvmsg(self,*args):
                family,kind,proto,mark=struct.unpack('4i',self.sent)
                descriptor=len(network.descriptors)+100;network.descriptors[descriptor]=mark
                return b'1',[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[descriptor]).tobytes())],0,None
            def recvfrom(self,maximum):
                reply=network.handler(self.sent,False,self.address[0])
                if isinstance(reply,tuple):return reply
                return reply,self.address
            def recv(self,maximum):
                block=self.read[:maximum];self.read=self.read[maximum:];return block
            def makefile(self,*args):return io.BytesIO(self.read)
        self.Socket=Socket

    def resolver(self,host,port,family=0,type=0,proto=0,flags=0):
        self.resolver_calls.append((host,port,family,type,proto,flags))
        if host=='provider.example.test' and not flags&socket.AI_NUMERICHOST:
            raise socket.gaierror(socket.EAI_AGAIN,'Existing conservative libc DNS unavailable')
        return LIBC(host,port,family,type,proto,flags)


class DNSLookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        hosts=Path(self.tmp.name)/'hosts';hosts.write_text('127.0.0.1 localhost static.test\n')
        self.enterContext(patch.object(dns,'HOSTS',hosts))
        self.enterContext(patch.object(socket,'gethostname',return_value='local-machine'))

    def lookup(self,network=None,host='provider.example.test',port=443,family=socket.AF_INET,type=socket.SOCK_STREAM,proto=0,flags=0):
        network=network or Network()
        return dns.getaddrinfo(network.resolver,network.Socket,host,port,family,type,proto,flags)

    def test_idna_bytes_family_service_protocol_and_canonical_name(self):
        network=Network()
        rows=self.lookup(network,host='b\N{LATIN SMALL LETTER U WITH DIAERESIS}cher.test.',port='https',family=socket.AF_UNSPEC,proto=socket.IPPROTO_TCP,flags=socket.AI_CANONNAME)
        self.assertEqual({row[0] for row in rows},{socket.AF_INET,socket.AF_INET6})
        self.assertTrue(all(row[1:3]==(socket.SOCK_STREAM,socket.IPPROTO_TCP) and row[4][1]==443 for row in rows))
        self.assertEqual(rows[0][3],'xn--bcher-kva.test')
        self.assertEqual(rows[1][3],'')
        self.assertTrue(all(call[-1]&socket.AI_NUMERICHOST for call in network.resolver_calls))
        self.assertEqual(self.lookup(host=b'provider.example.test')[0][4],(IP,443))

    def test_numeric_none_ipv6_zone_and_trusted_local_names_use_original(self):
        values=[None,'127.0.0.1',b'127.0.0.1','::1','fe80::1%1','localhost','static.test','local-machine']
        for host in values:
            with self.subTest(host=host):
                calls=[]
                def original(*args):
                    calls.append(args)
                    if host in ('localhost','static.test','local-machine') and args[-1]&socket.AI_NUMERICHOST:
                        raise socket.gaierror(socket.EAI_NONAME,'not numeric')
                    return ['local-result']
                with patch.object(dns,'_exchange',side_effect=AssertionError('Local names must not use external DNS')):
                    self.assertEqual(dns.getaddrinfo(original,None,host,443),['local-result'])
                self.assertEqual(calls[-1][0],host)

    def test_dotless_and_dot_local_are_not_libc_hostname_fallbacks(self):
        for host in ('external','external.local'):
            network=Network();self.lookup(network,host=host)
            self.assertTrue(all(call[-1]&socket.AI_NUMERICHOST for call in network.resolver_calls))
            self.assertTrue(network.sockets)

    def test_cname_answers_only_follow_the_queried_chain(self):
        def handler(query,*args):
            records=[record('unrelated.test',1,socket.inet_aton('203.0.113.99')),
                record('provider.example.test',5,name('alias.example.test')),
                record('alias.example.test',1,socket.inet_aton(IP))]
            return response(query,records)
        rows=self.lookup(Network(handler),flags=socket.AI_CANONNAME)
        self.assertEqual(rows[0][4][0],IP);self.assertEqual(rows[0][3],'alias.example.test')

    def test_cname_can_continue_in_a_bounded_subsequent_query(self):
        asked=[]
        def handler(query,*args):
            host,_=dns._name(query,12);asked.append(host)
            return response(query,[record(host,5,name('alias.test'))]) if host=='provider.example.test' else answer(query)
        self.assertEqual(self.lookup(Network(handler))[0][4][0],IP)
        self.assertEqual(asked,['provider.example.test','alias.test'])

    def test_standard_compressed_owner_is_accepted_and_record_limit_is_enforced(self):
        compressed=b'\xc0\x0c'+struct.pack('!HHIH',1,1,60,4)+socket.inet_aton(IP)
        self.assertEqual(self.lookup(Network(lambda query,*args:response(query,[compressed])))[0][4][0],IP)
        with self.assertRaises(socket.gaierror):
            self.lookup(Network(lambda query,*args:response(query,[compressed]*(dns.MAX_RECORDS+1))))

    def test_truncation_uses_tcp_to_same_fixed_resolver(self):
        seen=[]
        def handler(query,tcp,server):
            seen.append((tcp,server))
            return answer(query) if tcp else response(query,flags=0x8380)
        network=Network(handler)
        self.assertEqual(self.lookup(network)[0][4][0],IP)
        self.assertEqual(seen,[(False,'1.1.1.1'),(True,'1.1.1.1')])
        self.assertEqual([item.type for item in network.sockets],[socket.SOCK_DGRAM,socket.SOCK_STREAM])

    def test_invalid_reply_never_falls_back_to_libc_external_dns(self):
        corruptions=[lambda q: b'bad',lambda q:response(q)+b'junk',
            lambda q:bytes([q[0]^1])+response(q)[1:],
            lambda q:response(q,flags=0x0180),lambda q:response(q,flags=0x8980),
            lambda q:response(q,flags=0x8182),lambda q:response(q).replace(name('provider.example.test'),name('different.example.test')),
            lambda q:response(q,[(b'\xc0'+bytes([len(q)]))+struct.pack('!HHIH',1,1,60,4)+socket.inet_aton(IP)]),
            lambda q:response(q,[record('provider.example.test',5,name('provider.example.test'))]),
            lambda q:(response(q),('203.0.113.8',53)),lambda q:b'x'*(dns.MAX_MESSAGE+1)]
        for corrupt in corruptions:
            with self.subTest(corrupt=corrupt):
                network=Network(lambda query,*args:corrupt(query))
                with self.assertRaises(socket.gaierror):self.lookup(network)
                self.assertTrue(all(call[-1]&socket.AI_NUMERICHOST for call in network.resolver_calls))

    def test_unrelated_answers_and_nxdomain_are_not_addresses(self):
        for handler in (lambda q,*args:response(q,[record('unrelated.test',1,socket.inet_aton(IP))]),
                        lambda q,*args:response(q,flags=0x8183)):
            with self.assertRaises(socket.gaierror) as error:self.lookup(Network(handler))
            self.assertEqual(error.exception.errno,socket.EAI_NONAME)

    def test_resolver_failover_and_broker_failure_are_bounded(self):
        def handler(query,tcp,server):
            if server=='1.1.1.1':raise TimeoutError('DNS unavailable')
            return answer(query)
        network=Network(handler)
        self.assertEqual(self.lookup(network)[0][4][0],IP)
        self.assertEqual([item.address[0] for item in network.sockets],list(dns.RESOLVERS))
        with patch.object(dns.time,'monotonic',side_effect=[0,6,6,6]):
            with self.assertRaises(socket.gaierror):self.lookup(Network())

    def test_numeric_flags_invalid_host_and_v4_mapping(self):
        network=Network()
        with self.assertRaises(socket.gaierror):self.lookup(network,flags=socket.AI_NUMERICHOST)
        self.assertFalse(network.sockets)
        for host in ('bad..test','a'*64+'.test','bad\x00host'):
            with self.assertRaises((socket.gaierror,UnicodeError)):self.lookup(host=host)
        def no_ipv6(query,*args):
            host,offset=dns._name(query,12)
            return response(query) if struct.unpack('!H',query[offset:offset+2])[0]==28 else answer(query)
        mapped=self.lookup(Network(no_ipv6),family=socket.AF_INET6,flags=socket.AI_V4MAPPED)
        self.assertEqual(mapped[0][4][0],'::ffff:'+IP)

    def test_invalid_service_type_family_and_flags_preserve_libc_errors(self):
        for port,family,kind,flags in ((443,socket.AF_INET,9999,0),(443,99999,socket.SOCK_STREAM,0),
                                       ('not-service',socket.AF_INET,socket.SOCK_STREAM,0),(443,socket.AF_INET,socket.SOCK_STREAM,1<<29)):
            network=Network()
            with self.assertRaises(socket.gaierror) as expected:LIBC('provider.example.test',port,family,kind,0,flags|socket.AI_NUMERICHOST)
            with self.assertRaises(socket.gaierror) as actual:self.lookup(network,port=port,family=family,type=kind,flags=flags)
            self.assertEqual(actual.exception.errno,expected.exception.errno)
            self.assertFalse(network.sockets)


class MetadataTransportTests(unittest.TestCase):
    def install(self,network):
        self.enterContext(patch.dict('os.environ',{'REELDOCK_VPN_TRANSPORT':'1','REELDOCK_TRAFFIC':''}))
        self.enterContext(patch.object(socket,'socket',network.Socket))
        self.enterContext(patch.object(socket,'getaddrinfo',network.resolver))
        self.enterContext(patch.object(socket,'_reeldock_vpn',False,create=True))
        transport.install()

    @staticmethod
    @transport.metadata
    def provider_get():
        connection=http.client.HTTPConnection('provider.example.test',timeout=2)
        try:
            connection.request('GET','/catalogue')
            reply=connection.getresponse();return reply.status,reply.read()
        finally:connection.close()

    def test_metadata_get_uses_its_policy_in_all_four_switch_states(self):
        for downloads in (False,True):
            for metadata in (False,True):
                for connected in (False,True):
                    with self.subTest(downloads=downloads,metadata=metadata,connected=connected):
                        with ExitStack() as stack:
                            network=Network(downloads=downloads,metadata=metadata,connected=connected)
                            stack.enter_context(patch.dict('os.environ',{'REELDOCK_VPN_TRANSPORT':'1','REELDOCK_TRAFFIC':''}))
                            stack.enter_context(patch.object(socket,'socket',network.Socket))
                            stack.enter_context(patch.object(socket,'getaddrinfo',network.resolver))
                            stack.enter_context(patch.object(socket,'_reeldock_vpn',False,create=True))
                            transport.install()
                            if metadata and not connected:
                                with self.assertRaises(OSError):self.provider_get()
                                self.assertEqual(network.requests,[])
                            else:
                                self.assertEqual(self.provider_get(),(200,b'OK'))
                                self.assertTrue(all(mark==transport.METADATA and body.startswith(b'GET /catalogue ') for mark,body in network.requests))
                            self.assertTrue(all(item.mark==transport.METADATA for item in network.sockets if item.family!=socket.AF_UNIX))
                            self.assertTrue(all(call[-1]&socket.AI_NUMERICHOST for call in network.resolver_calls))
                            self.assertIsNone(transport._dns_deadline.get())

    def test_unclassified_and_download_resolution_use_conservative_marked_dns(self):
        network=Network(downloads=True,metadata=False);self.install(network)
        for lookup in (socket.getaddrinfo,transport.downloads(socket.getaddrinfo)):
            with self.assertRaises(socket.gaierror):lookup('provider.example.test',443)
        routed=[item for item in network.sockets if item.family!=socket.AF_UNIX]
        self.assertTrue(routed)
        self.assertTrue(all(item.mark==transport.DOWNLOAD for item in routed))
        self.assertTrue(all(call[-1]&socket.AI_NUMERICHOST for call in network.resolver_calls))
        self.assertEqual(transport.traffic_class(),transport.DOWNLOAD)

    def test_install_is_idempotent_and_broker_failure_does_not_use_unmarked_dns(self):
        network=Network();self.install(network);lookup=socket.getaddrinfo;transport.install()
        self.assertIs(socket.getaddrinfo,lookup)
        with patch.object(network.Socket,'connect',side_effect=OSError('Broker unavailable')):
            with self.assertRaises(socket.gaierror):transport.metadata(socket.getaddrinfo)('provider.example.test',443)
        self.assertFalse(network.requests)
        self.assertTrue(all(call[-1]&socket.AI_NUMERICHOST for call in network.resolver_calls))
        self.assertIsNone(transport._dns_deadline.get())

    def test_each_lookup_respects_current_policy_without_a_cached_bypass(self):
        network=Network(downloads=True,metadata=False);self.install(network)
        self.assertEqual(self.provider_get()[0],200)
        network.metadata=True
        with self.assertRaises(OSError):self.provider_get()
        network.connected=True
        self.assertEqual(self.provider_get()[0],200)
        network.connected=False;network.metadata=False
        self.assertEqual(self.provider_get()[0],200)
        self.assertEqual(len(network.requests),3)

    def test_tcp_dns_retry_and_provider_get_all_keep_metadata_mark(self):
        network=Network(lambda query,tcp,server:answer(query) if tcp else response(query,flags=0x8380),downloads=True,metadata=False)
        self.install(network)
        self.assertEqual(self.provider_get(),(200,b'OK'))
        routed=[item for item in network.sockets if item.family!=socket.AF_UNIX]
        self.assertTrue(any(item.type==socket.SOCK_DGRAM for item in routed))
        self.assertTrue(any(item.type==socket.SOCK_STREAM and item.address[1]==53 for item in routed))
        self.assertTrue(all(item.mark==transport.METADATA for item in routed))

    def test_broker_is_not_contacted_after_the_metadata_dns_deadline(self):
        network=Network();self.install(network)
        token=transport._dns_deadline.set(-1)
        try:
            with self.assertRaises(OSError):socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        finally:transport._dns_deadline.reset(token)
        self.assertTrue(all(item.sent==b'' for item in network.sockets))

    def test_discover_get_still_browses_while_protected_downloads_are_paused(self):
        from backend.app import create_app
        from backend.nzb import stop
        from backend.view_media import adapters
        with tempfile.TemporaryDirectory() as folder:
            app=create_app(dict(TESTING=True,DATA_DIR=folder,MEDIA_DIR=folder+'/media'))
            self.addCleanup(stop,app)
            store=app.extensions['store'];origin='https://dns.example.test'
            store.set('public_origin',origin);store.set('setup_complete','1')
            with store.connect() as db:db.execute("INSERT INTO users VALUES(1,'owner','unused')")
            client=app.test_client()
            with client.session_transaction(base_url=origin) as session:session.update(user_id=1,csrf='token')
            network=Network(downloads=True,metadata=False,connected=False);self.install(network)
            def browse(*args):
                self.assertEqual(self.provider_get()[0],200)
                return dict(items=[dict(id='fixture:1',title='Disposable title')],nextOffset=None)
            with patch('backend.vpn.downloads_blocked',return_value=True),patch.dict(adapters.REGISTRY,{'discover':{**adapters.REGISTRY['discover'],'browse':browse}}):
                result=client.get('/api/view-media?source=discover&media=movies&sort=title',base_url=origin)
            self.assertEqual(result.status_code,200,result.json)
            self.assertEqual(result.json['items'][0]['title'],'Disposable title')
            self.assertEqual(len(network.requests),1)


if __name__=='__main__':unittest.main()
