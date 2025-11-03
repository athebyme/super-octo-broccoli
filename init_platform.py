#!/usr/bin/env python3
"""
Скрипт инициализации платформы для продавцов WB
Создает базу данных и первого администратора
"""
import os
import sys
from getpass import getpass

from seller_platform import app, db
from models import User


def init_database():
    """Инициализация базы данных"""
    print("Инициализация базы данных...")
    with app.app_context():
        db.create_all()
    print("✓ База данных создана")


def create_admin():
    """Создание администратора"""
    print("\n=== Создание администратора ===")

    username = input("Введите имя пользователя: ").strip()
    if not username:
        print("Ошибка: имя пользователя не может быть пустым")
        sys.exit(1)

    email = input("Введите email: ").strip()
    if not email or "@" not in email:
        print("Ошибка: неверный формат email")
        sys.exit(1)

    password = getpass("Введите пароль: ")
    if len(password) < 6:
        print("Ошибка: пароль должен быть не менее 6 символов")
        sys.exit(1)

    password_confirm = getpass("Подтвердите пароль: ")
    if password != password_confirm:
        print("Ошибка: пароли не совпадают")
        sys.exit(1)

    with app.app_context():
        # Проверяем, не существует ли уже такой пользователь
        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            print(f"Ошибка: пользователь '{username}' уже существует")
            sys.exit(1)

        existing_email = User.query.filter_by(email=email).first()
        if existing_email:
            print(f"Ошибка: email '{email}' уже используется")
            sys.exit(1)

        # Создаем администратора
        admin = User(
            username=username,
            email=email,
            is_admin=True,
            is_active=True
        )
        admin.set_password(password)

        db.session.add(admin)
        db.session.commit()

    print(f"\n✓ Администратор '{username}' успешно создан!")
    print(f"  Email: {email}")
    print(f"  Права: Администратор")


def main():
    """Главная функция"""
    print("╔════════════════════════════════════════╗")
    print("║  WB Seller Platform - Инициализация  ║")
    print("╚════════════════════════════════════════╝")
    print()

    # Проверяем, существует ли уже база данных
    db_path = "seller_platform.db"
    if os.path.exists(db_path):
        response = input(f"База данных '{db_path}' уже существует. Пересоздать? (yes/no): ")
        if response.lower() in ['yes', 'y', 'да', 'д']:
            os.remove(db_path)
            print(f"✓ Файл '{db_path}' удален")
        else:
            print("Инициализация отменена")
            sys.exit(0)

    # Инициализируем базу данных
    init_database()

    # Создаем администратора
    create_admin()

    print("\n" + "="*50)
    print("Платформа успешно инициализирована!")
    print("\nДля запуска приложения выполните:")
    print("  python seller_platform.py")
    print("\nИли используйте Flask CLI:")
    print("  flask --app seller_platform run")
    print("="*50)


if __name__ == "__main__":
    main()
