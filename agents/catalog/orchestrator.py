# -*- coding: utf-8 -*-
"""
Оркестратор — мета-агент, который разбивает задачу на подзадачи
и делегирует их специализированным агентам.

Работает БЕЗ LLM для роутинга — используется rule-based маппинг
пресетов (pipelines) и keyword matching для свободного текста.
LLM используется только если нужно уточнить неоднозначный запрос.

Задачи:
  - pipeline:    запуск готового pipeline (full_prepare, seo_boost, audit, ...)
  - smart:       свободный текст — оркестратор сам выбирает агентов
  - custom:      пользователь выбрал конкретных агентов
"""
import json
import logging
import time
from typing import Optional

from ..base_agent import BaseAgent
from ..tools import create_orchestrator_tools

logger = logging.getLogger(__name__)


# ── Готовые pipelines ──────────────────────────────────────────────

PIPELINES = {
    # Полная подготовка товара к WB
    'full_prepare': {
        'label': 'Подготовить к WB',
        'description': 'Категория → Характеристики → SEO → Модерация',
        'steps': [
            {'agent': 'category-mapper', 'task_type': 'map_batch',
             'label': 'Определение категорий'},
            {'agent': 'characteristics-filler', 'task_type': 'fill_batch',
             'label': 'Заполнение характеристик'},
            {'agent': 'seo-writer', 'task_type': 'seo_batch',
             'label': 'SEO-оптимизация'},
            {'agent': 'card-doctor', 'task_type': 'diagnose_batch',
             'label': 'Проверка модерации'},
        ],
    },
    # Полный импорт: категория → бренд → характеристики → размеры → SEO → модерация
    # ВАЖНО: категория ПЕРЕД брендом — бренд валидируется в контексте категории (WB subject_id)
    'import_ready': {
        'label': 'Полный импорт',
        'description': 'Категория → Бренд → Характеристики → Размеры → SEO → Модерация',
        'steps': [
            {'agent': 'category-mapper', 'task_type': 'map_batch',
             'label': 'Определение категорий'},
            {'agent': 'brand-resolver', 'task_type': 'resolve_batch',
             'label': 'Определение брендов'},
            {'agent': 'characteristics-filler', 'task_type': 'fill_batch',
             'label': 'Заполнение характеристик'},
            {'agent': 'size-normalizer', 'task_type': 'normalize_batch',
             'label': 'Нормализация размеров'},
            {'agent': 'seo-writer', 'task_type': 'seo_batch',
             'label': 'SEO-оптимизация'},
            {'agent': 'card-doctor', 'task_type': 'diagnose_batch',
             'label': 'Проверка модерации'},
        ],
    },
    # Только SEO тексты + проверка
    'seo_boost': {
        'label': 'SEO тексты',
        'description': 'SEO-оптимизация → Проверка модерации',
        'steps': [
            {'agent': 'seo-writer', 'task_type': 'seo_batch',
             'label': 'SEO-оптимизация'},
            {'agent': 'card-doctor', 'task_type': 'diagnose_batch',
             'label': 'Проверка модерации'},
        ],
    },
    # Аудит карточек
    'audit': {
        'label': 'Аудит карточек',
        'description': 'Модерация → Цены → Отзывы',
        'steps': [
            {'agent': 'card-doctor', 'task_type': 'preventive_scan',
             'label': 'Проверка модерации'},
            {'agent': 'price-optimizer', 'task_type': 'margin_audit',
             'label': 'Аудит цен'},
            {'agent': 'review-analyst', 'task_type': 'analyze_reviews',
             'label': 'Анализ отзывов'},
        ],
    },
    # Исправление категорий + характеристик
    'category_fix': {
        'label': 'Исправить категории',
        'description': 'Категория → Характеристики',
        'steps': [
            {'agent': 'category-mapper', 'task_type': 'map_batch',
             'label': 'Определение категорий'},
            {'agent': 'characteristics-filler', 'task_type': 'fill_batch',
             'label': 'Заполнение характеристик'},
        ],
    },
}

# ── Keyword → agent маппинг для свободного текста ───────────────

KEYWORD_AGENTS = [
    # (keywords, agent_name, task_type_single, task_type_batch)
    (['категори', 'subject', 'предмет'], 'category-mapper', 'map_single', 'map_batch'),
    (['seo', 'сео', 'заголов', 'описани', 'текст'], 'seo-writer', 'seo_single', 'seo_batch'),
    (['бренд', 'brand', 'марк'], 'brand-resolver', 'resolve_single', 'resolve_batch'),
    (['размер', 'size', 'габарит', 'вес'], 'size-normalizer', 'normalize_single', 'normalize_batch'),
    (['характерист', 'заполн', 'атрибут'], 'characteristics-filler', 'fill_single', 'fill_batch'),
    (['цен', 'price', 'марж', 'экономик'], 'price-optimizer', 'optimize_prices', 'optimize_prices'),
    (['модерац', 'блокировк', 'стоп-слов', 'провер'], 'card-doctor', 'diagnose_single', 'diagnose_batch'),
    (['отзыв', 'review', 'рейтинг'], 'review-analyst', 'product_insights', 'analyze_reviews'),
    (['импорт', 'загруз', 'подготов'], None, None, None),  # → pipeline full_prepare
]

# keyword → pipeline (для запросов типа "подготовь к WB")
KEYWORD_PIPELINES = [
    (['подготов', 'импорт', 'загруз', 'полн'], 'full_prepare'),
    (['seo', 'сео', 'текст'], 'seo_boost'),
    (['аудит', 'провер', 'скан'], 'audit'),
    (['категори', 'маппинг'], 'category_fix'),
]


def resolve_agents_from_text(text: str, is_batch: bool = False) -> list[dict]:
    """
    Определяет нужных агентов по свободному тексту.
    Возвращает список {agent, task_type, label}.
    """
    text_lower = text.lower()

    # Сначала проверяем, не подходит ли целый pipeline
    for keywords, pipeline_name in KEYWORD_PIPELINES:
        if any(kw in text_lower for kw in keywords):
            pipeline = PIPELINES.get(pipeline_name)
            if pipeline:
                return pipeline['steps']

    # Иначе собираем отдельных агентов по ключевым словам
    agents = []
    seen = set()
    for keywords, agent_name, single_type, batch_type in KEYWORD_AGENTS:
        if agent_name and agent_name not in seen:
            if any(kw in text_lower for kw in keywords):
                task_type = batch_type if is_batch else single_type
                agents.append({
                    'agent': agent_name,
                    'task_type': task_type,
                    'label': agent_name,
                })
                seen.add(agent_name)

    # Если ничего не нашли — pipeline full_prepare по умолчанию
    if not agents:
        return PIPELINES['full_prepare']['steps']

    return agents


class OrchestratorAgent(BaseAgent):
    """
    Мета-агент оркестратор.

    НЕ использует LLM для роутинга — вся логика на правилах.
    Создаёт подзадачи через Internal API и ждёт их завершения.
    """
    agent_name = 'orchestrator'
    max_iterations = 60  # Нужно больше итераций — ждём подзадачи
    use_fallback_llm = True  # Нужен точный LLM для координации

    # Оркестратору не нужны product-level tools — он работает через подзадачи
    excluded_tools = (
        'get_product', 'update_product',
        'get_imported_product', 'update_imported_product',
        'batch_update_imported_products',
    )

    # Хранилище product_ids для текущей задачи (не пихаем в промпт)
    _current_product_ids: list = []
    _current_seller_id: int = None

    def get_tools(self):
        """Возвращает инструменты оркестратора с авто-подстановкой product_ids."""
        tools = create_orchestrator_tools(self.platform)

        # Оборачиваем create_subtask — автоматически inject product_ids
        original_handler = tools._handlers['create_subtask']

        def _wrapped_create_subtask(**kwargs):
            input_data = kwargs.get('input_data', '{}')
            if isinstance(input_data, str):
                try:
                    parsed = json.loads(input_data)
                except (json.JSONDecodeError, ValueError):
                    parsed = {}
            else:
                parsed = input_data

            # Если product_ids не переданы — подставляем из текущей задачи
            if not parsed.get('product_ids') and self._current_product_ids:
                parsed['product_ids'] = self._current_product_ids
                parsed['imported_product_ids'] = self._current_product_ids
            if not parsed.get('seller_id') and self._current_seller_id:
                parsed['seller_id'] = self._current_seller_id

            kwargs['input_data'] = json.dumps(parsed, ensure_ascii=False)
            return original_handler(**kwargs)

        tools._handlers['create_subtask'] = _wrapped_create_subtask
        return tools

    system_prompt = """Ты — оркестратор задач для платформы WB-селлеров.
Координируй работу агентов: создавай подзадачи и жди их завершения."""

    def execute_task(self, task: dict) -> dict:
        """Перед запуском ReAct — загружаем product_ids если они пустые."""
        input_data = self.parse_input_data(task)
        product_ids = input_data.get('product_ids') or input_data.get('imported_product_ids') or []
        seller_id = task.get('seller_id')

        if not product_ids and seller_id:
            logger.info(f"Orchestrator: product_ids empty, auto-fetching for seller {seller_id}")
            all_ids = []
            page = 1
            while True:
                try:
                    resp = self.platform.list_imported_products(seller_id, page=page, per_page=200)
                    products = resp.get('products', [])
                    if not products:
                        break
                    all_ids.extend(p['id'] for p in products)
                    total = resp.get('total', len(products))
                    if len(all_ids) >= total:
                        break
                    page += 1
                except Exception as e:
                    logger.warning(f"Orchestrator: failed to fetch products page {page}: {e}")
                    break

            if all_ids:
                logger.info(f"Orchestrator: auto-fetched {len(all_ids)} product IDs")
                product_ids = all_ids
            else:
                logger.warning(f"Orchestrator: no products found for seller {seller_id}")

        self._current_product_ids = product_ids
        self._current_seller_id = seller_id

        # Определяем pipeline
        task_type = task.get('task_type', 'pipeline')
        if task_type == 'pipeline':
            pipeline_name = input_data.get('pipeline', 'full_prepare')
            pipeline = PIPELINES.get(pipeline_name)
            if pipeline:
                return self._execute_pipeline(task, pipeline, product_ids, seller_id)

        elif task_type == 'smart':
            user_text = input_data.get('text', '')
            steps = resolve_agents_from_text(user_text, len(product_ids) > 1)
            pseudo_pipeline = {'label': f'Умный запрос: {user_text[:50]}', 'steps': steps}
            return self._execute_pipeline(task, pseudo_pipeline, product_ids, seller_id)

        elif task_type == 'custom':
            steps = input_data.get('steps', [])
            if steps:
                pseudo_pipeline = {'label': 'Пользовательский pipeline', 'steps': steps}
                return self._execute_pipeline(task, pseudo_pipeline, product_ids, seller_id)

        # Fallback: для нестандартных задач — ReAct
        input_data['product_count'] = len(product_ids)
        input_data.pop('product_ids', None)
        input_data.pop('imported_product_ids', None)
        task['input_data'] = json.dumps(input_data, ensure_ascii=False)
        return self._execute_react(task)

    def _execute_pipeline(self, task: dict, pipeline: dict,
                          product_ids: list, seller_id: int) -> dict:
        """Выполняет pipeline БЕЗ LLM — чистый Python.

        Создаёт подзадачи последовательно, ждёт завершения каждой.
        Экономит токены: 0 LLM-вызовов для оркестрации.
        """
        task_id = task['id']
        steps = pipeline['steps']
        label = pipeline['label']
        total = len(steps)
        results = []

        self.platform.log_thinking(
            task_id, f'Pipeline: {label}',
            f'{total} шагов, {len(product_ids)} товаров',
        )
        self.platform.update_progress(task_id, 0, f'Запуск: {label}', total)

        for idx, step in enumerate(steps):
            agent_name = step['agent']
            task_type = step['task_type']
            step_label = step.get('label', agent_name)

            self.platform.update_progress(
                task_id, idx, f'Шаг {idx + 1}/{total}: {step_label}',
            )
            self.platform.log_action(
                task_id, f'Запускаю: {step_label}',
                f'Агент: {agent_name}, тип: {task_type}',
            )

            # Создаём подзадачу
            try:
                input_data = {
                    'seller_id': seller_id,
                    'product_ids': product_ids,
                    'imported_product_ids': product_ids,
                }
                resp = self.platform.create_subtask(
                    agent_name=agent_name,
                    task_type=task_type,
                    seller_id=seller_id,
                    title=f'{step_label} ({len(product_ids)} товаров)',
                    input_data=input_data,
                    parent_task_id=task_id,
                )
                subtask_id = resp.get('task', {}).get('id')
                if not subtask_id:
                    raise ValueError(f"No task ID in response: {resp}")
            except Exception as e:
                logger.error(f"Orchestrator: failed to create subtask {agent_name}: {e}")
                results.append({
                    'step': step_label, 'agent': agent_name,
                    'status': 'failed', 'summary': f'Ошибка создания: {str(e)[:200]}',
                })
                continue

            # Ждём завершения (Python polling, без LLM)
            subtask_result = self.wait_for_subtask(
                subtask_id, timeout=1800, poll_interval=5,
            )

            # Анализируем результат
            task_data = subtask_result.get('task', subtask_result)
            status = task_data.get('status', 'unknown')
            duration = task_data.get('duration_seconds', 0)
            error = task_data.get('error_message', '')

            if status == 'completed':
                summary = f'Завершено за {duration}с'
                self.platform.log_result(task_id, f'{step_label}: OK', summary)
            else:
                summary = f'Статус: {status}. {error[:200]}' if error else f'Статус: {status}'
                self.platform.log_error(task_id, f'{step_label}: {status}', summary)

            results.append({
                'step': step_label,
                'agent': agent_name,
                'status': status,
                'summary': summary,
            })

        completed = sum(1 for r in results if r['status'] == 'completed')
        failed = total - completed

        self.platform.update_progress(task_id, total, 'Pipeline завершён')

        return {
            'pipeline': label,
            'total_steps': total,
            'completed': completed,
            'failed': failed,
            'results': results,
        }

    def build_task_prompt(self, task: dict) -> str:
        """Fallback prompt для нестандартных задач (используется только в ReAct mode)."""
        input_data = self.parse_input_data(task)
        seller_id = task.get('seller_id')
        parent_task_id = task.get('id', '')
        product_count = input_data.get('product_count', 0)

        return (
            f"Seller ID: {seller_id}\n"
            f"Parent Task ID: {parent_task_id}\n"
            f"Всего товаров: {product_count}\n\n"
            f"Задача: {input_data.get('text', 'выполни задачу')}\n\n"
            f"Создай подзадачи через create_subtask. "
            f"product_ids подставляются автоматически — передавай только seller_id в input_data.\n"
            f"Верни JSON с результатами."
        )
