"""Durable lifecycle for explicit administrator supplier catalog refreshes.

The request path only admits one supplier-scoped receipt.  A worker claims it
under the same per-supplier file lock used by the synchronous catalog path,
then keeps that lock through raw catalog commit, local postprocessing, price
follow-up, and the terminal receipt.  A process restart never replays a row
that was already running: the next lock owner records it as interrupted.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import ipaddress
import json
import logging
import re
import time
import uuid
from typing import Any, Optional

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError

from models import AdminAuditLog, Supplier, User, db
from services.supplier_catalog_refresh_models import SupplierCatalogRefreshRun
from services.supplier_catalog_schedule_models import (
    SupplierCatalogSchedule,
    SupplierCatalogScheduledRun,
)


logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("pending", "running")
MAX_CANDIDATES_PER_TICK = 100
MAX_RECOVERIES_PER_TICK = 20
MAX_SCHEDULE_CANDIDATES_PER_TICK = 100
MIN_CATALOG_SCHEDULE_INTERVAL_HOURS = 6
MAX_CATALOG_SCHEDULE_INTERVAL_HOURS = 168
ALLOWED_PRICE_STATUSES = frozenset({
    "success", "failed", "partial", "skipped", "not_configured",
    "busy", "source_changed", "unknown",
})
SAFE_ERROR_CODES = frozenset({
    "admin_not_authorized", "supplier_not_found", "configuration_unavailable",
    "configuration_changed", "worker_interrupted", "catalog_sync_failed",
    "catalog_sync_partial", "catalog_config_check_failed",
    "catalog_fetch_failed", "catalog_validation_failed", "catalog_empty",
    "catalog_apply_failed", "raw_checkpoint_failed", "source_changed",
    "postprocess_partial", "postprocess_failed", "price_sync_failed",
    "price_sync_partial",
    "price_source_changed", "price_sync_deferred", "price_sync_unknown",
    "scheduled_consent_revoked", "catalog_pre_fetch_check_failed",
    "claim_lost",
})
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class SupplierCatalogRefreshError(ValueError):
    """A bounded, safe error code for admission and scope failures."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _positive_id(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 < value <= (1 << 63) - 1
    )


def _valid_run_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.fullmatch(value))


def _utc_now() -> datetime:
    return datetime.utcnow()


def _active_query(supplier_id: int):
    return SupplierCatalogRefreshRun.query.filter(
        SupplierCatalogRefreshRun.supplier_id == supplier_id,
        SupplierCatalogRefreshRun.status.in_(ACTIVE_STATUSES),
    )


def _latest_query(supplier_id: int):
    return SupplierCatalogRefreshRun.query.filter_by(
        supplier_id=supplier_id,
    ).order_by(
        SupplierCatalogRefreshRun.created_at.desc(),
        SupplierCatalogRefreshRun.id.desc(),
    )


def _serialize(run: SupplierCatalogRefreshRun) -> dict:
    def nonnegative(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    with db.session.no_autoflush:
        scheduled_run = SupplierCatalogScheduledRun.query.filter_by(
            run_id=run.id,
        ).first()
    price_status = run.price_status
    if price_status not in ALLOWED_PRICE_STATUSES:
        price_status = None
    return {
        "id": run.id,
        "status": run.status,
        "created_at": _timestamp(run.created_at),
        "started_at": _timestamp(run.started_at),
        "raw_committed_at": _timestamp(run.raw_committed_at),
        "completed_at": _timestamp(run.completed_at),
        "trigger_kind": "scheduled" if scheduled_run is not None else "manual",
        "due_at": _timestamp(scheduled_run.due_at) if scheduled_run is not None else None,
        "total": nonnegative(run.total),
        "added": nonnegative(run.added),
        "updated": nonnegative(run.updated),
        "postprocess_errors": nonnegative(run.postprocess_errors),
        "price_status": price_status,
        "price_conflicts": nonnegative(run.price_conflicts),
        "error_code": (
            run.error_code
            if isinstance(run.error_code, str)
            and run.error_code in SAFE_ERROR_CODES else None
        ),
    }


def _ensure_clean_session() -> None:
    if db.session.new or db.session.dirty or db.session.deleted:
        raise SupplierCatalogRefreshError("session_not_clean")


def _begin_writer_transaction() -> None:
    """Start a short SQLite writer transaction without absorbing caller work."""
    _ensure_clean_session()
    if db.engine.dialect.name != "sqlite":
        raise SupplierCatalogRefreshError("sqlite_required")
    try:
        connection = db.session.connection()
        raw = connection.connection.driver_connection
        if getattr(raw, "in_transaction", False):
            raise SupplierCatalogRefreshError("transaction_not_fresh")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    except SupplierCatalogRefreshError:
        db.session.rollback()
        raise
    except Exception as exc:
        db.session.rollback()
        if _is_database_busy(exc):
            raise SupplierCatalogRefreshError("database_busy") from None
        raise SupplierCatalogRefreshError("database_unavailable") from None


def _is_database_busy(error: BaseException) -> bool:
    message = str(error).casefold()
    return "database is locked" in message or "database is busy" in message


def _admin_allowed(admin_user_id: int) -> bool:
    user = User.query.filter_by(id=admin_user_id).populate_existing().first()
    return bool(
        user
        and user.is_active
        and user.is_admin
        and not user.is_blocked()
    )


def _config_digest(supplier: Supplier, *, catalog_only: bool = False) -> str:
    from services.supplier_service import SupplierService

    try:
        if catalog_only:
            digest = SupplierService.supplier_catalog_config_digest(
                supplier, catalog_only=True,
            )
        else:
            digest = SupplierService.supplier_catalog_config_digest(supplier)
    except Exception:
        raise SupplierCatalogRefreshError("configuration_unavailable") from None
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SupplierCatalogRefreshError("configuration_unavailable")
    return digest


def _timestamp(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


def _valid_schedule_revision(value: Any) -> bool:
    return _positive_id(value)


def _serialize_schedule(schedule: SupplierCatalogSchedule) -> dict:
    return {
        "supplier_id": schedule.supplier_id,
        "enabled": bool(schedule.enabled),
        "interval_hours": schedule.interval_hours,
        "next_due_at": _timestamp(schedule.next_due_at),
        "authorized_by_user_id": schedule.authorized_by_user_id,
        "revision": schedule.revision,
        "updated_at": _timestamp(schedule.updated_at),
    }


def _valid_supplier_catalog_source(supplier: Optional[Supplier]) -> bool:
    if (
        supplier is None
        or not bool(supplier.is_active)
        or not isinstance(supplier.csv_source_url, str)
        or not supplier.csv_source_url.strip()
    ):
        return False
    try:
        # This validates source URL syntax and policy without resolving DNS or
        # making a provider request. The feed path performs the same check.
        from services.supplier_price_feed import _prepare_url

        _prepare_url(supplier.csv_source_url)
    except Exception:
        return False
    return True


def get_catalog_schedule(supplier_id: int) -> Optional[dict]:
    """Read a supplier's opt-in schedule without writing or creating state."""
    if not _positive_id(supplier_id):
        raise SupplierCatalogRefreshError("invalid_supplier_id")
    with db.session.no_autoflush:
        schedule = SupplierCatalogSchedule.query.filter_by(
            supplier_id=supplier_id,
        ).first()
    return _serialize_schedule(schedule) if schedule is not None else None


def set_catalog_schedule(
    *,
    supplier_id: int,
    admin_user_id: int,
    enabled: bool,
    interval_hours: int,
    expected_revision: Optional[int],
    ip_address: Optional[str] = None,
) -> dict:
    """Set an explicit supplier schedule using a revision CAS.

    A first opt-in is immediately due. Every consent or setting change bumps
    the generation, while an exact no-op preserves its due time and revision.
    """
    if not _positive_id(supplier_id):
        raise SupplierCatalogRefreshError("invalid_supplier_id")
    if not _positive_id(admin_user_id):
        raise SupplierCatalogRefreshError("invalid_admin_id")
    if type(enabled) is not bool:
        raise SupplierCatalogRefreshError("invalid_enabled")
    if (
        isinstance(interval_hours, bool)
        or not isinstance(interval_hours, int)
        or not MIN_CATALOG_SCHEDULE_INTERVAL_HOURS
        <= interval_hours
        <= MAX_CATALOG_SCHEDULE_INTERVAL_HOURS
    ):
        raise SupplierCatalogRefreshError("invalid_interval_hours")
    if expected_revision is not None and not _valid_schedule_revision(expected_revision):
        raise SupplierCatalogRefreshError("invalid_expected_revision")

    _begin_writer_transaction()
    try:
        db.session.expire_all()
        if not _admin_allowed(admin_user_id):
            db.session.rollback()
            raise SupplierCatalogRefreshError("admin_not_authorized")
        supplier = db.session.get(Supplier, supplier_id)
        if supplier is None:
            db.session.rollback()
            raise SupplierCatalogRefreshError("supplier_not_found")
        if enabled and not _valid_supplier_catalog_source(supplier):
            db.session.rollback()
            raise SupplierCatalogRefreshError("schedule_unavailable")
        schedule = SupplierCatalogSchedule.query.filter_by(
            supplier_id=supplier_id,
        ).populate_existing().first()
        if (
            (schedule is None and expected_revision is not None)
            or (
                schedule is not None
                and expected_revision != schedule.revision
            )
        ):
            db.session.rollback()
            raise SupplierCatalogRefreshError("schedule_conflict")

        now = _utc_now()
        if schedule is None:
            schedule = SupplierCatalogSchedule(
                supplier_id=supplier_id,
                enabled=enabled,
                interval_hours=interval_hours,
                next_due_at=now,
                authorized_by_user_id=admin_user_id,
                revision=1,
                updated_at=now,
            )
            db.session.add(schedule)
        else:
            changed = (
                bool(schedule.enabled) != enabled
                or schedule.interval_hours != interval_hours
                or schedule.authorized_by_user_id != admin_user_id
            )
            if not changed:
                result = _serialize_schedule(schedule)
                db.session.rollback()
                return result
            if not _valid_schedule_revision(schedule.revision):
                db.session.rollback()
                raise SupplierCatalogRefreshError("schedule_unavailable")
            schedule.enabled = enabled
            schedule.interval_hours = interval_hours
            schedule.authorized_by_user_id = admin_user_id
            schedule.revision += 1
            # Turning consent on, changing its cadence, or transferring it to
            # another admin makes the revised schedule due immediately. A
            # disable preserves its prior due time and active queue receipt.
            if enabled:
                schedule.next_due_at = now
            schedule.updated_at = now

        db.session.add(AdminAuditLog(
            admin_user_id=admin_user_id,
            action="set_supplier_catalog_schedule",
            target_type="supplier",
            target_id=supplier_id,
            details=json.dumps({
                "enabled": enabled,
                "interval_hours": interval_hours,
                "revision": schedule.revision,
            }, separators=(",", ":")),
            ip_address=_bounded_ip(ip_address),
            created_at=now,
        ))
        db.session.commit()
        return _serialize_schedule(schedule)
    except SupplierCatalogRefreshError:
        if db.session.is_active:
            db.session.rollback()
        raise
    except Exception as exc:
        db.session.rollback()
        if _is_database_busy(exc):
            raise SupplierCatalogRefreshError("database_busy") from None
        raise SupplierCatalogRefreshError("schedule_unavailable") from None


def _bounded_ip(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value or len(value) > 45:
        return None
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        return None


def enqueue_catalog_refresh(
    *,
    supplier_id: int,
    admin_user_id: int,
    expected_latest_id: Optional[str],
    ip_address: Optional[str] = None,
) -> tuple[dict, bool]:
    """Admit one explicit refresh or return the receipt the form observed.

    ``expected_latest_id`` is the optimistic token rendered by the supplier
    admin page. Replaying an old form after completion returns its original
    latest receipt instead of silently starting another refresh.
    """
    if not _positive_id(supplier_id):
        raise SupplierCatalogRefreshError("invalid_supplier_id")
    if not _positive_id(admin_user_id):
        raise SupplierCatalogRefreshError("invalid_admin_id")
    if expected_latest_id is not None and not _valid_run_id(expected_latest_id):
        raise SupplierCatalogRefreshError("invalid_expected_run_id")

    _begin_writer_transaction()
    try:
        db.session.expire_all()
        if not _admin_allowed(admin_user_id):
            db.session.rollback()
            raise SupplierCatalogRefreshError("admin_not_authorized")
        supplier = db.session.get(Supplier, supplier_id)
        if supplier is None:
            db.session.rollback()
            raise SupplierCatalogRefreshError("supplier_not_found")

        active = _active_query(supplier_id).order_by(
            SupplierCatalogRefreshRun.created_at.desc(),
            SupplierCatalogRefreshRun.id.desc(),
        ).first()
        latest = _latest_query(supplier_id).first()
        if active is not None:
            db.session.rollback()
            return _serialize(active), False
        if latest is not None and latest.id != expected_latest_id:
            db.session.rollback()
            return _serialize(latest), False
        if latest is None and expected_latest_id is not None:
            db.session.rollback()
            raise SupplierCatalogRefreshError("run_not_found")

        now = _utc_now()
        if latest is not None and now <= latest.created_at:
            now = latest.created_at + timedelta(microseconds=1)
        run = SupplierCatalogRefreshRun(
            id=str(uuid.uuid4()),
            supplier_id=supplier_id,
            admin_user_id=admin_user_id,
            status="pending",
            config_digest=_config_digest(supplier),
            created_at=now,
        )
        db.session.add(run)
        db.session.add(AdminAuditLog(
            admin_user_id=admin_user_id,
            action="queue_supplier_catalog_refresh",
            target_type="supplier",
            target_id=supplier_id,
            details=json.dumps({"run_id": run.id}, separators=(",", ":")),
            ip_address=_bounded_ip(ip_address),
            created_at=now,
        ))
        db.session.commit()
        return _serialize(run), True
    except SupplierCatalogRefreshError:
        if db.session.is_active:
            db.session.rollback()
        raise
    except IntegrityError:
        db.session.rollback()
        # A unique active-supplier constraint is a final backstop. Return the
        # exact receipt that won admission instead of creating a second job.
        active = _active_query(supplier_id).order_by(
            SupplierCatalogRefreshRun.created_at.desc(),
            SupplierCatalogRefreshRun.id.desc(),
        ).first()
        if active is not None:
            return _serialize(active), False
        raise SupplierCatalogRefreshError("queue_conflict") from None
    except Exception as exc:
        db.session.rollback()
        if _is_database_busy(exc):
            raise SupplierCatalogRefreshError("database_busy") from None
        raise SupplierCatalogRefreshError("queue_unavailable") from None


def get_catalog_refresh(
    supplier_id: int,
    run_id: Optional[str] = None,
) -> Optional[dict]:
    """Read one supplier-scoped receipt without changing job state."""
    if not _positive_id(supplier_id):
        raise SupplierCatalogRefreshError("invalid_supplier_id")
    if run_id is not None and not _valid_run_id(run_id):
        raise SupplierCatalogRefreshError("invalid_run_id")
    with db.session.no_autoflush:
        query = SupplierCatalogRefreshRun.query.filter_by(supplier_id=supplier_id)
        if run_id is not None:
            run = query.filter_by(id=run_id).first()
        else:
            run = _latest_query(supplier_id).first()
    return _serialize(run) if run is not None else None


def _finish_interrupted(run_id: str, supplier_id: int) -> bool:
    """Mark a running receipt abandoned after acquiring its exact flock."""
    _begin_writer_transaction()
    db.session.expire_all()
    run = SupplierCatalogRefreshRun.query.filter_by(
        id=run_id,
        supplier_id=supplier_id,
        status="running",
    ).first()
    if run is None:
        db.session.rollback()
        return False
    run.status = "interrupted"
    run.completed_at = _utc_now()
    run.error_code = "worker_interrupted"
    # Keep raw commit time/counts and any finished follow-up facts intact.
    db.session.commit()
    return True


def _claim_pending(run_id: str, supplier_id: int) -> Optional[str]:
    """Claim one pending receipt, rechecking actor and source under the lock."""
    _begin_writer_transaction()
    db.session.expire_all()
    run = SupplierCatalogRefreshRun.query.filter_by(
        id=run_id,
        supplier_id=supplier_id,
        status="pending",
    ).first()
    if run is None:
        db.session.rollback()
        return None
    if not _admin_allowed(run.admin_user_id):
        run.status = "failed"
        run.completed_at = _utc_now()
        run.error_code = "admin_not_authorized"
        db.session.commit()
        return "failed"
    supplier = db.session.get(Supplier, supplier_id)
    if supplier is None:
        run.status = "failed"
        run.completed_at = _utc_now()
        run.error_code = "supplier_not_found"
        db.session.commit()
        return "failed"
    try:
        current_digest = _config_digest(supplier)
    except SupplierCatalogRefreshError:
        run.status = "failed"
        run.completed_at = _utc_now()
        run.error_code = "configuration_unavailable"
        db.session.commit()
        return "failed"
    if current_digest != run.config_digest:
        run.status = "failed"
        run.completed_at = _utc_now()
        run.error_code = "configuration_changed"
        db.session.commit()
        return "failed"

    run.status = "running"
    run.started_at = _utc_now()
    run.completed_at = None
    run.error_code = None
    db.session.commit()
    return run.config_digest


def _scheduled_consent_digest(
    run: SupplierCatalogRefreshRun,
    *,
    expected_digest: Optional[str] = None,
) -> str:
    """Recheck the current opt-in generation and catalog-only source state."""
    extension = SupplierCatalogScheduledRun.query.filter_by(
        run_id=run.id,
    ).populate_existing().first()
    schedule = SupplierCatalogSchedule.query.filter_by(
        supplier_id=run.supplier_id,
    ).populate_existing().first()
    if (
        extension is None
        or schedule is None
        or not bool(schedule.enabled)
        or extension.schedule_revision != schedule.revision
        or schedule.authorized_by_user_id != run.admin_user_id
    ):
        raise SupplierCatalogRefreshError("scheduled_consent_revoked")
    if not _admin_allowed(run.admin_user_id):
        raise SupplierCatalogRefreshError("admin_not_authorized")
    supplier = Supplier.query.filter_by(
        id=run.supplier_id,
    ).populate_existing().first()
    if not _valid_supplier_catalog_source(supplier):
        raise SupplierCatalogRefreshError("configuration_changed")
    digest = _config_digest(supplier, catalog_only=True)
    if expected_digest is not None and digest != expected_digest:
        raise SupplierCatalogRefreshError("configuration_changed")
    return digest


def _claim_scheduled_pending(run_id: str, supplier_id: int) -> Optional[str]:
    """Claim scheduled work only while its exact admin consent generation holds."""
    _begin_writer_transaction()
    db.session.expire_all()
    run = SupplierCatalogRefreshRun.query.filter_by(
        id=run_id,
        supplier_id=supplier_id,
        status="pending",
    ).populate_existing().first()
    if run is None:
        db.session.rollback()
        return None
    try:
        digest = _scheduled_consent_digest(run, expected_digest=run.config_digest)
    except SupplierCatalogRefreshError as exc:
        run.status = "failed"
        run.completed_at = _utc_now()
        run.error_code = (
            exc.code if exc.code in SAFE_ERROR_CODES else "configuration_unavailable"
        )
        db.session.commit()
        return "failed"

    run.status = "running"
    run.started_at = _utc_now()
    run.completed_at = None
    run.error_code = None
    db.session.commit()
    return digest


def _safe_count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _price_status(result: Any) -> Optional[str]:
    value = getattr(result, "price_followup_status", None)
    if value in ALLOWED_PRICE_STATUSES:
        return value
    return None


def _raw_checkpoint_callback(
    run_id: str,
    supplier_id: int,
    config_digest: str,
    *,
    scheduled: bool = False,
):
    """Build the callback that must share the raw catalog SQL transaction."""
    def checkpoint(result: Any, raw_at: datetime) -> None:
        if not isinstance(raw_at, datetime):
            raise SupplierCatalogRefreshError("invalid_raw_checkpoint")
        run = SupplierCatalogRefreshRun.query.filter_by(
            id=run_id,
            supplier_id=supplier_id,
            status="running",
            config_digest=config_digest,
        ).populate_existing().first()
        if run is None:
            raise SupplierCatalogRefreshError("claim_lost")
        if scheduled:
            _scheduled_consent_digest(run, expected_digest=config_digest)
        else:
            if not _admin_allowed(run.admin_user_id):
                raise SupplierCatalogRefreshError("admin_not_authorized")
            supplier = Supplier.query.filter_by(
                id=supplier_id,
            ).populate_existing().first()
            if supplier is None or _config_digest(supplier) != config_digest:
                raise SupplierCatalogRefreshError("configuration_changed")

        total = _safe_count(getattr(result, "total_in_csv", None))
        added = _safe_count(getattr(result, "added", None))
        updated = _safe_count(getattr(result, "updated", None))
        if total is None or added is None or updated is None:
            raise SupplierCatalogRefreshError("invalid_raw_checkpoint")
        run.raw_committed_at = raw_at
        run.total = total
        run.added = added
        run.updated = updated

    return checkpoint


def _scheduled_pre_fetch_check(
    run_id: str,
    supplier_id: int,
    config_digest: str,
):
    """Return the zero-argument gate the CSV service calls before HTTP."""
    def check() -> None:
        run = SupplierCatalogRefreshRun.query.filter_by(
            id=run_id,
            supplier_id=supplier_id,
            status="running",
            config_digest=config_digest,
        ).populate_existing().first()
        if run is None:
            raise SupplierCatalogRefreshError("claim_lost")
        _scheduled_consent_digest(run, expected_digest=config_digest)

    return check


def _terminal_values(result: Any, run: SupplierCatalogRefreshRun) -> tuple[str, Optional[str]]:
    raw_committed = isinstance(run.raw_committed_at, datetime)
    if not raw_committed:
        code = getattr(result, "catalog_error_code", None)
        if code in SAFE_ERROR_CODES:
            return "failed", code
        return "failed", "catalog_sync_failed"

    errors = _safe_count(getattr(result, "errors", None))
    errors = errors if errors is not None else 1
    price_status = _price_status(result)
    if not bool(getattr(result, "success", False)):
        return "partial", "catalog_sync_partial"
    if errors > 0:
        return "partial", "postprocess_partial"
    if price_status == "partial":
        return "partial", "price_sync_partial"
    if price_status == "failed":
        return "partial", "price_sync_failed"
    if price_status == "source_changed":
        return "partial", "price_source_changed"
    if price_status == "busy":
        return "partial", "price_sync_deferred"
    if price_status == "unknown":
        return "partial", "price_sync_unknown"
    return "completed", None


def _finish_result(run_id: str, supplier_id: int, result: Any) -> bool:
    _begin_writer_transaction()
    db.session.expire_all()
    run = SupplierCatalogRefreshRun.query.filter_by(
        id=run_id,
        supplier_id=supplier_id,
        status="running",
    ).first()
    if run is None:
        db.session.rollback()
        return False
    status, error_code = _terminal_values(result, run)
    errors = _safe_count(getattr(result, "errors", None))
    if run.raw_committed_at is not None:
        run.postprocess_errors = errors if errors is not None else 1
    scheduled = SupplierCatalogScheduledRun.query.filter_by(
        run_id=run_id,
    ).first() is not None
    run.price_status = "skipped" if scheduled else _price_status(result)
    run.price_conflicts = None if scheduled else _safe_count(
        getattr(result, "price_followup_conflicts", None)
    )
    run.status = status
    run.error_code = error_code
    run.completed_at = _utc_now()
    db.session.commit()
    return True


def _finish_exception(run_id: str, supplier_id: int) -> bool:
    """Close a handled failure without storing exception text or response data."""
    _begin_writer_transaction()
    db.session.expire_all()
    run = SupplierCatalogRefreshRun.query.filter_by(
        id=run_id,
        supplier_id=supplier_id,
        status="running",
    ).first()
    if run is None:
        db.session.rollback()
        return False
    if run.raw_committed_at is None:
        run.status = "failed"
        run.error_code = "catalog_sync_failed"
    else:
        run.status = "partial"
        run.error_code = "postprocess_failed"
        if run.postprocess_errors is None:
            run.postprocess_errors = 1
    run.completed_at = _utc_now()
    db.session.commit()
    return True


def _candidate_ids(
    status: str,
    *,
    scheduled: Optional[bool] = None,
) -> list[tuple[str, int, str]]:
    """Scan bounded, well-formed receipts, including rows orphaned by deletion."""
    query = db.session.query(
        SupplierCatalogRefreshRun.id,
        SupplierCatalogRefreshRun.supplier_id,
        SupplierCatalogRefreshRun.status,
        SupplierCatalogRefreshRun.config_digest,
    ).outerjoin(
        SupplierCatalogScheduledRun,
        SupplierCatalogScheduledRun.run_id == SupplierCatalogRefreshRun.id,
    ).filter(
        SupplierCatalogRefreshRun.status == status,
    )
    if scheduled is True:
        query = query.filter(SupplierCatalogScheduledRun.run_id.isnot(None))
    elif scheduled is False:
        query = query.filter(SupplierCatalogScheduledRun.run_id.is_(None))
    rows = query.order_by(
        SupplierCatalogRefreshRun.created_at.asc(),
        SupplierCatalogRefreshRun.id.asc(),
    ).limit(MAX_CANDIDATES_PER_TICK).all()
    return [
        (run_id, supplier_id, observed_status)
        for run_id, supplier_id, observed_status, digest in rows
        if (
            _valid_run_id(run_id)
            and _positive_id(supplier_id)
            and observed_status == status
            and isinstance(digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", digest)
        )
    ]


def _process_claimed_run(
    run_id: str,
    supplier_id: int,
    digest: str,
    *,
    scheduled: bool = False,
) -> None:
    from services.supplier_service import SupplierService

    callback = _raw_checkpoint_callback(
        run_id, supplier_id, digest, scheduled=scheduled,
    )
    try:
        if scheduled:
            result = SupplierService._sync_from_csv_locked(
                supplier_id,
                start_time=time.time(),
                expected_config_digest=digest,
                raw_checkpoint=callback,
                catalog_only=True,
                pre_fetch_check=_scheduled_pre_fetch_check(
                    run_id, supplier_id, digest,
                ),
            )
        else:
            result = SupplierService._sync_from_csv_locked(
                supplier_id,
                start_time=time.time(),
                expected_config_digest=digest,
                raw_checkpoint=callback,
            )
    except Exception as exc:
        db.session.rollback()
        logger.warning(
            "Supplier catalog refresh %s failed in worker (%s)",
            run_id,
            type(exc).__name__,
        )
        try:
            _finish_exception(run_id, supplier_id)
        except Exception as finish_exc:
            db.session.rollback()
            logger.warning(
                "Could not persist supplier catalog refresh failure %s (%s)",
                run_id,
                type(finish_exc).__name__,
            )
        return

    try:
        _finish_result(run_id, supplier_id, result)
    except Exception as exc:
        db.session.rollback()
        logger.warning(
            "Could not persist supplier catalog refresh result %s (%s)",
            run_id,
            type(exc).__name__,
        )


def _due_schedule_candidates(now: datetime) -> list[tuple[int, int]]:
    """Return a bounded due window, excluding revoked actors in SQL itself."""
    rows = db.session.query(
        SupplierCatalogSchedule.supplier_id,
        SupplierCatalogSchedule.revision,
    ).join(
        Supplier,
        Supplier.id == SupplierCatalogSchedule.supplier_id,
    ).join(
        User,
        User.id == SupplierCatalogSchedule.authorized_by_user_id,
    ).filter(
        SupplierCatalogSchedule.enabled.is_(True),
        SupplierCatalogSchedule.next_due_at <= now,
        Supplier.is_active.is_(True),
        Supplier.csv_source_url.isnot(None),
        func.trim(Supplier.csv_source_url) != "",
        User.is_active.is_(True),
        User.is_admin.is_(True),
        User.blocked_at.is_(None),
    ).order_by(
        SupplierCatalogSchedule.next_due_at.asc(),
        SupplierCatalogSchedule.supplier_id.asc(),
    ).limit(MAX_SCHEDULE_CANDIDATES_PER_TICK).all()
    return [
        (supplier_id, revision)
        for supplier_id, revision in rows
        if _positive_id(supplier_id) and _valid_schedule_revision(revision)
    ]


def _admit_due_schedule(
    supplier_id: int,
    observed_revision: int,
) -> Optional[tuple[str, datetime]]:
    """Atomically create one scheduled queue receipt and advance its cadence."""
    _begin_writer_transaction()
    try:
        db.session.expire_all()
        schedule = SupplierCatalogSchedule.query.filter_by(
            supplier_id=supplier_id,
        ).populate_existing().first()
        now = _utc_now()
        if (
            schedule is None
            or not bool(schedule.enabled)
            or schedule.revision != observed_revision
            or not _valid_schedule_revision(schedule.revision)
            or not isinstance(schedule.next_due_at, datetime)
            or schedule.next_due_at > now
            or isinstance(schedule.interval_hours, bool)
            or not isinstance(schedule.interval_hours, int)
            or not MIN_CATALOG_SCHEDULE_INTERVAL_HOURS
            <= schedule.interval_hours
            <= MAX_CATALOG_SCHEDULE_INTERVAL_HOURS
        ):
            db.session.rollback()
            return None
        if not _admin_allowed(schedule.authorized_by_user_id):
            db.session.rollback()
            return None
        supplier = db.session.get(Supplier, supplier_id)
        if not _valid_supplier_catalog_source(supplier):
            db.session.rollback()
            return None
        if _active_query(supplier_id).first() is not None:
            db.session.rollback()
            return None

        try:
            digest = _config_digest(supplier, catalog_only=True)
        except SupplierCatalogRefreshError:
            db.session.rollback()
            return None

        due_at = schedule.next_due_at
        run_id = str(uuid.uuid4())
        run = SupplierCatalogRefreshRun(
            id=run_id,
            supplier_id=supplier_id,
            admin_user_id=schedule.authorized_by_user_id,
            status="pending",
            config_digest=digest,
            created_at=now,
        )
        schedule_run = SupplierCatalogScheduledRun(
            run_id=run_id,
            schedule_revision=schedule.revision,
            due_at=due_at,
        )
        db.session.add(run)
        # The scheduled extension has an immediate FK to the legacy receipt.
        # Flush its parent explicitly instead of depending on ORM statement
        # ordering or a connection with SQLite foreign keys disabled.
        db.session.flush()
        db.session.add(schedule_run)
        # Missed intervals are coalesced into one current run. Never queue a
        # burst of catch-up receipts after the application was offline.
        schedule.next_due_at = now + timedelta(hours=schedule.interval_hours)
        schedule.updated_at = now
        db.session.commit()
        return run_id, due_at
    except IntegrityError:
        db.session.rollback()
        return None
    except Exception:
        db.session.rollback()
        raise


def _claim_and_process_scheduled(
    run_id: str,
    supplier_id: int,
    summary: dict,
) -> bool:
    digest = _claim_scheduled_pending(run_id, supplier_id)
    if digest == "failed":
        summary["failed"] += 1
        summary["scheduled"] += 1
        return True
    if digest is None:
        return False
    summary["processed"] += 1
    summary["scheduled"] += 1
    _process_claimed_run(run_id, supplier_id, digest, scheduled=True)
    return True


def _run_one_scheduled(
    summary: dict,
    supplier_lock,
) -> bool:
    """Process an already-admitted scheduled row or admit one due opt-in."""
    pending_ids = _candidate_ids("pending", scheduled=True)
    db.session.rollback()
    for run_id, supplier_id, _observed_status in pending_ids:
        with supplier_lock(supplier_id) as acquired:
            if not acquired:
                summary["busy"] += 1
                continue
            try:
                if _claim_and_process_scheduled(run_id, supplier_id, summary):
                    return True
            except Exception as exc:
                db.session.rollback()
                summary["failed"] += 1
                logger.warning(
                    "Scheduled supplier catalog refresh claim failed %s (%s)",
                    run_id,
                    type(exc).__name__,
                )
                try:
                    _finish_exception(run_id, supplier_id)
                except Exception:
                    db.session.rollback()
                return True
            finally:
                db.session.remove()

    # No pending scheduled receipt remains. Admit at most one new due opt-in.
    due_candidates = _due_schedule_candidates(_utc_now())
    db.session.rollback()
    for supplier_id, revision in due_candidates:
        with supplier_lock(supplier_id) as acquired:
            if not acquired:
                summary["busy"] += 1
                continue
            try:
                admitted = _admit_due_schedule(supplier_id, revision)
                if admitted is None:
                    continue
                run_id, _due_at = admitted
                return _claim_and_process_scheduled(run_id, supplier_id, summary)
            except Exception as exc:
                db.session.rollback()
                summary["failed"] += 1
                logger.warning(
                    "Scheduled supplier catalog refresh admission deferred (%s)",
                    type(exc).__name__,
                )
                return True
            finally:
                db.session.remove()
    return False


def run_catalog_refresh_tick(flask_app) -> dict:
    """Process at most one new run and reconcile abandoned runs without replay."""
    from services.supplier_service import _supplier_catalog_sync_lock

    summary = {
        "processed": 0,
        "interrupted": 0,
        "busy": 0,
        "failed": 0,
        "scheduled": 0,
    }
    with flask_app.app_context():
        try:
            _ensure_clean_session()
            # Separate scans ensure a set of older, currently locked RUNNING
            # rows cannot hide newer PENDING work behind the bounded window.
            running_ids = _candidate_ids("running")
            pending_ids = _candidate_ids("pending", scheduled=False)
            db.session.rollback()

            recoveries = 0
            for run_id, supplier_id, _observed_status in running_ids:
                if recoveries >= MAX_RECOVERIES_PER_TICK:
                    break
                with _supplier_catalog_sync_lock(supplier_id) as acquired:
                    if not acquired:
                        summary["busy"] += 1
                        continue
                    try:
                        if _finish_interrupted(run_id, supplier_id):
                            summary["interrupted"] += 1
                            recoveries += 1
                    except Exception as exc:
                        db.session.rollback()
                        summary["failed"] += 1
                        logger.warning(
                            "Supplier catalog refresh recovery deferred %s (%s)",
                            run_id,
                            type(exc).__name__,
                        )
                    finally:
                        db.session.remove()

            claimed_new = False
            manual_pending_blocked = False
            for run_id, supplier_id, _observed_status in pending_ids:
                if claimed_new:
                    break
                with _supplier_catalog_sync_lock(supplier_id) as acquired:
                    if not acquired:
                        summary["busy"] += 1
                        manual_pending_blocked = True
                        continue
                    digest = None
                    try:
                        digest = _claim_pending(run_id, supplier_id)
                        if digest == "failed":
                            summary["failed"] += 1
                            claimed_new = True
                            break
                        if digest is None:
                            continue
                        claimed_new = True
                        summary["processed"] += 1
                        _process_claimed_run(run_id, supplier_id, digest)
                    except Exception as exc:
                        db.session.rollback()
                        summary["failed"] += 1
                        claimed_new = True
                        logger.warning(
                            "Supplier catalog refresh claim failed %s (%s)",
                            run_id,
                            type(exc).__name__,
                        )
                        if digest not in (None, "failed"):
                            try:
                                _finish_exception(run_id, supplier_id)
                            except Exception:
                                db.session.rollback()
                    finally:
                        db.session.remove()
            if not claimed_new and not manual_pending_blocked:
                _run_one_scheduled(summary, _supplier_catalog_sync_lock)
            return summary
        except SupplierCatalogRefreshError as exc:
            db.session.rollback()
            logger.warning("Supplier catalog refresh tick unavailable (%s)", exc.code)
            return summary
        except Exception as exc:
            db.session.rollback()
            logger.warning(
                "Supplier catalog refresh tick failed (%s)", type(exc).__name__,
            )
            return summary
        finally:
            db.session.remove()
