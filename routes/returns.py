from flask import jsonify, request, redirect, url_for
from flask_login import login_required, current_user
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from sqlalchemy import func, case, and_

from models import db, WBSale

logger = logging.getLogger(__name__)


def _compute_returns_from_db(seller_id, days):
    """Compute returns analytics from local DB (wb_sales table)."""
    date_from = datetime.now() - timedelta(days=days)

    sales_rows = WBSale.query.filter(
        WBSale.seller_id == seller_id,
        WBSale.date >= date_from
    ).all()

    returns = [s for s in sales_rows if s.is_return]
    sales = [s for s in sales_rows if not s.is_return]

    returns_count = len(returns)
    sales_count = len(sales)
    total = sales_count + returns_count
    return_rate = round(returns_count / total * 100, 1) if total > 0 else 0
    return_value = round(sum(abs(s.finished_price or 0) for s in returns), 2)

    # By product
    product_stats = defaultdict(lambda: {'returns': 0, 'sales': 0, 'return_value': 0.0, 'name': '', 'brand': '', 'article': ''})
    for s in sales_rows:
        nm_id = s.nm_id or 0
        product_stats[nm_id]['name'] = s.subject or product_stats[nm_id]['name']
        product_stats[nm_id]['brand'] = s.brand or product_stats[nm_id]['brand']
        product_stats[nm_id]['article'] = s.supplier_article or product_stats[nm_id]['article']
        if s.is_return:
            product_stats[nm_id]['returns'] += 1
            product_stats[nm_id]['return_value'] += abs(s.finished_price or 0)
        else:
            product_stats[nm_id]['sales'] += 1

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
    for s in sales_rows:
        region = s.region_name or 'Неизвестно'
        if s.is_return:
            region_stats[region]['returns'] += 1
        else:
            region_stats[region]['sales'] += 1

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
    for s in sales_rows:
        wh = s.warehouse_name or 'Неизвестно'
        if s.is_return:
            warehouse_stats[wh]['returns'] += 1
        else:
            warehouse_stats[wh]['sales'] += 1

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
    for s in sales_rows:
        if s.date:
            date_str = s.date.strftime('%Y-%m-%d')
            if s.is_return:
                daily_stats[date_str]['returns'] += 1
            else:
                daily_stats[date_str]['sales'] += 1

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
