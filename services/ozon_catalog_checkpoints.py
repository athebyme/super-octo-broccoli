"""Private, durable continuation of one Ozon catalog list page.

Only normalized provider facts are staged. A checkpoint is never a listing
projection; the caller applies it in one short transaction after every domain
has completed. No database write transaction spans a provider call.
"""

from datetime import datetime, timedelta
import hashlib
import json

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from models import (
    BackgroundJob,
    MarketplaceCatalogPageCheckpoint,
    MarketplaceCatalogPageItem,
    db,
)
from services.marketplace_operation_locks import _try_operation_lock
from services.marketplace_listings import MarketplaceCatalogProtocolError
from services.ozon_read_response import OzonReadResponseTooLarge


# These are conservative local staging candidates, not Ozon quotas. The
# normal 1000-ID request remains unchanged; a page shrinks only on measured
# local overflow before applying any listing.
PAGE_STAGE_BYTES = 64 * 1024 * 1024
GLOBAL_STAGE_BYTES = 512 * 1024 * 1024
MAX_ACTIVE_CHECKPOINTS = GLOBAL_STAGE_BYTES // PAGE_STAGE_BYTES
MAX_GENERATIONS = 16
MAX_TTL_RESTARTS = 8
PAGE_TTL = timedelta(minutes=30)
MAX_ITEM_BYTES = 2 * 1024 * 1024
MAX_BASE_BYTES = 2 * 1024 * 1024
DOMAINS = ('info', 'attributes', 'prices', 'stocks')
NEXT_DOMAIN = {'info': 'attributes', 'attributes': 'prices', 'prices': 'stocks', 'stocks': 'apply'}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _parse_json(value, fallback):
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        raise MarketplaceCatalogProtocolError('Invalid private catalog checkpoint JSON') from None
    if not isinstance(result, type(fallback)):
        raise MarketplaceCatalogProtocolError('Invalid private catalog checkpoint shape')
    return result


def _base_hash(checkpoint, encoded):
    material = _json({
        'run_id': checkpoint.run_id,
        'seller_id': checkpoint.seller_id,
        'marketplace_id': checkpoint.marketplace_id,
        'account_id': checkpoint.account_id,
        'fingerprint': checkpoint.credential_fingerprint,
        'phase': checkpoint.phase,
        'visibility': checkpoint.visibility,
        'start_cursor': checkpoint.start_cursor,
        'next_cursor': checkpoint.next_cursor,
        'total': checkpoint.list_total,
        'limit': checkpoint.page_limit,
        'base': encoded,
    })
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def _stage_item_json(domain, item):
    value = dict(item)
    if domain == 'info':
        for key in ('created_at', 'updated_at'):
            if isinstance(value.get(key), datetime):
                value[key] = value[key].isoformat()
    encoded = _json(value)
    size = len(encoded.encode('utf-8'))
    if size > MAX_ITEM_BYTES:
        raise MarketplaceCatalogProtocolError('Normalized Ozon catalog item exceeds staging limit')
    return encoded, size


def _decode_stage_item(domain, encoded):
    item = _parse_json(encoded, {})
    if domain == 'info':
        for key in ('created_at', 'updated_at'):
            value = item.get(key)
            if value is not None:
                if not isinstance(value, str):
                    raise MarketplaceCatalogProtocolError('Invalid staged Ozon datetime')
                try:
                    item[key] = datetime.fromisoformat(value)
                except ValueError:
                    raise MarketplaceCatalogProtocolError('Invalid staged Ozon datetime') from None
    return item


class CatalogCheckpointDeferred(RuntimeError):
    """A local capacity gate yielded this tick without a provider failure."""


class CatalogCheckpointTerminal(MarketplaceCatalogProtocolError):
    """This page exhausted its durable generation/TTL retry bound."""

    code = 'ozon_catalog_checkpoint_exhausted'


class CatalogPageCheckpointService:
    @classmethod
    def _delete(cls, checkpoint):
        MarketplaceCatalogPageItem.query.filter_by(checkpoint_id=checkpoint.id).delete(synchronize_session=False)
        db.session.delete(checkpoint)

    @classmethod
    def discard_for_run(cls, run_id):
        checkpoint = MarketplaceCatalogPageCheckpoint.query.filter_by(run_id=run_id).first()
        if checkpoint is not None:
            cls._delete(checkpoint)
            db.session.commit()

    @classmethod
    def _reset(cls, checkpoint, *, now, page_limit=None, ttl=False):
        if ttl and checkpoint.ttl_restarts >= MAX_TTL_RESTARTS:
            raise CatalogCheckpointTerminal('Ozon catalog page checkpoint repeatedly expired')
        if checkpoint.generation >= MAX_GENERATIONS:
            raise CatalogCheckpointTerminal('Ozon catalog page staging generations exhausted')
        MarketplaceCatalogPageItem.query.filter_by(checkpoint_id=checkpoint.id).delete(synchronize_session=False)
        checkpoint.domain = 'list'
        checkpoint.next_cursor = ''
        checkpoint.list_total = None
        checkpoint.base_items_json = '[]'
        checkpoint.base_hash = None
        checkpoint.domain_cursor = ''
        checkpoint.domain_total = None
        checkpoint.domain_seen_count = 0
        checkpoint.domain_page_count = 0
        checkpoint.domain_observed_at_json = '{}'
        checkpoint.staged_bytes = 0
        checkpoint.list_observed_at = None
        checkpoint.started_at = now
        checkpoint.updated_at = now
        checkpoint.generation += 1
        if ttl:
            checkpoint.ttl_restarts += 1
        if page_limit is not None:
            checkpoint.page_limit = page_limit
        db.session.commit()

    @classmethod
    def _quiesce_terminal(cls, checkpoint, *, now):
        """Free staged bytes but retain exhausted counters for the owner job."""
        MarketplaceCatalogPageItem.query.filter_by(checkpoint_id=checkpoint.id).delete(synchronize_session=False)
        checkpoint.domain = 'list'
        checkpoint.base_items_json = '[]'
        checkpoint.base_hash = None
        checkpoint.next_cursor = ''
        checkpoint.list_total = None
        checkpoint.list_observed_at = None
        checkpoint.domain_cursor = ''
        checkpoint.domain_total = None
        checkpoint.domain_seen_count = 0
        checkpoint.domain_page_count = 0
        checkpoint.domain_observed_at_json = '{}'
        checkpoint.staged_bytes = 0
        checkpoint.started_at = now - PAGE_TTL - timedelta(seconds=1)
        checkpoint.updated_at = now
        db.session.commit()

    @classmethod
    def _shrink(cls, checkpoint, *, now):
        if checkpoint.page_limit <= 1:
            raise MarketplaceCatalogProtocolError('One Ozon catalog product exceeds staging budget')
        try:
            cls._reset(checkpoint, now=now, page_limit=max(1, checkpoint.page_limit // 2))
        except CatalogCheckpointTerminal:
            cls._quiesce_terminal(checkpoint, now=now)
            raise

    @classmethod
    def _prune_expired(cls, *, now, own_account_id):
        """Reclaim private bytes only when a victim's catalog file claim is free."""
        from services.marketplace_listings import MarketplaceListingService

        expired = MarketplaceCatalogPageCheckpoint.query.filter(
            MarketplaceCatalogPageCheckpoint.started_at < now - PAGE_TTL,
            MarketplaceCatalogPageCheckpoint.account_id != own_account_id,
            MarketplaceCatalogPageCheckpoint.domain != 'list',
        ).order_by(MarketplaceCatalogPageCheckpoint.started_at).limit(16).all()
        for checkpoint in expired:
            claim = MarketplaceListingService._try_claim(checkpoint.account_id)
            if claim is None:
                continue
            try:
                try:
                    cls._reset(checkpoint, now=now, ttl=True)
                except CatalogCheckpointTerminal:
                    cls._quiesce_terminal(checkpoint, now=now)
            finally:
                MarketplaceListingService._release_claim(claim)

    @classmethod
    def _reclaim_future_due(cls, *, now, own_account_id):
        """Do not let long provider cooldowns occupy every staging slot."""
        from services.marketplace_listings import MarketplaceListingService

        candidates = MarketplaceCatalogPageCheckpoint.query.filter(
            MarketplaceCatalogPageCheckpoint.account_id != own_account_id,
            MarketplaceCatalogPageCheckpoint.domain != 'list',
        ).order_by(MarketplaceCatalogPageCheckpoint.updated_at).limit(16).all()
        for checkpoint in candidates:
            job = BackgroundJob.query.filter(
                BackgroundJob.seller_id == checkpoint.seller_id,
                BackgroundJob.job_type == 'ozon_account_sync',
                BackgroundJob.job_uid.like(f'oc:{checkpoint.account_id}:%'),
            ).order_by(BackgroundJob.id.desc()).first()
            if job is None:
                continue
            if job.status not in ('pending', 'running'):
                claim = MarketplaceListingService._try_claim(checkpoint.account_id)
                if claim is None:
                    continue
                try:
                    cls._quiesce_terminal(checkpoint, now=now)
                finally:
                    MarketplaceListingService._release_claim(claim)
                return True
            retry_at = job.get_result().get('next_retry_at')
            if not isinstance(retry_at, str):
                continue
            try:
                due = datetime.fromisoformat(retry_at.removesuffix('Z'))
            except ValueError:
                continue
            if due <= now + timedelta(seconds=60):
                continue
            claim = MarketplaceListingService._try_claim(checkpoint.account_id)
            if claim is None:
                continue
            try:
                try:
                    cls._reset(checkpoint, now=now)
                except CatalogCheckpointTerminal:
                    cls._quiesce_terminal(checkpoint, now=now)
            finally:
                MarketplaceListingService._release_claim(claim)
            return True
        return False

    @classmethod
    def _capacity_available(cls, *, now, own_account_id):
        capacity_claim = _try_operation_lock('ozon-catalog-stage-capacity', 1)
        if capacity_claim is None:
            return False
        try:
            cls._prune_expired(now=now, own_account_id=own_account_id)
            used = db.session.query(func.count(MarketplaceCatalogPageCheckpoint.id)).filter(
                MarketplaceCatalogPageCheckpoint.domain != 'list',
            ).scalar() or 0
            if used >= MAX_ACTIVE_CHECKPOINTS:
                if cls._reclaim_future_due(now=now, own_account_id=own_account_id):
                    used -= 1
            return used < MAX_ACTIVE_CHECKPOINTS
        finally:
            capacity_claim.close()

    @classmethod
    def get_or_create(cls, run, *, credential_fingerprint, now):
        checkpoint = MarketplaceCatalogPageCheckpoint.query.filter_by(run_id=run.id).first()
        if checkpoint is None:
            try:
                checkpoint = MarketplaceCatalogPageCheckpoint(
                    run_id=run.id, seller_id=run.seller_id,
                    marketplace_id=run.marketplace_id, account_id=run.account_id,
                    credential_fingerprint=credential_fingerprint,
                    phase=run.phase, visibility=run.visibility,
                    start_cursor=run.cursor, page_limit=1000,
                    domain='list', started_at=now, updated_at=now,
                )
                db.session.add(checkpoint)
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                checkpoint = MarketplaceCatalogPageCheckpoint.query.filter_by(run_id=run.id).one()
        if (checkpoint.seller_id != run.seller_id or checkpoint.marketplace_id != run.marketplace_id
                or checkpoint.account_id != run.account_id or checkpoint.run_id != run.id
                or checkpoint.credential_fingerprint != credential_fingerprint
                or checkpoint.phase != run.phase or checkpoint.visibility != run.visibility
                or checkpoint.start_cursor != run.cursor):
            raise MarketplaceCatalogProtocolError('Ozon catalog page checkpoint identity changed')
        if checkpoint.started_at < now - PAGE_TTL:
            try:
                cls._reset(checkpoint, now=now, ttl=True)
            except CatalogCheckpointTerminal:
                cls._quiesce_terminal(checkpoint, now=now)
                raise
        return checkpoint

    @classmethod
    def _base(cls, checkpoint):
        if checkpoint.domain == 'list':
            raise MarketplaceCatalogProtocolError('Ozon catalog base page is not yet observed')
        encoded = checkpoint.base_items_json
        if checkpoint.base_hash != _base_hash(checkpoint, encoded):
            raise MarketplaceCatalogProtocolError('Ozon catalog base page fingerprint changed')
        items = _parse_json(encoded, [])
        if len(items) > checkpoint.page_limit or len(items) > 1000:
            raise MarketplaceCatalogProtocolError('Ozon catalog base page exceeds request limit')
        product_ids = set()
        offer_ids = set()
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get('product_id'), str) or not isinstance(item.get('offer_id'), str):
                raise MarketplaceCatalogProtocolError('Invalid staged Ozon base item')
            if item['product_id'] in product_ids or item['offer_id'] in offer_ids:
                raise MarketplaceCatalogProtocolError('Duplicate staged Ozon base identity')
            product_ids.add(item['product_id'])
            offer_ids.add(item['offer_id'])
        if checkpoint.list_total is None or checkpoint.list_observed_at is None:
            raise MarketplaceCatalogProtocolError('Incomplete staged Ozon base page')
        return {'items': items, 'total': checkpoint.list_total, 'cursor': checkpoint.next_cursor}

    @classmethod
    def _save_list(cls, checkpoint, run, page, *, observed_at):
        if len(page['items']) > checkpoint.page_limit:
            raise MarketplaceCatalogProtocolError('Ozon list response exceeds requested limit')
        if run.phase_expected_total is not None and page['total'] != run.phase_expected_total:
            raise MarketplaceCatalogProtocolError('Ozon product list total changed during a phase')
        encoded = _json(page['items'])
        size = len(encoded.encode('utf-8'))
        if size > MAX_BASE_BYTES or size > PAGE_STAGE_BYTES:
            cls._shrink(checkpoint, now=observed_at)
            return False
        capacity_claim = _try_operation_lock('ozon-catalog-stage-capacity', 1)
        if capacity_claim is None:
            return False
        try:
            used = db.session.query(func.count(MarketplaceCatalogPageCheckpoint.id)).filter(
                MarketplaceCatalogPageCheckpoint.domain != 'list',
            ).scalar() or 0
            if used >= MAX_ACTIVE_CHECKPOINTS:
                return False
            checkpoint.base_items_json = encoded
            checkpoint.list_total = page['total']
            checkpoint.next_cursor = page['cursor']
            checkpoint.list_observed_at = observed_at
            checkpoint.staged_bytes = size
            checkpoint.domain = 'info' if page['items'] else 'apply'
            checkpoint.updated_at = observed_at
            checkpoint.base_hash = _base_hash(checkpoint, encoded)
            db.session.commit()
        finally:
            capacity_claim.close()
        return True

    @classmethod
    def _save_domain(cls, checkpoint, domain, items, *, observed_at, total=None, cursor=None):
        from services.marketplace_listings import MarketplaceListingService

        page = cls._base(checkpoint)
        base_by_product = {item['product_id']: item for item in page['items']}
        MarketplaceListingService._validate_enrichment_identities(items, base_by_product, 'product_' + domain)
        encoded_rows = []
        new_bytes = 0
        for product_id, item in items.items():
            encoded, size = _stage_item_json(domain, item)
            new_bytes += size
            if checkpoint.staged_bytes + new_bytes > PAGE_STAGE_BYTES:
                cls._shrink(checkpoint, now=observed_at)
                return False
            encoded_rows.append((product_id, encoded, size))
        if domain == 'info':
            if checkpoint.domain != 'info':
                raise MarketplaceCatalogProtocolError('Ozon catalog info stage changed')
            next_domain = 'attributes'
            next_cursor = ''
            next_total = None
            next_seen = 0
            next_pages = 0
        else:
            if checkpoint.domain != domain or total is None or cursor is None:
                raise MarketplaceCatalogProtocolError('Ozon catalog cursor stage changed')
            if checkpoint.domain_total is not None and total != checkpoint.domain_total:
                raise MarketplaceCatalogProtocolError('Ozon enrichment total changed during pagination')
            existing_ids = set(row[0] for row in db.session.query(MarketplaceCatalogPageItem.product_id).filter_by(
                checkpoint_id=checkpoint.id, domain=domain,
            ).all())
            if any(product_id in existing_ids for product_id in items):
                raise MarketplaceCatalogProtocolError('Ozon enrichment repeated product_id across pages')
            next_seen = checkpoint.domain_seen_count + len(items)
            next_pages = checkpoint.domain_page_count + 1
            if next_seen > total:
                raise MarketplaceCatalogProtocolError('Ozon enrichment returned more rows than total')
            if next_seen == total:
                next_domain = NEXT_DOMAIN[domain]
                next_cursor = ''
                next_total = None
                next_seen = 0
                next_pages = 0
            else:
                if not items or not cursor or cursor == checkpoint.domain_cursor:
                    raise MarketplaceCatalogProtocolError('Ozon enrichment pagination ended before total')
                if next_pages >= MarketplaceListingService.MAX_ENRICHMENT_PAGES:
                    raise MarketplaceCatalogProtocolError('Ozon enrichment exceeded pagination safety limit')
                next_domain = domain
                next_cursor = cursor
                next_total = total
        try:
            for product_id, encoded, size in encoded_rows:
                db.session.add(MarketplaceCatalogPageItem(
                    checkpoint_id=checkpoint.id, domain=domain,
                    product_id=product_id, item_json=encoded, byte_count=size,
                    observed_at=observed_at,
                ))
            observed = _parse_json(checkpoint.domain_observed_at_json, {})
            observed[domain] = observed_at.isoformat()
            checkpoint.domain_observed_at_json = _json(observed)
            checkpoint.staged_bytes += new_bytes
            checkpoint.domain = next_domain
            checkpoint.domain_cursor = next_cursor
            checkpoint.domain_total = next_total
            checkpoint.domain_seen_count = next_seen
            checkpoint.domain_page_count = next_pages
            checkpoint.updated_at = observed_at
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            raise MarketplaceCatalogProtocolError('Ozon enrichment repeated staged identity') from None
        return True

    @classmethod
    def _enrichment(cls, checkpoint):
        page = cls._base(checkpoint)
        base_by_product = {item['product_id']: item for item in page['items']}
        maps = {domain: {} for domain in DOMAINS}
        item_times = {domain: {} for domain in DOMAINS}
        rows = MarketplaceCatalogPageItem.query.filter_by(checkpoint_id=checkpoint.id).order_by(MarketplaceCatalogPageItem.id).all()
        total_bytes = len(checkpoint.base_items_json.encode('utf-8'))
        for row in rows:
            if row.domain not in maps or row.product_id in maps[row.domain]:
                raise MarketplaceCatalogProtocolError('Invalid staged Ozon enrichment identity')
            size = len(row.item_json.encode('utf-8'))
            if size != row.byte_count or size > MAX_ITEM_BYTES:
                raise MarketplaceCatalogProtocolError('Staged Ozon enrichment size changed')
            item = _decode_stage_item(row.domain, row.item_json)
            if item.get('product_id') != row.product_id:
                raise MarketplaceCatalogProtocolError('Staged Ozon product identity changed')
            maps[row.domain][row.product_id] = item
            item_times[row.domain][row.product_id] = row.observed_at
            total_bytes += size
        if total_bytes != checkpoint.staged_bytes:
            raise MarketplaceCatalogProtocolError('Staged Ozon page byte count changed')
        from services.marketplace_listings import MarketplaceListingService
        for domain, items in maps.items():
            MarketplaceListingService._validate_enrichment_identities(items, base_by_product, 'product_' + domain)
        observed = _parse_json(checkpoint.domain_observed_at_json, {})
        if page['items'] and any(domain not in observed for domain in DOMAINS):
            raise MarketplaceCatalogProtocolError('Staged Ozon page has unfinished enrichment')
        domain_times = {}
        for domain, value in observed.items():
            if domain not in DOMAINS or not isinstance(value, str):
                raise MarketplaceCatalogProtocolError('Invalid staged Ozon observed time')
            try:
                domain_times[domain] = datetime.fromisoformat(value)
            except ValueError:
                raise MarketplaceCatalogProtocolError('Invalid staged Ozon observed time') from None
        return page, maps, {'list': checkpoint.list_observed_at, 'domain': domain_times, 'items': item_times}

    @classmethod
    def advance_one_page(cls, *, run, adapter, credentials, credential_fingerprint, listing_service):
        """Return True only after an atomic page apply; False yields locally."""
        checkpoint = cls.get_or_create(run, credential_fingerprint=credential_fingerprint, now=datetime.utcnow())

        def bounded_read(method, payload):
            try:
                return method(credentials, payload)
            except OzonReadResponseTooLarge:
                # A complete 2xx body did not fit the local memory budget.
                # No listing has been applied; restart this page with fewer
                # IDs. A one-ID failure is terminal and cannot loop forever.
                cls._shrink(checkpoint, now=datetime.utcnow())
                raise CatalogCheckpointDeferred() from None

        while True:
            domain = checkpoint.domain
            if domain == 'apply':
                page, enrichment, times = cls._enrichment(checkpoint)
                listing_service._apply_catalog_page(
                    run=run, page=page, enrichment=enrichment,
                    now=datetime.utcnow(), checkpoint=checkpoint,
                    observed_at=times,
                )
                return True
            base = None if domain == 'list' else cls._base(checkpoint)
            if domain == 'list' and not cls._capacity_available(
                    now=datetime.utcnow(), own_account_id=run.account_id):
                return False
            ids = None if base is None else [item['product_id'] for item in base['items']]
            cursor = checkpoint.domain_cursor
            limit = checkpoint.page_limit
            visibility = checkpoint.visibility
            # Commit/close any ORM write work before every provider call.
            db.session.commit()
            if domain == 'list':
                response = bounded_read(adapter.list_products, {
                    'filter': {'offer_id': [], 'product_id': [], 'visibility': visibility},
                    'last_id': checkpoint.start_cursor, 'limit': limit,
                })
                observed_at = datetime.utcnow()
                page = listing_service.normalize_product_list_page(response)
                if not cls._save_list(checkpoint, run, page, observed_at=observed_at):
                    return False
            elif domain == 'info':
                response = bounded_read(adapter.get_products, {'product_id': ids})
                observed_at = datetime.utcnow()
                items = listing_service.normalize_product_info(response)
                if not cls._save_domain(checkpoint, domain, items, observed_at=observed_at):
                    return False
            else:
                payload = {'filter': {'product_id': ids, 'visibility': 'ALL'}, 'limit': listing_service.PAGE_SIZE}
                if domain == 'attributes':
                    payload['last_id'] = cursor
                    response = bounded_read(adapter.get_product_attributes, payload)
                    observed_at = datetime.utcnow()
                    result = listing_service.normalize_product_attributes_page(response)
                elif domain == 'prices':
                    payload['cursor'] = cursor
                    response = bounded_read(adapter.read_prices, payload)
                    observed_at = datetime.utcnow()
                    result = listing_service.normalize_prices_page(response)
                elif domain == 'stocks':
                    payload['cursor'] = cursor
                    response = bounded_read(adapter.read_stocks, payload)
                    observed_at = datetime.utcnow()
                    result = listing_service.normalize_stocks_page(response)
                else:
                    raise MarketplaceCatalogProtocolError('Unknown Ozon catalog checkpoint stage')
                if not cls._save_domain(checkpoint, domain, result['items'], observed_at=observed_at,
                                        total=result['total'], cursor=result['cursor']):
                    return False
