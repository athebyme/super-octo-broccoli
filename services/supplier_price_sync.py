"""Durable orchestration for supplier price and stock feed reads."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import re
import time
from typing import Any, Optional

from sqlalchemy import and_

from models import ImportedProduct, SellerSupplier, Supplier, SupplierProduct, db
from services.supplier_price_feed import (
    SupplierPriceFeed,
    SupplierPriceFeedError,
    SupplierPriceRow,
    read_supplier_price_feed,
)


logger = logging.getLogger(__name__)

MIN_AUTO_INTERVAL_MINUTES = 5
DEFAULT_AUTO_INTERVAL_MINUTES = 60
RUNNING_CLAIM_STALE_AFTER = timedelta(seconds=120)
MAX_SOURCE_ROWS = 50_000
MAX_SOURCE_PRODUCTS = 50_000
MAX_IMPORTED_TARGETS = 50_000
IMPORTED_QUERY_CHUNK = 500


def _result(**kwargs):
    # Delayed import keeps supplier_service's existing imports free of a cycle.
    from services.supplier_service import SyncResult

    return SyncResult(**kwargs)


def _begin_writer_transaction():
    """Start a short transaction that owns SQLite's writer slot."""
    db.session.rollback()
    try:
        connection = db.session.connection()
        if connection.dialect.name == "sqlite":
            raw_connection = connection.connection.driver_connection
            if getattr(raw_connection, "in_transaction", False):
                raise RuntimeError("supplier_price_transaction_not_fresh")
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        return connection
    except Exception:
        db.session.rollback()
        raise


def _is_database_busy(error: BaseException) -> bool:
    text = str(error).casefold()
    return "database is locked" in text or "database is busy" in text


def _effective_interval_minutes(value: Any) -> int:
    try:
        return max(MIN_AUTO_INTERVAL_MINUTES, int(value))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_AUTO_INTERVAL_MINUTES


def _safe_error_code(error: BaseException) -> str:
    raw_code = getattr(error, "code", None)
    if not isinstance(raw_code, str):
        return "feed_unavailable"
    code = raw_code.strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{1,64}", code):
        return "feed_unavailable"
    return code


def _source_identity(external_id: Optional[str]) -> Optional[int]:
    """Read only exact numeric IDs and anchored, documented legacy formats."""
    if not isinstance(external_id, str):
        return None
    value = external_id.strip()
    if re.fullmatch(r"[0-9]+", value):
        number = int(value)
        return number if number > 0 else None
    match = re.fullmatch(r"id[-_]([0-9]+)(?:[-_][A-Za-z0-9]+)*", value, re.IGNORECASE)
    if match:
        number = int(match.group(1))
        return number if number > 0 else None
    match = re.fullmatch(r"0T-([0-9]+)", value, re.IGNORECASE)
    if match:
        number = int(match.group(1))
        return number if number > 0 else None
    return None


def _validate_feed(feed: Any) -> tuple[dict[int, SupplierPriceRow], frozenset[int]]:
    """Apply a bounded envelope check around the reader's typed contract."""
    if not isinstance(feed, SupplierPriceFeed):
        raise ValueError("invalid_feed")
    rows = feed.rows
    conflicts = feed.conflicting_ids
    if not isinstance(rows, dict) or not isinstance(conflicts, frozenset):
        raise ValueError("invalid_feed")
    if len(rows) + len(conflicts) > MAX_SOURCE_ROWS:
        raise ValueError("feed_too_large")
    for source_id, row in rows.items():
        if type(source_id) is not int or source_id <= 0:
            raise ValueError("invalid_source_identity")
        if not isinstance(row, SupplierPriceRow):
            raise ValueError("invalid_feed_row")
    for source_id in conflicts:
        if type(source_id) is not int or source_id <= 0:
            raise ValueError("invalid_source_identity")
    safe_rows = {
        source_id: row for source_id, row in rows.items()
        if source_id not in conflicts
    }
    return safe_rows, conflicts


def _is_due(last_attempt: Optional[datetime], interval_minutes: Any, now: datetime) -> bool:
    if last_attempt is None:
        return True
    elapsed_seconds = (now - last_attempt).total_seconds()
    interval_seconds = _effective_interval_minutes(interval_minutes) * 60
    return elapsed_seconds >= interval_seconds


def _failure_result(code: str, started_at: float, *, busy: bool = False):
    result = _result(
        success=False,
        errors=0 if busy else 1,
        error_messages=[code],
        busy=busy,
        duration_seconds=max(0.0, time.monotonic() - started_at),
    )
    return result


def _claim_supplier(
    supplier_id: int,
    *,
    automatic: bool,
    now: datetime,
    expected_config: Optional[dict] = None,
):
    """Commit a durable run claim and return the config snapshot/token."""
    _begin_writer_transaction()
    try:
        supplier = (
            Supplier.query.filter_by(id=supplier_id)
            .populate_existing()
            .first()
        )
        if supplier is None:
            db.session.rollback()
            return None, "supplier_not_found"
        if expected_config is not None:
            current_config = {
                "price_file_url": supplier.price_file_url,
                "encoding": supplier.price_file_encoding or "cp1251",
                "delimiter": supplier.price_file_delimiter or ";",
            }
            if not isinstance(expected_config, dict) or current_config != expected_config:
                db.session.rollback()
                return None, "source_changed"
        if automatic and (not supplier.is_active or not supplier.auto_sync_prices):
            db.session.rollback()
            return None, "automatic_sync_disabled"
        if not isinstance(supplier.price_file_url, str) or not supplier.price_file_url.strip():
            db.session.rollback()
            return None, "price_feed_not_configured"

        previous_attempt = supplier.last_price_sync_attempt_at
        if supplier.last_price_sync_status == "running" and previous_attempt is not None:
            if now - previous_attempt < RUNNING_CLAIM_STALE_AFTER:
                db.session.rollback()
                return None, "sync_busy"

        if automatic and not _is_due(
            previous_attempt, supplier.auto_sync_interval_minutes, now,
        ):
            db.session.rollback()
            return None, "sync_not_due"

        claim_at = now
        if previous_attempt is not None and claim_at <= previous_attempt:
            claim_at = previous_attempt + timedelta(microseconds=1)

        snapshot = {
            "price_file_url": supplier.price_file_url,
            "encoding": supplier.price_file_encoding or "cp1251",
            "delimiter": supplier.price_file_delimiter or ";",
            "auto_sync_interval_minutes": supplier.auto_sync_interval_minutes,
            "claim_at": claim_at,
        }
        supplier.last_price_sync_attempt_at = claim_at
        supplier.last_price_sync_status = "running"
        supplier.last_price_sync_error = None
        supplier.last_price_sync_conflict_count = None
        db.session.commit()
        return snapshot, None
    except Exception:
        db.session.rollback()
        raise


def _mark_owned_failure(supplier_id: int, claim_at: datetime, code: str) -> bool:
    _begin_writer_transaction()
    try:
        supplier = (
            Supplier.query.filter_by(id=supplier_id)
            .populate_existing()
            .first()
        )
        if (
            supplier is None
            or supplier.last_price_sync_attempt_at != claim_at
            or supplier.last_price_sync_status != "running"
        ):
            db.session.rollback()
            return False
        supplier.last_price_sync_status = "failed"
        supplier.last_price_sync_error = code[:80]
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        raise


def _still_owns_claim(supplier: Optional[Supplier], claim_at: datetime) -> bool:
    return bool(
        supplier is not None
        and supplier.last_price_sync_attempt_at == claim_at
        and supplier.last_price_sync_status == "running"
    )


def _config_is_current(supplier: Supplier, snapshot: dict, *, automatic: bool) -> bool:
    if (
        supplier.price_file_url != snapshot["price_file_url"]
        or (supplier.price_file_encoding or "cp1251") != snapshot["encoding"]
        or (supplier.price_file_delimiter or ";") != snapshot["delimiter"]
    ):
        return False
    if automatic and (
        not supplier.is_active
        or not supplier.auto_sync_prices
        or supplier.auto_sync_interval_minutes != snapshot["auto_sync_interval_minutes"]
    ):
        return False
    return True


def _apply_feed(
    supplier_id: int,
    snapshot: dict,
    feed: dict[int, SupplierPriceRow],
    conflicting_ids: frozenset[int],
    *,
    automatic: bool,
    now: datetime,
    started_at: float,
):
    _begin_writer_transaction()
    try:
        supplier = (
            Supplier.query.filter_by(id=supplier_id)
            .populate_existing()
            .first()
        )
        if not _still_owns_claim(supplier, snapshot["claim_at"]):
            db.session.rollback()
            return _failure_result("sync_claim_lost", started_at, busy=True)
        if not _config_is_current(supplier, snapshot, automatic=automatic):
            supplier.last_price_sync_status = "failed"
            supplier.last_price_sync_error = "configuration_changed"
            db.session.commit()
            return _failure_result("configuration_changed", started_at)

        if not feed:
            supplier.last_price_sync_status = "failed"
            supplier.last_price_sync_error = "empty_feed"
            db.session.commit()
            return _failure_result("empty_feed", started_at)

        products = (
            SupplierProduct.query.filter_by(supplier_id=supplier_id)
            .populate_existing()
            .limit(MAX_SOURCE_PRODUCTS + 1)
            .all()
        )
        if len(products) > MAX_SOURCE_PRODUCTS:
            supplier.last_price_sync_status = "failed"
            supplier.last_price_sync_error = "supplier_catalog_too_large"
            db.session.commit()
            return _failure_result("supplier_catalog_too_large", started_at)
        identity_products: dict[int, list[SupplierProduct]] = {}
        for product in products:
            source_id = _source_identity(product.external_id)
            if source_id is not None:
                identity_products.setdefault(source_id, []).append(product)

        collisions = [source_id for source_id in feed if len(identity_products.get(source_id, ())) > 1]
        if collisions:
            supplier.last_price_sync_status = "failed"
            supplier.last_price_sync_error = "source_identity_collision"
            db.session.commit()
            return _failure_result("source_identity_collision", started_at)

        matched_rows = []
        data_bearing_count = 0
        for source_id, record in feed.items():
            matching = identity_products.get(source_id, [])
            if not matching:
                continue
            product = matching[0]
            matched_rows.append((product, record))
            if record.price is not None or record.quantity is not None:
                data_bearing_count += 1

        if data_bearing_count == 0:
            supplier.last_price_sync_status = "failed"
            supplier.last_price_sync_error = "no_matching_data_rows"
            db.session.commit()
            return _failure_result("no_matching_data_rows", started_at)

        cascade_fields = {}
        for product, record in matched_rows:
            if record.price is not None or record.quantity is not None:
                fields = cascade_fields.setdefault(product.id, {})
                if record.price is not None and record.price > 0:
                    fields["price"] = record.price
                if record.quantity is not None:
                    fields["quantity"] = record.quantity

        imported_targets = []
        affected_product_ids = list(cascade_fields)
        for chunk_start in range(0, len(affected_product_ids), IMPORTED_QUERY_CHUNK):
            chunk = affected_product_ids[
                chunk_start:chunk_start + IMPORTED_QUERY_CHUNK
            ]
            remaining_cap = MAX_IMPORTED_TARGETS - len(imported_targets)
            chunk_targets = (
                db.session.query(ImportedProduct, SupplierProduct)
                .join(
                    SupplierProduct,
                    and_(
                        ImportedProduct.supplier_product_id == SupplierProduct.id,
                        SupplierProduct.supplier_id == supplier_id,
                    ),
                )
                .join(
                    SellerSupplier,
                    and_(
                        SellerSupplier.seller_id == ImportedProduct.seller_id,
                        SellerSupplier.supplier_id == supplier_id,
                        SellerSupplier.is_active.is_(True),
                    ),
                )
                .filter(
                    ImportedProduct.supplier_id == supplier_id,
                    ImportedProduct.supplier_product_id.in_(chunk),
                )
                .populate_existing()
                .limit(max(1, remaining_cap + 1))
                .all()
            )
            imported_targets.extend(chunk_targets)
            if len(imported_targets) > MAX_IMPORTED_TARGETS:
                supplier.last_price_sync_status = "failed"
                supplier.last_price_sync_error = "seller_targets_too_large"
                db.session.commit()
                return _failure_result("seller_targets_too_large", started_at)

        for product, record in matched_rows:
            old_price = product.supplier_price
            price = record.price
            if price is not None:
                if price > 0:
                    product.supplier_price = price
                    product.last_price_sync_at = now
                    if old_price is not None and old_price != price:
                        product.previous_price = old_price
                        product.price_changed_at = now
                elif price == 0:
                    # A zero observation must not replace the seller's last
                    # positive amount or count as fresh price evidence.
                    product.last_price_sync_at = None

            if record.quantity is not None:
                product.supplier_quantity = record.quantity

            status = record.status.strip() if isinstance(record.status, str) else None
            if status == "":
                status = None
            quantity = record.quantity
            if status is not None or quantity is not None:
                if status == "1" or (quantity is not None and quantity > 0):
                    product.supplier_status = "in_stock"
                else:
                    product.supplier_status = "out_of_stock"

            if not automatic:
                if record.vendor_code:
                    product.vendor_code = record.vendor_code
                if record.additional_vendor_code:
                    product.additional_vendor_code = record.additional_vendor_code
                if record.barcode:
                    product.barcode = record.barcode
            if record.rrp is not None and record.rrp > 0:
                product.recommended_retail_price = record.rrp

        seller_rows_updated = 0
        for imported, product in imported_targets:
            fields = cascade_fields.get(product.id)
            if not fields:
                continue
            changed = False
            if "price" in fields and imported.supplier_price != fields["price"]:
                imported.supplier_price = fields["price"]
                changed = True
            if "quantity" in fields and imported.supplier_quantity != fields["quantity"]:
                imported.supplier_quantity = fields["quantity"]
                changed = True
            if changed:
                imported.updated_at = now
                seller_rows_updated += 1

        supplier.last_price_sync_at = now
        supplier.last_price_sync_status = "partial" if conflicting_ids else "success"
        supplier.last_price_sync_error = "ambiguous_source_rows" if conflicting_ids else None
        supplier.last_price_sync_conflict_count = len(conflicting_ids)
        db.session.commit()

        total_source_rows = len(set(feed).union(conflicting_ids))
        result = _result(
            success=True,
            total_in_csv=total_source_rows,
            updated=data_bearing_count,
            skipped=max(0, total_source_rows - data_bearing_count),
            errors=len(conflicting_ids),
            error_messages=(
                ["ambiguous_source_rows"] if conflicting_ids else []
            ),
            seller_rows_updated=seller_rows_updated,
            conflicting_source_rows=len(conflicting_ids),
            duration_seconds=max(0.0, time.monotonic() - started_at),
        )
        return result
    except Exception:
        db.session.rollback()
        raise


def sync_supplier_price_feed(
    supplier_id: int,
    *,
    automatic: bool = False,
    expected_config: Optional[dict] = None,
):
    """Claim, read, validate, and atomically apply one supplier price feed."""
    started_at = time.monotonic()
    now = datetime.utcnow()
    try:
        snapshot, claim_error = _claim_supplier(
            supplier_id,
            automatic=automatic,
            now=now,
            expected_config=expected_config,
        )
    except Exception as error:
        if _is_database_busy(error):
            return _failure_result("database_busy", started_at, busy=True)
        logger.warning("Supplier price sync claim failed (%s)", type(error).__name__)
        return _failure_result("claim_failed", started_at)

    if snapshot is None:
        return _failure_result(
            claim_error or "sync_unavailable",
            started_at,
            busy=claim_error == "sync_busy",
        )

    try:
        source_feed = read_supplier_price_feed(
            snapshot["price_file_url"],
            encoding=snapshot["encoding"],
            delimiter=snapshot["delimiter"],
        )
        normalized_feed, conflicting_ids = _validate_feed(source_feed)
    except Exception as error:
        if isinstance(error, SupplierPriceFeedError):
            code = _safe_error_code(error)
        elif isinstance(error, ValueError):
            code = str(error) if re.fullmatch(r"[a-z0-9_]{1,64}", str(error)) else "invalid_feed"
        else:
            code = "feed_unavailable"
        try:
            _mark_owned_failure(supplier_id, snapshot["claim_at"], code)
        except Exception as finalize_error:
            logger.warning(
                "Supplier price sync failure finalization failed (%s)",
                type(finalize_error).__name__,
            )
        return _failure_result(code, started_at)

    try:
        return _apply_feed(
            supplier_id,
            snapshot,
            normalized_feed,
            conflicting_ids,
            automatic=automatic,
            now=datetime.utcnow(),
            started_at=started_at,
        )
    except Exception as error:
        code = "database_busy" if _is_database_busy(error) else "apply_failed"
        try:
            _mark_owned_failure(supplier_id, snapshot["claim_at"], code)
        except Exception as finalize_error:
            logger.warning(
                "Supplier price sync apply failure finalization failed (%s)",
                type(finalize_error).__name__,
            )
        logger.warning("Supplier price sync apply failed (%s)", type(error).__name__)
        return _failure_result(code, started_at, busy=code == "database_busy")


def run_auto_supplier_price_sync_tick(flask_app) -> Optional[Any]:
    """Attempt at most one due active supplier, oldest attempt first."""
    with flask_app.app_context():
        db.session.rollback()
        now = datetime.utcnow()
        candidates = (
            Supplier.query.filter(
                Supplier.is_active.is_(True),
                Supplier.auto_sync_prices.is_(True),
                Supplier.price_file_url.isnot(None),
                Supplier.price_file_url != "",
            )
            .order_by(Supplier.last_price_sync_attempt_at.asc(), Supplier.id.asc())
            .all()
        )
        selected_id = None
        for supplier in candidates:
            if not supplier.price_file_url.strip():
                continue
            last_attempt = supplier.last_price_sync_attempt_at
            if last_attempt is None:
                selected_id = supplier.id
                break
            if _is_due(last_attempt, supplier.auto_sync_interval_minutes, now):
                selected_id = supplier.id
                break
        db.session.rollback()
        if selected_id is None:
            return None
        try:
            return sync_supplier_price_feed(selected_id, automatic=True)
        except Exception as error:
            logger.warning("Automatic supplier price tick failed (%s)", type(error).__name__)
            return _failure_result("tick_failed", time.monotonic())
