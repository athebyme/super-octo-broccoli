"""
Модели базы данных для платформы продавцов WB
"""
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

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
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True)
    company_name = db.Column(db.String(200), nullable=False)
    wb_api_key = db.Column(db.String(500))  # API ключ WB (опционально)
    wb_seller_id = db.Column(db.String(100))  # ID продавца в WB
    contact_phone = db.Column(db.String(20))
    notes = db.Column(db.Text)  # Заметки админа
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    reports = db.relationship(
        'SellerReport',
        backref='seller',
        cascade='all, delete-orphan',
        lazy='dynamic',
    )

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
