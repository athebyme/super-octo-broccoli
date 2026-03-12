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
"""
import json
import logging
import os
import signal
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from .config import AgentConfig
from .llm import BaseLLM, create_llm, create_fallback_llm
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

        # Инструменты
        self._tools = create_platform_tools(self.platform)
        extra = self.get_tools()
        if extra:
            for name, tool_def in extra._tools.items():
                self._tools._tools[name] = tool_def
            for name, handler in extra._handlers.items():
                self._tools._handlers[name] = handler

        self._running = False
        self._heartbeat_thread: Optional[threading.Thread] = None

        # Трекинг провалов задач (task_id → failure_count)
        self._task_failures: dict[str, int] = {}

    # ── Абстрактные методы ─────────────────────────────────────────

    def get_tools(self) -> Optional[ToolRegistry]:
        """Дополнительные инструменты агента. Переопределить в наследнике."""
        return None

    @abstractmethod
    def build_task_prompt(self, task: dict) -> str:
        """Формирует промпт для выполнения задачи."""
        ...

    def post_process(self, task: dict, result: dict) -> dict:
        """Постобработка результата (опционально)."""
        return result

    # ── Основной цикл ──────────────────────────────────────────────

    def run(self):
        """Запускает агента: heartbeat + poll loop."""
        self._running = True
        self._setup_signals()
        self._start_heartbeat()
        _touch_liveness()

        logger.info(f"Agent [{self.agent_name}] started. Polling every {self.config.POLL_INTERVAL}s")
        self.platform.heartbeat('online')

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

        try:
            # Берём задачу в работу
            self.platform.start_task(task_id)
            self.platform.log_thinking(task_id, 'Анализирую задачу',
                                       f"Тип: {task.get('task_type')}")

            # Выполняем ReAct цикл
            result = self._execute_react(task)

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

            # Инкрементируем счётчик провалов
            self._task_failures[task_id] = self._task_failures.get(task_id, 0) + 1

            try:
                self.platform.log_error(task_id, 'Ошибка выполнения', error_msg)
                self.platform.fail_task(task_id, error_msg)
            except Exception:
                pass

    # ── ReAct цикл ─────────────────────────────────────────────────

    def _execute_react(self, task: dict) -> dict:
        """
        ReAct (Reason-Act) цикл:
        1. LLM получает задачу + инструменты
        2. LLM рассуждает (thinking) и вызывает инструменты (action)
        3. Результат инструмента возвращается LLM (observation)
        4. Повторяем до финального ответа
        """
        task_id = task['id']
        task_prompt = self.build_task_prompt(task)

        messages = [{'role': 'user', 'content': task_prompt}]
        tool_schemas = self._tools.get_tool_schemas()
        total_steps = 0

        for iteration in range(self.max_iterations):
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

            # Логируем рассуждения
            if response['text']:
                total_steps += 1
                self.platform.log_thinking(
                    task_id,
                    f'Рассуждение (шаг {iteration + 1})',
                    response['text'][:1000],
                    duration_ms=duration_ms,
                )
                self.platform.update_progress(
                    task_id, completed_steps=total_steps,
                    current_step_label=f'Рассуждение (шаг {iteration + 1})',
                )

            # Если нет tool calls — финальный ответ
            if not response['tool_calls']:
                return self._parse_final_answer(response['text'])

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
                self.platform.log_action(
                    task_id,
                    f'Вызов: {tool_name}',
                    json.dumps(tool_args, ensure_ascii=False)[:500],
                )

                # Выполняем инструмент
                t1 = time.time()
                result_str = self._tools.execute(tool_name, tool_args)
                tool_duration = int((time.time() - t1) * 1000)

                self.platform.log_decision(
                    task_id,
                    f'Результат: {tool_name}',
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
                    current_step_label=f'{tool_name}',
                )

            # Добавляем результаты инструментов в контекст
            messages.append({
                'role': 'user',
                'content': self._format_tool_results(tool_results),
            })

        # Достигнут лимит итераций — пробуем извлечь частичный результат
        logger.warning(f"Task {task_id[:8]}: max iterations reached ({self.max_iterations})")
        self.platform.log_decision(
            task_id, 'Завершение по лимиту шагов',
            f'Агент выполнил {self.max_iterations} шагов. '
            f'Задача завершена с частичным результатом.',
        )

        # Если последнее сообщение LLM содержало текст — попробуем извлечь из него результат
        if messages and messages[-1].get('role') == 'user':
            # Последний ответ ассистента мог содержать частичные данные
            for msg in reversed(messages):
                if msg.get('role') == 'assistant':
                    partial = self._parse_final_answer(msg.get('content', ''))
                    if partial and partial.get('message') != 'Задача выполнена':
                        partial['status'] = 'partial'
                        partial['_note'] = (
                            f'Достигнут лимит шагов ({self.max_iterations}). '
                            f'Результат может быть неполным.'
                        )
                        return partial
                    break

        return {
            'status': 'partial',
            'message': (
                f'Агент выполнил максимум шагов ({self.max_iterations}) '
                f'и не успел завершить задачу. Попробуйте выбрать меньше товаров.'
            ),
        }

    def _format_assistant_message(self, response: dict) -> str:
        """Форматирует ответ ассистента для контекста."""
        parts = []
        if response['text']:
            # Обрезаем рассуждения для экономии контекста
            parts.append(response['text'][:800])
        for call in response['tool_calls']:
            parts.append(
                f"[Tool Call: {call['name']}({json.dumps(call['arguments'], ensure_ascii=False)[:200]})]"
            )
        return '\n'.join(parts)

    def _format_tool_results(self, results: list) -> str:
        """Форматирует результаты инструментов для LLM."""
        parts = []
        for r in results:
            # Ограничиваем размер результатов для экономии контекста
            result_text = r['result']
            if len(result_text) > 1500:
                result_text = result_text[:1500] + '\n... (данные обрезаны для экономии контекста)'
            parts.append(f"[Tool Result: {r['name']}]\n{result_text}")
        return '\n\n'.join(parts)

    def _parse_final_answer(self, text: str) -> dict:
        """Пытается извлечь JSON из финального ответа LLM."""
        if not text:
            return {'message': 'Задача выполнена'}

        # Пробуем JSON
        clean = text.strip()
        if clean.startswith('```'):
            lines = clean.split('\n')
            clean = '\n'.join(lines[1:-1] if lines[-1].strip() == '```' else lines[1:])

        try:
            return json.loads(clean)
        except (json.JSONDecodeError, ValueError):
            pass

        return {'message': text[:3000]}

    # ── Heartbeat ──────────────────────────────────────────────────

    def _start_heartbeat(self):
        """Запускает фоновый heartbeat + обновляет liveness-файл."""
        def _beat():
            while self._running:
                try:
                    self.platform.heartbeat('online')
                except Exception as e:
                    logger.warning(f"Heartbeat failed: {e}")
                _touch_liveness()
                time.sleep(self.config.HEARTBEAT_INTERVAL)

        self._heartbeat_thread = threading.Thread(target=_beat, daemon=True)
        self._heartbeat_thread.start()

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
        input_data = task.get('input_data', '{}')
        if isinstance(input_data, str):
            try:
                input_data = json.loads(input_data)
            except (json.JSONDecodeError, ValueError):
                pass

        return (
            f"Задача: {task.get('title', 'Без названия')}\n"
            f"Тип: {task.get('task_type', 'unknown')}\n"
            f"ID продавца: {task.get('seller_id')}\n"
            f"Входные данные:\n{json.dumps(input_data, ensure_ascii=False, indent=2)}\n\n"
            f"Выполни задачу, используя доступные инструменты. "
            f"Когда закончишь, верни итоговый результат в JSON."
        )
