# Card Quality UI Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Превратить страницу «Качество карточек» в крутой кокпит-триаж в стиле сайта, подвести продавца к слабым карточкам по всему флоу и дать безопасный движок «Предложить → Подтвердить» для улучшения карточек.

**Architecture:** Фронт — Jinja + Alpine.js + Tailwind(CDN), переиспуем дизайн-систему `sh-*` и макросы `score_gauge`/`wb_rating_badge`. Бэкенд — Flask + SQLAlchemy: детерминированный `card_quality_scorer` + новый `card_improver`; применение правок через существующий `WildberriesAPIClient.update_card` + `CardEditHistory` (аудит/откат бесплатно). Предложения агентов живут в `AgentTask.result_data`; отдельной staging-таблицы нет (Вариант ①).

**Tech Stack:** Python 3.11, Flask, SQLAlchemy, Jinja2, Alpine.js 3, Tailwind CDN, Chart.js 4, stdlib `unittest`.

Связанный spec: `docs/superpowers/specs/2026-06-17-card-quality-ui-redesign-design.md`.

## Global Constraints

- Все роуты seller-scoped: `Product.query.filter_by(seller_id=current_user.seller.id)`; проверка `current_user.seller` (и `has_valid_api_key()` там, где пишем в WB).
- Никакого автопуша в WB без подтверждения пользователя.
- Тесты — ТОЛЬКО stdlib `unittest` (`python -m unittest tests.test_NAME -v`), без pytest.
- Проза, комментарии и UI-копирайт — на русском; копирайт про **Quality Score и дельту**, не «звёзды WB подскочат».
- Переиспользовать существующее: макросы `score_gauge`/`wb_rating_badge`, компоненты `sh-*`, `$store.toasts`, авто-CSRF из `base.html` (вручную `X-CSRFToken` не добавлять), `update_card` + `CardEditHistory`, `_create_product_snapshot`.
- Дизайн-токены (CSS-переменные `base.html`): `--bg #faf9f7`, `--bg-card #fff`, `--bg-sidebar/hero #0a0a0a`, `--text #1a1a1a`, `--text-secondary #6b6b6b`, `--text-muted #9a9a9a`, `--accent #c45d3e`, `--accent-light #f0e0da`, `--border #e8e5e1`, `--font-display 'Instrument Serif'` (курсив для цифр/заголовков), `--font-body 'Inter'`; радиус 8px, без теней.
- Частые коммиты: каждая задача завершается коммитом.

## Shared Interfaces & Contracts

Эти сущности определяются в задаче-владельце; в остальных фазах их **импортируют и потребляют**, не переопределяя.

**services/card_quality_scorer.py — владелец Фаза 1, Задача 1.1:**
- `WEAK_QUALITY_THRESHOLD = 50.0`, `WEAK_WB_RATING_THRESHOLD = 6.0`
- `is_weak(quality_score, nm_rating) -> bool` — True если `quality < 50` ИЛИ `nm_rating < 6` (None игнорируется).
- `compute_quality_summary(seller_id) -> {'avg_quality': float|None, 'avg_wb_rating': float|None, 'total': int, 'need_attention': int, 'distribution': {'poor': int, 'average': int, 'good': int, 'excellent': int}}` — бакеты по `score_status`.

**services/card_quality_scorer.py — владелец Фаза 2, Задача 2.1:**
- `recompute_and_persist(product, capture_history=True) -> dict` — пересчитывает `compute_card_quality(product_to_card_input(product))`; ставит `product.quality_score`, `product.quality_breakdown_json = json.dumps(cq['dimensions'])`, `product.quality_checked_at`; при `capture_history` добавляет `CardRatingHistory`; **не делает commit** (коммитит вызывающий).

**services/card_improver.py — владелец Фаза 3:**
- `ALLOWED_FIELDS = {'title','brand','description','characteristics','dimensions','subject_id','photos'}`
- `apply_card_updates(product, updates: dict, seller, wb_client, source='card-quality') -> {'success': bool, 'fields_applied': list, 'old_quality': float|None, 'new_quality': float|None, 'wb_sync': bool, 'error': str|None}` — строит `wb_updates` из явных значений, `update_card(merge_with_existing=True, seller_id=...)`, обновляет `Product` локально, пишет `CardEditHistory` (snapshot через `_create_product_snapshot`), вызывает `recompute_and_persist`, делает `db.session.commit()`.
- `collect_weak_dimensions(detail: dict) -> list[str]` — из `detail['dimensions']` статусы `warning`/`error`, отсортированы по `weight*(100-score)`.
- `build_proposal_from_tasks(product, task_results: list[dict]) -> dict`.

**Форма proposal (что `/proposal` отдаёт фронту):** `{ '<field>': {'current': <val>, 'proposed': <val>, 'dimension': '<dim>', 'source': '<str>'} }`.

**Роуты (routes/card_quality.py):** существуют `GET /api/card-quality/list`, `GET /api/card-quality/<id>`, `POST /api/card-quality/<id>/ai-analyze`, `POST /api/card-quality/refresh`. Новые: `GET /api/card-quality/summary` (Фаза 2); `POST /api/card-quality/<id>/improve`, `POST /api/card-quality/<id>/proposal`, `POST /api/card-quality/<id>/apply` (Фаза 3).

**Русские названия измерений (для чипов/разбивки):** characteristics→«характеристики», photos→«фото», description→«описание», title→«заголовок», brand→«бренд», barcodes→«штрихкоды», price→«цена», category→«категория».

---

## Фаза 1: Редизайн кокпита + slideover (визуал, без записи в WB)

### Задача 1.1: Бэкенд-фундамент сводки — `is_weak`, `compute_quality_summary`, рефактор `/api/card-quality/list`

**Файлы:**
- Изменить: `services/card_quality_scorer.py` (добавить в конец файла, после `card_quality_detail`, строка 202)
- Изменить: `routes/card_quality.py:28-63` (рефактор `api_card_quality_list`)
- Тест: `tests/test_card_quality_summary.py`

**Интерфейсы:**
- Потребляет: `score_status(score)` (card_quality_scorer.py:24), `product_to_card_input` + `compute_card_quality` (для согласованности порогов), модели `Product` (models.py:168), `db` (models.py:13).
- Производит (владелец Фазы 1):
  - `WEAK_QUALITY_THRESHOLD = 50.0`, `WEAK_WB_RATING_THRESHOLD = 6.0`
  - `is_weak(quality_score, nm_rating) -> bool` — True если `quality<50` ИЛИ `nm_rating<6` (None игнорируется)
  - `compute_quality_summary(seller_id) -> {'avg_quality':float|None,'avg_wb_rating':float|None,'total':int,'need_attention':int,'distribution':{'poor':int,'average':int,'good':int,'excellent':int}}`
  - Фронт получает `summary.distribution` в ответе `/api/card-quality/list`.

- [ ] **Шаг 1: Написать падающий тест**
```python
# -*- coding: utf-8 -*-
"""Тест сводки качества карточек: is_weak + compute_quality_summary."""

import json
import unittest

from flask import Flask

from models import db, Product
from services.card_quality_scorer import (
    is_weak,
    compute_quality_summary,
    WEAK_QUALITY_THRESHOLD,
    WEAK_WB_RATING_THRESHOLD,
)


def _make_app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


def _product(seller_id, quality_score, nm_rating=None, wb_feedback_rating=None):
    return Product(
        seller_id=seller_id,
        nm_id=100000 + int(quality_score * 100) + seller_id,
        title='Товар',
        photos_json=json.dumps([]),
        characteristics_json=json.dumps({}),
        quality_score=quality_score,
        nm_rating=nm_rating,
        wb_feedback_rating=wb_feedback_rating,
        is_active=True,
    )


class TestIsWeak(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(WEAK_QUALITY_THRESHOLD, 50.0)
        self.assertEqual(WEAK_WB_RATING_THRESHOLD, 6.0)

    def test_weak_by_quality(self):
        self.assertTrue(is_weak(40.0, 9.0))

    def test_weak_by_rating(self):
        self.assertTrue(is_weak(90.0, 5.5))

    def test_strong_when_both_ok(self):
        self.assertFalse(is_weak(80.0, 8.0))

    def test_none_rating_ignored(self):
        self.assertFalse(is_weak(80.0, None))
        self.assertTrue(is_weak(30.0, None))

    def test_none_quality_ignored(self):
        self.assertFalse(is_weak(None, 8.0))
        self.assertTrue(is_weak(None, 4.0))


class TestComputeQualitySummary(unittest.TestCase):
    def setUp(self):
        self.app = _make_app()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        # seller 1: poor(40,nm=4), average(60,nm=9), good(75,nm=8), excellent(90,nm=10)
        db.session.add_all([
            _product(1, 40.0, nm_rating=4.0, wb_feedback_rating=3.0),
            _product(1, 60.0, nm_rating=9.0, wb_feedback_rating=4.0),
            _product(1, 75.0, nm_rating=8.0, wb_feedback_rating=4.5),
            _product(1, 90.0, nm_rating=10.0, wb_feedback_rating=5.0),
            # другой продавец — не должен попасть в сводку seller=1
            _product(2, 10.0, nm_rating=1.0, wb_feedback_rating=1.0),
        ])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_averages_scoped_to_seller(self):
        s = compute_quality_summary(1)
        self.assertEqual(s['total'], 4)
        self.assertEqual(s['avg_quality'], round((40 + 60 + 75 + 90) / 4.0, 1))
        self.assertEqual(s['avg_wb_rating'], round((4 + 9 + 8 + 10) / 4.0, 1))

    def test_distribution_buckets(self):
        s = compute_quality_summary(1)
        self.assertEqual(s['distribution'],
                         {'poor': 1, 'average': 1, 'good': 1, 'excellent': 1})

    def test_need_attention_counts_weak(self):
        # weak: poor(40) -> quality<50, average(60,nm=9) ok, good ok, excellent ok
        s = compute_quality_summary(1)
        self.assertEqual(s['need_attention'], 1)

    def test_empty_seller(self):
        s = compute_quality_summary(999)
        self.assertEqual(s['total'], 0)
        self.assertIsNone(s['avg_quality'])
        self.assertIsNone(s['avg_wb_rating'])
        self.assertEqual(s['need_attention'], 0)
        self.assertEqual(s['distribution'],
                         {'poor': 0, 'average': 0, 'good': 0, 'excellent': 0})
```

- [ ] **Шаг 2: Запустить тест — убедиться что падает**
Запуск: `python -m unittest tests.test_card_quality_summary -v`
Ожидание: FAIL (ImportError: cannot import name `is_weak` / `compute_quality_summary` / `WEAK_QUALITY_THRESHOLD` из `services.card_quality_scorer`)

- [ ] **Шаг 3: Реализовать минимальный код**
В `services/card_quality_scorer.py` дополнить импорт сверху (строка 10 — было `from typing import Dict, Any`):
```python
from typing import Dict, Any, Optional
```
В конец `services/card_quality_scorer.py` (после `card_quality_detail`, строка 202) добавить:
```python


WEAK_QUALITY_THRESHOLD = 50.0
WEAK_WB_RATING_THRESHOLD = 6.0


def is_weak(quality_score: Optional[float], nm_rating: Optional[float]) -> bool:
    """Карточка «слабая», если Quality Score < 50 ИЛИ WB-рейтинг карточки < 6.

    None-значения игнорируются (не делают карточку слабой сами по себе).
    """
    if quality_score is not None and quality_score < WEAK_QUALITY_THRESHOLD:
        return True
    if nm_rating is not None and nm_rating < WEAK_WB_RATING_THRESHOLD:
        return True
    return False


def compute_quality_summary(seller_id: int) -> Dict[str, Any]:
    """Сводка по качеству карточек продавца для кокпита.

    distribution — бакеты по score_status(quality_score): poor/average/good/excellent.
    need_attention — число «слабых» карточек по is_weak(quality_score, nm_rating).
    """
    from models import db, Product

    rows = db.session.query(Product.quality_score, Product.nm_rating).filter(
        Product.seller_id == seller_id,
        Product.is_active == True,  # noqa: E712
    ).all()

    distribution = {'poor': 0, 'average': 0, 'good': 0, 'excellent': 0}
    total = len(rows)
    need_attention = 0
    q_sum = 0.0
    q_cnt = 0
    r_sum = 0.0
    r_cnt = 0

    for quality_score, nm_rating in rows:
        if quality_score is not None:
            distribution[score_status(quality_score)] += 1
            q_sum += quality_score
            q_cnt += 1
        if nm_rating is not None:
            r_sum += nm_rating
            r_cnt += 1
        if is_weak(quality_score, nm_rating):
            need_attention += 1

    return {
        'avg_quality': round(q_sum / q_cnt, 1) if q_cnt else None,
        'avg_wb_rating': round(r_sum / r_cnt, 1) if r_cnt else None,
        'total': total,
        'need_attention': need_attention,
        'distribution': distribution,
    }
```

- [ ] **Шаг 4: Запустить тест — убедиться что проходит**
Запуск: `python -m unittest tests.test_card_quality_summary -v`
Ожидание: PASS

- [ ] **Шаг 5: Рефактор `/api/card-quality/list` на `compute_quality_summary`**
В `routes/card_quality.py` обновить импорт (строка 11):
```python
from services.card_quality_scorer import card_quality_detail, compute_quality_summary
```
Заменить блок построения summary (`routes/card_quality.py:46-58`, от `agg = db.session.query(` до закрывающей `}` объекта summary) на:
```python
            summary = compute_quality_summary(current_user.seller.id)
            summary['total'] = pagination.total
```
(Примечание: `compute_quality_summary` считает `total` по всем активным карточкам продавца; для совместимости с текущим UI, где «Карточек» показывает общее число строк выборки, оставляем `pagination.total` — оно равно общему числу при первой странице с большим `per_page`. `need_attention` и `distribution` берутся из сводки по всему каталогу.)

После замены `func` больше не используется в этом роуте, но импорт `from sqlalchemy import func` (строка 8) оставить — он может использоваться другими роутами файла; проверить `grep -n "func\." routes/card_quality.py` и удалить импорт только если совпадений не осталось.

- [ ] **Шаг 6: Прогнать существующие тесты качества (регресс)**
Запуск: `python -m unittest tests.test_card_quality_summary tests.test_card_quality_detail tests.test_card_quality_scorer -v`
Ожидание: PASS (новые порог-функции не ломают существующий scorer/detail)

- [ ] **Шаг 7: Коммит**
```bash
git add services/card_quality_scorer.py routes/card_quality.py tests/test_card_quality_summary.py && git commit -m "feat(card-quality): is_weak + compute_quality_summary с distribution, рефактор list-API"
```

---

### Задача 1.2: Тёмный hero + сводная лента (gauge, бар распределения, WB-бейджи)

**Файлы:**
- Изменить: `templates/card_quality.html:5-23` (заменить hero `sh-page-header` и блок `sh-stat-grid`)
- Изменить: `templates/card_quality.html:83-160` (Alpine `cardQualityPage()`: добавить `improveWeak()`, `gaugeRing()`, поправить `load()` под `summary.distribution`)

**Интерфейсы:**
- Потребляет: `summary` из `/api/card-quality/list` (теперь содержит `avg_quality`, `avg_wb_rating`, `total`, `need_attention`, `distribution`), макрос `score_gauge` (components.html:289) — недоступен из JS-данных, поэтому кольцо рисуется inline тем же `conic-gradient`-паттерном.
- Производит: визуальную сводную ленту; кнопка «⚡ Улучшить слабые» — заглушка под Фазу 3.

- [ ] **Шаг 1: Заменить hero и stat-grid**
В `templates/card_quality.html` заменить блок строк 6-23 (от `<div class="sh-page-header">` до закрывающего `</div>` сразу за `sh-stat-grid`) на:
```html
  <div class="sh-page-header"><div class="sh-page-header-content">
    <div><h1 class="sh-page-title">Качество карточек</h1>
      <p class="sh-page-subtitle">WB-рейтинг и наш Quality Score</p></div>
    <div class="sh-page-actions">
      <button @click="refresh()" :disabled="refreshing" class="sh-btn sh-btn--white-outline sh-btn--sm" x-text="refreshing ? 'Обновление…' : 'Обновить рейтинги'"></button>
      <button @click="improveWeak()" :disabled="!summary.need_attention" class="sh-btn sh-btn--accent sh-btn--sm">⚡ Улучшить слабые</button>
    </div>
  </div></div>

  {# ── Сводная лента ── #}
  <div class="sh-card" style="margin-bottom:32px">
    <div style="display:flex;gap:32px;align-items:center;flex-wrap:wrap">
      {# Кольцо среднего Quality Score (inline conic-gradient, как макрос score_gauge) #}
      <div style="flex:0 0 auto;text-align:center">
        <div :style="gaugeRing(summary.avg_quality, 96)">
          <div :style="gaugeRingInner(summary.avg_quality, 96)" x-text="summary.avg_quality ?? '—'"></div>
        </div>
        <div style="font-size:12px;color:var(--text-muted);margin-top:8px">Средний Quality Score</div>
      </div>

      {# Полоса распределения здоровья каталога #}
      <div style="flex:1 1 320px;min-width:260px">
        <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px">
          <span style="font-size:13px;color:var(--text-secondary)">Здоровье каталога</span>
          <span style="font-size:13px;color:var(--text-secondary)">Карточек: <b x-text="summary.total ?? 0"></b></span>
        </div>
        <div style="display:flex;height:14px;border-radius:7px;overflow:hidden;background:var(--border)">
          <template x-for="seg in distSegments" :key="seg.key">
            <div :style="'width:'+seg.pct+'%;background:'+seg.color"
                 :title="seg.label+': '+seg.count"></div>
          </template>
        </div>
        <div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:10px">
          <template x-for="seg in distSegments" :key="seg.key">
            <div style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-secondary)">
              <span :style="'width:10px;height:10px;border-radius:2px;display:inline-block;background:'+seg.color"></span>
              <span x-text="seg.label"></span>
              <b x-text="seg.count"></b>
            </div>
          </template>
        </div>
      </div>

      {# WB-бейджи + требуют внимания #}
      <div style="flex:0 0 auto;display:flex;flex-direction:column;gap:10px">
        <div style="font-size:13px;color:var(--text-secondary)">Средний WB-рейтинг карточек</div>
        <div x-html="wbBadge(summary.avg_wb_rating, 10)" style="font-size:20px"></div>
        <div style="font-size:13px;color:var(--text-secondary);margin-top:6px">Требуют внимания</div>
        <div style="font-family:var(--font-display);font-style:italic;font-size:28px;color:var(--accent)" x-text="summary.need_attention ?? 0"></div>
      </div>
    </div>
  </div>
```

- [ ] **Шаг 2: Добавить в Alpine `cardQualityPage()` производные `distSegments` и хелперы кольца**
В `templates/card_quality.html`, в объекте, возвращаемом из `cardQualityPage()`, после геттера `needAttention` (строка 87) добавить:
```javascript
    get distSegments() {
      const d = (this.summary && this.summary.distribution) || {};
      const defs = [
        { key:'poor',      label:'Низкое',     color:'#dc2626', count: d.poor || 0 },
        { key:'average',   label:'Среднее',    color:'#d97706', count: d.average || 0 },
        { key:'good',      label:'Хорошее',    color:'#16a34a', count: d.good || 0 },
        { key:'excellent', label:'Отличное',   color:'#15803d', count: d.excellent || 0 },
      ];
      const total = defs.reduce((s, x) => s + x.count, 0) || 1;
      return defs.map(x => ({ ...x, pct: (x.count / total * 100) }));
    },
```
И в том же объекте, рядом с `gaugeStyle` (строка 155), добавить хелперы кольца (повторяют макрос `score_gauge`, но управляются данными):
```javascript
    gaugeRing(v, size) {
      const val = (v ?? 0);
      const col = val >= 70 ? '#16a34a' : val >= 50 ? '#d97706' : '#dc2626';
      const s = size || 96;
      return `width:${s}px;height:${s}px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:conic-gradient(${col} ${val}%, #ece9e4 0)`;
    },
    gaugeRingInner(v, size) {
      const val = (v ?? 0);
      const col = val >= 70 ? '#16a34a' : val >= 50 ? '#d97706' : '#dc2626';
      const s = (size || 96) - 14;
      return `width:${s}px;height:${s}px;border-radius:50%;background:#fff;display:flex;align-items:center;justify-content:center;font-family:var(--font-display);font-style:italic;font-size:${Math.round((size||96)/3.2)}px;color:${col}`;
    },
    improveWeak() {
      this.$store.toasts.info('Массовое улучшение слабых карточек появится скоро', 'Скоро');
    },
```

- [ ] **Шаг 3: Визуальная проверка**
Открыть `/card-quality` у продавца с настроенным API-ключом и хотя бы несколькими карточками. Убедиться, что:
  - Тёмный hero показывает заголовок и две кнопки: «Обновить рейтинги» и «⚡ Улучшить слабые» (вторая дизейблится, если `need_attention=0`; по клику — тост «Скоро»).
  - Слева в сводной ленте — кольцо со средним Quality Score (зелёное ≥70 / оранжевое ≥50 / красное <50), число по центру курсивом.
  - Сегментированный бар «Здоровье каталога» делится на 4 цвета (красный/оранжевый/зелёный/тёмно-зелёный) пропорционально `distribution`, под ним легенда с числами.
  - Справа — звёздный WB-бейдж среднего рейтинга и крупное число «Требуют внимания».
  - Сумма чисел в легенде = числу активных карточек.

- [ ] **Шаг 4: Коммит**
```bash
git add templates/card_quality.html && git commit -m "feat(card-quality): тёмный hero + сводная лента (gauge, бар здоровья каталога, WB-бейджи)"
```

---

### Задача 1.3: Триаж-лист «Требуют внимания» / «В порядке» с чипами проблем и скелетонами

**Файлы:**
- Изменить: `services/card_quality_scorer.py:185-201` (добавить поле `photos` в `card_quality_detail`)
- Изменить: `templates/card_quality.html:25-43` (заменить таблицу на сгруппированный триаж-лист)
- Изменить: `templates/card_quality.html` (Alpine: `loading`, геттеры групп, `DIM_LABELS`, `weakChips()`, `firstPhoto()`)
- Тест: `tests/test_card_quality_detail.py` (дополнить проверкой нового поля `photos`)

**Интерфейсы:**
- Потребляет: items из `/api/card-quality/list` (каждый = `card_quality_detail(p)`), `is_weak`-логика повторена на фронте через `quality_score`/`wb_product_rating`, макрос-кольцо `gaugeStyle` (size 40).
- Производит: `card_quality_detail(...)['photos']` — список URL фото (новое поле, обратно совместимо: только добавление ключа); фронт-хелперы `DIM_LABELS`, `weakChips`, `firstPhoto`.

- [ ] **Шаг 1: Дополнить падающий тест на `photos` в detail**
В `tests/test_card_quality_detail.py` добавить метод в класс `TestCardQualityDetail`:
```python
    def test_includes_photos_list_for_thumbnail(self):
        product = types.SimpleNamespace(
            id=2, nm_id=200, vendor_code='SKU-2', title='Товар',
            photos_json=json.dumps(['https://x/1.jpg', 'https://x/2.jpg']),
            characteristics_json=json.dumps({}),
            sizes_json=json.dumps([]),
            description='', brand='', price=0, subject_id=None,
            nm_rating=None, wb_feedback_rating=None,
            nm_rating_checked_at=None,
        )
        d = card_quality_detail(product)
        self.assertEqual(d['photos'], ['https://x/1.jpg', 'https://x/2.jpg'])

    def test_photos_empty_when_none(self):
        product = types.SimpleNamespace(
            id=3, nm_id=300, vendor_code='SKU-3', title='Товар',
            photos_json=None, characteristics_json=None, sizes_json=None,
            description='', brand='', price=0, subject_id=None,
            nm_rating=None, wb_feedback_rating=None, nm_rating_checked_at=None,
        )
        d = card_quality_detail(product)
        self.assertEqual(d['photos'], [])
```

- [ ] **Шаг 2: Запустить тест — убедиться что падает**
Запуск: `python -m unittest tests.test_card_quality_detail -v`
Ожидание: FAIL (KeyError: `'photos'` — ключа ещё нет в payload)

- [ ] **Шаг 3: Добавить `photos` в `card_quality_detail`**
В `services/card_quality_scorer.py` в `card_quality_detail` (тело начинается на строке 187) после `cq = compute_card_quality(product_to_card_input(product))` (строка 187) и `checked = ...` (строка 188) вставить вычисление фото и добавить ключ в возвращаемый dict. Заменить строку 187:
```python
    cq = compute_card_quality(product_to_card_input(product))
```
на:
```python
    card_input = product_to_card_input(product)
    cq = compute_card_quality(card_input)
    photos = card_input.get('photos') or []
```
и в возвращаемый dict (между строкой 196 `'nm_rating_checked_at': ...` и строкой 197 `'quality_score': ...`) добавить:
```python
        'photos': photos,
```
(Обратная совместимость: добавляется только новый ключ `photos`, существующие поля не меняются; `card_quality_detail` уже отдаёт `dimensions` для чипов проблем.)

- [ ] **Шаг 4: Запустить тест — убедиться что проходит**
Запуск: `python -m unittest tests.test_card_quality_detail -v`
Ожидание: PASS

- [ ] **Шаг 5: Заменить таблицу на триаж-лист**
В `templates/card_quality.html` заменить блок строк 25-43 (от `<div class="sh-card" style="padding:0;overflow:hidden">` до его закрывающего `</div>` сразу перед комментарием `{# ── Slideover деталь ── #}`) на:
```html
  {# ── Скелетоны загрузки ── #}
  <div x-show="loading" class="sh-card" style="margin-bottom:24px">
    <template x-for="i in 5" :key="i">
      <div style="display:flex;gap:16px;align-items:center;padding:12px 0;border-bottom:1px solid var(--border)">
        <div style="width:48px;height:48px;border-radius:8px;background:var(--border)"></div>
        <div style="flex:1">
          <div style="width:40%;height:12px;border-radius:4px;background:var(--border);margin-bottom:8px"></div>
          <div style="width:60%;height:10px;border-radius:4px;background:var(--border)"></div>
        </div>
        <div style="width:40px;height:40px;border-radius:50%;background:var(--border)"></div>
      </div>
    </template>
  </div>

  {# ── Группа: требуют внимания ── #}
  <div x-show="!loading" class="sh-card" style="margin-bottom:20px">
    <div class="sh-card-header">
      <h3 class="sh-card-title">Требуют внимания · <span x-text="weakItems.length"></span></h3>
    </div>
    <template x-if="weakItems.length === 0">
      <div class="sh-empty"><p class="sh-empty-title">Слабых карточек нет — каталог в порядке</p></div>
    </template>
    <template x-for="it in weakItems" :key="it.product_id">
      <div class="cq-row cq-row--weak" @click="openDetail(it.product_id)">
        <div class="cq-thumb">
          <template x-if="firstPhoto(it)"><img :src="firstPhoto(it)" alt="" loading="lazy"></template>
          <template x-if="!firstPhoto(it)"><span class="cq-thumb-ph">нет фото</span></template>
        </div>
        <div class="cq-main">
          <div class="cq-title"><b x-text="it.nm_id"></b> · <span x-text="it.title || '—'"></span></div>
          <div class="cq-chips">
            <template x-for="chip in weakChips(it)" :key="chip.name">
              <span class="sh-badge" :class="chip.cls" x-text="chip.label"></span>
            </template>
          </div>
        </div>
        <div class="cq-ratings">
          <span x-html="wbBadge(it.wb_product_rating, 10)"></span>
        </div>
        <div :style="gaugeStyle(it.quality_score, 40)" x-text="it.quality_score ?? '—'"></div>
        <div class="cq-actions" @click.stop>
          <button class="sh-btn sh-btn--accent sh-btn--sm" disabled title="Появится в Фазе 3">Улучшить</button>
          <button class="sh-btn sh-btn--ghost sh-btn--sm" @click="openDetail(it.product_id)">Детали</button>
        </div>
      </div>
    </template>
  </div>

  {# ── Группа: в порядке (свёрнута) ── #}
  <div x-show="!loading" class="sh-card" x-data="{ open: false }">
    <div class="sh-card-header" style="cursor:pointer" @click="open = !open">
      <h3 class="sh-card-title">В порядке · <span x-text="okItems.length"></span></h3>
      <span class="sh-btn sh-btn--ghost sh-btn--sm" x-text="open ? 'Свернуть' : 'Показать'"></span>
    </div>
    <div x-show="open" x-cloak>
      <template x-for="it in okItems" :key="it.product_id">
        <div class="cq-row" @click="openDetail(it.product_id)">
          <div class="cq-thumb">
            <template x-if="firstPhoto(it)"><img :src="firstPhoto(it)" alt="" loading="lazy"></template>
            <template x-if="!firstPhoto(it)"><span class="cq-thumb-ph">нет фото</span></template>
          </div>
          <div class="cq-main">
            <div class="cq-title"><b x-text="it.nm_id"></b> · <span x-text="it.title || '—'"></span></div>
          </div>
          <div class="cq-ratings"><span x-html="wbBadge(it.wb_product_rating, 10)"></span></div>
          <div :style="gaugeStyle(it.quality_score, 40)" x-text="it.quality_score ?? '—'"></div>
          <div class="cq-actions" @click.stop>
            <button class="sh-btn sh-btn--ghost sh-btn--sm" @click="openDetail(it.product_id)">Детали</button>
          </div>
        </div>
      </template>
    </div>
  </div>
```

- [ ] **Шаг 6: Добавить стили строк триаж-листа**
В `templates/card_quality.html`, сразу после открывающего `<div x-data="cardQualityPage()" x-init="init()">` (строка 5), добавить `<style>`-блок:
```html
<style>
  .cq-row{display:flex;gap:16px;align-items:center;padding:12px 4px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .12s}
  .cq-row:last-child{border-bottom:none}
  .cq-row:hover{background:var(--bg)}
  .cq-row--weak{border-left:3px solid var(--accent);padding-left:12px}
  .cq-thumb{flex:0 0 48px;width:48px;height:48px;border-radius:8px;overflow:hidden;background:var(--border);display:flex;align-items:center;justify-content:center}
  .cq-thumb img{width:100%;height:100%;object-fit:cover}
  .cq-thumb-ph{font-size:9px;color:var(--text-muted);text-align:center}
  .cq-main{flex:1 1 auto;min-width:0}
  .cq-title{font-size:14px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .cq-chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
  .cq-ratings{flex:0 0 auto}
  .cq-actions{flex:0 0 auto;display:flex;gap:8px}
</style>
```

- [ ] **Шаг 7: Дополнить Alpine `cardQualityPage()`: флаг загрузки, группы, лейблы, чипы, фото**
В `templates/card_quality.html` в объекте `cardQualityPage()`:

(а) В блок состояния (строка 85) добавить `loading: true,` и таблицу русских лейблов измерений:
```javascript
    items: [], summary: {}, detail: null, drawer: false, loading: true,
    DIM_LABELS: { characteristics:'характеристики', photos:'фото', description:'описание',
                  title:'заголовок', brand:'бренд', barcodes:'штрихкоды', price:'цена', category:'категория' },
```
(заменив существующую строку `items: [], summary: {}, detail: null, drawer: false,`).

(б) После геттера `distSegments` (добавлен в Задаче 1.2) добавить геттеры групп:
```javascript
    get weakItems() {
      return this.items.filter(it =>
        (it.quality_score != null && it.quality_score < 50) ||
        (it.wb_product_rating != null && it.wb_product_rating < 6));
    },
    get okItems() {
      return this.items.filter(it => !(
        (it.quality_score != null && it.quality_score < 50) ||
        (it.wb_product_rating != null && it.wb_product_rating < 6)));
    },
```

(в) Обновить `load()` (строки 89-93), чтобы управлял `loading`:
```javascript
    async load() {
      this.loading = true;
      try {
        const r = await fetch('/api/card-quality/list?sort=quality_score&order=asc&per_page=100');
        const d = await r.json();
        this.items = d.items || []; this.summary = d.summary || {};
      } finally {
        this.loading = false;
      }
    },
```

(г) Рядом с `barColor` (строка 154) добавить хелперы чипов и первого фото:
```javascript
    firstPhoto(it) {
      const p = (it && it.photos) || [];
      return p.length ? p[0] : null;
    },
    weakChips(it) {
      const dims = (it && it.dimensions) || {};
      return Object.keys(dims)
        .filter(name => dims[name].status === 'warning' || dims[name].status === 'error')
        .sort((a, b) => (dims[b].weight * (100 - dims[b].score)) - (dims[a].weight * (100 - dims[a].score)))
        .map(name => ({
          name,
          label: this.DIM_LABELS[name] || name,
          cls: dims[name].status === 'error' ? 'sh-badge--red' : 'sh-badge--yellow',
        }));
    },
```

- [ ] **Шаг 8: Запустить регресс детали и сводки**
Запуск: `python -m unittest tests.test_card_quality_detail tests.test_card_quality_summary -v`
Ожидание: PASS

- [ ] **Шаг 9: Визуальная проверка**
Открыть `/card-quality`. Убедиться, что:
  - При загрузке кратко видны 5 строк-скелетонов (серые плейсхолдеры), затем они исчезают.
  - Блок «Требуют внимания · N» раскрыт, содержит слабые карточки; у каждой строки слева акцент-бордер, миниатюра (фото или плейсхолдер «нет фото»), артикул+название, чипы проблем по-русски (красные для error, жёлтые для warning), WB-бейдж, мини-кольцо Quality (40px), кнопки «Улучшить» (disabled) и «Детали».
  - Блок «В порядке · N» свёрнут; по клику на заголовок раскрывается список без акцент-бордера.
  - Клик по строке (вне кнопок) открывает slideover; клик по «Улучшить»/«Детали» не дублирует открытие лишний раз (`@click.stop`).
  - Hover подсвечивает строку.

- [ ] **Шаг 10: Коммит**
```bash
git add services/card_quality_scorer.py templates/card_quality.html tests/test_card_quality_detail.py && git commit -m "feat(card-quality): триаж-лист (слабые/в порядке) с миниатюрами, чипами проблем и скелетонами; photos в detail"
```

---

### Задача 1.4: Slideover-деталь — разбивка по 8 измерениям, тренд, блок действий

**Файлы:**
- Изменить: `templates/card_quality.html:45-79` (переработать содержимое slideover, классы `sh-slideover--lg`)
- Изменить: `templates/card_quality.html` (Alpine: empty/loading в детали; `aiAnalyze`/`renderTrend` сохранить без изменений)

**Интерфейсы:**
- Потребляет: `/api/card-quality/<id>` → `{success, data:{...detail, trend:[...]}}` (routes/card_quality.py:65-85), хелперы `wbBadge`, `barColor`, `gaugeStyle`/`gaugeRing`, `DIM_LABELS` (Задача 1.3), существующий `renderTrend` (Chart.js) и `aiAnalyze`.
- Производит: визуальную деталь; кнопка «⚡ Улучшить карточку» — заглушка под Фазу 3; «🤖 Глубокий AI-анализ» — существующий `aiAnalyze`.

- [ ] **Шаг 1: Переработать разметку slideover**
В `templates/card_quality.html` заменить блок строк 45-79 (от `{# ── Slideover деталь ── #}` до закрывающего `</div>` slideover перед `<script src=...chart.js...>`) на:
```html
  {# ── Slideover деталь ── #}
  <div x-show="drawer" x-cloak class="sh-slideover-backdrop" @click="drawer=false"></div>
  <div x-show="drawer" x-cloak class="sh-slideover sh-slideover--lg">
    <div class="sh-slideover-panel">
      <div class="sh-slideover-header">
        <div class="sh-slideover-title" x-text="detail ? ('Артикул ' + (detail.nm_id||'')) : 'Карточка'"></div>
        <button class="sh-btn sh-btn--ghost sh-btn--sm" @click="drawer=false" aria-label="Закрыть">✕</button>
      </div>

      <div class="sh-slideover-body">
        {# Загрузка #}
        <template x-if="drawer && !detail">
          <div class="sh-empty"><p class="sh-empty-title">Загрузка карточки…</p></div>
        </template>

        <template x-if="detail">
          <div>
            {# Шапка: кольцо + WB-бейджи + обновлено #}
            <div style="display:flex;gap:24px;align-items:center;margin-bottom:20px">
              <div :style="gaugeRing(detail.quality_score, 72)">
                <div :style="gaugeRingInner(detail.quality_score, 72)" x-text="detail.quality_score ?? '—'"></div>
              </div>
              <div style="display:flex;flex-direction:column;gap:4px">
                <div style="font-size:13px;color:var(--text-secondary)">WB: оценка карточки
                  <span x-html="wbBadge(detail.wb_product_rating, 10)"></span></div>
                <div style="font-size:13px;color:var(--text-secondary)">WB: по отзывам
                  <span x-html="wbBadge(detail.wb_feedback_rating, 5)"></span></div>
                <div style="font-size:11px;color:var(--text-muted)"
                     x-text="detail.nm_rating_checked_at ? ('обновлено ' + fmtDate(detail.nm_rating_checked_at)) : 'рейтинг ещё не синхронизирован'"></div>
              </div>
            </div>

            {# Разбивка по 8 измерениям #}
            <div class="sh-section-title"><span>Разбор Quality Score</span></div>
            <template x-for="(d, name) in detail.dimensions" :key="name">
              <div style="margin:10px 0">
                <div style="display:flex;justify-content:space-between;align-items:baseline;font-size:13px">
                  <span style="color:var(--text)" x-text="DIM_LABELS[name] || name"></span>
                  <span style="color:var(--text-muted)">
                    <span x-text="d.score + '%'"></span>
                    <span style="font-size:11px"> · вес <span x-text="d.weight"></span>%</span>
                  </span>
                </div>
                <div style="height:8px;background:#ece9e4;border-radius:4px;margin:4px 0">
                  <div :style="'height:8px;border-radius:4px;width:'+d.score+'%;background:'+barColor(d.status)"></div>
                </div>
                <div style="font-size:12px;color:var(--text-secondary)" x-show="d.hint" x-text="d.hint"></div>
              </div>
            </template>

            {# Тренд (СОХРАНЁН renderTrend) #}
            <div class="sh-section-title" style="margin-top:20px"><span>Тренд (Quality Score / рейтинг WB)</span></div>
            <div style="height:160px"><canvas x-ref="trendChart"></canvas></div>
            <div x-show="!detail.trend || !detail.trend.length" style="font-size:12px;color:var(--text-muted)">
              Пока нет истории — появится после нескольких синхронизаций</div>

            {# Действия #}
            <div style="display:flex;gap:8px;margin-top:20px;flex-wrap:wrap">
              <button class="sh-btn sh-btn--accent sh-btn--sm" disabled title="Появится в Фазе 3">⚡ Улучшить карточку</button>
              <button class="sh-btn sh-btn--primary sh-btn--sm" @click="aiAnalyze(detail.product_id)" :disabled="aiRunning">🤖 Глубокий AI-анализ</button>
            </div>
            <div x-show="aiRunning" style="font-size:12px;color:var(--text-secondary);margin-top:8px" x-text="aiStatus"></div>
          </div>
        </template>
      </div>
    </div>
  </div>
```

- [ ] **Шаг 2: Добавить хелпер `fmtDate` в Alpine**
В `templates/card_quality.html` в объекте `cardQualityPage()`, рядом с `barColor`/`firstPhoto` (около строки 154), добавить:
```javascript
    fmtDate(iso) {
      if (!iso) return '';
      const d = new Date(iso);
      return isNaN(d) ? iso : d.toLocaleString('ru-RU', { day:'numeric', month:'short', hour:'2-digit', minute:'2-digit' });
    },
```
`renderTrend` и `aiAnalyze` оставить без изменений (slideover по-прежнему содержит `x-ref="trendChart"` внутри `x-if="detail"`, а `openDetail` вызывает `this.$nextTick(() => this.renderTrend())`).

- [ ] **Шаг 3: Подтвердить наличие классов slideover в base.html**
Проверить присутствие `sh-slideover--lg`, `sh-slideover-panel/-header/-title/-body` (используются в разметке).
Запуск: `grep -n "sh-slideover--lg\|sh-slideover-panel\|sh-slideover-header\|sh-slideover-title\|sh-slideover-body" templates/base.html`
Ожидание: классы найдены. Если `sh-slideover--lg` отсутствует — добавить шаг с CSS (ширина панели, напр. `.sh-slideover--lg .sh-slideover-panel{max-width:640px}`) в `<style>` base.html; иначе шаг пропустить.

- [ ] **Шаг 4: Визуальная проверка + регресс Chart.js и aiAnalyze**
Открыть `/card-quality`, кликнуть «Детали» у карточки. Убедиться, что:
  - Slideover широкий (`--lg`), с заголовком «Артикул N» и крестиком закрытия.
  - До прихода данных видно «Загрузка карточки…».
  - Шапка: радиальное кольцо Quality Score (цвет по порогам), оба WB-бейджа, строка «обновлено …» (или «рейтинг ещё не синхронизирован»).
  - Раздел «Разбор Quality Score» показывает все 8 измерений с русскими подписями, полосой прогресса (зелёная ok / оранжевая warning / красная error), подсказкой `hint` и весом `· вес N%`.
  - Раздел «Тренд»: Chart.js рисует две линии (Quality Score и WB рейтинг) при наличии истории; иначе — текст «Пока нет истории…». (Регресс: график строится так же, как раньше — `renderTrend` не менялся.)
  - Кнопка «⚡ Улучшить карточку» — disabled. Кнопка «🤖 Глубокий AI-анализ» запускает агентов и поллит статус (регресс: `aiAnalyze` работает как прежде, под ней появляется строка статуса).
  - Клик по бэкдропу или крестику закрывает slideover.

- [ ] **Шаг 5: Коммит**
```bash
git add templates/card_quality.html && git commit -m "feat(card-quality): slideover-деталь с разбором по 8 измерениям, весами, трендом и блоком действий"
```

---

## Фаза 2: Подсказки во флоу

### Задача 2.1: `recompute_and_persist` в card_quality_scorer

**Файлы:**
- Изменить: `services/card_quality_scorer.py` (добавить функцию после `card_quality_detail`, конец файла :202)
- Тест: `tests/test_recompute_and_persist.py`

**Интерфейсы:**
- Потребляет: `compute_card_quality(card_dict)`, `product_to_card_input(product)` (этот же модуль, :116 и :150); `models.db`, `models.CardRatingHistory` (:2061); `score_status` (:24).
- Производит: `recompute_and_persist(product, capture_history=True) -> dict` — пересчитывает Quality Score, выставляет `product.quality_score`, `product.quality_breakdown_json`, `product.quality_checked_at`; при `capture_history` добавляет `CardRatingHistory` в сессию; НЕ коммитит. Потребляется Фазой 3 (`card_improver.apply_card_updates`).

- [ ] **Шаг 1: Написать падающий тест**
```python
# -*- coding: utf-8 -*-
"""Тест recompute_and_persist: персист Quality Score + снимок CardRatingHistory."""

import json
import os
import tempfile
import unittest
from datetime import datetime


class TestRecomputeAndPersist(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._db_fd, cls._db_path = tempfile.mkstemp(suffix='.db')
        os.close(cls._db_fd)
        os.environ['DATABASE_URL'] = 'sqlite:///' + cls._db_path
        os.environ['DISABLE_SECURE_COOKIE'] = '1'
        import seller_platform  # noqa: импорт инициализирует app + db
        cls.app = seller_platform.app
        cls.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + cls._db_path
        cls.app.config['WTF_CSRF_ENABLED'] = False
        from models import db
        cls.db = db
        cls.ctx = cls.app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        cls.db.session.remove()
        cls.db.drop_all()
        cls.ctx.pop()
        os.remove(cls._db_path)

    def _make_seller(self):
        from models import User, Seller
        user = User(username='u1', email='u1@example.com', password_hash='x')
        self.db.session.add(user)
        self.db.session.flush()
        seller = Seller(user_id=user.id, company_name='ООО Тест')
        self.db.session.add(seller)
        self.db.session.flush()
        return seller

    def test_persists_score_breakdown_and_history(self):
        from models import Product, CardRatingHistory
        from services.card_quality_scorer import recompute_and_persist

        seller = self._make_seller()
        product = Product(
            seller_id=seller.id, nm_id=105146863, vendor_code='SKU-1', title='Хороший товар детальный',
            photos_json=json.dumps(['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h']),
            characteristics_json=json.dumps({'Цвет': 'к', 'Размер': 'M', 'Состав': 'хлопок'}),
            sizes_json=json.dumps([{'skus': ['111']}]),
            description='d' * 400, brand='Бренд', price=999, subject_id=64,
            nm_rating=8.0, wb_feedback_rating=4.8,
        )
        self.db.session.add(product)
        self.db.session.flush()

        result = recompute_and_persist(product, capture_history=True)

        self.assertIsInstance(result, dict)
        self.assertIn('score', result)
        self.assertIsNotNone(product.quality_score)
        self.assertEqual(product.quality_score, result['score'])
        breakdown = json.loads(product.quality_breakdown_json)
        self.assertIn('photos', breakdown)
        self.assertIn('characteristics', breakdown)
        self.assertIsInstance(product.quality_checked_at, datetime)

        rows = CardRatingHistory.query.filter_by(product_id=product.id).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].seller_id, seller.id)
        self.assertEqual(rows[0].nm_id, 105146863)
        self.assertEqual(rows[0].quality_score, result['score'])
        self.assertEqual(rows[0].wb_product_rating, 8.0)
        self.assertEqual(rows[0].wb_feedback_rating, 4.8)

    def test_capture_history_false_skips_snapshot(self):
        from models import Product, CardRatingHistory
        from services.card_quality_scorer import recompute_and_persist

        seller = self._make_seller()
        product = Product(seller_id=seller.id, nm_id=100142591, vendor_code='SKU-2', title='Товар')
        self.db.session.add(product)
        self.db.session.flush()

        recompute_and_persist(product, capture_history=False)

        self.assertIsNotNone(product.quality_score)
        rows = CardRatingHistory.query.filter_by(product_id=product.id).all()
        self.assertEqual(len(rows), 0)


if __name__ == '__main__':
    unittest.main()
```
- [ ] **Шаг 2: Запустить тест — убедиться что падает**
Запуск: `python -m unittest tests.test_recompute_and_persist -v`
Ожидание: FAIL (`ImportError: cannot import name 'recompute_and_persist' from 'services.card_quality_scorer'`)
- [ ] **Шаг 3: Реализовать минимальный код**
В `services/card_quality_scorer.py` заменить строку импорта `from datetime import datetime` отсутствует — добавить её к шапке (рядом с `import json`, :9), затем добавить функцию в конец файла (после `card_quality_detail`, :202):
```python
def recompute_and_persist(product, capture_history: bool = True) -> Dict[str, Any]:
    """Пересчитать Quality Score карточки и записать его в Product.

    Выставляет product.quality_score, product.quality_breakdown_json (JSON разбивки
    по измерениям) и product.quality_checked_at. При capture_history=True добавляет
    снимок CardRatingHistory в сессию. НЕ делает commit — коммитит вызывающий код.
    Возвращает результат compute_card_quality (score, status, dimensions, recommendations).
    """
    from models import db, CardRatingHistory

    cq = compute_card_quality(product_to_card_input(product))

    product.quality_score = cq['score']
    product.quality_breakdown_json = json.dumps(cq['dimensions'], ensure_ascii=False)
    product.quality_checked_at = datetime.utcnow()

    if capture_history:
        db.session.add(CardRatingHistory(
            seller_id=getattr(product, 'seller_id', None),
            product_id=getattr(product, 'id', None),
            nm_id=getattr(product, 'nm_id', None),
            wb_product_rating=getattr(product, 'nm_rating', None),
            wb_feedback_rating=getattr(product, 'wb_feedback_rating', None),
            quality_score=cq['score'],
        ))

    return cq
```
И добавить импорт `datetime` в шапку файла (:9-10), заменив:
```python
import json
from typing import Dict, Any
```
на:
```python
import json
from datetime import datetime
from typing import Dict, Any
```
- [ ] **Шаг 4: Запустить тест — убедиться что проходит**
Запуск: `python -m unittest tests.test_recompute_and_persist -v`
Ожидание: PASS
- [ ] **Шаг 5: Коммит**
```bash
git add services/card_quality_scorer.py tests/test_recompute_and_persist.py && git commit -m "feat(card-quality): recompute_and_persist — персист Quality Score + снимок CardRatingHistory"
```

---

### Задача 2.2: Виджет «Качество карточек» на дашборде

**Файлы:**
- Изменить: `routes/card_quality.py` (добавить роут `GET /api/card-quality/summary` после `api_card_quality_list`, :64)
- Изменить: `templates/dashboard.html` (добавить секцию виджета после блока `dash-stats`, :298; добавить контроллер `cardQualityWidget()` в `<script>`, :522)
- Тест: `tests/test_card_quality_summary_route.py`

**Интерфейсы:**
- Потребляет: `compute_quality_summary(seller_id)` из `services/card_quality_scorer.py` (владелец — Фаза 1; возвращает `{'avg_quality','avg_wb_rating','total','need_attention','distribution':{'poor','average','good','excellent'}}`); макрос `score_gauge(value, size)` из `templates/macros/components.html` (:289); `url_for('card_quality_page')` (routes/card_quality.py:22).
- Производит: JSON `{'success':True,'data': <summary>}` по `GET /api/card-quality/summary`; фронт-контроллер `cardQualityWidget()`.

- [ ] **Шаг 1: Написать падающий тест (роут)**
```python
# -*- coding: utf-8 -*-
"""Тест роута GET /api/card-quality/summary (seller-scoped JSON)."""

import os
import tempfile
import unittest


class TestCardQualitySummaryRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._db_fd, cls._db_path = tempfile.mkstemp(suffix='.db')
        os.close(cls._db_fd)
        os.environ['DATABASE_URL'] = 'sqlite:///' + cls._db_path
        os.environ['DISABLE_SECURE_COOKIE'] = '1'
        import seller_platform  # noqa
        cls.app = seller_platform.app
        cls.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + cls._db_path
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.app.config['TESTING'] = True
        from models import db
        cls.db = db
        cls.ctx = cls.app.app_context()
        cls.ctx.push()
        db.create_all()
        cls._seed()

    @classmethod
    def _seed(cls):
        from models import User, Seller, Product
        user = User(username='seller1', email='seller1@example.com', password_hash='x')
        cls.db.session.add(user)
        cls.db.session.flush()
        seller = Seller(user_id=user.id, company_name='ООО Тест', wb_seller_id='123')
        seller.wb_api_key = 'test-api-key'
        cls.db.session.add(seller)
        cls.db.session.flush()
        cls.user_id = user.id
        cls.seller_id = seller.id
        # 2 хорошие, 1 слабая по quality, 1 слабая по nm_rating
        cls.db.session.add_all([
            Product(seller_id=seller.id, nm_id=1, vendor_code='A', is_active=True,
                    quality_score=90, nm_rating=9.0),
            Product(seller_id=seller.id, nm_id=2, vendor_code='B', is_active=True,
                    quality_score=75, nm_rating=8.0),
            Product(seller_id=seller.id, nm_id=3, vendor_code='C', is_active=True,
                    quality_score=40, nm_rating=7.0),
            Product(seller_id=seller.id, nm_id=4, vendor_code='D', is_active=True,
                    quality_score=80, nm_rating=5.0),
        ])
        cls.db.session.commit()

    @classmethod
    def tearDownClass(cls):
        cls.db.session.remove()
        cls.db.drop_all()
        cls.ctx.pop()
        os.remove(cls._db_path)

    def _client_logged_in(self):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(self.user_id)
            sess['_fresh'] = True
        return client

    def test_summary_returns_seller_scoped_data(self):
        client = self._client_logged_in()
        resp = client.get('/api/card-quality/summary')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload['success'])
        data = payload['data']
        self.assertEqual(data['total'], 4)
        self.assertEqual(data['need_attention'], 2)  # quality<50 ИЛИ nm_rating<6
        self.assertIn('distribution', data)
        self.assertIn('avg_quality', data)

    def test_requires_login(self):
        resp = self.app.test_client().get('/api/card-quality/summary')
        self.assertIn(resp.status_code, (302, 401))


if __name__ == '__main__':
    unittest.main()
```
- [ ] **Шаг 2: Запустить тест — убедиться что падает**
Запуск: `python -m unittest tests.test_card_quality_summary_route -v`
Ожидание: FAIL (404 на `/api/card-quality/summary` — роут не зарегистрирован)
- [ ] **Шаг 3: Реализовать роут**
В `routes/card_quality.py` добавить импорт `compute_quality_summary` в строку :11, заменив:
```python
from services.card_quality_scorer import card_quality_detail
```
на:
```python
from services.card_quality_scorer import card_quality_detail, compute_quality_summary
```
Затем вставить роут сразу после `api_card_quality_list` (после :64, перед `@app.route('/api/card-quality/<int:product_id>')`):
```python
    @app.route('/api/card-quality/summary')
    @login_required
    def api_card_quality_summary():
        if not current_user.seller:
            return jsonify({'error': 'Нет профиля продавца'}), 403
        try:
            data = compute_quality_summary(current_user.seller.id)
            return jsonify({'success': True, 'data': data})
        except Exception as e:
            logger.exception('Ошибка в api_card_quality_summary: %s', e)
            return jsonify({'error': 'Внутренняя ошибка'}), 500
```
- [ ] **Шаг 4: Запустить тест — убедиться что проходит**
Запуск: `python -m unittest tests.test_card_quality_summary_route -v`
Ожидание: PASS (требует, чтобы `compute_quality_summary` из Фазы 1 уже была реализована; если Фаза 1 ещё не слита — тест краснеет на `ImportError`, что корректно фиксирует зависимость)
- [ ] **Шаг 5: Добавить секцию виджета в `templates/dashboard.html`**
Вставить после закрывающего `</div>` блока `dash-stats` (после :298, перед `<!-- Quick navigation -->` :300). Использовать макрос `score_gauge`; добавить импорт макроса в начало файла (после `{% block extra_head %}` нельзя — импорт макроса в Jinja делается на верхнем уровне). В начало `templates/dashboard.html` после строки `{% extends "base.html" %}` (:1) добавить:
```jinja
{% from "macros/components.html" import score_gauge %}
```
Затем вставить секцию после :298:
```jinja
    <!-- Качество карточек -->
    {% if current_user.seller and current_user.seller.has_valid_api_key() %}
    <div class="dash-section-header">
        <span class="dash-section-title">Качество карточек</span>
        <div class="dash-section-line"></div>
    </div>

    <div class="dash-quality" x-data="cardQualityWidget()" x-init="load()">
        <div class="dash-quality-gauge">
            <template x-if="!loading && data && data.avg_quality !== null">
                <div :style="'width:96px;height:96px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:conic-gradient(' + gaugeColor() + ' ' + Math.round(data.avg_quality) + '%, #ece9e4 0)'">
                    <div style="width:80px;height:80px;border-radius:50%;background:var(--bg-card);display:flex;align-items:center;justify-content:center;font-family:var(--font-display);font-style:italic;font-size:28px"
                         :style="'color:' + gaugeColor()" x-text="Math.round(data.avg_quality)"></div>
                </div>
            </template>
            <template x-if="loading || !data || data.avg_quality === null">
                <div style="width:96px;height:96px;border-radius:50%;background:#ece9e4;display:flex;align-items:center;justify-content:center;font-family:var(--font-display);font-style:italic;font-size:28px;color:var(--text-muted)">—</div>
            </template>
            <div class="dash-quality-gauge-label">средний Quality Score</div>
        </div>

        <div class="dash-quality-body">
            <div class="dash-quality-headline">
                <span class="dash-quality-count" x-text="loading ? '…' : (data ? data.need_attention : 0)"></span>
                <span class="dash-quality-headline-text">карточек тянут рейтинг вниз</span>
            </div>
            <div class="dash-quality-bar" x-show="!loading && data && data.total">
                <template x-for="seg in segments()" :key="seg.key">
                    <div class="dash-quality-bar-seg" :style="'flex:' + seg.count + ';background:' + seg.color" :title="seg.label + ': ' + seg.count"></div>
                </template>
            </div>
            <div class="dash-quality-legend" x-show="!loading && data && data.total">
                <span><span class="dash-dot" style="background:#dc2626"></span> Слабые: <span x-text="data ? data.distribution.poor : 0"></span></span>
                <span><span class="dash-dot" style="background:#d97706"></span> Средние: <span x-text="data ? data.distribution.average : 0"></span></span>
                <span><span class="dash-dot" style="background:#16a34a"></span> Хорошие: <span x-text="data ? (data.distribution.good + data.distribution.excellent) : 0"></span></span>
            </div>
            <a href="{{ url_for('card_quality_page') }}" class="dash-quality-cta">Разобрать &rarr;</a>
        </div>
    </div>
    {% endif %}
```
Добавить стили `.dash-quality-*` в `<style>` блока `extra_head` (после `.dash-dot {…}`, :101, перед `/* Navigation cards */` :103):
```css
    /* Quality widget */
    .dash-quality {
        display: flex;
        gap: 32px;
        align-items: center;
        background: var(--bg-card);
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 24px 28px;
        margin-bottom: 40px;
    }
    @media (max-width: 640px) { .dash-quality { flex-direction: column; align-items: flex-start; gap: 20px; } }
    .dash-quality-gauge { display: flex; flex-direction: column; align-items: center; gap: 8px; flex-shrink: 0; }
    .dash-quality-gauge-label {
        font-size: 11px; font-weight: 600; text-transform: uppercase;
        letter-spacing: 0.08em; color: var(--text-muted); text-align: center; max-width: 110px;
    }
    .dash-quality-body { flex: 1; min-width: 0; }
    .dash-quality-headline { display: flex; align-items: baseline; gap: 8px; margin-bottom: 14px; }
    .dash-quality-count {
        font-family: var(--font-display); font-style: italic; font-size: 32px;
        color: var(--accent); line-height: 1;
    }
    .dash-quality-headline-text { font-size: 14px; color: var(--text-secondary); }
    .dash-quality-bar {
        display: flex; height: 10px; border-radius: 5px; overflow: hidden;
        background: #ece9e4; margin-bottom: 10px;
    }
    .dash-quality-bar-seg { min-width: 0; }
    .dash-quality-legend { display: flex; flex-wrap: wrap; gap: 16px; font-size: 12px; color: var(--text-muted); margin-bottom: 14px; }
    .dash-quality-legend .dash-dot { display: inline-block; margin-right: 4px; }
    .dash-quality-cta { font-size: 13px; font-weight: 600; color: var(--accent); text-decoration: none; }
    .dash-quality-cta:hover { text-decoration: underline; }
```
Добавить контроллер `cardQualityWidget()` в существующий `<script>` (внутри `{% if … has_valid_api_key() %}` блока, перед закрывающим `</script>` :523, после функции `dashMetrics()` :522):
```javascript
function cardQualityWidget() {
    return {
        loading: true,
        data: null,
        load() {
            fetch('/api/card-quality/summary')
                .then(r => r.json())
                .then(json => { if (json.success) this.data = json.data; })
                .catch(e => console.warn('Card quality widget error:', e))
                .finally(() => { this.loading = false; });
        },
        gaugeColor() {
            const v = this.data ? this.data.avg_quality : 0;
            return v >= 70 ? '#16a34a' : v >= 50 ? '#d97706' : '#dc2626';
        },
        segments() {
            if (!this.data) return [];
            const d = this.data.distribution;
            return [
                { key: 'poor', count: d.poor, color: '#dc2626', label: 'Слабые' },
                { key: 'average', count: d.average, color: '#d97706', label: 'Средние' },
                { key: 'good', count: d.good + d.excellent, color: '#16a34a', label: 'Хорошие' },
            ].filter(s => s.count > 0);
        }
    };
}
```
- [ ] **Шаг 6: Визуальная проверка**
Открыть `/dashboard` под продавцом с настроенным API-ключом. Ожидание: под блоком «Показатели за неделю» появилась секция «Качество карточек» с кольцом среднего Quality Score (цвет по порогам 70/50), числом «N карточек тянут рейтинг вниз», цветной полоской распределения (красный/оранжевый/зелёный), легендой и ссылкой «Разобрать →» на `/card-quality`. Если данных нет — кольцо показывает «—», полоска скрыта.
- [ ] **Шаг 7: Коммит**
```bash
git add routes/card_quality.py templates/dashboard.html tests/test_card_quality_summary_route.py && git commit -m "feat(card-quality): виджет качества карточек на дашборде + GET /api/card-quality/summary"
```

---

### Задача 2.3: Колонка «Качество» + сортировка + фильтр «Только слабые» в списке товаров

**Файлы:**
- Изменить: `seller_platform.py` (функция `products_list`, :1300-1536): чтение `filter_quality_weak` (:1339), условие фильтра (:1433), `sort_column` dict (:1446), `render_template` (:1535)
- Изменить: `templates/products.html`: чекбокс фильтра в `<details>` (:259), `<th>Качество</th>` в thead (:421), `<td>` с бейджем (:536)
- Тест: `tests/test_products_quality_filter.py`

**Интерфейсы:**
- Потребляет: `Product.quality_score`, `Product.nm_rating` (models.py:216, :221); пороги фильтра (`quality_score < 50` OR `nm_rating < 6`) — те же, что в существующем `api_card_quality_list` (routes/card_quality.py:52).
- Производит: query-параметр `filter_quality_weak`, переменную шаблона `filter_quality_weak`, ключ сортировки `quality_score`, колонку «Качество» в списке товаров.

- [ ] **Шаг 1: Написать падающий тест (через тест-клиент Flask по vendor_code в HTML)**
```python
# -*- coding: utf-8 -*-
"""Тест фильтра «Только слабые карточки» в products_list."""

import os
import tempfile
import unittest


class TestProductsQualityFilter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._db_fd, cls._db_path = tempfile.mkstemp(suffix='.db')
        os.close(cls._db_fd)
        os.environ['DATABASE_URL'] = 'sqlite:///' + cls._db_path
        os.environ['DISABLE_SECURE_COOKIE'] = '1'
        import seller_platform  # noqa
        cls.app = seller_platform.app
        cls.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + cls._db_path
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.app.config['TESTING'] = True
        from models import db
        cls.db = db
        cls.ctx = cls.app.app_context()
        cls.ctx.push()
        db.create_all()
        cls._seed()

    @classmethod
    def _seed(cls):
        from models import User, Seller, Product
        user = User(username='seller1', email='seller1@example.com', password_hash='x')
        cls.db.session.add(user)
        cls.db.session.flush()
        seller = Seller(user_id=user.id, company_name='ООО Тест', wb_seller_id='123')
        seller.wb_api_key = 'test-api-key'
        cls.db.session.add(seller)
        cls.db.session.flush()
        cls.user_id = user.id
        cls.db.session.add_all([
            Product(seller_id=seller.id, nm_id=1, vendor_code='STRONG-1', is_active=True,
                    quality_score=90, nm_rating=9.0),
            Product(seller_id=seller.id, nm_id=2, vendor_code='WEAK-QUALITY', is_active=True,
                    quality_score=40, nm_rating=8.0),
            Product(seller_id=seller.id, nm_id=3, vendor_code='WEAK-RATING', is_active=True,
                    quality_score=80, nm_rating=5.0),
        ])
        cls.db.session.commit()

    @classmethod
    def tearDownClass(cls):
        cls.db.session.remove()
        cls.db.drop_all()
        cls.ctx.pop()
        os.remove(cls._db_path)

    def _client(self):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(self.user_id)
            sess['_fresh'] = True
        return client

    def test_weak_filter_returns_only_weak(self):
        client = self._client()
        resp = client.get('/products?quality_weak=1')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('WEAK-QUALITY', html)
        self.assertIn('WEAK-RATING', html)
        self.assertNotIn('STRONG-1', html)

    def test_no_filter_returns_all(self):
        client = self._client()
        resp = client.get('/products')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('WEAK-QUALITY', html)
        self.assertIn('STRONG-1', html)


if __name__ == '__main__':
    unittest.main()
```
- [ ] **Шаг 2: Запустить тест — убедиться что падает**
Запуск: `python -m unittest tests.test_products_quality_filter -v`
Ожидание: FAIL (`test_weak_filter_returns_only_weak` — без фильтра `STRONG-1` присутствует в HTML, т.к. параметр `quality_weak` пока игнорируется)
- [ ] **Шаг 3: Реализовать бэкенд в `seller_platform.py`**
(а) Добавить чтение параметра после блока `filter_rating_max` (:1339), вставив новую строку:
```python
        filter_quality_weak = request.args.get('quality_weak', '').strip() in ['1', 'true', 'True', 'on']
```
(б) Добавить условие фильтра сразу после блока «Фильтр по рейтингу карточки» (после :1433, перед `# Сортировка` :1435):
```python
        # Фильтр «Только слабые карточки» (Quality Score < 50 ИЛИ WB-рейтинг < 6)
        if filter_quality_weak:
            query = query.filter(
                (Product.quality_score < 50) | (Product.nm_rating < 6)
            )
```
(в) Добавить ключ сортировки в `sort_column` dict (:1446), заменив:
```python
            'nm_rating': Product.nm_rating,
        }.get(sort_by, Product.updated_at)
```
на:
```python
            'nm_rating': Product.nm_rating,
            'quality_score': Product.quality_score,
        }.get(sort_by, Product.updated_at)
```
(г) Передать переменную в `render_template` (:1533), заменив:
```python
            filter_rating_min=filter_rating_min,
            filter_rating_max=filter_rating_max,
```
на:
```python
            filter_rating_min=filter_rating_min,
            filter_rating_max=filter_rating_max,
            filter_quality_weak=filter_quality_weak,
```
- [ ] **Шаг 4: Реализовать фронтенд в `templates/products.html`**
(а) Добавить чекбокс «Только слабые карточки» в `<details>` расширенных фильтров. После закрывающего `</div>` блока «Фильтр по рейтингу карточки» (после :259, внутри grid) вставить:
```jinja
                    <!-- Фильтр «Только слабые карточки» -->
                    <div class="flex items-end">
                        <label class="inline-flex items-center gap-2 text-sm font-medium text-gray-700 cursor-pointer">
                            <input type="checkbox" name="quality_weak" value="1"
                                   {% if filter_quality_weak %}checked{% endif %}
                                   class="w-4 h-4 rounded border-gray-300 text-red-600 focus:ring-red-500">
                            Только слабые карточки
                        </label>
                    </div>
```
(б) Добавить `<th>Качество</th>` в thead перед «Обновлено» (:422), заменив:
```jinja
                        <th class="text-center">Рейтинг</th>
                        <th>Обновлено</th>
```
на:
```jinja
                        <th class="text-center">Рейтинг</th>
                        <th class="text-center">Качество</th>
                        <th>Обновлено</th>
```
(в) Добавить `<td>` с бейджем Quality Score между ячейкой рейтинга (закрывается на :536) и ячейкой «Обновлено» (:537). Вставить после `</td>` на :536:
```jinja
                        <td class="px-4 py-3 text-center">
                            {% if product.quality_score is not none %}
                            <a href="{{ url_for('card_quality_page') }}"
                               class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold
                                {% if product.quality_score >= 70 %}bg-green-100 text-green-700
                                {% elif product.quality_score >= 50 %}bg-yellow-100 text-yellow-700
                                {% else %}bg-red-100 text-red-700{% endif %}"
                               title="Quality Score — разобрать в кокпите качества">
                                {{ product.quality_score|round|int }}
                            </a>
                            {% else %}
                            <span class="text-xs text-gray-400">—</span>
                            {% endif %}
                        </td>
```
(г) Обновить условие подсветки кнопки «Сбросить» и `open` для `<details>`, чтобы учитывать новый фильтр. Заменить на :172:
```jinja
                {% if search or filter_brand or filter_category or filter_has_stock or filter_block_status or filter_rating_min or filter_rating_max or active_only or (disabled_only is defined and disabled_only) %}
```
на:
```jinja
                {% if search or filter_brand or filter_category or filter_has_stock or filter_block_status or filter_rating_min or filter_rating_max or filter_quality_weak or active_only or (disabled_only is defined and disabled_only) %}
```
и заменить на :178:
```jinja
            <details class="group" {% if filter_brand or filter_category or filter_has_stock or filter_block_status or filter_rating_min or filter_rating_max or sort_by != 'updated_at' %}open{% endif %}>
```
на:
```jinja
            <details class="group" {% if filter_brand or filter_category or filter_has_stock or filter_block_status or filter_rating_min or filter_rating_max or filter_quality_weak or sort_by != 'updated_at' %}open{% endif %}>
```
- [ ] **Шаг 5: Запустить тест — убедиться что проходит**
Запуск: `python -m unittest tests.test_products_quality_filter -v`
Ожидание: PASS
- [ ] **Шаг 6: Визуальная проверка**
Открыть `/products`: в таблице между «Рейтинг» и «Обновлено» появилась колонка «Качество» с цветным бейглом (≥70 зелёный, ≥50 жёлтый, <50 красный, None → «—») со ссылкой на `/card-quality`. В «Расширенных фильтрах» есть чекбокс «Только слабые карточки»; при его включении и применении список сужается до карточек с Quality Score < 50 или WB-рейтингом < 6.
- [ ] **Шаг 7: Коммит**
```bash
git add seller_platform.py templates/products.html tests/test_products_quality_filter.py && git commit -m "feat(card-quality): колонка Качество, сортировка и фильтр «Только слабые» в списке товаров"
```

---

## Фаза 3: Движок «Предложить→Подтвердить» (improve/proposal/apply + UI + bulk)

### Задача 3.1: `services/card_improver.py` — `apply_card_updates`

**Файлы:**
- Создать: `services/card_improver.py`
- Тест: `tests/test_card_improver_apply.py`

**Интерфейсы:**
- Потребляет: `services.supplier_enrichment._create_product_snapshot(product)->dict`; `services.card_quality_scorer.card_quality_detail(product)->dict`, `compute_card_quality`, `product_to_card_input`; Фаза 2 `services.card_quality_scorer.recompute_and_persist(product, capture_history=True)->dict` (ставит `quality_score`/`quality_breakdown_json`/`quality_checked_at`, добавляет `CardRatingHistory`, НЕ коммитит); `WildberriesAPIClient.update_card(nm_id, updates, merge_with_existing=True, seller_id=...)`; `models.CardEditHistory`, `models.db`.
- Производит: `ALLOWED_FIELDS: set[str]`; `apply_card_updates(product, updates, seller, wb_client, source='card-quality')->{'success','fields_applied','old_quality','new_quality','wb_sync','error'}`.

- [ ] **Шаг 1: Написать падающий тест**
```python
# tests/test_card_improver_apply.py
import json
import unittest
from unittest.mock import patch


class FakeWBClient:
    def __init__(self):
        self.calls = []

    def update_card(self, nm_id, updates, merge_with_existing=True, seller_id=None):
        self.calls.append({
            'nm_id': nm_id,
            'updates': updates,
            'merge_with_existing': merge_with_existing,
            'seller_id': seller_id,
        })
        return {'data': {}, 'error': False}


class FakeSeller:
    id = 7


class FakeProduct:
    def __init__(self):
        self.id = 101
        self.nm_id = 555111
        self.vendor_code = 'VC-1'
        self.title = 'Старый заголовок'
        self.brand = 'OldBrand'
        self.description = 'кратко'
        self.object_name = 'Платье'
        self.subject_id = 999
        self.price = 1990
        self.discount_price = 1490
        self.quantity = 5
        self.characteristics_json = json.dumps([{'id': 1, 'name': 'Цвет', 'value': 'синий'}],
                                               ensure_ascii=False)
        self.dimensions_json = json.dumps({'length': 10, 'width': 5, 'height': 2}, ensure_ascii=False)
        self.photos_json = json.dumps(['a.jpg', 'b.jpg'])
        self.sizes_json = None
        self.is_active = True
        self.quality_score = 40.0
        self.quality_breakdown_json = None
        self.quality_checked_at = None
        self.nm_rating = 7.0
        self.wb_feedback_rating = 4.2
        self.nm_rating_checked_at = None
        self.updated_at = None


class ApplyCardUpdatesTest(unittest.TestCase):
    def setUp(self):
        # Снимки/история/recompute не должны ходить в реальную БД
        self.history_records = []

        def fake_snapshot(product):
            return {'title': product.title, 'brand': product.brand,
                    'description': product.description}

        def fake_recompute(product, capture_history=True):
            from services.card_quality_scorer import compute_card_quality, product_to_card_input
            cq = compute_card_quality(product_to_card_input(product))
            product.quality_score = cq['score']
            product.quality_breakdown_json = json.dumps(cq['dimensions'], ensure_ascii=False)
            return cq

        class FakeHistory:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class FakeSession:
            def __init__(self, outer):
                self.outer = outer
            def add(self, obj):
                self.outer.history_records.append(obj)
            def commit(self):
                self.outer.committed = True

        self.committed = False
        self.p1 = patch('services.card_improver._create_product_snapshot', side_effect=fake_snapshot)
        self.p2 = patch('services.card_improver.recompute_and_persist', side_effect=fake_recompute)
        self.p3 = patch('services.card_improver.CardEditHistory', FakeHistory)
        self.p4 = patch('services.card_improver.db')
        self.p1.start()
        self.p2.start()
        self.p3.start()
        mock_db = self.p4.start()
        mock_db.session = FakeSession(self)

    def tearDown(self):
        for p in (self.p1, self.p2, self.p3, self.p4):
            p.stop()

    def test_applies_text_fields_and_records_history(self):
        from services.card_improver import apply_card_updates
        product = FakeProduct()
        wb = FakeWBClient()
        seller = FakeSeller()

        new_desc = 'д' * 450
        res = apply_card_updates(
            product,
            {'title': 'Новый длинный заголовок про платье', 'description': new_desc},
            seller, wb, source='card-quality',
        )

        self.assertTrue(res['success'])
        self.assertIn('title', res['fields_applied'])
        self.assertIn('description', res['fields_applied'])
        self.assertTrue(res['wb_sync'])
        self.assertEqual(res['old_quality'], 40.0)
        self.assertIsNotNone(res['new_quality'])
        self.assertGreater(res['new_quality'], res['old_quality'])
        # WB-клиент вызван с merge и seller_id
        self.assertEqual(len(wb.calls), 1)
        self.assertEqual(wb.calls[0]['nm_id'], 555111)
        self.assertTrue(wb.calls[0]['merge_with_existing'])
        self.assertEqual(wb.calls[0]['seller_id'], 7)
        # Локальный продукт обновлён
        self.assertEqual(product.title, 'Новый длинный заголовок про платье')
        self.assertEqual(product.description, new_desc)
        # История создана и закоммичена
        self.assertEqual(len(self.history_records), 1)
        h = self.history_records[0]
        self.assertEqual(h.action, 'update')
        self.assertEqual(sorted(h.changed_fields), ['description', 'title'])
        self.assertTrue(h.wb_synced)
        self.assertEqual(h.wb_sync_status, 'success')
        self.assertTrue(self.committed)

    def test_ignores_unknown_fields(self):
        from services.card_improver import apply_card_updates
        product = FakeProduct()
        wb = FakeWBClient()
        res = apply_card_updates(product, {'foobar': 'x', 'brand': 'NewBrand'},
                                 FakeSeller(), wb, source='card-quality')
        self.assertEqual(res['fields_applied'], ['brand'])
        self.assertEqual(product.brand, 'NewBrand')

    def test_no_known_fields_skips_wb_call(self):
        from services.card_improver import apply_card_updates
        product = FakeProduct()
        wb = FakeWBClient()
        res = apply_card_updates(product, {'foobar': 'x'}, FakeSeller(), wb)
        self.assertFalse(res['success'])
        self.assertEqual(res['fields_applied'], [])
        self.assertFalse(res['wb_sync'])
        self.assertEqual(len(wb.calls), 0)


if __name__ == '__main__':
    unittest.main()
```
- [ ] **Шаг 2: Запустить тест — убедиться что падает**
Запуск: `python -m unittest tests.test_card_improver_apply -v`
Ожидание: FAIL (`ModuleNotFoundError: No module named 'services.card_improver'`)
- [ ] **Шаг 3: Реализовать минимальный код**
```python
# services/card_improver.py
# -*- coding: utf-8 -*-
"""
Движок «Предложить→Подтвердить» для опубликованных карточек (Product).

apply_card_updates применяет ЯВНЫЕ значения полей через WB Content API
(update_card с merge_with_existing) + локальное обновление Product,
пишет CardEditHistory (snapshot до/после) и пересчитывает Quality Score
через recompute_and_persist. Сетевые вызовы инкапсулированы в wb_client —
функция тестируется фейковым клиентом без сети.
"""
import json
import logging
from datetime import datetime
from typing import Dict, Any, List

from models import db, CardEditHistory
from services.supplier_enrichment import _create_product_snapshot
from services.card_quality_scorer import recompute_and_persist

logger = logging.getLogger('card_improver')

# Поля, которые движок имеет право применять к карточке.
ALLOWED_FIELDS = {'title', 'brand', 'description', 'characteristics', 'dimensions', 'subject_id', 'photos'}


def _build_wb_updates(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Из явных значений (уже отфильтрованных по ALLOWED_FIELDS) строит payload для update_card.
    photos и subject_id применяются только локально/через отдельную логику — в wb_updates не идут."""
    wb_updates = {}
    if updates.get('title'):
        wb_updates['title'] = str(updates['title'])[:60]
    if updates.get('brand'):
        wb_updates['brand'] = str(updates['brand'])
    if updates.get('description'):
        wb_updates['description'] = str(updates['description'])[:5000]
    if updates.get('characteristics'):
        chars = updates['characteristics']
        if isinstance(chars, list) and chars:
            wb_updates['characteristics'] = chars
    if updates.get('dimensions'):
        dims = updates['dimensions']
        if isinstance(dims, dict) and dims:
            wb_updates['dimensions'] = dims
    return wb_updates


def apply_card_updates(product, updates: Dict[str, Any], seller, wb_client,
                       source: str = 'card-quality') -> Dict[str, Any]:
    """Применяет явные значения полей к опубликованной карточке Product.

    Returns:
        {'success': bool, 'fields_applied': list, 'old_quality': float|None,
         'new_quality': float|None, 'wb_sync': bool, 'error': str|None}
    """
    old_quality = getattr(product, 'quality_score', None)

    # Фильтруем по белому списку и по непустым значениям
    clean = {k: v for k, v in (updates or {}).items()
             if k in ALLOWED_FIELDS and v not in (None, '', [], {})}

    if not clean:
        return {'success': False, 'fields_applied': [], 'old_quality': old_quality,
                'new_quality': old_quality, 'wb_sync': False,
                'error': 'Нет допустимых полей для применения'}

    snapshot_before = _create_product_snapshot(product)
    wb_updates = _build_wb_updates(clean)
    fields_applied: List[str] = []
    wb_sync_success = False
    wb_error = None

    if wb_updates:
        try:
            wb_client.update_card(
                product.nm_id,
                wb_updates,
                merge_with_existing=True,
                seller_id=seller.id,
            )
            wb_sync_success = True
            logger.info(f"[Improve/{source}] WB updated nmID={product.nm_id}: {list(wb_updates.keys())}")

            if 'title' in wb_updates:
                product.title = wb_updates['title']
                fields_applied.append('title')
            if 'brand' in wb_updates:
                product.brand = wb_updates['brand']
                fields_applied.append('brand')
            if 'description' in wb_updates:
                product.description = wb_updates['description']
                fields_applied.append('description')
            if 'characteristics' in wb_updates:
                product.characteristics_json = json.dumps(wb_updates['characteristics'], ensure_ascii=False)
                fields_applied.append('characteristics')
            if 'dimensions' in wb_updates:
                product.dimensions_json = json.dumps(wb_updates['dimensions'], ensure_ascii=False)
                fields_applied.append('dimensions')
        except Exception as e:
            wb_error = str(e)
            logger.error(f"[Improve/{source}] WB API error nmID={product.nm_id}: {e}")

    # subject_id применяем локально (категория не уходит через update_card в этом движке)
    if 'subject_id' in clean and clean['subject_id']:
        product.subject_id = clean['subject_id']
        fields_applied.append('subject_id')

    if hasattr(product, 'updated_at'):
        product.updated_at = datetime.utcnow()

    # Пересчёт Quality Score (Фаза 2). При ошибке WB историю всё равно пишем.
    new_quality = old_quality
    if fields_applied:
        cq = recompute_and_persist(product, capture_history=True)
        new_quality = cq['score']

    snapshot_after = _create_product_snapshot(product)
    history = CardEditHistory(
        product_id=product.id,
        seller_id=seller.id,
        bulk_edit_id=None,
        action='update',
        changed_fields=fields_applied,
        snapshot_before=snapshot_before,
        snapshot_after=snapshot_after,
        wb_synced=wb_sync_success,
        wb_sync_status='success' if wb_sync_success else ('failed' if wb_error else 'pending'),
        wb_error_message=wb_error,
        user_comment=f'Улучшение карточки ({source})',
    )
    db.session.add(history)
    db.session.commit()

    return {
        'success': bool(fields_applied) and wb_error is None,
        'fields_applied': fields_applied,
        'old_quality': old_quality,
        'new_quality': new_quality,
        'wb_sync': wb_sync_success,
        'error': wb_error,
    }
```
- [ ] **Шаг 4: Запустить тест — убедиться что проходит**
Запуск: `python -m unittest tests.test_card_improver_apply -v`
Ожидание: PASS
- [ ] **Шаг 5: Коммит**
```bash
git add services/card_improver.py tests/test_card_improver_apply.py && git commit -m "feat(card-quality): apply_card_updates — явное применение полей через WB+история+пересчёт Quality"
```

---

### Задача 3.2: `services/card_improver.py` — `collect_weak_dimensions` и `build_proposal_from_tasks`

**Файлы:**
- Изменить: `services/card_improver.py` (добавить две функции в конец файла)
- Тест: `tests/test_card_improver_proposal.py`

**Интерфейсы:**
- Потребляет: `detail` из `card_quality_detail(product)` (ключ `dimensions: {<name>:{score,status,weight,hint}}`); результаты задач агентов (`AgentTask.get_result()`), для photo-optimizer — `{'recommended_order': [...], 'recommendations': [...]}`.
- Производит: `collect_weak_dimensions(detail)->list[str]`; `build_proposal_from_tasks(product, task_results)->{'<field>': {'current','proposed','dimension','source'}}`.

- [ ] **Шаг 1: Написать падающий тест**
```python
# tests/test_card_improver_proposal.py
import json
import unittest


class FakeProduct:
    def __init__(self):
        self.id = 5
        self.title = 'Платье летнее'
        self.photos_json = json.dumps(['a.jpg', 'b.jpg', 'c.jpg'])


class CollectWeakDimensionsTest(unittest.TestCase):
    def test_returns_warning_and_error_sorted_by_impact(self):
        from services.card_improver import collect_weak_dimensions
        detail = {'dimensions': {
            'photos':          {'score': 0,  'status': 'error',   'weight': 20, 'hint': 'нет фото'},
            'characteristics': {'score': 50, 'status': 'warning', 'weight': 25, 'hint': 'мало'},
            'title':           {'score': 100,'status': 'ok',      'weight': 10, 'hint': ''},
            'description':     {'score': 0,  'status': 'error',   'weight': 15, 'hint': 'нет'},
        }}
        weak = collect_weak_dimensions(detail)
        # impact = weight*(100-score): photos=2000, chars=1250, description=1500
        self.assertEqual(weak, ['photos', 'description', 'characteristics'])
        self.assertNotIn('title', weak)

    def test_empty_when_all_ok(self):
        from services.card_improver import collect_weak_dimensions
        detail = {'dimensions': {'title': {'score': 100, 'status': 'ok', 'weight': 10, 'hint': ''}}}
        self.assertEqual(collect_weak_dimensions(detail), [])


class BuildProposalTest(unittest.TestCase):
    def test_photo_reorder_proposal(self):
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        task_results = [{
            'agent': 'photo-optimizer',
            'result': {'recommended_order': [2, 0, 1], 'recommendations': ['Главное фото — на белом фоне']},
        }]
        proposal = build_proposal_from_tasks(product, task_results)
        self.assertIn('photos', proposal)
        self.assertEqual(proposal['photos']['proposed'], ['c.jpg', 'a.jpg', 'b.jpg'])
        self.assertEqual(proposal['photos']['current'], ['a.jpg', 'b.jpg', 'c.jpg'])
        self.assertEqual(proposal['photos']['dimension'], 'photos')
        self.assertEqual(proposal['photos']['source'], 'photo-optimizer')

    def test_photo_reorder_skipped_when_order_equals_current(self):
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        task_results = [{'agent': 'photo-optimizer',
                         'result': {'recommended_order': [0, 1, 2]}}]
        proposal = build_proposal_from_tasks(product, task_results)
        self.assertNotIn('photos', proposal)

    def test_photo_reorder_ignores_out_of_range_indices(self):
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        task_results = [{'agent': 'photo-optimizer',
                         'result': {'recommended_order': [2, 0, 1, 9]}}]
        proposal = build_proposal_from_tasks(product, task_results)
        # 9 вне диапазона → отбрасываем, остаётся валидная перестановка
        self.assertEqual(proposal['photos']['proposed'], ['c.jpg', 'a.jpg', 'b.jpg'])

    def test_ignores_non_writing_diagnostic_agents(self):
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        task_results = [{'agent': 'card-doctor',
                         'result': {'recommendations': ['Добавьте описание']}}]
        proposal = build_proposal_from_tasks(product, task_results)
        self.assertEqual(proposal, {})


if __name__ == '__main__':
    unittest.main()
```
- [ ] **Шаг 2: Запустить тест — убедиться что падает**
Запуск: `python -m unittest tests.test_card_improver_proposal -v`
Ожидание: FAIL (`ImportError: cannot import name 'collect_weak_dimensions'`)
- [ ] **Шаг 3: Реализовать минимальный код** (добавить в конец `services/card_improver.py`)
```python
def collect_weak_dimensions(detail: Dict[str, Any]) -> List[str]:
    """Имена измерений со статусом warning/error, отсортированные по impact = weight*(100-score) убыванию."""
    dims = (detail or {}).get('dimensions') or {}
    weak = []
    for name, d in dims.items():
        if d.get('status') in ('warning', 'error'):
            impact = d.get('weight', 0) * (100 - d.get('score', 0))
            weak.append((impact, name))
    weak.sort(key=lambda t: (-t[0], t[1]))
    return [name for _, name in weak]


def build_proposal_from_tasks(product, task_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Маппит результаты завершённых задач агентов в форму proposal.

    task_results: [{'agent': '<name>', 'result': <dict из AgentTask.get_result()>}]
    Returns: {'<field>': {'current','proposed','dimension','source'}}

    photo-optimizer/recommended_order → предложение «переупорядочить фото».
    Генеративные значения seo/characteristics/brand/category добавляются
    в Задаче 3.7 (propose-mode), здесь — расширяемая ветка по agent name.
    """
    proposal: Dict[str, Any] = {}

    try:
        current_photos = json.loads(getattr(product, 'photos_json', None) or '[]')
    except (json.JSONDecodeError, TypeError):
        current_photos = []
    if not isinstance(current_photos, list):
        current_photos = []

    for entry in task_results or []:
        agent = entry.get('agent')
        result = entry.get('result') or {}

        if agent == 'photo-optimizer':
            order = result.get('recommended_order')
            if isinstance(order, list) and current_photos:
                # Берём только валидные индексы в диапазоне, без дублей, в указанном порядке
                seen = set()
                valid = []
                for idx in order:
                    if isinstance(idx, int) and 0 <= idx < len(current_photos) and idx not in seen:
                        seen.add(idx)
                        valid.append(idx)
                # Достраиваем хвостом пропущенные индексы, чтобы не потерять фото
                for idx in range(len(current_photos)):
                    if idx not in seen:
                        valid.append(idx)
                reordered = [current_photos[i] for i in valid]
                if reordered != current_photos:
                    proposal['photos'] = {
                        'current': current_photos,
                        'proposed': reordered,
                        'dimension': 'photos',
                        'source': 'photo-optimizer',
                    }
        # card-doctor — диагностический (рекомендации-текст), не формирует proposal-поля.
        # Генеративные агенты (Задача 3.7) подключаются здесь по agent name.

    return proposal
```
- [ ] **Шаг 4: Запустить тест — убедиться что проходит**
Запуск: `python -m unittest tests.test_card_improver_proposal -v`
Ожидание: PASS
- [ ] **Шаг 5: Коммит**
```bash
git add services/card_improver.py tests/test_card_improver_proposal.py && git commit -m "feat(card-quality): collect_weak_dimensions + build_proposal_from_tasks (порядок фото)"
```

---

### Задача 3.3: `routes/card_quality.py` — `POST /api/card-quality/<id>/apply`

**Файлы:**
- Изменить: `routes/card_quality.py` (добавить роут после `api_card_quality_refresh`, импорты вверху)
- Тест: `tests/test_card_quality_apply_route.py`

**Интерфейсы:**
- Потребляет: `services.card_improver.ALLOWED_FIELDS`, `apply_card_updates`; `WildberriesAPIClient`; `Product`, `current_user.seller`.
- Производит: HTTP `POST /api/card-quality/<id>/apply` тело `{updates:{field:value}}` → `{success, fields_applied, old_quality, new_quality, wb_sync}`.

- [ ] **Шаг 1: Написать падающий тест**
```python
# tests/test_card_quality_apply_route.py
import json
import unittest
from unittest.mock import patch, MagicMock


class ApplyRouteTest(unittest.TestCase):
    def setUp(self):
        import app as app_module
        self.app = app_module.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

    def _login_ctx(self, has_key=True, product=None):
        seller = MagicMock()
        seller.id = 7
        seller.has_valid_api_key.return_value = has_key
        user = MagicMock()
        user.is_authenticated = True
        user.seller = seller
        return user, seller

    def test_apply_success(self):
        user, seller = self._login_ctx()
        product = MagicMock()
        product.id = 101
        product.nm_id = 555

        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.WildberriesAPIClient') as MockWB, \
             patch('routes.card_quality.apply_card_updates') as mock_apply:
            MockProduct.query.filter_by.return_value.first.return_value = product
            mock_apply.return_value = {
                'success': True, 'fields_applied': ['title'],
                'old_quality': 40.0, 'new_quality': 62.0, 'wb_sync': True, 'error': None,
            }
            resp = self.client.post('/api/card-quality/101/apply',
                                    json={'updates': {'title': 'Новый заголовок'}})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data['success'])
            self.assertEqual(data['fields_applied'], ['title'])
            self.assertEqual(data['new_quality'], 62.0)
            # apply_card_updates получил только whitelisted поля
            called_updates = mock_apply.call_args.args[1] if mock_apply.call_args.args else mock_apply.call_args.kwargs['updates']
            self.assertIn('title', called_updates)

    def test_apply_rejects_when_no_api_key(self):
        user, seller = self._login_ctx(has_key=False)
        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user):
            resp = self.client.post('/api/card-quality/101/apply',
                                    json={'updates': {'title': 'x'}})
            self.assertEqual(resp.status_code, 403)

    def test_apply_filters_unknown_fields(self):
        user, seller = self._login_ctx()
        product = MagicMock(); product.id = 101; product.nm_id = 555
        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.WildberriesAPIClient'), \
             patch('routes.card_quality.apply_card_updates') as mock_apply:
            MockProduct.query.filter_by.return_value.first.return_value = product
            mock_apply.return_value = {'success': True, 'fields_applied': ['brand'],
                                       'old_quality': 1, 'new_quality': 2, 'wb_sync': True, 'error': None}
            self.client.post('/api/card-quality/101/apply',
                             json={'updates': {'brand': 'Nike', 'hacker': 'drop table'}})
            called_updates = mock_apply.call_args.args[1] if mock_apply.call_args.args else mock_apply.call_args.kwargs['updates']
            self.assertIn('brand', called_updates)
            self.assertNotIn('hacker', called_updates)


if __name__ == '__main__':
    unittest.main()
```
- [ ] **Шаг 2: Запустить тест — убедиться что падает**
Запуск: `python -m unittest tests.test_card_quality_apply_route -v`
Ожидание: FAIL (404 на `/api/card-quality/101/apply` — роут не зарегистрирован)
- [ ] **Шаг 3: Реализовать минимальный код**
Добавить импорты вверху `routes/card_quality.py` (после строки `from services import agent_service`):
```python
from services.wb_api_client import WildberriesAPIClient
from services.card_improver import ALLOWED_FIELDS, apply_card_updates
```
Добавить роут внутри `register_card_quality_routes` (после `api_card_quality_refresh`):
```python
    @app.route('/api/card-quality/<int:product_id>/apply', methods=['POST'])
    @login_required
    def api_card_quality_apply(product_id):
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        product = Product.query.filter_by(id=product_id, seller_id=current_user.seller.id).first()
        if not product:
            return jsonify({'error': 'Карточка не найдена'}), 404

        body = request.get_json(silent=True) or {}
        raw_updates = body.get('updates') or {}
        updates = {k: v for k, v in raw_updates.items() if k in ALLOWED_FIELDS}
        if not updates:
            return jsonify({'error': 'Нет допустимых полей для применения'}), 400

        try:
            wb_client = WildberriesAPIClient(current_user.seller.wb_api_key)
            res = apply_card_updates(product, updates, current_user.seller, wb_client,
                                     source='card-quality')
            status = 200 if res.get('success') else 422
            return jsonify({
                'success': res.get('success', False),
                'fields_applied': res.get('fields_applied', []),
                'old_quality': res.get('old_quality'),
                'new_quality': res.get('new_quality'),
                'wb_sync': res.get('wb_sync', False),
                'error': res.get('error'),
            }), status
        except Exception as e:
            logger.exception('Ошибка в api_card_quality_apply: %s', e)
            return jsonify({'error': 'Внутренняя ошибка'}), 500
```
Примечание: если конструктор `WildberriesAPIClient` отличается, прочитать `grep -n "def __init__" services/wb_api_client.py` и подставить точную сигнатуру (передать ключ продавца тем же способом, что и существующие роуты enrich/sync).
- [ ] **Шаг 4: Запустить тест — убедиться что проходит**
Запуск: `python -m unittest tests.test_card_quality_apply_route -v`
Ожидание: PASS
- [ ] **Шаг 5: Коммит**
```bash
git add routes/card_quality.py tests/test_card_quality_apply_route.py && git commit -m "feat(card-quality): POST /apply — валидация ALLOWED_FIELDS + apply_card_updates"
```

---

### Задача 3.4: `routes/card_quality.py` — `POST /improve` и `POST /proposal`

**Файлы:**
- Изменить: `routes/card_quality.py` (два роута + импорты)
- Тест: `tests/test_card_quality_improve_route.py`

**Интерфейсы:**
- Потребляет: `card_quality_detail`; `services.card_improver.collect_weak_dimensions`, `build_proposal_from_tasks`; `services.supplier_enrichment.get_enrichment_service().build_preview`, `find_supplier_data`; `agent_service.get_agent_by_name`, `create_task`; `AgentTask.get_result()`.
- Производит: `POST /improve` → `{weak_dims, supplier_diff|None, task_ids:{<agent>:id}}`; `POST /proposal` тело `{task_ids:{<agent>:id}}` → `{proposal, supplier_diff?}`.

- [ ] **Шаг 1: Написать падающий тест**
```python
# tests/test_card_quality_improve_route.py
import unittest
from unittest.mock import patch, MagicMock


def _user(has_key=True):
    seller = MagicMock(); seller.id = 7
    seller.has_valid_api_key.return_value = has_key
    u = MagicMock(); u.is_authenticated = True; u.seller = seller
    return u


class ImproveProposalRouteTest(unittest.TestCase):
    def setUp(self):
        import app as app_module
        self.app = app_module.app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

    def test_improve_agents_offline_returns_supplier_diff(self):
        user = _user()
        product = MagicMock(); product.id = 101; product.nm_id = 555
        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.card_quality_detail') as mock_detail, \
             patch('routes.card_quality.collect_weak_dimensions', return_value=['photos', 'description']), \
             patch('routes.card_quality.get_enrichment_service') as mock_es, \
             patch('routes.card_quality.agent_service') as mock_as:
            MockProduct.query.filter_by.return_value.first.return_value = product
            mock_detail.return_value = {'dimensions': {}}
            svc = MagicMock()
            svc.find_supplier_data.return_value = MagicMock()
            svc.build_preview.return_value = {'title': {'has_change': True}}
            mock_es.return_value = svc
            # агенты офлайн
            mock_as.get_agent_by_name.return_value = MagicMock(status='offline')

            resp = self.client.post('/api/card-quality/101/improve', json={})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data['weak_dims'], ['photos', 'description'])
            self.assertIsNotNone(data['supplier_diff'])
            self.assertEqual(data['task_ids'], {})

    def test_improve_enqueues_online_agents(self):
        user = _user()
        product = MagicMock(); product.id = 101; product.nm_id = 555
        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.card_quality_detail', return_value={'dimensions': {}}), \
             patch('routes.card_quality.collect_weak_dimensions', return_value=[]), \
             patch('routes.card_quality.get_enrichment_service') as mock_es, \
             patch('routes.card_quality.agent_service') as mock_as:
            MockProduct.query.filter_by.return_value.first.return_value = product
            svc = MagicMock(); svc.find_supplier_data.return_value = None
            mock_es.return_value = svc
            online = MagicMock(); online.status = 'online'; online.id = 'aid'
            mock_as.get_agent_by_name.return_value = online
            task = MagicMock(); task.id = 'tid-1'
            mock_as.create_task.return_value = task

            resp = self.client.post('/api/card-quality/101/improve', json={})
            data = resp.get_json()
            self.assertIn('photo-optimizer', data['task_ids'])
            self.assertIn('card-doctor', data['task_ids'])
            self.assertIsNone(data['supplier_diff'])

    def test_proposal_maps_completed_tasks(self):
        user = _user()
        product = MagicMock(); product.id = 101; product.nm_id = 555
        completed = MagicMock(); completed.status = 'completed'
        completed.get_result.return_value = {'recommended_order': [2, 0, 1]}
        with patch('routes.card_quality.current_user', user), \
             patch('flask_login.utils._get_user', return_value=user), \
             patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.AgentTask') as MockTask, \
             patch('routes.card_quality.build_proposal_from_tasks') as mock_build, \
             patch('routes.card_quality.get_enrichment_service') as mock_es:
            MockProduct.query.filter_by.return_value.first.return_value = product
            MockTask.query.filter_by.return_value.first.return_value = completed
            mock_es.return_value.find_supplier_data.return_value = None
            mock_build.return_value = {'photos': {'proposed': []}}
            resp = self.client.post('/api/card-quality/101/proposal',
                                    json={'task_ids': {'photo-optimizer': 'tid-1'}})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertIn('photos', data['proposal'])
            # build_proposal_from_tasks получил список {agent, result}
            passed = mock_build.call_args.args[1]
            self.assertEqual(passed[0]['agent'], 'photo-optimizer')
            self.assertEqual(passed[0]['result'], {'recommended_order': [2, 0, 1]})


if __name__ == '__main__':
    unittest.main()
```
- [ ] **Шаг 2: Запустить тест — убедиться что падает**
Запуск: `python -m unittest tests.test_card_quality_improve_route -v`
Ожидание: FAIL (404 — роуты не зарегистрированы)
- [ ] **Шаг 3: Реализовать минимальный код**
Расширить импорты вверху `routes/card_quality.py`:
```python
from models import db, Product, CardRatingHistory, AgentTask
from services.card_quality_scorer import card_quality_detail
from services.card_improver import (ALLOWED_FIELDS, apply_card_updates,
                                     collect_weak_dimensions, build_proposal_from_tasks)
from services.supplier_enrichment import get_enrichment_service
```
(объединить с уже добавленными в 3.3 импортами `card_improver`; убрать дубль строки `from models import db, Product, CardRatingHistory`).
Добавить два роута внутри `register_card_quality_routes`:
```python
    @app.route('/api/card-quality/<int:product_id>/improve', methods=['POST'])
    @login_required
    def api_card_quality_improve(product_id):
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        product = Product.query.filter_by(id=product_id, seller_id=current_user.seller.id).first()
        if not product:
            return jsonify({'error': 'Карточка не найдена'}), 404
        try:
            detail = card_quality_detail(product)
            weak_dims = collect_weak_dimensions(detail)

            # (a) данные поставщика → готовый дифф
            supplier_diff = None
            es = get_enrichment_service()
            imp = es.find_supplier_data(product, current_user.seller.id)
            if imp:
                supplier_diff = es.build_preview(product, imp)

            # (b)/(c) диагностические агенты по product_id (если online)
            task_ids = {}
            for agent_name, task_type in (('photo-optimizer', 'optimize_single'),
                                          ('card-doctor', 'diagnose_single')):
                agent = agent_service.get_agent_by_name(agent_name)
                if not agent or getattr(agent, 'status', None) != 'online':
                    continue
                task = agent_service.create_task(
                    agent_id=agent.id,
                    seller_id=current_user.seller.id,
                    task_type=task_type,
                    title=f'Улучшение карточки {product.nm_id}',
                    input_data={'product_id': product.id},
                )
                task_ids[agent_name] = task.id

            return jsonify({'success': True, 'weak_dims': weak_dims,
                            'supplier_diff': supplier_diff, 'task_ids': task_ids})
        except Exception as e:
            logger.exception('Ошибка в api_card_quality_improve: %s', e)
            return jsonify({'error': 'Внутренняя ошибка'}), 500

    @app.route('/api/card-quality/<int:product_id>/proposal', methods=['POST'])
    @login_required
    def api_card_quality_proposal(product_id):
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        product = Product.query.filter_by(id=product_id, seller_id=current_user.seller.id).first()
        if not product:
            return jsonify({'error': 'Карточка не найдена'}), 404
        try:
            body = request.get_json(silent=True) or {}
            task_ids = body.get('task_ids') or {}

            task_results = []
            for agent_name, task_id in task_ids.items():
                task = AgentTask.query.filter_by(id=task_id, seller_id=current_user.seller.id).first()
                if task and task.status == 'completed':
                    task_results.append({'agent': agent_name, 'result': task.get_result()})

            proposal = build_proposal_from_tasks(product, task_results)

            supplier_diff = None
            es = get_enrichment_service()
            imp = es.find_supplier_data(product, current_user.seller.id)
            if imp:
                supplier_diff = es.build_preview(product, imp)

            return jsonify({'success': True, 'proposal': proposal, 'supplier_diff': supplier_diff})
        except Exception as e:
            logger.exception('Ошибка в api_card_quality_proposal: %s', e)
            return jsonify({'error': 'Внутренняя ошибка'}), 500
```
- [ ] **Шаг 4: Запустить тест — убедиться что проходит**
Запуск: `python -m unittest tests.test_card_quality_improve_route -v`
Ожидание: PASS
- [ ] **Шаг 5: Коммит**
```bash
git add routes/card_quality.py tests/test_card_quality_improve_route.py && git commit -m "feat(card-quality): POST /improve (weak_dims+supplier_diff+enqueue) и POST /proposal (маппинг задач)"
```

---

### Задача 3.5: UI «Улучшить карточку» в `templates/card_quality.html` (slideover)

**Файлы:**
- Изменить: `templates/card_quality.html` (кнопки + блок диффа/предложений в slideover; методы `cardQualityPage()`)

**Интерфейсы:**
- Потребляет роуты: `POST /api/card-quality/<id>/improve`, `/proposal`, `/apply`; поллинг `/agents/api/tasks/<id>/status`. Тосты `$store.toasts.success/error/info`. CSRF добавляется автоматически (base.html) — вручную НЕ добавлять заголовок (в существующих refresh/aiAnalyze он есть вручную, для новых fetch не дублируем).

- [ ] **Шаг 1: Добавить кнопку «⚡ Улучшить карточку» и блок результатов в slideover**
В блоке кнопок slideover (после строки 74, рядом с «🤖 Глубокий AI-анализ»):
```html
          <button class="sh-btn sh-btn--accent sh-btn--sm" @click="startImprove(detail.product_id)" :disabled="improveRunning">⚡ Улучшить карточку</button>
```
Добавить после блока `aiStatus` (после строки 76), внутри `<template x-if="detail">`:
```html
        <div x-show="improveRunning" class="text-gray-500 text-xs" style="margin-top:8px" x-text="improveStatus"></div>

        {# ── Дифф от поставщика (было→стало) ── #}
        <template x-if="supplierDiff">
          <div style="margin-top:20px">
            <div style="font-weight:600;margin-bottom:8px">Данные поставщика</div>
            <template x-for="f in supplierFields" :key="f.key">
              <div x-show="f.hasChange" class="sh-card" style="padding:12px;margin-bottom:8px">
                <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
                  <input type="checkbox" x-model="selected[f.key]">
                  <span style="font-weight:500" x-text="f.label"></span>
                </label>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:8px;font-size:13px">
                  <div><div class="text-gray-400 text-xs">Сейчас</div><div x-text="f.current || '—'"></div></div>
                  <div style="background:var(--accent-light);border-radius:6px;padding:6px">
                    <div class="text-gray-500 text-xs">Предложено</div><div x-text="f.proposed || '—'"></div></div>
                </div>
              </div>
            </template>
          </div>
        </template>

        {# ── Предложения от агентов (порядок фото и т.п.) ── #}
        <template x-if="Object.keys(proposal).length">
          <div style="margin-top:16px">
            <div style="font-weight:600;margin-bottom:8px">Предложения по улучшению</div>
            <template x-for="(p,field) in proposal" :key="field">
              <div class="sh-card" style="padding:12px;margin-bottom:8px">
                <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
                  <input type="checkbox" x-model="selected[field]">
                  <span style="font-weight:500" x-text="proposalLabel(field)"></span>
                  <span class="text-gray-400 text-xs" x-text="'(' + (p.source||'') + ')'"></span>
                </label>
                <div class="text-gray-500 text-xs" style="margin-top:4px" x-text="proposalHint(field, p)"></div>
              </div>
            </template>
          </div>
        </template>

        <div x-show="supplierDiff || Object.keys(proposal).length" style="margin-top:16px">
          <button class="sh-btn sh-btn--primary sh-btn--sm" @click="applySelected(detail.product_id)"
                  :disabled="applyRunning || !anySelected" x-text="applyRunning ? 'Применяю…' : 'Применить выбранное'"></button>
        </div>
```
- [ ] **Шаг 2: Добавить методы и состояние в `cardQualityPage()`**
Расширить state-объект (строка 85-86):
```javascript
    items: [], summary: {}, detail: null, drawer: false,
    refreshing: false, aiRunning: false, aiStatus: '', _poll: null, _trendChart: null,
    improveRunning: false, improveStatus: '', applyRunning: false,
    supplierDiff: null, proposal: {}, selected: {}, _improvePoll: null,
```
В `openDetail` сбросить состояние improve (после `this.detail = null;`):
```javascript
      this.supplierDiff = null; this.proposal = {}; this.selected = {};
      this.improveRunning = false; this.improveStatus = '';
```
Добавить методы (перед `wbBadge`):
```javascript
    get supplierFields() {
      const sd = this.supplierDiff; if (!sd) return [];
      const fmt = (v) => Array.isArray(v) ? (v.length + ' значений') : (typeof v === 'object' && v ? JSON.stringify(v).slice(0,80) : v);
      const out = [];
      const map = [
        ['title','Заголовок', sd.title && sd.title.current, sd.title && sd.title.supplier, sd.title && sd.title.has_change],
        ['brand','Бренд', sd.brand && sd.brand.current, sd.brand && sd.brand.supplier, sd.brand && sd.brand.has_change],
        ['description','Описание', sd.description && sd.description.current, sd.description && sd.description.supplier, sd.description && sd.description.has_change],
        ['characteristics','Характеристики', (sd.characteristics && sd.characteristics.current||[]).length+' шт', (sd.characteristics && sd.characteristics.supplier_parsed||[]).length+' шт', sd.characteristics && sd.characteristics.has_change],
        ['dimensions','Габариты', sd.dimensions && fmt(sd.dimensions.current), sd.dimensions && fmt(sd.dimensions.supplier), sd.dimensions && sd.dimensions.has_change],
      ];
      for (const [key,label,cur,prop,ch] of map) out.push({key,label,current:cur,proposed:prop,hasChange:!!ch});
      return out;
    },
    get anySelected() { return Object.values(this.selected).some(Boolean); },
    proposalLabel(field) { return field === 'photos' ? 'Переупорядочить фото' : field; },
    proposalHint(field, p) {
      if (field === 'photos') return 'Новый порядок: ' + (p.proposed||[]).length + ' фото (рекомендация агента)';
      return p.proposed != null ? String(p.proposed).slice(0,120) : '';
    },
    async startImprove(id) {
      this.improveRunning = true; this.improveStatus = 'Анализ карточки…';
      this.supplierDiff = null; this.proposal = {}; this.selected = {};
      try {
        const r = await fetch('/api/card-quality/' + id + '/improve', {method:'POST'});
        const d = await r.json();
        if (!d.success) { this.improveStatus = d.error || 'Ошибка'; this.improveRunning = false; return; }
        this.supplierDiff = d.supplier_diff || null;
        const taskIds = d.task_ids || {};
        if (Object.keys(taskIds).length === 0) {
          this.improveRunning = false;
          this.improveStatus = this.supplierDiff ? '' : 'AI-агенты офлайн — доступен только дифф поставщика (если есть)';
          if (!this.supplierDiff) this.$store.toasts.info('Нет данных поставщика и агенты офлайн');
          return;
        }
        this.improveStatus = 'AI-агенты анализируют фото…';
        this._waitTasks(id, taskIds);
      } catch (e) { this.improveStatus = 'Ошибка сети'; this.improveRunning = false; }
    },
    _waitTasks(id, taskIds) {
      const ids = Object.values(taskIds);
      this._improvePoll = setInterval(async () => {
        const states = await Promise.all(ids.map(t =>
          fetch('/agents/api/tasks/' + t + '/status').then(r => r.json()).then(s => s.task.status)));
        const done = states.every(s => s !== 'queued' && s !== 'running');
        if (done) {
          clearInterval(this._improvePoll);
          const pr = await fetch('/api/card-quality/' + id + '/proposal',
            {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({task_ids: taskIds})});
          const pd = await pr.json();
          this.proposal = pd.proposal || {};
          if (pd.supplier_diff && !this.supplierDiff) this.supplierDiff = pd.supplier_diff;
          this.improveRunning = false;
          this.improveStatus = Object.keys(this.proposal).length ? '' : 'Дополнительных предложений нет';
        }
      }, 3000);
    },
    async applySelected(id) {
      if (!this.anySelected || this.applyRunning) return;
      this.applyRunning = true;
      const updates = {};
      const sd = this.supplierDiff;
      if (sd) {
        if (this.selected.title && sd.title) updates.title = sd.title.supplier;
        if (this.selected.brand && sd.brand) updates.brand = sd.brand.supplier;
        if (this.selected.description && sd.description) updates.description = sd.description.supplier;
        if (this.selected.dimensions && sd.dimensions) updates.dimensions = sd.dimensions.supplier;
      }
      if (this.selected.photos && this.proposal.photos) updates.photos = this.proposal.photos.proposed;
      try {
        const r = await fetch('/api/card-quality/' + id + '/apply',
          {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({updates})});
        const d = await r.json();
        if (r.ok && d.success) {
          this.$store.toasts.success('Quality ' + Math.round(d.old_quality ?? 0) + ' → ' + Math.round(d.new_quality ?? 0),
                                     'Карточка улучшена');
          this.supplierDiff = null; this.proposal = {}; this.selected = {};
          await this.openDetail(id);
          await this.load();
        } else {
          this.$store.toasts.error(d.error || 'Не удалось применить изменения');
        }
      } catch (e) { this.$store.toasts.error('Ошибка сети'); }
      finally { this.applyRunning = false; }
    },
```
- [ ] **Шаг 3: Активировать строковую кнопку [Улучшить] из Фазы 1**
В строке таблицы (рядом с кнопкой «Детали», строка 38) добавить:
```html
            <td><button class="sh-btn sh-btn--accent sh-btn--sm" @click="openAndImprove(it.product_id)">Улучшить</button></td>
```
И метод (рядом с `openDetail`):
```javascript
    async openAndImprove(id) {
      await this.openDetail(id);
      this.startImprove(id);
    },
```
Примечание: если Фаза 1 уже добавила колонку/кнопку «Улучшить» — не дублировать `<td>`, только привязать `@click="openAndImprove(it.product_id)"` к существующей кнопке.
- [ ] **Шаг 4: Визуальная проверка**
Открыть `/card-quality`. Нажать «Детали» на слабой карточке → в slideover нажать «⚡ Улучшить карточку». Если у карточки есть поставщик — увидеть карточки диффа «Сейчас → Предложено» с чекбоксами. Если агенты online — после поллинга появляется блок «Переупорядочить фото». Отметить чекбоксы, нажать «Применить выбранное» → тост «Quality X → Y», деталь и список перезагружаются с новым score. Нажать строковую кнопку «Улучшить» в таблице → slideover открывается и improve запускается сразу.
- [ ] **Шаг 5: Коммит**
```bash
git add templates/card_quality.html && git commit -m "feat(card-quality): UI Улучшить карточку — дифф поставщика, предложения агентов, применить выбранное"
```

---

### Задача 3.6: Bulk «⚡ Улучшить слабые» (страница подтверждения + apply пачкой)

**Файлы:**
- Создать: `templates/card_quality_bulk_confirm.html`
- Изменить: `routes/card_quality.py` (GET страница подтверждения + POST применения; импорт `BulkEditHistory`)
- Тест: `tests/test_card_quality_bulk.py`

**Интерфейсы:**
- Потребляет: `card_quality_detail`, `collect_weak_dimensions`; `get_enrichment_service().find_supplier_data/build_preview`; `apply_card_updates`; `BulkEditHistory`; Фаза 1 `is_weak`/сортировка (фильтр слабых на уровне SQL `(quality_score<50)|(nm_rating<6)` как в `api_card_quality_list`).
- Производит: вспомогательную функцию `_collect_bulk_candidates(seller_id, limit=30)->{'candidates':[...],'total_weak':int,'shown':int}`; роуты `GET /card-quality/bulk-improve`, `POST /card-quality/bulk-improve`.

- [ ] **Шаг 1: Написать падающий тест**
```python
# tests/test_card_quality_bulk.py
import json
import unittest
from unittest.mock import patch, MagicMock


class BulkCandidatesTest(unittest.TestCase):
    def test_collects_top_n_weak_and_reports_total(self):
        from routes.card_quality import _collect_bulk_candidates
        # 35 слабых карточек, top-N=30 показываем, total_weak=35
        products = []
        for i in range(35):
            p = MagicMock(); p.id = i; p.nm_id = 1000 + i
            p.quality_score = 30.0; p.nm_rating = 5.0
            products.append(p)

        with patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.card_quality_detail') as mock_detail, \
             patch('routes.card_quality.collect_weak_dimensions', return_value=['photos']), \
             patch('routes.card_quality.get_enrichment_service') as mock_es:
            # query.filter(...).filter(...).order_by(...).limit(30).all() → первые 30
            chain = MockProduct.query.filter.return_value.filter.return_value.order_by.return_value
            chain.limit.return_value.all.return_value = products[:30]
            # count() слабых = 35
            MockProduct.query.filter.return_value.filter.return_value.count.return_value = 35
            mock_detail.side_effect = lambda p: {'product_id': p.id, 'nm_id': p.nm_id,
                                                 'quality_score': p.quality_score, 'dimensions': {},
                                                 'title': 'T', 'vendor_code': 'VC'}
            svc = MagicMock(); svc.find_supplier_data.return_value = None
            mock_es.return_value = svc

            res = _collect_bulk_candidates(seller_id=7, limit=30)
            self.assertEqual(res['shown'], 30)
            self.assertEqual(res['total_weak'], 35)
            self.assertEqual(len(res['candidates']), 30)
            self.assertEqual(res['candidates'][0]['weak_dims'], ['photos'])

    def test_candidate_includes_supplier_diff_when_available(self):
        from routes.card_quality import _collect_bulk_candidates
        p = MagicMock(); p.id = 1; p.nm_id = 1001
        with patch('routes.card_quality.Product') as MockProduct, \
             patch('routes.card_quality.card_quality_detail',
                   return_value={'product_id': 1, 'nm_id': 1001, 'quality_score': 20,
                                 'dimensions': {}, 'title': 'T', 'vendor_code': 'VC'}), \
             patch('routes.card_quality.collect_weak_dimensions', return_value=['title']), \
             patch('routes.card_quality.get_enrichment_service') as mock_es:
            chain = MockProduct.query.filter.return_value.filter.return_value.order_by.return_value
            chain.limit.return_value.all.return_value = [p]
            MockProduct.query.filter.return_value.filter.return_value.count.return_value = 1
            svc = MagicMock()
            svc.find_supplier_data.return_value = MagicMock()
            svc.build_preview.return_value = {'title': {'current': 'A', 'supplier': 'B', 'has_change': True}}
            mock_es.return_value = svc
            res = _collect_bulk_candidates(seller_id=7, limit=30)
            self.assertTrue(res['candidates'][0]['has_supplier'])
            self.assertEqual(res['candidates'][0]['supplier_diff']['title']['supplier'], 'B')


if __name__ == '__main__':
    unittest.main()
```
- [ ] **Шаг 2: Запустить тест — убедиться что падает**
Запуск: `python -m unittest tests.test_card_quality_bulk -v`
Ожидание: FAIL (`ImportError: cannot import name '_collect_bulk_candidates'`)
- [ ] **Шаг 3: Реализовать минимальный код**
Добавить импорт вверху `routes/card_quality.py`: в строку `from models import ...` добавить `BulkEditHistory`. Добавить хелпер на уровне модуля (вне `register_card_quality_routes`):
```python
BULK_IMPROVE_LIMIT = 30


def _collect_bulk_candidates(seller_id: int, limit: int = BULK_IMPROVE_LIMIT) -> dict:
    """Собирает top-N слабых карточек продавца с весами и (если есть) диффом поставщика."""
    base = Product.query.filter(
        Product.seller_id == seller_id, Product.is_active == True
    ).filter((Product.quality_score < 50) | (Product.nm_rating < 6))

    total_weak = base.count()
    rows = base.order_by(Product.quality_score.asc()).limit(limit).all()

    es = get_enrichment_service()
    candidates = []
    for p in rows:
        detail = card_quality_detail(p)
        weak_dims = collect_weak_dimensions(detail)
        supplier_diff = None
        imp = es.find_supplier_data(p, seller_id)
        if imp:
            supplier_diff = es.build_preview(p, imp)
        candidates.append({
            'product_id': detail['product_id'],
            'nm_id': detail['nm_id'],
            'vendor_code': detail.get('vendor_code'),
            'title': detail.get('title'),
            'quality_score': detail.get('quality_score'),
            'weak_dims': weak_dims,
            'has_supplier': bool(supplier_diff),
            'supplier_diff': supplier_diff,
        })
    return {'candidates': candidates, 'total_weak': total_weak, 'shown': len(candidates)}
```
Добавить роуты внутри `register_card_quality_routes`:
```python
    @app.route('/card-quality/bulk-improve', methods=['GET'])
    @login_required
    def card_quality_bulk_improve_page():
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для массового улучшения необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('api_settings'))
        data = _collect_bulk_candidates(current_user.seller.id, BULK_IMPROVE_LIMIT)
        return render_template('card_quality_bulk_confirm.html',
                               candidates=data['candidates'],
                               total_weak=data['total_weak'],
                               shown=data['shown'],
                               limit=BULK_IMPROVE_LIMIT)

    @app.route('/card-quality/bulk-improve', methods=['POST'])
    @login_required
    def card_quality_bulk_improve_apply():
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403
        action = request.form.get('action')
        if action == 'reject':
            flash('Массовое улучшение отклонено', 'info')
            return redirect(url_for('card_quality_page'))

        # Карта product_id -> список выбранных полей
        selections = {}
        for key in request.form:
            if key.startswith('apply_'):
                # apply_<pid>_<field>
                _, pid, field = key.split('_', 2)
                selections.setdefault(int(pid), []).append(field)

        if not selections:
            flash('Не выбрано ни одного изменения', 'warning')
            return redirect(url_for('card_quality_bulk_improve_page'))

        bulk = BulkEditHistory(
            seller_id=current_user.seller.id,
            operation_type='card_quality_bulk_improve',
            operation_params={'product_ids': list(selections.keys())},
            description=f'Массовое улучшение {len(selections)} карточек',
            total_products=len(selections),
            status='in_progress',
        )
        db.session.add(bulk)
        db.session.commit()

        wb_client = WildberriesAPIClient(current_user.seller.wb_api_key)
        es = get_enrichment_service()
        success, errors = 0, 0
        for pid, fields in selections.items():
            product = Product.query.filter_by(id=pid, seller_id=current_user.seller.id).first()
            if not product:
                errors += 1
                continue
            updates = {}
            imp = es.find_supplier_data(product, current_user.seller.id)
            if imp:
                preview = es.build_preview(product, imp)
                if 'title' in fields and preview['title'].get('supplier'):
                    updates['title'] = preview['title']['supplier']
                if 'brand' in fields and preview['brand'].get('supplier'):
                    updates['brand'] = preview['brand']['supplier']
                if 'description' in fields and preview['description'].get('supplier'):
                    updates['description'] = preview['description']['supplier']
                if 'dimensions' in fields and preview['dimensions'].get('supplier'):
                    updates['dimensions'] = preview['dimensions']['supplier']
            if not updates:
                continue
            try:
                res = apply_card_updates(product, updates, current_user.seller, wb_client,
                                         source='card-quality-bulk')
                if res.get('success'):
                    success += 1
                else:
                    errors += 1
            except Exception as e:
                logger.exception('bulk improve pid=%s: %s', pid, e)
                errors += 1

        bulk.success_count = success
        bulk.error_count = errors
        bulk.status = 'completed'
        bulk.wb_synced = success > 0
        from datetime import datetime as _dt
        bulk.completed_at = _dt.utcnow()
        db.session.commit()

        flash(f'Улучшено карточек: {success}, ошибок: {errors}', 'success' if success else 'warning')
        return redirect(url_for('card_quality_page'))
```
Создать `templates/card_quality_bulk_confirm.html` по образцу `prices_batch_confirm.html`:
```html
{% extends "base.html" %}
{% block title %}Массовое улучшение карточек - Seller Hub{% endblock %}
{% block content %}
<div class="max-w-5xl mx-auto px-4 py-6">
  <div class="sh-page-header"><div class="sh-page-header-content">
    <div>
      <h1 class="sh-page-title">Массовое улучшение слабых карточек</h1>
      <p class="sh-page-subtitle">
        Обработано top-{{ shown }} из {{ total_weak }} слабых карточек
        {% if total_weak > shown %}<strong>(показаны самые слабые; остальные — следующим заходом)</strong>{% endif %}
      </p>
    </div>
  </div></div>

  {% if not candidates %}
    <div class="sh-empty">Нет слабых карточек или нет данных для предложений.</div>
  {% else %}
  <form method="POST" action="{{ url_for('card_quality_bulk_improve_apply') }}"
        class="bg-white rounded-xl border border-gray-200 shadow-sm p-6">
    <div class="overflow-x-auto mb-6">
      <table class="min-w-full divide-y divide-gray-200">
        <thead class="bg-gray-50">
          <tr>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Артикул</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Quality</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Слабые места</th>
            <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Поставщик → применить</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-200">
          {% for c in candidates %}
          <tr>
            <td class="px-4 py-3 text-sm">
              <div class="text-gray-900">{{ c.nm_id }}</div>
              <div class="text-xs text-gray-400">{{ c.vendor_code or '' }}</div>
            </td>
            <td class="px-4 py-3 text-sm font-medium text-red-600">{{ c.quality_score if c.quality_score is not none else '—' }}</td>
            <td class="px-4 py-3 text-xs text-gray-600">{{ c.weak_dims|join(', ') }}</td>
            <td class="px-4 py-3 text-sm">
              {% if c.has_supplier %}
                {% set sd = c.supplier_diff %}
                {% for fld, label in [('title','Заголовок'),('brand','Бренд'),('description','Описание'),('dimensions','Габариты')] %}
                  {% if sd[fld] and sd[fld].has_change %}
                  <label class="flex items-center gap-2 mb-1 cursor-pointer">
                    <input type="checkbox" name="apply_{{ c.product_id }}_{{ fld }}" checked
                           class="w-4 h-4 rounded text-indigo-600 border-gray-300">
                    <span class="text-xs text-gray-700">{{ label }}</span>
                  </label>
                  {% endif %}
                {% endfor %}
              {% else %}
                <span class="text-xs text-gray-400">Нет данных поставщика</span>
              {% endif %}
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>

    <div class="flex items-center justify-between">
      <button type="submit" name="action" value="reject"
              class="px-6 py-2 border border-gray-300 rounded-md text-sm font-medium text-gray-700 bg-white hover:bg-gray-50">
        Отклонить
      </button>
      <button type="submit" name="action" value="confirm"
              class="px-6 py-2 rounded-md text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700">
        Подтвердить улучшения
      </button>
    </div>
  </form>
  {% endif %}
</div>
{% endblock %}
```
Добавить кнопку входа в bulk на странице `templates/card_quality.html` в `sh-page-actions` (рядом с «Обновить рейтинги»):
```html
      <a href="{{ url_for('card_quality_bulk_improve_page') }}" class="sh-btn sh-btn--accent sh-btn--sm">⚡ Улучшить слабые</a>
```
- [ ] **Шаг 4: Запустить тест — убедиться что проходит**
Запуск: `python -m unittest tests.test_card_quality_bulk -v`
Ожидание: PASS
- [ ] **Шаг 5: Визуальная проверка + Коммит**
Открыть `/card-quality` → «⚡ Улучшить слабые» → страница показывает «Обработано top-N из M», таблицу слабых с чекбоксами полей поставщика. Снять часть чекбоксов, «Подтвердить улучшения» → flash «Улучшено карточек: X, ошибок: Y», редирект на кокпит с обновлёнными score.
```bash
git add routes/card_quality.py templates/card_quality_bulk_confirm.html templates/card_quality.html tests/test_card_quality_bulk.py && git commit -m "feat(card-quality): bulk Улучшить слабые — top-N кандидаты, страница подтверждения, BulkEditHistory"
```

---

### Задача 3.7 (additive/follow-up): propose-mode для генеративных агентов + маппинг в proposal

> Можно отложить/итерировать после оценки качества генерации. Подключается без изменения уже работающего пути (photo-optimizer/supplier_diff).

**Файлы:**
- Изменить: `agents/catalog/seo_writer.py`, `agents/catalog/characteristics_filler.py`, `agents/catalog/brand_resolver.py`, `agents/catalog/category_mapper.py` (non-writing ветка в `build_task_prompt`)
- Изменить: `services/card_improver.py` (`build_proposal_from_tasks` — маппинг генеративных результатов)
- Тест: `tests/test_card_improver_proposal_generative.py`

**Интерфейсы:**
- Потребляет: `input_data` задачи с флагом `mode='propose'`; результаты вида `{field: proposed, confidence}`.
- Производит: расширенный `build_proposal_from_tasks`, добавляющий поля `title/description/brand/subject_id/characteristics` в proposal с `source='<agent>'`.

- [ ] **Шаг 1: Написать падающий тест маппинга (без LLM)**
```python
# tests/test_card_improver_proposal_generative.py
import json
import unittest


class FakeProduct:
    def __init__(self):
        self.title = 'Старый'
        self.brand = 'OldBrand'
        self.description = 'кратко'
        self.subject_id = 100
        self.photos_json = '[]'


class GenerativeProposalTest(unittest.TestCase):
    def test_maps_seo_writer_result(self):
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        results = [{'agent': 'seo-writer',
                    'result': {'title': 'Новый SEO-заголовок', 'description': 'Д'*300,
                               'keywords': ['x'], 'confidence': 0.9}}]
        proposal = build_proposal_from_tasks(product, results)
        self.assertEqual(proposal['title']['proposed'], 'Новый SEO-заголовок')
        self.assertEqual(proposal['title']['current'], 'Старый')
        self.assertEqual(proposal['title']['dimension'], 'title')
        self.assertEqual(proposal['title']['source'], 'seo-writer')
        self.assertIn('description', proposal)

    def test_maps_brand_resolver_result(self):
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        results = [{'agent': 'brand-resolver', 'result': {'brand': 'Nike', 'confidence': 0.8}}]
        proposal = build_proposal_from_tasks(product, results)
        self.assertEqual(proposal['brand']['proposed'], 'Nike')

    def test_skips_low_confidence_or_same_value(self):
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        results = [{'agent': 'brand-resolver', 'result': {'brand': 'OldBrand', 'confidence': 0.9}}]
        # совпадает с текущим → не предлагаем
        self.assertNotIn('brand', build_proposal_from_tasks(product, results))

    def test_maps_category_mapper_result(self):
        from services.card_improver import build_proposal_from_tasks
        product = FakeProduct()
        results = [{'agent': 'category-mapper', 'result': {'subject_id': 555, 'confidence': 0.95}}]
        proposal = build_proposal_from_tasks(product, results)
        self.assertEqual(proposal['subject_id']['proposed'], 555)


if __name__ == '__main__':
    unittest.main()
```
- [ ] **Шаг 2: Запустить тест — убедиться что падает**
Запуск: `python -m unittest tests.test_card_improver_proposal_generative -v`
Ожидание: FAIL (генеративные поля не маппятся — `KeyError: 'title'`)
- [ ] **Шаг 3: Реализовать минимальный код**
В `services/card_improver.py`, внутри цикла `build_proposal_from_tasks`, добавить ветки после блока `photo-optimizer` (порог `confidence >= 0.7`):
```python
        GEN_MAP = {
            'seo-writer': [('title', 'title'), ('description', 'description')],
            'brand-resolver': [('brand', 'brand')],
            'category-mapper': [('subject_id', 'category')],
            'characteristics-filler': [('characteristics', 'characteristics')],
        }
        if agent in GEN_MAP:
            conf = result.get('confidence', 1.0)
            if isinstance(conf, (int, float)) and conf >= 0.7:
                for field, dim in GEN_MAP[agent]:
                    proposed = result.get(field)
                    current = getattr(product, field, None) if field != 'characteristics' else None
                    if proposed in (None, '', [], {}):
                        continue
                    if field != 'characteristics' and proposed == current:
                        continue
                    proposal[field] = {
                        'current': current,
                        'proposed': proposed,
                        'dimension': dim,
                        'source': agent,
                    }
```
В каждом из 4 агентов `build_task_prompt` добавить non-writing ветку в начале блока `product_id` соответствующего single-task. Пример для `seo_writer.py` (в `seo_single`, в ветке `if product_id:` перед текущим текстом prompt):
```python
                if input_data.get('mode') == 'propose':
                    return (
                        f"Сгенерируй улучшенные SEO-тексты для товара (РЕЖИМ ПРЕДЛОЖЕНИЯ).\n"
                        f"Seller ID: {seller_id}, Product ID: {product_id}\n\n"
                        f"1. Получи данные товара через get_product\n"
                        f"2. Сгенерируй оптимизированный заголовок (до 60 символов)\n"
                        f"3. Сгенерируй SEO-описание (до 1000 символов)\n"
                        f"ЗАПРЕЩЕНО вызывать update_product — НИЧЕГО не сохраняй.\n\n"
                        f"Верни ТОЛЬКО JSON: {{title, description, keywords: [...], confidence: 0..1}}"
                    )
```
Аналогично:
- `brand_resolver.py` (`resolve_single`, `if product_id:`): при `mode=='propose'` — «определи бренд, НЕ вызывай update_product», `Верни JSON: {brand, confidence}`.
- `category_mapper.py` (`map_single`, `if product_id:`): при `mode=='propose'` — «определи subject_id, НЕ сохраняй», `Верни JSON: {subject_id, subject_name, confidence}`.
- `characteristics_filler.py` (`fill_single`, `if product_id:`): при `mode=='propose'` — «заполни характеристики в формате [{id,value}], НЕ вызывай update_product», `Верни JSON: {characteristics: [{id,value}], confidence}`.
Прочитать точный текст каждого `if product_id:` блока (seo_writer.py:163-174, characteristics_filler.py:113-121, brand_resolver.py вокруг :135, category_mapper.py вокруг :126) и вставить ветку `mode=='propose'` ПЕРЕД существующим writing-промптом, не меняя writing-путь.
- [ ] **Шаг 4: Запустить тест — убедиться что проходит**
Запуск: `python -m unittest tests.test_card_improver_proposal_generative tests.test_card_improver_proposal -v`
Ожидание: PASS (новые + ранее зелёные из 3.2 не сломаны)
- [ ] **Шаг 5: Коммит**
```bash
git add services/card_improver.py agents/catalog/seo_writer.py agents/catalog/brand_resolver.py agents/catalog/category_mapper.py agents/catalog/characteristics_filler.py tests/test_card_improver_proposal_generative.py && git commit -m "feat(card-quality): propose-mode генеративных агентов + маппинг в build_proposal_from_tasks"
```
