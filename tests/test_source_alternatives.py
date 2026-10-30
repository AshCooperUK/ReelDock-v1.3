"""Alternatives are explicit, exact-identity, account-scoped and read-only on GET."""
import json
import time
import unittest
from unittest.mock import Mock, patch
import test_view_media as vm
from backend import discover_store as records, media_holds, source_alternatives as choices


class SourceAlternativeTests(unittest.TestCase):
    setUpBase=vm.ViewMediaTests.setUp
    tearDownBase=vm.ViewMediaTests.tearDown
    get=vm.ViewMediaTests.get

    def setUp(self):
        self.setUpBase();self.tasks=[]
        self.accounts=patch('backend.discover_sources.indexers_for',return_value=[dict(url='https://fixture.test/api',api_key='PRIVATE_KEY',name='Fixture indexer')]);self.accounts.start();self.addCleanup(self.accounts.stop)
        self.pool=patch.object(choices._POOL,'submit',side_effect=lambda fn:self.tasks.append(fn));self.pool.start();self.addCleanup(self.pool.stop)
        self.original=dict(id='original',kind='movie',externalId='12345',title='Fixture.2026.1080p-GROUP',quality='1080p',size=5_000_000_000,source='nzb',grab='https://fixture.test/api?id=original&apikey=PRIVATE_KEY')
        self.other=dict(self.original,id='different',title='Fixture.2026.1080p-OTHER',size=4_800_000_000)
        records.remember_candidates(self.store,[self.original,self.other])
        self.row,_=records.insert_request(self.store,dict(idempotency='failed-selection',kind='movie',title='Fixture',state='failed',quality='1080',fallback=False,manual=True,engine='nzb',episode_key='movie:12345',body=dict(externalId='12345',managedReleaseId='original',item=dict(id='movie:12345',kind='movie',externalId='12345',title='Fixture'))))
        records.add_attempt(self.store,self.row['id'],'original','nzb','1080p','failed','This Usenet server does not have these articles.')

    def tearDown(self):
        with choices._LOCK:
            choices._PENDING.difference_update(key for key in tuple(choices._PENDING) if key[0]==str(self.store.directory))
        self.tearDownBase()

    def post(self,body,token='token'):
        return self.client.post('/api/source-alternatives',base_url=vm.ORIGIN,json=body,headers={'Origin':vm.ORIGIN,'X-CSRF-Token':token})

    def url(self):return '/api/source-alternatives?request='+self.row['id']

    def search(self,releases=None):
        response=self.post(dict(action='search',requestId=self.row['id']))
        self.assertEqual(response.status_code,202,response.json)
        with patch.object(choices,'_search_candidates',return_value=releases or [self.other]):self.tasks.pop(0)()
        return self.get(self.url()).json

    def test_saved_get_has_no_provider_database_or_hold_mutation_and_requires_auth(self):
        with self.store.connect() as db:before=list(db.iterdump())
        with patch.object(choices,'_search_candidates',side_effect=AssertionError('GET cannot search')):
            response=self.get(self.url())
        self.assertEqual(response.status_code,200);self.assertEqual(response.json['state'],'idle');self.assertEqual(response.json['items'],[])
        self.assertEqual(response.json['original']['size'],5_000_000_000)
        from backend.source_choice import eligibility
        with self.store.connect() as db:self.assertEqual(response.json['attemptKey'],eligibility(db,self.row)['attemptKey'])
        with self.store.connect() as db:self.assertEqual(list(db.iterdump()),before)
        self.assertNotEqual(self.app.test_client().get(self.url(),base_url=vm.ORIGIN).status_code,200)
        self.assertEqual(self.post(dict(action='search',requestId=self.row['id']),token='wrong').status_code,403)
        self.assertEqual(self.client.post('/api/source-alternatives',base_url=vm.ORIGIN,json=dict(action='search',requestId=self.row['id']),headers={'Origin':'https://other.test','X-CSRF-Token':'token'}).status_code,403)

    def test_legacy_pinned_article_failure_is_available_without_rewriting_waiting_row(self):
        with self.store.connect() as db:
            db.execute("UPDATE discover_requests SET state='waiting',message='Waiting for release.' WHERE id=?",(self.row['id'],))
            before=list(db.iterdump())
        response=self.get(self.url());self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json['state'],'idle');self.assertEqual(response.json['original']['title'],self.original['title'])
        with self.store.connect() as db:self.assertEqual(list(db.iterdump()),before)

    def test_normal_legacy_failure_repair_preserves_same_attempt_choice_frame(self):
        from backend.source_choice import restore
        with self.store.connect() as db:db.execute("UPDATE discover_requests SET state='waiting' WHERE id=?",(self.row['id'],))
        frame=self.search()
        with self.store.connect() as db:waiting=dict(db.execute('SELECT * FROM discover_requests').fetchone())
        self.assertTrue(restore(self.store,waiting))
        updated=self.get(self.url()).json
        self.assertEqual(updated['attemptKey'],frame['attemptKey']);self.assertEqual(updated['revision'],frame['revision'])
        self.assertEqual(updated['items'],frame['items'])

    def test_public_choices_are_limited_to_ten_and_positive_difference_means_larger(self):
        releases=[dict(self.other,id='new'+str(number),title='Different.Release.'+str(number),size=5_000_000_000+number*10_000_000) for number in range(1,16)]
        ranked=choices.rank_candidates(releases,wanted='1080',allow_lower=False,original=self.original,failed_ids=set(),failed_titles=set(),minimum=0,maximum=10_000_000_000)
        self.assertEqual(len(ranked),10);self.assertGreater(ranked[0]['differencePercent'],0)

    def test_search_coalesces_and_reports_progress_without_unpinning_failed_source(self):
        original_body=self.row['body'];first=self.post(dict(action='search',requestId=self.row['id']))
        second=self.post(dict(action='search',requestId=self.row['id']))
        self.assertEqual(first.status_code,202);self.assertTrue(first.json['loading'])
        self.assertEqual(second.json['revision'],first.json['revision']);self.assertEqual(len(self.tasks),1)
        self.assertEqual(self.get(self.url()).json['state'],'searching')
        with patch.object(choices,'_search_candidates',return_value=[self.other]):self.tasks.pop(0)()
        result=self.get(self.url()).json;self.assertEqual(result['state'],'ready');self.assertFalse(result['loading'])
        self.assertEqual([item['id'] for item in result['items']],['different']);self.assertTrue(result['items'][0]['recommended'])
        self.assertEqual(result['items'][0]['differencePercent'],-4.0)
        self.assertEqual(result['items'][0]['availability'],'unchecked');self.assertNotIn('PRIVATE',json.dumps(result));self.assertNotIn('grab',json.dumps(result))
        with self.store.connect() as db:
            row=db.execute('SELECT body,state FROM discover_requests WHERE id=?',(self.row['id'],)).fetchone()
        self.assertEqual(row['body'],original_body);self.assertEqual(row['state'],'failed')

    def test_clear_before_queued_search_runs_prevents_provider_and_old_selection(self):
        frame=self.post(dict(action='search',requestId=self.row['id'])).json
        with self.store.connect() as db:media_holds.hold(db,['managed:movie:12345'],[('discover',self.row['id'])])
        with patch.object(choices,'_search_candidates',side_effect=AssertionError('Clear must fence provider')):self.tasks.pop(0)()
        self.assertEqual(self.get(self.url()).status_code,409)
        self.assertEqual(self.post(dict(action='select',requestId=self.row['id'],candidateId='different',revision=frame['revision'])).status_code,409)
        self.assertTrue(media_holds.blocked(self.store,'managed:movie:12345'))

    def test_saved_search_claim_coalesces_even_without_process_local_pending_entry(self):
        first=self.post(dict(action='search',requestId=self.row['id'])).json
        with choices._LOCK:choices._PENDING.discard((str(self.store.directory),self.row['id']))
        second=self.post(dict(action='search',requestId=self.row['id']))
        self.assertEqual(second.status_code,202);self.assertEqual(second.json['revision'],first['revision'])
        self.assertEqual(len(self.tasks),1)

    def test_changed_account_or_attempt_cannot_reuse_old_public_choices(self):
        first=self.search();self.assertTrue(first['items'])
        self.accounts.stop()
        self.accounts=patch('backend.discover_sources.indexers_for',return_value=[dict(url='https://fixture.test/api',api_key='NEW_PRIVATE_KEY')]);self.accounts.start()
        self.assertEqual(self.get(self.url()).json['items'],[])
        self.assertEqual(self.post(dict(action='select',requestId=self.row['id'],candidateId='different',revision=first['revision'])).status_code,409)

    def test_candidate_facts_are_server_approved_and_duplicate_select_runs_once(self):
        frame=self.search();body=dict(action='select',requestId=self.row['id'],candidateId='different',revision=frame['revision'])
        self.assertEqual(self.post(dict(body,size=1)).status_code,409)
        self.assertEqual(self.post(dict(body,candidateId='not-approved')).status_code,409)
        def ordinary_replace(app,store,row,candidate,attempt_key):
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                approved=choices.consume_approval(db,row['id'],candidate['id'],attempt_key)
                self.assertEqual(approved['id'],'different')
                db.execute("UPDATE discover_requests SET state='searching' WHERE id=?",(row['id'],))
            return dict(id='replacement',state='searching')
        with patch('backend.discover_acquire.replace_failed_source',create=True,side_effect=ordinary_replace) as replace:
            response=self.post(body);self.assertEqual(response.status_code,202,response.json)
            self.assertEqual(response.json['replacement'],dict(id='replacement',state='searching'))
            self.assertEqual(self.post(body).status_code,409);replace.assert_called_once()
            self.assertEqual(replace.call_args.args[3]['grab'],self.other['grab'])

    def test_clear_between_web_approval_and_replacement_transaction_wins(self):
        frame=self.search()
        def interrupted(app,store,row,candidate,attempt_key):
            with store.connect() as db:media_holds.hold(db,['managed:movie:12345'],[('discover',row['id'])])
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE');choices.consume_approval(db,row['id'],candidate['id'],attempt_key)
        with patch('backend.discover_acquire.replace_failed_source',create=True,side_effect=interrupted):
            result=self.post(dict(action='select',requestId=self.row['id'],candidateId='different',revision=frame['revision']))
        self.assertEqual(result.status_code,409);self.assertTrue(media_holds.blocked(self.store,'managed:movie:12345'))
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT state FROM discover_requests').fetchone()[0],'failed')

    def test_real_replacement_preserves_parent_attempts_and_schedules_only_explicit_choice(self):
        frame=self.search()
        with patch('backend.discovery_engine.schedule',return_value=True) as schedule:
            response=self.post(dict(action='select',requestId=self.row['id'],candidateId='different',revision=frame['revision']))
            self.assertEqual(response.status_code,202,response.json)
            self.assertEqual(response.json['replacement'],dict(id=self.row['id'],state='waiting'))
            self.assertEqual(self.post(dict(action='select',requestId=self.row['id'],candidateId='different',revision=frame['revision'])).status_code,409)
        self.assertEqual(schedule.call_count,1)
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM discover_requests').fetchone()
            self.assertEqual(row['id'],self.row['id']);self.assertEqual(row['engine_id'],'');self.assertEqual(row['state'],'waiting')
            self.assertEqual(json.loads(row['body'])['managedReleaseId'],'different')
            self.assertEqual(db.execute('SELECT count(*) FROM discover_attempts').fetchone()[0],1)

    def test_rejected_web_schedule_is_recovered_by_new_worker_without_repeating_failed_source(self):
        from backend import discover_acquire as acquire
        from backend.storage import Store
        frame=self.search()
        with patch('backend.discovery_engine.schedule',return_value=False):
            response=self.post(dict(action='select',requestId=self.row['id'],candidateId='different',revision=frame['revision']))
        self.assertEqual(response.status_code,202,response.json);self.assertEqual(response.json['replacement']['state'],'waiting')
        restarted=Store(self.store.directory)
        with patch.object(acquire.time,'time',return_value=time.time()+100),patch.object(acquire,'process') as process:
            acquire.resume_waiting(self.app,restarted)
        process.assert_called_once()
        retried=process.call_args.args[2]
        self.assertEqual(retried['state'],'searching');self.assertEqual(json.loads(retried['body'])['managedReleaseId'],'different')
        with restarted.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM discover_requests').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT candidate_id FROM discover_attempts').fetchone()[0],'original')

    def test_consumption_rechecks_account_after_web_approval(self):
        frame=self.search()
        def changed(app,store,row,candidate,attempt_key):
            self.accounts.stop();self.accounts=patch('backend.discover_sources.indexers_for',return_value=[dict(url='https://changed.test/api',api_key='PRIVATE_OTHER')]);self.accounts.start()
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE');choices.consume_approval(db,row['id'],candidate['id'],attempt_key)
        with patch('backend.discover_acquire.replace_failed_source',create=True,side_effect=changed):
            response=self.post(dict(action='select',requestId=self.row['id'],candidateId='different',revision=frame['revision']))
        self.assertEqual(response.status_code,409);self.assertNotIn('PRIVATE',json.dumps(response.json))

    def test_exact_mappings_cannot_cross_episode_special_or_direct_title_identity(self):
        row=dict(self.row);context=dict(row=row,body=dict(externalId='12345',episode=dict(season=1,number=2)))
        self.assertTrue(choices._exact(context,dict(self.other,title='Fixture.S01E02.1080p')))
        self.assertFalse(choices._exact(context,dict(self.other,title='Fixture.S01E03.1080p')))
        context['body']=dict(manualTvmaze='42',catalogueEpisode=dict(id=12))
        self.assertTrue(choices._exact(context,dict(kind='manual-episode:12',external_id='tvmaze:42')))
        self.assertFalse(choices._exact(context,dict(kind='manual-episode:13',external_id='tvmaze:42')))
        context['body']=dict(manualReleaseId='selected',externalId='12345',catalogueEpisode=dict(id=12))
        self.assertFalse(choices._exact(context,dict(kind='special:13',external_id='12345')))
        context['body']=dict(directTitleKey='verified',externalId='12345',item=dict(title='A.Documentary.2026.1080p.HDTV'))
        self.assertTrue(choices._exact(context,dict(external_id='12345',title='A.Documentary.2026.720p.HDTV')))
        self.assertFalse(choices._exact(context,dict(external_id='12345',title='Other.Documentary.2026.1080p.HDTV')))
        self.assertFalse(choices._exact(context,dict(external_id='12345',title='A.Documentary.2025.1080p.HDTV')))

    def test_overwritten_candidate_and_expired_revision_are_rejected(self):
        frame=self.search()
        records.remember_candidates(self.store,[dict(self.other,grab='https://fixture.test/changed?apikey=PRIVATE')])
        self.assertEqual(self.post(dict(action='select',requestId=self.row['id'],candidateId='different',revision=frame['revision'])).status_code,409)
        with self.store.connect() as db:db.execute('UPDATE source_alternative_frames SET updated=0')
        self.assertEqual(self.get(self.url()).json['state'],'idle')

    def test_search_errors_are_fixed_safe_and_completed_automatic_cancelled_are_ineligible(self):
        self.post(dict(action='search',requestId=self.row['id']))
        with patch.object(choices,'_search_candidates',side_effect=ValueError('https://secret.test/?apikey=PRIVATE')):self.tasks.pop(0)()
        result=self.get(self.url()).json;self.assertEqual(result['state'],'failed');self.assertNotIn('PRIVATE',json.dumps(result))
        for state in ('complete','cancelled','downloading'):
            with self.store.connect() as db:db.execute('UPDATE discover_requests SET state=? WHERE id=?',(state,self.row['id']))
            self.assertEqual(self.get(self.url()).status_code,409)
        with self.store.connect() as db:db.execute("UPDATE discover_requests SET state='failed',manual=0")
        self.assertEqual(self.get(self.url()).status_code,409)

    def test_normal_exact_search_unpins_only_temporary_body_and_rejects_wrong_identity(self):
        context=choices._context(self.store,self.row['id'])
        wrong=dict(self.other,id='wrong',externalId='99999')
        with patch('backend.discover_acquire.search_for',return_value=[self.other,wrong]) as search:
            found=choices._search_candidates(self.app,self.store,context)
        self.assertEqual([item['id'] for item in found],['different'])
        self.assertNotIn('managedReleaseId',json.loads(search.call_args.args[2]['body']))
        with self.store.connect() as db:self.assertEqual(json.loads(db.execute('SELECT body FROM discover_requests').fetchone()[0])['managedReleaseId'],'original')


if __name__=='__main__':unittest.main()
