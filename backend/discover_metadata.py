"""TVmaze metadata with a constrained fallback for missing external-ID links."""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from . import discover_sources as sources


def title_match(payload, tvdb, title, year):
    """A missing external ID is acceptable; a conflicting one never is."""
    if not isinstance(payload, list) or not title or not re.fullmatch(r'\d{4}', year or ''):
        return None
    matches = {}
    for row in payload:
        show = row.get('show') if isinstance(row, dict) else None
        if not isinstance(show, dict) or type(show.get('id')) is not int or show['id'] <= 0:
            continue
        if sources._norm_title(show.get('name')) != sources._norm_title(title): continue
        if str(show.get('premiered') or '')[:4] != year: continue
        matches[show['id']] = show
    if len(matches) != 1: return None
    show = next(iter(matches.values()))
    external = str((show.get('externals') or {}).get('thetvdb') or '')
    return show if not external or external == str(tvdb) else None


def resolve(tvdb, title='', year=''):
    try:
        show = json.loads(sources.fetch('https://api.tvmaze.com/lookup/shows?thetvdb=' + tvdb,
                                       limit=1_000_000, timeout=4))
    except sources.DiscoverError as exc:
        if 'HTTP 404' not in str(exc): raise
        if not title or not re.fullmatch(r'\d{4}', year or ''):
            raise sources.DiscoverError('No TVmaze show matches this TVDB ID.') from exc
        payload = json.loads(sources.fetch('https://api.tvmaze.com/search/shows?q=' + quote(title),
                                          limit=1_000_000, timeout=4))
        show = title_match(payload, tvdb, title, year)
        if not show:
            raise sources.DiscoverError('No unique TVmaze title and year match is available.') from exc
    if not isinstance(show, dict) or type(show.get('id')) is not int or show['id'] <= 0:
        raise sources.DiscoverError('TVmaze returned incomplete show information.')
    external = str((show.get('externals') or {}).get('thetvdb') or '')
    if external and external != str(tvdb):
        raise sources.DiscoverError('TVmaze returned a conflicting show identity.')
    return show


def save(store, tvdb, show, title='', year=''):
    card = {**sources._show_card(show), 'tvmazeId': show['id'], 'matchedAt': time.time(),
            'identityTitle': title, 'identityYear': year}
    sources._remember_show(store, tvdb, card)
    return card


def enrich(store, groups, known):
    cached = sources._load_shows(store, list(groups))
    pending = []
    for tvdb, group in groups.items():
        card = cached.get(tvdb, {})
        if card.get('tvmazeId') and time.time() - card.get('matchedAt', 0) < 86400:
            group.update({k: card[k] for k in ('title','image','year','releaseDate','description','genre','rating') if card.get(k)})
            continue
        # Hints come only from releases with a provider-supplied ID, not browser input.
        rows = [r for values in known.values() for r in values if r['identity'] == tvdb]
        titles = {sources._norm_title(r['title']) for r in rows}
        years = {r['year'] for r in rows if r['year']}
        title = rows[0]['title'] if len(titles) == 1 else ''
        year = next(iter(years)) if len(years) == 1 else ''
        if title and year:
            # Retain the safe hints so an episode lookup can retry a metadata outage.
            sources._remember_show(store, tvdb, {**card, 'identityTitle': title, 'identityYear': year})
        if len(pending) < 8: pending.append((tvdb, title, year))
    def load(args):
        tvdb, title, year = args
        try: return args, resolve(tvdb, title, year)
        except (sources.DiscoverError, ValueError, TypeError, KeyError): return args, None
    if pending:
        with ThreadPoolExecutor(max_workers=min(4, len(pending))) as pool:
            for (tvdb, title, year), show in pool.map(load, pending):
                if show:
                    card = save(store, tvdb, show, title, year)
                    groups[tvdb].update({k: card[k] for k in ('title','image','year','releaseDate','description','genre','rating') if card.get(k)})


def episode_show(store, tvdb):
    card = sources._load_shows(store, [tvdb]).get(tvdb, {})
    title, year = card.get('identityTitle', ''), card.get('identityYear', '')
    maze_id = card.get('tvmazeId')
    if type(maze_id) is int and maze_id > 0 and time.time() - card.get('matchedAt', 0) < 86400:
        show = json.loads(sources.fetch(f'https://api.tvmaze.com/shows/{maze_id}', limit=1_000_000, timeout=4))
        if not isinstance(show, dict):
            raise sources.DiscoverError('TVmaze returned incomplete show information.')
        external = str((show.get('externals') or {}).get('thetvdb') or '')
        if external == tvdb or title_match([{'show': show}], tvdb, title, year):
            return show
        # Metadata links changed: do not keep using the cached association.
    show = resolve(tvdb, title, year)
    save(store, tvdb, show, title, year)
    return show
