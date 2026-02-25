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

        Returns:
            ImportedProduct или None
        """
        from models import ImportedProduct
        from pricing_engine import extract_supplier_product_id

        # 1. Прямая FK-связь (самый надёжный)
        imp = ImportedProduct.query.filter_by(
            product_id=product.id,
            seller_id=seller_id
        ).first()
        if imp:
            logger.debug(f"[Enrich] Match by product_id FK: product={product.id} → imp={imp.id}")
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

        return None

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
        from photo_cache import get_supplier_photo_url, get_photo_cache

        # Текущие поля карточки
        current_chars = json.loads(product.characteristics_json or '[]')
        current_dims = json.loads(product.dimensions_json or '{}')
        current_photos_raw = json.loads(product.photos_json or '[]')

        # Данные поставщика
        sup_title = imp.ai_seo_title or imp.title
        sup_brand = imp.ai_detected_brand or imp.brand
        sup_chars_raw = imp.characteristics or '{}'
        sup_dims_raw = imp.ai_dimensions or '{}'

        # Фото поставщика
        supplier_photos = self._get_supplier_photo_list(imp)

        preview = {
            'title': {
                'current': product.title,
                'supplier': sup_title,
                'has_change': sup_title and sup_title != product.title,
            },
            'brand': {
                'current': product.brand,
                'supplier': sup_brand,
                'has_change': sup_brand and sup_brand != product.brand,
            },
            'description': {
                'current': product.description,
                'supplier': imp.description,
                'has_change': bool(imp.description and imp.description != product.description),
            },
            'characteristics': {
                'current': current_chars,
                'supplier_raw': sup_chars_raw,
                'has_change': bool(sup_chars_raw and sup_chars_raw != '{}'),
            },
            'dimensions': {
                'current': current_dims,
                'supplier': json.loads(sup_dims_raw) if sup_dims_raw else {},
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

    def _get_supplier_photo_list(self, imp) -> List[Dict]:
        """Возвращает список фото поставщика с serve URL и статусом кэша"""
        from photo_cache import get_photo_cache, get_supplier_photo_url

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
        bulk_edit_id: int = None
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

        snapshot_before = _create_product_snapshot(product)
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

        if 'characteristics' in fields and imp.characteristics:
            mapped_chars = self._map_characteristics(imp, product.subject_id)
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

        if wb_updates:
            try:
                wb_client.update_card(
                    product.nm_id,
                    wb_updates,
                    merge_with_existing=True,
                    seller_id=seller.id
                )
                wb_sync_success = True
                logger.info(f"[Enrich] WB API updated nmID={product.nm_id}: {list(wb_updates.keys())}")

                # Обновляем локально
                if 'title' in wb_updates:
                    product.title = wb_updates['title']
                if 'brand' in wb_updates:
                    product.brand = wb_updates['brand']
                if 'description' in wb_updates:
                    product.description = wb_updates['description']
                if 'characteristics' in wb_updates:
                    product.characteristics_json = json.dumps(wb_updates['characteristics'], ensure_ascii=False)
                if 'dimensions' in wb_updates:
                    product.dimensions_json = json.dumps(wb_updates['dimensions'], ensure_ascii=False)

            except Exception as e:
                wb_error = str(e)
                logger.error(f"[Enrich] WB API error for nmID={product.nm_id}: {e}")
                errors.append(f"WB API: {e}")

        # --- Фото ---
        photo_result = {'skipped': True}
        if 'photos' in fields:
            photo_result = self._apply_photos(product, imp, photo_strategy, seller, wb_client)
            if photo_result.get('uploaded', 0) > 0:
                fields_applied.append('photos')

        # --- Связываем ImportedProduct с Product (если ещё не) ---
        if imp.product_id is None:
            imp.product_id = product.id

        product.updated_at = datetime.utcnow()

        # --- История изменений ---
        snapshot_after = _create_product_snapshot(product)
        history = CardEditHistory(
            product_id=product.id,
            seller_id=seller.id,
            bulk_edit_id=bulk_edit_id,
            action='update',
            changed_fields=fields_applied,
            snapshot_before=snapshot_before,
            snapshot_after=snapshot_after,
            wb_synced=wb_sync_success,
            wb_sync_status='success' if wb_sync_success else ('failed' if wb_error else 'pending'),
            wb_error_message=wb_error,
            user_comment='Обогащение от поставщика'
        )
        db.session.add(history)
        db.session.commit()

        return {
            'success': not bool(errors),
            'fields_applied': fields_applied,
            'photos': photo_result,
            'error': '; '.join(errors) if errors else None,
            'wb_sync': wb_sync_success,
        }

    def _map_characteristics(self, imp, subject_id: int) -> List[Dict]:
        """
        Преобразует characteristics из ImportedProduct в формат WB API.
        WB ожидает: [{"id": <int>, "value": <str|list>}]
        """
        if not imp.characteristics:
            return []

        try:
            raw = json.loads(imp.characteristics)
        except (json.JSONDecodeError, TypeError):
            return []

        if isinstance(raw, list):
            # Уже в нужном формате
            valid = [c for c in raw if isinstance(c, dict) and 'id' in c]
            return valid

        if isinstance(raw, dict):
            # Конвертируем dict {name: value} → [{id: ..., value: ...}]
            # Без маппинга name→id возвращаем пустой список
            return []

        return []

    def _apply_photos(self, product, imp, strategy: str, seller, wb_client) -> Dict:
        """
        Скачивает фото поставщика (через кэш) и загружает в карточку WB.

        strategy:
            'replace'      - заменить все фото
            'append'       - добавить в конец
            'only_if_empty' - только если у карточки нет фото
        """
        from photo_cache import get_photo_cache, PhotoCacheManager

        # Проверка стратегии
        current_photos = json.loads(product.photos_json or '[]')
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

        # Ставим в очередь загрузки незакэшированные фото
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

        # Ждём пока фото скачаются (max 90 сек)
        cached_paths = self._wait_for_cached_photos(photo_urls, supplier_type, external_id, cache, timeout=90)

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
        timeout: int = 90
    ) -> List[str]:
        """Ожидает кэширования фото, возвращает пути к закэшированным файлам"""
        deadline = time.time() + timeout
        cached_paths = []

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

            # Если хотя бы половина фото закэшировалась — возвращаем
            total = sum(1 for ph in photo_urls if isinstance(ph, dict) and
                        (ph.get('sexoptovik') or ph.get('original') or ph.get('blur')))
            if total > 0 and len(cached_paths) >= max(1, total // 2):
                break

            time.sleep(2)

        return cached_paths

    def _get_sexoptovik_auth(self, seller) -> Optional[Dict]:
        """Получает cookies авторизации для sexoptovik"""
        try:
            from auto_import_manager import SexoptovikAuth
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

        # Запускаем в фоне
        thread = threading.Thread(
            target=self._run_bulk_job,
            args=(job_id, product_ids, fields, photo_strategy, seller.id, seller.wb_api_key),
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
        wb_api_key_encrypted: str
    ):
        """Фоновая задача массового обогащения"""
        from models import db, EnrichmentJob, Product, Seller
        from wb_api_client import WildberriesAPIClient

        # Нужно создать новый контекст приложения для фонового потока
        from seller_platform import app as flask_app

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
                wb_client = WildberriesAPIClient(seller.get_wb_api_key())
            except Exception as e:
                job.status = 'failed'
                db.session.commit()
                logger.error(f"[Enrich] WB client init failed: {e}")
                return

            # Создаём BulkEditHistory запись
            from models import BulkEditHistory
            bulk_history = BulkEditHistory(
                seller_id=seller_id,
                action='supplier_enrichment',
                status='running',
                total_products=len(product_ids),
                updated_products=0,
                failed_products=0,
                params=json.dumps({'fields': fields, 'photo_strategy': photo_strategy})
            )
            db.session.add(bulk_history)
            db.session.flush()
            bulk_edit_id = bulk_history.id

            results = []
            succeeded = 0
            failed = 0
            skipped = 0

            for i, product_id in enumerate(product_ids):
                product = Product.query.get(product_id)
                if not product or product.seller_id != seller_id:
                    skipped += 1
                    results.append({'product_id': product_id, 'status': 'skipped', 'reason': 'not_found'})
                    job.processed = i + 1
                    job.skipped = skipped
                    db.session.commit()
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
                    db.session.commit()
                    continue

                try:
                    result = self.apply_enrichment(
                        product, imp, fields, photo_strategy,
                        seller, wb_client, bulk_edit_id=bulk_edit_id
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
                    failed += 1
                    logger.error(f"[Enrich] Error enriching product {product_id}: {e}")
                    results.append({
                        'product_id': product_id,
                        'nm_id': getattr(product, 'nm_id', None),
                        'vendor_code': getattr(product, 'vendor_code', None),
                        'status': 'failed',
                        'error': str(e),
                    })

                # Обновляем прогресс
                job.processed = i + 1
                job.succeeded = succeeded
                job.failed = failed
                job.skipped = skipped
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
            bulk_history.updated_products = succeeded
            bulk_history.failed_products = failed

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
        'characteristics': json.loads(product.characteristics_json) if product.characteristics_json else [],
        'dimensions': json.loads(product.dimensions_json) if product.dimensions_json else {},
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
