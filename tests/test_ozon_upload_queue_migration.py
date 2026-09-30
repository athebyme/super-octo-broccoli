"""Empty Ozon upload queue DDL preserves historical jobs and write audit."""

import sqlite3

import pytest
from flask import Flask

from migrations.migrate_add_ozon_upload_queue import (
    ITEMS, RUNS, apply_migration, migrate,
)
from migrations.run_scoped_batch import run_batch
from models import db


def _parents(connection):
    connection.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sellers (id INTEGER PRIMARY KEY);
        CREATE TABLE seller_marketplace_accounts (id INTEGER PRIMARY KEY);
        CREATE TABLE background_jobs (
            id INTEGER PRIMARY KEY, seller_id INTEGER, progress_data TEXT
        );
        CREATE TABLE marketplace_operations (
            id INTEGER PRIMARY KEY, seller_id INTEGER, status TEXT
        );
        INSERT INTO users VALUES (1);
        INSERT INTO sellers VALUES (1);
        INSERT INTO sellers VALUES (2);
        INSERT INTO seller_marketplace_accounts VALUES (1);
        INSERT INTO background_jobs VALUES (1, 1, '{"legacy":"opaque"}');
        INSERT INTO background_jobs VALUES (2, 2, '{"legacy":"other"}');
        INSERT INTO marketplace_operations VALUES (1, 1, 'uncertain');
    """)


@pytest.fixture
def connection():
    value = sqlite3.connect(':memory:')
    value.execute('PRAGMA foreign_keys=ON')
    _parents(value)
    try:
        yield value
    finally:
        value.close()


def _run(connection, *, job_id=1, seller_id=1, request_key='a' * 64,
         mode='source_prepare', state='active', next_due_at='2026-09-27 00:00:00',
         lease_token=None, lease_until=None, parent_prepare_run_id=None):
    return connection.execute(f'''
        INSERT INTO {RUNS} (
            job_id,seller_id,account_id,mode,request_key_hash,
            request_fingerprint,created_by_user_id,parent_prepare_run_id,
            state,next_due_at,lease_token,lease_until,created_at,updated_at
        ) VALUES (?,?,1,?,?,?,1,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
    ''', (job_id, seller_id, mode, request_key, 'f' * 64,
          parent_prepare_run_id, state, next_due_at, lease_token, lease_until)).lastrowid


def _item(connection, run_id, *, ordinal=1, source_id=501, phase='pending',
          reviewed_draft_id=None, reviewed_version=None, draft_id=None,
          prepared_version=None, operation_id=None, failures=0):
    return connection.execute(f'''
        INSERT INTO {ITEMS} (
            run_id,ordinal,imported_product_id,reviewed_draft_id,
            reviewed_version,draft_id,prepared_version,operation_id,phase,
            local_failure_count,title_snapshot,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,'historical title',
                  CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
    ''', (run_id, ordinal, source_id, reviewed_draft_id, reviewed_version,
          draft_id, prepared_version, operation_id, phase, failures)).lastrowid


def test_empty_additive_repeat_and_legacy_rows_untouched(connection):
    jobs_before = connection.execute('SELECT * FROM background_jobs ORDER BY id').fetchall()
    operations_before = connection.execute('SELECT * FROM marketplace_operations').fetchall()
    assert apply_migration(connection, verbose=False) > 0
    assert apply_migration(connection, verbose=False) == 0
    assert connection.execute(f'SELECT count(*) FROM {RUNS}').fetchone() == (0,)
    assert connection.execute(f'SELECT count(*) FROM {ITEMS}').fetchone() == (0,)
    assert connection.execute('SELECT * FROM background_jobs ORDER BY id').fetchall() == jobs_before
    assert connection.execute('SELECT * FROM marketplace_operations').fetchall() == operations_before
    assert connection.execute('PRAGMA foreign_key_check').fetchall() == []


def test_model_created_schema_matches_migration_contract():
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI='sqlite://',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
        raw = db.engine.raw_connection()
        try:
            assert apply_migration(raw, verbose=False) == 0
        finally:
            raw.close()
            db.drop_all()


@pytest.mark.parametrize('run_changes', [
    {'mode': 'source_sync'},
    {'state': 'completed'},
    {'state': 'completed', 'next_due_at': '2026-09-27 00:00:00'},
    {'state': 'active', 'next_due_at': None},
    {'lease_token': 't' * 32},
    {'lease_until': '2026-09-27 00:00:00'},
    {'job_id': 999},
    {'parent_prepare_run_id': 999},
])
def test_run_guards_reject_invalid_state_lease_or_parent(connection, run_changes):
    apply_migration(connection, verbose=False)
    with pytest.raises(sqlite3.IntegrityError):
        _run(connection, **run_changes)


def test_run_request_key_is_unique_per_seller_and_job_one_to_one(connection):
    apply_migration(connection, verbose=False)
    first = _run(connection)
    assert first > 0
    assert connection.execute(
        f'SELECT mapping_preflight_at FROM {RUNS} WHERE id=?', (first,),
    ).fetchone() == (None,)
    connection.execute(
        f'UPDATE {RUNS} SET mapping_preflight_at=? WHERE id=?',
        ('2026-09-27 10:00:00', first),
    )
    assert connection.execute(
        f'SELECT mapping_preflight_at FROM {RUNS} WHERE id=?', (first,),
    ).fetchone() == ('2026-09-27 10:00:00',)
    with pytest.raises(sqlite3.IntegrityError):
        _run(connection, job_id=2)
    with pytest.raises(sqlite3.IntegrityError):
        _run(connection, request_key='b' * 64)
    assert _run(connection, job_id=2, seller_id=2) > first


@pytest.mark.parametrize('item_changes', [
    {'ordinal': 0},
    {'phase': 'source_sync'},
    {'phase': 'reviewed'},
    {'phase': 'prepared'},
    {'phase': 'operation_linked'},
    {'reviewed_draft_id': 10},
    {'reviewed_version': 1},
    {'reviewed_draft_id': 0, 'reviewed_version': 1},
    {'reviewed_draft_id': 10, 'reviewed_version': 0},
    {'failures': -1},
    {'failures': 4},
    {'operation_id': 999},
    {'run_id': 999},
])
def test_item_guards_reject_malformed_review_phase_failure_or_fk(connection, item_changes):
    apply_migration(connection, verbose=False)
    run_id = _run(connection)
    changes = dict(item_changes)
    with pytest.raises(sqlite3.IntegrityError):
        _item(connection, changes.pop('run_id', run_id), **changes)


def test_exact_operation_single_owner_logical_ids_and_audit_restrict(connection):
    apply_migration(connection, verbose=False)
    run_id = _run(connection)
    other = _run(connection, job_id=2, seller_id=2)
    linked = _item(
        connection, run_id, source_id=999999, phase='operation_linked',
        operation_id=1,
    )
    assert linked > 0  # Deleted source IDs remain historical logical snapshots.
    with pytest.raises(sqlite3.IntegrityError):
        _item(connection, other, source_id=999999, phase='operation_linked',
              operation_id=1)
    _item(connection, other, source_id=888888, phase='reviewed',
          reviewed_draft_id=777777, reviewed_version=5)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute('DELETE FROM marketplace_operations WHERE id=1')
    for table, ident in (
        ('background_jobs', 1), ('seller_marketplace_accounts', 1),
        ('sellers', 1), (RUNS, run_id),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f'DELETE FROM {table} WHERE id=?', (ident,))
    assert connection.execute(f'SELECT operation_id FROM {ITEMS} WHERE id=?',
                              (linked,)).fetchone() == (1,)
    assert connection.execute('PRAGMA foreign_key_check').fetchall() == []


def test_incompatible_existing_child_rolls_back_parent_creation(connection):
    connection.execute(f'CREATE TABLE {ITEMS} (id INTEGER PRIMARY KEY)')
    with pytest.raises(sqlite3.OperationalError, match='columns missing'):
        apply_migration(connection, verbose=False)
    assert connection.execute("SELECT name FROM sqlite_master WHERE name=?",
                              (RUNS,)).fetchone() is None
    assert connection.execute("SELECT sql FROM sqlite_master WHERE name=?",
                              (ITEMS,)).fetchone() == (
        f'CREATE TABLE {ITEMS} (id INTEGER PRIMARY KEY)',
    )


@pytest.mark.parametrize('defect', ['check', 'unique', 'fk'])
def test_incompatible_existing_constraint_fails_closed(connection, defect):
    apply_migration(connection, verbose=False)
    original = connection.execute(
        'SELECT sql FROM sqlite_master WHERE name=?', (RUNS,),
    ).fetchone()[0]
    connection.execute(f'DROP TABLE {ITEMS}')
    connection.execute(f'DROP TABLE {RUNS}')
    if defect == 'check':
        altered = original.replace(
            "CHECK (mode IN ('source_prepare','reviewed_drafts'))",
            "CHECK (mode IN ('source_sync','reviewed_drafts'))",
        )
    elif defect == 'unique':
        altered = original.replace(
            'UNIQUE (seller_id,request_key_hash)',
            'UNIQUE (seller_id,account_id,request_key_hash)',
        )
    else:
        altered = original.replace(
            'REFERENCES background_jobs(id) ON DELETE RESTRICT',
            'REFERENCES sellers(id) ON DELETE RESTRICT',
        )
    assert altered != original
    connection.execute(altered)
    with pytest.raises(sqlite3.OperationalError, match='incompatible'):
        apply_migration(connection, verbose=False)


def test_shadowed_named_index_fails_closed(connection):
    apply_migration(connection, verbose=False)
    connection.execute('DROP INDEX idx_ozon_bulk_run_due')
    connection.execute(f'CREATE INDEX idx_ozon_bulk_run_due ON {RUNS}(state)')
    with pytest.raises(sqlite3.OperationalError, match='index incompatible'):
        apply_migration(connection, verbose=False)


def test_missing_parent_or_database_fails_without_side_effect(tmp_path):
    missing = tmp_path / 'missing.sqlite'
    with pytest.raises(sqlite3.OperationalError, match='does not exist'):
        migrate(str(missing))
    assert not missing.exists()
    path = tmp_path / 'partial.sqlite'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE sellers (id INTEGER PRIMARY KEY)')
    with pytest.raises(sqlite3.OperationalError, match='prerequisite missing'):
        migrate(str(path))
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'ozon_bulk_upload_%'").fetchone() == (0,)


def test_scoped_batch_runner_registration_and_repeat(tmp_path):
    path = tmp_path / 'platform.sqlite'
    with sqlite3.connect(path) as connection:
        _parents(connection)
    step = 'migrations/migrate_add_ozon_upload_queue.py'
    run_batch(path, [step], verbose=False)
    run_batch(path, [step], verbose=False)
    with sqlite3.connect(path) as connection:
        assert connection.execute(f'SELECT count(*) FROM {RUNS}').fetchone() == (0,)
        assert connection.execute(f'SELECT count(*) FROM {ITEMS}').fetchone() == (0,)
