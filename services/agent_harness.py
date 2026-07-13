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
    Product,
    PricingSettings,
    ServiceAgent,
)
from services import agent_service


MAX_MESSAGE_LENGTH = 4000
MAX_PRODUCT_IDS = 500
HARNESS_PLAN_VERSION = 2
TERMINAL_TASK_STATUSES = {'completed', 'failed', 'cancelled'}
ACTIVE_TASK_STATUSES = {'queued', 'running'}
PAGE_CONTEXT_ENTITY_KEYS = {
    'product_id', 'seller_id', 'supplier_id', 'item_id', 'nm_id',
    'subject_id', 'brand_id', 'marketplace_id', 'factory_id',
    'account_id', 'category_id', 'task_id',
}

DIRECT_HELP_PATTERNS = (
    'что ты умеешь', 'что умеешь', 'твои возможности', 'как ты работаешь',
    'помощь', 'help',
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
    return {
        'single_model': bool(single_model),
        'provider': provider,
        'primary_model': primary_model,
        'fast_model': primary_model if single_model else 'deepseek-v4-flash',
        'write_model': primary_model,
    }


def direct_response(text: str) -> Optional[str]:
    normalized = text.strip().lower()
    if normalized in {'привет', 'здравствуй', 'hello', 'hi'}:
        return (
            'Здравствуйте. Опишите результат, который нужен: я соберу контекст, '
            'покажу план и попрошу подтверждение перед изменением данных.'
        )
    if _contains_any(normalized, DIRECT_HELP_PATTERNS):
        return (
            'Я работаю с карточками как единый помощник: готовлю товары к WB, '
            'улучшаю SEO и характеристики, проверяю категории, бренды, размеры, '
            'цены, фотографии, модерацию и отзывы. Перед изменениями показываю '
            'план, а после выполнения сохраняю возможность отката.'
        )
    return None


def _conversation_usage_response(conversation: AgentConversation, text: str) -> Optional[str]:
    normalized = text.lower()
    if not _contains_any(normalized, (
        'сколько токен', 'расход токен', 'api запрос', 'апи запрос',
        'сколько стоил', 'стоимость запуск', 'расход последн',
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
    normalized = text.lower()
    return (
        len(text) > 140
        or sum(1 for token in ('выбер', 'отбери', 'если', 'котор', 'из них', 'сначала', 'затем') if token in normalized) >= 2
        or bool(_extract_named_scope(normalized))
    )


def _is_no_write_request(text: str) -> bool:
    return _is_global_no_write_request(text)


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
    return 'imported_product'


def _resolve_entity_kind(value, page_context: dict = None) -> str:
    """Resolve an explicit typed scope, falling back to trusted page shape."""
    if value in (None, ''):
        return _page_entity_kind(page_context)
    normalized = str(value).strip().lower()
    if normalized not in {'product', 'imported_product'}:
        raise ValueError('entity_kind must be product or imported_product')
    return normalized


def _parse_number(value: str) -> float:
    return float(re.sub(r'\s+', '', value).replace(',', '.'))


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

    if not labels and not _contains_any(text, (
        'сколько всего', 'общее количество', 'всего товар', 'всего карточ',
        'покажи товар', 'покажи карточ', 'список товар', 'список карточ',
        'сколько импортирован', 'покажи импортирован', 'список импортирован',
    )):
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


def build_plan(text: str, product_ids=None, page_context=None,
               entity_kind=None) -> Optional[HarnessPlan]:
    """Build a conservative deterministic plan without spending LLM tokens."""
    normalized = text.strip().lower()
    if not normalized:
        return None

    selected_ids = _normalize_product_ids(product_ids)
    entity_kind = _resolve_entity_kind(entity_kind, page_context)
    named_scope = _extract_named_scope(normalized)
    if selected_ids and entity_kind == 'unsupported':
        return None
    if selected_ids and _contains_any(normalized, (
        'аудит', 'проверь выбран', 'проверить выбран', 'основные проблемы',
        'проблемы карточ', 'ошибки карточ',
    )):
        return HarnessPlan(
            title=f'Аудит выбранных карточек ({len(selected_ids)})',
            summary=(
                'Проверить выбранные карточки одним пакетным запросом по локальным '
                'правилам, без вызова LLM и без изменения данных.'
            ),
            steps=[{
                'agent': 'batch-audit', 'task_type': 'audit_selection',
                'label': f'Проверка {len(selected_ids)} карточек',
                'params': {'entity_kind': entity_kind, 'focus_limit': 100},
            }],
            execution_type='custom', pipeline=None,
            risk='read', confidence=0.99,
            scope_label=f'{len(selected_ids)} выбранных карточек',
        )
    if selected_ids and _contains_any(normalized, (
        'что можешь сказать', 'что скажешь', 'проанализируй карточ',
        'оцени карточ', 'как тебе карточ',
    )):
        return HarnessPlan(
            title='Анализ выбранной карточки',
            summary='Проверить содержимое карточки и вернуть сильные стороны, проблемы и следующие действия.',
            steps=[{
                'agent': 'card-insight', 'task_type': 'analyze_card',
                'label': 'Анализ карточки',
                'params': {'entity_kind': entity_kind},
            }],
            execution_type='custom', pipeline=None,
            risk='read', confidence=0.99,
            scope_label=f'Карточка #{selected_ids[0]} на текущей странице',
        )

    content_fields = extract_explicit_content_fields(normalized)
    if selected_ids and content_fields and not _is_global_no_write_request(normalized) and _contains_any(
        normalized, ('улучш', 'перепиш', 'обнов', 'сделай', 'исправь', 'оптимиз'),
    ):
        fields_label = content_fields_label(content_fields)
        selection_label = (
            f'карточки #{selected_ids[0]}'
            if len(selected_ids) == 1
            else f'{len(selected_ids)} выбранных карточек'
        )
        return HarnessPlan(
            title=f'Улучшить {fields_label}: {selection_label}',
            summary=(
                f'Переписать только запрошенные поля: {fields_label}; '
                'проверить стоп-слова и сохранить проверяемый diff.'
            ),
            steps=[{
                'agent': 'content-writer', 'task_type': 'rewrite_content',
                'label': f'Новые {fields_label}',
                'params': {
                    'entity_kind': entity_kind,
                    'fields': content_fields,
                    'instruction': text[:500],
                },
            }],
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
    if selected_ids and entity_kind == 'product':
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

    if _contains_any(normalized, ('подготов', 'импорт', 'к публикац', 'к wb', 'на wb')):
        pipeline = PIPELINES['full_prepare']
        return HarnessPlan(
            title='Подготовить товары к WB',
            summary=pipeline['description'],
            steps=pipeline['steps'], execution_type='pipeline', pipeline='full_prepare',
            risk='write', confidence=0.96,
        )

    if _contains_any(normalized, ('seo', 'сео', 'заголов', 'описани', 'ключев')):
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

    if _contains_any(normalized, ('категори', 'subject', 'предмет wb')):
        pipeline = PIPELINES['category_fix']
        return HarnessPlan(
            title='Исправить категории и характеристики', summary=pipeline['description'],
            steps=pipeline['steps'], execution_type='pipeline', pipeline='category_fix',
            risk='write', confidence=0.93,
        )

    matched = [spec for spec in SKILLS.values() if _contains_any(normalized, spec['keywords'])]
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

    input_data = {
        'seller_id': conversation.seller_id,
        'product_ids': metadata.get('product_ids') or [],
        'imported_product_ids': metadata.get('product_ids') or [],
        'model_policy': metadata.get('model_policy') or get_model_policy(conversation.seller_id),
        'source': 'unified_chat',
        'conversation_id': conversation.id,
        'plan_id': metadata.get('plan_id'),
        'risk': metadata.get('risk', 'write'),
        'text': metadata.get('request_text', ''),
        'page_context': metadata.get('page_context') or {},
        'entity_scope': metadata.get('entity_scope') or {
            'kind': 'imported_product',
            'ids': metadata.get('product_ids') or [],
        },
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
                         entity_kind: str = None) -> AgentMessage:
    state = runtime_state()
    if not state['online']:
        raise RuntimeError('ИИ-помощник не подключён')
    model_policy = get_model_policy(conversation.seller_id)
    resolved_kind = _resolve_entity_kind(entity_kind, page_context)
    entity_scope = {'kind': resolved_kind, 'ids': product_ids}
    task = agent_service.create_task(
        agent_id=state['orchestrator_id'], seller_id=conversation.seller_id,
        task_type='plan_request', title=f'Планирование: {text[:90]}',
        input_data={
            'seller_id': conversation.seller_id,
            'text': text,
            'product_ids': product_ids,
            'page_context': page_context or {},
            'entity_scope': entity_scope,
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
            'page_context': page_context or {},
            'entity_scope': entity_scope,
            'model_policy': model_policy,
        },
    )


def submit_turn(conversation: AgentConversation, text: str,
                product_ids=None, page_context=None, entity_kind=None) -> dict:
    text = (text or '').strip()
    if not text:
        raise ValueError('Введите сообщение')
    if len(text) > MAX_MESSAGE_LENGTH:
        raise ValueError(f'Сообщение длиннее {MAX_MESSAGE_LENGTH} символов')
    ids = _normalize_product_ids(product_ids)
    context = _normalize_page_context(page_context)
    resolved_kind = _resolve_entity_kind(entity_kind, context)

    if context and ids:
        previous = AgentMessage.query.filter_by(
            conversation_id=conversation.id, role='user',
        ).order_by(AgentMessage.created_at.desc()).first()
        if previous:
            previous_metadata = previous.get_metadata()
            previous_ids = _normalize_product_ids(previous_metadata.get('product_ids'))
            previous_context = previous_metadata.get('page_context') or {}
            scope_changed = set(previous_ids) != set(ids) or (
                (previous_metadata.get('entity_scope') or {}).get('kind', _page_entity_kind(previous_context))
                != resolved_kind
            )
            if previous_context and previous_ids and scope_changed:
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
            'scope_label': f'{len(ids)} товаров' if ids else 'Весь каталог',
            'page_context': context,
            'entity_scope': {'kind': resolved_kind, 'ids': ids},
        },
    )
    if conversation.title == 'Новый диалог':
        conversation.title = re.sub(r'\s+', ' ', text)[:72]

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
    plan = build_plan(text, ids, context, entity_kind)
    if plan is None and ids and resolved_kind in {'product', 'unsupported'}:
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
    if plan is None and _is_no_write_request(text):
        assistant = _new_message(
            conversation, 'assistant',
            'Режим без изменений принят. Уточните область анализа: выбранные ID, '
            'поставщика или конкретный фильтр вроде «карточки без описания».',
            kind='clarification', metadata={'reason': 'read_scope_required'},
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
    metadata['entity_scope'] = {
        'kind': resolved_kind,
        'ids': ids,
    }
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
        CardEditHistory.reverted.is_(False), Product.seller_id == seller_id,
    ).order_by(CardEditHistory.created_at.desc(), CardEditHistory.id.desc()).all()

    restored_products = set()
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
        'snapshots': len(snapshots) + len(card_snapshots),
        'products': len(restored_products), 'task_ids': ids,
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
                'risk': result.get('risk') or 'write',
                'confidence': result.get('confidence', 0.7),
                'requires_approval': True,
                'status': 'pending_approval',
                'product_ids': metadata.get('product_ids') or [],
                'scope_label': result.get('scope_label') or 'Область определена из запроса',
                'model_policy': metadata.get('model_policy') or {},
                'request_text': metadata.get('request_text') or '',
                'page_context': metadata.get('page_context') or {},
                'entity_scope': metadata.get('entity_scope') or {
                    'kind': _page_entity_kind(metadata.get('page_context') or {}),
                    'ids': metadata.get('product_ids') or [],
                },
                'planning_task_id': task.id,
            })
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
    run_messages = AgentMessage.query.filter_by(
        conversation_id=conversation.id, kind='run',
    ).order_by(AgentMessage.created_at.desc()).limit(1).all()
    changed = False
    for message in run_messages:
        changed = sync_run_message(message) or changed
    run_messages = [message for message in run_messages if message.kind == 'run']
    if changed:
        db.session.commit()

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

    return {
        'conversation': conversation.to_dict(),
        'messages': [message.to_dict() for message in messages],
        'run': task_payload,
        'steps': [step.to_dict() for step in steps],
        'subtasks': [task.to_dict() for task in subtasks],
        'proposals': [proposal.to_dict() for proposal in proposals],
        'last_step_id': steps[-1].id if steps else step_after,
    }
