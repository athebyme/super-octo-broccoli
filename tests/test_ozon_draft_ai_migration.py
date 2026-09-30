"""Empty, additive draft-AI and shared-call ledger schema contract."""

import sqlite3

import pytest
from flask import Flask

from migrations.migrate_add_ozon_draft_ai_completion import (
    ATTEMPTS, ITEMS, REVIEWS, RUNS, SUGGESTIONS, apply_migration,
)
from migrations.run_scoped_batch import run_batch
from models import db


@pytest.fixture
def connection():
    value = sqlite3.connect(':memory:')
    value.execute('PRAGMA foreign_keys=ON')
    value.executescript('''
        CREATE TABLE sellers (id INTEGER PRIMARY KEY);
        CREATE TABLE seller_marketplace_accounts (id INTEGER PRIMARY KEY);
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE background_jobs (id INTEGER PRIMARY KEY, result_data TEXT);
        CREATE TABLE marketplace_product_drafts (id INTEGER PRIMARY KEY, version INTEGER);
        INSERT INTO sellers VALUES (1),(2);
        INSERT INTO seller_marketplace_accounts VALUES (1);
        INSERT INTO users VALUES (1);
        INSERT INTO background_jobs VALUES (1,'{"old":"opaque"}'),(2,'{}');
        INSERT INTO marketplace_product_drafts VALUES (1,4),(2,8);
    ''')
    try:
        yield value
    finally:
        value.close()


def _run(connection, *, job_id=1, seller_id=1, key='a' * 64,
         status='pending', count=1, max_calls=6, requested=0):
    return connection.execute(f'''
        INSERT INTO {RUNS} (
            job_id,seller_id,account_id,actor_user_id,request_key_hash,
            request_fingerprint,profile_version,model,status,next_due_at,
            item_count,max_calls,requested_calls,created_at,updated_at
        ) VALUES (?,?,1,1,?,'f' || substr(?,2),'seller-draft-v1',
                  'deepseek-flash',?,'2026-09-27 12:00:00',?,?,?,
                  CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
    ''', (job_id, seller_id, key, 'f' * 64, status, count,
          max_calls, requested)).lastrowid


def _item(connection, run_id, *, draft_id=1, ordinal=1,
          status='pending', source_hash='s' * 64, type_hash='t' * 64,
          dictionary_hash='d' * 64, filled_hash='f' * 64,
          source_kind='imported', type_id=401, call_id=None,
          lease_token=None, lease_until=None, seller_id=1):
    return connection.execute(f'''
        INSERT INTO {ITEMS} (
            run_id,ordinal,seller_id,account_id,draft_id,
            imported_product_id,product_type_id,expected_draft_version,
            source_kind,source_hash,type_schema_hash,dictionary_hash,
            filled_slots_hash,status,call_id,lease_token,lease_until,
            created_at,updated_at
        ) VALUES (?,?,?,1,?,1001,?,4,?,?,?,?,?,?,?,?,?,
                  CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
    ''', (run_id, ordinal, seller_id, draft_id, type_id, source_kind,
          source_hash, type_hash, dictionary_hash, filled_hash,
          status, call_id, lease_token, lease_until)).lastrowid


def test_additive_empty_repeat_and_legacy_job_untouched(connection):
    before = connection.execute('SELECT * FROM background_jobs').fetchall()
    assert apply_migration(connection, verbose=False) > 0
    assert apply_migration(connection, verbose=False) == 0
    assert connection.execute('SELECT * FROM background_jobs').fetchall() == before
    for table in (RUNS, ITEMS, SUGGESTIONS, REVIEWS, ATTEMPTS):
        assert connection.execute(f'SELECT count(*) FROM {table}').fetchone() == (0,)
    assert connection.execute('PRAGMA foreign_key_check').fetchall() == []


def test_model_created_schema_is_accepted_without_rebuild():
    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI='sqlite://',
                      SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    with app.app_context():
        db.create_all()
        raw = db.engine.raw_connection()
        try:
            assert apply_migration(raw, verbose=False) == 0
        finally:
            raw.close()
            db.drop_all()


def test_run_key_job_and_counts_enforced(connection):
    apply_migration(connection, verbose=False)
    first = _run(connection)
    assert first > 0
    with pytest.raises(sqlite3.IntegrityError):
        _run(connection, job_id=2)
    with pytest.raises(sqlite3.IntegrityError):
        _run(connection, key='b' * 64)
    for changes in ({'count': 201}, {'max_calls': 81},
                    {'max_calls': 2, 'requested': 3},
                    {'status': 'sent'}):
        with pytest.raises(sqlite3.IntegrityError):
            _run(connection, job_id=2, key='b' * 64, **changes)


def test_blocked_item_can_have_missing_seal_but_pending_cannot(connection):
    apply_migration(connection, verbose=False)
    run_id = _run(connection)
    blocked = _item(connection, run_id, status='needs_input',
                    source_hash=None, source_kind=None, type_id=None)
    assert blocked > 0
    with pytest.raises(sqlite3.IntegrityError):
        _item(connection, run_id, draft_id=2, ordinal=2, source_hash=None)
    with pytest.raises(sqlite3.IntegrityError):
        _item(connection, run_id, draft_id=2, ordinal=2,
              status='reserved', call_id=None)


def test_exact_active_draft_and_suggestion_slot_uniqueness(connection):
    apply_migration(connection, verbose=False)
    first_run = _run(connection)
    second_run = _run(connection, job_id=2, key='b' * 64)
    first = _item(connection, first_run)
    with pytest.raises(sqlite3.IntegrityError):
        _item(connection, second_run)
    connection.execute(f"UPDATE {ITEMS} SET status='proposed' WHERE id=?", (first,))
    second = _item(connection, second_run)
    assert second > first
    suggestion = lambda item_id, complex_id='0', group=0: connection.execute(f'''
        INSERT INTO {SUGGESTIONS} (
            item_id,attribute_id,complex_id,group_ordinal,values_json,
            evidence_json,provenance_code,status,created_at,updated_at
        ) VALUES (?,'322',?,?,'[{{"value":"synthetic"}}]','[]',
                  'literal_source','proposed',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
    ''', (item_id, complex_id, group))
    suggestion(first)
    with pytest.raises(sqlite3.IntegrityError):
        suggestion(first)
    suggestion(first, complex_id='12', group=1)


def test_review_request_and_audit_foreign_keys(connection):
    apply_migration(connection, verbose=False)
    run_id = _run(connection)
    item_id = _item(connection, run_id)
    def review(key, *, action='apply', version_after=5):
        return connection.execute(f'''
            INSERT INTO {REVIEWS} (
                seller_id,account_id,draft_id,item_id,actor_user_id,
                request_key_hash,request_fingerprint,action,version_before,
                version_after,selected_ids_json,created_at
            ) VALUES (1,1,1,1,1,?,'f' || substr(?,2),?,4,?,'[1]',CURRENT_TIMESTAMP)
        ''', (key, 'f' * 64, action, version_after)).lastrowid
    assert review('r' * 64) > 0
    with pytest.raises(sqlite3.IntegrityError):
        review('r' * 64)
    with pytest.raises(sqlite3.IntegrityError):
        review('q' * 64, version_after=4)
    for table, ident in ((RUNS, run_id), (ITEMS, item_id),
                         ('marketplace_product_drafts', 1),
                         ('background_jobs', 1),
                         ('seller_marketplace_accounts', 1)):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f'DELETE FROM {table} WHERE id=?', (ident,))


def test_shared_attempt_nullable_usage_and_invalid_lifecycle(connection):
    apply_migration(connection, verbose=False)
    def insert(call_id, lane='admin_supplier_parsing', status='reserved',
               finished=None):
        return connection.execute(f'''
            INSERT INTO {ATTEMPTS} (
                call_id,run_uid,lane,seller_id,provider,model,request_fingerprint,
                status,reserved_at,deadline_at,finished_at
            ) VALUES (?,'admin-run',?,NULL,'deepseek','deepseek-flash',
                      ?,?,'2026-09-27 12:00:00','2026-09-27 12:02:00',?)
        ''', (call_id, lane, 'f' * 64, status, finished))
    insert('a' * 32)
    assert connection.execute(f'SELECT prompt_tokens,provider_cost FROM {ATTEMPTS}').fetchone() == (None, None)
    with pytest.raises(sqlite3.IntegrityError):
        insert('a' * 32)
    with pytest.raises(sqlite3.IntegrityError):
        insert('b' * 32, lane='unscoped')
    with pytest.raises(sqlite3.IntegrityError):
        insert('b' * 32, status='succeeded')
    insert('b' * 32, status='unknown_response', finished='2026-09-27 12:02:01')


def test_malformed_existing_child_fails_atomically(connection):
    connection.execute(f'CREATE TABLE {ITEMS} (id INTEGER PRIMARY KEY)')
    with pytest.raises(sqlite3.OperationalError, match='column contract'):
        apply_migration(connection, verbose=False)
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name=?", (RUNS,),
    ).fetchone() is None


def test_registered_scoped_batch_is_idempotent(tmp_path):
    path = tmp_path / 'synthetic.db'
    connection = sqlite3.connect(path)
    connection.executescript('''
        CREATE TABLE sellers (id INTEGER PRIMARY KEY);
        CREATE TABLE seller_marketplace_accounts (id INTEGER PRIMARY KEY);
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE background_jobs (id INTEGER PRIMARY KEY);
        CREATE TABLE marketplace_product_drafts (id INTEGER PRIMARY KEY);
    ''')
    connection.close()
    scripts = ['migrations/migrate_add_ozon_draft_ai_completion.py']
    run_batch(path, scripts, verbose=False)
    run_batch(path, scripts, verbose=False)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(f'SELECT count(*) FROM {ATTEMPTS}').fetchone() == (0,)
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
    finally:
        connection.close()
