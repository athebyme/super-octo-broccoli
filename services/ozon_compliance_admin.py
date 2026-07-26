# -*- coding: utf-8 -*-
"""Админский сервис compliance-дефолтов Ozon.

Здесь живёт запись подписанных админом решений по ТН ВЭД и управление
версиями нормативного перечня маркировки. Модуль не вызывает Ozon API и не
создаёт ``MarketplaceOperation`` — это только локальное хранилище решения,
которое читает ``services/ozon_compliance_defaults.py`` при сборке черновика.

ORM-импорты сделаны лениво внутри тел функций, как в соседнем
``services/ozon_compliance_defaults.py``, чтобы не тянуть модели при простом
импорте модуля (например, для валидации ввода без контекста приложения).

Обе write-функции (``save_decision``, ``activate_registry_version``) заменяют
единственную активную строку новой внутри одной транзакции с ОДНИМ commit.
Порядок UPDATE/INSERT/UPDATE внутри flush не гарантирован публичным API
SQLAlchemy (unit of work сортирует "грязные" объекты по возрастанию первичного
ключа), поэтому снятие старого активного флага фиксируется явным
``db.session.flush()`` до установки нового — без этого активация версии/типа с
МЕНЬШИМ id, чем у текущей активной строки, детерминированно ловит
partial-unique constraint по обеим строкам сразу.
"""
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from services.ozon_compliance_defaults import resolve_marking_batch


class OzonComplianceAdminError(Exception):
    """Ошибка валидации или бизнес-правила админского compliance-сервиса."""


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


def validate_decision_input(*, product_type_id, tnved_code, rationale) -> dict:
    """Проверить и нормализовать вход для решения по ТН ВЭД.

    Не обращается к БД — чистая валидация, пригодная для юнит-тестов без
    контекста приложения.
    """
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


def save_decision(*, product_type_id, tnved_code, rationale, user_id):
    """Заменить активное решение по типу новым, подписанным админом.

    Старое активное решение переводится в ``retired``, новое создаётся со
    ``status='active'`` в той же транзакции: partial-unique индекс допускает
    только одну активную строку на ``(marketplace_id, product_type_id)``.
    Между retire и insert стоит явный ``flush()``: новая строка ещё не имеет
    PK и в сортировку "грязных" объектов unit of work не попадает, поэтому
    здесь UPDATE и так уходит раньше INSERT, но это недокументированная
    деталь реализации ORM — код не должен на неё полагаться.
    """
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

    try:
        if current is not None:
            current.status = "retired"
        db.session.flush()

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
    except (StaleDataError, IntegrityError):
        db.session.rollback()
        raise OzonComplianceAdminError(
            "Решение изменено другим администратором, обновите страницу и "
            "повторите"
        ) from None
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
    """Задействованные Ozon-типы с текущим решением и выведенной маркировкой.

    Маркировка резолвится ОДНИМ batch-вызовом для всех кодов сразу
    (``resolve_marking_batch``), а не в цикле per-row: активная версия
    перечня и её правила не должны читаться заново на каждый из
    задействованных типов (N+1).
    """
    from models import db, OzonComplianceDefault
    from services.ozon_compliance_defaults import normalize_code

    rows = db.session.execute(db.text(TYPE_ROWS_SQL)).mappings().all()
    decisions = {
        item.product_type_id: item
        for item in OzonComplianceDefault.query.filter_by(status="active").all()
    }
    codes = [
        decision.tnved_code
        for decision in decisions.values()
        if decision.tnved_code
    ]
    marking_by_code = resolve_marking_batch(codes)

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
            "marking": (
                marking_by_code.get(normalize_code(code)) if code else None
            ),
            "decided_by_user_id": (
                decision.decided_by_user_id if decision is not None else None
            ),
            "decided_at": decision.decided_at if decision is not None else None,
        })
    return result


def activate_registry_version(*, version_id, user_id):
    """Сделать версию перечня активной. Активная всегда ровно одна.

    Текущая активная версия переводится в ``superseded`` и новая становится
    ``active`` в одной транзакции с одним commit.

    Между двумя UPDATE стоит явный ``flush()``. Без него, если у
    кандидата PK МЕНЬШЕ, чем у текущей активной строки (типичный случай —
    откат к более старой версии перечня), unit of work SQLAlchemy сортирует
    "грязные" объекты одной таблицы по возрастанию первичного ключа и может
    отправить в БД сначала UPDATE кандидата (``status='active'``), пока
    старая строка ещё активна — в таблице на мгновение две активные строки, и
    partial-unique индекс отклоняет всю транзакцию. Явный flush после снятия
    старого флага убирает зависимость от этого порядка.
    """
    from models import db, OzonMarkingRegistryVersion

    candidate = OzonMarkingRegistryVersion.query.get(int(version_id))
    if candidate is None:
        raise OzonComplianceAdminError("Версия перечня не найдена")

    current = OzonMarkingRegistryVersion.query.filter_by(status="active").first()

    try:
        if current is not None and current.id != candidate.id:
            current.status = "superseded"
        db.session.flush()

        candidate.status = "active"
        candidate.declared_by_user_id = int(user_id)
        candidate.declared_at = datetime.utcnow()
        db.session.commit()
    except (StaleDataError, IntegrityError):
        db.session.rollback()
        raise OzonComplianceAdminError(
            "Версия перечня уже активирована другим администратором"
        ) from None
    return candidate


def preview_registry_switch(version_id) -> dict:
    """Сколько типов поменяют вычисленный флаг при активации версии.

    Только считает — ничего не сохраняет. Текущая ("before") маркировка всех
    активных решений резолвится ОДНИМ batch-вызовом до цикла, а не per-item
    вызовом ``resolve_marking`` — иначе активная версия перечня и её правила
    читались бы заново на каждое решение (N+1).
    """
    from models import OzonComplianceDefault, OzonMarkingRegistryVersion
    from services.ozon_compliance_defaults import normalize_code

    candidate = OzonMarkingRegistryVersion.query.get(int(version_id))
    if candidate is None:
        raise OzonComplianceAdminError("Версия перечня не найдена")

    prefixes = [
        normalize_code(rule.code_prefix)
        for rule in candidate.rules.all()
    ]
    decisions = OzonComplianceDefault.query.filter_by(status="active").all()
    before_by_code = resolve_marking_batch(
        [decision.tnved_code for decision in decisions]
    )

    counters = {"to_true": 0, "to_false": 0, "to_unresolved": 0, "unchanged": 0}
    for decision in decisions:
        code = normalize_code(decision.tnved_code)
        before = before_by_code.get(code)
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
