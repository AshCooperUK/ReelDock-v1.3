import unittest
from unittest.mock import Mock,patch
from backend.quota import status
class FooterQuotaTests(unittest.TestCase):
 def test_status_and_reset_are_read_only(self):
  for values,state in [({'youtube_use_google_api':'false'},'off'),({},'disconnected'),({'google_tokens':'saved'},'ready'),({'google_tokens':'saved','google_quota_retry_at':'2000'},'limited')]:
   with self.subTest(state=state):
    store=Mock();store.get.side_effect=lambda key,default='':values.get(key,default)
    with patch('time.time',return_value=1000),patch('backend.quota.next_reset',return_value=3000):result=status(store)
    self.assertEqual(result['state'],state)
    self.assertEqual(result['reset_at'],1700 if state=='limited' else 2700)
    self.assertEqual(result['server_time'],1000)
    store.set.assert_not_called()
 def test_reset_grace_period_is_distinct_from_daily_reset(self):
  store=Mock();store.get.side_effect=lambda key,default='':{'google_tokens':'saved','google_quota_retry_at':'1200'}.get(key,default)
  with patch('time.time',return_value=1000):result=status(store)
  self.assertEqual(result['reset_at'],900);self.assertEqual(result['retry_at'],1200)
