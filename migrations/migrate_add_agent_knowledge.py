#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create curated agent knowledge storage and its SQLite FTS5 index."""
import sqlite3
import sys

try:
    from migrate_add_service_agents import find_database
except ImportError:
    from migrations.migrate_add_service_agents import find_database


def apply_migration(connection: sqlite3.Connection, verbose: bool = True) -> int:
    cursor = connection.cursor()
    before = set(
        row[0] for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index', 'trigger')"
        ).fetchall()
    )
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS agent_knowledge_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER REFERENCES sellers(id),
            scope_key VARCHAR(80) NOT NULL,
            source_key VARCHAR(160) NOT NULL,
            source_type VARCHAR(40) NOT NULL,
            source_uri VARCHAR(1000) NOT NULL,
            title VARCHAR(300) NOT NULL,
            version VARCHAR(80) NOT NULL,
            checksum VARCHAR(64) NOT NULL,
            language VARCHAR(12) NOT NULL DEFAULT 'ru',
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            valid_until DATETIME,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_agent_knowledge_source_version
              UNIQUE (scope_key, source_key, version)
        )
    ''')
    document_columns = {
        row[1] for row in cursor.execute(
            'PRAGMA table_info(agent_knowledge_documents)'
        ).fetchall()
    }
    if 'valid_until' not in document_columns:
        cursor.execute(
            'ALTER TABLE agent_knowledge_documents ADD COLUMN valid_until DATETIME'
        )
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS agent_knowledge_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES agent_knowledge_documents(id),
            ordinal INTEGER NOT NULL,
            heading VARCHAR(300),
            content TEXT NOT NULL,
            search_text TEXT NOT NULL,
            token_estimate INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_agent_knowledge_chunk_ordinal UNIQUE (document_id, ordinal)
        )
    ''')
    for statement in (
        'CREATE INDEX IF NOT EXISTS idx_agent_knowledge_document_seller '
        'ON agent_knowledge_documents(seller_id)',
        'CREATE INDEX IF NOT EXISTS idx_agent_knowledge_scope_status '
        'ON agent_knowledge_documents(seller_id, status, updated_at)',
        'CREATE INDEX IF NOT EXISTS idx_agent_knowledge_source_active '
        'ON agent_knowledge_documents(scope_key, source_key, status)',
        'CREATE INDEX IF NOT EXISTS idx_agent_knowledge_valid_until '
        'ON agent_knowledge_documents(valid_until)',
        'CREATE INDEX IF NOT EXISTS idx_agent_knowledge_chunk_document_id '
        'ON agent_knowledge_chunks(document_id)',
        'CREATE INDEX IF NOT EXISTS idx_agent_knowledge_chunk_document '
        'ON agent_knowledge_chunks(document_id, ordinal)',
    ):
        cursor.execute(statement)
    cursor.execute('''
        CREATE VIRTUAL TABLE IF NOT EXISTS agent_knowledge_chunks_fts USING fts5(
            chunk_id UNINDEXED,
            title,
            heading,
            content,
            tokenize='unicode61 remove_diacritics 2'
        )
    ''')
    cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS agent_knowledge_chunks_fts_ai
        AFTER INSERT ON agent_knowledge_chunks BEGIN
          INSERT INTO agent_knowledge_chunks_fts(rowid, chunk_id, title, heading, content)
          VALUES (
            new.id, new.id,
            COALESCE((SELECT title FROM agent_knowledge_documents WHERE id = new.document_id), ''),
            COALESCE(new.heading, ''), new.content
          );
        END
    ''')
    cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS agent_knowledge_chunks_fts_ad
        AFTER DELETE ON agent_knowledge_chunks BEGIN
          DELETE FROM agent_knowledge_chunks_fts WHERE rowid = old.id;
        END
    ''')
    cursor.execute('''
        CREATE TRIGGER IF NOT EXISTS agent_knowledge_chunks_fts_au
        AFTER UPDATE OF heading, content ON agent_knowledge_chunks BEGIN
          DELETE FROM agent_knowledge_chunks_fts WHERE rowid = old.id;
          INSERT INTO agent_knowledge_chunks_fts(rowid, chunk_id, title, heading, content)
          VALUES (
            new.id, new.id,
            COALESCE((SELECT title FROM agent_knowledge_documents WHERE id = new.document_id), ''),
            COALESCE(new.heading, ''), new.content
          );
        END
    ''')
    chunk_count = cursor.execute(
        'SELECT COUNT(*) FROM agent_knowledge_chunks'
    ).fetchone()[0]
    fts_count = cursor.execute(
        'SELECT COUNT(*) FROM agent_knowledge_chunks_fts'
    ).fetchone()[0]
    if chunk_count != fts_count:
        cursor.execute('DELETE FROM agent_knowledge_chunks_fts')
        cursor.execute('''
            INSERT INTO agent_knowledge_chunks_fts(rowid, chunk_id, title, heading, content)
            SELECT c.id, c.id, d.title, COALESCE(c.heading, ''), c.content
            FROM agent_knowledge_chunks c
            JOIN agent_knowledge_documents d ON d.id = c.document_id
        ''')
    after = set(
        row[0] for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index', 'trigger')"
        ).fetchall()
    )
    if verbose:
        print('Agent knowledge migration completed successfully!')
    return len(after - before)


def run_migration(db_path: str) -> None:
    connection = sqlite3.connect(db_path)
    try:
        apply_migration(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == '__main__':
    database = sys.argv[1] if len(sys.argv) > 1 else find_database()
    if not database:
        raise SystemExit('Database not found')
    run_migration(database)
