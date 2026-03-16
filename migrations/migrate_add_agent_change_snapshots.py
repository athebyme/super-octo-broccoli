#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: таблица agent_change_snapshots для отката изменений агентов.

Хранит снимки значений полей товаров до изменения агентом,
позволяя откатить изменения задачи.
"""
import sqlite3
import os
import sys


def find_database():
    """Находит базу данных."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(script_dir)
    possible_paths = [
        '/app/data/seller_platform.db',
        os.path.join(parent_dir, 'data', 'seller_platform.db'),
        os.path.join(parent_dir, 'seller_platform.db'),
    ]
    if len(sys.argv) > 1:
        return sys.argv[1]
    for path in possible_paths:
        if os.path.exists(path):
            return path
    return None


def run_migration(db_path):
    print(f"Running agent_change_snapshots migration on: {db_path}")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_change_snapshots (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id             TEXT REFERENCES agent_tasks(id),
            imported_product_id INTEGER NOT NULL REFERENCES imported_products(id),
            agent_id            TEXT,
            previous_values     TEXT NOT NULL,
            new_values          TEXT NOT NULL,
            is_rolled_back      BOOLEAN DEFAULT 0,
            rolled_back_at      TIMESTAMP,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_acs_task_id ON agent_change_snapshots(task_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_acs_product_id ON agent_change_snapshots(imported_product_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_acs_task_product ON agent_change_snapshots(task_id, imported_product_id)")

    conn.commit()
    conn.close()
    print("agent_change_snapshots migration completed successfully!")


if __name__ == '__main__':
    db_path = find_database()
    if not db_path:
        print("Database not found!")
        sys.exit(1)
    run_migration(db_path)
