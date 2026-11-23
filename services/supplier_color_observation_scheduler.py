"""Bounded periodic refresh of directly observed supplier color cells.

This worker only reads configured public supplier CSV feeds and writes the
private color-observation ledger. It never updates supplier, seller, Ozon, or
marketplace rows.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import logging
import re
from types import SimpleNamespace
from typing import Any, Optional

from sqlalchemy import and_

from models import (
    ImportedProduct,
    Marketplace,
    SellerMarketplaceAccount,
    SellerSupplier,
    Supplier,
    SupplierProduct,
    db,
)


logger = logging.getLogger(__name__)
_HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _utc_datetime(value: Optional[datetime]) -> datetime:
    current = value or datetime.utcnow()
    if current.tzinfo is not None and current.utcoffset() is not None:
        return current.astimezone(timezone.utc).replace(tzinfo=None)
    return current.replace(tzinfo=None)


def _result(status: str, **counts: int) -> dict:
    safe_counts = {
        key: value for key, value in counts.items()
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    }
    return {"status": status, **safe_counts}


def _linked_source_query(supplier: Supplier, *, with_snapshot: bool):
    """Build an exact seller/Ozon/current-revision source-row query."""
    columns = [SupplierProduct.id]
    if with_snapshot:
        columns.extend((
            SupplierProduct.external_id,
            SupplierProduct.content_revision,
            SupplierProduct.original_data_json,
        ))
    return (
        db.session.query(*columns)
        .select_from(SupplierProduct)
        .join(
            ImportedProduct,
            and_(
                ImportedProduct.supplier_product_id == SupplierProduct.id,
                ImportedProduct.supplier_id == SupplierProduct.supplier_id,
                ImportedProduct.external_id == SupplierProduct.external_id,
                ImportedProduct.source_type == supplier.code,
                ImportedProduct.supplier_content_revision
                == SupplierProduct.content_revision,
            ),
        )
        .join(
            SellerSupplier,
            and_(
                SellerSupplier.seller_id == ImportedProduct.seller_id,
                SellerSupplier.supplier_id == SupplierProduct.supplier_id,
                SellerSupplier.is_active.is_(True),
            ),
        )
        .join(
            SellerMarketplaceAccount,
            and_(
                SellerMarketplaceAccount.seller_id == ImportedProduct.seller_id,
                SellerMarketplaceAccount.is_active.is_(True),
                SellerMarketplaceAccount.connection_status == "connected",
            ),
        )
        .join(
            Marketplace,
            and_(
                Marketplace.id == SellerMarketplaceAccount.marketplace_id,
                Marketplace.code == "ozon",
                Marketplace.is_active.is_(True),
            ),
        )
        .filter(
            SupplierProduct.supplier_id == supplier.id,
            SupplierProduct.content_revision > 0,
            SupplierProduct.external_id.isnot(None),
            SupplierProduct.external_id != "",
            ImportedProduct.supplier_content_revision > 0,
            ImportedProduct.external_id.isnot(None),
            ImportedProduct.external_id != "",
        )
        .distinct()
        .order_by(SupplierProduct.id.asc())
    )


def _has_exact_linked_source(supplier: Supplier) -> bool:
    """Cheap existence check before durable reservation or raw-row loading."""
    return _linked_source_query(supplier, with_snapshot=False).limit(1).first() is not None


def _linked_source_rows(
    supplier: Supplier,
    *,
    limit: int,
) -> tuple[dict[int, tuple[str, int, Any]], int]:
    """Read bounded exact source rows only after the daily reservation."""
    rows = _linked_source_query(
        supplier,
        with_snapshot=True,
    ).limit(limit).all()
    result: dict[int, tuple[str, int, Any]] = {}
    for product_id, external_id, revision, raw_snapshot in rows:
        if (
            type(product_id) is not int
            or product_id <= 0
            or not isinstance(external_id, str)
            or not external_id.strip()
            or external_id != external_id.strip()
            or type(revision) is not int
            or revision <= 0
        ):
            continue
        result[product_id] = (external_id, revision, raw_snapshot)
    return result, len(rows)


def _admin_refresh_active(supplier_id: int) -> bool:
    from services.supplier_catalog_refresh_models import SupplierCatalogRefreshRun

    return SupplierCatalogRefreshRun.query.filter(
        SupplierCatalogRefreshRun.supplier_id == supplier_id,
        SupplierCatalogRefreshRun.status.in_(("pending", "running")),
    ).limit(1).first() is not None


def _snapshot_state(raw_snapshot: Any) -> tuple[str, bool, Optional[list[str]]]:
    """Classify copied raw evidence without deriving or normalizing a hue."""
    from services.supplier_field_observations import (
        load_source_snapshot,
        observed_supplier_fields,
        source_values,
    )

    snapshot = load_source_snapshot(raw_snapshot)
    if snapshot is None:
        return "snapshot_invalid", False, None
    present, valid, fields = observed_supplier_fields(snapshot)
    if present and not valid:
        return "marker_invalid", False, None
    if valid and "colors" in fields:
        return "native_marker", True, None
    raw_colors = source_values(snapshot.get("colors"), "colors")
    if raw_colors is None:
        return "raw_color_unavailable", valid, None
    return "eligible", valid, raw_colors


def _digest_external_id(external_id: str) -> str:
    return hashlib.sha256(external_id.encode("utf-8")).hexdigest()


def _feed_shape_is_valid(feed: Any) -> bool:
    return bool(
        type(getattr(feed, "total_rows", None)) is int
        and feed.total_rows > 0
        and isinstance(getattr(feed, "colors_by_external_id", None), dict)
        and isinstance(getattr(feed, "body_sha256", None), str)
        and _HEX_SHA256_RE.fullmatch(feed.body_sha256)
    )


def run_supplier_color_observation_tick(
    flask_app,
    *,
    now: Optional[datetime] = None,
    store=None,
    fetch_bytes=None,
) -> dict:
    """Refresh at most one due connected supplier through its configured CSV.

    The application database is read before and after the public feed read;
    its transaction is explicitly ended during network I/O. The only durable
    write is an atomic replacement in the private evidence ledger.
    """
    current_time = _utc_datetime(now)
    with flask_app.app_context():
        if not flask_app.config.get("MARKETPLACE_OZON_ENABLED", False):
            return _result("ozon_disabled", suppliers_considered=0, reads=0)

        from services.supplier_color_observation_store import MAX_OBSERVATIONS

        if store is None:
            try:
                from services.supplier_color_observation_store import (
                    configured_supplier_color_observation_store,
                )

                store = configured_supplier_color_observation_store()
            except Exception:
                store = None
        if store is None:
            return _result("store_unavailable", suppliers_considered=0, reads=0)

        from services.supplier_service import (
            _supplier_catalog_config_snapshot,
            _supplier_catalog_sync_lock,
            supplier_catalog_config_digest,
        )
        from services.supplier_color_observation_feed import (
            SupplierColorFeedError,
            read_supplier_color_observations,
            supports_direct_color,
        )
        from services.supplier_field_observations import same_color_literals

        supplier_ids = [
            supplier_id
            for (supplier_id,) in db.session.query(Supplier.id)
            .filter(Supplier.is_active.is_(True))
            .order_by(Supplier.id.asc())
            .all()
            if type(supplier_id) is int and supplier_id > 0
        ]
        db.session.rollback()
        counts = Counter({"reads": 0})

        for supplier_id in supplier_ids:
            counts["suppliers_considered"] += 1
            with _supplier_catalog_sync_lock(supplier_id) as acquired:
                if not acquired:
                    counts["catalog_busy"] += 1
                    db.session.rollback()
                    continue

                supplier = (
                    Supplier.query.filter_by(id=supplier_id)
                    .populate_existing()
                    .first()
                )
                if supplier is None or not supplier.is_active:
                    db.session.rollback()
                    continue
                try:
                    initial_config = _supplier_catalog_config_snapshot(supplier)
                    initial_digest = supplier_catalog_config_digest(supplier)
                    frozen_supplier = SimpleNamespace(**initial_config)
                    direct_supported = supports_direct_color(frozen_supplier)
                except Exception:
                    db.session.rollback()
                    counts["invalid_config"] += 1
                    continue
                if not direct_supported:
                    db.session.rollback()
                    counts["unsupported_config"] += 1
                    continue

                if _admin_refresh_active(supplier_id):
                    db.session.rollback()
                    counts["admin_refresh_deferred"] += 1
                    continue

                if not _has_exact_linked_source(supplier):
                    db.session.rollback()
                    counts["no_exact_ozon_links"] += 1
                    continue

                try:
                    claim = store.reserve(
                        supplier_id,
                        initial_digest,
                        now=current_time,
                    )
                except Exception:
                    claim = None
                    counts["store_reserve_failed"] += 1
                if claim is None:
                    db.session.rollback()
                    counts["not_due_or_store_blocked"] += 1
                    continue

                # Avoid pulling the raw source snapshots on each scheduler
                # pass. Even after a due reservation, never partially replace
                # a ledger whose exact linked scope exceeds its storage cap.
                pre_rows, pre_row_count = _linked_source_rows(
                    supplier,
                    limit=MAX_OBSERVATIONS + 1,
                )
                if not pre_rows:
                    db.session.rollback()
                    counts["no_exact_ozon_links"] += 1
                    return _result("no_exact_ozon_links", **counts)
                if pre_row_count > MAX_OBSERVATIONS:
                    db.session.rollback()
                    counts["source_scope_too_large"] += 1
                    return _result("source_scope_too_large", **counts)

                # Do not hold an application-database read transaction while
                # the public feed transport is in progress.
                db.session.rollback()
                feed = None
                feed_error = None
                fetch_attempts = 0
                potential_color_rows = []
                for product_id, (external_id, revision, raw_snapshot) in pre_rows.items():
                    state, _marker_valid, colors = _snapshot_state(raw_snapshot)
                    if state == "eligible" and colors:
                        potential_color_rows.append(product_id)

                if potential_color_rows:
                    try:
                        if fetch_bytes is None:
                            from services.supplier_price_feed import read_supplier_csv_bytes

                            fetcher = read_supplier_csv_bytes
                        else:
                            fetcher = fetch_bytes

                        def counted_fetch(url):
                            nonlocal fetch_attempts
                            fetch_attempts += 1
                            return fetcher(url)

                        feed = read_supplier_color_observations(
                            frozen_supplier,
                            fetch_bytes=counted_fetch,
                        )
                    except SupplierColorFeedError as exc:
                        feed_error = exc.code
                    except Exception:
                        feed_error = "feed_failed"
                    counts["reads"] += fetch_attempts
                else:
                    # A valid empty replacement revokes stale observations for
                    # rows that are now native, malformed, or no longer raw.
                    feed = None

                if feed_error is not None:
                    db.session.rollback()
                    counts["feed_failed"] += 1
                    logger.info(
                        "Supplier color observation tick status=feed_failed code=%s",
                        feed_error,
                    )
                    return _result(
                        "feed_failed",
                        **counts,
                    )

                post_supplier = (
                    Supplier.query.filter_by(id=supplier_id)
                    .populate_existing()
                    .first()
                )
                post_rows, post_row_count = (
                    _linked_source_rows(
                        post_supplier,
                        limit=MAX_OBSERVATIONS + 1,
                    )
                    if post_supplier is not None and post_supplier.is_active
                    else ({}, 0)
                )
                try:
                    post_config = (
                        _supplier_catalog_config_snapshot(post_supplier)
                        if post_supplier is not None else None
                    )
                    post_digest = (
                        supplier_catalog_config_digest(post_supplier)
                        if post_supplier is not None else None
                    )
                except Exception:
                    post_config = None
                    post_digest = None
                config_unchanged = bool(
                    post_config == initial_config
                    and post_digest == initial_digest
                    and post_supplier is not None
                    and post_supplier.is_active
                )
                admin_refresh_active = _admin_refresh_active(supplier_id)

                if (
                    not config_unchanged
                    or admin_refresh_active
                    or not flask_app.config.get("MARKETPLACE_OZON_ENABLED", False)
                    or post_row_count > MAX_OBSERVATIONS
                ):
                    # Preserve the last successful same-config batch. The
                    # store already fences it by config digest, revision, ID,
                    # and age; a queued admin refresh must not replace it with
                    # a projection taken across the refresh boundary.
                    db.session.rollback()
                    counts["source_config_changed"] += int(not config_unchanged)
                    counts["admin_refresh_deferred"] += int(admin_refresh_active)
                    counts["ozon_disabled_after_read"] += int(
                        not flask_app.config.get("MARKETPLACE_OZON_ENABLED", False)
                    )
                    counts["source_scope_too_large"] += int(
                        post_row_count > MAX_OBSERVATIONS
                    )
                    return _result("post_read_deferred", **counts)

                observations: dict[int, tuple[int, str, tuple[str, ...]]] = {}
                duplicate_external_ids = {
                    external_id
                    for external_id, occurrence_count in Counter(
                        row[0] for row in post_rows.values()
                    ).items()
                    if occurrence_count > 1
                }
                if feed is not None and not _feed_shape_is_valid(feed):
                    db.session.rollback()
                    counts["feed_failed"] += 1
                    return _result("feed_invalid", **counts)

                if feed is not None:
                    for product_id, (external_id, revision, raw_snapshot) in pre_rows.items():
                        current_row = post_rows.get(product_id)
                        if current_row != (external_id, revision, raw_snapshot):
                            counts["source_changed"] += 1
                            continue
                        if external_id in duplicate_external_ids:
                            counts["duplicate_source_id"] += 1
                            continue
                        state, _marker_valid, raw_colors = _snapshot_state(raw_snapshot)
                        if state == "native_marker":
                            counts["native_marker_skipped"] += 1
                            continue
                        if state != "eligible" or not raw_colors:
                            counts[f"{state}_skipped"] += 1
                            continue
                        direct_colors = feed.colors_by_external_id.get(external_id)
                        if (
                            not isinstance(direct_colors, (list, tuple))
                            or not direct_colors
                            or not same_color_literals(list(direct_colors), raw_colors)
                        ):
                            counts["feed_source_conflict"] += 1
                            continue
                        # Store exact CSV cell literals. The comparer is only
                        # an equality gate; it never normalizes or translates.
                        observations[product_id] = (
                            revision,
                            _digest_external_id(external_id),
                            tuple(direct_colors),
                        )
                else:
                    counts["source_changed"] += len(pre_rows)

                db.session.rollback()
                try:
                    finished = store.finish(
                        supplier_id,
                        claim,
                        initial_digest,
                        observations,
                        observed_at=current_time,
                    )
                except Exception:
                    finished = False
                if not finished:
                    counts["store_finish_failed"] += 1
                    return _result("store_finish_failed", **counts)

                counts["observations_stored"] += len(observations)
                counts["observed_feed_rows"] += feed.total_rows if feed is not None else 0
                status = "completed" if feed is not None else "cleared_without_read"
                logger.info(
                    "Supplier color observation tick status=%s reads=%s observations=%s",
                    status,
                    counts["reads"],
                    len(observations),
                )
                return _result(status, **counts)

        db.session.rollback()
        if counts["not_due_or_store_blocked"]:
            status = "no_due_supplier"
        elif counts["catalog_busy"]:
            status = "catalog_busy"
        elif counts["admin_refresh_deferred"]:
            status = "admin_refresh_deferred"
        else:
            status = "no_eligible_supplier"
        return _result(status, **counts)


__all__ = ["run_supplier_color_observation_tick"]
