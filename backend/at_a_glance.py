"""Read-only engine overview. Never opens an engine or contacts a provider.

Only counts and fixed labels leave this module: job payloads can contain private
tracker URLs, server credentials and archive passwords. Saved engine state is
explicitly distinguished from live in-process state.
"""
from collections import Counter
from datetime import datetime
import json
import math
import os
import sqlite3
from pathlib import Path
import time
from zoneinfo import ZoneInfo

LONDON = ZoneInfo('Europe/London')


def number(value):
    try:
        result = float(value or 0)
        return result if math.isfinite(result) else 0
    except (TypeError, ValueError):
        return 0


def object_file(path):
    if path.is_symlink() or path.stat().st_size > 32_000_000:
        raise ValueError('Invalid state file')
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError('Invalid state object')
    return data


def engine_summary(app, store, kind, paused):
    engine = app.extensions.get(kind + '_engine')
    live = engine is not None and not engine.stop.is_set()
    counts = Counter()
    errors = 0
    with store.connect() as db:
        replacements = db.execute("SELECT engine_id,title,body FROM discover_requests WHERE engine=? AND state IN ('queued','downloading','verifying','paused','complete')", (kind,)).fetchall()

    def superseded(job_id, data=None):
        if not replacements or not job_id:
            return False
        if data is None:
            path = Path(store.directory) / (kind+'-engine') / ('torrents' if kind=='torrent' else 'jobs') / (job_id+'.json')
            if not path.exists():
                return False
            data = object_file(path)
        # Only catalogue-owned attempts, not unrelated manual jobs for the title.
        if data.get('name') != 'discover' or not data.get('lookup_identity'):
            return False
        from .downloaders.torrent.rename import release_save_as
        for row in replacements:
            body = json.loads(row['body'] or '{}')
            identity = str(body.get('externalId') or '').removeprefix('tt').lstrip('0')
            saved = str(data['lookup_identity']).removeprefix('tt').lstrip('0')
            if row['engine_id'] and row['engine_id'] != job_id and identity == saved and release_save_as(row['title']) == release_save_as(data.get('save_as','')):
                return True
        return False
    if live:
        jobs = engine.jobs()
        for job in jobs:
            state = job['state']
            if state == 'failed' and superseded(job.get('id')):
                continue
            if state == 'downloading' and kind == 'nzb' and job.get('progress', 0) >= 1:
                state = 'processing'
            if state == 'downloading' and kind == 'torrent' and not job.get('peers') and not job.get('down_rate'):
                state = 'waiting'  # Includes trackers/web seeds; not proof that peers alone are the cause.
            if state == 'stopped' and job.get('progress', 0) >= 1 and not job.get('error'):
                state = 'complete'
            counts[state] += 1
            if job.get('error') and state != 'failed':
                errors += 1
    else:
        folder = Path(store.directory) / (kind + '-engine') / ('torrents' if kind == 'torrent' else 'jobs')
        # A missing state directory means this engine has no saved jobs.
        if folder.exists():
            for index, path in enumerate(folder.glob('*.json')):
                if index >= 2000:
                    raise ValueError('Summary limit exceeded')
                data = object_file(path)
                phase = data.get('phase', 'unknown')
                if phase == 'failed' and superseded(data.get('id') or path.stem, data):
                    continue
                state = 'paused' if data.get('user_paused') or (paused and phase not in ('stopped', 'choose', 'failed')) else phase
                counts[{'download': 'pending', 'seed': 'stopped'}.get(state, state)] += 1
                if data.get('error') and state != 'failed':
                    errors += 1
    labels = [('downloading', 'downloading'), ('pending', 'saved pending'), ('queued', 'queued'),
              ('waiting', 'waiting for transfer'), ('processing', 'processing'), ('paused', 'paused'), ('choose', 'need placement'),
              ('failed', 'failed'), ('stopped', 'stopped'), ('complete', 'completed')]
    detail = ' · '.join(f'{counts[key]:,} {label}' for key, label in labels if counts[key]) or 'No jobs'
    unknown = sum(value for key, value in counts.items() if key not in dict(labels))
    if unknown:
        detail += f' · {unknown:,} other states'
    if errors:
        detail += f' · {errors:,} with errors'
    status = 'Live' if live else 'Saved state'
    if kind == 'nzb':
        config_path = Path(store.directory) / 'nzb-engine' / 'settings.json'
        config = object_file(config_path) if config_path.exists() else {}
        if not any(server.get('enabled', True) and server.get('host') for server in config.get('servers', [])):
            status = 'Not configured'
    return dict(detail=detail, status=status,
                attention=counts['failed'] + counts['choose'] + errors)


def snapshot(app, store, overview, now=None):
    now = time.time() if now is None else now
    start = datetime.fromtimestamp(now, LONDON).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = []
    schedules = []
    paused = overview['downloads_paused'] or store.get('acquisition_hold') == '1'

    def row(identity, title, target, collect):
        try:
            value = collect()
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            value = dict(status='Unavailable', detail='Could not read this engine’s summary.', attention=1)
        rows.append(dict(id=identity, title=title, target=target, **value))

    with store.connect() as db:
        def states(table):
            return dict(db.execute(f'SELECT state,count(*) FROM {table} GROUP BY state'))

        def queue(counts):
            return ' · '.join(f'{counts.get(key, 0):,} {label}' for key, label in [('downloading', 'downloading'), ('running', 'running'), ('queued', 'queued'), ('failed', 'failed'), ('cancelling', 'cancelling')] if counts.get(key)) or 'No active downloads'

        def youtube():
            counts = overview['downloads']
            detail = f"{overview['channels']:,} subscriptions · {overview['favourites']:,} favourites · {counts.get('complete', 0):,} completed downloads"
            next_check = number(store.get('next_sync'))
            sync_paused = store.get('youtube_sync_paused') == 'true'
            if next_check and not sync_paused and overview['channels']:
                schedules.append(dict(title='YouTube upload check', at=next_check, target='YouTube'))
            return dict(brief=f"{overview['channels']:,} subscriptions · {counts.get('complete', 0):,} saved", status='Checks paused' if sync_paused else 'Ready', detail=detail + '\n' + queue(counts), attention=counts.get('failed', 0))
        row('youtube', 'YouTube', 'YouTube', youtube)

        def vod():
            counts = states('m3u_downloads')
            catalogue = overview['m3u']
            scheduled = db.execute("SELECT min(next_run) FROM m3u_sources WHERE state!='running' AND json_valid(options) AND json_extract(options,'$.enabled')=1 AND json_extract(options,'$.hours')>0").fetchone()[0]
            if scheduled is not None:
                schedules.append(dict(title='VOD library update', at=number(scheduled), target='M3U Subscriptions'))
            latest=db.execute("SELECT r.stats FROM m3u_runs r JOIN m3u_sources s ON s.id=r.source WHERE r.state='complete' ORDER BY r.finished DESC,r.id DESC LIMIT 1").fetchone()
            changes='No completed update yet'
            if latest:
                try:
                    report=json.loads(latest['stats'])
                    added,removed=report['added'],report['removed']
                    if type(added) is not int or type(removed) is not int or min(added,removed)<0:
                        raise ValueError('Invalid change counts')
                    changes=f'Latest update: {added:,} added · {removed:,} removed'
                except (ValueError,TypeError,KeyError):
                    changes='Latest update counts unavailable'
            shows = f"{catalogue['shows']:,}" if catalogue['shows'] is not None else 'Unknown'
            return dict(brief=f"Catalogue: {catalogue['movies']:,} movies · {shows} shows\n{changes}", status=f"{catalogue['playlists']:,} sources" if catalogue['playlists'] else 'Not configured',
                        detail=f"Catalogue: {catalogue['movies']:,} movies · {shows} shows · {catalogue['episodes']:,} episodes\n{counts.get('complete', 0):,} completed downloads · " + queue(counts), attention=counts.get('failed', 0))
        row('vod', 'VOD', 'M3U Subscriptions', vod)
        for kind, title in [('nzb', 'NZB'), ('torrent', 'Torrents')]:
            row(kind, title, kind, lambda kind=kind: engine_summary(app, store, kind, paused))

        def auto():
            from .auto_downloads import watch_folder, LIMITS, _key
            folder = watch_folder(app, store)
            pending = None
            failed = 0
            if folder:
                keys = set()
                # Only current arrivals count; old failed receipts are history.
                with os.scandir(folder) as entries:
                    for index, entry in enumerate(entries):
                        if index >= 2000:
                            raise ValueError('Intake summary limit exceeded')
                        if entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in LIMITS:
                            info = entry.stat(follow_symlinks=False)
                            keys.add(_key(entry.name, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino))
                pending = len(keys)
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_downloads'").fetchone():
                    failed = sum(item[0] in keys for item in db.execute("SELECT key FROM auto_downloads WHERE state='failed'"))
            enabled = store.get('download_auto_downloads') == 'true'
            status = 'Off' if not enabled else 'Paused' if paused else 'Watching' if overview['worker_online'] else 'Worker offline'
            if enabled and folder is None:
                status = 'Folder unavailable'
            return dict(status=status, detail=(f'{pending:,} files in intake' if pending is not None else 'Intake unavailable') + f' · {failed:,} awaiting retry', attention=failed)
        row('auto', 'Auto-Download', 'settings:Auto-Download', auto)

        def metadata():
            enabled = [name for key, name in [('youtube_share_metadata', 'YouTube'), ('m3u_share_metadata', 'VOD')] if store.get(key, 'true') == 'true']
            for kind, name in [('nzb', 'NZB'), ('torrent', 'Torrents')]:
                path = Path(store.directory) / (kind + '-engine') / 'settings.json'
                if not path.exists() or object_file(path).get('share_metadata', True):
                    enabled.append(name)
            pending = db.execute('SELECT count(*) FROM m3u_catalog_titles WHERE checked<?', (now - 7 * 86400,)).fetchone()[0]
            failed = db.execute("SELECT count(*) FROM activity_tasks WHERE (id LIKE '%metadata%' OR id IN ('m3u-catalogue','m3u-exports','youtube-artwork')) AND state IN ('failed','error') AND updated>?", (now - 86400,)).fetchone()[0]
            vod_status = f'{pending:,} VOD titles due for checks' if 'VOD' in enabled else 'VOD checks off'
            return dict(brief=' · '.join(enabled) if enabled else 'Disabled', status=f'{len(enabled)} engines on', detail=('Enabled: ' + ', '.join(enabled) if enabled else 'All metadata disabled') + f'\n{vod_status} · {failed:,} recent failed tasks\nMissing artwork is not audited.', attention=failed)
        row('metadata', 'Metadata', 'settings:Metadata', metadata)

        def players():
            configured = ['Emby'] if store.get('emby_url') and store.get('emby_key') else []
            syncs = []
            failed = 0
            for key, name in [('jellyfin', 'Jellyfin'), ('plex', 'Plex'), ('kodi', 'Kodi')]:
                if json.loads(store.get('player_' + key + '_connection') or '{}'):
                    configured.append(name)
                    last = number(store.get('player_' + key + '_last_sync'))
                    if last:
                        syncs.append(dict(title=name + ' last sync', at=last))
                    failed += store.get('player_' + key + '_status', '').startswith('Could not finish.')
                    if store.get('player_' + key + '_sync') == 'true' or store.get('player_' + key + '_auto_scan') == 'true':
                        schedules.append(dict(title=name + ' library update', at=number(store.get('player_' + key + '_next')), target='settings:Media Players'))
            failed += bool('Emby' in configured and 'failed' in store.get('emby_favourites_status', '').lower())
            return dict(brief=' · '.join(configured) if configured else '', status=f'{len(configured)} configured' if configured else 'Not configured',
                        detail=(', '.join(configured) + f' · {failed} reported sync problems\nSaved connection state; not a live health check.' if configured else 'Emby, Jellyfin, Plex and Kodi'),
                        times=syncs, attention=failed)
        row('players', 'Media Players', 'settings:Media Players', players)

        def discover():
            counts = states('discover_requests')
            from .activity import alert_id
            dismissed = {r[0] for r in db.execute('SELECT id FROM dismissed_alerts')}
            counts['failed'] = sum(alert_id('discover-'+r['id'], r['message'] or 'The request failed.') not in dismissed
                                   for r in db.execute("SELECT id,message FROM discover_requests WHERE state='failed'"))
            detail = ' · '.join(f'{counts.get(key, 0):,} {key}' for key in ('searching', 'starting', 'waiting', 'queued', 'downloading', 'verifying', 'paused', 'cancelling', 'failed') if counts.get(key)) or 'No pending requests'
            return dict(status='On hold' if paused else 'Requests', detail=detail, attention=counts.get('failed', 0))
        row('discover', 'Discover', 'Discover', discover)
        completed = sum(db.execute(f"SELECT count(*) FROM {table} WHERE state='complete' AND updated>=? AND updated<=?", (start, now)).fetchone()[0] for table in ('downloads', 'm3u_downloads'))
        local = db.execute('SELECT count(*) FROM media_files WHERE bytes>0').fetchone()[0]
    from .vpn import cached_status
    vpn = cached_status()
    if vpn.get('configured') is True:
        state = vpn.get('state')
        if state not in ('Connected', 'Connecting', 'Disconnected', 'Blocked', 'Unavailable'):
            state = 'Unavailable'
        protection = vpn.get('values') if isinstance(vpn.get('values'), dict) else {}
        rows.append(dict(id='vpn', title='VPN', target='settings:VPN', status=state,
                         brief='', detail='', attention=int(any(protection.values()) and vpn.get('connected') is not True)))
    schedules.sort(key=lambda item: item['at'])
    return dict(rows=rows, updated=now, paused=paused, worker_online=overview['worker_online'],
                attention=sum(bool(item.get('attention')) for item in rows),
                completed_today=completed, indexed_youtube=local,
                next=schedules[0] if schedules else None)
