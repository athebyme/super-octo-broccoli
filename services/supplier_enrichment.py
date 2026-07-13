# -*- coding: utf-8 -*-
"""
Сервис обогащения WB-карточек данными от поставщика.

Позволяет:
- Найти данные поставщика для существующей карточки (по FK, vendor_code или паттерну)
- Сформировать diff-превью (текущее WB vs поставщик)
- Применить выбранные поля к карточке (через WB API + локально)
- Запустить массовое обогащение в фоне с отслеживанием прогресса
"""

import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Базовая директория для кэша фото
PHOTO_CACHE_BASE = Path('data/photo_cache')


class EnrichmentService:
    """Сервис обогащения карточек WB данными от поставщика"""

    # =========================================================================
    # MATCHING: Product → ImportedProduct
    # =========================================================================

    def find_supplier_data(self, product, seller_id: int):
        """
        Находит ImportedProduct для данного WB-продукта.
        Перебирает стратегии по убыванию надёжности.

        ImportedProduct seller-scoped: совпадение по внешнему артикулу не
        даёт права читать импорт или менять связь другого продавца.

        Returns:
            ImportedProduct или None
        """
        from models import ImportedProduct
        from services.pricing_engine import extract_supplier_product_id

        if getattr(product, 'seller_id', seller_id) != seller_id:
            return None

        # 1. Прямая FK-связь (самый надёжный)
        imp = ImportedProduct.query.filter_by(
            product_id=product.id,
            seller_id=seller_id
        ).first()
        if imp:
            logger.debug(f"[Enrich] Match by product_id FK (seller): product={product.id} → imp={imp.id}")
            return imp

        # 2. По supplier_vendor_code карточки
        if product.supplier_vendor_code:
            imp = ImportedProduct.query.filter_by(
                external_vendor_code=product.supplier_vendor_code,
                seller_id=seller_id
            ).first()
            if imp:
                logger.debug(f"[Enrich] Match by supplier_vendor_code: {product.supplier_vendor_code}")
                return imp

        # 3. По vendor_code паттерну с множественными форматами external_id.
        # Vendor code имеет форму: id-{product_id}-{supplier_code}
        # ImportedProduct.external_id может быть: '25268', 'id-25268', 'id-25268-...'
        if product.vendor_code:
            numeric_pid = extract_supplier_product_id(product.vendor_code)  # int или None
            if numeric_pid:
                # Пробуем все варианты формата external_id, которые встречаются в реальных данных
                candidate_ids = [
                    str(numeric_pid),           # '25268'
                    f'id-{numeric_pid}',        # 'id-25268'  ← sexoptovik CSV
                ]
                # Также извлекаем часть vendor_code до второго дефиса
                vc_match = re.match(r'^(id-\w+)-', product.vendor_code)
                if vc_match:
                    candidate_ids.append(vc_match.group(1))  # 'id-25268'

                # Дедупликация
                seen = set()
                unique_ids = [x for x in candidate_ids if not (x in seen or seen.add(x))]

                for ext_id in unique_ids:
                    imp = ImportedProduct.query.filter_by(
                        external_id=ext_id,
                        seller_id=seller_id
                    ).first()
                    if imp:
                        logger.debug(
                            f"[Enrich] Match by vendor_code pattern: "
                            f"vendor_code={product.vendor_code} → external_id={ext_id}"
                        )
                        return imp

        # 4. Поиск через SupplierProduct (централизованный каталог)
        try:
            from models import SupplierProduct
            sp = self._find_supplier_product(product, seller_id)
            if sp:
                # Ищем ImportedProduct привязанный к этому SupplierProduct
                imp = ImportedProduct.query.filter_by(
                    supplier_product_id=sp.id, seller_id=seller_id
                ).first()
                if imp:
                    logger.debug(f"[Enrich] Match via SupplierProduct: sp={sp.id} → imp={imp.id}")
                    return imp
        except Exception as e:
            logger.debug(f"[Enrich] SupplierProduct search failed: {e}")

        return None

    def _find_supplier_product(self, product, seller_id: int):
        """
        Находит SupplierProduct для WB-карточки по vendor_code, barcode или title.
        """
        from models import SupplierProduct, SellerSupplier
        from services.pricing_engine import extract_supplier_product_id

        # Получаем supplier_ids для данного продавца
        seller_suppliers = SellerSupplier.query.filter_by(seller_id=seller_id).all()
        supplier_ids = [ss.supplier_id for ss in seller_suppliers] if seller_suppliers else []

        if not supplier_ids:
            return None

        base_query = SupplierProduct.query.filter(
            SupplierProduct.supplier_id.in_(supplier_ids)
        )

        # По vendor_code
        if product.vendor_code:
            numeric_pid = extract_supplier_product_id(product.vendor_code)
            if numeric_pid:
                candidates = [str(numeric_pid), f'id-{numeric_pid}']
                for ext_id in candidates:
                    sp = base_query.filter_by(external_id=ext_id).first()
                    if sp:
                        return sp

        # По supplier_vendor_code
        if product.supplier_vendor_code:
            sp = base_query.filter_by(vendor_code=product.supplier_vendor_code).first()
            if sp:
                return sp

        return None

    def find_supplier_data_with_source(self, product, seller_id: int) -> Dict[str, Any]:
        """
        Расширенный поиск данных поставщика с информацией об источнике.
        Возвращает и ImportedProduct и SupplierProduct (если найден).
        """
        from models import ImportedProduct, SupplierProduct

        imp = self.find_supplier_data(product, seller_id)
        sp = None

        # Пытаемся найти SupplierProduct для дополнительных данных
        if imp and imp.supplier_product_id:
            sp = SupplierProduct.query.get(imp.supplier_product_id)
        elif not sp:
            try:
                sp = self._find_supplier_product(product, seller_id)
            except Exception:
                pass

        return {
            'imported_product': imp,
            'supplier_product': sp,
            'has_data': imp is not None,
            'has_supplier_product': sp is not None,
        }

    # =========================================================================
    # BATCH: проверка доступности обогащения для списка карточек
    # =========================================================================

    def check_enrichment_availability(self, product_ids: List[int], seller_id: int) -> Dict[int, Dict]:
        """
        Быстрая проверка наличия данных поставщика для списка карточек.
        Используется для отображения индикаторов в списке товаров.

        Returns:
            {product_id: {'available': bool, 'imp_id': int|None, 'photo_count': int, 'has_description': bool}}
        """
        from models import Product, ImportedProduct

        result = {}

        # Оптимизация: сначала ищем по FK за один запрос
        fk_matches = ImportedProduct.query.filter(
            ImportedProduct.product_id.in_(product_ids),
            ImportedProduct.seller_id == seller_id,
        ).all()
        fk_map = {}
        for imp in fk_matches:
            if imp.product_id not in fk_map:
                fk_map[imp.product_id] = imp

        for pid in product_ids:
            if pid in fk_map:
                imp = fk_map[pid]
                photo_count = 0
                if imp.photo_urls:
                    try:
                        photo_count = len(json.loads(imp.photo_urls))
                    except (json.JSONDecodeError, TypeError):
                        pass
                result[pid] = {
                    'available': True,
                    'imp_id': imp.id,
                    'photo_count': photo_count,
                    'has_description': bool(imp.description),
                    'has_title': bool(imp.ai_seo_title or imp.title),
                    'has_characteristics': self._build_supplier_characteristic_source(imp)['has_data'],
                }
            else:
                result[pid] = {
                    'available': False,
                    'imp_id': None,
                    'photo_count': 0,
                    'has_description': False,
                    'has_title': False,
                    'has_characteristics': False,
                }

        # Для карточек без FK-связи — пытаемся найти по vendor_code (дорого, но точечно)
        missing = [pid for pid in product_ids if not result[pid]['available']]
        if missing and len(missing) <= 50:  # Ограничиваем для производительности
            products = Product.query.filter(
                Product.id.in_(missing),
                Product.seller_id == seller_id,
            ).all()
            for product in products:
                imp = self.find_supplier_data(product, seller_id)
                if imp:
                    photo_count = 0
                    if imp.photo_urls:
                        try:
                            photo_count = len(json.loads(imp.photo_urls))
                        except (json.JSONDecodeError, TypeError):
                            pass
                    result[product.id] = {
                        'available': True,
                        'imp_id': imp.id,
                        'photo_count': photo_count,
                        'has_description': bool(imp.description),
                        'has_title': bool(imp.ai_seo_title or imp.title),
                        'has_characteristics': self._build_supplier_characteristic_source(imp)['has_data'],
                    }

        return result

    # =========================================================================
    # PREVIEW: формирование diff между WB и поставщиком
    # =========================================================================

    def build_preview(self, product, imp) -> Dict[str, Any]:
        """
        Строит структуру для сравнения текущей WB-карточки с данными поставщика.

        Returns:
            dict с ключами: title, brand, description, characteristics,
                           dimensions, photos, supplier_meta
        """
        from services.photo_cache import get_photo_cache

        # Текущие поля карточки
        current_chars = self._safe_json_loads(
            product.characteristics_json, [])
        if isinstance(current_chars, dict):
            current_chars = [
                {'name': str(name), 'value': value}
                for name, value in current_chars.items()
            ]
        elif not isinstance(current_chars, list):
            current_chars = []
        else:
            current_chars = [
                item for item in current_chars if isinstance(item, dict)
            ]

        current_dims = self._safe_json_loads(product.dimensions_json, {})
        if not isinstance(current_dims, dict):
            current_dims = {}

        current_photos_raw = self._safe_json_loads(product.photos_json, [])
        if not isinstance(current_photos_raw, list):
            current_photos_raw = []

        # Данные поставщика
        sup_title = imp.ai_seo_title or imp.title
        sup_brand = imp.ai_detected_brand or imp.brand
        supplier_characteristics = self._build_supplier_characteristic_source(imp)
        sup_chars_raw = imp.characteristics or '{}'
        sup_dims_raw = imp.ai_dimensions or '{}'

        characteristic_validation = {
            'valid': True,
            'error': None,
            'normalized': [],
        }
        if supplier_characteristics['has_data']:
            from services.marketplace_validator import (
                WBCharacteristicValidationError,
            )
            try:
                characteristic_validation['normalized'] = self._map_characteristics(
                    imp,
                    product.subject_id,
                    source=supplier_characteristics,
                )
            except WBCharacteristicValidationError as exc:
                characteristic_validation.update({
                    'valid': False,
                    'error': str(exc),
                    'normalized': [],
                })

        # Preview и apply читают один и тот же источник: общие
        # characteristics плюс отдельные materials/gender поставщика.
        supplier_chars_parsed = supplier_characteristics['parsed']

        # Фото поставщика
        supplier_photos = self._get_supplier_photo_list(imp)

        # Ставим фото на фоновую загрузку чтобы кэш наполнялся
        if supplier_photos and not all(p.get('cached') for p in supplier_photos):
            self._trigger_photo_cache(imp)

        preview = {
            'title': {
                'current': product.title,
                'supplier': sup_title,
                'has_change': bool(sup_title and sup_title != product.title),
            },
            'brand': {
                'current': product.brand,
                'supplier': sup_brand,
                'has_change': bool(sup_brand and sup_brand != product.brand),
            },
            'description': {
                'current': product.description,
                'supplier': imp.description,
                'has_change': bool(imp.description and imp.description != product.description),
            },
            'characteristics': {
                'current': current_chars,
                'supplier_raw': sup_chars_raw,
                'supplier_parsed': supplier_chars_parsed,
                'has_change': supplier_characteristics['has_data'],
                'validation': characteristic_validation,
            },
            'dimensions': {
                'current': current_dims,
                'supplier': self._safe_json_loads(sup_dims_raw, {}),
                'has_change': bool(sup_dims_raw and sup_dims_raw != '{}'),
            },
            'photos': {
                'current_count': len(current_photos_raw),
                'supplier_photos': supplier_photos,
                'has_change': bool(supplier_photos),
            },
            'supplier_meta': {
                'id': imp.id,
                'external_id': imp.external_id,
                'source_type': imp.source_type,
                'title': imp.title,
                'created_at': imp.created_at.isoformat() if imp.created_at else None,
            }
        }

        return preview

    @staticmethod
    def _safe_json_loads(data: str, default=None):
        """Безопасный json.loads с дефолтным значением"""
        if not data:
            return default
        try:
            return json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return default

    @staticmethod
    def _parse_supplier_chars(chars_raw: Any) -> List[Dict]:
        """
        Парсит характеристики поставщика в список [{name, value}].
        Принимает JSON строку или уже разобраные dict/list.
        """
        if not chars_raw:
            return []
        if isinstance(chars_raw, str):
            if chars_raw in ('{}', '[]', 'null'):
                return []
            try:
                data = json.loads(chars_raw)
            except (json.JSONDecodeError, TypeError):
                return []
        else:
            data = chars_raw

        result = []
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, list):
                    v = ', '.join(str(x) for x in v)
                result.append({'name': str(k), 'value': str(v) if v is not None else ''})
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    name = item.get('name', item.get('id', ''))
                    value = item.get('value', '')
                    if isinstance(value, list):
                        value = ', '.join(str(x) for x in value)
                    result.append({'name': str(name), 'value': str(value)})
        return result

    @staticmethod
    def _decode_supplier_json_value(raw: Any) -> Any:
        """Decode a JSON-backed supplier field without hiding plain legacy text."""
        if not isinstance(raw, str):
            return raw
        raw = raw.strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

    @staticmethod
    def _has_supplier_value(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple, dict, set)):
            return bool(value)
        return True

    @staticmethod
    def _preview_value(value: Any) -> str:
        if isinstance(value, list):
            return ', '.join(str(item) for item in value)
        return str(value) if value is not None else ''

    @classmethod
    def _upsert_preview_characteristic(
        cls,
        parsed: List[Dict[str, Any]],
        name: str,
        value: Any,
    ) -> None:
        """Upsert only by an exact case-insensitive label; no fuzzy matching."""
        exact_name = name.strip().casefold()
        display_value = cls._preview_value(value)
        for item in parsed:
            if str(item.get('name', '')).strip().casefold() == exact_name:
                item['value'] = display_value
                return
        parsed.append({'name': name, 'value': display_value})

    @classmethod
    def _build_supplier_characteristic_source(cls, imp) -> Dict[str, Any]:
        """Build the shared preview/apply source from all ImportedProduct fields."""
        raw_values = getattr(imp, 'characteristics', None)
        raw_values_present = False
        if isinstance(raw_values, str):
            stripped = raw_values.strip()
            raw_values_present = bool(
                stripped and stripped not in ('{}', '[]', 'null'))
            try:
                values = json.loads(stripped) if stripped else {}
            except (json.JSONDecodeError, TypeError):
                # Не скрываем повреждённые данные: strict builder ниже вернёт
                # invalid_payload и остановит write до WB.
                values = raw_values
        elif isinstance(raw_values, (dict, list)):
            values = raw_values
            raw_values_present = bool(raw_values)
        else:
            values = {}

        materials = cls._decode_supplier_json_value(
            getattr(imp, 'materials', None))
        gender = getattr(imp, 'gender', None)
        if isinstance(gender, str):
            gender = gender.strip() or None

        parsed = cls._parse_supplier_chars(values)
        if cls._has_supplier_value(materials):
            cls._upsert_preview_characteristic(
                parsed, 'Материал изделия', materials)
        if cls._has_supplier_value(gender):
            cls._upsert_preview_characteristic(parsed, 'Пол', gender)

        return {
            'values': values,
            'materials': materials,
            'gender': gender,
            'parsed': parsed,
            'has_data': bool(parsed) or raw_values_present,
        }

    def _trigger_photo_cache(self, imp):
        """Ставит все фото ImportedProduct в очередь фоновой загрузки"""
        from services.photo_cache import get_photo_cache
        if not imp.photo_urls:
            return
        try:
            photo_urls = json.loads(imp.photo_urls)
        except (json.JSONDecodeError, TypeError):
            return

        cache = get_photo_cache()
        supplier_type = imp.source_type or 'unknown'
        external_id = imp.external_id or ''

        for ph in photo_urls:
            if not isinstance(ph, dict):
                continue
            url = ph.get('sexoptovik') or ph.get('original') or ph.get('blur')
            if url and not cache.is_cached(supplier_type, external_id, url):
                fallbacks = []
                if ph.get('blur') and ph['blur'] != url:
                    fallbacks.append(ph['blur'])
                if ph.get('original') and ph['original'] != url:
                    fallbacks.append(ph['original'])
                cache.queue_download(supplier_type, external_id, url, fallback_urls=fallbacks)

    def _get_supplier_photo_list(self, imp) -> List[Dict]:
        """Возвращает список фото поставщика с serve URL и статусом кэша"""
        from services.photo_cache import get_photo_cache, get_supplier_photo_url

        if not imp.photo_urls:
            return []

        try:
            photo_urls = json.loads(imp.photo_urls)
        except (json.JSONDecodeError, TypeError):
            return []

        cache = get_photo_cache()
        result = []

        for ph in photo_urls:
            if not isinstance(ph, dict):
                continue
            url = ph.get('sexoptovik') or ph.get('original') or ph.get('blur')
            if not url:
                continue

            is_cached = cache.is_cached(imp.source_type or 'unknown', imp.external_id or '', url)
            serve_url = get_supplier_photo_url(
                imp.source_type or 'unknown',
                imp.external_id or '',
                url
            )
            result.append({
                'original_url': url,
                'serve_url': serve_url,
                'cached': is_cached,
                'blur': ph.get('blur'),
                'has_original': bool(ph.get('original') or ph.get('sexoptovik')),
            })

        return result

    # =========================================================================
    # APPLY: применение данных поставщика к WB-карточке
    # =========================================================================

    def apply_enrichment(
        self,
        product,
        imp,
        fields: List[str],
        photo_strategy: str,
        seller,
        wb_client,
        bulk_edit_id: int = None,
        is_bulk: bool = False,
        validation_cache: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Применяет выбранные поля из ImportedProduct к WB-карточке.

        Args:
            product: Product ORM объект
            imp: ImportedProduct ORM объект
            fields: список полей для обогащения ['title','brand','description','characteristics','dimensions','photos']
            photo_strategy: 'replace' | 'append' | 'only_if_empty'
            seller: Seller ORM объект
            wb_client: WildberriesAPIClient экземпляр
            bulk_edit_id: ID bulk-операции для связи с историей

        Returns:
            {'success': bool, 'fields_applied': list, 'photos': dict, 'error': str|None}
        """
        from models import db, CardEditHistory

        seller_id = getattr(seller, 'id', None)
        if (
            seller_id is None
            or getattr(product, 'seller_id', seller_id) != seller_id
            or getattr(imp, 'seller_id', seller_id) != seller_id
        ):
            return {
                'success': False,
                'fields_applied': [],
                'photos': {'skipped': True},
                'error': 'Access denied: seller scope mismatch',
                'wb_sync': False,
            }

        snapshot_before_local = _create_product_snapshot(product)
        snapshot_before = snapshot_before_local
        wb_snapshot_context = {}
        wb_updates = {}
        fields_applied = []
        errors = []

        # --- Текстовые поля ---
        if 'title' in fields:
            sup_title = imp.ai_seo_title or imp.title
            if sup_title:
                wb_updates['title'] = sup_title[:60]
                fields_applied.append('title')

        if 'brand' in fields:
            sup_brand = imp.ai_detected_brand or imp.brand
            if sup_brand:
                wb_updates['brand'] = sup_brand
                fields_applied.append('brand')

        if 'description' in fields and imp.description:
            wb_updates['description'] = imp.description[:5000]
            fields_applied.append('description')

        if 'characteristics' in fields:
            supplier_characteristics = self._build_supplier_characteristic_source(imp)
        else:
            supplier_characteristics = None

        if supplier_characteristics and supplier_characteristics['has_data']:
            from services.marketplace_validator import (
                WBCharacteristicValidationError,
            )
            try:
                mapped_chars = self._map_characteristics(
                    imp,
                    product.subject_id,
                    source=supplier_characteristics,
                    validation_cache=validation_cache,
                )
            except WBCharacteristicValidationError as exc:
                return {
                    'success': False,
                    'fields_applied': [],
                    'photos': {'skipped': True},
                    'error': str(exc),
                    'wb_sync': False,
                }
            if mapped_chars:
                wb_updates['characteristics'] = mapped_chars
                fields_applied.append('characteristics')

        if 'dimensions' in fields and imp.ai_dimensions:
            try:
                dims = json.loads(imp.ai_dimensions)
                if dims:
                    wb_updates['dimensions'] = dims
                    fields_applied.append('dimensions')
            except (json.JSONDecodeError, TypeError):
                pass

        # --- Обновление через WB API (текстовые поля) ---
        wb_sync_success = False
        wb_error = None
        content_history = None
        content_history_id = None

        if wb_updates:
            from services.card_snapshot import overlay_snapshot_with_wb_card

            def mirror_sent_card_to_product(after_wb):
                if 'title' in wb_updates:
                    product.title = after_wb.get('title')
                if 'brand' in wb_updates:
                    product.brand = after_wb.get('brand')
                if 'description' in wb_updates:
                    product.description = after_wb.get('description')
                if 'characteristics' in wb_updates:
                    product.characteristics_json = json.dumps(
                        after_wb.get('characteristics') or [],
                        ensure_ascii=False,
                    )
                if 'dimensions' in wb_updates:
                    product.dimensions_json = json.dumps(
                        after_wb.get('dimensions') or {},
                        ensure_ascii=False,
                    )

            def persist_pending_content_history(exact_snapshot):
                """Commit rollback data before WB receives the replacement."""
                nonlocal content_history, content_history_id, snapshot_before
                snapshot_before = overlay_snapshot_with_wb_card(
                    snapshot_before_local,
                    exact_snapshot['before'],
                )
                snapshot_after = overlay_snapshot_with_wb_card(
                    snapshot_before_local,
                    exact_snapshot['after'],
                )
                content_history = CardEditHistory(
                    product_id=product.id,
                    seller_id=seller.id,
                    bulk_edit_id=bulk_edit_id,
                    action='update',
                    changed_fields=list(wb_updates),
                    snapshot_before=snapshot_before,
                    snapshot_after=snapshot_after,
                    wb_synced=False,
                    wb_sync_status='pending',
                    user_comment='Обогащение от поставщика',
                )
                db.session.add(content_history)
                db.session.commit()
                content_history_id = content_history.id

            try:
                wb_client.update_card(
                    product.nm_id,
                    wb_updates,
                    merge_with_existing=True,
                    seller_id=seller.id,
                    snapshot_context=wb_snapshot_context,
                    before_send_callback=persist_pending_content_history,
                )
            except Exception as e:
                # A successfully committed pending row survives this rollback;
                # a callback/DB failure happens before the HTTP request.
                db.session.rollback()
                wb_error = str(e)
                if content_history_id is not None:
                    content_history = CardEditHistory.query.filter_by(
                        id=content_history_id,
                        product_id=product.id,
                        seller_id=seller.id,
                    ).first()
                    if content_history is not None:
                        reconciliation = 'conflict'
                        try:
                            from services.card_rollback import (
                                classify_wb_card_history_state,
                            )
                            live_card = wb_client.get_card_by_nm_id(
                                product.nm_id,
                                seller_id=seller.id,
                            )
                            reconciliation = classify_wb_card_history_state(
                                live_card,
                                content_history.snapshot_before,
                                content_history.snapshot_after,
                                content_history.changed_fields,
                            )
                        except Exception as reconcile_error:
                            logger.warning(
                                '[Enrich] Could not reconcile nmID=%s after '
                                'ambiguous update: %s',
                                product.nm_id,
                                reconcile_error,
                            )

                        if reconciliation == 'after':
                            # WB confirms the exact sent values despite the
                            # transport exception: finish local state normally.
                            wb_sync_success = True
                            content_history.wb_synced = True
                            content_history.wb_sync_status = 'success'
                            content_history.wb_error_message = None
                            mirror_sent_card_to_product(
                                wb_snapshot_context['after'])
                        else:
                            # before/conflict may be eventual consistency. Keep
                            # an explicit uncertain row; conflict-aware rollback
                            # remains available after the short in-flight window.
                            content_history.wb_synced = False
                            content_history.wb_sync_status = 'uncertain'
                            content_history.wb_error_message = wb_error
                        db.session.commit()
                logger.error(f"[Enrich] WB API error for nmID={product.nm_id}: {e}")
                if not wb_sync_success:
                    errors.append(f"WB API: {e}")
                    fields_applied = [
                        field for field in fields_applied
                        if field not in {
                            'title', 'brand', 'description',
                            'characteristics', 'dimensions',
                        }
                    ]
            else:
                wb_sync_success = True
                logger.info(
                    f"[Enrich] WB API updated nmID={product.nm_id}: "
                    f"{list(wb_updates.keys())}"
                )

                # Real client invokes the callback before HTTP. Keep a guarded
                # fallback for compatible test/custom clients, while preserving
                # exact snapshots supplied through snapshot_context.
                if content_history is None:
                    persist_pending_content_history(wb_snapshot_context)

                mirror_sent_card_to_product(wb_snapshot_context['after'])

                content_history.wb_synced = True
                content_history.wb_sync_status = 'success'
                content_history.wb_error_message = None
                # If this local commit fails, the already committed pending row
                # remains a durable recovery marker with exact before/after.
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                    # WB already returned success. Retry the local finalization
                    # over the durable pending row before surfacing an error.
                    content_history = CardEditHistory.query.filter_by(
                        id=content_history_id,
                        product_id=product.id,
                        seller_id=seller.id,
                    ).first()
                    if content_history is None:
                        raise
                    content_history.wb_synced = True
                    content_history.wb_sync_status = 'success'
                    content_history.wb_error_message = None
                    mirror_sent_card_to_product(wb_snapshot_context['after'])
                    db.session.commit()

        # --- Фото ---
        # Photo changes are intentionally recorded separately: WB media has no
        # supported rollback path and must not make content history unrevertible.
        photo_result = {'skipped': True}
        if 'photos' in fields and (not wb_updates or wb_sync_success):
            photo_snapshot_before = _create_product_snapshot(product)
            photo_history = CardEditHistory(
                product_id=product.id,
                seller_id=seller.id,
                bulk_edit_id=bulk_edit_id,
                action='update',
                changed_fields=['photos'],
                snapshot_before=photo_snapshot_before,
                snapshot_after=photo_snapshot_before,
                wb_synced=False,
                wb_sync_status='pending',
                user_comment=(
                    'Обогащение фото от поставщика '
                    f'(стратегия: {photo_strategy})'
                ),
            )
            db.session.add(photo_history)
            db.session.commit()

            photo_result = self._apply_photos(
                product, imp, photo_strategy, seller, wb_client,
                is_bulk=is_bulk,
            )
            uploaded_count = int(photo_result.get('uploaded') or 0)
            failed_count = int(photo_result.get('failed') or 0)
            if uploaded_count > 0:
                fields_applied.append('photos')
                photo_history.wb_synced = True
                photo_history.snapshot_after = _create_product_snapshot(product)
                if failed_count > 0:
                    photo_history.wb_sync_status = 'partial'
                    photo_error = (
                        f'загружено {uploaded_count}, '
                        f'не загружено {failed_count}'
                    )
                    photo_history.wb_error_message = photo_error
                    errors.append(f'Фото WB: {photo_error}')
                else:
                    photo_history.wb_sync_status = 'success'
            elif photo_result.get('skipped'):
                # No WB side effect occurred, so a history row is unnecessary.
                db.session.delete(photo_history)
            else:
                photo_error = (
                    photo_result.get('error')
                    or f'не загружено, ошибок: {photo_result.get("failed", 0)}'
                )
                photo_history.wb_sync_status = 'failed'
                photo_history.wb_error_message = str(photo_error)
                errors.append(f'Фото WB: {photo_error}')
        elif 'photos' in fields:
            photo_result = {
                'skipped': True,
                'reason': 'content_update_failed',
            }

        # --- Связываем ImportedProduct с Product (если ещё не) ---
        if imp.product_id is None:
            imp.product_id = product.id

        product.updated_at = datetime.utcnow()
        db.session.commit()

        return {
            'success': not bool(errors),
            'fields_applied': fields_applied,
            'photos': photo_result,
            'error': '; '.join(errors) if errors else None,
            'wb_sync': wb_sync_success,
        }

    def _map_characteristics(
        self,
        imp,
        subject_id: int,
        source: Optional[Dict[str, Any]] = None,
        validation_cache: Optional[Dict[str, Any]] = None,
    ) -> List[Dict]:
        """
        Преобразует characteristics из ImportedProduct в формат WB API.
        WB ожидает: [{"id": <int>, "value": <str|list>}]

        Оба поддерживаемых формата — [{id, value}] и {name: value} — проходят
        обязательную category-scoped проверку по admin schema/dictionaries.
        """
        source = source or self._build_supplier_characteristic_source(imp)
        if not source['has_data']:
            return []

        from services.marketplace_validator import (
            build_wb_supplier_characteristic_patch,
        )
        return build_wb_supplier_characteristic_patch(
            subject_id,
            source['values'],
            materials=source['materials'],
            gender=source['gender'],
            validation_cache=validation_cache,
        )

    def apply_selective_photos(
        self,
        product,
        imp,
        photo_indices: List[int],
        strategy: str,
        seller,
        wb_client,
    ) -> Dict:
        """
        Применяет выборочные фото от поставщика к WB-карточке.

        Args:
            product: Product ORM объект
            imp: ImportedProduct ORM объект
            photo_indices: список индексов фото поставщика для применения
            strategy: 'replace' | 'append' | 'only_if_empty'
            seller: Seller ORM объект
            wb_client: WildberriesAPIClient экземпляр

        Returns:
            {'success': bool, 'uploaded': int, 'failed': int, 'error': str|None}
        """
        from models import db, CardEditHistory
        from services.photo_cache import get_photo_cache

        seller_id = getattr(seller, 'id', None)
        if (
            seller_id is None
            or getattr(product, 'seller_id', seller_id) != seller_id
            or getattr(imp, 'seller_id', seller_id) != seller_id
        ):
            return {
                'success': False,
                'uploaded': 0,
                'error': 'Access denied: seller scope mismatch',
            }

        if not imp.photo_urls:
            return {'success': False, 'error': 'Нет фото у поставщика'}

        try:
            all_photos = json.loads(imp.photo_urls)
        except (json.JSONDecodeError, TypeError):
            return {'success': False, 'error': 'Ошибка данных фото'}

        # Фильтруем только запрошенные индексы
        selected_photos = []
        for idx in photo_indices:
            if 0 <= idx < len(all_photos):
                selected_photos.append(all_photos[idx])

        if not selected_photos:
            return {'success': False, 'error': 'Нет валидных фото по указанным индексам'}

        # Проверка стратегии
        current_photos = self._safe_json_loads(product.photos_json, [])
        if not isinstance(current_photos, list):
            current_photos = []
        if strategy == 'only_if_empty' and current_photos:
            return {'success': True, 'uploaded': 0, 'skipped': True, 'reason': 'already_has_photos'}

        cache = get_photo_cache()
        supplier_type = imp.source_type or 'unknown'
        external_id = imp.external_id or ''

        # Получаем auth cookies
        auth_cookies = None
        if supplier_type == 'sexoptovik':
            auth_cookies = self._get_sexoptovik_auth(seller)

        # Загружаем выбранные фото в кэш
        for ph in selected_photos:
            if not isinstance(ph, dict):
                continue
            url = ph.get('sexoptovik') or ph.get('original') or ph.get('blur')
            if url and not cache.is_cached(supplier_type, external_id, url):
                fallbacks = []
                if ph.get('blur') and ph['blur'] != url:
                    fallbacks.append(ph['blur'])
                if ph.get('original') and ph['original'] != url:
                    fallbacks.append(ph['original'])
                cache.queue_download(supplier_type, external_id, url,
                                     auth_cookies=auth_cookies,
                                     fallback_urls=fallbacks)

        # Ждём кэширования (max 30 сек, early exit при stall)
        cached_paths = self._wait_for_cached_photos(selected_photos, supplier_type, external_id, cache, timeout=30)

        if not cached_paths:
            return {'success': False, 'error': 'Не удалось загрузить фото поставщика'}

        # Снапшот и durable pending-история до внешнего side effect.
        snapshot_before = _create_product_snapshot(product)
        history = CardEditHistory(
            product_id=product.id,
            seller_id=seller.id,
            action='update',
            changed_fields=['photos'],
            snapshot_before=snapshot_before,
            snapshot_after=snapshot_before,
            wb_synced=False,
            wb_sync_status='pending',
            user_comment=(
                f'Выборочное обогащение фото ({len(photo_indices)} шт, '
                f'стратегия: {strategy})'
            ),
        )
        db.session.add(history)
        db.session.commit()
        history_id = history.id

        # Загружаем в WB
        try:
            upload_results = wb_client.upload_photos_to_card(
                product.nm_id,
                cached_paths,
                seller_id=seller.id
            )
            uploaded_count = sum(1 for r in upload_results if r.get('success'))
            failed_count = len(upload_results) - uploaded_count

            product.updated_at = datetime.utcnow()

            snapshot_after = _create_product_snapshot(product)
            history.snapshot_after = snapshot_after
            history.wb_synced = uploaded_count > 0
            if uploaded_count > 0 and failed_count == 0:
                history.wb_sync_status = 'success'
            elif uploaded_count > 0:
                history.wb_sync_status = 'partial'
                history.wb_error_message = (
                    f'загружено {uploaded_count}, '
                    f'не загружено {failed_count}'
                )
            else:
                history.wb_sync_status = 'failed'
                history.wb_error_message = 'Ни одно фото не загружено'
            db.session.commit()

            return {
                'success': uploaded_count > 0 and failed_count == 0,
                'uploaded': uploaded_count,
                'failed': failed_count,
                'total': len(cached_paths),
                'strategy': strategy,
                'error': history.wb_error_message,
            }
        except Exception as e:
            db.session.rollback()
            persisted_history = CardEditHistory.query.filter_by(
                id=history_id,
                product_id=product.id,
                seller_id=seller.id,
            ).first()
            if persisted_history is not None:
                # A timeout/commit error after upload is ambiguous; retain the
                # pending marker instead of claiming WB definitely rejected it.
                persisted_history.wb_sync_status = 'pending'
                persisted_history.wb_error_message = str(e)
                db.session.commit()
            logger.error(f"[Enrich] Selective photo upload error for nmID={product.nm_id}: {e}")
            return {'success': False, 'uploaded': 0, 'error': str(e)}

    def _apply_photos(self, product, imp, strategy: str, seller, wb_client,
                      is_bulk: bool = False) -> Dict:
        """
        Скачивает фото поставщика (через кэш) и загружает в карточку WB.

        strategy:
            'replace'      - заменить все фото
            'append'       - добавить в конец
            'only_if_empty' - только если у карточки нет фото
            'selective'    - пропустить (фото обрабатываются через apply_selective_photos)
        is_bulk:
            True при вызове из _run_bulk_job — использует синхронную загрузку
            для надёжности (queue_download ненадёжен в bulk-режиме)
        """
        from services.photo_cache import get_photo_cache, PhotoCacheManager

        # Стратегия selective — фото обрабатываются отдельно
        if strategy == 'selective':
            logger.info(f"[Enrich] Photos skipped (strategy=selective, handled separately)")
            return {'skipped': True, 'reason': 'selective_mode'}

        # Проверка стратегии
        current_photos = self._safe_json_loads(product.photos_json, [])
        if not isinstance(current_photos, list):
            current_photos = []
        if strategy == 'only_if_empty' and current_photos:
            logger.info(f"[Enrich] Photos skipped (strategy=only_if_empty, has {len(current_photos)} photos)")
            return {'skipped': True, 'reason': 'already_has_photos'}

        if not imp.photo_urls:
            return {'skipped': True, 'reason': 'no_supplier_photos'}

        try:
            photo_urls = json.loads(imp.photo_urls)
        except (json.JSONDecodeError, TypeError):
            return {'skipped': True, 'reason': 'invalid_photo_urls_json'}

        if not photo_urls:
            return {'skipped': True, 'reason': 'empty_photo_list'}

        cache = get_photo_cache()
        supplier_type = imp.source_type or 'unknown'
        external_id = imp.external_id or ''

        # Получаем auth cookies для sexoptovik
        auth_cookies = None
        if supplier_type == 'sexoptovik':
            auth_cookies = self._get_sexoptovik_auth(seller)

        if is_bulk:
            # BULK-РЕЖИМ: синхронная загрузка каждого фото по очереди.
            # Не используем queue_download — в bulk-режиме очередь забивается
            # фотками от разных товаров, и _wait_for_cached_photos "зависает"
            # потому что воркеры заняты чужими фотками.
            cached_paths = self._download_photos_sync(
                photo_urls, supplier_type, external_id, cache, auth_cookies
            )
        else:
            # SINGLE-РЕЖИМ: ставим в очередь + ждём (быстрее для одного товара)
            for ph in photo_urls:
                if not isinstance(ph, dict):
                    continue
                url = ph.get('sexoptovik') or ph.get('original') or ph.get('blur')
                if url and not cache.is_cached(supplier_type, external_id, url):
                    fallbacks = []
                    if ph.get('blur') and ph['blur'] != url:
                        fallbacks.append(ph['blur'])
                    if ph.get('original') and ph['original'] != url:
                        fallbacks.append(ph['original'])
                    cache.queue_download(supplier_type, external_id, url,
                                         auth_cookies=auth_cookies,
                                         fallback_urls=fallbacks)

            cached_paths = self._wait_for_cached_photos(
                photo_urls, supplier_type, external_id, cache, timeout=30
            )

        if not cached_paths:
            return {'skipped': True, 'reason': 'photos_not_cached_after_timeout'}

        # Загружаем в WB
        try:
            upload_results = wb_client.upload_photos_to_card(
                product.nm_id,
                cached_paths,
                seller_id=seller.id
            )
            uploaded_count = sum(1 for r in upload_results if r.get('success'))
            failed_count = len(upload_results) - uploaded_count

            logger.info(f"[Enrich] Photos uploaded for nmID={product.nm_id}: {uploaded_count} ok, {failed_count} failed")
            return {
                'uploaded': uploaded_count,
                'failed': failed_count,
                'total': len(cached_paths),
                'strategy': strategy,
            }
        except Exception as e:
            logger.error(f"[Enrich] Photo upload error for nmID={product.nm_id}: {e}")
            return {'uploaded': 0, 'error': str(e)}

    def _wait_for_cached_photos(
        self,
        photo_urls: List[Dict],
        supplier_type: str,
        external_id: str,
        cache,
        timeout: int = 30
    ) -> List[str]:
        """Ожидает кэширования фото, возвращает пути к закэшированным файлам.

        Уменьшен таймаут (было 90с). Добавлен early exit если прогресс
        остановился (скачивание не продвигается 3 итерации подряд).
        """
        deadline = time.time() + timeout
        cached_paths = []
        prev_count = 0
        stall_count = 0

        total = sum(1 for ph in photo_urls if isinstance(ph, dict) and
                    (ph.get('sexoptovik') or ph.get('original') or ph.get('blur')))
        if total == 0:
            return []

        while time.time() < deadline:
            cached_paths = []
            for ph in photo_urls:
                if not isinstance(ph, dict):
                    continue
                url = ph.get('sexoptovik') or ph.get('original') or ph.get('blur')
                if not url:
                    continue
                if cache.is_cached(supplier_type, external_id, url):
                    path = cache.get_cache_path(supplier_type, external_id, url)
                    cached_paths.append(path)

            # Все фото скачаны
            if len(cached_paths) >= total:
                break

            # Хотя бы 1 фото есть — достаточно для продолжения
            if len(cached_paths) >= max(1, total // 2):
                break

            # Early exit: если 3 итерации подряд нет прогресса — не ждём
            if len(cached_paths) == prev_count:
                stall_count += 1
                if stall_count >= 3:
                    logger.warning(
                        f"[Enrich] Photo download stalled: {len(cached_paths)}/{total} "
                        f"after {stall_count} checks, giving up early"
                    )
                    break
            else:
                stall_count = 0
            prev_count = len(cached_paths)

            time.sleep(2)

        return cached_paths

    def _download_photos_sync(
        self,
        photo_urls: List[Dict],
        supplier_type: str,
        external_id: str,
        cache,
        auth_cookies: Optional[Dict] = None
    ) -> List[str]:
        """Синхронная загрузка фото для bulk-режима.

        В отличие от queue_download + _wait_for_cached_photos, скачивает фото
        по одному напрямую. Это надёжнее для bulk-обработки: не зависит от
        общей очереди воркеров, гарантирует загрузку всех фото текущего товара
        перед переходом к следующему.
        """
        cached_paths = []

        for ph in photo_urls:
            if not isinstance(ph, dict):
                continue
            url = ph.get('sexoptovik') or ph.get('original') or ph.get('blur')
            if not url:
                continue

            # Уже в кэше — сразу берём
            if cache.is_cached(supplier_type, external_id, url):
                cached_paths.append(cache.get_cache_path(supplier_type, external_id, url))
                continue

            # Скачиваем синхронно с fallbacks
            fallbacks = []
            if ph.get('blur') and ph['blur'] != url:
                fallbacks.append(ph['blur'])
            if ph.get('original') and ph['original'] != url:
                fallbacks.append(ph['original'])

            try:
                success = cache.download_now(
                    supplier_type, external_id, url,
                    auth_cookies=auth_cookies,
                    fallback_urls=fallbacks
                )
                if success:
                    cached_paths.append(cache.get_cache_path(supplier_type, external_id, url))
                else:
                    logger.debug(f"[Enrich] Photo download failed (sync): {url[:60]}")
            except Exception as e:
                logger.debug(f"[Enrich] Photo download error (sync): {e}")

        logger.info(
            f"[Enrich] Sync photo download: {len(cached_paths)}/{len(photo_urls)} "
            f"for {supplier_type}/{external_id}"
        )
        return cached_paths

    def _get_sexoptovik_auth(self, seller) -> Optional[Dict]:
        """Получает cookies авторизации для sexoptovik"""
        try:
            from services.auto_import_manager import SexoptovikAuth
            from models import AutoImportSettings

            settings = seller.auto_import_settings if seller else None
            login = getattr(settings, 'sexoptovik_login', None)
            password = getattr(settings, 'sexoptovik_password', None)

            if not login or not password:
                # Ищем у других продавцов
                other = AutoImportSettings.query.filter(
                    AutoImportSettings.sexoptovik_login.isnot(None),
                    AutoImportSettings.sexoptovik_password.isnot(None)
                ).first()
                if other:
                    login = other.sexoptovik_login
                    password = other.sexoptovik_password

            if login and password:
                return SexoptovikAuth.get_auth_cookies(login, password)
        except Exception as e:
            logger.warning(f"[Enrich] Sexoptovik auth failed: {e}")

        return None

    # =========================================================================
    # BULK: массовое обогащение в фоновом потоке
    # =========================================================================

    def start_bulk_enrichment(
        self,
        product_ids: List[int],
        fields: List[str],
        photo_strategy: str,
        seller,
        wb_client
    ) -> str:
        """
        Запускает массовое обогащение в фоновом потоке.

        Returns:
            job_id (UUID строка)
        """
        from models import db, EnrichmentJob

        job_id = str(uuid.uuid4())
        job = EnrichmentJob(
            id=job_id,
            seller_id=seller.id,
            status='pending',
            total=len(product_ids),
            processed=0,
            succeeded=0,
            failed=0,
            skipped=0,
            fields_config=json.dumps(fields),
            photo_strategy=photo_strategy,
            results=json.dumps([])
        )
        db.session.add(job)
        db.session.commit()

        # Захватываем Flask app для фонового потока
        # ВАЖНО: не делаем `from seller_platform import app` в потоке —
        # это circular import, который молча роняет фоновый поток.
        from flask import current_app
        try:
            flask_app = current_app._get_current_object()
        except RuntimeError:
            flask_app = None

        if not flask_app:
            logger.error("[Enrich] No Flask app context — bulk enrichment cannot start")
            job.status = 'failed'
            db.session.commit()
            return job_id

        # Запускаем в фоне
        thread = threading.Thread(
            target=self._run_bulk_job,
            args=(job_id, product_ids, fields, photo_strategy, seller.id, seller.wb_api_key, flask_app),
            daemon=True,
            name=f'EnrichJob-{job_id[:8]}'
        )
        thread.start()

        logger.info(f"[Enrich] Bulk job {job_id} started: {len(product_ids)} products, fields={fields}")
        return job_id

    def _run_bulk_job(
        self,
        job_id: str,
        product_ids: List[int],
        fields: List[str],
        photo_strategy: str,
        seller_id: int,
        wb_api_key_encrypted: str,
        flask_app=None
    ):
        """Фоновая задача массового обогащения"""
        from models import db, EnrichmentJob, Product, Seller
        from services.wb_api_client import WildberriesAPIClient

        if not flask_app:
            logger.error(f"[Enrich] Job {job_id}: no Flask app provided")
            return

        with flask_app.app_context():
            job = EnrichmentJob.query.get(job_id)
            if not job:
                logger.error(f"[Enrich] Job {job_id} not found")
                return

            job.status = 'running'
            db.session.commit()

            seller = Seller.query.get(seller_id)
            if not seller:
                job.status = 'failed'
                db.session.commit()
                return

            # Создаём WB клиент
            try:
                wb_client = WildberriesAPIClient(seller.wb_api_key)
            except Exception as e:
                job.status = 'failed'
                db.session.commit()
                logger.error(f"[Enrich] WB client init failed: {e}")
                return

            # Создаём BulkEditHistory запись
            from models import BulkEditHistory
            bulk_history = BulkEditHistory(
                seller_id=seller_id,
                operation_type='supplier_enrichment',
                operation_params={
                    'fields': fields,
                    'photo_strategy': photo_strategy,
                },
                description='Обновление карточек данными поставщика',
                status='in_progress',
                total_products=len(product_ids),
                success_count=0,
                error_count=0,
                errors_details=[],
            )
            db.session.add(bulk_history)
            db.session.flush()
            bulk_edit_id = bulk_history.id
            db.session.commit()

            results = []
            succeeded = 0
            failed = 0
            skipped = 0
            validation_cache = {}

            for i, product_id in enumerate(product_ids):
                product = Product.query.filter_by(
                    id=product_id,
                    seller_id=seller_id,
                ).first()
                if not product:
                    skipped += 1
                    results.append({'product_id': product_id, 'status': 'skipped', 'reason': 'not_found'})
                    job.processed = i + 1
                    job.skipped = skipped
                    continue

                imp = self.find_supplier_data(product, seller_id)
                if not imp:
                    skipped += 1
                    results.append({
                        'product_id': product_id,
                        'nm_id': product.nm_id,
                        'vendor_code': product.vendor_code,
                        'status': 'skipped',
                        'reason': 'no_supplier_data'
                    })
                    job.processed = i + 1
                    job.skipped = skipped
                    continue

                try:
                    result = self.apply_enrichment(
                        product, imp, fields, photo_strategy,
                        seller, wb_client, bulk_edit_id=bulk_edit_id,
                        is_bulk=True,
                        validation_cache=validation_cache,
                    )

                    if result['success']:
                        succeeded += 1
                        results.append({
                            'product_id': product_id,
                            'nm_id': product.nm_id,
                            'vendor_code': product.vendor_code,
                            'status': 'success',
                            'fields_applied': result['fields_applied'],
                        })
                    else:
                        failed += 1
                        results.append({
                            'product_id': product_id,
                            'nm_id': product.nm_id,
                            'vendor_code': product.vendor_code,
                            'status': 'failed',
                            'error': result.get('error'),
                        })

                except Exception as e:
                    db.session.rollback()
                    failed += 1
                    logger.error(f"[Enrich] Error enriching product {product_id}: {e}")
                    results.append({
                        'product_id': product_id,
                        'nm_id': getattr(product, 'nm_id', None),
                        'vendor_code': getattr(product, 'vendor_code', None),
                        'status': 'failed',
                        'error': str(e),
                    })

                # Обновляем прогресс (коммит каждые 5 товаров для снижения нагрузки на БД)
                job.processed = i + 1
                job.succeeded = succeeded
                job.failed = failed
                job.skipped = skipped
                if (i + 1) % 5 == 0 or (i + 1) == len(product_ids):
                    job.results = json.dumps(results, ensure_ascii=False)
                    db.session.commit()

                # Небольшая пауза чтобы не перегружать WB API
                if (i + 1) % 10 == 0:
                    time.sleep(1)

            # Завершение
            job.status = 'done'
            job.processed = len(product_ids)
            job.succeeded = succeeded
            job.failed = failed
            job.skipped = skipped
            job.results = json.dumps(results, ensure_ascii=False)
            job.updated_at = datetime.utcnow()

            bulk_history.status = 'completed'
            bulk_history.success_count = succeeded
            bulk_history.error_count = failed + skipped
            bulk_history.errors_details = [
                item for item in results
                if item.get('status') != 'success'
            ]
            bulk_history.wb_synced = succeeded > 0
            bulk_history.completed_at = datetime.utcnow()

            db.session.commit()

            logger.info(
                f"[Enrich] Job {job_id} done: "
                f"{succeeded} succeeded, {failed} failed, {skipped} skipped"
            )


# =========================================================================
# Вспомогательные функции (не методы класса)
# =========================================================================

def _create_product_snapshot(product) -> Dict:
    """Снапшот состояния карточки для истории (повторяет логику из seller_platform.py)"""
    characteristics = EnrichmentService._safe_json_loads(
        product.characteristics_json, [])
    if not isinstance(characteristics, (list, dict)):
        characteristics = []

    dimensions = EnrichmentService._safe_json_loads(
        product.dimensions_json, {})
    if not isinstance(dimensions, dict):
        dimensions = {}

    return {
        'nm_id': product.nm_id,
        'vendor_code': product.vendor_code,
        'title': product.title,
        'brand': product.brand,
        'description': product.description,
        'object_name': product.object_name,
        'price': float(product.price) if product.price else None,
        'discount_price': float(product.discount_price) if product.discount_price else None,
        'quantity': product.quantity,
        'characteristics': characteristics,
        'dimensions': dimensions,
        'photos_json': product.photos_json,
        'is_active': product.is_active,
    }


# Глобальный экземпляр сервиса (singleton)
_enrichment_service: Optional[EnrichmentService] = None


def get_enrichment_service() -> EnrichmentService:
    global _enrichment_service
    if _enrichment_service is None:
        _enrichment_service = EnrichmentService()
    return _enrichment_service
