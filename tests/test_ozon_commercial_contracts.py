# -*- coding: utf-8 -*-
"""Fail-closed contracts for Ozon prices, stocks and warehouses."""

from decimal import Decimal
import unittest

from services.ozon_commercial_contracts import (
    OzonCommercialPayloadError,
    OzonCommercialProtocolError,
    OzonPriceContract,
    OzonStockContract,
    OzonWarehouseContract,
)


class OzonPriceContractTest(unittest.TestCase):
    def _item(self, **overrides):
        item = {
            "offer_id": "seller-offer-1",
            "product_id": "12345",
            "price": "1250.50",
            "currency_code": "RUB",
        }
        item.update(overrides)
        return item

    def test_build_payload_is_whitelist_only_and_canonical(self):
        payload = OzonPriceContract.build_payload([
            self._item(old_price=Decimal("1500.00")),
        ])
        self.assertEqual(payload, {
            "prices": [{
                "offer_id": "seller-offer-1",
                "product_id": 12345,
                "price": "1250.5",
                "currency_code": "RUB",
                "old_price": "1500",
            }],
        })

        for invalid in (
            self._item(marketing_price="1"),
            self._item(images360=[]),
            self._item(currency_code="USD"),
            self._item(product_id=True),
            self._item(product_id=12.5),
            self._item(price=False),
            self._item(price="0"),
            self._item(price="NaN"),
            self._item(price="1.001"),
            self._item(old_price="1000"),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(OzonCommercialPayloadError):
                    OzonPriceContract.build_payload([invalid])

    def test_build_payload_rejects_duplicates_and_oversized_batches(self):
        with self.assertRaises(OzonCommercialPayloadError):
            OzonPriceContract.build_payload([self._item(), self._item()])
        with self.assertRaises(OzonCommercialPayloadError):
            OzonPriceContract.build_payload([])
        with self.assertRaises(OzonCommercialPayloadError):
            OzonPriceContract.build_payload([
                self._item(
                    offer_id=f"offer-{index}",
                    product_id=str(1000 + index),
                )
                for index in range(OzonPriceContract.MAX_BATCH + 1)
            ])

    def test_response_requires_exact_identity_set(self):
        expected = OzonPriceContract.build_payload([
            self._item(),
            self._item(offer_id="seller-offer-2", product_id="54321"),
        ])
        response = {
            "result": [
                {
                    "offer_id": "seller-offer-2",
                    "product_id": 54321,
                    "updated": False,
                    "errors": [{"code": "INVALID", "message": "Rejected"}],
                },
                {
                    "offer_id": "seller-offer-1",
                    "product_id": 12345,
                    "updated": True,
                    "errors": [],
                },
            ],
        }
        normalized = OzonPriceContract.normalize_response(response, expected)
        self.assertEqual(normalized["updated"], 1)
        self.assertEqual(normalized["failed"], 1)

        invalid_responses = (
            {"result": response["result"][:1]},
            {"result": [response["result"][1], response["result"][1]]},
            {"result": [
                response["result"][0],
                {**response["result"][1], "product_id": 99999},
            ]},
            {"result": [
                response["result"][0],
                {**response["result"][1], "updated": 1},
            ]},
            {"result": [
                response["result"][0],
                {**response["result"][1], "errors": None},
            ]},
        )
        for invalid in invalid_responses:
            with self.subTest(invalid=invalid):
                with self.assertRaises(OzonCommercialProtocolError):
                    OzonPriceContract.normalize_response(invalid, expected)


class OzonStockContractTest(unittest.TestCase):
    def _item(self, **overrides):
        item = {
            "offer_id": "seller-offer-1",
            "product_id": "12345",
            "warehouse_id": "7001",
            "stock": 8,
        }
        item.update(overrides)
        return item

    def test_write_payload_requires_exact_fields_and_integer_stock(self):
        self.assertEqual(
            OzonStockContract.build_payload([self._item()]),
            {"stocks": [{
                "offer_id": "seller-offer-1",
                "product_id": 12345,
                "warehouse_id": 7001,
                "stock": 8,
            }]},
        )
        for invalid in (
            self._item(stock=True),
            self._item(stock=1.0),
            self._item(stock="1"),
            self._item(stock=-1),
            self._item(warehouse_id=False),
            {**self._item(), "present": 8},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(OzonCommercialPayloadError):
                    OzonStockContract.build_payload([invalid])

    def test_write_response_requires_exact_offer_product_warehouse_set(self):
        expected = OzonStockContract.build_payload([
            self._item(),
            self._item(
                offer_id="seller-offer-2",
                product_id="12346",
                warehouse_id="7002",
                stock=0,
            ),
        ])
        valid_results = [
            {
                "offer_id": "seller-offer-1",
                "product_id": 12345,
                "warehouse_id": 7001,
                "updated": True,
                "errors": [],
            },
            {
                "offer_id": "seller-offer-2",
                "product_id": 12346,
                "warehouse_id": 7002,
                "updated": True,
                "errors": [],
            },
        ]
        normalized = OzonStockContract.normalize_response(
            {"result": list(reversed(valid_results))}, expected
        )
        self.assertEqual(normalized["updated"], 2)
        self.assertEqual(normalized["failed"], 0)

        for invalid in (
            {"result": valid_results[:1]},
            {"result": [valid_results[0], valid_results[0]]},
            {"result": [valid_results[0], {**valid_results[1], "warehouse_id": 9}]},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(OzonCommercialProtocolError):
                    OzonStockContract.normalize_response(invalid, expected)

    def test_fbs_page_is_strict_and_contains_free_stock(self):
        response = {
            "products": [{
                "sku": 999,
                "offer_id": "seller-offer-1",
                "product_id": 12345,
                "warehouse_id": 7001,
                "warehouse_name": "Main FBS",
                "present": 11,
                "reserved": 3,
                "free_stock": 8,
            }],
            "cursor": "next-page",
            "has_next": True,
        }
        normalized = OzonStockContract.normalize_fbs_page(response)
        self.assertEqual(normalized["products"][0]["free_stock"], 8)
        self.assertEqual(normalized["products"][0]["reserved"], 3)

        for invalid in (
            {**response, "cursor": "", "has_next": True},
            {**response, "has_next": 1},
            {**response, "products": [response["products"][0]] * 2},
            {**response, "products": [{**response["products"][0], "free_stock": "8"}]},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(OzonCommercialProtocolError):
                    OzonStockContract.normalize_fbs_page(invalid)


class OzonWarehouseContractTest(unittest.TestCase):
    def test_request_is_bounded_and_cursor_only(self):
        self.assertEqual(
            OzonWarehouseContract.request_payload(cursor="opaque"),
            {"limit": 100, "cursor": "opaque"},
        )
        with self.assertRaises(OzonCommercialPayloadError):
            OzonWarehouseContract.request_payload(cursor=123)

    def test_page_keeps_operational_fields_but_drops_contact_data(self):
        raw = {
            "warehouses": [{
                "warehouse_id": 7001,
                "name": "Main FBS",
                "status": "created",
                "warehouse_type": "ORDINARY",
                "phone": "+70000000000",
                "courier_phones": ["+71111111111"],
                "address_info": {"address": "private"},
                "is_rfbs": False,
                "is_express": False,
                "has_postings_limit": True,
                "postings_limit": 100,
            }],
            "cursor": None,
            "has_next": False,
        }
        normalized = OzonWarehouseContract.normalize_page(raw)
        warehouse = normalized["warehouses"][0]
        self.assertEqual(warehouse["warehouse_id"], "7001")
        self.assertEqual(warehouse["limits"], {"postings_limit": 100})
        self.assertEqual(warehouse["flags"]["is_rfbs"], False)
        self.assertNotIn("phone", warehouse)
        self.assertNotIn("address_info", warehouse)

    def test_page_rejects_invalid_pagination_and_duplicate_ids(self):
        item = {"warehouse_id": 7001, "name": "Main FBS"}
        for invalid in (
            {"warehouses": [item], "cursor": "", "has_next": True},
            {"warehouses": [item], "cursor": "", "has_next": 1},
            {"warehouses": [item, item], "cursor": "", "has_next": False},
            {"warehouses": [{**item, "warehouse_id": True}], "cursor": "", "has_next": False},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(OzonCommercialProtocolError):
                    OzonWarehouseContract.normalize_page(invalid)


if __name__ == "__main__":
    unittest.main()
