"""One explicit header action using the ordinary saved per-media policies."""
import hmac
import json
from flask import jsonify,request


def series(app,store,item):
    """Request the whole verified catalogue without changing future monitoring."""
    from . import discovery_engine,show_options,series_plan_work
    from .discover_title_releases import title_key
    item=show_options.clean(item)
    if title_key(item):
        return dict(state='choice_required',message='Automatic matching is unavailable for this title. Choose a saved release in Downloads.')
    option=show_options.ensure(store,item)
    if not show_options.snapshot(store,option)['automaticSupported']:
        return dict(state='choice_required',message='Verified episode details are not ready. Check Downloads before requesting all seasons.')
    with store.connect() as db:
        current=db.execute('''SELECT p.*,w.all_before FROM discover_series_plan p
            JOIN discover_plan_work w ON w.parent=p.parent WHERE p.parent=?''',(item['id'],)).fetchone()
    # Repeated clicks today share the current durable pass. A later explicit
    # Download All renews its released-episode boundary, while future stays as
    # chosen in Manage. Existing requests, owned files and holds remain fenced.
    if current and current['download_all'] and series_plan_work._all_day(current['all_before'])>=discovery_engine.service.today():
        plan=dict(current)
        discovery_engine.schedule(app,store,'plan:'+item['id'],lambda:discovery_engine.apply_plan(app,store,plan))
        result=dict(downloadAll=True,downloadFuture=bool(current['download_future']))
    else:
        result=discovery_engine.save_plan(app,store,item['id'],'all',True,renew_all=True)
    return dict(state='searching',**result,message='Requested all released episodes across all seasons using your saved Usenet & NZB settings. Existing downloads and cancelled-episode holds are kept. See Activity for progress.')


def run(app,store,item):
    if not isinstance(item,dict):raise ValueError('Choose a media item.')
    from .acquisition import ensure_open
    ensure_open(store)
    if item.get('kind')=='vod':
        if set(item)!={'kind','source','path'} or not all(isinstance(item.get(k),str) and item[k] for k in ('source','path')):
            raise ValueError('Choose a saved VOD file.')
        from .m3u_downloads import lookup,enqueue_resolved,_resume_selected
        row,_=lookup(app,store,item['source'],item['path'])
        _resume_selected(store,item['source'],item['path'])
        identity=enqueue_resolved(app,store,item['source'],item['path'],row)
        return dict(state='queued',requestId=identity,message='Queued using your saved server download settings.')
    from . import discover_store as records
    item=records.clean_item(item)
    if item['kind']=='show':return series(app,store,item)
    if item['kind']=='video':
        from . import downloads,channel_downloads,media_holds
        from .discover_youtube import register_video
        from .media_library import local_file
        with store.connect() as db:
            saved=db.execute('SELECT data FROM videos WHERE id=?',(item['videoId'],)).fetchone()
            current=db.execute('SELECT state FROM downloads WHERE id=?',(item['videoId'],)).fetchone()
        if local_file(store,app.config['MEDIA_DIR'],item['videoId']):return dict(state='complete',message='Already saved in your library.')
        if current and current['state'] in ('queued','downloading','cancelling'):
            return dict(state='choice_required' if current['state']=='cancelling' else 'queued',requestId=item['videoId'],message='This video is already queued or downloading.' if current['state']!='cancelling' else 'This video is still stopping. Retry once it has stopped.')
        video=json.loads(saved[0]) if saved else register_video(store,item)
        if video.get('browse_only') and video.get('state')=='unknown' and video.get('metadata_pending'):
            raise ValueError('Video details are still being checked. Open Downloads to check its status.')
        if video.get('state')!='published':
            raise ValueError('This video is not published yet. Open Downloads to check its availability.')
        refusal=channel_downloads.refusal(store,video)
        if refusal:raise ValueError(refusal)
        quality=channel_downloads.preferences(store,video['channel'])['quality']
        media_holds.resume(store,['video:'+video['id'],'managed:video:'+video['id']])
        downloads.enqueue(store,video,quality)
        return dict(state='queued',requestId=video['id'],message='Queued using your saved YouTube quality and download settings.')
    if item['kind']!='movie':raise ValueError('Choose one movie, video or VOD file.')
    if not item.get('externalId'):
        return dict(state='choice_required',message='This title needs a verified source. Choose one in Downloads.')
    from .media_management import service,store as managed
    from .discover_logic import movie_identity
    from . import discovery_engine
    key=movie_identity(item['externalId'])
    if not managed.row(store,key):service.enrol(app,store,item,refresh=False)
    row=managed.row(store,key)
    if not row:raise ValueError('Open this movie again before downloading.')
    composed=service.compose(app,store,row)
    if composed.get('file'):return dict(state='complete',message='Already saved in your library.')
    with store.connect() as db:
        previous=db.execute('SELECT * FROM discover_requests WHERE episode_key=? ORDER BY created DESC LIMIT 1',(key,)).fetchone()
        if previous and previous['state']=='cancelling':
            return dict(state='choice_required',requestId=previous['id'],message='This movie is still stopping. Retry once it has stopped.')
        from .source_choice import eligibility
        if previous and eligibility(db,previous):
            return dict(state='choice_required',requestId=previous['id'],message='The previous source failed. Choose Find alternatives in Activity or select another file in Downloads.')
    state=discovery_engine.queue(app,store,key)
    if state.get('file'):return dict(state='complete',message='Already saved in your library.')
    return dict(state='searching',requestId=state.get('requestId'),message='Requested using your saved movie quality, fallback and size settings. See Activity for progress.')


def register(app,store,access,csrf):
    @app.post('/api/quick-download')
    def quick_download():
        denied=access(local_action=True)
        if denied:return denied
        if not csrf() or not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return jsonify(error='Reload the page.'),403
        body=request.get_json(silent=True)
        if not isinstance(body,dict) or set(body)!={'item'}:return jsonify(error='Choose one media item.'),400
        from .discover import _limited
        if not _limited(app,'quick_download_hits',30):return jsonify(error='Too many download requests. Check Activity before trying again.'),429
        try:return jsonify(run(app,store,body['item'])),202
        except (ValueError,TypeError) as exc:return jsonify(error=str(exc) if isinstance(exc,ValueError) else 'Choose a valid media item.'),409
