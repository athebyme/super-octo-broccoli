# -*- coding: utf-8 -*-
"""
Ценообразование — настройка формулы расчёта розничных цен из закупочных.

Восстановлено из routes/auto_import.py после удаления модуля автоимпорта.
"""
import ipaddress
import json
import logging
import socket
from datetime import datetime
from urllib.parse import urlparse

from flask import request, jsonify, flash, redirect, url_for, render_template
from flask_login import login_required, current_user

from models import db, PricingSettings, ImportedProduct, Product
from services.pricing_engine import (
    DEFAULT_PRICE_RANGES,
    calculate_price,
    SupplierPriceLoader,
    extract_supplier_product_id,
)


def _validate_supplier_url(url: str) -> str | None:
    """
    Валидация URL поставщика: защита от SSRF.
    Возвращает строку с ошибкой или None если URL безопасен.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        return 'URL должен начинаться с http:// или https://'

    hostname = parsed.hostname
    if not hostname:
        return 'URL не содержит hostname'

    try:
        resolved = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return f'Не удалось разрешить hostname: {hostname}'

    for _family, _type, _proto, _canonname, sockaddr in resolved:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return 'URL не должен указывать на внутренние/приватные адреса'

    return None

logger = logging.getLogger(__name__)


def register_pricing_routes(app):
    """Регистрирует routes ценообразования."""

    @app.route('/pricing', methods=['GET', 'POST'])
    @login_required
    def auto_import_pricing():
        """Настройки формулы ценообразования."""
        if not current_user.seller:
            flash('Нет профиля продавца', 'warning')
            return redirect(url_for('dashboard'))

        seller = current_user.seller
        pricing = PricingSettings.query.filter_by(seller_id=seller.id).first()

        if request.method == 'POST':
            if not pricing:
                pricing = PricingSettings(seller_id=seller.id)
                db.session.add(pricing)

            pricing.is_enabled = 'is_enabled' in request.form
            pricing.formula_type = request.form.get('formula_type', 'standard')

            raw_price_url = request.form.get('supplier_price_url', '').strip() or None
            raw_inf_url = request.form.get('supplier_price_inf_url', '').strip() or None

            for label, url_val in [('URL цен', raw_price_url), ('URL INF', raw_inf_url)]:
                if url_val:
                    err = _validate_supplier_url(url_val)
                    if err:
                        flash(f'{label}: {err}', 'danger')
                        return redirect(url_for('auto_import_pricing'))

            pricing.supplier_price_url = raw_price_url
            pricing.supplier_price_inf_url = raw_inf_url

            float_fields = [
                'wb_commission_pct', 'tax_rate', 'logistics_cost', 'storage_cost',
                'packaging_cost', 'acquiring_cost', 'extra_cost', 'delivery_pct',
                'delivery_min', 'delivery_max', 'min_profit', 'max_profit',
                'spp_pct', 'spp_min', 'spp_max', 'inflated_multiplier',
            ]
            for field in float_fields:
                val = request.form.get(field, '').strip()
                if val:
                    try:
                        setattr(pricing, field, float(val))
                    except ValueError:
                        pass
                elif field == 'max_profit':
                    pricing.max_profit = None

            pricing.profit_column = request.form.get('profit_column', 'd').lower()
            pricing.use_random = 'use_random' in request.form

            for field in ['random_min', 'random_max']:
                val = request.form.get(field, '').strip()
                if val:
                    try:
                        setattr(pricing, field, int(val))
                    except ValueError:
                        pass

            ranges_json = request.form.get('price_ranges', '').strip()
            if ranges_json:
                try:
                    parsed = json.loads(ranges_json)
                    if isinstance(parsed, list):
                        pricing.price_ranges = json.dumps(parsed, ensure_ascii=False)
                except json.JSONDecodeError:
                    flash('Ошибка формата таблицы наценок (невалидный JSON)', 'danger')

            db.session.commit()
            flash('Настройки ценообразования сохранены', 'success')
            return redirect(url_for('auto_import_pricing'))

        ranges = []
        if pricing and pricing.price_ranges:
            try:
                ranges = json.loads(pricing.price_ranges)
            except json.JSONDecodeError:
                ranges = DEFAULT_PRICE_RANGES
        else:
            ranges = DEFAULT_PRICE_RANGES

        return render_template(
            'pricing_settings.html',
            pricing=pricing,
            ranges=ranges,
            default_ranges=DEFAULT_PRICE_RANGES,
        )

    @app.route('/api/pricing/sync-prices', methods=['POST'])
    @login_required
    def api_sync_supplier_prices():
        """Синхронизировать цены поставщика из CSV."""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Нет профиля продавца'}), 400

        seller = current_user.seller
        pricing = PricingSettings.query.filter_by(seller_id=seller.id).first()

        if not pricing or not pricing.supplier_price_url:
            return jsonify({'success': False, 'error': 'Не настроен URL файла цен'}), 400

        # Повторная валидация перед запросом (URL мог быть сохранён до внедрения проверки)
        for url_val in [pricing.supplier_price_url, pricing.supplier_price_inf_url]:
            if url_val:
                err = _validate_supplier_url(url_val)
                if err:
                    return jsonify({'success': False, 'error': f'URL не прошёл проверку безопасности: {err}'}), 400

        try:
            loader = SupplierPriceLoader(
                price_url=pricing.supplier_price_url,
                inf_url=pricing.supplier_price_inf_url,
            )
            prices = loader.load_prices()

            updated_imported = 0
            updated_products = 0
            now = datetime.utcnow()

            for ip in ImportedProduct.query.filter_by(seller_id=seller.id).all():
                supplier_pid = extract_supplier_product_id(ip.external_id)
                if supplier_pid and supplier_pid in prices:
                    new_price = prices[supplier_pid]['price']
                    new_qty = prices[supplier_pid].get('quantity', 0)
                    price_changed = ip.supplier_price != new_price
                    qty_changed = ip.supplier_quantity != new_qty
                    if price_changed or qty_changed:
                        ip.supplier_price = new_price
                        ip.supplier_quantity = new_qty
                        if price_changed:
                            result = calculate_price(new_price, pricing, product_id=supplier_pid)
                            if result:
                                ip.calculated_price = result['final_price']
                                ip.calculated_discount_price = result['discount_price']
                                ip.calculated_price_before_discount = result['price_before_discount']
                        updated_imported += 1
                elif supplier_pid:
                    if ip.supplier_quantity != 0:
                        ip.supplier_quantity = 0
                        updated_imported += 1

            for p in Product.query.filter_by(seller_id=seller.id).all():
                supplier_pid = extract_supplier_product_id(p.vendor_code)
                if supplier_pid and supplier_pid in prices:
                    new_price = prices[supplier_pid]['price']
                    if p.supplier_price != new_price:
                        p.supplier_price = new_price
                        p.supplier_price_updated_at = now
                        updated_products += 1

            pricing.last_price_sync_at = now
            db.session.commit()

            return jsonify({
                'success': True,
                'total_prices': len(prices),
                'updated_imported': updated_imported,
                'updated_products': updated_products,
            })

        except Exception as e:
            logger.error(f"Ошибка синхронизации цен поставщика: {e}", exc_info=True)
            return jsonify({'success': False, 'error': 'Ошибка загрузки цен. Проверьте URL и попробуйте позже.'}), 500

    @app.route('/api/pricing/recalculate', methods=['POST'])
    @login_required
    def api_recalculate_prices():
        """Пересчитать все розничные цены по текущей формуле."""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Нет профиля продавца'}), 400

        seller = current_user.seller
        pricing = PricingSettings.query.filter_by(seller_id=seller.id).first()
        if not pricing or not pricing.is_enabled:
            return jsonify({'success': False, 'error': 'Ценообразование не настроено'}), 400

        recalculated = 0
        for ip in ImportedProduct.query.filter(
            ImportedProduct.seller_id == seller.id,
            ImportedProduct.supplier_price.isnot(None),
            ImportedProduct.supplier_price > 0,
        ).all():
            supplier_pid = extract_supplier_product_id(ip.external_id) or ip.id
            result = calculate_price(ip.supplier_price, pricing, product_id=supplier_pid)
            if result:
                ip.calculated_price = result['final_price']
                ip.calculated_discount_price = result['discount_price']
                ip.calculated_price_before_discount = result['price_before_discount']
                recalculated += 1

        db.session.commit()
        return jsonify({'success': True, 'recalculated': recalculated})

    @app.route('/api/pricing/calculate-preview', methods=['POST'])
    @login_required
    def api_pricing_preview():
        """Предпросмотр расчёта цены для заданной закупочной цены."""
        if not current_user.seller:
            return jsonify({'success': False, 'error': 'Нет профиля продавца'}), 400

        data = request.get_json()
        if not data or 'purchase_price' not in data:
            return jsonify({'success': False, 'error': 'Не указана закупочная цена'}), 400

        purchase_price = float(data['purchase_price'])
        seller = current_user.seller
        pricing = PricingSettings.query.filter_by(seller_id=seller.id).first()

        result = calculate_price(purchase_price, pricing or {}, product_id=0, force_random=True)
        if not result:
            return jsonify({'success': False, 'error': 'Не удалось рассчитать цену'}), 400

        return jsonify({'success': True, 'result': result})
