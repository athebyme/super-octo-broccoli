"""A bounded, coherent local read of one observed Ozon analytics period."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from time import monotonic
import unicodedata
from urllib.parse import urlsplit

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import OperationalError

from models import db, MarketplaceAnalyticsSync as Sync, MarketplaceMetricFact as Fact, MarketplaceListing as Listing
from services.marketplace_accounts import MarketplaceAccountService
from services.marketplace_analytics import (
    MarketplaceAnalyticsService as Analytics, MarketplaceAnalyticsValidationError,
    MarketplaceAnalyticsNotFound, MarketplaceAnalyticsError,
)
from services.ozon_analytics_contracts import REQUEST_METRIC_DEFINITIONS, request_fingerprint

DEFINITIONS = {d.metric_code:d for d in REQUEST_METRIC_DEFINITIONS}
REVENUE = 'ordered_revenue_rub'
UNITS = 'ordered_units'
ENDPOINT = '/v1/analytics/data'
READ_SECONDS = 5


class WorkspaceBusy(MarketplaceAnalyticsError):
    status_code = 503
    code = 'analytics_read_timeout'


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite() or number < 0 or number > Decimal('9999999999999999.9999'):
        return None
    return number if number.as_tuple().exponent >= -4 else None


def _value(value):
    number = _decimal(value)
    return format(number, 'f') if number is not None else None


def _object(raw):
    try:
        result = json.loads(raw or '{}')
    except (ValueError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def _fold(value):
    return unicodedata.normalize('NFKC', str(value or '')).casefold()


def _image(raw):
    if not isinstance(raw, str) or len(raw) > 2 * 1024 * 1024:
        return None
    media = _object(raw)
    candidate = media.get('primary_image')
    if not candidate:
        images = media.get('images')
        candidate = next((p for p in images if isinstance(p, str)), None) if isinstance(images, list) else None
    try:
        url = urlsplit(candidate) if isinstance(candidate, str) and len(candidate) <= 2000 else None
        return candidate if url and url.scheme in {'http', 'https'} and url.hostname and not url.username and not url.password else None
    except ValueError:
        return None


def _metadata_valid(code, row, *, fact=False):
    definition = DEFINITIONS[code]
    valid = (row.get('unit') == definition.unit
             and row.get('definition_code') == definition.definition_code
             and row.get('cross_marketplace_comparable') in (False, 0))
    return valid and (not fact or (row.get('provider_metric') == definition.provider_metric
                                  and row.get('source_endpoint') == ENDPOINT))


def _valid_fact(code):
    definition = DEFINITIONS[code]
    return and_(Fact.metric_code == code, Fact.unit == definition.unit,
                Fact.definition_code == definition.definition_code,
                Fact.provider_metric == definition.provider_metric,
                Fact.source_endpoint == ENDPOINT, Fact.cross_marketplace_comparable.is_(False),
                Fact.metric_value >= 0, Fact.metric_value < Decimal('10000000000000000'))


def _as_of(value, *, snapshot_id, today):
    if value is None:
        return today
    try:
        observed = date.fromisoformat(value)
    except (ValueError, TypeError):
        raise MarketplaceAnalyticsValidationError('Некорректная дата периода') from None
    if snapshot_id is None or observed.isoformat() != value or observed > today:
        raise MarketplaceAnalyticsValidationError('Дата должна относиться к закреплённому снимку и не быть в будущем')
    return observed


def _utc_naive(value):
    """Normalize instants to the naive-UTC convention used by persisted rows."""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def get_workspace(*, seller_id, account_id, period_code='30d', snapshot_id=None, as_of=None,
                  search='', sort_by=REVENUE, sort_dir='desc', page=1, per_page=25,
                  now=None, today=None):
    """No provider, ORM writes, implicit refresh or last-good substitution of a pin."""
    Analytics._positive_integer(seller_id, 'seller_id')
    Analytics._positive_integer(account_id, 'account_id')
    Analytics._positive_integer(page, 'page', maximum=100000)
    Analytics._positive_integer(per_page, 'per_page', maximum=100)
    if snapshot_id is not None:
        Analytics._positive_integer(snapshot_id, 'snapshot_id')
    if sort_by not in DEFINITIONS or sort_dir not in ('asc', 'desc'):
        raise MarketplaceAnalyticsValidationError('Неизвестная сортировка товаров')
    if not isinstance(search, str) or len(search) > 200:
        raise MarketplaceAnalyticsValidationError('Поиск должен содержать не более 200 символов')
    # Sync uses a single UTC instant for both its freshness check and period
    # date. Keep the read workspace on that same anchor: `date.today()` is the
    # host's local date and can already be tomorrow while UTC is still today.
    effective_now = _utc_naive(now or datetime.utcnow())
    effective_today = today if today is not None else effective_now.date()
    if not isinstance(effective_today, date) or isinstance(effective_today, datetime):
        raise MarketplaceAnalyticsValidationError('Дата периода должна быть календарной датой')
    anchor = _as_of(as_of, snapshot_id=snapshot_id, today=effective_today)
    period_code, requested_start, requested_end = Analytics._period(period_code, today=anchor)
    account = MarketplaceAccountService.get_owned_account(
        seller_id=seller_id, account_id=account_id, marketplace_code='ozon')
    scope = {'account_id':account.id, 'marketplace_code':'ozon', 'account_label':account.label,
             'cross_marketplace_comparable':False, 'comparison_scope':'marketplace_account_only'}
    selectors = {'period':period_code, 'search':search.strip(), 'sort_by':sort_by, 'sort_dir':sort_dir}
    base = {'scope':scope, 'filters':selectors, 'as_of':anchor.isoformat(),
            'requested_period':{'start':requested_start.isoformat(), 'end':requested_end.isoformat()},
            'definitions':[d.to_public_dict() for d in DEFINITIONS.values()], 'source_endpoint':ENDPOINT}
    deadline = monotonic() + READ_SECONDS
    with db.engine.connect() as connection:
        raw = connection.connection.driver_connection
        sqlite = connection.dialect.name == 'sqlite'
        try:
            if sqlite:
                connection.exec_driver_sql('BEGIN')
                raw.set_progress_handler(lambda: int(monotonic() > deadline), 10000)
                raw.create_function('sh_analytics_casefold', 1, _fold, deterministic=True)
            query = select(Sync.__table__).where(
                Sync.seller_id == seller_id, Sync.account_id == account.id,
                Sync.marketplace_id == account.marketplace_id, Sync.period_code == period_code,
                Sync.status == 'completed', Sync.contract_version == Analytics.CONTRACT_VERSION)
            if snapshot_id is not None:
                query = query.where(Sync.id == snapshot_id)
            snapshot = connection.execute(query.order_by(Sync.completed_at.desc(), Sync.id.desc()).limit(1)).mappings().first()
            if snapshot is None:
                if snapshot_id is not None:
                    raise MarketplaceAnalyticsNotFound('Этот снимок аналитики недоступен. Откройте актуальные данные.')
                return {**base, 'status':'no_data', 'snapshot':None, 'totals':{REVENUE:None, UNITS:None, 'average_unit_rub':None},
                        'daily':[], 'products':[], 'pagination':{'page':page, 'per_page':per_page, 'total':0, 'pages':0}}
            start, end = snapshot['period_start'], snapshot['period_end']
            if end > anchor or (end - start).days + 1 != Analytics.SUPPORTED_PERIODS[period_code] or snapshot['request_fingerprint'] != request_fingerprint(period_start=start, period_end=end):
                raise MarketplaceAnalyticsValidationError('Сохранённый период аналитики не подтверждён. Обновите данные.')
            fact_scope = (Fact.sync_id == snapshot['id'], Fact.seller_id == seller_id,
                          Fact.account_id == account.id, Fact.marketplace_id == account.marketplace_id)
            daily_rows = connection.execute(select(
                Fact.fact_date, Fact.dimension_id, Fact.metric_code, Fact.metric_value,
                Fact.unit, Fact.definition_code, Fact.provider_metric, Fact.source_endpoint,
                Fact.cross_marketplace_comparable,
            ).where(*fact_scope, Fact.dimension_kind == 'day', Fact.metric_code.in_(DEFINITIONS))
              .order_by(Fact.fact_date, Fact.metric_code).limit(63)).mappings().all()
            # Each dimension has at most one fact for each metric. A conflicting FK
            # across metrics must never choose an arbitrary linked product.
            coherent_link = case((and_(func.count(Fact.listing_id) == func.count(),
                                      func.min(Fact.listing_id) == func.max(Fact.listing_id)),
                                  func.min(Fact.listing_id)), else_=None)
            grouped = select(
                Fact.dimension_id.label('sku'), func.max(Fact.dimension_name).label('observed_name'),
                coherent_link.label('listing_id'),
                *[func.max(case((_valid_fact(code), Fact.metric_value), else_=None)).label(code)
                  for code in DEFINITIONS],
            ).where(*fact_scope, Fact.dimension_kind == 'listing', Fact.metric_code.in_(DEFINITIONS))\
             .group_by(Fact.dimension_id).subquery()
            title = func.coalesce(Listing.title, grouped.c.observed_name)
            table = grouped.outerjoin(Listing, and_(Listing.id == grouped.c.listing_id,
                Listing.seller_id == seller_id, Listing.account_id == account.id,
                Listing.marketplace_id == account.marketplace_id))
            filters = []
            if search.strip():
                fold = func.sh_analytics_casefold if sqlite else func.lower
                needle = '%' + _fold(search.strip()).replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%'
                filters.append(or_(*(fold(expr).like(needle, escape='\\') for expr in (title, Listing.offer_id, grouped.c.sku))))
            total = connection.execute(select(func.count()).select_from(table).where(*filters)).scalar_one()
            metric = grouped.c[sort_by]
            order = metric.desc() if sort_dir == 'desc' else metric.asc()
            rows = connection.execute(select(grouped, Listing.id.label('owned_listing_id'),
                Listing.title.label('current_title'), Listing.offer_id, Listing.media_json)
                .select_from(table).where(*filters).order_by(metric.is_(None), order, grouped.c.sku.asc())
                .offset((page - 1) * per_page).limit(per_page)).mappings().all()
        except OperationalError:
            if monotonic() > deadline:
                raise WorkspaceBusy('Чтение аналитики заняло слишком много времени. Повторите загрузку.') from None
            raise
        finally:
            if sqlite:
                raw.set_progress_handler(None, 0)
            connection.rollback()
    if monotonic() > deadline:
        raise WorkspaceBusy('Чтение аналитики заняло слишком много времени. Повторите загрузку.')
    totals = {}
    stored = _object(snapshot['totals_json'])
    for code in DEFINITIONS:
        value = stored.get(code)
        totals[code] = _value(value.get('value')) if isinstance(value, dict) and _metadata_valid(code, value) else None
    revenue, units = _decimal(totals[REVENUE]), _decimal(totals[UNITS])
    totals['average_unit_rub'] = format((revenue / units).quantize(Decimal('.01'), rounding=ROUND_HALF_UP), 'f') if revenue is not None and units is not None and units > 0 else None
    days = {(start + timedelta(days=i)).isoformat():{'date':(start+timedelta(days=i)).isoformat(),REVENUE:None,UNITS:None}
            for i in range((end-start).days+1)}
    if len(daily_rows) > 62:
        raise MarketplaceAnalyticsValidationError('Сохранённая динамика выходит за период. Обновите аналитику.')
    seen = set()
    for row in daily_rows:
        key = row['fact_date'].isoformat() if row['fact_date'] is not None else None
        code = row['metric_code']
        if key not in days or row['dimension_id'] != key or (key,code) in seen:
            raise MarketplaceAnalyticsValidationError('Сохранённые даты аналитики не согласованы. Обновите данные.')
        seen.add((key,code))
        if _metadata_valid(code, row, fact=True):
            days[key][code] = _value(row['metric_value'])
    products = []
    for row in rows:
        listing_id = row['owned_listing_id']
        products.append({'sku':row['sku'], 'title':row['current_title'] or row['observed_name'],
            'offer_id':row['offer_id'], 'listing_id':listing_id,
            'url':f'/marketplaces/listings/view/{listing_id}?account_id={account.id}' if listing_id else None,
            'image':_image(row['media_json']) if listing_id else None,
            'matched':listing_id is not None, 'metrics':{code:_value(row[code]) for code in DEFINITIONS}})
    completed = snapshot['completed_at']
    exact = start == requested_start and end == requested_end
    stale = (
        not exact
        or not completed
        or _utc_naive(completed) < effective_now - Analytics.CACHE_TTL
    )
    return {**base, 'status':'stale' if stale else 'ready',
            'snapshot':{'id':snapshot['id'], 'period_start':start.isoformat(), 'period_end':end.isoformat(),
                        'completed_at':completed.isoformat() if completed else None, 'period_matches_request':exact},
            'totals':totals, 'daily':list(days.values()), 'products':products,
            'pagination':{'page':page, 'per_page':per_page, 'total':total, 'pages':(total+per_page-1)//per_page}}
