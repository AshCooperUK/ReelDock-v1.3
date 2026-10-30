"""Installed software credits. Only package-owned notice files are exposed."""
from functools import lru_cache
from importlib import metadata
from pathlib import Path
import re
import json
import subprocess
import sys
from urllib.parse import quote


def read(path):
    try:
        return Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


@lru_cache(maxsize=1)
def catalog():
    entries = {}
    def add(key, name, version, group, licence, files, url):
        entries[key] = dict(id=key, name=name, version=version, group=group,
                            licence=licence, files=files, url=url)
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
        name = dist.metadata['Name']
        files = [Path(dist.locate_file(f)) for f in dist.files or []
                 if re.search(r'(^|/)(?:licen[cs]e[^/]*|copying[^/]*|notice[^/]*)$', str(f), re.I)
                 and not str(f).endswith(('.py', '.pyc'))]
        licence = dist.metadata.get('License-Expression') or dist.metadata.get('License') or ''
        if not licence:
            licence = {'blinker': 'MIT', 'itsdangerous': 'BSD-3-Clause', 'jinja2': 'BSD-3-Clause'}.get(name.lower(), '')
        if not licence or len(licence) > 150 or '\n' in licence:
            licence = 'See bundled licence notices'
        add('python-' + re.sub(r'[^a-z0-9-]', '-', name.lower()), name, dist.version,
            'Python modules', licence, files, 'https://pypi.org/project/' + quote(name) + '/' + quote(dist.version) + '/')
    python_license = Path(sys.base_prefix) / ('LICENSE.txt' if sys.platform == 'win32' else f'lib/python{sys.version_info.major}.{sys.version_info.minor}/LICENSE.txt')
    add('runtime-python', 'Python', sys.version.split()[0], 'Runtimes', 'PSF-2.0 and bundled component notices', [python_license], 'https://www.python.org/')
    try:
        version = subprocess.check_output(['node', '--version'], text=True, timeout=5).strip()
        add('runtime-node', 'Node.js', version, 'Runtimes', 'MIT and bundled component notices',
            [Path('/usr/local/share/licenses/node/LICENSE')], 'https://github.com/nodejs/node/blob/' + version + '/LICENSE')
    except (OSError, subprocess.SubprocessError):
        pass
    if sys.platform == 'linux':
        try:
            packages = subprocess.check_output(['dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${db:Status-Status}\n'], text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            packages = ''
        for line in packages.splitlines():
            name, version, status = line.split('\t')
            if status != 'installed':
                continue
            package = name.split(':')[0]
            if not re.fullmatch(r'[a-z0-9+.-]+', package):
                continue
            path = Path('/usr/share/doc') / package / 'copyright'
            licences = sorted(set(re.findall(r'^License: (.+)$', read(path), re.M)))
            label = ', '.join(licences) if licences else 'See Debian copyright and licence notices'
            if len(label) > 160:
                label = 'Multiple licences — see full notices'
            add('debian-' + name, name, version, 'Container and media libraries', label,
                [path], 'https://packages.debian.org/' + quote(package))
    manifest = Path(__file__).resolve().parent.parent / 'frontend/dist/third-party-notices.json'
    try:
        packages = json.loads(read(manifest))
    except (ValueError, OSError):
        packages = []
    for package in packages:
        key = 'frontend-' + re.sub(r'[^a-z0-9-]', '-', (package['name'] + '-' + package['version']).lower())
        add(key, package['name'], package['version'], 'React frontend and build tools', package['licence'], [], package['url'])
        entries[key]['notices'] = package.get('notices', '')
    return entries


def summaries():
    return [{k: v for k, v in e.items() if k not in ('files', 'notices')} for e in catalog().values()]


def notice(key):
    entry = catalog().get(key)
    if not entry:
        return None
    blocks = [f"{entry['name']} {entry['version']}\n{entry['licence']}\n{entry['url']}"]
    if entry.get('notices'):
        blocks.append(entry['notices'])
    for path in entry['files']:
        content = read(path)
        if content:
            blocks.append(path.name + '\n\n' + content)
            # Debian copyright files refer to standard texts in this directory.
            for name in sorted(set(re.findall(r'/usr/share/common-licenses/([A-Za-z0-9.+-]+)', content))):
                common = read(Path('/usr/share/common-licenses') / name)
                if common:
                    blocks.append(name + '\n\n' + common)
    if len(blocks) == 1:
        blocks.append('The local installation does not include a notice file. See the project link above for its licence and copyright notices.')
    return '\n\n' + '\n\n----------------------------------------\n\n'.join(blocks)
