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
    # If empty, create default WB
    if not marketplaces:
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
    """Browse categories for a marketplace."""
    marketplace = Marketplace.query.get_or_404(marketplace_id)
    search = request.args.get('search', '')
    
    query = MarketplaceCategory.query.filter_by(marketplace_id=marketplace_id)
    if search:
        query = query.filter(MarketplaceCategory.subject_name.ilike(f'%{search}%'))
        
    categories = query.order_by(MarketplaceCategory.subject_name).limit(500).all()
    
    return render_template('admin_marketplace_categories.html', marketplace=marketplace, categories=categories, search=search)


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
    """Generates a prompt for a category and tests it on a product."""
    data = request.json
    category_id = data.get('category_id')
    product_id = data.get('product_id')
    
    if not category_id or not product_id:
        return jsonify({"success": False, "error": "category_id and product_id are required"})
        
    category = MarketplaceCategory.query.get(category_id)
    product = SupplierProduct.query.get(product_id)
    
    if not category or not product:
        return jsonify({"success": False, "error": "Selected item not found"})
        
    characteristics = MarketplaceCategoryCharacteristic.query.filter_by(category_id=category_id, is_enabled=True).all()
    
    try:
        from seller_platform import app
        # Import config to initialize AI Client
        from services.ai_service import AIConfig
        supplier = product.supplier
        config = AIConfig(
            provider=supplier.ai_provider,
            model=supplier.ai_model,
            api_key=supplier.ai_api_key,
            base_url=supplier.ai_base_url,
            client_id=supplier.ai_client_id,
            client_secret=supplier.ai_client_secret
        )
        client = AIClient(config)
        
        task = MarketplaceAwareParsingTask(client=client, characteristics=characteristics)
        
        sys_prompt = task.get_system_prompt()
        product_info = product.get_all_data_for_parsing()
        user_prompt = task.build_user_prompt(product_info=product_info, original_data=product.get_original_data())
        
        # Test it by calling the LLM
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        raw_response = client.chat_completion(messages)
        parsed_result = task.parse_response(raw_response) if raw_response else None
        
        return jsonify({
            "success": True,
            "sys_prompt": sys_prompt,
            "user_prompt": user_prompt,
            "raw_response": raw_response,
            "parsed_result": parsed_result
        })
    except Exception as e:
        import traceback
        return jsonify({"success": False, "error": str(e), "traceback": traceback.format_exc()})

def register_marketplaces_routes(app):
    app.register_blueprint(marketplaces_bp)
