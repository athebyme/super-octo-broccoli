"""Synthetic tests for the current read-only Ozon accrual contracts."""

from datetime import date, datetime
from decimal import Decimal
import unittest

from services.ozon_finance_contracts import (
    OzonFinanceContractError,
    build_accrual_by_day_request,
    build_accrual_postings_request,
    build_accrual_types_request,
    normalize_accrual_by_day_response,
    normalize_accrual_postings_response,
    normalize_accrual_types_response,
)


class OzonFinanceRequestContractTest(unittest.TestCase):
    def test_current_requests_are_exact_and_bounded(self):
        self.assertEqual(build_accrual_types_request(), {})
        self.assertEqual(
            build_accrual_by_day_request(
                accrual_date=date(2026, 7, 15),
                last_id="next-1",
            ),
            {"date": "2026-07-15", "last_id": "next-1"},
        )
        self.assertEqual(
            build_accrual_postings_request(
                posting_numbers=["100-1-1", "200-2-1"],
            ),
            {"posting_numbers": ["100-1-1", "200-2-1"]},
        )

    def test_requests_reject_loose_or_duplicate_values(self):
        with self.assertRaises(OzonFinanceContractError):
            build_accrual_by_day_request(accrual_date=datetime(2026, 7, 15))
        with self.assertRaises(OzonFinanceContractError):
            build_accrual_by_day_request(
                accrual_date="2026-07-15",
                last_id=True,
            )
        with self.assertRaises(OzonFinanceContractError):
            build_accrual_postings_request(posting_numbers=["1-1", "1-1"])
        with self.assertRaises(OzonFinanceContractError):
            build_accrual_postings_request(posting_numbers=[])


class OzonFinanceResponseContractTest(unittest.TestCase):
    @staticmethod
    def _accrual(accrual_id=9001):
        return {
            "accrual_id": accrual_id,
            "date": "2026-07-15",
            "unit_number": "100-1-1",
            "accrued_category": "POSTING",
            "total_amount": {"amount": "75.50", "currency": "RUB"},
            "posting": {
                "delivery_schema": "FBS",
                "products": [{
                    "sku": 101,
                    "commission": {
                        "sale_amount": {"amount": "100", "currency": "RUB"},
                        "commission": {"amount": "-20", "currency": "RUB"},
                    },
                    "delivery": {
                        "services": [{
                            "type_id": 7,
                            "accrued": {"amount": "-4.50", "currency": "RUB"},
                        }],
                    },
                }],
            },
            "item_fees": {
                "fees": [{
                    "sku": 101,
                    "fees": [{
                        "type_id": 8,
                        "accrued": {"amount": "-20", "currency": "RUB"},
                    }],
                }],
            },
            "non_item_fee": None,
            # Unknown provider/user fields never survive normalization.
            "buyer": {"name": "must not survive"},
            "raw_transaction": {"secret": "must not survive"},
        }

    def test_by_day_uses_new_accrual_id_and_signed_money(self):
        page = normalize_accrual_by_day_response(
            {"accruals": [self._accrual()], "last_id": "cursor-2"},
            requested_date=date(2026, 7, 15),
        )
        self.assertTrue(page["has_next"])
        self.assertEqual(page["next_last_id"], "cursor-2")
        row = page["rows"][0]
        self.assertEqual(row["accrual_id"], "9001")
        self.assertEqual(row["amount"], Decimal("75.5000"))
        self.assertEqual(row["skus"], ["101"])
        self.assertEqual(
            [component["amount"] for component in row["components"]],
            [Decimal("-4.5000"), Decimal("-20.0000")],
        )
        self.assertNotIn("buyer", row)
        self.assertNotIn("commission", row)
        self.assertNotIn("raw_transaction", row)

    def test_negative_top_level_amount_is_valid_and_not_absolute(self):
        raw = self._accrual()
        raw["accrued_category"] = "NON_ITEM"
        raw["total_amount"]["amount"] = "-123.45"
        raw["posting"] = None
        raw["item_fees"] = None
        raw["non_item_fee"] = {
            "type_id": 55,
            "accrued": {"amount": "-123.45", "currency": "RUB"},
        }
        page = normalize_accrual_by_day_response(
            {"accruals": [raw], "last_id": None},
            requested_date="2026-07-15",
        )
        self.assertFalse(page["has_next"])
        self.assertEqual(page["rows"][0]["amount"], Decimal("-123.4500"))

    def test_retired_top_level_type_id_is_rejected(self):
        raw = self._accrual()
        raw["type_id"] = raw.pop("accrual_id")
        with self.assertRaisesRegex(OzonFinanceContractError, "retired"):
            normalize_accrual_by_day_response(
                {"accruals": [raw], "last_id": None},
                requested_date="2026-07-15",
            )

    def test_cursor_duplicates_date_drift_and_unknown_money_fail_page(self):
        with self.assertRaises(OzonFinanceContractError):
            normalize_accrual_by_day_response(
                {"accruals": [self._accrual(), self._accrual()], "last_id": None},
                requested_date="2026-07-15",
            )
        with self.assertRaises(OzonFinanceContractError):
            normalize_accrual_by_day_response(
                {"accruals": [self._accrual()], "last_id": "same"},
                requested_date="2026-07-15",
                requested_last_id="same",
            )
        wrong_day = self._accrual()
        wrong_day["date"] = "2026-07-14"
        with self.assertRaises(OzonFinanceContractError):
            normalize_accrual_by_day_response(
                {"accruals": [wrong_day], "last_id": None},
                requested_date="2026-07-15",
            )
        unknown_money = self._accrual()
        unknown_money["total_amount"]["raw"] = 1
        with self.assertRaises(OzonFinanceContractError):
            normalize_accrual_by_day_response(
                {"accruals": [unknown_money], "last_id": None},
                requested_date="2026-07-15",
            )

    def test_nonempty_uncontracted_container_fees_fail_closed(self):
        raw = self._accrual()
        raw["container_fees"] = [{"amount": "1"}]
        with self.assertRaisesRegex(OzonFinanceContractError, "container_fees"):
            normalize_accrual_by_day_response(
                {"accruals": [raw], "last_id": None},
                requested_date="2026-07-15",
            )

    def test_type_dictionary_is_strict_and_deduplicated(self):
        normalized = normalize_accrual_types_response({
            "accrual_types": [{
                "id": 7,
                "name": "Synthetic delivery fee",
                "description": "Synthetic description",
            }],
        })
        self.assertEqual(normalized["types"][0]["type_id"], 7)
        with self.assertRaises(OzonFinanceContractError):
            normalize_accrual_types_response({
                "accrual_types": [
                    {"id": 7, "name": "A"},
                    {"id": 7, "name": "B"},
                ],
            })

    def test_posting_response_cannot_escape_exact_request_scope(self):
        response = {
            "posting_accruals": [{
                "posting_number": "100-1-1",
                "accruals": [{
                    "accrual_date": "2026-07-15",
                    "accrued": {"amount": "-10", "currency": "RUB"},
                    "quantity": 1,
                    "seller_price": {"amount": "100", "currency": "RUB"},
                    "sku": 101,
                    "type_id": 7,
                }],
            }],
        }
        normalized = normalize_accrual_postings_response(
            response,
            requested_posting_numbers=["100-1-1"],
        )
        self.assertEqual(
            normalized["posting_accruals"][0]["accruals"][0]["amount"],
            Decimal("-10.0000"),
        )
        response["posting_accruals"][0]["posting_number"] = "foreign-1"
        with self.assertRaises(OzonFinanceContractError):
            normalize_accrual_postings_response(
                response,
                requested_posting_numbers=["100-1-1"],
            )


if __name__ == "__main__":
    unittest.main()
