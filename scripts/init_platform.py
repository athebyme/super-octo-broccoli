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


def create_admin(non_interactive=False):
    """Создание администратора"""
    print("\n=== Создание администратора ===")

    # Проверяем переменные окружения для неинтерактивного режима
    username = os.environ.get('ADMIN_USERNAME')
    email = os.environ.get('ADMIN_EMAIL')
    password = os.environ.get('ADMIN_PASSWORD')

    # Если не в неинтерактивном режиме и переменные не заданы - запрашиваем
    if not non_interactive and not (username and email and password):
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
    elif non_interactive or (username and email and password):
        # Неинтерактивный режим - используем значения по умолчанию или из окружения
        username = username or 'admin'
        email = email or 'admin@example.com'
        password = password or 'admin123'
        print(f"  Используется пользователь: {username}")
        print(f"  Email: {email}")
    else:
        print("Ошибка: для неинтерактивного режима требуются переменные окружения:")
        print("  ADMIN_USERNAME, ADMIN_EMAIL, ADMIN_PASSWORD")
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
    import argparse

    parser = argparse.ArgumentParser(description='Инициализация WB Seller Platform')
    parser.add_argument('--non-interactive', action='store_true',
                        help='Неинтерактивный режим (использует переменные окружения или значения по умолчанию)')
    parser.add_argument('--skip-existing', action='store_true',
                        help='Пропустить если администратор уже существует')
    args = parser.parse_args()

    print("╔════════════════════════════════════════╗")
    print("║  WB Seller Platform - Инициализация  ║")
    print("╚════════════════════════════════════════╝")
    print()

    # Проверяем, существует ли уже база данных
    db_path = "seller_platform.db"
    if os.path.exists(db_path) and not args.non_interactive:
        response = input(f"База данных '{db_path}' уже существует. Пересоздать? (yes/no): ")
        if response.lower() in ['yes', 'y', 'да', 'д']:
            os.remove(db_path)
            print(f"✓ Файл '{db_path}' удален")
        else:
            print("Инициализация отменена")
            sys.exit(0)
    elif os.path.exists(db_path) and args.non_interactive:
        print(f"✓ База данных '{db_path}' уже существует, продолжаем")

    # Инициализируем базу данных
    init_database()

    # Проверяем, есть ли уже администратор
    with app.app_context():
        existing_admin = User.query.filter_by(is_admin=True).first()
        if existing_admin and args.skip_existing:
            print(f"✓ Администратор уже существует: {existing_admin.username}")
            print("Инициализация завершена")
            sys.exit(0)

    # Создаем администратора
    create_admin(non_interactive=args.non_interactive)

    print("\n" + "="*50)
    print("Платформа успешно инициализирована!")
    if not args.non_interactive:
        print("\nДля запуска приложения выполните:")
        print("  python seller_platform.py")
        print("\nИли используйте Flask CLI:")
        print("  flask --app seller_platform run")
    print("="*50)


if __name__ == "__main__":
    main()
