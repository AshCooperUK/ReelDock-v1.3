"""Activity parent links retain provider identity without background lookups."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

from backend.app import create_app
from backend import activity_media,download_history
from backend.downloaders.torrent.metadata import _lookup_card

ORIGIN='https://activity.example.test'
SHOW=dict(id='show:81580',kind='show',externalId='81580',title='Come Dine with Me',year='2005')
MOVIE=dict(id='movie:1234567',kind='movie',externalId='1234567',title='Exact movie')


class ActivityMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.media=Path(self.tmp.name)/'media';self.media.mkdir()
        self.app=create_app(dict(TESTING=True,DATA_DIR=self.tmp.name,MEDIA_DIR=str(self.media)))
        self.store=self.app.extensions['store'];self.store.set('public_origin',ORIGIN);self.store.set('setup_complete','1')
        with self.store.connect() as db:db.execute("INSERT INTO users VALUES(1,'owner','unused')")
        self.client=self.app.test_client()
        with self.client.session_transaction(base_url=ORIGIN) as session:session.update(user_id=1,csrf='token')

    def request(self,identity,item=SHOW,engine='nzb',job='job-one',kind='episode',state='downloading',updated=10):
        body=dict(item=item,episode=dict(id=999,season=2026,number=9,name='Episode only')) if item is not None else dict(itemId='show:81580',externalId='81580')
        with self.store.connect() as db:
            db.execute('''INSERT INTO discover_requests(id,idempotency,kind,title,state,quality,fallback,body,engine,engine_id,updated,created)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(identity,identity,kind,'Episode filename S2026E09',state,'1080',0,json.dumps(body),engine,job,updated,updated))
            return dict(db.execute('SELECT * FROM discover_requests WHERE id=?',(identity,)).fetchone())

    def policy(self,identity,owner,engine='nzb'):
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS show_download_policy(engine TEXT,job TEXT,body TEXT,PRIMARY KEY(engine,job))')
            db.execute('INSERT OR REPLACE INTO show_download_policy VALUES(?,?,?)',(engine,identity,json.dumps(dict(requestId=owner))))

    def test_saved_episode_parent_is_exact_show_not_episode_or_filename(self):
        self.request('request-one')
        before=dict(id='job-one',name='Misleading filename',state='downloading',activity_generation=7,activity_observation=dict(token='unchanged'))
        result=activity_media.native_items(self.store,'nzb',[before])[0]
        self.assertEqual(result['mediaItem']['id'],'show:81580')
        self.assertEqual(result['mediaItem']['title'],'Come Dine with Me')
        self.assertEqual(result['mediaItem']['externalId'],'81580')
        self.assertNotIn('episode',result['mediaItem'])
        self.assertEqual({key:result[key] for key in before},before)
        self.assertNotIn('mediaItem',before,'Native engine DTO was not mutated.')

    def test_unknown_or_mismatched_parent_has_no_link(self):
        for item in (None,dict(kind='show',title='No identity'),dict(id='show:0',kind='show'),
                     dict(id='episode:999',kind='show'),dict(SHOW,externalId='99999'),MOVIE):
            with self.subTest(item=item):
                row=dict(kind='episode',body=json.dumps(dict(item=item)))
                self.assertIsNone(activity_media.request_item(row))
        self.request('unknown',None)
        self.assertNotIn('mediaItem',activity_media.native_items(self.store,'nzb',[dict(id='job-one')])[0])

    def test_movie_and_torrent_binding_are_source_and_job_specific(self):
        self.request('torrent',MOVIE,engine='torrent',job='torrent-one',kind='movie')
        rows=activity_media.native_items(self.store,'torrent',[dict(id='torrent-one'),dict(id='other-job')])
        self.assertEqual(rows[0]['mediaItem']['id'],MOVIE['id'])
        self.assertNotIn('mediaItem',rows[1])
        self.assertNotIn('mediaItem',activity_media.native_items(self.store,'nzb',[dict(id='torrent-one')])[0])

    def test_policy_owner_must_match_the_request_native_job_fence(self):
        self.request('old',SHOW,job='old-job',state='failed')
        replacement=dict(SHOW,title='Current saved parent')
        self.request('new',replacement,job='new-job',updated=20)
        self.policy('old-job','old');self.policy('new-job','new')
        rows=activity_media.native_items(self.store,'nzb',[dict(id='old-job'),dict(id='new-job')])
        self.assertEqual(rows[0]['mediaItem']['title'],'Come Dine with Me')
        self.assertEqual(rows[1]['mediaItem']['title'],'Current saved parent')
        self.policy('new-job','old')
        self.assertNotIn('mediaItem',activity_media.native_items(self.store,'nzb',[dict(id='new-job')])[0])

    def test_ambiguous_parent_identities_are_omitted_until_policy_selects_owner(self):
        self.request('one',SHOW)
        self.request('two',dict(SHOW,id='show:123',externalId='123'),updated=20)
        self.assertNotIn('mediaItem',activity_media.native_items(self.store,'nzb',[dict(id='job-one')])[0])
        self.policy('job-one','one')
        self.assertEqual(activity_media.native_items(self.store,'nzb',[dict(id='job-one')])[0]['mediaItem']['id'],SHOW['id'])

    def test_tmdb_and_tvdb_show_namespaces_never_mix(self):
        item=activity_media.tmdb_item(123,'tv',dict(title='TMDB show',year='2026'))
        self.assertEqual(item['id'],'tmdb:show:123');self.assertNotIn('externalId',item)
        self.assertEqual(activity_media.parent_item(dict(item,externalId='123'))['id'],'tmdb:show:123')
        self.assertNotIn('externalId',activity_media.parent_item(dict(item,externalId='123')))
        self.assertEqual(activity_media.parent_item(SHOW)['externalId'],'81580')
        self.assertIsNone(activity_media.tmdb_item(True,'tv',{}))
        self.assertIsNone(activity_media.tmdb_item(0,'tv',{}))

    def test_saved_imdb_namespaces_preserve_leading_zero_identity(self):
        for kind in ('movie','show'):
            item=activity_media.parent_item(dict(id='imdb:'+kind+':0120737',kind=kind,externalId='0120737',title='Exact IMDb parent'))
            self.assertEqual(item['id'],'imdb:'+kind+':0120737')
            if kind=='show':self.assertNotIn('externalId',item,'IMDb show digits are not a TVDB ID.')
        self.assertIsNone(activity_media.parent_item(dict(id='imdb:movie:000',kind='movie')))
        self.assertIsNone(activity_media.parent_item(dict(id='imdb:movie:0120737',kind='movie',externalId='9999')))

    def test_parent_public_allowlist_excludes_unrelated_urls_and_video_fields(self):
        parent=activity_media.parent_item(dict(SHOW,url='https://www.youtube.com/watch?v=fixture&token=PRIVATE',
            videoId='abcdefghijk',channelId='UC'+'a'*22,channel='Unrelated channel',views=100,releaseDate='2027-01-01'))
        self.assertNotIn('PRIVATE',json.dumps(parent))
        self.assertTrue(set(parent).isdisjoint({'url','videoId','channelId','channel','views'}))
        self.assertEqual(parent['releaseDate'],'2027-01-01')

    def test_authoritative_parent_also_owns_lookup_presentation_when_filename_match_differs(self):
        self.request('known',dict(SHOW,description='Saved show summary',genre='Food, Reality',rating='7.2'))
        wrong=dict(matched=True,title='Different film',overview='Wrong overview',poster='wrong',cast=['Wrong cast'],media_type='movie',mediaItem=dict(id='tmdb:movie:123',kind='movie'))
        result=activity_media.lookup_item(self.store,'nzb','job-one',wrong)
        self.assertEqual(result['mediaItem']['id'],SHOW['id'])
        self.assertEqual(result['title'],SHOW['title'])
        self.assertEqual(result['overview'],'Saved show summary')
        self.assertEqual(result['genres'],['Food','Reality'])
        self.assertEqual(result['rating'],7.2)
        self.assertEqual(result['media_type'],'tv')
        self.assertFalse(result['matched'])
        self.assertNotIn('Wrong',json.dumps(result))

    def test_lookup_keeps_exact_found_or_manually_selected_tmdb_parent(self):
        class Client:
            def get(self,path,**params):
                if path=='search/tv':return dict(results=[dict(id=123,name='Exact show',first_air_date='2005-01-01')])
                if path in ('tv/123','tv/456'):return dict(id=int(path.split('/')[1]),name='Exact show',first_air_date='2005-01-01',overview='Known overview')
                return {}
        card=_lookup_card(self.store,dict(name='Exact show',season=2026),'series',Client())
        self.assertEqual(card['mediaItem']['id'],'tmdb:show:123')
        self.assertNotIn('externalId',card['mediaItem'])
        selected=_lookup_card(self.store,dict(name='Wrong filename',season=2026),'series',Client(),dict(mode='tmdb',media_type='tv',tmdb_id=456))
        self.assertEqual(selected['mediaItem']['id'],'tmdb:show:456')
        manual=_lookup_card(self.store,dict(name='Exact show'),'series',Client(),dict(mode='manual',data=dict(title='User details')))
        self.assertTrue(manual['matched']);self.assertNotIn('mediaItem',manual)
        missing=_lookup_card(self.store,dict(name='Exact show'),'series')
        self.assertFalse(missing['matched']);self.assertNotIn('mediaItem',missing)
        known=_lookup_card(self.store,dict(name='Wrong filename'),'series',choice=dict(mode='tmdb',media_type='tv',tmdb_id=456,data=dict(title='Saved manual choice')))
        self.assertFalse(known['matched'],'A saved identity does not claim refreshed metadata.')
        self.assertEqual(known['mediaItem']['id'],'tmdb:show:456')
        self.assertEqual(known['mediaItem']['title'],'Saved manual choice')

    def test_mismatched_provider_detail_id_does_not_invent_link(self):
        client=Mock();client.get.side_effect=[dict(results=[dict(id=123,name='Exact show')]),dict(id=456,name='Exact show')]
        card=_lookup_card(self.store,dict(name='Exact show'),'series',client)
        self.assertTrue(card['matched']);self.assertNotIn('mediaItem',card)

    def test_discover_history_and_active_rows_export_saved_parent_without_mutation(self):
        self.request('history',SHOW)
        def snapshot():
            with self.store.connect() as db:
                tables=[row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                return {table:[tuple(row) for row in db.execute('SELECT * FROM "'+table+'" ORDER BY rowid')] for table in tables}
        before=snapshot()
        with patch('backend.discover_sources.show_media',side_effect=AssertionError('No provider during plain export')):
            page=download_history.page(self.store,str(self.media),plain=True,include_archived=True)
        self.assertEqual(page['items'][0]['item']['mediaItem']['id'],SHOW['id'])
        self.assertEqual(page['active'][0]['item']['mediaItem']['id'],SHOW['id'])
        self.assertEqual(download_history._active_row(self.store,'discover','history')['item']['mediaItem']['id'],SHOW['id'])
        self.assertEqual(snapshot(),before)

    def test_native_get_list_uses_existing_engine_and_no_metadata_lookup_per_row(self):
        self.request('native',SHOW)
        native=Mock();native.jobs.return_value=[dict(id='job-one',name='Source file',state='downloading')]
        with patch('backend.nzb.download_base',return_value=str(self.media)),patch('backend.nzb._engine',return_value=native) as engine,patch('backend.downloaders.torrent.metadata.preview',side_effect=AssertionError('No lookup per row')):
            response=self.client.get('/api/nzb',base_url=ORIGIN)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['nzbs'][0]['mediaItem']['id'],SHOW['id'])
        engine.assert_called_once();native.jobs.assert_called_once()
        self.assertEqual(self.app.test_client().get('/api/nzb',base_url=ORIGIN).status_code,401)


if __name__=='__main__':unittest.main()
