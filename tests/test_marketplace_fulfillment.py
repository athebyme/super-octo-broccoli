from datetime import date, datetime
import json
import unittest

from flask import Flask

from models import (
    Marketplace,
    MarketplaceCancellation,
    MarketplaceFulfillmentSync,
    MarketplaceListing,
    MarketplacePosting,
    MarketplacePostingItem,
    MarketplacePostingStatusEvent,
    MarketplaceReturn,
    Seller,
    SellerMarketplaceAccount,
    User,
    db,
)
from services.marketplace_adapters import MarketplaceCredentials
from services.marketplace_fulfillment import (
    MarketplaceFulfillmentConfigurationError,
    MarketplaceFulfillmentNotFound,
    MarketplaceFulfillmentProtocolError,
    MarketplaceFulfillmentService,
)


SYNTHETIC_CREDENTIALS = MarketplaceCredentials(
    external_account_id="synthetic-client",
    api_key="synthetic-key",
)


class SyntheticFulfillmentAdapter:
    capabilities = {"orders_read"}

    def __init__(self, *, malformed_phase=None, fbs_status="delivered"):
        self.malformed_phase = malformed_phase
        self.fbs_status = fbs_status
        self.calls = []

    def require_capability(self, capability):
        if capability not in self.capabilities:
            raise AssertionError("missing capability")

    def _record(self, phase, credentials, payload):
        assert credentials == SYNTHETIC_CREDENTIALS
        self.calls.append((phase, payload))
        if self.malformed_phase == phase:
            return True
        return False

    def read_fbs_postings(self, credentials, payload):
        if self._record("fbs_postings", credentials, payload):
            return {"result": {"postings": [], "has_next": "wrong"}}
        return {
            "result": {
                "postings": [{
                    "posting_number": "100-1-1",
                    "order_id": 100,
                    "order_number": "100-1",
                    "status": self.fbs_status,
                    "substatus": "posting_received",
                    "in_process_at": "2026-07-10T10:00:00Z",
                    "products": [{
                        "sku": 1101,
                        "offer_id": "offer-1",
                        "name": "Synthetic matched product",
                        "quantity": 2,
                        "price": "100.50",
                        "currency_code": "RUB",
                    }],
                    "customer": {
                        "name": "must never persist",
                        "phone": "+70000000000",
                    },
                }],
                "has_next": False,
            }
        }

    def read_fbo_postings(self, credentials, payload):
        if self._record("fbo_postings", credentials, payload):
            return {"result": {}}
        return {
            "result": [{
                "posting_number": "200-2-1",
                "order_id": 200,
                "status": "cancelled",
                "cancelled_at": "2026-07-11T10:00:00Z",
                "cancellation": {
                    "cancel_reason_id": 12,
                    "cancel_reason": "Provider reason",
                },
                "products": [{
                    "sku": 9999,
                    "offer_id": "unmatched-offer",
                    "name": "Synthetic unmatched product",
                    "quantity": 1,
                    "price": "200",
                    "currency_code": "RUB",
                }],
            }]
        }

    def read_returns(self, credentials, payload):
        if self._record("returns", credentials, payload):
            return {"returns": [], "has_next": 1}
        return {
            "returns": [{
                "id": 501,
                "order_id": 100,
                "posting_number": "100-1-1",
                "schema": "FBS",
                "return_reason_name": "Не подошёл товар",
                "visual": {
                    "status": {
                        "sys_name": "MovingToSeller",
                        "display_name": "Едет продавцу",
                    },
                    "change_moment": "2026-07-13T10:00:00Z",
                },
                "logistic": {"return_date": "2026-07-12T10:00:00Z"},
                "product": {
                    "sku": 1101,
                    "offer_id": "offer-1",
                    "name": "Synthetic matched product",
                    "quantity": 1,
                    "price": {"price": 100.5, "currency_code": "RUB"},
                },
                "place": {"address": "must never persist"},
            }],
            "has_next": False,
        }

    def read_rfbs_returns(self, credentials, payload):
        if self._record("rfbs_returns", credentials, payload):
            return {"returns": []}
        return {
            "returns": [{
                "return_id": 601,
                "posting_number": "300-3-1",
                "order_number": "300-3",
                "created_at": "2026-07-13T11:00:00Z",
                "client_name": "must never persist",
                "comment": "must never persist",
                "state": {
                    "group_state": "IN_PROGRESS",
                    "state_name": "На проверке",
                },
                "product": {
                    "sku": 1101,
                    "offer_id": "offer-1",
                    "name": "Synthetic matched product",
                    "price": "100.5",
                    "currency_code": "RUB",
                },
            }],
            "last_id": 601,
        }

    def read_conditional_cancellations(self, credentials, payload):
        if self._record("conditional_cancellations", credentials, payload):
            return {"result": [], "last_id": False}
        return {
            "result": [{
                "cancellation_id": 701,
                "posting_number": "300-3-1",
                "cancellation_initiator": "CLIENT",
                "cancellation_reason": {"id": 5, "name": "Передумал"},
                "cancellation_reason_message": "must never persist",
                "cancelled_at": "2026-07-14T10:00:00Z",
                "state": {"state": "ON_APPROVAL", "name": "На согласовании"},
            }],
            "last_id": 701,
        }


class MarketplaceFulfillmentServiceTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        user = User(username="seller", email="seller@example.test")
        user.set_password("test-password")
        self.seller = Seller(user=user, company_name="Seller")
        other_user = User(username="other", email="other@example.test")
        other_user.set_password("test-password")
        self.other_seller = Seller(user=other_user, company_name="Other")
        self.marketplace = Marketplace(
            code="ozon",
            name="Ozon",
            is_active=True,
            adapter_code="ozon",
        )
        db.session.add_all([
            user, self.seller, other_user, self.other_seller, self.marketplace,
        ])
        db.session.flush()
        self.account = SellerMarketplaceAccount(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            external_account_id="synthetic-client",
            label="Основной Ozon",
            is_active=True,
            is_default=True,
            connection_status="connected",
        )
        self.other_account = SellerMarketplaceAccount(
            seller_id=self.other_seller.id,
            marketplace_id=self.marketplace.id,
            external_account_id="other-client",
            label="Other Ozon",
            is_active=True,
            is_default=True,
            connection_status="connected",
        )
        db.session.add_all([self.account, self.other_account])
        db.session.flush()
        self.listing = MarketplaceListing(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            offer_id="offer-1",
            external_product_id="101",
            primary_sku="1101",
            identifiers_json=json.dumps({"sku": "1101"}),
            title="Synthetic product",
            normalized_status="active",
            is_available=True,
            is_archived=False,
            sync_fingerprint="a" * 64,
        )
        db.session.add(self.listing)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _sync(self, adapter=None, **kwargs):
        now = kwargs.pop("now", datetime(2026, 7, 15, 12, 0, 0))
        today = kwargs.pop("today", date(2026, 7, 15))
        return MarketplaceFulfillmentService.sync_account(
            seller_id=self.seller.id,
            account_id=self.account.id,
            period_code="30d",
            force=True,
            max_pages=5,
            adapter=adapter or SyntheticFulfillmentAdapter(),
            credentials=SYNTHETIC_CREDENTIALS,
            now=now,
            today=today,
            **kwargs,
        )

    def test_full_sync_persists_safe_account_scoped_projections(self):
        adapter = SyntheticFulfillmentAdapter()
        run = self._sync(adapter)

        self.assertEqual(run.status, "completed")
        self.assertEqual(run.phase, "completed")
        self.assertEqual(run.page_count, 5)
        self.assertEqual(run.posting_count, 2)
        self.assertEqual(run.return_count, 2)
        self.assertEqual(run.cancellation_count, 1)
        self.assertEqual(run.matched_item_count, 3)
        self.assertEqual(run.unmatched_item_count, 1)
        self.assertEqual(
            [phase for phase, _ in adapter.calls],
            list(MarketplaceFulfillmentService.PHASES),
        )

        self.assertEqual(MarketplacePosting.query.count(), 2)
        self.assertEqual(MarketplacePostingItem.query.count(), 2)
        self.assertEqual(MarketplacePostingStatusEvent.query.count(), 2)
        self.assertEqual(MarketplaceReturn.query.count(), 2)
        self.assertEqual(MarketplaceCancellation.query.count(), 2)

        matched_item = MarketplacePostingItem.query.filter_by(
            offer_id="offer-1"
        ).one()
        self.assertEqual(matched_item.listing_id, self.listing.id)
        unmatched_item = MarketplacePostingItem.query.filter_by(
            offer_id="unmatched-offer"
        ).one()
        self.assertIsNone(unmatched_item.listing_id)
        derived = MarketplaceCancellation.query.filter_by(
            source_kind="posting_fbo"
        ).one()
        self.assertEqual(derived.reason, "Provider reason")
        conditional = MarketplaceCancellation.query.filter_by(
            source_kind="rfbs_conditional"
        ).one()
        self.assertEqual(conditional.initiator, "CLIENT")

        model_columns = {
            column.name
            for model in (MarketplacePosting, MarketplaceReturn, MarketplaceCancellation)
            for column in model.__table__.columns
        }
        for forbidden in (
            "client_name", "customer_name", "phone", "address", "comment",
            "financial_data", "analytics_data", "barcode",
        ):
            self.assertNotIn(forbidden, model_columns)

    def test_bounded_run_resumes_from_durable_phase(self):
        adapter = SyntheticFulfillmentAdapter()
        first = MarketplaceFulfillmentService.sync_account(
            seller_id=self.seller.id,
            account_id=self.account.id,
            period_code="30d",
            force=True,
            max_pages=2,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
            now=datetime(2026, 7, 15, 12, 0, 0),
            today=date(2026, 7, 15),
        )
        self.assertEqual(first.status, "running")
        self.assertEqual(first.phase, "returns")
        second = MarketplaceFulfillmentService.sync_account(
            seller_id=self.seller.id,
            account_id=self.account.id,
            period_code="30d",
            force=False,
            max_pages=3,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
            now=datetime(2026, 7, 15, 12, 1, 0),
            today=date(2026, 7, 15),
        )
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.status, "completed")
        self.assertEqual(len(adapter.calls), 5)

    def test_running_force_refresh_wins_over_fresh_completed_cache(self):
        self._sync(SyntheticFulfillmentAdapter())
        adapter = SyntheticFulfillmentAdapter()
        running = MarketplaceFulfillmentService.sync_account(
            seller_id=self.seller.id,
            account_id=self.account.id,
            period_code="30d",
            force=True,
            max_pages=1,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
            now=datetime(2026, 7, 15, 12, 5, 0),
            today=date(2026, 7, 15),
        )
        self.assertEqual(running.status, "running")
        resumed = MarketplaceFulfillmentService.sync_account(
            seller_id=self.seller.id,
            account_id=self.account.id,
            period_code="30d",
            force=False,
            max_pages=4,
            adapter=adapter,
            credentials=SYNTHETIC_CREDENTIALS,
            now=datetime(2026, 7, 15, 12, 6, 0),
            today=date(2026, 7, 15),
        )
        self.assertEqual(resumed.id, running.id)
        self.assertEqual(resumed.status, "completed")

    def test_status_history_appends_only_on_transition(self):
        self._sync(SyntheticFulfillmentAdapter(fbs_status="awaiting_deliver"))
        self._sync(
            SyntheticFulfillmentAdapter(fbs_status="delivered"),
            now=datetime(2026, 7, 15, 12, 10, 0),
        )
        posting = MarketplacePosting.query.filter_by(
            posting_number="100-1-1"
        ).one()
        events = MarketplacePostingStatusEvent.query.filter_by(
            posting_id=posting.id
        ).order_by(MarketplacePostingStatusEvent.id.asc()).all()
        self.assertEqual([event.status for event in events], ["awaiting_deliver", "delivered"])

    def test_malformed_provider_page_fails_run_and_keeps_prior_rows(self):
        completed = self._sync(SyntheticFulfillmentAdapter())
        with self.assertRaises(MarketplaceFulfillmentProtocolError):
            self._sync(
                SyntheticFulfillmentAdapter(malformed_phase="fbo_postings"),
                now=datetime(2026, 7, 15, 13, 0, 0),
            )
        self.assertEqual(MarketplacePosting.query.count(), 2)
        self.assertEqual(completed.status, "completed")
        failed = MarketplaceFulfillmentSync.query.filter_by(status="failed").one()
        self.assertEqual(failed.error_code, "ozon_fulfillment_protocol_error")

    def test_ambiguous_sku_fails_before_provider_read(self):
        duplicate = MarketplaceListing(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            offer_id="offer-2",
            external_product_id="102",
            primary_sku="1101",
            identifiers_json="{}",
            title="Duplicate SKU",
            normalized_status="active",
            is_available=True,
            is_archived=False,
            sync_fingerprint="b" * 64,
        )
        db.session.add(duplicate)
        db.session.commit()
        adapter = SyntheticFulfillmentAdapter()
        with self.assertRaises(MarketplaceFulfillmentConfigurationError):
            self._sync(adapter)
        self.assertEqual(adapter.calls, [])

    def test_read_models_enforce_tenant_and_account_scope(self):
        self._sync(SyntheticFulfillmentAdapter())
        result = MarketplaceFulfillmentService.list_postings(
            seller_id=self.seller.id,
            account_id=self.account.id,
            search="offer-1",
            today=date(2026, 7, 15),
        )
        self.assertEqual(result["pagination"]["total"], 1)
        with self.assertRaises(MarketplaceFulfillmentNotFound):
            MarketplaceFulfillmentService.list_postings(
                seller_id=self.other_seller.id,
                account_id=self.account.id,
            )
        posting = MarketplacePosting.query.first()
        with self.assertRaises(MarketplaceFulfillmentNotFound):
            MarketplaceFulfillmentService.get_posting(
                seller_id=self.other_seller.id,
                account_id=self.account.id,
                posting_id=posting.id,
            )

    def test_read_period_filters_projection_without_deleting_history(self):
        self._sync(SyntheticFulfillmentAdapter())
        old = MarketplacePosting(
            seller_id=self.seller.id,
            marketplace_id=self.marketplace.id,
            account_id=self.account.id,
            posting_number="old-but-in-30d",
            fulfillment_kind="fbs",
            status="delivered",
            upstream_created_at=datetime(2026, 6, 20, 12, 0, 0),
            source_endpoint="/v4/posting/fbs/list",
            sync_fingerprint="f" * 64,
            last_seen_at=datetime(2026, 7, 15, 12, 0, 0),
        )
        db.session.add(old)
        db.session.commit()
        seven = MarketplaceFulfillmentService.list_postings(
            seller_id=self.seller.id,
            account_id=self.account.id,
            period_code="7d",
            today=date(2026, 7, 15),
        )
        thirty = MarketplaceFulfillmentService.list_postings(
            seller_id=self.seller.id,
            account_id=self.account.id,
            period_code="30d",
            today=date(2026, 7, 15),
        )
        self.assertEqual(seven["pagination"]["total"], 2)
        self.assertEqual(thirty["pagination"]["total"], 3)
        self.assertEqual(MarketplacePosting.query.count(), 3)


if __name__ == "__main__":
    unittest.main()
