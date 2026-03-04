"""
Миграция для создания таблицы prohibited_words (запрещённые слова WB)

Запуск:
    python migrations/add_prohibited_words.py

Или через Flask shell:
    flask shell
    >>> exec(open('migrations/add_prohibited_words.py').read())
"""

import sqlite3
import os


def get_db_path():
    """Получить путь к базе данных"""
    paths = [
        'data/seller_platform.db',
        '../data/seller_platform.db',
        '/app/data/seller_platform.db',
        'instance/app.db',
        '../instance/app.db',
        'app.db',
        os.path.join(os.path.dirname(__file__), '..', 'data', 'seller_platform.db'),
        os.path.join(os.path.dirname(__file__), '..', 'instance', 'app.db'),
    ]

    for path in paths:
        if os.path.exists(path):
            print(f"Found database at: {path}")
            return path

    db_url = os.environ.get('DATABASE_URL', '')
    if db_url.startswith('sqlite:///'):
        db_path = db_url.replace('sqlite:///', '')
        if os.path.exists(db_path):
            return db_path

    print("Database not found, using default: data/seller_platform.db")
    return 'data/seller_platform.db'


def migrate():
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Проверяем, существует ли таблица
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='prohibited_words'")
    if cursor.fetchone():
        print("Table 'prohibited_words' already exists, skipping creation.")
        conn.close()
        return

    print("Creating table 'prohibited_words'...")
    cursor.execute('''
        CREATE TABLE prohibited_words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word VARCHAR(100) NOT NULL,
            replacement VARCHAR(200) NOT NULL DEFAULT '',
            scope VARCHAR(20) NOT NULL DEFAULT 'global',
            seller_id INTEGER,
            is_active BOOLEAN NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            created_by_user_id INTEGER,
            FOREIGN KEY (seller_id) REFERENCES sellers(id),
            FOREIGN KEY (created_by_user_id) REFERENCES users(id),
            UNIQUE (word, scope, seller_id)
        )
    ''')

    cursor.execute('CREATE INDEX idx_prohibited_words_word ON prohibited_words(word)')
    cursor.execute('CREATE INDEX idx_prohibited_words_scope ON prohibited_words(scope)')
    cursor.execute('CREATE INDEX idx_prohibited_words_seller ON prohibited_words(seller_id)')

    conn.commit()
    print("Table 'prohibited_words' created successfully.")

    # Подсчёт
    cursor.execute("SELECT COUNT(*) FROM prohibited_words")
    count = cursor.fetchone()[0]
    print(f"Total rows in prohibited_words: {count}")

    conn.close()
    print("Migration completed!")


if __name__ == '__main__':
    migrate()
