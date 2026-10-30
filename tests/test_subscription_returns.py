import json
import unittest
from unittest.mock import patch
import test_guide as fixture
from backend import subscription_returns as returns, activity

class SubscriptionReturnTests(unittest.TestCase):
    setUp=fixture.GuideTests.setUp
    tearDown=fixture.GuideTests.tearDown
    google=fixture.GuideTests.google
    def item(self,identity='new'):
        return dict(id=identity,snippet=dict(title='Returning channel',resourceId={'channelId':fixture.CHANNEL}))
    def remove(self):
        with self.store.connect() as db:db.execute('INSERT OR IGNORE INTO removed_channels VALUES(?)',(fixture.CHANNEL,))
    def test_changed_subscription_prompts_and_cross_persists(self):
        returns.observe(self.store,[self.item('old')]);self.remove()
        returns.observe(self.store,[self.item('old')])
        self.assertFalse(activity.snapshot(self.store)['alerts'])
        returns.observe(self.store,[self.item()])
        alerts=activity.snapshot(self.store)['alerts'];self.assertEqual(len(alerts),1)
        returns.decide(self.store,fixture.CHANNEL,'new',False)
        returns.observe(self.store,[self.item()]);self.assertFalse(activity.snapshot(self.store)['alerts'])
        returns.observe(self.store,[]);returns.observe(self.store,[self.item('newer')])
        self.assertEqual(len(activity.snapshot(self.store)['alerts']),1)
    def test_legacy_removal_prompts_once_and_accept_restores(self):
        self.remove();returns.observe(self.store,[self.item()])
        returns.decide(self.store,fixture.CHANNEL,'new',True)
        with self.store.connect() as db:
            self.assertIsNone(db.execute('SELECT id FROM removed_channels').fetchone())
            self.assertEqual(db.execute('SELECT active FROM channels WHERE id=?',(fixture.CHANNEL,)).fetchone()[0],1)
        self.assertEqual(self.store.get('next_sync'),'0')
        with self.assertRaises(ValueError):returns.decide(self.store,fixture.CHANNEL,'new',True)
    def test_restore_endpoint_requires_csrf_and_current_alert(self):
        self.remove();returns.observe(self.store,[self.item()])
        body=dict(channel=fixture.CHANNEL,subscription='new',accept=True)
        route='/api/activity/subscription-return'
        self.assertEqual(self.client.post(route,base_url=fixture.ORIGIN,json=body).status_code,403)
        headers={'Origin':fixture.ORIGIN,'X-CSRF-Token':'csrf-fixture'}
        self.assertEqual(self.client.post(route,base_url=fixture.ORIGIN,json={**body,'subscription':'stale'},headers=headers).status_code,409)
        self.assertEqual(self.client.post(route,base_url=fixture.ORIGIN,json=body,headers=headers).status_code,200)
