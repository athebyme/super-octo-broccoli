from flask import render_template, jsonify, request, redirect, url_for, flash
from flask_login import login_required, current_user
import logging
from datetime import datetime, timedelta
from collections import defaultdict

from models import db, WBRealizationRow
from utils.safe_error import safe_error_message

logger = logging.getLogger(__name__)


def _compute_finance_from_db(seller_id, days):
    """Compute per-product P&L, totals, weekly breakdown from local DB."""
    date_from = datetime.now() - timedelta(days=days)

    records = WBRealizationRow.query.filter(
        WBRealizationRow.seller_id == seller_id,
        WBRealizationRow.rr_dt >= date_from
    ).all()

    # --- Per-product aggregation ---
    product_stats = defaultdict(lambda: {
        'sa_name': '', 'subject_name': '', 'brand_name': '',
        'sales_count': 0, 'returns_count': 0,
        'retail_amount': 0.0, 'ppvz_for_pay': 0.0, 'commission': 0.0,
        'logistics': 0.0, 'storage': 0.0, 'penalties': 0.0,
        'deductions': 0.0, 'additional_payment': 0.0, 'acceptance': 0.0,
    })

    totals = {
        'total_sales': 0.0, 'total_returns': 0.0, 'total_retail_amount': 0.0,
        'total_commission': 0.0, 'total_logistics': 0.0, 'total_storage': 0.0,
        'total_penalties': 0.0, 'total_deductions': 0.0, 'total_additional_payment': 0.0,
        'total_acceptance': 0.0, 'total_for_pay': 0.0,
        'sales_count': 0, 'returns_count': 0,
    }

    weekly_stats = defaultdict(lambda: {
        'date_from': '', 'date_to': '',
        'retail_amount': 0.0, 'returns_amount': 0.0,
        'commission': 0.0, 'logistics': 0.0, 'storage': 0.0,
        'penalties': 0.0, 'for_pay': 0.0,
    })

    commission_brackets = defaultdict(lambda: {'count': 0, 'amount': 0.0})

    for r in records:
        nm_id = r.nm_id or 0
        oper = r.supplier_oper_name or ''
        ppvz = r.ppvz_for_pay or 0
        delivery_rub = r.delivery_rub or 0
        penalty = r.penalty or 0
        storage_fee = r.storage_fee or 0
        deduction = r.deduction or 0
        additional_payment = r.additional_payment or 0
        acceptance = r.acceptance or 0
        retail_amount = r.retail_amount or 0
        retail_price_disc = r.retail_price_withdisc_rub or 0
        commission_percent = r.commission_percent or 0

        commission_amount = retail_price_disc * commission_percent / 100.0 if commission_percent else 0.0

        ps = product_stats[nm_id]
        ps['sa_name'] = r.sa_name or ps['sa_name']
        ps['subject_name'] = r.subject_name or ps['subject_name']
        ps['brand_name'] = r.brand_name or ps['brand_name']
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

        totals['total_commission'] += commission_amount
        totals['total_logistics'] += delivery_rub
        totals['total_storage'] += storage_fee
        totals['total_penalties'] += penalty
        totals['total_deductions'] += deduction
        totals['total_additional_payment'] += additional_payment
        totals['total_acceptance'] += acceptance
        totals['total_for_pay'] += ppvz
        totals['total_retail_amount'] += retail_amount

        # Weekly by realizationreport_id
        report_id = r.realizationreport_id or 0
        ws = weekly_stats[report_id]
        df_str = r.date_from.isoformat() if r.date_from else ''
        dt_str = r.date_to.isoformat() if r.date_to else ''
        ws['date_from'] = df_str if not ws['date_from'] else min(ws['date_from'], df_str)
        ws['date_to'] = dt_str if not ws['date_to'] else max(ws['date_to'], dt_str)

        if 'Продажа' in oper:
            ws['retail_amount'] += retail_price_disc
        elif 'Возврат' in oper:
            ws['returns_amount'] += retail_price_disc
        ws['commission'] += commission_amount
        ws['logistics'] += delivery_rub
        ws['storage'] += storage_fee
        ws['penalties'] += penalty
        ws['for_pay'] += ppvz

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

    products.sort(key=lambda x: x['ppvz_for_pay'])
    unprofitable = [p for p in products if p['unprofitable']]

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

    comm_breakdown = []
    for bracket, info in sorted(commission_brackets.items(), key=lambda x: x[1]['amount'], reverse=True):
        comm_breakdown.append({
            'bracket': bracket,
            'count': info['count'],
            'amount': round(info['amount'], 2),
        })

    for k in totals:
        if isinstance(totals[k], float):
            totals[k] = round(totals[k], 2)

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


def register_finance_detail_routes(app):
    """Register detailed financial report routes."""

    @app.route('/finance-detail')
    @login_required
    def finance_detail_page():
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для детального финотчёта необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('api_settings'))
        return render_template('finance_detail.html')

    @app.route('/api/finance-detail/data')
    @login_required
    def api_finance_detail_data():
        """Get detailed financial report from local DB. Supports any period."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403

        try:
            days = min(int(request.args.get('days', 30)), 365)
            seller_id = current_user.seller.id
            data = _compute_finance_from_db(seller_id, days)
            return jsonify(data)

        except Exception as e:
            logger.error(f"Error in finance detail analytics: {e}")
            return jsonify({'error': safe_error_message(e)}), 500

    @app.route('/api/finance-detail/status')
    @login_required
    def api_finance_detail_status():
        """Backward compat — data always ready from DB."""
        if not current_user.seller:
            return jsonify({'error': 'No seller'}), 403
        days = min(int(request.args.get('days', 30)), 365)
        data = _compute_finance_from_db(current_user.seller.id, days)
        data['_cached'] = True
        data['_stale'] = False
        data['_loading'] = False
        return jsonify(data)
