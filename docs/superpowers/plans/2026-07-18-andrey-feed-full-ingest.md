# Полный приём фида Андрея (sex-opt.ru) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перестать терять данные фида поставщика `andrey` на каждом этапе цепочки «фид → SupplierProduct → ImportedProduct → WB card / Ozon fact pack».

**Architecture:** Расширяем декларативный `csv_column_mapping` поставщика и header-based парсер `SupplierCSVParser._parse_with_mapping` (merge-колонки для списков, сбор несмаппленных колонок, dict-габариты), доводим новые поля до `SupplierProduct` (полный список ШК, РРЦ-fallback, видео, освежаемый `original_data_json`) и до `ImportedProduct` (РРЦ, полные ШК, свежий original_data). Ozon получает данные автоматически: `marketplace_fact_pack.build` читает `original_data` (observed provenance) и уже ждёт `dimensions` как dict и `barcodes` как список.

**Tech Stack:** Flask/SQLAlchemy, SQLite, идемпотентные миграции в `migrations/`, pytest (`SKIP_SCHEDULER=1`).

## Global Constraints

- НЕ коммитить в git: рабочее дерево содержит незакоммиченные изменения другой работы (multimarketplace frontend); деплой у пользователя — rebuild из рабочего дерева. Шаги «Commit» из шаблона скилла заменены на «py_compile + тесты».
- Не переписывать уже развёрнутую миграцию `migrate_add_sexopt_supplier.py` — новая конфигурация едет отдельной миграцией.
- Новые колонки, которые ORM читает при старте, подключаются в `docker-entrypoint.sh` fail-fast (без `|| echo`).
- Тесты без реальных WB/LLM вызовов; scheduler отключён `SKIP_SCHEDULER=1`.
- `AGENTS.md` обновить в том же изменении (миграции + контракт фида).
- Существующее поведение для legacy `sexoptovik` (supplier id=1, позиционный парсер) не менять.

## Каталог фактов (проверено на живом фиде и прод-БД 2026-07-18)

- Фид: 57 колонок, 5942 товара. Не читаем: `category_new_title/code` (100%), `retail_price_minsk` (99.8%), `video` (17%), `modification_code` (66%), `manufacturer` (45%), `marked` (24%), `start_price` (100%), `url` (100%), `group_title` (100%) и др.
- `original_data_json` пишется один раз при создании (supplier_service.py:4242-4243) и содержит только смаппленные поля.
- Баркоды: парсер отдаёт список, но хранится только `barcodes[0]` (supplier_service.py:4234-4235).
- РРЦ (`recommended_retail_price`) не копируется в ImportedProduct — нет колонки.
- `marketplace_fact_pack.build` читает `original.get("dimensions")` только как dict (fact_pack:459-460) — лист андрея пропускается; `original.get("barcodes")` — как список (398-405).
- `_CHAR_ALIASES` в `wb_product_importer._build_wb_characteristics` не знает «Тип батареек».
- `_update_supplier_product` вызывается для каждой строки при каждом sync (sync_from_csv:948-961) — освежение original_data накроет все 6120 строк при первом ресинке.

---

### Task 1: Миграция + модели (barcodes_json, imported РРЦ, новый маппинг andrey)

**Files:**
- Create: `migrations/migrate_andrey_feed_full_ingest.py`
- Modify: `models.py:3297` (SupplierProduct, после `barcode`), `models.py:1142` (ImportedProduct, после блока цен)
- Modify: `docker-entrypoint.sh:172` (после sexopt-миграции, fail-fast)
- Modify: `migrations/run_all_migrations.py` (колонки в блоках supplier_products/imported_products)

**Interfaces:**
- Produces: колонка `supplier_products.barcodes_json TEXT` (JSON list строк), колонка `imported_products.recommended_retail_price FLOAT`, обновлённый `suppliers.csv_column_mapping` для `code='andrey'`.

- [ ] **Step 1: Написать миграцию** — идемпотентный скрипт по образцу `migrate_add_sexopt_supplier.py`: `ADD COLUMN` при отсутствии + `UPDATE suppliers SET csv_column_mapping=? WHERE code='andrey'`. Новый маппинг:

```python
ANDREY_CSV_COLUMN_MAPPING = {
    "external_id": {"column": "code", "type": "string"},
    "vendor_code": {"column": "article", "type": "string"},
    "title": {"column": "title", "type": "string"},
    "brand": {"column": "brand_title", "type": "string"},
    # Новое дерево категорий (100% заполнено) + старое (19%) как дополнение:
    # list с несколькими колонками объединяет значения, первая колонка первой.
    "categories": {"columns": ["category_new_title", "category_title"],
                   "type": "list", "separator": "/"},
    "description": {"column": "description", "type": "string"},
    "country": {"column": "country", "type": "string"},
    "supplier_price": {"column": "price", "type": "number"},
    "recommended_retail_price": {"column": "retail_price", "type": "number"},
    "recommended_retail_price_fallback": {"column": "retail_price_minsk", "type": "number"},
    "video_url": {"column": "video", "type": "string"},
    "colors": {"column": "color", "type": "list", "separator": ","},
    "materials": {"column": "material", "type": "list", "separator": ","},
    "sizes_raw": {"column": "size", "type": "string"},
    "barcodes": {"column": "barcodes", "type": "list", "separator": ","},
    "supplier_quantity": {"columns": ["msk", "spb", "tmn", "rst", "nsk", "ast", "kdr"],
                          "type": "stock_sum"},
    "photo_urls": {"columns": ["image", "image1", "image2", "images"],
                   "columns_prefix": "image", "separator": ",", "type": "photo_urls"},
    "characteristics": {"columns": {"length": "Длина, см", "width": "Ширина, см",
                                    "weight": "Вес, кг", "battery": "Тип батареек",
                                    "waterproof": "Водонепроницаемость",
                                    "manufacturer": "Производитель"},
                        "type": "characteristics"},
    "dimensions": {"columns": {"width_packed": "Ширина упаковки, см",
                               "height_packed": "Высота упаковки, см",
                               "length_packed": "Длина упаковки, см",
                               "weight_packed": "Вес упаковки, кг"},
                   "type": "characteristics"},
    "_include_unmapped": True,
}
```

- [ ] **Step 2: Модели** — `SupplierProduct.barcodes_json = db.Column(db.Text)` (комментарий: полный список ШК, JSON), `ImportedProduct.recommended_retail_price = db.Column(db.Float, nullable=True)`.
- [ ] **Step 3: Подключить** — `docker-entrypoint.sh` fail-fast строкой `python migrations/migrate_andrey_feed_full_ingest.py /app/data/seller_platform.db` после sexopt-миграции; `run_all_migrations.py` — обе колонки в соответствующие списки.
- [ ] **Step 4: Проверка** — `python -m py_compile migrations/migrate_andrey_feed_full_ingest.py models.py`; прогнать миграцию на копии-пустышке (`sqlite3` tmp с таблицами suppliers/supplier_products/imported_products) дважды — идемпотентность.

### Task 2: Парсер — merge-list, raw_extra, dict-габариты

**Files:**
- Modify: `services/supplier_service.py:246-542` (`_parse_with_mapping`, `_resolve_mapping_config`, `_extract_fields_by_mapping`)
- Test: `tests/test_andrey_feed_ingest.py` (новый)

**Interfaces:**
- Produces: parsed dict дополнительно содержит `recommended_retail_price_fallback: float|None`, `video_url: str`, `raw_extra: dict[str,str]` (несмаппленные непустые колонки), `dimensions: dict[str,str]` (имя→значение вместо списка), `categories` объединяет несколько колонок.

- [ ] **Step 1: Тест на парсер** — собрать CSV-строку с заголовком фида (полный 57-колоночный заголовок из аудита) и 2 товарами; supplier-стаб (SimpleNamespace: `code='andrey'`, `csv_column_mapping=ANDREY_CSV_COLUMN_MAPPING`, `csv_has_header=True`, `csv_delimiter=';'`). Ассерты: категории объединены (новая цепочка первой), `barcodes` — список из 2 ШК, `dimensions` — dict с «Ширина упаковки, см», `recommended_retail_price_fallback` распарсен, `video_url` есть, `raw_extra` содержит `manufacturer`, `marked`, `url`, `modification_code`, `start_price`; при `_include_unmapped=False`/отсутствии — `raw_extra` нет.
- [ ] **Step 2: Реализация**:
  - `'list'`-ветка: если в config есть `columns` (список индексов) — объединить значения всех колонок по порядку с дедупликацией (для `categories` → `all_categories` merged, `category` = первый элемент).
  - `_parse_with_mapping`: если `mapping.get('_include_unmapped')` и header-режим — собрать `unmapped = {idx: name}` для заголовков, не участвующих ни в одном resolved config (`column` int, `columns` list/dict), и передать в `_extract_fields_by_mapping(row, resolved_mapping, unmapped_columns=unmapped)`; там собрать `raw_extra` из непустых ячеек.
  - Финализация `_extra_dimensions` → dict: `product['dimensions'] = {c['name']: c['value'] for c in ...}` (последнее значение при дубле имени).
  - Проверить `CSVPreValidator.validate` и `DataNormalizer.normalize_product_list` на толерантность к новым ключам (`_include_unmapped` — bool, оба цикла парсера уже пропускают не-dict; нормализатор — прочитать и убедиться, что неизвестные ключи проходят насквозь).
- [ ] **Step 3: Прогнать** `SKIP_SCHEDULER=1 python -m pytest -q tests/test_andrey_feed_ingest.py` — зелёный.

### Task 3: Запись в SupplierProduct и доводка до ImportedProduct

**Files:**
- Modify: `services/supplier_service.py:4215-4276` (`_update_supplier_product`), `:4396-4449` (`_copy_to_imported_product`), `:4452-4495` (`_update_imported_from_supplier`)
- Test: `tests/test_andrey_feed_ingest.py` (дополнить)

**Interfaces:**
- Consumes: parsed dict из Task 2.
- Produces: `sp.barcodes_json` (полный список), `sp.video_url`, `sp.recommended_retail_price` (с fallback), `sp.original_data_json` освежается каждым sync; `imp.barcodes` — полный список, `imp.recommended_retail_price`, `imp.original_data` освежается при явном «Обновить карточки».

- [ ] **Step 1: Тесты** (in-memory SQLite app context по образцу существующих тестов):
  - `_update_supplier_product` дважды с разными data → `original_data_json` отражает последний sync (включая `raw_extra`).
  - barcodes: список → `barcode == first`, `barcodes_json == полный список`.
  - РРЦ: только fallback → пишется fallback; оба → приоритет у `recommended_retail_price`.
  - `video_url` пишется.
  - `_copy_to_imported_product`: `imp.barcodes` = полный список (fallback `[sp.barcode]` без barcodes_json), `imp.recommended_retail_price` скопирована.
  - `_update_imported_from_supplier`: освежает barcodes/РРЦ/original_data.
- [ ] **Step 2: Реализация**:
  - `_update_supplier_product`: `if 'barcodes' in data:` → `sp.barcode = first` (как было) **и** `sp.barcodes_json = json.dumps(data['barcodes'])` при непустом; РРЦ-блок дополнить `elif data.get('recommended_retail_price_fallback'):`; `if data.get('video_url', '').startswith('http'): sp.video_url = ...`; `original_data_json` — писать всегда (убрать `if not sp.original_data_json`).
  - `_copy_to_imported_product`: barcodes из `sp.barcodes_json` (json.loads с fallback `[sp.barcode]`), `recommended_retail_price=sp.recommended_retail_price`.
  - `_update_imported_from_supplier`: `imp.barcodes = sp.barcodes_json or imp.barcodes` (тот же fallback-хелпер), `imp.recommended_retail_price = sp.recommended_retail_price if sp.recommended_retail_price is not None else imp.recommended_retail_price`, `imp.original_data = sp.original_data_json or imp.original_data`.
- [ ] **Step 3: Прогнать** тесты файла + смежные: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_andrey_feed_ingest.py tests/test_supplier_photo_mapping.py tests/test_supplier_update_hub.py`.

### Task 4: WB-алиас «Тип батареек»

**Files:**
- Modify: `services/wb_product_importer.py:2272-2361` (`_CHAR_ALIASES` — вынести в модульную константу `CHAR_ALIASES`, добавить запись)
- Test: `tests/test_andrey_feed_ingest.py` (дополнить)

- [ ] **Step 1: Тест** — `from services.wb_product_importer import CHAR_ALIASES; assert CHAR_ALIASES['тип батареек'] == 'тип элемента питания'`.
- [ ] **Step 2: Реализация** — поднять словарь на уровень модуля (имя `CHAR_ALIASES`), в методе ссылаться на него, добавить `'тип батареек': 'тип элемента питания'`.
- [ ] **Step 3: Прогнать** `SKIP_SCHEDULER=1 python -m pytest -q tests/test_andrey_feed_ingest.py tests/test_wb_product_importer_strict_matching.py`.

### Task 5: Документация, память, финальная проверка

**Files:**
- Modify: `AGENTS.md` (список миграций + карта репозитория/поставщики: контракт полного приёма фида andrey)
- Modify: память `project_andrey_feed_audit.md` (статус: реализовано, что осталось)

- [ ] **Step 1:** AGENTS.md — добавить `python migrations/migrate_andrey_feed_full_ingest.py data/seller_platform.db` в список; короткий абзац: header-based маппинг andrey читает merge-категории/РРЦ-fallback/видео/полные ШК, несмаппленные колонки сохраняются в `original_data_json.raw_extra`, `original_data_json` освежается каждым sync (наблюдённые данные поставщика, provenance observed для Ozon fact pack).
- [ ] **Step 2:** Полный смок: `python -m py_compile` изменённых файлов, `git diff --check`, `SKIP_SCHEDULER=1 python -m pytest -q tests/test_andrey_feed_ingest.py tests/test_supplier_photo_mapping.py tests/test_supplier_update_hub.py tests/test_wb_product_importer_strict_matching.py tests/test_supplier_catalog_enrichment.py`.
- [ ] **Step 3:** Операционные шаги (пользователю, не выполнять): rebuild контейнера (миграция отработает в entrypoint) → админка: ресинк каталога andrey (заполнит новые поля и original_data у всех 6120) → массовое обогащение категорий на хвост из ~5291 товара в предмете 5038 → затем режим характеристик.

## Отложено сознательно (не в этом изменении)

- Публикация видео в WB/Ozon и склейка вариантов по `modification_code` — новая функциональность, отдельное обсуждение.
- Расширение контракта `marketplace_fact_pack` новыми ключами (manufacturer/РРЦ/marked) — контракт версионирован; данные уже сохраняются в `original_data.raw_extra`, взять их — отдельный versioned diff.
- Чтение `ai_parsed_data_json` при WB-публикации — идёт через существующий канал `ai_marketplace_json` + enrichment, не трогаем.
