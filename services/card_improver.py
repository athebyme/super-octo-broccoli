# -*- coding: utf-8 -*-
"""
Движок «Предложить→Подтвердить» для опубликованных карточек (Product).

apply_card_updates применяет ЯВНЫЕ значения полей через WB Content API
(update_card с merge_with_existing) + локальное обновление Product,
пишет CardEditHistory (snapshot до/после) и пересчитывает Quality Score
через recompute_and_persist. Сетевые вызовы инкапсулированы в wb_client —
функция тестируется фейковым клиентом без сети.

Структурный шаблон: EnrichmentService.apply_enrichment (services/supplier_enrichment.py:456).
Отличие: принимает ЯВНЫЕ значения полей (не ImportedProduct).
"""
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from models import db, CardEditHistory
from services.supplier_enrichment import _create_product_snapshot
from services.card_quality_scorer import recompute_and_persist

logger = logging.getLogger('card_improver')

# Поля, которые движок имеет право применять к карточке.
ALLOWED_FIELDS = {'title', 'brand', 'description', 'characteristics', 'dimensions', 'subject_id', 'photos'}


def _build_wb_updates(clean: Dict[str, Any]) -> Dict[str, Any]:
    """Из отфильтрованных явных значений строит payload для WB update_card.

    photos и subject_id не уходят в WB Content API через этот метод
    (photos — через отдельный photo-upload endpoint, subject_id — локально).
    """
    wb_updates: Dict[str, Any] = {}

    if clean.get('title'):
        wb_updates['title'] = str(clean['title'])[:60]

    if clean.get('brand'):
        wb_updates['brand'] = str(clean['brand'])

    if clean.get('description'):
        wb_updates['description'] = str(clean['description'])[:5000]

    if 'characteristics' in clean:
        chars = clean['characteristics']
        if isinstance(chars, list) and chars:
            wb_updates['characteristics'] = chars

    if 'dimensions' in clean:
        dims = clean['dimensions']
        if isinstance(dims, dict) and dims:
            wb_updates['dimensions'] = dims

    return wb_updates


def apply_card_updates(
    product,
    updates: Dict[str, Any],
    seller,
    wb_client,
    source: str = 'card-quality',
) -> Dict[str, Any]:
    """Применяет явные значения полей к опубликованной карточке Product.

    Алгоритм:
    1. Фильтрация updates по ALLOWED_FIELDS + непустые значения.
    2. Если фильтрованный dict пуст — ранний возврат без WB-вызова.
    3. snapshot_before → _build_wb_updates → wb_client.update_card (с merge).
    4. При успехе WB — локальное обновление Product-атрибутов.
    5. При ошибке WB — фиксируем error, поля НЕ применяем локально.
    6. subject_id применяется локально (без WB-вызова).
    7. recompute_and_persist → обновляет quality_score (без commit).
    8. Запись CardEditHistory (snapshot_before/after) + db.session.commit().

    Args:
        product:   Product ORM-объект (или Duck-type для тестов).
        updates:   Словарь с явными значениями полей для применения.
        seller:    Seller ORM-объект (нужен seller.id).
        wb_client: WildberriesAPIClient (или FakeWBClient в тестах).
        source:    Строковая метка источника для истории и логов.

    Returns:
        {
            'success':        bool   — True если хотя бы одно поле применено и нет ошибки WB,
            'fields_applied': list   — список применённых полей,
            'old_quality':    float  — quality_score до изменений,
            'new_quality':    float  — quality_score после пересчёта,
            'wb_sync':        bool   — True если WB API вернул успех,
            'error':          str|None — сообщение об ошибке WB или None,
        }
    """
    # Запоминаем старый Quality Score до любых изменений
    old_quality: Optional[float] = getattr(product, 'quality_score', None)

    # Фильтруем по белому списку; пропускаем None, '', [], {}
    clean: Dict[str, Any] = {
        k: v for k, v in (updates or {}).items()
        if k in ALLOWED_FIELDS and v not in (None, '', [], {})
    }

    # Нет допустимых полей — ранний выход без WB-вызова и без истории
    if not clean:
        return {
            'success': False,
            'fields_applied': [],
            'old_quality': old_quality,
            'new_quality': old_quality,
            'wb_sync': False,
            'error': 'Нет допустимых полей для применения',
        }

    # Снимок состояния ДО изменений
    snapshot_before = _create_product_snapshot(product)

    wb_updates = _build_wb_updates(clean)
    fields_applied: List[str] = []
    wb_sync_success = False
    wb_error: Optional[str] = None

    # --- Синхронизация с WB Content API ---
    if wb_updates:
        try:
            wb_client.update_card(
                product.nm_id,
                wb_updates,
                merge_with_existing=True,
                seller_id=seller.id,
            )
            wb_sync_success = True
            logger.info(
                f"[Improve/{source}] WB updated nmID={product.nm_id}: {list(wb_updates.keys())}"
            )

            # Локальное обновление Product-атрибутов — только при успехе WB
            if 'title' in wb_updates:
                product.title = wb_updates['title']
                fields_applied.append('title')

            if 'brand' in wb_updates:
                product.brand = wb_updates['brand']
                fields_applied.append('brand')

            if 'description' in wb_updates:
                product.description = wb_updates['description']
                fields_applied.append('description')

            if 'characteristics' in wb_updates:
                product.characteristics_json = json.dumps(
                    wb_updates['characteristics'], ensure_ascii=False
                )
                fields_applied.append('characteristics')

            if 'dimensions' in wb_updates:
                product.dimensions_json = json.dumps(
                    wb_updates['dimensions'], ensure_ascii=False
                )
                fields_applied.append('dimensions')

        except Exception as e:
            wb_error = str(e)
            logger.error(f"[Improve/{source}] WB API error nmID={product.nm_id}: {e}")

    # subject_id применяем локально (без WB-вызова в этом движке)
    if 'subject_id' in clean and clean['subject_id']:
        product.subject_id = clean['subject_id']
        fields_applied.append('subject_id')

    # photos хранятся локально; синхронизация медиа с WB — отдельный flow, не через update_card
    if 'photos' in clean:
        product.photos_json = json.dumps(clean['photos'], ensure_ascii=False)
        fields_applied.append('photos')

    # Обновляем метку времени карточки
    if hasattr(product, 'updated_at'):
        product.updated_at = datetime.utcnow()

    # --- Пересчёт Quality Score (Фаза 2) ---
    # recompute_and_persist обновляет product.quality_score / quality_breakdown_json
    # и добавляет CardRatingHistory, но НЕ коммитит — коммит ниже.
    new_quality: Optional[float] = old_quality
    if fields_applied:
        cq = recompute_and_persist(product, capture_history=True)
        new_quality = cq['score']

    # Снимок состояния ПОСЛЕ изменений
    snapshot_after = _create_product_snapshot(product)

    # --- Запись истории ---
    history = CardEditHistory(
        product_id=product.id,
        seller_id=seller.id,
        bulk_edit_id=None,
        action='update',
        changed_fields=fields_applied,
        snapshot_before=snapshot_before,
        snapshot_after=snapshot_after,
        wb_synced=wb_sync_success,
        wb_sync_status='success' if wb_sync_success else ('failed' if wb_error else 'skipped'),
        wb_error_message=wb_error,
        user_comment=f'Улучшение карточки ({source})',
    )
    db.session.add(history)

    # Единственный commit для истории + product + rating-snapshot (recompute_and_persist не коммитит)
    db.session.commit()

    return {
        'success': bool(fields_applied) and wb_error is None,
        'fields_applied': fields_applied,
        'old_quality': old_quality,
        'new_quality': new_quality,
        'wb_sync': wb_sync_success,
        'error': wb_error,
    }
