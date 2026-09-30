from flask import render_template, jsonify, request, redirect, url_for, flash
from flask_login import login_required, current_user
import requests
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from sqlalchemy.orm import Load

from models import db, Product, ProductStock

logger = logging.getLogger(__name__)

STATISTICS_API_URL = "https://statistics-api.wildberries.ru"

LOW_STOCK_THRESHOLD = 5


def register_warehouse_routes(app):
    """Register warehouse analytics routes."""

    @app.route('/inventory')
    @login_required
    def inventory_page():
        """Combined warehouse + returns analytics page."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            flash('Для складской аналитики необходимо настроить API ключ WB', 'warning')
            return redirect(url_for('api_settings'))
        return render_template('inventory.html')

    @app.route('/warehouse')
    @login_required
    def warehouse_page():
        """Warehouse analytics page (redirects to combined page)."""
        return redirect(url_for('inventory_page'))

    @app.route('/api/warehouse/data')
    @login_required
    def api_warehouse_data():
        """Get warehouse analytics data from DB."""
        if not current_user.seller:
            return jsonify({'error': 'Продавец не настроен'}), 403

        try:
            seller_id = current_user.seller.id
            from services.wb_stock_sync import latest_stock_sync
            sync = latest_stock_sync(seller_id)

            # Query all stocks joined with products for this seller
            stocks = db.session.query(
                ProductStock, Product
            ).options(
                Load(Product).load_only(Product.id, Product.nm_id, Product.title, Product.vendor_code, Product.brand, Product.price, Product.discount_price),
            ).join(
                Product, ProductStock.product_id == Product.id
            ).filter(
                Product.seller_id == seller_id
            ).all()

            if not stocks:
                return jsonify({
                    'sync': sync,
                    'observedAt': sync['updated_at'] if sync and sync['status'] == 'completed' else None,
                    'observationAvailable': bool(sync and sync['status'] == 'completed'),
                    'totalQuantity': 0,
                    'warehouseCount': 0,
                    'stockValue': 0,
                    'lowStockCount': 0,
                    'warehouses': [],
                    'topProducts': [],
                    'lowStockAlerts': [],
                })

            # Aggregate by warehouse NAME (WB has multiple warehouse_ids per physical warehouse)
            wh_data = defaultdict(lambda: {
                'name': '',
                'totalQty': 0,
                'totalValue': 0,
                'inWayToClient': 0,
                'inWayFromClient': 0,
                'products': set(),
            })

            # Aggregate products by nm_id (sum across all warehouses)
            product_agg = defaultdict(lambda: {
                'nmId': 0,
                'title': '',
                'brand': '',
                'vendorCode': '',
                'quantity': 0,
                'value': 0.0,
            })

            low_stock_alerts = []
            total_quantity = 0
            total_value = 0

            for stock, product in stocks:
                wh_name = stock.warehouse_name or f'Склад {stock.warehouse_id}'
                wh = wh_data[wh_name]
                wh['name'] = wh_name

                qty = stock.quantity or 0
                price = float(product.discount_price or product.price or 0)
                value = qty * price

                wh['totalQty'] += qty
                wh['totalValue'] += value
                wh['inWayToClient'] += stock.in_way_to_client or 0
                wh['inWayFromClient'] += stock.in_way_from_client or 0
                wh['products'].add(product.id)

                total_quantity += qty
                total_value += value

                # Aggregate per product
                pa = product_agg[product.nm_id]
                pa['id'] = product.id
                pa['nmId'] = product.nm_id
                pa['title'] = product.title or product.vendor_code or str(product.nm_id)
                pa['brand'] = product.brand or ''
                pa['vendorCode'] = product.vendor_code or ''
                pa['quantity'] += qty
                pa['value'] += value

                # Low stock alerts: per stock record with low qty
                if 0 < qty < LOW_STOCK_THRESHOLD:
                    low_stock_alerts.append({
                        'id': product.id,
                        'nmId': product.nm_id,
                        'title': product.title or product.vendor_code or str(product.nm_id),
                        'brand': product.brand or '',
                        'vendorCode': product.vendor_code or '',
                        'warehouse': wh_name,
                        'quantity': qty,
                    })

            # Build warehouse list
            warehouses = []
            for wh_name, wh in sorted(wh_data.items(), key=lambda x: x[1]['totalQty'], reverse=True):
                warehouses.append({
                    'name': wh['name'],
                    'productCount': len(wh['products']),
                    'totalQty': wh['totalQty'],
                    'totalValue': round(wh['totalValue'], 2),
                    'inWayToClient': wh['inWayToClient'],
                    'inWayFromClient': wh['inWayFromClient'],
                })

            # Top stocked products (by total quantity across all warehouses)
            all_products = sorted(product_agg.values(), key=lambda x: x['quantity'], reverse=True)
            top_products = [{**p, 'value': round(p['value'], 2)} for p in all_products[:20]]

            # Low stock sorted by quantity ascending
            low_stock_alerts.sort(key=lambda x: x['quantity'])

            # Dead stock: products with highest stock
            dead_stock = [{**p, 'value': round(p['value'], 2)} for p in all_products[:10]]

            return jsonify({
                'sync': sync,
                'observedAt': min(stock.updated_at for stock, _ in stocks if stock.updated_at).isoformat() if any(stock.updated_at for stock, _ in stocks) else None,
                'observationAvailable': True,
                'totalQuantity': total_quantity,
                'warehouseCount': len(warehouses),
                'stockValue': round(total_value, 2),
                'lowStockCount': len(low_stock_alerts),
                'warehouses': warehouses,
                'topProducts': top_products,
                'lowStockAlerts': low_stock_alerts[:20],
                'deadStock': dead_stock,
            })

        except Exception as e:
            logger.error(f"Error in warehouse analytics: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/warehouse/refresh', methods=['POST'])
    @login_required
    def api_warehouse_refresh():
        """Enqueue the same bounded Analytics reader used by catalog sync."""
        if not current_user.seller:
            return jsonify({'error': 'Продавец не настроен'}), 403
        from services.wb_stock_sync import WBStockSyncError, enqueue_stock_sync
        try:
            job = enqueue_stock_sync(current_user.seller.id)
            return jsonify({
                'success': True, 'queued': True, 'sync': job,
                'message': 'Обновление остатков в очереди. Последние данные остаются доступны; прогресс виден в фоновых задачах.',
            }), 202
        except WBStockSyncError as exc:
            db.session.rollback()
            return jsonify({'error': str(exc), 'code': exc.code}), exc.status_code

    @app.route('/api/analytics/sync', methods=['POST'])
    @login_required
    def api_analytics_sync():
        """Trigger manual WB analytics data sync for current seller."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403

        import threading

        def _run_sync(seller_id, app):
            with app.app_context():
                from models import Seller
                from services.wb_data_sync import sync_all
                seller = Seller.query.get(seller_id)
                if seller:
                    try:
                        result = sync_all(seller)
                        logger.info(f"Manual analytics sync for seller={seller_id}: {result}")
                    except Exception as e:
                        logger.error(f"Manual analytics sync failed for seller={seller_id}: {e}")

        thread = threading.Thread(
            target=_run_sync,
            args=(current_user.seller.id, app._get_current_object()),
            daemon=True,
            name=f"manual-analytics-sync-{current_user.seller.id}"
        )
        thread.start()

        return jsonify({
            'success': True,
            'message': 'Синхронизация запущена. Данные появятся через 1-3 минуты.'
        })

    @app.route('/api/analytics/sync-status')
    @login_required
    def api_analytics_sync_status():
        """Check how much analytics data exists for current seller."""
        if not current_user.seller:
            return jsonify({'error': 'Нет профиля продавца'}), 403

        from models import WBSale, WBOrder, WBFeedback, WBRealizationRow
        seller_id = current_user.seller.id

        sales_count = WBSale.query.filter_by(seller_id=seller_id).count()
        orders_count = WBOrder.query.filter_by(seller_id=seller_id).count()
        feedbacks_count = WBFeedback.query.filter_by(seller_id=seller_id).count()
        realization_count = WBRealizationRow.query.filter_by(seller_id=seller_id).count()

        return jsonify({
            'sales': sales_count,
            'orders': orders_count,
            'feedbacks': feedbacks_count,
            'realization': realization_count,
            'total': sales_count + orders_count + feedbacks_count + realization_count,
            'has_data': (sales_count + orders_count + feedbacks_count + realization_count) > 0,
        })

    @app.route('/api/settings/stock-refresh', methods=['POST'])
    @login_required
    def api_stock_refresh_settings():
        """Update stock refresh interval setting."""
        if not current_user.seller:
            return jsonify({'error': 'Нет профиля продавца'}), 403
        try:
            data = request.get_json()
            interval = int(data.get('interval', 30))
            interval = max(1, min(60, interval))
            current_user.seller.stock_refresh_interval = interval
            db.session.commit()
            return jsonify({'success': True, 'interval': interval})
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error saving stock refresh interval: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/settings/stock-refresh', methods=['GET'])
    @login_required
    def api_stock_refresh_settings_get():
        """Get stock refresh interval setting."""
        if not current_user.seller:
            return jsonify({'error': 'Нет профиля продавца'}), 403
        return jsonify({'interval': current_user.seller.stock_refresh_interval or 30})
