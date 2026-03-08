# -*- coding: utf-8 -*-
"""
Модуль для импорта товаров из ImportedProduct в Wildberries через API
"""
import json
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime

from models import db, ImportedProduct, Product, Seller, PricingSettings, Marketplace, MarketplaceDirectory
from services.wb_api_client import WildberriesAPIClient
from services.prohibited_words_filter import filter_prohibited_words
from services.pricing_engine import calculate_price

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
            from services.auto_import_manager import AutoImportManager
            settings = self.seller.auto_import_settings
            if settings and settings.vendor_code_pattern:
                pattern = settings.vendor_code_pattern
                vendor_code = pattern.replace('{product_id}', str(imported_product.external_id))
                vendor_code = vendor_code.replace('{supplier_code}', settings.supplier_code or '')
            else:
                vendor_code = imported_product.external_vendor_code

            # Формируем sizes для WB API v2
            # WB различает:
            # - Размерные товары (одежда, обувь): [{techSize, wbSize, price, skus}]
            # - Безразмерные товары (аксессуары, интим): [{price, skus}] — БЕЗ techSize/wbSize!
            #
            # ВАЖНО: Если WB-категория безразмерная, передавать techSize/wbSize НЕЛЬЗЯ
            # (ошибка: "Недопустимо указывать Размер и Рос.Размер для безразмерного товара")
            wb_sizes = []

            # Проверяем через WB API, поддерживает ли категория размеры
            category_has_sizes = self._category_supports_sizes(imported_product.wb_subject_id)

            if has_real_sizes and sizes_list and category_has_sizes:
                # Товар С размерами И категория поддерживает размеры
                for idx, size_val in enumerate(sizes_list):
                    size_str = str(size_val)
                    barcode = barcodes[idx] if idx < len(barcodes) else (barcodes[0] if barcodes else '')

                    wb_sizes.append({
                        'techSize': size_str,
                        'wbSize': size_str,
                        'price': 0,
                        'skus': [barcode] if barcode else []
                    })
            else:
                # Безразмерный товар — НЕ указываем techSize/wbSize
                if barcodes:
                    for barcode in barcodes:
                        wb_sizes.append({
                            'price': 0,
                            'skus': [barcode]
                        })
                else:
                    wb_sizes.append({
                        'price': 0,
                        'skus': []
                    })

            if not wb_sizes:
                wb_sizes.append({
                    'price': 0,
                    'skus': []
                })

            # Формируем характеристики для WB API v2
            # ВАЖНО: WB API v2 требует характеристики в формате [{id, value}]
            # где id - это числовой ID из справочника WB
            characteristics = self._build_wb_characteristics(imported_product)

            # Формируем медиа (фотографии) — серверные URL
            from routes.photos import generate_public_photo_urls
            media_urls = generate_public_photo_urls(imported_product)

            # Формируем dimensions (габариты)
            # Используем настроенные дефолты продавца (глобальные или по категории)
            from routes.product_defaults import get_defaults_for_product
            _product_defaults = get_defaults_for_product(
                self.seller.id, imported_product.wb_subject_id
            )
            dimensions = {
                'length': _product_defaults.get('length', 10),
                'width': _product_defaults.get('width', 10),
                'height': _product_defaults.get('height', 5),
                'weightBrutto': _product_defaults.get('weightBrutto', 0.1)
            }

            # Формируем бренд — используем систему резолва брендов
            # Pipeline: resolved_brand_id → MarketplaceBrand → BrandEngine.resolve() → raw brand fallback
            product_brand = self._resolve_brand_for_wb(imported_product)

            # Логируем бренд для отладки
            logger.info(f"Товар {imported_product.external_id}: бренд = {product_brand} (исходный: {imported_product.brand})")

            # Фильтрация запрещённых слов WB перед отправкой (финальная страховка)
            safe_title = filter_prohibited_words(
                imported_product.title[:60] if imported_product.title else 'Товар'
            )
            safe_description = filter_prohibited_words(
                imported_product.description or imported_product.title or 'Описание товара'
            )

            # Формируем variant для WB API v2
            variant = {
                'vendorCode': vendor_code,
                'title': safe_title,  # Макс 60 символов
                'description': safe_description,
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
                # WB обрабатывает создание асинхронно — нужно больше попыток с паузами
                import time as _time
                nm_id = None
                retry_delays = [2, 4, 6, 10, 15]  # 5 попыток: 2s, 4s, 6s, 10s, 15s = до 37с ожидания
                for attempt in range(len(retry_delays)):
                    try:
                        _time.sleep(retry_delays[attempt])
                        created_card = self.api_client.get_card_by_vendor_code(vendor_code)
                        nm_id = created_card.get('nmID')
                        if nm_id:
                            logger.info(f"Карточка создана с nmID: {nm_id} (попытка {attempt+1}/{len(retry_delays)})")
                            break
                    except Exception as e:
                        logger.warning(f"Попытка {attempt+1}/{len(retry_delays)} получить nmID: {e}")
                        nm_id = None

                if not nm_id:
                    # Последняя попытка: проверяем список ошибок создания
                    try:
                        errors_resp = self.api_client.get_cards_errors_list(
                            log_to_db=True,
                            seller_id=self.seller.id
                        )
                        # Поддерживаем оба формата: новый (data.items) и старый (data=[])
                        data = errors_resp.get('data', [])
                        if isinstance(data, dict):
                            error_items = data.get('items', [])
                        else:
                            error_items = data or []

                        for ec in error_items:
                            # Новый формат: errors = {"vendor_code": ["ошибка1", "ошибка2"]}
                            ec_errors = ec.get('errors', {})
                            if isinstance(ec_errors, dict) and vendor_code in ec_errors:
                                err_detail = ec_errors[vendor_code]
                                raise Exception(f"WB отклонил карточку: {err_detail}")
                            # Старый формат: vendorCode + errors (list)
                            elif ec.get('vendorCode') == vendor_code:
                                err_detail = ec.get('errors', [])
                                raise Exception(f"WB отклонил карточку: {err_detail}")
                            # Проверяем vendorCodes (массив)
                            elif vendor_code in (ec.get('vendorCodes') or []):
                                err_detail = ec_errors.get(vendor_code, ec_errors) if isinstance(ec_errors, dict) else ec_errors
                                raise Exception(f"WB отклонил карточку: {err_detail}")
                    except Exception as err_check:
                        if 'отклонил' in str(err_check):
                            raise
                        logger.debug(f"Не удалось проверить ошибки создания: {err_check}")

            except Exception as e:
                error_msg = str(e)

                # Обрабатываем известные ошибки WB
                if 'Бренда' in error_msg and 'пока нет на WB' in error_msg:
                    error_msg = f"Бренд '{product_brand}' не зарегистрирован на Wildberries. Необходимо сначала добавить бренд в личном кабинете WB или использовать существующий бренд."
                elif 'повторяющиеся Баркоды' in error_msg:
                    error_msg = f"Баркод уже используется в другой карточке или дублируется в текущей. Баркоды: {barcodes}"
                elif 'vendor code is used' in error_msg.lower() or 'Артикул продавца' in error_msg:
                    # Артикул уже существует на WB — пытаемся найти и привязать существующую карточку
                    logger.info(f"Артикул '{vendor_code}' уже используется на WB. Пытаемся привязать существующую карточку...")
                    try:
                        existing_card = self.api_client.get_card_by_vendor_code(vendor_code)
                        existing_nm_id = existing_card.get('nmID')
                        if existing_nm_id:
                            logger.info(f"Найдена существующая карточка WB: nmID={existing_nm_id} для артикула '{vendor_code}'")
                            # Привязываем как успешный импорт
                            imported_product.wb_nm_id = existing_nm_id
                            imported_product.import_status = 'completed'
                            imported_product.import_error = None
                            imported_product.imported_at = datetime.utcnow()
                            db.session.commit()

                            # Синхронизируем или создаём Product запись
                            product = Product.query.filter_by(
                                seller_id=self.seller.id,
                                nm_id=existing_nm_id
                            ).first()
                            if not product:
                                product = Product(
                                    seller_id=self.seller.id,
                                    nm_id=existing_nm_id,
                                    vendor_code=vendor_code,
                                    title=existing_card.get('title', imported_product.name or ''),
                                    brand=existing_card.get('brand', ''),
                                )
                                db.session.add(product)
                                db.session.commit()
                                logger.info(f"Создана Product запись для nmID={existing_nm_id}")

                            # Загружаем фото в существующую карточку
                            try:
                                self._upload_photos_for_card(existing_nm_id, imported_product)
                                logger.info(f"Фото загружены в существующую карточку nmID={existing_nm_id}")
                            except Exception as photo_err:
                                logger.warning(f"Не удалось загрузить фото в существующую карточку: {photo_err}")

                            return (True,
                                    f"Карточка уже существовала на WB (nmID={existing_nm_id}). Привязана к импорту.",
                                    existing_nm_id)
                    except Exception as link_err:
                        logger.warning(f"Не удалось привязать существующую карточку: {link_err}")
                    error_msg = f"Артикул '{vendor_code}' уже используется в другой карточке на WB. Синхронизируйте товары чтобы обновить базу."

                full_error = f"Ошибка создания карточки WB: {error_msg}"
                logger.error(full_error)
                try:
                    variant_json = json.dumps(
                        [{'subjectID': imported_product.wb_subject_id, 'variants': [variant]}],
                        ensure_ascii=False, indent=2
                    )
                    logger.error(f"Полный request body:\n{variant_json}")
                except Exception:
                    logger.error(f"Данные которые отправлялись: variant={variant}")
                raise Exception(full_error)

            # ============================================================
            # ПОСЛЕ СОЗДАНИЯ КАРТОЧКИ — устанавливаем цены через Prices API
            # WB обрабатывает создание асинхронно, поэтому Prices API может
            # отклонить запрос если карточка ещё не полностью зарегистрирована.
            # Делаем несколько попыток с паузой.
            # ============================================================
            calculated_price_value = None
            calculated_discount_price = None
            calculated_price_before_discount = None
            post_create_warnings = []

            if nm_id:
                price_set = False
                price_retries = [5, 5, 10]  # Пауза перед каждой попыткой (сек). Первая задержка 5с — WB обрабатывает карточку асинхронно
                for price_attempt, delay in enumerate(price_retries):
                    if delay > 0:
                        _time.sleep(delay)
                    try:
                        calculated_price_value, calculated_discount_price, calculated_price_before_discount = \
                            self._set_price_for_card(nm_id, imported_product)
                        if calculated_price_value is not None:
                            price_set = True
                            break
                        else:
                            # Цена None — нет данных для расчёта, повторять бесполезно
                            break
                    except Exception as price_err:
                        logger.warning(
                            f"Попытка {price_attempt+1}/{len(price_retries)} установки цены для nmID={nm_id}: {price_err}"
                        )
                        if price_attempt == len(price_retries) - 1:
                            logger.error(f"Не удалось установить цену для nmID={nm_id} после {len(price_retries)} попыток", exc_info=True)
                            post_create_warnings.append(f"Ошибка установки цены: {price_err}")

                if not price_set and not post_create_warnings:
                    post_create_warnings.append("Цена не установлена (нет закупочной цены или настроек ценообразования)")
            else:
                post_create_warnings.append("nmID не получен — цена и фото не установлены")

            # ============================================================
            # ПОСЛЕ СОЗДАНИЯ КАРТОЧКИ — загружаем фото через WB API
            # WB обрабатывает карточку асинхронно, поэтому нужны ретраи.
            # ============================================================
            if nm_id:
                photo_uploaded = False
                photo_retries = [0, 5, 10]  # Пауза перед каждой попыткой (0 — первая уже после задержки цены)
                for photo_attempt, photo_delay in enumerate(photo_retries):
                    if photo_delay > 0:
                        _time.sleep(photo_delay)
                    try:
                        self._upload_photos_for_card(nm_id, imported_product)
                        photo_uploaded = True
                        break
                    except Exception as photo_err:
                        logger.warning(
                            f"Попытка {photo_attempt+1}/{len(photo_retries)} загрузки фото для nmID={nm_id}: {photo_err}"
                        )
                        if photo_attempt == len(photo_retries) - 1:
                            logger.error(f"Не удалось загрузить фото для nmID={nm_id} после {len(photo_retries)} попыток", exc_info=True)
                            post_create_warnings.append(f"Ошибка загрузки фото: {photo_err}")

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
                price=calculated_price_before_discount,
                discount_price=calculated_price_value,
                supplier_price=imported_product.supplier_price,
                is_active=True,
                last_sync=datetime.utcnow()
            )

            db.session.add(product)
            db.session.flush()  # Получить ID

            # Обновляем ImportedProduct
            imported_product.product_id = product.id
            imported_product.import_status = 'imported'
            imported_product.imported_at = datetime.utcnow()
            imported_product.import_error = '; '.join(post_create_warnings) if post_create_warnings else None

            db.session.commit()

            if post_create_warnings:
                logger.warning(f"Товар {imported_product.external_id} импортирован с предупреждениями: {'; '.join(post_create_warnings)}")
            else:
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

    def _category_supports_sizes(self, wb_subject_id: int) -> bool:
        """
        Проверяет, поддерживает ли WB-категория размеры.
        Запрашивает характеристики категории и ищет поле 'Размер'.

        Returns:
            True если категория поддерживает размеры, False если безразмерная
        """
        if not wb_subject_id or not self.api_client:
            return False

        try:
            chars_config = self.api_client.get_card_characteristics_config(wb_subject_id)
            wb_chars = chars_config.get('data', [])

            for char in wb_chars:
                char_name = (char.get('name') or '').lower()
                # Ищем характеристики связанные с размерами
                if char_name in ('размер', 'рос. размер', 'размер пользователя'):
                    logger.info(f"Категория {wb_subject_id}: поддерживает размеры (найдена характеристика '{char.get('name')}')")
                    return True

            logger.info(f"Категория {wb_subject_id}: безразмерная (нет характеристики 'Размер')")
            return False
        except Exception as e:
            logger.warning(f"Не удалось проверить размеры для категории {wb_subject_id}: {e}")
            # По умолчанию считаем безразмерной — безопаснее
            return False

    @staticmethod
    def _sanitize_country(country: str) -> str:
        """
        Нормализует страну производства для WB API.
        WB не принимает составные значения типа 'Россия-Китай'.

        Returns:
            Валидное название страны
        """
        if not country:
            return ''

        # Разбиваем по разделителям: '-', '/', ','
        import re
        parts = re.split(r'[-/,;]', country)
        parts = [p.strip() for p in parts if p.strip()]

        if not parts:
            return country.strip()

        # Берём первую страну
        return parts[0]

    @staticmethod
    def _extract_dimensions_from_text(text: str) -> Dict:
        """
        Извлекает физические размеры из текста описания с помощью regex.
        Fallback когда ai_dimensions и dimensions_json пусты.

        Returns:
            Dict с ключами вроде 'length_cm', 'diameter_cm' и т.д.
        """
        import re

        if not text:
            return {}

        _DIM_PATTERNS = [
            (r'(?:общая\s+)?(?:длина|дл\.?)\s*[:=—–-]?\s*(\d+[.,]?\d*)\s*(?:см|cm)', 'length_cm'),
            (r'(?:рабочая\s+длина|раб\.?\s*дл\.?)\s*[:=—–-]?\s*(\d+[.,]?\d*)\s*(?:см|cm)', 'working_length_cm'),
            (r'(?:диаметр|диам\.?|Ø)\s*[:=—–-]?\s*(\d+[.,]?\d*)\s*(?:см|cm)', 'diameter_cm'),
            (r'(?:макс(?:имальный)?\.?\s*диаметр)\s*[:=—–-]?\s*(\d+[.,]?\d*)\s*(?:см|cm)', 'max_diameter_cm'),
            (r'(?:ширина|шир\.?)\s*[:=—–-]?\s*(\d+[.,]?\d*)\s*(?:см|cm)', 'width_cm'),
            (r'(?:высота|выс\.?)\s*[:=—–-]?\s*(\d+[.,]?\d*)\s*(?:см|cm)', 'height_cm'),
            (r'(?:глубина)\s*[:=—–-]?\s*(\d+[.,]?\d*)\s*(?:см|cm)', 'depth_cm'),
            (r'(?:глубина\s+проникновения)\s*[:=—–-]?\s*(\d+[.,]?\d*)\s*(?:см|cm)', 'working_length_cm'),
            (r'(?:обхват)\s*[:=—–-]?\s*(\d+[.,]?\d*)\s*(?:см|cm)', 'circumference_cm'),
            (r'(?:вес|масса)\s*[:=—–-]?\s*(\d+[.,]?\d*)\s*(?:г|гр|g)\b', 'weight_g'),
            (r'(?:вес|масса)\s*[:=—–-]?\s*(\d+[.,]?\d*)\s*(?:кг|kg)', 'weight_kg'),
            (r'(?:объ[её]м)\s*[:=—–-]?\s*(\d+[.,]?\d*)\s*(?:мл|ml)', 'volume_ml'),
            # мм → конвертируем в см
            (r'(?:общая\s+)?(?:длина|дл\.?)\s*[:=—–-]?\s*(\d+[.,]?\d*)\s*(?:мм|mm)', 'length_mm'),
            (r'(?:диаметр|диам\.?|Ø)\s*[:=—–-]?\s*(\d+[.,]?\d*)\s*(?:мм|mm)', 'diameter_mm'),
            (r'(?:рабочая\s+длина)\s*[:=—–-]?\s*(\d+[.,]?\d*)\s*(?:мм|mm)', 'working_length_mm'),
        ]

        dims = {}
        text_lower = text.lower()

        for pattern, key in _DIM_PATTERNS:
            m = re.search(pattern, text_lower)
            if m:
                val_str = m.group(1).replace(',', '.')
                try:
                    val = float(val_str)
                except ValueError:
                    continue

                # Конвертация мм → см
                if key.endswith('_mm'):
                    real_key = key.replace('_mm', '_cm')
                    val = round(val / 10, 1)
                elif key == 'weight_kg':
                    real_key = 'weight_g'
                    val = round(val * 1000)
                else:
                    real_key = key

                if real_key not in dims:
                    dims[real_key] = val

        if dims:
            logger.info(f"Извлечено из описания: {dims}")

        return dims

    @staticmethod
    def _assemble_chars_from_fields(imported_product: ImportedProduct) -> Dict:
        """
        Собирает словарь характеристик из отдельных полей ImportedProduct
        (colors, materials, gender, country, brand) когда characteristics пуст.
        """
        chars = {}

        # Цвет
        if imported_product.colors:
            try:
                colors = json.loads(imported_product.colors)
                if colors:
                    chars['Цвет'] = colors[0] if len(colors) == 1 else colors
            except Exception:
                pass

        # Материал / Состав — добавляем под несколькими названиями,
        # т.к. WB называет по-разному в разных категориях:
        # "Состав", "Материал", "Материал изделия", "Основной материал"
        if imported_product.materials:
            try:
                materials = json.loads(imported_product.materials)
                if materials:
                    mat_value = '; '.join(materials) if len(materials) > 1 else materials[0]
                    chars['Состав'] = mat_value
                    chars['Материал'] = mat_value
                    chars['Материал изделия'] = mat_value
            except Exception:
                pass

        # Пол
        if imported_product.gender:
            gender = imported_product.gender
            gender_map = {
                'male': 'Мужской', 'female': 'Женский', 'unisex': 'Унисекс',
                'для женщин и мужчин': 'Унисекс', 'для женщин': 'Женский', 'для мужчин': 'Мужской',
                'мужской': 'Мужской', 'женский': 'Женский', 'унисекс': 'Унисекс',
            }
            chars['Пол'] = gender_map.get(gender.lower(), gender)

        # Страна производства (WB не принимает составные: "Россия-Китай")
        if imported_product.country:
            chars['Страна производства'] = WBProductImporter._sanitize_country(imported_product.country)

        # Бренд
        if imported_product.brand:
            chars['Бренд'] = imported_product.brand

        if chars:
            logger.info(f"Товар {imported_product.external_id}: собрано {len(chars)} характеристик из отдельных полей")

        return chars

    @staticmethod
    def _collect_ai_characteristics(imported_product: ImportedProduct) -> Dict:
        """
        Собирает характеристики из AI-обогащённых полей ImportedProduct.
        Используется как дополнительный источник, если основные характеристики неполные.
        """
        chars = {}

        # AI-определённые цвета
        if imported_product.ai_colors:
            try:
                ai_colors = json.loads(imported_product.ai_colors)
                if ai_colors and isinstance(ai_colors, list):
                    chars['Цвет'] = ai_colors[0] if len(ai_colors) == 1 else ai_colors
            except Exception:
                pass

        # AI-определённые материалы
        if imported_product.ai_materials:
            try:
                ai_materials = json.loads(imported_product.ai_materials)
                if ai_materials and isinstance(ai_materials, list):
                    chars['Состав'] = '; '.join(ai_materials) if len(ai_materials) > 1 else ai_materials[0]
                elif ai_materials and isinstance(ai_materials, str):
                    chars['Состав'] = ai_materials
            except Exception:
                pass

        # AI-определённый пол
        if imported_product.ai_gender:
            gender_map = {
                'male': 'Мужской', 'female': 'Женский', 'unisex': 'Унисекс',
            }
            chars.setdefault('Пол', gender_map.get(imported_product.ai_gender.lower(), imported_product.ai_gender))

        # AI-определённая страна
        if imported_product.ai_country:
            chars.setdefault('Страна производства', WBProductImporter._sanitize_country(imported_product.ai_country))

        # AI-определённый сезон
        if imported_product.ai_season:
            season_map = {
                'all_season': 'Всесезонный', 'summer': 'Лето', 'winter': 'Зима',
                'demi': 'Демисезон', 'spring': 'Весна', 'autumn': 'Осень',
            }
            chars.setdefault('Сезон', season_map.get(imported_product.ai_season.lower(), imported_product.ai_season))

        # AI-определённые атрибуты (полный набор)
        if imported_product.ai_attributes:
            try:
                ai_attrs = json.loads(imported_product.ai_attributes)
                if isinstance(ai_attrs, dict):
                    for key, val in ai_attrs.items():
                        if key not in chars and val and not key.startswith('_'):
                            chars[key] = val
            except Exception:
                pass

        # AI-определённые физические размеры (длина, диаметр, объём и т.д.)
        # Маппинг: внутренний ключ → список возможных WB-названий (от точного к общему)
        # WB разные категории называют одну характеристику по-разному
        # Маппинг: внутренний ключ → список WB-названий для создания промежуточных chars.
        # ВАЖНО: короткое базовое имя ПЕРВЫМ — оно лучше матчится через
        # нормализованное частичное совпадение ("диаметр" in "диаметр секс игрушки").
        # Более точные имена идут после — для категорий где WB-имя точно совпадает.
        _DIM_TO_WB = {
            'length_cm': ['Длина', 'Длина, см', 'Длина изделия, см'],
            'width_cm': ['Ширина', 'Ширина, см', 'Ширина изделия, см'],
            'height_cm': ['Высота', 'Высота, см', 'Высота изделия, см'],
            'depth_cm': ['Глубина', 'Глубина, см'],
            'diameter_cm': ['Диаметр', 'Диаметр, см'],
            'max_diameter_cm': ['Максимальный диаметр', 'Максимальный диаметр, см'],
            'min_diameter_cm': ['Минимальный диаметр', 'Минимальный диаметр, см'],
            'working_length_cm': ['Рабочая длина', 'Рабочая длина, см', 'Глубина проникновения'],
            'circumference_cm': ['Обхват', 'Обхват, см'],
            'weight_g': ['Вес товара', 'Вес товара, г', 'Вес, г'],
            'volume_ml': ['Объем', 'Объем, мл', 'Объём, мл'],
        }
        ai_dims = None
        # Источник 1: ai_dimensions (заполняется при AI-парсинге)
        if imported_product.ai_dimensions:
            try:
                ai_dims = json.loads(imported_product.ai_dimensions) if isinstance(imported_product.ai_dimensions, str) else imported_product.ai_dimensions
            except Exception:
                pass
        # Источник 2: dimensions_json из SupplierProduct
        if not ai_dims and hasattr(imported_product, 'supplier_product') and imported_product.supplier_product:
            sp = imported_product.supplier_product
            if hasattr(sp, 'dimensions_json') and sp.dimensions_json:
                try:
                    ai_dims = json.loads(sp.dimensions_json) if isinstance(sp.dimensions_json, str) else sp.dimensions_json
                except Exception:
                    pass
        # Источник 3: парсим размеры regex-ом прямо из описания товара
        if not ai_dims:
            ai_dims = WBProductImporter._extract_dimensions_from_text(imported_product.description or '')

        if ai_dims and isinstance(ai_dims, dict):
            for dim_key, wb_names in _DIM_TO_WB.items():
                val = ai_dims.get(dim_key)
                if not val:
                    continue
                # Пробуем каждое возможное WB-название, первое не занятое используем
                for wb_name in wb_names:
                    if wb_name not in chars:
                        chars[wb_name] = val
                        break

        if chars:
            logger.info(f"Товар {imported_product.external_id}: собрано {len(chars)} AI-характеристик")

        return chars

    def _resolve_brand_for_wb(self, imported_product: ImportedProduct) -> str:
        """
        Резолвит бренд для отправки в WB, используя систему брендов.

        Приоритет:
        1. Если есть resolved_brand_id — берём MarketplaceBrand.marketplace_brand_name для WB
        2. Если нет resolved_brand_id — запускаем BrandEngine.resolve()
        3. Fallback — сырое имя бренда из imported_product.brand
        """
        raw_brand = (imported_product.brand or '').strip()
        if not raw_brand:
            return 'NoName'

        # Step 1: Если бренд уже зарезолвлен (resolved_brand_id установлен)
        if imported_product.resolved_brand_id:
            try:
                from models import Brand, MarketplaceBrand, Marketplace
                brand = Brand.query.get(imported_product.resolved_brand_id)
                if brand and brand.status != 'rejected':
                    # Ищем маркетплейс-специфичное имя для WB
                    wb_mp = Marketplace.query.filter_by(code='wb').first()
                    if wb_mp:
                        mp_brand = MarketplaceBrand.query.filter_by(
                            brand_id=brand.id,
                            marketplace_id=wb_mp.id
                        ).first()
                        if mp_brand and mp_brand.status != 'rejected' and mp_brand.marketplace_brand_name:
                            logger.info(
                                f"Бренд '{raw_brand}' → MarketplaceBrand: '{mp_brand.marketplace_brand_name}' "
                                f"(brand_id={brand.id}, mp_brand_id={mp_brand.marketplace_brand_id})"
                            )
                            return mp_brand.marketplace_brand_name
                    # Нет MarketplaceBrand для WB — используем каноническое имя
                    logger.info(
                        f"Бренд '{raw_brand}' → каноническое имя: '{brand.name}' (brand_id={brand.id})"
                    )
                    return brand.name
                elif brand and brand.status == 'rejected':
                    logger.warning(
                        f"Бренд '{raw_brand}' (brand_id={brand.id}) отклонён, используем сырое имя"
                    )
            except Exception as e:
                logger.warning(f"Ошибка при поиске resolved бренда: {e}")

        # Step 2: Пробуем BrandEngine.resolve()
        try:
            from services.brand_engine import get_brand_engine
            from models import Marketplace
            engine = get_brand_engine()

            wb_mp = Marketplace.query.filter_by(code='wb').first()
            mp_id = wb_mp.id if wb_mp else None

            resolution = engine.resolve(
                raw_brand,
                marketplace_id=mp_id,
                category_id=imported_product.wb_subject_id,
                marketplace_client=self.api_client,
            )

            if resolution.status in ('exact', 'confident'):
                # Используем маркетплейс-специфичное имя если есть, иначе каноническое
                resolved_name = resolution.marketplace_brand_name or resolution.canonical_name
                if resolved_name:
                    logger.info(
                        f"BrandEngine: '{raw_brand}' → '{resolved_name}' "
                        f"(status={resolution.status}, confidence={resolution.confidence})"
                    )
                    # Обновляем resolved_brand_id в ImportedProduct для будущих запросов
                    if resolution.brand_id and not imported_product.resolved_brand_id:
                        imported_product.resolved_brand_id = resolution.brand_id
                        imported_product.brand_status = resolution.status
                        try:
                            db.session.commit()
                        except Exception:
                            db.session.rollback()
                    return resolved_name

            logger.info(
                f"BrandEngine: '{raw_brand}' не резолвлен (status={resolution.status}), "
                f"используем сырое имя"
            )
        except Exception as e:
            logger.warning(f"BrandEngine недоступен: {e}, используем сырое имя")

        # Step 3: Fallback — сырой бренд
        return raw_brand

    def _build_wb_characteristics(self, imported_product: ImportedProduct) -> List[Dict]:
        """
        Строит массив характеристик в формате WB API

        Args:
            imported_product: Импортированный товар

        Returns:
            Список характеристик в формате [{id: int, value: [str]}]
        """
        import re

        if not imported_product.wb_subject_id:
            logger.warning(f"Товар {imported_product.external_id}: нет wb_subject_id для получения характеристик")
            return []

        # Загружаем сохранённые характеристики
        product_chars = {}
        try:
            if imported_product.characteristics:
                product_chars = json.loads(imported_product.characteristics) if isinstance(imported_product.characteristics, str) else imported_product.characteristics
        except Exception as e:
            logger.warning(f"Ошибка парсинга характеристик: {e}")
            product_chars = {}

        # Всегда дополняем из отдельных полей ImportedProduct (цвет, материал, пол и т.д.)
        field_chars = self._assemble_chars_from_fields(imported_product)
        for key, val in field_chars.items():
            if key not in product_chars:
                product_chars[key] = val

        # Дополняем данными из AI-анализа (если есть)
        ai_chars = self._collect_ai_characteristics(imported_product)
        for key, val in ai_chars.items():
            if key not in product_chars:
                product_chars[key] = val

        if not product_chars:
            logger.info(f"Товар {imported_product.external_id}: нет сохранённых характеристик")
            return []

        # Получаем конфигурацию характеристик WB для данного предмета
        try:
            chars_config = self.api_client.get_card_characteristics_config(imported_product.wb_subject_id)
            wb_chars_list = chars_config.get('data', [])
        except Exception as e:
            logger.error(f"Не удалось получить конфигурацию характеристик: {e}")
            return []

        if not wb_chars_list:
            logger.warning(f"Пустая конфигурация характеристик для subject_id={imported_product.wb_subject_id}")
            return []

        # Загружаем справочники WB из БД для характеристик с пустым dictionary
        wb_directories = self._load_wb_directories()

        # Создаём словарь: name -> {id, type, dictionary, ...}
        wb_chars_by_name = {}
        for char in wb_chars_list:
            char_name = char.get('name', '').strip()
            if char_name:
                # Если dictionary пуст — подставляем из справочника
                if not char.get('dictionary'):
                    dir_type = self._get_directory_type_for_char(char_name)
                    if dir_type and dir_type in wb_directories:
                        dir_data = wb_directories[dir_type]
                        # Преобразуем в формат dictionary: [{value: "..."}]
                        dict_items = []
                        for entry in dir_data:
                            if isinstance(entry, dict):
                                val = entry.get('name') or entry.get('value') or entry.get('id')
                            elif isinstance(entry, str):
                                val = entry
                            else:
                                continue
                            if val:
                                dict_items.append({'value': val})
                        if dict_items:
                            char['dictionary'] = dict_items
                            logger.info(f"Подставлен справочник '{dir_type}' ({len(dict_items)} значений) для характеристики '{char_name}'")

                wb_chars_by_name[char_name.lower()] = char

        # Словарь алиасов: наши названия характеристик -> WB названия
        # Решает проблему несовпадения имён между данными поставщика и WB API
        # Алиасы строят маппинг: наше_название -> wb_название (lowercased).
        # Сначала пробуем точное совпадение по алиасу, потом partial matching.
        # ВАЖНО: алиас должен указывать на ТОЧНОЕ имя WB-характеристики (lowercase).
        # Если указать просто 'материал', а в WB есть 'материал изделия' — не найдёт.
        # Поэтому для неоднозначных случаев лучше полагаться на partial matching (шаг 2).
        _CHAR_ALIASES = {
            # === Цвет ===
            'цвет товара': 'цвет',
            'цвета': 'цвет',
            'основной цвет': 'цвет',

            # === Материал / Состав ===
            # WB называет по-разному в разных категориях:
            # "Материал изделия", "Материал презерватива", "Состав", "Материал"
            # Partial matching (шаг 2) ловит "материал" in "материал изделия"/"материал презерватива"
            'материал товара': 'материал изделия',
            'основной материал': 'материал изделия',
            'состав материала': 'материал изделия',
            # 'состав' и 'материал' обрабатываются через partial matching

            # === Страна ===
            'страна': 'страна производства',
            'страна-производитель': 'страна производства',
            'страна изготовления': 'страна производства',
            'страна изготовитель': 'страна производства',
            'производство': 'страна производства',
            'made in': 'страна производства',

            # === Пол ===
            'пол товара': 'пол',
            'назначение': 'пол',
            'для кого': 'пол',
            'целевой пол': 'пол',

            # === Комплектация ===
            'комплектация товара': 'комплектация',
            'в комплекте': 'комплектация',

            # === Размеры / Габариты ===
            # WB категории секс-товаров используют специфические имена:
            # "Общая длина секс игрушки", "Диаметр секс игрушки"
            # Partial matching (шаг 2) ловит "длина" in "общая длина секс игрушки"
            'длина товара': 'длина',
            'длина изделия': 'длина',
            'общая длина': 'длина',
            'рабочая длина': 'рабочая длина',
            'диаметр товара': 'диаметр',

            # === Вес ===
            'вес товара': 'вес товара без упаковки (г)',
            'масса': 'вес товара без упаковки (г)',
            'вес изделия': 'вес товара без упаковки (г)',
            'вес нетто': 'вес товара без упаковки (г)',

            # === Объем ===
            'объем товара': 'объем',
            'объём': 'объем',

            # === Количество ===
            'количество в упаковке': 'количество предметов в упаковке (шт.)',
            'кол-во в упаковке': 'количество предметов в упаковке (шт.)',
            'кол-во': 'количество предметов в упаковке (шт.)',
            'количество штук': 'количество предметов в упаковке (шт.)',

            # === Режимы ===
            'количество скоростей': 'количество режимов',
            'кол-во режимов': 'количество режимов',
            'количество режимов вибрации': 'количество режимов',

            # === Питание ===
            'тип питания': 'тип элемента питания',
            'питание': 'тип элемента питания',
            'батарейки': 'тип элемента питания',
            'элемент питания': 'тип элемента питания',

            # === Особенности ===
            # WB: "Особенности секс игрушки", "Особенности презерватива"
            # Partial matching ловит "особенности" in "особенности секс игрушки"
            'особенности товара': 'особенности',
            'feature': 'особенности',
            'описание особенностей': 'особенности',

            # === Упаковка ===
            'вид упаковки': 'упаковка',
            'тип упаковки': 'упаковка',

            # === Прочее ===
            'водонепроницаемость': 'водонепроницаемый',
            'защита от воды': 'водонепроницаемый',

            # === Ширина ===
            'ширина товара': 'ширина предмета',
            'ширина изделия': 'ширина предмета',

            # === Текстура (для презервативов) ===
            'текстура': 'текстура презерватива',
            'поверхность': 'текстура презерватива',

            # === Аромат ===
            'запах': 'аромат',
            'аромат товара': 'аромат',
        }

        result_characteristics = []
        used_char_ids = set()  # Дедупликация: не добавляем одну и ту же WB-характеристику дважды

        for char_name, char_value in product_chars.items():
            # Пропускаем служебные поля
            if char_name.startswith('_'):
                continue

            # Санитизация: WB не принимает составные страны ("Россия-Китай")
            if char_name.lower() in ('страна производства', 'страна'):
                if isinstance(char_value, str):
                    char_value = self._sanitize_country(char_value)

            # Находим соответствующую характеристику WB
            char_name_lower = char_name.lower().strip()
            wb_char = wb_chars_by_name.get(char_name_lower)

            # Шаг 1: Проверяем алиасы
            if not wb_char:
                alias = _CHAR_ALIASES.get(char_name_lower)
                if alias:
                    wb_char = wb_chars_by_name.get(alias)
                    if wb_char:
                        logger.debug(f"Характеристика '{char_name}' -> алиас '{alias}'")

            # Шаг 2: Частичное совпадение
            if not wb_char:
                for wb_name, wb_data in wb_chars_by_name.items():
                    if char_name_lower in wb_name or wb_name in char_name_lower:
                        wb_char = wb_data
                        break

            # Шаг 3: Нормализация единиц измерения
            if not wb_char:
                _UNIT_RE = re.compile(r'[,(]\s*(?:см|мм|г|гр|кг|мл|л|шт|м)\s*\)?$', re.IGNORECASE)
                char_norm = _UNIT_RE.sub('', char_name_lower).strip()
                if char_norm:
                    # Сначала пробуем алиас нормализованного имени
                    alias = _CHAR_ALIASES.get(char_norm)
                    if alias:
                        wb_char = wb_chars_by_name.get(alias)

                    if not wb_char:
                        for wb_name, wb_data in wb_chars_by_name.items():
                            wb_norm = _UNIT_RE.sub('', wb_name).strip()
                            if char_norm == wb_norm:
                                wb_char = wb_data
                                break

                    if not wb_char and char_norm:
                        for wb_name, wb_data in wb_chars_by_name.items():
                            wb_norm = _UNIT_RE.sub('', wb_name).strip()
                            if char_norm in wb_norm or wb_norm in char_norm:
                                wb_char = wb_data
                                break

            # Шаг 4: Пробуем по первому слову (для длинных названий)
            if not wb_char and ' ' in char_name_lower:
                first_word = char_name_lower.split()[0]
                if len(first_word) >= 4:  # Минимум 4 символа чтобы не ловить "тип", "вид" и т.д.
                    for wb_name, wb_data in wb_chars_by_name.items():
                        if wb_name.startswith(first_word):
                            wb_char = wb_data
                            logger.debug(f"Характеристика '{char_name}' -> совпадение по первому слову с '{wb_name}'")
                            break

            if not wb_char:
                logger.debug(f"Характеристика '{char_name}' не найдена в WB конфиге (subject_id={imported_product.wb_subject_id})")
                continue

            char_id = wb_char.get('charcID') or wb_char.get('id')
            if not char_id:
                continue

            # Дедупликация: пропускаем если эта WB-характеристика уже добавлена
            # (например, "Состав", "Материал", "Материал изделия" все маппятся на одну WB-характеристику)
            if char_id in used_char_ids:
                logger.debug(f"Характеристика '{char_name}' -> id={char_id} уже добавлена, пропускаем дубликат")
                continue

            # Получаем maxCount - максимальное количество значений для характеристики
            max_count = wb_char.get('maxCount', 1)

            # Форматируем значение
            charc_type = wb_char.get('charcType', 1)
            formatted_value = self._format_char_value(char_value, wb_char)

            if formatted_value is not None and formatted_value != '':
                # charcType=4 (числовой) — WB ожидает число, НЕ массив
                if charc_type == 4:
                    if isinstance(formatted_value, list):
                        formatted_value = formatted_value[0]
                    # Убеждаемся что это число
                    if isinstance(formatted_value, str):
                        try:
                            n = float(formatted_value)
                            formatted_value = int(n) if n == int(n) else n
                        except ValueError:
                            logger.warning(f"Характеристика '{char_name}': не удалось привести '{formatted_value}' к числу")
                            continue
                else:
                    # charcType=1 (строки) — WB ожидает массив строк
                    if not isinstance(formatted_value, list):
                        formatted_value = [str(formatted_value)]
                    else:
                        formatted_value = [str(v) for v in formatted_value]

                    # ВАЖНО: Обрезаем до maxCount (например, Особенности max 3)
                    if max_count > 0 and len(formatted_value) > max_count:
                        logger.info(f"Характеристика '{char_name}': обрезаем с {len(formatted_value)} до {max_count} значений (maxCount)")
                        formatted_value = formatted_value[:max_count]

                result_characteristics.append({
                    'id': int(char_id),
                    'value': formatted_value
                })
                used_char_ids.add(char_id)
                logger.debug(f"Характеристика '{char_name}' -> id={char_id}, value={formatted_value}, charcType={charc_type}")

        logger.info(f"Товар {imported_product.external_id}: подготовлено {len(result_characteristics)} характеристик для WB")
        return result_characteristics

    def _format_char_value(self, value, wb_char: Dict) -> any:
        """
        Форматирует значение характеристики для WB API

        Args:
            value: Значение из наших данных
            wb_char: Конфигурация характеристики WB

        Returns:
            Отформатированное значение (строка, число или список)
        """
        import re

        charc_type = wb_char.get('charcType', 1)  # 1=строки, 4=число
        unit_name = wb_char.get('unitName', '')
        dictionary = wb_char.get('dictionary', [])
        char_name = wb_char.get('name', '')

        # Если значение уже список - обрабатываем каждый элемент
        if isinstance(value, list):
            return [self._format_single_value(v, charc_type, unit_name, dictionary, char_name) for v in value if v]

        return self._format_single_value(value, charc_type, unit_name, dictionary, char_name)

    def _format_single_value(self, value, charc_type, unit_name: str, dictionary: List, char_name: str):
        """
        Форматирует одиночное значение характеристики.
        charc_type: int (1=массив строк, 4=число) или str для legacy
        """
        import re

        if value is None:
            return None

        str_value = str(value).strip()
        if not str_value:
            return None

        # Для числовых типов (charcType=4) - извлекаем число и возвращаем как число
        is_numeric = (charc_type == 4) or (isinstance(charc_type, str) and charc_type in ('numeric', 'integer', 'float', 'number'))
        if is_numeric:
            # Убираем единицы измерения и извлекаем число
            # "6 шт" -> 6, "10 мм" -> 10, "0.1 кг" -> 0.1, "250 мл" -> 250
            match = re.search(r'([\d.,]+)', str_value)
            if match:
                num_str = match.group(1).replace(',', '.')
                try:
                    num = float(num_str)
                    if num == int(num):
                        return int(num)
                    return num
                except:
                    pass
            # Если не удалось распарсить, пробуем вернуть как число напрямую
            if isinstance(value, (int, float)):
                return value
            # ВАЖНО: НЕ возвращаем строку для числовой характеристики (charcType=4)!
            # WB отклонит карточку: "Числовое значение характеристики ... не должно быть строкой"
            logger.warning(f"Числовая характеристика '{char_name}': не удалось извлечь число из '{str_value}', пропускаем")
            return None

        # Для словарных значений - ищем точное или частичное совпадение
        if dictionary:
            str_value_lower = str_value.lower()

            # Сначала ищем точное совпадение
            for dict_item in dictionary:
                if isinstance(dict_item, dict):
                    dict_value = dict_item.get('value', '')
                else:
                    dict_value = str(dict_item)

                if dict_value.lower() == str_value_lower:
                    return dict_value

            # Ищем частичное совпадение
            for dict_item in dictionary:
                if isinstance(dict_item, dict):
                    dict_value = dict_item.get('value', '')
                else:
                    dict_value = str(dict_item)

                if str_value_lower in dict_value.lower() or dict_value.lower() in str_value_lower:
                    return dict_value

        # Возвращаем как есть
        return str_value

    def _load_wb_directories(self) -> Dict[str, list]:
        """
        Загружает справочники WB из БД (MarketplaceDirectory).
        Возвращает dict: directory_type -> list of entries.
        """
        directories = {}
        try:
            marketplace = Marketplace.query.filter_by(code='wb').first()
            if not marketplace:
                return directories
            for md in MarketplaceDirectory.query.filter_by(marketplace_id=marketplace.id).all():
                try:
                    data = json.loads(md.data_json) if md.data_json else []
                    directories[md.directory_type] = data
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Не удалось загрузить справочники из БД: {e}")
        return directories

    @staticmethod
    def _get_directory_type_for_char(char_name: str) -> Optional[str]:
        """Определяет тип справочника по названию характеристики."""
        name_lower = char_name.lower().strip()
        if 'цвет' in name_lower:
            return 'colors'
        elif 'стран' in name_lower and 'производ' in name_lower:
            return 'countries'
        elif name_lower in ('пол', 'пол товара'):
            return 'kinds'
        elif 'сезон' in name_lower:
            return 'seasons'
        elif 'тнвэд' in name_lower or 'тн вэд' in name_lower:
            return 'tnved'
        return None

    def _set_price_for_card(self, nm_id: int, imported_product: ImportedProduct) -> tuple:
        """
        Рассчитывает и устанавливает цену на WB карточку по формуле ценообразования продавца.

        Args:
            nm_id: Артикул WB (nmID)
            imported_product: Товар с данными

        Returns:
            Tuple[final_price, discount_price, price_before_discount] или (None, None, None)
        """
        # Проверяем наличие уже рассчитанных цен (приоритет — они могут быть заданы
        # вручную или рассчитаны при импорте, даже если supplier_price нулевая)
        if imported_product.calculated_price and imported_product.calculated_price > 0:
            final_price = int(imported_product.calculated_price)
            price_before = int(imported_product.calculated_price_before_discount or final_price * 1.55)
            discount_pct = max(0, min(99, int((1 - final_price / price_before) * 100))) if price_before > 0 else 0

            prices_data = [{
                'nmID': nm_id,
                'price': price_before,
                'discount': discount_pct
            }]
            self.api_client.upload_prices_v2(
                prices=prices_data,
                log_to_db=True,
                seller_id=self.seller.id
            )
            logger.info(
                f"Цена из calculated_price для nmID={nm_id}: "
                f"price={price_before}, discount={discount_pct}% (final~{final_price})"
            )
            return final_price, imported_product.calculated_discount_price, price_before

        supplier_price = imported_product.supplier_price
        if not supplier_price or supplier_price <= 0:
            logger.warning(
                f"Товар {imported_product.external_id}: нет ни calculated_price, "
                f"ни supplier_price — цена НЕ установлена для nmID={nm_id}"
            )
            return None, None, None

        # Загружаем настройки ценообразования продавца
        pricing_settings = PricingSettings.query.filter_by(seller_id=self.seller.id).first()
        if not pricing_settings or not pricing_settings.is_enabled:
            logger.info(f"Ценообразование не настроено/отключено для продавца {self.seller.id}")
            # Даже без формулы, если есть рассчитанные цены в ImportedProduct - используем их
            if imported_product.calculated_price and imported_product.calculated_price > 0:
                final_price = int(imported_product.calculated_price)
                price_before = int(imported_product.calculated_price_before_discount or final_price * 1.55)
                discount_pct = max(0, min(99, int((1 - final_price / price_before) * 100))) if price_before > 0 else 0

                prices_data = [{
                    'nmID': nm_id,
                    'price': price_before,
                    'discount': discount_pct
                }]
                self.api_client.upload_prices_v2(
                    prices=prices_data,
                    log_to_db=True,
                    seller_id=self.seller.id
                )
                logger.info(f"Цена установлена для nmID={nm_id}: price={price_before}, discount={discount_pct}% (final~{final_price})")
                return final_price, imported_product.calculated_discount_price, price_before

            # Фолбэк: рассчитываем минимальную цену из supplier_price
            # Используем стандартную наценку x2.5 от закупки, скидка 35%
            logger.warning(f"Ценообразование не настроено — используем фолбэк наценку для товара {imported_product.external_id}")
            final_price = int(supplier_price * 2.5)
            price_before = int(final_price * 1.55)
            discount_pct = max(0, min(99, int((1 - final_price / price_before) * 100))) if price_before > 0 else 0

            prices_data = [{
                'nmID': nm_id,
                'price': price_before,
                'discount': discount_pct
            }]
            try:
                self.api_client.upload_prices_v2(
                    prices=prices_data,
                    log_to_db=True,
                    seller_id=self.seller.id
                )
                logger.info(
                    f"Фолбэк-цена установлена для nmID={nm_id}: "
                    f"supplier_price={supplier_price}, price_before={price_before}, "
                    f"discount={discount_pct}%, final~{final_price}"
                )
                imported_product.calculated_price = final_price
                imported_product.calculated_price_before_discount = price_before
                return final_price, 0, price_before
            except Exception as e:
                logger.error(f"Не удалось установить фолбэк-цену для nmID={nm_id}: {e}")
                return None, None, None

        # Рассчитываем цену по формуле
        price_result = calculate_price(
            purchase_price=supplier_price,
            settings=pricing_settings,
            product_id=imported_product.id
        )

        if not price_result:
            logger.warning(f"Не удалось рассчитать цену для товара {imported_product.external_id} (supplier_price={supplier_price})")
            return None, None, None

        final_price = price_result['final_price']  # Z — итоговая цена
        price_before_discount = price_result['price_before_discount']  # Y — завышенная цена
        discount_price = price_result.get('discount_price', 0)  # X — цена с SPP

        # Вычисляем скидку в процентах: discount = (1 - Z/Y) * 100
        if price_before_discount > 0:
            discount_pct = max(0, min(99, int((1 - final_price / price_before_discount) * 100)))
        else:
            discount_pct = 0

        # Отправляем цену через Prices API v2
        prices_data = [{
            'nmID': nm_id,
            'price': int(price_before_discount),  # Цена до скидки (Y)
            'discount': discount_pct  # Скидка в % чтобы итоговая = Z
        }]

        self.api_client.upload_prices_v2(
            prices=prices_data,
            log_to_db=True,
            seller_id=self.seller.id
        )

        logger.info(
            f"Цена установлена для nmID={nm_id}: "
            f"supplier_price={supplier_price}, "
            f"price_before_discount(Y)={price_before_discount}, "
            f"discount={discount_pct}%, "
            f"final_price(Z)={final_price}, "
            f"discount_price(X)={discount_price}"
        )

        # Обновляем рассчитанные цены в ImportedProduct
        imported_product.calculated_price = final_price
        imported_product.calculated_discount_price = discount_price
        imported_product.calculated_price_before_discount = price_before_discount

        return final_price, discount_price, price_before_discount

    def _upload_photos_for_card(self, nm_id: int, imported_product: ImportedProduct):
        """
        Загружает фотографии товара в WB карточку.

        Стратегия (приоритетная — file upload, т.к. надёжнее):
        1. Сначала кэшируем все фото локально (скачиваем с поставщика).
        2. Загружаем через /media/file (multipart upload) — самый надёжный способ,
           не зависит от доступности нашего сервера из WB.
        3. Если file upload провалился — пробуем URL-based /media/save как fallback
           (работает только если PUBLIC_BASE_URL доступен из интернета).

        Args:
            nm_id: Артикул WB (nmID)
            imported_product: Товар с данными
        """
        if not imported_product.photo_urls:
            logger.warning(f"Товар {imported_product.external_id}: photo_urls пусто — фото НЕ будут загружены на WB")
            return

        try:
            photo_urls_raw = json.loads(imported_product.photo_urls)
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"Товар {imported_product.external_id}: не удалось разобрать photo_urls")
            return

        if not photo_urls_raw:
            return

        # Получаем supplier product
        sp = getattr(imported_product, 'supplier_product', None)
        if not sp and imported_product.supplier_product_id:
            from models import SupplierProduct
            sp = SupplierProduct.query.get(imported_product.supplier_product_id)

        # =============================================
        # Шаг 1: Кэшируем фото локально
        # =============================================
        if sp:
            cached_photo_paths = self._ensure_photos_cached(photo_urls_raw, sp)
        else:
            cached_photo_paths = self._ensure_photos_cached_direct(photo_urls_raw, imported_product)

        # =============================================
        # Добавляем глобальные медиа продавца (брендовые слайды, сертификаты и т.п.)
        # =============================================
        try:
            from routes.product_defaults import get_defaults_for_product
            _defaults = get_defaults_for_product(self.seller.id, imported_product.wb_subject_id)
            global_media_list = _defaults.get('global_media', [])
            if global_media_list:
                import os
                from flask import current_app
                media_dir = os.path.join(current_app.root_path, 'data', 'global_media', str(self.seller.id))
                for media_item in global_media_list:
                    if media_item.get('type') == 'photo':
                        media_path = os.path.join(media_dir, media_item['filename'])
                        if os.path.exists(media_path):
                            cached_photo_paths.append(media_path)
                            logger.info(f"Добавлено глобальное медиа: {media_item.get('original_name', media_item['filename'])}")
        except Exception as gm_err:
            logger.warning(f"Не удалось добавить глобальные медиа: {gm_err}")

        # =============================================
        # Стратегия 1 (приоритетная): File-based upload (media/file)
        # Загружаем файлы напрямую через multipart — не зависит от PUBLIC_BASE_URL
        # =============================================
        if cached_photo_paths:
            logger.info(f"Загружаем {len(cached_photo_paths)} фото (file upload) для nmID={nm_id}")
            try:
                results = self.api_client.upload_photos_to_card(
                    nm_id=nm_id,
                    photo_paths=cached_photo_paths,
                    seller_id=self.seller.id
                )
                success_count = sum(1 for r in results if r.get('success'))
                logger.info(f"Фото загружено для nmID={nm_id}: {success_count}/{len(cached_photo_paths)} успешно")
                if success_count > 0:
                    return  # Успех — выходим
                else:
                    logger.warning(f"File upload: 0/{len(cached_photo_paths)} фото загружено, пробуем URL-based")
            except Exception as e:
                logger.warning(f"File upload failed для nmID={nm_id}: {e}, пробуем URL-based")

        # =============================================
        # Стратегия 2 (fallback): URL-based upload (media/save)
        # WB сам скачивает фото по публичным URL нашего сервера.
        # Работает только если PUBLIC_BASE_URL доступен из интернета.
        # =============================================
        from flask import current_app
        public_base_url = current_app.config.get('PUBLIC_BASE_URL', '')

        if public_base_url:
            from routes.photos import generate_public_photo_urls
            public_urls = generate_public_photo_urls(imported_product)

            if public_urls:
                logger.info(
                    f"Товар {imported_product.external_id}: загрузка {len(public_urls)} фото "
                    f"через media/save (URL-based) для nmID={nm_id}"
                )
                logger.info(f"  Пример URL: {public_urls[0][:100]}...")
                try:
                    result = self.api_client.upload_photos_by_url(
                        nm_id=nm_id,
                        photo_urls=public_urls,
                        seller_id=self.seller.id
                    )
                    logger.info(f"Фото отправлены по URL для nmID={nm_id}: {result}")
                    return
                except Exception as e:
                    logger.error(f"URL-based upload также failed для nmID={nm_id}: {e}")

        if not cached_photo_paths:
            logger.warning(f"Товар {imported_product.external_id}: ни одно фото не удалось получить для загрузки")

        # Если ни одна стратегия не сработала — бросаем исключение для ретрая
        raise Exception(f"Все стратегии загрузки фото провалились для nmID={nm_id}")

    def _ensure_photos_cached(self, photo_urls_raw: list, sp) -> list:
        """
        Убеждается что все фото скачаны и лежат в кэше.
        Возвращает список путей к кэшированным файлам.
        """
        from services.photo_cache import get_photo_cache
        cache = get_photo_cache()

        supplier_type = sp.supplier.code if sp.supplier else 'unknown'
        external_id = sp.external_id or ''

        cached_paths = []
        for idx, ph in enumerate(photo_urls_raw):
            if isinstance(ph, dict):
                url = ph.get('sexoptovik') or ph.get('original') or ph.get('blur')
            elif isinstance(ph, str):
                url = ph
            else:
                continue

            if not url:
                continue

            if cache.is_cached(supplier_type, external_id, url):
                cached_paths.append(cache.get_cache_path(supplier_type, external_id, url))
            else:
                try:
                    photo_bytes = self._download_photo(url, ph, sp.supplier)
                    if photo_bytes:
                        cache.save_to_cache(supplier_type, external_id, url, photo_bytes)
                        cached_paths.append(cache.get_cache_path(supplier_type, external_id, url))
                    else:
                        logger.warning(f"Фото {idx+1}: _download_photo вернул None для {url[:80]}")
                except Exception as dl_err:
                    logger.warning(f"Не удалось скачать фото {idx+1} ({url[:60]}): {dl_err}")

        logger.info(f"Кэш фото: {len(cached_paths)}/{len(photo_urls_raw)} скачано")
        return cached_paths

    def _ensure_photos_cached_direct(self, photo_urls_raw: list, imported_product) -> list:
        """
        Фолбэк: скачивает фото напрямую из URL без SupplierProduct.
        Используется когда supplier_product_id = None.
        """
        from services.photo_cache import get_photo_cache
        cache = get_photo_cache()

        supplier_type = 'imported'
        external_id = str(imported_product.external_id or imported_product.id)

        # Получаем supplier для auth cookies (если есть)
        supplier = None
        if imported_product.supplier:
            supplier = imported_product.supplier

        cached_paths = []
        for idx, ph in enumerate(photo_urls_raw):
            if isinstance(ph, dict):
                url = ph.get('sexoptovik') or ph.get('original') or ph.get('blur')
            elif isinstance(ph, str):
                url = ph
            else:
                continue

            if not url:
                continue

            if cache.is_cached(supplier_type, external_id, url):
                cached_paths.append(cache.get_cache_path(supplier_type, external_id, url))
            else:
                try:
                    photo_bytes = self._download_photo(url, ph, supplier)
                    if photo_bytes:
                        cache.save_to_cache(supplier_type, external_id, url, photo_bytes)
                        cached_paths.append(cache.get_cache_path(supplier_type, external_id, url))
                    else:
                        logger.warning(f"Фото {idx+1}: _download_photo вернул None для {url[:80]}")
                except Exception as dl_err:
                    logger.warning(f"Не удалось скачать фото {idx+1} ({url[:60]}): {dl_err}")

        logger.info(f"Кэш фото (direct): {len(cached_paths)}/{len(photo_urls_raw)} скачано")
        return cached_paths

    @staticmethod
    def _download_photo(url: str, photo_data, supplier) -> Optional[bytes]:
        """
        Скачивает фото с URL поставщика.

        Args:
            url: Основной URL фото
            photo_data: Данные фото (dict или str) для fallback URL
            supplier: Объект поставщика для авторизации

        Returns:
            Байты изображения или None
        """
        import requests as _requests
        from PIL import Image as _Image
        from io import BytesIO

        # Получаем cookies авторизации
        auth_cookies = {}
        if supplier and supplier.code == 'sexoptovik' and supplier.auth_login and supplier.auth_password:
            try:
                session = _requests.Session()
                resp = session.post('https://sexoptovik.ru/admin/login', data={
                    'login': supplier.auth_login,
                    'password': supplier.auth_password,
                }, timeout=10, allow_redirects=False)
                if resp.status_code in (200, 302):
                    auth_cookies = dict(session.cookies)
            except Exception:
                pass

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'image/*,*/*;q=0.8',
        }
        if 'sexoptovik.ru' in url:
            headers['Referer'] = 'https://sexoptovik.ru/admin/'

        # Fallback URLs
        fallbacks = []
        if isinstance(photo_data, dict):
            if photo_data.get('blur') and photo_data['blur'] != url:
                fallbacks.append(photo_data['blur'])
            if photo_data.get('original') and photo_data['original'] != url:
                fallbacks.append(photo_data['original'])

        all_urls = [url] + fallbacks
        logger.info(f"Скачивание фото: {len(all_urls)} URL(s), первый: {url[:80]}")
        for url_idx, current_url in enumerate(all_urls):
            try:
                resp = _requests.get(
                    current_url, headers=headers, cookies=auth_cookies,
                    timeout=15, allow_redirects=True
                )
                resp.raise_for_status()

                content_type = resp.headers.get('Content-Type', '')
                content_len = len(resp.content)
                if not content_type.startswith('image/') and content_len < 1024:
                    logger.warning(
                        f"Фото {url_idx+1}/{len(all_urls)}: неверный Content-Type='{content_type}', "
                        f"size={content_len}b, URL={current_url[:80]}"
                    )
                    continue

                img = _Image.open(BytesIO(resp.content))
                output = BytesIO()
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                img.save(output, format='JPEG', quality=95)
                logger.info(f"Фото скачано: {content_len}b, {img.size[0]}x{img.size[1]}, URL={current_url[:60]}")
                return output.getvalue()
            except Exception as dl_exc:
                logger.warning(f"Фото {url_idx+1}/{len(all_urls)} ошибка: {dl_exc}, URL={current_url[:80]}")
                continue

        logger.error(f"Все {len(all_urls)} URL фото провалились. URLs: {[u[:60] for u in all_urls]}")
        return None

    def build_wb_card_preview(self, imported_product: ImportedProduct) -> Dict:
        """
        Формирует превью карточки WB БЕЗ отправки на маркетплейс.
        Показывает продавцу как будет выглядеть карточка.

        Returns:
            dict с полями карточки WB и статусами готовности
        """
        issues = []

        # --- Базовые данные ---
        colors = json.loads(imported_product.colors) if imported_product.colors else []
        sizes_data = json.loads(imported_product.sizes) if imported_product.sizes else {}
        barcodes = json.loads(imported_product.barcodes) if imported_product.barcodes else []
        materials = json.loads(imported_product.materials) if imported_product.materials else []
        photo_urls = json.loads(imported_product.photo_urls) if imported_product.photo_urls else []

        # --- Photos ---
        # Генерируем серверные proxy URLs вместо прямых URL поставщика
        from routes.photos import generate_public_photo_urls
        media_urls = generate_public_photo_urls(imported_product)

        if not media_urls:
            issues.append({'field': 'photos', 'level': 'error', 'message': 'Нет фотографий'})

        # --- Brand ---
        product_brand = imported_product.brand or ''
        brand_resolved = bool(imported_product.resolved_brand_id)
        if not product_brand:
            issues.append({'field': 'brand', 'level': 'error', 'message': 'Бренд не указан'})
        elif not brand_resolved:
            issues.append({'field': 'brand', 'level': 'warning', 'message': f'Бренд "{product_brand}" не подтверждён в реестре'})

        # --- Category ---
        if not imported_product.wb_subject_id:
            issues.append({'field': 'category', 'level': 'error', 'message': 'Не определена категория WB'})

        # --- Title ---
        title = imported_product.title or ''
        if not title:
            issues.append({'field': 'title', 'level': 'error', 'message': 'Нет названия'})
        elif len(title) > 60:
            issues.append({'field': 'title', 'level': 'warning', 'message': f'Название {len(title)} символов (макс. 60 для WB)'})

        # --- Description ---
        description = imported_product.description or imported_product.title or ''
        if not description or len(description) < 10:
            issues.append({'field': 'description', 'level': 'warning', 'message': 'Описание слишком короткое'})

        # --- Проверка запрещённых слов WB ---
        from services.prohibited_words_filter import get_prohibited_words_filter
        word_filter = get_prohibited_words_filter()
        prohibited_in_title = word_filter.has_prohibited_words(title)
        prohibited_in_desc = word_filter.has_prohibited_words(description)
        if prohibited_in_title:
            title = filter_prohibited_words(title)
            issues.append({
                'field': 'title', 'level': 'warning',
                'message': f'Заменены запрещённые слова WB: {", ".join(prohibited_in_title)}'
            })
        if prohibited_in_desc:
            description = filter_prohibited_words(description)
            issues.append({
                'field': 'description', 'level': 'warning',
                'message': f'Заменены запрещённые слова WB: {", ".join(prohibited_in_desc[:5])}'
            })

        # --- Sizes ---
        has_real_sizes = False
        sizes_list = []
        if isinstance(sizes_data, dict):
            sizes_list = sizes_data.get('simple_sizes', [])
            if sizes_list:
                has_real_sizes = True
        elif isinstance(sizes_data, list):
            sizes_list = sizes_data
            has_real_sizes = len(sizes_list) > 0

        # Проверяем, поддерживает ли WB-категория размеры
        category_has_sizes = self._category_supports_sizes(imported_product.wb_subject_id)
        if has_real_sizes and not category_has_sizes:
            issues.append({
                'field': 'sizes', 'level': 'warning',
                'message': f'Категория WB безразмерная — размеры {sizes_list} будут проигнорированы'
            })
            has_real_sizes = False

        wb_sizes = []
        if has_real_sizes and sizes_list and category_has_sizes:
            for idx, size_val in enumerate(sizes_list):
                barcode = barcodes[idx] if idx < len(barcodes) else (barcodes[0] if barcodes else '')
                wb_sizes.append({
                    'techSize': str(size_val),
                    'wbSize': str(size_val),
                    'price': 0,
                    'skus': [barcode] if barcode else []
                })
        else:
            if barcodes:
                for barcode in barcodes:
                    wb_sizes.append({'price': 0, 'skus': [barcode]})
            else:
                wb_sizes.append({'price': 0, 'skus': []})
                issues.append({'field': 'barcodes', 'level': 'warning', 'message': 'Нет баркодов'})

        # --- Vendor code ---
        vendor_code = imported_product.external_vendor_code or f'SP-{imported_product.id}'

        # --- Characteristics ---
        chars_dict = {}
        try:
            if imported_product.characteristics:
                raw = json.loads(imported_product.characteristics) if isinstance(imported_product.characteristics, str) else imported_product.characteristics
                chars_dict = {k: v for k, v in raw.items() if not k.startswith('_')}
        except Exception:
            pass

        # Фолбэк: собираем из отдельных полей
        if not chars_dict:
            chars_dict = self._assemble_chars_from_fields(imported_product)

        # Дополняем AI-характеристиками (включая физические размеры из описания)
        ai_chars = self._collect_ai_characteristics(imported_product)
        for key, val in ai_chars.items():
            if key not in chars_dict:
                chars_dict[key] = val

        if not chars_dict:
            issues.append({'field': 'characteristics', 'level': 'warning', 'message': 'Нет характеристик — WB может отклонить карточку'})

        # --- Readiness ---
        errors = len([i for i in issues if i['level'] == 'error'])
        warnings = len([i for i in issues if i['level'] == 'warning'])
        is_ready = errors == 0

        # --- Pricing — рассчитываем полную разбивку по формуле продавца ---
        pricing_data = {
            'supplier_price': imported_product.supplier_price,
            'calculated_price': imported_product.calculated_price,
            'calculated_discount_price': imported_product.calculated_discount_price,
            'calculated_price_before_discount': imported_product.calculated_price_before_discount,
            'supplier_quantity': imported_product.supplier_quantity,
        }

        if imported_product.supplier_price and imported_product.supplier_price > 0:
            try:
                pricing_settings = PricingSettings.query.filter_by(
                    seller_id=imported_product.seller_id
                ).first()
                if pricing_settings and pricing_settings.is_enabled:
                    price_result = calculate_price(
                        purchase_price=imported_product.supplier_price,
                        settings=pricing_settings,
                        product_id=imported_product.id
                    )
                    if price_result:
                        pricing_data['final_price'] = price_result['final_price']  # Z
                        pricing_data['discount_price'] = price_result['discount_price']  # X
                        pricing_data['price_before_discount'] = price_result['price_before_discount']  # Y
                        pricing_data['profit_q'] = price_result['profit_q']
                        pricing_data['profit_pct_d'] = price_result['profit_pct_d']
                        pricing_data['delivery_s'] = price_result['delivery_s']
                        pricing_data['wb_commission'] = price_result['wb_commission']
                        pricing_data['base_price_r'] = price_result['base_price_r']
                        pricing_data['spp_discount_t'] = price_result['spp_discount_t']
                        pricing_data['formula_applied'] = True

                        Y = price_result['price_before_discount']
                        Z = price_result['final_price']
                        if Y > 0:
                            pricing_data['discount_pct'] = max(0, min(99, int((1 - Z / Y) * 100)))
                elif imported_product.calculated_price:
                    pricing_data['final_price'] = imported_product.calculated_price
                    pricing_data['discount_price'] = imported_product.calculated_discount_price
                    pricing_data['price_before_discount'] = imported_product.calculated_price_before_discount
                    pricing_data['formula_applied'] = False
            except Exception as pe:
                logger.debug(f"Ошибка расчёта цены в превью: {pe}")

        return {
            'product_id': imported_product.id,
            'is_ready': is_ready,
            'can_push': is_ready and bool(self.api_client),
            'issues': issues,
            'errors_count': errors,
            'warnings_count': warnings,

            # WB card data
            'card': {
                'vendorCode': vendor_code,
                'title': title[:60] if title else '',
                'description': description,
                'brand': product_brand,
                'brand_resolved': brand_resolved,
                'subjectID': imported_product.wb_subject_id,
                'subjectName': imported_product.mapped_wb_category or '',
                'dimensions': {
                    'length': 10,
                    'width': 10,
                    'height': 5,
                    'weightBrutto': 0.1
                },
                'sizes': wb_sizes,
                'has_real_sizes': has_real_sizes,
                'photos': media_urls,
                'photos_count': len(media_urls),
                'colors': colors,
                'materials': materials,
                'gender': imported_product.gender or '',
                'country': imported_product.country or '',
                'characteristics': chars_dict,
                'characteristics_count': len(chars_dict),
                'barcodes': barcodes,
            },

            # Pricing
            'pricing': pricing_data,
        }

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
            'imported': 0,
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
                stats['imported'] += 1
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
