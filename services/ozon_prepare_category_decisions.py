"""Read-only candidate construction for exact Ozon source preparation.

This module does not create category mappings or modify drafts. It admits an
AI suggestion only for a queue-owned, untyped draft with a fresh local Ozon
category tree, using exact source-taxonomy matches first and bounded current
exact-link evidence only as a per-product fallback.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
import unicodedata
from typing import Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import joinedload

from models import (
    ImportedProduct,
    Marketplace,
    MarketplaceCategoryMapping,
    MarketplaceListing,
    MarketplaceListingLinkEvent,
    MarketplaceProductDraft,
    MarketplaceOperation,
    MarketplaceProductType,
    MarketplaceTaxonomyCategory,
    OzonBulkUploadItem,
    OzonBulkUploadRun,
    SellerMarketplaceAccount,
    SupplierProduct,
    db,
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
# A scope above this limit is incomplete for our purposes and cannot support
# an observed fallback.
_MAX_OBSERVED_SCOPE_ROWS = 1_000
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
class _ObservedLinkedCategoryEvidence:
    """Current exact-link evidence for one supplier/raw-category identity."""

    category_id: int
    product_types: Tuple[MarketplaceProductType, ...] = field(repr=False)
    fingerprint: str


@dataclass(frozen=True)
class _ResolvedCategoryOptions:
    """Fresh exact Ozon node and bounded product-type choices."""

    category: OzonCategoryTreeNode
    product_types: Tuple[MarketplaceProductType, ...] = field(repr=False)
    match_kind: str
    observed_evidence_fingerprint: Optional[str] = None


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
    """Require an exact linked supplier snapshot and current revision.

    A missing copied vendor code is usable only when the supplier's current
    serialized source snapshot independently proves that code for the exact
    shared external ID. The proof is folded into the private scope signature;
    it is never added to the model context.
    """
    supplier_product = product.supplier_product
    supplier = supplier_product.supplier if supplier_product is not None else None
    if (
        supplier_product is None
        or supplier is None
        or product.supplier_product_id != supplier_product.id
        or product.supplier_id is None
        or product.supplier_id != supplier_product.supplier_id
        or type(product.supplier_content_revision) is not int
        or product.supplier_content_revision <= 0
        or type(supplier_product.content_revision) is not int
        or product.supplier_content_revision != supplier_product.content_revision
        or product.source_type != supplier.code
        or product.external_id != supplier_product.external_id
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

    supplier_vendor_proof_signature = None
    copied_vendor_code = product.external_vendor_code
    supplier_vendor_code = supplier_product.vendor_code
    if copied_vendor_code != supplier_vendor_code:
        if copied_vendor_code is not None:
            # A populated (including empty-string) contradictory copy is never
            # treated as a missing value.
            return None
        # SQL NULL is the only missing-copy case. Require the exact same
        # nonblank source ID and an exact nonblank vendor code in the supplier's
        # raw current snapshot; do not trim or normalize identity values.
        source_external_id = supplier_product.external_id
        snapshot_vendor_code = supplier_original.get("vendor_code")
        if not (
            type(product.external_id) is str
            and product.external_id.strip()
            and type(source_external_id) is str
            and source_external_id.strip()
            and product.external_id == source_external_id
            and type(supplier_vendor_code) is str
            and supplier_vendor_code.strip()
            and type(snapshot_vendor_code) is str
            and snapshot_vendor_code.strip()
            and snapshot_vendor_code == supplier_vendor_code
        ):
            return None
        supplier_vendor_proof_signature = _sha256({
            "external_id": source_external_id,
            "supplier_vendor_code": supplier_vendor_code,
            "snapshot_vendor_code": snapshot_vendor_code,
        })

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
    signature_document = {
        "fact_hash": pack.get("fact_hash"),
        "source_title": clean_title,
        "source_category": clean_category,
        "title_source": title_provenance.get("source"),
        "category_source": category_provenance.get("source"),
    }
    if supplier_vendor_proof_signature is not None:
        signature_document["supplier_vendor_proof_signature"] = (
            supplier_vendor_proof_signature
        )
    fact_signature = _sha256(signature_document)
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


def _mapping_state_allows_category_decision(
    *,
    seller_id: int,
    marketplace_id: int,
    product: ImportedProduct,
    source_category: str,
    raw_source_category: Optional[str],
) -> Tuple[bool, Optional[MarketplaceCategoryMapping]]:
    """Keep every stored mapping authoritative except one stale auto conflict.

    The exception only opens a per-product decision candidate. It never
    changes, reactivates, or writes the category mapping itself.
    """
    identities = MarketplaceDraftService._mapping_identities(product)
    keys = {
        (identity.get("scope_key"), identity.get("source_category_normalized"))
        for identity in identities
        if isinstance(identity.get("scope_key"), str)
        and isinstance(identity.get("source_category_normalized"), str)
    }
    keys.add((
        MarketplaceDraftService._scope_key(product),
        MarketplaceDraftService._normalized_text(source_category),
    ))
    rows = []
    for scope_key, normalized_category in sorted(keys):
        row = MarketplaceCategoryMapping.query.filter_by(
            seller_id=seller_id,
            marketplace_id=marketplace_id,
            scope_key=scope_key,
            source_category_normalized=normalized_category,
        ).first()
        if row is not None and row not in rows:
            rows.append(row)
    if not rows:
        return True, None
    if raw_source_category is None:
        # Exact tree matching historically does not depend on the copied
        # category column. Without it, however, no observed alias is safe.
        return False, None
    if len(rows) != 1:
        return False, None

    mapping = rows[0]
    expected_scope = f"supplier:{product.supplier_id}"
    if (
        not product.supplier_id
        or mapping.scope_key != expected_scope
        or mapping.supplier_id != product.supplier_id
        or mapping.source_type != product.source_type
        or mapping.source_category != raw_source_category
        or mapping.source_category_normalized
        != MarketplaceDraftService._normalized_text(raw_source_category)
        or mapping.mapping_source != "deterministic"
        or mapping.mapping_status != "stale"
        or mapping.corrected_by_user_id is not None
    ):
        return False, None
    try:
        evidence = json.loads(mapping.evidence_json or "{}")
    except (TypeError, ValueError):
        return False, None
    if not isinstance(evidence, dict) or not (
        evidence.get("algorithm")
        == MarketplaceDraftService.OBSERVED_MAPPING_ALGORITHM
        and evidence.get("state") == "stale"
        and evidence.get("reason") in {
            "conflicting_product_types",
            "heterogeneous_source_category",
        }
    ):
        return False, None
    return True, mapping


def _stale_mapping_scope(mapping: Optional[MarketplaceCategoryMapping]) -> Optional[dict]:
    if mapping is None:
        return None
    return {
        "id": mapping.id,
        "updated_at": mapping.updated_at.isoformat() if mapping.updated_at else None,
        "evidence_fingerprint": _sha256(mapping.evidence_json or "{}"),
    }


def _naive_utc(value: object) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _fresh_timestamp(
    value: object,
    *,
    now: datetime,
    max_age: timedelta,
) -> bool:
    timestamp = _naive_utc(value)
    return bool(
        timestamp is not None
        and now - max_age <= timestamp <= now
    )


def _exact_raw_source_category(
    product: ImportedProduct,
    *,
    clean_source_category: str,
) -> Optional[str]:
    """Require the copied raw category to equal the current source snapshot."""
    raw_category = product.category
    supplier_product = product.supplier_product
    if (
        not isinstance(raw_category, str)
        or not raw_category.strip()
        or supplier_product is None
        or product.supplier_id != supplier_product.supplier_id
    ):
        return None
    try:
        supplier_original = json.loads(
            supplier_product.original_data_json or "{}"
        )
    except (TypeError, ValueError):
        return None
    if not isinstance(supplier_original, dict):
        return None
    if (
        supplier_original.get("category") != raw_category
        or " ".join(raw_category.split()) != clean_source_category
    ):
        return None
    return raw_category


def _fresh_observed_product_type(
    product_type: object,
    *,
    marketplace: Marketplace,
    now: datetime,
) -> Optional[OzonCategoryTreeNode]:
    if type(product_type) is not MarketplaceProductType:
        return None
    category = product_type.category
    if type(category) is not MarketplaceTaxonomyCategory:
        return None
    max_age = timedelta(hours=OzonReferenceService.HARD_TTL_HOURS)
    if (
        product_type.marketplace_id != marketplace.id
        or type(product_type.id) is not int
        or product_type.id <= 0
        or type(product_type.category_id) is not int
        or product_type.category_id <= 0
        or not product_type.is_available
        or product_type.is_disabled_upstream
        or not product_type.is_seller_selectable
        or not _fresh_timestamp(product_type.last_seen_at, now=now, max_age=max_age)
        or not OzonReferenceService.reference_is_fresh(product_type, now=now)
        or not _fresh_timestamp(
            product_type.attributes_synced_at, now=now, max_age=max_age
        )
        or category.id != product_type.category_id
        or category.marketplace_id != marketplace.id
        or not category.is_available
        or category.is_disabled_upstream
        or not _fresh_timestamp(category.last_seen_at, now=now, max_age=max_age)
        or not isinstance(category.name, str)
        or not category.name.strip()
        or len(category.name.encode("utf-8")) > 300
        or not isinstance(category.full_path, str)
        or not category.full_path.strip()
        or len(category.full_path.encode("utf-8")) > 2_000
        or _canonical_external_id(category.external_category_id) is None
    ):
        return None
    return OzonCategoryTreeNode(
        local_id=category.id,
        external_id=category.external_category_id,
        name=category.name,
        full_path=category.full_path,
        is_available=category.is_available,
        is_disabled_upstream=category.is_disabled_upstream,
    )


def _current_link_event_is_exact(
    listing: MarketplaceListing,
    event: Optional[MarketplaceListingLinkEvent],
) -> bool:
    if (
        event is None
        or type(listing.link_version) is not int
        or listing.link_version <= 0
        or event.listing_id != listing.id
        or event.link_version != listing.link_version
        or event.imported_product_id != listing.imported_product_id
        or event.seller_id != listing.seller_id
        or event.marketplace_id != listing.marketplace_id
        or event.account_id != listing.account_id
        or listing.linked_at is None
        or listing.linked_by_user_id != event.actor_user_id
        or listing.link_source != event.source
    ):
        return False
    if (
        event.action == "auto_link"
        and event.source in {"exact_offer_identity", "exact_source_identity"}
    ):
        return True
    return bool(
        event.action == "manual_link"
        and event.source == "seller_confirmation"
        and type(event.actor_user_id) is int
        and event.actor_user_id > 0
    )


def _observed_scope_cache_key(
    *,
    seller_id: int,
    marketplace_id: int,
    account_id: int,
    product: ImportedProduct,
    raw_source_category: str,
) -> Tuple[object, ...]:
    return (
        seller_id,
        marketplace_id,
        account_id,
        product.supplier_id,
        product.source_type,
        raw_source_category,
    )


def _load_exact_observed_scope(
    *,
    seller_id: int,
    account: SellerMarketplaceAccount,
    product: ImportedProduct,
    raw_source_category: str,
    clean_source_category: str,
    now: datetime,
    cache: Optional[dict],
) -> Optional[Tuple[Tuple[MarketplaceListing, ImportedProduct, MarketplaceListingLinkEvent, MarketplaceProductType], ...]]:
    """Read one bounded current scope; every row must have exact live lineage.

    These current Ozon projections are joint catalog evidence, not a claim
    about why a category was chosen at the original publication time.
    """
    account_marketplace = account.marketplace
    cache_key = _observed_scope_cache_key(
        seller_id=seller_id,
        marketplace_id=account.marketplace_id,
        account_id=account.id,
        product=product,
        raw_source_category=raw_source_category,
    )
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    def remember(value):
        if cache is not None:
            cache[cache_key] = value
        return value

    if (
        account_marketplace is None
        or not OzonReferenceService.tree_is_fresh(account_marketplace, now=now)
        or not _fresh_timestamp(
            account_marketplace.categories_synced_at,
            now=now,
            max_age=timedelta(hours=OzonReferenceService.HARD_TTL_HOURS),
        )
        or not product.supplier_id
        or not isinstance(product.source_type, str)
        or not product.source_type
        or _exact_raw_source_category(
            product,
            clean_source_category=clean_source_category,
        ) != raw_source_category
    ):
        return remember(None)

    rows = MarketplaceListing.query.options(
        joinedload(MarketplaceListing.imported_product)
        .joinedload(ImportedProduct.supplier_product)
        .joinedload(SupplierProduct.supplier),
        joinedload(MarketplaceListing.product_type)
        .joinedload(MarketplaceProductType.category),
    ).join(
        ImportedProduct,
        ImportedProduct.id == MarketplaceListing.imported_product_id,
    ).filter(
        MarketplaceListing.seller_id == seller_id,
        MarketplaceListing.marketplace_id == account.marketplace_id,
        MarketplaceListing.account_id == account.id,
        MarketplaceListing.is_available.is_(True),
        MarketplaceListing.is_archived.is_(False),
        MarketplaceListing.imported_product_id.isnot(None),
        ImportedProduct.seller_id == seller_id,
        ImportedProduct.supplier_id == product.supplier_id,
        ImportedProduct.source_type == product.source_type,
        ImportedProduct.category == raw_source_category,
    ).order_by(
        MarketplaceListing.id.asc(),
    ).limit(
        _MAX_OBSERVED_SCOPE_ROWS + 1
    ).all()
    if (
        len(rows) > _MAX_OBSERVED_SCOPE_ROWS
        or not rows
    ):
        return remember(None)

    listing_ids = [row.id for row in rows]
    latest_events = db.session.query(
        MarketplaceListingLinkEvent.listing_id.label("listing_id"),
        func.max(MarketplaceListingLinkEvent.id).label("latest_event_id"),
    ).filter(
        MarketplaceListingLinkEvent.listing_id.in_(listing_ids),
    ).group_by(
        MarketplaceListingLinkEvent.listing_id,
    ).subquery()
    event_rows = MarketplaceListingLinkEvent.query.join(
        latest_events,
        MarketplaceListingLinkEvent.id == latest_events.c.latest_event_id,
    ).filter(
        MarketplaceListingLinkEvent.seller_id == seller_id,
        MarketplaceListingLinkEvent.marketplace_id == account.marketplace_id,
        MarketplaceListingLinkEvent.account_id == account.id,
    ).order_by(
        MarketplaceListingLinkEvent.listing_id.asc(),
    ).all()
    events_by_listing = {event.listing_id: event for event in event_rows}
    if len(events_by_listing) != len(rows):
        return remember(None)

    freshness = MarketplaceDraftService.LISTING_HARD_TTL
    exact_rows = []
    for listing in rows:
        linked_product = listing.imported_product
        event = events_by_listing.get(listing.id)
        if (
            linked_product is None
            or linked_product.id != listing.imported_product_id
            or linked_product.seller_id != seller_id
            or linked_product.supplier_id != product.supplier_id
            or linked_product.source_type != product.source_type
            or linked_product.category != raw_source_category
            or linked_product.supplier_product_id is None
            or linked_product.supplier_product_id <= 0
            or not _current_link_event_is_exact(listing, event)
            or listing.link_status != "linked"
            or listing.link_source not in {
                "exact_offer_identity",
                "exact_source_identity",
                "seller_confirmation",
            }
            or not all(_fresh_timestamp(
                value, now=now, max_age=freshness
            ) for value in (
                listing.last_seen_at,
                listing.info_synced_at,
                listing.attributes_synced_at,
            ))
            or not isinstance(listing.attributes_json, str)
            or len(listing.attributes_json.encode("utf-8"))
            > MarketplaceDraftService.MAX_JSON_BYTES
        ):
            return remember(None)
        try:
            attributes = json.loads(listing.attributes_json)
        except (TypeError, ValueError):
            return remember(None)
        if (
            not isinstance(attributes, list)
            or len(attributes) > MarketplaceDraftService.MAX_ATTRIBUTES
            or _source_observation(linked_product) is None
        ):
            return remember(None)
        product_type = listing.product_type
        category_node = _fresh_observed_product_type(
            product_type,
            marketplace=account_marketplace,
            now=now,
        )
        if (
            category_node is None
            or _canonical_external_id(listing.external_category_id)
            != category_node.external_id
            or _canonical_external_id(listing.external_type_id)
            != product_type.external_type_id
            or product_type.category_id != category_node.local_id
            or MarketplaceDraftService._automatic_mapping_conflict_reason(
                identity={"source_category": raw_source_category},
                product_type=product_type,
            ) is not None
        ):
            return remember(None)
        exact_rows.append((listing, linked_product, event, product_type))

    result = tuple(exact_rows)
    return remember(result)


def _observed_category_evidence(
    *,
    seller_id: int,
    account: SellerMarketplaceAccount,
    product: ImportedProduct,
    source_title: str,
    source_category: str,
    raw_source_category: str,
    now: datetime,
    evidence_cache: Optional[dict],
    required_category_id: Optional[int] = None,
) -> Optional[_ObservedLinkedCategoryEvidence]:
    """Build a private, per-product shortlist from independent exact peers."""
    rows = _load_exact_observed_scope(
        seller_id=seller_id,
        account=account,
        product=product,
        raw_source_category=raw_source_category,
        clean_source_category=source_category,
        now=now,
        cache=evidence_cache,
    )
    if rows is None:
        return None
    # The held-out product and every duplicate projection of its exact source
    # product are excluded from both counts and the evidence fingerprint.
    peers = tuple(
        row for row in rows
        if row[1].id != product.id
        and row[1].supplier_product_id != product.supplier_product_id
    )
    if not peers:
        return None
    product_type_by_id = {}
    identities_by_type = {}
    canonical_type = {}
    source_type = {}
    fingerprint_rows = []
    category_ids = set()
    for listing, linked_product, event, product_type in peers:
        type_id = product_type.id
        category_ids.add(product_type.category_id)
        canonical_key = linked_product.id
        supplier_key = linked_product.supplier_product_id
        previous_type = canonical_type.setdefault(canonical_key, type_id)
        previous_source_type = source_type.setdefault(supplier_key, type_id)
        if previous_type != type_id or previous_source_type != type_id:
            return None
        product_type_by_id[type_id] = product_type
        identities = identities_by_type.setdefault(type_id, (set(), set()))
        identities[0].add(canonical_key)
        identities[1].add(supplier_key)
        fingerprint_rows.append([
            listing.id,
            listing.link_version,
            event.id,
            event.action,
            event.source,
            linked_product.id,
            linked_product.supplier_product_id,
            linked_product.supplier_content_revision,
            linked_product.supplier_product.content_revision,
            product_type.category_id,
            product_type.id,
            product_type.external_type_id,
            product_type.attributes_version,
            product_type.attributes_schema_hash,
        ])
    if required_category_id is None:
        if len(category_ids) != 1:
            return None
        category_id = next(iter(category_ids))
    else:
        if category_ids != {required_category_id}:
            return None
        category_id = required_category_id
    if not 2 <= len(product_type_by_id) <= _MAX_TYPES:
        return None
    if any(
        len(identities_by_type[type_id][0]) < 2
        or len(identities_by_type[type_id][1]) < 2
        for type_id in product_type_by_id
    ):
        return None
    source_adult = _adult_marker_present(source_title) or _adult_marker_present(
        source_category
    )
    if source_adult and len(_path_parts(source_category)) < 2:
        return None
    for product_type in product_type_by_id.values():
        category = product_type.category
        if source_adult != _adult_marker_present(category.full_path):
            return None
    fingerprint = _sha256({
        "version": 1,
        "seller_id": seller_id,
        "marketplace_id": account.marketplace_id,
        "account_id": account.id,
        "supplier_id": product.supplier_id,
        "source_type": product.source_type,
        "raw_category_signature": _sha256(raw_source_category),
        "rows": sorted(fingerprint_rows),
    })
    return _ObservedLinkedCategoryEvidence(
        category_id=category_id,
        product_types=tuple(
            product_type_by_id[type_id]
            for type_id in sorted(product_type_by_id)
        ),
        fingerprint=fingerprint,
    )


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


def _category_node_is_current(
    category: OzonCategoryTreeNode,
) -> bool:
    return bool(
        type(category.local_id) is int
        and category.local_id > 0
        and isinstance(category.name, str)
        and category.name.strip()
        and len(category.name.encode("utf-8")) <= 300
        and isinstance(category.full_path, str)
        and category.full_path.strip()
        and len(category.full_path.encode("utf-8")) <= 2_000
        and category.is_available
        and not category.is_disabled_upstream
        and _canonical_external_id(category.external_id) is not None
    )


def _resolve_category_options(
    *,
    seller_id: int,
    account: SellerMarketplaceAccount,
    product: ImportedProduct,
    source_title: str,
    source_category: str,
    raw_source_category: Optional[str],
    tree_index: OzonCategoryTreeIndex,
    now: datetime,
    evidence_cache: Optional[dict],
) -> Optional[_ResolvedCategoryOptions]:
    """Prefer exact source/tree identity, then use bounded exact-link peers.

    The observed fallback never writes a category map. It is eligible only
    when all peer types resolve to one current Ozon category, optionally the
    exact literal node when that node's tree is too broad for the planner.
    """
    marketplace = account.marketplace
    if marketplace is None or (
        tree_index.marketplace_id != marketplace.id
        or tree_index.tree_version != marketplace.categories_version
        or tree_index.snapshot_hash != marketplace.categories_snapshot_hash
        or tree_index.synced_at != marketplace.categories_synced_at
        or not OzonReferenceService.tree_is_fresh(marketplace, now=now)
        or not _fresh_timestamp(
            marketplace.categories_synced_at,
            now=now,
            max_age=timedelta(hours=OzonReferenceService.HARD_TTL_HOURS),
        )
    ):
        return None

    matched = _category_match(source_category, tree_index)
    if matched is not None:
        category, match_kind = matched
        if not _category_node_is_current(category):
            return None
        source_adult = _adult_marker_present(source_title) or _adult_marker_present(
            source_category
        )
        category_adult = _adult_marker_present(category.full_path)
        if source_adult != category_adult or (
            source_adult and match_kind != "full_path"
        ):
            return None
        literal_types = tuple(MarketplaceProductType.query.filter_by(
            marketplace_id=marketplace.id,
            category_id=category.local_id,
            is_available=True,
            is_seller_selectable=True,
            is_disabled_upstream=False,
        ).order_by(
            MarketplaceProductType.external_type_id.asc(),
            MarketplaceProductType.id.asc(),
        ).limit(_MAX_TYPES + 1).all())
        if 2 <= len(literal_types) <= _MAX_TYPES:
            return _ResolvedCategoryOptions(
                category=category,
                product_types=literal_types,
                match_kind=match_kind,
            )
        if len(literal_types) <= _MAX_TYPES:
            # A narrow exact category with too few choices remains governed by
            # the literal tree contract; observed peers do not override it.
            return None
        required_category_id = category.local_id
    else:
        category = None
        match_kind = "observed_exact_linked_category"
        required_category_id = None

    if raw_source_category is None:
        return None
    evidence = _observed_category_evidence(
        seller_id=seller_id,
        account=account,
        product=product,
        source_title=source_title,
        source_category=source_category,
        raw_source_category=raw_source_category,
        now=now,
        evidence_cache=evidence_cache,
        required_category_id=required_category_id,
    )
    if evidence is None:
        return None
    product_types = evidence.product_types
    if len(product_types) < 2 or len(product_types) > _MAX_TYPES:
        return None
    category_node = _fresh_observed_product_type(
        product_types[0], marketplace=marketplace, now=now,
    )
    if (
        category_node is None
        or category_node.local_id != evidence.category_id
        or not _category_node_is_current(category_node)
    ):
        return None
    return _ResolvedCategoryOptions(
        category=category_node,
        product_types=product_types,
        match_kind=(
            "observed_same_literal_node"
            if category is not None
            else match_kind
        ),
        observed_evidence_fingerprint=evidence.fingerprint,
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
    observed_evidence_cache: Optional[dict] = None,
) -> Optional[OzonPrepareCategoryCandidate]:
    """Return one exact category/type shortlist without writing.

    The pre-create IDs and version are captured by the queue before
    ``create_draft``. A manual draft cannot enter the initial new-draft branch;
    a retry needs the exact prepared version captured before the transition.
    Exact supplier-to-Ozon taxonomy matches retain priority. When no literal
    match exists, or the exact Ozon node exceeds the planner limit, current
    independently linked type observations may bound a per-product choice.
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
    raw_source_category = _exact_raw_source_category(
        product,
        clean_source_category=source_category,
    )
    mapping_allowed, stale_conflict_mapping = _mapping_state_allows_category_decision(
        seller_id=run.seller_id,
        marketplace_id=account.marketplace_id,
        product=product,
        source_category=source_category,
        raw_source_category=raw_source_category,
    )
    if not mapping_allowed:
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
    options = _resolve_category_options(
        seller_id=run.seller_id,
        account=account,
        product=product,
        source_title=source_title,
        source_category=source_category,
        raw_source_category=raw_source_category,
        tree_index=tree_index,
        now=current_time,
        evidence_cache=observed_evidence_cache,
    )
    if options is None:
        return None
    category = options.category
    match_kind = options.match_kind
    category_external_id = category.external_id
    types = options.product_types

    type_candidates = []
    context_choices = []
    for product_type in types:
        external_type_id = _canonical_external_id(product_type.external_type_id)
        if (
            type(product_type.id) is not int
            or product_type.id <= 0
            or external_type_id is None
            or product_type.marketplace_id != marketplace.id
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
    if options.observed_evidence_fingerprint is not None:
        # Private exact-link evidence changes the local apply scope without
        # changing planner prompt/cache content.
        scope_document["observed_evidence_fingerprint"] = (
            options.observed_evidence_fingerprint
        )
    stale_mapping_scope = _stale_mapping_scope(stale_conflict_mapping)
    if stale_mapping_scope is not None:
        scope_document["stale_conflict_mapping"] = stale_mapping_scope
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
    raw_source_category = _exact_raw_source_category(
        product,
        clean_source_category=source_category,
    )
    mapping_allowed, stale_conflict_mapping = _mapping_state_allows_category_decision(
        seller_id=run.seller_id,
        marketplace_id=account.marketplace_id,
        product=product,
        source_category=source_category,
        raw_source_category=raw_source_category,
    )
    if not mapping_allowed:
        return None
    if stale_conflict_mapping is not None:
        source_signature = _sha256({
            "source_signature": source_signature,
            "stale_conflict_mapping": _stale_mapping_scope(
                stale_conflict_mapping
            ),
        })
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
    observed_evidence_cache: Optional[dict] = None,
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
    raw_source_category = _exact_raw_source_category(
        product,
        clean_source_category=source_category,
    )
    options = _resolve_category_options(
        seller_id=run.seller_id,
        account=account,
        product=product,
        source_title=source_title,
        source_category=source_category,
        raw_source_category=raw_source_category,
        tree_index=tree_index,
        now=now,
        evidence_cache=observed_evidence_cache,
    )
    if options is None:
        return None
    category = options.category
    match_kind = options.match_kind
    category_external_id = category.external_id
    types = options.product_types
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
            or product_type.marketplace_id != account.marketplace_id
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
        "version": 2,
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
            [candidate.product_type_id, candidate.choice.category_id,
             candidate.choice.type_id]
            for candidate in type_candidates
        ],
        "match_kind": match_kind,
        "observed_evidence_fingerprint": options.observed_evidence_fingerprint,
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
    observed_evidence_cache = {}
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
        observed_evidence_cache=observed_evidence_cache,
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
            observed_evidence_cache=observed_evidence_cache,
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
