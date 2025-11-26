"""
Модели базы данных для платформы продавцов WB
"""
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from typing import Optional
import os
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

    # Медиа
    photos_json = db.Column(db.Text)  # JSON с URL фотографий
    video_url = db.Column(db.String(500))  # URL видео

    # Размеры и баркоды
    sizes_json = db.Column(db.Text)  # JSON с размерами и баркодами

    # Характеристики и описание
    characteristics_json = db.Column(db.Text)  # JSON с характеристиками товара
    description = db.Column(db.Text)  # Описание товара
    dimensions_json = db.Column(db.Text)  # JSON с габаритами (длина, ширина, высота)

    # Метаданные
    is_active = db.Column(db.Boolean, default=True)  # Активна ли карточка
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_sync = db.Column(db.DateTime)  # Последняя синхронизация данных

    # Индексы для быстрых запросов
    __table_args__ = (
        db.Index('idx_seller_nm_id', 'seller_id', 'nm_id'),
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
    sexoptovik_password = db.Column(db.String(200))  # Пароль для sexoptovik.ru

    # Настройки импорта
    import_only_new = db.Column(db.Boolean, default=True, nullable=False)  # Импортировать только новые товары
    auto_enable_products = db.Column(db.Boolean, default=False, nullable=False)  # Автоматически активировать товары
    use_blurred_images = db.Column(db.Boolean, default=True, nullable=False)  # Использовать блюренные фото когда доступно

    # Настройки обработки фото
    resize_images_to_1200 = db.Column(db.Boolean, default=True, nullable=False)  # Приводить к 1200x1200
    image_background_color = db.Column(db.String(20), default='white')  # Цвет фона для дорисовки

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


class ImportedProduct(db.Model):
    """Импортированные товары из внешних источников"""
    __tablename__ = 'imported_products'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True, index=True)  # Ссылка на созданный товар (если создан)

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

    # Статус импорта
    import_status = db.Column(db.String(50), default='pending')  # 'pending', 'validated', 'imported', 'failed'
    validation_errors = db.Column(db.Text)  # Ошибки валидации (JSON)
    import_error = db.Column(db.Text)  # Ошибка импорта

    # Метаданные
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    imported_at = db.Column(db.DateTime)  # Когда импортировано в WB
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связи
    product = db.relationship('Product', backref=db.backref('import_source', uselist=False))

    # Индексы
    __table_args__ = (
        db.Index('idx_imported_seller_status', 'seller_id', 'import_status'),
        db.Index('idx_imported_external_id', 'external_id', 'source_type'),
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


# ============= TELEGRAM NOTIFICATIONS =============

class TelegramSettings(db.Model):
    """Настройки Telegram уведомлений для продавца"""
    __tablename__ = 'telegram_settings'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, unique=True, index=True)

    # Настройки бота
    is_enabled = db.Column(db.Boolean, default=False, nullable=False)  # Включены ли уведомления
    bot_token = db.Column(db.String(500))  # Токен бота (зашифрованный)
    chat_id = db.Column(db.String(200))  # ID чата для отправки уведомлений

    # Типы уведомлений
    notify_low_stock = db.Column(db.Boolean, default=True, nullable=False)  # Низкие остатки
    low_stock_threshold = db.Column(db.Integer, default=5)  # Порог низких остатков

    notify_price_changes = db.Column(db.Boolean, default=True, nullable=False)  # Изменения цен
    notify_stock_changes = db.Column(db.Boolean, default=False, nullable=False)  # Изменения остатков

    notify_sync_errors = db.Column(db.Boolean, default=True, nullable=False)  # Ошибки синхронизации
    notify_import_complete = db.Column(db.Boolean, default=True, nullable=False)  # Завершение импорта
    notify_bulk_operations = db.Column(db.Boolean, default=True, nullable=False)  # Массовые операции

    # Расписание уведомлений
    daily_summary = db.Column(db.Boolean, default=False, nullable=False)  # Ежедневная сводка
    daily_summary_time = db.Column(db.String(5), default='09:00')  # Время отправки (HH:MM)

    # Статус последней отправки
    last_notification_at = db.Column(db.DateTime)  # Время последнего уведомления
    last_notification_status = db.Column(db.String(50))  # Статус ('success', 'failed')
    last_notification_error = db.Column(db.Text)  # Текст ошибки

    # Метаданные
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Связь с продавцом
    seller = db.relationship('Seller', backref=db.backref('telegram_settings', uselist=False))

    def __repr__(self) -> str:
        return f'<TelegramSettings seller_id={self.seller_id} enabled={self.is_enabled}>'

    def to_dict(self) -> dict:
        """Конвертировать в словарь для JSON"""
        return {
            'id': self.id,
            'seller_id': self.seller_id,
            'is_enabled': self.is_enabled,
            'chat_id': self.chat_id,
            'notify_low_stock': self.notify_low_stock,
            'low_stock_threshold': self.low_stock_threshold,
            'notify_price_changes': self.notify_price_changes,
            'notify_stock_changes': self.notify_stock_changes,
            'notify_sync_errors': self.notify_sync_errors,
            'notify_import_complete': self.notify_import_complete,
            'notify_bulk_operations': self.notify_bulk_operations,
            'daily_summary': self.daily_summary,
            'daily_summary_time': self.daily_summary_time,
            'last_notification_at': self.last_notification_at.isoformat() if self.last_notification_at else None,
            'last_notification_status': self.last_notification_status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class TelegramNotificationLog(db.Model):
    """Лог отправленных Telegram уведомлений"""
    __tablename__ = 'telegram_notification_log'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)

    # Тип уведомления
    notification_type = db.Column(db.String(50), nullable=False, index=True)  # 'low_stock', 'price_change', 'error', etc.

    # Содержимое
    message_text = db.Column(db.Text, nullable=False)  # Текст сообщения

    # Связанные объекты
    related_product_id = db.Column(db.Integer, db.ForeignKey('products.id'))  # Связанный товар
    related_data = db.Column(db.JSON)  # Дополнительные данные

    # Статус отправки
    sent_successfully = db.Column(db.Boolean, default=False, nullable=False)
    error_message = db.Column(db.Text)  # Ошибка при отправке
    telegram_message_id = db.Column(db.String(100))  # ID сообщения в Telegram

    # Метаданные
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Связи
    product = db.relationship('Product', backref=db.backref('telegram_notifications', lazy='dynamic'))

    # Индексы
    __table_args__ = (
        db.Index('idx_tg_log_seller_created', 'seller_id', 'created_at'),
        db.Index('idx_tg_log_type_created', 'notification_type', 'created_at'),
    )

    def __repr__(self) -> str:
        return f'<TelegramNotificationLog seller_id={self.seller_id} type={self.notification_type}>'
