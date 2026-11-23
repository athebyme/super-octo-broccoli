"""Synthetic coverage for the read-only supplier color observation tick."""

from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sqlalchemy import update

from models import (
    MarketplaceOperation,
    SellerSupplier,
    Supplier,
    SupplierProduct,
    db,
)
from services.product_sync_scheduler import init_scheduler, shutdown_scheduler
from services.supplier_color_observation_scheduler import (
    run_supplier_color_observation_tick,
)
from services.supplier_color_observation_store import SupplierColorObservationStore
from services.supplier_service import supplier_catalog_config_digest
from services.supplier_catalog_refresh_models import SupplierCatalogRefreshRun
from tests.test_marketplace_publications import OzonPublicationFixture


_COLOR_MAPPING = {
    "external_id": {"column": "id", "type": "string"},
    "title": {"column": "title", "type": "string"},
    "colors": {"column": "colors", "type": "list", "separator": "|"},
}
_CSV = "id;title;colors\nsku-color-1;Synthetic color;бордо\n".encode("utf-8")


@contextmanager
def _catalog_lock(_supplier_id):
    yield True


class SupplierColorObservationSchedulerTest(
    OzonPublicationFixture,
    unittest.TestCase,
):
    def setUp(self):
        super().setUp()
        self.app.config["MARKETPLACE_OZON_ENABLED"] = True
        self.supplier = Supplier(
            name="Synthetic direct color feed",
            code="synthetic-color-feed",
            is_active=True,
            csv_source_url="https://supplier.example.test/catalog.csv",
            csv_delimiter=";",
            csv_encoding="utf-8",
            csv_column_mapping=_COLOR_MAPPING,
            csv_has_header=True,
        )
        db.session.add(self.supplier)
        db.session.flush()
        self.supplier_product = SupplierProduct(
            supplier_id=self.supplier.id,
            external_id="sku-color-1",
            content_revision=7,
            colors_json=json.dumps(["бордо"], ensure_ascii=False),
            original_data_json=json.dumps(
                {"external_id": "sku-color-1", "title": "Synthetic color", "colors": ["бордо"]},
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        db.session.add(self.supplier_product)
        db.session.flush()
        self.source.supplier_product_id = self.supplier_product.id
        self.source.supplier_id = self.supplier.id
        self.source.source_type = self.supplier.code
        self.source.external_id = self.supplier_product.external_id
        self.source.supplier_content_revision = self.supplier_product.content_revision
        self.source.original_data = self.supplier_product.original_data_json
        self.source.colors = self.supplier_product.colors_json
        db.session.add(SellerSupplier(
            seller_id=self.seller.id,
            supplier_id=self.supplier.id,
            is_active=True,
        ))
        db.session.commit()
        self.now = datetime.utcnow()

    def _run(self, store, fetch_bytes=None, *, now=None):
        with patch(
            "services.supplier_service._supplier_catalog_sync_lock",
            _catalog_lock,
        ):
            return run_supplier_color_observation_tick(
                self.app,
                now=now or self.now,
                store=store,
                fetch_bytes=fetch_bytes,
            )

    def _store(self, directory):
        return SupplierColorObservationStore(Path(directory) / "color-proof.sqlite3")

    def _seed_old_observation(self, store, *, observed_at):
        digest = supplier_catalog_config_digest(self.supplier)
        token = store.reserve(self.supplier.id, digest, now=observed_at)
        self.assertIsNotNone(token)
        self.assertTrue(store.finish(
            self.supplier.id,
            token,
            digest,
            {
                self.supplier_product.id: (
                    self.supplier_product.content_revision,
                    hashlib.sha256(
                        self.supplier_product.external_id.encode("utf-8")
                    ).hexdigest(),
                    ("бордо",),
                ),
            },
            observed_at=observed_at,
        ))
        return digest

    def _stored_colors(self, store, *, digest=None, now=None):
        digest = digest or supplier_catalog_config_digest(self.supplier)
        return store.get(
            self.supplier.id,
            self.supplier_product.id,
            self.supplier_product.content_revision,
            hashlib.sha256(self.supplier_product.external_id.encode("utf-8")).hexdigest(),
            digest,
            now=now or self.now,
        )

    def test_real_parser_feed_stores_only_exact_current_linked_color(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            fetched = []

            def fetch(url):
                fetched.append(url)
                self.assertFalse(db.session().in_transaction())
                return _CSV

            result = self._run(store, fetch)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["reads"], 1)
            self.assertEqual(result["observations_stored"], 1)
            self.assertEqual(fetched, [self.supplier.csv_source_url])
            self.assertEqual(self._stored_colors(store), ["бордо"])
            self.assertEqual(MarketplaceOperation.query.count(), 0)
            self.assertEqual(self.supplier_product.content_revision, 7)
            self.assertEqual(self.source.supplier_content_revision, 7)

    def test_wrong_revision_or_disconnected_account_never_reads_feed(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            calls = []
            self.source.supplier_content_revision = 6
            db.session.commit()
            result = self._run(store, lambda url: calls.append(url) or _CSV)
            self.assertEqual(result["reads"], 0)
            self.assertEqual(calls, [])
            self.assertEqual(result["no_exact_ozon_links"], 1)

            self.source.supplier_content_revision = 7
            self.account.connection_status = "invalid"
            db.session.commit()
            result = self._run(store, lambda url: calls.append(url) or _CSV)
            self.assertEqual(result["reads"], 0)
            self.assertEqual(calls, [])
            self.assertEqual(result["no_exact_ozon_links"], 1)

    def test_feed_literal_conflict_replaces_batch_with_no_observations(self):
        conflicting = "id;title;colors\nsku-color-1;Synthetic color;белый\n".encode("utf-8")
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            result = self._run(store, lambda _url: conflicting)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["reads"], 1)
            self.assertEqual(result["feed_source_conflict"], 1)
            self.assertEqual(result["observations_stored"], 0)
            self.assertIsNone(self._stored_colors(store))

    def test_bad_config_and_invalid_feed_fail_closed_and_report_fetch_attempt(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            self.supplier.csv_column_mapping = {
                **_COLOR_MAPPING,
                "colors": {"column": "colors", "type": "string"},
            }
            db.session.commit()
            calls = []
            result = self._run(store, lambda url: calls.append(url) or _CSV)
            self.assertEqual(result["status"], "no_eligible_supplier")
            self.assertEqual(result["reads"], 0)
            self.assertEqual(calls, [])

            self.supplier.csv_column_mapping = _COLOR_MAPPING
            db.session.commit()
            result = self._run(store, lambda url: calls.append(url) or b"\xff")
            self.assertEqual(result["status"], "feed_failed")
            self.assertEqual(result["reads"], 1)
            self.assertEqual(result["feed_failed"], 1)
            self.assertIsNone(self._stored_colors(store))

    def test_admin_catalog_refresh_request_defers_without_feed_read(self):
        db.session.add(SupplierCatalogRefreshRun(
            id="synthetic-refresh-run",
            supplier_id=self.supplier.id,
            admin_user_id=self.user.id,
            status="pending",
            config_digest="a" * 64,
            created_at=self.now,
        ))
        db.session.commit()
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            calls = []
            result = self._run(store, lambda url: calls.append(url) or _CSV)

            self.assertEqual(result["status"], "admin_refresh_deferred")
            self.assertEqual(result["reads"], 0)
            self.assertEqual(calls, [])

    def test_native_or_malformed_marker_clears_old_fallback_without_read(self):
        for marker in (
            {
                "observed_supplier_fields_version": 1,
                "observed_supplier_fields": {"colors": ["бордо"]},
            },
            {
                "observed_supplier_fields_version": 2,
                "observed_supplier_fields": {"colors": ["бордо"]},
            },
        ):
            with self.subTest(marker=marker), TemporaryDirectory() as directory:
                store = self._store(directory)
                old_digest = self._seed_old_observation(
                    store,
                    observed_at=self.now - timedelta(hours=25),
                )
                snapshot = {
                    "external_id": self.supplier_product.external_id,
                    "title": "Synthetic color",
                    "colors": ["бордо"],
                    **marker,
                }
                serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
                self.supplier_product.original_data_json = serialized
                self.source.original_data = serialized
                db.session.commit()

                calls = []
                result = self._run(store, lambda url: calls.append(url) or _CSV)

                self.assertEqual(result["status"], "cleared_without_read")
                self.assertEqual(result["reads"], 0)
                self.assertEqual(calls, [])
                self.assertIsNone(
                    self._stored_colors(store, digest=old_digest),
                )

    def test_config_change_during_feed_read_preserves_previous_batch(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            old_digest = self._seed_old_observation(
                store,
                observed_at=self.now - timedelta(hours=25),
            )

            def fetch(_url):
                self.assertFalse(db.session().in_transaction())
                db.session.execute(
                    update(Supplier)
                    .filter_by(id=self.supplier.id)
                    .values(csv_delimiter=",")
                )
                db.session.commit()
                return _CSV

            result = self._run(store, fetch)

            self.assertEqual(result["status"], "post_read_deferred")
            self.assertEqual(result["reads"], 1)
            self.assertEqual(result["source_config_changed"], 1)
            self.assertEqual(
                self._stored_colors(store, digest=old_digest),
                ["бордо"],
            )


class _FakeScheduler:
    def __init__(self, **_kwargs):
        self.jobs = {}
        self.running = False

    def add_job(self, func, trigger, **kwargs):
        self.jobs[kwargs["id"]] = {"func": func, "trigger": trigger, **kwargs}

    def start(self):
        self.running = True

    def shutdown(self, wait=False):
        self.running = False


class SupplierColorSchedulerRegistrationTest(
    OzonPublicationFixture,
    unittest.TestCase,
):
    def tearDown(self):
        shutdown_scheduler()
        super().tearDown()

    def test_scheduler_registers_delayed_singleton_interval(self):
        from services import product_sync_scheduler

        fake = _FakeScheduler()
        with patch.object(product_sync_scheduler, "scheduler", None), patch.object(
            product_sync_scheduler,
            "BackgroundScheduler",
            return_value=fake,
        ), patch.object(
            product_sync_scheduler,
            "_acquire_scheduler_process_lock",
            return_value=True,
        ):
            initialized = init_scheduler(self.app)

        self.assertIs(initialized, fake)
        job = fake.jobs["supplier_color_observation"]
        self.assertEqual(job["trigger"].interval.total_seconds(), 30 * 60)
        self.assertEqual(job["max_instances"], 1)
        self.assertTrue(job["coalesce"])
        self.assertGreaterEqual(
            (job["next_run_time"] - datetime.now(job["next_run_time"].tzinfo)).total_seconds(),
            110,
        )


if __name__ == "__main__":
    unittest.main()
