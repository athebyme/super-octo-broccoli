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

# In-memory cache: {seller_id:days: {data, updated_at, loading}}
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


def _compute_analytics(sales_list, days):
    """Compute returns analytics from raw sales list."""
    returns = [s for s in sales_list
               if str(s.get('saleID', '')).startswith('R') or float(s.get('finishedPrice', 0) or 0) < 0]
    sales = [s for s in sales_list if str(s.get('saleID', '')).startswith('S')]

    returns_count = len(returns)
    sales_count = len(sales)
    total = sales_count + returns_count
    return_rate = round(returns_count / total * 100, 1) if total > 0 else 0
    return_value = round(sum(abs(float(s.get('finishedPrice', 0) or 0)) for s in returns), 2)

    # By product (nmId)
    product_stats = defaultdict(lambda: {'returns': 0, 'sales': 0, 'return_value': 0.0, 'name': '', 'brand': '', 'article': ''})
    for s in sales_list:
        nm_id = s.get('nmId', 0)
        product_stats[nm_id]['name'] = s.get('subject', '')
        product_stats[nm_id]['brand'] = s.get('brand', '')
        product_stats[nm_id]['article'] = s.get('supplierArticle', '')
        sale_id = str(s.get('saleID', ''))
        finished_price = float(s.get('finishedPrice', 0) or 0)
        if sale_id.startswith('R') or finished_price < 0:
            product_stats[nm_id]['returns'] += 1
            product_stats[nm_id]['return_value'] += abs(finished_price)
        elif sale_id.startswith('S'):
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
    for s in sales_list:
        region = s.get('regionName', 'Неизвестно') or 'Неизвестно'
        sale_id = str(s.get('saleID', ''))
        finished_price = float(s.get('finishedPrice', 0) or 0)
        if sale_id.startswith('R') or finished_price < 0:
            region_stats[region]['returns'] += 1
        elif sale_id.startswith('S'):
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
    for s in sales_list:
        wh = s.get('warehouseName', 'Неизвестно') or 'Неизвестно'
        sale_id = str(s.get('saleID', ''))
        finished_price = float(s.get('finishedPrice', 0) or 0)
        if sale_id.startswith('R') or finished_price < 0:
            warehouse_stats[wh]['returns'] += 1
        elif sale_id.startswith('S'):
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

    # Daily trend
    daily_stats = defaultdict(lambda: {'sales': 0, 'returns': 0})
    for s in sales_list:
        date_str = s.get('date', '')[:10]
        if date_str:
            sale_id = str(s.get('saleID', ''))
            finished_price = float(s.get('finishedPrice', 0) or 0)
            if sale_id.startswith('R') or finished_price < 0:
                daily_stats[date_str]['returns'] += 1
            elif sale_id.startswith('S'):
                daily_stats[date_str]['sales'] += 1

    daily_trend = []
    for date_str in sorted(daily_stats.keys()):
        d = daily_stats[date_str]
        t = d['sales'] + d['returns']
        daily_trend.append({
            'date': date_str,
            'sales': d['sales'],
            'returns': d['returns'],
            'returnRate': round(d['returns'] / t * 100, 1) if t > 0 else 0
        })

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
            f"{STATISTICS_API_URL}/api/v1/supplier/sales",
            params={'dateFrom': date_from},
            timeout=60
        )
        resp.raise_for_status()
        sales_list = resp.json()

        if isinstance(sales_list, list):
            data = _compute_analytics(sales_list, days)
            _set_cached(seller_id, days, data)
            logger.info(f"Returns cache updated for seller {seller_id}, {days}d: {len(sales_list)} sales entries")
        else:
            _set_loading(seller_id, days, False)
    except Exception as e:
        logger.error(f"Background returns fetch error for seller {seller_id}: {e}")
        _set_loading(seller_id, days, False)


def register_returns_routes(app):
    """Register returns analytics routes."""

    @app.route('/returns')
    @login_required
    def returns_page():
        """Returns analytics page."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для аналитики возвратов необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('api_settings'))
        return render_template('returns.html')

    @app.route('/api/returns/data')
    @login_required
    def api_returns_data():
        """Get returns analytics data. Returns cached data instantly if available,
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

            # Force refresh — synchronous
            date_from = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            session = requests.Session()
            session.headers.update({
                'Authorization': current_user.seller.wb_api_key,
                'Content-Type': 'application/json'
            })
            resp = session.get(
                f"{STATISTICS_API_URL}/api/v1/supplier/sales",
                params={'dateFrom': date_from},
                timeout=60
            )
            resp.raise_for_status()
            sales_list = resp.json()

            if not isinstance(sales_list, list):
                return jsonify({'error': 'Unexpected API response'}), 500

            data = _compute_analytics(sales_list, days)
            _set_cached(seller_id, days, data)
            return jsonify(data)

        except requests.exceptions.RequestException as e:
            logger.error(f"WB API error in returns: {e}")
            return jsonify({'error': f'Ошибка WB API: {str(e)}'}), 502
        except Exception as e:
            logger.error(f"Error in returns analytics: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/returns/status')
    @login_required
    def api_returns_status():
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
