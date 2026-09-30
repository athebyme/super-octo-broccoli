"""Synthetic 10k-row atomic final-apply capacity measurement, no provider I/O."""

from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import resource
import sqlite3
import tempfile
import threading
import time

from cryptography.fernet import Fernet
from flask import Flask


OUT = Path(os.environ.get('OZON_BROWSER_ARTIFACTS', '/artifacts'))
OUT.mkdir(parents=True, exist_ok=True)
temporary = tempfile.TemporaryDirectory(prefix='warehouse-capacity-', dir=OUT)
database = Path(temporary.name) / 'synthetic.sqlite'
os.environ['ENCRYPTION_KEY'] = Fernet.generate_key().decode()
app = Flask(__name__)
app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///' + str(database),
                  SQLALCHEMY_TRACK_MODIFICATIONS=False)

from models import (db, Marketplace, MarketplaceListing, MarketplaceWarehouse,
                    MarketplaceWarehouseReadJob, MarketplaceWarehouseReadItem,
                    MarketplaceWarehouseStock, Seller, SellerMarketplaceAccount, User)
from services.marketplace_credential_identity import ozon_credential_fingerprint
from services import ozon_warehouse_reads as reads
from services.marketplace_operation_locks import try_account_operation_lock

db.init_app(app)


def rss_kib():
    for line in Path('/proc/self/status').read_text().splitlines():
        if line.startswith('VmRSS:'):
            return int(line.split()[1])
    return 0


def stable_json(item):
    return json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


with app.app_context():
    db.create_all()
    with sqlite3.connect(database) as writer:
        assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() == 'wal'
    user = User(username='warehouse-capacity', email='warehouse-capacity@test.local', is_active=True)
    user.set_password('synthetic-password')
    seller = Seller(user=user, company_name='Warehouse Capacity')
    market = Marketplace(name='Ozon', code='ozon', adapter_code='ozon', is_active=True)
    db.session.add_all([seller, market])
    db.session.flush()
    account = SellerMarketplaceAccount(seller_id=seller.id, marketplace_id=market.id,
                                       external_account_id='synthetic-capacity-client', label='Capacity',
                                       is_active=True, connection_status='connected')
    account.set_credentials({'api_key': 'synthetic-capacity-key'})
    db.session.add(account)
    db.session.flush()
    listing = MarketplaceListing(seller_id=seller.id, marketplace_id=market.id,
                                 account_id=account.id, offer_id='capacity-offer',
                                 external_product_id='101', primary_sku='9001',
                                 normalized_status='active', is_available=True,
                                 sync_fingerprint='a' * 64)
    db.session.add(listing)
    db.session.commit()
    seller_id, market_id, account_id, listing_id = seller.id, market.id, account.id, listing.id
    external_account_id = account.external_account_id
    marker = ozon_credential_fingerprint(account)

    def stage(kind, ids, *, update=False):
        now = datetime.utcnow()
        job = MarketplaceWarehouseReadJob(
            seller_id=seller_id, marketplace_id=market_id, account_id=account_id,
            kind=kind, listing_id=listing_id if kind == 'fbs_stock' else None,
            external_account_id=external_account_id,
            offer_id='capacity-offer' if kind == 'fbs_stock' else None,
            external_product_id='101' if kind == 'fbs_stock' else None,
            credential_fingerprint=marker, status='running',
            next_due_at=now, lease_token='capacity-' + kind + '-' + str(int(update)),
            lease_expires_at=now + timedelta(hours=1),
            failure_count=0, page_count=100, next_cursor=None,
            seen_cursor_hashes_json='[]', requested_at=now, created_at=now, updated_at=now)
        db.session.add(job)
        db.session.flush()
        rows, total_bytes = [], 0
        for index, external_id in enumerate(ids):
            if kind == 'warehouses':
                item = {'warehouse_id': external_id,
                        'name': ('Changed ' if update else 'Original ') + external_id,
                        'status': 'created', 'warehouse_type': 'ORDINARY',
                        'flags': {}, 'limits': {}}
            else:
                item = {'warehouse_id': external_id, 'offer_id': 'capacity-offer',
                        'product_id': '101', 'sku': str(900000 + index),
                        'warehouse_name': 'Capacity ' + external_id,
                        'present': 11 + int(update), 'reserved': 3,
                        'free_stock': 8 + int(update)}
            payload = stable_json(item)
            size = len(payload.encode())
            total_bytes += size
            rows.append({'job_id': job.id, 'external_warehouse_id': external_id,
                         'item_kind': kind, 'normalized_json': payload,
                         'normalized_bytes': size,
                         'fingerprint': hashlib.sha256(payload.encode()).hexdigest(),
                         'observed_at': now})
        job.staged_count = len(rows)
        job.staged_bytes = total_bytes
        db.session.bulk_insert_mappings(MarketplaceWarehouseReadItem, rows)
        db.session.commit()
        return job.id, job.lease_token

    def measure(name, kind, ids, *, update=False):
        job_id, token = stage(kind, ids, update=update)
        db.session.remove()
        events = []
        connection = db.session.connection().connection.driver_connection
        def trace(sql):
            head = sql.lstrip().upper()
            if head.startswith('BEGIN') or head.startswith('COMMIT'):
                events.append((head.split()[0], time.perf_counter()))
        connection.set_trace_callback(trace)
        stop = threading.Event()
        read_latencies = []
        read_errors = []
        def concurrent_reader():
            con = sqlite3.connect(database, timeout=1)
            try:
                while not stop.is_set():
                    before = time.perf_counter()
                    try:
                        con.execute('SELECT count(*) FROM marketplace_warehouses').fetchone()
                        read_latencies.append(time.perf_counter() - before)
                    except sqlite3.Error as exc:
                        read_errors.append(type(exc).__name__)
                    time.sleep(0.005)
            finally:
                con.close()
        reader = threading.Thread(target=concurrent_reader, daemon=True)
        reader.start()
        before_rss = rss_kib()
        start = time.perf_counter()
        claim = try_account_operation_lock(account_id)
        assert claim is not None
        try:
            completed = reads._finalize(job_id, token, datetime.utcnow())
        finally:
            claim.close()
        elapsed = time.perf_counter() - start
        stop.set()
        reader.join(timeout=3)
        connection.set_trace_callback(None)
        assert completed
        assert MarketplaceWarehouseReadItem.query.filter_by(job_id=job_id).count() == 0
        begins = [value for event, value in events if event == 'BEGIN']
        commits = [value for event, value in events if event == 'COMMIT']
        writer_held = (commits[-1] - begins[0]) if begins and commits else None
        result = {'case': name, 'rows': len(ids), 'wall_seconds': round(elapsed, 3),
                  'writer_held_seconds': round(writer_held, 3) if writer_held is not None else None,
                  'rss_before_mib': round(before_rss / 1024, 1),
                  'rss_after_mib': round(rss_kib() / 1024, 1),
                  'peak_rss_mib': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
                  'max_concurrent_read_ms': round(max(read_latencies, default=0) * 1000, 1),
                  'concurrent_read_errors': read_errors,
                  'completed': completed}
        print(json.dumps(result), flush=True)
        db.session.remove()
        return result

    original_ids = [str(700000 + value) for value in range(10_000)]
    replacement_ids = original_ids[:5000] + [str(710000 + value) for value in range(5000)]
    results = []
    results.append(measure('warehouse_insert_10k', 'warehouses', original_ids))
    results.append(measure('warehouse_update_disappear_10k', 'warehouses', replacement_ids, update=True))
    results.append(measure('fbs_insert_10k', 'fbs_stock', replacement_ids))
    extra_ids = [str(720000 + value) for value in range(5000)]
    now = datetime.utcnow()
    db.session.bulk_insert_mappings(MarketplaceWarehouse, [{
        'seller_id': seller_id, 'marketplace_id': market_id, 'account_id': account_id,
        'external_warehouse_id': external_id, 'name': 'Extra ' + external_id,
        'status': 'created', 'warehouse_type': 'ORDINARY', 'flags_json': '{}',
        'limits_json': '{}', 'is_available': True,
        'sync_fingerprint': 'c' * 64, 'last_seen_at': now, 'last_synced_at': now,
        'created_at': now, 'updated_at': now,
    } for external_id in extra_ids])
    db.session.commit()
    results.append(measure('fbs_update_disappear_10k', 'fbs_stock', replacement_ids[:5000] + extra_ids, update=True))
    report = {'status': 'passed' if all(r['writer_held_seconds'] is not None
                                         and r['writer_held_seconds'] <= 2
                                         and r['peak_rss_mib'] < 2048
                                         and not r['concurrent_read_errors'] for r in results) else 'capacity_gate_failed',
              'cases': results, 'synthetic_only': True,
              'gate': {'max_atomic_apply_seconds': 2, 'max_peak_rss_mib': 2048}}
    (OUT / 'warehouse-read-capacity.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'cases': len(results)}), flush=True)

temporary.cleanup()
if report['status'] != 'passed':
    raise SystemExit(1)
