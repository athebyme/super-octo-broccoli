from flask import jsonify, request, redirect, url_for
from flask_login import login_required, current_user
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from sqlalchemy import func, case, and_

from models import db, WBSale, WBOrder

logger = logging.getLogger(__name__)


def _compute_returns_from_db(seller_id, days):
    """Compute returns analytics from local DB.

    Combines two data sources:
    - wb_sales: rows with is_return=True (sale_id starts with 'R' or finishedPrice < 0)
    - wb_orders: rows with is_cancel=True (cancelled/returned orders)

    wb_orders cancellations appear faster in the API than wb_sales returns,
    so combining both gives a more complete and timely picture.
    Deduplication by nm_id+date prevents double-counting when the same
    return appears in both tables.
    """
    date_from = datetime.now() - timedelta(days=days)

    # --- Source 1: wb_sales ---
    sales_rows = WBSale.query.filter(
        WBSale.seller_id == seller_id,
        WBSale.date >= date_from
    ).all()

    sale_returns = [s for s in sales_rows if s.is_return]
    sale_ok = [s for s in sales_rows if not s.is_return]

    # --- Source 2: wb_orders (cancelled) ---
    cancelled_orders = WBOrder.query.filter(
        WBOrder.seller_id == seller_id,
        WBOrder.date >= date_from,
        WBOrder.is_cancel == True  # noqa: E712
    ).all()

    # Build a set of (nm_id, date_str) from sale returns to avoid double-counting
    sale_return_keys = set()
    for s in sale_returns:
        if s.nm_id and s.date:
            sale_return_keys.add((s.nm_id, s.date.strftime('%Y-%m-%d')))

    # Only count cancelled orders that are NOT already captured in wb_sales returns
    extra_cancelled = []
    for o in cancelled_orders:
        cancel_date = o.cancel_dt or o.date
        if not cancel_date:
            continue
        key = (o.nm_id, cancel_date.strftime('%Y-%m-%d'))
        if key not in sale_return_keys:
            extra_cancelled.append(o)

    # --- Unified return items for aggregation ---
    class _ReturnItem:
        __slots__ = ('nm_id', 'date', 'finished_price', 'subject', 'brand',
                     'supplier_article', 'warehouse_name', 'region_name')

        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    all_returns = []
    for s in sale_returns:
        all_returns.append(_ReturnItem(
            nm_id=s.nm_id, date=s.date, finished_price=abs(s.finished_price or 0),
            subject=s.subject, brand=s.brand, supplier_article=s.supplier_article,
            warehouse_name=s.warehouse_name, region_name=s.region_name,
        ))
    for o in extra_cancelled:
        all_returns.append(_ReturnItem(
            nm_id=o.nm_id, date=o.cancel_dt or o.date,
            finished_price=abs(o.finished_price or 0),
            subject=o.subject, brand=o.brand, supplier_article=o.supplier_article,
            warehouse_name=o.warehouse_name, region_name=o.region_name,
        ))

    returns_count = len(all_returns)
    sales_count = len(sale_ok)
    total = sales_count + returns_count
    return_rate = round(returns_count / total * 100, 1) if total > 0 else 0
    return_value = round(sum(r.finished_price for r in all_returns), 2)

    # By product
    product_stats = defaultdict(lambda: {'returns': 0, 'sales': 0, 'return_value': 0.0, 'name': '', 'brand': '', 'article': ''})
    for s in sale_ok:
        nm_id = s.nm_id or 0
        product_stats[nm_id]['name'] = s.subject or product_stats[nm_id]['name']
        product_stats[nm_id]['brand'] = s.brand or product_stats[nm_id]['brand']
        product_stats[nm_id]['article'] = s.supplier_article or product_stats[nm_id]['article']
        product_stats[nm_id]['sales'] += 1

    for r in all_returns:
        nm_id = r.nm_id or 0
        product_stats[nm_id]['name'] = r.subject or product_stats[nm_id]['name']
        product_stats[nm_id]['brand'] = r.brand or product_stats[nm_id]['brand']
        product_stats[nm_id]['article'] = r.supplier_article or product_stats[nm_id]['article']
        product_stats[nm_id]['returns'] += 1
        product_stats[nm_id]['return_value'] += r.finished_price

    top_products = []
    for nm_id, stats in sorted(product_stats.items(), key=lambda x: x[1]['returns'], reverse=True)[:20]:
        if stats['returns'] > 0:
            t = stats['sales'] + stats['returns']
            top_products.append({
                'nmId': nm_id,
                'name': stats['name'],
                'brand': stats['brand'],
                'article': stats['article'],
                'sales': stats['sales'],
                'returns': stats['returns'],
                'returnRate': round(stats['returns'] / t * 100, 1) if t > 0 else 0,
                'returnValue': round(stats['return_value'], 2)
            })

    # By region
    region_stats = defaultdict(lambda: {'returns': 0, 'sales': 0})
    for s in sale_ok:
        region_stats[s.region_name or 'Неизвестно']['sales'] += 1
    for r in all_returns:
        region_stats[r.region_name or 'Неизвестно']['returns'] += 1

    top_regions = []
    for region, stats in sorted(region_stats.items(), key=lambda x: x[1]['returns'], reverse=True)[:15]:
        if stats['returns'] > 0:
            t = stats['sales'] + stats['returns']
            top_regions.append({
                'region': region,
                'sales': stats['sales'],
                'returns': stats['returns'],
                'returnRate': round(stats['returns'] / t * 100, 1) if t > 0 else 0
            })

    # By warehouse
    warehouse_stats = defaultdict(lambda: {'returns': 0, 'sales': 0})
    for s in sale_ok:
        warehouse_stats[s.warehouse_name or 'Неизвестно']['sales'] += 1
    for r in all_returns:
        warehouse_stats[r.warehouse_name or 'Неизвестно']['returns'] += 1

    top_warehouses = []
    for wh, stats in sorted(warehouse_stats.items(), key=lambda x: x[1]['returns'], reverse=True)[:10]:
        t = stats['sales'] + stats['returns']
        top_warehouses.append({
            'warehouse': wh,
            'sales': stats['sales'],
            'returns': stats['returns'],
            'returnRate': round(stats['returns'] / t * 100, 1) if t > 0 else 0
        })

    # Daily trend — fill all days in range for continuous chart
    daily_stats = defaultdict(lambda: {'sales': 0, 'returns': 0})
    for s in sale_ok:
        if s.date:
            daily_stats[s.date.strftime('%Y-%m-%d')]['sales'] += 1
    for r in all_returns:
        if r.date:
            daily_stats[r.date.strftime('%Y-%m-%d')]['returns'] += 1

    # Fill gaps — ensure every day in range has an entry
    daily_trend = []
    current = date_from.date() if hasattr(date_from, 'date') else date_from
    end = datetime.now().date()
    while current <= end:
        ds = current.strftime('%Y-%m-%d')
        d = daily_stats.get(ds, {'sales': 0, 'returns': 0})
        t = d['sales'] + d['returns']
        daily_trend.append({
            'date': ds,
            'sales': d['sales'],
            'returns': d['returns'],
            'returnRate': round(d['returns'] / t * 100, 1) if t > 0 else 0
        })
        current += timedelta(days=1)

    return {
        'totalSales': sales_count,
        'returnCount': returns_count,
        'returnRate': return_rate,
        'returnValue': return_value,
        'topProducts': top_products,
        'topRegions': top_regions,
        'topWarehouses': top_warehouses,
        'dailyTrend': daily_trend,
        'period': days
    }


def register_returns_routes(app):
    """Register returns analytics routes."""

    @app.route('/returns')
    @login_required
    def returns_page():
        """Returns analytics page (redirects to combined inventory page)."""
        return redirect(url_for('inventory_page'))

    @app.route('/api/returns/data')
    @login_required
    def api_returns_data():
        """Get returns analytics from local DB. Supports any period."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403

        try:
            days = min(int(request.args.get('days', 30)), 365)
            seller_id = current_user.seller.id

            data = _compute_returns_from_db(seller_id, days)
            return jsonify(data)

        except Exception as e:
            logger.error(f"Error in returns analytics: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/returns/status')
    @login_required
    def api_returns_status():
        """Backward compat — data is always ready from DB now."""
        if not current_user.seller:
            return jsonify({'error': 'No seller'}), 403
        days = min(int(request.args.get('days', 30)), 365)
        data = _compute_returns_from_db(current_user.seller.id, days)
        data['_cached'] = True
        data['_stale'] = False
        data['_loading'] = False
        return jsonify(data)
