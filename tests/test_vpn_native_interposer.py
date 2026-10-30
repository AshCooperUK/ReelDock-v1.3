"""Actual native ABI tests using private Unix brokers, with no public DNS.

The second preload library refuses every nonnumeric libc DNS lookup, including
localhost. Thus a regression cannot contact a provider or leak a DNS query.
Only the explicit integration case imports the gateway's pure broker functions;
its DNS resolver and socket creation remain fixtures. No installed state,
running gateway or Internet sockets are used.
"""
import array
import contextlib
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]/'runtime/vpn/socket_mark.c'
ROLES={'youtube':0xD11,'nzb':0xD12,'torrent':0xD13,'vod':0xD14,'metadata':0xD02,'unknown':0xD01,None:0xD01}
SPY=r'''
#define _GNU_SOURCE
#include <netdb.h>
#include <dlfcn.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <stdio.h>
#include <unistd.h>
int getaddrinfo(const char *host,const char *service,const struct addrinfo *hints,struct addrinfo **result) {
 int (*original)(const char*,const char*,const struct addrinfo*,struct addrinfo**)=dlsym(RTLD_NEXT,"getaddrinfo");
 int flags=hints ? hints->ai_flags : 0;
 const char *log=getenv("RD_NATIVE_SPY_LOG");
 if(log) {int fd=open(log,O_WRONLY|O_CREAT|O_APPEND,0600);if(fd>=0){dprintf(fd,"%d|%s\n",flags,host ? host : "<null>");close(fd);}}
 if(host && !(flags&AI_NUMERICHOST)) return EAI_FAIL;
 return original ? original(host,service,hints,result) : EAI_FAIL;
}
'''
LOOKUP=r'''
import json,os,socket
try:
 rows=socket.getaddrinfo(os.environ['RD_NATIVE_HOST'],443,int(os.environ.get('RD_NATIVE_FAMILY','0')),socket.SOCK_STREAM,0,int(os.environ.get('RD_NATIVE_FLAGS','0')))
 print(json.dumps(dict(rows=[[int(family),int(kind),protocol,canonical,list(address)] for family,kind,protocol,canonical,address in rows])))
except socket.gaierror as error:print(json.dumps(dict(error=error.errno)))
'''
OPEN_SOCKET=r'''
import json,socket
try:
 with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as connection:
  connection.sendall(b'fixture-byte')
  print(json.dumps(dict(opened=True,inheritable=connection.get_inheritable())))
except OSError as error:print(json.dumps(dict(error=error.errno)))
'''


def exact(connection,length):
    data=bytearray()
    while len(data)<length:
        part=connection.recv(length-len(data))
        if not part:raise EOFError('Private fixture IPC ended early')
        data.extend(part)
    return bytes(data)


def dns_answer(addresses=('203.0.113.17',),canonical=b'canonical.invalid',status=0):
    records=[]
    for value in addresses:
        family=socket.AF_INET6 if ':' in value else socket.AF_INET
        records.append(struct.pack('=i16s',family,socket.inet_pton(family,value)))
    return struct.pack('=3i',status,len(records),len(canonical))+b''.join(records)+canonical


class UnixBroker:
    def __init__(self,path,handler):
        self.path=path;self.handler=handler;self.stop=threading.Event();self.errors=[]
    def __enter__(self):
        self.server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.server.bind(str(self.path));self.server.listen(16);self.server.settimeout(.1)
        self.thread=threading.Thread(target=self.serve,daemon=True);self.thread.start()
        return self
    def serve(self):
        while not self.stop.is_set():
            try:connection,_=self.server.accept()
            except socket.timeout:continue
            except OSError:return
            with connection:
                connection.settimeout(1)
                try:self.handler(connection)
                except (BrokenPipeError,ConnectionResetError):pass
                except BaseException as error:self.errors.append(error)
    def __exit__(self,*_):
        self.stop.set();self.server.close();self.thread.join(1)
        self.path.unlink(missing_ok=True)
        if self.errors:raise self.errors[0]


class NativeInterposerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory(prefix='rd-native-')
        cls.root=Path(cls.temporary.name);cls.socket_path=cls.root/'s.sock';cls.dns_path=cls.root/'d.sock'
        cls.library=cls.root/'socket_mark.so';cls.spy=cls.root/'dns_spy.so'
        precompiled=os.environ.get('REELDOCK_TEST_PRECOMPILED_INTERPOSER')
        guard=os.environ.get('REELDOCK_TEST_PRECOMPILED_DNS_GUARD')
        cls.dns_budget=.2
        if precompiled:
            assert guard,'Packaged interposer qualification also requires the frozen precompiled DNS guard.'
            assert os.environ.get('REELDOCK_TEST_PRIVATE_BROKERS')=='1','Use an isolated, empty /vpn-run tmpfs; never the installed gateway volume.'
            assert {path.name for path in Path('/sys/class/net').iterdir()}=={'lo'},'Packaged ABI tests require --network none.'
            broker=Path('/vpn-run')
            assert broker.is_dir() and not broker.is_symlink() and not any(broker.iterdir()),'Packaged tests require empty disposable /vpn-run.'
            cls.library=Path(precompiled);cls.spy=Path(guard)
            cls.socket_path=broker/'socket.sock';cls.dns_path=broker/'dns.sock';cls.dns_budget=5
            for path in (cls.library,cls.spy):
                assert path.is_file() and not path.is_symlink() and path.read_bytes()[:4]==b'\x7fELF','Only exact regular ELF artifacts are accepted.'
            cls.library_sha=hashlib.sha256(cls.library.read_bytes()).hexdigest()
            cls.guard_sha=hashlib.sha256(cls.spy.read_bytes()).hexdigest()
            cls.source_sha=None
            return
        compiler=shutil.which('gcc')
        if not compiler:raise unittest.SkipTest('The native ABI qualification requires gcc or both frozen precompiled libraries.')
        cls.source_sha=hashlib.sha256(SOURCE.read_bytes()).hexdigest()
        command=[compiler,'-shared','-fPIC','-Wall','-Werror','-O2',
                 '-DREELDOCK_SOCKET_PATH="'+str(cls.socket_path)+'"',
                 '-DREELDOCK_DNS_PATH="'+str(cls.dns_path)+'"','-DREELDOCK_DNS_TIMEOUT_MS=200',
                 '-o',str(cls.library),str(SOURCE),'-ldl']
        run=subprocess.run(command,capture_output=True,text=True,timeout=30)
        if run.returncode:raise AssertionError(run.stderr)
        spy_source=cls.root/'spy.c';spy_source.write_text(SPY)
        run=subprocess.run([compiler,'-shared','-fPIC','-Wall','-Werror','-O2','-o',str(cls.spy),str(spy_source),'-ldl'],capture_output=True,text=True,timeout=30)
        if run.returncode:raise AssertionError(run.stderr)
        assert hashlib.sha256(SOURCE.read_bytes()).hexdigest()==cls.source_sha,'Native source changed while compiling.'
        cls.library_sha=hashlib.sha256(cls.library.read_bytes()).hexdigest()
        cls.guard_sha=hashlib.sha256(cls.spy.read_bytes()).hexdigest()
        destination=os.environ.get('REELDOCK_TEST_SAVE_DNS_GUARD')
        if destination:
            target=Path(destination)
            if target.exists():assert not target.is_symlink() and hashlib.sha256(target.read_bytes()).hexdigest()==cls.guard_sha
            else:
                descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o644)
                with os.fdopen(descriptor,'wb') as output:output.write(cls.spy.read_bytes())
    @classmethod
    def tearDownClass(cls):cls.temporary.cleanup()

    def child(self,*,role='nzb',host='engine.invalid',family=0,flags=0,code=LOOKUP):
        log=self.root/('spy-'+str(time.monotonic_ns())+'.log')
        environment={key:value for key,value in os.environ.items() if not key.startswith('REELDOCK_') and key not in ('LD_PRELOAD','PYTHONPATH')}
        environment.update(LD_PRELOAD=str(self.library)+':'+str(self.spy),RD_NATIVE_SPY_LOG=str(log),
                           RD_NATIVE_HOST=host,RD_NATIVE_FAMILY=str(family),RD_NATIVE_FLAGS=str(flags))
        if role is not None:environment['REELDOCK_TRAFFIC']=role
        run=subprocess.run([sys.executable,'-I','-S','-c',code],env=environment,capture_output=True,text=True,timeout=max(3,self.dns_budget+2))
        self.assertEqual(run.returncode,0,run.stderr)
        records=[line.split('|',1) for line in log.read_text().splitlines()] if log.exists() else []
        if code==LOOKUP:self.assertTrue(records,'The DNS guard must actually intercept libc calls.')
        self.assertTrue(all(int(value)&socket.AI_NUMERICHOST for value,name in records),
                        'Native lookup attempted nonnumeric libc network DNS: '+repr(records))
        return json.loads(run.stdout)

    @contextlib.contextmanager
    def dns(self,response=None,*,delay=0):
        requests=[]
        def handle(connection):
            mark,family,flags,length=struct.unpack('=4i',exact(connection,16))
            self.assertGreater(length,0);self.assertLessEqual(length,1024)
            host=exact(connection,length)
            requests.append(dict(mark=mark,family=family,flags=flags,host=host.decode('utf-8')))
            if delay:time.sleep(delay)
            connection.sendall(dns_answer() if response is None else response)
        with UnixBroker(self.dns_path,handle):yield requests

    def test_every_role_reaches_the_dns_broker_without_libc_network_fallback(self):
        with self.dns() as requests:
            for role,mark in ROLES.items():
                with self.subTest(role=role):
                    result=self.child(role=role,flags=socket.AI_CANONNAME)
                    self.assertEqual(result['rows'][0][3],'canonical.invalid')
                    self.assertEqual(result['rows'][0][4][0],'203.0.113.17')
                    self.assertEqual(requests[-1],dict(mark=mark,family=0,flags=socket.AI_CANONNAME,host='engine.invalid'))

    def test_every_role_uses_scm_rights_socket_broker(self):
        requests=[];delivered=[]
        def handle(connection):
            request=struct.unpack('=4i',exact(connection,16));requests.append(request)
            first,second=socket.socketpair(socket.AF_UNIX,socket.SOCK_STREAM)
            with first,second:
                connection.sendmsg([b'1'],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[first.fileno()]))])
                second.settimeout(1);delivered.append(second.recv(64))
        with UnixBroker(self.socket_path,handle):
            for role,mark in ROLES.items():
                with self.subTest(role=role):
                    result=self.child(role=role,code=OPEN_SOCKET)
                    self.assertTrue(result['opened']);self.assertFalse(result['inheritable'])
                    self.assertEqual(requests[-1][0],socket.AF_INET)
                    self.assertEqual(requests[-1][1]&0xf,socket.SOCK_STREAM)
                    self.assertEqual(requests[-1][3],mark)
        self.assertEqual(delivered,[b'fixture-byte']*len(ROLES))

    def test_numeric_localhost_and_unix_sockets_do_not_need_brokers(self):
        for host,family in [('203.0.113.17',socket.AF_INET),('2001:db8::17',socket.AF_INET6),('localhost',socket.AF_UNSPEC)]:
            with self.subTest(host=host):self.assertTrue(self.child(host=host,family=family)['rows'])
        self.assertTrue(self.child(code="import json,socket; s=socket.socket(socket.AF_UNIX); print(json.dumps(dict(local=True))); s.close()")['local'])

    def test_family_canonical_and_ipv4_mapped_semantics(self):
        both=dns_answer(('203.0.113.17','2001:db8::17'))
        with self.dns(both):
            rows=self.child(family=socket.AF_INET,flags=socket.AI_CANONNAME)['rows']
            self.assertEqual({row[0] for row in rows},{socket.AF_INET})
            self.assertEqual(rows[0][3],'canonical.invalid')
            rows=self.child(family=socket.AF_INET6,flags=socket.AI_V4MAPPED)['rows']
            self.assertEqual([row[4][0] for row in rows],['2001:db8::17'])
            rows=self.child(family=socket.AF_INET6,flags=socket.AI_V4MAPPED|socket.AI_ALL)['rows']
            self.assertEqual({row[4][0] for row in rows},{'::ffff:203.0.113.17','2001:db8::17'})
        with self.dns(dns_answer()):
            rows=self.child(family=socket.AF_INET6,flags=socket.AI_V4MAPPED)['rows']
            self.assertEqual(rows[0][4][0],'::ffff:203.0.113.17')

    def test_null_hints_preserve_glibc_default_flags_without_libc_dns(self):
        code=r'''
import ctypes,json
class Info(ctypes.Structure):
 _fields_=[('flags',ctypes.c_int),('family',ctypes.c_int),('kind',ctypes.c_int),('protocol',ctypes.c_int),('length',ctypes.c_uint),('address',ctypes.c_void_p),('canonical',ctypes.c_char_p),('next',ctypes.c_void_p)]
library=ctypes.CDLL(None)
library.getaddrinfo.argtypes=[ctypes.c_char_p,ctypes.c_char_p,ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p)]
result=ctypes.c_void_p();status=library.getaddrinfo(b'engine.invalid',b'443',None,ctypes.byref(result))
if status:print(json.dumps(dict(error=status)))
else:
 flags=ctypes.cast(result,ctypes.POINTER(Info)).contents.flags
 library.freeaddrinfo.argtypes=[ctypes.c_void_p];library.freeaddrinfo(result)
 print(json.dumps(dict(flags=flags)))
'''
        with self.dns() as requests:
            self.assertEqual(self.child(code=code)['flags'],socket.AI_V4MAPPED|socket.AI_ADDRCONFIG)
            self.assertEqual(requests[-1]['flags'],socket.AI_V4MAPPED|socket.AI_ADDRCONFIG)

    def test_broker_negative_answers_keep_native_error_categories(self):
        for status,error in ((1,socket.EAI_NONAME),(2,socket.EAI_AGAIN)):
            with self.subTest(status=status),self.dns(struct.pack('=3i',status,0,0)):
                self.assertEqual(self.child()['error'],error)

    def test_actual_native_dns_protocol_uses_selected_engine_in_python_gateway(self):
        from backend.vpn import dns as resolver
        spec=importlib.util.spec_from_file_location('synthetic_native_gateway',SOURCE.with_name('gateway.py'))
        gateway=importlib.util.module_from_spec(spec);spec.loader.exec_module(gateway)
        gateway.CONFIG['values']=dict(youtube=False,nzb=True,torrent=True,vod=True,metadata=False,downloads=True)
        gateway.STATUS['connected']=False;gateway.DNS=['198.18.0.1','198.18.0.2']
        lookups=[];marks=[]
        class RoutedFixture:
            def setsockopt(self,level,option,value):marks.append((level,option,value))
            def close(self):pass
        def resolve(name,qtype,factory,deadline,*,resolvers=None):
            lookups.append((name,qtype,tuple(resolvers)))
            with patch.object(gateway.socket,'socket',return_value=RoutedFixture()):
                connection=factory(socket.AF_INET,socket.SOCK_DGRAM);connection.close()
            return (['203.0.113.17'] if qtype==1 else []),'canonical.invalid'
        class AuthorizedFixturePeer:
            # The fixture grants the same UID as an app container. All framing,
            # deadline, resolver-selection and response code runs in production.
            def __init__(self,connection):self.connection=connection
            def __enter__(self):return self
            def __exit__(self,*_):self.connection.close()
            def getsockopt(self,level,option,length):
                if (level,option)==(socket.SOL_SOCKET,socket.SO_PEERCRED):return struct.pack('3i',os.getpid(),10001,10001)
                return self.connection.getsockopt(level,option,length)
            def __getattr__(self,name):return getattr(self.connection,name)
        def handle(connection):gateway.native_dns_broker(AuthorizedFixturePeer(connection))
        with patch.object(resolver,'_resolve',side_effect=resolve),UnixBroker(self.dns_path,handle):
            self.assertEqual(self.child(role='youtube')['rows'][0][4][0],'203.0.113.17')
            self.assertTrue(lookups)
            self.assertTrue(all(row[2]==resolver.RESOLVERS for row in lookups))
            self.assertTrue(all(mark[2]==0xD11 for mark in marks))
            lookups.clear();marks.clear()
            self.assertEqual(self.child(role='nzb')['error'],socket.EAI_AGAIN)
            self.assertEqual(lookups,[],'A disconnected protected engine must never use direct DNS')
            gateway.STATUS['connected']=True
            self.assertEqual(self.child(role='nzb')['rows'][0][4][0],'203.0.113.17')
            self.assertTrue(all(row[2]==tuple(gateway.DNS) for row in lookups))
            self.assertTrue(all(mark[2]==0xD12 for mark in marks))

    def test_malformed_and_truncated_broker_frames_fail_without_fallback(self):
        record=struct.pack('=i16s',socket.AF_INET,socket.inet_pton(socket.AF_INET,'203.0.113.17'))
        frames=[b'',b'\0',struct.pack('=3i',0,129,0),struct.pack('=3i',0,0,0),struct.pack('=3i',3,0,0),
                struct.pack('=3i',1,1,0),struct.pack('=3i',0,1,254),struct.pack('=3i',0,1,-1),
                struct.pack('=3i',0,1,0)+record[:-1],struct.pack('=3i',0,1,3)+record+b'ab',
                struct.pack('=3i',0,1,3)+record+b'a\0b',struct.pack('=3i',0,1,0)+struct.pack('=i16s',socket.AF_UNIX,b'x')]
        for index,frame in enumerate(frames):
            with self.subTest(frame=index),self.dns(frame):self.assertEqual(self.child()['error'],socket.EAI_AGAIN)

    def test_deadline_and_missing_broker_never_fall_back_to_public_dns(self):
        self.assertEqual(self.child()['error'],socket.EAI_AGAIN)
        with self.dns(delay=self.dns_budget+.15):
            started=time.monotonic();self.assertEqual(self.child()['error'],socket.EAI_AGAIN)
            self.assertLess(time.monotonic()-started,self.dns_budget+1.5)

    def test_invalid_numeric_host_request_is_not_sent_to_broker(self):
        with self.dns() as requests:
            self.assertEqual(self.child(flags=socket.AI_NUMERICHOST)['error'],socket.EAI_NONAME)
            self.assertEqual(requests,[])

    def test_missing_or_malformed_scm_rights_never_opens_direct_socket(self):
        for response in (b'0',b'1',b''):
            def handle(connection):
                exact(connection,16);connection.sendall(response)
            with self.subTest(response=response),UnixBroker(self.socket_path,handle):
                self.assertEqual(self.child(code=OPEN_SOCKET)['error'],errno.ENETDOWN)

    def test_malformed_multiple_received_descriptors_are_all_closed(self):
        def handle(connection):
            exact(connection,16)
            first,second=socket.socketpair(socket.AF_UNIX,socket.SOCK_STREAM)
            with first,second:
                connection.sendmsg([b'1'],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[first.fileno(),second.fileno()]))])
        code=r'''
import json,os,socket
before=len(os.listdir('/proc/self/fd'))
try:connection=socket.socket(socket.AF_INET,socket.SOCK_STREAM);connection.close();error=None
except OSError as failure:error=failure.errno
print(json.dumps(dict(error=error,descriptorsBefore=before,descriptorsAfter=len(os.listdir('/proc/self/fd')))))
'''
        with UnixBroker(self.socket_path,handle):
            result=self.child(code=code)
        self.assertEqual(result['error'],errno.ENETDOWN)
        self.assertEqual(result['descriptorsBefore'],result['descriptorsAfter'],'Rejected broker descriptors must all be closed')


if __name__=='__main__':unittest.main()
