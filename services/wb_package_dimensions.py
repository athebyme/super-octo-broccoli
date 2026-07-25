# -*- coding: utf-8 -*-
"""
Явно заявленные продавцом габариты упаковки для карточек Wildberries.

WB требует положительный ``dimensions.weightBrutto`` в каждом full-replacement
обновлении. Живая карточка при этом может прийти из WB с ``weightBrutto: 0`` и
``isValid: false`` — тогда заблокировано ЛЮБОЕ обновление контента, включая
обновление одних только фото или заголовка.

Подставлять сюда захардкоженный дефолт или AI-оценку нельзя: вес упаковки
определяет логистический тариф WB. Единственный дополнительный допустимый
источник — факт, который продавец сам заявил в ``/settings/product-defaults``
(``ProductDefaults``). Путь СОЗДАНИЯ карточки использует его с самого начала
(``WBProductImporter._build_wb_dimensions``); этот модуль отдаёт ровно те же
заявленные значения, чтобы путь ОБНОВЛЕНИЯ опирался на тот же факт, а не падал
или не фабриковал вес на карточке, которую сама WB считает невалидной.

Модуль ничего не изобретает: если продавец ничего не заявил, он возвращает
пустой результат, и вызывающий код обязан честно отказаться от записи.
Отличие от ``routes.product_defaults.get_defaults_for_product`` принципиальное:
та функция подмешивает захардкоженные ``10x10x5 / 0.1`` и поэтому непригодна как
доказательство факта.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

# Канонические ключи габаритов WB. Порядок фиксирован для стабильного аудита.
DECLARED_DIMENSION_KEYS = ("length", "width", "height", "weightBrutto")

# Заявленные дефолты меняются вручную и редко, а читаются в цикле по карточкам.
# TTL держим коротким: кеш процессный, а web-контейнер запускает несколько
# воркеров, поэтому межворкерная согласованность обеспечивается только TTL.
DECLARED_CACHE_TTL_SECONDS = 60

# Единый seller-facing текст. Ошибка обязана называть конкретное действие, а не
# просто сообщать, что вес невалиден.
MISSING_PACKAGE_WEIGHT_HINT = (
    "Укажите фактический вес упаковки в килограммах: в самой карточке WB, "
    "в наблюдённых данных поставщика либо в «Настройки → Дефолты товаров» "
    "(глобальное правило или правило категории)"
)


def is_usable_dimension_value(value: Any) -> bool:
    """Габарит WB — конечное положительное число; bool числом не считается."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    return math.isfinite(number) and number > 0


def _normalize_declared_value(key: str, value: Any) -> Optional[float]:
    """
    Привести заявленное значение к wire-форме WB.

    Источник уже в целевых единицах (см и кг), поэтому эвристики
    ``wb_content_payload._coerce_dimension_value`` (граммы/миллиметры по имени
    поля) здесь применять нельзя: заявленные 50 кг означают ровно 50 кг.
    """
    if not is_usable_dimension_value(value):
        return None
    number = float(value)
    if key == "weightBrutto":
        # WB отклоняет больше трёх знаков после запятой.
        normalized: float = round(number, 3)
    else:
        normalized = float(int(round(number)))
    if not is_usable_dimension_value(normalized):
        # Округление могло обнулить значение вида 0.0004 — это не факт.
        return None
    if key != "weightBrutto":
        return int(normalized)
    return normalized


def _declared_from_rule(rule: Any) -> Dict[str, Any]:
    """Достать из одного ``ProductDefaults`` только заявленные габариты."""
    if rule is None:
        return {}
    try:
        raw = rule.get_dimensions_dict() or {}
    except Exception:  # pragma: no cover — защита от битой legacy-строки
        logger.warning("Не удалось прочитать заявленные габариты правила", exc_info=False)
        return {}
    declared: Dict[str, Any] = {}
    for key in DECLARED_DIMENSION_KEYS:
        if key not in raw:
            continue
        normalized = _normalize_declared_value(key, raw.get(key))
        if normalized is not None:
            declared[key] = normalized
    return declared


def resolve_declared_package_dimensions(
    seller_id: Any,
    wb_subject_id: Any = None,
) -> Dict[str, Any]:
    """
    Вернуть ТОЛЬКО явно заявленные продавцом габариты упаковки.

    Категорийное правило перекрывает глобальное по каждому отдельному ключу:
    правило категории может задавать один лишь вес, не отменяя заявленные
    глобально длину/ширину/высоту.

    Неактивное правило заявлением не считается. Ошибка чтения БД гасится в
    пустой результат: вызывающий код тогда честно откажет, а не запишет догадку.
    """
    try:
        seller_key = int(seller_id)
    except (TypeError, ValueError):
        return {}
    if seller_key <= 0:
        return {}

    subject_key: Optional[int] = None
    if wb_subject_id is not None and not isinstance(wb_subject_id, bool):
        try:
            subject_key = int(wb_subject_id)
        except (TypeError, ValueError):
            subject_key = None
        if subject_key is not None and subject_key <= 0:
            subject_key = None

    def _load() -> Dict[str, Any]:
        from models import ProductDefaults

        declared = _declared_from_rule(
            ProductDefaults.query.filter_by(
                seller_id=seller_key, rule_type="global", is_active=True
            ).first()
        )
        if subject_key is not None:
            declared.update(
                _declared_from_rule(
                    ProductDefaults.query.filter_by(
                        seller_id=seller_key,
                        rule_type="category",
                        wb_subject_id=subject_key,
                        is_active=True,
                    ).first()
                )
            )
        return declared

    try:
        from services.ttl_cache import cache

        cached = cache.get_or_load(
            f"pkg_dims:{seller_key}:{subject_key}",
            DECLARED_CACHE_TTL_SECONDS,
            _load,
        )
        return dict(cached or {})
    except Exception:
        logger.warning(
            "Не удалось прочитать заявленные габариты упаковки продавца %s",
            seller_key,
            exc_info=False,
        )
        return {}


def plan_declared_dimension_repair(
    live_dimensions: Any,
    declared: Any,
) -> Dict[str, Any]:
    """
    Посчитать патч, чинящий ТОЛЬКО невалидные габариты живой карточки WB.

    Правила, которые нельзя ослаблять:

    * валидное положительное живое значение никогда не перезаписывается —
      заявленный дефолт слабее наблюдённого факта площадки;
    * отсутствующий в живой карточке ключ не фабрикуется: preserve-live
      контракт запрещает выдумывать габарит, которого WB не вернула;
    * ключ, для которого продавец ничего не заявил, попадает в
      ``unresolved_keys`` — вызывающий код обязан честно отказать.
    """
    live: Mapping[str, Any] = (
        live_dimensions if isinstance(live_dimensions, Mapping) else {}
    )
    declared_map: Mapping[str, Any] = (
        declared if isinstance(declared, Mapping) else {}
    )

    patch: Dict[str, Any] = {}
    invalid_live_keys: List[str] = []
    unresolved_keys: List[str] = []

    for key in DECLARED_DIMENSION_KEYS:
        if key not in live:
            # Ключа нет в живой карточке — не наше дело его создавать.
            continue
        if is_usable_dimension_value(live.get(key)):
            continue
        invalid_live_keys.append(key)
        candidate = _normalize_declared_value(key, declared_map.get(key))
        if candidate is None:
            unresolved_keys.append(key)
        else:
            patch[key] = candidate

    return {
        "patch": patch,
        "invalid_live_keys": invalid_live_keys,
        "unresolved_keys": unresolved_keys,
    }


def repair_card_dimensions_in_place(
    card: Dict[str, Any],
    seller_id: Any,
    wb_subject_id: Any = None,
) -> Dict[str, Any]:
    """
    Применить заявленную починку к ``card['dimensions']`` прямо в карточке.

    Используется batch-путями, которые оперируют уже собранной полной
    карточкой. Возвращает тот же отчёт, что и ``plan_declared_dimension_repair``,
    чтобы вызывающий код мог отсеять карточку с ``unresolved_keys`` до записи.
    """
    if not isinstance(card, dict):
        return {"patch": {}, "invalid_live_keys": [], "unresolved_keys": []}

    live_dimensions = card.get("dimensions")
    if not isinstance(live_dimensions, Mapping):
        return {"patch": {}, "invalid_live_keys": [], "unresolved_keys": []}

    probe = plan_declared_dimension_repair(live_dimensions, {})
    if not probe["invalid_live_keys"]:
        # Живые габариты валидны — ни читать дефолты, ни трогать карточку не нужно.
        return probe

    declared = resolve_declared_package_dimensions(seller_id, wb_subject_id)
    plan = plan_declared_dimension_repair(live_dimensions, declared)
    if plan["patch"]:
        repaired = dict(live_dimensions)
        repaired.update(plan["patch"])
        card["dimensions"] = repaired
    return plan


def describe_unresolved_dimensions(plan: Mapping[str, Any]) -> str:
    """Человекочитаемая причина отказа для durable job/result и UI."""
    unresolved = list((plan or {}).get("unresolved_keys") or [])
    if not unresolved:
        return ""
    human = {
        "length": "длина упаковки",
        "width": "ширина упаковки",
        "height": "высота упаковки",
        "weightBrutto": "вес упаковки",
    }
    names = ", ".join(human.get(key, key) for key in unresolved)
    return (
        f"WB считает невалидными габариты карточки ({names}), "
        f"а заявленного значения нет. {MISSING_PACKAGE_WEIGHT_HINT}"
    )
