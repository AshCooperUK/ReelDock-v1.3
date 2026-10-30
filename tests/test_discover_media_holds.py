"""Cleared exact requests cannot restart, fall back or publish before manual release."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import test_dashboard as dashboard
from backend import discover_store as ds, discover_acquire as acquire, media_holds, activity_items


class DiscoverHoldTests(unittest.TestCase):
    def setUp(self):
        self.fixture=dashboard.DashboardTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.store=self.fixture.store;self.app=self.fixture.app
        self.payload=dict(idempotency='exact-request-identity',kind='movie',title='Same display title',quality='1080',
            fallback=False,episode_key='movie:12345',body=dict(externalId='12345',source='NZB first'))
        self.row,_=ds.insert_request(self.store,self.payload)

    def hold(self,keys=None):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            media_holds.hold(db,keys or media_holds.request_keys(self.row),[('discover',self.row['id'])])

    def test_request_only_hold_blocks_stale_search_claim_commit_update_and_retry(self):
        generation=ds.claim_start(self.store,self.row['id']);self.assertIsNotNone(generation)
        self.hold(['request:'+self.row['id']])
        self.assertFalse(ds.fence_start(self.store,self.row['id'],generation))
        self.assertFalse(ds.commit_engine(self.store,self.row['id'],generation,'nzb','native-one','Queued'))
        ds.update_request(self.store,self.row['id'],state='searching')
        with patch.object(acquire,'search_for',side_effect=AssertionError('provider called')):
            acquire.process(self.app,self.store,dict(self.row,state='searching'))
        self.assertFalse(acquire._retry_candidate(self.store,self.row,'Incomplete articles'))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT state FROM discover_requests').fetchone()[0],'starting')

    def test_new_automatic_and_private_requests_do_not_release_exact_hold(self):
        self.hold()
        row,created=ds.insert_request(self.store,dict(self.payload,idempotency='new-auto-id',automatic=True))
        self.assertFalse(created);self.assertEqual(row['id'],self.row['id'])
        with self.assertRaisesRegex(ValueError,'cleared'):
            ds.insert_request(self.store,dict(self.payload,idempotency='new-private-id'))
        self.assertTrue(media_holds.blocked(self.store,'managed:movie:12345'))
        other,created=ds.insert_request(self.store,dict(self.payload,idempotency='unrelated-id',episode_key='movie:67890'))
        self.assertTrue(created);self.assertIsNotNone(ds.claim_start(self.store,other['id']))

    def test_waiting_legacy_request_only_hold_never_reaches_provider(self):
        ds.update_request(self.store,self.row['id'],state='waiting')
        with self.store.connect() as db:db.execute('UPDATE discover_requests SET updated=1 WHERE id=?',(self.row['id'],))
        self.hold(['request:'+self.row['id']])
        with patch.object(acquire,'process',side_effect=AssertionError('held search restarted')):
            acquire.resume_waiting(self.app,self.store)

    def test_explicit_validated_movie_download_releases_only_selected_group(self):
        ds.cancel_request(self.store,self.row['id']);self.hold()
        ds.update_request(self.store,self.row['id'],state='cancelled')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');media_holds.hold(db,['managed:movie:67890'],[])
        result=acquire.queue_manual(self.store,dict(kind='movie',externalId='12345',title='Same display title'),[],
            '1080',False,'NZB first','manual-selected-download')
        self.assertEqual(result[0]['state'],'searching')
        self.assertFalse(media_holds.blocked(self.store,'managed:movie:12345'))
        self.assertTrue(media_holds.blocked(self.store,'managed:movie:67890'))

    def test_clear_before_verified_binding_rejects_all_new_downstream_identities(self):
        generation=ds.claim_start(self.store,self.row['id'])
        activity_items.clear_one(self.app,self.store,'discover',self.row['id'])
        for key in ('native:nzb:'+'a'*64,media_holds.vod_key('fixture','exact.strm'),'video:abcdefghijk'):
            self.assertFalse(media_holds.bind_request(self.store,self.row['id'],generation,[key]))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM media_request_keys').fetchone()[0],0)
        self.assertFalse(ds.commit_engine(self.store,self.row['id'],generation,'nzb','not-started','Queued'))

    def test_binding_before_clear_fences_content_even_before_child_job_exists(self):
        generation=ds.claim_start(self.store,self.row['id'])
        keys=['native:nzb:'+'a'*64,media_holds.vod_key('fixture','exact.strm'),'video:abcdefghijk']
        self.assertTrue(media_holds.bind_request(self.store,self.row['id'],generation,keys))
        activity_items.clear_one(self.app,self.store,'discover',self.row['id'])
        for key in keys:self.assertTrue(media_holds.blocked(self.store,key),key)
        self.assertFalse(ds.commit_engine(self.store,self.row['id'],generation,'nzb','late-job','Queued'))

    def test_clear_bound_child_before_engine_commit_also_fences_parent_and_other_release(self):
        for kind in ('nzb','m3u','youtube'):
            with self.subTest(kind=kind):
                row,_=ds.insert_request(self.store,dict(self.payload,idempotency='binding-'+kind,episode_key='movie:'+kind))
                generation=ds.claim_start(self.store,row['id'])
                if kind=='nzb':
                    identity='b'*32;key='native:nzb:'+'c'*64
                    class Engine:
                        removed=False
                        def jobs(self):return [] if self.removed else [dict(id=identity,name='Exact bound release',state='queued',progress=0,added=1)]
                        def activity_key(self,target):return key
                        def remove(self,target,timeout=0.5):self.removed=True
                    self.app.extensions['nzb_engine']=Engine()
                elif kind=='m3u':
                    identity='bound-vod';key=media_holds.vod_key('fixture','exact.strm')
                    with self.store.connect() as db:
                        db.execute("INSERT INTO m3u_downloads(id,source,path,title,kind,state,created,updated) VALUES(?,'fixture','exact.strm','Exact bound release','movies','queued',1,1)",(identity,))
                else:
                    identity='abcdefghijk';key='video:'+identity
                    with self.store.connect() as db:
                        db.execute("INSERT INTO downloads(id,title,quality,state,created,updated) VALUES(?,'Exact bound release','720','queued',1,1)",(identity,))
                self.assertTrue(media_holds.bind_request(self.store,row['id'],generation,[key]))
                activity_items.clear_one(self.app,self.store,kind,identity)
                self.assertTrue(media_holds.blocked(self.store,'request:'+row['id']))
                self.assertTrue(media_holds.blocked(self.store,'managed:'+row['episode_key']))
                self.assertFalse(ds.commit_engine(self.store,row['id'],generation,'nzb','different-release','Queued'))
                self.assertFalse(acquire._retry_candidate(self.store,row,'Incomplete articles'))


class PublicationHoldTests(unittest.TestCase):
    def test_clear_during_metadata_prevents_video_publication_and_keeps_staging(self):
        from test_discover_publication import DiscoverPublicationTests
        from backend import discover_publication as publication
        from backend.media_library import reserve
        from backend.discover_youtube import claim_path
        fixture=DiscoverPublicationTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        record=reserve(fixture.store,fixture.media,fixture.video['id'])
        record['path']=claim_path(fixture.store,fixture.media,fixture.video['id'],fixture.video['title'])
        produced=fixture.media/'staged.mkv';produced.write_bytes(b'keep-staged-video')
        existing=fixture.media/'keep.mkv';existing.write_bytes(b'completed-file')
        def clear_then_art(*args):
            with fixture.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                media_holds.hold(db,media_holds.request_keys(fixture.row),[('discover',fixture.row['id'])])
            return fixture.art
        with patch('backend.m3u_player.cached_image_path',side_effect=clear_then_art):
            with self.assertRaisesRegex(InterruptedError,'cleared'):
                publication.publish(fixture.store,fixture.media,record,fixture.row,produced)
        self.assertFalse((fixture.media/record['path']).exists())
        self.assertEqual(produced.read_bytes(),b'keep-staged-video');self.assertEqual(existing.read_bytes(),b'completed-file')
