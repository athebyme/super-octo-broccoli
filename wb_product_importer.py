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
            sizes_data = json.loads(imported_product.sizes) if imported_product.sizes else {}
            barcodes = json.loads(imported_product.barcodes) if imported_product.barcodes else []
            materials = json.loads(imported_product.materials) if imported_product.materials else []
            photo_urls = json.loads(imported_product.photo_urls) if imported_product.photo_urls else []

            # Извлекаем простые размеры из структуры парсера
            # sizes_data - это словарь с полями: raw, dimensions, simple_sizes
            # Определяем, есть ли у товара реальные размеры (S/M/L, 42/44 и т.д.)
            has_real_sizes = False
            sizes_list = []

            if isinstance(sizes_data, dict):
                # Если есть simple_sizes (например, "46-48" -> ["46", "48"])
                # это реальные размеры одежды/белья
                sizes_list = sizes_data.get('simple_sizes', [])
                if sizes_list:
                    has_real_sizes = True
                # Если нет simple_sizes, проверяем dimensions
                # Для интим-товаров (длина, диаметр) - это НЕ размеры в понимании WB
                # Это характеристики товара
                elif sizes_data.get('dimensions'):
                    # У товара есть габариты (длина, диаметр), но нет размеров
                    has_real_sizes = False
                    sizes_list = []
                # Если есть только raw без dimensions и simple_sizes
                elif sizes_data.get('raw'):
                    # Пытаемся определить, это размер или габарит
                    raw = sizes_data['raw']
                    # Если содержит "см", "мм", "длина", "диаметр" - это габарит, не размер
                    if any(word in raw.lower() for word in ['см', 'мм', 'длина', 'диаметр', 'ширина', 'вес', 'объем']):
                        has_real_sizes = False
                        sizes_list = []
                    else:
                        # Возможно, это размер (S, M, 42 и т.д.)
                        has_real_sizes = True
                        sizes_list = [raw]
            elif isinstance(sizes_data, list):
                # Старый формат - список размеров
                sizes_list = sizes_data
                has_real_sizes = len(sizes_list) > 0
            else:
                sizes_list = []
                has_real_sizes = False

            # Формируем артикул
            from auto_import_manager import AutoImportManager
            settings = self.seller.auto_import_settings
            if settings and settings.vendor_code_pattern:
                pattern = settings.vendor_code_pattern
                vendor_code = pattern.replace('{product_id}', str(imported_product.external_id))
                vendor_code = vendor_code.replace('{supplier_code}', settings.supplier_code or '')
            else:
                vendor_code = imported_product.external_vendor_code

            # Формируем sizes для WB API v2
            # Два формата:
            # 1. Для товаров С размерами (одежда, обувь): [{techSize, wbSize, price, skus}]
            # 2. Для товаров БЕЗ размеров (интим-товары, аксессуары): [{skus}] - БЕЗ techSize/wbSize!
            wb_sizes = []

            if has_real_sizes and sizes_list:
                # Товар С размерами (одежда, обувь)
                for idx, size_val in enumerate(sizes_list):
                    size_str = str(size_val)

                    # Берем соответствующий баркод или первый доступный
                    barcode = barcodes[idx] if idx < len(barcodes) else (barcodes[0] if barcodes else '')

                    wb_sizes.append({
                        'techSize': size_str,
                        'wbSize': size_str,
                        'price': 0,  # Цена будет установлена позже
                        'skus': [barcode] if barcode else []
                    })
            else:
                # Товар БЕЗ размеров (интим-товары, электроника и т.д.)
                # Согласно Swagger примеру creatingGroupOfIndividualCards (строки 6380-6382)
                # для товаров без размеров НЕ указываем techSize и wbSize!
                # Только баркод в skus
                if barcodes:
                    # Если несколько баркодов - создаем несколько записей
                    # ВАЖНО: каждый баркод = отдельная запись!
                    for barcode in barcodes:
                        wb_sizes.append({
                            'skus': [barcode]
                        })
                else:
                    # Если нет баркодов, WB сгенерирует автоматически
                    wb_sizes.append({
                        'skus': []
                    })

            # Проверка: не должно быть пустого массива sizes
            if not wb_sizes:
                wb_sizes.append({
                    'skus': []
                })

            # Формируем характеристики для WB API v2
            # ВАЖНО: WB API v2 требует характеристики в формате [{id, value}]
            # где id - это числовой ID из справочника WB
            # Но для начального создания карточки можно передать пустой массив,
            # а потом обновить характеристики через update_card
            #
            # TODO: Реализовать маппинг характеристик на ID из WB API
            # Для этого нужно:
            # 1. Получить список характеристик для subject_id: api_client.get_card_characteristics_config(subject_id)
            # 2. Найти соответствующие ID для каждой характеристики
            # 3. Преобразовать в формат [{id, value}]

            characteristics = []

            # Пока оставляем пустой массив - характеристики можно добавить позже через update
            # После создания карточки можно будет получить её ID и обновить характеристики

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

            # Формируем dimensions (габариты)
            # Для начала используем дефолтные значения
            dimensions = {
                'length': 10,  # см
                'width': 10,   # см
                'height': 5,   # см
                'weightBrutto': 0.1  # кг
            }

            # Формируем бренд - используем безопасный fallback
            # Некоторые бренды могут не существовать на WB
            # В таком случае WB вернет ошибку: "Бренда «XXX» пока нет на WB"
            # Используем бренд из товара, но если он пустой - используем общий
            product_brand = imported_product.brand if imported_product.brand else 'NoName'

            # Логируем бренд для отладки
            logger.info(f"Товар {imported_product.external_id}: бренд = {product_brand}")

            # Формируем variant для WB API v2
            variant = {
                'vendorCode': vendor_code,
                'title': imported_product.title[:60] if imported_product.title else 'Товар',  # Макс 60 символов
                'description': imported_product.description or imported_product.title or 'Описание товара',
                'brand': product_brand,
                'dimensions': dimensions,
                'sizes': wb_sizes,
                'characteristics': characteristics
            }

            logger.info(f"Создание карточки WB для товара {imported_product.external_id}")
            logger.info(f"  - Артикул: {vendor_code}")
            logger.info(f"  - Бренд: {product_brand}")
            logger.info(f"  - Категория WB (subject_id): {imported_product.wb_subject_id}")
            logger.info(f"  - Размеры (has_real_sizes={has_real_sizes}): {wb_sizes}")
            logger.info(f"  - Баркоды: {barcodes}")
            logger.debug(f"Данные варианта: {variant}")

            # Вызов API WB для создания карточки
            try:
                response = self.api_client.create_product_card(
                    subject_id=imported_product.wb_subject_id,
                    variants=[variant],
                    log_to_db=True,
                    seller_id=self.seller.id
                )

                # Проверяем ответ
                if response.get('error'):
                    error_text = response.get('errorText', 'Unknown error')
                    raise Exception(f"WB API error: {error_text}")

                # Получаем nmID из ответа
                # После создания карточки нужно получить её ID
                # Это можно сделать через get_cards_list с фильтром по vendorCode
                # Или через проверку списка несозданных карточек (errors list)

                # Пытаемся найти созданную карточку по vendorCode
                try:
                    created_card = self.api_client.get_card_by_vendor_code(vendor_code)
                    nm_id = created_card.get('nmID')
                    logger.info(f"Карточка создана с nmID: {nm_id}")
                except Exception as e:
                    logger.warning(f"Не удалось получить nmID: {e}")
                    nm_id = None

            except Exception as e:
                error_msg = str(e)

                # Обрабатываем известные ошибки WB
                if 'Бренда' in error_msg and 'пока нет на WB' in error_msg:
                    error_msg = f"Бренд '{product_brand}' не зарегистрирован на Wildberries. Необходимо сначала добавить бренд в личном кабинете WB или использовать существующий бренд."
                elif 'повторяющиеся Баркоды' in error_msg:
                    error_msg = f"Баркод уже используется в другой карточке или дублируется в текущей. Баркоды: {barcodes}"
                elif 'Артикул продавца' in error_msg:
                    error_msg = f"Проблема с артикулом продавца '{vendor_code}'. Возможно, он уже используется или имеет неверный формат."

                full_error = f"Ошибка создания карточки WB: {error_msg}"
                logger.error(full_error)
                logger.error(f"Данные которые отправлялись: variant={variant}")
                raise Exception(full_error)

            # Создаем запись Product в БД
            product = Product(
                seller_id=self.seller.id,
                nm_id=nm_id or 0,  # nm_id от WB или 0 если не удалось получить
                vendor_code=vendor_code,
                title=imported_product.title,
                brand=imported_product.brand,
                subject_id=imported_product.wb_subject_id,
                object_name=imported_product.mapped_wb_category,
                description=imported_product.description,
                photos_json=json.dumps(media_urls, ensure_ascii=False),
                sizes_json=json.dumps(wb_sizes, ensure_ascii=False),
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
