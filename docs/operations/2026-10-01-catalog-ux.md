# Catalog UX audit and common-content editor contract

2026-10-01. Audit on isolated worktree `codex/catalog-ux-20261001`, base
`c6c5431`. This note records the audit findings, accepted implementation
contract, and bounded implementation evidence for the common-content backend
and catalog UX work.

## Findings

- Supplier product detail is a read/prepare surface. `templates/supplier_catalog_product_detail.html`
  does not edit seller common title, description, photo selection, or attributes.
  Its existing edits are supplier/WB category actions.
- `templates/admin_supplier_product_detail.html` and
  `SupplierService.update_product` edit shared `SupplierProduct` content, but
  belong to the admin/source side. They are not a seller common-product editor.
- `templates/marketplace_listing_beta_detail.html` is an Ozon-account listing
  overview. Its existing link review changes only the internal link. Ozon draft
  editing remains a separate account-scoped draft flow. `MarketplaceProductDraft`
  stores channel snapshots; saved drafts are not live views over the common row.
- The legacy WB editor and its histories act on `Product`, not
  `ImportedProduct`. They must keep their current write, preview, enrichment,
  FBS, and history behavior.

### Current write paths into `ImportedProduct`

1. `services/supplier_service.py:_copy_to_imported_product` creates a seller
   copy from supplier observations. `_update_imported_from_supplier` refreshes
   title, description, characteristics, photos and other source fields; it also
   replaces `original_data` with the latest supplier observation.
2. `services/auto_import_manager.py` creates and refreshes legacy CSV imports,
   including title, description, `photo_urls`, and supplier `original_data`.
3. `routes/internal_api.py:_validate_and_apply_imported_product_update` and its
   batch endpoint accept agent changes to title, description, and
   characteristics. They currently record task-scoped `AgentChangeSnapshot`s.
4. `services/marketplace_canonical_content.py` applies a separately reviewed
   Ozon listing observation to title/description. It requires the exact listing
   and account and writes a snapshot with `task_id=NULL` and
   `agent_id="ozon-canonical-review"`. Keep this source-review proposal distinct
   from seller-authored overrides.

Other nearby assignments in WB card editing, enrichment, and publication
reconciliation write `Product` or marketplace listing snapshots; they are not
common `ImportedProduct` writes. New common writes must not mutate
`original_data` to make seller-authored values look supplier-observed.

`AgentChangeSnapshot` consumers are task-scoped: agent task history queries by
`task_id`; `snapshot_count` and `rollback_task_tree` query task trees; the latter
restores `previous_values` only for snapshots owned by those task IDs. Supplier
bulk delete removes snapshots by ImportedProduct ID. Existing Ozon canonical
review already demonstrates `task_id=NULL` with a namespaced `agent_id`, but
there is no general user-facing snapshot history or user actor column. A
namespaced metadata convention is technically possible, yet persistent override
state and a reliable edit version would then depend on scanning snapshot history
and its current product-only/task-product indexes. Prefer an explicit additive
ImportedProduct override document and version unless a measured migration
constraint justifies that extra coupling. Continue writing an audit snapshot for
each committed seller edit; derive actor identity from the authenticated server
session, never from request JSON.

## Bounded common editor contract

Use `ImportedProduct` as the current seller common object. Do not add a second
PIM model or extend `MarketplaceCanonicalContentProposal`. The minimal editor
fields are title, description, existing photo selection/order, and common
characteristics. Exclude category IDs, WB `imtID`, price, stock, channel
identities, and publication state.

Accepted additive metadata on `ImportedProduct`:

- `content_overrides_json`: nullable, versioned, strict allowlist by field. Each
  entry records whether the seller chose an override, the typed effective value,
  and server-derived edit metadata. The authenticated seller's user ID comes
  from the server session, never the request body. Clearing an override means
  re-inherit from the latest source; an explicitly empty value must remain
  distinguishable from inheritance where the field contract allows it.
- `content_edit_version`: integer, initialized to 1, incremented on every
  accepted common edit. Writes compare the expected version and current field
  baseline in one transaction; stale saves return a conflict with current state.

The effective common fields remain in their current `ImportedProduct` columns
for existing readers. The override document explains why a value is effective;
`original_data` remains observed source data. Supplier refresh updates inherited
fields and source revision through one centralized helper, but preserves
overridden effective fields. Reverting a field to inheritance copies the latest
source value in that same transaction.

Read and preview contracts should expose, per field, effective value, inherited
source value, origin (`source`, `seller_override`, or `unknown`), source identity
and revision/freshness. Photo choices are limited to exact already-known seller
or supplier URLs and their existing display slots; do not accept arbitrary URLs,
create a proxy, or introduce an unreviewed upload route. Preserve photo order as
explicit seller state. Common characteristics are seller-current content, not
supplier evidence. FactPack/source-only AI paths must label or exclude manual
overrides rather than copying them into `original_data` or presenting them as
original source facts.

Accepted API boundary in a dedicated service module and seller-scoped route
module:

- `GET /api/my-products/<id>/common-content`: authenticate seller ownership,
  return effective/inherited fields and `content_edit_version`.
- `POST /api/my-products/common-content/preview`: accept at most 50 exact
  seller-scoped product IDs, expected versions, typed field changes, and an
  explicit recipient set. Each change is `{mode: "override", value: ...}` or
  `{mode: "inherit"}`. Return normalized old/new common values, per-recipient
  projection diff, exact channel/account/listing or draft identity, source drift,
  and a signed preview token valid for at most 600 seconds. The token binds the
  authenticated seller/user, exact item set, expected versions, normalized
  changes, recipient references, and source fingerprints. Preview performs no
  write.
- `POST /api/my-products/common-content/apply`: accept the token only; verify
  its signature/expiry and recompute current content versions, inherited-source
  fingerprints, and recipient identities. Atomically apply the entire exact set
  or roll it all back, update only common content and audit snapshots. Recipient
  references are preview context, not channel writes. A changed source, target,
  value, or version returns a conflict and requires a newly opened preview.
  Cancellation/reopen only discard the preview.

Reject unknown fields/types and bodies above 256 KiB. Use seller-scoped 404s for
foreign identities, 400/422 for invalid field/value, 413 for a set above 50, and
409 for stale content, source, or recipient context.

Saving common content changes only the seller common object. The UI button is
labelled **«Сохранить общий товар»** and states that draft/live cards are not
updated. Existing channel drafts and live listing snapshots stay unchanged;
after saving, the seller can open a reviewed exact recipient and follow that
channel's existing preview/save flow. Channel save and marketplace publication
remain separate explicit steps;
no implicit publish, relink, price/stock change, WB imtID change, or silent
overwrite is permitted. Use existing account, listing, draft, WB edit/history,
enrichment, FBS and link guards; do not invent parallel channel write lanes.

The writer inventory above is implemented: supplier refresh, CSV import, and
internal agent writes are guarded against stale common-content edits, while
Ozon canonical updates retain their listing/account/source review and conflict
with active manual overrides. Existing channel draft snapshots are not rewritten
by a common save.

## UX scope accepted for the current worker

- UX-01.5: explain Ozon moderation from decoded provider message, including the
  long-words cause only when that message confirms it; show a safe next step and
  keep raw technical details secondary. Fix provider-status warning text so a
  known `declined` status displays its Russian label instead of a raw code. Add
  descriptive hero image alternative text, intrinsic dimensions, and
  `aria-pressed` for gallery selection. Keep listing data and moderation
  payloads unchanged.
- UX-01.7: show readable changed-field values and exact seller-scoped item
  links in WB bulk history; preserve retry, rollback, uncertainty and
  completion/error semantics.

The broader UX-01.1/.11 regressions and the remaining already-accepted tasks
must use the existing implementation and verification artifacts in
`docs/design/ux-01-implementation.md`; this work does not repeat those changes.

## Implementation notes and limits

- Non-editor `ImportedProduct` writers now capture and conditionally guard the
  exact common-content state they read before writing. Supplier refresh uses a
  per-row savepoint inside its owned batch commit; CSV import rolls back its
  row on any guarded-refresh failure. A late edit-version/override change
  conflicts even when the visible common value is unchanged.
- Current common content and observed supplier data remain separate. Supplier
  AI suggestions and enrichment can be inherited with their own provenance,
  but manual common values are not copied into `original_data`. Explicitly
  cleared source characteristics stay cleared after refresh/category rollback.
- Photo preview follows the existing authenticated supplier-first slot route.
  If a legacy selected URL was in an imported-product slot that is now occupied
  by a different supplier URL, the legacy URL can remain selected and ordered
  while its `preview_url` is unavailable. The UI must show that unavailable
  preview without substituting another supplier image. This is a display
  limitation; it does not show that the effective/published photo URL was lost.
  A supplier URL receives a preview only when the existing route serves that
  exact URL from that exact slot. No arbitrary-URL proxy or second photo route
  is introduced.
- Focused writer, source, and history verification is recorded in commits
  `0107b91` and `c196c62`; the exact photo-slot retention regression is in a
  separate follow-up change. These are isolated-worktree code checks, not a
  production or browser-session claim.
