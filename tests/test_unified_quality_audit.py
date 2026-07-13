# -*- coding: utf-8 -*-
"""Тесты детерминированного skill quality-audit."""
import unittest

from agents.unified import QualityAuditSkill, SKILL_CLASSES, _CHAINING_SOURCE_SKILLS


class _FakePlatform:
    def __init__(self, products):
        self._products = products
        self.calls = []

    def get_card_quality_brief(self, seller_id, product_ids=None, reason=None, limit=30):
        self.calls.append((seller_id, product_ids, reason, limit))
        return {
            'reason_labels': {'few_photos': 'Мало фото', 'no_views': 'Нет просмотров'},
            'total': len(self._products),
            'products': self._products,
        }


def _make_skill(products):
    # BaseAgent.__init__ builds a real PlatformClient/LLM from AgentConfig
    # (network- and env-dependent), which this deterministic unit test does
    # not need. execute_task only touches self.parse_input_data (staticmethod)
    # and self.platform, so bypass __init__ the same way test_base_agent.py's
    # _make_bare_agent does.
    skill = object.__new__(QualityAuditSkill)
    skill.platform = _FakePlatform(products)
    return skill


class TestQualityAuditSkill(unittest.TestCase):
    def test_registered(self):
        self.assertIn('quality-audit', SKILL_CLASSES)
        self.assertIs(SKILL_CLASSES['quality-audit'], QualityAuditSkill)

    def test_aggregates_reasons_and_collection(self):
        products = [
            {'id': 1, 'nm_id': 10, 'attention_reasons': ['few_photos', 'no_views'],
             'quality_impact': 30.0},
            {'id': 2, 'nm_id': 20, 'attention_reasons': ['few_photos'],
             'quality_impact': 20.0},
        ]
        skill = _make_skill(products)
        res = skill.execute_task({'id': 't1', 'seller_id': 1, 'input_data': {}})
        self.assertEqual(res['status'], 'completed')
        self.assertEqual(res['selected_product_ids'], [1, 2])
        self.assertEqual(res['entity_kind'], 'product')
        self.assertEqual(res['reason_summary'][0]['reason'], 'few_photos')
        self.assertEqual(res['reason_summary'][0]['count'], 2)
        self.assertIn('Мало фото', res['message'])
        self.assertEqual(res['_usage']['api_requests'], 1)
        self.assertEqual(res['_usage']['total_tokens'], 0)
        self.assertEqual(res['_usage']['mode'], 'deterministic_aggregate')
        self.assertEqual(skill.platform.calls[0], (1, None, None, 30))

    def test_empty_selection(self):
        skill = _make_skill([])
        res = skill.execute_task({'id': 't1', 'seller_id': 1, 'input_data': {}})
        self.assertEqual(res['status'], 'completed')
        self.assertEqual(res['selected_product_ids'], [])
        self.assertEqual(res['entity_kind'], 'product')
        self.assertEqual(res['_usage']['api_requests'], 1)

    def test_params_passthrough(self):
        skill = _make_skill([])
        skill.execute_task({'id': 't1', 'seller_id': 7, 'input_data': {
            'params': {'product_ids': [5, 6], 'reason': 'few_photos', 'limit': 10}}})
        self.assertEqual(skill.platform.calls[0], (7, [5, 6], 'few_photos', 10))

    def test_chaining_source_skills_includes_quality_audit(self):
        # Multi-step plan_request execution in UnifiedSellerAgent._plan_request
        # forwards selected_product_ids from one step's result into the next
        # step's product_ids only for skills in this source set. quality-audit
        # returns a selected_product_ids collection (see
        # test_aggregates_reasons_and_collection above) and per the card-quality
        # v2 spec must chain into a following step (e.g. content-writer), so it
        # belongs in the set alongside the existing sources.
        self.assertIn('quality-audit', _CHAINING_SOURCE_SKILLS)
        self.assertEqual(
            _CHAINING_SOURCE_SKILLS,
            {'candidate-selector', 'supplier-audit', 'quality-audit'},
        )


if __name__ == '__main__':
    unittest.main()
