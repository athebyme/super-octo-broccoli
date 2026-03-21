"""
Модели базы данных для платформы продавцов WB
"""
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from typing import Optional
import os
import json
from cryptography.fernet import Fernet

db = SQLAlchemy()


class User(UserMixin, db.Model):
    """Модель пользователя системы (админы и продавцы)"""
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    last_login = db.Column(db.DateTime)

    # Дополнительные поля для админки
    blocked_at = db.Column(db.DateTime)  # Когда заблокирован
    blocked_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'))  # Кто заблокировал
    notes = db.Column(db.Text)  # Заметки администратора о пользователе

    # Связь с продавцом (если это продавец)
    seller = db.relationship('Seller', backref='user', uselist=False, cascade='all, delete-orphan')

    # Связь для блокировки (кто заблокировал этого пользователя)
    blocked_by = db.relationship('User', remote_side=[id], backref='blocked_users', foreign_keys=[blocked_by_user_id])

    def set_password(self, password: str) -> None:
        """Установить хеш пароля"""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        """Проверить пароль"""
        return check_password_hash(self.password_hash, password)

    def is_blocked(self) -> bool:
        """Проверить заблокирован ли пользователь"""
        return self.blocked_at is not None

    def to_dict(self, include_sensitive: bool = False) -> dict:
        """Конвертировать в словарь для JSON"""
        data = {
            'id': self.id,
            'username': self.username,
            'email': self.email if include_sensitive else f"{self.email[:3]}***@{self.email.split('@')[1] if '@' in self.email else '***'}",
            'is_admin': self.is_admin,
            'is_active': self.is_active,
            'is_blocked': self.is_blocked(),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_login': self.last_login.isoformat() if self.last_login else None,
            'blocked_at': self.blocked_at.isoformat() if self.blocked_at else None,
            'blocked_by_username': self.blocked_by.username if self.blocked_by else None,
            'notes': self.notes if include_sensitive else None,
            'has_seller': self.seller is not None,
            'seller_company': self.seller.company_name if self.seller else None
        }
        return data

    def __repr__(self) -> str:
        return f'<User {self.username}>'


class Seller(db.Model):
    """Модель продавца"""
    __tablename__ = 'sellers'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True, index=True)
    company_name = db.Column(db.String(200), nullable=False)
    _wb_api_key_encrypted = db.Column('wb_api_key', db.String(500))  # Зашифрованный API ключ
    wb_seller_id = db.Column(db.String(100))  # ID продавца в WB
    contact_phone = db.Column(db.String(20))
    notes = db.Column(db.Text)  # Заметки админа
    api_last_sync = db.Column(db.DateTime)  # Последняя синхронизация с API
    api_sync_status = db.Column(db.String(50))  # Статус синхронизации
    stock_refresh_interval = db.Column(db.Integer, default=30)  # Интервал обновления остатков (мин), от 5 до 60
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    reports = db.relationship(
        'SellerReport',
        backref='seller',
        cascade='all, delete-orphan',
        lazy='dynamic',
    )

    # Связь с карточками товаров
    products = db.relationship('Product', backref='seller', lazy='dynamic', cascade='all, delete-orphan')
    # Связь с логами API
    api_logs = db.relationship('APILog', backref='seller', lazy='dynamic', cascade='all, delete-orphan')

    @property
    def wb_api_key(self) -> Optional[str]:
        """Расшифровать API ключ"""
        if not self._wb_api_key_encrypted:
            return None

        # Получаем ключ шифрования из переменных окружения
        encryption_key = os.environ.get('ENCRYPTION_KEY')
        if not encryption_key:
            # Если ключ не настроен, возвращаем как есть (для обратной совместимости)
            return self._wb_api_key_encrypted

        try:
            f = Fernet(encryption_key.encode())
            return f.decrypt(self._wb_api_key_encrypted.encode()).decode()
        except Exception:
            # Если не удалось расшифровать, возможно ключ хранится незашифрованным
            return self._wb_api_key_encrypted

    @wb_api_key.setter
    def wb_api_key(self, value: Optional[str]) -> None:
        """Зашифровать API ключ"""
        if value is None:
            self._wb_api_key_encrypted = None
            return

        # Получаем ключ шифрования
        encryption_key = os.environ.get('ENCRYPTION_KEY')
        if not encryption_key:
            # Если ключ не настроен, сохраняем как есть
            self._wb_api_key_encrypted = value
            return

        try:
            f = Fernet(encryption_key.encode())
            self._wb_api_key_encrypted = f.encrypt(value.encode()).decode()
        except Exception:
            # В случае ошибки сохраняем незашифрованным
            self._wb_api_key_encrypted = value

    def has_valid_api_key(self) -> bool:
        """Проверить наличие валидного API ключа"""
        return self.wb_api_key is not None and len(self.wb_api_key) > 0

    def __repr__(self) -> str:
        return f'<Seller {self.company_name}>'


class SellerReport(db.Model):
    """История расчетов прибыли продавца"""
    __tablename__ = 'seller_reports'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    statistics_path = db.Column(db.String(500), nullable=False)
    price_path = db.Column(db.String(500), nullable=False)
    processed_path = db.Column(db.String(500), nullable=False)
    selected_columns = db.Column(db.JSON, nullable=False, default=list)
    summary = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self) -> str:
        return f'<SellerReport {self.id} seller={self.seller_id}>'


class Product(db.Model):
    """Модель карточки товара WB"""
    __tablename__ = 'products'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)

    # Основные данные из WB API
    nm_id = db.Column(db.BigInteger, nullable=False, index=True)  # Артикул WB (nmID)
    imt_id = db.Column(db.BigInteger)  # ID товара
    vendor_code = db.Column(db.String(100), index=True)  # Артикул поставщика
    title = db.Column(db.String(500))  # Название товара

    # Характеристики
    brand = db.Column(db.String(200))  # Бренд
    object_name = db.Column(db.String(200))  # Тип товара (футболка, платье и т.д.)
    subject_id = db.Column(db.Integer)  # ID предмета (для получения характеристик из API)
    supplier_vendor_code = db.Column(db.String(100))  # Внутренний артикул поставщика

    # Цены и остатки (из последней синхронизации)
    price = db.Column(db.Numeric(10, 2))  # Цена
    discount_price = db.Column(db.Numeric(10, 2))  # Цена со скидкой
    quantity = db.Column(db.Integer, default=0)  # Остаток

    # Цена поставщика (из CSV поставщика)
    supplier_price = db.Column(db.Float, nullable=True)
    supplier_price_updated_at = db.Column(db.DateTime, nullable=True)

    # Медиа
    photos_json = db.Column(db.Text)  # JSON с URL фотографий
    video_url = db.Column(db.String(500))  # URL видео

    # Размеры и баркоды
    sizes_json = db.Column(db.Text)  # JSON с размерами и баркодами

    # Характеристики и описание
    characteristics_json = db.Column(db.Text)  # JSON с характеристиками товара
    description = db.Column(db.Text)  # Описание товара
    dimensions_json = db.Column(db.Text)  # JSON с габаритами (длина, ширина, высота)
    tags_json = db.Column(db.Text)  # JSON список ключевых слов/тегов (хранится локально, не в WB)

    # Рейтинг карточки WB (из Analytics API)
    nm_rating = db.Column(db.Float, nullable=True)  # Рейтинг карточки (0-10)

    # Метаданные
    is_active = db.Column(db.Boolean, default=True)  # Активна ли карточка
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_sync = db.Column(db.DateTime)  # Последняя синхронизация данных

    # Индексы для быстрых запросов
    __table_args__ = (
        db.UniqueConstraint('seller_id', 'nm_id', name='uq_seller_nm_id'),
        db.Index('idx_seller_vendor_code', 'seller_id', 'vendor_code'),
        db.Index('idx_seller_active', 'seller_id', 'is_active'),
    )

    def __repr__(self) -> str:
        return f'<Product {self.vendor_code} ({self.nm_id})>'

    def to_dict(self):
        """Конвертировать в словарь для JSON"""
        return {
            'id': self.id,
            'nm_id': self.nm_id,
            'vendor_code': self.vendor_code,
            'title': self.title,
            'brand': self.brand,
            'object_name': self.object_name,
            'subject_id': self.subject_id,
            'price': float(self.price) if self.price else None,
            'discount_price': float(self.discount_price) if self.discount_price else None,
            'quantity': self.quantity,
            'nm_rating': self.nm_rating,
            'is_active': self.is_active,
            'last_sync': self.last_sync.isoformat() if self.last_sync else None
        }

    def get_characteristics(self):
        """Получить характеристики товара как список словарей"""
        if not self.characteristics_json:
            return []
        try:
            import json
            return json.loads(self.characteristics_json)
        except:
            return []

    def set_characteristics(self, characteristics):
        """Установить характеристики товара из списка словарей"""
        try:
            import json
            self.characteristics_json = json.dumps(characteristics, ensure_ascii=False)
        except:
            self.characteristics_json = '[]'

    def to_wb_card_format(self):
        """
        Конвертировать данные из БД в формат карточки WB API

        Returns:
            Словарь в формате WB API для Content API v2
        """
        import json

        # Базовая структура карточки
        card = {
            'nmID': self.nm_id,
            'imtID': self.imt_id,
            'vendorCode': self.vendor_code or '',
            'subjectID': self.subject_id,
            'title': self.title or '',
            'description': self.description or '',
            'brand': self.brand or '',
        }

        # Размеры (обязательно!)
        try:
            card['sizes'] = json.loads(self.sizes_json) if self.sizes_json else []
        except:
            card['sizes'] = []

        # Характеристики
        try:
            card['characteristics'] = json.loads(self.characteristics_json) if self.characteristics_json else []
        except:
            card['characteristics'] = []

        # Фотографии
        try:
            card['photos'] = json.loads(self.photos_json) if self.photos_json else []
        except:
            card['photos'] = []

        # Габариты
        try:
            card['dimensions'] = json.loads(self.dimensions_json) if self.dimensions_json else {}
        except:
            card['dimensions'] = {}

        # Видео
        if self.video_url:
            card['video'] = self.video_url

        return card


class APILog(db.Model):
    """Логи взаимодействия с API WB"""
    __tablename__ = 'api_logs'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)

    # Детали запроса
    endpoint = db.Column(db.String(200), nullable=False)  # Эндпоинт API
    method = db.Column(db.String(10), nullable=False)  # HTTP метод
    status_code = db.Column(db.Integer)  # Код ответа
    response_time = db.Column(db.Float)  # Время ответа в секундах

    # Тела запроса и ответа
    request_body = db.Column(db.Text)  # JSON body запроса
    response_body = db.Column(db.Text)  # JSON body ответа

    # Результат
    success = db.Column(db.Boolean, default=True)  # Успешен ли запрос
    error_message = db.Column(db.Text)  # Сообщение об ошибке

    # Метаданные
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Индекс для быстрого поиска последних логов
    __table_args__ = (
        db.Index('idx_seller_created', 'seller_id', 'created_at'),
    )

    def __repr__(self) -> str:
        return f'<APILog {self.method} {self.endpoint} [{self.status_code}]>'

    @staticmethod
    def log_request(seller_id: int, endpoint: str, method: str, status_code: int,
                    response_time: float, success: bool = True, error_message: str = None,
                    request_body: str = None, response_body: str = None):
        """Создать запись лога"""
        log = APILog(
            seller_id=seller_id,
            endpoint=endpoint,
            method=method,
            status_code=status_code,
            response_time=response_time,
            success=success,
            error_message=error_message,
            request_body=request_body,
            response_body=response_body
        )
        db.session.add(log)
        db.session.commit()
        return log


class BulkEditHistory(db.Model):
    """История массовых изменений карточек"""
    __tablename__ = 'bulk_edit_history'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)

    # Параметры операции
    operation_type = db.Column(db.String(50), nullable=False)  # 'update_brand', 'update_characteristic', 'add_characteristic', etc.
    operation_params = db.Column(db.JSON)  # Параметры операции (например, {field: 'brand', value: 'Nike'})

    # Описание операции
    description = db.Column(db.Text)  # Человекочитаемое описание

    # Статистика
    total_products = db.Column(db.Integer, default=0)  # Всего товаров в операции
    success_count = db.Column(db.Integer, default=0)  # Успешно обработано
    error_count = db.Column(db.Integer, default=0)  # Ошибок
    errors_details = db.Column(db.JSON)  # Детали ошибок

    # Статус выполнения
    status = db.Column(db.String(50), default='pending')  # 'pending', 'in_progress', 'completed', 'failed'
    wb_synced = db.Column(db.Boolean, default=False)  # Синхронизировано ли с WB

    # Откат
    reverted = db.Column(db.Boolean, default=False)  # Было ли отменено
    reverted_at = db.Column(db.DateTime)  # Когда отменено
    reverted_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'))  # Кто откатил

    # Метаданные
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    completed_at = db.Column(db.DateTime)  # Когда завершено
    duration_seconds = db.Column(db.Float)  # Длительность выполнения

    # Связи
    product_changes = db.relationship('CardEditHistory', backref='bulk_operation', lazy='dynamic')

    def __repr__(self) -> str:
        return f'<BulkEditHistory {self.operation_type} ({self.success_count}/{self.total_products})>'

    def can_revert(self) -> bool:
        """Можно ли откатить эту операцию"""
        return (
            not self.reverted and
            self.status == 'completed' and
            self.success_count > 0 and
            self.wb_synced
        )

    def get_progress_percent(self) -> float:
        """Получить процент выполнения"""
        if self.total_products == 0:
            return 0.0
        processed = self.success_count + self.error_count
        return (processed / self.total_products) * 100

    def to_dict(self) -> dict:
        """Конвертировать в словарь для JSON"""
        return {
            'id': self.id,
            'operation_type': self.operation_type,
            'description': self.description,
            'total_products': self.total_products,
            'success_count': self.success_count,
            'error_count': self.error_count,
            'status': self.status,
            'reverted': self.reverted,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'duration_seconds': self.duration_seconds,
            'progress_percent': self.get_progress_percent()
        }


class CardEditHistory(db.Model):
    """История изменений карточек товаров"""
    __tablename__ = 'card_edit_history'

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False, index=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)

    # Связь с bulk операцией (если это часть массового изменения)
    bulk_edit_id = db.Column(db.Integer, db.ForeignKey('bulk_edit_history.id'), index=True)

    # Что изменилось
    action = db.Column(db.String(50), nullable=False)  # 'update', 'create', 'delete'
    changed_fields = db.Column(db.JSON)  # Список измененных полей

    # Снимок данных ДО изменения
    snapshot_before = db.Column(db.JSON)  # Полное состояние карточки до изменения

    # Снимок данных ПОСЛЕ изменения
    snapshot_after = db.Column(db.JSON)  # Полное состояние карточки после изменения

    # Результат синхронизации с WB
    wb_synced = db.Column(db.Boolean, default=False)  # Синхронизировано ли с WB
    wb_sync_status = db.Column(db.String(50))  # 'success', 'failed', 'pending'
    wb_error_message = db.Column(db.Text)  # Сообщение об ошибке от WB

    # Откат
    reverted = db.Column(db.Boolean, default=False)  # Было ли отменено
    reverted_at = db.Column(db.DateTime)  # Когда отменено
    reverted_by_history_id = db.Column(db.Integer, db.ForeignKey('card_edit_history.id'))  # ID истории отката

    # Метаданные
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    user_comment = db.Column(db.Text)  # Комментарий пользователя

    # Связи
    product = db.relationship('Product', backref='edit_history')

    def __repr__(self) -> str:
        return f'<CardEditHistory {self.action} product_id={self.product_id}>'

    def can_revert(self) -> bool:
        """Можно ли откатить это изменение"""
        return not self.reverted and self.snapshot_before is not None

    def get_changes_summary(self) -> dict:
        """Получить краткую сводку изменений"""
        if not self.changed_fields:
            return {}

        summary = {}
        for field in self.changed_fields:
            before = self.snapshot_before.get(field) if self.snapshot_before else None
            after = self.snapshot_after.get(field) if self.snapshot_after else None

            # Форматируем для отображения
            if isinstance(before, list) and field == 'characteristics':
                # Для характеристик показываем количество
                before_display = f"{len(before)} характеристик" if before else None
            elif isinstance(before, list):
                before_display = str(before) if before else None
            else:
                before_display = before

            if isinstance(after, list) and field == 'characteristics':
                after_display = f"{len(after)} характеристик" if after else None
            elif isinstance(after, list):
                after_display = str(after) if after else None
            else:
                after_display = after

            summary[field] = {
                'before': before_display,
                'after': after_display,
                'before_raw': before,
                'after_raw': after
            }

        return summary


class ProductStock(db.Model):
    """Остатки товаров по складам"""
    __tablename__ = 'product_stocks'

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='CASCADE'), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, index=True)  # ID склада WB
    warehouse_name = db.Column(db.String(200))  # Название склада

    # Остатки
    quantity = db.Column(db.Integer, default=0)  # Доступный остаток
    quantity_full = db.Column(db.Integer, default=0)  # Полный остаток
    in_way_to_client = db.Column(db.Integer, default=0)  # В пути к клиенту
    in_way_from_client = db.Column(db.Integer, default=0)  # В пути от клиента

    # Метаданные
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связь с товаром
    product = db.relationship('Product', backref=db.backref('stocks', lazy='dynamic'))

    # Уникальный индекс product_id + warehouse_id
    __table_args__ = (
        db.UniqueConstraint('product_id', 'warehouse_id', name='uq_product_warehouse'),
        db.Index('idx_product_stocks_product_id', 'product_id'),
        db.Index('idx_product_stocks_warehouse_id', 'warehouse_id'),
    )

    def __repr__(self) -> str:
        return f'<ProductStock product={self.product_id} warehouse={self.warehouse_name} qty={self.quantity}>'

    def to_dict(self):
        """Конвертировать в словарь для JSON"""
        return {
            'warehouse_id': self.warehouse_id,
            'warehouse_name': self.warehouse_name,
            'quantity': self.quantity,
            'quantity_full': self.quantity_full,
            'in_way_to_client': self.in_way_to_client,
            'in_way_from_client': self.in_way_from_client,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class PriceMonitorSettings(db.Model):
    """Настройки мониторинга цен для продавца"""
    __tablename__ = 'price_monitor_settings'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, unique=True, index=True)

    # Настройки мониторинга
    is_enabled = db.Column(db.Boolean, default=False, nullable=False)  # Включен ли мониторинг
    monitor_prices = db.Column(db.Boolean, default=True, nullable=False)  # Мониторить цены
    monitor_stocks = db.Column(db.Boolean, default=False, nullable=False)  # Мониторить остатки

    # Частота синхронизации (в минутах)
    sync_interval_minutes = db.Column(db.Integer, default=60, nullable=False)  # По умолчанию раз в час

    # Процент допустимого скачка цены
    price_change_threshold_percent = db.Column(db.Float, default=10.0, nullable=False)  # По умолчанию 10%

    # Процент допустимого скачка остатков
    stock_change_threshold_percent = db.Column(db.Float, default=50.0, nullable=False)  # По умолчанию 50%

    # Последняя синхронизация
    last_sync_at = db.Column(db.DateTime)  # Когда была последняя синхронизация
    last_sync_status = db.Column(db.String(50))  # Статус последней синхронизации ('success', 'failed', 'running')
    last_sync_error = db.Column(db.Text)  # Ошибка последней синхронизации

    # Метаданные
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связь с продавцом
    seller = db.relationship('Seller', backref=db.backref('price_monitor_settings', uselist=False))

    def __repr__(self) -> str:
        return f'<PriceMonitorSettings seller_id={self.seller_id} enabled={self.is_enabled}>'

    def to_dict(self) -> dict:
        """Конвертировать в словарь для JSON"""
        return {
            'id': self.id,
            'seller_id': self.seller_id,
            'is_enabled': self.is_enabled,
            'monitor_prices': self.monitor_prices,
            'monitor_stocks': self.monitor_stocks,
            'sync_interval_minutes': self.sync_interval_minutes,
            'price_change_threshold_percent': self.price_change_threshold_percent,
            'stock_change_threshold_percent': self.stock_change_threshold_percent,
            'last_sync_at': self.last_sync_at.isoformat() if self.last_sync_at else None,
            'last_sync_status': self.last_sync_status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class ProductSyncSettings(db.Model):
    """Настройки автоматической синхронизации товаров"""
    __tablename__ = 'product_sync_settings'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, unique=True, index=True)

    # Настройки автосинхронизации
    is_enabled = db.Column(db.Boolean, default=False, nullable=False)  # Включена ли автосинхронизация
    sync_interval_minutes = db.Column(db.Integer, default=60, nullable=False)  # Частота синхронизации (по умолчанию раз в час)

    # Типы синхронизации
    sync_products = db.Column(db.Boolean, default=True, nullable=False)  # Синхронизировать карточки товаров
    sync_stocks = db.Column(db.Boolean, default=True, nullable=False)  # Синхронизировать остатки

    # Последняя синхронизация
    last_sync_at = db.Column(db.DateTime)  # Когда была последняя синхронизация
    next_sync_at = db.Column(db.DateTime)  # Когда запланирована следующая синхронизация
    last_sync_status = db.Column(db.String(50))  # Статус ('success', 'failed', 'running')
    last_sync_error = db.Column(db.Text)  # Текст ошибки если была
    last_sync_duration = db.Column(db.Float)  # Длительность последней синхронизации в секундах

    # Статистика
    products_synced = db.Column(db.Integer, default=0)  # Количество синхронизированных товаров
    products_added = db.Column(db.Integer, default=0)  # Количество добавленных товаров
    products_updated = db.Column(db.Integer, default=0)  # Количество обновленных товаров

    # Метаданные
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связь с продавцом
    seller = db.relationship('Seller', backref=db.backref('product_sync_settings', uselist=False))

    def __repr__(self) -> str:
        return f'<ProductSyncSettings seller_id={self.seller_id} enabled={self.is_enabled}>'

    def to_dict(self) -> dict:
        """Конвертировать в словарь для JSON"""
        return {
            'id': self.id,
            'seller_id': self.seller_id,
            'is_enabled': self.is_enabled,
            'sync_interval_minutes': self.sync_interval_minutes,
            'sync_products': self.sync_products,
            'sync_stocks': self.sync_stocks,
            'last_sync_at': self.last_sync_at.isoformat() if self.last_sync_at else None,
            'next_sync_at': self.next_sync_at.isoformat() if self.next_sync_at else None,
            'last_sync_status': self.last_sync_status,
            'last_sync_duration': self.last_sync_duration,
            'products_synced': self.products_synced,
            'products_added': self.products_added,
            'products_updated': self.products_updated,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class AutoImportSettings(db.Model):
    """Настройки автоимпорта товаров из внешних источников"""
    __tablename__ = 'auto_import_settings'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, unique=True, index=True)

    # Основные настройки
    is_enabled = db.Column(db.Boolean, default=False, nullable=False)  # Включен ли автоимпорт
    supplier_code = db.Column(db.String(50))  # Код продавца для формирования артикулов

    # Шаблон артикула (regex pattern)
    vendor_code_pattern = db.Column(db.String(200), default='id-{product_id}-{supplier_code}')  # Шаблон артикула

    # URL источника данных
    csv_source_url = db.Column(db.String(500))  # URL CSV файла с товарами
    csv_source_type = db.Column(db.String(50), default='sexoptovik')  # Тип источника (sexoptovik, fixprice, custom)
    csv_delimiter = db.Column(db.String(5), default=';')  # Разделитель полей в CSV

    # Авторизация для доступа к фотографиям sexoptovik
    sexoptovik_login = db.Column(db.String(200))  # Логин для sexoptovik.ru
    _sexoptovik_password_encrypted = db.Column('sexoptovik_password', db.String(500))  # Пароль (зашифрованный)

    @property
    def sexoptovik_password(self) -> Optional[str]:
        """Расшифровать пароль sexoptovik."""
        if not self._sexoptovik_password_encrypted:
            return None
        encryption_key = os.environ.get('ENCRYPTION_KEY')
        if not encryption_key:
            return self._sexoptovik_password_encrypted
        try:
            f = Fernet(encryption_key.encode())
            return f.decrypt(self._sexoptovik_password_encrypted.encode()).decode()
        except Exception:
            return self._sexoptovik_password_encrypted

    @sexoptovik_password.setter
    def sexoptovik_password(self, value: Optional[str]) -> None:
        """Зашифровать пароль sexoptovik."""
        if value is None or value == '':
            self._sexoptovik_password_encrypted = value
            return
        encryption_key = os.environ.get('ENCRYPTION_KEY')
        if not encryption_key:
            self._sexoptovik_password_encrypted = value
            return
        try:
            f = Fernet(encryption_key.encode())
            self._sexoptovik_password_encrypted = f.encrypt(value.encode()).decode()
        except Exception:
            self._sexoptovik_password_encrypted = value

    # Настройки импорта
    import_only_new = db.Column(db.Boolean, default=True, nullable=False)  # Импортировать только новые товары
    auto_enable_products = db.Column(db.Boolean, default=False, nullable=False)  # Автоматически активировать товары
    use_blurred_images = db.Column(db.Boolean, default=True, nullable=False)  # Использовать блюренные фото когда доступно

    # Настройки обработки фото
    resize_images_to_1200 = db.Column(db.Boolean, default=True, nullable=False)  # Приводить к 1200x1200
    image_background_color = db.Column(db.String(20), default='white')  # Цвет фона для дорисовки

    # AI настройки
    ai_enabled = db.Column(db.Boolean, default=False, nullable=False)  # Использовать AI для определения категорий/размеров
    ai_provider = db.Column(db.String(50), default='openai')  # Провайдер AI (openai, cloudru, custom)
    ai_api_key = db.Column(db.String(500))  # API ключ для AI
    ai_api_base_url = db.Column(db.String(500))  # Базовый URL API (для custom провайдеров)
    ai_model = db.Column(db.String(100), default='gpt-4o-mini')  # Модель AI
    ai_temperature = db.Column(db.Float, default=0.3)  # Температура для AI
    ai_max_tokens = db.Column(db.Integer, default=2000)  # Максимум токенов
    ai_timeout = db.Column(db.Integer, default=60)  # Таймаут запросов в секундах
    ai_use_for_categories = db.Column(db.Boolean, default=True, nullable=False)  # Использовать AI для категорий
    ai_use_for_sizes = db.Column(db.Boolean, default=True, nullable=False)  # Использовать AI для размеров
    ai_category_confidence_threshold = db.Column(db.Float, default=0.7)  # Минимальная уверенность AI для принятия категории
    # Дополнительные параметры AI для Cloud.ru
    ai_top_p = db.Column(db.Float, default=0.95)  # Top P для семплирования
    ai_presence_penalty = db.Column(db.Float, default=0.0)  # Штраф за присутствие
    ai_frequency_penalty = db.Column(db.Float, default=0.0)  # Штраф за частоту
    # Кастомные инструкции AI для каждой функции
    ai_category_instruction = db.Column(db.Text)  # Кастомная инструкция для определения категорий
    ai_size_instruction = db.Column(db.Text)  # Кастомная инструкция для парсинга размеров
    ai_seo_title_instruction = db.Column(db.Text)  # Кастомная инструкция для SEO заголовков
    ai_keywords_instruction = db.Column(db.Text)  # Кастомная инструкция для ключевых слов
    ai_bullets_instruction = db.Column(db.Text)  # Кастомная инструкция для преимуществ
    ai_description_instruction = db.Column(db.Text)  # Кастомная инструкция для описания
    ai_rich_content_instruction = db.Column(db.Text)  # Кастомная инструкция для Rich контента
    ai_analysis_instruction = db.Column(db.Text)  # Кастомная инструкция для анализа карточки
    # Новые кастомные инструкции для расширенного анализа
    ai_dimensions_instruction = db.Column(db.Text)  # Инструкция для извлечения габаритов
    ai_clothing_sizes_instruction = db.Column(db.Text)  # Инструкция для размеров одежды
    ai_brand_instruction = db.Column(db.Text)  # Инструкция для определения бренда
    ai_material_instruction = db.Column(db.Text)  # Инструкция для определения материалов
    ai_color_instruction = db.Column(db.Text)  # Инструкция для определения цвета
    ai_attributes_instruction = db.Column(db.Text)  # Инструкция для комплексного анализа
    # Cloud.ru OAuth2 credentials (вместо простого API ключа)
    ai_client_id = db.Column(db.String(500))  # Client ID для Cloud.ru OAuth2
    ai_client_secret = db.Column(db.String(500))  # Client Secret для Cloud.ru OAuth2

    # Настройки генерации изображений для инфографики
    image_gen_enabled = db.Column(db.Boolean, default=False, nullable=False)  # Включена генерация картинок
    image_gen_provider = db.Column(db.String(50), default='fluxapi')  # Провайдер (fluxapi рекомендуется!)
    # API ключи для разных провайдеров
    fluxapi_key = db.Column(db.String(500))  # FluxAPI.ai (рекомендуется - есть trial)
    tensorart_app_id = db.Column(db.String(500))  # Tensor.art App ID
    tensorart_api_key = db.Column(db.String(500))  # Tensor.art API Key
    together_api_key = db.Column(db.String(500))  # Together AI
    openai_api_key = db.Column(db.String(500))  # OpenAI DALL-E
    replicate_api_key = db.Column(db.String(500))  # Replicate (Flux/SDXL)
    image_gen_width = db.Column(db.Integer, default=1440)  # Ширина изображения
    image_gen_height = db.Column(db.Integer, default=810)  # Высота изображения
    openai_image_quality = db.Column(db.String(20), default='standard')  # standard или hd
    openai_image_style = db.Column(db.String(20), default='vivid')  # vivid или natural

    # Частота автоимпорта (в часах)
    auto_import_interval_hours = db.Column(db.Integer, default=24, nullable=False)  # По умолчанию раз в сутки

    # Последний импорт
    last_import_at = db.Column(db.DateTime)  # Когда был последний импорт
    next_import_at = db.Column(db.DateTime)  # Когда запланирован следующий импорт
    last_import_status = db.Column(db.String(50))  # Статус ('success', 'failed', 'running')
    last_import_error = db.Column(db.Text)  # Текст ошибки если была
    last_import_duration = db.Column(db.Float)  # Длительность последнего импорта в секундах

    # Статистика
    total_products_found = db.Column(db.Integer, default=0)  # Найдено товаров в источнике
    products_imported = db.Column(db.Integer, default=0)  # Импортировано товаров
    products_skipped = db.Column(db.Integer, default=0)  # Пропущено (уже есть)
    products_failed = db.Column(db.Integer, default=0)  # Ошибки импорта

    # Метаданные
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связь с продавцом
    seller = db.relationship('Seller', backref=db.backref('auto_import_settings', uselist=False))

    def __repr__(self) -> str:
        return f'<AutoImportSettings seller_id={self.seller_id} enabled={self.is_enabled}>'

    def to_dict(self) -> dict:
        """Конвертировать в словарь для JSON"""
        return {
            'id': self.id,
            'seller_id': self.seller_id,
            'is_enabled': self.is_enabled,
            'supplier_code': self.supplier_code,
            'vendor_code_pattern': self.vendor_code_pattern,
            'csv_source_url': self.csv_source_url,
            'csv_source_type': self.csv_source_type,
            'csv_delimiter': self.csv_delimiter,
            'sexoptovik_login': self.sexoptovik_login,  # Только логин, пароль не отдаем
            'import_only_new': self.import_only_new,
            'auto_enable_products': self.auto_enable_products,
            'use_blurred_images': self.use_blurred_images,
            'resize_images_to_1200': self.resize_images_to_1200,
            'image_background_color': self.image_background_color,
            # AI настройки
            'ai_enabled': self.ai_enabled,
            'ai_provider': self.ai_provider,
            'ai_api_base_url': self.ai_api_base_url,
            'ai_model': self.ai_model,
            'ai_temperature': self.ai_temperature,
            'ai_max_tokens': self.ai_max_tokens,
            'ai_timeout': self.ai_timeout,
            'ai_use_for_categories': self.ai_use_for_categories,
            'ai_use_for_sizes': self.ai_use_for_sizes,
            'ai_category_confidence_threshold': self.ai_category_confidence_threshold,
            'ai_top_p': self.ai_top_p,
            'ai_presence_penalty': self.ai_presence_penalty,
            'ai_frequency_penalty': self.ai_frequency_penalty,
            'ai_category_instruction': self.ai_category_instruction,
            'ai_size_instruction': self.ai_size_instruction,
            'ai_seo_title_instruction': self.ai_seo_title_instruction,
            'ai_keywords_instruction': self.ai_keywords_instruction,
            'ai_bullets_instruction': self.ai_bullets_instruction,
            'ai_description_instruction': self.ai_description_instruction,
            'ai_rich_content_instruction': self.ai_rich_content_instruction,
            'ai_analysis_instruction': self.ai_analysis_instruction,
            # Не отдаем ai_api_key в JSON из соображений безопасности
            # Настройки генерации изображений
            'image_gen_enabled': self.image_gen_enabled,
            'image_gen_provider': self.image_gen_provider,
            'image_gen_width': self.image_gen_width,
            'image_gen_height': self.image_gen_height,
            'openai_image_quality': self.openai_image_quality,
            'openai_image_style': self.openai_image_style,
            # Не отдаем API ключи в JSON
            'auto_import_interval_hours': self.auto_import_interval_hours,
            'last_import_at': self.last_import_at.isoformat() if self.last_import_at else None,
            'next_import_at': self.next_import_at.isoformat() if self.next_import_at else None,
            'last_import_status': self.last_import_status,
            'last_import_duration': self.last_import_duration,
            'total_products_found': self.total_products_found,
            'products_imported': self.products_imported,
            'products_skipped': self.products_skipped,
            'products_failed': self.products_failed,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class ProductDefaults(db.Model):
    """Дефолтные значения габаритов/веса и глобальное медиа для товаров продавца"""
    __tablename__ = 'product_defaults'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)

    # Тип правила: 'global' (для всех) или 'category' (для конкретной категории WB)
    rule_type = db.Column(db.String(20), default='global', nullable=False)

    # Привязка к категории (если rule_type='category')
    wb_subject_id = db.Column(db.Integer, nullable=True)  # ID категории WB
    wb_category_name = db.Column(db.String(300), nullable=True)  # Название для отображения

    # Дефолтные габариты упаковки (см)
    length_cm = db.Column(db.Float, nullable=True)  # Длина
    width_cm = db.Column(db.Float, nullable=True)   # Ширина
    height_cm = db.Column(db.Float, nullable=True)   # Высота

    # Дефолтный вес брутто (кг)
    weight_kg = db.Column(db.Float, nullable=True)

    # Дефолтные характеристики товаров
    # JSON: {"Страна производства": "Китай", "Цвет": "Черный", ...}
    default_characteristics = db.Column(db.Text, nullable=True)

    # Глобальное медиа — файлы, добавляемые ко всем карточкам продавца
    # JSON: [{"filename": "...", "original_name": "...", "type": "photo|video", "size": 12345}]
    global_media = db.Column(db.Text, nullable=True)

    # Активность правила
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    # Приоритет (чем выше — тем важнее, категорийные > глобальных)
    priority = db.Column(db.Integer, default=0, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    seller = db.relationship('Seller', backref=db.backref('product_defaults', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('seller_id', 'rule_type', 'wb_subject_id', name='uq_product_defaults_rule'),
    )

    def __repr__(self):
        if self.rule_type == 'category':
            return f'<ProductDefaults seller={self.seller_id} category={self.wb_category_name}>'
        return f'<ProductDefaults seller={self.seller_id} global>'

    def has_dimensions(self):
        return any([self.length_cm, self.width_cm, self.height_cm, self.weight_kg])

    def get_dimensions_dict(self):
        """Вернуть габариты в формате WB API"""
        d = {}
        if self.length_cm is not None:
            d['length'] = self.length_cm
        if self.width_cm is not None:
            d['width'] = self.width_cm
        if self.height_cm is not None:
            d['height'] = self.height_cm
        if self.weight_kg is not None:
            d['weightBrutto'] = self.weight_kg
        return d

    def get_default_characteristics(self):
        """Вернуть словарь дефолтных характеристик"""
        if not self.default_characteristics:
            return {}
        try:
            return json.loads(self.default_characteristics)
        except (json.JSONDecodeError, TypeError):
            return {}

    def set_default_characteristics(self, chars_dict):
        """Установить дефолтные характеристики"""
        self.default_characteristics = json.dumps(chars_dict, ensure_ascii=False) if chars_dict else None

    def get_global_media_list(self):
        """Вернуть список глобальных медиа"""
        if not self.global_media:
            return []
        try:
            return json.loads(self.global_media)
        except (json.JSONDecodeError, TypeError):
            return []


class ImportedProduct(db.Model):
    """Импортированные товары из внешних источников"""
    __tablename__ = 'imported_products'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True, index=True)  # Ссылка на созданный товар (если создан)

    # Связь с централизованной базой поставщика
    supplier_product_id = db.Column(db.Integer, db.ForeignKey('supplier_products.id'), nullable=True, index=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=True, index=True)

    # Исходные данные из CSV
    external_id = db.Column(db.String(200), index=True)  # ID из внешнего источника
    external_vendor_code = db.Column(db.String(200))  # Артикул поставщика из источника
    source_type = db.Column(db.String(50), default='sexoptovik')  # Источник данных

    # Данные товара
    title = db.Column(db.String(500))  # Название
    category = db.Column(db.String(200))  # Категория из источника
    all_categories = db.Column(db.Text)  # Все категории из цепочки (JSON)
    mapped_wb_category = db.Column(db.String(200))  # Маппированная категория WB
    wb_subject_id = db.Column(db.Integer)  # ID предмета WB
    category_confidence = db.Column(db.Float, default=0.0)  # Уверенность в определении категории (0-1)

    brand = db.Column(db.String(200))  # Бренд
    resolved_brand_id = db.Column(db.Integer, db.ForeignKey('brands.id'), nullable=True, index=True)
    brand_status = db.Column(db.String(20))  # exact/confident/uncertain/unresolved
    country = db.Column(db.String(100))  # Страна производства
    gender = db.Column(db.String(50))  # Пол
    colors = db.Column(db.Text)  # Цвета (JSON)
    sizes = db.Column(db.Text)  # Размеры (JSON)
    materials = db.Column(db.Text)  # Материалы (JSON)

    # Медиа
    photo_urls = db.Column(db.Text)  # URLs фотографий (JSON)
    processed_photos = db.Column(db.Text)  # Обработанные фотографии (JSON)

    # Характеристики
    barcodes = db.Column(db.Text)  # Баркоды (JSON)
    characteristics = db.Column(db.Text)  # Полные характеристики (JSON)
    description = db.Column(db.Text)  # Описание
    original_data = db.Column(db.Text)  # Оригинальные данные от поставщика (JSON) - для отката AI изменений

    # Цена поставщика и рассчитанные цены
    supplier_price = db.Column(db.Float, nullable=True)  # Закупочная цена из CSV поставщика
    supplier_quantity = db.Column(db.Integer, nullable=True, default=None)  # Остаток на складе поставщика
    calculated_price = db.Column(db.Float, nullable=True)  # Z — итоговая цена
    calculated_discount_price = db.Column(db.Float, nullable=True)  # X — цена с SPP скидкой
    calculated_price_before_discount = db.Column(db.Float, nullable=True)  # Y — завышенная цена до скидки

    # Статус импорта
    import_status = db.Column(db.String(50), default='pending')  # 'pending', 'validated', 'imported', 'failed'
    validation_errors = db.Column(db.Text)  # Ошибки валидации (JSON)
    import_error = db.Column(db.Text)  # Ошибка импорта

    # AI-оптимизация (кэшированные результаты)
    ai_keywords = db.Column(db.Text)  # Ключевые слова (JSON)
    ai_bullets = db.Column(db.Text)  # Преимущества/буллиты (JSON)
    ai_rich_content = db.Column(db.Text)  # Rich контент (JSON)
    ai_seo_title = db.Column(db.String(500))  # SEO заголовок
    ai_analysis = db.Column(db.Text)  # Последний анализ карточки (JSON)
    ai_analysis_at = db.Column(db.DateTime)  # Когда был сделан анализ
    content_hash = db.Column(db.String(64))  # Хеш контента для отслеживания изменений

    # Новые AI поля для расширенного анализа
    ai_dimensions = db.Column(db.Text)  # Габариты (JSON) - length, width, height, weight
    ai_clothing_sizes = db.Column(db.Text)  # Размеры одежды (JSON) - стандартизированные
    ai_detected_brand = db.Column(db.Text)  # Определенный AI бренд (JSON)
    ai_materials = db.Column(db.Text)  # Материалы и состав (JSON)
    ai_colors = db.Column(db.Text)  # Цвета товара (JSON)
    ai_attributes = db.Column(db.Text)  # Полный набор атрибутов (JSON)
    ai_gender = db.Column(db.String(20))  # Пол: male/female/unisex
    ai_age_group = db.Column(db.String(20))  # Возрастная группа
    ai_season = db.Column(db.String(20))  # Сезон: all_season/summer/winter/demi
    ai_country = db.Column(db.String(100))  # Страна производства

    # Метаданные
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    imported_at = db.Column(db.DateTime)  # Когда импортировано в WB
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связи
    product = db.relationship('Product', backref=db.backref('import_source', uselist=False))
    supplier_product = db.relationship('SupplierProduct', backref=db.backref('imported_copies', lazy='dynamic'))
    supplier = db.relationship('Supplier', backref=db.backref('imported_products', lazy='dynamic'))

    # Индексы
    __table_args__ = (
        db.Index('idx_imported_seller_status', 'seller_id', 'import_status'),
        db.Index('idx_imported_external_id', 'external_id', 'source_type'),
        db.Index('idx_imported_supplier_product', 'supplier_product_id'),
    )

    def __repr__(self) -> str:
        return f'<ImportedProduct {self.external_id} status={self.import_status}>'

    def to_dict(self) -> dict:
        """Конвертировать в словарь для JSON"""
        import json
        return {
            'id': self.id,
            'external_id': self.external_id,
            'external_vendor_code': self.external_vendor_code,
            'title': self.title,
            'category': self.category,
            'mapped_wb_category': self.mapped_wb_category,
            'brand': self.brand,
            'import_status': self.import_status,
            'validation_errors': json.loads(self.validation_errors) if self.validation_errors else [],
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'imported_at': self.imported_at.isoformat() if self.imported_at else None
        }


class PricingSettings(db.Model):
    """Настройки формулы ценообразования для продавца"""
    __tablename__ = 'pricing_settings'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, unique=True, index=True)

    is_enabled = db.Column(db.Boolean, default=False, nullable=False)
    formula_type = db.Column(db.String(50), default='standard')  # standard, custom

    # URL файлов цен поставщика
    supplier_price_url = db.Column(db.String(500))
    supplier_price_inf_url = db.Column(db.String(500))
    last_price_sync_at = db.Column(db.DateTime)
    last_price_file_hash = db.Column(db.String(64))

    # Комиссия WB (%)
    wb_commission_pct = db.Column(db.Float, default=40.0, nullable=False)

    # Налоговый коэффициент
    tax_rate = db.Column(db.Float, default=1.13, nullable=False)

    # Константы формулы R (цена без доставки)
    logistics_cost = db.Column(db.Float, default=55.0, nullable=False)
    storage_cost = db.Column(db.Float, default=0.0, nullable=False)
    packaging_cost = db.Column(db.Float, default=20.0, nullable=False)

    # Константы формулы Z (итоговая цена)
    acquiring_cost = db.Column(db.Float, default=25.0, nullable=False)
    extra_cost = db.Column(db.Float, default=20.0, nullable=False)

    # Формула S (стоимость доставки)
    delivery_pct = db.Column(db.Float, default=5.0, nullable=False)
    delivery_min = db.Column(db.Float, default=55.0, nullable=False)
    delivery_max = db.Column(db.Float, default=205.0, nullable=False)

    # Колонка прибыли из таблицы наценок (A/B/C/D)
    profit_column = db.Column(db.String(1), default='d', nullable=False)

    # Желаемая прибыль Q
    min_profit = db.Column(db.Float, default=30.0, nullable=False)
    max_profit = db.Column(db.Float, nullable=True)

    # Случайная добавка к Q
    use_random = db.Column(db.Boolean, default=False, nullable=False)
    random_min = db.Column(db.Integer, default=1, nullable=False)
    random_max = db.Column(db.Integer, default=10, nullable=False)

    # SPP (Скидка постоянного покупателя)
    spp_pct = db.Column(db.Float, default=5.0, nullable=False)
    spp_min = db.Column(db.Float, default=20.0, nullable=False)
    spp_max = db.Column(db.Float, default=500.0, nullable=False)

    # Множитель завышенной цены (Y = Z * множитель)
    inflated_multiplier = db.Column(db.Float, default=1.55, nullable=False)

    # Таблица наценок (JSON)
    # Формат: [{"from": 1, "to": 100, "a": 35, "b": 0, "c": 0, "d": 38}, ...]
    price_ranges = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    seller = db.relationship('Seller', backref=db.backref('pricing_settings', uselist=False))

    def __repr__(self) -> str:
        return f'<PricingSettings seller_id={self.seller_id} enabled={self.is_enabled}>'


class CategoryMapping(db.Model):
    """Маппинг категорий из внешних источников в категории WB"""
    __tablename__ = 'category_mappings'

    id = db.Column(db.Integer, primary_key=True)

    # Исходная категория
    source_category = db.Column(db.String(200), nullable=False, index=True)  # Категория из источника
    source_type = db.Column(db.String(50), default='sexoptovik')  # Тип источника

    # Целевая категория WB
    wb_category_name = db.Column(db.String(200), nullable=False)  # Название категории WB
    wb_subject_id = db.Column(db.Integer, nullable=False)  # ID предмета WB
    wb_subject_name = db.Column(db.String(200))  # Название предмета WB

    # Приоритет (для случаев когда одна исходная категория может мапиться на несколько WB)
    priority = db.Column(db.Integer, default=0)  # Чем выше, тем приоритетнее

    # Автоматическое vs ручное
    is_auto_mapped = db.Column(db.Boolean, default=True)  # Автоматически определено или вручную
    confidence_score = db.Column(db.Float, default=0.0)  # Уверенность в маппинге (0-1)

    # Метаданные
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Индексы
    __table_args__ = (
        db.Index('idx_category_source', 'source_category', 'source_type'),
        db.UniqueConstraint('source_category', 'source_type', 'wb_subject_id', name='uq_category_mapping'),
    )

    def __repr__(self) -> str:
        return f'<CategoryMapping {self.source_category} -> {self.wb_category_name}>'


class ProductCategoryCorrection(db.Model):
    """Ручные исправления категорий для конкретных товаров"""
    __tablename__ = 'product_category_corrections'

    id = db.Column(db.Integer, primary_key=True)

    # Связь с импортированным товаром
    imported_product_id = db.Column(db.Integer, db.ForeignKey('imported_products.id'), nullable=True, index=True)

    # Идентификация товара (для переиспользования при повторном импорте)
    external_id = db.Column(db.String(200), index=True)  # ID из внешнего источника
    source_type = db.Column(db.String(50), default='sexoptovik')
    product_title = db.Column(db.String(500))  # Название товара
    original_category = db.Column(db.String(200))  # Оригинальная категория из CSV

    # Исправленная категория WB
    corrected_wb_subject_id = db.Column(db.Integer, nullable=False)  # ID предмета WB
    corrected_wb_subject_name = db.Column(db.String(200))  # Название предмета WB

    # Метаданные
    corrected_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'))  # Кто исправил
    correction_reason = db.Column(db.Text)  # Причина исправления (опционально)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связи
    imported_product = db.relationship('ImportedProduct', backref=db.backref('category_correction', uselist=False))
    corrected_by = db.relationship('User', backref='category_corrections')

    # Индексы
    __table_args__ = (
        db.Index('idx_correction_external', 'external_id', 'source_type'),
        db.Index('idx_correction_category', 'original_category', 'source_type'),
    )

    def __repr__(self) -> str:
        return f'<ProductCategoryCorrection {self.external_id} -> {self.corrected_wb_subject_name}>'


class PriceHistory(db.Model):
    """История изменений цен и остатков товаров"""
    __tablename__ = 'price_history'

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='CASCADE'), nullable=False, index=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)

    # Предыдущие значения
    old_price = db.Column(db.Numeric(10, 2))  # Старая цена
    old_discount_price = db.Column(db.Numeric(10, 2))  # Старая цена со скидкой
    old_quantity = db.Column(db.Integer)  # Старый остаток

    # Новые значения
    new_price = db.Column(db.Numeric(10, 2))  # Новая цена
    new_discount_price = db.Column(db.Numeric(10, 2))  # Новая цена со скидкой
    new_quantity = db.Column(db.Integer)  # Новый остаток

    # Изменения в процентах
    price_change_percent = db.Column(db.Float)  # Изменение цены в %
    discount_price_change_percent = db.Column(db.Float)  # Изменение цены со скидкой в %
    quantity_change_percent = db.Column(db.Float)  # Изменение остатка в %

    # Метаданные
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Связь с товаром
    product = db.relationship('Product', backref=db.backref('price_history', lazy='dynamic', order_by='PriceHistory.created_at.desc()'))

    # Индексы
    __table_args__ = (
        db.Index('idx_price_history_product_created', 'product_id', 'created_at'),
        db.Index('idx_price_history_seller_created', 'seller_id', 'created_at'),
    )

    def __repr__(self) -> str:
        return f'<PriceHistory product_id={self.product_id} price={self.old_price}->{self.new_price}>'

    def to_dict(self) -> dict:
        """Конвертировать в словарь для JSON"""
        return {
            'id': self.id,
            'product_id': self.product_id,
            'old_price': float(self.old_price) if self.old_price else None,
            'old_discount_price': float(self.old_discount_price) if self.old_discount_price else None,
            'old_quantity': self.old_quantity,
            'new_price': float(self.new_price) if self.new_price else None,
            'new_discount_price': float(self.new_discount_price) if self.new_discount_price else None,
            'new_quantity': self.new_quantity,
            'price_change_percent': self.price_change_percent,
            'discount_price_change_percent': self.discount_price_change_percent,
            'quantity_change_percent': self.quantity_change_percent,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class SuspiciousPriceChange(db.Model):
    """Подозрительные изменения цен (скачки больше допустимого порога)"""
    __tablename__ = 'suspicious_price_changes'

    id = db.Column(db.Integer, primary_key=True)
    price_history_id = db.Column(db.Integer, db.ForeignKey('price_history.id', ondelete='CASCADE'), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='CASCADE'), nullable=False, index=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)

    # Тип изменения ('price', 'discount_price', 'quantity')
    change_type = db.Column(db.String(50), nullable=False, index=True)

    # Значения
    old_value = db.Column(db.Numeric(10, 2))
    new_value = db.Column(db.Numeric(10, 2))
    change_percent = db.Column(db.Float, nullable=False)  # Изменение в процентах

    # Порог, который был превышен
    threshold_percent = db.Column(db.Float, nullable=False)

    # Статус обработки
    is_reviewed = db.Column(db.Boolean, default=False, nullable=False, index=True)  # Просмотрено ли
    reviewed_at = db.Column(db.DateTime)  # Когда просмотрено
    reviewed_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'))  # Кто просмотрел
    notes = db.Column(db.Text)  # Заметки

    # Метаданные
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Связи
    price_history = db.relationship('PriceHistory', backref=db.backref('suspicious_changes', lazy='dynamic'))
    product = db.relationship('Product', backref=db.backref('suspicious_price_changes', lazy='dynamic'))

    # Индексы
    __table_args__ = (
        db.Index('idx_suspicious_seller_created', 'seller_id', 'created_at'),
        db.Index('idx_suspicious_seller_reviewed', 'seller_id', 'is_reviewed'),
        db.Index('idx_suspicious_product_created', 'product_id', 'created_at'),
    )

    def __repr__(self) -> str:
        return f'<SuspiciousPriceChange product_id={self.product_id} {self.change_type} {self.change_percent}%>'

    def to_dict(self) -> dict:
        """Конвертировать в словарь для JSON"""
        result = {
            'id': self.id,
            'product_id': self.product_id,
            'change_type': self.change_type,
            'old_value': float(self.old_value) if self.old_value else None,
            'new_value': float(self.new_value) if self.new_value else None,
            'change_percent': self.change_percent,
            'threshold_percent': self.threshold_percent,
            'is_reviewed': self.is_reviewed,
            'reviewed_at': self.reviewed_at.isoformat() if self.reviewed_at else None,
            'notes': self.notes,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

        # Добавляем информацию о товаре, если она есть
        if self.product:
            result['product'] = {
                'nm_id': self.product.nm_id,
                'vendor_code': self.product.vendor_code,
                'title': self.product.title,
                'brand': self.product.brand,
                'current_price': float(self.product.price) if self.product.price else None,
                'current_discount_price': float(self.product.discount_price) if self.product.discount_price else None,
                'current_quantity': self.product.quantity
            }

        return result


class CardMergeHistory(db.Model):
    """История объединений и разъединений карточек товаров"""
    __tablename__ = 'card_merge_history'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)

    # Тип операции
    operation_type = db.Column(db.String(20), nullable=False)  # 'merge' или 'unmerge'

    # Данные объединения (для merge)
    target_imt_id = db.Column(db.BigInteger, index=True)  # Целевой imtID (к которому присоединяли)
    merged_nm_ids = db.Column(db.JSON, nullable=False)  # Список nmID которые объединили/разъединили

    # Снимок состояния ДО операции
    snapshot_before = db.Column(db.JSON)  # {nmID: {imtID, vendor_code, title, ...}}

    # Снимок состояния ПОСЛЕ операции
    snapshot_after = db.Column(db.JSON)  # {nmID: {imtID, vendor_code, title, ...}}

    # Статус выполнения
    status = db.Column(db.String(50), default='pending')  # 'pending', 'in_progress', 'completed', 'failed'
    wb_synced = db.Column(db.Boolean, default=False)  # Синхронизировано ли с WB
    wb_sync_status = db.Column(db.String(50))  # 'success', 'failed'
    wb_error_message = db.Column(db.Text)  # Сообщение об ошибке от WB

    # Откат
    reverted = db.Column(db.Boolean, default=False)  # Было ли отменено
    reverted_at = db.Column(db.DateTime)  # Когда отменено
    reverted_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'))  # Кто откатил
    revert_operation_id = db.Column(db.Integer, db.ForeignKey('card_merge_history.id'))  # ID операции отката

    # Метаданные
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    completed_at = db.Column(db.DateTime)  # Когда завершено
    duration_seconds = db.Column(db.Float)  # Длительность выполнения

    # Пользовательский комментарий
    user_comment = db.Column(db.Text)

    # Связи
    reverted_by = db.relationship('User', foreign_keys=[reverted_by_user_id], backref='merge_reverts')
    revert_operation = db.relationship('CardMergeHistory', remote_side=[id], foreign_keys=[revert_operation_id])

    # Индексы
    __table_args__ = (
        db.Index('idx_merge_seller_created', 'seller_id', 'created_at'),
        db.Index('idx_merge_operation', 'operation_type', 'status'),
    )

    def __repr__(self) -> str:
        return f'<CardMergeHistory {self.operation_type} target_imt={self.target_imt_id} nm_count={len(self.merged_nm_ids) if self.merged_nm_ids else 0}>'

    def can_revert(self) -> bool:
        """Можно ли откатить эту операцию"""
        return (
            not self.reverted and
            self.status == 'completed' and
            self.wb_synced and
            self.wb_sync_status == 'success'
        )

    def to_dict(self) -> dict:
        """Конвертировать в словарь для JSON"""
        return {
            'id': self.id,
            'operation_type': self.operation_type,
            'target_imt_id': self.target_imt_id,
            'merged_nm_ids': self.merged_nm_ids,
            'status': self.status,
            'wb_synced': self.wb_synced,
            'wb_sync_status': self.wb_sync_status,
            'wb_error_message': self.wb_error_message,
            'reverted': self.reverted,
            'reverted_at': self.reverted_at.isoformat() if self.reverted_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'duration_seconds': self.duration_seconds,
            'user_comment': self.user_comment,
            'can_revert': self.can_revert()
        }


# ============= ADMIN PANEL MODELS =============

class UserActivity(db.Model):
    """Логирование активности пользователей"""
    __tablename__ = 'user_activity'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    action = db.Column(db.String(100), nullable=False, index=True)  # login, logout, view_page, etc.
    details = db.Column(db.Text)  # Дополнительная информация в JSON
    ip_address = db.Column(db.String(45))  # IPv4 или IPv6
    user_agent = db.Column(db.String(500))  # Browser user agent
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Связи
    user = db.relationship('User', backref=db.backref('activities', lazy='dynamic'))

    # Индексы
    __table_args__ = (
        db.Index('idx_activity_user_created', 'user_id', 'created_at'),
        db.Index('idx_activity_action_created', 'action', 'created_at'),
    )

    def __repr__(self) -> str:
        return f'<UserActivity user_id={self.user_id} action={self.action}>'

    def to_dict(self) -> dict:
        """Конвертировать в словарь для JSON"""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'username': self.user.username if self.user else None,
            'action': self.action,
            'details': self.details,
            'ip_address': self.ip_address,
            'user_agent': self.user_agent,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class AdminAuditLog(db.Model):
    """Логирование действий администраторов"""
    __tablename__ = 'admin_audit_log'

    id = db.Column(db.Integer, primary_key=True)
    admin_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    action = db.Column(db.String(100), nullable=False, index=True)  # create_user, delete_seller, etc.
    target_type = db.Column(db.String(50))  # user, seller, product, etc.
    target_id = db.Column(db.Integer)  # ID целевого объекта
    details = db.Column(db.Text)  # Подробности в JSON (что изменилось)
    ip_address = db.Column(db.String(45))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Связи
    admin_user = db.relationship('User', backref=db.backref('admin_actions', lazy='dynamic'))

    # Индексы
    __table_args__ = (
        db.Index('idx_audit_admin_created', 'admin_user_id', 'created_at'),
        db.Index('idx_audit_action_created', 'action', 'created_at'),
        db.Index('idx_audit_target', 'target_type', 'target_id'),
    )

    def __repr__(self) -> str:
        return f'<AdminAuditLog admin={self.admin_user_id} action={self.action}>'

    def to_dict(self) -> dict:
        """Конвертировать в словарь для JSON"""
        return {
            'id': self.id,
            'admin_user_id': self.admin_user_id,
            'admin_username': self.admin_user.username if self.admin_user else None,
            'action': self.action,
            'target_type': self.target_type,
            'target_id': self.target_id,
            'details': self.details,
            'ip_address': self.ip_address,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


# ============= SAFE PRICE CHANGE MODELS =============

class SafePriceChangeSettings(db.Model):
    """Настройки безопасного изменения цен для продавца"""
    __tablename__ = 'safe_price_change_settings'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, unique=True, index=True)

    # Основные настройки безопасности
    is_enabled = db.Column(db.Boolean, default=True, nullable=False)  # Включена ли защита

    # Пороги изменения цены (в процентах)
    safe_threshold_percent = db.Column(db.Float, default=10.0, nullable=False)  # До этого % - безопасно (зеленый)
    warning_threshold_percent = db.Column(db.Float, default=20.0, nullable=False)  # До этого % - предупреждение (желтый)
    # Выше warning_threshold_percent - опасно (красный), требует подтверждения

    # Режим работы
    # 'notify' - только уведомлять о больших изменениях
    # 'confirm' - требовать подтверждения для опасных изменений
    # 'block' - блокировать опасные изменения полностью
    mode = db.Column(db.String(20), default='confirm', nullable=False)

    # Дополнительные настройки
    require_comment_for_dangerous = db.Column(db.Boolean, default=True, nullable=False)  # Требовать комментарий для опасных
    allow_bulk_dangerous = db.Column(db.Boolean, default=False, nullable=False)  # Разрешить массовые опасные изменения
    max_products_per_batch = db.Column(db.Integer, default=1000, nullable=False)  # Макс. товаров в одном батче (увеличен до 1000)
    allow_unlimited_batch = db.Column(db.Boolean, default=True, nullable=False)  # Разрешить выбор всех товаров (игнорировать лимит)

    # Уведомления
    notify_on_dangerous = db.Column(db.Boolean, default=True, nullable=False)  # Уведомлять о попытках опасных изменений
    notify_email = db.Column(db.String(200))  # Email для уведомлений

    # Метаданные
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связь с продавцом
    seller = db.relationship('Seller', backref=db.backref('safe_price_settings', uselist=False))

    def __repr__(self) -> str:
        return f'<SafePriceChangeSettings seller_id={self.seller_id} mode={self.mode}>'

    def classify_change(self, old_price: float, new_price: float) -> str:
        """
        Классифицировать изменение цены

        Returns:
            'safe' - безопасное изменение (зеленый)
            'warning' - предупреждение (желтый)
            'dangerous' - опасное изменение (красный)
        """
        if old_price is None or old_price == 0:
            return 'safe' if new_price and new_price > 0 else 'warning'

        change_percent = abs((new_price - old_price) / old_price * 100)

        if change_percent <= self.safe_threshold_percent:
            return 'safe'
        elif change_percent <= self.warning_threshold_percent:
            return 'warning'
        else:
            return 'dangerous'

    def to_dict(self) -> dict:
        """Конвертировать в словарь для JSON"""
        return {
            'id': self.id,
            'seller_id': self.seller_id,
            'is_enabled': self.is_enabled,
            'safe_threshold_percent': self.safe_threshold_percent,
            'warning_threshold_percent': self.warning_threshold_percent,
            'mode': self.mode,
            'require_comment_for_dangerous': self.require_comment_for_dangerous,
            'allow_bulk_dangerous': self.allow_bulk_dangerous,
            'max_products_per_batch': self.max_products_per_batch,
            'notify_on_dangerous': self.notify_on_dangerous,
            'notify_email': self.notify_email,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class PriceChangeBatch(db.Model):
    """Пакет изменений цен (группа заявок)"""
    __tablename__ = 'price_change_batches'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)

    # Описание операции
    name = db.Column(db.String(200))  # Название операции (опционально)
    description = db.Column(db.Text)  # Описание / причина изменения
    change_type = db.Column(db.String(50), nullable=False)  # 'fixed', 'percent', 'formula'

    # Параметры изменения
    change_value = db.Column(db.Float)  # Значение изменения (число или процент)
    change_formula = db.Column(db.String(500))  # Формула изменения (если type='formula')

    # Статус
    # 'draft' - черновик, можно редактировать
    # 'pending_review' - ожидает подтверждения (есть опасные изменения)
    # 'confirmed' - подтверждено, готово к применению
    # 'applying' - применяется к WB
    # 'applied' - успешно применено
    # 'partially_applied' - частично применено (были ошибки)
    # 'failed' - ошибка применения
    # 'reverted' - откачено
    # 'cancelled' - отменено пользователем
    status = db.Column(db.String(30), default='draft', nullable=False, index=True)

    # Классификация безопасности (агрегированная)
    has_safe_changes = db.Column(db.Boolean, default=False)
    has_warning_changes = db.Column(db.Boolean, default=False)
    has_dangerous_changes = db.Column(db.Boolean, default=False)

    # Статистика
    total_items = db.Column(db.Integer, default=0)
    safe_count = db.Column(db.Integer, default=0)
    warning_count = db.Column(db.Integer, default=0)
    dangerous_count = db.Column(db.Integer, default=0)
    applied_count = db.Column(db.Integer, default=0)
    failed_count = db.Column(db.Integer, default=0)

    # Подтверждение
    confirmed_at = db.Column(db.DateTime)
    confirmed_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    confirmation_comment = db.Column(db.Text)  # Комментарий при подтверждении

    # Применение
    applied_at = db.Column(db.DateTime)
    wb_task_id = db.Column(db.String(100))  # ID задачи в WB API
    apply_errors = db.Column(db.JSON)  # Ошибки применения

    # Откат
    reverted = db.Column(db.Boolean, default=False)
    reverted_at = db.Column(db.DateTime)
    reverted_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    revert_batch_id = db.Column(db.Integer, db.ForeignKey('price_change_batches.id'))

    # Метаданные
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связи
    items = db.relationship('PriceChangeItem', backref='batch', lazy='dynamic', cascade='all, delete-orphan')
    confirmed_by = db.relationship('User', foreign_keys=[confirmed_by_user_id], backref='confirmed_price_batches')
    reverted_by = db.relationship('User', foreign_keys=[reverted_by_user_id], backref='reverted_price_batches')
    revert_batch = db.relationship('PriceChangeBatch', remote_side=[id], foreign_keys=[revert_batch_id])

    # Индексы
    __table_args__ = (
        db.Index('idx_price_batch_seller_status', 'seller_id', 'status'),
        db.Index('idx_price_batch_seller_created', 'seller_id', 'created_at'),
    )

    def __repr__(self) -> str:
        return f'<PriceChangeBatch id={self.id} status={self.status} items={self.total_items}>'

    def can_confirm(self) -> bool:
        """Можно ли подтвердить этот батч"""
        return self.status == 'pending_review'

    def can_apply(self) -> bool:
        """Можно ли применить этот батч"""
        return self.status in ('draft', 'confirmed') and self.total_items > 0

    def can_revert(self) -> bool:
        """Можно ли откатить этот батч"""
        return self.status in ('applied', 'partially_applied') and not self.reverted

    def can_cancel(self) -> bool:
        """Можно ли отменить этот батч"""
        return self.status in ('draft', 'pending_review', 'confirmed')

    def get_safety_level(self) -> str:
        """Получить общий уровень безопасности батча"""
        if self.has_dangerous_changes:
            return 'dangerous'
        elif self.has_warning_changes:
            return 'warning'
        else:
            return 'safe'

    def to_dict(self, include_items: bool = False) -> dict:
        """Конвертировать в словарь для JSON"""
        result = {
            'id': self.id,
            'seller_id': self.seller_id,
            'name': self.name,
            'description': self.description,
            'change_type': self.change_type,
            'change_value': self.change_value,
            'status': self.status,
            'safety_level': self.get_safety_level(),
            'has_dangerous_changes': self.has_dangerous_changes,
            'has_warning_changes': self.has_warning_changes,
            'total_items': self.total_items,
            'safe_count': self.safe_count,
            'warning_count': self.warning_count,
            'dangerous_count': self.dangerous_count,
            'applied_count': self.applied_count,
            'failed_count': self.failed_count,
            'confirmed_at': self.confirmed_at.isoformat() if self.confirmed_at else None,
            'applied_at': self.applied_at.isoformat() if self.applied_at else None,
            'reverted': self.reverted,
            'reverted_at': self.reverted_at.isoformat() if self.reverted_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'can_confirm': self.can_confirm(),
            'can_apply': self.can_apply(),
            'can_revert': self.can_revert(),
            'can_cancel': self.can_cancel()
        }

        if include_items:
            result['items'] = [item.to_dict() for item in self.items.all()]

        return result


class PriceChangeItem(db.Model):
    """Отдельный элемент изменения цены в батче"""
    __tablename__ = 'price_change_items'

    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey('price_change_batches.id', ondelete='CASCADE'), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id', ondelete='CASCADE'), nullable=False, index=True)

    # Идентификация товара (для WB API)
    nm_id = db.Column(db.BigInteger, nullable=False, index=True)
    vendor_code = db.Column(db.String(100))
    product_title = db.Column(db.String(500))

    # Текущие значения (до изменения)
    old_price = db.Column(db.Numeric(10, 2))
    old_discount = db.Column(db.Integer)  # Скидка в процентах
    old_discount_price = db.Column(db.Numeric(10, 2))  # Цена со скидкой

    # Новые значения (после изменения)
    new_price = db.Column(db.Numeric(10, 2))
    new_discount = db.Column(db.Integer)
    new_discount_price = db.Column(db.Numeric(10, 2))

    # Расчетные метрики
    price_change_amount = db.Column(db.Numeric(10, 2))  # Абсолютное изменение
    price_change_percent = db.Column(db.Float)  # Изменение в процентах

    # Классификация безопасности
    # 'safe' - безопасное изменение (зеленый)
    # 'warning' - предупреждение (желтый)
    # 'dangerous' - опасное изменение (красный)
    safety_level = db.Column(db.String(20), default='safe', nullable=False, index=True)

    # Статус элемента
    # 'pending' - ожидает применения
    # 'applied' - успешно применено
    # 'failed' - ошибка применения
    # 'skipped' - пропущено
    # 'reverted' - откачено
    status = db.Column(db.String(20), default='pending', nullable=False)
    error_message = db.Column(db.Text)  # Сообщение об ошибке

    # Результат от WB
    wb_applied_at = db.Column(db.DateTime)
    wb_status = db.Column(db.String(50))  # Статус от WB API

    # Метаданные
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связь с товаром
    product = db.relationship('Product', backref=db.backref('price_change_items', lazy='dynamic'))

    # Индексы
    __table_args__ = (
        db.Index('idx_price_item_batch_safety', 'batch_id', 'safety_level'),
        db.Index('idx_price_item_batch_status', 'batch_id', 'status'),
    )

    def __repr__(self) -> str:
        return f'<PriceChangeItem nm_id={self.nm_id} {self.old_price}->{self.new_price} ({self.safety_level})>'

    def calculate_change(self):
        """Рассчитать метрики изменения"""
        if self.old_price and self.new_price:
            self.price_change_amount = float(self.new_price) - float(self.old_price)
            if float(self.old_price) > 0:
                self.price_change_percent = (self.price_change_amount / float(self.old_price)) * 100
            else:
                self.price_change_percent = 100 if self.price_change_amount > 0 else 0
        else:
            self.price_change_amount = 0
            self.price_change_percent = 0

    def to_dict(self) -> dict:
        """Конвертировать в словарь для JSON"""
        return {
            'id': self.id,
            'batch_id': self.batch_id,
            'product_id': self.product_id,
            'nm_id': self.nm_id,
            'vendor_code': self.vendor_code,
            'product_title': self.product_title,
            'old_price': float(self.old_price) if self.old_price else None,
            'old_discount': self.old_discount,
            'old_discount_price': float(self.old_discount_price) if self.old_discount_price else None,
            'new_price': float(self.new_price) if self.new_price else None,
            'new_discount': self.new_discount,
            'new_discount_price': float(self.new_discount_price) if self.new_discount_price else None,
            'price_change_amount': float(self.price_change_amount) if self.price_change_amount else None,
            'price_change_percent': self.price_change_percent,
            'safety_level': self.safety_level,
            'status': self.status,
            'error_message': self.error_message,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class SystemSettings(db.Model):
    """Глобальные настройки системы"""
    __tablename__ = 'system_settings'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False, index=True)
    value = db.Column(db.Text)  # Значение в JSON
    value_type = db.Column(db.String(20), nullable=False)  # string, int, bool, json
    description = db.Column(db.Text)  # Описание настройки
    updated_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Связи
    updated_by = db.relationship('User', backref=db.backref('system_settings_updates', lazy='dynamic'))

    def __repr__(self) -> str:
        return f'<SystemSettings key={self.key}>'

    def get_value(self):
        """Получить значение с правильным типом"""
        if not self.value:
            return None

        if self.value_type == 'bool':
            return self.value.lower() in ['true', '1', 'yes']
        elif self.value_type == 'int':
            return int(self.value)
        elif self.value_type == 'json':
            import json
            return json.loads(self.value)
        else:
            return self.value

    def set_value(self, value):
        """Установить значение с правильным типом"""
        if self.value_type == 'json':
            import json
            self.value = json.dumps(value, ensure_ascii=False)
        else:
            self.value = str(value)

    def to_dict(self) -> dict:
        """Конвертировать в словарь для JSON"""
        return {
            'id': self.id,
            'key': self.key,
            'value': self.get_value(),
            'value_type': self.value_type,
            'description': self.description,
            'updated_by_user_id': self.updated_by_user_id,
            'updated_by_username': self.updated_by.username if self.updated_by else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class AIHistory(db.Model):
    """История AI-действий для товаров (расширенная версия для полного логирования)"""
    __tablename__ = 'ai_history'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    imported_product_id = db.Column(db.Integer, db.ForeignKey('imported_products.id'), nullable=True, index=True)

    # Тип действия
    action_type = db.Column(db.String(50), nullable=False, index=True)  # 'seo_title', 'keywords', 'bullets', 'rich_content', 'analysis', 'full_optimize', 'description', 'category', 'sizes'

    # AI провайдер и модель
    ai_provider = db.Column(db.String(50))  # 'cloudru', 'openai', 'custom'
    ai_model = db.Column(db.String(100))  # Использованная модель

    # Промпты (полные тексты для воспроизведения)
    system_prompt = db.Column(db.Text)  # Системный промпт (инструкция)
    user_prompt = db.Column(db.Text)  # Пользовательский промпт

    # Входные данные (для воспроизведения) - JSON
    input_data = db.Column(db.Text)  # JSON с входными данными

    # Результат
    result_data = db.Column(db.Text)  # JSON с результатом
    raw_response = db.Column(db.Text)  # Сырой ответ от AI (до парсинга)
    success = db.Column(db.Boolean, default=True)
    error_message = db.Column(db.Text)

    # Статистика
    tokens_used = db.Column(db.Integer, default=0)  # Использовано токенов (всего)
    tokens_prompt = db.Column(db.Integer, default=0)  # Токенов в промпте
    tokens_completion = db.Column(db.Integer, default=0)  # Токенов в ответе
    response_time_ms = db.Column(db.Integer, default=0)  # Время ответа в мс

    # Источник запроса
    source_module = db.Column(db.String(100))  # Модуль откуда пришел запрос (auto_import, product_edit, etc.)

    # Метаданные
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Связи
    imported_product = db.relationship('ImportedProduct', backref=db.backref('ai_history', lazy='dynamic', order_by='AIHistory.created_at.desc()'))

    # Индексы
    __table_args__ = (
        db.Index('idx_ai_history_seller_action', 'seller_id', 'action_type'),
        db.Index('idx_ai_history_product_created', 'imported_product_id', 'created_at'),
        db.Index('idx_ai_history_created', 'created_at'),
    )


class AgentChangeSnapshot(db.Model):
    """Снимок значений полей до изменения агентом — для отката.

    Каждая запись хранит старые значения полей одного товара,
    которые были изменены конкретной задачей агента.
    """
    __tablename__ = 'agent_change_snapshots'

    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.String(36), db.ForeignKey('agent_tasks.id'), nullable=True, index=True)
    imported_product_id = db.Column(db.Integer, db.ForeignKey('imported_products.id'), nullable=False, index=True)
    agent_id = db.Column(db.String(36), nullable=True)

    # JSON: {"field_name": old_value, ...}
    previous_values = db.Column(db.Text, nullable=False)
    # JSON: {"field_name": new_value, ...}
    new_values = db.Column(db.Text, nullable=False)

    is_rolled_back = db.Column(db.Boolean, default=False)
    rolled_back_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    imported_product = db.relationship('ImportedProduct', backref=db.backref(
        'agent_changes', lazy='dynamic', order_by='AgentChangeSnapshot.created_at.desc()',
        cascade='all, delete-orphan'))

    __table_args__ = (
        db.Index('idx_acs_task_product', 'task_id', 'imported_product_id'),
    )

    def __repr__(self):
        return f'<AgentChangeSnapshot task={self.task_id[:8] if self.task_id else "?"} product={self.imported_product_id}>'


class BlockedCard(db.Model):
    """Заблокированная карточка товара WB (кэш из seller-analytics-api)"""
    __tablename__ = 'blocked_cards'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)

    nm_id = db.Column(db.BigInteger, nullable=False)
    vendor_code = db.Column(db.String(200))
    title = db.Column(db.String(500))
    brand = db.Column(db.String(200))
    reason = db.Column(db.Text)

    # Отслеживание
    first_seen_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)  # False = разблокирована

    __table_args__ = (
        db.Index('idx_blocked_seller_nm', 'seller_id', 'nm_id'),
        db.Index('idx_blocked_seller_active', 'seller_id', 'is_active'),
    )

    def __repr__(self):
        return f'<BlockedCard nm_id={self.nm_id} reason={self.reason[:30] if self.reason else "N/A"}>'


class ShadowedCard(db.Model):
    """Карточка товара WB, скрытая из каталога (кэш из seller-analytics-api)"""
    __tablename__ = 'shadowed_cards'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)

    nm_id = db.Column(db.BigInteger, nullable=False)
    vendor_code = db.Column(db.String(200))
    title = db.Column(db.String(500))
    brand = db.Column(db.String(200))
    nm_rating = db.Column(db.Float)

    # Отслеживание
    first_seen_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)  # False = вернулась в каталог

    __table_args__ = (
        db.Index('idx_shadowed_seller_nm', 'seller_id', 'nm_id'),
        db.Index('idx_shadowed_seller_active', 'seller_id', 'is_active'),
    )

    def __repr__(self):
        return f'<ShadowedCard nm_id={self.nm_id} rating={self.nm_rating}>'


class BlockedCardsSyncSettings(db.Model):
    """Настройки и статус синхронизации заблокированных карточек"""
    __tablename__ = 'blocked_cards_sync_settings'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, unique=True)

    last_sync_at = db.Column(db.DateTime)
    last_sync_status = db.Column(db.String(20))  # 'success', 'error', 'running'
    last_sync_error = db.Column(db.Text)
    blocked_count = db.Column(db.Integer, default=0)
    shadowed_count = db.Column(db.Integer, default=0)

    seller = db.relationship('Seller', backref=db.backref('blocked_cards_sync', uselist=False))

    def __repr__(self) -> str:
        return f'<AIHistory {self.action_type} product_id={self.imported_product_id}>'

    def to_dict(self, include_prompts: bool = False) -> dict:
        """
        Конвертировать в словарь для JSON

        Args:
            include_prompts: Включать ли полные промпты (для детального просмотра)
        """
        import json
        result = {
            'id': self.id,
            'action_type': self.action_type,
            'ai_provider': self.ai_provider,
            'ai_model': self.ai_model,
            'input_data': json.loads(self.input_data) if self.input_data else None,
            'result_data': json.loads(self.result_data) if self.result_data else None,
            'success': self.success,
            'error_message': self.error_message,
            'tokens_used': self.tokens_used,
            'tokens_prompt': self.tokens_prompt,
            'tokens_completion': self.tokens_completion,
            'response_time_ms': self.response_time_ms,
            'source_module': self.source_module,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'imported_product_id': self.imported_product_id
        }
        if include_prompts:
            result['system_prompt'] = self.system_prompt
            result['user_prompt'] = self.user_prompt
            result['raw_response'] = self.raw_response
        return result

    @staticmethod
    def get_action_type_display(action_type: str) -> str:
        """Возвращает человекочитаемое название действия"""
        action_names = {
            'seo_title': 'SEO Заголовок',
            'keywords': 'Ключевые слова',
            'bullets': 'Преимущества',
            'bullet_points': 'Преимущества',
            'rich_content': 'Rich контент',
            'analysis': 'Анализ карточки',
            'full_optimize': 'Полная оптимизация',
            'description': 'Генерация описания',
            'enhance_description': 'Улучшение описания',
            'category': 'Определение категории',
            'sizes': 'Парсинг размеров',
            'dimensions': 'Характеристики'
        }
        return action_names.get(action_type, action_type)


# ============= HELPER FUNCTIONS FOR LOGGING =============

def log_user_activity(user_id: int, action: str, details: str = None, request=None):
    """
    Логировать активность пользователя

    Args:
        user_id: ID пользователя
        action: Действие (например, 'login', 'logout', 'view_products')
        details: Дополнительные детали в виде строки или JSON
        request: Flask request object для получения IP и user agent
    """
    import json

    activity = UserActivity(
        user_id=user_id,
        action=action,
        details=details if isinstance(details, str) else json.dumps(details, ensure_ascii=False) if details else None,
        ip_address=request.remote_addr if request else None,
        user_agent=request.user_agent.string if request and request.user_agent else None
    )
    db.session.add(activity)
    db.session.commit()
    return activity


def log_admin_action(admin_user_id: int, action: str, target_type: str = None,
                     target_id: int = None, details: dict = None, request=None):
    """
    Логировать действие администратора

    Args:
        admin_user_id: ID администратора
        action: Действие (например, 'create_user', 'delete_seller')
        target_type: Тип целевого объекта ('user', 'seller', 'product')
        target_id: ID целевого объекта
        details: Детали действия (dict)
        request: Flask request object для получения IP
    """
    import json

    audit_log = AdminAuditLog(
        admin_user_id=admin_user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=json.dumps(details, ensure_ascii=False) if details else None,
        ip_address=request.remote_addr if request else None
    )
    db.session.add(audit_log)
    db.session.commit()
    return audit_log


# ============= SUPPLIER DATABASE MODELS =============

class Supplier(db.Model):
    """Поставщик товаров (централизованная сущность)"""
    __tablename__ = 'suppliers'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)  # "Sexoptovik", "FixPrice"
    code = db.Column(db.String(50), unique=True, nullable=False, index=True)  # slug: "sexoptovik"
    description = db.Column(db.Text)
    website = db.Column(db.String(500))
    logo_url = db.Column(db.String(500))

    # Источник данных
    csv_source_url = db.Column(db.String(500))  # URL CSV для автоимпорта
    csv_delimiter = db.Column(db.String(5), default=';')
    csv_encoding = db.Column(db.String(20), default='cp1251')
    csv_column_mapping = db.Column(db.JSON, default=None)  # Конфигурируемый маппинг колонок CSV
    csv_has_header = db.Column(db.Boolean, default=False)  # Есть ли заголовок в CSV
    api_endpoint = db.Column(db.String(500))  # Для будущей API-интеграции

    # Источник цен и остатков (отдельный файл)
    price_file_url = db.Column(db.String(500))       # URL CSV цен/остатков
    price_file_inf_url = db.Column(db.String(500))    # URL INF файла (change detection)
    price_file_delimiter = db.Column(db.String(5), default=';')
    price_file_encoding = db.Column(db.String(20), default='cp1251')

    # Статистика синхронизации цен
    last_price_sync_at = db.Column(db.DateTime)
    last_price_sync_status = db.Column(db.String(50))  # success/failed/running
    last_price_sync_error = db.Column(db.Text)
    last_price_file_hash = db.Column(db.String(64))     # MD5 hash INF файла

    # Автосинхронизация
    auto_sync_prices = db.Column(db.Boolean, default=False, nullable=False)
    auto_sync_interval_minutes = db.Column(db.Integer, default=60)

    # Артикулы: настройки формирования vendor_code
    # Regex для извлечения product_id из external_id поставщика.
    # Должен содержать группу (?P<product_id>...).
    # Примеры: r'[A-Za-z]+-(?P<product_id>\d+)' для "0T-00003031" → "00003031"
    external_id_pattern = db.Column(db.String(300))
    # Шаблон артикула по умолчанию для новых подключений продавцов
    default_vendor_code_pattern = db.Column(db.String(200))

    # Авторизация для доступа к ресурсам поставщика (фото и т.д.)
    auth_login = db.Column(db.String(200))
    _auth_password_encrypted = db.Column('auth_password', db.String(500))

    # AI настройки (централизованные для поставщика)
    ai_enabled = db.Column(db.Boolean, default=False, nullable=False)
    ai_provider = db.Column(db.String(50), default='openai')
    _ai_api_key_encrypted = db.Column('ai_api_key', db.String(500))
    ai_api_base_url = db.Column(db.String(500))
    ai_model = db.Column(db.String(100), default='gpt-4o-mini')
    ai_temperature = db.Column(db.Float, default=0.3)
    ai_max_tokens = db.Column(db.Integer, default=2000)
    ai_timeout = db.Column(db.Integer, default=60)
    # Cloud.ru OAuth2
    ai_client_id = db.Column(db.String(500))
    ai_client_secret = db.Column(db.String(500))
    # Кастомные AI инструкции
    ai_category_instruction = db.Column(db.Text)
    ai_size_instruction = db.Column(db.Text)
    ai_seo_title_instruction = db.Column(db.Text)
    ai_keywords_instruction = db.Column(db.Text)
    ai_description_instruction = db.Column(db.Text)
    ai_analysis_instruction = db.Column(db.Text)
    ai_parsing_instruction = db.Column(db.Text)  # Кастомная инструкция для AI парсинга

    # Файл описаний товаров (отдельный CSV с описаниями)
    description_file_url = db.Column(db.String(500))       # URL CSV с описаниями
    description_file_delimiter = db.Column(db.String(5), default=';')
    description_file_encoding = db.Column(db.String(20), default='cp1251')
    last_description_sync_at = db.Column(db.DateTime)
    last_description_sync_status = db.Column(db.String(50))

    # Прокси для AI запросов (OpenRouter, OpenAI и др. зарубежные провайдеры)
    ai_proxy_enabled = db.Column(db.Boolean, default=False, nullable=False)

    # Настройки генерации изображений для инфографики
    image_gen_enabled = db.Column(db.Boolean, default=False, nullable=False)
    image_gen_provider = db.Column(db.String(50), default='openrouter')  # openrouter, fluxapi, openai_dalle, etc.

    # Настройки обработки фото
    resize_images = db.Column(db.Boolean, default=True, nullable=False)
    image_target_size = db.Column(db.Integer, default=1200)
    image_background_color = db.Column(db.String(20), default='white')

    # Наценка по умолчанию (%)
    default_markup_percent = db.Column(db.Float)

    # Статус
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    # Статистика
    total_products = db.Column(db.Integer, default=0)
    last_sync_at = db.Column(db.DateTime)
    last_sync_status = db.Column(db.String(50))
    last_sync_error = db.Column(db.Text)
    last_sync_duration = db.Column(db.Float)

    # Метаданные
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'))

    # Связи
    products = db.relationship('SupplierProduct', backref='supplier', lazy='dynamic', cascade='all, delete-orphan')
    seller_connections = db.relationship('SellerSupplier', backref='supplier', lazy='dynamic', cascade='all, delete-orphan')
    created_by = db.relationship('User', backref='created_suppliers', foreign_keys=[created_by_user_id])

    @property
    def auth_password(self) -> Optional[str]:
        """Расшифровать пароль"""
        if not self._auth_password_encrypted:
            return None
        encryption_key = os.environ.get('ENCRYPTION_KEY')
        if not encryption_key:
            return self._auth_password_encrypted
        try:
            f = Fernet(encryption_key.encode())
            return f.decrypt(self._auth_password_encrypted.encode()).decode()
        except Exception:
            return self._auth_password_encrypted

    @auth_password.setter
    def auth_password(self, value: Optional[str]) -> None:
        """Зашифровать пароль"""
        if value is None:
            self._auth_password_encrypted = None
            return
        encryption_key = os.environ.get('ENCRYPTION_KEY')
        if not encryption_key:
            self._auth_password_encrypted = value
            return
        try:
            f = Fernet(encryption_key.encode())
            self._auth_password_encrypted = f.encrypt(value.encode()).decode()
        except Exception:
            self._auth_password_encrypted = value

    @property
    def ai_api_key(self) -> Optional[str]:
        """Расшифровать AI API ключ"""
        if not self._ai_api_key_encrypted:
            return None
        encryption_key = os.environ.get('ENCRYPTION_KEY')
        if not encryption_key:
            return self._ai_api_key_encrypted
        try:
            f = Fernet(encryption_key.encode())
            return f.decrypt(self._ai_api_key_encrypted.encode()).decode()
        except Exception:
            return self._ai_api_key_encrypted

    @ai_api_key.setter
    def ai_api_key(self, value: Optional[str]) -> None:
        """Зашифровать AI API ключ"""
        if value is None:
            self._ai_api_key_encrypted = None
            return
        encryption_key = os.environ.get('ENCRYPTION_KEY')
        if not encryption_key:
            self._ai_api_key_encrypted = value
            return
        try:
            f = Fernet(encryption_key.encode())
            self._ai_api_key_encrypted = f.encrypt(value.encode()).decode()
        except Exception:
            self._ai_api_key_encrypted = value

    def get_connected_sellers_count(self) -> int:
        """Количество подключённых продавцов"""
        return self.seller_connections.filter_by(is_active=True).count()

    def __repr__(self) -> str:
        return f'<Supplier {self.code} ({self.name})>'

    def to_dict(self, include_sensitive: bool = False) -> dict:
        """Конвертировать в словарь для JSON"""
        data = {
            'id': self.id,
            'name': self.name,
            'code': self.code,
            'description': self.description,
            'website': self.website,
            'logo_url': self.logo_url,
            'csv_source_url': self.csv_source_url,
            'is_active': self.is_active,
            'total_products': self.total_products,
            'last_sync_at': self.last_sync_at.isoformat() if self.last_sync_at else None,
            'last_sync_status': self.last_sync_status,
            'price_file_url': self.price_file_url,
            'last_price_sync_at': self.last_price_sync_at.isoformat() if self.last_price_sync_at else None,
            'last_price_sync_status': self.last_price_sync_status,
            'auto_sync_prices': self.auto_sync_prices,
            'ai_enabled': self.ai_enabled,
            'ai_provider': self.ai_provider,
            'ai_model': self.ai_model,
            'external_id_pattern': self.external_id_pattern,
            'default_vendor_code_pattern': self.default_vendor_code_pattern,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_sensitive:
            data['auth_login'] = self.auth_login
            data['ai_api_base_url'] = self.ai_api_base_url
        return data


class SupplierProduct(db.Model):
    """Товар в централизованной базе поставщика"""
    __tablename__ = 'supplier_products'

    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False, index=True)

    # Идентификация
    external_id = db.Column(db.String(200), index=True)  # ID из каталога поставщика
    vendor_code = db.Column(db.String(200))  # Артикул поставщика
    barcode = db.Column(db.String(200))

    # Основные данные (нормализованные)
    title = db.Column(db.String(500))
    description = db.Column(db.Text)
    brand = db.Column(db.String(200))
    resolved_brand_id = db.Column(db.Integer, db.ForeignKey('brands.id'), nullable=True, index=True)
    category = db.Column(db.String(200))  # Категория поставщика
    all_categories = db.Column(db.Text)  # Все категории из цепочки (JSON)

    # WB маппинг
    wb_category_name = db.Column(db.String(200))
    wb_subject_id = db.Column(db.Integer)
    wb_subject_name = db.Column(db.String(200))
    category_confidence = db.Column(db.Float, default=0.0)

    # Цена и остатки
    supplier_price = db.Column(db.Float)  # Закупочная цена
    supplier_quantity = db.Column(db.Integer)  # Остаток у поставщика
    currency = db.Column(db.String(10), default='RUB')
    recommended_retail_price = db.Column(db.Float)   # РРЦ от поставщика
    supplier_status = db.Column(db.String(50))        # Статус у поставщика (in_stock/out_of_stock)
    additional_vendor_code = db.Column(db.String(200)) # Доп. артикул

    # Синхронизация цен/остатков
    last_price_sync_at = db.Column(db.DateTime)       # Когда последний раз обновилась цена
    price_changed_at = db.Column(db.DateTime)          # Когда цена реально изменилась
    previous_price = db.Column(db.Float)               # Предыдущая цена (для трекинга)

    # Характеристики (нормализованные JSON)
    characteristics_json = db.Column(db.Text)  # [{name, value}, ...]
    sizes_json = db.Column(db.Text)  # Размеры
    colors_json = db.Column(db.Text)  # Цвета
    materials_json = db.Column(db.Text)  # Материалы
    dimensions_json = db.Column(db.Text)  # Габариты (д/ш/в/вес)
    gender = db.Column(db.String(50))
    country = db.Column(db.String(100))
    season = db.Column(db.String(50))
    age_group = db.Column(db.String(50))

    # Медиа
    photo_urls_json = db.Column(db.Text)  # Оригинальные URL фото от поставщика
    processed_photos_json = db.Column(db.Text)  # Обработанные фото (локальные пути)
    video_url = db.Column(db.String(500))

    # AI-обогащённые данные
    ai_seo_title = db.Column(db.String(500))
    ai_description = db.Column(db.Text)
    ai_keywords_json = db.Column(db.Text)
    ai_bullets_json = db.Column(db.Text)
    ai_rich_content_json = db.Column(db.Text)
    ai_analysis_json = db.Column(db.Text)
    ai_validated = db.Column(db.Boolean, default=False)
    ai_validated_at = db.Column(db.DateTime)
    ai_validation_score = db.Column(db.Float)  # Оценка качества 0-100
    content_hash = db.Column(db.String(64))

    # AI полный парсинг — результаты комплексного AI-извлечения
    ai_parsed_data_json = db.Column(db.Text)  # Полный JSON со всеми извлечёнными характеристиками
    ai_parsed_at = db.Column(db.DateTime)     # Когда был выполнен парсинг
    ai_model_used = db.Column(db.String(100)) # Какой AI моделью спарсено (e.g. "openai/gpt-oss-120b")
    ai_marketplace_json = db.Column(db.Text)  # Данные форматированные для маркетплейса (WB)
    description_source = db.Column(db.String(50))  # csv/ai/manual — откуда описание

    # Оригинальные данные для отката
    original_data_json = db.Column(db.Text)

    # Качество парсинга
    parsing_confidence = db.Column(db.Float)  # 0.0-1.0 оценка качества парсинга
    normalization_applied = db.Column(db.Boolean, default=False)  # Была ли применена нормализация

    # Статус: draft → validated → ready → archived
    status = db.Column(db.String(50), default='draft', index=True)
    validation_errors_json = db.Column(db.Text)

    # Marketplace Integration
    marketplace_fields_json = db.Column(db.Text)
    marketplace_validation_status = db.Column(db.String(50))
    marketplace_fill_pct = db.Column(db.Float)

    # Метаданные
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Составные индексы
    __table_args__ = (
        db.UniqueConstraint('supplier_id', 'external_id', name='uq_supplier_external_id'),
        db.Index('idx_supplier_product_status', 'supplier_id', 'status'),
        db.Index('idx_supplier_product_category', 'supplier_id', 'wb_subject_id'),
        db.Index('idx_supplier_product_brand', 'supplier_id', 'brand'),
    )

    def __repr__(self) -> str:
        return f'<SupplierProduct {self.external_id} ({self.title[:30] if self.title else "N/A"})>'

    def get_photos(self) -> list:
        """Получить список URL фотографий"""
        if not self.photo_urls_json:
            return []
        try:
            import json
            return json.loads(self.photo_urls_json)
        except Exception:
            return []

    def get_processed_photos(self) -> list:
        """Получить обработанные фотографии"""
        if not self.processed_photos_json:
            return []
        try:
            import json
            return json.loads(self.processed_photos_json)
        except Exception:
            return []

    def get_characteristics(self) -> list:
        """Получить характеристики"""
        if not self.characteristics_json:
            return []
        try:
            import json
            return json.loads(self.characteristics_json)
        except Exception:
            return []

    def get_sizes(self) -> list:
        """Получить размеры"""
        if not self.sizes_json:
            return []
        try:
            import json
            return json.loads(self.sizes_json)
        except Exception:
            return []

    def get_validation_errors(self) -> list:
        """Получить ошибки валидации"""
        if not self.validation_errors_json:
            return []
        try:
            import json
            return json.loads(self.validation_errors_json)
        except Exception:
            return []

    def to_dict(self, include_ai: bool = False) -> dict:
        """Конвертировать в словарь для JSON"""
        import json
        data = {
            'id': self.id,
            'supplier_id': self.supplier_id,
            'external_id': self.external_id,
            'vendor_code': self.vendor_code,
            'barcode': self.barcode,
            'title': self.title,
            'description': self.description,
            'brand': self.brand,
            'category': self.category,
            'wb_category_name': self.wb_category_name,
            'wb_subject_id': self.wb_subject_id,
            'wb_subject_name': self.wb_subject_name,
            'category_confidence': self.category_confidence,
            'supplier_price': self.supplier_price,
            'supplier_quantity': self.supplier_quantity,
            'gender': self.gender,
            'country': self.country,
            'season': self.season,
            'photo_urls': self.get_photos(),
            'processed_photos': self.get_processed_photos(),
            'status': self.status,
            'ai_validated': self.ai_validated,
            'ai_validation_score': self.ai_validation_score,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'marketplace_validation_status': self.marketplace_validation_status,
            'marketplace_fill_pct': self.marketplace_fill_pct,
            'marketplace_fields': self.get_marketplace_fields(),
        }
        if include_ai:
            data['ai_seo_title'] = self.ai_seo_title
            data['ai_description'] = self.ai_description
            data['ai_keywords'] = json.loads(self.ai_keywords_json) if self.ai_keywords_json else []
            data['ai_bullets'] = json.loads(self.ai_bullets_json) if self.ai_bullets_json else []
            data['ai_analysis'] = json.loads(self.ai_analysis_json) if self.ai_analysis_json else None
            data['ai_validated_at'] = self.ai_validated_at.isoformat() if self.ai_validated_at else None
            data['validation_errors'] = self.get_validation_errors()
            data['ai_parsed_data'] = self.get_ai_parsed_data()
            data['ai_parsed_at'] = self.ai_parsed_at.isoformat() if self.ai_parsed_at else None
            data['ai_marketplace_data'] = self.get_ai_marketplace_data()
            data['description_source'] = self.description_source
        return data

    def get_ai_parsed_data(self) -> dict:
        """Получить AI-извлечённые данные"""
        if not self.ai_parsed_data_json:
            return {}
        try:
            import json
            return json.loads(self.ai_parsed_data_json)
        except Exception:
            return {}

    def get_ai_marketplace_data(self) -> dict:
        """Получить данные в формате маркетплейса"""
        if not self.ai_marketplace_json:
            return {}
        try:
            import json
            return json.loads(self.ai_marketplace_json)
        except Exception:
            return {}

    def get_all_data_for_parsing(self) -> dict:
        """Собрать все данные товара для AI парсинга"""
        import json
        data = {
            'title': self.title or '',
            'description': self.description or '',
            'brand': self.brand or '',
            'category': self.category or '',
            'wb_category': self.wb_category_name or '',
            'gender': self.gender or '',
            'country': self.country or '',
            'season': self.season or '',
            'age_group': self.age_group or '',
            'vendor_code': self.vendor_code or '',
            'barcode': self.barcode or '',
            'price': self.supplier_price,
        }
        # JSON поля
        try:
            data['colors'] = json.loads(self.colors_json) if self.colors_json else []
        except Exception:
            data['colors'] = []
        try:
            data['materials'] = json.loads(self.materials_json) if self.materials_json else []
        except Exception:
            data['materials'] = []
        try:
            data['sizes'] = json.loads(self.sizes_json) if self.sizes_json else {}
        except Exception:
            data['sizes'] = {}
        try:
            data['dimensions'] = json.loads(self.dimensions_json) if self.dimensions_json else {}
        except Exception:
            data['dimensions'] = {}
        try:
            data['characteristics'] = json.loads(self.characteristics_json) if self.characteristics_json else []
        except Exception:
            data['characteristics'] = []
        try:
            data['original_data'] = json.loads(self.original_data_json) if self.original_data_json else {}
        except Exception:
            data['original_data'] = {}
        data['photos_count'] = len(self.get_photos())
        data['ai_seo_title'] = self.ai_seo_title or ''
        data['ai_description'] = self.ai_description or ''
        return data

    def get_original_data(self) -> dict:
        """Получить оригинальные данные товара (для AI парсинга)"""
        if not self.original_data_json:
            return {}
        try:
            import json
            return json.loads(self.original_data_json)
        except Exception:
            return {}

    def get_marketplace_fields(self) -> dict:
        """Получить валидированные поля маркетплейса"""
        if not self.marketplace_fields_json:
            return {}
        try:
            import json
            return json.loads(self.marketplace_fields_json)
        except Exception:
            return {}


class SellerSupplier(db.Model):
    """Связь продавца с поставщиком (M2M)"""
    __tablename__ = 'seller_suppliers'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False, index=True)

    # Настройки продавца для этого поставщика
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    supplier_code = db.Column(db.String(50))  # Код продавца для артикулов
    vendor_code_pattern = db.Column(db.String(200), default='id-{product_id}-{supplier_code}')

    # Ценообразование
    custom_markup_percent = db.Column(db.Float)  # Переопределение наценки

    # Статистика
    products_imported = db.Column(db.Integer, default=0)
    last_import_at = db.Column(db.DateTime)

    # Метаданные
    connected_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связи
    seller = db.relationship('Seller', backref=db.backref('supplier_connections', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('seller_id', 'supplier_id', name='uq_seller_supplier'),
        db.Index('idx_seller_supplier_active', 'seller_id', 'is_active'),
    )

    def __repr__(self) -> str:
        return f'<SellerSupplier seller={self.seller_id} supplier={self.supplier_id}>'

    def to_dict(self) -> dict:
        """Конвертировать в словарь для JSON"""
        return {
            'id': self.id,
            'seller_id': self.seller_id,
            'supplier_id': self.supplier_id,
            'is_active': self.is_active,
            'supplier_code': self.supplier_code,
            'vendor_code_pattern': self.vendor_code_pattern,
            'custom_markup_percent': self.custom_markup_percent,
            'products_imported': self.products_imported,
            'last_import_at': self.last_import_at.isoformat() if self.last_import_at else None,
            'connected_at': self.connected_at.isoformat() if self.connected_at else None,
        }


class EnrichmentJob(db.Model):
    """Задача массового обогащения карточек данными поставщика"""
    __tablename__ = 'enrichment_jobs'

    id            = db.Column(db.String(36), primary_key=True)   # UUID
    seller_id     = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False)
    status        = db.Column(db.String(20), default='pending')  # pending/running/done/failed
    total         = db.Column(db.Integer, default=0)
    processed     = db.Column(db.Integer, default=0)
    succeeded     = db.Column(db.Integer, default=0)
    failed        = db.Column(db.Integer, default=0)
    skipped       = db.Column(db.Integer, default=0)
    fields_config  = db.Column(db.Text)    # JSON список полей: ['title','photos',...]
    photo_strategy = db.Column(db.String(20), default='replace')  # replace/append/only_if_empty
    results       = db.Column(db.Text)    # JSON [{product_id, nm_id, status, error}]
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    seller = db.relationship('Seller', foreign_keys=[seller_id])


class AIParseJob(db.Model):
    """Задача фонового AI парсинга товаров поставщика"""
    __tablename__ = 'ai_parse_jobs'

    id             = db.Column(db.String(36), primary_key=True)   # UUID
    supplier_id    = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False)
    admin_user_id  = db.Column(db.Integer)                        # Кто запустил
    job_type       = db.Column(db.String(30), default='parse')    # parse / parse_single / sync_descriptions
    status         = db.Column(db.String(20), default='pending')  # pending / running / done / failed / cancelled
    total          = db.Column(db.Integer, default=0)
    processed      = db.Column(db.Integer, default=0)
    succeeded      = db.Column(db.Integer, default=0)
    failed         = db.Column(db.Integer, default=0)
    current_product_title = db.Column(db.String(200))             # Название текущего обрабатываемого товара
    model_used     = db.Column(db.String(100))                    # Название AI модели (gpt-4o, claude-sonnet и т.д.)
    results        = db.Column(db.Text)                           # JSON [{product_id, title, status, fill_pct, error}]
    error_message  = db.Column(db.Text)                           # Общая ошибка если failed
    heartbeat_at   = db.Column(db.DateTime)                        # Последний heartbeat от воркера
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at     = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Таймаут: если heartbeat_at старше STALE_TIMEOUT_SECONDS — задача считается зависшей
    # 5 минут: с фоновым heartbeat-потоком (обновляется каждые 30с) это сработает
    # только при реальном зависании, а не при долгих AI-запросах (DeepSeek, two-pass)
    STALE_TIMEOUT_SECONDS = 300

    supplier = db.relationship('Supplier', foreign_keys=[supplier_id])

    @property
    def is_stale(self):
        """Проверяет, зависла ли задача (нет heartbeat дольше таймаута)."""
        if self.status not in ('pending', 'running'):
            return False
        if not self.heartbeat_at:
            # Если heartbeat ни разу не обновлялся — смотрим на created_at
            check_time = self.created_at
        else:
            check_time = self.heartbeat_at
        if not check_time:
            return False
        return (datetime.utcnow() - check_time).total_seconds() > self.STALE_TIMEOUT_SECONDS


# ============= MARKETPLACE INTEGRATION MODELS =============

class Marketplace(db.Model):
    """Маркетплейс (WB, Ozon, Yandex Market и т.д.)"""
    __tablename__ = 'marketplaces'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)          # "Wildberries"
    code = db.Column(db.String(50), unique=True, nullable=False)  # "wb"
    logo_url = db.Column(db.String(500))
    is_active = db.Column(db.Boolean, default=True)

    # API configuration
    api_base_url = db.Column(db.String(500))                  # base URL
    _api_key_encrypted = db.Column('api_key', db.String(500)) # encrypted key
    api_version = db.Column(db.String(20), default='v2')      # v2 / v3

    # Category sync state
    categories_synced_at = db.Column(db.DateTime)
    categories_sync_status = db.Column(db.String(50))         # success/failed/running
    total_categories = db.Column(db.Integer, default=0)
    total_characteristics = db.Column(db.Integer, default=0)

    # Directories sync
    directories_synced_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def api_key(self) -> Optional[str]:
        """Расшифровать API ключ"""
        if not self._api_key_encrypted:
            return None
        encryption_key = os.environ.get('ENCRYPTION_KEY')
        if not encryption_key:
            return self._api_key_encrypted
        try:
            f = Fernet(encryption_key.encode())
            return f.decrypt(self._api_key_encrypted.encode()).decode()
        except Exception:
            return self._api_key_encrypted

    @api_key.setter
    def api_key(self, value: Optional[str]) -> None:
        """Зашифровать API ключ"""
        if value is None:
            self._api_key_encrypted = None
            return
        encryption_key = os.environ.get('ENCRYPTION_KEY')
        if not encryption_key:
            self._api_key_encrypted = value
            return
        try:
            f = Fernet(encryption_key.encode())
            self._api_key_encrypted = f.encrypt(value.encode()).decode()
        except Exception:
            self._api_key_encrypted = value

    def __repr__(self) -> str:
        return f'<Marketplace {self.code} ({self.name})>'


class MarketplaceCategory(db.Model):
    """Категория маркетплейса (subject / предмет)"""
    __tablename__ = 'marketplace_categories'

    id = db.Column(db.Integer, primary_key=True)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)

    # From WB API: /content/v2/object/all
    subject_id = db.Column(db.Integer, nullable=False)         # subjectID from API
    subject_name = db.Column(db.String(300))                   # "Анальные пробки"
    parent_id = db.Column(db.Integer)                          # parentID
    parent_name = db.Column(db.String(300))                    # "Товары для взрослых"

    # Hierarchy management
    is_enabled = db.Column(db.Boolean, default=False)          # Admin toggle
    is_leaf = db.Column(db.Boolean, default=True)

    # Characteristics cache state
    characteristics_synced_at = db.Column(db.DateTime)
    characteristics_count = db.Column(db.Integer, default=0)
    required_count = db.Column(db.Integer, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    marketplace = db.relationship('Marketplace', backref=db.backref('categories', lazy='dynamic', cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint('marketplace_id', 'subject_id', name='uq_mp_category'),
        db.Index('idx_mp_category_parent', 'marketplace_id', 'parent_id'),
        db.Index('idx_mp_category_enabled', 'marketplace_id', 'is_enabled'),
    )

    def __repr__(self) -> str:
        return f'<MarketplaceCategory {self.subject_id} ({self.subject_name})>'


class MarketplaceCategoryCharacteristic(db.Model):
    """Характеристика категории маркетплейса"""
    __tablename__ = 'marketplace_category_characteristics'

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey('marketplace_categories.id'), nullable=False)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)

    # From WB API: /content/v2/object/charcs/{subjectId}
    charc_id = db.Column(db.Integer, nullable=False)           # charcID
    name = db.Column(db.String(300), nullable=False)           # "Длина"
    charc_type = db.Column(db.Integer, nullable=False)         # 0=unused, 1=string[], 4=number
    required = db.Column(db.Boolean, default=False)
    unit_name = db.Column(db.String(50))                       # "см", "г", etc
    max_count = db.Column(db.Integer, default=0)               # Max values (0=unlimited)
    popular = db.Column(db.Boolean, default=False)

    # Dictionary of allowed values (JSON array)
    dictionary_json = db.Column(db.Text)                       # [{"value":"Черный"},...]

    # AI parsing instruction — auto-generated or admin-customized
    ai_instruction = db.Column(db.Text)                        # Per-field AI instruction
    ai_example_value = db.Column(db.String(500))               # Example for AI prompt

    # Admin customization
    is_enabled = db.Column(db.Boolean, default=True)           # Include in AI parsing
    display_order = db.Column(db.Integer, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    category = db.relationship('MarketplaceCategory', backref=db.backref('characteristics', lazy='dynamic', cascade='all, delete-orphan'))
    # NOTE: marketplace can be accessed via self.category.marketplace

    __table_args__ = (
        db.UniqueConstraint('category_id', 'charc_id', name='uq_category_charc'),
        db.Index('idx_mp_charc_required', 'category_id', 'required'),
    )

    @property
    def type_label(self) -> str:
        """Human-readable type label"""
        if self.charc_type == 4:
            return 'Число'
        elif self.charc_type == 1:
            if self.max_count == 1:
                return 'Строка'
            return f'Строка[] (макс. {self.max_count})' if self.max_count > 0 else 'Строка[]'
        elif self.charc_type == 0:
            return 'Не используется'
        return f'Тип {self.charc_type}'

    def __repr__(self) -> str:
        t = 'num' if self.charc_type == 4 else 'str'
        r = '*' if self.required else ''
        return f'<Charc {self.charc_id} "{self.name}" ({t}{r})>'


class MarketplaceDirectory(db.Model):
    """Справочник маркетплейса (цвета, страны, пол, сезоны и т.д.)"""
    __tablename__ = 'marketplace_directories'

    id = db.Column(db.Integer, primary_key=True)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)
    directory_type = db.Column(db.String(50), nullable=False)  # colors/countries/kinds/seasons/vat/tnved
    data_json = db.Column(db.Text, nullable=False)             # Cached response from API
    synced_at = db.Column(db.DateTime)
    items_count = db.Column(db.Integer, default=0)

    marketplace = db.relationship('Marketplace', backref=db.backref('directories', lazy='dynamic', cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint('marketplace_id', 'directory_type', name='uq_mp_directory'),
    )

    def __repr__(self) -> str:
        return f'<MarketplaceDirectory {self.directory_type} ({self.items_count} items)>'


class MarketplaceConnection(db.Model):
    """Привязка поставщика к маркетплейсу"""
    __tablename__ = 'marketplace_connections'

    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)

    is_active = db.Column(db.Boolean, default=True)

    # Which categories are enabled for this supplier on this marketplace
    enabled_categories_json = db.Column(db.Text)

    # Auto-mapping settings
    auto_map_categories = db.Column(db.Boolean, default=True)
    default_category_id = db.Column(db.Integer)  # Fallback category

    # Stats
    products_mapped = db.Column(db.Integer, default=0)
    products_validated = db.Column(db.Integer, default=0)
    last_mapping_at = db.Column(db.DateTime)

    connected_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    supplier = db.relationship('Supplier', backref=db.backref('marketplace_connections', lazy='dynamic', cascade='all, delete-orphan'))
    marketplace = db.relationship('Marketplace', backref=db.backref('supplier_connections', lazy='dynamic', cascade='all, delete-orphan'))

    __table_args__ = (
        db.UniqueConstraint('supplier_id', 'marketplace_id', name='uq_supplier_marketplace'),
    )

    def __repr__(self) -> str:
        active = 'active' if self.is_active else 'inactive'
        return f'<MarketplaceConnection supplier={self.supplier_id} mp={self.marketplace_id} ({active})>'


class MarketplaceSyncJob(db.Model):
    """Background job for syncing marketplace data"""
    __tablename__ = 'marketplace_sync_jobs'

    id = db.Column(db.String(36), primary_key=True)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False)
    job_type = db.Column(db.String(30))       # categories/characteristics/directories/validate
    status = db.Column(db.String(20))         # pending/running/done/failed
    total = db.Column(db.Integer, default=0)
    processed = db.Column(db.Integer, default=0)
    error_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    marketplace = db.relationship('Marketplace', backref=db.backref('sync_jobs', lazy='dynamic', cascade='all, delete-orphan'))

    def __repr__(self) -> str:
        return f'<MarketplaceSyncJob {self.id[:8]} {self.job_type} ({self.status})>'


class ParsingLog(db.Model):
    """Лог и метрики парсинга для отслеживания качества."""
    __tablename__ = 'parsing_logs'

    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False, index=True)
    event_type = db.Column(db.String(50), nullable=False)  # sync, ai_parse, validate, normalize, pre_validate
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Общие метрики
    total_products = db.Column(db.Integer, default=0)
    processed_successfully = db.Column(db.Integer, default=0)
    errors_count = db.Column(db.Integer, default=0)
    duration_seconds = db.Column(db.Float)

    # Заполненность полей (JSON: {"title": 0.99, "brand": 0.87, ...})
    field_fill_rates = db.Column(db.JSON)

    # AI метрики
    ai_tokens_used = db.Column(db.Integer)
    ai_cache_hits = db.Column(db.Integer, default=0)
    ai_cache_misses = db.Column(db.Integer, default=0)

    # Детали ошибок (JSON массив)
    errors_json = db.Column(db.JSON)

    # Метрики нормализации
    normalization_stats = db.Column(db.JSON)  # {"brands_normalized": 42, "barcodes_fixed": 12, ...}

    supplier = db.relationship('Supplier', backref=db.backref('parsing_logs', lazy='dynamic'))

    def __repr__(self) -> str:
        return f'<ParsingLog {self.event_type} supplier={self.supplier_id} ({self.created_at})>'


class Brand(db.Model):
    """Централизованный реестр брендов (глобальный, без привязки к маркетплейсу)"""
    __tablename__ = 'brands'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)  # Каноническое имя бренда
    name_normalized = db.Column(db.String(200), nullable=False, index=True)  # lowercase без спецсимволов
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)  # pending/verified/rejected/needs_review
    country = db.Column(db.String(100))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связи
    aliases = db.relationship('BrandAlias', backref='brand', lazy='dynamic', cascade='all, delete-orphan')
    marketplace_brands = db.relationship('MarketplaceBrand', backref='brand', lazy='dynamic', cascade='all, delete-orphan')
    imported_products = db.relationship('ImportedProduct', backref='resolved_brand', lazy='dynamic', foreign_keys='ImportedProduct.resolved_brand_id')
    supplier_products = db.relationship('SupplierProduct', backref='resolved_brand', lazy='dynamic', foreign_keys='SupplierProduct.resolved_brand_id')

    __table_args__ = (
        db.UniqueConstraint('name_normalized', name='uq_brand_name_normalized'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'name_normalized': self.name_normalized,
            'status': self.status,
            'country': self.country,
            'notes': self.notes,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'aliases_count': self.aliases.count() if self.aliases else 0,
            'marketplaces_count': self.marketplace_brands.count() if self.marketplace_brands else 0,
        }

    def __repr__(self):
        return f'<Brand {self.name} ({self.status})>'


class BrandAlias(db.Model):
    """Маппинг вариантов написания бренда к каноническому имени"""
    __tablename__ = 'brand_aliases'

    id = db.Column(db.Integer, primary_key=True)
    brand_id = db.Column(db.Integer, db.ForeignKey('brands.id'), nullable=False, index=True)
    alias = db.Column(db.String(200), nullable=False)
    alias_normalized = db.Column(db.String(200), nullable=False, index=True)  # lowercase без спецсимволов
    source = db.Column(db.String(30), nullable=False, default='manual')  # manual/ai_detected/supplier_csv/auto_matched
    confidence = db.Column(db.Float, default=1.0)
    supplier_id = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    supplier = db.relationship('Supplier', backref=db.backref('brand_aliases', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('alias_normalized', name='uq_brand_alias_normalized'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'brand_id': self.brand_id,
            'alias': self.alias,
            'source': self.source,
            'confidence': self.confidence,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f'<BrandAlias "{self.alias}" -> Brand#{self.brand_id}>'


class MarketplaceBrand(db.Model):
    """Привязка бренда к маркетплейсу — имя, ID и статус на конкретной площадке"""
    __tablename__ = 'marketplace_brands'

    id = db.Column(db.Integer, primary_key=True)
    brand_id = db.Column(db.Integer, db.ForeignKey('brands.id'), nullable=False, index=True)
    marketplace_id = db.Column(db.Integer, db.ForeignKey('marketplaces.id'), nullable=False, index=True)

    marketplace_brand_name = db.Column(db.String(200), nullable=False)  # Имя бренда на площадке ("LELO", "Lelo")
    marketplace_brand_id = db.Column(db.Integer)  # ID бренда в справочнике площадки (wb_brand_id, ozon_brand_id...)
    status = db.Column(db.String(20), nullable=False, default='pending')  # pending/verified/rejected/needs_review
    verified_at = db.Column(db.DateTime)
    notes = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связи
    marketplace = db.relationship('Marketplace', backref=db.backref('marketplace_brands', lazy='dynamic'))
    category_links = db.relationship('BrandCategoryLink', backref='marketplace_brand', lazy='dynamic', cascade='all, delete-orphan')

    __table_args__ = (
        db.UniqueConstraint('brand_id', 'marketplace_id', name='uq_brand_marketplace'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'brand_id': self.brand_id,
            'marketplace_id': self.marketplace_id,
            'marketplace_brand_name': self.marketplace_brand_name,
            'marketplace_brand_id': self.marketplace_brand_id,
            'status': self.status,
            'verified_at': self.verified_at.isoformat() if self.verified_at else None,
            'marketplace_code': self.marketplace.code if self.marketplace else None,
            'marketplace_name': self.marketplace.name if self.marketplace else None,
        }

    def __repr__(self):
        return f'<MarketplaceBrand Brand#{self.brand_id} @ Marketplace#{self.marketplace_id} "{self.marketplace_brand_name}">'


class BrandCategoryLink(db.Model):
    """Допустимость бренда в категории маркетплейса"""
    __tablename__ = 'brand_category_links'

    id = db.Column(db.Integer, primary_key=True)
    marketplace_brand_id = db.Column(db.Integer, db.ForeignKey('marketplace_brands.id'), nullable=False, index=True)
    category_id = db.Column(db.Integer, nullable=False, index=True)  # subject_id (WB), category_id (Ozon) и т.д.
    category_name = db.Column(db.String(200))
    is_available = db.Column(db.Boolean, default=True, nullable=False)
    verified_at = db.Column(db.DateTime)

    __table_args__ = (
        db.UniqueConstraint('marketplace_brand_id', 'category_id', name='uq_mp_brand_category'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'marketplace_brand_id': self.marketplace_brand_id,
            'category_id': self.category_id,
            'category_name': self.category_name,
            'is_available': self.is_available,
            'verified_at': self.verified_at.isoformat() if self.verified_at else None,
        }

    def __repr__(self):
        return f'<BrandCategoryLink MPBrand#{self.marketplace_brand_id} -> Cat#{self.category_id}>'


# ============================================================================
# Запрещённые слова WB
# ============================================================================

class ProhibitedWord(db.Model):
    """
    Запрещённые слова для фильтрации при импорте в WB.

    scope:
      - 'global' — задаётся админом, действует для всех продавцов
      - 'seller' — задаётся продавцом, действует только для него

    Если replacement пустой — слово просто удаляется из текста.
    """
    __tablename__ = 'prohibited_words'

    id = db.Column(db.Integer, primary_key=True)
    word = db.Column(db.String(100), nullable=False, index=True)
    replacement = db.Column(db.String(200), nullable=False, default='')
    scope = db.Column(db.String(20), nullable=False, default='global', index=True)  # 'global' | 'seller'
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=True, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    seller = db.relationship('Seller', backref='prohibited_words', lazy=True)
    created_by = db.relationship('User', lazy=True)

    __table_args__ = (
        db.UniqueConstraint('word', 'scope', 'seller_id', name='uq_prohibited_word_scope_seller'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'word': self.word,
            'replacement': self.replacement,
            'scope': self.scope,
            'seller_id': self.seller_id,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f'<ProhibitedWord "{self.word}" → "{self.replacement}" ({self.scope})>'


class AnalyticsSnapshot(db.Model):
    """Снимок агрегированной аналитики продавца за период (кэш данных из WB Analytics API)"""
    __tablename__ = 'analytics_snapshots'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)

    # KPI агрегаты
    revenue = db.Column(db.Float, default=0)  # ordersSumRub / buyoutsSumRub
    orders_count = db.Column(db.Integer, default=0)
    buyouts_count = db.Column(db.Integer, default=0)
    buyouts_sum = db.Column(db.Float, default=0)
    cancel_count = db.Column(db.Integer, default=0)
    cancel_sum = db.Column(db.Float, default=0)
    open_card_count = db.Column(db.Integer, default=0)
    add_to_cart_count = db.Column(db.Integer, default=0)

    # Конверсии (средние)
    avg_add_to_cart_percent = db.Column(db.Float)
    avg_cart_to_order_percent = db.Column(db.Float)
    avg_buyout_percent = db.Column(db.Float)

    # Сравнение с предыдущим периодом (динамика в %)
    revenue_dynamics = db.Column(db.Float)
    orders_dynamics = db.Column(db.Float)
    buyouts_dynamics = db.Column(db.Float)

    # Данные для графиков (JSON массивы по дням)
    daily_data = db.Column(db.JSON)  # [{date, orderSum, orderCount, buyoutSum, buyoutCount, openCount, cartCount}, ...]

    # Топ товаров (JSON)
    top_products = db.Column(db.JSON)  # [{nmId, title, vendorCode, brandName, orderSum, orderCount, buyoutSum}, ...]

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    seller = db.relationship('Seller', backref=db.backref('analytics_snapshots', lazy='dynamic'))

    __table_args__ = (
        db.Index('idx_analytics_snapshot_seller_period', 'seller_id', 'period_start', 'period_end'),
    )

    def to_dict(self):
        avg_check = self.revenue / self.orders_count if self.orders_count else 0
        return {
            'period_start': self.period_start.isoformat() if self.period_start else None,
            'period_end': self.period_end.isoformat() if self.period_end else None,
            'kpi': {
                'revenue': self.revenue or 0,
                'orders': self.orders_count or 0,
                'avgCheck': round(avg_check, 2),
                'buyouts': self.buyouts_count or 0,
                'cancels': self.cancel_count or 0,
                'openCardCount': self.open_card_count or 0,
                'addToCartCount': self.add_to_cart_count or 0,
            },
            'conversions': {
                'addToCartPercent': self.avg_add_to_cart_percent or 0,
                'cartToOrderPercent': self.avg_cart_to_order_percent or 0,
                'buyoutPercent': self.avg_buyout_percent or 0,
            },
            'dynamics': {
                'revenue': self.revenue_dynamics or 0,
                'orders': self.orders_dynamics or 0,
                'buyouts': self.buyouts_dynamics or 0,
            },
            'dailyData': self.daily_data or [],
            'topProducts': self.top_products or [],
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f'<AnalyticsSnapshot seller={self.seller_id} {self.period_start}..{self.period_end}>'


class ProductAnalytics(db.Model):
    """Аналитика по отдельному товару из WB Sales Funnel API"""
    __tablename__ = 'product_analytics'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    nm_id = db.Column(db.BigInteger, nullable=False, index=True)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)

    # Продуктовые данные
    title = db.Column(db.String(500))
    vendor_code = db.Column(db.String(100))
    brand_name = db.Column(db.String(200))
    subject_name = db.Column(db.String(200))

    # Статистика за период
    open_card_count = db.Column(db.Integer, default=0)
    add_to_cart_count = db.Column(db.Integer, default=0)
    orders_count = db.Column(db.Integer, default=0)
    orders_sum = db.Column(db.Float, default=0)
    buyouts_count = db.Column(db.Integer, default=0)
    buyouts_sum = db.Column(db.Float, default=0)
    cancel_count = db.Column(db.Integer, default=0)
    cancel_sum = db.Column(db.Float, default=0)

    # Конверсии
    add_to_cart_percent = db.Column(db.Float)
    cart_to_order_percent = db.Column(db.Float)
    buyout_percent = db.Column(db.Float)

    # Остатки
    stocks_wb = db.Column(db.Integer, default=0)
    stocks_mp = db.Column(db.Integer, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    seller = db.relationship('Seller', backref=db.backref('product_analytics', lazy='dynamic'))

    __table_args__ = (
        db.Index('idx_product_analytics_seller_nm', 'seller_id', 'nm_id', 'period_start'),
    )

    def to_dict(self):
        return {
            'nmId': self.nm_id,
            'title': self.title,
            'vendorCode': self.vendor_code,
            'brandName': self.brand_name,
            'subjectName': self.subject_name,
            'openCardCount': self.open_card_count or 0,
            'addToCartCount': self.add_to_cart_count or 0,
            'ordersCount': self.orders_count or 0,
            'ordersSum': self.orders_sum or 0,
            'buyoutsCount': self.buyouts_count or 0,
            'buyoutsSum': self.buyouts_sum or 0,
            'cancelCount': self.cancel_count or 0,
            'cancelSum': self.cancel_sum or 0,
            'conversions': {
                'addToCartPercent': self.add_to_cart_percent or 0,
                'cartToOrderPercent': self.cart_to_order_percent or 0,
                'buyoutPercent': self.buyout_percent or 0,
            },
            'stocks': {
                'wb': self.stocks_wb or 0,
                'mp': self.stocks_mp or 0,
            },
        }

    def __repr__(self):
        return f'<ProductAnalytics nm={self.nm_id} seller={self.seller_id}>'


class FinanceSnapshot(db.Model):
    """Кэшированный снимок финансовых данных продавца из WB reportDetailByPeriod"""
    __tablename__ = 'finance_snapshots'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)

    # Агрегаты
    sales_total = db.Column(db.Float, default=0)          # Выручка (retail_price_withdisc_rub за продажи)
    for_pay_total = db.Column(db.Float, default=0)         # К перечислению (ppvz_for_pay)
    returns_total = db.Column(db.Float, default=0)          # Возвраты (ppvz_for_pay < 0)
    commission_total = db.Column(db.Float, default=0)       # Комиссия WB
    logistics_total = db.Column(db.Float, default=0)        # Логистика (delivery_rub + rebill_logistic_cost)
    storage_total = db.Column(db.Float, default=0)          # Хранение
    penalties_total = db.Column(db.Float, default=0)        # Штрафы
    deductions_total = db.Column(db.Float, default=0)       # Удержания
    acceptance_total = db.Column(db.Float, default=0)       # Приёмка
    additional_payment_total = db.Column(db.Float, default=0)  # Доплаты

    # Данные для графиков — по неделям [{week, forPay, commission, logistics, storage}, ...]
    weekly_data = db.Column(db.JSON)

    # Последние операции — [{date, type, description, amount, nmId}, ...]
    recent_transactions = db.Column(db.JSON)

    # Общее кол-во строк в отчёте
    report_rows_count = db.Column(db.Integer, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    seller = db.relationship('Seller', backref=db.backref('finance_snapshots', lazy='dynamic'))

    __table_args__ = (
        db.Index('idx_finance_snapshot_seller_period', 'seller_id', 'period_start', 'period_end'),
    )

    def to_dict(self):
        # ppvz_for_pay уже рассчитан WB за вычетом комиссии,
        # поэтому commission НЕ включаем в expenses (иначе двойной учёт).
        income = self.for_pay_total or 0          # положительные ppvz_for_pay
        returns = abs(self.returns_total or 0)     # отрицательные ppvz_for_pay (возвраты)

        # Доп. расходы, которые WB вычитает отдельно от ppvz_for_pay
        expenses = (
            (self.logistics_total or 0) +
            (self.storage_total or 0) +
            (self.penalties_total or 0) +
            (self.deductions_total or 0) +
            (self.acceptance_total or 0)
        )

        # Итого к перечислению = доход − возвраты − доп.расходы
        net_payout = income - returns - expenses

        return {
            'period_start': self.period_start.isoformat() if self.period_start else None,
            'period_end': self.period_end.isoformat() if self.period_end else None,
            'balance': {
                'total': round(net_payout, 2),
                'income': round(income, 2),
                'expenses': round(expenses, 2),
                'returns': round(returns, 2),
            },
            'breakdown': {
                'salesTotal': round(self.sales_total or 0, 2),
                'forPayTotal': round(self.for_pay_total or 0, 2),
                'returnsTotal': round(abs(self.returns_total or 0), 2),
                'commission': round(self.commission_total or 0, 2),
                'logistics': round(self.logistics_total or 0, 2),
                'storage': round(self.storage_total or 0, 2),
                'penalties': round(self.penalties_total or 0, 2),
                'deductions': round(self.deductions_total or 0, 2),
                'acceptance': round(self.acceptance_total or 0, 2),
                'additionalPayment': round(self.additional_payment_total or 0, 2),
            },
            'weeklyData': self.weekly_data or [],
            'recentTransactions': self.recent_transactions or [],
            'reportRowsCount': self.report_rows_count or 0,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f'<FinanceSnapshot seller={self.seller_id} {self.period_start}..{self.period_end}>'


# ============================================================================
# Сырые данные WB — локальное хранилище для аналитики за произвольный период
# ============================================================================

class WBSale(db.Model):
    """Строка продажи/возврата из WB Statistics API /api/v1/supplier/sales.
    Дедупликация по srid (уникальный ID операции на стороне WB)."""
    __tablename__ = 'wb_sales'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False)
    srid = db.Column(db.String(100), nullable=False)
    sale_id = db.Column(db.String(50))  # S-xxxxx / R-xxxxx
    nm_id = db.Column(db.BigInteger)
    date = db.Column(db.DateTime)
    last_change_date = db.Column(db.DateTime)
    supplier_article = db.Column(db.String(100))
    subject = db.Column(db.String(200))
    brand = db.Column(db.String(200))
    warehouse_name = db.Column(db.String(200))
    region_name = db.Column(db.String(200))
    country_name = db.Column(db.String(200))
    finished_price = db.Column(db.Float, default=0)
    price_with_disc = db.Column(db.Float, default=0)
    for_pay = db.Column(db.Float, default=0)
    is_return = db.Column(db.Boolean, default=False)

    __table_args__ = (
        db.UniqueConstraint('seller_id', 'srid', name='uq_wb_sale_srid'),
        db.Index('idx_wb_sale_seller_date', 'seller_id', 'date'),
        db.Index('idx_wb_sale_nm', 'seller_id', 'nm_id'),
    )


class WBOrder(db.Model):
    """Строка заказа из WB Statistics API /api/v1/supplier/orders.
    Дедупликация по srid."""
    __tablename__ = 'wb_orders'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False)
    srid = db.Column(db.String(100), nullable=False)
    nm_id = db.Column(db.BigInteger)
    date = db.Column(db.DateTime)
    last_change_date = db.Column(db.DateTime)
    supplier_article = db.Column(db.String(100))
    subject = db.Column(db.String(200))
    brand = db.Column(db.String(200))
    warehouse_name = db.Column(db.String(200))
    region_name = db.Column(db.String(200))
    oblast_okrug_name = db.Column(db.String(200))
    country_name = db.Column(db.String(200))
    total_price = db.Column(db.Float, default=0)
    finished_price = db.Column(db.Float, default=0)
    is_cancel = db.Column(db.Boolean, default=False)
    cancel_dt = db.Column(db.DateTime)
    order_type = db.Column(db.String(50))
    sticker = db.Column(db.String(100))

    __table_args__ = (
        db.UniqueConstraint('seller_id', 'srid', name='uq_wb_order_srid'),
        db.Index('idx_wb_order_seller_date', 'seller_id', 'date'),
        db.Index('idx_wb_order_nm', 'seller_id', 'nm_id'),
    )


class WBFeedback(db.Model):
    """Отзыв из WB Feedbacks API /api/v1/feedbacks.
    Дедупликация по wb_id (id отзыва на стороне WB)."""
    __tablename__ = 'wb_feedbacks'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False)
    wb_id = db.Column(db.String(100), nullable=False)  # id from WB API
    nm_id = db.Column(db.BigInteger)
    created_date = db.Column(db.DateTime)
    updated_date = db.Column(db.DateTime)
    valuation = db.Column(db.Integer)  # 1-5
    text = db.Column(db.Text)
    user_name = db.Column(db.String(200))
    product_name = db.Column(db.String(500))
    subject_name = db.Column(db.String(200))
    brand_name = db.Column(db.String(200))
    is_answered = db.Column(db.Boolean, default=False)

    __table_args__ = (
        db.UniqueConstraint('seller_id', 'wb_id', name='uq_wb_feedback_id'),
        db.Index('idx_wb_feedback_seller_date', 'seller_id', 'created_date'),
        db.Index('idx_wb_feedback_nm', 'seller_id', 'nm_id'),
    )


class WBRealizationRow(db.Model):
    """Строка отчёта реализации из WB /api/v5/supplier/reportDetailByPeriod.
    Дедупликация по rrd_id (уникальный ID строки отчёта)."""
    __tablename__ = 'wb_realization_rows'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False)
    rrd_id = db.Column(db.BigInteger, nullable=False)
    realizationreport_id = db.Column(db.BigInteger)
    rr_dt = db.Column(db.DateTime)  # Дата отчёта
    date_from = db.Column(db.Date)
    date_to = db.Column(db.Date)
    nm_id = db.Column(db.BigInteger)
    sa_name = db.Column(db.String(200))
    subject_name = db.Column(db.String(200))
    brand_name = db.Column(db.String(200))
    supplier_oper_name = db.Column(db.String(200))
    doc_type_name = db.Column(db.String(100))
    retail_price_withdisc_rub = db.Column(db.Float, default=0)
    retail_amount = db.Column(db.Float, default=0)
    ppvz_for_pay = db.Column(db.Float, default=0)
    ppvz_sales_commission = db.Column(db.Float, default=0)
    commission_percent = db.Column(db.Float, default=0)
    delivery_rub = db.Column(db.Float, default=0)
    rebill_logistic_cost = db.Column(db.Float, default=0)
    storage_fee = db.Column(db.Float, default=0)
    penalty = db.Column(db.Float, default=0)
    deduction = db.Column(db.Float, default=0)
    acceptance = db.Column(db.Float, default=0)
    additional_payment = db.Column(db.Float, default=0)
    return_amount = db.Column(db.Integer, default=0)
    delivery_amount = db.Column(db.Integer, default=0)

    __table_args__ = (
        db.UniqueConstraint('seller_id', 'rrd_id', name='uq_wb_realization_rrd'),
        db.Index('idx_wb_real_seller_date', 'seller_id', 'rr_dt'),
        db.Index('idx_wb_real_nm', 'seller_id', 'nm_id'),
        db.Index('idx_wb_real_report', 'seller_id', 'realizationreport_id'),
    )


# ============================================================================
# BackgroundJob — фоновые задачи (массовый импорт и т.д.)
# ============================================================================
class BackgroundJob(db.Model):
    __tablename__ = 'background_jobs'

    id = db.Column(db.Integer, primary_key=True)
    job_uid = db.Column(db.String(64), unique=True, nullable=False, index=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False)
    job_type = db.Column(db.String(50), nullable=False)  # 'bulk_wb_import', 'price_sync', etc.
    status = db.Column(db.String(20), nullable=False, default='pending')  # pending, running, completed, failed
    total = db.Column(db.Integer, default=0)
    processed = db.Column(db.Integer, default=0)
    succeeded = db.Column(db.Integer, default=0)
    failed_count = db.Column(db.Integer, default=0)
    progress_data = db.Column(db.Text, default='{}')  # JSON: per-item results
    result_data = db.Column(db.Text, default='{}')     # JSON: final summary
    error_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    seller = db.relationship('Seller', backref=db.backref('background_jobs', lazy='dynamic'))

    __table_args__ = (
        db.Index('idx_bgjob_seller_status', 'seller_id', 'status'),
    )

    def set_progress(self, data):
        import json as _json
        self.progress_data = _json.dumps(data, ensure_ascii=False)

    def get_progress(self):
        import json as _json
        try:
            return _json.loads(self.progress_data or '{}')
        except Exception:
            return {}

    def set_result(self, data):
        import json as _json
        self.result_data = _json.dumps(data, ensure_ascii=False)

    def get_result(self):
        import json as _json
        try:
            return _json.loads(self.result_data or '{}')
        except Exception:
            return {}

    def to_dict(self):
        return {
            'job_uid': self.job_uid,
            'job_type': self.job_type,
            'status': self.status,
            'total': self.total,
            'processed': self.processed,
            'succeeded': self.succeeded,
            'failed_count': self.failed_count,
            'progress': self.get_progress(),
            'result': self.get_result(),
            'error_message': self.error_message,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f'<BackgroundJob {self.job_uid} [{self.status}] {self.processed}/{self.total}>'


# ============================================================================
# Notification — центр уведомлений
# ============================================================================
class Notification(db.Model):
    __tablename__ = 'notifications'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False)
    category = db.Column(db.String(30), nullable=False, default='info')  # info, success, warning, error, promo
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    link = db.Column(db.String(500))  # optional URL to navigate to
    metadata_json = db.Column(db.Text, default='{}')  # extra context
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    seller = db.relationship('Seller', backref=db.backref('notifications', lazy='dynamic'))

    __table_args__ = (
        db.Index('idx_notif_seller_read', 'seller_id', 'is_read'),
        db.Index('idx_notif_created', 'created_at'),
    )

    def get_metadata(self):
        import json as _json
        try:
            return _json.loads(self.metadata_json or '{}')
        except Exception:
            return {}

    def to_dict(self):
        return {
            'id': self.id,
            'category': self.category,
            'title': self.title,
            'message': self.message,
            'is_read': self.is_read,
            'link': self.link,
            'metadata': self.get_metadata(),
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f'<Notification {self.id} [{self.category}] {self.title[:30]}>'


# ============================================================================
# ProhibitedBrand — запрещённые бренды по маркетплейсам
# ============================================================================
class ProhibitedBrand(db.Model):
    """Бренд, запрещённый к импорту на конкретном маркетплейсе."""
    __tablename__ = 'prohibited_brands'

    id = db.Column(db.Integer, primary_key=True)
    brand_name = db.Column(db.String(200), nullable=False)
    brand_name_normalized = db.Column(db.String(200), nullable=False, index=True)
    marketplace = db.Column(db.String(50), nullable=False, index=True)  # 'wb', 'ozon', 'sber' или 'all'
    reason = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('brand_name_normalized', 'marketplace', name='uq_prohibited_brand_mp'),
        db.Index('idx_prohibited_brand_active', 'marketplace', 'is_active'),
    )

    @staticmethod
    def normalize_name(name: str) -> str:
        """Нормализует имя бренда для сравнения."""
        import re as _re
        if not name:
            return ''
        n = name.strip().lower()
        n = _re.sub(r'[^\w\s]', '', n)  # убираем спецсимволы
        n = _re.sub(r'\s+', ' ', n).strip()
        return n

    def to_dict(self):
        return {
            'id': self.id,
            'brand_name': self.brand_name,
            'marketplace': self.marketplace,
            'reason': self.reason,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f'<ProhibitedBrand {self.brand_name} [{self.marketplace}]>'


# ============= АГЕНТЫ =============

class ServiceAgent(db.Model):
    """Зарегистрированный сервисный агент (Go-микросервис или внутренний)"""
    __tablename__ = 'service_agents'

    id = db.Column(db.String(36), primary_key=True)  # UUID
    name = db.Column(db.String(100), nullable=False)  # 'category-mapper', 'size-normalizer', 'seo-writer'
    display_name = db.Column(db.String(200), nullable=False)  # 'Агент категорий'
    description = db.Column(db.Text)
    agent_type = db.Column(db.String(30), nullable=False, default='external')  # 'external' (Go), 'internal' (Python)
    category = db.Column(db.String(50), nullable=False, default='general')
    # Специализация: 'catalog' (каталог/импорт), 'content' (контент/SEO),
    # 'pricing' (цены), 'compliance' (модерация/блокировки), 'analytics' (аналитика), 'general'
    status = db.Column(db.String(20), nullable=False, default='offline')  # online, offline, error
    version = db.Column(db.String(30))  # '1.0.0'
    endpoint_url = db.Column(db.String(500))  # 'http://agent-import:8080' для внешних
    api_key_hash = db.Column(db.String(255))  # Хеш API-ключа для аутентификации агента
    capabilities = db.Column(db.Text, default='[]')  # JSON: ['parse_csv', 'enrich_product', 'map_category']
    config_json = db.Column(db.Text, default='{}')  # Конфигурация агента (JSON)
    task_types = db.Column(db.Text, default='[]')  # JSON: типы задач которые принимает агент
    icon = db.Column(db.String(30), default='cpu')  # Иконка: 'cpu', 'tag', 'ruler', 'pen', 'shield', 'chart', 'camera', 'palette'
    color = db.Column(db.String(20), default='blue')  # Цвет: 'blue', 'violet', 'emerald', 'amber', 'red', 'pink', 'cyan'
    last_heartbeat = db.Column(db.DateTime)
    last_error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tasks = db.relationship('AgentTask', backref='agent', lazy='dynamic', cascade='all, delete-orphan')

    __table_args__ = (
        db.Index('idx_agent_name', 'name'),
        db.Index('idx_agent_status', 'status'),
    )

    def get_capabilities(self):
        import json as _json
        try:
            return _json.loads(self.capabilities or '[]')
        except Exception:
            return []

    def get_config(self):
        import json as _json
        try:
            return _json.loads(self.config_json or '{}')
        except Exception:
            return {}

    def is_online(self):
        if not self.last_heartbeat:
            return False
        return (datetime.utcnow() - self.last_heartbeat).total_seconds() < 120

    def get_task_types(self):
        import json as _json
        try:
            return _json.loads(self.task_types or '[]')
        except Exception:
            return []

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'display_name': self.display_name,
            'description': self.description,
            'agent_type': self.agent_type,
            'category': self.category,
            'status': self.status,
            'version': self.version,
            'endpoint_url': self.endpoint_url,
            'capabilities': self.get_capabilities(),
            'task_types': self.get_task_types(),
            'config': self.get_config(),
            'icon': self.icon or 'cpu',
            'color': self.color or 'blue',
            'is_online': self.is_online(),
            'last_heartbeat': self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            'last_error': self.last_error,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f'<ServiceAgent {self.name} [{self.status}]>'


class AgentTask(db.Model):
    """Задача, выполняемая агентом"""
    __tablename__ = 'agent_tasks'

    id = db.Column(db.String(36), primary_key=True)  # UUID
    agent_id = db.Column(db.String(36), db.ForeignKey('service_agents.id'), nullable=False, index=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    parent_task_id = db.Column(db.String(36), db.ForeignKey('agent_tasks.id'), nullable=True, index=True)

    task_type = db.Column(db.String(50), nullable=False)  # 'import_products', 'optimize_prices', 'fix_card'
    title = db.Column(db.String(300), nullable=False)  # Человекочитаемое описание
    status = db.Column(db.String(20), nullable=False, default='queued')
    # queued -> running -> completed / failed / cancelled
    priority = db.Column(db.Integer, default=0)  # 0=normal, 1=high, 2=critical

    # Входные данные
    input_data = db.Column(db.Text, default='{}')  # JSON

    # Прогресс
    total_steps = db.Column(db.Integer, default=0)
    completed_steps = db.Column(db.Integer, default=0)
    current_step_label = db.Column(db.String(300))  # 'Обогащение товара 45 из 120'

    # Retry tracking
    retry_count = db.Column(db.Integer, default=0)

    # Результат
    result_data = db.Column(db.Text, default='{}')  # JSON
    error_message = db.Column(db.Text)

    # Таймстампы
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    seller = db.relationship('Seller', foreign_keys=[seller_id])
    subtasks = db.relationship('AgentTask', backref=db.backref('parent_task', remote_side='AgentTask.id'),
                               lazy='dynamic', foreign_keys=[parent_task_id])
    steps = db.relationship('AgentTaskStep', backref='task', lazy='dynamic',
                            cascade='all, delete-orphan', order_by='AgentTaskStep.step_number')

    __table_args__ = (
        db.Index('idx_atask_seller_status', 'seller_id', 'status'),
        db.Index('idx_atask_agent_status', 'agent_id', 'status'),
        db.Index('idx_atask_created', 'created_at'),
    )

    @property
    def progress_percent(self):
        if self.total_steps and self.total_steps > 0:
            return min(100, int(self.completed_steps / self.total_steps * 100))
        return 0

    @property
    def duration_seconds(self):
        if self.started_at:
            end = self.completed_at or datetime.utcnow()
            return int((end - self.started_at).total_seconds())
        return 0

    def get_input(self):
        import json as _json
        try:
            return _json.loads(self.input_data or '{}')
        except Exception:
            return {}

    def get_result(self):
        import json as _json
        try:
            return _json.loads(self.result_data or '{}')
        except Exception:
            return {}

    @property
    def is_pipeline(self):
        """True если это задача-оркестратор с подзадачами."""
        return self.subtasks.count() > 0

    def to_dict(self):
        d = {
            'id': self.id,
            'agent_id': self.agent_id,
            'agent_name': self.agent.display_name if self.agent else None,
            'seller_id': self.seller_id,
            'task_type': self.task_type,
            'title': self.title,
            'status': self.status,
            'priority': self.priority,
            'input_data': self.input_data or '{}',
            'total_steps': self.total_steps,
            'completed_steps': self.completed_steps,
            'current_step_label': self.current_step_label,
            'progress_percent': self.progress_percent,
            'duration_seconds': self.duration_seconds,
            'result': self.get_result(),
            'error_message': self.error_message,
            'parent_task_id': self.parent_task_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
        }
        return d

    def __repr__(self):
        return f'<AgentTask {self.id[:8]} [{self.status}] {self.title[:40]}>'


class AgentTaskStep(db.Model):
    """Шаг выполнения задачи агента (лог рассуждений и действий)"""
    __tablename__ = 'agent_task_steps'

    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.String(36), db.ForeignKey('agent_tasks.id'), nullable=False, index=True)
    step_number = db.Column(db.Integer, nullable=False)

    step_type = db.Column(db.String(30), nullable=False, default='action')
    # 'thinking' — рассуждение агента
    # 'action'   — выполняемое действие (API call, DB query)
    # 'result'   — результат действия
    # 'error'    — ошибка
    # 'decision' — принятое решение

    title = db.Column(db.String(300), nullable=False)
    detail = db.Column(db.Text)  # Подробное описание / данные
    status = db.Column(db.String(20), default='completed')  # running, completed, failed, skipped
    duration_ms = db.Column(db.Integer)  # Длительность шага в мс
    metadata_json = db.Column(db.Text, default='{}')  # Доп. данные (JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index('idx_atstep_task_num', 'task_id', 'step_number'),
    )

    def get_metadata(self):
        import json as _json
        try:
            return _json.loads(self.metadata_json or '{}')
        except Exception:
            return {}

    def to_dict(self):
        return {
            'id': self.id,
            'task_id': self.task_id,
            'step_number': self.step_number,
            'step_type': self.step_type,
            'title': self.title,
            'detail': self.detail,
            'status': self.status,
            'duration_ms': self.duration_ms,
            'metadata': self.get_metadata(),
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f'<AgentTaskStep #{self.step_number} [{self.step_type}] {self.title[:30]}>'


# ============================================================================
# CONTENT FACTORY — Контент-фабрика для продвижения в соцсетях
# ============================================================================

CONTENT_PLATFORMS = ['telegram', 'vk', 'instagram', 'tiktok', 'youtube']
CONTENT_TYPES = ['promo_post', 'review', 'story_script', 'carousel']
CONTENT_TONES = ['formal', 'casual', 'creative', 'expert']
CONTENT_STATUSES = ['draft', 'approved', 'scheduled', 'publishing', 'published', 'failed', 'archived']
PRODUCT_SELECTION_MODES = ['manual', 'bestsellers', 'new_arrivals', 'rules']


class ContentFactory(db.Model):
    """Контент-фабрика продавца — конвейер генерации контента для соцсетей"""
    __tablename__ = 'content_factories'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)

    platform = db.Column(db.String(20), nullable=False)  # telegram, vk, instagram, tiktok, youtube
    content_types_json = db.Column(db.Text, default='[]')  # ["promo_post", "review", ...]

    tone = db.Column(db.String(20), default='casual')  # formal, casual, creative, expert
    style_guidelines = db.Column(db.Text)  # Текстовые инструкции для AI
    language = db.Column(db.String(10), default='ru')

    product_selection_mode = db.Column(db.String(20), default='manual')
    product_selection_rules_json = db.Column(db.Text, default='{}')  # {category, brand, price_range}

    ai_provider = db.Column(db.String(20), default='openai')  # openai, claude, gigachat, gemini
    ai_model = db.Column(db.String(100))  # Конкретная модель (если пусто — дефолт провайдера)
    schedule_cron = db.Column(db.String(100))  # cron-выражение для автогенерации
    auto_approve = db.Column(db.Boolean, default=False)
    auto_generate = db.Column(db.Boolean, default=False)  # Автогенерация: рандомный товар → AI → пост
    generate_interval_minutes = db.Column(db.Integer, default=120)  # Интервал автогенерации (мин)
    last_auto_generate_at = db.Column(db.DateTime, nullable=True)  # Время последней автогенерации
    auto_publish = db.Column(db.Boolean, default=False)  # Автопубликация одобренных постов
    publish_interval_minutes = db.Column(db.Integer, default=60)  # Интервал между публикациями (мин)
    last_auto_publish_at = db.Column(db.DateTime, nullable=True)  # Время последней автопубликации

    default_social_account_id = db.Column(db.Integer, db.ForeignKey('social_accounts.id', use_alter=True), nullable=True)

    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    seller = db.relationship('Seller', backref=db.backref('content_factories', lazy='dynamic'))
    items = db.relationship('ContentItem', backref='factory', lazy='dynamic', cascade='all, delete-orphan')
    plans = db.relationship('ContentPlan', backref='factory', lazy='dynamic', cascade='all, delete-orphan')

    def get_content_types(self):
        try:
            return json.loads(self.content_types_json or '[]')
        except Exception:
            return []

    def set_content_types(self, types):
        self.content_types_json = json.dumps(types)

    def get_selection_rules(self):
        try:
            return json.loads(self.product_selection_rules_json or '{}')
        except Exception:
            return {}

    def set_selection_rules(self, rules):
        self.product_selection_rules_json = json.dumps(rules)

    def to_dict(self):
        return {
            'id': self.id,
            'seller_id': self.seller_id,
            'name': self.name,
            'description': self.description,
            'platform': self.platform,
            'content_types': self.get_content_types(),
            'tone': self.tone,
            'style_guidelines': self.style_guidelines,
            'product_selection_mode': self.product_selection_mode,
            'product_selection_rules': self.get_selection_rules(),
            'ai_provider': self.ai_provider,
            'schedule_cron': self.schedule_cron,
            'auto_approve': self.auto_approve,
            'auto_generate': self.auto_generate,
            'generate_interval_minutes': self.generate_interval_minutes,
            'auto_publish': self.auto_publish,
            'publish_interval_minutes': self.publish_interval_minutes,
            'is_active': self.is_active,
            'items_count': self.items.count() if self.items else 0,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f'<ContentFactory #{self.id} "{self.name}" [{self.platform}]>'


class SocialAccount(db.Model):
    """Подключённый аккаунт в социальной сети"""
    __tablename__ = 'social_accounts'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)

    platform = db.Column(db.String(20), nullable=False)  # telegram, vk, instagram, tiktok, youtube
    account_name = db.Column(db.String(200))  # Название канала/группы/аккаунта
    account_id = db.Column(db.String(200))  # ID в соцсети (chat_id, group_id, etc.)

    _credentials_encrypted = db.Column('credentials', db.Text)  # Зашифрованные токены

    is_active = db.Column(db.Boolean, default=True)
    connected_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_used_at = db.Column(db.DateTime)
    last_error = db.Column(db.Text)

    seller = db.relationship('Seller', backref=db.backref('social_accounts', lazy='dynamic'))

    @property
    def credentials(self):
        if not self._credentials_encrypted:
            return None
        encryption_key = os.environ.get('ENCRYPTION_KEY', '')
        if not encryption_key:
            return self._credentials_encrypted
        try:
            f = Fernet(encryption_key.encode())
            return f.decrypt(self._credentials_encrypted.encode()).decode()
        except Exception:
            return self._credentials_encrypted

    @credentials.setter
    def credentials(self, value):
        if not value:
            self._credentials_encrypted = None
            return
        encryption_key = os.environ.get('ENCRYPTION_KEY', '')
        if not encryption_key:
            self._credentials_encrypted = value
            return
        try:
            f = Fernet(encryption_key.encode())
            self._credentials_encrypted = f.encrypt(value.encode()).decode()
        except Exception:
            self._credentials_encrypted = value

    def get_credentials_dict(self):
        creds = self.credentials
        if not creds:
            return {}
        try:
            return json.loads(creds)
        except Exception:
            return {}

    def set_credentials_dict(self, creds_dict):
        self.credentials = json.dumps(creds_dict)

    def to_dict(self):
        return {
            'id': self.id,
            'seller_id': self.seller_id,
            'platform': self.platform,
            'account_name': self.account_name,
            'account_id': self.account_id,
            'is_active': self.is_active,
            'connected_at': self.connected_at.isoformat() if self.connected_at else None,
            'last_used_at': self.last_used_at.isoformat() if self.last_used_at else None,
            'last_error': self.last_error,
        }

    def __repr__(self):
        return f'<SocialAccount #{self.id} {self.platform}:{self.account_name}>'


class ContentTemplate(db.Model):
    """Шаблон для генерации контента"""
    __tablename__ = 'content_templates'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=True, index=True)  # NULL = системный

    platform = db.Column(db.String(20), nullable=False)
    content_type = db.Column(db.String(30), nullable=False)  # promo_post, review, story_script, carousel
    name = db.Column(db.String(200), nullable=False)

    system_prompt = db.Column(db.Text, nullable=False)
    user_prompt_template = db.Column(db.Text, nullable=False)  # С плейсхолдерами {product_name}, {price}, etc.
    example_output = db.Column(db.Text)
    hashtag_strategy = db.Column(db.Text)  # Инструкции по хештегам

    is_system = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    seller = db.relationship('Seller', backref=db.backref('content_templates', lazy='dynamic'))

    __table_args__ = (
        db.Index('idx_ct_platform_type', 'platform', 'content_type'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'seller_id': self.seller_id,
            'platform': self.platform,
            'content_type': self.content_type,
            'name': self.name,
            'system_prompt': self.system_prompt,
            'user_prompt_template': self.user_prompt_template,
            'example_output': self.example_output,
            'hashtag_strategy': self.hashtag_strategy,
            'is_system': self.is_system,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f'<ContentTemplate #{self.id} "{self.name}" [{self.platform}/{self.content_type}]>'


class ContentItem(db.Model):
    """Единица сгенерированного контента"""
    __tablename__ = 'content_items'

    id = db.Column(db.Integer, primary_key=True)
    factory_id = db.Column(db.Integer, db.ForeignKey('content_factories.id'), nullable=False, index=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    template_id = db.Column(db.Integer, db.ForeignKey('content_templates.id'), nullable=True)

    platform = db.Column(db.String(20), nullable=False)
    content_type = db.Column(db.String(30), nullable=False)

    product_ids_json = db.Column(db.Text, default='[]')  # IDs товаров
    title = db.Column(db.String(500))
    body_text = db.Column(db.Text, nullable=False)
    hashtags_json = db.Column(db.Text, default='[]')
    media_urls_json = db.Column(db.Text, default='[]')
    platform_specific_json = db.Column(db.Text, default='{}')  # Доп. данные под платформу

    status = db.Column(db.String(20), default='draft', index=True)
    scheduled_at = db.Column(db.DateTime)
    published_at = db.Column(db.DateTime)

    social_account_id = db.Column(db.Integer, db.ForeignKey('social_accounts.id'), nullable=True)
    external_post_id = db.Column(db.String(200))  # ID поста после публикации
    external_post_url = db.Column(db.String(500))  # URL поста

    ai_provider = db.Column(db.String(20))
    ai_model = db.Column(db.String(50))
    tokens_used = db.Column(db.Integer)
    generation_time_ms = db.Column(db.Integer)
    error_message = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    seller = db.relationship('Seller', backref=db.backref('content_items', lazy='dynamic'))
    template = db.relationship('ContentTemplate', backref=db.backref('items', lazy='dynamic'))
    social_account = db.relationship('SocialAccount', backref=db.backref('published_items', lazy='dynamic'))

    __table_args__ = (
        db.Index('idx_ci_factory_status', 'factory_id', 'status'),
        db.Index('idx_ci_scheduled', 'status', 'scheduled_at'),
    )

    def get_product_ids(self):
        try:
            return json.loads(self.product_ids_json or '[]')
        except Exception:
            return []

    def set_product_ids(self, ids):
        self.product_ids_json = json.dumps(ids)

    def get_hashtags(self):
        try:
            return json.loads(self.hashtags_json or '[]')
        except Exception:
            return []

    def set_hashtags(self, tags):
        self.hashtags_json = json.dumps(tags)

    def get_media_urls(self):
        # Сохранённые URL (локальные /content-photos/... или /photos/public/...)
        try:
            urls = json.loads(self.media_urls_json or '[]')
            public_urls = [u for u in urls if isinstance(u, str) and u.startswith(('http', '/'))]
            if public_urls:
                return public_urls
        except Exception:
            pass

        # Фоллбэк 1: кэшированные content-photos по nm_id
        try:
            product_ids = self.get_product_ids()
            if product_ids:
                product = Product.query.get(product_ids[0])
                if product and product.nm_id:
                    from services.content_photo_cache import get_cached_photo_urls
                    cached = get_cached_photo_urls(product.nm_id)
                    if cached:
                        return cached
        except Exception:
            pass

        # Фоллбэк 2: ImportedProduct фото
        try:
            product_ids = self.get_product_ids()
            if product_ids:
                imported = ImportedProduct.query.filter_by(product_id=product_ids[0]).first()
                if imported:
                    from routes.photos import generate_public_photo_urls
                    local_urls = generate_public_photo_urls(imported)
                    if local_urls:
                        return local_urls
        except Exception:
            pass
        return []

    def get_platform_specific(self):
        try:
            return json.loads(self.platform_specific_json or '{}')
        except Exception:
            return {}

    def to_dict(self):
        return {
            'id': self.id,
            'factory_id': self.factory_id,
            'seller_id': self.seller_id,
            'template_id': self.template_id,
            'platform': self.platform,
            'content_type': self.content_type,
            'product_ids': self.get_product_ids(),
            'title': self.title,
            'body_text': self.body_text,
            'hashtags': self.get_hashtags(),
            'media_urls': self.get_media_urls(),
            'platform_specific': self.get_platform_specific(),
            'status': self.status,
            'scheduled_at': self.scheduled_at.isoformat() if self.scheduled_at else None,
            'published_at': self.published_at.isoformat() if self.published_at else None,
            'social_account_id': self.social_account_id,
            'external_post_id': self.external_post_id,
            'external_post_url': self.external_post_url,
            'ai_provider': self.ai_provider,
            'ai_model': self.ai_model,
            'tokens_used': self.tokens_used,
            'generation_time_ms': self.generation_time_ms,
            'error_message': self.error_message,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f'<ContentItem #{self.id} [{self.platform}/{self.content_type}] {self.status}>'


class ContentPlan(db.Model):
    """Контент-план (календарь публикаций)"""
    __tablename__ = 'content_plans'

    id = db.Column(db.Integer, primary_key=True)
    factory_id = db.Column(db.Integer, db.ForeignKey('content_factories.id'), nullable=False, index=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)

    name = db.Column(db.String(200), nullable=False)
    date_from = db.Column(db.Date, nullable=False)
    date_to = db.Column(db.Date, nullable=False)

    # [{day_of_week: 0-6, time: "10:00", content_type: "promo_post"}, ...]
    slots_json = db.Column(db.Text, default='[]')

    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    seller = db.relationship('Seller', backref=db.backref('content_plans', lazy='dynamic'))

    def get_slots(self):
        try:
            return json.loads(self.slots_json or '[]')
        except Exception:
            return []

    def set_slots(self, slots):
        self.slots_json = json.dumps(slots)

    def to_dict(self):
        return {
            'id': self.id,
            'factory_id': self.factory_id,
            'seller_id': self.seller_id,
            'name': self.name,
            'date_from': self.date_from.isoformat() if self.date_from else None,
            'date_to': self.date_to.isoformat() if self.date_to else None,
            'slots': self.get_slots(),
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f'<ContentPlan #{self.id} "{self.name}">'
