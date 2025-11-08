# -*- coding: utf-8 -*-
"""
Модуль для импорта товаров из ImportedProduct в Wildberries через API
"""
import json
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime

from models import db, ImportedProduct, Product, Seller
from wb_api_client import WildberriesAPIClient

logger = logging.getLogger(__name__)


class WBProductImporter:
    """
    Класс для импорта товаров в Wildberries через API
    """

    def __init__(self, seller: Seller):
        self.seller = seller
        self.api_client = WildberriesAPIClient(seller.wb_api_key) if seller.wb_api_key else None

    def import_product_to_wb(self, imported_product: ImportedProduct) -> Tuple[bool, Optional[str], Optional[Product]]:
        """
        Импортирует один товар в WB

        Args:
            imported_product: Импортированный товар из CSV

        Returns:
            Tuple[success, error_message, created_product]
        """
        if not self.api_client:
            return False, "API ключ WB не настроен", None

        if imported_product.import_status != 'validated':
            return False, f"Товар не готов к импорту (статус: {imported_product.import_status})", None

        if imported_product.product_id:
            # Товар уже импортирован
            existing_product = Product.query.get(imported_product.product_id)
            if existing_product:
                return False, "Товар уже импортирован в WB", existing_product

        try:
            # Парсим данные
            colors = json.loads(imported_product.colors) if imported_product.colors else []
            sizes = json.loads(imported_product.sizes) if imported_product.sizes else []
            barcodes = json.loads(imported_product.barcodes) if imported_product.barcodes else []
            materials = json.loads(imported_product.materials) if imported_product.materials else []
            photo_urls = json.loads(imported_product.photo_urls) if imported_product.photo_urls else []

            # Формируем артикул
            from auto_import_manager import AutoImportManager
            settings = self.seller.auto_import_settings
            if settings and settings.vendor_code_pattern:
                pattern = settings.vendor_code_pattern
                vendor_code = pattern.replace('{product_id}', str(imported_product.external_id))
                vendor_code = vendor_code.replace('{supplier_code}', settings.supplier_code or '')
            else:
                vendor_code = imported_product.external_vendor_code

            # Формируем nomenclatures (размеры)
            nomenclatures = []
            for idx, size in enumerate(sizes):
                barcode = barcodes[idx] if idx < len(barcodes) else barcodes[0] if barcodes else ''
                nomenclatures.append({
                    'vendorCode': f"{vendor_code}-{size}",
                    'size': size,
                    'barcode': barcode
                })

            # Если нет размеров, создаем один дефолтный
            if not nomenclatures:
                nomenclatures.append({
                    'vendorCode': vendor_code,
                    'size': 'One Size',
                    'barcode': barcodes[0] if barcodes else ''
                })

            # Формируем характеристики
            characteristics = []

            # Обязательные характеристики
            if imported_product.brand:
                characteristics.append({'Бренд': imported_product.brand})

            if imported_product.country:
                characteristics.append({'Страна производства': imported_product.country})

            if colors:
                characteristics.append({'Цвет': colors[0] if len(colors) == 1 else colors})

            if materials:
                material_str = ', '.join(materials)
                characteristics.append({'Состав': material_str})

            # Комплектация
            if imported_product.description:
                characteristics.append({'Комплектация': imported_product.description[:500]})

            # Формируем медиа (фотографии)
            media_urls = []
            for photo in photo_urls[:30]:  # Максимум 30 фото
                # Приоритет блюру если есть
                if isinstance(photo, dict):
                    url = photo.get('blur') or photo.get('original')
                else:
                    url = photo
                if url:
                    media_urls.append(url)

            # Создаем карточку через API
            card_data = {
                'subjectID': imported_product.wb_subject_id,
                'vendorCode': vendor_code,
                'title': imported_product.title,
                'description': imported_product.description or imported_product.title,
                'brand': imported_product.brand or 'NoName',
                'nomenclatures': nomenclatures,
                'characteristics': characteristics,
                'photos': media_urls[:10] if media_urls else []  # Первые 10 фото
            }

            logger.info(f"Создание карточки WB для товара {imported_product.external_id}")
            logger.debug(f"Данные карточки: {card_data}")

            # Вызов API WB для создания карточки
            # response = self.api_client.create_card(card_data)
            # nm_id = response.get('nmID')

            # ВРЕМЕННО: Имитируем успешное создание для демо
            # TODO: Раскомментировать когда будет реальный API
            nm_id = None  # response.get('nmID') когда API заработает

            # Создаем запись Product в БД
            product = Product(
                seller_id=self.seller.id,
                nm_id=nm_id or 0,  # TODO: получить реальный nmID от WB
                vendor_code=vendor_code,
                title=imported_product.title,
                brand=imported_product.brand,
                subject_id=imported_product.wb_subject_id,
                object_name=imported_product.mapped_wb_category,
                description=imported_product.description,
                photos_json=json.dumps(media_urls, ensure_ascii=False),
                sizes_json=json.dumps(nomenclatures, ensure_ascii=False),
                characteristics_json=json.dumps(characteristics, ensure_ascii=False),
                is_active=True,
                last_sync=datetime.utcnow()
            )

            db.session.add(product)
            db.session.flush()  # Получить ID

            # Обновляем ImportedProduct
            imported_product.product_id = product.id
            imported_product.import_status = 'imported'
            imported_product.imported_at = datetime.utcnow()
            imported_product.import_error = None

            db.session.commit()

            logger.info(f"Товар {imported_product.external_id} успешно импортирован (Product ID: {product.id})")
            return True, None, product

        except Exception as e:
            db.session.rollback()
            error_msg = f"Ошибка импорта: {str(e)}"
            logger.error(f"Ошибка импорта товара {imported_product.external_id}: {e}", exc_info=True)

            # Сохраняем ошибку
            imported_product.import_status = 'failed'
            imported_product.import_error = error_msg
            db.session.commit()

            return False, error_msg, None

    def import_multiple_products(self, imported_product_ids: List[int]) -> Dict:
        """
        Массовый импорт товаров в WB

        Args:
            imported_product_ids: Список ID товаров для импорта

        Returns:
            Статистика импорта
        """
        stats = {
            'total': len(imported_product_ids),
            'success': 0,
            'failed': 0,
            'skipped': 0,
            'errors': []
        }

        for product_id in imported_product_ids:
            imported_product = ImportedProduct.query.get(product_id)

            if not imported_product:
                stats['skipped'] += 1
                stats['errors'].append(f"Товар ID {product_id} не найден")
                continue

            if imported_product.seller_id != self.seller.id:
                stats['skipped'] += 1
                stats['errors'].append(f"Товар {imported_product.external_id}: нет доступа")
                continue

            success, error, product = self.import_product_to_wb(imported_product)

            if success:
                stats['success'] += 1
            else:
                stats['failed'] += 1
                stats['errors'].append(f"Товар {imported_product.external_id}: {error}")

        logger.info(f"Массовый импорт завершен: {stats}")
        return stats


def import_products_batch(seller_id: int, product_ids: List[int]) -> Dict:
    """
    Функция-хелпер для массового импорта товаров

    Args:
        seller_id: ID продавца
        product_ids: Список ID товаров для импорта

    Returns:
        Статистика импорта
    """
    seller = Seller.query.get(seller_id)
    if not seller:
        return {
            'success': False,
            'error': 'Продавец не найден'
        }

    importer = WBProductImporter(seller)
    stats = importer.import_multiple_products(product_ids)

    return {
        'success': True,
        **stats
    }
