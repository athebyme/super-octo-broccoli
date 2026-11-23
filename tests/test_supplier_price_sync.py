"""Durable supplier price-feed claim, apply, and cascade behavior."""

import io
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from unittest.mock import patch

from flask import Flask

from models import (
    ImportedProduct,
    Seller,
    SellerSupplier,
    Supplier,
    SupplierProduct,
    User,
    db,
)
from migrations.migrate_add_price_stock_sync import migrate
from services.supplier_price_feed import (
    SupplierPriceFeed,
    SupplierPriceFeedError,
    SupplierPriceRow,
)
from services.supplier_price_sync import (
    run_auto_supplier_price_sync_tick,
    sync_supplier_price_feed,
)
from services.supplier_service import SupplierService, _supplier_catalog_sync_lock


class SupplierPriceSyncTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="supplier-price-sync-test",
            SQLALCHEMY_DATABASE_URI="sqlite://",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def _supplier(self, code="price-test", **values):
        fields = {
            "price_file_url": "https://supplier.invalid/prices.csv",
            "price_file_encoding": "utf-8",
            "price_file_delimiter": ";",
            "is_active": True,
            "auto_sync_prices": False,
            "auto_sync_interval_minutes": 60,
        }
        fields.update(values)
        supplier = Supplier(name="Price fixture", code=code, **fields)
        db.session.add(supplier)
        db.session.flush()
        return supplier

    def _product(self, supplier, external_id="123", **values):
        fields = {
            "supplier_price": 20.0,
            "supplier_quantity": 4,
            "currency": "USD",
        }
        fields.update(values)
        product = SupplierProduct(
            supplier_id=supplier.id,
            external_id=external_id,
            **fields,
        )
        db.session.add(product)
        db.session.flush()
        return product

    def _seller(self, supplier, *, active=True, username="seller"):
        user = User(username=username, email=f"{username}@test.local")
        user.set_password("fixture-password")
        seller = Seller(user=user, company_name=f"{username} company")
        db.session.add(seller)
        db.session.flush()
        connection = SellerSupplier(
            seller_id=seller.id,
            supplier_id=supplier.id,
            is_active=active,
        )
        db.session.add(connection)
        db.session.flush()
        return seller

    def _imported(self, seller, supplier, product, *, price=20.0, quantity=4, calculated=100.0):
        imported = ImportedProduct(
            seller_id=seller.id,
            supplier_id=supplier.id,
            supplier_product_id=product.id,
            external_id=f"copy-{seller.id}-{product.id}",
            supplier_price=price,
            supplier_quantity=quantity,
            calculated_price=calculated,
            content_overrides_json='{"schema_version":1,"fields":{}}',
        )
        db.session.add(imported)
        db.session.flush()
        return imported

    @staticmethod
    def _row(**overrides):
        values = {
            "vendor_code": None,
            "price": 25.0,
            "quantity": 3,
            "status": None,
            "additional_vendor_code": None,
            "barcode": None,
            "rrp": None,
        }
        values.update(overrides)
        return SupplierPriceRow(**values)

    @staticmethod
    def _feed(rows=None, conflicts=()):
        return SupplierPriceFeed(
            rows=rows or {},
            conflicting_ids=frozenset(conflicts),
        )

    def test_feed_failure_is_rate_limited_and_retains_good_facts(self):
        supplier = self._supplier(auto_sync_prices=True, auto_sync_interval_minutes=5)
        product = self._product(supplier, supplier_price=44.0, supplier_quantity=6)
        good_supplier_sync = datetime.utcnow() - timedelta(days=1)
        good_row_sync = datetime.utcnow() - timedelta(hours=2)
        supplier.last_price_sync_at = good_supplier_sync
        supplier.last_price_sync_status = "partial"
        supplier.last_price_sync_error = "ambiguous_source_rows"
        supplier.last_price_sync_conflict_count = 2
        product.last_price_sync_at = good_row_sync
        db.session.commit()

        with patch(
            "services.supplier_price_sync.read_supplier_price_feed",
            side_effect=SupplierPriceFeedError("http_status"),
        ) as reader:
            result = sync_supplier_price_feed(supplier.id, automatic=True)
            self.assertFalse(result.success)
            self.assertEqual(result.error_messages, ["http_status"])
            self.assertTrue(supplier.last_price_sync_attempt_at)
            self.assertEqual(supplier.last_price_sync_status, "failed")
            self.assertEqual(supplier.last_price_sync_error, "http_status")
            self.assertIsNone(supplier.last_price_sync_conflict_count)
            self.assertEqual(supplier.last_price_sync_at, good_supplier_sync)
            self.assertEqual(product.supplier_price, 44.0)
            self.assertEqual(product.supplier_quantity, 6)
            self.assertEqual(product.last_price_sync_at, good_row_sync)

            # The attempt timestamp gates failures as well as successes.
            self.assertIsNone(run_auto_supplier_price_sync_tick(self.app))
            reader.assert_called_once()

    def test_price_feed_does_not_read_while_catalog_lock_is_owned(self):
        supplier = self._supplier()
        supplier_id = supplier.id
        db.session.commit()
        lock_ready = threading.Event()
        release_lock = threading.Event()
        lock_results = []

        def hold_catalog_lock():
            with _supplier_catalog_sync_lock(supplier_id) as acquired:
                lock_results.append(acquired)
                lock_ready.set()
                release_lock.wait(timeout=5)

        holder = threading.Thread(target=hold_catalog_lock)
        holder.start()
        try:
            self.assertTrue(lock_ready.wait(timeout=2))
            self.assertEqual(lock_results, [True])
            with patch(
                'services.supplier_price_sync.read_supplier_price_feed',
            ) as reader:
                result = sync_supplier_price_feed(supplier_id)
        finally:
            release_lock.set()
            holder.join(timeout=2)

        self.assertFalse(result.success)
        self.assertTrue(result.busy)
        self.assertEqual(result.error_messages, ['sync_busy'])
        reader.assert_not_called()

    def test_manual_catalog_price_followup_reuses_held_catalog_lock(self):
        supplier = self._supplier()
        product = self._product(supplier, external_id='123')
        db.session.commit()

        with _supplier_catalog_sync_lock(supplier.id) as acquired:
            self.assertTrue(acquired)
            with patch(
                'services.supplier_price_sync.read_supplier_price_feed',
                return_value=self._feed({123: self._row()}),
            ) as reader:
                result = SupplierService.sync_prices_and_stock(
                    supplier.id,
                    force=True,
                )

        self.assertTrue(result.success)
        self.assertEqual(product.supplier_price, 25.0)
        reader.assert_called_once_with(
            'https://supplier.invalid/prices.csv',
            encoding='utf-8',
            delimiter=';',
        )

    def test_positive_unchanged_price_refreshes_evidence_and_only_active_exact_sellers(self):
        supplier = self._supplier()
        wrong_supplier = self._supplier(code="other-price-test")
        old_sync = datetime.utcnow() - timedelta(days=4)
        changed_at = datetime.utcnow() - timedelta(days=8)
        product = self._product(
            supplier,
            external_id="123",
            supplier_price=20.0,
            supplier_quantity=4,
            last_price_sync_at=old_sync,
            previous_price=18.0,
            price_changed_at=changed_at,
            currency="USD",
        )

        active_seller = self._seller(supplier, active=True, username="active-price")
        inactive_seller = self._seller(supplier, active=False, username="inactive-price")
        wrong_link_seller = self._seller(wrong_supplier, active=True, username="wrong-price")
        active_copy = self._imported(active_seller, supplier, product)
        inactive_copy = self._imported(inactive_seller, supplier, product)
        wrong_supplier_copy = self._imported(wrong_link_seller, wrong_supplier, product)
        original_overrides = active_copy.content_overrides_json
        db.session.commit()

        with patch(
            "services.supplier_price_sync.read_supplier_price_feed",
            return_value=self._feed({123: self._row(price=20.0, quantity=0)}),
        ):
            result = sync_supplier_price_feed(supplier.id)

        self.assertTrue(result.success)
        self.assertEqual(result.updated, 1)
        self.assertEqual(result.seller_rows_updated, 1)
        self.assertGreater(product.last_price_sync_at, old_sync)
        self.assertEqual(product.previous_price, 18.0)
        self.assertEqual(product.price_changed_at, changed_at)
        self.assertEqual(product.supplier_price, 20.0)
        self.assertEqual(product.supplier_quantity, 0)
        self.assertEqual(product.supplier_status, "out_of_stock")
        self.assertEqual(product.currency, "USD")
        self.assertEqual(active_copy.supplier_price, 20.0)
        self.assertEqual(active_copy.supplier_quantity, 0)
        self.assertEqual(active_copy.calculated_price, 100.0)
        self.assertEqual(active_copy.content_overrides_json, original_overrides)
        self.assertEqual(inactive_copy.supplier_price, 20.0)
        self.assertEqual(inactive_copy.supplier_quantity, 4)
        self.assertEqual(wrong_supplier_copy.supplier_quantity, 4)

    def test_zero_and_blank_price_preserve_amount_and_observation_rules(self):
        supplier = self._supplier()
        observed_at = datetime.utcnow() - timedelta(days=2)
        product = self._product(
            supplier,
            supplier_price=64.0,
            supplier_quantity=9,
            last_price_sync_at=observed_at,
        )
        db.session.commit()

        with patch(
            "services.supplier_price_sync.read_supplier_price_feed",
            return_value=self._feed({123: self._row(price=0, quantity=None)}),
        ):
            result = sync_supplier_price_feed(supplier.id)
        self.assertTrue(result.success)
        self.assertEqual(product.supplier_price, 64.0)
        self.assertEqual(product.supplier_quantity, 9)
        self.assertIsNone(product.last_price_sync_at)

        product.last_price_sync_at = observed_at
        db.session.commit()
        with patch(
            "services.supplier_price_sync.read_supplier_price_feed",
            return_value=self._feed({123: self._row(price=None, quantity=0)}),
        ):
            result = sync_supplier_price_feed(supplier.id)
        self.assertTrue(result.success)
        self.assertEqual(product.supplier_price, 64.0)
        self.assertEqual(product.supplier_quantity, 0)
        self.assertEqual(product.last_price_sync_at, observed_at)

    def test_stock_only_row_does_not_overwrite_seller_purchase_price(self):
        supplier = self._supplier()
        product = self._product(
            supplier,
            supplier_price=50.0,
            supplier_quantity=7,
            last_price_sync_at=datetime.utcnow() - timedelta(days=1),
        )
        seller = self._seller(supplier, active=True, username="stock-only")
        imported = self._imported(
            seller,
            supplier,
            product,
            price=17.0,
            quantity=4,
            calculated=140.0,
        )
        previous_imported_updated_at = imported.updated_at
        db.session.commit()

        with patch(
            "services.supplier_price_sync.read_supplier_price_feed",
            return_value=self._feed({123: self._row(price=None, quantity=0)}),
        ):
            result = sync_supplier_price_feed(supplier.id)

        self.assertTrue(result.success)
        self.assertEqual(product.supplier_price, 50.0)
        self.assertEqual(product.supplier_quantity, 0)
        self.assertEqual(imported.supplier_price, 17.0)
        self.assertEqual(imported.supplier_quantity, 0)
        self.assertEqual(imported.calculated_price, 140.0)
        self.assertEqual(result.seller_rows_updated, 1)
        self.assertGreater(imported.updated_at, previous_imported_updated_at)

    def test_target_cap_checks_next_chunk_and_fails_atomically(self):
        supplier = self._supplier()
        product = self._product(supplier, supplier_price=20.0, supplier_quantity=4)
        seller = self._seller(supplier, active=True, username="target-cap")
        imported = self._imported(seller, supplier, product, price=17.0, quantity=2)
        db.session.commit()

        with (
            patch(
                "services.supplier_price_sync.read_supplier_price_feed",
                return_value=self._feed({123: self._row(price=35.0, quantity=5)}),
            ),
            patch("services.supplier_price_sync.MAX_IMPORTED_TARGETS", 0),
        ):
            result = sync_supplier_price_feed(supplier.id)

        self.assertFalse(result.success)
        self.assertEqual(result.error_messages, ["seller_targets_too_large"])
        self.assertEqual(product.supplier_price, 20.0)
        self.assertEqual(product.supplier_quantity, 4)
        self.assertEqual(imported.supplier_price, 17.0)
        self.assertEqual(imported.supplier_quantity, 2)
        self.assertIsNone(supplier.last_price_sync_at)

    def test_vendor_metadata_is_manual_only_but_rrp_is_allowed_automatically(self):
        supplier = self._supplier(auto_sync_prices=True)
        product = self._product(
            supplier,
            vendor_code="old-vendor",
            additional_vendor_code="old-additional",
            barcode="old-barcode",
            recommended_retail_price=30.0,
        )
        db.session.commit()
        row = self._row(
            vendor_code="new-vendor",
            additional_vendor_code="new-additional",
            barcode="new-barcode",
            rrp=42.0,
        )

        with patch(
            "services.supplier_price_sync.read_supplier_price_feed",
            return_value=self._feed({123: row}),
        ):
            automatic_result = sync_supplier_price_feed(supplier.id, automatic=True)

        self.assertTrue(automatic_result.success)
        self.assertEqual(product.vendor_code, "old-vendor")
        self.assertEqual(product.additional_vendor_code, "old-additional")
        self.assertEqual(product.barcode, "old-barcode")
        self.assertEqual(product.recommended_retail_price, 42.0)

        with patch(
            "services.supplier_price_sync.read_supplier_price_feed",
            return_value=self._feed({123: row}),
        ):
            manual_result = sync_supplier_price_feed(supplier.id)

        self.assertTrue(manual_result.success)
        self.assertEqual(product.vendor_code, "new-vendor")
        self.assertEqual(product.additional_vendor_code, "new-additional")
        self.assertEqual(product.barcode, "new-barcode")
        self.assertEqual(product.recommended_retail_price, 42.0)

    def test_conflicting_source_id_is_quarantined_and_partial_state_recovers(self):
        supplier = self._supplier()
        safe_product = self._product(
            supplier,
            external_id="101",
            supplier_price=10.0,
            supplier_quantity=2,
        )
        conflict_observed_at = datetime.utcnow() - timedelta(days=3)
        conflict_product = self._product(
            supplier,
            external_id="202",
            supplier_price=40.0,
            supplier_quantity=4,
            last_price_sync_at=conflict_observed_at,
        )
        seller = self._seller(supplier, active=True, username="ambiguous-feed")
        safe_copy = self._imported(seller, supplier, safe_product, price=10.0, quantity=2)
        conflict_copy = self._imported(
            seller, supplier, conflict_product, price=40.0, quantity=4,
        )
        db.session.commit()

        # Even if a producer accidentally supplies a candidate row for a
        # quarantined ID, conflicting_ids wins and no first/last value applies.
        partial_feed = self._feed(
            {
                101: self._row(price=15.0, quantity=3),
                202: self._row(price=999.0, quantity=99),
            },
            conflicts={202},
        )
        with patch(
            "services.supplier_price_sync.read_supplier_price_feed",
            return_value=partial_feed,
        ):
            result = sync_supplier_price_feed(supplier.id)

        self.assertTrue(result.success)
        self.assertEqual(result.errors, 1)
        self.assertEqual(result.conflicting_source_rows, 1)
        self.assertEqual(result.total_in_csv, 2)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.error_messages, ["ambiguous_source_rows"])
        self.assertEqual(supplier.last_price_sync_status, "partial")
        self.assertEqual(supplier.last_price_sync_error, "ambiguous_source_rows")
        self.assertEqual(supplier.last_price_sync_conflict_count, 1)
        self.assertGreater(supplier.last_price_sync_at, conflict_observed_at)
        self.assertEqual(safe_product.supplier_price, 15.0)
        self.assertEqual(safe_product.supplier_quantity, 3)
        self.assertEqual(safe_copy.supplier_price, 15.0)
        self.assertEqual(safe_copy.supplier_quantity, 3)
        self.assertEqual(conflict_product.supplier_price, 40.0)
        self.assertEqual(conflict_product.supplier_quantity, 4)
        self.assertEqual(conflict_product.last_price_sync_at, conflict_observed_at)
        self.assertEqual(conflict_copy.supplier_price, 40.0)
        self.assertEqual(conflict_copy.supplier_quantity, 4)

        with patch(
            "services.supplier_price_sync.read_supplier_price_feed",
            return_value=self._feed({
                101: self._row(price=16.0, quantity=4),
                202: self._row(price=42.0, quantity=5),
            }),
        ):
            recovered = sync_supplier_price_feed(supplier.id)

        self.assertTrue(recovered.success)
        self.assertEqual(recovered.errors, 0)
        self.assertEqual(recovered.conflicting_source_rows, 0)
        self.assertEqual(recovered.error_messages, [])
        self.assertEqual(supplier.last_price_sync_status, "success")
        self.assertIsNone(supplier.last_price_sync_error)
        self.assertEqual(supplier.last_price_sync_conflict_count, 0)
        self.assertEqual(conflict_product.supplier_price, 42.0)
        self.assertEqual(conflict_product.supplier_quantity, 5)
        self.assertEqual(conflict_copy.supplier_price, 42.0)
        self.assertEqual(conflict_copy.supplier_quantity, 5)

    def test_collision_fails_closed_and_identity_matching_is_anchored(self):
        supplier = self._supplier()
        first = self._product(supplier, external_id="123", supplier_price=10.0)
        self._product(supplier, external_id="id-123-copy", supplier_price=11.0)
        db.session.commit()

        with patch(
            "services.supplier_price_sync.read_supplier_price_feed",
            return_value=self._feed({123: self._row(price=99.0)}),
        ):
            result = sync_supplier_price_feed(supplier.id)
        self.assertFalse(result.success)
        self.assertEqual(result.error_messages, ["source_identity_collision"])
        self.assertEqual(first.supplier_price, 10.0)

        # A numeric substring embedded in an arbitrary identifier is not a match.
        other = self._supplier(code="anchored-price-test")
        nonmatching = self._product(other, external_id="catalog-item-id-123-extra", supplier_price=13.0)
        db.session.commit()
        with patch(
            "services.supplier_price_sync.read_supplier_price_feed",
            return_value=self._feed({123: self._row(price=99.0)}),
        ):
            result = sync_supplier_price_feed(other.id)
        self.assertFalse(result.success)
        self.assertEqual(result.error_messages, ["no_matching_data_rows"])
        self.assertEqual(result.updated, 0)
        self.assertEqual(nonmatching.supplier_price, 13.0)

    def test_configuration_drift_after_fetch_rejects_rows(self):
        supplier = self._supplier()
        product = self._product(supplier, supplier_price=31.0)
        original_url = supplier.price_file_url
        db.session.commit()

        def change_config_during_read(*args, **kwargs):
            current = Supplier.query.filter_by(id=supplier.id).populate_existing().first()
            current.price_file_url = "https://supplier.invalid/replaced.csv"
            db.session.commit()
            return self._feed({123: self._row(price=88.0)})

        with patch(
            "services.supplier_price_sync.read_supplier_price_feed",
            side_effect=change_config_during_read,
        ):
            result = sync_supplier_price_feed(supplier.id)

        self.assertFalse(result.success)
        self.assertEqual(result.error_messages, ["configuration_changed"])
        self.assertEqual(product.supplier_price, 31.0)
        self.assertEqual(supplier.last_price_sync_error, "configuration_changed")
        self.assertNotEqual(supplier.price_file_url, original_url)

    def test_fresh_running_claim_rejects_overlap_but_stale_claim_recovers(self):
        supplier = self._supplier()
        product = self._product(supplier)
        supplier.last_price_sync_status = "running"
        supplier.last_price_sync_attempt_at = datetime.utcnow() - timedelta(seconds=10)
        db.session.commit()

        with patch("services.supplier_price_sync.read_supplier_price_feed") as reader:
            result = sync_supplier_price_feed(supplier.id)
            self.assertTrue(result.busy)
            self.assertFalse(result.success)
            reader.assert_not_called()

        supplier.last_price_sync_attempt_at = datetime.utcnow() - timedelta(minutes=3)
        db.session.commit()
        with patch(
            "services.supplier_price_sync.read_supplier_price_feed",
            return_value=self._feed({123: self._row(price=21.0)}),
        ):
            result = sync_supplier_price_feed(supplier.id)
        self.assertTrue(result.success)
        self.assertEqual(product.supplier_price, 21.0)

    def test_auto_tick_applies_minimum_interval_and_selects_one_due_supplier(self):
        too_recent = self._supplier(
            code="auto-too-recent",
            auto_sync_prices=True,
            auto_sync_interval_minutes=1,
        )
        too_recent.last_price_sync_attempt_at = datetime.utcnow() - timedelta(minutes=2)
        too_recent.last_price_sync_status = "failed"
        not_due = self._supplier(
            code="auto-not-due",
            auto_sync_prices=True,
            auto_sync_interval_minutes=20,
        )
        not_due.last_price_sync_attempt_at = datetime.utcnow() - timedelta(minutes=10)
        not_due.last_price_sync_status = "success"
        due = self._supplier(
            code="auto-due",
            auto_sync_prices=True,
            auto_sync_interval_minutes=1,
        )
        due_product = self._product(due, external_id="123", supplier_price=10.0)
        due.last_price_sync_attempt_at = datetime.utcnow() - timedelta(minutes=7)
        due.last_price_sync_status = "failed"
        disabled = self._supplier(
            code="auto-disabled",
            auto_sync_prices=False,
        )
        missing = self._supplier(
            code="auto-no-url",
            auto_sync_prices=True,
            price_file_url=None,
        )
        db.session.commit()

        with patch(
            "services.supplier_price_sync.read_supplier_price_feed",
            return_value=self._feed({123: self._row()}),
        ) as reader:
            result = run_auto_supplier_price_sync_tick(self.app)

        self.assertTrue(result.success)
        reader.assert_called_once_with(
            due.price_file_url,
            encoding="utf-8",
            delimiter=";",
        )
        self.assertEqual(due.last_price_sync_status, "success")
        self.assertEqual(due_product.supplier_price, 25.0)
        self.assertEqual(too_recent.last_price_sync_status, "failed")
        self.assertEqual(not_due.last_price_sync_status, "success")
        self.assertEqual(disabled.last_price_sync_attempt_at, None)
        self.assertEqual(missing.last_price_sync_attempt_at, None)


class SupplierPriceSyncMigrationTest(unittest.TestCase):
    def test_attempt_timestamp_migration_is_idempotent_and_preserves_rows(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        successful_at = "2026-11-20 12:34:56"
        try:
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE suppliers (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    code TEXT,
                    last_price_sync_at DATETIME
                );
                CREATE TABLE supplier_products (
                    id INTEGER PRIMARY KEY,
                    supplier_id INTEGER NOT NULL,
                    external_id TEXT
                );
                INSERT INTO suppliers(id, name, code, last_price_sync_at)
                VALUES (1, 'Legacy supplier', 'legacy', '2026-11-20 12:34:56');
                INSERT INTO supplier_products(id, supplier_id, external_id)
                VALUES (9, 1, '123');
                """
            )
            connection.commit()
            connection.close()

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertTrue(migrate(path))
                self.assertTrue(migrate(path))

            connection = sqlite3.connect(path)
            supplier_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(suppliers)")
            }
            supplier_values = connection.execute(
                "SELECT last_price_sync_at, last_price_sync_attempt_at FROM suppliers WHERE id=1"
            ).fetchone()
            product_values = connection.execute(
                "SELECT supplier_id, external_id FROM supplier_products WHERE id=9"
            ).fetchone()
            connection.close()

            self.assertIn("last_price_sync_attempt_at", supplier_columns)
            self.assertIn("last_price_sync_conflict_count", supplier_columns)
            self.assertEqual(supplier_values, (successful_at, None))
            self.assertEqual(product_values, (1, "123"))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
