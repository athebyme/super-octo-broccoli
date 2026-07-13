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
from services.card_quality_scorer import build_seller_scoring_context, recompute_and_persist

logger = logging.getLogger('card_improver')

# Поля, которые движок имеет право применять к карточке.
ALLOWED_FIELDS = {'title', 'brand', 'description', 'characteristics', 'dimensions', 'subject_id', 'photos'}

# Маппинг генеративных агентов (propose-mode) на поля proposal.
# agent_name → [(result_field, dimension_name), ...]
_GENERATIVE_FIELD_MAP: Dict[str, List] = {
    'seo-writer': [('title', 'title'), ('description', 'description')],
    'brand-resolver': [('brand', 'brand')],
    'category-mapper': [('subject_id', 'category')],
    'characteristics-filler': [('characteristics', 'characteristics')],
}


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


def _clean_updates(updates: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Фильтр по белому списку полей; пропускаем None, '', [], {}."""
    return {
        k: v for k, v in (updates or {}).items()
        if k in ALLOWED_FIELDS and v not in (None, '', [], {})
    }


def _apply_local_wb_fields(product, wb_updates: Dict[str, Any]) -> List[str]:
    """Перенести успешно ушедшие в WB текстовые поля на локальный Product."""
    applied: List[str] = []
    if 'title' in wb_updates:
        product.title = wb_updates['title']
        applied.append('title')
    if 'brand' in wb_updates:
        product.brand = wb_updates['brand']
        applied.append('brand')
    if 'description' in wb_updates:
        product.description = wb_updates['description']
        applied.append('description')
    if 'characteristics' in wb_updates:
        from services.marketplace_validator import merge_wb_characteristics
        try:
            existing = json.loads(product.characteristics_json or '[]')
        except (json.JSONDecodeError, TypeError):
            existing = []
        product.characteristics_json = json.dumps(
            merge_wb_characteristics(
                existing,
                wb_updates['characteristics'],
                subject_id=getattr(product, 'subject_id', None),
            ),
            ensure_ascii=False,
        )
        applied.append('characteristics')
    if 'dimensions' in wb_updates:
        product.dimensions_json = json.dumps(
            wb_updates['dimensions'], ensure_ascii=False)
        applied.append('dimensions')
    return applied


def apply_card_updates_bulk(
    items: List,
    seller,
    wb_client,
    source: str = 'bulk',
) -> Dict[int, Dict[str, Any]]:
    """Массовое применение обновлений: тексты — ОДНИМ батчем cards/update
    (лимит WB 10 запросов/мин, до 3000 карточек в запросе), фото — по
    карточке через media/save (общий пул). История и Quality Score — по
    карточке, как в apply_card_updates.

    Args:
        items: [(product, updates)] — updates как в apply_card_updates.

    Returns:
        {product.id: результат в формате apply_card_updates}
    """
    per_product = []
    nm_updates: Dict[int, Dict[str, Any]] = {}
    characteristic_validation_cache = {}
    for product, updates in items:
        clean = _clean_updates(updates)
        validation_error = None
        if clean.get('characteristics'):
            from services.marketplace_validator import (
                WBCharacteristicValidationError,
                build_wb_characteristic_patch,
            )
            try:
                clean = dict(clean)
                clean['characteristics'] = build_wb_characteristic_patch(
                    getattr(product, 'subject_id', None),
                    clean['characteristics'],
                    validation_cache=characteristic_validation_cache,
                )
            except WBCharacteristicValidationError as exc:
                validation_error = str(exc)
        wb_updates = _build_wb_updates(clean)
        per_product.append((product, clean, wb_updates, validation_error))
        if not validation_error and wb_updates and getattr(product, 'nm_id', None):
            nm_updates[int(product.nm_id)] = wb_updates

    batch = {'sent': [], 'missing': [], 'invalid': {}, 'failed': {}, 'requests': 0}
    batch_error: Optional[str] = None
    if nm_updates:
        try:
            batch = wb_client.update_cards_merged(nm_updates, seller_id=seller.id)
        except Exception as e:
            batch_error = str(e)
            logger.error(f"[Improve/{source}] batch cards/update failed: {e}")

    sent = {int(x) for x in (batch.get('sent') or [])}
    invalid = {int(k): v for k, v in (batch.get('invalid') or {}).items()}
    missing = {int(x) for x in (batch.get('missing') or [])}
    failed_send = {int(k): v for k, v in (batch.get('failed') or {}).items()}

    # Контекст скоринга (дубликаты описаний + конфиги характеристик по каталогу
    # продавца) строится ОДИН раз на весь bulk-вызов, а не на каждый товар —
    # без этого recompute_and_persist делает полный скан каталога на каждой
    # карточке (N+1 DB scans).
    shared_context = build_seller_scoring_context(seller.id) if per_product else None

    results: Dict[int, Dict[str, Any]] = {}
    for product, clean, wb_updates, validation_error in per_product:
        old_quality: Optional[float] = getattr(product, 'quality_score', None)
        if validation_error:
            results[product.id] = {
                'success': False, 'fields_applied': [],
                'old_quality': old_quality, 'new_quality': old_quality,
                'wb_sync': False, 'error': validation_error,
            }
            continue
        if not clean:
            results[product.id] = {
                'success': False, 'fields_applied': [],
                'old_quality': old_quality, 'new_quality': old_quality,
                'wb_sync': False, 'error': 'Нет допустимых полей для применения',
            }
            continue

        snapshot_before = _create_product_snapshot(product)
        nm = int(product.nm_id) if getattr(product, 'nm_id', None) else None

        fields_applied: List[str] = []
        wb_sync_success = False
        wb_error: Optional[str] = None

        if wb_updates:
            if nm is not None and nm in sent:
                wb_sync_success = True
                fields_applied.extend(_apply_local_wb_fields(product, wb_updates))
            elif nm in invalid:
                wb_error = invalid[nm]
            elif nm in failed_send:
                wb_error = failed_send[nm]
            elif nm in missing:
                wb_error = 'Не удалось получить карточку из WB (не найдена)'
            elif batch_error:
                wb_error = batch_error
            elif nm is None:
                wb_error = 'У карточки нет nm_id'
            else:
                wb_error = 'Карточка не была отправлена в WB'

        if 'subject_id' in clean and clean['subject_id']:
            product.subject_id = clean['subject_id']
            fields_applied.append('subject_id')

        if 'photos' in clean and isinstance(clean['photos'], list) and clean['photos']:
            try:
                wb_client.upload_photos_by_url(
                    product.nm_id, clean['photos'], seller_id=seller.id)
                product.photos_json = json.dumps(clean['photos'], ensure_ascii=False)
                fields_applied.append('photos')
                wb_sync_success = True
            except Exception as e:
                wb_error = str(e)
                logger.error(
                    f"[Improve/{source}] WB media/save error nmID={product.nm_id}: {e}")

        if hasattr(product, 'updated_at'):
            product.updated_at = datetime.utcnow()

        new_quality: Optional[float] = old_quality
        if fields_applied:
            cq = recompute_and_persist(product, capture_history=True, context=shared_context)
            new_quality = cq['score']

        snapshot_after = _create_product_snapshot(product)
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
        db.session.commit()

        results[product.id] = {
            'success': bool(fields_applied) and wb_error is None,
            'fields_applied': fields_applied,
            'old_quality': old_quality,
            'new_quality': new_quality,
            'wb_sync': wb_sync_success,
            'error': wb_error,
        }

    return results


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
    clean: Dict[str, Any] = _clean_updates(updates)

    if clean.get('characteristics'):
        from services.marketplace_validator import (
            WBCharacteristicValidationError,
            build_wb_characteristic_patch,
        )
        try:
            clean = dict(clean)
            clean['characteristics'] = build_wb_characteristic_patch(
                getattr(product, 'subject_id', None),
                clean['characteristics'],
            )
        except WBCharacteristicValidationError as exc:
            return {
                'success': False,
                'fields_applied': [],
                'old_quality': old_quality,
                'new_quality': old_quality,
                'wb_sync': False,
                'error': str(exc),
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
            fields_applied.extend(_apply_local_wb_fields(product, wb_updates))

        except Exception as e:
            wb_error = str(e)
            logger.error(f"[Improve/{source}] WB API error nmID={product.nm_id}: {e}")

    # subject_id применяем локально (без WB-вызова в этом движке)
    if 'subject_id' in clean and clean['subject_id']:
        product.subject_id = clean['subject_id']
        fields_applied.append('subject_id')

    # photos — отправляем упорядоченный список URL в WB через media/save (Content API v3).
    # Вызов upload_photos_by_url заменяет набор фото в карточке; порядок сохраняется.
    # При ошибке — фиксируем wb_error / статус 'failed' (аналогично текстовым полям).
    if 'photos' in clean and isinstance(clean['photos'], list) and clean['photos']:
        try:
            wb_client.upload_photos_by_url(
                product.nm_id,
                clean['photos'],
                seller_id=seller.id,
            )
            wb_sync_success = True
            product.photos_json = json.dumps(clean['photos'], ensure_ascii=False)
            fields_applied.append('photos')
            logger.info(
                f"[Improve/{source}] WB media/save nmID={product.nm_id}: {len(clean['photos'])} фото"
            )
        except Exception as e:
            wb_error = str(e)
            logger.error(
                f"[Improve/{source}] WB media/save error nmID={product.nm_id}: {e}"
            )

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


def collect_weak_dimensions(detail: Dict[str, Any]) -> List[str]:
    """Имена измерений со статусом warning/error, отсортированные по impact = weight*(100-score) убыванию."""
    dims = (detail or {}).get('dimensions') or {}
    weak = []
    for name, d in dims.items():
        if d.get('status') in ('warning', 'error'):
            impact = d.get('weight', 0) * (100 - d.get('score', 0))
            weak.append((impact, name))
    weak.sort(key=lambda t: (-t[0], t[1]))
    return [name for _, name in weak]


def build_proposal_from_tasks(product, task_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Маппит результаты завершённых задач агентов в форму proposal.

    task_results: [{'agent': '<name>', 'result': <dict из AgentTask.get_result()>}]
    Returns: {'<field>': {'current','proposed','dimension','source'}}

    photo-optimizer/recommended_order → предложение «переупорядочить фото».
    Генеративные значения seo/characteristics/brand/category добавляются
    в Задаче 3.7 (propose-mode), здесь — расширяемая ветка по agent name.
    """
    proposal: Dict[str, Any] = {}

    try:
        current_photos = json.loads(getattr(product, 'photos_json', None) or '[]')
    except (json.JSONDecodeError, TypeError):
        current_photos = []
    if not isinstance(current_photos, list):
        current_photos = []

    for entry in task_results or []:
        agent = entry.get('agent')
        result = entry.get('result') or {}

        if agent == 'photo-optimizer':
            order = result.get('recommended_order')
            if isinstance(order, list) and current_photos:
                # Берём только валидные индексы в диапазоне, без дублей, в указанном порядке
                seen = set()
                valid = []
                for idx in order:
                    if isinstance(idx, int) and 0 <= idx < len(current_photos) and idx not in seen:
                        seen.add(idx)
                        valid.append(idx)
                # Достраиваем хвостом пропущенные индексы, чтобы не потерять фото
                for idx in range(len(current_photos)):
                    if idx not in seen:
                        valid.append(idx)
                reordered = [current_photos[i] for i in valid]
                if reordered != current_photos:
                    proposal['photos'] = {
                        'current': current_photos,
                        'proposed': reordered,
                        'dimension': 'photos',
                        'source': 'photo-optimizer',
                    }
        # card-doctor — диагностический (рекомендации-текст), не формирует proposal-поля.

        # Генеративные агенты (Задача 3.7): seo-writer, brand-resolver,
        # category-mapper, characteristics-filler — propose-mode.
        # Порог confidence >= 0.7; совпадения с текущим значением пропускаются.
        if agent in _GENERATIVE_FIELD_MAP:
            conf = result.get('confidence', 1.0)
            if isinstance(conf, (int, float)) and conf >= 0.7:
                for field, dim in _GENERATIVE_FIELD_MAP[agent]:
                    proposed = result.get(field)
                    # characteristics — не сравниваем с текущим (нет прямого атрибута)
                    current = getattr(product, field, None) if field != 'characteristics' else None
                    if proposed in (None, '', [], {}):
                        continue
                    if field != 'characteristics' and proposed == current:
                        continue
                    proposal[field] = {
                        'current': current,
                        'proposed': proposed,
                        'dimension': dim,
                        'source': agent,
                    }

    return proposal
