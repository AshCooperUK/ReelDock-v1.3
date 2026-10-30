"""Find existing stream links before choosing an export filename."""
import hashlib
import os
import re
from pathlib import Path


def compatible(existing, proposed, tv_folder='TV_VOD'):
    """Do not adopt a reused provider URL that now names a different title."""
    from .m3u_naming import tidy, YEAR
    def name(path):
        parts = Path(path).parts
        value = parts[1] if len(parts)>2 and parts[0] in ('TV_VOD',tv_folder) else Path(path).stem
        value = re.sub(r'(?i)[\[{](?:imdb|tmdb|tvdb)(?:id)?[=-](?:tt)?\d+[\]}]|\s*\[[a-f0-9]{10}\]$', '', value)
        from .plain_names import plain_text
        value = tidy(plain_text(value)) or 'Untitled'
        year = YEAR.search(value)
        return (''.join(c for c in (value[:year.start()] if year else value).casefold() if c.isalnum()), year[1] if year else '')
    left, ly = name(existing); right, ry = name(proposed)
    return left == right and (not ly or not ry or ly == ry)


def stream_key(content):
    from .m3u import validate_url
    try:
        value = content.decode('utf-8-sig').strip() if isinstance(content, bytes) else content.strip()
        validate_url(value)
    except (UnicodeError, ValueError):
        return None
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def scan(base, excluded=()):
    """One bounded read per STRM, excluding symlinks and other subscriptions."""
    by_stream = {}
    excluded = set(excluded)
    if not base.exists(): return by_stream
    def failed(_):
        raise ValueError('Could not check the existing library for duplicates. Check folder read access before retrying.')
    for directory, folders, files in os.walk(base, followlinks=False, onerror=failed):
        folders[:] = sorted(name for name in folders if not (Path(directory)/name).is_symlink()
                            and str((Path(directory)/name).relative_to(base)).replace('\\', '/') not in excluded)
        for name in sorted(files):
            path = Path(directory)/name
            if path.suffix.lower() != '.strm' or path.is_symlink(): continue
            try:
                with path.open('rb') as file: raw = file.read(32769)
            except OSError:
                failed(None)
            if len(raw)>32768: continue
            key = stream_key(raw)
            if key:
                by_stream.setdefault(key, []).append(path.relative_to(base).as_posix())
    return by_stream
