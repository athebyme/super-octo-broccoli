#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create durable unified-agent chat tables and seller model policy."""
import sqlite3
import sys

from migrate_add_service_agents import find_database


def run_migration(db_path):
    connection = sqlite3.connect(db_path)
    cursor = connection.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS agent_conversations (
            id TEXT PRIMARY KEY,
            seller_id INTEGER NOT NULL REFERENCES sellers(id),
            user_id INTEGER NOT NULL REFERENCES users(id),
            title VARCHAR(160) NOT NULL DEFAULT 'Новый диалог',
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_message_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS agent_messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES agent_conversations(id),
            role VARCHAR(20) NOT NULL,
            kind VARCHAR(30) NOT NULL DEFAULT 'text',
            content TEXT NOT NULL DEFAULT '',
            task_id TEXT REFERENCES agent_tasks(id),
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_agent_conversation_seller_recent ON agent_conversations(seller_id, last_message_at)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_agent_conversation_user_status ON agent_conversations(user_id, status)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_agent_message_conversation_created ON agent_messages(conversation_id, created_at)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_agent_messages_task ON agent_messages(task_id)')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS agent_review_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES agent_tasks(id),
            seller_id INTEGER NOT NULL REFERENCES sellers(id),
            imported_product_id INTEGER NOT NULL REFERENCES imported_products(id),
            proposal_type VARCHAR(40) NOT NULL DEFAULT 'protected_fields',
            changes_json TEXT NOT NULL DEFAULT '{}',
            reason TEXT,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            reviewed_by_user_id INTEGER REFERENCES users(id),
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_at DATETIME
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_agent_proposal_seller_status ON agent_review_proposals(seller_id, status)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_agent_proposal_task_status ON agent_review_proposals(task_id, status)')

    columns = {row[1] for row in cursor.execute('PRAGMA table_info(auto_import_settings)').fetchall()}
    if 'agent_single_model' not in columns:
        cursor.execute('ALTER TABLE auto_import_settings ADD COLUMN agent_single_model BOOLEAN DEFAULT 0 NOT NULL')

    task_columns = {row[1] for row in cursor.execute('PRAGMA table_info(agent_tasks)').fetchall()}
    if 'checkpoint_json' not in task_columns:
        cursor.execute("ALTER TABLE agent_tasks ADD COLUMN checkpoint_json TEXT DEFAULT '{}'")

    connection.commit()
    connection.close()
    print('Unified agent chat migration completed successfully!')


if __name__ == '__main__':
    database = sys.argv[1] if len(sys.argv) > 1 else find_database()
    if not database:
        raise SystemExit('Database not found')
    run_migration(database)
