"""Federated release search. Provider identities take precedence over metadata."""
import json
import re
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode
from . import discover_sources as sources
from .discover_logic import next_offset, opaque_id
from .discover_store import remember_candidates


class IndexerWarnings(list):
    pass


def query_all(indexers, params, *, timeout=12, response_limit=8_000_000, store=None):
    from .downloaders.nzb.indexers import api_base
    def query(indexer):
        name = sources._provider_name(indexer)
        try:
            from .discover_indexer_rate import request_slot
            with request_slot(store,indexer) if store is not None else nullcontext():
                data = sources.fetch(api_base(str(indexer['url'])) + '?' + urlencode(
                    {**params, 'apikey': indexer['api_key']}), limit=response_limit, timeout=timeout)
                return name, sources._rss_root(data), None
        except Exception as exc:
            # Provider exceptions can contain access-bearing URLs. Never expose them.
            from .discover_indexer_rate import limit_message
            reason=sources.failure_reason(exc)
            limited=(name,) if 'http_429' in reason.split(',') else ()
            message=limit_message(limited)+' Checks will resume automatically.' if limited else name+' could not complete this search.'
            return name, None, (message,reason,getattr(exc,'retry_at',0),limited)
    with ThreadPoolExecutor(max_workers=min(4, len(indexers))) as pool:
        results = list(pool.map(query, indexers))
    good = [(name, root) for name, root, error in results if root is not None]
    warnings=IndexerWarnings(error[0] for _,_,error in results if error)
    warnings.failures=tuple(error[1] for _,_,error in results if error)
    warnings.retry_at=max((error[2] for _,_,error in results if error),default=0)
    warnings.limited_indexers=tuple(dict.fromkeys(name for _,_,error in results if error for name in error[3]))
    if not good:
        error=sources.DiscoverError(' '.join(warnings))
        error.failures=warnings.failures
        error.retry_at=warnings.retry_at
        error.limited_indexers=warnings.limited_indexers
        raise error
    return good, warnings


def page_info(pages, offset):
    count, totals, more = 0, [], False
    for _, root in pages:
        n = len(root.findall('./channel/item')[:100]); count += n
        response = next((r for r in root.iter() if r.tag.rsplit('}', 1)[-1] == 'response'), None)
        total = int(response.get('total')) if response is not None and str(response.get('total', '')).isdigit() else None
        totals.append(total)
        more |= next_offset(offset, n, total) is not None
    return dict(releaseCount=count, total=sum(totals) if all(n is not None for n in totals) else None,
                nextOffset=offset + 100 if more else None,
                scope='Live NZB releases • up to 100 per indexer per page • more results load on scroll')


def television(store, pages, offset):
    records = []
    known = defaultdict(list)
    for provider, root in pages:
        for row in root.findall('./channel/item')[:100]:
            attrs = sources._attrs(row); release = row.findtext('title') or ''; grab = sources._grab(row)
            if not release or not grab: continue
            name = sources._show_name(release)
            year_match = re.search(r'\s+((?:19|20)\d{2})$', name)
            year = year_match.group(1) if year_match else ''
            title = name[:year_match.start()].strip() if year_match else name
            key = sources._norm_title(title)
            if not key: continue
            identity = str(attrs.get('tvdbid') or attrs.get('tvdb') or '')
            if not re.fullmatch(r'[1-9][0-9]{0,11}', identity): identity = ''
            quality = re.search(r'(?i)(2160|1080|720|480)[pi]', release)
            public = dict(id=opaque_id(grab), title=release, provider=provider,
                          quality=quality.group(0) if quality else 'Unspecified',
                          size=int(attrs['size']) if str(attrs.get('size', '')).isdigit() else 0,
                          published=row.findtext('pubDate') or '')
            record = dict(key=key, title=title, year=year, identity=identity, public=public, grab=grab, attrs=attrs)
            records.append(record)
            if identity: known[key].append(record)
    # Exact title and compatible year only; never cross a conflicting provider ID.
    for row in records:
        if row['identity']: continue
        peers = known[row['key']]
        ids = {p['identity'] for p in peers if not row['year'] or not p['year'] or row['year'] == p['year']}
        if len(ids) == 1: row['identity'] = ids.pop()
    fallback = {}
    for row in records:
        if not row['identity'] and not known[row['key']]:
            fallback[row['key']] = dict(candidates=[row['title']])
    try:
        resolved = sources._resolve_shows(store, list(fallback.values())[:40]) if fallback else {}
    except (sources.DiscoverError, ValueError, TypeError):
        resolved = {}
    fallback_cards = sources._load_shows(store, list(resolved.values()))
    groups, secret, unidentified = {}, [], {}
    cards = sources._load_shows(store, [r['identity'] for r in records if r['identity']])
    for row in records:
        identity = row['identity'] or (resolved.get(row['key'], '') if row['key'] in fallback else '')
        public = row['public']
        if not row['identity'] and identity:
            card = fallback_cards.get(identity, {})
            if sources._norm_title(card.get('title')) != row['key'] or (row['year'] and card.get('year') != row['year']):
                identity = ''
        if not identity:
            unidentified[public['id']] = public
            continue
        if identity not in groups:
            attrs = row['attrs']; card = cards.get(identity, fallback_cards.get(identity, {}))
            groups[identity] = dict(id='show:' + identity, kind='show', externalId=identity,
                title=card.get('title') or row['title'], year=card.get('year') or row['year'],
                image=card.get('image') or sources._cover(attrs.get('coverurl')),
                genre=card.get('genre', ''), rating=card.get('rating', ''),
                description=card.get('description') or 'Available NZB releases. Episode information is loaded separately.',
                provider='', live=True, releases=[])
        group = groups[identity]
        if public['id'] not in {r['id'] for r in group['releases']}:
            group['releases'].append({**public, 'externalId': identity})
            secret.append({**public, 'grab': row['grab'], 'source': 'nzb', 'kind': 'show', 'externalId': identity})
    from .discover_metadata import enrich
    enrich(store, groups, known)
    for group in groups.values():
        group['provider'] = ', '.join(dict.fromkeys(r['provider'] for r in group['releases']))
    return dict(items=list(groups.values()), secret=secret, unidentified=list(unidentified.values()), **page_info(pages, offset))


def search(app, store, provider, query, offset, *, browse=False):
    query = ' '.join(str(query or '').split())
    if provider not in ('movies', 'shows') or (not (browse and not query) and not 2 <= len(query) <= 160):
        raise sources.DiscoverError('Enter 2–160 characters to search.')
    indexers = sources.indexers_for(app, store)
    # Invalidate when accounts/order change; keep credentials out of cache keys.
    account = opaque_id(json.dumps(indexers, sort_keys=True))
    key = (str(store.directory), account, provider, query.casefold(), offset)
    with sources.LOCK:
        cached = sources.SEARCH_CACHE.get(key)
        if cached and time.time() - cached[0] < 300: return cached[1]
    if not sources.SEARCH_SLOTS.acquire(blocking=False):
        raise sources.DiscoverError('Search is busy. Please try again shortly.')
    try:
        # Movie keyword searches must match release names too. Some indexers'
        # specialised movie endpoint returns no matches for these terms even
        # when its category search has identified releases. ID lookups use the
        # separate movie acquisition path and retain the specialised endpoint.
        # Empty specialised queries can also return rejected/old catalogues.
        params=dict(t='search' if provider=='movies' or (browse and not query) else 'tvsearch',
                    cat='2000' if provider=='movies' else '5000',limit=100,offset=offset,extended=1)
        if query:params['q']=query
        pages, warnings = query_all(indexers,params,store=store)
        if provider == 'shows': result = television(store, pages, offset)
        else:
            import xml.etree.ElementTree as ET
            groups, secret, unknown = {}, [], []
            for name, root in pages:
                parsed = sources.parse_search(ET.tostring(root), provider, offset, name)
                secret.extend(parsed['secret'])
                for item in parsed['items']:
                    for release in item['releases']: release['provider'] = name
                    if item['id'] in groups: groups[item['id']]['releases'].extend(item['releases'])
                    else: groups[item['id']] = item
                for row in root.findall('./channel/item')[:100]:
                    attrs = sources._attrs(row); identity = str(attrs.get('imdb', '')).removeprefix('tt')
                    if not identity.isdigit() and sources._grab(row):
                        unknown.append(dict(id=opaque_id(sources._grab(row)), title=row.findtext('title') or '', provider=name))
            result = dict(items=list(groups.values()), secret=secret, unidentified=unknown, **page_info(pages, offset))
        remember_candidates(store, result.pop('secret'))
        if provider=='movies':
            from .movie_options import observe
            observe(store,result['items'],'NZB')
        result['warnings'] = warnings
        result['fetchedAt'] = datetime.now(timezone.utc).isoformat()
        with sources.LOCK:
            if len(sources.SEARCH_CACHE) >= 40: sources.SEARCH_CACHE.pop(next(iter(sources.SEARCH_CACHE)))
            if not warnings: sources.SEARCH_CACHE[key] = (time.time(), result)
        return result
    finally:
        sources.SEARCH_SLOTS.release()
