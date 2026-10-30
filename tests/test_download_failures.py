"""Public error categories preserve causes without exposing extractor secrets."""
import unittest
from unittest.mock import Mock

from backend.downloads import failed_download,failure_category,MANUAL_LIVE_FILTER
from backend.cookie_recovery import DOWNLOAD_MESSAGE


class DownloadFailureTests(unittest.TestCase):
    GEO_MESSAGE='YouTube reports a country/region restriction for this video on ReelDock’s current connection. Check the VPN exit location.'

    def test_real_manual_live_filter_accepts_unknown_details_and_completed_broadcasts(self):
        from yt_dlp.utils import match_filter_func
        apply=match_filter_func(MANUAL_LIVE_FILTER)
        for facts in [{},{'live_status':None},{'live_status':'not_live'},{'live_status':'was_live'},{'live_status':'post_live'}]:
            self.assertIsNone(apply(facts),facts)
        for state in ['is_live','is_upcoming']:
            self.assertIsNotNone(apply({'live_status':state}))

    def test_format_unavailable_is_not_video_availability_or_account_failure(self):
        self.assertEqual(failure_category('ERROR: Requested format is not available. Use --list-formats.'),'format')
        self.assertEqual(failure_category('ERROR: No video formats found!'),'format')
        self.assertEqual(failure_category('ERROR: Video unavailable'),'availability')
        self.assertEqual(failure_category('ERROR: This video is private. Sign in to confirm access.'),'availability')

    def test_authenticated_availability_is_not_masked_by_anonymous_signin_error(self):
        store=Mock();store.get.return_value=''
        with self.assertRaisesRegex(ValueError,'unavailable, removed, restricted or inaccessible'):
            failed_download(store,[(True,'ERROR: Video unavailable private-token'),(False,'ERROR: Sign in to confirm you are not a bot https://private.invalid/?token=secret')])
        store.set.assert_not_called()

    def test_explicit_country_and_region_errors_precede_generic_availability(self):
        for reason in ('This video is not available in your country.',
                       'The uploader has not made this video available in your country.',
                       'This video is not available in your region.',
                       'This video is blocked in your country.',
                       'This video is geo-restricted.', 'This video is georestricted.'):
            with self.subTest(reason=reason):
                diagnostic='ERROR: Video unavailable\nERROR: '+reason+' https://private.invalid/?token=secret'
                self.assertEqual(failure_category(diagnostic),'geo')
                store=Mock()
                with self.assertRaises(ValueError) as raised:failed_download(store,[(True,diagnostic)])
                self.assertEqual(str(raised.exception),self.GEO_MESSAGE)
                self.assertNotIn('https:',str(raised.exception));self.assertNotIn('secret',str(raised.exception))
                store.set.assert_not_called()

    def test_authenticated_geo_is_authoritative_over_generic_or_anonymous_errors(self):
        geo='ERROR: Video unavailable. The uploader has not made this video available in your country. private-token'
        for attempts in ([(True,geo),(False,'ERROR: Video unavailable')],
                         [(True,'ERROR: Video unavailable'),(True,geo),(False,'ERROR: Sign in to confirm you are not a bot')],
                         [(True,geo),(False,'WARNING: Cookies have expired\nERROR: Video unavailable')]):
            with self.subTest(attempts=attempts):
                store=Mock();store.get.return_value=''
                with self.assertRaises(ValueError) as raised:failed_download(store,attempts)
                self.assertEqual(str(raised.exception),self.GEO_MESSAGE)
                self.assertNotIn('private-token',str(raised.exception));store.set.assert_not_called()

    def test_authenticated_expiry_warning_still_precedes_geo_error(self):
        diagnostic='WARNING: Your cookies have expired private-token\nERROR: This video is not available in your country. https://private.invalid/'
        self.assertEqual(failure_category(diagnostic),'expired')
        store=Mock();store.get.return_value=''
        with self.assertRaises(ValueError) as raised:failed_download(store,[(True,diagnostic),(False,'ERROR: Video unavailable')])
        self.assertEqual(str(raised.exception),DOWNLOAD_MESSAGE)
        store.set.assert_any_call('youtube_cookie_issue','auth_expired')

    def test_nonexpiry_geo_warning_cannot_replace_private_video_error(self):
        diagnostic='WARNING: Not available in your country\nERROR: This video is private. https://private.invalid/?token=secret'
        self.assertEqual(failure_category(diagnostic),'availability')
        store=Mock()
        with self.assertRaises(ValueError) as raised:failed_download(store,[(True,diagnostic)])
        self.assertIn('unavailable, removed, restricted or inaccessible',str(raised.exception))
        self.assertNotIn('https:',str(raised.exception));store.set.assert_not_called()

    def test_anonymous_geo_reports_region_without_mutating_saved_cookie_issue(self):
        store=Mock();store.get.return_value='auth_unverified'
        with self.assertRaises(ValueError) as raised:failed_download(store,[(False,'ERROR: This video is not available in your country. private-token')])
        self.assertEqual(str(raised.exception),self.GEO_MESSAGE)
        self.assertNotIn('private-token',str(raised.exception));store.set.assert_not_called()

    def test_authentication_expiry_is_preserved_without_raw_diagnostics(self):
        store=Mock();store.get.return_value=''
        with self.assertRaises(ValueError) as raised:
            failed_download(store,[(True,'ERROR: Your cookies have expired private-token'),(False,'ERROR: Video unavailable https://private.invalid/')])
        self.assertIn('cookies need attention',str(raised.exception))
        self.assertNotIn('private-token',str(raised.exception));self.assertNotIn('https:',str(raised.exception))
        store.set.assert_any_call('youtube_cookie_issue','auth_expired')

    def test_authenticated_cookie_expiry_warning_survives_availability_and_signin_errors(self):
        for warning in ('Your cookies have expired','The provided YouTube account cookies are no longer valid'):
            for error in ('Video unavailable','Sign in to confirm your age'):
                with self.subTest(warning=warning,error=error):
                    diagnostic='WARNING: '+warning+' private-token\nERROR: '+error+' https://private.invalid/?token=secret'
                    self.assertEqual(failure_category(diagnostic),'expired')
                    store=Mock();store.get.return_value=''
                    with self.assertRaises(ValueError) as raised:
                        failed_download(store,[(True,diagnostic),(False,'ERROR: Video unavailable')])
                    self.assertEqual(str(raised.exception),DOWNLOAD_MESSAGE)
                    self.assertNotIn('private-token',str(raised.exception));self.assertNotIn('https:',str(raised.exception))
                    store.set.assert_any_call('youtube_cookie_issue','auth_expired')

    def test_anonymous_cookie_expiry_warning_cannot_mark_saved_cookies_invalid(self):
        anonymous='WARNING: cookies are no longer valid private-token\nERROR: Video unavailable https://private.invalid/'
        for attempts in ([(False,anonymous)],[(True,'ERROR: This video is private. Sign in to confirm access.'),(False,anonymous)]):
            with self.subTest(authenticated=attempts[0][0]):
                store=Mock();store.get.return_value=''
                with self.assertRaises(ValueError) as raised:failed_download(store,attempts)
                self.assertNotEqual(str(raised.exception),DOWNLOAD_MESSAGE)
                self.assertNotIn('private-token',str(raised.exception));self.assertNotIn('https:',str(raised.exception))
                if attempts[0][0]:self.assertIn('unavailable, removed, restricted or inaccessible',str(raised.exception))
                store.set.assert_not_called()

    def test_nonexpiry_warning_does_not_override_private_video_error(self):
        self.assertEqual(failure_category('WARNING: Sign in to confirm access\nERROR: This video is private.'),'availability')

    def test_unknown_and_network_errors_never_echo_provider_text(self):
        for diagnostic in ['ERROR: timed out https://private.invalid/token','ERROR: unexpected private-token']:
            with self.subTest(diagnostic=diagnostic):
                store=Mock()
                with self.assertRaises(ValueError) as raised:failed_download(store,[(False,diagnostic)])
                self.assertNotIn('private',str(raised.exception));self.assertNotIn('https:',str(raised.exception))
                store.set.assert_not_called()


if __name__=='__main__':unittest.main()
