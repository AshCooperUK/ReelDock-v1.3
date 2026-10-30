"""NZBgeek catalogue, public YouTube charts and TVmaze metadata.

Provider credentials stay on the server. Public objects never include grab
addresses or API keys. A failed chart refresh keeps the last good snapshot.
"""
import json
import re
import time
import ipaddress
import socket
import threading
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

from .discover_logic import FEED_NAMES, FEED_LIMIT, CHARTS, next_offset, opaque_id, redact, release_covers, release_pairs, same_external_id
from .discover_store import initialise

ROOT = Path(__file__).resolve().parent
SEED = ROOT / 'discover_seed' / 'youtube.json'
USER_AGENT = 'ReelDock/0.1'
LOCK = threading.Lock()
EPISODE_CACHE = {}
UPLOAD_CACHE = {}
SEARCH_CACHE = {}
SEARCH_SLOTS = threading.BoundedSemaphore(2)
REFRESH_INTERVAL = 12 * 3600
BACKOFF = 30 * 60
COVER_RE = re.compile(r'https://api\.nzb(?:geek\.info|\.life)/covers/[A-Za-z0-9/_.-]+')
LOOKUP_LIMIT = 80
LOOKUP_BUDGET = 25
LOOKUP_TTL = 7 * 24 * 3600
FEED_FALLBACK = {
    'trending_movies': {'t': 'movie', 'cat': '2000'},
    'new_movies': {'t': 'movie', 'cat': '2000'},
    'trending_shows': {'t': 'tvsearch', 'cat': '5000'},
}


class DiscoverError(ValueError):
    pass


def failure_reason(error):
    """Fixed diagnostic codes only; exceptions can contain private provider URLs."""
    reasons=getattr(error,'failures',None)
    if reasons:return ','.join(sorted(set(reasons)))
    cause=error
    while cause is not None:
        if isinstance(cause,HTTPError):return 'http_'+str(cause.code)
        if isinstance(cause,(TimeoutError,socket.timeout)):return 'timeout'
        if isinstance(cause,URLError) and isinstance(cause.reason,(TimeoutError,socket.timeout)):return 'timeout'
        cause=cause.__cause__
    reason=getattr(error,'reason','')
    return reason if isinstance(reason,str) and re.fullmatch(r'indexer_(?:[0-9]+|error)',reason) else 'provider_error'


def _public_host(hostname):
    if not hostname or hostname.lower() == 'localhost':
        raise DiscoverError('That address cannot be used.')
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise DiscoverError('The provider address could not be found.') from exc
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if address.is_private or address.is_loopback or address.is_link_local or address.is_multicast or address.is_reserved or address.is_unspecified:
            raise DiscoverError('That address cannot be used.')


class _Redirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _public_host(urlsplit(newurl).hostname)
        if urlsplit(newurl).scheme != 'https':
            raise DiscoverError('That address cannot be used.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, limit=4_000_000, timeout=25):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname:
        raise DiscoverError('That address cannot be used.')
    _public_host(parsed.hostname)
    opener = build_opener(_Redirects)
    request = Request(url, headers={'User-Agent': USER_AGENT})
    try:
        with opener.open(request, timeout=timeout) as response:
            chunks, total = [], 0
            while total <= limit:
                chunk = response.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                chunks.append(chunk)
    except DiscoverError:
        raise
    except HTTPError as exc:
        raise DiscoverError('The provider returned HTTP ' + str(exc.code) + '.') from exc
    except (OSError, URLError) as exc:
        raise DiscoverError('The provider could not be reached.') from exc
    if total > limit:
        raise DiscoverError('The provider response was too large.')
    data = b''.join(chunks)
    if not _download_allowed(data):
        raise DiscoverError('Unsupported provider response.')
    return data


def _download_allowed(data):
    """RSS and NZB documents both use a doctype. A doctype with no known root is rejected."""
    head = data[:4096].lower()
    if b'<!entity' in head:
        return False
    if b'<!doctype' in head and b'<rss' not in head and b'<html' not in head and b'<nzb' not in head:
        return False
    return True


def indexers_for(app, store):
    """Read the indexer account without starting the downloader.

    Opening the NZB engine resumes every saved job. Discover only needs the
    account, and doing that from the worker made it download inside both processes.
    """
    path = Path(store.directory) / 'nzb-engine' / 'settings.json'
    try:
        config = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    rows = [item for item in config.get('indexers') or [] if isinstance(item, dict) and item.get('enabled', True) and item.get('api_key') and item.get('url')]
    if not rows:
        raise DiscoverError('NZB indexer is not configured. Add it in Settings, then try again.')
    return rows


def previous_indexer_sets(indexers):
    """Safe cache reuse only when every earlier account is still enabled."""
    seen=set()
    for subset in [*[indexers[:n] for n in range(1,len(indexers))],*[indexers[:n]+indexers[n+1:] for n in range(len(indexers))]]:
        key=json.dumps(subset,sort_keys=True)
        if subset and key not in seen:
            seen.add(key);yield subset


def _strip_doctype(data):
    if b'<!ENTITY' in data.upper():
        raise DiscoverError('Unsupported provider response.')
    if b'<!DOCTYPE' in data.upper() and b'<rss' not in data.lower():
        raise DiscoverError('Unsupported provider response.')
    return re.sub(br'<!DOCTYPE\s+[^>\[]*(?:\[[^\]]*\])?\s*>', b'', data, count=1, flags=re.IGNORECASE)


def _rss_root(data):
    try:
        root = ET.fromstring(_strip_doctype(data))
    except ET.ParseError as exc:
        raise DiscoverError('The provider returned an unexpected response.') from exc
    tag = root.tag.rsplit('}', 1)[-1]
    if tag == 'error':
        code = str(root.get('code') or '')
        if code == '202':
            raise DiscoverError('missing-function')
        error=DiscoverError(redact(root.get('description') or 'The indexer rejected this collection.'))
        error.reason='indexer_'+code if code.isdigit() else 'indexer_error'
        raise error
    if tag != 'rss':
        raise DiscoverError('The provider returned an unexpected response.')
    return root


def _cover(value):
    text = str(value or '').strip()
    return text if COVER_RE.fullmatch(text) else ''


def _provider_name(indexer):
    name = re.sub(r'[\x00-\x1f]+', ' ', str(indexer.get('name') or '')).strip()
    host = urlsplit(str(indexer.get('url') or '')).hostname or ''
    if not name or re.search(r'(?i)https?://|[?&](?:api_?key|token|password)=',name) or '---' in name or (indexer.get('api_key') and str(indexer['api_key']) in name):
        return host or 'NZB catalogue'
    return name[:40]


def _kind_identity(feed, item):
    kind = FEED_NAMES[feed]
    identity = (item.findtext('imdbid') or item.findtext('tvdbid') or '').strip()
    if not identity.isdigit():
        for attr in item:
            if not str(attr.tag).endswith('attr'):
                continue
            name = (attr.get('name') or '').lower()
            if kind == 'movie' and name in ('imdb', 'imdbid'):
                identity = (attr.get('value') or '').removeprefix('tt')
            if kind == 'show' and name in ('tvdbid', 'tvdb'):
                identity = attr.get('value') or ''
    return kind, identity if identity.isdigit() else ''


def parse_feed(data, feed, provider='NZB catalogue'):
    if feed not in FEED_NAMES:
        raise DiscoverError('Unknown collection.')
    root = _rss_root(data)
    result = []
    seen = set()
    for item in root.findall('./channel/item')[:FEED_LIMIT]:
        kind, identity = _kind_identity(feed, item)
        attrs = _attrs(item)
        release = item.findtext('title') or ''
        if kind == 'movie' and not (attrs.get('imdbtitle') or '').strip() and release_pairs(release):
            continue
        title = (attrs.get('imdbtitle') or release).strip() if kind == 'movie' else release.strip()
        if not title or not identity or identity in seen:
            continue
        seen.add(identity)
        year_match = re.search(r'\((\d{4})\)$', title)
        year = year_match.group(1) if year_match else ''
        if not year and re.fullmatch(r'\d{4}', attrs.get('imdbyear') or ''):
            year = attrs['imdbyear']
        image = _cover(item.findtext('coverurl') or '')
        if not image:
            for attr in item:
                if str(attr.tag).endswith('attr') and (attr.get('name') or '').lower() == 'coverurl':
                    image = _cover(attr.get('value') or '')
                    break
        plot = text_summary(attrs.get('imdbplot') or '')
        genres = [part.strip() for part in (attrs.get('genre') or '').split(',') if part.strip()]
        score = str(attrs.get('imdbscore') or '').strip()
        if not re.fullmatch(r'\d{1,2}(?:\.\d)?', score):
            score = ''
        result.append({
            'id': kind + ':' + identity,
            'title': re.sub(r'\s*\(\d{4}\)$', '', title),
            'year': year,
            'kind': kind,
            'externalId': identity,
            'image': image,
            'provider': provider,
            'feed': feed,
            'live': True,
            'genre': ', '.join(genres[:3]),
            'rating': score,
            'description': plot or 'Discovered through the NZB catalogue. Quality is checked when you request a download.',
        })
    return result


def _norm_title(value):
    text = str(value or '').casefold().replace("'", '').replace('’', '')
    return re.sub(r'[^a-z0-9]+', ' ', text).strip()[:180]


def _matched_show(title, payload):
    key = _norm_title(title)
    if not key or not isinstance(payload, list):
        return None
    exact, prefix = [], []
    for row in payload:
        show = row.get('show') if isinstance(row, dict) else None
        if not isinstance(show, dict):
            continue
        tvdb = str((show.get('externals') or {}).get('thetvdb') or '')
        name = _norm_title(show.get('name'))
        if not tvdb.isdigit() or not name:
            continue
        if name == key:
            exact.append(show)
        elif name.startswith(key + ' '):
            prefix.append(show)
    chosen = exact or prefix
    ids = {str((show.get('externals') or {}).get('thetvdb')) for show in chosen}
    if len(ids) != 1:
        return None
    return chosen[0]


def _show_card(show):
    image = show.get('image') if isinstance(show, dict) else None
    medium = image.get('medium') if isinstance(image, dict) else ''
    if not re.fullmatch(r'https://static\.tvmaze\.com/uploads/images/[A-Za-z0-9/_.-]+', medium or ''):
        medium = ''
    genres = [str(part) for part in (show.get('genres') or []) if isinstance(part, str)]
    rating = show.get('rating') if isinstance(show, dict) else None
    score = ''
    if isinstance(rating, dict) and isinstance(rating.get('average'), (int, float)):
        score = f'{rating["average"]:.1f}'.rstrip('0').rstrip('.')
        if not re.fullmatch(r'\d{1,2}(?:\.\d)?', score):
            score = ''
    premiered = str(show.get('premiered') or '')
    return {
        'releaseDate': premiered if re.fullmatch(r'\d{4}-\d{2}-\d{2}',premiered) else '',
        'title': str(show.get('name') or '')[:180],
        'image': medium,
        'genre': ', '.join(genres[:3])[:80],
        'rating': score,
        'year': premiered[:4] if re.fullmatch(r'\d{4}-\d{2}-\d{2}', premiered) else '',
        'description': text_summary(show.get('summary') or ''),
    }


def _cached_tvdb(store, keys):
    if not keys:
        return {}
    placeholders = ','.join('?' for _ in keys)
    with store.connect() as db:
        rows = db.execute(
            f'SELECT name,tvdb,checked FROM discover_show_lookup WHERE name IN ({placeholders})', keys).fetchall()
    now = time.time()
    return {row['name']: row['tvdb'] for row in rows if now - row['checked'] < LOOKUP_TTL}


def _remember_tvdb(store, key, tvdb):
    with store.connect() as db:
        db.execute('''INSERT INTO discover_show_lookup(name,tvdb,checked) VALUES (?,?,?)
            ON CONFLICT(name) DO UPDATE SET tvdb=excluded.tvdb, checked=excluded.checked''',
                   (key, tvdb, time.time()))


def _remember_show(store, tvdb, card):
    with store.connect() as db:
        db.execute('''INSERT INTO discover_show_media(tvdb,body,checked) VALUES (?,?,?)
            ON CONFLICT(tvdb) DO UPDATE SET body=excluded.body, checked=excluded.checked''',
                   (tvdb, json.dumps(card), time.time()))


def _load_shows(store, ids):
    wanted = [tvdb for tvdb in ids if str(tvdb).isdigit()]
    if not wanted:
        return {}
    placeholders = ','.join('?' for _ in wanted)
    with store.connect() as db:
        rows = db.execute(f'SELECT tvdb,body FROM discover_show_media WHERE tvdb IN ({placeholders})', wanted).fetchall()
    found = {}
    for row in rows:
        try:
            found[row['tvdb']] = json.loads(row['body'])
        except json.JSONDecodeError:
            continue
    return found


def _resolve_shows(store, rows):
    cached = _cached_tvdb(store, [_norm_title(name) for row in rows for name in row['candidates']])
    calls = 0
    started = time.time()
    for row in rows:
        for name in row['candidates']:
            key = _norm_title(name)
            if cached.get(key):
                break
            if key in cached:
                continue
            if calls >= LOOKUP_LIMIT or time.time() - started > LOOKUP_BUDGET:
                return cached
            calls += 1
            try:
                payload = json.loads(fetch(
                    'https://api.tvmaze.com/search/shows?q=' + quote(name), limit=1_000_000, timeout=4))
            except DiscoverError:
                continue
            show = _matched_show(name, payload)
            tvdb = str((show.get('externals') or {}).get('thetvdb') or '') if show else ''
            if not tvdb.isdigit():
                tvdb = ''
            cached[key] = tvdb
            _remember_tvdb(store, key, tvdb)
            if tvdb:
                _remember_show(store, tvdb, _show_card(show))
                break
    return cached


def _show_name(release):
    prefix = re.split(r'(?i)[. _-]+(?:S[0-9]{1,4}(?:E[0-9]{1,4})?|season[. _-]*\d+|\d{4}[. _-]\d{2}[. _-]\d{2})(?:[. _-]|$)', release, maxsplit=1)[0]
    name = re.sub(r'[._]+', ' ', prefix)
    name = re.sub(r'^\[[^\]]+\]\s*', '', name)
    return re.sub(r'\s+', ' ', name).strip(' -')


def _name_candidates(name):
    found = [name] if name else []
    for extra in (re.sub(r'\s+(?:19|20)\d{2}$', '', name), re.sub(r'\s+\d{1,3}$', '', name)):
        if extra and extra not in found and len(extra) > 2:
            found.append(extra)
    return found


def shows_from_browse(store, data, feed, provider):
    """Group a standard TV search by the release's show name and keep one TVmaze match."""
    initialise(store)
    root = _rss_root(data)
    grouped = []
    seen = set()
    for item in root.findall('./channel/item')[:FEED_LIMIT]:
        attrs = _attrs(item)
        title = _show_name(item.findtext('title') or '')
        key = _norm_title(title)
        if not key or key in seen:
            continue
        seen.add(key)
        air = attrs.get('tvairdate') or ''
        grouped.append({
            'key': key, 'title': title, 'candidates': _name_candidates(title),
            'image': _cover(attrs.get('coverurl') or ''),
            'year': air[:4] if re.fullmatch(r'\d{4}-\d{2}-\d{2}', air) else '',
        })
    resolved = _resolve_shows(store, grouped[:200])
    chosen = []
    used = set()
    for row in grouped:
        title, tvdb = row['title'], ''
        for name in row['candidates']:
            found = resolved.get(_norm_title(name)) or ''
            if found.isdigit():
                title, tvdb = name, found
                break
        if not tvdb or tvdb in used:
            continue
        used.add(tvdb)
        chosen.append((row, title, tvdb))
    media = _load_shows(store, [tvdb for _row, _title, tvdb in chosen])
    for tvdb in [item[2] for item in chosen if item[2] not in media][:30]:
        info = show_media(tvdb)
        card = {'title': '', 'image': info.get('image') or '', 'genre': info.get('genre') or '',
                'rating': info.get('rating') or '', 'year': info.get('year') or '', 'description': info.get('summary') or ''}
        if any(card.values()):
            _remember_show(store, tvdb, card)
            media[tvdb] = card
    result = []
    for row, title, tvdb in chosen:
        card = media.get(tvdb) or {}
        result.append({
            'id': 'show:' + tvdb,
            'title': card.get('title') or title,
            'year': card.get('year') or row['year'],
            'kind': 'show',
            'externalId': tvdb,
            'image': card.get('image') or row['image'],
            'provider': provider,
            'feed': feed,
            'live': True,
            'genre': card.get('genre') or '',
            'rating': card.get('rating') or '',
            'description': card.get('description') or 'Matched to TVDB because the release name agrees with one TVmaze show. CC BY-SA.',
        })
    finished = all(
        any(resolved.get(_norm_title(name)) for name in row['candidates']) or all(_norm_title(name) in resolved for name in row['candidates'])
        for row in grouped[:200])
    return result, finished


def _indexer_query(indexer, params):
    from .downloaders.nzb.indexers import api_base
    base = api_base(str(indexer.get('url') or ''))
    query = {'apikey': indexer.get('api_key') or '', 'extended': 1, 'limit': FEED_LIMIT, **params}
    return fetch(base + '?' + urlencode(query), limit=8_000_000, timeout=18)


def merge_rss(documents):
    """Join Newznab pages into one channel. NZB.life returns about 100 items per call."""
    rss = ET.Element('rss')
    channel = ET.SubElement(rss, 'channel')
    for data in documents:
        for item in _rss_root(data).findall('./channel/item'):
            channel.append(item)
    if not list(channel):
        raise DiscoverError('No titles were returned.')
    return ET.tostring(rss, encoding='utf-8')


def _paged_search(indexer, params, pages=6):
    documents = []
    for page in range(pages):
        data = _indexer_query(indexer, {**params, 'limit': 100, 'offset': page * 100})
        documents.append(data)
        if len(_rss_root(data).findall('./channel/item')) < 90:
            break
    return merge_rss(documents)


def _named_catalogue(indexer, name):
    try:
        data = _indexer_query(indexer, {'t': name})
        _rss_root(data)
        return data
    except DiscoverError as exc:
        if str(exc) != 'missing-function':
            named_error = exc
        else:
            named_error = None
    from .downloaders.nzb.indexers import api_base
    rss = api_base(str(indexer.get('url') or '')).rsplit('/api', 1)[0] + '/rss'
    url = rss + '?' + urlencode({'t': name, 'limit': FEED_LIMIT, 'r': indexer.get('api_key') or ''})
    try:
        data = fetch(url, limit=8_000_000, timeout=18)
        _rss_root(data)
        return data
    except DiscoverError:
        if named_error:
            raise named_error
        raise DiscoverError('missing-function')


def refresh_catalogue(app, store):
    """Refresh one stale collection in the background so opening TV shows stays quick."""
    with store.connect() as db:
        rows = db.execute('SELECT name,fetched FROM discover_feeds').fetchall()
    ages = {row['name']: row['fetched'] for row in rows}
    now = time.time()
    for name in ('trending_shows', 'trending_movies', 'new_movies'):
        if now - float(ages.get(name) or 0) <= 900:
            continue
        try:
            feed(app, store, name, refresh=True)
        except DiscoverError:
            return
        return


def feed(app, store, name, refresh=False):
    initialise(store)
    if name not in FEED_NAMES:
        raise DiscoverError('Unknown collection.')
    with store.connect() as db:
        saved = db.execute('SELECT body,fetched FROM discover_feeds WHERE name=?', (name,)).fetchone()
    age = time.time() - saved['fetched'] if saved else None
    if saved and (age < 900 or (not refresh and age < 6 * 3600)):
        return json.loads(saved['body'])
    try:
        indexer = indexers_for(app, store)[0]
        provider = _provider_name(indexer)
        scope = 'NZB catalogue'
        try:
            data = _named_catalogue(indexer, name)
        except DiscoverError as exc:
            fallback = FEED_FALLBACK.get(name)
            if str(exc) != 'missing-function' or not fallback:
                if str(exc) == 'missing-function':
                    raise DiscoverError('This indexer does not publish that collection.') from exc
                raise
            data = _paged_search(indexer, fallback) if fallback.get('t') == 'tvsearch' else _indexer_query(indexer, fallback)
            _rss_root(data)
            scope = 'Recent releases from ' + provider
        items = parse_feed(data, name, provider)
        finished = True
        if FEED_NAMES[name] == 'show' and not items:
            items, finished = shows_from_browse(store, data, name, provider)
            if items:
                scope = 'Recent releases from ' + provider + '. Release names matched through TVmaze.'
        if not items and name not in FEED_FALLBACK:
            raise DiscoverError('This indexer does not publish that collection.')
        payload = {
            'items': items, 'fetchedAt': int(time.time()), 'source': 'live', 'feed': name,
            'stale': False, 'scope': scope,
        }
        if not finished:
            payload['warning'] = 'More shows are matched each time you open this collection.'
        else:
            with store.connect() as db:
                db.execute('INSERT INTO discover_feeds(name,body,fetched) VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET body=excluded.body, fetched=excluded.fetched',
                           (name, json.dumps(payload), time.time()))
        return payload
    except Exception as exc:
        if saved:
            payload = json.loads(saved['body'])
            payload['stale'] = True
            payload['warning'] = 'Provider unavailable; showing the previous results.'
            return payload
        message = str(exc)
        if message == 'missing-function':
            message = 'This indexer does not publish that collection.'
        raise DiscoverError(redact(message)) from exc


def _attrs(item):
    return {attr.get('name'): attr.get('value', '') for attr in item if str(attr.tag).endswith('attr')}


def _grab(item):
    enclosure = item.find('enclosure')
    url = enclosure.get('url') if enclosure is not None else ''
    if url.startswith('https://') and ' ' not in url:
        return url
    link = item.findtext('link') or ''
    if link.startswith('https://') and ('t=get' in link or link.lower().endswith('.nzb')):
        return link
    return ''


def _search_details(attrs, release, kind):
    title = (attrs.get('imdbtitle') or '').strip() if kind == 'movie' else ''
    if not title and kind == 'show':
        title = (attrs.get('tvtitle') or '').strip()
    if not title:
        if kind=='movie':
            from .m3u_naming import tidy
            title=tidy(release.replace('.', ' ').replace('_', ' '))
        else:title = _show_name(release) or release.strip()
    found=re.search(r'\s+\(((?:19|20)\d{2})\)$' if (attrs.get('imdbtitle') or '').strip() else r'\s+\(?((?:19|20)\d{2})\)?$',title)
    year = attrs.get('imdbyear') or (found[1] if found else '')
    if not re.fullmatch(r'\d{4}', year):year = ''
    if found:title=title[:found.start()].strip()
    image = _cover(attrs.get('coverurl') or '')
    plot = text_summary(attrs.get('imdbplot') or '')
    genres = [part.strip() for part in (attrs.get('genre') or '').split(',') if part.strip()]
    score = str(attrs.get('imdbscore') or '').strip()
    if not re.fullmatch(r'\d{1,2}(?:\.\d)?', score):
        score = ''
    return title, year, image, ', '.join(genres[:3]), score, plot


def parse_search(data, provider, offset, source='NZB catalogue'):
    try:
        root = _rss_root(data)
    except DiscoverError as exc:
        if str(exc) in ('The provider returned an unexpected response.', 'Unsupported provider response.'):
            raise DiscoverError('Unexpected search response.') from exc
        raise DiscoverError('NZBgeek could not complete this search. Check the indexer account or API allowance.') from exc
    groups = {}
    secret = []
    rows = root.findall('./channel/item')[:100]
    for row in rows:
        attrs = _attrs(row)
        release = row.findtext('title') or ''
        grab = _grab(row)
        if not release or not grab:
            continue
        kind = 'movie' if provider == 'movies' else 'show'
        external = attrs.get('imdb', '') if kind == 'movie' else attrs.get('tvdbid', attrs.get('tvdb', ''))
        external = str(external).removeprefix('tt')
        if not re.fullmatch(r'[0-9]{1,12}',external) or int(external)==0:
            external = ''
        title, year, image, genre, rating, plot = _search_details(attrs, release, kind)
        if not external:
            continue
        identity = kind + ':' + external
        quality = re.search(r'(?i)(2160|1080|720|480)[pi]', release)
        release_id = opaque_id(grab)
        if identity not in groups:
            groups[identity] = {
                'id': identity, 'kind': kind, 'externalId': external, 'title': title, 'image': image,
                'year': year, 'genre': genre, 'rating': rating, 'identified': True, 'provider': source, 'live': True,
                'description': plot or 'Live NZB search result. Compare releases before downloading.',
                'releases': [],
            }
        else:
            current = groups[identity]
            if image and not current.get('image'):
                current['image'] = image
            if plot and current.get('description', '').startswith('Live NZB'):
                current['description'] = plot
            if genre and not current.get('genre'):
                current['genre'] = genre
            if rating and not current.get('rating'):
                current['rating'] = rating
            if year and not current.get('year'):
                current['year'] = year
        public = {
            'id': release_id, 'title': release, 'quality': quality.group(0) if quality else 'Unspecified',
            'size': int(attrs.get('size', '0')) if str(attrs.get('size', '0')).isdigit() else 0,
            'published': row.findtext('pubDate') or '',
        }
        groups[identity]['releases'].append(public)
        secret.append({**public, 'grab': grab, 'source': 'nzb', 'kind': kind, 'externalId': external})
    response = next((element for element in root.iter() if element.tag.endswith('}response') or element.tag == 'response'), None)
    total = int(response.get('total')) if response is not None and str(response.get('total', '')).isdigit() else None
    return {
        'items': list(groups.values()), 'total': total,
        'nextOffset': next_offset(offset, len(rows), total),
        'scope': 'Live NZB releases • 100 per page • more results load on scroll',
        'releaseCount': len(rows), 'secret': secret,
    }


def _counted(value, prefix):
    # Newznab uses both 2026/20 and S2026/E20. Dates such as 07/20 and
    # multi-episode values are not a single episode number; never truncate them.
    match = re.fullmatch(prefix + r'?([0-9]{1,4})', str(value or '').strip(), re.I)
    return int(match.group(1)) if match else None


def episode_releases(data, tvdb, season, number):
    """Releases for one episode. NZB.life omits tvdbid, so a tvdb-scoped search stamps that id."""
    root = _rss_root(data)
    public, secret = [], []
    season, number = int(season), int(number)
    for row in root.findall('./channel/item')[:100]:
        attrs = _attrs(row)
        release = row.findtext('title') or ''
        grab = _grab(row)
        if not release or not grab:
            continue
        identity = str(attrs.get('tvdbid') or attrs.get('tvdb') or '')
        if identity.isdigit() and not same_external_id(identity, tvdb):
            continue
        row_season = _counted(attrs.get('season'), 'S')
        row_episode = _counted(attrs.get('episode'), 'E')
        if row_season is not None and row_episode is not None:
            if (row_season, row_episode) != (season, number):
                continue
        elif not release_covers(release, season, number):
            continue
        quality = re.search(r'(?i)(2160|1080|720|480)[pi]', release)
        size = attrs.get('size', '0')
        item = {
            'id': opaque_id(grab), 'title': release, 'quality': quality.group(0) if quality else 'Unspecified',
            'size': int(size) if str(size).isdigit() else 0, 'published': row.findtext('pubDate') or '',
            'externalId': str(tvdb), 'kind': 'show',
        }
        public.append(item)
        secret.append({**item, 'grab': grab, 'source': 'nzb'})
    return public, secret


def search_episode(app, store, tvdb, season, number):
    if not re.fullmatch(r'[0-9]{1,12}', str(tvdb or '')):
        raise DiscoverError('This show has no TVDB identity.')
    from .discover_search import query_all
    from xml.etree.ElementTree import tostring
    pages, warnings = query_all(indexers_for(app, store), {
        't': 'tvsearch', 'tvdbid': str(tvdb), 'season': int(season), 'ep': int(number),
        'cat': '5000', 'limit': 100, 'offset': 0, 'extended': 1,
    },store=store)
    public, secret, seen = [], [], set()
    for provider, root in pages:
        matches, candidates = episode_releases(tostring(root), tvdb, season, number)
        for release, saved in zip(matches, candidates):
            if release['id'] in seen:
                continue
            seen.add(release['id'])
            public.append({**release, 'provider': provider})
            secret.append(saved)
    if not public and warnings:
        raise _incomplete_search('Episode search is incomplete.',warnings)
    from .discover_store import remember_candidates
    remember_candidates(store, secret)
    return public


def search_movie(app, store, imdb):
    """Releases for one film. A title search only keeps whichever copies fit on the first page."""
    imdb = str(imdb or '').removeprefix('tt')
    if not re.fullmatch(r'[0-9]{5,12}', imdb):
        raise DiscoverError('This film has no IMDb identity.')
    from .discover_search import query_all
    from xml.etree.ElementTree import tostring
    pages, warnings = query_all(indexers_for(app, store), {'t': 'movie', 'imdbid': str(int(imdb)).zfill(7), 'cat': '2000', 'limit': 100, 'offset': 0, 'extended': 1},store=store)
    from .discover_store import remember_candidates
    wanted = imdb.lstrip('0') or '0'
    releases, seen = [], set()
    for provider, root in pages:
        parsed = parse_search(tostring(root), 'movies', 0, provider)
        remember_candidates(store, parsed.pop('secret'))
        for item in parsed.get('items') or []:
            found = str(item.get('externalId') or '').lstrip('0') or '0'
            if found != wanted:
                continue
            for release in item.get('releases') or []:
                if release['id'] in seen:
                    continue
                seen.add(release['id'])
                releases.append({**release, 'externalId': imdb, 'kind': 'movie', 'provider': provider})
    if not releases and warnings:
        raise _incomplete_search('Movie search is incomplete.',warnings)

    return releases


def _incomplete_search(message,warnings):
    error=DiscoverError(message+' '+' '.join(warnings))
    error.failures=getattr(warnings,'failures',())
    error.retry_at=getattr(warnings,'retry_at',0)
    error.limited_indexers=getattr(warnings,'limited_indexers',())
    return error


def movie_availability(app,store,imdb):
    """A small exact-identity probe; full release selection remains a live lookup."""
    imdb=str(imdb or '').removeprefix('tt')
    if not re.fullmatch(r'[0-9]{5,12}',imdb):raise DiscoverError('This film has no IMDb identity.')
    from .discover_search import query_all
    from xml.etree.ElementTree import tostring
    releases=[];failures=[];limited=[];retry_at=0;has_rows=False
    # One confirmed indexer is enough for a card. The popup still searches ALL.
    for indexer in indexers_for(app,store):
        try:
            pages,warnings=query_all([indexer],dict(t='movie',imdbid=str(int(imdb)).zfill(7),cat='2000',limit=5,offset=0,extended=1),timeout=5,response_limit=512_000,store=store)
        except DiscoverError as exc:
            failures.extend(getattr(exc,'failures',('provider_error',)))
            limited.extend(getattr(exc,'limited_indexers',()))
            retry_at=max(retry_at,getattr(exc,'retry_at',0));continue
        failures.extend(getattr(warnings,'failures',('provider_error',) if warnings else ()))
        limited.extend(getattr(warnings,'limited_indexers',()))
        retry_at=max(retry_at,getattr(warnings,'retry_at',0))
        for provider,root in pages:
            has_rows|=bool(root.findall('./channel/item'))
            parsed=parse_search(tostring(root),'movies',0,provider)
            for item in parsed['items']:
                if int(item['externalId'])==int(imdb):
                    releases.extend(dict(r,provider=provider,externalId=imdb,kind='movie') for r in item['releases'] if 'sample' not in r['title'].casefold())
        if releases:return releases
    # A sample or a row without an identity must not hide later valid releases.
    if has_rows:
        return [r for r in search_movie(app,store,imdb) if 'sample' not in r['title'].casefold()]
    if failures:
        error=DiscoverError('Movie availability search is incomplete.')
        error.failures=tuple(failures)
        error.retry_at=retry_at
        error.limited_indexers=tuple(dict.fromkeys(limited))
        raise error
    return releases


def television_search(store, data, offset, source):
    from .discover_search import television
    return television(store, [(source, _rss_root(data))], offset)


def search_nzb(app, store, provider, query, offset):
    from .discover_search import search
    return search(app, store, provider, query, offset)


def search_youtube(query, offset=0, dates=False):
    import json as json_module
    import subprocess
    import sys
    query = ' '.join(str(query or '').split())
    if not 2 <= len(query) <= 160:
        raise DiscoverError('Enter 2–160 characters to search.')
    if type(offset) is not int or offset < 0 or offset % 20:
        raise DiscoverError('Invalid search page.')
    command = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist', '--no-cache-dir',
               '--flat-playlist', *(['--extractor-args','youtubetab:approximate_date'] if dates else []),
               '--dump-single-json', '--skip-download', '--socket-timeout', '12',
               '--retries', '1', '--playlist-start', str(offset + 1),
               '--playlist-end', str(offset + 21), '--', 'ytsearchall:' + query]
    try:
        from .vpn.transport import subprocess_env
        result = subprocess.run(command, capture_output=True, text=True, timeout=45, env=subprocess_env('metadata'))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DiscoverError('YouTube search is temporarily unavailable.') from exc
    if result.returncode:
        raise DiscoverError('YouTube search is temporarily unavailable. Please retry.')
    try:
        data = json_module.loads(result.stdout or '{}')
    except json_module.JSONDecodeError as exc:
        raise DiscoverError('YouTube search is temporarily unavailable.') from exc
    from datetime import datetime, timezone
    items = []
    entries = data.get('entries') or []
    for entry in entries[:20]:
        if not entry:
            continue
        video_id = entry.get('id') or ''
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            continue
        published=''
        # Flat search dates are inferred from YouTube's relative publication text.
        # Keep that uncertainty explicit; never treat a scheduled stream as uploaded.
        if dates and entry.get('live_status') not in ('is_upcoming','is_live'):
            try:
                if entry.get('timestamp') is not None:
                    published=datetime.fromtimestamp(float(entry['timestamp']),timezone.utc).isoformat()
                elif re.fullmatch(r'\d{8}',str(entry.get('upload_date') or '')):
                    published=datetime.strptime(entry['upload_date'],'%Y%m%d').replace(tzinfo=timezone.utc).isoformat()
            except (ValueError,TypeError,OverflowError,OSError):pass
        items.append({
            'id': 'youtube:video:' + video_id, 'videoId': video_id,
            'url': 'https://www.youtube.com/watch?v=' + video_id, 'kind': 'video',
            'title': entry.get('title') or video_id,
            'channel': entry.get('channel') or entry.get('uploader') or 'YouTube',
            'channelId': entry.get('channel_id') if re.fullmatch(r'UC[A-Za-z0-9_-]{22}', entry.get('channel_id') or '') else '',
            'image': 'https://i.ytimg.com/vi/' + video_id + '/hqdefault.jpg',
            'published': published, 'publishedApproximate': bool(published),
            **({'durationSeconds':entry['duration']} if type(entry.get('duration')) in (int,float) and 0<entry['duration']<=31536000 else {}),
            'views': entry.get('view_count') if isinstance(entry.get('view_count'), int) else None,
            'provider': 'YouTube', 'public': True,
            'description': 'Public YouTube search result. Choose a quality to download this video only.',
        })
    return {'items': items, 'total': None, 'nextOffset': offset + 20 if len(entries) > 20 else None,
            'scope': 'Public YouTube search • more results load on scroll • no Google API key', 'releaseCount': None}


def text_summary(value):
    import html
    return html.unescape(re.sub(r'<[^>]+>', ' ', value or '')).strip()[:800]


def show_media(tvdb):
    """Poster and summary for one TVDB id. Empty when TVmaze has no single match."""
    if not re.fullmatch(r'[0-9]{1,12}', str(tvdb or '')):
        return {}
    with LOCK:
        saved = EPISODE_CACHE.get('poster:' + tvdb)
        if saved and time.time() - saved['fetchedAt'] < 3600:
            return saved
    try:
        show = json.loads(fetch('https://api.tvmaze.com/lookup/shows?thetvdb=' + tvdb, limit=1_000_000, timeout=8))
    except DiscoverError:
        result = {'image': '', 'summary': '', 'fetchedAt': time.time()}
        with LOCK:
            EPISODE_CACHE['poster:' + tvdb] = result
        return result
    image = show.get('image') if isinstance(show, dict) else None
    medium = image.get('medium') if isinstance(image, dict) else ''
    if not re.fullmatch(r'https://static\.tvmaze\.com/uploads/images/[A-Za-z0-9/_.-]+', medium or ''):
        medium = ''
    genres = [str(part) for part in (show.get('genres') or []) if isinstance(part, str)] if isinstance(show, dict) else []
    rating = show.get('rating') if isinstance(show, dict) else None
    score = ''
    if isinstance(rating, dict) and isinstance(rating.get('average'), (int, float)):
        score = f'{rating["average"]:.1f}'.rstrip('0').rstrip('.')
        if not re.fullmatch(r'\d{1,2}(?:\.\d)?', score):
            score = ''
    premiered = str(show.get('premiered') or '') if isinstance(show, dict) else ''
    result = {
        'image': medium,
        'summary': text_summary(show.get('summary') or '') if isinstance(show, dict) else '',
        'genre': ', '.join(genres[:3])[:80],
        'rating': score,
        'year': premiered[:4] if re.fullmatch(r'\d{4}-\d{2}-\d{2}', premiered) else '',
        'fetchedAt': time.time(),
    }
    with LOCK:
        EPISODE_CACHE['poster:' + tvdb] = result
    return result


def episodes(tvdb, store=None, tvmaze=None):
    if not re.fullmatch(r'[0-9]{1,12}', str(tvmaze or tvdb or '')):
        raise DiscoverError('Invalid show ID.')
    cache_key = (str(store.directory) if store else '', 'tvmaze:'+str(tvmaze) if tvmaze else tvdb)
    with LOCK:
        saved = EPISODE_CACHE.get(cache_key)
        if saved and time.time() - saved['fetchedAt'] < 3600:
            return saved
    try:
        from .discover_metadata import episode_show
        show = json.loads(fetch('https://api.tvmaze.com/shows/'+str(tvmaze),limit=1_000_000)) if tvmaze else episode_show(store, tvdb) if store is not None else json.loads(fetch('https://api.tvmaze.com/lookup/shows?thetvdb=' + tvdb, limit=1_000_000))
        show_id = int(show['id'])
        rows = json.loads(fetch(f'https://api.tvmaze.com/shows/{show_id}/episodes?specials=1', limit=4_000_000))
        seasons = json.loads(fetch(f'https://api.tvmaze.com/shows/{show_id}/seasons', limit=1_000_000))
    except (DiscoverError, ValueError, TypeError, KeyError) as exc:
        message = str(exc)
        if message.startswith(('No TVmaze', 'No unique')):
            raise DiscoverError(message) from exc
        if 'HTTP 404' in message:
            raise DiscoverError('No TVmaze show matches this TVDB ID.') from exc
        raise DiscoverError('TVmaze is temporarily unavailable.') from exc
    def image(value):
        medium = value.get('medium') if isinstance(value, dict) else ''
        return medium if medium and re.fullmatch(r'https://static\.tvmaze\.com/uploads/images/[A-Za-z0-9/_.-]+', medium) else ''
    result = {
        'externals':show.get('externals') or {}, 'premiered':show.get('premiered') or '', 'image':image(show.get('image')),
        'country': ((show.get('network') or {}).get('country') or (show.get('webChannel') or {}).get('country') or {}),
        'showId': show_id, 'name': show.get('name') or '', 'summary': text_summary(show.get('summary')),
        'url': show.get('url') or 'https://www.tvmaze.com/', 'provider': 'TVmaze', 'status': show.get('status') or '',
        'fetchedAt': int(time.time()),
        'seasons': [{'id': season.get('id'), 'number': season.get('number'), 'name': season.get('name') or '',
                     'episodeOrder': season.get('episodeOrder'), 'premiereDate': season.get('premiereDate')} for season in seasons if isinstance(season, dict)],
        'episodes': [{
            'id': episode.get('id'), 'name': episode.get('name') or 'Episode', 'season': episode.get('season'),
            'number': episode.get('number'), 'type': episode.get('type') or '', 'airdate': episode.get('airdate') or '',
            'runtime': episode.get('runtime'), 'summary': text_summary(episode.get('summary')),
            'image': image(episode.get('image')), 'url': episode.get('url') or '',
        } for episode in rows if isinstance(episode, dict) and episode.get('id') is not None],
    }
    with LOCK:
        EPISODE_CACHE[cache_key] = result
    return result


def subscription_uploads(store, limit=60):
    """Recent videos already stored for active YouTube subscriptions."""
    from .media_duration import fields as duration_fields
    import json
    import sqlite3
    channels, items = [], []
    try:
        with store.connect() as db:
            channels = [row[0] for row in db.execute('SELECT id FROM channels WHERE active=1')
                        if re.fullmatch(r'UC[A-Za-z0-9_-]{22}', row[0] or '')]
            if channels:
                rows = db.execute(
                    '''SELECT v.id, v.channel, v.published, v.data, c.data AS channel_data
                       FROM videos v JOIN channels c ON c.id=v.channel
                       WHERE c.active=1 ORDER BY v.published DESC LIMIT ?''',
                    (max(1, min(int(limit), 60)),)).fetchall()
            else:
                rows = []
    except sqlite3.OperationalError:
        return {'items': [], 'channels': []}
    for row in rows:
        try:
            video = json.loads(row['data'] or '{}')
            channel = json.loads(row['channel_data'] or '{}')
        except json.JSONDecodeError:
            continue
        video_id = row['id']
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id or ''):
            continue
        image = video.get('thumbnail') or f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg'
        items.append({
            'id': 'youtube:video:' + video_id, 'videoId': video_id, 'kind': 'video',
            'title': video.get('title') or 'Untitled', 'channel': channel.get('name') or '',
            'channelId': row['channel'], 'published': row['published'] or '',
            'image': image, 'url': 'https://www.youtube.com/watch?v=' + video_id,
            'provider': 'YouTube', 'public': True,
            'description': str(video.get('description') or 'Recent upload from a YouTube subscription.')[:400],
            **duration_fields(seconds=video.get('duration_seconds'),clock=video.get('duration'),pending=video.get('metadata_pending')),
        })
    return {'items': items, 'channels': channels}


def uploads(channel):
    if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', str(channel or '')):
        raise DiscoverError('Invalid channel ID.')
    with LOCK:
        saved = UPLOAD_CACHE.get(channel)
        if saved and time.time() - saved['fetchedAt'] < 900:
            return saved
    raw = fetch('https://www.youtube.com/feeds/videos.xml?channel_id=' + channel, limit=2_000_000)
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise DiscoverError('Unsupported feed.')
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise DiscoverError('Unsupported feed.') from exc
    ns = {'a': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015'}
    rows = []
    for entry in root.findall('a:entry', ns)[:15]:
        video_id = entry.findtext('yt:videoId', '', ns)
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            continue
        rows.append({
            'id': 'youtube:video:' + video_id, 'videoId': video_id, 'kind': 'video',
            'title': entry.findtext('a:title', '', ns), 'channel': entry.findtext('a:author/a:name', '', ns),
            'channelId': channel, 'published': entry.findtext('a:published', '', ns),
            'image': f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg',
            'url': 'https://www.youtube.com/watch?v=' + video_id, 'provider': 'YouTube', 'public': True,
            'description': 'Recent upload from this channel’s public YouTube feed.',
        })
    result = {'items': rows, 'fetchedAt': int(time.time())}
    with LOCK:
        UPLOAD_CACHE[channel] = result
    return result


def load_chart(store, name):
    initialise(store)
    if name not in CHARTS:
        raise DiscoverError('Unknown YouTube collection.')
    with store.connect() as db:
        row = db.execute('SELECT body,error FROM discover_charts WHERE name=?', (name,)).fetchone()
    if row:
        payload = json.loads(row['body'])
        if row['error']:
            payload['error'] = row['error']
            payload['stale'] = True
        return payload
    if not SEED.is_file():
        return {'items': [], 'title': name, 'scope': 'No public chart snapshot is available yet.', 'error': 'Chart snapshot missing.', 'itemsCount': 0}
    data = json.loads(SEED.read_text())
    payload = data.get(name) or {'items': [], 'title': name, 'scope': ''}
    payload = dict(payload)
    payload['seed'] = True
    with store.connect() as db:
        db.execute('INSERT OR IGNORE INTO discover_charts(name,body,fetched,error) VALUES (?,?,?,?)',
                   (name, json.dumps(payload), time.time(), ''))
    return payload


class _Table(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'tr':
            self.row = []
        if tag == 'td' and self.row is not None:
            self.cell = {'text': '', 'links': [], 'images': []}
        if self.cell is not None:
            if tag == 'a':
                self.cell['links'].append(attributes)
            if tag == 'img':
                self.cell['images'].append(attributes)

    def handle_data(self, data):
        if self.cell is not None:
            self.cell['text'] += data

    def handle_endtag(self, tag):
        if tag == 'td' and self.cell is not None:
            self.cell['text'] = ' '.join(self.cell['text'].split())
            self.row.append(self.cell)
            self.cell = None
        if tag == 'tr' and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None


def _number(text):
    return int(re.sub(r'[^0-9]', '', text or '') or '0')


def _video(video_id, title, rank, source):
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise DiscoverError('Invalid video identity')
    return {
        'id': 'youtube:video:' + video_id, 'videoId': video_id, 'kind': 'video', 'title': title, 'rank': rank,
        'provider': 'YouTube', 'public': True, 'image': f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg',
        'url': f'https://www.youtube.com/watch?v={video_id}', 'sourceUrl': source, 'year': '',
        'description': 'Public YouTube chart entry. Counts are a source snapshot, not live YouTube API data.',
    }


def _chart_all_time():
    all_url = 'https://us.youtubers.me/global/all/top-500-youtube-videos'
    parser = _Table()
    parser.feed(fetch(all_url, limit=12_000_000).decode('utf-8', 'replace'))
    videos, seen = [], set()
    for row in parser.rows:
        if len(row) < 7:
            continue
        links = [link for link in row[1]['links'] if link.get('class') == 'video-link']
        if not links:
            continue
        anchor = links[0]
        video_id = anchor.get('data-src', '')
        if video_id in seen:
            continue
        item = _video(video_id, anchor.get('data-title') or video_id, _number(row[0]['text']), all_url)
        item.update(views=_number(row[2]['text']), channel=anchor.get('data-channel-name', ''), genre=row[5]['text'], year=row[6]['text'])
        videos.append(item)
        seen.add(video_id)
        if len(videos) == 500:
            break
    if len(videos) != 500:
        raise DiscoverError('All-time chart did not provide 500 unique videos.')
    return {'items': videos, 'title': 'All-time most-viewed videos', 'scope': 'Top 500 in YouTubers.me’s tracked catalogue. Source update date is not supplied; counts may lag.', 'source': 'YouTubers.me', 'sourceUrl': all_url, 'updatedAt': None}


def _chart_popular():
    daily_url = 'https://kworb.net/youtube/'
    daily_html = fetch(daily_url, limit=12_000_000).decode('utf-8', 'replace')
    parser = _Table()
    parser.feed(daily_html)
    daily = []
    for row in parser.rows:
        if len(row) < 5 or not row[0]['text'].isdigit() or not row[2]['links']:
            continue
        match = re.search(r'(?:video/|[?&]v=)([A-Za-z0-9_-]{11})', row[2]['links'][0].get('href', ''))
        if not match:
            continue
        item = _video(match.group(1), row[2]['text'], _number(row[0]['text']), daily_url)
        item.update(dailyViews=_number(row[3]['text']), genre='Music',
                    description='Ranked by daily views in Kworb’s tracked music-video chart. This is not a platform-wide trending chart.')
        daily.append(item)
    if len(daily) < 500:
        raise DiscoverError('Daily chart did not provide 500 videos.')
    chart_date = re.search(r'20\d\d-\d\d-\d\d', daily_html)
    return {'items': daily[:500], 'title': 'Popular now · music videos', 'scope': 'Top 500 by daily views in Kworb’s music-video chart. Music only, not all YouTube categories.', 'source': 'Kworb', 'sourceUrl': daily_url, 'updatedAt': chart_date.group() if chart_date else None}


def _chart_channels(growth=False):
    channel_url = 'https://top.communitrics.com/top-500-most-subscribed-youtube-channels'
    page = fetch(channel_url, limit=4_000_000).decode('utf-8', 'replace')
    controls = re.search(r'component-url="(/_astro/ListControls\.[^"/]+\.js)"', page)
    if not controls:
        raise DiscoverError('Channel chart format changed.')
    script = fetch('https://top.communitrics.com' + controls.group(1), limit=2_000_000).decode('utf-8', 'replace')
    meta_path = re.search(r'\./(snapshot-meta\.[^"/]+\.js)', script)
    if not meta_path:
        raise DiscoverError('Channel chart format changed.')
    meta = fetch('https://top.communitrics.com/_astro/' + meta_path.group(1), limit=1_000_000).decode('utf-8', 'replace')
    snapshot = re.search(r'snapshotId:`([a-f0-9]+)', meta)
    if not snapshot:
        raise DiscoverError('Channel chart format changed.')
    dataset = json.loads(fetch('https://top.communitrics.com/generated/' + snapshot.group(1) + '/data/top-1000.json', limit=8_000_000))
    deltas = json.loads(fetch('https://top.communitrics.com/data/rank-deltas.json', limit=4_000_000)) if growth else {}
    growth_valid = dataset.get('generatedAt') == deltas.get('at')
    if growth and not growth_valid:
        raise DiscoverError('Channel growth snapshot mismatch.')
    channels = []
    for row in dataset.get('channels') or []:
        values = dict(zip(dataset.get('fields') or [], row))
        channel_id = values.get('id') or ''
        if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', channel_id):
            continue
        avatar = values.get('avatar') or ''
        if not str(avatar).startswith(('https://yt3.ggpht.com/', 'https://yt3.googleusercontent.com/')):
            avatar = ''
        movement = (deltas.get('deltas') or {}).get(channel_id, [0, 0])[0] if growth_valid else None
        channels.append({
            'id': 'youtube:channel:' + channel_id, 'channelId': channel_id, 'kind': 'channel', 'title': values.get('title') or channel_id,
            'rank': values.get('rank'), 'subscribers': values.get('sub_num'), 'views': values.get('view_num'),
            'growth': movement, 'growthDays': round(deltas.get('days') or 0, 1), 'country': values.get('country_code') or '',
            'provider': 'YouTube', 'public': True, 'image': avatar, 'url': 'https://www.youtube.com/channel/' + channel_id,
            'sourceUrl': channel_url,
            'description': 'Public channel statistics. Subscribe adds this channel to ReelDock and to YouTube when Google or cookies are connected.',
        })
    if len(channels) < 500:
        raise DiscoverError('Insufficient channel data.')
    rising = [dict(channel) for channel in channels]
    rising.sort(key=lambda channel: channel.get('growth') or 0, reverse=True)
    rising = rising[:500]
    for index, channel in enumerate(rising, 1):
        channel['rank'] = index
        channel['growthChart'] = True
    return {'items': rising, 'title': 'Channel momentum', 'scope': f"Subscriber-rank movement over {round(deltas.get('days') or 0, 1)} days, among the 1,000 most-subscribed tracked channels; unchanged ranks are included.", 'source': 'Communitrics', 'sourceUrl': channel_url, 'updatedAt': dataset.get('generatedAt')} if growth else {'items': channels[:500], 'title': 'Most-subscribed channels', 'scope': 'Top 500 by subscribers in Communitrics’ public catalogue.', 'source': 'Communitrics', 'sourceUrl': channel_url, 'updatedAt': dataset.get('generatedAt')}


def refresh_charts(store):
    """Refresh independent sources; preserve only the failed source's last good data."""
    from datetime import datetime, timezone
    initialise(store)
    loaders={'popular':_chart_popular, 'all-time':_chart_all_time,
             'channels':_chart_channels, 'rising':lambda:_chart_channels(growth=True)}
    results={};errors=[]
    for name,loader in loaders.items():
        if float(store.get('discover_chart_next_'+name,'0') or 0)>time.time():
            continue
        try:
            group=loader()
            group['fetchedAt']=datetime.now(timezone.utc).isoformat()
            save_chart(store,name,group)
            store.set('discover_chart_next_'+name,time.time()+REFRESH_INTERVAL)
            results[name]=len(group['items'])
        except Exception as exc:
            message=redact(exc) or 'Source refresh failed.'
            errors.append(name+': '+message)
            with store.connect() as db:
                db.execute('UPDATE discover_charts SET error=? WHERE name=?',(message,name))
            store.set('discover_chart_next_'+name,time.time()+BACKOFF)
    store.set('discover_youtube_error','; '.join(errors))
    store.set('discover_youtube_next', min(float(store.get('discover_chart_next_'+name,'0') or 0) for name in loaders))
    return results


def save_chart(store, name, group):
    initialise(store)
    with store.connect() as db:
        db.execute('''INSERT INTO discover_charts(name,body,fetched,error) VALUES (?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET body=excluded.body, fetched=excluded.fetched, error='' ''',
                   (name, json.dumps(group), time.time(), ''))


def maybe_refresh(store):
    initialise(store)
    due = float(store.get('discover_youtube_next', '0') or 0)
    if due and time.time() < due:
        return
    # Load the seed once so a failed first refresh does not replace it with an empty file.
    for name in CHARTS:
        load_chart(store, name)
    try:
        refresh_charts(store)
    except Exception as exc:
        message = redact(exc)
        store.set('discover_youtube_error', message or 'The public chart refresh failed. Showing the last good snapshot.')
        store.set('discover_youtube_next', time.time() + BACKOFF)
