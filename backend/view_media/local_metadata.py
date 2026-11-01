"""Reuse exact completed Discover ownership without fetching or writing metadata."""
import json
import re
from pathlib import Path

from ..discover_store import _image


def fields(rows, root):
    result = {}
    ambiguous = set()
    for row in rows:
        if row['request_state'] != 'complete' or row['kind'] != 'movie' or row['key'] != row['episode_key']:
            continue
        match = re.fullmatch(r'movie:([0-9]{5,12})', row['key'] or '')
        if not match or not row['path']:
            continue
        try:
            body = json.loads(row['body'])
            body = {**body, **(body.get('item') or {})}
            external = str(body.get('externalId') or '')
            if not external.isdigit() or int(external) != int(match[1]):
                continue
            path = Path(root) / row['path']
            if '..' in path.parts or not path.is_relative_to(Path(root)):
                continue
            key = str(path)
            value = dict(year=str(body.get('year') or '')[:4],
                         description=str(body.get('description') or '')[:800],
                         rating=str(body.get('rating') or '')[:12],
                         genre=str(body.get('genre') or '')[:80],
                         image=_image(body.get('image')), backdrop=_image(body.get('backdrop')),
                         _imdb=int(external))
            if key in result and result[key]['_imdb'] != value['_imdb']:
                ambiguous.add(key)
            result[key] = value
        except (ValueError, TypeError, AttributeError):
            continue
    return {key: value for key, value in result.items() if key not in ambiguous}


def fill(item, entry, saved):
    if not saved or item['kind'] != 'movies':
        return item
    # A local author or a replacement can carry a different identity/year.
    # Never attach the former movie's artwork to that file.
    ids = entry.get('imdb_ids') or []
    if ids and any(value != saved['_imdb'] for value in ids):
        return item
    if item.get('year') and saved.get('year') and str(item['year']) != saved['year']:
        return item
    result = dict(item)
    for key, value in saved.items():
        if key != '_imdb' and value and not result.get(key):
            result[key] = value
    if not result.get('backdrop'):
        result['backdrop'] = result.get('image') or ''
    return result
