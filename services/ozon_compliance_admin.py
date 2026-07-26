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
import hashlib
import json
import logging
from datetime import date, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from services.ozon_compliance_defaults import (
    MARKING_ATTRIBUTE_ID,
    TNVED_ATTRIBUTE_ID,
    build_provenance_entries,
    normalize_code,
    resolve_marking_batch,
)

logger = logging.getLogger(__name__)


class OzonComplianceAdminError(Exception):
    """Ошибка валидации или бизнес-правила админского compliance-сервиса."""


def _parse_positive_int(value, field_name: str) -> int:
    """Общая граница валидации числового ID из формы/аргумента.

    Используется везде, где raw-значение может прийти прямо из тела POST
    (``version_id`` формы, а не URL ``<int:...>`` converter): без этой
    проверки ``int(None)``/``int("abc")`` роняет запрос необработанным
    ``TypeError``/``ValueError`` в голый Flask 500 вместо понятного flash.
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise OzonComplianceAdminError(f"{field_name} должен быть числом")
    if parsed <= 0:
        raise OzonComplianceAdminError(f"{field_name} должен быть положительным")
    return parsed


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

    type_key = _parse_positive_int(product_type_id, "product_type_id")

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

    ДО любой записи код обязан пройти проверку по свежему официальному
    словарю ТН ВЭД этого типа (``is_type_tnved_code_available``). Без этой
    проверки опечатка в поле ввода тихо сохранялась бы как «успех»:
    ``resolve_tnved`` позже молча вернул бы ``None`` для несуществующего в
    словаре кода, и карточки типа остались бы заблокированы без единой
    видимой причины — та же инертность, что уже чинили в Task 6, только
    теперь она приходит от простой опечатки, а не от отсутствия версии
    реестра.

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
    from services.ozon_compliance_defaults import (
        TNVED_ATTRIBUTE_ID, is_type_tnved_code_available,
    )

    cleaned = validate_decision_input(
        product_type_id=product_type_id,
        tnved_code=tnved_code,
        rationale=rationale,
    )
    product_type = MarketplaceProductType.query.get(cleaned["product_type_id"])
    if product_type is None:
        raise OzonComplianceAdminError("Ozon product type не найден")

    available = is_type_tnved_code_available(
        product_type.id, cleaned["tnved_code"],
    )
    if available is None:
        raise OzonComplianceAdminError(
            "Официальный словарь ТН ВЭД этого типа недоступен или устарел — "
            "решение нельзя сохранить до синхронизации справочника"
        )
    if not available:
        raise OzonComplianceAdminError(
            f"Код {cleaned['tnved_code']} не найден среди официальных "
            "значений ТН ВЭД этого типа — выберите код из списка"
        )

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
    задействованных типов (N+1). Готовность и размер официального словаря
    ТН ВЭД считаются тем же способом — ``type_tnved_dictionary_summary_batch``
    на ВСЕ задействованные типы разом, а не отдельным запросом на тип.
    """
    from models import db, OzonComplianceDefault
    from services.ozon_compliance_defaults import (
        normalize_code, type_tnved_dictionary_summary_batch,
    )

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
    dictionary_by_type = type_tnved_dictionary_summary_batch(
        [row["id"] for row in rows]
    )

    result = []
    for row in rows:
        decision = decisions.get(row["id"])
        code = decision.tnved_code if decision is not None else None
        dictionary_summary = dictionary_by_type.get(
            row["id"], {"ready": False, "count": 0},
        )
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
            "tnved_dictionary_ready": dictionary_summary["ready"],
            "tnved_dictionary_count": dictionary_summary["count"],
        })
    return result


def type_tnved_dictionary(product_type_id) -> dict:
    """Официальный словарь ТН ВЭД одного типа — proxy для admin-экрана.

    Тонкая обёртка над
    ``services.ozon_compliance_defaults.get_type_tnved_dictionary``: экран
    (роуты/шаблон) импортирует reference-функции только из этого модуля, как
    и остальные (``save_decision``, ``list_type_rows`` и т.д.), а не тянет
    ``ozon_compliance_defaults`` напрямую.
    """
    from services.ozon_compliance_defaults import get_type_tnved_dictionary
    return get_type_tnved_dictionary(product_type_id)


_MIN_REGISTRY_PREFIX_DIGITS = 2
_MAX_REGISTRY_PREFIX_DIGITS = 10
_MAX_REGISTRY_RULES = 5000
_MAX_REGISTRY_LABEL_LEN = 200
_MAX_REGISTRY_NORMATIVE_REF_LEN = 300
_MAX_REGISTRY_NOTE_LEN = 2000


def _parse_registry_rule_date(raw, line_no):
    """Строго распарсить необязательную дату действия правила (ISO)."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise OzonComplianceAdminError(
            f"Строка {line_no}: дата действия должна быть в формате "
            "ГГГГ-ММ-ДД"
        )


def _parse_registry_rules(rules_text) -> list:
    """Разобрать textarea новой версии перечня в проверенный список правил.

    Формат строки — ``код;ссылка на акт;дата ГГГГ-ММ-ДД;примечание``, только
    код обязателен, остальные поля можно опустить (``maxsplit=3`` — точка с
    запятой внутри примечания не ломает разбор). Пустые строки и строки,
    начинающиеся с ``#``, — комментарии и пропускаются, в результат и в
    итоговый ``rule_count`` не попадают.

    Любая проблема прерывает разбор целиком с номером строки: версия либо
    сохраняется полностью валидной, либо не сохраняется вовсе — частичный
    перечень маркировки опаснее явного отказа.
    """
    lines = (rules_text or "").splitlines()
    parsed = []
    seen = {}
    for line_no, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split(";", 3)
        code_raw = parts[0].strip()
        normative_ref = parts[1].strip() if len(parts) > 1 else ""
        valid_from_raw = parts[2].strip() if len(parts) > 2 else ""
        note = parts[3].strip() if len(parts) > 3 else ""

        code = normalize_code(code_raw)
        if not (
            _MIN_REGISTRY_PREFIX_DIGITS
            <= len(code)
            <= _MAX_REGISTRY_PREFIX_DIGITS
        ):
            # Пустой префикс совпал бы с ЛЮБЫМ кодом через ``startswith`` и
            # сделал бы маркируемым весь каталог — это не мелкая опечатка, а
            # отказ, который обязан остановить сохранение версии целиком.
            raise OzonComplianceAdminError(
                f"Строка {line_no}: код должен содержать от "
                f"{_MIN_REGISTRY_PREFIX_DIGITS} до "
                f"{_MAX_REGISTRY_PREFIX_DIGITS} цифр"
            )
        if code in seen:
            raise OzonComplianceAdminError(
                f"Строка {line_no}: код {code} уже встречался на строке "
                f"{seen[code]}"
            )
        seen[code] = line_no

        if len(normative_ref) > _MAX_REGISTRY_NORMATIVE_REF_LEN:
            raise OzonComplianceAdminError(
                f"Строка {line_no}: ссылка на нормативный акт слишком длинная"
            )
        if len(note) > _MAX_REGISTRY_NOTE_LEN:
            raise OzonComplianceAdminError(
                f"Строка {line_no}: примечание слишком длинное"
            )

        parsed.append({
            "code_prefix": code,
            "normative_ref": normative_ref or None,
            "valid_from": _parse_registry_rule_date(valid_from_raw, line_no),
            "note": note or None,
        })

    if not parsed:
        raise OzonComplianceAdminError(
            "Нужно хотя бы одно правило — построчно, "
            "код;ссылка на акт;дата ГГГГ-ММ-ДД;примечание"
        )
    if len(parsed) > _MAX_REGISTRY_RULES:
        raise OzonComplianceAdminError(
            f"Правил больше {_MAX_REGISTRY_RULES} — похоже на вставку "
            "целого файла, разбейте на части"
        )
    return parsed


def _compute_registry_checksum(prefixes, is_complete) -> str:
    """SHA-256 канонического содержимого версии перечня.

    Порядок строк в исходном тексте не должен влиять на контрольную сумму:
    список нормализованных префиксов сортируется перед хэшированием, поэтому
    две версии с одинаковым набором правил и одинаковым ``is_complete`` дают
    одинаковый checksum независимо от порядка ввода.
    """
    canonical = "|".join(sorted(prefixes)) + f"::{bool(is_complete)}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_registry_version(*, label, is_complete, rules_text, user_id):
    """Создать новую версию нормативного перечня маркировки.

    Версия сохраняется со ``status='superseded'`` — активация всегда
    отдельный явный шаг (``activate_registry_version``), который админ
    выполняет после того, как посмотрел ``preview_registry_switch`` на уже
    сохранённой версии. Совмещать создание с активацией нельзя: тогда теряется
    смысл предпросмотра последствий переключения — сколько типов поменяют
    вычисленный флаг маркировки ДО того, как это реально произойдёт.
    """
    from models import db, OzonMarkingRegistryVersion, OzonMarkingRule

    clean_label = (label or "").strip()
    if not clean_label:
        raise OzonComplianceAdminError("Название версии обязательно")
    if len(clean_label) > _MAX_REGISTRY_LABEL_LEN:
        raise OzonComplianceAdminError("Название версии слишком длинное")

    complete_flag = bool(is_complete)
    parsed_rules = _parse_registry_rules(rules_text)
    checksum = _compute_registry_checksum(
        [rule["code_prefix"] for rule in parsed_rules], complete_flag,
    )

    try:
        version = OzonMarkingRegistryVersion(
            label=clean_label,
            is_complete=complete_flag,
            declared_by_user_id=int(user_id),
            declared_at=datetime.utcnow(),
            rule_count=len(parsed_rules),
            checksum=checksum,
            status="superseded",
        )
        db.session.add(version)
        db.session.flush()

        for rule in parsed_rules:
            db.session.add(OzonMarkingRule(
                registry_version_id=version.id,
                code_prefix=rule["code_prefix"],
                normative_ref=rule["normative_ref"],
                valid_from=rule["valid_from"],
                note=rule["note"],
            ))
        db.session.commit()
    except (StaleDataError, IntegrityError):
        db.session.rollback()
        raise OzonComplianceAdminError(
            "Не удалось сохранить версию перечня — конкурентное изменение, "
            "повторите"
        ) from None
    return version


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

    ``version_id`` приходит сюда прямо из тела POST-формы (не через URL
    ``<int:...>`` converter), поэтому обязан пройти ``_parse_positive_int``
    ДО первого запроса: без этого отсутствующее или нечисловое значение
    роняет запрос необработанным ``TypeError``/``ValueError`` в голый Flask
    500 вместо единого контракта «ошибка -> flash».
    """
    from models import db, OzonMarkingRegistryVersion

    version_key = _parse_positive_int(version_id, "version_id")

    candidate = OzonMarkingRegistryVersion.query.get(version_key)
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
    except IntegrityError:
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

    Роут даёт сюда ``version_id`` уже через URL ``<int:...>`` converter
    (Flask сам вернёт 404 на нечисловой путь до входа в функцию), но
    ``_parse_positive_int`` всё равно применяется для единообразия с
    ``activate_registry_version`` и на случай прямого вызова из другого места.
    """
    from models import OzonComplianceDefault, OzonMarkingRegistryVersion
    from services.ozon_compliance_defaults import normalize_code

    version_key = _parse_positive_int(version_id, "version_id")

    candidate = OzonMarkingRegistryVersion.query.get(version_key)
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


# Единственные НЕтерминальные значения ``MarketplaceOperation.status`` по
# ``ck_marketplace_operation_status`` в ``models.py``
# (``queued|submitting|submitted|polling|succeeded|partial|failed|uncertain|
# cancelled``): пока строка находится в одном из этих статусов, черновик,
# на который она ссылается, уже отправляется/сверяется провайдером и не
# должен получить локальную правку атрибутов параллельно с этим.
ACTIVE_OPERATION_STATUSES = (
    "queued", "submitting", "submitted", "polling", "uncertain",
)


def _draft_is_eligible(draft, *, has_active_operation: bool) -> bool:
    """Чистый предикат приемлемости черновика для локального compliance-прогона.

    ``has_active_operation`` вычисляется вызывающим кодом одним batched
    запросом на весь прогон (см. ``apply_to_existing_drafts``) — здесь
    только сама логика, чтобы её можно было проверить юнит-тестом без БД.
    """
    if has_active_operation:
        return False
    return getattr(draft, "status", None) != "archived"


def _bounded_limit(limit) -> int:
    """Зажать произвольный ``limit`` в допустимый диапазон ``[1, 200]``.

    Чистая функция без БД — вынесена отдельно, чтобы верхнюю границу можно
    было проверить дешёвым юнит-тестом, не создавая 200+ строк в тестовой БД.
    """
    return max(1, min(int(limit), 200))


def compliance_value_is_ours(attribute_id, stored_item, provenance) -> bool:
    """Ставил ли это значение сам слой, и не менял ли его с тех пор продавец.

    Единственное допустимое доказательство — побайтовое совпадение текущего
    сохранённого значения с тем, что зафиксировано в провенансе в момент
    записи (``build_provenance_entries``). Отсутствие записи в провенансе
    (черновик создан до Task 8b, либо значение вообще не из этого слоя)
    трактуется fail-closed как «не наше» — обновлять его нельзя.
    """
    entry = (provenance or {}).get(f"compliance.{attribute_id}")
    if not isinstance(entry, dict):
        return False
    values = (stored_item or {}).get("values")
    if not isinstance(values, list) or len(values) != 1:
        return False
    value = values[0]
    if not isinstance(value, dict):
        return False
    if attribute_id == MARKING_ATTRIBUTE_ID:
        return value.get("value") == entry.get("value")
    return (
        value.get("dictionary_value_id") == entry.get("external_value_id")
    )


def _stored_provenance(raw_value) -> dict:
    """Разобрать ``provenance_json`` черновика в безопасный dict.

    Возвращает свежий словарь на каждый вызов (как
    ``MarketplaceDraftService._stored_json`` в соседнем модуле): вызывающий
    код мутирует и пересохраняет его, не трогая переданную ORM-строку.
    """
    try:
        value = json.loads(raw_value or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _stored_attribute_item(attributes, attribute_id):
    """Найти существующую запись атрибута по ``attribute_id`` в списке."""
    for item in attributes:
        if isinstance(item, dict) and str(item.get("attribute_id")) == attribute_id:
            return item
    return None


def _compliance_desired_values(attribute_id, resolved) -> list:
    """Каноническое представление разрешённого значения в формате ``values``.

    Обязано побайтово совпадать с тем, что кладёт ``_set_value`` внутри
    ``ozon_compliance_defaults.apply_to_attributes`` — иначе refresh считал бы
    «уже актуально» при фактически другой сериализации значения.
    """
    if attribute_id == MARKING_ATTRIBUTE_ID:
        return [{"value": "true" if resolved else "false"}]
    return [{
        "dictionary_value_id": resolved.get("external_value_id"),
        "value": resolved.get("value"),
    }]


def _replace_attribute_values(attributes, attribute_id, values) -> None:
    """Заменить ``values`` существующей записи атрибута на месте."""
    for item in attributes:
        if isinstance(item, dict) and str(item.get("attribute_id")) == attribute_id:
            item["values"] = values
            return


def apply_to_existing_drafts(
    *, product_type_id, limit: int = 200, refresh: bool = False,
) -> dict:
    """Разнести подписанное админом решение по уже существующим черновикам типа.

    Прогон только локальный: ни одной ``MarketplaceOperation`` он не создаёт и
    ни одного вызова Ozon не делает. Он лишь дозаполняет ПУСТОЙ compliance-
    атрибут (``apply_to_attributes`` из Task 4 никогда не трогает уже
    заполненное значение) и выставляет ``validation_status='stale'`` —
    обычный seller-путь сам пересчитает валидацию и поднимет черновик до
    ``ready``, если он теперь полон.

    ``draft.status`` этот прогон НЕ трогает: ``ck_marketplace_product_draft_status``
    допускает только ``needs_category|draft|blocked|ready|published|archived``,
    а ``ready_to_retry`` — статус item'а массовой Ozon-загрузки, не черновика;
    присваивание его сюда уронило бы commit на CHECK-констрейнте.

    Черновик пропускается, если он архивный либо на него уже ссылается
    активная (ещё не терминальная) ``MarketplaceOperation`` — черновик,
    который прямо сейчас отправляется/сверяется с Ozon, не должен получить
    параллельную локальную правку. По умолчанию (``refresh=False``) уже
    заполненный атрибут не трогается — это ДОзаполнение пустого.

    ``refresh=True`` дополнительно включает обновление уже заполненного
    значения, но ТОЛЬКО когда оно побайтово равно тому, что этот же слой
    записал в прошлый раз (``compliance_value_is_ours`` по провенансу
    ``MarketplaceProductDraft.provenance_json``, ключи ``compliance.<id>``
    из ``build_provenance_entries``). Значение, вписанное продавцом вручную
    (или вообще без сохранённого провенанса — черновики до Task 8b),
    считается fail-closed «не нашим» и не трогается ни при каких условиях.
    Совпавшее с текущим резолвом значение не переписывается повторно.

    Запросы к БД bounded и НЕ растут с числом черновиков: черновики типа,
    множество ``draft_id`` с активной операцией среди них и резолв
    compliance-дефолтов типа считаются каждый ОДИН раз до цикла (не на
    каждый черновик отдельно) — иначе прогон на 200 черновиках делал бы
    200 лишних round-trip'ов только на проверку активной операции.
    ``apply_to_attributes`` получает уже посчитанный резолв через
    ``resolved_defaults=defaults``, поэтому и она не резолвит заново на
    каждой карточке (см. её docstring в ``ozon_compliance_defaults.py``).
    Проверка ``refresh`` — чистое сравнение уже загруженного JSON, без
    дополнительных SELECT: query count не растёт при включении флага.

    Работа по одной строке (проверка архивности/активной операции, парсинг
    ``attributes_json``/``provenance_json``, вызов ``apply_to_attributes``,
    запись) целиком находится под ОДНИМ per-draft ``try/except`` —
    требование «ошибка одной строки не откатывает уже обработанные» не
    ограничивается только фазой записи, поэтому ни одна часть построчной
    работы не должна оставаться снаружи try. Внутри самой записи
    используется ``db.session.begin_nested()``; ошибка там учитывается в
    ``failed``, но явный ``db.session.rollback()`` для НЕЁ не вызывается —
    SQLAlchemy сама откатывает транзакцию только до SAVEPOINT при выходе из
    ``with`` с исключением, а полный ``session.rollback()`` откатил бы весь
    ещё не закоммиченный прогон целиком (тот же паттерн, что в
    ``services/supplier_service.py`` и ``services/brand_engine.py``).

    Отдельно вся функция обёрнута внешним ``try/except``: если исключение
    всё же вышло за пределы per-draft try (prefetch, резолв, финальный
    ``commit()`` — например транзиентная ошибка вида «database is locked»),
    оно конвертируется в ``OzonComplianceAdminError`` после
    ``db.session.rollback()``, а не всплывает наружу голым ``500`` — маршрут
    ловит только ``OzonComplianceAdminError``.

    Возвращает счётчики, честно различающие причину. ``updated`` считает
    черновики, получившие хотя бы одно НОВОЕ (ранее пустое) значение;
    ``refreshed``/``already_current``/``skipped_seller_owned`` — per-атрибут
    счётчики режима обновления (``refresh=True``): обновили своё устаревшее,
    наше и уже совпадает, значение не наше — не тронуто. Плюс
    ``skipped_active_operation``, ``skipped_archived``,
    ``skipped_already_filled`` (черновик вообще не изменился в этом вызове),
    ``failed`` и типовой (не per-черновик) список ``unresolved`` — какие из
    двух compliance-атрибутов вообще не резолвятся для этого типа прямо
    сейчас.
    """
    from models import db, MarketplaceOperation, MarketplaceProductDraft
    from services.ozon_compliance_defaults import (
        apply_to_attributes, resolve_type_defaults,
    )

    type_key = _parse_positive_int(product_type_id, "product_type_id")
    bounded = _bounded_limit(limit)
    if not isinstance(refresh, bool):
        raise OzonComplianceAdminError("refresh должен быть boolean")

    counters = {
        "updated": 0,
        "refreshed": 0,
        "already_current": 0,
        "skipped_active_operation": 0,
        "skipped_archived": 0,
        "skipped_already_filled": 0,
        "skipped_seller_owned": 0,
        "failed": 0,
        "unresolved": [],
    }

    try:
        # Резолв не зависит от конкретного черновика — только от типа. Считаем
        # его ОДИН раз: это и честный список ``unresolved`` для всего прогона
        # (иначе он менялся бы бессмысленно на каждой итерации одним и тем же
        # значением), и fail-fast короткое замыкание, когда для типа не
        # резолвится вообще ничего — тогда ни один черновик не может быть
        # изменён, и помечать их "уже заполнено" было бы неправдой.
        defaults = resolve_type_defaults(type_key)
        counters["unresolved"] = list(defaults.get("unresolved") or [])
        if defaults.get("tnved") is None and defaults.get("marking") is None:
            return counters

        drafts = MarketplaceProductDraft.query.filter_by(
            product_type_id=type_key,
        ).order_by(MarketplaceProductDraft.id.asc()).limit(bounded).all()

        # Одним batched запросом на ВЕСЬ набор черновиков — не по одному на
        # черновик. ``uq_marketplace_operation_active_draft`` в любом случае
        # не допускает больше одной активной операции на ``draft_id``, но
        # здесь нужен просто набор ID, а не количество.
        draft_ids = [draft.id for draft in drafts]
        active_operation_draft_ids = set()
        if draft_ids:
            active_operation_draft_ids = {
                row[0] for row in db.session.query(
                    MarketplaceOperation.draft_id,
                ).filter(
                    MarketplaceOperation.draft_id.in_(draft_ids),
                    MarketplaceOperation.status.in_(ACTIVE_OPERATION_STATUSES),
                ).distinct().all()
            }

        for draft in drafts:
            try:
                if getattr(draft, "status", None) == "archived":
                    counters["skipped_archived"] += 1
                    continue

                has_active_operation = draft.id in active_operation_draft_ids
                if not _draft_is_eligible(
                    draft, has_active_operation=has_active_operation,
                ):
                    counters["skipped_active_operation"] += 1
                    continue

                current = json.loads(draft.attributes_json or "[]")
                if not isinstance(current, list):
                    raise ValueError("attributes_json is not a JSON array")

                current, report = apply_to_attributes(
                    current, type_key, resolved_defaults=defaults,
                )
                newly_filled = list(report.get("applied") or [])
                draft_changed = bool(newly_filled)
                provenance_updates = build_provenance_entries(
                    report, report.get("defaults") or defaults,
                )

                if refresh:
                    provenance = _stored_provenance(draft.provenance_json)
                    refreshed_ids = []
                    for attribute_id, resolved in (
                        (TNVED_ATTRIBUTE_ID, defaults.get("tnved")),
                        (MARKING_ATTRIBUTE_ID, defaults.get("marking")),
                    ):
                        # Just filled from empty above -- not an "already
                        # filled" refresh candidate, and no prior provenance
                        # to compare against.
                        if attribute_id in newly_filled:
                            continue
                        if resolved is None:
                            continue
                        stored_item = _stored_attribute_item(
                            current, attribute_id,
                        )
                        if stored_item is None:
                            continue
                        if not compliance_value_is_ours(
                            attribute_id, stored_item, provenance,
                        ):
                            counters["skipped_seller_owned"] += 1
                            continue
                        desired_values = _compliance_desired_values(
                            attribute_id, resolved,
                        )
                        if stored_item.get("values") == desired_values:
                            counters["already_current"] += 1
                            continue
                        _replace_attribute_values(
                            current, attribute_id, desired_values,
                        )
                        counters["refreshed"] += 1
                        refreshed_ids.append(attribute_id)
                        draft_changed = True
                    if refreshed_ids:
                        refreshed_report = {
                            "applied": refreshed_ids,
                            "evidence": dict(defaults.get("evidence") or {}),
                        }
                        provenance_updates.update(
                            build_provenance_entries(refreshed_report, defaults)
                        )

                if not draft_changed:
                    counters["skipped_already_filled"] += 1
                    continue

                with db.session.begin_nested():
                    draft.attributes_json = json.dumps(
                        current, ensure_ascii=False, sort_keys=True,
                    )
                    if provenance_updates:
                        merged_provenance = _stored_provenance(
                            draft.provenance_json,
                        )
                        merged_provenance.update(provenance_updates)
                        draft.provenance_json = json.dumps(
                            merged_provenance,
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    draft.validation_status = "stale"
                    draft.updated_at = datetime.utcnow()
                    db.session.flush()
                if newly_filled:
                    counters["updated"] += 1
            except Exception:
                logger.exception(
                    "Не удалось применить compliance-дефолты к черновику %s",
                    draft.id,
                )
                counters["failed"] += 1

        db.session.commit()
    except OzonComplianceAdminError:
        db.session.rollback()
        raise
    except Exception:
        logger.exception(
            "apply_to_existing_drafts: неожиданная ошибка для "
            "product_type_id=%s", type_key,
        )
        db.session.rollback()
        raise OzonComplianceAdminError(
            "Не удалось применить решение к существующим черновикам — "
            "техническая ошибка, повторите попытку"
        ) from None

    return counters
