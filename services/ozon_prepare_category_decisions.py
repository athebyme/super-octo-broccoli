"""Read-only candidate construction for exact Ozon source preparation.

This module does not create category mappings or modify drafts. It admits an
AI suggestion only for a queue-owned, untyped draft with exact observed source
taxonomy evidence and a fresh local Ozon category tree.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import unicodedata
from typing import Optional, Tuple

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceCategoryMapping,
    MarketplaceListing,
    MarketplaceProductDraft,
    MarketplaceOperation,
    MarketplaceProductType,
    MarketplaceTaxonomyCategory,
    OzonBulkUploadItem,
    OzonBulkUploadRun,
    SellerMarketplaceAccount,
)
from services.marketplace_decisions_preparation import (
    CategoryOption,
    CategoryProductContext,
    DecisionItemResult,
    ExactCategoryTypeChoice,
    plan_category_phase,
)
from services.marketplace_drafts import MarketplaceDraftService
from services.marketplace_fact_pack import (
    MarketplaceFactPackBuilder,
    MarketplaceFactPackError,
)
from services.ozon_reference_service import OzonReferenceService


_MAX_TREE_ROWS = 100_000
_MAX_TYPES = 8
_MAX_WINDOW_PRODUCTS = 12
_MAX_SIBLING_INPUTS = 40
_ACTIVE_OPERATION_STATUSES = frozenset({
    "queued", "submitting", "submitted", "polling", "uncertain",
})
_PLACEHOLDER_NAMES = frozenset({
    "товар", "product", "item", "без названия", "не указано", "unknown",
})
_ADULT_MARKERS = (
    "18+", "эрот", "секс", "вибратор", "мастурб", "фалло", "лубрикант",
    "анальн", "страпон", "презерватив", "интим",
)


@dataclass(frozen=True)
class OzonPrepareCategoryTypeCandidate:
    """One local product type paired with its exact provider-facing identity."""

    product_type_id: int
    choice: ExactCategoryTypeChoice = field(repr=False)


@dataclass(frozen=True)
class OzonCategoryTreeNode:
    """Small projected category row reused across one preparation batch."""

    local_id: int
    external_id: str
    name: str
    full_path: str
    is_available: bool
    is_disabled_upstream: bool


@dataclass(frozen=True)
class OzonCategoryTreeIndex:
    """Bounded local view of one fresh, complete Ozon category snapshot."""

    marketplace_id: int
    tree_version: int
    snapshot_hash: str
    synced_at: datetime
    path_matches: Tuple[
        Tuple[Tuple[str, ...], int, Optional[OzonCategoryTreeNode]], ...
    ] = field(repr=False)
    leaf_matches: Tuple[
        Tuple[str, int, Optional[OzonCategoryTreeNode]], ...
    ] = field(repr=False)


@dataclass(frozen=True)
class OzonPrepareCategoryCandidate:
    """A scoped planner input and the local IDs needed to recheck its result."""

    context: CategoryProductContext = field(repr=False)
    product_types: Tuple[OzonPrepareCategoryTypeCandidate, ...] = field(repr=False)
    question_fingerprint: str
    candidate_signature: str
    scope_signature: str
    match_kind: str
    tree_version: int
    tree_snapshot_hash: str


@dataclass(frozen=True)
class OzonPrepareCategorySiblingCandidate:
    """Read-only virtual source context for one unlinked sibling product."""

    context: CategoryProductContext = field(repr=False)
    product_types: Tuple[OzonPrepareCategoryTypeCandidate, ...] = field(repr=False)
    candidate_signature: str


@dataclass(frozen=True)
class OzonPrepareCategoryWindowEntry:
    """Planner result paired with the candidate signature used in its prompt."""

    product_key: str
    candidate_signature: str
    result: DecisionItemResult = field(repr=False)


@dataclass(frozen=True)
class OzonPrepareCategoryWindowResult:
    """One bounded category phase and its current-draft resolution, if safe."""

    entries: Tuple[OzonPrepareCategoryWindowEntry, ...] = field(repr=False)
    current_candidate: OzonPrepareCategoryCandidate = field(repr=False)
    current_result: DecisionItemResult = field(repr=False)
    current_product_type_id: Optional[int]
    physical_request_started: bool
    reservation_state: str
    finish_state: Optional[str]
    retry_at: Optional[datetime] = None
    requires_current_scope_recheck: bool = True


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _path_parts(value: str) -> Tuple[str, ...]:
    parts = re.split(r"\s*(?:>|/)\s*", value)
    return tuple(_normalized(part).replace("ё", "е") for part in parts)


def _canonical_external_id(value: object) -> Optional[str]:
    if type(value) is not str or not value.isascii() or not value.isdigit():
        return None
    if value.startswith("0") or len(value) > 100:
        return None
    return value


def _sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_observation(
    product: ImportedProduct,
) -> Optional[Tuple[str, str, str, str]]:
    """Require exact linked supplier snapshot, current revision, and SKU parity."""
    supplier_product = product.supplier_product
    supplier = supplier_product.supplier if supplier_product is not None else None
    if (
        supplier_product is None
        or supplier is None
        or product.supplier_product_id != supplier_product.id
        or product.supplier_id is None
        or product.supplier_id != supplier_product.supplier_id
        or product.supplier_content_revision != supplier_product.content_revision
        or product.source_type != supplier.code
        or product.external_id != supplier_product.external_id
        or product.external_vendor_code != supplier_product.vendor_code
        or not any((supplier_product.external_id, supplier_product.vendor_code))
    ):
        return None
    try:
        imported_original = json.loads(product.original_data or "{}")
        supplier_original = json.loads(supplier_product.original_data_json or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(imported_original, dict) or not isinstance(supplier_original, dict):
        return None
    source_title = supplier_original.get("title")
    source_category = supplier_original.get("category")
    if (
        not isinstance(source_title, str)
        or not source_title.strip()
        or not isinstance(source_category, str)
        or not source_category.strip()
        or imported_original.get("title") != source_title
        or imported_original.get("category") != source_category
    ):
        return None
    try:
        pack = MarketplaceFactPackBuilder.build(product)
    except MarketplaceFactPackError:
        return None
    facts = pack.get("facts")
    provenance = pack.get("provenance")
    identity = facts.get("identity") if isinstance(facts, dict) else None
    if not isinstance(identity, dict) or not isinstance(provenance, dict):
        return None
    title = identity.get("source_title")
    category = identity.get("source_category")
    title_provenance = provenance.get("identity.source_title")
    category_provenance = provenance.get("identity.source_category")
    if (
        not isinstance(title_provenance, dict)
        or title_provenance.get("trust") != "observed"
        or not isinstance(category_provenance, dict)
        or category_provenance.get("trust") != "observed"
        or not isinstance(title, str)
        or not isinstance(category, str)
        or title != source_title
        or category != source_category
    ):
        return None
    clean_title = " ".join(title.split())
    clean_category = " ".join(category.split())
    normalized_title = _normalized(clean_title)
    if (
        len(clean_title) < 3
        or len(clean_title.encode("utf-8")) > 500
        or normalized_title in _PLACEHOLDER_NAMES
        or sum(character.isalpha() for character in clean_title) < 3
        or not clean_category
        or len(clean_category.encode("utf-8")) > 300
    ):
        return None
    fact_signature = _sha256({
        "fact_hash": pack.get("fact_hash"),
        "source_title": clean_title,
        "source_category": clean_category,
        "title_source": title_provenance.get("source"),
        "category_source": category_provenance.get("source"),
    })
    fact_hash = pack.get("fact_hash")
    if type(fact_hash) is not str or re.fullmatch(r"[0-9a-f]{64}", fact_hash) is None:
        return None
    return clean_title, clean_category, fact_hash, fact_signature


def _category_match(
    source_category: str,
    index: OzonCategoryTreeIndex,
) -> Optional[Tuple[OzonCategoryTreeNode, str]]:
    source_parts = _path_parts(source_category)
    if not source_parts or any(not part for part in source_parts):
        return None
    if len(source_parts) > 1:
        match = next((
            (count, node) for path, count, node in index.path_matches
            if path == source_parts
        ), None)
        if match is None or match[0] != 1 or match[1] is None:
            return None
        return match[1], "full_path"

    # A bare source label is usable only when it names one category in the
    # entire current tree, including rows currently disabled upstream.
    match = next((
        (count, node) for label, count, node in index.leaf_matches
        if label == source_parts[0]
    ), None)
    if match is None or match[0] != 1 or match[1] is None:
        return None
    return match[1], "unique_leaf"


def _adult_marker_present(value: str) -> bool:
    normalized = _normalized(value).replace("ё", "е")
    return any(marker in normalized for marker in _ADULT_MARKERS)


def _mapping_exists_for_source(
    *,
    seller_id: int,
    marketplace_id: int,
    product: ImportedProduct,
    source_category: str,
) -> bool:
    """Treat any stored mapping state as authoritative, including disabled."""
    identities = MarketplaceDraftService._mapping_identities(product)
    scope_keys_and_categories = {
        (identity.get("scope_key"), identity.get("source_category_normalized"))
        for identity in identities
        if isinstance(identity.get("scope_key"), str)
        and isinstance(identity.get("source_category_normalized"), str)
    }
    scope_keys_and_categories.add((
        MarketplaceDraftService._scope_key(product),
        MarketplaceDraftService._normalized_text(source_category),
    ))
    for scope_key, normalized_category in scope_keys_and_categories:
        if not isinstance(scope_key, str) or not isinstance(normalized_category, str):
            continue
        if MarketplaceCategoryMapping.query.filter_by(
            seller_id=seller_id,
            marketplace_id=marketplace_id,
            scope_key=scope_key,
            source_category_normalized=normalized_category,
        ).first() is not None:
            return True
    return False


def _draft_and_claim_are_exact(
    *,
    run: OzonBulkUploadRun,
    item: OzonBulkUploadItem,
    account: SellerMarketplaceAccount,
    product: ImportedProduct,
    draft: MarketplaceProductDraft,
    prior_draft_id_before_create: Optional[int],
    item_draft_id_before_create: Optional[int],
    item_prepared_version_before_create: Optional[int],
    now: datetime,
) -> bool:
    if not all(type(value) is int and value > 0 for value in (
        run.id, item.id, account.id, product.id, draft.id,
    )):
        return False
    if not (
        run.mode == "source_prepare"
        and run.state == "active"
        and run.lease_token
        and run.lease_until is not None
        and run.lease_until > now
        and run.seller_id == account.seller_id == product.seller_id == draft.seller_id
        and run.account_id == account.id == draft.account_id
        and draft.marketplace_id == account.marketplace_id
        and item.run_id == run.id
        and item.imported_product_id == product.id == draft.imported_product_id
        and item.phase == "preparing"
        and item.draft_id == draft.id
        and item.prepared_version == draft.version
        and draft.status == "needs_category"
        and draft.published_listing_id is None
        and draft.product_type_id is None
        and draft.category_mapping_id is None
        and draft.external_category_id is None
        and draft.external_type_id is None
    ):
        return False
    if not (
        account.is_active
        and account.marketplace is not None
        and account.marketplace.code == "ozon"
        and account.marketplace.is_active
    ):
        return False

    # Initial preparation requires no prior draft. A retry is permitted only
    # if the exact prepared version was captured before the transition.
    return bool(
        prior_draft_id_before_create is None
        and item_draft_id_before_create is None
        and item_prepared_version_before_create is None
    ) or bool(
        type(prior_draft_id_before_create) is int
        and prior_draft_id_before_create == draft.id
        and item_draft_id_before_create == draft.id
        and item_prepared_version_before_create == draft.version
    )


def build_ozon_category_tree_index(
    marketplace: Marketplace,
    source_categories: Tuple[str, ...] | list,
    *,
    now: Optional[datetime] = None,
) -> Optional[OzonCategoryTreeIndex]:
    """Scan projected tree rows once, retaining only requested exact matches."""
    if type(marketplace) is not Marketplace:
        return None
    if type(source_categories) not in (tuple, list) or len(source_categories) > 12:
        return None
    requested_paths = set()
    requested_leaves = set()
    for source_category in source_categories:
        if type(source_category) is not str or len(source_category.encode("utf-8")) > 500:
            return None
        parts = _path_parts(source_category)
        if not parts or any(not part for part in parts):
            return None
        if len(parts) > 1:
            requested_paths.add(parts)
        else:
            requested_leaves.add(parts[0])
    current_time = now or datetime.utcnow()
    if current_time.tzinfo is not None:
        current_time = current_time.astimezone(timezone.utc).replace(tzinfo=None)
    if not OzonReferenceService.tree_is_fresh(marketplace, now=current_time):
        return None
    rows = MarketplaceTaxonomyCategory.query.with_entities(
        MarketplaceTaxonomyCategory.id,
        MarketplaceTaxonomyCategory.external_category_id,
        MarketplaceTaxonomyCategory.name,
        MarketplaceTaxonomyCategory.full_path,
        MarketplaceTaxonomyCategory.is_available,
        MarketplaceTaxonomyCategory.is_disabled_upstream,
    ).filter_by(
        marketplace_id=marketplace.id,
    ).order_by(
        MarketplaceTaxonomyCategory.id.asc(),
    ).yield_per(2000)
    path_matches = {key: [0, None] for key in requested_paths}
    leaf_matches = {key: [0, None] for key in requested_leaves}
    row_count = 0
    for row in rows:
        row_count += 1
        if row_count > _MAX_TREE_ROWS:
            return None
        if (
            type(row.id) is not int
            or row.id <= 0
            or not isinstance(row.external_category_id, str)
            or not isinstance(row.name, str)
            or not isinstance(row.full_path, str)
            or len(row.name.encode("utf-8")) > 500
            or len(row.full_path.encode("utf-8")) > 2_000
        ):
            return None
        path_key = _path_parts(row.full_path)
        leaf_key = _normalized(row.name).replace("ё", "е")
        matched_keys = []
        if path_key in path_matches:
            matched_keys.append((path_matches[path_key], path_key))
        if leaf_key in leaf_matches:
            matched_keys.append((leaf_matches[leaf_key], leaf_key))
        if not matched_keys:
            continue
        node = OzonCategoryTreeNode(
            local_id=row.id,
            external_id=row.external_category_id,
            name=row.name,
            full_path=row.full_path,
            is_available=bool(row.is_available),
            is_disabled_upstream=bool(row.is_disabled_upstream),
        )
        for state, _key in matched_keys:
            state[0] = min(2, state[0] + 1)
            if state[1] is None:
                state[1] = node
    if not row_count:
        return None
    return OzonCategoryTreeIndex(
        marketplace_id=marketplace.id,
        tree_version=marketplace.categories_version,
        snapshot_hash=marketplace.categories_snapshot_hash,
        synced_at=marketplace.categories_synced_at,
        path_matches=tuple(
            (key, state[0], state[1])
            for key, state in sorted(path_matches.items())
        ),
        leaf_matches=tuple(
            (key, state[0], state[1])
            for key, state in sorted(leaf_matches.items())
        ),
    )


def build_ozon_prepare_category_candidate(
    *,
    run: OzonBulkUploadRun,
    item: OzonBulkUploadItem,
    account: SellerMarketplaceAccount,
    product: ImportedProduct,
    draft: MarketplaceProductDraft,
    tree_index: OzonCategoryTreeIndex,
    prior_draft_id_before_create: Optional[int],
    item_draft_id_before_create: Optional[int],
    item_prepared_version_before_create: Optional[int],
    now: Optional[datetime] = None,
) -> Optional[OzonPrepareCategoryCandidate]:
    """Return one exact, fresh-tree category/type shortlist without writing.

    The pre-create IDs and version are captured by the queue before
    ``create_draft``. A manual draft cannot enter the initial new-draft branch;
    a retry needs the exact prepared version captured before the transition.
    """
    if not all(type(value) is expected for value, expected in (
        (run, OzonBulkUploadRun),
        (item, OzonBulkUploadItem),
        (account, SellerMarketplaceAccount),
        (product, ImportedProduct),
        (draft, MarketplaceProductDraft),
        (tree_index, OzonCategoryTreeIndex),
    )):
        return None
    current_time = now or datetime.utcnow()
    if current_time.tzinfo is not None:
        current_time = current_time.astimezone(timezone.utc).replace(tzinfo=None)
    if not _draft_and_claim_are_exact(
        run=run,
        item=item,
        account=account,
        product=product,
        draft=draft,
        prior_draft_id_before_create=prior_draft_id_before_create,
        item_draft_id_before_create=item_draft_id_before_create,
        item_prepared_version_before_create=item_prepared_version_before_create,
        now=current_time,
    ):
        return None
    # Preserve Ozon+WB products. Only an exact target-account Ozon listing
    # makes this a category-change path.
    if MarketplaceListing.query.filter_by(
        seller_id=run.seller_id,
        marketplace_id=account.marketplace_id,
        account_id=account.id,
        imported_product_id=product.id,
    ).limit(1).first() is not None:
        return None

    observation = _source_observation(product)
    if observation is None:
        return None
    source_title, source_category, source_fact_hash, source_fact_signature = observation
    if draft.source_fact_hash != source_fact_hash:
        return None
    if _mapping_exists_for_source(
        seller_id=run.seller_id,
        marketplace_id=account.marketplace_id,
        product=product,
        source_category=source_category,
    ):
        return None
    if MarketplaceDraftService._explicit_source_mapping_candidate(product) is not None:
        # Deterministic source recipes remain owned by create_draft's normal
        # rule path; category AI never competes with them.
        return None
    if MarketplaceOperation.query.filter_by(
        seller_id=run.seller_id,
        marketplace_id=account.marketplace_id,
        account_id=account.id,
        draft_id=draft.id,
    ).filter(
        MarketplaceOperation.status.in_(_ACTIVE_OPERATION_STATUSES),
    ).first() is not None:
        return None

    marketplace: Marketplace = account.marketplace
    if (
        tree_index.marketplace_id != marketplace.id
        or tree_index.tree_version != marketplace.categories_version
        or tree_index.snapshot_hash != marketplace.categories_snapshot_hash
        or tree_index.synced_at != marketplace.categories_synced_at
        or not OzonReferenceService.tree_is_fresh(marketplace, now=current_time)
    ):
        return None
    matched = _category_match(source_category, tree_index)
    if matched is None:
        return None
    category, match_kind = matched
    if not (
        category.local_id > 0
        and isinstance(category.name, str)
        and category.name.strip()
        and len(category.name.encode("utf-8")) <= 300
        and isinstance(category.full_path, str)
        and category.full_path.strip()
        and len(category.full_path.encode("utf-8")) <= 2_000
        and category.is_available
        and not category.is_disabled_upstream
    ):
        return None
    category_external_id = _canonical_external_id(category.external_id)
    if category_external_id is None:
        return None

    source_adult = _adult_marker_present(source_title) or _adult_marker_present(source_category)
    category_adult = _adult_marker_present(category.full_path)
    if source_adult != category_adult or (
        source_adult and match_kind != "full_path"
    ):
        return None

    types = tuple(
        MarketplaceProductType.query.filter_by(
            marketplace_id=marketplace.id,
            category_id=category.local_id,
            is_available=True,
            is_seller_selectable=True,
            is_disabled_upstream=False,
        ).order_by(
            MarketplaceProductType.external_type_id.asc(),
            MarketplaceProductType.id.asc(),
        ).limit(_MAX_TYPES + 1).all()
    )
    if not 2 <= len(types) <= _MAX_TYPES:
        return None

    type_candidates = []
    context_choices = []
    for product_type in types:
        external_type_id = _canonical_external_id(product_type.external_type_id)
        if (
            type(product_type.id) is not int
            or product_type.id <= 0
            or external_type_id is None
            or product_type.category_id != category.local_id
            or not isinstance(product_type.name, str)
            or not product_type.name.strip()
            or len(product_type.name.encode("utf-8")) > 300
        ):
            return None
        choice = ExactCategoryTypeChoice(
            marketplace="ozon",
            reference_version=(
                f"ozon-tree-v{marketplace.categories_version}-"
                f"{marketplace.categories_snapshot_hash}"
            ),
            category_id=category_external_id,
            category_name=category.name.strip(),
            type_id=external_type_id,
            type_name=product_type.name.strip(),
        )
        context_choices.append(choice)
        type_candidates.append(OzonPrepareCategoryTypeCandidate(
            product_type_id=product_type.id,
            choice=choice,
        ))

    reference_version = context_choices[0].reference_version
    if any(choice.reference_version != reference_version for choice in context_choices):
        return None
    context = CategoryProductContext(
        product_key=(
            f"seller-{run.seller_id}-account-{account.id}-product-{product.id}"
        ),
        marketplace="ozon",
        reference_version=reference_version,
        source_fields={"name": source_title, "category": source_category},
        unresolved=True,
        choices=tuple(context_choices),
    )
    question_fingerprints = plan_category_phase([context]).question_fingerprints
    if len(question_fingerprints) != 1:
        return None
    question_fingerprint = question_fingerprints[0]
    scope_document = {
        "version": 1,
        "seller_id": run.seller_id,
        "account_id": account.id,
        "product_id": product.id,
        "draft_id": draft.id,
        "draft_version": draft.version,
        "run_id": run.id,
        "item_id": item.id,
        "source_fact_signature": source_fact_signature,
        "supplier_link": {
            "supplier_id": product.supplier_id,
            "supplier_product_id": product.supplier_product_id,
            "content_revision": product.supplier_content_revision,
            "external_id_signature": _sha256(product.external_id),
            "vendor_code_signature": _sha256(product.external_vendor_code),
        },
        "source_category_normalized": _normalized(source_category),
        "tree": {
            "version": tree_index.tree_version,
            "snapshot_hash": tree_index.snapshot_hash,
            "synced_at": tree_index.synced_at.isoformat(),
        },
        "match_kind": match_kind,
        "category": {
            "local_id": category.local_id,
            "external_id": category_external_id,
            "name_signature": _sha256(category.name),
            "path_signature": _sha256(category.full_path),
        },
        "types": [
            {
                "local_id": candidate.product_type_id,
                "external_id": candidate.choice.type_id,
                "name_signature": _sha256(candidate.choice.type_name),
                "available": True,
                "seller_selectable": True,
            }
            for candidate in type_candidates
        ],
    }
    scope_signature = _sha256(scope_document)
    candidate_signature = _sha256({
        "scope_signature": scope_signature,
        "question_fingerprint": question_fingerprint,
        "question_product_key": context.product_key,
        "choice_identities": [
            {
                "category_id": choice.category_id,
                "type_id": choice.type_id,
                "category_name": choice.category_name,
                "type_name": choice.type_name,
            }
            for choice in context_choices
        ],
    })
    return OzonPrepareCategoryCandidate(
        context=context,
        product_types=tuple(type_candidates),
        question_fingerprint=question_fingerprint,
        candidate_signature=candidate_signature,
        scope_signature=scope_signature,
        match_kind=match_kind,
        tree_version=marketplace.categories_version,
        tree_snapshot_hash=marketplace.categories_snapshot_hash,
    )


def same_ozon_prepare_category_scope(
    before: Optional[OzonPrepareCategoryCandidate],
    after: Optional[OzonPrepareCategoryCandidate],
) -> bool:
    """Compare exact local evidence snapshots taken before and after a call."""
    return bool(
        type(before) is OzonPrepareCategoryCandidate
        and type(after) is OzonPrepareCategoryCandidate
        and before.scope_signature == after.scope_signature
        and before.candidate_signature == after.candidate_signature
        and before.question_fingerprint == after.question_fingerprint
        and before.context.product_key == after.context.product_key
    )


def selected_product_type_id(
    candidate: OzonPrepareCategoryCandidate,
    result: DecisionItemResult,
) -> Optional[int]:
    """Resolve a planner-selected exact external pair to one local type ID."""
    if (
        type(candidate) is not OzonPrepareCategoryCandidate
        or type(result) is not DecisionItemResult
        or result.phase != "category"
        or result.status != "selected"
        or type(result.option) is not CategoryOption
        or type(result.confidence) not in (int, float)
        or not math.isfinite(result.confidence)
        or result.confidence < 0.95
        or result.confidence > 1.0
        or result.signature != candidate.question_fingerprint
    ):
        return None
    matches = [
        item.product_type_id
        for item in candidate.product_types
        if (
            item.choice.marketplace == result.option.marketplace
            and item.choice.reference_version == result.option.reference_version
            and item.choice.category_id == result.option.category_id
            and item.choice.category_name == result.option.category_name
            and item.choice.type_id == result.option.type_id
            and item.choice.type_name == result.option.type_name
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _sibling_preflight(
    *,
    run: OzonBulkUploadRun,
    account: SellerMarketplaceAccount,
    item: OzonBulkUploadItem,
    product: ImportedProduct,
    now: datetime,
) -> Optional[Tuple[str, str, str, str]]:
    """Return trusted source and exact offer only for a clean due sibling."""
    if not (
        type(item) is OzonBulkUploadItem
        and type(product) is ImportedProduct
        and item.run_id == run.id
        and item.imported_product_id == product.id
        and product.seller_id == run.seller_id == account.seller_id
        and run.account_id == account.id
        and item.phase in {"pending", "preparing"}
        and item.next_due_at is not None
        and item.next_due_at <= now
        and item.draft_id is None
        and item.prepared_version is None
        and item.reviewed_draft_id is None
        and item.operation_id is None
    ):
        return None
    if MarketplaceProductDraft.query.filter_by(
        seller_id=run.seller_id,
        account_id=account.id,
        imported_product_id=product.id,
    ).first() is not None:
        return None
    if MarketplaceListing.query.filter_by(
        seller_id=run.seller_id,
        marketplace_id=account.marketplace_id,
        account_id=account.id,
        imported_product_id=product.id,
    ).first() is not None:
        return None
    active = MarketplaceOperation.query.join(
        MarketplaceProductDraft,
        MarketplaceProductDraft.id == MarketplaceOperation.draft_id,
    ).filter(
        MarketplaceOperation.seller_id == run.seller_id,
        MarketplaceOperation.marketplace_id == account.marketplace_id,
        MarketplaceOperation.account_id == account.id,
        MarketplaceOperation.status.in_(_ACTIVE_OPERATION_STATUSES),
        MarketplaceProductDraft.imported_product_id == product.id,
    ).first()
    if active is not None:
        return None
    observation = _source_observation(product)
    if observation is None:
        return None
    source_title, source_category, source_fact_hash, source_signature = observation
    if _mapping_exists_for_source(
        seller_id=run.seller_id,
        marketplace_id=account.marketplace_id,
        product=product,
        source_category=source_category,
    ):
        return None
    if MarketplaceDraftService._explicit_source_mapping_candidate(product) is not None:
        return None
    try:
        facts_document, _provenance, fact_hash = MarketplaceDraftService._fact_snapshot(product)
        offer_id = MarketplaceDraftService._derive_offer_id(product, facts_document)
    except Exception:
        return None
    if fact_hash != source_fact_hash or not isinstance(offer_id, str) or not offer_id:
        return None
    # A duplicate offer in either the draft workspace or the published target
    # account makes this product ineligible as prompt context.
    if MarketplaceProductDraft.query.filter_by(
        account_id=account.id,
        offer_id=offer_id,
    ).first() is not None:
        return None
    if MarketplaceListing.query.filter_by(
        account_id=account.id,
        offer_id=offer_id,
    ).first() is not None:
        return None
    return source_title, source_category, source_fact_hash, source_signature


def _build_sibling_candidate(
    *,
    run: OzonBulkUploadRun,
    account: SellerMarketplaceAccount,
    item: OzonBulkUploadItem,
    product: ImportedProduct,
    source: Tuple[str, str, str, str],
    tree_index: OzonCategoryTreeIndex,
    now: datetime,
) -> Optional[OzonPrepareCategorySiblingCandidate]:
    if (
        tree_index.marketplace_id != account.marketplace_id
        or tree_index.tree_version != account.marketplace.categories_version
        or tree_index.snapshot_hash != account.marketplace.categories_snapshot_hash
        or tree_index.synced_at != account.marketplace.categories_synced_at
        or not OzonReferenceService.tree_is_fresh(account.marketplace, now=now)
    ):
        return None
    source_title, source_category, _source_fact_hash, source_signature = source
    matched = _category_match(source_category, tree_index)
    if matched is None:
        return None
    category, match_kind = matched
    if not (
        category.local_id > 0
        and category.name.strip()
        and len(category.name.encode("utf-8")) <= 300
        and category.full_path.strip()
        and len(category.full_path.encode("utf-8")) <= 2_000
        and category.is_available
        and not category.is_disabled_upstream
    ):
        return None
    source_adult = _adult_marker_present(source_title) or _adult_marker_present(source_category)
    category_adult = _adult_marker_present(category.full_path)
    if source_adult != category_adult or (source_adult and match_kind != "full_path"):
        return None
    category_external_id = _canonical_external_id(category.external_id)
    if category_external_id is None:
        return None
    types = tuple(MarketplaceProductType.query.filter_by(
        marketplace_id=account.marketplace_id,
        category_id=category.local_id,
        is_available=True,
        is_seller_selectable=True,
        is_disabled_upstream=False,
    ).order_by(
        MarketplaceProductType.external_type_id.asc(),
        MarketplaceProductType.id.asc(),
    ).limit(_MAX_TYPES + 1).all())
    if not 2 <= len(types) <= _MAX_TYPES:
        return None
    choices = []
    type_candidates = []
    reference_version = (
        f"ozon-tree-v{tree_index.tree_version}-{tree_index.snapshot_hash}"
    )
    for product_type in types:
        external_type_id = _canonical_external_id(product_type.external_type_id)
        if (
            type(product_type.id) is not int
            or product_type.id <= 0
            or product_type.category_id != category.local_id
            or external_type_id is None
            or not isinstance(product_type.name, str)
            or not product_type.name.strip()
            or len(product_type.name.encode("utf-8")) > 300
        ):
            return None
        choice = ExactCategoryTypeChoice(
            marketplace="ozon",
            reference_version=reference_version,
            category_id=category_external_id,
            category_name=category.name.strip(),
            type_id=external_type_id,
            type_name=product_type.name.strip(),
        )
        choices.append(choice)
        type_candidates.append(OzonPrepareCategoryTypeCandidate(
            product_type_id=product_type.id,
            choice=choice,
        ))
    context = CategoryProductContext(
        product_key=f"seller-{run.seller_id}-account-{account.id}-product-{product.id}",
        marketplace="ozon",
        reference_version=reference_version,
        source_fields={"name": source_title, "category": source_category},
        unresolved=True,
        choices=tuple(choices),
    )
    signature = _sha256({
        "version": 1,
        "seller_id": run.seller_id,
        "account_id": account.id,
        "item_id": item.id,
        "product_id": product.id,
        "source_fact_signature": source_signature,
        "tree_version": tree_index.tree_version,
        "tree_snapshot_hash": tree_index.snapshot_hash,
        "category": {
            "local_id": category.local_id,
            "external_id": category_external_id,
            "name_signature": _sha256(category.name),
            "path_signature": _sha256(category.full_path),
        },
        "types": [
            [choice.product_type_id, choice.choice.category_id, choice.choice.type_id]
            for choice in type_candidates
        ],
    })
    return OzonPrepareCategorySiblingCandidate(
        context=context,
        product_types=tuple(type_candidates),
        candidate_signature=signature,
    )


def prepare_ozon_category_window(
    *,
    run: OzonBulkUploadRun,
    account: SellerMarketplaceAccount,
    current_item: OzonBulkUploadItem,
    current_product: ImportedProduct,
    current_draft: MarketplaceProductDraft,
    prior_draft_id_before_create: Optional[int],
    item_draft_id_before_create: Optional[int],
    item_prepared_version_before_create: Optional[int],
    sibling_pairs: Tuple[Tuple[OzonBulkUploadItem, ImportedProduct], ...] | list,
    pipeline,
    now: Optional[datetime] = None,
) -> Optional[OzonPrepareCategoryWindowResult]:
    """Run one <=12-product category phase using exact due sibling context.

    Siblings are read-only prompt context. Only the current leased draft gets
    a local type suggestion, and its caller must rebuild and compare the
    current candidate scope before applying that suggestion.
    """
    if (
        type(sibling_pairs) not in (tuple, list)
        or len(sibling_pairs) > _MAX_SIBLING_INPUTS
        or type(run) is not OzonBulkUploadRun
        or type(account) is not SellerMarketplaceAccount
        or type(current_item) is not OzonBulkUploadItem
        or type(current_product) is not ImportedProduct
        or type(current_draft) is not MarketplaceProductDraft
        or not hasattr(pipeline, "run_category_phase")
    ):
        return None
    current_time = now or datetime.utcnow()
    if current_time.tzinfo is not None:
        current_time = current_time.astimezone(timezone.utc).replace(tzinfo=None)
    if not (
        run.mode == "source_prepare"
        and run.state == "active"
        and run.lease_token
        and run.lease_until is not None
        and run.lease_until > current_time
        and run.seller_id == account.seller_id == current_product.seller_id
        and run.account_id == account.id
        and account.is_active
        and account.marketplace is not None
        and account.marketplace.code == "ozon"
    ):
        return None
    current_source = _source_observation(current_product)
    if current_source is None:
        return None

    sibling_sources = []
    seen_product_ids = {current_product.id}
    for pair in sibling_pairs:
        if type(pair) not in (tuple, list) or len(pair) != 2:
            continue
        sibling_item, sibling_product = pair
        if (
            type(sibling_item) is not OzonBulkUploadItem
            or type(sibling_product) is not ImportedProduct
            or sibling_product.id in seen_product_ids
        ):
            continue
        source = _sibling_preflight(
            run=run,
            account=account,
            item=sibling_item,
            product=sibling_product,
            now=current_time,
        )
        if source is None:
            continue
        sibling_sources.append((sibling_item, sibling_product, source))
        seen_product_ids.add(sibling_product.id)
        if len(sibling_sources) >= _MAX_WINDOW_PRODUCTS - 1:
            break

    source_categories = [current_source[1]] + [
        source[2][1] for source in sibling_sources
    ]
    tree_index = build_ozon_category_tree_index(
        account.marketplace,
        source_categories,
        now=current_time,
    )
    if tree_index is None:
        return None
    current_candidate = build_ozon_prepare_category_candidate(
        run=run,
        item=current_item,
        account=account,
        product=current_product,
        draft=current_draft,
        tree_index=tree_index,
        prior_draft_id_before_create=prior_draft_id_before_create,
        item_draft_id_before_create=item_draft_id_before_create,
        item_prepared_version_before_create=item_prepared_version_before_create,
        now=current_time,
    )
    if current_candidate is None:
        return None
    sibling_candidates = []
    for sibling_item, sibling_product, source in sibling_sources:
        candidate = _build_sibling_candidate(
            run=run,
            account=account,
            item=sibling_item,
            product=sibling_product,
            source=source,
            tree_index=tree_index,
            now=current_time,
        )
        if candidate is not None:
            sibling_candidates.append(candidate)
    candidates = [current_candidate, *sibling_candidates[:_MAX_WINDOW_PRODUCTS - 1]]
    contexts = [candidate.context for candidate in candidates]
    # The pipeline owns its reservation/transport clock. Passing the earlier
    # eligibility timestamp here would shorten cooldowns after a slow request.
    pipeline_result = pipeline.run_category_phase(contexts)
    if len(pipeline_result.results) != len(candidates):
        return None
    entries = tuple(
        OzonPrepareCategoryWindowEntry(
            product_key=candidate.context.product_key,
            candidate_signature=candidate.candidate_signature,
            result=result,
        )
        for candidate, result in zip(candidates, pipeline_result.results)
    )
    current_result = pipeline_result.results[0]
    return OzonPrepareCategoryWindowResult(
        entries=entries,
        current_candidate=current_candidate,
        current_result=current_result,
        current_product_type_id=selected_product_type_id(
            current_candidate,
            current_result,
        ),
        physical_request_started=pipeline_result.physical_request_started,
        reservation_state=pipeline_result.reservation_state,
        finish_state=pipeline_result.finish_state,
        retry_at=getattr(pipeline_result, "retry_at", None),
        requires_current_scope_recheck=True,
    )


__all__ = [
    "OzonCategoryTreeIndex",
    "OzonCategoryTreeNode",
    "OzonPrepareCategoryCandidate",
    "OzonPrepareCategorySiblingCandidate",
    "OzonPrepareCategoryTypeCandidate",
    "OzonPrepareCategoryWindowEntry",
    "OzonPrepareCategoryWindowResult",
    "build_ozon_category_tree_index",
    "build_ozon_prepare_category_candidate",
    "prepare_ozon_category_window",
    "same_ozon_prepare_category_scope",
    "selected_product_type_id",
]
