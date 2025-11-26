"""
Модуль аналитики для дашборда продавца
"""
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from sqlalchemy import func, desc, and_, or_
from models import (
    db, Product, SellerReport, PriceHistory, SuspiciousPriceChange,
    ProductStock, BulkEditHistory
)
import json


class SellerAnalytics:
    """Класс для сбора аналитики продавца"""

    def __init__(self, seller_id: int):
        """
        Инициализация

        Args:
            seller_id: ID продавца
        """
        self.seller_id = seller_id

    def get_overview_stats(self) -> Dict[str, Any]:
        """
        Получить общую статистику

        Returns:
            Словарь с основными показателями
        """
        # Всего товаров
        total_products = db.session.query(func.count(Product.id)).filter(
            Product.seller_id == self.seller_id
        ).scalar() or 0

        # Активных товаров
        active_products = db.session.query(func.count(Product.id)).filter(
            and_(
                Product.seller_id == self.seller_id,
                Product.is_active == True
            )
        ).scalar() or 0

        # Товары с низкими остатками (< 5)
        low_stock_products = db.session.query(func.count(Product.id)).filter(
            and_(
                Product.seller_id == self.seller_id,
                Product.is_active == True,
                Product.quantity.isnot(None),
                Product.quantity < 5,
                Product.quantity > 0
            )
        ).scalar() or 0

        # Товары без остатков (включая NULL как 0)
        out_of_stock_products = db.session.query(func.count(Product.id)).filter(
            and_(
                Product.seller_id == self.seller_id,
                Product.is_active == True,
                or_(
                    Product.quantity == 0,
                    Product.quantity.is_(None)
                )
            )
        ).scalar() or 0

        # Средняя цена
        avg_price = db.session.query(func.avg(Product.price)).filter(
            and_(
                Product.seller_id == self.seller_id,
                Product.is_active == True,
                Product.price.isnot(None)
            )
        ).scalar() or 0

        # Общий остаток
        total_stock = db.session.query(func.sum(Product.quantity)).filter(
            and_(
                Product.seller_id == self.seller_id,
                Product.is_active == True
            )
        ).scalar() or 0

        # Изменения цен за последние 24 часа
        yesterday = datetime.utcnow() - timedelta(days=1)
        price_changes_24h = db.session.query(func.count(PriceHistory.id)).filter(
            and_(
                PriceHistory.seller_id == self.seller_id,
                PriceHistory.created_at >= yesterday
            )
        ).scalar() or 0

        # Подозрительные изменения (не просмотренные)
        suspicious_changes = db.session.query(func.count(SuspiciousPriceChange.id)).filter(
            and_(
                SuspiciousPriceChange.seller_id == self.seller_id,
                SuspiciousPriceChange.is_reviewed == False
            )
        ).scalar() or 0

        return {
            'total_products': total_products,
            'active_products': active_products,
            'low_stock_products': low_stock_products,
            'out_of_stock_products': out_of_stock_products,
            'avg_price': float(avg_price) if avg_price else 0,
            'total_stock': total_stock,
            'price_changes_24h': price_changes_24h,
            'suspicious_changes': suspicious_changes
        }

    def get_price_history_chart(self, days: int = 30) -> Dict[str, List]:
        """
        Получить данные для графика изменения цен

        Args:
            days: Количество дней для отображения

        Returns:
            Данные для Chart.js (labels и datasets)
        """
        start_date = datetime.utcnow() - timedelta(days=days)

        # Получаем историю изменений цен
        history = db.session.query(
            func.date(PriceHistory.created_at).label('date'),
            func.count(PriceHistory.id).label('count')
        ).filter(
            and_(
                PriceHistory.seller_id == self.seller_id,
                PriceHistory.created_at >= start_date
            )
        ).group_by(
            func.date(PriceHistory.created_at)
        ).order_by(
            func.date(PriceHistory.created_at)
        ).all()

        labels = []
        data = []

        if history:
            for record in history:
                labels.append(record.date.strftime('%d.%m'))
                data.append(record.count)
        else:
            # Если истории нет, показываем пустой график
            labels = ['Нет данных']
            data = [0]

        return {
            'labels': labels,
            'datasets': [{
                'label': 'Изменения цен',
                'data': data,
                'borderColor': 'rgb(75, 192, 192)',
                'backgroundColor': 'rgba(75, 192, 192, 0.2)',
                'tension': 0.1
            }]
        }

    def get_top_products_by_stock(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Получить ТОП товаров по остаткам

        Args:
            limit: Количество товаров

        Returns:
            Список товаров с наибольшими остатками
        """
        products = db.session.query(Product).filter(
            and_(
                Product.seller_id == self.seller_id,
                Product.is_active == True
            )
        ).order_by(
            desc(Product.quantity)
        ).limit(limit).all()

        return [
            {
                'nm_id': p.nm_id,
                'vendor_code': p.vendor_code,
                'title': p.title,
                'brand': p.brand,
                'quantity': p.quantity,
                'price': float(p.price) if p.price else 0
            }
            for p in products
        ]

    def get_low_stock_products(self, threshold: int = 5, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Получить товары с низкими остатками

        Args:
            threshold: Порог низких остатков
            limit: Количество товаров

        Returns:
            Список товаров с низкими остатками
        """
        products = db.session.query(Product).filter(
            and_(
                Product.seller_id == self.seller_id,
                Product.is_active == True,
                Product.quantity > 0,
                Product.quantity <= threshold
            )
        ).order_by(
            Product.quantity
        ).limit(limit).all()

        return [
            {
                'nm_id': p.nm_id,
                'vendor_code': p.vendor_code,
                'title': p.title,
                'brand': p.brand,
                'quantity': p.quantity,
                'price': float(p.price) if p.price else 0
            }
            for p in products
        ]

    def get_price_distribution_chart(self) -> Dict[str, List]:
        """
        Получить распределение товаров по ценовым категориям

        Returns:
            Данные для Chart.js (круговая диаграмма)
        """
        # Определяем ценовые категории
        categories = [
            ('0-500', 0, 500),
            ('500-1000', 500, 1000),
            ('1000-2000', 1000, 2000),
            ('2000-5000', 2000, 5000),
            ('5000+', 5000, float('inf'))
        ]

        labels = []
        data = []

        for label, min_price, max_price in categories:
            if max_price == float('inf'):
                count = db.session.query(func.count(Product.id)).filter(
                    and_(
                        Product.seller_id == self.seller_id,
                        Product.is_active == True,
                        Product.price >= min_price
                    )
                ).scalar() or 0
            else:
                count = db.session.query(func.count(Product.id)).filter(
                    and_(
                        Product.seller_id == self.seller_id,
                        Product.is_active == True,
                        Product.price >= min_price,
                        Product.price < max_price
                    )
                ).scalar() or 0

            if count > 0:  # Добавляем только непустые категории
                labels.append(label)
                data.append(count)

        # Если нет данных, показываем пустую диаграмму
        if not labels:
            labels = ['Нет данных']
            data = [0]

        return {
            'labels': labels,
            'datasets': [{
                'label': 'Товары по цене',
                'data': data,
                'backgroundColor': [
                    'rgba(255, 99, 132, 0.6)',
                    'rgba(54, 162, 235, 0.6)',
                    'rgba(255, 206, 86, 0.6)',
                    'rgba(75, 192, 192, 0.6)',
                    'rgba(153, 102, 255, 0.6)'
                ]
            }]
        }

    def get_recent_reports(self, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Получить последние отчеты

        Args:
            limit: Количество отчетов

        Returns:
            Список последних отчетов
        """
        reports = db.session.query(SellerReport).filter(
            SellerReport.seller_id == self.seller_id
        ).order_by(
            desc(SellerReport.created_at)
        ).limit(limit).all()

        result = []
        for report in reports:
            summary = report.summary if isinstance(report.summary, dict) else json.loads(report.summary)
            result.append({
                'id': report.id,
                'created_at': report.created_at.strftime('%d.%m.%Y %H:%M'),
                'total_revenue': summary.get('total_revenue', 0),
                'total_profit': summary.get('total_profit', 0),
                'total_items': summary.get('total_items', 0),
                'processed_path': report.processed_path
            })

        return result

    def get_brands_distribution(self, limit: int = 10) -> Dict[str, List]:
        """
        Получить распределение товаров по брендам

        Args:
            limit: Количество брендов

        Returns:
            Данные для Chart.js (горизонтальная диаграмма)
        """
        brands = db.session.query(
            Product.brand,
            func.count(Product.id).label('count')
        ).filter(
            and_(
                Product.seller_id == self.seller_id,
                Product.is_active == True,
                Product.brand.isnot(None),
                Product.brand != ''
            )
        ).group_by(
            Product.brand
        ).order_by(
            desc('count')
        ).limit(limit).all()

        labels = []
        data = []

        for brand, count in brands:
            labels.append(brand)
            data.append(count)

        # Если нет данных, показываем пустую диаграмму
        if not labels:
            labels = ['Нет данных']
            data = [0]

        return {
            'labels': labels,
            'datasets': [{
                'label': 'Количество товаров',
                'data': data,
                'backgroundColor': 'rgba(54, 162, 235, 0.6)',
                'borderColor': 'rgba(54, 162, 235, 1)',
                'borderWidth': 1
            }]
        }

    def get_stock_status_chart(self) -> Dict[str, List]:
        """
        Получить статистику по статусам остатков

        Returns:
            Данные для Chart.js (круговая диаграмма)
        """
        # В наличии (> 5)
        in_stock = db.session.query(func.count(Product.id)).filter(
            and_(
                Product.seller_id == self.seller_id,
                Product.is_active == True,
                Product.quantity > 5
            )
        ).scalar() or 0

        # Низкие остатки (1-5)
        low_stock = db.session.query(func.count(Product.id)).filter(
            and_(
                Product.seller_id == self.seller_id,
                Product.is_active == True,
                Product.quantity > 0,
                Product.quantity <= 5
            )
        ).scalar() or 0

        # Нет в наличии (0)
        out_of_stock = db.session.query(func.count(Product.id)).filter(
            and_(
                Product.seller_id == self.seller_id,
                Product.is_active == True,
                Product.quantity == 0
            )
        ).scalar() or 0

        return {
            'labels': ['В наличии', 'Низкие остатки', 'Нет в наличии'],
            'datasets': [{
                'data': [in_stock, low_stock, out_of_stock],
                'backgroundColor': [
                    'rgba(75, 192, 192, 0.6)',
                    'rgba(255, 206, 86, 0.6)',
                    'rgba(255, 99, 132, 0.6)'
                ]
            }]
        }

    def get_recent_suspicious_changes(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Получить последние подозрительные изменения

        Args:
            limit: Количество записей

        Returns:
            Список подозрительных изменений
        """
        changes = db.session.query(SuspiciousPriceChange).filter(
            SuspiciousPriceChange.seller_id == self.seller_id
        ).order_by(
            desc(SuspiciousPriceChange.created_at)
        ).limit(limit).all()

        return [change.to_dict() for change in changes]

    def get_dashboard_data(self) -> Dict[str, Any]:
        """
        Получить все данные для дашборда одним запросом

        Returns:
            Полный набор данных для дашборда
        """
        return {
            'overview': self.get_overview_stats(),
            'price_history_chart': self.get_price_history_chart(days=30),
            'price_distribution_chart': self.get_price_distribution_chart(),
            'brands_distribution_chart': self.get_brands_distribution(limit=10),
            'stock_status_chart': self.get_stock_status_chart(),
            'top_products': self.get_top_products_by_stock(limit=10),
            'low_stock_products': self.get_low_stock_products(threshold=5, limit=10),
            'recent_reports': self.get_recent_reports(limit=5),
            'suspicious_changes': self.get_recent_suspicious_changes(limit=10)
        }
