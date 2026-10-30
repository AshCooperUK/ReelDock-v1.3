"""Provider fixtures only: no user servers or credentials are used."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from tests import test_engine_settings as fixture
ORIGIN=fixture.ORIGIN
from backend import media_players, settings
from backend.media_players.clients import Client


class PlayerSettingsTests(unittest.TestCase):
    setUp=fixture.EngineSettingsTests.setUp

    def post(self, player, body, token='test-token', origin=ORIGIN):
        return self.client.post('/api/media-players/'+player,base_url=ORIGIN,json=body,headers={'X-CSRF-Token':token,'Origin':origin})

    def get(self, player):
        return self.client.get('/api/media-players/'+player,base_url=ORIGIN)

    def connect(self, player='plex'):
        with patch.object(Client,'connect',return_value={'name':'Fixture','libraries':[{'id':'1','name':'Movies','type':'movie'}], 'users':[{'id':'user1','name':'Fixture'}]}):
            response=self.post(player,dict(action='connect',url='http://fixture.test:32400',secret='PRIVATE-FIXTURE'))
        self.assertEqual(response.status_code,200,response.json)
        return response

    def test_connection_secrets_are_masked_and_preferences_isolated(self):
        response=self.connect()
        self.assertNotIn('PRIVATE-FIXTURE',response.text)
        self.assertTrue(response.json['connected'])
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        settings.save(self.store,'media-players',{'plex_sync':True,'plex_minutes':30})
        self.assertEqual(self.store.get('player_plex_sync'),'true')
        self.assertFalse(settings.read(self.store,'media-players')['kodi_sync'])
        self.assertNotIn('PRIVATE-FIXTURE',json.dumps(settings.read(self.store,'media-players')))

    def test_failed_connection_preserves_saved_connection(self):
        self.connect()
        before=media_players.connection(self.store,'plex')
        with patch.object(Client,'connect',side_effect=ValueError('Rejected.')):
            self.assertEqual(self.post('plex',dict(action='connect',url='http://other.test',secret='new')).status_code,400)
        self.assertEqual(media_players.connection(self.store,'plex'),before)
        self.assertEqual(self.post('plex',dict(action='connect',url='http://other.test',secret='')).status_code,400)

    def test_permissions_url_validation_and_unknown_fields(self):
        self.assertEqual(self.post('plex',{'action':'scan'},token='bad').status_code,403)
        self.assertEqual(self.post('plex',{'action':'scan'},origin='https://wrong.test').status_code,403)
        self.assertEqual(self.app.test_client().get('/api/media-players/plex',base_url=ORIGIN).status_code,401)
        self.assertEqual(self.get('invalid').status_code,404)
        for url in ('file:///tmp/x','http://user:pass@host','http://host/?token=secret','http://host:99999','http://host/#secret'):
            self.assertEqual(self.post('plex',dict(action='connect',url=url,secret='x')).status_code,400)
        self.assertEqual(self.post('plex',dict(action='connect',url='http://host',secret='x',root='/tmp')).status_code,400)
        with self.assertRaises(ValueError):settings.save(self.store,'media-players',{'plex_sync':True})

    def test_library_import_is_atomic_and_pending_work_is_consumed(self):
        self.connect()
        self.assertEqual(self.post('plex',{'action':'sync'}).status_code,200)
        rows=[dict(id='1',title='Fixture movie',watched=True,favourite=None)]
        with patch.object(Client,'items',return_value=iter(rows)):
            media_players.tick(self.store,threading.Event())
        result=self.get('plex').json
        self.assertEqual(result['items'],1)
        self.assertEqual(result['watched'],1)
        self.assertTrue(result['last_sync'])
        self.assertFalse(self.store.get('player_plex_pending_sync'))
        def fail(*args):
            yield dict(id='2',title='Partial',watched=False,favourite=None)
            raise ValueError('fixture pagination failure')
        self.post('plex',{'action':'sync'})
        with patch.object(Client,'items',side_effect=fail):media_players.tick(self.store,threading.Event())
        self.assertEqual(self.get('plex').json['items'],1)
        self.assertEqual(self.get('plex').json['watched'],1)
        self.assertIn('Could not finish',self.get('plex').json['status'])

    def test_disable_during_sync_cancels_publication(self):
        self.connect()
        settings.save(self.store,'media-players',{'plex_sync':True})
        def cancel(*args):
            settings.save(self.store,'media-players',{'plex_sync':False})
            yield dict(id='1',title='Cancelled',watched=True,favourite=None)
        with patch.object(Client,'items',side_effect=cancel):media_players.tick(self.store,threading.Event())
        self.assertEqual(self.get('plex').json['items'],0)

    def test_scan_is_queued_and_automatic_scan_is_opt_in(self):
        self.connect()
        with patch.object(Client,'scan') as scan:
            media_players.tick(self.store,threading.Event());scan.assert_not_called()
            self.post('plex',{'action':'scan'})
            media_players.tick(self.store,threading.Event());scan.assert_called_once()
        self.assertIn('Scan accepted',self.get('plex').json['status'])
        self.assertEqual(self.post('plex',{'action':'disconnect'}).status_code,200)
        self.assertFalse(self.get('plex').json['connected'])
        self.assertEqual(media_players.connection(self.store,'plex'),{})

    def test_jellyfin_user_and_plex_selection_validation(self):
        self.connect('jellyfin')
        self.assertEqual(self.get('jellyfin').json['user'],'user1')
        self.assertEqual(self.post('jellyfin',{'action':'selection','user':'unknown'}).status_code,400)
        self.post('jellyfin',{'action':'selection','user':''})
        with self.assertRaises(ValueError):settings.save(self.store,'media-players',{'jellyfin_sync':True})
        self.connect()
        self.assertEqual(self.post('plex',{'action':'selection','library':'unknown'}).status_code,400)
        self.assertEqual(self.post('plex',{'action':'selection','library':'1'}).status_code,200)


class ClientFixtureTests(unittest.TestCase):
    def setUp(self):
        self.calls=[]
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):self.respond()
            def do_POST(self):self.respond()
            def respond(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length']))) if int(self.headers.get('Content-Length') or 0)>0 else None
                owner.calls.append((self.path,dict(self.headers),body))
                code,payload=owner.response(self.path,body)
                self.send_response(code)
                if code==302:self.send_header('Location',owner.url+'/redirect-target')
                self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(payload).encode())
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.response=lambda path,body:(200,{})

    def client(self,player):return Client(player,dict(url=self.url,secret='fixture-key',username='fixture',user='user1',library='1'))

    def test_plex_connection_scan_and_pagination(self):
        def response(path,body):
            if path=='/library/sections':return 200,{'MediaContainer':{'Directory':[{'key':'1','title':'Films','type':'movie'}]}}
            if '/refresh' in path:return 200,{}
            start='X-Plex-Container-Start=200' in path
            return 200,{'MediaContainer':{'totalSize':201,'Metadata':([{'ratingKey':'201','title':'Last','viewCount':1}] if start else [{'ratingKey':str(i),'title':'Fixture','viewCount':0} for i in range(200)])}}
        self.response=response
        client=self.client('plex');self.assertEqual(client.connect()['libraries'][0]['id'],'1');client.scan()
        items=list(client.items());self.assertEqual(len(items),201);self.assertTrue(items[-1]['watched'])
        self.assertTrue(all(headers.get('X-Plex-Token')=='fixture-key' for _,headers,_ in self.calls))
        self.assertTrue(all('fixture-key' not in path for path,_,_ in self.calls))

    def test_kodi_jsonrpc_scan_and_watched_import(self):
        def response(path,body):
            method=body['method']
            result={'name':'Kodi'} if method=='Application.GetProperties' else 'OK' if method=='VideoLibrary.Scan' else {'movies':[{'movieid':1,'title':'Fixture','playcount':1}],'limits':{'total':1}} if method=='VideoLibrary.GetMovies' else {'episodes':[],'limits':{'total':0}}
            return 200,{'jsonrpc':'2.0','id':1,'result':result}
        self.response=response
        client=self.client('kodi');self.assertEqual(client.connect()['name'],'Kodi');client.scan()
        self.assertEqual(list(client.items())[0]['watched'],True)
        self.assertTrue(all(path=='/jsonrpc' and headers.get('Authorization','').startswith('Basic ') for path,headers,_ in self.calls))

    def test_jellyfin_user_specific_items_and_favourites(self):
        def response(path,body):
            if path=='/System/Info':return 200,{'Id':'server1','ServerName':'Fixture'}
            if path=='/Users':return 200,[{'Id':'user1','Name':'Test'}]
            if path=='/Library/Refresh':return 200,{}
            return 200,{'Items':[{'Id':'video1','Name':'Movie','UserData':{'Played':True,'IsFavorite':True}}],'TotalRecordCount':1}
        self.response=response
        client=self.client('jellyfin');self.assertEqual(client.connect()['users'][0]['id'],'user1');client.scan()
        self.assertTrue(list(client.items())[0]['favourite'])
        self.assertTrue(any(path.startswith('/Users/user1/Items') for path,_,_ in self.calls))

    def test_redirect_does_not_forward_credentials_and_rpc_errors_fail(self):
        self.response=lambda path,body:(302,{})
        with self.assertRaises(ValueError):self.client('plex').connect()
        self.assertEqual(len(self.calls),1)
        self.response=lambda path,body:(200,{'jsonrpc':'2.0','id':1,'error':{'message':'fixture'}})
        with self.assertRaises(ValueError):self.client('kodi').scan()
