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
    # Полный импорт: бренд + категория + характеристики + размеры + SEO + модерация
    'import_ready': {
        'label': 'Полный импорт',
        'description': 'Бренд → Категория → Характеристики → Размеры → SEO → Модерация',
        'steps': [
            {'agent': 'brand-resolver', 'task_type': 'resolve_batch',
             'label': 'Определение брендов'},
            {'agent': 'category-mapper', 'task_type': 'map_batch',
             'label': 'Определение категорий'},
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

    def get_tools(self):
        """Возвращает инструменты оркестратора."""
        return create_orchestrator_tools(self.platform)

    system_prompt = """Ты — оркестратор задач для платформы WB-селлеров.

Твоя роль — координировать работу специализированных агентов.
Ты НЕ выполняешь задачи сам — ты создаёшь подзадачи для других агентов
и следишь за их выполнением.

Доступные агенты:
- category-mapper: определение категорий WB
- characteristics-filler: заполнение характеристик
- seo-writer: SEO-оптимизация заголовков и описаний
- brand-resolver: нормализация брендов
- size-normalizer: нормализация размеров
- card-doctor: проверка на стоп-слова и модерацию
- price-optimizer: оптимизация цен
- review-analyst: анализ отзывов

Порядок работы:
1. Создай подзадачу через create_subtask (ОБЯЗАТЕЛЬНО передавай parent_task_id!)
2. Проверь статус через get_subtask_status
3. Когда подзадача завершена — получи результат через get_subtask_result
4. Создай следующую подзадачу (если есть)
5. Когда все шаги выполнены — верни итоговый JSON

ВАЖНО: Выполняй шаги ПОСЛЕДОВАТЕЛЬНО. Жди завершения текущего шага
перед запуском следующего.
ВАЖНО: Всегда передавай parent_task_id при создании подзадач."""

    def build_task_prompt(self, task: dict) -> str:
        input_data = self.parse_input_data(task)

        task_type = task.get('task_type', 'pipeline')
        seller_id = task.get('seller_id')
        parent_task_id = task.get('id', '')
        product_ids = input_data.get('product_ids', [])
        is_batch = len(product_ids) > 1

        # Определяем шаги pipeline
        if task_type == 'pipeline':
            pipeline_name = input_data.get('pipeline', 'full_prepare')
            pipeline = PIPELINES.get(pipeline_name)
            if not pipeline:
                return f"Ошибка: неизвестный pipeline '{pipeline_name}'.\n\nВерни JSON: {{error: 'unknown_pipeline'}}"
            steps = pipeline['steps']
            label = pipeline['label']

        elif task_type == 'smart':
            user_text = input_data.get('text', '')
            steps = resolve_agents_from_text(user_text, is_batch)
            label = f'Умный запрос: {user_text[:50]}'

        elif task_type == 'custom':
            # Пользователь сам выбрал агентов
            steps = input_data.get('steps', [])
            if not steps:
                return "Ошибка: не указаны шаги.\n\nВерни JSON: {error: 'no_steps'}"
            label = 'Пользовательский pipeline'

        else:
            return (
                f"Неизвестный тип задачи: {task_type}.\n\n"
                f"Верни JSON: {{error: 'unknown_task_type'}}"
            )

        # Формируем prompt с конкретными шагами
        ids_str = ', '.join(str(i) for i in product_ids[:20]) if product_ids else 'все'
        steps_description = '\n'.join(
            f"  {i+1}. [{s['agent']}] {s.get('label', s['task_type'])}"
            for i, s in enumerate(steps)
        )

        input_json = json.dumps({
            'product_ids': product_ids,
            'seller_id': seller_id,
        }, ensure_ascii=False)

        return (
            f"Pipeline: {label}\n"
            f"Seller ID: {seller_id}\n"
            f"Parent Task ID: {parent_task_id}\n"
            f"Товары: {ids_str}\n"
            f"Всего товаров: {len(product_ids) if product_ids else 'определить автоматически'}\n\n"
            f"Шаги:\n{steps_description}\n\n"
            f"Для КАЖДОГО шага по порядку:\n"
            f"1. Создай подзадачу: create_subtask(\n"
            f"     agent_name='...',\n"
            f"     task_type='...',\n"
            f"     seller_id={seller_id},\n"
            f"     title='...',\n"
            f"     input_data={input_json},\n"
            f"     parent_task_id='{parent_task_id}'\n"
            f"   )\n"
            f"2. Дождись завершения: вызывай get_subtask_status(task_id=...) пока статус не станет 'completed' или 'failed'\n"
            f"3. Получи результат: get_subtask_result(task_id=...)\n"
            f"4. Переходи к следующему шагу\n\n"
            f"ВАЖНО:\n"
            f"- Выполняй шаги СТРОГО ПО ПОРЯДКУ\n"
            f"- НЕ запускай следующий шаг пока текущий не завершён\n"
            f"- Если шаг завершился с ошибкой — продолжай со следующим, но отметь ошибку\n"
            f"- ОБЯЗАТЕЛЬНО передавай parent_task_id='{parent_task_id}' в каждую подзадачу\n\n"
            f"Верни итоговый JSON:\n"
            f"{{\n"
            f"  pipeline: '{label}',\n"
            f"  total_steps: {len(steps)},\n"
            f"  completed: число,\n"
            f"  failed: число,\n"
            f"  results: [{{step: 'название', agent: 'имя', status: 'completed'|'failed', summary: '...'}}]\n"
            f"}}"
        )
