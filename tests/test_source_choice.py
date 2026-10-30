"""Selected release failures need a source choice, without automatic retry churn."""
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_dashboard as dashboard
from backend import discover_store as ds, discover_acquire as acquire, source_choice, download_history, media_holds


class SourceChoiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture=dashboard.DashboardTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.app=self.fixture.app;self.store=self.fixture.store
        self.candidate=dict(id='release-one',source='nzb',kind='movie',externalId='123',quality='1080p',size=5_000_000_000,title='Exact.Movie.2026.1080p.Group',grab='https://example.invalid/private')
        ds.remember_candidates(self.store,[self.candidate])
        self.row,_=ds.insert_request(self.store,dict(idempotency='manual-failed-source',kind='movie',title='Exact movie',quality='1080',fallback=False,manual=True,episode_key='movie:123',
            body=dict(externalId='123',managedReleaseId='release-one',source='NZB first')))

    def current(self):
        with self.store.connect() as db:return dict(db.execute('SELECT * FROM discover_requests WHERE id=?',(self.row['id'],)).fetchone())

    def test_missing_article_precheck_is_failed_choice_not_unreleased_or_retried(self):
        with patch.object(acquire,'submit_nzb',side_effect=acquire.CandidateError('This Usenet server does not have these articles.')) as start:
            acquire.resolve_release(self.app,self.store,self.row,[self.candidate])
            self.assertEqual(start.call_count,1)
        saved=self.current();self.assertEqual(saved['state'],'failed');self.assertIn('Choose another source',saved['message'])
        with self.store.connect() as db:
            marker=source_choice.public(db,saved);self.assertTrue(marker['eligible'])
            self.assertEqual(db.execute("SELECT count(*) FROM discover_attempts WHERE source='torrent'").fetchone()[0],0)
        with patch.object(acquire,'search_for',side_effect=AssertionError('Pinned source was retried')):
            acquire.resume_waiting(self.app,self.store)
        history=download_history.page(self.store,self.app.config['MEDIA_DIR'],cached=True)
        public=next(row['item'] for row in history['items'] if row['kind']=='discover')
        self.assertEqual(public['sourceChoice'],marker)

    def test_legacy_pinned_waiting_is_readonly_eligible_then_repaired_once(self):
        ds.add_attempt(self.store,self.row['id'],'release-one','nzb','1080p','failed','This Usenet server does not have these articles.')
        ds.update_request(self.store,self.row['id'],state='waiting')
        with self.store.connect() as db:
            before=db.total_changes;marker=source_choice.public(db,self.current());self.assertEqual(db.total_changes,before)
        self.assertTrue(marker['eligible']);self.assertTrue(source_choice.restore(self.store,self.current()))
        self.assertEqual(self.current()['state'],'failed')
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM discover_attempts').fetchone()[0],1)

    def test_native_failure_persists_failed_candidate_even_after_checkpoint_disappears(self):
        ds.add_attempt(self.store,self.row['id'],'release-one','nzb','1080p','queued','Accepted')
        ds.update_request(self.store,self.row['id'],state='downloading',engine='nzb',engine_id='native-one')
        engine=SimpleNamespace(stop=threading.Event(),state_dir=Path(self.fixture.tmp.name)/'jobs',jobs=lambda:[dict(id='native-one',state='failed',error='Incomplete articles.',published=[])])
        self.app.extensions['nzb_engine']=engine
        try:acquire.sync_jobs(self.app,self.store)
        finally:self.app.extensions.pop('nzb_engine')
        self.assertEqual(self.current()['state'],'failed')
        with self.store.connect() as db:
            last=dict(db.execute('SELECT * FROM discover_attempts ORDER BY id DESC LIMIT 1').fetchone())
            self.assertEqual(last['state'],'failed');self.assertEqual(last['candidate_id'],'release-one')
            self.assertIn('Incomplete',source_choice.public(db,self.current())['reason'])

    def test_automatic_insert_cannot_override_pending_explicit_source_decision(self):
        self.assertTrue(source_choice.fail(self.store,self.row,'Incomplete articles.'))
        saved,created=ds.insert_request(self.store,dict(idempotency='automatic-new',kind='movie',title='Exact movie',quality='1080',fallback=False,manual=False,automatic=True,episode_key='movie:123',body=dict(externalId='123')))
        self.assertFalse(created);self.assertEqual(saved['id'],self.row['id'])

    def test_clear_wins_failure_classification_and_stale_popup(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');media_holds.hold(db,media_holds.request_keys(self.row),[('discover',self.row['id'])])
        self.assertFalse(source_choice.fail(self.store,self.row,'Incomplete articles.'))
        with self.store.connect() as db:self.assertIsNone(source_choice.public(db,self.current()))

    def test_shared_errors_remain_actionable_without_candidate_churn(self):
        for message in ('Wrong password for Usenet server.','Disk permission denied.','Connection timed out while reading articles.','API quota exceeded.'):
            with self.subTest(message=message):self.assertFalse(source_choice.fail(self.store,self.row,message))
        self.assertTrue(source_choice.local_failure('This Usenet server does not have these articles.'))
        self.assertEqual(self.current()['state'],'searching')

    def test_source_choice_has_one_dialog_feedback_owner_but_account_errors_still_alert(self):
        from backend.activity import snapshot
        ds.update_request(self.store,self.row['id'],state='failed',message='Wrong password for Usenet server.')
        self.assertTrue(any(value['id'].startswith('discover-'+self.row['id']) for value in snapshot(self.store)['alerts']))
        self.assertTrue(source_choice.fail(self.store,self.current(),'Incomplete articles.'))
        self.assertFalse(any(value['id'].startswith('discover-'+self.row['id']) for value in snapshot(self.store)['alerts']))
