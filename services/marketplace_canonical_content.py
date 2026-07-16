"""Reviewed Ozon observations -> canonical common-content proposals.

This is deliberately not a marketplace round-trip.  Only title and
description can cross the boundary, and only after a fresh exact-account read,
human review, an optimistic drift preflight and a local snapshot.  Category,
attribute/dictionary IDs, price, stock and media never enter this contract.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256
import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from sqlalchemy.orm.exc import StaleDataError

from models import (
    AgentChangeSnapshot,
    ImportedProduct,
    MarketplaceCanonicalContentProposal,
    MarketplaceListing,
    Seller,
    db,
)
from services.marketplace_operation_locks import (
    release_account_operation_lock,
    try_account_operation_lock,
)


class MarketplaceCanonicalContentError(RuntimeError):
    status_code = 400
    code = "marketplace_canonical_content_error"

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        listing_id: Optional[int] = None,
        proposal_id: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        if code:
            self.code = code
        self.listing_id = listing_id
        self.proposal_id = proposal_id


class MarketplaceCanonicalContentValidationError(
    MarketplaceCanonicalContentError
):
    status_code = 400
    code = "invalid_marketplace_canonical_content"


class MarketplaceCanonicalContentNotFound(MarketplaceCanonicalContentError):
    status_code = 404
    code = "marketplace_canonical_content_not_found"


class MarketplaceCanonicalContentConflict(MarketplaceCanonicalContentError):
    status_code = 409
    code = "marketplace_canonical_content_conflict"


class MarketplaceCanonicalContentService:
    """Own the complete local proposal/apply/rollback lifecycle."""

    CONTRACT_VERSION = "ozon-canonical-common-content-v1"
    SUPPORTED_FIELDS = ("title", "description")
    FIELD_LIMITS = {"title": 500, "description": 50_000}
    HARD_TTL = timedelta(hours=48)

    @staticmethod
    def _positive_integer(value: Any, field_name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise MarketplaceCanonicalContentValidationError(
                f"{field_name} должен быть положительным целым числом"
            )
        return value

    @staticmethod
    def _stable_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def _fingerprint(cls, value: Any) -> str:
        return sha256(cls._stable_json(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _comparison_key(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return " ".join(value.split()) or None

    @classmethod
    def _canonical_value(
        cls,
        value: Any,
        field_name: str,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        if value is None:
            return True, None, None
        if not isinstance(value, str):
            return False, None, "canonical_value_not_text"
        if len(value) > cls.FIELD_LIMITS[field_name]:
            return False, None, "canonical_value_too_large"
        if any(
            ord(character) < 32 and character not in "\n\t"
            for character in value
        ) or any(ord(character) == 127 for character in value):
            return False, None, "canonical_value_has_control_chars"
        return True, value, None

    @classmethod
    def _observed_value(
        cls,
        value: Any,
        field_name: str,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        if value in (None, ""):
            return True, None, "source_value_empty"
        if not isinstance(value, str):
            return False, None, "source_value_not_text"
        value = value.strip()
        if not value:
            return True, None, "source_value_empty"
        if len(value) > cls.FIELD_LIMITS[field_name]:
            return False, None, "source_value_too_large"
        if any(
            ord(character) < 32 and character not in "\n\t"
            for character in value
        ) or any(ord(character) == 127 for character in value):
            return False, None, "source_value_has_control_chars"
        return True, value, None

    @classmethod
    def _owned_listing(
        cls,
        *,
        seller_id: int,
        listing_id: int,
        require_operational: bool = True,
        require_connected: bool = True,
        require_link: bool = True,
    ) -> MarketplaceListing:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        listing_id = cls._positive_integer(listing_id, "listing_id")
        listing = MarketplaceListing.query.options(
            joinedload(MarketplaceListing.marketplace),
            joinedload(MarketplaceListing.account),
            joinedload(MarketplaceListing.imported_product),
        ).filter_by(id=listing_id, seller_id=seller_id).first()
        if listing is None:
            raise MarketplaceCanonicalContentNotFound(
                "Ozon-листинг не найден",
                listing_id=listing_id,
            )
        if (
            listing.marketplace is None
            or listing.marketplace.code != "ozon"
            or listing.account is None
            or listing.account_id is None
            or listing.account.seller_id != seller_id
            or listing.account.marketplace_id != listing.marketplace_id
        ):
            raise MarketplaceCanonicalContentValidationError(
                "Reverse proposal доступен только для exact seller-owned Ozon account",
                listing_id=listing.id,
            )
        if require_connected and not listing.marketplace.is_active:
            raise MarketplaceCanonicalContentValidationError(
                "Ozon marketplace неактивен",
                listing_id=listing.id,
            )
        if require_connected and not listing.account.is_active:
            raise MarketplaceCanonicalContentValidationError(
                "Кабинет Ozon неактивен",
                listing_id=listing.id,
            )
        if (
            require_connected
            and listing.account.connection_status != "connected"
        ):
            raise MarketplaceCanonicalContentValidationError(
                "Кабинет Ozon не подключён",
                listing_id=listing.id,
            )
        if require_link and (
            listing.canonical_link_status != "linked"
            or listing.imported_product is None
            or listing.imported_product.seller_id != seller_id
        ):
            raise MarketplaceCanonicalContentValidationError(
                "Сначала свяжите Ozon-листинг с общей внутренней карточкой",
                listing_id=listing.id,
            )
        if require_operational and (
            not listing.is_available
            or listing.is_archived
            or listing.normalized_status != "active"
        ):
            raise MarketplaceCanonicalContentValidationError(
                "Reverse proposal недоступен для неактивного или архивного листинга",
                listing_id=listing.id,
            )
        return listing

    @classmethod
    def _owned_proposal(
        cls,
        *,
        seller_id: int,
        proposal_id: int,
    ) -> MarketplaceCanonicalContentProposal:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        proposal_id = cls._positive_integer(proposal_id, "proposal_id")
        proposal = MarketplaceCanonicalContentProposal.query.options(
            joinedload(MarketplaceCanonicalContentProposal.marketplace),
            joinedload(MarketplaceCanonicalContentProposal.account),
            joinedload(MarketplaceCanonicalContentProposal.listing),
            joinedload(MarketplaceCanonicalContentProposal.imported_product),
            joinedload(MarketplaceCanonicalContentProposal.snapshot),
        ).filter_by(id=proposal_id, seller_id=seller_id).first()
        if proposal is None:
            raise MarketplaceCanonicalContentNotFound(
                "Content proposal не найден",
                proposal_id=proposal_id,
            )
        cls._validate_proposal_scope(proposal)
        return proposal

    @classmethod
    def _validate_proposal_scope(
        cls,
        proposal: MarketplaceCanonicalContentProposal,
    ) -> None:
        """Reject a stored row whose denormalized tenant scope is inconsistent.

        The listing -> canonical link is intentionally not checked here because
        it is mutable and apply persists that condition as a review conflict.
        Everything else is immutable ownership/provider grounding.
        """
        marketplace = proposal.marketplace
        account = proposal.account
        listing = proposal.listing
        product = proposal.imported_product
        if (
            marketplace is None
            or marketplace.code != "ozon"
            or account is None
            or account.id != proposal.account_id
            or account.seller_id != proposal.seller_id
            or account.marketplace_id != proposal.marketplace_id
            or listing is None
            or listing.id != proposal.listing_id
            or listing.seller_id != proposal.seller_id
            or listing.account_id != proposal.account_id
            or listing.marketplace_id != proposal.marketplace_id
            or product is None
            or product.id != proposal.imported_product_id
            or product.seller_id != proposal.seller_id
        ):
            raise MarketplaceCanonicalContentConflict(
                "Сохранённый proposal имеет несогласованный seller/account scope",
                code="canonical_content_proposal_scope_corrupt",
                listing_id=proposal.listing_id,
                proposal_id=proposal.id,
            )

    @classmethod
    def _actor(cls, *, seller_id: int, user_id: Any) -> int:
        user_id = cls._positive_integer(user_id, "user_id")
        seller = Seller.query.filter_by(id=seller_id, user_id=user_id).first()
        if seller is None:
            raise MarketplaceCanonicalContentNotFound("Продавец не найден")
        return user_id

    @classmethod
    def _comparison(
        cls,
        listing: MarketplaceListing,
        *,
        now: Optional[datetime] = None,
    ) -> Tuple[Dict[str, Any], Optional[datetime]]:
        now = now or datetime.utcnow()
        timestamps = [listing.info_synced_at, listing.attributes_synced_at]
        source_observed_at = min(timestamps) if all(timestamps) else None
        source_fresh = bool(
            source_observed_at
            and source_observed_at >= now - cls.HARD_TTL
        )
        product = listing.imported_product
        field_rows = []
        for field_name in cls.SUPPORTED_FIELDS:
            canonical_ok, canonical_value, canonical_reason = cls._canonical_value(
                getattr(product, field_name, None),
                field_name,
            )
            source_ok, observed_value, source_reason = cls._observed_value(
                getattr(listing, field_name, None),
                field_name,
            )
            eligible = bool(
                canonical_ok
                and source_ok
                and observed_value is not None
            )
            differs = bool(
                eligible
                and cls._comparison_key(canonical_value)
                != cls._comparison_key(observed_value)
            )
            field_rows.append({
                "field": field_name,
                "canonical_value": canonical_value,
                "ozon_value": observed_value,
                "eligible": eligible,
                "differs": differs,
                "reason": canonical_reason or source_reason,
            })
        differing_fields = [
            row["field"] for row in field_rows if row["differs"]
        ]
        blocked_reasons = []
        if not source_fresh:
            blocked_reasons.append("ozon_content_snapshot_stale")
        if not differing_fields:
            blocked_reasons.append("no_common_content_diff")
        document = {
            "contract_version": cls.CONTRACT_VERSION,
            "listing_id": listing.id,
            "marketplace_code": "ozon",
            "account_id": listing.account_id,
            "imported_product_id": listing.imported_product_id,
            "source_fresh": source_fresh,
            "source_observed_at": (
                source_observed_at.isoformat() if source_observed_at else None
            ),
            "hard_ttl_hours": int(cls.HARD_TTL.total_seconds() // 3600),
            "fields": field_rows,
            "differing_fields": differing_fields,
            "proposal_allowed": not blocked_reasons,
            "blocked_reasons": blocked_reasons,
            "excluded_scopes": [
                "brand_without_exact_semantic_mapping",
                "category_and_product_type_ids",
                "attribute_and_dictionary_value_ids",
                "price_and_stock",
                "media_and_provider_urls",
                "dimensions_barcodes_and_fulfillment",
            ],
            "side_effects": {
                "provider_calls": False,
                "updates_wb": False,
                "updates_ozon": False,
                "updates_canonical_only_after_review": True,
            },
        }
        return document, source_observed_at

    @classmethod
    def comparison(
        cls,
        *,
        seller_id: int,
        listing_id: int,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        listing = cls._owned_listing(
            seller_id=seller_id,
            listing_id=listing_id,
        )
        document, _ = cls._comparison(listing, now=now)
        return document

    @classmethod
    def _normalize_fields(
        cls,
        values: Optional[Iterable[Any]],
        comparison: Dict[str, Any],
    ) -> List[str]:
        available = set(comparison["differing_fields"])
        if values is None:
            normalized = [
                field for field in cls.SUPPORTED_FIELDS if field in available
            ]
        else:
            if not isinstance(values, list) or not 1 <= len(values) <= len(
                cls.SUPPORTED_FIELDS
            ):
                raise MarketplaceCanonicalContentValidationError(
                    "fields должен быть массивом из title/description"
                )
            normalized = []
            for value in values:
                if not isinstance(value, str) or value not in cls.SUPPORTED_FIELDS:
                    raise MarketplaceCanonicalContentValidationError(
                        "fields содержит неподдерживаемое поле"
                    )
                if value in normalized:
                    raise MarketplaceCanonicalContentValidationError(
                        "fields не должен содержать дубли"
                    )
                if value not in available:
                    raise MarketplaceCanonicalContentValidationError(
                        f"Поле {value} не имеет допустимого Ozon diff"
                    )
                normalized.append(value)
        if not normalized:
            raise MarketplaceCanonicalContentValidationError(
                "Нет свежих различий title/description для proposal",
                code="no_common_content_diff",
                listing_id=comparison["listing_id"],
            )
        return normalized

    @classmethod
    def _states(
        cls,
        comparison: Dict[str, Any],
        fields: List[str],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        rows = {row["field"]: row for row in comparison["fields"]}
        return (
            {field: rows[field]["canonical_value"] for field in fields},
            {field: rows[field]["ozon_value"] for field in fields},
        )

    @classmethod
    def _state_fingerprint(
        cls,
        *,
        listing: MarketplaceListing,
        fields: List[str],
        state: Dict[str, Any],
        source: str,
    ) -> str:
        return cls._fingerprint({
            "contract_version": cls.CONTRACT_VERSION,
            "source": source,
            "seller_id": listing.seller_id,
            "account_id": listing.account_id,
            "listing_id": listing.id,
            "offer_id": listing.offer_id,
            "external_product_id": listing.external_product_id,
            "imported_product_id": listing.imported_product_id,
            "fields": fields,
            "state": state,
        })

    @classmethod
    def _commit(cls, *, listing_id: int, proposal_id: Optional[int] = None) -> None:
        try:
            db.session.commit()
        except (IntegrityError, StaleDataError):
            db.session.rollback()
            raise MarketplaceCanonicalContentConflict(
                "Proposal изменился конкурентно; обновите страницу",
                listing_id=listing_id,
                proposal_id=proposal_id,
            ) from None

    @classmethod
    def _conditional_product_update(
        cls,
        *,
        product_id: int,
        seller_id: int,
        expected: Dict[str, Any],
        replacement: Dict[str, Any],
        now: datetime,
    ) -> bool:
        """Atomically replace only the exact reviewed common-content state."""
        if set(expected) != set(replacement) or not expected:
            return False
        filters = [
            ImportedProduct.id == product_id,
            ImportedProduct.seller_id == seller_id,
        ]
        values = {ImportedProduct.updated_at: now}
        for field_name, expected_value in expected.items():
            if field_name not in cls.SUPPORTED_FIELDS:
                return False
            column = getattr(ImportedProduct, field_name)
            filters.append(
                column.is_(None)
                if expected_value is None
                else column == expected_value
            )
            values[column] = replacement[field_name]
        updated = ImportedProduct.query.filter(*filters).update(
            values,
            synchronize_session=False,
        )
        return updated == 1

    @classmethod
    def create_proposal(
        cls,
        *,
        seller_id: int,
        listing_id: int,
        created_by_user_id: int,
        fields: Optional[List[str]] = None,
        now: Optional[datetime] = None,
    ) -> MarketplaceCanonicalContentProposal:
        listing = cls._owned_listing(
            seller_id=seller_id,
            listing_id=listing_id,
        )
        claim = try_account_operation_lock(listing.account_id)
        if claim is None:
            raise MarketplaceCanonicalContentConflict(
                "Кабинет Ozon сейчас изменяется; повторите после завершения операции",
                code="ozon_account_busy",
                listing_id=listing.id,
            )
        try:
            db.session.expire_all()
            return cls._create_proposal_locked(
                seller_id=seller_id,
                listing_id=listing_id,
                created_by_user_id=created_by_user_id,
                fields=fields,
                now=now,
            )
        finally:
            release_account_operation_lock(claim)

    @classmethod
    def _create_proposal_locked(
        cls,
        *,
        seller_id: int,
        listing_id: int,
        created_by_user_id: int,
        fields: Optional[List[str]] = None,
        now: Optional[datetime] = None,
    ) -> MarketplaceCanonicalContentProposal:
        listing = cls._owned_listing(
            seller_id=seller_id,
            listing_id=listing_id,
        )
        actor_id = cls._actor(
            seller_id=listing.seller_id,
            user_id=created_by_user_id,
        )
        now = now or datetime.utcnow()
        comparison, source_observed_at = cls._comparison(listing, now=now)
        if not comparison["source_fresh"] or source_observed_at is None:
            raise MarketplaceCanonicalContentValidationError(
                "Ozon content snapshot старше 48 часов; сначала синхронизируйте каталог",
                code="ozon_content_snapshot_stale",
                listing_id=listing.id,
            )
        selected = cls._normalize_fields(fields, comparison)
        baseline, proposed = cls._states(comparison, selected)
        baseline_fingerprint = cls._state_fingerprint(
            listing=listing,
            fields=selected,
            state=baseline,
            source="canonical",
        )
        source_fingerprint = cls._state_fingerprint(
            listing=listing,
            fields=selected,
            state=proposed,
            source="ozon_observation",
        )
        fields_json = cls._stable_json(selected)
        baseline_json = cls._stable_json(baseline)
        proposed_json = cls._stable_json(proposed)

        existing = MarketplaceCanonicalContentProposal.query.filter_by(
            seller_id=listing.seller_id,
            listing_id=listing.id,
            status="pending_review",
        ).first()
        if existing is not None:
            cls._validate_proposal_scope(existing)
            if (
                existing.contract_version == cls.CONTRACT_VERSION
                and existing.marketplace_id == listing.marketplace_id
                and existing.account_id == listing.account_id
                and existing.imported_product_id == listing.imported_product_id
                and existing.fields_json == fields_json
                and existing.baseline_fingerprint == baseline_fingerprint
                and existing.source_fingerprint == source_fingerprint
            ):
                return existing
            existing.status = "conflict"
            existing.error_code = "proposal_baseline_replaced"
            existing.error_message = (
                "Canonical или Ozon content изменился до review; создан новый proposal"
            )
            db.session.flush()

        proposal = MarketplaceCanonicalContentProposal(
            seller_id=listing.seller_id,
            marketplace_id=listing.marketplace_id,
            account_id=listing.account_id,
            listing_id=listing.id,
            imported_product_id=listing.imported_product_id,
            created_by_user_id=actor_id,
            status="pending_review",
            fields_json=fields_json,
            baseline_state_json=baseline_json,
            proposed_state_json=proposed_json,
            baseline_fingerprint=baseline_fingerprint,
            source_fingerprint=source_fingerprint,
            contract_version=cls.CONTRACT_VERSION,
            source_observed_at=source_observed_at,
            created_at=now,
            updated_at=now,
        )
        db.session.add(proposal)
        cls._commit(listing_id=listing.id)
        return cls._owned_proposal(
            seller_id=listing.seller_id,
            proposal_id=proposal.id,
        )

    @classmethod
    def _proposal_states(
        cls,
        proposal: MarketplaceCanonicalContentProposal,
    ) -> Tuple[List[str], Dict[str, Any], Dict[str, Any]]:
        try:
            fields = json.loads(proposal.fields_json)
            baseline = json.loads(proposal.baseline_state_json)
            proposed = json.loads(proposal.proposed_state_json)
        except (TypeError, json.JSONDecodeError):
            fields, baseline, proposed = None, None, None
        if (
            proposal.contract_version != cls.CONTRACT_VERSION
            or not isinstance(fields, list)
            or not fields
            or any(field not in cls.SUPPORTED_FIELDS for field in fields)
            or not isinstance(baseline, dict)
            or not isinstance(proposed, dict)
            or set(fields) != set(baseline)
            or set(fields) != set(proposed)
            or len(fields) != len(set(fields))
        ):
            raise MarketplaceCanonicalContentConflict(
                "Сохранённый proposal повреждён и не может быть применён",
                code="canonical_content_proposal_corrupt",
                listing_id=proposal.listing_id,
                proposal_id=proposal.id,
            )
        for field in fields:
            canonical_ok, _, _ = cls._canonical_value(baseline[field], field)
            source_ok, source_value, _ = cls._observed_value(
                proposed[field],
                field,
            )
            if not canonical_ok or not source_ok or source_value is None:
                raise MarketplaceCanonicalContentConflict(
                    "Сохранённый proposal нарушает content contract",
                    code="canonical_content_proposal_corrupt",
                    listing_id=proposal.listing_id,
                    proposal_id=proposal.id,
                )
        return fields, baseline, proposed

    @classmethod
    def _persist_conflict(
        cls,
        proposal: MarketplaceCanonicalContentProposal,
        *,
        reviewer_id: int,
        code: str,
        message: str,
        now: datetime,
    ) -> None:
        proposal.status = "conflict"
        proposal.reviewed_by_user_id = reviewer_id
        proposal.reviewed_at = now
        proposal.error_code = code
        proposal.error_message = message[:1000]
        cls._commit(listing_id=proposal.listing_id, proposal_id=proposal.id)
        raise MarketplaceCanonicalContentConflict(
            message,
            code=code,
            listing_id=proposal.listing_id,
            proposal_id=proposal.id,
        )

    @classmethod
    def apply_proposal(
        cls,
        *,
        seller_id: int,
        proposal_id: int,
        expected_version: int,
        reviewed_by_user_id: int,
        note: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> MarketplaceCanonicalContentProposal:
        proposal = cls._owned_proposal(
            seller_id=seller_id,
            proposal_id=proposal_id,
        )
        claim = try_account_operation_lock(proposal.account_id)
        if claim is None:
            raise MarketplaceCanonicalContentConflict(
                "Кабинет Ozon сейчас изменяется; повторите после завершения операции",
                code="ozon_account_busy",
                listing_id=proposal.listing_id,
                proposal_id=proposal.id,
            )
        try:
            db.session.expire_all()
            return cls._apply_proposal_locked(
                seller_id=seller_id,
                proposal_id=proposal_id,
                expected_version=expected_version,
                reviewed_by_user_id=reviewed_by_user_id,
                note=note,
                now=now,
            )
        finally:
            release_account_operation_lock(claim)

    @classmethod
    def _apply_proposal_locked(
        cls,
        *,
        seller_id: int,
        proposal_id: int,
        expected_version: int,
        reviewed_by_user_id: int,
        note: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> MarketplaceCanonicalContentProposal:
        proposal = cls._owned_proposal(
            seller_id=seller_id,
            proposal_id=proposal_id,
        )
        reviewer_id = cls._actor(
            seller_id=proposal.seller_id,
            user_id=reviewed_by_user_id,
        )
        expected_version = cls._positive_integer(
            expected_version,
            "expected_version",
        )
        if proposal.version != expected_version:
            raise MarketplaceCanonicalContentConflict(
                "Proposal уже изменился; обновите страницу",
                listing_id=proposal.listing_id,
                proposal_id=proposal.id,
            )
        if proposal.status != "pending_review":
            raise MarketplaceCanonicalContentConflict(
                "Применить можно только pending_review proposal",
                listing_id=proposal.listing_id,
                proposal_id=proposal.id,
            )
        normalized_note = cls._note(note)
        now = now or datetime.utcnow()
        try:
            listing = cls._owned_listing(
                seller_id=proposal.seller_id,
                listing_id=proposal.listing_id,
            )
        except MarketplaceCanonicalContentError as exc:
            cls._persist_conflict(
                proposal,
                reviewer_id=reviewer_id,
                code="ozon_content_source_unavailable",
                message=str(exc),
                now=now,
            )
        if listing.imported_product_id != proposal.imported_product_id:
            cls._persist_conflict(
                proposal,
                reviewer_id=reviewer_id,
                code="canonical_link_drift",
                message="Ozon-листинг теперь связан с другой внутренней карточкой",
                now=now,
            )
        comparison, source_observed_at = cls._comparison(listing, now=now)
        if not comparison["source_fresh"] or source_observed_at is None:
            cls._persist_conflict(
                proposal,
                reviewer_id=reviewer_id,
                code="ozon_content_snapshot_stale",
                message="Ozon content snapshot устарел до подтверждения",
                now=now,
            )
        if source_observed_at < proposal.source_observed_at:
            cls._persist_conflict(
                proposal,
                reviewer_id=reviewer_id,
                code="ozon_content_observation_regressed",
                message="Ozon content observation старее сохранённого proposal",
                now=now,
            )
        try:
            fields, baseline, proposed = cls._proposal_states(proposal)
        except MarketplaceCanonicalContentConflict as exc:
            cls._persist_conflict(
                proposal,
                reviewer_id=reviewer_id,
                code=exc.code,
                message=str(exc),
                now=now,
            )
        current_baseline, current_proposed = cls._states(comparison, fields)
        current_baseline_fingerprint = cls._state_fingerprint(
            listing=listing,
            fields=fields,
            state=current_baseline,
            source="canonical",
        )
        current_source_fingerprint = cls._state_fingerprint(
            listing=listing,
            fields=fields,
            state=current_proposed,
            source="ozon_observation",
        )
        if (
            current_baseline != baseline
            or current_proposed != proposed
            or current_baseline_fingerprint != proposal.baseline_fingerprint
            or current_source_fingerprint != proposal.source_fingerprint
        ):
            cls._persist_conflict(
                proposal,
                reviewer_id=reviewer_id,
                code="canonical_or_ozon_content_drift",
                message=(
                    "Canonical или Ozon content изменился после создания proposal; "
                    "создайте новый diff"
                ),
                now=now,
            )

        snapshot = AgentChangeSnapshot(
            imported_product_id=proposal.imported_product_id,
            task_id=None,
            agent_id="ozon-canonical-review",
            previous_values=cls._stable_json(baseline),
            new_values=cls._stable_json(proposed),
        )
        db.session.add(snapshot)
        db.session.flush()
        if not cls._conditional_product_update(
            product_id=proposal.imported_product_id,
            seller_id=proposal.seller_id,
            expected=baseline,
            replacement=proposed,
            now=now,
        ):
            db.session.delete(snapshot)
            db.session.flush()
            cls._persist_conflict(
                proposal,
                reviewer_id=reviewer_id,
                code="canonical_apply_race",
                message=(
                    "Canonical content изменился между preflight и записью; "
                    "создайте новый diff"
                ),
                now=now,
            )

        db.session.expire(listing.imported_product)
        proposal.snapshot_id = snapshot.id
        proposal.status = "applied"
        proposal.reviewed_by_user_id = reviewer_id
        proposal.reviewed_at = now
        proposal.applied_at = now
        proposal.review_note = normalized_note
        proposal.error_code = None
        proposal.error_message = None
        cls._commit(listing_id=listing.id, proposal_id=proposal.id)
        return cls._owned_proposal(
            seller_id=proposal.seller_id,
            proposal_id=proposal.id,
        )

    @staticmethod
    def _note(value: Optional[str]) -> Optional[str]:
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            raise MarketplaceCanonicalContentValidationError(
                "note должен быть строкой"
            )
        value = " ".join(value.split()).strip()
        if len(value) > 1000:
            raise MarketplaceCanonicalContentValidationError(
                "note длиннее 1000 символов"
            )
        return value or None

    @classmethod
    def reject_proposal(
        cls,
        *,
        seller_id: int,
        proposal_id: int,
        expected_version: int,
        reviewed_by_user_id: int,
        note: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> MarketplaceCanonicalContentProposal:
        proposal = cls._owned_proposal(
            seller_id=seller_id,
            proposal_id=proposal_id,
        )
        claim = try_account_operation_lock(proposal.account_id)
        if claim is None:
            raise MarketplaceCanonicalContentConflict(
                "Кабинет Ozon сейчас изменяется; повторите после завершения операции",
                code="ozon_account_busy",
                listing_id=proposal.listing_id,
                proposal_id=proposal.id,
            )
        try:
            db.session.expire_all()
            return cls._reject_proposal_locked(
                seller_id=seller_id,
                proposal_id=proposal_id,
                expected_version=expected_version,
                reviewed_by_user_id=reviewed_by_user_id,
                note=note,
                now=now,
            )
        finally:
            release_account_operation_lock(claim)

    @classmethod
    def _reject_proposal_locked(
        cls,
        *,
        seller_id: int,
        proposal_id: int,
        expected_version: int,
        reviewed_by_user_id: int,
        note: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> MarketplaceCanonicalContentProposal:
        proposal = cls._owned_proposal(
            seller_id=seller_id,
            proposal_id=proposal_id,
        )
        reviewer_id = cls._actor(
            seller_id=proposal.seller_id,
            user_id=reviewed_by_user_id,
        )
        expected_version = cls._positive_integer(
            expected_version,
            "expected_version",
        )
        if proposal.version != expected_version or proposal.status != "pending_review":
            raise MarketplaceCanonicalContentConflict(
                "Отклонить можно только актуальный pending_review proposal",
                listing_id=proposal.listing_id,
                proposal_id=proposal.id,
            )
        normalized_note = cls._note(note)
        proposal.status = "rejected"
        proposal.reviewed_by_user_id = reviewer_id
        proposal.reviewed_at = now or datetime.utcnow()
        proposal.review_note = normalized_note
        proposal.error_code = None
        proposal.error_message = None
        cls._commit(listing_id=proposal.listing_id, proposal_id=proposal.id)
        return cls._owned_proposal(
            seller_id=proposal.seller_id,
            proposal_id=proposal.id,
        )

    @classmethod
    def rollback_proposal(
        cls,
        *,
        seller_id: int,
        proposal_id: int,
        expected_version: int,
        rolled_back_by_user_id: int,
        now: Optional[datetime] = None,
    ) -> MarketplaceCanonicalContentProposal:
        proposal = cls._owned_proposal(
            seller_id=seller_id,
            proposal_id=proposal_id,
        )
        actor_id = cls._actor(
            seller_id=proposal.seller_id,
            user_id=rolled_back_by_user_id,
        )
        expected_version = cls._positive_integer(
            expected_version,
            "expected_version",
        )
        if proposal.version != expected_version:
            raise MarketplaceCanonicalContentConflict(
                "Proposal уже изменился; обновите страницу",
                listing_id=proposal.listing_id,
                proposal_id=proposal.id,
            )
        if proposal.status == "rolled_back":
            return proposal
        if proposal.status != "applied":
            raise MarketplaceCanonicalContentConflict(
                "Откат доступен только для applied proposal",
                listing_id=proposal.listing_id,
                proposal_id=proposal.id,
            )
        fields, baseline, proposed = cls._proposal_states(proposal)
        product = ImportedProduct.query.filter_by(
            id=proposal.imported_product_id,
            seller_id=proposal.seller_id,
        ).first()
        if product is None:
            raise MarketplaceCanonicalContentNotFound(
                "Общая внутренняя карточка не найдена",
                listing_id=proposal.listing_id,
                proposal_id=proposal.id,
            )
        current = {field: getattr(product, field, None) for field in fields}
        now = now or datetime.utcnow()
        if current not in (baseline, proposed):
            proposal.error_code = "canonical_rollback_drift"
            proposal.error_message = (
                "Canonical content изменён после apply; автоматический откат заблокирован"
            )
            cls._commit(
                listing_id=proposal.listing_id,
                proposal_id=proposal.id,
            )
            raise MarketplaceCanonicalContentConflict(
                proposal.error_message,
                code=proposal.error_code,
                listing_id=proposal.listing_id,
                proposal_id=proposal.id,
            )
        if not cls._conditional_product_update(
            product_id=proposal.imported_product_id,
            seller_id=proposal.seller_id,
            expected=current,
            replacement=baseline,
            now=now,
        ):
            proposal.error_code = "canonical_rollback_race"
            proposal.error_message = (
                "Canonical content изменён конкурентно; автоматический откат заблокирован"
            )
            cls._commit(
                listing_id=proposal.listing_id,
                proposal_id=proposal.id,
            )
            raise MarketplaceCanonicalContentConflict(
                proposal.error_message,
                code=proposal.error_code,
                listing_id=proposal.listing_id,
                proposal_id=proposal.id,
            )
        db.session.expire(product)
        proposal.status = "rolled_back"
        proposal.rolled_back_by_user_id = actor_id
        proposal.rolled_back_at = now
        proposal.error_code = None
        proposal.error_message = None
        if proposal.snapshot is not None:
            proposal.snapshot.is_rolled_back = True
            proposal.snapshot.rolled_back_at = now
        cls._commit(listing_id=proposal.listing_id, proposal_id=proposal.id)
        return cls._owned_proposal(
            seller_id=proposal.seller_id,
            proposal_id=proposal.id,
        )

    @classmethod
    def list_for_listing(
        cls,
        *,
        seller_id: int,
        listing_id: int,
        limit: int = 20,
    ) -> List[MarketplaceCanonicalContentProposal]:
        seller_id = cls._positive_integer(seller_id, "seller_id")
        listing_id = cls._positive_integer(listing_id, "listing_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise MarketplaceCanonicalContentValidationError(
                "limit должен быть integer 1..50"
            )
        cls._owned_listing(
            seller_id=seller_id,
            listing_id=listing_id,
            require_operational=False,
            require_connected=False,
            require_link=False,
        )
        return MarketplaceCanonicalContentProposal.query.options(
            joinedload(MarketplaceCanonicalContentProposal.account),
            joinedload(MarketplaceCanonicalContentProposal.marketplace),
        ).filter_by(
            seller_id=seller_id,
            listing_id=listing_id,
        ).order_by(
            MarketplaceCanonicalContentProposal.created_at.desc(),
            MarketplaceCanonicalContentProposal.id.desc(),
        ).limit(limit).all()
