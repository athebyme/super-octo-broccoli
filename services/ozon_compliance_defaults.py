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


def _active_registry_version():
    from models import OzonMarkingRegistryVersion
    return OzonMarkingRegistryVersion.query.filter_by(status="active").first()


def _registry_rules(version) -> list:
    from models import OzonMarkingRule
    return OzonMarkingRule.query.filter_by(
        registry_version_id=version.id,
    ).all()


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

    matched = any(
        prefix and code.startswith(prefix)
        for prefix in (
            normalize_code(rule.code_prefix) for rule in _registry_rules(version)
        )
    )
    if matched:
        return True
    return False if bool(version.is_complete) else None


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
