from flask import render_template, jsonify, request, redirect, url_for, flash
from flask_login import login_required, current_user
import logging
from datetime import datetime, timedelta
from collections import defaultdict

from models import db, WBOrder

logger = logging.getLogger(__name__)


def _compute_regional_from_db(seller_id, days):
    """Compute regional analytics from local DB (wb_orders table)."""
    date_from = datetime.now() - timedelta(days=days)

    orders = WBOrder.query.filter(
        WBOrder.seller_id == seller_id,
        WBOrder.date >= date_from
    ).all()

    total_orders = len(orders)

    # Aggregate by region
    region_stats = defaultdict(lambda: {'orders': 0, 'revenue': 0.0, 'cancels': 0})
    for o in orders:
        region = o.region_name or 'Неизвестно'
        s = region_stats[region]
        s['orders'] += 1
        s['revenue'] += abs(o.finished_price or 0)
        if o.is_cancel:
            s['cancels'] += 1

    all_regions = []
    for region, s in region_stats.items():
        avg_check = round(s['revenue'] / s['orders'], 2) if s['orders'] > 0 else 0
        cancel_rate = round(s['cancels'] / s['orders'] * 100, 1) if s['orders'] > 0 else 0
        all_regions.append({
            'region': region,
            'orders': s['orders'],
            'revenue': round(s['revenue'], 2),
            'avgCheck': avg_check,
            'avgSpp': 0,
            'cancels': s['cancels'],
            'cancelRate': cancel_rate
        })

    all_regions.sort(key=lambda x: x['revenue'], reverse=True)
    top_regions_revenue = all_regions[:15]
    top_regions_orders = sorted(all_regions, key=lambda x: x['orders'], reverse=True)[:15]

    # Federal districts
    district_stats = defaultdict(lambda: {'orders': 0, 'revenue': 0.0})
    for o in orders:
        district = o.oblast_okrug_name or 'Неизвестно'
        district_stats[district]['orders'] += 1
        district_stats[district]['revenue'] += abs(o.finished_price or 0)

    top_districts = []
    for district, s in sorted(district_stats.items(), key=lambda x: x[1]['revenue'], reverse=True)[:10]:
        top_districts.append({
            'district': district,
            'orders': s['orders'],
            'revenue': round(s['revenue'], 2)
        })

    total_revenue = sum(abs(o.finished_price or 0) for o in orders)
    avg_check_total = round(total_revenue / total_orders, 2) if total_orders > 0 else 0
    top_region_name = all_regions[0]['region'] if all_regions else '—'

    # Daily breakdown by top 5 regions
    top5_names = [r['region'] for r in all_regions[:5]]
    daily_by_region = defaultdict(lambda: defaultdict(lambda: {'orders': 0, 'revenue': 0.0}))
    for o in orders:
        if o.date:
            date_str = o.date.strftime('%Y-%m-%d')
            region = o.region_name or 'Неизвестно'
            if region in top5_names:
                daily_by_region[date_str][region]['orders'] += 1
                daily_by_region[date_str][region]['revenue'] += abs(o.finished_price or 0)

    # Fill all days
    daily_trend = []
    current = date_from.date() if hasattr(date_from, 'date') else date_from
    end = datetime.now().date()
    while current <= end:
        ds = current.strftime('%Y-%m-%d')
        entry = {'date': ds, 'regions': {}}
        for rname in top5_names:
            d = daily_by_region.get(ds, {}).get(rname, {'orders': 0, 'revenue': 0.0})
            entry['regions'][rname] = {
                'orders': d['orders'],
                'revenue': round(d['revenue'], 2)
            }
        daily_trend.append(entry)
        current += timedelta(days=1)

    return {
        'totalOrders': total_orders,
        'totalRevenue': round(total_revenue, 2),
        'totalRegions': len(region_stats),
        'avgCheck': avg_check_total,
        'topRegionName': top_region_name,
        'topRegionsRevenue': top_regions_revenue,
        'topRegionsOrders': top_regions_orders,
        'topDistricts': top_districts,
        'allRegions': all_regions,
        'dailyTrend': daily_trend,
        'top5RegionNames': top5_names,
        'period': days
    }


def register_regional_routes(app):
    """Register regional analytics routes."""

    @app.route('/regional')
    @login_required
    def regional_page():
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для региональной аналитики необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('api_settings'))
        return render_template('regional.html')

    @app.route('/api/regional/data')
    @login_required
    def api_regional_data():
        """Get regional analytics from local DB. Supports any period."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403

        try:
            days = min(int(request.args.get('days', 30)), 365)
            seller_id = current_user.seller.id
            data = _compute_regional_from_db(seller_id, days)
            return jsonify(data)

        except Exception as e:
            logger.error(f"Error in regional analytics: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/regional/status')
    @login_required
    def api_regional_status():
        """Backward compat — data always ready from DB."""
        if not current_user.seller:
            return jsonify({'error': 'No seller'}), 403
        days = min(int(request.args.get('days', 30)), 365)
        data = _compute_regional_from_db(current_user.seller.id, days)
        data['_cached'] = True
        data['_stale'] = False
        data['_loading'] = False
        return jsonify(data)
