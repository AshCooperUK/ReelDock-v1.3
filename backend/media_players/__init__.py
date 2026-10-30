"""Independent player connections, queued scans and atomic library-state imports."""
import hmac
import json
import time
from urllib.parse import urlsplit
from .clients import Client
from ..settings.media_players import PLAYERS


def initialise(store):
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS player_items(player TEXT,id TEXT,title TEXT,watched INTEGER,favourite INTEGER,PRIMARY KEY(player,id))')


def connection(store, player):
    return json.loads(store.get('player_'+player+'_connection') or '{}')


def settings(store, player):
    config=connection(store,player)
    with store.connect() as db:
        count=db.execute('SELECT COUNT(*),COALESCE(SUM(watched),0),COALESCE(SUM(favourite),0) FROM player_items WHERE player=?',(player,)).fetchone()
    return dict(url=config.get('url',''),username=config.get('username',''),user=config.get('user',''),library=config.get('library',''),
                connected=bool(config),secret_saved=bool(config.get('secret')),name=config.get('name',''),users=config.get('users',[]),libraries=config.get('libraries',[]),
                status=store.get('player_'+player+'_status','Not connected.'),last_sync=store.get('player_'+player+'_last_sync',''),items=count[0],watched=count[1],favourites=count[2])


def connect(store, player, body):
    old=connection(store,player)
    if set(body)-{'action','url','username','secret'}:raise ValueError('Choose supported connection fields.')
    for key in ('url','username','secret'):
        if key in body and not isinstance(body[key],str):raise ValueError('Enter valid connection details.')
    url=body.get('url','').strip().rstrip('/')
    parsed=urlsplit(url)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or len(url)>2048 or any(ord(c)<33 for c in url):
        raise ValueError('Enter an HTTP or HTTPS server address without credentials or query parameters.')
    try:parsed.port
    except ValueError:raise ValueError('Enter a valid server port.') from None
    username=body.get('username','').strip()
    secret=body.get('secret','')
    if not secret:
        if old.get('url')!=url or old.get('username','')!=username:raise ValueError('Enter credentials for the new connection.')
        secret=old.get('secret','')
    if not secret or len(secret)>4096 or any(c in secret for c in '\r\n') or len(username)>200 or ':' in username:
        raise ValueError('Enter valid credentials.')
    candidate=dict(url=url,username=username,secret=secret)
    try:
        info=Client(player,candidate).connect() # Test before replacing a working connection.
    except (KeyError,TypeError,AttributeError):
        raise ValueError('The server returned an invalid connection response.') from None
    same=old.get('url')==url and old.get('username','')==username and old.get('secret')==secret
    candidate.update(info)
    candidate['user']=old.get('user','') if same else ''
    candidate['library']=old.get('library','') if same else ''
    if player=='jellyfin':
        ids={u['id'] for u in info['users']}
        if candidate['user'] not in ids:candidate['user']=next(iter(ids)) if len(ids)==1 else ''
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if connection(store,player)!=old:
            raise ValueError('Connection changed during the check. Reload and try again.')
        values={'connection':json.dumps(candidate),'generation':str(time.time_ns()),'status':'Connected.','next':'0'}
        if not same:
            db.execute('DELETE FROM player_items WHERE player=?',(player,))
            values.update(sync='false',auto_scan='false',pending_scan='',pending_sync='',last_sync='')
        db.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)', [('player_'+player+'_'+key,value) for key,value in values.items()])


def action(store, player, body):
    name=body.get('action')
    if name=='connect':return connect(store,player,body)
    if name not in ('scan','sync','selection','disconnect'):raise ValueError('Choose a supported player action.')
    if set(body)-({'action','user' if player=='jellyfin' else 'library'} if name=='selection' else {'action'}):raise ValueError('Unexpected player fields.')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        config=connection(store,player)
        if not config:raise ValueError('Connect the media player first.')
        values={}
        if name=='disconnect':
            values=dict(connection='',sync='false',auto_scan='false',pending_scan='',pending_sync='',status='Disconnected.',last_sync='')
            db.execute('DELETE FROM player_items WHERE player=?',(player,))
        elif name=='selection':
            key='user' if player=='jellyfin' else 'library'
            options=config.get('users' if key=='user' else 'libraries',[])
            value=body.get(key)
            if player=='kodi' or not isinstance(value,str) or (value and value not in {x['id'] for x in options}):raise ValueError('Choose an available user or library.')
            config[key]=value
            values=dict(connection=json.dumps(config),next='0',last_sync='',pending_scan='',pending_sync='',status='Selection saved.')
            db.execute('DELETE FROM player_items WHERE player=?',(player,))
        else:
            if name=='sync' and player=='jellyfin' and not config.get('user'):raise ValueError('Choose a Jellyfin user first.')
            values['pending_'+name]=str(time.time_ns())
            values['status']='Library '+('scan' if name=='scan' else 'synchronisation')+' queued.'
        if name in ('selection','disconnect'):values['generation']=str(time.time_ns())
        db.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)',[('player_'+player+'_'+key,value) for key,value in values.items()])


class Superseded(Exception):pass


def tick(store, stop):
    for player in PLAYERS:
        if stop.is_set() or store.reset_pending():return
        prefix='player_'+player+'_'
        config=connection(store,player)
        if not config:continue
        generation=store.get(prefix+'generation')
        scan_request=store.get(prefix+'pending_scan')
        sync_request=store.get(prefix+'pending_sync')
        due=float(store.get(prefix+'next','0') or 0)<=time.time()
        scan=bool(scan_request) or due and store.get(prefix+'auto_scan')=='true'
        sync=bool(sync_request) or due and store.get(prefix+'sync')=='true'
        if not scan and not sync:continue
        def checkpoint():
            if stop.is_set() or store.reset_pending() or store.get(prefix+'generation')!=generation:raise Superseded()
        try:
            checkpoint()
            store.set(prefix+'next',str(time.time()+int(store.get(prefix+'minutes','15'))*60))
            client=Client(player,config)
            if scan:client.scan(checkpoint)
            rows=[]
            if sync:
                for item in client.items(checkpoint):
                    rows.append((player,item['id'],item['title'],int(item['watched']),None if item['favourite'] is None else int(item['favourite'])))
                    if len(rows)>100000:raise ValueError('Library sync exceeded the 100,000 item limit.')
            checkpoint()
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                checkpoint()
                if sync:
                    db.execute('DELETE FROM player_items WHERE player=?',(player,))
                    db.executemany('INSERT OR REPLACE INTO player_items VALUES (?,?,?,?,?)',rows)
                    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(prefix+'last_sync',str(time.time())))
                for key,request in (('pending_scan',scan_request),('pending_sync',sync_request)):
                    db.execute('DELETE FROM settings WHERE key=? AND value=?',(prefix+key,request))
                status=('Scan accepted; indexing finishes on the server. ' if scan else '')+(f'Synchronised {len(rows)} library items.' if sync else '')
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(prefix+'status',status))
        except Superseded:continue
        except Exception:
            # Never persist raw HTTP errors, credentials, response bodies or remote URLs.
            if store.get(prefix+'generation')==generation:
                store.set(prefix+'status','Could not finish. Check the connection, user and permissions; retry the action.')
                for key,request in (('pending_scan',scan_request),('pending_sync',sync_request)):
                    with store.connect() as db:db.execute('DELETE FROM settings WHERE key=? AND value=?',(prefix+key,request))


def register(app, store, access, csrf):
    from flask import request, jsonify
    initialise(store)
    @app.route('/api/media-players/<player>',methods=['GET','POST'])
    def player_settings(player):
        denied=access(local_action=True)
        if denied:return denied
        if player not in PLAYERS:return jsonify(error='Unknown media player.'),404
        if request.method=='POST':
            if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),csrf()):return jsonify(error='Reload the page.'),403
            body=request.get_json(silent=True)
            if not isinstance(body,dict):return jsonify(error='Invalid player request.'),400
            try:action(store,player,body)
            except ValueError as exc:return jsonify(error=str(exc)),400
        response=jsonify(settings(store,player));response.headers['Cache-Control']='no-store'
        return response


def loop(store, stop):
    while not stop.is_set():
        with store.background_operation():
            if not store.reset_pending():
                tick(store,stop)
        stop.wait(30)
