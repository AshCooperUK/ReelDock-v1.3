import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch
from backend.app import create_app
from backend.download_history import page

class DownloadHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=create_app({'TESTING':True,'DATA_DIR':self.tmp.name,'MEDIA_DIR':self.tmp.name+'/media'})
        self.store=self.app.extensions['store']
        self.store.set('public_origin','http://127.0.0.1:8790')
        with self.store.connect() as db:
            for i in range(65):
                if i%2:
                    db.execute("INSERT INTO m3u_downloads(id,source,path,title,state,created,updated,message,destination) VALUES (?,'missing',?,'Server video','failed',?,?,?,?)",(str(i),str(i)+'.strm',i,999-i,'The provider could not be reached.','/media/movie.mp4'))
                else:
                    db.execute("INSERT INTO downloads(id,title,quality,state,created,updated,message) VALUES (?,'Video','720','complete',?,?,'Saved on NAS')",(str(i).zfill(11),i,999-i))
    def tearDown(self):self.tmp.cleanup()
    def test_combined_limit_and_creation_order(self):
        for limit,count in [(15,15),(30,30),(45,45),(50,50)]:
            data=page(self.store,self.app.config['MEDIA_DIR'],limit)
            self.assertEqual(len(data['items']),count)
            self.assertEqual(data['total'],65)
            self.assertTrue(data['more'])
            self.assertEqual([r['item']['created'] for r in data['items']],list(range(64,64-count,-1)))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM downloads').fetchone()[0],33)
    def test_all_history_in_fifteen_item_pages(self):
        rows=[]
        for offset in range(0,65,15):
            data=page(self.store,self.app.config['MEDIA_DIR'],15,offset)
            rows.extend(data['items'])
            self.assertEqual(data['more'],offset+15<65)
        self.assertEqual([row['item']['created'] for row in rows],list(range(64,-1,-1)))
        with self.assertRaises(ValueError):page(self.store,self.app.config['MEDIA_DIR'],15,-1)

    def test_metadata_and_missing_channel_preserved(self):
        data=page(self.store,self.app.config['MEDIA_DIR'])
        server=next(row['item'] for row in data['items'] if row['kind']=='m3u')
        self.assertEqual(server['message'],'The provider could not be reached.')
        self.assertEqual(server['destination'],'/media/movie.mp4')
        self.assertEqual(server['subscription'],'M3U')
        self.assertEqual(data['items'][0]['item']['destination'],'')
    def test_invalid_limit_and_login(self):
        for limit in (0,51,-1):
            with self.assertRaises(ValueError):page(self.store,self.app.config['MEDIA_DIR'],limit)
        self.assertEqual(self.app.test_client().get('/api/download-history',base_url='http://127.0.0.1:8790').status_code,401)

    def test_syncs_interleave_with_downloads_and_preserve_breakdown(self):
        import json
        stats={'added':3,'removed':2,'changes':{'movies':{'added':3,'updated':0,'removed':2}}}
        with self.store.connect() as db:
            db.execute("INSERT INTO m3u_runs(source,finished,state,message,stats) VALUES ('missing',63.5,'complete','Import complete.',?)",(json.dumps(stats),))
            db.execute("INSERT INTO m3u_runs(source,finished,state,message,stats) VALUES ('missing',62.5,'error','Failed.',?)",(json.dumps(stats),))
        data=page(self.store,self.app.config['MEDIA_DIR'])
        self.assertEqual(data['total'],67)
        self.assertEqual([row['kind'] for row in data['items'][:4]],['youtube','m3u_sync','m3u','m3u_sync'])
        self.assertEqual(data['items'][1]['item']['stats'],stats)
        self.assertEqual(data['items'][3]['item']['stats'],{})

    def seed_unenriched_show(self):
        body={'externalId':'fixture-show','description':'Saved overview','posterChecked':0}
        with self.store.connect() as db:
            db.execute("INSERT INTO discover_requests(id,idempotency,kind,title,state,quality,fallback,body,message,progress,updated,created) VALUES('show-read','show-read','season','Saved programme','failed','1080',0,?,'Saved outcome',0,100,100)", (json.dumps(body),))
        return body

    def dump(self):
        with self.store.connect() as db:return '\n'.join(db.iterdump())

    def test_plain_history_is_saved_facts_only_with_no_provider_filesystem_or_database_writes(self):
        body=self.seed_unenriched_show()
        self.store.set('acquisition_hold','1')
        with self.store.connect() as db:db.execute("UPDATE downloads SET state='queued' WHERE id='00000000000'")
        before=self.dump()
        with patch('backend.discover_sources.show_media',side_effect=AssertionError('No provider work during export')) as provider, \
             patch('backend.download_history.local_file',side_effect=AssertionError('No filesystem resolution during export')) as local:
            data=page(self.store,self.app.config['MEDIA_DIR'],plain=True)
        provider.assert_not_called();local.assert_not_called()
        self.assertEqual(self.dump(),before)
        show=next(row['item'] for row in data['items'] if row['item']['id']=='show-read')
        self.assertEqual(show['description'],'Saved overview')
        self.assertEqual(show['message'],'Saved outcome')
        self.assertEqual(show['artwork'],'')
        self.assertEqual(json.loads(show['body']),body)
        self.assertNotIn('externalId',show)
        self.assertNotIn('posterChecked',show)
        self.assertEqual(data['total'],66)
        self.assertTrue(data['more'])
        self.assertTrue(data['acquisitionHold'])
        active=next(row for row in data['active'] if row['item']['id']=='00000000000')
        self.assertEqual(active['item']['state'],'queued')
        self.assertEqual(active['item']['destination'],'')

    def test_default_history_still_enriches_show_and_resolves_saved_youtube_file(self):
        self.seed_unenriched_show()
        metadata=dict(image='/fixture-poster.jpg',genre='Drama',summary='Fetched overview',year='2026',rating='8.2')
        with patch('backend.discover_sources.show_media',return_value=metadata) as provider, \
             patch('backend.download_history.local_file',return_value=Path('/fixture/video.mkv')) as local:
            data=page(self.store,self.app.config['MEDIA_DIR'])
        provider.assert_called_once_with('fixture-show')
        self.assertGreater(local.call_count,0)
        show=next(row['item'] for row in data['items'] if row['item']['id']=='show-read')
        self.assertEqual(show['artwork'],'/fixture-poster.jpg')
        self.assertEqual(show['genre'],'Drama')
        self.assertEqual(show['year'],'2026')
        self.assertEqual(next(row['item'] for row in data['items'] if row['kind']=='youtube')['destination'],'/fixture/video.mkv')
        with self.store.connect() as db:body=json.loads(db.execute("SELECT body FROM discover_requests WHERE id='show-read'").fetchone()[0])
        self.assertGreater(body['posterChecked'],0)
        self.assertEqual(body['image'],'/fixture-poster.jpg')

    def test_cached_history_does_not_contact_provider_but_keeps_checked_local_paths(self):
        self.seed_unenriched_show();before=self.dump()
        with patch('backend.discover_sources.show_media',side_effect=AssertionError('Activity must not contact a provider')) as provider, \
             patch('backend.download_history.local_file',return_value=Path('/fixture/video.mkv')) as local:
            data=page(self.store,self.app.config['MEDIA_DIR'],cached=True)
        provider.assert_not_called();self.assertGreater(local.call_count,0);self.assertEqual(self.dump(),before)
        show=next(row['item'] for row in data['items'] if row['item']['id']=='show-read')
        self.assertEqual(show['artwork'],'');self.assertEqual(show['description'],'Saved overview')
        self.assertTrue(all(row['item']['destination']=='/fixture/video.mkv' for row in data['items'] if row['kind']=='youtube'))

    def test_plain_query_uses_existing_auth_guard_and_read_only_page_mode(self):
        self.seed_unenriched_show()
        origin='http://127.0.0.1:8790'
        route='/api/download-history?limit=15&offset=0&plain=1'
        client=self.app.test_client()
        self.assertEqual(client.get(route,base_url=origin).status_code,401)
        self.store.set('setup_complete','1')
        with self.store.connect() as db:db.execute("INSERT INTO users VALUES(1,'admin','unused')")
        with client.session_transaction(base_url=origin) as session:session['user_id']=1
        before=self.dump()
        with patch('backend.discover_sources.show_media',side_effect=AssertionError('No provider work during export')), \
             patch('backend.download_history.local_file',side_effect=AssertionError('No filesystem resolution during export')):
            response=client.get(route,base_url=origin)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['total'],66)
        self.assertEqual(self.dump(),before)
        self.assertEqual(client.get('/api/download-history?limit=51&plain=1',base_url=origin).status_code,400)
