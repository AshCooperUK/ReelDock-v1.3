"""In-repo BitTorrent engine.

No libtorrent, Transmission, qBittorrent, WebTorrent, aria2, or other torrent
stack is imported or shelled out to. Sockets, SHA-1, and the MSE Diffie-Hellman
/ RC4 handshake are implemented here.

Implemented and covered by fixtures or localhost peers:
bencode, metainfo, the media gate, metadata-before-payload, TCP peer wire,
MSE (RC4), HTTP and UDP trackers, DHT KRPC, PEX, LSD packets, UPnP SOAP and
NAT-PMP packet mapping, HTTP and FTP web seeds, selective files, queue slots,
share-ratio stop, bandwidth buckets, and editable trackers.

Not implemented: uTP, WebRTC or WebSocket trackers, IPv6, DHT holepunch,
super-seeding, the fast extension, and Usenet/NZB.
"""
