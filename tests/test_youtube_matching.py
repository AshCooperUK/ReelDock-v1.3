"""Shared matching and player responses are fixtures, never live history writes."""
import json
import threading
import unittest
from unittest.mock import patch
from tests import test_engine_settings as fixture
from backend import youtube_matching as matching
from backend.media_players.clients import Client


class MatchingTests(unittest.TestCase):
    setUp=fixture.EngineSettingsTests.setUp
    def fixture(self, player='plex'):
        self.store.set('youtube_watched_source',player)
        self.store.set('emby_watched','true')
        self.store.set('player_'+player+'_connection',json.dumps(dict(url='http://fixture',secret='PRIVATE',user='u')))
        self.store.media_root='/reeldock'
        with self.store.connect() as db:
            for video,path in [('video000001','YouTube Downloads/channel/one.mkv'),('video000002','YouTube Subscriptions/channel/two.mkv')]:
                db.execute('INSERT INTO media_files VALUES (?,?,?,?,?,?,?)',(video,'channel',path,'2026-10-10',int(video[-1]),'emby',100))
                db.execute("INSERT INTO downloads(id,title,quality,state,created,updated) VALUES (?,?, '1080','complete',0,0)",(video,video))

    def run_match(self, rows):
        with patch.object(Client,'items',return_value=iter(rows)),patch('backend.watched_status.send_watched_request',return_value=('sent','Sent fixture')) as send:
            matching.match_watched(self.store,threading.Event())
            return send

    def test_shared_exact_match_and_conflicting_identity(self):
        self.fixture()
        rows=[dict(watched=True,provider_ids={'youtube':'unknown0000'},paths=['/reeldock/YouTube Subscriptions/channel/two.mkv']),dict(watched=True,provider_ids={'youtube':'video000001'},paths=['/reeldock/YouTube Subscriptions/channel/two.mkv']),dict(watched=False,provider_ids={'youtube':'video000001'}),dict(watched=True,title='video000001'),dict(watched=True,paths=['/reeldock/YouTube Downloads/channel/one.mkv'])]
        send=self.run_match(rows);send.assert_called_once();self.assertEqual(send.call_args.args[0],'video000001')
        self.run_match(rows).assert_not_called()
        self.store.set('youtube_watched_source','kodi')
        self.run_match(rows).assert_not_called() # Receipt protects against resending after a source switch.

    def test_change_or_disable_during_scan_sends_nothing(self):
        self.fixture()
        for key,value in [('youtube_watched_source','kodi'),('emby_watched','false'),('youtube_watched_generation','new')]:
            self.store.set('youtube_watched_source','plex');self.store.set('emby_watched','true');self.store.set('youtube_watched_generation','old')
            def scan(checkpoint):
                self.store.set(key,value)
                yield dict(watched=True,provider_ids={'youtube':'video000001'})
            with patch.object(Client,'items',side_effect=scan),patch('backend.watched_status.send_watched_request') as send:
                with self.assertRaises(matching.Cancelled):matching.match_watched(self.store,threading.Event())
                send.assert_not_called()

    def test_partial_scan_and_inflight_source_change(self):
        self.fixture()
        def broken(checkpoint):
            yield dict(watched=True,provider_ids={'youtube':'video000001'})
            raise ValueError('Fixture pagination error')
        with patch.object(Client,'items',side_effect=broken),patch('backend.watched_status.send_watched_request') as send:
            with self.assertRaises(ValueError):matching.match_watched(self.store,threading.Event())
            send.assert_not_called()
        original=matching.source_scope(self.store)
        def switch(*args):
            self.store.set('youtube_watched_source','kodi')
            self.store.set('emby_watched_status','New source queued')
            return 'sent','Sent fixture'
        rows=[dict(watched=True,provider_ids={'youtube':v}) for v in ('video000001','video000002')]
        with patch.object(Client,'items',return_value=iter(rows)),patch('backend.watched_status.send_watched_request',side_effect=switch) as send:
            with self.assertRaises(matching.Cancelled):matching.match_watched(self.store,threading.Event())
            send.assert_called_once()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM emby_watched WHERE scope=?',(original,)).fetchone()[0],'sent')
        self.assertEqual(self.store.get('emby_watched_status'),'New source queued')

    def test_api_security_validation_and_secret_masking(self):
        self.fixture()
        endpoint='/api/youtube/watched-matching'
        self.assertEqual(self.app.test_client().get(endpoint,base_url=fixture.ORIGIN).status_code,401)
        self.assertEqual(self.client.post(endpoint,base_url=fixture.ORIGIN,json={'action':'sync'}).status_code,403)
        with self.assertRaises(ValueError):matching.save(self.store,{'source':'unsupported'})
        with self.assertRaises(ValueError):matching.save(self.store,{'enabled':True}) # No cookies
        matching.save(self.store,{'enabled':False,'source':'kodi'})
        self.assertFalse(matching.settings(self.store)['enabled'])
        self.assertNotIn('PRIVATE',json.dumps(matching.settings(self.store)))

    def test_each_adapter_supplies_exact_paths_and_provider_ids(self):
        for player in ('jellyfin','plex','kodi'):
            client=Client(player,dict(user='u'))
            if player=='jellyfin':
                response={'Items':[{'Id':'1','ProviderIds':{'YouTube':'video000001'},'Path':'/one','UserData':{'Played':True}}],'TotalRecordCount':1}
                with patch.object(client,'request',return_value=response) as request:
                    rows=list(client.items());self.assertIn('ProviderIds',request.call_args.kwargs['params']['Fields'])
            elif player=='plex':
                response={'MediaContainer':{'size':1,'Metadata':[{'ratingKey':'1','viewCount':1,'Guid':[{'id':'youtube://video000001'}],'Media':[{'Part':[{'file':'/one'}]}]}]}}
                with patch.object(client,'connect',return_value={'libraries':[{'id':'1','type':'movie'}]}),patch.object(client,'request',return_value=response):rows=list(client.items())
            else:
                responses=[{'movies':[{'movieid':1,'playcount':1,'file':'/one','uniqueid':{'youtube':'video000001'}}],'limits':{'total':1}},{'episodes':[],'limits':{'total':0}}]
                with patch.object(client,'rpc',side_effect=responses):rows=list(client.items())
            self.assertTrue(rows[0]['watched']);self.assertEqual(rows[0]['paths'],['/one'])
            self.assertEqual({k.lower():v for k,v in rows[0]['provider_ids'].items()},{'youtube':'video000001'})
