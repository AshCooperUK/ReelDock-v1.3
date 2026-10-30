"""Background removal of a subscription's unchanged, owned export files."""
import json
from pathlib import Path

from . import m3u


def queue(db, source):
    row = db.execute('SELECT options FROM m3u_sources WHERE id=?', (source,)).fetchone()
    options = json.loads(row['options']); options['enabled'] = False; options['hours'] = 0
    db.execute("UPDATE m3u_sources SET options=?,state='running',message='Removing subscription and its imported files…' WHERE id=?", (json.dumps(options), source))
    db.execute("INSERT OR REPLACE INTO m3u_removals VALUES(?,'queued')", (source,))


def tick(store, root):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        source = db.execute("SELECT s.* FROM m3u_sources s JOIN m3u_removals r ON r.source=s.id WHERE r.state IN ('queued','running') LIMIT 1").fetchone()
        if not source: return False
        source = dict(source)
        db.execute("UPDATE m3u_removals SET state='running' WHERE source=?", (source['id'],))
        db.execute("UPDATE m3u_sources SET state='running' WHERE id=?", (source['id'],))
        files = list(db.execute('SELECT path,digest FROM m3u_files WHERE source=?', (source['id'],)))
        others = list(db.execute('SELECT s.folder,f.path FROM m3u_files f JOIN m3u_sources s ON s.id=f.source WHERE f.source!=?', (source['id'],)))
    try:
        from .storage_layout import source_base
        base = source_base(store, root, source['folder'])
        shared = {str((source_base(store, root, r['folder'])/r['path']).absolute()) for r in others}
        checked = []
        # Check every path before deleting anything. A restart repeats safely: missing
        # files are allowed, while changed files and other subscriptions stay intact.
        for row in files:
            path = m3u.contained(base, row['path'])
            if path.suffix.lower() not in ('.strm', '.m3u', '.m3u8') or str(path) in shared: continue
            if m3u.digest_file(path) == row['digest']: checked.append((path, row['digest']))
        for index, (path, digest) in enumerate(checked):
            if m3u.digest_file(path) != digest: continue
            path.unlink(missing_ok=True)
            parent = path.parent
            while parent != base and parent.is_relative_to(base) and not (getattr(store, 'media_root', '') and parent.parent == base):
                try: parent.rmdir()
                except OSError: break
                parent = parent.parent
            if index % 250 == 0:
                with store.connect() as db:
                    db.execute('UPDATE m3u_sources SET message=? WHERE id=?', (f'Removing imported files: {index+1:,} of {len(checked):,}.',source['id']))
        with store.connect() as db:
            for table in ('m3u_files','m3u_file_info','m3u_file_aliases','m3u_runs','m3u_dry_runs','m3u_metadata_runs','m3u_matches','m3u_removals'):
                db.execute(f'DELETE FROM {table} WHERE source=?', (source['id'],))
            for table in ('xtream_accounts','xtream_series_cache','xtream_vod_info'):
                db.execute(f'DELETE FROM {table} WHERE source=?', (source['id'],))
            db.execute('DELETE FROM m3u_sources WHERE id=?', (source['id'],))
    except (OSError, ValueError):
        with store.connect() as db:
            db.execute("UPDATE m3u_removals SET state='error' WHERE source=?", (source['id'],))
            db.execute("UPDATE m3u_sources SET state='error',message='Removal interrupted. Check library permissions, then choose Remove subscription to retry. Automatic imports are off.' WHERE id=?", (source['id'],))
    return True
