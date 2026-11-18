import unittest
from unittest.mock import Mock,patch
from gallery_dl import exception
from instagram_archive_core import gallery_worker

class BoundaryTests(unittest.TestCase):
    def extractor(self):
        value=Mock(subcategory='posts')
        value.config=lambda name,default=None: {'archive-target':'fixture','archive-full-check':False}.get(name,default)
        value._user={'username':'fixture'}
        return value
    def post(self):return {'post_id':'55','username':'fixture','_files':[{'media_id':'11'}]}
    def test_known_complete_post_stops_history(self):
        wrapped=gallery_worker.parse_with_boundary(lambda self,raw:self.post_result)
        obj=self.extractor();obj.post_result=self.post()
        with patch.object(gallery_worker,'known_media',return_value=True),patch.object(gallery_worker,'known_post_id',return_value=True),patch.object(gallery_worker,'remember_target'),patch.object(gallery_worker,'save_post'):
            with self.assertRaises(exception.StopExtraction):wrapped(obj,{'pk':'11'})
    def test_missing_carousel_child_cannot_stop_history(self):
        wrapped=gallery_worker.parse_with_boundary(lambda self,raw:self.post_result)
        obj=self.extractor();obj.post_result=self.post()
        with patch.object(gallery_worker,'known_media',side_effect=lambda i:i=='11'),patch.object(gallery_worker,'known_post_id') as known:
            result=wrapped(obj,{'carousel_media':[{'pk':'11'},{'pk':'12'}]})
        known.assert_not_called();self.assertEqual(result['_archive_post_ids'],['11','12'])
    def test_new_target_membership_cannot_stop_on_shared_media(self):
        wrapped=gallery_worker.parse_with_boundary(lambda self,raw:self.post_result)
        obj=self.extractor();obj.post_result=self.post()
        with patch.object(gallery_worker,'known_media',return_value=True),patch.object(gallery_worker,'known_post_id',return_value=False),patch.object(gallery_worker,'remember_target') as remember,patch.object(gallery_worker,'save_post'):
            wrapped(obj,{'pk':'11'})
        remember.assert_called_once()
