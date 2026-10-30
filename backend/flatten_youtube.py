"""Explicit, offline migration of ReelDock's former tv_shows directory."""
from pathlib import Path
import json
from datetime import datetime, timezone


def migrate(store, media):
    root = Path(media).resolve(strict=True)
    legacy = root / 'tv_shows'
    if legacy.is_symlink() or (legacy.exists() and legacy.resolve().parent != root):
        raise ValueError('Legacy folder is not a direct media directory.')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute("SELECT id,path FROM media_files WHERE path LIKE 'tv_shows/%'").fetchall()
        folders = {r[0] for r in db.execute('SELECT folder FROM media_channels')}
        moves = []
        if legacy.exists():
            for source in legacy.iterdir():
                target = root / source.name
                if source.name not in folders or source.is_symlink() or not source.is_dir():
                    raise ValueError('Unexpected entry in legacy folder: ' + source.name)
                if source.resolve().parent != legacy.resolve() or target.resolve().parent != root or target.exists():
                    raise ValueError('Unsafe or conflicting destination: ' + source.name)
                moves.append((source, target))
        updates = [(r['path'].removeprefix('tv_shows/'), r['id']) for r in rows]
        existing = {r[0] for r in db.execute("SELECT path FROM media_files WHERE path NOT LIKE 'tv_shows/%'")}
        if any(path in existing for path, _ in updates):
            raise ValueError('A destination is already recorded.')
        backup = store.directory / 'backups'
        backup.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        (backup / ('youtube-paths-' + stamp + '.json')).write_text(json.dumps([dict(r) for r in rows], indent=2), encoding='utf-8')
        moved = []
        try:
            for source, target in moves:
                source.rename(target); moved.append((source, target))
            db.executemany('UPDATE media_files SET path=? WHERE id=?', updates)
            current = db.execute("SELECT value FROM settings WHERE key='emby_root'").fetchone()
            if current and current[0].rstrip('/').endswith('/tv_shows'):
                db.execute("UPDATE settings SET value=? WHERE key='emby_root'", (current[0].rstrip('/')[:-9],))
            db.commit()
        except Exception:
            db.rollback()
            for source, target in reversed(moved):
                target.rename(source)
            raise
    if legacy.exists():
        legacy.rmdir()
    return {'moved_channels': len(moves), 'updated_paths': len(updates)}


if __name__ == '__main__':
    import argparse
    from backend.storage import Store
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', required=True)
    parser.parse_args()
    print(migrate(Store('/data'), '/media'))
