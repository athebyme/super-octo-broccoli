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

# In-memory cache: {seller_id: {period: {data, updated_at, loading}}}
_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = 300  # 5 minutes


def _get_cache_key(seller_id, days):
    return f"{seller_id}:{days}"


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


def _compute_analytics(orders, days):
    """Compute all analytics from raw orders list."""
    total_orders = len(orders)
    cancelled = [o for o in orders if o.get('isCancel')]
    cancel_count = len(cancelled)
    cancel_rate = round(cancel_count / total_orders * 100, 1) if total_orders > 0 else 0

    # By product (nmId)
    product_stats = defaultdict(lambda: {'orders': 0, 'cancels': 0, 'name': '', 'brand': '', 'article': ''})
    for o in orders:
        nm_id = o.get('nmId', 0)
        product_stats[nm_id]['orders'] += 1
        product_stats[nm_id]['name'] = o.get('subject', '')
        product_stats[nm_id]['brand'] = o.get('brand', '')
        product_stats[nm_id]['article'] = o.get('supplierArticle', '')
        if o.get('isCancel'):
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
        region = o.get('regionName', 'Неизвестно') or 'Неизвестно'
        region_stats[region]['orders'] += 1
        if o.get('isCancel'):
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
        wh = o.get('warehouseName', 'Неизвестно') or 'Неизвестно'
        warehouse_stats[wh]['orders'] += 1
        if o.get('isCancel'):
            warehouse_stats[wh]['cancels'] += 1

    top_warehouses = []
    for wh, stats in sorted(warehouse_stats.items(), key=lambda x: x[1]['cancels'], reverse=True)[:10]:
        top_warehouses.append({
            'warehouse': wh,
            'orders': stats['orders'],
            'cancels': stats['cancels'],
            'cancelRate': round(stats['cancels'] / stats['orders'] * 100, 1) if stats['orders'] > 0 else 0
        })

    # Daily trend
    daily_stats = defaultdict(lambda: {'orders': 0, 'cancels': 0})
    for o in orders:
        date_str = o.get('date', '')[:10]
        if date_str:
            daily_stats[date_str]['orders'] += 1
            if o.get('isCancel'):
                daily_stats[date_str]['cancels'] += 1

    daily_trend = []
    for date_str in sorted(daily_stats.keys()):
        s = daily_stats[date_str]
        daily_trend.append({
            'date': date_str,
            'orders': s['orders'],
            'cancels': s['cancels'],
            'cancelRate': round(s['cancels'] / s['orders'] * 100, 1) if s['orders'] > 0 else 0
        })

    # By category
    category_stats = defaultdict(lambda: {'orders': 0, 'cancels': 0})
    for o in orders:
        cat = o.get('category', 'Неизвестно') or 'Неизвестно'
        category_stats[cat]['orders'] += 1
        if o.get('isCancel'):
            category_stats[cat]['cancels'] += 1

    categories = []
    for cat, stats in sorted(category_stats.items(), key=lambda x: x[1]['cancels'], reverse=True)[:10]:
        categories.append({
            'category': cat,
            'orders': stats['orders'],
            'cancels': stats['cancels'],
            'cancelRate': round(stats['cancels'] / stats['orders'] * 100, 1) if stats['orders'] > 0 else 0
        })

    lost_revenue = sum(float(o.get('finishedPrice', 0) or 0) for o in cancelled)

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


def _fetch_and_cache(api_key, seller_id, days):
    """Fetch data from WB API and update cache. Runs in background thread."""
    try:
        _set_loading(seller_id, days, True)
        date_from = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')

        session = requests.Session()
        session.headers.update({
            'Authorization': api_key,
            'Content-Type': 'application/json'
        })
        resp = session.get(
            f"{STATISTICS_API_URL}/api/v1/supplier/orders",
            params={'dateFrom': date_from},
            timeout=60
        )
        resp.raise_for_status()
        orders = resp.json()

        if isinstance(orders, list):
            data = _compute_analytics(orders, days)
            _set_cached(seller_id, days, data)
            logger.info(f"Cancellations cache updated for seller {seller_id}, {days}d: {len(orders)} orders")
        else:
            _set_loading(seller_id, days, False)
    except Exception as e:
        logger.error(f"Background cancellations fetch error for seller {seller_id}: {e}")
        _set_loading(seller_id, days, False)


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
        """Get cancellation analytics data. Returns cached data instantly if available,
        triggers background refresh if stale."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403

        try:
            days = min(int(request.args.get('days', 30)), 90)
            seller_id = current_user.seller.id
            force = request.args.get('force', '').lower() == 'true'

            cached_data, is_fresh, is_loading = _get_cached(seller_id, days)

            if cached_data and not force:
                # Return cached data immediately
                result = dict(cached_data)
                result['_cached'] = True
                result['_stale'] = not is_fresh
                result['_loading'] = is_loading

                # Trigger background refresh if stale and not already loading
                if not is_fresh and not is_loading:
                    t = threading.Thread(
                        target=_fetch_and_cache,
                        args=(current_user.seller.wb_api_key, seller_id, days),
                        daemon=True
                    )
                    t.start()

                return jsonify(result)

            # No cache — check if already loading
            if is_loading:
                return jsonify({'_loading': True, '_cached': False}), 202

            # No cache, not loading — fetch synchronously for first load
            # but also start background thread for very first request
            if not force:
                # Start background fetch
                _set_loading(seller_id, days, True)
                t = threading.Thread(
                    target=_fetch_and_cache,
                    args=(current_user.seller.wb_api_key, seller_id, days),
                    daemon=True
                )
                t.start()
                return jsonify({'_loading': True, '_cached': False}), 202

            # Force refresh — synchronous
            date_from = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            session = requests.Session()
            session.headers.update({
                'Authorization': current_user.seller.wb_api_key,
                'Content-Type': 'application/json'
            })
            resp = session.get(
                f"{STATISTICS_API_URL}/api/v1/supplier/orders",
                params={'dateFrom': date_from},
                timeout=60
            )
            resp.raise_for_status()
            orders = resp.json()

            if not isinstance(orders, list):
                return jsonify({'error': 'Unexpected API response'}), 500

            data = _compute_analytics(orders, days)
            _set_cached(seller_id, days, data)
            return jsonify(data)

        except requests.exceptions.RequestException as e:
            logger.error(f"WB API error in cancellations: {e}")
            return jsonify({'error': f'Ошибка WB API: {str(e)}'}), 502
        except Exception as e:
            logger.error(f"Error in cancellations analytics: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/cancellations/status')
    @login_required
    def api_cancellations_status():
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
