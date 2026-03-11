from flask import render_template, jsonify, request, redirect, url_for, flash
from flask_login import login_required, current_user
import requests
import logging
from datetime import datetime, timedelta
from collections import defaultdict

logger = logging.getLogger(__name__)

STATISTICS_API_URL = "https://statistics-api.wildberries.ru"


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
        """Get cancellation analytics data."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403

        try:
            days = int(request.args.get('days', 30))
            days = min(days, 90)  # WB API max 90 days
            date_from = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')

            # Fetch orders from WB API
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

            # Process analytics
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

            # Top cancelled products
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

            # Lost revenue from cancellations
            lost_revenue = sum(float(o.get('finishedPrice', 0) or 0) for o in cancelled)

            return jsonify({
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
            })

        except requests.exceptions.RequestException as e:
            logger.error(f"WB API error in cancellations: {e}")
            return jsonify({'error': f'Ошибка WB API: {str(e)}'}), 502
        except Exception as e:
            logger.error(f"Error in cancellations analytics: {e}")
            return jsonify({'error': str(e)}), 500
