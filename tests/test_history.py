import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit, urlencode
from unittest.mock import patch
from backend.app import create_app
from backend.catalogue import Importer
from backend.history import seed, batch, coverage

class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.calls=[]
        self.current=datetime.now(timezone.utc)
        self.provider_failed=False
        self.repeat=False
        self.old=False
        self.app=create_app({'TESTING':True,'DATA_DIR':self.tmp.name,'GOOGLE_REQUEST':self.google})
        fallback=patch('backend.youtube_fallback.api',side_effect=lambda importer,resource,**params:self.google('public','/youtube/v3/'+resource+'?'+urlencode(params)))
        fallback.start();self.addCleanup(fallback.stop)
        self.importer=Importer(self.app)
        self.store=self.importer.store
        self.store.set('google_tokens',json.dumps({'access_token':'test','expires_in':3600,'received_at':time.time()}))
        self.store.set('next_sync',time.time()+1800)
        self.channel={'id':'channel','playlist':'uploads','name':'Channel'}
        with self.store.connect() as db:
            db.execute('INSERT INTO channels VALUES (?,?,1)',('channel',json.dumps(self.channel)))
            db.execute("INSERT INTO download_rules(channel,videos,range_days) VALUES('channel',1,365)")
        seed(self.store,self.channel,{'nextPageToken':'page2'})

    def tearDown(self):
        self.tmp.cleanup()

    def google(self,host,path,**kwargs):
        args=parse_qs(urlsplit(path).query)
        self.calls.append(path)
        if '/playlistItems?' in path:
            page=args.get('pageToken',[''])[0]
            index=2 if page=='page2' else 3
            stamp=self.current-timedelta(days=400 if self.old else 100*index)
            result={'items':[{'contentDetails':{'videoId':f'history{index:04}','videoPublishedAt':stamp.isoformat()}}]}
            if index==2:result['nextPageToken']='page2' if self.repeat else 'page3'
            if self.old:result['nextPageToken']='evenolder'
            return result
        if self.provider_failed:raise ValueError('Provider unavailable')
        stamp=self.current-timedelta(days=400 if self.old else 200)
        return {'items':[{'id':args['id'][0],'snippet':{'title':'Older upload','channelId':'channel','publishedAt':stamp.isoformat()},'contentDetails':{'duration':'PT10M'}}]}

    def job(self):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM history_jobs WHERE channel=?',('channel',)).fetchone())

    def test_checkpoint_survives_restart_and_recent_reseed(self):
        self.assertTrue(batch(self.importer,limit=1))
        self.assertEqual(self.job()['cursor'],'page3')
        seed(self.store,self.channel,{'nextPageToken':'newfirstpage'})
        self.assertEqual(self.job()['cursor'],'page3')
        restarted=Importer(self.app)
        self.assertTrue(batch(restarted,limit=1))
        self.assertEqual(self.job()['state'],'complete')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM videos').fetchone()[0],2)
        label=coverage(self.store,[dict(self.channel)])[0]['coverage']
        self.assertIn('selected range',label)
        self.assertIn('2 saved videos',label)

    def test_failure_does_not_advance_cursor(self):
        self.provider_failed=True
        self.assertFalse(batch(self.importer,limit=1))
        self.assertEqual(self.job()['cursor'],'page2')
        self.assertEqual(self.job()['failures'],1)
        self.assertIn('Provider unavailable',self.job()['error'])

    def test_cached_browse_metadata_is_promoted_by_actual_normal_history_observation(self):
        stamp=(self.current-timedelta(days=200)).isoformat()
        self.importer.save_videos([{'id':'history0002','snippet':{'title':'Keep rich metadata','channelId':'channel','publishedAt':stamp},'contentDetails':{'duration':'PT10M'},'statistics':{'viewCount':'1234'}}])
        with self.store.connect() as db:
            db.execute("UPDATE videos SET data=json_set(data,'$.browse_only',1,'$.forecast_eligible',json('false')) WHERE id='history0002'")
            before=dict(db.execute("SELECT * FROM videos WHERE id='history0002'").fetchone())
        self.assertTrue(batch(self.importer,limit=1))
        with self.store.connect() as db:after=dict(db.execute("SELECT * FROM videos WHERE id='history0002'").fetchone())
        before_data=json.loads(before.pop('data'));after_data=json.loads(after.pop('data'))
        self.assertEqual(before,after,'normal observation does not rewrite indexed metadata/dates')
        self.assertTrue(before_data.pop('browse_only'))
        self.assertFalse(before_data.pop('forecast_eligible'))
        self.assertIs(after_data.pop('forecast_eligible'),True,'published normal history observation restores baseline forecasting')
        self.assertEqual(before_data,after_data,'only the browsing provenance marker is removed')
        self.assertEqual(len(self.calls),1,'rich cache hit must not refetch video details')
        self.assertIn('/playlistItems?',self.calls[0])
        self.assertEqual(self.job()['cursor'],'page3','ordinary history progress is retained')

    def test_history_browse_promotion_rechecks_active_membership_pause_and_reset(self):
        stamp=(self.current-timedelta(days=200)).isoformat()
        self.importer.save_videos([{'id':'history0002','snippet':{'title':'Keep saved facts','channelId':'channel','publishedAt':stamp},'contentDetails':{'duration':'PT10M'}}])
        from backend import history
        for guard in ['youtube_sync_paused','reset_in_progress','inactive']:
            with self.subTest(guard=guard):
                self.store.set('youtube_sync_paused','false');self.store.set('reset_in_progress','false')
                with self.store.connect() as db:
                    db.execute("UPDATE channels SET active=1 WHERE id='channel'")
                    db.execute("UPDATE history_jobs SET state='indexing',cursor='page2',trail='[]'")
                    db.execute("UPDATE videos SET data=json_set(data,'$.browse_only',1) WHERE id='history0002'")
                def listing(*args,**kwargs):
                    if guard=='inactive':
                        with self.store.connect() as db:db.execute("UPDATE channels SET active=0 WHERE id='channel'")
                    else:self.store.set(guard,'true')
                    return {'items':[{'contentDetails':{'videoId':'history0002','videoPublishedAt':stamp}}]}
                with patch.object(history,'request',side_effect=listing):batch(self.importer,limit=1)
                with self.store.connect() as db:item=json.loads(db.execute("SELECT data FROM videos WHERE id='history0002'").fetchone()[0])
                self.assertTrue(item['browse_only'],'a stale response cannot promote after '+guard)

    def test_repeated_cursor_resets_after_three_retries(self):
        self.repeat=True
        for _ in range(3):
            with self.store.connect() as db:db.execute('UPDATE history_jobs SET updated=0')
            batch(self.importer,limit=1)
        self.assertEqual(self.job()['cursor'],'')
        self.assertEqual(self.job()['trail'],'[]')

    def test_page_older_than_target_stops_indexing(self):
        self.old=True
        batch(self.importer,limit=1)
        self.assertEqual(self.job()['state'],'complete')
        self.assertEqual(len(self.calls),2)

    def test_manual_refresh_takes_priority(self):
        self.store.set('sync_requested','1')
        self.assertFalse(batch(self.importer))
        self.assertEqual(self.calls,[])
        self.assertEqual(self.job()['cursor'],'page2')

    def test_complete_history_revalidates_after_twenty_days(self):
        batch(self.importer,limit=2)
        with self.store.connect() as db:
            db.execute('UPDATE history_jobs SET updated=?',(time.time()-21*86400,))
        seed(self.store,self.channel,{'nextPageToken':'freshpage'})
        self.assertEqual(self.job()['state'],'indexing')
        self.assertEqual(self.job()['cursor'],'freshpage')

    def test_history_counter_does_not_stop_provider_calls(self):
        from zoneinfo import ZoneInfo
        day=datetime.now(ZoneInfo('America/Los_Angeles')).date().isoformat()
        self.store.set('history_quota_day',day)
        self.store.set('history_quota_used','1500')
        batch(self.importer,limit=1)
        self.assertTrue(self.calls)
        self.assertEqual(self.job()['failures'],0)
        self.assertGreater(int(self.store.get('history_quota_used')),1500)

    def test_channels_take_turns(self):
        other={'id':'other','playlist':'otheruploads','name':'Other'}
        with self.store.connect() as db:db.execute('INSERT INTO channels VALUES (?,?,1)',('other',json.dumps(other)))
        with self.store.connect() as db:db.execute("INSERT INTO download_rules(channel,videos,range_days) VALUES('other',1,365)")
        seed(self.store,other,{'nextPageToken':'page2'})
        batch(self.importer,limit=2)
        playlist_calls=[parse_qs(urlsplit(p).query)['playlistId'][0] for p in self.calls if '/playlistItems?' in p]
        self.assertEqual(playlist_calls,['uploads','otheruploads'])
