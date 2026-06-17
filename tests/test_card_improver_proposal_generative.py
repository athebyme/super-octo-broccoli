# -*- coding: utf-8 -*-
"""
Тест маппинга результатов генеративных агентов в build_proposal_from_tasks.
Проверяет только логику маппинга (без LLM): mock-результаты агентов → proposal.
"""
import json
import unittest


class FakeProduct:
    def __init__(self):
        self.title = 'Старый'
        self.brand = 'OldBrand'
        self.description = 'кратко'
        self.subject_id = 100
        self.photos_json = '[]'


class GenerativeProposalTest(unittest.TestCase):

    def test_maps_seo_writer_result(self):
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        results = [{'agent': 'seo-writer',
                    'result': {'title': 'Новый SEO-заголовок', 'description': 'Д' * 300,
                               'keywords': ['x'], 'confidence': 0.9}}]
        proposal = build_proposal_from_tasks(product, results)
        self.assertEqual(proposal['title']['proposed'], 'Новый SEO-заголовок')
        self.assertEqual(proposal['title']['current'], 'Старый')
        self.assertEqual(proposal['title']['dimension'], 'title')
        self.assertEqual(proposal['title']['source'], 'seo-writer')
        self.assertIn('description', proposal)
        self.assertEqual(proposal['description']['dimension'], 'description')
        self.assertEqual(proposal['description']['source'], 'seo-writer')

    def test_maps_brand_resolver_result(self):
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        results = [{'agent': 'brand-resolver', 'result': {'brand': 'Nike', 'confidence': 0.8}}]
        proposal = build_proposal_from_tasks(product, results)
        self.assertEqual(proposal['brand']['proposed'], 'Nike')
        self.assertEqual(proposal['brand']['dimension'], 'brand')
        self.assertEqual(proposal['brand']['source'], 'brand-resolver')

    def test_skips_low_confidence_or_same_value(self):
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        # Совпадает с текущим → не предлагаем
        results = [{'agent': 'brand-resolver', 'result': {'brand': 'OldBrand', 'confidence': 0.9}}]
        self.assertNotIn('brand', build_proposal_from_tasks(product, results))

    def test_skips_low_confidence_below_threshold(self):
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        # Низкая уверенность → не предлагаем
        results = [{'agent': 'brand-resolver', 'result': {'brand': 'Nike', 'confidence': 0.5}}]
        self.assertNotIn('brand', build_proposal_from_tasks(product, results))

    def test_maps_category_mapper_result(self):
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        results = [{'agent': 'category-mapper', 'result': {'subject_id': 555, 'confidence': 0.95}}]
        proposal = build_proposal_from_tasks(product, results)
        self.assertEqual(proposal['subject_id']['proposed'], 555)
        self.assertEqual(proposal['subject_id']['dimension'], 'category')
        self.assertEqual(proposal['subject_id']['source'], 'category-mapper')

    def test_maps_characteristics_filler_result(self):
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        chars = [{'id': 1, 'value': 'Красный'}]
        results = [{'agent': 'characteristics-filler',
                    'result': {'characteristics': chars, 'confidence': 0.85}}]
        proposal = build_proposal_from_tasks(product, results)
        self.assertIn('characteristics', proposal)
        self.assertEqual(proposal['characteristics']['proposed'], chars)
        self.assertEqual(proposal['characteristics']['dimension'], 'characteristics')
        self.assertEqual(proposal['characteristics']['source'], 'characteristics-filler')

    def test_photo_optimizer_still_works(self):
        """Проверяет, что photo-optimizer mapping не сломан (задача 3.2)."""
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        product.photos_json = json.dumps(['a.jpg', 'b.jpg', 'c.jpg'])
        results = [{'agent': 'photo-optimizer', 'result': {'recommended_order': [2, 0, 1]}}]
        proposal = build_proposal_from_tasks(product, results)
        self.assertIn('photos', proposal)
        self.assertEqual(proposal['photos']['proposed'], ['c.jpg', 'a.jpg', 'b.jpg'])
        self.assertEqual(proposal['photos']['source'], 'photo-optimizer')

    def test_skips_empty_proposed_values(self):
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        results = [{'agent': 'seo-writer',
                    'result': {'title': '', 'description': None, 'confidence': 0.9}}]
        proposal = build_proposal_from_tasks(product, results)
        self.assertNotIn('title', proposal)
        self.assertNotIn('description', proposal)

    def test_confidence_boundary_at_0_7(self):
        """confidence == 0.7 должна пройти порог."""
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        results = [{'agent': 'brand-resolver', 'result': {'brand': 'Nike', 'confidence': 0.7}}]
        proposal = build_proposal_from_tasks(product, results)
        self.assertIn('brand', proposal)

    def test_multiple_agents_combined(self):
        """Несколько агентов одновременно — все маппятся в один proposal."""
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        results = [
            {'agent': 'seo-writer',
             'result': {'title': 'Лучший заголовок', 'description': 'Отличное описание' * 20,
                        'confidence': 0.85}},
            {'agent': 'brand-resolver', 'result': {'brand': 'Nike', 'confidence': 0.8}},
            {'agent': 'category-mapper', 'result': {'subject_id': 999, 'confidence': 0.92}},
        ]
        proposal = build_proposal_from_tasks(product, results)
        self.assertIn('title', proposal)
        self.assertIn('brand', proposal)
        self.assertIn('subject_id', proposal)


if __name__ == '__main__':
    unittest.main()
