"""Assign a traffic class to Python sockets; the kernel enforces the policy.

Unclassified calls are downloads, so adding a new downloader fails closed.
Native children default to downloads via the bundled socket interposer.
"""
import os
import socket
import sys
import array
import struct
import time
from contextvars import ContextVar
from functools import wraps

_kind = ContextVar('vpn_traffic', default=None)
_dns_deadline = ContextVar('vpn_dns_deadline', default=None)

DOWNLOAD = 0xD01
METADATA = 0xD02
YOUTUBE = 0xD11
NZB = 0xD12
TORRENT = 0xD13
VOD = 0xD14
ENGINE_MARKS = {'youtube': YOUTUBE, 'nzb': NZB, 'torrent': TORRENT, 'vod': VOD}
ROLES = {DOWNLOAD: 'downloads', METADATA: 'metadata', **{mark: role for role, mark in ENGINE_MARKS.items()}}


def _scope(mark, *, preserve_engine=False):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            current = _kind.get()
            token = _kind.set(current if preserve_engine and current in ENGINE_MARKS.values() else mark)
            try: return function(*args, **kwargs)
            finally: _kind.reset(token)
        return wrapped
    return decorate


downloads = _scope(DOWNLOAD, preserve_engine=True)
metadata = _scope(METADATA)
youtube = _scope(YOUTUBE)
nzb = _scope(NZB)
torrent = _scope(TORRENT)
vod = _scope(VOD)

# Closest ReelDock frame identifies legacy helpers/threads lacking an explicit
# scope. Shared artwork/post-processing remains metadata, including NZB's use
# of the torrent metadata writer. Unknown engines remain conservative.
METADATA_MODULES = frozenset({
    'network', 'artwork', 'm3u_overrides', 'emby', 'm3u_metadata', 'm3u_sidecars', 'm3u_emby',
    'discover_sources', 'youtube_fallback', 'youtube_account',
    'youtube_source', 'youtube_push', 'watched_status', 'm3u', 'xtream',
    'youtube_extract', 'media_library', 'media_players.clients',
    'downloaders.nzb.indexers', 'downloaders.torrent.metadata', 'media_management.providers',
})
ENGINE_MODULES = {
    'downloads': YOUTUBE, 'discover_youtube': YOUTUBE,
    'm3u_downloads': VOD, 'm3u_player': VOD,
    'downloaders.nzb': NZB, 'downloaders.torrent': TORRENT,
}


def traffic_class():
    current = _kind.get()
    if current is not None: return current
    # A child extractor has its own frames, so explicit child intent must win
    # over the metadata default of its shared module. Nested decorators above
    # still deliberately override that intent for artwork/catalogue requests.
    role = os.environ.get('REELDOCK_TRAFFIC')
    if role == 'metadata': return METADATA
    if role in ENGINE_MARKS: return ENGINE_MARKS[role]
    frame = sys._getframe(1)
    while frame:
        module = frame.f_globals.get('__name__', '')
        if module.startswith('backend.') and not module.startswith('backend.vpn'):
            module = module.removeprefix('backend.')
            if any(module == name or module.startswith(name + '.') for name in METADATA_MODULES):
                return METADATA
            for name, mark in ENGINE_MODULES.items():
                if module == name or module.startswith(name + '.'):
                    return mark
            return DOWNLOAD
        frame = frame.f_back
    return DOWNLOAD


def subprocess_env(engine=None, env=None):
    """Copy child environment while carrying its exact routing intent."""
    result = dict(os.environ if env is None else env)
    mark = traffic_class() if engine is None else METADATA if engine == 'metadata' else ENGINE_MARKS.get(engine, DOWNLOAD)
    result['REELDOCK_TRAFFIC'] = ROLES.get(mark, 'downloads')
    return result


def install():
    if os.environ.get('REELDOCK_VPN_TRANSPORT') != '1' or getattr(socket, '_reeldock_vpn', False): return
    original = socket.socket
    original_getaddrinfo = socket.getaddrinfo
    class RoutedSocket(original):
        def __init__(self, family=-1, type=-1, proto=-1, fileno=None):
            if fileno is None and family in (socket.AF_INET, socket.AF_INET6, -1):
                family = socket.AF_INET if family == -1 else family
                type = socket.SOCK_STREAM if type == -1 else type
                proto = 0 if proto == -1 else proto
                fd = None
                try:
                    with original(socket.AF_UNIX, socket.SOCK_STREAM) as broker:
                        def budget():
                            deadline=_dns_deadline.get()
                            if deadline is None:return 3
                            remaining=deadline-time.monotonic()
                            if remaining<=0:raise TimeoutError('VPN DNS deadline exceeded')
                            return min(3,remaining)
                        broker.settimeout(budget())
                        broker.connect('/vpn-run/socket.sock')
                        broker.settimeout(budget())
                        broker.sendall(struct.pack('4i', family, type, proto, traffic_class()))
                        broker.settimeout(budget())
                        data, ancillary, flags, _ = broker.recvmsg(1, socket.CMSG_SPACE(4), socket.MSG_CMSG_CLOEXEC)
                    for level, kind, payload in ancillary:
                        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                            fds = array.array('i'); fds.frombytes(payload[:4]); fd = fds[0]
                    if fd is None: raise OSError()
                    super().__init__(family, type, proto, fd)
                except OSError:
                    if fd is not None: os.close(fd)
                    raise OSError('ReelDock VPN routing is unavailable; network request blocked.') from None
            else:
                super().__init__(family, type, proto, fileno)
    def routed_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        from .dns import TIMEOUT, getaddrinfo
        # Freeze the caller's class through all DNS helpers. Each UDP/TCP
        # resolver socket is brokered with that mark; a blocked class cannot
        # fall back to libc/WAN when another engine is permitted directly.
        intent = _kind.set(traffic_class())
        token = _dns_deadline.set(time.monotonic() + TIMEOUT)
        try: return getaddrinfo(original_getaddrinfo, RoutedSocket, host, port, family, type, proto, flags)
        finally:
            _dns_deadline.reset(token)
            _kind.reset(intent)

    socket.socket = RoutedSocket
    socket.getaddrinfo = routed_getaddrinfo
    socket._reeldock_vpn = True
