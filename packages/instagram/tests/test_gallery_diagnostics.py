import logging
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from instagram_archive_core.gallery_diagnostics import safe_url, trace_request, trace_job


class GalleryDiagnosticsTest(unittest.TestCase):
    def test_http_failure_retains_status_before_upstream_masks_as_not_found(self):
        from gallery_dl.exception import HttpError
        logger=Mock()
        extractor=SimpleNamespace(subcategory='avatar',url='https://www.instagram.com/example/avatar/',log=logger)
        error=HttpError('private response details',SimpleNamespace(status_code=429))
        with self.assertRaises(HttpError):
            trace_request(Mock(side_effect=error))(extractor,'https://www.instagram.com/api/v1/users/123/info/')
        template,*args=logger.error.call_args.args
        message=template%tuple(args)
        self.assertIn('status=429',message)
        self.assertNotIn('private response details',message)

    def test_request_failure_identifies_endpoint_without_credentials(self):
        logger = Mock()
        extractor = SimpleNamespace(subcategory='highlights', url='https://www.instagram.com/example/highlights/', log=logger)
        original = Mock(side_effect=RuntimeError('redirect'))
        with self.assertRaises(RuntimeError):
            trace_request(original)(extractor, 'https://user:password@i.instagram.com/api/v1/highlights/123/highlights_tray/?token=secret',
                                    headers={'Cookie':'private'}, data={'secret':'private'})
        output = str(logger.mock_calls)
        self.assertIn('highlights_tray/', output)
        for secret in ('password', 'token=', 'secret', 'private'):
            self.assertNotIn(secret, output)

    def test_error_with_zero_exit_becomes_nonzero(self):
        logger = logging.getLogger('instagram.diagnostic-test')
        extractor = SimpleNamespace(category='instagram', subcategory='highlights',
                                    url='https://www.instagram.com/example/highlights/', log=logger)
        job = SimpleNamespace(extractor=extractor, status=0)
        def original(job):
            logger.error('HTTP redirect to home page')
            return 0
        self.assertEqual(trace_job(original)(job), 1)
        self.assertEqual(job.status, 1)

    def test_known_boundary_remains_success(self):
        extractor = SimpleNamespace(category='instagram', subcategory='posts', url='https://www.instagram.com/example/posts/', log=Mock())
        job = SimpleNamespace(extractor=extractor, status=0)
        self.assertEqual(trace_job(lambda job:0)(job), 0)

    def test_url_rejects_invalid_and_strips_queries_fragments_controls(self):
        self.assertEqual(safe_url('https://u:p@example.com/path?q=secret#secret'), 'https://example.com/path')
        self.assertEqual(safe_url('file:///private'), '[invalid-url]')
