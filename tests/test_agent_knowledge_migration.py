# -*- coding: utf-8 -*-
"""Idempotency and FTS backfill for the standalone knowledge migration."""
import sqlite3

from migrations.migrate_add_agent_knowledge import apply_migration


def test_agent_knowledge_migration_is_idempotent_and_fts_is_live():
    connection = sqlite3.connect(':memory:')
    try:
        connection.execute('CREATE TABLE sellers (id INTEGER PRIMARY KEY)')
        apply_migration(connection, verbose=False)
        apply_migration(connection, verbose=False)
        connection.execute("""
            INSERT INTO agent_knowledge_documents (
                seller_id, scope_key, source_key, source_type, source_uri,
                title, version, checksum
            ) VALUES (NULL, 'global', 'wb/test', 'wb_official',
                      'https://seller.wildberries.ru/test', 'Правила описания',
                      '1', 'checksum')
        """)
        document_id = connection.execute(
            'SELECT id FROM agent_knowledge_documents'
        ).fetchone()[0]
        connection.execute("""
            INSERT INTO agent_knowledge_chunks (
                document_id, ordinal, content, search_text, token_estimate
            ) VALUES (?, 0, 'Внешние ссылки запрещены',
                      'внешние ссылки запрещены', 10)
        """, (document_id,))
        match = connection.execute("""
            SELECT chunk_id FROM agent_knowledge_chunks_fts
            WHERE agent_knowledge_chunks_fts MATCH 'ссылк*'
        """).fetchone()
        assert match is not None
    finally:
        connection.close()
