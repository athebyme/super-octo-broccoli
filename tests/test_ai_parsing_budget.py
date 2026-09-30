"""Durable shared Flash claim behavior without importing the host application."""

from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import hashlib

import pytest
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, create_engine, select

from services import ai_parsing_budget as budget
from services.ozon_draft_ai_transport import FlashOutcome, FlashUsage


NOW = datetime(2026, 9, 27, 0, 0, 0)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "ai.sqlite3"),
                           connect_args={"timeout": 3})
    table = Table("ai_parsing_attempts", MetaData(),
        Column("id", Integer, primary_key=True),
        Column("call_id", String, unique=True, nullable=False),
        Column("lane", String, nullable=False),
        Column("run_uid", String),
        Column("seller_id", Integer),
        Column("provider", String, nullable=False),
        Column("model", String, nullable=False),
        Column("request_fingerprint", String, nullable=False),
        Column("status", String, nullable=False),
        Column("reserved_at", DateTime, nullable=False),
        Column("deadline_at", DateTime, nullable=False),
        Column("finished_at", DateTime),
        Column("http_status", Integer),
        Column("retry_due_at", DateTime),
        Column("safe_code", String),
        Column("prompt_tokens", Integer),
        Column("completion_tokens", Integer),
        Column("cache_hit_tokens", Integer),
        Column("cache_miss_tokens", Integer),
        Column("reasoning_tokens", Integer),
    )
    table.metadata.create_all(engine)
    monkeypatch.setattr(budget, "_table", lambda: table)
    monkeypatch.setattr(budget, "_engine", lambda: engine)
    yield engine, table
    engine.dispose()


def claim(number, *, seller=7, run="r1", fingerprint=None, now=NOW, items=1):
    return budget.reserve_attempt(
        call_id="call-%s" % number,
        lane=budget.SELLER_LANE,
        run_uid=run,
        seller_id=seller,
        item_count=items,
        request_fingerprint=fingerprint or hashlib.sha256(str(number).encode()).hexdigest(),
        now=now,
    )


def rows(ledger):
    engine, table = ledger
    with engine.connect() as conn:
        return conn.execute(select(table).order_by(table.c.id)).mappings().all()


def test_atomic_global_and_seller_claims_across_concurrent_coordinators(ledger):
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(
            lambda number: claim(number, seller=7 if number < 4 else 8,
                                 run="run-%s" % number),
            range(6),
        ))
    admitted = [result for result in results if isinstance(result, budget.AttemptClaim)]
    assert len(admitted) == 3
    assert len([row for row in rows(ledger) if row["seller_id"] == 7]) <= 2
    assert all(row["status"] == "reserved" for row in rows(ledger))


def test_429_cooldown_full_delay_and_nullable_usage(ledger):
    assert isinstance(claim(1), budget.AttemptClaim)
    assert budget.finish_attempt(
        "call-1", FlashOutcome("rate_limited", http_status=429,
                               retry_after_seconds=7200), now=NOW + timedelta(seconds=1),
    )
    denied = claim(2, now=NOW + timedelta(seconds=2))
    assert denied.code == "ai_provider_cooldown"
    assert denied.retry_at == NOW + timedelta(seconds=7201)
    assert rows(ledger)[0]["prompt_tokens"] is None
    assert isinstance(claim(3, now=NOW + timedelta(seconds=7202)), budget.AttemptClaim)


def test_expired_reservation_is_unknown_consumed_budget_and_late_result_fenced(ledger):
    fingerprint = hashlib.sha256(b"same chunk").hexdigest()
    assert isinstance(claim(1, fingerprint=fingerprint), budget.AttemptClaim)
    assert budget.expire_unacknowledged(now=NOW + timedelta(seconds=121)) == 1
    assert rows(ledger)[0]["status"] == "unknown_response"
    assert claim(2, fingerprint=fingerprint, now=NOW + timedelta(seconds=122)).code == "ai_call_already_attempted"
    assert not budget.finish_attempt("call-1", FlashOutcome("success", content={}),
                                     now=NOW + timedelta(seconds=123))
    assert rows(ledger)[0]["status"] == "unknown_response"


def test_lifetime_formula_and_reported_usage_only(ledger):
    assert budget.seller_run_call_limit(1) == 6
    assert budget.seller_run_call_limit(200) == 72
    assert isinstance(claim(1), budget.AttemptClaim)
    assert budget.finish_attempt(
        "call-1", FlashOutcome("success", content={}, http_status=200,
                               usage=FlashUsage(prompt_tokens=20, completion_tokens=4)),
        now=NOW + timedelta(seconds=1),
    )
    assert rows(ledger)[0]["prompt_tokens"] == 20
    assert rows(ledger)[0]["cache_hit_tokens"] is None
    for number in range(2, 7):
        assert isinstance(claim(number, now=NOW + timedelta(seconds=number)), budget.AttemptClaim)
        assert budget.finish_attempt("call-%s" % number,
                                     FlashOutcome("http_error", http_status=503),
                                     now=NOW + timedelta(seconds=number, milliseconds=1))
    assert claim(7, now=NOW + timedelta(seconds=20)).code == "ai_run_call_budget_exhausted"


def test_admin_and_seller_share_global_capacity(ledger):
    assert isinstance(claim(1), budget.AttemptClaim)
    assert isinstance(claim(2), budget.AttemptClaim)
    admin = budget.reserve_attempt(
        call_id="admin-1", lane=budget.ADMIN_LANE, run_uid="admin-run",
        request_fingerprint=hashlib.sha256(b"admin").hexdigest(),
        max_run_calls=1, now=NOW,
    )
    assert isinstance(admin, budget.AttemptClaim)
    assert claim(3, seller=8).code == "ai_global_capacity_full"


def test_total_timeout_holds_slot_until_reader_recovery_fence(ledger):
    assert isinstance(claim(1), budget.AttemptClaim)
    assert budget.finish_attempt(
        "call-1", FlashOutcome("unknown_response", safe_code="ai_total_timeout"),
        now=NOW + timedelta(seconds=60),
    )
    assert rows(ledger)[0]["status"] == "reserved"
    assert budget.expire_unacknowledged(now=NOW + timedelta(seconds=121)) == 1
    assert rows(ledger)[0]["status"] == "unknown_response"


def test_extreme_retry_after_never_wraps_into_early_retry(ledger):
    assert isinstance(claim(1), budget.AttemptClaim)
    assert budget.finish_attempt(
        "call-1", FlashOutcome("rate_limited", http_status=429,
                               retry_after_seconds=1e300), now=NOW,
    )
    assert rows(ledger)[0]["retry_due_at"] == datetime.max
    assert claim(2, now=NOW + timedelta(days=1)).code == "ai_provider_cooldown"


def test_busy_main_sqlite_writer_defers_without_reserving_call(ledger):
    engine, _table = ledger
    blocker = engine.connect()
    blocker.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        denied = claim(1)
        assert denied.code == "ai_budget_busy"
        assert rows(ledger) == []
    finally:
        blocker.rollback()
        blocker.close()
    assert isinstance(claim(1), budget.AttemptClaim)
