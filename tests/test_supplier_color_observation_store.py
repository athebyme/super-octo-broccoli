from datetime import datetime, timedelta, timezone
import hashlib
import os
import sqlite3
import stat
from unittest.mock import patch

from flask import Flask

from services.supplier_color_observation_store import (
    MAX_OBSERVATIONS,
    SupplierColorObservationStore,
    configured_supplier_color_observation_store,
)


CONFIG_A = "a" * 64
CONFIG_B = "b" * 64
EXTERNAL_HASH_A = hashlib.sha256(b"supplier-sku-a").hexdigest()
EXTERNAL_HASH_B = hashlib.sha256(b"supplier-sku-b").hexdigest()


def _at(hours=0, *, minutes=0):
    return datetime(2026, 11, 23, tzinfo=timezone.utc) + timedelta(
        hours=hours, minutes=minutes,
    )


def _observation(revision=3, external_hash=EXTERNAL_HASH_A, colors=("rose / gold",)):
    return {41: (revision, external_hash, colors)}


def test_configured_store_is_sibling_and_get_does_not_create_file(tmp_path):
    app_db = tmp_path / "seller_platform.db"
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{app_db}"

    with app.app_context():
        store = configured_supplier_color_observation_store()

    assert store is not None
    assert store.path.parent == app_db.parent
    assert store.path.name == app_db.name + ".supplier-color-observations.sqlite3"
    assert store.get(
        9, 41, 3, EXTERNAL_HASH_A, CONFIG_A, now=_at(),
    ) is None
    assert not store.path.exists()

    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    with app.app_context():
        assert configured_supplier_color_observation_store() is None


def test_reservation_is_durable_and_config_changes_do_not_bypass_cooldown(tmp_path):
    path = tmp_path / "private.sqlite3"
    store = SupplierColorObservationStore(path)

    first = store.reserve(9, CONFIG_A, now=_at())
    assert isinstance(first, str) and len(first) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    # A new object models a restarted worker. No result is needed to preserve
    # the physical reservation and its cooldown.
    restarted = SupplierColorObservationStore(path)
    assert restarted.reserve(9, CONFIG_B, now=_at(hours=23, minutes=59)) is None
    second = restarted.reserve(9, CONFIG_B, now=_at(hours=24))
    assert isinstance(second, str) and second != first

    # Clock rollback cannot bypass the reservation interval.
    assert restarted.reserve(9, CONFIG_A, now=_at(hours=23)) is None


def test_naive_datetimes_are_interpreted_as_utc(tmp_path):
    store = SupplierColorObservationStore(tmp_path / "private.sqlite3")
    token = store.reserve(9, CONFIG_A, now=datetime(2026, 11, 23, 0, 0))
    assert token
    assert store.finish(
        9, token, CONFIG_A,
        _observation(),
        observed_at=datetime(2026, 11, 23, 0, 1),
    )
    assert store.get(
        9, 41, 3, EXTERNAL_HASH_A, CONFIG_A, now=_at(minutes=1),
    ) == ["rose / gold"]


def test_finish_and_get_require_exact_supplier_product_revision_hash_and_config(tmp_path):
    store = SupplierColorObservationStore(tmp_path / "private.sqlite3")
    token = store.reserve(9, CONFIG_A, now=_at())
    assert token
    colors = ("rose / gold", "Сине-зелёный")
    assert store.finish(
        9, token, CONFIG_A,
        {41: (3, EXTERNAL_HASH_A, colors)},
        observed_at=_at(minutes=1),
    )

    fresh_at = _at(hours=36, minutes=1)
    assert store.get(9, 41, 3, EXTERNAL_HASH_A, CONFIG_A, now=fresh_at) == list(colors)
    assert store.get(9, 41, 3, EXTERNAL_HASH_A, CONFIG_A, now=_at(hours=36, minutes=2)) is None
    assert store.get(9, 41, 3, EXTERNAL_HASH_A, CONFIG_A, now=_at()) is None

    assert store.get(9, 41, 4, EXTERNAL_HASH_A, CONFIG_A, now=fresh_at) is None
    assert store.get(9, 41, 3, EXTERNAL_HASH_B, CONFIG_A, now=fresh_at) is None
    assert store.get(9, 42, 3, EXTERNAL_HASH_A, CONFIG_A, now=fresh_at) is None
    assert store.get(10, 41, 3, EXTERNAL_HASH_A, CONFIG_A, now=fresh_at) is None
    assert store.get(9, 41, 3, EXTERNAL_HASH_A, CONFIG_B, now=fresh_at) is None


def test_replacement_is_atomic_supplier_scoped_and_empty_success_clears_only_its_rows(tmp_path):
    store = SupplierColorObservationStore(tmp_path / "private.sqlite3")
    supplier_a_token = store.reserve(9, CONFIG_A, now=_at())
    supplier_b_token = store.reserve(10, CONFIG_A, now=_at())
    assert supplier_a_token and supplier_b_token
    assert store.finish(
        9, supplier_a_token, CONFIG_A,
        {41: (3, EXTERNAL_HASH_A, ("old color",))},
        observed_at=_at(minutes=1),
    )
    assert store.finish(
        10, supplier_b_token, CONFIG_A,
        {51: (6, EXTERNAL_HASH_B, ("supplier b color",))},
        observed_at=_at(minutes=1),
    )

    # A new configuration hides the old snapshot before any replacement is
    # accepted, but a failed finish leaves the previous snapshot intact.
    replacement = store.reserve(9, CONFIG_B, now=_at(hours=24))
    assert replacement
    assert store.get(9, 41, 3, EXTERNAL_HASH_A, CONFIG_B, now=_at(hours=24)) is None
    assert store.get(9, 41, 3, EXTERNAL_HASH_A, CONFIG_A, now=_at(hours=24)) == ["old color"]
    assert not store.finish(
        9, replacement, CONFIG_A,
        {41: (4, EXTERNAL_HASH_B, ("unvalidated",))},
        observed_at=_at(hours=24, minutes=1),
    )
    assert store.get(9, 41, 3, EXTERNAL_HASH_A, CONFIG_A, now=_at(hours=24)) == ["old color"]
    assert store.get(10, 51, 6, EXTERNAL_HASH_B, CONFIG_A, now=_at(hours=24)) == [
        "supplier b color",
    ]

    # A validated empty snapshot is still a successful replacement and clears
    # only the selected supplier's prior rows.
    assert store.finish(
        9, replacement, CONFIG_B, {}, observed_at=_at(hours=24, minutes=1),
    )
    assert store.get(9, 41, 3, EXTERNAL_HASH_A, CONFIG_A, now=_at(hours=24, minutes=2)) is None
    assert store.get(9, 41, 3, EXTERNAL_HASH_A, CONFIG_B, now=_at(hours=24, minutes=2)) is None
    assert store.get(10, 51, 6, EXTERNAL_HASH_B, CONFIG_A, now=_at(hours=24, minutes=2)) == [
        "supplier b color",
    ]


def test_consumed_or_late_reservation_token_cannot_replace_snapshot(tmp_path):
    store = SupplierColorObservationStore(tmp_path / "private.sqlite3")
    first = store.reserve(9, CONFIG_A, now=_at())
    assert first
    assert store.finish(
        9, first, CONFIG_A,
        {41: (3, EXTERNAL_HASH_A, ("first",))},
        observed_at=_at(minutes=1),
    )
    assert not store.finish(
        9, first, CONFIG_A,
        {41: (4, EXTERNAL_HASH_B, ("duplicate",))},
        observed_at=_at(minutes=2),
    )

    second = store.reserve(9, CONFIG_A, now=_at(hours=24))
    assert second
    assert not store.finish(
        9, first, CONFIG_A,
        {41: (5, EXTERNAL_HASH_B, ("late",))},
        observed_at=_at(hours=24, minutes=1),
    )
    assert store.get(9, 41, 3, EXTERNAL_HASH_A, CONFIG_A, now=_at(hours=24)) == ["first"]
    assert store.finish(
        9, second, CONFIG_A,
        {41: (5, EXTERNAL_HASH_B, ("second",))},
        observed_at=_at(hours=24, minutes=1),
    )
    assert store.get(9, 41, 5, EXTERNAL_HASH_B, CONFIG_A, now=_at(hours=24, minutes=2)) == [
        "second",
    ]


def test_finish_rejects_physical_observation_at_24_hour_boundary(tmp_path):
    store = SupplierColorObservationStore(tmp_path / "private.sqlite3")
    token = store.reserve(9, CONFIG_A, now=_at())
    assert token

    assert not store.finish(
        9, token, CONFIG_A,
        _observation(),
        observed_at=_at(hours=24),
    )
    assert store.get(9, 41, 3, EXTERNAL_HASH_A, CONFIG_A, now=_at(hours=24)) is None


def test_connect_closes_connection_when_pragma_setup_fails(tmp_path):
    class BrokenConnection:
        closed = False
        calls = 0

        def execute(self, _statement):
            self.calls += 1
            if self.calls == 1:
                return None
            raise sqlite3.OperationalError("synthetic pragma failure")

        def close(self):
            self.closed = True

    broken = BrokenConnection()
    store = SupplierColorObservationStore(tmp_path / "private.sqlite3")
    with patch(
        "services.supplier_color_observation_store.sqlite3.connect",
        return_value=broken,
    ):
        assert store._connect(write=True, create=True) is None
    assert broken.calls == 2
    assert broken.closed


def test_reservation_rejects_unsafe_parent_directory(tmp_path):
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    unsafe.chmod(0o777)
    path = unsafe / "private.sqlite3"

    assert SupplierColorObservationStore(path).reserve(
        9, CONFIG_A, now=_at(),
    ) is None
    assert not path.exists()


def test_invalid_or_backward_observation_does_not_replace_previous_success(tmp_path):
    store = SupplierColorObservationStore(tmp_path / "private.sqlite3")
    token = store.reserve(9, CONFIG_A, now=_at())
    assert token
    assert store.finish(
        9, token, CONFIG_A,
        {41: (3, EXTERNAL_HASH_A, ("valid",))},
        observed_at=_at(minutes=1),
    )
    replacement = store.reserve(9, CONFIG_A, now=_at(hours=24))
    assert replacement

    assert not store.finish(
        9, replacement, CONFIG_A,
        {41: (4, EXTERNAL_HASH_B, ("before reservation",))},
        observed_at=_at(hours=23, minutes=59),
    )
    assert not store.finish(
        9, replacement, CONFIG_A,
        {41: (4, EXTERNAL_HASH_B, ("duplicate", "DUPLICATE"))},
        observed_at=_at(hours=24, minutes=1),
    )
    assert not store.finish(
        9, replacement, CONFIG_A,
        {41: (4, "not-a-sha256", ("invalid digest",))},
        observed_at=_at(hours=24, minutes=1),
    )
    too_many = {
        product_id: (1, EXTERNAL_HASH_A, ("red",))
        for product_id in range(1, MAX_OBSERVATIONS + 2)
    }
    assert not store.finish(
        9, replacement, CONFIG_A, too_many,
        observed_at=_at(hours=24, minutes=1),
    )
    broad_colors = tuple(f"{index:02d}" + "x" * 198 for index in range(20))
    oversized = {
        product_id: (1, EXTERNAL_HASH_A, broad_colors)
        for product_id in range(1, 2_200)
    }
    assert not store.finish(
        9, replacement, CONFIG_A, oversized,
        observed_at=_at(hours=24, minutes=1),
    )
    assert store.get(9, 41, 3, EXTERNAL_HASH_A, CONFIG_A, now=_at(hours=24)) == ["valid"]


def test_store_file_is_private_and_reader_fails_closed_on_wrong_permissions(tmp_path):
    path = tmp_path / "private.sqlite3"
    store = SupplierColorObservationStore(path)
    token = store.reserve(9, CONFIG_A, now=_at())
    assert token
    assert store.finish(
        9, token, CONFIG_A,
        {41: (3, EXTERNAL_HASH_A, ("private color",))},
        observed_at=_at(minutes=1),
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    path.chmod(0o644)
    assert store.get(9, 41, 3, EXTERNAL_HASH_A, CONFIG_A, now=_at(minutes=2)) is None
    assert stat.S_IMODE(path.stat().st_mode) == 0o644

    if hasattr(os, "O_NOFOLLOW"):
        target = tmp_path / "target.sqlite3"
        target_store = SupplierColorObservationStore(target)
        target_token = target_store.reserve(10, CONFIG_A, now=_at())
        assert target_token
        assert target_store.finish(
            10, target_token, CONFIG_A,
            {51: (1, EXTERNAL_HASH_B, ("other",))},
            observed_at=_at(minutes=1),
        )
        symlink = tmp_path / "alias.sqlite3"
        symlink.symlink_to(target)
        assert SupplierColorObservationStore(symlink).get(
            10, 51, 1, EXTERNAL_HASH_B, CONFIG_A, now=_at(minutes=2),
        ) is None


def test_store_contains_only_internal_ids_hashes_and_color_literals(tmp_path):
    path = tmp_path / "private.sqlite3"
    external_id = "raw-external-id-must-not-be-stored"
    external_hash = hashlib.sha256(external_id.encode("utf-8")).hexdigest()
    store = SupplierColorObservationStore(path)
    token = store.reserve(9, CONFIG_A, now=_at())
    assert token
    assert store.finish(
        9, token, CONFIG_A,
        {41: (3, external_hash, ("raw color literal",))},
        observed_at=_at(minutes=1),
    )
    raw = path.read_bytes()
    assert external_id.encode("utf-8") not in raw
    assert b"https://" not in raw
    assert b"api_key" not in raw
    assert b"raw color literal" in raw


def test_read_status_on_missing_store_is_read_only(tmp_path):
    path = tmp_path / "private.sqlite3"
    store = SupplierColorObservationStore(path)

    assert store.read_status(9, CONFIG_A, now=_at()) is None
    assert not path.exists()


def test_success_receipt_and_snapshot_metadata_are_atomic_and_bounded(tmp_path):
    path = tmp_path / "private.sqlite3"
    store = SupplierColorObservationStore(path)
    token = store.reserve(9, CONFIG_A, now=_at())
    assert token

    before = store.read_status(9, CONFIG_A, now=_at(minutes=1))
    assert before["last_attempt"]["status"] == "unconfirmed"
    assert before["last_attempt"]["outcome"] is None
    assert before["last_snapshot"]["status"] == "missing"

    assert store.finish(
        9, token, CONFIG_A, _observation(),
        observed_at=_at(minutes=1), fetch_attempted=True,
    )
    status = store.read_status(9, CONFIG_A, now=_at(minutes=2))
    assert status == {
        "last_attempt": {
            "status": "succeeded",
            "outcome": "snapshot_saved",
            "fetch_attempted": True,
            "reserved_at": "2026-11-23T00:00:00+00:00",
            "finished_at": "2026-11-23T00:01:00+00:00",
            "cooldown_remaining_seconds": 86_280,
            "config_matches": True,
        },
        "last_snapshot": {
            "status": "fresh",
            "at": "2026-11-23T00:01:00+00:00",
            "age_seconds": 60,
            "observation_count": 1,
            "fresh": True,
            "config_matches": True,
        },
    }
    assert not store.finish(
        9, token, CONFIG_A, _observation(), observed_at=_at(minutes=3),
    )

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM attempt_results",
        ).fetchone() == (1,)
    finally:
        connection.close()


def test_failure_receipt_preserves_prior_snapshot_and_24_hour_reservation(tmp_path):
    store = SupplierColorObservationStore(tmp_path / "private.sqlite3")
    first = store.reserve(9, CONFIG_A, now=_at())
    assert first
    assert store.finish(
        9, first, CONFIG_A, _observation(colors=("old color",)),
        observed_at=_at(minutes=1), fetch_attempted=True,
    )

    second = store.reserve(9, CONFIG_A, now=_at(hours=24))
    assert second and second != first
    assert store.finish_failure(
        9, second, CONFIG_A,
        outcome="feed_fetch_failed",
        fetch_attempted=True,
        finished_at=_at(hours=24, minutes=2),
    )
    assert not store.finish_failure(
        9, second, CONFIG_A,
        outcome="feed_fetch_failed",
        fetch_attempted=True,
        finished_at=_at(hours=24, minutes=3),
    )
    # A terminal failure consumes the exact token but does not replace the
    # previous successful observation or move the physical reservation time.
    assert not store.finish(
        9, second, CONFIG_A, _observation(colors=("bad replacement",)),
        observed_at=_at(hours=24, minutes=3),
    )
    assert store.get(
        9, 41, 3, EXTERNAL_HASH_A, CONFIG_A, now=_at(hours=24, minutes=3),
    ) == ["old color"]
    status = store.read_status(9, CONFIG_A, now=_at(hours=24, minutes=3))
    assert status["last_attempt"]["status"] == "failed"
    assert status["last_attempt"]["outcome"] == "feed_fetch_failed"
    assert status["last_attempt"]["fetch_attempted"] is True
    assert status["last_attempt"]["cooldown_remaining_seconds"] == 86_220
    assert status["last_snapshot"]["status"] == "fresh"
    assert status["last_snapshot"]["observation_count"] == 1
    assert store.reserve(9, CONFIG_B, now=_at(hours=47, minutes=59)) is None
    assert store.reserve(9, CONFIG_B, now=_at(hours=48))


def test_empty_success_records_no_read_and_fresh_empty_snapshot(tmp_path):
    store = SupplierColorObservationStore(tmp_path / "private.sqlite3")
    token = store.reserve(9, CONFIG_A, now=_at())
    assert token
    assert store.finish(
        9, token, CONFIG_A, {},
        observed_at=_at(minutes=2), fetch_attempted=False,
    )

    status = store.read_status(9, CONFIG_A, now=_at(minutes=3))
    assert status["last_attempt"]["status"] == "succeeded"
    assert status["last_attempt"]["outcome"] == "cleared_without_read"
    assert status["last_attempt"]["fetch_attempted"] is False
    assert status["last_snapshot"]["status"] == "empty"
    assert status["last_snapshot"]["fresh"] is True
    assert status["last_snapshot"]["observation_count"] == 0


def test_cleared_without_read_requires_an_empty_replacement(tmp_path):
    path = tmp_path / "private.sqlite3"
    store = SupplierColorObservationStore(path)
    token = store.reserve(9, CONFIG_A, now=_at())
    assert token
    assert not store.finish(
        9, token, CONFIG_A, _observation(),
        observed_at=_at(minutes=1), fetch_attempted=False,
    )
    assert store.read_status(9, CONFIG_A, now=_at(minutes=1))[
        "last_attempt"]["status"] == "unconfirmed"

    assert store.finish(
        9, token, CONFIG_A, {},
        observed_at=_at(minutes=2), fetch_attempted=False,
    )
    connection = sqlite3.connect(path)
    try:
        # Simulate a contradictory receipt/batch state. The read-side check
        # must not report cleared_without_read as a confirmed success.
        connection.execute(
            "UPDATE observation_batches SET row_count = 1 WHERE supplier_id = ?",
            (9,),
        )
        connection.commit()
    finally:
        connection.close()
    status = store.read_status(9, CONFIG_A, now=_at(minutes=3))
    assert status["last_attempt"]["status"] == "unconfirmed"
    assert status["last_attempt"]["outcome"] is None


def test_legacy_v1_receipts_remain_readable_and_read_status_does_not_migrate(tmp_path):
    path = tmp_path / "private.sqlite3"
    store = SupplierColorObservationStore(path)
    token = store.reserve(9, CONFIG_A, now=_at())
    assert token
    # Omitted fetch_attempted preserves the original V1 direct-caller contract.
    assert store.finish(
        9, token, CONFIG_A, _observation(), observed_at=_at(minutes=1),
    )

    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE attempt_results")
        connection.commit()
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
    finally:
        connection.close()

    assert store.get(
        9, 41, 3, EXTERNAL_HASH_A, CONFIG_A, now=_at(minutes=2),
    ) == ["rose / gold"]
    status = store.read_status(9, CONFIG_A, now=_at(minutes=2))
    assert status["last_attempt"]["status"] == "legacy_finished"
    assert status["last_attempt"]["outcome"] is None
    assert status["last_attempt"]["fetch_attempted"] is None
    assert status["last_attempt"]["finished_at"] is None
    assert status["last_snapshot"]["status"] == "fresh"

    connection = sqlite3.connect(path)
    try:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'",
            )
        }
        assert "attempt_results" not in tables
    finally:
        connection.close()

    # The first later write adds only the optional table; V1 base rows/TTL stay.
    next_token = store.reserve(9, CONFIG_A, now=_at(hours=24))
    assert next_token
    status = store.read_status(9, CONFIG_A, now=_at(hours=24, minutes=1))
    assert status["last_attempt"]["status"] == "unconfirmed"
    assert store.get(
        9, 41, 3, EXTERNAL_HASH_A, CONFIG_A, now=_at(hours=24, minutes=1),
    ) == ["rose / gold"]


def test_failure_receipt_validates_allowlist_clock_and_exact_digest(tmp_path):
    store = SupplierColorObservationStore(tmp_path / "private.sqlite3")
    token = store.reserve(9, CONFIG_A, now=_at())
    assert token
    assert not store.finish_failure(
        9, token, CONFIG_A,
        outcome="arbitrary error text",
        fetch_attempted=True,
        finished_at=_at(minutes=1),
    )
    assert not store.finish_failure(
        9, token, CONFIG_B,
        outcome="feed_fetch_failed",
        fetch_attempted=True,
        finished_at=_at(minutes=1),
    )
    assert not store.finish_failure(
        9, token, CONFIG_A,
        outcome="feed_fetch_failed",
        fetch_attempted=1,
        finished_at=_at(minutes=1),
    )
    assert not store.finish_failure(
        9, token, CONFIG_A,
        outcome="feed_fetch_failed",
        fetch_attempted=True,
        finished_at=_at(hours=24),
    )
    assert not store.finish_failure(
        9, token, CONFIG_A,
        outcome="feed_fetch_failed",
        fetch_attempted=True,
        finished_at=_at(minutes=-1),
    )
    status = store.read_status(9, CONFIG_A, now=_at(minutes=1))
    assert status["last_attempt"]["status"] == "unconfirmed"
