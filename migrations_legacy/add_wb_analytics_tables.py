"""
Миграция: создание таблиц для хранения сырых данных WB аналитики.

Таблицы:
- wb_sales      — продажи и возвраты (из /api/v1/supplier/sales)
- wb_orders     — заказы (из /api/v1/supplier/orders)
- wb_feedbacks  — отзывы (из feedbacks-api)
- wb_realization_rows — строки реализации (из /api/v5/supplier/reportDetailByPeriod)

Запуск:
    python migrations/add_wb_analytics_tables.py
"""

import sqlite3
import os


def get_db_path():
    paths = [
        'data/seller_platform.db',
        '../data/seller_platform.db',
        '/app/data/seller_platform.db',
        'instance/app.db',
        '../instance/app.db',
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
    return 'data/seller_platform.db'


def migrate(db_path=None):
    if db_path is None:
        db_path = get_db_path()
    print(f"Using database: {db_path}")

    if not os.path.exists(db_path):
        print(f"Database file not found: {db_path}")
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get existing tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing_tables = {row[0] for row in cursor.fetchall()}

    created = 0

    # --- wb_sales ---
    if 'wb_sales' not in existing_tables:
        cursor.execute('''
            CREATE TABLE wb_sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL,
                srid TEXT NOT NULL,
                sale_id TEXT,
                nm_id INTEGER,
                date DATETIME,
                last_change_date DATETIME,
                supplier_article TEXT,
                subject TEXT,
                brand TEXT,
                warehouse_name TEXT,
                region_name TEXT,
                country_name TEXT,
                finished_price REAL DEFAULT 0,
                price_with_disc REAL DEFAULT 0,
                for_pay REAL DEFAULT 0,
                is_return BOOLEAN DEFAULT 0,
                UNIQUE(seller_id, srid)
            )
        ''')
        cursor.execute('CREATE INDEX idx_wb_sales_seller_date ON wb_sales(seller_id, date)')
        cursor.execute('CREATE INDEX idx_wb_sales_seller_lcd ON wb_sales(seller_id, last_change_date)')
        print('  + Created table: wb_sales')
        created += 1
    else:
        print('  = Table wb_sales already exists')

    # --- wb_orders ---
    if 'wb_orders' not in existing_tables:
        cursor.execute('''
            CREATE TABLE wb_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL,
                srid TEXT NOT NULL,
                nm_id INTEGER,
                date DATETIME,
                last_change_date DATETIME,
                supplier_article TEXT,
                subject TEXT,
                brand TEXT,
                warehouse_name TEXT,
                region_name TEXT,
                oblast_okrug_name TEXT,
                country_name TEXT,
                total_price REAL DEFAULT 0,
                finished_price REAL DEFAULT 0,
                is_cancel BOOLEAN DEFAULT 0,
                cancel_dt DATETIME,
                order_type TEXT,
                sticker TEXT,
                UNIQUE(seller_id, srid)
            )
        ''')
        cursor.execute('CREATE INDEX idx_wb_orders_seller_date ON wb_orders(seller_id, date)')
        cursor.execute('CREATE INDEX idx_wb_orders_seller_lcd ON wb_orders(seller_id, last_change_date)')
        print('  + Created table: wb_orders')
        created += 1
    else:
        print('  = Table wb_orders already exists')

    # --- wb_feedbacks ---
    if 'wb_feedbacks' not in existing_tables:
        cursor.execute('''
            CREATE TABLE wb_feedbacks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL,
                wb_id TEXT NOT NULL,
                nm_id INTEGER,
                created_date DATETIME,
                updated_date DATETIME,
                valuation INTEGER DEFAULT 0,
                text TEXT,
                user_name TEXT,
                product_name TEXT,
                subject_name TEXT,
                brand_name TEXT,
                is_answered BOOLEAN DEFAULT 0,
                UNIQUE(seller_id, wb_id)
            )
        ''')
        cursor.execute('CREATE INDEX idx_wb_feedbacks_seller ON wb_feedbacks(seller_id)')
        print('  + Created table: wb_feedbacks')
        created += 1
    else:
        print('  = Table wb_feedbacks already exists')

    # --- wb_realization_rows ---
    if 'wb_realization_rows' not in existing_tables:
        cursor.execute('''
            CREATE TABLE wb_realization_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL,
                rrd_id INTEGER NOT NULL,
                realizationreport_id INTEGER,
                rr_dt DATETIME,
                date_from DATE,
                date_to DATE,
                nm_id INTEGER,
                sa_name TEXT,
                subject_name TEXT,
                brand_name TEXT,
                supplier_oper_name TEXT,
                doc_type_name TEXT,
                retail_price_withdisc_rub REAL DEFAULT 0,
                retail_amount REAL DEFAULT 0,
                ppvz_for_pay REAL DEFAULT 0,
                ppvz_sales_commission REAL DEFAULT 0,
                commission_percent REAL DEFAULT 0,
                delivery_rub REAL DEFAULT 0,
                rebill_logistic_cost REAL DEFAULT 0,
                storage_fee REAL DEFAULT 0,
                penalty REAL DEFAULT 0,
                deduction REAL DEFAULT 0,
                acceptance REAL DEFAULT 0,
                additional_payment REAL DEFAULT 0,
                return_amount INTEGER DEFAULT 0,
                delivery_amount INTEGER DEFAULT 0,
                UNIQUE(seller_id, rrd_id)
            )
        ''')
        cursor.execute('CREATE INDEX idx_wb_realization_seller_dt ON wb_realization_rows(seller_id, rr_dt)')
        cursor.execute('CREATE INDEX idx_wb_realization_seller_rrd ON wb_realization_rows(seller_id, rrd_id)')
        print('  + Created table: wb_realization_rows')
        created += 1
    else:
        print('  = Table wb_realization_rows already exists')

    conn.commit()
    conn.close()
    print(f'\nMigration completed! Created {created} new tables.')
    return True


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        migrate(sys.argv[1])
    else:
        migrate()
