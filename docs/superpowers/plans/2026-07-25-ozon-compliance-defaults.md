# Админские compliance-дефолты Ozon (ТН ВЭД + маркировка) — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Убрать ручной ввод двух обязательных Ozon-атрибутов — `22232` «ТН ВЭД коды ЕАЭС» и `23536` «Нужен код маркировки» — заменив его одним админским решением на Ozon product type и версионированным нормативным реестром маркировки.

**Architecture:** Три admin-owned таблицы хранят подписанное решение по ТН ВЭД (ключ — `product_type_id`, хранится строка кода) и версионированный перечень маркируемых групп кодов. Чистый резолвер `services/ozon_compliance_defaults.py` разрешает код в `dictionary_value_id` свежего type-scoped словаря и выводит флаг маркировки longest-prefix матчем по активной версии реестра. Слой применения вызывается в хвосте `MarketplaceDraftService._auto_map_attributes` — после source-рецептов, только в пустые атрибуты, с provenance. Ни один путь не вызывает Ozon и не инициирует provider write.

**Tech Stack:** Python 3.11, Flask, SQLAlchemy (не Alembic — идемпотентные скрипты в `migrations/`), Jinja2 + Alpine.js + Tailwind CDN, pytest/unittest.

## Global Constraints

- Спека: `docs/superpowers/specs/2026-07-25-ozon-compliance-defaults-design.md`. При расхождении плана и спеки — прав план, спеку обновить.
- ID атрибутов: ТН ВЭД `"22232"`, маркировка `"23536"`. `MarketplaceAttributeDefinition.external_attribute_id` — `db.String(100)`, сравнивать строками.
- Форма элемента списка атрибутов черновика: `{"attribute_id": <str>, "complex_id": <str>, "values": [{"dictionary_value_id": <str>, "value": <str>}]}`; для значения без словаря — `{"value": <str>}`.
- Ни один новый код не вызывает Ozon API и не создаёт `MarketplaceOperation`.
- Любой констрейнт новой таблицы объявляется И в модели (`__table_args__`), И в миграции. `docker-entrypoint.sh` вызывает `db.create_all()` до скриптов из `migrations/`, поэтому таблицу почти всегда создаёт ORM, а `CREATE TABLE IF NOT EXISTS` миграции становится no-op; в SQLite CHECK нельзя добавить через `ALTER TABLE`, так что объявленный только в миграции констрейнт не применится нигде.
- Seller-edited значение атрибута не перезаписывается никогда.
- Отсутствующий/несвежий/неоднозначный источник — fail-closed: не ставим ничего, черновик остаётся `blocked` с явной причиной.
- Свежесть словаря проверять только через `OzonReferenceService.dictionary_is_fresh(attribute)`; свежесть типа — `OzonReferenceService.reference_is_fresh(product_type)`.
- Все новые тесты запускать с `SKIP_SCHEDULER=1`.
- Venv в этом репозитории — `./venv`, не `.venv`.
- Перед завершением каждой задачи: `./venv/bin/python -m py_compile <изменённые .py>` и `git diff --check`.
- UI — только токены «Тёплой редакции» и существующие `.sh-*` компоненты, обе темы, радиусы карточек ≤ 8px, никаких инлайновых `#hex` для статусов.

---

### Task 1: Модели и идемпотентная миграция

**Files:**
- Modify: `models.py` (добавить три класса после `class MarketplaceAttributeValue`)
- Create: `migrations/migrate_add_ozon_compliance_defaults.py`
- Test: `tests/test_ozon_compliance_defaults.py`

**Interfaces:**
- Consumes: существующие `Marketplace`, `MarketplaceProductType`, `User`.
- Produces: `OzonComplianceDefault`, `OzonMarkingRegistryVersion`, `OzonMarkingRule` — используются во всех последующих задачах.

- [ ] **Step 1: Написать падающий тест на модели**

Создать `tests/test_ozon_compliance_defaults.py`:

```python
# -*- coding: utf-8 -*-
"""Контракт админских compliance-дефолтов Ozon."""
import unittest


class ComplianceModelsTestCase(unittest.TestCase):
    def test_models_expose_admin_owned_decision_fields(self):
        from models import (
            OzonComplianceDefault,
            OzonMarkingRegistryVersion,
            OzonMarkingRule,
        )

        default_columns = set(OzonComplianceDefault.__table__.columns.keys())
        self.assertLessEqual(
            {
                'marketplace_id', 'product_type_id', 'tnved_code',
                'tnved_display', 'status', 'decided_by_user_id', 'decided_at',
                'rationale', 'dictionary_version', 'dictionary_hash', 'version',
            },
            default_columns,
        )

        version_columns = set(
            OzonMarkingRegistryVersion.__table__.columns.keys()
        )
        self.assertLessEqual(
            {
                'label', 'is_complete', 'declared_by_user_id', 'declared_at',
                'rule_count', 'checksum', 'status',
            },
            version_columns,
        )

        rule_columns = set(OzonMarkingRule.__table__.columns.keys())
        self.assertLessEqual(
            {
                'registry_version_id', 'code_prefix', 'normative_ref',
                'valid_from', 'note',
            },
            rule_columns,
        )


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_defaults.py`
Expected: FAIL — `ImportError: cannot import name 'OzonComplianceDefault' from 'models'`

- [ ] **Step 3: Добавить модели в `models.py`**

Вставить сразу после класса `MarketplaceAttributeValue` (найти его концом класса, перед следующим `class `):

```python
class OzonComplianceDefault(db.Model):
    """Подписанное админом решение по ТН ВЭД для одного Ozon product type.

    Хранится строка кода, а не ``external_value_id``: ID принадлежит scope
    конкретного attribute/type и может смениться при пересинхронизации
    словаря.  Код переживает ресинк и является тем, что решил человек.
    """
    __tablename__ = 'ozon_compliance_defaults'

    id = db.Column(db.Integer, primary_key=True)
    marketplace_id = db.Column(
        db.Integer, db.ForeignKey('marketplaces.id'),
        nullable=False, index=True,
    )
    product_type_id = db.Column(
        db.Integer, db.ForeignKey('marketplace_product_types.id'),
        nullable=False, index=True,
    )
    tnved_code = db.Column(db.String(20), nullable=False)
    tnved_display = db.Column(db.String(500))
    status = db.Column(db.String(20), default='active', nullable=False)
    decided_by_user_id = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False,
    )
    decided_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    rationale = db.Column(db.Text, nullable=False)
    dictionary_version = db.Column(db.Integer)
    dictionary_hash = db.Column(db.String(64))
    version = db.Column(db.Integer, default=1, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
    )

    product_type = db.relationship('MarketplaceProductType')

    # КРИТИЧНО: constraints обязаны быть и здесь, и в миграции. entrypoint
    # вызывает db.create_all() ДО скриптов из migrations/, поэтому таблицу
    # почти всегда создаёт ORM, а CREATE TABLE IF NOT EXISTS миграции потом
    # становится no-op. В SQLite CHECK нельзя добавить через ALTER TABLE —
    # объявленный только в миграции констрейнт не применится никогда.
    __table_args__ = (
        db.CheckConstraint(
            "status IN ('active', 'retired')",
            name='ck_ozon_compliance_default_status',
        ),
        db.Index(
            'uq_ozon_compliance_default_active',
            'marketplace_id',
            'product_type_id',
            unique=True,
            sqlite_where=db.text("status = 'active'"),
            postgresql_where=db.text("status = 'active'"),
        ),
    )

    __mapper_args__ = {'version_id_col': version}

    def __repr__(self):
        return (
            f'<OzonComplianceDefault type={self.product_type_id} '
            f'code={self.tnved_code} status={self.status}>'
        )


class OzonMarkingRegistryVersion(db.Model):
    """Версия нормативного перечня маркируемых групп кодов ТН ВЭД."""
    __tablename__ = 'ozon_marking_registry_versions'

    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(200), nullable=False)
    is_complete = db.Column(db.Boolean, default=False, nullable=False)
    declared_by_user_id = db.Column(
        db.Integer, db.ForeignKey('users.id'), nullable=False,
    )
    declared_at = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False,
    )
    rule_count = db.Column(db.Integer, default=0, nullable=False)
    checksum = db.Column(db.String(64))
    status = db.Column(db.String(20), default='superseded', nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
    )

    rules = db.relationship(
        'OzonMarkingRule', backref='registry_version', lazy='dynamic',
    )

    # Та же причина, что у OzonComplianceDefault: ORM и миграция обязаны
    # давать идентичный набор констрейнтов.
    __table_args__ = (
        db.CheckConstraint(
            "status IN ('active', 'superseded')",
            name='ck_ozon_marking_registry_status',
        ),
        db.Index(
            'uq_ozon_marking_registry_active',
            'status',
            unique=True,
            sqlite_where=db.text("status = 'active'"),
            postgresql_where=db.text("status = 'active'"),
        ),
    )

    def __repr__(self):
        return (
            f'<OzonMarkingRegistryVersion {self.label} '
            f'status={self.status} complete={self.is_complete}>'
        )


class OzonMarkingRule(db.Model):
    """Одна строка перечня: префикс кода ТН ВЭД, подлежащий маркировке."""
    __tablename__ = 'ozon_marking_rules'

    id = db.Column(db.Integer, primary_key=True)
    registry_version_id = db.Column(
        db.Integer, db.ForeignKey('ozon_marking_registry_versions.id'),
        nullable=False, index=True,
    )
    code_prefix = db.Column(db.String(20), nullable=False)
    normative_ref = db.Column(db.String(300))
    valid_from = db.Column(db.Date)
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint(
            'registry_version_id', 'code_prefix',
            name='uq_ozon_marking_rule_scope',
        ),
    )

    def __repr__(self):
        return f'<OzonMarkingRule {self.code_prefix}>'
```

- [ ] **Step 4: Убедиться, что тест проходит**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_defaults.py`
Expected: PASS

- [ ] **Step 5: Написать миграцию**

Создать `migrations/migrate_add_ozon_compliance_defaults.py`. Скопировать структуру хелперов (`_tables`, `_columns`, argparse, `main`) из `migrations/migrate_add_ozon_product_type_visibility.py`, добавив:

```python
CREATE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS ozon_compliance_defaults (
        id INTEGER PRIMARY KEY,
        marketplace_id INTEGER NOT NULL REFERENCES marketplaces(id),
        product_type_id INTEGER NOT NULL
            REFERENCES marketplace_product_types(id),
        tnved_code VARCHAR(20) NOT NULL,
        tnved_display VARCHAR(500),
        status VARCHAR(20) NOT NULL DEFAULT 'active',
        decided_by_user_id INTEGER NOT NULL REFERENCES users(id),
        decided_at DATETIME NOT NULL,
        rationale TEXT NOT NULL,
        dictionary_version INTEGER,
        dictionary_hash VARCHAR(64),
        version INTEGER NOT NULL DEFAULT 1,
        created_at DATETIME NOT NULL,
        updated_at DATETIME,
        CHECK (status IN ('active', 'retired'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ozon_marking_registry_versions (
        id INTEGER PRIMARY KEY,
        label VARCHAR(200) NOT NULL,
        is_complete BOOLEAN NOT NULL DEFAULT 0,
        declared_by_user_id INTEGER NOT NULL REFERENCES users(id),
        declared_at DATETIME NOT NULL,
        rule_count INTEGER NOT NULL DEFAULT 0,
        checksum VARCHAR(64),
        status VARCHAR(20) NOT NULL DEFAULT 'superseded',
        created_at DATETIME NOT NULL,
        updated_at DATETIME,
        CHECK (status IN ('active', 'superseded'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ozon_marking_rules (
        id INTEGER PRIMARY KEY,
        registry_version_id INTEGER NOT NULL
            REFERENCES ozon_marking_registry_versions(id),
        code_prefix VARCHAR(20) NOT NULL,
        normative_ref VARCHAR(300),
        valid_from DATE,
        note TEXT,
        created_at DATETIME NOT NULL,
        UNIQUE (registry_version_id, code_prefix)
    )
    """,
)

INDEX_STATEMENTS = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_ozon_compliance_default_active
    ON ozon_compliance_defaults (marketplace_id, product_type_id)
    WHERE status = 'active'
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_ozon_marking_registry_active
    ON ozon_marking_registry_versions (status)
    WHERE status = 'active'
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_ozon_marking_rules_prefix
    ON ozon_marking_rules (code_prefix)
    """,
)
```

Модуль обязан экспортировать публичную `apply_migration(db_path)` — её вызывает и `main()`, и тест из Step 6:

```python
MANAGED_TABLES = {
    "ozon_compliance_defaults",
    "ozon_marking_registry_versions",
    "ozon_marking_rules",
}


def apply_migration(db_path) -> None:
    connection = sqlite3.connect(str(db_path))
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        baseline = {
            tuple(row)
            for row in connection.execute("PRAGMA foreign_key_check").fetchall()
        }
        for statement in CREATE_STATEMENTS:
            connection.execute(statement)
        for statement in INDEX_STATEMENTS:
            connection.execute(statement)

        violations = {
            tuple(row)
            for row in connection.execute("PRAGMA foreign_key_check").fetchall()
        }
        new_violations = violations - baseline
        relevant = [
            row for row in new_violations
            if str(row[0]) in MANAGED_TABLES or str(row[2]) in MANAGED_TABLES
        ]
        if relevant:
            connection.rollback()
            raise RuntimeError(
                f"Миграция создала нарушения внешних ключей: {relevant}"
            )
        connection.commit()
        logger.info("Compliance-таблицы Ozon готовы")
    finally:
        connection.close()


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
    apply_migration(db_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Backfill отсутствует: существующие черновики не изменяются молча.

- [ ] **Step 6: Тест идемпотентности миграции**

Дописать в `tests/test_ozon_compliance_defaults.py`:

```python
class ComplianceMigrationTestCase(unittest.TestCase):
    def test_migration_is_idempotent(self):
        import sqlite3
        import tempfile
        import os
        from migrations.migrate_add_ozon_compliance_defaults import (
            apply_migration,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'test.db')
            con = sqlite3.connect(path)
            con.executescript(
                "CREATE TABLE marketplaces (id INTEGER PRIMARY KEY);"
                "CREATE TABLE users (id INTEGER PRIMARY KEY);"
                "CREATE TABLE marketplace_product_types (id INTEGER PRIMARY KEY);"
            )
            con.commit()
            con.close()

            apply_migration(path)
            apply_migration(path)

            con = sqlite3.connect(path)
            tables = {
                row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            con.close()
            self.assertIn('ozon_compliance_defaults', tables)
            self.assertIn('ozon_marking_registry_versions', tables)
            self.assertIn('ozon_marking_rules', tables)
```

`apply_migration(db_path)` — публичная функция миграции, которую вызывает `main()`.

- [ ] **Step 7: Прогнать тесты**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_defaults.py`
Expected: PASS (2 теста)

- [ ] **Step 8: Проверки и коммит**

```bash
./venv/bin/python -m py_compile models.py migrations/migrate_add_ozon_compliance_defaults.py
git diff --check
git add models.py migrations/migrate_add_ozon_compliance_defaults.py tests/test_ozon_compliance_defaults.py
git commit -m "feat(ozon): модели и миграция админских compliance-дефолтов"
```

---

### Task 2: Резолвер ТН ВЭД

**Files:**
- Create: `services/ozon_compliance_defaults.py`
- Test: `tests/test_ozon_compliance_defaults.py` (дописать)

**Interfaces:**
- Consumes: `OzonComplianceDefault` (Task 1), `OzonReferenceService.dictionary_is_fresh`.
- Produces:
  - `TNVED_ATTRIBUTE_ID = "22232"`, `MARKING_ATTRIBUTE_ID = "23536"`
  - `normalize_code(value) -> str` — только цифры
  - `dictionary_code(value) -> str` — ведущие цифры значения словаря
  - `resolve_tnved(product_type_id) -> dict | None` с ключами `code`, `value`, `external_value_id`, `default_id`, `dictionary_version`. Ключ называется именно `value` (не `display`) — так его читает `apply_to_attributes` в Task 4.

- [ ] **Step 1: Написать падающие тесты нормализации и разбора**

```python
class TnvedCodeParsingTestCase(unittest.TestCase):
    def test_normalize_keeps_digits_only(self):
        from services.ozon_compliance_defaults import normalize_code
        self.assertEqual(normalize_code(' 3307 90 000 8 '), '3307900008')
        self.assertEqual(normalize_code('6402-99'), '640299')
        self.assertEqual(normalize_code(''), '')
        self.assertEqual(normalize_code(None), '')

    def test_dictionary_code_takes_leading_digits(self):
        from services.ozon_compliance_defaults import dictionary_code
        self.assertEqual(
            dictionary_code('3307900008 - Косметические средства'),
            '3307900008',
        )
        self.assertEqual(dictionary_code('6402990000'), '6402990000')
        self.assertEqual(dictionary_code('Без кода'), '')
        self.assertEqual(dictionary_code(None), '')
```

- [ ] **Step 2: Убедиться, что падает**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_defaults.py::TnvedCodeParsingTestCase`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.ozon_compliance_defaults'`

- [ ] **Step 3: Создать модуль с чистыми функциями**

```python
# -*- coding: utf-8 -*-
"""Админские compliance-дефолты Ozon: ТН ВЭД и признак маркировки.

Ozon требует два обязательных атрибута, которые платформа принципиально не
имеет права выводить из фактов товара: `22232` («ТН ВЭД коды ЕАЭС») и `23536`
(«Нужен код маркировки»).  Единственный допустимый источник ТН ВЭД —
подписанное админом решение, привязанное к Ozon product type; единственный
допустимый источник признака маркировки — активная версия нормативного
перечня, применённая к этому коду.

Модуль чистый: только SQL и строки.  Ни provider-вызовов, ни LLM.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

TNVED_ATTRIBUTE_ID = "22232"
MARKING_ATTRIBUTE_ID = "23536"

_DIGITS = re.compile(r"\d+")
_LEADING_DIGITS = re.compile(r"^\s*(\d+)")


def normalize_code(value: Any) -> str:
    """Свести код ТН ВЭД к последовательности цифр."""
    if value is None:
        return ""
    return "".join(_DIGITS.findall(str(value)))


def dictionary_code(value: Any) -> str:
    """Извлечь код из значения официального словаря.

    Наблюдённая форма — ``"3307900008 - Косметические средства ..."``.
    Кодом считается ведущая непрерывная последовательность цифр; всё, что
    после неё, игнорируется.  Значение без ведущих цифр кандидатом не является.
    """
    if value is None:
        return ""
    match = _LEADING_DIGITS.match(str(value))
    return match.group(1) if match else ""
```

- [ ] **Step 4: Прогнать тесты разбора**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_defaults.py::TnvedCodeParsingTestCase`
Expected: PASS (2 теста)

- [ ] **Step 5: Написать падающий тест резолвера**

```python
from unittest.mock import MagicMock, patch


def _definition(*, fresh=True, available=True):
    defn = MagicMock()
    defn.id = 678
    defn.external_attribute_id = '22232'
    defn.is_available = available
    defn.dictionary_id = '124412395'
    defn.values_version = 3
    defn.restriction_value_ids = []
    return defn


def _value_row(external_value_id, value):
    row = MagicMock()
    row.external_value_id = external_value_id
    row.value = value
    return row


class ResolveTnvedTestCase(unittest.TestCase):
    def _run(self, *, default, definition, rows, fresh=True):
        module = 'services.ozon_compliance_defaults'
        with patch(f'{module}._active_default', return_value=default), \
             patch(f'{module}._tnved_definition', return_value=definition), \
             patch(f'{module}._dictionary_rows', return_value=rows), \
             patch(f'{module}._dictionary_is_fresh', return_value=fresh):
            from services.ozon_compliance_defaults import resolve_tnved
            return resolve_tnved(1609)

    def _default(self, code='3307900008'):
        obj = MagicMock()
        obj.id = 7
        obj.tnved_code = code
        obj.dictionary_version = 3
        return obj

    def test_exact_single_match_resolves_value_id(self):
        result = self._run(
            default=self._default(),
            definition=_definition(),
            rows=[
                _value_row('971397774', '3403990000 - Прочие смазочные'),
                _value_row('971397758', '3307900008 - Косметические средства'),
            ],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result['code'], '3307900008')
        self.assertEqual(result['external_value_id'], '971397758')
        self.assertEqual(result['value'], '3307900008 - Косметические средства')

    def test_missing_decision_returns_none(self):
        self.assertIsNone(
            self._run(default=None, definition=_definition(), rows=[])
        )

    def test_stale_dictionary_returns_none(self):
        self.assertIsNone(self._run(
            default=self._default(),
            definition=_definition(),
            rows=[_value_row('1', '3307900008 - X')],
            fresh=False,
        ))

    def test_code_absent_from_dictionary_returns_none(self):
        self.assertIsNone(self._run(
            default=self._default(code='9999999999'),
            definition=_definition(),
            rows=[_value_row('1', '3307900008 - X')],
        ))

    def test_duplicate_code_in_dictionary_returns_none(self):
        self.assertIsNone(self._run(
            default=self._default(),
            definition=_definition(),
            rows=[
                _value_row('1', '3307900008 - X'),
                _value_row('2', '3307900008 - Y'),
            ],
        ))
```

- [ ] **Step 6: Убедиться, что падает**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_defaults.py::ResolveTnvedTestCase`
Expected: FAIL — `cannot import name 'resolve_tnved'`

- [ ] **Step 7: Реализовать резолвер**

Дописать в `services/ozon_compliance_defaults.py`:

```python
def _active_default(product_type_id: int):
    from models import OzonComplianceDefault
    return OzonComplianceDefault.query.filter_by(
        product_type_id=product_type_id, status="active",
    ).first()


def _tnved_definition(product_type_id: int):
    from models import MarketplaceAttributeDefinition
    return MarketplaceAttributeDefinition.query.filter_by(
        product_type_id=product_type_id,
        external_attribute_id=TNVED_ATTRIBUTE_ID,
    ).first()


def _dictionary_rows(definition) -> list:
    from models import MarketplaceAttributeValue
    return MarketplaceAttributeValue.query.filter_by(
        attribute_id=definition.id, is_available=True,
    ).all()


def _dictionary_is_fresh(definition) -> bool:
    from services.ozon_reference_service import OzonReferenceService
    return bool(OzonReferenceService.dictionary_is_fresh(definition))


def resolve_tnved(product_type_id: Any) -> Optional[dict]:
    """Разрешить админский код ТН ВЭД в значение свежего словаря типа.

    Возвращает ``None`` при отсутствии решения, несвежем словаре, отсутствии
    кода в словаре и при более чем одном совпадении.  Вызывающий код обязан
    трактовать ``None`` как fail-closed и ничего не записывать.
    """
    try:
        type_key = int(product_type_id)
    except (TypeError, ValueError):
        return None

    default = _active_default(type_key)
    if default is None:
        return None
    code = normalize_code(default.tnved_code)
    if not code:
        return None

    definition = _tnved_definition(type_key)
    if definition is None or not definition.is_available:
        return None
    if not _dictionary_is_fresh(definition):
        return None

    restriction = set(getattr(definition, "restriction_value_ids", None) or [])
    matches = [
        row for row in _dictionary_rows(definition)
        if dictionary_code(row.value) == code
        and (not restriction or row.external_value_id in restriction)
    ]
    if len(matches) != 1:
        if matches:
            logger.warning(
                "Код ТН ВЭД %s неоднозначен в словаре типа %s (%s совпадений)",
                code, type_key, len(matches),
            )
        return None

    row = matches[0]
    return {
        "code": code,
        "value": row.value,
        "external_value_id": row.external_value_id,
        "default_id": default.id,
        "dictionary_version": getattr(definition, "values_version", None),
    }
```

- [ ] **Step 8: Прогнать тесты резолвера**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_defaults.py`
Expected: PASS (все тесты файла)

- [ ] **Step 9: Проверки и коммит**

```bash
./venv/bin/python -m py_compile services/ozon_compliance_defaults.py
git diff --check
git add services/ozon_compliance_defaults.py tests/test_ozon_compliance_defaults.py
git commit -m "feat(ozon): резолвер админского ТН ВЭД по свежему словарю типа"
```

---

### Task 3: Вывод маркировки и сводный резолвер

**Files:**
- Modify: `services/ozon_compliance_defaults.py`
- Test: `tests/test_ozon_compliance_defaults.py` (дописать)

**Interfaces:**
- Consumes: `normalize_code`, `resolve_tnved` (Task 2), `OzonMarkingRegistryVersion`, `OzonMarkingRule` (Task 1).
- Produces:
  - `resolve_marking(tnved_code) -> Optional[bool]`
  - `resolve_type_defaults(product_type_id) -> dict` с ключами `tnved`, `marking`, `unresolved`, `evidence`

- [ ] **Step 1: Написать падающие тесты вывода маркировки**

```python
class ResolveMarkingTestCase(unittest.TestCase):
    def _rules(self, prefixes):
        rows = []
        for prefix in prefixes:
            row = MagicMock()
            row.code_prefix = prefix
            rows.append(row)
        return rows

    def _run(self, code, *, prefixes, is_complete, has_version=True):
        module = 'services.ozon_compliance_defaults'
        version = None
        if has_version:
            version = MagicMock()
            version.id = 1
            version.is_complete = is_complete
        with patch(f'{module}._active_registry_version', return_value=version), \
             patch(f'{module}._registry_rules',
                   return_value=self._rules(prefixes)):
            from services.ozon_compliance_defaults import resolve_marking
            return resolve_marking(code)

    def test_prefix_match_requires_marking(self):
        self.assertIs(
            self._run('6402990000', prefixes=['6402'], is_complete=True),
            True,
        )

    def test_longest_prefix_wins(self):
        self.assertIs(
            self._run('6402990000', prefixes=['64', '6402990000'],
                      is_complete=False),
            True,
        )

    def test_complete_registry_without_match_means_false(self):
        self.assertIs(
            self._run('3307900008', prefixes=['6402'], is_complete=True),
            False,
        )

    def test_incomplete_registry_without_match_is_unresolved(self):
        self.assertIsNone(
            self._run('3307900008', prefixes=['6402'], is_complete=False)
        )

    def test_no_active_version_is_unresolved(self):
        self.assertIsNone(
            self._run('3307900008', prefixes=[], is_complete=True,
                      has_version=False)
        )

    def test_empty_code_is_unresolved(self):
        self.assertIsNone(
            self._run('', prefixes=['6402'], is_complete=True)
        )
```

- [ ] **Step 2: Убедиться, что падает**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_defaults.py::ResolveMarkingTestCase`
Expected: FAIL — `cannot import name 'resolve_marking'`

- [ ] **Step 3: Реализовать вывод маркировки и сводный резолвер**

```python
def _active_registry_version():
    from models import OzonMarkingRegistryVersion
    return OzonMarkingRegistryVersion.query.filter_by(status="active").first()


def _registry_rules(version) -> list:
    from models import OzonMarkingRule
    return OzonMarkingRule.query.filter_by(
        registry_version_id=version.id,
    ).all()


def resolve_marking(tnved_code: Any) -> Optional[bool]:
    """Вывести признак маркировки из кода по активной версии перечня.

    ``True``  — код попал в перечень маркируемых групп (longest-prefix).
    ``False`` — не попал, но перечень объявлен исчерпывающим.
    ``None``  — ответа нет: перечень отсутствует либо не объявлен полным.
    """
    code = normalize_code(tnved_code)
    if not code:
        return None

    version = _active_registry_version()
    if version is None:
        return None

    best = ""
    for rule in _registry_rules(version):
        prefix = normalize_code(rule.code_prefix)
        if not prefix or not code.startswith(prefix):
            continue
        if len(prefix) > len(best):
            best = prefix

    if best:
        return True
    return False if bool(version.is_complete) else None


def resolve_type_defaults(product_type_id: Any) -> dict:
    """Свести оба compliance-значения для одного Ozon product type."""
    unresolved: list = []
    evidence: dict = {}

    tnved = resolve_tnved(product_type_id)
    if tnved is None:
        unresolved.append(TNVED_ATTRIBUTE_ID)
        return {
            "tnved": None,
            "marking": None,
            "unresolved": unresolved + [MARKING_ATTRIBUTE_ID],
            "evidence": evidence,
        }

    evidence["tnved_default_id"] = tnved["default_id"]
    evidence["dictionary_version"] = tnved["dictionary_version"]

    marking = resolve_marking(tnved["code"])
    if marking is None:
        unresolved.append(MARKING_ATTRIBUTE_ID)
    else:
        version = _active_registry_version()
        evidence["registry_version_id"] = (
            version.id if version is not None else None
        )

    return {
        "tnved": tnved,
        "marking": marking,
        "unresolved": unresolved,
        "evidence": evidence,
    }
```

- [ ] **Step 4: Прогнать тесты**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_defaults.py`
Expected: PASS

- [ ] **Step 5: Проверки и коммит**

```bash
./venv/bin/python -m py_compile services/ozon_compliance_defaults.py
git diff --check
git add services/ozon_compliance_defaults.py tests/test_ozon_compliance_defaults.py
git commit -m "feat(ozon): вывод признака маркировки из ТН ВЭД по нормативному перечню"
```

---

### Task 4: Слой применения в сборке черновика

**Files:**
- Modify: `services/ozon_compliance_defaults.py` (добавить `apply_to_attributes`)
- Modify: `services/marketplace_drafts.py` — хвост `_auto_map_attributes` (строка `return result`, ~3479)
- Test: `tests/test_ozon_compliance_defaults.py` (дописать)

**Interfaces:**
- Consumes: `resolve_type_defaults` (Task 3).
- Produces: `apply_to_attributes(attributes, product_type_id) -> tuple[list, dict]` — возвращает новый список атрибутов и отчёт `{"applied": [...], "unresolved": [...], "evidence": {...}}`.

- [ ] **Step 1: Написать падающий тест слоя применения**

```python
class ApplyComplianceDefaultsTestCase(unittest.TestCase):
    def _run(self, attributes, defaults):
        module = 'services.ozon_compliance_defaults'
        with patch(f'{module}.resolve_type_defaults', return_value=defaults):
            from services.ozon_compliance_defaults import apply_to_attributes
            return apply_to_attributes(attributes, 1609)

    def _defaults(self, marking=True):
        return {
            'tnved': {
                'code': '3307900008',
                'value': '3307900008 - Косметические средства',
                'external_value_id': '971397758',
                'default_id': 7,
                'dictionary_version': 3,
            },
            'marking': marking,
            'unresolved': [],
            'evidence': {'tnved_default_id': 7, 'registry_version_id': 1},
        }

    def test_fills_both_empty_attributes(self):
        attributes, report = self._run([], self._defaults())
        by_id = {item['attribute_id']: item for item in attributes}
        self.assertEqual(
            by_id['22232']['values'],
            [{
                'dictionary_value_id': '971397758',
                'value': '3307900008 - Косметические средства',
            }],
        )
        self.assertEqual(by_id['23536']['values'], [{'value': 'true'}])
        self.assertEqual(sorted(report['applied']), ['22232', '23536'])

    def test_false_marking_is_written_as_literal_false(self):
        attributes, _ = self._run([], self._defaults(marking=False))
        by_id = {item['attribute_id']: item for item in attributes}
        self.assertEqual(by_id['23536']['values'], [{'value': 'false'}])

    def test_existing_seller_value_is_never_overwritten(self):
        existing = [{
            'attribute_id': '22232',
            'complex_id': '0',
            'values': [{'dictionary_value_id': '1', 'value': 'Ручной код'}],
        }]
        attributes, report = self._run(existing, self._defaults())
        by_id = {item['attribute_id']: item for item in attributes}
        self.assertEqual(by_id['22232']['values'][0]['value'], 'Ручной код')
        self.assertEqual(report['applied'], ['23536'])

    def test_unresolved_marking_writes_nothing_for_that_attribute(self):
        defaults = self._defaults()
        defaults['marking'] = None
        defaults['unresolved'] = ['23536']
        attributes, report = self._run([], defaults)
        ids = {item['attribute_id'] for item in attributes}
        self.assertIn('22232', ids)
        self.assertNotIn('23536', ids)
        self.assertEqual(report['unresolved'], ['23536'])
```

- [ ] **Step 2: Убедиться, что падает**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_defaults.py::ApplyComplianceDefaultsTestCase`
Expected: FAIL — `cannot import name 'apply_to_attributes'`

- [ ] **Step 3: Реализовать слой применения**

```python
def _has_value(attributes: list, attribute_id: str) -> bool:
    for item in attributes:
        if not isinstance(item, dict):
            continue
        if str(item.get("attribute_id")) != attribute_id:
            continue
        values = item.get("values")
        if isinstance(values, list) and values:
            return True
    return False


def apply_to_attributes(attributes: Any, product_type_id: Any) -> tuple:
    """Дозаполнить compliance-атрибуты из админского решения.

    Заполняются только ПУСТЫЕ атрибуты; значение, уже присутствующее в
    черновике, не перезаписывается никогда.  При нерешённом источнике не
    записывается ничего — черновик остаётся невалидным с явной причиной.
    """
    result = list(attributes) if isinstance(attributes, list) else []
    report = {"applied": [], "unresolved": [], "evidence": {}}

    defaults = resolve_type_defaults(product_type_id)
    report["unresolved"] = list(defaults.get("unresolved") or [])
    report["evidence"] = dict(defaults.get("evidence") or {})

    tnved = defaults.get("tnved")
    if tnved and not _has_value(result, TNVED_ATTRIBUTE_ID):
        result.append({
            "attribute_id": TNVED_ATTRIBUTE_ID,
            "complex_id": "0",
            "values": [{
                "dictionary_value_id": tnved["external_value_id"],
                "value": tnved["value"],
            }],
        })
        report["applied"].append(TNVED_ATTRIBUTE_ID)

    marking = defaults.get("marking")
    if marking is not None and not _has_value(result, MARKING_ATTRIBUTE_ID):
        result.append({
            "attribute_id": MARKING_ATTRIBUTE_ID,
            "complex_id": "0",
            "values": [{"value": "true" if marking else "false"}],
        })
        report["applied"].append(MARKING_ATTRIBUTE_ID)

    return result, report
```

- [ ] **Step 4: Прогнать тесты слоя**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_defaults.py::ApplyComplianceDefaultsTestCase`
Expected: PASS (4 теста)

- [ ] **Step 5: Подключить слой в `_auto_map_attributes`**

В `services/marketplace_drafts.py` заменить финальный `return result` метода `_auto_map_attributes` (~строка 3479, сразу после блока `result.append({...})`) на:

```python
        # Compliance-атрибуты не выводятся из фактов товара — это остаётся
        # запрещённым.  Здесь применяется отдельный слой: подписанное админом
        # решение по ТН ВЭД и выведенный из него по нормативному перечню
        # признак маркировки.  Слой заполняет только пустые поля и никогда не
        # трогает уже заданное значение.
        from services.ozon_compliance_defaults import apply_to_attributes
        result, _compliance_report = apply_to_attributes(
            result, product_type.id,
        )
        return result
```

- [ ] **Step 6: Проверить, что существующие тесты черновиков не сломались**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_marketplace_drafts.py tests/test_marketplace_draft_bulk_prepare.py tests/test_marketplace_draft_routes.py tests/test_seller_ozon_preparation_ui.py`
Expected: PASS. Если какой-то тест падает из-за появления новых атрибутов — это ожидаемо только когда в тестовой БД есть активный `OzonComplianceDefault`; при пустых новых таблицах `resolve_type_defaults` возвращает `tnved=None` и слой не добавляет ничего.

- [ ] **Step 7: Прогнать полный набор**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 8: Проверки и коммит**

```bash
./venv/bin/python -m py_compile services/ozon_compliance_defaults.py services/marketplace_drafts.py
git diff --check
git add services/ozon_compliance_defaults.py services/marketplace_drafts.py tests/test_ozon_compliance_defaults.py
git commit -m "feat(ozon): применение админских compliance-дефолтов при сборке черновика"
```

---

### Task 5: Фикс кириллических ТПЭ/ТПР в подсказках ТН ВЭД

**Files:**
- Modify: `services/ozon_compliance_suggestions.py:147-159`
- Test: `tests/test_ozon_compliance_suggestions.py` — **файл уже существует (274 строки)**, дописать новый класс в конец, ничего не удаляя. Существующий `test_non_vibrating_tpr_shows_material_alternatives_not_vibration` использует `"TPR (Термопластичная резина)"` и обязан продолжать проходить.

**Interfaces:**
- Consumes: ничего нового.
- Produces: ничего нового. Меняется только поведение `_observed_signals`.

**Контекст:** проверено на живой карточке — при `material_label="ТПЭ"` получается `rubber_like=false`, `plastic_like=false`, и подсказчик возвращает ноль кандидатов из 115 значений словаря. Регексы знают латинские `tpr|tpe` и кириллические `резин*|эластомер*|термопласт*`, но не кириллические аббревиатуры.

- [ ] **Step 1: Написать падающий тест**

```python
# -*- coding: utf-8 -*-
"""Распознавание материалов в подсказках ТН ВЭД."""
import unittest


class MaterialSignalTestCase(unittest.TestCase):
    def _signals(self, material):
        from services.ozon_compliance_suggestions import (
            OzonComplianceSuggestionService as S,
        )
        return S._material_flags(material)

    def test_cyrillic_tpe_abbreviation_is_rubber_like(self):
        flags = self._signals('ТПЭ')
        self.assertTrue(flags['rubber_like'])

    def test_cyrillic_tpr_abbreviation_is_rubber_like(self):
        flags = self._signals('ТПР')
        self.assertTrue(flags['rubber_like'])

    def test_latin_tpe_still_recognized(self):
        self.assertTrue(self._signals('TPE')['rubber_like'])

    def test_unrelated_material_is_not_rubber_like(self):
        self.assertFalse(self._signals('Стекло')['rubber_like'])
```

- [ ] **Step 2: Убедиться, что падает**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_suggestions.py`
Expected: FAIL — `AttributeError: ... has no attribute '_material_flags'`

- [ ] **Step 3: Выделить `_material_flags` и добавить кириллические аббревиатуры**

В `services/ozon_compliance_suggestions.py` заменить инлайновое вычисление `rubber_like` / `plastic_like` внутри `_observed_signals` на вызов нового classmethod:

```python
    @classmethod
    def _material_flags(cls, material_signal: Any) -> dict:
        """Флаги материала. Кириллические ТПЭ/ТПР — та же аббревиатура, что
        латинские TPE/TPR: поставщики пишут её обоими алфавитами."""
        normalized = cls._normalize(material_signal)
        rubber_like = bool(
            re.search(
                r"\b(?:tpr|tpe|тпэ|тпр|тпу|резин\w*|эластомер\w*|"
                r"термоэластопласт\w*)\b",
                normalized,
            )
        )
        plastic_like = bool(
            re.search(
                r"\b(?:пластик\w*|пластмасс\w*|полимер\w*|"
                r"термопласт\w*|tpr|tpe|тпэ|тпр)\b",
                normalized,
            )
        )
        return {"rubber_like": rubber_like, "plastic_like": plastic_like}
```

В `_observed_signals` заменить два присваивания на:

```python
        material_flags = cls._material_flags(material_signal)
        rubber_like = material_flags["rubber_like"]
        plastic_like = material_flags["plastic_like"]
```

- [ ] **Step 4: Прогнать тесты**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_suggestions.py`
Expected: PASS (4 теста)

- [ ] **Step 5: Проверки и коммит**

```bash
./venv/bin/python -m py_compile services/ozon_compliance_suggestions.py
git diff --check
git add services/ozon_compliance_suggestions.py tests/test_ozon_compliance_suggestions.py
git commit -m "fix(ozon): распознавать кириллические ТПЭ/ТПР в подсказках ТН ВЭД"
```

---

### Task 6: Админский сервис управления решениями и реестром

**Files:**
- Create: `services/ozon_compliance_admin.py`
- Test: `tests/test_ozon_compliance_admin.py`

**Interfaces:**
- Consumes: модели Task 1, `resolve_marking` (Task 3).
- Produces:
  - `list_type_rows() -> list[dict]` — задействованные типы с текущим решением и вычисленной маркировкой
  - `save_decision(*, product_type_id, tnved_code, rationale, user_id) -> OzonComplianceDefault`. Отдельный `expected_version` не нужен: конкурентную правку ловит `version_id_col` модели, а сервис превращает `StaleDataError` в `OzonComplianceAdminError` с человекочитаемым текстом.
  - `activate_registry_version(*, version_id, user_id) -> OzonMarkingRegistryVersion`
  - `preview_registry_switch(version_id) -> dict` — сколько типов поменяют флаг

- [ ] **Step 1: Написать падающий тест валидации решения**

```python
# -*- coding: utf-8 -*-
"""Админский сервис compliance-дефолтов Ozon."""
import unittest


class SaveDecisionValidationTestCase(unittest.TestCase):
    def test_empty_rationale_is_rejected(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            validate_decision_input,
        )
        with self.assertRaises(OzonComplianceAdminError):
            validate_decision_input(
                product_type_id=1609, tnved_code='3307900008', rationale='  ',
            )

    def test_non_numeric_code_is_rejected(self):
        from services.ozon_compliance_admin import (
            OzonComplianceAdminError,
            validate_decision_input,
        )
        with self.assertRaises(OzonComplianceAdminError):
            validate_decision_input(
                product_type_id=1609, tnved_code='нет кода', rationale='ок',
            )

    def test_valid_input_returns_normalized_code(self):
        from services.ozon_compliance_admin import validate_decision_input
        cleaned = validate_decision_input(
            product_type_id=1609,
            tnved_code=' 3307 90 0008 ',
            rationale='Лубриканты, косметические средства',
        )
        self.assertEqual(cleaned['tnved_code'], '3307900008')
        self.assertEqual(cleaned['product_type_id'], 1609)
```

- [ ] **Step 2: Убедиться, что падает**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_admin.py`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Реализовать валидацию и сервис**

Создать `services/ozon_compliance_admin.py` c `class OzonComplianceAdminError(Exception)` и функцией:

```python
def validate_decision_input(*, product_type_id, tnved_code, rationale) -> dict:
    from services.ozon_compliance_defaults import normalize_code

    try:
        type_key = int(product_type_id)
    except (TypeError, ValueError):
        raise OzonComplianceAdminError("product_type_id должен быть числом")
    if type_key <= 0:
        raise OzonComplianceAdminError("product_type_id должен быть положительным")

    code = normalize_code(tnved_code)
    if not code:
        raise OzonComplianceAdminError(
            "Код ТН ВЭД должен содержать хотя бы одну цифру"
        )
    if len(code) > 20:
        raise OzonComplianceAdminError("Код ТН ВЭД слишком длинный")

    text = (rationale or "").strip()
    if not text:
        raise OzonComplianceAdminError("Обоснование обязательно")
    if len(text) > 2000:
        raise OzonComplianceAdminError("Обоснование слишком длинное")

    return {
        "product_type_id": type_key,
        "tnved_code": code,
        "rationale": text,
    }
```

Затем в том же модуле:

```python
def save_decision(*, product_type_id, tnved_code, rationale, user_id):
    """Заменить активное решение по типу новым, подписанным админом."""
    from models import (
        db, MarketplaceAttributeDefinition, MarketplaceProductType,
        OzonComplianceDefault,
    )
    from services.ozon_compliance_defaults import TNVED_ATTRIBUTE_ID

    cleaned = validate_decision_input(
        product_type_id=product_type_id,
        tnved_code=tnved_code,
        rationale=rationale,
    )
    product_type = MarketplaceProductType.query.get(cleaned["product_type_id"])
    if product_type is None:
        raise OzonComplianceAdminError("Ozon product type не найден")

    definition = MarketplaceAttributeDefinition.query.filter_by(
        product_type_id=product_type.id,
        external_attribute_id=TNVED_ATTRIBUTE_ID,
    ).first()

    current = OzonComplianceDefault.query.filter_by(
        product_type_id=product_type.id, status="active",
    ).first()
    if current is not None:
        current.status = "retired"

    decision = OzonComplianceDefault(
        marketplace_id=product_type.marketplace_id,
        product_type_id=product_type.id,
        tnved_code=cleaned["tnved_code"],
        tnved_display=_observed_display(definition, cleaned["tnved_code"]),
        status="active",
        decided_by_user_id=int(user_id),
        decided_at=datetime.utcnow(),
        rationale=cleaned["rationale"],
        dictionary_version=getattr(definition, "values_version", None),
        dictionary_hash=getattr(definition, "values_snapshot_hash", None),
    )
    db.session.add(decision)
    db.session.commit()
    return decision


def _observed_display(definition, code):
    """Наблюдённый display кода на момент решения — только для аудита."""
    if definition is None:
        return None
    from models import MarketplaceAttributeValue
    from services.ozon_compliance_defaults import dictionary_code

    for row in MarketplaceAttributeValue.query.filter_by(
        attribute_id=definition.id, is_available=True,
    ).all():
        if dictionary_code(row.value) == code:
            return row.value[:500]
    return None


def list_type_rows() -> list:
    """Задействованные Ozon-типы с текущим решением и выведенной маркировкой."""
    from models import db, OzonComplianceDefault
    from services.ozon_compliance_defaults import resolve_marking

    rows = db.session.execute(db.text(TYPE_ROWS_SQL)).mappings().all()
    decisions = {
        item.product_type_id: item
        for item in OzonComplianceDefault.query.filter_by(status="active").all()
    }
    result = []
    for row in rows:
        decision = decisions.get(row["id"])
        code = decision.tnved_code if decision is not None else None
        result.append({
            "product_type_id": row["id"],
            "name": row["name"],
            "external_type_id": row["external_type_id"],
            "listings": row["listings"],
            "drafts": row["drafts"],
            "tnved_code": code,
            "tnved_display": (
                decision.tnved_display if decision is not None else None
            ),
            "marking": resolve_marking(code) if code else None,
            "decided_by_user_id": (
                decision.decided_by_user_id if decision is not None else None
            ),
            "decided_at": decision.decided_at if decision is not None else None,
        })
    return result


def activate_registry_version(*, version_id, user_id):
    """Сделать версию перечня активной. Активная всегда ровно одна."""
    from models import db, OzonMarkingRegistryVersion

    candidate = OzonMarkingRegistryVersion.query.get(int(version_id))
    if candidate is None:
        raise OzonComplianceAdminError("Версия перечня не найдена")

    current = OzonMarkingRegistryVersion.query.filter_by(status="active").first()
    if current is not None and current.id != candidate.id:
        current.status = "superseded"
    candidate.status = "active"
    candidate.declared_by_user_id = int(user_id)
    candidate.declared_at = datetime.utcnow()
    db.session.commit()
    return candidate


def preview_registry_switch(version_id) -> dict:
    """Сколько типов поменяют вычисленный флаг при активации версии."""
    from models import OzonComplianceDefault, OzonMarkingRegistryVersion
    from services.ozon_compliance_defaults import normalize_code

    candidate = OzonMarkingRegistryVersion.query.get(int(version_id))
    if candidate is None:
        raise OzonComplianceAdminError("Версия перечня не найдена")

    prefixes = [
        normalize_code(rule.code_prefix)
        for rule in candidate.rules.all()
    ]
    counters = {"to_true": 0, "to_false": 0, "to_unresolved": 0, "unchanged": 0}
    for decision in OzonComplianceDefault.query.filter_by(status="active").all():
        code = normalize_code(decision.tnved_code)
        before = resolve_marking(code)
        matched = any(prefix and code.startswith(prefix) for prefix in prefixes)
        after = True if matched else (
            False if bool(candidate.is_complete) else None
        )
        if before == after:
            counters["unchanged"] += 1
        elif after is True:
            counters["to_true"] += 1
        elif after is False:
            counters["to_false"] += 1
        else:
            counters["to_unresolved"] += 1
    return counters
```

Модуль импортирует `from datetime import datetime` и `from services.ozon_compliance_defaults import resolve_marking` наверху. `TYPE_ROWS_SQL` — константа этого же модуля (проверена на живой БД, возвращает 47 строк):

```python
TYPE_ROWS_SQL = """
SELECT t.id, t.name, t.external_type_id,
  (SELECT COUNT(*) FROM marketplace_listings l
     WHERE l.product_type_id = t.id) AS listings,
  (SELECT COUNT(*) FROM marketplace_product_drafts d
     WHERE d.product_type_id = t.id) AS drafts
FROM marketplace_product_types t
WHERE EXISTS (SELECT 1 FROM marketplace_listings l
                WHERE l.product_type_id = t.id)
   OR EXISTS (SELECT 1 FROM marketplace_product_drafts d
                WHERE d.product_type_id = t.id)
   OR EXISTS (SELECT 1 FROM marketplace_category_mappings m
                WHERE m.product_type_id = t.id)
ORDER BY listings DESC
"""
```

- [ ] **Step 4: Прогнать тесты**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_admin.py`
Expected: PASS (3 теста)

- [ ] **Step 5: Проверки и коммит**

```bash
./venv/bin/python -m py_compile services/ozon_compliance_admin.py
git diff --check
git add services/ozon_compliance_admin.py tests/test_ozon_compliance_admin.py
git commit -m "feat(ozon): админский сервис compliance-решений и реестра маркировки"
```

---

### Task 7: Админский экран

**Files:**
- Create: `routes/admin_ozon_compliance.py`
- Create: `templates/admin_ozon_compliance.html`
- Modify: `seller_platform.py` (регистрация blueprint рядом с `app.register_blueprint(internal_api_bp)`, ~строка 6806)
- Test: `tests/test_ozon_compliance_admin_routes.py`

**Interfaces:**
- Consumes: `OzonComplianceAdminService` (Task 6).
- Produces: маршруты `GET /admin/ozon/compliance`, `POST /admin/ozon/compliance/decision`, `POST /admin/ozon/compliance/registry`, `POST /admin/ozon/compliance/registry/activate`.
- Также добавляет в `services/ozon_compliance_admin.py` функцию `create_registry_version(*, label, is_complete, rules_text, user_id)`.

**Дыра в плане, обнаруженная при исполнении.** Task 6 дал только `activate_registry_version`, переключающую статус УЖЕ существующей строки. Функции, создающей `OzonMarkingRegistryVersion` и `OzonMarkingRule`, во всём плане не было ни в одной задаче. Последствие серьёзнее неработающей формы: без активной версии перечня `resolve_marking` всегда возвращает `None`, атрибут маркировки не заполняется никогда, и вся фича остаётся инертной. Поэтому область Task 7 расширена на сервисный слой.

`rules_text` — textarea, одно правило на строку, поля через `;`, обязательно только первое: `code_prefix;normative_ref;valid_from;note`. Пустые строки и начинающиеся с `#` пропускаются. `code_prefix` нормализуется `normalize_code` и обязан дать от 2 до 10 цифр — пустой или однозначный префикс отклоняется, потому что через `startswith` он совпал бы с любым кодом и сделал бы маркируемым весь каталог. Дубликаты префиксов внутри версии отклоняются внятной ошибкой, а не `IntegrityError`. Лимит 5000 правил. Новая версия создаётся со `status='superseded'`: активация остаётся отдельным явным шагом, которому предшествует превью последствий через `preview_registry_switch`.

SQL задействованных типов уже объявлен константой `TYPE_ROWS_SQL` в Task 6.

- [ ] **Step 1: Написать падающий тест авторизации**

```python
# -*- coding: utf-8 -*-
"""Доступ к админскому экрану compliance-дефолтов Ozon."""
import unittest
from unittest.mock import MagicMock, patch


class ComplianceRouteAuthTestCase(unittest.TestCase):
    def test_non_admin_is_rejected(self):
        from routes.admin_ozon_compliance import _admin_required

        wrapped = _admin_required(lambda: 'ok')
        user = MagicMock()
        user.is_authenticated = True
        user.is_admin = False
        with patch('routes.admin_ozon_compliance.current_user', user):
            with self.assertRaises(Exception):
                wrapped()
```

- [ ] **Step 2: Убедиться, что падает**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_admin_routes.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'routes.admin_ozon_compliance'`

- [ ] **Step 3: Реализовать роуты**

Создать `routes/admin_ozon_compliance.py` по образцу `routes/admin_sales_intelligence.py`:

```python
# -*- coding: utf-8 -*-
"""Админский экран compliance-дефолтов Ozon."""
from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from services.ozon_compliance_admin import (
    OzonComplianceAdminError,
    activate_registry_version,
    list_type_rows,
    preview_registry_switch,
    save_decision,
)

admin_ozon_compliance_bp = Blueprint(
    "admin_ozon_compliance",
    __name__,
    url_prefix="/admin/ozon/compliance",
)


def _admin_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return function(*args, **kwargs)
    return wrapper


@admin_ozon_compliance_bp.get("/")
@login_required
@_admin_required
def index():
    from models import OzonMarkingRegistryVersion
    return render_template(
        "admin_ozon_compliance.html",
        type_rows=list_type_rows(),
        registry_versions=OzonMarkingRegistryVersion.query.order_by(
            OzonMarkingRegistryVersion.id.desc()
        ).limit(20).all(),
    )


@admin_ozon_compliance_bp.post("/decision")
@login_required
@_admin_required
def save():
    try:
        save_decision(
            product_type_id=request.form.get("product_type_id"),
            tnved_code=request.form.get("tnved_code"),
            rationale=request.form.get("rationale"),
            user_id=current_user.id,
        )
        flash("Решение по ТН ВЭД сохранено", "success")
    except OzonComplianceAdminError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("admin_ozon_compliance.index"))


@admin_ozon_compliance_bp.post("/registry/activate")
@login_required
@_admin_required
def activate_registry():
    try:
        activate_registry_version(
            version_id=request.form.get("version_id"),
            user_id=current_user.id,
        )
        flash("Версия перечня активирована", "success")
    except OzonComplianceAdminError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("admin_ozon_compliance.index"))


@admin_ozon_compliance_bp.get("/registry/<int:version_id>/preview")
@login_required
@_admin_required
def registry_preview(version_id):
    try:
        return preview_registry_switch(version_id)
    except OzonComplianceAdminError as exc:
        return {"error": str(exc)}, 400
```

`user_id` берётся только из `current_user.id`, никогда из формы. CSRF обеспечивается общим механизмом приложения — форма обязана передавать токен по существующему в проекте паттерну (`{{ csrf_token() }}` в скрытом поле, как в других admin-шаблонах).

- [ ] **Step 4: Реализовать шаблон**

`templates/admin_ozon_compliance.html`, наследует `base.html`. Две вкладки через `.sh-segmented`. Таблица типов: `.sh-table`, колонки «Тип», «Листингов», «Черновиков», «ТН ВЭД», «Маркировка», «Кто и когда». Строка без решения — `.sh-chip` со статусным токеном `--warn` и подписью «держит N карточек». Форма выбора кода — `datalist` по локальному словарю. Поле обоснования — `required`. Вторая вкладка: активная версия реестра, таблица правил, форма загрузки новой версии, чекбокс «перечень исчерпывающий», кнопка активации с `.sh-confirm`. Все цвета — только через `var(--...)`, инлайновые `#hex` запрещены.

- [ ] **Step 5: Зарегистрировать blueprint**

В `seller_platform.py` рядом со строкой `app.register_blueprint(internal_api_bp)`:

```python
from routes.admin_ozon_compliance import admin_ozon_compliance_bp
app.register_blueprint(admin_ozon_compliance_bp)
```

- [ ] **Step 6: Прогнать тесты**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_admin_routes.py`
Expected: PASS

- [ ] **Step 7: Проверить обе темы вручную**

Открыть `/admin/ozon/compliance`, переключить тему кнопкой в топбаре, проверить: нет горизонтального скролла, состояния empty/loading/disabled читаемы, фокус виден с клавиатуры.

- [ ] **Step 8: Проверки и коммит**

```bash
./venv/bin/python -m py_compile routes/admin_ozon_compliance.py seller_platform.py
git diff --check
git add routes/admin_ozon_compliance.py templates/admin_ozon_compliance.html seller_platform.py tests/test_ozon_compliance_admin_routes.py
git commit -m "feat(ozon): админский экран compliance-дефолтов и реестра маркировки"
```

---

### Task 8: Прогон «Применить к существующим»

**Files:**
- Modify: `services/ozon_compliance_admin.py` (добавить `apply_to_existing_drafts`)
- Modify: `routes/admin_ozon_compliance.py` (маршрут `POST .../apply-existing`)
- Modify: `templates/admin_ozon_compliance.html` (кнопка в строке типа)
- Test: `tests/test_ozon_compliance_admin.py` (дописать)

**Interfaces:**
- Consumes: `apply_to_attributes` (Task 4).
- Produces: `apply_to_existing_drafts(*, product_type_id, limit=200) -> dict` со счётчиками `updated`, `skipped_active_operation`, `skipped_already_filled`, `unresolved`.

**Scope:** неархивные `MarketplaceProductDraft` с этим `product_type_id`, у которых соответствующий атрибут пуст и нет активной `MarketplaceOperation`. Прогон обновляет только `attributes_json` и выставляет `validation_status='stale'`. Provider write не выполняется.

**Поправка к спеке:** в спеке написано «выставляет `ready_to_retry`» — это ошибка. `ck_marketplace_product_draft_status` допускает только `needs_category|draft|blocked|ready|published|archived`; `ready_to_retry` — статус item'а массовой загрузки, не черновика. Присвоение уронило бы commit на CHECK-констрейнте. Спеку поправить в Task 9.

- [ ] **Step 1: Написать падающий тест**

```python
class ApplyToExistingDraftsTestCase(unittest.TestCase):
    def test_draft_with_active_operation_is_skipped(self):
        from services.ozon_compliance_admin import _draft_is_eligible

        draft = MagicMock()
        draft.status = 'blocked'
        self.assertFalse(_draft_is_eligible(draft, has_active_operation=True))

    def test_archived_draft_is_skipped(self):
        from services.ozon_compliance_admin import _draft_is_eligible

        draft = MagicMock()
        draft.status = 'archived'
        self.assertFalse(_draft_is_eligible(draft, has_active_operation=False))

    def test_blocked_draft_without_operation_is_eligible(self):
        from services.ozon_compliance_admin import _draft_is_eligible

        draft = MagicMock()
        draft.status = 'blocked'
        self.assertTrue(_draft_is_eligible(draft, has_active_operation=False))
```

- [ ] **Step 2: Убедиться, что падает**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_admin.py::ApplyToExistingDraftsTestCase`
Expected: FAIL — `cannot import name '_draft_is_eligible'`

- [ ] **Step 3: Реализовать**

```python
def _draft_is_eligible(draft, *, has_active_operation: bool) -> bool:
    if has_active_operation:
        return False
    return getattr(draft, "status", None) != "archived"
```

Затем в том же модуле:

```python
ACTIVE_OPERATION_STATUSES = (
    "queued", "preparing", "submitting", "submitted", "polling", "uncertain",
)


def apply_to_existing_drafts(*, product_type_id, limit: int = 200) -> dict:
    """Разнести админские значения по уже существующим черновикам типа.

    Только локальные изменения: ни одной записи в Ozon и ни одной
    ``MarketplaceOperation`` этот прогон не создаёт.
    """
    import json

    from models import db, MarketplaceOperation, MarketplaceProductDraft
    from services.ozon_compliance_defaults import apply_to_attributes

    type_key = int(product_type_id)
    bounded = max(1, min(int(limit), 200))

    counters = {
        "updated": 0,
        "skipped_active_operation": 0,
        "skipped_already_filled": 0,
        "failed": 0,
        "unresolved": [],
    }

    drafts = MarketplaceProductDraft.query.filter_by(
        product_type_id=type_key,
    ).order_by(MarketplaceProductDraft.id.asc()).limit(bounded).all()

    for draft in drafts:
        has_active_operation = db.session.query(
            MarketplaceOperation.query.filter(
                MarketplaceOperation.draft_id == draft.id,
                MarketplaceOperation.status.in_(ACTIVE_OPERATION_STATUSES),
            ).exists()
        ).scalar()
        if not _draft_is_eligible(
            draft, has_active_operation=bool(has_active_operation)
        ):
            counters["skipped_active_operation"] += 1
            continue

        try:
            with db.session.begin_nested():
                try:
                    current = json.loads(draft.attributes_json or "[]")
                except (TypeError, json.JSONDecodeError):
                    counters["failed"] += 1
                    continue
                if not isinstance(current, list):
                    counters["failed"] += 1
                    continue

                updated, report = apply_to_attributes(current, type_key)
                counters["unresolved"] = report["unresolved"]
                if not report["applied"]:
                    counters["skipped_already_filled"] += 1
                    continue

                draft.attributes_json = json.dumps(
                    updated, ensure_ascii=False, sort_keys=True,
                )
                # ВАЖНО: `status` не трогаем. CHECK-констрейнт допускает только
                # needs_category|draft|blocked|ready|published|archived, а
                # `ready_to_retry` — статус item'а массовой загрузки, не
                # черновика. Помечаем валидацию устаревшей: обычный seller-путь
                # пересчитает её и сам поднимет черновик до `ready`.
                draft.validation_status = "stale"
                draft.updated_at = datetime.utcnow()
                counters["updated"] += 1
        except Exception:
            db.session.rollback()
            counters["failed"] += 1

    db.session.commit()
    return counters
```

- [ ] **Step 4: Прогнать тесты**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_admin.py`
Expected: PASS

- [ ] **Step 5: Добавить маршрут и кнопку**

В `routes/admin_ozon_compliance.py` дополнить импорт из `services.ozon_compliance_admin` именем `apply_to_existing_drafts` (в Task 7 его там намеренно не было — функция ещё не существовала) и добавить маршрут:

```python
@admin_ozon_compliance_bp.post("/<int:product_type_id>/apply-existing")
@login_required
@_admin_required
def apply_existing(product_type_id):
    try:
        counters = apply_to_existing_drafts(product_type_id=product_type_id)
    except OzonComplianceAdminError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("admin_ozon_compliance.index"))

    flash(
        "Обновлено черновиков: {updated}. Пропущено занятых операцией: "
        "{skipped_active_operation}. Уже заполнено: "
        "{skipped_already_filled}. Ошибок: {failed}.".format(**counters),
        "success" if not counters["failed"] else "warning",
    )
    return redirect(url_for("admin_ozon_compliance.index"))
```

Кнопка в строке типа — `.sh-btn` вторичного вида внутри формы с CSRF-токеном, рендерится только когда у типа есть активное решение (`row.tnved_code`). Подпись — «Применить к существующим», рядом мелким текстом пояснение «только локально, без отправки в Ozon».

- [ ] **Step 6: Проверки и коммит**

```bash
./venv/bin/python -m py_compile services/ozon_compliance_admin.py routes/admin_ozon_compliance.py
git diff --check
git add services/ozon_compliance_admin.py routes/admin_ozon_compliance.py templates/admin_ozon_compliance.html tests/test_ozon_compliance_admin.py
git commit -m "feat(ozon): локальный прогон compliance-дефолтов по существующим черновикам"
```

---

### Task 8b: Провенанс compliance-значений и обновление уже заполненных черновиков

**Files:**
- Modify: `services/ozon_compliance_defaults.py`
- Modify: `services/marketplace_drafts.py` (сохранение провенанса в точках создания/привязки типа)
- Modify: `services/ozon_compliance_admin.py` (`apply_to_existing_drafts` — режим обновления)
- Test: `tests/test_ozon_compliance_defaults.py`, `tests/test_ozon_compliance_admin.py`

**Зачем эта задача.** После Task 4 админское исправление не доходит до уже созданных черновиков ни одним путём: `rebase_source_defaults` compliance-атрибуты намеренно не трогает, `apply_reference_defaults` добавляет только отсутствующие идентичности, а `apply_to_existing_drafts` заполняет только пустые поля. Для фичи «задал один раз — дальше под капотом» это означает, что первое же исправление ошибочного кода ТН ВЭД не сработает.

Обновлять вслепую нельзя: значение могло быть вписано продавцом вручную на экране массовой починки, и затирать его запрещено.

**Правило, снимающее неоднозначность.** Обновлять разрешено ТОЛЬКО значение, побайтово равное тому, которое слой сам записал в прошлый раз. Поэтому провенанс хранит не только источник, но и точное записанное значение. Продавец, изменивший значение, автоматически выпадает из-под обновления — и это не требует, чтобы `update_draft` умел поддерживать провенанс.

**Неймспейс провенанса.** `provenance_json` уже содержит факты источника с ключами вида `attributes.colors`, `commercial.price`. Compliance-записи кладутся под отдельный префикс `compliance.` и с фактами не смешиваются:

```python
{
  "compliance.22232": {
    "source": "admin_compliance_default",
    "default_id": 7,
    "code": "3307900008",
    "external_value_id": "971397758",
    "dictionary_version": 3
  },
  "compliance.23536": {
    "source": "admin_marking_registry",
    "registry_version_id": 1,
    "value": "false"
  }
}
```

- [ ] **Step 1: Написать падающий тест на построение провенанса**

```python
class ComplianceProvenanceTestCase(unittest.TestCase):
    def test_provenance_records_written_values(self):
        from services.ozon_compliance_defaults import build_provenance_entries

        report = {
            'applied': ['22232', '23536'],
            'unresolved': [],
            'evidence': {'tnved_default_id': 7, 'dictionary_version': 3,
                         'registry_version_id': 1},
        }
        defaults = {
            'tnved': {'code': '3307900008', 'value': '3307900008 - X',
                      'external_value_id': '971397758', 'default_id': 7,
                      'dictionary_version': 3},
            'marking': False,
        }
        entries = build_provenance_entries(report, defaults)
        self.assertEqual(entries['compliance.22232']['source'],
                         'admin_compliance_default')
        self.assertEqual(entries['compliance.22232']['external_value_id'],
                         '971397758')
        self.assertEqual(entries['compliance.23536']['value'], 'false')

    def test_nothing_recorded_for_unapplied_attributes(self):
        from services.ozon_compliance_defaults import build_provenance_entries

        report = {'applied': [], 'unresolved': ['22232', '23536'],
                  'evidence': {}}
        entries = build_provenance_entries(report, {'tnved': None,
                                                    'marking': None})
        self.assertEqual(entries, {})
```

- [ ] **Step 2: Убедиться, что падает**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_defaults.py::ComplianceProvenanceTestCase`
Expected: FAIL — `cannot import name 'build_provenance_entries'`

- [ ] **Step 3: Реализовать построение провенанса**

`apply_to_attributes` дополнительно кладёт в `report` ключ `defaults` с разрешёнными значениями, чтобы вызывающий не резолвил повторно. Затем:

```python
def build_provenance_entries(report, defaults) -> dict:
    """Провенанс заполненных compliance-атрибутов.

    Записывается точное значение, которое слой поставил: обновлять его позже
    разрешено только пока оно побайтово равно записанному. Любая правка
    продавца автоматически выводит атрибут из-под автоматического обновления.
    """
    applied = set((report or {}).get("applied") or [])
    evidence = (report or {}).get("evidence") or {}
    entries: dict = {}

    tnved = (defaults or {}).get("tnved")
    if TNVED_ATTRIBUTE_ID in applied and tnved:
        entries[f"compliance.{TNVED_ATTRIBUTE_ID}"] = {
            "source": "admin_compliance_default",
            "default_id": tnved.get("default_id"),
            "code": tnved.get("code"),
            "external_value_id": tnved.get("external_value_id"),
            "dictionary_version": tnved.get("dictionary_version"),
        }

    marking = (defaults or {}).get("marking")
    if MARKING_ATTRIBUTE_ID in applied and marking is not None:
        entries[f"compliance.{MARKING_ATTRIBUTE_ID}"] = {
            "source": "admin_marking_registry",
            "registry_version_id": evidence.get("registry_version_id"),
            "value": "true" if marking else "false",
        }

    return entries
```

- [ ] **Step 4: Прогнать тесты провенанса**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_defaults.py::ComplianceProvenanceTestCase`
Expected: PASS

- [ ] **Step 5: Сохранять провенанс в точках создания и привязки типа**

В `services/marketplace_drafts.py` в местах, где `attributes_json` перезаписывается результатом `_auto_map_attributes` (строки около 3541, 4112, 4337, 4709), после присвоения атрибутов слить `build_provenance_entries(...)` в существующий `provenance_json` черновика, не затирая ключи фактов. `rebase_source_defaults` провенанс compliance-ключей не трогает — он их и не пересчитывает.

- [ ] **Step 6: Написать падающий тест на режим обновления**

```python
class RefreshExistingDraftsTestCase(unittest.TestCase):
    def test_only_value_we_wrote_is_refreshed(self):
        from services.ozon_compliance_admin import compliance_value_is_ours

        provenance = {'compliance.22232': {'external_value_id': '971397758'}}
        stored = {'attribute_id': '22232',
                  'values': [{'dictionary_value_id': '971397758',
                              'value': '3307900008 - X'}]}
        self.assertTrue(compliance_value_is_ours('22232', stored, provenance))

    def test_seller_edited_value_is_not_ours(self):
        from services.ozon_compliance_admin import compliance_value_is_ours

        provenance = {'compliance.22232': {'external_value_id': '971397758'}}
        stored = {'attribute_id': '22232',
                  'values': [{'dictionary_value_id': '999999999',
                              'value': 'Другой код'}]}
        self.assertFalse(compliance_value_is_ours('22232', stored, provenance))

    def test_value_without_provenance_is_not_ours(self):
        from services.ozon_compliance_admin import compliance_value_is_ours

        stored = {'attribute_id': '22232',
                  'values': [{'dictionary_value_id': '971397758'}]}
        self.assertFalse(compliance_value_is_ours('22232', stored, {}))
```

- [ ] **Step 7: Убедиться, что падает**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_admin.py::RefreshExistingDraftsTestCase`
Expected: FAIL — `cannot import name 'compliance_value_is_ours'`

- [ ] **Step 8: Реализовать проверку принадлежности и режим обновления**

```python
def compliance_value_is_ours(attribute_id, stored_item, provenance) -> bool:
    """Ставил ли это значение сам слой, и не менял ли его с тех пор продавец."""
    entry = (provenance or {}).get(f"compliance.{attribute_id}")
    if not isinstance(entry, dict):
        return False
    values = (stored_item or {}).get("values")
    if not isinstance(values, list) or len(values) != 1:
        return False
    value = values[0]
    if not isinstance(value, dict):
        return False
    if attribute_id == "23536":
        return value.get("value") == entry.get("value")
    return (
        value.get("dictionary_value_id") == entry.get("external_value_id")
    )
```

`apply_to_existing_drafts` получает параметр `refresh=False`. При `refresh=True` для заполненного атрибута проверяется `compliance_value_is_ours`; если да и текущее разрешённое значение отличается — значение заменяется и провенанс обновляется, счётчик `refreshed`. Если нет — счётчик `skipped_seller_owned`, значение не трогается. Если совпадает — `already_current`.

- [ ] **Step 9: Прогнать тесты и полный набор**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q tests/test_ozon_compliance_defaults.py tests/test_ozon_compliance_admin.py`
Expected: PASS

Затем полный набор синхронно: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q`

- [ ] **Step 10: Проверки и коммит**

```bash
./venv/bin/python -m py_compile services/ozon_compliance_defaults.py services/ozon_compliance_admin.py services/marketplace_drafts.py
git diff --check
git add services/ozon_compliance_defaults.py services/ozon_compliance_admin.py services/marketplace_drafts.py tests/test_ozon_compliance_defaults.py tests/test_ozon_compliance_admin.py
git commit -m "feat(ozon): провенанс compliance-значений и обновление существующих черновиков"
```

---

### Task 9: Подключение миграции и обновление AGENTS.md

**Files:**
- Modify: `docker-entrypoint.sh`
- Modify: `migrations/run_all_migrations.py`
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: миграция Task 1.
- Produces: ничего для кода.

- [ ] **Step 1: Подключить миграцию fail-fast в entrypoint**

В `docker-entrypoint.sh` найти вызов `migrate_add_ozon_product_type_visibility.py` и добавить сразу после него, тем же fail-fast стилем (без `|| echo`):

```sh
python migrations/migrate_add_ozon_compliance_defaults.py "$DB_PATH"
```

- [ ] **Step 2: Подключить в comprehensive runner**

В `migrations/run_all_migrations.py` добавить `migrate_add_ozon_compliance_defaults` в список после `migrate_add_ozon_product_type_visibility`, следуя существующему формату записи.

- [ ] **Step 3: Обновить AGENTS.md**

В разделе про `services/marketplace_drafts.py` найти предложение «Маркировка и ТН ВЭД никогда не выводятся из этих рецептов.» и добавить сразу после него:

```
Этот запрет остаётся в силе дословно и относится к выводу из фактов товара и к
consensus по опубликованным карточкам. Единственный допустимый источник ТН ВЭД —
активное подписанное решение `OzonComplianceDefault`, привязанное к Ozon product
type; единственный допустимый источник признака маркировки — активная версия
`OzonMarkingRegistryVersion`, применённая к этому коду longest-prefix матчем, и
только когда версия объявлена исчерпывающей (`is_complete`). Резолвер
`services/ozon_compliance_defaults.py` хранит строку кода, заново разрешает
`dictionary_value_id` в свежем type-scoped словаре на каждом применении,
заполняет только пустые атрибуты, никогда не перезаписывает seller value и при
любом нерешённом источнике не записывает ничего. Админский экран
`/admin/ozon/compliance` требует обоснование, хранит автора и дату, а смена кода
не инициирует provider write: существующие черновики обновляются только явным
локальным прогоном.
```

Также добавить `migrate_add_ozon_compliance_defaults.py` в список миграций раздела «База данных и миграции».

- [ ] **Step 2b: Закрыть отложенный минор Task 1**

В `models.py` у класса `OzonMarkingRule` в существующий `__table_args__` добавить недостающий перф-индекс, объявленный в миграции как `ix_ozon_marking_rules_prefix`, чтобы `db.create_all()` и миграция давали одинаковый набор индексов:

```python
        db.Index('ix_ozon_marking_rules_prefix', 'code_prefix'),
```

Существующий `db.UniqueConstraint('registry_version_id', 'code_prefix', name='uq_ozon_marking_rule_scope')` не трогать.

- [ ] **Step 2c: Убрать недостижимый обработчик**

В `services/ozon_compliance_admin.py` в `activate_registry_version` убрать `StaleDataError` из кортежа `except`, оставив только `IntegrityError`. У модели `OzonMarkingRegistryVersion` нет `version_id_col`, поэтому `StaleDataError` там недостижим; непроверяемая ветка создаёт ложное впечатление, что конкурентная активация защищена оптимистической блокировкой, тогда как её сдерживает только partial-unique индекс. В `save_decision` кортеж НЕ трогать — там `version_id_col` есть и обработчик реально срабатывает.

- [ ] **Step 3b: Поправить ошибку в спеке**

В `docs/superpowers/specs/2026-07-25-ozon-compliance-defaults-design.md` в разделе «Охват существующих карточек» заменить «выставляет `ready_to_retry`» на «выставляет `validation_status='stale'`, не трогая `status`», и добавить пояснение: `ck_marketplace_product_draft_status` допускает только `needs_category|draft|blocked|ready|published|archived`, а `ready_to_retry` — статус item'а массовой загрузки.

- [ ] **Step 4: Прогнать полный набор тестов**

Run: `SKIP_SCHEDULER=1 ./venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Проверки и коммит**

```bash
git diff --check
git add docker-entrypoint.sh migrations/run_all_migrations.py AGENTS.md
git commit -m "chore(ozon): вайринг миграции compliance-дефолтов и обновление AGENTS.md"
```

---

## Проверка результата

После Task 9 повторить сценарий, на котором задача была обнаружена:

1. Через `/admin/ozon/compliance` задать код ТН ВЭД для типа 1609 «Мастурбатор» с обоснованием.
2. Загрузить версию реестра маркировки и объявить её исчерпывающей.
3. Создать черновик для `ImportedProduct` id=57 (или пересобрать существующий №9).
4. Убедиться: `required_supplied = 5/5`, `publishable = true`, в `attributes_json` присутствуют `22232` и `23536`.

Ожидаемый результат — черновик, который раньше блокировался, становится готов к отправке без единого ручного ввода на карточку.
