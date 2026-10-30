"""Expected automatic policy skips remain logs, not actionable failure alerts."""
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import activity, download_history
import test_activity as activity_fixture


class ActivityPolicySkipTests(unittest.TestCase):
    setUp = activity_fixture.ActivityTests.setUp
    tearDown = activity_fixture.ActivityTests.tearDown

    def add(self, identity, message, state='skipped', automatic=1):
        with self.store.connect() as db:
            db.execute('''INSERT INTO downloads(id,title,quality,state,automatic,message,created,updated)
                VALUES(?,?,'720',?,?,?,?,?)''', (identity, identity, state, automatic, message, time.time(), time.time()))

    def test_only_exact_known_automatic_policy_outcomes_are_classified(self):
        reasons = [
            'automatic downloads were turned off for this channel',
            'auto download settings no longer include this video',
            'normal videos are turned off for this channel',
            'Shorts are turned off for this channel',
            'live streams are turned off for this channel',
            "published before this channel's download range (last 1 day)",
            "published before this channel's download range (last 730 days)",
            'shorter than the 5-minute minimum length',
        ]
        for prefix in ['Not downloaded', 'Not saved', 'Removed from the queue']:
            for reason in reasons:
                row = dict(state='skipped', automatic=1, message=f'{prefix}: {reason}.')
                self.assertTrue(activity._expected_automatic_skip(row), row)
                for state, automatic in [('failed', 1), ('skipped', 0), ('skipped', None), ('skipped', 2)]:
                    self.assertFalse(activity._expected_automatic_skip(dict(row, state=state, automatic=automatic)))
        for message in [
            "Could not identify this video type, so this channel's download settings could not be checked.",
            'Not downloaded: its length could not be checked against the 5-minute minimum.',
            'Waiting for the live broadcast to finish.',
            'Not downloaded: downloads are available for published videos only.',
            'Cookies have expired. Upload a fresh file.',
            'Download interrupted. Check media folder access and free space, then retry.',
            'Not downloaded: automatic downloads were turned off for this channel. Cookie access also failed.',
            'Not downloaded: a new unknown policy reason.',
            '',
        ]:
            self.assertFalse(activity._expected_automatic_skip(dict(state='skipped', automatic=1, message=message)), message)

    def test_policy_group_is_absent_but_saved_and_export_history_are_unchanged(self):
        message = 'Not downloaded: automatic downloads were turned off for this channel.'
        self.add('policy-one', message)
        self.add('policy-two', message)
        with self.store.connect() as db:
            db.execute('INSERT INTO media_clear_holds VALUES(?,?,?)',('video:unrelated-held','fixture-hold',time.time()))
            before = [tuple(row) for row in db.execute('SELECT * FROM downloads ORDER BY id')]
            holds = [tuple(row) for row in db.execute('SELECT * FROM media_clear_holds ORDER BY key')]
            archives = db.execute('SELECT COUNT(*) FROM activity_archive').fetchone()[0]
        self.assertFalse(activity.snapshot(self.store)['alerts'])
        for options in [dict(cached=True), dict(plain=True, include_archived=True)]:
            rows = download_history.page(self.store, Path(self.temp.name)/'media', limit=50, **options)['items']
            videos = [row['item'] for row in rows if row['kind']=='youtube']
            self.assertEqual({row['id'] for row in videos}, {'policy-one','policy-two'})
            self.assertTrue(all(row['state']=='skipped' and row['message']==message for row in videos))
        with self.store.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM downloads ORDER BY id')], before)
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM media_clear_holds ORDER BY key')], holds)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM activity_archive').fetchone()[0], archives)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM dismissed_alerts').fetchone()[0], 0)

    def test_manual_failed_unknown_and_cookie_alerts_keep_their_retry_path(self):
        policy = 'Not downloaded: automatic downloads were turned off for this channel.'
        self.add('quiet-policy', policy)
        self.add('manual-skip', policy, automatic=0)
        self.add('real-failure', policy, state='failed')
        self.add('unknown-type', 'Could not identify this video type.')
        self.add('unknown-length', 'Not downloaded: its length could not be checked against the 5-minute minimum.')
        self.add('cookie-failure', 'Saved cookies need attention.', state='failed', automatic=0)
        self.store.set('youtube_fallback_error', 'Some video details are unavailable. Check saved cookies.')
        alerts = activity.snapshot(self.store)['alerts']
        identities = {identity for alert in alerts for identity in alert.get('members', [alert['id']])}
        self.assertNotIn('download-quiet-policy', identities)
        self.assertTrue({'download-manual-skip','download-real-failure','download-unknown-type','download-unknown-length','download-cookie-failure'} <= identities)
        retries = {alert['retry']['id'] for alert in alerts if alert.get('retry')}
        self.assertTrue({'unknown-type','unknown-length','cookie-failure'} <= retries)
        self.assertTrue(any(alert['title']=='YouTube details' for alert in alerts))
        client = self.app.test_client()
        with client.session_transaction(base_url='https://test.example') as session:
            session['user_id']=1
        warning = dict(id='cookie-warning-fixture', message='The saved cookie file needs attention.')
        with patch('backend.cookie_recovery.warning', return_value=warning):
            response = client.get('/api/activity', base_url='https://test.example')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['cookie_warning'], warning)


if __name__=='__main__':
    unittest.main()
