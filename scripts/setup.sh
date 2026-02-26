#!/bin/bash
# Скрипт быстрой установки WB Seller Platform

set -e  # Остановиться при ошибке

echo "╔════════════════════════════════════════╗"
echo "║  WB Seller Platform - Установка      ║"
echo "╚════════════════════════════════════════╝"
echo ""

# Проверка Python
echo "🔍 Проверка Python..."
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 не установлен!"
    echo "Установите Python3:"
    echo "  Ubuntu/Debian: sudo apt install python3 python3-pip python3-venv"
    echo "  CentOS/RHEL: sudo yum install python3 python3-pip"
    exit 1
fi

PYTHON_VERSION=$(python3 --version)
echo "✓ $PYTHON_VERSION установлен"

# Проверка pip
echo ""
echo "🔍 Проверка pip..."
if ! command -v pip3 &> /dev/null; then
    echo "❌ pip3 не установлен!"
    echo "Установите pip3:"
    echo "  Ubuntu/Debian: sudo apt install python3-pip"
    echo "  CentOS/RHEL: sudo yum install python3-pip"
    exit 1
fi
echo "✓ pip3 установлен"

# Создание виртуального окружения
echo ""
echo "📦 Создание виртуального окружения..."
if [ -d ".venv" ]; then
    echo "⚠️  Виртуальное окружение уже существует"
    read -p "Пересоздать? (y/n): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -rf .venv
        python3 -m venv .venv
        echo "✓ Виртуальное окружение пересоздано"
    fi
else
    python3 -m venv .venv
    echo "✓ Виртуальное окружение создано"
fi

# Активация виртуального окружения
echo ""
echo "🔌 Активация виртуального окружения..."
source .venv/bin/activate
echo "✓ Виртуальное окружение активировано"

# Установка зависимостей
echo ""
echo "📥 Установка зависимостей..."
pip install --upgrade pip > /dev/null 2>&1
pip install -r requirements.txt
echo "✓ Зависимости установлены"

# Инициализация базы данных
echo ""
echo "🗄️  Инициализация базы данных..."
if [ -f "seller_platform.db" ]; then
    echo "⚠️  База данных уже существует"
    read -p "Пересоздать с тестовыми данными? (y/n): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        python scripts/test_init.py
    fi
else
    python scripts/test_init.py
fi

echo ""
echo "╔════════════════════════════════════════╗"
echo "║  ✓ Установка завершена успешно!      ║"
echo "╚════════════════════════════════════════╝"
echo ""
echo "Для запуска приложения выполните:"
echo ""
echo "  1. Активировать виртуальное окружение:"
echo "     source .venv/bin/activate"
echo ""
echo "  2. Запустить приложение:"
echo "     python seller_platform.py"
echo ""
echo "     или с gunicorn (для production):"
echo "     gunicorn -w 4 -b 0.0.0.0:5001 seller_platform:app"
echo ""
echo "  3. Открыть в браузере:"
echo "     http://localhost:5001/login"
echo ""
echo "Тестовые учетные данные:"
echo "  Админ:    admin / admin123"
echo "  Продавец: seller1 / seller123"
echo ""
