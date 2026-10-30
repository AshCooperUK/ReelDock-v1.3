"""Playlist metadata classification and media-server filenames."""
import re
from datetime import date
from urllib.parse import urlsplit

QUALITY = re.compile(r'(?i)(?:\d{3,4}[pi]|4k|8k|web(?:[ .-]?dl|rip)?|blu[ .-]?ray|bd(?:rip|remux)|dvd(?:rip)?|hdtv|[xh][ .]?26[45]|hevc|av1|hdr10?\+?|dv|dolby[ .-]?vision|\d{1,2}bit|remux|aac|flac|dts|ddp|ac3|ts|tc|cam|imax|nf|amzn|dsnp|avi|mp4|mkv)')
EPISODE = re.compile(r'(?i)\b(?:S(\d{1,4})[ ._-]*E(\d{1,4})((?:[ ._-]*E\d{1,4})*)|(\d{1,4})x(\d{1,4}))\b')
DATED = re.compile(r'\b((?:19|20)\d{2})[ .-](\d{2})[ .-](\d{2})\b')
YEAR = re.compile(r'(?:\s+|\s*\()((?:19|20)\d{2})\)?\s*$')
GENERIC_GROUP = re.compile(r'(?i)(?:tv|series|shows?|episodes?|vod)(?:[ _-]+(?:vod|shows?|series))?$')


def series_name(title_name, group):
    """Prefer a playlist group only when it is the fuller series title.

    Xtream and many M3U sources put a genre such as Drama in group-title, while
    the episode title still has the real show name. Use the group when the
    extracted name is a shortened form of it (Jimmy Fallon → The Tonight Show
    Starring Jimmy Fallon), not when it is an unrelated category.
    """
    name = (title_name or '').strip()
    group = tidy(group) if group else ''
    if not name:
        return group or 'Unknown show'
    if group and not GENERIC_GROUP.fullmatch(group) and name.casefold() != group.casefold():
        if re.search(r'(?i)(?<![A-Za-z0-9])' + re.escape(name) + r'(?![A-Za-z0-9])', group):
            return group
    return name or 'Unknown show'


def tidy(title):
    """Drop recognised release tags; preserve real bracketed title text."""
    title = re.sub(r'\[([^\]]*)\]', lambda m: '' if QUALITY.fullmatch(m[1].strip()) else m[0], title)
    # Bare release details follow a resolution/codec marker, not ordinary words
    # such as Web, Cam, or IT that can be part of a programme's title.
    title = re.sub(r'(?i)\s+(?:\d{3,4}[pi]|[xh][ .]?26[45]|HEVC|WEB[.-]DL|WEBRip|HDTV|BluRay)\b.*$', '', title)
    title = re.sub(r'\[\s*\]|\(\s*\)', '', title)
    return re.sub(r'\s+', ' ', title).strip(' ,.-_')


def provider_id(attrs, options):
    for provider in ('imdb', 'tmdb', 'tvdb'):
        value = attrs.get(provider+'-id', '').strip()
        if re.fullmatch(r'tt\d{1,12}', value) if provider == 'imdb' else re.fullmatch(r'\d{1,12}', value):
            return provider, value
    value = attrs.get('tvg-id', '').strip()
    if re.fullmatch(r'tt\d{1,12}', value): return 'imdb', value
    match = re.fullmatch(r'(?i)(imdb|tmdb|tvdb)[.:=-](tt\d{1,12}|\d{1,12})', value)
    if match:
        provider, value = match[1].lower(), match[2]
        if (provider == 'imdb') == value.startswith('tt'): return provider, value
    if re.fullmatch(r'\d{1,12}', value) and options.get('numeric_ids') in ('auto', 'tmdb', 'tvdb'):
        return options['numeric_ids'], value
    return '', ''


def describe(title, duration, group, url, options, attrs=None):
    attrs = attrs or {}
    for term in options['scrub']:
        if term in (',', r'\,'): continue
        found = title.casefold().find(term.casefold())
        if found >= 0: title = title[found+len(term):].strip()
    for before, after in options['replace'].items(): title = title.replace(before, after)
    title = tidy(title)
    episode = EPISODE.search(title); dated = DATED.search(title)
    declared = attrs.get('tvg-type', '').casefold().strip()
    group_kind = group.casefold().strip()
    url_path = urlsplit(url).path
    movie = declared in ('movie', 'film', 'vod') or (not declared and (re.search(r'\b(movies?|films?)\b', group_kind) or '/movie/' in url_path))
    live = declared in ('live', 'livetv', 'live tv', 'channel')
    series = declared in ('series', 'tv', 'episode') or (not declared and (re.search(r'\b(series|tv vod|tv shows?)\b', group_kind) or '/series/' in url_path))
    year_match = YEAR.search(title)
    year = int(year_match[1]) if year_match and title[:year_match.start()].strip() else None
    kind = ('live' if live else 'movies' if movie and declared else 'series' if episode else 'tv' if dated
            else 'unsorted' if series else 'movies' if movie or year else 'live' if duration == '-1' and not re.search(r'(?i)vod', group) else 'unsorted')
    if kind in options['cleaners']:
        from .m3u import clean_terms
        title = tidy(clean_terms(title, options['remove']))
        return describe(title, duration, group, url, dict(options, cleaners=[], scrub=[], replace={}), attrs)
    provider, identity = provider_id(attrs, options)
    info = dict(kind=kind, name=title, year=year, provider=provider, identity=identity, day='', season=None, episode=None, extra='', matched=False, note='')
    if kind == 'movies':
        if year: info['name'] = title[:year_match.start()].strip(' ._-(')
        if not year: info['note'] = 'Movie year missing; metadata match needed.'
    elif kind in ('series', 'tv'):
        marker = episode if kind == 'series' else dated
        name = title[:marker.start()].strip(' ,.-_') if marker else title
        info['name'] = series_name(name, group if series else '')
        if kind == 'series' and episode:
            info.update(season=int(episode[1] or episode[4]), episode=int(episode[2] or episode[5]))
            info['extra'] = ''.join('E'+str(int(n)).zfill(2) for n in re.findall(r'(?i)E(\d+)', episode[3] or ''))
        elif dated:
            day = '-'.join(dated.groups())
            try: date.fromisoformat(day)
            except ValueError: info.update(kind='unsorted', note='Invalid air date; check this title.')
            else:
                info.update(day=day, note='Episode number missing; keeping the air date.')
                from .m3u import clean_show
                match = next((m for m in options.get('episode_maps', []) if clean_show(m['show']).casefold() == clean_show(info['name']).casefold() and m['date'] == day), None)
                if match: info.update(kind='series', season=match['season'], episode=match['episode'], matched=True, note='Confirmed episode match.')
    elif kind == 'unsorted': info['note'] = 'No reliable episode number, date or media type.'
    return info


def match_key(info):
    # IDs are scoped to media type: a TMDB movie and show can share a number.
    if not info['identity']: return ''
    if info['provider'] == 'auto':
        return '|'.join(('auto', 'movie' if info['kind'] == 'movies' else 'series', info['identity'], info['name'].casefold(), str(info.get('year') or ''), info['day']))
    return '|'.join(('movie' if info['kind'] == 'movies' else 'series', info['provider'], info['identity'], info['day']))


def apply_match(info, metadata):
    result = dict(info)
    cached = (metadata or {}).get(match_key(info), {})
    if info['provider'] == 'auto' and cached.get('provider') == 'tmdb' and re.fullmatch(r'\d{1,12}', str(cached.get('identity', ''))):
        result.update(provider='tmdb', identity=str(cached['identity']), matched=True, note='Numeric ID verified against title and year.')
    if info['kind'] == 'movies' and not info['year'] and cached.get('year'):
        result.update(year=cached['year'], matched=True, note='Movie year matched by provider ID.')
    elif info['kind'] == 'tv' and cached.get('episode') is not None and cached.get('season') is not None:
        result.update(kind='series', season=cached['season'], episode=cached['episode'], matched=True, note='Episode matched by provider ID and air date.')
    return result


def paths(info, naming):
    from .m3u import filename
    kind = info['kind']; name = filename(info['name'].replace('.', ' ') if '.' in info['name'] and ' ' not in info['name'] else info['name'])
    tag = ''
    if info['provider'] in ('imdb', 'tmdb', 'tvdb') and info['identity']:
        provider, identity = info['provider'], info['identity']
        tag = f' {{{provider}-{identity}}}' if naming == 'plex' else f' [{provider}id{"=" if naming == "emby" else "-"}{identity}]'
    if kind == 'movies':
        title = name + (f" ({info['year']})" if info['year'] else '')
        folder = filename(title+tag)
        return title, kind, f'Movie_VOD/{folder}/{folder}.strm'
    if kind in ('series', 'tv'):
        folder = filename(name+tag)
        if kind == 'series':
            title = f"{name} - S{info['season']:02d}E{info['episode']:02d}{info['extra']}"
            return title, kind, f"TV_VOD/{folder}/Season {info['season']:02d}/{filename(title)}.strm"
        title = f"{name} - {info['day']}"
        return title, kind, f'TV_VOD/{folder}/{filename(title)}.strm'
    return name, kind, f'Unsorted_VOD/{name}/{name}.strm' if kind == 'unsorted' else ''
