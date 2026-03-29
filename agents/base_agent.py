# -*- coding: utf-8 -*-
"""
Базовый агент с ADK-паттернами.

Реализует:
- ReAct loop (Reason → Act → Observe)
- Автоматическое логирование шагов в платформу
- Tool calling через LLM (Gemini / Claude)
- Heartbeat в фоне + liveness file для Docker healthcheck
- Graceful shutdown
- Защита от переполнения контекста LLM
- Пропуск задач, которые уже провалились слишком много раз
- Cancel propagation — проверка отмены задачи на лету
- Robust JSON parsing из ответов LLM
"""
import json
import logging
import os
import re
import signal
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from .config import AgentConfig
from .llm import BaseLLM, create_llm, create_fallback_llm, create_step_namer_llm
from .platform_client import PlatformClient
from .tools import ToolRegistry, create_platform_tools

logger = logging.getLogger(__name__)

# Файл liveness для Docker healthcheck
LIVENESS_FILE = Path('/tmp/agent-alive')

# Примерный лимит символов контекста перед сжатием
# (грубая оценка: ~4 символа ≈ 1 токен, лимит ~80k токенов → ~300k символов,
#  оставляем запас для системного промпта и ответа)
CONTEXT_CHAR_LIMIT = 120_000

# Макс. число провалов задачи перед пропуском (dead letter protection)
MAX_TASK_FAILURES = 3

# Максимальная длина сообщения об ошибке для платформы
MAX_ERROR_LENGTH = 500

# Интервал проверки отмены задачи (каждые N итераций ReAct)
CANCEL_CHECK_INTERVAL = 3


# ── Креативные названия шагов ──────────────────────────────────────

# Статический маппинг tool_name → пара (action_label, result_label)
TOOL_LABELS = {
    'get_products': ('Загружаю каталог товаров', 'Каталог получен'),
    'get_product': ('Открываю карточку товара', 'Карточка загружена'),
    'update_product': ('Обновляю карточку товара', 'Карточка обновлена'),
    'get_imported_products': ('Ищу товары поставщика', 'Товары найдены'),
    'get_imported_product': ('Изучаю товар поставщика', 'Товар загружен'),
    'update_imported_product': ('Сохраняю изменения', 'Изменения сохранены'),
    'batch_update_imported_products': ('Пакетное сохранение', 'Пакет сохранён'),
    'search_wb_categories': ('Подбираю категорию WB', 'Категории найдены'),
    'get_category_characteristics': ('Загружаю характеристики категории', 'Характеристики получены'),
    'get_directory': ('Обращаюсь к справочнику WB', 'Справочник получен'),
    'get_prohibited_words': ('Проверяю стоп-слова', 'Стоп-слова загружены'),
    'check_text_prohibited': ('Сканирую текст на запреты', 'Текст проверен'),
    'validate_brand': ('Валидирую бренд в WB', 'Бренд проверен'),
    'get_seller_info': ('Загружаю данные продавца', 'Данные продавца получены'),
    'get_pricing_settings': ('Читаю настройки цен', 'Настройки цен получены'),
    'create_subtask': ('Создаю подзадачу для агента', 'Подзадача создана'),
    'get_subtask_status': ('Проверяю статус подзадачи', 'Статус получен'),
    'get_subtask_result': ('Забираю результат подзадачи', 'Результат получен'),
}

# Промпт для генерации креативного названия thinking-шага
_STEP_NAMER_PROMPT = (
    'Ты генератор коротких названий шагов AI-агента для красивого UI. '
    'Тебе дан фрагмент мыслей агента. Придумай ОДНО короткое (3-6 слов) '
    'креативное и понятное название этого шага на русском. '
    'Без кавычек, без точки. Примеры хороших названий:\n'
    '- Анализирую структуру каталога\n'
    '- Формирую SEO-заголовок\n'
    '- Сверяю бренд с реестром WB\n'
    '- Составляю план оптимизации\n'
    '- Оцениваю качество описания\n'
    '- Подбираю ключевые слова\n'
    '- Финальная проверка карточки\n'
)


# ── Bounded failure tracker ────────────────────────────────────────

class _BoundedFailureTracker(OrderedDict):
    """LRU-ограниченный трекер провалов задач.

    Предотвращает утечку памяти при длительной работе агента.
    Хранит не более maxsize записей, вытесняя самые старые.
    """

    def __init__(self, maxsize: int = 1000):
        super().__init__()
        self.maxsize = maxsize

    def increment(self, key: str) -> int:
        """Инкрементирует счётчик и возвращает новое значение."""
        if key in self:
            self.move_to_end(key)
        self[key] = self.get(key, 0) + 1
        # Вытесняем самые старые записи при переполнении
        while len(self) > self.maxsize:
            self.popitem(last=False)
        return self[key]


# ── Утилиты ────────────────────────────────────────────────────────

def _sanitize_error(error_msg: str) -> str:
    """Очищает сообщение об ошибке от HTML и обрезает до разумной длины."""
    if not error_msg:
        return 'Неизвестная ошибка'

    # Если ошибка содержит HTML — значит LLM API вернул веб-страницу вместо JSON
    if '<!DOCTYPE' in error_msg or '<html' in error_msg or '<!doctype' in error_msg:
        return (
            'LLM API вернул HTML-страницу вместо JSON-ответа. '
            'Вероятно, неверный CLOUDRU_BASE_URL. '
            'Проверьте настройки: правильный URL — '
            'https://foundation-models.api.cloud.ru/v1'
        )

    # Обрезаем слишком длинные сообщения
    if len(error_msg) > MAX_ERROR_LENGTH:
        return error_msg[:MAX_ERROR_LENGTH] + '...'

    return error_msg


def _touch_liveness():
    """Обновляет liveness-файл для Docker healthcheck."""
    try:
        LIVENESS_FILE.touch()
    except OSError:
        pass


def _estimate_context_size(messages: list[dict]) -> int:
    """Примерная оценка размера контекста в символах."""
    return sum(len(m.get('content', '')) for m in messages)


def _summarize_old_messages(messages: list[dict]) -> list[dict]:
    """
    Сжимает старые сообщения, оставляя первое (задачу) и последние 2.
    Промежуточные заменяются кратким резюме.
    """
    if len(messages) <= 4:
        return messages

    first = messages[0]  # исходный промпт задачи
    tail = messages[-2:]  # последняя пара (assistant + user)

    # Собираем краткое резюме промежуточных шагов
    middle = messages[1:-2]
    tool_names = set()
    tool_call_count = 0
    for m in middle:
        content = m.get('content', '')
        # Извлекаем имена вызванных инструментов
        for marker in ('[Tool Call:', '[Tool Result:'):
            idx = 0
            while True:
                pos = content.find(marker, idx)
                if pos == -1:
                    break
                end = content.find(']', pos)
                if end != -1:
                    name_part = content[pos + len(marker):end].split('(')[0].strip()
                    if name_part:
                        tool_names.add(name_part)
                        if marker == '[Tool Call:':
                            tool_call_count += 1
                idx = pos + 1

    summary = (
        f"[Контекст сжат: {len(middle)} промежуточных сообщений опущены. "
        f"Выполнено вызовов: {tool_call_count}. "
        f"Вызванные инструменты: {', '.join(sorted(tool_names)) or 'нет'}. "
        f"Продолжай выполнение задачи. НЕ повторяй уже выполненные вызовы.]"
    )

    return [first, {'role': 'user', 'content': summary}] + tail


def _extract_json(text: str) -> dict:
    """Надёжное извлечение JSON из текстового ответа LLM.

    Поддерживает:
    - Чистый JSON
    - JSON в ```json ... ``` блоке
    - JSON в ``` ... ``` блоке (без указания языка)
    - JSON внутри текстового ответа (первый { ... } блок)
    """
    if not text:
        return {'message': 'Задача выполнена'}

    clean = text.strip()

    # 1. Пробуем весь текст как JSON
    try:
        return json.loads(clean)
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. Извлекаем из code block (```json ... ``` или ``` ... ```)
    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', clean, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            pass

    # 3. Ищем первый { ... } блок (с вложенными объектами)
    brace_depth = 0
    start_idx = None
    for i, ch in enumerate(clean):
        if ch == '{':
            if brace_depth == 0:
                start_idx = i
            brace_depth += 1
        elif ch == '}':
            brace_depth -= 1
            if brace_depth == 0 and start_idx is not None:
                try:
                    return json.loads(clean[start_idx:i + 1])
                except (json.JSONDecodeError, ValueError):
                    start_idx = None

    # 4. Возвращаем как текстовое сообщение
    return {'message': text[:3000]}


class BaseAgent(ABC):
    """
    Базовый агент с ReAct-циклом.

    Наследники определяют:
      - agent_name: str
      - system_prompt: str
      - get_tools() -> ToolRegistry  (дополнительные инструменты)
      - build_task_prompt(task) -> str  (промпт для конкретной задачи)
    """

    agent_name: str = 'base'
    system_prompt: str = 'Ты AI-агент для платформы WB-селлеров.'
    max_iterations: int = 15  # макс. итераций ReAct
    max_tool_retries: int = 2
    use_fallback_llm: bool = False  # True → использовать Claude/Sonnet для сложных задач
    max_batch_size: int = 10  # макс. товаров в одном промпте (чтобы не переполнить контекст)

    def __init__(self, config: AgentConfig = None):
        self.config = config or AgentConfig
        self.config.validate()

        self.platform = PlatformClient(self.config)

        # Выбор LLM: fallback (Claude) для сложных агентов, иначе основной (Cloud.ru)
        if self.use_fallback_llm:
            fallback = create_fallback_llm(self.config)
            if fallback:
                self.llm: BaseLLM = fallback
                logger.info(f"Agent [{self.agent_name}] using fallback LLM")
            else:
                self.llm: BaseLLM = create_llm(self.config)
                logger.info(f"Agent [{self.agent_name}] fallback not configured, using default LLM")
        else:
            self.llm: BaseLLM = create_llm(self.config)

        # Step namer LLM (быстрая модель для генерации названий шагов)
        self._step_namer: Optional[BaseLLM] = None
        try:
            self._step_namer = create_step_namer_llm(self.config)
            if self._step_namer:
                logger.info(f"Agent [{self.agent_name}] step namer LLM configured")
        except Exception as e:
            logger.debug(f"Step namer LLM not available: {e}")

        # Инструменты
        self._tools = create_platform_tools(self.platform)
        extra = self.get_tools()
        if extra:
            self._tools.merge(extra)
        # Удаляем инструменты, запрещённые для данного агента
        for tool_name in getattr(self, 'excluded_tools', ()):
            self._tools.remove(tool_name)

        self._running = False
        self._heartbeat_thread: Optional[threading.Thread] = None

        # Трекинг провалов задач — bounded LRU для предотвращения утечки памяти
        self._task_failures = _BoundedFailureTracker(maxsize=1000)

    # ── Абстрактные методы ─────────────────────────────────────────

    def get_tools(self) -> Optional[ToolRegistry]:
        """Дополнительные инструменты агента. Переопределить в наследнике."""
        return None

    @abstractmethod
    def build_task_prompt(self, task: dict) -> str:
        """Формирует промпт для выполнения задачи."""
        ...

    def execute_task(self, task: dict) -> dict:
        """Выполняет задачу. Агенты могут переопределить для chunked batch."""
        return self._execute_react(task)

    def post_process(self, task: dict, result: dict) -> dict:
        """Постобработка результата (опционально)."""
        return result

    def _run_chunked_batch(self, task: dict, product_ids: list,
                           chunk_size: int = None) -> dict:
        """Разбивает большой batch на чанки и обрабатывает каждый отдельным ReAct-циклом.

        Полезно для пакетных задач с большим кол-вом товаров,
        чтобы не переполнить контекст LLM.
        Возвращает объединённый результат.
        """
        chunk_size = chunk_size or self.max_batch_size
        task_id = task['id']
        seller_id = task.get('seller_id')
        task_type = task.get('task_type', 'fill_batch')

        chunks = [product_ids[i:i + chunk_size]
                  for i in range(0, len(product_ids), chunk_size)]

        total_processed = 0
        total_saved = 0
        all_results = []
        total_input_tokens = 0
        total_output_tokens = 0

        for chunk_idx, chunk_ids in enumerate(chunks):
            self.platform.log_thinking(
                task_id,
                f'Чанк {chunk_idx + 1}/{len(chunks)}',
                f'Обрабатываю товары {chunk_ids[:5]}... ({len(chunk_ids)} шт)',
            )

            # Создаём виртуальную задачу для чанка
            chunk_task = {
                **task,
                'input_data': json.dumps({
                    'product_ids': chunk_ids,
                    'imported_product_ids': chunk_ids,
                    'seller_id': seller_id,
                }),
                'task_type': task_type,
            }

            chunk_result = self._execute_react(chunk_task)

            # Собираем статистику
            usage = chunk_result.pop('_usage', {})
            total_input_tokens += usage.get('input_tokens', 0)
            total_output_tokens += usage.get('output_tokens', 0)

            total_processed += chunk_result.get('processed', chunk_result.get('saved', 0))
            total_saved += chunk_result.get('saved', chunk_result.get('processed', 0))
            chunk_results = chunk_result.get('results', [])
            if isinstance(chunk_results, list):
                all_results.extend(chunk_results)

        return {
            'processed': total_processed,
            'saved': total_saved,
            'results': all_results,
            'chunks': len(chunks),
            'message': f'Обработано {total_processed} товаров ({len(chunks)} чанков)',
            '_usage': {
                'input_tokens': total_input_tokens,
                'output_tokens': total_output_tokens,
                'total_tokens': total_input_tokens + total_output_tokens,
                'react_iterations': len(chunks),
            },
        }

    # ── Structured Batch Mode ─────────────────────────────────────

    def _execute_structured_batch(
        self,
        task: dict,
        product_ids: list[int],
        chunk_size: int = 25,
        max_workers: int = 3,
    ) -> dict:
        """Пакетная обработка через structured_output (без tool calling).

        Идеально для генеративных агентов (SEO, бренды): Python предзагружает
        все данные, LLM возвращает JSON-массив результатов, Python сохраняет.
        1 LLM-вызов на чанк вместо десятков ReAct-итераций.
        """
        task_id = task['id']
        total = len(product_ids)

        self.platform.log_thinking(
            task_id, 'Structured Batch Mode',
            f'Обработка {total} товаров чанками по {chunk_size}. '
            f'Параллельность: {max_workers}.',
        )
        self.platform.update_progress(task_id, 0, 'Загрузка данных товаров', total)

        # 1. Предзагрузка всех товаров
        all_products = self._prefetch_for_structured_batch(product_ids)
        if not all_products:
            return {
                'processed': 0, 'saved': 0, 'failed': total,
                'message': 'Не удалось загрузить данные товаров',
            }

        # Индекс для быстрого поиска
        products_by_id = {p['id']: p for p in all_products}

        # 2. Разбиваем на чанки
        chunks = [product_ids[i:i + chunk_size]
                  for i in range(0, total, chunk_size)]

        # 3. Обработка чанков (параллельно или последовательно)
        progress_lock = threading.Lock()
        processed_count = 0
        saved_count = 0
        failed_count = 0
        all_results = []
        all_errors = []
        total_input_tokens = 0
        total_output_tokens = 0

        def _process_chunk(chunk_idx: int, chunk_ids: list[int]) -> dict:
            """Обрабатывает один чанк: LLM structured_output → batch save."""
            nonlocal processed_count, saved_count, failed_count
            nonlocal total_input_tokens, total_output_tokens

            chunk_products = [products_by_id[pid] for pid in chunk_ids
                              if pid in products_by_id]
            if not chunk_products:
                return {'processed': 0, 'saved': 0, 'errors': []}

            self.platform.log_thinking(
                task_id,
                f'Чанк {chunk_idx + 1}/{len(chunks)}',
                f'LLM обрабатывает {len(chunk_products)} товаров',
            )

            # LLM structured_output
            try:
                prompt = self.build_structured_prompt(chunk_products)
                schema = self.batch_result_schema()
                t0 = time.time()
                llm_result = self.llm.structured_output(
                    system=self.system_prompt,
                    prompt=prompt,
                    schema=schema,
                )
                duration_ms = int((time.time() - t0) * 1000)

                self.platform.log_action(
                    task_id,
                    f'LLM ответ (чанк {chunk_idx + 1})',
                    f'Получен за {duration_ms}мс',
                    duration_ms=duration_ms,
                )
            except Exception as e:
                err_msg = f'Чанк {chunk_idx + 1}: LLM ошибка — {str(e)[:200]}'
                logger.warning(f"Structured batch chunk {chunk_idx} failed: {e}")
                self.platform.log_error(task_id, f'Ошибка чанка {chunk_idx + 1}', err_msg)

                # Fallback: пробуем ReAct для этого чанка
                try:
                    self.platform.log_thinking(
                        task_id, f'Fallback на ReAct (чанк {chunk_idx + 1})',
                        'Structured output не удался, переключаюсь на ReAct',
                    )
                    chunk_task = {
                        **task,
                        'input_data': json.dumps({
                            'product_ids': chunk_ids,
                            'imported_product_ids': chunk_ids,
                            'seller_id': task.get('seller_id'),
                        }),
                    }
                    react_result = self._execute_react(chunk_task)
                    chunk_saved = react_result.get('saved', react_result.get('processed', 0))
                    with progress_lock:
                        processed_count += len(chunk_ids)
                        saved_count += chunk_saved
                    return react_result
                except Exception as e2:
                    logger.error(f"ReAct fallback also failed for chunk {chunk_idx}: {e2}")
                    with progress_lock:
                        failed_count += len(chunk_ids)
                    return {'processed': 0, 'saved': 0,
                            'errors': [{'chunk': chunk_idx, 'error': str(e)[:200]}]}

            # Извлекаем результаты
            results = llm_result.get('results', [])
            if isinstance(llm_result, list):
                results = llm_result

            # Пост-обработка (проверка стоп-слов и т.п.)
            try:
                results = self._postprocess_structured_results(results)
            except Exception as e:
                logger.warning(f"Post-processing error in chunk {chunk_idx}: {e}")

            # Маппинг в формат batch update
            try:
                updates = self._map_structured_result_to_updates(results)
            except Exception as e:
                logger.warning(f"Mapping error in chunk {chunk_idx}: {e}")
                updates = []

            # Batch save
            chunk_saved = 0
            chunk_errors = []
            if updates:
                try:
                    save_resp = self.platform.batch_update_imported_products(updates)
                    chunk_saved = save_resp.get('updated', 0)
                    for r in save_resp.get('results', []):
                        if r.get('status') == 'error':
                            chunk_errors.append(r)
                except Exception as e:
                    logger.error(f"Batch save error for chunk {chunk_idx}: {e}")
                    chunk_errors.append({'error': str(e)[:200]})

            # Обновляем прогресс
            with progress_lock:
                processed_count += len(chunk_products)
                saved_count += chunk_saved
                failed_count += len(chunk_errors)
                all_results.extend(results)
                all_errors.extend(chunk_errors)
                self.platform.update_progress(
                    task_id,
                    completed_steps=processed_count,
                    current_step_label=(
                        f'Чанк {chunk_idx + 1}/{len(chunks)}: '
                        f'обработано {processed_count}/{total}'
                    ),
                )

            return {
                'processed': len(chunk_products),
                'saved': chunk_saved,
                'errors': chunk_errors,
            }

        # Запускаем обработку
        if max_workers <= 1 or len(chunks) <= 1:
            # Последовательная обработка
            for idx, chunk_ids in enumerate(chunks):
                _process_chunk(idx, chunk_ids)
        else:
            # Параллельная обработка
            with ThreadPoolExecutor(max_workers=min(max_workers, len(chunks))) as executor:
                futures = {
                    executor.submit(_process_chunk, idx, chunk_ids): idx
                    for idx, chunk_ids in enumerate(chunks)
                }
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        logger.error(f"Chunk future error: {e}")

        self.platform.log_result(
            task_id, 'Batch завершён',
            f'Обработано: {processed_count}, сохранено: {saved_count}, '
            f'ошибок: {failed_count}',
        )

        return {
            'processed': processed_count,
            'saved': saved_count,
            'failed': failed_count,
            'results': all_results,
            'errors': all_errors if all_errors else None,
            'chunks': len(chunks),
            'message': (
                f'Обработано {processed_count} товаров ({len(chunks)} чанков). '
                f'Сохранено: {saved_count}.'
            ),
            '_usage': {
                'input_tokens': total_input_tokens,
                'output_tokens': total_output_tokens,
                'total_tokens': total_input_tokens + total_output_tokens,
                'mode': 'structured_batch',
                'chunks': len(chunks),
            },
        }

    # ── Overridable hooks для Structured Batch ────────────────────

    def _prefetch_for_structured_batch(self, product_ids: list[int]) -> list[dict]:
        """Предзагрузка данных товаров для structured batch.

        По умолчанию: brief API пачками по 50.
        Агенты могут переопределить для загрузки полных данных.
        """
        all_products = []
        for i in range(0, len(product_ids), 50):
            batch = product_ids[i:i + 50]
            try:
                products = self.platform.get_imported_products_brief(batch)
                all_products.extend(products)
            except Exception as e:
                logger.warning(f"Failed to prefetch batch {i//50}: {e}")
        return all_products

    def build_structured_prompt(self, products_data: list[dict]) -> str:
        """Строит промпт для structured batch output.

        Переопределить в агенте. Должен включать данные товаров
        и инструкцию вернуть JSON-массив результатов.
        """
        raise NotImplementedError(
            f'{self.__class__.__name__} must implement build_structured_prompt() '
            f'to use _execute_structured_batch()'
        )

    def batch_result_schema(self) -> dict:
        """JSON-схема для structured batch output.

        Переопределить в агенте. Формат: {results: [{product_id, ...fields}]}.
        """
        raise NotImplementedError(
            f'{self.__class__.__name__} must implement batch_result_schema() '
            f'to use _execute_structured_batch()'
        )

    def _map_structured_result_to_updates(self, results: list[dict]) -> list[dict]:
        """Маппит результаты LLM в payload для batch_update API.

        Переопределить в агенте. Каждый элемент: {product_id: int, ...fields}.
        """
        raise NotImplementedError(
            f'{self.__class__.__name__} must implement _map_structured_result_to_updates() '
            f'to use _execute_structured_batch()'
        )

    def _postprocess_structured_results(self, results: list[dict]) -> list[dict]:
        """Пост-обработка результатов LLM (проверка стоп-слов, валидация и т.п.).

        По умолчанию: возвращает как есть. Переопределить при необходимости.
        """
        return results

    # ── Tool-Assisted Batch Mode ──────────────────────────────────

    def _execute_tool_batch(
        self,
        task: dict,
        product_ids: list[int],
        chunk_size: int = 15,
        max_workers: int = 2,
    ) -> dict:
        """Пакетная обработка с tool calling и предзагрузкой данных.

        Для агентов, которым нужны справочные запросы (категории, характеристики):
        - Все данные товаров предзагружены и встроены в промпт
        - Справочные данные кэшированы заранее
        - Урезанный toolset (без fetch/save — они в Python)
        - Результаты сохраняются пакетно через batch API
        """
        task_id = task['id']
        total = len(product_ids)
        seller_id = task.get('seller_id')
        task_type = task.get('task_type', 'batch')

        self.platform.log_thinking(
            task_id, 'Tool-Assisted Batch Mode',
            f'Обработка {total} товаров чанками по {chunk_size}. '
            f'Предзагрузка данных и справочников.',
        )
        self.platform.update_progress(task_id, 0, 'Предзагрузка данных', total)

        # 1. Предзагрузка данных товаров
        all_products = self._prefetch_for_structured_batch(product_ids)
        if not all_products:
            return {
                'processed': 0, 'saved': 0, 'failed': total,
                'message': 'Не удалось загрузить данные товаров',
            }

        # 2. Кэширование справочных данных
        self.platform.log_thinking(task_id, 'Кэширование справочников', '')
        reference_data = self._prefetch_reference_data(all_products)

        # 3. Разбиваем на чанки
        products_by_id = {p['id']: p for p in all_products}
        chunks = [product_ids[i:i + chunk_size]
                  for i in range(0, total, chunk_size)]

        # 4. Создаём урезанный toolset (без get_product/get_imported_product)
        batch_tools = ToolRegistry()
        batch_tools.merge(self._tools)
        # Убираем fetch-инструменты — данные уже в промпте
        for tool_name in ('get_imported_product', 'get_imported_products',
                          'get_product', 'get_products',
                          'get_imported_products_brief'):
            batch_tools.remove(tool_name)
        # Убираем single update — будем сохранять пакетно
        batch_tools.remove('update_imported_product')

        # 5. Обработка чанков
        progress_lock = threading.Lock()
        processed_count = 0
        saved_count = 0
        failed_count = 0
        all_results = []
        all_errors = []
        total_input_tokens = 0
        total_output_tokens = 0

        def _process_tool_chunk(chunk_idx: int, chunk_ids: list[int]) -> dict:
            nonlocal processed_count, saved_count, failed_count
            nonlocal total_input_tokens, total_output_tokens

            chunk_products = [products_by_id[pid] for pid in chunk_ids
                              if pid in products_by_id]
            if not chunk_products:
                return {'processed': 0, 'saved': 0}

            self.platform.log_thinking(
                task_id,
                f'Чанк {chunk_idx + 1}/{len(chunks)}',
                f'ReAct для {len(chunk_products)} товаров (данные предзагружены)',
            )

            # Строим промпт с предзагруженными данными
            prompt = self._build_tool_batch_prompt(chunk_products, reference_data)

            # Создаём виртуальную задачу для чанка
            chunk_task = {
                **task,
                'input_data': json.dumps({
                    'product_ids': chunk_ids,
                    'imported_product_ids': chunk_ids,
                    'seller_id': seller_id,
                }),
                'task_type': task_type,
                '_prefetched_prompt': prompt,
            }

            # Динамический max_iterations: ~3 итерации на товар
            dynamic_max_iter = max(len(chunk_products) * 3, 10)

            chunk_result = self._execute_react(
                chunk_task,
                tools_override=batch_tools,
                max_iterations_override=dynamic_max_iter,
            )

            usage = chunk_result.pop('_usage', {})

            with progress_lock:
                total_input_tokens += usage.get('input_tokens', 0)
                total_output_tokens += usage.get('output_tokens', 0)
                chunk_processed = chunk_result.get('processed',
                                                   chunk_result.get('saved', len(chunk_products)))
                chunk_saved = chunk_result.get('saved', chunk_result.get('processed', 0))
                processed_count += chunk_processed
                saved_count += chunk_saved

                chunk_results = chunk_result.get('results', [])
                if isinstance(chunk_results, list):
                    all_results.extend(chunk_results)

                self.platform.update_progress(
                    task_id,
                    completed_steps=processed_count,
                    current_step_label=(
                        f'Чанк {chunk_idx + 1}/{len(chunks)}: '
                        f'обработано {processed_count}/{total}'
                    ),
                )

            return chunk_result

        # Запускаем обработку
        if max_workers <= 1 or len(chunks) <= 1:
            for idx, chunk_ids in enumerate(chunks):
                _process_tool_chunk(idx, chunk_ids)
        else:
            with ThreadPoolExecutor(max_workers=min(max_workers, len(chunks))) as executor:
                futures = {
                    executor.submit(_process_tool_chunk, idx, chunk_ids): idx
                    for idx, chunk_ids in enumerate(chunks)
                }
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        logger.error(f"Tool batch chunk error: {e}")

        return {
            'processed': processed_count,
            'saved': saved_count,
            'failed': failed_count,
            'results': all_results,
            'errors': all_errors if all_errors else None,
            'chunks': len(chunks),
            'message': (
                f'Обработано {processed_count} товаров ({len(chunks)} чанков). '
                f'Сохранено: {saved_count}.'
            ),
            '_usage': {
                'input_tokens': total_input_tokens,
                'output_tokens': total_output_tokens,
                'total_tokens': total_input_tokens + total_output_tokens,
                'mode': 'tool_batch',
                'chunks': len(chunks),
            },
        }

    # ── Overridable hooks для Tool-Assisted Batch ─────────────────

    def _prefetch_reference_data(self, products_data: list[dict]) -> dict:
        """Предзагрузка справочных данных для tool batch.

        Переопределить в агенте. Например:
        - CategoryMapper: поиск категорий по уникальным category поставщика
        - CharacteristicsFiller: характеристики по уникальным wb_subject_id

        Возвращает dict, который передаётся в _build_tool_batch_prompt().
        """
        return {}

    def _build_tool_batch_prompt(
        self, products_data: list[dict], reference_data: dict,
    ) -> str:
        """Строит промпт для tool batch чанка с предзагруженными данными.

        По умолчанию: делегирует в build_task_prompt() стандартного агента.
        Переопределить для включения reference_data в промпт.
        """
        # Fallback: используем стандартный build_task_prompt
        # (будет вызван из _execute_react через build_task_prompt)
        return ''

    # ── Креативные названия шагов ──────────────────────────────────

    def _get_tool_label(self, tool_name: str, is_result: bool = False) -> str:
        """Возвращает креативное название для tool call/result."""
        labels = TOOL_LABELS.get(tool_name)
        if labels:
            return labels[1] if is_result else labels[0]
        # Fallback: человекочитаемый формат
        return f'Результат: {tool_name}' if is_result else f'Вызов: {tool_name}'

    def _generate_step_label(self, thinking_text: str, iteration: int) -> str:
        """Генерирует креативное название для thinking-шага через быструю модель."""
        default = f'Рассуждение (шаг {iteration + 1})'

        if not self._step_namer or not thinking_text:
            return default

        # Обрезаем текст до 200 символов для быстрого ответа
        snippet = thinking_text[:200].strip()
        if not snippet:
            return default

        try:
            label = self._step_namer.chat(
                system=_STEP_NAMER_PROMPT,
                messages=[{'role': 'user', 'content': snippet}],
                temperature=0.7,
                max_tokens=30,
            )
            label = label.strip().strip('"\'').strip('.')
            # Валидация: 2-50 символов, не пустой
            if label and 2 <= len(label) <= 50:
                return label
        except Exception as e:
            logger.debug(f"Step namer failed: {e}")

        return default

    # ── Утилиты ────────────────────────────────────────────────────

    @staticmethod
    def parse_input_data(task: dict) -> dict:
        """Парсит input_data из задачи. Убирает дублирование в наследниках."""
        input_data = task.get('input_data', '{}')
        if isinstance(input_data, str):
            try:
                return json.loads(input_data)
            except (json.JSONDecodeError, ValueError):
                return {}
        return input_data or {}

    def wait_for_subtask(self, task_id: str, timeout: int = 600,
                         poll_interval: int = 10) -> dict:
        """Ожидание завершения подзадачи БЕЗ LLM-итераций.

        Используется оркестратором для экономии токенов.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                status = self.platform.get_task_status(task_id)
                task_data = status.get('task', status)
                task_status = task_data.get('status', '')
                if task_status in ('completed', 'failed', 'cancelled'):
                    return status
            except Exception as e:
                logger.warning(f"Subtask poll error for {task_id[:8]}: {e}")
            time.sleep(poll_interval)
        return {'error': f'Таймаут ожидания подзадачи ({timeout}с)', 'task_id': task_id}

    # ── Основной цикл ──────────────────────────────────────────────

    def run(self):
        """Запускает агента: heartbeat + poll loop."""
        self._running = True
        self._setup_signals()
        self._start_heartbeat()
        _touch_liveness()

        logger.info(f"Agent [{self.agent_name}] started. Polling every {self.config.POLL_INTERVAL}s")

        # Ждём готовности платформы перед первым heartbeat
        for attempt in range(1, 13):  # до 2 минут (12 * 10с)
            try:
                self.platform.heartbeat('online')
                break
            except Exception as e:
                logger.warning(f"Platform not ready (attempt {attempt}/12): {e}")
                time.sleep(10)

        try:
            while self._running:
                try:
                    self._poll_and_execute()
                except Exception as e:
                    logger.error(f"Poll cycle error: {e}", exc_info=True)
                time.sleep(self.config.POLL_INTERVAL)
        finally:
            self._running = False
            logger.info(f"Agent [{self.agent_name}] shutting down")
            self._stop_heartbeat()
            try:
                self.platform.heartbeat('offline')
            except Exception:
                pass

    def stop(self):
        """Останавливает агента."""
        self._running = False

    def _poll_and_execute(self):
        """Один цикл: получить задачу → выполнить."""
        tasks = self.platform.poll_tasks(limit=1)
        if not tasks:
            return

        task = tasks[0]
        task_id = task['id']

        # Dead letter protection: пропускаем задачи с слишком большим числом провалов
        fail_count = self._task_failures.get(task_id, 0)
        if fail_count >= MAX_TASK_FAILURES:
            logger.warning(
                f"Task {task_id[:8]} skipped: failed {fail_count} times (dead letter)"
            )
            try:
                self.platform.fail_task(
                    task_id,
                    f'Задача провалилась {fail_count} раз подряд, пропущена агентом'
                )
            except Exception:
                pass
            return

        logger.info(f"Picked up task {task_id[:8]}: {task.get('title', '?')}")

        # Устанавливаем task_id для передачи в X-Task-Id (нужен для снимков отката)
        self.platform.set_task_id(task_id)

        try:
            # Берём задачу в работу
            self.platform.start_task(task_id)
            self.platform.log_thinking(task_id, 'Анализирую задачу',
                                       f"Тип: {task.get('task_type')}")

            # Выполняем задачу (по умолчанию ReAct цикл, агенты могут переопределить)
            result = self.execute_task(task)

            # Если задача была отменена во время выполнения
            if isinstance(result, dict) and result.get('status') == 'cancelled':
                logger.info(f"Task {task_id[:8]} cancelled during execution")
                return

            # Постобработка
            result = self.post_process(task, result)

            # Завершаем
            self.platform.complete_task(task_id, result)
            self.platform.log_result(task_id, 'Задача завершена',
                                     json.dumps(result, ensure_ascii=False)[:500])
            logger.info(f"Task {task_id[:8]} completed")

            # Сбрасываем счётчик провалов при успехе
            self._task_failures.pop(task_id, None)

        except Exception as e:
            error_msg = _sanitize_error(str(e))
            logger.error(f"Task {task_id[:8]} failed: {error_msg}", exc_info=True)

            # Инкрементируем счётчик провалов (bounded)
            self._task_failures.increment(task_id)

            try:
                self.platform.log_error(task_id, 'Ошибка выполнения', error_msg)
                self.platform.fail_task(task_id, error_msg)
            except Exception:
                pass
        finally:
            self.platform.set_task_id(None)

    # ── ReAct цикл ─────────────────────────────────────────────────

    def _check_task_cancelled(self, task_id: str) -> bool:
        """Проверяет, не была ли задача отменена."""
        try:
            status = self.platform.get_task_status(task_id)
            task_data = status.get('task', status)
            return task_data.get('status') == 'cancelled'
        except Exception:
            return False

    def _execute_react(self, task: dict,
                       tools_override: ToolRegistry = None,
                       max_iterations_override: int = None) -> dict:
        """
        ReAct (Reason-Act) цикл:
        1. LLM получает задачу + инструменты
        2. LLM рассуждает (thinking) и вызывает инструменты (action)
        3. Результат инструмента возвращается LLM (observation)
        4. Повторяем до финального ответа

        tools_override: заменяет self._tools (для batch mode с урезанным toolset)
        max_iterations_override: заменяет self.max_iterations (для dynamic batch sizing)
        """
        task_id = task['id']

        # Поддержка предзагруженного промпта (от _execute_tool_batch)
        task_prompt = task.get('_prefetched_prompt') or self.build_task_prompt(task)

        messages = [{'role': 'user', 'content': task_prompt}]

        active_tools = tools_override or self._tools
        tool_schemas = active_tools.get_tool_schemas()
        effective_max_iterations = max_iterations_override or self.max_iterations
        total_steps = 0
        total_input_tokens = 0
        total_output_tokens = 0

        for iteration in range(effective_max_iterations):
            # Cancel propagation: проверяем отмену каждые N итераций
            if iteration > 0 and iteration % CANCEL_CHECK_INTERVAL == 0:
                if self._check_task_cancelled(task_id):
                    logger.info(f"Task {task_id[:8]}: cancelled by user, stopping ReAct")
                    self.platform.log_decision(
                        task_id, 'Задача отменена',
                        'Задача была отменена пользователем во время выполнения.',
                    )
                    return {'status': 'cancelled', 'message': 'Задача отменена пользователем'}

            # Защита от переполнения контекста
            if _estimate_context_size(messages) > CONTEXT_CHAR_LIMIT:
                logger.info(f"Task {task_id[:8]}: context overflow, summarizing")
                messages = _summarize_old_messages(messages)

            t0 = time.time()

            # Вызов LLM
            if tool_schemas:
                response = self.llm.chat_with_tools(
                    system=self.system_prompt,
                    messages=messages,
                    tools=tool_schemas,
                )
            else:
                text = self.llm.chat(self.system_prompt, messages)
                response = {'text': text, 'tool_calls': [], 'stop_reason': 'end_turn'}

            duration_ms = int((time.time() - t0) * 1000)

            # Трекинг токенов
            usage = response.get('usage', {})
            total_input_tokens += usage.get('input_tokens', 0)
            total_output_tokens += usage.get('output_tokens', 0)

            # Логируем рассуждения с креативным названием
            if response['text']:
                total_steps += 1
                step_label = self._generate_step_label(response['text'], iteration)
                self.platform.log_thinking(
                    task_id,
                    step_label,
                    response['text'][:1000],
                    duration_ms=duration_ms,
                )
                self.platform.update_progress(
                    task_id, completed_steps=total_steps,
                    current_step_label=step_label,
                )

            # Если нет tool calls — финальный ответ
            if not response['tool_calls']:
                result = _extract_json(response['text'])
                result['_usage'] = {
                    'input_tokens': total_input_tokens,
                    'output_tokens': total_output_tokens,
                    'total_tokens': total_input_tokens + total_output_tokens,
                    'react_iterations': iteration + 1,
                }
                return result

            # Выполняем tool calls
            messages.append({
                'role': 'assistant',
                'content': self._format_assistant_message(response),
            })

            tool_results = []
            for call in response['tool_calls']:
                tool_name = call['name']
                tool_args = call['arguments']

                total_steps += 1
                action_label = self._get_tool_label(tool_name, is_result=False)
                self.platform.log_action(
                    task_id,
                    action_label,
                    json.dumps(tool_args, ensure_ascii=False)[:500],
                )

                # Выполняем инструмент
                t1 = time.time()
                result_str = active_tools.execute(tool_name, tool_args)
                tool_duration = int((time.time() - t1) * 1000)

                result_label = self._get_tool_label(tool_name, is_result=True)
                self.platform.log_decision(
                    task_id,
                    result_label,
                    result_str[:500],
                    duration_ms=tool_duration,
                )

                tool_results.append({
                    'tool_use_id': call.get('id', ''),
                    'name': tool_name,
                    'result': result_str,
                })

                self.platform.update_progress(
                    task_id, completed_steps=total_steps,
                    current_step_label=action_label,
                )

            # Добавляем результаты инструментов в контекст
            messages.append({
                'role': 'user',
                'content': self._format_tool_results(tool_results),
            })

        # Достигнут лимит итераций — пробуем извлечь частичный результат
        logger.warning(f"Task {task_id[:8]}: max iterations reached ({effective_max_iterations})")
        self.platform.log_decision(
            task_id, 'Завершение по лимиту шагов',
            f'Агент выполнил {effective_max_iterations} шагов. '
            f'Задача завершена с частичным результатом.',
        )

        # Если последнее сообщение LLM содержало текст — попробуем извлечь из него результат
        if messages and messages[-1].get('role') == 'user':
            for msg in reversed(messages):
                if msg.get('role') == 'assistant':
                    partial = _extract_json(msg.get('content', ''))
                    if partial and partial.get('message') != 'Задача выполнена':
                        partial['status'] = 'partial'
                        partial['_note'] = (
                            f'Достигнут лимит шагов ({effective_max_iterations}). '
                            f'Результат может быть неполным.'
                        )
                        partial['_usage'] = {
                            'input_tokens': total_input_tokens,
                            'output_tokens': total_output_tokens,
                            'total_tokens': total_input_tokens + total_output_tokens,
                            'react_iterations': effective_max_iterations,
                        }
                        return partial
                    break

        return {
            'status': 'partial',
            'message': (
                f'Агент выполнил максимум шагов ({effective_max_iterations}) '
                f'и не успел завершить задачу. Попробуйте выбрать меньше товаров.'
            ),
            '_usage': {
                'input_tokens': total_input_tokens,
                'output_tokens': total_output_tokens,
                'total_tokens': total_input_tokens + total_output_tokens,
                'react_iterations': effective_max_iterations,
            },
        }

    def _format_assistant_message(self, response: dict) -> str:
        """Форматирует ответ ассистента для контекста."""
        parts = []
        if response['text']:
            # Обрезаем рассуждения для экономии контекста
            parts.append(response['text'][:500])
        for call in response['tool_calls']:
            parts.append(
                f"[Tool Call: {call['name']}({json.dumps(call['arguments'], ensure_ascii=False)[:150]})]"
            )
        return '\n'.join(parts)

    def _format_tool_results(self, results: list) -> str:
        """Форматирует результаты инструментов для LLM."""
        parts = []
        for r in results:
            # Ограничиваем размер результатов для экономии контекста
            result_text = r['result']
            if len(result_text) > 1200:
                result_text = result_text[:1200] + '\n... (обрезано)'
            parts.append(f"[Tool Result: {r['name']}]\n{result_text}")
        return '\n\n'.join(parts)

    # ── Heartbeat ──────────────────────────────────────────────────

    def _start_heartbeat(self):
        """Запускает фоновый heartbeat + обновляет liveness-файл."""
        config_reload_counter = 0
        config_reload_every = 10  # каждые N heartbeat-ов (~5 мин при 30с интервале)

        def _beat():
            nonlocal config_reload_counter
            while self._running:
                try:
                    self.platform.heartbeat('online')
                except Exception as e:
                    logger.warning(f"Heartbeat failed: {e}")

                # Периодически обновляем remote LLM config
                config_reload_counter += 1
                if config_reload_counter >= config_reload_every:
                    config_reload_counter = 0
                    self.config.reload_remote_config()

                _touch_liveness()
                time.sleep(self.config.HEARTBEAT_INTERVAL)

        self._heartbeat_thread = threading.Thread(target=_beat, daemon=True)
        self._heartbeat_thread.start()

    def _stop_heartbeat(self):
        """Graceful stop для heartbeat thread."""
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=5)

    # ── Graceful shutdown ──────────────────────────────────────────

    def _setup_signals(self):
        """Ловим SIGINT/SIGTERM для graceful shutdown."""
        def _handler(signum, frame):
            logger.info(f"Signal {signum} received, stopping agent...")
            self.stop()

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)


class SimpleAgent(BaseAgent):
    """
    Простой агент без дополнительных инструментов.
    Использует только платформенные инструменты и LLM.
    """

    def build_task_prompt(self, task: dict) -> str:
        input_data = self.parse_input_data(task)

        return (
            f"Задача: {task.get('title', 'Без названия')}\n"
            f"Тип: {task.get('task_type', 'unknown')}\n"
            f"ID продавца: {task.get('seller_id')}\n"
            f"Входные данные:\n{json.dumps(input_data, ensure_ascii=False, indent=2)}\n\n"
            f"Выполни задачу, используя доступные инструменты. "
            f"Когда закончишь, верни итоговый результат в JSON."
        )
