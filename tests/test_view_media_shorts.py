"""Discover's Shorts switch filters saved YouTube records, not download rules."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from backend.app import create_app
from backend.nzb import stop
from backend.view_media import adapters, local, saved_catalogue

ORIGIN='https://dev.example.com'
CHANNEL='UC'+'a'*22


class ViewMediaShortsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)/'media';self.root.mkdir()
        self.app=create_app({'TESTING':True,'DATA_DIR':self.tmp.name,'MEDIA_DIR':str(self.root)})
        self.store=self.app.extensions['store']
        self.store.set('public_origin',ORIGIN);self.store.set('setup_complete','1')
        self.store.set('download_minimum_video_minutes','0')
        with self.store.connect() as db:
            db.execute("INSERT INTO users VALUES(1,'owner','unused')")
            db.execute('INSERT INTO channels VALUES(?,?,1)',(CHANNEL,json.dumps(dict(id=CHANNEL,name='Main Channel'))))
            for index in range(25):
                self.video(db,'short'+f'{index:06}',f'Short {index:02}',f'2026-10-25T20:{index:02}:00+00:00',kind='shorts',duration_seconds=180)
            for index in range(30):
                extra={} if index==0 else {'kind':None if index==1 else 'live' if index==2 else 'unknown' if index==3 else 'video'}
                self.video(db,'video'+f'{index:06}',f'Video {index:02}',f'2026-10-24T20:{index:02}:00+00:00',duration_seconds=20,**extra)
        self.client=self.app.test_client()
        with self.client.session_transaction(base_url=ORIGIN) as session:
            session['user_id']=1;session['csrf']='token'

    def tearDown(self):
        stop(self.app);local._CACHE.pop(str(self.root),None);self.tmp.cleanup()

    @staticmethod
    def video(db,identity,title,published,**extra):
        data=dict(id=identity,title=title,thumbnail='/artwork/'+'a'*64,description='Saved description',**extra)
        db.execute('INSERT INTO videos VALUES(?,?,?,?,?,0)',(identity,CHANNEL,published[:10],published,json.dumps(data)))

    def get(self,**filters):
        query=urlencode(dict(source='subscriptions',media='youtube',**filters))
        return self.client.get('/api/view-media?'+query,base_url=ORIGIN)

    def ids(self,response):
        self.assertEqual(response.status_code,200,response.json)
        return [item['action']['id'] for item in response.json['items']]

    def pages(self,**filters):
        ids=[];offset=0
        while True:
            response=self.get(offset=offset,**filters);ids.extend(self.ids(response))
            following=response.json['nextOffset']
            if following is None:return ids
            self.assertGreater(following,offset);offset=following

    def test_default_and_explicit_off_filter_before_page_size(self):
        expected=['video'+f'{index:06}' for index in range(29,5,-1)]
        default=self.get()
        self.assertEqual(self.ids(default),expected)
        self.assertEqual(default.json['nextOffset'],24)
        self.assertEqual(self.get(show_shorts='0').json,default.json)

    def test_on_includes_shorts_and_each_mode_has_complete_stable_pages(self):
        regular=['video'+f'{index:06}' for index in range(29,-1,-1)]
        shorts=['short'+f'{index:06}' for index in range(24,-1,-1)]
        self.assertEqual(self.pages(),regular)
        self.assertEqual(self.pages(show_shorts='1'),shorts+regular)
        self.assertEqual(self.ids(self.get(offset=24,show_shorts='1')),[shorts[-1],*regular[:23]])
        self.assertEqual(self.ids(self.get(offset=0,show_shorts='0')),regular[:24],'Changing mode starts from its own first page.')

    def test_missing_null_live_unknown_and_short_duration_are_preserved(self):
        items=self.get(sort='oldest').json['items']
        self.assertEqual([item['action']['id'] for item in items[:5]],['video'+f'{index:06}' for index in range(5)])
        self.assertTrue(all(item['durationSeconds']==20 for item in items))
        item=items[0]
        self.assertEqual(item['id'],'youtube:video000000')
        self.assertEqual(item['action'],dict(type='youtube',id='video000000'))
        self.assertEqual(item['channelId'],CHANNEL)
        self.assertEqual(item['channel'],'Main Channel')
        self.assertEqual(item['description'],'Saved description')
        self.assertEqual(item['kind'],'youtube')
        self.assertEqual(item['image'],'/artwork/'+'a'*64)

    def test_title_order_and_ties_are_stable_across_pages(self):
        with self.store.connect() as db:
            db.execute("UPDATE videos SET data=json_set(data,'$.title','Same title'),published='2026-01-01' WHERE id LIKE 'video%'")
        expected=['video'+f'{index:06}' for index in range(30)]
        for sort in ('new','oldest','title','title_desc'):
            self.assertEqual(self.pages(sort=sort),expected)

    def test_source_search_filters_titles_and_channel_names_before_paging(self):
        self.assertEqual(self.ids(self.get(q='Short')),[])
        self.assertEqual(len(self.pages(q='Short',show_shorts='1')),25)
        self.assertEqual(len(self.pages(q='Main Channel')),30)
        self.assertEqual(self.ids(self.get(q='Video 03')),['video000003'])
        self.assertEqual(self.ids(self.get(q='Video %')),[],'Search wildcards stay literal.')

    def test_strict_filter_validation(self):
        for value in ('','true','false','2','-1','00',' 1','1 '):
            response=self.get(show_shorts=value)
            self.assertEqual(response.status_code,400,(value,response.json))
            self.assertEqual(response.json['error'],'Choose a valid Show Shorts filter.')
        response=self.client.get('/api/view-media?source=subscriptions&show_shorts=0&show_shorts=1',base_url=ORIGIN)
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.get(show_shorts='1').status_code,200)
        self.assertEqual(self.get(show_shorts='0').status_code,200)

    def test_saved_catalogue_enabled_uses_same_authoritative_records(self):
        before=self.get().json
        self.store.set('catalogue_pre_download','true')
        self.assertTrue(saved_catalogue.enabled(self.store))
        with patch.object(saved_catalogue,'browse',side_effect=AssertionError('Subscriptions must use native saved records')):
            self.assertEqual(self.get().json,before)
            self.assertEqual(len(self.pages(show_shorts='1')),55)
            self.assertEqual(len(self.pages(show_shorts='0')),30)

    def test_inactive_channels_are_excluded_in_both_modes(self):
        with self.store.connect() as db:db.execute('UPDATE channels SET active=0 WHERE id=?',(CHANNEL,))
        for enabled in ('false','true'):
            self.store.set('catalogue_pre_download',enabled)
            for show_shorts in ('0','1'):
                response=self.get(show_shorts=show_shorts)
                self.assertEqual(self.ids(response),[])
                self.assertIsNone(response.json['nextOffset'])

    def test_other_sources_ignore_the_unrelated_filter_and_global_search_is_unchanged(self):
        with patch.dict(adapters.REGISTRY,{'local':{**adapters.REGISTRY['local'],'browse':lambda app,store,args:dict(items=[],nextOffset=None,received=args)}}):
            response=self.client.get('/api/view-media?source=local&show_shorts=invalid',base_url=ORIGIN)
        self.assertEqual(response.status_code,200,response.json)
        self.assertNotIn('show_shorts',response.json['received'])
        with patch('backend.discovery_engine.cached_browse',return_value=dict(items=[],nextOffset=None)) as browse:
            response=self.client.get('/api/view-media/search?q=Video&show_shorts=invalid',base_url=ORIGIN)
        self.assertEqual(response.status_code,200,response.json)
        self.assertNotIn('show_shorts',browse.call_args.args[2])

    def test_authentication_and_verified_origin_guards_remain(self):
        path='/api/view-media?source=subscriptions&media=youtube&show_shorts=1'
        anonymous=self.app.test_client().get(path,base_url=ORIGIN)
        self.assertEqual(anonymous.status_code,401)
        wrong_host=self.client.get(path,base_url='https://other.example.com')
        self.assertEqual(wrong_host.status_code,302)
        self.assertEqual(wrong_host.headers['Location'],ORIGIN+'/login')

    def test_channel_upload_endpoint_ignores_this_discover_filter(self):
        with patch('backend.discover_sources.uploads',return_value=dict(items=[dict(id='short000000',kind='shorts')])) as uploads:
            response=self.client.get('/api/youtube-discover/channel/'+CHANNEL+'/uploads?show_shorts=0',base_url=ORIGIN)
        self.assertEqual(response.status_code,200,response.json)
        uploads.assert_called_once_with(CHANNEL)
        self.assertEqual(response.json['items'],[dict(id='short000000',kind='shorts')])

    def test_global_minimum_defaults_to_five_minutes_and_zero_disables_it(self):
        with self.store.connect() as db:db.execute("DELETE FROM settings WHERE key='download_minimum_video_minutes'")
        response=self.get()
        self.assertEqual(response.json['minimumVideoMinutes'],5)
        self.assertEqual(self.ids(response),[])
        self.assertEqual(len(self.pages(show_shorts='1')),55)
        self.store.set('download_minimum_video_minutes','0')
        response=self.get()
        self.assertEqual(response.json['minimumVideoMinutes'],0)
        self.assertEqual(len(self.pages()),30)

    def minimum_fixture(self,entries):
        self.store.set('download_minimum_video_minutes','5')
        with self.store.connect() as db:
            db.execute('DELETE FROM videos')
            for index,entry in enumerate(entries):
                self.video(db,'bound'+f'{index:06}',f'Boundary {index:02}','2026-10-23T20:00:00+00:00',kind='video',**entry)

    def test_minimum_boundary_uses_valid_numeric_seconds_including_strings(self):
        self.minimum_fixture([dict(duration_seconds=value) for value in (299,299.9,300,301,'299','300',' 300 ','300.5')])
        self.assertEqual(self.ids(self.get(sort='title')),['bound'+f'{index:06}' for index in (2,3,5,6,7)])
        self.assertEqual(len(self.pages(show_shorts='1')),8)

    def test_minimum_clock_iso_fallback_matches_display_duration_units(self):
        self.minimum_fixture([dict(duration='4:59'),dict(duration='5:00'),dict(duration='PT5M'),dict(duration='00:05:00'),
            dict(duration='PT4M59S'),dict(duration_seconds='unavailable',duration='5:01'),dict(duration_seconds=299,duration='PT20M')])
        self.assertEqual(self.ids(self.get(sort='title')),['bound'+f'{index:06}' for index in (1,2,3,5)])

    def test_unknown_invalid_and_unavailable_schedule_durations_are_retained(self):
        self.minimum_fixture([{},dict(duration_seconds=None),dict(duration_seconds=True),dict(duration_seconds=0),
            dict(duration_seconds=-1),dict(duration_seconds=1e50),dict(duration_seconds='bad',duration='unavailable'),
            dict(state='upcoming',duration=''),dict(duration_seconds='5',duration='PT10M')])
        self.assertEqual(self.ids(self.get(sort='title')),['bound'+f'{index:06}' for index in range(8)])

    def test_json_booleans_are_unknown_seconds_and_use_valid_clock_fallback(self):
        self.minimum_fixture([dict(duration_seconds=True,duration='4:59'),dict(duration_seconds=False,duration='PT5M'),
            dict(duration_seconds=True),dict(duration_seconds=False),dict(duration_seconds='300'),dict(duration_seconds='299')])
        self.assertEqual(self.ids(self.get(sort='title')),['bound'+f'{index:06}' for index in (1,2,3,4)])

    def test_minimum_filter_runs_before_sort_and_pagination_in_both_catalogue_modes(self):
        self.store.set('download_minimum_video_minutes','5')
        with self.store.connect() as db:
            for index in range(30):
                self.video(db,'large'+f'{index:06}',f'Long video {index:02}',f'2026-10-23T20:{index:02}:00+00:00',duration_seconds=360)
        expected=['large'+f'{index:06}' for index in range(29,-1,-1)]
        for catalogue in ('false','true'):
            self.store.set('catalogue_pre_download',catalogue)
            self.assertEqual(self.pages(),expected)
            self.assertEqual(self.get().json['nextOffset'],24)
            self.assertEqual(len(self.pages(show_shorts='1')),85)
            self.assertEqual(self.ids(self.get(show_shorts='0')),expected[:24])
        self.assertEqual(self.ids(self.get(q='Video 20')),['large000020'])

    def test_minimum_reads_do_not_change_download_settings_or_saved_video_state(self):
        self.minimum_fixture([dict(duration_seconds=34),dict(duration_seconds=89),dict(duration_seconds=300),{}])
        with self.store.connect() as db:
            before_settings=[tuple(row) for row in db.execute('SELECT * FROM settings ORDER BY key')]
            before_videos=[tuple(row) for row in db.execute('SELECT * FROM videos ORDER BY id')]
            before_tables={row['name']:db.execute('SELECT count(*) FROM "'+row['name']+'"').fetchone()[0]
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE '%hold%' OR name LIKE '%job%' OR name IN ('queue','downloads'))")}
        self.assertEqual(self.ids(self.get()),['bound000002','bound000003'])
        self.assertEqual(len(self.pages(show_shorts='1')),4)
        with self.store.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM settings ORDER BY key')],before_settings)
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM videos ORDER BY id')],before_videos)
            self.assertEqual({table:db.execute('SELECT count(*) FROM "'+table+'"').fetchone()[0] for table in before_tables},before_tables)


if __name__=='__main__':unittest.main()
