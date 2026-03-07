# Архитектура раздела Аналитики — Мультимаркетплейс Платформа

## 1. Обзор

Раздел аналитики предоставляет продавцам единый дашборд с данными по всем подключённым маркетплейсам (WB, Ozon, Яндекс Маркет и др.). Архитектура спроектирована так, чтобы:

- Данные из разных маркетплейсов хранились в **унифицированных таблицах** с колонкой `marketplace_id`
- API-клиенты для каждого маркетплейса реализовывали **общий интерфейс** (`BaseMarketplaceAnalyticsClient`)
- UI показывал как **сводную аналитику** по всем маркетплейсам, так и **детализацию** по каждому

---

## 2. Источники данных WB API

### 2.1 Analytics API (`seller-analytics-api.wildberries.ru`)

| Эндпоинт | Метод | Описание | Частота обновления |
|---|---|---|---|
| `/api/analytics/v3/sales-funnel/products` | POST | Статистика карточек за период (воронка продаж) | 1 час |
| `/api/analytics/v3/sales-funnel/products/history` | POST | Статистика карточек по дням/неделям (макс. 7 дней) | 1 час |
| `/api/analytics/v3/sales-funnel/grouped/history` | POST | Группированная статистика по брендам/категориям/тегам | 1 час |
| `/api/v2/nm-report/downloads` | POST | Создание CSV-отчёта расширенной аналитики | — |
| `/api/v2/nm-report/downloads` | GET | Список отчётов и их статусы | — |
| `/api/v2/nm-report/downloads/file/{id}` | GET | Скачивание готового CSV-отчёта (ZIP) | — |
| `/api/v2/nm-report/downloads/retry` | POST | Повторная генерация отчёта | — |
| `/api/v2/search-report/report` | POST | Отчёт по поисковым запросам (главная страница) | — |
| `/api/v2/search-report/table/groups` | POST | Пагинация по группам поисковых запросов | — |
| `/api/v2/search-report/table/details` | POST | Пагинация по товарам внутри группы | — |
| `/api/v2/search-report/product/search-texts` | POST | Топ поисковых запросов по товару | — |
| `/api/v2/search-report/product/orders` | POST | Заказы и позиции по поисковым запросам товара | — |
| `/api/v2/stocks-report/products/groups` | POST | Остатки по группам товаров | — |
| `/api/v2/stocks-report/products/products` | POST | Остатки по отдельным товарам | — |
| `/api/v2/stocks-report/products/sizes` | POST | Остатки по размерам товара | — |
| `/api/v2/stocks-report/offices` | POST | Остатки по складам | — |

### 2.2 Reports/Statistics API (`statistics-api.wildberries.ru`)

| Эндпоинт | Метод | Описание | Частота обновления |
|---|---|---|---|
| `/api/v1/supplier/orders` | GET | Заказы (обновление каждые 30 мин, хранение 90 дней) | 30 мин |
| `/api/v1/supplier/sales` | GET | Продажи и возвраты (30 мин, 90 дней) | 30 мин |
| `/api/v1/supplier/stocks` | GET | Остатки на складах WB (реальное время) | real-time |
| `/api/v5/supplier/reportDetailByPeriod` | GET | Детализация еженедельного финансового отчёта | еженедельно |

### 2.3 Reports — Retention & Finance (`seller-analytics-api.wildberries.ru`)

| Эндпоинт | Метод | Описание |
|---|---|---|
| `/api/v1/analytics/region-sale` | GET | Продажи по регионам (макс. 31 день) |
| `/api/v1/analytics/brand-share` | GET | Доля бренда в продажах (до 365 дней) |
| `/api/v1/analytics/goods-return` | GET | Возвраты товаров (макс. 31 день) |
| `/api/v1/analytics/excise-report` | POST | Маркированные товары |
| `/api/v1/analytics/antifraud-details` | GET | Самовыкупы |
| `/api/v1/analytics/goods-labeling` | GET | Штрафы за маркировку |
| `/api/analytics/v1/measurement-penalties` | GET | Штрафы за габариты |
| `/api/analytics/v1/deductions` | GET | Удержания и пересортица |
| `/api/v1/warehouse_remains` | GET | Остатки на складах (задача) |
| `/api/v1/paid_storage` | GET | Платное хранение (задача) |
| `/api/v1/acceptance_report` | GET | Платная приёмка (задача) |
| `/api/v1/analytics/banned-products/blocked` | GET | Заблокированные карточки |
| `/api/v1/analytics/banned-products/shadowed` | GET | Скрытые из каталога товары |

### 2.4 Promotion API (`advert-api.wildberries.ru`)

| Эндпоинт | Метод | Описание |
|---|---|---|
| `/adv/v1/promotion/count` | GET | Список кампаний по типам и статусам |
| `/api/advert/v2/adverts` | GET | Информация о кампаниях |
| `/adv/v2/seacat/save-ad` | POST | Создание кампании |
| `/adv/v0/start`, `/pause`, `/stop` | GET | Управление кампанией |
| `/api/advert/v1/bids` | PATCH | Управление ставками |
| `/adv/v1/budget/deposit` | POST | Пополнение бюджета |
| `/adv/v1/balance` | GET | Баланс (бонусы, кешбэк) |
| `/adv/v0/normquery/stats` | POST | Статистика по кластерам поиска |

### 2.5 Feedback API (`feedbacks-api.wildberries.ru`)

| Эндпоинт | Метод | Описание |
|---|---|---|
| `/api/v1/feedbacks` | GET | Список отзывов |
| `/api/v1/feedbacks/count-unanswered` | GET | Количество неотвеченных отзывов |
| `/api/v1/feedbacks/count` | GET | Общее количество отзывов |
| `/api/v1/questions` | GET | Список вопросов |
| `/api/v1/questions/count-unanswered` | GET | Количество неотвеченных вопросов |
| `/api/v1/new-feedbacks-questions` | GET | Проверка новых отзывов и вопросов |

### 2.6 Finance API (`finance-api.wildberries.ru`)

| Эндпоинт | Метод | Описание |
|---|---|---|
| `/api/v1/account/balance` | GET | Баланс продавца |

---

## 3. Модели данных (models.py)

### 3.1 Принцип: маркетплейс-агностичные таблицы

Все аналитические таблицы привязаны к `seller_id` + `marketplace_id`. Это позволяет:
- Хранить данные из разных маркетплейсов в единой структуре
- Строить сводные отчёты через `GROUP BY marketplace_id`
- Добавлять новые маркетплейсы без миграций

### 3.2 Новые модели

```python
# ============================================================
# АНАЛИТИКА — ЗАКАЗЫ И ПРОДАЖИ
# ============================================================

class AnalyticsOrder(db.Model):
    """Заказы с маркетплейсов (унифицированная таблица)"""
    __tablename__ = 'analytics_orders'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)

    # Идентификаторы
    external_id = db.Column(db.String(100), nullable=False)  # srid (WB), posting_number (Ozon)
    order_date = db.Column(db.DateTime, nullable=False, index=True)
    last_change_date = db.Column(db.DateTime)

    # Товар
    nm_id = db.Column(db.BigInteger, index=True)          # nmID (WB) / sku (Ozon)
    vendor_code = db.Column(db.String(200))
    product_name = db.Column(db.String(500))
    brand = db.Column(db.String(200))
    category = db.Column(db.String(300))
    barcode = db.Column(db.String(100))
    size = db.Column(db.String(50))

    # Финансы
    price_full = db.Column(db.Numeric(12, 2))             # Полная цена
    price_with_discount = db.Column(db.Numeric(12, 2))     # Цена со скидкой
    discount_percent = db.Column(db.Numeric(5, 2))
    spp = db.Column(db.Numeric(5, 2))                      # Скидка WB (SPP)
    quantity = db.Column(db.Integer, default=1)
    currency = db.Column(db.String(10), default='RUB')

    # Логистика
    warehouse_name = db.Column(db.String(200))
    region = db.Column(db.String(200))
    country = db.Column(db.String(100))
    delivery_type = db.Column(db.String(50))                # FBO / FBS

    # Статус
    status = db.Column(db.String(50))                       # ordered/delivered/cancelled/returned
    cancel_reason = db.Column(db.String(300))

    # Мета
    raw_data_json = db.Column(db.Text)                      # Полный JSON ответа (для дебага)
    synced_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('seller_id', 'marketplace_id', 'external_id', name='uq_analytics_order'),
        db.Index('idx_ao_seller_mp_date', 'seller_id', 'marketplace_id', 'order_date'),
        db.Index('idx_ao_seller_nm', 'seller_id', 'nm_id'),
        db.Index('idx_ao_status', 'seller_id', 'marketplace_id', 'status'),
    )


class AnalyticsSale(db.Model):
    """Продажи и возвраты (фактические выкупы)"""
    __tablename__ = 'analytics_sales'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)

    # Идентификаторы
    external_id = db.Column(db.String(100), nullable=False)  # srid (WB)
    sale_date = db.Column(db.DateTime, nullable=False, index=True)
    last_change_date = db.Column(db.DateTime)

    # Товар
    nm_id = db.Column(db.BigInteger, index=True)
    vendor_code = db.Column(db.String(200))
    product_name = db.Column(db.String(500))
    brand = db.Column(db.String(200))
    category = db.Column(db.String(300))
    barcode = db.Column(db.String(100))
    size = db.Column(db.String(50))

    # Финансы
    total_price = db.Column(db.Numeric(12, 2))
    discount_percent = db.Column(db.Numeric(5, 2))
    spp = db.Column(db.Numeric(5, 2))
    for_pay = db.Column(db.Numeric(12, 2))                 # К перечислению продавцу
    finished_price = db.Column(db.Numeric(12, 2))           # Фактическая цена выкупа
    price_with_disc = db.Column(db.Numeric(12, 2))
    quantity = db.Column(db.Integer, default=1)
    currency = db.Column(db.String(10), default='RUB')

    # Тип операции
    sale_type = db.Column(db.String(20))                    # sale / return
    order_type = db.Column(db.String(50))                   # Тип заказа

    # Логистика
    warehouse_name = db.Column(db.String(200))
    region = db.Column(db.String(200))
    country = db.Column(db.String(100))

    # Мета
    raw_data_json = db.Column(db.Text)
    synced_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('seller_id', 'marketplace_id', 'external_id', name='uq_analytics_sale'),
        db.Index('idx_as_seller_mp_date', 'seller_id', 'marketplace_id', 'sale_date'),
        db.Index('idx_as_sale_type', 'seller_id', 'marketplace_id', 'sale_type'),
    )


# ============================================================
# АНАЛИТИКА — ВОРОНКА ПРОДАЖ (КАРТОЧКИ ТОВАРОВ)
# ============================================================

class AnalyticsProductCard(db.Model):
    """Ежедневная статистика карточек товаров (воронка продаж)"""
    __tablename__ = 'analytics_product_cards'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)

    nm_id = db.Column(db.BigInteger, nullable=False, index=True)
    vendor_code = db.Column(db.String(200))
    product_name = db.Column(db.String(500))
    brand = db.Column(db.String(200))
    category = db.Column(db.String(300))

    dt = db.Column(db.Date, nullable=False, index=True)     # Дата статистики

    # Воронка
    open_card_count = db.Column(db.Integer, default=0)       # Переходы в карточку
    add_to_cart_count = db.Column(db.Integer, default=0)     # Добавления в корзину
    orders_count = db.Column(db.Integer, default=0)          # Заказы
    orders_sum = db.Column(db.Numeric(12, 2), default=0)     # Сумма заказов
    buyouts_count = db.Column(db.Integer, default=0)         # Выкупы
    buyouts_sum = db.Column(db.Numeric(12, 2), default=0)    # Сумма выкупов
    cancel_count = db.Column(db.Integer, default=0)          # Отмены
    cancel_sum = db.Column(db.Numeric(12, 2), default=0)     # Сумма отмен
    add_to_wishlist = db.Column(db.Integer, default=0)       # В избранное

    # Конверсии
    add_to_cart_conversion = db.Column(db.Numeric(5, 2))     # Карточка → Корзина (%)
    cart_to_order_conversion = db.Column(db.Numeric(5, 2))   # Корзина → Заказ (%)
    buyout_percent = db.Column(db.Numeric(5, 2))             # Процент выкупа (%)

    currency = db.Column(db.String(10), default='RUB')
    synced_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('seller_id', 'marketplace_id', 'nm_id', 'dt', name='uq_analytics_card_day'),
        db.Index('idx_apc_seller_mp_dt', 'seller_id', 'marketplace_id', 'dt'),
        db.Index('idx_apc_nm_dt', 'seller_id', 'nm_id', 'dt'),
    )


# ============================================================
# АНАЛИТИКА — ОСТАТКИ НА СКЛАДАХ
# ============================================================

class AnalyticsStock(db.Model):
    """Снимок остатков на складах маркетплейса"""
    __tablename__ = 'analytics_stocks'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)

    snapshot_date = db.Column(db.Date, nullable=False, index=True)

    nm_id = db.Column(db.BigInteger, index=True)
    vendor_code = db.Column(db.String(200))
    barcode = db.Column(db.String(100))
    size = db.Column(db.String(50))
    product_name = db.Column(db.String(500))
    brand = db.Column(db.String(200))
    category = db.Column(db.String(300))

    warehouse_name = db.Column(db.String(200))
    region = db.Column(db.String(200))

    quantity = db.Column(db.Integer, default=0)               # Остаток
    quantity_full = db.Column(db.Integer, default=0)          # Полный остаток (вкл. брони)
    in_way_to_client = db.Column(db.Integer, default=0)      # В пути к клиенту
    in_way_from_client = db.Column(db.Integer, default=0)    # В пути от клиента (возвраты)

    price = db.Column(db.Numeric(12, 2))
    discount = db.Column(db.Numeric(5, 2))

    days_on_site = db.Column(db.Integer)                     # Дней на площадке
    synced_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index('idx_ast_seller_mp_date', 'seller_id', 'marketplace_id', 'snapshot_date'),
        db.Index('idx_ast_nm_date', 'seller_id', 'nm_id', 'snapshot_date'),
        db.Index('idx_ast_warehouse', 'seller_id', 'marketplace_id', 'warehouse_name'),
    )


# ============================================================
# АНАЛИТИКА — ФИНАНСЫ
# ============================================================

class AnalyticsFinancialReport(db.Model):
    """Детализация финансового отчёта (еженедельный отчёт о реализации)"""
    __tablename__ = 'analytics_financial_reports'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)

    # Период отчёта
    report_period_start = db.Column(db.Date, nullable=False)
    report_period_end = db.Column(db.Date, nullable=False)
    rrd_id = db.Column(db.BigInteger)                        # Уникальный ID строки (для пагинации WB)

    # Товар
    nm_id = db.Column(db.BigInteger, index=True)
    vendor_code = db.Column(db.String(200))
    barcode = db.Column(db.String(100))
    product_name = db.Column(db.String(500))
    brand = db.Column(db.String(200))
    category = db.Column(db.String(300))
    size = db.Column(db.String(50))

    # Операция
    operation_name = db.Column(db.String(200))               # Тип операции (Продажа, Возврат, Логистика...)
    operation_date = db.Column(db.DateTime)

    # Финансы
    retail_price = db.Column(db.Numeric(12, 2))              # Розничная цена
    retail_price_with_discount = db.Column(db.Numeric(12, 2))
    retail_amount = db.Column(db.Numeric(12, 2))             # Сумма продаж
    commission_percent = db.Column(db.Numeric(5, 2))         # Комиссия (%)
    commission_amount = db.Column(db.Numeric(12, 2))         # Сумма комиссии
    delivery_cost = db.Column(db.Numeric(12, 2))             # Стоимость логистики
    storage_cost = db.Column(db.Numeric(12, 2))              # Стоимость хранения
    penalty_amount = db.Column(db.Numeric(12, 2))            # Штрафы
    additional_payment = db.Column(db.Numeric(12, 2))        # Доплаты
    to_pay = db.Column(db.Numeric(12, 2))                    # К перечислению продавцу
    quantity = db.Column(db.Integer, default=1)
    currency = db.Column(db.String(10), default='RUB')

    # Логистика
    warehouse_name = db.Column(db.String(200))
    delivery_type = db.Column(db.String(50))                 # FBO / FBS
    region = db.Column(db.String(200))

    raw_data_json = db.Column(db.Text)
    synced_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index('idx_afr_seller_period', 'seller_id', 'marketplace_id', 'report_period_start', 'report_period_end'),
        db.Index('idx_afr_nm', 'seller_id', 'nm_id'),
        db.Index('idx_afr_operation', 'seller_id', 'marketplace_id', 'operation_name'),
    )


# ============================================================
# АНАЛИТИКА — РАСХОДЫ (ЛОГИСТИКА, ХРАНЕНИЕ, ШТРАФЫ)
# ============================================================

class AnalyticsExpense(db.Model):
    """Расходы: логистика, хранение, штрафы, удержания"""
    __tablename__ = 'analytics_expenses'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)

    expense_date = db.Column(db.Date, nullable=False, index=True)
    expense_type = db.Column(db.String(50), nullable=False, index=True)
    # Типы: logistics, storage, acceptance, penalty_measurement,
    #        penalty_labeling, deduction, antifraud, commission

    nm_id = db.Column(db.BigInteger)
    vendor_code = db.Column(db.String(200))
    barcode = db.Column(db.String(100))
    product_name = db.Column(db.String(500))

    amount = db.Column(db.Numeric(12, 2), nullable=False)    # Сумма расхода
    quantity = db.Column(db.Integer, default=1)
    currency = db.Column(db.String(10), default='RUB')

    description = db.Column(db.String(500))                  # Описание/причина
    warehouse_name = db.Column(db.String(200))

    raw_data_json = db.Column(db.Text)
    synced_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index('idx_ae_seller_type_date', 'seller_id', 'marketplace_id', 'expense_type', 'expense_date'),
        db.Index('idx_ae_nm', 'seller_id', 'nm_id'),
    )


# ============================================================
# АНАЛИТИКА — ПОИСКОВЫЕ ЗАПРОСЫ (SEO)
# ============================================================

class AnalyticsSearchQuery(db.Model):
    """Поисковые запросы, по которым находят товары продавца"""
    __tablename__ = 'analytics_search_queries'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)

    nm_id = db.Column(db.BigInteger, nullable=False, index=True)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)

    search_text = db.Column(db.String(500), nullable=False)  # Текст запроса
    frequency = db.Column(db.Integer, default=0)             # Частота запроса
    median_position = db.Column(db.Numeric(6, 2))            # Медианная позиция
    average_position = db.Column(db.Integer)                  # Средняя позиция
    open_card = db.Column(db.Integer, default=0)             # Переходы
    add_to_cart = db.Column(db.Integer, default=0)           # В корзину
    orders = db.Column(db.Integer, default=0)                # Заказы
    visibility = db.Column(db.Integer)                        # Видимость (%)

    synced_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index('idx_asq_seller_nm', 'seller_id', 'marketplace_id', 'nm_id'),
        db.Index('idx_asq_period', 'seller_id', 'marketplace_id', 'period_start'),
    )


# ============================================================
# АНАЛИТИКА — РЕКЛАМА
# ============================================================

class AnalyticsAdCampaign(db.Model):
    """Рекламные кампании"""
    __tablename__ = 'analytics_ad_campaigns'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)

    external_campaign_id = db.Column(db.String(100), nullable=False)
    campaign_name = db.Column(db.String(300))
    campaign_type = db.Column(db.String(50))                 # auto / auction / search / catalog
    status = db.Column(db.String(30))                        # active / paused / stopped / ready
    payment_type = db.Column(db.String(30))                  # cpm / cpc

    # Бюджет
    daily_budget = db.Column(db.Numeric(12, 2))
    total_budget = db.Column(db.Numeric(12, 2))
    spent_total = db.Column(db.Numeric(12, 2))

    start_date = db.Column(db.DateTime)
    end_date = db.Column(db.DateTime)
    created_at_mp = db.Column(db.DateTime)                   # Дата создания на маркетплейсе

    synced_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('seller_id', 'marketplace_id', 'external_campaign_id', name='uq_ad_campaign'),
        db.Index('idx_aac_seller_mp', 'seller_id', 'marketplace_id'),
    )


class AnalyticsAdStat(db.Model):
    """Ежедневная статистика рекламных кампаний"""
    __tablename__ = 'analytics_ad_stats'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)
    campaign_id = db.Column(db.Integer, db.ForeignKey('analytics_ad_campaigns.id'), nullable=False)

    dt = db.Column(db.Date, nullable=False, index=True)
    nm_id = db.Column(db.BigInteger)                          # На уровне товара (если есть)

    # Метрики
    views = db.Column(db.Integer, default=0)                  # Показы
    clicks = db.Column(db.Integer, default=0)                 # Клики
    ctr = db.Column(db.Numeric(5, 2))                         # CTR (%)
    cpc = db.Column(db.Numeric(8, 2))                         # Стоимость клика
    cpm = db.Column(db.Numeric(8, 2))                         # Стоимость 1000 показов
    spent = db.Column(db.Numeric(12, 2), default=0)           # Потрачено
    orders = db.Column(db.Integer, default=0)                 # Заказы из рекламы
    orders_sum = db.Column(db.Numeric(12, 2), default=0)      # Сумма заказов
    atbs = db.Column(db.Integer, default=0)                   # Добавления в корзину
    cr = db.Column(db.Numeric(5, 2))                          # Конверсия (%)
    currency = db.Column(db.String(10), default='RUB')

    synced_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index('idx_aas_campaign_dt', 'campaign_id', 'dt'),
        db.Index('idx_aas_seller_mp_dt', 'seller_id', 'marketplace_id', 'dt'),
    )


# ============================================================
# АНАЛИТИКА — ОТЗЫВЫ И ВОПРОСЫ
# ============================================================

class AnalyticsFeedback(db.Model):
    """Отзывы покупателей"""
    __tablename__ = 'analytics_feedbacks'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)

    external_id = db.Column(db.String(100), nullable=False)
    nm_id = db.Column(db.BigInteger, index=True)
    product_name = db.Column(db.String(500))

    rating = db.Column(db.Integer)                            # 1-5 звёзд
    text = db.Column(db.Text)
    answer = db.Column(db.Text)                               # Ответ продавца
    is_answered = db.Column(db.Boolean, default=False)
    has_photo = db.Column(db.Boolean, default=False)

    created_at_mp = db.Column(db.DateTime)                    # Дата на маркетплейсе
    answered_at = db.Column(db.DateTime)

    synced_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('seller_id', 'marketplace_id', 'external_id', name='uq_feedback'),
        db.Index('idx_af_seller_nm', 'seller_id', 'nm_id'),
        db.Index('idx_af_rating', 'seller_id', 'marketplace_id', 'rating'),
        db.Index('idx_af_unanswered', 'seller_id', 'marketplace_id', 'is_answered'),
    )


class AnalyticsQuestion(db.Model):
    """Вопросы покупателей"""
    __tablename__ = 'analytics_questions'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)

    external_id = db.Column(db.String(100), nullable=False)
    nm_id = db.Column(db.BigInteger, index=True)
    product_name = db.Column(db.String(500))

    text = db.Column(db.Text)
    answer = db.Column(db.Text)
    is_answered = db.Column(db.Boolean, default=False)

    created_at_mp = db.Column(db.DateTime)
    answered_at = db.Column(db.DateTime)

    synced_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('seller_id', 'marketplace_id', 'external_id', name='uq_question'),
        db.Index('idx_aq_unanswered', 'seller_id', 'marketplace_id', 'is_answered'),
    )


# ============================================================
# АНАЛИТИКА — РЕГИОНАЛЬНЫЕ ПРОДАЖИ
# ============================================================

class AnalyticsRegionSale(db.Model):
    """Продажи по регионам"""
    __tablename__ = 'analytics_region_sales'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)

    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)

    region = db.Column(db.String(200), nullable=False)
    country = db.Column(db.String(100))

    orders_count = db.Column(db.Integer, default=0)
    orders_sum = db.Column(db.Numeric(12, 2), default=0)
    buyouts_count = db.Column(db.Integer, default=0)
    buyouts_sum = db.Column(db.Numeric(12, 2), default=0)
    returns_count = db.Column(db.Integer, default=0)
    returns_sum = db.Column(db.Numeric(12, 2), default=0)
    currency = db.Column(db.String(10), default='RUB')

    synced_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index('idx_ars_seller_period', 'seller_id', 'marketplace_id', 'period_start'),
        db.Index('idx_ars_region', 'seller_id', 'marketplace_id', 'region'),
    )


# ============================================================
# АНАЛИТИКА — ВОЗВРАТЫ
# ============================================================

class AnalyticsReturn(db.Model):
    """Возвраты товаров"""
    __tablename__ = 'analytics_returns'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)

    external_id = db.Column(db.String(100))
    return_date = db.Column(db.DateTime, nullable=False, index=True)

    nm_id = db.Column(db.BigInteger, index=True)
    vendor_code = db.Column(db.String(200))
    barcode = db.Column(db.String(100))
    product_name = db.Column(db.String(500))
    size = db.Column(db.String(50))

    reason = db.Column(db.String(300))                       # Причина возврата
    status = db.Column(db.String(50))                        # Статус возврата
    warehouse_name = db.Column(db.String(200))

    amount = db.Column(db.Numeric(12, 2))
    currency = db.Column(db.String(10), default='RUB')

    synced_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index('idx_ar_seller_date', 'seller_id', 'marketplace_id', 'return_date'),
        db.Index('idx_ar_nm', 'seller_id', 'nm_id'),
    )


# ============================================================
# АНАЛИТИКА — МЕТАДАННЫЕ СИНХРОНИЗАЦИИ
# ============================================================

class AnalyticsSyncState(db.Model):
    """Состояние синхронизации аналитики по продавцу и маркетплейсу"""
    __tablename__ = 'analytics_sync_states'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)

    data_type = db.Column(db.String(50), nullable=False)
    # Типы: orders, sales, stocks, product_cards, financial_reports,
    #        expenses, search_queries, ad_campaigns, ad_stats,
    #        feedbacks, questions, region_sales, returns

    last_sync_at = db.Column(db.DateTime)
    last_sync_status = db.Column(db.String(20))              # success / failed / running
    last_sync_error = db.Column(db.Text)
    last_sync_count = db.Column(db.Integer, default=0)       # Кол-во записей в последней синхронизации

    # Курсор для инкрементальной синхронизации
    sync_cursor = db.Column(db.String(200))                  # dateFrom / rrd_id / offset
    sync_cursor_type = db.Column(db.String(50))              # date / id / offset

    # Расписание
    sync_interval_minutes = db.Column(db.Integer, default=60)
    is_auto_sync = db.Column(db.Boolean, default=False)
    next_sync_at = db.Column(db.DateTime)

    __table_args__ = (
        db.UniqueConstraint('seller_id', 'marketplace_id', 'data_type', name='uq_sync_state'),
    )
```

---

## 4. Архитектура API-клиентов

### 4.1 Базовый интерфейс

```python
# services/analytics/base_client.py

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Dict, Any, Optional


@dataclass
class SyncResult:
    """Результат синхронизации"""
    success: bool
    data_type: str
    records_fetched: int = 0
    records_created: int = 0
    records_updated: int = 0
    errors: List[str] = None
    cursor: Optional[str] = None      # Для следующей инкрементальной загрузки


class BaseAnalyticsClient(ABC):
    """
    Базовый интерфейс для клиентов аналитики маркетплейсов.
    Каждый маркетплейс (WB, Ozon, YM) реализует свой класс-наследник.
    """

    def __init__(self, api_key: str, seller_id: int, marketplace_id: int):
        self.api_key = api_key
        self.seller_id = seller_id
        self.marketplace_id = marketplace_id

    # --- Заказы и продажи ---

    @abstractmethod
    def sync_orders(self, date_from: datetime, date_to: datetime = None) -> SyncResult:
        """Синхронизация заказов"""
        ...

    @abstractmethod
    def sync_sales(self, date_from: datetime, date_to: datetime = None) -> SyncResult:
        """Синхронизация продаж и возвратов"""
        ...

    # --- Воронка продаж ---

    @abstractmethod
    def sync_product_cards(self, date_from: date, date_to: date,
                           nm_ids: List[int] = None) -> SyncResult:
        """Синхронизация статистики карточек товаров (воронка)"""
        ...

    # --- Остатки ---

    @abstractmethod
    def sync_stocks(self) -> SyncResult:
        """Синхронизация остатков на складах"""
        ...

    # --- Финансы ---

    @abstractmethod
    def sync_financial_report(self, date_from: date, date_to: date) -> SyncResult:
        """Синхронизация финансового отчёта"""
        ...

    # --- Расходы ---

    @abstractmethod
    def sync_expenses(self, date_from: date, date_to: date) -> SyncResult:
        """Синхронизация расходов (логистика, хранение, штрафы)"""
        ...

    # --- SEO/Поисковые запросы ---

    @abstractmethod
    def sync_search_queries(self, nm_ids: List[int],
                            date_from: date, date_to: date) -> SyncResult:
        """Синхронизация поисковых запросов"""
        ...

    # --- Реклама ---

    @abstractmethod
    def sync_ad_campaigns(self) -> SyncResult:
        """Синхронизация списка рекламных кампаний"""
        ...

    @abstractmethod
    def sync_ad_stats(self, date_from: date, date_to: date,
                      campaign_ids: List[str] = None) -> SyncResult:
        """Синхронизация статистики рекламы"""
        ...

    # --- Отзывы и вопросы ---

    @abstractmethod
    def sync_feedbacks(self, date_from: datetime = None) -> SyncResult:
        """Синхронизация отзывов"""
        ...

    @abstractmethod
    def sync_questions(self, date_from: datetime = None) -> SyncResult:
        """Синхронизация вопросов"""
        ...

    # --- Региональные продажи ---

    @abstractmethod
    def sync_region_sales(self, date_from: date, date_to: date) -> SyncResult:
        """Синхронизация продаж по регионам"""
        ...

    # --- Возвраты ---

    @abstractmethod
    def sync_returns(self, date_from: date, date_to: date) -> SyncResult:
        """Синхронизация возвратов"""
        ...
```

### 4.2 Реализация для WB

```python
# services/analytics/wb_analytics_client.py

class WBAnalyticsClient(BaseAnalyticsClient):
    """Клиент аналитики Wildberries"""

    ANALYTICS_BASE = "https://seller-analytics-api.wildberries.ru"
    STATISTICS_BASE = "https://statistics-api.wildberries.ru"
    ADVERT_BASE = "https://advert-api.wildberries.ru"
    FEEDBACKS_BASE = "https://feedbacks-api.wildberries.ru"

    def sync_orders(self, date_from, date_to=None):
        # GET /api/v1/supplier/orders?dateFrom=...
        # Маппинг полей WB → AnalyticsOrder
        ...

    def sync_sales(self, date_from, date_to=None):
        # GET /api/v1/supplier/sales?dateFrom=...
        # Маппинг полей WB → AnalyticsSale
        ...

    def sync_product_cards(self, date_from, date_to, nm_ids=None):
        # POST /api/analytics/v3/sales-funnel/products/history
        # Маппинг: openCardCount → open_card_count, и т.д.
        ...

    def sync_stocks(self):
        # GET /api/v1/supplier/stocks?dateFrom=...
        # Маппинг полей WB → AnalyticsStock
        ...

    def sync_financial_report(self, date_from, date_to):
        # GET /api/v5/supplier/reportDetailByPeriod
        # Пагинация по rrd_id
        ...

    def sync_expenses(self, date_from, date_to):
        # Агрегирует данные из нескольких эндпоинтов:
        # - /api/v1/paid_storage (хранение)
        # - /api/v1/acceptance_report (приёмка)
        # - /api/analytics/v1/measurement-penalties (штрафы за габариты)
        # - /api/analytics/v1/deductions (удержания)
        # - /api/v1/analytics/goods-labeling (штрафы за маркировку)
        # - /api/v1/analytics/antifraud-details (самовыкупы)
        ...

    def sync_search_queries(self, nm_ids, date_from, date_to):
        # POST /api/v2/search-report/product/search-texts
        ...

    def sync_ad_campaigns(self):
        # GET /adv/v1/promotion/count + GET /api/advert/v2/adverts
        ...

    def sync_ad_stats(self, date_from, date_to, campaign_ids=None):
        # POST /adv/v0/normquery/stats (для кластеров)
        ...

    def sync_feedbacks(self, date_from=None):
        # GET /api/v1/feedbacks
        ...

    def sync_questions(self, date_from=None):
        # GET /api/v1/questions
        ...

    def sync_region_sales(self, date_from, date_to):
        # GET /api/v1/analytics/region-sale
        ...

    def sync_returns(self, date_from, date_to):
        # GET /api/v1/analytics/goods-return
        ...
```

### 4.3 Будущие клиенты (заготовки)

```python
# services/analytics/ozon_analytics_client.py
class OzonAnalyticsClient(BaseAnalyticsClient):
    """Клиент аналитики Ozon — будет реализован при подключении Ozon"""
    ...

# services/analytics/yandex_analytics_client.py
class YandexMarketAnalyticsClient(BaseAnalyticsClient):
    """Клиент аналитики Яндекс Маркета"""
    ...
```

### 4.4 Фабрика клиентов

```python
# services/analytics/client_factory.py

def get_analytics_client(marketplace_code: str, api_key: str,
                         seller_id: int, marketplace_id: int) -> BaseAnalyticsClient:
    """Создать клиент аналитики для маркетплейса"""
    clients = {
        'wb': WBAnalyticsClient,
        'ozon': OzonAnalyticsClient,
        'yandex_market': YandexMarketAnalyticsClient,
    }
    client_class = clients.get(marketplace_code)
    if not client_class:
        raise ValueError(f"Unsupported marketplace: {marketplace_code}")
    return client_class(api_key=api_key, seller_id=seller_id, marketplace_id=marketplace_id)
```

---

## 5. Сервис аналитики (агрегация)

```python
# services/analytics/analytics_service.py

class AnalyticsService:
    """
    Сервис для агрегации и вычисления аналитических метрик.
    Работает с данными из analytics_* таблиц, агрегирует по маркетплейсам.
    """

    @staticmethod
    def get_dashboard_summary(seller_id: int,
                              marketplace_id: int = None,
                              date_from: date = None,
                              date_to: date = None) -> dict:
        """
        Сводка для главного дашборда.

        Returns:
            {
                "orders": {
                    "total": 1523, "total_sum": 4567890.00,
                    "prev_total": 1200, "growth_percent": 26.9
                },
                "sales": {
                    "total": 1100, "total_sum": 3200000.00,
                    "returns": 85, "buyout_percent": 88.5
                },
                "revenue": {
                    "gross": 3200000.00, "net": 2100000.00,
                    "commission": 640000.00, "logistics": 280000.00,
                    "storage": 45000.00, "penalties": 12000.00
                },
                "products": {
                    "active_count": 450, "top_product_nm_id": 12345678,
                    "avg_conversion": 3.2
                },
                "feedbacks": {
                    "avg_rating": 4.3, "unanswered": 12,
                    "total": 856
                },
                "advertising": {
                    "total_spent": 125000.00, "total_orders": 340,
                    "avg_cpc": 15.50, "roas": 8.2
                }
            }
        """
        ...

    @staticmethod
    def get_sales_funnel(seller_id: int,
                         date_from: date, date_to: date,
                         marketplace_id: int = None,
                         nm_ids: List[int] = None,
                         group_by: str = 'day') -> dict:
        """
        Данные воронки продаж.

        Returns:
            {
                "chart": [
                    {"date": "2026-03-01", "views": 5000, "cart": 450,
                     "orders": 120, "buyouts": 95},
                    ...
                ],
                "totals": {"views": 35000, "cart": 3150, "orders": 840, "buyouts": 665},
                "conversion": {
                    "view_to_cart": 9.0, "cart_to_order": 26.7,
                    "order_to_buyout": 79.2
                },
                "products": [
                    {"nm_id": 123, "name": "...", "views": 500, "orders": 20, ...},
                    ...
                ]
            }
        """
        ...

    @staticmethod
    def get_financial_report(seller_id: int,
                             date_from: date, date_to: date,
                             marketplace_id: int = None) -> dict:
        """
        Финансовый отчёт (P&L).

        Returns:
            {
                "revenue": 3200000.00,
                "cost_of_goods": 1600000.00,    # Если есть supplier_price
                "gross_profit": 1600000.00,
                "expenses": {
                    "commission": 640000.00,
                    "logistics": 280000.00,
                    "storage": 45000.00,
                    "acceptance": 8000.00,
                    "penalties": 12000.00,
                    "advertising": 125000.00,
                    "total": 1110000.00
                },
                "net_profit": 490000.00,
                "margin_percent": 15.3,
                "by_marketplace": [
                    {"marketplace": "Wildberries", "revenue": 2800000, ...},
                    {"marketplace": "Ozon", "revenue": 400000, ...}
                ],
                "chart": [
                    {"date": "2026-03-01", "revenue": 120000, "expenses": 48000, "profit": 72000},
                    ...
                ]
            }
        """
        ...

    @staticmethod
    def get_product_analytics(seller_id: int, nm_id: int,
                              date_from: date, date_to: date,
                              marketplace_id: int = None) -> dict:
        """
        Детальная аналитика по одному товару.

        Returns:
            {
                "product": {"nm_id": ..., "name": ..., "brand": ...},
                "funnel": {...},
                "sales_chart": [...],
                "stock_chart": [...],
                "search_queries": [...],       # Топ поисковых запросов
                "feedbacks": {"avg_rating": ..., "count": ..., "distribution": [...]},
                "financial": {"revenue": ..., "costs": ..., "profit": ...},
                "positions": {"avg_position": ..., "visibility": ...}
            }
        """
        ...

    @staticmethod
    def get_stock_analytics(seller_id: int,
                            marketplace_id: int = None) -> dict:
        """
        Аналитика остатков.

        Returns:
            {
                "total_sku": 1250,
                "in_stock": 980,
                "out_of_stock": 270,
                "total_quantity": 45000,
                "total_value": 12500000.00,
                "by_warehouse": [
                    {"warehouse": "Коледино", "quantity": 15000, "sku": 450},
                    ...
                ],
                "low_stock": [...],            # Товары с критически малым остатком
                "overstocked": [...],          # Затоваренные позиции
                "turnover_days": 32.5          # Средняя оборачиваемость
            }
        """
        ...

    @staticmethod
    def get_advertising_analytics(seller_id: int,
                                   date_from: date, date_to: date,
                                   marketplace_id: int = None) -> dict:
        """
        Аналитика рекламы.

        Returns:
            {
                "summary": {
                    "total_spent": 125000, "total_views": 2500000,
                    "total_clicks": 35000, "total_orders": 340,
                    "avg_ctr": 1.4, "avg_cpc": 3.57,
                    "roas": 8.2, "drr": 12.2     # ДРР = расходы / доход * 100
                },
                "campaigns": [
                    {"id": ..., "name": ..., "type": ..., "spent": ...,
                     "views": ..., "clicks": ..., "orders": ..., "roas": ...},
                    ...
                ],
                "chart": [
                    {"date": "2026-03-01", "spent": 5000, "orders": 15, "roas": 9.6},
                    ...
                ]
            }
        """
        ...

    @staticmethod
    def get_feedback_analytics(seller_id: int,
                                marketplace_id: int = None) -> dict:
        """
        Аналитика отзывов и вопросов.

        Returns:
            {
                "feedbacks": {
                    "total": 856, "avg_rating": 4.3,
                    "distribution": {1: 12, 2: 25, 3: 45, 4: 180, 5: 594},
                    "unanswered": 12, "answer_rate": 98.6,
                    "with_photo": 234,
                    "rating_trend": [{"date": "2026-02", "avg": 4.2}, ...]
                },
                "questions": {
                    "total": 156, "unanswered": 5, "answer_rate": 96.8
                },
                "worst_rated_products": [
                    {"nm_id": ..., "name": ..., "avg_rating": 3.1, "count": 15},
                    ...
                ]
            }
        """
        ...

    @staticmethod
    def get_region_analytics(seller_id: int,
                              date_from: date, date_to: date,
                              marketplace_id: int = None) -> dict:
        """
        Аналитика по регионам.

        Returns:
            {
                "regions": [
                    {"region": "Москва", "orders": 450, "sum": 1200000,
                     "buyouts": 380, "returns": 30, "share": 29.5},
                    ...
                ],
                "map_data": {...}              # Данные для тепловой карты
            }
        """
        ...
```

---

## 6. Структура дашборда (UI)

### 6.1 Главный дашборд (`/analytics`)

```
┌─────────────────────────────────────────────────────────────┐
│ Аналитика                    [WB ▼] [Ozon ▼] [Все ▼]      │
│                              [Период: 01.03 - 07.03 ▼]     │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
│  │ Заказы   │  │ Выручка  │  │ Прибыль  │  │ Выкуп    │   │
│  │ 1 523    │  │ 3.2M ₽   │  │ 490K ₽   │  │ 88.5%    │   │
│  │ ↑ +26.9% │  │ ↑ +15.2% │  │ ↑ +8.1%  │  │ ↓ -1.2%  │   │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘   │
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
│  │ Реклама  │  │ Рейтинг  │  │ Возвраты │  │ Остатки  │   │
│  │ 125K ₽   │  │ ★ 4.3    │  │ 85 шт    │  │ 45 000   │   │
│  │ ROAS 8.2 │  │ 12 новых │  │ ↓ -5.3%  │  │ 270 = 0  │   │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘   │
│                                                             │
│  ╔═══════════════════════════════════════════════════════╗  │
│  ║  Графики продаж (линейный, по дням)                  ║  │
│  ║  [Заказы] [Выручка] [Прибыль]                        ║  │
│  ║  ═══╗    ╔═══                                        ║  │
│  ║     ╚════╝      Визуализация тренда                  ║  │
│  ╚═══════════════════════════════════════════════════════╝  │
│                                                             │
│  ╔═════════════════════╗  ╔═════════════════════════════╗  │
│  ║ Топ-10 товаров      ║  ║ Структура расходов (пончик)║  │
│  ║ по выручке          ║  ║ Комиссия: 57%              ║  │
│  ║                     ║  ║ Логистика: 25%             ║  │
│  ║ 1. Товар A  150K    ║  ║ Хранение: 4%              ║  │
│  ║ 2. Товар B  120K    ║  ║ Реклама: 11%              ║  │
│  ║ ...                 ║  ║ Штрафы: 3%                ║  │
│  ╚═════════════════════╝  ╚═════════════════════════════╝  │
└─────────────────────────────────────────────────────────────┘
```

### 6.2 Воронка продаж (`/analytics/funnel`)

```
┌─────────────────────────────────────────────────────────────┐
│ Воронка продаж               [Период ▼] [Категория ▼]      │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Показы → Карточка → Корзина → Заказ → Выкуп              │
│  35 000    3 150      840       665                         │
│       9.0%      26.7%     79.2%                             │
│                                                             │
│  ╔═══════════════════════════════════════════════════════╗  │
│  ║  График воронки по дням (stacked area)               ║  │
│  ╚═══════════════════════════════════════════════════════╝  │
│                                                             │
│  Таблица по товарам:                                       │
│  ┌─────────┬────────┬────────┬────────┬────────┬────────┐ │
│  │ Товар   │ Показы │ В корз.│ Заказы │ Выкупы │ CR %   │ │
│  ├─────────┼────────┼────────┼────────┼────────┼────────┤ │
│  │ Тов. A  │  5 200 │   620  │  180   │  145   │  3.5%  │ │
│  │ Тов. B  │  3 800 │   410  │  110   │   88   │  2.9%  │ │
│  └─────────┴────────┴────────┴────────┴────────┴────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### 6.3 Финансовый отчёт (`/analytics/finance`)

```
┌─────────────────────────────────────────────────────────────┐
│ P&L (Отчёт о прибылях и убытках)        [Период ▼]        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Выручка (GMV)                       3 200 000 ₽  100%    │
│  ─ Себестоимость                    -1 600 000 ₽  -50%    │
│  ═ Валовая прибыль                   1 600 000 ₽   50%    │
│                                                             │
│  Расходы:                                                  │
│    Комиссия маркетплейса              -640 000 ₽  -20%    │
│    Логистика (доставка)               -280 000 ₽   -9%    │
│    Хранение                            -45 000 ₽   -1%    │
│    Приёмка                              -8 000 ₽    0%    │
│    Штрафы и удержания                  -12 000 ₽    0%    │
│    Реклама                            -125 000 ₽   -4%    │
│  ─ Итого расходов                   -1 110 000 ₽  -35%    │
│                                                             │
│  ═ Чистая прибыль                      490 000 ₽   15%    │
│                                                             │
│  ╔═══════════════════════════════════════════════════════╗  │
│  ║  График: Выручка / Расходы / Прибыль (по неделям)    ║  │
│  ╚═══════════════════════════════════════════════════════╝  │
│                                                             │
│  По маркетплейсам:                                         │
│  ┌──────────────┬────────────┬───────────┬─────────────┐   │
│  │ Маркетплейс  │ Выручка    │ Расходы   │ Прибыль     │   │
│  ├──────────────┼────────────┼───────────┼─────────────┤   │
│  │ Wildberries  │ 2 800 000  │  970 000  │  430 000    │   │
│  │ Ozon         │   400 000  │  140 000  │   60 000    │   │
│  └──────────────┴────────────┴───────────┴─────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### 6.4 Другие страницы

| Страница | URL | Описание |
|---|---|---|
| Остатки | `/analytics/stocks` | Таблица остатков по складам, товарам, размерам. Алерты на дефицит/затоваривание |
| Реклама | `/analytics/advertising` | Список кампаний, статистика, ROI, ДРР, графики расходов |
| Отзывы | `/analytics/feedbacks` | Рейтинги, тренды, неотвеченные, худшие товары |
| Поисковые запросы | `/analytics/search` | Позиции, видимость, топ запросов по товарам |
| География | `/analytics/regions` | Тепловая карта продаж по регионам |
| Возвраты | `/analytics/returns` | Статистика возвратов, причины, проблемные товары |
| Товар (детально) | `/analytics/product/<nm_id>` | Полная аналитика одного товара (всё в одном) |
| Сравнение периодов | `/analytics/compare` | Сравнение двух периодов по ключевым метрикам |

---

## 7. Маршруты (routes)

```python
# routes/analytics.py

analytics_bp = Blueprint('analytics', __name__, url_prefix='/analytics')

# === Дашборды ===
@analytics_bp.route('/')                      # Главный дашборд
@analytics_bp.route('/funnel')                # Воронка продаж
@analytics_bp.route('/finance')               # P&L
@analytics_bp.route('/stocks')                # Остатки
@analytics_bp.route('/advertising')           # Реклама
@analytics_bp.route('/feedbacks')             # Отзывы
@analytics_bp.route('/search')                # Поисковые запросы
@analytics_bp.route('/regions')               # География
@analytics_bp.route('/returns')               # Возвраты
@analytics_bp.route('/product/<int:nm_id>')   # Детали товара
@analytics_bp.route('/compare')               # Сравнение периодов

# === API (JSON для AJAX-графиков) ===
@analytics_bp.route('/api/dashboard')         # Данные главного дашборда
@analytics_bp.route('/api/funnel')            # Данные воронки
@analytics_bp.route('/api/finance')           # Данные P&L
@analytics_bp.route('/api/stocks')            # Данные остатков
@analytics_bp.route('/api/advertising')       # Данные рекламы
@analytics_bp.route('/api/feedbacks')         # Данные отзывов
@analytics_bp.route('/api/search')            # Данные поисковых запросов
@analytics_bp.route('/api/regions')           # Данные по регионам
@analytics_bp.route('/api/returns')           # Данные возвратов
@analytics_bp.route('/api/product/<int:nm_id>')  # Данные товара

# === Синхронизация ===
@analytics_bp.route('/sync/start', methods=['POST'])   # Запуск синхронизации
@analytics_bp.route('/sync/status')                    # Статус синхронизации
@analytics_bp.route('/sync/settings', methods=['GET', 'POST'])  # Настройки автосинхронизации

# === Экспорт ===
@analytics_bp.route('/export/excel')          # Экспорт в Excel
@analytics_bp.route('/export/csv')            # Экспорт в CSV
```

---

## 8. Расписание синхронизации

```python
# services/analytics/sync_scheduler.py

SYNC_SCHEDULE = {
    'orders':            {'interval_minutes': 30,  'description': 'Заказы'},
    'sales':             {'interval_minutes': 30,  'description': 'Продажи'},
    'stocks':            {'interval_minutes': 60,  'description': 'Остатки'},
    'product_cards':     {'interval_minutes': 120, 'description': 'Воронка продаж'},
    'financial_reports': {'interval_minutes': 1440,'description': 'Финансы (1 раз/день)'},
    'expenses':          {'interval_minutes': 1440,'description': 'Расходы (1 раз/день)'},
    'search_queries':    {'interval_minutes': 1440,'description': 'Поисковые запросы (1 раз/день)'},
    'ad_campaigns':      {'interval_minutes': 60,  'description': 'Рекламные кампании'},
    'ad_stats':          {'interval_minutes': 120, 'description': 'Статистика рекламы'},
    'feedbacks':         {'interval_minutes': 60,  'description': 'Отзывы'},
    'questions':         {'interval_minutes': 60,  'description': 'Вопросы'},
    'region_sales':      {'interval_minutes': 1440,'description': 'Продажи по регионам'},
    'returns':           {'interval_minutes': 1440,'description': 'Возвраты'},
}
```

---

## 9. Rate limits WB API (учёт при синхронизации)

| API | Лимит | Стратегия |
|---|---|---|
| Analytics (sales-funnel) | 3 req/min | Очередь с паузой 20 сек |
| Statistics (orders/sales/stocks) | 1 req/min | Очередь с паузой 60 сек |
| Financial reports | 1 req/min | Пагинация по rrd_id |
| Paid storage | 1 req/min | Асинхронная задача |
| Region sales | 1 req/10 сек | Пауза 10 сек |
| Feedbacks | 1 req/сек (блок при >3) | Token bucket |
| Promotion | Зависит от метода | Адаптивный |

---

## 10. Структура файлов

```
services/
  analytics/
    __init__.py
    base_client.py              # BaseAnalyticsClient (абстрактный)
    wb_analytics_client.py      # Реализация для WB
    ozon_analytics_client.py    # Заготовка для Ozon
    yandex_analytics_client.py  # Заготовка для Яндекс Маркет
    client_factory.py           # Фабрика клиентов
    analytics_service.py        # Агрегация и вычисления
    sync_scheduler.py           # Расписание синхронизации

routes/
  analytics.py                  # Маршруты аналитики

templates/
  analytics/
    dashboard.html              # Главный дашборд
    funnel.html                 # Воронка продаж
    finance.html                # P&L
    stocks.html                 # Остатки
    advertising.html            # Реклама
    feedbacks.html              # Отзывы
    search.html                 # Поисковые запросы
    regions.html                # География
    returns.html                # Возвраты
    product_detail.html         # Аналитика товара
    compare.html                # Сравнение периодов
    _partials/
      kpi_card.html             # Компонент KPI-карточки
      chart_container.html      # Обёртка для графика
      period_selector.html      # Выбор периода
      marketplace_filter.html   # Фильтр по маркетплейсу

migrations/
  migrate_add_analytics_tables.py  # Миграция для новых таблиц
```

---

## 11. Ключевые принципы

1. **marketplace_id всегда присутствует** — нет данных "без маркетплейса"
2. **Инкрементальная синхронизация** — sync_cursor хранит последнюю точку загрузки
3. **Идемпотентность** — повторная загрузка не дублирует данные (UPSERT по unique constraint)
4. **raw_data_json** — всегда сохраняем сырые данные API для дебага и реконструкции
5. **Единая валюта** — все суммы в RUB (конвертация при загрузке, если нужно)
6. **Один сервис — одна ответственность** — клиент загружает, сервис агрегирует, роут отдаёт
7. **Graceful degradation** — если один тип данных не загрузился, остальные работают

---

## 12. Порядок реализации

### Фаза 1: Фундамент
1. Добавить модели в `models.py` + миграция
2. Создать `BaseAnalyticsClient` и `WBAnalyticsClient`
3. Реализовать `sync_orders` и `sync_sales` (самые простые эндпоинты)
4. Создать `AnalyticsSyncState` и базовую логику инкрементальной синхронизации

### Фаза 2: Основная аналитика
5. Реализовать `sync_product_cards` (воронка продаж)
6. Реализовать `sync_stocks` (остатки)
7. Реализовать `sync_financial_report` (финансы)
8. Создать `AnalyticsService.get_dashboard_summary()`
9. Создать маршрут `/analytics` и шаблон главного дашборда

### Фаза 3: Расширенная аналитика
10. Реализовать `sync_expenses` (все виды расходов)
11. Реализовать `sync_feedbacks` и `sync_questions`
12. Реализовать `sync_search_queries`
13. Реализовать `sync_region_sales` и `sync_returns`
14. Создать страницы P&L, воронки, остатков

### Фаза 4: Реклама
15. Реализовать `sync_ad_campaigns` и `sync_ad_stats`
16. Создать страницу рекламной аналитики

### Фаза 5: Автоматизация
17. Настроить автосинхронизацию (scheduler)
18. Добавить экспорт в Excel/CSV
19. Алерты (низкие остатки, плохие отзывы, падение продаж)

### Фаза 6: Мультимаркетплейс
20. Реализовать `OzonAnalyticsClient`
21. Реализовать `YandexMarketAnalyticsClient`
22. Сводные отчёты по всем маркетплейсам
