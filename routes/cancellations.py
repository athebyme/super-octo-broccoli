from flask import render_template, jsonify, request, redirect, url_for, flash
from flask_login import login_required, current_user
import logging
from datetime import datetime, timedelta
from collections import defaultdict

from models import db, WBOrder

logger = logging.getLogger(__name__)


def _compute_cancellations_from_db(seller_id, days):
    """Compute cancellation analytics from local DB (wb_orders table)."""
    date_from = datetime.now() - timedelta(days=days)

    orders = WBOrder.query.filter(
        WBOrder.seller_id == seller_id,
        WBOrder.date >= date_from
    ).all()

    total_orders = len(orders)
    cancelled = [o for o in orders if o.is_cancel]
    cancel_count = len(cancelled)
    cancel_rate = round(cancel_count / total_orders * 100, 1) if total_orders > 0 else 0

    # By product
    product_stats = defaultdict(lambda: {'orders': 0, 'cancels': 0, 'name': '', 'brand': '', 'article': ''})
    for o in orders:
        nm_id = o.nm_id or 0
        product_stats[nm_id]['orders'] += 1
        product_stats[nm_id]['name'] = o.subject or product_stats[nm_id]['name']
        product_stats[nm_id]['brand'] = o.brand or product_stats[nm_id]['brand']
        product_stats[nm_id]['article'] = o.supplier_article or product_stats[nm_id]['article']
        if o.is_cancel:
            product_stats[nm_id]['cancels'] += 1

    top_cancelled = []
    for nm_id, stats in sorted(product_stats.items(), key=lambda x: x[1]['cancels'], reverse=True)[:20]:
        if stats['cancels'] > 0:
            top_cancelled.append({
                'nmId': nm_id,
                'name': stats['name'],
                'brand': stats['brand'],
                'article': stats['article'],
                'orders': stats['orders'],
                'cancels': stats['cancels'],
                'cancelRate': round(stats['cancels'] / stats['orders'] * 100, 1) if stats['orders'] > 0 else 0
            })

    # By region
    region_stats = defaultdict(lambda: {'orders': 0, 'cancels': 0})
    for o in orders:
        region = o.region_name or 'Неизвестно'
        region_stats[region]['orders'] += 1
        if o.is_cancel:
            region_stats[region]['cancels'] += 1

    top_regions = []
    for region, stats in sorted(region_stats.items(), key=lambda x: x[1]['cancels'], reverse=True)[:15]:
        if stats['cancels'] > 0:
            top_regions.append({
                'region': region,
                'orders': stats['orders'],
                'cancels': stats['cancels'],
                'cancelRate': round(stats['cancels'] / stats['orders'] * 100, 1) if stats['orders'] > 0 else 0
            })

    # By warehouse
    warehouse_stats = defaultdict(lambda: {'orders': 0, 'cancels': 0})
    for o in orders:
        wh = o.warehouse_name or 'Неизвестно'
        warehouse_stats[wh]['orders'] += 1
        if o.is_cancel:
            warehouse_stats[wh]['cancels'] += 1

    top_warehouses = []
    for wh, stats in sorted(warehouse_stats.items(), key=lambda x: x[1]['cancels'], reverse=True)[:10]:
        top_warehouses.append({
            'warehouse': wh,
            'orders': stats['orders'],
            'cancels': stats['cancels'],
            'cancelRate': round(stats['cancels'] / stats['orders'] * 100, 1) if stats['orders'] > 0 else 0
        })

    # Daily trend — fill all days
    daily_stats = defaultdict(lambda: {'orders': 0, 'cancels': 0})
    for o in orders:
        if o.date:
            date_str = o.date.strftime('%Y-%m-%d')
            daily_stats[date_str]['orders'] += 1
            if o.is_cancel:
                daily_stats[date_str]['cancels'] += 1

    daily_trend = []
    current = date_from.date() if hasattr(date_from, 'date') else date_from
    end = datetime.now().date()
    while current <= end:
        ds = current.strftime('%Y-%m-%d')
        s = daily_stats.get(ds, {'orders': 0, 'cancels': 0})
        daily_trend.append({
            'date': ds,
            'orders': s['orders'],
            'cancels': s['cancels'],
            'cancelRate': round(s['cancels'] / s['orders'] * 100, 1) if s['orders'] > 0 else 0
        })
        current += timedelta(days=1)

    # By category (using subject as proxy)
    category_stats = defaultdict(lambda: {'orders': 0, 'cancels': 0})
    for o in orders:
        cat = o.subject or 'Неизвестно'
        category_stats[cat]['orders'] += 1
        if o.is_cancel:
            category_stats[cat]['cancels'] += 1

    categories = []
    for cat, stats in sorted(category_stats.items(), key=lambda x: x[1]['cancels'], reverse=True)[:10]:
        categories.append({
            'category': cat,
            'orders': stats['orders'],
            'cancels': stats['cancels'],
            'cancelRate': round(stats['cancels'] / stats['orders'] * 100, 1) if stats['orders'] > 0 else 0
        })

    lost_revenue = sum(abs(o.finished_price or 0) for o in cancelled)

    return {
        'totalOrders': total_orders,
        'cancelCount': cancel_count,
        'cancelRate': cancel_rate,
        'lostRevenue': round(lost_revenue, 2),
        'topCancelled': top_cancelled,
        'topRegions': top_regions,
        'topWarehouses': top_warehouses,
        'dailyTrend': daily_trend,
        'categories': categories,
        'period': days
    }


def register_cancellations_routes(app):
    """Register cancellation analytics routes."""

    @app.route('/cancellations')
    @login_required
    def cancellations_page():
        """Cancellation analytics page."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для аналитики отмен необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('api_settings'))
        return render_template('cancellations.html')

    @app.route('/api/cancellations/data')
    @login_required
    def api_cancellations_data():
        """Get cancellation analytics from local DB. Supports any period."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403

        try:
            days = min(int(request.args.get('days', 30)), 365)
            seller_id = current_user.seller.id
            data = _compute_cancellations_from_db(seller_id, days)
            return jsonify(data)

        except Exception as e:
            logger.error(f"Error in cancellations analytics: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/cancellations/status')
    @login_required
    def api_cancellations_status():
        """Backward compat — data is always ready from DB."""
        if not current_user.seller:
            return jsonify({'error': 'No seller'}), 403
        days = min(int(request.args.get('days', 30)), 365)
        data = _compute_cancellations_from_db(current_user.seller.id, days)
        data['_cached'] = True
        data['_stale'] = False
        data['_loading'] = False
        return jsonify(data)
