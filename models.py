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

    # Связь с продавцом (если это продавец)
    seller = db.relationship('Seller', backref='user', uselist=False, cascade='all, delete-orphan')

    def set_password(self, password: str) -> None:
        """Установить хеш пароля"""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        """Проверить пароль"""
        return check_password_hash(self.password_hash, password)

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
            'price': float(self.price) if self.price else None,
            'discount_price': float(self.discount_price) if self.discount_price else None,
            'quantity': self.quantity,
            'is_active': self.is_active,
            'last_sync': self.last_sync.isoformat() if self.last_sync else None
        }


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
