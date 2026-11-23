"""Durable exact Ozon brand-search evidence for source preparation."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import re
import time
import unicodedata
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError

from models import (
    ImportedProduct,
    MarketplaceAttributeDefinition,
    MarketplaceAttributeValue,
    MarketplaceListing,
    MarketplaceOperation,
    MarketplaceProductDraft,
    MarketplaceProductType,
    OzonBulkUploadItem,
    OzonBulkUploadRun,
    SellerMarketplaceAccount,
    SellerSupplier,
    db,
)
from services.marketplace_adapters.types import MarketplaceCredentials
from services.marketplace_credential_identity import ozon_credential_fingerprint
from services.marketplace_drafts import MarketplaceDraftService
from services.ozon_brand_policy import first_forbidden_ozon_brand
from services.ozon_brand_search_models import OzonBrandSearchEvidence
from services.ozon_reference_service import OzonReferenceService


_HASH = re.compile(r"^[0-9a-f]{64}$")
_BRAND_IDS = ("31", "85")
_GENERIC = {
    "без бренда", "no brand", "unbranded", "generic", "unknown",
    "not specified", "не указан", "не указано", "неизвестно",
    "none", "null", "n a", "na",
}


class OzonBrandSearch:
    """One exact read per seller/account/reference/source-literal scope."""

    RESULT_LIMIT = 100
    LABEL_LIMIT = 1_000
    PROOF_TTL = timedelta(hours=12)
    CLAIM_TTL = timedelta(seconds=120)
    MIN_START_SECONDS = 12
    RESPONSE_LIMIT_BYTES = 512 * 1024
    PROVENANCE_PREFIX = "ozon_brand_search."

    @staticmethod
    def _norm(value: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())

    @staticmethod
    def _display(value: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", value).split())

    @staticmethod
    def _hash(value: Any) -> str:
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _positive_id(value: Any) -> Optional[int]:
        if type(value) is int:
            return value if 0 < value <= (1 << 63) - 1 else None
        if (
            not isinstance(value, str) or not value.isascii()
            or not value.isdigit() or value.startswith("0") or len(value) > 19
        ):
            return None
        try:
            parsed = int(value)
        except (ValueError, OverflowError):
            return None
        return parsed if 0 < parsed <= (1 << 63) - 1 else None

    @classmethod
    def _literal(cls, value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        value = value.strip()
        normalized = cls._norm(value)
        if (
            not value or len(value) > 200 or len(normalized) < 2
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or normalized.replace("ё", "е") in _GENERIC
            or first_forbidden_ozon_brand((value,)) is not None
        ):
            return None
        return value

    @classmethod
    def _char_brand(cls, value: Any) -> Optional[str]:
        """Extract one exact named brand value from source characteristics."""
        if isinstance(value, dict):
            items = list(value.items())[:200]
        elif isinstance(value, list):
            items = [
                (
                    item.get("name") or item.get("key") or item.get("title"),
                    item.get("value") if "value" in item else item.get("values"),
                )
                for item in value[:200] if isinstance(item, dict)
            ]
        else:
            return None
        brands = []
        for name, raw in items:
            if not isinstance(name, str) or cls._norm(name) not in {"brand", "бренд"}:
                continue
            if isinstance(raw, list):
                if len(raw) != 1:
                    return None
                raw = raw[0]
            brand = cls._literal(raw)
            if brand is None:
                return None
            brands.append(brand)
        if not brands or any(cls._norm(value) != cls._norm(brands[0]) for value in brands):
            return None
        return brands[0]

    @classmethod
    def _observed_source(cls, draft: MarketplaceProductDraft) -> Optional[dict]:
        imported = ImportedProduct.query.filter_by(
            id=draft.imported_product_id, seller_id=draft.seller_id,
        ).first()
        if imported is None:
            return None
        supplier = imported.supplier_product
        from services.supplier_field_observations import supplier_copy_identity_rejection
        if (
            supplier_copy_identity_rejection(imported, supplier) is not None
            or supplier is None
            or supplier.supplier is None
            or not supplier.supplier.is_active
            or SellerSupplier.query.filter_by(
                seller_id=imported.seller_id,
                supplier_id=supplier.supplier_id,
                is_active=True,
            ).first() is None
        ):
            return None
        try:
            current_raw = json.loads(imported.original_data or "{}")
            supplier_raw = json.loads(supplier.original_data_json or "{}")
            from services.supplier_service import _supplier_observed_snapshot
            current_supplier = json.loads(
                _supplier_observed_snapshot(supplier) or "{}"
            )
            facts, provenance, current_hash = MarketplaceDraftService._fact_snapshot(
                imported,
            )
            stored_facts = MarketplaceDraftService._stored_json(
                draft.source_facts_json, dict,
            )
            stored_provenance = MarketplaceDraftService._stored_json(
                draft.provenance_json, dict,
            )
        except Exception:
            return None
        if (
            not all(isinstance(value, dict) for value in (
                current_raw, supplier_raw, current_supplier, facts,
            ))
            or draft.source_fact_hash != current_hash
            or stored_facts != facts
        ):
            return None
        source_facts = facts.get("facts")
        identity = source_facts.get("identity") if isinstance(source_facts, dict) else None
        attributes = source_facts.get("attributes") if isinstance(source_facts, dict) else None
        if not isinstance(identity, dict) or not isinstance(attributes, dict):
            return None
        literal = cls._literal(identity.get("brand"))
        brand_provenance = provenance.get("identity.brand")
        characteristic_provenance = provenance.get("attributes.characteristics")
        if (
            literal is None
            or cls._norm(cls._literal(imported.brand) or "") != cls._norm(literal)
            or cls._norm(cls._literal(current_raw.get("brand")) or "") != cls._norm(literal)
            or cls._norm(cls._literal(supplier_raw.get("brand")) or "") != cls._norm(literal)
            or cls._norm(cls._literal(current_supplier.get("brand")) or "") != cls._norm(literal)
            or not isinstance(brand_provenance, dict)
            or brand_provenance.get("trust") != "observed"
            or not str(brand_provenance.get("source") or "").endswith(".brand")
            or stored_provenance.get("identity.brand") != brand_provenance
            or not isinstance(characteristic_provenance, dict)
            or characteristic_provenance.get("trust") != "observed"
            or not str(characteristic_provenance.get("source") or "").endswith(".characteristics")
            or stored_provenance.get("attributes.characteristics")
            != characteristic_provenance
        ):
            return None
        fact_brand = cls._char_brand(attributes.get("characteristics"))
        copied_brand = cls._char_brand(current_raw.get("characteristics"))
        live_brand = cls._char_brand(current_supplier.get("characteristics"))
        if any(
            value is None or cls._norm(value) != cls._norm(literal)
            for value in (fact_brand, copied_brand, live_brand)
        ):
            return None
        return {
            "literal": literal,
            "fact_hash": current_hash,
            "literal_hash": hashlib.sha256(literal.encode("utf-8")).hexdigest(),
        }

    @classmethod
    def _allowlist(cls, definition: MarketplaceAttributeDefinition) -> Optional[tuple[str, ...]]:
        try:
            values = json.loads(definition.restriction_value_ids_json or "[]")
        except (TypeError, ValueError, RecursionError):
            return None
        if type(values) is not list or len(values) > 10_000:
            return None
        ids = [str(cls._positive_id(value) or "") for value in values]
        if any(not value for value in ids) or len(set(ids)) != len(ids):
            return None
        return tuple(sorted(ids))

    @classmethod
    def _definition_scope(
        cls, draft, definition, *, account=None, now=None,
    ) -> Optional[dict]:
        now = now or datetime.utcnow()
        if (
            draft is None or definition is None
            or draft.published_listing_id is not None
            or MarketplaceListing.query.filter(
                MarketplaceListing.seller_id == draft.seller_id,
                MarketplaceListing.marketplace_id == draft.marketplace_id,
                MarketplaceListing.account_id == draft.account_id,
                db.or_(
                    MarketplaceListing.imported_product_id == draft.imported_product_id,
                    MarketplaceListing.offer_id == draft.offer_id,
                ),
            ).first() is not None
            or definition.external_attribute_id not in _BRAND_IDS
            or definition.product_type_id != draft.product_type_id
            or definition.marketplace_id != draft.marketplace_id
            or not definition.is_available or not definition.is_enabled
            or not definition.is_required or definition.attribute_complex_id
            or definition.is_collection or definition.max_value_count not in (0, 1)
            or cls._positive_id(definition.dictionary_id) is None
            or cls._positive_id(draft.external_category_id) is None
            or cls._positive_id(draft.external_type_id) is None
        ):
            return None
        product_type = MarketplaceProductType.query.filter_by(
            id=draft.product_type_id, marketplace_id=draft.marketplace_id,
        ).first()
        if (
            product_type is None or product_type.category is None
            or not product_type.is_available or not product_type.category.is_available
            or str(product_type.external_type_id) != str(draft.external_type_id)
            or str(product_type.category.external_category_id) != str(draft.external_category_id)
            or type(product_type.attributes_version) is not int
            or product_type.attributes_version <= 0
            or not _HASH.fullmatch(product_type.attributes_schema_hash or "")
            or not OzonReferenceService.reference_is_fresh(product_type, now=now)
            or not OzonReferenceService.dictionary_is_fresh(definition, now=now)
            or type(definition.values_version) is not int or definition.values_version <= 0
            or not _HASH.fullmatch(definition.values_snapshot_hash or "")
        ):
            return None
        allowlist = cls._allowlist(definition)
        source = cls._observed_source(draft)
        account = account or SellerMarketplaceAccount.query.filter_by(
            id=draft.account_id, seller_id=draft.seller_id,
        ).first()
        if (
            allowlist is None or source is None or account is None
            or not account.is_active or not account.has_credentials
            or account.connection_status != "connected"
            or account.marketplace_id != draft.marketplace_id
            or account.credential_expires_at is not None
            and account.credential_expires_at <= now
            or type(account.credential_version) is not int
            or account.credential_version <= 0
        ):
            return None
        fingerprint = ozon_credential_fingerprint(account)
        query_scope = {
            "seller": draft.seller_id,
            "account": account.id,
            "marketplace": draft.marketplace_id,
            "external_account": hashlib.sha256(
                account.external_account_id.encode("utf-8")
            ).hexdigest(),
            "credential": fingerprint,
            "credential_version": account.credential_version,
            "category": int(draft.external_category_id),
            "type": int(draft.external_type_id),
            "type_row": product_type.id,
            "schema_version": product_type.attributes_version,
            "schema_hash": product_type.attributes_schema_hash,
            "attribute_row": definition.id,
            "attribute": int(definition.external_attribute_id),
            "dictionary": int(definition.dictionary_id),
            "dictionary_version": definition.values_version,
            "dictionary_hash": definition.values_snapshot_hash,
            "allowlist": allowlist,
            "literal_hash": source["literal_hash"],
        }
        query_scope["scope_hash"] = cls._hash(query_scope)
        return {
            **query_scope,
            "literal": source["literal"],
            "fact_hash": source["fact_hash"],
            "credential_fingerprint": fingerprint,
        }

    @classmethod
    def _brand_cells(cls, draft) -> Optional[list[tuple[str, dict]]]:
        try:
            rows = json.loads(draft.attributes_json or "[]")
        except (TypeError, ValueError, RecursionError):
            return None
        if type(rows) is not list:
            return None
        found = []
        for row in rows:
            if not isinstance(row, dict):
                return None
            if row.get("attribute_id") not in _BRAND_IDS:
                continue
            if row.get("complex_id", "0") != "0" or not isinstance(row.get("values"), list):
                return None
            values = row["values"]
            if len(values) != 1:
                found.append((row["attribute_id"], {}))
                continue
            value = values[0]
            if (
                not isinstance(value, dict)
                or set(value) - {"value", "dictionary_value_id"}
                or not isinstance(value.get("value"), str)
                or not isinstance(value.get("dictionary_value_id"), str)
            ):
                return None
            found.append((row["attribute_id"], {
                "value": value["value"],
                "dictionary_value_id": value["dictionary_value_id"],
            }))
        return found

    @classmethod
    def _listed_or_busy(cls, draft) -> bool:
        if draft.published_listing_id is not None or MarketplaceListing.query.filter(
            MarketplaceListing.seller_id == draft.seller_id,
            MarketplaceListing.marketplace_id == draft.marketplace_id,
            MarketplaceListing.account_id == draft.account_id,
            db.or_(
                MarketplaceListing.imported_product_id == draft.imported_product_id,
                MarketplaceListing.offer_id == draft.offer_id,
            ),
        ).first() is not None:
            return True
        return MarketplaceOperation.query.filter(
            MarketplaceOperation.seller_id == draft.seller_id,
            MarketplaceOperation.marketplace_id == draft.marketplace_id,
            MarketplaceOperation.account_id == draft.account_id,
            MarketplaceOperation.draft_id == draft.id,
            MarketplaceOperation.status.in_(
                MarketplaceDraftService.ACTIVE_PUBLICATION_STATUSES,
            ),
        ).first() is not None

    @classmethod
    def _choices(cls, evidence) -> Optional[list[dict]]:
        if evidence is None or evidence.status != "completed":
            return None
        try:
            choices = json.loads(evidence.choices_json or "[]")
        except (TypeError, ValueError, RecursionError):
            return None
        if type(choices) is not list or len(choices) > cls.RESULT_LIMIT:
            return None
        result, seen = [], {}
        for row in choices:
            if (
                not isinstance(row, dict)
                or set(row) != {"dictionary_value_id", "value"}
                or cls._positive_id(row.get("dictionary_value_id")) is None
                or not isinstance(row.get("value"), str)
                or not row["value"] or len(row["value"]) > cls.LABEL_LIMIT
                or row["value"].strip() != row["value"]
            ):
                return None
            value_id, label = str(int(row["dictionary_value_id"])), row["value"]
            if value_id in seen and seen[value_id] != label:
                return None
            if value_id not in seen:
                seen[value_id] = label
                result.append({"dictionary_value_id": value_id, "value": label})
        return result

    @classmethod
    def _eligible_choices(cls, draft, definition, choices, allowlist) -> list[dict]:
        allowed = set(allowlist)
        ids = [row["dictionary_value_id"] for row in choices]
        local_rows = MarketplaceAttributeValue.query.filter(
            MarketplaceAttributeValue.marketplace_id == draft.marketplace_id,
            MarketplaceAttributeValue.product_type_id == draft.product_type_id,
            MarketplaceAttributeValue.attribute_id == definition.id,
            MarketplaceAttributeValue.external_value_id.in_(ids or [""]),
        ).all()
        by_id: dict[str, list] = {}
        for row in local_rows:
            by_id.setdefault(row.external_value_id, []).append(row)
        return [
            item for item in choices
            if (not allowed or item["dictionary_value_id"] in allowed)
            and first_forbidden_ozon_brand((item["value"],)) is None
            and cls._norm(item["value"]).replace("ё", "е") not in _GENERIC
            and not any(
                not row.is_available or row.value != item["value"]
                for row in by_id.get(item["dictionary_value_id"], [])
            )
        ]

    @classmethod
    def value_choices(cls, draft, definition, *, now=None) -> list[dict]:
        """Read a fresh proof only. This method never calls Ozon."""
        now = now or datetime.utcnow()
        scope = cls._definition_scope(draft, definition, now=now)
        if scope is None:
            return []
        proof = OzonBrandSearchEvidence.query.filter_by(
            scope_hash=scope["scope_hash"],
            seller_id=draft.seller_id,
            account_id=draft.account_id,
            status="completed",
        ).first()
        if proof is None or proof.expires_at <= now:
            return []
        choices = cls._choices(proof)
        return cls._eligible_choices(
            draft, definition, choices or [], scope["allowlist"],
        ) if choices is not None else []

    @classmethod
    def _cell_has_proof(cls, draft, definition, scope) -> bool:
        cells = cls._brand_cells(draft)
        if cells is None or len(cells) != 1 or cells[0][0] != definition.external_attribute_id:
            return False
        proof = OzonBrandSearchEvidence.query.filter_by(
            scope_hash=scope["scope_hash"],
            seller_id=draft.seller_id,
            account_id=draft.account_id,
            status="completed",
        ).first()
        choices = cls._choices(proof)
        return bool(choices is not None and cells[0][1] in choices)

    @classmethod
    def _local_match(cls, definition, literal, allowlist) -> bool:
        query = MarketplaceAttributeValue.query.filter(
            MarketplaceAttributeValue.marketplace_id == definition.marketplace_id,
            MarketplaceAttributeValue.product_type_id == definition.product_type_id,
            MarketplaceAttributeValue.attribute_id == definition.id,
            MarketplaceAttributeValue.value_normalized
            == OzonReferenceService.normalize_value(literal),
            MarketplaceAttributeValue.is_available.is_(True),
        )
        if allowlist:
            query = query.filter(
                MarketplaceAttributeValue.external_value_id.in_(allowlist),
            )
        return query.first() is not None

    @classmethod
    def _claim(cls, scope, seller_id, account_id, now):
        """Return state, generation, choices, completeness, and expired restore."""
        row = OzonBrandSearchEvidence.query.filter_by(
            scope_hash=scope["scope_hash"],
        ).first()
        if row is None:
            db.session.add(OzonBrandSearchEvidence(
                scope_hash=scope["scope_hash"],
                seller_id=seller_id,
                account_id=account_id,
                status="reserved",
                choices_json="[]",
                reserved_at=now,
                expires_at=now + cls.CLAIM_TTL,
                complete_result=False,
            ))
            try:
                db.session.commit()
                return "claimed", now, None, False, None
            except IntegrityError:
                db.session.rollback()
                return "blocked", None, None, False, None
            except Exception:
                db.session.rollback()
                return "blocked", None, None, False, None
        if row.seller_id != seller_id or row.account_id != account_id:
            db.session.rollback()
            return "blocked", None, None, False, None
        if row.status == "unavailable":
            db.session.rollback()
            return "blocked", None, None, False, None
        if row.status == "reserved":
            if row.expires_at > now:
                db.session.rollback()
                return "blocked", None, None, False, None
            update = OzonBrandSearchEvidence.query.filter_by(
                scope_hash=scope["scope_hash"],
                status="reserved",
                reserved_at=row.reserved_at,
            ).filter(OzonBrandSearchEvidence.expires_at <= now).update({
                OzonBrandSearchEvidence.status: "unavailable",
                OzonBrandSearchEvidence.choices_json: "[]",
                OzonBrandSearchEvidence.finished_at: now,
            }, synchronize_session=False)
            db.session.commit() if update == 1 else db.session.rollback()
            return "blocked", None, None, False, None
        choices = cls._choices(row)
        if row.status != "completed" or choices is None:
            db.session.rollback()
            return "blocked", None, None, False, None
        if row.expires_at > now:
            complete = row.complete_result is True
            db.session.rollback()
            return "cached", None, choices, complete, None
        restore = {
            "choices_json": row.choices_json,
            "reserved_at": row.reserved_at,
            "expires_at": row.expires_at,
            "finished_at": row.finished_at,
            "complete_result": bool(row.complete_result),
        }
        generation = now
        update = OzonBrandSearchEvidence.query.filter_by(
            scope_hash=scope["scope_hash"],
            status="completed",
            reserved_at=row.reserved_at,
        ).filter(OzonBrandSearchEvidence.expires_at <= now).update({
            OzonBrandSearchEvidence.status: "reserved",
            OzonBrandSearchEvidence.choices_json: "[]",
            OzonBrandSearchEvidence.reserved_at: generation,
            OzonBrandSearchEvidence.expires_at: generation + cls.CLAIM_TTL,
            OzonBrandSearchEvidence.finished_at: None,
            OzonBrandSearchEvidence.complete_result: False,
        }, synchronize_session=False)
        if update == 1:
            db.session.commit()
            return "claimed", generation, None, False, restore
        db.session.rollback()
        return "blocked", None, None, False, None

    @classmethod
    def _release_local_limit(cls, scope_hash, generation, restore):
        query = OzonBrandSearchEvidence.query.filter_by(
            scope_hash=scope_hash, status="reserved", reserved_at=generation,
        )
        try:
            if restore is None:
                query.delete(synchronize_session=False)
            else:
                query.update({
                    OzonBrandSearchEvidence.status: "completed",
                    OzonBrandSearchEvidence.choices_json: restore["choices_json"],
                    OzonBrandSearchEvidence.reserved_at: restore["reserved_at"],
                    OzonBrandSearchEvidence.expires_at: restore["expires_at"],
                    OzonBrandSearchEvidence.finished_at: restore["finished_at"],
                    OzonBrandSearchEvidence.complete_result: restore["complete_result"],
                }, synchronize_session=False)
            db.session.commit()
        except Exception:
            db.session.rollback()

    @classmethod
    def _finish(cls, scope_hash, generation, now, choices, complete):
        encoded = "[]" if choices is None else json.dumps(
            choices, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        updated = OzonBrandSearchEvidence.query.filter_by(
            scope_hash=scope_hash, status="reserved", reserved_at=generation,
        ).update({
            OzonBrandSearchEvidence.status: (
                "completed" if choices is not None else "unavailable"
            ),
            OzonBrandSearchEvidence.choices_json: encoded,
            OzonBrandSearchEvidence.finished_at: now,
            OzonBrandSearchEvidence.expires_at: now + cls.PROOF_TTL,
            OzonBrandSearchEvidence.complete_result: (
                bool(complete) if choices is not None else False
            ),
        }, synchronize_session=False)
        if updated != 1:
            db.session.rollback()
            return False
        db.session.commit()
        return True

    @classmethod
    def _normalize_response(cls, response, literal):
        results = response.get("result") if isinstance(response, dict) else None
        if type(results) is not list or len(results) > cls.RESULT_LIMIT:
            return None
        complete = len(results) < cls.RESULT_LIMIT
        by_id = {}
        for row in results:
            value_id = (
                row.get("id") if isinstance(row, dict)
                and type(row.get("id")) is int else None
            )
            label = row.get("value") if isinstance(row, dict) else None
            if (
                cls._positive_id(value_id) is None
                or not isinstance(label, str) or not label
                or label.strip() != label or len(label) > cls.LABEL_LIMIT
                or any(ord(char) < 32 or ord(char) == 127 for char in label)
            ):
                return None
            key = str(value_id)
            if key in by_id and by_id[key] != label:
                return None
            by_id[key] = label
        normalized = cls._norm(literal)
        exact = [
            {"dictionary_value_id": key, "value": value}
            for key, value in by_id.items()
            if cls._norm(value) == normalized
        ]
        return exact, complete

    @classmethod
    def _auto_choice(cls, scope, definition, choices, complete):
        if not complete or len(choices) != 1:
            return None
        choice = choices[0]
        if cls._display(choice["value"]) != cls._display(scope["literal"]):
            return None
        if scope["allowlist"] and choice["dictionary_value_id"] not in scope["allowlist"]:
            return None
        if (
            first_forbidden_ozon_brand((choice["value"],)) is not None
            or cls._norm(choice["value"]).replace("ё", "е") in _GENERIC
        ):
            return None
        rows = MarketplaceAttributeValue.query.filter_by(
            marketplace_id=definition.marketplace_id,
            product_type_id=definition.product_type_id,
            attribute_id=definition.id,
            external_value_id=choice["dictionary_value_id"],
        ).all()
        return None if any(
            not row.is_available or row.value != choice["value"] for row in rows
        ) else choice

    @classmethod
    def _stage(cls, run, item, draft_id, version, definition_id,
               scope_hash, literal_hash, choice, lease_token):
        from services.ozon_upload_queue import (
            OzonUploadLeaseLost, OzonUploadQueueService,
        )
        if db.session.new or db.session.dirty or db.session.deleted:
            db.session.rollback()
            return False
        db.session.expire_all()
        now = datetime.utcnow()
        try:
            OzonUploadQueueService._fence_lease(
                run_id=run.id, lease_token=lease_token, now=now,
            )
            fresh_run = OzonBulkUploadRun.query.filter_by(
                id=run.id, seller_id=run.seller_id,
                account_id=run.account_id, mode="source_prepare", state="active",
            ).first()
            fresh_item = OzonBulkUploadItem.query.filter_by(
                id=item.id, run_id=run.id,
            ).first()
            draft = MarketplaceProductDraft.query.filter_by(
                id=draft_id, seller_id=run.seller_id,
                account_id=run.account_id, imported_product_id=item.imported_product_id,
            ).first()
            allowed_lineage = {
                OzonUploadQueueService.QUEUE_CREATED_DRAFT_CODE,
                *(
                    f"{OzonUploadQueueService.DECISION_CONTINUATION_CODE_PREFIX}{n}"
                    for n in range(1, OzonUploadQueueService.MAX_DECISION_CONTINUATIONS + 1)
                ),
            }
            if (
                fresh_run is None or fresh_item is None or draft is None
                or fresh_item.phase != "preparing" or fresh_item.operation_id is not None
                or fresh_item.draft_id != draft.id
                or fresh_item.error_code not in allowed_lineage
                or draft.version != version or cls._listed_or_busy(draft)
            ):
                db.session.rollback()
                return False
            definition = MarketplaceAttributeDefinition.query.filter_by(
                id=definition_id, marketplace_id=draft.marketplace_id,
                product_type_id=draft.product_type_id,
            ).first()
            scope = cls._definition_scope(draft, definition, now=now) if definition else None
            if scope is None or scope["scope_hash"] != scope_hash:
                db.session.rollback()
                return False
            if cls._brand_cells(draft) != []:
                db.session.rollback()
                return False
            attributes = json.loads(draft.attributes_json or "[]")
            attributes.append({
                "attribute_id": definition.external_attribute_id,
                "complex_id": "0",
                "values": [{
                    "dictionary_value_id": choice["dictionary_value_id"],
                    "value": choice["value"],
                }],
            })
            MarketplaceDraftService._update_draft_core(
                seller_id=run.seller_id, draft_id=draft.id,
                expected_version=version, patch={"attributes": attributes},
                commit=False,
            )
            provenance = MarketplaceDraftService._stored_json(
                draft.provenance_json, dict,
            )
            provenance[cls.PROVENANCE_PREFIX + definition.external_attribute_id] = {
                "version": 1,
                "scope_hash": scope_hash,
                "source_literal_sha256": literal_hash,
                "dictionary_value_id": choice["dictionary_value_id"],
                "value": choice["value"],
                "source": "ozon_attribute_values_search",
                "trust": "official_exact_search",
            }
            draft.provenance_json = MarketplaceDraftService._canonical_json(
                provenance, dict,
            )
            db.session.commit()
            return True
        except Exception as exc:
            db.session.rollback()
            if isinstance(exc, OzonUploadLeaseLost):
                raise
            return False

    @classmethod
    def prepare_source_brand(
        cls, *, run, item, account, product, draft, lease_token,
        confirmed_queue_created_draft, deadline_monotonic=None, now=None,
        client_factory=None,
    ) -> bool:
        """Run one durable search; auto-stage only one exact provider ID."""
        from services.ozon_upload_queue import OzonUploadQueueService
        now = now or datetime.utcnow()
        if (
            run.mode != "source_prepare" or run.seller_id != draft.seller_id
            or run.account_id != draft.account_id
            or item.imported_product_id != draft.imported_product_id
            or account.id != draft.account_id or account.seller_id != draft.seller_id
            or product.id != draft.imported_product_id or product.seller_id != draft.seller_id
            or item.phase != "preparing" or item.operation_id is not None
            or cls._listed_or_busy(draft)
            or not OzonUploadQueueService.lease_current(
                run_id=run.id, lease_token=lease_token, now=now,
            )
            or db.session.new or db.session.dirty or db.session.deleted
        ):
            return False
        definitions = MarketplaceAttributeDefinition.query.filter(
            MarketplaceAttributeDefinition.marketplace_id == draft.marketplace_id,
            MarketplaceAttributeDefinition.product_type_id == draft.product_type_id,
            MarketplaceAttributeDefinition.external_attribute_id.in_(_BRAND_IDS),
            MarketplaceAttributeDefinition.is_available.is_(True),
            MarketplaceAttributeDefinition.is_enabled.is_(True),
            MarketplaceAttributeDefinition.is_required.is_(True),
        ).order_by(MarketplaceAttributeDefinition.id.asc()).all()
        eligible = [
            (definition, cls._definition_scope(draft, definition, account=account, now=now))
            for definition in definitions
        ]
        eligible = [(definition, scope) for definition, scope in eligible if scope is not None]
        if len(eligible) != 1:
            return False
        definition, scope = eligible[0]
        draft_id, version, definition_id = draft.id, draft.version, definition.id
        cells = cls._brand_cells(draft)
        if cells is None:
            return False
        if not cells:
            if cls._local_match(definition, scope["literal"], scope["allowlist"]):
                return False
        elif not cls._cell_has_proof(draft, definition, scope):
            return False
        if deadline_monotonic is not None and (
            deadline_monotonic - time.monotonic() < cls.MIN_START_SECONDS
        ):
            cached = OzonBrandSearchEvidence.query.filter_by(
                scope_hash=scope["scope_hash"], seller_id=run.seller_id,
                account_id=run.account_id, status="completed",
            ).first()
            if cached is None or cached.expires_at <= now:
                return False
        state, generation, choices, complete, restore = cls._claim(
            scope, run.seller_id, run.account_id, now,
        )
        if state == "cached":
            if cells or confirmed_queue_created_draft is not True:
                return False
            choice = cls._auto_choice(scope, definition, choices or [], complete)
            return bool(choice and cls._stage(
                run, item, draft_id, version, definition_id,
                scope["scope_hash"], scope["literal_hash"],
                choice, lease_token,
            ))
        if state != "claimed" or generation is None:
            return False

        owns_client = client_factory is None
        session = None
        try:
            secret = account.get_credentials()
            credentials = MarketplaceCredentials(
                external_account_id=account.external_account_id,
                api_key=secret["api_key"],
            )
            del secret
            if client_factory is None:
                from services.ozon_api_client import OzonSellerAPIClient
                client_factory = OzonSellerAPIClient
            client = client_factory(
                credentials, timeout=(3.0, 6.0), read_retries=0,
            )
            session = getattr(client, "session", None)
            try:
                if session is not None:
                    from services.ozon_read_response import bound_read_responses
                    if not getattr(session, "_ozon_read_response_bounded", False):
                        bound_read_responses(
                            session, maximum_bytes=cls.RESPONSE_LIMIT_BYTES,
                        )
                response = client.search_description_category_attribute_values({
                    "attribute_id": scope["attribute"],
                    "description_category_id": scope["category"],
                    "type_id": scope["type"],
                    "limit": cls.RESULT_LIMIT,
                    "value": scope["literal"],
                })
            finally:
                if owns_client and session is not None:
                    try:
                        session.close()
                    except Exception:
                        pass
            normalized = cls._normalize_response(response, scope["literal"])
            choices = normalized[0] if normalized is not None else None
            complete = bool(normalized is not None and normalized[1])

            db.session.expire_all()
            fresh_account = SellerMarketplaceAccount.query.filter_by(
                id=scope["account"], seller_id=scope["seller"],
            ).first()
            fresh_draft = MarketplaceProductDraft.query.filter_by(
                id=draft_id, seller_id=scope["seller"], account_id=scope["account"],
            ).first()
            fresh_definition = MarketplaceAttributeDefinition.query.filter_by(
                id=definition_id, marketplace_id=scope["marketplace"],
                product_type_id=scope["type_row"],
            ).first()
            current_scope = (
                cls._definition_scope(fresh_draft, fresh_definition, account=fresh_account)
                if fresh_draft is not None and fresh_definition is not None else None
            )
            if (
                current_scope is None
                or current_scope["scope_hash"] != scope["scope_hash"]
                or fresh_account is None
                or ozon_credential_fingerprint(fresh_account)
                != scope["credential_fingerprint"]
            ):
                choices, complete = None, False
            if not cls._finish(
                scope["scope_hash"], generation, datetime.utcnow(), choices, complete,
            ):
                return False
            if (
                choices is None or cells or not confirmed_queue_created_draft
                or fresh_draft is None or fresh_draft.version != version
                or current_scope["fact_hash"] != scope["fact_hash"]
            ):
                return False
            # Keep the provider's exact candidate count intact through the
            # auto-stage decision. An allowlist may limit picker choices, but
            # it cannot resolve an ambiguous provider response.
            choice = cls._auto_choice(scope, fresh_definition, choices, complete)
            return bool(choice and cls._stage(
                run, item, draft_id, version, definition_id,
                scope["scope_hash"], scope["literal_hash"],
                choice, lease_token,
            ))
        except Exception as exc:
            from services.ozon_api_client import OzonRateLimitError
            if (
                type(exc) is OzonRateLimitError
                and getattr(exc, "code", None) == "ozon_local_rate_limited"
            ):
                cls._release_local_limit(scope["scope_hash"], generation, restore)
            else:
                try:
                    db.session.expire_all()
                    cls._finish(
                        scope["scope_hash"], generation, datetime.utcnow(), None, False,
                    )
                except Exception:
                    db.session.rollback()
            return False
