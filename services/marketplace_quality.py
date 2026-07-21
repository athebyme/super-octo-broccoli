"""Deterministic marketplace-scoped content and performance quality scoring."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, FrozenSet, Mapping, Optional
import json

from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from models import (
    MarketplaceAttributeDefinition,
    MarketplaceListing,
    MarketplaceProductType,
    MarketplaceQualityAssessment,
    db,
)
from services.marketplace_accounts import (
    MarketplaceAccountNotFound,
    MarketplaceAccountService,
)
from services.marketplace_analytics import MarketplaceAnalyticsService
from services.ozon_reference_service import OzonReferenceService


class MarketplaceQualityError(RuntimeError):
    status_code = 400
    code = "marketplace_quality_error"


class MarketplaceQualityValidationError(MarketplaceQualityError):
    status_code = 400
    code = "invalid_marketplace_quality_request"


class MarketplaceQualityNotFound(MarketplaceQualityError):
    status_code = 404
    code = "marketplace_quality_not_found"


@dataclass(frozen=True)
class MarketplaceQualityInput:
    seller_id: int
    marketplace_id: int
    account_id: int
    listing_id: int
    marketplace_code: str
    listing_fingerprint: str
    title: str
    description: str
    filled_attribute_ids: FrozenSet[str]
    media_count: int
    barcode_count: int
    has_price: bool
    moderation_error_count: int
    normalized_status: str


@dataclass(frozen=True)
class MarketplaceQualitySchemaContext:
    schema_hash: str
    required_attribute_ids: FrozenSet[str]
    filterable_attribute_ids: FrozenSet[str]


QUALITY_DEFINITION_VERSION = "marketplace-quality-v1"
DIMENSION_WEIGHTS = {
    "attributes": 35,
    "media": 20,
    "description": 15,
    "title": 15,
    "barcodes": 5,
    "price": 5,
    "publication_health": 5,
}

REASON_DEFINITIONS = {
    "ozon_schema_stale": ("Справочник категории Ozon устарел", "critical", 25.0),
    "ozon_schema_mapping_missing": ("Не определён точный тип товара Ozon", "critical", 25.0),
    "ozon_listing_attributes_missing": ("Нет подтверждённого снимка характеристик", "critical", 20.0),
    "ozon_listing_snapshot_stale": ("Снимок характеристик Ozon устарел", "critical", 20.0),
    "ozon_missing_required_attribute": ("Не заполнены обязательные характеристики", "critical", 16.0),
    "ozon_missing_filterable_attribute": ("Не заполнены характеристики для фильтров", "warning", 7.0),
    "ozon_few_media": ("Мало изображений", "warning", 10.0),
    "ozon_weak_description": ("Слабое описание", "warning", 8.0),
    "ozon_weak_title": ("Слабый заголовок", "warning", 8.0),
    "ozon_missing_barcodes": ("Нет штрихкода", "warning", 4.0),
    "ozon_missing_price": ("Нет подтверждённой цены", "critical", 10.0),
    "ozon_moderation_error": ("Есть ошибки модерации Ozon", "critical", 20.0),
    "ozon_listing_inactive": ("Карточка не активна", "warning", 8.0),
    "ozon_no_analytics_signal": ("Нет завершённого снимка аналитики", "info", 0.0),
    "ozon_low_views": ("Мало просмотров Ozon", "warning", 8.0),
    "ozon_low_cart_conversion": ("Низкая конверсия в корзину Ozon", "warning", 8.0),
    "ozon_no_orders": ("Есть просмотры, но нет заказов Ozon", "warning", 8.0),
    "ozon_high_cancellation_rate": ("Высокая доля отмен Ozon", "warning", 6.0),
    "ozon_high_return_rate": ("Высокая доля возвратов Ozon", "warning", 6.0),
}


def _reason(
    code: str,
    *,
    details: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    label, severity, impact = REASON_DEFINITIONS[code]
    result = {
        "code": code,
        "label": label,
        "severity": severity,
        "impact": impact,
        "marketplace_code": "ozon",
    }
    if details:
        result["details"] = dict(details)
    return result


def _dimension(score: float, status: str, hint: str) -> Dict[str, Any]:
    return {
        "score": round(max(0.0, min(100.0, float(score))), 1),
        "status": status,
        "hint": hint,
    }


def evaluate_ozon_quality(
    quality_input: MarketplaceQualityInput,
    schema: MarketplaceQualitySchemaContext,
    *,
    metrics: Optional[Mapping[str, float]] = None,
) -> Dict[str, Any]:
    """Pure quality scorer; all provider/ORM work happens before this call."""
    filled = quality_input.filled_attribute_ids
    required = schema.required_attribute_ids
    filterable = schema.filterable_attribute_ids
    missing_required = sorted(required - filled)
    missing_filterable = sorted((filterable - required) - filled)
    weighted_total = len(required) * 3 + len(filterable - required)
    weighted_filled = len(required & filled) * 3 + len((filterable - required) & filled)
    attribute_score = 100.0 if weighted_total == 0 else 100.0 * weighted_filled / weighted_total

    reasons = []
    if missing_required:
        reasons.append(_reason(
            "ozon_missing_required_attribute",
            details={
                "missing_count": len(missing_required),
                "attribute_ids": missing_required[:50],
            },
        ))
    if missing_filterable:
        reasons.append(_reason(
            "ozon_missing_filterable_attribute",
            details={
                "missing_count": len(missing_filterable),
                "attribute_ids": missing_filterable[:50],
            },
        ))
    dimensions = {
        "attributes": _dimension(
            attribute_score,
            "error" if missing_required else "warning" if missing_filterable else "ok",
            (
                f"Не заполнено обязательных: {len(missing_required)}"
                if missing_required
                else f"Не заполнено для фильтров: {len(missing_filterable)}"
                if missing_filterable
                else "Схема категории заполнена"
            ),
        )
    }

    media_count = quality_input.media_count
    if media_count >= 5:
        media_score = 100
    elif media_count >= 3:
        media_score = 70
    elif media_count >= 1:
        media_score = 30
    else:
        media_score = 0
    if media_count < 3:
        reasons.append(_reason(
            "ozon_few_media",
            details={"media_count": media_count, "recommended_minimum": 5},
        ))
    dimensions["media"] = _dimension(
        media_score,
        "ok" if media_count >= 5 else "warning" if media_count else "error",
        f"Изображений: {media_count}; ориентир качества — 5+",
    )

    description_length = len(quality_input.description.strip())
    if description_length >= 500:
        description_score = 100
    elif description_length >= 200:
        description_score = 70
    elif description_length:
        description_score = 40
    else:
        description_score = 0
    if description_score < 70:
        reasons.append(_reason(
            "ozon_weak_description",
            details={"length": description_length},
        ))
    dimensions["description"] = _dimension(
        description_score,
        "ok" if description_score >= 70 else "warning" if description_length else "error",
        f"Длина описания: {description_length}",
    )

    title_length = len(quality_input.title.strip())
    title_score = 100 if 20 <= title_length <= 120 else 60 if title_length else 0
    if title_score < 100:
        reasons.append(_reason(
            "ozon_weak_title",
            details={"length": title_length},
        ))
    dimensions["title"] = _dimension(
        title_score,
        "ok" if title_score == 100 else "warning" if title_length else "error",
        f"Длина заголовка: {title_length}",
    )

    if quality_input.barcode_count:
        dimensions["barcodes"] = _dimension(100, "ok", "Штрихкод указан")
    else:
        dimensions["barcodes"] = _dimension(0, "warning", "Штрихкод не найден")
        reasons.append(_reason("ozon_missing_barcodes"))

    if quality_input.has_price:
        dimensions["price"] = _dimension(100, "ok", "Цена подтверждена")
    else:
        dimensions["price"] = _dimension(0, "error", "Цена не подтверждена")
        reasons.append(_reason("ozon_missing_price"))

    publication_score = 100
    publication_status = "ok"
    publication_hint = "Карточка без ошибок модерации"
    if quality_input.moderation_error_count:
        publication_score = 0
        publication_status = "error"
        publication_hint = f"Ошибок модерации: {quality_input.moderation_error_count}"
        reasons.append(_reason(
            "ozon_moderation_error",
            details={"error_count": quality_input.moderation_error_count},
        ))
    elif quality_input.normalized_status != "active":
        publication_score = 50
        publication_status = "warning"
        publication_hint = f"Статус карточки: {quality_input.normalized_status}"
        reasons.append(_reason(
            "ozon_listing_inactive",
            details={"normalized_status": quality_input.normalized_status},
        ))
    dimensions["publication_health"] = _dimension(
        publication_score,
        publication_status,
        publication_hint,
    )

    metric_values = dict(metrics or {})
    if metrics is None or not metric_values:
        reasons.append(_reason("ozon_no_analytics_signal"))
    else:
        views_raw = metric_values.get("views")
        orders_raw = metric_values.get("ordered_units")
        conversion_raw = metric_values.get("cart_conversion_percent")
        cancellations_raw = metric_values.get("cancelled_units")
        delivered_raw = metric_values.get("delivered_units")
        returns_raw = metric_values.get("returned_units")
        views = float(views_raw or 0) if views_raw is not None else None
        orders = float(orders_raw or 0) if orders_raw is not None else None
        cart_conversion = (
            float(conversion_raw or 0) if conversion_raw is not None else None
        )
        cancellations = (
            float(cancellations_raw or 0)
            if cancellations_raw is not None else None
        )
        delivered = (
            float(delivered_raw or 0) if delivered_raw is not None else None
        )
        returns = float(returns_raw or 0) if returns_raw is not None else None
        if views is not None and views < 30:
            reasons.append(_reason(
                "ozon_low_views",
                details={"views": views, "threshold": 30},
            ))
        if (
            views is not None
            and cart_conversion is not None
            and views >= 100
            and cart_conversion < 4
        ):
            reasons.append(_reason(
                "ozon_low_cart_conversion",
                details={"percent": cart_conversion, "threshold": 4},
            ))
        if views is not None and orders is not None and views >= 100 and orders == 0:
            reasons.append(_reason(
                "ozon_no_orders",
                details={"views": views},
            ))
        if (
            orders is not None
            and cancellations is not None
            and orders >= 5
            and cancellations / orders * 100 > 30
        ):
            reasons.append(_reason(
                "ozon_high_cancellation_rate",
                details={
                    "percent": round(cancellations / orders * 100, 1),
                    "threshold": 30,
                },
            ))
        if (
            delivered is not None
            and returns is not None
            and delivered >= 5
            and returns / delivered * 100 > 20
        ):
            reasons.append(_reason(
                "ozon_high_return_rate",
                details={
                    "percent": round(returns / delivered * 100, 1),
                    "threshold": 20,
                },
            ))

    weighted_score = sum(
        dimensions[name]["score"] * weight
        for name, weight in DIMENSION_WEIGHTS.items()
    ) / 100.0
    score = round(weighted_score, 1)
    impact = round(sum(float(reason["impact"]) for reason in reasons), 1)
    has_critical = any(reason["severity"] == "critical" for reason in reasons)
    has_warning = any(reason["severity"] == "warning" for reason in reasons)
    if has_critical or score < 50:
        severity = "critical"
    elif has_warning or score < 70:
        severity = "warning"
    elif score < 90:
        severity = "good"
    else:
        severity = "excellent"
    for name, weight in DIMENSION_WEIGHTS.items():
        dimensions[name]["weight"] = weight
    return {
        "status": "scored",
        "severity": severity,
        "score": score,
        "impact": impact,
        "breakdown": dimensions,
        "reasons": reasons,
        "metrics": metric_values,
    }


class MarketplaceQualityService:
    MAX_BATCH = 500
    LISTING_HARD_TTL = timedelta(hours=48)
    ALLOWED_SEVERITIES = {"critical", "warning", "good", "excellent"}
    ALLOWED_REASON_CODES = frozenset(REASON_DEFINITIONS)

    @staticmethod
    def _positive_integer(
        value: Any,
        field_name: str,
        *,
        maximum: Optional[int] = None,
    ) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise MarketplaceQualityValidationError(
                f"{field_name} должен быть положительным целым числом"
            )
        if maximum is not None and value > maximum:
            raise MarketplaceQualityValidationError(
                f"{field_name} превышает лимит {maximum}"
            )
        return value

    @classmethod
    def _exact_ids(cls, values: Any, field_name: str) -> list:
        if not isinstance(values, list) or not values or len(values) > cls.MAX_BATCH:
            raise MarketplaceQualityValidationError(
                f"{field_name} должен быть массивом 1..{cls.MAX_BATCH}"
            )
        result = []
        seen = set()
        for value in values:
            value = cls._positive_integer(value, "listing_id")
            if value in seen:
                raise MarketplaceQualityValidationError(
                    f"{field_name} не должен содержать дубли"
                )
            seen.add(value)
            result.append(value)
        return result

    @staticmethod
    def _json_list(raw: Optional[str]) -> list:
        try:
            value = json.loads(raw or "[]")
        except (TypeError, json.JSONDecodeError):
            return []
        return value if isinstance(value, list) else []

    @staticmethod
    def _json_object(raw: Optional[str]) -> dict:
        try:
            value = json.loads(raw or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _stable_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def _account(cls, *, seller_id: int, account_id: int):
        seller_id = cls._positive_integer(seller_id, "seller_id")
        account_id = cls._positive_integer(account_id, "account_id")
        try:
            return MarketplaceAccountService.get_owned_account(
                seller_id=seller_id,
                account_id=account_id,
                marketplace_code="ozon",
            )
        except MarketplaceAccountNotFound:
            raise MarketplaceQualityNotFound("Кабинет Ozon не найден") from None

    @classmethod
    def _listing_input(cls, listing: MarketplaceListing) -> MarketplaceQualityInput:
        attributes = cls._json_list(listing.attributes_json)
        complex_groups = cls._json_list(listing.complex_attributes_json)
        filled = set()

        def collect(raw_attributes: Any) -> None:
            if not isinstance(raw_attributes, list):
                return
            for raw in raw_attributes[:2000]:
                if not isinstance(raw, dict):
                    continue
                attribute_id = raw.get("id")
                values = raw.get("values")
                if not isinstance(attribute_id, str) or not isinstance(values, list):
                    continue
                if any(
                    isinstance(value, dict)
                    and (
                        value.get("dictionary_value_id") not in (None, "")
                        or value.get("value") not in (None, "")
                    )
                    for value in values[:500]
                ):
                    filled.add(attribute_id)

        collect(attributes)
        for group in complex_groups[:200]:
            if isinstance(group, dict):
                collect(group.get("attributes"))

        media = cls._json_object(listing.media_json)
        media_urls = []
        primary = media.get("primary_image")
        if isinstance(primary, str) and primary:
            media_urls.append(primary)
        images = media.get("images")
        if isinstance(images, list):
            media_urls.extend(
                value for value in images[:100]
                if isinstance(value, str) and value
            )
        barcodes = cls._json_list(listing.barcodes_json)
        prices = cls._json_object(listing.price_summary_json)
        moderation_errors = cls._json_list(listing.moderation_errors_json)
        return MarketplaceQualityInput(
            seller_id=listing.seller_id,
            marketplace_id=listing.marketplace_id,
            account_id=listing.account_id,
            listing_id=listing.id,
            marketplace_code="ozon",
            listing_fingerprint=listing.sync_fingerprint,
            title=listing.title or "",
            description=listing.description or "",
            filled_attribute_ids=frozenset(filled),
            media_count=len(set(media_urls)),
            barcode_count=len({str(value) for value in barcodes if value not in (None, "")}),
            has_price=bool(prices.get("available")),
            moderation_error_count=len(moderation_errors),
            normalized_status=listing.normalized_status,
        )

    @classmethod
    def _unscorable(
        cls,
        code: str,
        *,
        status: str,
    ) -> Dict[str, Any]:
        reason = _reason(code)
        return {
            "status": status,
            "severity": "critical",
            "score": None,
            "impact": reason["impact"],
            "breakdown": {},
            "reasons": [reason],
            "metrics": {},
        }

    @classmethod
    def recompute_account(
        cls,
        *,
        seller_id: int,
        account_id: int,
        listing_ids: Optional[list] = None,
        limit: int = 200,
        offset: int = 0,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        account = cls._account(seller_id=seller_id, account_id=account_id)
        limit = cls._positive_integer(limit, "limit", maximum=cls.MAX_BATCH)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise MarketplaceQualityValidationError(
                "offset должен быть неотрицательным целым числом"
            )
        query = MarketplaceListing.query.options(
            joinedload(MarketplaceListing.product_type).joinedload(
                MarketplaceProductType.category
            ),
            joinedload(MarketplaceListing.product_type).joinedload(
                MarketplaceProductType.marketplace
            ),
        ).filter(
            MarketplaceListing.seller_id == account.seller_id,
            MarketplaceListing.marketplace_id == account.marketplace_id,
            MarketplaceListing.account_id == account.id,
        )
        explicit_ids = None
        if listing_ids is not None:
            explicit_ids = cls._exact_ids(listing_ids, "listing_ids")
            if offset != 0 or len(explicit_ids) > limit:
                raise MarketplaceQualityValidationError(
                    "Явный listing_ids обрабатывается exact-set без offset "
                    "и должен полностью помещаться в limit"
                )
            query = query.filter(MarketplaceListing.id.in_(explicit_ids))
        else:
            query = query.filter(
                MarketplaceListing.is_available.is_(True),
                MarketplaceListing.is_archived.is_(False),
            )
        total = query.count()
        listings = query.order_by(MarketplaceListing.id.asc()).offset(offset).limit(limit).all()
        if explicit_ids is not None and {item.id for item in listings} != set(explicit_ids):
            raise MarketplaceQualityNotFound(
                "Одна или несколько карточек не принадлежат кабинету"
            )
        product_type_ids = {
            listing.product_type_id
            for listing in listings
            if listing.product_type_id is not None
        }
        definitions_by_type: Dict[int, list] = {item: [] for item in product_type_ids}
        if product_type_ids:
            definitions = MarketplaceAttributeDefinition.query.filter(
                MarketplaceAttributeDefinition.marketplace_id == account.marketplace_id,
                MarketplaceAttributeDefinition.product_type_id.in_(product_type_ids),
                MarketplaceAttributeDefinition.is_available.is_(True),
            ).all()
            for definition in definitions:
                definitions_by_type.setdefault(definition.product_type_id, []).append(definition)

        current_time = now or datetime.utcnow()
        analytics_sync, metrics_by_listing = MarketplaceAnalyticsService.latest_listing_metrics(
            seller_id=account.seller_id,
            account_id=account.id,
            listing_ids=[listing.id for listing in listings],
            period_code="30d",
            now=current_time,
        )
        existing = {
            item.listing_id: item
            for item in MarketplaceQualityAssessment.query.filter(
                MarketplaceQualityAssessment.seller_id == account.seller_id,
                MarketplaceQualityAssessment.account_id == account.id,
                MarketplaceQualityAssessment.listing_id.in_(
                    [listing.id for listing in listings] or [-1]
                ),
            ).all()
        }
        scored = stale = unscorable = 0
        for listing in listings:
            quality_input = cls._listing_input(listing)
            product_type = listing.product_type
            schema_hash = None
            if product_type is None:
                result = cls._unscorable(
                    "ozon_schema_mapping_missing",
                    status="unscorable",
                )
                unscorable += 1
            elif (
                product_type.category is None
                or product_type.marketplace is None
                or product_type.marketplace_id != listing.marketplace_id
                or product_type.external_type_id != listing.external_type_id
                or product_type.category.external_category_id
                != listing.external_category_id
            ):
                result = cls._unscorable(
                    "ozon_schema_mapping_missing",
                    status="unscorable",
                )
                unscorable += 1
            elif not OzonReferenceService.reference_is_fresh(
                product_type,
                now=current_time,
            ):
                result = cls._unscorable(
                    "ozon_schema_stale",
                    status="schema_stale",
                )
                schema_hash = product_type.attributes_schema_hash
                stale += 1
            elif listing.attributes_synced_at is None:
                result = cls._unscorable(
                    "ozon_listing_attributes_missing",
                    status="unscorable",
                )
                schema_hash = product_type.attributes_schema_hash
                unscorable += 1
            elif listing.attributes_synced_at < current_time - cls.LISTING_HARD_TTL:
                result = cls._unscorable(
                    "ozon_listing_snapshot_stale",
                    status="unscorable",
                )
                schema_hash = product_type.attributes_schema_hash
                unscorable += 1
            else:
                schema_hash = product_type.attributes_schema_hash
                definitions = definitions_by_type.get(product_type.id, [])
                expected_count = int(product_type.attributes_count or 0)
                expected_required = int(
                    product_type.required_attributes_count or 0
                )
                actual_required = sum(
                    1 for item in definitions if item.is_required
                )
                if (
                    product_type.attributes_sync_status != "success"
                    or len(definitions) != expected_count
                    or actual_required != expected_required
                ):
                    result = cls._unscorable(
                        "ozon_schema_stale",
                        status="schema_stale",
                    )
                    stale += 1
                    definitions = None
                if definitions is None:
                    pass
                else:
                    schema = MarketplaceQualitySchemaContext(
                        schema_hash=schema_hash,
                        required_attribute_ids=frozenset(
                            item.external_attribute_id
                            for item in definitions
                            if item.is_required
                        ),
                        filterable_attribute_ids=frozenset(
                            item.external_attribute_id
                            for item in definitions
                            if item.is_filterable
                        ),
                    )
                    result = evaluate_ozon_quality(
                        quality_input,
                        schema,
                        metrics=metrics_by_listing.get(listing.id),
                    )
                    scored += 1

            assessment = existing.get(listing.id)
            if assessment is None:
                assessment = MarketplaceQualityAssessment(
                    seller_id=listing.seller_id,
                    marketplace_id=listing.marketplace_id,
                    account_id=listing.account_id,
                    listing_id=listing.id,
                    listing_fingerprint=listing.sync_fingerprint,
                )
                db.session.add(assessment)
            assessment.analytics_sync_id = analytics_sync.id if analytics_sync else None
            assessment.status = result["status"]
            assessment.severity = result["severity"]
            assessment.score = result["score"]
            assessment.impact = result["impact"]
            assessment.schema_hash = schema_hash
            assessment.listing_fingerprint = listing.sync_fingerprint
            assessment.definition_version = QUALITY_DEFINITION_VERSION
            assessment.breakdown_json = cls._stable_json(result["breakdown"])
            assessment.reasons_json = cls._stable_json(result["reasons"])
            assessment.metrics_json = cls._stable_json({
                "period_code": "30d",
                "comparison_scope": "ozon_account_only",
                "cross_marketplace_comparable": False,
                "values": result["metrics"],
            })
            assessment.evaluated_at = current_time
        db.session.commit()
        processed = len(listings)
        return {
            "processed": processed,
            "scored": scored,
            "schema_stale": stale,
            "unscorable": unscorable,
            "total": total,
            "next_offset": offset + processed if offset + processed < total else None,
            "analytics_sync_id": analytics_sync.id if analytics_sync else None,
        }

    @classmethod
    def _summary(cls, *, seller_id: int, account_id: int) -> Dict[str, Any]:
        account = cls._account(seller_id=seller_id, account_id=account_id)
        total_listings = MarketplaceListing.query.filter(
            MarketplaceListing.seller_id == seller_id,
            MarketplaceListing.marketplace_id == account.marketplace_id,
            MarketplaceListing.account_id == account_id,
            MarketplaceListing.is_available.is_(True),
            MarketplaceListing.is_archived.is_(False),
        ).count()
        assessments = MarketplaceQualityAssessment.query.filter(
            MarketplaceQualityAssessment.seller_id == seller_id,
            MarketplaceQualityAssessment.marketplace_id == account.marketplace_id,
            MarketplaceQualityAssessment.account_id == account_id,
        ).all()
        distribution = {key: 0 for key in cls.ALLOWED_SEVERITIES}
        reason_counts = {key: 0 for key in REASON_DEFINITIONS}
        score_total = score_count = 0
        attention = 0
        for assessment in assessments:
            distribution[assessment.severity] += 1
            if assessment.score is not None:
                score_total += assessment.score
                score_count += 1
            reasons = cls._json_list(assessment.reasons_json)
            actionable = False
            for reason in reasons:
                if not isinstance(reason, dict):
                    continue
                code = reason.get("code")
                if code in reason_counts:
                    reason_counts[code] += 1
                if reason.get("severity") in {"critical", "warning"}:
                    actionable = True
            attention += int(actionable)
        return {
            "avg_quality": round(score_total / score_count, 1) if score_count else None,
            "total": total_listings,
            "assessed": len(assessments),
            "need_attention": attention,
            "distribution": distribution,
            "reason_counts": reason_counts,
            "reason_labels": {
                code: values[0] for code, values in REASON_DEFINITIONS.items()
            },
            "entity_kind": "marketplace_listing",
            "marketplace_code": "ozon",
            "account_id": account_id,
        }

    @classmethod
    def list_assessments(
        cls,
        *,
        seller_id: int,
        account_id: int,
        page: int = 1,
        per_page: int = 50,
        severity: Optional[str] = None,
        reason: Optional[str] = None,
        sort_by: str = "impact",
        sort_dir: str = "desc",
        search: str = "",
    ) -> Dict[str, Any]:
        account = cls._account(seller_id=seller_id, account_id=account_id)
        page = cls._positive_integer(page, "page")
        per_page = cls._positive_integer(per_page, "per_page", maximum=100)
        if severity is not None and severity not in cls.ALLOWED_SEVERITIES:
            raise MarketplaceQualityValidationError("Неизвестная severity")
        if reason is not None and reason not in cls.ALLOWED_REASON_CODES:
            raise MarketplaceQualityValidationError("Неизвестный reason code")
        if sort_by not in {"impact", "score", "evaluated_at", "title"}:
            raise MarketplaceQualityValidationError("Неизвестная сортировка")
        if sort_dir not in {"asc", "desc"}:
            raise MarketplaceQualityValidationError("sort_dir должен быть asc или desc")
        if not isinstance(search, str) or len(search) > 200:
            raise MarketplaceQualityValidationError("Некорректный поиск")

        query = MarketplaceQualityAssessment.query.options(
            joinedload(MarketplaceQualityAssessment.listing),
            joinedload(MarketplaceQualityAssessment.account),
            joinedload(MarketplaceQualityAssessment.marketplace),
        ).join(
            MarketplaceListing,
            MarketplaceListing.id == MarketplaceQualityAssessment.listing_id,
        ).filter(
            MarketplaceQualityAssessment.seller_id == seller_id,
            MarketplaceQualityAssessment.marketplace_id == account.marketplace_id,
            MarketplaceQualityAssessment.account_id == account_id,
            MarketplaceListing.seller_id == seller_id,
            MarketplaceListing.account_id == account_id,
            MarketplaceListing.is_available.is_(True),
            MarketplaceListing.is_archived.is_(False),
        )
        if severity:
            query = query.filter(MarketplaceQualityAssessment.severity == severity)
        if reason:
            query = query.filter(
                MarketplaceQualityAssessment.reasons_json.like(
                    f'%"code":"{reason}"%'
                )
            )
        needle = search.strip()
        if needle:
            escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            query = query.filter(
                or_(
                    MarketplaceListing.title.ilike(f"%{escaped}%", escape="\\"),
                    MarketplaceListing.offer_id.ilike(f"%{escaped}%", escape="\\"),
                    MarketplaceListing.primary_sku.ilike(f"%{escaped}%", escape="\\"),
                )
            )
        sort_column = {
            "impact": MarketplaceQualityAssessment.impact,
            "score": MarketplaceQualityAssessment.score,
            "evaluated_at": MarketplaceQualityAssessment.evaluated_at,
            "title": MarketplaceListing.title,
        }[sort_by]
        order = sort_column.asc() if sort_dir == "asc" else sort_column.desc()
        pagination = query.order_by(
            order,
            MarketplaceQualityAssessment.id.asc(),
        ).paginate(page=page, per_page=per_page, error_out=False)
        return {
            "items": [item.to_public_dict() for item in pagination.items],
            "total": pagination.total,
            "page": pagination.page,
            "pages": pagination.pages,
            "summary": cls._summary(seller_id=seller_id, account_id=account_id),
        }

    @classmethod
    def get_assessment(
        cls,
        *,
        seller_id: int,
        account_id: int,
        listing_id: int,
        recompute: bool = True,
    ) -> MarketplaceQualityAssessment:
        listing_id = cls._positive_integer(listing_id, "listing_id")
        account = cls._account(seller_id=seller_id, account_id=account_id)
        listing = MarketplaceListing.query.filter(
            MarketplaceListing.id == listing_id,
            MarketplaceListing.seller_id == seller_id,
            MarketplaceListing.marketplace_id == account.marketplace_id,
            MarketplaceListing.account_id == account_id,
        ).first()
        if listing is None:
            raise MarketplaceQualityNotFound("Карточка Ozon не найдена")
        if recompute:
            cls.recompute_account(
                seller_id=seller_id,
                account_id=account_id,
                listing_ids=[listing_id],
                limit=1,
            )
        assessment = MarketplaceQualityAssessment.query.options(
            joinedload(MarketplaceQualityAssessment.listing),
            joinedload(MarketplaceQualityAssessment.account),
            joinedload(MarketplaceQualityAssessment.marketplace),
        ).filter(
            MarketplaceQualityAssessment.seller_id == seller_id,
            MarketplaceQualityAssessment.marketplace_id == account.marketplace_id,
            MarketplaceQualityAssessment.account_id == account_id,
            MarketplaceQualityAssessment.listing_id == listing_id,
        ).first()
        if assessment is None:
            raise MarketplaceQualityNotFound("Оценка карточки не найдена")
        return assessment
