"""The write fence must survive upgrades without inventing operator decisions."""

import sqlite3

import pytest
from flask import Flask

from models import db
from migrations.migrate_add_marketplace_write_quarantine import (
    apply_migration,
    migrate,
)
from migrations.run_scoped_batch import run_batch


def _parents(connection):
    connection.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sellers (id INTEGER PRIMARY KEY);
        CREATE TABLE marketplaces (id INTEGER PRIMARY KEY);
        CREATE TABLE seller_marketplace_accounts (id INTEGER PRIMARY KEY);
        CREATE TABLE marketplace_operations (id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE marketplace_media_operations (id INTEGER PRIMARY KEY, status TEXT);
        INSERT INTO users VALUES (1);
        INSERT INTO sellers VALUES (1);
        INSERT INTO marketplaces VALUES (1);
        INSERT INTO seller_marketplace_accounts VALUES (1);
        INSERT INTO marketplace_operations VALUES (1, 'uncertain');
        INSERT INTO marketplace_media_operations VALUES (1, 'uncertain');
    """)


def _quarantine(connection, *, operation_id=1, media_operation_id=None,
                scope_kind='product', offer_id='offer-1', product_id='123',
                scope_reason='immutable_target_verified', status='active',
                released_at=None):
    return connection.execute("""
        INSERT INTO marketplace_write_quarantines (
            seller_id, marketplace_id, account_id, operation_id,
            media_operation_id, scope_kind, offer_id, product_id, scope_reason,
            reviewed_scope_token, status, version, created_at, updated_at,
            released_at
        ) VALUES (1, 1, 1, ?, ?, ?, ?, ?, ?, ?, ?, 1,
                  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)
    """, (operation_id, media_operation_id, scope_kind, offer_id, product_id,
          scope_reason, 'a' * 64, status, released_at)).lastrowid


def _event(connection, quarantine_id=1, *, action='placed', version=1,
           reason='Reviewed unknown provider outcome'):
    return connection.execute("""
        INSERT INTO marketplace_write_quarantine_events (
            quarantine_id, seller_id, marketplace_id, account_id,
            actor_user_id, action, reason, quarantine_version,
            operation_version, created_at
        ) VALUES (?, 1, 1, 1, 1, ?, ?, ?, 1, CURRENT_TIMESTAMP)
    """, (quarantine_id, action, reason, version))


@pytest.fixture
def connection():
    db_connection = sqlite3.connect(':memory:')
    db_connection.execute('PRAGMA foreign_keys=ON')
    _parents(db_connection)
    try:
        yield db_connection
    finally:
        db_connection.close()


def test_historical_operations_remain_untouched_and_repeat_is_noop(connection):
    before = list(connection.execute('SELECT * FROM marketplace_operations'))
    before_media = list(connection.execute('SELECT * FROM marketplace_media_operations'))
    first = apply_migration(connection, verbose=False)
    second = apply_migration(connection, verbose=False)

    assert first > 0
    assert second == 0
    assert list(connection.execute('SELECT * FROM marketplace_operations')) == before
    assert list(connection.execute('SELECT * FROM marketplace_media_operations')) == before_media
    assert connection.execute('SELECT count(*) FROM marketplace_write_quarantines').fetchone()[0] == 0
    assert connection.execute('SELECT count(*) FROM marketplace_write_quarantine_events').fetchone()[0] == 0
    assert list(connection.execute('PRAGMA foreign_key_check')) == []

    qid = _quarantine(connection)
    _event(connection, qid)
    assert apply_migration(connection, verbose=False) == 0
    assert connection.execute('SELECT count(*) FROM marketplace_write_quarantine_events').fetchone()[0] == 1


def test_orm_created_schema_is_accepted():
    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI='sqlite://', SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    with app.app_context():
        db.create_all()
        raw = db.engine.raw_connection()
        try:
            assert apply_migration(raw, verbose=False) == 0
        finally:
            raw.close()
            db.drop_all()


@pytest.mark.parametrize('changes', [
    {'operation_id': None, 'media_operation_id': None},
    {'operation_id': 1, 'media_operation_id': 1},
    {'scope_kind': 'account', 'offer_id': 'offer-1'},
    {'scope_kind': 'product', 'offer_id': None},
    {'scope_kind': 'product', 'product_id': 'bad-id'},
    {'scope_kind': 'product', 'product_id': '0123'},
    {'scope_reason': 'made_up'},
    {'status': 'released'},
    {'operation_id': 999},
])
def test_invalid_scope_origin_status_or_foreign_key_is_rejected(connection, changes):
    apply_migration(connection, verbose=False)
    with pytest.raises(sqlite3.IntegrityError):
        _quarantine(connection, **changes)


def test_unique_origins_event_versions_and_event_guards(connection):
    apply_migration(connection, verbose=False)
    qid = _quarantine(connection)
    _event(connection, qid)
    with pytest.raises(sqlite3.IntegrityError):
        _quarantine(connection)
    with pytest.raises(sqlite3.IntegrityError):
        _event(connection, qid, version=1)
    with pytest.raises(sqlite3.IntegrityError):
        _event(connection, qid, version=2, action='invented')
    with pytest.raises(sqlite3.IntegrityError):
        _event(connection, qid, version=2, reason='short')
    with pytest.raises(sqlite3.IntegrityError):
        _event(connection, 999)

    connection.execute('INSERT INTO marketplace_operations VALUES (2, \'uncertain\')')
    second = _quarantine(connection, operation_id=2, scope_kind='account',
                         offer_id=None, product_id=None, scope_reason='identity_unknown')
    assert second != qid
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute('DELETE FROM marketplace_operations WHERE id=1')
    assert connection.execute('SELECT count(*) FROM marketplace_write_quarantines').fetchone()[0] == 2

    media = _quarantine(connection, operation_id=None, media_operation_id=1,
                        scope_kind='account', offer_id=None, product_id=None,
                        scope_reason='identity_unknown')
    assert media != qid
    with pytest.raises(sqlite3.IntegrityError):
        _quarantine(connection, operation_id=None, media_operation_id=1,
                    scope_kind='account', offer_id=None, product_id=None,
                    scope_reason='identity_unknown')


def test_missing_prerequisite_fails_without_creating_tables(tmp_path):
    path = tmp_path / 'platform.sqlite'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE sellers (id INTEGER PRIMARY KEY)')
    with pytest.raises(sqlite3.OperationalError, match='prerequisite missing'):
        migrate(str(path))
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'marketplace_write_quarantine%'").fetchone()[0] == 0


def test_scoped_batch_runner_accepts_quarantine_profile(tmp_path):
    path = tmp_path / 'platform.sqlite'
    with sqlite3.connect(path) as connection:
        _parents(connection)
    step = 'migrations/migrate_add_marketplace_write_quarantine.py'
    run_batch(path, [step], verbose=False)
    run_batch(path, [step], verbose=False)
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT count(*) FROM marketplace_write_quarantines').fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM marketplace_write_quarantine_events').fetchone()[0] == 0


def test_interrupted_step_rolls_back_both_tables(connection):
    # Simulate a preexisting, incompatible event table. Creating the parent
    # quarantine table must be undone when validating the child fails.
    connection.execute('CREATE TABLE marketplace_write_quarantine_events (id INTEGER PRIMARY KEY)')
    with pytest.raises(sqlite3.OperationalError, match='columns incompatible'):
        apply_migration(connection, verbose=False)
    assert connection.execute("SELECT count(*) FROM sqlite_master WHERE name='marketplace_write_quarantines'").fetchone()[0] == 0
    assert connection.execute("SELECT sql FROM sqlite_master WHERE name='marketplace_write_quarantine_events'").fetchone()[0] == 'CREATE TABLE marketplace_write_quarantine_events (id INTEGER PRIMARY KEY)'


def test_failure_keeps_caller_transaction_and_prior_work(connection):
    connection.commit()
    connection.execute('CREATE TABLE marketplace_write_quarantine_events (id INTEGER PRIMARY KEY)')
    connection.commit()
    connection.execute('INSERT INTO marketplace_operations VALUES (2, \'uncertain\')')
    assert connection.in_transaction
    with pytest.raises(sqlite3.OperationalError):
        apply_migration(connection, verbose=False)
    assert connection.in_transaction
    assert connection.execute('SELECT status FROM marketplace_operations WHERE id=2').fetchone() == ('uncertain',)
    connection.commit()
    assert connection.execute('SELECT status FROM marketplace_operations WHERE id=2').fetchone() == ('uncertain',)


def test_shadowed_composite_index_fails_closed(connection):
    apply_migration(connection, verbose=False)
    connection.execute('DROP INDEX idx_write_quarantine_offer')
    connection.execute('CREATE INDEX idx_write_quarantine_offer ON marketplace_write_quarantines(seller_id)')
    with pytest.raises(sqlite3.OperationalError, match='index incompatible'):
        apply_migration(connection, verbose=False)
    assert tuple(row[2] for row in connection.execute('PRAGMA index_info(idx_write_quarantine_offer)')) == ('seller_id',)


@pytest.mark.parametrize('defect', ['check', 'unique', 'foreign_key'])
def test_incompatible_existing_constraints_fail_closed(connection, defect):
    apply_migration(connection, verbose=False)
    original = connection.execute("SELECT sql FROM sqlite_master WHERE name='marketplace_write_quarantines'").fetchone()[0]
    connection.execute('DROP TABLE marketplace_write_quarantine_events')
    connection.execute('DROP TABLE marketplace_write_quarantines')
    if defect == 'check':
        changed = original.replace('CHECK (version > 0)', 'CHECK (version >= 0)')
    elif defect == 'unique':
        changed = original.replace('UNIQUE (operation_id)', 'UNIQUE (seller_id, operation_id)')
    else:
        changed = original.replace('REFERENCES marketplace_operations(id)', 'REFERENCES sellers(id)')
    assert changed != original
    connection.execute(changed)
    with pytest.raises(sqlite3.OperationalError, match='incompatible|missing'):
        apply_migration(connection, verbose=False)
