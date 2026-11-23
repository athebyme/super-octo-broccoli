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
from services.supplier_color_observation_store import (
    MAX_OBSERVATIONS,
    SupplierColorObservationStore,
)
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

    def _last_attempt(self, store, *, digest=None, now=None):
        status = store.read_status(
            self.supplier.id,
            digest or supplier_catalog_config_digest(self.supplier),
            now=now or self.now,
        )
        self.assertIsNotNone(status)
        return status["last_attempt"]

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
            self.assertEqual(
                self._last_attempt(store)["outcome"], "snapshot_saved",
            )
            self.assertIs(
                self._last_attempt(store)["fetch_attempted"], True,
            )
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
            self.assertEqual(
                self._last_attempt(store)["outcome"], "feed_invalid",
            )
            self.assertIs(
                self._last_attempt(store)["fetch_attempted"], True,
            )

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
                attempt = self._last_attempt(store, digest=old_digest)
                self.assertEqual(attempt["outcome"], "cleared_without_read")
                self.assertIs(attempt["fetch_attempted"], False)
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
            attempt = self._last_attempt(
                store,
                digest=supplier_catalog_config_digest(self.supplier),
            )
            self.assertEqual(attempt["outcome"], "source_config_changed")
            self.assertIs(attempt["fetch_attempted"], True)
            self.assertEqual(
                self._stored_colors(store, digest=old_digest),
                ["бордо"],
            )

    def test_post_reservation_empty_and_oversized_scopes_close_without_fetch(self):
        from services import supplier_color_observation_scheduler as scheduler

        with TemporaryDirectory() as directory:
            store = self._store(directory)
            calls = []
            with patch.object(scheduler, "_linked_source_rows", return_value=({}, 0)):
                result = self._run(store, lambda url: calls.append(url) or _CSV)
            self.assertEqual(result["status"], "no_exact_ozon_links")
            self.assertEqual(result["reads"], 0)
            attempt = self._last_attempt(store)
            self.assertEqual(attempt["outcome"], "no_exact_ozon_links")
            self.assertIs(attempt["fetch_attempted"], False)
            self.assertEqual(calls, [])

        with TemporaryDirectory() as directory:
            store = self._store(directory)
            calls = []
            row = (
                self.supplier_product.external_id,
                self.supplier_product.content_revision,
                self.supplier_product.original_data_json,
            )
            with patch.object(
                scheduler,
                "_linked_source_rows",
                return_value=({self.supplier_product.id: row}, MAX_OBSERVATIONS + 1),
            ):
                result = self._run(store, lambda url: calls.append(url) or _CSV)
            self.assertEqual(result["status"], "source_scope_too_large")
            self.assertEqual(result["reads"], 0)
            attempt = self._last_attempt(store)
            self.assertEqual(attempt["outcome"], "source_scope_too_large")
            self.assertIs(attempt["fetch_attempted"], False)
            self.assertEqual(calls, [])

    def test_fetch_failure_is_durably_distinct_from_invalid_feed(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)

            def fail_fetch(_url):
                raise OSError("synthetic private detail must not be retained")

            result = self._run(store, fail_fetch)
            self.assertEqual(result["status"], "feed_failed")
            self.assertEqual(result["reads"], 1)
            attempt = self._last_attempt(store)
            self.assertEqual(attempt["outcome"], "feed_fetch_failed")
            self.assertIs(attempt["fetch_attempted"], True)
            self.assertNotIn("synthetic private detail", str(attempt))

    def test_post_read_admin_ozon_and_scope_deferrals_preserve_previous_snapshot(self):
        from services import supplier_color_observation_scheduler as scheduler

        # A newly queued admin refresh after the read leaves the old good batch.
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            old_digest = self._seed_old_observation(
                store, observed_at=self.now - timedelta(hours=25),
            )

            def queue_admin_refresh(_url):
                db.session.add(SupplierCatalogRefreshRun(
                    id="synthetic-post-read-refresh",
                    supplier_id=self.supplier.id,
                    admin_user_id=self.user.id,
                    status="pending",
                    config_digest="b" * 64,
                    created_at=self.now,
                ))
                db.session.commit()
                return _CSV

            result = self._run(store, queue_admin_refresh)
            self.assertEqual(result["status"], "post_read_deferred")
            attempt = self._last_attempt(store, digest=old_digest)
            self.assertEqual(attempt["outcome"], "admin_refresh_deferred")
            self.assertIs(attempt["fetch_attempted"], True)
            self.assertEqual(self._stored_colors(store, digest=old_digest), ["бордо"])
            refresh = SupplierCatalogRefreshRun.query.filter_by(
                id="synthetic-post-read-refresh",
            ).one()
            refresh.status = "completed"
            db.session.commit()

        # Disabling Ozon during the feed read is also an explicit deferred result.
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            old_digest = self._seed_old_observation(
                store, observed_at=self.now - timedelta(hours=25),
            )

            def disable_ozon(_url):
                self.app.config["MARKETPLACE_OZON_ENABLED"] = False
                return _CSV

            result = self._run(store, disable_ozon)
            self.assertEqual(result["status"], "post_read_deferred")
            attempt = self._last_attempt(store, digest=old_digest)
            self.assertEqual(attempt["outcome"], "ozon_disabled_after_read")
            self.assertIs(attempt["fetch_attempted"], True)
            self.assertEqual(self._stored_colors(store, digest=old_digest), ["бордо"])

        # A post-read cap race does not publish the sampled subset.
        self.app.config["MARKETPLACE_OZON_ENABLED"] = True
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            old_digest = self._seed_old_observation(
                store, observed_at=self.now - timedelta(hours=25),
            )
            original = scheduler._linked_source_rows
            calls = 0

            def oversized_after_read(supplier, *, limit):
                nonlocal calls
                calls += 1
                rows, count = original(supplier, limit=limit)
                return (rows, MAX_OBSERVATIONS + 1) if calls == 2 else (rows, count)

            with patch.object(
                scheduler, "_linked_source_rows", side_effect=oversized_after_read,
            ):
                result = self._run(store, lambda _url: _CSV)
            self.assertEqual(result["status"], "post_read_deferred")
            attempt = self._last_attempt(store, digest=old_digest)
            self.assertEqual(attempt["outcome"], "source_scope_too_large")
            self.assertIs(attempt["fetch_attempted"], True)
            self.assertEqual(self._stored_colors(store, digest=old_digest), ["бордо"])

    def test_invalid_shape_store_failure_and_unexpected_exception_record_safe_result(self):
        from types import SimpleNamespace
        from services import supplier_color_observation_scheduler as scheduler

        with TemporaryDirectory() as directory:
            store = self._store(directory)

            def invalid_shape(_supplier, *, fetch_bytes):
                fetch_bytes(self.supplier.csv_source_url)
                return SimpleNamespace(
                    total_rows=1,
                    colors_by_external_id={"sku-color-1": ("бордо",)},
                    body_sha256="bad",
                )

            with patch(
                "services.supplier_color_observation_feed.read_supplier_color_observations",
                side_effect=invalid_shape,
            ):
                result = self._run(store, lambda _url: _CSV)
            self.assertEqual(result["status"], "feed_invalid")
            attempt = self._last_attempt(store)
            self.assertEqual(attempt["outcome"], "feed_invalid")
            self.assertIs(attempt["fetch_attempted"], True)

        with TemporaryDirectory() as directory:
            store = self._store(directory)
            old_digest = self._seed_old_observation(
                store, observed_at=self.now - timedelta(hours=25),
            )
            with patch.object(store, "finish", return_value=False):
                result = self._run(store, lambda _url: _CSV)
            self.assertEqual(result["status"], "store_finish_failed")
            attempt = self._last_attempt(store, digest=old_digest)
            self.assertEqual(attempt["outcome"], "store_finish_failed")
            self.assertIs(attempt["fetch_attempted"], True)
            self.assertEqual(self._stored_colors(store, digest=old_digest), ["бордо"])

        with TemporaryDirectory() as directory:
            store = self._store(directory)
            calls = []
            with patch.object(
                scheduler,
                "_linked_source_rows",
                side_effect=RuntimeError("synthetic private failure detail"),
            ):
                result = self._run(store, lambda url: calls.append(url) or _CSV)
            self.assertEqual(result["status"], "local_failure")
            self.assertEqual(result["reads"], 0)
            attempt = self._last_attempt(store)
            self.assertEqual(attempt["outcome"], "local_failure")
            self.assertIs(attempt["fetch_attempted"], False)
            self.assertEqual(calls, [])


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
