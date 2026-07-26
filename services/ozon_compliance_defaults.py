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


def type_tnved_dictionary_status(product_type_id: Any) -> dict:
    """Полный статус официального словаря ТН ВЭД одного Ozon product type.

    Единая точка правды: и ``get_type_tnved_dictionary`` (UI datalist), и
    ``is_type_tnved_code_available`` (проверка кода перед сохранением решения
    в ``save_decision``) читают ОДИН и тот же набор строк, отфильтрованный
    тем же admin restriction, что и ``resolve_tnved`` — иначе код мог бы
    пройти UI-подсказку, но не пройти фактическое разрешение (или наоборот).

    ``is_fresh=False`` означает, что словарь недоступен/устарел: вызывающий
    код обязан считать `entries` пустым списком, а не молчаливым «кодов нет».
    """
    try:
        type_key = int(product_type_id)
    except (TypeError, ValueError):
        return {"is_fresh": False, "entries": []}

    definition = _tnved_definition(type_key)
    if definition is None or not definition.is_available:
        return {"is_fresh": False, "entries": []}
    if not _dictionary_is_fresh(definition):
        return {"is_fresh": False, "entries": []}

    restriction = set(getattr(definition, "restriction_value_ids", None) or [])
    entries = []
    for row in _dictionary_rows(definition):
        if restriction and row.external_value_id not in restriction:
            continue
        code = dictionary_code(row.value)
        if not code:
            continue
        entries.append({
            "code": code,
            "value": row.value,
            "external_value_id": row.external_value_id,
        })
    return {"is_fresh": True, "entries": entries}


def get_type_tnved_dictionary(product_type_id: Any, *, limit: int = 1000) -> dict:
    """Официальный словарь ТН ВЭД одного типа — для UI datalist экрана.

    Отдаёт значения ТОЛЬКО когда словарь свежий; иначе — пустой список и
    явный ``is_fresh=False``, чтобы UI не выдавал устаревшие/отсутствующие
    подсказки за актуальные. ``values`` ограничен ``limit`` (защита от
    отправки в браузер десятков тысяч `<option>` для крупных категорий);
    ``total_count`` — точное число доступных значений НЕЗАВИСИМО от лимита
    отображения, чтобы UI мог честно сказать «показаны не все».
    """
    status = type_tnved_dictionary_status(product_type_id)
    entries = status["entries"]
    bounded_limit = max(0, int(limit))
    return {
        "is_fresh": status["is_fresh"],
        "values": entries[:bounded_limit],
        "total_count": len(entries),
    }


def is_type_tnved_code_available(product_type_id: Any, code: Any) -> Optional[bool]:
    """Проверить код по свежему словарю типа ДО сохранения решения.

    ``None``  — словарь несвежий/отсутствует: решение нельзя ни подтвердить,
                ни отклонить по словарю, вызывающий код обязан фейлиться
                closed (не сохранять).
    ``True``  — код найден среди официальных значений этого типа.
    ``False`` — словарь свежий, но код среди значений не найден (опечатка
                или чужой код).
    """
    status = type_tnved_dictionary_status(product_type_id)
    if not status["is_fresh"]:
        return None
    target = normalize_code(code)
    if not target:
        return False
    return any(entry["code"] == target for entry in status["entries"])


def type_tnved_dictionary_summary_batch(product_type_ids: Any) -> dict:
    """Готовность и размер словаря ТН ВЭД для набора типов ОДНИМ проходом.

    ``type_tnved_dictionary_status`` на один тип делает: SELECT определения,
    переход по `product_type -> category`/`marketplace` для расчёта
    свежести (``dictionary_is_fresh``) и SELECT всех строк словаря. В цикле
    по N задействованным типам ``list_type_rows`` это было бы N+1 —
    здесь определения грузятся batched с ``joinedload`` нужных связей, а
    число строк — одним GROUP BY. Admin restriction (редкий кейс: не у
    каждого типа) уточняется отдельным bounded запросом только для тех
    типов, у которых он реально задан — не по всем N типам разом.

    Возвращает ``{product_type_id: {"ready": bool, "count": int}}`` для
    каждого запрошенного id (даже если словаря/определения нет вовсе).
    """
    from sqlalchemy.orm import joinedload
    from models import (
        db, MarketplaceAttributeDefinition, MarketplaceAttributeValue,
        MarketplaceProductType,
    )
    from services.ozon_reference_service import OzonReferenceService

    try:
        ids = sorted({int(pid) for pid in product_type_ids})
    except (TypeError, ValueError):
        return {}

    result = {pid: {"ready": False, "count": 0} for pid in ids}
    if not ids:
        return result

    definitions = MarketplaceAttributeDefinition.query.filter(
        MarketplaceAttributeDefinition.product_type_id.in_(ids),
        MarketplaceAttributeDefinition.external_attribute_id == TNVED_ATTRIBUTE_ID,
    ).options(
        joinedload(MarketplaceAttributeDefinition.product_type)
        .joinedload(MarketplaceProductType.category),
        joinedload(MarketplaceAttributeDefinition.product_type)
        .joinedload(MarketplaceProductType.marketplace),
    ).all()

    fresh_by_type = {
        definition.product_type_id: definition
        for definition in definitions
        if definition.is_available
        and OzonReferenceService.dictionary_is_fresh(definition)
    }
    if not fresh_by_type:
        return result

    attribute_ids = [definition.id for definition in fresh_by_type.values()]
    counts_by_attribute = dict(
        db.session.query(
            MarketplaceAttributeValue.attribute_id,
            db.func.count(MarketplaceAttributeValue.id),
        ).filter(
            MarketplaceAttributeValue.attribute_id.in_(attribute_ids),
            MarketplaceAttributeValue.is_available.is_(True),
        ).group_by(MarketplaceAttributeValue.attribute_id).all()
    )

    for type_id, definition in fresh_by_type.items():
        restriction = set(getattr(definition, "restriction_value_ids", None) or [])
        if restriction:
            count = MarketplaceAttributeValue.query.filter(
                MarketplaceAttributeValue.attribute_id == definition.id,
                MarketplaceAttributeValue.is_available.is_(True),
                MarketplaceAttributeValue.external_value_id.in_(restriction),
            ).count()
        else:
            count = counts_by_attribute.get(definition.id, 0)
        result[type_id] = {"ready": True, "count": count}
    return result


def _active_registry_version():
    from models import OzonMarkingRegistryVersion
    return OzonMarkingRegistryVersion.query.filter_by(status="active").first()


def _registry_rules(version) -> list:
    from models import OzonMarkingRule
    return OzonMarkingRule.query.filter_by(
        registry_version_id=version.id,
    ).all()


def _marking_from_prefixes(
    code: str, prefixes: list, is_complete: bool,
) -> Optional[bool]:
    """Чистое сравнение кода с уже полученным набором префиксов правил.

    Вынесено отдельно от ``_marking_for_version``, чтобы batch-резолвер
    (``resolve_marking_batch``) мог посчитать маркировку для множества кодов
    по ОДНОМУ уже полученному набору префиксов, не читая заново активную
    версию и её правила на каждый код — иначе вызывающий код в цикле по N
    кодам делает 2*N запросов к БД.
    """
    matched = any(prefix and code.startswith(prefix) for prefix in prefixes)
    if matched:
        return True
    return False if is_complete else None


def _marking_for_version(code: str, version) -> Optional[bool]:
    """Определить признак маркировки по уже полученной версии перечня.

    Перечень содержит только положительные правила без признака
    исключения, поэтому для ``True`` достаточно факта совпадения хотя бы
    одного непустого нормализованного префикса — самый длинный совпавший
    префикс ни на что не влияет и отдельно не выбирается. Поддержка
    правил-исключений потребовала бы отдельного поля в ``OzonMarkingRule``
    и отдельного решения.

    Версия передаётся параметром, а не запрашивается заново, чтобы вызывающий
    код (``resolve_marking`` и ``resolve_type_defaults``) считал маркировку и
    ссылался на неё в evidence по одному и тому же снимку активной версии.
    """
    if version is None:
        return None

    prefixes = [
        normalize_code(rule.code_prefix) for rule in _registry_rules(version)
    ]
    return _marking_from_prefixes(code, prefixes, bool(version.is_complete))


def resolve_marking(tnved_code: Any) -> Optional[bool]:
    """Вывести признак маркировки из кода по активной версии перечня.

    ``True``  — код попал в перечень маркируемых групп (см. ``_marking_for_version``).
    ``False`` — не попал, но перечень объявлен исчерпывающим.
    ``None``  — ответа нет: перечень отсутствует либо не объявлен полным.
    """
    code = normalize_code(tnved_code)
    if not code:
        return None

    version = _active_registry_version()
    return _marking_for_version(code, version)


def resolve_marking_batch(codes: Any) -> dict:
    """Вывести признак маркировки для набора кодов ОДНИМ проходом по БД.

    ``resolve_marking(code)`` в цикле по N кодам делает 2*N запросов:
    активная версия перечня и её правила читаются заново на каждый вызов.
    Здесь оба запроса выполняются ровно один раз независимо от количества
    кодов. Предназначено для admin-агрегаций (``list_type_rows``,
    ``preview_registry_switch``), которым нужна маркировка сразу для многих
    решений; публичный ``resolve_marking`` для одиночного кода не меняется и
    остаётся основным API для остальных вызывающих мест.

    Возвращает ``{нормализованный_код: True|False|None}``. Пустой/невалидный
    исходный код в результат не попадает — вызывающий код сам решает, что
    показать при отсутствии кода.
    """
    normalized = {normalize_code(raw) for raw in codes}
    normalized.discard("")
    if not normalized:
        return {}

    version = _active_registry_version()
    if version is None:
        return {code: None for code in normalized}

    prefixes = [
        normalize_code(rule.code_prefix) for rule in _registry_rules(version)
    ]
    is_complete = bool(version.is_complete)
    return {
        code: _marking_from_prefixes(code, prefixes, is_complete)
        for code in normalized
    }


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

    # Версия берётся ровно один раз: и marking, и evidence обязаны
    # ссылаться на один и тот же снимок активной версии перечня, иначе
    # переключение версии между двумя SELECT рассинхронизирует результат
    # с его собственным evidence.
    version = _active_registry_version()
    marking = _marking_for_version(tnved["code"], version)
    if marking is None:
        unresolved.append(MARKING_ATTRIBUTE_ID)
    else:
        evidence["registry_version_id"] = version.id

    return {
        "tnved": tnved,
        "marking": marking,
        "unresolved": unresolved,
        "evidence": evidence,
    }


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


def _set_value(
    attributes: list, attribute_id: str, complex_id: str, values: list,
) -> None:
    """Fill ``attribute_id`` in place, never adding a second entry for it.

    A seller may legitimately save a draft with an explicitly cleared
    attribute (``values: []``); ``_has_value`` correctly treats that as
    empty, but appending a second object with the same ``attribute_id``
    would produce a payload with two entries for one Ozon attribute, which
    is undefined behaviour on the provider side.  If an entry with this
    identity already exists (empty or not — callers only reach this helper
    after confirming it is empty), its slot is replaced in place, preserving
    position in the list; a brand new dict is built rather than mutating the
    existing one, and the ``attribute_id`` is written in its canonical
    string form regardless of how the existing entry represented it.
    """
    for index, item in enumerate(attributes):
        if not isinstance(item, dict):
            continue
        if str(item.get("attribute_id")) != attribute_id:
            continue
        attributes[index] = {
            "attribute_id": attribute_id,
            "complex_id": complex_id,
            "values": values,
        }
        return
    attributes.append({
        "attribute_id": attribute_id,
        "complex_id": complex_id,
        "values": values,
    })


def apply_to_attributes(
    attributes: Any,
    product_type_id: Any,
    *,
    resolved_defaults: Optional[dict] = None,
) -> tuple:
    """Дозаполнить compliance-атрибуты из админского решения.

    Заполняются только ПУСТЫЕ атрибуты; значение, уже присутствующее в
    черновике, не перезаписывается никогда.  При нерешённом источнике не
    записывается ничего — черновик остаётся невалидным с явной причиной.

    ``resolved_defaults`` — необязательный уже посчитанный результат
    ``resolve_type_defaults(product_type_id)``.  По умолчанию ``None``:
    поведение не меняется, функция резолвит сама на каждом вызове (именно
    так её вызывает ``_auto_map_attributes`` в ``services/marketplace_drafts.py``
    — на одну карточку). Явная передача нужна вызывающему коду, который уже
    посчитал резолв ОДИН раз для набора карточек одного и того же
    ``product_type_id`` (массовый локальный прогон
    ``services.ozon_compliance_admin.apply_to_existing_drafts``) — тогда
    здесь не выполняется повторный SELECT/резолв на каждую карточку.
    """
    result = list(attributes) if isinstance(attributes, list) else []
    report = {"applied": [], "unresolved": [], "evidence": {}}

    defaults = (
        resolved_defaults if resolved_defaults is not None
        else resolve_type_defaults(product_type_id)
    )
    report["unresolved"] = list(defaults.get("unresolved") or [])
    report["evidence"] = dict(defaults.get("evidence") or {})
    # Exposed so a caller that wants provenance (`build_provenance_entries`)
    # never has to resolve the same type a second time — this is exactly the
    # already-resolved dict used above, not a re-resolved copy.
    report["defaults"] = defaults

    tnved = defaults.get("tnved")
    if tnved and not _has_value(result, TNVED_ATTRIBUTE_ID):
        _set_value(result, TNVED_ATTRIBUTE_ID, "0", [{
            "dictionary_value_id": tnved["external_value_id"],
            "value": tnved["value"],
        }])
        report["applied"].append(TNVED_ATTRIBUTE_ID)

    marking = defaults.get("marking")
    if marking is not None and not _has_value(result, MARKING_ATTRIBUTE_ID):
        _set_value(result, MARKING_ATTRIBUTE_ID, "0", [
            {"value": "true" if marking else "false"},
        ])
        report["applied"].append(MARKING_ATTRIBUTE_ID)

    return result, report


def build_provenance_entries(report: Optional[dict], defaults: Optional[dict]) -> dict:
    """Провенанс заполненных compliance-атрибутов.

    Записывается точное значение, которое слой поставил: обновлять его позже
    (``services.ozon_compliance_admin.compliance_value_is_ours``) разрешено
    только пока оно побайтово равно записанному здесь. Любая последующая
    правка продавца автоматически выводит атрибут из-под автоматического
    обновления — сравнение всегда идёт с этой записью, а не с текущим
    состоянием черновика.

    Ничего не резолвит и не обращается к БД: ``report`` — результат
    ``apply_to_attributes`` (со списком фактически ``applied`` атрибутов), а
    ``defaults`` — тот же ``resolve_type_defaults(...)`` снимок, из которого
    эти значения были взяты (доступен как ``report["defaults"]``, если
    вызывающий код не резолвил его отдельно).
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
