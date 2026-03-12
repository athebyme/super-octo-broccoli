#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: таблицы для сервисных агентов.

Создаёт таблицы:
  - service_agents     — зарегистрированные агенты
  - agent_tasks        — задачи агентов
  - agent_task_steps   — шаги выполнения (лог рассуждений)
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
    print(f"Running service agents migration on: {db_path}")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # service_agents
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_agents (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            display_name    TEXT NOT NULL,
            description     TEXT,
            agent_type      TEXT NOT NULL DEFAULT 'external',
            status          TEXT NOT NULL DEFAULT 'offline',
            version         TEXT,
            endpoint_url    TEXT,
            api_key_hash    TEXT,
            capabilities    TEXT DEFAULT '[]',
            config_json     TEXT DEFAULT '{}',
            last_heartbeat  TIMESTAMP,
            last_error      TEXT,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_name ON service_agents(name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_status ON service_agents(status)")

    # agent_tasks
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_tasks (
            id                  TEXT PRIMARY KEY,
            agent_id            TEXT NOT NULL REFERENCES service_agents(id),
            seller_id           INTEGER NOT NULL REFERENCES sellers(id),
            task_type           TEXT NOT NULL,
            title               TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'queued',
            priority            INTEGER DEFAULT 0,
            input_data          TEXT DEFAULT '{}',
            total_steps         INTEGER DEFAULT 0,
            completed_steps     INTEGER DEFAULT 0,
            current_step_label  TEXT,
            result_data         TEXT DEFAULT '{}',
            error_message       TEXT,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at          TIMESTAMP,
            completed_at        TIMESTAMP,
            updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_atask_seller_status ON agent_tasks(seller_id, status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_atask_agent_status ON agent_tasks(agent_id, status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_atask_created ON agent_tasks(created_at)")

    # agent_task_steps
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_task_steps (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id         TEXT NOT NULL REFERENCES agent_tasks(id),
            step_number     INTEGER NOT NULL,
            step_type       TEXT NOT NULL DEFAULT 'action',
            title           TEXT NOT NULL,
            detail          TEXT,
            status          TEXT DEFAULT 'completed',
            duration_ms     INTEGER,
            metadata_json   TEXT DEFAULT '{}',
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_atstep_task_num ON agent_task_steps(task_id, step_number)")

    conn.commit()
    conn.close()
    print("Service agents migration completed successfully!")


if __name__ == '__main__':
    db_path = find_database()
    if not db_path:
        print("Database not found!")
        sys.exit(1)
    run_migration(db_path)
