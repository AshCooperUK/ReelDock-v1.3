import json
import subprocess
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from backend.discover_sources import search_youtube, DiscoverError
from backend.discover_logic import next_offset

class SearchPagesTests(unittest.TestCase):
    def result(self, count):
        return SimpleNamespace(returncode=0,stdout=json.dumps({'entries':[{'id':f'{i:011d}','title':str(i)} for i in range(count)]}))

    def test_youtube_has_next_page_without_a_fixed_total(self):
        with patch('subprocess.run',return_value=self.result(21)) as run:
            page=search_youtube('baby',2000)
        self.assertEqual(len(page['items']),20)
        self.assertEqual(page['nextOffset'],2020)
        self.assertIsNone(page['total'])
        args=run.call_args.args[0]
        self.assertEqual(args[args.index('--playlist-start')+1],'2001')
        self.assertEqual(args[args.index('--playlist-end')+1],'2021')
        self.assertEqual(args[-1],'ytsearchall:baby')

    def test_youtube_end_and_failed_page(self):
        for count in (0,7,20):
            with patch('subprocess.run',return_value=self.result(count)):
                self.assertIsNone(search_youtube('baby',20)['nextOffset'])
        with patch('subprocess.run',side_effect=subprocess.TimeoutExpired('yt-dlp',45)):
            with self.assertRaises(DiscoverError):search_youtube('baby',20)
        for offset in (-20,1):
            with self.assertRaises(DiscoverError):search_youtube('baby',offset)

    def test_nzb_continues_past_old_cap_but_stops_on_empty(self):
        self.assertEqual(next_offset(10000,100,50000),10100)
        self.assertEqual(next_offset(10000,100,None),10100)
        self.assertIsNone(next_offset(10000,0,50000))
        self.assertIsNone(next_offset(10000,30,10030))
