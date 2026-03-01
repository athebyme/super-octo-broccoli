# -*- coding: utf-8 -*-
"""
Marketplaces and integration routes
"""
import json
from flask import Blueprint, render_template, request, flash, redirect, url_for, jsonify, current_app
from flask_login import login_required, current_user

from models import db, Marketplace, MarketplaceCategory, MarketplaceCategoryCharacteristic, SupplierProduct
from services.marketplace_service import MarketplaceService
from services.marketplace_ai_parser import MarketplaceAwareParsingTask
from services.ai_service import AIClient

marketplaces_bp = Blueprint('marketplaces', __name__, url_prefix='/admin/marketplaces')

def admin_required(f):
    from seller_platform import admin_required as global_admin_required
    return global_admin_required(f)

# ==================== WEB UI ====================

@marketplaces_bp.route('/')
@login_required
@admin_required
def index():
    """List of all marketplaces and their sync status."""
    marketplaces = Marketplace.query.all()
    # Seed default WB marketplace if none exist (idempotent — only on first visit)
    if not marketplaces:
        existing_wb = Marketplace.query.filter_by(code='wb').first()
        if not existing_wb:
            wb = Marketplace(
                name="Wildberries",
                code="wb",
                api_base_url="https://content-api.wildberries.ru",
                api_version="v2"
            )
            db.session.add(wb)
            db.session.commit()
            marketplaces = [wb]

    return render_template('admin_marketplaces.html', marketplaces=marketplaces)


@marketplaces_bp.route('/<int:marketplace_id>/categories')
@login_required
@admin_required
def categories(marketplace_id):
    """Browse categories for a marketplace with hierarchy grouping."""
    from collections import OrderedDict
    marketplace = Marketplace.query.get_or_404(marketplace_id)
    search = request.args.get('search', '')

    query = MarketplaceCategory.query.filter_by(marketplace_id=marketplace_id)
    if search:
        query = query.filter(
            MarketplaceCategory.subject_name.ilike(f'%{search}%') |
            MarketplaceCategory.parent_name.ilike(f'%{search}%')
        )

    all_categories = query.order_by(
        MarketplaceCategory.parent_name,
        MarketplaceCategory.subject_name
    ).all()

    # Group by parent_name for tree-view
    grouped = OrderedDict()
    for cat in all_categories:
        parent = cat.parent_name or 'Без родителя'
        if parent not in grouped:
            grouped[parent] = []
        grouped[parent].append(cat)

    return render_template(
        'admin_marketplace_categories.html',
        marketplace=marketplace,
        grouped_categories=grouped,
        search=search,
        total_count=len(all_categories)
    )


@marketplaces_bp.route('/categories/<int:category_id>')
@login_required
@admin_required
def category_detail(category_id):
    """View and edit characteristics for a category."""
    category = MarketplaceCategory.query.get_or_404(category_id)
    characteristics = MarketplaceCategoryCharacteristic.query.filter_by(category_id=category_id).order_by(MarketplaceCategoryCharacteristic.required.desc(), MarketplaceCategoryCharacteristic.name).all()
    
    return render_template('admin_marketplace_category_detail.html', category=category, characteristics=characteristics)


@marketplaces_bp.route('/prompt_tester')
@login_required
@admin_required
def prompt_tester():
    """Interactive Playground to test Prompt Generation."""
    categories = MarketplaceCategory.query.filter((MarketplaceCategory.characteristics_count > 0)).order_by(MarketplaceCategory.subject_name).all()
    products = SupplierProduct.query.limit(20).all()
    
    return render_template('admin_prompt_tester.html', categories=categories, products=products)


# ==================== ACTIONS / API ====================

@marketplaces_bp.route('/<int:marketplace_id>/settings', methods=['POST'])
@login_required
@admin_required
def update_settings(marketplace_id):
    """Save marketplace settings (API key, etc.)."""
    marketplace = Marketplace.query.get_or_404(marketplace_id)

    api_key = request.form.get('api_key', '').strip()
    if api_key:
        marketplace.api_key = api_key
        db.session.commit()
        flash('API ключ сохранён.', 'success')
    else:
        flash('API ключ не может быть пустым.', 'warning')

    return redirect(url_for('marketplaces.index'))


@marketplaces_bp.route('/<int:marketplace_id>/sync_categories', methods=['POST'])
@login_required
@admin_required
def sync_categories(marketplace_id):
    """Sync categories hierarchy."""
    result = MarketplaceService.sync_categories(marketplace_id)
    if result.get('success'):
        flash(f"Категории успешно синхронизированы. Добавлено: {result.get('added')}, Обновлено: {result.get('updated')}", 'success')
    else:
        flash(f"Ошибка синхронизации: {result.get('error')}", 'danger')
    return redirect(url_for('marketplaces.index'))


@marketplaces_bp.route('/<int:marketplace_id>/sync_directories', methods=['POST'])
@login_required
@admin_required
def sync_directories(marketplace_id):
    """Sync base directories like colors, materials, etc."""
    result = MarketplaceService.sync_directories(marketplace_id)
    if result.get('success'):
        flash("Справочники успешно синхронизированы.", 'success')
    else:
        flash(f"Ошибка синхронизации: {result.get('error')}", 'danger')
    return redirect(url_for('marketplaces.index'))


@marketplaces_bp.route('/categories/<int:category_id>/sync_characteristics', methods=['POST'])
@login_required
@admin_required
def sync_characteristics(category_id):
    """Sync characteristics for specific category."""
    result = MarketplaceService.sync_category_characteristics(category_id)
    if result.get('success'):
        flash(f"Характеристики синхронизированы. Добавлено: {result.get('added')}, Обновлено: {result.get('updated')}", 'success')
    else:
        flash(f"Ошибка синхронизации: {result.get('error')}", 'danger')
    return redirect(url_for('marketplaces.category_detail', category_id=category_id))


@marketplaces_bp.route('/characteristics/<int:charc_id>/update', methods=['POST'])
@login_required
@admin_required
def update_characteristic(charc_id):
    """Update characteristic properties (is_enabled, custom_instruction)."""
    charc = MarketplaceCategoryCharacteristic.query.get_or_404(charc_id)
    
    data = request.json
    if 'is_enabled' in data:
        charc.is_enabled = bool(data['is_enabled'])
    if 'ai_instruction' in data:
        charc.ai_instruction = data['ai_instruction']
        
    db.session.commit()
    return jsonify({"success": True})


@marketplaces_bp.route('/api/test_prompt', methods=['POST'])
@login_required
@admin_required
def test_prompt():
    """Generates a prompt for a category and optionally tests it on a product via LLM."""
    data = request.json
    category_id = data.get('category_id')
    product_id = data.get('product_id')
    run_ai = data.get('run_ai', False)  # Only call LLM if explicitly requested

    if not category_id or not product_id:
        return jsonify({"success": False, "error": "category_id and product_id are required"})

    category = MarketplaceCategory.query.get(category_id)
    product = SupplierProduct.query.get(product_id)

    if not category or not product:
        return jsonify({"success": False, "error": "Selected item not found"})

    characteristics = MarketplaceCategoryCharacteristic.query.filter_by(
        category_id=category_id, is_enabled=True
    ).all()

    if not characteristics:
        return jsonify({"success": False, "error": "No enabled characteristics for this category. Sync characteristics first."})

    try:
        task = MarketplaceAwareParsingTask(client=None, characteristics=characteristics)

        sys_prompt = task.get_system_prompt()
        product_info = product.get_all_data_for_parsing()
        original_data = product.get_original_data()
        user_prompt = task.build_user_prompt(product_info=product_info, original_data=original_data)

        result = {
            "success": True,
            "sys_prompt": sys_prompt,
            "user_prompt": user_prompt,
            "characteristics_count": len(characteristics),
        }

        # Optionally run through AI
        if run_ai:
            from services.ai_service import AIConfig
            supplier = product.supplier

            if not supplier or not supplier.ai_enabled or not supplier.ai_api_key:
                return jsonify({"success": False, "error": "AI not configured for this supplier"})

            config = AIConfig.from_settings(supplier)
            if not config:
                return jsonify({"success": False, "error": "Failed to create AI config from supplier settings"})

            client = AIClient(config)
            task_with_client = MarketplaceAwareParsingTask(
                client=client, characteristics=characteristics
            )

            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt}
            ]
            raw_response = client.chat_completion(messages)
            parsed_result = task_with_client.parse_response(raw_response) if raw_response else None

            result["raw_response"] = raw_response
            result["parsed_result"] = parsed_result

        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({"success": False, "error": str(e), "traceback": traceback.format_exc()})

def register_marketplaces_routes(app):
    app.register_blueprint(marketplaces_bp)
