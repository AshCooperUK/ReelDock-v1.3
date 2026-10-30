"""YouTube watched requests. Never infer account history from HTTP success."""
import http.cookiejar
import json
import re
import time
import requests
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from .vpn.transport import subprocess_env


def cookie_session_file_status(cookie_path):
    try:
        jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
        jar.load(ignore_discard=True, ignore_expires=True)
        tokens = [c for c in jar if c.domain.lstrip('.') == 'youtube.com' and c.name in
                  {'SID','SAPISID','__Secure-1PAPISID','__Secure-3PAPISID'}]
        if not tokens:
            return 'auth_required', 'Signed-in YouTube cookies are missing. Upload a fresh cookie file in Settings > Google & YouTube.'
        if not any(c.expires is None or c.expires > time.time() for c in tokens):
            return 'auth_expired', 'Your YouTube cookies have expired. Upload fresh signed-in cookies in Settings > Google & YouTube. Watched requests are paused.'
        return 'unchecked', 'Cookie file available. The signed-in session will be checked before each watched request.'
    except (OSError, ValueError, http.cookiejar.LoadError):
        return 'auth_required', 'YouTube cookies are missing or invalid. Upload fresh signed-in cookies in Settings > Google & YouTube.'


def check_cookie_session(cookie_path):
    state,message = cookie_session_file_status(cookie_path)
    if state != 'unchecked':
        return state,message
    try:
        jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
        jar.load(ignore_discard=True, ignore_expires=False)
        with requests.Session() as session:
            session.cookies.update(jar)
            response = session.get('https://www.youtube.com/feed/history', timeout=(5,15),
                headers={'User-Agent':'Mozilla/5.0','Accept-Language':'en-GB,en;q=0.9'})
            response.raise_for_status()
        host=(requests.utils.urlparse(response.url).hostname or '').lower()
        if host in ('consent.youtube.com','consent.google.com'):
            return 'auth_unverified', 'YouTube returned a consent page. Complete consent in the cookie account, then upload fresh cookies. No watched request was sent.'
        if host in ('accounts.google.com','accounts.youtube.com'):
            return 'auth_expired', 'YouTube redirected to sign-in. Upload fresh signed-in cookies. No watched request was sent.'
        # Only explicit server confirmation counts as an active signed-in session.
        states = []
        for match in re.finditer(r'ytcfg\.set\(\s*', response.text):
            try:
                config,_ = json.JSONDecoder().raw_decode(response.text[match.end():])
                if isinstance(config,dict) and isinstance(config.get('LOGGED_IN'),bool):
                    states.append(config['LOGGED_IN'])
            except ValueError:
                continue
        if False in states:
            return 'auth_expired', 'YouTube rejected the saved session. Upload fresh signed-in cookies in Settings > Google & YouTube. No watched request was sent.'
        if True in states:
            return 'active', 'YouTube confirmed an active signed-in cookie session.'
        return 'auth_unverified', 'Unable to confirm a signed-in YouTube session. Check cookies, consent and network access. No watched request was sent.'
    except requests.exceptions.SSLError:
        return 'network_error', 'YouTube session check failed: TLS certificate verification failed. Check the NAS clock and trusted certificates. No watched request was sent.'
    except requests.Timeout:
        return 'network_error', 'YouTube session check timed out. Check NAS internet access. No watched request was sent; the check will retry.'
    except requests.HTTPError as exc:
        code=exc.response.status_code if exc.response is not None else None
        if code==429:return 'rate_limited', 'YouTube limited the session checks (HTTP 429). No watched request was sent; retry is delayed for an hour.'
        if code==403:return 'auth_unverified', 'YouTube blocked the session check (HTTP 403). Check the NAS network and uploaded cookies. No watched request was sent.'
        return 'network_error', f'YouTube session check returned HTTP {code or "error"}. No watched request was sent; the check will retry.'
    except requests.ConnectionError:
        return 'network_error', 'Could not connect to YouTube for the session check. Check NAS DNS and internet access. No watched request was sent.'
    except requests.RequestException:
        return 'network_error', 'YouTube session check failed before sending a watched request. Check NAS network access.'


def describe_failure(output, returncode):
    """Use fixed explanations; extractor output can contain private URLs/cookies."""
    errors=[line for line in output.splitlines() if 'ERROR:' in line]
    text=('\n'.join(errors) if errors else output).lower()
    rules=[
        (('no supported javascript runtime','javascript runtime','javascript challenge','signature solving','n challenge'), 'The YouTube JavaScript challenge could not be solved. Update both ReelDock containers; this build enables the bundled Node runtime.'),
        (("not a bot",'confirm you’re not a bot','confirm you are not a bot','confirm you’re not a bot'), 'YouTube requested a bot/sign-in check. Refresh signed-in cookies in Settings > Google & YouTube and check that YouTube works from the NAS network.'),
        (('cookies are no longer valid','cookies have expired','login required','sign in to confirm your age','age-restricted','age restricted'), 'YouTube requires a valid signed-in or age-verified session. Upload fresh cookies from the correct YouTube account.'),
        (('private video','video unavailable','video is unavailable','video has been removed','members-only','members only','not available in your country'), 'The video is private, removed, restricted or unavailable to the cookie account. Check this video on YouTube with that account.'),
        (('429','too many requests'), 'YouTube is rate-limiting requests. Wait before retrying.'),
        (('certificate verify failed','certificate_verify_failed'), 'The NAS could not verify YouTube’s TLS certificate. Check its clock and trusted certificates.'),
        (('name resolution','getaddrinfo','name or service not known','nodename nor servname'), 'The NAS could not resolve YouTube’s address. Check its DNS settings.'),
        (('timed out','timeout'), 'The connection to YouTube timed out. Check NAS internet access.'),
        (('403','forbidden'), 'YouTube rejected the request (HTTP 403). Check the cookie account and NAS network access.'),
        (('requested format','no video formats','only images are available'), 'yt-dlp could not extract a playable format. Watched checks now allow missing formats; update both containers before retrying.'),
        (('unable to mark','unable to download webpage'), 'yt-dlp could not complete YouTube’s watched endpoint request. Check cookies, video access and NAS connectivity.'),
    ]
    for needles,message in rules:
        if any(needle in text for needle in needles):return message
    return f'yt-dlp stopped with exit code {returncode}. The failure did not match a known cause. Update both containers and retry this video; private extractor output is not displayed.'


def interpret_result(returncode, output):
    text=output.lower()
    attempted='marking watched' in text or 'marking fully watched' in text
    failed_mark='unable to mark' in text
    if not returncode and 'marking fully watched' in text and not failed_mark:
        return 'sent', 'YouTube watched request completed without a reported endpoint error. Check the video’s progress bar on the cookie account to confirm YouTube retained it.'
    reason=describe_failure(output,returncode)
    if attempted or failed_mark:
        return 'unverified', reason+' A watched request may have reached YouTube. Check its history before choosing Retry; this will not retry automatically.'
    if returncode:
        return 'extractor_failed', reason+' No watched request was reported. Choose Retry after addressing the cause.'
    return 'unverified', 'yt-dlp did not report a fully-watched request. YouTube completion is unverified; check its history before retrying.'


def send_watched_request(video_id, cookie_path):
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}',video_id):return 'extractor_failed','Invalid saved video identifier. No watched request was sent.'
    state,message=cookie_session_file_status(cookie_path)
    if state!='unchecked':return state,message
    # Work on a private cookie copy so yt-dlp cannot rewrite the downloader's jar.
    with tempfile.TemporaryDirectory(prefix='reeldock-watched-') as directory:
        cookies = Path(directory) / 'cookies.txt'
        try:shutil.copyfile(cookie_path, cookies)
        except OSError:return 'auth_required','Could not read the uploaded cookies. Upload a new file in Settings > Google & YouTube. No request was sent.'
        cookies.chmod(0o600)
        state,message = check_cookie_session(cookies)
        if state != 'active':
            return state,message
        command = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--simulate',
                   '--mark-watched', '--js-runtimes', 'node', '--ignore-no-formats-error', '--no-playlist', '--no-progress', '--no-colors',
                   '--socket-timeout', '15', '--retries', '0', '--extractor-retries', '0',
                   '--cookies', str(cookies), 'https://www.youtube.com/watch?v=' + video_id]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=90, env=subprocess_env('metadata'))
        except subprocess.TimeoutExpired:
            return 'unverified', 'yt-dlp timed out after 90 seconds. A watched request may have reached YouTube; check its history before retrying.'
        except OSError:
            return 'extractor_failed', 'Could not start yt-dlp. Update both ReelDock containers. No watched request was sent.'
    # Do not persist raw extractor output: it can contain account/session information.
    output = (result.stdout or '') + '\n' + (result.stderr or '')
    return interpret_result(result.returncode,output)
