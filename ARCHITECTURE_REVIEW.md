# Архитектурный обзор агентной системы WB Seller Platform

## Резюме

Система представляет собой зрелую мульти-агентную платформу для автоматизации управления каталогом товаров на Wildberries. Архитектура включает 10 специализированных агентов, HTTP-based координацию через Internal API, ReAct-цикл для LLM-взаимодействия, и поддержку 4 LLM-провайдеров.

**Общая оценка: 7/10** — хорошая основа, но есть серьёзные пробелы, которые мешают системе работать "как часы" в продакшене.

---

## 1. КРИТИЧЕСКИЕ ПРОБЛЕМЫ (нужно исправить в первую очередь)

### 1.1. Отсутствие тестов — CRITICAL

**Файлы:** весь проект
**Проблема:** Нет ни одного теста. Ни unit, ни integration, ни smoke.

**Что нужно:**
- Unit-тесты для `ToolRegistry.execute()`, `_parse_final_answer()`, `_summarize_old_messages()`
- Unit-тесты для `resolve_agents_from_text()` (orchestrator keyword matching)
- Integration-тесты для `PlatformClient` с мок-сервером
- Smoke-тесты для каждого агента (создание, build_task_prompt)
- Тесты для `_sanitize_error()`, конфиг-валидации, retry-логики

**Почему критично:** Без тестов невозможно уверенно вносить изменения. Одно случайное изменение в `base_agent.py` может сломать все 10 агентов, и вы узнаете об этом только в продакшене.

### 1.2. Нет таймаута на задачи (stuck tasks) — CRITICAL

**Файл:** `services/agent_service.py`
**Проблема:** Задача может застрять в статусе `running` навсегда, если агент упал во время выполнения. Нет механизма для:
- Обнаружения зависших задач
- Автоматического переназначения задач
- Таймаута на уровне задачи

**Решение:**
```python
# В agent_service.py добавить:
def requeue_stuck_tasks(timeout_minutes: int = 30):
    """Переставляет зависшие задачи обратно в очередь."""
    threshold = datetime.utcnow() - timedelta(minutes=timeout_minutes)
    stuck = AgentTask.query.filter(
        AgentTask.status == 'running',
        AgentTask.started_at < threshold,
    ).all()
    for task in stuck:
        task.status = 'queued'
        task.started_at = None
        task.retry_count = (task.retry_count or 0) + 1
        if task.retry_count >= 3:
            task.status = 'failed'
            task.error_message = f'Задача зависла {task.retry_count} раз'
    db.session.commit()
    return len(stuck)
```

### 1.3. Race condition при poll_tasks — CRITICAL

**Файл:** `services/agent_service.py:511-518`
**Проблема:** `get_pending_tasks()` — простой SELECT без блокировки. Если два экземпляра одного агента запущены (горизонтальное масштабирование), оба могут взять одну и ту же задачу.

**Решение:** Использовать SELECT FOR UPDATE SKIP LOCKED:
```python
def get_pending_tasks(agent_id: str, limit: int = 1) -> list:
    return AgentTask.query.filter_by(
        agent_id=agent_id, status='queued'
    ).order_by(
        AgentTask.priority.desc(),
        AgentTask.created_at.asc(),
    ).with_for_update(skip_locked=True).limit(limit).all()
```

Либо атомарный claim: UPDATE + RETURNING в одном запросе.

### 1.4. Утечка памяти в _task_failures — HIGH

**Файл:** `agents/base_agent.py:171`
**Проблема:** `self._task_failures: dict[str, int]` растёт бесконечно. Dead-letter задачи помечаются в платформе как failed, но их ID навсегда остаётся в словаре. При долгой работе агента это приведёт к неконтролируемому росту памяти.

**Решение:** Использовать LRU-кэш или периодическую очистку:
```python
from collections import OrderedDict

class _LRUFailureTracker(OrderedDict):
    def __init__(self, maxsize=1000):
        super().__init__()
        self.maxsize = maxsize

    def increment(self, key):
        if key in self:
            self.move_to_end(key)
        self[key] = self.get(key, 0) + 1
        while len(self) > self.maxsize:
            self.popitem(last=False)
```

---

## 2. СЕРЬЁЗНЫЕ ПРОБЛЕМЫ (влияют на надёжность)

### 2.1. Оркестратор не передаёт parent_task_id

**Файл:** `agents/tools.py:177-184`
**Проблема:** При создании подзадачи оркестратором `parent_task_id` не передаётся автоматически. Это значит:
- Подзадачи не связаны с родительской задачей в БД
- Невозможно отследить цепочку выполнения
- Невозможно каскадно отменить подзадачи

**Решение:** Передавать `parent_task_id` из контекста текущей задачи оркестратора. Но сейчас LLM должен сам догадаться передать этот параметр — он его не передаст.

### 2.2. Polling-based orchestration тратит LLM-токены впустую

**Файл:** `agents/catalog/orchestrator.py:192-198`
**Проблема:** Оркестратор вызывает `get_subtask_status()` в ReAct-цикле. Каждый вызов — это итерация LLM (рассуждение + tool call). Если подзадача выполняется 5 минут, оркестратор может потратить 10-20 итераций просто на polling, сжигая токены.

**Решение:** Вынести polling из ReAct-цикла на уровень BaseAgent:
```python
def _wait_for_task(self, task_id: str, timeout: int = 600, poll_interval: int = 10) -> dict:
    """Ждёт завершения задачи БЕЗ LLM-итераций."""
    for _ in range(timeout // poll_interval):
        status = self.platform.get_task_status(task_id)
        if status.get('task', {}).get('status') in ('completed', 'failed'):
            return status
        time.sleep(poll_interval)
    return {'error': 'timeout'}
```

### 2.3. Нет валидации tool arguments перед вызовом

**Файл:** `agents/tools.py:42-54`
**Проблема:** `ToolRegistry.execute()` просто передаёт аргументы в handler через `**arguments`. Если LLM передаст неожиданные аргументы (лишние поля, неправильные типы), это вызовет неинформативный TypeError.

**Решение:** Валидация по JSON Schema перед вызовом:
```python
def execute(self, name: str, arguments: dict) -> str:
    handler = self._handlers.get(name)
    if not handler:
        return json.dumps({'error': f'Unknown tool: {name}'})

    # Валидация по схеме
    schema = self._tools.get(name, {}).get('input_schema', {})
    required = schema.get('required', [])
    for field in required:
        if field not in arguments:
            return json.dumps({'error': f'Missing required argument: {field}'})

    # Фильтрация неизвестных аргументов
    known_props = schema.get('properties', {}).keys()
    filtered_args = {k: v for k, v in arguments.items() if k in known_props}

    try:
        result = handler(**filtered_args)
        ...
```

### 2.4. SSL verification отключён глобально

**Файл:** `agents/platform_client.py:22, 36`
**Проблема:** `urllib3.disable_warnings()` + `session.verify = False` — это глобальное отключение SSL верификации. Подавление предупреждений скрывает потенциальные проблемы безопасности.

**Решение:** Сделать конфигурируемым:
```python
self.session.verify = not self.cfg.get('PLATFORM_SKIP_TLS_VERIFY', False)
```
И убрать глобальное подавление warnings. В Docker-среде лучше настроить корректные сертификаты.

### 2.5. Heartbeat thread — не daemon, нет graceful stop

**Файл:** `agents/base_agent.py:455-467`
**Проблема:** Thread помечен как daemon, но нет join при shutdown. Если heartbeat-запрос застрянет на 90с (timeout в platform_client), process exit будет заблокирован.

**Решение:**
```python
def _stop_heartbeat(self):
    self._running = False
    if self._heartbeat_thread and self._heartbeat_thread.is_alive():
        self._heartbeat_thread.join(timeout=5)
```

### 2.6. Все коммиты в БД — синхронные, без batch

**Файл:** `services/agent_service.py`
**Проблема:** Каждый `log_step()`, `update_progress()`, `heartbeat()` делает отдельный `db.session.commit()`. При активной работе агента это десятки коммитов в секунду.

**Решение:** Буферизация шагов и batch-commit:
- Для шагов: собирать в буфер, flush каждые N шагов или каждые K секунд
- Для progress: дедупликация, обновлять не чаще раза в 2 секунды

---

## 3. ПРОБЛЕМЫ ЗРЕЛОСТИ (мешают масштабированию)

### 3.1. multi-agent через threads — не для продакшена

**Файл:** `agents/runner.py:273-307`
**Проблема:** `run_all_agents()` запускает все агенты в потоках одного процесса. Все агенты используют один и тот же `AGENT_ID` / `AGENT_API_KEY`. При падении одного потока — нет recovery.

**Рекомендация:** Это годится только для dev. В продакшене — один агент = один процесс (Docker container). Добавить в документацию предупреждение (уже есть комментарий в коде, но не хватает `--all` deprecation warning).

### 3.2. Нет метрик (Prometheus/StatsD)

**Проблема:** Нет способа мониторить:
- Количество обработанных задач/час
- Среднее время выполнения
- Количество LLM-вызовов и потраченных токенов
- Error rate по агентам
- Размер очереди задач

**Решение:** Добавить `prometheus_client` с метриками:
```python
from prometheus_client import Counter, Histogram, Gauge

TASKS_PROCESSED = Counter('agent_tasks_total', 'Tasks processed', ['agent', 'status'])
TASK_DURATION = Histogram('agent_task_duration_seconds', 'Task duration', ['agent'])
LLM_CALLS = Counter('agent_llm_calls_total', 'LLM calls', ['provider', 'agent'])
LLM_TOKENS = Counter('agent_llm_tokens_total', 'LLM tokens used', ['provider', 'direction'])
QUEUE_SIZE = Gauge('agent_queue_size', 'Pending tasks', ['agent'])
```

### 3.3. Нет трекинга токенов LLM

**Файл:** `agents/llm.py`
**Проблема:** Ни один провайдер не трекает `usage.input_tokens` / `usage.output_tokens` из ответа LLM. Невозможно:
- Контролировать расходы
- Устанавливать бюджеты на задачу
- Отслеживать аномалии (LLM зациклился)

**Решение:** Все провайдеры должны возвращать `usage` в ответе:
```python
return {
    'text': text,
    'tool_calls': tool_calls,
    'stop_reason': stop_reason,
    'usage': {
        'input_tokens': resp.usage.input_tokens,
        'output_tokens': resp.usage.output_tokens,
    }
}
```

### 3.4. Нет rate limiting на Internal API

**Файл:** `routes/internal_api.py`
**Проблема:** Единственная защита — аутентификация по ключу. Взломанный или зациклившийся агент может заспамить API тысячами запросов.

**Решение:** Добавить rate limiting (Flask-Limiter или middleware):
```python
# Heartbeat: max 1/10s
# Log step: max 10/s
# Poll tasks: max 1/2s
```

### 3.5. Отсутствует cancel propagation

**Файл:** `services/agent_service.py:473-481`
**Проблема:** `cancel_task()` меняет статус в БД, но работающий агент НЕ ЗНАЕТ, что задачу отменили. Он продолжает выполнение, тратит LLM-токены, и в итоге завершит задачу "успешно" — но в БД она cancelled.

**Решение:** Агент должен проверять статус задачи перед каждой итерацией ReAct:
```python
# В _execute_react, внутри цикла:
if iteration % 3 == 0:  # каждые 3 итерации
    task_status = self.platform.get_task_status(task_id)
    if task_status.get('task', {}).get('status') == 'cancelled':
        logger.info(f"Task {task_id[:8]} was cancelled, stopping")
        return {'status': 'cancelled', 'message': 'Задача отменена'}
```

---

## 4. АРХИТЕКТУРНЫЕ УЛУЧШЕНИЯ

### 4.1. ToolRegistry.merge() — отсутствует метод

**Файл:** `agents/base_agent.py:162-165`
**Проблема:** Прямой доступ к приватным атрибутам `extra._tools` и `extra._handlers`. Нарушает инкапсуляцию.

**Решение:**
```python
class ToolRegistry:
    def merge(self, other: 'ToolRegistry'):
        """Объединяет инструменты из другого реестра."""
        self._tools.update(other._tools)
        self._handlers.update(other._handlers)
```

### 4.2. Context overflow — грубая оценка

**Файл:** `agents/base_agent.py:75-77`
**Проблема:** `_estimate_context_size()` считает символы, что грубо (~4x от реального числа токенов). Для Claude правильнее использовать `anthropic.count_tokens()`, для OpenAI — `tiktoken`.

**Рекомендация:** Для MVP текущий подход работает, но при масштабировании стоит добавить tiktoken для точного подсчёта, и сохранять `usage` из предыдущих ответов LLM.

### 4.3. Дублирование парсинга input_data

**Файлы:** Все агенты в `agents/catalog/*.py`
**Проблема:** Каждый агент повторяет один и тот же блок:
```python
input_data = task.get('input_data', '{}')
if isinstance(input_data, str):
    try:
        input_data = json.loads(input_data)
    except (json.JSONDecodeError, ValueError):
        input_data = {}
```

**Решение:** Вынести в `BaseAgent`:
```python
def _parse_input_data(self, task: dict) -> dict:
    input_data = task.get('input_data', '{}')
    if isinstance(input_data, str):
        try:
            return json.loads(input_data)
        except (json.JSONDecodeError, ValueError):
            return {}
    return input_data or {}
```

### 4.4. photo-optimizer зарегистрирован в каталоге, но нет реализации

**Файл:** `services/agent_service.py:108-123` vs `agents/catalog/`
**Проблема:** `photo-optimizer` описан в `AGENT_CATALOG`, но не имеет реализации в `agents/catalog/` и не зарегистрирован в `AGENT_REGISTRY` (`runner.py`). Это "мёртвый" агент.

**Решение:** Либо реализовать, либо убрать из каталога и пометить как "coming soon" на уровне UI.

### 4.5. `structured_output()` — хрупкий JSON-парсинг

**Файл:** `agents/llm.py:157-172, 414-427`
**Проблема:** Парсинг JSON из текстового ответа LLM через strip + startswith("```") — хрупкий подход. LLM может вернуть:
- `json\n{...}\n` (без закрывающих ```)
- Текст до/после JSON блока
- Невалидный JSON

**Решение:** Более надёжный парсинг:
```python
import re

def _extract_json(text: str) -> dict:
    # Пробуем весь текст как JSON
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # Извлекаем из code block
    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Ищем первый { ... } блок
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Cannot extract JSON from LLM response: {text[:200]}")
```

### 4.6. Конфигурация — нет типизации и IDE-поддержки

**Файл:** `agents/config.py`
**Проблема:** Metaclass-based lazy config не даёт IDE автодополнение, type hints, и статический анализ. `AgentConfig.NONEXISTENT_FIELD` вызовет ошибку только в рантайме.

**Рекомендация:** Для зрелой системы стоит перейти на pydantic BaseSettings:
```python
from pydantic_settings import BaseSettings

class AgentConfig(BaseSettings):
    PLATFORM_URL: str = 'http://localhost:5000'
    AGENT_ID: str = ''
    LLM_PROVIDER: Literal['cloudru', 'claude', 'gemini', 'openai_compat'] = 'cloudru'
    # ... итд

    class Config:
        env_file = '.env'
```

---

## 5. ХОРОШИЕ РЕШЕНИЯ (сильные стороны)

Для полноты картины — что сделано хорошо:

1. **ReAct loop** (`base_agent.py:281-410`) — грамотная реализация с защитой от overflow, частичными результатами, и подробным логированием.

2. **Lazy config** (`config.py`) — позволяет `load_dotenv()` до обращения к полям. Простой и рабочий паттерн.

3. **Dead letter protection** (`base_agent.py:229-241`) — задачи, которые падают >3 раз, автоматически пропускаются. Предотвращает бесконечные retry-циклы.

4. **Context overflow protection** (`base_agent.py:80-121`) — суммаризация старых сообщений при переполнении контекста. Хорошо что не просто обрезает, а сохраняет мета-информацию.

5. **Multi-provider LLM** (`llm.py`) — унифицированный интерфейс с 4 провайдерами + fallback для сложных задач. Грамотная декомпозиция.

6. **Rule-based orchestration** (`orchestrator.py:124-156`) — orchestrator не тратит LLM-токены на роутинг, используя keyword matching + predefined pipelines.

7. **Structured JSON logging** (`runner.py:154-193`) — поддержка JSON-формата для агрегации в ELK/Loki.

8. **Sanitized errors** (`base_agent.py:46-64`) — обработка HTML-ответов вместо JSON, обрезка длинных ошибок.

9. **Graceful shutdown** (`base_agent.py:471-478`) — SIGINT/SIGTERM обработка для корректного завершения.

10. **Token-efficient serialization** (`internal_api.py:347-407`) — фильтрация photo URLs и дублирующих данных из ответов API для экономии LLM-токенов.

---

## 6. ПРИОРИТЕТНЫЙ ПЛАН ДЕЙСТВИЙ

### Фаза 1: Стабильность (1-2 недели)
- [ ] Добавить тесты для core-модулей (base_agent, tools, llm, config)
- [ ] Исправить race condition в poll_tasks (SELECT FOR UPDATE)
- [ ] Добавить таймаут зависших задач (requeue_stuck_tasks)
- [ ] Исправить утечку памяти в _task_failures
- [ ] Добавить cancel propagation в ReAct-цикл

### Фаза 2: Наблюдаемость (1 неделя)
- [ ] Трекинг LLM-токенов (usage в каждом ответе)
- [ ] Prometheus метрики (tasks, duration, tokens, errors)
- [ ] Логирование стоимости выполнения задач

### Фаза 3: Зрелость (2-3 недели)
- [ ] Вынести polling подзадач из ReAct в dedicated wait
- [ ] Передача parent_task_id в оркестраторе
- [ ] Валидация tool arguments по JSON Schema
- [ ] Рефакторинг: merge() для ToolRegistry, _parse_input_data() в BaseAgent
- [ ] Rate limiting на Internal API
- [ ] Реализация или удаление photo-optimizer
- [ ] Миграция конфига на pydantic BaseSettings

---

*Ревью проведено: 2026-03-15*
*Автор: Claude Code Agent*
