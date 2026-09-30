"""Reviewed, ordered Docker database migration command plan.

Keep this plan explicit: ordinary application routes and presentation code do
not participate in migration fingerprints. Each command is one durable step.
"""
from __future__ import annotations

from dataclasses import dataclass


PLAN_FORMAT = 1


@dataclass(frozen=True)
class MigrationStep:
    key: str
    kind: str
    script: str = ""
    args: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    batch_group: str = ""


def _python(script: str, *args: str, dependencies: tuple[str, ...] = ()) -> MigrationStep:
    name = script.removeprefix("migrations/").removesuffix(".py")
    return MigrationStep(name.replace("_", "-"), "python", script, tuple(args), dependencies)


def _scoped(script: str, group: str) -> MigrationStep:
    name = script.removeprefix("migrations/").removesuffix(".py")
    return MigrationStep(name.replace("_", "-"), "scoped", script, batch_group=group)


def migration_steps() -> tuple[MigrationStep, ...]:
    """The fail-fast migration order previously executed by docker-entrypoint."""
    steps: list[MigrationStep] = [
        MigrationStep(
            "bootstrap-database", "bootstrap", "scripts/startup_migration_bootstrap.py",
            dependencies=("models.py", "seller_platform.py"),
        ),
        _python("migrations/migrate_db.py", "--db-path", "{database}"),
        _python("migrations/migrate_add_characteristics.py", "{database}"),
        _python("migrations/migrate_add_history_and_logging.py", "--db-path", "{database}"),
        _python("migrations/migrate_add_subject_id.py", "{database}"),
        _python("migrations/migrate_add_price_monitoring.py"),
        _python("migrations/migrate_add_product_sync_settings.py"),
        _python("migrations/migrate_add_admin_features.py"),
        _python("migrations/migrate_add_card_merge_history.py", "--db-path", "{database}"),
        _python("migrations/migrate_add_supplier_price.py"),
        _python("migrations/migrate_add_safe_price_change.py"),
        _python("migrations/migrate_add_unlimited_batch.py"),
        _python("migrations/migrate_add_blocked_cards.py"),
        _python("migrations/migrate_add_price_stock_sync.py", "{database}"),
        _python("migrations/migrate_add_marketplace_tables.py"),
    ]

    # Keep each child as a separately journaled transaction. A helper change
    # affects the steps importing it, while completed neighbors remain reusable.
    for script in (
        "migrations/migrate_add_marketplace_accounts.py",
        "migrations/migrate_add_marketplace_credential_notices.py",
        "migrations/migrate_add_marketplace_account_events.py",
        "migrations/migrate_add_ozon_references.py",
        "migrations/migrate_add_ozon_product_type_visibility.py",
        "migrations/migrate_add_ozon_reference_reviews.py",
        "migrations/migrate_add_ozon_compliance_defaults.py",
        "migrations/migrate_add_marketplace_reference_freshness.py",
        "migrations/migrate_add_brand_category_external_id.py",
        "migrations/migrate_add_wb_dictionary_provenance.py",
        "migrations/migrate_add_marketplace_listings.py",
        "migrations/migrate_add_ozon_catalog_checkpoints.py",
        "migrations/migrate_add_marketplace_product_links.py",
        "migrations/migrate_add_marketplace_canonical_content.py",
        "migrations/migrate_add_marketplace_rollout.py",
        "migrations/migrate_add_marketplace_drafts.py",
        "migrations/migrate_add_marketplace_draft_attribute_removals.py",
        "migrations/migrate_add_marketplace_operations.py",
        "migrations/migrate_add_ozon_upload_queue.py",
        "migrations/migrate_add_ozon_draft_ai_completion.py",
        "migrations/migrate_add_marketplace_commercial.py",
        "migrations/migrate_add_ozon_warehouse_reads.py",
        "migrations/migrate_add_marketplace_product_updates.py",
        "migrations/migrate_add_marketplace_auto_publish.py",
        "migrations/migrate_add_marketplace_quality_analytics.py",
        "migrations/migrate_add_marketplace_fulfillment.py",
        "migrations/migrate_add_marketplace_finance.py",
        "migrations/migrate_add_marketplace_inbox.py",
        "migrations/migrate_add_marketplace_read_schedules.py",
        "migrations/migrate_add_marketplace_read_requests.py",
        "migrations/migrate_add_inbox_read_queue.py",
        "migrations/migrate_add_marketplace_read_credential_identity.py",
    ):
        steps.append(_scoped(script, "marketplace-reference-batch"))

    steps.extend([
        _python("migrations/add_ai_job_model_field.py"),
        _python("migrations/add_ai_job_heartbeat.py"),
        _python("migrations/add_parsing_quality_fields.py"),
        _python("migrations/migrate_add_supplier_catalog_enrichment.py", "{database}"),
        _python("migrations/migrate_add_service_agents.py", "{database}"),
        _python("migrations/migrate_add_card_quality_v2.py", "{database}"),
        _python("migrations/migrate_add_agent_chat.py", "{database}"),
        _python("migrations/migrate_add_agent_knowledge.py", "{database}"),
        _python("migrations/run_all_migrations.py", "{database}", "--base-only"),
        _python("migrations/migrate_add_imported_wb_nm_id.py", "{database}"),
        _python("migrations/migrate_add_image_generation_lab.py", "{database}"),
        _python("migrations/migrate_add_image_lab_reference_watermark.py", "{database}"),
        _python("migrations/migrate_add_image_lab_angle_synthesis.py", "{database}"),
        _python("migrations/migrate_add_image_lab_marketplace_target.py", "{database}"),
    ])

    for script in (
        "migrations/migrate_add_infographic_campaigns.py",
        "migrations/migrate_add_marketplace_media_publications.py",
        "migrations/migrate_add_marketplace_write_quarantine.py",
        "migrations/migrate_add_bestseller_image_recommendations.py",
        "migrations/migrate_add_content_factory_marketplace_scope.py",
        "migrations/migrate_add_social_account_publish_health.py",
    ):
        steps.append(_scoped(script, "media-quarantine-batch"))

    steps.extend([
        _python("migrations/migrate_add_sexopt_supplier.py", "{database}"),
        _python("migrations/migrate_andrey_feed_full_ingest.py", "{database}"),
        _python("migrations/migrate_add_enrichment_inference.py", "{database}"),
        _python("migrations/migrate_clean_characteristic_dimensions.py", "{database}"),
        _python("migrations/migrate_add_wb_card_audit.py", "{database}"),
        _python("migrations/migrate_add_enrichment_merge_audit.py", "{database}"),
        _python("migrations/migrate_enrichment_reliability_v2.py", "{database}"),
        _python("migrations/migrate_competitor_monitor_v2.py", "{database}"),
        _python("migrations/migrate_add_competitor_matching.py", "{database}"),
        _python("migrations/migrate_add_competitor_price_lanes.py", "{database}"),
        _python("migrations/migrate_backfill_imported_supplier_links.py", "{database}"),
        _python("migrations/migrate_compact_competitor_snapshots.py", "{database}"),
    ])

    steps.append(_python(
        "migrations/migrate_add_imported_content_overrides.py",
        "{database}",
    ))
    steps.append(_python(
        "migrations/migrate_add_wb_bulk_review_key.py",
        "{database}",
    ))

    keys = [step.key for step in steps]
    if len(keys) != len(set(keys)):
        raise RuntimeError("Startup migration plan contains duplicate step keys")
    return tuple(steps)
