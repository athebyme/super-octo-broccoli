from flask import render_template, jsonify, request, redirect, url_for, flash
from flask_login import login_required, current_user
import requests
import logging
import threading
import time
from datetime import datetime, timedelta
from collections import defaultdict

logger = logging.getLogger(__name__)

STATISTICS_API_URL = "https://statistics-api.wildberries.ru"

# In-memory cache: {cache_key: {data, updated_at, loading}}
_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = 300  # 5 minutes


def _get_cache_key(seller_id, days):
    return f"findetail:{seller_id}:{days}"


def _get_cached(seller_id, days):
    key = _get_cache_key(seller_id, days)
    with _cache_lock:
        entry = _cache.get(key)
        if entry and entry.get('data'):
            age = time.time() - entry.get('updated_at', 0)
            return entry['data'], age < CACHE_TTL, entry.get('loading', False)
    return None, False, False


def _set_cached(seller_id, days, data):
    key = _get_cache_key(seller_id, days)
    with _cache_lock:
        _cache[key] = {
            'data': data,
            'updated_at': time.time(),
            'loading': False
        }


def _set_loading(seller_id, days, loading=True):
    key = _get_cache_key(seller_id, days)
    with _cache_lock:
        if key not in _cache:
            _cache[key] = {'data': None, 'updated_at': 0, 'loading': loading}
        else:
            _cache[key]['loading'] = loading


def _is_loading(seller_id, days):
    key = _get_cache_key(seller_id, days)
    with _cache_lock:
        entry = _cache.get(key)
        return entry.get('loading', False) if entry else False


def _compute_finance_analytics(records, days):
    """Compute per-product P&L, totals, weekly breakdown from reportDetailByPeriod."""

    # --- Per-product aggregation ---
    product_stats = defaultdict(lambda: {
        'sa_name': '',
        'subject_name': '',
        'brand_name': '',
        'sales_count': 0,
        'returns_count': 0,
        'retail_amount': 0.0,
        'ppvz_for_pay': 0.0,
        'commission': 0.0,
        'logistics': 0.0,
        'storage': 0.0,
        'penalties': 0.0,
        'deductions': 0.0,
        'additional_payment': 0.0,
        'acceptance': 0.0,
    })

    # --- Totals ---
    totals = {
        'total_sales': 0.0,
        'total_returns': 0.0,
        'total_retail_amount': 0.0,
        'total_commission': 0.0,
        'total_logistics': 0.0,
        'total_storage': 0.0,
        'total_penalties': 0.0,
        'total_deductions': 0.0,
        'total_additional_payment': 0.0,
        'total_acceptance': 0.0,
        'total_for_pay': 0.0,
        'sales_count': 0,
        'returns_count': 0,
    }

    # --- Weekly breakdown ---
    weekly_stats = defaultdict(lambda: {
        'date_from': '',
        'date_to': '',
        'retail_amount': 0.0,
        'returns_amount': 0.0,
        'commission': 0.0,
        'logistics': 0.0,
        'storage': 0.0,
        'penalties': 0.0,
        'for_pay': 0.0,
    })

    # --- Commission % breakdown ---
    commission_brackets = defaultdict(lambda: {'count': 0, 'amount': 0.0})

    for r in records:
        nm_id = r.get('nm_id', 0)
        oper = r.get('supplier_oper_name', '')
        ppvz = float(r.get('ppvz_for_pay', 0) or 0)
        delivery_rub = float(r.get('delivery_rub', 0) or 0)
        penalty = float(r.get('penalty', 0) or 0)
        storage_fee = float(r.get('storage_fee', 0) or 0)
        deduction = float(r.get('deduction', 0) or 0)
        additional_payment = float(r.get('additional_payment', 0) or 0)
        acceptance = float(r.get('acceptance', 0) or 0)
        retail_amount = float(r.get('retail_amount', 0) or 0)
        retail_price_disc = float(r.get('retail_price_withdisc_rub', 0) or 0)
        commission_percent = float(r.get('commission_percent', 0) or 0)
        return_amount = int(r.get('return_amount', 0) or 0)
        delivery_amount = int(r.get('delivery_amount', 0) or 0)

        # Compute commission amount per record
        # Commission = retail_price_withdisc_rub * commission_percent / 100
        commission_amount = retail_price_disc * commission_percent / 100.0 if commission_percent else 0.0

        # Product-level stats
        ps = product_stats[nm_id]
        ps['sa_name'] = r.get('sa_name', '') or ps['sa_name']
        ps['subject_name'] = r.get('subject_name', '') or ps['subject_name']
        ps['brand_name'] = r.get('brand_name', '') or ps['brand_name']
        ps['ppvz_for_pay'] += ppvz
        ps['logistics'] += delivery_rub
        ps['storage'] += storage_fee
        ps['penalties'] += penalty
        ps['deductions'] += deduction
        ps['additional_payment'] += additional_payment
        ps['acceptance'] += acceptance
        ps['commission'] += commission_amount

        if 'Продажа' in oper:
            ps['sales_count'] += 1
            ps['retail_amount'] += retail_price_disc
            totals['sales_count'] += 1
            totals['total_sales'] += retail_price_disc
        elif 'Возврат' in oper:
            ps['returns_count'] += 1
            totals['returns_count'] += 1
            totals['total_returns'] += retail_price_disc

        # Totals
        totals['total_commission'] += commission_amount
        totals['total_logistics'] += delivery_rub
        totals['total_storage'] += storage_fee
        totals['total_penalties'] += penalty
        totals['total_deductions'] += deduction
        totals['total_additional_payment'] += additional_payment
        totals['total_acceptance'] += acceptance
        totals['total_for_pay'] += ppvz
        totals['total_retail_amount'] += retail_amount

        # Weekly breakdown by realizationreport_id
        report_id = r.get('realizationreport_id', 0)
        ws = weekly_stats[report_id]
        ws['date_from'] = r.get('date_from', '')[:10] if not ws['date_from'] else min(ws['date_from'], (r.get('date_from', '') or '')[:10])
        ws['date_to'] = r.get('date_to', '')[:10] if not ws['date_to'] else max(ws['date_to'], (r.get('date_to', '') or '')[:10])

        if 'Продажа' in oper:
            ws['retail_amount'] += retail_price_disc
        elif 'Возврат' in oper:
            ws['returns_amount'] += retail_price_disc
        ws['commission'] += commission_amount
        ws['logistics'] += delivery_rub
        ws['storage'] += storage_fee
        ws['penalties'] += penalty
        ws['for_pay'] += ppvz

        # Commission % bracket
        if commission_percent > 0:
            bracket = f"{int(commission_percent)}%"
            commission_brackets[bracket]['count'] += 1
            commission_brackets[bracket]['amount'] += commission_amount

    # Build per-product list
    products = []
    for nm_id, ps in product_stats.items():
        margin = 0.0
        if ps['retail_amount'] > 0:
            margin = round(ps['ppvz_for_pay'] / ps['retail_amount'] * 100, 1)
        products.append({
            'nm_id': nm_id,
            'sa_name': ps['sa_name'],
            'subject_name': ps['subject_name'],
            'brand_name': ps['brand_name'],
            'sales_count': ps['sales_count'],
            'returns_count': ps['returns_count'],
            'retail_amount': round(ps['retail_amount'], 2),
            'commission': round(ps['commission'], 2),
            'logistics': round(ps['logistics'], 2),
            'storage': round(ps['storage'], 2),
            'penalties': round(ps['penalties'], 2),
            'deductions': round(ps['deductions'], 2),
            'ppvz_for_pay': round(ps['ppvz_for_pay'], 2),
            'margin': margin,
            'unprofitable': ps['ppvz_for_pay'] < 0,
        })

    # Sort by ppvz_for_pay ascending so unprofitable are first
    products.sort(key=lambda x: x['ppvz_for_pay'])

    # Unprofitable products
    unprofitable = [p for p in products if p['unprofitable']]

    # Weekly list sorted by date_from
    weeks = []
    for report_id, ws in sorted(weekly_stats.items(), key=lambda x: x[1].get('date_from', '')):
        weeks.append({
            'report_id': report_id,
            'date_from': ws['date_from'],
            'date_to': ws['date_to'],
            'retail_amount': round(ws['retail_amount'], 2),
            'returns_amount': round(ws['returns_amount'], 2),
            'commission': round(ws['commission'], 2),
            'logistics': round(ws['logistics'], 2),
            'storage': round(ws['storage'], 2),
            'penalties': round(ws['penalties'], 2),
            'for_pay': round(ws['for_pay'], 2),
        })

    # Commission brackets sorted
    comm_breakdown = []
    for bracket, info in sorted(commission_brackets.items(), key=lambda x: x[1]['amount'], reverse=True):
        comm_breakdown.append({
            'bracket': bracket,
            'count': info['count'],
            'amount': round(info['amount'], 2),
        })

    # Round totals
    for k in totals:
        if isinstance(totals[k], float):
            totals[k] = round(totals[k], 2)

    # Expense breakdown for chart
    expense_breakdown = {
        'commission': abs(totals['total_commission']),
        'logistics': abs(totals['total_logistics']),
        'storage': abs(totals['total_storage']),
        'penalties': abs(totals['total_penalties']) + abs(totals['total_deductions']),
    }
    expense_total = sum(expense_breakdown.values()) or 1
    expense_pct = {k: round(v / expense_total * 100, 1) for k, v in expense_breakdown.items()}

    return {
        'totals': totals,
        'products': products,
        'unprofitable': unprofitable,
        'unprofitable_count': len(unprofitable),
        'weeks': weeks,
        'commission_breakdown': comm_breakdown,
        'expense_breakdown': expense_breakdown,
        'expense_pct': expense_pct,
        'period': days,
    }


def _fetch_and_cache(api_key, seller_id, days):
    """Fetch data from WB API and update cache. Runs in background thread."""
    try:
        _set_loading(seller_id, days, True)
        date_from = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        date_to = datetime.now().strftime('%Y-%m-%d')

        session = requests.Session()
        session.headers.update({
            'Authorization': api_key,
            'Content-Type': 'application/json'
        })
        resp = session.get(
            f"{STATISTICS_API_URL}/api/v1/supplier/reportDetailByPeriod",
            params={'dateFrom': date_from, 'dateTo': date_to},
            timeout=120
        )
        resp.raise_for_status()
        records = resp.json()

        if isinstance(records, list):
            data = _compute_finance_analytics(records, days)
            _set_cached(seller_id, days, data)
            logger.info(f"Finance detail cache updated for seller {seller_id}, {days}d: {len(records)} records")
        else:
            _set_loading(seller_id, days, False)
    except Exception as e:
        logger.error(f"Background finance detail fetch error for seller {seller_id}: {e}")
        _set_loading(seller_id, days, False)


def register_finance_detail_routes(app):
    """Register detailed financial report routes."""

    @app.route('/finance-detail')
    @login_required
    def finance_detail_page():
        """Detailed financial report page."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для детального финотчёта необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('api_settings'))
        return render_template('finance_detail.html')

    @app.route('/api/finance-detail/data')
    @login_required
    def api_finance_detail_data():
        """Get detailed financial report data. Returns cached data instantly if available,
        triggers background refresh if stale."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403

        try:
            days = min(int(request.args.get('days', 30)), 90)
            seller_id = current_user.seller.id
            force = request.args.get('force', '').lower() == 'true'

            cached_data, is_fresh, is_loading = _get_cached(seller_id, days)

            if cached_data and not force:
                result = dict(cached_data)
                result['_cached'] = True
                result['_stale'] = not is_fresh
                result['_loading'] = is_loading

                if not is_fresh and not is_loading:
                    t = threading.Thread(
                        target=_fetch_and_cache,
                        args=(current_user.seller.wb_api_key, seller_id, days),
                        daemon=True
                    )
                    t.start()

                return jsonify(result)

            if is_loading:
                return jsonify({'_loading': True, '_cached': False}), 202

            if not force:
                _set_loading(seller_id, days, True)
                t = threading.Thread(
                    target=_fetch_and_cache,
                    args=(current_user.seller.wb_api_key, seller_id, days),
                    daemon=True
                )
                t.start()
                return jsonify({'_loading': True, '_cached': False}), 202

            # Force refresh - synchronous
            date_from = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            date_to = datetime.now().strftime('%Y-%m-%d')
            session = requests.Session()
            session.headers.update({
                'Authorization': current_user.seller.wb_api_key,
                'Content-Type': 'application/json'
            })
            resp = session.get(
                f"{STATISTICS_API_URL}/api/v1/supplier/reportDetailByPeriod",
                params={'dateFrom': date_from, 'dateTo': date_to},
                timeout=120
            )
            resp.raise_for_status()
            records = resp.json()

            if not isinstance(records, list):
                return jsonify({'error': 'Unexpected API response'}), 500

            data = _compute_finance_analytics(records, days)
            _set_cached(seller_id, days, data)
            return jsonify(data)

        except requests.exceptions.RequestException as e:
            logger.error(f"WB API error in finance detail: {e}")
            return jsonify({'error': f'Ошибка WB API: {str(e)}'}), 502
        except Exception as e:
            logger.error(f"Error in finance detail analytics: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/finance-detail/status')
    @login_required
    def api_finance_detail_status():
        """Check if background data fetch is complete."""
        if not current_user.seller:
            return jsonify({'error': 'No seller'}), 403
        days = min(int(request.args.get('days', 30)), 90)
        seller_id = current_user.seller.id
        cached_data, is_fresh, is_loading = _get_cached(seller_id, days)
        if cached_data:
            result = dict(cached_data)
            result['_cached'] = True
            result['_stale'] = not is_fresh
            result['_loading'] = is_loading
            return jsonify(result)
        return jsonify({'_loading': is_loading, '_cached': False}), 202 if is_loading else 200
