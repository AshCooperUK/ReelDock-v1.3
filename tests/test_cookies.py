import tempfile
import unittest
from pathlib import Path
from backend.cookies import save, LIMIT

class CookieTests(unittest.TestCase):
    def test_bounds_and_domains(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for data in (b'x' * (LIMIT + 1), b'\xff', b'# Netscape HTTP Cookie File\n.youtube.com.evil.test\tTRUE\t/\tTRUE\t0\tSID\tsecret\n', b'# Netscape HTTP Cookie File\ninvalid row'):
                with self.assertRaises(ValueError):
                    save(root, data)
            self.assertEqual(list(root.iterdir()), [])
            save(root, b'# Netscape HTTP Cookie File\n#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tvalue\n')
            self.assertIn('#HttpOnly_', (root / 'youtube-cookies.txt').read_text())
