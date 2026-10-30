"""One dedicated process owns scheduled imports, independent of open browsers."""
import os
import time
import threading
import logging
import sqlite3
from .app import create_app, VERSION
from .catalogue import Importer
from .artwork import cache_batch, cache_loop
from .history import batch as history_batch
from .downloads import loop as download_loop, recover as recover_downloads


def tick(importer, download_artwork=True):
    store = importer.store
    store.set('worker_heartbeat', time.time())
    store.set('worker_version', VERSION)
    if not store.get('setup_complete'):
        return
    from .watchlist import sync_automatic
    sync_automatic(store)
    try:
        from .youtube_account import flush
        flush(importer.app, store)
    except Exception:
        logging.exception('YouTube subscription sync will retry')
    if store.get('youtube_sync_paused')=='true':
        if download_artwork:cache_batch(store)
        return
    from .youtube_fallback import add_batch, backfill_channel_art
    add_batch(importer)
    backfill_channel_art(importer)
    due = float(store.get('next_sync', '0')) <= time.time()
    requested = store.get('sync_requested') == '1'
    # The persisted schedule survives upgrades and restarts. A release number
    # alone must not spend quota rechecking an already scanned catalogue.
    if due or requested:
        if requested:
            store.set('youtube_subscriptions_next', '0')
            store.set('youtube_cookie_subscriptions_next', '0')
        store.set('sync_requested', '0')
        store.set('sync_running', '1')
        try:
            importer.run()
            from .event_log import record
            if not int(store.get('youtube_fallback_remaining','0')):record(store,'YouTube scan finished','The scheduled or requested YouTube scan finished.','YouTube')
        except Exception as exc:
            # Provider errors are already sanitised. Never persist token/raw bodies.
            from .sync_controls import SyncPaused
            message = str(exc) if isinstance(exc, ValueError) else 'Import interrupted. Saved data is safe; the worker will retry.'
            store.set('sync_message', message)
            from .event_log import record
            record(store,'YouTube scan paused' if isinstance(exc,SyncPaused) else 'YouTube scan failed','The scan did not finish. Check YouTube connection and scan status.','YouTube','cancelled' if isinstance(exc,SyncPaused) else 'failed')
            if not isinstance(exc,SyncPaused):store.set('sync_error', message)
        finally:
            store.set('sync_running', '0')
            from .sync_schedule import minutes
            interval=5 if int(store.get('youtube_fallback_remaining','0')) else minutes(store)*60
            store.set('next_sync',time.time()+interval)
    if store.get('youtube_sync_paused')!='true' and float(store.get('next_history','0')) <= time.time():
        store.set('guide_running','1')
        try:
            from .guide_weeks import batch as week_batch
            week_batch(importer)
            store.set('guide_running','0')
            store.set('history_running','1')
            history_batch(importer)
        finally:
            store.set('guide_running','0')
            store.set('history_running','0')
            store.set('next_history',time.time()+60)
    if download_artwork:
        cache_batch(store)


def run_tick(importer):
    with importer.store.background_operation():
        if importer.store.reset_pending():
            return
        try:
            tick(importer, download_artwork=False)
        except Exception:
            logging.exception('Background worker cycle failed; retrying')
            try:
                importer.store.set('sync_message', 'Background worker paused after an error. Retrying shortly.')
            except (sqlite3.Error, OSError):
                # The original error may be the database itself. Keep the loop alive.
                logging.warning('Could not save worker status; retrying on the next cycle')


def main():
    os.environ["REELDOCK_DOWNLOADS"] = "worker"
    app = create_app({'PROCESS_ROLE':'worker'})
    importer = Importer(app)
    # Linux advisory lock is held for the process lifetime and released on a crash.
    import fcntl
    with (importer.store.directory / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        from .event_log import started
        started(importer.store,VERSION,'worker')
        importer.store.set('sync_running', '0')
        importer.store.set('history_running', '0')
        importer.store.set('guide_running', '0')
        importer.store.set('youtube_push_running', '0')
        stop = threading.Event()
        from .channel_refresh import recover as recover_viewed_channels
        recover_viewed_channels(importer.store)
        from .channel_upload_browse import recover as recover_upload_pages
        from .channel_metadata_refresh import recover as recover_channel_metadata
        recover_upload_pages(importer.store)
        recover_channel_metadata(importer.store)
        from .guide_weeks import recover as recover_viewed_guide
        recover_viewed_guide(importer.store)
        from .network_activity import loop as network_loop
        network = threading.Thread(target=network_loop, args=(importer.store, stop), daemon=True, name='network')
        network.start()
        from .channel_refresh import loop as viewed_channel_loop
        viewed_channels = threading.Thread(target=viewed_channel_loop, args=(app, importer.store, stop), daemon=True, name='viewed-channel-uploads')
        viewed_channels.start()
        from .channel_upload_browse import loop as viewed_pages_loop
        viewed_pages = threading.Thread(target=viewed_pages_loop, args=(app, importer.store, stop), daemon=True, name='viewed-channel-pages')
        viewed_pages.start()
        from .guide_weeks import loop as viewed_guide_loop
        viewed_guide = threading.Thread(target=viewed_guide_loop, args=(app, importer.store, stop), daemon=True, name='viewed-guide')
        viewed_guide.start()
        from .channel_metadata_refresh import loop as viewed_information_loop
        viewed_information = threading.Thread(target=viewed_information_loop, args=(app, importer.store, stop), daemon=True, name='viewed-channel-information')
        viewed_information.start()
        artwork = threading.Thread(target=cache_loop, args=(importer.store, stop), daemon=True, name='artwork')
        artwork.start()
        recover_downloads(importer.store)
        downloader = threading.Thread(target=download_loop, args=(importer.store, app.config['MEDIA_DIR'], stop), daemon=True, name='downloads')
        downloader.start()
        from .emby import loop as emby_loop
        media_sync=threading.Thread(target=emby_loop,args=(importer.store,stop),daemon=True,name='emby')
        media_sync.start()
        from .media_players import loop as players_loop
        players_sync=threading.Thread(target=players_loop,args=(importer.store,stop),daemon=True,name='media-players')
        players_sync.start()
        from .youtube_push import loop as push_loop
        push = threading.Thread(target=push_loop,args=(app,stop),daemon=True,name='youtube-push')
        push.start()
        from .m3u import loop as m3u_loop
        playlists = threading.Thread(target=m3u_loop, args=(importer.store, app.config['M3U_DIR'], stop), daemon=True, name='m3u')
        playlists.start()
        from .m3u_catalogue import loop as catalogue_loop
        m3u_catalogue=threading.Thread(target=catalogue_loop,args=(importer.store,stop),daemon=True,name='m3u-catalogue')
        m3u_catalogue.start()
        from .m3u_downloads import loop as server_download_loop
        from .m3u_sidecars import loop as sidecar_loop
        server_downloads=threading.Thread(target=server_download_loop,args=(app,importer.store,stop),daemon=True,name="m3u-downloads")
        sidecars=threading.Thread(target=sidecar_loop,args=(app,importer.store,stop),daemon=True,name="m3u-sidecars")
        server_downloads.start();sidecars.start()
        from .auto_downloads import loop as auto_loop
        auto_downloads=threading.Thread(target=auto_loop,args=(app,importer.store,stop,{'strm'}),daemon=True,name='auto-downloads')
        auto_downloads.start()
        from .discover import loop as discover_loop
        discover=threading.Thread(target=discover_loop,args=(app,importer.store,stop),daemon=True,name='discover')
        discover.start()
        from .view_media.saved_catalogue import loop as saved_catalogue_loop
        saved_catalogue=threading.Thread(target=saved_catalogue_loop,args=(app,importer.store,stop),daemon=True,name='saved-media-catalogue')
        saved_catalogue.start()
        try:
            while True:
                run_tick(importer)
                time.sleep(5)
        finally:
            stop.set()
            network.join(timeout=6)
            viewed_channels.join(timeout=20)
            viewed_pages.join(timeout=35)
            viewed_guide.join(timeout=65)
            viewed_information.join(timeout=35)
            artwork.join(timeout=30)
            downloader.join(timeout=15)
            media_sync.join(timeout=15)
            players_sync.join(timeout=15)
            push.join(timeout=15)
            playlists.join(timeout=15)
            m3u_catalogue.join(timeout=15)
            server_downloads.join(timeout=25)
            sidecars.join(timeout=25)
            auto_downloads.join(timeout=10)
            discover.join(timeout=15)
            saved_catalogue.join(timeout=10)


if __name__ == '__main__':
    main()
