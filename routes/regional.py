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

# In-memory cache: {seller_id:days -> {data, updated_at, loading}}
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


def _compute_regional_analytics(orders, days):
    """Compute regional analytics from raw orders list."""
    total_orders = len(orders)

    # Aggregate by region
    region_stats = defaultdict(lambda: {
        'orders': 0, 'revenue': 0.0, 'total_spp': 0.0,
        'cancels': 0, 'finished_prices': []
    })
    for o in orders:
        region = o.get('regionName', 'Неизвестно') or 'Неизвестно'
        s = region_stats[region]
        s['orders'] += 1
        finished = float(o.get('finishedPrice', 0) or 0)
        s['revenue'] += finished
        s['finished_prices'].append(finished)
        s['total_spp'] += float(o.get('spp', 0) or 0)
        if o.get('isCancel'):
            s['cancels'] += 1

    # Build regions list
    all_regions = []
    for region, s in region_stats.items():
        avg_check = round(s['revenue'] / s['orders'], 2) if s['orders'] > 0 else 0
        avg_spp = round(s['total_spp'] / s['orders'], 1) if s['orders'] > 0 else 0
        cancel_rate = round(s['cancels'] / s['orders'] * 100, 1) if s['orders'] > 0 else 0
        all_regions.append({
            'region': region,
            'orders': s['orders'],
            'revenue': round(s['revenue'], 2),
            'avgCheck': avg_check,
            'avgSpp': avg_spp,
            'cancels': s['cancels'],
            'cancelRate': cancel_rate
        })

    # Sort by revenue descending
    all_regions.sort(key=lambda x: x['revenue'], reverse=True)

    # Top regions by revenue (top 15)
    top_regions_revenue = all_regions[:15]

    # Top regions by order count
    top_regions_orders = sorted(all_regions, key=lambda x: x['orders'], reverse=True)[:15]

    # Federal districts (oblastOkrugName)
    district_stats = defaultdict(lambda: {'orders': 0, 'revenue': 0.0})
    for o in orders:
        district = o.get('oblastOkrugName', 'Неизвестно') or 'Неизвестно'
        district_stats[district]['orders'] += 1
        district_stats[district]['revenue'] += float(o.get('finishedPrice', 0) or 0)

    top_districts = []
    for district, s in sorted(district_stats.items(), key=lambda x: x[1]['revenue'], reverse=True)[:10]:
        top_districts.append({
            'district': district,
            'orders': s['orders'],
            'revenue': round(s['revenue'], 2)
        })

    # Total revenue & avg check overall
    total_revenue = sum(float(o.get('finishedPrice', 0) or 0) for o in orders)
    avg_check_total = round(total_revenue / total_orders, 2) if total_orders > 0 else 0
    total_regions_count = len(region_stats)

    # Top region by revenue
    top_region_name = all_regions[0]['region'] if all_regions else '—'

    # Daily breakdown by top 5 regions
    top5_names = [r['region'] for r in all_regions[:5]]
    daily_by_region = defaultdict(lambda: defaultdict(lambda: {'orders': 0, 'revenue': 0.0}))
    for o in orders:
        date_str = o.get('date', '')[:10]
        region = o.get('regionName', 'Неизвестно') or 'Неизвестно'
        if date_str and region in top5_names:
            daily_by_region[date_str][region]['orders'] += 1
            daily_by_region[date_str][region]['revenue'] += float(o.get('finishedPrice', 0) or 0)

    daily_trend = []
    for date_str in sorted(daily_by_region.keys()):
        entry = {'date': date_str, 'regions': {}}
        for rname in top5_names:
            d = daily_by_region[date_str].get(rname, {'orders': 0, 'revenue': 0.0})
            entry['regions'][rname] = {
                'orders': d['orders'],
                'revenue': round(d['revenue'], 2)
            }
        daily_trend.append(entry)

    return {
        'totalOrders': total_orders,
        'totalRevenue': round(total_revenue, 2),
        'totalRegions': total_regions_count,
        'avgCheck': avg_check_total,
        'topRegionName': top_region_name,
        'topRegionsRevenue': top_regions_revenue,
        'topRegionsOrders': top_regions_orders,
        'topDistricts': top_districts,
        'allRegions': all_regions,
        'dailyTrend': daily_trend,
        'top5Names': top5_names,
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
            data = _compute_regional_analytics(orders, days)
            _set_cached(seller_id, days, data)
            logger.info(f"Regional cache updated for seller {seller_id}, {days}d: {len(orders)} orders")
        else:
            _set_loading(seller_id, days, False)
    except Exception as e:
        logger.error(f"Background regional fetch error for seller {seller_id}: {e}")
        _set_loading(seller_id, days, False)


def register_regional_routes(app):
    """Register regional analytics routes."""

    @app.route('/regional')
    @login_required
    def regional_page():
        """Regional analytics page."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для региональной аналитики необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('api_settings'))
        return render_template('regional.html')

    @app.route('/api/regional/data')
    @login_required
    def api_regional_data():
        """Get regional analytics data. Returns cached data instantly if available,
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
                f"{STATISTICS_API_URL}/api/v1/supplier/orders",
                params={'dateFrom': date_from},
                timeout=60
            )
            resp.raise_for_status()
            orders = resp.json()

            if not isinstance(orders, list):
                return jsonify({'error': 'Unexpected API response'}), 500

            data = _compute_regional_analytics(orders, days)
            _set_cached(seller_id, days, data)
            return jsonify(data)

        except requests.exceptions.RequestException as e:
            logger.error(f"WB API error in regional: {e}")
            return jsonify({'error': f'Ошибка WB API: {str(e)}'}), 502
        except Exception as e:
            logger.error(f"Error in regional analytics: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/regional/status')
    @login_required
    def api_regional_status():
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
