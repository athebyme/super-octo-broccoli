"""Durable physical attempt budget shared by admin and seller Flash parsing.

Only a coordinator calls this module. Claims and outcomes use short transactions
in the main SQLite database; no transaction remains open during provider I/O.
The physical call belongs to the reserved ``call_id`` even if its response is
lost. Unknown attempts are never made pending by expiry or restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math
import re
from typing import Optional

from sqlalchemy import and_, case, func, select, update
from sqlalchemy.exc import OperationalError

from services.ozon_draft_ai_transport import FlashOutcome


GLOBAL_ACTIVE_LIMIT = 3
SELLER_ACTIVE_LIMIT = 2
RESERVATION_SECONDS = 120
SELLER_LANE = "seller_draft_completion"
ADMIN_LANE = "admin_supplier_parsing"
LANES = frozenset((SELLER_LANE, ADMIN_LANE))
FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class AttemptClaim:
    call_id: str
    deadline_at: datetime


@dataclass(frozen=True)
class BudgetDenied:
    code: str
    retry_at: Optional[datetime] = None


class BudgetBusy(RuntimeError):
    """Main SQLite writer claim was busy; caller must defer without HTTP."""


def seller_run_call_limit(item_count: int) -> int:
    """Physical calls for a seller run, including known 429 and unknown calls."""
    if type(item_count) is not int or not 1 <= item_count <= 200:
        raise ValueError("invalid_seller_item_count")
    return min(80, ((item_count + 5) // 6) * 2 + 4)


def _table():
    from models import AIParsingAttempt
    return AIParsingAttempt.__table__


def _engine():
    from models import db
    engine = db.engine
    if engine.dialect.name != "sqlite":
        # The claim below relies on SQLite's database-wide writer lock.
        raise RuntimeError("ai_budget_sqlite_required")
    return engine


def _begin_immediate(engine):
    connection = engine.connect()
    try:
        old_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
        connection.info["ai_budget_previous_busy_timeout"] = old_timeout
        connection.exec_driver_sql("PRAGMA busy_timeout=200")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    except Exception:
        _close_connection(connection)
        raise
    return connection


def _close_connection(connection):
    previous = connection.info.pop("ai_budget_previous_busy_timeout", None)
    if previous is not None:
        try:
            connection.exec_driver_sql("PRAGMA busy_timeout=%d" % previous)
        except Exception:
            # The current claim already failed or committed; do not hide it.
            pass
    connection.close()


def _is_busy(error):
    return "database is locked" in str(error).lower() or "database is busy" in str(error).lower()


def _claim_or_busy(engine):
    try:
        return _begin_immediate(engine)
    except OperationalError as error:
        if _is_busy(error):
            raise BudgetBusy("ai_budget_busy") from None
        raise


def expire_unacknowledged(*, now: Optional[datetime] = None) -> int:
    """Fence crashed workers permanently after their 120-second deadline."""
    instant = now or datetime.utcnow()
    table = _table()
    connection = _claim_or_busy(_engine())
    try:
        result = connection.execute(
            update(table).where(and_(
                table.c.status == "reserved", table.c.deadline_at <= instant,
            )).values(
                status="unknown_response", finished_at=instant,
                safe_code=case(
                    (table.c.safe_code == "ai_total_timeout", table.c.safe_code),
                    else_="ai_reservation_expired",
                ),
            )
        )
        connection.commit()
        return result.rowcount
    except OperationalError as error:
        connection.rollback()
        if _is_busy(error):
            raise BudgetBusy("ai_budget_busy") from None
        raise
    except Exception:
        connection.rollback()
        raise
    finally:
        _close_connection(connection)


def reserve_attempt(
    *,
    call_id: str,
    lane: str,
    run_uid: str,
    request_fingerprint: str,
    seller_id: Optional[int] = None,
    item_count: Optional[int] = None,
    max_run_calls: Optional[int] = None,
    now: Optional[datetime] = None,
) -> AttemptClaim | BudgetDenied:
    """Atomically reserve one physical call before creating its HTTP future.

    Seller capacity is derived from exact admitted item count, never supplied
    as a caller-chosen retry allowance. Admin work has an explicit run cap.
    A repeated exact fingerprint after an unknown outcome cannot be replayed.
    """
    if (lane not in LANES or type(call_id) is not str or not 1 <= len(call_id) <= 128
            or type(run_uid) is not str or not 1 <= len(run_uid) <= 128
            or type(request_fingerprint) is not str
            or not FINGERPRINT_RE.fullmatch(request_fingerprint)):
        raise ValueError("invalid_ai_attempt_identity")
    if lane == SELLER_LANE:
        if type(seller_id) is not int or seller_id <= 0 or max_run_calls is not None:
            raise ValueError("invalid_seller_scope")
        limit = seller_run_call_limit(item_count)
    else:
        if seller_id is not None or item_count is not None:
            raise ValueError("invalid_admin_scope")
        if type(max_run_calls) is not int or not 1 <= max_run_calls <= 1600:
            raise ValueError("invalid_admin_call_limit")
        limit = max_run_calls
    instant = now or datetime.utcnow()
    deadline = instant + timedelta(seconds=RESERVATION_SECONDS)
    table = _table()
    try:
        connection = _claim_or_busy(_engine())
    except BudgetBusy:
        return BudgetDenied("ai_budget_busy", instant + timedelta(seconds=1))
    try:
        # Expiry changes only the attempt state. It never frees its call budget.
        connection.execute(
            update(table).where(and_(
                table.c.status == "reserved", table.c.deadline_at <= instant,
            )).values(
                status="unknown_response", finished_at=instant,
                safe_code=case(
                    (table.c.safe_code == "ai_total_timeout", table.c.safe_code),
                    else_="ai_reservation_expired",
                ),
            )
        )
        previous_call = connection.execute(select(table.c.id).where(table.c.call_id == call_id)).first()
        if previous_call:
            connection.commit()
            return BudgetDenied("ai_duplicate_call_id")
        prior = connection.execute(
            select(table.c.status).where(and_(
                table.c.lane == lane,
                table.c.run_uid == run_uid,
                table.c.request_fingerprint == request_fingerprint,
            )).order_by(table.c.id.desc()).limit(1)
        ).scalar_one_or_none()
        if prior in ("reserved", "unknown_response", "succeeded", "invalid_response", "http_error"):
            connection.commit()
            return BudgetDenied("ai_call_already_attempted")
        due = connection.execute(
            select(func.max(table.c.retry_due_at)).where(and_(
                table.c.status == "rate_limited", table.c.retry_due_at > instant,
            ))
        ).scalar_one()
        if due is not None:
            connection.commit()
            return BudgetDenied("ai_provider_cooldown", due)
        count = connection.execute(
            select(func.count()).select_from(table).where(and_(
                table.c.lane == lane, table.c.run_uid == run_uid,
            ))
        ).scalar_one()
        if count >= limit:
            connection.commit()
            return BudgetDenied("ai_run_call_budget_exhausted")
        active = connection.execute(
            select(func.count()).select_from(table).where(table.c.status == "reserved")
        ).scalar_one()
        if active >= GLOBAL_ACTIVE_LIMIT:
            connection.commit()
            return BudgetDenied("ai_global_capacity_full")
        if seller_id is not None:
            seller_active = connection.execute(
                select(func.count()).select_from(table).where(and_(
                    table.c.seller_id == seller_id, table.c.status == "reserved",
                ))
            ).scalar_one()
            if seller_active >= SELLER_ACTIVE_LIMIT:
                connection.commit()
                return BudgetDenied("ai_seller_capacity_full")
        connection.execute(table.insert().values(
            call_id=call_id,
            lane=lane,
            run_uid=run_uid,
            seller_id=seller_id,
            provider="deepseek",
            model="deepseek-flash",
            request_fingerprint=request_fingerprint,
            status="reserved",
            reserved_at=instant,
            deadline_at=deadline,
        ))
        connection.commit()
        return AttemptClaim(call_id, deadline)
    except OperationalError as error:
        connection.rollback()
        if _is_busy(error):
            return BudgetDenied("ai_budget_busy", instant + timedelta(seconds=1))
        raise
    except Exception:
        connection.rollback()
        raise
    finally:
        _close_connection(connection)


def finish_attempt(
    call_id: str,
    outcome: FlashOutcome,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Commit bounded outcome metadata; return False for a fenced late result."""
    if type(call_id) is not str or not 1 <= len(call_id) <= 128:
        raise ValueError("invalid_call_id")
    if outcome.kind not in (
        "success", "rate_limited", "http_error", "invalid_response", "unknown_response",
    ):
        raise ValueError("invalid_ai_outcome")
    instant = now or datetime.utcnow()
    table = _table()
    try:
        connection = _claim_or_busy(_engine())
    except BudgetBusy:
        # No outcome can be committed, so leave the reservation to expire
        # unknown; the caller must discard the result.
        return False
    try:
        row = connection.execute(
            select(table.c.id, table.c.status, table.c.deadline_at).where(table.c.call_id == call_id)
        ).first()
        if row is None or row.status != "reserved":
            connection.commit()
            return False
        if row.deadline_at <= instant:
            connection.execute(
                update(table).where(table.c.id == row.id).values(
                    status="unknown_response", finished_at=instant,
                    safe_code="ai_reservation_expired",
                )
            )
            connection.commit()
            return False
        if outcome.kind == "unknown_response" and outcome.safe_code == "ai_total_timeout":
            # The response reader may still be unwinding after forced close.
            # Keep the shared physical slot occupied until the 120s fence.
            connection.execute(
                update(table).where(table.c.id == row.id).values(
                    safe_code="ai_total_timeout",
                )
            )
            connection.commit()
            return True
        status = "succeeded" if outcome.kind == "success" else outcome.kind
        due = None
        if status == "rate_limited":
            delay = outcome.retry_after_seconds
            if type(delay) not in (int, float) or not math.isfinite(delay) or delay < 0:
                delay = 60.0
            delay = max(1.0, delay)
            max_delay = (datetime.max - instant).total_seconds()
            due = datetime.max if delay >= max_delay else instant + timedelta(seconds=delay)
        usage = outcome.usage
        counts = (
            usage.prompt_tokens, usage.completion_tokens, usage.cache_hit_tokens,
            usage.cache_miss_tokens, usage.reasoning_tokens,
        )
        if any(value is not None and (type(value) is not int or value < 0 or value > (1 << 63) - 1)
               for value in counts):
            raise ValueError("invalid_ai_usage")
        safe_code = outcome.safe_code
        if safe_code is not None and (
            type(safe_code) is not str or len(safe_code) > 80
            or not re.fullmatch(r"[a-z0-9_]+", safe_code)
        ):
            raise ValueError("invalid_ai_safe_code")
        connection.execute(
            update(table).where(table.c.id == row.id).values(
                status=status,
                finished_at=instant,
                http_status=outcome.http_status,
                retry_due_at=due,
                safe_code=safe_code,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cache_hit_tokens=usage.cache_hit_tokens,
                cache_miss_tokens=usage.cache_miss_tokens,
                reasoning_tokens=usage.reasoning_tokens,
            )
        )
        connection.commit()
        return True
    except OperationalError as error:
        connection.rollback()
        if _is_busy(error):
            return False
        raise
    except Exception:
        connection.rollback()
        raise
    finally:
        _close_connection(connection)
