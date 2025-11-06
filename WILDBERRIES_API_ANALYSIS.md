# Анализ логики работы с API Wildberries

**Дата анализа:** 2025-11-03
**Проверенные компоненты:** app.py, seller_platform.py, models.py

---

## Исполнительное резюме

**КРИТИЧЕСКАЯ НАХОДКА:** Текущая реализация **НЕ использует API Wildberries**. Приложение работает только с вручную загруженными Excel-файлами отчетов.

---

## 1. Текущее состояние реализации

### 1.1. Что реализовано

#### Приложение калькулятора прибыли (app.py)
- ✅ Загрузка Excel-отчетов Wildberries через веб-форму
- ✅ Обработка данных из колонок: "Обоснование для оплаты", "Артикул поставщика", "Кол-во", "К перечислению Продавцу за реализованный Товар"
- ✅ Парсинг и расчет:
  - Распределение логистики по артикулам
  - Расчет себестоимости (закупка + упаковка 45₽)
  - Расчет прибыли и маржи
  - Обработка штрафов и хранения
- ✅ Генерация Excel-отчетов с итоговыми показателями

#### Платформа продавцов (seller_platform.py)
- ✅ Система авторизации (Flask-Login)
- ✅ Управление пользователями и продавцами
- ✅ Разделение прав (админ/продавец)
- ⚠️ Поле `wb_api_key` в БД (модель Seller) — **НЕ ИСПОЛЬЗУЕТСЯ**

### 1.2. Что отсутствует

- ❌ Интеграция с API Wildberries
- ❌ Автоматическая загрузка отчетов через API
- ❌ Получение статистики продаж через API
- ❌ Синхронизация данных о товарах
- ❌ Работа с заказами через API
- ❌ Получение накладных через API

---

## 2. Официальное API Wildberries

### 2.1. Документация

**Основной портал:** https://dev.wildberries.ru/

**Доступные разделы API:**
- Content (товары, цены, остатки)
- Analytics (аналитика, статистика)
- Statistics (продажи, заказы, остатки)
- Reports (отчеты о реализации)
- Financial Reports (финансовые отчеты)
- Marketplace (управление магазином)
- Advertising (реклама)

### 2.2. Аутентификация

**Метод:** API Token в заголовке `Authorization`

```http
Authorization: {your_api_token}
```

**Типы токенов:**
- Content — управление карточками товаров
- Statistics — доступ к статистике и аналитике
- Orders — управление заказами и поставками
- Advertising — управление рекламными кампаниями

**Срок действия:**
- Обычный токен: 180 дней
- OAuth токен: 12 часов (access) + 30 дней (refresh)

**Получение токена:**
1. Личный кабинет продавца → Профиль
2. Настройки → Доступ к API
3. Выбрать тип токена → Создать токен
4. Скопировать (показывается только один раз!)

### 2.3. Ключевые эндпоинты для текущего функционала

#### Статистика продаж (Statistics API)
```
GET https://statistics-api.wildberries.ru/api/v1/supplier/sales
```
- Данные о продажах
- Обновление каждые 30 минут
- Хранение 90 дней

#### Отчеты о реализации (Analytics API)
```
GET https://statistics-api.wildberries.ru/api/v1/supplier/reportDetailByPeriod
```
- Детализированный отчет по периоду
- Включает логистику, штрафы, хранение
- Данные с 29 января 2024 года

#### Заказы (Statistics API)
```
GET https://statistics-api.wildberries.ru/api/v1/supplier/orders
```
- Данные о заказах
- Обновление каждые 30 минут

#### Остатки товаров (Statistics API)
```
GET https://statistics-api.wildberries.ru/api/v1/supplier/stocks
```
- Информация об остатках на складах WB
- Обновление каждые 30 минут

### 2.4. Sandbox-окружение

**Тестовый сервер:** `statistics-api-sandbox.wildberries.ru`
**Продакшн:** `statistics-api.wildberries.ru`

---

## 3. Проблемы текущей реализации

### 3.1. Критические проблемы

#### ❌ Отсутствие API-интеграции
**Проблема:** Данные не загружаются автоматически из Wildberries
**Последствия:**
- Ручная работа для каждого отчета
- Риск ошибок при выгрузке файлов
- Отсутствие real-time данных
- Невозможность автоматизации

**Файлы с проблемой:** `app.py`, `seller_platform.py`

#### ❌ Неиспользуемое поле wb_api_key
**Проблема:** В модели `Seller` есть поле `wb_api_key`, но оно нигде не используется
**Местоположение:** `models.py:47`

```python
wb_api_key = db.Column(db.String(500))  # API ключ WB (опционально)
```

**Последствия:**
- Введение в заблуждение (создается впечатление, что API интегрирован)
- Ключи могут храниться, но не применяются

#### ❌ Отсутствие валидации данных из Excel
**Проблема:** Нет проверки, что загруженный файл действительно является отчетом WB
**Местоположение:** `app.py:71-72`, `app.py:172-179`

**Текущая логика:**
```python
def read_statistics(path: Path) -> pd.DataFrame:
    return pd.read_excel(path)  # Нет валидации формата
```

**Последствия:**
- Пользователь может загрузить неправильный файл
- Ошибки обработки возникают на поздних этапах

### 3.2. Функциональные ограничения

#### ⚠️ Зависимость от ручной выгрузки
- Пользователь должен вручную заходить в ЛК WB
- Скачивать отчет в нужном формате
- Загружать через веб-форму

#### ⚠️ Нет актуальных данных
- Данные обновляются только при ручной загрузке
- Невозможно получить данные в real-time
- WB API предоставляет данные каждые 30 минут

#### ⚠️ Нет автоматизации
- Невозможно настроить автоматическую генерацию отчетов
- Нет cron-задач для периодического обновления
- Скрипт `scripts/run_report.py` работает только с уже загруженными файлами

---

## 4. Рекомендации по внедрению API

### 4.1. Приоритет 1: Базовая интеграция

#### Реализовать получение отчетов через API

**Новый модуль:** `wb_api_client.py`

```python
import requests
from typing import Dict, List, Optional
from datetime import datetime

class WildberriesAPIClient:
    """Клиент для работы с API Wildberries"""

    BASE_URL = "https://statistics-api.wildberries.ru/api/v1"
    SANDBOX_URL = "https://statistics-api-sandbox.wildberries.ru/api/v1"

    def __init__(self, api_key: str, sandbox: bool = False):
        self.api_key = api_key
        self.base_url = self.SANDBOX_URL if sandbox else self.BASE_URL
        self.headers = {
            "Authorization": api_key
        }

    def get_sales_report(self, date_from: str, date_to: str) -> List[Dict]:
        """
        Получить отчет о продажах за период

        Args:
            date_from: Дата начала в формате YYYY-MM-DD
            date_to: Дата окончания в формате YYYY-MM-DD

        Returns:
            Список продаж
        """
        endpoint = f"{self.base_url}/supplier/reportDetailByPeriod"
        params = {
            "dateFrom": date_from,
            "dateTo": date_to
        }

        response = requests.get(
            endpoint,
            headers=self.headers,
            params=params,
            timeout=30
        )
        response.raise_for_status()
        return response.json()

    def get_orders(self, date_from: str) -> List[Dict]:
        """Получить заказы с указанной даты"""
        endpoint = f"{self.base_url}/supplier/orders"
        params = {"dateFrom": date_from}

        response = requests.get(
            endpoint,
            headers=self.headers,
            params=params,
            timeout=30
        )
        response.raise_for_status()
        return response.json()

    def get_stocks(self, date_from: str) -> List[Dict]:
        """Получить остатки товаров"""
        endpoint = f"{self.base_url}/supplier/stocks"
        params = {"dateFrom": date_from}

        response = requests.get(
            endpoint,
            headers=self.headers,
            params=params,
            timeout=30
        )
        response.raise_for_status()
        return response.json()
```

#### Интегрировать в seller_platform.py

**Добавить маршрут для автоматической загрузки:**

```python
@app.route('/reports/sync', methods=['POST'])
@login_required
def sync_reports():
    """Синхронизация отчетов через API WB"""
    if not current_user.seller or not current_user.seller.wb_api_key:
        flash('API ключ Wildberries не настроен', 'warning')
        return redirect(url_for('dashboard'))

    try:
        from wb_api_client import WildberriesAPIClient

        client = WildberriesAPIClient(current_user.seller.wb_api_key)

        # Получить данные за последние 30 дней
        date_from = (datetime.utcnow() - timedelta(days=30)).strftime('%Y-%m-%d')
        date_to = datetime.utcnow().strftime('%Y-%m-%d')

        sales_data = client.get_sales_report(date_from, date_to)

        # Конвертировать в DataFrame и сохранить
        df = pd.DataFrame(sales_data)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = UPLOAD_DIR / f"wb_api_report_{timestamp}.xlsx"
        df.to_excel(output_path, index=False)

        flash(f'Отчет успешно загружен через API ({len(sales_data)} записей)', 'success')

    except requests.HTTPError as e:
        if e.response.status_code == 401:
            flash('Ошибка авторизации: проверьте API ключ', 'danger')
        else:
            flash(f'Ошибка API WB: {e}', 'danger')
    except Exception as e:
        flash(f'Ошибка синхронизации: {str(e)}', 'danger')

    return redirect(url_for('dashboard'))
```

#### Добавить интерфейс настройки API ключа

**Маршрут редактирования профиля продавца:**

```python
@app.route('/profile/api-settings', methods=['GET', 'POST'])
@login_required
def api_settings():
    """Настройка API ключей Wildberries"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'danger')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        wb_api_key = request.form.get('wb_api_key', '').strip()

        # Проверить валидность ключа
        try:
            from wb_api_client import WildberriesAPIClient
            client = WildberriesAPIClient(wb_api_key)
            # Проверочный запрос
            date_from = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')
            client.get_orders(date_from)

            # Ключ валиден — сохраняем
            current_user.seller.wb_api_key = wb_api_key
            db.session.commit()

            flash('API ключ успешно сохранен и проверен', 'success')
        except Exception as e:
            flash(f'Ошибка проверки API ключа: {str(e)}', 'danger')

    return render_template('api_settings.html', seller=current_user.seller)
```

### 4.2. Приоритет 2: Автоматизация

#### Cron-задача для периодической загрузки

**Новый файл:** `scripts/sync_wb_reports.py`

```python
#!/usr/bin/env python3
"""
Скрипт для автоматической синхронизации отчетов WB через API
Использование: python scripts/sync_wb_reports.py
Для cron: 0 */6 * * * cd /path/to/app && python scripts/sync_wb_reports.py
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta

# Добавить родительскую директорию в путь
sys.path.insert(0, str(Path(__file__).parent.parent))

from seller_platform import app, db
from models import Seller
from wb_api_client import WildberriesAPIClient
import pandas as pd

def sync_all_sellers():
    """Синхронизировать отчеты для всех активных продавцов"""
    with app.app_context():
        sellers = Seller.query.join(Seller.user).filter(
            Seller.wb_api_key.isnot(None),
            Seller.user.has(is_active=True)
        ).all()

        print(f"Найдено {len(sellers)} продавцов с API ключами")

        for seller in sellers:
            try:
                print(f"Синхронизация для {seller.company_name}...")

                client = WildberriesAPIClient(seller.wb_api_key)

                # Получить данные за последние 7 дней
                date_from = (datetime.utcnow() - timedelta(days=7)).strftime('%Y-%m-%d')
                date_to = datetime.utcnow().strftime('%Y-%m-%d')

                sales_data = client.get_sales_report(date_from, date_to)

                if sales_data:
                    # Сохранить в файл
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    output_path = Path("uploads") / f"wb_api_{seller.id}_{timestamp}.xlsx"
                    df = pd.DataFrame(sales_data)
                    df.to_excel(output_path, index=False)

                    print(f"✓ Загружено {len(sales_data)} записей для {seller.company_name}")
                else:
                    print(f"⚠ Нет данных для {seller.company_name}")

            except Exception as e:
                print(f"✗ Ошибка для {seller.company_name}: {str(e)}")

if __name__ == "__main__":
    sync_all_sellers()
```

### 4.3. Приоритет 3: Расширенная функциональность

- Кэширование API-запросов (Redis)
- Rate limiting для соблюдения лимитов WB API
- Webhook-уведомления при появлении новых данных
- Графики и визуализация в реальном времени
- Экспорт данных в различные форматы (CSV, JSON, PDF)

---

## 5. Безопасность API ключей

### 5.1. Текущие риски

⚠️ **Хранение в открытом виде**
API ключи хранятся в БД без шифрования (`models.py:47`)

```python
wb_api_key = db.Column(db.String(500))  # Хранится в открытом виде!
```

### 5.2. Рекомендуемые улучшения

#### Шифрование ключей в БД

```python
from cryptography.fernet import Fernet
import os

class Seller(db.Model):
    # ...
    _wb_api_key_encrypted = db.Column('wb_api_key', db.String(500))

    @property
    def wb_api_key(self) -> Optional[str]:
        """Расшифровать API ключ"""
        if not self._wb_api_key_encrypted:
            return None

        key = os.environ.get('ENCRYPTION_KEY').encode()
        f = Fernet(key)
        return f.decrypt(self._wb_api_key_encrypted.encode()).decode()

    @wb_api_key.setter
    def wb_api_key(self, value: Optional[str]) -> None:
        """Зашифровать API ключ"""
        if value is None:
            self._wb_api_key_encrypted = None
            return

        key = os.environ.get('ENCRYPTION_KEY').encode()
        f = Fernet(key)
        self._wb_api_key_encrypted = f.encrypt(value.encode()).decode()
```

#### Переменные окружения

```bash
# .env
ENCRYPTION_KEY=your-generated-fernet-key-here
WB_API_RATE_LIMIT=100  # запросов в минуту
```

#### Логирование API-запросов

```python
import logging

logger = logging.getLogger('wb_api')

class WildberriesAPIClient:
    def _make_request(self, method: str, endpoint: str, **kwargs):
        logger.info(f"WB API Request: {method} {endpoint}")
        try:
            response = requests.request(method, endpoint, **kwargs)
            logger.info(f"WB API Response: {response.status_code}")
            return response
        except Exception as e:
            logger.error(f"WB API Error: {str(e)}")
            raise
```

---

## 6. Тестирование API-интеграции

### 6.1. Unit-тесты

**Новый файл:** `tests/test_wb_api_client.py`

```python
import pytest
from unittest.mock import Mock, patch
from wb_api_client import WildberriesAPIClient

def test_api_client_initialization():
    """Тест инициализации клиента"""
    client = WildberriesAPIClient("test_api_key", sandbox=True)
    assert client.api_key == "test_api_key"
    assert "sandbox" in client.base_url

@patch('wb_api_client.requests.get')
def test_get_sales_report_success(mock_get):
    """Тест успешного получения отчета"""
    mock_response = Mock()
    mock_response.json.return_value = [{"sale_id": 1, "amount": 100}]
    mock_response.status_code = 200
    mock_get.return_value = mock_response

    client = WildberriesAPIClient("test_key")
    result = client.get_sales_report("2024-01-01", "2024-01-31")

    assert len(result) == 1
    assert result[0]["sale_id"] == 1

@patch('wb_api_client.requests.get')
def test_get_sales_report_auth_error(mock_get):
    """Тест ошибки авторизации"""
    mock_response = Mock()
    mock_response.status_code = 401
    mock_response.raise_for_status.side_effect = requests.HTTPError()
    mock_get.return_value = mock_response

    client = WildberriesAPIClient("invalid_key")

    with pytest.raises(requests.HTTPError):
        client.get_sales_report("2024-01-01", "2024-01-31")
```

### 6.2. Интеграционные тесты

Использовать sandbox-окружение WB для тестирования реальных API-вызовов:

```python
# Использовать тестовый ключ из sandbox
TEST_API_KEY = os.environ.get('WB_SANDBOX_API_KEY')

@pytest.mark.integration
def test_real_api_call():
    """Тест реального API-вызова к sandbox"""
    if not TEST_API_KEY:
        pytest.skip("WB_SANDBOX_API_KEY not set")

    client = WildberriesAPIClient(TEST_API_KEY, sandbox=True)
    result = client.get_sales_report("2024-01-01", "2024-01-31")

    assert isinstance(result, list)
```

---

## 7. План внедрения

### Этап 1: Подготовка (1-2 дня)
- [ ] Изучить полную документацию WB API
- [ ] Получить тестовые API ключи для sandbox
- [ ] Создать тестовый аккаунт продавца в WB
- [ ] Настроить окружение для разработки

### Этап 2: Базовая интеграция (3-5 дней)
- [ ] Создать модуль `wb_api_client.py` с базовыми методами
- [ ] Добавить интерфейс настройки API ключей
- [ ] Реализовать маршрут `/reports/sync` для ручной синхронизации
- [ ] Добавить валидацию API ключей при сохранении
- [ ] Написать unit-тесты для API-клиента

### Этап 3: Интеграция с существующей логикой (2-3 дня)
- [ ] Модифицировать `app.py` для работы с данными из API
- [ ] Конвертировать JSON-ответы API в формат, совместимый с текущей логикой
- [ ] Обеспечить обратную совместимость с Excel-загрузкой
- [ ] Добавить маппинг полей API → колонки Excel

### Этап 4: Автоматизация (2-3 дня)
- [ ] Создать cron-скрипт для периодической синхронизации
- [ ] Добавить настройки расписания в админ-панель
- [ ] Реализовать логирование API-запросов
- [ ] Добавить уведомления при ошибках синхронизации

### Этап 5: Безопасность (1-2 дня)
- [ ] Внедрить шифрование API ключей в БД
- [ ] Добавить rate limiting для API-запросов
- [ ] Настроить логирование всех API-операций
- [ ] Провести аудит безопасности

### Этап 6: Тестирование и развертывание (3-5 дней)
- [ ] Полное тестирование в sandbox-окружении
- [ ] Интеграционные тесты с реальными данными
- [ ] Нагрузочное тестирование
- [ ] Документация для пользователей
- [ ] Развертывание на production
- [ ] Мониторинг и сбор метрик

**Общая длительность:** 12-20 рабочих дней

---

## 8. Зависимости для добавления

Обновить `requirements.txt`:

```txt
# Существующие зависимости
Flask==3.0.3
pandas==2.2.3
openpyxl==3.1.5
python-dateutil==2.9.0.post0
tzdata==2024.1
requests==2.32.3
gunicorn==21.2.0
Flask-Login==0.6.3
Flask-SQLAlchemy==3.1.1
Werkzeug==3.0.3

# Новые зависимости для API-интеграции
cryptography==41.0.7      # Шифрование API ключей
redis==5.0.1              # Кэширование API-запросов
celery==5.3.4             # Асинхронные задачи
pytest==7.4.3             # Тестирование
pytest-mock==3.12.0       # Мокирование для тестов
requests-mock==1.11.0     # Мокирование HTTP-запросов
python-dotenv==1.0.0      # Переменные окружения
```

---

## 9. Выводы и рекомендации

### 9.1. Критические выводы

1. **Нет API-интеграции:** Текущая реализация полностью игнорирует API Wildberries и работает только с Excel-файлами
2. **Техдолг:** Поле `wb_api_key` добавлено в модели, но нигде не используется
3. **Ручная работа:** Пользователи вынуждены вручную выгружать отчеты из WB и загружать в систему

### 9.2. Немедленные действия

**Приоритет ВЫСОКИЙ:**
1. Реализовать модуль `WildberriesAPIClient` для базовых API-запросов
2. Добавить интерфейс настройки API ключей в профиле продавца
3. Создать маршрут для ручной синхронизации отчетов через API

**Приоритет СРЕДНИЙ:**
4. Внедрить автоматическую периодическую синхронизацию (cron)
5. Добавить шифрование API ключей в БД
6. Написать unit и интеграционные тесты

**Приоритет НИЗКИЙ:**
7. Добавить расширенную аналитику с использованием real-time данных
8. Интегрировать webhook-уведомления
9. Создать визуализацию данных с графиками

### 9.3. Долгосрочная стратегия

- Полный переход на API как основной источник данных
- Excel-загрузка как запасной вариант (legacy)
- Кэширование данных для снижения нагрузки на API
- Мониторинг и алертинг при сбоях API
- Регулярное обновление в соответствии с изменениями WB API

---

## 10. Полезные ссылки

- **Официальная документация WB API:** https://dev.wildberries.ru/
- **OpenAPI спецификация (Swagger):** https://dev.wildberries.ru/swagger/
- **Statistics API:** https://openapi.wildberries.ru/statistics/api/ru/
- **Sandbox для тестирования:** https://statistics-api-sandbox.wildberries.ru/
- **FAQ по интеграции:** https://dev.wildberries.ru/en/faq
- **GitHub примеры:** https://github.com/topics/wildberries-api

---

**Подготовил:** Claude (AI Assistant)
**Контакт для вопросов:** См. GitHub Issues проекта

---

## Appendix A: Пример структуры ответа WB API

### Отчет о продажах (reportDetailByPeriod)

```json
[
  {
    "realizationreport_id": 123456,
    "date_from": "2024-01-01T00:00:00Z",
    "date_to": "2024-01-31T23:59:59Z",
    "suppliercontract_code": "CONTRACT123",
    "rrd_id": 78910,
    "gi_id": 0,
    "subject_name": "Футболка",
    "nm_id": 98765432,
    "brand_name": "MyBrand",
    "sa_name": "Футболка женская хлопок",
    "ts_name": "44",
    "barcode": "2000012345678",
    "doc_type_name": "Продажа",
    "quantity": 1,
    "retail_price": 1500.00,
    "retail_amount": 1500.00,
    "sale_percent": 10,
    "commission_percent": 15.0,
    "office_name": "Коледино",
    "supplier_oper_name": "Продажа",
    "order_dt": "2024-01-15T12:30:00Z",
    "sale_dt": "2024-01-16T10:20:00Z",
    "rr_dt": "2024-01-20T00:00:00Z",
    "shk_id": 123456789,
    "retail_price_withdisc_rub": 1350.00,
    "delivery_amount": 0,
    "return_amount": 0,
    "delivery_rub": 50.00,
    "gi_box_type_name": "Без коробов",
    "product_discount_for_report": 10.00,
    "supplier_promo": 0,
    "rid": 111222333,
    "ppvz_spp_prc": 10.00,
    "ppvz_kvw_prc_base": 15.00,
    "ppvz_kvw_prc": 15.00,
    "sup_rating_prc_up": 0,
    "is_kgvp_v2": 0,
    "ppvz_sales_commission": 202.50,
    "ppvz_for_pay": 1097.50,
    "ppvz_reward": 0,
    "acquiring_fee": 13.50,
    "acquiring_bank": "Сбербанк",
    "ppvz_vw": 50.00,
    "ppvz_vw_nds": 10.00,
    "ppvz_office_id": 100,
    "ppvz_office_name": "Коледино",
    "ppvz_supplier_id": 12345,
    "ppvz_supplier_name": "ООО Поставщик",
    "ppvz_inn": "1234567890",
    "declaration_number": "DECL123456",
    "bonus_type_name": "",
    "sticker_id": "WB987654",
    "site_country": "Россия",
    "penalty": 0,
    "additional_payment": 0,
    "rebill_logistic_cost": 0,
    "rebill_logistic_org": "",
    "kiz": "",
    "storage_fee": 5.00,
    "deduction": 0,
    "acceptance": 0
  }
]
```

### Маппинг полей API → Excel

| Поле WB API | Колонка Excel | Примечание |
|-------------|---------------|------------|
| `barcode` | Баркод | Штрихкод товара |
| `sa_name` | Артикул поставщика | Название товара |
| `quantity` | Кол-во | Количество проданных единиц |
| `ppvz_for_pay` | К перечислению Продавцу за реализованный Товар | Сумма к выплате |
| `doc_type_name` | Обоснование для оплаты | Тип операции (Продажа/Логистика/Штраф) |
| `delivery_rub` | Услуги по доставке товара покупателю | Стоимость логистики |
| `nm_id` | Код номенклатуры | ID товара в системе WB |
| `penalty` | Общая сумма штрафов | Штрафы от WB |
| `storage_fee` | Хранение | Плата за хранение |

---

**КОНЕЦ ОТЧЕТА**
