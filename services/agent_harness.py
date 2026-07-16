# -*- coding: utf-8 -*-
"""Durable user-facing harness for the unified seller assistant.

The existing specialist workers remain execution skills. This service owns the
stable product contract: conversations, deterministic planning for common
workflows, approval gates, model policy, task projection, and recursive undo.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional
from urllib.parse import urlsplit

from agents.catalog.orchestrator import PIPELINES
from agents.content_contract import (
    content_fields_label,
    extract_explicit_content_fields,
    normalize_content_fields,
)
from models import (
    db,
    AgentChangeSnapshot,
    CardEditHistory,
    AgentConversation,
    AgentMessage,
    AgentReviewProposal,
    AgentTask,
    AgentTaskStep,
    AutoImportSettings,
    ImportedProduct,
    Marketplace,
    MarketplaceListing,
    Product,
    PricingSettings,
    SellerMarketplaceAccount,
    ServiceAgent,
)
from services import agent_service
from agents.image_chat_contract import (
    CHAT_IMAGE_MODEL,
    chat_image_cost_label,
)


MAX_MESSAGE_LENGTH = 4000
MAX_PRODUCT_IDS = 500
MAX_MARKETPLACE_LISTING_IDS = 200
SEMANTIC_CONTEXT_MAX_CHARS = 6000
SEMANTIC_CONTEXT_MAX_MESSAGES = 12
HARNESS_PLAN_VERSION = 6
TERMINAL_TASK_STATUSES = {'completed', 'failed', 'cancelled'}
ACTIVE_TASK_STATUSES = {'queued', 'running'}
PAGE_CONTEXT_ENTITY_KEYS = {
    'product_id', 'seller_id', 'supplier_id', 'item_id', 'nm_id',
    'subject_id', 'brand_id', 'marketplace_id', 'factory_id',
    'account_id', 'category_id', 'task_id', 'listing_id',
    'marketplace_code',
}

DIRECT_HELP_PATTERNS = (
    'что ты умеешь', 'что умеешь', 'твои возможности', 'как ты работаешь',
    'помощь', 'помоги', 'help',
)

SKILLS = {
    'brand': {
        'keywords': ('бренд', 'brand'),
        'step': {'agent': 'brand-resolver', 'task_type': 'resolve_batch', 'label': 'Нормализация брендов'},
        'risk': 'write',
    },
    'sizes': {
        'keywords': ('размер', 'габарит', 'вес', 'size'),
        'step': {'agent': 'size-normalizer', 'task_type': 'normalize_batch', 'label': 'Нормализация размеров'},
        'risk': 'write',
    },
    'characteristics': {
        'keywords': ('характерист', 'атрибут', 'состав'),
        'step': {'agent': 'characteristics-filler', 'task_type': 'fill_batch', 'label': 'Заполнение характеристик'},
        'risk': 'write',
    },
    'price': {
        'keywords': ('цен', 'марж', 'экономик', 'price'),
        'step': {'agent': 'price-optimizer', 'task_type': 'optimize_prices', 'label': 'Расчёт и оптимизация цен'},
        'risk': 'write',
    },
    'reviews': {
        'keywords': ('отзыв', 'рейтинг', 'review'),
        'step': {'agent': 'review-analyst', 'task_type': 'analyze_reviews', 'label': 'Анализ отзывов'},
        'risk': 'read',
    },
    'photos': {
        'keywords': ('фото', 'изображен', 'photo'),
        'step': {'agent': 'photo-optimizer', 'task_type': 'quality_check', 'label': 'Проверка фотографий'},
        'risk': 'read',
    },
    'system': {
        'keywords': (
            'дефолт', 'настройк товар', 'api ключ', 'апи ключ',
            'логи api', 'логи апи', 'ошибк api', 'ошибк апи',
            'стоп-слов', 'подключен api', 'подключен апи',
        ),
        'step': {'agent': 'system-context', 'task_type': 'inspect_system', 'label': 'Проверка настроек и журналов'},
        'risk': 'read',
    },
}


@dataclass(frozen=True)
class HarnessPlan:
    title: str
    summary: str
    steps: list[dict]
    execution_type: str
    pipeline: Optional[str]
    risk: str
    confidence: float
    scope_label: Optional[str] = None

    @property
    def requires_approval(self) -> bool:
        return self.risk != 'read'

    def to_dict(self, product_ids: list[int], model_policy: dict) -> dict:
        return {
            'plan_id': str(uuid.uuid4()),
            'plan_version': HARNESS_PLAN_VERSION,
            'title': self.title,
            'summary': self.summary,
            'steps': self.steps,
            'execution_type': self.execution_type,
            'pipeline': self.pipeline,
            'risk': self.risk,
            'confidence': self.confidence,
            'requires_approval': self.requires_approval,
            'status': 'pending_approval' if self.requires_approval else 'ready',
            'product_ids': product_ids,
            'scope_label': self.scope_label or (
                f'{len(product_ids)} выбранных товаров' if product_ids else 'Весь каталог'
            ),
            'model_policy': model_policy,
        }


def _contains_any(text: str, needles: Iterable[str]) -> bool:
    return any(needle in text for needle in needles)


def _normalize_product_ids(value) -> list[int]:
    if value is None:
        return []
    if isinstance(value, str):
        value = re.findall(r'\d+', value)
    if not isinstance(value, (list, tuple, set)):
        raise ValueError('product_ids must be a list')

    result = []
    seen = set()
    for raw in value:
        try:
            product_id = int(raw)
        except (TypeError, ValueError):
            continue
        if product_id <= 0 or product_id in seen:
            continue
        result.append(product_id)
        seen.add(product_id)
        if len(result) >= MAX_PRODUCT_IDS:
            break
    return result


def _message_numeric_references(text: str) -> tuple[list[str], set[str]]:
    """Extract possible numeric card references without classifying intent.

    Long numeric tokens are cheap exact-lookup candidates. Tokens immediately
    following an explicit article/nmID/SKU label are also marked as explicit,
    so an unknown reference can produce a clarification instead of silently
    widening a write to the whole catalog.
    """
    value = str(text or '')
    positioned: list[tuple[int, str]] = []
    explicit: set[str] = set()
    url_spans = [
        (match.start(), match.end())
        for match in re.finditer(r'https?://[^\s<>"\']+', value, flags=re.IGNORECASE)
    ]
    for match in re.finditer(r'(?<!\w)\d{6,18}(?!\w)', value):
        if any(start <= match.start() < end for start, end in url_spans):
            continue
        positioned.append((match.start(), match.group(0)))

    cue_pattern = re.compile(
        r'\b(?:артик\w*|арт[ие]к\w*|nm\s*id|sku)\b',
        flags=re.IGNORECASE,
    )
    for cue in cue_pattern.finditer(value):
        tail = value[cue.end():cue.end() + 260]
        refs = re.match(
            r'\s*[:№#-]?\s*('
            r'\d{5,18}(?:\s*(?:[,;/]|\bи\b)\s*\d{5,18})*'
            r')',
            tail,
            flags=re.IGNORECASE,
        )
        if not refs:
            continue
        for number in re.finditer(r'\d{5,18}', refs.group(1)):
            token = number.group(0)
            explicit.add(token)
            positioned.append((cue.end() + refs.start(1) + number.start(), token))

    ordered = []
    seen = set()
    for _, token in sorted(positioned, key=lambda item: item[0]):
        if token in seen:
            continue
        seen.add(token)
        ordered.append(token)
        if len(ordered) >= 20:
            break
    return ordered, explicit


def _resolve_message_product_scope(seller_id: int, text: str) -> Optional[dict]:
    """Resolve exact seller-owned nmID/article references to one typed scope.

    This is grounding, not an intent parser: unmatched conversational text is
    still handled by the semantic planner. Product wins over its imported
    source when the same WB nmID exists in both catalogs.
    """
    references, explicit = _message_numeric_references(text)
    if not references:
        return None
    numeric_values = [int(value) for value in references]
    products = Product.query.filter(
        Product.seller_id == seller_id,
        Product.nm_id.in_(numeric_values),
    ).all()
    products_by_reference = {str(int(product.nm_id)): product for product in products}

    imported = ImportedProduct.query.filter(
        ImportedProduct.seller_id == seller_id,
        db.or_(
            ImportedProduct.wb_nm_id.in_(numeric_values),
            ImportedProduct.external_id.in_(references),
            ImportedProduct.external_vendor_code.in_(references),
        ),
    ).all()
    imported_by_reference: dict[str, list[ImportedProduct]] = {}
    for item in imported:
        aliases = {
            str(item.wb_nm_id) if item.wb_nm_id is not None else '',
            str(item.external_id or '').strip(),
            str(item.external_vendor_code or '').strip(),
        }
        for reference in aliases & set(references):
            imported_by_reference.setdefault(reference, []).append(item)

    resolved = []
    unresolved_explicit = []
    ambiguous = []
    for reference in references:
        product = products_by_reference.get(str(int(reference)))
        if product is not None:
            resolved.append({
                'reference': reference,
                'kind': 'product',
                'id': int(product.id),
                'matched_by': 'nm_id',
            })
            continue
        imported_matches = {
            int(item.id): item for item in imported_by_reference.get(reference, [])
        }
        if len(imported_matches) == 1:
            item = next(iter(imported_matches.values()))
            resolved.append({
                'reference': reference,
                'kind': 'imported_product',
                'id': int(item.id),
                'matched_by': (
                    'wb_nm_id' if str(item.wb_nm_id or '') == reference
                    else 'supplier_article'
                ),
            })
        elif len(imported_matches) > 1:
            ambiguous.append(reference)
        elif reference in explicit:
            unresolved_explicit.append(reference)

    kinds = {item['kind'] for item in resolved}
    if ambiguous or len(kinds) > 1:
        return {
            'issue': 'ambiguous',
            'references': references,
            'ambiguous': ambiguous or [item['reference'] for item in resolved],
        }
    if unresolved_explicit:
        return {
            'issue': 'not_found',
            'references': references,
            'unresolved': unresolved_explicit,
        }
    if not resolved:
        return None

    ids = []
    seen_ids = set()
    for item in resolved:
        if item['id'] not in seen_ids:
            seen_ids.add(item['id'])
            ids.append(item['id'])
    return {
        'kind': next(iter(kinds)),
        'ids': ids,
        'references': resolved,
    }


def _strict_positive_integer(value, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f'{field} must be a positive integer')
    return value


def _ground_marketplace_entity_scope(value, seller_id: int) -> dict:
    """Turn an untrusted browser listing scope into a seller-owned DB scope.

    A marketplace listing ID is never interchangeable with Product or
    ImportedProduct IDs.  The account and marketplace identity are therefore
    mandatory and every selected listing is resolved as one exact set before
    it can be persisted in a chat task.
    """
    if not isinstance(value, dict):
        raise ValueError('entity_scope must be an object')
    allowed_keys = {
        'kind', 'ids', 'listing_ids', 'marketplace_code', 'account_id',
        'scope_mode',
    }
    unknown_keys = sorted(set(value) - allowed_keys)
    if unknown_keys:
        raise ValueError(
            'entity_scope contains unsupported fields: ' + ', '.join(unknown_keys)
        )
    if value.get('kind') != 'marketplace_listing':
        raise ValueError('entity_scope.kind must be marketplace_listing')

    marketplace_code = str(value.get('marketplace_code') or '').strip().lower()
    if not re.fullmatch(r'[a-z][a-z0-9_-]{1,49}', marketplace_code):
        raise ValueError('entity_scope.marketplace_code is invalid')
    account_id = _strict_positive_integer(
        value.get('account_id'), 'entity_scope.account_id',
    )
    scope_mode = str(value.get('scope_mode') or 'selected').strip().lower()
    if scope_mode not in {'selected', 'global'}:
        raise ValueError('entity_scope.scope_mode must be selected or global')

    raw_ids = value.get('ids')
    if raw_ids is not None and value.get('listing_ids') is not None:
        raise ValueError('Use only entity_scope.ids for marketplace listings')
    if raw_ids is None:
        raw_ids = value.get('listing_ids')
    if not isinstance(raw_ids, list):
        raise ValueError('entity_scope.ids must be an array')
    if len(raw_ids) > MAX_MARKETPLACE_LISTING_IDS:
        raise ValueError(
            f'Maximum {MAX_MARKETPLACE_LISTING_IDS} marketplace listing IDs per request'
        )
    listing_ids = []
    seen = set()
    for index, raw in enumerate(raw_ids):
        listing_id = _strict_positive_integer(
            raw, f'entity_scope.ids[{index}]',
        )
        if listing_id in seen:
            raise ValueError(f'Duplicate marketplace listing ID: {listing_id}')
        listing_ids.append(listing_id)
        seen.add(listing_id)
    if scope_mode == 'selected' and not listing_ids:
        raise ValueError('Selected marketplace scope requires at least one listing')
    if scope_mode == 'global' and listing_ids:
        raise ValueError('Global marketplace scope cannot contain listing IDs')

    account = SellerMarketplaceAccount.query.join(
        Marketplace,
        SellerMarketplaceAccount.marketplace_id == Marketplace.id,
    ).filter(
        SellerMarketplaceAccount.id == account_id,
        SellerMarketplaceAccount.seller_id == seller_id,
        SellerMarketplaceAccount.is_active.is_(True),
        Marketplace.code == marketplace_code,
        Marketplace.is_active.is_(True),
    ).first()
    if not account:
        raise ValueError('Marketplace account is unavailable in this seller scope')

    if listing_ids:
        listings = MarketplaceListing.query.filter(
            MarketplaceListing.seller_id == seller_id,
            MarketplaceListing.marketplace_id == account.marketplace_id,
            MarketplaceListing.account_id == account.id,
            MarketplaceListing.id.in_(listing_ids),
        ).all()
        if len(listings) != len(listing_ids):
            raise ValueError(
                'Some marketplace listings are unavailable in this seller/account scope'
            )

    return {
        'kind': 'marketplace_listing',
        'ids': listing_ids,
        'marketplace_code': marketplace_code,
        'account_id': account.id,
        'scope_mode': scope_mode,
    }


def _scope_identity(scope: dict) -> tuple:
    scope = scope if isinstance(scope, dict) else {}
    raw_ids = scope.get('ids') if isinstance(scope.get('ids'), list) else []
    ids = tuple(sorted(
        value for value in raw_ids
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    ))
    return (
        str(scope.get('kind') or ''),
        ids,
        str(scope.get('marketplace_code') or ''),
        scope.get('account_id'),
        str(scope.get('scope_mode') or ('selected' if ids else 'global')),
    )

def _normalize_page_context(value) -> dict:
    """Keep only small, non-secret page metadata supplied by the UI."""
    if not isinstance(value, dict):
        return {}

    def clean_text(raw, limit):
        return re.sub(r'\s+', ' ', str(raw or '')).strip()[:limit]

    context = {}
    for key, limit in (('title', 200), ('url', 500), ('route', 120)):
        cleaned = clean_text(value.get(key), limit)
        if cleaned:
            context[key] = cleaned

    raw_entities = value.get('entities')
    entities = {}
    total = 0
    if isinstance(raw_entities, dict):
        for key in sorted(PAGE_CONTEXT_ENTITY_KEYS):
            raw_values = raw_entities.get(key)
            if not isinstance(raw_values, (list, tuple, set)):
                continue
            cleaned_values = []
            seen = set()
            for raw in raw_values:
                cleaned = clean_text(raw, 64)
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', cleaned or ''):
                    continue
                if cleaned in seen:
                    continue
                cleaned_values.append(cleaned)
                seen.add(cleaned)
                total += 1
                if len(cleaned_values) >= 12 or total >= 24:
                    break
            if cleaned_values:
                entities[key] = cleaned_values
            if total >= 24:
                break
    if entities:
        context['entities'] = entities
    return context


def get_model_policy(seller_id: int) -> dict:
    """Return a non-secret model policy safe to persist in task metadata."""
    settings = AutoImportSettings.query.filter_by(seller_id=seller_id).first()
    provider = (settings.ai_provider if settings else None) or 'deepseek'
    primary_model = (settings.ai_model if settings else None) or 'deepseek-v4-pro'
    single_model = settings.agent_single_model if settings else False
    execution_model = primary_model if single_model else 'deepseek-v4-flash'
    return {
        'single_model': bool(single_model),
        'provider': provider,
        'primary_model': primary_model,
        'fast_model': execution_model,
        # Compatibility field for already persisted plan metadata.
        'write_model': execution_model,
    }


def direct_response(text: str) -> Optional[str]:
    normalized = text.strip().lower()
    standalone = re.sub(r'[?!.]+$', '', normalized).strip()
    if standalone in {'привет', 'здравствуй', 'hello', 'hi'}:
        return (
            'Здравствуйте. Опишите результат, который нужен: я соберу контекст, '
            'покажу план и попрошу подтверждение перед изменением данных.'
        )
    if standalone in DIRECT_HELP_PATTERNS:
        return (
            'Я работаю с карточками как единый помощник: готовлю товары к WB, '
            'улучшаю SEO и характеристики, проверяю категории, бренды, размеры, '
            'цены, фотографии, модерацию и отзывы. Перед изменениями показываю '
            'план. Для одной выбранной карточки Gemini Flash составляет только '
            f'промпт сцены, а {CHAT_IMAGE_MODEL} через OpenRouter генерирует '
            'изображение в Фотостудии; результат всегда требует review.'
        )
    return None


def _conversation_usage_response(conversation: AgentConversation, text: str) -> Optional[str]:
    normalized = text.lower()
    if not _contains_any(normalized, (
        'сколько токен', 'расход токен', 'api запрос', 'апи запрос',
        'сколько стоил запуск', 'стоимость запуск', 'расход последн',
    )):
        return None
    run = AgentMessage.query.join(
        AgentTask, AgentMessage.task_id == AgentTask.id,
    ).filter(
        AgentMessage.conversation_id == conversation.id,
        AgentMessage.kind == 'run',
        AgentTask.status.in_(TERMINAL_TASK_STATUSES),
    ).order_by(AgentMessage.created_at.desc()).first()
    if not run or not run.task:
        return 'В этом диалоге ещё нет завершённого запуска с метриками.'
    usage = (run.task.get_result().get('_usage') or {})
    total = int(usage.get('total_tokens') or 0)
    requests = int(usage.get('api_requests') or 0)
    hit_rate = float(usage.get('cache_hit_rate') or 0) * 100
    cost = float(usage.get('estimated_cost_usd') or 0)
    return (
        f'Последний запуск: {total:,} токенов, {requests} LLM API-запросов, '
        f'cache hit {hit_rate:.1f}%, оценочная стоимость ${cost:.6f}.'
    ).replace(',', ' ')


def _needs_semantic_planner(text: str) -> bool:
    """Route every unresolved user goal through the bounded semantic fallback."""
    return bool((text or '').strip())


def _compact_dialog_context(conversation: AgentConversation,
                            current_message_id: str = None) -> list[dict]:
    """Return recent language and run outcomes; entity scope stays task-owned."""
    query = AgentMessage.query.filter_by(conversation_id=conversation.id)
    if current_message_id:
        query = query.filter(AgentMessage.id != current_message_id)
    rows = query.filter(
        AgentMessage.role.in_({'user', 'assistant'}),
        AgentMessage.kind.in_({'text', 'clarification', 'plan', 'run', 'status'}),
    ).order_by(
        AgentMessage.created_at.desc(), AgentMessage.id.desc(),
    ).limit(SEMANTIC_CONTEXT_MAX_MESSAGES).all()

    result = []
    remaining = SEMANTIC_CONTEXT_MAX_CHARS
    # Rows are newest-first so the bounded budget always keeps the turns that
    # are most useful for pronouns and follow-up requests.
    for message in rows:
        content = re.sub(r'\s+', ' ', str(message.content or '')).strip()
        if message.kind == 'run':
            run_status = str(message.get_metadata().get('status') or '').strip()
            content = f'[Результат запуска{f": {run_status}" if run_status else ""}] {content}'
        if not content or remaining <= 0:
            continue
        content = content[:min(900, remaining)]
        result.append({'role': message.role, 'content': content})
        remaining -= len(content)
    return list(reversed(result))


def _latest_conversation_scope(
    conversation: AgentConversation,
    current_message_id: str = None,
) -> Optional[dict]:
    """Return the latest server-persisted non-global user scope.

    Only user-message metadata written by the harness is considered. An empty
    or explicitly global turn is a boundary, so an older card selection cannot
    leak back into a later unrelated part of the conversation.
    """
    query = AgentMessage.query.filter_by(
        conversation_id=conversation.id, role='user',
    )
    if current_message_id:
        query = query.filter(AgentMessage.id != current_message_id)
    message = query.order_by(
        AgentMessage.created_at.desc(), AgentMessage.id.desc(),
    ).first()
    if not message:
        return None
    metadata = message.get_metadata()
    if metadata.get('scope_origin') == 'global':
        return None
    raw_scope = metadata.get('entity_scope') or {}
    kind = str(raw_scope.get('kind') or '').strip().lower()
    if kind == 'marketplace_listing':
        raw_ids = raw_scope.get('ids')
        marketplace_code = str(
            raw_scope.get('marketplace_code') or ''
        ).strip().lower()
        account_id = raw_scope.get('account_id')
        if (
            not isinstance(raw_ids, list)
            or not 0 < len(raw_ids) <= MAX_MARKETPLACE_LISTING_IDS
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                for value in raw_ids
            )
            or len(set(raw_ids)) != len(raw_ids)
            or not re.fullmatch(r'[a-z][a-z0-9_-]{1,49}', marketplace_code)
            or not isinstance(account_id, int)
            or isinstance(account_id, bool)
            or account_id <= 0
            or raw_scope.get('scope_mode') != 'selected'
        ):
            return None
        return {
            'kind': 'marketplace_listing',
            'ids': list(raw_ids),
            'marketplace_code': marketplace_code,
            'account_id': account_id,
            'scope_mode': 'selected',
            'page_context': _normalize_page_context(metadata.get('page_context')),
        }
    if kind not in {'product', 'imported_product'}:
        return None
    ids = _normalize_product_ids(raw_scope.get('ids'))
    if not ids:
        return None
    return {
        'kind': kind,
        'ids': ids,
        'page_context': _normalize_page_context(metadata.get('page_context')),
    }


def _scope_inheritance_blocked(text: str) -> bool:
    """Detect an explicit request to leave or replace the active card scope."""
    normalized = str(text or '').strip().lower()
    if not normalized:
        return True
    if _extract_named_scope(normalized) or _allows_global_write(normalized):
        return True
    if _contains_any(normalized, (
        'весь каталог', 'всём каталоге', 'во всем каталоге',
        'все карточки', 'все товары', 'другие карточки', 'другие товары',
        'новые карточки', 'новые товары', 'смени выбор', 'сбрось выбор',
        'убери выбор', 'без выбранных',
    )):
        return True
    # IDs mentioned only in free text are not a trusted typed selection. Do
    # not silently apply the previous selection; the planner will clarify it.
    return bool(re.search(
        r'\b(?:id|карточк\w*|товар\w*|артикул\w*)\s*[#№:]?\s*\d{2,}\b',
        normalized,
    ))


def _conversation_memory(
    conversation: AgentConversation,
    current_message_id: str = None,
) -> dict:
    """Build bounded durable state from the whole chat without another LLM."""
    query = AgentMessage.query.filter_by(conversation_id=conversation.id)
    if current_message_id:
        query = query.filter(AgentMessage.id != current_message_id)
    rows = query.filter(
        AgentMessage.role == 'assistant',
        AgentMessage.kind.in_({'plan', 'run', 'clarification'}),
    ).order_by(
        AgentMessage.created_at.desc(), AgentMessage.id.desc(),
    ).limit(40).all()

    memory = {}
    for message in rows:
        metadata = message.get_metadata()
        if message.kind == 'plan' and 'last_plan' not in memory:
            steps = metadata.get('steps') if isinstance(metadata.get('steps'), list) else []
            memory['last_plan'] = {
                'title': str(metadata.get('title') or '')[:180],
                'summary': str(metadata.get('summary') or message.content or '')[:700],
                'status': str(metadata.get('status') or '')[:40],
                'risk': str(metadata.get('risk') or '')[:20],
                'skills': [
                    str(step.get('agent') or '')[:80]
                    for step in steps[:8] if isinstance(step, dict)
                ],
            }
        elif message.kind == 'run' and 'last_run' not in memory:
            result = metadata.get('result') if isinstance(metadata.get('result'), dict) else {}
            compact = {
                'status': str(metadata.get('status') or result.get('status') or '')[:40],
                'message': str(result.get('message') or message.content or '')[:900],
            }
            for key in ('processed', 'saved', 'failed', 'requested_fields'):
                if key in result:
                    compact[key] = result[key]
            step_results = result.get('results') if isinstance(result.get('results'), list) else []
            if step_results:
                compact['steps'] = [{
                    'skill': str(item.get('skill') or '')[:80],
                    'status': str(item.get('status') or '')[:40],
                    'message': str((item.get('result') or {}).get('message') or '')[:500],
                    'requested_fields': (item.get('result') or {}).get('requested_fields'),
                } for item in step_results[-6:] if isinstance(item, dict)]
                for item in reversed(step_results):
                    if not isinstance(item, dict):
                        continue
                    step_result = item.get('result')
                    if not isinstance(step_result, dict):
                        continue
                    requested_fields = normalize_content_fields(
                        step_result.get('requested_fields'),
                    )
                    if requested_fields:
                        compact['requested_fields'] = requested_fields
                        break
            memory['last_run'] = compact
        elif message.kind == 'clarification' and 'last_clarification' not in memory:
            memory['last_clarification'] = str(message.content or '')[:500]
        if {'last_plan', 'last_run', 'last_clarification'} <= set(memory):
            break
    return memory


def _is_no_write_request(text: str) -> bool:
    return _is_global_no_write_request(text)


def _has_write_action(text: str) -> bool:
    return _contains_any(text.lower(), (
        'улучш', 'перепиш', 'обнов', 'сделай', 'исправ', 'оптимиз',
        'нормализ', 'заполн', 'подготов', 'импортиру', 'опублику',
        'установ', 'поставь', 'рассчитай', 'пересчитай', 'проверь',
    ))


def _has_mutation_intent(text: str) -> bool:
    """Return true only for an action that can change data, not an audit verb."""
    return _contains_any(str(text or '').lower(), (
        'улучш', 'перепиш', 'обнов', 'сделай', 'исправ', 'оптимиз',
        'нормализ', 'заполн', 'подготов', 'импортиру', 'опублику',
        'отправ', 'примен', 'установ', 'поставь', 'рассчитай', 'пересчитай',
    ))


def _allows_global_write(text: str) -> bool:
    normalized = text.lower()
    return _contains_any(normalized, (
        'все карточ', 'все товар', 'всех карточ', 'всех товар',
        'весь каталог', 'всём каталоге', 'всем карточ', 'всем товар',
        'всю витрину', 'для всех карточ', 'для всех товар',
    ))


def _is_global_no_write_request(text: str) -> bool:
    """Detect an instruction that forbids every side effect, not one field."""
    normalized = text.lower()
    if _contains_any(normalized, (
        'только анализ', 'ничего не сохраняй', 'не сохраняй', 'не применяй',
        'не записывай', 'только покажи', 'просто покажи', 'только предложи',
        'оставь всё как есть', 'оставь все как есть',
    )) or re.search(r'^\s*без\s+изменений\b', normalized):
        return True
    action = (
        r'(?:меняй|изменяй|обновляй|переписывай|трогай|улучшай|'
        r'оптимизируй|исправляй|сохраняй|применяй)'
    )
    return bool(
        re.search(rf'\bничего\s+не\s+(?:надо\s+)?{action}\b', normalized)
        or re.search(rf'\bне\s+(?:надо\s+)?{action}\s+ничего\b', normalized)
        or re.search(
            r'\bне\s+(?:надо|нужно)\s+ничего\s+'
            r'(?:менять|изменять|обновлять|переписывать|трогать|улучшать|'
            r'оптимизировать|исправлять)\b', normalized,
        )
        or re.search(
            r'\bничего\s+(?:менять|изменять|обновлять|переписывать|трогать|'
            r'улучшать|оптимизировать|исправлять)\s+'
            r'не\s+(?:надо|нужно)\b', normalized,
        )
    )


def _extract_named_scope(text: str) -> str:
    """Extract a supplier/person reference without expanding it to all products."""
    match = re.search(
        r'\b(?:карточ(?:ки|ек|ку|кам|ками|ках)?|товар(?:ы|ов|ам|ами|ах)?)\s+'
        r'([a-zа-яё][a-zа-яё-]{1,40})',
        text.lower(),
    )
    if not match:
        return ''
    candidate = match.group(1)
    if candidate in {
        'которые', 'которых', 'поставщика', 'готовые', 'импортировали',
        'без', 'для', 'из', 'на', 'по', 'со', 'импортированные',
    }:
        return ''
    return candidate


def _page_entity_kind(page_context: dict) -> str:
    url = str((page_context or {}).get('url') or '')
    route = str((page_context or {}).get('route') or '')
    path = urlsplit(url).path.rstrip('/')
    if route == 'product_detail' or re.fullmatch(
        r'/products/\d+(?:/(?:edit|enrich|history(?:/\d+(?:/revert)?)?))?', path,
    ):
        return 'product'
    if re.search(r'/products/\d+(?:/|$)', path):
        return 'unsupported'
    if re.fullmatch(r'/marketplaces/listings/\d+', path):
        # The URL identifies only a row number.  A usable marketplace scope
        # still requires the separately grounded marketplace/account tuple.
        return 'marketplace_listing'
    return 'imported_product'


def _resolve_entity_kind(value, page_context: dict = None) -> str:
    """Resolve an explicit typed scope, falling back to trusted page shape."""
    if value in (None, ''):
        return _page_entity_kind(page_context)
    normalized = str(value).strip().lower()
    if normalized not in {'product', 'imported_product', 'marketplace_listing'}:
        raise ValueError(
            'entity_kind must be product, imported_product or marketplace_listing'
        )
    return normalized


def _parse_number(value: str) -> float:
    return float(re.sub(r'\s+', '', value).replace(',', '.'))


def _is_plain_catalog_listing(text: str, wb_catalog: bool = False) -> bool:
    """Accept only an unqualified count/list; modifiers belong to semantics."""
    normalized = re.sub(r'[?!.]+$', '', re.sub(r'\s+', ' ', text.strip().lower())).strip()
    noun = r'(?:товар\w*|карточ\w*)'
    imported = r'(?:импортированн\w*\s+)?'
    if wb_catalog:
        patterns = (
            rf'сколько(?:\s+всего)?(?:\s+у\s+меня)?\s+{noun}\s+(?:на\s+wb|wb)',
            rf'(?:общее\s+количество|всего)\s+{noun}\s+(?:на\s+wb|wb)',
            rf'(?:покажи|показать|выведи|дай)(?:\s+мне)?\s+(?:все\s+)?{noun}\s+(?:на\s+wb|wb)',
            rf'список\s+{noun}\s+(?:на\s+wb|wb)',
        )
    else:
        patterns = (
            rf'сколько(?:\s+всего)?(?:\s+у\s+меня)?\s+{imported}{noun}',
            rf'(?:общее\s+количество|всего)\s+{imported}{noun}',
            rf'(?:покажи|показать|выведи|дай)(?:\s+мне)?\s+(?:все\s+)?{imported}{noun}',
            rf'список\s+{imported}{noun}',
        )
    return any(re.fullmatch(pattern, normalized) for pattern in patterns)


def _extract_catalog_query(text: str) -> Optional[dict]:
    """Parse common read-only catalog intents without an LLM call."""
    if _contains_any(text, (
        'измени', 'установ', 'поставь', 'подними', 'снизь', 'оптимиз',
        'пересчитай', 'заполни', 'исправь', 'обнови',
    )):
        return None
    read_intent = _contains_any(text, (
        'сколько', 'какие', 'найди', 'покажи', 'посмотр', 'список',
        'есть ли', 'имеют', 'товар', 'карточ', 'артикул',
    ))
    if not read_intent:
        return None

    number = r'(\d[\d\s]*(?:[.,]\d+)?)'
    params = {'limit': 100}
    labels = []

    between = re.search(rf'цен\w*\s+(?:от\s+)?{number}\s+(?:до|—|-)\s*{number}', text)
    above = re.search(
        rf'(?:цен\w*\s+(?:выше|больше|дороже)|дороже)\s*{number}', text,
    )
    below = re.search(
        rf'(?:цен\w*\s+(?:ниже|меньше|дешевле)|дешевле)\s*{number}', text,
    )
    if between:
        low, high = sorted((_parse_number(between.group(1)), _parse_number(between.group(2))))
        params.update(price_min=low, price_max=high)
        labels.append(f'с ценой от {low:,.0f} до {high:,.0f} ₽'.replace(',', ' '))
    elif above:
        value = _parse_number(above.group(1))
        params['price_min'] = value
        labels.append(f'с ценой выше {value:,.0f} ₽'.replace(',', ' '))
    elif below:
        value = _parse_number(below.group(1))
        params['price_max'] = value
        labels.append(f'с ценой ниже {value:,.0f} ₽'.replace(',', ' '))

    quantity_below = re.search(
        rf'остат\w*\s+(?:ниже|меньше)\s*{number}', text,
    )
    quantity_above = re.search(
        rf'остат\w*\s+(?:выше|больше)\s*{number}', text,
    )
    if _contains_any(text, ('нет в наличии', 'не в наличии', 'закончились', 'нулевым остат', 'остаток 0')):
        params['stock_state'] = 'out_of_stock'
        labels.append('без остатка')
    elif _contains_any(text, ('есть в наличии', 'в наличии', 'положительным остат')):
        params['stock_state'] = 'in_stock'
        labels.append('в наличии')
    elif quantity_below:
        value = int(_parse_number(quantity_below.group(1)))
        params['quantity_max'] = value
        labels.append(f'с остатком меньше {value}')
    elif quantity_above:
        value = int(_parse_number(quantity_above.group(1)))
        params['quantity_min'] = value
        labels.append(f'с остатком больше {value}')

    missing_patterns = (
        ('description', ('без описани', 'нет описани', 'пустым описани'), 'без описания'),
        ('brand', ('без бренд', 'нет бренд', 'пустым бренд'), 'без бренда'),
        ('category', ('без категори', 'нет категори', 'не определена категори'), 'без категории WB'),
        ('photos', ('без фото', 'нет фото', 'без изображен'), 'без фотографий'),
        ('characteristics', ('без характерист', 'нет характерист', 'без атрибут'), 'без характеристик'),
        ('title', ('без назван', 'нет назван', 'пустым назван'), 'без названия'),
        ('price', ('без цен', 'нет цен', 'цена не рассчитана'), 'без рассчитанной цены'),
        ('validation_errors', (
            'с ошибк валидац', 'с ошибками валидац', 'не прошли валидац',
            'ошибки валидац',
        ), 'с ошибками валидации'),
    )
    for field, patterns, label in missing_patterns:
        if _contains_any(text, patterns):
            params['missing_field'] = field
            labels.append(label)
            break

    if _contains_any(text, ('не опубликован', 'неопубликован', 'не выгружен', 'не привязан к wb')):
        params['published'] = 'no'
        labels.append('ещё не опубликованные')
    elif _contains_any(text, ('уже опубликован', 'выгружен на wb', 'привязан к wb')):
        params['published'] = 'yes'
        labels.append('опубликованные')

    status_patterns = {
        'failed': (
            'ошибка импорт', 'не импортировались', 'импорт завершился с ошиб',
            'статус failed', 'статусом failed',
        ),
        'pending': ('ожидают импорт', 'в ожидании импорт', 'статус pending'),
        'validated': ('прошли валидац', 'статус validated'),
        'imported': ('успешно импортирован', 'статус imported'),
    }
    for status, patterns in status_patterns.items():
        if _contains_any(text, patterns):
            params['import_status'] = status
            labels.append(f'со статусом {status}')
            break

    vendor = re.search(
        r'(?:артикул(?:у|ом)?|sku)\s*[:№#]?\s*([a-zа-яё0-9._/-]{2,100})', text,
    )
    if vendor:
        params['vendor_code'] = vendor.group(1)
        labels.append(f'с артикулом {vendor.group(1)}')

    if not labels and not _is_plain_catalog_listing(text):
        return None
    params['condition_label'] = ', '.join(labels) if labels else 'во всём импортированном каталоге'
    return params


def _extract_wb_catalog_query(text: str) -> Optional[dict]:
    """Recognize unambiguous queries about cards already in the WB catalog."""
    if _contains_any(text, (
        'измени', 'установ', 'поставь', 'подними', 'снизь', 'оптимиз',
        'пересчитай', 'заполни', 'исправь', 'обнови', 'отправь',
    )):
        return None
    if not _contains_any(text, ('на wb', 'карточки wb', 'карточек wb', 'товары wb')):
        return None
    if not _contains_any(text, ('сколько', 'какие', 'покажи', 'найди', 'посмотр', 'список')):
        return None
    params = {'limit': 100, 'entity_kind': 'product'}
    labels = []
    if _contains_any(text, ('неактивн', 'отключен')):
        params['active'] = 'no'
        labels.append('неактивные')
    elif 'активн' in text:
        params['active'] = 'yes'
        labels.append('активные')
    if _contains_any(text, ('нет в наличии', 'не в наличии', 'без остат', 'остаток 0')):
        params['stock_state'] = 'out_of_stock'
        labels.append('без остатка')
    elif _contains_any(text, ('есть в наличии', 'в наличии')):
        params['stock_state'] = 'in_stock'
        labels.append('в наличии')
    quality = re.search(r'(?:quality\s*score|качеств\w*)\s*(?:ниже|меньше)\s*(\d{1,3})', text)
    if quality:
        params['quality_max'] = min(float(quality.group(1)), 100.0)
        labels.append(f'с Quality Score ниже {quality.group(1)}')
    if not labels and not _is_plain_catalog_listing(text, wb_catalog=True):
        return None
    params['condition_label'] = ', '.join(labels) if labels else 'в каталоге WB'
    return params


def _extract_system_query(text: str) -> Optional[dict]:
    has_api = 'api' in text or 'апи' in text
    if has_api and _contains_any(text, ('лог', 'ошиб')):
        return {'kind': 'api_errors', 'limit': 20}
    if has_api and _contains_any(text, ('ключ', 'подключ', 'работает')):
        return {'kind': 'api_status'}
    if 'дефолт' in text or ('настрой' in text and 'товар' in text) or 'минимум фото' in text:
        return {'kind': 'product_defaults'}
    if _contains_any(text, ('стоп-слов', 'запрещенн', 'запрещённ')):
        return {'kind': 'prohibited_words'}
    if (
        ('цен' in text and _contains_any(text, ('настрой', 'формул')))
        or 'комисси wb' in text or 'минимальн прибыль' in text
    ):
        return {'kind': 'pricing'}
    return None


def _is_explicit_knowledge_query(text: str) -> bool:
    """Recognize explicit document questions without spending a routing call."""
    return bool(re.search(
        r'(?:\bбаз(?:а|е|у|ой)\s+знаний\b|'
        r'\b(?:правил|регламент|инструкц|документац)\w*\s+(?:wb|вб|wildberries)\b|'
        r'\b(?:wb|вб|wildberries)\s+(?:правил|регламент|инструкц|документац)\w*\b|'
        r'\bсогласно\s+(?:правил|регламент|инструкц|документац)\w*\b|'
        r'\bчто\s+(?:сказано|написано)\s+в\s+(?:правил|регламент|инструкц|документац)\w*\b)',
        text, flags=re.IGNORECASE,
    ))


def _image_generation_intent(text: str) -> bool:
    """Strict local parser for an explicit request to create a new image."""
    normalized = str(text or '').lower()
    has_visual = _contains_any(normalized, (
        'фото', 'изображен', 'картинк', 'инфографик', 'фотостуди', 'сцен', 'scene',
    ))
    has_generation = _contains_any(normalized, (
        'сгенер', 'генерац', 'создай', 'создать', 'нарисуй',
        'новое фото', 'новую фотограф', 'помести в сцен', 'сделай сцен',
        'собери сцен', 'собрать сцен',
    ))
    return has_visual and has_generation


def _explicit_any_card_image_intent(text: str) -> bool:
    normalized = str(text or '').lower()
    return bool(
        _image_generation_intent(normalized)
        and re.search(r'\bлюб\w*\b', normalized)
        and re.search(r'\b(?:карточ\w*|товар\w*)\b', normalized)
    )


_IMAGE_PROVIDER_UNSAFE_CARD_RE = re.compile(
    r'(?:мастурб|вагин|аналь|фалл|дилдо|вибрат|клитор|эрекц|страпон|'
    r'бдсм|фетиш|пенис|простат|оростимул|секс[-\s]?(?:кукл|набор)|эрот)',
    re.IGNORECASE,
)


def _image_source_safety_rank(source: ImportedProduct) -> Optional[int]:
    """Prefer a provider-compatible card for an explicit "любая" request."""
    title = str(source.title or '').casefold()
    # This neutral packaged SKU type is compatible with the paid native-edit
    # path. The preference is generic by product type, never by seller or ID.
    if 'массажное масло' in title:
        return 0
    visible_context = ' '.join((
        title,
        str(source.category or '').casefold(),
        str(source.mapped_wb_category or '').casefold(),
    ))
    if _IMAGE_PROVIDER_UNSAFE_CARD_RE.search(visible_context):
        return None
    return 1


def _auto_select_image_scope(seller_id: int) -> Optional[dict]:
    """Pick one deterministic seller-owned card with a linked photo source.

    This is allowed only after an explicit "любая карточка" request and merely
    grounds the approval plan; it never starts the paid generation itself.
    """
    # Reuse Image Lab's URL normalization so the card selected for the plan is
    # guaranteed to pass the same source-photo gate during execution.
    from services.image_lab_service import photo_count

    sources = ImportedProduct.query.filter(
        ImportedProduct.seller_id == seller_id,
        ImportedProduct.photo_urls.isnot(None),
    ).order_by(ImportedProduct.id.asc()).limit(500).all()
    ranked_sources = [
        (_image_source_safety_rank(source), source)
        for source in sources if photo_count(source.photo_urls) > 0
    ]
    ranked_sources = [item for item in ranked_sources if item[0] is not None]
    if not ranked_sources:
        return None
    best_rank = min(item[0] for item in ranked_sources)
    sources = [
        source for rank, source in ranked_sources if rank == best_rank
    ]

    linked_ids = {
        int(source.product_id) for source in sources
        if isinstance(source.product_id, int) and source.product_id > 0
    }
    linked_nm_ids = {
        int(source.wb_nm_id) for source in sources
        if isinstance(source.wb_nm_id, int) and source.wb_nm_id > 0
    }
    product_filters = []
    if linked_ids:
        product_filters.append(Product.id.in_(linked_ids))
    if linked_nm_ids:
        product_filters.append(Product.nm_id.in_(linked_nm_ids))
    if product_filters:
        products = Product.query.filter(
            Product.seller_id == seller_id,
            db.or_(*product_filters),
        ).all()
        products.sort(key=lambda item: (
            -float(item.quality_impact or 0), int(item.id),
        ))
        for product in products:
            if product.id in linked_ids or product.nm_id in linked_nm_ids:
                return {'kind': 'product', 'ids': [int(product.id)]}

    # ImportedProduct is itself a supported typed Image Lab scope, so an
    # unlinked but seller-owned source remains a safe deterministic fallback.
    return {'kind': 'imported_product', 'ids': [int(sources[0].id)]}


def _style_reference_url(text: str) -> str:
    match = re.search(r'https://[^\s<>"\']{1,1000}', str(text or ''), re.IGNORECASE)
    return match.group(0).rstrip('.,);]') if match else ''


def build_plan(text: str, product_ids=None, page_context=None,
               entity_kind=None, entity_scope=None) -> Optional[HarnessPlan]:
    """Build a conservative deterministic plan without spending LLM tokens."""
    normalized = text.strip().lower()
    if not normalized:
        return None

    trusted_scope = entity_scope if isinstance(entity_scope, dict) else {}
    if trusted_scope.get('kind') == 'marketplace_listing':
        selected_ids = list(trusted_scope.get('ids') or [])
        entity_kind = 'marketplace_listing'
    else:
        selected_ids = _normalize_product_ids(product_ids)
        entity_kind = _resolve_entity_kind(entity_kind, page_context)
    named_scope = _extract_named_scope(normalized)
    if selected_ids and entity_kind == 'unsupported':
        return None
    if _is_explicit_knowledge_query(normalized):
        return HarnessPlan(
            title='Ответ по базе знаний',
            summary=(
                'Найти релевантные версии проверенных инструкций, собрать bounded '
                'контекст и ответить только по нему с обязательными источниками.'
            ),
            steps=[{
                'agent': 'knowledge-query', 'task_type': 'answer_knowledge',
                'label': 'Поиск в проверенных инструкциях',
                'params': {'query': text[:500]},
            }],
            execution_type='custom', pipeline=None,
            risk='read', confidence=0.99,
            scope_label='Глобальные и seller-scoped документы базы знаний',
        )
    marketplace_scope = entity_kind == 'marketplace_listing'
    if (
        selected_ids
        and not marketplace_scope
        and _image_generation_intent(normalized)
    ):
        if len(selected_ids) != 1:
            return None
        photo_match = re.search(
            r'(?:фото|изображен\w*)\s*[№#]?\s*(\d{1,2})\b', normalized,
        )
        photo_index = min(max(int(photo_match.group(1)) - 1, 0), 9) if photo_match else 0
        params = {
            'entity_kind': entity_kind,
            'photo_index': photo_index,
            'scene_hint': text[:700],
        }
        reference_url = _style_reference_url(text)
        if reference_url:
            params['style_reference_url'] = reference_url
        return HarnessPlan(
            title='Сгенерировать фото товара',
            summary=(
                'Gemini Flash подготовит безопасное описание сцены'
                + (' по приложенному визуальному референсу' if reference_url else '')
                + f', затем {CHAT_IMAGE_MODEL} создаст один review-only вариант '
                f'за {chat_image_cost_label()}. Автопубликации не будет.'
            ),
            steps=[{
                'agent': 'image-generator',
                'task_type': 'generate_product_image',
                'label': 'Сцена Gemini Flash → генерация фото',
                'params': params,
            }],
            execution_type='custom', pipeline=None,
            risk='write', confidence=0.99,
            scope_label=f'Карточка #{selected_ids[0]} · 1 платная генерация',
        )
    if _image_generation_intent(normalized):
        # A paid run never expands to the catalog and never falls through to
        # the legacy read-only photo audit.
        return None
    selected_audit_fast_path = bool(
        'аудит' in normalized
        or re.search(
            r'\b(?:проверь|проверить|проанализируй)\b.{0,50}'
            r'\b(?:выбран\w*|карточ\w*)\b',
            normalized,
        )
    )
    if selected_ids and not _has_mutation_intent(normalized) and selected_audit_fast_path:
        return HarnessPlan(
            title=f'Аудит выбранных карточек ({len(selected_ids)})',
            summary=(
                'Проверить выбранные карточки одним пакетным запросом по локальным '
                'правилам, без вызова LLM и без изменения данных.'
            ),
            steps=[{
                'agent': (
                    'marketplace-listing-audit' if marketplace_scope
                    else 'batch-audit'
                ),
                'task_type': (
                    'audit_marketplace_listings' if marketplace_scope
                    else 'audit_selection'
                ),
                'label': f'Проверка {len(selected_ids)} карточек',
                'params': {'entity_kind': entity_kind, 'focus_limit': 100},
            }],
            execution_type='custom', pipeline=None,
            risk='read', confidence=0.99,
            scope_label=(
                f'{len(selected_ids)} карточек '
                f'{trusted_scope.get("marketplace_code", "маркетплейса").upper()} · '
                f'кабинет #{trusted_scope.get("account_id")}'
                if marketplace_scope
                else f'{len(selected_ids)} выбранных карточек'
            ),
        )
    if selected_ids and _contains_any(normalized, (
        'что можешь сказать', 'что скажешь', 'проанализируй карточ',
        'оцени карточ', 'как тебе карточ',
    )):
        marketplace_scope = entity_kind == 'marketplace_listing'
        if marketplace_scope and len(selected_ids) != 1:
            return None
        return HarnessPlan(
            title='Анализ выбранной карточки',
            summary='Проверить содержимое карточки и вернуть сильные стороны, проблемы и следующие действия.',
            steps=[{
                'agent': (
                    'marketplace-listing-insight' if marketplace_scope
                    else 'card-insight'
                ),
                'task_type': (
                    'analyze_marketplace_listing' if marketplace_scope
                    else 'analyze_card'
                ),
                'label': 'Анализ карточки',
                'params': {'entity_kind': entity_kind},
            }],
            execution_type='custom', pipeline=None,
            risk='read', confidence=0.99,
            scope_label=(
                f'{trusted_scope.get("marketplace_code", "marketplace").upper()} '
                f'листинг #{selected_ids[0]} · кабинет #{trusted_scope.get("account_id")}'
                if marketplace_scope
                else f'Карточка #{selected_ids[0]} на текущей странице'
            ),
        )

    content_fields = extract_explicit_content_fields(normalized)
    content_request_is_composite = any(
        _contains_any(normalized, spec['keywords'])
        for key, spec in SKILLS.items()
        if key in {'characteristics', 'photos', 'reviews', 'price', 'sizes', 'brand'}
    )
    content_request_is_publish = bool(
        re.search(r'\b(?:отправ|опублик|примен)\w*\b.{0,80}\b(?:wb|вб)\b', normalized)
        or re.search(r'\b(?:wb|вб)\b.{0,80}\b(?:отправ|опублик|примен)\w*\b', normalized)
        or re.search(r'\b(?:подготовленн|предложенн)\w*\s+измен', normalized)
    )
    if (
        entity_kind != 'marketplace_listing'
        and selected_ids
        and content_fields
        and not _is_global_no_write_request(normalized)
        and _has_mutation_intent(normalized)
        and not content_request_is_composite
        and not content_request_is_publish
    ):
        fields_label = content_fields_label(content_fields)
        selection_label = (
            f'карточки #{selected_ids[0]}'
            if len(selected_ids) == 1
            else f'{len(selected_ids)} выбранных карточек'
        )
        steps = [{
            'agent': 'content-writer', 'task_type': 'rewrite_content',
            'label': f'Новые {fields_label}',
            'params': {
                'entity_kind': entity_kind,
                'fields': content_fields,
                'instruction': text[:500],
            },
        }]
        return HarnessPlan(
            title=f'Улучшить {fields_label}: {selection_label}',
            summary=(
                f'Переписать только запрошенные поля: {fields_label}; '
                'проверить стоп-слова и сохранить проверяемый diff.'
            ),
            steps=steps,
            execution_type='custom', pipeline=None,
            risk='write', confidence=0.99,
            scope_label=(
                f'Карточка #{selected_ids[0]} на текущей странице'
                if len(selected_ids) == 1
                else f'{len(selected_ids)} выбранных карточек'
            ),
        )

    content_write = content_fields and _contains_any(
        normalized, ('улучш', 'перепиш', 'обнов', 'сделай', 'исправь', 'оптимиз'),
    )
    if named_scope and content_write:
        # A named supplier must first resolve to an explicit entity set. Never
        # let the generic SEO pipeline reinterpret it as the whole catalog.
        return None

    # Legacy catalog skills operate on ImportedProduct. Until a typed Product
    # implementation exists, never pass a main Product numeric ID to them.
    if selected_ids and entity_kind in {'product', 'marketplace_listing'}:
        return None

    system_params = _extract_system_query(normalized)
    if system_params:
        labels = {
            'api_errors': 'Последние ошибки API',
            'api_status': 'Статус подключения WB API',
            'product_defaults': 'Дефолты товаров',
            'prohibited_words': 'Стоп-слова',
            'pricing': 'Настройки ценообразования',
        }
        label = labels[system_params['kind']]
        return HarnessPlan(
            title=label, summary=f'Получить {label.lower()} напрямую из настроек.',
            steps=[{
                'agent': 'system-query', 'task_type': 'read_system_setting',
                'label': label, 'params': system_params,
            }],
            execution_type='custom', pipeline=None,
            risk='read', confidence=0.99, scope_label='Настройки текущего продавца',
        )

    wb_catalog_params = _extract_wb_catalog_query(normalized)
    if wb_catalog_params:
        condition = wb_catalog_params['condition_label']
        return HarnessPlan(
            title='Карточки в каталоге WB',
            summary=(
                f'Найти карточки {condition} одним SQL без изменения данных; '
                'кратко оформить точный счётчик через Flash.'
            ),
            steps=[{
                'agent': 'catalog-query', 'task_type': 'filter_wb_catalog',
                'label': f'Поиск: {condition}', 'params': wb_catalog_params,
            }],
            execution_type='custom', pipeline=None,
            risk='read', confidence=0.99,
            scope_label=f'Основные карточки {condition}',
        )
    catalog_params = _extract_catalog_query(normalized)
    if catalog_params:
        condition = catalog_params['condition_label']
        return HarnessPlan(
            title='Поиск по каталогу',
            summary=(
                f'Найти карточки {condition} одним SQL без изменения данных; '
                'кратко оформить точный счётчик через Flash.'
            ),
            steps=[{
                'agent': 'catalog-query', 'task_type': 'filter_imported_catalog',
                'label': f'Поиск: {condition}',
                'params': catalog_params,
            }],
            execution_type='custom', pipeline=None,
            risk='read', confidence=0.99,
            scope_label=f'Импортированные карточки {condition}',
        )
    is_audit = _contains_any(
        normalized, ('аудит', 'проверь карточ', 'проверить карточ', 'основные проблемы', 'ошибк карточ'),
    )
    is_supplier_unpublished_count = (
        named_scope
        and _contains_any(normalized, ('сколько', 'количество'))
        and _contains_any(normalized, (
            'недозагруж', 'не дозагруж', 'неопубликован', 'не опубликован',
            'не выгружен', 'не отправлен',
        ))
    )
    if is_supplier_unpublished_count:
        return HarnessPlan(
            title=f'Неопубликованные карточки: {named_scope.title()}',
            summary='Посчитать импортированные, но ещё не опубликованные карточки поставщика.',
            steps=[{
                'agent': 'supplier-audit',
                'task_type': 'count_unpublished_supplier_cards',
                'label': f'Подсчёт карточек: {named_scope.title()}',
                'params': {
                    'supplier_query': named_scope,
                    'focus_limit': 1,
                    'response_mode': 'unpublished_count',
                },
            }],
            execution_type='custom', pipeline=None,
            risk='read', confidence=0.99,
            scope_label=f'Импортированные карточки поставщика «{named_scope.title()}»',
        )
    if named_scope and is_audit:
        return HarnessPlan(
            title=f'Аудит карточек: {named_scope.title()}',
            summary=(
                'Сначала однозначно определить поставщика, затем агрегировать '
                'проблемы всех его импортированных карточек без изменения данных.'
            ),
            steps=[{
                'agent': 'supplier-audit',
                'task_type': 'audit_imported_supplier',
                'label': f'Аудит импортированных карточек: {named_scope.title()}',
                'params': {'supplier_query': named_scope, 'focus_limit': 100},
            }],
            execution_type='custom', pipeline=None,
            risk='read', confidence=0.98,
            scope_label=f'Импортированные карточки поставщика «{named_scope.title()}»',
        )

    named_write = named_scope and _has_write_action(normalized)
    if named_write:
        # The semantic planner may prepend a typed supplier resolver. A local
        # keyword plan must never silently reinterpret a named scope as the
        # entire imported catalog.
        return None

    if _is_no_write_request(normalized):
        return None

    is_rank_and_prepare = (
        _contains_any(normalized, ('выбер', 'отбери', 'отбрать'))
        and _contains_any(normalized, ('привлекатель', 'лучш', 'перспектив'))
        and _contains_any(normalized, ('подготов', 'публикац', 'на wb', 'к wb'))
    )
    if is_rank_and_prepare:
        count_match = re.search(r'\b(\d{1,3})\b', normalized)
        selection_count = min(max(int(count_match.group(1)) if count_match else 10, 1), 100)
        supplier_match = re.search(
            r'карточ(?:ки|ек|ку|кам)?\s+([a-zа-яё][a-zа-яё-]{1,40})',
            normalized,
        )
        supplier_query = supplier_match.group(1) if supplier_match else ''
        selection_step = {
            'agent': 'candidate-selector',
            'task_type': 'select_attractive_ready',
            'label': f'Выбор {selection_count} готовых карточек',
            'params': {
                'count': selection_count,
                'supplier_query': supplier_query,
            },
        }
        return HarnessPlan(
            title=f'Выбрать и подготовить {selection_count} товаров',
            summary=(
                f'Найти готовые карточки поставщика, выбрать {selection_count} '
                'наиболее привлекательных и подготовить их к WB с проверкой фактов.'
            ),
            steps=[selection_step, *PIPELINES['full_prepare']['steps']],
            execution_type='custom', pipeline=None,
            risk='write', confidence=0.91,
        )

    if _contains_any(normalized, (
        'подготов', 'импортиру', 'запусти импорт', 'опублику',
        'выгрузи', 'отправь на wb', 'отправить на wb',
    )):
        pipeline = PIPELINES['full_prepare']
        return HarnessPlan(
            title='Подготовить товары к WB',
            summary=pipeline['description'],
            steps=pipeline['steps'], execution_type='pipeline', pipeline='full_prepare',
            risk='write', confidence=0.96,
        )

    if _has_write_action(normalized) and _contains_any(
        normalized, ('seo', 'сео', 'заголов', 'описани', 'ключев'),
    ):
        pipeline = PIPELINES['seo_boost']
        return HarnessPlan(
            title='Улучшить SEO карточек', summary=pipeline['description'],
            steps=pipeline['steps'], execution_type='pipeline', pipeline='seo_boost',
            risk='write', confidence=0.94,
        )

    if _contains_any(normalized, ('аудит', 'проверь карточ', 'модерац', 'блокиров', 'ошибк карточ')):
        pipeline = PIPELINES['audit']
        return HarnessPlan(
            title='Провести аудит карточек', summary=pipeline['description'],
            steps=pipeline['steps'], execution_type='pipeline', pipeline='audit',
            risk='read', confidence=0.92,
        )

    if _has_write_action(normalized) and _contains_any(
        normalized, ('категори', 'subject', 'предмет wb'),
    ):
        pipeline = PIPELINES['category_fix']
        return HarnessPlan(
            title='Исправить категории и характеристики', summary=pipeline['description'],
            steps=pipeline['steps'], execution_type='pipeline', pipeline='category_fix',
            risk='write', confidence=0.93,
        )

    has_write_action = _has_write_action(normalized)
    matched = [
        spec for spec in SKILLS.values()
        if _contains_any(normalized, spec['keywords'])
        and (spec['risk'] == 'read' or has_write_action)
    ]
    if not matched:
        return None

    steps = [dict(spec['step']) for spec in matched]
    risk = 'write' if any(spec['risk'] == 'write' for spec in matched) else 'read'
    labels = ', '.join(step['label'].lower() for step in steps)
    return HarnessPlan(
        title='Выполнить задачу по товарам',
        summary=labels[:1].upper() + labels[1:],
        steps=steps, execution_type='custom', pipeline=None,
        risk=risk, confidence=0.88,
    )


def _new_message(conversation: AgentConversation, role: str, content: str,
                 kind: str = 'text', metadata: dict = None,
                 task_id: str = None) -> AgentMessage:
    message = AgentMessage(
        id=str(uuid.uuid4()), conversation_id=conversation.id,
        role=role, kind=kind, content=content, task_id=task_id,
    )
    message.set_metadata(metadata or {})
    db.session.add(message)
    now = datetime.utcnow()
    conversation.last_message_at = now
    conversation.updated_at = now
    return message


def create_conversation(seller_id: int, user_id: int, title: str = None) -> AgentConversation:
    conversation = AgentConversation(
        id=str(uuid.uuid4()), seller_id=seller_id, user_id=user_id,
        title=(title or 'Новый диалог')[:160],
    )
    db.session.add(conversation)
    db.session.commit()
    return conversation


def get_conversation(conversation_id: str, seller_id: int, user_id: int = None) -> Optional[AgentConversation]:
    query = AgentConversation.query.filter_by(id=conversation_id, seller_id=seller_id)
    if user_id is not None:
        query = query.filter_by(user_id=user_id)
    return query.first()


def list_conversations(seller_id: int, user_id: int, limit: int = 40) -> list[AgentConversation]:
    return AgentConversation.query.filter_by(
        seller_id=seller_id, user_id=user_id, status='active',
    ).order_by(AgentConversation.last_message_at.desc()).limit(min(limit, 100)).all()


def runtime_state(steps: list[dict] = None) -> dict:
    """The chat depends on one runtime; domain skills are in-process modules."""
    names = {'orchestrator'}
    agents = ServiceAgent.query.filter(ServiceAgent.name.in_(names)).all()
    by_name = {agent.name: agent for agent in agents}
    unavailable = []
    for name in sorted(names):
        agent = by_name.get(name)
        if not agent or agent.status != 'online' or not agent.is_online():
            unavailable.append(name)
    return {
        'online': not unavailable,
        'unavailable': unavailable,
        'orchestrator_id': by_name['orchestrator'].id if 'orchestrator' in by_name else None,
    }


def _create_run_from_plan(conversation: AgentConversation, plan_message: AgentMessage) -> AgentMessage:
    metadata = plan_message.get_metadata()
    if metadata.get('plan_version') != HARNESS_PLAN_VERSION:
        raise ValueError(
            'План создан старой версией помощника. Отправьте запрос заново, '
            'чтобы пересчитать область и стоимость выполнения.'
        )
    existing_task_id = metadata.get('task_id')
    if existing_task_id:
        existing = AgentMessage.query.filter_by(
            conversation_id=conversation.id, task_id=existing_task_id, kind='run',
        ).first()
        if existing:
            return existing

    state = runtime_state(metadata.get('steps'))
    if not state['online']:
        missing = ', '.join(state['unavailable'])
        raise RuntimeError(f'Исполнитель недоступен: {missing}')

    entity_scope = metadata.get('entity_scope') or {
        'kind': 'imported_product',
        'ids': metadata.get('product_ids') or [],
    }
    is_marketplace_scope = entity_scope.get('kind') == 'marketplace_listing'
    product_ids = [] if is_marketplace_scope else (metadata.get('product_ids') or [])
    marketplace_listing_ids = (
        list(entity_scope.get('ids') or []) if is_marketplace_scope else []
    )
    input_data = {
        'seller_id': conversation.seller_id,
        'product_ids': product_ids,
        'imported_product_ids': product_ids,
        'marketplace_listing_ids': marketplace_listing_ids,
        'model_policy': metadata.get('model_policy') or get_model_policy(conversation.seller_id),
        'source': 'unified_chat',
        'conversation_id': conversation.id,
        'plan_id': metadata.get('plan_id'),
        'risk': metadata.get('risk', 'write'),
        'text': metadata.get('request_text', ''),
        'page_context': metadata.get('page_context') or {},
        'entity_scope': entity_scope,
        'planning_usage': metadata.get('planning_usage') or {},
    }
    if metadata.get('execution_type') == 'pipeline' and metadata.get('pipeline'):
        task_type = 'pipeline'
        input_data['pipeline'] = metadata['pipeline']
    else:
        task_type = 'custom'
        input_data['steps'] = metadata.get('steps') or []

    task = agent_service.create_task(
        agent_id=state['orchestrator_id'], seller_id=conversation.seller_id,
        task_type=task_type, title=metadata.get('title') or 'Задача ИИ-помощника',
        input_data=input_data, priority=1,
        total_steps=len(metadata.get('steps') or []),
    )
    metadata['status'] = 'queued'
    metadata['task_id'] = task.id
    metadata['approved_at'] = datetime.utcnow().isoformat()
    plan_message.set_metadata(metadata)
    plan_message.updated_at = datetime.utcnow()

    return _new_message(
        conversation, 'assistant', 'Задача поставлена в очередь.',
        kind='run', task_id=task.id,
        metadata={'status': 'queued', 'plan_id': metadata.get('plan_id')},
    )


def _create_planning_run(conversation: AgentConversation, text: str,
                         product_ids: list[int], page_context: dict = None,
                         entity_kind: str = None,
                         entity_scope: dict = None,
                         current_message_id: str = None,
                         scope_origin: str = 'request') -> AgentMessage:
    state = runtime_state()
    if not state['online']:
        raise RuntimeError('ИИ-помощник не подключён')
    model_policy = get_model_policy(conversation.seller_id)
    if isinstance(entity_scope, dict) and entity_scope.get('kind') == 'marketplace_listing':
        trusted_scope = dict(entity_scope)
        resolved_kind = 'marketplace_listing'
        product_ids = []
        marketplace_listing_ids = list(trusted_scope.get('ids') or [])
    else:
        resolved_kind = _resolve_entity_kind(entity_kind, page_context)
        trusted_scope = {'kind': resolved_kind, 'ids': product_ids}
        marketplace_listing_ids = []
    task = agent_service.create_task(
        agent_id=state['orchestrator_id'], seller_id=conversation.seller_id,
        task_type='plan_request', title=f'Планирование: {text[:90]}',
        input_data={
            'seller_id': conversation.seller_id,
            'text': text,
            'product_ids': product_ids,
            'marketplace_listing_ids': marketplace_listing_ids,
            'page_context': page_context or {},
            'entity_scope': trusted_scope,
            'scope_origin': scope_origin,
            'source_message_id': current_message_id,
            'dialog_context': _compact_dialog_context(
                conversation, current_message_id=current_message_id,
            ),
            'conversation_memory': _conversation_memory(
                conversation, current_message_id=current_message_id,
            ),
            'named_scope_hint': _extract_named_scope(text),
            'allow_writes': not _is_global_no_write_request(text),
            'allow_global_write': _allows_global_write(text),
            'model_policy': model_policy,
            'source': 'unified_chat_planner',
        },
        priority=1, total_steps=1,
    )
    return _new_message(
        conversation, 'assistant', 'Анализирую цель и собираю безопасный план.',
        kind='run', task_id=task.id,
        metadata={
            'status': 'queued', 'phase': 'planning',
            'request_text': text, 'product_ids': product_ids,
            'marketplace_listing_ids': marketplace_listing_ids,
            'page_context': page_context or {},
            'entity_scope': trusted_scope,
            'scope_origin': scope_origin,
            'source_message_id': current_message_id,
            'model_policy': model_policy,
        },
    )


def submit_turn(conversation: AgentConversation, text: str,
                product_ids=None, page_context=None, entity_kind=None,
                scope_mode=None, entity_scope=None) -> dict:
    text = (text or '').strip()
    if not text:
        raise ValueError('Введите сообщение')
    if len(text) > MAX_MESSAGE_LENGTH:
        raise ValueError(f'Сообщение длиннее {MAX_MESSAGE_LENGTH} символов')
    if scope_mode not in {None, 'selected', 'global', 'page'}:
        raise ValueError('Неизвестный режим области карточек')
    context = _normalize_page_context(page_context)
    reference_resolution = None
    reference_issue = None
    auto_selected_image = False
    previous_scope = _latest_conversation_scope(conversation)

    if isinstance(entity_scope, dict) and entity_scope.get('kind') == 'marketplace_listing':
        if product_ids not in (None, []):
            raise ValueError(
                'marketplace_listing scope cannot be sent through product_ids'
            )
        if entity_kind not in (None, '', 'marketplace_listing'):
            raise ValueError('entity_kind does not match entity_scope.kind')
        trusted_scope = _ground_marketplace_entity_scope(
            entity_scope, conversation.seller_id,
        )
        ids = []
        resolved_kind = 'marketplace_listing'
        selected_scope_ids = trusted_scope['ids']
        scope_origin = 'request' if selected_scope_ids else 'global'
        if previous_scope and _scope_identity(previous_scope) == _scope_identity(trusted_scope):
            scope_origin = 'conversation'
    else:
        ids = _normalize_product_ids(product_ids)
        resolved_kind = _resolve_entity_kind(entity_kind, context)
        if resolved_kind == 'marketplace_listing':
            raise ValueError(
                'marketplace_listing requires exact marketplace_code, account_id and ids'
            )
        if scope_mode == 'global' and ids:
            raise ValueError('Глобальная область не может содержать ID карточек')
        if scope_mode in {'selected', 'page'} and not ids:
            raise ValueError('Выбранная область не содержит карточек')
        scope_origin = 'request' if ids else 'global'
        if not ids:
            reference_resolution = _resolve_message_product_scope(
                conversation.seller_id, text,
            )
            if reference_resolution and reference_resolution.get('ids'):
                ids = _normalize_product_ids(reference_resolution['ids'])
                resolved_kind = reference_resolution['kind']
                scope_origin = 'message_reference'
            elif reference_resolution and reference_resolution.get('issue'):
                reference_issue = reference_resolution
        if (
            not ids
            and reference_issue is None
            and _explicit_any_card_image_intent(text)
        ):
            automatic_scope = _auto_select_image_scope(conversation.seller_id)
            if automatic_scope:
                ids = _normalize_product_ids(automatic_scope['ids'])
                resolved_kind = automatic_scope['kind']
                scope_origin = 'request'
                auto_selected_image = True
        if (
            not auto_selected_image
            and scope_origin == 'request'
            and ids
            and previous_scope
            and previous_scope.get('ids') == ids
            and previous_scope.get('kind') == resolved_kind
        ):
            # Repeated transport of the same browser selection is conversational
            # context, not proof that every later sentence still targets it.
            scope_origin = 'conversation'
        if (
            not ids
            and reference_issue is None
            and scope_mode != 'global'
            and not _scope_inheritance_blocked(text)
            and previous_scope
            and previous_scope.get('kind') in {'product', 'imported_product'}
        ):
            ids = list(previous_scope['ids'])
            resolved_kind = previous_scope['kind']
            scope_origin = 'conversation'
        trusted_scope = {'kind': resolved_kind, 'ids': ids}
        selected_scope_ids = ids

    if context and selected_scope_ids:
        previous = AgentMessage.query.filter_by(
            conversation_id=conversation.id, role='user',
        ).order_by(AgentMessage.created_at.desc()).first()
        if previous:
            previous_metadata = previous.get_metadata()
            previous_context = previous_metadata.get('page_context') or {}
            previous_scope = previous_metadata.get('entity_scope') or {
                'kind': _page_entity_kind(previous_context),
                'ids': _normalize_product_ids(previous_metadata.get('product_ids')),
            }
            previous_scope_ids = previous_scope.get('ids') or []
            scope_changed = _scope_identity(previous_scope) != _scope_identity(trusted_scope)
            if previous_context and previous_scope_ids and scope_changed:
                raise RuntimeError(
                    'Открыта другая карточка. Для неё нужен отдельный диалог, '
                    'чтобы не смешивать контекст товаров.'
                )

    active = AgentMessage.query.join(AgentTask, AgentMessage.task_id == AgentTask.id).filter(
        AgentMessage.conversation_id == conversation.id,
        AgentMessage.kind == 'run',
        AgentTask.status.in_(ACTIVE_TASK_STATUSES),
    ).first()
    if active:
        raise RuntimeError('Сначала дождитесь завершения текущей задачи или остановите её')

    user_message = _new_message(
        conversation, 'user', text,
        metadata={
            'product_ids': ids,
            'marketplace_listing_ids': (
                selected_scope_ids if resolved_kind == 'marketplace_listing' else []
            ),
            'scope_label': (
                f'{len(selected_scope_ids)} карточек '
                f'{trusted_scope.get("marketplace_code", "").upper()} · '
                f'кабинет #{trusted_scope.get("account_id")}'
                if resolved_kind == 'marketplace_listing'
                else '1 товар · выбран помощником по запросу «любой»'
                if ids and auto_selected_image
                else
                f'{len(ids)} товаров · по артикулу из сообщения'
                if ids and scope_origin == 'message_reference'
                else
                f'{len(ids)} товаров · контекст диалога'
                if ids and scope_origin == 'conversation'
                else f'{len(ids)} товаров' if ids else 'Весь каталог'
            ),
            'scope_origin': scope_origin,
            'scope_mode': (
                trusted_scope.get('scope_mode', 'selected')
                if resolved_kind == 'marketplace_listing'
                else ('selected' if ids else (scope_mode or 'global'))
            ),
            'page_context': context,
            'entity_scope': trusted_scope,
            'resolved_references': (
                reference_resolution.get('references', [])
                if reference_resolution and reference_resolution.get('ids') else []
            ),
        },
    )
    if conversation.title == 'Новый диалог':
        conversation.title = re.sub(r'\s+', ' ', text)[:72]

    if reference_issue:
        problem_refs = (
            reference_issue.get('unresolved')
            or reference_issue.get('ambiguous')
            or reference_issue.get('references')
            or []
        )
        shown = ', '.join(problem_refs[:5])
        if reference_issue.get('issue') == 'ambiguous':
            content = (
                f'Нашёл несколько карточек для артикула {shown}. '
                'Уточните карточку или выберите её в каталоге — я не буду '
                'угадывать область изменения.'
            )
        else:
            content = (
                f'Не нашёл у текущего продавца карточку с артикулом {shown}. '
                'Проверьте артикул или выберите карточку в каталоге.'
            )
        assistant = _new_message(
            conversation, 'assistant', content, kind='clarification',
            metadata={
                'reason': f"product_reference_{reference_issue.get('issue')}",
                'references': problem_refs[:20],
            },
        )
        db.session.commit()
        return {
            'user_message': user_message,
            'assistant_message': assistant,
            'run': None,
        }

    answer = direct_response(text)
    if answer:
        assistant = _new_message(conversation, 'assistant', answer)
        db.session.commit()
        return {'user_message': user_message, 'assistant_message': assistant, 'run': None}

    usage_answer = _conversation_usage_response(conversation, text)
    if usage_answer:
        assistant = _new_message(conversation, 'assistant', usage_answer)
        db.session.commit()
        return {'user_message': user_message, 'assistant_message': assistant, 'run': None}

    # Exact, safety-critical recipes win over the semantic planner. This keeps
    # named scopes and common audits deterministic and avoids one model call.
    # A scope inherited from an earlier turn is trusted as a set of IDs, but
    # interpreting whether the new natural-language request still refers to it
    # is semantic work. Never let a keyword fast-path silently act on that
    # inherited set (especially when the seller made a typo).
    plan = (
        None if scope_origin == 'conversation'
        else build_plan(
            text, ids, context, resolved_kind, entity_scope=trusted_scope,
        )
    )
    marketplace_write_requested = bool(re.search(
        r'\b(?:измени(?:ть)?|поменяй|обнови|установи|подними|снизь|обнули|'
        r'опубликуй|создай|перепиши|улучши|исправь|оптимизируй)\b',
        text.lower(),
    ))
    if (
        plan is None
        and resolved_kind == 'marketplace_listing'
        and selected_scope_ids
        and marketplace_write_requested
    ):
        assistant = _new_message(
            conversation, 'assistant',
            'Эта Ozon-карточка связана с общей внутренней карточкой, но старый '
            'WB-редактор нельзя безопасно применять к ID листинга. Сейчас я могу '
            'провести read-only аудит или анализ. Изменения Ozon будут доступны '
            'только через отдельный marketplace proposal с ручным подтверждением.',
            kind='clarification', metadata={
                'reason': 'marketplace_listing_write_requires_proposal',
                'entity_scope': trusted_scope,
            },
        )
        db.session.commit()
        return {'user_message': user_message, 'assistant_message': assistant, 'run': None}
    if plan is None and ids and resolved_kind == 'unsupported':
        assistant = _new_message(
            conversation, 'assistant',
            'Я вижу карточку на текущей странице, но это действие пока не имеет '
            'безопасного обработчика для данного типа товара. Я не буду подставлять '
            'совпадающий ID из другого каталога. Для основной карточки сейчас доступны '
            '«что можешь сказать по этой карточке?» и «улучши её описание».',
            kind='clarification', metadata={'reason': 'unsupported_entity_action'},
        )
        db.session.commit()
        return {'user_message': user_message, 'assistant_message': assistant, 'run': None}
    named_scope = _extract_named_scope(text)
    named_content_write = (
        named_scope
        and extract_explicit_content_fields(text)
        and _contains_any(text.lower(), (
            'улучш', 'перепиш', 'обнов', 'сделай', 'исправь', 'оптимиз',
        ))
    )
    if plan is None and named_content_write:
        assistant = _new_message(
            conversation, 'assistant',
            f'Я распознал поставщика «{named_scope.title()}», но перед изменением '
            'нужен точный список его карточек. Сначала запросите аудит карточек этого '
            'поставщика, выберите нужные позиции в результате и запустите изменение '
            'для выбранных. Я не расширяю такую задачу на весь каталог.',
            kind='clarification', metadata={
                'reason': 'named_write_scope_requires_selection',
                'supplier_query': named_scope,
            },
        )
        db.session.commit()
        return {'user_message': user_message, 'assistant_message': assistant, 'run': None}
    if plan is None and _needs_semantic_planner(text):
        try:
            planning_message = _create_planning_run(
                conversation, text, ids, context, resolved_kind,
                entity_scope=trusted_scope,
                current_message_id=user_message.id,
                scope_origin=scope_origin,
            )
        except RuntimeError as exc:
            assistant = _new_message(
                conversation, 'assistant', str(exc), kind='clarification',
            )
            db.session.commit()
            return {'user_message': user_message, 'assistant_message': assistant, 'run': None}
        db.session.commit()
        return {
            'user_message': user_message,
            'assistant_message': planning_message,
            'run': planning_message,
        }

    if not plan:
        assistant = _new_message(
            conversation, 'assistant',
            'Уточните действие: например, «проведи аудит карточек», '
            '«улучши SEO» или «подготовь товары к WB». Я не запускаю '
            'изменения по неоднозначному запросу.',
            kind='clarification', metadata={'reason': 'low_confidence'},
        )
        db.session.commit()
        return {'user_message': user_message, 'assistant_message': assistant, 'run': None}

    metadata = plan.to_dict(ids, get_model_policy(conversation.seller_id))
    metadata['request_text'] = text
    metadata['page_context'] = context
    metadata['entity_scope'] = trusted_scope
    metadata['marketplace_listing_ids'] = (
        selected_scope_ids if resolved_kind == 'marketplace_listing' else []
    )
    plan_message = _new_message(
        conversation, 'assistant', plan.summary,
        kind='plan', metadata=metadata,
    )
    run_message = None
    if not plan.requires_approval:
        try:
            run_message = _create_run_from_plan(conversation, plan_message)
        except RuntimeError as exc:
            metadata = plan_message.get_metadata()
            metadata['status'] = 'runtime_unavailable'
            metadata['runtime_error'] = str(exc)
            plan_message.set_metadata(metadata)
    db.session.commit()
    return {'user_message': user_message, 'assistant_message': plan_message, 'run': run_message}


def approve_plan(conversation: AgentConversation, message_id: str) -> AgentMessage:
    plan_message = AgentMessage.query.filter_by(
        id=message_id, conversation_id=conversation.id, kind='plan',
    ).first()
    if not plan_message:
        raise ValueError('План не найден')
    metadata = plan_message.get_metadata()
    if metadata.get('status') not in {'pending_approval', 'runtime_unavailable'}:
        raise ValueError('Этот план уже обработан')
    run_message = _create_run_from_plan(conversation, plan_message)
    db.session.commit()
    return run_message


def reject_plan(conversation: AgentConversation, message_id: str) -> AgentMessage:
    plan_message = AgentMessage.query.filter_by(
        id=message_id, conversation_id=conversation.id, kind='plan',
    ).first()
    if not plan_message:
        raise ValueError('План не найден')
    metadata = plan_message.get_metadata()
    if metadata.get('task_id'):
        raise ValueError('План уже запущен')
    metadata['status'] = 'rejected'
    metadata['rejected_at'] = datetime.utcnow().isoformat()
    plan_message.set_metadata(metadata)
    plan_message.updated_at = datetime.utcnow()
    _new_message(conversation, 'assistant', 'План отменён. Данные не изменялись.', kind='status')
    db.session.commit()
    return plan_message


def task_tree_ids(root_task_id: str) -> list[str]:
    """Return a task tree breadth-first; callers may reverse it for undo."""
    result = []
    frontier = [root_task_id]
    seen = set()
    while frontier:
        batch = [task_id for task_id in frontier if task_id not in seen]
        if not batch:
            break
        result.extend(batch)
        seen.update(batch)
        frontier = [row[0] for row in db.session.query(AgentTask.id).filter(
            AgentTask.parent_task_id.in_(batch),
        ).all()]
    return result


def snapshot_count(root_task_id: str, pending_only: bool = True) -> int:
    ids = task_tree_ids(root_task_id)
    query = AgentChangeSnapshot.query.filter(AgentChangeSnapshot.task_id.in_(ids))
    if pending_only:
        query = query.filter_by(is_rolled_back=False)
    card_query = CardEditHistory.query.filter(
        CardEditHistory.user_comment.in_([f'agent_task:{task_id}' for task_id in ids]),
        CardEditHistory.wb_synced.isnot(True),
        db.or_(
            CardEditHistory.wb_sync_status.is_(None),
            CardEditHistory.wb_sync_status.in_({'pending', 'failed', 'skipped'}),
        ),
    )
    if pending_only:
        card_query = card_query.filter_by(reverted=False)
    return query.count() + card_query.count()


def rollback_task_tree(root_task_id: str, seller_id: int) -> dict:
    root = AgentTask.query.filter_by(id=root_task_id, seller_id=seller_id).first()
    if not root:
        raise ValueError('Задача не найдена')
    ids = task_tree_ids(root_task_id)
    if AgentTask.query.filter(
        AgentTask.id.in_(ids), AgentTask.status.in_(ACTIVE_TASK_STATUSES),
    ).first():
        raise ValueError('Сначала остановите выполняющуюся задачу')

    snapshots = AgentChangeSnapshot.query.join(
        ImportedProduct, AgentChangeSnapshot.imported_product_id == ImportedProduct.id,
    ).filter(
        AgentChangeSnapshot.task_id.in_(ids),
        AgentChangeSnapshot.is_rolled_back.is_(False),
        ImportedProduct.seller_id == seller_id,
    ).order_by(AgentChangeSnapshot.created_at.desc(), AgentChangeSnapshot.id.desc()).all()
    card_snapshots = CardEditHistory.query.join(
        Product, CardEditHistory.product_id == Product.id,
    ).filter(
        CardEditHistory.user_comment.in_([f'agent_task:{task_id}' for task_id in ids]),
        CardEditHistory.reverted.is_(False),
        CardEditHistory.wb_synced.isnot(True),
        db.or_(
            CardEditHistory.wb_sync_status.is_(None),
            CardEditHistory.wb_sync_status.in_({'pending', 'failed', 'skipped'}),
        ),
        Product.seller_id == seller_id,
    ).order_by(CardEditHistory.created_at.desc(), CardEditHistory.id.desc()).all()

    restored_products = set()
    card_conflicts = []
    now = datetime.utcnow()
    try:
        for snapshot in snapshots:
            previous = json.loads(snapshot.previous_values or '{}')
            product = snapshot.imported_product
            for field, old_value in previous.items():
                if hasattr(product, field):
                    setattr(product, field, old_value)
            product.updated_at = now
            snapshot.is_rolled_back = True
            snapshot.rolled_back_at = now
            restored_products.add(('imported_product', product.id))
        for history in card_snapshots:
            product = history.product
            changed_fields = [
                field for field in (history.changed_fields or [])
                if isinstance(field, str)
                and field in (history.snapshot_after or {})
                and hasattr(product, field)
            ]
            conflicting_fields = [
                field for field in changed_fields
                if getattr(product, field) != history.snapshot_after[field]
            ]
            if conflicting_fields:
                history.wb_sync_status = 'conflict'
                history.wb_error_message = (
                    'Локальный откат заблокирован: карточка была '
                    'изменена после AI-предложения.'
                )
                card_conflicts.append({
                    'product_id': product.id,
                    'history_id': history.id,
                    'fields': conflicting_fields,
                })
                continue
            for field, old_value in (history.snapshot_before or {}).items():
                if hasattr(product, field):
                    setattr(product, field, old_value)
            product.updated_at = now
            history.reverted = True
            history.reverted_at = now
            restored_products.add(('product', product.id))
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return {
        'snapshots': len(snapshots) + len(card_snapshots) - len(card_conflicts),
        'products': len(restored_products),
        'conflicts': len(card_conflicts),
        'conflict_details': card_conflicts[:20],
        'task_ids': ids,
    }


def cancel_run(task_id: str, seller_id: int) -> AgentTask:
    task = AgentTask.query.filter_by(id=task_id, seller_id=seller_id).first()
    if not task:
        raise ValueError('Задача не найдена')
    cancelled_any = False
    for current_id in reversed(task_tree_ids(task.id)):
        cancelled_any = bool(agent_service.cancel_task(current_id)) or cancelled_any
    if not cancelled_any:
        raise ValueError('Задачу уже нельзя остановить')
    return task


def review_proposal(proposal_id: int, seller_id: int, user_id: int,
                    decision: str) -> AgentReviewProposal:
    """Apply or reject a protected price/stock proposal after human review."""
    proposal = AgentReviewProposal.query.filter_by(
        id=proposal_id, seller_id=seller_id, status='pending',
    ).first()
    if not proposal:
        raise ValueError('Предложение не найдено или уже обработано')
    if decision not in {'apply', 'reject'}:
        raise ValueError('Некорректное решение')

    now = datetime.utcnow()
    if decision == 'reject':
        proposal.status = 'rejected'
        proposal.reviewed_by_user_id = user_id
        proposal.reviewed_at = now
        db.session.commit()
        return proposal

    product = ImportedProduct.query.filter_by(
        id=proposal.imported_product_id, seller_id=seller_id,
    ).first()
    if not product:
        raise ValueError('Товар не найден')

    field_aliases = {
        'stock': 'supplier_quantity', 'stocks': 'supplier_quantity',
        'quantity': 'supplier_quantity', 'amount': 'supplier_quantity',
    }
    price_fields = {
        'calculated_price', 'calculated_discount_price',
        'calculated_price_before_discount', 'supplier_price',
    }
    changes = proposal.get_changes()
    previous = {}
    applied = {}
    pricing = PricingSettings.query.filter_by(seller_id=seller_id).first()
    min_margin = pricing.min_profit if pricing and pricing.min_profit is not None else 20.0

    for requested_field, change in changes.items():
        field = field_aliases.get(requested_field, requested_field)
        if not hasattr(product, field):
            continue
        new_value = change.get('new') if isinstance(change, dict) else change
        if field in price_fields:
            try:
                new_value = float(new_value)
            except (TypeError, ValueError):
                raise ValueError(f'Некорректное значение цены: {requested_field}')
            if new_value <= 0:
                raise ValueError('Цена должна быть больше нуля')
            if field != 'supplier_price' and product.supplier_price:
                minimum = product.supplier_price * (1 + float(min_margin) / 100)
                if new_value < minimum:
                    raise ValueError(
                        f'Цена ниже безопасного порога {round(minimum, 2)} руб.'
                    )
        elif field == 'supplier_quantity':
            try:
                new_value = int(new_value)
            except (TypeError, ValueError):
                raise ValueError('Остаток должен быть целым числом')
            if new_value < 0:
                raise ValueError('Остаток не может быть отрицательным')

        old_value = getattr(product, field)
        if str(old_value) == str(new_value):
            continue
        previous[field] = old_value
        applied[field] = new_value
        setattr(product, field, new_value)

    if applied:
        db.session.add(AgentChangeSnapshot(
            task_id=proposal.task_id,
            imported_product_id=product.id,
            previous_values=json.dumps(previous, ensure_ascii=False),
            new_values=json.dumps(applied, ensure_ascii=False),
        ))
        product.updated_at = now
    proposal.status = 'applied'
    proposal.reviewed_by_user_id = user_id
    proposal.reviewed_at = now
    db.session.commit()
    return proposal


def sync_run_message(message: AgentMessage) -> bool:
    task = message.task
    if not task:
        return False
    original = {
        'kind': message.kind, 'content': message.content,
        'task_id': message.task_id, 'metadata': message.get_metadata(),
    }
    metadata = dict(original['metadata'])
    metadata.update({
        'status': task.status,
        'progress_percent': task.progress_percent,
        'current_step_label': task.current_step_label,
        'duration_seconds': task.duration_seconds,
        'error': task.error_message,
        'undo_count': snapshot_count(task.id),
    })
    if metadata.get('phase') == 'planning' and task.status in TERMINAL_TASK_STATUSES:
        result = task.get_result()
        if task.status == 'completed' and result.get('steps'):
            risk = result.get('risk') or 'write'
            requires_approval = risk != 'read'
            original_scope = metadata.get('entity_scope') or {}
            original_kind = str(original_scope.get('kind') or '')
            raw_resolved_ids = result.get('product_ids')
            if (
                isinstance(raw_resolved_ids, list)
                and len(raw_resolved_ids) <= MAX_PRODUCT_IDS
                and all(
                    isinstance(value, int) and not isinstance(value, bool) and value > 0
                    for value in raw_resolved_ids
                )
                and len(set(raw_resolved_ids)) == len(raw_resolved_ids)
            ):
                resolved_ids = list(raw_resolved_ids)
            else:
                resolved_ids = metadata.get('product_ids') or []
            resolved_scope_mode = (
                'global' if result.get('scope_mode') == 'global' else 'active'
            )
            resolved_kind = str(
                result.get('entity_kind')
                or original_kind
                or 'imported_product'
            )
            marketplace_listing_ids = []
            if resolved_scope_mode == 'global':
                resolved_ids = []
                if resolved_kind not in {'product', 'imported_product'}:
                    resolved_kind = 'imported_product'
                resolved_scope = {'kind': resolved_kind, 'ids': []}
            elif resolved_kind == 'marketplace_listing' and original_kind == 'marketplace_listing':
                marketplace_listing_ids = list(
                    metadata.get('marketplace_listing_ids') or []
                )
                resolved_ids = []
                resolved_scope = {
                    **original_scope,
                    'kind': 'marketplace_listing',
                    'ids': marketplace_listing_ids,
                    'scope_mode': 'selected',
                }
            else:
                if resolved_kind not in {'product', 'imported_product'}:
                    resolved_kind = 'imported_product'
                resolved_scope = {'kind': resolved_kind, 'ids': resolved_ids}

            source_message_id = metadata.get('source_message_id')
            conversation_id = getattr(message, 'conversation_id', None)
            if resolved_scope_mode == 'global' and source_message_id and conversation_id:
                source_message = AgentMessage.query.filter_by(
                    id=source_message_id,
                    conversation_id=conversation_id,
                    role='user',
                ).first()
                if source_message:
                    source_metadata = source_message.get_metadata()
                    source_metadata.update({
                        'product_ids': [],
                        'marketplace_listing_ids': [],
                        'scope_origin': 'global',
                        'scope_mode': 'global',
                        'scope_label': 'Весь каталог',
                        'entity_scope': resolved_scope,
                    })
                    source_message.set_metadata(source_metadata)
                    source_message.updated_at = datetime.utcnow()
            message.kind = 'plan'
            message.content = result.get('summary') or 'План готов.'
            message.task_id = None
            message.set_metadata({
                'plan_id': str(uuid.uuid4()),
                'plan_version': HARNESS_PLAN_VERSION,
                'title': result.get('title') or 'План работы',
                'summary': result.get('summary') or '',
                'steps': result.get('steps') or [],
                'execution_type': 'custom',
                'pipeline': None,
                'risk': risk,
                'confidence': result.get('confidence', 0.7),
                'requires_approval': requires_approval,
                'status': 'pending_approval' if requires_approval else 'ready',
                'product_ids': resolved_ids,
                'marketplace_listing_ids': marketplace_listing_ids,
                'scope_label': result.get('scope_label') or 'Область определена из запроса',
                'scope_mode': resolved_scope_mode,
                'model_policy': metadata.get('model_policy') or {},
                'request_text': metadata.get('request_text') or '',
                'page_context': metadata.get('page_context') or {},
                'entity_scope': resolved_scope,
                'planning_task_id': task.id,
                'planning_usage': result.get('_usage') or {},
                'auto_started': not requires_approval,
            })
            if not requires_approval:
                try:
                    _create_run_from_plan(message.conversation, message)
                except RuntimeError as exc:
                    plan_metadata = message.get_metadata()
                    plan_metadata['status'] = 'runtime_unavailable'
                    plan_metadata['runtime_error'] = str(exc)
                    message.set_metadata(plan_metadata)
        else:
            message.kind = 'clarification'
            message.content = (
                result.get('clarification_question')
                or task.error_message
                or 'Не удалось построить безопасный план. Уточните цель и область товаров.'
            )
            message.task_id = None
            message.set_metadata({'reason': 'planner_clarification', 'planning_task_id': task.id})
        message.updated_at = datetime.utcnow()
        return True
    if task.status in TERMINAL_TASK_STATUSES:
        result = task.get_result()
        metadata['result'] = result
        if task.status == 'completed':
            message.content = result.get('message') or 'Задача завершена.'
        elif task.status == 'failed':
            message.content = task.error_message or 'Задача завершилась с ошибкой.'
        else:
            message.content = 'Задача остановлена.'
    changed = (
        original['kind'] != message.kind
        or original['content'] != message.content
        or original['task_id'] != message.task_id
        or original['metadata'] != metadata
    )
    if original['metadata'] != metadata:
        message.set_metadata(metadata)
    if changed:
        message.updated_at = datetime.utcnow()
    return changed


def conversation_payload(conversation: AgentConversation, message_limit: int = 100,
                         step_after: int = 0) -> dict:
    def latest_run_messages():
        return AgentMessage.query.filter_by(
            conversation_id=conversation.id, kind='run',
        ).order_by(AgentMessage.created_at.desc()).limit(1).all()

    run_messages = latest_run_messages()
    changed = False
    for message in run_messages:
        changed = sync_run_message(message) or changed
    if changed:
        db.session.commit()
        # A completed semantic read-plan may have auto-created its execution
        # run while replacing the planning message. Re-query so this response
        # keeps UI polling alive without one empty cycle.
        run_messages = latest_run_messages()
    else:
        run_messages = [message for message in run_messages if message.kind == 'run']

    messages = list(reversed(
        AgentMessage.query.filter_by(conversation_id=conversation.id).order_by(
            AgentMessage.created_at.desc(), AgentMessage.id.desc(),
        ).limit(min(message_limit, 200)).all()
    ))

    active_run = run_messages[0] if run_messages else None
    task_payload = None
    steps = []
    subtasks = []
    proposals = []
    if active_run and active_run.task:
        task = active_run.task
        task_payload = task.to_dict()
        task_ids = task_tree_ids(task.id)
        steps = AgentTaskStep.query.filter(
            AgentTaskStep.task_id.in_(task_ids), AgentTaskStep.id > max(step_after, 0),
        ).order_by(AgentTaskStep.id.asc()).limit(100).all()
        subtasks = AgentTask.query.filter_by(parent_task_id=task.id).order_by(
            AgentTask.created_at.asc(),
        ).all()
        proposals = AgentReviewProposal.query.filter(
            AgentReviewProposal.task_id.in_(task_ids),
        ).order_by(AgentReviewProposal.created_at.desc()).limit(100).all()

    active_scope = _latest_conversation_scope(conversation)
    return {
        'conversation': conversation.to_dict(),
        'messages': [message.to_dict() for message in messages],
        'active_scope': active_scope,
        'run': task_payload,
        'steps': [step.to_dict() for step in steps],
        'subtasks': [task.to_dict() for task in subtasks],
        'proposals': [proposal.to_dict() for proposal in proposals],
        'last_step_id': steps[-1].id if steps else step_after,
    }
