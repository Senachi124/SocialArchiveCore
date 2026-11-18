import unittest
from instagram_archive_core.gallery_pacing import speed_choice,gallery_options

class PacingTests(unittest.TestCase):
    def test_conservative_default(self):
        self.assertEqual(speed_choice({}),('conservative',12))
        self.assertEqual(gallery_options({'gallery_speed':'conservative'})['sleep-request'],'3-8')
    def test_full_scan_cap(self):
        with self.assertRaises(ValueError):speed_choice({'fullCheck':True,'maxRpm':61})
        self.assertEqual(gallery_options({'gallery_speed':'custom','max_rpm':100,'full_check':True})['sleep-request'][0],1)
    def test_invalid_rate_rejected(self):
        for value in (True,0,101):
            with self.assertRaises(ValueError):speed_choice({'maxRpm':value})
