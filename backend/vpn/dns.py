"""Bounded metadata DNS over the existing marked socket broker.

Only Python METADATA getaddrinfo uses these fixed public resolvers. Their UDP
and TCP sockets have the same METADATA mark as the subsequent provider request:
metadata off uses the ordinary route; metadata on stays protected by the VPN
and fails closed. These are not the imported profile's DNS servers. Downloads,
native/libc resolution and unclassified calls retain their existing policy.
There is no DNS cache or fallback to unmarked hostname resolution.
"""
import ipaddress
import secrets
import socket
import struct
import time
from pathlib import Path

RESOLVERS=('1.1.1.1','1.0.0.1')
HOSTS=Path('/etc/hosts')
TIMEOUT=5.0
ATTEMPT_TIMEOUT=1.0
MAX_MESSAGE=8192
MAX_RECORDS=128
MAX_CHAIN=8


class InvalidDNS(ValueError):
    pass


def _hostname(host):
    if isinstance(host,bytes):host=host.decode('ascii')
    if not isinstance(host,str):raise InvalidDNS('Invalid host name')
    name=host.encode('idna').decode('ascii').lower().removesuffix('.')
    labels=name.split('.')
    if (not name or len(name)>253 or any(not label or len(label)>63 or
        any(char not in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for char in label) for label in labels)):
        raise InvalidDNS('Invalid host name')
    return name


def _local(name):
    if name in ('localhost','localhost.localdomain') or name.endswith('.localhost'):
        return True
    if name==socket.gethostname().lower().rstrip('.'):
        return True
    # Static host entries are safe to hand to libc; arbitrary dotless/.local
    # names are not, because a search domain may send them to external DNS.
    try:
        with HOSTS.open('r',encoding='ascii',errors='ignore') as hosts:
            for line in hosts.read(65536).splitlines():
                fields=line.split('#',1)[0].split()
                if len(fields)>1:
                    try:ipaddress.ip_address(fields[0])
                    except ValueError:continue
                    if name in (alias.lower().rstrip('.') for alias in fields[1:]):return True
    except OSError:pass
    return False


def _name(message,start):
    """Read one bounded wire name, rejecting cycles and non-prior pointers."""
    position=start;following=None;labels=[];visited=set();size=0
    for _ in range(128):
        if position>=len(message) or position in visited:raise InvalidDNS('Invalid DNS name')
        visited.add(position);length=message[position]
        if length&0xc0==0xc0:
            if position+1>=len(message):raise InvalidDNS('Truncated DNS pointer')
            target=((length&0x3f)<<8)|message[position+1]
            if target>=position:raise InvalidDNS('Invalid DNS pointer')
            if following is None:following=position+2
            position=target;continue
        if length&0xc0:raise InvalidDNS('Invalid DNS label')
        position+=1
        if not length:
            return '.'.join(labels),following if following is not None else position
        if length>63 or position+length>len(message):raise InvalidDNS('Truncated DNS label')
        try:label=message[position:position+length].decode('ascii').lower()
        except UnicodeError:raise InvalidDNS('Invalid DNS label') from None
        if any(char not in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for char in label):raise InvalidDNS('Invalid DNS label')
        size+=length+1
        if size>254:raise InvalidDNS('DNS name too long')
        labels.append(label);position+=length
    raise InvalidDNS('DNS compression limit exceeded')


def _packet(name,qtype):
    transaction=secrets.randbits(16)
    encoded=b''.join(bytes([len(label)])+label.encode('ascii') for label in name.split('.'))+b'\0'
    return transaction,struct.pack('!6H',transaction,0x0100,1,0,0,0)+encoded+struct.pack('!2H',qtype,1)


def _parse(message,transaction,name,qtype):
    if not 12<=len(message)<=MAX_MESSAGE:raise InvalidDNS('Invalid DNS message size')
    identity,flags,questions,answers,authority,additional=struct.unpack('!6H',message[:12])
    if identity!=transaction or not flags&0x8000 or flags&0x7800 or questions!=1:
        raise InvalidDNS('Mismatched DNS response')
    question,offset=_name(message,12)
    if offset+4>len(message) or question!=name or struct.unpack('!2H',message[offset:offset+4])!=(qtype,1):
        raise InvalidDNS('Mismatched DNS question')
    offset+=4
    if flags&0x0200:return None,None
    rcode=flags&0xf
    if rcode==3:return [],name
    if rcode:raise InvalidDNS('DNS query failed')
    if answers+authority+additional>MAX_RECORDS:raise InvalidDNS('Too many DNS records')
    aliases={};addresses={}
    for index in range(answers+authority+additional):
        owner,offset=_name(message,offset)
        if offset+10>len(message):raise InvalidDNS('Truncated DNS record')
        kind,klass,_ttl,length=struct.unpack('!HHIH',message[offset:offset+10]);offset+=10
        end=offset+length
        if end>len(message):raise InvalidDNS('Truncated DNS record')
        if index<answers and klass==1:
            if kind==5:
                target,following=_name(message,offset)
                if following!=end or not target or owner in aliases and aliases[owner]!=target:
                    raise InvalidDNS('Invalid DNS alias')
                aliases[owner]=target
            elif kind==qtype and kind in (1,28):
                if length!={1:4,28:16}[kind]:raise InvalidDNS('Invalid DNS address')
                addresses.setdefault(owner,[]).append(str(ipaddress.ip_address(message[offset:end])))
        offset=end
    if offset!=len(message):raise InvalidDNS('Trailing DNS data')
    current=name;visited=set()
    for _ in range(MAX_CHAIN):
        if current in visited:raise InvalidDNS('DNS alias loop')
        visited.add(current)
        if current not in aliases:return list(dict.fromkeys(addresses.get(current,[]))),current
        current=aliases[current]
    raise InvalidDNS('DNS alias limit exceeded')


def _remaining(deadline):
    remaining=deadline-time.monotonic()
    if remaining<=0:raise TimeoutError('DNS deadline exceeded')
    return min(remaining,ATTEMPT_TIMEOUT)


def _receive(stream,length,deadline):
    result=bytearray()
    while len(result)<length:
        stream.settimeout(_remaining(deadline))
        block=stream.recv(length-len(result))
        if not block:raise InvalidDNS('Truncated DNS stream')
        result.extend(block)
    return bytes(result)


def _exchange(name,qtype,factory,deadline,*,resolvers=None):
    transaction,query=_packet(name,qtype)
    for resolver in RESOLVERS if resolvers is None else resolvers:
        try:
            _remaining(deadline)
            with factory(socket.AF_INET,socket.SOCK_DGRAM) as connection:
                connection.settimeout(_remaining(deadline));connection.connect((resolver,53))
                connection.settimeout(_remaining(deadline))
                connection.send(query)
                connection.settimeout(_remaining(deadline))
                reply,source=connection.recvfrom(MAX_MESSAGE+1)
                if source!=(resolver,53):raise InvalidDNS('Unexpected DNS source')
                addresses,canonical=_parse(reply,transaction,name,qtype)
            if addresses is None:
                # Truncated UDP replies use the same marked route and deadline.
                _remaining(deadline)
                with factory(socket.AF_INET,socket.SOCK_STREAM) as stream:
                    stream.settimeout(_remaining(deadline));stream.connect((resolver,53))
                    stream.settimeout(_remaining(deadline))
                    stream.sendall(struct.pack('!H',len(query))+query)
                    length=struct.unpack('!H',_receive(stream,2,deadline))[0]
                    if not 12<=length<=MAX_MESSAGE:raise InvalidDNS('Invalid DNS stream size')
                    addresses,canonical=_parse(_receive(stream,length,deadline),transaction,name,qtype)
                    if addresses is None:raise InvalidDNS('Truncated DNS stream')
            return addresses,canonical
        except (OSError,InvalidDNS):continue
    raise OSError('Metadata DNS resolution is unavailable')


def _resolve(name,qtype,factory,deadline,*,resolvers=None):
    current=name;visited=set()
    for _ in range(MAX_CHAIN):
        if current in visited:raise InvalidDNS('DNS alias loop')
        visited.add(current)
        addresses,canonical=_exchange(current,qtype,factory,deadline) if resolvers is None else _exchange(current,qtype,factory,deadline,resolvers=resolvers)
        if addresses or canonical==current:return addresses,canonical
        current=canonical
    raise InvalidDNS('DNS alias limit exceeded')


def getaddrinfo(original,factory,host,port,family=0,type=0,proto=0,flags=0):
    if host is None or flags&socket.AI_NUMERICHOST:
        return original(host,port,family,type,proto,flags)
    # Preserve libc's full numeric/IPv6-zone semantics without any DNS lookup.
    try:return original(host,port,family,type,proto,flags|socket.AI_NUMERICHOST)
    except socket.gaierror as exc:
        if exc.errno not in (socket.EAI_NONAME,getattr(socket,'EAI_NODATA',socket.EAI_NONAME)):
            raise
    try:name=_hostname(host)
    except (UnicodeError,InvalidDNS):raise socket.gaierror(socket.EAI_NONAME,'Invalid metadata host name') from None
    if _local(name):return original(host,port,family,type,proto,flags)
    if family not in (0,socket.AF_UNSPEC,socket.AF_INET,socket.AF_INET6):
        raise socket.gaierror(socket.EAI_FAMILY,'Unsupported address family')
    deadline=time.monotonic()+TIMEOUT;resolved=[];canonical=name;failed=False
    queries=[1] if family==socket.AF_INET else [28] if family==socket.AF_INET6 else [1,28]
    for qtype in queries:
        try:
            addresses,alias=_resolve(name,qtype,factory,deadline)
            resolved.extend(addresses)
            if addresses:canonical=alias
        except (OSError,InvalidDNS):failed=True
    if family==socket.AF_INET6 and flags&getattr(socket,'AI_V4MAPPED',0) and (not resolved or flags&getattr(socket,'AI_ALL',0)):
        try:
            addresses,alias=_resolve(name,1,factory,deadline);resolved.extend(addresses)
            if addresses:canonical=alias
        except (OSError,InvalidDNS):failed=True
    results=[]
    for address in dict.fromkeys(resolved):
        try:rows=original(address,port,family,type,proto,flags|socket.AI_NUMERICHOST)
        except socket.gaierror as exc:
            if exc.errno not in (socket.EAI_NONAME,getattr(socket,'EAI_NODATA',socket.EAI_NONAME),getattr(socket,'EAI_ADDRFAMILY',socket.EAI_NONAME)):
                raise
            continue
        for fam,kind,protocol,_canon,sockaddr in rows:
            results.append((fam,kind,protocol,canonical if not results and flags&socket.AI_CANONNAME else '',sockaddr))
    if not results:raise socket.gaierror(socket.EAI_AGAIN if failed else socket.EAI_NONAME,'Metadata name resolution failed')
    return results
