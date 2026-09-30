"""Run synthetic regression in the dedicated offline container, never a live DB."""
import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
EXTRA_TESTS = (
    'test_scheduler_heartbeat.py', 'test_startup_migrations.py',
    'test_migration_foreign_key_safety.py', 'test_migration_scan_scope.py',
    'test_migration_scoped_batch.py', 'test_verified_sqlite_backup.py', 'test_local_operations.py',
    'test_verified_sqlite_restore.py', 'test_photo_delivery_nonblocking.py',
    'test_photo_proxy.py', 'test_api_settings_marketplaces.py',
    'test_content_factory_marketplace_scope.py', 'test_content_factory_marketplace_routes.py',
    'test_my_products_filters.py', 'test_inbox_read_queue_migration.py',
    'test_deploy_telegram.py',
    'test_unauthorized_response_shape.py',
    'test_login_next.py',
    'test_ai_parsing_budget.py', 'test_supplier_flash_profile.py',
    'test_supplier_catalog_enrichment.py', 'test_supplier_catalog_enrichment_routes.py',
    'test_supplier_enrichment_parallel.py', 'test_enrichment_inference.py',
    'test_admin_flash_handoff.py',
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-only', action='store_true', help='Debug browser suite; not a full release check.')
    args = parser.parse_args()
    # Host checkout can hold production DB/credentials; do not import the app there.
    if not Path('/.dockerenv').exists() or os.geteuid() == 0:
        parser.error('Use scripts/check_ozon_release.sh (isolated, non-root container).')
    interfaces = {p.name for p in Path('/sys/class/net').iterdir()}
    if interfaces != {'lo'}:
        parser.error('The check requires Docker --network=none.')
    if not shutil.which('node') or not shutil.which('chromium'):
        parser.error('Node and Chromium are required; skipped UI tests are not accepted.')
    if any((ROOT / name).exists() for name in ('.env', '.env.autodeploy', 'data/seller_platform.db')):
        parser.error('The check requires a clean image without credentials or a previous application DB.')

    out = Path('/artifacts')
    out.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update(SKIP_SCHEDULER='1', PYTHONPATH=str(ROOT), PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
               DATABASE_URL='sqlite:////tmp/ozon-contract-import.db',
               SECRET_KEY='synthetic-ci-session-only',
               ENCRYPTION_KEY=base64.urlsafe_b64encode(b'0'*32).decode('ascii'),
               MARKETPLACE_OZON_ENABLED='1', MARKETPLACE_OZON_PUBLICATION_ENABLED='0',
               MARKETPLACE_OZON_COMMERCIAL_WRITES_ENABLED='0', MARKETPLACE_OZON_AUTO_PUBLISH_ENABLED='0',
               IMAGE_LAB_INLINE_WORKER='0', OZON_RATE_LIMIT_DIR='/tmp/ozon-ci-limits')
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'running',
              'network': 'none', 'synthetic_only': True, 'browser_only': args.browser_only,
              'hosted_ci_execution_proven': False, 'production_or_live_api_verified': False, 'stages': []}
    started = time.monotonic()

    def stage(name, command, timeout):
        stamp = time.monotonic()
        with (out / f'{name}.log').open('w') as log:
            try:
                result = subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                                        stderr=subprocess.STDOUT, timeout=timeout)
                code = result.returncode
            except subprocess.TimeoutExpired:
                code = 124
        report['stages'].append({'name': name, 'exit_code': code, 'seconds': round(time.monotonic()-stamp, 2)})
        print(json.dumps(report['stages'][-1]), flush=True)
        if code:
            # Only synthetic test output; never include container environment or DB.
            print((out / f'{name}.log').read_text()[-12000:], flush=True)
            raise RuntimeError(f'{name} failed ({code})')

    try:
        if not args.browser_only:
            selected = set((ROOT / 'tests').glob('test_ozon_*.py')) | set((ROOT / 'tests').glob('test_marketplace_*.py'))
            selected.update(ROOT / 'tests' / name for name in EXTRA_TESTS)
            assert len(selected) >= 80 and all(path.is_file() for path in selected)
            files = [str(path.relative_to(ROOT)) for path in sorted(selected)]
            report['test_files'] = files
            stage('contracts', [sys.executable, '-m', 'pytest', '-q', '--tb=short',
                                '--junitxml=/artifacts/contracts.xml', *files], 1800)
            suites = ET.parse(out / 'contracts.xml').getroot().findall('testsuite')
            totals = {key: sum(int(s.attrib[key]) for s in suites) for key in ('tests', 'failures', 'errors', 'skipped')}
            assert totals['tests'] > 0 and all(totals[key] == 0 for key in ('failures', 'errors', 'skipped')), totals
            report['contracts'] = totals
        stage('browser', [sys.executable, '-m', 'tests.ozon_release.browser'], 600)
        browser = json.loads((out / 'browser.json').read_text())
        assert browser['status'] == 'passed' and browser['checks']
        report['browser'] = browser
        stage('bulk-browser', [sys.executable, '-m', 'tests.ozon_release.bulk_repair_browser'], 600)
        bulk = json.loads((out / 'bulk-browser.json').read_text())
        assert bulk['status'] == 'passed' and len(bulk['checks']) >= 9
        assert bulk['rows'] == 200 and bulk['provider_attempts'] == 0
        assert not bulk['js_errors'] and not bulk['unexpected_http'] and not bulk['external']
        report['bulk_browser'] = bulk
        stage('credential-browser', [sys.executable, '-m', 'tests.ozon_release.credential_browser'], 600)
        credential = json.loads((out / 'credential-browser.json').read_text())
        assert credential['status'] == 'passed' and len(credential['checks']) >= 7
        assert not credential['js_errors'] and not credential['unexpected_http'] and not credential['external']
        assert credential['provider_attempts'] == 0
        report['credential_browser'] = credential
        stage('account-history-browser', [sys.executable, '-m', 'tests.ozon_release.account_history_browser'], 600)
        history = json.loads((out / 'account-history-browser.json').read_text())
        assert history['status'] == 'passed' and len(history['checks']) >= 8
        assert not history['js_errors'] and not history['unexpected_http'] and not history['external']
        assert history['provider_attempts'] == 0
        report['account_history_browser'] = history
        stage('quarantine-browser', [sys.executable, '-m', 'tests.ozon_release.quarantine_browser'], 600)
        quarantine = json.loads((out / 'quarantine-browser-report.json').read_text())
        assert quarantine['status'] == 'passed' and len(quarantine['checks']) >= 10
        assert len(quarantine['layouts']) >= 40 and quarantine['provider_attempts'] == 0
        assert not quarantine['js_errors'] and not quarantine['unexpected_http'] and not quarantine['external']
        report['quarantine_browser'] = quarantine
        stage('quarantine-commercial-browser', [sys.executable, '-m', 'tests.ozon_release.quarantine_commercial_browser'], 600)
        held_commercial = json.loads((out / 'quarantine-commercial-browser-report.json').read_text())
        assert held_commercial['status'] == 'passed' and len(held_commercial['checks']) >= 4
        assert len(held_commercial['layouts']) >= 16 and held_commercial['provider_attempts'] == 0
        assert not held_commercial['js_errors'] and not held_commercial['unexpected_http'] and not held_commercial['external']
        report['quarantine_commercial_browser'] = held_commercial
        for stage_name, module, filename, minimum_checks, minimum_layouts in (
            ('warehouse-read-browser', 'warehouse_read_browser', 'warehouse-read-browser-report.json', 6, 32),
            ('vue-link-category-browser', 'vue_link_category_browser', 'vue-link-category-browser.json', 4, 24),
            ('upload-two-step-browser', 'upload_two_step_browser', 'upload-two-step-browser.json', 8, 48),
            ('draft-ai-browser', 'draft_ai_browser', 'draft-ai-browser.json', 5, 40),
            ('draft-editor-photo-browser', 'draft_editor_photo_browser', 'draft-editor-photo-browser.json', 8, 12),
        ):
            stage(stage_name, [sys.executable, '-m', 'tests.ozon_release.' + module], 600)
            checked = json.loads((out / filename).read_text())
            assert checked['status'] == 'passed' and len(checked['checks']) >= minimum_checks
            assert len(checked['layouts']) >= minimum_layouts
            assert checked['provider_attempts'] == 0
            assert not checked['js_errors'] and not checked['unexpected_http'] and not checked['external']
            report[stage_name.replace('-', '_')] = checked
        report['status'] = 'passed'
        return 0
    except Exception as error:
        report['status'] = 'failed'
        report['error'] = f'{type(error).__name__}: {error}'
        print(report['error'], flush=True)
        return 1
    finally:
        report['seconds'] = round(time.monotonic() - started, 2)
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        (out / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'status': report['status'], 'seconds': report['seconds']}), flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
