import copy
import unittest
import tempfile
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from instagram_archive_core.instagram_web_reels import (
    CONNECTION_DOC_ID, INITIAL_DOC_ID, PROVIDER, ReelsError,
    pages, parse_page, query,
)


def page(ids=('100',), next_cursor=None):
    return {'data': {'fetch__XDTUserDict': {'id': '123', 'clips_connection': {
        'edges': [{'node': {'media': {'pk': identity, 'code': 'code_' + identity}}} for identity in ids],
        'page_info': {'has_next_page': next_cursor is not None, 'end_cursor': next_cursor},
    }}}}


class ReelsQueriesTests(unittest.TestCase):
    def test_browser_initial_query_has_distinct_user_id_variables(self):
        doc_id, variables = query('123')
        self.assertEqual(INITIAL_DOC_ID, doc_id)
        self.assertEqual({'data': {'include_feed_video': True, 'page_size': 12,
                                  'target_user_id': '123'}, 'user_id': '123',
                          PROVIDER: True}, variables)

    def test_browser_pagination_uses_connection_id_and_cursor(self):
        doc_id, variables = query('123', 'next-page')
        self.assertEqual(CONNECTION_DOC_ID, doc_id)
        self.assertEqual('next-page', variables['after'])
        self.assertEqual('123', variables['id'])
        self.assertEqual(3, variables['first'])
        self.assertEqual(12, variables['data']['page_size'])
        self.assertNotIn('user_id', variables)
        self.assertTrue(variables[PROVIDER])

    def test_two_pages_keep_media_wrappers_and_bound_route(self):
        context = Mock()
        context.doc_id_graphql_query.side_effect = [page(('100',), 'next-page'), page(('101',))]
        result = list(pages(context, '123', 'example'))
        self.assertEqual(['100', '101'], [p['nodes'][0]['media']['pk'] for p in result])
        self.assertFalse(result[0]['complete'])
        self.assertTrue(result[1]['complete'])
        calls = context.doc_id_graphql_query.call_args_list
        self.assertEqual([INITIAL_DOC_ID, CONNECTION_DOC_ID], [c.args[0] for c in calls])
        self.assertEqual('next-page', calls[1].args[1]['after'])
        for call in calls:
            self.assertEqual({'referer': 'https://www.instagram.com/example/reels/', 'web_api': True}, call.kwargs)

    def test_resume_uses_connection_operation(self):
        context = Mock()
        context.doc_id_graphql_query.return_value = page()
        list(pages(context, '123', 'example', start_cursor='saved-cursor'))
        self.assertEqual(CONNECTION_DOC_ID, context.doc_id_graphql_query.call_args.args[0])
        self.assertEqual('saved-cursor', context.doc_id_graphql_query.call_args.args[1]['after'])

    def test_empty_final_page_is_valid(self):
        self.assertEqual(([], None), parse_page(page(())))
        context = Mock()
        context.doc_id_graphql_query.return_value = page(())
        self.assertTrue(list(pages(context, '123', 'example'))[0]['complete'])

    def test_errors_missing_connection_and_unknown_page_state_fail_closed(self):
        invalid = [None, {}, {'data': None}, {'errors': [{'message': 'synthetic'}], **page()},
                   {'data': {'fetch__XDTUserDict': {'other': {'edges': []}}}}]
        bad = page(); bad['data']['fetch__XDTUserDict']['clips_connection']['page_info']['has_next_page'] = 0
        invalid.append(bad)
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ReelsError): parse_page(payload)

    def test_partial_invalid_page_never_yields_valid_prefix(self):
        bad = page(('100', '101'))
        bad['data']['fetch__XDTUserDict']['clips_connection']['edges'][1]['node']['media']['pk'] = 'invalid'
        context = Mock(); context.doc_id_graphql_query.return_value = bad
        with self.assertRaisesRegex(ReelsError, 'identity'): next(pages(context, '123', 'example'))

    def test_invalid_media_membership_and_cursor_are_rejected(self):
        invalid = [page(('100', '100')), page((), 'next')]
        for key, value in [('pk', ''), ('pk', None), ('code', ''), ('code', 'bad/code')]:
            bad = page(); bad['data']['fetch__XDTUserDict']['clips_connection']['edges'][0]['node']['media'][key] = value
            invalid.append(bad)
        bad = page(); bad['data']['fetch__XDTUserDict']['clips_connection']['page_info'] = {'has_next_page': True}
        invalid.append(bad)
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ReelsError): parse_page(payload)

    def test_repeated_cursor_and_repeated_pages_stop(self):
        for responses in ([page(('100',), 'next'), page(('101',), 'next')],
                          [page(('100',), 'next'), page(('100',), 'other')]):
            context = Mock(); context.doc_id_graphql_query.side_effect = responses
            stream = pages(context, '123', 'example')
            next(stream)
            with self.assertRaisesRegex(ReelsError, 'repeated'): next(stream)

    def test_overlapping_pages_deduplicate_only_known_media(self):
        context = Mock()
        context.doc_id_graphql_query.side_effect = [page(('100', '101'), 'next'), page(('101', '102'))]
        self.assertEqual([['100', '101'], ['102']],
            [[node['media']['pk'] for node in p['nodes']] for p in pages(context, '123', 'example')])

    def test_budget_exhaustion_does_not_report_complete(self):
        context = Mock(); context.doc_id_graphql_query.return_value = page(('100',), 'next')
        stream = pages(context, '123', 'example', max_pages=1)
        self.assertFalse(next(stream)['complete'])
        with self.assertRaisesRegex(ReelsError, 'budget_reached'): next(stream)
        context.doc_id_graphql_query.assert_called_once()

    def test_transport_restriction_is_not_retried_or_swallowed(self):
        context = Mock(); error = RuntimeError('restriction')
        context.doc_id_graphql_query.side_effect = error
        with self.assertRaises(RuntimeError) as caught: list(pages(context, '123', 'example'))
        self.assertIs(error, caught.exception)
        context.doc_id_graphql_query.assert_called_once()

    def test_inputs_validated_before_request_and_payload_not_mutated(self):
        context = Mock()
        for user_id, name in [('bad', 'example'), ('123', '../example'), ('123', '..')]:
            with self.assertRaises(ReelsError): list(pages(context, user_id, name))
        for budget in (0, 121, True):
            with self.assertRaises(ReelsError): list(pages(context, '123', 'example', max_pages=budget))
        context.doc_id_graphql_query.assert_not_called()
        payload = page(); original = copy.deepcopy(payload)
        parse_page(payload); self.assertEqual(original, payload)


@unittest.skipUnless(version('gallery-dl') == '1.32.17', 'requires deployed gallery-dl version')
class GalleryReelsIntegrationTests(unittest.TestCase):
    def setUp(self):
        from gallery_dl import exception
        from gallery_dl.extractor.instagram import InstagramAPI, InstagramExtractor
        from instagram_archive_core.instagram_web_gallery import install_web_graphql
        self.api_class, self.extractor_class = InstagramAPI, InstagramExtractor
        self.previous = {name: getattr(InstagramAPI, name) for name in
            ('user_feed', 'user_reels', '_user_by_web_impl', '_user_by_id_impl',
             '_call_graphql', '_pagination_graphql', 'media')}
        self.init, self.finalize = InstagramExtractor._init, InstagramExtractor.finalize
        self.addCleanup(self.restore)
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        install_web_graphql(Path(self.directory.name) / 'rate.sqlite3')
        self.client = Mock()
        self.extractor = SimpleNamespace(_init_cursor=Mock(return_value=None),
            _update_cursor=Mock(side_effect=lambda value: value))
        self.api = object.__new__(InstagramAPI)
        self.api.extractor, self.api.exc = self.extractor, exception
        self.api._vm_web_client = self.client
        self.api.user = Mock(return_value={'id': '123', 'username': 'example'})

    def restore(self):
        for name, value in self.previous.items(): setattr(self.api_class, name, value)
        self.extractor_class._init, self.extractor_class.finalize = self.init, self.finalize
        if hasattr(self.api_class, '_vm_web_graphql'): del self.api_class._vm_web_graphql

    def test_user_reels_installed_and_cursor_preserved(self):
        self.client.doc_id_graphql_query.side_effect = [page(('100',), 'next'), page(('101',))]
        self.assertEqual(['100', '101'], [node['media']['pk'] for node in self.api.user_reels('example')])
        self.extractor._update_cursor.assert_called_once_with('next')
        self.assertEqual([INITIAL_DOC_ID, CONNECTION_DOC_ID],
            [call.args[0] for call in self.client.doc_id_graphql_query.call_args_list])

    def test_single_media_uses_authenticated_web_endpoint(self):
        self.client.doc_id_graphql_query.return_value = {'data': {
            'xdt_api__v1__media__shortcode__web_info': {'items': [{'pk': '100', 'code': 'example'}]}}}
        self.assertEqual('100', next(self.api.media('example'))['pk'])
        self.assertEqual(True, self.client.doc_id_graphql_query.call_args.kwargs['web_api'])

    def test_auth_failure_aborts_without_fallback(self):
        from instagram_archive_core.instagram_web_client import WebStop
        self.client.doc_id_graphql_query.side_effect = WebStop('AuthRequired: synthetic')
        with self.assertRaises(self.api.exc.AbortExtraction): list(self.api.user_reels('example'))
        self.client.doc_id_graphql_query.assert_called_once()


if __name__ == '__main__':
    unittest.main()
