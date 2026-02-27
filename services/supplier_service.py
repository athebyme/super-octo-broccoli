# -*- coding: utf-8 -*-
"""
Сервисный слой для работы с базой товаров поставщиков

Предоставляет бизнес-логику для:
- CRUD поставщиков
- Синхронизация каталога из CSV
- Импорт товаров к продавцу
- Обновление товаров продавца из базы поставщика
- AI-валидация и обогащение на уровне поставщика
"""
import csv
import json
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from io import StringIO
from typing import Dict, List, Optional, Tuple

import requests

from models import (
    db, Supplier, SupplierProduct, SellerSupplier,
    ImportedProduct, Seller, CategoryMapping,
    log_admin_action
)

logger = logging.getLogger(__name__)


# ============================================================================
# RESULT DATACLASSES
# ============================================================================

@dataclass
class SyncResult:
    """Результат синхронизации каталога"""
    success: bool = True
    total_in_csv: int = 0
    added: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    error_messages: list = field(default_factory=list)
    duration_seconds: float = 0.0


@dataclass
class ImportResult:
    """Результат импорта товаров к продавцу"""
    success: bool = True
    total_requested: int = 0
    imported: int = 0
    skipped: int = 0
    errors: int = 0
    error_messages: list = field(default_factory=list)
    imported_product_ids: list = field(default_factory=list)


@dataclass
class BulkActionResult:
    """Результат массовой операции"""
    success: bool = True
    total: int = 0
    processed: int = 0
    errors: int = 0
    error_messages: list = field(default_factory=list)


# ============================================================================
# CSV PARSER (для supplier_products)
# ============================================================================

class SupplierCSVParser:
    """
    Парсер CSV для различных поставщиков.
    Сейчас поддерживает формат sexoptovik, расширяемо для других.
    """

    def __init__(self, supplier: Supplier):
        self.supplier = supplier
        self.delimiter = supplier.csv_delimiter or ';'
        self.encoding = supplier.csv_encoding or 'cp1251'

    def fetch_csv(self) -> Optional[str]:
        """Скачать CSV по URL"""
        if not self.supplier.csv_source_url:
            logger.error(f"Supplier {self.supplier.code}: CSV URL не задан")
            return None

        try:
            resp = requests.get(self.supplier.csv_source_url, timeout=60)
            resp.raise_for_status()
            return resp.content.decode(self.encoding, errors='replace')
        except Exception as e:
            logger.error(f"Ошибка загрузки CSV для {self.supplier.code}: {e}")
            return None

    def parse(self, csv_content: str) -> List[Dict]:
        """Парсит CSV и возвращает список товаров"""
        if self.supplier.code == 'sexoptovik':
            return self._parse_sexoptovik(csv_content)
        # Для других поставщиков — generic парсинг
        return self._parse_generic(csv_content)

    def _parse_sexoptovik(self, csv_content: str) -> List[Dict]:
        """Парсинг формата sexoptovik"""
        products = []
        reader = csv.reader(StringIO(csv_content), delimiter=self.delimiter, quotechar='"')

        for row_num, row in enumerate(reader, 1):
            try:
                if len(row) < 15:
                    continue

                external_id = row[0].strip() if len(row) > 0 else ''
                vendor_code = row[1].strip() if len(row) > 1 else ''
                title = row[2].strip() if len(row) > 2 else ''

                if not external_id or not title:
                    continue

                # Категории (через #)
                categories_raw = row[3].strip() if len(row) > 3 else ''
                categories = [c.strip() for c in categories_raw.split('#') if c.strip()]
                main_category = categories[0] if categories else ''

                brand = row[4].strip() if len(row) > 4 else ''
                country = row[5].strip() if len(row) > 5 else ''
                general_category = row[6].strip() if len(row) > 6 else ''
                gender = row[8].strip() if len(row) > 8 else ''

                # Цвета
                colors_raw = row[9].strip() if len(row) > 9 else ''
                colors = [c.strip() for c in colors_raw.split(',') if c.strip()]

                # Размеры
                sizes_raw = row[10].strip() if len(row) > 10 else ''

                # Фото
                photo_codes_raw = row[13].strip() if len(row) > 13 else ''
                photo_urls = self._parse_sexoptovik_photos(external_id, photo_codes_raw)

                # Баркоды
                barcodes_raw = row[14].strip() if len(row) > 14 else ''
                barcodes = [b.strip() for b in barcodes_raw.split('#') if b.strip()]

                # Материалы
                materials_raw = row[15].strip() if len(row) > 15 else ''
                materials = [m.strip() for m in materials_raw.split(',') if m.strip()]

                products.append({
                    'external_id': external_id,
                    'vendor_code': vendor_code,
                    'title': title,
                    'category': main_category,
                    'all_categories': categories,
                    'brand': brand,
                    'country': country,
                    'gender': gender,
                    'colors': colors,
                    'sizes_raw': sizes_raw,
                    'photo_urls': photo_urls,
                    'barcodes': barcodes,
                    'materials': materials,
                    'description': '',
                })

            except Exception as e:
                logger.error(f"Ошибка парсинга строки {row_num}: {e}")
                continue

        logger.info(f"Распарсено {len(products)} товаров из CSV ({self.supplier.code})")
        return products

    def _parse_sexoptovik_photos(self, product_id: str, photo_codes: str) -> List[Dict]:
        """Формирует URL фотографий для sexoptovik"""
        if not photo_codes or not product_id:
            return []

        photos = []
        match = re.search(r'id-(\d+)', product_id)
        numeric_id = match.group(1) if match else product_id

        if ',' in photo_codes:
            photo_nums = [p.strip() for p in photo_codes.split(',') if p.strip()]
        else:
            photo_nums = [p.strip() for p in photo_codes.split() if p.strip()]

        for num in photo_nums:
            photos.append({
                'sexoptovik': f"https://sexoptovik.ru/admin/_project/user_images/prods_res/{numeric_id}/{numeric_id}_{num}_1200.jpg",
                'blur': f"https://x-story.ru/mp/_project/img_sx0_1200/{numeric_id}_{num}_1200.jpg",
                'original': f"https://x-story.ru/mp/_project/img_sx_1200/{numeric_id}_{num}_1200.jpg",
            })

        return photos

    def _parse_generic(self, csv_content: str) -> List[Dict]:
        """Generic парсинг CSV (заголовки в первой строке)"""
        products = []
        reader = csv.DictReader(StringIO(csv_content), delimiter=self.delimiter)

        for row_num, row in enumerate(reader, 1):
            try:
                # Пытаемся найти поля по типовым названиям
                external_id = (row.get('id') or row.get('ID') or
                               row.get('id товара') or row.get('Артикул') or
                               str(row_num))
                title = (row.get('title') or row.get('Название') or
                         row.get('name') or row.get('наименование') or '')
                brand = row.get('brand') or row.get('Бренд') or ''
                category = row.get('category') or row.get('Категория') or ''
                price_str = (row.get('price') or row.get('Цена') or
                             row.get('цена') or row.get('цена, руб.') or '0')

                try:
                    price = float(str(price_str).replace(',', '.').replace(' ', ''))
                except (ValueError, TypeError):
                    price = None

                if not title:
                    continue

                products.append({
                    'external_id': str(external_id).strip(),
                    'vendor_code': row.get('vendor_code') or row.get('Артикул поставщика') or '',
                    'title': title.strip(),
                    'category': category.strip(),
                    'all_categories': [category.strip()] if category else [],
                    'brand': brand.strip(),
                    'country': row.get('country') or row.get('Страна') or '',
                    'gender': '',
                    'colors': [],
                    'sizes_raw': '',
                    'photo_urls': [],
                    'barcodes': [],
                    'materials': [],
                    'description': row.get('description') or row.get('Описание') or '',
                    'supplier_price': price,
                })

            except Exception as e:
                logger.error(f"Generic парсинг строка {row_num}: {e}")
                continue

        logger.info(f"Generic парсинг: {len(products)} товаров")
        return products


# ============================================================================
# SUPPLIER SERVICE
# ============================================================================

class SupplierService:
    """Основной сервисный класс для работы с поставщиками"""

    # -----------------------------------------------------------------------
    # CRUD поставщиков
    # -----------------------------------------------------------------------

    @staticmethod
    def create_supplier(data: dict, created_by_user_id: int = None) -> Supplier:
        """Создать нового поставщика"""
        supplier = Supplier(
            name=data['name'],
            code=data['code'],
            description=data.get('description'),
            website=data.get('website'),
            csv_source_url=data.get('csv_source_url'),
            csv_delimiter=data.get('csv_delimiter', ';'),
            csv_encoding=data.get('csv_encoding', 'cp1251'),
            auth_login=data.get('auth_login'),
            ai_enabled=data.get('ai_enabled', False),
            ai_provider=data.get('ai_provider', 'openai'),
            ai_api_base_url=data.get('ai_api_base_url'),
            ai_model=data.get('ai_model', 'gpt-4o-mini'),
            ai_temperature=data.get('ai_temperature', 0.3),
            ai_max_tokens=data.get('ai_max_tokens', 2000),
            ai_timeout=data.get('ai_timeout', 60),
            resize_images=data.get('resize_images', True),
            image_target_size=data.get('image_target_size', 1200),
            image_background_color=data.get('image_background_color', 'white'),
            default_markup_percent=data.get('default_markup_percent'),
            is_active=data.get('is_active', True),
            created_by_user_id=created_by_user_id,
        )

        # Зашифрованные поля через setter
        if data.get('auth_password'):
            supplier.auth_password = data['auth_password']
        if data.get('ai_api_key'):
            supplier.ai_api_key = data['ai_api_key']

        # AI кастомные инструкции
        for field_name in ('ai_category_instruction', 'ai_size_instruction',
                           'ai_seo_title_instruction', 'ai_keywords_instruction',
                           'ai_description_instruction', 'ai_analysis_instruction'):
            if data.get(field_name):
                setattr(supplier, field_name, data[field_name])

        db.session.add(supplier)
        db.session.commit()
        logger.info(f"Создан поставщик: {supplier.code} (id={supplier.id})")
        return supplier

    @staticmethod
    def update_supplier(supplier_id: int, data: dict) -> Optional[Supplier]:
        """Обновить данные поставщика"""
        supplier = Supplier.query.get(supplier_id)
        if not supplier:
            return None

        # Обычные поля
        simple_fields = [
            'name', 'description', 'website', 'csv_source_url', 'csv_delimiter',
            'csv_encoding', 'api_endpoint', 'auth_login', 'ai_enabled', 'ai_provider',
            'ai_api_base_url', 'ai_model', 'ai_temperature', 'ai_max_tokens',
            'ai_timeout', 'ai_client_id', 'ai_client_secret',
            'resize_images', 'image_target_size', 'image_background_color',
            'default_markup_percent', 'is_active',
            'ai_category_instruction', 'ai_size_instruction',
            'ai_seo_title_instruction', 'ai_keywords_instruction',
            'ai_description_instruction', 'ai_analysis_instruction',
        ]
        for f in simple_fields:
            if f in data:
                setattr(supplier, f, data[f])

        # Зашифрованные поля
        if 'auth_password' in data and data['auth_password']:
            supplier.auth_password = data['auth_password']
        if 'ai_api_key' in data and data['ai_api_key']:
            supplier.ai_api_key = data['ai_api_key']

        db.session.commit()
        logger.info(f"Обновлён поставщик: {supplier.code} (id={supplier.id})")
        return supplier

    @staticmethod
    def delete_supplier(supplier_id: int) -> bool:
        """Удалить поставщика (и все его товары)"""
        supplier = Supplier.query.get(supplier_id)
        if not supplier:
            return False

        db.session.delete(supplier)
        db.session.commit()
        logger.info(f"Удалён поставщик: {supplier.code} (id={supplier_id})")
        return True

    @staticmethod
    def get_supplier(supplier_id: int) -> Optional[Supplier]:
        """Получить поставщика по ID"""
        return Supplier.query.get(supplier_id)

    @staticmethod
    def get_supplier_by_code(code: str) -> Optional[Supplier]:
        """Получить поставщика по коду"""
        return Supplier.query.filter_by(code=code).first()

    @staticmethod
    def list_suppliers(active_only: bool = False) -> List[Supplier]:
        """Список всех поставщиков"""
        q = Supplier.query
        if active_only:
            q = q.filter_by(is_active=True)
        return q.order_by(Supplier.name).all()

    # -----------------------------------------------------------------------
    # Синхронизация каталога из CSV
    # -----------------------------------------------------------------------

    @staticmethod
    def sync_from_csv(supplier_id: int, price_data: Dict[str, float] = None) -> SyncResult:
        """
        Синхронизация каталога поставщика из CSV.
        Скачивает CSV, парсит, создаёт/обновляет SupplierProduct.
        """
        result = SyncResult()
        start_time = time.time()

        supplier = Supplier.query.get(supplier_id)
        if not supplier:
            result.success = False
            result.error_messages.append("Поставщик не найден")
            return result

        supplier.last_sync_status = 'running'
        db.session.commit()

        try:
            # Скачиваем и парсим CSV
            parser = SupplierCSVParser(supplier)
            csv_content = parser.fetch_csv()
            if not csv_content:
                result.success = False
                result.error_messages.append("Не удалось скачать CSV")
                supplier.last_sync_status = 'failed'
                supplier.last_sync_error = "Не удалось скачать CSV"
                db.session.commit()
                return result

            parsed_products = parser.parse(csv_content)
            result.total_in_csv = len(parsed_products)

            if not parsed_products:
                result.error_messages.append("CSV пустой или не удалось распарсить")
                supplier.last_sync_status = 'failed'
                supplier.last_sync_error = "CSV пустой"
                db.session.commit()
                return result

            # Загружаем существующие товары поставщика
            existing_map = {}
            for sp in SupplierProduct.query.filter_by(supplier_id=supplier_id).all():
                if sp.external_id:
                    existing_map[sp.external_id] = sp

            # Обрабатываем каждый товар
            batch_count = 0
            for pd in parsed_products:
                try:
                    ext_id = pd['external_id']
                    existing = existing_map.get(ext_id)

                    if existing:
                        # Обновляем существующий
                        _update_supplier_product(existing, pd, price_data)
                        result.updated += 1
                    else:
                        # Создаём новый
                        sp = _create_supplier_product(supplier_id, pd, price_data)
                        db.session.add(sp)
                        result.added += 1

                    batch_count += 1
                    if batch_count % 100 == 0:
                        db.session.flush()

                except Exception as e:
                    result.errors += 1
                    result.error_messages.append(f"Товар {pd.get('external_id', '?')}: {str(e)[:100]}")
                    if len(result.error_messages) > 50:
                        result.error_messages.append("...и другие ошибки")
                        break

            db.session.commit()

            # Обновляем статистику поставщика
            supplier.total_products = SupplierProduct.query.filter_by(supplier_id=supplier_id).count()
            supplier.last_sync_at = datetime.utcnow()
            supplier.last_sync_status = 'success'
            supplier.last_sync_error = None
            result.duration_seconds = time.time() - start_time
            supplier.last_sync_duration = result.duration_seconds
            db.session.commit()

            logger.info(
                f"Синхронизация {supplier.code}: "
                f"+{result.added} / ~{result.updated} / err={result.errors} "
                f"({result.duration_seconds:.1f}s)"
            )

            # Запускаем фоновое скачивание всех фото поставщика
            try:
                from services.photo_cache import bulk_download_supplier_photos
                dl_result = bulk_download_supplier_photos(supplier_id)
                logger.info(
                    f"Фото {supplier.code}: "
                    f"всего={dl_result['total_photos']}, "
                    f"в кэше={dl_result['already_cached']}, "
                    f"в очереди={dl_result['queued']}"
                )
            except Exception as e:
                logger.warning(f"Ошибка запуска скачивания фото: {e}")

        except Exception as e:
            db.session.rollback()
            result.success = False
            result.error_messages.append(f"Критическая ошибка: {str(e)}")
            supplier.last_sync_status = 'failed'
            supplier.last_sync_error = str(e)[:500]
            db.session.commit()
            logger.error(f"Ошибка синхронизации {supplier.code}: {e}")

        result.duration_seconds = time.time() - start_time
        return result

    # -----------------------------------------------------------------------
    # Управление товарами
    # -----------------------------------------------------------------------

    @staticmethod
    def get_products(supplier_id: int, page: int = 1, per_page: int = 50,
                     search: str = None, status: str = None,
                     category: str = None, brand: str = None,
                     ai_validated: bool = None, has_photos: bool = None,
                     sort_by: str = 'created_at', sort_dir: str = 'desc'):
        """Получить товары поставщика с фильтрацией и пагинацией"""
        q = SupplierProduct.query.filter_by(supplier_id=supplier_id)

        # Фильтры
        if search:
            search_term = f"%{search}%"
            q = q.filter(
                db.or_(
                    SupplierProduct.title.ilike(search_term),
                    SupplierProduct.external_id.ilike(search_term),
                    SupplierProduct.vendor_code.ilike(search_term),
                    SupplierProduct.brand.ilike(search_term),
                )
            )
        if status:
            q = q.filter(SupplierProduct.status == status)
        if category:
            q = q.filter(SupplierProduct.category.ilike(f"%{category}%"))
        if brand:
            q = q.filter(SupplierProduct.brand == brand)
        if ai_validated is not None:
            q = q.filter(SupplierProduct.ai_validated == ai_validated)
        if has_photos is True:
            q = q.filter(SupplierProduct.photo_urls_json.isnot(None))
            q = q.filter(SupplierProduct.photo_urls_json != '[]')
        elif has_photos is False:
            q = q.filter(
                db.or_(
                    SupplierProduct.photo_urls_json.is_(None),
                    SupplierProduct.photo_urls_json == '[]'
                )
            )

        # Сортировка
        sort_column = getattr(SupplierProduct, sort_by, SupplierProduct.created_at)
        if sort_dir == 'asc':
            q = q.order_by(sort_column.asc())
        else:
            q = q.order_by(sort_column.desc())

        return q.paginate(page=page, per_page=per_page, error_out=False)

    @staticmethod
    def get_product(product_id: int) -> Optional[SupplierProduct]:
        """Получить конкретный товар"""
        return SupplierProduct.query.get(product_id)

    @staticmethod
    def update_product(product_id: int, data: dict) -> Optional[SupplierProduct]:
        """Обновить товар поставщика"""
        product = SupplierProduct.query.get(product_id)
        if not product:
            return None

        updatable = [
            'title', 'description', 'brand', 'category', 'vendor_code',
            'wb_category_name', 'wb_subject_id', 'wb_subject_name',
            'supplier_price', 'supplier_quantity', 'gender', 'country',
            'season', 'age_group', 'status',
        ]
        for f in updatable:
            if f in data:
                setattr(product, f, data[f])

        # JSON поля
        json_fields = [
            'characteristics_json', 'sizes_json', 'colors_json',
            'materials_json', 'dimensions_json', 'photo_urls_json',
            'ai_keywords_json', 'ai_bullets_json',
        ]
        for f in json_fields:
            if f in data:
                val = data[f]
                if isinstance(val, (list, dict)):
                    setattr(product, f, json.dumps(val, ensure_ascii=False))
                else:
                    setattr(product, f, val)

        # Текстовые AI поля
        if 'ai_seo_title' in data:
            product.ai_seo_title = data['ai_seo_title']
        if 'ai_description' in data:
            product.ai_description = data['ai_description']

        db.session.commit()
        return product

    @staticmethod
    def delete_products(product_ids: List[int]) -> int:
        """Удалить товары по ID"""
        count = SupplierProduct.query.filter(SupplierProduct.id.in_(product_ids)).delete(
            synchronize_session=False
        )
        db.session.commit()
        return count

    @staticmethod
    def get_product_stats(supplier_id: int) -> dict:
        """Статистика по товарам поставщика"""
        base = SupplierProduct.query.filter_by(supplier_id=supplier_id)
        total = base.count()
        return {
            'total': total,
            'draft': base.filter_by(status='draft').count(),
            'validated': base.filter_by(status='validated').count(),
            'ready': base.filter_by(status='ready').count(),
            'archived': base.filter_by(status='archived').count(),
            'ai_validated': base.filter_by(ai_validated=True).count(),
            'with_photos': base.filter(
                SupplierProduct.photo_urls_json.isnot(None),
                SupplierProduct.photo_urls_json != '[]'
            ).count(),
            'brands': db.session.query(SupplierProduct.brand).filter(
                SupplierProduct.supplier_id == supplier_id,
                SupplierProduct.brand.isnot(None),
                SupplierProduct.brand != ''
            ).distinct().count(),
            'categories': db.session.query(SupplierProduct.category).filter(
                SupplierProduct.supplier_id == supplier_id,
                SupplierProduct.category.isnot(None),
                SupplierProduct.category != ''
            ).distinct().count(),
        }

    # -----------------------------------------------------------------------
    # Подключение продавцов
    # -----------------------------------------------------------------------

    @staticmethod
    def connect_seller(seller_id: int, supplier_id: int,
                       supplier_code: str = None,
                       vendor_code_pattern: str = None) -> SellerSupplier:
        """Подключить продавца к поставщику"""
        existing = SellerSupplier.query.filter_by(
            seller_id=seller_id, supplier_id=supplier_id
        ).first()

        if existing:
            existing.is_active = True
            if supplier_code:
                existing.supplier_code = supplier_code
            if vendor_code_pattern:
                existing.vendor_code_pattern = vendor_code_pattern
            db.session.commit()
            return existing

        conn = SellerSupplier(
            seller_id=seller_id,
            supplier_id=supplier_id,
            supplier_code=supplier_code,
            vendor_code_pattern=vendor_code_pattern or 'id-{product_id}-{supplier_code}',
        )
        db.session.add(conn)
        db.session.commit()
        logger.info(f"Продавец {seller_id} подключён к поставщику {supplier_id}")
        return conn

    @staticmethod
    def disconnect_seller(seller_id: int, supplier_id: int) -> bool:
        """Отключить продавца от поставщика"""
        conn = SellerSupplier.query.filter_by(
            seller_id=seller_id, supplier_id=supplier_id
        ).first()
        if not conn:
            return False
        conn.is_active = False
        db.session.commit()
        logger.info(f"Продавец {seller_id} отключён от поставщика {supplier_id}")
        return True

    @staticmethod
    def get_seller_suppliers(seller_id: int, active_only: bool = True) -> List[SellerSupplier]:
        """Получить список поставщиков продавца"""
        q = SellerSupplier.query.filter_by(seller_id=seller_id)
        if active_only:
            q = q.filter_by(is_active=True)
        return q.all()

    @staticmethod
    def get_supplier_sellers(supplier_id: int, active_only: bool = True) -> List[SellerSupplier]:
        """Получить список продавцов поставщика"""
        q = SellerSupplier.query.filter_by(supplier_id=supplier_id)
        if active_only:
            q = q.filter_by(is_active=True)
        return q.all()

    # -----------------------------------------------------------------------
    # Импорт товаров к продавцу
    # -----------------------------------------------------------------------

    @staticmethod
    def import_to_seller(seller_id: int, supplier_product_ids: List[int]) -> ImportResult:
        """
        Импортировать товары из базы поставщика к продавцу.
        Копирует данные в ImportedProduct и сохраняет связь с SupplierProduct.
        """
        result = ImportResult(total_requested=len(supplier_product_ids))

        seller = Seller.query.get(seller_id)
        if not seller:
            result.success = False
            result.error_messages.append("Продавец не найден")
            return result

        # Получаем товары поставщика
        supplier_products = SupplierProduct.query.filter(
            SupplierProduct.id.in_(supplier_product_ids)
        ).all()

        if not supplier_products:
            result.error_messages.append("Товары не найдены")
            return result

        # Проверяем, что продавец подключён к поставщику
        supplier_ids = set(sp.supplier_id for sp in supplier_products)
        for sup_id in supplier_ids:
            conn = SellerSupplier.query.filter_by(
                seller_id=seller_id, supplier_id=sup_id, is_active=True
            ).first()
            if not conn:
                result.success = False
                result.error_messages.append(f"Продавец не подключён к поставщику id={sup_id}")
                return result

        # Проверяем дубликаты
        existing_sp_ids = set()
        existing_imports = ImportedProduct.query.filter(
            ImportedProduct.seller_id == seller_id,
            ImportedProduct.supplier_product_id.in_(supplier_product_ids)
        ).all()
        for imp in existing_imports:
            existing_sp_ids.add(imp.supplier_product_id)

        # Импортируем
        for sp in supplier_products:
            try:
                if sp.id in existing_sp_ids:
                    result.skipped += 1
                    continue

                imported = _copy_to_imported_product(seller_id, sp)
                db.session.add(imported)
                result.imported += 1
                result.imported_product_ids.append(imported.id if imported.id else 0)

            except Exception as e:
                result.errors += 1
                result.error_messages.append(f"Товар {sp.external_id}: {str(e)[:100]}")

        # Обновляем статистику подключения
        db.session.flush()
        for sup_id in supplier_ids:
            conn = SellerSupplier.query.filter_by(
                seller_id=seller_id, supplier_id=sup_id
            ).first()
            if conn:
                conn.products_imported = ImportedProduct.query.filter_by(
                    seller_id=seller_id, supplier_id=sup_id
                ).count()
                conn.last_import_at = datetime.utcnow()

        db.session.commit()

        logger.info(
            f"Импорт к продавцу {seller_id}: "
            f"+{result.imported} / skip={result.skipped} / err={result.errors}"
        )
        return result

    @staticmethod
    def update_seller_products(seller_id: int,
                               supplier_product_ids: List[int] = None) -> ImportResult:
        """
        Обновить данные у продавца из базы поставщика.
        Обновляет ImportedProduct по связанному SupplierProduct.
        """
        result = ImportResult()

        q = ImportedProduct.query.filter(
            ImportedProduct.seller_id == seller_id,
            ImportedProduct.supplier_product_id.isnot(None)
        )
        if supplier_product_ids:
            q = q.filter(ImportedProduct.supplier_product_id.in_(supplier_product_ids))

        imported_products = q.all()
        result.total_requested = len(imported_products)

        for imp in imported_products:
            try:
                sp = imp.supplier_product
                if not sp:
                    result.skipped += 1
                    continue

                _update_imported_from_supplier(imp, sp)
                result.imported += 1

            except Exception as e:
                result.errors += 1
                result.error_messages.append(f"ImportedProduct {imp.id}: {str(e)[:100]}")

        db.session.commit()
        logger.info(f"Обновление продавца {seller_id}: ~{result.imported} / err={result.errors}")
        return result

    @staticmethod
    def get_available_products_for_seller(seller_id: int, supplier_id: int,
                                          page: int = 1, per_page: int = 50,
                                          search: str = None,
                                          show_imported: bool = False):
        """Товары поставщика, доступные для импорта продавцу"""
        q = SupplierProduct.query.filter_by(supplier_id=supplier_id)
        q = q.filter(SupplierProduct.status.in_(['validated', 'ready']))

        if not show_imported:
            # Исключаем уже импортированные
            imported_sp_ids = db.session.query(ImportedProduct.supplier_product_id).filter(
                ImportedProduct.seller_id == seller_id,
                ImportedProduct.supplier_product_id.isnot(None)
            ).subquery()
            q = q.filter(~SupplierProduct.id.in_(imported_sp_ids))

        if search:
            search_term = f"%{search}%"
            q = q.filter(
                db.or_(
                    SupplierProduct.title.ilike(search_term),
                    SupplierProduct.external_id.ilike(search_term),
                    SupplierProduct.brand.ilike(search_term),
                )
            )

        return q.order_by(SupplierProduct.title).paginate(
            page=page, per_page=per_page, error_out=False
        )

    # ===================================================================
    # AI ОПЕРАЦИИ НА УРОВНЕ ПОСТАВЩИКА
    # ===================================================================

    @staticmethod
    def _get_ai_service(supplier: Supplier):
        """Создать AIService из настроек поставщика"""
        from services.ai_service import AIConfig, AIService as AISvc
        config = AIConfig.from_settings(supplier)
        if not config:
            return None
        return AISvc(config)

    @staticmethod
    def ai_validate_product(product_id: int) -> dict:
        """
        AI валидация одного товара поставщика.
        Запускает analyze_card и обновляет ai_validation_score, ai_validated.

        Returns:
            dict с ключами: success, score, issues, recommendations, error
        """
        product = SupplierProduct.query.get(product_id)
        if not product:
            return {'success': False, 'error': 'Товар не найден'}

        supplier = Supplier.query.get(product.supplier_id)
        if not supplier or not supplier.ai_enabled:
            return {'success': False, 'error': 'AI не включен для этого поставщика'}

        ai_svc = SupplierService._get_ai_service(supplier)
        if not ai_svc:
            return {'success': False, 'error': 'Не удалось создать AI сервис (проверьте API ключ)'}

        photos = product.get_photos() if hasattr(product, 'get_photos') else []

        success, result, error = ai_svc.analyze_card(
            title=product.title or '',
            description=product.description or '',
            category=product.wb_category_name or product.category or '',
            photos_count=len(photos),
            price=product.supplier_price or 0
        )

        if success and result:
            score = result.get('score', 0)
            product.ai_analysis_json = json.dumps(result, ensure_ascii=False)
            product.ai_validation_score = score
            product.ai_validated = True
            product.ai_validated_at = datetime.utcnow()
            if score >= 70:
                product.status = 'validated'
            db.session.commit()
            return {
                'success': True,
                'score': score,
                'issues': result.get('issues', []),
                'recommendations': result.get('recommendations', []),
                'strengths': result.get('strengths', []),
            }

        return {'success': False, 'error': error or 'Ошибка AI анализа'}

    @staticmethod
    def ai_validate_bulk(supplier_id: int, product_ids: List[int]) -> dict:
        """
        AI валидация нескольких товаров.

        Returns:
            dict: {success, validated, errors, results: [{product_id, score, error}]}
        """
        supplier = Supplier.query.get(supplier_id)
        if not supplier or not supplier.ai_enabled:
            return {'success': False, 'error': 'AI не включен', 'validated': 0, 'errors': 0, 'results': []}

        results = []
        validated = 0
        errors = 0

        for pid in product_ids:
            res = SupplierService.ai_validate_product(pid)
            results.append({
                'product_id': pid,
                'score': res.get('score'),
                'error': res.get('error'),
                'success': res.get('success', False),
            })
            if res.get('success'):
                validated += 1
            else:
                errors += 1

        return {
            'success': True,
            'validated': validated,
            'errors': errors,
            'results': results,
        }

    @staticmethod
    def ai_generate_seo(product_id: int) -> dict:
        """
        AI генерация SEO заголовка для товара.

        Returns:
            dict: {success, title, keywords_used, error}
        """
        product = SupplierProduct.query.get(product_id)
        if not product:
            return {'success': False, 'error': 'Товар не найден'}

        supplier = Supplier.query.get(product.supplier_id)
        if not supplier or not supplier.ai_enabled:
            return {'success': False, 'error': 'AI не включен'}

        ai_svc = SupplierService._get_ai_service(supplier)
        if not ai_svc:
            return {'success': False, 'error': 'Не удалось создать AI сервис'}

        success, result, error = ai_svc.generate_seo_title(
            title=product.title or '',
            category=product.wb_category_name or product.category or '',
            brand=product.brand or '',
            description=product.description or ''
        )

        if success and result:
            product.ai_seo_title = result.get('title', '')
            product.updated_at = datetime.utcnow()
            db.session.commit()
            return {'success': True, 'title': result.get('title', ''), 'keywords_used': result.get('keywords_used', [])}

        return {'success': False, 'error': error or 'Ошибка AI'}

    @staticmethod
    def ai_generate_description(product_id: int) -> dict:
        """
        AI генерация описания для товара.

        Returns:
            dict: {success, description, error}
        """
        product = SupplierProduct.query.get(product_id)
        if not product:
            return {'success': False, 'error': 'Товар не найден'}

        supplier = Supplier.query.get(product.supplier_id)
        if not supplier or not supplier.ai_enabled:
            return {'success': False, 'error': 'AI не включен'}

        ai_svc = SupplierService._get_ai_service(supplier)
        if not ai_svc:
            return {'success': False, 'error': 'Не удалось создать AI сервис'}

        success, result, error = ai_svc.enhance_description(
            title=product.title or '',
            description=product.description or '',
            category=product.wb_category_name or product.category or ''
        )

        if success and result:
            product.ai_description = result.get('description', '')
            product.updated_at = datetime.utcnow()
            db.session.commit()
            return {'success': True, 'description': result.get('description', '')}

        return {'success': False, 'error': error or 'Ошибка AI'}

    @staticmethod
    def ai_generate_keywords(product_id: int) -> dict:
        """
        AI генерация ключевых слов для товара.

        Returns:
            dict: {success, keywords, error}
        """
        product = SupplierProduct.query.get(product_id)
        if not product:
            return {'success': False, 'error': 'Товар не найден'}

        supplier = Supplier.query.get(product.supplier_id)
        if not supplier or not supplier.ai_enabled:
            return {'success': False, 'error': 'AI не включен'}

        ai_svc = SupplierService._get_ai_service(supplier)
        if not ai_svc:
            return {'success': False, 'error': 'Не удалось создать AI сервис'}

        success, result, error = ai_svc.generate_keywords(
            title=product.title or '',
            category=product.wb_category_name or product.category or '',
            description=product.description or ''
        )

        if success and result:
            product.ai_keywords_json = json.dumps(result.get('keywords', []), ensure_ascii=False)
            product.updated_at = datetime.utcnow()
            db.session.commit()
            return {'success': True, 'keywords': result.get('keywords', [])}

        return {'success': False, 'error': error or 'Ошибка AI'}

    @staticmethod
    def ai_analyze_product(product_id: int) -> dict:
        """
        Комплексный AI анализ товара (без изменения статуса валидации).

        Returns:
            dict: {success, score, issues, recommendations, strengths, error}
        """
        product = SupplierProduct.query.get(product_id)
        if not product:
            return {'success': False, 'error': 'Товар не найден'}

        supplier = Supplier.query.get(product.supplier_id)
        if not supplier or not supplier.ai_enabled:
            return {'success': False, 'error': 'AI не включен'}

        ai_svc = SupplierService._get_ai_service(supplier)
        if not ai_svc:
            return {'success': False, 'error': 'Не удалось создать AI сервис'}

        photos = product.get_photos() if hasattr(product, 'get_photos') else []

        success, result, error = ai_svc.analyze_card(
            title=product.title or '',
            description=product.description or '',
            category=product.wb_category_name or product.category or '',
            photos_count=len(photos),
            price=product.supplier_price or 0
        )

        if success and result:
            product.ai_analysis_json = json.dumps(result, ensure_ascii=False)
            product.updated_at = datetime.utcnow()
            db.session.commit()
            return {
                'success': True,
                'score': result.get('score', 0),
                'issues': result.get('issues', []),
                'recommendations': result.get('recommendations', []),
                'strengths': result.get('strengths', []),
            }

        return {'success': False, 'error': error or 'Ошибка AI'}

    @staticmethod
    def ai_full_enrich(product_id: int) -> dict:
        """
        Полное AI обогащение товара: SEO + описание + ключевые слова + анализ.

        Returns:
            dict: {success, seo_title, description, keywords, score, errors}
        """
        errors = []
        result_data = {}

        seo_res = SupplierService.ai_generate_seo(product_id)
        if seo_res.get('success'):
            result_data['seo_title'] = seo_res.get('title', '')
        else:
            errors.append(f"SEO: {seo_res.get('error', '?')}")

        desc_res = SupplierService.ai_generate_description(product_id)
        if desc_res.get('success'):
            result_data['description'] = desc_res.get('description', '')
        else:
            errors.append(f"Описание: {desc_res.get('error', '?')}")

        kw_res = SupplierService.ai_generate_keywords(product_id)
        if kw_res.get('success'):
            result_data['keywords'] = kw_res.get('keywords', [])
        else:
            errors.append(f"Ключевые слова: {kw_res.get('error', '?')}")

        val_res = SupplierService.ai_validate_product(product_id)
        if val_res.get('success'):
            result_data['score'] = val_res.get('score', 0)
        else:
            errors.append(f"Анализ: {val_res.get('error', '?')}")

        return {
            'success': len(errors) == 0,
            'errors': errors,
            **result_data,
        }


# ============================================================================
# PRIVATE HELPERS
# ============================================================================

def _create_supplier_product(supplier_id: int, data: dict,
                             price_data: Dict[str, float] = None) -> SupplierProduct:
    """Создать SupplierProduct из парсерных данных"""
    sp = SupplierProduct(supplier_id=supplier_id)
    _update_supplier_product(sp, data, price_data)
    return sp


def _update_supplier_product(sp: SupplierProduct, data: dict,
                             price_data: Dict[str, float] = None) -> None:
    """Обновить SupplierProduct из парсерных данных"""
    sp.external_id = data.get('external_id', sp.external_id)
    sp.vendor_code = data.get('vendor_code', sp.vendor_code)
    sp.title = data.get('title', sp.title)
    sp.description = data.get('description', sp.description)
    sp.brand = data.get('brand', sp.brand)
    sp.category = data.get('category', sp.category)
    sp.country = data.get('country', sp.country)
    sp.gender = data.get('gender', sp.gender)

    # JSON поля
    if 'all_categories' in data:
        sp.all_categories = json.dumps(data['all_categories'], ensure_ascii=False)
    if 'colors' in data:
        sp.colors_json = json.dumps(data['colors'], ensure_ascii=False)
    if 'materials' in data:
        sp.materials_json = json.dumps(data['materials'], ensure_ascii=False)
    if 'barcodes' in data:
        sp.barcode = data['barcodes'][0] if data['barcodes'] else sp.barcode
    if 'photo_urls' in data:
        sp.photo_urls_json = json.dumps(data['photo_urls'], ensure_ascii=False)
    if 'sizes_raw' in data and data['sizes_raw']:
        sp.sizes_json = json.dumps({'raw': data['sizes_raw']}, ensure_ascii=False)

    # Оригинальные данные (для отката)
    if not sp.original_data_json:
        sp.original_data_json = json.dumps(data, ensure_ascii=False, default=str)

    # Цена из price_data (если есть)
    if price_data and sp.external_id:
        ext_id = sp.external_id
        # Пробуем найти цену по числовому ID
        match = re.search(r'(\d+)', ext_id)
        if match:
            numeric_id = match.group(1)
            price = price_data.get(numeric_id) or price_data.get(ext_id)
            if price:
                sp.supplier_price = price

    if 'supplier_price' in data and data['supplier_price']:
        sp.supplier_price = data['supplier_price']

    # Контент хеш для отслеживания изменений
    content_str = f"{sp.title}|{sp.brand}|{sp.category}|{sp.supplier_price}"
    sp.content_hash = hashlib.md5(content_str.encode()).hexdigest()


def _copy_to_imported_product(seller_id: int, sp: SupplierProduct) -> ImportedProduct:
    """Копировать данные из SupplierProduct в новый ImportedProduct"""
    imp = ImportedProduct(
        seller_id=seller_id,
        supplier_product_id=sp.id,
        supplier_id=sp.supplier_id,
        external_id=sp.external_id,
        external_vendor_code=sp.vendor_code,
        source_type=sp.supplier.code if sp.supplier else 'unknown',
        title=sp.title,
        description=sp.description or sp.ai_description or '',
        brand=sp.brand,
        category=sp.category,
        all_categories=sp.all_categories,
        mapped_wb_category=sp.wb_category_name,
        wb_subject_id=sp.wb_subject_id,
        category_confidence=sp.category_confidence or 0.0,
        country=sp.country,
        gender=sp.gender,
        colors=sp.colors_json,
        sizes=sp.sizes_json,
        materials=sp.materials_json,
        photo_urls=sp.photo_urls_json,
        processed_photos=sp.processed_photos_json,
        barcodes=json.dumps([sp.barcode], ensure_ascii=False) if sp.barcode else None,
        characteristics=sp.characteristics_json,
        supplier_price=sp.supplier_price,
        supplier_quantity=sp.supplier_quantity,
        import_status='pending',
        original_data=sp.original_data_json,
    )

    # Копируем AI данные если есть
    if sp.ai_seo_title:
        imp.ai_seo_title = sp.ai_seo_title
    if sp.ai_keywords_json:
        imp.ai_keywords = sp.ai_keywords_json
    if sp.ai_bullets_json:
        imp.ai_bullets = sp.ai_bullets_json
    if sp.ai_rich_content_json:
        imp.ai_rich_content = sp.ai_rich_content_json
    if sp.ai_analysis_json:
        imp.ai_analysis = sp.ai_analysis_json
        imp.ai_analysis_at = sp.ai_validated_at

    return imp


def _update_imported_from_supplier(imp: ImportedProduct, sp: SupplierProduct) -> None:
    """Обновить ImportedProduct из SupplierProduct (sync)"""
    imp.title = sp.title or imp.title
    imp.brand = sp.brand or imp.brand
    imp.category = sp.category or imp.category
    imp.all_categories = sp.all_categories or imp.all_categories
    imp.mapped_wb_category = sp.wb_category_name or imp.mapped_wb_category
    imp.wb_subject_id = sp.wb_subject_id or imp.wb_subject_id
    imp.category_confidence = sp.category_confidence or imp.category_confidence
    imp.country = sp.country or imp.country
    imp.gender = sp.gender or imp.gender
    imp.colors = sp.colors_json or imp.colors
    imp.sizes = sp.sizes_json or imp.sizes
    imp.materials = sp.materials_json or imp.materials
    imp.supplier_price = sp.supplier_price if sp.supplier_price is not None else imp.supplier_price
    imp.supplier_quantity = sp.supplier_quantity if sp.supplier_quantity is not None else imp.supplier_quantity

    # Обновляем фото (общие для всех продавцов)
    if sp.photo_urls_json:
        imp.photo_urls = sp.photo_urls_json
    if sp.processed_photos_json:
        imp.processed_photos = sp.processed_photos_json

    # AI данные — только если продавец не генерировал свои
    if sp.ai_seo_title and not imp.ai_seo_title:
        imp.ai_seo_title = sp.ai_seo_title
    if sp.ai_keywords_json and not imp.ai_keywords:
        imp.ai_keywords = sp.ai_keywords_json
    if sp.ai_bullets_json and not imp.ai_bullets:
        imp.ai_bullets = sp.ai_bullets_json

    imp.updated_at = datetime.utcnow()
