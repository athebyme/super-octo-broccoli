# Card Quality (WB Rating) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a "Качество карточек" feature that surfaces WB's native card rating (`productRating` 0–10, `feedbackRating` 0–5) alongside a deterministic in-house Quality Score (0–100) with per-dimension breakdown, recommendations, trend history, and on-demand AI deep-analysis.

**Architecture:** Two independent axes plus an AI layer. WB ratings are pulled for the whole active catalog by a new APScheduler job that reuses the EXISTING `WildberriesAPIClient.get_sales_funnel_products` method and stored on `Product` + a new `card_rating_history` table for trends. The Quality Score is a new pure, deterministic scorer (`services/card_quality_scorer.py`) reading a normalized card dict (works on published `Product`). A new seller page (`/card-quality`) shows a cockpit (KPIs, trend chart, worst-cards table) and a per-card detail slideover with a gauge, sub-bars, recommendations, and a button that enqueues `card-doctor`/`photo-optimizer` agents (async) polled via the existing task-status endpoint.

**Tech Stack:** Python 3.11, Flask + Flask-SQLAlchemy (SQLite), stdlib `unittest`, Jinja2 + Tailwind (CDN) + Alpine.js 3 + Chart.js 4.4 (custom `sh-*` design system), APScheduler.

## Global Constraints

- Tests use stdlib **unittest**, not pytest. Run one module from repo root `/home/athebyme/super-octo-broccoli`: `venv/bin/python -m unittest tests.<module> -v`. Test files are `tests/test_*.py` (`unittest.TestCase`, `self.assert*`), imports top-level absolute.
- Migrations: the in-app runner `_run_startup_migrations()` in `seller_platform.py` (~:6175) runs automatically on startup. Add columns by appending `(table, column, col_type)` tuples to its `migrations` list; add tables via an existence-check block AND as a SQLAlchemy model (so `db.create_all()` covers fresh DBs). Also provide a standalone `migrations/add_*.py` script mirroring `migrations/add_nm_rating_column.py` for manual/Docker runs. SQLite `ALTER TABLE ADD COLUMN` cannot add NOT NULL without DEFAULT.
- WB scales (empirically confirmed 2026-06-16): `productRating` is **0–10** (float), `feedbackRating` is **0–5** (float; `0` = no reviews). Response shape: `resp['data']['products'][i]['product']` with keys `nmId` (lowercase d), `productRating`, `feedbackRating`.
- WB sales-funnel rate limit: 3 req/min → space batches with `time.sleep(20)`; batch up to 1000 `nmIds` per request.
- Routes are `register_<name>_routes(app)` functions with `@app.route` + `@login_required` (NOT blueprints), registered at the bottom of `seller_platform.py`. Seller guard: `if not current_user.seller or not current_user.seller.has_valid_api_key():` → page: flash+redirect to `api_settings`; JSON: `jsonify({'error': ...}), 403`.
- New pages use the modern scaffold: `{% extends "base.html" %}` + `{% from "macros/components.html" import ... %}` + `{% block title %}` + `{% block content %}`; design-system classes `sh-*`; page `<script>` (incl. Chart.js CDN) goes INSIDE `{% block content %}` (no `scripts` block exists).
- AI execution is fully async: routes only ENQUEUE via `agent_service.create_task(...)`; a separate runner process executes; UI polls `GET /agents/api/tasks/<task_id>/status`.
- Source comments/docstrings/UI strings in Russian; files start with `# -*- coding: utf-8 -*-`.

---

### Task 1: Data model + migrations

**Files:**
- Modify: `models.py` — add columns to `Product` (after `nm_rating` at :216), add a `get_quality_breakdown()` helper, add a new `CardRatingHistory` model.
- Modify: `seller_platform.py` `_run_startup_migrations()` (~:6175) — append column tuples + a `card_rating_history` table block.
- Create: `migrations/add_card_quality_columns.py` (standalone, mirrors `add_nm_rating_column.py`).
- Test: `tests/test_card_quality_migration.py` (create).

**Interfaces:**
- Produces: `Product.wb_feedback_rating` (Float), `Product.nm_rating_checked_at` (DateTime), `Product.quality_score` (Float), `Product.quality_breakdown_json` (Text), `Product.quality_checked_at` (DateTime), `Product.get_quality_breakdown() -> dict`; model `CardRatingHistory(id, seller_id, product_id, nm_id, wb_product_rating, wb_feedback_rating, quality_score, captured_at)`; standalone `migrations.add_card_quality_columns.migrate(db_path=None) -> bool`.

- [ ] **Step 1: Write the failing migration test**

Create `tests/test_card_quality_migration.py`:
```python
# -*- coding: utf-8 -*-
"""Тест идемпотентной миграции колонок качества карточки."""

import os
import sqlite3
import tempfile
import unittest

from migrations.add_card_quality_columns import migrate

NEW_COLS = ['wb_feedback_rating', 'nm_rating_checked_at',
            'quality_score', 'quality_breakdown_json', 'quality_checked_at']


class TestCardQualityMigration(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = sqlite3.connect(self.path)
        conn.execute('CREATE TABLE products (id INTEGER PRIMARY KEY, nm_rating REAL)')
        conn.commit()
        conn.close()

    def tearDown(self):
        os.remove(self.path)

    def _cols(self):
        conn = sqlite3.connect(self.path)
        cols = [r[1] for r in conn.execute('PRAGMA table_info(products)').fetchall()]
        conn.close()
        return cols

    def test_adds_all_columns(self):
        migrate(self.path)
        cols = self._cols()
        for c in NEW_COLS:
            self.assertIn(c, cols)

    def test_idempotent_second_run_does_not_raise(self):
        migrate(self.path)
        migrate(self.path)  # must not raise
        self.assertIn('quality_score', self._cols())
```

- [ ] **Step 2: Run to verify it fails**

Run: `venv/bin/python -m unittest tests.test_card_quality_migration -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'migrations.add_card_quality_columns'`.

- [ ] **Step 3: Create the standalone migration script**

Create `migrations/add_card_quality_columns.py`:
```python
# -*- coding: utf-8 -*-
"""
Миграция: колонки качества карточки в таблицу products.

Добавляет: wb_feedback_rating (оценка по отзывам WB, 0-5),
nm_rating_checked_at (когда обновлён WB-рейтинг),
quality_score (наш Quality Score 0-100),
quality_breakdown_json (разбивка по измерениям),
quality_checked_at (когда пересчитан Quality Score).

Запуск:
    python migrations/add_card_quality_columns.py
    docker exec seller-platform python /app/migrations/add_card_quality_columns.py
"""

import sqlite3
import os


COLUMNS = [
    ('wb_feedback_rating', 'REAL'),
    ('nm_rating_checked_at', 'DATETIME'),
    ('quality_score', 'REAL'),
    ('quality_breakdown_json', 'TEXT'),
    ('quality_checked_at', 'DATETIME'),
]


def get_db_path():
    paths = [
        'data/seller_platform.db',
        '/app/data/seller_platform.db',
        os.path.join(os.path.dirname(__file__), '..', 'data', 'seller_platform.db'),
    ]
    for path in paths:
        if os.path.exists(path):
            print(f"Found database at: {path}")
            return path
    db_url = os.environ.get('DATABASE_URL', '')
    if db_url.startswith('sqlite:///'):
        db_path = db_url.replace('sqlite:///', '')
        if os.path.exists(db_path):
            return db_path
    return 'data/seller_platform.db'


def migrate(db_path=None):
    if db_path is None:
        db_path = get_db_path()
    print(f"Using database: {db_path}")
    if not os.path.exists(db_path):
        print(f"Database file not found: {db_path}")
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute('PRAGMA table_info(products)')
    existing = [row[1] for row in cursor.fetchall()]

    added = 0
    for col_name, col_type in COLUMNS:
        if col_name not in existing:
            try:
                cursor.execute(f'ALTER TABLE products ADD COLUMN {col_name} {col_type}')
                print(f'  + Added column: {col_name}')
                added += 1
            except sqlite3.OperationalError as e:
                print(f'  ! Error adding {col_name}: {e}')
        else:
            print(f'  = Column {col_name} already exists')

    conn.commit()
    conn.close()
    print(f'\nMigration completed! Added {added} new columns.')
    return True


if __name__ == '__main__':
    import sys
    migrate(sys.argv[1] if len(sys.argv) > 1 else None)
```

- [ ] **Step 4: Run to verify it passes**

Run: `venv/bin/python -m unittest tests.test_card_quality_migration -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Add the model columns + helper to `Product`**

In `models.py`, immediately AFTER `nm_rating = db.Column(db.Float, nullable=True)  # Рейтинг карточки (0-10)` (:216), add:
```python
    wb_feedback_rating = db.Column(db.Float, nullable=True)  # Оценка по отзывам WB (0-5)
    nm_rating_checked_at = db.Column(db.DateTime, nullable=True)  # Когда обновлён WB-рейтинг

    # Наш Quality Score (детерминированный, 0-100)
    quality_score = db.Column(db.Float, nullable=True)  # Quality Score (0-100)
    quality_breakdown_json = db.Column(db.Text)  # JSON разбивки по измерениям
    quality_checked_at = db.Column(db.DateTime, nullable=True)  # Когда пересчитан Quality Score
```
Then add this helper method to `Product` (next to `get_characteristics` at :252):
```python
    def get_quality_breakdown(self):
        """Разбивка Quality Score по измерениям."""
        if not self.quality_breakdown_json:
            return {}
        try:
            import json
            return json.loads(self.quality_breakdown_json)
        except Exception:
            return {}
```

- [ ] **Step 6: Add the `CardRatingHistory` model**

In `models.py`, after the `ShadowedCard` class (~:2041), add:
```python
class CardRatingHistory(db.Model):
    """Снимок рейтингов и Quality Score карточки для трендов."""
    __tablename__ = 'card_rating_history'

    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey('sellers.id'), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True, index=True)
    nm_id = db.Column(db.BigInteger, nullable=False)

    wb_product_rating = db.Column(db.Float)   # Оценка карточки WB (0-10)
    wb_feedback_rating = db.Column(db.Float)  # Оценка по отзывам WB (0-5)
    quality_score = db.Column(db.Float)       # Наш Quality Score (0-100)

    captured_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.Index('idx_crh_product_captured', 'product_id', 'captured_at'),
        db.Index('idx_crh_seller_captured', 'seller_id', 'captured_at'),
    )

    def __repr__(self):
        return f'<CardRatingHistory nm_id={self.nm_id} q={self.quality_score} at={self.captured_at}>'
```

- [ ] **Step 7: Register columns + table in the startup migrator**

In `seller_platform.py` `_run_startup_migrations()`, append to the `migrations` list (after the existing `('products', 'nm_rating', 'REAL'),`):
```python
        ('products', 'wb_feedback_rating', 'REAL'),
        ('products', 'nm_rating_checked_at', 'DATETIME'),
        ('products', 'quality_score', 'REAL'),
        ('products', 'quality_breakdown_json', 'TEXT'),
        ('products', 'quality_checked_at', 'DATETIME'),
```
Then, after the `prohibited_words` table block (~:6279), add a `card_rating_history` existence-check block:
```python
    # Таблица истории рейтингов карточек (для трендов)
    if 'card_rating_history' not in insp.get_table_names():
        try:
            db.session.execute(db.text('''
                CREATE TABLE card_rating_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER NOT NULL REFERENCES sellers(id),
                    product_id INTEGER REFERENCES products(id),
                    nm_id BIGINT NOT NULL,
                    wb_product_rating REAL,
                    wb_feedback_rating REAL,
                    quality_score REAL,
                    captured_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
            '''))
            db.session.execute(db.text('CREATE INDEX idx_crh_product_captured ON card_rating_history(product_id, captured_at)'))
            db.session.execute(db.text('CREATE INDEX idx_crh_seller_captured ON card_rating_history(seller_id, captured_at)'))
            db.session.commit()
            logger.info("Created table 'card_rating_history'")
        except Exception as e:
            db.session.rollback()
            logger.warning(f"Could not create card_rating_history table: {e}")
```

- [ ] **Step 8: Verify models import cleanly**

Run: `venv/bin/python -c "from models import Product, CardRatingHistory; assert hasattr(Product, 'quality_score'); assert hasattr(Product, 'get_quality_breakdown'); print('ok')"`
Expected: prints `ok`.

- [ ] **Step 9: Commit**

```bash
git add models.py seller_platform.py migrations/add_card_quality_columns.py tests/test_card_quality_migration.py
git commit -m "feat(card-quality): data model + migrations for WB rating + quality score"
```

---

### Task 2: Quality Score scorer

**Files:**
- Create: `services/card_quality_scorer.py`
- Test: `tests/test_card_quality_scorer.py` (create)

**Interfaces:**
- Produces:
  - `WEIGHTS: dict` — dimension → int weight, summing to 100.
  - `score_status(score: float) -> str` — `'excellent'` (≥85) / `'good'` (≥70) / `'average'` (≥50) / `'poor'` (else).
  - `compute_card_quality(card: dict) -> dict` — input keys `photos` (list), `characteristics` (dict|list), `title` (str), `description` (str), `brand` (str), `barcodes` (list), `price` (number), `subject_id` (int|None). Returns `{'score': float, 'status': str, 'dimensions': {name: {'score': int, 'status': str, 'weight': int, 'hint': str}}, 'recommendations': [str]}`.
  - `product_to_card_input(product) -> dict` — builds the input dict from a `Product`.
  - `card_quality_detail(product) -> dict` — full per-card payload (used by Task 4 route). Consumed by Task 3 (sync) and Task 4 (routes).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_card_quality_scorer.py`:
```python
# -*- coding: utf-8 -*-
"""Тесты детерминированного Quality Score."""

import json
import types
import unittest

from services.card_quality_scorer import (
    WEIGHTS, score_status, compute_card_quality, product_to_card_input,
)


def _perfect_card():
    return {
        'photos': ['u'] * 8,
        'characteristics': {f'k{i}': 'v' for i in range(10)},
        'title': 'x' * 40,
        'description': 'y' * 400,
        'brand': 'BrandX',
        'barcodes': ['1234567890123'],
        'price': 999,
        'subject_id': 5,
    }


def _empty_card():
    return {
        'photos': [], 'characteristics': {}, 'title': '', 'description': '',
        'brand': '', 'barcodes': [], 'price': 0, 'subject_id': None,
    }


class TestScoreStatus(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(score_status(90), 'excellent')
        self.assertEqual(score_status(85), 'excellent')
        self.assertEqual(score_status(70), 'good')
        self.assertEqual(score_status(69.9), 'average')
        self.assertEqual(score_status(50), 'average')
        self.assertEqual(score_status(30), 'poor')


class TestWeights(unittest.TestCase):
    def test_weights_sum_to_100(self):
        self.assertEqual(sum(WEIGHTS.values()), 100)


class TestComputeCardQuality(unittest.TestCase):
    def test_perfect_card_scores_100(self):
        result = compute_card_quality(_perfect_card())
        self.assertEqual(result['score'], 100.0)
        self.assertEqual(result['status'], 'excellent')
        self.assertEqual(result['recommendations'], [])

    def test_empty_card_scores_0_and_has_recommendations(self):
        result = compute_card_quality(_empty_card())
        self.assertEqual(result['score'], 0.0)
        self.assertEqual(result['status'], 'poor')
        self.assertTrue(len(result['recommendations']) >= 5)

    def test_photos_subscore_is_proportional(self):
        card = _perfect_card()
        card['photos'] = ['u', 'u', 'u']  # 3 photos
        dim = compute_card_quality(card)['dimensions']['photos']
        self.assertEqual(dim['score'], 37)        # 3 * 100 // 8
        self.assertEqual(dim['status'], 'warning')
        self.assertTrue(dim['hint'])

    def test_all_dimensions_present(self):
        dims = compute_card_quality(_perfect_card())['dimensions']
        self.assertEqual(set(dims.keys()), set(WEIGHTS.keys()))

    def test_recommendations_sorted_by_impact(self):
        # missing photos (weight 20) must rank above missing brand (weight 10)
        card = _perfect_card()
        card['photos'] = []
        card['brand'] = ''
        recs = compute_card_quality(card)['recommendations']
        joined = ' || '.join(recs)
        self.assertIn('фото', joined.lower())
        self.assertLess(joined.lower().index('фото'), joined.lower().index('бренд'))


class TestProductToCardInput(unittest.TestCase):
    def test_reads_product_attributes(self):
        product = types.SimpleNamespace(
            photos_json=json.dumps(['a', 'b']),
            characteristics_json=json.dumps({'Цвет': 'красный'}),
            sizes_json=json.dumps([{'skus': ['111', '222']}]),
            title='Товар', description='Описание', brand='Бренд',
            price=500, subject_id=64,
        )
        card = product_to_card_input(product)
        self.assertEqual(card['photos'], ['a', 'b'])
        self.assertEqual(card['characteristics'], {'Цвет': 'красный'})
        self.assertEqual(card['barcodes'], ['111', '222'])
        self.assertEqual(card['title'], 'Товар')
        self.assertEqual(card['subject_id'], 64)
```

- [ ] **Step 2: Run to verify it fails**

Run: `venv/bin/python -m unittest tests.test_card_quality_scorer -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.card_quality_scorer'`.

- [ ] **Step 3: Implement the scorer**

Create `services/card_quality_scorer.py`:
```python
# -*- coding: utf-8 -*-
"""
Детерминированный Quality Score карточки (0-100).

Считает взвешенную оценку по измерениям (фото, характеристики, описание,
заголовок, бренд, штрихкоды, цена, категория) и формирует рекомендации
«как поднять». Чистые функции без БД — пригодны для unit-тестов.
"""
import json
from typing import Dict, Any

WEIGHTS = {
    'characteristics': 25,
    'photos': 20,
    'description': 15,
    'title': 10,
    'brand': 10,
    'barcodes': 10,
    'price': 5,
    'category': 5,
}


def score_status(score: float) -> str:
    if score >= 85:
        return 'excellent'
    if score >= 70:
        return 'good'
    if score >= 50:
        return 'average'
    return 'poor'


def _dim_photos(card) -> tuple:
    count = len(card.get('photos') or [])
    sub = min(100, count * 100 // 8)
    if count == 0:
        return 0, 'error', 'Нет фото — добавьте минимум 5 (до 30 на WB)'
    if count < 5:
        return sub, 'warning', f'Мало фото ({count}) — рекомендуем 8+ (до 30)'
    if count < 8:
        return sub, 'ok', f'Можно добавить фото ({count}/8+)'
    return sub, 'ok', ''


def _count_characteristics(chars) -> int:
    if isinstance(chars, dict):
        return len([k for k, v in chars.items() if not str(k).startswith('_') and v])
    if isinstance(chars, list):
        return len(chars)
    return 0


def _dim_characteristics(card) -> tuple:
    count = _count_characteristics(card.get('characteristics'))
    sub = min(100, count * 10)
    if count == 0:
        return 0, 'error', 'Заполните характеристики товара'
    if count < 3:
        return sub, 'warning', f'Мало характеристик ({count}) — WB может отклонить'
    if count < 10:
        return sub, 'ok', f'Добавьте характеристики ({count}/10)'
    return sub, 'ok', ''


def _dim_description(card) -> tuple:
    length = len(card.get('description') or '')
    sub = min(100, length * 100 // 400)
    if length == 0:
        return 0, 'error', 'Добавьте описание товара'
    if length < 200:
        return sub, 'warning', 'Короткое описание — расширьте до 400+ символов'
    return sub, 'ok', ''


def _dim_title(card) -> tuple:
    length = len(card.get('title') or '')
    if length == 0:
        return 0, 'error', 'Нет заголовка'
    if length > 60:
        return 50, 'warning', 'Заголовок длиннее 60 символов — WB обрежет'
    if length < 25:
        return min(100, length * 100 // 25), 'warning', 'Короткий заголовок — добавьте деталей'
    return 100, 'ok', ''


def _dim_brand(card) -> tuple:
    return (100, 'ok', '') if card.get('brand') else (0, 'warning', 'Не указан бренд')


def _dim_barcodes(card) -> tuple:
    return (100, 'ok', '') if (card.get('barcodes') or []) else (0, 'warning', 'Нет штрихкодов')


def _dim_price(card) -> tuple:
    price = card.get('price') or 0
    return (100, 'ok', '') if price and price > 0 else (0, 'error', 'Нет цены')


def _dim_category(card) -> tuple:
    return (100, 'ok', '') if card.get('subject_id') else (0, 'error', 'Не задана категория WB')


_DIMENSIONS = {
    'characteristics': _dim_characteristics,
    'photos': _dim_photos,
    'description': _dim_description,
    'title': _dim_title,
    'brand': _dim_brand,
    'barcodes': _dim_barcodes,
    'price': _dim_price,
    'category': _dim_category,
}


def compute_card_quality(card: Dict[str, Any]) -> Dict[str, Any]:
    dimensions = {}
    weighted_sum = 0
    rec_candidates = []  # (impact, name, hint)

    for name, fn in _DIMENSIONS.items():
        weight = WEIGHTS[name]
        sub, status, hint = fn(card)
        dimensions[name] = {'score': sub, 'status': status, 'weight': weight, 'hint': hint}
        weighted_sum += sub * weight
        if hint:
            rec_candidates.append((weight * (100 - sub), name, hint))

    score = round(weighted_sum / 100.0, 1)
    rec_candidates.sort(key=lambda t: (-t[0], t[1]))
    recommendations = [hint for _, _, hint in rec_candidates]

    return {
        'score': score,
        'status': score_status(score),
        'dimensions': dimensions,
        'recommendations': recommendations,
    }


def _loads(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def product_to_card_input(product) -> Dict[str, Any]:
    """Построить нормализованный dict из опубликованной карточки Product."""
    photos = _loads(getattr(product, 'photos_json', None), [])
    chars = _loads(getattr(product, 'characteristics_json', None), {})
    sizes = _loads(getattr(product, 'sizes_json', None), [])

    barcodes = []
    if isinstance(sizes, list):
        for sz in sizes:
            if isinstance(sz, dict):
                bc = sz.get('skus') or sz.get('barcodes') or sz.get('barcode')
                if isinstance(bc, list):
                    barcodes.extend(bc)
                elif bc:
                    barcodes.append(bc)

    price = 0
    if getattr(product, 'price', None):
        try:
            price = float(product.price)
        except (TypeError, ValueError):
            price = 0

    return {
        'photos': photos if isinstance(photos, list) else [],
        'characteristics': chars,
        'title': getattr(product, 'title', '') or '',
        'description': getattr(product, 'description', '') or '',
        'brand': getattr(product, 'brand', '') or '',
        'barcodes': barcodes,
        'price': price,
        'subject_id': getattr(product, 'subject_id', None),
    }


def card_quality_detail(product) -> Dict[str, Any]:
    """Полный payload карточки для UI: WB-рейтинг + Quality Score + рекомендации."""
    cq = compute_card_quality(product_to_card_input(product))
    checked = getattr(product, 'nm_rating_checked_at', None)
    return {
        'product_id': getattr(product, 'id', None),
        'nm_id': getattr(product, 'nm_id', None),
        'vendor_code': getattr(product, 'vendor_code', None),
        'title': getattr(product, 'title', None),
        'wb_product_rating': getattr(product, 'nm_rating', None),       # 0-10
        'wb_feedback_rating': getattr(product, 'wb_feedback_rating', None),  # 0-5
        'nm_rating_checked_at': checked.isoformat() if checked else None,
        'quality_score': cq['score'],
        'quality_status': cq['status'],
        'dimensions': cq['dimensions'],
        'recommendations': cq['recommendations'],
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: `venv/bin/python -m unittest tests.test_card_quality_scorer -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add services/card_quality_scorer.py tests/test_card_quality_scorer.py
git commit -m "feat(card-quality): deterministic quality scorer with per-dimension breakdown"
```

---

### Task 3: WB rating ingestion + sync job

**Files:**
- Modify: `services/product_sync_scheduler.py` — add `parse_sales_funnel_ratings(...)`, `sync_card_ratings_all_sellers(flask_app)`, and register the APScheduler job in the scheduler setup function (same place `sync_blocked_cards_all_sellers` is registered).
- Test: `tests/test_card_rating_sync.py` (create).

**Interfaces:**
- Consumes: existing `WildberriesAPIClient.get_sales_funnel_products(period_start, period_end, nm_ids=None, limit=50, offset=0, log_to_db=True, seller_id=None)` (returns parsed JSON); `services.card_quality_scorer.compute_card_quality/product_to_card_input`.
- Produces: `parse_sales_funnel_ratings(api_response: dict) -> dict[int, dict]` mapping `nm_id -> {'product_rating': float|None, 'feedback_rating': float|None}`; `sync_card_ratings_all_sellers(flask_app)`.

- [ ] **Step 1: Write the failing parse test**

Create `tests/test_card_rating_sync.py`:
```python
# -*- coding: utf-8 -*-
"""Тест парсинга ответа sales-funnel в карту рейтингов."""

import unittest

from services.product_sync_scheduler import parse_sales_funnel_ratings


class TestParseSalesFunnelRatings(unittest.TestCase):
    def test_extracts_ratings_keyed_by_nm_id(self):
        resp = {'data': {'products': [
            {'product': {'nmId': 105146863, 'productRating': 8, 'feedbackRating': 4.8}},
            {'product': {'nmId': 100142591, 'productRating': 6, 'feedbackRating': 0}},
        ]}}
        result = parse_sales_funnel_ratings(resp)
        self.assertEqual(result[105146863], {'product_rating': 8, 'feedback_rating': 4.8})
        self.assertEqual(result[100142591], {'product_rating': 6, 'feedback_rating': 0})

    def test_handles_missing_and_malformed(self):
        self.assertEqual(parse_sales_funnel_ratings({}), {})
        self.assertEqual(parse_sales_funnel_ratings({'data': {}}), {})
        self.assertEqual(parse_sales_funnel_ratings({'data': {'products': [{}]}}), {})

    def test_supports_nmID_alias(self):
        resp = {'data': {'products': [{'product': {'nmID': 42, 'productRating': 9.3, 'feedbackRating': 5}}]}}
        self.assertEqual(parse_sales_funnel_ratings(resp), {42: {'product_rating': 9.3, 'feedback_rating': 5}})
```

- [ ] **Step 2: Run to verify it fails**

Run: `venv/bin/python -m unittest tests.test_card_rating_sync -v`
Expected: FAIL — `ImportError: cannot import name 'parse_sales_funnel_ratings'`.

- [ ] **Step 3: Implement `parse_sales_funnel_ratings`**

In `services/product_sync_scheduler.py` (module level, near the other helpers), add:
```python
def parse_sales_funnel_ratings(api_response: dict) -> dict:
    """Извлечь {nm_id: {product_rating, feedback_rating}} из ответа sales-funnel."""
    out = {}
    data = (api_response or {}).get('data') or {}
    for item in data.get('products', []) or []:
        prod = item.get('product') if isinstance(item, dict) else None
        if not isinstance(prod, dict):
            continue
        nm = prod.get('nmId', prod.get('nmID'))
        if nm is None:
            continue
        out[int(nm)] = {
            'product_rating': prod.get('productRating'),
            'feedback_rating': prod.get('feedbackRating'),
        }
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `venv/bin/python -m unittest tests.test_card_rating_sync -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Implement the sync job (no separate test — needs app+DB; verified manually in Task 6)**

In `services/product_sync_scheduler.py`, add (mirroring `sync_blocked_cards_all_sellers`):
```python
def sync_card_ratings_all_sellers(flask_app):
    """Тянет WB productRating/feedbackRating для активного каталога и
    пересчитывает Quality Score. Запускается планировщиком (раз в несколько часов).
    Лимит sales-funnel: 3 req/min → пауза 20с между батчами по 1000 nmId."""
    import time
    from datetime import timedelta
    from models import Seller, Product, CardRatingHistory, APILog, db
    from services.wb_api_client import WildberriesAPIClient
    from services.card_quality_scorer import compute_card_quality, product_to_card_input

    with flask_app.app_context():
        try:
            sellers = Seller.query.filter(
                Seller._wb_api_key_encrypted.isnot(None),
                Seller._wb_api_key_encrypted != ''
            ).all()
            period_end = datetime.utcnow().date()
            period_start = period_end - timedelta(days=30)
            ps, pe = period_start.isoformat(), period_end.isoformat()

            for seller in sellers:
                if not seller.has_valid_api_key():
                    continue
                products = Product.query.filter_by(seller_id=seller.id, is_active=True)\
                    .filter(Product.nm_id.isnot(None)).all()
                if not products:
                    continue
                by_nm = {p.nm_id: p for p in products}
                nm_ids = list(by_nm.keys())

                client = WildberriesAPIClient(
                    api_key=seller.wb_api_key,
                    db_logger_callback=lambda **kwargs: APILog.log_request(**kwargs)
                )
                now = datetime.utcnow()
                try:
                    for i in range(0, len(nm_ids), 1000):
                        batch = nm_ids[i:i + 1000]
                        resp = client.get_sales_funnel_products(
                            period_start=ps, period_end=pe,
                            nm_ids=batch, limit=1000,
                            log_to_db=True, seller_id=seller.id
                        )
                        ratings = parse_sales_funnel_ratings(resp)
                        for nm_id, r in ratings.items():
                            p = by_nm.get(nm_id)
                            if not p:
                                continue
                            if r['product_rating'] is not None:
                                p.nm_rating = r['product_rating']
                            p.wb_feedback_rating = r['feedback_rating']
                            p.nm_rating_checked_at = now
                        if i + 1000 < len(nm_ids):
                            time.sleep(20)  # лимит 3 req/min

                    # Пересчёт Quality Score для всех активных карточек (дёшево)
                    for p in products:
                        cq = compute_card_quality(product_to_card_input(p))
                        p.quality_score = cq['score']
                        p.quality_breakdown_json = json.dumps(cq['dimensions'], ensure_ascii=False)
                        p.quality_checked_at = now
                        db.session.add(CardRatingHistory(
                            seller_id=seller.id, product_id=p.id, nm_id=p.nm_id,
                            wb_product_rating=p.nm_rating, wb_feedback_rating=p.wb_feedback_rating,
                            quality_score=cq['score'], captured_at=now,
                        ))
                    db.session.commit()
                    logger.info(f"✅ Card ratings synced for seller {seller.id}: {len(products)} products")
                except Exception as e:
                    db.session.rollback()
                    logger.error(f"❌ Card rating sync failed for seller {seller.id}: {e}")
        except Exception as e:
            logger.exception(f"❌ Error in sync_card_ratings_all_sellers: {e}")
```
Confirm `import json` and `from datetime import datetime` are present at the top of the module (they are used by existing jobs); add `import json` at module top if missing.

- [ ] **Step 6: Register the APScheduler job**

Find where `sync_blocked_cards_all_sellers` is registered with the scheduler (search the module for `add_job` referencing it). Add an adjacent job registration:
```python
    scheduler.add_job(
        func=sync_card_ratings_all_sellers,
        trigger='interval',
        hours=6,
        args=[flask_app],
        id='sync_card_ratings',
        replace_existing=True,
    )
```
(Match the exact `add_job` keyword style used by the existing blocked-cards job in this file — copy its argument shape, only changing `func`, `id`, and the interval to `hours=6`.)

- [ ] **Step 7: Run the full backend test suite**

Run: `venv/bin/python -m unittest tests.test_card_rating_sync tests.test_card_quality_scorer tests.test_card_quality_migration -v`
Expected: PASS (all).

- [ ] **Step 8: Commit**

```bash
git add services/product_sync_scheduler.py tests/test_card_rating_sync.py
git commit -m "feat(card-quality): WB rating ingestion + quality recompute sync job"
```

---

### Task 4: Routes (cockpit page, detail JSON, AI analyze, refresh)

**Files:**
- Create: `routes/card_quality.py`
- Modify: `seller_platform.py` (~:6064-6078) — register the new routes.
- Test: `tests/test_card_quality_detail.py` (create) — covers the pure `card_quality_detail` payload via a fake product.

**Interfaces:**
- Consumes: `services.card_quality_scorer.card_quality_detail`; `agent_service.get_agent_by_name`, `agent_service.create_task`.
- Produces routes: `GET /card-quality` (page), `GET /api/card-quality/<int:product_id>` (JSON detail), `POST /api/card-quality/<int:product_id>/ai-analyze` (enqueue agents → `{task_ids}`), `POST /api/card-quality/refresh` (on-demand sync trigger).

- [ ] **Step 1: Write the failing detail test**

Create `tests/test_card_quality_detail.py`:
```python
# -*- coding: utf-8 -*-
"""Тест payload карточки для UI."""

import json
import types
import unittest
from datetime import datetime

from services.card_quality_scorer import card_quality_detail


class TestCardQualityDetail(unittest.TestCase):
    def test_combines_wb_rating_and_quality_score(self):
        product = types.SimpleNamespace(
            id=1, nm_id=105146863, vendor_code='SKU-1', title='Товар',
            photos_json=json.dumps(['a', 'b', 'c']),
            characteristics_json=json.dumps({'Цвет': 'к', 'Размер': 'M'}),
            sizes_json=json.dumps([{'skus': ['111']}]),
            description='d' * 300, brand='Бренд', price=999, subject_id=64,
            nm_rating=8.0, wb_feedback_rating=4.8,
            nm_rating_checked_at=datetime(2026, 6, 16, 12, 0, 0),
        )
        d = card_quality_detail(product)
        self.assertEqual(d['nm_id'], 105146863)
        self.assertEqual(d['wb_product_rating'], 8.0)
        self.assertEqual(d['wb_feedback_rating'], 4.8)
        self.assertEqual(d['nm_rating_checked_at'], '2026-06-16T12:00:00')
        self.assertIn('photos', d['dimensions'])
        self.assertIsInstance(d['quality_score'], float)
        self.assertIn(d['quality_status'], ('excellent', 'good', 'average', 'poor'))
```

- [ ] **Step 2: Run to verify it fails**

Run: `venv/bin/python -m unittest tests.test_card_quality_detail -v`
Expected: FAIL — `ImportError: cannot import name 'card_quality_detail'` UNLESS Task 2 is complete; if Task 2 done, this passes immediately (the function already exists). If it passes, proceed (this test documents/guards the route payload contract).

- [ ] **Step 3: Create the routes module**

Create `routes/card_quality.py`:
```python
# -*- coding: utf-8 -*-
"""Роуты фичи «Качество карточек»: кокпит, деталь карточки, AI-анализ, обновление."""
import json
import logging

from flask import render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user

from models import db, Product, CardRatingHistory
from services.card_quality_scorer import card_quality_detail
from services import agent_service

logger = logging.getLogger('card_quality')


def register_card_quality_routes(app):
    """Регистрация роутов качества карточек."""

    @app.route('/card-quality')
    @login_required
    def card_quality_page():
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для оценки качества карточек необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('api_settings'))
        return render_template('card_quality.html')

    @app.route('/api/card-quality/list')
    @login_required
    def api_card_quality_list():
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        sort = request.args.get('sort', 'quality_score')
        order = request.args.get('order', 'asc')
        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 50, type=int), 200)

        q = Product.query.filter_by(seller_id=current_user.seller.id, is_active=True)
        col = {'quality_score': Product.quality_score, 'nm_rating': Product.nm_rating,
               'wb_feedback_rating': Product.wb_feedback_rating}.get(sort, Product.quality_score)
        q = q.order_by(col.asc() if order == 'asc' else col.desc())
        pagination = q.paginate(page=page, per_page=per_page, error_out=False)
        items = [card_quality_detail(p) for p in pagination.items]

        scored = [p.quality_score for p in pagination.items if p.quality_score is not None]
        ratings = [p.nm_rating for p in pagination.items if p.nm_rating is not None]
        summary = {
            'avg_quality': round(sum(scored) / len(scored), 1) if scored else None,
            'avg_wb_rating': round(sum(ratings) / len(ratings), 1) if ratings else None,
            'total': pagination.total,
        }
        return jsonify({'success': True, 'items': items, 'summary': summary,
                        'page': page, 'pages': pagination.pages})

    @app.route('/api/card-quality/<int:product_id>')
    @login_required
    def api_card_quality_detail(product_id):
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        product = Product.query.filter_by(id=product_id, seller_id=current_user.seller.id).first()
        if not product:
            return jsonify({'error': 'Карточка не найдена'}), 404
        detail = card_quality_detail(product)
        trend = CardRatingHistory.query.filter_by(product_id=product_id)\
            .order_by(CardRatingHistory.captured_at.asc()).limit(90).all()
        detail['trend'] = [{
            'captured_at': h.captured_at.isoformat() if h.captured_at else None,
            'wb_product_rating': h.wb_product_rating,
            'quality_score': h.quality_score,
        } for h in trend]
        return jsonify({'success': True, 'data': detail})

    @app.route('/api/card-quality/<int:product_id>/ai-analyze', methods=['POST'])
    @login_required
    def api_card_quality_ai_analyze(product_id):
        if not current_user.seller:
            return jsonify({'error': 'Нет профиля продавца'}), 403
        product = Product.query.filter_by(id=product_id, seller_id=current_user.seller.id).first()
        if not product:
            return jsonify({'error': 'Карточка не найдена'}), 404

        task_ids = {}
        for agent_name, task_type in (('card-doctor', 'diagnose_single'),
                                      ('photo-optimizer', 'optimize_single')):
            agent = agent_service.get_agent_by_name(agent_name)
            if not agent or getattr(agent, 'status', None) != 'online':
                continue
            task = agent_service.create_task(
                agent_id=agent.id,
                seller_id=current_user.seller.id,
                task_type=task_type,
                title=f'AI-анализ карточки {product.nm_id}',
                input_data={'product_id': product.id},
            )
            task_ids[agent_name] = task.id

        if not task_ids:
            return jsonify({'error': 'AI-агенты сейчас офлайн'}), 409
        return jsonify({'success': True, 'task_ids': task_ids})

    @app.route('/api/card-quality/refresh', methods=['POST'])
    @login_required
    def api_card_quality_refresh():
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        from services.product_sync_scheduler import sync_card_ratings_all_sellers
        import threading
        from flask import current_app
        app_obj = current_app._get_current_object()
        threading.Thread(target=sync_card_ratings_all_sellers, args=(app_obj,), daemon=True).start()
        return jsonify({'success': True, 'message': 'Обновление рейтингов запущено'})
```

- [ ] **Step 4: Register the routes**

In `seller_platform.py`, after the blocked-cards registration block (~:6070), add:
```python
# ============= РОУТЫ КАЧЕСТВА КАРТОЧЕК =============
from routes.card_quality import register_card_quality_routes
register_card_quality_routes(app)
```

- [ ] **Step 5: Run the detail test + smoke-import the routes module**

Run: `venv/bin/python -m unittest tests.test_card_quality_detail -v`
Expected: PASS.
Run: `venv/bin/python -c "import routes.card_quality; print('import ok')"`
Expected: prints `import ok` (no syntax/import errors).

- [ ] **Step 6: Commit**

```bash
git add routes/card_quality.py seller_platform.py tests/test_card_quality_detail.py
git commit -m "feat(card-quality): routes for cockpit, detail, ai-analyze, refresh"
```

---

### Task 5: UI — macros, cockpit page, slideover, nav, calibration fix

**Files:**
- Modify: `templates/macros/components.html` — add `score_gauge` and `wb_rating_badge` macros.
- Create: `templates/card_quality.html`
- Modify: `templates/base.html` — add nav entry (Товары group) + active state.
- Modify: `templates/blocked_cards.html:396-405` (both tabs) — replace inline rating cell with `wb_rating_badge` (0–10 calibration fix).

**Interfaces:**
- Consumes: routes from Task 4 (`/api/card-quality/list`, `/api/card-quality/<id>`, `.../ai-analyze`); the agent status poll `GET /agents/api/tasks/<task_id>/status` (existing).

- [ ] **Step 1: Add the gauge + badge macros**

Append to `templates/macros/components.html`:
```jinja
{% macro wb_rating_badge(value, scale=10) %}
{# WB productRating 0-10 (scale=10) или feedbackRating 0-5 (scale=5). #}
{% if value is not none %}
  {% if scale == 10 %}
    {% set color = 'text-green-700' if value >= 8 else 'text-orange-700' if value >= 6 else 'text-red-700' %}
  {% else %}
    {% set color = 'text-green-700' if value >= 4.5 else 'text-orange-700' if value >= 3.5 else 'text-red-700' %}
  {% endif %}
  <span class="inline-flex items-center gap-1 font-medium {{ color }}">
    <svg class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20"><path d="M9.049 2.927c.3-.921 1.603-.921 1.902 0l1.07 3.292a1 1 0 00.95.69h3.462c.969 0 1.371 1.24.588 1.81l-2.8 2.034a1 1 0 00-.364 1.118l1.07 3.292c.3.921-.755 1.688-1.54 1.118l-2.8-2.034a1 1 0 00-1.175 0l-2.8 2.034c-.784.57-1.838-.197-1.539-1.118l1.07-3.292a1 1 0 00-.364-1.118L2.98 8.72c-.783-.57-.38-1.81.588-1.81h3.461a1 1 0 00.951-.69l1.07-3.292z"></path></svg>
    {{ '%.1f'|format(value) }}<span class="text-gray-400 text-xs">/{{ scale }}</span>
  </span>
{% else %}<span class="text-gray-400">—</span>{% endif %}
{% endmacro %}

{% macro score_gauge(value, size=72) %}
{# Quality Score 0-100 как кольцо conic-gradient с числом по центру. #}
{% set v = value if value is not none else 0 %}
{% set col = '#16a34a' if v >= 70 else '#d97706' if v >= 50 else '#dc2626' %}
<div style="width:{{ size }}px;height:{{ size }}px;border-radius:50%;
     background:conic-gradient({{ col }} {{ v }}%, #ece9e4 0);
     display:flex;align-items:center;justify-content:center">
  <div style="width:{{ size - 14 }}px;height:{{ size - 14 }}px;border-radius:50%;background:#fff;
       display:flex;align-items:center;justify-content:center;
       font-family:var(--font-display);font-style:italic;font-size:{{ (size/3.2)|int }}px;color:{{ col }}">
    {{ value|round|int if value is not none else '—' }}
  </div>
</div>
{% endmacro %}
```

- [ ] **Step 2: Create the cockpit page**

Create `templates/card_quality.html` extending base.html, importing macros, with KPI stat cards, a Chart.js trend chart, a worst-cards table (each row: `score_gauge` mini + `wb_rating_badge`), and a detail slideover bound to `cardQualityPage()` Alpine state. The page fetches `/api/card-quality/list`, opens detail via `/api/card-quality/<id>`, triggers AI via POST `/api/card-quality/<id>/ai-analyze` then polls `/agents/api/tasks/<task_id>/status` every 3000ms until status leaves `queued`/`running`, and refresh via POST `/api/card-quality/refresh` with a `$store.toasts` confirmation. Use the analytics.html scaffold (`sh-page-header`, `sh-stat-grid sh-stat-grid--4`, `sh-card`), the Chart.js CDN `<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js">` inside `{% block content %}`, canvas via `x-ref`, and CSRF header `X-CSRFToken: '{{ csrf_token() }}'` on POSTs. Page skeleton:
```jinja
{% extends "base.html" %}
{% from "macros/components.html" import page_header, stat_card, stat_grid, badge, score_gauge, wb_rating_badge %}
{% block title %}Качество карточек - Seller Hub{% endblock %}
{% block content %}
<div x-data="cardQualityPage()" x-init="init()">
  <div class="sh-page-header"><div class="sh-page-header-content">
    <div><h1 class="sh-page-title">Качество карточек</h1>
      <p class="sh-page-subtitle">WB-рейтинг и наш Quality Score</p></div>
    <div class="sh-page-actions">
      <button @click="refresh()" :disabled="refreshing" class="sh-btn sh-btn--white-outline sh-btn--sm">Обновить рейтинги</button>
    </div>
  </div></div>

  <div class="sh-stat-grid sh-stat-grid--4" style="margin-bottom:32px">
    <div class="sh-stat-card"><div class="sh-stat-header"><span class="sh-stat-label">Средний Quality Score</span></div>
      <div class="sh-stat-value" x-text="summary.avg_quality ?? '—'"></div></div>
    <div class="sh-stat-card"><div class="sh-stat-header"><span class="sh-stat-label">Средний WB-рейтинг (0–10)</span></div>
      <div class="sh-stat-value" x-text="summary.avg_wb_rating ?? '—'"></div></div>
    <div class="sh-stat-card"><div class="sh-stat-header"><span class="sh-stat-label">Карточек</span></div>
      <div class="sh-stat-value" x-text="summary.total ?? 0"></div></div>
    <div class="sh-stat-card"><div class="sh-stat-header"><span class="sh-stat-label">Требуют внимания</span></div>
      <div class="sh-stat-value" x-text="needAttention"></div></div>
  </div>

  <div class="sh-card" style="padding:0;overflow:hidden">
    <table class="sh-table" style="width:100%">
      <thead><tr>
        <th>Артикул</th><th>Название</th><th>WB карточка</th><th>WB отзывы</th><th>Quality</th><th></th>
      </tr></thead>
      <tbody>
        <template x-for="it in items" :key="it.product_id">
          <tr>
            <td x-text="it.nm_id"></td>
            <td x-text="it.title"></td>
            <td x-html="wbBadge(it.wb_product_rating, 10)"></td>
            <td x-html="wbBadge(it.wb_feedback_rating, 5)"></td>
            <td><span :style="gaugeStyle(it.quality_score)" x-text="it.quality_score ?? '—'"></span></td>
            <td><button class="sh-btn sh-btn--ghost sh-btn--sm" @click="openDetail(it.product_id)">Детали</button></td>
          </tr>
        </template>
      </tbody>
    </table>
  </div>

  {# ── Slideover деталь ── #}
  <div x-show="drawer" x-cloak class="sh-slideover-backdrop" @click="drawer=false"></div>
  <div x-show="drawer" x-cloak class="sh-slideover">
    <template x-if="detail">
      <div style="padding:24px">
        <h2 class="sh-page-title" style="font-size:1.25rem" x-text="'Артикул ' + (detail.nm_id||'')"></h2>
        <div style="display:flex;gap:24px;align-items:center;margin:16px 0">
          <div :style="gaugeStyle(detail.quality_score, 72)" x-text="Math.round(detail.quality_score)"></div>
          <div>
            <div>WB: оценка карточки <span x-html="wbBadge(detail.wb_product_rating,10)"></span></div>
            <div>WB: по отзывам <span x-html="wbBadge(detail.wb_feedback_rating,5)"></span></div>
            <div class="text-gray-400 text-xs" x-text="detail.nm_rating_checked_at ? ('обновлено '+detail.nm_rating_checked_at) : ''"></div>
          </div>
        </div>
        <template x-for="(d,name) in detail.dimensions" :key="name">
          <div style="margin:8px 0">
            <div style="display:flex;justify-content:space-between;font-size:13px">
              <span x-text="name"></span><span x-text="d.score + '%'"></span></div>
            <div style="height:8px;background:#ece9e4;border-radius:4px">
              <div :style="'height:8px;border-radius:4px;width:'+d.score+'%;background:'+barColor(d.status)"></div></div>
            <div class="text-gray-500 text-xs" x-show="d.hint" x-text="d.hint"></div>
          </div>
        </template>
        <div style="margin-top:16px;display:flex;gap:8px">
          <button class="sh-btn sh-btn--primary sh-btn--sm" @click="aiAnalyze(detail.product_id)" :disabled="aiRunning">🤖 Глубокий AI-анализ</button>
        </div>
        <div x-show="aiRunning" class="text-gray-500 text-xs" style="margin-top:8px" x-text="aiStatus"></div>
      </div>
    </template>
  </div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
function cardQualityPage() {
  return {
    items: [], summary: {}, detail: null, drawer: false,
    refreshing: false, aiRunning: false, aiStatus: '', _poll: null,
    get needAttention() { return this.items.filter(i => (i.quality_score ?? 0) < 50 || (i.wb_product_rating ?? 10) < 6).length; },
    async init() { await this.load(); },
    async load() {
      const r = await fetch('/api/card-quality/list?sort=quality_score&order=asc&per_page=100');
      const d = await r.json();
      this.items = d.items || []; this.summary = d.summary || {};
    },
    async openDetail(id) {
      this.drawer = true; this.detail = null;
      const r = await fetch('/api/card-quality/' + id);
      const d = await r.json(); this.detail = d.data;
    },
    async refresh() {
      this.refreshing = true;
      await fetch('/api/card-quality/refresh', {method:'POST', headers:{'X-CSRFToken':'{{ csrf_token() }}'}});
      this.$store.toasts.info('Обновление рейтингов запущено — данные появятся через несколько минут');
      this.refreshing = false;
    },
    async aiAnalyze(id) {
      this.aiRunning = true; this.aiStatus = 'Запуск агентов…';
      const r = await fetch('/api/card-quality/' + id + '/ai-analyze', {method:'POST', headers:{'X-CSRFToken':'{{ csrf_token() }}'}});
      const d = await r.json();
      if (!d.success) { this.aiStatus = d.error || 'Ошибка'; this.aiRunning = false; return; }
      const taskId = Object.values(d.task_ids)[0];
      this._poll = setInterval(async () => {
        const s = await (await fetch('/agents/api/tasks/' + taskId + '/status')).json();
        const t = s.task; this.aiStatus = 'Статус: ' + t.status;
        if (t.status !== 'queued' && t.status !== 'running') {
          clearInterval(this._poll); this.aiRunning = false;
          this.aiStatus = (t.result && t.result.recommendations) ? t.result.recommendations.join(' · ') : 'Готово';
        }
      }, 3000);
    },
    wbBadge(v, scale) {
      if (v === null || v === undefined) return '<span style="color:#9a9a9a">—</span>';
      const col = scale === 10 ? (v>=8?'#15803d':v>=6?'#b45309':'#b91c1c') : (v>=4.5?'#15803d':v>=3.5?'#b45309':'#b91c1c');
      return '<span style="color:'+col+';font-weight:500">★ '+v.toFixed(1)+'<span style="color:#9a9a9a;font-size:11px">/'+scale+'</span></span>';
    },
    barColor(status) { return status==='ok'?'#16a34a':status==='warning'?'#d97706':'#dc2626'; },
    gaugeStyle(v, size) {
      const col = (v??0)>=70?'#16a34a':(v??0)>=50?'#d97706':'#dc2626';
      const s = size||40;
      return `display:inline-flex;align-items:center;justify-content:center;width:${s}px;height:${s}px;border-radius:50%;color:#fff;font-weight:600;font-size:${Math.round(s/3)}px;background:${col}`;
    },
  };
}
</script>
</div>
{% endblock %}
```

- [ ] **Step 3: Add the sidebar nav entry**

In `templates/base.html` Товары group (~:1834), add `'card_quality_page'` to the `products_active` endpoint list, and add a sublink after the «Блокировки» link:
```jinja
                    <a href="{{ url_for('card_quality_page') }}" class="sidebar-sublink {% if request.endpoint == 'card_quality_page' %}active{% endif %}">Качество</a>
```

- [ ] **Step 4: Fix the rating calibration in blocked_cards.html**

In `templates/blocked_cards.html`, add the macro import at the very top (line 1, before `{% extends %}` is not allowed — put the import right after `{% extends "base.html" %}`):
```jinja
{% from "macros/components.html" import wb_rating_badge %}
```
Then replace BOTH occurrences of the inline rating `<td>` (the shadowed-tab block at ~:396-405 and the duplicated blocked-tab block) with:
```jinja
                        <td class="px-6 py-4 whitespace-nowrap text-sm">{{ wb_rating_badge(card.nm_rating, 10) }}</td>
```
This switches the thresholds from the wrong 0–5 (`>=4`/`>=3`) to the correct 0–10 (`>=8`/`>=6`).

- [ ] **Step 5: Manual verification (no unit test — templates/routes)**

Start the app and verify visually (see `/run`):
1. Sidebar → Товары → «Качество» opens `/card-quality`; KPI cards and the table render; no console errors.
2. Click «Детали» on a row → slideover opens with gauge (0–100), WB badges (0–10 and 0–5), per-dimension bars, recommendations.
3. Click «🤖 Глубокий AI-анализ» → status text updates (requires the `card-doctor` agent seeded+online; otherwise expect the 409 "офлайн" message — acceptable).
4. Click «Обновить рейтинги» → toast appears; after the background sync, ratings/Quality populate.
5. Open `/blocked-cards` → rating column now uses the 0–10 calibrated badge (a 7.x shows amber, not green).

- [ ] **Step 6: Commit**

```bash
git add templates/macros/components.html templates/card_quality.html templates/base.html templates/blocked_cards.html
git commit -m "feat(card-quality): cockpit page, gauge/badge macros, nav, 0-10 calibration fix"
```

---

### Task 6: End-to-end verification

**Files:** none.

- [ ] **Step 1: Apply migrations on the running DB**

Start the app once (startup runner adds columns + table), or run `venv/bin/python migrations/add_card_quality_columns.py`. Confirm `products` has the 5 new columns and `card_rating_history` exists.

- [ ] **Step 2: Trigger a real rating sync**

Either wait for the 6h job or POST `/api/card-quality/refresh`. Confirm `Product.nm_rating` (0–10), `wb_feedback_rating` (0–5), `quality_score` populate and `card_rating_history` gets rows.

- [ ] **Step 3: Run the full test suite**

Run: `venv/bin/python -m unittest tests.test_card_quality_migration tests.test_card_quality_scorer tests.test_card_rating_sync tests.test_card_quality_detail tests.test_supplier_photo_mapping -v`
Expected: PASS (all).

- [ ] **Step 4: Verify scale correctness in UI**

Confirm a card with `productRating` ~7 shows amber (not green) on `/card-quality` and `/blocked-cards`, and `feedbackRating` renders on a 0–5 scale.

## Self-Review

- **Spec coverage:** §2.1 scorer → Task 2; §2.2 calibration → Task 5 Step 4 + `wb_rating_badge`; §3 data model + migrations → Task 1; §4 WB API + sync → Task 3 (reuses existing `get_sales_funnel_products`, noted); §5 routes → Task 4; §6 UI (page, gauge, slideover, trend, listing badge, nav) → Task 5; §7 AI layer → Task 4 (`ai-analyze`) + Task 5 (poll); §8 limits/errors/token → Task 3 (sleep 20, batch 1000), runtime uses encrypted `seller.wb_api_key`; §9 testing → Tasks 1–4 unit tests + Task 5/6 manual. All covered.
- **Placeholder scan:** backend tasks contain full code + real assertions; UI task gives full macro/page/JS code; Task 5 page logic and AI poll are complete (not "add polling"). No "TBD"/"add error handling" placeholders. The only non-code deferred item is the exact APScheduler `add_job` argument shape (Task 3 Step 6), which is "copy the existing blocked-cards job" — a concrete instruction against existing code.
- **Type consistency:** `compute_card_quality`/`product_to_card_input`/`card_quality_detail` signatures defined in Task 2 are consumed unchanged in Tasks 3–4; `parse_sales_funnel_ratings` returns `{nm_id: {'product_rating','feedback_rating'}}` produced in Task 3 Step 3 and consumed in Step 5; routes produced in Task 4 are consumed by the Task 5 JS (`/api/card-quality/list`, `/<id>`, `/<id>/ai-analyze`, `/refresh`) with matching paths; `wb_rating_badge(value, scale)` and `score_gauge(value, size)` macro signatures match their call sites.
- **Deviations from spec (intentional, noted):** (a) WB client method already exists — no new method written (spec assumed new). (b) Scorer is standalone rather than reusing `upload_readiness_validator._check_*`, because `Product` and `ImportedProduct` expose different attribute names (`photos_json` vs `photo_urls`, `subject_id` vs `wb_subject_id`); a focused scorer is cleaner and unit-testable. (c) Migrations go through `_run_startup_migrations` (the actual startup runner) + standalone script, not `run_all_migrations.py` (which is manual-only).
