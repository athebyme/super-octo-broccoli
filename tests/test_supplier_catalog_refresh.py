"""Durable lifecycle tests for administrator supplier catalog refreshes."""

from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

from flask import Flask
import pytest

from models import AdminAuditLog, Supplier, User, db
from services.supplier_catalog_refresh import (
    SupplierCatalogRefreshError,
    enqueue_catalog_refresh,
    get_catalog_refresh,
    get_catalog_schedule,
    run_catalog_refresh_tick,
    set_catalog_schedule,
)
from services.supplier_catalog_refresh_models import SupplierCatalogRefreshRun
from services.supplier_catalog_schedule_models import (
    SupplierCatalogSchedule,
    SupplierCatalogScheduledRun,
)
from services.supplier_service import SupplierService
import services.supplier_service as supplier_service_module


@pytest.fixture(scope="module")
def refresh_app(tmp_path_factory):
    database = tmp_path_factory.mktemp("supplier-refresh") / "catalog-refresh.sqlite"
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SECRET_KEY="catalog-refresh-test",
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{database}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def refresh_db(refresh_app):
    suffix = uuid.uuid4().hex
    with refresh_app.app_context():
        admin = User(
            username=f"refresh-admin-{suffix}",
            email=f"refresh-admin-{suffix}@test.local",
        )
        admin.set_password("synthetic-password")
        admin.is_admin = True
        supplier = Supplier(
            name="Refresh fixture",
            code=f"refresh-{suffix}",
            csv_source_url="https://supplier.invalid/catalog.csv",
        )
        db.session.add_all([admin, supplier])
        db.session.commit()
        fixture = {
            "app": refresh_app,
            "supplier_id": supplier.id,
            "admin_id": admin.id,
            "extra_supplier_ids": [],
            "extra_admin_ids": [],
        }
        db.session.remove()

    yield fixture

    with refresh_app.app_context():
        db.session.remove()
        SupplierCatalogScheduledRun.query.filter(
            SupplierCatalogScheduledRun.run_id.in_(
                db.session.query(SupplierCatalogRefreshRun.id).filter(
                    SupplierCatalogRefreshRun.supplier_id.in_(
                        [fixture["supplier_id"], *fixture["extra_supplier_ids"]],
                    ),
                ),
            ),
        ).delete(synchronize_session=False)
        SupplierCatalogSchedule.query.filter(
            SupplierCatalogSchedule.supplier_id.in_(
                [fixture["supplier_id"], *fixture["extra_supplier_ids"]],
            ),
        ).delete(synchronize_session=False)
        AdminAuditLog.query.filter_by(admin_user_id=fixture["admin_id"]).delete()
        for extra_admin_id in fixture["extra_admin_ids"]:
            AdminAuditLog.query.filter_by(admin_user_id=extra_admin_id).delete()
        SupplierCatalogRefreshRun.query.filter_by(
            supplier_id=fixture["supplier_id"],
        ).delete()
        supplier = db.session.get(Supplier, fixture["supplier_id"])
        admin = db.session.get(User, fixture["admin_id"])
        for supplier_id in fixture["extra_supplier_ids"]:
            SupplierCatalogSchedule.query.filter_by(
                supplier_id=supplier_id,
            ).delete()
            SupplierCatalogRefreshRun.query.filter_by(
                supplier_id=supplier_id,
            ).delete()
            extra_supplier = db.session.get(Supplier, supplier_id)
            if extra_supplier is not None:
                db.session.delete(extra_supplier)
        if supplier is not None:
            db.session.delete(supplier)
        if admin is not None:
            db.session.delete(admin)
        db.session.commit()
        db.session.remove()


@pytest.fixture
def stable_config(monkeypatch):
    digest = "a" * 64
    monkeypatch.setattr(
        SupplierService,
        "supplier_catalog_config_digest",
        staticmethod(lambda _supplier: digest),
        raising=False,
    )
    return digest


@pytest.fixture
def stable_catalog_config(monkeypatch):
    digest = "a" * 64
    monkeypatch.setattr(
        SupplierService,
        "supplier_catalog_config_digest",
        staticmethod(lambda _supplier, *, catalog_only=False: digest),
        raising=False,
    )
    return digest


def _enqueue(fixture, expected_latest_id=None, *, ip_address="127.0.0.1"):
    with fixture["app"].app_context():
        return enqueue_catalog_refresh(
            supplier_id=fixture["supplier_id"],
            admin_user_id=fixture["admin_id"],
            expected_latest_id=expected_latest_id,
            ip_address=ip_address,
        )


@contextmanager
def _acquired_supplier_lock(_supplier_id):
    yield True


def _patch_lock(monkeypatch):
    monkeypatch.setattr(
        supplier_service_module,
        "_supplier_catalog_sync_lock",
        _acquired_supplier_lock,
    )


def _result(*, success=True, errors=0):
    return SimpleNamespace(
        success=success,
        total_in_csv=2,
        added=1,
        updated=1,
        errors=errors,
        price_followup_status="success",
        price_followup_conflicts=0,
    )


def _mock_sync_with_checkpoint(monkeypatch, *, crash=None, result=None):
    result = result or _result()

    def sync(
        supplier_id,
        *,
        start_time,
        expected_config_digest,
        raw_checkpoint,
    ):
        del start_time
        assert supplier_id > 0
        assert expected_config_digest == "a" * 64
        db.session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        raw_checkpoint(result, datetime.utcnow())
        if crash == "before_commit":
            raise SystemExit("synthetic process exit before raw commit")
        db.session.commit()
        if crash == "after_commit":
            raise SystemExit("synthetic process exit after raw commit")
        return result

    monkeypatch.setattr(
        SupplierService,
        "_sync_from_csv_locked",
        staticmethod(sync),
    )


def _read_receipt(fixture, run_id, supplier_id=None):
    with fixture["app"].app_context():
        return get_catalog_refresh(
            supplier_id or fixture["supplier_id"], run_id,
        )


def _set_schedule(fixture, *, enabled=True, interval_hours=24, revision=None, actor_id=None):
    with fixture["app"].app_context():
        return set_catalog_schedule(
            supplier_id=fixture["supplier_id"],
            admin_user_id=actor_id or fixture["admin_id"],
            enabled=enabled,
            interval_hours=interval_hours,
            expected_revision=revision,
        )


def _mock_catalog_sync(monkeypatch, *, result=None, crash=None, revoke_before_checkpoint=False):
    result = result or _result()
    calls = []

    def sync(
        supplier_id,
        *,
        start_time,
        expected_config_digest,
        raw_checkpoint,
        catalog_only=False,
        pre_fetch_check=None,
    ):
        del start_time
        assert supplier_id > 0
        assert expected_config_digest == "a" * 64
        calls.append({
            "supplier_id": supplier_id,
            "catalog_only": catalog_only,
            "pre_fetch_check": pre_fetch_check is not None,
        })
        if pre_fetch_check is not None:
            pre_fetch_check()
        db.session.rollback()
        calls.append({"provider_started": True})
        if revoke_before_checkpoint:
            schedule = db.session.get(SupplierCatalogSchedule, supplier_id)
            schedule.enabled = False
            schedule.revision += 1
            schedule.updated_at = datetime.utcnow()
            db.session.commit()
        db.session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        if crash == "before_commit":
            raw_checkpoint(result, datetime.utcnow())
            raise SystemExit("synthetic scheduled worker exit before raw commit")
        try:
            raw_checkpoint(result, datetime.utcnow())
        except SupplierCatalogRefreshError as exc:
            db.session.rollback()
            return SimpleNamespace(
                success=False,
                total_in_csv=0,
                added=0,
                updated=0,
                errors=1,
                catalog_error_code=exc.code,
                price_followup_status="skipped",
                price_followup_conflicts=0,
            )
        if crash == "after_commit":
            db.session.commit()
            raise SystemExit("synthetic scheduled worker exit after raw commit")
        db.session.commit()
        return result

    monkeypatch.setattr(
        SupplierService,
        "_sync_from_csv_locked",
        staticmethod(sync),
    )
    return calls


def test_duplicate_active_post_returns_same_receipt_and_one_audit(
    refresh_db, stable_config,
):
    del stable_config
    first, created = _enqueue(refresh_db)
    replay, replay_created = _enqueue(refresh_db, first["id"])
    stale_form, stale_created = _enqueue(refresh_db, None)

    assert created is True
    assert replay_created is False
    assert stale_created is False
    assert replay == first
    assert stale_form == first
    with refresh_db["app"].app_context():
        rows = AdminAuditLog.query.filter_by(
            action="queue_supplier_catalog_refresh",
        ).all()
        assert len(rows) == 1
        assert rows[0].target_type == "supplier"
        assert rows[0].target_id == refresh_db["supplier_id"]
        assert json.loads(rows[0].details) == {"run_id": first["id"]}
        assert rows[0].ip_address == "127.0.0.1"


def test_old_form_replay_after_terminal_does_not_create_new_run(
    refresh_db, stable_config,
):
    del stable_config
    first, created = _enqueue(refresh_db)
    assert created is True
    with refresh_db["app"].app_context():
        run = db.session.get(SupplierCatalogRefreshRun, first["id"])
        run.status = "completed"
        run.completed_at = datetime.utcnow()
        db.session.commit()

    replay, replay_created = _enqueue(refresh_db, None)
    assert replay_created is False
    assert replay["id"] == first["id"]
    assert replay["status"] == "completed"

    fresh, fresh_created = _enqueue(refresh_db, first["id"])
    assert fresh_created is True
    assert fresh["id"] != first["id"]
    assert fresh["created_at"] > first["created_at"]


def test_concurrent_same_form_posts_admit_one_receipt(refresh_db, stable_config):
    del stable_config
    barrier = threading.Barrier(2)
    outcomes = []
    errors = []

    def submit():
        try:
            with refresh_db["app"].app_context():
                barrier.wait(timeout=5)
                outcomes.append(enqueue_catalog_refresh(
                    supplier_id=refresh_db["supplier_id"],
                    admin_user_id=refresh_db["admin_id"],
                    expected_latest_id=None,
                    ip_address="2001:db8::1",
                ))
                db.session.remove()
        except Exception as exc:  # surfaced as an assertion in the parent thread
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert len(outcomes) == 2
    assert sum(created for _payload, created in outcomes) == 1
    assert outcomes[0][0] == outcomes[1][0]
    with refresh_db["app"].app_context():
        assert SupplierCatalogRefreshRun.query.count() == 1
        assert AdminAuditLog.query.filter_by(
            action="queue_supplier_catalog_refresh",
        ).count() == 1


def test_changed_config_fails_before_catalog_provider_or_apply(
    refresh_db, monkeypatch,
):
    digests = iter(("a" * 64, "b" * 64))
    monkeypatch.setattr(
        SupplierService,
        "supplier_catalog_config_digest",
        staticmethod(lambda _supplier: next(digests)),
        raising=False,
    )
    _patch_lock(monkeypatch)
    sync = Mock(side_effect=AssertionError("must not start with changed config"))
    monkeypatch.setattr(SupplierService, "_sync_from_csv_locked", sync, raising=False)
    queued, created = _enqueue(refresh_db)

    result = run_catalog_refresh_tick(refresh_db["app"])

    assert created is True
    assert result["failed"] == 1
    sync.assert_not_called()
    receipt = _read_receipt(refresh_db, queued["id"])
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "configuration_changed"


def test_disabled_supplier_can_be_queued_but_active_toggle_invalidates_snapshot(
    refresh_db, monkeypatch,
):
    _patch_lock(monkeypatch)
    sync = Mock(side_effect=AssertionError("changed config must not start"))
    monkeypatch.setattr(SupplierService, "_sync_from_csv_locked", sync, raising=False)
    with refresh_db["app"].app_context():
        supplier = db.session.get(Supplier, refresh_db["supplier_id"])
        supplier.is_active = False
        db.session.commit()

    queued, created = _enqueue(refresh_db)
    with refresh_db["app"].app_context():
        supplier = db.session.get(Supplier, refresh_db["supplier_id"])
        supplier.is_active = True
        db.session.commit()

    summary = run_catalog_refresh_tick(refresh_db["app"])

    assert created is True
    assert summary["failed"] == 1
    sync.assert_not_called()
    receipt = _read_receipt(refresh_db, queued["id"])
    assert receipt["error_code"] == "configuration_changed"


@pytest.mark.parametrize("revocation", ["inactive", "blocked"])
def test_deactivated_or_blocked_admin_cannot_claim_pending_run(
    refresh_db, stable_config, monkeypatch, revocation,
):
    del stable_config
    _patch_lock(monkeypatch)
    sync = Mock(side_effect=AssertionError("unauthorized job must not run"))
    monkeypatch.setattr(SupplierService, "_sync_from_csv_locked", sync, raising=False)
    queued, _ = _enqueue(refresh_db)
    with refresh_db["app"].app_context():
        admin = db.session.get(User, refresh_db["admin_id"])
        if revocation == "inactive":
            admin.is_active = False
        else:
            admin.blocked_at = datetime.utcnow()
        db.session.commit()

    result = run_catalog_refresh_tick(refresh_db["app"])

    assert result["failed"] == 1
    sync.assert_not_called()
    receipt = _read_receipt(refresh_db, queued["id"])
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "admin_not_authorized"


@pytest.mark.parametrize(
    ("crash", "expected_raw_committed"),
    [("before_commit", False), ("after_commit", True)],
)
def test_restart_marks_running_interrupted_without_replay_and_preserves_phase(
    refresh_db, stable_config, monkeypatch, crash, expected_raw_committed,
):
    del stable_config
    _patch_lock(monkeypatch)
    _mock_sync_with_checkpoint(monkeypatch, crash=crash)
    queued, _ = _enqueue(refresh_db)

    with pytest.raises(SystemExit):
        run_catalog_refresh_tick(refresh_db["app"])
    after_crash = _read_receipt(refresh_db, queued["id"])
    assert after_crash["status"] == "running"
    assert bool(after_crash["raw_committed_at"]) is expected_raw_committed

    sync = Mock(side_effect=AssertionError("recovery must never replay"))
    monkeypatch.setattr(SupplierService, "_sync_from_csv_locked", sync)
    recovery = run_catalog_refresh_tick(refresh_db["app"])

    assert recovery["interrupted"] == 1
    sync.assert_not_called()
    final = _read_receipt(refresh_db, queued["id"])
    assert final["status"] == "interrupted"
    assert bool(final["raw_committed_at"]) is expected_raw_committed
    if expected_raw_committed:
        assert (final["total"], final["added"], final["updated"]) == (2, 1, 1)
    else:
        assert final["total"] is None


@pytest.mark.parametrize(
    ("initial_status", "expected_status", "expected_summary"),
    [
        ("pending", "failed", "failed"),
        ("running", "interrupted", "interrupted"),
    ],
)
def test_deleted_supplier_receipts_are_reconciled_without_replay(
    refresh_db, stable_config, monkeypatch,
    initial_status, expected_status, expected_summary,
):
    del stable_config
    _patch_lock(monkeypatch)
    queued, _ = _enqueue(refresh_db)
    with refresh_db["app"].app_context():
        if initial_status == "running":
            run = db.session.get(SupplierCatalogRefreshRun, queued["id"])
            run.status = "running"
            run.started_at = datetime.utcnow()
            db.session.commit()
        assert SupplierService.delete_supplier(refresh_db["supplier_id"]) is True
        assert db.session.get(Supplier, refresh_db["supplier_id"]) is None
        # Runtime SQLite connections do not enable FK enforcement, so this
        # reproduces the native supplier hard-delete's surviving queue row.
        assert db.session.get(SupplierCatalogRefreshRun, queued["id"]) is not None

    sync = Mock(side_effect=AssertionError("orphan recovery must never replay"))
    monkeypatch.setattr(SupplierService, "_sync_from_csv_locked", sync, raising=False)
    summary = run_catalog_refresh_tick(refresh_db["app"])

    assert summary[expected_summary] == 1
    sync.assert_not_called()
    with refresh_db["app"].app_context():
        receipt = db.session.get(SupplierCatalogRefreshRun, queued["id"])
        assert receipt.status == expected_status
        assert receipt.completed_at is not None
        if initial_status == "running":
            assert receipt.error_code == "worker_interrupted"
        else:
            assert receipt.error_code == "supplier_not_found"


def test_recreated_supplier_cannot_claim_deleted_supplier_pending_run(
    refresh_db, monkeypatch,
):
    _patch_lock(monkeypatch)
    queued, _ = _enqueue(refresh_db)
    with refresh_db["app"].app_context():
        supplier = db.session.get(Supplier, refresh_db["supplier_id"])
        original_created_at = supplier.created_at
        original_code = supplier.code
        assert SupplierService.delete_supplier(refresh_db["supplier_id"]) is True

        replacement = Supplier(
            name="Refresh fixture",
            code=original_code,
            csv_source_url="https://supplier.invalid/catalog.csv",
            created_at=original_created_at + timedelta(seconds=1),
        )
        db.session.add(replacement)
        db.session.commit()
        assert replacement.id == refresh_db["supplier_id"]

    sync = Mock(side_effect=AssertionError("recreated supplier must not claim old work"))
    monkeypatch.setattr(SupplierService, "_sync_from_csv_locked", sync, raising=False)
    summary = run_catalog_refresh_tick(refresh_db["app"])

    assert summary["failed"] == 1
    sync.assert_not_called()
    receipt = _read_receipt(refresh_db, queued["id"])
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "configuration_changed"


def test_normal_worker_commits_raw_checkpoint_and_terminal_price_result(
    refresh_db, stable_config, monkeypatch,
):
    del stable_config
    _patch_lock(monkeypatch)
    _mock_sync_with_checkpoint(monkeypatch, result=_result())
    queued, _ = _enqueue(refresh_db)

    result = run_catalog_refresh_tick(refresh_db["app"])

    assert result["processed"] == 1
    receipt = _read_receipt(refresh_db, queued["id"])
    assert receipt["status"] == "completed"
    assert receipt["raw_committed_at"]
    assert receipt["completed_at"]
    assert (receipt["total"], receipt["added"], receipt["updated"]) == (2, 1, 1)
    assert receipt["postprocess_errors"] == 0
    assert receipt["price_status"] == "success"
    assert receipt["price_conflicts"] == 0


def test_price_conflicts_finish_as_partial_not_success(
    refresh_db, stable_config, monkeypatch,
):
    del stable_config
    _patch_lock(monkeypatch)
    _mock_sync_with_checkpoint(monkeypatch, result=SimpleNamespace(
        **{
            **_result().__dict__,
            "price_followup_status": "partial",
            "price_followup_conflicts": 2,
        },
    ))
    queued, _ = _enqueue(refresh_db)

    run_catalog_refresh_tick(refresh_db["app"])

    receipt = _read_receipt(refresh_db, queued["id"])
    assert receipt["status"] == "partial"
    assert receipt["price_status"] == "partial"
    assert receipt["price_conflicts"] == 2
    assert receipt["error_code"] == "price_sync_partial"


def test_busy_running_lock_does_not_starve_pending_supplier(
    refresh_db, stable_config, monkeypatch,
):
    del stable_config
    with refresh_db["app"].app_context():
        second_supplier = Supplier(
            name="Second refresh fixture",
            code=f"refresh-second-{uuid.uuid4().hex}",
            csv_source_url="https://supplier.invalid/second.csv",
        )
        db.session.add(second_supplier)
        db.session.commit()
        second_supplier_id = second_supplier.id
        refresh_db["extra_supplier_ids"].append(second_supplier_id)

    running, _ = _enqueue(refresh_db)
    with refresh_db["app"].app_context():
        run = db.session.get(SupplierCatalogRefreshRun, running["id"])
        run.status = "running"
        run.started_at = datetime.utcnow()
        db.session.commit()
    with refresh_db["app"].app_context():
        pending, created = enqueue_catalog_refresh(
            supplier_id=second_supplier_id,
            admin_user_id=refresh_db["admin_id"],
            expected_latest_id=None,
        )
    assert created is True

    @contextmanager
    def lock(supplier_id):
        yield supplier_id != refresh_db["supplier_id"]

    monkeypatch.setattr(supplier_service_module, "_supplier_catalog_sync_lock", lock)
    _mock_sync_with_checkpoint(monkeypatch)

    summary = run_catalog_refresh_tick(refresh_db["app"])

    assert summary["busy"] == 1
    assert summary["processed"] == 1
    assert _read_receipt(refresh_db, running["id"])["status"] == "running"
    assert _read_receipt(
        refresh_db, pending["id"], supplier_id=second_supplier_id,
    )["status"] == "completed"


def test_corrupt_candidate_is_not_locked_or_mutated(
    refresh_db, stable_config, monkeypatch,
):
    del stable_config
    queued, _ = _enqueue(refresh_db)
    with refresh_db["app"].app_context():
        run = db.session.get(SupplierCatalogRefreshRun, queued["id"])
        run.config_digest = "invalid-private-config"
        db.session.commit()

    lock = Mock(side_effect=AssertionError("corrupt row must be filtered before lock"))
    monkeypatch.setattr(supplier_service_module, "_supplier_catalog_sync_lock", lock)
    summary = run_catalog_refresh_tick(refresh_db["app"])

    assert summary == {
        "processed": 0,
        "interrupted": 0,
        "busy": 0,
        "failed": 0,
        "scheduled": 0,
    }
    lock.assert_not_called()
    receipt = _read_receipt(refresh_db, queued["id"])
    assert receipt["status"] == "pending"


def test_get_is_read_only_and_supplier_scoped(refresh_db, stable_config):
    del stable_config
    queued, _ = _enqueue(refresh_db)
    with refresh_db["app"].app_context():
        supplier = db.session.get(Supplier, refresh_db["supplier_id"])
        supplier.name = "uncommitted fixture edit"
        first = get_catalog_refresh(refresh_db["supplier_id"], queued["id"])
        absent = get_catalog_refresh(refresh_db["supplier_id"] + 1, queued["id"])
        assert first["id"] == queued["id"]
        assert absent is None
        assert first["status"] == "pending"
        assert db.session.is_modified(supplier)
        db.session.rollback()


def test_admission_rejects_bad_actor_and_malformed_scope(refresh_db):
    with refresh_db["app"].app_context():
        with pytest.raises(SupplierCatalogRefreshError) as actor_error:
            enqueue_catalog_refresh(
                supplier_id=refresh_db["supplier_id"],
                admin_user_id=999999,
                expected_latest_id=None,
            )
        assert actor_error.value.code == "admin_not_authorized"

        with pytest.raises(SupplierCatalogRefreshError) as id_error:
            enqueue_catalog_refresh(
                supplier_id=refresh_db["supplier_id"],
                admin_user_id=refresh_db["admin_id"],
                expected_latest_id="not-a-run-id",
            )
        assert id_error.value.code == "invalid_expected_run_id"


def test_schedule_get_set_cas_noop_and_actor_audit(refresh_db):
    with refresh_db["app"].app_context():
        assert get_catalog_schedule(refresh_db["supplier_id"]) is None
        assert SupplierCatalogSchedule.query.count() == 0

    created = _set_schedule(refresh_db)
    assert created["supplier_id"] == refresh_db["supplier_id"]
    assert created["enabled"] is True
    assert created["interval_hours"] == 24
    assert created["authorized_by_user_id"] == refresh_db["admin_id"]
    assert created["revision"] == 1
    assert created["next_due_at"] is not None

    unchanged = _set_schedule(refresh_db, revision=created["revision"])
    assert unchanged == created
    with refresh_db["app"].app_context():
        assert AdminAuditLog.query.filter_by(
            action="set_supplier_catalog_schedule",
        ).count() == 1

        second_admin = User(
            username=f"refresh-second-admin-{uuid.uuid4().hex}",
            email=f"refresh-second-admin-{uuid.uuid4().hex}@test.local",
            is_admin=True,
        )
        second_admin.set_password("synthetic-password")
        db.session.add(second_admin)
        db.session.commit()
        refresh_db["extra_admin_ids"].append(second_admin.id)
        second_admin_id = second_admin.id

    transferred = _set_schedule(
        refresh_db,
        revision=created["revision"],
        actor_id=second_admin_id,
    )
    assert transferred["authorized_by_user_id"] == second_admin_id
    assert transferred["revision"] == created["revision"] + 1
    assert transferred["next_due_at"] >= created["next_due_at"]
    with refresh_db["app"].app_context():
        audits = AdminAuditLog.query.filter_by(
            action="set_supplier_catalog_schedule",
        ).order_by(AdminAuditLog.id.asc()).all()
        assert [row.admin_user_id for row in audits] == [
            refresh_db["admin_id"], second_admin_id,
        ]
        assert json.loads(audits[-1].details) == {
            "enabled": True,
            "interval_hours": 24,
            "revision": transferred["revision"],
        }
        with pytest.raises(SupplierCatalogRefreshError) as stale:
            set_catalog_schedule(
                supplier_id=refresh_db["supplier_id"],
                admin_user_id=second_admin_id,
                enabled=True,
                interval_hours=24,
                expected_revision=created["revision"],
            )
        assert stale.value.code == "schedule_conflict"


@pytest.mark.parametrize(
    ("enabled", "interval_hours", "expected_code"),
    [
        (1, 24, "invalid_enabled"),
        (True, True, "invalid_interval_hours"),
        (True, 5, "invalid_interval_hours"),
        (True, 169, "invalid_interval_hours"),
        (True, 24, "invalid_expected_revision"),
    ],
)
def test_schedule_rejects_noncanonical_values(
    refresh_db, enabled, interval_hours, expected_code,
):
    with refresh_db["app"].app_context():
        with pytest.raises(SupplierCatalogRefreshError) as error:
            set_catalog_schedule(
                supplier_id=refresh_db["supplier_id"],
                admin_user_id=refresh_db["admin_id"],
                enabled=enabled,
                interval_hours=interval_hours,
                expected_revision=("bad" if expected_code == "invalid_expected_revision" else None),
            )
        assert error.value.code == expected_code


def test_schedule_get_is_read_only_and_does_not_create_queue(refresh_db):
    before = _set_schedule(refresh_db)
    with refresh_db["app"].app_context():
        supplier = db.session.get(Supplier, refresh_db["supplier_id"])
        supplier.name = "uncommitted schedule fixture edit"
        first = get_catalog_schedule(refresh_db["supplier_id"])
        second = get_catalog_schedule(refresh_db["supplier_id"])
        assert first == second == before
        assert db.session.is_modified(supplier)
        assert SupplierCatalogRefreshRun.query.count() == 0
        assert AdminAuditLog.query.filter_by(
            action="set_supplier_catalog_schedule",
        ).count() == 1
        db.session.rollback()


@pytest.mark.parametrize(
    ("result", "expected_status", "expected_error"),
    [
        (_result(), "completed", None),
        (_result(errors=1), "partial", "postprocess_partial"),
    ],
)
def test_due_schedule_runs_catalog_only_and_records_origin(
    refresh_db, stable_catalog_config, monkeypatch,
    result, expected_status, expected_error,
):
    del stable_catalog_config
    _patch_lock(monkeypatch)
    calls = _mock_catalog_sync(monkeypatch, result=result)
    schedule = _set_schedule(refresh_db, interval_hours=24)

    summary = run_catalog_refresh_tick(refresh_db["app"])

    assert summary["processed"] == 1
    assert summary["scheduled"] == 1
    assert calls[0]["catalog_only"] is True
    assert calls[0]["pre_fetch_check"] is True
    with refresh_db["app"].app_context():
        run = SupplierCatalogRefreshRun.query.filter_by(
            supplier_id=refresh_db["supplier_id"],
        ).one()
        receipt = get_catalog_refresh(refresh_db["supplier_id"], run.id)
        extension = db.session.get(SupplierCatalogScheduledRun, run.id)
        current_schedule = db.session.get(
            SupplierCatalogSchedule, refresh_db["supplier_id"],
        )
        assert run.admin_user_id == refresh_db["admin_id"]
        assert receipt["trigger_kind"] == "scheduled"
        assert receipt["due_at"] == schedule["next_due_at"]
        assert extension.schedule_revision == schedule["revision"]
        assert receipt["status"] == expected_status
        assert receipt["error_code"] == expected_error
        assert receipt["price_status"] == "skipped"
        assert receipt["price_conflicts"] is None
        assert current_schedule.revision == schedule["revision"]
        assert current_schedule.next_due_at > datetime.utcnow()
        assert AdminAuditLog.query.filter_by(
            action="queue_supplier_catalog_refresh",
        ).count() == 0

    again = run_catalog_refresh_tick(refresh_db["app"])
    assert again["processed"] == 0
    assert again["scheduled"] == 0
    assert len([call for call in calls if call.get("provider_started")]) == 1


def test_manual_pending_receipt_runs_before_due_schedule_and_coexists(
    refresh_db, stable_catalog_config, monkeypatch,
):
    del stable_catalog_config
    _patch_lock(monkeypatch)
    calls = _mock_catalog_sync(monkeypatch)
    schedule = _set_schedule(refresh_db)
    manual, created = _enqueue(refresh_db)
    assert created is True
    duplicate, duplicate_created = _enqueue(refresh_db, manual["id"])
    assert duplicate_created is False
    assert duplicate["id"] == manual["id"]

    first = run_catalog_refresh_tick(refresh_db["app"])
    assert first["processed"] == 1
    assert first["scheduled"] == 0
    assert _read_receipt(refresh_db, manual["id"])["trigger_kind"] == "manual"
    with refresh_db["app"].app_context():
        current_schedule = db.session.get(
            SupplierCatalogSchedule, refresh_db["supplier_id"],
        )
        assert current_schedule.revision == schedule["revision"]
        assert current_schedule.next_due_at == datetime.fromisoformat(schedule["next_due_at"])
        assert SupplierCatalogScheduledRun.query.count() == 0

    second = run_catalog_refresh_tick(refresh_db["app"])
    assert second["processed"] == 1
    assert second["scheduled"] == 1
    assert _read_receipt(refresh_db, manual["id"])["trigger_kind"] == "manual"
    with refresh_db["app"].app_context():
        scheduled = SupplierCatalogRefreshRun.query.filter_by(
            supplier_id=refresh_db["supplier_id"],
        ).filter(SupplierCatalogRefreshRun.id != manual["id"]).one()
        assert get_catalog_refresh(refresh_db["supplier_id"], scheduled.id)["trigger_kind"] == "scheduled"
        assert len([call for call in calls if call.get("provider_started")]) == 2


def test_manual_active_run_prevents_duplicate_scheduled_admission(
    refresh_db, stable_catalog_config, monkeypatch,
):
    del stable_catalog_config
    _patch_lock(monkeypatch)
    schedule = _set_schedule(refresh_db)
    manual, created = _enqueue(refresh_db)
    assert created is True
    with refresh_db["app"].app_context():
        scheduled_module = __import__(
            "services.supplier_catalog_refresh", fromlist=["_admit_due_schedule"],
        )
        second_admission = scheduled_module._admit_due_schedule(
            refresh_db["supplier_id"], schedule["revision"],
        )
        assert second_admission is None
        assert SupplierCatalogRefreshRun.query.filter_by(
            supplier_id=refresh_db["supplier_id"],
        ).count() == 1
        assert SupplierCatalogScheduledRun.query.count() == 0
        assert db.session.get(
            SupplierCatalogSchedule, refresh_db["supplier_id"],
        ).next_due_at == datetime.fromisoformat(schedule["next_due_at"])
    receipt, created_again = _enqueue(refresh_db, manual["id"])
    assert created_again is False
    assert receipt["id"] == manual["id"]


def test_scheduled_admission_guard_and_missed_interval_coalescing(
    refresh_db, stable_catalog_config, monkeypatch,
):
    del stable_catalog_config
    _patch_lock(monkeypatch)
    calls = _mock_catalog_sync(monkeypatch)
    schedule = _set_schedule(refresh_db)
    old_due = datetime.utcnow() - timedelta(days=9)
    with refresh_db["app"].app_context():
        current = db.session.get(
            SupplierCatalogSchedule, refresh_db["supplier_id"],
        )
        current.next_due_at = old_due
        db.session.commit()
        scheduled_module = __import__(
            "services.supplier_catalog_refresh", fromlist=["_admit_due_schedule"],
        )
        admitted = scheduled_module._admit_due_schedule(
            refresh_db["supplier_id"], schedule["revision"],
        )
        assert admitted is not None
        run_id, admitted_due = admitted
        current = db.session.get(
            SupplierCatalogSchedule, refresh_db["supplier_id"],
        )
        advanced_due = current.next_due_at
        assert advanced_due > datetime.utcnow()
        assert advanced_due - datetime.utcnow() <= timedelta(hours=24)

        # Force the schedule due again while the first receipt remains active;
        # the supplier-scoped partial unique guard must still reject a second.
        current.next_due_at = old_due
        db.session.commit()
        duplicate = scheduled_module._admit_due_schedule(
            refresh_db["supplier_id"], schedule["revision"],
        )
        assert duplicate is None
        assert SupplierCatalogRefreshRun.query.filter_by(
            supplier_id=refresh_db["supplier_id"],
        ).count() == 1
        assert SupplierCatalogScheduledRun.query.count() == 1
        assert db.session.get(
            SupplierCatalogScheduledRun, run_id,
        ).due_at == old_due
        current = db.session.get(
            SupplierCatalogSchedule, refresh_db["supplier_id"],
        )
        current.next_due_at = advanced_due
        db.session.commit()

    # This test exercises admission directly; a later tick processes its one
    # pending receipt despite next_due_at already advancing.
    summary = run_catalog_refresh_tick(refresh_db["app"])
    assert summary["scheduled"] == 1
    assert len([call for call in calls if call.get("provider_started")]) == 1
    with refresh_db["app"].app_context():
        receipt = get_catalog_refresh(refresh_db["supplier_id"], run_id)
        assert receipt["due_at"] == old_due.isoformat()
        assert admitted_due == old_due


@pytest.mark.parametrize("crash", ["before_commit", "after_commit"])
def test_scheduled_running_receipt_is_interrupted_without_replay(
    refresh_db, stable_catalog_config, monkeypatch, crash,
):
    del stable_catalog_config
    _patch_lock(monkeypatch)
    calls = _mock_catalog_sync(monkeypatch, crash=crash)
    _set_schedule(refresh_db)

    with pytest.raises(SystemExit):
        run_catalog_refresh_tick(refresh_db["app"])
    with refresh_db["app"].app_context():
        run = SupplierCatalogRefreshRun.query.filter_by(
            supplier_id=refresh_db["supplier_id"],
        ).one()
        assert run.status == "running"
        before_raw = run.raw_committed_at
        assert (before_raw is not None) is (crash == "after_commit")

    sync = Mock(side_effect=AssertionError("interrupted scheduled work must not replay"))
    monkeypatch.setattr(SupplierService, "_sync_from_csv_locked", sync)
    recovered = run_catalog_refresh_tick(refresh_db["app"])
    assert recovered["interrupted"] == 1
    assert recovered["processed"] == 0
    sync.assert_not_called()
    with refresh_db["app"].app_context():
        run = SupplierCatalogRefreshRun.query.filter_by(
            supplier_id=refresh_db["supplier_id"],
        ).one()
        assert run.status == "interrupted"
        assert (run.raw_committed_at is not None) is (crash == "after_commit")
        assert db.session.get(
            SupplierCatalogSchedule, refresh_db["supplier_id"],
        ).revision == 1
    assert len([call for call in calls if call.get("provider_started")]) == 1


def test_schedule_revoked_after_fetch_fails_raw_checkpoint_and_skips_price(
    refresh_db, stable_catalog_config, monkeypatch,
):
    del stable_catalog_config
    _patch_lock(monkeypatch)
    calls = _mock_catalog_sync(monkeypatch, revoke_before_checkpoint=True)
    _set_schedule(refresh_db)

    summary = run_catalog_refresh_tick(refresh_db["app"])

    assert summary["processed"] == 1
    assert len([call for call in calls if call.get("provider_started")]) == 1
    with refresh_db["app"].app_context():
        run = SupplierCatalogRefreshRun.query.filter_by(
            supplier_id=refresh_db["supplier_id"],
        ).one()
        receipt = get_catalog_refresh(refresh_db["supplier_id"], run.id)
        assert run.raw_committed_at is None
        assert receipt["status"] == "failed"
        assert receipt["error_code"] == "scheduled_consent_revoked"
        assert receipt["price_status"] == "skipped"


@pytest.mark.parametrize(
    "change",
    [
        "disabled_schedule", "inactive_supplier", "blank_source",
        "malformed_source", "inactive_admin", "blocked_admin",
    ],
)
def test_due_schedule_skips_invalid_source_or_revoked_consent_without_provider_get(
    refresh_db, stable_catalog_config, monkeypatch, change,
):
    del stable_catalog_config
    _patch_lock(monkeypatch)
    sync = Mock(side_effect=AssertionError("ineligible schedule must not read source"))
    monkeypatch.setattr(SupplierService, "_sync_from_csv_locked", sync)
    _set_schedule(refresh_db)
    with refresh_db["app"].app_context():
        if change == "disabled_schedule":
            current = db.session.get(
                SupplierCatalogSchedule, refresh_db["supplier_id"],
            )
            set_catalog_schedule(
                supplier_id=refresh_db["supplier_id"],
                admin_user_id=refresh_db["admin_id"],
                enabled=False,
                interval_hours=current.interval_hours,
                expected_revision=current.revision,
            )
        elif change == "inactive_supplier":
            db.session.get(Supplier, refresh_db["supplier_id"]).is_active = False
        elif change == "blank_source":
            db.session.get(Supplier, refresh_db["supplier_id"]).csv_source_url = "  "
        elif change == "malformed_source":
            db.session.get(Supplier, refresh_db["supplier_id"]).csv_source_url = "file:///etc/passwd"
        else:
            admin = db.session.get(User, refresh_db["admin_id"])
            if change == "inactive_admin":
                admin.is_active = False
            else:
                admin.blocked_at = datetime.utcnow()
        db.session.commit()

    summary = run_catalog_refresh_tick(refresh_db["app"])
    assert summary["processed"] == 0
    assert summary["scheduled"] == 0
    sync.assert_not_called()
    with refresh_db["app"].app_context():
        assert SupplierCatalogRefreshRun.query.count() == 0


def test_disabling_schedule_does_not_mutate_an_already_running_receipt(
    refresh_db, stable_catalog_config,
):
    del stable_catalog_config
    schedule = _set_schedule(refresh_db)
    with refresh_db["app"].app_context():
        scheduled_module = __import__(
            "services.supplier_catalog_refresh", fromlist=["_admit_due_schedule"],
        )
        admitted = scheduled_module._admit_due_schedule(
            refresh_db["supplier_id"], schedule["revision"],
        )
        assert admitted is not None
        run_id, _due_at = admitted
        run = db.session.get(SupplierCatalogRefreshRun, run_id)
        run.status = "running"
        run.started_at = datetime.utcnow()
        run.raw_committed_at = datetime.utcnow() - timedelta(seconds=1)
        before = (run.status, run.started_at, run.raw_committed_at)
        db.session.commit()
        current_schedule = db.session.get(
            SupplierCatalogSchedule, refresh_db["supplier_id"],
        )
        old_due = current_schedule.next_due_at

        disabled = set_catalog_schedule(
            supplier_id=refresh_db["supplier_id"],
            admin_user_id=refresh_db["admin_id"],
            enabled=False,
            interval_hours=current_schedule.interval_hours,
            expected_revision=current_schedule.revision,
        )
        current_run = db.session.get(SupplierCatalogRefreshRun, run_id)
        assert disabled["enabled"] is False
        assert disabled["revision"] == schedule["revision"] + 1
        assert disabled["next_due_at"] == old_due.isoformat()
        assert (current_run.status, current_run.started_at, current_run.raw_committed_at) == before


def test_changed_schedule_generation_fails_pending_receipt_before_provider_get(
    refresh_db, stable_catalog_config, monkeypatch,
):
    del stable_catalog_config
    _patch_lock(monkeypatch)
    sync = Mock(side_effect=AssertionError("stale generation must not read source"))
    monkeypatch.setattr(SupplierService, "_sync_from_csv_locked", sync)
    schedule = _set_schedule(refresh_db)
    with refresh_db["app"].app_context():
        scheduled_module = __import__(
            "services.supplier_catalog_refresh", fromlist=["_admit_due_schedule"],
        )
        admitted = scheduled_module._admit_due_schedule(
            refresh_db["supplier_id"], schedule["revision"],
        )
        assert admitted is not None
        run_id, _due_at = admitted
        current = db.session.get(
            SupplierCatalogSchedule, refresh_db["supplier_id"],
        )
        current.revision += 1
        current.updated_at = datetime.utcnow()
        db.session.commit()

    summary = run_catalog_refresh_tick(refresh_db["app"])
    assert summary["processed"] == 0
    assert summary["failed"] == 1
    sync.assert_not_called()
    receipt = _read_receipt(refresh_db, run_id)
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "scheduled_consent_revoked"
    assert receipt["trigger_kind"] == "scheduled"
