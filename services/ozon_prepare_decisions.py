"""Bounded, source-backed Ozon color preparation for local draft queues.

Only the explicit parser-owned supplier color field can enter the Decisions
request.  This module does not choose categories, call Ozon, or write sibling
drafts; a result is applied only to the newly-created draft owned by the
current source-preparation item.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import re
import time
from types import SimpleNamespace
from typing import Any, Optional

from sqlalchemy import or_

from models import (
    ImportedProduct,
    MarketplaceAttributeDefinition,
    MarketplaceAttributeValue,
    MarketplaceProductDraft,
    MarketplaceListing,
    MarketplaceOperation,
    OzonBulkUploadItem,
    OzonBulkUploadRun,
    SellerMarketplaceAccount,
    db,
)
from services.marketplace_decisions_preparation import (
    CharacteristicAttributeContext,
    CharacteristicProductContext,
    CharacteristicOption,
    DictionaryValueChoice,
    plan_characteristic_phase,
)
from services.marketplace_drafts import MarketplaceDraftService
from services.marketplace_publications import MarketplacePublicationService
from services.ozon_bulk_repair import OzonBulkRepairService
from services.ozon_reference_service import OzonReferenceService


MAX_WINDOW_ITEMS = 12
MAX_DICTIONARY_SCAN = 500
MAX_SHORTLIST_CHOICES = 12
MIN_SELECTED_CONFIDENCE = 0.95
MIN_DECISION_SECONDS_REMAINING = 22.0
COLOR_ATTRIBUTE_ID = "10096"
COLOR_DICTIONARY_ID = "1494"


@dataclass(frozen=True)
class _Candidate:
    item_id: int
    draft_id: Optional[int]
    draft_version: Optional[int]
    context: CharacteristicProductContext
    choice_rows: dict[str, str]
    deterministic_cell: Optional[dict] = None


class OzonPrepareDecisionsService:
    """Stage one safely selected color on a confirmed queue-owned draft."""

    @classmethod
    def prepare_source_window(
        cls,
        *,
        run: OzonBulkUploadRun,
        current_item: OzonBulkUploadItem,
        current_draft,
        lease_token: str,
        now: Optional[datetime] = None,
        confirmed_queue_created_draft: bool,
        deadline_monotonic: Optional[float] = None,
    ) -> bool:
        """Warm at most 12 due product decisions and apply only this item.

        Siblings are virtual, read-only projections and must have no draft or
        listing yet. Their normalized answers can be reused when the queue
        creates each draft later. This item must carry persisted creation
        lineage and an unchanged draft version; unconfirmed drafts never enter
        a request.
        """
        if confirmed_queue_created_draft is not True:
            return False
        current_time = now or datetime.utcnow()
        try:
            window = cls._due_items(
                run=run, current_item=current_item, lease_token=lease_token,
                now=current_time,
            )
            candidates: list[_Candidate] = []
            current_candidate: Optional[_Candidate] = None
            for item in window:
                if item.id == current_item.id:
                    candidate = cls._candidate_for_new_draft(
                        run=run, item=item, draft=current_draft,
                    )
                    if candidate is not None:
                        current_candidate = candidate
                else:
                    candidate = cls._candidate_for_uncreated_sibling(
                        run=run, item=item,
                    )
                if candidate is None:
                    continue
                if candidate.deterministic_cell is not None:
                    if item.id == current_item.id:
                        return cls._apply_current_cell(
                            run=run, item=item, candidate=candidate,
                            cell=candidate.deterministic_cell,
                            lease_token=lease_token,
                        )
                    continue
                candidates.append(candidate)

            if not candidates:
                return False

            # A direct exact or existing deterministic result always wins.
            # Only ask Decisions when the current lease-owned draft itself is
            # unresolved; sibling-only prewarming is intentionally omitted.
            current_candidate = next(
                (
                    candidate for candidate in candidates
                    if candidate.item_id == current_item.id
                ),
                None,
            )
            if current_candidate is None:
                return False
            if (
                deadline_monotonic is not None
                and deadline_monotonic - time.monotonic()
                < MIN_DECISION_SECONDS_REMAINING
            ):
                return False

            from services.openai_decisions_runtime import (
                create_openai_decisions_pipeline,
            )

            pipeline = create_openai_decisions_pipeline()
            if pipeline is None:
                return False

            contexts = [candidate.context for candidate in candidates]
            plan = plan_characteristic_phase(contexts)
            if plan.phase != "characteristic" or not plan.questions:
                return False
            fingerprints = plan.question_fingerprints
            if len(fingerprints) != len(candidates):
                return False
            fingerprint_by_item = {
                candidate.item_id: fingerprint
                for candidate, fingerprint in zip(candidates, fingerprints)
            }
            current_fingerprint = fingerprint_by_item.get(current_item.id)
            if not current_fingerprint:
                return False

            if db.session.new or db.session.dirty or db.session.deleted:
                db.session.rollback()
                return False
            result = pipeline.run_plan(plan)
            # The request runs outside the draft writer transaction. Discard
            # stale ORM identities before the post-request lease fence.
            if db.session.new or db.session.dirty or db.session.deleted:
                db.session.rollback()
                return False
            db.session.expire_all()
            if (
                getattr(result, "phase", None) != "characteristic"
                or not isinstance(getattr(result, "results", None), (list, tuple))
            ):
                return False
            selected = next(
                (
                    row for row in result.results
                    if getattr(row, "signature", None) == current_fingerprint
                ),
                None,
            )
            if (
                selected is None
                or getattr(selected, "status", None) != "selected"
                or type(getattr(selected, "confidence", None)) not in (int, float)
                or not math.isfinite(selected.confidence)
                or not MIN_SELECTED_CONFIDENCE <= selected.confidence <= 1
                or type(getattr(selected, "option", None)) is not CharacteristicOption
            ):
                return False

            cell = cls._selected_cell(
                selected.option, candidate=current_candidate,
            )
            if cell is None:
                return False
            return cls._apply_current_cell(
                run=run, item=current_item, candidate=current_candidate,
                cell=cell, lease_token=lease_token,
                expected_fingerprint=current_fingerprint,
                selected_option=selected.option,
            )
        except Exception as exc:
            from services.ozon_upload_queue import OzonUploadLeaseLost
            if isinstance(exc, OzonUploadLeaseLost):
                raise
            # This is opportunistic local preparation. Failed reads, stale
            # references, provider uncertainty and concurrent edits leave the
            # draft untouched and fall through to ordinary validation.
            db.session.rollback()
            return False

    @classmethod
    def _due_items(
        cls,
        *,
        run: OzonBulkUploadRun,
        current_item: OzonBulkUploadItem,
        lease_token: str,
        now: datetime,
    ) -> list[OzonBulkUploadItem]:
        live_run = OzonBulkUploadRun.query.filter_by(
            id=run.id,
            seller_id=run.seller_id,
            account_id=run.account_id,
            mode="source_prepare",
            state="active",
        ).first()
        if live_run is None or not cls._lease_matches(
            live_run, lease_token=lease_token, now=now,
        ):
            return []
        due = or_(
            OzonBulkUploadItem.next_due_at.is_(None),
            OzonBulkUploadItem.next_due_at <= now,
        )
        siblings = OzonBulkUploadItem.query.filter(
            OzonBulkUploadItem.run_id == run.id,
            OzonBulkUploadItem.id != current_item.id,
            OzonBulkUploadItem.phase.in_(("pending", "preparing", "waiting_reference")),
            OzonBulkUploadItem.operation_id.is_(None),
            due,
        ).order_by(
            OzonBulkUploadItem.ordinal.asc(),
            OzonBulkUploadItem.id.asc(),
        ).limit(MAX_WINDOW_ITEMS - 1).all()
        current = OzonBulkUploadItem.query.filter_by(
            id=current_item.id, run_id=run.id,
        ).first()
        if (
            current is None
            or current.phase != "preparing"
            or current.operation_id is not None
            or current.next_due_at is not None and current.next_due_at > now
        ):
            return []
        return [current, *siblings]

    @classmethod
    def _candidate_for_new_draft(
        cls,
        *,
        run: OzonBulkUploadRun,
        item: OzonBulkUploadItem,
        draft,
    ) -> Optional[_Candidate]:
        if (
            item.imported_product_id != draft.imported_product_id
            or item.draft_id != draft.id
            or item.phase != "preparing"
            or item.operation_id is not None
            or draft.seller_id != run.seller_id
            or draft.account_id != run.account_id
            or draft.published_listing_id is not None
            or draft.status == "archived"
            or cls._draft_has_active_operation(draft)
            or cls._has_exact_listing(
                seller_id=run.seller_id, account_id=run.account_id,
                imported_product_id=item.imported_product_id,
            )
            or cls._has_offer_collision(
                account_id=run.account_id, offer_id=draft.offer_id,
                except_draft_id=draft.id,
            )
            or cls._has_color_removal(draft)
        ):
            return None
        product = ImportedProduct.query.filter_by(
            id=item.imported_product_id, seller_id=run.seller_id,
        ).first()
        if product is None:
            return None
        cells, valid = OzonBulkRepairService._source_current_attribute_cells(
            draft=draft, external_id=COLOR_ATTRIBUTE_ID,
        )
        if not cls._attribute_json_is_valid(draft) or not valid or cells:
            return None
        return cls._candidate(
            run=run, item=item, product=product, draft=draft,
            product_type=draft.product_type,
        )

    @classmethod
    def _candidate_for_uncreated_sibling(
        cls,
        *,
        run: OzonBulkUploadRun,
        item: OzonBulkUploadItem,
    ) -> Optional[_Candidate]:
        if item.draft_id is not None or item.operation_id is not None:
            return None
        product = ImportedProduct.query.filter_by(
            id=item.imported_product_id, seller_id=run.seller_id,
        ).first()
        account = SellerMarketplaceAccount.query.filter_by(
            id=run.account_id, seller_id=run.seller_id, is_active=True,
        ).first()
        if product is None or account is None or account.marketplace.code != "ozon":
            return None
        if MarketplaceProductDraft.query.filter_by(
            seller_id=run.seller_id,
            account_id=run.account_id,
            imported_product_id=product.id,
        ).first() is not None:
            return None
        if cls._has_exact_listing(
            seller_id=run.seller_id,
            account_id=run.account_id,
            imported_product_id=product.id,
        ):
            return None
        try:
            facts_document, _provenance, _fact_hash = (
                MarketplaceDraftService._fact_snapshot(product)
            )
            offer_id = MarketplaceDraftService._derive_offer_id(
                product, facts_document,
            )
            if cls._has_offer_collision(
                account_id=run.account_id, offer_id=offer_id,
            ):
                return None
            mapping = MarketplaceDraftService._active_mapping(
                seller_id=run.seller_id,
                marketplace_id=account.marketplace_id,
                product=product,
            )
            if mapping is None or mapping.mapping_status != "active":
                return None
            product_type = mapping.product_type
            if product_type is None:
                return None
            deterministic, _report = MarketplaceDraftService._auto_map_attributes(
                product_type=product_type,
                facts_document=facts_document,
                category_mapping=mapping,
            )
            if cls._has_color_value(deterministic):
                return None
            projection = SimpleNamespace(
                id=None,
                seller_id=run.seller_id,
                marketplace_id=account.marketplace_id,
                account_id=account.id,
                imported_product_id=product.id,
                product_type_id=product_type.id,
                product_type=product_type,
                category_mapping=mapping,
                imported_product=product,
                attributes_json="[]",
                attribute_removals_json="[]",
                offer_id=offer_id,
                published_listing_id=None,
                status="needs_review",
            )
            cells, valid = OzonBulkRepairService._source_current_attribute_cells(
                draft=projection, external_id=COLOR_ATTRIBUTE_ID,
            )
            if not cls._attribute_json_is_valid(projection) or not valid or cells:
                return None
            return cls._candidate(
                run=run, item=item, product=product, draft=projection,
                product_type=product_type,
                draft_id=None, draft_version=None,
            )
        except Exception:
            return None

    @classmethod
    def _candidate(
        cls,
        *,
        run: OzonBulkUploadRun,
        item: OzonBulkUploadItem,
        product: ImportedProduct,
        draft,
        product_type,
        draft_id: Optional[int] = None,
        draft_version: Optional[int] = None,
    ) -> Optional[_Candidate]:
        if (
            product_type is None
            or product_type.id != draft.product_type_id
            or product_type.marketplace_id != draft.marketplace_id
            or product_type.category is None
            or product_type.category.marketplace_id != draft.marketplace_id
        ):
            return None
        evidence = OzonBulkRepairService._source_supplier_evidence(draft)
        colors = evidence.get("fields", {}).get("colors")
        if (
            evidence.get("status") != "confirmed"
            or not isinstance(colors, list)
            or len(colors) != 1
            or not isinstance(colors[0], str)
            or not colors[0].strip()
        ):
            return None
        definition, reason = OzonBulkRepairService._source_attribute_definition(
            draft=draft, external_id=COLOR_ATTRIBUTE_ID,
        )
        if definition is None or reason is not None:
            return None
        literal = colors[0].strip()
        deterministic_cell, deterministic_reason, _state = (
            OzonBulkRepairService._resolve_source_dictionary_cell(
                definition=definition,
                literal=literal,
            )
        )
        if deterministic_cell is not None:
            return cls._make_candidate(
                run=run, item=item, product=product, draft=draft,
                source_evidence=evidence, product_type=product_type,
                definition=definition, literal=literal,
                deterministic_cell=deterministic_cell,
                draft_id=draft_id, draft_version=draft_version,
            )
        if deterministic_reason in {
            "dictionary_value_ambiguous",
            "dictionary_value_restricted",
        }:
            return None
        if MarketplaceDraftService._color_observation_is_uncertain(literal):
            return None
        canonical_colors = MarketplaceDraftService._observed_colors([literal])
        if len(canonical_colors) == 1:
            canonical_cell, canonical_reason, _canonical_state = (
                OzonBulkRepairService._resolve_source_dictionary_cell(
                    definition=definition,
                    literal=canonical_colors[0],
                )
            )
            if canonical_cell is not None:
                return cls._make_candidate(
                    run=run, item=item, product=product, draft=draft,
                    source_evidence=evidence, product_type=product_type,
                    definition=definition, literal=literal,
                    deterministic_cell=canonical_cell,
                    draft_id=draft_id, draft_version=draft_version,
                )
            if canonical_reason in {
                "dictionary_value_ambiguous",
                "dictionary_value_restricted",
            }:
                return None
        elif len(canonical_colors) > 1:
            return None

        choices = cls._shortlisted_dictionary_values(
            definition=definition,
            literal=literal,
        )
        if len(choices) < 2:
            return None
        return cls._make_candidate(
            run=run, item=item, product=product, draft=draft,
            source_evidence=evidence, product_type=product_type,
            definition=definition, literal=literal, choices=choices,
            draft_id=draft_id, draft_version=draft_version,
        )

    @classmethod
    def _make_candidate(
        cls,
        *,
        run: OzonBulkUploadRun,
        item: OzonBulkUploadItem,
        product: ImportedProduct,
        draft,
        source_evidence: dict,
        product_type,
        definition: MarketplaceAttributeDefinition,
        literal: str,
        choices: tuple | list = (),
        deterministic_cell: Optional[dict] = None,
        draft_id: Optional[int] = None,
        draft_version: Optional[int] = None,
    ) -> _Candidate:
        return _Candidate(
            item_id=item.id,
            draft_id=draft_id if draft_id is not None else getattr(draft, "id", None),
            draft_version=draft_version if draft_version is not None else getattr(draft, "version", None),
            context=cls._context(
                run=run, product=product, source_evidence=source_evidence,
                product_type=product_type, definition=definition,
                literal=literal, choices=choices,
            ),
            choice_rows={
                row.external_value_id: row.value
                for row, _score in choices
            },
            deterministic_cell=deterministic_cell,
        )

    @classmethod
    def _context(
        cls,
        *,
        run: OzonBulkUploadRun,
        product: ImportedProduct,
        source_evidence: dict,
        product_type,
        definition: MarketplaceAttributeDefinition,
        literal: str,
        choices: tuple | list,
    ) -> CharacteristicProductContext:
        identity = source_evidence.get("identity")
        if not isinstance(identity, dict):
            raise ValueError("source_identity_unavailable")
        source_scope = cls._digest({
            "seller_id": run.seller_id,
            "account_id": run.account_id,
            "imported_product_id": product.id,
            "supplier_identity": identity,
            "observed_color": literal,
        })
        product_key = (
            f"s{run.seller_id}:a{run.account_id}:p{product.id}:"
            f"source-{source_scope}"
        )
        reference_version = cls._reference_version(
            product=product,
            source_evidence=source_evidence,
            product_type=product_type,
            definition=definition,
        )
        dictionary_version = (
            f"{definition.values_version}:{definition.values_snapshot_hash}"
        )
        schema_version = str(product_type.attributes_version)
        dictionary_choices = tuple(
            DictionaryValueChoice(
                marketplace="ozon",
                reference_version=reference_version,
                type_id=product_type.external_type_id,
                schema_version=schema_version,
                schema_hash=product_type.attributes_schema_hash,
                attribute_id=COLOR_ATTRIBUTE_ID,
                dictionary_version=dictionary_version,
                value_id=row.external_value_id,
                value=row.value,
            )
            for row, _score in choices
        )
        return CharacteristicProductContext(
            product_key=product_key,
            marketplace="ozon",
            reference_version=reference_version,
            current_type_id=product_type.external_type_id,
            current_type_name=product_type.name,
            schema_version=schema_version,
            schema_hash=product_type.attributes_schema_hash,
            source_fields={"color": literal},
            attributes=(CharacteristicAttributeContext(
                attribute_id=COLOR_ATTRIBUTE_ID,
                attribute_name=definition.name,
                choices=dictionary_choices,
                existing_value=False,
                manual_value=False,
                regulatory=False,
            ),),
        )

    @classmethod
    def _reference_version(
        cls,
        *,
        product: ImportedProduct,
        source_evidence: dict,
        product_type,
        definition: MarketplaceAttributeDefinition,
    ) -> str:
        category = product_type.category
        source_identity = source_evidence.get("identity")
        if not isinstance(source_identity, dict):
            raise ValueError("source_identity_unavailable")
        return cls._digest({
            "marketplace": "ozon",
            "imported_product_id": product.id,
            "source_identity": source_identity,
            "category_id": category.id,
            "external_category_id": category.external_category_id,
            "product_type_id": product_type.id,
            "external_type_id": product_type.external_type_id,
            "type_name": product_type.name,
            "schema_version": product_type.attributes_version,
            "schema_hash": product_type.attributes_schema_hash,
            "attribute_row_id": definition.id,
            "attribute_id": COLOR_ATTRIBUTE_ID,
            "dictionary_id": COLOR_DICTIONARY_ID,
            "dictionary_version": definition.values_version,
            "dictionary_hash": definition.values_snapshot_hash,
            "restriction_ids": sorted(definition.restriction_value_ids),
        })

    @classmethod
    def _shortlisted_dictionary_values(
        cls,
        *,
        definition: MarketplaceAttributeDefinition,
        literal: str,
    ) -> list[tuple[MarketplaceAttributeValue, int]]:
        rows = MarketplaceAttributeValue.query.filter(
            MarketplaceAttributeValue.attribute_id == definition.id,
            MarketplaceAttributeValue.product_type_id == definition.product_type_id,
            MarketplaceAttributeValue.marketplace_id == definition.marketplace_id,
            MarketplaceAttributeValue.is_available.is_(True),
        ).order_by(
            MarketplaceAttributeValue.external_value_id.asc(),
        ).limit(MAX_DICTIONARY_SCAN + 1).all()
        if len(rows) > MAX_DICTIONARY_SCAN:
            return []
        restrictions = set(definition.restriction_value_ids)
        allowed = [
            row for row in rows
            if not restrictions or row.external_value_id in restrictions
        ]
        normalized_literal = OzonBulkRepairService._source_literal_dictionary_text(
            literal,
        ).casefold().replace("ё", "е")
        literal_tokens = cls._tokens(normalized_literal)
        if not literal_tokens:
            return []
        scored_by_token: dict[str, dict[str, tuple[MarketplaceAttributeValue, int]]] = {
            token: {} for token in literal_tokens
        }
        seen_labels = set()
        for row in allowed:
            if (
                not isinstance(row.external_value_id, str)
                or not row.external_value_id
                or not isinstance(row.value, str)
                or not row.value.strip()
                or len(row.value) > 300
            ):
                continue
            normalized_value = OzonReferenceService.normalize_value(
                row.value,
            ).replace("ё", "е")
            if normalized_value in seen_labels:
                # Duplicate visible labels cannot produce a safe value ID.
                return []
            seen_labels.add(normalized_value)
            value_tokens = cls._tokens(normalized_value)
            for token in literal_tokens:
                score = cls._token_evidence_score({token}, value_tokens)
                if score:
                    scored_by_token[token][row.external_value_id] = (row, score)
        anchors = [
            token for token, rows_by_id in scored_by_token.items()
            if len(rows_by_id) >= 2
        ]
        if len(anchors) != 1:
            return []
        anchor = anchors[0]
        scored = list(scored_by_token[anchor].values())
        highest = max(score for _row, score in scored)
        # Retain only close, evidence-sharing choices. If the bounded result
        # would require arbitrary truncation, abstain instead.
        selected = [
            pair for pair in scored
            if pair[1] >= highest - 1
        ]
        selected_ids = {row.external_value_id for row, _score in selected}
        if any(
            row.external_value_id not in selected_ids
            for token, rows_by_id in scored_by_token.items()
            if token != anchor
            for row, _score in rows_by_id.values()
        ):
            return []
        if not 2 <= len(selected) <= MAX_SHORTLIST_CHOICES:
            return []
        return sorted(selected, key=lambda pair: pair[0].external_value_id)

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return {
            token for token in re.findall(r"[a-zа-яё0-9]+", value.casefold())
            if len(token) >= 3 and token not in {"цвет", "цвета", "color", "the"}
        }

    @staticmethod
    def _token_evidence_score(source_tokens: set[str], choice_tokens: set[str]) -> int:
        score = 0
        for source in source_tokens:
            for choice in choice_tokens:
                if source == choice:
                    score += 2
                elif min(len(source), len(choice)) >= 5 and (
                    source.startswith(choice) or choice.startswith(source)
                ):
                    score += 1
                elif min(len(source), len(choice)) >= 6 and (
                    source in choice or choice in source
                ):
                    score += 1
        return score

    @classmethod
    def _selected_cell(
        cls,
        option: CharacteristicOption,
        *,
        candidate: _Candidate,
    ) -> Optional[dict]:
        if (
            option.kind != "dictionary"
            or option.marketplace != "ozon"
            or option.reference_version != candidate.context.reference_version
            or option.type_id != candidate.context.current_type_id
            or option.schema_version != candidate.context.schema_version
            or option.schema_hash != candidate.context.schema_hash
            or option.attribute_id != COLOR_ATTRIBUTE_ID
            or option.dictionary_value_id not in candidate.choice_rows
        ):
            return None
        expected = next(
            (
                choice for choice in candidate.context.attributes[0].choices
                if choice.value_id == option.dictionary_value_id
            ),
            None,
        )
        if (
            expected is None
            or expected.marketplace != option.marketplace
            or expected.reference_version != option.reference_version
            or expected.type_id != option.type_id
            or expected.schema_version != option.schema_version
            or expected.schema_hash != option.schema_hash
            or expected.attribute_id != option.attribute_id
            or expected.dictionary_version != option.dictionary_version
            or expected.value != option.value
        ):
            return None
        value = candidate.choice_rows[option.dictionary_value_id]
        if option.value != value:
            return None
        return {
            "value": value,
            "dictionary_value_id": option.dictionary_value_id,
        }

    @classmethod
    def _apply_current_cell(
        cls,
        *,
        run: OzonBulkUploadRun,
        item: OzonBulkUploadItem,
        candidate: _Candidate,
        cell: dict,
        lease_token: str,
        expected_fingerprint: Optional[str] = None,
        selected_option: Optional[CharacteristicOption] = None,
    ) -> bool:
        from services.ozon_upload_queue import OzonUploadQueueService

        if db.session.new or db.session.dirty or db.session.deleted:
            db.session.rollback()
            return False
        db.session.expire_all()
        current_time = datetime.utcnow()
        try:
            # Acquire the queue's run CAS before the authoritative re-read.
            # The resulting short writer transaction includes the draft CAS.
            OzonUploadQueueService._fence_lease(
                run_id=run.id,
                lease_token=lease_token,
                now=current_time,
            )
            fresh_run = OzonBulkUploadRun.query.filter_by(
                id=run.id,
                seller_id=run.seller_id,
                account_id=run.account_id,
                mode="source_prepare",
                state="active",
            ).first()
            fresh_item = OzonBulkUploadItem.query.filter_by(
                id=item.id, run_id=run.id,
            ).first()
            draft = cls._owned_draft(
                seller_id=run.seller_id,
                account_id=run.account_id,
                imported_product_id=item.imported_product_id,
                draft_id=candidate.draft_id,
            )
            if (
                fresh_run is None
                or fresh_item is None
                or draft is None
                or not cls._lease_matches(
                    fresh_run, lease_token=lease_token, now=current_time,
                )
                or fresh_item.phase != "preparing"
                or fresh_item.operation_id is not None
                or fresh_item.draft_id != draft.id
                or draft.version != candidate.draft_version
                or draft.published_listing_id is not None
            ):
                db.session.rollback()
                return False
            fresh_candidate = cls._candidate_for_new_draft(
                run=fresh_run,
                item=fresh_item,
                draft=draft,
            )
            if (
                fresh_candidate is None
                or fresh_candidate.context != candidate.context
                or fresh_candidate.draft_version != candidate.draft_version
            ):
                db.session.rollback()
                return False
            if expected_fingerprint is None:
                if (
                    fresh_candidate.deterministic_cell is None
                    or fresh_candidate.deterministic_cell != cell
                ):
                    db.session.rollback()
                    return False
            else:
                if type(selected_option) is not CharacteristicOption:
                    db.session.rollback()
                    return False
                current_plan = plan_characteristic_phase(
                    [fresh_candidate.context],
                )
                if (
                    fresh_candidate.deterministic_cell is not None
                    or current_plan.question_fingerprints != (expected_fingerprint,)
                ):
                    db.session.rollback()
                    return False
                fresh_cell = cls._selected_cell(
                    selected_option,
                    candidate=fresh_candidate,
                )
                if fresh_cell != cell:
                    db.session.rollback()
                    return False

            attributes = json.loads(draft.attributes_json or "[]")
            matches = [
                index for index, row in enumerate(attributes)
                if isinstance(row, dict)
                and row.get("attribute_id") == COLOR_ATTRIBUTE_ID
                and row.get("complex_id", "0") == "0"
            ]
            if len(matches) > 1:
                db.session.rollback()
                return False
            color_row = {
                "attribute_id": COLOR_ATTRIBUTE_ID,
                "complex_id": "0",
                "values": [cell],
            }
            if matches:
                attributes[matches[0]] = color_row
            else:
                attributes.append(color_row)
            MarketplaceDraftService._update_draft_core(
                seller_id=run.seller_id,
                draft_id=draft.id,
                expected_version=draft.version,
                patch={"attributes": attributes},
                commit=False,
            )
            db.session.commit()
            return True
        except Exception as exc:
            db.session.rollback()
            from services.ozon_upload_queue import OzonUploadLeaseLost
            if isinstance(exc, OzonUploadLeaseLost):
                raise
            return False

    @classmethod
    def _owned_draft(
        cls,
        *,
        seller_id: int,
        account_id: int,
        imported_product_id: int,
        draft_id: Optional[int],
    ):
        if draft_id is None:
            return None
        return MarketplaceProductDraft.query.filter_by(
            id=draft_id,
            seller_id=seller_id,
            account_id=account_id,
            imported_product_id=imported_product_id,
        ).first()

    @classmethod
    def _has_color_removal(cls, draft) -> bool:
        try:
            removals = json.loads(
                getattr(draft, "attribute_removals_json", "[]") or "[]",
            )
        except (TypeError, ValueError, RecursionError):
            return True
        if type(removals) is not list:
            return True
        return any(
            isinstance(row, dict)
            and row.get("attribute_id") == COLOR_ATTRIBUTE_ID
            and row.get("complex_id", "0") == "0"
            for row in removals
        )

    @staticmethod
    def _attribute_json_is_valid(draft) -> bool:
        try:
            attributes = json.loads(
                getattr(draft, "attributes_json", "[]") or "[]",
            )
        except (TypeError, ValueError, RecursionError):
            return False
        return type(attributes) is list and all(
            isinstance(row, dict) for row in attributes
        )

    @classmethod
    def _has_color_value(cls, attributes: Any) -> bool:
        if not isinstance(attributes, list):
            return True
        for row in attributes:
            if (
                isinstance(row, dict)
                and row.get("attribute_id") == COLOR_ATTRIBUTE_ID
                and row.get("complex_id", "0") == "0"
                and row.get("values")
            ):
                return True
        return False

    @classmethod
    def _draft_has_active_operation(cls, draft) -> bool:
        return MarketplaceOperation.query.filter(
            MarketplaceOperation.seller_id == draft.seller_id,
            MarketplaceOperation.account_id == draft.account_id,
            MarketplaceOperation.draft_id == draft.id,
            MarketplaceOperation.status.in_(
                MarketplacePublicationService.ACTIVE_STATUSES,
            ),
        ).first() is not None

    @staticmethod
    def _has_exact_listing(
        *, seller_id: int, account_id: int, imported_product_id: int,
    ) -> bool:
        return MarketplaceListing.query.filter_by(
            seller_id=seller_id,
            account_id=account_id,
            imported_product_id=imported_product_id,
        ).first() is not None

    @staticmethod
    def _has_offer_collision(
        *, account_id: int, offer_id: str, except_draft_id: Optional[int] = None,
    ) -> bool:
        drafts = MarketplaceProductDraft.query.filter(
            MarketplaceProductDraft.account_id == account_id,
            MarketplaceProductDraft.offer_id == offer_id,
        )
        if except_draft_id is not None:
            drafts = drafts.filter(MarketplaceProductDraft.id != except_draft_id)
        if drafts.first() is not None:
            return True
        return MarketplaceListing.query.filter_by(
            account_id=account_id,
            offer_id=offer_id,
        ).first() is not None

    @staticmethod
    def _lease_matches(
        run: OzonBulkUploadRun, *, lease_token: str, now: datetime,
    ) -> bool:
        return bool(
            run.state == "active"
            and run.mode == "source_prepare"
            and run.lease_token == lease_token
            and run.lease_until is not None
            and run.lease_until > now
        )

    @staticmethod
    def _digest(value: dict) -> str:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

__all__ = [
    "COLOR_ATTRIBUTE_ID",
    "COLOR_DICTIONARY_ID",
    "MAX_WINDOW_ITEMS",
    "MIN_SELECTED_CONFIDENCE",
    "OzonPrepareDecisionsService",
]
