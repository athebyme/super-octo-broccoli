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
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Базовая директория для кэша фото
PHOTO_CACHE_BASE = Path('data/photo_cache')
SAFE_ENRICHMENT_PHOTO_STRATEGIES = frozenset({
    'smart_merge', 'replace', 'append', 'only_if_empty',
})
ALLOWED_ENRICHMENT_FIELDS = frozenset({
    'title', 'brand', 'description',
    'characteristics', 'dimensions', 'photos',
})


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def enrichment_items_per_tick() -> int:
    return _bounded_env_int('WB_ENRICHMENT_ITEMS_PER_TICK', 3, 1, 20)


def enrichment_tick_seconds() -> int:
    return _bounded_env_int('WB_ENRICHMENT_TICK_SECONDS', 45, 10, 55)


def enrichment_lease_seconds() -> int:
    return _bounded_env_int('WB_ENRICHMENT_LEASE_SECONDS', 600, 120, 1800)


class WbMediaOperationBusy(RuntimeError):
    """Another WB photo/gallery write owns the seller-wide boundary."""


class WbLiveMediaDrift(RuntimeError):
    """The live WB gallery changed after append positions were planned."""


class WbEnrichmentAwaitingReconciliation(RuntimeError):
    """A prior asynchronous enrichment write must settle before another one."""

    def __init__(self, *, history_id: Optional[int], operation_kind: str):
        self.history_id = history_id
        self.operation_kind = operation_kind
        label = 'фото' if operation_kind == 'photos' else 'контента'
        suffix = f' (операция #{history_id})' if history_id is not None else ''
        super().__init__(
            f'Предыдущая отправка {label} на WB ещё проверяется{suffix}; '
            'новая отправка отложена'
        )


class EnrichmentJobAlreadyActive(RuntimeError):
    """A seller already owns a durable pending/running enrichment cursor."""

    def __init__(self, job_id: Optional[str] = None):
        self.job_id = job_id
        super().__init__('Массовое обновление уже выполняется')


class EnrichmentService:
    """Supplier-driven WB card enrichment service."""

    @staticmethod
    def _assert_no_unreconciled_write(
        *,
        seller_id: int,
        nm_id: int,
        operation_kind: str,
    ) -> None:
        """Fail closed while an older write can still be absent from live WB.

        The caller invokes this under the corresponding seller-wide provider
        lock.  That ordering closes the check→receipt race between two local
        workers while still allowing content and media lifecycles to reconcile
        independently.
        """
        if operation_kind not in {'content', 'photos'}:
            raise ValueError('Unsupported enrichment operation kind')

        from models import CardEditHistory, Product
        from services.wb_enrichment_reconciliation import RECONCILABLE_STATUSES

        rows = CardEditHistory.query.join(
            Product,
            Product.id == CardEditHistory.product_id,
        ).filter(
            CardEditHistory.seller_id == seller_id,
            Product.seller_id == seller_id,
            Product.nm_id == nm_id,
            CardEditHistory.reverted.is_(False),
            CardEditHistory.wb_sync_status.in_(tuple(RECONCILABLE_STATUSES)),
            CardEditHistory.wb_reconcile_due_at.isnot(None),
        ).order_by(CardEditHistory.id.desc()).limit(51).all()
        for history in rows[:50]:
            fields = {
                str(field) for field in (history.changed_fields or [])
                if isinstance(field, str)
            }
            relevant = (
                'photos' in fields
                if operation_kind == 'photos'
                else bool(fields - {'photos'})
            )
            if relevant:
                raise WbEnrichmentAwaitingReconciliation(
                    history_id=history.id,
                    operation_kind=operation_kind,
                )
        if len(rows) > 50:
            # An unexpectedly large unresolved journal must not let an older
            # relevant receipt hide beyond the bounded scan.
            raise WbEnrichmentAwaitingReconciliation(
                history_id=None,
                operation_kind=operation_kind,
            )

    @staticmethod
    def upload_photos_to_card_locked(
        wb_client,
        *,
        seller_id: int,
        nm_id: int,
        photo_paths: List[str],
    ):
        """Run legacy multipart upload under the shared WB media lock."""
        from services.marketplace_operation_locks import (
            release_wb_seller_media_lock,
            try_wb_seller_media_lock,
        )

        claim = try_wb_seller_media_lock(seller_id)
        if claim is None:
            raise WbMediaOperationBusy(
                'Для продавца уже выполняется другая операция с фото WB'
            )
        try:
            return wb_client.upload_photos_to_card(
                nm_id,
                photo_paths,
                seller_id=seller_id,
            )
        finally:
            release_wb_seller_media_lock(claim)

    @staticmethod
    def merge_photos_to_card_locked(
        wb_client,
        *,
        seller_id: int,
        nm_id: int,
        photo_paths: List[str],
        strategy: str,
        before_upload_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        before_live_read_callback: Optional[Callable[[], None]] = None,
    ) -> Dict[str, Any]:
        """Match against the exact live gallery and append only missing photos.

        The seller-wide media lock covers the live read, perceptual match,
        durable pre-write callback and multipart writes.  Therefore another
        Seller Hub media operation cannot invalidate the selected append slots.
        """
        if strategy not in SAFE_ENRICHMENT_PHOTO_STRATEGIES:
            raise ValueError('Unsupported supplier enrichment photo strategy')

        from services.marketplace_operation_locks import (
            release_wb_seller_media_lock,
            try_wb_seller_media_lock,
        )
        from services.wb_api_client import MAX_WB_MEDIA_FILES
        from services.wb_enrichment_merge import (
            MERGE_POLICY_VERSION,
            PHOTO_MATCH_THRESHOLD,
            fingerprint_local_photo,
            fingerprint_remote_photo,
            live_wb_photo_match_urls,
            live_wb_photo_urls,
            plan_photo_merge,
            photo_similarity,
        )

        claim = try_wb_seller_media_lock(seller_id)
        if claim is None:
            raise WbMediaOperationBusy(
                'Для продавца уже выполняется другая операция с фото WB'
            )
        try:
            if before_live_read_callback is not None:
                before_live_read_callback()
            live_card = wb_client.get_card_by_nm_id(
                nm_id,
                seller_id=seller_id,
            )
            if not isinstance(live_card, dict):
                raise RuntimeError(
                    'Нельзя безопасно обновить фото: live-карточка WB не получена'
                )
            live_urls = live_wb_photo_urls(live_card)
            live_match_urls = live_wb_photo_match_urls(live_card)

            if strategy == 'only_if_empty' and live_urls:
                report = {
                    'policy': MERGE_POLICY_VERSION,
                    'mode': 'preserve_live_append_missing',
                    'requested_strategy': strategy,
                    'live_count': len(live_urls),
                    'candidate_count': len(photo_paths),
                    'live_fingerprint_failures': 0,
                    'matching_status': 'not_needed',
                    'append_indices': [],
                    'counts': {
                        'already_present': 0,
                        'append': 0,
                        'duplicate_candidate': 0,
                        'skipped_capacity': 0,
                        'skipped_match_unavailable': 0,
                        'skipped_unreadable': 0,
                        'skipped_non_empty_gallery': len(photo_paths),
                    },
                    'items': [],
                }
                receipt = {
                    'report': report,
                    'live_urls_before': live_urls,
                    'planned_photos_after': list(live_urls),
                }
                if before_upload_callback is not None:
                    before_upload_callback(receipt)
                return {
                    'success': True,
                    'uploaded': 0,
                    'failed': 0,
                    'skipped': True,
                    'reason': 'already_has_photos',
                    'merge_report': report,
                    'live_urls_before': live_urls,
                    'snapshot_after_photos': list(live_urls),
                }

            live_fingerprints = []
            for url in live_match_urls:
                try:
                    live_fingerprints.append(fingerprint_remote_photo(url))
                except Exception as exc:
                    logger.info(
                        '[Enrich] Live WB photo fingerprint unavailable (%s)',
                        type(exc).__name__,
                    )
                    live_fingerprints.append(None)

            candidate_fingerprints = []
            for path in photo_paths:
                try:
                    candidate_fingerprints.append(
                        fingerprint_local_photo(path))
                except Exception as exc:
                    logger.info(
                        '[Enrich] Supplier photo fingerprint unavailable (%s)',
                        type(exc).__name__,
                    )
                    candidate_fingerprints.append(None)

            report = plan_photo_merge(
                live_fingerprints,
                candidate_fingerprints,
                max_images=MAX_WB_MEDIA_FILES,
            )
            report['requested_strategy'] = strategy
            report['comparison_variant'] = 'square_preferred'
            append_indices = report['append_indices']
            # Bounded hashes let the delayed reconciler prove that every
            # pre-existing position survived and every submitted candidate
            # actually appeared in the provider gallery.
            report['live_position_fingerprints'] = [
                dict(value) if isinstance(value, dict) else None
                for value in live_fingerprints
            ]
            report['expected_append_fingerprints'] = [
                dict(candidate_fingerprints[index])
                for index in append_indices
                if isinstance(candidate_fingerprints[index], dict)
            ]
            append_paths = [photo_paths[index] for index in append_indices]
            planned_tokens = [
                'enrichment-sha256:' + str(
                    candidate_fingerprints[index]['pixel_sha'])
                for index in append_indices
            ]
            receipt = {
                'report': report,
                'live_urls_before': live_urls,
                'planned_photos_after': list(live_urls) + planned_tokens,
            }
            if before_upload_callback is not None:
                before_upload_callback(receipt)

            if not append_paths:
                counts = report['counts']
                if counts['skipped_capacity']:
                    reason = 'gallery_full'
                elif (
                    counts['skipped_unreadable']
                    or counts['skipped_match_unavailable']
                ):
                    reason = 'photo_match_unavailable'
                else:
                    reason = 'all_supplier_photos_already_present'
                return {
                    'success': True,
                    'uploaded': 0,
                    'failed': 0,
                    'skipped': True,
                    'reason': reason,
                    'merge_report': report,
                    'live_urls_before': live_urls,
                    'snapshot_after_photos': list(live_urls),
                }

            # Fingerprinting and durable receipt persistence both widen the
            # read→write window. Re-read the exact gallery immediately before
            # multipart I/O and refuse to use stale X-Photo-Number positions.
            try:
                preflight_card = wb_client.get_card_by_nm_id(
                    nm_id,
                    seller_id=seller_id,
                )
            except Exception as exc:
                raise WbLiveMediaDrift(
                    'Не удалось повторно прочитать live-галерею WB; '
                    'дозагрузка безопасно остановлена'
                ) from exc
            if not isinstance(preflight_card, dict):
                raise WbLiveMediaDrift(
                    'Повторный live-read WB не вернул галерею'
                )
            preflight_urls = live_wb_photo_urls(preflight_card)
            if preflight_urls != live_urls:
                raise WbLiveMediaDrift(
                    'Live-галерея WB изменилась перед дозагрузкой; '
                    'операция безопасно остановлена'
                )
            preflight_match_urls = live_wb_photo_match_urls(preflight_card)
            try:
                preflight_fingerprints = [
                    fingerprint_remote_photo(url)
                    for url in preflight_match_urls
                ]
                append_fingerprints = [
                    fingerprint_local_photo(path)
                    for path in append_paths
                ]
            except Exception as exc:
                raise WbLiveMediaDrift(
                    'Не удалось повторно подтвердить байты фото перед '
                    'дозагрузкой; операция безопасно остановлена'
                ) from exc
            if any(
                original is None
                or photo_similarity(original, current)
                < PHOTO_MATCH_THRESHOLD
                for original, current in zip(
                    live_fingerprints, preflight_fingerprints
                )
            ):
                raise WbLiveMediaDrift(
                    'Содержимое live-фото WB изменилось перед дозагрузкой; '
                    'операция безопасно остановлена'
                )
            if any(
                planned.get('pixel_sha') != current.get('pixel_sha')
                or (
                    planned.get('source_sha256') is not None
                    and planned.get('source_sha256')
                    != current.get('source_sha256')
                )
                for planned, current in zip(
                    (
                        candidate_fingerprints[index]
                        for index in append_indices
                    ),
                    append_fingerprints,
                )
            ):
                raise WbLiveMediaDrift(
                    'Локальный source-файл изменился после построения плана; '
                    'дозагрузка безопасно остановлена'
                )

            upload_kwargs = {}
            if all(
                isinstance(value.get('source_sha256'), str)
                for value in append_fingerprints
            ):
                upload_kwargs['expected_source_sha256'] = [
                    str(value['source_sha256'])
                    for value in append_fingerprints
                ]
            upload_results = wb_client.upload_photos_to_card(
                nm_id,
                append_paths,
                seller_id=seller_id,
                start_photo_number=len(live_urls) + 1,
                **upload_kwargs,
            )
            uploaded_count = sum(
                1 for item in upload_results if item.get('success'))
            uncertain_count = sum(
                1 for item in upload_results
                if item.get('request_may_have_been_applied')
            )
            rejected_count = sum(
                1 for item in upload_results
                if not item.get('success')
                and not item.get('request_may_have_been_applied')
            )
            not_attempted_count = max(
                0, len(append_paths) - len(upload_results),
            )
            failed_count = rejected_count + not_attempted_count
            upload_items = []
            for item in upload_results[:MAX_WB_MEDIA_FILES]:
                if item.get('success'):
                    status = 'accepted'
                elif item.get('request_may_have_been_applied'):
                    status = 'uncertain'
                else:
                    status = 'rejected'
                upload_items.append({
                    'photo_number': item.get('photo_number'),
                    'status': status,
                    'error': (
                        str(item.get('error'))[:300]
                        if item.get('error') else None
                    ),
                })
            potentially_submitted_tokens = [
                planned_tokens[index]
                for index, item in enumerate(upload_results)
                if (
                    item.get('success')
                    or item.get('request_may_have_been_applied')
                ) and index < len(planned_tokens)
            ]
            return {
                'success': (
                    uploaded_count == len(append_paths)
                    and failed_count == 0
                    and uncertain_count == 0
                ),
                'uploaded': uploaded_count,
                'uncertain': uncertain_count,
                'failed': failed_count,
                'rejected': rejected_count,
                'not_attempted': not_attempted_count,
                'total': len(append_paths),
                'strategy': 'smart_merge',
                'requested_strategy': strategy,
                'merge_report': report,
                'live_urls_before': live_urls,
                'snapshot_after_photos': (
                    list(live_urls) + potentially_submitted_tokens
                ),
                'upload_items': upload_items,
                'error': next((
                    item['error'] for item in upload_items
                    if item.get('error')
                ), None),
            }
        finally:
            release_wb_seller_media_lock(claim)

    # =========================================================================
    # MATCHING: Product → ImportedProduct
    # =========================================================================

    @staticmethod
    def _unique_source_match(query, *, label: str):
        """Return one exact source row or fail closed on ambiguity."""
        rows = query.order_by(None).limit(2).all()
        if len(rows) > 1:
            logger.error('[Enrich] Ambiguous supplier source: %s', label)
            return None, True
        return (rows[0] if rows else None), False

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
        imp, ambiguous = self._unique_source_match(
            ImportedProduct.query.filter_by(
                product_id=product.id,
                seller_id=seller_id,
            ),
            label=f'product_id={product.id},seller_id={seller_id}',
        )
        if ambiguous:
            return None
        if imp:
            logger.debug(f"[Enrich] Match by product_id FK (seller): product={product.id} → imp={imp.id}")
            return imp

        # 2. По supplier_vendor_code карточки
        if product.supplier_vendor_code:
            imp, ambiguous = self._unique_source_match(
                ImportedProduct.query.filter_by(
                    external_vendor_code=product.supplier_vendor_code,
                    seller_id=seller_id,
                ),
                label=(
                    'external_vendor_code='
                    f'{product.supplier_vendor_code},seller_id={seller_id}'
                ),
            )
            if ambiguous:
                return None
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
                    imp, ambiguous = self._unique_source_match(
                        ImportedProduct.query.filter_by(
                            external_id=ext_id,
                            seller_id=seller_id,
                        ),
                        label=(
                            f'external_id={ext_id},seller_id={seller_id}'
                        ),
                    )
                    if ambiguous:
                        return None
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
                imp, ambiguous = self._unique_source_match(
                    ImportedProduct.query.filter_by(
                        supplier_product_id=sp.id,
                        seller_id=seller_id,
                    ),
                    label=(
                        f'supplier_product_id={sp.id},seller_id={seller_id}'
                    ),
                )
                if ambiguous:
                    return None
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
                    sp, ambiguous = self._unique_source_match(
                        base_query.filter_by(external_id=ext_id),
                        label=(
                            f'SupplierProduct.external_id={ext_id},'
                            f'seller_id={seller_id}'
                        ),
                    )
                    if ambiguous:
                        return None
                    if sp:
                        return sp

        # По supplier_vendor_code
        if product.supplier_vendor_code:
            sp, ambiguous = self._unique_source_match(
                base_query.filter_by(
                    vendor_code=product.supplier_vendor_code,
                ),
                label=(
                    'SupplierProduct.vendor_code='
                    f'{product.supplier_vendor_code},seller_id={seller_id}'
                ),
            )
            if ambiguous:
                return None
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

    @staticmethod
    def _has_potential_supplier_data(products, seller_id: int) -> bool:
        """Negative-only batch preflight; never admits an identity match.

        Skip the expensive per-card resolver only when no owned import can
        match any of its exact keys. A positive/ambiguous result still goes
        through the original resolver with its ordering and conflict gates.
        """
        from sqlalchemy import or_, select
        from models import db, ImportedProduct, SellerSupplier, SupplierProduct
        from services.pricing_engine import extract_supplier_product_id

        vendor_codes, source_ids, supplier_source_ids = set(), set(), set()
        for product in products:
            if product.supplier_vendor_code:
                vendor_codes.add(product.supplier_vendor_code)
            if product.vendor_code:
                numeric_id = extract_supplier_product_id(product.vendor_code)
                if numeric_id:
                    candidates = {str(numeric_id), f'id-{numeric_id}'}
                    source_ids.update(candidates)
                    supplier_source_ids.update(candidates)
                    prefix = re.match(r'^(id-\w+)-', product.vendor_code)
                    if prefix:
                        source_ids.add(prefix.group(1))
        connected_suppliers = select(SellerSupplier.supplier_id).where(
            SellerSupplier.seller_id == seller_id,
        )
        candidate_supplier_ids = select(SupplierProduct.id).where(
            SupplierProduct.supplier_id.in_(connected_suppliers),
            or_(SupplierProduct.external_id.in_(supplier_source_ids),
                SupplierProduct.vendor_code.in_(vendor_codes)),
        )
        candidates = ImportedProduct.query.filter(
            ImportedProduct.seller_id == seller_id,
            or_(
                ImportedProduct.product_id.in_([p.id for p in products]),
                ImportedProduct.external_vendor_code.in_(vendor_codes),
                ImportedProduct.external_id.in_(source_ids),
                ImportedProduct.supplier_product_id.in_(candidate_supplier_ids),
            ),
        )
        return bool(db.session.query(candidates.exists()).scalar())

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
                photo_count = len(self._photo_source(imp)[0])
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
            if not self._has_potential_supplier_data(products, seller_id):
                return result
            for product in products:
                imp = self.find_supplier_data(product, seller_id)
                if imp:
                    photo_count = len(self._photo_source(imp)[0])
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
    def _photo_url(photo: Any) -> Optional[str]:
        if isinstance(photo, str):
            return photo if photo.startswith(('http://', 'https://')) else None
        if not isinstance(photo, dict):
            return None
        url = photo.get('sexoptovik') or photo.get('original') or photo.get('blur')
        return url if isinstance(url, str) and url.startswith(('http://', 'https://')) else None

    @staticmethod
    def _photo_fallbacks(photo: Any, primary_url: str) -> List[str]:
        if not isinstance(photo, dict):
            return []
        result = []
        for key in ('blur', 'original'):
            candidate = photo.get(key)
            if (
                isinstance(candidate, str)
                and candidate.startswith(('http://', 'https://'))
                and candidate != primary_url
                and candidate not in result
            ):
                result.append(candidate)
        return result

    def _photo_source(self, imp) -> Tuple[List[Any], str, str]:
        """Return the freshest exact supplier gallery and cache identity.

        ImportedProduct.photo_urls is a staging copy and may lag behind the
        shared supplier catalog. An exact supplier_product_id therefore wins;
        the seller-owned import remains the authorization boundary.
        """
        photo_urls = self._safe_json_loads(
            getattr(imp, 'photo_urls', None), []
        )
        if not isinstance(photo_urls, list):
            photo_urls = []
        supplier_type = getattr(imp, 'source_type', None) or 'unknown'
        external_id = str(getattr(imp, 'external_id', None) or '')

        supplier_product_id = getattr(imp, 'supplier_product_id', None)
        if supplier_product_id is not None:
            from models import db, SupplierProduct

            if (
                not isinstance(supplier_product_id, int)
                or isinstance(supplier_product_id, bool)
                or supplier_product_id <= 0
            ):
                logger.error(
                    '[Enrich] ImportedProduct %s has invalid exact '
                    'supplier_product_id=%r',
                    getattr(imp, 'id', None),
                    supplier_product_id,
                )
                return [], supplier_type, external_id
            supplier_product = db.session.get(
                SupplierProduct, supplier_product_id
            )
            if supplier_product is None:
                logger.error(
                    '[Enrich] ImportedProduct %s references missing exact '
                    'supplier_product_id=%s',
                    getattr(imp, 'id', None),
                    supplier_product_id,
                )
                return [], supplier_type, external_id
            imported_supplier_id = getattr(imp, 'supplier_id', None)
            if (
                imported_supplier_id is not None
                and supplier_product.supplier_id != imported_supplier_id
            ):
                logger.error(
                    '[Enrich] ImportedProduct %s has mismatched exact '
                    'supplier_product_id=%s',
                    getattr(imp, 'id', None),
                    supplier_product_id,
                )
                return [], supplier_type, external_id
            latest = supplier_product.get_photos()
            # An observed empty/malformed current supplier gallery is a
            # fact too. Falling back to the stale ImportedProduct copy
            # would resurrect photos that the supplier has removed.
            photo_urls = latest if isinstance(latest, list) else []
            external_id = str(
                supplier_product.external_id or external_id
            )
            supplier = getattr(supplier_product, 'supplier', None)
            supplier_type = (
                getattr(supplier, 'code', None) or supplier_type
            )

        from services.wb_api_client import MAX_WB_MEDIA_FILES
        return photo_urls[:MAX_WB_MEDIA_FILES], supplier_type, external_id

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

        # ``original_data`` is the seller-local copy of the latest observed
        # SupplierProduct.original_data_json. Package dimensions stay separate
        # from characteristics and must not be reconstructed from defaults or
        # normalized/AI product dimensions.
        observed_dimensions = {}
        raw_original = getattr(imp, 'original_data', None)
        if isinstance(raw_original, str):
            try:
                raw_original = json.loads(raw_original)
            except (json.JSONDecodeError, TypeError):
                raw_original = None
        if isinstance(raw_original, dict):
            raw_dimensions = raw_original.get('dimensions')
            if isinstance(raw_dimensions, (dict, list)):
                from services.wb_content_payload import extract_dimensions
                observed_dimensions = extract_dimensions(raw_dimensions)

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
            'observed_dimensions': observed_dimensions,
        }

    def _trigger_photo_cache(self, imp):
        """Ставит актуальную exact supplier gallery в очередь кэша."""
        from services.photo_cache import get_photo_cache
        photo_urls, supplier_type, external_id = self._photo_source(imp)
        if not photo_urls:
            return

        cache = get_photo_cache()

        for ph in photo_urls:
            url = self._photo_url(ph)
            if url and not cache.is_cached(supplier_type, external_id, url):
                cache.queue_download(
                    supplier_type, external_id, url,
                    fallback_urls=self._photo_fallbacks(ph, url),
                )

    def _get_supplier_photo_list(self, imp) -> List[Dict]:
        """Возвращает список фото поставщика с serve URL и статусом кэша"""
        from services.photo_cache import get_photo_cache, get_supplier_photo_url

        photo_urls, supplier_type, external_id = self._photo_source(imp)
        if not photo_urls:
            return []

        cache = get_photo_cache()
        result = []

        for ph in photo_urls:
            url = self._photo_url(ph)
            if not url:
                continue

            is_cached = cache.is_cached(supplier_type, external_id, url)
            serve_url = get_supplier_photo_url(
                supplier_type,
                external_id,
                url
            )
            result.append({
                'original_url': url,
                'serve_url': serve_url,
                'cached': is_cached,
                'blur': ph.get('blur') if isinstance(ph, dict) else None,
                'has_original': bool(
                    isinstance(ph, str)
                    or ph.get('original')
                    or ph.get('sexoptovik')
                ),
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
            photo_strategy: 'smart_merge' | legacy safe aliases
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
        if (
            not isinstance(fields, list)
            or not fields
            or len(fields) > len(ALLOWED_ENRICHMENT_FIELDS)
            or any(
                not isinstance(field, str)
                or field not in ALLOWED_ENRICHMENT_FIELDS
                for field in fields
            )
            or len(set(fields)) != len(fields)
            or photo_strategy not in SAFE_ENRICHMENT_PHOTO_STRATEGIES
        ):
            return {
                'success': False,
                'fields_applied': [],
                'photos': {'skipped': True},
                'error': 'Invalid enrichment fields or photo strategy',
                'wb_sync': False,
            }

        snapshot_before_local = _create_product_snapshot(product)
        snapshot_before = snapshot_before_local
        wb_snapshot_context = {}
        wb_updates = {}
        fields_applied = []
        errors = []
        merge_decisions = {}

        # --- Текстовые поля ---
        if 'title' in fields:
            sup_title = imp.ai_seo_title or imp.title
            if sup_title:
                wb_updates['title'] = sup_title[:60]

        if 'brand' in fields:
            sup_brand = imp.ai_detected_brand or imp.brand
            if sup_brand:
                wb_updates['brand'] = sup_brand

        if 'description' in fields and imp.description:
            wb_updates['description'] = imp.description[:5000]

        if 'characteristics' in fields or 'dimensions' in fields:
            supplier_characteristics = self._build_supplier_characteristic_source(imp)
        else:
            supplier_characteristics = None

        if (
            'characteristics' in fields
            and supplier_characteristics
            and supplier_characteristics['has_data']
        ):
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

        # A dimensions-only request must still extract package dimensions
        # that arrived in the supplier characteristics column, without also
        # applying unrelated characteristics the seller did not select.
        if (
            'dimensions' in fields
            and 'characteristics' not in fields
            and supplier_characteristics
            and supplier_characteristics['has_data']
        ):
            from services.marketplace_validator import (
                partition_supplier_characteristic_input,
            )
            partition = partition_supplier_characteristic_input(
                product.subject_id,
                supplier_characteristics['values'],
                materials=supplier_characteristics['materials'],
                gender=supplier_characteristics['gender'],
                validation_cache=(
                    validation_cache
                    if validation_cache is not None else {}
                ),
            )
            supplier_characteristics['skipped_characteristics'] = (
                partition['skipped']
            )
            supplier_characteristics['extracted_dimensions'] = (
                partition['dimensions']
            )

        if 'dimensions' in fields:
            dims = {}
            if imp.ai_dimensions:
                try:
                    parsed_dims = json.loads(imp.ai_dimensions)
                    if isinstance(parsed_dims, dict):
                        from services.wb_content_payload import (
                            extract_dimensions,
                        )
                        # Canonicalize aliases before the merge decision. If
                        # aliases were accepted as arbitrary keys and only
                        # dropped later by the wire normalizer, history could
                        # claim a dimensions change that was never sent.
                        dims.update(extract_dimensions(parsed_dims))
                except (json.JSONDecodeError, TypeError):
                    pass
            # Наблюдённые габариты упаковки, исторически лежавшие среди
            # характеристик поставщика: они сильнее AI-оценки.
            if supplier_characteristics:
                dims.update(
                    supplier_characteristics.get('extracted_dimensions') or {})
                # Актуальный original_data snapshot является каноническим
                # observed source и сильнее staging/AI значений.
                dims.update(
                    supplier_characteristics.get('observed_dimensions') or {})
            if dims:
                wb_updates['dimensions'] = dims

        # --- Обновление через WB API (текстовые поля) ---
        wb_sync_success = False
        wb_error = None
        content_history = None
        content_history_id = None
        photo_history = None

        if wb_updates:
            from services.card_snapshot import overlay_snapshot_with_wb_card

            def actual_content_fields(exact_snapshot):
                explicit = exact_snapshot.get('applied_update_fields')
                if isinstance(explicit, list):
                    return [
                        field for field in explicit
                        if field in wb_updates
                    ]
                before = exact_snapshot.get('before') or {}
                after = exact_snapshot.get('after') or {}
                result = []
                for field in wb_updates:
                    if before.get(field) != after.get(field):
                        result.append(field)
                return result

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
                changed_fields = actual_content_fields(exact_snapshot)
                decisions = exact_snapshot.get('merge_decisions')
                if isinstance(decisions, dict):
                    merge_decisions['content'] = decisions
                content_history = CardEditHistory(
                    product_id=product.id,
                    seller_id=seller.id,
                    bulk_edit_id=bulk_edit_id,
                    action='update',
                    changed_fields=changed_fields,
                    snapshot_before=snapshot_before,
                    snapshot_after=snapshot_after,
                    merge_decisions=decisions,
                    wb_synced=False,
                    wb_sync_status='pending',
                    user_comment='Обогащение от поставщика',
                )
                from services.wb_enrichment_reconciliation import (
                    schedule_history_reconciliation,
                )
                # Persist a recovery marker before network I/O. If the process
                # dies immediately afterwards, a read-only reconciliation can
                # still determine whether the request reached WB; it is never
                # blindly replayed.
                schedule_history_reconciliation(
                    content_history,
                    status='pending',
                )
                db.session.add(content_history)
                db.session.commit()
                content_history_id = content_history.id

            def assert_content_settled():
                self._assert_no_unreconciled_write(
                    seller_id=seller.id,
                    nm_id=product.nm_id,
                    operation_kind='content',
                )

            try:
                wb_client.update_card(
                    product.nm_id,
                    wb_updates,
                    merge_with_existing=True,
                    seller_id=seller.id,
                    snapshot_context=wb_snapshot_context,
                    before_send_callback=persist_pending_content_history,
                    preserve_richer_enrichment=True,
                    before_live_read_callback=assert_content_settled,
                )
            except Exception as e:
                # A successfully committed pending row survives this rollback;
                # a callback/DB failure happens before the HTTP request.
                db.session.rollback()
                wb_error = str(e)
                from services.wb_api_client import WBContentOperationBusy
                if content_history_id is not None:
                    content_history = CardEditHistory.query.filter_by(
                        id=content_history_id,
                        product_id=product.id,
                        seller_id=seller.id,
                    ).first()
                    if content_history is not None:
                        from services.wb_api_client import (
                            WBAPIException,
                            WBAuthException,
                            WBContentOperationBusy,
                            WBLiveCardDrift,
                            WBRateLimitException,
                            WBTransportUncertainException,
                        )
                        if isinstance(e, WBTransportUncertainException):
                            definitely_not_sent = not (
                                e.request_may_have_been_applied
                            )
                        else:
                            # Explicit API rejection, auth/rate/validation,
                            # local content lock and observed live drift cannot
                            # have produced an accepted provider mutation.
                            definitely_not_sent = isinstance(e, (
                                WBAPIException,
                                WBAuthException,
                                WBContentOperationBusy,
                                WBLiveCardDrift,
                                WBRateLimitException,
                            ))
                        if definitely_not_sent:
                            content_history.wb_synced = False
                            content_history.wb_sync_status = 'failed'
                            content_history.wb_reconcile_due_at = None
                            content_history.wb_reconciled_at = datetime.utcnow()
                            content_history.wb_reconcile_code = 'request_not_sent'
                            content_history.wb_error_message = wb_error
                        else:
                            from services.wb_enrichment_reconciliation import (
                                schedule_history_reconciliation,
                            )
                            schedule_history_reconciliation(
                                content_history,
                                status='uncertain',
                                error=wb_error,
                            )
                        db.session.commit()
                logger.error(f"[Enrich] WB API error for nmID={product.nm_id}: {e}")
                if (
                    isinstance(e, (
                        WbEnrichmentAwaitingReconciliation,
                        WBContentOperationBusy,
                    ))
                    and content_history_id is None
                ):
                    return {
                        'success': False,
                        'fields_applied': [],
                        'photos': {
                            'skipped': True,
                            'reason': (
                                'content_operation_busy'
                                if isinstance(e, WBContentOperationBusy)
                                else 'previous_content_write_pending'
                            ),
                        },
                        'skipped_characteristics': (
                            (supplier_characteristics or {}).get(
                                'skipped_characteristics'
                            ) or []
                        ),
                        'merge_decisions': merge_decisions,
                        'wb_audit': None,
                        'error': str(e),
                        'wb_sync': False,
                        'wb_confirmed': False,
                        'reconciliation_pending': True,
                        'fields_pending': sorted(set(fields)),
                        'deferred': True,
                    }
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
                write_required = wb_snapshot_context.get(
                    'write_required', True)
                actual_fields = actual_content_fields(wb_snapshot_context)
                fields_applied.extend(actual_fields)

                if not write_required:
                    # The live card already won every merge decision. Persist
                    # the receipt even though no provider write was needed.
                    if content_history is None:
                        persist_pending_content_history(wb_snapshot_context)
                    from services.wb_enrichment_reconciliation import (
                        schedule_history_reconciliation,
                    )
                    schedule_history_reconciliation(content_history)
                    content_history.user_comment = (
                        'Дообогащение: live-данные WB сохранены, изменений нет'
                    )
                    # Product is a local projection of WB.  Even when the
                    # provider write is a no-op, retain the richer observed
                    # live values locally instead of leaving an older staging
                    # value that would mislead the next preview.
                    mirror_sent_card_to_product(wb_snapshot_context['after'])
                    db.session.commit()
                else:
                    wb_sync_success = True
                    logger.info(
                        f"[Enrich] WB API updated nmID={product.nm_id}: "
                        f"{actual_fields}"
                    )

                    # Real client invokes the callback before HTTP. Keep a guarded
                    # fallback for compatible test/custom clients, while preserving
                    # exact snapshots supplied through snapshot_context.
                    if content_history is None:
                        persist_pending_content_history(wb_snapshot_context)

                    from services.wb_enrichment_reconciliation import (
                        schedule_history_reconciliation,
                    )
                    schedule_history_reconciliation(
                        content_history,
                        status='submitted',
                    )
                    # If this local commit fails, the already committed
                    # uncertain row remains a durable recovery marker.
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
                        schedule_history_reconciliation(
                            content_history,
                            status='submitted',
                        )
                        db.session.commit()

        # --- Фото ---
        # Photo changes are intentionally recorded separately: WB media has no
        # supported rollback path and must not make content history unrevertible.
        photo_result = {'skipped': True}
        content_noop = wb_snapshot_context.get('write_required') is False
        if 'photos' in fields and photo_strategy == 'selective':
            photo_result = {'skipped': True, 'reason': 'selective_mode'}
        elif 'photos' in fields and (
            not wb_updates or wb_sync_success or content_noop
        ):
            photo_history = None
            photo_history_id = None

            def persist_photo_plan(receipt):
                nonlocal photo_history, photo_history_id
                report = receipt.get('report') or {}
                merge_decisions['photos'] = report
                before = _create_product_snapshot(product)
                before['photos'] = list(receipt.get('live_urls_before') or [])
                before['photos_json'] = json.dumps(
                    before['photos'], ensure_ascii=False)
                after = dict(before)
                after['photos'] = list(
                    receipt.get('planned_photos_after') or before['photos'])
                after['photos_json'] = json.dumps(
                    after['photos'], ensure_ascii=False)
                write_planned = bool(report.get('append_indices'))
                photo_history = CardEditHistory(
                    product_id=product.id,
                    seller_id=seller.id,
                    bulk_edit_id=bulk_edit_id,
                    action='update',
                    changed_fields=['photos'] if write_planned else [],
                    snapshot_before=before,
                    snapshot_after=after,
                    merge_decisions={'photos': report},
                    wb_synced=False,
                    wb_sync_status='pending' if write_planned else 'skipped',
                    user_comment=(
                        'Умное дообогащение фото: live-галерея WB сохранена'
                    ),
                )
                if write_planned:
                    from services.wb_enrichment_reconciliation import (
                        schedule_history_reconciliation,
                    )
                    schedule_history_reconciliation(
                        photo_history,
                        status='pending',
                    )
                db.session.add(photo_history)
                db.session.commit()
                photo_history_id = photo_history.id

            def assert_photo_settled():
                self._assert_no_unreconciled_write(
                    seller_id=seller.id,
                    nm_id=product.nm_id,
                    operation_kind='photos',
                )

            photo_result = self._apply_photos(
                product, imp, photo_strategy, seller, wb_client,
                is_bulk=is_bulk,
                before_upload_callback=persist_photo_plan,
                before_live_read_callback=assert_photo_settled,
            )
            if photo_result.get('deferred'):
                pending_fields = {'photos'}
                for history in (content_history,):
                    if (
                        history is not None
                        and history.wb_reconcile_due_at is not None
                    ):
                        pending_fields.update(history.changed_fields or [])
                return {
                    'success': False,
                    'fields_applied': fields_applied,
                    'photos': photo_result,
                    'skipped_characteristics': (
                        (supplier_characteristics or {}).get(
                            'skipped_characteristics'
                        ) or []
                    ),
                    'merge_decisions': merge_decisions,
                    'wb_audit': None,
                    'error': photo_result.get('error'),
                    'wb_sync': wb_sync_success,
                    'wb_confirmed': False,
                    'reconciliation_pending': True,
                    'fields_pending': sorted(pending_fields),
                    'deferred': True,
                }
            if photo_history is None:
                # No live plan was reached (for example, no supplier bytes).
                # Keep a durable no-op receipt instead of silently losing the
                # attempted field decision.
                reason = photo_result.get('reason') or photo_result.get(
                    'error') or 'photo_plan_unavailable'
                from services.wb_enrichment_merge import MERGE_POLICY_VERSION
                report = {
                    'policy': MERGE_POLICY_VERSION,
                    'mode': 'preserve_live_append_missing',
                    'requested_strategy': photo_strategy,
                    'decision': 'skipped_before_live_plan',
                    'reason': str(reason)[:100],
                }
                merge_decisions['photos'] = report
                snapshot = _create_product_snapshot(product)
                photo_history = CardEditHistory(
                    product_id=product.id,
                    seller_id=seller.id,
                    bulk_edit_id=bulk_edit_id,
                    action='update',
                    changed_fields=[],
                    snapshot_before=snapshot,
                    snapshot_after=snapshot,
                    merge_decisions={'photos': report},
                    wb_synced=False,
                    wb_sync_status=(
                        'skipped' if photo_result.get('skipped') else 'failed'
                    ),
                    wb_error_message=(
                        None if photo_result.get('skipped') else str(reason)
                    ),
                    user_comment='Дообогащение фото без изменений',
                )
                db.session.add(photo_history)
                db.session.commit()
                photo_history_id = photo_history.id

            uploaded_count = int(photo_result.get('uploaded') or 0)
            uncertain_count = int(photo_result.get('uncertain') or 0)
            failed_count = int(photo_result.get('failed') or 0)
            potentially_submitted = (
                uploaded_count + uncertain_count
                if set(photo_history.changed_fields or []) == {'photos'}
                else 0
            )
            if potentially_submitted > 0:
                fields_applied.append('photos')
                actual_after = dict(photo_history.snapshot_after or {})
                if 'snapshot_after_photos' in photo_result:
                    actual_after['photos'] = list(
                        photo_result.get('snapshot_after_photos') or [])
                    actual_after['photos_json'] = json.dumps(
                        actual_after['photos'], ensure_ascii=False)
                photo_history.snapshot_after = actual_after
                photo_receipt = dict(photo_history.merge_decisions or {})
                photo_report = dict(photo_receipt.get('photos') or {})
                photo_report['upload'] = {
                    'uploaded': uploaded_count,
                    'uncertain': uncertain_count,
                    'potentially_submitted': potentially_submitted,
                    'failed': failed_count,
                    'rejected': int(photo_result.get('rejected') or 0),
                    'not_attempted': int(
                        photo_result.get('not_attempted') or 0
                    ),
                    'items': list(photo_result.get('upload_items') or [])[:30],
                }
                photo_receipt['photos'] = photo_report
                photo_history.merge_decisions = photo_receipt
                from services.wb_enrichment_reconciliation import (
                    schedule_history_reconciliation,
                )
                if uncertain_count > 0:
                    photo_error = (
                        f'подтверждено отправкой {uploaded_count}, '
                        f'исход {uncertain_count} фото пока неизвестен, '
                        f'не отправлено {failed_count}'
                    )
                    schedule_history_reconciliation(
                        photo_history,
                        status='uncertain',
                        error=photo_error,
                    )
                    errors.append(f'Фото WB: {photo_error}')
                elif failed_count > 0:
                    photo_error = (
                        f'загружено {uploaded_count}, '
                        f'не загружено {failed_count}'
                    )
                    schedule_history_reconciliation(
                        photo_history,
                        status='partial',
                        error=photo_error,
                    )
                    errors.append(f'Фото WB: {photo_error}')
                else:
                    schedule_history_reconciliation(
                        photo_history,
                        status='submitted',
                    )
            elif photo_result.get('skipped'):
                photo_history.wb_sync_status = 'skipped'
                photo_history.wb_reconcile_due_at = None
                photo_history.wb_reconciled_at = datetime.utcnow()
                photo_history.wb_reconcile_code = 'no_change'
            else:
                photo_error = (
                    photo_result.get('error')
                    or f'не загружено, ошибок: {photo_result.get("failed", 0)}'
                )
                photo_history.wb_sync_status = 'failed'
                photo_history.wb_reconcile_due_at = None
                photo_history.wb_reconciled_at = datetime.utcnow()
                photo_history.wb_reconcile_code = str(
                    photo_result.get('reconcile_code') or 'upload_failed'
                )[:64]
                photo_history.wb_error_message = str(photo_error)
                errors.append(f'Фото WB: {photo_error}')
            photo_result.pop('live_urls_before', None)
            photo_result.pop('snapshot_after_photos', None)
        elif 'photos' in fields:
            photo_result = {
                'skipped': True,
                'reason': 'content_update_failed',
            }

        # A transport-uncertain content write may still appear in WB later.
        # When photos were selected in the same durable row, advancing the
        # cursor here would silently drop that follow-up. Keep the row parked;
        # restart recovery will remove content from remaining_fields and build
        # a fresh photo-only plan after content reconciliation is terminal.
        photo_followup_deferred = bool(
            'photos' in fields
            and content_history is not None
            and content_history.wb_reconcile_due_at is not None
            and not wb_sync_success
            and not content_noop
        )
        if photo_followup_deferred:
            photo_result = {
                **photo_result,
                'skipped': True,
                'reason': 'awaiting_content_reconciliation',
                'deferred': True,
            }

        # --- Связываем ImportedProduct с Product (если ещё не) ---
        if imp.product_id is None:
            imp.product_id = product.id

        product.updated_at = datetime.utcnow()
        db.session.commit()

        photo_sync_success = int(photo_result.get('uploaded') or 0) > 0
        reconciliation_pending = any(
            history is not None
            and history.wb_reconcile_due_at is not None
            for history in (content_history, photo_history)
        )
        fields_pending = sorted({
            field
            for history in (content_history, photo_history)
            if history is not None and history.wb_reconcile_due_at is not None
            for field in (history.changed_fields or [])
        })
        if photo_followup_deferred and 'photos' not in fields_pending:
            fields_pending.append('photos')
            fields_pending.sort()
        skipped_characteristics = (
            (supplier_characteristics or {}).get('skipped_characteristics')
            or []
        )

        return {
            'success': not bool(errors),
            'fields_applied': fields_applied,
            'photos': photo_result,
            'skipped_characteristics': skipped_characteristics,
            'merge_decisions': merge_decisions,
            'wb_audit': None,
            'error': '; '.join(errors) if errors else None,
            'wb_sync': wb_sync_success or photo_sync_success,
            'wb_confirmed': False,
            'reconciliation_pending': reconciliation_pending,
            'fields_pending': fields_pending,
            'deferred': photo_followup_deferred,
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
            partition_supplier_characteristic_input,
        )
        # Общий кэш на оба вызова: иначе partition и строгий билдер грузят
        # одну и ту же схему категории из БД дважды на карточку.
        if validation_cache is None:
            validation_cache = {}
        # Габариты упаковки и вне-схемные необязательные имена отделяются до
        # строгого билдера: они попадают в source['skipped_characteristics'] /
        # source['extracted_dimensions'] и не блокируют валидные поля.
        partition = partition_supplier_characteristic_input(
            subject_id,
            source['values'],
            materials=source['materials'],
            gender=source['gender'],
            validation_cache=validation_cache,
        )
        source['skipped_characteristics'] = partition['skipped']
        source['extracted_dimensions'] = partition['dimensions']
        return build_wb_supplier_characteristic_patch(
            subject_id,
            partition['values'],
            materials=partition['materials'],
            gender=partition['gender'],
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
        *,
        expected_source_urls: Optional[List[str]] = None,
    ) -> Dict:
        """
        Применяет выборочные фото от поставщика к WB-карточке.

        Args:
            product: Product ORM объект
            imp: ImportedProduct ORM объект
            photo_indices: список индексов фото поставщика для применения
            strategy: 'smart_merge' | legacy safe aliases
            seller: Seller ORM объект
            wb_client: WildberriesAPIClient экземпляр

        Returns:
            {'success': bool, 'uploaded': int, 'failed': int, 'error': str|None}
        """
        from models import db, CardEditHistory
        from services.photo_cache import get_photo_cache
        from services.wb_api_client import MAX_WB_MEDIA_FILES

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
        if (
            strategy not in SAFE_ENRICHMENT_PHOTO_STRATEGIES
            or
            not isinstance(photo_indices, list)
            or not 1 <= len(photo_indices) <= MAX_WB_MEDIA_FILES
            or any(
                not isinstance(idx, int)
                or isinstance(idx, bool)
                or idx < 0
                for idx in photo_indices
            )
            or len(set(photo_indices)) != len(photo_indices)
        ):
            return {
                'success': False,
                'uploaded': 0,
                'error': (
                    'Invalid photo strategy or photo_indices; indices must '
                    'be unique non-negative integers'
                ),
            }

        all_photos, supplier_type, external_id = self._photo_source(imp)
        if not all_photos:
            return {'success': False, 'uploaded': 0, 'error': 'Нет фото у поставщика'}

        if any(idx >= len(all_photos) for idx in photo_indices):
            return {
                'success': False,
                'uploaded': 0,
                'error': 'photo_indices contains an unavailable position',
            }
        if expected_source_urls is not None:
            if (
                not isinstance(expected_source_urls, list)
                or len(expected_source_urls) != len(photo_indices)
                or any(
                    not isinstance(value, str) or not value
                    for value in expected_source_urls
                )
            ):
                return {
                    'success': False,
                    'uploaded': 0,
                    'error': 'Invalid expected selective photo source',
                    'definitely_not_sent': True,
                }
            current_source_urls = [
                self._photo_url(all_photos[idx]) for idx in photo_indices
            ]
            if current_source_urls != expected_source_urls:
                return {
                    'success': False,
                    'uploaded': 0,
                    'reason': 'supplier_photo_source_drift',
                    'error': (
                        'Галерея поставщика изменилась после выбора; '
                        'фото не отправлялись'
                    ),
                    'definitely_not_sent': True,
                }
        selected_photos = [all_photos[idx] for idx in photo_indices]

        cache = get_photo_cache()

        # Получаем auth cookies
        auth_cookies = None
        if supplier_type == 'sexoptovik':
            auth_cookies = self._get_sexoptovik_auth(seller)

        # Загружаем выбранные фото в кэш
        for ph in selected_photos:
            url = self._photo_url(ph)
            if url and not cache.is_cached(supplier_type, external_id, url):
                cache.queue_download(supplier_type, external_id, url,
                                     auth_cookies=auth_cookies,
                                     fallback_urls=self._photo_fallbacks(ph, url))

        # Ждём кэширования (max 30 сек, early exit при stall)
        cached_paths = self._wait_for_cached_photos(selected_photos, supplier_type, external_id, cache, timeout=30)

        if not cached_paths:
            return {
                'success': False,
                'uploaded': 0,
                'error': 'Не удалось загрузить фото поставщика',
            }

        history = None
        history_id = None

        def persist_photo_plan(receipt):
            nonlocal history, history_id
            report = receipt.get('report') or {}
            before = _create_product_snapshot(product)
            before['photos'] = list(receipt.get('live_urls_before') or [])
            before['photos_json'] = json.dumps(
                before['photos'], ensure_ascii=False)
            after = dict(before)
            after['photos'] = list(
                receipt.get('planned_photos_after') or before['photos'])
            after['photos_json'] = json.dumps(
                after['photos'], ensure_ascii=False)
            write_planned = bool(report.get('append_indices'))
            history = CardEditHistory(
                product_id=product.id,
                seller_id=seller.id,
                action='update',
                changed_fields=['photos'] if write_planned else [],
                snapshot_before=before,
                snapshot_after=after,
                merge_decisions={'photos': report},
                wb_synced=False,
                wb_sync_status='pending' if write_planned else 'skipped',
                user_comment=(
                    f'Выборочное умное дообогащение фото '
                    f'({len(photo_indices)} выбрано)'
                ),
            )
            if write_planned:
                from services.wb_enrichment_reconciliation import (
                    schedule_history_reconciliation,
                )
                schedule_history_reconciliation(
                    history,
                    status='pending',
                )
            db.session.add(history)
            db.session.commit()
            history_id = history.id

        def assert_photo_settled():
            self._assert_no_unreconciled_write(
                seller_id=seller.id,
                nm_id=product.nm_id,
                operation_kind='photos',
            )

        try:
            result = self.merge_photos_to_card_locked(
                wb_client,
                seller_id=seller.id,
                nm_id=product.nm_id,
                photo_paths=cached_paths,
                strategy=strategy,
                before_upload_callback=persist_photo_plan,
                before_live_read_callback=assert_photo_settled,
            )
            uploaded_count = int(result.get('uploaded') or 0)
            uncertain_count = int(result.get('uncertain') or 0)
            failed_count = int(result.get('failed') or 0)
            potentially_submitted = uploaded_count + uncertain_count

            product.updated_at = datetime.utcnow()

            if history is not None:
                actual_after = dict(history.snapshot_after or {})
                actual_after['photos'] = list(
                    result.get('snapshot_after_photos') or [])
                actual_after['photos_json'] = json.dumps(
                    actual_after['photos'], ensure_ascii=False)
                history.snapshot_after = actual_after
                photo_receipt = dict(history.merge_decisions or {})
                photo_report = dict(photo_receipt.get('photos') or {})
                photo_report['upload'] = {
                    'uploaded': uploaded_count,
                    'uncertain': uncertain_count,
                    'potentially_submitted': potentially_submitted,
                    'failed': failed_count,
                    'rejected': int(result.get('rejected') or 0),
                    'not_attempted': int(
                        result.get('not_attempted') or 0
                    ),
                    'items': list(result.get('upload_items') or [])[:30],
                }
                photo_receipt['photos'] = photo_report
                history.merge_decisions = photo_receipt
                if uncertain_count > 0:
                    from services.wb_enrichment_reconciliation import (
                        schedule_history_reconciliation,
                    )
                    photo_error = (
                        f'подтверждено отправкой {uploaded_count}, '
                        f'исход {uncertain_count} фото пока неизвестен, '
                        f'не отправлено {failed_count}'
                    )
                    schedule_history_reconciliation(
                        history,
                        status='uncertain',
                        error=photo_error,
                    )
                elif uploaded_count > 0 and failed_count == 0:
                    from services.wb_enrichment_reconciliation import (
                        schedule_history_reconciliation,
                    )
                    schedule_history_reconciliation(
                        history,
                        status='submitted',
                    )
                elif uploaded_count > 0:
                    from services.wb_enrichment_reconciliation import (
                        schedule_history_reconciliation,
                    )
                    photo_error = (
                        f'загружено {uploaded_count}, '
                        f'не загружено {failed_count}'
                    )
                    schedule_history_reconciliation(
                        history,
                        status='partial',
                        error=photo_error,
                    )
                elif result.get('skipped'):
                    history.wb_sync_status = 'skipped'
                    history.wb_reconcile_due_at = None
                    history.wb_reconciled_at = datetime.utcnow()
                    history.wb_reconcile_code = 'no_change'
                else:
                    history.wb_sync_status = 'failed'
                    history.wb_error_message = str(
                        result.get('error') or 'Ни одно фото не загружено'
                    )[:1000]
                    history.wb_reconcile_due_at = None
                    history.wb_reconciled_at = datetime.utcnow()
                    history.wb_reconcile_code = str(
                        result.get('reconcile_code') or 'upload_failed'
                    )[:64]
            db.session.commit()

            result['error'] = history.wb_error_message if history else None
            result['wb_confirmed'] = False
            result['reconciliation_pending'] = potentially_submitted > 0
            result.pop('live_urls_before', None)
            result.pop('snapshot_after_photos', None)
            return result
        except WbEnrichmentAwaitingReconciliation as e:
            db.session.rollback()
            return {
                'success': False,
                'uploaded': 0,
                'uncertain': 0,
                'skipped': True,
                'reason': 'previous_photo_write_pending',
                'error': str(e),
                'deferred': True,
                'reconciliation_pending': True,
            }
        except WbMediaOperationBusy as e:
            db.session.rollback()
            return {
                'success': False,
                'uploaded': 0,
                'uncertain': 0,
                'skipped': True,
                'reason': 'media_operation_busy',
                'error': str(e),
                'deferred': True,
                'reconciliation_pending': True,
            }
        except WbLiveMediaDrift as e:
            db.session.rollback()
            persisted_history = (
                CardEditHistory.query.filter_by(
                    id=history_id,
                    product_id=product.id,
                    seller_id=seller.id,
                ).first()
                if history_id is not None else None
            )
            if persisted_history is not None:
                persisted_history.wb_sync_status = 'failed'
                persisted_history.wb_error_message = str(e)
                persisted_history.wb_reconcile_due_at = None
                persisted_history.wb_reconciled_at = datetime.utcnow()
                persisted_history.wb_reconcile_code = (
                    'live_photo_drift'
                )
                db.session.commit()
            return {
                'success': False,
                'uploaded': 0,
                'uncertain': 0,
                'error': str(e),
                'definitely_not_sent': True,
            }
        except Exception as e:
            db.session.rollback()
            persisted_history = (
                CardEditHistory.query.filter_by(
                    id=history_id,
                    product_id=product.id,
                    seller_id=seller.id,
                ).first()
                if history_id is not None else None
            )
            if persisted_history is not None:
                # A timeout/commit error after upload is ambiguous; retain a
                # durable read-only reconciliation instead of retrying upload.
                from services.wb_enrichment_reconciliation import (
                    schedule_history_reconciliation,
                )
                schedule_history_reconciliation(
                    persisted_history,
                    status='uncertain',
                    error=str(e),
                )
                db.session.commit()
            logger.error(f"[Enrich] Selective photo upload error for nmID={product.nm_id}: {e}")
            return {
                'success': False,
                'uploaded': 0,
                'uncertain': 1 if persisted_history is not None else 0,
                'error': str(e),
                'reconciliation_pending': persisted_history is not None,
            }

    def _apply_photos(
        self,
        product,
        imp,
        strategy: str,
        seller,
        wb_client,
        is_bulk: bool = False,
        before_upload_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        before_live_read_callback: Optional[Callable[[], None]] = None,
    ) -> Dict:
        """
        Скачивает фото поставщика (через кэш) и загружает в карточку WB.

        strategy:
            'smart_merge'  - сохранить live-галерею и добавить отсутствующие
            'replace'/'append' - legacy aliases того же безопасного merge
            'only_if_empty' - только если у карточки нет фото
        is_bulk:
            True при вызове из _run_bulk_job — использует синхронную загрузку
            для надёжности (queue_download ненадёжен в bulk-режиме)
        """
        from services.photo_cache import get_photo_cache

        photo_urls, supplier_type, external_id = self._photo_source(imp)

        if not photo_urls:
            return {'skipped': True, 'reason': 'empty_photo_list'}

        from services.wb_api_client import MAX_WB_MEDIA_FILES
        photo_urls = photo_urls[:MAX_WB_MEDIA_FILES]

        cache = get_photo_cache()

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
                url = self._photo_url(ph)
                if url and not cache.is_cached(supplier_type, external_id, url):
                    cache.queue_download(supplier_type, external_id, url,
                                         auth_cookies=auth_cookies,
                                         fallback_urls=self._photo_fallbacks(ph, url))

            cached_paths = self._wait_for_cached_photos(
                photo_urls, supplier_type, external_id, cache, timeout=30
            )

        if not cached_paths:
            return {'skipped': True, 'reason': 'photos_not_cached_after_timeout'}

        # Multipart is the reliable path used by the working single-card flow.
        # Preserve seller/category standard pins without relying on public URLs:
        # compose their exact local files around the fresh supplier gallery.
        try:
            from flask import current_app
            from models import get_min_photos, get_standard_media
            from services.standard_photos import compose_card_photo_paths

            media_dir = (
                Path(current_app.root_path)
                / 'data'
                / 'global_media'
                / str(seller.id)
            )
            composed_paths = compose_card_photo_paths(
                cached_paths,
                get_standard_media(seller.id, getattr(product, 'subject_id', None)),
                media_dir,
                get_min_photos(seller.id),
            )
            if composed_paths:
                cached_paths = composed_paths
        except Exception as exc:
            # Standard media is optional; supplier photos must still proceed.
            logger.warning(
                '[Enrich] Could not compose standard photos for nmID=%s: %s',
                product.nm_id,
                exc,
            )

        # Live-aware merge under one seller media lock. Legacy names no longer
        # authorize replacement: enrichment always preserves manual WB slots.
        try:
            result = self.merge_photos_to_card_locked(
                wb_client,
                seller_id=seller.id,
                nm_id=product.nm_id,
                photo_paths=cached_paths,
                strategy=strategy,
                before_upload_callback=before_upload_callback,
                before_live_read_callback=before_live_read_callback,
            )
            logger.info(
                '[Enrich] Smart photo merge nmID=%s: %s uploaded, %s preserved',
                product.nm_id,
                result.get('uploaded', 0),
                (result.get('merge_report') or {}).get('live_count', 0),
            )
            return result
        except WbEnrichmentAwaitingReconciliation as e:
            logger.info(
                '[Enrich] Photo upload deferred for nmID=%s: %s',
                product.nm_id,
                e,
            )
            return {
                'success': False,
                'uploaded': 0,
                'uncertain': 0,
                'skipped': True,
                'reason': 'previous_photo_write_pending',
                'error': str(e),
                'deferred': True,
                'reconciliation_pending': True,
            }
        except WbMediaOperationBusy as e:
            logger.info(
                '[Enrich] Photo upload deferred while media lock is busy '
                'for nmID=%s',
                product.nm_id,
            )
            return {
                'success': False,
                'uploaded': 0,
                'uncertain': 0,
                'skipped': True,
                'reason': 'media_operation_busy',
                'error': str(e),
                'deferred': True,
                'reconciliation_pending': True,
            }
        except WbLiveMediaDrift as e:
            logger.error(
                '[Enrich] Photo upload safely stopped for nmID=%s: %s',
                product.nm_id,
                e,
            )
            return {
                'uploaded': 0,
                'uncertain': 0,
                'error': str(e),
                'reason': 'live_photo_drift',
                'reconcile_code': 'live_photo_drift',
                'definitely_not_sent': True,
            }
        except Exception as e:
            logger.error(f"[Enrich] Photo upload error for nmID={product.nm_id}: {e}")
            return {'uploaded': 0, 'uncertain': 1, 'error': str(e)}

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

        total = sum(1 for ph in photo_urls if self._photo_url(ph))
        if total == 0:
            return []

        while time.time() < deadline:
            cached_paths = []
            for ph in photo_urls:
                url = self._photo_url(ph)
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
            url = self._photo_url(ph)
            if not url:
                continue

            # Уже в кэше — сразу берём
            if cache.is_cached(supplier_type, external_id, url):
                cached_paths.append(cache.get_cache_path(supplier_type, external_id, url))
                continue

            # Скачиваем синхронно с fallbacks
            try:
                success = cache.download_now(
                    supplier_type, external_id, url,
                    auth_cookies=auth_cookies,
                    fallback_urls=self._photo_fallbacks(ph, url),
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
        *,
        fields_by_product: Optional[Dict[int, List[str]]] = None,
    ) -> str:
        """Create a durable bulk cursor and kick one bounded worker tick.

        ``fields_by_product`` is used by review screens where every row can
        have its own explicit checkbox selection.  It is persisted in the
        same leased cursor as the product IDs; a worker never widens one row
        to the union selected for another row.
        """
        from models import BulkEditHistory, db, EnrichmentJob

        if (
            not isinstance(product_ids, list)
            or not 1 <= len(product_ids) <= 200
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                for value in product_ids
            )
            or len(set(product_ids)) != len(product_ids)
        ):
            raise ValueError('product_ids must contain 1..200 unique positive integers')
        if (
            not isinstance(fields, list)
            or not fields
            or len(fields) > len(ALLOWED_ENRICHMENT_FIELDS)
            or any(
                not isinstance(field, str)
                or field not in ALLOWED_ENRICHMENT_FIELDS
                for field in fields
            )
            or len(set(fields)) != len(fields)
            or photo_strategy not in SAFE_ENRICHMENT_PHOTO_STRATEGIES
        ):
            raise ValueError('invalid enrichment fields or photo strategy')
        fields_payload: Any = list(fields)
        if fields_by_product is not None:
            if (
                not isinstance(fields_by_product, dict)
                or set(fields_by_product) != set(product_ids)
                or any(
                    not isinstance(product_id, int)
                    or isinstance(product_id, bool)
                    or product_id <= 0
                    for product_id in fields_by_product
                )
            ):
                raise ValueError(
                    'fields_by_product must exactly match product_ids'
                )
            normalized_by_product = {}
            for product_id in product_ids:
                item_fields = fields_by_product.get(product_id)
                if (
                    not isinstance(item_fields, list)
                    or not item_fields
                    or len(item_fields) > len(ALLOWED_ENRICHMENT_FIELDS)
                    or any(
                        not isinstance(field, str)
                        or field not in ALLOWED_ENRICHMENT_FIELDS
                        for field in item_fields
                    )
                    or len(set(item_fields)) != len(item_fields)
                ):
                    raise ValueError(
                        'fields_by_product contains invalid enrichment fields'
                    )
                normalized_by_product[str(product_id)] = list(item_fields)
            if set(fields) != {
                field
                for item_fields in normalized_by_product.values()
                for field in item_fields
            }:
                raise ValueError(
                    'fields must exactly equal fields_by_product union'
                )
            fields_payload = {
                'version': 1,
                'default': list(fields),
                'by_product': normalized_by_product,
            }
        if (
            not isinstance(getattr(seller, 'id', None), int)
            or isinstance(seller.id, bool)
            or seller.id <= 0
        ):
            raise ValueError('seller must have a positive integer id')

        from services.marketplace_operation_locks import (
            release_wb_seller_enrichment_job_lock,
            try_wb_seller_enrichment_job_lock,
        )
        creation_claim = try_wb_seller_enrichment_job_lock(seller.id)
        if creation_claim is None:
            raise EnrichmentJobAlreadyActive()
        try:
            active_job = EnrichmentJob.query.filter(
                EnrichmentJob.seller_id == seller.id,
                EnrichmentJob.status.in_(('pending', 'running')),
            ).order_by(EnrichmentJob.created_at.desc()).first()
            if active_job is not None:
                raise EnrichmentJobAlreadyActive(active_job.id)

            job_id = str(uuid.uuid4())
            bulk_history = BulkEditHistory(
                seller_id=seller.id,
                operation_type='supplier_enrichment',
                operation_params={
                    'job_id': job_id,
                    'fields': fields,
                    'photo_strategy': photo_strategy,
                    'per_product_fields': fields_by_product is not None,
                },
                description=(
                    'Массовое дообогащение фото карточек от поставщика'
                    if fields == ['photos']
                    else 'Дообогащение карточек данными поставщика'
                ),
                status='in_progress',
                total_products=len(product_ids),
                success_count=0,
                error_count=0,
                errors_details=[],
                wb_synced=False,
            )
            db.session.add(bulk_history)
            db.session.flush()
            job = EnrichmentJob(
                id=job_id,
                seller_id=seller.id,
                status='pending',
                total=len(product_ids),
                processed=0,
                succeeded=0,
                failed=0,
                skipped=0,
                confirmed=0,
                conflicted=0,
                fields_config=json.dumps(fields_payload, ensure_ascii=False),
                photo_strategy=photo_strategy,
                results=json.dumps([]),
                product_ids_json=json.dumps(product_ids),
                bulk_edit_id=bulk_history.id,
            )
            db.session.add(job)
            db.session.commit()
        finally:
            release_wb_seller_enrichment_job_lock(creation_claim)

        # Захватываем Flask app для фонового потока
        # ВАЖНО: не делаем `from seller_platform import app` в потоке —
        # это circular import, который молча роняет фоновый поток.
        from flask import current_app
        try:
            flask_app = current_app._get_current_object()
        except RuntimeError:
            flask_app = None

        if not flask_app:
            # The singleton scheduler owns recovery. Lack of a request-bound
            # app object must never turn a durable pending job into a failure.
            logger.info('[Enrich] Job %s queued for scheduler', job_id)
            return job_id

        # Запускаем в фоне
        thread = threading.Thread(
            target=self._run_bulk_job,
            args=(
                job_id, list(product_ids), list(fields), photo_strategy,
                seller.id, flask_app,
            ),
            kwargs={
                'item_limit': enrichment_items_per_tick(),
                'time_budget_seconds': enrichment_tick_seconds(),
            },
            daemon=True,
            name=f'EnrichJob-{job_id[:8]}'
        )
        try:
            thread.start()
        except Exception:
            logger.exception('[Enrich] Could not start bulk job %s', job_id)
            # Still pending and recoverable by the scheduler.
            return job_id

        logger.info(f"[Enrich] Bulk job {job_id} started: {len(product_ids)} products, fields={fields}")
        return job_id

    def _run_bulk_job(
        self,
        job_id: str,
        product_ids: List[int],
        fields: List[str],
        photo_strategy: str,
        seller_id: int,
        flask_app=None,
        *,
        item_limit: Optional[int] = None,
        time_budget_seconds: Optional[int] = None,
    ):
        """Advance a leased durable job; every completed row is committed."""
        from models import (
            BulkEditHistory,
            CardEditHistory,
            db,
            EnrichmentJob,
            Product,
            Seller,
        )
        from services.wb_api_client import WildberriesAPIClient

        if not flask_app:
            logger.error(f"[Enrich] Job {job_id}: no Flask app provided")
            return

        with flask_app.app_context():
            claim_token = uuid.uuid4().hex
            wb_client = None
            try:
                job = db.session.get(EnrichmentJob, job_id)
                if not job:
                    logger.error('[Enrich] Job %s not found', job_id)
                    return

                if job.status in {'done', 'failed'}:
                    return

                # Backward compatibility for durable rows created before v2
                # (and for direct unit/maintenance calls).
                try:
                    persisted_ids = json.loads(job.product_ids_json or '[]')
                except (TypeError, ValueError):
                    persisted_ids = []
                legacy_payload = not persisted_ids
                if legacy_payload and product_ids:
                    persisted_ids = list(product_ids)
                    job.product_ids_json = json.dumps(persisted_ids)
                    job.total = len(persisted_ids)
                try:
                    persisted_fields_payload = json.loads(
                        job.fields_config or '[]'
                    )
                except (TypeError, ValueError):
                    persisted_fields_payload = []
                if (
                    (legacy_payload or not persisted_fields_payload)
                    and fields
                ):
                    persisted_fields_payload = list(fields)
                    job.fields_config = json.dumps(persisted_fields_payload)

                item_fields_by_product = None
                if isinstance(persisted_fields_payload, dict):
                    if set(persisted_fields_payload) != {
                        'version', 'default', 'by_product',
                    } or persisted_fields_payload.get('version') != 1:
                        raise RuntimeError('invalid_durable_job_payload')
                    persisted_fields = persisted_fields_payload.get('default')
                    raw_by_product = persisted_fields_payload.get('by_product')
                    if not isinstance(raw_by_product, dict):
                        raise RuntimeError('invalid_durable_job_payload')
                    parsed_by_product = {}
                    for raw_product_id, item_fields in raw_by_product.items():
                        if (
                            not isinstance(raw_product_id, str)
                            or not raw_product_id.isdigit()
                            or raw_product_id.startswith('0')
                        ):
                            raise RuntimeError('invalid_durable_job_payload')
                        product_id = int(raw_product_id)
                        if product_id in parsed_by_product:
                            raise RuntimeError('invalid_durable_job_payload')
                        parsed_by_product[product_id] = item_fields
                    if set(parsed_by_product) != set(persisted_ids):
                        raise RuntimeError('invalid_durable_job_payload')
                    item_fields_by_product = parsed_by_product
                else:
                    persisted_fields = persisted_fields_payload
                if (
                    not isinstance(persisted_ids, list)
                    or not 1 <= len(persisted_ids) <= 200
                    or len(persisted_ids) != job.total
                    or any(
                        not isinstance(value, int)
                        or isinstance(value, bool)
                        or value <= 0
                        for value in persisted_ids
                    )
                    or len(set(persisted_ids)) != len(persisted_ids)
                    or not isinstance(persisted_fields, list)
                    or not persisted_fields
                    or len(persisted_fields) > len(ALLOWED_ENRICHMENT_FIELDS)
                    or any(
                        not isinstance(field, str)
                        or field not in ALLOWED_ENRICHMENT_FIELDS
                        for field in persisted_fields
                    )
                    or len(set(persisted_fields)) != len(persisted_fields)
                    or (
                        item_fields_by_product is not None
                        and any(
                            not isinstance(item_fields, list)
                            or not item_fields
                            or len(item_fields)
                            > len(ALLOWED_ENRICHMENT_FIELDS)
                            or any(
                                not isinstance(field, str)
                                or field not in ALLOWED_ENRICHMENT_FIELDS
                                for field in item_fields
                            )
                            or len(set(item_fields)) != len(item_fields)
                            for item_fields in item_fields_by_product.values()
                        )
                    )
                    or (
                        item_fields_by_product is not None
                        and set(persisted_fields) != {
                            field
                            for item_fields in item_fields_by_product.values()
                            for field in item_fields
                        }
                    )
                ):
                    raise RuntimeError('invalid_durable_job_payload')
                product_ids = persisted_ids
                fields = persisted_fields
                photo_strategy = job.photo_strategy or photo_strategy or 'smart_merge'
                if photo_strategy not in SAFE_ENRICHMENT_PHOTO_STRATEGIES:
                    raise RuntimeError('invalid_durable_job_payload')
                if (
                    not isinstance(job.seller_id, int)
                    or isinstance(job.seller_id, bool)
                    or job.seller_id <= 0
                ):
                    raise RuntimeError('invalid_durable_job_payload')
                seller_id = job.seller_id

                now = datetime.utcnow()
                claimed = EnrichmentJob.query.filter(
                    EnrichmentJob.id == job_id,
                    EnrichmentJob.status.in_(('pending', 'running')),
                    db.or_(
                        EnrichmentJob.claim_token.is_(None),
                        EnrichmentJob.claim_expires_at.is_(None),
                        EnrichmentJob.claim_expires_at <= now,
                    ),
                ).update({
                    EnrichmentJob.status: 'running',
                    EnrichmentJob.claim_token: claim_token,
                    EnrichmentJob.claim_expires_at: now + timedelta(
                        seconds=enrichment_lease_seconds()
                    ),
                    EnrichmentJob.heartbeat_at: now,
                    EnrichmentJob.updated_at: now,
                }, synchronize_session=False)
                db.session.commit()
                if claimed != 1:
                    return
                db.session.expire_all()
                job = db.session.get(EnrichmentJob, job_id)
                if not job or job.claim_token != claim_token:
                    return

                seller = db.session.get(Seller, seller_id)
                if not seller:
                    raise RuntimeError('seller_not_found')

                wb_client = WildberriesAPIClient(seller.wb_api_key)
                bulk_history = (
                    db.session.get(BulkEditHistory, job.bulk_edit_id)
                    if job.bulk_edit_id else None
                )
                if bulk_history is not None and (
                    bulk_history.seller_id != seller_id
                    or bulk_history.operation_type != 'supplier_enrichment'
                ):
                    raise RuntimeError('invalid_durable_job_payload')
                if bulk_history is None:
                    bulk_history = BulkEditHistory(
                        seller_id=seller_id,
                        operation_type='supplier_enrichment',
                        operation_params={
                            'job_id': job_id,
                            'fields': fields,
                            'photo_strategy': photo_strategy,
                        },
                        description='Дообогащение карточек данными поставщика',
                        status='in_progress',
                        total_products=len(product_ids),
                        success_count=0,
                        error_count=0,
                        errors_details=[],
                        wb_synced=False,
                    )
                    db.session.add(bulk_history)
                    db.session.flush()
                    job.bulk_edit_id = bulk_history.id
                    db.session.commit()
                bulk_edit_id = bulk_history.id

                try:
                    results = json.loads(job.results or '[]')
                except (TypeError, ValueError):
                    results = []
                counters = (
                    job.processed, job.succeeded, job.failed, job.skipped,
                )
                if (
                    not isinstance(results, list)
                    or any(
                        not isinstance(value, int)
                        or isinstance(value, bool)
                        or value < 0
                        for value in counters
                    )
                    or job.processed > len(product_ids)
                    or len(results) != job.processed
                    or job.succeeded + job.failed + job.skipped
                    != job.processed
                ):
                    raise RuntimeError('invalid_durable_job_progress')
                succeeded = int(job.succeeded or 0)
                failed = int(job.failed or 0)
                skipped = int(job.skipped or 0)

                validation_cache = {}
                handled = 0
                deadline = (
                    time.monotonic() + time_budget_seconds
                    if time_budget_seconds else None
                )
                start_index = int(job.processed or 0)
                for zero_index in range(start_index, len(product_ids)):
                    if item_limit is not None and handled >= item_limit:
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    product_id = product_ids[zero_index]
                    requested_item_fields = (
                        list(item_fields_by_product[product_id])
                        if item_fields_by_product is not None
                        else list(fields)
                    )
                    resumed_inflight = bool(
                        job.current_product_id == product_id
                        and job.current_item_started_at is not None
                    )
                    if not resumed_inflight:
                        job.current_product_id = product_id
                        job.current_item_started_at = datetime.utcnow()
                    job.heartbeat_at = datetime.utcnow()
                    job.claim_expires_at = datetime.utcnow() + timedelta(
                        seconds=enrichment_lease_seconds()
                    )
                    db.session.commit()

                    remaining_fields = list(requested_item_fields)
                    recovered_receipts = []
                    blocked_followup = False
                    recovered_photo_sent = False
                    recovered_terminal_failure = None
                    if resumed_inflight:
                        histories = CardEditHistory.query.filter(
                            CardEditHistory.seller_id == seller_id,
                            CardEditHistory.product_id == product_id,
                            CardEditHistory.bulk_edit_id == bulk_edit_id,
                            CardEditHistory.created_at
                            >= job.current_item_started_at,
                        ).order_by(CardEditHistory.id.asc()).all()
                        for history in histories:
                            decisions = history.merge_decisions or {}
                            is_photo = isinstance(decisions, dict) and isinstance(
                                decisions.get('photos'), dict
                            )
                            potentially_sent = history.wb_sync_status in {
                                'pending', 'submitted', 'uncertain',
                                'partial', 'success',
                            }
                            if is_photo and potentially_sent:
                                recovered_photo_sent = True
                                remaining_fields = [
                                    value for value in remaining_fields
                                    if value != 'photos'
                                ]
                                recovered_receipts.append(history)
                            elif not is_photo and potentially_sent:
                                remaining_fields = [
                                    value for value in remaining_fields
                                    if value == 'photos'
                                ]
                                recovered_receipts.append(history)
                                if history.wb_reconcile_due_at is not None:
                                    # Do not begin a new media side effect while
                                    # the content side of a crashed item is unknown.
                                    remaining_fields = [
                                        value for value in remaining_fields
                                        if value != 'photos'
                                    ]
                                    blocked_followup = True
                            elif history.wb_sync_status in {
                                'failed', 'conflict',
                            }:
                                remaining_fields = []
                                recovered_receipts.append(history)
                                blocked_followup = True
                                recovered_terminal_failure = history
                            elif history.wb_sync_status == 'skipped':
                                # A durable no-op receipt is still proof that
                                # this phase was fully planned before a crash.
                                # Keep it in recovery accounting; otherwise an
                                # empty remaining_fields list would be passed
                                # back into apply_enrichment and turn a safe
                                # no-op into a false failure.
                                recovered_receipts.append(history)
                                if is_photo:
                                    remaining_fields = [
                                        value for value in remaining_fields
                                        if value != 'photos'
                                    ]
                                else:
                                    remaining_fields = [
                                        value for value in remaining_fields
                                        if value == 'photos'
                                    ]

                    # If content may have reached WB but the photo phase had
                    # not started before a crash, keep the cursor on this item
                    # until read-only reconciliation reaches a terminal state.
                    # Advancing here would silently drop the selected photos;
                    # replaying content would risk a second full replacement.
                    if (
                        blocked_followup
                        and recovered_terminal_failure is None
                        and 'photos' in requested_item_fields
                        and not recovered_photo_sent
                    ):
                        break

                    product = Product.query.filter_by(
                        id=product_id,
                        seller_id=seller_id,
                    ).first()
                    row = {
                        'product_id': product_id,
                        'nm_id': getattr(product, 'nm_id', None),
                        'vendor_code': getattr(product, 'vendor_code', None),
                    }

                    if not product:
                        skipped += 1
                        row.update(status='skipped', reason='not_found')
                    else:
                        imp = self.find_supplier_data(product, seller_id)
                        if not imp:
                            skipped += 1
                            row.update(
                                status='skipped',
                                reason='no_supplier_data',
                            )
                        elif recovered_terminal_failure is not None:
                            failed += 1
                            row.update(
                                status='failed',
                                error=(
                                    recovered_terminal_failure.wb_error_message
                                    or 'Предыдущая отправка завершилась '
                                    'конфликтом; автоматический replay запрещён'
                                )[:500],
                            )
                        elif not remaining_fields and recovered_receipts:
                            recovered_applied = sorted({
                                field
                                for receipt in recovered_receipts
                                for field in (receipt.changed_fields or [])
                            })
                            if recovered_applied:
                                succeeded += 1
                                row.update(
                                    status='submitted',
                                    fields_applied=recovered_applied,
                                    reason=(
                                        'recovered_inflight_followup_deferred'
                                        if blocked_followup else
                                        'recovered_inflight'
                                    ),
                                )
                            else:
                                skipped += 1
                                row.update(
                                    status='skipped',
                                    reason='recovered_noop',
                                )
                        else:
                            try:
                                result = self.apply_enrichment(
                                    product, imp, remaining_fields, photo_strategy,
                                    seller, wb_client,
                                    bulk_edit_id=bulk_edit_id,
                                    is_bulk=True,
                                    validation_cache=validation_cache,
                                )
                                if result.get('deferred'):
                                    # Keep the cursor on this exact product.
                                    # The scheduler will retry the *plan* only
                                    # after the older provider receipt reaches
                                    # a terminal reconciliation state.
                                    db.session.expire_all()
                                    owned_job = db.session.get(
                                        EnrichmentJob, job_id,
                                    )
                                    if (
                                        owned_job is None
                                        or owned_job.claim_token != claim_token
                                    ):
                                        return
                                    owned_job.heartbeat_at = datetime.utcnow()
                                    owned_job.claim_expires_at = (
                                        datetime.utcnow() + timedelta(
                                            seconds=enrichment_lease_seconds()
                                        )
                                    )
                                    owned_job.last_error = str(
                                        result.get('error')
                                        or 'Ожидание предыдущей отправки WB'
                                    )[:500]
                                    db.session.commit()
                                    break
                                applied = list(result.get('fields_applied') or [])
                                decisions = result.get('merge_decisions') or {}
                                content_decisions = decisions.get('content') or {}
                                characteristic_decisions = (
                                    content_decisions.get('characteristics') or {}
                                )
                                field_decisions = (
                                    content_decisions.get('fields') or {}
                                )
                                photo_decisions = decisions.get('photos') or {}
                                merge_summary = {}
                                if field_decisions:
                                    merge_summary['fields'] = {
                                        field: {
                                            key: value.get(key)
                                            for key in (
                                                'decision',
                                                'existing_length',
                                                'candidate_length',
                                            )
                                            if value.get(key) is not None
                                        }
                                        for field, value in field_decisions.items()
                                        if isinstance(value, dict)
                                    }
                                if characteristic_decisions.get('counts'):
                                    merge_summary['characteristics'] = (
                                        characteristic_decisions['counts'])
                                if (
                                    photo_decisions.get('counts')
                                    or photo_decisions.get('live_count') is not None
                                ):
                                    merge_summary['photos'] = {
                                        'counts': photo_decisions.get('counts', {}),
                                        'live_count': photo_decisions.get('live_count'),
                                        'matching_status': photo_decisions.get(
                                            'matching_status'),
                                    }
                                if merge_summary:
                                    row['merge_summary'] = merge_summary
                                if applied and result.get(
                                    'reconciliation_pending'
                                ):
                                    succeeded += 1
                                    row.update(
                                        status='submitted',
                                        fields_applied=applied,
                                        reconciliation_pending=True,
                                    )
                                    if result.get('error'):
                                        row['warning'] = str(
                                            result['error']
                                        )[:500]
                                elif result.get('success') and applied:
                                    succeeded += 1
                                    row.update(
                                        status=(
                                            'submitted'
                                            if result.get('reconciliation_pending')
                                            else 'success'
                                        ),
                                        fields_applied=applied,
                                        reconciliation_pending=bool(
                                            result.get('reconciliation_pending')
                                        ),
                                    )
                                elif result.get('success'):
                                    # A no-op is not an updated card. This is
                                    # especially important for photo-only jobs:
                                    # only_if_empty/no source used to be counted
                                    # as a green success although WB was untouched.
                                    skipped += 1
                                    photos = result.get('photos') or {}
                                    row.update(
                                        status='skipped',
                                        reason=(
                                            photos.get('reason')
                                            or 'nothing_to_update'
                                        ),
                                    )
                                elif result.get('reconciliation_pending'):
                                    succeeded += 1
                                    row.update(
                                        status='uncertain',
                                        fields_applied=list(
                                            result.get('fields_applied')
                                            or result.get('fields_pending')
                                            or []
                                        ),
                                        reconciliation_pending=True,
                                        warning=str(
                                            result.get('error')
                                            or 'Исход отправки уточняется по live WB'
                                        )[:500],
                                    )
                                else:
                                    failed += 1
                                    row.update(
                                        status='failed',
                                        error=str(
                                            result.get('error')
                                            or 'WB не применил обновление'
                                        )[:500],
                                    )
                            except Exception:
                                db.session.rollback()
                                failed += 1
                                logger.exception(
                                    '[Enrich] Product %s failed in bulk job %s',
                                    product_id, job_id,
                                )
                                row.update(
                                    status='failed',
                                    error='Не удалось обработать карточку',
                                )

                    # A long photo download/provider call can outlive its DB
                    # lease. Never let a stale worker advance the cursor after
                    # another worker has taken ownership; its pre-send receipt
                    # remains available for restart-safe recovery.
                    db.session.expire_all()
                    owned_job = db.session.get(EnrichmentJob, job_id)
                    if (
                        owned_job is None
                        or owned_job.claim_token != claim_token
                    ):
                        return
                    job = owned_job

                    results.append(row)
                    handled += 1

                    # Re-query after a per-card rollback and commit every row.
                    # This makes progress durable and lets one bad card never
                    # stop or hide the remaining mass update.
                    job = db.session.get(EnrichmentJob, job_id)
                    if not job:
                        raise RuntimeError('job_disappeared')
                    job.processed = zero_index + 1
                    job.succeeded = succeeded
                    job.failed = failed
                    job.skipped = skipped
                    job.results = json.dumps(results, ensure_ascii=False)
                    job.current_product_id = None
                    job.current_item_started_at = None
                    job.heartbeat_at = datetime.utcnow()
                    job.claim_expires_at = datetime.utcnow() + timedelta(
                        seconds=enrichment_lease_seconds()
                    )
                    job.last_error = None
                    job.updated_at = datetime.utcnow()
                    db.session.commit()

                db.session.expire_all()
                job = db.session.get(EnrichmentJob, job_id)
                if job is None or job.claim_token != claim_token:
                    return
                completed = int(job.processed or 0) >= len(product_ids)
                job.status = 'done' if completed else 'pending'
                job.succeeded = succeeded
                job.failed = failed
                job.skipped = skipped
                job.results = json.dumps(results, ensure_ascii=False)
                job.claim_token = None
                job.claim_expires_at = None
                if completed:
                    job.last_error = None
                job.updated_at = datetime.utcnow()

                bulk_history = db.session.get(BulkEditHistory, bulk_edit_id)
                bulk_history.status = 'completed' if completed else 'in_progress'
                bulk_history.success_count = succeeded
                bulk_history.error_count = failed + skipped
                bulk_history.errors_details = [
                    item for item in results
                    if item.get('status') in {'failed', 'skipped'}
                ]
                bulk_history.wb_synced = False
                bulk_history.completed_at = datetime.utcnow() if completed else None
                db.session.commit()

                logger.info(
                    '[Enrich] Job %s %s: %s submitted, %s failed, '
                    '%s skipped',
                    job_id, job.status, succeeded, failed, skipped,
                )
            except Exception as exc:
                db.session.rollback()
                logger.exception('[Enrich] Bulk job %s failed', job_id)

                job = db.session.get(EnrichmentJob, job_id)
                fatal = str(exc) in {
                        'seller_not_found',
                        'invalid_durable_job_payload',
                        'invalid_durable_job_progress',
                }
                if job and (
                    job.claim_token == claim_token
                    or (fatal and job.claim_token is None)
                ):
                    job.status = 'failed' if fatal else 'pending'
                    job.last_error = (
                        str(exc)[:500] if fatal else
                        'Временная ошибка worker; задача будет продолжена'
                    )
                    job.claim_token = None
                    job.claim_expires_at = None
                    job.updated_at = datetime.utcnow()
                    if fatal and job.bulk_edit_id:
                        bulk_history = db.session.get(
                            BulkEditHistory, job.bulk_edit_id
                        )
                        if (
                            bulk_history
                            and bulk_history.seller_id == job.seller_id
                            and bulk_history.operation_type
                            == 'supplier_enrichment'
                        ):
                            bulk_history.status = 'failed'
                            bulk_history.completed_at = datetime.utcnow()
                db.session.commit()
            finally:
                close = getattr(wb_client, 'close', None)
                if callable(close):
                    close()


def process_due_enrichment_jobs(flask_app, *, max_jobs: int = 1) -> int:
    """Let the singleton scheduler resume a bounded number of durable jobs."""
    from models import db, EnrichmentJob

    with flask_app.app_context():
        now = datetime.utcnow()
        rows = EnrichmentJob.query.filter(
            EnrichmentJob.status.in_(('pending', 'running')),
            db.or_(
                EnrichmentJob.claim_token.is_(None),
                EnrichmentJob.claim_expires_at.is_(None),
                EnrichmentJob.claim_expires_at <= now,
            ),
        ).order_by(
            EnrichmentJob.created_at.asc(), EnrichmentJob.id.asc(),
        ).limit(max(1, min(3, int(max_jobs)))).all()
        targets = [(row.id, row.seller_id) for row in rows]
        db.session.remove()

    service = get_enrichment_service()
    for job_id, seller_id in targets:
        service._run_bulk_job(
            job_id,
            [],
            [],
            'smart_merge',
            seller_id,
            flask_app,
            item_limit=enrichment_items_per_tick(),
            time_budget_seconds=enrichment_tick_seconds(),
        )
    return len(targets)


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

    photos = EnrichmentService._safe_json_loads(product.photos_json, [])
    if not isinstance(photos, list):
        photos = []

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
        'photos': photos,
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
