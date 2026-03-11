from flask import render_template, jsonify, request, redirect, url_for, flash
from flask_login import login_required, current_user
import requests
import logging
from datetime import datetime, timedelta
from collections import defaultdict

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

            # Query all stocks joined with products for this seller
            stocks = db.session.query(
                ProductStock, Product
            ).join(
                Product, ProductStock.product_id == Product.id
            ).filter(
                Product.seller_id == seller_id
            ).all()

            if not stocks:
                return jsonify({
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
        """Fetch fresh stock data from WB Statistics API and update ProductStock table."""
        if not current_user.seller or not current_user.seller.has_valid_api_key():
            return jsonify({'error': 'API ключ WB не настроен'}), 403

        try:
            seller_id = current_user.seller.id
            api_key = current_user.seller.wb_api_key

            # Fetch stocks from WB API
            date_from = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
            session = requests.Session()
            session.headers.update({
                'Authorization': api_key,
                'Content-Type': 'application/json'
            })

            resp = session.get(
                f"{STATISTICS_API_URL}/api/v1/supplier/stocks",
                params={'dateFrom': date_from},
                timeout=60
            )
            resp.raise_for_status()
            wb_stocks = resp.json()

            if not isinstance(wb_stocks, list):
                return jsonify({'error': 'Неожиданный ответ API'}), 500

            # Build a map of nmId -> product_id for this seller
            products = Product.query.filter_by(seller_id=seller_id).all()
            nm_to_product = {p.nm_id: p for p in products}

            updated = 0
            created = 0

            for item in wb_stocks:
                nm_id = item.get('nmId')
                if not nm_id:
                    continue

                product = nm_to_product.get(nm_id)
                if not product:
                    continue

                warehouse_id = item.get('warehouseId') or 0
                warehouse_name = item.get('warehouseName', '')

                # Try to find existing stock record
                stock = ProductStock.query.filter_by(
                    product_id=product.id,
                    warehouse_id=warehouse_id
                ).first()

                if stock:
                    stock.warehouse_name = warehouse_name
                    stock.quantity = item.get('quantity', 0)
                    stock.quantity_full = item.get('quantityFull', 0)
                    stock.in_way_to_client = item.get('inWayToClient', 0)
                    stock.in_way_from_client = item.get('inWayFromClient', 0)
                    stock.updated_at = datetime.utcnow()
                    updated += 1
                else:
                    stock = ProductStock(
                        product_id=product.id,
                        warehouse_id=warehouse_id,
                        warehouse_name=warehouse_name,
                        quantity=item.get('quantity', 0),
                        quantity_full=item.get('quantityFull', 0),
                        in_way_to_client=item.get('inWayToClient', 0),
                        in_way_from_client=item.get('inWayFromClient', 0),
                    )
                    db.session.add(stock)
                    created += 1

            db.session.commit()

            logger.info(f"Warehouse refresh for seller {seller_id}: {len(wb_stocks)} items from API, {created} created, {updated} updated")

            return jsonify({
                'success': True,
                'message': f'Обновлено: {updated}, создано: {created} записей из {len(wb_stocks)} позиций API',
                'apiItems': len(wb_stocks),
                'created': created,
                'updated': updated,
            })

        except requests.exceptions.RequestException as e:
            logger.error(f"WB API error in warehouse refresh: {e}")
            return jsonify({'error': f'Ошибка WB API: {str(e)}'}), 502
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error in warehouse refresh: {e}")
            return jsonify({'error': str(e)}), 500

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
