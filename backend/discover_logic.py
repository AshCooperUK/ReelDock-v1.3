"""Pure Discover matching rules. No network access and no credentials."""
import hashlib
import re
from urllib.parse import quote

QUALITIES = ('720', '1080', '2160')
FEED_NAMES = {
    'trending_movies': 'movie',
    'new_movies': 'movie',
    'geek_rated_movies': 'movie',
    'endorsed_movies': 'movie',
    'trending_shows': 'show',
    'geek_rated_tv': 'show',
}
FEED_LIMIT = 1000
SEARCH_PAGE = 100
CHARTS = ('all-time', 'popular', 'channels', 'rising')
ACTIVE_REQUESTS = ('searching', 'queued', 'downloading', 'verifying', 'waiting')
TERMINAL_REQUESTS = ('complete', 'failed', 'cancelled')


def parse_height(label):
    match = re.search(r'(?<![0-9])(2160|1080|720|480)(?![0-9])', str(label or ''))
    return int(match.group(1)) if match else None


def acceptable(label, wanted, allow_lower):
    """Unknown quality never satisfies a request. Higher quality is not a substitute."""
    height = parse_height(label)
    if height is None or str(wanted) not in QUALITIES:
        return False
    target = int(wanted)
    if height == target:
        return True
    return bool(allow_lower) and height < target


def rank_releases(releases, wanted, allow_lower):
    target = int(wanted)
    chosen = [item for item in releases if acceptable(item.get('quality'), wanted, allow_lower)]

    def key(item):
        height = parse_height(item.get('quality')) or 0
        return (0 if height == target else 1, target - height, -(int(item.get('size') or 0)))

    return sorted(chosen, key=key)


def boundary_season(episodes, today):
    """Next season after the highest numbered season with an aired episode.

    Specials (season 0) do not move the boundary. An announced season that has
    not aired is included, because the boundary is the next season number rather
    than a later season that is absent from metadata. Nothing aired means season 1.
    """
    aired = []
    for episode in episodes or []:
        season = int(episode.get('season') or 0)
        airdate = str(episode.get('airdate') or '')
        if season > 0 and airdate and airdate <= today:
            aired.append(season)
    return (max(aired) + 1) if aired else 1


def episode_identity(show_id, season, number):
    return f'show:{show_id}:{int(season)}:{int(number)}'


def movie_identity(external_id):
    return f'movie:{external_id}'


def same_external_id(left, right):
    """Numeric catalogue identifiers may differ only in zero padding."""
    left,right=str(left or ''),str(right or '')
    return left==right or bool(re.fullmatch(r'[0-9]{1,12}',left) and
                              re.fullmatch(r'[0-9]{1,12}',right) and int(left)==int(right))


def release_pairs(title):
    # Catalogue seasons include years (Panorama S2026); keep the full number.
    # Four digits also matches the existing catalogue and library naming range.
    return [(int(season), int(episode)) for season, episode in re.findall(
        r'(?i)(?:^|[^A-Z0-9])S([0-9]{1,4})E([0-9]{1,4})(?=[^0-9]|$)', title or '')]


def single_episode(title, season, number):
    """Single-file requests must not silently acquire a combined episode release."""
    return release_pairs(title)==[(season,number)] and not re.search(
        r'(?i)S[0-9]{1,4}E[0-9]{1,4}(?:[ ._-]*E[0-9]|[ _]*-[ _]*[0-9])',title or '')


def season_packs(title):
    if release_pairs(title):
        return []
    found = [int(value) for value in re.findall(r'(?i)(?:^|[^A-Z0-9])S([0-9]{1,4})(?![E0-9])', title or '')]
    found += [int(value) for value in re.findall(r'(?i)(?:^|[^A-Z0-9])season\s*([0-9]{1,4})(?![0-9]|\s*E)', title or '')]
    return found


def release_covers(title, season, number):
    """Match a release to an episode by season/episode codes, never by title alone."""
    if number is None:
        return False
    season = int(season)
    number = int(number)
    pairs = release_pairs(title)
    if pairs:
        return (season, number) in pairs
    return season in season_packs(title)


def covered_episode_ids(title, episodes):
    pairs = release_pairs(title)
    if pairs:
        wanted = set(pairs)
        return [episode['id'] for episode in episodes
                if episode.get('number') is not None and (int(episode['season']), int(episode['number'])) in wanted]
    packs = set(season_packs(title))
    if not packs:
        return []
    return [episode['id'] for episode in episodes
            if int(episode.get('season') or 0) in packs and episode.get('number') is not None]


def classify_episodes(episodes, rule, today):
    """Split a saved rule into episodes to search and episodes still waiting to air.

    An air date is not treated as a downloadable release. Episodes with a future
    air date wait. Episodes with no air date are searched. Specials are manual.
    """
    due, waiting = [], []
    scope = rule.get('scope') or 'future'
    start = int(rule.get('season') or 1)
    for episode in episodes or []:
        season = int(episode.get('season') or 0)
        if season <= 0 or episode.get('number') is None:
            continue
        if scope == 'one':
            if season != start:
                continue
        elif season < start:
            continue
        airdate = str(episode.get('airdate') or '')
        if airdate and airdate > today:
            waiting.append(episode)
        else:
            due.append(episode)
    return due, waiting


def next_offset(offset, row_count, total, page=SEARCH_PAGE):
    if type(offset) is not int or offset < 0 or offset % page:
        raise ValueError('Invalid search page.')
    more = row_count > 0 and (row_count == page if total is None else offset + row_count < int(total))
    return offset + page if more else None


def opaque_id(secret):
    return 'rel:' + hashlib.sha256(str(secret).encode()).hexdigest()[:32]


def redact(text):
    cleaned = re.sub(r'(?i)(apikey|api_key|r)=([^&\s]+)', r'\1=redacted', str(text or ''))
    cleaned = re.sub(r'https?://\S+', '[provider URL]', cleaned)
    return cleaned[:500]


def shared_failure(message):
    lowered = redact(message).lower()
    markers = (
        'not configured', 'could not be reached', 'api allowance', 'rate limit', 'too many',
        'free space', 'nearly full', 'usenet server', 'api key', 'credential', 'timed out',
        'temporarily unavailable', 'directory',
    )
    return any(marker in lowered for marker in markers)


def content_disposition(filename, attachment=False):
    fallback = re.sub(r'[^A-Za-z0-9 ._()-]', '_', filename) or 'download'
    kind = 'attachment' if attachment else 'inline'
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"
