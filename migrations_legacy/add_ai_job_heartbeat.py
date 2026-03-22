# -*- coding: utf-8 -*-
"""
Миграция: Добавление поля heartbeat_at в таблицу ai_parse_jobs.

Позволяет обнаруживать зависшие задачи AI парсинга (stale jobs),
когда воркер-поток умирает без обновления статуса.
"""
import sqlite3
import logging
import os

logger = logging.getLogger(__name__)


def run_migration(db_path: str = None):
    """Запустить миграцию."""
    if db_path is None:
        base_dir = os.path.dirname(os.path.dirname(__file__))
        candidates = [
            os.path.join(base_dir, 'data', 'seller_platform.db'),
            os.path.join(base_dir, 'instance', 'seller_platform.db'),
            os.environ.get('DATABASE_URL', '').replace('sqlite:////', '/').replace('sqlite:///', ''),
        ]
        for path in candidates:
            if path and os.path.exists(path):
                db_path = path
                break

    if not db_path or not os.path.exists(db_path):
        logger.warning("БД не найдена, пропускаем миграцию add_ai_job_heartbeat")
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # Проверяем существование таблицы
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ai_parse_jobs'")
        if not cursor.fetchone():
            logger.info("Таблица ai_parse_jobs не существует, пропускаем")
            return False

        # Проверяем есть ли уже колонка
        cursor.execute("PRAGMA table_info(ai_parse_jobs)")
        columns = {row[1] for row in cursor.fetchall()}

        if 'heartbeat_at' not in columns:
            cursor.execute("ALTER TABLE ai_parse_jobs ADD COLUMN heartbeat_at DATETIME")
            conn.commit()
            logger.info("Добавлено поле heartbeat_at в ai_parse_jobs")
        else:
            logger.info("Поле heartbeat_at уже существует")

        # Помечаем старые running/pending задачи как failed (они точно stale)
        cursor.execute("""
            UPDATE ai_parse_jobs
            SET status = 'failed',
                error_message = 'Задача зависла (обнаружено при миграции). Воркер-поток был убит до завершения.'
            WHERE status IN ('pending', 'running')
        """)
        stale_count = cursor.rowcount
        if stale_count > 0:
            conn.commit()
            logger.info(f"Помечено {stale_count} зависших задач как failed")

        return True
    except Exception as e:
        logger.error(f"Ошибка миграции: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    run_migration()
