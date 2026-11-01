import unittest
from backend.view_media.saved_search import _checks

class SearchWarningTests(unittest.TestCase):
    def test_new_successful_check_removes_old_warning_without_changing_frame(self):
        items=[dict(id='film',kind='movie',action=dict(type='discover',item=dict(id='movie:1234567')))]
        page=dict(items=items,warnings=['Some TV metadata could not be checked. Known dates are shown; unknown dates sort last.'])
        result=_checks(page,dict(items=items,checkedAt=100),dict(status='complete',checkedAt=101,warnings=[],resultKeys=[]))
        self.assertEqual(result['warnings'],[]);self.assertIs(result['items'],items)
        self.assertTrue(result['providerChecksComplete'])

    def test_newer_failure_stays_visible_and_old_snapshot_warning_does_not_duplicate_it(self):
        result=_checks(dict(items=[],warnings=['Old error']),dict(items=[],checkedAt=100),dict(status='complete',checkedAt=101,warnings=['Current source error']))
        self.assertEqual(result['warnings'],['Current source error'])

    def test_pending_or_same_age_check_retains_unresolved_warnings(self):
        for status,checked in [('checking',101),('complete',100)]:
            with self.subTest(status=status):
                result=_checks(dict(items=[],warnings=['Saved source error']),dict(items=[],checkedAt=100),dict(status=status,checkedAt=checked,warnings=['Saved source error']))
                self.assertEqual(result['warnings'],['Saved source error'])

if __name__=='__main__':unittest.main()
