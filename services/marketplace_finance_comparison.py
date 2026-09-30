"""Compare two complete local observations, never infer accounting events."""
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
import time

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from models import (db, Marketplace, SellerMarketplaceAccount, MarketplaceFinanceSync,
                    MarketplaceFinanceFact, MarketplaceFinanceFactItem, MarketplaceFinanceComponent)
from services.marketplace_finance import (MarketplaceFinanceService as Finance,
    MarketplaceFinanceValidationError, MarketplaceFinanceNotFound, MarketplaceFinanceError)

MAX_FACTS = 10_000
MAX_CHILDREN = 100_000
MAX_TEXT_BYTES = 16 * 1024 * 1024
READ_SECONDS = 5
HISTORY_LIMIT = 16
CHANGE_KINDS = ('added', 'missing', 'changed')


class ComparisonLimit(MarketplaceFinanceError):
    status_code = 422
    code = 'finance_comparison_limit'


def _bounded(deadline):
    if time.monotonic() > deadline:
        raise ComparisonLimit('Сравнение заняло слишком много времени. Выберите загрузки за 7 дней или повторите позже.')


@contextmanager
def _read(engine):
    with engine.connect() as connection:
        if connection.dialect.name != 'sqlite':
            raise ComparisonLimit('Эта конфигурация базы пока не поддерживает сравнение.')
        raw = connection.connection.driver_connection
        timeout = raw.execute('PRAGMA busy_timeout').fetchone()[0]
        deadline = time.monotonic() + READ_SECONDS
        try:
            raw.execute('PRAGMA busy_timeout=200')
            raw.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            connection.exec_driver_sql('BEGIN')
            yield connection, deadline
            _bounded(deadline)
        except SQLAlchemyError:
            raise ComparisonLimit('Не удалось прочитать обе загрузки целиком. Повторите сравнение позже.') from None
        finally:
            raw.set_progress_handler(None, 0)
            connection.rollback()
            raw.execute(f'PRAGMA busy_timeout={int(timeout)}')


def _account(connection, seller_id, account_id):
    if any(type(v) is not int or v <= 0 for v in (seller_id, account_id)):
        raise MarketplaceFinanceNotFound('Магазин Ozon не найден.')
    a = SellerMarketplaceAccount
    row = connection.execute(select(a.id, a.seller_id, a.marketplace_id, a.label)
        .join(Marketplace, a.marketplace_id == Marketplace.id)
        .where(a.id == account_id, a.seller_id == seller_id, Marketplace.code == 'ozon')).mappings().first()
    if row is None:
        raise MarketplaceFinanceNotFound('Магазин Ozon не найден.')
    return dict(row)


def _scope(model, account):
    result = [model.seller_id == account['seller_id'], model.account_id == account['id']]
    if hasattr(model, 'marketplace_id'):
        result.append(model.marketplace_id == account['marketplace_id'])
    return result


def _snapshots(account):
    s = MarketplaceFinanceSync
    return select(s.id, s.period_code, s.period_start, s.period_end, s.completed_at,
        s.fact_count, s.contract_version, s.request_fingerprint).where(*_scope(s, account),
        s.status == 'completed', s.contract_version == Finance.CONTRACT_VERSION)


def _snapshot(connection, account, identity):
    if type(identity) is not int or identity <= 0:
        raise MarketplaceFinanceValidationError('Выберите две сохранённые загрузки.')
    row = connection.execute(_snapshots(account).where(MarketplaceFinanceSync.id == identity)).mappings().first()
    if row is None:
        raise MarketplaceFinanceNotFound('Выбранная загрузка недоступна. Откройте историю и выберите сохранённую версию.')
    if (row['period_code'] not in ('7d', '30d') or not row['completed_at']
            or row['completed_at'] > datetime.utcnow()
            or (row['period_end'] - row['period_start']).days != (6 if row['period_code'] == '7d' else 29)
            or row['request_fingerprint'] != Finance._run_fingerprint(row['period_start'], row['period_end'])):
        raise MarketplaceFinanceValidationError('Формат сохранённой загрузки не подтверждён. Откройте финансы и обновите данные.')
    return dict(row)


def _public_snapshot(row):
    return {'id': row['id'], 'period': row['period_code'], 'start': row['period_start'].isoformat(),
        'end': row['period_end'].isoformat(), 'completed_at': row['completed_at'].isoformat() + 'Z' if row['completed_at'] else None,
        'fact_count': row['fact_count']}


def history(*, seller_id, account_id, anchor_id=None, engine=None):
    with _read(engine or db.engine) as (connection, deadline):
        account = _account(connection, seller_id, account_id)
        anchor = _snapshot(connection, account, anchor_id) if anchor_id is not None else None
        s = MarketplaceFinanceSync
        rows = connection.execute(_snapshots(account).order_by(s.completed_at.desc(), s.id.desc())
            .limit(HISTORY_LIMIT + 1)).mappings().all()
        _bounded(deadline)
        return {'scope': {'marketplace': 'ozon', 'account_id': account_id},
            'items': [_public_snapshot(r) for r in rows[:HISTORY_LIMIT]],
            'truncated': len(rows) > HISTORY_LIMIT, 'anchor': _public_snapshot(anchor) if anchor else None,
            'observation_only': True}


def _money(value):
    if not isinstance(value, Decimal) or not value.is_finite():
        raise MarketplaceFinanceValidationError('Сумма сохранённого начисления не подтверждена.')
    return value


def _facts(connection, account, snapshot, budget):
    f = MarketplaceFinanceFact
    rows = connection.execute(select(f.id, f.accrual_id, f.fact_date, f.unit_number,
        f.accrued_category, f.total_amount, f.currency, f.definition_code, f.source_endpoint, f.contract_version)
        .where(*_scope(f, account), f.sync_id == snapshot['id']).order_by(f.id).limit(MAX_FACTS + 1)).mappings().all()
    if len(rows) > MAX_FACTS:
        raise ComparisonLimit('Слишком много начислений для сравнения целиком. Выберите загрузки за 7 дней.')
    if len(rows) != snapshot['fact_count']:
        raise MarketplaceFinanceValidationError('Не удалось подтвердить полноту сохранённой загрузки. Обновите данные в финансовом разделе.')
    result = {}
    by_id = {}
    for raw in rows:
        _bounded(budget['deadline'])
        row = dict(raw)
        if (row['contract_version'] != Finance.CONTRACT_VERSION or row['definition_code'] != Finance.DEFINITION_CODE
                or row['source_endpoint'] != '/v1/finance/accrual/by-day'
                or not snapshot['period_start'] <= row['fact_date'] <= snapshot['period_end']
                or row['accrual_id'] in result or not row['accrual_id']
                or len(row['currency']) != 3 or not row['currency'].isascii() or not row['currency'].isupper() or not row['currency'].isalpha()):
            raise MarketplaceFinanceValidationError('Контракт сохранённых начислений не подтверждён. Обновите данные в финансовом разделе.')
        _money(row['total_amount'])
        row['items'] = []
        row['components'] = []
        result[row['accrual_id']] = row
        by_id[row['id']] = row
        _text_budget(row.values(), budget)
    for model, fields, name in [
        (MarketplaceFinanceFactItem, ('external_sku',), 'items'),
        (MarketplaceFinanceComponent, ('component_kind', 'external_type_id', 'external_sku', 'amount', 'currency'), 'components'),
    ]:
        children = connection.execute(select(model.fact_id, *(getattr(model, field) for field in fields))
            .join(f, model.fact_id == f.id).where(*_scope(f, account), f.sync_id == snapshot['id'], *_scope(model, account))
            .order_by(model.id).limit(MAX_CHILDREN - budget['children'] + 1)).all()
        budget['children'] += len(children)
        if budget['children'] > MAX_CHILDREN:
            raise ComparisonLimit('Слишком много строк расшифровки. Выберите загрузки за 7 дней.')
        for child in children:
            _bounded(budget['deadline'])
            values = tuple(child[1:])
            _text_budget(values, budget)
            by_id[child[0]][name].append(values)
    return result


def _text_budget(values, budget):
    budget['text'] += sum(len(v.encode('utf-8')) for v in values if isinstance(v, str))
    if budget['text'] > MAX_TEXT_BYTES:
        raise ComparisonLimit('Описание начислений превышает размер сравнения. Выберите загрузки за 7 дней.')


def _changes(before, after):
    fields = ('total_amount', 'currency', 'fact_date', 'unit_number', 'accrued_category')
    changed = [field for field in fields if before[field] != after[field]]
    for field in ('items', 'components'):
        # Counts preserve multiplicity; provider row order and current display labels do not matter.
        if Counter(before[field]) != Counter(after[field]):
            changed.append(field)
    return changed


def _public_fact(row):
    if row is None:
        return None
    return {'id': row['id'], 'date': row['fact_date'].isoformat(), 'number': row['unit_number'],
        'category': row['accrued_category'], 'amount': str(row['total_amount']), 'currency': row['currency'],
        'items_count': len(row['items']), 'components_count': len(row['components'])}


def compare(*, seller_id, account_id, older_id, newer_id, kind='', page=1, per_page=50, engine=None):
    if kind not in ('', *CHANGE_KINDS) or type(page) is not int or not 1 <= page <= 100_000 or type(per_page) is not int or not 1 <= per_page <= 100:
        raise MarketplaceFinanceValidationError('Проверьте фильтр и страницу сравнения.')
    with _read(engine or db.engine) as (connection, deadline):
        account = _account(connection, seller_id, account_id)
        older = _snapshot(connection, account, older_id)
        newer = _snapshot(connection, account, newer_id)
        if (older['completed_at'], older['id']) >= (newer['completed_at'], newer['id']):
            raise MarketplaceFinanceValidationError('В поле «Раньше» выберите более раннюю загрузку.')
        start, end = max(older['period_start'], newer['period_start']), min(older['period_end'], newer['period_end'])
        if start > end:
            raise MarketplaceFinanceValidationError('У загрузок нет общих дат. Выберите пересекающиеся периоды.')
        budget = {'children': 0, 'text': 0, 'deadline': deadline}
        before_all = _facts(connection, account, older, budget)
        after_all = _facts(connection, account, newer, budget)
        # A moved fact can leave the common period. It is absent from that observation's
        # comparison window, never described as a cancellation or provider deletion.
        before = {key: row for key, row in before_all.items() if start <= row['fact_date'] <= end}
        after = {key: row for key, row in after_all.items() if start <= row['fact_date'] <= end}
        totals = defaultdict(lambda: {'older': Decimal(0), 'newer': Decimal(0), 'older_count': 0, 'newer_count': 0})
        for name, data in [('older', before), ('newer', after)]:
            for row in data.values():
                _bounded(deadline)
                totals[row['currency']][name] += row['total_amount']
                totals[row['currency']][name + '_count'] += 1
        counts = {name: 0 for name in (*CHANGE_KINDS, 'unchanged')}
        changes = []
        for identity in sorted(set(before) | set(after)):
            _bounded(deadline)
            left, right = before.get(identity), after.get(identity)
            fields = _changes(left, right) if left and right else []
            state = 'added' if left is None else 'missing' if right is None else 'changed' if fields else 'unchanged'
            counts[state] += 1
            if state != 'unchanged' and (not kind or state == kind):
                changes.append({'accrual_id': identity, 'kind': state, 'fields': fields,
                    'older': _public_fact(left), 'newer': _public_fact(right)})
        count = len(changes)
        return {'scope': {'marketplace': 'ozon', 'account_id': account_id}, 'observation_only': True,
            'accounting_reconciliation': False, 'older': _public_snapshot(older), 'newer': _public_snapshot(newer),
            'period': {'start': start.isoformat(), 'end': end.isoformat(), 'common_dates_only': True},
            'totals': [{'currency': currency, 'older': str(values['older']), 'newer': str(values['newer']),
                'delta': str(values['newer'] - values['older']), 'older_count': values['older_count'], 'newer_count': values['newer_count']}
                for currency, values in sorted(totals.items())],
            'counts': counts, 'filter': kind, 'items': changes[(page - 1) * per_page:page * per_page],
            'pagination': {'page': page, 'per_page': per_page, 'total': count, 'pages': (count + per_page - 1) // per_page}}
