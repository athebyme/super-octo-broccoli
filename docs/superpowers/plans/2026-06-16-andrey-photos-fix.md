# Andrey 3-Photos Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop supplier «Андрей» (and any header-mapped supplier) from being capped at 3 product photos by making the CSV photo-column mapping auto-discover all `image*` columns.

**Architecture:** Root cause: `migrations/migrate_add_sexopt_supplier.py` maps the photo field to exactly three columns `["image","image1","image2"]`, and `SupplierService._extract_fields_by_mapping` (`services/supplier_service.py:431-441`) reads only the columns listed in the mapping. Fix in two layers: (1) a robust, reusable prefix-discovery helper used by `_resolve_mapping_config` so a `photo_urls` field can declare `columns_prefix` instead of (or in addition to) explicit `columns`; (2) switch Andrey's config to use the prefix. Confirm against the live feed/DB first; the code fix is safe either way (if the feed really has only 3 image columns, discovery yields the same 3).

**Tech Stack:** Python 3.11, stdlib `unittest`, SQLite, raw-csv parsing in `services/supplier_service.py`.

## Global Constraints

- Tests use stdlib **unittest**, not pytest (pytest is not installed). Run one module with: `venv/bin/python -m unittest tests.<module> -v` from repo root `/home/athebyme/super-octo-broccoli`.
- Test files live in `tests/test_*.py` as `unittest.TestCase` subclasses; imports are top-level absolute (`from services.supplier_service import ...`).
- Source comments/docstrings/UI strings in Russian; files start with `# -*- coding: utf-8 -*-`.
- Backward compatibility is mandatory: existing mappings that use explicit `columns` (list/dict) or `column` must behave exactly as before.

---

### Task 0: Confirm root cause on the live environment (PREREQUISITE, no code)

This task is verification only — it does not change code, but its result decides whether the fix is needed and which `columns_prefix` value to use. The checkout DB is stale (no `suppliers`/`supplier_products` tables), so run on the live/production DB.

- [ ] **Step 1: Confirm the photo cap in the production DB**

Run (production `seller_platform.db`):
```sql
SELECT sp.external_id, json_array_length(sp.photo_urls_json) AS n
FROM supplier_products sp JOIN suppliers s ON s.id = sp.supplier_id
WHERE s.code = 'andrey' ORDER BY n DESC LIMIT 20;
```
Expected if bug present: every row `n = 3`.

- [ ] **Step 2: List the real image columns in the live feed header**

Fetch the header row of Andrey's feed (`csv_source_url` from `migrations/migrate_add_sexopt_supplier.py:149-156`, may require the supplier's `auth_login`/`auth_password`) and list every column name matching `image*`.
- If there are more than 3 (`image3`, `image4`, …) → the bug is real, proceed with the fix and use prefix `image`.
- If there are exactly 3 → 3 photos is the supplier's true maximum; STOP, the cap is upstream, no code change needed.
- If photo URLs are packed into ONE column with a `;`/`,` separator → use the delimiter variant in Task 2 Step 8 instead of prefix discovery.

- [ ] **Step 3: Record the finding**

Write the confirmed column list / decision into this plan file under Task 0 before continuing.

---

### Task 1: Add the `discover_columns_by_prefix` helper

**Files:**
- Modify: `services/supplier_service.py` (add a module-level function near the top, after imports)
- Test: `tests/test_supplier_photo_mapping.py` (create)

**Interfaces:**
- Produces: `discover_columns_by_prefix(header_index: dict, prefix: str) -> list[int]` — returns the column indices whose header matches `^<prefix>\d*$`, ordered so the bare prefix (`image`) comes first, then numeric suffixes ascending (`image1`, `image2`, … `image10`). Used by Task 2.

- [ ] **Step 1: Write the failing test**

Create `tests/test_supplier_photo_mapping.py`:
```python
# -*- coding: utf-8 -*-
"""Тесты авто-обнаружения фото-колонок по префиксу и резолва маппинга."""

import unittest

from services.supplier_service import discover_columns_by_prefix


class TestDiscoverColumnsByPrefix(unittest.TestCase):
    def test_finds_all_image_columns_in_numeric_order(self):
        header_index = {
            'name': 0, 'price': 1,
            'image': 5, 'image1': 6, 'image2': 7, 'image10': 15, 'image3': 8,
        }
        result = discover_columns_by_prefix(header_index, 'image')
        # bare 'image' first, then numeric suffixes ascending (10 after 3, not after 1)
        self.assertEqual(result, [5, 6, 7, 8, 15])

    def test_ignores_non_matching_headers(self):
        header_index = {'image': 5, 'image_big': 6, 'thumbnail': 7, 'image2': 8}
        # 'image_big' has a non-digit suffix -> must NOT match
        self.assertEqual(discover_columns_by_prefix(header_index, 'image'), [5, 8])

    def test_returns_empty_when_no_match(self):
        self.assertEqual(discover_columns_by_prefix({'a': 0, 'b': 1}, 'image'), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m unittest tests.test_supplier_photo_mapping -v`
Expected: FAIL with `ImportError: cannot import name 'discover_columns_by_prefix'`.

- [ ] **Step 3: Implement the helper**

In `services/supplier_service.py`, confirm `import re` is present at the top (add it if missing), then add this module-level function above the `SupplierService` class:
```python
def discover_columns_by_prefix(header_index: dict, prefix: str) -> list:
    """Найти индексы колонок, заголовки которых матчат ^<prefix>\\d*$.

    Возвращает индексы в порядке: голый префикс (image) первым, затем
    числовые суффиксы по возрастанию (image1, image2, ..., image10).
    """
    pattern = re.compile(rf'^{re.escape(prefix)}(\d*)$')
    matched = []
    for name, idx in header_index.items():
        m = pattern.match(name)
        if m:
            suffix = m.group(1)
            order = int(suffix) if suffix else -1  # голый префикс — первым
            matched.append((order, idx))
    matched.sort(key=lambda pair: pair[0])
    return [idx for _, idx in matched]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m unittest tests.test_supplier_photo_mapping -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add services/supplier_service.py tests/test_supplier_photo_mapping.py
git commit -m "feat(supplier): add discover_columns_by_prefix helper for photo columns"
```

---

### Task 2: Wire `columns_prefix` into mapping resolution + delimiter support

**Files:**
- Modify: `services/supplier_service.py:360-402` (`_resolve_mapping_config`) and `:431-441` (`_extract_fields_by_mapping` photo_urls branch)
- Test: `tests/test_supplier_photo_mapping.py` (extend)

**Interfaces:**
- Consumes: `discover_columns_by_prefix` (Task 1).
- Produces: a `photo_urls` mapping config may now include `"columns_prefix": "image"` (resolved to all matching column indices) and/or `"separator"` (split a single column holding multiple URLs). Explicit `columns` continue to work and are merged with discovered ones (explicit first, dedup).

- [ ] **Step 1: Write the failing test for prefix resolution**

Add to `tests/test_supplier_photo_mapping.py`:
```python
from services.supplier_service import SupplierService


class TestResolveMappingConfigPrefix(unittest.TestCase):
    def setUp(self):
        # _resolve_mapping_config does not use any attribute of self except being
        # a bound method; pass a bare object as self to avoid building a Supplier.
        self.resolve = SupplierService._resolve_mapping_config.__get__(object())

    def test_columns_prefix_discovers_all_image_columns(self):
        header_index = {'image': 5, 'image1': 6, 'image2': 7, 'image3': 8}
        cfg = {'type': 'photo_urls', 'columns_prefix': 'image'}
        resolved = self.resolve(cfg, header_index)
        self.assertEqual(resolved['columns'], [5, 6, 7, 8])

    def test_explicit_columns_still_work_without_prefix(self):
        header_index = {'image': 5, 'image1': 6, 'image2': 7}
        cfg = {'type': 'photo_urls', 'columns': ['image', 'image1', 'image2']}
        resolved = self.resolve(cfg, header_index)
        self.assertEqual(resolved['columns'], [5, 6, 7])

    def test_explicit_columns_merge_with_prefix_no_duplicates(self):
        header_index = {'image': 5, 'image1': 6, 'image2': 7, 'image3': 8}
        cfg = {'type': 'photo_urls', 'columns': ['image'], 'columns_prefix': 'image'}
        resolved = self.resolve(cfg, header_index)
        self.assertEqual(resolved['columns'], [5, 6, 7, 8])
```

- [ ] **Step 2: Run to verify it fails**

Run: `venv/bin/python -m unittest tests.test_supplier_photo_mapping.TestResolveMappingConfigPrefix -v`
Expected: FAIL — `test_columns_prefix_discovers_all_image_columns` gets no `columns` key (KeyError) because prefix is not handled yet.

- [ ] **Step 3: Implement prefix resolution in `_resolve_mapping_config`**

In `services/supplier_service.py`, inside `_resolve_mapping_config`, AFTER the existing `cols = config.get('columns')` block that handles list/dict (right before `return resolved`), add:
```python
        # Авто-обнаружение колонок по префиксу (например, все image, image1..imageN)
        prefix = config.get('columns_prefix')
        if isinstance(prefix, str) and prefix:
            discovered = discover_columns_by_prefix(header_index, prefix)
            existing = resolved.get('columns')
            merged = list(existing) if isinstance(existing, list) else []
            for idx in discovered:
                if idx not in merged:
                    merged.append(idx)
            resolved['columns'] = merged
```

- [ ] **Step 4: Run to verify it passes**

Run: `venv/bin/python -m unittest tests.test_supplier_photo_mapping.TestResolveMappingConfigPrefix -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Write the failing test for the extraction branch (incl. delimiter)**

Add to `tests/test_supplier_photo_mapping.py`:
```python
class TestExtractPhotoUrls(unittest.TestCase):
    def setUp(self):
        self.extract = SupplierService._extract_fields_by_mapping.__get__(object())

    def test_collects_all_mapped_columns(self):
        row = ['n', 'p', '', '', '', 'http://a/1.jpg', 'http://a/2.jpg', 'http://a/3.jpg', 'http://a/4.jpg']
        mapping = {'photo_urls': {'type': 'photo_urls', 'columns': [5, 6, 7, 8]}}
        product = self.extract(row, mapping)
        self.assertEqual(product['photo_urls'],
                         [{'original': 'http://a/1.jpg'}, {'original': 'http://a/2.jpg'},
                          {'original': 'http://a/3.jpg'}, {'original': 'http://a/4.jpg'}])

    def test_single_column_with_separator_splits(self):
        row = ['n', 'http://a/1.jpg; http://a/2.jpg ; http://a/3.jpg']
        mapping = {'photo_urls': {'type': 'photo_urls', 'columns': [1], 'separator': ';'}}
        product = self.extract(row, mapping)
        self.assertEqual(product['photo_urls'],
                         [{'original': 'http://a/1.jpg'}, {'original': 'http://a/2.jpg'},
                          {'original': 'http://a/3.jpg'}])

    def test_skips_blank_and_non_http(self):
        row = ['n', 'http://a/1.jpg', '', 'not-a-url', 'http://a/2.jpg']
        mapping = {'photo_urls': {'type': 'photo_urls', 'columns': [1, 2, 3, 4]}}
        product = self.extract(row, mapping)
        self.assertEqual(product['photo_urls'],
                         [{'original': 'http://a/1.jpg'}, {'original': 'http://a/2.jpg'}])
```

- [ ] **Step 6: Run to verify the new delimiter test fails**

Run: `venv/bin/python -m unittest tests.test_supplier_photo_mapping.TestExtractPhotoUrls -v`
Expected: `test_collects_all_mapped_columns` and `test_skips_blank_and_non_http` PASS (current behavior already handles them); `test_single_column_with_separator_splits` FAILS (current code appends the whole cell as one URL because it does not split).

- [ ] **Step 7: Implement delimiter splitting in the `photo_urls` branch**

In `services/supplier_service.py`, replace the `photo_urls` branch body (`:431-441`) with a version that splits delimited cells:
```python
            if field_type == 'photo_urls':
                # Сборка фото из нескольких колонок (прямые URL).
                # Если в колонке несколько URL через разделитель — разбиваем.
                columns = config.get('columns', [])
                sep = config.get('separator')
                photos = []
                for col_idx in columns:
                    if isinstance(col_idx, int) and 0 <= col_idx < len(row):
                        cell = row[col_idx].strip()
                        if not cell:
                            continue
                        parts = cell.split(sep) if sep else [cell]
                        for part in parts:
                            url = part.strip()
                            if url and url.startswith('http'):
                                photos.append({'original': url})
                product['photo_urls'] = photos
                continue
```

- [ ] **Step 8: Run to verify all extraction tests pass**

Run: `venv/bin/python -m unittest tests.test_supplier_photo_mapping -v`
Expected: PASS (all tests across all classes in the module).

- [ ] **Step 9: Commit**

```bash
git add services/supplier_service.py tests/test_supplier_photo_mapping.py
git commit -m "feat(supplier): support columns_prefix + delimiter for photo_urls mapping"
```

---

### Task 3: Switch Andrey's config to prefix discovery

**Files:**
- Modify: `migrations/migrate_add_sexopt_supplier.py:58-62` (the `photo_urls` mapping entry)

**Interfaces:**
- Consumes: the `columns_prefix` support from Task 2.

- [ ] **Step 1: Update the mapping**

In `migrations/migrate_add_sexopt_supplier.py`, replace the `photo_urls` entry (`:58-62`) with the prefix form (keep the explicit columns as a floor for backward-compatible parsing if Task 0 confirmed the real header names; otherwise use prefix only):
```python
    # Фото (прямые URL — все колонки image, image1..imageN автоматически)
    "photo_urls": {
        "columns": ["image", "image1", "image2"],
        "columns_prefix": "image",
        "type": "photo_urls"
    },
```

- [ ] **Step 2: Re-run the idempotent migration on the target DB**

The migration UPDATEs the existing supplier row's `csv_column_mapping` (`migrate_add_sexopt_supplier.py:162-178`).
Run (local): `venv/bin/python migrations/migrate_add_sexopt_supplier.py`
Run (Docker): `docker exec seller-platform python /app/migrations/migrate_add_sexopt_supplier.py`
Expected: log line confirming supplier «Андрей (sex-opt.ru)» updated.

- [ ] **Step 3: Verify the persisted mapping**

```sql
SELECT json_extract(csv_column_mapping, '$.photo_urls') FROM suppliers WHERE code='andrey';
```
Expected: includes `"columns_prefix":"image"`.

- [ ] **Step 4: Commit**

```bash
git add migrations/migrate_add_sexopt_supplier.py
git commit -m "fix(supplier): auto-discover all image columns for Andrey (sex-opt.ru)"
```

---

### Task 4: Backfill and verify

**Files:** none (operational)

- [ ] **Step 1: Re-import Andrey's catalog**

Trigger a re-parse/import of supplier `andrey` (admin supplier sync UI, or the supplier import job). This rebuilds `supplier_products.photo_urls_json` using the new mapping.

- [ ] **Step 2: Confirm photo counts increased**

```sql
SELECT MAX(json_array_length(photo_urls_json)) AS max_photos,
       AVG(json_array_length(photo_urls_json)) AS avg_photos
FROM supplier_products sp JOIN suppliers s ON s.id = sp.supplier_id
WHERE s.code='andrey';
```
Expected: `max_photos > 3` (matching the real number of image columns found in Task 0).

- [ ] **Step 3: Confirm downstream**

For one re-imported product, confirm `routes/photos.py:generate_public_photo_urls` emits more than 3 URLs and the WB card preview shows all photos.

## Self-Review

- **Spec coverage:** §4.1 parser fix → Tasks 1–2; §4.2 config → Task 3; §4.3 backfill → Task 4; §3 confirmation → Task 0; §5 tests → Tasks 1–2 unit tests. All covered.
- **Placeholders:** none — every code step shows full code; the only deferred value is the real feed column list (Task 0), which is an environment fact, not a code placeholder.
- **Type consistency:** `discover_columns_by_prefix(header_index, prefix) -> list[int]` defined in Task 1 and consumed in Task 2 with matching signature; `_resolve_mapping_config(config, header_index)` and `_extract_fields_by_mapping(row, mapping)` match the existing source signatures.
