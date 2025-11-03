# Быстрый старт на сервере

## Для нетерпеливых 🚀

```bash
# 1. Клонируйте репозиторий (если еще не сделано)
git clone <repo-url>
cd super-octo-broccoli

# 2. Запустите автоматическую установку
chmod +x setup.sh
./setup.sh

# 3. Активируйте виртуальное окружение
source .venv/bin/activate

# 4. Запустите приложение
python seller_platform.py
```

Откройте в браузере: `http://localhost:5001/login`

**Тестовые данные:**
- Админ: `admin` / `admin123`
- Продавец: `seller1` / `seller123`

---

## Пошаговая установка

### Шаг 1: Установка Python (если не установлен)

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install python3 python3-pip python3-venv

# CentOS/RHEL
sudo yum install python3 python3-pip
```

### Шаг 2: Создание виртуального окружения

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### Шаг 3: Установка зависимостей

```bash
pip install -r requirements.txt
```

### Шаг 4: Инициализация базы данных

**Быстрая инициализация с тестовыми данными:**
```bash
python test_init.py
```

**Или создайте своего администратора:**
```bash
python init_platform.py
```

### Шаг 5: Запуск

**Для разработки:**
```bash
python seller_platform.py
```

**Для production:**
```bash
gunicorn -w 4 -b 0.0.0.0:5001 seller_platform:app
```

---

## Решение проблем

### `python: command not found`

Используйте `python3` вместо `python`:
```bash
python3 seller_platform.py
```

### `No module named 'venv'`

Установите venv:
```bash
sudo apt install python3-venv
```

### `ModuleNotFoundError: No module named 'flask'`

Активируйте виртуальное окружение:
```bash
source .venv/bin/activate
pip install -r requirements.txt
```

### Порт 5001 уже занят

Измените порт:
```bash
export PORT=5002
python seller_platform.py
```

---

## Полная документация

См. [PLATFORM_README.md](PLATFORM_README.md) для подробной информации.
