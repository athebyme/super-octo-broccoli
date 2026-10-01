"""Seller-scoped marketplace drafts and deterministic Ozon validation.

No provider request and no LLM call is allowed in this module.  Draft values
must resolve against the current SQL reference snapshot; publication will be a
separate durable P5 operation that revalidates this state.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import unicodedata
from urllib.parse import urlsplit

from flask import current_app
from sqlalchemy import func, or_, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.orm.exc import StaleDataError

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceAttributeDefinition,
    MarketplaceAttributeValue,
    MarketplaceCategoryMapping,
    MarketplaceListing,
    MarketplaceOperation,
    MarketplaceProductDraft,
    MarketplaceProductType,
    MarketplaceTaxonomyCategory,
    Seller,
    SellerMarketplaceAccount,
    db,
)
from services.marketplace_fact_pack import (
    MarketplaceFactPackBuilder,
    MarketplaceFactPackError,
)
from services.marketplace_category_review import (
    PAGE_SIZE as CATEGORY_IMPACT_PAGE_SIZE,
    build_impact as build_category_impact,
    issue_token as issue_category_review_token,
    valid_token as valid_category_review_token,
)
from services.marketplace_operation_locks import (
    release_marketplace_category_mapping_lock,
    try_marketplace_category_mapping_lock,
)
from services.ozon_brand_policy import first_forbidden_ozon_brand
from services.ozon_product_state import (
    OzonProductStateContract,
    OzonProductStateError,
)
from services.ozon_reference_service import OzonReferenceService
logger = logging.getLogger(__name__)


class MarketplaceDraftError(RuntimeError):
    status_code = 400
    code = "marketplace_draft_error"


class MarketplaceDraftValidationError(MarketplaceDraftError):
    status_code = 400
    code = "invalid_marketplace_draft"


class MarketplaceDraftNotFound(MarketplaceDraftError):
    status_code = 404
    code = "marketplace_draft_not_found"


class MarketplaceDraftConflict(MarketplaceDraftError):
    status_code = 409
    code = "marketplace_draft_conflict"


class MarketplaceCategoryImpactTooLarge(MarketplaceDraftConflict):
    code = "category_impact_too_large"


class MarketplaceDraftService:
    MAX_JSON_BYTES = 256 * 1024
    MAX_ATTRIBUTES = 5_000
    MAX_ATTRIBUTE_REMOVALS = 5_000
    MAX_COMPLEX_GROUPS = 500
    MAX_ATTRIBUTE_VALUES = 100
    MAX_IMAGES = 30
    MAX_BARCODES = 100
    MAX_IMPORT_BARCODES = 1
    MAX_OZON_OFFER_ID_CHARS = 50
    MAX_OZON_API_PHYSICAL_INTEGER = 2_147_483_647
    OZON_DESCRIPTION_ATTRIBUTE_ID = "4191"
    OZON_TYPE_ATTRIBUTE_ID = "8229"
    OZON_RUSSIAN_SIZE_ATTRIBUTE_ID = "4295"
    OZON_MODEL_NAME_ATTRIBUTE_ID = "9048"
    OZON_GROUP_ATTRIBUTE_ID = "8292"
    OZON_ADULT_ATTRIBUTE_ID = "9070"
    OZON_HASHTAG_ATTRIBUTE_ID = "23171"
    OZON_ACCESSORY_GENDER_ATTRIBUTE_ID = "4539"
    OZON_CLOTHING_GENDER_ATTRIBUTE_ID = "9163"
    OZON_BRAND_ATTRIBUTE_IDS = frozenset({"31", "85"})
    MAX_VALIDATION_ITEMS = 250
    DIMENSION_UNITS = {"MILLIMETERS", "CENTIMETERS", "INCHES"}
    WEIGHT_UNITS = {"GRAMS", "KILOGRAMS", "POUNDS"}
    VAT_VALUES = {"0", "0.05", "0.07", "0.1", "0.10", "0.2", "0.20", "0.22"}
    CURRENCY_CODES = {"RUB"}
    LISTING_HARD_TTL = timedelta(hours=48)
    DATA_TYPES = {"string", "integer", "decimal", "boolean"}
    ACTIVE_PUBLICATION_STATUSES = {
        "queued",
        "submitting",
        "submitted",
        "polling",
        "uncertain",
    }
    OBSERVED_MAPPING_ALGORITHM = "ozon-exact-linked-category-consensus-v3"
    EXPLICIT_SOURCE_MAPPING_ALGORITHM = "ozon-explicit-source-taxonomy-v2"
    EXPLICIT_SOURCE_MAPPING_ALGORITHMS = frozenset({
        "ozon-explicit-source-taxonomy-v1",
        EXPLICIT_SOURCE_MAPPING_ALGORITHM,
    })
    EXPLICIT_EROTIC_CLOTHING_PATH = (
        "Одежда / Одежда и аксессуары эротические"
    )
    EXPLICIT_PERSONAL_HYGIENE_PATH = "Аптека / Личная гигиена"
    EXPLICIT_ADULT_BDSM_PATH = "Товары для взрослых / БДСМ"
    EXPLICIT_ADULT_COSMETICS_PATH = (
        "Товары для взрослых / Интимная косметика"
    )
    EXPLICIT_ADULT_SEX_TOYS_PATH = (
        "Товары для взрослых / Секс игрушки"
    )
    EXPLICIT_ADULT_SOUVENIRS_PATH = (
        "Товары для взрослых / Сувениры и игры эротические"
    )
    EXPLICIT_ADULT_CARE_PATH = (
        "Товары для взрослых / Уход и хранение секс игрушек"
    )
    OBSERVED_MAPPING_MIN_LISTINGS = 2
    OBSERVED_MAPPING_MAX_LISTINGS = 20_000
    OBSERVED_MAPPING_MAX_PRODUCTS = 50_000
    OBSERVED_MAPPING_EXACT_LINK_SOURCES = frozenset({
        "exact_offer_identity",
        "exact_source_identity",
    })
    OBSERVED_MODEL_RECIPE = "source_vendor_code_exact_v1"
    EXPLICIT_SOURCE_MODEL_RECIPE = "isolated_source_product_model_v1"
    # Reviewed identity aliases only.  The result is still accepted solely
    # through one fresh exact type-scoped Ozon dictionary row; this table does
    # not authorize fuzzy matching or a free-form brand replacement.
    OBSERVED_BRAND_CANONICAL_ALIASES = {
        "bioritm": "Bioritmlab",
        'лаборатория "биоритм"': "Bioritmlab",
        "system jo": "System JO, США",
        "soft line": "SoftLine",
        "le shali": "LeShaLi",
        "mens max": "Men's Max",
        "nsnovelties": "NS Novelties",
        "svakom design usa limited": "Svakom Design",
    }

    @staticmethod
    def _positive_integer(value: Any, field_name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise MarketplaceDraftValidationError(
                f"{field_name} должен быть положительным целым числом"
            )
        return value

    @staticmethod
    def _strict_boolean(value: Any, field_name: str) -> bool:
        if not isinstance(value, bool):
            raise MarketplaceDraftValidationError(
                f"{field_name} должен быть boolean"
            )
        return value

    @classmethod
    def _assert_no_active_publication(
        cls,
        draft: MarketplaceProductDraft,
    ) -> None:
        active = MarketplaceOperation.query.filter(
            MarketplaceOperation.seller_id == draft.seller_id,
            MarketplaceOperation.marketplace_id == draft.marketplace_id,
            MarketplaceOperation.account_id == draft.account_id,
            MarketplaceOperation.draft_id == draft.id,
            MarketplaceOperation.status.in_(cls.ACTIVE_PUBLICATION_STATUSES),
        ).first()
        if active is not None:
            raise MarketplaceDraftConflict(
                "Черновик нельзя менять, пока публикация не завершена"
            )

    @staticmethod
    def _text(
        value: Any,
        field_name: str,
        *,
        maximum: int,
        required: bool = True,
        multiline: bool = False,
    ) -> str:
        if not isinstance(value, str):
            raise MarketplaceDraftValidationError(
                f"{field_name} должен быть строкой"
            )
        normalized = value.strip()
        if required and not normalized:
            raise MarketplaceDraftValidationError(f"{field_name} обязателен")
        if len(normalized) > maximum:
            raise MarketplaceDraftValidationError(
                f"{field_name} длиннее {maximum} символов"
            )
        allowed_controls = "\n\t" if multiline else ""
        if any(
            ord(character) < 32 and character not in allowed_controls
            for character in normalized
        ) or any(ord(character) == 127 for character in normalized):
            raise MarketplaceDraftValidationError(
                f"{field_name} содержит управляющие символы"
            )
        return normalized

    @classmethod
    def _optional_text(
        cls,
        value: Any,
        field_name: str,
        *,
        maximum: int,
        multiline: bool = False,
    ) -> str:
        if value in (None, ""):
            return ""
        return cls._text(
            value,
            field_name,
            maximum=maximum,
            required=False,
            multiline=multiline,
        )

    @staticmethod
    def _normalized_text(value: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())

    @classmethod
    def _reviewed_brand_candidate(cls, value: Any) -> Any:
        if not isinstance(value, str) or not value.strip():
            return value
        return cls.OBSERVED_BRAND_CANONICAL_ALIASES.get(
            cls._normalized_text(value),
            value,
        )

    @classmethod
    def _external_id(cls, value: Any, field_name: str) -> str:
        value = cls._text(value, field_name, maximum=100)
        if not value.isascii() or not value.isdigit() or value.startswith("0"):
            raise MarketplaceDraftValidationError(
                f"{field_name} должен быть canonical positive string ID"
            )
        return value

    @staticmethod
    def _decimal(value: Any, field_name: str, *, positive: bool = False) -> str:
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise MarketplaceDraftValidationError(
                f"{field_name} должен быть числом"
            )
        raw = str(value).strip()
        if not raw or not re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?", raw):
            raise MarketplaceDraftValidationError(
                f"{field_name} должен быть canonical decimal"
            )
        try:
            parsed = Decimal(raw)
        except InvalidOperation:
            raise MarketplaceDraftValidationError(
                f"{field_name} должен быть числом"
            ) from None
        if not parsed.is_finite() or (positive and parsed <= 0):
            qualifier = "положительным " if positive else ""
            raise MarketplaceDraftValidationError(
                f"{field_name} должен быть {qualifier}числом"
            )
        rendered = format(parsed, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return rendered or "0"

    @classmethod
    def _canonical_json(cls, value: Any, expected_type: type) -> str:
        if not isinstance(value, expected_type):
            raise MarketplaceDraftValidationError(
                f"Ожидался JSON {expected_type.__name__}"
            )
        try:
            rendered = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            raise MarketplaceDraftValidationError(
                "JSON содержит неподдерживаемое значение"
            ) from None
        if len(rendered.encode("utf-8")) > cls.MAX_JSON_BYTES:
            raise MarketplaceDraftValidationError(
                "JSON черновика превышает лимит размера"
            )
        return rendered

    @staticmethod
    def _stored_json(raw_value: Optional[str], expected_type: type) -> Any:
        try:
            value = json.loads(raw_value or "")
        except (TypeError, ValueError):
            return expected_type()
        return value if isinstance(value, expected_type) else expected_type()

    @classmethod
    def _owned_account(
        cls,
        *,
        seller_id: int,
        account_id: int,
    ) -> SellerMarketplaceAccount:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        account_id = cls._positive_integer(account_id, "account_id")
        account = SellerMarketplaceAccount.query.join(Marketplace).filter(
            SellerMarketplaceAccount.id == account_id,
            SellerMarketplaceAccount.seller_id == seller_id,
            Marketplace.code == "ozon",
            Marketplace.is_active.is_(True),
        ).first()
        if account is None:
            raise MarketplaceDraftNotFound("Кабинет Ozon не найден")
        return account

    @classmethod
    def _owned_imported_product(
        cls,
        *,
        seller_id: int,
        imported_product_id: int,
    ) -> ImportedProduct:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        imported_product_id = cls._positive_integer(
            imported_product_id,
            "imported_product_id",
        )
        product = ImportedProduct.query.options(
            joinedload(ImportedProduct.supplier_product),
            joinedload(ImportedProduct.product),
        ).filter_by(
            id=imported_product_id,
            seller_id=seller_id,
        ).first()
        if product is None:
            raise MarketplaceDraftNotFound("Импортированный товар не найден")
        return product

    @classmethod
    def get_draft(
        cls,
        *,
        seller_id: int,
        draft_id: int,
    ) -> MarketplaceProductDraft:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        draft_id = cls._positive_integer(draft_id, "draft_id")
        draft = MarketplaceProductDraft.query.options(
            joinedload(MarketplaceProductDraft.marketplace),
            joinedload(MarketplaceProductDraft.account),
            joinedload(MarketplaceProductDraft.imported_product).joinedload(
                ImportedProduct.product
            ),
            joinedload(MarketplaceProductDraft.imported_product).joinedload(
                ImportedProduct.supplier_product
            ),
            joinedload(MarketplaceProductDraft.product_type).joinedload(
                MarketplaceProductType.category
            ),
            joinedload(MarketplaceProductDraft.category_mapping),
        ).filter_by(
            id=draft_id,
            seller_id=seller_id,
        ).first()
        if draft is None:
            raise MarketplaceDraftNotFound("Черновик не найден")
        if (
            not draft.account
            or draft.account.seller_id != seller_id
            or draft.account.marketplace_id != draft.marketplace_id
            or not draft.marketplace
            or draft.marketplace.code != "ozon"
            or not draft.imported_product
            or draft.imported_product.seller_id != seller_id
        ):
            raise MarketplaceDraftNotFound("Черновик не найден")
        return draft

    @classmethod
    def _product_type(
        cls,
        *,
        marketplace_id: int,
        product_type_id: int,
    ) -> MarketplaceProductType:
        product_type_id = cls._positive_integer(product_type_id, "product_type_id")
        product_type = MarketplaceProductType.query.options(
            joinedload(MarketplaceProductType.category),
            joinedload(MarketplaceProductType.marketplace),
        ).filter_by(
            id=product_type_id,
            marketplace_id=marketplace_id,
            is_available=True,
            is_seller_selectable=True,
        ).first()
        if (
            product_type is None
            or product_type.marketplace.code != "ozon"
            or not product_type.category
            or not product_type.category.is_available
        ):
            raise MarketplaceDraftValidationError(
                "Доступный для продавца тип товара Ozon не найден"
            )
        return product_type

    @staticmethod
    def _scope_key(product: ImportedProduct) -> str:
        if product.supplier_id:
            return f"supplier:{product.supplier_id}"
        source_type = MarketplaceDraftService._normalized_text(
            product.source_type or "unknown"
        )
        return f"source:{source_type}"[:220]

    @classmethod
    def _source_category(cls, product: ImportedProduct) -> Tuple[str, str]:
        category = cls._optional_text(
            product.category,
            "source_category",
            maximum=500,
        )
        if not category:
            try:
                original = json.loads(product.original_data or "{}")
            except (TypeError, ValueError):
                original = {}
            if isinstance(original, dict):
                category = cls._optional_text(
                    original.get("category"),
                    "source_category",
                    maximum=500,
                )
        return category, cls._normalized_text(category) if category else ""

    @classmethod
    def _mapping_identities(cls, product: ImportedProduct) -> list:
        """Return exact category identities in deterministic priority order.

        A confirmed WB subject is stronger and more reusable than a supplier
        category label.  The legacy supplier/source identity remains as a
        fallback so existing mappings keep working after this rollout.
        """
        identities = []
        wb_product = product.product
        remote_wb_subject_id = (
            wb_product.subject_id
            if wb_product is not None
            and isinstance(wb_product.subject_id, int)
            and not isinstance(wb_product.subject_id, bool)
            and wb_product.subject_id > 0
            else None
        )
        imported_wb_subject_id = (
            product.wb_subject_id
            if isinstance(product.wb_subject_id, int)
            and not isinstance(product.wb_subject_id, bool)
            and product.wb_subject_id > 0
            else None
        )
        has_confirmed_wb_projection = bool(
            wb_product is not None
            or product.product_id is not None
            or product.wb_nm_id is not None
            or product.import_status == "imported"
        )
        wb_subject_id = remote_wb_subject_id or (
            imported_wb_subject_id
            if has_confirmed_wb_projection else None
        )
        if wb_subject_id is not None:
            wb_label = cls._optional_text(
                product.mapped_wb_category,
                "mapped_wb_category",
                maximum=400,
            )
            if not wb_label and wb_product is not None:
                wb_label = cls._optional_text(
                    wb_product.object_name,
                    "wb_object_name",
                    maximum=400,
                )
            identities.append({
                "scope_key": "wb_subject",
                "supplier_id": None,
                "source_type": "wb",
                "source_category": (
                    f"{wb_label} · WB subject {wb_subject_id}"
                    if wb_label else f"WB subject {wb_subject_id}"
                ),
                "source_category_normalized": f"wb_subject:{wb_subject_id}",
                "evidence": {
                    "wb_subject_id": wb_subject_id,
                    "wb_subject_source": (
                        "product_projection"
                        if remote_wb_subject_id is not None
                        else "confirmed_imported_projection"
                    ),
                },
            })

        category, normalized = cls._source_category(product)
        if category and normalized:
            identities.append({
                "scope_key": cls._scope_key(product),
                "supplier_id": product.supplier_id,
                "source_type": cls._optional_text(
                    product.source_type,
                    "source_type",
                    maximum=80,
                ) or "unknown",
                "source_category": category,
                "source_category_normalized": normalized,
                "evidence": {},
            })
        return identities

    @classmethod
    def _explicit_source_taxonomy_rule(
        cls,
        source_category: Any,
    ) -> Optional[dict]:
        """Resolve only literal, reviewed source leaves to official types.

        This is intentionally an allowlist rather than a lexical classifier.
        Broad ancestors and mixed leaves may contain several Ozon types, so
        only exact observed categories reviewed below may select a type.
        """
        if not isinstance(source_category, str):
            return None
        source = cls._normalized_text(source_category).replace("ё", "е")
        parts = [part.strip() for part in source.split(">")]
        if not all(parts):
            return None

        def target(
            rule_id: str,
            product_type_name: str,
            product_type_path: str,
        ) -> dict:
            return {
                "rule_id": rule_id,
                "product_type_name": product_type_name,
                "product_type_path": product_type_path,
            }

        if parts == ["гели, смазки и лубриканты"]:
            return target(
                "explicit_lubricant_standalone_leaf_v1",
                "Лубрикант",
                cls.EXPLICIT_PERSONAL_HYGIENE_PATH,
            )
        if len(parts) != 2:
            return None
        root, leaf = parts

        if root == "бдсм товары и фетиш" and leaf in {
            "одежда и белье для женщин",
            "одежда и белье для мужчин",
        }:
            return target(
                "explicit_bdsm_clothing_leaf_v1",
                "БДСМ одежда",
                cls.EXPLICIT_EROTIC_CLOTHING_PATH,
            )
        if root == "аксессуары, украшения для тела":
            if leaf in {"стикини, пестис", "стикини, пэстис"}:
                return target(
                    "explicit_pasties_leaf_v1",
                    "Пэстисы",
                    cls.EXPLICIT_EROTIC_CLOTHING_PATH,
                )
            if leaf in {"портупеи, стрепы", "портупеи, стрэпы"}:
                return target(
                    "explicit_erotic_harness_leaf_v1",
                    "Портупея эротическая",
                    cls.EXPLICIT_EROTIC_CLOTHING_PATH,
                )

        if root == "эротическое белье для женщин":
            if leaf in {"игровые костюмы", "ролевые костюмы"}:
                return target(
                    "explicit_roleplay_costume_leaf_v1",
                    "Костюм для ролевых игр",
                    cls.EXPLICIT_EROTIC_CLOTHING_PATH,
                )
            if leaf == "платья, мини-платья":
                return target(
                    "explicit_gogo_dress_leaf_v1",
                    "Платье гоу-гоу",
                    cls.EXPLICIT_EROTIC_CLOTHING_PATH,
                )
            if leaf in {
                "белье большого размера",
                "боди, ками, корсаж",
                "длинные халатики, сорочки",
                "колготки",
                "комбинезоны",
                "комплекты",
                "корсеты",
                "лифы, топы, бюстье, стрепы",
                "пеньюары, сорочки, пижамы",
                "пояса для чулок",
                "свадебный образ",
                "трусики, стринги, шортики",
                "халатики",
                "чулки, гольфины, леггинсы",
                "юбки, брюки, шорты",
            }:
                return target(
                    "explicit_womens_erotic_lingerie_leaf_v1",
                    "Эротическое белье",
                    cls.EXPLICIT_EROTIC_CLOTHING_PATH,
                )
        if root == "эротическое белье для мужчин" and leaf in {
            "боди, комбинезоны",
            "комплекты",
            "трусы, стринги, шорты",
        }:
            return target(
                "explicit_mens_erotic_lingerie_leaf_v1",
                "Эротическое белье",
                cls.EXPLICIT_EROTIC_CLOTHING_PATH,
            )

        exact_rules = {
            ("смазки, косметика", "вагинальные смазки"): (
                "explicit_vaginal_lubricant_leaf_v1",
                "Лубрикант",
                cls.EXPLICIT_PERSONAL_HYGIENE_PATH,
            ),
            ("смазки, косметика", "анальные смазки"): (
                "explicit_anal_lubricant_leaf_v1",
                "Лубрикант",
                cls.EXPLICIT_PERSONAL_HYGIENE_PATH,
            ),
            ("смазки, косметика", "оральные смазки"): (
                "explicit_oral_lubricant_leaf_v1",
                "Лубрикант",
                cls.EXPLICIT_PERSONAL_HYGIENE_PATH,
            ),
            ("смазки, косметика", "возбуждающие смазки"): (
                "explicit_arousal_lubricant_leaf_v1",
                "Лубрикант",
                cls.EXPLICIT_PERSONAL_HYGIENE_PATH,
            ),
            ("смазки, косметика", "пролонгирующие смазки"): (
                "explicit_prolonging_lubricant_leaf_v1",
                "Лубрикант",
                cls.EXPLICIT_PERSONAL_HYGIENE_PATH,
            ),
            ("анальные стимуляторы и пробки", "анальные пробки, втулки"): (
                "explicit_anal_plug_leaf_v1",
                "Анальная пробка",
                cls.EXPLICIT_ADULT_SEX_TOYS_PATH,
            ),
            ("анальные стимуляторы и пробки", "анальные стимуляторы"): (
                "explicit_anal_stimulator_leaf_v1",
                "Анальный стимулятор",
                cls.EXPLICIT_ADULT_SEX_TOYS_PATH,
            ),
            ("анальные стимуляторы и пробки", "стимуляторы простаты"): (
                "explicit_prostate_stimulator_leaf_v1",
                "Массажер простаты",
                cls.EXPLICIT_ADULT_SEX_TOYS_PATH,
            ),
            ("вибраторы и фаллоимитаторы", "хай-тек вибраторы"): (
                "explicit_high_tech_vibrator_leaf_v1",
                "Вибратор",
                cls.EXPLICIT_ADULT_SEX_TOYS_PATH,
            ),
            ("вибраторы и фаллоимитаторы", "с вибрацией"): (
                "explicit_vibrating_vibrator_leaf_v1",
                "Вибратор",
                cls.EXPLICIT_ADULT_SEX_TOYS_PATH,
            ),
            ("вибраторы и фаллоимитаторы", "для g точки"): (
                "explicit_g_spot_vibrator_leaf_v1",
                "Вибратор",
                cls.EXPLICIT_ADULT_SEX_TOYS_PATH,
            ),
            ("вибраторы и фаллоимитаторы", "без вибрации"): (
                "explicit_non_vibrating_dildo_leaf_v1",
                "Фаллоимитатор",
                cls.EXPLICIT_ADULT_SEX_TOYS_PATH,
            ),
            ("женские стимуляторы", "вибро-яйца"): (
                "explicit_vibro_egg_leaf_v1",
                "Виброяйцо",
                cls.EXPLICIT_ADULT_SEX_TOYS_PATH,
            ),
            ("насадки и кольца", "эрекционные"): (
                "explicit_erection_ring_leaf_v1",
                "Эрекционное кольцо",
                cls.EXPLICIT_ADULT_SEX_TOYS_PATH,
            ),
            ("насадки и кольца", "удлиняющие и расширяющие насадки"): (
                "explicit_erotic_extension_leaf_v1",
                "Насадки, удлинители эротические",
                cls.EXPLICIT_ADULT_SEX_TOYS_PATH,
            ),
            ("вакуумные помпы", "мужские помпы для тренировки эрекции"): (
                "explicit_male_pump_leaf_v1",
                "Помпа эротическая",
                cls.EXPLICIT_ADULT_SEX_TOYS_PATH,
            ),
            ("вакуумные помпы", "помпы женские"): (
                "explicit_female_pump_leaf_v1",
                "Помпа эротическая",
                cls.EXPLICIT_ADULT_SEX_TOYS_PATH,
            ),
            ("вакуумные помпы", "насадки на помпу"): (
                "explicit_pump_accessory_leaf_v1",
                "Аксессуары для помпы",
                cls.EXPLICIT_ADULT_SEX_TOYS_PATH,
            ),
            ("красивая грудь", "зажимы для сосков, стимуляторы"): (
                "explicit_nipple_clamp_leaf_v1",
                "Зажим, стимулятор для сосков",
                cls.EXPLICIT_ADULT_BDSM_PATH,
            ),
            ("бдсм товары и фетиш", "уретральные стимуляторы"): (
                "explicit_urethral_stimulator_leaf_v1",
                "Расширитель уретральный",
                cls.EXPLICIT_ADULT_BDSM_PATH,
            ),
            ("бдсм товары и фетиш", "шоковая терапия (электростимуляция)"): (
                "explicit_electrostimulation_leaf_v1",
                "Электростимуляторы",
                cls.EXPLICIT_ADULT_BDSM_PATH,
            ),
            ("бдсм товары и фетиш", "наборы"): (
                "explicit_bdsm_set_leaf_v1",
                "БДСМ набор",
                cls.EXPLICIT_ADULT_BDSM_PATH,
            ),
            ("аксессуары для игр", "эротические игры"): (
                "explicit_erotic_game_leaf_v1",
                "Игра эротическая",
                cls.EXPLICIT_ADULT_SOUVENIRS_PATH,
            ),
            ("эротические сувениры", "сувениры"): (
                "explicit_erotic_souvenir_leaf_v1",
                "Эротический сувенир",
                cls.EXPLICIT_ADULT_SOUVENIRS_PATH,
            ),
            ("сумочки для хранения", "мешочки"): (
                "explicit_toy_storage_leaf_v1",
                "Хранение секс игрушек",
                cls.EXPLICIT_ADULT_CARE_PATH,
            ),
            ("препараты и возбудители", "возбуждающие средства для женщин"): (
                "explicit_female_arousal_product_leaf_v1",
                "Возбуждающее средство",
                cls.EXPLICIT_ADULT_COSMETICS_PATH,
            ),
            ("препараты и возбудители", "возбуждающие средства для мужчин"): (
                "explicit_male_arousal_product_leaf_v1",
                "Возбуждающее средство",
                cls.EXPLICIT_ADULT_COSMETICS_PATH,
            ),
            ("препараты и возбудители", "пролонгаторы для мужчин"): (
                "explicit_prolonger_leaf_v1",
                "Пролонгатор",
                cls.EXPLICIT_ADULT_COSMETICS_PATH,
            ),
            ("секс-наборы", "секс-наборы"): (
                "explicit_erotic_set_leaf_v1",
                "Эротический набор",
                cls.EXPLICIT_ADULT_SEX_TOYS_PATH,
            ),
        }
        for leaf_name in {
            "мастурбаторы и оростимуляторы",
            "вагины без вибрации",
            "вагины с вибрацией",
            "мастурбаторы fleshlight, в колбах",
            "автоматические мастурбаторы",
            "мастурбаторы tenga",
            "полуторсы, торсы",
        }:
            exact_rules[("мастурбаторы и вагины", leaf_name)] = (
                "explicit_masturbator_leaf_v1",
                "Мастурбатор",
                cls.EXPLICIT_ADULT_SEX_TOYS_PATH,
            )
        exact_rules[("страпоны и фаллопротезы", "страпоны")] = (
            "explicit_strapon_leaf_v1",
            "Страпон",
            cls.EXPLICIT_ADULT_SEX_TOYS_PATH,
        )
        for leaf_name in {
            "вагинальные со смещенным центром тяжести",
            "вагинальные не со смещенным центром тяжести",
            "вагинальные с вибрацией",
        }:
            exact_rules[("шарики", leaf_name)] = (
                "explicit_vaginal_balls_leaf_v1",
                "Вагинальные шарики",
                cls.EXPLICIT_ADULT_SEX_TOYS_PATH,
            )
        for leaf_name in {
            "обычные",
            "с шариками и усиками",
            "ароматизированные",
            "полиуретановые",
        }:
            exact_rules[("презервативы", leaf_name)] = (
                "explicit_condoms_leaf_v1",
                "Презервативы",
                cls.EXPLICIT_PERSONAL_HYGIENE_PATH,
            )
        for leaf_name in {
            "простого класса",
            "среднего класса",
            "премиум класса",
        }:
            exact_rules[("секс куклы", leaf_name)] = (
                "explicit_sex_doll_leaf_v1",
                "Кукла для секса",
                cls.EXPLICIT_ADULT_SEX_TOYS_PATH,
            )

        resolved = exact_rules.get((root, leaf))
        return target(*resolved) if resolved is not None else None

    @classmethod
    def _explicit_source_mapping_candidate(
        cls,
        product: ImportedProduct,
    ) -> Optional[tuple]:
        """Return exact source identity + rule from observed fact provenance."""
        try:
            fact_pack = MarketplaceFactPackBuilder.build(product)
        except MarketplaceFactPackError:
            return None
        facts = fact_pack.get("facts")
        provenance = fact_pack.get("provenance")
        identity_facts = facts.get("identity") if isinstance(facts, dict) else None
        source_category = (
            identity_facts.get("source_category")
            if isinstance(identity_facts, dict)
            else None
        )
        source_provenance = (
            provenance.get("identity.source_category")
            if isinstance(provenance, dict)
            else None
        )
        if (
            not isinstance(source_provenance, dict)
            or source_provenance.get("trust") != "observed"
        ):
            return None
        rule = cls._explicit_source_taxonomy_rule(source_category)
        if rule is None:
            return None
        normalized = cls._normalized_text(source_category)
        source_identity = next((
            identity
            for identity in cls._mapping_identities(product)
            if (
                identity.get("scope_key") != "wb_subject"
                and identity.get("source_category_normalized") == normalized
            )
        ), None)
        if source_identity is None:
            # A seller-current category that disagrees with the observed
            # source snapshot must not inherit this source-only recipe.
            return None
        return source_identity, rule

    @classmethod
    def _is_explicit_source_mapping(
        cls,
        mapping: Optional[MarketplaceCategoryMapping],
    ) -> bool:
        if mapping is None or mapping.mapping_source != "deterministic":
            return False
        evidence = cls._stored_json(mapping.evidence_json, dict)
        return evidence.get("algorithm") in cls.EXPLICIT_SOURCE_MAPPING_ALGORITHMS

    @classmethod
    def _explicit_source_product_type(
        cls,
        *,
        marketplace_id: int,
        product_type_name: str,
        product_type_path: str,
    ) -> Optional[MarketplaceProductType]:
        rows = MarketplaceProductType.query.options(
            joinedload(MarketplaceProductType.category),
        ).join(
            MarketplaceTaxonomyCategory,
            MarketplaceProductType.category_id
            == MarketplaceTaxonomyCategory.id,
        ).filter(
            MarketplaceProductType.marketplace_id == marketplace_id,
            MarketplaceProductType.name == product_type_name,
            MarketplaceProductType.is_available.is_(True),
            MarketplaceProductType.is_seller_selectable.is_(True),
            MarketplaceTaxonomyCategory.full_path
            == product_type_path,
            MarketplaceTaxonomyCategory.is_available.is_(True),
        ).limit(2).all()
        if len(rows) != 1:
            return None
        product_type = rows[0]
        if not OzonReferenceService.reference_is_fresh(product_type):
            return None
        return product_type

    @classmethod
    def _ensure_explicit_source_taxonomy_mapping(
        cls,
        *,
        seller_id: int,
        marketplace_id: int,
        product: ImportedProduct,
    ) -> Optional[MarketplaceCategoryMapping]:
        """Persist one seller-scoped exact source-taxonomy mapping.

        Manual/AI/rejected/corrected decisions and the stronger confirmed WB
        subject identity remain authoritative.  Only an unreviewed
        deterministic source-category row may be created or replaced.
        """
        candidate = cls._explicit_source_mapping_candidate(product)
        if candidate is None:
            return None
        identity, rule = candidate

        active = cls._active_mapping(
            seller_id=seller_id,
            marketplace_id=marketplace_id,
            product=product,
        )
        if active is not None and active.scope_key == "wb_subject":
            return active

        def mapping_for_identity() -> Optional[MarketplaceCategoryMapping]:
            return MarketplaceCategoryMapping.query.options(
                joinedload(MarketplaceCategoryMapping.product_type).joinedload(
                    MarketplaceProductType.category
                )
            ).filter_by(
                seller_id=seller_id,
                marketplace_id=marketplace_id,
                scope_key=identity["scope_key"],
                source_category_normalized=(
                    identity["source_category_normalized"]
                ),
            ).first()

        existing = mapping_for_identity()
        if existing is not None and (
            existing.mapping_source != "deterministic"
            or existing.corrected_by_user_id is not None
            or existing.mapping_status == "rejected"
        ):
            return existing if existing.mapping_status == "active" else None
        if (
            existing is not None
            and existing.mapping_status == "active"
            and cls._is_explicit_source_mapping(existing)
            and existing.product_type is not None
            and existing.product_type.name == rule["product_type_name"]
            and existing.product_type.category is not None
            and existing.product_type.category.full_path
            == rule["product_type_path"]
        ):
            return existing

        claim = try_marketplace_category_mapping_lock(seller_id)
        if claim is None:
            raise MarketplaceDraftConflict(
                "Сопоставление категорий Ozon обновляется; повторите подготовку"
            )
        try:
            db.session.expire_all()
            fresh_product = cls._owned_imported_product(
                seller_id=seller_id,
                imported_product_id=product.id,
            )
            fresh_candidate = cls._explicit_source_mapping_candidate(
                fresh_product
            )
            if fresh_candidate is None:
                return None
            identity, rule = fresh_candidate
            existing = mapping_for_identity()
            if existing is not None and (
                existing.mapping_source != "deterministic"
                or existing.corrected_by_user_id is not None
                or existing.mapping_status == "rejected"
            ):
                return existing if existing.mapping_status == "active" else None

            product_type = cls._explicit_source_product_type(
                marketplace_id=marketplace_id,
                product_type_name=rule["product_type_name"],
                product_type_path=rule["product_type_path"],
            )
            if product_type is None:
                if existing is not None:
                    # The literal rule already disproves the old automatic
                    # target, but a missing/stale official target cannot be
                    # selected yet.  Keep the category untyped until the
                    # reference scheduler restores a fresh exact schema.
                    existing.mapping_status = "stale"
                    existing.evidence_json = json.dumps(
                        {
                            "algorithm": (
                                cls.EXPLICIT_SOURCE_MAPPING_ALGORITHM
                            ),
                            "reason": "official_target_unavailable_or_stale",
                            "rule_id": rule["rule_id"],
                            "target_type_name": rule["product_type_name"],
                            "target_type_path": rule["product_type_path"],
                            "source_category_fingerprint": hashlib.sha256(
                                identity[
                                    "source_category_normalized"
                                ].encode("utf-8")
                            ).hexdigest(),
                            "state": "stale",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    existing.updated_at = datetime.utcnow()
                    db.session.commit()
                return None
            evidence_json = json.dumps(
                {
                    "algorithm": cls.EXPLICIT_SOURCE_MAPPING_ALGORITHM,
                    "attribute_recipes": [{
                        "attribute_id": cls.OZON_MODEL_NAME_ATTRIBUTE_ID,
                        "isolation": "per_imported_product",
                        "recipe": cls.EXPLICIT_SOURCE_MODEL_RECIPE,
                    }],
                    "rule_id": rule["rule_id"],
                    "source_category_fingerprint": hashlib.sha256(
                        identity["source_category_normalized"].encode("utf-8")
                    ).hexdigest(),
                    "state": "active",
                    "target_type_path": rule["product_type_path"],
                    "target_category_id": (
                        product_type.category.external_category_id
                    ),
                    "target_type_id": product_type.external_type_id,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if existing is None:
                existing = MarketplaceCategoryMapping(
                    seller_id=seller_id,
                    marketplace_id=marketplace_id,
                    supplier_id=identity["supplier_id"],
                    product_type_id=product_type.id,
                    scope_key=identity["scope_key"],
                    source_type=identity["source_type"],
                    source_category=identity["source_category"],
                    source_category_normalized=(
                        identity["source_category_normalized"]
                    ),
                    external_category_id=(
                        product_type.category.external_category_id
                    ),
                    external_type_id=product_type.external_type_id,
                    mapping_source="deterministic",
                    mapping_status="active",
                    confidence=0.995,
                    evidence_json=evidence_json,
                    corrected_by_user_id=None,
                )
                db.session.add(existing)
            else:
                existing.supplier_id = identity["supplier_id"]
                existing.product_type_id = product_type.id
                existing.source_type = identity["source_type"]
                existing.source_category = identity["source_category"]
                existing.external_category_id = (
                    product_type.category.external_category_id
                )
                existing.external_type_id = product_type.external_type_id
                existing.mapping_source = "deterministic"
                existing.mapping_status = "active"
                existing.confidence = 0.995
                existing.evidence_json = evidence_json
                existing.corrected_by_user_id = None
                existing.updated_at = datetime.utcnow()
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                winner = mapping_for_identity()
                if (
                    winner is not None
                    and winner.mapping_status == "active"
                    and cls._is_explicit_source_mapping(winner)
                ):
                    return winner
                raise MarketplaceDraftConflict(
                    "Category mapping изменился параллельно; повторите подготовку"
                ) from None
            return mapping_for_identity()
        finally:
            release_marketplace_category_mapping_lock(claim)

    @classmethod
    def _active_mapping(
        cls,
        *,
        seller_id: int,
        marketplace_id: int,
        product: ImportedProduct,
    ) -> Optional[MarketplaceCategoryMapping]:
        for identity in cls._mapping_identities(product):
            mapping = MarketplaceCategoryMapping.query.options(
                joinedload(MarketplaceCategoryMapping.product_type).joinedload(
                    MarketplaceProductType.category
                )
            ).filter_by(
                seller_id=seller_id,
                marketplace_id=marketplace_id,
                scope_key=identity["scope_key"],
                source_category_normalized=(
                    identity["source_category_normalized"]
                ),
                mapping_status="active",
            ).first()
            if mapping is None:
                continue
            if (
                mapping.product_type
                and mapping.product_type.is_seller_selectable
                and mapping.product_type.is_available
                and mapping.product_type.category
                and mapping.product_type.category.is_available
            ):
                return mapping
            # A stored stronger identity must fail closed when its target was
            # disabled/removed; silently falling through to a weaker category
            # label could route the product to a different Ozon type.
            return None
        return None

    @classmethod
    def _observed_model_recipe_observation(
        cls,
        listing: MarketplaceListing,
        *,
        now: datetime,
    ) -> Optional[tuple]:
        """Prove that Ozon model name equals one observed source vendor code.

        Only a current exact listing projection is eligible.  The returned
        tuple contains IDs and a one-way value fingerprint, never the model or
        vendor code itself.
        """
        if (
            listing.imported_product is None
            or listing.imported_product_id is None
            or listing.attributes_synced_at is None
            or listing.attributes_synced_at < now - cls.LISTING_HARD_TTL
            or not isinstance(listing.attributes_json, str)
            or len(listing.attributes_json.encode("utf-8"))
            > cls.MAX_JSON_BYTES
        ):
            return None
        try:
            attributes = json.loads(listing.attributes_json)
        except (TypeError, ValueError):
            return None
        if not isinstance(attributes, list) or len(attributes) > cls.MAX_ATTRIBUTES:
            return None
        model_items = [
            item
            for item in attributes
            if (
                isinstance(item, dict)
                and str(
                    item.get("id")
                    if item.get("id") is not None
                    else item.get("attribute_id")
                ) == cls.OZON_MODEL_NAME_ATTRIBUTE_ID
            )
        ]
        if len(model_items) != 1:
            return None
        item = model_items[0]
        if item.get("complex_id") not in (None, 0, "0"):
            return None
        values = item.get("values")
        if (
            not isinstance(values, list)
            or len(values) != 1
            or not isinstance(values[0], dict)
            or values[0].get("dictionary_value_id") not in (None, "")
        ):
            return None
        model_value = cls._optional_text(
            values[0].get("value"),
            "observed_model_name",
            maximum=1_000,
        )
        if not model_value:
            return None
        try:
            facts = MarketplaceFactPackBuilder.build(
                listing.imported_product
            ).get("facts", {})
        except MarketplaceFactPackError:
            return None
        identifiers = (
            facts.get("identifiers", {})
            if isinstance(facts, dict) else {}
        )
        vendor_code = cls._optional_text(
            identifiers.get("vendor_code")
            if isinstance(identifiers, dict) else None,
            "observed_vendor_code",
            maximum=200,
        )
        normalized_vendor = cls._normalized_text(vendor_code or "")
        if (
            not normalized_vendor
            or cls._normalized_text(model_value) != normalized_vendor
        ):
            return None
        return (
            listing.id,
            listing.imported_product_id,
            hashlib.sha256(
                normalized_vendor.encode("utf-8")
            ).hexdigest(),
        )

    @classmethod
    def _mapping_has_model_recipe(
        cls,
        mapping: Optional[MarketplaceCategoryMapping],
        *,
        product_type: MarketplaceProductType,
    ) -> bool:
        """Accept only the bounded recipe emitted by our own reconciler."""
        if (
            mapping is None
            or mapping.mapping_source != "deterministic"
            or mapping.mapping_status != "active"
            or mapping.corrected_by_user_id is not None
            or mapping.product_type_id != product_type.id
        ):
            return False
        evidence = cls._stored_json(mapping.evidence_json, dict)
        if (
            evidence.get("algorithm") != cls.OBSERVED_MAPPING_ALGORITHM
            or evidence.get("state") != "active"
        ):
            return False
        recipes = evidence.get("attribute_recipes")
        if not isinstance(recipes, list) or len(recipes) > 10:
            return False
        matches = [
            item
            for item in recipes
            if (
                isinstance(item, dict)
                and item.get("attribute_id")
                == cls.OZON_MODEL_NAME_ATTRIBUTE_ID
                and item.get("recipe") == cls.OBSERVED_MODEL_RECIPE
                and isinstance(item.get("observations"), int)
                and not isinstance(item.get("observations"), bool)
                and item["observations"]
                >= cls.OBSERVED_MAPPING_MIN_LISTINGS
                and isinstance(item.get("distinct_products"), int)
                and not isinstance(item.get("distinct_products"), bool)
                and item["distinct_products"]
                >= cls.OBSERVED_MAPPING_MIN_LISTINGS
                and isinstance(item.get("evidence_fingerprint"), str)
                and re.fullmatch(
                    r"[0-9a-f]{64}",
                    item["evidence_fingerprint"],
                )
            )
        ]
        return len(matches) == 1

    @classmethod
    def _mapping_has_explicit_source_model_recipe(
        cls,
        mapping: Optional[MarketplaceCategoryMapping],
        *,
        product_type: MarketplaceProductType,
    ) -> bool:
        """Allow an isolated model key only when v2 evidence owns the type."""
        if (
            mapping is None
            or mapping.mapping_source != "deterministic"
            or mapping.mapping_status != "active"
            or mapping.corrected_by_user_id is not None
            or mapping.product_type_id != product_type.id
        ):
            return False
        evidence = cls._stored_json(mapping.evidence_json, dict)
        if (
            evidence.get("algorithm") != cls.EXPLICIT_SOURCE_MAPPING_ALGORITHM
            or evidence.get("state") != "active"
        ):
            return False
        recipes = evidence.get("attribute_recipes")
        if not isinstance(recipes, list) or len(recipes) > 10:
            return False
        matches = [
            item
            for item in recipes
            if (
                isinstance(item, dict)
                and item.get("attribute_id")
                == cls.OZON_MODEL_NAME_ATTRIBUTE_ID
                and item.get("recipe")
                == cls.EXPLICIT_SOURCE_MODEL_RECIPE
                and item.get("isolation") == "per_imported_product"
            )
        ]
        return len(matches) == 1

    @classmethod
    def _isolated_source_model_name(cls, facts_document: dict) -> str:
        """Build a readable model value that cannot merge two local cards."""
        source = facts_document.get("source")
        facts = facts_document.get("facts")
        identifiers = facts.get("identifiers") if isinstance(facts, dict) else None
        if not isinstance(source, dict) or not isinstance(identifiers, dict):
            return ""
        imported_product_id = source.get("imported_product_id")
        supplier_id = source.get("supplier_id")
        if (
            not isinstance(imported_product_id, int)
            or isinstance(imported_product_id, bool)
            or imported_product_id <= 0
            or not isinstance(supplier_id, int)
            or isinstance(supplier_id, bool)
            or supplier_id <= 0
        ):
            return ""
        external_id = cls._optional_text(
            identifiers.get("external_id"),
            "source_external_id",
            maximum=200,
        )
        if not external_id:
            return ""
        readable = cls._optional_text(
            identifiers.get("vendor_code"),
            "source_vendor_code",
            maximum=200,
        ) or external_id
        suffix = f" · SH-{supplier_id}-{imported_product_id}"
        prefix = readable[: max(1, 200 - len(suffix))].rstrip()
        return f"{prefix}{suffix}"[:200]

    @classmethod
    def _automatic_mapping_conflict_reason(
        cls,
        *,
        identity: dict,
        product_type: MarketplaceProductType,
    ) -> Optional[str]:
        """Reject direct taxonomy contradictions without selecting a type.

        Exact linked listings prove what was historically published, not that
        the provider category was semantically correct.  This negative-only
        guard recognizes narrow, literal contradictions in the observed
        source category.  It never proposes an alternative and is used only
        for deterministic mappings; seller-reviewed mappings stay untouched.
        """
        source_category = (
            identity.get("source_category")
            if isinstance(identity, dict)
            else None
        )
        source = cls._normalized_text(
            source_category if isinstance(source_category, str) else ""
        )
        category = product_type.category
        target_category = cls._normalized_text(
            category.full_path if category is not None else ""
        )
        target_type = cls._normalized_text(product_type.name or "")
        if not source or not target_type or not target_category:
            return "explicit_source_category_conflict"

        # Wearable-size evidence is deliberately broader than category
        # identity: strap-on briefs, for example, can still be a Strap-on.
        # Only explicit apparel taxonomy is strong enough for this boundary.
        explicit_clothing_markers = (
            "одежда >",
            "одежда /",
            "одежда и белье",
            "одежда и бельё",
            "эротическое белье",
            "эротическое бельё",
            "игровые костюмы",
            "ролевые костюмы",
            "платья, мини-платья",
            "пеньюары, сорочки",
            "чулки, гольфины",
            "колготки",
            "комбинезоны",
            "боди, ками",
            "лифы, топы",
            "пояса для чулок",
            "пэстис",
            "пестис",
            "стикини",
        )
        if any(marker in source for marker in explicit_clothing_markers):
            if not (
                target_category == "одежда"
                or target_category.startswith("одежда /")
            ):
                return "explicit_source_category_conflict"

        required_target_tokens = (
            (("менструальн", "чаш"), ("менструальн", "чаш")),
            (("анальн", "крюк"), ("анальн", "крюк")),
            (("насадк", "помп"), ("помп",)),
            (("вакуум", "стимулятор"), ("вакуум", "стимулятор")),
        )
        for source_tokens, target_tokens in required_target_tokens:
            if all(token in source for token in source_tokens) and not all(
                token in target_type for token in target_tokens
            ):
                return "explicit_source_category_conflict"

        if "анальн" in source and "вагинальн" in target_type:
            return "explicit_source_category_conflict"
        if "вагинальн" in source and "анальн" in target_type:
            return "explicit_source_category_conflict"
        return None

    @classmethod
    def reconcile_observed_category_mappings(
        cls,
        *,
        seller_id: int,
        marketplace_id: int,
    ) -> dict:
        """Activate only unanimous mappings proven by exact linked listings.

        Category reuse is a generalization, so one example is never enough.
        For one exact WB-subject or supplier-category identity, at least two
        active Ozon listings must expose the same fresh official product type.
        A conflicting, missing or stale observation keeps the identity
        unmapped and invalidates a previously automatic mapping.  Manual
        seller decisions are never changed here.

        The scan is seller/marketplace scoped and hard bounded.  If the bound
        cannot prove that every current observation was seen, no mapping is
        changed.
        """
        seller_id = cls._positive_integer(seller_id, "seller_id")
        marketplace_id = cls._positive_integer(
            marketplace_id,
            "marketplace_id",
        )
        if Seller.query.filter_by(id=seller_id).first() is None:
            raise MarketplaceDraftNotFound("Seller не найден")
        marketplace = Marketplace.query.filter_by(
            id=marketplace_id,
            code="ozon",
            is_active=True,
        ).first()
        if marketplace is None:
            raise MarketplaceDraftValidationError(
                "Активный marketplace Ozon не найден"
            )

        claim = try_marketplace_category_mapping_lock(seller_id)
        if claim is None:
            return {
                "success": False,
                "code": "observed_category_mapping_busy",
                "created": 0,
                "refreshed": 0,
                "staled": 0,
                "protected": 0,
                "safe_groups": 0,
                "unsafe_groups": 0,
                "observed_listings": 0,
            }
        try:
            db.session.expire_all()
            rows = MarketplaceListing.query.options(
                selectinload(
                    MarketplaceListing.imported_product
                ).selectinload(ImportedProduct.product),
                selectinload(
                    MarketplaceListing.imported_product
                ).selectinload(ImportedProduct.supplier_product),
                joinedload(
                    MarketplaceListing.product_type
                ).joinedload(MarketplaceProductType.category),
            ).filter(
                MarketplaceListing.seller_id == seller_id,
                MarketplaceListing.marketplace_id == marketplace_id,
                MarketplaceListing.is_available.is_(True),
                MarketplaceListing.is_archived.is_(False),
                MarketplaceListing.link_status == "linked",
                MarketplaceListing.link_source.in_(
                    cls.OBSERVED_MAPPING_EXACT_LINK_SOURCES
                ),
                MarketplaceListing.imported_product_id.isnot(None),
            ).order_by(
                MarketplaceListing.id.asc(),
            ).limit(
                cls.OBSERVED_MAPPING_MAX_LISTINGS + 1
            ).all()
            if len(rows) > cls.OBSERVED_MAPPING_MAX_LISTINGS:
                return {
                    "success": False,
                    "code": "observed_category_mapping_scan_truncated",
                    "created": 0,
                    "refreshed": 0,
                    "staled": 0,
                    "protected": 0,
                    "safe_groups": 0,
                    "unsafe_groups": 0,
                    "observed_listings": len(rows),
                }

            # A supplier leaf may still contain multiple structured product
            # subtypes (notably broad clothing/lingerie buckets).  Legacy
            # ``wb_subject_id`` is not trusted to select an Ozon type, but
            # disagreement between those structured hints is valid
            # negative-only evidence: two matching Ozon examples must not
            # generalize across a demonstrably heterogeneous source bucket.
            product_rows = ImportedProduct.query.with_entities(
                ImportedProduct.id,
                ImportedProduct.supplier_id,
                ImportedProduct.source_type,
                ImportedProduct.category,
                ImportedProduct.wb_subject_id,
            ).filter(
                ImportedProduct.seller_id == seller_id,
            ).order_by(
                ImportedProduct.id.asc(),
            ).limit(
                cls.OBSERVED_MAPPING_MAX_PRODUCTS + 1
            ).all()
            if len(product_rows) > cls.OBSERVED_MAPPING_MAX_PRODUCTS:
                return {
                    "success": False,
                    "code": "observed_category_product_scan_truncated",
                    "created": 0,
                    "refreshed": 0,
                    "staled": 0,
                    "protected": 0,
                    "safe_groups": 0,
                    "unsafe_groups": 0,
                    "observed_listings": len(rows),
                }
            structured_subtypes = defaultdict(set)
            for product_row in product_rows:
                category = (
                    product_row.category.strip()
                    if isinstance(product_row.category, str)
                    else ""
                )
                subject_id = product_row.wb_subject_id
                if (
                    not category
                    or not isinstance(subject_id, int)
                    or isinstance(subject_id, bool)
                    or subject_id <= 0
                ):
                    continue
                scope_key = (
                    f"supplier:{product_row.supplier_id}"
                    if product_row.supplier_id
                    else "source:" + cls._normalized_text(
                        product_row.source_type or "unknown"
                    )
                )[:220]
                structured_subtypes[(
                    scope_key,
                    cls._normalized_text(category),
                )].add(subject_id)

            current_time = datetime.utcnow()
            evidence = defaultdict(lambda: {
                "identity": None,
                "types": Counter(),
                "product_types": {},
                "unknown": 0,
                "observations": [],
                "model_recipe_observations": [],
                "model_recipe_unknown": 0,
                "structured_subtypes": set(),
            })
            for listing in rows:
                product = listing.imported_product
                if product is None:
                    continue
                product_type = listing.product_type
                type_is_fresh = bool(
                    product_type is not None
                    and OzonReferenceService.reference_is_fresh(
                        product_type
                    )
                )
                model_recipe_observation = (
                    cls._observed_model_recipe_observation(
                        listing,
                        now=current_time,
                    )
                )
                for identity in cls._mapping_identities(product):
                    key = (
                        identity["scope_key"],
                        identity["source_category_normalized"],
                    )
                    item = evidence[key]
                    if item["identity"] is None:
                        item["identity"] = identity
                        if identity["scope_key"] != "wb_subject":
                            item["structured_subtypes"] = set(
                                structured_subtypes.get(key, set())
                            )
                    observed_type_id = (
                        product_type.id
                        if type_is_fresh and product_type is not None
                        else None
                    )
                    item["observations"].append((
                        listing.id,
                        listing.imported_product_id,
                        observed_type_id,
                    ))
                    if observed_type_id is None:
                        item["unknown"] += 1
                    else:
                        item["types"][observed_type_id] += 1
                        item["product_types"][
                            observed_type_id
                        ] = product_type
                    if model_recipe_observation is None:
                        item["model_recipe_unknown"] += 1
                    else:
                        item["model_recipe_observations"].append(
                            model_recipe_observation
                        )

            existing_rows = MarketplaceCategoryMapping.query.filter_by(
                seller_id=seller_id,
                marketplace_id=marketplace_id,
            ).all()
            existing = {
                (
                    mapping.scope_key,
                    mapping.source_category_normalized,
                ): mapping
                for mapping in existing_rows
            }
            created = 0
            refreshed = 0
            staled = 0
            protected = 0
            safe_groups = 0
            unsafe_groups = 0
            changed = False
            observed_keys = set(evidence)

            def encoded_evidence(
                *,
                item: dict,
                state: str,
                reason: Optional[str] = None,
            ) -> str:
                observations = sorted(item["observations"])
                fingerprint = hashlib.sha256(
                    json.dumps(
                        observations,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                document = {
                    "algorithm": cls.OBSERVED_MAPPING_ALGORITHM,
                    "state": state,
                    "observations": len(observations),
                    "distinct_products": len({
                        observation[1] for observation in observations
                    }),
                    "evidence_fingerprint": fingerprint,
                    "exact_link_sources": sorted(
                        cls.OBSERVED_MAPPING_EXACT_LINK_SOURCES
                    ),
                    "structured_subtype_count": len(
                        item["structured_subtypes"]
                    ),
                }
                if reason:
                    document["reason"] = reason
                recipe_observations = sorted(
                    item["model_recipe_observations"]
                )
                if (
                    state == "active"
                    and item["model_recipe_unknown"] == 0
                    and len(recipe_observations) == len(observations)
                    and len(recipe_observations)
                    >= cls.OBSERVED_MAPPING_MIN_LISTINGS
                    and len({
                        observation[1]
                        for observation in recipe_observations
                    })
                    >= cls.OBSERVED_MAPPING_MIN_LISTINGS
                ):
                    recipe_fingerprint = hashlib.sha256(
                        json.dumps(
                            recipe_observations,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    document["attribute_recipes"] = [{
                        "attribute_id": (
                            cls.OZON_MODEL_NAME_ATTRIBUTE_ID
                        ),
                        "recipe": cls.OBSERVED_MODEL_RECIPE,
                        "observations": len(recipe_observations),
                        "distinct_products": len({
                            observation[1]
                            for observation in recipe_observations
                        }),
                        "evidence_fingerprint": recipe_fingerprint,
                    }]
                identity_evidence = (
                    item["identity"].get("evidence")
                    if isinstance(item.get("identity"), dict)
                    else None
                )
                if isinstance(identity_evidence, dict):
                    document.update(identity_evidence)
                return json.dumps(
                    document,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )

            for key in sorted(evidence):
                item = evidence[key]
                mapping = existing.get(key)
                if cls._is_explicit_source_mapping(mapping):
                    # This row is owned by the reviewed literal taxonomy
                    # recipe, not by historical-listing consensus.  A later
                    # listing scan must neither rewrite nor stale it.
                    protected += 1
                    continue
                reason = None
                if len(item["structured_subtypes"]) > 1:
                    reason = "heterogeneous_source_category"
                elif item["unknown"]:
                    reason = "incomplete_or_stale_product_type_evidence"
                elif len(item["types"]) != 1:
                    reason = "conflicting_product_types"
                elif (
                    sum(item["types"].values())
                    < cls.OBSERVED_MAPPING_MIN_LISTINGS
                    or len({
                        observation[1]
                        for observation in item["observations"]
                    })
                    < cls.OBSERVED_MAPPING_MIN_LISTINGS
                ):
                    reason = "not_enough_exact_listings"
                else:
                    observed_product_type_id = next(iter(item["types"]))
                    reason = cls._automatic_mapping_conflict_reason(
                        identity=item["identity"],
                        product_type=item["product_types"][
                            observed_product_type_id
                        ],
                    )

                if reason is not None:
                    unsafe_groups += 1
                    if (
                        mapping is not None
                        and mapping.mapping_source == "deterministic"
                        and mapping.mapping_status == "active"
                        and mapping.corrected_by_user_id is None
                    ):
                        mapping.mapping_status = "stale"
                        mapping.evidence_json = encoded_evidence(
                            item=item,
                            state="stale",
                            reason=reason,
                        )
                        mapping.updated_at = datetime.utcnow()
                        staled += 1
                        changed = True
                    continue

                safe_groups += 1
                product_type_id = next(iter(item["types"]))
                product_type = item["product_types"][product_type_id]
                identity = item["identity"]
                next_evidence = encoded_evidence(
                    item=item,
                    state="active",
                )
                if mapping is not None and (
                    mapping.mapping_source != "deterministic"
                    or mapping.corrected_by_user_id is not None
                    or mapping.mapping_status == "rejected"
                ):
                    protected += 1
                    continue
                if mapping is None:
                    mapping = MarketplaceCategoryMapping(
                        seller_id=seller_id,
                        marketplace_id=marketplace_id,
                        supplier_id=identity["supplier_id"],
                        product_type_id=product_type.id,
                        scope_key=identity["scope_key"],
                        source_type=identity["source_type"],
                        source_category=identity["source_category"],
                        source_category_normalized=(
                            identity["source_category_normalized"]
                        ),
                        external_category_id=(
                            product_type.category.external_category_id
                        ),
                        external_type_id=product_type.external_type_id,
                        mapping_source="deterministic",
                        mapping_status="active",
                        confidence=0.99,
                        evidence_json=next_evidence,
                        corrected_by_user_id=None,
                    )
                    db.session.add(mapping)
                    existing[key] = mapping
                    created += 1
                    changed = True
                    continue

                next_values = {
                    "supplier_id": identity["supplier_id"],
                    "product_type_id": product_type.id,
                    "source_type": identity["source_type"],
                    "source_category": identity["source_category"],
                    "external_category_id": (
                        product_type.category.external_category_id
                    ),
                    "external_type_id": product_type.external_type_id,
                    "mapping_status": "active",
                    "confidence": 0.99,
                    "evidence_json": next_evidence,
                }
                if any(
                    getattr(mapping, field) != value
                    for field, value in next_values.items()
                ):
                    for field, value in next_values.items():
                        setattr(mapping, field, value)
                    mapping.updated_at = datetime.utcnow()
                    refreshed += 1
                    changed = True

            for key, mapping in existing.items():
                if (
                    key in observed_keys
                    or mapping.mapping_source != "deterministic"
                    or mapping.mapping_status != "active"
                    or mapping.corrected_by_user_id is not None
                    or cls._is_explicit_source_mapping(mapping)
                ):
                    continue
                mapping.mapping_status = "stale"
                mapping.evidence_json = json.dumps(
                    {
                        "algorithm": cls.OBSERVED_MAPPING_ALGORITHM,
                        "state": "stale",
                        "reason": "no_current_exact_listing_evidence",
                        "observations": 0,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                mapping.updated_at = datetime.utcnow()
                staled += 1
                changed = True

            if changed:
                try:
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                    raise MarketplaceDraftConflict(
                        "Category mapping изменился параллельно; повторите подготовку"
                    ) from None
            return {
                "success": True,
                "code": "observed_category_mapping_reconciled",
                "created": created,
                "refreshed": refreshed,
                "staled": staled,
                "protected": protected,
                "safe_groups": safe_groups,
                "unsafe_groups": unsafe_groups,
                "observed_listings": len(rows),
            }
        finally:
            release_marketplace_category_mapping_lock(claim)

    @classmethod
    def _upsert_mapping(
        cls,
        *,
        seller_id: int,
        marketplace_id: int,
        product: ImportedProduct,
        product_type: MarketplaceProductType,
        corrected_by_user_id: Optional[int],
    ) -> MarketplaceCategoryMapping:
        identities = cls._mapping_identities(product)
        if not identities:
            raise MarketplaceDraftValidationError(
                "Нельзя сохранить mapping без исходной категории"
            )
        identity = identities[0]
        if corrected_by_user_id is not None:
            corrected_by_user_id = cls._positive_integer(
                corrected_by_user_id,
                "corrected_by_user_id",
            )
            if Seller.query.filter_by(
                id=seller_id,
                user_id=corrected_by_user_id,
            ).first() is None:
                raise MarketplaceDraftValidationError(
                    "corrected_by_user_id не принадлежит seller"
                )
        mapping = MarketplaceCategoryMapping.query.filter_by(
            seller_id=seller_id,
            marketplace_id=marketplace_id,
            scope_key=identity["scope_key"],
            source_category_normalized=(
                identity["source_category_normalized"]
            ),
        ).first()
        evidence = {"confirmation": "seller"}
        evidence.update(identity["evidence"])
        evidence_json = json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if mapping is None:
            mapping = MarketplaceCategoryMapping(
                seller_id=seller_id,
                marketplace_id=marketplace_id,
                supplier_id=identity["supplier_id"],
                scope_key=identity["scope_key"],
                source_type=identity["source_type"],
                source_category=identity["source_category"],
                source_category_normalized=(
                    identity["source_category_normalized"]
                ),
                product_type_id=product_type.id,
                external_category_id=product_type.category.external_category_id,
                external_type_id=product_type.external_type_id,
                mapping_source="manual",
                mapping_status="active",
                confidence=1.0,
                evidence_json=evidence_json,
                corrected_by_user_id=corrected_by_user_id,
            )
            db.session.add(mapping)
        else:
            mapping.supplier_id = identity["supplier_id"]
            mapping.source_type = identity["source_type"]
            mapping.source_category = identity["source_category"]
            mapping.product_type_id = product_type.id
            mapping.external_category_id = product_type.category.external_category_id
            mapping.external_type_id = product_type.external_type_id
            mapping.mapping_source = "manual"
            mapping.mapping_status = "active"
            mapping.confidence = 1.0
            mapping.evidence_json = evidence_json
            mapping.corrected_by_user_id = corrected_by_user_id
            mapping.updated_at = datetime.utcnow()
        db.session.flush()
        return mapping

    @classmethod
    def search_product_types(
        cls,
        *,
        seller_id: int,
        query: Any = "",
        limit: int = 50,
    ) -> list:
        cls._positive_integer(seller_id, "seller_id")
        limit = cls._positive_integer(limit, "limit")
        if limit > 100:
            raise MarketplaceDraftValidationError("limit не может быть больше 100")
        search = cls._optional_text(query, "query", maximum=200)
        rows = MarketplaceProductType.query.join(Marketplace).join(
            MarketplaceTaxonomyCategory,
            MarketplaceProductType.category_id == MarketplaceTaxonomyCategory.id,
        ).filter(
            Marketplace.code == "ozon",
            Marketplace.is_active.is_(True),
            MarketplaceProductType.is_available.is_(True),
            MarketplaceProductType.is_seller_selectable.is_(True),
            MarketplaceTaxonomyCategory.is_available.is_(True),
        )
        if search:
            if db.engine.dialect.name == 'sqlite':
                connection = db.session.connection().connection.driver_connection
                connection.create_function(
                    'sh_ozon_type_casefold', 1,
                    lambda value: str(value or '').casefold(), deterministic=True,
                )
                lower = func.sh_ozon_type_casefold
            else:
                lower = func.lower
            rows = rows.filter(or_(
                lower(MarketplaceProductType.name).contains(search.casefold(), autoescape=True),
                lower(MarketplaceTaxonomyCategory.full_path).contains(search.casefold(), autoescape=True),
            ))
        product_types = rows.order_by(
            MarketplaceTaxonomyCategory.full_path.asc(),
            MarketplaceProductType.name.asc(),
            MarketplaceProductType.id.asc(),
        ).limit(limit).all()
        return [{
            "id": item.id,
            "name": item.name,
            "category_path": item.category.full_path,
            "external_category_id": item.category.external_category_id,
            "external_type_id": item.external_type_id,
            "schema_fresh": OzonReferenceService.reference_is_fresh(item),
            "schema_version": item.attributes_version,
        } for item in product_types]

    @classmethod
    def suggest_product_types(
        cls,
        *,
        seller_id: int,
        draft_id: int,
        limit: int = 3,
    ) -> list:
        """Детерминированные кандидаты типа Ozon для черновика без типа.

        Только предложение: лексический подбор по имени WB-предмета и
        категории поставщика среди seller-selectable/available типов дерева.
        Ничего не применяет — подтверждение остаётся отдельным явным
        действием продавца (существующая форма «Привязать»)."""
        draft = cls.get_draft(seller_id=seller_id, draft_id=draft_id)
        if draft.product_type_id:
            return []
        queries = []
        imported = draft.imported_product
        if imported is not None:
            if imported.mapped_wb_category:
                queries.append(('wb_subject', imported.mapped_wb_category))
            if imported.category:
                # Последний сегмент цепочки категорий поставщика
                tail = str(imported.category).split('/')[-1].strip()
                if tail:
                    queries.append(('supplier_category', tail))
        if not queries:
            return []

        def _norm(value: Any) -> str:
            return ' '.join(str(value or '').casefold().split())

        best: dict = {}
        for priority, (source, query) in enumerate(queries):
            query_norm = _norm(query)
            if not query_norm:
                continue
            try:
                found = cls.search_product_types(
                    seller_id=seller_id, query=query, limit=10,
                )
            except MarketplaceDraftError:
                continue
            for item in found:
                name_norm = _norm(item.get('name'))
                if name_norm == query_norm:
                    score = 100
                elif query_norm in name_norm or name_norm in query_norm:
                    score = 60
                else:
                    score = 30
                # Совпадение по WB-предмету приоритетнее категории поставщика
                score -= priority * 5
                existing = best.get(item['id'])
                if existing is None or existing['score'] < score:
                    best[item['id']] = {
                        **item, 'score': score, 'matched_on': query,
                        'matched_source': source,
                    }
        ranked = sorted(
            best.values(), key=lambda i: (-i['score'], i['name']),
        )
        return ranked[:max(1, min(int(limit), 10))]

    @classmethod
    def mapping_readiness(
        cls,
        *,
        seller_id: int,
        draft_id: int,
    ) -> dict:
        """Return a bounded explanation of WB/canonical -> Ozon readiness.

        This is a local read.  It deliberately reports exact reference state
        instead of guessing whether a value can be translated between the WB
        and Ozon schemas.  ``validate_draft`` remains the final publishability
        authority because it also checks content, media, physical values,
        commercial data and account health.
        """
        draft = cls.get_draft(seller_id=seller_id, draft_id=draft_id)
        product_type = draft.product_type
        definitions = []
        if product_type is not None:
            definitions = MarketplaceAttributeDefinition.query.filter_by(
                product_type_id=product_type.id,
                is_available=True,
                is_enabled=True,
            ).order_by(
                MarketplaceAttributeDefinition.sort_order.asc(),
                MarketplaceAttributeDefinition.id.asc(),
            ).all()

        supplied_ids = set()
        for item in cls._stored_json(draft.attributes_json, list):
            if isinstance(item, dict) and isinstance(item.get("attribute_id"), str):
                supplied_ids.add(item["attribute_id"])
        for group in cls._stored_json(draft.complex_attributes_json, list):
            if not isinstance(group, dict):
                continue
            for item in group.get("attributes", []):
                if isinstance(item, dict) and isinstance(item.get("attribute_id"), str):
                    supplied_ids.add(item["attribute_id"])
        content = cls._stored_json(draft.content_json, dict)
        if isinstance(content.get("description"), str) and content["description"].strip():
            supplied_ids.add(cls.OZON_DESCRIPTION_ATTRIBUTE_ID)

        required = [item for item in definitions if item.is_required]
        missing_required = [
            {
                "attribute_id": item.external_attribute_id,
                "name": item.name,
                "complex_id": item.attribute_complex_id or "0",
            }
            for item in required
            if item.external_attribute_id not in supplied_ids
        ]
        dictionary_definitions = [
            item for item in definitions if item.dictionary_id
        ]
        stale_dictionaries = [
            {
                "attribute_id": item.external_attribute_id,
                "name": item.name,
                "dictionary_id": item.dictionary_id,
            }
            for item in dictionary_definitions
            if not OzonReferenceService.dictionary_is_fresh(item)
        ]
        schema_fresh = bool(
            product_type
            and OzonReferenceService.reference_is_fresh(product_type)
        )

        product = draft.imported_product
        supplier_product = product.supplier_product if product else None
        if supplier_product and (
            supplier_product.ai_parsed_at is not None
            or bool(supplier_product.ai_parsed_data_json)
        ):
            ai_source = "supplier_product_cache"
        elif product and any((
            product.ai_analysis_at,
            product.ai_keywords,
            product.ai_attributes,
            product.ai_seo_title,
        )):
            ai_source = "imported_product_cache"
        else:
            ai_source = None

        try:
            source_facts_fresh = bool(
                product
                and MarketplaceFactPackBuilder.build(product)["fact_hash"]
                == draft.source_fact_hash
            )
        except MarketplaceFactPackError:
            source_facts_fresh = False
        try:
            wb_projection = MarketplaceFactPackBuilder.wb_projection_drift(
                product
            ) if product else {
                "linked": False,
                "in_sync": None,
                "differing_fields": [],
            }
        except MarketplaceFactPackError:
            wb_projection = {
                "linked": False,
                "in_sync": None,
                "differing_fields": [],
            }
        now = datetime.utcnow()
        account_ready = bool(
            draft.account
            and draft.account.is_active
            and draft.account.connection_status == "connected"
            and draft.account.has_credentials
            and (
                draft.account.credential_expires_at is None
                or draft.account.credential_expires_at > now
            )
        )

        validation = cls._stored_json(draft.validation_result_json, dict)
        if product_type is None:
            overall = "needs_category"
        elif not source_facts_fresh:
            overall = "source_stale"
        elif not schema_fresh or stale_dictionaries:
            overall = "references_stale"
        elif missing_required:
            overall = "needs_attributes"
        elif not account_ready:
            overall = "account_blocked"
        elif draft.status == "ready" and validation.get("publishable") is True:
            overall = "ready"
        elif draft.validation_status in {"never_validated", "stale"}:
            overall = "needs_validation"
        else:
            overall = "blocked"

        if draft.category_mapping_id is not None:
            category_status = "exact_mapping"
        elif draft.published_listing_id is not None and product_type is not None:
            category_status = "linked_listing"
        elif product_type is not None:
            category_status = "selected"
        else:
            category_status = "missing"

        wb_product = product.product if product else None
        wb_nm_id = None
        if wb_product is not None and wb_product.nm_id is not None:
            wb_nm_id = str(wb_product.nm_id)
        elif product is not None and product.wb_nm_id is not None:
            wb_nm_id = str(product.wb_nm_id)

        return {
            "version": 1,
            "overall": overall,
            "source": {
                "kind": "canonical_imported_product",
                "imported_product_id": draft.imported_product_id,
                "wb_projection_linked": bool(
                    product and (product.product_id is not None or wb_nm_id)
                ),
                "wb_product_id": product.product_id if product else None,
                "wb_nm_id": wb_nm_id,
                "ai_cache_reused": ai_source is not None,
                "ai_source": ai_source,
                "facts_fresh": source_facts_fresh,
                "wb_projection": wb_projection,
            },
            "account": {
                "ready": account_ready,
                "active": bool(draft.account and draft.account.is_active),
                "connection_status": (
                    draft.account.connection_status if draft.account else None
                ),
                "has_credentials": bool(
                    draft.account and draft.account.has_credentials
                ),
            },
            "category": {
                "status": category_status,
                "explicit_source_taxonomy": cls._is_explicit_source_mapping(draft.category_mapping),
                "mapping_id": draft.category_mapping_id,
                "mapping_origin": (
                    draft.category_mapping.mapping_source
                    if draft.category_mapping else None
                ),
                "mapping_source_type": (
                    draft.category_mapping.source_type
                    if draft.category_mapping else None
                ),
                "mapping_source_category": (
                    draft.category_mapping.source_category
                    if draft.category_mapping else None
                ),
                "product_type_id": draft.product_type_id,
                "external_category_id": draft.external_category_id,
                "external_type_id": draft.external_type_id,
            },
            "schema": {
                "fresh": schema_fresh,
                "version": (
                    product_type.attributes_version if product_type else None
                ),
                "hash": (
                    product_type.attributes_schema_hash if product_type else None
                ),
            },
            "attributes": {
                "schema_total": len(definitions),
                "supplied_known": sum(
                    1
                    for item in definitions
                    if item.external_attribute_id in supplied_ids
                ),
                "required_total": len(required),
                "required_supplied": len(required) - len(missing_required),
                "missing_required": missing_required[:50],
                "missing_required_truncated": len(missing_required) > 50,
            },
            "dictionaries": {
                "total": len(dictionary_definitions),
                "fresh": len(dictionary_definitions) - len(stale_dictionaries),
                "stale": stale_dictionaries[:50],
                "stale_truncated": len(stale_dictionaries) > 50,
            },
            "validation": {
                "status": draft.validation_status,
                "publishable": validation.get("publishable") is True,
                "error_count": len(validation.get("errors", []))
                if isinstance(validation.get("errors"), list) else 0,
                "warning_count": len(validation.get("warnings", []))
                if isinstance(validation.get("warnings"), list) else 0,
            },
            "reverse_mapping": {
                "automatic_round_trip": False,
                "mode": "reviewed_common_fact_diff",
            },
        }

    @classmethod
    def completeness_summary(
        cls,
        draft: MarketplaceProductDraft,
    ) -> dict:
        """Return bounded field coverage for one normalized Ozon draft."""
        if not isinstance(draft, MarketplaceProductDraft):
            raise MarketplaceDraftValidationError(
                "MarketplaceProductDraft обязателен"
            )
        definitions = []
        if draft.product_type_id is not None:
            definitions = MarketplaceAttributeDefinition.query.filter_by(
                product_type_id=draft.product_type_id,
                is_available=True,
            ).all()
        supplied_ids = set()
        update_baseline = None
        listing_state_error = None
        try:
            effective_documents, update_baseline = cls.publication_documents(
                draft
            )
        except MarketplaceDraftError as exc:
            effective_documents = cls._stored_draft_documents(draft)
            listing_state_error = getattr(
                exc,
                "code",
                "ozon_listing_state_not_reconstructable",
            )
        attributes = effective_documents["attributes"]
        complex_groups = effective_documents["complex_attributes"]

        def attribute_has_value(item: Any) -> bool:
            if not isinstance(item, dict):
                return False
            values = item.get("values")
            return bool(
                isinstance(values, list)
                and values
                and any(
                    isinstance(value, dict)
                    and isinstance(value.get("value"), str)
                    and bool(value["value"].strip())
                    for value in values
                )
            )

        for item in attributes:
            if attribute_has_value(item) and isinstance(
                item.get("attribute_id"),
                str,
            ):
                supplied_ids.add(item["attribute_id"])
        for group in complex_groups:
            if not isinstance(group, dict):
                continue
            for item in group.get("attributes", []):
                if attribute_has_value(item) and isinstance(
                    item.get("attribute_id"),
                    str,
                ):
                    supplied_ids.add(item["attribute_id"])

        content = effective_documents["content"]
        if (
            isinstance(content.get("description"), str)
            and content["description"].strip()
        ):
            supplied_ids.add(cls.OZON_DESCRIPTION_ATTRIBUTE_ID)
        known_ids = {
            definition.external_attribute_id
            for definition in definitions
        }
        required_ids = {
            definition.external_attribute_id
            for definition in definitions
            if definition.is_required
        }
        supplied_known = len(known_ids & supplied_ids)
        supplied_required = len(required_ids & supplied_ids)

        media = effective_documents["media"]
        images = media.get("images")

        def public_image(value: Any) -> bool:
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 2_000
            ):
                return False
            parsed = urlsplit(value)
            return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

        valid_images = {
            value
            for value in images
            if public_image(value)
        } if isinstance(images, list) else set()
        primary_image = media.get("primary_image")
        if public_image(primary_image):
            valid_images.add(primary_image)
        image_count = len(valid_images)
        dimensions = effective_documents["dimensions"]
        commercial = effective_documents["commercial"]
        barcodes = effective_documents["barcodes"]
        valid_barcodes = {
            value.strip()
            for value in barcodes
            if (
                isinstance(value, str)
                and value.strip()
                and len(value) <= 100
            )
        }
        forbidden_brand = cls._forbidden_draft_brand(
            draft,
            attributes=attributes,
            complex_groups=complex_groups,
            definitions=definitions,
        )

        def positive_decimal(value: Any) -> bool:
            try:
                return Decimal(str(value)) > 0
            except (InvalidOperation, TypeError, ValueError):
                return False

        validation = cls._stored_json(
            draft.validation_result_json,
            dict,
        )
        content_complete = bool(
            isinstance(content.get("name"), str)
            and content["name"].strip()
            and isinstance(content.get("description"), str)
            and content["description"].strip()
        )
        physical_complete = bool(
            all(
                positive_decimal(dimensions.get(field))
                for field in ("width", "height", "depth", "weight")
            )
            and dimensions.get("dimension_unit") in cls.DIMENSION_UNITS
            and dimensions.get("weight_unit") in cls.WEIGHT_UNITS
        )
        commercial_complete = bool(
            positive_decimal(commercial.get("price"))
            and commercial.get("vat") in cls.VAT_VALUES
            and commercial.get("currency_code") in cls.CURRENCY_CODES
        )
        return {
            "schema_total": len(known_ids),
            "supplied_known": supplied_known,
            "required_total": len(required_ids),
            "required_supplied": supplied_required,
            "required_complete": supplied_required == len(required_ids),
            "optional_total": max(0, len(known_ids) - len(required_ids)),
            "optional_supplied": max(
                0,
                supplied_known - supplied_required,
            ),
            "flat_attribute_count": len(attributes),
            "complex_group_count": len(complex_groups),
            "image_count": image_count,
            "has_color_image": public_image(media.get("color_image")),
            "media_complete": image_count > 0,
            "barcode_count": len(valid_barcodes),
            "has_barcode": bool(valid_barcodes),
            "brand_allowed": forbidden_brand is None,
            "forbidden_brand": forbidden_brand,
            "preserves_existing_ozon_state": update_baseline is not None,
            "ozon_listing_state_error": listing_state_error,
            "content_complete": content_complete,
            "physical_complete": physical_complete,
            "commercial_complete": commercial_complete,
            "publishable": bool(
                validation.get("publishable") is True
                and draft.status in {"ready", "published"}
                and supplied_required == len(required_ids)
                and content_complete
                and physical_complete
                and commercial_complete
                and image_count > 0
                and forbidden_brand is None
            ),
        }

    @classmethod
    def _forbidden_draft_brand(
        cls,
        draft: MarketplaceProductDraft,
        *,
        attributes: Optional[list] = None,
        complex_groups: Optional[list] = None,
        definitions: Optional[list] = None,
    ) -> Optional[str]:
        """Match only explicit source/current Ozon brand values."""
        candidates: List[Any] = []
        facts_document = cls._stored_json(
            draft.source_facts_json,
            dict,
        )
        facts = facts_document.get("facts")
        if isinstance(facts, dict):
            identity = facts.get("identity")
            if isinstance(identity, dict):
                candidates.append(identity.get("brand"))

        imported_product = draft.imported_product
        candidates.append(getattr(imported_product, "brand", None))
        supplier_product = getattr(
            imported_product,
            "supplier_product",
            None,
        )
        candidates.append(getattr(supplier_product, "brand", None))

        attributes = attributes if isinstance(attributes, list) else (
            cls._stored_json(draft.attributes_json, list)
        )
        complex_groups = (
            complex_groups if isinstance(complex_groups, list) else
            cls._stored_json(draft.complex_attributes_json, list)
        )
        if definitions is None:
            definitions = []
            if draft.product_type_id is not None:
                definitions = MarketplaceAttributeDefinition.query.filter_by(
                    product_type_id=draft.product_type_id,
                    is_available=True,
                ).all()
        brand_attribute_ids = {
            definition.external_attribute_id
            for definition in definitions
            if cls._normalized_text(definition.name) in {
                "brand",
                "бренд",
                "торговая марка",
                "марка производителя",
            }
        }

        def collect(items: Any) -> None:
            if not isinstance(items, list):
                return
            for item in items[: cls.MAX_ATTRIBUTES]:
                if (
                    not isinstance(item, dict)
                    or item.get("attribute_id") not in brand_attribute_ids
                ):
                    continue
                values = item.get("values")
                if not isinstance(values, list):
                    continue
                for value in values[: cls.MAX_ATTRIBUTE_VALUES]:
                    if isinstance(value, dict):
                        candidates.append(value.get("value"))

        collect(attributes)
        for group in complex_groups[: cls.MAX_COMPLEX_GROUPS]:
            if isinstance(group, dict):
                collect(group.get("attributes"))
        return first_forbidden_ozon_brand(candidates)

    @classmethod
    def list_drafts(
        cls,
        *,
        seller_id: int,
        account_id: Optional[int] = None,
        status: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
    ):
        seller_id = cls._positive_integer(seller_id, "seller_id")
        page = cls._positive_integer(page, "page")
        per_page = cls._positive_integer(per_page, "per_page")
        if per_page > 100:
            raise MarketplaceDraftValidationError(
                "per_page не может быть больше 100"
            )
        query = MarketplaceProductDraft.query.options(
            joinedload(MarketplaceProductDraft.marketplace),
            joinedload(MarketplaceProductDraft.account),
            joinedload(MarketplaceProductDraft.imported_product).load_only(
                ImportedProduct.id, ImportedProduct.seller_id, ImportedProduct.title,
            ),
            joinedload(MarketplaceProductDraft.published_listing).load_only(
                MarketplaceListing.id, MarketplaceListing.seller_id,
                MarketplaceListing.account_id, MarketplaceListing.media_json,
            ),
            joinedload(MarketplaceProductDraft.product_type).joinedload(
                MarketplaceProductType.category
            ),
        ).filter_by(seller_id=seller_id)
        if account_id is not None:
            cls._owned_account(seller_id=seller_id, account_id=account_id)
            query = query.filter(MarketplaceProductDraft.account_id == account_id)
        if status:
            status = cls._text(status, "status", maximum=30)
            if status not in {
                "needs_category", "draft", "blocked", "ready", "published", "archived"
            }:
                raise MarketplaceDraftValidationError("Неизвестный статус черновика")
            query = query.filter(MarketplaceProductDraft.status == status)
        return query.order_by(
            MarketplaceProductDraft.updated_at.desc(),
            MarketplaceProductDraft.id.desc(),
        ).paginate(page=page, per_page=per_page, error_out=False)

    @classmethod
    def recent_sources(cls, *, seller_id: int, limit: int = 100) -> list:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        limit = cls._positive_integer(limit, "limit")
        if limit > 200:
            raise MarketplaceDraftValidationError("limit не может быть больше 200")
        return ImportedProduct.query.filter_by(seller_id=seller_id).order_by(
            ImportedProduct.updated_at.desc(),
            ImportedProduct.id.desc(),
        ).limit(limit).all()

    @classmethod
    def _fact_snapshot(cls, product: ImportedProduct) -> Tuple[dict, dict, str]:
        try:
            pack = MarketplaceFactPackBuilder.build(product)
        except MarketplaceFactPackError as exc:
            raise MarketplaceDraftValidationError(str(exc)) from None
        facts_document = {
            "version": pack["version"],
            "source": pack["source"],
            "facts": pack["facts"],
            "unverified_suggestions": pack["unverified_suggestions"],
        }
        if pack.get("seller_common_content", {}).get("fields"):
            facts_document["seller_common_content"] = pack["seller_common_content"]
        cls._canonical_json(facts_document, dict)
        cls._canonical_json(pack["provenance"], dict)
        return facts_document, pack["provenance"], pack["fact_hash"]

    @classmethod
    def _derive_offer_id(cls, product: ImportedProduct, facts_document: dict) -> str:
        identifiers = facts_document.get("facts", {}).get("identifiers", {})
        candidate = (
            identifiers.get("vendor_code")
            or identifiers.get("external_id")
            or f"sellerhub-{product.id}"
        )
        return cls._text(candidate, "offer_id", maximum=200)

    @classmethod
    def _decimal_from_fact(cls, value: Any) -> Optional[str]:
        try:
            return cls._decimal(value, "fact", positive=True)
        except MarketplaceDraftValidationError:
            return None

    @classmethod
    def _dimensions_from_facts(cls, facts_document: dict) -> dict:
        physical = facts_document.get("facts", {}).get("physical", {})
        raw = physical.get("dimensions", {}) if isinstance(physical, dict) else {}
        if not isinstance(raw, dict):
            raw = {}

        # Ozon requires package measurements.  Generic source keys such as
        # ``length_cm`` or ``diameter_cm`` describe the product itself in
        # several supplier feeds and therefore must never be promoted to
        # package dimensions.  Accept only explicit package/packing names.
        result = {}
        dimension_units: Dict[str, Optional[str]] = {}

        for raw_key, raw_value in raw.items():
            if not isinstance(raw_key, str):
                continue
            key_name = cls._normalized_text(raw_key)
            if "упаков" not in key_name:
                continue
            try:
                numeric = Decimal(
                    cls._decimal(raw_value, raw_key, positive=True)
                )
            except MarketplaceDraftValidationError:
                continue
            target = None
            if "ширина" in key_name:
                target = "width"
            elif "высота" in key_name:
                target = "height"
            elif "длина" in key_name or "глубина" in key_name:
                target = "depth"
            if target is not None:
                if "мм" in key_name:
                    factor = Decimal("1")
                elif "см" in key_name:
                    factor = Decimal("10")
                else:
                    continue
                result[target] = cls._decimal(
                    numeric * factor,
                    target,
                    positive=True,
                )
                dimension_units[target] = "MILLIMETERS"
                continue
            if "вес" in key_name:
                if "кг" in key_name:
                    factor = Decimal("1000")
                elif re.search(r"(^|[^а-я])г([^а-я]|$)", key_name):
                    factor = Decimal("1")
                else:
                    continue
                result["weight"] = cls._decimal(
                    numeric * factor,
                    "weight",
                    positive=True,
                )
                result["weight_unit"] = "GRAMS"
        normalized = {
            cls._normalized_text(str(key)).replace(" ", "_"): value
            for key, value in raw.items()
            if isinstance(key, str)
        }

        def dimension(name: str, aliases: Sequence[str]) -> Tuple[Optional[str], Optional[str]]:
            for alias in aliases:
                if alias not in normalized:
                    continue
                raw_value = normalized[alias]
                try:
                    value = Decimal(cls._decimal(raw_value, name, positive=True))
                except MarketplaceDraftValidationError:
                    continue
                if alias.endswith("_mm"):
                    return cls._decimal(value, name, positive=True), "MILLIMETERS"
                if alias.endswith("_cm"):
                    return cls._decimal(value * 10, name, positive=True), "MILLIMETERS"
                if alias.endswith("_inch") or alias.endswith("_in"):
                    return cls._decimal(value, name, positive=True), "INCHES"
                unit = str(
                    normalized.get("dimension_unit")
                    or normalized.get("unit")
                    or ""
                ).strip().upper()
                unit_aliases = {
                    "MM": "MILLIMETERS",
                    "ММ": "MILLIMETERS",
                    "CM": "CENTIMETERS",
                    "СМ": "CENTIMETERS",
                    "IN": "INCHES",
                    "INCH": "INCHES",
                }
                unit = unit_aliases.get(unit, unit)
                return (
                    cls._decimal(value, name, positive=True),
                    unit if unit in cls.DIMENSION_UNITS else None,
                )
            return None, None

        width, width_unit = dimension(
            "width",
            (
                "package_width_mm", "package_width_cm",
                "pack_width_mm", "pack_width_cm",
                "packed_width_mm", "packed_width_cm",
                "width_packed_mm", "width_packed_cm",
                "package_width", "width_packed",
            ),
        )
        height, height_unit = dimension(
            "height",
            (
                "package_height_mm", "package_height_cm",
                "pack_height_mm", "pack_height_cm",
                "packed_height_mm", "packed_height_cm",
                "height_packed_mm", "height_packed_cm",
                "package_height", "height_packed",
            ),
        )
        depth, depth_unit = dimension(
            "depth",
            (
                "package_depth_mm", "package_length_mm",
                "package_depth_cm", "package_length_cm",
                "pack_depth_mm", "pack_length_mm",
                "pack_depth_cm", "pack_length_cm",
                "packed_depth_mm", "packed_length_mm",
                "packed_depth_cm", "packed_length_cm",
                "depth_packed_mm", "length_packed_mm",
                "depth_packed_cm", "length_packed_cm",
                "package_depth", "package_length",
                "depth_packed", "length_packed",
            ),
        )
        if width:
            result["width"] = width
            dimension_units["width"] = width_unit
        if height:
            result["height"] = height
            dimension_units["height"] = height_unit
        if depth:
            result["depth"] = depth
            dimension_units["depth"] = depth_unit
        # A later package alias may replace one Russian mm fact. Convert each
        # observed field from its own explicit unit, without Decimal rounding.
        # Decimal.as_integer_ratio() preserves even a long fractional source
        # exactly; 25.4 mm/inch is represented as the exact ratio 127/5.
        units = [dimension_units.get(field) for field in ("width", "height", "depth")]
        if all(units) and all(field in result for field in ("width", "height", "depth")):
            millimeters_per_unit = {
                "MILLIMETERS": (1, 1),
                "CENTIMETERS": (10, 1),
                "INCHES": (127, 5),
            }
            for target_unit in ("MILLIMETERS", "CENTIMETERS", "INCHES"):
                target_num, target_den = millimeters_per_unit[target_unit]
                converted = {}
                for field in ("width", "height", "depth"):
                    value_num, value_den = Decimal(result[field]).as_integer_ratio()
                    source_num, source_den = millimeters_per_unit[dimension_units[field]]
                    numerator = value_num * source_num * target_den
                    denominator = value_den * source_den * target_num
                    exact, remainder = divmod(numerator, denominator)
                    if remainder or not (0 < exact <= cls.MAX_OZON_API_PHYSICAL_INTEGER):
                        break
                    converted[field] = str(exact)
                if len(converted) == 3:
                    result.update(converted)
                    result["dimension_unit"] = target_unit
                    break

        for alias in (
            "package_weight_g", "package_weight_kg",
            "pack_weight_g", "pack_weight_kg",
            "packed_weight_g", "packed_weight_kg",
            "weight_packed_g", "weight_packed_kg",
            "package_weight", "weight_packed",
        ):
            if alias not in normalized:
                continue
            try:
                value = Decimal(cls._decimal(normalized[alias], "weight", positive=True))
            except MarketplaceDraftValidationError:
                continue
            if alias.endswith("_g"):
                result["weight"] = cls._decimal(value, "weight", positive=True)
                result["weight_unit"] = "GRAMS"
            elif alias.endswith("_kg"):
                result["weight"] = cls._decimal(value * 1000, "weight", positive=True)
                result["weight_unit"] = "GRAMS"
            else:
                unit = str(normalized.get("weight_unit") or "").strip().upper()
                unit_aliases = {
                    "G": "GRAMS",
                    "Г": "GRAMS",
                    "KG": "KILOGRAMS",
                    "КГ": "KILOGRAMS",
                    "LB": "POUNDS",
                }
                unit = unit_aliases.get(unit, unit)
                result["weight"] = cls._decimal(value, "weight", positive=True)
                if unit in cls.WEIGHT_UNITS:
                    result["weight_unit"] = unit
            break

        source_complete = bool(
            all(
                result.get(field) not in (None, "")
                for field in ("width", "height", "depth", "weight")
            )
            and result.get("dimension_unit") in cls.DIMENSION_UNITS
            and result.get("weight_unit") in cls.WEIGHT_UNITS
        )
        if source_complete:
            return result

        # A fresh exact WB projection is a coherent package observation.  Use
        # it only as a complete fallback; never mix a partial source tuple with
        # unrelated per-field measurements.
        wb_package = (
            physical.get("wb_package_dimensions", {})
            if isinstance(physical, dict) else {}
        )
        if isinstance(wb_package, dict):
            try:
                wb_result = {
                    "width": cls._decimal(
                        Decimal(str(wb_package["width_cm"])) * 10,
                        "width",
                        positive=True,
                    ),
                    "height": cls._decimal(
                        Decimal(str(wb_package["height_cm"])) * 10,
                        "height",
                        positive=True,
                    ),
                    "depth": cls._decimal(
                        Decimal(str(wb_package["length_cm"])) * 10,
                        "depth",
                        positive=True,
                    ),
                    "weight": cls._decimal(
                        Decimal(str(wb_package["weight_kg"])) * 1000,
                        "weight",
                        positive=True,
                    ),
                    "dimension_unit": "MILLIMETERS",
                    "weight_unit": "GRAMS",
                }
            except (KeyError, InvalidOperation, TypeError, ValueError):
                wb_result = {}
            if wb_result:
                return wb_result
        return result

    @classmethod
    def _description_fact_text(cls, value: Any) -> str:
        if isinstance(value, bool):
            return "Да" if value else "Нет"
        if isinstance(value, (int, float, Decimal)):
            try:
                return cls._decimal(value, "description fact")[:500]
            except MarketplaceDraftValidationError:
                return ""
        if isinstance(value, str):
            return " ".join(value.split())[:500]
        if isinstance(value, dict):
            return cls._description_fact_text(value.get("raw"))
        if not isinstance(value, list):
            return ""
        rendered = []
        seen = set()
        for item in value[:20]:
            text = cls._description_fact_text(item)
            normalized = cls._normalized_text(text) if text else ""
            if not text or normalized in seen:
                continue
            seen.add(normalized)
            rendered.append(text)
        return ", ".join(rendered)[:500]

    @classmethod
    def _fact_grounded_description(cls, facts_document: dict) -> str:
        """Compose bounded plain text solely from verified current/source facts."""
        facts = facts_document.get("facts", {})
        if not isinstance(facts, dict):
            return ""
        identity = facts.get("identity", {})
        attributes = facts.get("attributes", {})
        if not isinstance(identity, dict):
            identity = {}
        if not isinstance(attributes, dict):
            attributes = {}

        title = cls._description_fact_text(
            identity.get("source_title") or identity.get("title")
        )
        if not title:
            return ""
        lines = [title if title[-1:] in ".!?" else f"{title}."]
        detail_lines = []
        detail_identities = set()

        def add_detail(label: str, value: Any) -> None:
            rendered = cls._description_fact_text(value)
            if not rendered or "http://" in rendered or "https://" in rendered:
                return
            line = f"{label}: {rendered}"
            identity_key = cls._normalized_text(line)
            if identity_key in detail_identities:
                return
            detail_identities.add(identity_key)
            detail_lines.append(f"{line}.")

        add_detail("Бренд", identity.get("brand"))
        add_detail("Категория", identity.get("source_category"))
        add_detail("Цвет", attributes.get("colors"))
        add_detail("Материал", attributes.get("materials"))
        add_detail("Размер", attributes.get("sizes"))
        add_detail("Пол", attributes.get("gender"))
        add_detail("Страна производства", attributes.get("country"))

        blocked_characteristic_tokens = (
            "артикул",
            "закуп",
            "остат",
            "поставщик",
            "ррц",
            "склад",
            "штрихкод",
            "barcode",
            "цена",
        )
        represented_names = {
            "brand",
            "gender",
            "material",
            "size",
            "бренд",
            "материал",
            "пол",
            "размер",
            "страна производства",
            "цвет",
            "цвет товара",
        }
        characteristics = attributes.get("characteristics")
        if isinstance(characteristics, list):
            for item in characteristics[: cls.MAX_ATTRIBUTES]:
                if len(detail_lines) >= 12:
                    break
                if not isinstance(item, dict):
                    continue
                name = cls._description_fact_text(item.get("name"))[:120]
                normalized_name = cls._normalized_text(name) if name else ""
                if (
                    not name
                    or normalized_name in represented_names
                    or any(
                        token in normalized_name
                        for token in blocked_characteristic_tokens
                    )
                ):
                    continue
                add_detail(name, item.get("value"))

        lines.extend(detail_lines)
        return "\n".join(lines)[:5_000]

    @classmethod
    def _seller_common_override(
        cls,
        facts_document: dict,
        field_name: str,
    ) -> Tuple[bool, Any]:
        projection = facts_document.get("seller_common_content")
        if (
            not isinstance(projection, dict)
            or projection.get("schema_version") != 1
            or isinstance(projection.get("content_edit_version"), bool)
            or not isinstance(projection.get("content_edit_version"), int)
            or projection["content_edit_version"] < 1
            or not isinstance(projection.get("fields"), dict)
        ):
            return False, None
        entry = projection["fields"].get(field_name)
        if (
            not isinstance(entry, dict)
            or set(entry) != {"value", "origin"}
            or entry.get("origin") != "seller_override"
        ):
            return False, None
        value = entry.get("value")
        if field_name == "title":
            return (True, value) if isinstance(value, str) and 0 < len(value) <= 500 else (False, None)
        if field_name == "description":
            return (True, value) if isinstance(value, str) and len(value) <= 100_000 else (False, None)
        if field_name == "photos":
            if (
                isinstance(value, list)
                and len(value) <= 30
                and all(isinstance(url, str) and 0 < len(url) <= 2_000 for url in value)
                and len(value) == len(set(value))
            ):
                return True, value
        return False, None

    @classmethod
    def _content_from_facts(cls, facts_document: dict) -> dict:
        facts = facts_document.get("facts", {})
        identity = facts.get("identity", {}) if isinstance(facts, dict) else {}
        if not isinstance(identity, dict):
            identity = {}
        result = {}
        has_title_override, title_override = cls._seller_common_override(
            facts_document, "title",
        )
        has_description_override, description_override = cls._seller_common_override(
            facts_document, "description",
        )
        name = (
            title_override if has_title_override
            else identity.get("title") or identity.get("source_title")
        )
        description = (
            description_override if has_description_override
            else identity.get("description")
        )
        if isinstance(name, str) and name.strip():
            result["name"] = name.strip()[:500]
        if has_description_override:
            result["description"] = description.strip()[:100_000]
            return result
        if not isinstance(description, str) or not description.strip():
            description = identity.get("source_description")
        if (
            (not isinstance(description, str) or not description.strip())
            and isinstance(facts_document.get("version"), int)
            and not isinstance(facts_document.get("version"), bool)
            and facts_document["version"] >= 3
        ):
            description = cls._fact_grounded_description(facts_document)
        if isinstance(description, str) and description.strip():
            result["description"] = description.strip()[:100_000]
        return result

    @classmethod
    def _media_from_facts(cls, facts_document: dict) -> dict:
        has_photo_override, photo_override = cls._seller_common_override(
            facts_document, "photos",
        )
        if has_photo_override:
            return {"images": list(photo_override)}
        images = facts_document.get("facts", {}).get("media", {}).get("images", [])
        if not isinstance(images, list):
            return {}
        normalized = []
        seen = set()
        for raw in images[: cls.MAX_IMAGES]:
            if not isinstance(raw, str):
                continue
            value = raw.strip()
            if not value or len(value) > 2_000 or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return {"images": normalized} if normalized else {}

    @classmethod
    def _barcodes_from_facts(cls, facts_document: dict) -> list:
        values = facts_document.get("facts", {}).get("identifiers", {}).get(
            "barcodes", []
        )
        if not isinstance(values, list):
            return []
        result = []
        seen = set()
        for value in values[: cls.MAX_BARCODES]:
            if not isinstance(value, str):
                continue
            value = value.strip()
            if value and len(value) <= 100 and value not in seen:
                seen.add(value)
                result.append(value)
                if len(result) >= cls.MAX_IMPORT_BARCODES:
                    break
        return result

    @classmethod
    def _commercial_defaults(
        cls,
        account: SellerMarketplaceAccount,
    ) -> dict:
        """Return only explicit account defaults safe for product import.

        Currency is fixed to RUB by the current rollout. VAT is never guessed:
        it is copied only from the seller-owned Ozon account setting.
        """
        result = {"currency_code": "RUB"}
        settings = cls._stored_json(account.settings_json, dict)
        vat = settings.get("default_vat")
        if isinstance(vat, str) and vat in cls.VAT_VALUES:
            result["vat"] = vat
        return result

    @classmethod
    def _commercial_from_facts(
        cls,
        facts_document: dict,
        *,
        account: SellerMarketplaceAccount,
    ) -> dict:
        source = facts_document.get("facts", {}).get("commercial", {})
        result = cls._commercial_defaults(account)
        for key in ("price", "old_price"):
            value = cls._decimal_from_fact(source.get(key))
            if value:
                result[key] = value
        return result

    @staticmethod
    def _positive_fact_decimal(value: Any) -> Optional[Decimal]:
        if isinstance(value, bool) or not isinstance(
            value,
            (str, int, float, Decimal),
        ):
            return None
        try:
            parsed = Decimal(str(value).strip())
        except (InvalidOperation, TypeError, ValueError):
            return None
        return parsed if parsed.is_finite() and parsed > 0 else None

    @classmethod
    def _observed_size_literal(cls, value: Any) -> str:
        """Return only the literal observed supplier size text."""
        raw_values: list = []
        if isinstance(value, dict):
            raw = value.get("raw")
            if isinstance(raw, str) and raw.strip():
                raw_values.append(raw.strip())
        elif isinstance(value, str) and value.strip():
            raw_values.append(value.strip())
        elif isinstance(value, list):
            raw_values.extend(
                item.strip()
                for item in value[: cls.MAX_ATTRIBUTE_VALUES]
                if isinstance(item, str) and item.strip()
            )
        if not raw_values:
            return ""
        return "; ".join(dict.fromkeys(raw_values))[:1_000]

    @classmethod
    def _observed_clothing_sizes(cls, value: Any) -> Tuple[list, str]:
        """Return exact RU-size candidates and the literal clothing size.

        Only the observed ``raw`` value is parsed.  Embedded ``ai_*`` members
        from legacy size containers are deliberately ignored.  Numeric
        clothing ranges are expanded by the official two-point RU size step;
        measurements such as ``50 мл`` or ``длина 38-40 см`` never qualify.
        The caller separately proves that the source category is clothing.
        """
        literal = cls._observed_size_literal(value)
        if not literal:
            return [], ""
        normalized = unicodedata.normalize("NFKC", literal).casefold()
        russian_sizes: list = []

        def add(candidate: str) -> None:
            if candidate not in russian_sizes:
                russian_sizes.append(candidate)

        range_pattern = re.compile(
            r"(?<!\d)(3[2-9]|[4-7]\d|80)\s*[-–—]\s*"
            r"(3[2-9]|[4-7]\d|80)(?!\d)",
        )
        for match in range_pattern.finditer(normalized):
            start_text, end_text = match.groups()
            suffix = normalized[match.end():match.end() + 16]
            if re.match(
                r"\s*(?:мм|см|мл|л|г|кг|дюйм\w*|inch(?:es)?)\b",
                suffix,
            ):
                continue
            bare_range = bool(re.fullmatch(
                r"\s*(?:универсальн\w*\s*)?[\[(]?\s*"
                r"(?:3[2-9]|[4-7]\d|80)\s*[-–—]\s*"
                r"(?:3[2-9]|[4-7]\d|80)\s*[\])]?\s*",
                normalized,
            ))
            prefix = normalized[max(0, match.start() - 50):match.start()]
            explicit_size_context = bool(re.search(
                r"(?:размер\w*|трусик\w*|бель\w*)[^0-9]{0,24}$",
                prefix,
            ))
            leading_size_range = not normalized[:match.start()].strip()
            alpha_parenthetical_range = bool(re.fullmatch(
                r"\s*(?:xxxxl|xxxl|xxl|xl|xs|xxs|xxxxs|xxxs|"
                r"[smlx]|[2-9]xl|os)\s*\(\s*",
                prefix,
            ))
            trailing_parenthetical_range = bool(
                match.start() > 0
                and normalized[match.start() - 1] == "("
                and re.fullmatch(r"\)\s*", normalized[match.end():])
            )
            if not (
                bare_range
                or explicit_size_context
                or leading_size_range
                or alpha_parenthetical_range
                or trailing_parenthetical_range
            ):
                continue
            start = int(start_text)
            end = int(end_text)
            if (
                start <= end
                and end - start <= 16
                and start % 2 == end % 2
            ):
                for size in range(start, end + 1, 2):
                    add(str(size))

        if not russian_sizes:
            leading_single = re.match(
                r"^\s*(3[2-9]|[4-7]\d|80)"
                r"(?=\s*(?:\(|[,;]|(?:российск\w+\s+)?размер\b|$))",
                normalized,
            )
            if leading_single:
                suffix = normalized[leading_single.end():
                                    leading_single.end() + 16]
                if not re.match(
                    r"\s*(?:мм|см|мл|л|г|кг|дюйм\w*|inch(?:es)?)\b",
                    suffix,
                ):
                    add(leading_single.group(1))

        if not russian_sizes:
            hosiery_ranges = re.findall(
                r"(?<!\d)([1-9])\s*[/\\]+\s*([1-9])(?!\d)",
                normalized,
            )
            for start_text, end_text in hosiery_ranges:
                start = int(start_text)
                end = int(end_text)
                if start <= end and end - start <= 3:
                    for size in range(start, end + 1):
                        add(str(size))
        if not russian_sizes:
            leading_hosiery_range = re.match(
                r"^\s*([1-9])\s*[-–—]\s*([1-9])"
                r"(?=\s*(?:[,;(]|размер\b|$))",
                normalized,
            )
            if leading_hosiery_range:
                start = int(leading_hosiery_range.group(1))
                end = int(leading_hosiery_range.group(2))
                if start <= end and end - start <= 3:
                    for size in range(start, end + 1):
                        add(str(size))
        if not russian_sizes:
            leading_hosiery = re.match(
                r"^\s*([1-9])\s*(?:"
                r"\(\s*(?:xxxxl|xxxl|xxl|xl|xs|xxs|[smlx])\s*\)"
                r"|\([^\r\n)]{1,80}\)"
                r"(?=\s*(?:[,;]|на\s+рост\b|$))"
                r"|(?:xxxxl|xxxl|xxl|xl|xs|xxs|[smlx])(?=\s*[,;])"
                r"|размер\b)",
                normalized,
            )
            if leading_hosiery:
                add(leading_hosiery.group(1))
        if not russian_sizes:
            explicit_hosiery = re.search(
                r"\bразмер\s*([1-9])(?!\d)",
                normalized,
            )
            if explicit_hosiery:
                suffix = normalized[
                    explicit_hosiery.end():explicit_hosiery.end() + 16
                ]
                if not re.match(
                    r"\s*(?:мм|см|мл|л|г|кг|дюйм\w*|inch(?:es)?)\b",
                    suffix,
                ):
                    add(explicit_hosiery.group(1))
        if not russian_sizes:
            standalone = re.fullmatch(
                r"\s*(3[2-9]|[4-7]\d|80)\s*",
                normalized,
            )
            if standalone:
                add(standalone.group(1))
        if not russian_sizes:
            hosiery = re.fullmatch(r"\s*([1-9])\s*", normalized)
            if hosiery:
                add(hosiery.group(1))
        if not russian_sizes:
            bra_literal = re.fullmatch(
                r"\s*((?:6[5-9]|[7-9]\d|1[0-2]\d))\s*([a-h])\s*",
                normalized,
            )
            if bra_literal:
                add(
                    f"{bra_literal.group(1)}"
                    f"{bra_literal.group(2).upper()}"
                )
        universal_size = bool(re.search(
            r"\b(?:универсальн\w*|one[\s_-]*size)\b",
            normalized,
        ))
        if not russian_sizes and universal_size:
            add("Универсальный")
        alpha_size = bool(re.search(
            r"(?<![a-zа-яё0-9])"
            r"(?:xxxxl|xxxl|xxl|xl|xs|xxs|xxxxs|xxxs|"
            r"[smlx]|[2-9]xl|os)"
            r"(?![a-zа-яё0-9])",
            normalized,
        ))
        bra_size = bool(re.search(
            r"(?<!\d)(?:6[5-9]|[7-9]\d|1[0-2]\d)\s*[a-hа-н](?![a-zа-яё0-9])",
            normalized,
        ))
        manufacturer_size = (
            literal
            if russian_sizes or universal_size or alpha_size or bra_size
            else ""
        )
        return russian_sizes, manufacturer_size

    @classmethod
    def _observed_wearable_size(cls, value: Any) -> str:
        """Return one exact size suitable for single-value wearable enums."""
        literal = cls._observed_size_literal(value)
        if not literal:
            return ""
        normalized = unicodedata.normalize("NFKC", literal).casefold()
        if re.search(r"\b(?:универсальн\w*|one[\s_-]*size)\b", normalized):
            return "Универсальный"
        alpha = re.search(
            r"(?<![a-zа-яё0-9])"
            r"(xxxxl|xxxl|xxl|xl|xs|xxs|xxxxs|xxxs|"
            r"[smlx]|[2-9]xl|os)"
            r"(?![a-zа-яё0-9])",
            normalized,
        )
        if alpha:
            return alpha.group(1).upper()
        numeric = re.fullmatch(r"\s*(3[2-9]|[4-7]\d|80)\s*", normalized)
        return numeric.group(1) if numeric else ""

    @classmethod
    def _clothing_source_signal(
        cls,
        category_signals: Sequence[str],
    ) -> bool:
        joined = " | ".join(category_signals)
        markers = (
            "одежда >",
            "одежда /",
            "белье для",
            "бельё для",
            "одежда и белье",
            "одежда и бельё",
            "эротическое белье",
            "эротическое бельё",
            "игровые костюмы",
            "ролевые костюмы",
            "платья, мини-платья",
            "пеньюары, сорочки",
            "трусики, стринги",
            "трусы, стринги",
            "чулки, гольфины",
            "колготки",
            "комбинезоны",
            "боди, ками",
            "лифы, топы",
            "пояса для чулок",
            "портупе",
            "пэстис",
            "пестис",
            "стикини",
            "гартер",
            "эротические сувениры > белье",
            "эротические сувениры > бельё",
            "эротическое белье для женщин > обувь",
            "эротическое бельё для женщин > обувь",
            "страпоны и фаллопротезы > трусики",
        )
        return any(marker in joined for marker in markers)

    @classmethod
    def _observed_colors(cls, value: Any) -> list:
        """Map explicit Russian color morphology to Ozon base colors."""
        values = value if isinstance(value, list) else [value]
        tokens = []
        for item in values[: cls.MAX_ATTRIBUTE_VALUES]:
            if not isinstance(item, str):
                continue
            tokens.extend(re.findall(
                r"[a-zа-яё]+",
                unicodedata.normalize("NFKC", item)
                .casefold()
                .replace("ё", "е"),
            ))
        mappings = (
            ("разноцвет", "Разноцветный"),
            ("серебр", "Серебристый"),
            ("золот", "Золотой"),
            ("прозрач", "Прозрачный"),
            ("черн", "Черный"),
            ("бел", "Белый"),
            ("красн", "Красный"),
            ("розов", "Розовый"),
            ("оранж", "Оранжевый"),
            ("желт", "Желтый"),
            ("зелен", "Зеленый"),
            ("голуб", "Голубой"),
            ("син", "Синий"),
            ("фиолет", "Фиолетовый"),
            ("сер", "Серый"),
            ("коричнев", "Коричневый"),
            ("беж", "Бежевый"),
        )
        result = []
        for token in tokens:
            for stem, canonical in mappings:
                if token.startswith(stem):
                    if canonical not in result:
                        result.append(canonical)
                    break
        return result

    @classmethod
    def _observed_material_composition(cls, value: Any) -> str:
        """Render a source-labelled percentage composition, or nothing."""
        values = value if isinstance(value, list) else [value]
        parts = []
        total = Decimal("0")
        for item in values[:20]:
            if not isinstance(item, str) or not item.strip():
                return ""
            match = re.fullmatch(
                r"\s*(\d{1,3}(?:[.,]\d{1,2})?)\s*%\s*(.+?)\s*[.;]?\s*",
                item,
            )
            if not match:
                return ""
            try:
                percentage = Decimal(match.group(1).replace(",", "."))
            except InvalidOperation:
                return ""
            material = " ".join(match.group(2).split())
            if percentage <= 0 or percentage > 100 or not material:
                return ""
            rendered = format(percentage, "f")
            if "." in rendered:
                rendered = rendered.rstrip("0").rstrip(".")
            parts.append(f"{rendered}% {material}")
            total += percentage
        if not parts or not Decimal("99") <= total <= Decimal("101"):
            return ""
        return ", ".join(parts)[:1_000]

    @classmethod
    def _adult_source_signal(cls, category_signals: Sequence[str]) -> bool:
        joined = " | ".join(category_signals)
        direct_markers = (
            "товары для взрослых",
            "эротичес",
            "бдсм",
            "секс игруш",
            "секс-игруш",
            "секс машин",
            "вибратор",
            "фаллоимитатор",
            "мастурбатор",
            "страпон",
            "фаллопротез",
            "анальные стимуляторы",
            "вагинальные шарики",
            "эрекционные кольца",
            "интимная косметика",
        )
        if any(marker in joined for marker in direct_markers):
            return True
        return bool(re.search(
            r"\b(?:вагинальн|анальн|оральн)\w*\s+смазк",
            joined,
        ))

    @classmethod
    def _observed_hashtags(
        cls,
        category_signals: Sequence[str],
        *,
        is_adult: bool,
        is_lubricant: bool,
    ) -> str:
        """Return only category-theme tags allowed by Ozon's own contract."""
        joined = " | ".join(category_signals)
        tags = []
        if is_lubricant or "интимная косметика" in joined:
            tags.append("#интимный_уход")
        if (
            "эротическое белье" in joined
            or "одежда и белье" in joined
        ):
            tags.append("#эротический_образ")
        if "ролев" in joined or "игровые костюмы" in joined:
            tags.append("#ролевой_образ")
        if "бдсм" in joined:
            tags.append("#бдсм")
        if is_adult and not tags:
            tags.append("#для_взрослых")
        return " ".join(dict.fromkeys(tags[:3]))

    @classmethod
    def _official_type_hashtag(cls, product_type_name: Any) -> str:
        """Build one stable taxonomy tag from the selected official type."""
        if not isinstance(product_type_name, str):
            return ""
        normalized = cls._normalized_text(product_type_name)
        slug = re.sub(r"[^0-9a-zа-яё]+", "_", normalized).strip("_")
        return f"#{slug[:80]}" if slug else ""

    @classmethod
    def _merged_official_hashtags(
        cls,
        raw_value: Any,
        *,
        product_type_name: Any,
    ) -> str:
        observed = (
            " ".join(raw_value)
            if isinstance(raw_value, list)
            and all(isinstance(item, str) for item in raw_value)
            else raw_value if isinstance(raw_value, str) else ""
        )
        observed = " ".join(observed.split())
        official = cls._official_type_hashtag(product_type_name)
        tokens = observed.split()
        if official and official not in tokens:
            tokens.append(official)
        return " ".join(tokens[:4])[:1_000]

    @classmethod
    def _attribute_candidate_values(
        cls,
        facts_document: dict,
    ) -> Dict[str, Any]:
        """Build deterministic attribute candidates from observed facts.

        The exact Ozon dictionary still owns the final value: these candidates
        are discarded unless one fresh type-scoped official value matches.
        Category/title-derived values below use narrow, explicit phrases only;
        no fuzzy, LLM or legacy ``ai_*`` data is eligible.
        """
        facts = facts_document.get("facts", {})
        identity = facts.get("identity", {}) if isinstance(facts, dict) else {}
        attributes = facts.get("attributes", {}) if isinstance(facts, dict) else {}
        if not isinstance(identity, dict):
            identity = {}
        if not isinstance(attributes, dict):
            attributes = {}
        candidates: Dict[str, Any] = {}

        def assign(
            names: Sequence[str],
            value: Any,
            *,
            overwrite: bool = False,
        ) -> None:
            if value in (None, "", [], {}):
                return
            for name in names:
                key = cls._normalized_text(name)
                if overwrite:
                    candidates[key] = value
                else:
                    candidates.setdefault(key, value)

        characteristics = attributes.get("characteristics", [])
        if isinstance(characteristics, list):
            for item in characteristics:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                value = item.get("value")
                if isinstance(name, str) and value not in (None, "", [], {}):
                    candidates[cls._normalized_text(name)] = value
        aliases = {
            "brand": (
                "бренд",
                "бренд в одежде и обуви",
                "brand",
            ),
            "country": ("страна производства", "страна-изготовитель", "country of origin"),
            "gender": ("пол", "gender"),
            "season": ("сезон", "season"),
            "age_group": ("возрастная группа", "age group"),
            "colors": ("цвет", "цвет товара", "color"),
            "materials": ("материал", "материал изделия", "material"),
            "sizes": ("размер", "размер товара", "size"),
        }
        source_values = {
            "brand": identity.get("brand"),
            "country": attributes.get("country"),
            "gender": attributes.get("gender"),
            "season": attributes.get("season"),
            "age_group": attributes.get("age_group"),
            "colors": attributes.get("colors"),
            "materials": attributes.get("materials"),
            "sizes": attributes.get("sizes"),
        }
        for source_key, names in aliases.items():
            value = source_values.get(source_key)
            if value in (None, "", [], {}):
                continue
            if source_key == "brand":
                brand_keys = [cls._normalized_text(name) for name in names]
                existing = [
                    candidates[key]
                    for key in brand_keys
                    if candidates.get(key) not in (None, "", [], {})
                ]
                brand_normalized = (
                    cls._normalized_text(value)
                    if isinstance(value, str)
                    else ""
                )
                if any(
                    not isinstance(candidate, str)
                    or cls._normalized_text(candidate) != brand_normalized
                    for candidate in existing
                ):
                    # Two observed source fields disagree on identity.  Never
                    # pick one silently; leave every Ozon brand alias absent
                    # so validation/repair exposes the conflict to the seller.
                    for key in brand_keys:
                        candidates.pop(key, None)
                    continue
                # ``identity.brand`` is the explicitly mapped supplier field.
                # It may restore source display case over a redundant equal
                # characteristic, which can disambiguate provider IDs without
                # changing the normalized brand identity.
                assign(names, value, overwrite=True)
                continue
            assign(names, value)

        # Ozon exposes the display title and free-form color name as ordinary
        # optional attributes in addition to the top-level product fields.
        # Reusing the same observed/current fact is lossless.
        assign(("название",), identity.get("title"))
        assign(("название цвета",), attributes.get("colors"))
        canonical_colors = cls._observed_colors(attributes.get("colors"))
        if canonical_colors:
            assign(
                ("цвет", "цвет товара", "color"),
                canonical_colors,
                overwrite=True,
            )

        identifiers = (
            facts.get("identifiers", {})
            if isinstance(facts, dict) else {}
        )
        if isinstance(identifiers, dict):
            assign(("код продавца",), identifiers.get("vendor_code"))
            # Ozon explicitly requires a unique vendor/article value here when
            # variants must not be combined.  Reusing the observed vendor code
            # is safer than guessing a shared model family.
            assign(
                ("объединить на одной карточке",),
                identifiers.get("vendor_code") or identifiers.get("external_id"),
            )

        source_categories = identity.get("source_categories")
        if not isinstance(source_categories, list):
            source_categories = []
        size_category_values = [
            value
            for value in [
                identity.get("source_category"),
                *source_categories,
            ]
            if isinstance(value, str) and value.strip()
        ]
        size_category_signals = [
            cls._normalized_text(value)
            for value in size_category_values
        ]
        is_clothing_source = cls._clothing_source_signal(
            size_category_signals
        )
        size_literal = cls._observed_size_literal(attributes.get("sizes"))
        russian_sizes, manufacturer_size = cls._observed_clothing_sizes(
            attributes.get("sizes")
        )
        if is_clothing_source and russian_sizes:
            assign(("российский размер",), russian_sizes)
        if is_clothing_source and manufacturer_size:
            assign(("размер производителя",), manufacturer_size)
        wearable_size = cls._observed_wearable_size(
            attributes.get("sizes")
        )
        if wearable_size:
            assign(("размер бдсм-атрибутики",), wearable_size)

        material_composition = cls._observed_material_composition(
            attributes.get("materials")
        )
        if is_clothing_source and material_composition:
            assign(("состав материала",), material_composition)

        # Normalize only well-known spelling expansions.  This is not fuzzy
        # material classification: TPR/TPE must be explicitly present in the
        # observed supplier value, and the result must still exist in the
        # current official Ozon dictionary for this exact type.
        raw_materials = attributes.get("materials")
        material_values = (
            raw_materials
            if isinstance(raw_materials, list)
            else [raw_materials]
        )
        normalized_materials = []
        for material in material_values:
            if not isinstance(material, str) or not material.strip():
                continue
            observed_material = material.strip()
            percentage_match = re.fullmatch(
                r"\s*\d{1,3}(?:[.,]\d{1,2})?\s*%\s*(.+?)\s*[.;]?\s*",
                observed_material,
            )
            if percentage_match:
                observed_material = percentage_match.group(1).strip()
            folded = cls._normalized_text(observed_material)
            tokens = set(re.findall(r"[0-9a-zа-яё]+", folded))
            if (
                "tpr" in tokens
                or "термоэластопласт" in tokens
                or (
                    "термопластичная" in tokens
                    and "резина" in tokens
                )
            ):
                canonical = "Термопластичная резина (TPR)"
            elif (
                "tpe" in tokens
                or (
                    any(token.startswith("термопласт") for token in tokens)
                    and any(token.startswith("эластомер") for token in tokens)
                )
            ):
                canonical = "Термопластичный эластомер (TPE)"
            elif any(token.startswith("силикон") for token in tokens):
                # This input is already the observed supplier ``materials``
                # field.  Strip descriptive surface/quality wording without
                # promoting it into a material: the exact official dictionary
                # still decides whether the canonical material is admissible.
                canonical = (
                    "Медицинский силикон"
                    if any(token.startswith("медицинск") for token in tokens)
                    else "Силикон"
                )
            else:
                canonical = observed_material
            if canonical not in normalized_materials:
                normalized_materials.append(canonical)
        if normalized_materials:
            assign(
                ("материал", "материал изделия", "material"),
                normalized_materials,
                overwrite=True,
            )

        physical = (
            facts.get("physical", {})
            if isinstance(facts, dict) else {}
        )
        product_dimensions = (
            physical.get("dimensions", {})
            if isinstance(physical, dict) else {}
        )
        if not isinstance(product_dimensions, dict):
            product_dimensions = {}
        package_dimensions = (
            physical.get("wb_package_dimensions", {})
            if isinstance(physical, dict) else {}
        )
        if not isinstance(package_dimensions, dict):
            package_dimensions = {}

        length_cm = cls._positive_fact_decimal(
            product_dimensions.get("length_cm")
        )
        if length_cm is not None:
            assign(("длина, см",), length_cm)

        working_length_cm = cls._positive_fact_decimal(
            product_dimensions.get("working_length_cm")
        )
        if working_length_cm is not None:
            working_length_mm = working_length_cm * Decimal("10")
            if working_length_mm == working_length_mm.to_integral_value():
                assign(("длина рабочей части, мм",), working_length_mm)

        diameter_cm = next((
            parsed
            for parsed in (
                cls._positive_fact_decimal(
                    product_dimensions.get("max_diameter_cm")
                ),
                cls._positive_fact_decimal(
                    product_dimensions.get("diameter_cm")
                ),
                cls._positive_fact_decimal(
                    product_dimensions.get("width_cm")
                ),
            )
            if parsed is not None
        ), None)
        if diameter_cm is not None:
            assign(("ширина/диаметр, мм",), diameter_cm * Decimal("10"))

        product_weight_g = cls._positive_fact_decimal(
            product_dimensions.get("weight_g")
        )
        if product_weight_g is not None:
            assign(("вес товара, г",), product_weight_g)

        package_weight_g = cls._positive_fact_decimal(
            product_dimensions.get("package_weight_g")
        )
        if package_weight_g is None:
            package_weight_kg = cls._positive_fact_decimal(
                package_dimensions.get("weight_kg")
            )
            if package_weight_kg is not None:
                package_weight_g = package_weight_kg * Decimal("1000")
        if package_weight_g is not None:
            assign(("вес с упаковкой, г",), package_weight_g)

        source_title = (
            identity.get("source_title")
            if isinstance(identity.get("source_title"), str)
            else ""
        )
        source_categories = identity.get("source_categories")
        if not isinstance(source_categories, list):
            source_categories = []
        category_values = [
            value
            for value in [
                identity.get("source_category"),
                *source_categories,
            ]
            if isinstance(value, str) and value.strip()
        ]
        title_signal = cls._normalized_text(source_title)
        category_signals = [
            cls._normalized_text(value)
            for value in category_values
        ]
        category_signal = " | ".join(category_signals)
        combined_signal = f"{title_signal} {category_signal}"
        is_lubricant = bool(
            re.search(r"\b(?:лубрикант|смазк)\w*", title_signal)
            or any(
                re.search(r"\b(?:лубрикант|смазк)\w*", value)
                for value in category_signals
            )
        )
        is_adult = cls._adult_source_signal(category_signals)
        if is_adult:
            assign(("признак 18+",), True)

        hashtags = cls._observed_hashtags(
            category_signals,
            is_adult=is_adult,
            is_lubricant=is_lubricant,
        )
        if hashtags:
            assign(("#хештеги",), hashtags)

        raw_gender = attributes.get("gender")
        if isinstance(raw_gender, str):
            gender_signal = cls._normalized_text(raw_gender)
            has_female = any(
                marker in gender_signal
                for marker in ("женщ", "женск", "female")
            )
            has_male = any(
                marker in gender_signal
                for marker in ("мужч", "мужск", "male")
            )
            is_unisex = any(
                marker in gender_signal
                for marker in ("унисекс", "unisex", "для двоих", "для пары")
            )
            if is_unisex or (has_female and has_male):
                candidates["__ozon_accessory_gender__"] = "Унисекс"
                candidates["__ozon_clothing_gender__"] = [
                    "Женский",
                    "Мужской",
                ]
            elif has_female:
                candidates["__ozon_accessory_gender__"] = "Для нее"
                candidates["__ozon_clothing_gender__"] = "Женский"
            elif has_male:
                candidates["__ozon_accessory_gender__"] = "Для него"
                candidates["__ozon_clothing_gender__"] = "Мужской"

        characteristic_signals = []
        if isinstance(characteristics, list):
            for item in characteristics:
                if not isinstance(item, dict):
                    continue
                name = cls._normalized_text(str(item.get("name") or ""))
                value = item.get("value")
                values = value if isinstance(value, list) else [value]
                for raw_value in values:
                    if not isinstance(raw_value, str) or not raw_value.strip():
                        continue
                    characteristic_signals.append((
                        name,
                        cls._normalized_text(raw_value),
                    ))

        no_aroma = (
            "без запаха" in title_signal
            or "без аромата" in title_signal
            or any(
                name in {"аромат", "аромат 18+", "запах"}
                and value in {"без запаха", "без аромата"}
                for name, value in characteristic_signals
            )
        )
        if no_aroma:
            assign(("аромат", "аромат 18+"), "Без аромата")

        no_taste = (
            "без вкуса" in title_signal
            or any(
                name in {"вкус", "вкус 18+"}
                and value == "без вкуса"
                for name, value in characteristic_signals
            )
        )
        if no_taste:
            assign(
                (
                    "вкус",
                    "вкус 18+",
                    "вкус презервативов, средств для взрослых",
                ),
                "Без вкуса",
            )
        elif re.search(r"\b(?:эспрессо|espresso)\b", title_signal):
            # Espresso is a literal coffee taste, not a fuzzy flavour guess.
            # A missing exact ``Кофе`` row in the current type dictionary
            # still leaves the attribute empty.
            assign(
                (
                    "вкус",
                    "вкус 18+",
                    "вкус презервативов, средств для взрослых",
                ),
                "Кофе",
            )

        composition_values = [
            value
            for name, value in characteristic_signals
            if name in {
                "состав",
                "основа состава",
                "материал",
                "материал изделия",
            }
        ]
        composition_values.extend(
            cls._normalized_text(value)
            for value in material_values
            if isinstance(value, str) and value.strip()
        )
        composition_signal = (
            f"{title_signal} {' '.join(composition_values)}"
        )
        silicone_observed = (
            bool(re.search(r"\b(?:силикон\w*|silicone)\b", title_signal))
            or any(
            re.search(r"\b(?:силикон\w*|silicone)\b", value)
            for value in composition_values
            )
        )
        if is_lubricant:
            if re.search(
                r"\b(?:водн\w*[\s-]*силикон\w*|"
                r"силикон\w*[\s-]*водн\w*)\b",
                composition_signal,
            ):
                assign(("основа состава",), "Водно-силиконовая")
            elif silicone_observed:
                assign(("основа состава",), "Силиконовая")
            elif re.search(
                r"\b(?:на\s+водн\w*\s+основ\w*|water[\s-]*based)\b",
                composition_signal,
            ):
                assign(("основа состава",), "Водная")
                assign(("текстура",), "На водной основе")
            elif re.search(
                r"\b(?:на\s+маслян\w*\s+основ\w*|oil[\s-]*based)\b",
                composition_signal,
            ):
                assign(("основа состава",), "Масляная")
            elif "глицерин" in title_signal:
                assign(("основа состава",), "Глицериновая")
            elif re.search(r"\bспиртов\w*\s+основ\w*", title_signal):
                assign(("основа состава",), "Спиртовая")
        if is_lubricant and silicone_observed:
            assign(("материал", "материал изделия"), "Силикон")

        if is_lubricant:
            effects = ["Скольжения"]
            for pattern, value in (
                (r"\bвозбуждающ\w*", "Возбуждающий"),
                (r"\bпролонгир\w*", "Продлевающий"),
                (r"\bохлаждающ\w*", "Охлаждающий"),
                (r"\bсогревающ\w*", "Согревающий"),
                (r"\bувлажняющ\w*", "Увлажняющий"),
                (r"\bобезболивающ\w*", "Обезболивающий"),
                (r"\bрасслабляющ\w*", "Расслабляющий"),
                (r"\bсужен\w*|сужающ\w*", "Сужающий"),
                (r"\bочищающ\w*", "Очищающий"),
            ):
                if re.search(pattern, f"{title_signal} {category_signal}"):
                    effects.append(value)
            assign(
                ("эффект интимного средства",),
                list(dict.fromkeys(effects)),
            )
            use_areas = []
            for pattern, value in (
                (r"\bвагинальн\w*", "Вагинальная"),
                (r"\bанальн\w*", "Анальная"),
                (r"\bоральн\w*", "Пероральная"),
            ):
                if any(re.search(pattern, signal) for signal in category_signals):
                    use_areas.append(value)
            if use_areas:
                assign(
                    ("область использования",),
                    use_areas[0] if len(use_areas) == 1 else use_areas,
                )

            volume_signal = f"{source_title} {size_literal}"
            volume_match = re.search(
                r"(?<!\d)(\d{1,5}(?:[.,]\d{1,3})?)\s*мл\b",
                volume_signal,
                flags=re.IGNORECASE,
            )
            if volume_match:
                assign(
                    ("объем, мл", "объём, мл"),
                    volume_match.group(1).replace(",", "."),
                )

        masturbator_kind = None
        if (
            re.search(r"\b(?:вагин\w*|вагина)\b", title_signal)
            or "мастурбаторы и вагины > вагины" in category_signal
        ):
            masturbator_kind = "Вагина"
        elif re.search(r"\b(?:анус\w*|анальн\w*\s+отверст\w*)", title_signal):
            masturbator_kind = "Анус"
        elif (
            "оротимулятор" in category_signal
            or re.search(r"\b(?:ротик\w*|оральн\w*\s+мастурбатор\w*)", title_signal)
        ):
            masturbator_kind = "Ротик"
        if masturbator_kind:
            assign(("тип мастурбатора",), masturbator_kind)

        strapon_kind = None
        strapon_scope = bool(
            "страпон" in combined_signal
            or "фаллопротез" in combined_signal
        )
        if strapon_scope:
            if (
                "безремнев" in title_signal
                or "безремневые страпоны" in category_signal
            ):
                strapon_kind = "Безремневой"
            elif re.search(r"\bтрус(?:ик)?\w*", title_signal):
                strapon_kind = "Трусики"
            elif re.search(r"\bна\s+ремн\w*", title_signal):
                strapon_kind = "На ремнях"
            elif re.search(r"\bс\s+пояс\w*", title_signal):
                strapon_kind = "С поясом"
            elif re.search(r"\bсоединител\w*", title_signal):
                strapon_kind = "Соединитель"
            elif re.search(r"\bнасадк\w*", title_signal):
                strapon_kind = "Насадка"
        if strapon_kind:
            assign(("тип страпона",), strapon_kind)

        bdsm_accessory_kind = None
        for marker, value in (
            ("ошейники, поводки", "Ошейник, поводок"),
            ("маски, шлемы", "Маска"),
            ("кляпы, распорки для рта", "Кляп"),
            ("пояса верности", "Пояс верности"),
            ("перчатки для фистинга", "Перчатки для фистинга"),
            ("бдсм товары и фетиш > наборы", "Набор"),
            ("бдсм свеч", "Свеча"),
        ):
            if marker in category_signal:
                bdsm_accessory_kind = value
                break
        if bdsm_accessory_kind:
            assign(("тип аксессуара бдсм",), bdsm_accessory_kind)

        bdsm_fixator_kind = None
        if re.search(r"\bнаручник\w*", title_signal):
            bdsm_fixator_kind = "Наручники"
        elif re.search(r"\b(?:кандал\w*|оков\w*)", title_signal):
            bdsm_fixator_kind = "Оковы"
        elif re.search(
            r"\bфиксатор\w*(?:\s+\w+){0,5}\s+(?:рук|ног)\w*",
            title_signal,
        ):
            bdsm_fixator_kind = "Фиксатор для рук и ног"
        elif "веревки, скотч для тела" in category_signal:
            if "скотч" in title_signal:
                bdsm_fixator_kind = "Скотч БДСМ"
            elif "лент" in title_signal:
                bdsm_fixator_kind = "Лента БДСМ"
            elif "верев" in title_signal:
                bdsm_fixator_kind = "Веревка для связывания"
        if bdsm_fixator_kind:
            assign(("тип фиксатора бдсм",), bdsm_fixator_kind)

        condom_kinds = []
        if "презервативы > обычные" in category_signal:
            condom_kinds.append("Классические")
        if "с шариками и усиками" in category_signal:
            condom_kinds.append("С усиками")
        if "полиуретановые" in category_signal:
            condom_kinds.append("Полиуретановые")
        if "ароматизированные" in category_signal:
            condom_kinds.append("Ароматизированные")
        for pattern, value in (
            (r"\bультратонк\w*", "Ультратонкие"),
            (r"\b(?:особо\s+)?прочн\w*", "Повышенной прочности"),
            (r"\bxxl\b|увеличенн\w*\s+размер\w*", "Увеличенного размера (XXL)"),
            (r"\bбезлатексн\w*", "Безлатексные"),
            (r"\bлатексн\w*", "Латексные"),
            (r"\bнеароматизирован\w*", "Неароматизированные"),
            (r"\bдополнительн\w*\s+смазк\w*", "С дополнительной смазкой"),
        ):
            if re.search(pattern, title_signal):
                condom_kinds.append(value)
        if condom_kinds:
            assign(
                ("вид презерватива",),
                list(dict.fromkeys(condom_kinds)),
            )

        anal_stimulator_type = None
        for marker, value in (
            ("шарики и цепочки", "Шарики"),
            ("анальный душ", "Душ"),
        ):
            if marker in category_signal:
                anal_stimulator_type = value
                break
        for marker, value in (
            ("елочк", "Елочка"),
            ("рука", "Руки"),
        ):
            if marker in title_signal:
                anal_stimulator_type = value
                break
        if anal_stimulator_type:
            assign(("тип стимулятора",), anal_stimulator_type)

        without_vibration = bool(
            re.search(
                r"\b(?:без[\s-]*вибраци\w*|безвибрацион\w*)",
                combined_signal,
            )
        )
        with_vibration = bool(
            re.search(
                r"\b(?:с\s+вибраци\w*|вибратор\w*|вибро"
                r"(?:пул\w*|яйц\w*|массаж\w*|стимулятор\w*))",
                combined_signal,
            )
        )
        if without_vibration:
            assign(("вибрация",), "Без вибрации")
        elif with_vibration:
            assign(
                ("вибрация",),
                "С вибрацией",
            )

        is_extension = (
            "удлиняющие и расширяющие насадки" in category_signal
        )
        is_member_attachment = bool(
            re.search(r"\bнасадк\w*\s+на\s+(?:член|пенис)\b", title_signal)
            or is_extension
        )
        if is_member_attachment:
            assign(("вид насадки",), "На член")
            assign(("способ крепления 18+",), "На член")

        purposes = []
        if is_extension:
            purposes.append("Для увеличения члена")
        if re.search(r"\bклитор\w*", title_signal):
            purposes.append("Для клиторальной стимуляции")
        if re.search(r"\bанальн\w*", combined_signal):
            purposes.append("Для анального секса")
        if re.search(r"\bвагинальн\w*", combined_signal):
            purposes.append("Для вагинального секса")
        if re.search(r"\bоральн\w*", combined_signal):
            purposes.append("Для орального секса")
        if re.search(r"\bпростат\w*", combined_signal):
            purposes.append("Для стимуляции простаты")
        if re.search(r"\bуретральн\w*", combined_signal):
            purposes.append("Для стимуляции уретры")
        if "ролев" in category_signal:
            purposes.append("Для ролевых игр")
        if "бдсм" in category_signal:
            purposes.append("Для БДСМ")
        if purposes:
            assign(
                ("назначение товара 18+",),
                list(dict.fromkeys(purposes)),
            )

        stimulator_kinds = []
        if "клитор" in title_signal:
            stimulator_kinds.append("Клиторальный")
        if "анальн" in title_signal:
            stimulator_kinds.append("Анальный")
        if "вагинальн" in title_signal:
            stimulator_kinds.append("Вагинальный")
        if re.search(r"(?:точк\w*\s+g|g[\s-]*точк)", title_signal):
            stimulator_kinds.append("Для точки G")
        for marker, value in (
            ("реалистич", "Реалистичный"),
            ("двусторон", "Двусторонний"),
            ("двойной", "Двойной"),
            ("классическ", "Классический"),
            ("вакуумн", "Вакуумный"),
            ("автоматическ", "Автоматический"),
            ("пульсирующ", "Пульсирующий"),
        ):
            if marker in title_signal:
                stimulator_kinds.append(value)
        if re.search(r"\bмини[\s-]*вибратор\w*", title_signal):
            stimulator_kinds.append("Мини вибратор")
        if stimulator_kinds:
            assign(
                ("вид стимулятора",),
                list(dict.fromkeys(stimulator_kinds)),
            )

        features = []
        if re.search(r"\b(?:два|двумя|2)\s+мотор", title_signal):
            features.append("Два мотора")
        if re.search(r"\b(?:три|тремя|3)\s+мотор", title_signal):
            features.append("Три мотора")
        exact_rotation_category = any(
            re.search(
                r"(?:^|>)\s*с\s+ротаци\w*"
                r"(?:\s*\(\s*вращени\w*\s*\))?\s*$",
                value,
            )
            for value in category_signals
        )
        if (
            exact_rotation_category
            or re.search(r"\b(?:ротатор\w*|ротаци\w*|вращени\w*)", title_signal)
        ):
            features.append("С вращением")
        for pattern, value in (
            (r"\bводонепроницаем\w*", "Водонепроницаемость"),
            (r"\b(?:гибк\w*|гнущ\w*)", "Гибкий корпус"),
            (r"\b(?:на|с)\s+присоск\w*", "На присоске"),
            (
                r"\b(?:с\s+пульт\w*|дистанционн\w*\s+управлен\w*)",
                "С пультом управления",
            ),
            (r"\b(?:функци\w*\s+)?нагрев\w*", "Функция нагрева"),
            (r"\bребрист\w*", "Ребристый"),
            (r"\bнадувн\w*", "Надувной"),
            (r"\bбархатист\w*", "Бархатистая поверхность"),
            (r"\bгипоаллерген\w*", "Гипоаллергенно"),
            (r"\bизогнут\w*", "Изогнутый"),
            (r"\bс\s+цепочк\w*", "С цепочкой"),
            (r"\bс\s+шип\w*", "С шипами"),
        ):
            if re.search(pattern, combined_signal):
                features.append(value)
        if any(
            re.search(r"\bбархатист\w*", value)
            for value in composition_values
        ):
            features.append("Бархатистая поверхность")

        is_explicit_set = bool(
            re.search(r"\b(?:набор|комплект)\w*", title_signal)
            and any(
                re.search(r"\b(?:набор|комплект)\w*", value)
                for value in category_signals
            )
        )
        if is_explicit_set:
            kit_parts = []
            for pattern, value in (
                (r"\bвакуумн\w*\s+стимулятор\w*", "Вакуумный стимулятор"),
                (r"\bвибро[\s-]*яйц\w*", "Виброяйцо"),
                (r"\bпульт\w*\s+управлен\w*", "Пульт управления"),
            ):
                if re.search(pattern, title_signal):
                    kit_parts.append(value)
            if len(kit_parts) >= 2:
                assign(("состав комплекта",), "; ".join(kit_parts))
                features.append("Набор")
        if features:
            assign(
                ("особенности 18+",),
                list(dict.fromkeys(features)),
            )

        mode_match = re.search(
            r"(?<!\d)(\d{1,3})\s+режим\w*",
            title_signal,
        )
        if mode_match:
            assign(("количество режимов",), mode_match.group(1))

        if length_cm is not None:
            size_value = None
            if Decimal("5") <= length_cm <= Decimal("7"):
                size_value = "Mini: 5-7 см"
            elif Decimal("8") <= length_cm <= Decimal("12"):
                size_value = "Small: 8-12 см"
            elif Decimal("13") <= length_cm <= Decimal("20"):
                size_value = "Medium: 13-20 см"
            elif Decimal("21") <= length_cm <= Decimal("25"):
                size_value = "Large: 21-25 см"
            elif length_cm >= Decimal("26"):
                size_value = "Extra large: от 26 см"
            if size_value is not None:
                assign(("размер секс-игрушек",), size_value)
        return candidates

    @classmethod
    def _value_strings(cls, raw_value: Any) -> list:
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        # Do not map a partial prefix of an over-large source list. A partial
        # candidate would look authoritative while silently losing the tail.
        # The hard cap still bounds dictionary queries and the serialized
        # draft; an oversized candidate is left unmapped for the existing
        # required-field/preflight checks to surface.
        if len(values) > cls.MAX_ATTRIBUTE_VALUES:
            return []
        result = []
        seen = set()
        for value in values:
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                try:
                    rendered = cls._decimal(value, "attribute value")
                except MarketplaceDraftValidationError:
                    continue
            elif isinstance(value, str):
                rendered = value.strip()
            else:
                continue
            if not rendered or len(rendered) > 1_000 or rendered in seen:
                continue
            seen.add(rendered)
            result.append(rendered)
        return result

    @classmethod
    def _auto_map_attributes(
        cls,
        *,
        product_type: MarketplaceProductType,
        facts_document: dict,
        category_mapping: Optional[MarketplaceCategoryMapping] = None,
    ) -> Tuple[list, dict]:
        """Return ``(attributes, compliance_report)``.

        ``compliance_report`` is the report dict from
        ``ozon_compliance_defaults.apply_to_attributes`` (including its
        ``defaults`` key) so a caller that just (re)built the draft's
        ``attributes_json`` from this result can merge the written
        compliance values into ``provenance_json`` via
        ``_merge_compliance_provenance`` without resolving the type's
        compliance defaults a second time. Callers that only need the
        attribute list (rebase diffing, reference-defaults backfill) discard
        the report.
        """
        if not OzonReferenceService.reference_is_fresh(product_type):
            return [], {"applied": [], "unresolved": [], "evidence": {}, "defaults": {}}
        definitions = MarketplaceAttributeDefinition.query.filter_by(
            product_type_id=product_type.id,
            is_available=True,
            is_enabled=True,
        ).order_by(MarketplaceAttributeDefinition.sort_order.asc()).all()
        candidates = cls._attribute_candidate_values(facts_document)
        facts = facts_document.get("facts", {})
        identifiers = (
            facts.get("identifiers", {})
            if isinstance(facts, dict) else {}
        )
        vendor_code = (
            identifiers.get("vendor_code")
            if isinstance(identifiers, dict) else None
        )
        model_recipe_enabled = cls._mapping_has_model_recipe(
            category_mapping,
            product_type=product_type,
        )
        explicit_source_model_enabled = (
            cls._mapping_has_explicit_source_model_recipe(
                category_mapping,
                product_type=product_type,
            )
        )
        official_scope = cls._normalized_text(
            " ".join((
                product_type.name or "",
                (
                    product_type.category.full_path
                    if product_type.category is not None
                    else ""
                ),
            ))
        )
        official_erotic_clothing = (
            "одежда и аксессуары эротические" in official_scope
        )
        official_adult = bool(
            official_erotic_clothing
            or "товары для взрослых" in official_scope
        )
        matched: List[Tuple[MarketplaceAttributeDefinition, list]] = []
        for attribute in definitions:
            if attribute.attribute_complex_id:
                continue
            if (
                attribute.external_attribute_id
                == cls.OZON_ADULT_ATTRIBUTE_ID
                and official_adult
            ):
                raw_value = candidates.get("признак 18+", True)
            elif (
                attribute.external_attribute_id
                == cls.OZON_HASHTAG_ATTRIBUTE_ID
            ):
                raw_value = cls._merged_official_hashtags(
                    candidates.get("#хештеги"),
                    product_type_name=product_type.name,
                )
            elif (
                attribute.external_attribute_id
                == cls.OZON_ACCESSORY_GENDER_ATTRIBUTE_ID
            ):
                raw_value = candidates.get("__ozon_accessory_gender__")
            elif (
                attribute.external_attribute_id
                == cls.OZON_CLOTHING_GENDER_ATTRIBUTE_ID
            ):
                raw_value = candidates.get("__ozon_clothing_gender__")
            elif (
                attribute.external_attribute_id
                == cls.OZON_TYPE_ATTRIBUTE_ID
                and attribute.dictionary_id
            ):
                # The selected official product type is the exact source of
                # this category-dependent Ozon field.  Its name is accepted
                # only when the same fresh type-scoped dictionary contains one
                # exact normalized value; the normal dictionary gate below
                # remains authoritative.
                raw_value = product_type.name
            elif (
                attribute.external_attribute_id
                == cls.OZON_RUSSIAN_SIZE_ATTRIBUTE_ID
                and product_type.name == "Пэстисы"
            ):
                # Pasties have physical diameter but no wearable Russian size.
                # The official type nevertheless requires this dictionary;
                # keep an explicit observed size when one exists, otherwise
                # use the type-owned non-numeric value if the fresh dictionary
                # contains it exactly.
                raw_value = (
                    candidates.get(cls._normalized_text(attribute.name))
                    or "Универсальный"
                )
            elif (
                attribute.external_attribute_id
                == cls.OZON_MODEL_NAME_ATTRIBUTE_ID
            ):
                if model_recipe_enabled:
                    raw_value = vendor_code
                elif explicit_source_model_enabled:
                    raw_value = cls._isolated_source_model_name(
                        facts_document
                    )
                else:
                    raw_value = candidates.get(
                        cls._normalized_text(attribute.name)
                    )
            elif (
                attribute.external_attribute_id
                in cls.OZON_BRAND_ATTRIBUTE_IDS
                and attribute.dictionary_id
            ):
                raw_value = cls._reviewed_brand_candidate(
                    candidates.get(cls._normalized_text(attribute.name))
                )
            else:
                raw_value = candidates.get(
                    cls._normalized_text(attribute.name)
                )
            # Preserve every bounded source-backed candidate that resolves to
            # the exact current dictionary. Cropping here used to hide source
            # ambiguity and discard meaningful values before draft preflight
            # could explain the provider's max-count constraint.
            values = cls._value_strings(raw_value) if raw_value is not None else []
            if values:
                matched.append((attribute, values))

        dictionary_pairs = []
        attribute_ids = set()
        normalized_values = set()
        for attribute, values in matched:
            if not attribute.dictionary_id:
                continue
            if not OzonReferenceService.dictionary_is_fresh(attribute):
                continue
            for value in values:
                normalized = OzonReferenceService.normalize_value(value)
                dictionary_pairs.append((attribute, value, normalized))
                attribute_ids.add(attribute.id)
                normalized_values.add(normalized)

        value_rows: Dict[Tuple[int, str], list] = {}
        if attribute_ids and normalized_values:
            attribute_id_list = list(attribute_ids)
            normalized_value_list = list(normalized_values)
            for attribute_chunk_start in range(0, len(attribute_ids), 300):
                attribute_chunk = attribute_id_list[
                    attribute_chunk_start:attribute_chunk_start + 300
                ]
                for value_chunk_start in range(0, len(normalized_value_list), 500):
                    value_chunk = normalized_value_list[
                        value_chunk_start:value_chunk_start + 500
                    ]
                    rows = MarketplaceAttributeValue.query.filter(
                        MarketplaceAttributeValue.attribute_id.in_(attribute_chunk),
                        MarketplaceAttributeValue.value_normalized.in_(value_chunk),
                        MarketplaceAttributeValue.is_available.is_(True),
                    ).all()
                    for row in rows:
                        value_rows.setdefault(
                            (row.attribute_id, row.value_normalized),
                            [],
                        ).append(row)

        result = []

        def literal_dictionary_text(value: Any) -> str:
            if not isinstance(value, str):
                return ""
            return " ".join(
                unicodedata.normalize("NFKC", value).split()
            )

        for attribute, values in matched:
            canonical_values = []
            if attribute.dictionary_id:
                if not OzonReferenceService.dictionary_is_fresh(attribute):
                    continue
                restriction = set(attribute.restriction_value_ids)
                for value in values:
                    key = (
                        attribute.id,
                        OzonReferenceService.normalize_value(value),
                    )
                    matches = value_rows.get(key, [])
                    if restriction:
                        matches = [
                            candidate
                            for candidate in matches
                            if candidate.external_value_id in restriction
                        ]
                    if len(matches) == 1:
                        row = matches[0]
                    else:
                        # Ozon sometimes exposes several IDs whose values
                        # differ only by case (or even repeat the same display
                        # value).  The exact type restriction is applied
                        # first; an observed literal may then select exactly
                        # one remaining provider-owned display value without
                        # fuzzy/case inference.
                        literal = literal_dictionary_text(value)
                        literal_matches = [
                            candidate
                            for candidate in matches
                            if literal_dictionary_text(candidate.value)
                            == literal
                        ]
                        if len(literal_matches) != 1:
                            continue
                        row = literal_matches[0]
                    canonical_values.append({
                        "dictionary_value_id": row.external_value_id,
                        "value": row.value,
                    })
            else:
                canonical_values = [{"value": value} for value in values]
            if not canonical_values:
                continue
            result.append({
                "attribute_id": attribute.external_attribute_id,
                "complex_id": attribute.attribute_complex_id or "0",
                "values": canonical_values,
            })

        # Compliance-атрибуты не выводятся из фактов товара — это остаётся
        # запрещённым.  Здесь применяется отдельный слой: подписанное админом
        # решение по ТН ВЭД и выведенный из него по нормативному перечню
        # признак маркировки.  Слой заполняет только пустые поля и никогда не
        # трогает уже заданное значение.
        from services.ozon_compliance_defaults import apply_to_attributes
        result, compliance_report = apply_to_attributes(
            result, product_type.id,
        )
        return result, compliance_report

    @classmethod
    def _merge_compliance_provenance(
        cls,
        draft: MarketplaceProductDraft,
        compliance_report: Optional[dict],
    ) -> None:
        """Merge freshly-written compliance provenance into the draft.

        Called right after ``draft.attributes_json`` was (re)built from
        ``_auto_map_attributes`` at a creation/type-binding point. Only the
        exact value the layer just wrote is recorded (``compliance.<id>``
        keys); pre-existing fact-provenance keys (``attributes.*``,
        ``commercial.*`` etc.) are preserved untouched -- this never does a
        blind overwrite of ``provenance_json``.
        """
        from services.ozon_compliance_defaults import build_provenance_entries

        entries = build_provenance_entries(
            compliance_report,
            (compliance_report or {}).get("defaults"),
        )
        if not entries:
            return
        current_provenance = cls._stored_json(draft.provenance_json, dict)
        current_provenance.update(entries)
        draft.provenance_json = cls._canonical_json(current_provenance, dict)

    @classmethod
    def _provenance_with_preserved_compliance(
        cls,
        draft: MarketplaceProductDraft,
        fresh_provenance: dict,
    ) -> dict:
        """Merge a freshly-snapshotted fact provenance without discarding
        ``compliance.*`` markers recorded by a separate layer.

        ``refresh_facts``/``rebase_source_defaults`` fully replace
        ``provenance_json`` with a new fact-derived snapshot on every source
        sync -- a normal, frequent draft lifecycle event unrelated to
        compliance defaults. Neither of them recomputes the compliance
        layer's own provenance, so a blind overwrite would silently erase it
        even though the compliance attribute's *value* in ``attributes_json``
        is left untouched by the same call (they already exclude compliance
        attribute IDs from their diff). Without this, the very first routine
        facts refresh after a type bind would make the admin-refresh path
        (``ozon_compliance_admin.apply_to_existing_drafts(refresh=True)``)
        treat the stored value as "not ours" and stop refreshing it, even
        though nothing about it changed.
        """
        current_provenance = cls._stored_json(draft.provenance_json, dict)
        preserved_compliance = {
            key: value for key, value in current_provenance.items()
            if isinstance(key, str) and key.startswith("compliance.")
        }
        merged = dict(fresh_provenance)
        merged.update(preserved_compliance)
        return merged

    @classmethod
    def _bind_type(
        cls,
        draft: MarketplaceProductDraft,
        product_type: MarketplaceProductType,
    ) -> None:
        draft.product_type_id = product_type.id
        draft.external_category_id = product_type.category.external_category_id
        draft.external_type_id = product_type.external_type_id
        draft.schema_version = None
        draft.schema_hash = None
        draft.validation_status = "stale"
        draft.validation_result_json = '{}'
        draft.validated_at = None
        draft.status = "draft"

    @classmethod
    def _apply_active_mapping_to_existing_draft(
        cls,
        *,
        seller_id: int,
        marketplace_id: int,
        draft: MarketplaceProductDraft,
        product: ImportedProduct,
    ) -> bool:
        """Bind a newly confirmed exact mapping to an old untyped draft.

        Mass-upload retry calls ``create_draft`` again for terminal
        ``needs_input`` rows.  Reuse the same active exact mapping that a new
        draft would receive, but keep the draft's stored fact snapshot and all
        non-type-specific seller edits.  Refreshing source facts here would
        silently absorb source drift; validation must continue to expose it.
        """
        if (
            draft.product_type_id is not None
            or draft.published_listing_id is not None
            or draft.status in {"published", "archived"}
        ):
            return False
        mapping = cls._active_mapping(
            seller_id=seller_id,
            marketplace_id=marketplace_id,
            product=product,
        )
        if mapping is None:
            return False

        cls._assert_no_active_publication(draft)
        facts_document = cls._stored_json(draft.source_facts_json, dict)
        auto_attributes, compliance_report = cls._auto_map_attributes(
            product_type=mapping.product_type,
            facts_document=facts_document,
            category_mapping=mapping,
        )
        cls._bind_type(draft, mapping.product_type)
        draft.category_mapping_id = mapping.id
        # Attribute IDs and complex groups are type-scoped.  Starting from an
        # untyped draft therefore follows the same contract as an explicit
        # type bind: rebuild deterministic flat attributes from the stored
        # observed facts and discard any ungrounded type-specific containers.
        draft.attributes_json = cls._canonical_json(
            auto_attributes,
            list,
        )
        cls._merge_compliance_provenance(draft, compliance_report)
        draft.complex_attributes_json = '[]'
        draft.attribute_removals_json = '[]'
        draft.updated_at = datetime.utcnow()
        return True

    @classmethod
    def _linked_listing_for_product(
        cls,
        *,
        seller_id: int,
        account: SellerMarketplaceAccount,
        product: ImportedProduct,
    ) -> Optional[MarketplaceListing]:
        rows = MarketplaceListing.query.options(
            joinedload(MarketplaceListing.product_type).joinedload(
                MarketplaceProductType.category
            )
        ).filter_by(
            seller_id=seller_id,
            marketplace_id=account.marketplace_id,
            account_id=account.id,
            imported_product_id=product.id,
        ).order_by(MarketplaceListing.id.asc()).limit(2).all()
        if len(rows) > 1:
            raise MarketplaceDraftConflict(
                "Внутренняя карточка связана с несколькими листингами "
                "одного кабинета Ozon"
            )
        return rows[0] if rows else None

    @classmethod
    def _listing_state_error(
        cls,
        code: str,
        message: str,
    ) -> MarketplaceDraftConflict:
        error = MarketplaceDraftConflict(message)
        error.code = code
        return error

    @classmethod
    def _product_state_error_for_seller(
        cls,
        error: OzonProductStateError,
    ) -> MarketplaceDraftConflict:
        """Translate only known static reconstruction failures for the UI."""
        message = str(error)
        if message == (
            "Current Ozon barcode set cannot be restored through "
            "/v3/product/import"
        ):
            return cls._listing_state_error(
                "ozon_listing_multiple_barcodes",
                "У текущей карточки Ozon несколько штрихкодов. Полное "
                "обновление остановлено, потому что /v3/product/import "
                "переносит только один и мог бы удалить остальные",
            )
        if message == "Ozon listing.type_id must be a string":
            return cls._listing_state_error(
                "ozon_listing_type_missing",
                "Ozon не вернул type_id для текущей карточки. "
                "Синхронизируйте каталог; если карточка остаётся в статусе "
                "«Ошибка», её нужно отдельно разобрать или пересоздать",
            )
        if (
            message.startswith("Ozon listing.attributes[")
            and message.endswith(".value must be a string")
        ):
            return cls._listing_state_error(
                "ozon_listing_attribute_value_invalid",
                "Ozon вернул атрибут в неподдерживаемом формате. "
                "Синхронизируйте каталог и откройте карточку отдельно",
            )
        if (
            message.startswith("listing.dimensions.")
            and "must be an exact positive integer" in message
        ):
            return cls._listing_state_error(
                "ozon_listing_physical_value_invalid",
                "В текущей карточке Ozon есть нулевой, дробный или "
                "неподдерживаемый габарит/вес. Укажите корректный полный "
                "комплект упаковки перед обновлением",
            )
        return cls._listing_state_error(
            "ozon_listing_state_not_reconstructable",
            "Текущую карточку Ozon нельзя полностью и безопасно "
            "восстановить из каталога; синхронизируйте каталог и проверьте "
            "карточку",
        )

    @classmethod
    def _stored_draft_documents(
        cls,
        draft: MarketplaceProductDraft,
    ) -> dict:
        return {
            "content": cls._stored_json(draft.content_json, dict),
            "attributes": cls._stored_json(draft.attributes_json, list),
            "complex_attributes": cls._stored_json(
                draft.complex_attributes_json,
                list,
            ),
            "media": cls._stored_json(draft.media_json, dict),
            "dimensions": cls._stored_json(draft.dimensions_json, dict),
            "barcodes": cls._stored_json(draft.barcodes_json, list),
            "commercial": cls._stored_json(draft.commercial_json, dict),
        }

    @classmethod
    def _normalize_attribute_removals(cls, value: Any) -> list:
        """Normalize explicit exact identities removed from an update baseline.

        Attribute deletion is intentionally separate from the ordinary draft
        overlay.  This keeps preserve-live the default and makes every
        full-state removal reviewable, versioned and bounded.
        """
        if (
            not isinstance(value, list)
            or len(value) > cls.MAX_ATTRIBUTE_REMOVALS
        ):
            raise MarketplaceDraftValidationError(
                "attribute_removals должен быть массивом до "
                f"{cls.MAX_ATTRIBUTE_REMOVALS} элементов"
            )
        result = []
        seen = set()
        for index, item in enumerate(value):
            if not isinstance(item, dict) or set(item) - {
                "attribute_id",
                "complex_id",
            }:
                raise MarketplaceDraftValidationError(
                    f"attribute_removals[{index}] имеет неизвестные поля"
                )
            attribute_id = cls._external_id(
                item.get("attribute_id"),
                f"attribute_removals[{index}].attribute_id",
            )
            complex_raw = item.get("complex_id", "0")
            complex_id = (
                "0"
                if complex_raw == "0"
                else cls._external_id(
                    complex_raw,
                    f"attribute_removals[{index}].complex_id",
                )
            )
            identity = (attribute_id, complex_id)
            if identity in seen:
                raise MarketplaceDraftValidationError(
                    "attribute_removals содержит дубликат "
                    "attribute_id/complex_id"
                )
            seen.add(identity)
            result.append({
                "attribute_id": attribute_id,
                "complex_id": complex_id,
            })
        return result

    @staticmethod
    def _attribute_identity(value: Any) -> Optional[Tuple[str, str]]:
        if not isinstance(value, dict):
            return None
        attribute_id = value.get("attribute_id")
        complex_id = value.get("complex_id", "0")
        if not isinstance(attribute_id, str) or not isinstance(complex_id, str):
            return None
        return attribute_id, complex_id

    @classmethod
    def _merge_listing_documents(
        cls,
        *,
        baseline: dict,
        draft: dict,
        attribute_removals: Sequence[dict] = (),
    ) -> dict:
        """Overlay reviewed draft values while preserving existing Ozon state.

        Content, physical values and commercial values remain editable through
        the draft.  Existing attributes/media are the replace-style baseline;
        new normalized values overlay by exact attribute identity and new image
        URLs append without deleting live slots.  Barcode identity is preserved
        for an existing card unless Ozon currently has no barcode.
        """
        content = dict(baseline["content"])
        for key in ("name", "description"):
            value = draft["content"].get(key)
            if isinstance(value, str) and value.strip():
                content[key] = value

        removal_identities = {
            (
                item["attribute_id"],
                item.get("complex_id", "0"),
            )
            for item in attribute_removals
        }
        baseline_identities = {
            identity
            for identity in (
                cls._attribute_identity(item)
                for item in baseline["attributes"]
            )
            if identity is not None
        }
        for group in baseline["complex_attributes"]:
            if not isinstance(group, dict):
                continue
            baseline_identities.update(
                identity
                for identity in (
                    cls._attribute_identity(item)
                    for item in group.get("attributes", [])
                )
                if identity is not None
            )
        missing_removals = removal_identities - baseline_identities
        if missing_removals:
            raise MarketplaceDraftConflict(
                "Выбранный для удаления атрибут уже отсутствует в текущем "
                "снимке Ozon; обновите каталог и повторно проверьте очистку"
            )

        attributes_by_identity = {}
        attribute_order = []
        for item in baseline["attributes"]:
            identity = cls._attribute_identity(item)
            # Description comes from effective content so a stale catalog
            # value cannot conflict with the deliberate content overlay.
            if (
                identity is None
                or identity
                == (cls.OZON_DESCRIPTION_ATTRIBUTE_ID, "0")
                or identity in removal_identities
            ):
                continue
            attributes_by_identity[identity] = item
            attribute_order.append(identity)
        malformed_attributes = []
        for item in draft["attributes"]:
            identity = cls._attribute_identity(item)
            if identity is None:
                malformed_attributes.append(item)
                continue
            if identity not in attributes_by_identity:
                attribute_order.append(identity)
            attributes_by_identity[identity] = item
        attributes = [
            attributes_by_identity[identity]
            for identity in attribute_order
        ] + malformed_attributes

        complex_groups = []
        for baseline_group in baseline["complex_attributes"]:
            if not isinstance(baseline_group, dict):
                complex_groups.append(baseline_group)
                continue
            remaining = [
                item
                for item in baseline_group.get("attributes", [])
                if cls._attribute_identity(item) not in removal_identities
            ]
            if remaining:
                complex_groups.append({"attributes": remaining})
        if draft["complex_attributes"]:
            positions = {}
            for index, group in enumerate(complex_groups):
                if not isinstance(group, dict):
                    continue
                identities = tuple(
                    identity
                    for identity in (
                        cls._attribute_identity(item)
                        for item in group.get("attributes", [])
                    )
                    if identity is not None
                )
                if identities:
                    positions.setdefault(identities, index)
            for group in draft["complex_attributes"]:
                identities = ()
                if isinstance(group, dict):
                    identities = tuple(
                        identity
                        for identity in (
                            cls._attribute_identity(item)
                            for item in group.get("attributes", [])
                        )
                        if identity is not None
                    )
                position = positions.get(identities) if identities else None
                if position is None:
                    complex_groups.append(group)
                    if identities:
                        positions[identities] = len(complex_groups) - 1
                else:
                    complex_groups[position] = group

        baseline_media = baseline["media"]
        draft_media = draft["media"]
        primary = baseline_media.get("primary_image")
        ordered_main = []
        for value in (
            [primary]
            + list(baseline_media.get("images") or [])
            + [draft_media.get("primary_image")]
            + list(draft_media.get("images") or [])
        ):
            if (
                isinstance(value, str)
                and value
                and value not in ordered_main
                and len(ordered_main) < cls.MAX_IMAGES
            ):
                ordered_main.append(value)
        media = {
            "primary_image": ordered_main[0] if ordered_main else primary,
            "images": ordered_main[1:] if ordered_main else [],
        }
        color = (
            baseline_media.get("color_image")
            or draft_media.get("color_image")
        )
        if color not in (None, ""):
            media["color_image"] = color

        dimensions = dict(baseline["dimensions"])
        for key, value in draft["dimensions"].items():
            if value not in (None, ""):
                dimensions[key] = value

        commercial = dict(baseline["commercial"])
        for key, value in draft["commercial"].items():
            if value not in (None, ""):
                commercial[key] = value

        baseline_barcodes = [
            value
            for value in baseline["barcodes"]
            if isinstance(value, str) and value.strip()
        ]
        draft_barcodes = [
            value
            for value in draft["barcodes"]
            if isinstance(value, str) and value.strip()
        ]
        barcodes = (
            baseline_barcodes[:1]
            if baseline_barcodes
            else draft_barcodes[:1]
        )
        return {
            "content": content,
            "attributes": attributes,
            "complex_attributes": complex_groups,
            "media": media,
            "dimensions": dimensions,
            "barcodes": barcodes,
            "commercial": commercial,
        }

    @classmethod
    def publication_documents(
        cls,
        draft: MarketplaceProductDraft,
        *,
        now: Optional[datetime] = None,
        include_baseline_editor_data: bool = False,
    ) -> Tuple[dict, Optional[dict]]:
        """Return effective normalized blocks and an exact update baseline.

        Create drafts use their stored blocks verbatim.  An update uses a fresh
        exact-account ``MarketplaceListing`` projection as non-destructive
        fallback.  No provider call occurs here; the worker later proves the
        baseline fingerprint with an independent live full-state read.
        """
        if not isinstance(draft, MarketplaceProductDraft):
            raise MarketplaceDraftValidationError(
                "MarketplaceProductDraft обязателен"
            )
        stored = cls._stored_draft_documents(draft)
        try:
            attribute_removals = cls._normalize_attribute_removals(
                cls._stored_json(
                    draft.attribute_removals_json,
                    list,
                )
            )
        except MarketplaceDraftValidationError as exc:
            raise cls._listing_state_error(
                "ozon_attribute_removals_invalid",
                "Список явной очистки атрибутов повреждён; откройте "
                "массовый редактор и сохраните выбор заново",
            ) from exc
        if draft.published_listing_id is None:
            if attribute_removals:
                raise cls._listing_state_error(
                    "ozon_attribute_removals_create_forbidden",
                    "Удаление live-атрибутов допустимо только для уже "
                    "опубликованной карточки Ozon",
                )
            return stored, None

        listing = MarketplaceListing.query.filter_by(
            id=draft.published_listing_id,
            seller_id=draft.seller_id,
            marketplace_id=draft.marketplace_id,
            account_id=draft.account_id,
        ).first()
        if (
            listing is None
            or listing.imported_product_id != draft.imported_product_id
            or listing.offer_id != draft.offer_id
            or not listing.external_product_id
            or not listing.is_available
            or listing.is_archived
        ):
            raise cls._listing_state_error(
                "ozon_listing_identity_conflict",
                "Связанный листинг Ozon отсутствует, архивирован или изменил identity",
            )
        if (
            draft.product_type_id is None
            or listing.product_type_id != draft.product_type_id
            or listing.external_category_id != draft.external_category_id
            or listing.external_type_id != draft.external_type_id
        ):
            raise cls._listing_state_error(
                "ozon_listing_type_conflict",
                "Тип или категория связанного листинга Ozon изменились; синхронизируйте каталог",
            )

        current_time = now or datetime.utcnow()
        freshness = (
            listing.last_seen_at,
            listing.attributes_synced_at,
            listing.prices_synced_at,
        )
        if (
            any(value is None for value in freshness)
            or min(freshness) < current_time - cls.LISTING_HARD_TTL
        ):
            raise cls._listing_state_error(
                "ozon_listing_snapshot_stale",
                "Снимок существующей карточки Ozon старше 48 часов; сначала синхронизируйте каталог",
            )
        try:
            state = OzonProductStateContract.from_listing_projection({
                "product_id": listing.external_product_id,
                "offer_id": listing.offer_id,
                "category_id": listing.external_category_id,
                "type_id": listing.external_type_id,
                "title": listing.title,
                "attributes": cls._stored_json(
                    listing.attributes_json,
                    list,
                ),
                "complex_attributes": cls._stored_json(
                    listing.complex_attributes_json,
                    list,
                ),
                "media": cls._stored_json(listing.media_json, dict),
                "dimensions": cls._stored_json(
                    listing.dimensions_json,
                    dict,
                ),
                "barcodes": cls._stored_json(
                    listing.barcodes_json,
                    list,
                ),
                "price": cls._stored_json(
                    listing.price_summary_json,
                    dict,
                ),
            })
            baseline_documents = OzonProductStateContract.draft_documents(
                state["payload"]
            )
        except MarketplaceDraftError:
            raise
        except OzonProductStateError as exc:
            raise cls._product_state_error_for_seller(exc) from exc
        try:
            effective_documents = cls._merge_listing_documents(
                baseline=baseline_documents,
                draft=stored,
                attribute_removals=attribute_removals,
            )
        except MarketplaceDraftConflict as exc:
            raise cls._listing_state_error(
                "ozon_attribute_removal_snapshot_changed",
                str(exc),
            ) from exc
        baseline_identities = []
        if include_baseline_editor_data:
            baseline_rows = baseline_documents['attributes'] + [
                attribute for group in baseline_documents['complex_attributes']
                for attribute in group.get('attributes', [])
            ]
            exact_identities = set()
            for row in baseline_rows:
                identity = cls._attribute_identity(row)
                if identity is not None:
                    exact_identities.add(identity)
            baseline_identities = [
                {'attribute_id': item[0], 'complex_id': item[1]}
                for item in sorted(exact_identities)
            ]
        return (
            effective_documents,
            {
                "listing_id": listing.id,
                "external_product_id": listing.external_product_id,
                "fingerprint": state["fingerprint"],
                "synced_at": min(freshness).isoformat(),
                "contract_version": OzonProductStateContract.CONTRACT_VERSION,
                **({'attribute_identities': baseline_identities,
                    'preserved_media': baseline_documents['media'],
                    'preserved_barcodes': baseline_documents['barcodes'][:1]}
                   if include_baseline_editor_data else {}),
            },
        )

    @classmethod
    def _apply_linked_listing_to_existing_draft(
        cls,
        *,
        draft: MarketplaceProductDraft,
        listing: MarketplaceListing,
    ) -> bool:
        if (
            draft.published_listing_id is not None
            and draft.published_listing_id != listing.id
        ):
            raise MarketplaceDraftConflict(
                "Черновик уже связан с другим листингом Ozon"
            )
        if (
            draft.product_type_id is not None
            and listing.product_type_id is not None
            and draft.product_type_id != listing.product_type_id
        ):
            raise MarketplaceDraftConflict(
                "Тип товара черновика не совпадает с точным листингом Ozon"
            )

        needs_offer_alignment = draft.offer_id != listing.offer_id
        needs_listing_alignment = draft.published_listing_id is None
        needs_type_alignment = (
            draft.product_type_id is None
            and listing.product_type is not None
        )
        if not (
            needs_offer_alignment
            or needs_listing_alignment
            or needs_type_alignment
        ):
            return False

        cls._assert_no_active_publication(draft)
        if needs_offer_alignment:
            duplicate = MarketplaceProductDraft.query.filter(
                MarketplaceProductDraft.account_id == draft.account_id,
                MarketplaceProductDraft.offer_id == listing.offer_id,
                MarketplaceProductDraft.id != draft.id,
            ).first()
            if duplicate is not None:
                raise MarketplaceDraftConflict(
                    "offer_id точного листинга уже принадлежит другому черновику"
                )
            draft.offer_id = listing.offer_id
        draft.published_listing_id = listing.id
        if needs_type_alignment:
            facts_document = cls._stored_json(draft.source_facts_json, dict)
            auto_attributes, compliance_report = cls._auto_map_attributes(
                product_type=listing.product_type,
                facts_document=facts_document,
            )
            cls._bind_type(draft, listing.product_type)
            draft.category_mapping_id = None
            draft.attributes_json = cls._canonical_json(
                auto_attributes,
                list,
            )
            cls._merge_compliance_provenance(draft, compliance_report)
            draft.complex_attributes_json = "[]"
            draft.attribute_removals_json = "[]"
        draft.updated_at = datetime.utcnow()
        return True

    @classmethod
    def create_draft(
        cls,
        *,
        seller_id: int,
        account_id: int,
        imported_product_id: int,
        product_type_id: Optional[int] = None,
        offer_id: Optional[Any] = None,
        save_mapping: bool = False,
        corrected_by_user_id: Optional[int] = None,
        source_link_preflight: bool = True,
        observed_mapping_preflight: bool = True,
    ) -> MarketplaceProductDraft:
        if not isinstance(save_mapping, bool):
            raise MarketplaceDraftValidationError("save_mapping должен быть boolean")
        if not isinstance(source_link_preflight, bool):
            raise MarketplaceDraftValidationError(
                "source_link_preflight должен быть boolean"
            )
        if not isinstance(observed_mapping_preflight, bool):
            raise MarketplaceDraftValidationError(
                "observed_mapping_preflight должен быть boolean"
            )
        if product_type_id is not None:
            product_type_id = cls._positive_integer(
                product_type_id,
                "product_type_id",
            )
        if offer_id is not None:
            offer_id = cls._text(offer_id, "offer_id", maximum=200)
        if save_mapping and product_type_id is None:
            raise MarketplaceDraftValidationError(
                "save_mapping требует product_type_id"
            )
        account = cls._owned_account(seller_id=seller_id, account_id=account_id)
        product = cls._owned_imported_product(
            seller_id=seller_id,
            imported_product_id=imported_product_id,
        )
        if source_link_preflight:
            from services.marketplace_product_links import (
                MarketplaceProductLinkService,
            )

            link_result = (
                MarketplaceProductLinkService.reconcile_account_products(
                    seller_id=seller_id,
                    account_id=account.id,
                    products=[product],
                )
            )
            blocked = link_result["blocked"].get(product.id)
            if blocked is not None:
                raise MarketplaceDraftConflict(blocked["message"])

        linked_listing = cls._linked_listing_for_product(
            seller_id=seller_id,
            account=account,
            product=product,
        )
        if linked_listing is not None and product_type_id is not None:
            observed_type = linked_listing.product_type
            if (
                observed_type is None
                or product_type_id != observed_type.id
                or observed_type.marketplace_id != account.marketplace_id
                or observed_type.category is None
                or linked_listing.external_category_id != observed_type.category.external_category_id
                or linked_listing.external_type_id != observed_type.external_type_id
            ):
                raise MarketplaceDraftConflict(
                    "Для существующей карточки нельзя выбрать другой тип: "
                    "измените категорию в Ozon и синхронизируйте каталог"
                )
        mapping_identities = cls._mapping_identities(product)
        if (
            linked_listing is None
            and product_type_id is None
            and mapping_identities
        ):
            current_mapping = cls._active_mapping(
                seller_id=seller_id,
                marketplace_id=account.marketplace_id,
                product=product,
            )
            if current_mapping is None or (
                current_mapping.scope_key != "wb_subject"
                and current_mapping.mapping_source == "deterministic"
                and current_mapping.corrected_by_user_id is None
            ):
                cls._ensure_explicit_source_taxonomy_mapping(
                    seller_id=seller_id,
                    marketplace_id=account.marketplace_id,
                    product=product,
                )
            if (
                observed_mapping_preflight
                and cls._active_mapping(
                    seller_id=seller_id,
                    marketplace_id=account.marketplace_id,
                    product=product,
                ) is None
            ):
                cls.reconcile_observed_category_mappings(
                    seller_id=seller_id,
                    marketplace_id=account.marketplace_id,
                )
        if (
            offer_id is not None
            and linked_listing is not None
            and offer_id != linked_listing.offer_id
        ):
            raise MarketplaceDraftConflict(
                "Для связанного листинга Ozon нельзя изменить offer_id"
            )
        existing = MarketplaceProductDraft.query.filter_by(
            seller_id=seller_id,
            account_id=account.id,
            imported_product_id=product.id,
        ).first()
        if existing is not None:
            changed = False
            if linked_listing is not None:
                changed = cls._apply_linked_listing_to_existing_draft(
                    draft=existing,
                    listing=linked_listing,
                )
            if (
                product_type_id is None
                and existing.published_listing_id is None
                and cls._apply_active_mapping_to_existing_draft(
                    seller_id=seller_id,
                    marketplace_id=account.marketplace_id,
                    draft=existing,
                    product=product,
                )
            ):
                changed = True
            if changed:
                try:
                    db.session.commit()
                except StaleDataError:
                    db.session.rollback()
                    raise MarketplaceDraftConflict(
                        "Черновик изменился параллельно; повторите после обновления"
                    ) from None
                except IntegrityError:
                    db.session.rollback()
                    raise MarketplaceDraftConflict(
                        "Черновик конфликтует с точным category mapping"
                    ) from None
            return cls.get_draft(seller_id=seller_id, draft_id=existing.id)

        facts_document, provenance, fact_hash = cls._fact_snapshot(product)
        normalized_offer = (
            linked_listing.offer_id
            if linked_listing is not None
            else offer_id or cls._derive_offer_id(product, facts_document)
        )
        duplicate_listing = MarketplaceListing.query.filter_by(
            account_id=account.id,
            offer_id=normalized_offer,
        ).first()
        if duplicate_listing is not None and (
            duplicate_listing.seller_id != seller_id
            or duplicate_listing.marketplace_id != account.marketplace_id
            or duplicate_listing.imported_product_id != product.id
        ):
            raise MarketplaceDraftConflict(
                "offer_id принадлежит листингу без подтверждённой связи с этой внутренней карточкой"
            )

        selected_type = None
        mapping = None
        if product_type_id is not None:
            selected_type = cls._product_type(
                marketplace_id=account.marketplace_id,
                product_type_id=product_type_id,
            )
        elif linked_listing is not None and linked_listing.product_type:
            selected_type = linked_listing.product_type
        elif duplicate_listing is not None and duplicate_listing.product_type:
            selected_type = duplicate_listing.product_type
        else:
            mapping = cls._active_mapping(
                seller_id=seller_id,
                marketplace_id=account.marketplace_id,
                product=product,
            )
            selected_type = mapping.product_type if mapping else None

        draft = MarketplaceProductDraft(
            seller_id=seller_id,
            marketplace_id=account.marketplace_id,
            account_id=account.id,
            imported_product_id=product.id,
            supplier_product_id=product.supplier_product_id,
            published_listing_id=(
                linked_listing.id
                if linked_listing is not None
                else (
                    duplicate_listing.id
                    if duplicate_listing is not None
                    else None
                )
            ),
            category_mapping_id=mapping.id if mapping else None,
            offer_id=normalized_offer,
            status="needs_category",
            source_fact_hash=fact_hash,
            source_facts_json=cls._canonical_json(facts_document, dict),
            provenance_json=cls._canonical_json(provenance, dict),
            content_json=cls._canonical_json(
                cls._content_from_facts(facts_document), dict
            ),
            media_json=cls._canonical_json(
                cls._media_from_facts(facts_document), dict
            ),
            dimensions_json=cls._canonical_json(
                cls._dimensions_from_facts(facts_document), dict
            ),
            barcodes_json=cls._canonical_json(
                cls._barcodes_from_facts(facts_document), list
            ),
            commercial_json=cls._canonical_json(
                cls._commercial_from_facts(
                    facts_document,
                    account=account,
                ),
                dict,
            ),
            attributes_json='[]',
            complex_attributes_json='[]',
            attribute_removals_json='[]',
            validation_status="never_validated",
            validation_result_json='{}',
        )
        db.session.add(draft)
        if selected_type:
            cls._bind_type(draft, selected_type)
            auto_attributes, compliance_report = cls._auto_map_attributes(
                product_type=selected_type,
                facts_document=facts_document,
                category_mapping=mapping,
            )
            draft.attributes_json = cls._canonical_json(auto_attributes, list)
            cls._merge_compliance_provenance(draft, compliance_report)
            if save_mapping:
                mapping = cls._upsert_mapping(
                    seller_id=seller_id,
                    marketplace_id=account.marketplace_id,
                    product=product,
                    product_type=selected_type,
                    corrected_by_user_id=corrected_by_user_id,
                )
                draft.category_mapping_id = mapping.id
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            existing = MarketplaceProductDraft.query.filter_by(
                seller_id=seller_id,
                account_id=account.id,
                imported_product_id=product.id,
            ).first()
            if existing is not None:
                return cls.get_draft(seller_id=seller_id, draft_id=existing.id)
            raise MarketplaceDraftConflict(
                "offer_id уже используется другим черновиком кабинета"
            ) from None
        return cls.get_draft(seller_id=seller_id, draft_id=draft.id)

    @classmethod
    def _normalize_content(cls, value: Any) -> dict:
        if not isinstance(value, dict) or set(value) - {"name", "description"}:
            raise MarketplaceDraftValidationError(
                "content должен содержать только name и description"
            )
        return {
            "name": cls._optional_text(
                value.get("name"), "content.name", maximum=500
            ),
            "description": cls._optional_text(
                value.get("description"),
                "content.description",
                maximum=100_000,
                multiline=True,
            ),
        }

    @classmethod
    def _normalize_value_items(cls, value: Any, field_name: str) -> list:
        if not isinstance(value, list) or len(value) > cls.MAX_ATTRIBUTE_VALUES:
            raise MarketplaceDraftValidationError(
                f"{field_name} должен быть массивом до {cls.MAX_ATTRIBUTE_VALUES} значений"
            )
        result = []
        for index, item in enumerate(value):
            if not isinstance(item, dict) or set(item) - {
                "dictionary_value_id", "value"
            }:
                raise MarketplaceDraftValidationError(
                    f"{field_name}[{index}] имеет неизвестные поля"
                )
            dictionary_value_id = item.get("dictionary_value_id")
            normalized = {
                "value": cls._text(
                    item.get("value"),
                    f"{field_name}[{index}].value",
                    maximum=1_000,
                    multiline=True,
                )
            }
            if dictionary_value_id not in (None, ""):
                normalized["dictionary_value_id"] = cls._external_id(
                    dictionary_value_id,
                    f"{field_name}[{index}].dictionary_value_id",
                )
            result.append(normalized)
        return result

    @classmethod
    def _normalize_attribute_items(cls, value: Any, field_name: str) -> list:
        if not isinstance(value, list) or len(value) > cls.MAX_ATTRIBUTES:
            raise MarketplaceDraftValidationError(
                f"{field_name} должен быть массивом до {cls.MAX_ATTRIBUTES} элементов"
            )
        result = []
        seen = set()
        for index, item in enumerate(value):
            if not isinstance(item, dict) or set(item) - {
                "attribute_id", "complex_id", "values"
            }:
                raise MarketplaceDraftValidationError(
                    f"{field_name}[{index}] имеет неизвестные поля"
                )
            attribute_id = cls._external_id(
                item.get("attribute_id"),
                f"{field_name}[{index}].attribute_id",
            )
            complex_raw = item.get("complex_id", "0")
            if complex_raw == "0":
                complex_id = "0"
            else:
                complex_id = cls._external_id(
                    complex_raw,
                    f"{field_name}[{index}].complex_id",
                )
            identity = (attribute_id, complex_id)
            if identity in seen:
                raise MarketplaceDraftValidationError(
                    f"{field_name} содержит дубликат attribute_id/complex_id"
                )
            seen.add(identity)
            result.append({
                "attribute_id": attribute_id,
                "complex_id": complex_id,
                "values": cls._normalize_value_items(
                    item.get("values"),
                    f"{field_name}[{index}].values",
                ),
            })
        return result

    @classmethod
    def _normalize_complex_attributes(cls, value: Any) -> list:
        if not isinstance(value, list) or len(value) > cls.MAX_COMPLEX_GROUPS:
            raise MarketplaceDraftValidationError(
                f"complex_attributes должен быть массивом до {cls.MAX_COMPLEX_GROUPS} групп"
            )
        result = []
        total_attributes = 0
        for index, group in enumerate(value):
            if not isinstance(group, dict) or set(group) != {"attributes"}:
                raise MarketplaceDraftValidationError(
                    f"complex_attributes[{index}] должен содержать только attributes"
                )
            attributes = cls._normalize_attribute_items(
                group.get("attributes"),
                f"complex_attributes[{index}].attributes",
            )
            total_attributes += len(attributes)
            if total_attributes > cls.MAX_ATTRIBUTES:
                raise MarketplaceDraftValidationError(
                    "complex_attributes содержит слишком много атрибутов суммарно"
                )
            result.append({"attributes": attributes})
        return result

    @classmethod
    def _normalize_dimensions(cls, value: Any) -> dict:
        allowed = {
            "width", "height", "depth", "dimension_unit", "weight", "weight_unit"
        }
        if not isinstance(value, dict) or set(value) - allowed:
            raise MarketplaceDraftValidationError(
                "dimensions содержит неизвестные поля"
            )
        result = {}
        for field_name in ("width", "height", "depth", "weight"):
            if value.get(field_name) not in (None, ""):
                result[field_name] = cls._decimal(
                    value[field_name],
                    f"dimensions.{field_name}",
                    positive=True,
                )
        if value.get("dimension_unit") not in (None, ""):
            unit = cls._text(
                value["dimension_unit"],
                "dimensions.dimension_unit",
                maximum=30,
            ).upper()
            if unit not in cls.DIMENSION_UNITS:
                raise MarketplaceDraftValidationError(
                    "Неизвестная единица габаритов"
                )
            result["dimension_unit"] = unit
        if value.get("weight_unit") not in (None, ""):
            unit = cls._text(
                value["weight_unit"],
                "dimensions.weight_unit",
                maximum=30,
            ).upper()
            if unit not in cls.WEIGHT_UNITS:
                raise MarketplaceDraftValidationError("Неизвестная единица веса")
            result["weight_unit"] = unit
        return result

    @classmethod
    def _normalize_media(cls, value: Any) -> dict:
        allowed = {"images", "primary_image", "color_image"}
        if not isinstance(value, dict) or set(value) - allowed:
            raise MarketplaceDraftValidationError(
                "media поддерживает только images, primary_image и color_image; "
                "images360 больше не поддерживается Ozon"
            )
        images = value.get("images", [])
        primary_image = value.get("primary_image")
        if primary_image not in (None, ""):
            primary_image = cls._text(
                primary_image,
                "media.primary_image",
                maximum=2_000,
            )
        else:
            primary_image = None
        maximum_images = cls.MAX_IMAGES - (1 if primary_image else 0)
        if not isinstance(images, list) or len(images) > maximum_images:
            raise MarketplaceDraftValidationError(
                f"media.images должен быть массивом до {maximum_images} URL"
            )
        result = []
        seen = {primary_image} if primary_image else set()
        for index, image in enumerate(images):
            image = cls._text(
                image,
                f"media.images[{index}]",
                maximum=2_000,
            )
            if image in seen:
                raise MarketplaceDraftValidationError(
                    "media.images содержит дубликат"
                )
            seen.add(image)
            result.append(image)
        normalized = {"images": result}
        if primary_image:
            normalized["primary_image"] = primary_image
        color_image = value.get("color_image")
        if color_image not in (None, ""):
            color_image = cls._text(
                color_image,
                "media.color_image",
                maximum=2_000,
            )
            if color_image in seen:
                raise MarketplaceDraftValidationError(
                    "media.color_image не должен дублировать основную фотографию"
                )
            normalized["color_image"] = color_image
        return normalized

    @classmethod
    def _normalize_barcodes(cls, value: Any) -> list:
        if not isinstance(value, list) or len(value) > cls.MAX_BARCODES:
            raise MarketplaceDraftValidationError(
                f"barcodes должен быть массивом до {cls.MAX_BARCODES} значений"
            )
        result = []
        seen = set()
        for index, barcode in enumerate(value):
            barcode = cls._text(
                barcode,
                f"barcodes[{index}]",
                maximum=100,
            )
            if barcode in seen:
                raise MarketplaceDraftValidationError("barcodes содержит дубликат")
            seen.add(barcode)
            result.append(barcode)
        return result

    @classmethod
    def _normalize_commercial(cls, value: Any) -> dict:
        if not isinstance(value, dict) or set(value) - {
            "price", "old_price", "vat", "currency_code"
        }:
            raise MarketplaceDraftValidationError(
                "commercial содержит неизвестные поля"
            )
        result = {}
        for key in ("price", "old_price"):
            if value.get(key) not in (None, ""):
                result[key] = cls._decimal(
                    value[key],
                    f"commercial.{key}",
                    positive=True,
                )
        if value.get("vat") not in (None, ""):
            vat = cls._text(value["vat"], "commercial.vat", maximum=10)
            if vat not in cls.VAT_VALUES:
                raise MarketplaceDraftValidationError(
                    "commercial.vat не входит в поддерживаемый Ozon enum"
                )
            result["vat"] = vat
        if value.get("currency_code") not in (None, ""):
            currency_code = cls._text(
                value["currency_code"],
                "commercial.currency_code",
                maximum=3,
            ).upper()
            if currency_code not in cls.CURRENCY_CODES:
                raise MarketplaceDraftValidationError(
                    "На текущем этапе поддерживается только currency_code=RUB"
                )
            result["currency_code"] = currency_code
        return result

    @classmethod
    def _category_change_plan(
        cls,
        *,
        draft: MarketplaceProductDraft,
        target_product_type_id: Optional[int],
        save_mapping: bool,
    ) -> dict:
        cls._assert_linked_category_change(
            draft=draft, target_product_type_id=target_product_type_id,
            save_mapping=save_mapping,
        )
        if target_product_type_id is not None:
            selected_type = cls._product_type(
                marketplace_id=draft.marketplace_id,
                product_type_id=target_product_type_id,
            )
        else:
            selected_type = None
        if save_mapping and selected_type is None:
            raise MarketplaceDraftValidationError(
                "save_mapping требует выбранный product_type_id"
            )
        changing = target_product_type_id != draft.product_type_id or (
            draft.published_listing_id is not None
            and cls.linked_category_state(draft)["mode"] == "repair"
        )
        if changing and selected_type is not None:
            facts = cls._stored_json(draft.source_facts_json, dict)
            auto_attributes, compliance_report = cls._auto_map_attributes(
                product_type=selected_type,
                facts_document=facts,
            )
        elif changing:
            auto_attributes, compliance_report = [], None
        else:
            auto_attributes = cls._stored_json(draft.attributes_json, list)
            compliance_report = None
        type_ids = {value for value in (
            draft.product_type_id,
            target_product_type_id,
        ) if value is not None}
        names = {
            (row.external_attribute_id, row.attribute_complex_id or "0"): row.name
            for row in MarketplaceAttributeDefinition.query.filter(
                MarketplaceAttributeDefinition.marketplace_id == draft.marketplace_id,
                MarketplaceAttributeDefinition.product_type_id.in_(type_ids),
            ).order_by(MarketplaceAttributeDefinition.product_type_id.desc()).limit(
                cls.MAX_ATTRIBUTES * 2 + 1
            ).all()
        } if type_ids else {}
        try:
            impact = build_category_impact(
                draft, target_type=selected_type, auto_attributes=auto_attributes,
                names=names, save_mapping=save_mapping, changing=changing,
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise MarketplaceCategoryImpactTooLarge(str(exc)) from None
        return {
            "selected_type": selected_type,
            "auto_attributes": auto_attributes,
            "compliance_report": compliance_report,
            "impact": impact,
            "changing": changing,
        }

    @classmethod
    def linked_category_state(cls, draft: MarketplaceProductDraft) -> dict:
        """Exact local category identity for an existing Ozon listing."""
        if draft.published_listing_id is None:
            return {"mode": "new"}
        listing = MarketplaceListing.query.filter_by(
            id=draft.published_listing_id,
            seller_id=draft.seller_id,
            marketplace_id=draft.marketplace_id,
            account_id=draft.account_id,
            imported_product_id=draft.imported_product_id,
            offer_id=draft.offer_id,
        ).first()
        if (listing is None or not listing.external_product_id or not listing.is_available
                or listing.is_archived or listing.link_status != "linked"):
            return {"mode": "unavailable"}
        aligned = (
            draft.product_type_id is not None
            and draft.product_type_id == listing.product_type_id
            and draft.external_category_id == listing.external_category_id
            and draft.external_type_id == listing.external_type_id
        )
        if aligned:
            return {"mode": "fixed"}
        product_type = listing.product_type
        if (
            product_type is None
            or product_type.marketplace_id != draft.marketplace_id
            or not product_type.is_available
            or not product_type.is_seller_selectable
            or product_type.category is None
            or product_type.category.marketplace_id != draft.marketplace_id
            or not product_type.category.is_available
            or listing.external_category_id != product_type.category.external_category_id
            or listing.external_type_id != product_type.external_type_id
            or listing.last_seen_at is None
            or listing.info_synced_at is None
            or min(listing.last_seen_at, listing.info_synced_at) < datetime.utcnow() - cls.LISTING_HARD_TTL
        ):
            return {"mode": "unavailable"}
        return {
            "mode": "repair",
            "observed_type": {
                "id": product_type.id,
                "name": product_type.name,
                "category_path": product_type.category.full_path,
            },
        }

    @classmethod
    def _assert_linked_category_change(
        cls, *, draft: MarketplaceProductDraft,
        target_product_type_id: Optional[int], save_mapping: bool,
    ) -> None:
        state = cls.linked_category_state(draft)
        if state["mode"] == "new":
            return
        if (state["mode"] == "fixed" and not save_mapping
                and target_product_type_id == draft.product_type_id):
            return
        if (state["mode"] == "repair" and not save_mapping
                and target_product_type_id == state["observed_type"]["id"]):
            return
        raise MarketplaceDraftConflict(
            "Категория существующей карточки меняется в Ozon. "
            "После изменения синхронизируйте каталог; здесь можно только "
            "восстановить тип из свежего точного листинга."
        )

    @classmethod
    def category_impact(
        cls,
        *,
        seller_id: int,
        draft_id: int,
        expected_version: int,
        target_product_type_id: Optional[int],
        save_mapping: bool,
        actor_user_id: Optional[int],
        page: int = 1,
    ) -> dict:
        expected_version = cls._positive_integer(expected_version, "expected_version")
        page = cls._positive_integer(page, "page")
        if page > 100:
            raise MarketplaceDraftValidationError("Страница preview вне предела")
        save_mapping = cls._strict_boolean(save_mapping, "save_mapping")
        if target_product_type_id is not None:
            target_product_type_id = cls._positive_integer(
                target_product_type_id, "target_product_type_id"
            )
        draft = cls.get_draft(seller_id=seller_id, draft_id=draft_id)
        if draft.version != expected_version:
            raise MarketplaceDraftConflict(
                "Черновик изменился; проверьте сохранённую версию перед сменой категории"
            )
        if draft.status == "archived":
            raise MarketplaceDraftConflict("Архивный черновик нельзя редактировать")
        cls._assert_no_active_publication(draft)
        plan = cls._category_change_plan(
            draft=draft,
            target_product_type_id=target_product_type_id,
            save_mapping=save_mapping,
        )
        impact = plan["impact"]
        start = (page - 1) * CATEGORY_IMPACT_PAGE_SIZE
        if start >= impact["total"] and page != 1:
            raise MarketplaceDraftValidationError("Страница preview вне списка")
        return {
            "draft_id": draft.id,
            "account_id": draft.account_id,
            "version": draft.version,
            "current_type": {
                "id": draft.product_type_id,
                "name": draft.product_type.name,
                "category_path": draft.product_type.category.full_path,
            } if draft.product_type and draft.product_type.category else None,
            "target_type": impact["target_type"],
            "save_mapping": save_mapping,
            "counts": impact["counts"],
            "total": impact["total"],
            "digest": impact["digest"],
            "page": page,
            "has_more": start + CATEGORY_IMPACT_PAGE_SIZE < impact["total"],
            "rows": impact["rows"][start:start + CATEGORY_IMPACT_PAGE_SIZE],
            "review_token": issue_category_review_token(
                draft, actor_user_id=actor_user_id,
                target_type=plan["selected_type"],
                save_mapping=save_mapping, impact=impact,
            ),
        }

    @classmethod
    def update_draft(
        cls,
        *,
        seller_id: int,
        draft_id: int,
        expected_version: int,
        patch: Dict[str, Any],
        corrected_by_user_id: Optional[int] = None,
        category_review_token: Optional[str] = None,
        category_review_required: bool = True,
    ) -> MarketplaceProductDraft:
        reviewed_change = (
            category_review_required
            and isinstance(patch, dict)
            and "product_type_id" in patch
        )
        if reviewed_change:
            session = db.session()
            if session.new or session.dirty or session.deleted:
                raise MarketplaceDraftConflict(
                    "Сначала завершите текущее изменение черновика"
                )
            if db.engine.dialect.name == "sqlite":
                raw = session.connection().connection.driver_connection
                if raw.in_transaction:
                    raise MarketplaceDraftConflict(
                        "Сначала завершите текущую транзакцию черновика"
                    )
                previous_timeout = raw.execute("PRAGMA busy_timeout").fetchone()[0]
                try:
                    raw.execute("PRAGMA busy_timeout=200")
                    session.execute(text("BEGIN IMMEDIATE"))
                except OperationalError:
                    raw.execute(f"PRAGMA busy_timeout={int(previous_timeout)}")
                    session.rollback()
                    raise MarketplaceDraftConflict(
                        "Черновик сейчас меняется; повторите проверку позже"
                    ) from None
                else:
                    raw.execute(f"PRAGMA busy_timeout={int(previous_timeout)}")
                    # A prior preview may have cached the draft/type/source in
                    # this Session. The lock protects the database, so refresh
                    # every identity before recomputing the signed impact.
                    session.expire_all()
        try:
            return cls._update_draft_core(
                seller_id=seller_id, draft_id=draft_id,
                expected_version=expected_version, patch=patch,
                corrected_by_user_id=corrected_by_user_id,
                category_review_token=category_review_token,
                category_review_required=category_review_required,
            )
        except Exception:
            if reviewed_change:
                db.session.rollback()
            raise

    @classmethod
    def _update_draft_core(
        cls,
        *,
        seller_id: int,
        draft_id: int,
        expected_version: int,
        patch: Dict[str, Any],
        corrected_by_user_id: Optional[int] = None,
        category_review_token: Optional[str] = None,
        category_review_required: bool = True,
        commit: bool = True,
    ) -> MarketplaceProductDraft:
        """Mutate a draft; internal review workflows may flush with their audit.

        ``commit=False`` requires a caller-owned short writer transaction and
        rollback on any error. It never weakens version/publication checks.
        """
        expected_version = cls._positive_integer(expected_version, "expected_version")
        if not isinstance(patch, dict) or not patch:
            raise MarketplaceDraftValidationError("patch должен быть непустым объектом")
        allowed = {
            "offer_id", "product_type_id", "save_mapping", "content",
            "attributes", "complex_attributes", "media", "dimensions",
            "attribute_removals", "barcodes", "commercial",
        }
        unknown = set(patch) - allowed
        if unknown:
            raise MarketplaceDraftValidationError(
                "patch содержит неизвестные поля: " + ", ".join(sorted(unknown))
            )
        draft = cls.get_draft(seller_id=seller_id, draft_id=draft_id)
        if draft.version != expected_version:
            raise MarketplaceDraftConflict(
                "Черновик изменился; обновите страницу и повторите"
            )
        if draft.status == "archived":
            raise MarketplaceDraftConflict(
                "Архивный черновик нельзя редактировать до восстановления listing"
            )
        cls._assert_no_active_publication(draft)

        save_mapping = patch.get("save_mapping", False)
        if "save_mapping" in patch:
            save_mapping = cls._strict_boolean(save_mapping, "save_mapping")
        if "product_type_id" in patch or save_mapping:
            target_type_id = patch.get("product_type_id", draft.product_type_id)
            if target_type_id is not None:
                target_type_id = cls._positive_integer(target_type_id, "product_type_id")
            cls._assert_linked_category_change(
                draft=draft, target_product_type_id=target_type_id,
                save_mapping=save_mapping,
            )
        category_plan = None
        if "product_type_id" in patch and category_review_required:
            if set(patch) & {"attributes", "complex_attributes", "attribute_removals"}:
                raise MarketplaceDraftValidationError(
                    "Смену категории и изменение характеристик сохраните отдельно"
                )
            target_type_id = patch["product_type_id"]
            if target_type_id is not None:
                target_type_id = cls._positive_integer(target_type_id, "product_type_id")
            category_plan = cls._category_change_plan(
                draft=draft, target_product_type_id=target_type_id,
                save_mapping=save_mapping,
            )
            if not valid_category_review_token(
                category_review_token, draft,
                actor_user_id=corrected_by_user_id,
                target_type=category_plan["selected_type"],
                save_mapping=save_mapping, impact=category_plan["impact"],
            ):
                raise MarketplaceDraftConflict(
                    "Проверка последствий смены категории устарела; откройте её снова"
                )

        product_type_changed = False
        if "offer_id" in patch:
            offer_id = cls._text(patch["offer_id"], "offer_id", maximum=200)
            conflict = MarketplaceProductDraft.query.filter(
                MarketplaceProductDraft.account_id == draft.account_id,
                MarketplaceProductDraft.offer_id == offer_id,
                MarketplaceProductDraft.id != draft.id,
            ).first()
            listing = MarketplaceListing.query.filter_by(
                account_id=draft.account_id,
                offer_id=offer_id,
            ).first()
            if conflict or (listing and listing.id != draft.published_listing_id):
                raise MarketplaceDraftConflict(
                    "offer_id уже используется в этом кабинете"
                )
            draft.offer_id = offer_id

        selected_type = draft.product_type
        if category_plan is not None:
            if category_plan["selected_type"] is None and category_plan["changing"]:
                selected_type = None
                draft.product_type_id = None
                draft.category_mapping_id = None
                draft.external_category_id = None
                draft.external_type_id = None
                draft.attributes_json = '[]'
                draft.complex_attributes_json = '[]'
                draft.attribute_removals_json = '[]'
                draft.status = "needs_category"
                product_type_changed = True
            elif category_plan["selected_type"] is not None:
                selected_type = category_plan["selected_type"]
                if category_plan["changing"]:
                    cls._bind_type(draft, selected_type)
                    draft.attributes_json = cls._canonical_json(
                        category_plan["auto_attributes"], list,
                    )
                    cls._merge_compliance_provenance(
                        draft, category_plan["compliance_report"]
                    )
                    draft.complex_attributes_json = '[]'
                    draft.attribute_removals_json = '[]'
                    draft.category_mapping_id = None
                    product_type_changed = True
        elif "product_type_id" in patch:
            # Internal bulk repair already has its own reviewed, scoped form
            # contract. The interactive JSON/classic route always opts into
            # the signed single-draft review above.
            if patch["product_type_id"] is None:
                selected_type = None
                draft.product_type_id = None
                draft.category_mapping_id = None
                draft.external_category_id = None
                draft.external_type_id = None
                draft.attributes_json = '[]'
                draft.complex_attributes_json = '[]'
                draft.attribute_removals_json = '[]'
                draft.status = "needs_category"
                product_type_changed = True
            else:
                selected_type = cls._product_type(
                    marketplace_id=draft.marketplace_id,
                    product_type_id=patch["product_type_id"],
                )
                if selected_type.id != draft.product_type_id or (
                    draft.published_listing_id is not None
                    and cls.linked_category_state(draft)["mode"] == "repair"
                ):
                    cls._bind_type(draft, selected_type)
                    facts_document = cls._stored_json(draft.source_facts_json, dict)
                    auto_attributes, compliance_report = cls._auto_map_attributes(
                        product_type=selected_type,
                        facts_document=facts_document,
                    )
                    draft.attributes_json = cls._canonical_json(auto_attributes, list)
                    cls._merge_compliance_provenance(draft, compliance_report)
                    draft.complex_attributes_json = '[]'
                    draft.attribute_removals_json = '[]'
                    draft.category_mapping_id = None
                    product_type_changed = True

        if save_mapping:
            if selected_type is None:
                raise MarketplaceDraftValidationError(
                    "save_mapping требует выбранный product_type_id"
                )
            mapping = cls._upsert_mapping(
                seller_id=seller_id,
                marketplace_id=draft.marketplace_id,
                product=draft.imported_product,
                product_type=selected_type,
                corrected_by_user_id=corrected_by_user_id,
            )
            draft.category_mapping_id = mapping.id

        field_normalizers = {
            "content": (cls._normalize_content, "content_json", dict),
            "attributes": (
                lambda value: cls._normalize_attribute_items(value, "attributes"),
                "attributes_json",
                list,
            ),
            "complex_attributes": (
                cls._normalize_complex_attributes,
                "complex_attributes_json",
                list,
            ),
            "attribute_removals": (
                cls._normalize_attribute_removals,
                "attribute_removals_json",
                list,
            ),
            "media": (cls._normalize_media, "media_json", dict),
            "dimensions": (cls._normalize_dimensions, "dimensions_json", dict),
            "barcodes": (cls._normalize_barcodes, "barcodes_json", list),
            "commercial": (cls._normalize_commercial, "commercial_json", dict),
        }
        for patch_name, (normalizer, column_name, expected_type) in field_normalizers.items():
            if patch_name in patch:
                normalized = normalizer(patch[patch_name])
                setattr(
                    draft,
                    column_name,
                    cls._canonical_json(normalized, expected_type),
                )

        if draft.product_type_id:
            draft.status = "draft"
        elif product_type_changed:
            draft.status = "needs_category"
        draft.validation_status = "stale"
        draft.validation_result_json = cls._canonical_json({
            "publishable": False,
            "errors": [{
                "code": "validation_stale",
                "field": "draft",
                "message": "Черновик изменён и требует повторной валидации",
            }],
            "warnings": [],
        }, dict)
        draft.validated_at = None
        draft.updated_at = datetime.utcnow()
        if not commit:
            db.session.flush()
            return draft
        try:
            db.session.commit()
        except StaleDataError:
            db.session.rollback()
            raise MarketplaceDraftConflict(
                "Черновик изменился параллельно; повторите после обновления"
            ) from None
        except IntegrityError:
            db.session.rollback()
            raise MarketplaceDraftConflict(
                "Черновик конфликтует с offer/category mapping этого кабинета"
            ) from None
        return cls.get_draft(seller_id=seller_id, draft_id=draft_id)

    @classmethod
    def refresh_facts(
        cls,
        *,
        seller_id: int,
        draft_id: int,
        expected_version: int,
    ) -> MarketplaceProductDraft:
        expected_version = cls._positive_integer(expected_version, "expected_version")
        draft = cls.get_draft(seller_id=seller_id, draft_id=draft_id)
        if draft.version != expected_version:
            raise MarketplaceDraftConflict(
                "Черновик изменился; обновите страницу и повторите"
            )
        if draft.status == "archived":
            raise MarketplaceDraftConflict(
                "Архивный черновик нельзя обновлять до восстановления listing"
            )
        cls._assert_no_active_publication(draft)
        facts_document, provenance, fact_hash = cls._fact_snapshot(
            draft.imported_product
        )
        draft.source_facts_json = cls._canonical_json(facts_document, dict)
        draft.provenance_json = cls._canonical_json(
            cls._provenance_with_preserved_compliance(draft, provenance), dict,
        )
        draft.source_fact_hash = fact_hash
        draft.validation_status = "stale"
        draft.validation_result_json = cls._canonical_json({
            "publishable": False,
            "errors": [{
                "code": "facts_refreshed",
                "field": "source_facts",
                "message": "Факты обновлены; пользовательские поля не перезаписаны",
            }],
            "warnings": [],
        }, dict)
        draft.validated_at = None
        if draft.product_type_id:
            draft.status = "draft"
        try:
            db.session.commit()
        except StaleDataError:
            db.session.rollback()
            raise MarketplaceDraftConflict(
                "Черновик изменился параллельно; повторите после обновления"
            ) from None
        return cls.get_draft(seller_id=seller_id, draft_id=draft_id)

    @classmethod
    def rebase_source_defaults(
        cls,
        *,
        seller_id: int,
        draft_id: int,
        expected_version: int,
    ) -> MarketplaceProductDraft:
        """Refresh source-derived draft values without overwriting seller edits.

        This is a deterministic three-way rebase:

        * values still equal to the previous source-derived default follow the
          current canonical fact pack;
        * values that diverged from the previous default are seller edits and
          remain untouched;
        * newly observed safe defaults fill fields that were absent before.

        No provider or LLM call is allowed here.  Complex groups are always
        preserved because the automatic mapper never invents their grouping.
        """
        expected_version = cls._positive_integer(
            expected_version,
            "expected_version",
        )
        draft = cls.get_draft(seller_id=seller_id, draft_id=draft_id)
        if draft.version != expected_version:
            raise MarketplaceDraftConflict(
                "Черновик изменился; обновите страницу и повторите"
            )
        if draft.status == "archived":
            raise MarketplaceDraftConflict(
                "Архивный черновик нельзя обновлять до восстановления listing"
            )
        cls._assert_no_active_publication(draft)

        previous_facts = cls._stored_json(draft.source_facts_json, dict)
        current_facts, provenance, fact_hash = cls._fact_snapshot(
            draft.imported_product
        )
        if fact_hash == draft.source_fact_hash:
            return draft

        def merge_mapping(
            current: dict,
            previous_default: dict,
            current_default: dict,
        ) -> dict:
            result = dict(current)
            keys = (
                set(previous_default)
                | set(current_default)
                | set(current)
            )
            for key in keys:
                if current.get(key) != previous_default.get(key):
                    continue
                if key in current_default:
                    result[key] = current_default[key]
                else:
                    result.pop(key, None)
            return result

        current_content = cls._stored_json(draft.content_json, dict)
        draft.content_json = cls._canonical_json(
            merge_mapping(
                current_content,
                cls._content_from_facts(previous_facts),
                cls._content_from_facts(current_facts),
            ),
            dict,
        )

        current_media = cls._stored_json(draft.media_json, dict)
        previous_media = cls._media_from_facts(previous_facts)
        if current_media == previous_media:
            draft.media_json = cls._canonical_json(
                cls._media_from_facts(current_facts),
                dict,
            )

        current_dimensions = cls._stored_json(
            draft.dimensions_json,
            dict,
        )
        draft.dimensions_json = cls._canonical_json(
            merge_mapping(
                current_dimensions,
                cls._dimensions_from_facts(previous_facts),
                cls._dimensions_from_facts(current_facts),
            ),
            dict,
        )

        current_barcodes = cls._stored_json(draft.barcodes_json, list)
        if current_barcodes == cls._barcodes_from_facts(previous_facts):
            draft.barcodes_json = cls._canonical_json(
                cls._barcodes_from_facts(current_facts),
                list,
            )

        current_commercial = cls._stored_json(
            draft.commercial_json,
            dict,
        )
        draft.commercial_json = cls._canonical_json(
            merge_mapping(
                current_commercial,
                cls._commercial_from_facts(
                    previous_facts,
                    account=draft.account,
                ),
                cls._commercial_from_facts(
                    current_facts,
                    account=draft.account,
                ),
            ),
            dict,
        )

        if draft.product_type is not None:
            previous_auto, _ = cls._auto_map_attributes(
                product_type=draft.product_type,
                facts_document=previous_facts,
                category_mapping=draft.category_mapping,
            )
            current_auto, _ = cls._auto_map_attributes(
                product_type=draft.product_type,
                facts_document=current_facts,
                category_mapping=draft.category_mapping,
            )

            # Compliance attributes (ТН ВЭД / признак маркировки) are not
            # source-fact-derived: `apply_to_attributes` inside
            # `_auto_map_attributes` ignores `facts_document` entirely and
            # only reads the live admin decision, so `previous_auto` and
            # `current_auto` always carry the SAME (most recent) value for
            # these two IDs regardless of which facts snapshot was passed
            # in. Feeding them into the three-way "still equals previous
            # default -> follow current" comparison below is meaningless for
            # them and, when a stored record is absent, would let an
            # unrelated facts-triggered rebase silently invent a compliance
            # attribute that no admin/seller action asked for in this call.
            # These two IDs are excluded from the rebase decision entirely:
            # a stored record passes through untouched below (it simply
            # never matches `previous_by_identity`), and a missing one is
            # not created here.
            from services.ozon_compliance_defaults import (
                MARKING_ATTRIBUTE_ID,
                TNVED_ATTRIBUTE_ID,
            )
            compliance_ids = {TNVED_ATTRIBUTE_ID, MARKING_ATTRIBUTE_ID}

            def _not_compliance(item: Any) -> bool:
                return not (
                    isinstance(item, dict)
                    and str(item.get("attribute_id")) in compliance_ids
                )

            previous_auto = [
                item for item in previous_auto if _not_compliance(item)
            ]
            current_auto = [
                item for item in current_auto if _not_compliance(item)
            ]

            def identity(item: dict) -> Tuple[Any, Any]:
                return (
                    item.get("attribute_id"),
                    item.get("complex_id", "0"),
                )

            previous_by_identity = {
                identity(item): item
                for item in previous_auto
                if isinstance(item, dict)
            }
            current_by_identity = {
                identity(item): item
                for item in current_auto
                if isinstance(item, dict)
            }
            merged_attributes = []
            occupied = set()
            for item in cls._stored_json(draft.attributes_json, list):
                if not isinstance(item, dict):
                    merged_attributes.append(item)
                    continue
                item_identity = identity(item)
                previous_item = previous_by_identity.get(item_identity)
                if previous_item is not None and item == previous_item:
                    replacement = current_by_identity.get(item_identity)
                    if replacement is not None:
                        merged_attributes.append(replacement)
                        occupied.add(item_identity)
                    continue
                merged_attributes.append(item)
                occupied.add(item_identity)
            for item in current_auto:
                item_identity = identity(item)
                if item_identity not in occupied:
                    merged_attributes.append(item)
                    occupied.add(item_identity)
            draft.attributes_json = cls._canonical_json(
                merged_attributes,
                list,
            )

        draft.source_facts_json = cls._canonical_json(current_facts, dict)
        draft.provenance_json = cls._canonical_json(
            cls._provenance_with_preserved_compliance(draft, provenance), dict,
        )
        draft.source_fact_hash = fact_hash
        draft.validation_status = "stale"
        draft.validation_result_json = cls._canonical_json({
            "publishable": False,
            "errors": [{
                "code": "facts_rebased",
                "field": "source_facts",
                "message": (
                    "Свежие факты применены только к полям без "
                    "пользовательских изменений; нужна повторная проверка"
                ),
            }],
            "warnings": [],
        }, dict)
        draft.validated_at = None
        draft.status = "draft" if draft.product_type_id else "needs_category"
        draft.updated_at = datetime.utcnow()
        try:
            db.session.commit()
        except StaleDataError:
            db.session.rollback()
            raise MarketplaceDraftConflict(
                "Черновик изменился параллельно; повторите после обновления"
            ) from None
        return cls.get_draft(seller_id=seller_id, draft_id=draft_id)

    @classmethod
    def apply_account_defaults(
        cls,
        *,
        seller_id: int,
        draft_id: int,
        expected_version: int,
    ) -> MarketplaceProductDraft:
        """Fill missing safe commercial defaults without overwriting a draft.

        This upgrades drafts created before account-level defaults existed.
        Explicit per-card VAT, currency, price and old price always win.
        """
        expected_version = cls._positive_integer(
            expected_version,
            "expected_version",
        )
        draft = cls.get_draft(seller_id=seller_id, draft_id=draft_id)
        if draft.version != expected_version:
            raise MarketplaceDraftConflict(
                "Черновик изменился; обновите страницу и повторите"
            )
        current = cls._stored_json(draft.commercial_json, dict)
        merged = dict(current)
        for key, value in cls._commercial_defaults(draft.account).items():
            if merged.get(key) in (None, ""):
                merged[key] = value
        if merged == current:
            return draft
        return cls.update_draft(
            seller_id=seller_id,
            draft_id=draft_id,
            expected_version=expected_version,
            patch={"commercial": merged},
        )

    @classmethod
    def apply_reference_defaults(
        cls,
        *,
        seller_id: int,
        draft_id: int,
        expected_version: int,
    ) -> MarketplaceProductDraft:
        """Fill newly-known exact attributes without overwriting seller input.

        A seller may bind an official type before its on-demand Ozon schema is
        cached.  Once the read-only refresh completes, the bulk worker calls
        this method to add only deterministic source-backed flat attributes
        whose exact ``attribute_id/complex_id`` pair is still absent. Existing
        values, content and complex groups always win.
        """
        expected_version = cls._positive_integer(
            expected_version,
            "expected_version",
        )
        draft = cls.get_draft(seller_id=seller_id, draft_id=draft_id)
        if draft.version != expected_version:
            raise MarketplaceDraftConflict(
                "Черновик изменился; обновите страницу и повторите"
            )
        if (
            draft.product_type is None
            or not OzonReferenceService.reference_is_fresh(
                draft.product_type
            )
        ):
            return draft

        current = cls._stored_json(draft.attributes_json, list)
        identities = {
            (
                item.get("attribute_id"),
                item.get("complex_id", "0"),
            )
            for item in current
            if isinstance(item, dict)
        }
        generated, compliance_report = cls._auto_map_attributes(
            product_type=draft.product_type,
            facts_document=cls._stored_json(
                draft.source_facts_json,
                dict,
            ),
            category_mapping=draft.category_mapping,
        )
        additions = [
            item
            for item in generated
            if (
                item.get("attribute_id"),
                item.get("complex_id", "0"),
            ) not in identities
        ]
        if not additions:
            return draft

        # `_auto_map_attributes` recomputes the compliance layer from
        # scratch on every call, independent of what `current` already
        # holds -- its `compliance_report["applied"]` therefore always lists
        # a resolvable TNVED/marking ID even when that identity is already
        # present in `current` (and thus excluded from `additions` above,
        # left untouched here). Attributing provenance for an ID that was
        # NOT actually just written would falsely claim a fresh write over
        # an untouched, possibly-stale stored value and point provenance at
        # the wrong (current, not stored) resolved value. Only IDs that
        # genuinely ended up in `additions` in THIS call may be recorded.
        added_compliance_ids = {
            item.get("attribute_id")
            for item in additions
            if isinstance(item, dict)
        }
        filtered_report = dict(compliance_report or {})
        filtered_report["applied"] = [
            attribute_id
            for attribute_id in (compliance_report or {}).get("applied") or []
            if attribute_id in added_compliance_ids
        ]

        updated = cls.update_draft(
            seller_id=seller_id,
            draft_id=draft.id,
            expected_version=expected_version,
            patch={"attributes": [*current, *additions]},
        )
        if filtered_report["applied"]:
            cls._merge_compliance_provenance(updated, filtered_report)
            try:
                db.session.commit()
            except StaleDataError:
                db.session.rollback()
                raise MarketplaceDraftConflict(
                    "Черновик изменился параллельно; повторите после обновления"
                ) from None
            return cls.get_draft(seller_id=seller_id, draft_id=updated.id)
        return updated

    @staticmethod
    def _validation_item(code: str, field: str, message: str) -> dict:
        return {"code": code, "field": field, "message": message}

    @classmethod
    def _attribute_type_error(
        cls,
        attribute: MarketplaceAttributeDefinition,
        value: str,
    ) -> Optional[str]:
        data_type = cls._normalized_text(attribute.data_type)
        if data_type not in cls.DATA_TYPES:
            return "unsupported"
        if data_type == "string":
            return None if value.strip() else "string"
        if data_type == "integer":
            return None if re.fullmatch(r"-?(?:0|[1-9]\d*)", value) else "integer"
        if data_type == "decimal":
            return None if re.fullmatch(
                r"-?(?:0|[1-9]\d*)(?:\.\d+)?", value
            ) else "decimal"
        if data_type == "boolean":
            return None if value in {"true", "false"} else "boolean"
        return "unsupported"

    @classmethod
    def _validate_attributes(
        cls,
        *,
        product_type: MarketplaceProductType,
        attributes: list,
        complex_groups: list,
        errors: list,
        implicitly_supplied: Optional[set] = None,
    ) -> None:
        implicitly_supplied = implicitly_supplied or set()
        definitions = MarketplaceAttributeDefinition.query.filter_by(
            product_type_id=product_type.id,
            is_available=True,
        ).all()
        definitions_by_external = {
            item.external_attribute_id: item for item in definitions
        }
        occurrences: Dict[str, int] = {}
        supplied: List[Tuple[dict, MarketplaceAttributeDefinition, str]] = []

        def collect(items: Any, container: str, group_index: Optional[int] = None) -> None:
            if not isinstance(items, list):
                errors.append(cls._validation_item(
                    "malformed_attributes", container, "Атрибуты повреждены"
                ))
                return
            seen = set()
            for index, item in enumerate(items):
                path = f"{container}[{index}]"
                if not isinstance(item, dict):
                    errors.append(cls._validation_item(
                        "malformed_attribute", path, "Атрибут должен быть объектом"
                    ))
                    continue
                external_id = item.get("attribute_id")
                complex_id = item.get("complex_id", "0")
                identity = (external_id, complex_id)
                if identity in seen:
                    errors.append(cls._validation_item(
                        "duplicate_attribute", path, "Атрибут продублирован в группе"
                    ))
                    continue
                seen.add(identity)
                definition = definitions_by_external.get(external_id)
                if definition is None:
                    errors.append(cls._validation_item(
                        "unknown_attribute", path, "Атрибут отсутствует в текущей Ozon schema"
                    ))
                    continue
                expected_complex = definition.attribute_complex_id or "0"
                if complex_id != expected_complex:
                    errors.append(cls._validation_item(
                        "complex_id_mismatch", path, "complex_id не совпадает со schema"
                    ))
                if container == "attributes" and definition.attribute_complex_id:
                    errors.append(cls._validation_item(
                        "complex_attribute_outside_group", path,
                        "Complex-атрибут должен находиться в complex_attributes",
                    ))
                if container != "attributes" and not definition.attribute_complex_id:
                    errors.append(cls._validation_item(
                        "simple_attribute_inside_group", path,
                        "Обычный атрибут не должен находиться в complex_attributes",
                    ))
                if not definition.is_enabled:
                    errors.append(cls._validation_item(
                        "attribute_disabled", path, "Атрибут отключён администратором"
                    ))
                values = item.get("values")
                if not isinstance(values, list) or not values:
                    errors.append(cls._validation_item(
                        "attribute_values_empty", path, "У атрибута нет значений"
                    ))
                    continue
                if len(values) > cls.MAX_ATTRIBUTE_VALUES:
                    errors.append(cls._validation_item(
                        "attribute_values_limit", path, "Слишком много значений атрибута"
                    ))
                    continue
                schema_max = definition.max_value_count or None
                allowed_count = schema_max
                if not definition.is_collection:
                    allowed_count = min(schema_max, 1) if schema_max else 1
                if allowed_count and len(values) > allowed_count:
                    non_collection_limit = (
                        not definition.is_collection
                        and (schema_max is None or schema_max > 1)
                    )
                    code = (
                        "attribute_not_collection"
                        if non_collection_limit
                        else "attribute_max_value_count"
                    )
                    error = cls._validation_item(
                        code,
                        f"{path}.values",
                        f"Атрибут «{definition.name}» (ID {external_id}): "
                        f"передано значений {len(values)}; допустимо не более "
                        f"{allowed_count}",
                    )
                    error.update({
                        "attribute_id": external_id,
                        "attribute_name": definition.name,
                        "actual_count": len(values),
                        "max_value_count": allowed_count,
                    })
                    if schema_max is not None:
                        error["schema_max_value_count"] = schema_max
                    errors.append(error)
                occurrences[external_id] = occurrences.get(external_id, 0) + 1
                supplied.append((item, definition, path))

        collect(attributes, "attributes")
        if not isinstance(complex_groups, list):
            errors.append(cls._validation_item(
                "malformed_complex_attributes", "complex_attributes",
                "Complex-атрибуты повреждены",
            ))
        else:
            for group_index, group in enumerate(complex_groups):
                path = f"complex_attributes[{group_index}]"
                if not isinstance(group, dict) or not isinstance(group.get("attributes"), list):
                    errors.append(cls._validation_item(
                        "malformed_complex_group", path,
                        "Complex-группа должна содержать attributes",
                    ))
                    continue
                collect(group["attributes"], f"{path}.attributes", group_index)

        for definition in definitions:
            if definition.is_required and occurrences.get(
                definition.external_attribute_id, 0
            ) == 0 and definition.external_attribute_id not in implicitly_supplied:
                errors.append(cls._validation_item(
                    "required_attribute_missing",
                    f"attributes.{definition.external_attribute_id}",
                    f"Обязательный атрибут «{definition.name}» не заполнен",
                ))
            if (
                definition.attribute_complex_id
                and not definition.complex_is_collection
                and occurrences.get(definition.external_attribute_id, 0) > 1
            ):
                errors.append(cls._validation_item(
                    "complex_attribute_repeated",
                    f"attributes.{definition.external_attribute_id}",
                    f"Complex-атрибут «{definition.name}» не является коллекцией",
                ))
            if (
                definition.is_required
                and definition.dictionary_id
                and not OzonReferenceService.dictionary_is_fresh(definition)
            ):
                errors.append(cls._validation_item(
                    "dictionary_stale",
                    f"attributes.{definition.external_attribute_id}",
                    f"Справочник «{definition.name}» устарел или не синхронизирован",
                ))

        dictionary_requests: Dict[int, set] = {}
        for item, definition, path in supplied:
            if definition.dictionary_id:
                if not OzonReferenceService.dictionary_is_fresh(definition):
                    errors.append(cls._validation_item(
                        "dictionary_stale", path,
                        f"Справочник «{definition.name}» устарел или не синхронизирован",
                    ))
                    continue
                for value in item.get("values", []):
                    if not isinstance(value, dict):
                        continue
                    external_value_id = value.get("dictionary_value_id")
                    if not isinstance(external_value_id, str):
                        errors.append(cls._validation_item(
                            "dictionary_value_id_missing", path,
                            f"«{definition.name}» требует exact dictionary_value_id",
                        ))
                        continue
                    dictionary_requests.setdefault(definition.id, set()).add(
                        external_value_id
                    )
            else:
                for value in item.get("values", []):
                    if not isinstance(value, dict):
                        errors.append(cls._validation_item(
                            "malformed_attribute_value", path,
                            "Значение атрибута должно быть объектом",
                        ))
                        continue
                    if value.get("dictionary_value_id") not in (None, ""):
                        errors.append(cls._validation_item(
                            "unexpected_dictionary_value_id", path,
                            f"«{definition.name}» не является справочником",
                        ))
                    raw_value = value.get("value")
                    if not isinstance(raw_value, str):
                        errors.append(cls._validation_item(
                            "attribute_value_not_string", path,
                            "Ozon attribute value должен быть строкой",
                        ))
                        continue
                    type_error = cls._attribute_type_error(definition, raw_value)
                    if type_error == "unsupported":
                        errors.append(cls._validation_item(
                            "unsupported_attribute_type", path,
                            f"Тип Ozon «{definition.data_type}» не поддержан валидатором",
                        ))
                    elif type_error:
                        errors.append(cls._validation_item(
                            "attribute_type_mismatch", path,
                            f"«{definition.name}» ожидает {definition.data_type}",
                        ))

        resolved: Dict[Tuple[int, str], MarketplaceAttributeValue] = {}
        for attribute_id, ids in dictionary_requests.items():
            for offset in range(0, len(ids), 500):
                chunk = list(ids)[offset:offset + 500]
                rows = MarketplaceAttributeValue.query.filter(
                    MarketplaceAttributeValue.attribute_id == attribute_id,
                    MarketplaceAttributeValue.external_value_id.in_(chunk),
                    MarketplaceAttributeValue.is_available.is_(True),
                ).all()
                for row in rows:
                    resolved[(attribute_id, row.external_value_id)] = row

        for item, definition, path in supplied:
            if not definition.dictionary_id:
                continue
            restriction = set(definition.restriction_value_ids)
            for value in item.get("values", []):
                if not isinstance(value, dict):
                    continue
                external_id = value.get("dictionary_value_id")
                if not isinstance(external_id, str):
                    continue
                row = resolved.get((definition.id, external_id))
                if row is None:
                    errors.append(cls._validation_item(
                        "dictionary_value_out_of_scope", path,
                        f"Значение отсутствует в справочнике «{definition.name}» этого типа",
                    ))
                    continue
                if restriction and external_id not in restriction:
                    errors.append(cls._validation_item(
                        "dictionary_value_restricted", path,
                        f"Значение запрещено admin allowlist для «{definition.name}»",
                    ))
                if value.get("value") != row.value:
                    errors.append(cls._validation_item(
                        "dictionary_display_mismatch", path,
                        f"Display value для «{definition.name}» не совпадает с official value",
                    ))

    @classmethod
    def _build_validation_result(
        cls,
        draft: MarketplaceProductDraft,
    ) -> dict:
        errors: List[dict] = []
        warnings: List[dict] = []
        now = datetime.utcnow()

        if (
            draft.account.seller_id != draft.seller_id
            or draft.account.marketplace_id != draft.marketplace_id
            or draft.marketplace.code != "ozon"
        ):
            errors.append(cls._validation_item(
                "account_scope_mismatch", "account_id",
                "Кабинет не совпадает с seller/marketplace scope черновика",
            ))
        if not draft.account.is_active:
            errors.append(cls._validation_item(
                "account_inactive", "account_id", "Кабинет Ozon отключён"
            ))
        if draft.account.connection_status != "connected":
            errors.append(cls._validation_item(
                "account_not_connected", "account_id",
                "Кабинет Ozon должен пройти проверку подключения",
            ))
        if not draft.account.has_credentials:
            errors.append(cls._validation_item(
                "account_credentials_missing", "account_id",
                "В кабинете Ozon отсутствует API key",
            ))
        if (
            draft.account.credential_expires_at
            and draft.account.credential_expires_at <= now
        ):
            errors.append(cls._validation_item(
                "account_credentials_expired", "account_id",
                "Срок действия Ozon credentials истёк",
            ))

        try:
            current_pack = MarketplaceFactPackBuilder.build(draft.imported_product)
            if current_pack["fact_hash"] != draft.source_fact_hash:
                errors.append(cls._validation_item(
                    "source_facts_stale", "source_facts",
                    "Исходный товар изменился; обновите fact snapshot",
                ))
        except MarketplaceFactPackError:
            errors.append(cls._validation_item(
                "source_facts_unavailable", "source_facts",
                "Не удалось повторно проверить исходные факты",
            ))

        try:
            wb_projection = MarketplaceFactPackBuilder.wb_projection_drift(
                draft.imported_product
            )
        except MarketplaceFactPackError:
            wb_projection = {
                "linked": False,
                "differing_fields": [],
            }
        if wb_projection.get("differing_fields"):
            warnings.append(cls._validation_item(
                "wb_projection_differs_from_canonical",
                "source_facts",
                "Связанная WB-карточка отличается в общих полях: "
                + ", ".join(wb_projection["differing_fields"])
                + ". Ozon-черновик использует общую внутреннюю карточку; "
                "сначала проверьте diff, если нужна именно версия WB.",
            ))

        facts_document = cls._stored_json(draft.source_facts_json, dict)
        suggestions = facts_document.get("unverified_suggestions", {})
        if isinstance(suggestions, dict) and suggestions:
            warnings.append(cls._validation_item(
                "unverified_ai_suggestions_ignored", "source_facts",
                "Неподтверждённые legacy AI-предложения не включены автоматически",
            ))

        if not draft.offer_id:
            errors.append(cls._validation_item(
                "offer_id_required", "offer_id",
                "offer_id обязателен в /v3/product/import с 10.07.2026",
            ))
        elif len(draft.offer_id) > cls.MAX_OZON_OFFER_ID_CHARS:
            errors.append(cls._validation_item(
                "offer_id_too_long", "offer_id",
                f"offer_id длиннее {cls.MAX_OZON_OFFER_ID_CHARS} символов для /v3/product/import",
            ))
        listing = MarketplaceListing.query.filter_by(
            account_id=draft.account_id,
            offer_id=draft.offer_id,
        ).first()
        if listing and listing.id != draft.published_listing_id:
            errors.append(cls._validation_item(
                "offer_id_already_published", "offer_id",
                "offer_id уже принадлежит другой опубликованной карточке",
            ))

        product_type = draft.product_type
        if product_type is None:
            errors.append(cls._validation_item(
                "product_type_required", "product_type_id",
                "Выберите точную пару Ozon category/type",
            ))
        elif (
            product_type.marketplace_id != draft.marketplace_id
            or not product_type.is_seller_selectable
            or not product_type.is_available
            or not product_type.category
            or not product_type.category.is_available
        ):
            errors.append(cls._validation_item(
                "product_type_unavailable", "product_type_id",
                "Выбранный Ozon product type недоступен",
            ))
        elif (
            draft.external_category_id != product_type.category.external_category_id
            or draft.external_type_id != product_type.external_type_id
        ):
            errors.append(cls._validation_item(
                "product_type_identity_mismatch", "product_type_id",
                "Сохранённая category/type identity не совпадает со schema",
            ))
        elif not OzonReferenceService.reference_is_fresh(product_type, now=now):
            errors.append(cls._validation_item(
                "schema_stale", "product_type_id",
                "Ozon category/attribute schema старше hard TTL или неполна",
            ))

        update_baseline = None
        try:
            effective_documents, update_baseline = cls.publication_documents(
                draft,
                now=now,
            )
        except MarketplaceDraftError as exc:
            effective_documents = cls._stored_draft_documents(draft)
            errors.append(cls._validation_item(
                getattr(
                    exc,
                    "code",
                    "ozon_listing_state_not_reconstructable",
                ),
                "published_listing_id",
                str(exc),
            ))

        content = effective_documents["content"]
        attributes = effective_documents["attributes"]
        complex_groups = effective_documents["complex_attributes"]
        implicit_attribute_ids = set()
        forbidden_brand = cls._forbidden_draft_brand(
            draft,
            attributes=attributes,
            complex_groups=complex_groups,
        )
        if forbidden_brand is not None:
            errors.append(cls._validation_item(
                "ozon_brand_forbidden",
                "brand",
                (
                    f"Бренд «{forbidden_brand}» запрещён для загрузки "
                    "на Ozon. Исправьте исходный бренд или исключите товар "
                    "из синхронизации"
                ),
            ))
        if not isinstance(content.get("name"), str) or not content.get("name", "").strip():
            errors.append(cls._validation_item(
                "name_required", "content.name", "Название товара обязательно"
            ))
        elif len(content["name"]) > 500:
            errors.append(cls._validation_item(
                "name_too_long", "content.name", "Название длиннее 500 символов"
            ))
        description_value = content.get("description")
        if (
            not isinstance(description_value, str)
            or not description_value.strip()
        ):
            errors.append(cls._validation_item(
                "description_required", "content.description",
                "Описание товара обязательно",
            ))

        if product_type is not None:
            description_definitions = MarketplaceAttributeDefinition.query.filter_by(
                product_type_id=product_type.id,
                external_attribute_id=cls.OZON_DESCRIPTION_ATTRIBUTE_ID,
                is_available=True,
                is_enabled=True,
            ).all()
            if len(description_definitions) != 1:
                errors.append(cls._validation_item(
                    "description_attribute_unavailable",
                    "content.description",
                    "Fresh Ozon schema должен содержать один enabled атрибут описания 4191",
                ))
            elif (
                description_definitions[0].dictionary_id
                or description_definitions[0].attribute_complex_id
            ):
                errors.append(cls._validation_item(
                    "description_attribute_unsupported",
                    "content.description",
                    "Атрибут описания 4191 имеет неподдерживаемую Ozon schema",
                ))
            elif isinstance(description_value, str) and description_value.strip():
                implicit_attribute_ids.add(cls.OZON_DESCRIPTION_ATTRIBUTE_ID)

        description_attributes = [
            item for item in attributes
            if isinstance(item, dict)
            and item.get("attribute_id") == cls.OZON_DESCRIPTION_ATTRIBUTE_ID
        ]
        if len(description_attributes) > 1:
            errors.append(cls._validation_item(
                "description_attribute_duplicated",
                "attributes.4191",
                "Атрибут описания 4191 продублирован",
            ))
        elif description_attributes and isinstance(description_value, str):
            values = description_attributes[0].get("values")
            if (
                not isinstance(values, list)
                or len(values) != 1
                or not isinstance(values[0], dict)
                or values[0].get("dictionary_value_id") not in (None, "")
                or not isinstance(values[0].get("value"), str)
                or values[0]["value"].strip() != description_value.strip()
            ):
                errors.append(cls._validation_item(
                    "description_attribute_conflict",
                    "attributes.4191",
                    "Атрибут 4191 должен точно совпадать с content.description",
                ))

        media = effective_documents["media"]
        images = media.get("images") if isinstance(media, dict) else None
        primary_image = media.get("primary_image") if isinstance(media, dict) else None
        color_image = media.get("color_image") if isinstance(media, dict) else None
        if not isinstance(images, list) or (not images and not primary_image):
            errors.append(cls._validation_item(
                "images_required", "media", "Нужна хотя бы одна основная фотография"
            ))
        else:
            maximum_images = cls.MAX_IMAGES - (1 if primary_image else 0)
            if len(images) > maximum_images:
                errors.append(cls._validation_item(
                    "images_limit", "media.images",
                    f"Допустимо не более {maximum_images} фотографий в images",
                ))
            seen_images = set()
            checked_images = []
            if primary_image is not None:
                checked_images.append(("media.primary_image", primary_image))
            checked_images.extend(
                (f"media.images[{index}]", value)
                for index, value in enumerate(images)
            )
            if color_image is not None:
                checked_images.append(("media.color_image", color_image))
            for field, value in checked_images:
                if not isinstance(value, str) or len(value) > 2_000:
                    errors.append(cls._validation_item(
                        "image_url_invalid", field, "Некорректный URL изображения"
                    ))
                    continue
                parsed = urlsplit(value)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    errors.append(cls._validation_item(
                        "image_url_not_public", field,
                        "Ozon требует публичный HTTP(S) URL изображения",
                    ))
                if value in seen_images:
                    errors.append(cls._validation_item(
                        "image_duplicate", field, "URL изображения продублирован"
                    ))
                seen_images.add(value)
            if "images360" in media:
                errors.append(cls._validation_item(
                    "images360_removed", "media.images360",
                    "images360 удалён из /v3/product/import 10.07.2026",
                ))

        dimensions = effective_documents["dimensions"]
        for field_name in ("width", "height", "depth", "weight"):
            try:
                rendered = cls._decimal(
                    dimensions.get(field_name),
                    f"dimensions.{field_name}",
                    positive=True,
                )
                parsed = Decimal(rendered)
                if parsed != parsed.to_integral_value():
                    errors.append(cls._validation_item(
                        "physical_fact_not_integer",
                        f"dimensions.{field_name}",
                        f"{field_name} должен быть целым числом в выбранной единице Ozon",
                    ))
                elif parsed > cls.MAX_OZON_API_PHYSICAL_INTEGER:
                    errors.append(cls._validation_item(
                        "physical_fact_out_of_range",
                        f"dimensions.{field_name}",
                        f"{field_name} превышает максимум целого числа Ozon API",
                    ))
            except MarketplaceDraftValidationError:
                errors.append(cls._validation_item(
                    "physical_fact_required",
                    f"dimensions.{field_name}",
                    f"{field_name} должен быть подтверждённым положительным значением",
                ))
        if dimensions.get("dimension_unit") not in cls.DIMENSION_UNITS:
            errors.append(cls._validation_item(
                "dimension_unit_required", "dimensions.dimension_unit",
                "Укажите поддерживаемую Ozon единицу габаритов",
            ))
        if dimensions.get("weight_unit") not in cls.WEIGHT_UNITS:
            errors.append(cls._validation_item(
                "weight_unit_required", "dimensions.weight_unit",
                "Укажите поддерживаемую Ozon единицу веса",
            ))

        barcodes = effective_documents["barcodes"]
        if len(barcodes) > cls.MAX_IMPORT_BARCODES:
            errors.append(cls._validation_item(
                "barcodes_limit", "barcodes",
                "/v3/product/import принимает один штрихкод; дополнительные добавляются отдельным workflow",
            ))
        seen_barcodes = set()
        for index, barcode in enumerate(barcodes):
            if (
                not isinstance(barcode, str)
                or not barcode.strip()
                or len(barcode) > 100
            ):
                errors.append(cls._validation_item(
                    "barcode_invalid", f"barcodes[{index}]", "Некорректный штрихкод"
                ))
            elif barcode in seen_barcodes:
                errors.append(cls._validation_item(
                    "barcode_duplicate", f"barcodes[{index}]", "Штрихкод продублирован"
                ))
            seen_barcodes.add(barcode)

        commercial = effective_documents["commercial"]
        price = None
        try:
            price = Decimal(cls._decimal(
                commercial.get("price"), "commercial.price", positive=True
            ))
        except MarketplaceDraftValidationError:
            errors.append(cls._validation_item(
                "price_required", "commercial.price",
                "Цена продажи должна быть явно рассчитана и положительна",
            ))
        if commercial.get("old_price") not in (None, ""):
            try:
                old_price = Decimal(cls._decimal(
                    commercial["old_price"], "commercial.old_price", positive=True
                ))
                if price is not None and old_price <= price:
                    errors.append(cls._validation_item(
                        "old_price_not_greater", "commercial.old_price",
                        "old_price должен быть больше price",
                    ))
            except MarketplaceDraftValidationError:
                errors.append(cls._validation_item(
                    "old_price_invalid", "commercial.old_price",
                    "old_price должен быть положительным числом",
                ))
        if commercial.get("vat") not in cls.VAT_VALUES:
            errors.append(cls._validation_item(
                "vat_required", "commercial.vat",
                "Выберите явную ставку НДС из поддерживаемого Ozon enum",
            ))
        if commercial.get("currency_code") not in cls.CURRENCY_CODES:
            errors.append(cls._validation_item(
                "currency_code_required", "commercial.currency_code",
                "Укажите явный currency_code=RUB для текущего rollout",
            ))

        if product_type and OzonReferenceService.reference_is_fresh(product_type, now=now):
            cls._validate_attributes(
                product_type=product_type,
                attributes=attributes,
                complex_groups=complex_groups,
                errors=errors,
                implicitly_supplied=implicit_attribute_ids,
            )

        if len(errors) > cls.MAX_VALIDATION_ITEMS:
            errors = errors[: cls.MAX_VALIDATION_ITEMS]
            errors.append(cls._validation_item(
                "validation_items_truncated", "draft",
                "Список ошибок обрезан safety limit",
            ))
        result = {
            "version": 1,
            "marketplace": "ozon",
            "publishable": not errors,
            "errors": errors,
            "warnings": warnings,
            "schema": {
                "product_type_id": product_type.id if product_type else None,
                "external_category_id": (
                    product_type.category.external_category_id
                    if product_type and product_type.category else None
                ),
                "external_type_id": (
                    product_type.external_type_id if product_type else None
                ),
                "version": product_type.attributes_version if product_type else None,
                "hash": product_type.attributes_schema_hash if product_type else None,
                "fresh": bool(
                    product_type
                    and OzonReferenceService.reference_is_fresh(product_type, now=now)
                ),
            },
            "validated_at": now.isoformat(),
        }
        if update_baseline is not None:
            result["update_baseline"] = update_baseline
        return result

    BULK_PREPARE_MAX_PRODUCTS = 200

    @classmethod
    def bulk_prepare(
        cls,
        *,
        seller_id: int,
        account_id: int,
        imported_product_ids: Sequence[int],
        validate: bool = False,
        corrected_by_user_id: Optional[int] = None,
    ) -> dict:
        """Детерминированная bulk-подготовка черновиков одного owned кабинета.

        Локальный путь без provider/LLM: для каждого товара создаётся
        отсутствующий draft, существующие считаются отдельно, ошибка одного
        товара не блокирует остальные. Неудачная validation уже созданного
        черновика не считается failed — черновик сохранён и остаётся
        доступным для доводки на /marketplaces/drafts/.
        """
        # Fail-closed на уровне сервиса: kill-switch Ozon гейтит создание
        # черновиков и для вызовов в обход UI/route (например, supplier import
        # с draft_account_ids в form body при выключенном флаге).
        if not current_app.config.get("MARKETPLACE_OZON_ENABLED", False):
            raise MarketplaceDraftValidationError(
                "Черновики Ozon отключены feature flag"
            )
        seller_id = cls._positive_integer(seller_id, "seller_id")
        validate = cls._strict_boolean(validate, "validate")
        if not isinstance(imported_product_ids, (list, tuple)):
            raise MarketplaceDraftValidationError(
                "imported_product_ids должен быть списком"
            )
        if not imported_product_ids:
            raise MarketplaceDraftValidationError(
                "imported_product_ids обязателен и должен быть непустым списком"
            )
        if len(imported_product_ids) > cls.BULK_PREPARE_MAX_PRODUCTS:
            raise MarketplaceDraftValidationError(
                f"За один запрос можно подготовить не больше "
                f"{cls.BULK_PREPARE_MAX_PRODUCTS} товаров"
            )
        product_ids: List[int] = []
        seen_ids: set = set()
        for raw in imported_product_ids:
            parsed = cls._positive_integer(raw, "imported_product_id")
            if parsed in seen_ids:
                raise MarketplaceDraftValidationError(
                    "imported_product_ids содержит дубликаты"
                )
            seen_ids.add(parsed)
            product_ids.append(parsed)

        # Tenant scope: кабинет и весь exact-set товаров текущего продавца.
        # is_active проверяется здесь, чтобы неактивный кабинет давал один
        # быстрый отказ, а не N per-item ошибок.
        account = cls._owned_account(seller_id=seller_id, account_id=account_id)
        if not account.is_active:
            raise MarketplaceDraftValidationError(
                "Кабинет Ozon отключён — черновики для него не создаются"
            )
        owned_ids = {
            row[0]
            for row in db.session.query(ImportedProduct.id).filter(
                ImportedProduct.seller_id == seller_id,
                ImportedProduct.id.in_(product_ids),
            ).all()
        }
        if owned_ids != seen_ids:
            raise MarketplaceDraftValidationError(
                "Выбранные товары не принадлежат текущему продавцу"
            )

        existing_ids = {
            row[0]
            for row in db.session.query(
                MarketplaceProductDraft.imported_product_id
            ).filter(
                MarketplaceProductDraft.seller_id == seller_id,
                MarketplaceProductDraft.account_id == account.id,
                MarketplaceProductDraft.imported_product_id.in_(product_ids),
            ).all()
        }
        created = existing = failed = 0
        for product_id in product_ids:
            if product_id in existing_ids:
                existing += 1
                continue
            try:
                draft = cls.create_draft(
                    seller_id=seller_id,
                    account_id=account.id,
                    imported_product_id=product_id,
                    corrected_by_user_id=corrected_by_user_id,
                )
                created += 1
                if validate:
                    try:
                        cls.validate_draft(
                            seller_id=seller_id,
                            draft_id=draft.id,
                            expected_version=draft.version,
                        )
                    except Exception:
                        # Черновик уже создан и закоммичен: любая ошибка
                        # validation не откатывает его и не считается failed —
                        # иначе товар попадёт и в created, и в failed.
                        db.session.rollback()
                        logger.exception(
                            "bulk-prepare validate failed seller_id=%s draft_id=%s",
                            seller_id,
                            draft.id,
                        )
            except MarketplaceDraftError:
                db.session.rollback()
                failed += 1
            except Exception:
                db.session.rollback()
                logger.exception(
                    "bulk-prepare draft failed seller_id=%s product_id=%s",
                    seller_id,
                    product_id,
                )
                failed += 1
        return {
            "created": created,
            "existing": existing,
            "failed": failed,
            "total": len(product_ids),
        }

    @classmethod
    def validate_draft(
        cls,
        *,
        seller_id: int,
        draft_id: int,
        expected_version: int,
    ) -> MarketplaceProductDraft:
        expected_version = cls._positive_integer(expected_version, "expected_version")
        draft = cls.get_draft(seller_id=seller_id, draft_id=draft_id)
        if draft.version != expected_version:
            raise MarketplaceDraftConflict(
                "Черновик изменился; обновите страницу и повторите"
            )
        if draft.status == "archived":
            raise MarketplaceDraftConflict(
                "Архивный черновик нельзя валидировать до восстановления listing"
            )
        cls._assert_no_active_publication(draft)
        result = cls._build_validation_result(draft)
        draft.validation_result_json = cls._canonical_json(result, dict)
        draft.validation_status = "valid" if result["publishable"] else "invalid"
        if draft.product_type:
            draft.schema_version = draft.product_type.attributes_version
            draft.schema_hash = draft.product_type.attributes_schema_hash
        else:
            draft.schema_version = None
            draft.schema_hash = None
        draft.validated_at = datetime.utcnow()
        draft.status = (
            "ready"
            if result["publishable"]
            else "needs_category" if draft.product_type is None else "blocked"
        )
        try:
            db.session.commit()
        except StaleDataError:
            db.session.rollback()
            raise MarketplaceDraftConflict(
                "Черновик изменился параллельно; повторите после обновления"
            ) from None
        return cls.get_draft(seller_id=seller_id, draft_id=draft_id)
