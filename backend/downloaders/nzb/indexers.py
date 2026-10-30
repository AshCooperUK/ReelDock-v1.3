"""Newznab indexer addresses. NZBGeek and NZB.life both use this API."""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


class IndexerError(ValueError):
    pass


def api_base(url: str) -> str:
    """Normalise an indexer address. A host such as https://api.nzbgeek.info/ becomes …/api."""
    text = url.strip()
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise IndexerError("Enter an indexer address starting with https://.")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise IndexerError("Enter an indexer address starting with https://.")
    _assert_host(parsed.hostname)
    path = parsed.path.rstrip("/")
    if not path.endswith("/api"):
        path = (path + "/api") if path else "/api"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def caps(indexer: dict, opener=None) -> str:
    base = api_base(str(indexer.get("url") or ""))
    key = str(indexer.get("api_key") or "")
    address = base + "?" + urllib.parse.urlencode({"t": "caps", "apikey": key})
    data = _get(address, opener, limit=1_000_000)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise IndexerError("This indexer did not return a usable response.") from exc
    title = ""
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "title" and (element.text or "").strip():
            title = element.text.strip()
            break
    return title or "Connected."


def _get(url: str, opener, limit: int = 20_000_000) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise IndexerError("The indexer address is not valid.")
    _assert_host(parsed.hostname)
    request = urllib.request.Request(url, headers={"User-Agent": "ReelDock/0.1"})
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=25) as response:
            chunks = []
            total = 0
            while total <= limit:
                chunk = response.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                chunks.append(chunk)
    except IndexerError:
        raise
    except (OSError, urllib.error.URLError) as exc:
        raise IndexerError("The indexer could not be reached.") from exc
    if total > limit:
        raise IndexerError("The indexer response was too large.")
    return b"".join(chunks)


def _assert_host(hostname: str) -> None:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise IndexerError("The indexer address could not be found.") from exc
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if address.is_link_local or address.is_multicast or address.is_unspecified or address.is_reserved:
            raise IndexerError("That indexer address cannot be used.")
