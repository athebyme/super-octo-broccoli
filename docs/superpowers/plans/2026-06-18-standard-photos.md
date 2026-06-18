# Standard Photos Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать продавцу в ЛК библиотеку стандартных фото (глобально + по категориям) с позицией (первая/последняя) и режимом (всегда / если мало), и применять их к существующим карточкам через card-quality «Улучшить» + bulk, сохраняя порядок в WB.

**Architecture:** Расширяем существующий `ProductDefaults.global_media` (JSON) полями `position`/`mode`/`order` (без миграции) + колонка `min_photos`. Чистый composer `services/standard_photos.py` собирает упорядоченный список URL. Применение через существующий `WildberriesAPIClient.upload_photos_by_url` (media/save сохраняет порядок). Публичный read-only маршрут отдаёт стандартные фото, чтобы WB их забрал.

**Tech Stack:** Python 3.11, Flask, SQLAlchemy, Jinja2, Alpine.js, Pillow (валидация размера), stdlib `unittest`.

Связанный spec: `docs/superpowers/specs/2026-06-18-standard-photos-design.md`.

## Global Constraints

- Тесты — ТОЛЬКО stdlib `unittest`, запуск `venv/bin/python -m unittest tests.test_NAME -v` (НЕ `.venv`, НЕ pytest).
- Все роуты, кроме публичной отдачи медиа, seller-scoped (`seller_id`); проверка `current_user.seller`.
- Публичная отдача стандартных фото — БЕЗ авторизации, БЕЗ листинга, только один файл по имени, защита от path traversal (`werkzeug.utils.secure_filename`, отдавать только из `data/global_media/<seller_id>/`).
- Никакого автопуша в WB без подтверждения (кроме существующего применения медиа на импорте).
- Проза/комментарии/UI-копирайт — на русском.
- Переиспользовать: `ProductDefaults.global_media` + `get_global_media_list()`, существующие роуты `/settings/product-defaults/*`, `upload_photos_by_url`, `apply_card_updates`/`CardEditHistory`, `BulkEditHistory`, `build_proposal_from_tasks`.
- Дефолты: `position='last'`, `mode='fill'`, `order=0`, `min_photos=4`. WB-лимит фото = 30. WB мин. размер 700×900.
- Каждая задача завершается коммитом.

## Shared Interfaces & Contracts

**Media item (элемент `global_media` JSON):** существующие `{filename, original_name, type, size}` + новые `position: 'first'|'last'`, `mode: 'pin'|'fill'`, `order: int`. Старые элементы без новых ключей читаются как `position='last'`, `mode='fill'`, `order=0`.

**services/standard_photos.py — владелец Задача 2:**
- `normalize_media_item(item: dict) -> dict` — гарантирует ключи position/mode/order с дефолтами (Задача 1 может переиспользовать).
- `public_media_url(seller_id, filename) -> str` — строит абсолютный публичный URL стандартного фото (база из конфигурации, см. Задача 3).
- `compose_card_photo_urls(own_urls: list[str], media_items: list[dict], seller_id: int, min_photos: int) -> list[str]` — итоговый упорядоченный список URL (first → own → last), дедуп, кап 30; `[]` если добавлять нечего (результат == own).

**models.py ProductDefaults — владелец Задача 1:**
- Новая колонка `min_photos = db.Column(db.Integer, nullable=True)` (дефолт логики 4).
- `get_min_photos(seller_id) -> int` (хелпер-функция или метод) — берёт `min_photos` из глобального правила продавца, иначе 4.
- `get_standard_media(seller_id, subject_id) -> list[dict]` — нормализованный union: media глобального правила + media правила категории `subject_id`.

**Публичный роут (Задача 3):** `GET /media/standard/<int:seller_id>/<path:filename>` → отдаёт файл из `data/global_media/<seller_id>/`, без auth, 404 при отсутствии/traversal.

**apply_card_updates (Задача 5):** для поля `photos` со значением-списком URL — вызывать `wb_client.upload_photos_by_url(product.nm_id, urls, seller_id=seller.id)` (media/save, порядок), затем `product.photos_json = json.dumps(urls)`; остальное (CardEditHistory/recompute/commit) как есть.

**Роуты card-quality (Задача 6/7):** `/improve` добавляет photos-предложение из composer; новый bulk `GET/POST /card-quality/standard-photos-bulk`.

---

## Task 1: ProductDefaults — min_photos + нормализация media + union-хелпер

**Files:**
- Modify: `models.py` (класс `ProductDefaults` ~:904 — добавить колонку `min_photos`; добавить методы/функции `get_min_photos`, `get_standard_media`)
- Modify: миграция — добавить колонку `min_photos` в `product_defaults` (паттерн стартап-миграций проекта; найди где регистрируются `_run_startup_migrations` / `migrations/add_*` и добавь идемпотентный ALTER `ALTER TABLE product_defaults ADD COLUMN min_photos INTEGER`)
- Test: `tests/test_standard_photos_model.py`

**Interfaces:**
- Produces: `ProductDefaults.min_photos` (Integer, nullable); `get_min_photos(seller_id) -> int` (дефолт 4); `get_standard_media(seller_id, subject_id) -> list[dict]` (нормализованный union global+category); `normalize_media_item(item) -> dict`.

- [ ] **Step 1: Написать падающий тест**
```python
# tests/test_standard_photos_model.py
# -*- coding: utf-8 -*-
import json, unittest
from flask import Flask
from models import db, ProductDefaults


def _app():
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)
    return app


class StandardMediaModelTest(unittest.TestCase):
    def setUp(self):
        self.app = _app(); self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def _media(self, fn, position=None, mode=None, order=None, typ='photo'):
        d = {'filename': fn, 'original_name': fn, 'type': typ, 'size': 1}
        if position is not None: d['position'] = position
        if mode is not None: d['mode'] = mode
        if order is not None: d['order'] = order
        return d

    def test_normalize_backward_compat(self):
        from models import normalize_media_item
        n = normalize_media_item({'filename': 'a.jpg', 'type': 'photo'})
        self.assertEqual(n['position'], 'last')
        self.assertEqual(n['mode'], 'fill')
        self.assertEqual(n['order'], 0)

    def test_get_min_photos_default_and_value(self):
        from models import get_min_photos
        self.assertEqual(get_min_photos(1), 4)  # нет правила → дефолт
        db.session.add(ProductDefaults(seller_id=1, rule_type='global', min_photos=6))
        db.session.commit()
        self.assertEqual(get_min_photos(1), 6)

    def test_get_standard_media_union_global_and_category(self):
        from models import get_standard_media
        db.session.add(ProductDefaults(seller_id=1, rule_type='global',
            global_media=json.dumps([self._media('g.jpg', 'first', 'pin', 0)])))
        db.session.add(ProductDefaults(seller_id=1, rule_type='category', wb_subject_id=105,
            global_media=json.dumps([self._media('c.jpg', 'last', 'fill', 1)])))
        db.session.commit()
        media = get_standard_media(1, 105)
        names = {m['filename'] for m in media}
        self.assertEqual(names, {'g.jpg', 'c.jpg'})
        # для другой категории — только глобальное
        self.assertEqual({m['filename'] for m in get_standard_media(1, 999)}, {'g.jpg'})
```

- [ ] **Step 2: Запустить — убедиться что падает**
Run: `venv/bin/python -m unittest tests.test_standard_photos_model -v`
Expected: FAIL (ImportError `normalize_media_item`/`get_min_photos`/`get_standard_media`; нет колонки `min_photos`).

- [ ] **Step 3: Реализовать**
В `models.py` в классе `ProductDefaults` добавить колонку рядом с остальными (после `global_media`):
```python
    min_photos = db.Column(db.Integer, nullable=True)  # порог «мало фото» (глобальное правило); дефолт логики 4
```
В `models.py` (на уровне модуля, рядом с ProductDefaults) добавить:
```python
def normalize_media_item(item: dict) -> dict:
    """Гарантирует поля позиционирования у элемента global_media (обратная совместимость)."""
    out = dict(item or {})
    out.setdefault('position', 'last')
    out.setdefault('mode', 'fill')
    out.setdefault('order', 0)
    return out


def get_min_photos(seller_id: int) -> int:
    """Порог «мало фото» из глобального правила продавца; дефолт 4."""
    rule = ProductDefaults.query.filter_by(seller_id=seller_id, rule_type='global').first()
    val = getattr(rule, 'min_photos', None) if rule else None
    return int(val) if val else 4


def get_standard_media(seller_id: int, subject_id) -> list:
    """Нормализованный union стандартных медиа: глобальное правило + правило категории subject_id."""
    items = []
    g = ProductDefaults.query.filter_by(seller_id=seller_id, rule_type='global').first()
    if g:
        items.extend(g.get_global_media_list() or [])
    if subject_id is not None:
        c = ProductDefaults.query.filter_by(
            seller_id=seller_id, rule_type='category', wb_subject_id=subject_id).first()
        if c:
            items.extend(c.get_global_media_list() or [])
    return [normalize_media_item(m) for m in items]
```
Добавить идемпотентную миграцию колонки. Найди в проекте startup-миграции (grep `_run_startup_migrations` / `ADD COLUMN`), добавь:
```python
('product_defaults', 'min_photos', 'INTEGER'),
```
в список ALTER (формат проекта — см. соседние записи вроде `('suppliers', 'image_gen_provider', ...)` в seller_platform.py).

- [ ] **Step 4: Запустить — PASS**
Run: `venv/bin/python -m unittest tests.test_standard_photos_model -v` → OK. Затем весь набор: `venv/bin/python -m unittest discover -s tests 2>&1 | tail -3` (новых падений быть не должно; 5 pre-existing pytest-ошибок — ок).

- [ ] **Step 5: Commit**
```bash
git add models.py tests/test_standard_photos_model.py seller_platform.py
git commit -m "feat(standard-photos): ProductDefaults min_photos + normalize/union media helpers"
```

---

## Task 2: Composer — `services/standard_photos.py`

**Files:**
- Create: `services/standard_photos.py`
- Test: `tests/test_standard_photos_compose.py`

**Interfaces:**
- Consumes: `normalize_media_item` (Task 1).
- Produces: `public_media_url(seller_id, filename) -> str`; `compose_card_photo_urls(own_urls, media_items, seller_id, min_photos) -> list[str]`.

- [ ] **Step 1: Написать падающий тест**
```python
# tests/test_standard_photos_compose.py
# -*- coding: utf-8 -*-
import unittest
from services.standard_photos import compose_card_photo_urls, public_media_url


def m(fn, position, mode, order=0):
    return {'filename': fn, 'type': 'photo', 'position': position, 'mode': mode, 'order': order}


class ComposeTest(unittest.TestCase):
    def setUp(self):
        self.own = ['https://wb/own1.jpg', 'https://wb/own2.jpg']  # 2 свои (sparse при min=4)

    def test_pin_always_added_even_when_not_sparse(self):
        own = ['o1', 'o2', 'o3', 'o4', 'o5']  # не sparse при min=4
        media = [m('banner.jpg', 'first', 'pin')]
        res = compose_card_photo_urls(own, media, seller_id=1, min_photos=4)
        self.assertTrue(res[0].endswith('banner.jpg'))
        self.assertEqual(res[1:], own)

    def test_fill_only_when_sparse(self):
        media = [m('size.jpg', 'last', 'fill')]
        # sparse (2 < 4) → добавляется
        res = compose_card_photo_urls(self.own, media, 1, 4)
        self.assertTrue(res[-1].endswith('size.jpg'))
        # не sparse → не добавляется
        own_full = ['o1', 'o2', 'o3', 'o4']
        self.assertEqual(compose_card_photo_urls(own_full, media, 1, 4), [])

    def test_order_first_own_last(self):
        media = [m('b.jpg', 'first', 'pin', 0), m('s.jpg', 'last', 'pin', 0)]
        res = compose_card_photo_urls(self.own, media, 1, 4)
        self.assertTrue(res[0].endswith('b.jpg'))
        self.assertEqual(res[1:3], self.own)
        self.assertTrue(res[-1].endswith('s.jpg'))

    def test_order_within_group_sorted(self):
        media = [m('b2.jpg', 'first', 'pin', 2), m('b1.jpg', 'first', 'pin', 1)]
        res = compose_card_photo_urls(self.own, media, 1, 4)
        self.assertTrue(res[0].endswith('b1.jpg'))
        self.assertTrue(res[1].endswith('b2.jpg'))

    def test_dedup_and_cap_30(self):
        media = [m(f'p{i}.jpg', 'last', 'pin', i) for i in range(40)]
        res = compose_card_photo_urls(self.own, media, 1, 4)
        self.assertEqual(len(res), 30)

    def test_returns_empty_when_nothing_added(self):
        self.assertEqual(compose_card_photo_urls(self.own, [], 1, 4), [])

    def test_skips_non_photo(self):
        media = [{'filename': 'v.mp4', 'type': 'video', 'position': 'first', 'mode': 'pin', 'order': 0}]
        self.assertEqual(compose_card_photo_urls(self.own, media, 1, 4), [])
```

- [ ] **Step 2: Запустить — FAIL** (`ModuleNotFoundError: services.standard_photos`)
Run: `venv/bin/python -m unittest tests.test_standard_photos_compose -v`

- [ ] **Step 3: Реализовать**
```python
# services/standard_photos.py
# -*- coding: utf-8 -*-
"""Композиция стандартных фото продавца в упорядоченный набор URL для карточки."""
import os

WB_MAX_PHOTOS = 30


def public_media_url(seller_id: int, filename: str) -> str:
    """Абсолютный публичный URL стандартного фото (WB забирает по нему)."""
    base = (os.environ.get('PUBLIC_BASE_URL')
            or os.environ.get('DOMAIN')
            or '').rstrip('/')
    if base and not base.startswith('http'):
        base = 'https://' + base
    return f"{base}/media/standard/{seller_id}/{filename}"


def compose_card_photo_urls(own_urls, media_items, seller_id, min_photos):
    """Итог = [first: pin + (fill если sparse)] + own + [last: ...], дедуп, кап 30.

    Возвращает [] если добавлять нечего (итог == own).
    """
    own = list(own_urls or [])
    sparse = len(own) < int(min_photos or 4)
    photos = [it for it in (media_items or []) if (it or {}).get('type', 'photo') == 'photo']

    def picked(position):
        sel = [it for it in photos
               if it.get('position', 'last') == position
               and (it.get('mode', 'fill') == 'pin' or sparse)]
        sel.sort(key=lambda it: it.get('order', 0))
        return [public_media_url(seller_id, it['filename']) for it in sel]

    first, last = picked('first'), picked('last')
    if not first and not last:
        return []

    result, seen = [], set()
    for url in first + own + last:
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result[:WB_MAX_PHOTOS]
```

- [ ] **Step 4: Запустить — PASS**
Run: `venv/bin/python -m unittest tests.test_standard_photos_compose -v` → OK.

- [ ] **Step 5: Commit**
```bash
git add services/standard_photos.py tests/test_standard_photos_compose.py
git commit -m "feat(standard-photos): pure composer compose_card_photo_urls (first/own/last, pin/fill)"
```

---

## Task 3: Публичный маршрут отдачи стандартных фото

**Files:**
- Modify: `routes/product_defaults.py` (добавить публичный роут рядом с существующим `product_defaults_serve_media`; прочитай его verbatim — там уже есть `_get_media_dir(seller_id)` и `send_file`)
- Test: `tests/test_standard_photos_public_route.py`

**Interfaces:**
- Produces: `GET /media/standard/<int:seller_id>/<path:filename>` (без `@login_required`) → файл из `data/global_media/<seller_id>/`, 404 если нет/traversal.

- [ ] **Step 1: Написать падающий тест**
```python
# tests/test_standard_photos_public_route.py
# -*- coding: utf-8 -*-
import os, unittest, tempfile
from flask import Flask


class PublicMediaRouteTest(unittest.TestCase):
    def setUp(self):
        from routes.product_defaults import register_product_defaults_routes
        self.app = Flask(__name__, root_path=tempfile.mkdtemp())
        self.app.config['TESTING'] = True
        register_product_defaults_routes(self.app)
        # положим файл
        d = os.path.join(self.app.root_path, 'data', 'global_media', '7')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'pic.jpg'), 'wb') as f:
            f.write(b'\xff\xd8\xff\xe0JPEGDATA')
        self.client = self.app.test_client()

    def test_serves_file_without_login(self):
        r = self.client.get('/media/standard/7/pic.jpg')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'JPEGDATA', r.data)

    def test_missing_file_404(self):
        self.assertEqual(self.client.get('/media/standard/7/nope.jpg').status_code, 404)

    def test_path_traversal_blocked(self):
        r = self.client.get('/media/standard/7/..%2f..%2fmodels.py')
        self.assertIn(r.status_code, (400, 404))
```

- [ ] **Step 2: FAIL** (404 — роут не зарегистрирован)
Run: `venv/bin/python -m unittest tests.test_standard_photos_public_route -v`

- [ ] **Step 3: Реализовать** — в `routes/product_defaults.py` внутри `register_product_defaults_routes(app)` добавить (рядом с приватным serve-media; переиспользуй `_get_media_dir` если есть, иначе путь `data/global_media/<seller_id>`):
```python
    @app.route('/media/standard/<int:seller_id>/<path:filename>')
    def standard_media_public(seller_id, filename):
        """Публичная read-only отдача стандартного фото (чтобы WB media/save забрал по URL)."""
        from werkzeug.utils import secure_filename
        safe = secure_filename(filename)
        if not safe or safe != filename:
            from flask import abort
            abort(404)
        directory = os.path.join(current_app.root_path, 'data', 'global_media', str(seller_id))
        path = os.path.join(directory, safe)
        if not os.path.isfile(path):
            from flask import abort
            abort(404)
        return send_file(path)
```
(`os`, `send_file`, `current_app` уже импортированы в файле — проверь; если нет, добавь.)

- [ ] **Step 4: PASS**
Run: `venv/bin/python -m unittest tests.test_standard_photos_public_route -v` → OK.

- [ ] **Step 5: Commit**
```bash
git add routes/product_defaults.py tests/test_standard_photos_public_route.py
git commit -m "feat(standard-photos): public read-only media route for WB media/save fetch"
```

---

## Task 4: ЛК UI — позиция/режим/порядок + min_photos + сохранение

**Files:**
- Modify: `templates/product_defaults.html` (вкладка «Медиа» ~:501-560 — добавить у каждого фото селекторы Позиция/Режим + поле «Минимум фото»; прочитай блок verbatim)
- Modify: `routes/product_defaults.py` (роуты `save-global`/`upload-media`/`save-category` ~:84-345 — принимать и сохранять `position`/`mode`/`order` в media-элементах и `min_photos`; прочитай verbatim)
- Test: `tests/test_standard_photos_save.py`

**Interfaces:**
- Consumes: `normalize_media_item` (Task 1), `ProductDefaults.min_photos` (Task 1).
- Produces: сохранённые media-элементы с position/mode/order; min_photos в глобальном правиле.

- [ ] **Step 1: Написать падающий тест** (тест роута сохранения метаданных медиа — Flask client + seeded seller/login; mirror `tests/test_card_quality_summary_route.py` для паттерна логина)
```python
# tests/test_standard_photos_save.py
# (псевдоструктура — реализатор подгоняет под фактический контракт save-роутов,
#  прочитав routes/product_defaults.py; СУТЬ ассертов:)
#  1) POST на сохранение медиа с position='first', mode='pin', order=2 →
#     ProductDefaults.global_media элемент содержит эти значения.
#  2) POST min_photos=6 → ProductDefaults(rule_type='global').min_photos == 6.
#  Реализатор: точные имена полей/эндпоинтов взять из routes/product_defaults.py.
```
ВАЖНО: прочитай `routes/product_defaults.py` save-роуты, чтобы тест отражал реальный контракт; ассерты — на состояние БД (реальное), не на моки.

- [ ] **Step 2: FAIL**
Run: `venv/bin/python -m unittest tests.test_standard_photos_save -v`

- [ ] **Step 3: Реализовать**
- В `routes/product_defaults.py`: при сохранении медиа-элементов записывать `position` (из формы, дефолт 'last'), `mode` (дефолт 'fill'), `order` (int); добавить приём `min_photos` в save-global. Нормализуй через `normalize_media_item`.
- В `templates/product_defaults.html` (вкладка media): у каждого превью добавить `<select>` Позиция (Первая/Последняя) и Режим (Всегда/Если мало) + drag/`order`; над сеткой — поле «Минимум фото» (число). Сохранение — существующим Alpine/fetch паттерном (CSRF авто). Русский копирайт + подсказка из spec §7.

- [ ] **Step 4: PASS** + проверь Jinja parse `product_defaults.html`.
Run: `venv/bin/python -m unittest tests.test_standard_photos_save -v`

- [ ] **Step 5: Commit**
```bash
git add routes/product_defaults.py templates/product_defaults.html tests/test_standard_photos_save.py
git commit -m "feat(standard-photos): ЛК UI position/mode/order + min_photos with save"
```

---

## Task 5: apply_card_updates — фото через media/save (порядок)

**Files:**
- Modify: `services/card_improver.py` (ветка `photos` в `apply_card_updates` ~ текущая «локально-только»; прочитай verbatim)
- Test: `tests/test_card_improver_photos.py`

**Interfaces:**
- Consumes: `WildberriesAPIClient.upload_photos_by_url(nm_id, photo_urls, seller_id=...)`.
- Produces: при `updates['photos']` = список URL → media/save в WB + `product.photos_json = json.dumps(urls)` + 'photos' в fields_applied.

- [ ] **Step 1: Написать падающий тест** (FakeWBClient записывает `upload_photos_by_url`; assert вызван с упорядоченным списком, photos_json обновлён, 'photos' в fields_applied, CardEditHistory создан). Mirror fixture из `tests/test_card_improver_apply.py`.
```python
# tests/test_card_improver_photos.py — ключевые ассерты:
#  fake.calls содержит ('upload_photos_by_url', nm_id, ['u_first','own1','own2','u_last'])
#  res['fields_applied'] содержит 'photos'; product.photos_json == json.dumps(тот же список)
#  res['wb_sync'] is True; создана 1 запись CardEditHistory с changed_fields=['photos']
```

- [ ] **Step 2: FAIL**
Run: `venv/bin/python -m unittest tests.test_card_improver_photos -v`

- [ ] **Step 3: Реализовать** — в `apply_card_updates`, в обработке поля `photos`: если значение — непустой список, вызвать `wb_client.upload_photos_by_url(product.nm_id, clean['photos'], seller_id=seller.id)` внутри того же try (как текст-поля), при успехе `product.photos_json = json.dumps(clean['photos'], ensure_ascii=False)` и `fields_applied.append('photos')`, `wb_sync_success=True`. (Заменяет прежнюю «локально-только» запись фото — теперь фото уходят в WB упорядоченно.) Сохрани failure-path (исключение → wb_error, status 'failed').

- [ ] **Step 4: PASS** + регресс `venv/bin/python -m unittest tests.test_card_improver_apply -v`.

- [ ] **Step 5: Commit**
```bash
git add services/card_improver.py tests/test_card_improver_photos.py
git commit -m "feat(standard-photos): apply_card_updates pushes photos via WB media/save (ordered)"
```

---

## Task 6: Интеграция в «Улучшить» (photos-предложение из стандартных фото)

**Files:**
- Modify: `routes/card_quality.py` (`/improve` и/или `/proposal` — добавить photos-предложение из composer; прочитай verbatim)
- Modify: `templates/card_quality.html` (slideover — показать photos-предложение «было→стало» в порядке; уже есть supplier_diff/proposal блоки — добавить рядом)
- Test: `tests/test_card_quality_standard_photos_improve.py`

**Interfaces:**
- Consumes: `get_standard_media`/`get_min_photos` (Task 1), `compose_card_photo_urls` (Task 2). Форма proposal: `proposal['photos'] = {'current': own_urls, 'proposed': composed_urls, 'dimension': 'photos', 'source': 'standard-photos'}`.
- Produces: photos-предложение в ответе `/improve` (или `/proposal`).

- [ ] **Step 1: Написать падающий тест** (seeded ProductDefaults с media `pin first`; Product с 1 фото → `/improve` (или /proposal) возвращает proposal с ключом 'photos', proposed[0] = публичный URL баннера, дальше own). Mirror `tests/test_card_quality_improve_route.py`.

- [ ] **Step 2: FAIL**
Run: `venv/bin/python -m unittest tests.test_card_quality_standard_photos_improve -v`

- [ ] **Step 3: Реализовать** — в `/improve` (или при сборке proposal) вычислить `own` (из product.photos_json), `media = get_standard_media(seller_id, product.subject_id)`, `composed = compose_card_photo_urls(own, media, seller_id, get_min_photos(seller_id))`; если `composed` непустой — добавить `proposal['photos'] = {...}` (или вернуть в `/improve` как часть supplier_diff-аналога). В slideover (`card_quality.html`) отрисовать photos-предложение: миниатюры в итоговом порядке + чекбокс; на «Применить» отправлять `updates.photos = composed` (Task 5 применит). Русский копирайт.

- [ ] **Step 4: PASS** + Jinja parse.

- [ ] **Step 5: Commit**
```bash
git add routes/card_quality.py templates/card_quality.html tests/test_card_quality_standard_photos_improve.py
git commit -m "feat(standard-photos): offer composed photos proposal in card-quality improve"
```

---

## Task 7: Bulk «Дополнить фото слабым карточкам»

**Files:**
- Modify: `routes/card_quality.py` (новые `GET /card-quality/standard-photos-bulk` + `POST .../apply`)
- Create: `templates/card_quality_standard_photos_bulk.html` (по образцу `templates/card_quality_bulk_confirm.html`)
- Test: `tests/test_card_quality_standard_photos_bulk.py`

**Interfaces:**
- Consumes: `compose_card_photo_urls`, `get_standard_media`, `get_min_photos` (Task 1/2), `apply_card_updates` (Task 5), `BulkEditHistory`.
- Produces: страница подтверждения top-N карточек с `len(own) < min_photos` и непустой композицией; POST применяет пачкой; явно «обработано N из M».

- [ ] **Step 1: Написать падающий тест** (candidate-gathering: seeded Products разной полноты фото + ProductDefaults media → функция/роут возвращает только sparse-карточки с непустой композицией, top-N, total). Real in-memory sqlite (НЕ моки ORM).

- [ ] **Step 2: FAIL**
Run: `venv/bin/python -m unittest tests.test_card_quality_standard_photos_bulk -v`

- [ ] **Step 3: Реализовать** — кандидаты: активные Product продавца с `len(own) < get_min_photos` и непустым `compose_card_photo_urls`; страница подтверждения (collapsible, чекбоксы — паттерн `card_quality_bulk_confirm.html`); POST → per-product `apply_card_updates(product, {'photos': composed}, ...)` + `BulkEditHistory`; явный «обработано top-N из M». Кнопку входа добавить рядом с «⚡ Улучшить слабые».

- [ ] **Step 4: PASS** + Jinja parse.

- [ ] **Step 5: Commit**
```bash
git add routes/card_quality.py templates/card_quality_standard_photos_bulk.html tests/test_card_quality_standard_photos_bulk.py
git commit -m "feat(standard-photos): bulk fill sparse cards with standard photos"
```

---

## Task 8: Валидация размера при загрузке (WB ≥700×900)

**Files:**
- Modify: `routes/product_defaults.py` (`upload-media` ~:264 — проверить размеры через Pillow; прочитай verbatim)
- Test: `tests/test_standard_photos_upload_validation.py`

**Interfaces:**
- Produces: upload-media отклоняет фото меньше 700×900 с понятной ошибкой (для `type=='photo'`).

- [ ] **Step 1: Написать падающий тест** (POST маленькой картинки (100×100, сгенерить через Pillow) → ответ-ошибка/не сохранён; нормальной (700×900) → ок). Если Pillow недоступен в venv — реализатор подтверждает наличие `from PIL import Image` (он уже используется в `image_generation_service`).

- [ ] **Step 2: FAIL**
Run: `venv/bin/python -m unittest tests.test_standard_photos_upload_validation -v`

- [ ] **Step 3: Реализовать** — в `upload-media`: для `_file_type(filename)=='photo'` открыть через `PIL.Image`, проверить `width>=700 and height>=900`; иначе вернуть 400 с русским сообщением «Фото меньше 700×900 — WB отклонит». Видео не проверять.

- [ ] **Step 4: PASS**

- [ ] **Step 5: Commit**
```bash
git add routes/product_defaults.py tests/test_standard_photos_upload_validation.py
git commit -m "feat(standard-photos): validate min 700x900 on media upload"
```
