#!/usr/bin/env python3
"""
Миграция: добавление таблицы card_merge_history для истории объединений карточек
"""
import sqlite3
import sys
import argparse


def migrate_database(db_path: str):
    """Применить миграцию к базе данных"""
    print(f"🔄 Запуск миграции базы данных: {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # === Миграция: Создание таблицы card_merge_history ===
        print("📝 Проверка таблицы card_merge_history...")

        # Проверяем существует ли таблица
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='card_merge_history'
        """)

        if cursor.fetchone():
            print("  ✓ Таблица card_merge_history уже существует")
        else:
            print("  ➕ Создание таблицы card_merge_history...")
            cursor.execute("""
                CREATE TABLE card_merge_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER NOT NULL,
                    operation_type VARCHAR(20) NOT NULL,
                    target_imt_id BIGINT,
                    merged_nm_ids JSON NOT NULL,
                    snapshot_before JSON,
                    snapshot_after JSON,
                    status VARCHAR(50) DEFAULT 'pending',
                    wb_synced BOOLEAN DEFAULT 0,
                    wb_sync_status VARCHAR(50),
                    wb_error_message TEXT,
                    reverted BOOLEAN DEFAULT 0,
                    reverted_at DATETIME,
                    reverted_by_user_id INTEGER,
                    revert_operation_id INTEGER,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at DATETIME,
                    duration_seconds REAL,
                    user_comment TEXT,
                    FOREIGN KEY (seller_id) REFERENCES sellers(id),
                    FOREIGN KEY (reverted_by_user_id) REFERENCES users(id),
                    FOREIGN KEY (revert_operation_id) REFERENCES card_merge_history(id)
                )
            """)

            # Создаем индексы
            cursor.execute("""
                CREATE INDEX idx_merge_seller_created
                ON card_merge_history(seller_id, created_at)
            """)

            cursor.execute("""
                CREATE INDEX idx_merge_operation
                ON card_merge_history(operation_type, status)
            """)

            cursor.execute("""
                CREATE INDEX idx_merge_target_imt
                ON card_merge_history(target_imt_id)
            """)

            conn.commit()
            print("  ✅ Таблица card_merge_history создана")

        print("✅ Миграция успешно завершена")

    except Exception as e:
        print(f"❌ Ошибка миграции: {e}")
        conn.rollback()
        print("💥 Миграция не удалась: {e}")
        return False

    finally:
        conn.close()

    return True


def main():
    parser = argparse.ArgumentParser(description='Миграция БД: добавление таблицы card_merge_history')
    parser.add_argument('--db-path', default='data/seller_platform.db',
                        help='Путь к файлу базы данных')

    args = parser.parse_args()

    success = migrate_database(args.db_path)
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
