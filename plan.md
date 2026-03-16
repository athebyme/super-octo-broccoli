# Plan: Orchestrator Agent + Smart UI

## Проблема сейчас
- Пользователь видит 9 отдельных агентов и должен сам выбирать нужного
- Каждый агент работает изолированно — нет цепочек (категория → характеристики → SEO)
- UI на продуктах показывает длинный dropdown из 9 агентов — непонятно что выбрать
- Нет "сделай мне хорошо" кнопки — нужно вручную запускать каждый шаг
- auto-importer пытается делать всё в одном ReAct-цикле, но это ненадёжно

## Архитектура решения

### 1. Новый OrchestratorAgent (`agents/catalog/orchestrator.py`)

Мета-агент, который:
- Принимает **свободный текст** от пользователя ИЛИ **пресет** (pipeline)
- Анализирует задачу и разбивает на подзадачи
- Создаёт AgentTask для каждого нужного агента через platform API
- Следит за выполнением подзадач
- Собирает результаты и отдаёт итог

**Не использует LLM для роутинга** (экономия токенов) — роутинг по правилам:

```python
# Маппинг пресетов → цепочки агентов
PIPELINES = {
    'full_prepare': ['category-mapper', 'characteristics-filler', 'seo-writer', 'card-doctor'],
    'import_ready': ['category-mapper', 'brand-resolver', 'characteristics-filler', 'seo-writer', 'size-normalizer', 'card-doctor'],
    'seo_boost': ['seo-writer', 'card-doctor'],
    'audit': ['card-doctor', 'price-optimizer', 'review-analyst'],
    'category_fix': ['category-mapper', 'characteristics-filler'],
}

# Для свободного текста — keyword matching
KEYWORDS_TO_AGENTS = {
    'категори': 'category-mapper',
    'seo': 'seo-writer',
    'заголов': 'seo-writer',
    'описани': 'seo-writer',
    'бренд': 'brand-resolver',
    'размер': 'size-normalizer',
    'характерист': 'characteristics-filler',
    'цен': 'price-optimizer',
    'модерац': 'card-doctor',
    'блокировк': 'card-doctor',
    'отзыв': 'review-analyst',
    'импорт': 'auto-importer',
    'подготов': 'full_prepare',  # → pipeline
}
```

### 2. Новые инструменты в tools.py

```python
# Создать подзадачу для другого агента
'create_subtask': {
    'agent_name': str,     # 'seo-writer'
    'task_type': str,      # 'seo_batch'
    'input_data': dict,    # {product_ids: [...], seller_id: 1}
    'title': str,
    'depends_on': str,     # task_id предыдущей задачи (опционально)
}

# Проверить статус подзадачи
'get_subtask_status': {
    'task_id': str,
}

# Получить результат завершённой подзадачи
'get_subtask_result': {
    'task_id': str,
}
```

Platform client добавляет:
```python
def create_task(self, agent_name, task_type, seller_id, title, input_data, parent_task_id=None):
    """Создаёт задачу для указанного агента."""
    return self._request('POST', '/tasks/create', json={...})

def get_task_status(self, task_id):
    return self._request('GET', f'/tasks/{task_id}')
```

### 3. Модель данных — parent_task_id

В `AgentTask` добавляем:
```python
parent_task_id = db.Column(UUID, db.ForeignKey('agent_tasks.id'), nullable=True)
subtasks = db.relationship('AgentTask', backref=db.backref('parent_task', remote_side=[id]))
```

Это позволит:
- Показывать дерево задач в UI (оркестратор → подзадачи)
- Трекать прогресс pipeline целиком
- Отменять все подзадачи при отмене оркестратора

### 4. Internal API — новые эндпоинты

```python
# POST /internal/v1/tasks/create — создание задачи агентом (для оркестратора)
# GET /internal/v1/tasks/{id} — получить статус задачи
# GET /internal/v1/tasks/{id}/result — получить результат задачи
```

### 5. UI — "Умный помощник" вместо dropdown агентов

#### A. На странице товара — заменяем AI dropdown на:

```
┌─────────────────────────────────────────────┐
│  🤖 AI-помощник                    [▼]      │
│                                              │
│  Быстрые действия:                           │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐    │
│  │ Подготовить│ │ SEO      │ │ Проверить│    │
│  │ к WB      │ │ тексты   │ │ карточку │    │
│  └──────────┘ └──────────┘ └──────────┘    │
│                                              │
│  💬 Или опиши что нужно:                    │
│  ┌──────────────────────────────────────┐   │
│  │ Подбери категорию и напиши описание  │   │
│  └──────────────────────────────────────┘   │
│                                    [Запустить]│
└─────────────────────────────────────────────┘
```

Пресеты:
- **"Подготовить к WB"** → pipeline `full_prepare` (категория → характеристики → SEO → модерация)
- **"SEO тексты"** → pipeline `seo_boost` (SEO → модерация)
- **"Проверить карточку"** → single `card-doctor:diagnose_single`
- **"Свободный текст"** → оркестратор парсит и выбирает нужных агентов

#### B. На странице списка товаров — bulk вариант:

```
┌─────────────────────────────────────────────┐
│ Выбрано: 15 товаров                          │
│                                              │
│ [Подготовить к WB] [SEO пакетом] [Аудит]    │
│                                              │
│ 💬 ────────────────────────── [Запустить]    │
└─────────────────────────────────────────────┘
```

#### C. Страница задачи оркестратора — pipeline view:

```
┌─────────────────────────────────────────────┐
│ 📋 Подготовка к WB — 15 товаров              │
│ ═══════════════════════════════ 60%          │
│                                              │
│ ✅ Категории          7/15 товаров   0:42   │
│ ⏳ Характеристики     3/15 товаров   0:18   │
│ ⏸️ SEO тексты          ожидание              │
│ ⏸️ Модерация           ожидание              │
│                                              │
│ Общее время: 1:00  │  Ошибок: 0             │
└─────────────────────────────────────────────┘
```

### 6. Синхронизация с существующими фичами

**С auto_import:**
- Оркестратор заменяет auto-importer для pipeline задач
- auto-importer остаётся для простых одиночных импортов
- На странице `/auto-import/products` кнопка "AI обработка" → оркестратор с пресетом `import_ready`

**С enrichment:**
- Enrichment из supplier data (`/products/<id>/enrich`) остаётся как отдельная фича
- Оркестратор может запускаться ПОСЛЕ enrichment — пресет "Подготовить к WB"
- На странице enrichment добавляем кнопку "Продолжить с AI" → оркестратор

**С product_detail и products list:**
- Старый AI dropdown заменяется на новый компонент
- Отдельные агенты доступны через "Ещё →" в dropdown (для опытных)

### 7. План файлов

Новые файлы:
```
agents/catalog/orchestrator.py          — OrchestratorAgent
templates/partials/ai_assistant.html    — переиспользуемый UI компонент
templates/agent_pipeline_detail.html    — страница pipeline задачи
```

Изменяемые файлы:
```
models.py                    — parent_task_id в AgentTask
agents/tools.py              — create_subtask, get_subtask_status, get_subtask_result
agents/platform_client.py    — create_task, get_task_status endpoints
services/agent_service.py    — добавить orchestrator в каталог, create_task from agent
routes/agents.py             — pipeline detail route, create orchestrator task API
templates/product_detail.html — заменить AI dropdown на ai_assistant partial
templates/products.html      — заменить bulk AI dropdown
templates/agents.html        — показать pipelines на dashboard
```

### 8. Порядок реализации

**Phase 1 — Backend (оркестратор)**
1. Миграция: `parent_task_id` в AgentTask
2. Internal API: `POST /tasks/create`, `GET /tasks/{id}/result`
3. Новые tools: `create_subtask`, `get_subtask_status`, `get_subtask_result`
4. `OrchestratorAgent` с rule-based routing и PIPELINES
5. Регистрация в AGENT_CATALOG

**Phase 2 — UI (умный помощник)**
6. `ai_assistant.html` partial — пресеты + свободный ввод
7. Pipeline detail page с progress по подзадачам
8. Интеграция в product_detail и products list
9. Интеграция в auto_import pages

**Phase 3 — Polish**
10. Dashboard: секция "Активные pipelines"
11. Уведомления о завершении pipeline
12. Отмена pipeline = отмена всех подзадач
