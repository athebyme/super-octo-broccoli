# Курируемая база знаний AI-помощника

RAG предназначен только для неструктурированных, проверенных правил и
инструкций. Товары, цены, остатки, категории, характеристики, настройки и
live-статусы остаются в typed SQL/internal API и не индексируются.

## Добавление документа

Сначала сохраните проверенный текст в UTF-8 Markdown или plain text. Затем
добавьте его как неизменяемую версию:

```bash
SKIP_SCHEDULER=1 python scripts/manage_agent_knowledge.py ingest /tmp/wb-rule.md \
  --title "Правила заполнения описания WB" \
  --source-key wb/content/description \
  --source-type wb_official \
  --source-uri https://seller.wildberries.ru/instructions/example \
  --version 2026-07-14 \
  --valid-until 2026-10-14T00:00:00
```

`wb_official` принимает только HTTPS URL доменов Wildberries. Для внутренних
инструкций платформы используется `platform_guide` + `sellerhub://...`. Для
tenant-инструкции обязательны `seller_policy`, `seller://...` или HTTPS URL и
`--seller-id`. Повторная загрузка той же версии с другим содержимым отклоняется;
новая версия атомарно архивирует предыдущую.
Для `wb_official` и `official_reference` обязателен будущий `--valid-until`:
после этого срока документ fail-closed исключается из retrieval до явной проверки
и загрузки новой версии.

Проверка поиска без LLM:

```bash
SKIP_SCHEDULER=1 python scripts/manage_agent_knowledge.py search \
  --seller-id 1 "какие требования к описанию карточки"
```

## Evaluation dataset

JSON-файл содержит узкие вопросы и ожидаемый `source_key`:

```json
[
  {
    "query": "какие требования к описанию карточки",
    "expected_source_key": "wb/content/description"
  }
]
```

Запуск метрик:

```bash
SKIP_SCHEDULER=1 python scripts/manage_agent_knowledge.py evaluate \
  --seller-id 1 --limit 6 /tmp/knowledge-eval.json
```

Команда возвращает Recall@K, MRR и ранг ожидаемого документа для каждого
вопроса. Перед расширением retrieval на embeddings, RAPTOR или GraphRAG сначала
фиксируйте реальные misses в таком наборе.

В репозитории есть один явно отобранный стартовый документ
`knowledge/curated/wb-card-creation-2026-06-25.md` и его smoke-evaluation
`knowledge/evals/wb-card-core.json`. Он не индексируется скрыто: загрузите его
той же командой `ingest` с `source_key=wb/cards/create`, версией `2026-06-25`,
официальным URL и ограниченным `valid_until`, затем прогоните evaluation.

```bash
SKIP_SCHEDULER=1 python scripts/manage_agent_knowledge.py ingest \
  knowledge/curated/wb-card-creation-2026-06-25.md \
  --title "Создание и заполнение карточки товара WB" \
  --source-key wb/cards/create \
  --source-type wb_official \
  --source-uri "https://seller.wildberries.ru/instructions/ru/kg/material/how-to-create-card?recommended=true" \
  --version 2026-06-25 \
  --valid-until 2026-09-25T00:00:00

SKIP_SCHEDULER=1 python scripts/manage_agent_knowledge.py evaluate \
  --seller-id 1 --limit 6 knowledge/evals/wb-card-core.json
```
