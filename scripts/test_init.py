#!/usr/bin/env python3
"""
Тестовый скрипт для быстрой инициализации платформы с тестовыми данными
"""
import os
from seller_platform import app, db
from models import User, Seller


def init_test_platform():
    """Инициализация с тестовыми данными"""
    print("Инициализация тестовой платформы...")

    # Удаляем старую БД если есть
    db_path = "seller_platform.db"
    if os.path.exists(db_path):
        os.remove(db_path)
        print(f"✓ Удален старый файл БД: {db_path}")

    # Создаем БД
    with app.app_context():
        db.create_all()
        print("✓ База данных создана")

        # Создаем администратора
        admin = User(
            username="admin",
            email="admin@example.com",
            is_admin=True,
            is_active=True
        )
        admin.set_password("admin123")
        db.session.add(admin)
        print("✓ Создан администратор (admin / admin123)")

        # Создаем тестового продавца
        seller_user = User(
            username="seller1",
            email="seller1@example.com",
            is_admin=False,
            is_active=True
        )
        seller_user.set_password("seller123")
        db.session.add(seller_user)
        db.session.flush()

        seller = Seller(
            user_id=seller_user.id,
            company_name="ООО 'Тестовая компания'",
            contact_phone="+7 (999) 123-45-67",
            wb_seller_id="12345678",
            notes="Тестовый продавец для демонстрации"
        )
        db.session.add(seller)
        print("✓ Создан тестовый продавец (seller1 / seller123)")

        # Создаем еще одного продавца
        seller_user2 = User(
            username="seller2",
            email="seller2@example.com",
            is_admin=False,
            is_active=True
        )
        seller_user2.set_password("seller123")
        db.session.add(seller_user2)
        db.session.flush()

        seller2 = Seller(
            user_id=seller_user2.id,
            company_name="ИП Иванов И.И.",
            contact_phone="+7 (999) 987-65-43",
            wb_seller_id="87654321",
            notes="Второй тестовый продавец"
        )
        db.session.add(seller2)
        print("✓ Создан второй тестовый продавец (seller2 / seller123)")

        db.session.commit()

    print("\n" + "="*60)
    print("✓ Тестовая платформа успешно инициализирована!")
    print("\nУчетные данные для входа:")
    print("\nАдминистратор:")
    print("  Username: admin")
    print("  Password: admin123")
    print("\nПродавец 1:")
    print("  Username: seller1")
    print("  Password: seller123")
    print("  Компания: ООО 'Тестовая компания'")
    print("\nПродавец 2:")
    print("  Username: seller2")
    print("  Password: seller123")
    print("  Компания: ИП Иванов И.И.")
    print("\nДля запуска приложения:")
    print("  python seller_platform.py")
    print("\nОткройте в браузере:")
    print("  http://localhost:5001/login")
    print("="*60)


if __name__ == "__main__":
    init_test_platform()
