"""Authenticated management facade around existing acquisition engines."""
import hmac
import json
import time
from flask import jsonify,request
from . import store as records,service

def _history(store,key,message):records.history(store,key,message)

def change(app,store,key,action,value=None,confirm=False):
    r=records.row(store,key)
    if not r:raise ValueError('Unknown managed title.')
    if action in ('resume','search','replace'):
        from ..media_holds import resume
        resume(store,['managed:'+key]+([key] if key.startswith('video:') else []))
        r=records.row(store,key)
    state=service.compose(app,store,r)
    if action in ('remove','delete') and confirm is not True:raise ValueError('Confirm this action first.')
    if action in ('remove','delete') and state['active']:raise ValueError('Cancel the active acquisition before removing this item.')
    local_changes = None
    if action=='cancel':
        with store.connect() as db:db.execute('UPDATE managed_media SET hold=1 WHERE key=?',(key,))
        if r['kind']=='video':
            video=service.body(r)['item'].get('videoId','')
            with store.connect() as db:db.execute("UPDATE downloads SET state=CASE WHEN state='queued' THEN 'cancelled' ELSE 'cancelling' END WHERE id=? AND state IN ('queued','downloading')",(video,))
        elif state.get('requestId'):
            from ..acquisition import cancel_discover
            result=cancel_discover(app,store,state['requestId'])
            if not result.get('ok'):raise ValueError('Acquisition is still stopping. Retry shortly.')
    elif action=='delete':
        from .files import delete
        delete(app,store,r,bool(value.get('folder')) if isinstance(value,dict) else bool(value))
    elif action=='rename':
        from .files import rename
        local_changes = rename(app,store,r,value)
    elif action=='repair-metadata':
        from .files import repair
        message=repair(app,store,r)
        with store.connect() as db:db.execute('UPDATE managed_media SET message=? WHERE key=?',(message,key))
    elif action=='replace':
        service.queue(app,store,r,value,replace=True)
    elif action in ('monitor','quality','fallback','remove','restore','resume','inherit'):
        with store.connect() as db:
            if action=='monitor':
                if type(value)!=bool:raise ValueError('Choose whether to monitor.')
                if value and not service.body(r).get('automatic'):raise ValueError('Automatic matching is unavailable. Choose a release manually.')
                db.execute('UPDATE managed_media SET monitor=?,quality=?,fallback=?,next_check=0 WHERE key=?',(int(value),state['quality'],int(state['fallback']),key))
            elif action=='quality':
                if value not in ('720','1080','2160'):raise ValueError('Choose a supported quality.')
                db.execute('UPDATE managed_media SET quality=?,fallback=?,monitor=? WHERE key=?',(value,int(state['fallback']),int(state['monitored']),key))
            elif action=='fallback':
                if type(value)!=bool:raise ValueError('Choose whether lower quality is allowed.')
                db.execute('UPDATE managed_media SET fallback=?,quality=?,monitor=? WHERE key=?',(int(value),state['quality'],int(state['monitored']),key))
            elif action=='remove':db.execute('UPDATE managed_media SET removed=1,hold=1,monitor=0 WHERE key=?',(key,))
            elif action=='restore':db.execute('UPDATE managed_media SET removed=0,hold=1,monitor=0 WHERE key=?',(key,))
            elif action=='resume':db.execute('UPDATE managed_media SET hold=0,next_check=0 WHERE key=?',(key,))
            elif action=='inherit':db.execute('UPDATE managed_media SET monitor=-1 WHERE key=?',(key,))
    elif action=='search':
        if state['active']:raise ValueError('An acquisition is already active.')
        with store.connect() as db:db.execute('UPDATE managed_media SET hold=0 WHERE key=?',(key,))
        try:service.queue(app,store,records.row(store,key),value)
        except Exception:
            with store.connect() as db:db.execute('UPDATE managed_media SET hold=? WHERE key=?',(r['hold'],key))
            raise
    else:raise ValueError('Unknown media action.')
    if action not in ('rename','delete','repair-metadata','replace'):_history(store,key,{'delete':'Deleted video file; catalogue and sidecars retained; acquisition paused.','remove':'Removed from management; saved file kept.','restore':'Restored; monitoring off and acquisition paused.'}.get(action,action.capitalize()+' saved.'))
    item = service.compose(app,store,records.row(store,key))
    if local_changes is not None:item['localChanges'] = local_changes
    return item

def register(app,store,access,csrf):
    records.initialise(store)
    @app.route('/api/media-management',methods=['GET','POST'])
    def management():
        denied=access(local_action=True)
        if denied:return denied
        if request.method=='POST' and not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return jsonify(error='Reload the page.'),403
        try:
            if request.method=='GET':return jsonify(service.listing(app,store,parent=request.args.get('parent'),kind=request.args.get('kind'),key=request.args.get('key')))
            b=request.get_json(silent=True) or {}
            if not isinstance(b,dict):raise ValueError('Choose a media action.')
            action=b.get('action');key=str(b.get('key') or '')
            from ..discover import _limited
            if action in ('enrol','enrol-local','releases','search','replace') and not _limited(app,'managed_media_search',30):return jsonify(error='Too many requests. Wait a moment.'),429
            if action=='enrol':return jsonify(service.enrol(app,store,b.get('item')))
            if action=='enrol-local':
                from .local import enrol
                return jsonify(enrol(app,store,b.get('value')))
            if action=='series-settings':
                parent=b.get('parent');field=b.get('field');value=b.get('value')
                with store.connect() as db:rows=db.execute("SELECT * FROM managed_media WHERE parent=? AND kind='episode'",(parent,)).fetchall()
                if not rows:raise ValueError('Open a series first.')
                r=dict(rows[0]);existing=records.series_for_key(store,r['key'],r)
                defaults=service.effective_policy(store,r)
                policy=existing or dict(quality=defaults['quality'],fallback=defaults['fallback'],monitoring='inherit')
                if field=='quality' and value not in ('720','1080','2160'):raise ValueError('Choose a supported quality.')
                if field=='fallback' and type(value)!=bool:raise ValueError('Choose whether lower quality is allowed.')
                if field=='monitoring' and value not in ('inherit','all','off'):raise ValueError('Choose a monitoring policy.')
                if field not in ('quality','fallback','monitoring'):raise ValueError('Unknown series setting.')
                policy[field]=value
                show=service.body(r)['item'].get('externalId','')
                if field=='monitoring' and value=='all' and not show:raise ValueError('This series requires manual release selection.')
                with store.connect() as db:
                    db.execute('INSERT OR REPLACE INTO managed_series(parent,show_id,quality,fallback,monitoring) VALUES(?,?,?,?,?)',(parent,show,policy['quality'],int(policy['fallback']),policy['monitoring']))
                    db.execute('UPDATE managed_media SET next_check=0 WHERE parent=?',(parent,))
                    if field=='quality' and db.execute("SELECT 1 FROM sqlite_master WHERE name='show_options'").fetchone():
                        db.execute("UPDATE show_options SET quality=?,updated=? WHERE json_extract(item,'$.id')=? AND key=?",(value,time.time(),parent,parent))
                records.history(store,r['key'],'Series '+field+' saved.')
                from ..watchlist import sync_auto_titles
                sync_auto_titles(store)
                return jsonify(service.listing(app,store,parent=parent))
            if action=='season':
                if type(b.get('value'))!=bool:raise ValueError('Choose whether to monitor.')
                with store.connect() as db:rows=db.execute('SELECT * FROM managed_media WHERE parent=?',(b.get('parent'),)).fetchall()
                for row in rows:
                    r=dict(row)
                    if service.body(r).get('episode',{}).get('season')==b.get('season') and not r['removed'] and service.body(r).get('automatic'):
                        change(app,store,r['key'],'monitor',b['value'])
                from ..watchlist import sync_auto_titles
                sync_auto_titles(store)
                return jsonify(service.listing(app,store,parent=b.get('parent')))
            r=records.row(store,key)
            if not r:raise ValueError('Unknown managed title.')
            if action=='releases':return jsonify(service.candidates(app,store,r))
            if action=='history':
                with store.connect() as db:
                    rows=[dict(row) for row in db.execute('SELECT message,created FROM managed_history WHERE media_key=? ORDER BY id DESC LIMIT 100',(key,))]
                    rows.extend(dict(row) for row in db.execute('SELECT a.message,a.created FROM discover_attempts a JOIN discover_requests r ON r.id=a.request_id WHERE r.episode_key=? ORDER BY a.id DESC LIMIT 100',(key,)))
                return jsonify(items=sorted(rows,key=lambda r:r['created'],reverse=True))
            item=change(app,store,key,action,b.get('value'),b.get('confirm'))
            from ..watchlist import sync_auto_titles
            sync_auto_titles(store)
            return jsonify(item=item,**({'message':item['message']} if action=='repair-metadata' else {}))
        except (ValueError,OSError) as exc:
            from ..discover_logic import redact
            return jsonify(error=redact(str(exc))),400
        except Exception:
            app.logger.exception('Media management request failed')
            return jsonify(error='Media management is temporarily unavailable. Retry shortly.'),503

def tick(app,store):
    from ..acquisition import held
    if held(store):return
    # Self-heal pre-existing duplicate records; reconciled once per day per data set.
    if time.time()-float(store.get('managed_alias_reconcile','0') or 0)>86400:
        try:
            if records.reconcile(store):app.logger.info('Media management folded duplicate entries.')
        except Exception:app.logger.warning('Media management reconciliation failed.',exc_info=True)
        store.set('managed_alias_reconcile',str(time.time()))
    now=time.time()
    with store.connect() as db:
        rows=db.execute("SELECT * FROM managed_media WHERE (monitor=1 OR parent IN (SELECT parent FROM managed_series WHERE monitoring<>'off')) AND removed=0 AND hold=0 AND next_check<=? AND lease_until<? ORDER BY next_check LIMIT 2",(now,now)).fetchall()
    for saved in rows:
        r=dict(saved);b=service.body(r);claimed=r['key']
        if not b.get('automatic') and not (r['kind']=='video' and r['monitor']==1):continue
        with store.connect() as db:
            claim=db.execute('UPDATE managed_media SET lease_until=? WHERE key=? AND lease_until<? AND hold=0 AND removed=0',(now+300,r['key'],now)).rowcount
        if not claim:continue
        message='';delay=900
        try:
            if r['kind']=='movie':
                from ..movie_options import find,check_automatic
                if find(store,b['item']):
                    check_automatic(app,store,r)
                    continue
            # Refresh catalogue before scheduling so postponed dates are respected.
            # Enrolment may fold this row into an alias survivor; re-resolve.
            service.enrol(app,store,b['item'])
            fresh=records.locate(store,r['key'],r['parent'],r['kind'],b)
            if fresh is None:continue
            r=fresh;b=service.body(r)
            state=service.compose(app,store,r)
            if state['monitored'] and not records.policy_blocks(store,r['key']) and b.get('date') and b['date']<=service.today() and not state['file'] and not state['active']:
                q=service.request_for(store,r['key'])
                if not q or q['state'] not in ('waiting','failed'):
                    if r['kind']=='video':
                        from ..downloads import enqueue
                        with store.connect() as db:video=db.execute('SELECT data FROM videos WHERE id=?',(b['item']['videoId'],)).fetchone()
                        value=json.loads(video['data']) if video else {}
                        with store.connect() as db:paused=db.execute('SELECT subscription_paused FROM download_rules WHERE channel=?',(value.get('channel',''),)).fetchone()
                        if value.get('state')=='published' and not (paused and paused[0]):
                            policy=service.effective_policy(store,r)
                            enqueue(store,value,policy['quality'],automatic=True)
                    else:service.queue(app,store,r,automatic=True)
        except Exception as exc:
            from ..discover_logic import redact
            message='Source check unavailable. '+redact(str(exc));delay=3600
        finally:
            # The claimed row may have been folded away; only release our own lease.
            with store.connect() as db:db.execute('UPDATE managed_media SET lease_until=0,last_check=?,next_check=?,message=? WHERE key=?',(now,now+delay,message,claimed))
