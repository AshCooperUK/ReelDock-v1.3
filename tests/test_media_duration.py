"""Explicit length units survive saved-source DTOs without per-card probes."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_view_media as fixture
from backend import discover_sources, m3u, m3u_catalogue, media_duration, xtream
from backend.media_management import service, store as records
from backend.view_media import adapters, local, nzb_metadata


class DurationUnitsTests(unittest.TestCase):
    def test_card_dto_bounds_reject_bad_units_without_losing_known_clock_or_pending_state(self):
        item=dict(id='youtube:abcdefghijk',kind='video',title='Clip',duration='12:03',metadata_pending=True)
        for value in (True,-1,float('nan'),float('inf'),31536001):
            card=adapters.card(dict(item,durationSeconds=value,runtimeMinutes=value),dict(type='youtube',id='abcdefghijk'))
            self.assertNotIn('durationSeconds',card)
            self.assertNotIn('runtimeMinutes',card)
            self.assertEqual(card['duration'],'12:03')
            self.assertTrue(card['metadata_pending'])

    def test_explicit_seconds_minutes_clocks_and_iso_have_no_ambiguous_numeric_guess(self):
        self.assertEqual(media_duration.fields(seconds='125',minutes='42',clock='PT1H2M3S'),dict(durationSeconds=125,runtimeMinutes=42,duration='PT1H2M3S'))
        for clock,seconds in [('12:03',723),('1:02:03',3723),('PT2H5M',7500),('PT42.5S',42.5)]:
            self.assertEqual(media_duration.clock_seconds(clock),seconds)
        for value in (None,True,False,0,-1,float('nan'),float('inf'),'Infinity','-3','PT','12:90','125','9'*100):
            with self.subTest(value=value):self.assertIsNone(media_duration.clock_seconds(value))
        for value in (True,0,-1,float('nan'),float('inf'),31536001,'99999999999999999999999999999999999999'):
            self.assertEqual(media_duration.fields(seconds=value),{})
        for value in (True,-1,float('nan'),525601):self.assertEqual(media_duration.fields(minutes=value),{})
        self.assertEqual(media_duration.fields(clock=125),{})
        self.assertEqual(media_duration.fields(pending=True),dict(metadata_pending=True))

    def test_xtream_units_are_explicit_not_an_ambiguous_duration_number(self):
        self.assertEqual(xtream.duration_seconds(dict(duration_secs='2550')),2550)
        self.assertEqual(xtream.duration_seconds(dict(duration='00:42:30')),2550)
        self.assertEqual(xtream.duration_seconds(dict(duration='2550')),0)
        self.assertEqual(xtream.duration_seconds(dict(duration_secs='NaN')),0)


class SavedDurationTests(unittest.TestCase):
    setUp=fixture.ViewMediaTests.setUp
    tearDown=fixture.ViewMediaTests.tearDown

    def video(self):
        channel='UC'+'a'*22;identity='abcdefghijk'
        video=dict(id=identity,title='Saved exact video',channel=channel,state='published',date='2026-10-01',duration_seconds=125)
        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES(?,?,1)',(channel,json.dumps(dict(id=channel,name='Channel'))))
            db.execute('INSERT INTO videos VALUES(?,?,?,?,?,0)',(identity,channel,video['date'],'2026-10-01T12:00:00Z',json.dumps(video)))
        return video

    def test_saved_youtube_seconds_reach_both_browse_dtos_without_network_or_writes(self):
        # This checks duration transport, independently of the saved five-minute
        # Discover display filter which would deliberately hide this 125s clip.
        self.store.set('download_minimum_video_minutes','0')
        video=self.video()
        with self.store.connect() as db:before='\n'.join(db.iterdump())
        with patch('backend.discover_sources.fetch',side_effect=AssertionError('No provider request')),patch('subprocess.run',side_effect=AssertionError('No per-card probe')):
            cards=adapters.subscriptions(self.app,self.store,dict(q='',sort='new',offset=0))['items']
            public=discover_sources.subscription_uploads(self.store)['items']
        self.assertEqual(cards[0]['durationSeconds'],125)
        self.assertEqual(public[0]['durationSeconds'],125)
        self.assertEqual(cards[0]['action']['id'],video['id'])
        with self.store.connect() as db:self.assertEqual('\n'.join(db.iterdump()),before)

    def test_nzb_movie_enrichment_keeps_known_runtime_and_original_release_identity(self):
        self.store.set('m3u_tmdb_token','synthetic token')
        item=dict(id='movie:1234567',externalId='1234567',kind='movie',title='Film',releases=[dict(id='release')])
        with patch('backend.view_media.nzb_metadata.TMDB',return_value=Mock(entity=Mock(return_value=dict(id=42,title='Film',runtime=125)))) as client:
            nzb_metadata.movies(self.store,[item])
        self.assertEqual(item['runtimeMinutes'],125)
        self.assertEqual(item['id'],'movie:1234567')
        self.assertEqual(item['releases'],[dict(id='release')])
        self.assertEqual(client.return_value.entity.call_count,1,'runtime uses the existing metadata response')

    def test_episode_runtime_is_specific_to_episode_not_parent_series(self):
        item=dict(id='show:42',externalId='42',kind='show',title='A show',runtimeMinutes=99)
        for number,runtime,expected in [(1,42,42),(2,None,None),(3,float('nan'),None)]:
            key='episode:42:1:'+str(number)
            records.remember(self.store,key,item['id'],'episode',dict(item=item,episode=dict(name='Episode',season=1,number=number,runtime=runtime),date='2026-10-01'))
            with patch('subprocess.run',side_effect=AssertionError('No duration probe')):
                row=service.compose(self.app,self.store,records.row(self.store,key),probe=False)
            self.assertEqual(row.get('runtimeMinutes'),expected)
            self.assertNotIn('durationSeconds',row)

    def import_playlist(self,text):
        source=m3u.save(self.store,dict(name='Duration source',url='https://example.invalid/playlist',existing_library=True))
        m3u.request_sync(self.store,source)
        m3u.tick(self.store,self.root,fetch=lambda url:text)
        m3u_catalogue.index(self.store)
        return source

    def test_positive_extinf_seconds_survive_import_and_individual_episode_dto(self):
        text='#EXTM3U\n#EXTINF:2550 group-title="TV VOD",A show S01E01\nhttps://example.invalid/1.mp4\n#EXTINF:0 group-title="TV VOD",A show S01E02\nhttps://example.invalid/2.mp4\n'
        source=self.import_playlist(text)
        with self.store.connect() as db:db.execute('UPDATE m3u_catalog_titles SET data=?',(json.dumps(dict(runtimeMinutes=99)),))
        with patch('backend.m3u_player.nfo_data',side_effect=AssertionError('No per-card NFO read')),patch('backend.m3u_metadata.TMDB.get',side_effect=AssertionError('No provider lookup')),patch('subprocess.run',side_effect=AssertionError('No per-card ffprobe')):
            page=adapters.vod(self.app,self.store,dict(collection=source,media='shows',q='',offset=0,sort='title'))
        self.assertEqual(len(page['items']),2)
        first,second=page['items']
        self.assertEqual(first['kind'],'show','the shared UI must distinguish this single VOD episode from a series parent')
        self.assertEqual(first['action']['item']['kind'],'series')
        self.assertEqual(first['durationSeconds'],2550)
        self.assertNotIn('durationSeconds',second)
        self.assertNotIn('runtimeMinutes',second,'a parent series runtime is never used as an individual episode length')
        with self.store.connect() as db:
            saved=[json.loads(row[0]) for row in db.execute("SELECT i.data FROM m3u_file_info i JOIN m3u_files f ON f.source=i.source AND f.path=i.path WHERE i.source=? AND f.kind='series' ORDER BY i.path",(source,))]
        self.assertEqual(saved[0]['durationSeconds'],2550)
        self.assertNotIn('durationSeconds',saved[1])

    def test_xtream_existing_import_calls_keep_episode_duration_without_extra_queries(self):
        from test_xtream import API,Provider
        api=copy.deepcopy(API)
        api['get_vod_streams'][0]['duration_secs']='5400'
        api['get_series_info']['episodes']['1'][0]['info']['duration']='00:42:30'
        source=xtream.save(self.store,dict(name='Duration provider',server='https://tv.example.invalid',username='user',password='p@ss/word'))
        provider=Provider(api)
        text=xtream.playlist(self.store,source,fetch=provider)
        entries,_=m3u.parse(text,dict(m3u.DEFAULTS))
        self.assertEqual(next(row for row in entries if row['kind']=='movies')['durationSeconds'],5400)
        episodes=[row for row in entries if row['kind']=='series']
        self.assertEqual(episodes[0]['durationSeconds'],2550)
        self.assertNotIn('durationSeconds',episodes[1])
        self.assertNotIn('get_vod_info',provider.calls,'no new API request is added to an import or card read')

    def test_local_nfo_and_exact_completed_youtube_cache_are_used_without_probing(self):
        video=self.video()
        known=self.root/'Movies'/'Film'/'Film.mkv';known.parent.mkdir(parents=True);known.write_bytes(b'media')
        known.with_suffix('.nfo').write_text('<movie><title>Film</title><runtime>93</runtime></movie>')
        saved=self.root/'YouTube Downloads'/'Channel'/'Saved.mp4';saved.parent.mkdir(parents=True);saved.write_bytes(b'saved')
        unknown=self.root/'Movies'/'Unknown.mp4';unknown.write_bytes(b'unknown')
        with self.store.connect() as db:
            db.execute('INSERT INTO media_files(id,channel,path,day,episode,profile,bytes) VALUES(?,?,?,?,?,?,?)',(video['id'],video['channel'],str(saved.relative_to(self.root)),'2026-10-01',1,'emby',5))
            db.execute("INSERT INTO downloads(id,title,quality,state,created,updated) VALUES(?,?,?,'complete',0,0)",(video['id'],video['title'],'720'))
        with patch('subprocess.run',side_effect=AssertionError('Inventory never probes a file')):
            state={};local._scan(self.root,state)
            rows=local.library_dates(self.store,state,self.root)
        by_title={row['title']:row for row in rows}
        self.assertEqual(by_title['Film']['runtimeMinutes'],93)
        self.assertEqual(by_title['Saved']['durationSeconds'],125)
        self.assertNotIn('runtimeMinutes',by_title['Unknown'])
        self.assertNotIn('durationSeconds',by_title['Unknown'],'file size never becomes an estimated runtime')
        self.assertEqual(saved.read_bytes(),b'saved')


if __name__=='__main__':unittest.main()
