# -*- coding: utf-8 -*-
"""Durable read-after-write confirmation for supplier WB enrichment.

WB acknowledges content and media writes before its read model necessarily
contains them.  This runner never retries a provider write.  It only observes
live cards/error-list state and turns a pre-send history receipt into one of:
confirmed success, provider rejection, not-applied, or manual/conflicting drift.
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

from models import (
    BackgroundJob,
    BulkEditHistory,
    CardEditHistory,
    EnrichmentJob,
    Product,
    Seller,
    db,
)
from services.card_rollback import classify_wb_card_history_state
from services.wb_enrichment_merge import (
    MERGE_POLICY_VERSION,
    PHOTO_MATCH_THRESHOLD,
    fingerprint_remote_photo,
    live_wb_photo_legacy_urls,
    live_wb_photo_match_urls,
    live_wb_photo_urls,
    photo_similarity,
)


logger = logging.getLogger(__name__)

RECONCILABLE_STATUSES = frozenset({
    "pending", "submitted", "uncertain", "partial",
})
RETRY_MINUTES = (2, 5, 15, 30, 60, 180)


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def reconciliation_items_per_tick() -> int:
    return _bounded_env_int(
        "WB_ENRICHMENT_RECONCILE_ITEMS_PER_TICK", 10, 1, 50,
    )


def reconciliation_max_attempts() -> int:
    return _bounded_env_int(
        "WB_ENRICHMENT_RECONCILE_MAX_ATTEMPTS", 6, 2, 10,
    )


def schedule_history_reconciliation(
    history: CardEditHistory,
    *,
    status: str = "submitted",
    now: Optional[datetime] = None,
    error: Optional[str] = None,
) -> None:
    """Schedule a persisted pre-send receipt without claiming WB applied it."""
    if not history.changed_fields:
        history.wb_sync_status = "skipped"
        history.wb_synced = False
        history.wb_reconcile_due_at = None
        history.wb_reconciled_at = now or datetime.utcnow()
        history.wb_reconcile_code = "no_change"
        return
    if status not in RECONCILABLE_STATUSES:
        raise ValueError("Unsupported enrichment reconciliation status")
    observed_at = now or datetime.utcnow()
    initial_seconds = _bounded_env_int(
        "WB_ENRICHMENT_RECONCILE_INITIAL_SECONDS", 90, 30, 600,
    )
    history.wb_synced = False
    history.wb_sync_status = status
    history.wb_error_message = str(error)[:1000] if error else None
    history.wb_reconcile_attempts = int(history.wb_reconcile_attempts or 0)
    history.wb_reconcile_due_at = observed_at + timedelta(
        seconds=initial_seconds
    )
    history.wb_reconciled_at = None
    history.wb_reconcile_code = "awaiting_provider"


def _terminal(
    history: CardEditHistory,
    *,
    status: str,
    code: str,
    now: datetime,
    error: Optional[str] = None,
    synced: bool = False,
) -> None:
    history.wb_sync_status = status
    history.wb_synced = bool(synced)
    history.wb_error_message = str(error)[:1000] if error else None
    history.wb_reconcile_code = code[:64]
    history.wb_reconcile_due_at = None
    history.wb_reconciled_at = now


def _reschedule(
    history: CardEditHistory,
    *,
    code: str,
    now: datetime,
    error: Optional[str] = None,
) -> None:
    attempts = int(history.wb_reconcile_attempts or 0)
    if attempts >= reconciliation_max_attempts():
        _terminal(
            history,
            status="failed",
            code="reconcile_exhausted",
            now=now,
            error=error or "WB не подтвердил изменение в срок",
        )
        return
    delay_index = min(max(0, attempts - 1), len(RETRY_MINUTES) - 1)
    history.wb_reconcile_due_at = now + timedelta(
        minutes=RETRY_MINUTES[delay_index]
    )
    history.wb_reconcile_code = code[:64]
    if error:
        history.wb_error_message = str(error)[:1000]


def _mirror_content(product: Product, live_card: Mapping[str, Any], fields) -> None:
    if "title" in fields:
        product.title = live_card.get("title")
    if "description" in fields:
        product.description = live_card.get("description")
    if "brand" in fields:
        product.brand = live_card.get("brand")
    if "characteristics" in fields:
        product.characteristics_json = json.dumps(
            live_card.get("characteristics") or [], ensure_ascii=False,
        )
    if "dimensions" in fields:
        product.dimensions_json = json.dumps(
            live_card.get("dimensions") or {}, ensure_ascii=False,
        )
    product.updated_at = datetime.utcnow()


def _provider_error_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000
        try:
            parsed = datetime.utcfromtimestamp(seconds)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _history_provider_errors(
    raw_errors: Any,
    product: Product,
    history: CardEditHistory,
) -> list[str]:
    """Return only errors temporally and exactly attributable to this receipt.

    WB's error list has no operation ID. Untimestamped or older entries cannot
    safely reject a newer enrichment attempt and are ignored; live comparison
    will still resolve the receipt without any write replay.
    """
    cutoff = (history.created_at or datetime.utcnow()) - timedelta(seconds=10)
    messages = []
    for entry in raw_errors if isinstance(raw_errors, list) else []:
        if not isinstance(entry, Mapping):
            continue
        observed_at = _provider_error_time(
            entry.get("updatedAt") or entry.get("updated_at")
        )
        if observed_at is None or observed_at < cutoff:
            continue
        raw = entry.get("errors")
        if isinstance(raw, Mapping):
            values = raw.get(str(product.vendor_code)) if product.vendor_code else None
            if values:
                messages.extend(str(value) for value in values)
            continue
        nm_value = entry.get("nmID") or entry.get("nmId")
        vendor_value = entry.get("vendorCode") or entry.get("vendor_code")
        identity_matches = False
        try:
            identity_matches = bool(
                nm_value is not None and int(nm_value) == int(product.nm_id)
            )
        except (TypeError, ValueError):
            pass
        if product.vendor_code and vendor_value is not None:
            identity_matches = identity_matches or (
                str(vendor_value) == str(product.vendor_code)
            )
        if identity_matches and isinstance(raw, list):
            messages.extend(str(value) for value in raw)
    return messages[:5]


def _photo_expected_fingerprints(history: CardEditHistory):
    snapshot = history.snapshot_after if isinstance(history.snapshot_after, dict) else {}
    photos = snapshot.get("photos") if isinstance(snapshot, dict) else []
    result = []
    for value in photos if isinstance(photos, list) else []:
        if isinstance(value, str) and value.startswith("enrichment-sha256:"):
            result.append(value.split(":", 1)[1])
    return result


def _photo_plan(history: CardEditHistory) -> Mapping[str, Any]:
    decisions = history.merge_decisions
    if not isinstance(decisions, Mapping):
        return {}
    report = decisions.get("photos")
    return report if isinstance(report, Mapping) else {}


def _mirror_exact_photo_state(
    history: CardEditHistory,
    product: Product,
    live_urls: list[str],
) -> None:
    """Replace planned hash tokens only with provider-observed exact URLs."""
    exact_after = dict(history.snapshot_after or {})
    exact_after["photos"] = list(live_urls)
    exact_after["photos_json"] = json.dumps(live_urls, ensure_ascii=False)
    history.snapshot_after = exact_after
    product.photos_json = exact_after["photos_json"]
    product.updated_at = datetime.utcnow()


def _reconcile_photos(
    history: CardEditHistory,
    product: Product,
    live_card: Mapping[str, Any],
    now: datetime,
) -> None:
    report = _photo_plan(history)
    before_count = report.get("live_count")
    if isinstance(before_count, bool) or not isinstance(before_count, int):
        _terminal(
            history, status="failed", code="invalid_photo_receipt", now=now,
            error="История фото не содержит точный live_count",
        )
        return

    expected_hashes = _photo_expected_fingerprints(history)
    raw_expected_fingerprints = report.get("expected_append_fingerprints")
    expected_by_sha = {
        value.get("pixel_sha"): value
        for value in (
            raw_expected_fingerprints
            if isinstance(raw_expected_fingerprints, list) else []
        )
        if isinstance(value, Mapping) and value.get("pixel_sha")
    }
    if (
        not expected_hashes
        or any(value not in expected_by_sha for value in expected_hashes)
    ):
        _terminal(
            history, status="failed", code="invalid_photo_receipt", now=now,
            error="История фото не содержит fingerprints отправленных файлов",
        )
        return
    try:
        live_urls = live_wb_photo_urls(live_card)
        match_urls = live_wb_photo_match_urls(live_card)
        legacy_urls = live_wb_photo_legacy_urls(live_card)
    except Exception as exc:
        _reschedule(
            history, code="photo_live_unavailable", now=now,
            error=str(exc),
        )
        return

    # Only the original prefix and our exact submitted positions are proof
    # inputs. Photos manually appended after those positions are unrelated
    # live state: preserve their URLs, but never make confirmation depend on
    # downloading/fingerprinting those extra thumbnails.
    before_fingerprints = report.get("live_position_fingerprints")
    if not isinstance(before_fingerprints, list) or len(
        before_fingerprints
    ) != before_count:
        _terminal(
            history, status="failed", code="invalid_photo_receipt", now=now,
            error="История фото не содержит fingerprints исходных позиций",
        )
        return

    if len(live_urls) < before_count:
        _mirror_exact_photo_state(history, product, live_urls)
        _terminal(
            history,
            status="conflict",
            code="live_photo_prefix_shortened",
            now=now,
            error=(
                "Live-галерея WB стала короче исходной; ручное или внешнее "
                "изменение не будет автоматически исправляться"
            ),
        )
        return

    policy = str(report.get("policy") or "")
    prefer_square_for_prefix = (
        report.get("comparison_variant") == "square_preferred"
        or policy == MERGE_POLICY_VERSION
    )
    fingerprint_cache: dict[str, Mapping[str, Any]] = {}

    def position_matches(
        index: int,
        expected: Mapping[str, Any],
        *,
        prefer_square: bool,
    ) -> Optional[bool]:
        if index >= len(live_urls):
            return None
        primary = match_urls[index] if prefer_square else legacy_urls[index]
        fallback = legacy_urls[index] if prefer_square else match_urls[index]
        downloaded = False
        unavailable = False
        for url in dict.fromkeys((primary, fallback)):
            try:
                actual = fingerprint_cache.get(url)
                if actual is None:
                    actual = fingerprint_remote_photo(url)
                    fingerprint_cache[url] = actual
                downloaded = True
            except Exception:
                unavailable = True
                continue
            if photo_similarity(expected, actual) >= PHOTO_MATCH_THRESHOLD:
                return True
        if not downloaded or unavailable:
            return None
        return False

    for index, expected in enumerate(before_fingerprints):
        matched = (
            position_matches(
                index,
                expected,
                prefer_square=prefer_square_for_prefix,
            )
            if isinstance(expected, Mapping) else False
        )
        if matched is None:
            _reschedule(
                history, code="photo_fingerprint_unavailable", now=now,
                error="Не удалось сравнить live-фото WB",
            )
            return
        if not matched:
            _mirror_exact_photo_state(history, product, live_urls)
            _terminal(
                history,
                status="conflict",
                code="live_photo_prefix_changed",
                now=now,
                error=(
                    "Галерея WB была изменена вручную или другим процессом; "
                    "автоматические действия остановлены"
                ),
            )
            return

    matched_expected = 0
    for offset, expected_sha in enumerate(expected_hashes):
        actual_index = before_count + offset
        if actual_index >= len(live_urls):
            if int(history.wb_reconcile_attempts or 0) < (
                reconciliation_max_attempts()
            ):
                _reschedule(
                    history, code="photo_not_visible_yet", now=now,
                    error="WB ещё не показывает все отправленные фото",
                )
                return

            _mirror_exact_photo_state(history, product, live_urls)
            if matched_expected > 0:
                _terminal(
                    history,
                    status="partial",
                    code="photo_partially_confirmed",
                    now=now,
                    error=(
                        f"WB подтвердил {matched_expected} из "
                        f"{len(expected_hashes)} ожидаемых фото; "
                        "автоматический повтор запрещён"
                    ),
                    synced=True,
                )
            else:
                _terminal(
                    history,
                    status="failed",
                    code="photo_not_applied",
                    now=now,
                    error=(
                        "WB не подтвердил ни одного ожидаемого фото; "
                        "автоматический повтор запрещён"
                    ),
                )
            return

        matched = position_matches(
            actual_index,
            expected_by_sha[expected_sha],
            prefer_square=True,
        )
        if matched is None:
            _reschedule(
                history, code="photo_fingerprint_unavailable", now=now,
                error="Не удалось сравнить отправленное фото с live WB",
            )
            return
        if not matched:
            if int(history.wb_reconcile_attempts or 0) < 2:
                _reschedule(
                    history,
                    code="appended_photo_not_confirmed",
                    now=now,
                    error=(
                        "WB пока не подтвердил точный порядок "
                        "дозагруженных фото"
                    ),
                )
            else:
                _mirror_exact_photo_state(history, product, live_urls)
                _terminal(
                    history,
                    status="conflict",
                    code="appended_photo_position_conflict",
                    now=now,
                    error=(
                        "В ожидаемой позиции WB находится другое фото; "
                        "ручное или внешнее изменение сохранено"
                    ),
                )
            return
        matched_expected += 1

    _mirror_exact_photo_state(history, product, live_urls)
    partial = bool((report.get("upload") or {}).get("failed"))
    _terminal(
        history,
        status="partial" if partial else "success",
        code="photo_confirmed",
        now=now,
        error=(history.wb_error_message if partial else None),
        synced=True,
    )


def _reconcile_content(
    history: CardEditHistory,
    product: Product,
    live_card: Mapping[str, Any],
    now: datetime,
    provider_errors: Optional[list[str]] = None,
) -> None:
    try:
        state = classify_wb_card_history_state(
            live_card,
            history.snapshot_before,
            history.snapshot_after,
            history.changed_fields,
        )
    except Exception as exc:
        _terminal(
            history, status="failed", code="invalid_content_receipt", now=now,
            error=str(exc),
        )
        return

    if state == "after":
        _mirror_content(product, live_card, history.changed_fields or [])
        _terminal(
            history, status="success", code="content_confirmed", now=now,
            synced=True,
        )
    elif state in {"before", "unchanged"} and provider_errors:
        _terminal(
            history,
            status="failed",
            code="provider_rejected",
            now=now,
            error="; ".join(provider_errors)[:1000],
        )
    elif state in {"before", "unchanged"}:
        _reschedule(
            history, code="content_not_visible_yet", now=now,
            error="WB ещё не показывает отправленное изменение",
        )
    else:
        # One mixed read can be an intermediate provider state. A second
        # observation is required before declaring a manual/provider conflict.
        if int(history.wb_reconcile_attempts or 0) < 2:
            _reschedule(
                history, code="content_mixed_state", now=now,
                error="WB показывает промежуточное или изменённое состояние",
            )
        else:
            _terminal(
                history,
                status="conflict",
                code="content_conflict",
                now=now,
                error=(
                    "Live-карточка не совпадает ни с состоянием до, ни с "
                    "отправленным состоянием; автоматический повтор запрещён"
                ),
            )


def _refresh_parent_jobs(bulk_edit_ids) -> None:
    for bulk_edit_id in {value for value in bulk_edit_ids if value is not None}:
        histories = CardEditHistory.query.filter_by(
            bulk_edit_id=bulk_edit_id,
        ).all()
        by_product = defaultdict(list)
        for row in histories:
            if row.changed_fields:
                by_product[row.product_id].append(row)
        conflicted_products = {
            product_id
            for product_id, rows in by_product.items()
            if any(
                row.wb_sync_status in {"conflict", "failed"}
                or (
                    row.wb_sync_status == "partial"
                    and row.wb_reconcile_due_at is None
                )
                for row in rows
            )
        }
        pending_products = {
            product_id
            for product_id, rows in by_product.items()
            if any(row.wb_reconcile_due_at is not None for row in rows)
        }
        confirmed_products = {
            product_id
            for product_id, rows in by_product.items()
            if (
                product_id not in conflicted_products
                and product_id not in pending_products
                and rows
                and all(row.wb_synced for row in rows)
            )
        }
        for job in EnrichmentJob.query.filter_by(
            bulk_edit_id=bulk_edit_id,
        ).all():
            job.confirmed = len(confirmed_products)
            job.conflicted = len(conflicted_products)

        bulk = db.session.get(BulkEditHistory, bulk_edit_id)
        if bulk is not None:
            pending = bool(pending_products)
            bulk.wb_synced = bool(histories) and not pending and not (
                conflicted_products
            )

            # Supplier update hub uses the same histories but a BackgroundJob
            # cursor. Its durable result must not keep saying "awaiting" after
            # the shared reconciler has reached terminal states.
            photo_jobs = BackgroundJob.query.filter_by(
                seller_id=bulk.seller_id,
                job_type="supplier_photos_update",
            ).order_by(BackgroundJob.created_at.desc()).limit(50).all()
            for photo_job in photo_jobs:
                progress = photo_job.get_progress() or {}
                if progress.get("bulk_edit_id") != bulk_edit_id:
                    continue
                result = photo_job.get_result() or {}
                result.update({
                    "confirmed": len(confirmed_products),
                    "conflicted": len(conflicted_products),
                    "reconciliation_pending": len(pending_products),
                })
                photo_job.set_result(result)


def process_due_reconciliations(
    *,
    limit: Optional[int] = None,
    now: Optional[datetime] = None,
    client_factory: Optional[Callable[[str], Any]] = None,
) -> dict[str, int]:
    """Process one bounded durable reconciliation batch in an app context."""
    now = now or datetime.utcnow()
    limit = limit or reconciliation_items_per_tick()
    rows = CardEditHistory.query.filter(
        CardEditHistory.wb_sync_status.in_(tuple(RECONCILABLE_STATUSES)),
        CardEditHistory.wb_reconcile_due_at.isnot(None),
        CardEditHistory.wb_reconcile_due_at <= now,
        CardEditHistory.reverted.is_(False),
    ).order_by(
        CardEditHistory.wb_reconcile_due_at.asc(),
        CardEditHistory.id.asc(),
    ).limit(limit).all()
    summary = {"processed": 0, "confirmed": 0, "failed": 0, "pending": 0}
    if not rows:
        return summary

    if client_factory is None:
        from services.wb_api_client import WildberriesAPIClient
        client_factory = WildberriesAPIClient

    rows_by_seller = defaultdict(list)
    for row in rows:
        rows_by_seller[row.seller_id].append(row)
    touched_bulk_ids = set()

    for seller_id, seller_rows in rows_by_seller.items():
        touched_bulk_ids.update(
            history.bulk_edit_id for history in seller_rows
            if history.bulk_edit_id is not None
        )
        seller = db.session.get(Seller, seller_id)
        products = Product.query.filter(
            Product.seller_id == seller_id,
            Product.id.in_([row.product_id for row in seller_rows]),
        ).all()
        products_by_id = {row.id: row for row in products}
        if not seller or not products:
            for history in seller_rows:
                history.wb_reconcile_attempts = int(
                    history.wb_reconcile_attempts or 0
                ) + 1
                _terminal(
                    history, status="failed", code="local_target_missing",
                    now=now, error="Seller или карточка больше не существует",
                )
            continue

        client = None
        attempted_history_ids = set()
        completed_history_ids = set()
        try:
            client = client_factory(seller.wb_api_key)
            nm_ids = [
                int(product.nm_id)
                for product in products
                if product.nm_id
            ]
            cards = client.fetch_cards_by_nm_ids(
                nm_ids, seller_id=seller_id,
            )
            try:
                errors = client.get_cards_error_list(seller_id=seller_id)
            except Exception:
                logger.warning(
                    "WB enrichment reconciliation error-list unavailable",
                    exc_info=True,
                )
                errors = []

            for history in seller_rows:
                attempted_history_ids.add(history.id)
                history.wb_reconcile_attempts = int(
                    history.wb_reconcile_attempts or 0
                ) + 1
                product = products_by_id.get(history.product_id)
                touched_bulk_ids.add(history.bulk_edit_id)
                if product is None or not product.nm_id:
                    _terminal(
                        history, status="failed", code="local_target_missing",
                        now=now, error="Локальная карточка WB больше не существует",
                    )
                    completed_history_ids.add(history.id)
                    continue
                is_photo = set(history.changed_fields or []) == {"photos"}
                messages = (
                    [] if is_photo else
                    _history_provider_errors(errors, product, history)
                )
                live_card = cards.get(int(product.nm_id))
                if not isinstance(live_card, Mapping):
                    if messages:
                        _terminal(
                            history,
                            status="failed",
                            code="provider_rejected",
                            now=now,
                            error="; ".join(messages)[:1000],
                        )
                        completed_history_ids.add(history.id)
                        continue
                    _reschedule(
                        history, code="card_not_visible", now=now,
                        error="Карточка временно не найдена в live-каталоге WB",
                    )
                    completed_history_ids.add(history.id)
                    continue
                if is_photo:
                    _reconcile_photos(history, product, live_card, now)
                else:
                    _reconcile_content(
                        history, product, live_card, now,
                        provider_errors=messages,
                    )
                completed_history_ids.add(history.id)
        except Exception as exc:
            logger.warning(
                "WB enrichment reconciliation seller=%s failed: %s",
                seller_id, exc, exc_info=True,
            )
            for history in seller_rows:
                # Do not turn a receipt already confirmed/terminal (or even
                # normally rescheduled earlier in this batch) back into a
                # contradictory pending row merely because a later sibling
                # raised unexpectedly.
                if history.id in completed_history_ids:
                    continue
                if (
                    history.wb_reconcile_due_at is None
                    or history.wb_sync_status not in RECONCILABLE_STATUSES
                ):
                    continue
                if history.id not in attempted_history_ids:
                    history.wb_reconcile_attempts = int(
                        history.wb_reconcile_attempts or 0
                    ) + 1
                _reschedule(
                    history, code="provider_read_failed", now=now,
                    error="Не удалось прочитать live-состояние WB",
                )
                touched_bulk_ids.add(history.bulk_edit_id)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    _refresh_parent_jobs(touched_bulk_ids)
    db.session.commit()
    for history in rows:
        summary["processed"] += 1
        if history.wb_synced:
            summary["confirmed"] += 1
        elif history.wb_reconcile_due_at is not None:
            summary["pending"] += 1
        else:
            summary["failed"] += 1
    return summary
