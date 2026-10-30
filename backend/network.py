"""Bounded HTTPS verification, pinned to validated public DNS addresses."""
import http.client
import ipaddress
import json
import re
import socket
import ssl
from urllib.parse import urlsplit


def public_origin(value):
    value = value.strip()
    if '://' not in value:
        value = 'https://' + value
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('Enter an HTTPS hostname without a path or port, such as https://reeldock.example.com.')
    host = parsed.hostname.encode('idna').decode('ascii').lower()
    if len(host) > 253 or '.' not in host or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                                    for label in host.split('.')):
        raise ValueError('Enter a valid public hostname.')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return 'https://' + host
    raise ValueError('Use the public hostname, not an IP address.')


def public_addresses(host):
    try:
        addresses = list(dict.fromkeys(result[4][0] for result in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    except OSError as exc:
        raise ValueError('The hostname could not be found. Check its spelling and Cloudflare DNS route, then retry.') from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError('The hostname must resolve to public addresses. Check its Cloudflare DNS route.')
    return addresses


def verify_route(origin, nonce, instance):
    host = urlsplit(origin).hostname
    addresses = public_addresses(host)
    # DNS is resolved once; connecting to that exact address prevents a second DNS lookup.
    connection = http.client.HTTPSConnection(host, timeout=8)
    try:
        raw = socket.create_connection((addresses[0], 443), timeout=8)
        try:
            connection.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        except Exception:
            raw.close()
            raise
        connection.request('GET', '/setup/probe/' + nonce, headers={'Host': host, 'Accept': 'application/json'})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('The hostname did not reach the setup probe. Check that the tunnel points at this ReelDock web service and port, and any Cloudflare Access policy.')
        body = response.read(4097)
        if len(body) > 4096:
            raise ValueError('The hostname returned an unexpected page.')
        data = json.loads(body)
        if data != {'nonce': nonce, 'instance': instance}:
            raise ValueError('This hostname reaches a different service or installation.')
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        raise ValueError('Could not verify this installation through HTTPS. Check DNS, tunnel status and that the tunnel points at this ReelDock web service and port.') from exc
    finally:
        connection.close()


def google_failure(host, status, data):
    """Only return fixed messages and recognised codes, never provider text."""
    messages = {
        'quotaExceeded': 'The YouTube API quota has been reached. Check the project quota in Google Cloud and retry when it is available.',
        'dailyLimitExceeded': 'The daily YouTube API quota has been reached. Retry after the quota resets.',
        'accessNotConfigured': 'Enable YouTube Data API v3 in the Google Cloud project that owns your OAuth client.',
        'SERVICE_DISABLED': 'Enable YouTube Data API v3 in the Google Cloud project that owns your OAuth client.',
        'invalid_client': 'Google rejected the OAuth client. Check that the saved Client ID and Client secret belong to the same Web application client.',
        'invalid_grant': 'Google rejected the authorisation code or refresh token. Start Connect YouTube again; check the callback URL if it repeats.',
        'redirect_uri_mismatch': 'The callback URL does not match the authorised redirect URI in your Google OAuth client.',
        'insufficientPermissions': 'Google did not grant the required YouTube read access. Reconnect and accept the requested permission.',
        'authError': 'Google rejected the access token. Reconnect YouTube.',
        'youtubeSignupRequired': 'This Google account needs a YouTube channel before its subscriptions can be read.',
        'rateLimitExceeded': 'Google temporarily limited requests. Wait a few minutes before retrying.',
        'userRateLimitExceeded': 'Google temporarily limited requests for this account. Wait a few minutes before retrying.',
    }
    codes = []
    error = data.get('error') if isinstance(data, dict) else None
    if isinstance(error, str):
        codes.append(error)
    elif isinstance(error, dict):
        for key in ('errors', 'details'):
            entries = error.get(key, [])
            if isinstance(entries, list):
                codes.extend(item.get('reason') for item in entries if isinstance(item, dict))
    stage = 'Google sign-in' if host == 'oauth2.googleapis.com' else 'YouTube API check'
    for code in codes:
        if isinstance(code, str) and code in messages:
            return f'{stage} failed (HTTP {status}, {code}). {messages[code]}'
    return f'{stage} failed (HTTP {status}). Google rejected the request. Check the OAuth client, permissions and API configuration.'


def google_request(host, path, body=None, token=None, method=None, content_type=None):
    headers = {'Accept': 'application/json'}
    if body is not None:
        headers['Content-Type'] = content_type or 'application/x-www-form-urlencoded'
    if token:
        headers['Authorization'] = 'Bearer ' + token
    connection = http.client.HTTPSConnection(host, timeout=15)
    try:
        connection.request(method or ('POST' if body is not None else 'GET'), path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError('Google returned an oversized response. Try again.')
        if response.status == 204 and method == 'DELETE':return {}
        data = json.loads(raw)
        if response.status != 200:
            # Never expose response bodies: they may contain sensitive data.
            raise ValueError(google_failure(host, response.status, data))
        if not isinstance(data, dict):
            raise ValueError('Google returned an unexpected response. Please try again.')
        return data
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        raise ValueError('Google could not be reached. Please try again.') from exc
    finally:
        connection.close()
