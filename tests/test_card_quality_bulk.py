# tests/test_card_quality_bulk.py
import json
import unittest
from unittest.mock import patch, MagicMock


class BulkCandidatesTest(unittest.TestCase):
    def test_collects_top_n_weak_and_reports_total(self):
        from routes.card_quality import _collect_bulk_candidates
        # 35 слабых карточек, top-N=30 показываем, total_weak=35
        products = []
        for i in range(35):
            p = MagicMock(); p.id = i; p.nm_id = 1000 + i
            p.quality_score = 30.0; p.nm_rating = 5.0
            products.append(p)

        with patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.card_quality_detail') as mock_detail, \
             patch('routes.card_quality.collect_weak_dimensions', return_value=['photos']), \
             patch('routes.card_quality.get_enrichment_service') as mock_es:
            # query.filter(...).filter(...).order_by(...).limit(30).all() → первые 30
            chain = MockProduct.query.filter.return_value.filter.return_value.order_by.return_value
            chain.limit.return_value.all.return_value = products[:30]
            # count() слабых = 35
            MockProduct.query.filter.return_value.filter.return_value.count.return_value = 35
            mock_detail.side_effect = lambda p: {'product_id': p.id, 'nm_id': p.nm_id,
                                                 'quality_score': p.quality_score, 'dimensions': {},
                                                 'title': 'T', 'vendor_code': 'VC'}
            svc = MagicMock(); svc.find_supplier_data.return_value = None
            mock_es.return_value = svc

            res = _collect_bulk_candidates(seller_id=7, limit=30)
            self.assertEqual(res['shown'], 30)
            self.assertEqual(res['total_weak'], 35)
            self.assertEqual(len(res['candidates']), 30)
            self.assertEqual(res['candidates'][0]['weak_dims'], ['photos'])

    def test_candidate_includes_supplier_diff_when_available(self):
        from routes.card_quality import _collect_bulk_candidates
        p = MagicMock(); p.id = 1; p.nm_id = 1001
        with patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.card_quality_detail',
                   return_value={'product_id': 1, 'nm_id': 1001, 'quality_score': 20,
                                 'dimensions': {}, 'title': 'T', 'vendor_code': 'VC'}), \
             patch('routes.card_quality.collect_weak_dimensions', return_value=['title']), \
             patch('routes.card_quality.get_enrichment_service') as mock_es:
            chain = MockProduct.query.filter.return_value.filter.return_value.order_by.return_value
            chain.limit.return_value.all.return_value = [p]
            MockProduct.query.filter.return_value.filter.return_value.count.return_value = 1
            svc = MagicMock()
            svc.find_supplier_data.return_value = MagicMock()
            svc.build_preview.return_value = {'title': {'current': 'A', 'supplier': 'B', 'has_change': True}}
            mock_es.return_value = svc
            res = _collect_bulk_candidates(seller_id=7, limit=30)
            self.assertTrue(res['candidates'][0]['has_supplier'])
            self.assertEqual(res['candidates'][0]['supplier_diff']['title']['supplier'], 'B')


if __name__ == '__main__':
    unittest.main()
