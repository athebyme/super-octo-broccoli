#!/usr/bin/env python3
"""
Автоматическая инициализация базы данных
"""
import os
import secrets
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from seller_platform import app
from models import db, User

def init_database():
    """Инициализировать базу данных"""
    with app.app_context():
        print("Создание всех таблиц...")
        db.create_all()
        print("✓ Все таблицы созданы")

        # Проверяем, есть ли админ
        admin = User.query.filter_by(is_admin=True).first()
        if not admin:
            # Генерируем безопасный пароль (или берём из env)
            admin_password = os.environ.get('ADMIN_INITIAL_PASSWORD') or secrets.token_urlsafe(16)
            admin = User(
                username='admin',
                email='admin@example.com',
                is_admin=True,
                is_active=True
            )
            admin.set_password(admin_password)
            db.session.add(admin)
            db.session.commit()
            print(f"✓ Создан администратор: admin / {admin_password}")
            print("⚠️  ВАЖНО: сохраните пароль и смените его при первом входе!")
        else:
            print(f"✓ Администратор уже существует: {admin.username}")

        print("\n✅ Инициализация завершена!")

if __name__ == '__main__':
    init_database()
