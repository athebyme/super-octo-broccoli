# -*- coding: utf-8 -*-
"""Админский сервис compliance-дефолтов Ozon."""
import unittest


class SaveDecisionValidationTestCase(unittest.TestCase):
    def test_empty_rationale_is_rejected(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            validate_decision_input,
        )
        with self.assertRaises(OzonComplianceAdminError):
            validate_decision_input(
                product_type_id=1609, tnved_code='3307900008', rationale='  ',
            )

    def test_non_numeric_code_is_rejected(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            validate_decision_input,
        )
        with self.assertRaises(OzonComplianceAdminError):
            validate_decision_input(
                product_type_id=1609, tnved_code='нет кода', rationale='ок',
            )

    def test_valid_input_returns_normalized_code(self):
        from services.ozon_compliance_admin import validate_decision_input
        cleaned = validate_decision_input(
            product_type_id=1609,
            tnved_code=' 3307 90 0008 ',
            rationale='Лубриканты, косметические средства',
        )
        self.assertEqual(cleaned['tnved_code'], '3307900008')
        self.assertEqual(cleaned['product_type_id'], 1609)


if __name__ == '__main__':
    unittest.main()
