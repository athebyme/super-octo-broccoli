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
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def _get_marketplace_categories_block(supplier_id: int) -> str:
    """
    Получить текстовый блок включённых категорий маркетплейса для AI промпта.
    Если у поставщика нет активных подключений к маркетплейсам — возвращает ''.
    """
    try:
        from models import MarketplaceConnection
        from services.marketplace_service import MarketplaceService

        conn = MarketplaceConnection.query.filter_by(
            supplier_id=supplier_id,
            is_active=True
        ).first()
        if not conn:
            return ""

        return MarketplaceService.get_enabled_categories_for_prompt(conn.marketplace_id)
    except Exception as e:
        logger.debug(f"Could not load marketplace categories for supplier {supplier_id}: {e}")
        return ""


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
            price_file_url=data.get('price_file_url'),
            price_file_inf_url=data.get('price_file_inf_url'),
            price_file_delimiter=data.get('price_file_delimiter', ';'),
            price_file_encoding=data.get('price_file_encoding', 'cp1251'),
            auto_sync_prices=data.get('auto_sync_prices', False),
            auto_sync_interval_minutes=data.get('auto_sync_interval_minutes', 60),
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
            'price_file_url', 'price_file_inf_url', 'price_file_delimiter',
            'price_file_encoding', 'auto_sync_prices', 'auto_sync_interval_minutes',
            'ai_category_instruction', 'ai_size_instruction',
            'ai_seo_title_instruction', 'ai_keywords_instruction',
            'ai_description_instruction', 'ai_analysis_instruction',
            'ai_parsing_instruction',
            'description_file_url', 'description_file_delimiter', 'description_file_encoding',
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

            # Автоматическая синхронизация цен/остатков после каталога
            if supplier.price_file_url:
                try:
                    price_result = SupplierService.sync_prices_and_stock(supplier_id, force=True)
                    if price_result.success:
                        logger.info(
                            f"Авто-синхр цен {supplier.code}: обновлено={price_result.updated}"
                        )
                        # Каскадное обновление к продавцам
                        if price_result.updated > 0:
                            SupplierService.cascade_prices_to_sellers(supplier_id)
                except Exception as e:
                    logger.warning(f"Ошибка авто-синхр цен после каталога: {e}")

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
    # Синхронизация цен и остатков
    # -----------------------------------------------------------------------

    @staticmethod
    def sync_prices_and_stock(supplier_id: int, force: bool = False) -> SyncResult:
        """
        Синхронизация цен и остатков из отдельного CSV файла поставщика.

        CSV формат (sexoptovik): id;осн.артикул;цена;наличие;статус;доп.артикул;штрихкод;ррц

        Обновляет: supplier_price, supplier_quantity, supplier_status,
        recommended_retail_price, barcode, vendor_code, additional_vendor_code.
        Сохраняет previous_price для трекинга изменений.
        """
        result = SyncResult()
        start_time = time.time()

        supplier = Supplier.query.get(supplier_id)
        if not supplier:
            result.success = False
            result.error_messages.append("Поставщик не найден")
            return result

        if not supplier.price_file_url:
            result.success = False
            result.error_messages.append("URL файла цен не задан")
            return result

        supplier.last_price_sync_status = 'running'
        db.session.commit()

        try:
            # Проверяем обновление через INF файл (если не force)
            if not force and supplier.price_file_inf_url:
                try:
                    inf_resp = requests.get(supplier.price_file_inf_url, timeout=30)
                    inf_resp.raise_for_status()
                    import hashlib as _hl
                    new_hash = _hl.md5(inf_resp.content).hexdigest()
                    if new_hash == supplier.last_price_file_hash:
                        result.success = True
                        result.error_messages.append("Файл не изменился с последней синхронизации")
                        supplier.last_price_sync_status = 'success'
                        db.session.commit()
                        result.duration_seconds = time.time() - start_time
                        return result
                except Exception as e:
                    logger.warning(f"Не удалось проверить INF файл: {e}")

            # Загружаем CSV цен
            encoding = supplier.price_file_encoding or 'cp1251'
            delimiter = supplier.price_file_delimiter or ';'

            try:
                resp = requests.get(supplier.price_file_url, timeout=120)
                resp.raise_for_status()
            except Exception as e:
                result.success = False
                result.error_messages.append(f"Ошибка загрузки файла цен: {str(e)[:200]}")
                supplier.last_price_sync_status = 'failed'
                supplier.last_price_sync_error = str(e)[:500]
                db.session.commit()
                return result

            text = resp.content.decode(encoding, errors='replace')

            # Парсим CSV
            price_data = {}
            reader = csv.reader(StringIO(text), delimiter=delimiter)
            header_skipped = False

            for row in reader:
                if len(row) < 4:
                    continue

                raw_id = row[0].strip()

                # Пропускаем заголовок
                if not header_skipped:
                    try:
                        int(raw_id)
                    except ValueError:
                        header_skipped = True
                        continue
                    header_skipped = True

                try:
                    product_id = int(raw_id)
                except ValueError:
                    continue

                try:
                    price = float(row[2].strip().replace(',', '.')) if row[2].strip() else 0
                except (ValueError, IndexError):
                    price = 0

                try:
                    quantity = int(row[3].strip()) if len(row) > 3 and row[3].strip() else 0
                except (ValueError, IndexError):
                    quantity = 0

                # Статус поставщика (колонка 4)
                sup_status_raw = row[4].strip() if len(row) > 4 else ''

                # Доп. артикул (колонка 5)
                add_vendor = row[5].strip() if len(row) > 5 else ''

                # Штрихкод (колонка 6)
                barcode = row[6].strip() if len(row) > 6 else ''

                # РРЦ (колонка 7)
                try:
                    rrp = float(row[7].strip().replace(',', '.')) if len(row) > 7 and row[7].strip() else None
                except (ValueError, IndexError):
                    rrp = None

                price_data[product_id] = {
                    'vendor_code': row[1].strip() if len(row) > 1 else '',
                    'price': price,
                    'quantity': quantity,
                    'status': sup_status_raw,
                    'additional_vendor_code': add_vendor,
                    'barcode': barcode,
                    'rrp': rrp,
                }

            result.total_in_csv = len(price_data)
            logger.info(f"Загружено {len(price_data)} записей цен из CSV ({supplier.code})")

            if not price_data:
                result.error_messages.append("Файл цен пустой или не удалось распарсить")
                supplier.last_price_sync_status = 'failed'
                supplier.last_price_sync_error = "Файл цен пустой"
                db.session.commit()
                return result

            # Обновляем товары
            products = SupplierProduct.query.filter_by(supplier_id=supplier_id).all()
            now = datetime.utcnow()

            batch_count = 0
            for sp in products:
                try:
                    # Извлекаем числовой ID из external_id
                    numeric_id = None
                    if sp.external_id:
                        match = re.search(r'(\d+)', sp.external_id)
                        if match:
                            numeric_id = int(match.group(1))

                    if numeric_id is None:
                        continue

                    data = price_data.get(numeric_id)
                    if data is None:
                        continue

                    # Сохраняем предыдущую цену
                    old_price = sp.supplier_price

                    # Обновляем поля
                    if data['price'] > 0:
                        sp.supplier_price = data['price']
                    sp.supplier_quantity = data['quantity']

                    # Статус поставщика
                    if data['status'] == '1' or data['quantity'] > 0:
                        sp.supplier_status = 'in_stock'
                    else:
                        sp.supplier_status = 'out_of_stock'

                    # РРЦ
                    if data['rrp'] is not None and data['rrp'] > 0:
                        sp.recommended_retail_price = data['rrp']

                    # Артикулы и штрихкод
                    if data['vendor_code']:
                        sp.vendor_code = data['vendor_code']
                    if data['additional_vendor_code']:
                        sp.additional_vendor_code = data['additional_vendor_code']
                    if data['barcode']:
                        sp.barcode = data['barcode']

                    # Трекинг изменения цены
                    sp.last_price_sync_at = now
                    if old_price is not None and data['price'] > 0 and old_price != data['price']:
                        sp.previous_price = old_price
                        sp.price_changed_at = now

                    result.updated += 1

                    batch_count += 1
                    if batch_count % 200 == 0:
                        db.session.flush()

                except Exception as e:
                    result.errors += 1
                    result.error_messages.append(f"Товар {sp.external_id}: {str(e)[:100]}")
                    if len(result.error_messages) > 50:
                        result.error_messages.append("...и другие ошибки")
                        break

            db.session.commit()

            # Обновляем hash INF файла
            if supplier.price_file_inf_url:
                try:
                    inf_resp = requests.get(supplier.price_file_inf_url, timeout=30)
                    inf_resp.raise_for_status()
                    import hashlib as _hl
                    supplier.last_price_file_hash = _hl.md5(inf_resp.content).hexdigest()
                except Exception:
                    pass

            supplier.last_price_sync_at = now
            supplier.last_price_sync_status = 'success'
            supplier.last_price_sync_error = None
            db.session.commit()

            result.duration_seconds = time.time() - start_time
            logger.info(
                f"Синхронизация цен {supplier.code}: "
                f"обновлено={result.updated}, ошибок={result.errors} "
                f"({result.duration_seconds:.1f}s)"
            )

        except Exception as e:
            db.session.rollback()
            result.success = False
            result.error_messages.append(f"Критическая ошибка: {str(e)}")
            supplier.last_price_sync_status = 'failed'
            supplier.last_price_sync_error = str(e)[:500]
            db.session.commit()
            logger.error(f"Ошибка синхронизации цен {supplier.code}: {e}")

        result.duration_seconds = time.time() - start_time
        return result

    @staticmethod
    def cascade_prices_to_sellers(supplier_id: int) -> dict:
        """
        Каскадное обновление закупочных цен и остатков к продавцам.
        Обновляет supplier_price и supplier_quantity в ImportedProduct,
        НЕ меняет calculated_price — это делает продавец через свои PricingSettings.
        """
        updated = 0
        errors = 0

        imported_products = ImportedProduct.query.filter(
            ImportedProduct.supplier_product_id.isnot(None),
            ImportedProduct.supplier_id == supplier_id
        ).all()

        for imp in imported_products:
            try:
                sp = imp.supplier_product
                if not sp:
                    continue
                if sp.supplier_price is not None:
                    imp.supplier_price = sp.supplier_price
                if sp.supplier_quantity is not None:
                    imp.supplier_quantity = sp.supplier_quantity
                imp.updated_at = datetime.utcnow()
                updated += 1
            except Exception as e:
                errors += 1
                logger.warning(f"Cascade error ImportedProduct {imp.id}: {e}")

        db.session.commit()
        logger.info(f"Каскадное обновление цен для поставщика {supplier_id}: "
                     f"обновлено={updated}, ошибок={errors}")
        return {'updated': updated, 'errors': errors, 'total': len(imported_products)}

    @staticmethod
    def get_price_stock_stats(supplier_id: int) -> dict:
        """Статистика по ценам и остаткам товаров поставщика"""
        base = SupplierProduct.query.filter_by(supplier_id=supplier_id)

        in_stock = base.filter(
            SupplierProduct.supplier_quantity.isnot(None),
            SupplierProduct.supplier_quantity > 0
        ).count()

        out_of_stock = base.filter(
            db.or_(
                SupplierProduct.supplier_quantity.is_(None),
                SupplierProduct.supplier_quantity == 0
            )
        ).count()

        with_price = base.filter(
            SupplierProduct.supplier_price.isnot(None),
            SupplierProduct.supplier_price > 0
        ).count()

        price_stats = db.session.query(
            db.func.min(SupplierProduct.supplier_price),
            db.func.max(SupplierProduct.supplier_price),
            db.func.avg(SupplierProduct.supplier_price),
            db.func.sum(SupplierProduct.supplier_quantity),
        ).filter(
            SupplierProduct.supplier_id == supplier_id,
            SupplierProduct.supplier_price.isnot(None),
            SupplierProduct.supplier_price > 0
        ).first()

        with_rrp = base.filter(
            SupplierProduct.recommended_retail_price.isnot(None),
            SupplierProduct.recommended_retail_price > 0
        ).count()

        price_changed = base.filter(
            SupplierProduct.previous_price.isnot(None)
        ).count()

        return {
            'in_stock': in_stock,
            'out_of_stock': out_of_stock,
            'with_price': with_price,
            'with_rrp': with_rrp,
            'price_changed': price_changed,
            'min_price': round(price_stats[0], 2) if price_stats[0] else 0,
            'max_price': round(price_stats[1], 2) if price_stats[1] else 0,
            'avg_price': round(price_stats[2], 2) if price_stats[2] else 0,
            'total_stock': int(price_stats[3]) if price_stats[3] else 0,
        }

    # -----------------------------------------------------------------------
    # Управление товарами
    # -----------------------------------------------------------------------

    @staticmethod
    def get_products(supplier_id: int, page: int = 1, per_page: int = 50,
                     search: str = None, status: str = None,
                     category: str = None, brand: str = None,
                     ai_validated: bool = None, has_photos: bool = None,
                     stock_status: str = None,
                     sort_by: str = 'created_at', sort_dir: str = 'desc'):
        """Получить товары поставщика с фильтрацией и пагинацией"""
        q = SupplierProduct.query.filter_by(supplier_id=supplier_id)

        # Фильтры
        if stock_status == 'in_stock':
            q = q.filter(SupplierProduct.supplier_quantity.isnot(None),
                         SupplierProduct.supplier_quantity > 0)
        elif stock_status == 'out_of_stock':
            q = q.filter(db.or_(
                SupplierProduct.supplier_quantity.is_(None),
                SupplierProduct.supplier_quantity == 0))
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
                                          show_imported: bool = False,
                                          stock_status: str = None):
        """Товары поставщика, доступные для импорта продавцу

        stock_status: 'in_stock' | 'out_of_stock' | None (все)
        """
        q = SupplierProduct.query.filter_by(supplier_id=supplier_id)
        q = q.filter(SupplierProduct.status.in_(['draft', 'validated', 'ready']))

        if not show_imported:
            # Исключаем уже импортированные
            imported_sp_ids = db.session.query(ImportedProduct.supplier_product_id).filter(
                ImportedProduct.seller_id == seller_id,
                ImportedProduct.supplier_product_id.isnot(None)
            ).subquery()
            q = q.filter(~SupplierProduct.id.in_(imported_sp_ids))

        # Фильтр по наличию
        if stock_status == 'in_stock':
            q = q.filter(SupplierProduct.supplier_quantity.isnot(None),
                         SupplierProduct.supplier_quantity > 0)
        elif stock_status == 'out_of_stock':
            q = q.filter(db.or_(
                SupplierProduct.supplier_quantity.is_(None),
                SupplierProduct.supplier_quantity == 0))

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

    # ===================================================================
    # СИНХРОНИЗАЦИЯ ОПИСАНИЙ ИЗ ВНЕШНЕГО CSV
    # ===================================================================

    @staticmethod
    def sync_descriptions(supplier_id: int) -> dict:
        """
        Синхронизация описаний товаров из отдельного CSV файла поставщика.

        Формат CSV: id;описание (или с заголовками)
        Привязка по external_id (числовой ID).

        Returns:
            dict: {success, updated, not_found, errors, error_messages}
        """
        supplier = Supplier.query.get(supplier_id)
        if not supplier:
            return {'success': False, 'error': 'Поставщик не найден'}
        if not supplier.description_file_url:
            return {'success': False, 'error': 'URL файла описаний не задан'}

        delimiter = supplier.description_file_delimiter or ';'
        encoding = supplier.description_file_encoding or 'cp1251'

        try:
            resp = requests.get(supplier.description_file_url, timeout=60)
            resp.raise_for_status()
            content = resp.content.decode(encoding, errors='replace')
        except Exception as e:
            supplier.last_description_sync_status = 'failed'
            db.session.commit()
            return {'success': False, 'error': f'Ошибка загрузки: {e}'}

        updated = 0
        not_found = 0
        errors = 0
        error_messages = []

        reader = csv.reader(StringIO(content), delimiter=delimiter, quotechar='"')
        for row_num, row in enumerate(reader, 1):
            try:
                if len(row) < 2:
                    continue

                product_id_raw = row[0].strip()
                description = row[1].strip() if len(row) > 1 else ''

                if not product_id_raw or not description:
                    continue

                # Ищем товар по external_id или числовому ID
                product = SupplierProduct.query.filter(
                    SupplierProduct.supplier_id == supplier_id,
                    db.or_(
                        SupplierProduct.external_id == product_id_raw,
                        SupplierProduct.external_id.like(f'%{product_id_raw}%')
                    )
                ).first()

                if not product:
                    not_found += 1
                    continue

                product.description = description
                product.description_source = 'csv'
                product.updated_at = datetime.utcnow()
                updated += 1

                if updated % 100 == 0:
                    db.session.flush()

            except Exception as e:
                errors += 1
                if len(error_messages) < 10:
                    error_messages.append(f"Строка {row_num}: {e}")

        supplier.last_description_sync_at = datetime.utcnow()
        supplier.last_description_sync_status = 'success' if errors == 0 else 'partial'
        db.session.commit()

        return {
            'success': True,
            'updated': updated,
            'not_found': not_found,
            'errors': errors,
            'error_messages': error_messages,
        }

    # ===================================================================
    # AI ПОЛНЫЙ ПАРСИНГ ТОВАРА
    # ===================================================================

    @staticmethod
    def ai_full_parse(product_id: int) -> dict:
        """
        Полный AI парсинг товара — извлекает ВСЕ возможные характеристики.

        Собирает все данные товара, отправляет в AI, получает структурированный
        JSON со всеми характеристиками, сохраняет результат.

        Returns:
            dict: {success, parsed_data, marketplace_data, fill_percentage, error}
        """
        product = SupplierProduct.query.get(product_id)
        if not product:
            return {'success': False, 'error': 'Товар не найден'}

        supplier = Supplier.query.get(product.supplier_id)
        if not supplier or not supplier.ai_enabled:
            return {'success': False, 'error': 'AI не включен для этого поставщика'}

        ai_svc = SupplierService._get_ai_service(supplier)
        if not ai_svc:
            return {'success': False, 'error': 'Не удалось создать AI сервис'}

        # Собираем все данные товара
        product_data = product.get_all_data_for_parsing()

        # Включённые категории маркетплейса для AI
        mp_categories = _get_marketplace_categories_block(supplier.id)

        success, result, error = ai_svc.full_product_parse(
            product_data, marketplace_categories_block=mp_categories
        )

        if success and result:
            # Сохраняем полный результат парсинга
            product.ai_parsed_data_json = json.dumps(result, ensure_ascii=False)
            product.ai_parsed_at = datetime.utcnow()

            # Формируем данные для маркетплейса (WB)
            marketplace_data = _build_marketplace_data(product, result)
            product.ai_marketplace_json = json.dumps(marketplace_data, ensure_ascii=False)

            # Обновляем поля товара из результатов парсинга если они были пустые
            _apply_parsed_data_to_product(product, result)
            
            # Интеграция с MarketplaceAwareParsingTask
            _run_marketplace_aware_parse(product, result, ai_svc)

            product.updated_at = datetime.utcnow()
            db.session.commit()

            fill_pct = result.get('parsing_meta', {}).get('fill_percentage', 0)

            return {
                'success': True,
                'parsed_data': result,
                'marketplace_data': marketplace_data,
                'fill_percentage': fill_pct,
            }

        return {'success': False, 'error': error or 'Ошибка AI парсинга'}

    # ===================================================================
    # ФОНОВЫЙ AI ПАРСИНГ
    # ===================================================================

    @staticmethod
    def start_ai_parse_job(supplier_id: int, product_ids: List[int],
                           admin_user_id: int = None,
                           max_workers: int = 4) -> dict:
        """
        Запускает фоновый AI парсинг товаров.

        Returns:
            dict: {job_id, total} или {error}
        """
        from models import AIParseJob

        supplier = Supplier.query.get(supplier_id)
        if not supplier or not supplier.ai_enabled:
            return {'error': 'AI не включен для этого поставщика'}

        if not product_ids:
            return {'error': 'Не выбраны товары'}

        job_id = str(uuid.uuid4())
        job = AIParseJob(
            id=job_id,
            supplier_id=supplier_id,
            admin_user_id=admin_user_id,
            job_type='parse' if len(product_ids) > 1 else 'parse_single',
            status='pending',
            total=len(product_ids),
            processed=0,
            succeeded=0,
            failed=0,
            results=json.dumps([]),
        )
        db.session.add(job)
        db.session.commit()

        effective_workers = max(1, min(max_workers, 8, len(product_ids)))

        thread = threading.Thread(
            target=SupplierService._run_ai_parse_job,
            args=(job_id, supplier_id, product_ids, effective_workers),
            daemon=True,
            name=f'AIParse-{job_id[:8]}'
        )
        thread.start()

        logger.info(
            f"[AI Parse] Job {job_id} started: {len(product_ids)} products, "
            f"{effective_workers} workers, supplier={supplier_id}"
        )
        return {'job_id': job_id, 'total': len(product_ids)}

    @staticmethod
    def _run_ai_parse_job(job_id: str, supplier_id: int, product_ids: List[int],
                          max_workers: int = 4):
        """
        Фоновый поток AI парсинга с параллельной обработкой.

        Использует ThreadPoolExecutor для одновременной отправки нескольких
        AI-запросов. Каждый воркер получает свой AIService (свою HTTP-сессию).
        Прогресс обновляется thread-safe через Lock.
        """
        from seller_platform import app as flask_app
        from models import AIParseJob

        with flask_app.app_context():
            job = AIParseJob.query.get(job_id)
            if not job:
                logger.error(f"[AI Parse] Job {job_id} not found")
                return

            job.status = 'running'
            db.session.commit()

            supplier = Supplier.query.get(supplier_id)
            if not supplier or not supplier.ai_enabled:
                job.status = 'failed'
                job.error_message = 'AI не включен'
                db.session.commit()
                return

            # Проверяем что AI сервис создаётся
            test_svc = SupplierService._get_ai_service(supplier)
            if not test_svc:
                job.status = 'failed'
                job.error_message = 'Не удалось создать AI сервис'
                db.session.commit()
                return

            # Для одного товара — без пула
            if len(product_ids) == 1:
                SupplierService._parse_single_in_job(
                    job_id, supplier_id, product_ids[0], test_svc
                )
                return

            # Resolve marketplace categories once for all workers
            mp_categories = _get_marketplace_categories_block(supplier_id)

            # --- Параллельный режим ---
            effective_workers = min(max_workers, len(product_ids))
            logger.info(
                f"[AI Parse] Job {job_id}: {len(product_ids)} products, "
                f"{effective_workers} parallel workers"
            )

            # Thread-safe счётчики и результаты
            lock = threading.Lock()
            counters = {'processed': 0, 'succeeded': 0, 'failed': 0}
            results = []
            cancelled = threading.Event()

            def _update_job_progress(current_title=None):
                """Обновляет прогресс в БД (вызывается под lock из main thread)."""
                try:
                    job_ref = AIParseJob.query.get(job_id)
                    if not job_ref:
                        return
                    job_ref.processed = counters['processed']
                    job_ref.succeeded = counters['succeeded']
                    job_ref.failed = counters['failed']
                    job_ref.current_product_title = current_title
                    job_ref.results = json.dumps(results[-100:], ensure_ascii=False)
                    db.session.commit()
                except Exception:
                    db.session.rollback()

            def _check_cancelled():
                """Проверяет не отменена ли задача."""
                try:
                    db.session.expire_all()
                    j = AIParseJob.query.get(job_id)
                    if j and j.status == 'cancelled':
                        cancelled.set()
                        return True
                except Exception:
                    pass
                return False

            def _parse_one(pid: int) -> dict:
                """
                Парсит один товар. Запускается в воркер-потоке.
                Каждый вызов создаёт свой AIService (свою HTTP-сессию).
                Оборачивается в app_context(), т.к. потоки пула не наследуют
                Flask application context из родительского потока.
                """
                if cancelled.is_set():
                    return {'product_id': pid, 'status': 'cancelled'}

                with flask_app.app_context():
                    product = SupplierProduct.query.get(pid)
                    if not product or product.supplier_id != supplier_id:
                        return {
                            'product_id': pid, 'title': '',
                            'status': 'error', 'error': 'Товар не найден',
                        }

                    title = (product.title or '')[:80]

                    try:
                        product_data = product.get_all_data_for_parsing()

                        # Создаём отдельный AIService для этого воркера
                        worker_svc = SupplierService._get_ai_service(supplier)
                        if not worker_svc:
                            return {
                                'product_id': pid, 'title': title,
                                'status': 'error', 'error': 'AI сервис недоступен',
                            }

                        success, result, error = worker_svc.full_product_parse(
                            product_data, marketplace_categories_block=mp_categories
                        )

                        if success and result:
                            product.ai_parsed_data_json = json.dumps(result, ensure_ascii=False)
                            product.ai_parsed_at = datetime.utcnow()

                            marketplace_data = _build_marketplace_data(product, result)
                            product.ai_marketplace_json = json.dumps(marketplace_data, ensure_ascii=False)

                            _apply_parsed_data_to_product(product, result)
                            
                            # Интеграция с MarketplaceAwareParsingTask
                            _run_marketplace_aware_parse(product, result, worker_svc)
                            
                            product.updated_at = datetime.utcnow()
                            db.session.commit()

                            fill_pct = result.get('parsing_meta', {}).get('fill_percentage', 0)
                            return {
                                'product_id': pid, 'title': title,
                                'status': 'success', 'fill_pct': fill_pct,
                            }
                        else:
                            return {
                                'product_id': pid, 'title': title,
                                'status': 'error', 'error': error or 'Ошибка AI',
                            }
                    except Exception as e:
                        db.session.rollback()
                        logger.error(f"[AI Parse] Worker error pid={pid}: {e}")
                        return {
                            'product_id': pid, 'title': title,
                            'status': 'error', 'error': str(e)[:200],
                        }

            # Запускаем пул
            with ThreadPoolExecutor(max_workers=effective_workers,
                                    thread_name_prefix='AIParse') as pool:
                futures = {}
                for pid in product_ids:
                    if cancelled.is_set():
                        break
                    fut = pool.submit(_parse_one, pid)
                    futures[fut] = pid

                # Проверяем отмену периодически
                check_interval = max(1, effective_workers)
                done_count = 0

                for fut in as_completed(futures):
                    res = fut.result()
                    done_count += 1

                    with lock:
                        counters['processed'] += 1
                        if res.get('status') == 'success':
                            counters['succeeded'] += 1
                        elif res.get('status') == 'cancelled':
                            pass  # не считаем
                        else:
                            counters['failed'] += 1
                        results.append(res)

                    # Обновляем прогресс в БД каждые N завершений
                    if done_count % check_interval == 0 or done_count == len(product_ids):
                        current_title = res.get('title') if res.get('status') != 'success' else None
                        _update_job_progress(current_title)

                        # Проверяем отмену
                        if done_count % (check_interval * 2) == 0:
                            _check_cancelled()
                            if cancelled.is_set():
                                pool.shutdown(wait=False, cancel_futures=True)
                                break

            # Завершение задачи
            try:
                job_final = AIParseJob.query.get(job_id)
                if job_final:
                    if job_final.status != 'cancelled':
                        job_final.status = 'done'
                    job_final.processed = counters['processed']
                    job_final.succeeded = counters['succeeded']
                    job_final.failed = counters['failed']
                    job_final.current_product_title = None
                    job_final.results = json.dumps(results[-100:], ensure_ascii=False)
                    job_final.updated_at = datetime.utcnow()
                    db.session.commit()
            except Exception:
                db.session.rollback()

            logger.info(
                f"[AI Parse] Job {job_id} done: "
                f"{counters['succeeded']} ok, {counters['failed']} fail "
                f"out of {len(product_ids)} ({effective_workers} workers)"
            )

    @staticmethod
    def _parse_single_in_job(job_id: str, supplier_id: int, product_id: int, ai_svc):
        """Парсит один товар в рамках job (без пула)."""
        from models import AIParseJob

        job = AIParseJob.query.get(job_id)
        if not job:
            return

        product = SupplierProduct.query.get(product_id)
        if not product or product.supplier_id != supplier_id:
            job.status = 'done'
            job.processed = 1
            job.failed = 1
            job.results = json.dumps([{
                'product_id': product_id, 'status': 'error',
                'error': 'Товар не найден',
            }], ensure_ascii=False)
            db.session.commit()
            return

        job.current_product_title = (product.title or 'Без названия')[:200]
        db.session.commit()

        try:
            product_data = product.get_all_data_for_parsing()
            mp_categories = _get_marketplace_categories_block(supplier_id)
            success, result, error = ai_svc.full_product_parse(
                product_data, marketplace_categories_block=mp_categories
            )

            if success and result:
                product.ai_parsed_data_json = json.dumps(result, ensure_ascii=False)
                product.ai_parsed_at = datetime.utcnow()
                marketplace_data = _build_marketplace_data(product, result)
                product.ai_marketplace_json = json.dumps(marketplace_data, ensure_ascii=False)
                _apply_parsed_data_to_product(product, result)

                # Интеграция с MarketplaceAwareParsingTask
                _run_marketplace_aware_parse(product, result, ai_svc)

                product.updated_at = datetime.utcnow()
                fill_pct = result.get('parsing_meta', {}).get('fill_percentage', 0)

                job.status = 'done'
                job.processed = 1
                job.succeeded = 1
                job.results = json.dumps([{
                    'product_id': product_id,
                    'title': (product.title or '')[:80],
                    'status': 'success',
                    'fill_pct': fill_pct,
                }], ensure_ascii=False)
            else:
                job.status = 'done'
                job.processed = 1
                job.failed = 1
                job.results = json.dumps([{
                    'product_id': product_id,
                    'title': (product.title or '')[:80],
                    'status': 'error',
                    'error': error or 'Ошибка AI',
                }], ensure_ascii=False)
        except Exception as e:
            logger.error(f"[AI Parse] Single parse error {product_id}: {e}")
            job.status = 'done'
            job.processed = 1
            job.failed = 1
            job.results = json.dumps([{
                'product_id': product_id,
                'status': 'error',
                'error': str(e)[:200],
            }], ensure_ascii=False)

        job.current_product_title = None
        job.updated_at = datetime.utcnow()
        db.session.commit()

    @staticmethod
    def start_description_sync_job(supplier_id: int, admin_user_id: int = None) -> dict:
        """
        Запускает фоновую синхронизацию описаний.

        Returns:
            dict: {job_id} или {error}
        """
        from models import AIParseJob

        supplier = Supplier.query.get(supplier_id)
        if not supplier:
            return {'error': 'Поставщик не найден'}
        if not supplier.description_file_url:
            return {'error': 'URL файла описаний не задан'}

        job_id = str(uuid.uuid4())
        job = AIParseJob(
            id=job_id,
            supplier_id=supplier_id,
            admin_user_id=admin_user_id,
            job_type='sync_descriptions',
            status='pending',
            total=0,
            processed=0,
            succeeded=0,
            failed=0,
            results=json.dumps([]),
        )
        db.session.add(job)
        db.session.commit()

        thread = threading.Thread(
            target=SupplierService._run_description_sync_job,
            args=(job_id, supplier_id),
            daemon=True,
            name=f'DescSync-{job_id[:8]}'
        )
        thread.start()

        return {'job_id': job_id}

    @staticmethod
    def _run_description_sync_job(job_id: str, supplier_id: int):
        """Фоновый поток синхронизации описаний."""
        from seller_platform import app as flask_app
        from models import AIParseJob

        with flask_app.app_context():
            job = AIParseJob.query.get(job_id)
            if not job:
                return

            job.status = 'running'
            job.current_product_title = 'Загрузка CSV...'
            db.session.commit()

            result = SupplierService.sync_descriptions(supplier_id)

            job.status = 'done' if result.get('success') else 'failed'
            job.succeeded = result.get('updated', 0)
            job.failed = result.get('errors', 0)
            job.processed = job.succeeded + job.failed + result.get('not_found', 0)
            job.total = job.processed
            job.error_message = result.get('error')
            job.current_product_title = None
            job.results = json.dumps([{
                'updated': result.get('updated', 0),
                'not_found': result.get('not_found', 0),
                'errors': result.get('errors', 0),
                'error_messages': result.get('error_messages', []),
            }], ensure_ascii=False)
            job.updated_at = datetime.utcnow()
            db.session.commit()

            logger.info(f"[Desc Sync] Job {job_id} done: {result}")

    @staticmethod
    def get_ai_parse_job(job_id: str) -> Optional[dict]:
        """Получить статус фоновой задачи AI парсинга."""
        from models import AIParseJob

        job = AIParseJob.query.get(job_id)
        if not job:
            return None

        results = []
        if job.results:
            try:
                results = json.loads(job.results)
            except (json.JSONDecodeError, TypeError):
                pass

        return {
            'job_id': job.id,
            'job_type': job.job_type,
            'status': job.status,
            'total': job.total,
            'processed': job.processed,
            'succeeded': job.succeeded,
            'failed': job.failed,
            'current_product': job.current_product_title,
            'error_message': job.error_message,
            'progress_pct': round(job.processed / job.total * 100) if job.total else 0,
            'results': results[-50:],
            'created_at': job.created_at.isoformat() if job.created_at else None,
            'updated_at': job.updated_at.isoformat() if job.updated_at else None,
        }

    @staticmethod
    def cancel_ai_parse_job(job_id: str) -> bool:
        """Отменить фоновую задачу AI парсинга."""
        from models import AIParseJob

        job = AIParseJob.query.get(job_id)
        if not job or job.status not in ('pending', 'running'):
            return False
        job.status = 'cancelled'
        db.session.commit()
        return True

    @staticmethod
    def get_active_ai_parse_jobs(supplier_id: int) -> List[dict]:
        """Получить активные задачи AI парсинга для поставщика."""
        from models import AIParseJob

        jobs = AIParseJob.query.filter(
            AIParseJob.supplier_id == supplier_id,
            AIParseJob.status.in_(['pending', 'running'])
        ).order_by(AIParseJob.created_at.desc()).all()

        return [SupplierService.get_ai_parse_job(j.id) for j in jobs]

    @staticmethod
    def get_recent_ai_parse_jobs(supplier_id: int, limit: int = 10) -> List[dict]:
        """Получить последние задачи AI парсинга."""
        from models import AIParseJob

        jobs = AIParseJob.query.filter_by(supplier_id=supplier_id)\
            .order_by(AIParseJob.created_at.desc()).limit(limit).all()

        return [SupplierService.get_ai_parse_job(j.id) for j in jobs]

    @staticmethod
    def get_product_raw_json(product_id: int) -> dict:
        """
        Возвращает полный JSON дамп товара для анализа.

        Returns:
            dict: Все данные товара включая AI парсинг
        """
        product = SupplierProduct.query.get(product_id)
        if not product:
            return {'error': 'Товар не найден'}

        data = product.to_dict(include_ai=True)
        # Добавляем полные данные
        data['all_data_for_parsing'] = product.get_all_data_for_parsing()
        data['ai_parsed_data'] = product.get_ai_parsed_data()
        data['ai_marketplace_data'] = product.get_ai_marketplace_data()

        # Оригинальные данные
        try:
            data['original_data'] = json.loads(product.original_data_json) if product.original_data_json else {}
        except Exception:
            data['original_data'] = {}

        # JSON поля развёрнутые
        try:
            data['colors'] = json.loads(product.colors_json) if product.colors_json else []
        except Exception:
            data['colors'] = []
        try:
            data['materials'] = json.loads(product.materials_json) if product.materials_json else []
        except Exception:
            data['materials'] = []
        try:
            data['sizes'] = json.loads(product.sizes_json) if product.sizes_json else {}
        except Exception:
            data['sizes'] = {}
        try:
            data['dimensions'] = json.loads(product.dimensions_json) if product.dimensions_json else {}
        except Exception:
            data['dimensions'] = {}
        try:
            data['characteristics'] = json.loads(product.characteristics_json) if product.characteristics_json else []
        except Exception:
            data['characteristics'] = []

        return data


# ============================================================================
# MARKETPLACE DATA BUILDER
# ============================================================================

def _build_marketplace_data(product: SupplierProduct, parsed: dict) -> dict:
    """
    Формирует данные в формате маркетплейса WB из AI парсинга.

    Returns:
        dict с данными готовыми для загрузки в WB
    """
    mp = parsed.get('marketplace_ready', {})
    brand_info = parsed.get('brand_info', {})
    physical = parsed.get('physical', {})
    pkg = parsed.get('package', {})
    materials = parsed.get('materials', {})
    color = parsed.get('color', {})
    sizing = parsed.get('sizing', {})
    audience = parsed.get('audience', {})
    seasonality = parsed.get('seasonality', {})
    contents = parsed.get('contents', {})
    identity = parsed.get('product_identity', {})
    origin = parsed.get('origin', {})

    # --- Оценка веса, если AI не извлёк ---
    estimated_weight_g = _estimate_weight_g(product, parsed)

    # Габариты товара
    dims_length = physical.get('length_cm')
    dims_width = physical.get('width_cm')
    dims_height = physical.get('height_cm')
    dims_weight_kg = round(physical.get('weight_g', 0) / 1000, 2) if physical.get('weight_g') else None

    if not dims_weight_kg and estimated_weight_g:
        dims_weight_kg = round(estimated_weight_g / 1000, 2)

    # Габариты упаковки — fallback 20×20×30 если ничего нет
    pkg_length = pkg.get('package_length_cm') or 20
    pkg_width = pkg.get('package_width_cm') or 20
    pkg_height = pkg.get('package_height_cm') or 30
    pkg_weight_kg = round(pkg.get('package_weight_g', 0) / 1000, 2) if pkg.get('package_weight_g') else None

    if not pkg_weight_kg and dims_weight_kg:
        # Упаковка ≈ товар + 50-100г на коробку/пакет
        pkg_weight_kg = round(dims_weight_kg + 0.08, 2)

    wb_data = {
        'title': mp.get('wb_title_suggestion') or product.ai_seo_title or product.title or '',
        'description': mp.get('wb_description_short') or product.ai_description or product.description or '',
        'brand': brand_info.get('brand_normalized') or brand_info.get('brand') or product.brand or '',
        'vendor_code': product.vendor_code or '',
        'barcode': product.barcode or '',

        # Категория
        'category': identity.get('wb_category') or product.wb_category_name or product.category or '',
        'subject': identity.get('wb_subject') or product.wb_subject_name or '',
        'subject_id': product.wb_subject_id,

        # Цена
        'price': product.supplier_price,

        # Цвет
        'color': color.get('wb_color') or color.get('primary_color') or '',

        # Размеры
        'sizes': sizing.get('available_sizes', []),
        'ru_sizes': sizing.get('ru_sizes', []),

        # Габариты для WB
        'dimensions': {
            'length': dims_length,
            'width': dims_width,
            'height': dims_height,
            'weight_kg': dims_weight_kg,
        },
        'package_dimensions': {
            'length': pkg_length,
            'width': pkg_width,
            'height': pkg_height,
            'weight_kg': pkg_weight_kg,
        },

        # Характеристики
        'characteristics': {
            'Бренд': brand_info.get('brand_normalized') or brand_info.get('brand') or product.brand or '',
            'Цвет': color.get('wb_color') or '',
            'Пол': audience.get('gender') or product.gender or '',
            'Страна производства': origin.get('country_of_origin') or product.country or '',
            'Материал': materials.get('primary_material') or '',
            'Состав': materials.get('composition') or '',
            'Сезон': seasonality.get('season') or product.season or '',
            'Комплектация': ', '.join(contents.get('package_contents', [])) if contents.get('package_contents') else '',
        },

        # SEO
        'keywords': mp.get('search_keywords', []),
        'bullet_points': mp.get('bullet_points', []),

        # Доп. данные
        'photos_count': len(product.get_photos()),
    }

    # Удаляем пустые характеристики
    wb_data['characteristics'] = {k: v for k, v in wb_data['characteristics'].items() if v}

    # Добавляем доп характеристики из физических
    if physical.get('diameter_cm'):
        wb_data['characteristics']['Диаметр'] = physical['diameter_cm']
    if physical.get('volume_ml'):
        wb_data['characteristics']['Объем'] = physical['volume_ml']
    if physical.get('working_length_cm'):
        wb_data['characteristics']['Рабочая длина'] = physical['working_length_cm']

    return wb_data


def _apply_parsed_data_to_product(product: SupplierProduct, parsed: dict) -> None:
    """Обновляет пустые поля товара из результатов AI парсинга"""
    brand_info = parsed.get('brand_info', {})
    audience = parsed.get('audience', {})
    origin = parsed.get('origin', {})
    seasonality = parsed.get('seasonality', {})
    color = parsed.get('color', {})
    materials = parsed.get('materials', {})
    physical = parsed.get('physical', {})

    # Обновляем только пустые поля
    if not product.brand and brand_info.get('brand'):
        product.brand = brand_info['brand']

    if not product.gender and audience.get('gender'):
        gender_map = {'male': 'Мужской', 'female': 'Женский', 'unisex': 'Унисекс'}
        product.gender = gender_map.get(audience['gender'], audience['gender'])

    if not product.country and origin.get('country_of_origin'):
        product.country = origin['country_of_origin']

    if not product.season and seasonality.get('season'):
        season_map = {'all_season': 'Всесезонный', 'summer': 'Лето', 'winter': 'Зима', 'demi': 'Демисезон'}
        product.season = season_map.get(seasonality['season'], seasonality['season'])

    if not product.age_group and audience.get('age_group'):
        age_map = {'adult': 'Взрослый', 'teen': 'Подросток', 'child': 'Детский', 'baby': 'Малыш'}
        product.age_group = age_map.get(audience['age_group'], audience['age_group'])

    # Обновляем JSON поля если пустые
    if not product.colors_json and color.get('primary_color'):
        colors = [color['primary_color']]
        if color.get('secondary_colors'):
            colors.extend(color['secondary_colors'])
        product.colors_json = json.dumps(colors, ensure_ascii=False)

    if not product.materials_json and materials.get('materials_list'):
        product.materials_json = json.dumps(materials['materials_list'], ensure_ascii=False)

    if not product.dimensions_json and any(v for v in physical.values() if v is not None):
        product.dimensions_json = json.dumps(physical, ensure_ascii=False)

    # Маркетплейс-ready данные
    mp = parsed.get('marketplace_ready', {})
    if not product.ai_seo_title and mp.get('wb_title_suggestion'):
        product.ai_seo_title = mp['wb_title_suggestion'][:60]

    if not product.ai_description and mp.get('wb_description_short'):
        product.ai_description = mp['wb_description_short']

    if not product.ai_keywords_json and mp.get('search_keywords'):
        product.ai_keywords_json = json.dumps(mp['search_keywords'], ensure_ascii=False)

    if not product.ai_bullets_json and mp.get('bullet_points'):
        product.ai_bullets_json = json.dumps(mp['bullet_points'], ensure_ascii=False)


def _run_marketplace_aware_parse(product: SupplierProduct, parsed_data: dict, ai_svc) -> None:
    """Запускает MarketplaceAwareParsingTask если удалось определить категорию WB."""
    from models import MarketplaceCategory, MarketplaceCategoryCharacteristic, MarketplaceConnection, Marketplace
    from services.marketplace_ai_parser import MarketplaceAwareParsingTask
    from services.marketplace_validator import MarketplaceValidator
    
    # 1. Пытаемся определить subject_id
    subject_id = product.wb_subject_id
    if not subject_id:
        wb_subject_name = parsed_data.get('product_identity', {}).get('wb_subject')
        if wb_subject_name:
            # Ищем категорию по имени
            cat = MarketplaceCategory.query.filter(MarketplaceCategory.subject_name.ilike(wb_subject_name)).first()
            if cat:
                subject_id = cat.subject_id
                product.wb_subject_id = subject_id
                product.wb_subject_name = cat.subject_name

    # Если не нашли, ищем категорию по умолчанию у поставщика
    if not subject_id:
        conn = MarketplaceConnection.query.filter_by(supplier_id=product.supplier_id, is_active=True).first()
        if conn and conn.auto_map_categories and conn.default_category_id:
            cat = MarketplaceCategory.query.get(conn.default_category_id)
            if cat:
                subject_id = cat.subject_id
                product.wb_subject_id = subject_id
                product.wb_subject_name = cat.subject_name
                
    if not subject_id:
        logger.info(f"Skipping MarketplaceAwareParsingTask for {product.id} - no subject_id found.")
        return
        
    # Ищем характеристики для этой категории
    wb_marketplace = Marketplace.query.filter_by(code='wb').first()
    if not wb_marketplace:
        return
        
    cat = MarketplaceCategory.query.filter_by(marketplace_id=wb_marketplace.id, subject_id=subject_id).first()
    if not cat:
        return
        
    characteristics = MarketplaceCategoryCharacteristic.query.filter_by(category_id=cat.id, is_enabled=True).all()
    if not characteristics:
        return
        
    # Запускаем таску
    task = MarketplaceAwareParsingTask(
        client=ai_svc.client, 
        characteristics=characteristics,
        custom_instruction=ai_svc.config.custom_parsing_instruction
    )
    
    product_info = {
        'title': product.title or product.ai_seo_title,
        'description': product.description or product.ai_description,
        'brand': product.brand
    }
    
    success, result, error = task.execute(product_info=product_info, original_data=parsed_data)
    
    if success and result:
        # Сохраняем в marketplace_fields_json
        product.marketplace_fields_json = json.dumps(result, ensure_ascii=False)

        # Валидируем
        validation_result = MarketplaceValidator.validate_product_for_marketplace(product, wb_marketplace.id)

        product.marketplace_validation_status = 'valid' if validation_result.get('valid') else 'invalid'
        product.marketplace_fill_pct = validation_result.get('fill_pct', 0)

        logger.info(f"Marketplace aware parse success for {product.id}, valid: {validation_result.get('valid')}")
    else:
        logger.error(f"Marketplace aware parse failed for {product.id}: {error}")


# ============================================================================
# WEIGHT / DIMENSIONS ESTIMATION
# ============================================================================

# Оценка веса по категории / типу товара (граммы)
# Ключ — подстрока в wb_subject, category или product_type (lowercase)
_WEIGHT_ESTIMATES = {
    # Белье и одежда
    'белье': 120,
    'бельё': 120,
    'трусы': 60,
    'стринг': 40,
    'бюстгальтер': 80,
    'лиф': 80,
    'корсет': 250,
    'корсаж': 200,
    'боди': 150,
    'комбинезон': 250,
    'пеньюар': 150,
    'халат': 300,
    'сорочка': 120,
    'чулки': 60,
    'колготки': 80,
    'носки': 40,
    'перчатки': 50,
    'маска': 60,
    'повязка': 30,
    'костюм': 350,
    'платье': 250,
    'юбка': 150,
    'накидка': 150,
    'плетка': 200,
    'флоггер': 250,

    # БДСМ аксессуары
    'наручники': 200,
    'кляп': 100,
    'ошейник': 120,
    'поводок': 80,
    'привязь': 150,
    'бондаж': 300,
    'фиксатор': 200,
    'зажим': 40,
    'шлепалка': 120,
    'стек': 150,
    'кнут': 200,
    'паддл': 180,
    'веревка': 250,
    'верёвка': 250,
    'лента': 60,
    'ремень': 150,

    # Вибраторы и секс-игрушки
    'вибратор': 180,
    'массажер': 250,
    'массажёр': 250,
    'стимулятор': 120,
    'фаллоимитатор': 300,
    'дилдо': 300,
    'анальн': 100,
    'пробка': 100,
    'plug': 100,
    'кольцо': 50,
    'насадка': 80,
    'помпа': 250,
    'мастурбатор': 350,
    'вагина': 400,
    'яйцо': 60,
    'шарик': 80,
    'бусы': 80,
    'клитор': 80,

    # Косметика и смазки
    'смазка': 150,
    'лубрикант': 150,
    'гель': 120,
    'крем': 100,
    'масло': 200,
    'спрей': 100,
    'духи': 80,
    'парфюм': 80,
    'свеча': 250,

    # Презервативы
    'презерватив': 40,

    # Прочее
    'батарейк': 30,
    'зарядк': 80,
    'чехол': 60,
    'сумка': 150,
}


def _estimate_weight_g(product, parsed: dict) -> Optional[int]:
    """
    Оценивает вес товара на основе категории, типа и материалов.
    Возвращает вес в граммах или None если оценить невозможно.
    """
    physical = parsed.get('physical', {})

    # Если AI уже извлёк вес — не трогаем
    if physical.get('weight_g'):
        return physical['weight_g']

    # Собираем текстовые подсказки для определения типа
    identity = parsed.get('product_identity', {})
    hints = ' '.join(filter(None, [
        (identity.get('wb_subject') or '').lower(),
        (identity.get('product_type') or '').lower(),
        (identity.get('product_subtype') or '').lower(),
        (product.category or '').lower(),
        (product.wb_subject_name or '').lower(),
        (product.title or '').lower(),
    ]))

    # Ищем совпадение по таблице
    best_weight = None
    best_len = 0
    for keyword, weight in _WEIGHT_ESTIMATES.items():
        if keyword in hints and len(keyword) > best_len:
            best_weight = weight
            best_len = len(keyword)

    if best_weight:
        # Корректируем вес по материалу
        materials = parsed.get('materials', {})
        mat_hint = (materials.get('primary_material') or '').lower()
        composition = (materials.get('composition') or '').lower()
        mat_text = mat_hint + ' ' + composition

        # Силикон / латекс тяжелее
        if 'силикон' in mat_text or 'латекс' in mat_text:
            best_weight = int(best_weight * 1.3)
        # Металл значительно тяжелее
        elif 'металл' in mat_text or 'нержав' in mat_text or 'сталь' in mat_text:
            best_weight = int(best_weight * 2.0)
        elif 'стекл' in mat_text:
            best_weight = int(best_weight * 1.8)
        # Кожа чуть тяжелее
        elif 'кожа' in mat_text or 'кожан' in mat_text:
            best_weight = int(best_weight * 1.15)
        # Кружево / сетка легче
        elif 'кружев' in mat_text or 'сетк' in mat_text or 'сетч' in mat_text:
            best_weight = int(best_weight * 0.7)

        return best_weight

    # Если ничего не подошло — базовая оценка 150г
    return 150


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
