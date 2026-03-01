import sqlite3

db_path = r"c:\super-octo-broccoli\data\seller_platform.db"

try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cursor.fetchall()
    print("Tables:")
    for table in tables:
        print(f"  {table[0]}")
    conn.close()
except Exception as e:
    print(f"Error: {e}")
