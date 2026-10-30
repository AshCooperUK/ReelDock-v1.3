"""Authenticated Discover routes. They do not replace /api/downloads."""
import hmac
import re
import time

from flask import jsonify, request

from . import discover_sources, discover_store
from .discover_acquire import process, queue_manual, resume_waiting, sync_jobs, sync_videos
from .discover_logic import CHARTS, FEED_NAMES
from .discover_monitor import remove_rule, save_rule, set_enabled
from .discover_sources import DiscoverError


def _limited(app, key, maximum):
    now = time.monotonic()
    hits = app.extensions.setdefault(key, [])
    fresh = [stamp for stamp in hits if now - stamp < 60]
    if len(fresh) >= maximum:
        hits[:] = fresh
        return False
    fresh.append(now)
    hits[:] = fresh
    return True


def _body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise DiscoverError('Send a JSON object.')
    return body


_YOUTUBE_SHORT_HOSTS = {'youtu.be'}
_YOUTUBE_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com',
                  'youtube-nocookie.com', 'www.youtube-nocookie.com'}


def _video_id(text):
    """Extract an 11-character video ID from a YouTube address or a bare ID.

    Only the ID leaves this function — extraction requests always use the
    canonical watch URL, so no arbitrary address ever reaches yt-dlp."""
    from urllib.parse import parse_qs, urlsplit
    text = text.strip()
    if re.fullmatch(r'[A-Za-z0-9_-]{11}', text):
        return text
    if len(text) > 400:
        return ''
    try:
        parsed = urlsplit(text if '://' in text else 'https://' + text)
    except ValueError:
        return ''
    host = (parsed.hostname or '').lower()
    candidate = ''
    if host in _YOUTUBE_SHORT_HOSTS:
        candidate = parsed.path.strip('/').split('/')[0]
    elif host in _YOUTUBE_HOSTS:
        if parsed.path == '/watch':
            candidate = (parse_qs(parsed.query).get('v') or [''])[0]
        elif re.match(r'^/(shorts|live|embed|v)/', parsed.path):
            candidate = parsed.path.split('/')[2]
    return candidate if re.fullmatch(r'[A-Za-z0-9_-]{11}', candidate) else ''


def register(app, store, access, csrf):
    discover_store.initialise(store)

    def denied():
        return access(local_action=True)

    def csrf_ok():
        return hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), csrf())

    @app.get('/api/discover/state')
    def discover_state():
        blocked = denied()
        if blocked:
            return blocked
        from .system_stats import worker_online
        prefs = discover_store.preferences(store)
        nzb = True
        torrent = False
        try:
            discover_sources.indexers_for(app, store)
        except DiscoverError:
            nzb = False
        return jsonify(
            preferences=prefs,
            watchlist=discover_store.watchlist(store),
            follows=[],
            subscriptions=_subscription_ids(store),
            rules=discover_store.rules(store),
            requests=discover_store.public_requests(store),
            capabilities={
                'nzb': nzb,
                'torrent': torrent,
                'torrentMessage': 'Torrent search is not configured.',
                'workerOnline': worker_online(store.get('worker_heartbeat', '0')),
                'youtubeError': store.get('discover_youtube_error', ''),
            },
        )

    @app.get('/api/discover/feed')
    def discover_feed():
        blocked = denied()
        if blocked:
            return blocked
        if not _limited(app, 'discover_feed_hits', 60):
            return jsonify(error='Too many catalogue requests. Wait a moment.'), 429
        name = request.args.get('name', '')
        if name not in FEED_NAMES:
            return jsonify(error='Unknown collection.'), 400
        try:
            return jsonify(discover_sources.feed(app, store, name))
        except DiscoverError as exc:
            return jsonify(error=str(exc)), 502

    @app.get('/api/discover/youtube')
    def discover_youtube_chart():
        blocked = denied()
        if blocked:
            return blocked
        name = request.args.get('name', '')
        if name not in CHARTS:
            return jsonify(error='Unknown YouTube collection.'), 400
        try:
            payload = discover_sources.load_chart(store, name)
            if request.args.get('limit'):
                try: limit = int(request.args['limit'])
                except ValueError: return jsonify(error='Choose a valid chart page size.'), 400
                if not 1 <= limit <= 1000: return jsonify(error='Choose a valid chart page size.'), 400
                payload = dict(payload, items=payload['items'][:limit], partial=len(payload['items']) > limit)
            return jsonify(payload)
        except DiscoverError as exc:
            return jsonify(error=str(exc)), 400

    @app.get('/api/discover/episodes')
    def discover_episodes():
        blocked = denied()
        if blocked:
            return blocked
        if not _limited(app, 'discover_episode_hits', 40):
            return jsonify(error='Too many episode lookups. Wait a moment.'), 429
        try:
            return jsonify(discover_sources.episodes(request.args.get('tvdb', ''), store, tvmaze=request.args.get('tvmaze')))
        except DiscoverError as exc:
            status = 404 if 'No TVmaze' in str(exc) else 502
            return jsonify(error=str(exc)), status

    @app.post('/api/discover/manual-episodes/search')
    @app.post('/api/discover/manual-episodes/acquire')
    def discover_manual_episode():
        blocked=denied()
        if blocked:return blocked
        if not csrf_ok():return jsonify(error='Reload the page.'),403
        if not _limited(app,'discover_special_hits',20):return jsonify(error='Too many requests. Wait a moment.'),429
        try:
            from . import discover_manual_episodes as manual
            body=_body()
            if request.path.endswith('/search'):return jsonify(manual.search(app,store,body.get('tvmaze'),body.get('episodeId')))
            return jsonify(request=manual.queue(app,store,body.get('tvmaze'),body.get('episodeId'),body.get('releaseId'),body.get('requestId')))
        except ValueError as exc:return jsonify(error=str(exc)),400

    @app.post('/api/discover/specials/search')
    @app.post('/api/discover/specials/acquire')
    def discover_special():
        blocked = denied()
        if blocked: return blocked
        if not csrf_ok(): return jsonify(error='Reload the page.'), 403
        if not _limited(app, 'discover_special_hits', 20):
            return jsonify(error='Too many special requests. Wait a moment.'), 429
        try:
            from . import discover_specials
            body = _body()
            tvdb, episode_id = body.get('tvdb'), body.get('episodeId')
            if request.path.endswith('/search'):
                return jsonify(discover_specials.search(app, store, tvdb, episode_id))
            row = discover_specials.queue(app, store, tvdb, episode_id, body.get('releaseId'), body.get('requestId'))
            return jsonify(request=row)
        except (DiscoverError, ValueError) as exc:
            return jsonify(error=str(exc)), 400

    @app.get('/api/discover/uploads')
    def discover_uploads():
        blocked = denied()
        if blocked:
            return blocked
        try:
            offset = max(0, min(int(request.args.get('offset', '0')), 10000))
        except ValueError:
            return jsonify(error='Invalid page.'), 400
        saved = discover_sources.subscription_uploads(store)
        subscribed = set(saved['channels'])
        follows = discover_store.follows(store)
        followed = []
        for item in follows:
            channel_id = item.get('channelId') or ''
            if not channel_id and str(item.get('id') or '').startswith('youtube:channel:'):
                channel_id = item['id'].split(':', 2)[-1]
            if re.fullmatch(r'UC[A-Za-z0-9_-]{22}', channel_id) and channel_id not in subscribed:
                followed.append(channel_id)
        batch = followed[offset:offset + 12]
        items, failed = list(saved['items']), False
        seen = {item.get('videoId') for item in items}
        for channel in batch:
            try:
                for upload in discover_sources.uploads(channel).get('items') or []:
                    if upload.get('videoId') not in seen:
                        seen.add(upload.get('videoId'))
                        items.append(upload)
            except DiscoverError:
                failed = True
        items.sort(key=lambda item: item.get('published') or '', reverse=True)
        total = len(subscribed) + len(followed)
        message = ''
        if not total:
            message = 'No YouTube subscriptions yet. Channels from your YouTube subscriptions appear here after they are added to ReelDock.'
        elif not items:
            message = 'Your YouTube subscriptions are saved. Recent uploads appear after the next guide check.'
        return jsonify(items=items[:60], nextOffset=offset + 12 if offset + 12 < len(followed) else None,
                       failed=failed, totalChannels=total, message=message, source='subscriptions')

    @app.get('/api/discover/search')
    def discover_search():
        blocked = denied()
        if blocked:
            return blocked
        if not _limited(app, 'discover_search_hits', 30):
            return jsonify(error='Too many searches. Wait a moment.'), 429
        provider = request.args.get('provider', '')
        query = request.args.get('q', '')
        try:
            offset = int(request.args.get('offset', '0'))
        except ValueError:
            return jsonify(error='Invalid search page.'), 400
        try:
            if provider == 'youtube':
                return jsonify(discover_sources.search_youtube(query, offset))
            if offset < 0 or offset % 100:
                return jsonify(error='Invalid search page.'), 400
            if provider not in ('movies', 'shows'):
                return jsonify(error='Unknown search.'), 400
            return jsonify(discover_sources.search_nzb(app, store, provider, query, offset))
        except DiscoverError as exc:
            return jsonify(error=str(exc)), 400

    @app.post('/api/discover/preferences')
    def discover_preferences():
        blocked = denied()
        if blocked:
            return blocked
        if not csrf_ok():
            return jsonify(error='Reload the page.'), 403
        try:
            body = _body()
            return jsonify(preferences=discover_store.save_preference(store, body.get('key'), body.get('value')))
        except DiscoverError as exc:
            return jsonify(error=str(exc)), 400
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    @app.post('/api/discover/watchlist')
    def discover_watch():
        blocked = denied()
        if blocked:
            return blocked
        if not csrf_ok():
            return jsonify(error='Reload the page.'), 403
        try:
            return jsonify(discover_store.toggle_watch(store, _body().get('item')))
        except (DiscoverError, ValueError) as exc:
            return jsonify(error=str(exc)), 400

    @app.post('/api/discover/subscribe')
    @app.post('/api/discover/follows')
    def discover_subscribe():
        blocked = denied()
        if blocked:
            return blocked
        if not csrf_ok():
            return jsonify(error='Reload the page.'), 403
        try:
            from .youtube_account import toggle, subscribe, unsubscribe
            item = _body().get('item') or {}
            if item.get('kind') == 'video':
                from .view_media.download_sources import channel_item
                item = channel_item(app, store, item)
            action = _body().get('action')
            if action not in (None, 'subscribe', 'unsubscribe'):
                raise ValueError('Choose a subscription action.')
            handler = subscribe if action == 'subscribe' else unsubscribe if action == 'unsubscribe' else toggle
            return jsonify(handler(app, store, item))
        except (DiscoverError, ValueError) as exc:
            return jsonify(error=str(exc)), 400

    @app.post('/api/discover/rules')
    def discover_rules():
        blocked = denied()
        if blocked:
            return blocked
        if not csrf_ok():
            return jsonify(error='Reload the page.'), 403
        try:
            body = _body()
            action = body.get('action') or 'save'
            if action == 'remove':
                remove_rule(store, str(body.get('id') or ''))
                return jsonify(rules=discover_store.rules(store))
            if action == 'pause':
                set_enabled(store, str(body.get('id') or ''), False)
                return jsonify(rules=discover_store.rules(store))
            if action == 'resume':
                set_enabled(store, str(body.get('id') or ''), True)
                return jsonify(rules=discover_store.rules(store))
            episodes = None
            if body.get('enabled'):
                show_id = str((body.get('showId') or ''))
                catalogue = discover_sources.episodes(show_id, store)
                episodes = catalogue.get('episodes') or []
            saved = save_rule(store, body, episodes)
            return jsonify(rule=saved, rules=discover_store.rules(store))
        except (DiscoverError, ValueError) as exc:
            return jsonify(error=str(exc)), 400

    @app.post('/api/discover/youtube/register')
    def discover_register_video():
        blocked = denied()
        if blocked:
            return blocked
        if not csrf_ok():
            return jsonify(error='Reload the page.'), 403
        try:
            from .discover_youtube import register_video
            video = register_video(store, _body().get('item') or {})
            return jsonify(id=video['id'])
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    @app.post('/api/discover/youtube/resolve')
    def discover_resolve_video():
        blocked = denied()
        if blocked:
            return blocked
        if not csrf_ok():
            return jsonify(error='Reload the page.'), 403
        if not _limited(app, 'discover_resolve_hits', 15):
            return jsonify(error='Too many lookups. Wait a moment.'), 429
        try:
            video_id = _video_id(str(_body().get('url') or ''))
        except DiscoverError as exc:
            return jsonify(error=str(exc)), 400
        if not video_id:
            return jsonify(error='Paste a YouTube video address, such as https://www.youtube.com/watch?v=… or https://youtu.be/…'), 400
        import json as _json
        with store.connect() as db:
            row = db.execute('''SELECT v.data,c.data AS channel_data FROM videos v
                LEFT JOIN channels c ON c.id=v.channel WHERE v.id=?''', (video_id,)).fetchone()
            job = db.execute('SELECT state FROM downloads WHERE id=? AND state IN (\'queued\',\'downloading\',\'cancelling\')', (video_id,)).fetchone()
        from .local_library import entry
        saved = entry(store, app.config['MEDIA_DIR'], video_id)
        saved_file = bool(saved and saved['bytes'])
        if row:
            video = _json.loads(row['data'])
            channel = _json.loads(row['channel_data']) if row['channel_data'] else {}
            item = {'kind': 'video', 'id': 'youtube:video:' + video_id, 'videoId': video_id,
                    'title': video.get('title') or video_id, 'channel': channel.get('name') or 'YouTube',
                    'channelId': video.get('channel') or '', 'image': video.get('thumbnail') or '',
                    'description': video.get('description') or '', 'published': video.get('date') or ''}
            return jsonify(item=item, saved=saved_file, state=job['state'] if job else '')
        try:
            from .auto_downloads import _extract_youtube
            info = _extract_youtube(store, 'https://www.youtube.com/watch?v=' + video_id, 'video')
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        live_status = str(info.get('live_status') or '')
        if live_status == 'is_upcoming':
            return jsonify(error='This is a scheduled live broadcast. It can be downloaded after it ends.'), 409
        if live_status == 'is_live':
            return jsonify(error='This video is broadcasting live. Try again after it ends.'), 409
        uploaded = str(info.get('upload_date') or '')
        published = uploaded[:4] + '-' + uploaded[4:6] + '-' + uploaded[6:8] if re.fullmatch(r'\d{8}', uploaded) else ''
        thumbnail = str(info.get('thumbnail') or '')
        item = {'kind': 'video', 'id': 'youtube:video:' + video_id, 'videoId': video_id,
                'title': str(info.get('title') or video_id)[:300],
                'channel': str(info.get('channel') or info.get('uploader') or 'YouTube')[:180],
                'channelId': str(info.get('channel_id') or ''),
                'image': thumbnail if thumbnail.startswith('https://i.ytimg.com/') else 'https://i.ytimg.com/vi/' + video_id + '/hqdefault.jpg',
                'description': str(info.get('description') or '')[:800],
                'published': published, 'publishedApproximate': not bool(published)}
        return jsonify(item=item, saved=False, state='')

    @app.post('/api/discover/acquire')
    def discover_acquire():
        blocked = denied()
        if blocked:
            return blocked
        if not csrf_ok():
            return jsonify(error='Reload the page.'), 403
        if not _limited(app, 'discover_acquire_hits', 20):
            return jsonify(error='Too many download requests. Wait a moment.'), 429
        try:
            body = _body()
            item = body.get('item') or {}
            if item.get('kind') == 'video':
                return jsonify(_queue_youtube(store, item, body))
            rows = queue_manual(store, item, body.get('episodes') or [], body.get('quality'), body.get('fallback'),
                                body.get('source') or 'NZB first', str(body.get('requestId') or ''))
            results = []
            for row in rows:
                if row.get('blocked'):
                    results.append({'id': row.get('id'), 'state': row.get('state'), 'message': row.get('message'), 'blocked': True})
                    continue
                results.append(process(app, store, row))
            return jsonify(requests=results)
        except (DiscoverError, ValueError) as exc:
            return jsonify(error=str(exc)), 400

    @app.post('/api/discover/requests/cancel')
    def discover_cancel():
        blocked = denied()
        if blocked:
            return blocked
        if not csrf_ok():
            return jsonify(error='Reload the page.'), 403
        try:
            body = _body()
            request_id = str(body.get('id') or '')
            with store.connect() as db:
                row = db.execute('SELECT * FROM discover_requests WHERE id=?', (request_id,)).fetchone()
            if not row:
                video_id = str(body.get('videoId') or '')
                if re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
                    with store.connect() as db:
                        db.execute("UPDATE downloads SET state=CASE WHEN state='queued' THEN 'cancelled' ELSE 'cancelling' END, updated=? WHERE id=? AND state IN ('queued','downloading')",
                                   (time.time(), video_id))
                    return jsonify(ok=True)
                return jsonify(error='Unknown request.'), 404
            from .acquisition import cancel_discover
            result = cancel_discover(app, store, request_id)
            if not result.get('ok'):
                return jsonify(error='; '.join(result.get('errors') or [result.get('error') or 'The download is still stopping.'])), result.get('status') or 409
            return jsonify(ok=True, state=result.get('state'), message=result.get('message') or '')
        except DiscoverError as exc:
            return jsonify(error=str(exc)), 400

    @app.get('/api/discover/requests')
    def discover_requests():
        blocked = denied()
        if blocked:
            return blocked
        sync_jobs(app, store)
        return jsonify(requests=discover_store.public_requests(store))


def json_video(row):
    import json
    try:
        return json.loads(row['body']).get('videoId') or ''
    except (TypeError, json.JSONDecodeError):
        return ''


def _subscription_ids(store):
    try:
        from .youtube_account import active_ids
        return active_ids(store)
    except Exception:
        return []


def _remember_channel(store, item):
    import json
    channel_id = item.get('channelId') or ''
    if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', channel_id):
        return
    channel = {'id': channel_id, 'name': item.get('title') or 'YouTube', 'avatar': item.get('image') or '',
               'description': 'Followed from Discover. This does not subscribe through Google.'}
    with store.connect() as db:
        existing = db.execute('SELECT active FROM channels WHERE id=?', (channel_id,)).fetchone()
        if not existing:
            db.execute('INSERT INTO channels(id,data,active) VALUES (?,?,0)', (channel_id, json.dumps(channel)))


def _queue_youtube(store, item, body):
    from .downloads import enqueue
    from .discover_youtube import register_video
    from .discover_logic import QUALITIES
    quality = str(body.get('quality') or '')
    fallback = body.get('fallback')
    token = str(body.get('requestId') or '')
    if quality not in QUALITIES or type(fallback) is not bool:
        raise ValueError('Choose a quality.')
    import re as regex
    if not regex.fullmatch(r'[A-Za-z0-9-]{16,64}', token):
        raise ValueError('Invalid request identity.')
    video = register_video(store, item)
    from .media_holds import resume
    resume(store,['video:'+video['id'],'managed:video:'+video['id']])
    with store.connect() as db:
        current = db.execute('SELECT state FROM downloads WHERE id=?', (video['id'],)).fetchone()
    if current and current['state'] in ('queued', 'downloading', 'cancelling', 'complete'):
        state = current['state']
        message = 'Already saved.' if state == 'complete' else 'Already downloading.'
        row, _created = discover_store.insert_request(store, {
            'idempotency': token, 'kind': 'video', 'title': video['title'], 'quality': quality, 'fallback': fallback,
            'manual': True, 'state': 'complete' if state == 'complete' else 'queued', 'message': message,
            'body': {'videoId': video['id']}, 'episode_key': 'video:' + video['id'],
        })
        return {'requests': [{'id': row['id'], 'state': row['state'], 'message': message, 'videoId': video['id'], 'title': row['title'], 'quality': row['quality']}]}
    from .discover_store import set_quality_policy
    set_quality_policy(store, video['id'], fallback)
    enqueue(store, video, quality, automatic=False)
    row, _created = discover_store.insert_request(store, {
        'idempotency': token, 'kind': 'video', 'title': video['title'], 'quality': quality, 'fallback': fallback,
        'manual': True, 'state': 'queued', 'message': 'Queued with yt-dlp. The worker downloads this video on its own.',
        'body': {'videoId': video['id']}, 'episode_key': 'video:' + video['id'],
    })
    return {'requests': [{'id': row['id'], 'state': 'queued', 'message': row['message'], 'videoId': video['id'], 'title': row['title'], 'quality': row['quality']}]}


def loop(app, store, stop):
    """Worker-owned chart refresh, future-season checks and NZB progress."""
    import logging
    from .discover_monitor import tick
    while not stop.is_set():
        try:
            if store.get('setup_complete') and not store.reset_pending():
                with store.background_operation():
                    from .discover_handoff import recover as recover_handoffs
                    recover_handoffs(app,store)
                    from .discovery_engine import warm_first_pages
                    warm_first_pages(app,store)
                    tick(app, store)
                    from .media_management import tick as management_tick
                    management_tick(app, store)
                    discover_sources.maybe_refresh(store)
                    discover_sources.refresh_catalogue(app, store)
                    sync_videos(store)
                    from .discovery_engine import tick as discovery_tick
                    discovery_tick(app,store)
                    resume_waiting(app, store)
                    sync_jobs(app, store)
        except Exception:
            logging.exception('Discover background work paused; retrying')
        stop.wait(30)
