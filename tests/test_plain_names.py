import unittest
from backend.media_library import safe_name
from backend.m3u import filename
from backend.m3u_existing import compatible

class PlainNamesTests(unittest.TestCase):
    def test_new_filenames(self):
        for clean in (safe_name, filename):
            self.assertEqual(clean('Café 🚗'), 'Cafe')
            self.assertEqual(clean('日本語'), 'Untitled')
            self.assertEqual(clean('CON.txt'), '_CON.txt')
            self.assertNotRegex(clean('a/b: c?'), r'[<>:"/\\|?*]')
    def test_existing_stream_name_stays_compatible(self):
        self.assertTrue(compatible('Movie_VOD/Café 🚗 (2026).strm', 'Movie_VOD/Cafe (2026).strm'))
        self.assertFalse(compatible('Movie_VOD/Another (2026).strm', 'Movie_VOD/Cafe (2026).strm'))
