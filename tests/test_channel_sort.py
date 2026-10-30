import json
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from backend.storage import Store
from backend.catalogue import initialise
from backend.channel_sort import add_sort_metrics


class ChannelSortTests(unittest.TestCase):
    def test_saved_history_scheduled_forecasts_and_unknown_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            initialise(store)
            now = 1800000000
            stamp = lambda n: datetime.fromtimestamp(n, timezone.utc).isoformat()
            with store.connect() as db:
                for vid, state, moment in [('old', 'published', now-40*86400),
                        ('recent', 'published', now-86400), ('future', 'published', now+86400),
                        ('scheduled', 'scheduled', now+7200)]:
                    db.execute('INSERT INTO videos VALUES (?,?,?,?,?,?)',
                        (vid,'a',stamp(moment)[:10],stamp(moment),json.dumps(dict(state=state)),now))
                for channel in ['a','b']:
                    db.execute('INSERT INTO forecasts VALUES (?,?,?,?)',
                        (channel,channel,stamp(now)[:10],json.dumps(dict(event_at=stamp(now+3600)))))
                db.execute('INSERT INTO channel_forecast_preferences VALUES (?,0)', ('b',))
            store.set('forecasts_built', now)
            channels = [dict(id='a',last_checked=now),dict(id='b'),dict(id='c',last_checked=now)]
            with patch('backend.channel_sort.time.time', return_value=now):
                result = add_sort_metrics(store, channels)
                self.assertEqual(result[0]['sort_metrics'],dict(latest=now-86400,activity=1,upcoming=now+3600))
                self.assertEqual(result[1]['sort_metrics'],dict(latest=None,activity=None,upcoming=None))
                self.assertEqual(result[2]['sort_metrics']['activity'],0)
                store.set('forecasts_built', now-49*3600)
                self.assertEqual(add_sort_metrics(store, channels)[0]['sort_metrics']['upcoming'],now+7200)
