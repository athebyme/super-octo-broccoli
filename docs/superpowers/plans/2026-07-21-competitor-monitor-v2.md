# Мониторинг конкурентов v2 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Полный рефактор мониторинга конкурентов WB: сбор через singleton scheduler вместо per-seller тредов, честная политика наблюдений (miss ≠ изменение), чистка ~10M мусорных снимков, алерты в общем центре уведомлений, связка групп со своими карточками, UI «Тёплая редакция».

**Architecture:** ORM-free fetch-слой (`services/competitor_fetch.py`: basket CDN для метаданных, catalog/search для цен, глобальный rate limiter + circuit breaker) + sync-оркестрация (`services/competitor_monitor.py`, переписан: bounded тик scheduler'а каждую минуту, до 2 due-продавцов, фазирование сеть→запись). Треды удаляются полностью. Спека: `docs/superpowers/specs/2026-07-21-competitor-monitor-v2-design.md`.

**Tech Stack:** Flask/SQLAlchemy/SQLite, APScheduler (существующий singleton), requests, Jinja2 + Alpine.js + Tailwind CDN, Chart.js 4.4.0 + `window.shChart`.

## Поправка пользователя (2026-07-21, в ходе исполнения)

«Старое выпиливаем с корнем»: миграция v2 дополнительно ДРОПАЕТ колонки
`competitor_monitor_settings.pause_between_cycles_seconds` и
`.requests_per_minute` (guard: `sqlite_version >= 3.35`, иначе колонки просто
остаются мёртвыми — идемпотентно в обоих случаях); модель и `to_dict()` эти
поля не объявляют. `is_running` НЕ legacy — v2 использует его для живого
статуса синка. Неподключённый v1-скрипт
`migrations/migrate_add_competitor_monitoring.py` удаляется, если grep не
находит ссылок на него (Task 9).

## Global Constraints

- Ветка: `feature/competitor-monitor-v2` (уже создана, спека закоммичена).
- Тесты запускаются `SKIP_SCHEDULER=1 python -m pytest -q tests/<file>`; никаких реальных WB/LLM вызовов в тестах.
- Цены конкурентов хранятся **в рублях, integer** (существующие данные уже в рублях; комментарии «в копейках» в models.py — ложь, исправить).
- Интервал синка: 30..1440 минут, default 60 (`normalize_sync_interval_minutes`).
- Глобальный rate limit публичных WB-вызовов: env `COMPETITOR_PUBLIC_RPM`, default 20, границы 1..60. Один limiter на процесс, общий для всех продавцов.
- Никаких `time.sleep` ожиданий 429 ни в request path, ни в sync (429 = немедленное завершение источника + cooldown).
- SQLite-инвариант: сетевые вызовы не внутри открытой write-транзакции; изоляция ошибок строк — `db.session.begin_nested()`.
- Прокси — credential: чтение с legacy plaintext fallback, запись fail-closed через Fernet (`ENCRYPTION_KEY`); наружу только маска.
- UI: только sh-* компоненты и токены, без inline-hex; графики через `window.shChart` и `--chart-N`; обе темы; поллинг с visibility-гейтом.
- Каждый коммит: `git commit -m "..."` с трейлером `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- После Python-изменений: `python -m py_compile <files>` и `git diff --check`.

---

### Task 1: Миграция схемы v2 + новые колонки моделей

**Files:**
- Create: `migrations/migrate_competitor_monitor_v2.py`
- Modify: `models.py` (секция `# ============= МОНИТОРИНГ КОНКУРЕНТОВ =============`, строки ~10509–10792)
- Test: `tests/test_competitor_v2_migration.py`

**Interfaces:**
- Produces: колонки `competitor_monitor_settings.sync_interval_minutes INT DEFAULT 60`, `.next_sync_due_at DATETIME NULL`, `.discount_alert_pp FLOAT DEFAULT 5.0`; `competitor_products.metadata_synced_at DATETIME NULL`, `.is_adult BOOLEAN NULL`, `.price_miss_count INT DEFAULT 0`, `.last_price_at DATETIME NULL`; `competitor_groups.import_requested BOOLEAN DEFAULT 0`. Те же поля объявлены в моделях. `CompetitorMonitorSettings.to_dict()` содержит `sync_interval_minutes`, `next_sync_due_at`, `discount_alert_pp`; `CompetitorProduct.to_dict()` содержит `last_price_at`, `price_miss_count`, `is_adult`; `CompetitorGroup.to_dict()` содержит `import_requested`.

- [ ] **Step 1: Написать падающий тест миграции**

```python
# tests/test_competitor_v2_migration.py
# -*- coding: utf-8 -*-
"""Миграция v2 мониторинга конкурентов: идемпотентность и backfill."""
import sqlite3
import tempfile
import unittest
from pathlib import Path


def _create_v1_schema(db_path):
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE competitor_monitor_settings (
            id INTEGER PRIMARY KEY, seller_id INTEGER NOT NULL,
            is_enabled BOOLEAN DEFAULT 0, is_running BOOLEAN DEFAULT 0,
            price_change_alert_percent FLOAT DEFAULT 5.0,
            requests_per_minute INTEGER DEFAULT 60,
            max_products INTEGER DEFAULT 100000,
            pause_between_cycles_seconds INTEGER DEFAULT 60,
            proxy_url VARCHAR(500),
            last_sync_at DATETIME, last_sync_status VARCHAR(50),
            last_sync_error TEXT, last_full_cycle_duration FLOAT,
            total_products_monitored INTEGER DEFAULT 0,
            total_cycles_completed INTEGER DEFAULT 0,
            created_at DATETIME, updated_at DATETIME
        );
        CREATE TABLE competitor_groups (
            id INTEGER PRIMARY KEY, seller_id INTEGER NOT NULL,
            name VARCHAR(200) NOT NULL, description TEXT, color VARCHAR(7),
            own_product_id INTEGER, auto_source VARCHAR(20),
            auto_source_value VARCHAR(200), is_active BOOLEAN DEFAULT 1,
            created_at DATETIME, updated_at DATETIME
        );
        CREATE TABLE competitor_products (
            id INTEGER PRIMARY KEY, seller_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL, nm_id BIGINT NOT NULL,
            title VARCHAR(500), brand VARCHAR(200), supplier_name VARCHAR(200),
            wb_supplier_id BIGINT, image_url VARCHAR(500),
            current_price INTEGER, current_sale_price INTEGER,
            current_rating FLOAT, current_feedbacks_count INTEGER,
            current_total_stock INTEGER, priority INTEGER DEFAULT 2,
            is_active BOOLEAN DEFAULT 1, last_fetched_at DATETIME,
            fetch_error_count INTEGER DEFAULT 0,
            created_at DATETIME, updated_at DATETIME
        );
        INSERT INTO competitor_monitor_settings
            (id, seller_id, is_enabled, pause_between_cycles_seconds)
            VALUES (1, 2, 1, 60), (2, 1, 0, 0);
        INSERT INTO competitor_products
            (id, seller_id, group_id, nm_id, current_sale_price, last_fetched_at)
            VALUES (1, 2, 1, 111, 9471, '2026-07-21 10:00:00'),
                   (2, 2, 1, 222, NULL, '2026-07-21 10:00:00');
    """)
    con.commit()
    con.close()


class CompetitorV2MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        _create_v1_schema(self.db_path)

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    def _columns(self, table):
        con = sqlite3.connect(self.db_path)
        cols = {row[1] for row in con.execute(f'PRAGMA table_info({table})')}
        con.close()
        return cols

    def test_adds_columns_and_backfills(self):
        from migrations.migrate_competitor_monitor_v2 import migrate
        self.assertTrue(migrate(self.db_path))
        self.assertIn('sync_interval_minutes', self._columns('competitor_monitor_settings'))
        self.assertIn('next_sync_due_at', self._columns('competitor_monitor_settings'))
        self.assertIn('discount_alert_pp', self._columns('competitor_monitor_settings'))
        self.assertIn('metadata_synced_at', self._columns('competitor_products'))
        self.assertIn('is_adult', self._columns('competitor_products'))
        self.assertIn('price_miss_count', self._columns('competitor_products'))
        self.assertIn('last_price_at', self._columns('competitor_products'))
        self.assertIn('import_requested', self._columns('competitor_groups'))
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            'SELECT seller_id, sync_interval_minutes FROM competitor_monitor_settings ORDER BY seller_id'
        ).fetchall()
        # backfill: всем существующим строкам interval 60
        self.assertEqual(rows, [(1, 60), (2, 60)])
        # last_price_at backfill только там, где цена есть
        lp = con.execute(
            'SELECT id, last_price_at FROM competitor_products ORDER BY id'
        ).fetchall()
        self.assertEqual(lp[0][1], '2026-07-21 10:00:00')
        self.assertIsNone(lp[1][1])
        con.close()

    def test_idempotent(self):
        from migrations.migrate_competitor_monitor_v2 import migrate
        self.assertTrue(migrate(self.db_path))
        self.assertTrue(migrate(self.db_path))  # повторный запуск не падает

    def test_missing_tables_is_noop(self):
        from migrations.migrate_competitor_monitor_v2 import migrate
        empty = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        empty.close()
        try:
            self.assertTrue(migrate(empty.name))
        finally:
            Path(empty.name).unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_competitor_v2_migration.py`
Expected: FAIL — `ModuleNotFoundError`/`ImportError: migrate_competitor_monitor_v2`.

- [ ] **Step 3: Написать миграцию**

По образцу `migrations/migrate_add_wb_card_audit.py` (та же шапка: logging, `BASE_DIR`, `DEFAULT_DB_PATH`, `main()` с `sys.argv[1]`):

```python
# migrations/migrate_competitor_monitor_v2.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Миграция: мониторинг конкурентов v2 (scheduler вместо тредов).

Добавляет:
- competitor_monitor_settings: sync_interval_minutes (30..1440, default 60),
  next_sync_due_at (NULL = due сейчас), discount_alert_pp;
- competitor_products: metadata_synced_at, is_adult, price_miss_count,
  last_price_at (backfill из last_fetched_at, где цена наблюдалась);
- competitor_groups: import_requested (заявка на импорт каталога продавца).

Старые колонки (pause_between_cycles_seconds, requests_per_minute,
is_running) сохраняются, но рантаймом v2 не используются.
Идемпотентная — безопасно запускать повторно.
"""
import logging
import sqlite3
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / 'data' / 'seller_platform.db'


def migrate(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        tables = {row[0] for row in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        added = []

        if 'competitor_monitor_settings' in tables:
            cols = {r[1] for r in cur.execute(
                'PRAGMA table_info(competitor_monitor_settings)')}
            if 'sync_interval_minutes' not in cols:
                cur.execute("ALTER TABLE competitor_monitor_settings "
                            "ADD COLUMN sync_interval_minutes INTEGER DEFAULT 60")
                cur.execute("UPDATE competitor_monitor_settings "
                            "SET sync_interval_minutes = 60 "
                            "WHERE sync_interval_minutes IS NULL")
                added.append('settings.sync_interval_minutes')
            if 'next_sync_due_at' not in cols:
                cur.execute("ALTER TABLE competitor_monitor_settings "
                            "ADD COLUMN next_sync_due_at DATETIME")
                added.append('settings.next_sync_due_at')
            if 'discount_alert_pp' not in cols:
                cur.execute("ALTER TABLE competitor_monitor_settings "
                            "ADD COLUMN discount_alert_pp FLOAT DEFAULT 5.0")
                cur.execute("UPDATE competitor_monitor_settings "
                            "SET discount_alert_pp = 5.0 "
                            "WHERE discount_alert_pp IS NULL")
                added.append('settings.discount_alert_pp')

        if 'competitor_products' in tables:
            cols = {r[1] for r in cur.execute(
                'PRAGMA table_info(competitor_products)')}
            if 'metadata_synced_at' not in cols:
                cur.execute("ALTER TABLE competitor_products "
                            "ADD COLUMN metadata_synced_at DATETIME")
                added.append('products.metadata_synced_at')
            if 'is_adult' not in cols:
                cur.execute("ALTER TABLE competitor_products "
                            "ADD COLUMN is_adult BOOLEAN")
                added.append('products.is_adult')
            if 'price_miss_count' not in cols:
                cur.execute("ALTER TABLE competitor_products "
                            "ADD COLUMN price_miss_count INTEGER DEFAULT 0")
                cur.execute("UPDATE competitor_products "
                            "SET price_miss_count = 0 "
                            "WHERE price_miss_count IS NULL")
                added.append('products.price_miss_count')
            if 'last_price_at' not in cols:
                cur.execute("ALTER TABLE competitor_products "
                            "ADD COLUMN last_price_at DATETIME")
                cur.execute("UPDATE competitor_products "
                            "SET last_price_at = last_fetched_at "
                            "WHERE current_sale_price IS NOT NULL "
                            "   OR current_price IS NOT NULL")
                added.append('products.last_price_at')

        if 'competitor_groups' in tables:
            cols = {r[1] for r in cur.execute(
                'PRAGMA table_info(competitor_groups)')}
            if 'import_requested' not in cols:
                cur.execute("ALTER TABLE competitor_groups "
                            "ADD COLUMN import_requested BOOLEAN DEFAULT 0")
                cur.execute("UPDATE competitor_groups "
                            "SET import_requested = 0 "
                            "WHERE import_requested IS NULL")
                added.append('groups.import_requested')

        conn.commit()
        logger.info('competitor v2: добавлено %s', added or 'ничего (уже применено)')
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT_DB_PATH)
    if not Path(db_path).exists():
        logger.error('БД не найдена: %s', db_path)
        return 1
    return 0 if migrate(db_path) else 1


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 4: Добавить колонки в модели**

В `models.py`, класс `CompetitorMonitorSettings` — после `pause_between_cycles_seconds` добавить (и исправить комментарий у `pause_between_cycles_seconds` на `# v1 legacy, не используется рантаймом v2`):

```python
    # v2: интервал между синками (минуты, 30..1440) и момент следующего запуска.
    # next_sync_due_at IS NULL означает «due прямо сейчас».
    sync_interval_minutes = db.Column(db.Integer, default=60)
    next_sync_due_at = db.Column(db.DateTime, nullable=True)
    # Порог алерта по изменению скидки, процентные пункты
    discount_alert_pp = db.Column(db.Float, default=5.0)
```

В `to_dict()` settings добавить ключи:

```python
            'sync_interval_minutes': self.sync_interval_minutes,
            'next_sync_due_at': self.next_sync_due_at.isoformat() if self.next_sync_due_at else None,
            'discount_alert_pp': self.discount_alert_pp,
```

В `CompetitorProduct` после `fetch_error_count` (и заменить комментарии `# цена в копейках` на `# цена в рублях (integer)` у `current_price`, `current_sale_price` — и те же комментарии в `CompetitorPriceSnapshot.price/sale_price`):

```python
    # v2: честные наблюдения
    metadata_synced_at = db.Column(db.DateTime, nullable=True)  # свежесть basket-метаданных
    is_adult = db.Column(db.Boolean, nullable=True)  # 18+ (search может фильтровать выдачу)
    price_miss_count = db.Column(db.Integer, default=0)  # подряд синков без наблюдения цены
    last_price_at = db.Column(db.DateTime, nullable=True)  # последнее успешное наблюдение цены
```

В `CompetitorProduct.to_dict()` добавить:

```python
            'last_price_at': self.last_price_at.isoformat() if self.last_price_at else None,
            'price_miss_count': self.price_miss_count,
            'is_adult': self.is_adult,
```

В `CompetitorGroup` после `auto_source_value`:

```python
    # v2: заявка на фоновый импорт каталога продавца (снимается sync-джобом)
    import_requested = db.Column(db.Boolean, default=False)
```

В `CompetitorGroup.to_dict()` добавить `'import_requested': bool(self.import_requested),`.

- [ ] **Step 5: Прогнать тесты**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_competitor_v2_migration.py && python -m py_compile migrations/migrate_competitor_monitor_v2.py models.py`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add migrations/migrate_competitor_monitor_v2.py models.py tests/test_competitor_v2_migration.py
git commit -m "feat(competitors): миграция и колонки v2 (интервал, due, честные наблюдения)"
```

---

### Task 2: Прокси как credential (шифрование + маска)

**Files:**
- Modify: `models.py` (класс `CompetitorMonitorSettings`)
- Test: `tests/test_competitor_proxy_credential.py`

**Interfaces:**
- Produces: `CompetitorProxyEncryptionError(RuntimeError)` (экспорт из `models`); `CompetitorMonitorSettings.proxy_url` — property: getter возвращает plaintext URL (расшифровка или legacy plaintext), setter шифрует fail-closed (без `ENCRYPTION_KEY` — raise), `None`/`''` очищает; `CompetitorMonitorSettings.proxy_display()` → `{'is_set': bool, 'masked': str|None, 'has_credentials': bool}`. `to_dict()` больше НЕ содержит `proxy_url`, вместо него `'proxy': self.proxy_display()`.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_competitor_proxy_credential.py
# -*- coding: utf-8 -*-
"""proxy_url конкурентов — credential: шифрование fail-closed, маска наружу."""
import os
import unittest

from cryptography.fernet import Fernet
from flask import Flask

from models import (
    CompetitorMonitorSettings, CompetitorProxyEncryptionError, Seller, User, db,
)


class CompetitorProxyTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        user = User(username='proxy-user', email='proxy@test.local')
        user.set_password('x')
        db.session.add(user)
        db.session.flush()
        self.seller = Seller(user_id=user.id, name='ProxyShop')
        db.session.add(self.seller)
        db.session.commit()
        self.key = Fernet.generate_key().decode('ascii')
        self._old_key = os.environ.get('ENCRYPTION_KEY')

    def tearDown(self):
        if self._old_key is None:
            os.environ.pop('ENCRYPTION_KEY', None)
        else:
            os.environ['ENCRYPTION_KEY'] = self._old_key
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_write_encrypts_and_read_decrypts(self):
        os.environ['ENCRYPTION_KEY'] = self.key
        s = CompetitorMonitorSettings(seller_id=self.seller.id)
        s.proxy_url = 'http://user:secret@proxy.example.com:3128'
        db.session.add(s)
        db.session.commit()
        raw = db.session.execute(db.text(
            'SELECT proxy_url FROM competitor_monitor_settings WHERE seller_id = :sid'
        ), {'sid': self.seller.id}).scalar()
        self.assertNotIn('secret', raw)
        self.assertEqual(s.proxy_url, 'http://user:secret@proxy.example.com:3128')

    def test_write_without_key_fails_closed(self):
        os.environ.pop('ENCRYPTION_KEY', None)
        s = CompetitorMonitorSettings(seller_id=self.seller.id)
        with self.assertRaises(CompetitorProxyEncryptionError):
            s.proxy_url = 'http://user:secret@proxy.example.com:3128'

    def test_legacy_plaintext_still_readable(self):
        os.environ['ENCRYPTION_KEY'] = self.key
        s = CompetitorMonitorSettings(seller_id=self.seller.id)
        db.session.add(s)
        db.session.commit()
        db.session.execute(db.text(
            "UPDATE competitor_monitor_settings SET proxy_url = :v WHERE seller_id = :sid"
        ), {'v': 'http://legacy:pw@old.example.com:8080', 'sid': self.seller.id})
        db.session.commit()
        db.session.refresh(s)
        self.assertEqual(s.proxy_url, 'http://legacy:pw@old.example.com:8080')

    def test_display_masks_credentials(self):
        os.environ['ENCRYPTION_KEY'] = self.key
        s = CompetitorMonitorSettings(seller_id=self.seller.id)
        s.proxy_url = 'http://user:secret@proxy.example.com:3128'
        d = s.proxy_display()
        self.assertTrue(d['is_set'])
        self.assertTrue(d['has_credentials'])
        self.assertEqual(d['masked'], 'http://proxy.example.com:3128')
        self.assertNotIn('secret', str(d))

    def test_to_dict_has_no_raw_proxy(self):
        os.environ['ENCRYPTION_KEY'] = self.key
        s = CompetitorMonitorSettings(seller_id=self.seller.id)
        s.proxy_url = 'http://user:secret@proxy.example.com:3128'
        data = s.to_dict()
        self.assertNotIn('proxy_url', data)
        self.assertNotIn('secret', str(data))
        self.assertEqual(data['proxy']['masked'], 'http://proxy.example.com:3128')

    def test_clear_proxy(self):
        os.environ['ENCRYPTION_KEY'] = self.key
        s = CompetitorMonitorSettings(seller_id=self.seller.id)
        s.proxy_url = 'http://proxy.example.com:3128'
        s.proxy_url = None
        self.assertIsNone(s.proxy_url)
        self.assertFalse(s.proxy_display()['is_set'])


if __name__ == '__main__':
    unittest.main()
```

Примечание: если конструктор `User`/`Seller` в репо отличается (посмотреть ближайший тест, например `tests/test_marketplace_product_links.py`, `_seller(...)`) — скопировать оттуда рабочий способ создания seller.

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_competitor_proxy_credential.py`
Expected: FAIL — `ImportError: CompetitorProxyEncryptionError`.

- [ ] **Step 3: Реализовать property в models.py**

В классе `CompetitorMonitorSettings`: переименовать колонку-атрибут и добавить property (паттерн — `Seller.wb_api_key`, models.py:103–141, но запись fail-closed). Класс исключения объявить рядом с секцией конкурентов:

```python
class CompetitorProxyEncryptionError(RuntimeError):
    """ENCRYPTION_KEY обязателен для сохранения прокси мониторинга конкурентов."""
```

В модели заменить `proxy_url = db.Column(db.String(500), nullable=True)` на:

```python
    # Прокси для обхода блокировки WB. Может содержать user:pass — это
    # credential: новая запись шифруется Fernet (fail-closed), чтение
    # поддерживает legacy plaintext. Наружу — только proxy_display().
    _proxy_url = db.Column('proxy_url', db.String(500), nullable=True)

    @property
    def proxy_url(self):
        if not self._proxy_url:
            return None
        encryption_key = os.environ.get('ENCRYPTION_KEY', '').strip()
        if not encryption_key:
            return self._proxy_url  # legacy plaintext
        try:
            f = Fernet(encryption_key.encode('ascii'))
            return f.decrypt(self._proxy_url.encode('ascii')).decode('utf-8')
        except Exception:
            return self._proxy_url  # не расшифровалось => plaintext

    @proxy_url.setter
    def proxy_url(self, value):
        if not value:
            self._proxy_url = None
            return
        encryption_key = os.environ.get('ENCRYPTION_KEY', '').strip()
        if not encryption_key:
            raise CompetitorProxyEncryptionError(
                'ENCRYPTION_KEY обязателен для сохранения прокси')
        try:
            f = Fernet(encryption_key.encode('ascii'))
        except (TypeError, ValueError):
            raise CompetitorProxyEncryptionError(
                'ENCRYPTION_KEY не является валидным Fernet-ключом') from None
        self._proxy_url = f.encrypt(value.strip().encode('utf-8')).decode('ascii')

    def proxy_display(self):
        raw = self.proxy_url
        if not raw:
            return {'is_set': False, 'masked': None, 'has_credentials': False}
        try:
            from urllib.parse import urlsplit
            parts = urlsplit(raw)
            host = parts.hostname or '***'
            port = f':{parts.port}' if parts.port else ''
            masked = f'{parts.scheme or "http"}://{host}{port}'
            return {
                'is_set': True,
                'masked': masked,
                'has_credentials': bool(parts.username or parts.password),
            }
        except (ValueError, AttributeError):
            return {'is_set': True, 'masked': '***', 'has_credentials': True}
```

`os` и `Fernet` уже импортированы в models.py (проверить шапку; если `Fernet` импортируется локально в методах — сделать так же). В `to_dict()` удалить `'proxy_url': self.proxy_url,` и добавить `'proxy': self.proxy_display(),`.

- [ ] **Step 4: Прогнать тесты**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_competitor_proxy_credential.py tests/test_competitor_v2_migration.py`
Expected: все passed.

- [ ] **Step 5: Commit**

```bash
git add models.py tests/test_competitor_proxy_credential.py
git commit -m "feat(competitors): прокси как credential — Fernet fail-closed, маска в serializer"
```

---

### Task 3: ORM-free fetch-слой `services/competitor_fetch.py`

**Files:**
- Create: `services/competitor_fetch.py`
- Test: `tests/test_competitor_fetch.py`

**Interfaces:**
- Consumes: `services.wb_api_client.RateLimiter` (конструктор `RateLimiter(max_requests=N, time_window=60)`, метод `wait_if_needed()`); `services.wb_media.wb_basket_base_url(nm_id)`, `wb_photo_url(nm_id, 1, 'big')`.
- Produces (используется Task 4/5/8):
  - `WBRateLimitedError(RuntimeError)`
  - `SOURCE_BASKET = 'basket'`, `SOURCE_CATALOG = 'catalog'`, `SOURCE_SEARCH = 'search'`
  - `get_global_rate_limiter() -> RateLimiter` (singleton, env `COMPETITOR_PUBLIC_RPM` default 20, clamp 1..60)
  - `get_source_health() -> SourceHealthRegistry`; методы `allowed(source) -> bool`, `record_success(source)`, `record_failure(source)`, `snapshot() -> dict`; 3 подряд 429/5xx → cooldown 600с
  - `get_cached_observation(nm_id) -> dict|None`, `put_cached_observation(nm_id, obs)` (кросс-селлер кэш, TTL 300с, максимум 10 000 записей)
  - `class CompetitorFetchService(proxy_url=None, session=None, rate_limiter=None, health=None)`:
    - `fetch_basket_metadata(nm_id) -> dict | 'gone' | None` — dict: `{'nm_id', 'title', 'brand', 'supplier_name', 'wb_supplier_id', 'image_url', 'is_adult', 'subject_name'}`; `'gone'` при 404; `None` при transient ошибке
    - `fetch_supplier_prices(supplier_id, target_nm_ids, max_pages=5) -> dict[int, dict]`
    - `fetch_brand_prices(brand, target_nm_ids, max_pages=2) -> dict[int, dict]`
    - `search_products(query, limit=100) -> list[dict]` — ОДНА страница; `WBRateLimitedError` при 429
    - `fetch_seller_catalog_page(supplier_id, page=1) -> list[dict]` — ОДНА страница; `WBRateLimitedError` при 429
  - observation-dict (значения в рублях): `{'price': int|None, 'sale_price': int|None, 'rating': float|None, 'feedbacks_count': int|None, 'total_stock': int|None}`; полный product-dict из search/catalog добавляет `nm_id, title, brand, supplier_name, wb_supplier_id, image_url`.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_competitor_fetch.py
# -*- coding: utf-8 -*-
"""Fetch-слой конкурентов: парсинг, health, кэш, bounded-поведение."""
import unittest
from unittest.mock import MagicMock, patch

from services import competitor_fetch as cf


def _resp(status=200, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    return r


SEARCH_PRODUCT = {
    'id': 872594650, 'name': 'Носки высокие набор', 'brand': 'OSMAN',
    'supplier': 'OSMAN', 'supplierId': 4116984,
    'reviewRating': 4.9, 'feedbacks': 1966, 'totalQuantity': 66,
    'sizes': [{'price': {'basic': 370000, 'product': 58900}}],
}


class ParserTest(unittest.TestCase):
    def test_parse_search_product_rubles(self):
        obs = cf.parse_price_observation(SEARCH_PRODUCT)
        self.assertEqual(obs['price'], 3700)       # копейки -> рубли
        self.assertEqual(obs['sale_price'], 589)
        self.assertEqual(obs['rating'], 4.9)
        self.assertEqual(obs['feedbacks_count'], 1966)
        self.assertEqual(obs['total_stock'], 66)

    def test_parse_legacy_priceu_format(self):
        obs = cf.parse_price_observation(
            {'priceU': 250000, 'salePriceU': 199000, 'sizes': []})
        self.assertEqual(obs['price'], 2500)
        self.assertEqual(obs['sale_price'], 1990)

    def test_parse_full_product(self):
        p = cf.parse_full_product(SEARCH_PRODUCT)
        self.assertEqual(p['nm_id'], 872594650)
        self.assertEqual(p['wb_supplier_id'], 4116984)
        self.assertEqual(p['sale_price'], 589)
        self.assertIn('image_url', p)


class HealthTest(unittest.TestCase):
    def test_cooldown_after_three_failures(self):
        h = cf.SourceHealthRegistry()
        self.assertTrue(h.allowed(cf.SOURCE_SEARCH))
        h.record_failure(cf.SOURCE_SEARCH)
        h.record_failure(cf.SOURCE_SEARCH)
        self.assertTrue(h.allowed(cf.SOURCE_SEARCH))
        h.record_failure(cf.SOURCE_SEARCH)
        self.assertFalse(h.allowed(cf.SOURCE_SEARCH))
        # другой источник не задет
        self.assertTrue(h.allowed(cf.SOURCE_CATALOG))

    def test_success_resets_counter(self):
        h = cf.SourceHealthRegistry()
        h.record_failure(cf.SOURCE_SEARCH)
        h.record_failure(cf.SOURCE_SEARCH)
        h.record_success(cf.SOURCE_SEARCH)
        h.record_failure(cf.SOURCE_SEARCH)
        h.record_failure(cf.SOURCE_SEARCH)
        self.assertTrue(h.allowed(cf.SOURCE_SEARCH))

    def test_cooldown_expires(self):
        h = cf.SourceHealthRegistry()
        for _ in range(3):
            h.record_failure(cf.SOURCE_CATALOG)
        with patch.object(cf.time, 'time', return_value=cf.time.time() + 601):
            self.assertTrue(h.allowed(cf.SOURCE_CATALOG))


class CacheTest(unittest.TestCase):
    def setUp(self):
        cf.clear_observation_cache()

    def test_roundtrip_and_ttl(self):
        cf.put_cached_observation(1, {'sale_price': 100})
        self.assertEqual(cf.get_cached_observation(1)['sale_price'], 100)
        with patch.object(cf.time, 'time', return_value=cf.time.time() + 301):
            self.assertIsNone(cf.get_cached_observation(1))

    def test_bounded_size(self):
        for i in range(cf.CACHE_MAX_ENTRIES + 100):
            cf.put_cached_observation(i, {'sale_price': i})
        # кэш не растёт бесконечно
        self.assertLessEqual(len(cf._observation_cache), cf.CACHE_MAX_ENTRIES)


class FetchServiceTest(unittest.TestCase):
    def _service(self, session):
        limiter = MagicMock()
        health = cf.SourceHealthRegistry()
        return cf.CompetitorFetchService(
            session=session, rate_limiter=limiter, health=health), limiter, health

    def test_basket_metadata_ok(self):
        session = MagicMock()
        session.get.side_effect = [
            _resp(200, {'imt_name': 'Вибратор', 'subj_name': 'Вибраторы',
                        'selling': {'brand_name': 'JOS', 'supplier_id': 332183,
                                    'is_adult': True}}),
            _resp(200, {'supplierName': 'MAGIC TOYS', 'supplierId': 332183}),
        ]
        svc, limiter, _ = self._service(session)
        with patch.object(cf, '_basket_base_url', return_value='https://basket-05.wbbasket.ru/vol807/part80786/80786423'):
            meta = svc.fetch_basket_metadata(80786423)
        self.assertEqual(meta['title'], 'Вибратор')
        self.assertEqual(meta['wb_supplier_id'], 332183)
        self.assertEqual(meta['supplier_name'], 'MAGIC TOYS')
        self.assertTrue(meta['is_adult'])
        # basket — тоже через глобальный limiter
        self.assertTrue(limiter.wait_if_needed.called)

    def test_basket_404_means_gone(self):
        session = MagicMock()
        session.get.return_value = _resp(404)
        svc, _, _ = self._service(session)
        with patch.object(cf, '_basket_base_url', return_value='https://basket-05.wbbasket.ru/x'):
            self.assertEqual(svc.fetch_basket_metadata(80786423), 'gone')

    def test_search_products_single_page_raises_on_429(self):
        session = MagicMock()
        session.get.return_value = _resp(429)
        svc, _, health = self._service(session)
        with self.assertRaises(cf.WBRateLimitedError):
            svc.search_products('носки')
        # 429 зафиксирован в health
        self.assertEqual(health.snapshot()[cf.SOURCE_SEARCH]['consecutive_failures'], 1)
        # ровно один HTTP-вызов, никакой пагинации/sleep
        self.assertEqual(session.get.call_count, 1)

    def test_supplier_prices_collects_targets_and_stops(self):
        page1 = {'data': {'products': [
            dict(SEARCH_PRODUCT, id=111), dict(SEARCH_PRODUCT, id=222)]}}
        session = MagicMock()
        session.get.return_value = _resp(200, page1)
        svc, _, _ = self._service(session)
        found = svc.fetch_supplier_prices(4116984, {111, 222})
        self.assertEqual(set(found.keys()), {111, 222})
        # все цели найдены на первой странице — вторая не запрашивается
        self.assertEqual(session.get.call_count, 1)

    def test_supplier_prices_429_stops_without_sleep(self):
        session = MagicMock()
        session.get.return_value = _resp(429)
        svc, _, health = self._service(session)
        found = svc.fetch_supplier_prices(4116984, {111})
        self.assertEqual(found, {})
        self.assertEqual(session.get.call_count, 1)

    def test_source_in_cooldown_skipped(self):
        session = MagicMock()
        svc, _, health = self._service(session)
        for _ in range(3):
            health.record_failure(cf.SOURCE_CATALOG)
        found = svc.fetch_supplier_prices(4116984, {111})
        self.assertEqual(found, {})
        session.get.assert_not_called()


class GlobalLimiterTest(unittest.TestCase):
    def test_env_clamped(self):
        with patch.dict(cf.os.environ, {'COMPETITOR_PUBLIC_RPM': '500'}):
            self.assertEqual(cf._resolve_public_rpm(), 60)
        with patch.dict(cf.os.environ, {'COMPETITOR_PUBLIC_RPM': '0'}):
            self.assertEqual(cf._resolve_public_rpm(), 1)
        with patch.dict(cf.os.environ, {'COMPETITOR_PUBLIC_RPM': 'мусор'}):
            self.assertEqual(cf._resolve_public_rpm(), 20)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_competitor_fetch.py`
Expected: FAIL — `ModuleNotFoundError: services.competitor_fetch`.

- [ ] **Step 3: Реализовать `services/competitor_fetch.py`**

```python
# services/competitor_fetch.py
# -*- coding: utf-8 -*-
"""
ORM-free fetch-слой мониторинга конкурентов (публичные API WB).

Источники:
- basket CDN (wbbasket.ru card.json/sellers.json) — метаданные; без жёсткого
  IP-лимита, но всё равно проходит общий rate limiter;
- catalog.wb.ru/sellers/catalog — цены каталога продавца (основной);
- search.wb.ru exactmatch v18 — цены через поиск по бренду (fallback) и
  интерактивный поиск.

Правила устойчивости:
- ОДИН глобальный process-wide rate limiter на все публичные вызовы всех
  продавцов (бюджет WB per-IP): env COMPETITOR_PUBLIC_RPM, default 20;
- circuit breaker на источник: 3 подряд 429/5xx -> cooldown 10 минут;
- 429 никогда не ждётся sleep-ом: источник немедленно завершает работу
  в текущем проходе (WBRateLimitedError для интерактивных вызовов);
- кросс-селлер кэш наблюдений: TTL 300с, максимум 10 000 записей.

Цены наружу — В РУБЛЯХ (integer): WB отдаёт копейки, здесь делим на 100.
"""
import logging
import os
import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from services.wb_api_client import RateLimiter

logger = logging.getLogger(__name__)

SOURCE_BASKET = 'basket'
SOURCE_CATALOG = 'catalog'
SOURCE_SEARCH = 'search'

SEARCH_URL = 'https://search.wb.ru/exactmatch/ru/common/v18/search'
CATALOG_URL = 'https://catalog.wb.ru/sellers/catalog'
DEFAULT_PARAMS = {'appType': '1', 'curr': 'rub', 'dest': '-1257786', 'lang': 'ru'}
HTTP_TIMEOUT = 10

CACHE_TTL_SECONDS = 300
CACHE_MAX_ENTRIES = 10_000

HEALTH_FAILURES_TO_COOLDOWN = 3
HEALTH_COOLDOWN_SECONDS = 600

DEFAULT_PUBLIC_RPM = 20


class WBRateLimitedError(RuntimeError):
    """WB вернул 429 — публичный IP-бюджет исчерпан."""


def _resolve_public_rpm():
    raw = os.environ.get('COMPETITOR_PUBLIC_RPM', '')
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PUBLIC_RPM
    return max(1, min(60, value))


_rate_limiter = None
_rate_limiter_lock = threading.Lock()


def get_global_rate_limiter():
    global _rate_limiter
    with _rate_limiter_lock:
        if _rate_limiter is None:
            _rate_limiter = RateLimiter(
                max_requests=_resolve_public_rpm(), time_window=60)
        return _rate_limiter


class SourceHealthRegistry:
    """Circuit breaker per источник: N подряд 429/5xx -> cooldown."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state = {}  # source -> {'consecutive_failures': int, 'cooldown_until': float}

    def _entry(self, source):
        return self._state.setdefault(
            source, {'consecutive_failures': 0, 'cooldown_until': 0.0})

    def allowed(self, source):
        with self._lock:
            return time.time() >= self._entry(source)['cooldown_until']

    def record_success(self, source):
        with self._lock:
            entry = self._entry(source)
            entry['consecutive_failures'] = 0
            entry['cooldown_until'] = 0.0

    def record_failure(self, source):
        with self._lock:
            entry = self._entry(source)
            entry['consecutive_failures'] += 1
            if entry['consecutive_failures'] >= HEALTH_FAILURES_TO_COOLDOWN:
                entry['cooldown_until'] = time.time() + HEALTH_COOLDOWN_SECONDS
                logger.warning(
                    'Источник %s уходит в cooldown на %sс после %s ошибок подряд',
                    source, HEALTH_COOLDOWN_SECONDS, entry['consecutive_failures'])

    def snapshot(self):
        with self._lock:
            return {k: dict(v) for k, v in self._state.items()}


_source_health = SourceHealthRegistry()


def get_source_health():
    return _source_health


# Кросс-селлер кэш наблюдений: {nm_id: (obs, ts)}
_observation_cache = {}
_cache_lock = threading.Lock()


def get_cached_observation(nm_id):
    now = time.time()
    with _cache_lock:
        entry = _observation_cache.get(nm_id)
        if entry and (now - entry[1]) < CACHE_TTL_SECONDS:
            return entry[0]
        if entry:
            _observation_cache.pop(nm_id, None)
    return None


def put_cached_observation(nm_id, obs):
    now = time.time()
    with _cache_lock:
        if len(_observation_cache) >= CACHE_MAX_ENTRIES:
            # грубая очистка: выкинуть протухшие, при нехватке — старейшие
            expired = [k for k, v in _observation_cache.items()
                       if (now - v[1]) >= CACHE_TTL_SECONDS]
            for k in expired:
                _observation_cache.pop(k, None)
            while len(_observation_cache) >= CACHE_MAX_ENTRIES:
                oldest = min(_observation_cache, key=lambda k: _observation_cache[k][1])
                _observation_cache.pop(oldest, None)
        _observation_cache[nm_id] = (obs, now)


def clear_observation_cache():
    with _cache_lock:
        _observation_cache.clear()


def _basket_base_url(nm_id):
    from services.wb_media import wb_basket_base_url
    return wb_basket_base_url(nm_id)


def _image_url(nm_id):
    from services.wb_media import wb_photo_url
    return wb_photo_url(nm_id, 1, 'big')


def parse_price_observation(raw):
    """Наблюдение цены/остатка/рейтинга из элемента search/catalog. Рубли."""
    sizes = raw.get('sizes') or []
    price = None
    sale_price = None
    total_stock = raw.get('totalQuantity', 0) or 0

    if sizes:
        price_obj = sizes[0].get('price') or {}
        if price_obj.get('basic'):
            price = price_obj['basic'] // 100
        if price_obj.get('product'):
            sale_price = price_obj['product'] // 100

    if price is None and raw.get('priceU'):
        price = raw['priceU'] // 100
    if sale_price is None and raw.get('salePriceU'):
        sale_price = raw['salePriceU'] // 100

    if total_stock == 0 and sizes:
        for s in sizes:
            for stock in s.get('stocks') or []:
                total_stock += stock.get('qty', 0) or 0

    return {
        'price': price,
        'sale_price': sale_price,
        'rating': raw.get('reviewRating') or None,
        'feedbacks_count': raw.get('feedbacks') or 0,
        'total_stock': total_stock,
    }


def parse_full_product(raw):
    """Полная карточка из search/catalog: метаданные + наблюдение."""
    nm_id = raw.get('id', 0)
    result = {
        'nm_id': nm_id,
        'title': raw.get('name', ''),
        'brand': raw.get('brand', ''),
        'supplier_name': raw.get('supplier', ''),
        'wb_supplier_id': raw.get('supplierId'),
        'image_url': _image_url(nm_id) if nm_id else None,
    }
    result.update(parse_price_observation(raw))
    return result


class CompetitorFetchService:
    """HTTP-клиент публичных WB-источников. Без ORM, без sleep на 429."""

    def __init__(self, proxy_url=None, session=None, rate_limiter=None, health=None):
        self._session = session or self._create_session(proxy_url)
        self._rate_limiter = rate_limiter or get_global_rate_limiter()
        self._health = health or get_source_health()

    @staticmethod
    def _create_session(proxy_url):
        session = requests.Session()
        retry = Retry(total=1, backoff_factor=0.3,
                      status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
        session.mount('https://', adapter)
        session.headers.update({
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/131.0.0.0 Safari/537.36'),
            'Accept': 'application/json',
            'Accept-Language': 'ru-RU,ru;q=0.9',
        })
        if proxy_url:
            session.proxies = {'http': proxy_url, 'https': proxy_url}
        return session

    # ---------- метаданные (basket CDN) ----------

    def fetch_basket_metadata(self, nm_id):
        """dict | 'gone' (404 — товар удалён с WB) | None (transient)."""
        base_url = _basket_base_url(nm_id)
        self._rate_limiter.wait_if_needed()
        try:
            response = self._session.get(
                f'{base_url}/info/ru/card.json', timeout=HTTP_TIMEOUT)
        except requests.exceptions.RequestException as e:
            logger.warning('Товар %s: basket card.json недоступен: %s', nm_id, e)
            return None
        if response.status_code == 404:
            return 'gone'
        if response.status_code != 200:
            logger.warning('Товар %s: card.json -> %s', nm_id, response.status_code)
            return None
        try:
            card = response.json()
        except ValueError:
            return None

        seller_data = None
        try:
            self._rate_limiter.wait_if_needed()
            sellers_resp = self._session.get(
                f'{base_url}/info/sellers.json', timeout=HTTP_TIMEOUT)
            if sellers_resp.status_code == 200:
                seller_data = sellers_resp.json()
        except (requests.exceptions.RequestException, ValueError):
            pass

        selling = card.get('selling') or {}
        return {
            'nm_id': nm_id,
            'title': card.get('imt_name', ''),
            'brand': selling.get('brand_name', ''),
            'supplier_name': (
                (seller_data or {}).get('supplierName')
                or selling.get('brand_name', '')),
            'wb_supplier_id': (
                selling.get('supplier_id')
                or (seller_data or {}).get('supplierId')),
            'image_url': _image_url(nm_id),
            'is_adult': bool(selling.get('is_adult', False)),
            'subject_name': card.get('subj_name', ''),
        }

    # ---------- цены ----------

    def _bounded_get(self, source, url, params):
        """Один HTTP GET с health/limiter. 429 -> record_failure + WBRateLimitedError."""
        if not self._health.allowed(source):
            raise WBRateLimitedError(f'{source} в cooldown')
        self._rate_limiter.wait_if_needed()
        try:
            response = self._session.get(url, params=params, timeout=HTTP_TIMEOUT)
        except requests.exceptions.RequestException as e:
            self._health.record_failure(source)
            raise WBRateLimitedError(f'{source}: transport error: {e}') from e
        if response.status_code == 429:
            self._health.record_failure(source)
            raise WBRateLimitedError(f'{source}: 429')
        if response.status_code >= 500:
            self._health.record_failure(source)
            raise WBRateLimitedError(f'{source}: {response.status_code}')
        if response.status_code != 200:
            # 403/404 и прочее: не считаем поломкой источника, но и данных нет
            return None
        self._health.record_success(source)
        try:
            return response.json()
        except ValueError:
            self._health.record_failure(source)
            return None

    @staticmethod
    def _extract_products(data):
        if not isinstance(data, dict):
            return []
        products = data.get('products')
        if not products:
            products = (data.get('data') or {}).get('products')
        return products or []

    def fetch_supplier_prices(self, supplier_id, target_nm_ids, max_pages=5):
        """Цены товаров из каталога продавца. Молча останавливается на 429."""
        found = {}
        remaining = set(target_nm_ids)
        for page in range(1, max_pages + 1):
            if not remaining:
                break
            params = {
                'appType': '1', 'curr': 'rub', 'dest': '-1257786',
                'supplier': str(supplier_id), 'sort': 'popular',
                'page': str(page), 'limit': '100',
            }
            try:
                data = self._bounded_get(SOURCE_CATALOG, CATALOG_URL, params)
            except WBRateLimitedError:
                break
            if data is None:
                break
            products = self._extract_products(data)
            if not products:
                break
            for p in products:
                pid = p.get('id')
                if pid in remaining:
                    found[pid] = parse_price_observation(p)
                    remaining.discard(pid)
        return found

    def fetch_brand_prices(self, brand, target_nm_ids, max_pages=2):
        """Цены через search по бренду (fallback). Молча останавливается на 429."""
        found = {}
        remaining = set(target_nm_ids)
        for page in range(1, max_pages + 1):
            if not remaining:
                break
            params = {
                **DEFAULT_PARAMS, 'query': brand, 'resultset': 'catalog',
                'sort': 'popular', 'spp': '30', 'page': str(page),
            }
            try:
                data = self._bounded_get(SOURCE_SEARCH, SEARCH_URL, params)
            except WBRateLimitedError:
                break
            if data is None:
                break
            products = self._extract_products(data)
            if not products:
                break
            for p in products:
                pid = p.get('id')
                if pid in remaining:
                    found[pid] = parse_price_observation(p)
                    remaining.discard(pid)
        return found

    # ---------- интерактивные (bounded, 1 страница) ----------

    def search_products(self, query, limit=100):
        """Одна страница поиска. WBRateLimitedError при 429 — наружу."""
        params = {
            **DEFAULT_PARAMS, 'query': query, 'resultset': 'catalog',
            'sort': 'popular', 'spp': '30',
        }
        data = self._bounded_get(SOURCE_SEARCH, SEARCH_URL, params)
        if data is None:
            return []
        return [parse_full_product(p)
                for p in self._extract_products(data)[:limit]]

    def fetch_seller_catalog_page(self, supplier_id, page=1):
        """Одна страница каталога продавца. WBRateLimitedError при 429."""
        params = {
            'appType': '1', 'curr': 'rub', 'dest': '-1257786',
            'supplier': str(supplier_id), 'sort': 'popular',
            'page': str(page), 'limit': '100',
        }
        data = self._bounded_get(SOURCE_CATALOG, CATALOG_URL, params)
        if data is None:
            return []
        return [parse_full_product(p) for p in self._extract_products(data)]
```

- [ ] **Step 4: Прогнать тесты**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_competitor_fetch.py`
Expected: все passed. Если тест `test_basket_metadata_ok` падает на подсчёте limiter — проверить, что `fetch_basket_metadata` вызывает `wait_if_needed` до card.json.

- [ ] **Step 5: Commit**

```bash
git add services/competitor_fetch.py tests/test_competitor_fetch.py
git commit -m "feat(competitors): ORM-free fetch-слой — глобальный limiter, circuit breaker, bounded вызовы"
```

---

### Task 4: Sync-ядро — политика наблюдений, снимков и алертов

**Files:**
- Modify: `services/competitor_monitor.py` — ПОЛНАЯ перезапись (старое содержимое удаляется целиком: треды, `_monitor_threads`, `_stop_events`, `start/stop_competitor_monitor_loop`, `check_and_restart_monitor_loops`, `stop_all_monitor_loops`, `CompetitorMonitorService` v1)
- Test: `tests/test_competitor_sync.py`

**Interfaces:**
- Consumes: `services.competitor_fetch` (Task 3), модели (Task 1/2).
- Produces (используется Task 5/7/8):
  - `normalize_sync_interval_minutes(value) -> int` (30..1440, default 60)
  - `sync_seller_competitors(seller_id, flask_app, fetch_service=None, now=None) -> dict` — результат `{'status': 'ok'|'partial'|'no_products'|'disabled', 'observed': int, 'misses': int, 'snapshots': int, 'alerts': int, 'deactivated': int}`
  - константы: `MAX_PRODUCTS_HARD_CAP = 1000`, `METADATA_REFRESH_DAYS = 7`, `METADATA_BATCH_PER_SYNC = 50`, `PRICE_MISS_RECHECK_THRESHOLD = 3`, `DEACTIVATE_AFTER_GONE = 20`, `SYNC_WALL_CLOCK_BUDGET_SECONDS = 90`, `SUPPLIER_PAGES_PER_SYNC = 5`, `BRAND_PAGES_PER_SYNC = 2`
  - `_generate_alerts(product, obs, settings) -> list[CompetitorAlert]` (модульная функция, тестируется напрямую)

**Политика (ядро задачи):**
1. Наблюдение цены есть (`sale_price` или `price` не None): обновить `current_*`, `last_price_at=now`, `price_miss_count=0`; снимок — только если значения изменились против текущих или это первое наблюдение (`current_price is None and current_sale_price is None`).
2. Товар не найден ни одним источником: `price_miss_count += 1`; `current_*` НЕ трогаются, снимок НЕ пишется.
3. `price_miss_count >= PRICE_MISS_RECHECK_THRESHOLD` → на следующем sync товар попадает в metadata-recheck; basket 404 (`'gone'`) → `fetch_error_count += 1`; `fetch_error_count >= DEACTIVATE_AFTER_GONE` → `is_active=False`. Любой не-404 ответ basket сбрасывает `fetch_error_count=0`.
4. Метаданные обновляются для товаров с `metadata_synced_at IS NULL` или старше `METADATA_REFRESH_DAYS`, максимум `METADATA_BATCH_PER_SYNC` за sync.
5. Сначала ВСЯ сеть (metadata + supplier prices + brand prices, с учётом кэша наблюдений), затем ОДИН write-проход: `begin_nested()` на товар, общий commit в конце + обновление settings.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_competitor_sync.py
# -*- coding: utf-8 -*-
"""Sync-ядро v2: честные наблюдения, снимки только при изменении, алерты."""
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from flask import Flask

from models import (
    CompetitorAlert, CompetitorGroup, CompetitorMonitorSettings,
    CompetitorPriceSnapshot, CompetitorProduct, Seller, User, db,
)
from services import competitor_monitor as cm


class SyncTestBase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        user = User(username='sync-user', email='sync@test.local')
        user.set_password('x')
        db.session.add(user)
        db.session.flush()
        self.seller = Seller(user_id=user.id, name='SyncShop')
        db.session.add(self.seller)
        db.session.flush()
        self.settings = CompetitorMonitorSettings(
            seller_id=self.seller.id, is_enabled=True,
            price_change_alert_percent=5.0, discount_alert_pp=5.0,
            sync_interval_minutes=60)
        self.group = CompetitorGroup(seller_id=self.seller.id, name='G1')
        db.session.add_all([self.settings, self.group])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _product(self, nm_id=111, **kwargs):
        defaults = dict(
            seller_id=self.seller.id, group_id=self.group.id, nm_id=nm_id,
            title='Товар', brand='BrandX', wb_supplier_id=500,
            metadata_synced_at=datetime.utcnow(),
            current_price=None, current_sale_price=None,
            price_miss_count=0, fetch_error_count=0)
        defaults.update(kwargs)
        p = CompetitorProduct(**defaults)
        db.session.add(p)
        db.session.commit()
        return p

    def _fetch_mock(self, supplier_prices=None, brand_prices=None, metadata=None):
        svc = MagicMock()
        svc.fetch_supplier_prices.return_value = supplier_prices or {}
        svc.fetch_brand_prices.return_value = brand_prices or {}
        svc.fetch_basket_metadata.side_effect = (
            lambda nm: (metadata or {}).get(nm))
        svc.fetch_seller_catalog_page.return_value = []
        return svc

    def _obs(self, price=2000, sale=1500, stock=10, rating=4.5):
        return {'price': price, 'sale_price': sale, 'rating': rating,
                'feedbacks_count': 5, 'total_stock': stock}


class ObservationPolicyTest(SyncTestBase):
    def test_first_observation_creates_snapshot(self):
        p = self._product()
        svc = self._fetch_mock(supplier_prices={111: self._obs()})
        result = cm.sync_seller_competitors(self.seller.id, self.app, fetch_service=svc)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['snapshots'], 1)
        db.session.refresh(p)
        self.assertEqual(p.current_sale_price, 1500)
        self.assertEqual(p.price_miss_count, 0)
        self.assertIsNotNone(p.last_price_at)

    def test_unchanged_observation_no_snapshot(self):
        p = self._product(current_price=2000, current_sale_price=1500,
                          current_total_stock=10, current_rating=4.5,
                          last_price_at=datetime.utcnow())
        svc = self._fetch_mock(supplier_prices={111: self._obs()})
        result = cm.sync_seller_competitors(self.seller.id, self.app, fetch_service=svc)
        self.assertEqual(result['snapshots'], 0)
        self.assertEqual(CompetitorPriceSnapshot.query.count(), 0)

    def test_miss_does_not_null_current_and_no_snapshot(self):
        p = self._product(current_price=2000, current_sale_price=1500,
                          last_price_at=datetime.utcnow() - timedelta(hours=2))
        svc = self._fetch_mock()  # источники ничего не вернули
        result = cm.sync_seller_competitors(self.seller.id, self.app, fetch_service=svc)
        db.session.refresh(p)
        self.assertEqual(p.current_sale_price, 1500)      # не затёрто
        self.assertEqual(p.price_miss_count, 1)
        self.assertEqual(CompetitorPriceSnapshot.query.count(), 0)
        self.assertEqual(result['misses'], 1)

    def test_changed_price_snapshot_and_alert(self):
        p = self._product(current_price=2000, current_sale_price=1500,
                          current_total_stock=10, current_rating=4.5)
        svc = self._fetch_mock(supplier_prices={111: self._obs(sale=1200)})  # -20%
        result = cm.sync_seller_competitors(self.seller.id, self.app, fetch_service=svc)
        self.assertEqual(result['snapshots'], 1)
        alerts = CompetitorAlert.query.all()
        self.assertTrue(any(a.alert_type == 'price_drop' for a in alerts))
        snap = CompetitorPriceSnapshot.query.one()
        self.assertEqual(snap.sale_price, 1200)
        self.assertAlmostEqual(snap.price_change_percent, -20.0)

    def test_gone_product_deactivates_after_threshold(self):
        p = self._product(price_miss_count=cm.PRICE_MISS_RECHECK_THRESHOLD,
                          fetch_error_count=cm.DEACTIVATE_AFTER_GONE - 1)
        svc = self._fetch_mock(metadata={111: 'gone'})
        cm.sync_seller_competitors(self.seller.id, self.app, fetch_service=svc)
        db.session.refresh(p)
        self.assertFalse(p.is_active)

    def test_stale_metadata_refreshed(self):
        p = self._product(metadata_synced_at=datetime.utcnow() - timedelta(days=8))
        meta = {'nm_id': 111, 'title': 'Новое имя', 'brand': 'B', 'supplier_name': 'S',
                'wb_supplier_id': 500, 'image_url': 'http://x', 'is_adult': True,
                'subject_name': 'Категория'}
        svc = self._fetch_mock(metadata={111: meta},
                               supplier_prices={111: self._obs()})
        cm.sync_seller_competitors(self.seller.id, self.app, fetch_service=svc)
        db.session.refresh(p)
        self.assertEqual(p.title, 'Новое имя')
        self.assertTrue(p.is_adult)
        self.assertIsNotNone(p.metadata_synced_at)

    def test_settings_updated_and_next_due_scheduled(self):
        self._product()
        svc = self._fetch_mock(supplier_prices={111: self._obs()})
        before = datetime.utcnow()
        cm.sync_seller_competitors(self.seller.id, self.app, fetch_service=svc)
        db.session.refresh(self.settings)
        self.assertEqual(self.settings.last_sync_status, 'success')
        self.assertFalse(self.settings.is_running)
        self.assertGreaterEqual(
            self.settings.next_sync_due_at,
            before + timedelta(minutes=59))

    def test_disabled_seller_skipped(self):
        self.settings.is_enabled = False
        db.session.commit()
        svc = self._fetch_mock()
        result = cm.sync_seller_competitors(self.seller.id, self.app, fetch_service=svc)
        self.assertEqual(result['status'], 'disabled')
        svc.fetch_supplier_prices.assert_not_called()


class AlertRulesTest(SyncTestBase):
    def test_discount_alert_pp_threshold(self):
        p = self._product(current_price=2000, current_sale_price=1800)  # скидка 10%
        obs = self._obs(price=2000, sale=1500)  # скидка 25% => +15 п.п.
        alerts = cm._generate_alerts(p, obs, self.settings)
        self.assertTrue(any(a.alert_type == 'discount_increase' for a in alerts))

    def test_out_of_stock_and_back(self):
        p = self._product(current_total_stock=5, current_price=100, current_sale_price=90)
        alerts = cm._generate_alerts(
            p, self._obs(price=100, sale=90, stock=0), self.settings)
        self.assertTrue(any(a.alert_type == 'out_of_stock' for a in alerts))
        p.current_total_stock = 0
        alerts = cm._generate_alerts(
            p, self._obs(price=100, sale=90, stock=7), self.settings)
        self.assertTrue(any(a.alert_type == 'back_in_stock' for a in alerts))

    def test_below_threshold_no_alert(self):
        p = self._product(current_price=2000, current_sale_price=1500)
        alerts = cm._generate_alerts(
            p, self._obs(price=2000, sale=1470), self.settings)  # -2%
        self.assertEqual([a for a in alerts if 'price' in a.alert_type], [])


class NormalizeIntervalTest(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual(cm.normalize_sync_interval_minutes(60), 60)
        self.assertEqual(cm.normalize_sync_interval_minutes(5), 30)
        self.assertEqual(cm.normalize_sync_interval_minutes(999999), 1440)
        self.assertEqual(cm.normalize_sync_interval_minutes('мусор'), 60)
        self.assertEqual(cm.normalize_sync_interval_minutes(None), 60)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_competitor_sync.py`
Expected: FAIL — у `competitor_monitor` нет `sync_seller_competitors` (модульной) / `normalize_sync_interval_minutes` с новой семантикой.

- [ ] **Step 3: Переписать `services/competitor_monitor.py`**

Полное новое содержимое (Task 5 добавит tick/notification/import; здесь — ядро):

```python
# services/competitor_monitor.py
# -*- coding: utf-8 -*-
"""
Мониторинг конкурентов v2: bounded-синхронизация через singleton scheduler.

Архитектура:
- никакого собственного threading: run_competitor_monitor_tick() вызывается
  scheduler-джобом раз в минуту и обрабатывает до 2 due-продавцов;
- фазирование: сначала ВСЯ сеть (fetch), затем один write-проход
  (savepoint на товар, общий commit) — SQLite-инвариант;
- честные наблюдения: fetch-miss не затирает current_* и не создаёт снимок;
- снимок только при успешном наблюдении с фактическим изменением;
- деактивация только по доказанному basket 404 (товар удалён с WB).

HTTP-слой — services/competitor_fetch.py (глобальный rate limiter,
circuit breaker, кросс-селлер кэш).
"""
import logging
import time
from datetime import datetime, timedelta

from services.competitor_fetch import (
    CompetitorFetchService, WBRateLimitedError,
    get_cached_observation, put_cached_observation,
)

logger = logging.getLogger(__name__)

MIN_SYNC_INTERVAL_MINUTES = 30
MAX_SYNC_INTERVAL_MINUTES = 1440
DEFAULT_SYNC_INTERVAL_MINUTES = 60

MAX_PRODUCTS_HARD_CAP = 1000
METADATA_REFRESH_DAYS = 7
METADATA_BATCH_PER_SYNC = 50
PRICE_MISS_RECHECK_THRESHOLD = 3
DEACTIVATE_AFTER_GONE = 20
SYNC_WALL_CLOCK_BUDGET_SECONDS = 90
SUPPLIER_PAGES_PER_SYNC = 5
BRAND_PAGES_PER_SYNC = 2


def normalize_sync_interval_minutes(value):
    """Интервал между синками продавца: 30..1440 минут, default 60."""
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = DEFAULT_SYNC_INTERVAL_MINUTES
    return max(MIN_SYNC_INTERVAL_MINUTES,
               min(MAX_SYNC_INTERVAL_MINUTES, minutes))


def _generate_alerts(product, obs, settings):
    """Алерты по успешному наблюдению против current_* товара."""
    from models import CompetitorAlert

    alerts = []
    threshold = settings.price_change_alert_percent or 5.0
    discount_pp = settings.discount_alert_pp or 5.0

    old_sale = product.current_sale_price
    new_sale = obs.get('sale_price')
    if old_sale and new_sale and old_sale > 0:
        change_pct = round((new_sale - old_sale) / old_sale * 100, 2)
        if abs(change_pct) >= threshold:
            alert_type = 'price_drop' if change_pct < 0 else 'price_increase'
            severity = 'critical' if abs(change_pct) >= threshold * 2 else 'warning'
            alerts.append(CompetitorAlert(
                seller_id=product.seller_id, product_id=product.id,
                group_id=product.group_id, alert_type=alert_type,
                severity=severity, old_value=old_sale, new_value=new_sale,
                change_percent=change_pct,
                message=(f'{product.title or product.nm_id}: цена '
                         f'{"снизилась" if change_pct < 0 else "выросла"} '
                         f'на {abs(change_pct):.1f}% ({old_sale} → {new_sale} ₽)')))

    old_discount = None
    new_discount = None
    if product.current_price and old_sale and product.current_price > 0:
        old_discount = round((1 - old_sale / product.current_price) * 100, 1)
    if obs.get('price') and new_sale and obs['price'] > 0:
        new_discount = round((1 - new_sale / obs['price']) * 100, 1)
    if old_discount is not None and new_discount is not None:
        delta = round(new_discount - old_discount, 1)
        if abs(delta) >= discount_pp:
            alerts.append(CompetitorAlert(
                seller_id=product.seller_id, product_id=product.id,
                group_id=product.group_id,
                alert_type='discount_increase' if delta > 0 else 'discount_decrease',
                severity='warning' if abs(delta) >= discount_pp * 2 else 'info',
                old_value=old_discount, new_value=new_discount,
                change_percent=delta,
                message=(f'{product.title or product.nm_id}: скидка '
                         f'{"выросла" if delta > 0 else "уменьшилась"} '
                         f'на {abs(delta):.0f} п.п. '
                         f'({old_discount:.0f}% → {new_discount:.0f}%)')))

    old_stock = product.current_total_stock
    new_stock = obs.get('total_stock')
    if old_stock and old_stock > 0 and new_stock == 0:
        alerts.append(CompetitorAlert(
            seller_id=product.seller_id, product_id=product.id,
            group_id=product.group_id, alert_type='out_of_stock',
            severity='info', old_value=old_stock, new_value=0,
            message=(f'{product.title or product.nm_id}: товар закончился '
                     f'(было {old_stock} шт.)')))
    if old_stock is not None and old_stock == 0 and (new_stock or 0) > 0:
        alerts.append(CompetitorAlert(
            seller_id=product.seller_id, product_id=product.id,
            group_id=product.group_id, alert_type='back_in_stock',
            severity='info', old_value=0, new_value=new_stock,
            message=(f'{product.title or product.nm_id}: снова в наличии '
                     f'({new_stock} шт.)')))
    return alerts


def _observation_changed(product, obs):
    return (
        product.current_price != obs.get('price')
        or product.current_sale_price != obs.get('sale_price')
        or product.current_total_stock != obs.get('total_stock')
        or product.current_rating != obs.get('rating')
    )


def sync_seller_competitors(seller_id, flask_app, fetch_service=None, now=None):
    """Один bounded sync продавца. Возвращает summary-dict."""
    from models import (
        db, CompetitorMonitorSettings, CompetitorPriceSnapshot,
        CompetitorProduct,
    )

    started = time.time()
    now = now or datetime.utcnow()
    result = {'status': 'ok', 'observed': 0, 'misses': 0,
              'snapshots': 0, 'alerts': 0, 'deactivated': 0}

    with flask_app.app_context():
        settings = CompetitorMonitorSettings.query.filter_by(
            seller_id=seller_id).first()
        if not settings or not settings.is_enabled:
            result['status'] = 'disabled'
            return result

        settings.is_running = True
        settings.last_sync_status = 'running'
        db.session.commit()

        try:
            service = fetch_service or CompetitorFetchService(
                proxy_url=settings.proxy_url)

            limit = min(settings.max_products or MAX_PRODUCTS_HARD_CAP,
                        MAX_PRODUCTS_HARD_CAP)
            products = CompetitorProduct.query.filter_by(
                seller_id=seller_id, is_active=True,
            ).order_by(
                CompetitorProduct.priority.asc(),
                CompetitorProduct.last_fetched_at.asc().nullsfirst(),
            ).limit(limit).all()

            if not products:
                settings.last_sync_at = now
                settings.last_sync_status = 'idle'
                settings.total_products_monitored = 0
                settings.next_sync_due_at = now + timedelta(
                    minutes=normalize_sync_interval_minutes(
                        settings.sync_interval_minutes))
                settings.is_running = False
                db.session.commit()
                result['status'] = 'no_products'
                return result

            # ---------- Фаза A: сеть (write-транзакция не открыта) ----------
            def out_of_budget():
                return (time.time() - started) > SYNC_WALL_CLOCK_BUDGET_SECONDS

            # A1: метаданные — отсутствующие/протухшие/подозрительные на gone
            metadata_cutoff = now - timedelta(days=METADATA_REFRESH_DAYS)
            meta_targets = [
                p for p in products
                if p.metadata_synced_at is None
                or p.metadata_synced_at < metadata_cutoff
                or p.price_miss_count >= PRICE_MISS_RECHECK_THRESHOLD
            ][:METADATA_BATCH_PER_SYNC]
            meta_results = {}
            for p in meta_targets:
                if out_of_budget():
                    result['status'] = 'partial'
                    break
                meta_results[p.nm_id] = service.fetch_basket_metadata(p.nm_id)

            # A2: цены — кэш, затем каталог продавца, затем search по бренду
            observations = {}
            uncached = []
            for p in products:
                cached = get_cached_observation(p.nm_id)
                if cached is not None:
                    observations[p.nm_id] = cached
                else:
                    uncached.append(p)

            def supplier_of(p):
                meta = meta_results.get(p.nm_id)
                if isinstance(meta, dict) and meta.get('wb_supplier_id'):
                    return meta['wb_supplier_id']
                return p.wb_supplier_id

            suppliers = {}
            for p in uncached:
                sid = supplier_of(p)
                if sid:
                    suppliers.setdefault(sid, set()).add(p.nm_id)
            for sid, nm_ids in suppliers.items():
                if out_of_budget():
                    result['status'] = 'partial'
                    break
                found = service.fetch_supplier_prices(
                    sid, nm_ids, max_pages=SUPPLIER_PAGES_PER_SYNC)
                observations.update(found)

            remaining = [p for p in uncached if p.nm_id not in observations]
            brands = {}
            for p in remaining:
                meta = meta_results.get(p.nm_id)
                brand = (meta.get('brand') if isinstance(meta, dict) else None) or p.brand
                if brand:
                    brands.setdefault(brand, set()).add(p.nm_id)
            for brand, nm_ids in brands.items():
                if out_of_budget():
                    result['status'] = 'partial'
                    break
                found = service.fetch_brand_prices(
                    brand, nm_ids, max_pages=BRAND_PAGES_PER_SYNC)
                observations.update(found)

            for nm_id, obs in observations.items():
                put_cached_observation(nm_id, obs)

            # ---------- Фаза B: запись ----------
            new_alerts = []
            for product in products:
                try:
                    with db.session.begin_nested():
                        meta = meta_results.get(product.nm_id)
                        if meta == 'gone':
                            product.fetch_error_count = (product.fetch_error_count or 0) + 1
                            if product.fetch_error_count >= DEACTIVATE_AFTER_GONE:
                                product.is_active = False
                                result['deactivated'] += 1
                        elif isinstance(meta, dict):
                            product.fetch_error_count = 0
                            product.title = meta.get('title') or product.title
                            product.brand = meta.get('brand') or product.brand
                            product.supplier_name = (
                                meta.get('supplier_name') or product.supplier_name)
                            product.wb_supplier_id = (
                                meta.get('wb_supplier_id') or product.wb_supplier_id)
                            product.image_url = meta.get('image_url') or product.image_url
                            if meta.get('is_adult') is not None:
                                product.is_adult = meta['is_adult']
                            product.metadata_synced_at = now

                        obs = observations.get(product.nm_id)
                        price_observed = obs is not None and (
                            obs.get('sale_price') is not None
                            or obs.get('price') is not None)

                        if price_observed:
                            first = (product.current_price is None
                                     and product.current_sale_price is None)
                            changed = _observation_changed(product, obs)
                            if first or changed:
                                change_pct = None
                                if (product.current_sale_price
                                        and obs.get('sale_price')
                                        and product.current_sale_price > 0):
                                    change_pct = round(
                                        (obs['sale_price'] - product.current_sale_price)
                                        / product.current_sale_price * 100, 2)
                                db.session.add(CompetitorPriceSnapshot(
                                    product_id=product.id, seller_id=seller_id,
                                    price=obs.get('price'),
                                    sale_price=obs.get('sale_price'),
                                    rating=obs.get('rating'),
                                    feedbacks_count=obs.get('feedbacks_count'),
                                    total_stock=obs.get('total_stock'),
                                    price_change_percent=change_pct,
                                    created_at=now))
                                result['snapshots'] += 1
                                if not first:
                                    alerts = _generate_alerts(product, obs, settings)
                                    for a in alerts:
                                        db.session.add(a)
                                    new_alerts.extend(alerts)
                                    result['alerts'] += len(alerts)
                            product.current_price = obs.get('price')
                            product.current_sale_price = obs.get('sale_price')
                            product.current_rating = obs.get('rating')
                            product.current_feedbacks_count = obs.get('feedbacks_count')
                            product.current_total_stock = obs.get('total_stock')
                            product.last_price_at = now
                            product.price_miss_count = 0
                            result['observed'] += 1
                        elif meta != 'gone':
                            product.price_miss_count = (product.price_miss_count or 0) + 1
                            result['misses'] += 1

                        product.last_fetched_at = now
                except Exception:
                    logger.exception('Ошибка записи товара %s (seller=%s)',
                                     product.nm_id, seller_id)

            settings.last_sync_at = now
            settings.last_sync_status = (
                'success' if result['status'] == 'ok' else 'partial')
            settings.last_sync_error = None
            settings.last_full_cycle_duration = round(time.time() - started, 2)
            settings.total_products_monitored = result['observed']
            settings.total_cycles_completed = (settings.total_cycles_completed or 0) + 1
            settings.next_sync_due_at = now + timedelta(
                minutes=normalize_sync_interval_minutes(
                    settings.sync_interval_minutes))
            settings.is_running = False
            db.session.commit()

            _notify_new_alerts(seller_id, new_alerts)
            return result

        except Exception as e:
            db.session.rollback()
            logger.exception('[Seller %s] Sync упал: %s', seller_id, e)
            settings = CompetitorMonitorSettings.query.filter_by(
                seller_id=seller_id).first()
            if settings:
                settings.is_running = False
                settings.last_sync_status = 'failed'
                settings.last_sync_error = str(e)[:500]
                settings.next_sync_due_at = now + timedelta(
                    minutes=normalize_sync_interval_minutes(
                        settings.sync_interval_minutes))
                db.session.commit()
            result['status'] = 'failed'
            return result


def _notify_new_alerts(seller_id, new_alerts):
    """Заглушка: агрегированное уведомление добавляется в Task 5."""
```

- [ ] **Step 4: Прогнать тесты**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_competitor_sync.py tests/test_competitor_fetch.py`
Expected: все passed. Внимание на `begin_nested` + autoflush: если `test_miss_does_not_null_current_and_no_snapshot` падает из-за flush — обернуть Фазу B в `with db.session.no_autoflush` не нужно, но убедиться, что Фаза A не делает SELECT после изменения settings (settings commit-ится до Фазы A — это уже так).

- [ ] **Step 5: Commit**

```bash
git add services/competitor_monitor.py tests/test_competitor_sync.py
git commit -m "feat(competitors): sync-ядро v2 — честные наблюдения, снимки только при изменении"
```

Примечание: старые импорты (`routes/competitors.py`, scheduler) временно сломаны — чинятся в Task 5/7/8; полный pytest до Task 8 не гонять, только файлы конкурентов.

---

### Task 5: Tick, агрегированное уведомление, импорт каталога продавца

**Files:**
- Modify: `services/competitor_monitor.py`
- Test: `tests/test_competitor_sync.py` (дополнение)

**Interfaces:**
- Consumes: `create_notification(seller_id, category, title, message, link=None, metadata=None)` из `seller_platform` (импорт внутри функции — как в `notify_supplier_updates`, services/product_sync_scheduler.py:571–621); модель `Notification`.
- Produces:
  - `run_competitor_monitor_tick(flask_app, seller_limit=2) -> dict` — `{'synced': [seller_id, ...]}`; выбирает `is_enabled AND (next_sync_due_at IS NULL OR next_sync_due_at <= now)`, oldest-first (`next_sync_due_at ASC NULLS FIRST`)
  - `COMPETITOR_NOTIFICATION_TITLE = 'Конкуренты: изменения'`, дедуп 4 часа
  - импорт каталога: внутри `sync_seller_competitors` до Фазы A — группы с `import_requested=1`: до `IMPORT_PAGES_PER_TICK = 3` страниц `fetch_seller_catalog_page`, до `IMPORT_MAX_PRODUCTS = 300` товаров суммарно в группе; создание отсутствующих `CompetitorProduct` с метаданными и первым наблюдением; флаг снимается, когда страница вернула < 100 товаров (каталог исчерпан) или достигнут лимит
  - `_notify_new_alerts(seller_id, new_alerts)` — реальная реализация

- [ ] **Step 1: Дописать падающие тесты в `tests/test_competitor_sync.py`**

```python
class TickTest(SyncTestBase):
    def test_picks_due_sellers_only(self):
        # наш seller due (next_sync_due_at NULL), второй — не due
        user2 = User(username='tick-user2', email='tick2@test.local')
        user2.set_password('x')
        db.session.add(user2)
        db.session.flush()
        seller2 = Seller(user_id=user2.id, name='NotDue')
        db.session.add(seller2)
        db.session.flush()
        db.session.add(CompetitorMonitorSettings(
            seller_id=seller2.id, is_enabled=True,
            next_sync_due_at=datetime.utcnow() + timedelta(hours=1)))
        db.session.commit()
        with unittest.mock.patch.object(cm, 'sync_seller_competitors',
                                        return_value={'status': 'ok'}) as sync:
            out = cm.run_competitor_monitor_tick(self.app)
        self.assertEqual(out['synced'], [self.seller.id])
        sync.assert_called_once()

    def test_disabled_never_picked(self):
        self.settings.is_enabled = False
        db.session.commit()
        with unittest.mock.patch.object(cm, 'sync_seller_competitors') as sync:
            out = cm.run_competitor_monitor_tick(self.app)
        self.assertEqual(out['synced'], [])
        sync.assert_not_called()


class NotificationTest(SyncTestBase):
    def _alert(self, severity='warning'):
        a = CompetitorAlert(
            seller_id=self.seller.id, alert_type='price_drop',
            severity=severity, message='x')
        return a

    def test_aggregated_notification_created(self):
        from models import Notification
        with unittest.mock.patch.object(
                cm, '_create_notification_compat') as create:
            cm._notify_new_alerts(self.seller.id, [self._alert(), self._alert('critical')])
        create.assert_called_once()
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs['category'], 'error')      # max severity critical
        self.assertEqual(kwargs['title'], cm.COMPETITOR_NOTIFICATION_TITLE)
        self.assertEqual(kwargs['link'], '/competitors/alerts')

    def test_dedup_within_4h(self):
        from models import Notification
        db.session.add(Notification(
            seller_id=self.seller.id, category='warning',
            title=cm.COMPETITOR_NOTIFICATION_TITLE, message='старое',
            created_at=datetime.utcnow() - timedelta(hours=1)))
        db.session.commit()
        with unittest.mock.patch.object(
                cm, '_create_notification_compat') as create:
            cm._notify_new_alerts(self.seller.id, [self._alert()])
        create.assert_not_called()

    def test_no_alerts_no_notification(self):
        with unittest.mock.patch.object(
                cm, '_create_notification_compat') as create:
            cm._notify_new_alerts(self.seller.id, [])
        create.assert_not_called()


class SellerImportTest(SyncTestBase):
    def test_import_creates_products_and_clears_flag(self):
        self.group.import_requested = True
        self.group.auto_source = 'seller'
        self.group.auto_source_value = '332183'
        db.session.commit()
        page = [{'nm_id': 900 + i, 'title': f'T{i}', 'brand': 'B',
                 'supplier_name': 'S', 'wb_supplier_id': 332183,
                 'image_url': 'http://x', 'price': 100, 'sale_price': 90,
                 'rating': 4.0, 'feedbacks_count': 1, 'total_stock': 5}
                for i in range(30)]  # < 100 => каталог исчерпан
        svc = self._fetch_mock()
        svc.fetch_seller_catalog_page.return_value = page
        cm.sync_seller_competitors(self.seller.id, self.app, fetch_service=svc)
        db.session.refresh(self.group)
        self.assertFalse(self.group.import_requested)
        self.assertEqual(
            CompetitorProduct.query.filter_by(group_id=self.group.id).count(), 30)
        # у созданных сразу есть метаданные и наблюдение
        p = CompetitorProduct.query.filter_by(nm_id=900).one()
        self.assertEqual(p.current_sale_price, 90)
        self.assertIsNotNone(p.metadata_synced_at)

    def test_import_respects_max_products(self):
        self.group.import_requested = True
        self.group.auto_source_value = '332183'
        db.session.commit()
        full_page = [{'nm_id': 10_000 + i, 'title': 'T', 'brand': 'B',
                      'supplier_name': 'S', 'wb_supplier_id': 332183,
                      'image_url': None, 'price': 100, 'sale_price': 90,
                      'rating': None, 'feedbacks_count': 0, 'total_stock': 1}
                     for i in range(100)]
        svc = self._fetch_mock()
        svc.fetch_seller_catalog_page.side_effect = [
            full_page,
            [dict(x, nm_id=x['nm_id'] + 100) for x in full_page],
            [dict(x, nm_id=x['nm_id'] + 200) for x in full_page],
        ]
        cm.sync_seller_competitors(self.seller.id, self.app, fetch_service=svc)
        self.assertLessEqual(
            CompetitorProduct.query.filter_by(group_id=self.group.id).count(),
            cm.IMPORT_MAX_PRODUCTS)
```

Добавить `import unittest.mock` в шапку файла, если ещё нет (используется `unittest.mock.patch`).

- [ ] **Step 2: Запустить — убедиться, что новые тесты падают**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_competitor_sync.py`
Expected: FAIL — нет `run_competitor_monitor_tick`, `_create_notification_compat`, `IMPORT_MAX_PRODUCTS`.

- [ ] **Step 3: Реализовать в `services/competitor_monitor.py`**

Добавить константы и функции:

```python
IMPORT_PAGES_PER_TICK = 3
IMPORT_MAX_PRODUCTS = 300
COMPETITOR_NOTIFICATION_TITLE = 'Конкуренты: изменения'
NOTIFICATION_DEDUP_HOURS = 4


def _create_notification_compat(**kwargs):
    """Тонкая обёртка для тестируемости (patched в unit-тестах)."""
    from seller_platform import create_notification
    return create_notification(**kwargs)


def _notify_new_alerts(seller_id, new_alerts):
    """Одно агрегированное уведомление в общий центр, дедуп 4 часа."""
    if not new_alerts:
        return
    from models import Notification

    cutoff = datetime.utcnow() - timedelta(hours=NOTIFICATION_DEDUP_HOURS)
    recent = Notification.query.filter(
        Notification.seller_id == seller_id,
        Notification.title == COMPETITOR_NOTIFICATION_TITLE,
        Notification.created_at >= cutoff,
    ).first()
    if recent:
        return

    severities = {a.severity for a in new_alerts}
    category = ('error' if 'critical' in severities
                else 'warning' if 'warning' in severities else 'info')
    price_changes = sum(1 for a in new_alerts
                        if a.alert_type in ('price_drop', 'price_increase'))
    discount_changes = sum(1 for a in new_alerts
                           if a.alert_type.startswith('discount'))
    stock_events = sum(1 for a in new_alerts
                       if a.alert_type in ('out_of_stock', 'back_in_stock'))
    parts = []
    if price_changes:
        parts.append(f'изменений цены: {price_changes}')
    if discount_changes:
        parts.append(f'изменений скидки: {discount_changes}')
    if stock_events:
        parts.append(f'событий наличия: {stock_events}')
    try:
        _create_notification_compat(
            seller_id=seller_id, category=category,
            title=COMPETITOR_NOTIFICATION_TITLE,
            message='У конкурентов ' + ', '.join(parts) + '.',
            link='/competitors/alerts')
    except Exception:
        logger.exception('Не удалось создать уведомление о конкурентах '
                         '(seller=%s)', seller_id)


def run_competitor_monitor_tick(flask_app, seller_limit=2):
    """Scheduler-джоб: выбрать до seller_limit due-продавцов и синхронизировать."""
    from models import CompetitorMonitorSettings, db

    now = datetime.utcnow()
    with flask_app.app_context():
        due = CompetitorMonitorSettings.query.filter(
            CompetitorMonitorSettings.is_enabled.is_(True),
            db.or_(
                CompetitorMonitorSettings.next_sync_due_at.is_(None),
                CompetitorMonitorSettings.next_sync_due_at <= now,
            ),
        ).order_by(
            CompetitorMonitorSettings.next_sync_due_at.asc().nullsfirst(),
        ).limit(seller_limit).all()
        seller_ids = [s.seller_id for s in due]

    synced = []
    for seller_id in seller_ids:
        try:
            sync_seller_competitors(seller_id, flask_app)
            synced.append(seller_id)
        except Exception:
            logger.exception('Tick: sync продавца %s упал', seller_id)
    return {'synced': synced}
```

Импорт каталога — внутрь `sync_seller_competitors`, после определения `def out_of_budget()` (начало Фазы A), ПЕРЕД блоком A1. При этом ранний выход `no_products` из Task 4 ОБЯЗАТЕЛЬНО переработать: `def out_of_budget()` и блок A0 поднимаются ВЫШЕ проверки пустого списка товаров, а сама проверка меняется на `if not products and not import_rows:` — иначе новая пустая группа с заявкой импорта каталога никогда не импортируется (write-фаза с B0 должна выполняться и при пустом `products`; это покрыто тестом `SellerImportTest.test_import_creates_products_and_clears_flag`). ВАЖНО: в сетевой фазе ORM-объекты НЕ мутируются (изменение `group.import_requested` здесь открыло бы через autoflush write-транзакцию SQLite посреди сетевых вызовов) — только локальные структуры; флаги снимаются в write-фазе. Вставить:

```python
            # A0: заявки на импорт каталога продавца (только сеть; ORM не трогаем)
            import_rows = []      # [(group_id, product_dict)]
            import_done = set()   # group_id, у которых заявку снимаем в B0
            from models import CompetitorGroup
            import_groups = CompetitorGroup.query.filter_by(
                seller_id=seller_id, import_requested=True).all()
            import_existing = {
                g.id: CompetitorProduct.query.filter_by(group_id=g.id).count()
                for g in import_groups}
            for group in import_groups:
                try:
                    supplier_id = int(group.auto_source_value or 0)
                except (TypeError, ValueError):
                    supplier_id = 0
                if not supplier_id:
                    import_done.add(group.id)
                    continue
                fetched = 0
                exhausted = False
                for page in range(1, IMPORT_PAGES_PER_TICK + 1):
                    if out_of_budget() or (
                            import_existing[group.id] + fetched
                            >= IMPORT_MAX_PRODUCTS):
                        break
                    try:
                        items = service.fetch_seller_catalog_page(
                            supplier_id, page=page)
                    except WBRateLimitedError:
                        break
                    for item in items:
                        import_rows.append((group.id, item))
                    fetched += len(items)
                    if len(items) < 100:
                        exhausted = True
                        break
                if exhausted or (import_existing[group.id] + fetched
                                 >= IMPORT_MAX_PRODUCTS):
                    import_done.add(group.id)
```

И в write-фазе (перед циклом по products) — создание строк импорта и снятие флагов:

```python
            # B0: создать товары из импорта каталога (идемпотентно по nm_id)
            for group_id, item in import_rows:
                nm_id = item.get('nm_id')
                if not nm_id:
                    continue
                try:
                    with db.session.begin_nested():
                        existing = CompetitorProduct.query.filter_by(
                            seller_id=seller_id, nm_id=nm_id,
                            group_id=group_id).first()
                        if existing:
                            continue
                        if CompetitorProduct.query.filter_by(
                                group_id=group_id).count() >= IMPORT_MAX_PRODUCTS:
                            break
                        db.session.add(CompetitorProduct(
                            seller_id=seller_id, group_id=group_id, nm_id=nm_id,
                            title=item.get('title'), brand=item.get('brand'),
                            supplier_name=item.get('supplier_name'),
                            wb_supplier_id=item.get('wb_supplier_id'),
                            image_url=item.get('image_url'),
                            current_price=item.get('price'),
                            current_sale_price=item.get('sale_price'),
                            current_rating=item.get('rating'),
                            current_feedbacks_count=item.get('feedbacks_count'),
                            current_total_stock=item.get('total_stock'),
                            metadata_synced_at=now,
                            last_price_at=now if item.get('sale_price') is not None else None,
                            last_fetched_at=now))
                except Exception:
                    logger.exception('Импорт товара %s в группу %s не удался',
                                     nm_id, group_id)

            for done_group_id in import_done:
                grp = db.session.get(CompetitorGroup, done_group_id)
                if grp:
                    grp.import_requested = False
```

Вызов `_notify_new_alerts(seller_id, new_alerts)` уже стоит после commit (Task 4) — оставить, заглушку заменить реализацией выше.

- [ ] **Step 4: Прогнать тесты**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_competitor_sync.py`
Expected: все passed.

- [ ] **Step 5: Commit**

```bash
git add services/competitor_monitor.py tests/test_competitor_sync.py
git commit -m "feat(competitors): tick due-продавцов, агрегированное уведомление, фоновый импорт каталога"
```

---

### Task 6: Компакция снимков + одноразовая чистка мусора

**Files:**
- Modify: `services/competitor_monitor.py` (добавить `compact_competitor_snapshots`)
- Create: `migrations/migrate_compact_competitor_snapshots.py`
- Test: `tests/test_competitor_compaction.py`

**Interfaces:**
- Produces:
  - `compact_competitor_snapshots(flask_app, max_seconds=55, chunk_size=5000) -> dict` — `{'deleted_null': n, 'deleted_dup': n, 'deleted_alerts': n, 'complete': bool}`; удаляет: (1) all-NULL снимки (price, sale_price, total_stock, rating все NULL), (2) подряд идущие дубликаты per product (LAG window, NULL-safe `IS`), (3) прочитанные алерты старше 90 дней; всё чанками `chunk_size` с отдельным commit на чанк, общий бюджет `max_seconds`
  - `migrations/migrate_compact_competitor_snapshots.py::migrate(db_path, max_seconds=60)` — та же логика на raw sqlite3 (для 10M-строчного прод-хвоста), идемпотентная, бюджет времени на прогон; недочищенный хвост добирает регулярная компакция

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_competitor_compaction.py
# -*- coding: utf-8 -*-
"""Компакция снимков: чанки, NULL-мусор, подряд-дубли, ретеншн алертов."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask

from models import (
    CompetitorAlert, CompetitorGroup, CompetitorMonitorSettings,
    CompetitorPriceSnapshot, CompetitorProduct, Seller, User, db,
)
from services import competitor_monitor as cm


class CompactionTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        user = User(username='cmp-user', email='cmp@test.local')
        user.set_password('x')
        db.session.add(user)
        db.session.flush()
        self.seller = Seller(user_id=user.id, name='CmpShop')
        db.session.add(self.seller)
        db.session.flush()
        self.group = CompetitorGroup(seller_id=self.seller.id, name='G')
        db.session.add(self.group)
        db.session.flush()
        self.product = CompetitorProduct(
            seller_id=self.seller.id, group_id=self.group.id, nm_id=1)
        db.session.add(self.product)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _snap(self, ts, price=None, sale=None, stock=None, rating=None):
        s = CompetitorPriceSnapshot(
            product_id=self.product.id, seller_id=self.seller.id,
            price=price, sale_price=sale, total_stock=stock, rating=rating,
            created_at=ts)
        db.session.add(s)
        return s

    def test_all_null_snapshots_deleted(self):
        base = datetime.utcnow() - timedelta(days=1)
        for i in range(10):
            self._snap(base + timedelta(minutes=i))          # мусор
        self._snap(base + timedelta(hours=1), price=100, sale=90, stock=5)
        db.session.commit()
        out = cm.compact_competitor_snapshots(self.app)
        self.assertEqual(out['deleted_null'], 10)
        self.assertEqual(CompetitorPriceSnapshot.query.count(), 1)

    def test_consecutive_duplicates_deleted_null_safe(self):
        base = datetime.utcnow() - timedelta(days=1)
        self._snap(base, price=100, sale=90, stock=5)
        self._snap(base + timedelta(minutes=1), price=100, sale=90, stock=5)   # дубль
        self._snap(base + timedelta(minutes=2), price=100, sale=90, stock=5)   # дубль
        self._snap(base + timedelta(minutes=3), price=100, sale=80, stock=5)   # изменение
        self._snap(base + timedelta(minutes=4), price=100, sale=80, stock=5)   # дубль
        db.session.commit()
        out = cm.compact_competitor_snapshots(self.app)
        self.assertEqual(out['deleted_dup'], 3)
        remaining = CompetitorPriceSnapshot.query.order_by(
            CompetitorPriceSnapshot.created_at).all()
        self.assertEqual([s.sale_price for s in remaining], [90, 80])

    def test_read_alerts_retention(self):
        old = CompetitorAlert(
            seller_id=self.seller.id, alert_type='price_drop', severity='info',
            message='x', is_read=True,
            created_at=datetime.utcnow() - timedelta(days=91))
        fresh_read = CompetitorAlert(
            seller_id=self.seller.id, alert_type='price_drop', severity='info',
            message='y', is_read=True,
            created_at=datetime.utcnow() - timedelta(days=30))
        old_unread = CompetitorAlert(
            seller_id=self.seller.id, alert_type='price_drop', severity='info',
            message='z', is_read=False,
            created_at=datetime.utcnow() - timedelta(days=120))
        db.session.add_all([old, fresh_read, old_unread])
        db.session.commit()
        out = cm.compact_competitor_snapshots(self.app)
        self.assertEqual(out['deleted_alerts'], 1)
        self.assertEqual(CompetitorAlert.query.count(), 2)

    def test_time_budget_returns_incomplete(self):
        base = datetime.utcnow() - timedelta(days=1)
        for i in range(50):
            self._snap(base + timedelta(seconds=i))
        db.session.commit()
        out = cm.compact_competitor_snapshots(self.app, max_seconds=0)
        self.assertFalse(out['complete'])


class OneShotMigrationTest(unittest.TestCase):
    def test_migration_cleans_null_and_dups(self):
        tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        tmp.close()
        try:
            con = sqlite3.connect(tmp.name)
            con.executescript("""
                CREATE TABLE competitor_price_snapshots (
                    id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL,
                    seller_id INTEGER NOT NULL, price INTEGER,
                    sale_price INTEGER, rating FLOAT, feedbacks_count INTEGER,
                    total_stock INTEGER, price_change_percent FLOAT,
                    created_at DATETIME);
            """)
            rows = []
            # 100 all-NULL + пары дублей + значащие переходы
            for i in range(100):
                rows.append((1, 1, None, None, None, None,
                             f'2026-05-01 10:{i // 60:02d}:{i % 60:02d}'))
            rows += [
                (1, 1, 100, 90, 4.5, 5, '2026-05-02 10:00:00'),
                (1, 1, 100, 90, 4.5, 5, '2026-05-02 11:00:00'),   # дубль
                (1, 1, 100, 80, 4.5, 5, '2026-05-02 12:00:00'),   # переход
            ]
            con.executemany(
                'INSERT INTO competitor_price_snapshots '
                '(product_id, seller_id, price, sale_price, rating, total_stock, created_at) '
                'VALUES (?,?,?,?,?,?,?)', rows)
            con.commit()
            con.close()

            from migrations.migrate_compact_competitor_snapshots import migrate
            self.assertTrue(migrate(tmp.name))
            con = sqlite3.connect(tmp.name)
            count = con.execute(
                'SELECT COUNT(*) FROM competitor_price_snapshots').fetchone()[0]
            con.close()
            self.assertEqual(count, 2)  # остались только значащие переходы
            # идемпотентность
            self.assertTrue(migrate(tmp.name))
        finally:
            Path(tmp.name).unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_competitor_compaction.py`
Expected: FAIL — нет `compact_competitor_snapshots` / модуля миграции.

- [ ] **Step 3: Реализовать компакцию в `services/competitor_monitor.py`**

```python
def compact_competitor_snapshots(flask_app, max_seconds=55, chunk_size=5000):
    """
    Чанковая компакция без длинного write-lock:
    1) all-NULL снимки (исторический мусор fetch-miss'ов v1);
    2) подряд идущие дубликаты per product (NULL-safe сравнение);
    3) прочитанные алерты старше 90 дней.
    Каждый чанк — отдельная короткая транзакция.
    """
    from models import db

    started = time.time()
    out = {'deleted_null': 0, 'deleted_dup': 0, 'deleted_alerts': 0,
           'complete': True}

    def out_of_time():
        return (time.time() - started) > max_seconds

    with flask_app.app_context():
        # 1) all-NULL
        while True:
            if out_of_time():
                out['complete'] = False
                return out
            res = db.session.execute(db.text("""
                DELETE FROM competitor_price_snapshots WHERE id IN (
                    SELECT id FROM competitor_price_snapshots
                    WHERE price IS NULL AND sale_price IS NULL
                      AND total_stock IS NULL AND rating IS NULL
                    LIMIT :chunk)
            """), {'chunk': chunk_size})
            db.session.commit()
            out['deleted_null'] += res.rowcount
            if res.rowcount < chunk_size:
                break

        # 2) подряд-дубликаты (LAG, NULL-safe IS)
        while True:
            if out_of_time():
                out['complete'] = False
                return out
            res = db.session.execute(db.text("""
                DELETE FROM competitor_price_snapshots WHERE id IN (
                    SELECT id FROM (
                        SELECT id,
                               price IS LAG(price) OVER w
                               AND sale_price IS LAG(sale_price) OVER w
                               AND total_stock IS LAG(total_stock) OVER w
                               AND rating IS LAG(rating) OVER w AS is_dup
                        FROM competitor_price_snapshots
                        WINDOW w AS (PARTITION BY product_id
                                     ORDER BY created_at, id)
                    ) WHERE is_dup LIMIT :chunk)
            """), {'chunk': chunk_size})
            db.session.commit()
            out['deleted_dup'] += res.rowcount
            if res.rowcount < chunk_size:
                break

        # 3) ретеншн прочитанных алертов
        cutoff = datetime.utcnow() - timedelta(days=90)
        while True:
            if out_of_time():
                out['complete'] = False
                return out
            res = db.session.execute(db.text("""
                DELETE FROM competitor_alerts WHERE id IN (
                    SELECT id FROM competitor_alerts
                    WHERE is_read = 1 AND created_at < :cutoff
                    LIMIT :chunk)
            """), {'cutoff': cutoff, 'chunk': chunk_size})
            db.session.commit()
            out['deleted_alerts'] += res.rowcount
            if res.rowcount < chunk_size:
                break

    return out
```

- [ ] **Step 4: Написать одноразовую миграцию**

```python
# migrations/migrate_compact_competitor_snapshots.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Одноразовая чистка мусора competitor_price_snapshots (v1 накопил ~10M строк
на 2 товара: fetch-miss писался как «изменение на None» каждый цикл).

Удаляет чанками: (1) all-NULL снимки, (2) подряд идущие дубликаты per product.
Бюджет времени на прогон — 60с: недочищенный хвост доберёт регулярная
чанковая компакция scheduler-джоба. Идемпотентная. Место на диске вернёт
только последующий ручной VACUUM (не выполняется здесь: БД многогигабайтная).
"""
import logging
import sqlite3
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / 'data' / 'seller_platform.db'
CHUNK = 20_000


def migrate(db_path, max_seconds=60):
    conn = sqlite3.connect(str(db_path))
    conn.execute('PRAGMA busy_timeout = 30000')
    started = time.time()
    deleted_null = 0
    deleted_dup = 0
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if 'competitor_price_snapshots' not in tables:
            logger.info('Таблица снимков отсутствует — чистка не требуется')
            return True

        def out_of_time():
            return (time.time() - started) > max_seconds

        while not out_of_time():
            cur = conn.execute("""
                DELETE FROM competitor_price_snapshots WHERE id IN (
                    SELECT id FROM competitor_price_snapshots
                    WHERE price IS NULL AND sale_price IS NULL
                      AND total_stock IS NULL AND rating IS NULL
                    LIMIT ?)""", (CHUNK,))
            conn.commit()
            deleted_null += cur.rowcount
            if cur.rowcount < CHUNK:
                break

        while not out_of_time():
            cur = conn.execute("""
                DELETE FROM competitor_price_snapshots WHERE id IN (
                    SELECT id FROM (
                        SELECT id,
                               price IS LAG(price) OVER w
                               AND sale_price IS LAG(sale_price) OVER w
                               AND total_stock IS LAG(total_stock) OVER w
                               AND rating IS LAG(rating) OVER w AS is_dup
                        FROM competitor_price_snapshots
                        WINDOW w AS (PARTITION BY product_id
                                     ORDER BY created_at, id)
                    ) WHERE is_dup LIMIT ?)""", (CHUNK,))
            conn.commit()
            deleted_dup += cur.rowcount
            if cur.rowcount < CHUNK:
                break

        logger.info('Чистка снимков: удалено %s all-NULL, %s дублей '
                    '(бюджет %sс, потрачено %.1fс)',
                    deleted_null, deleted_dup, max_seconds,
                    time.time() - started)
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT_DB_PATH)
    if not Path(db_path).exists():
        logger.error('БД не найдена: %s', db_path)
        return 1
    return 0 if migrate(db_path) else 1


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 5: Прогнать тесты**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_competitor_compaction.py && python -m py_compile migrations/migrate_compact_competitor_snapshots.py`
Expected: все passed. Если SQLite в тестовом окружении старше 3.25 (нет window functions) — тест упадёт на синтаксисе `LAG`; в таком случае проверить `python3 -c "import sqlite3; print(sqlite3.sqlite_version)"` (в проде Docker Python 3.11 — заведомо новее).

- [ ] **Step 6: Commit**

```bash
git add services/competitor_monitor.py migrations/migrate_compact_competitor_snapshots.py tests/test_competitor_compaction.py
git commit -m "feat(competitors): чанковая компакция снимков + одноразовая чистка v1-мусора"
```

---

### Task 7: Scheduler wiring + удаление тредов

**Files:**
- Modify: `services/product_sync_scheduler.py` (строки ~500–516, ~531–533, ~1882–1897)
- Delete: `tests/test_competitor_monitor_loops.py`

**Interfaces:**
- Consumes: `run_competitor_monitor_tick(flask_app, seller_limit=2)`, `compact_competitor_snapshots(flask_app)` (Task 5/6).
- Produces: джоб `competitor_monitor_tick` (interval 1 мин), джоб `competitor_snapshot_compaction` (interval 24 ч, тело заменено). Джоб `check_competitor_monitor_loops` и startup `threading.Timer` удалены.

- [ ] **Step 1: Заменить регистрации джобов**

В `init_scheduler` найти блок (строки ~500–507):

```python
    scheduler.add_job(
        func=lambda: _check_competitor_monitor_loops(flask_app),
        trigger=IntervalTrigger(minutes=5),
        id='check_competitor_monitor_loops',
        name='Check and restart competitor monitor loops',
        ...
    )
```

заменить на:

```python
    scheduler.add_job(
        func=lambda: _run_competitor_monitor_tick(flask_app),
        trigger=IntervalTrigger(minutes=1),
        id='competitor_monitor_tick',
        name='Sync due competitor monitor sellers (bounded)',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
```

Регистрацию `competitor_snapshot_compaction` (строки ~509–516) оставить (interval 24ч), тело помощника меняется ниже. Строку `threading.Timer(15.0, lambda: _check_competitor_monitor_loops(flask_app)).start()` (~533) — УДАЛИТЬ.

- [ ] **Step 2: Заменить функции-помощники**

Найти (строки ~1882–1897):

```python
def _check_competitor_monitor_loops(flask_app):
    ...
def _compact_competitor_snapshots(flask_app):
    ...
```

заменить на:

```python
def _run_competitor_monitor_tick(flask_app):
    """Bounded tick мониторинга конкурентов: до 2 due-продавцов за минуту."""
    try:
        from services.competitor_monitor import run_competitor_monitor_tick
        run_competitor_monitor_tick(flask_app)
    except Exception as e:
        logger.error(f"Ошибка competitor monitor tick: {e}")


def _compact_competitor_snapshots(flask_app):
    """Чанковая компакция снимков конкурентов (без длинного write-lock)."""
    try:
        from services.competitor_monitor import compact_competitor_snapshots
        compact_competitor_snapshots(flask_app)
    except Exception as e:
        logger.error(f"Ошибка компакции снимков конкурентов: {e}")
```

- [ ] **Step 3: Удалить тесты тредов и проверить отсутствие ссылок**

```bash
git rm tests/test_competitor_monitor_loops.py
grep -rn "start_competitor_monitor_loop\|stop_competitor_monitor_loop\|check_and_restart_monitor_loops\|stop_all_monitor_loops\|_monitor_threads\|normalize_cycle_pause_seconds" --include='*.py' .
```

Expected: единственные оставшиеся упоминания — в `routes/competitors.py` (чинится в Task 8). Если grep находит другие места (например, `seller_platform.py` shutdown-hook) — удалить эти вызовы.

- [ ] **Step 4: Компиляция**

Run: `python -m py_compile services/product_sync_scheduler.py`
Expected: без ошибок.

- [ ] **Step 5: Commit**

```bash
git add -A services/product_sync_scheduler.py tests/
git commit -m "feat(competitors): scheduler tick вместо per-seller тредов"
```

---

### Task 8: Роуты v2

**Files:**
- Modify: `routes/competitors.py` (существенная переработка API-секции; страницы-рендеры остаются)
- Test: `tests/test_competitor_routes.py`

**Interfaces:**
- Consumes: `normalize_sync_interval_minutes`, модели с новыми полями, `CompetitorProxyEncryptionError`, `CompetitorFetchService.search_products/fetch_seller_catalog_page`, `WBRateLimitedError`.
- Produces (контракты для UI, Task 10/11):
  - `POST /api/competitors/products` — body `{group_id, nm_ids: [int]}` (строгая валидация: только `int`, не `bool`, `> 0`, дедуп, cap 300; нарушение → 400 весь запрос) ИЛИ `{group_id, wb_supplier_id: int}` (заявка импорта: `auto_source='seller'`, `import_requested=True`). Ответ: `{'success': True, 'added': n, 'reactivated': n, 'skipped': n, 'scheduled': True}`. WB НЕ вызывается; `settings.next_sync_due_at = utcnow()`.
  - `GET /api/competitors/search?q=` — 1 страница; при `WBRateLimitedError` → 503 `{'error': 'WB ограничивает запросы, повторите позже'}`.
  - `GET /api/competitors/seller-catalog?supplier_id=&page=` — 1 страница, тот же 429→503 контракт.
  - `PUT /api/competitors/settings` — поля `is_enabled`, `sync_interval_minutes` (нормализуется), `price_change_alert_percent` (clamp 0.1..90), `discount_alert_pp` (clamp 1..50), `max_products` (clamp 1..1000), `proxy_url` (set через property; `CompetitorProxyEncryptionError` → 400). Включение (`false→true`) ставит `next_sync_due_at = utcnow()`. Никаких стартов тредов.
  - `POST /api/competitors/sync` → `{'success': True, 'scheduled': True}` (только `next_sync_due_at = utcnow()`).
  - `PUT /api/competitors/groups/<id>` — `own_product_id` проверяется tenant-scoped (`Product.id + seller_id`), чужой/несуществующий → 400.
  - `GET /api/competitors/compare/<group_id>` — добавляет `own_product.position` (1-based место own-цены среди competitor sale-цен по возрастанию), `own_product.total_with_own`, `own_product.vs_min_percent` (на сколько % own дороже минимума; отрицательное = дешевле), `own_product.median_competitor_price`.
  - `GET /api/competitors/dashboard-data` — агрегаты групп ОДНИМ SQL-запросом (`func.count/min/avg/max` с `group_by(group_id)`), без N+1; для групп с `own_product_id` — те же position-поля.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_competitor_routes.py
# -*- coding: utf-8 -*-
"""Роуты конкурентов v2: строгая валидация, tenant scope, bounded WB."""
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from flask import Flask

from models import (
    CompetitorGroup, CompetitorMonitorSettings, CompetitorProduct,
    Product, Seller, User, db,
)
from routes.competitors import register_competitor_routes
from services.competitor_fetch import WBRateLimitedError


class RoutesTestBase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True, SECRET_KEY='t',
            SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        register_competitor_routes(self.app)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.seller = self._mk_seller('shop1', 'r1@test.local')
        self.other = self._mk_seller('shop2', 'r2@test.local')
        self.group = CompetitorGroup(seller_id=self.seller.id, name='G')
        db.session.add(self.group)
        db.session.commit()
        self.client = self.app.test_client()
        self.user = MagicMock()
        self.user.is_authenticated = True
        self.user.seller = self.seller

    def _mk_seller(self, name, email):
        user = User(username=name, email=email)
        user.set_password('x')
        db.session.add(user)
        db.session.flush()
        seller = Seller(user_id=user.id, name=name)
        db.session.add(seller)
        db.session.commit()
        return seller

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _as_user(self):
        return patch('flask_login.utils._get_user', return_value=self.user)


class AddProductsTest(RoutesTestBase):
    def test_add_by_nm_ids_no_wb_calls(self):
        with self._as_user(), \
             patch('routes.competitors.CompetitorFetchService') as svc:
            resp = self.client.post('/api/competitors/products', json={
                'group_id': self.group.id, 'nm_ids': [111, 222]})
            self.assertEqual(resp.status_code, 200)
            svc.assert_not_called()
        self.assertEqual(resp.get_json()['added'], 2)
        self.assertTrue(resp.get_json()['scheduled'])
        rows = CompetitorProduct.query.filter_by(group_id=self.group.id).all()
        self.assertEqual({r.nm_id for r in rows}, {111, 222})
        settings = CompetitorMonitorSettings.query.filter_by(
            seller_id=self.seller.id).first()
        self.assertIsNotNone(settings.next_sync_due_at)

    def test_rejects_bool_float_string_and_dups(self):
        with self._as_user():
            for bad in ([True, 5], [1.5], ['123'], [111, 111], [0], [-3]):
                resp = self.client.post('/api/competitors/products', json={
                    'group_id': self.group.id, 'nm_ids': bad})
                self.assertEqual(resp.status_code, 400, f'nm_ids={bad}')
        self.assertEqual(CompetitorProduct.query.count(), 0)

    def test_cap_300(self):
        with self._as_user():
            resp = self.client.post('/api/competitors/products', json={
                'group_id': self.group.id,
                'nm_ids': list(range(1, 302))})
        self.assertEqual(resp.status_code, 400)

    def test_foreign_group_404(self):
        foreign = CompetitorGroup(seller_id=self.other.id, name='F')
        db.session.add(foreign)
        db.session.commit()
        with self._as_user():
            resp = self.client.post('/api/competitors/products', json={
                'group_id': foreign.id, 'nm_ids': [111]})
        self.assertEqual(resp.status_code, 404)

    def test_supplier_import_request_sets_flag(self):
        with self._as_user():
            resp = self.client.post('/api/competitors/products', json={
                'group_id': self.group.id, 'wb_supplier_id': 332183})
        self.assertEqual(resp.status_code, 200)
        db.session.refresh(self.group)
        self.assertTrue(self.group.import_requested)
        self.assertEqual(self.group.auto_source, 'seller')
        self.assertEqual(self.group.auto_source_value, '332183')

    def test_reactivates_inactive_duplicate(self):
        row = CompetitorProduct(
            seller_id=self.seller.id, group_id=self.group.id, nm_id=111,
            is_active=False, fetch_error_count=20, price_miss_count=7)
        db.session.add(row)
        db.session.commit()
        with self._as_user():
            resp = self.client.post('/api/competitors/products', json={
                'group_id': self.group.id, 'nm_ids': [111]})
        self.assertEqual(resp.get_json()['reactivated'], 1)
        db.session.refresh(row)
        self.assertTrue(row.is_active)
        self.assertEqual(row.fetch_error_count, 0)
        self.assertEqual(row.price_miss_count, 0)


class SearchBoundedTest(RoutesTestBase):
    def test_429_returns_503(self):
        with self._as_user(), \
             patch('routes.competitors.CompetitorFetchService') as SvcCls:
            SvcCls.return_value.search_products.side_effect = WBRateLimitedError('429')
            resp = self.client.get('/api/competitors/search?q=носки')
        self.assertEqual(resp.status_code, 503)
        self.assertIn('WB', resp.get_json()['error'])

    def test_search_ok(self):
        with self._as_user(), \
             patch('routes.competitors.CompetitorFetchService') as SvcCls:
            SvcCls.return_value.search_products.return_value = [
                {'nm_id': 1, 'title': 'X'}]
            resp = self.client.get('/api/competitors/search?q=носки')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()), 1)


class SettingsTest(RoutesTestBase):
    def test_put_normalizes_and_schedules_on_enable(self):
        with self._as_user():
            resp = self.client.put('/api/competitors/settings', json={
                'is_enabled': True, 'sync_interval_minutes': 5,
                'max_products': 99999, 'discount_alert_pp': 200})
        data = resp.get_json()
        self.assertEqual(data['sync_interval_minutes'], 30)   # clamp снизу
        self.assertEqual(data['max_products'], 1000)          # clamp cap
        self.assertEqual(data['discount_alert_pp'], 50)       # clamp
        self.assertIsNotNone(data['next_sync_due_at'])

    def test_proxy_masked_in_response(self):
        import os
        from cryptography.fernet import Fernet
        old = os.environ.get('ENCRYPTION_KEY')
        os.environ['ENCRYPTION_KEY'] = Fernet.generate_key().decode('ascii')
        try:
            with self._as_user():
                resp = self.client.put('/api/competitors/settings', json={
                    'proxy_url': 'http://u:pw@p.example.com:1080'})
            data = resp.get_json()
            self.assertNotIn('proxy_url', data)
            self.assertNotIn('pw', str(data))
            self.assertEqual(data['proxy']['masked'], 'http://p.example.com:1080')
        finally:
            if old is None:
                os.environ.pop('ENCRYPTION_KEY', None)
            else:
                os.environ['ENCRYPTION_KEY'] = old

    def test_force_sync_schedules(self):
        with self._as_user():
            resp = self.client.post('/api/competitors/sync')
        self.assertTrue(resp.get_json()['scheduled'])


class GroupOwnProductTest(RoutesTestBase):
    def test_foreign_own_product_rejected(self):
        foreign_product = Product(
            seller_id=self.other.id, nm_id=999, title='Чужой')
        db.session.add(foreign_product)
        db.session.commit()
        with self._as_user():
            resp = self.client.put(
                f'/api/competitors/groups/{self.group.id}',
                json={'own_product_id': foreign_product.id})
        self.assertEqual(resp.status_code, 400)

    def test_own_product_accepted_and_compare_position(self):
        own = Product(seller_id=self.seller.id, nm_id=1000, title='Мой',
                      price=2000, discount_price=1500)
        db.session.add(own)
        db.session.flush()
        for i, sale in enumerate([1000, 1400, 1600, 2000]):
            db.session.add(CompetitorProduct(
                seller_id=self.seller.id, group_id=self.group.id,
                nm_id=2000 + i, current_sale_price=sale))
        db.session.commit()
        with self._as_user():
            resp = self.client.put(
                f'/api/competitors/groups/{self.group.id}',
                json={'own_product_id': own.id})
            self.assertEqual(resp.status_code, 200)
            resp = self.client.get(f'/api/competitors/compare/{self.group.id}')
        data = resp.get_json()
        # own 1500: дешевле него 1000 и 1400 => позиция 3 из 5
        self.assertEqual(data['own_product']['position'], 3)
        self.assertEqual(data['own_product']['total_with_own'], 5)
        self.assertEqual(data['own_product']['vs_min_percent'], 50.0)


if __name__ == '__main__':
    unittest.main()
```

Если конструктор `Product` требует иные обязательные поля — посмотреть ближайшее использование в тестах репо и добавить минимум.

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_competitor_routes.py`
Expected: FAIL (импорт `CompetitorFetchService` в routes отсутствует, старые контракты).

- [ ] **Step 3: Переработать `routes/competitors.py`**

Ключевые замены (страничные роуты `/competitors*` не трогать, кроме удаления мёртвых импортов):

1. Шапка: добавить `from services.competitor_fetch import CompetitorFetchService, WBRateLimitedError`, `from services.competitor_monitor import normalize_sync_interval_minutes`, `from models import CompetitorProxyEncryptionError, Product` и `from datetime import datetime`.

2. Валидатор nm_ids (модульная функция над `register_competitor_routes`):

```python
MAX_NM_IDS_PER_REQUEST = 300


def _validate_nm_ids(raw):
    """Строгий список уникальных positive int. Ошибка -> (None, 'текст')."""
    if not isinstance(raw, list) or not raw:
        return None, 'nm_ids должен быть непустым списком целых чисел'
    if len(raw) > MAX_NM_IDS_PER_REQUEST:
        return None, f'Не больше {MAX_NM_IDS_PER_REQUEST} товаров за раз'
    seen = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            return None, f'Недопустимый nm_id: {item!r}'
        if item in seen:
            return None, f'Дубликат nm_id: {item}'
        seen.append(item)
    return seen, None
```

3. `api_competitors_add_products` — полная замена тела:

```python
        seller = _get_seller()
        if not seller:
            return jsonify({'error': 'Магазин не настроен'}), 403
        data = request.get_json() or {}
        group = CompetitorGroup.query.filter_by(
            id=data.get('group_id'), seller_id=seller.id).first()
        if not group:
            return jsonify({'error': 'Группа не найдена'}), 404

        settings = _get_or_create_settings(seller.id)

        # Режим 2: заявка на импорт каталога продавца (fetch делает scheduler)
        if data.get('wb_supplier_id') is not None:
            supplier_id = data['wb_supplier_id']
            if isinstance(supplier_id, bool) or not isinstance(supplier_id, int) \
                    or supplier_id <= 0:
                return jsonify({'error': 'Некорректный wb_supplier_id'}), 400
            group.auto_source = 'seller'
            group.auto_source_value = str(supplier_id)
            group.import_requested = True
            settings.next_sync_due_at = datetime.utcnow()
            db.session.commit()
            return jsonify({'success': True, 'added': 0, 'reactivated': 0,
                            'skipped': 0, 'scheduled': True,
                            'import_requested': True})

        # Режим 1: точные nm_ids — только вставка, без WB
        nm_ids, err = _validate_nm_ids(data.get('nm_ids'))
        if err:
            return jsonify({'error': err}), 400

        added = reactivated = skipped = 0
        for nm_id in nm_ids:
            existing = CompetitorProduct.query.filter_by(
                seller_id=seller.id, nm_id=nm_id, group_id=group.id).first()
            if existing:
                if existing.is_active:
                    skipped += 1
                else:
                    existing.is_active = True
                    existing.fetch_error_count = 0
                    existing.price_miss_count = 0
                    reactivated += 1
                continue
            db.session.add(CompetitorProduct(
                seller_id=seller.id, group_id=group.id, nm_id=nm_id))
            added += 1

        settings.next_sync_due_at = datetime.utcnow()
        db.session.commit()
        return jsonify({'success': True, 'added': added,
                        'reactivated': reactivated, 'skipped': skipped,
                        'scheduled': True})
```

4. `api_competitors_search` и `api_competitors_seller_catalog` — bounded:

```python
        try:
            service = CompetitorFetchService()
            results = service.search_products(query, limit=50)
        except WBRateLimitedError:
            return jsonify({'error': 'WB ограничивает запросы, повторите позже'}), 503
        return jsonify(results)
```

(для seller-catalog аналогично: `service.fetch_seller_catalog_page(wb_supplier_id, page=request.args.get('page', 1, type=int))`).

5. `api_competitors_settings` PUT — заменить обработку полей:

```python
        if 'is_enabled' in data:
            enabling = bool(data['is_enabled']) and not settings.is_enabled
            settings.is_enabled = bool(data['is_enabled'])
            if enabling:
                settings.next_sync_due_at = datetime.utcnow()
        if 'sync_interval_minutes' in data:
            settings.sync_interval_minutes = normalize_sync_interval_minutes(
                data['sync_interval_minutes'])
        if 'price_change_alert_percent' in data:
            settings.price_change_alert_percent = max(
                0.1, min(90.0, float(data['price_change_alert_percent'])))
        if 'discount_alert_pp' in data:
            settings.discount_alert_pp = max(
                1.0, min(50.0, float(data['discount_alert_pp'])))
        if 'max_products' in data:
            settings.max_products = max(1, min(1000, int(data['max_products'])))
        if 'proxy_url' in data:
            try:
                settings.proxy_url = (data['proxy_url'] or '').strip() or None
            except CompetitorProxyEncryptionError as e:
                db.session.rollback()
                return jsonify({'error': str(e)}), 400
        db.session.commit()
        return jsonify(settings.to_dict())
```

Блок запуска/остановки тредов после commit — УДАЛИТЬ вместе с импортами `start/stop_competitor_monitor_loop`.

6. `api_competitors_force_sync` — заменить на:

```python
        settings = _get_or_create_settings(seller.id)
        settings.next_sync_due_at = datetime.utcnow()
        db.session.commit()
        return jsonify({'success': True, 'scheduled': True,
                        'message': 'Синхронизация запустится в течение минуты'})
```

7. `api_competitors_group` PUT — при `own_product_id` добавить tenant-проверку:

```python
        if 'own_product_id' in data:
            opid = data['own_product_id']
            if opid is not None:
                if isinstance(opid, bool) or not isinstance(opid, int):
                    return jsonify({'error': 'Некорректный own_product_id'}), 400
                own = Product.query.filter_by(
                    id=opid, seller_id=seller.id).first()
                if not own:
                    return jsonify({'error': 'Товар не найден'}), 400
            group.own_product_id = opid
```

Ту же проверку добавить в POST создания группы.

8. `api_competitors_compare` — расширить own_product:

```python
        own_product = None
        if group.own_product_id and group.own_product:
            own = group.own_product
            own_price = float(own.discount_price or own.price or 0) or None
            comp_prices = sorted(
                p['current_sale_price'] for p in competitors
                if p.get('current_sale_price'))
            position = None
            vs_min = None
            median = None
            if own_price and comp_prices:
                position = 1 + sum(1 for c in comp_prices if c < own_price)
                vs_min = round((own_price - comp_prices[0]) / comp_prices[0] * 100, 1)
                mid = len(comp_prices) // 2
                median = (comp_prices[mid] if len(comp_prices) % 2
                          else round((comp_prices[mid - 1] + comp_prices[mid]) / 2))
            own_product = {
                'nm_id': own.nm_id, 'title': own.title,
                'price': float(own.price) if own.price else None,
                'discount_price': float(own.discount_price) if own.discount_price else None,
                'position': position,
                'total_with_own': (len(comp_prices) + 1) if comp_prices else None,
                'vs_min_percent': vs_min,
                'median_competitor_price': median,
            }
```

9. `api_competitors_dashboard_data` — один агрегатный запрос вместо цикла:

```python
        from sqlalchemy import func
        agg = dict()
        rows = db.session.query(
            CompetitorProduct.group_id,
            func.count(CompetitorProduct.id),
            func.min(CompetitorProduct.current_sale_price),
            func.avg(CompetitorProduct.current_sale_price),
            func.max(CompetitorProduct.current_sale_price),
        ).filter(
            CompetitorProduct.seller_id == seller.id,
            CompetitorProduct.is_active.is_(True),
        ).group_by(CompetitorProduct.group_id).all()
        for group_id, cnt, mn, avg, mx in rows:
            agg[group_id] = {'products_count': cnt, 'min_price': mn,
                             'avg_price': round(avg) if avg else None,
                             'max_price': mx}
        groups_data = [{**g.to_dict(), **agg.get(g.id, {
            'products_count': 0, 'min_price': None,
            'avg_price': None, 'max_price': None})} for g in groups]
```

- [ ] **Step 4: Прогнать тесты**

Run: `SKIP_SCHEDULER=1 python -m pytest -q tests/test_competitor_routes.py tests/test_competitor_sync.py tests/test_competitor_fetch.py tests/test_competitor_compaction.py tests/test_competitor_proxy_credential.py tests/test_competitor_v2_migration.py && python -m py_compile routes/competitors.py`
Expected: все passed.

- [ ] **Step 5: Commit**

```bash
git add routes/competitors.py tests/test_competitor_routes.py
git commit -m "feat(competitors): роуты v2 — строгая валидация, bounded WB, позиция own-товара"
```

---

### Task 9: Вайринг миграций + AGENTS.md

**Files:**
- Modify: `docker-entrypoint.sh` (после строки `python migrations/migrate_add_wb_card_audit.py ...`, ~182)
- Modify: `migrations/run_all_migrations.py` (по образцу импорта `migrate_add_wb_card_audit` на ~1329)
- Modify: `AGENTS.md` (пункт про competitor_monitor в «Карте репозитория» + список миграций в «База данных и миграции»)

- [ ] **Step 1: docker-entrypoint.sh**

После строки `python migrations/migrate_add_wb_card_audit.py /app/data/seller_platform.db` добавить (fail-fast, без `|| echo`):

```bash
python migrations/migrate_competitor_monitor_v2.py /app/data/seller_platform.db
python migrations/migrate_compact_competitor_snapshots.py /app/data/seller_platform.db
```

- [ ] **Step 2: run_all_migrations.py**

Найти блок импорта/вызова `migrate_add_wb_card_audit` (~1329) и добавить рядом аналогичные блоки для обеих новых миграций, точно повторяя локальный паттерн (try/except-обёртка, лог).

- [ ] **Step 3: AGENTS.md**

Заменить существующий пункт про `services/competitor_monitor.py` (в «Карте репозитория») на:

```markdown
- `services/competitor_monitor.py`, `services/competitor_fetch.py`,
  `routes/competitors.py`: мониторинг конкурентов v2. Никаких собственных
  тредов: singleton scheduler раз в минуту синхронизирует до 2 due-продавцов
  (`next_sync_due_at`, интервал 30..1440 минут per seller, default 60).
  Fetch-слой ORM-free: basket CDN для метаданных (404 = товар удалён),
  catalog.wb.ru по продавцу и search v18 по бренду для цен; ОДИН глобальный
  process-wide rate limiter (`COMPETITOR_PUBLIC_RPM`, default 20) на все
  публичные вызовы всех продавцов + circuit breaker per источник (3 подряд
  429/5xx -> cooldown 10 минут). 429 никогда не ждётся sleep-ом. Fetch-miss —
  не наблюдение: current-значения не затираются, снимок не пишется,
  `price_miss_count` растёт и включает basket-recheck. Снимок цены создаётся
  только при успешном наблюдении с фактическим изменением; цены хранятся в
  рублях (integer). HTTP-пути bounded: добавление товаров — только вставка
  nm_ids (строгая typed-валидация, cap 300) с `next_sync_due_at=now`;
  интерактивный поиск/превью каталога — одна страница, 429 -> честный 503.
  Импорт каталога продавца — заявка `import_requested` на группе, выполняет
  scheduler (до 3 страниц за тик, до 300 товаров). `proxy_url` — credential:
  запись fail-closed шифруется Fernet, наружу только маска. Алерты зеркалятся
  одним агрегированным Notification (дедуп 4 часа). Компакция снимков —
  чанковая (5000 строк/commit), без длинного SQLite write-lock; исторический
  v1-мусор чистит идемпотентная `migrate_compact_competitor_snapshots.py`
  с бюджетом времени на прогон.
```

В раздел «База данных и миграции» (список команд) добавить:

```markdown
python migrations/migrate_competitor_monitor_v2.py data/seller_platform.db
python migrations/migrate_compact_competitor_snapshots.py data/seller_platform.db
```

- [ ] **Step 4: Проверка**

```bash
bash -n docker-entrypoint.sh && python -m py_compile migrations/run_all_migrations.py && git diff --check
```

Expected: без ошибок.

- [ ] **Step 5: Commit**

```bash
git add docker-entrypoint.sh migrations/run_all_migrations.py AGENTS.md
git commit -m "chore(competitors): вайринг миграций v2 fail-fast + AGENTS.md"
```

---

### Task 10: UI — дашборд и группы («Тёплая редакция»)

**Files:**
- Modify: `templates/competitors_dashboard.html` (переработка)
- Modify: `templates/competitors_groups.html` (переработка)

**Interfaces:**
- Consumes: `GET /api/competitors/dashboard-data` (Task 8: settings + groups с агрегатами), `POST /api/competitors/sync` (`{'scheduled': True}`), CRUD `/api/competitors/groups`, `GET /api/products/search?q=` (существующий login-scoped поиск своих товаров — для привязки own_product), макросы `templates/macros/components.html` (`stat_card`, `empty_state`, `btn`, `chip`, `skeleton`, `toggle`) и `templates/macros/icons.html` (`icon(name, size)`), стор `$store.toasts`.
- Правила: только `.sh-*` классы и токены (`var(--danger)` и т.п., БЕЗ inline-hex); радиусы карточек ≤ `--r-md`; поллинг «раз в 30с» строго за `document.hidden`-гейтом + немедленный refresh на `visibilitychange`; CSRF из `meta[name=csrf-token]` в мутирующих fetch; обе темы; empty/loading states.

- [ ] **Step 1: Переработать `competitors_dashboard.html`**

Структура страницы (внутри существующего base-layout блока, как на других страницах раздела):

1. Заголовок `.sh-page-head`: h1 «Конкуренты», строка статуса (`последний синк <относительное время> · следующий ~<next_sync_due_at>`), справа кнопки: «Синхронизировать» (`.sh-btn`, spinner при `is_running`), «Настройки» (ссылка).
2. Если `settings.is_enabled == false` — `.sh-alert` warning с CTA «Включить в настройках».
3. Ряд stat-карточек через макрос `stat_card`: «Товаров под наблюдением», «Групп», «Непрочитанных алертов», «Последний цикл, с».
4. Сетка групп: карточка группы = имя + цветная точка (`style="background: {{ g.color }}"` — единственное допустимое динамическое значение цвета, это пользовательский выбор, не статус), `products_count`, `min/avg/max` цены, бейдж позиции own-товара, если есть (`ниже минимума`/`+N% к минимуму` через `var(--ok)`/`var(--warn)` классы `.sh-chip`), клик → `/competitors/groups/<id>`.
5. Лента последних алертов (до 10): иконка по типу через `status_icon`, текст, относительное время; ссылка «Все алерты».
6. Alpine-функция:

```javascript
function competitorsDashboard(initial) {
  return {
    data: initial, loading: false, syncing: false, timer: null,
    init() {
      this.timer = setInterval(() => {
        if (!document.hidden) this.refresh();
      }, 30000);
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden) this.refresh();
      });
    },
    async refresh() {
      try {
        const r = await fetch('/api/competitors/dashboard-data');
        if (r.ok) this.data = await r.json();
      } catch (e) { /* сеть моргнула — обновимся в следующий тик */ }
    },
    async forceSync() {
      this.syncing = true;
      try {
        const r = await fetch('/api/competitors/sync', {
          method: 'POST',
          headers: {'X-CSRFToken': document.querySelector('meta[name=csrf-token]').content},
        });
        const j = await r.json();
        if (j.scheduled) Alpine.store('toasts').add(
          {type: 'success', text: 'Синхронизация запустится в течение минуты'});
      } finally { this.syncing = false; }
    },
  };
}
```

(точную сигнатуру `$store.toasts.add` посмотреть в `static/sh-ui.js` и использовать её).

- [ ] **Step 2: Переработать `competitors_groups.html`**

1. Таблица/список групп `.sh-table`: имя+точка цвета, описание, число товаров, own-товар (название или «—»), статус импорта (`chip` «Импорт каталога…» если `import_requested`), действия (редактировать/удалить с confirm).
2. Модалка создания/редактирования (паттерн оверлея с `x-trap.noscroll.inert`, панель выше `.sh-backdrop`): имя, описание, цвет (8 свотчей), **привязка own-товара**: input с debounce-поиском по `GET /api/products/search?q=` (тот же endpoint, что ⌘K), список результатов, выбранный товар показывается chip-ом с крестиком.
3. Alpine: `competitorGroups()` c `searchOwnProduct(q)` (debounce 300мс, AbortController), `saveGroup()` (POST/PUT с CSRF), `deleteGroup(id)` (confirm через существующий паттерн подтверждения).
4. Удалить все inline-hex статусов; цвет группы — только пользовательский `g.color` на точке.

- [ ] **Step 3: Проверить обе темы и состояния**

Открыть страницы на scratch-БД (см. Task 12 Step 2), переключить тему тумблером, проверить: пустое состояние (нет групп), загрузку, ошибку sync (отключить сеть в devtools), мобильную ширину 375px — без горизонтального скролла.

- [ ] **Step 4: Commit**

```bash
git add templates/competitors_dashboard.html templates/competitors_groups.html
git commit -m "feat(competitors): дашборд и группы в «Тёплой редакции», привязка own-товара"
```

---

### Task 11: UI — деталь группы (график), алерты, настройки

**Files:**
- Modify: `templates/competitors_group_detail.html`
- Modify: `templates/competitors_alerts.html`
- Modify: `templates/competitors_settings.html`

**Interfaces:**
- Consumes: `GET /api/competitors/compare/<group_id>` (position-поля own), `GET /api/competitors/products/<id>/history?period=`, `POST /api/competitors/products` (nm_ids / wb_supplier_id), `GET /api/competitors/search`, `GET /api/competitors/seller-catalog`, `DELETE /api/competitors/products/<id>`, `PUT /api/competitors/settings`, `POST /api/competitors/alerts/mark-read`; `window.shChart` API (`palette/color/fade/textMuted/grid/register`), Chart.js CDN `https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js` (per-page, как `templates/analytics.html:322`).

- [ ] **Step 1: `competitors_group_detail.html`**

1. Шапка: имя группы, chip импорта, если `import_requested`; блок own-товара, если привязан: «Ваш товар: <title> — <цена> ₽ · позиция N из M · +X% к минимуму» (данные из `/api/competitors/compare/<id>` при загрузке).
2. Таблица товаров `.sh-table`, отсортирована по цене: фото (mini), название (ссылка на wildberries.ru), цена/скидка, рейтинг+отзывы, остаток, **свежесть цены**: если `last_price_at` старше 24ч — chip `цена от <дата>` (`var(--warn)`), если `price_miss_count > 0` и цены нет — chip «нет данных» (`var(--text-muted)`); строка own-товара закреплена сверху и выделена (`.sh-row-own` с `background: var(--accent-light)`).
3. Строки «ожидает данных» (без метаданных): skeleton-заглушка вместо цены.
4. Модалка «Добавить товары» — три вкладки `.sh-segmented`:
   - «По артикулам»: textarea, клиентский парсинг в int-список (`/[\s,;]+/`, `Number.isInteger`, cap 300, невалидные подсвечиваются), POST nm_ids;
   - «Поиск»: input + кнопка, `GET /api/competitors/search?q=`; 503 → toast «WB ограничивает запросы, повторите позже»; результаты с чекбоксами → выбранные добавляются как nm_ids;
   - «Каталог продавца»: input supplier_id + превью первой страницы (`GET /api/competitors/seller-catalog`), кнопка «Импортировать весь каталог» → POST `{wb_supplier_id}` → toast «Импорт запланирован, товары появятся в течение нескольких минут».
5. Модалка истории цен: canvas + Chart.js с shChart-паттерном (скопировать структуру из `templates/analytics.html:533-575`):

```javascript
const P = window.shChart;
this.chart = P.register(new Chart(this.$refs.historyChart, {
  type: 'line',
  data: { labels, datasets: [
    { label: 'Цена со скидкой', data: salePrices, borderColor: P.color(1),
      backgroundColor: P.fade(P.color(1), 0.08), fill: true, tension: 0.3,
      borderWidth: 2, pointRadius: 2, pointHoverRadius: 5, spanGaps: true },
    { label: 'Цена без скидки', data: basePrices, borderColor: P.color(3),
      borderDash: [4, 4], borderWidth: 1.5, pointRadius: 0, fill: false },
    ...(ownPrice ? [{ label: 'Ваша цена', data: labels.map(() => ownPrice),
      borderColor: P.color(5), borderDash: [8, 4], borderWidth: 1.5,
      pointRadius: 0, fill: false }] : []),
  ]},
  options: { responsive: true, maintainAspectRatio: false,
    interaction: {mode: 'index', intersect: false},
    plugins: { legend: {labels: {color: P.textMuted()}},
               tooltip: {backgroundColor: P._read('--bg-sidebar', '#0a0a0a'),
                         cornerRadius: 8, padding: 12} },
    scales: { x: {grid: {display: false}, ticks: {color: P.textMuted()}},
              y: {grid: {color: P.grid()}, ticks: {color: P.textMuted(),
                  callback: v => v + ' ₽'}} } },
}), (c, p) => {
  c.data.datasets.forEach((ds, i) => {
    const ci = [1, 3, 5][i] || 1;
    ds.borderColor = p.color(ci);
    if (ds.backgroundColor) ds.backgroundColor = p.fade(p.color(ci), 0.08);
  });
  c.options.scales.y.grid.color = p.grid();
  c.options.scales.x.ticks.color = p.textMuted();
  c.options.scales.y.ticks.color = p.textMuted();
});
```

Удалить захардкоженные `#3B82F6`/`#9CA3AF`.

- [ ] **Step 2: `competitors_alerts.html`**

Оставить SSR-пагинацию; переработать разметку: фильтры-чипы по типу/severity (`.sh-chip`, query-параметры), список `.sh-card` строк с `status_icon` (critical → `var(--danger)`, warning → `var(--warn)`, info → `var(--info)`), кнопка «Прочитать все» с CSRF, относительное время (даты naive-UTC — в JS добавлять `Z` при парсе). Empty state через макрос `empty_state`.

- [ ] **Step 3: `competitors_settings.html`**

Форма `.sh-card` секциями:
1. «Мониторинг»: toggle `is_enabled`; select интервала (30 мин / 1 ч / 2 ч / 4 ч / 12 ч / 24 ч → значения 30/60/120/240/720/1440); `max_products` (число, max 1000).
2. «Алерты»: `price_change_alert_percent` (число, шаг 0.5), `discount_alert_pp` (число).
3. «Прокси»: если `settings.proxy.is_set` — показать `settings.proxy.masked` + chip «логин/пароль заданы» + кнопка «Заменить»/«Удалить»; input нового значения `type=password` с подсказкой формата `http://user:pass@host:port`; 400 от сервера (нет ENCRYPTION_KEY) — показать текст ошибки.
4. «Статус»: last_sync_at/status/error, next_sync_due_at, длительность цикла, циклов всего. Никакого `requests_per_minute` и `pause_between_cycles_seconds` в UI.
Сохранение — PUT с CSRF, toast об успехе; ответ сервера перезаписывает форму (нормализованные значения видны сразу).

- [ ] **Step 4: Проверка тем/состояний**

Как в Task 10 Step 3: обе темы, mobile 375px, empty (группа без товаров), ошибка 503 поиска, отсутствие горизонтального скролла. График: переключить тему при открытой модалке — цвета перекрашиваются (это делает `P.register` recolor).

- [ ] **Step 5: Commit**

```bash
git add templates/competitors_group_detail.html templates/competitors_alerts.html templates/competitors_settings.html
git commit -m "feat(competitors): деталь группы с графиком на chart-токенах, алерты, настройки v2"
```

---

### Task 12: Финальная верификация

- [ ] **Step 1: Полный тестовый прогон**

```bash
SKIP_SCHEDULER=1 python -m pytest -q tests/test_competitor_v2_migration.py \
  tests/test_competitor_proxy_credential.py tests/test_competitor_fetch.py \
  tests/test_competitor_sync.py tests/test_competitor_compaction.py \
  tests/test_competitor_routes.py
SKIP_SCHEDULER=1 python -m pytest -q   # полный прогон: новых падений нет
python -m py_compile services/competitor_monitor.py services/competitor_fetch.py \
  routes/competitors.py models.py services/product_sync_scheduler.py \
  migrations/migrate_competitor_monitor_v2.py migrations/migrate_compact_competitor_snapshots.py
git diff --check
```

Expected: все конкурентские тесты passed; полный прогон — не хуже базового (существующие падения, не связанные с фичей, зафиксировать в отчёте).

- [ ] **Step 2: Живой смоук на scratch-БД**

По рецепту из памяти (`project_local_env`): скопировать прод-БД в scratch, прогнать обе миграции вручную, поднять локальный сервер и пройти путь: `/competitors` → создать группу → добавить nm_ids → увидеть «ожидает данных» → форс-синк (или дождаться тика при запущенном scheduler) → проверить страницы alerts/settings/деталь группы, обе темы. Проверить, что миграция чистки на копии прод-БД реально удаляет мусор (счётчики в логе) и укладывается в бюджет.

- [ ] **Step 3: Итоговый отчёт**

Свести: что сделано, что удалено, результаты тестов, результат чистки на копии прода, план деплоя (rebuild, миграции fail-fast на старте, ручной VACUUM опционально после чистки, снятие старых тредов рестартом).
