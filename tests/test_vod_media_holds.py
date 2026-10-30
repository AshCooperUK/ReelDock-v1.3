"""VOD/private STRM Clear fences acquisition, restart and final publication.

Temporary generated catalogues and bytes only; no provider or owner state.
"""
import io
import json
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend import discover_store, media_holds, m3u_downloads as downloads, strm
from tests import test_m3u_downloads as vod_fixture, test_strm as strm_fixture

MEDIA=b'\x00\x00\x00\x20ftypisom'+b'x'*(600*1024)


class VodMediaHoldTests(unittest.TestCase):
    setUp=vod_fixture.ServerDownloadTests.setUp
    tearDown=vod_fixture.ServerDownloadTests.tearDown
    enqueue=vod_fixture.ServerDownloadTests.enqueue
    response=vod_fixture.ServerDownloadTests.response

    def hold(self,job,keys=None):
        keys=keys or [media_holds.vod_key(job['source'],job['path'])]
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');media_holds.hold(db,keys,[('m3u',job['id'])])

    def row(self,job):
        with self.store.connect() as db:return dict(db.execute('SELECT * FROM m3u_downloads WHERE id=?',(job['id'],)).fetchone())

    def post(self,body,csrf='token',client=None):
        return (client or self.client).post('/api/m3u-server-downloads',base_url=vod_fixture.ORIGIN,
            headers={'Origin':vod_fixture.ORIGIN,'X-CSRF-Token':csrf},json=body)

    def request(self,job=None,key='movie:exact'):
        row,_=discover_store.insert_request(self.store,dict(idempotency=key,kind='movie',title='Same display title',
            quality='1080',fallback=False,episode_key=key,body={'item':{'id':'tmdb:movie:12','kind':'movie','title':'Exact'}}))
        with self.store.connect() as db:
            db.execute("UPDATE discover_requests SET state='starting',engine=?,engine_id=? WHERE id=?",('vod' if job else '',job['id'] if job else '',row['id']))
        return row

    def assert_unpublished(self):
        self.assertFalse(list(self.movies.rglob('*.mp4')));self.assertFalse(list(self.movies.rglob('*.part')))

    def test_generic_enqueue_never_releases_hold_or_resolves_held_source(self):
        job=self.enqueue();self.hold(job)
        with patch.object(downloads,'lookup',side_effect=AssertionError('Held source resolved')):
            with self.assertRaisesRegex(ValueError,'cleared|Cleared'):downloads.enqueue(self.app,self.store,job['source'],job['path'])
        self.assertTrue(media_holds.blocked(self.store,media_holds.vod_key(job['source'],job['path'])))

    def test_enqueue_rechecks_hold_in_transaction_after_resolution_race(self):
        movie=next(row for row in self.entries if row['kind']=='movies');key=media_holds.vod_key(self.source,movie['path']);original=downloads.lookup
        def lookup(*args):
            result=original(*args)
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE');media_holds.hold(db,[key],[])
            return result
        with patch.object(downloads,'lookup',side_effect=lookup):
            with self.assertRaises(ValueError):downloads.enqueue(self.app,self.store,self.source,movie['path'])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM m3u_downloads').fetchone()[0],0)
        self.assert_unpublished()

    def test_linked_request_hold_blocks_existing_source_without_title_guessing(self):
        job=self.enqueue();request=self.request(job);self.hold(job,['request:'+request['id']])
        with self.assertRaises(ValueError):downloads.enqueue(self.app,self.store,job['source'],job['path'])
        with patch('backend.m3u_player.upstream',side_effect=AssertionError('Held linked provider contacted')):
            downloads.transfer(self.app,self.store,job,threading.Event())
        self.assertEqual(self.row(job)['state'],'cancelled');self.assert_unpublished()
        # A different source/path with even the same display title stays eligible.
        episode=next(row for row in self.entries if row['kind']!='movies')
        with patch.object(downloads,'lookup',wraps=downloads.lookup):
            other=downloads.enqueue(self.app,self.store,self.source,episode['path'])
        self.assertNotEqual(other,job['id'])

    def test_selected_new_request_hold_blocks_enqueue_before_policy_attachment(self):
        request=self.request();key='managed:'+request['episode_key'];movie=next(row for row in self.entries if row['kind']=='movies')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');media_holds.hold(db,[key],[('discover',request['id'])])
        with patch('backend.discover_publication.planned_path',return_value='Movie/movie.mkv'):
            with self.assertRaises(ValueError):downloads.enqueue(self.app,self.store,self.source,movie['path'],movie_policy={'requestId':request['id']})
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM m3u_downloads').fetchone()[0],0)

    def test_restart_and_claim_skip_held_without_blocking_other_work(self):
        job=self.enqueue();episode=next(row for row in self.entries if row['kind']!='movies')
        other=downloads.enqueue(self.app,self.store,self.source,episode['path']);self.hold(job)
        with self.store.connect() as db:db.execute("UPDATE m3u_downloads SET state='running'")
        downloads.recover(self.store)
        self.assertEqual(self.row(job)['state'],'cancelled');claimed=downloads.claim(self.store)
        self.assertEqual(claimed['id'],other);self.assertTrue(media_holds.blocked(self.store,media_holds.vod_key(job['source'],job['path'])))

    def test_claim_cancels_newly_held_queue_and_selects_next_in_same_turn(self):
        job=self.enqueue();episode=next(row for row in self.entries if row['kind']!='movies')
        other=downloads.enqueue(self.app,self.store,self.source,episode['path']);self.hold(job)
        self.assertEqual(downloads.claim(self.store)['id'],other);self.assertEqual(self.row(job)['state'],'cancelled')

    def test_held_transfer_does_not_lookup_network_or_create_partial(self):
        job=self.enqueue();self.hold(job)
        with patch.object(downloads,'lookup',side_effect=AssertionError('Held source looked up')),patch('backend.m3u_player.upstream',side_effect=AssertionError('Held provider contacted')):
            downloads.transfer(self.app,self.store,job,threading.Event())
        self.assertEqual(self.row(job)['state'],'cancelled');self.assert_unpublished()

    def test_hold_while_connecting_closes_connection_before_reading_provider(self):
        job=self.enqueue();connection,response,resolved=self.response(MEDIA);response.read=Mock(wraps=response.read)
        def upstream(*args):self.hold(job);return connection,response,resolved
        with patch('backend.m3u_player.upstream',side_effect=upstream):downloads.transfer(self.app,self.store,job,threading.Event())
        connection.close.assert_called_once();response.read.assert_not_called();self.assertEqual(self.row(job)['state'],'cancelled');self.assert_unpublished()

    def test_mid_stream_hold_stops_before_next_chunk_and_cleans_private_partial(self):
        job=self.enqueue();connection,response,resolved=self.response(MEDIA);stream=io.BytesIO(MEDIA);reads=[]
        def read(limit):
            chunk=stream.read(limit);reads.append(len(chunk))
            if len(reads)==2:self.hold(job)
            return chunk
        response.read=read
        with patch('backend.m3u_player.upstream',return_value=(connection,response,resolved)):
            downloads.transfer(self.app,self.store,job,threading.Event())
        self.assertEqual(reads,[4096,256*1024]);self.assertEqual(self.row(job)['state'],'cancelled')
        self.assert_unpublished();connection.close.assert_called_once()

    def test_hold_after_verification_prevents_final_link(self):
        job=self.enqueue()
        with patch('backend.m3u_player.upstream',return_value=self.response(MEDIA)),patch('backend.movie_options.verify_file',side_effect=lambda *args:self.hold(job)),patch('backend.m3u_downloads.os.link') as link:
            downloads.transfer(self.app,self.store,job,threading.Event())
        link.assert_not_called();self.assertEqual(self.row(job)['state'],'cancelled');self.assert_unpublished()

    def test_late_progress_shutdown_or_error_settlement_cannot_resurrect_clear(self):
        job=self.enqueue();self.hold(job)
        for state in ('failed','queued','running','complete'):
            with self.subTest(state=state):
                downloads.update(self.store,job,state,'Late callback from an old attempt.',75)
                self.assertEqual(self.row(job)['state'],'cancelled')
                self.assertEqual(self.row(job)['message'],downloads.CLEARED_MESSAGE)
        self.assertTrue(media_holds.blocked(self.store,media_holds.vod_key(job['source'],job['path'])))

    def test_late_error_never_downgrades_already_complete_saved_media(self):
        job=self.enqueue()
        with patch('backend.m3u_player.upstream',return_value=self.response(MEDIA)):downloads.transfer(self.app,self.store,job,threading.Event())
        saved=self.row(job);self.hold(job)
        for state in ('failed','queued','running','cancelled'):
            downloads.update(self.store,job,state,'Late callback.',0)
            self.assertEqual(self.row(job)['state'],'complete')
        self.assertEqual(Path(saved['destination']).read_bytes(),MEDIA)

    def test_publication_is_fenced_against_concurrent_clear_and_saved_bytes_stay(self):
        import os
        job=self.enqueue();started=threading.Event();committed=threading.Event();errors=[];order=[];real_link=os.link
        def clear():
            started.set()
            try:self.hold(job);order.append('clear committed')
            except Exception as exc:errors.append(exc)
            finally:committed.set()
        threads=[]
        def link(source,target):
            thread=threading.Thread(target=clear);threads.append(thread);thread.start();self.assertTrue(started.wait(1))
            self.assertFalse(committed.wait(.05),'Clear bypassed publication transaction')
            real_link(source,target);order.append('video linked')
        try:
            with patch('backend.m3u_player.upstream',return_value=self.response(MEDIA)),patch('backend.m3u_downloads.os.link',side_effect=link),patch.object(downloads,'basic_metadata',return_value=False):
                downloads.transfer(self.app,self.store,job,threading.Event())
        finally:
            for thread in threads:thread.join(3)
        self.assertFalse(errors);self.assertTrue(committed.is_set());self.assertEqual(order,['video linked','clear committed'])
        self.assertEqual(Path(self.row(job)['destination']).read_bytes(),MEDIA)
        self.assertFalse(list(self.movies.rglob('*.part')))

    def test_only_authenticated_csrf_explicit_download_releases_exact_group(self):
        job=self.enqueue();self.hold(job);unrelated='vod:unrelated'
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');media_holds.hold(db,[unrelated],[('m3u','other')]);db.execute("UPDATE m3u_downloads SET state='cancelled' WHERE id=?",(job['id'],))
        body={'action':'download','source':job['source'],'path':job['path']}
        self.assertEqual(self.post(body,client=self.app.test_client()).status_code,401);self.assertEqual(self.post(body,csrf='wrong').status_code,403)
        self.assertTrue(media_holds.blocked(self.store,media_holds.vod_key(job['source'],job['path'])))
        response=self.post(body);self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.row(job)['state'],'queued');self.assertFalse(media_holds.blocked(self.store,media_holds.vod_key(job['source'],job['path'])))
        self.assertTrue(media_holds.blocked(self.store,unrelated))

    def test_explicit_retry_waits_for_old_worker_to_stop_before_releasing_hold(self):
        job=self.enqueue();self.hold(job)
        with self.store.connect() as db:db.execute("UPDATE m3u_downloads SET state='cancelling' WHERE id=?",(job['id'],))
        response=self.post({'action':'download','source':job['source'],'path':job['path']})
        self.assertEqual(response.status_code,400);self.assertIn('still stopping',response.json['error'])
        self.assertTrue(media_holds.blocked(self.store,media_holds.vod_key(job['source'],job['path'])))
        self.assertEqual(self.row(job)['state'],'cancelling')

    def test_existing_completed_media_and_sidecars_are_kept_on_clear(self):
        job=self.enqueue()
        with patch('backend.m3u_player.upstream',return_value=self.response(MEDIA)):downloads.transfer(self.app,self.store,job,threading.Event())
        target=Path(self.row(job)['destination']);before={p:p.read_bytes() for p in target.parent.iterdir() if p.is_file()}
        self.hold(job);downloads.transfer(self.app,self.store,self.row(job),threading.Event())
        self.assertEqual({p:p.read_bytes() for p in target.parent.iterdir() if p.is_file()},before)


class StrmMediaHoldTests(unittest.TestCase):
    setUp=strm_fixture.StrmTests.setUp
    tearDown=strm_fixture.StrmTests.tearDown
    post=strm_fixture.StrmTests.post
    preview=strm_fixture.StrmTests.preview
    start=strm_fixture.StrmTests.start
    rows=strm_fixture.StrmTests.rows
    job=strm_fixture.StrmTests.job
    response=strm_fixture.StrmTests.response

    def hold(self):
        job=self.job();key=media_holds.vod_key(job['source'],job['path'])
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');media_holds.hold(db,[key],[('m3u',job['id'])]);db.execute("UPDATE m3u_downloads SET state='cancelled' WHERE id=?",(job['id'],))
        return key

    def test_generic_start_and_retry_never_release_but_validated_explicit_start_does(self):
        self.assertEqual(self.start().status_code,200);key=self.hold();job=self.job();preview=self.preview()
        with self.app.test_request_context('/api/strm-downloads'):
            parsed=strm.parse(strm_fixture.POINTER,'Film.strm');digest=strm.fingerprint(self.app,strm_fixture.POINTER,'Film.strm')
            with self.assertRaises(ValueError):strm.start(self.app,self.store,parsed,digest,'Film','movies')
        with self.assertRaises(ValueError):downloads.enqueue(self.app,self.store,job['source'],job['path'])
        fields={'preview_id':preview['id'],'title':'Film','kind':'movies'}
        self.assertEqual(self.post('/api/strm-downloads',csrf='wrong',fields=fields).status_code,403)
        self.assertEqual(self.post('/api/strm-downloads',fields={**fields,'title':''}).status_code,400)
        self.assertTrue(media_holds.blocked(self.store,key))
        response=self.post('/api/strm-downloads',fields=fields);self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['id'],job['id']);self.assertFalse(media_holds.blocked(self.store,key));self.assertEqual(self.job()['state'],'queued')

    def test_held_preview_is_still_offline_and_does_not_release_or_persist(self):
        self.start();key=self.hold();before=self.rows('strm_sources')
        with patch('backend.m3u_player.upstream',side_effect=AssertionError('preview contacted provider')):self.preview()
        self.assertTrue(media_holds.blocked(self.store,key));self.assertEqual(self.rows('strm_sources'),before)

    def test_held_private_transfer_is_cancelled_without_url_leak_or_network(self):
        self.start();self.hold();job=self.job()
        with patch('backend.m3u_player.upstream',side_effect=AssertionError('held private provider called')):downloads.transfer(self.app,self.store,job,threading.Event())
        self.assertEqual(self.job()['state'],'cancelled');self.assertNotIn('private-token',json.dumps(self.job()))
        self.assertNotIn('https://',self.job()['message']);self.assertFalse(list(self.movies.rglob('*.mp4')))


if __name__=='__main__':unittest.main()
