"""Private cookie storage for the future downloader; never used by embeds."""
import os
import tempfile

LIMIT = 256 * 1024

def save(directory, data):
    if len(data) > LIMIT:
        raise ValueError('Choose a cookies.txt file smaller than 256 KB.')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValueError('Choose a UTF-8 Netscape cookies.txt export.') from None
    lines = text.splitlines()
    if not lines or lines[0].strip() not in ('# Netscape HTTP Cookie File', '# HTTP Cookie File'):
        raise ValueError('The file must be a Netscape cookies.txt export.')
    selected = []
    for line in lines[1:]:
        if not line.strip() or (line.startswith('#') and not line.startswith('#HttpOnly_')):
            continue
        fields = line.removeprefix('#HttpOnly_').split('\t')
        if len(fields) != 7:
            raise ValueError('The cookie file contains an invalid row.')
        domain, subdomains, path, secure, expiry, name, value = fields
        if (subdomains not in ('TRUE', 'FALSE') or secure not in ('TRUE', 'FALSE')
                or not expiry.isdigit() or not path.startswith('/') or not name
                or any(ord(c) < 32 or ord(c) == 127 for field in fields for c in field)):
            raise ValueError('The cookie file contains an invalid row.')
        domain = domain.lower().lstrip('.')
        if any(domain == allowed or domain.endswith('.' + allowed) for allowed in ('youtube.com', 'google.com')):
            selected.append(line)
    if not selected:
        raise ValueError('No YouTube or Google cookies were found in this file.')
    target = directory / 'youtube-cookies.txt'
    fd, temporary = tempfile.mkstemp(prefix='.cookies-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as output:
            output.write('# Netscape HTTP Cookie File\n' + '\n'.join(selected) + '\n')
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def allow_channel_import(store):
    from .settings.youtube import FIELDS
    return FIELDS['cookie_import_channels'].read(store)


def allow_downloads(store):
    from .settings.youtube import FIELDS
    return FIELDS['cookie_downloads'].read(store)
