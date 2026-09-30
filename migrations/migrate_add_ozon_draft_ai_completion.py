#!/usr/bin/env python3
"""Add empty seller draft-suggestion audit and shared AI physical-call ledger.

No historical job, draft, supplier content, or provider operation is scanned or
backfilled. Existing objects are checked before a no-op is accepted.
"""

import os
import sqlite3
import sys

if __package__:
    from ._foreign_key_safety import foreign_key_snapshot, assert_foreign_key_safety
else:
    from _foreign_key_safety import foreign_key_snapshot, assert_foreign_key_safety


RUNS = 'ozon_draft_completion_runs'
ITEMS = 'ozon_draft_completion_items'
SUGGESTIONS = 'ozon_draft_completion_suggestions'
REVIEWS = 'ozon_draft_completion_reviews'
ATTEMPTS = 'ai_parsing_attempts'
MANAGED = (RUNS, ITEMS, SUGGESTIONS, REVIEWS, ATTEMPTS)

CREATE_SQL = {
    RUNS: f'''CREATE TABLE IF NOT EXISTS {RUNS} (
        id INTEGER PRIMARY KEY,
        job_id INTEGER NOT NULL UNIQUE REFERENCES background_jobs(id) ON DELETE RESTRICT,
        seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE RESTRICT,
        account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE RESTRICT,
        actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        request_key_hash CHAR(64) NOT NULL,
        request_fingerprint CHAR(64) NOT NULL,
        profile_version VARCHAR(60) NOT NULL,
        model VARCHAR(80) NOT NULL,
        status VARCHAR(24) NOT NULL,
        next_due_at DATETIME,
        lease_token CHAR(32),
        lease_until DATETIME,
        item_count INTEGER NOT NULL,
        max_calls INTEGER NOT NULL,
        requested_calls INTEGER NOT NULL DEFAULT 0,
        prompt_tokens BIGINT,
        completion_tokens BIGINT,
        cache_hit_tokens BIGINT,
        cache_miss_tokens BIGINT,
        reasoning_tokens BIGINT,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        completed_at DATETIME,
        UNIQUE (seller_id, request_key_hash),
        CONSTRAINT ck_ozon_draft_ai_run_status CHECK (status IN ('pending','running','cancelling','completed','cancelled','failed')),
        CONSTRAINT ck_ozon_draft_ai_run_counts CHECK (item_count BETWEEN 1 AND 200 AND max_calls BETWEEN 1 AND 80 AND requested_calls BETWEEN 0 AND max_calls),
        CONSTRAINT ck_ozon_draft_ai_run_lease CHECK ((lease_token IS NULL) = (lease_until IS NULL))
    )''',
    ITEMS: f'''CREATE TABLE IF NOT EXISTS {ITEMS} (
        id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL REFERENCES {RUNS}(id) ON DELETE RESTRICT,
        ordinal INTEGER NOT NULL,
        seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE RESTRICT,
        account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE RESTRICT,
        draft_id INTEGER NOT NULL REFERENCES marketplace_product_drafts(id) ON DELETE RESTRICT,
        imported_product_id INTEGER NOT NULL,
        product_type_id INTEGER,
        expected_draft_version INTEGER NOT NULL,
        source_kind VARCHAR(16),
        source_product_id INTEGER,
        source_hash CHAR(64),
        type_schema_hash CHAR(64),
        dictionary_hash CHAR(64),
        filled_slots_hash CHAR(64),
        reviewed_filled_slots_hash CHAR(64),
        status VARCHAR(24) NOT NULL,
        next_due_at DATETIME,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        call_id CHAR(32),
        lease_token CHAR(32),
        lease_until DATETIME,
        last_attempt_at DATETIME,
        safe_code VARCHAR(80),
        prompt_tokens BIGINT,
        completion_tokens BIGINT,
        cache_hit_tokens BIGINT,
        cache_miss_tokens BIGINT,
        reasoning_tokens BIGINT,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        completed_at DATETIME,
        UNIQUE (run_id, ordinal),
        UNIQUE (run_id, draft_id),
        CONSTRAINT ck_ozon_draft_ai_item_identity CHECK (ordinal BETWEEN 1 AND 200 AND imported_product_id > 0 AND (product_type_id IS NULL OR product_type_id > 0) AND expected_draft_version > 0),
        CONSTRAINT ck_ozon_draft_ai_item_status CHECK (status IN ('pending','reserved','unknown_response','proposed','no_evidence','stale','needs_input','failed','cancelled')),
        CONSTRAINT ck_ozon_draft_ai_item_source_kind CHECK (source_kind IS NULL OR source_kind IN ('imported','supplier')),
        CONSTRAINT ck_ozon_draft_ai_item_seal CHECK (status NOT IN ('pending','reserved') OR (product_type_id IS NOT NULL AND source_kind IS NOT NULL AND source_hash IS NOT NULL AND type_schema_hash IS NOT NULL AND dictionary_hash IS NOT NULL AND filled_slots_hash IS NOT NULL)),
        CONSTRAINT ck_ozon_draft_ai_item_reservation CHECK (status != 'reserved' OR (call_id IS NOT NULL AND lease_token IS NOT NULL AND lease_until IS NOT NULL)),
        CONSTRAINT ck_ozon_draft_ai_item_lease CHECK ((lease_token IS NULL) = (lease_until IS NULL)),
        CONSTRAINT ck_ozon_draft_ai_item_attempts CHECK (attempt_count BETWEEN 0 AND 80)
    )''',
    SUGGESTIONS: f'''CREATE TABLE IF NOT EXISTS {SUGGESTIONS} (
        id INTEGER PRIMARY KEY,
        item_id INTEGER NOT NULL REFERENCES {ITEMS}(id) ON DELETE RESTRICT,
        attribute_id VARCHAR(100) NOT NULL,
        complex_id VARCHAR(100) NOT NULL DEFAULT '0',
        group_ordinal INTEGER NOT NULL DEFAULT 0,
        values_json TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        provenance_code VARCHAR(60) NOT NULL,
        status VARCHAR(16) NOT NULL,
        reviewer_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        reviewed_at DATETIME,
        applied_draft_version INTEGER,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        UNIQUE (item_id, attribute_id, complex_id, group_ordinal),
        CONSTRAINT ck_ozon_draft_ai_suggestion_identity CHECK (length(attribute_id) BETWEEN 1 AND 100 AND length(complex_id) BETWEEN 1 AND 100 AND group_ordinal >= 0),
        CONSTRAINT ck_ozon_draft_ai_suggestion_status CHECK (status IN ('proposed','accepted','rejected','stale')),
        CONSTRAINT ck_ozon_draft_ai_suggestion_version CHECK (applied_draft_version IS NULL OR applied_draft_version > 0)
    )''',
    REVIEWS: f'''CREATE TABLE IF NOT EXISTS {REVIEWS} (
        id INTEGER PRIMARY KEY,
        seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE RESTRICT,
        account_id INTEGER NOT NULL REFERENCES seller_marketplace_accounts(id) ON DELETE RESTRICT,
        draft_id INTEGER NOT NULL REFERENCES marketplace_product_drafts(id) ON DELETE RESTRICT,
        item_id INTEGER NOT NULL REFERENCES {ITEMS}(id) ON DELETE RESTRICT,
        actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        request_key_hash CHAR(64) NOT NULL,
        request_fingerprint CHAR(64) NOT NULL,
        action VARCHAR(12) NOT NULL,
        version_before INTEGER NOT NULL,
        version_after INTEGER,
        selected_ids_json TEXT NOT NULL,
        created_at DATETIME NOT NULL,
        UNIQUE (seller_id, request_key_hash),
        CONSTRAINT ck_ozon_draft_ai_review_action CHECK (action IN ('apply','reject')),
        CONSTRAINT ck_ozon_draft_ai_review_version CHECK (version_before > 0 AND ((action = 'apply' AND version_after > version_before) OR (action = 'reject' AND version_after IS NULL)))
    )''',
    ATTEMPTS: f'''CREATE TABLE IF NOT EXISTS {ATTEMPTS} (
        id INTEGER PRIMARY KEY,
        call_id CHAR(32) NOT NULL UNIQUE,
        run_uid VARCHAR(80),
        lane VARCHAR(40) NOT NULL,
        seller_id INTEGER REFERENCES sellers(id) ON DELETE RESTRICT,
        provider VARCHAR(24) NOT NULL,
        model VARCHAR(80) NOT NULL,
        request_fingerprint CHAR(64) NOT NULL,
        status VARCHAR(24) NOT NULL,
        reserved_at DATETIME NOT NULL,
        deadline_at DATETIME NOT NULL,
        finished_at DATETIME,
        http_status INTEGER,
        retry_due_at DATETIME,
        safe_code VARCHAR(80),
        prompt_tokens BIGINT,
        completion_tokens BIGINT,
        cache_hit_tokens BIGINT,
        cache_miss_tokens BIGINT,
        reasoning_tokens BIGINT,
        provider_cost NUMERIC(20, 8),
        estimated_cost NUMERIC(20, 8),
        CONSTRAINT ck_ai_parsing_attempt_lane CHECK (lane IN ('seller_draft_completion','admin_supplier_parsing')),
        CONSTRAINT ck_ai_parsing_attempt_status CHECK (status IN ('reserved','succeeded','rate_limited','http_error','invalid_response','unknown_response')),
        CONSTRAINT ck_ai_parsing_attempt_deadline CHECK (deadline_at > reserved_at),
        CONSTRAINT ck_ai_parsing_attempt_finished CHECK ((status = 'reserved' AND finished_at IS NULL) OR (status != 'reserved' AND finished_at IS NOT NULL)),
        CONSTRAINT ck_ai_parsing_attempt_http CHECK (http_status IS NULL OR http_status BETWEEN 100 AND 599)
    )''',
}

INDEX_SQL = {
    'idx_ozon_draft_ai_run_due': f'CREATE INDEX IF NOT EXISTS idx_ozon_draft_ai_run_due ON {RUNS} (status,next_due_at,id)',
    'idx_ozon_draft_ai_run_history': f'CREATE INDEX IF NOT EXISTS idx_ozon_draft_ai_run_history ON {RUNS} (seller_id,account_id,created_at,id)',
    'uq_ozon_draft_ai_item_active': f"CREATE UNIQUE INDEX IF NOT EXISTS uq_ozon_draft_ai_item_active ON {ITEMS} (seller_id,account_id,draft_id) WHERE status IN ('pending','reserved')",
    'idx_ozon_draft_ai_item_due': f'CREATE INDEX IF NOT EXISTS idx_ozon_draft_ai_item_due ON {ITEMS} (status,next_due_at,last_attempt_at,id)',
    'idx_ozon_draft_ai_item_run': f'CREATE INDEX IF NOT EXISTS idx_ozon_draft_ai_item_run ON {ITEMS} (run_id,ordinal)',
    'idx_ozon_draft_ai_suggestion_item': f'CREATE INDEX IF NOT EXISTS idx_ozon_draft_ai_suggestion_item ON {SUGGESTIONS} (item_id,status,id)',
    'idx_ozon_draft_ai_review_item': f'CREATE INDEX IF NOT EXISTS idx_ozon_draft_ai_review_item ON {REVIEWS} (item_id,created_at,id)',
    'idx_ai_parsing_attempt_active': f'CREATE INDEX IF NOT EXISTS idx_ai_parsing_attempt_active ON {ATTEMPTS} (status,deadline_at)',
    'idx_ai_parsing_attempt_run': f'CREATE INDEX IF NOT EXISTS idx_ai_parsing_attempt_run ON {ATTEMPTS} (lane,run_uid)',
    'idx_ai_parsing_attempt_cooldown': f'CREATE INDEX IF NOT EXISTS idx_ai_parsing_attempt_cooldown ON {ATTEMPTS} (status,retry_due_at)',
    'idx_ai_parsing_attempt_seller': f'CREATE INDEX IF NOT EXISTS idx_ai_parsing_attempt_seller ON {ATTEMPTS} (seller_id,status)',
}


def _normal(value):
    return ''.join(str(value or '').lower().replace('"', '').split())


def _table_info(connection, table):
    # SQLAlchemy renders INTEGER PRIMARY KEY as `INTEGER NOT NULL` while a
    # hand-written SQLite declaration reports notnull=0; both reject NULL.
    return {row[1]: (row[2].upper(), row[3] if not row[5] else None,
                     _normal(row[4]), row[5])
            for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _foreign_keys(connection, table):
    return {(row[3], row[2], row[4], row[6])
            for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')}


def _unique_columns(connection, table):
    return {tuple(row[2] for row in connection.execute(f'PRAGMA index_info("{entry[1]}")'))
            for entry in connection.execute(f'PRAGMA index_list("{table}")')
            if entry[2] and not entry[4]}


def _named_checks(sql):
    normalized = _normal(sql)
    result = {}
    for name in (
                'ck_ozon_draft_ai_run_status', 'ck_ozon_draft_ai_run_counts', 'ck_ozon_draft_ai_run_lease',
                'ck_ozon_draft_ai_item_identity', 'ck_ozon_draft_ai_item_status', 'ck_ozon_draft_ai_item_source_kind',
                'ck_ozon_draft_ai_item_seal', 'ck_ozon_draft_ai_item_reservation', 'ck_ozon_draft_ai_item_lease',
                'ck_ozon_draft_ai_item_attempts', 'ck_ozon_draft_ai_suggestion_identity',
                'ck_ozon_draft_ai_suggestion_status', 'ck_ozon_draft_ai_suggestion_version',
                'ck_ozon_draft_ai_review_action', 'ck_ozon_draft_ai_review_version',
                'ck_ai_parsing_attempt_lane', 'ck_ai_parsing_attempt_status',
                'ck_ai_parsing_attempt_deadline', 'ck_ai_parsing_attempt_finished', 'ck_ai_parsing_attempt_http',
            ):
        marker = 'constraint' + name + 'check('
        start = normalized.find(marker)
        if start < 0:
            continue
        start += len(marker)
        depth = 1
        for end in range(start, len(normalized)):
            if normalized[end] == '(':
                depth += 1
            elif normalized[end] == ')':
                depth -= 1
                if depth == 0:
                    result[name] = normalized[start:end]
                    break
    return result


def _require_schema(connection, *, require_indexes=True):
    expected = sqlite3.connect(':memory:')
    try:
        for ddl in CREATE_SQL.values():
            expected.execute(ddl)
        for ddl in INDEX_SQL.values():
            expected.execute(ddl)
        for table in MANAGED:
            if _table_info(connection, table) != _table_info(expected, table):
                raise sqlite3.OperationalError('Draft AI column contract incompatible: ' + table)
            if _foreign_keys(connection, table) != _foreign_keys(expected, table):
                raise sqlite3.OperationalError('Draft AI FK contract incompatible: ' + table)
            if not _unique_columns(expected, table) <= _unique_columns(connection, table):
                raise sqlite3.OperationalError('Draft AI UNIQUE contract incompatible: ' + table)
            actual_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,),
            ).fetchone()
            expected_sql = expected.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,),
            ).fetchone()
            actual_checks = _named_checks(actual_sql[0] if actual_sql else '')
            expected_checks = _named_checks(expected_sql[0])
            if set(actual_checks) != set(expected_checks):
                raise sqlite3.OperationalError('Draft AI CHECK contract incompatible: ' + table)
            for name in expected_checks:
                if actual_checks[name] != expected_checks[name]:
                    raise sqlite3.OperationalError('Draft AI CHECK predicate incompatible: ' + name)
        for name in INDEX_SQL if require_indexes else ():
            actual = connection.execute(
                "SELECT tbl_name,sql FROM sqlite_master WHERE type='index' AND name=?", (name,),
            ).fetchone()
            required = expected.execute(
                "SELECT tbl_name,sql FROM sqlite_master WHERE type='index' AND name=?", (name,),
            ).fetchone()
            if not actual or actual[0] != required[0] or _normal(actual[1]).replace('ifnotexists', '') != _normal(required[1]).replace('ifnotexists', ''):
                raise sqlite3.OperationalError('Draft AI index incompatible: ' + name)
    finally:
        expected.close()


def apply_migration(connection, *, verbose=True):
    for table in ('background_jobs', 'sellers', 'seller_marketplace_accounts',
                  'users', 'marketplace_product_drafts'):
        if 'id' not in _table_info(connection, table):
            raise sqlite3.OperationalError('Draft AI prerequisite missing: ' + table)
    baseline = foreign_key_snapshot(connection)
    before = set(connection.execute('SELECT type,name FROM sqlite_master'))
    connection.execute('SAVEPOINT ozon_draft_ai_ddl')
    try:
        for ddl in CREATE_SQL.values():
            connection.execute(ddl)
        _require_schema(connection, require_indexes=False)
        for ddl in INDEX_SQL.values():
            connection.execute(ddl)
        _require_schema(connection)
        assert_foreign_key_safety(
            connection, baseline=baseline, managed_tables=set(MANAGED),
            label='Draft AI completion migration',
        )
        connection.execute('RELEASE SAVEPOINT ozon_draft_ai_ddl')
    except Exception:
        connection.execute('ROLLBACK TO SAVEPOINT ozon_draft_ai_ddl')
        connection.execute('RELEASE SAVEPOINT ozon_draft_ai_ddl')
        raise
    if verbose:
        print('Draft AI completion migration completed successfully')
    return len(set(connection.execute('SELECT type,name FROM sqlite_master')) - before)


def migrate(db_path):
    if not os.path.isfile(db_path):
        raise sqlite3.OperationalError('Draft AI completion database does not exist')
    connection = sqlite3.connect(db_path)
    try:
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('BEGIN IMMEDIATE')
        apply_migration(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == '__main__':
    migrate(sys.argv[1] if len(sys.argv) > 1 else os.environ.get('DATABASE_PATH', 'data/seller_platform.db'))
