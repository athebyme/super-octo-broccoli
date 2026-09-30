#!/usr/bin/env python3
"""Run reviewed additive migrations in order, committing each on one connection.

The regular startup guard remains the owner of locking and success recording.
This runner neither skips a migration nor creates a startup success journal.
"""
from __future__ import annotations

import argparse
from importlib import import_module
from pathlib import Path
import sqlite3
import subprocess
import sys
from time import monotonic

from migrations._foreign_key_safety import reuse_foreign_key_scans

# Preserve each standalone wrapper's foreign_keys setting. Rebuild migrations
# with special connection/transaction semantics deliberately keep their own CLI.
_FOREIGN_KEYS_OFF = (
    'marketplace_accounts', 'ozon_references', 'marketplace_reference_freshness',
    'brand_category_external_id', 'wb_dictionary_provenance',
    'marketplace_listings', 'social_account_publish_health',
)
_FOREIGN_KEYS_ON = (
    'ozon_product_type_visibility', 'ozon_reference_reviews', 'marketplace_credential_notices', 'marketplace_account_events',
    'marketplace_product_links', 'marketplace_canonical_content',
    'marketplace_rollout', 'marketplace_drafts', 'marketplace_operations',
    'ozon_upload_queue',
    'ozon_draft_ai_completion',
    'marketplace_commercial', 'marketplace_quality_analytics',
    'marketplace_fulfillment', 'marketplace_finance', 'marketplace_inbox',
    'marketplace_read_schedules', 'marketplace_read_requests',
    'infographic_campaigns', 'marketplace_media_publications',
    'marketplace_write_quarantine',
    'ozon_catalog_checkpoints', 'ozon_warehouse_reads',
    'marketplace_read_credential_identity',
    'bestseller_image_recommendations',
)
PROFILES = {f'migrations/migrate_add_{name}.py': enabled
            for names, enabled in ((_FOREIGN_KEYS_OFF, False), (_FOREIGN_KEYS_ON, True))
            for name in names}
LISTINGS = 'migrations/migrate_add_marketplace_listings.py'
# These keep their original Python CLI, connection, FK mode and rebuild rules.
# The batch connection holds no transaction while the child runs. A child write
# changes its data_version, so the next shared scan cannot reuse old evidence.
STANDALONE = frozenset(f'migrations/migrate_add_{name}.py' for name in (
    'ozon_compliance_defaults', 'marketplace_draft_attribute_removals',
    'marketplace_product_updates', 'marketplace_auto_publish',
    'image_lab_marketplace_target', 'content_factory_marketplace_scope',
    'supplier_catalog_enrichment', 'inbox_read_queue',
))


def run_batch(
    database_path: str | Path,
    scripts: list[str],
    *,
    verbose=True,
    skip_scripts=(),
    before_step=None,
    after_step=None,
    connection: sqlite3.Connection | None = None,
) -> None:
    if (not scripts or len(scripts) > 32 or len(set(scripts)) != len(scripts)
            or any(name not in PROFILES and name not in STANDALONE for name in scripts)):
        raise ValueError('Migration batch must contain reviewed, unique script paths')
    skip_scripts = set(skip_scripts)
    if not skip_scripts <= set(scripts):
        raise ValueError('Skipped migration must belong to the reviewed batch')
    selected = [name for name in scripts if name not in skip_scripts]
    if not selected:
        return
    # Resolve/import before opening the database; an unsupported script is not
    # allowed to leave the first part of an accidentally configured batch applied.
    steps = [(name, None if name in STANDALONE else import_module(name[:-3].replace('/', '.')).apply_migration)
             for name in selected]
    path = Path(database_path).resolve()
    owns_connection = connection is None
    if owns_connection:
        connection = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True)
    try:
        with reuse_foreign_key_scans(connection):
            for name, apply in steps:
                started = monotonic()
                if connection.in_transaction:
                    raise RuntimeError('Migration step did not release its transaction')
                if before_step is not None:
                    before_step(connection, name)
                if connection.in_transaction:
                    raise RuntimeError('Migration journal callback left a transaction open')
                if apply is None:
                    project_root = Path(__file__).resolve().parent.parent
                    subprocess.run([sys.executable, str(project_root / name), str(path)],
                                   cwd=project_root, check=True)
                    if after_step is not None:
                        after_step(connection, name)
                    if connection.in_transaction:
                        raise RuntimeError('Migration journal callback left a transaction open')
                    if verbose:
                        print(f'Migration batch: {Path(name).stem} standalone complete ({monotonic()-started:.3f}s)', flush=True)
                    continue
                # Every standalone migration starts with these default factories
                # and its own FK mode. A previous step must not leak them.
                connection.row_factory = None
                connection.text_factory = str
                connection.execute('PRAGMA foreign_keys=' + ('ON' if PROFILES[name] else 'OFF'))
                if bool(connection.execute('PRAGMA foreign_keys').fetchone()[0]) != PROFILES[name]:
                    raise RuntimeError('Migration step did not release its transaction')
                kwargs = {'backfill_limit': 200} if name == LISTINGS else {}
                try:
                    apply(connection, verbose=verbose, **kwargs)
                    connection.commit()
                    if after_step is not None:
                        after_step(connection, name)
                    if connection.in_transaction:
                        raise RuntimeError('Migration journal callback left a transaction open')
                except Exception:
                    connection.rollback()
                    raise
                if verbose:
                    print(f'Migration batch: {Path(name).stem} complete ({monotonic()-started:.3f}s)', flush=True)
    finally:
        if owns_connection:
            connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('scripts', nargs='+', choices=sorted(set(PROFILES) | STANDALONE))
    args = parser.parse_args()
    run_batch(args.database, args.scripts)


if __name__ == '__main__':
    main()
