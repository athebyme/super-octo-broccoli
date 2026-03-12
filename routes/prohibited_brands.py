# -*- coding: utf-8 -*-
"""
Роуты для управления запрещёнными брендами по маркетплейсам.
"""
import logging
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user

from models import db, ProhibitedBrand
from services.prohibited_brands_service import (
    add_prohibited_brand,
    remove_prohibited_brand,
    bulk_add_brands,
    invalidate_cache,
    seed_default_brands,
)

logger = logging.getLogger(__name__)

prohibited_brands_bp = Blueprint('prohibited_brands', __name__)

MARKETPLACE_LABELS = {
    'wb': 'Wildberries',
    'ozon': 'Ozon',
    'sber': 'СберМегаМаркет',
    'all': 'Все маркетплейсы',
}


@prohibited_brands_bp.route('/admin/prohibited-brands')
@login_required
def admin_index():
    """Список запрещённых брендов."""
    if not current_user.is_admin:
        flash('Доступ запрещён', 'error')
        return redirect(url_for('index'))

    marketplace_filter = request.args.get('marketplace', '')
    search = request.args.get('search', '')

    query = ProhibitedBrand.query

    if marketplace_filter:
        query = query.filter_by(marketplace=marketplace_filter)
    if search:
        query = query.filter(ProhibitedBrand.brand_name.ilike(f'%{search}%'))

    brands = query.order_by(ProhibitedBrand.marketplace, ProhibitedBrand.brand_name).all()

    # Статистика по маркетплейсам
    stats = {}
    for mp_code in ['wb', 'ozon', 'sber']:
        stats[mp_code] = ProhibitedBrand.query.filter_by(
            marketplace=mp_code, is_active=True
        ).count()

    return render_template(
        'admin_prohibited_brands.html',
        brands=brands,
        stats=stats,
        marketplace_filter=marketplace_filter,
        search=search,
        marketplace_labels=MARKETPLACE_LABELS,
    )


@prohibited_brands_bp.route('/admin/prohibited-brands/add', methods=['POST'])
@login_required
def admin_add():
    """Добавить запрещённый бренд."""
    if not current_user.is_admin:
        return jsonify({'error': 'Доступ запрещён'}), 403

    brand_name = request.form.get('brand_name', '').strip()
    marketplace = request.form.get('marketplace', '').strip().lower()
    reason = request.form.get('reason', '').strip()

    if not brand_name or not marketplace:
        flash('Укажите бренд и маркетплейс', 'error')
        return redirect(url_for('prohibited_brands.admin_index'))

    ok, msg = add_prohibited_brand(brand_name, marketplace, reason or None)
    flash(msg, 'success' if ok else 'warning')
    return redirect(url_for('prohibited_brands.admin_index'))


@prohibited_brands_bp.route('/admin/prohibited-brands/bulk-add', methods=['POST'])
@login_required
def admin_bulk_add():
    """Массовое добавление запрещённых брендов."""
    if not current_user.is_admin:
        return jsonify({'error': 'Доступ запрещён'}), 403

    brands_text = request.form.get('brands_text', '').strip()
    marketplace = request.form.get('marketplace', '').strip().lower()
    reason = request.form.get('reason', '').strip()

    if not brands_text or not marketplace:
        flash('Укажите список брендов и маркетплейс', 'error')
        return redirect(url_for('prohibited_brands.admin_index'))

    added, skipped = bulk_add_brands(brands_text, marketplace, reason or None)
    flash(f'Добавлено: {added}, пропущено (уже есть): {skipped}', 'success')
    return redirect(url_for('prohibited_brands.admin_index'))


@prohibited_brands_bp.route('/admin/prohibited-brands/<int:brand_id>/delete', methods=['POST'])
@login_required
def admin_delete(brand_id):
    """Удалить запрещённый бренд."""
    if not current_user.is_admin:
        return jsonify({'error': 'Доступ запрещён'}), 403

    ok, msg = remove_prohibited_brand(brand_id)
    flash(msg, 'success' if ok else 'error')
    return redirect(url_for('prohibited_brands.admin_index'))


@prohibited_brands_bp.route('/admin/prohibited-brands/<int:brand_id>/toggle', methods=['POST'])
@login_required
def admin_toggle(brand_id):
    """Включить/выключить запрещённый бренд."""
    if not current_user.is_admin:
        return jsonify({'error': 'Доступ запрещён'}), 403

    pb = ProhibitedBrand.query.get_or_404(brand_id)
    pb.is_active = not pb.is_active
    db.session.commit()
    invalidate_cache()

    status = 'активирован' if pb.is_active else 'деактивирован'
    flash(f'Бренд "{pb.brand_name}" {status}', 'success')
    return redirect(url_for('prohibited_brands.admin_index'))


@prohibited_brands_bp.route('/admin/prohibited-brands/seed', methods=['POST'])
@login_required
def admin_seed():
    """Заполнить начальными данными."""
    if not current_user.is_admin:
        return jsonify({'error': 'Доступ запрещён'}), 403

    count = seed_default_brands()
    if count:
        flash(f'Добавлено {count} запрещённых брендов', 'success')
    else:
        flash('Запрещённые бренды уже заполнены', 'info')
    return redirect(url_for('prohibited_brands.admin_index'))


@prohibited_brands_bp.route('/api/prohibited-brands/check')
@login_required
def api_check():
    """API: проверить бренд."""
    brand = request.args.get('brand', '')
    marketplace = request.args.get('marketplace', '')

    from services.prohibited_brands_service import is_brand_prohibited, get_prohibited_marketplaces
    if marketplace:
        prohibited = is_brand_prohibited(brand, marketplace)
        return jsonify({'brand': brand, 'marketplace': marketplace, 'prohibited': prohibited})
    else:
        marketplaces = get_prohibited_marketplaces(brand)
        return jsonify({'brand': brand, 'prohibited_on': marketplaces})


def register_prohibited_brands_routes(app):
    """Регистрирует blueprint."""
    app.register_blueprint(prohibited_brands_bp)
