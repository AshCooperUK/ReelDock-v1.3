"""UPnP IGD and NAT-PMP port mapping.

Both clients are real protocol implementations. This environment has no
guarantee of a gateway, so failures are reported and downloads continue.
"""

from __future__ import annotations

import socket
import struct
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


def natpmp_request(opcode: int, internal_port: int, external_port: int, lifetime: int = 3600) -> bytes:
    return struct.pack("!BBHHHI", 0, opcode, 0, internal_port, external_port, lifetime)


def parse_natpmp_response(data: bytes) -> dict:
    if len(data) < 16:
        raise ValueError("short NAT-PMP response")
    version, opcode, result, _seconds, internal, external, lifetime = struct.unpack("!BBHIHHI", data[:16])
    return {
        "ok": result == 0 and version == 0,
        "opcode": opcode,
        "result": result,
        "internal_port": internal,
        "external_port": external,
        "lifetime": lifetime,
    }


def default_gateway() -> str | None:
    try:
        lines = open("/proc/net/route", encoding="ascii").read().splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if len(fields) < 3 or fields[1] != "00000000":
            continue
        try:
            raw = bytes.fromhex(fields[2])
        except ValueError:
            continue
        if len(raw) != 4:
            continue
        return socket.inet_ntoa(raw[::-1])
    return None


def try_natpmp(internal_port: int, lifetime: int = 3600) -> dict:
    gateway = default_gateway()
    if not gateway or gateway.startswith("0."):
        return {"ok": False, "detail": "No NAT-PMP gateway"}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.5)
    mapped = []
    try:
        for opcode in (2, 1):  # TCP then UDP
            sock.sendto(natpmp_request(opcode, internal_port, internal_port, lifetime), (gateway, 5351))
            try:
                data, _addr = sock.recvfrom(64)
                parsed = parse_natpmp_response(data)
            except (OSError, ValueError) as exc:
                return {"ok": False, "detail": f"NAT-PMP {exc}"}
            if not parsed["ok"]:
                return {"ok": False, "detail": f"NAT-PMP result {parsed['result']}"}
            mapped.append(parsed["external_port"])
    finally:
        sock.close()
    return {"ok": True, "detail": f"NAT-PMP mapped {mapped[0]}", "external_port": mapped[0] if mapped else internal_port}


def ssdp_search() -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 1\r\n"
        "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
        "\r\n"
    ).encode("ascii")


def parse_ssdp_location(packet: bytes) -> str | None:
    text = packet.decode("latin1", "replace")
    for line in text.split("\r\n"):
        if line.lower().startswith("location:"):
            return line.split(":", 1)[1].strip()
    return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def find_wan_service(xml_bytes: bytes) -> tuple[str, str] | None:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    for node in root.iter():
        if _local(node.tag) != "service":
            continue
        values = {_local(child.tag): (child.text or "").strip() for child in list(node)}
        service = values.get("serviceType", "")
        control = values.get("controlURL", "")
        if "WANIPConnection" in service or "WANPPPConnection" in service:
            return service, control
    return None


def soap_add_port(service: str, external_port: int, internal_port: int, client: str, protocol: str) -> bytes:
    return f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:AddPortMapping xmlns:u="{service}">
<NewRemoteHost></NewRemoteHost>
<NewExternalPort>{external_port}</NewExternalPort>
<NewProtocol>{protocol}</NewProtocol>
<NewInternalPort>{internal_port}</NewInternalPort>
<NewInternalClient>{client}</NewInternalClient>
<NewEnabled>1</NewEnabled>
<NewPortMappingDescription>ReelDock</NewPortMappingDescription>
<NewLeaseDuration>3600</NewLeaseDuration>
</u:AddPortMapping>
</s:Body>
</s:Envelope>
""".encode("utf-8")


def local_ipv4() -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 80))
        address = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    if address.startswith("127."):
        return None
    return address


def try_upnp(internal_port: int) -> dict:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.5)
    try:
        sock.sendto(ssdp_search(), ("239.255.255.250", 1900))
        try:
            packet, _addr = sock.recvfrom(4096)
        except socket.timeout:
            return {"ok": False, "detail": "No UPnP response"}
    except OSError as exc:
        return {"ok": False, "detail": f"UPnP {exc}"}
    finally:
        sock.close()
    location = parse_ssdp_location(packet)
    if not location:
        return {"ok": False, "detail": "UPnP response had no location"}
    try:
        with urllib.request.urlopen(location, timeout=2) as response:
            description = response.read(256_000)
    except (OSError, urllib.error.URLError) as exc:
        return {"ok": False, "detail": f"UPnP description {exc}"}
    found = find_wan_service(description)
    if not found:
        return {"ok": False, "detail": "No WANIPConnection service"}
    service, control = found
    if control.startswith("/"):
        parsed = urllib.parse.urlparse(location)
        control = f"{parsed.scheme}://{parsed.netloc}{control}"
    client = local_ipv4()
    if not client:
        return {"ok": False, "detail": "No LAN address for UPnP"}
    for protocol in ("TCP", "UDP"):
        body = soap_add_port(service, internal_port, internal_port, client, protocol)
        request = urllib.request.Request(
            control,
            data=body,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f'"{service}#AddPortMapping"',
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.status >= 400:
                    return {"ok": False, "detail": f"UPnP HTTP {response.status}"}
        except (OSError, urllib.error.URLError) as exc:
            return {"ok": False, "detail": f"UPnP map {exc}"}
    return {"ok": True, "detail": f"UPnP mapped {internal_port}", "external_port": internal_port}
