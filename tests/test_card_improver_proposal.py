# -*- coding: utf-8 -*-
import json
import unittest


class FakeProduct:
    def __init__(self):
        self.id = 5
        self.title = 'Платье летнее'
        self.photos_json = json.dumps(['a.jpg', 'b.jpg', 'c.jpg'])


class CollectWeakDimensionsTest(unittest.TestCase):
    def test_returns_warning_and_error_sorted_by_impact(self):
        from services.card_improver import collect_weak_dimensions
        detail = {'dimensions': {
            'photos':          {'score': 0,  'status': 'error',   'weight': 20, 'hint': 'нет фото'},
            'characteristics': {'score': 50, 'status': 'warning', 'weight': 25, 'hint': 'мало'},
            'title':           {'score': 100,'status': 'ok',      'weight': 10, 'hint': ''},
            'description':     {'score': 0,  'status': 'error',   'weight': 15, 'hint': 'нет'},
        }}
        weak = collect_weak_dimensions(detail)
        # impact = weight*(100-score): photos=2000, chars=1250, description=1500
        self.assertEqual(weak, ['photos', 'description', 'characteristics'])
        self.assertNotIn('title', weak)

    def test_empty_when_all_ok(self):
        from services.card_improver import collect_weak_dimensions
        detail = {'dimensions': {'title': {'score': 100, 'status': 'ok', 'weight': 10, 'hint': ''}}}
        self.assertEqual(collect_weak_dimensions(detail), [])


class BuildProposalTest(unittest.TestCase):
    def test_photo_reorder_proposal(self):
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        task_results = [{
            'agent': 'photo-optimizer',
            'result': {'recommended_order': [2, 0, 1], 'recommendations': ['Главное фото — на белом фоне']},
        }]
        proposal = build_proposal_from_tasks(product, task_results)
        self.assertIn('photos', proposal)
        self.assertEqual(proposal['photos']['proposed'], ['c.jpg', 'a.jpg', 'b.jpg'])
        self.assertEqual(proposal['photos']['current'], ['a.jpg', 'b.jpg', 'c.jpg'])
        self.assertEqual(proposal['photos']['dimension'], 'photos')
        self.assertEqual(proposal['photos']['source'], 'photo-optimizer')

    def test_photo_reorder_skipped_when_order_equals_current(self):
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        task_results = [{'agent': 'photo-optimizer',
                         'result': {'recommended_order': [0, 1, 2]}}]
        proposal = build_proposal_from_tasks(product, task_results)
        self.assertNotIn('photos', proposal)

    def test_photo_reorder_ignores_out_of_range_indices(self):
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        task_results = [{'agent': 'photo-optimizer',
                         'result': {'recommended_order': [2, 0, 1, 9]}}]
        proposal = build_proposal_from_tasks(product, task_results)
        # 9 вне диапазона → отбрасываем, остаётся валидная перестановка
        self.assertEqual(proposal['photos']['proposed'], ['c.jpg', 'a.jpg', 'b.jpg'])

    def test_ignores_non_writing_diagnostic_agents(self):
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        task_results = [{'agent': 'card-doctor',
                         'result': {'recommendations': ['Добавьте описание']}}]
        proposal = build_proposal_from_tasks(product, task_results)
        self.assertEqual(proposal, {})


if __name__ == '__main__':
    unittest.main()
