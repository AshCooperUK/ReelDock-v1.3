import hashlib
import tempfile
import unittest
from backend.app import create_app
from backend.cookie_recovery import resume, record, warning, DOWNLOAD_MESSAGE
from backend.cookies import save

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=create_app({'TESTING':True,'DATA_DIR':self.tmp.name}).extensions['store']
    def tearDown(self):self.tmp.cleanup()
    def test_resume_preserves_sent_uncertain_other_users_and_other_failures(self):
        scope=hashlib.sha256(b'|').hexdigest()
        states=['auth_expired','auth_required','auth_unverified','network_error','rate_limited','sent','unverified','sending','extractor_failed']
        with self.store.connect() as db:
            for state in states:db.execute('INSERT INTO emby_watched VALUES (?,?,?,?)',(scope,state,state,''))
            db.execute("INSERT INTO emby_watched VALUES ('other','video','auth_expired','')")
            for name,state,message in [('cookie','failed',DOWNLOAD_MESSAGE),('disk','failed','Disk full'),('cancelled','cancelled',DOWNLOAD_MESSAGE)]:
                db.execute('INSERT INTO downloads(id,title,quality,state,message,created,updated) VALUES (?,?,720,?,?,0,0)',(name,name,state,message))
        self.store.set('emby_watched','false')
        resume(self.store)
        self.assertEqual(self.store.get('emby_watched'),'false')
        with self.store.connect() as db:
            actual={r['id']:r['state'] for r in db.execute('SELECT * FROM emby_watched WHERE scope=?',(scope,))}
            for state in states:self.assertEqual(actual[state],'retry' if state in states[:5] else state)
            self.assertEqual(db.execute("SELECT state FROM emby_watched WHERE scope='other'").fetchone()[0],'auth_expired')
            actual={r['id']:r['state'] for r in db.execute('SELECT * FROM downloads')}
            self.assertEqual(actual,{'cookie':'queued','disk':'failed','cancelled':'cancelled'})
    def test_warning_distinguishes_expiry_and_unverified_session(self):
        self.assertIsNone(warning(self.store))
        record(self.store,'auth_unverified')
        first=warning(self.store)
        self.assertNotIn('expired',first['message'])
        record(self.store,'auth_unverified')
        self.assertEqual(first['id'],warning(self.store)['id'])
        record(self.store,'active')
        self.assertIsNone(warning(self.store))
        save(self.store.directory,b'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1\tSID\tvalue\n')
        self.assertIn('expired',warning(self.store)['message'])

    def test_download_cookie_errors_are_identified_without_requeuing_other_errors(self):
        from backend.downloads import cookie_failure
        cookie_failure(self.store,'Video unavailable')
        self.assertIsNone(warning(self.store))
        with self.assertRaisesRegex(ValueError,'retry after fresh cookies'):
            cookie_failure(self.store,'ERROR: Your cookies are no longer valid')
        self.assertIn('expired',warning(self.store)['message'])
