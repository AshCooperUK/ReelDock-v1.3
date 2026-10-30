"""Native playlist subscriptions and owned .strm exports; no external parser service."""
import hashlib
import errno
import http.client
import json
import os
import re
import socket
import ssl
import time
import threading

SIDECAR_LOCK = threading.RLock()
import unicodedata
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit, urljoin

from .network import public_addresses
from .media_duration import fields as duration_fields

from .settings.m3u import DEFAULTS, NAMING
# Bounds checks of existing generated sidecars, not playlist imports.
MAX_BYTES = 128 * 1024 * 1024


def initialise(store):
    with store.connect() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS m3u_sources (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL, options TEXT NOT NULL,
                folder TEXT NOT NULL UNIQUE, state TEXT NOT NULL DEFAULT 'idle',
                message TEXT NOT NULL DEFAULT 'Ready for first import.', next_run REAL NOT NULL DEFAULT 0,
                last_run REAL NOT NULL DEFAULT 0, stats TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS m3u_files (
                source TEXT NOT NULL, path TEXT NOT NULL, title TEXT NOT NULL, kind TEXT NOT NULL,
                group_name TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(source,path));
            CREATE INDEX IF NOT EXISTS m3u_file_title ON m3u_files(source,title COLLATE NOCASE,path);
            CREATE INDEX IF NOT EXISTS m3u_file_kind_title ON m3u_files(source,kind,title COLLATE NOCASE,path);
            CREATE TABLE IF NOT EXISTS m3u_file_info (source TEXT, path TEXT, data TEXT NOT NULL, PRIMARY KEY(source,path));
            CREATE TABLE IF NOT EXISTS m3u_file_aliases (source TEXT, expected TEXT, path TEXT NOT NULL, PRIMARY KEY(source,expected));
            CREATE TABLE IF NOT EXISTS m3u_removals (source TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS m3u_runs (
                id INTEGER PRIMARY KEY, source TEXT NOT NULL, finished REAL NOT NULL,
                state TEXT NOT NULL, message TEXT NOT NULL, stats TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS m3u_dry_runs (
                source TEXT PRIMARY KEY, state TEXT NOT NULL, phase TEXT NOT NULL,
                message TEXT NOT NULL, report TEXT NOT NULL DEFAULT '{}', updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS m3u_metadata_runs (
                source TEXT PRIMARY KEY, state TEXT NOT NULL, message TEXT NOT NULL,
                report TEXT NOT NULL DEFAULT '{}', updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS m3u_matches (
                source TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(source,key));
            CREATE TABLE IF NOT EXISTS m3u_tmdb_cache (
                key TEXT PRIMARY KEY, data TEXT NOT NULL, updated REAL NOT NULL);
        ''')

    from .m3u_catalogue import initialise as initialise_catalogue
    initialise_catalogue(store)
    from .xtream import initialise as xtream_init
    xtream_init(store)
    from .m3u_downloads import initialise as downloads_init
    from .m3u_sidecars import initialise as sidecars_init
    downloads_init(store)
    sidecars_init(store)


def validate_url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment or not 0 < (parsed.port or 80) < 65536
                or len(value) > 4096 or any(ord(c) < 33 for c in value)):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Enter a complete HTTP or HTTPS playlist URL.') from None
    return value


def options_from(payload):
    """Compatibility entry point for the shared M3U/Xtream settings module."""
    from .settings.m3u import source_options
    return source_options(payload)


def save(store, payload):
    name = str(payload.get('name', '')).strip()
    if not name or len(name) > 100:
        raise ValueError('Give this subscription a name (up to 100 characters).')
    options = options_from(payload)
    source_id = str(payload.get('id') or uuid.uuid4().hex)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT * FROM m3u_sources WHERE id=?', (source_id,)).fetchone()
        if payload.get('id') and not old:
            raise ValueError('Subscription not found.')
        checking = db.execute("SELECT 1 FROM m3u_dry_runs WHERE source=? AND state IN ('queued','running') UNION ALL SELECT 1 FROM m3u_metadata_runs WHERE source=? AND state IN ('queued','running')", (source_id,source_id)).fetchone()
        if old and (old['state'] == 'running' or checking):
            raise ValueError('Wait for the current import to finish before editing.')
        # Changing paths must not trigger a scheduled rewrite before review.
        naming_keys = ('naming', 'episode_maps', 'scrub', 'remove', 'replace', 'cleaners', 'numeric_ids')
        changed_naming = old and any(json.loads(old['options']).get(k, DEFAULTS[k]) != options[k] for k in naming_keys)
        if changed_naming:
            options['enabled'] = False
            options['hours'] = 0
        if old and old['url'].startswith('xtream:'):
            url = old['url']  # Xtream logins are edited in the Xtream section.
        else:
            url = validate_url(str(payload.get('url', '')).strip() or (old['url'] if old else ''))
        # One source can own the existing root; additional subscriptions get their own folder.
        from .storage_layout import source_folder, fixed
        if fixed(store) and payload.get('existing_library'): raise ValueError('Compose manages the stream library folders.')
        folder = old['folder'] if old else ('' if payload.get('existing_library') is True else source_folder(store,db,name,source_id))
        if not old and db.execute('SELECT 1 FROM m3u_sources WHERE folder=?', (folder,)).fetchone():
            raise ValueError('The existing library already belongs to another subscription.')
        if not old and db.execute('SELECT COUNT(*) FROM m3u_sources').fetchone()[0] >= 50:
            raise ValueError('Up to 50 subscriptions are supported.')
        db.execute('''INSERT INTO m3u_sources(id,name,url,options,folder,next_run) VALUES(?,?,?,?,?,?)
                      ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=excluded.url,
                      options=excluded.options,next_run=excluded.next_run''',
                   (source_id, name, url, json.dumps(options), folder, time.time() + options['hours'] * 3600))
        db.execute('DELETE FROM m3u_dry_runs WHERE source=?', (source_id,))
        if changed_naming:
            db.execute("UPDATE m3u_sources SET state='idle',message='Naming rules changed. Automatic refresh paused; review a dry run before importing.' WHERE id=?", (source_id,))
    return source_id


def subscriptions(store, root):
    with store.connect() as db:
        rows = db.execute('SELECT * FROM m3u_sources ORDER BY name COLLATE NOCASE').fetchall()
    result = []
    for row in rows:
        item = dict(row)
        url = item.pop('url')
        item['kind'] = 'xtream' if url.startswith('xtream:') else 'm3u'
        if item['kind'] == 'xtream':
            from .xtream import account
            saved = account(store, item['id'])
            item['host'] = urlsplit(saved['server']).hostname if saved else ''
        else:
            item['host'] = urlsplit(url).hostname
        item['options'] = json.loads(item['options'])
        if not item['options'].get('enabled', True):
            item['options']['hours'] = 0
        item['stats'] = json.loads(item['stats'])
        from .storage_layout import fixed
        item['output'] = ('; '.join(str(Path(root)/name/item['folder']) for name in ('VOD Movies','VOD TV Shows'))
                          if fixed(store) else str(Path(root) / item['folder']))
        result.append(item)
    return result


def request_sync(store, source_id):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM m3u_metadata_runs WHERE source=? AND state IN ('queued','running')", (source_id,)).fetchone():
            raise ValueError('Wait for metadata matching to finish before importing.')
        if db.execute("SELECT 1 FROM m3u_dry_runs WHERE source=? AND state IN ('queued','running')", (source_id,)).fetchone():
            raise ValueError('Wait for the dry run to finish before importing.')
        changed = db.execute("UPDATE m3u_sources SET state='queued',message='Waiting for the background worker.',next_run=0 WHERE id=? AND state!='running'", (source_id,)).rowcount
        if not changed:
            raise ValueError('This subscription is already importing or no longer exists.')


def fetch_playlist(url, check_cancel=lambda: None):
    """Fetch without a total size/time cap; retain idle timeouts and cancellation."""
    for _ in range(6):
        parsed = urlsplit(validate_url(url))
        addresses = public_addresses(parsed.hostname)
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=30)
        try:
            raw = socket.create_connection((addresses[0], port), timeout=30)
            connection.sock = raw
            if parsed.scheme == 'https':
                connection.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname)
            connection.request('GET', (parsed.path or '/') + ('?' + parsed.query if parsed.query else ''),
                               headers={'Host': parsed.netloc, 'User-Agent': 'ReelDock', 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                url = urljoin(url, response.getheader('Location', ''))
                continue
            if response.status != 200:
                raise ValueError('Playlist provider returned HTTP ' + str(response.status) + '. Check the subscription address and expiry.')
            content = bytearray()
            while True:
                check_cancel()
                chunk = response.read(65536)
                if not chunk:
                    break
                content.extend(chunk)
            return content.decode('utf-8-sig')
        except (OSError, http.client.HTTPException, UnicodeError):
            raise ValueError('Could not read the playlist. Check the address, provider and connection.') from None
        finally:
            connection.close()
    raise ValueError('The playlist redirected too many times.')


def filename(value):
    from .plain_names import plain_text
    value = plain_text(value)
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', value).strip(' .-')
    value = re.sub(r'\s+', ' ', value) or 'Untitled'
    if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM\d|LPT\d)(\..*)?', value):
        value = '_' + value
    return value.encode('utf-8')[:140].decode('utf-8', 'ignore')


def clean_show(value):
    """Remove empty tag remnants, not meaningful bracketed title text."""
    value = re.sub(r'[\[\]{}]', '', value)
    return filename(value.replace('.', ' ').strip(' ,_()-'))


def clean_terms(title, terms):
    # Consume the whole bracketed tag, instead of leaving its opening bracket.
    for term in terms:
        token = re.escape(term)
        title = re.sub(r'(?i)\[\s*' + token + r'[^\]]*\]', '', title)
        title = re.sub(r'(?i)(?<!\w)' + token + r'[^\s\[\]]*', '', title)
    return re.sub(r'\[\s*\]|\(\s*\)', '', title).strip()


def title_path(title, duration, group, url, options):
    if options.get('naming', 'legacy') != 'legacy':
        from .m3u_naming import describe, paths
        return paths(describe(title, duration, group, url, options), options['naming'])
    for term in options['scrub']:
        # Display names are already separated from the group by the M3U comma.
        term = ',' if term == r'\,' else term
        if term != ',':
            found = title.lower().find(term.lower())
            if found >= 0:
                title = title[found + len(term):].strip()
    for before, after in options['replace'].items():
        title = title.replace(before, after)
    season = re.search(r'(?i)\b(?:S(\d{1,3})\s*E(\d{1,3})|(\d{1,3})x(\d{1,3}))\b', title)
    dated = re.search(r'\b((?:19|20)\d{2})[ .-](\d{2})[ .-](\d{2})\b', title)
    years = list(re.finditer(r'\b((?:19|20)\d{2})\b', title))
    vod = bool(re.search(r'(?i)movie|vod|series', group) or re.search(r'/(movie|series)/', url))
    kind = 'series' if season else 'tv' if dated else 'movies' if years else 'live' if duration == '-1' and not vod else 'unsorted'
    if kind in options['cleaners']:
        if options.get('naming', 'legacy') == 'legacy':
            for term in options['remove']:
                title = re.sub(re.escape(term) + r'\S*', '', title).strip()
        else:
            title = clean_terms(title, options['remove'])
        # Reclassify after removal without applying text replacements twice.
        return title_path(title, duration, group, url, dict(options, cleaners=[], scrub=[], replace={}))
    modern = options.get('naming', 'legacy') != 'legacy'
    if modern and kind == 'tv':
        show = clean_show(title[:dated.start()])
        day = '-'.join(dated.groups())
        try: date.fromisoformat(day)
        except ValueError:
            title = filename(title)
            return title, 'unsorted', f'Unsorted_VOD/{title}/{title}.strm'
        match = next((m for m in options.get('episode_maps', [])
                      if clean_show(m['show']).casefold() == show.casefold() and m['date'] == day), None)
        if match:
            number = match['season']; episode = match['episode']
            title = f'{show} - S{number:02d}E{episode:02d}'
            return title, 'series', f'TV_VOD/{show}/Season {number:02d}/{title}.strm'
        title = f'{show} - {day}'
        return title, 'tv', f'TV_VOD/{show}/{title}.strm'
    if kind == 'series':
        show = clean_show(title[:season.start()]) if modern else filename(title[:season.start()])
        number = int(season[1] or season[3]); episode = int(season[2] or season[4])
        title = f'{show}{" -" if modern else ""} S{number:02d}E{episode:02d}'
        path = f'TV_VOD/{show}/Season {number:02d}/{title}.strm'
    elif kind == 'tv':
        show = filename(title[:dated.start()])
        title = filename(title)
        path = f'TV_VOD/{show}/{title}.strm'
    elif kind == 'movies':
        year = years[-1]
        name = title[:year.start()].rstrip(' ([-')
        title = (clean_show(name) if modern else filename(name)) + f' ({year[1]})'
        path = f'Movie_VOD/{title}/{title}.strm'
    else:
        title = filename(title)
        path = f'Unsorted_VOD/{title}/{title}.strm' if kind == 'unsorted' else ''
    return title, kind, path


def preview_naming(payload):
    options = options_from(payload)
    titles = payload.get('titles', [])
    if not isinstance(titles, list) or not 1 <= len(titles) <= 20 or any(not isinstance(t, str) or not t.strip() or len(t) > 1000 for t in titles):
        raise ValueError('Enter 1–20 example titles, one per line (up to 1,000 characters each).')
    return [dict(original=t, title=result[0], kind=result[1], path=result[2])
            for t in titles for result in [title_path(t, '0', 'VOD', '', options)]]


def parse(text, options, metadata=None):
    if not text.lstrip().startswith('#EXTM3U'):
        raise ValueError('The provider returned a page rather than an extended M3U playlist.')
    entries = []; pending = None; invalid = 0; excluded = 0; seen = set(); paths = {}
    for line in text.splitlines():
        line = line.strip()
        if len(line) > 32768:
            raise ValueError('A playlist entry is too long.')
        if line.startswith('#EXTINF:'):
            if pending is not None:
                invalid += 1
            # The first comma outside quoted attributes starts the display name.
            quoted = False; split = None
            for index, char in enumerate(line):
                if char == '"': quoted = not quoted
                elif char == ',' and not quoted:
                    split = index; break
            if split is None:
                pending = None; invalid += 1; continue
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', line[:split]))
            pending = (line, attrs, line[split+1:].strip(), line[8:].split(' ', 1)[0].split(',', 1)[0], [])
        elif pending and line.startswith(('#EXTGRP:', '#EXTVLCOPT:', '#KODIPROP:')):
            pending[4].append(line)
        elif line and not line.startswith('#'):
            if not pending:
                invalid += 1; continue
            header, attrs, title, duration, extra = pending; pending = None
            try:
                validate_url(line)
            except ValueError:
                invalid += 1; continue
            group = attrs.get('group-title', '')
            if any(term.casefold() in group.casefold() for term in options['exclude']):
                excluded += 1; continue
            title = title or attrs.get('tvg-name') or group
            if not title:
                invalid += 1; continue
            original = title
            info = {}
            if options.get('naming', 'legacy') != 'legacy':
                from .m3u_naming import describe, apply_match, paths as naming_paths
                info = apply_match(describe(title, duration, group, line, options, attrs), metadata)
                title, kind, path = naming_paths(info, options['naming'])
            else:
                title, kind, path = title_path(title, duration, group, line, options)
            if (kind == 'live' and not options['live_tv']) or (kind == 'unsorted' and not options['unsorted']):
                excluded += 1; continue
            # A repeated stream under another title is still the same file.
            key = (path.casefold(), line) if kind == 'live' else ('strm', line)
            if key in seen: continue
            seen.add(key)
            if path:
                folded = path.casefold()
                if folded in paths:
                    path = str(Path(path).with_stem(Path(path).stem + ' [' + hashlib.sha256(line.encode()).hexdigest()[:10] + ']')).replace('\\', '/')
                paths[path.casefold()] = True
            entries.append(dict(title=title, original=original, kind=kind, path=path, group=group, info=info, **duration_fields(seconds=duration), attrs={k:v for k,v in attrs.items() if k in ('tvg-id','tvg-type','tvg-name','tvg-logo','tmdb-id','imdb-id','tvdb-id','x-source','x-plot','x-cast','x-genre','x-rating','x-director','release-date','release_date','releasedate','premiered','first-air-date','air-date','air_date','date')},
                                content='\n'.join([header, *extra, line]) if kind == 'live' else line + '\n'))
            if len(entries) > 250000:
                raise ValueError('Playlist has more than 250,000 entries.')
    if pending: invalid += 1
    if invalid:
        raise ValueError(f'Playlist contains {invalid} incomplete or invalid entries. Existing files were kept; check the source and retry.')
    if not entries:
        raise ValueError('No usable entries remain after filtering. Existing files were kept.')
    return entries, excluded


def contained(root, relative):
    root = Path(root).absolute()
    path = root / relative
    if path.is_absolute() and (Path(relative).is_absolute() or '..' in Path(relative).parts):
        raise ValueError('Unsafe library path; import stopped.')
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise ValueError('Library paths must not contain symbolic links.')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Unsafe library path; import stopped.')
    return path


def digest_file(path):
    if not path.exists(): return ''
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError('An existing output file could not be safely checked.')
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def export(store, root, source, entries, excluded, dry_run=False, progress=None):
    try:
        with SIDECAR_LOCK:
            return _export(store, root, source, entries, excluded, dry_run, progress)
    except OSError as exc:
        reason = {
            errno.EACCES: 'Permission denied. Give the ReelDock service user access to this library folder.',
            errno.EPERM: 'Permission denied. Check folder permissions and access rules.',
            errno.ENOSPC: 'The library disk is full.',
            errno.EDQUOT: 'The library storage quota has been reached.',
            errno.EROFS: 'The library is mounted read-only.',
            errno.ENAMETOOLONG: 'The output filename or path is too long.',
        }.get(exc.errno, 'A library file could not be read or written.')
        location = ''
        if exc.filename:
            try:
                path = Path(exc.filename)
                if path.name.startswith('.reeldock-'): path = path.parent
                location = str(path.absolute().relative_to(Path(root).absolute()))
            except (TypeError, ValueError):
                pass
        if location: reason += f' Affected path: {location}.'
        reason += ' Import did not finish; review permissions or storage, then retry.'
        raise ValueError(reason) from None


def _export(store, root, source, entries, excluded, dry_run=False, progress=None):
    options = json.loads(source['options'])
    from .storage_layout import source_base
    base = source_base(store, root, source['folder'])
    stats = dict(movies=0, series=0, tv=0, live=0, unsorted=0, added=0, updated=0, removed=0, adopted=0, conflicts=0, excluded=excluded)
    stats["changes"] = {}
    def changed(kind, action):
        counts = stats["changes"].setdefault(kind, dict(added=0, updated=0, removed=0))
        counts[action] += 1
    outputs = []
    live = []
    for item in entries:
        stats[item['kind']] += 1
        if item['kind'] == 'live': live.append(item['content'])
        else:
            from .storage_layout import export_path
            outputs.append({**item,'path':export_path(store,item['path'],source['folder'])})
    if options['live_tv']:
        from .storage_layout import export_path
        outputs.append(dict(path=export_path(store,'Live_TV/livetv.m3u',source['folder']), title='Live TV playlist', kind='playlist', group='',
                            content='#EXTM3U\n' + '\n'.join(live) + '\n'))
    with store.connect() as db:
        previous = {r['path']: dict(r) for r in db.execute('SELECT * FROM m3u_files WHERE source=?', (source['id'],))}
        aliases = {r['expected']:r['path'] for r in db.execute('SELECT expected,path FROM m3u_file_aliases WHERE source=?', (source['id'],))}
        excluded_folders = [r['folder'] for r in db.execute('SELECT folder FROM m3u_sources WHERE id!=?', (source['id'],)) if r['folder']]
    from .storage_layout import names as folder_names
    from .m3u_existing import scan, stream_key, compatible
    # Sources in dedicated folders cannot inspect or adopt another source's files.
    from .storage_layout import fixed, source_streams
    stream_files = source_streams(store, root, source['folder']) if fixed(store) else scan(base, excluded_folders if not source['folder'] else ())
    aliases_to_save = []; resolved_outputs = []; used_streams = set(); assigned_paths = set()
    previous_folded = {path.casefold():path for path in previous}
    previous_plain = {}
    for path in previous:
        previous_plain.setdefault('/'.join(filename(part) for part in Path(path).parts), []).append(path)
    for original in outputs:
        item = dict(original)
        expected = item['path']
        # Retain the old spelling on disk when only text cleaning changed it.
        preserved = [path for path in previous_plain.get(expected, []) if path not in assigned_paths]
        if len(preserved) == 1 and contained(base, preserved[0]).is_file():
            item['path'] = preserved[0]
        if item['kind'] != 'playlist':
            key = stream_key(item['content'])
            if key in used_streams: continue
            if key: used_streams.add(key)
            candidates = [path for path in stream_files.get(key, []) if compatible(path, expected, folder_names(store)['series']) and path not in assigned_paths]
            mapped = item['path'] if item['path'] != expected else aliases.get(expected)
            if mapped in previous and mapped not in assigned_paths and contained(base, mapped).is_file():
                item['path'] = mapped
            elif candidates:
                # Keep a previously adopted location stable, then prefer the requested
                # name, then an existing original file. Never create another copy.
                item['path'] = min(candidates, key=lambda path:(path not in previous, path != expected, len(path), path.casefold(), path))
            elif expected.casefold() in previous_folded:
                item['path'] = previous_folded[expected.casefold()]
        if item['path'] in assigned_paths:
            raise ValueError('Two playlist entries resolve to the same existing file. Import stopped before writing; review naming rules.')
        assigned_paths.add(item['path'])
        aliases_to_save.append((source['id'], expected, item['path']))
        resolved_outputs.append(item)
    outputs = resolved_outputs
    keep = set(); records = []; samples = []; sample_groups = set()
    def sample(item, action):
        key = (item['kind'], action)
        if len(samples) < 12 and (key not in sample_groups or len(samples) < 4):
            samples.append(dict(title=item['title'], original=item.get('original', ''), kind=item['kind'], path=item['path'], action=action, note=item.get('info', {}).get('note', '')))
            sample_groups.add(key)
    for count, item in enumerate(outputs):
        relative = item['path']; keep.add(relative)
        path = contained(base, relative); data = item['content'].encode('utf-8')
        digest = hashlib.sha256(data).hexdigest(); old = previous.get(relative)
        existing = digest_file(path)
        # Earlier exporters omit the trailing newline; this is the same stream.
        if existing and item['kind'] != 'playlist' and path.stat().st_size < 32768:
            if path.read_text(encoding='utf-8-sig').strip() == item['content'].strip():
                digest = existing
        if existing and existing != digest and (not old or old['digest'] != existing):
            # Take over matching files, but never overwrite untracked or externally changed ones.
            stats['conflicts'] += 1
            sample(item, 'Keep conflict')
            continue
        if existing != digest:
            if not dry_run:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name('.reeldock-' + uuid.uuid4().hex + '.tmp')
                try:
                    with temporary.open('xb') as file: file.write(data)
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            stats['updated' if existing else 'added'] += 1
            changed(item['kind'], 'updated' if existing else 'added')
            sample(item, 'Update' if existing else 'Add')
        elif not old:
            stats['adopted'] += 1
            sample(item, 'Adopt existing')
        else:
            stats['unchanged'] = stats.get('unchanged', 0) + 1
            sample(item, 'Unchanged')
        records.append((source['id'], relative, item['title'], item['kind'], item['group'], digest))
        if count % 500 == 0:
            if dry_run:
                if progress: progress(count + 1, len(outputs))
            else:
                with store.connect() as db:
                    db.execute("UPDATE m3u_sources SET message=? WHERE id=?", (f'Writing library: {count + 1:,} of {len(outputs):,} files.', source['id']))
    # All downloads and parsing succeeded before any obsolete owned file is removed.
    if not dry_run:
        with store.connect() as db:
            db.executemany('INSERT OR REPLACE INTO m3u_files VALUES(?,?,?,?,?,?)', records)
            record_paths = {r[1] for r in records}
            from .m3u_catalogue import normal_date,DATE_ATTRIBUTES
            for item in outputs:
                release=next((normal_date(item.get('attrs',{}).get(k)) for k in DATE_ATTRIBUTES if normal_date(item.get('attrs',{}).get(k))), '')
                if item['path'] in record_paths:db.execute('UPDATE m3u_catalog_entries SET release_date=? WHERE source=? AND path=?',(release,source['id'],item['path']))
            db.executemany('INSERT OR REPLACE INTO m3u_file_info VALUES(?,?,?)', [(source['id'], item['path'], json.dumps(dict(info=item.get('info', {}), attrs=item.get('attrs', {}), **duration_fields(seconds=item.get('durationSeconds'))))) for item in outputs if item['path'] in record_paths])
            db.execute('DELETE FROM m3u_catalog_entries WHERE source=? AND path NOT IN (SELECT path FROM m3u_files WHERE source=?)',(source['id'],source['id']))
            db.executemany('INSERT OR REPLACE INTO m3u_file_aliases VALUES(?,?,?)', [r for r in aliases_to_save if r[2] in record_paths])
    stale = set(previous) - keep
    stats['cleanup_held'] = False
    if options['clean_sync']:
        removed = []
        for relative in stale:
            path = contained(base, relative)
            existing = digest_file(path)
            if existing and existing != previous[relative]['digest']:
                stats['conflicts'] += 1; continue
            sample(previous[relative], 'Remove')
            changed(previous[relative]['kind'], 'removed')
            if dry_run:
                stats['removed'] += 1
                continue
            path.unlink(missing_ok=True); removed.append((source['id'], relative)); stats['removed'] += 1
            parent = path.parent
            while parent != base and parent.is_relative_to(base) and not (getattr(store, 'media_root', '') and parent.parent == base):
                try: parent.rmdir()
                except OSError: break
                parent = parent.parent
        if not dry_run:
            with store.connect() as db:
                db.executemany('DELETE FROM m3u_files WHERE source=? AND path=?', removed)
                db.executemany('DELETE FROM m3u_file_info WHERE source=? AND path=?', removed)
                db.executemany('DELETE FROM m3u_file_aliases WHERE source=? AND path=?', removed)
            from .m3u_sidecars import cleanup_removed
            cleanup_removed(store,base,[relative for _,relative in removed])
            with store.connect() as db:
                db.executemany("DELETE FROM m3u_exported WHERE source=? AND path=? AND target='strm'",removed)
    if dry_run:
        stats['samples'] = samples
        stats['output_files'] = len(outputs)
        stats['entries'] = len(entries)
        stats['metadata_matched'] = sum(bool(e.get('info', {}).get('matched')) for e in entries)
        stats['metadata_unresolved'] = sum(bool(e.get('info', {}).get('note')) and not e.get('info', {}).get('matched') for e in entries)
    return stats


def request_dry_run(store, source_id):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM m3u_metadata_runs WHERE source=? AND state IN ('queued','running')", (source_id,)).fetchone():
            raise ValueError('Wait for metadata matching to finish before a dry run.')
        source = db.execute('SELECT state FROM m3u_sources WHERE id=?', (source_id,)).fetchone()
        current = db.execute('SELECT state FROM m3u_dry_runs WHERE source=?', (source_id,)).fetchone()
        if not source or source['state'] in ('queued', 'running') or (current and current['state'] in ('queued', 'running')):
            raise ValueError('Wait for the current import or dry run to finish.')
        db.execute("INSERT OR REPLACE INTO m3u_dry_runs VALUES(?,'queued','queued','Waiting for the background worker.','{}',?)", (source_id, time.time()))


def dry_tick(store, root, fetch):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT s.* FROM m3u_sources s JOIN m3u_dry_runs d ON d.source=s.id WHERE d.state='queued' AND s.state NOT IN ('running','queued') ORDER BY d.updated LIMIT 1").fetchone()
        if not row: return False
        source = dict(row)
        db.execute("UPDATE m3u_dry_runs SET state='running',phase='download',message='Downloading the playlist for inspection.',updated=? WHERE source=?", (time.time(), source['id']))
    report = {}; started = time.monotonic(); steps = []
    def update(phase, message, **extra):
        report.update(extra); report['steps'] = steps
        with store.connect() as db:
            db.execute('UPDATE m3u_dry_runs SET phase=?,message=?,report=?,updated=? WHERE source=?', (phase, message, json.dumps(report), time.time(), source['id']))
    try:
        text = fetch(source['url'])
        steps.append('Playlist downloaded')
        update('parse', 'Reading titles, groups and stream types.', playlist_bytes=len(text.encode('utf-8')))
        from .m3u_metadata import matches
        entries, excluded = parse(text, json.loads(source['options']), matches(store, source['id']))
        steps.append('Titles classified and naming rules applied')
        update('compare', 'Comparing proposed files with the current library.', entries=len(entries), excluded=excluded)
        stats = export(store, root, source, entries, excluded, dry_run=True,
                       progress=lambda done, total: update('compare', f'Checking files: {done:,} of {total:,}.', checked=done, total=total))
        steps.extend(['Existing files compared', 'Cleanup rules checked', 'Preview ready — no library files changed'])
        update('complete', 'Dry run complete. No media files were downloaded, exported or removed.', **stats, elapsed_seconds=round(time.monotonic()-started, 1))
        state = 'complete'
    except Exception as exc:
        state = 'error'
        update('error', str(exc) if isinstance(exc, ValueError) else 'Dry run interrupted. Check the playlist and library permissions. No library files were changed.')
    with store.connect() as db:
        db.execute('UPDATE m3u_dry_runs SET state=? WHERE source=?', (state, source['id']))
    return True


def fetch_source(store, url):
    """Playlist text for an M3U URL, or one generated from an Xtream login."""
    if url.startswith('xtream:'):
        from .xtream import playlist
        return playlist(store, url.removeprefix('xtream:'))
    return fetch_playlist(url, check_cancel=lambda: store.get('setup_complete'))


def tick(store, root, fetch=None):
    fetch = fetch or (lambda url: fetch_source(store, url))
    from .m3u_removal import tick as removal_tick
    if removal_tick(store, root): return
    from .m3u_metadata import tick as metadata_tick, matches
    if metadata_tick(store, fetch): return
    if dry_tick(store, root, fetch): return
    now = time.time()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        source = db.execute("SELECT * FROM m3u_sources WHERE state!='running' AND id NOT IN (SELECT source FROM m3u_dry_runs WHERE state IN ('queued','running')) AND id NOT IN (SELECT source FROM m3u_metadata_runs WHERE state IN ('queued','running')) AND (state='queued' OR (json_extract(options,'$.enabled')=1 AND json_extract(options,'$.hours')>0 AND next_run<=?)) ORDER BY next_run LIMIT 1", (now,)).fetchone()
        if not source: return
        source = dict(source)
        db.execute("UPDATE m3u_sources SET state='running',message='Fetching playlist…' WHERE id=?", (source['id'],))
    stats = {}; state = 'complete'
    try:
        entries, excluded = parse(fetch(source['url']), json.loads(source['options']), matches(store, source['id']))
        stats = export(store, root, source, entries, excluded)
        message = f"Import complete. {stats['added']:,} added, {stats['updated']:,} updated, {stats['removed']:,} removed."
        if stats['adopted']: message += f" {stats['adopted']:,} existing files matched."
        if stats['conflicts']: message += f" {stats['conflicts']:,} changed or unowned files kept."
        if json.loads(source['options'])['refresh_emby'] and store.get('emby_url'):
            from .emby import queue_refresh
            queue_refresh(store, 'm3u-' + source['id'])
    except ValueError as exc:
        state = 'error'; message = str(exc)
    except Exception:
        state = 'error'; message = 'Import interrupted. Check library permissions and free space, then retry. Cleanup was not completed.'
    with store.connect() as db:
        db.execute('UPDATE m3u_sources SET state=?,message=?,stats=?,last_run=?,next_run=? WHERE id=?',
                   (state, message, json.dumps(stats), now, time.time() + json.loads(source['options'])['hours'] * 3600, source['id']))
        db.execute('INSERT INTO m3u_runs(source,finished,state,message,stats) VALUES(?,?,?,?,?)', (source['id'], time.time(), state, message, json.dumps(stats)))
    if state == 'complete':
        # Catalogue the imported files now rather than on the next browse request.
        from .m3u_catalogue import index
        try: index(store)
        except Exception: pass  # the catalogue loop retries every minute



def loop(store, root, stop):
    with store.connect() as db:
        db.execute("UPDATE m3u_sources SET state='queued',message='Resuming interrupted import.' WHERE state='running' AND id NOT IN (SELECT source FROM m3u_removals WHERE state IN ('queued','running'))")
        db.execute("UPDATE m3u_dry_runs SET state='queued',phase='queued',message='Resuming interrupted dry run.' WHERE state='running'")
        db.execute("UPDATE m3u_metadata_runs SET state='queued',message='Resuming interrupted metadata matching.' WHERE state='running'")
    while not stop.is_set():
        paused = True
        with store.background_operation():
            if store.reset_pending():
                paused = True
            else:
                paused = False
                try:
                    if store.get('setup_complete'):
                        from .storage_layout import path
                        tick(store, path(store,'m3u',root))
                except Exception:
                    # Never log exception bodies that may contain credential-bearing URLs.
                    pass
        if paused:
            stop.wait(1)
            continue
        stop.wait(10)
