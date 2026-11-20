"""Focused coverage for Ozon's ``total`` to ``total_items`` transition."""

from types import SimpleNamespace

import pytest

from models import BackgroundJob, MarketplaceCatalogSync, MarketplaceListing, db
from services.marketplace_commercial import (
    MarketplaceCommercialConflict,
    MarketplaceCommercialService,
)
from services.marketplace_listings import (
    MarketplaceCatalogProtocolError,
    MarketplaceListingService,
)
from services.ozon_commercial_contracts import (
    OzonCommercialProtocolError,
    OzonPriceContract,
)
from tests.test_ozon_account_sync import Catalog, advance, connected, enqueue, setup
from tests.test_marketplace_listing_service import SYNTHETIC_CREDENTIALS


def _list_response(total_fields):
    return {
        "result": {
            "items": [{
                "product_id": 101,
                "offer_id": "offer-101",
                "archived": False,
                "has_fbo_stocks": True,
                "has_fbs_stocks": False,
            }],
            "last_id": "next",
            **total_fields,
        },
    }


def _price_summary_response(total_fields):
    return {
        "items": [{
            "product_id": 101,
            "offer_id": "offer-101",
            "price": {"price": "100.00", "currency_code": "RUB"},
        }],
        "cursor": "next",
        **total_fields,
    }


def _stock_summary_response(total_fields):
    return {
        "items": [{
            "product_id": 101,
            "offer_id": "offer-101",
            "stocks": [],
        }],
        "cursor": "next",
        **total_fields,
    }


def test_affected_catalog_page_shapes_normalize_to_same_internal_total():
    old = MarketplaceListingService.normalize_product_list_page(
        _list_response({"total": 1})
    )
    current = MarketplaceListingService.normalize_product_list_page(
        _list_response({"total_items": 1})
    )
    both = MarketplaceListingService.normalize_product_list_page(
        _list_response({"total": 1, "total_items": 1})
    )
    assert old == current == both
    assert old["total"] == 1

    for normalize, build in (
        (MarketplaceListingService.normalize_prices_page, _price_summary_response),
        (MarketplaceListingService.normalize_stocks_page, _stock_summary_response),
    ):
        old = normalize(build({"total": 1}))
        current = normalize(build({"total_items": 1}))
        both = normalize(build({"total": 1, "total_items": 1}))
        assert old == current == both
        assert old["total"] == 1


@pytest.mark.parametrize("invalid", [None, "1", -1, True, 1.0])
def test_catalog_totals_reject_non_integer_or_negative_values(invalid):
    for normalize, build in (
        (
            MarketplaceListingService.normalize_product_list_page,
            lambda value: _list_response({"total_items": value}),
        ),
        (
            MarketplaceListingService.normalize_prices_page,
            lambda value: _price_summary_response({"total_items": value}),
        ),
        (
            MarketplaceListingService.normalize_stocks_page,
            lambda value: _stock_summary_response({"total_items": value}),
        ),
    ):
        with pytest.raises(MarketplaceCatalogProtocolError):
            normalize(build(invalid))

    with pytest.raises(OzonCommercialProtocolError):
        OzonPriceContract.normalize_read_page({
            "items": [],
            "total_items": invalid,
            "cursor": "",
        })


def test_catalog_totals_fail_closed_when_absent_malformed_or_conflicting():
    for normalize, empty_page in (
        (
            MarketplaceListingService.normalize_product_list_page,
            lambda totals: {"result": {"items": [], "last_id": "", **totals}},
        ),
        (
            MarketplaceListingService.normalize_prices_page,
            lambda totals: {"items": [], "cursor": "", **totals},
        ),
        (
            MarketplaceListingService.normalize_stocks_page,
            lambda totals: {"items": [], "cursor": "", **totals},
        ),
    ):
        with pytest.raises(MarketplaceCatalogProtocolError):
            normalize(empty_page({}))
        with pytest.raises(MarketplaceCatalogProtocolError):
            normalize(empty_page({"total": "0", "total_items": 0}))
        with pytest.raises(MarketplaceCatalogProtocolError):
            normalize(empty_page({"total": 0, "total_items": 1}))

    for totals in (
        {},
        {"total": "1", "total_items": 1},
        {"total": 1, "total_items": 2},
    ):
        with pytest.raises(OzonCommercialProtocolError):
            OzonPriceContract.normalize_read_page({
                "items": [], "cursor": "", **totals,
            })


def test_unaffected_product_attributes_total_contract_stays_legacy_only():
    legacy = MarketplaceListingService.normalize_product_attributes_page({
        "result": [], "total": 0, "last_id": "",
    })
    assert legacy["total"] == 0
    # Extra fields on this different method do not change its existing contract.
    unchanged = MarketplaceListingService.normalize_product_attributes_page({
        "result": [], "total": 0, "total_items": 7, "last_id": "",
    })
    assert unchanged["total"] == 0
    with pytest.raises(MarketplaceCatalogProtocolError):
        MarketplaceListingService.normalize_product_attributes_page({
            "result": [], "total_items": 0, "last_id": "",
        })


def test_catalog_checkpoint_accepts_new_totals_and_empty_archive_sentinel(setup):
    class CurrentTotalsCatalog(Catalog):
        @staticmethod
        def _rename_total(response, nested=False):
            target = response["result"] if nested else response
            target["total_items"] = target.pop("total")
            return response

        def list_products(self, credentials, payload):
            return self._rename_total(
                super().list_products(credentials, payload), nested=True
            )

        def read_prices(self, credentials, payload):
            return self._rename_total(super().read_prices(credentials, payload))

        def read_stocks(self, credentials, payload):
            return self._rename_total(super().read_stocks(credentials, payload))

    connected(setup)
    job_id = enqueue(setup).id
    adapter = CurrentTotalsCatalog()
    for _ in range(8):
        db.session.remove()
        assert advance(setup, job_id, adapter)
        job = db.session.get(BackgroundJob, job_id)
        if job.status == "completed":
            break

    assert job.status == "completed"
    assert MarketplaceListing.query.count() == 1
    run = MarketplaceCatalogSync.query.one()
    assert run.status == "completed"
    assert run.page_count == 2
    assert [payload["filter"]["visibility"] for payload in adapter.list_payloads] == [
        "ALL", "ARCHIVED",
    ]
    assert adapter.list_payloads[-1]["last_id"] == ""


def test_commercial_live_read_accepts_new_total_and_terminal_empty_page():
    listing = SimpleNamespace(id=9, offer_id="offer-101", external_product_id="101")
    responses = iter([
        {
            "items": [{
                "offer_id": "offer-101",
                "product_id": 101,
                "price": {"price": "100.00", "currency_code": "RUB"},
            }],
            "total_items": 1,
            "cursor": "next",
        },
        {"items": [], "total_items": 0, "cursor": ""},
    ])

    class Adapter:
        def read_prices(self, credentials, payload):
            assert credentials == SYNTHETIC_CREDENTIALS
            return next(responses)

    state = MarketplaceCommercialService._read_price_state(
        listing=listing,
        adapter=Adapter(),
        credentials=SYNTHETIC_CREDENTIALS,
    )
    assert state["product_id"] == "101"
    assert state["price"] == "100"


def test_mixed_total_names_do_not_hide_live_pagination_drift():
    listing = SimpleNamespace(id=9, offer_id="offer-101", external_product_id="101")
    responses = iter([
        {
            "items": [{
                "offer_id": "offer-101",
                "product_id": 101,
                "price": {"price": "100.00", "currency_code": "RUB"},
            }],
            "total_items": 2,
            "cursor": "next",
        },
        {"items": [], "total": 1, "cursor": ""},
    ])

    class Adapter:
        def read_prices(self, credentials, payload):
            return next(responses)

    with pytest.raises(MarketplaceCommercialConflict):
        MarketplaceCommercialService._read_price_state(
            listing=listing,
            adapter=Adapter(),
            credentials=SYNTHETIC_CREDENTIALS,
        )


def test_catalog_enrichment_pagination_still_detects_total_drift():
    pages = iter([
        _price_summary_response({"total_items": 2}),
        _price_summary_response({"total": 1}),
    ])
    with pytest.raises(MarketplaceCatalogProtocolError, match="total changed"):
        MarketplaceListingService._collect_cursor_pages(
            fetch=lambda payload: next(pages),
            payload={},
            normalize=MarketplaceListingService.normalize_prices_page,
            cursor_field="cursor",
            endpoint="product_prices",
        )
