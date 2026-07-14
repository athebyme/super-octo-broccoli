# -*- coding: utf-8 -*-
"""
Marketplaces and integration routes
"""
import json
from datetime import datetime, timedelta
from functools import wraps
from flask import Blueprint, render_template, request, flash, redirect, url_for, jsonify, current_app, abort
from flask_login import login_required, current_user
from sqlalchemy.orm import joinedload

from models import (
    db,
    Marketplace,
    MarketplaceAttributeDefinition,
    MarketplaceAttributeValue,
    MarketplaceCategory,
    MarketplaceCategoryCharacteristic,
    MarketplaceProductType,
    MarketplaceReferenceAccount,
    MarketplaceTaxonomyCategory,
    SupplierProduct,
)
from services.marketplace_service import MarketplaceService
from services.marketplace_ai_parser import MarketplaceAwareParsingTask
from services.marketplace_reference_accounts import (
    MarketplaceReferenceAccountError,
    MarketplaceReferenceAccountService,
)
from services.ozon_reference_service import (
    OzonReferenceService,
    OzonReferenceValidationError,
)
from services.ai_service import AIClient

marketplaces_bp = Blueprint('marketplaces', __name__, url_prefix='/admin/marketplaces')


def _legacy_wb_only(marketplace):
    """Guard WB-shaped reference models/routes from other adapters."""
    if marketplace and marketplace.code == 'wb':
        return None
    return jsonify({
        'success': False,
        'error': 'Этот экран пока поддерживает только WB reference data',
    }), 409


def _ozon_feature_enabled():
    return bool(current_app.config.get('MARKETPLACE_OZON_ENABLED', False))


def _require_ozon_feature():
    if not _ozon_feature_enabled():
        abort(404)


def _ozon_marketplace_or_404(marketplace_id):
    return Marketplace.query.filter_by(
        id=marketplace_id,
        code='ozon',
        is_active=True,
    ).first_or_404()


def _ozon_product_type_or_404(product_type_id):
    product_type = MarketplaceProductType.query.filter_by(
        id=product_type_id,
    ).first_or_404()
    if product_type.marketplace.code != 'ozon':
        abort(404)
    return product_type


def _ozon_attribute_or_404(attribute_id):
    attribute = MarketplaceAttributeDefinition.query.filter_by(
        id=attribute_id,
    ).first_or_404()
    if attribute.marketplace.code != 'ozon':
        abort(404)
    return attribute


def _positive_page(raw_value):
    try:
        page = int(raw_value)
    except (TypeError, ValueError):
        return 1
    return page if page > 0 else 1


def _like_pattern(value):
    return '%' + value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'

def admin_required(f):
    """Keep the blueprint importable when seller_platform.py is __main__."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('У вас нет прав для доступа к этой странице', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

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

    reference_accounts = {
        account.marketplace_id: account
        for account in MarketplaceReferenceAccount.query.filter(
            MarketplaceReferenceAccount.marketplace_id.in_(
                [marketplace.id for marketplace in marketplaces]
            )
        ).all()
    } if marketplaces else {}

    return render_template(
        'admin_marketplaces.html',
        marketplaces=marketplaces,
        reference_accounts=reference_accounts,
        ozon_enabled=_ozon_feature_enabled(),
    )


@marketplaces_bp.route('/<int:marketplace_id>/categories')
@login_required
@admin_required
def categories(marketplace_id):
    """Browse categories for a marketplace with hierarchy grouping."""
    from collections import OrderedDict
    marketplace = Marketplace.query.get_or_404(marketplace_id)
    unsupported = _legacy_wb_only(marketplace)
    if unsupported:
        return unsupported
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

    enabled_count = MarketplaceCategory.query.filter_by(
        marketplace_id=marketplace_id, is_enabled=True, is_available=True,
    ).count()

    return render_template(
        'admin_marketplace_categories.html',
        marketplace=marketplace,
        grouped_categories=grouped,
        search=search,
        total_count=len(all_categories),
        enabled_count=enabled_count
    )


@marketplaces_bp.route('/categories/<int:category_id>')
@login_required
@admin_required
def category_detail(category_id):
    """View and edit characteristics for a category."""
    category = MarketplaceCategory.query.get_or_404(category_id)
    unsupported = _legacy_wb_only(category.marketplace)
    if unsupported:
        return unsupported
    characteristics = MarketplaceCategoryCharacteristic.query.filter_by(category_id=category_id).order_by(MarketplaceCategoryCharacteristic.required.desc(), MarketplaceCategoryCharacteristic.name).all()
    characteristic_allowlists = {
        str(charc.id): MarketplaceService.characteristic_allowlist_values(
            charc.dictionary_json,
        )
        for charc in characteristics
    }

    return render_template(
        'admin_marketplace_category_detail.html',
        category=category,
        characteristics=characteristics,
        characteristic_allowlists=characteristic_allowlists,
    )


@marketplaces_bp.route('/<int:marketplace_id>/ozon/reference-account', methods=['POST'])
@login_required
@admin_required
def save_ozon_reference_account(marketplace_id):
    """Save the one explicit global credential used only for Ozon references."""
    _require_ozon_feature()
    _ozon_marketplace_or_404(marketplace_id)
    try:
        MarketplaceReferenceAccountService.save(
            marketplace_id=marketplace_id,
            external_account_id=request.form.get('external_account_id'),
            api_key=request.form.get('api_key'),
        )
    except MarketplaceReferenceAccountError as exc:
        flash(str(exc), 'danger')
    except Exception:
        current_app.logger.exception(
            'Failed to save Ozon reference account marketplace_id=%s',
            marketplace_id,
        )
        flash('Не удалось сохранить Ozon reference account', 'danger')
    else:
        flash('Ozon reference account сохранён. Выполните проверку подключения.', 'success')
    return redirect(url_for('marketplaces.index'))


@marketplaces_bp.route('/<int:marketplace_id>/ozon/reference-account/check', methods=['POST'])
@login_required
@admin_required
def check_ozon_reference_account(marketplace_id):
    _require_ozon_feature()
    _ozon_marketplace_or_404(marketplace_id)
    try:
        checked, result = MarketplaceReferenceAccountService.check(
            marketplace_id=marketplace_id,
        )
    except MarketplaceReferenceAccountError as exc:
        flash(str(exc), 'danger')
    except Exception:
        current_app.logger.exception(
            'Failed to check Ozon reference account marketplace_id=%s',
            marketplace_id,
        )
        flash('Не удалось проверить Ozon reference account', 'danger')
    else:
        if result.ok:
            flash('Подключение reference account к Ozon подтверждено.', 'success')
        else:
            flash(
                checked.last_error_message or 'Ozon отклонил проверку подключения',
                'danger',
            )
    return redirect(url_for('marketplaces.index'))


@marketplaces_bp.route('/<int:marketplace_id>/ozon/reference-account/disconnect', methods=['POST'])
@login_required
@admin_required
def disconnect_ozon_reference_account(marketplace_id):
    # Secret removal remains available during a feature rollback.
    _ozon_marketplace_or_404(marketplace_id)
    try:
        MarketplaceReferenceAccountService.disconnect(
            marketplace_id=marketplace_id,
        )
    except MarketplaceReferenceAccountError as exc:
        flash(str(exc), 'danger')
    except Exception:
        current_app.logger.exception(
            'Failed to disconnect Ozon reference account marketplace_id=%s',
            marketplace_id,
        )
        flash('Не удалось удалить Ozon reference credential', 'danger')
    else:
        flash('Ozon reference credential удалён.', 'success')
    return redirect(url_for('marketplaces.index'))


@marketplaces_bp.route('/<int:marketplace_id>/ozon/sync-tree', methods=['POST'])
@login_required
@admin_required
def sync_ozon_tree(marketplace_id):
    _require_ozon_feature()
    _ozon_marketplace_or_404(marketplace_id)
    result = OzonReferenceService.sync_tree(marketplace_id)
    if result.get('success'):
        flash(
            'Справочник Ozon синхронизирован: '
            f"{result.get('available_categories', 0)} категорий, "
            f"{result.get('available_types', 0)} типов.",
            'success',
        )
    else:
        flash(result.get('error') or 'Не удалось синхронизировать Ozon', 'danger')
    return redirect(url_for('marketplaces.index'))


@marketplaces_bp.route('/<int:marketplace_id>/ozon/categories')
@login_required
@admin_required
def ozon_categories(marketplace_id):
    _require_ozon_feature()
    marketplace = _ozon_marketplace_or_404(marketplace_id)
    search = str(request.args.get('search', '') or '').strip()[:200]
    page = _positive_page(request.args.get('page', '1'))
    per_page = 100
    query = MarketplaceProductType.query.options(
        joinedload(MarketplaceProductType.category),
    ).join(
        MarketplaceTaxonomyCategory,
        MarketplaceProductType.category_id == MarketplaceTaxonomyCategory.id,
    ).filter(
        MarketplaceProductType.marketplace_id == marketplace.id,
    )
    if search:
        pattern = _like_pattern(search)
        query = query.filter(
            MarketplaceProductType.name.ilike(pattern, escape='\\')
            | MarketplaceProductType.external_type_id.ilike(pattern, escape='\\')
            | MarketplaceTaxonomyCategory.full_path.ilike(pattern, escape='\\')
            | MarketplaceTaxonomyCategory.external_category_id.ilike(
                pattern,
                escape='\\',
            )
        )
    total = query.count()
    product_types = query.order_by(
        MarketplaceTaxonomyCategory.full_path.asc(),
        MarketplaceProductType.name.asc(),
        MarketplaceProductType.id.asc(),
    ).offset((page - 1) * per_page).limit(per_page).all()
    enabled_count = MarketplaceProductType.query.filter_by(
        marketplace_id=marketplace.id,
        is_enabled=True,
        is_available=True,
    ).count()
    return render_template(
        'admin_ozon_categories.html',
        marketplace=marketplace,
        product_types=product_types,
        search=search,
        page=page,
        per_page=per_page,
        total=total,
        enabled_count=enabled_count,
    )


@marketplaces_bp.route('/ozon/types/<int:product_type_id>')
@login_required
@admin_required
def ozon_type_detail(product_type_id):
    _require_ozon_feature()
    product_type = _ozon_product_type_or_404(product_type_id)
    attributes = MarketplaceAttributeDefinition.query.filter_by(
        product_type_id=product_type.id,
    ).order_by(
        MarketplaceAttributeDefinition.is_required.desc(),
        MarketplaceAttributeDefinition.group_name.asc(),
        MarketplaceAttributeDefinition.sort_order.asc(),
        MarketplaceAttributeDefinition.id.asc(),
    ).all()
    return render_template(
        'admin_ozon_type_detail.html',
        product_type=product_type,
        attributes=attributes,
        schema_fresh=OzonReferenceService.reference_is_fresh(product_type),
    )


@marketplaces_bp.route('/ozon/types/<int:product_type_id>/sync', methods=['POST'])
@login_required
@admin_required
def sync_ozon_type_attributes(product_type_id):
    _require_ozon_feature()
    _ozon_product_type_or_404(product_type_id)
    result = OzonReferenceService.sync_attributes(product_type_id)
    if result.get('success'):
        flash(
            f"Схема Ozon обновлена: {result.get('total', 0)} атрибутов.",
            'success',
        )
    else:
        flash(result.get('error') or 'Не удалось обновить схему Ozon', 'danger')
    return redirect(
        url_for('marketplaces.ozon_type_detail', product_type_id=product_type_id)
    )


@marketplaces_bp.route('/ozon/types/<int:product_type_id>/toggle', methods=['POST'])
@login_required
@admin_required
def toggle_ozon_type(product_type_id):
    _require_ozon_feature()
    _ozon_product_type_or_404(product_type_id)
    payload = request.get_json(silent=True)
    if (
        not isinstance(payload, dict)
        or set(payload) != {'is_enabled'}
        or not isinstance(payload.get('is_enabled'), bool)
    ):
        return jsonify({
            'success': False,
            'error': 'Ожидается только boolean is_enabled',
        }), 400
    result = OzonReferenceService.set_product_type_enabled(
        product_type_id,
        payload['is_enabled'],
    )
    return jsonify(result), (200 if result.get('success') else 409)


@marketplaces_bp.route('/ozon/attributes/<int:attribute_id>')
@login_required
@admin_required
def ozon_attribute_detail(attribute_id):
    _require_ozon_feature()
    attribute = _ozon_attribute_or_404(attribute_id)
    search = str(request.args.get('search', '') or '').strip()[:200]
    page = _positive_page(request.args.get('page', '1'))
    per_page = 100
    values_query = MarketplaceAttributeValue.query.filter_by(
        attribute_id=attribute.id,
        is_available=True,
    )
    if search:
        pattern = _like_pattern(search)
        values_query = values_query.filter(
            MarketplaceAttributeValue.value.ilike(pattern, escape='\\')
            | MarketplaceAttributeValue.external_value_id.ilike(
                pattern,
                escape='\\',
            )
        )
    values_total = values_query.count()
    values = values_query.order_by(
        MarketplaceAttributeValue.value_normalized.asc(),
        MarketplaceAttributeValue.external_value_id.asc(),
    ).offset((page - 1) * per_page).limit(per_page).all()

    restriction_ids = attribute.restriction_value_ids
    available_restriction_ids = set()
    for offset in range(0, len(restriction_ids), 500):
        chunk = restriction_ids[offset:offset + 500]
        available_restriction_ids.update(
            row.external_value_id
            for row in MarketplaceAttributeValue.query.filter(
                MarketplaceAttributeValue.attribute_id == attribute.id,
                MarketplaceAttributeValue.is_available.is_(True),
                MarketplaceAttributeValue.external_value_id.in_(chunk),
            ).all()
        )
    return render_template(
        'admin_ozon_attribute_detail.html',
        attribute=attribute,
        values=values,
        values_total=values_total,
        search=search,
        page=page,
        per_page=per_page,
        restriction_ids=restriction_ids,
        invalid_restriction_ids=sorted(
            set(restriction_ids) - available_restriction_ids,
            key=lambda value: (
                (0, int(value)) if value.isdigit() else (1, value)
            ),
        ),
        dictionary_fresh=OzonReferenceService.dictionary_is_fresh(attribute),
    )


@marketplaces_bp.route('/ozon/attributes/<int:attribute_id>/sync-values', methods=['POST'])
@login_required
@admin_required
def sync_ozon_attribute_values(attribute_id):
    _require_ozon_feature()
    _ozon_attribute_or_404(attribute_id)
    result = OzonReferenceService.sync_attribute_values(attribute_id)
    if result.get('success'):
        flash(
            f"Словарь Ozon обновлён: {result.get('total', 0)} значений.",
            'success',
        )
    else:
        flash(result.get('error') or 'Не удалось обновить словарь Ozon', 'danger')
    return redirect(
        url_for('marketplaces.ozon_attribute_detail', attribute_id=attribute_id)
    )


@marketplaces_bp.route('/ozon/attributes/<int:attribute_id>/update', methods=['POST'])
@login_required
@admin_required
def update_ozon_attribute(attribute_id):
    _require_ozon_feature()
    _ozon_attribute_or_404(attribute_id)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'success': False, 'error': 'Ожидается JSON-объект'}), 400
    allowed = {'is_enabled', 'ai_instruction', 'restriction_value_ids'}
    if len(payload) != 1 or not set(payload).issubset(allowed):
        return jsonify({
            'success': False,
            'error': 'Изменяйте ровно одну настройку Ozon за запрос',
        }), 400
    try:
        result = OzonReferenceService.update_attribute_configuration(
            attribute_id,
            **payload,
        )
    except OzonReferenceValidationError as exc:
        status = 409 if 'cannot be disabled' in str(exc) else 400
        return jsonify({'success': False, 'error': str(exc)}), status
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            'Failed to update Ozon attribute configuration attribute_id=%s',
            attribute_id,
        )
        return jsonify({
            'success': False,
            'error': 'Не удалось сохранить настройку атрибута Ozon',
        }), 500
    return jsonify(result)


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
    unsupported = _legacy_wb_only(marketplace)
    if unsupported:
        return unsupported

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
    marketplace = Marketplace.query.get_or_404(marketplace_id)
    unsupported = _legacy_wb_only(marketplace)
    if unsupported:
        return unsupported
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
    marketplace = Marketplace.query.get_or_404(marketplace_id)
    unsupported = _legacy_wb_only(marketplace)
    if unsupported:
        return unsupported
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
    category = MarketplaceCategory.query.get_or_404(category_id)
    unsupported = _legacy_wb_only(category.marketplace)
    if unsupported:
        return unsupported
    result = MarketplaceService.sync_category_characteristics(category_id)
    if result.get('success'):
        flash(f"Характеристики синхронизированы. Добавлено: {result.get('added')}, Обновлено: {result.get('updated')}", 'success')
    else:
        flash(f"Ошибка синхронизации: {result.get('error')}", 'danger')
    return redirect(url_for('marketplaces.category_detail', category_id=category_id))


@marketplaces_bp.route('/categories/<int:category_id>/toggle', methods=['POST'])
@login_required
@admin_required
def toggle_category(category_id):
    """Toggle is_enabled for a single category. Auto-syncs characteristics if enabling and none exist."""
    category = MarketplaceCategory.query.get_or_404(category_id)
    unsupported = _legacy_wb_only(category.marketplace)
    if unsupported:
        return unsupported
    data = request.json
    new_state = bool(data.get('is_enabled', not category.is_enabled))
    if new_state and not category.is_available:
        return jsonify({
            "success": False,
            "error": "Категория больше недоступна в актуальном справочнике WB",
        }), 409
    category.is_enabled = new_state
    db.session.commit()

    synced = False
    sync_error = None
    schema_stale = (
        not category.characteristics_synced_at
        or category.characteristics_synced_at < datetime.utcnow() - timedelta(hours=48)
        or category.characteristics_sync_status != 'success'
    )
    if new_state and schema_stale:
        result = MarketplaceService.sync_category_characteristics(category_id)
        if result.get('success'):
            synced = True
        else:
            sync_error = result.get('error')

    return jsonify({
        "success": True,
        "is_enabled": category.is_enabled,
        "synced": synced,
        "sync_error": sync_error,
        "characteristics_count": category.characteristics_count or 0,
    })


@marketplaces_bp.route('/<int:marketplace_id>/categories/toggle_group', methods=['POST'])
@login_required
@admin_required
def toggle_category_group(marketplace_id):
    """Toggle is_enabled for all categories in a parent group."""
    marketplace = Marketplace.query.get_or_404(marketplace_id)
    unsupported = _legacy_wb_only(marketplace)
    if unsupported:
        return unsupported
    data = request.json
    parent_name = data.get('parent_name')
    is_enabled = bool(data.get('is_enabled', True))

    if parent_name is None:
        return jsonify({"success": False, "error": "parent_name required"}), 400

    query = MarketplaceCategory.query.filter_by(marketplace_id=marketplace_id)
    if is_enabled:
        query = query.filter(MarketplaceCategory.is_available.is_(True))
    if parent_name == '__none__':
        query = query.filter(MarketplaceCategory.parent_name.is_(None))
    else:
        query = query.filter_by(parent_name=parent_name)

    count = query.update({MarketplaceCategory.is_enabled: is_enabled}, synchronize_session='fetch')
    db.session.commit()
    return jsonify({"success": True, "updated": count, "is_enabled": is_enabled})


@marketplaces_bp.route('/<int:marketplace_id>/categories/sync_enabled', methods=['POST'])
@login_required
@admin_required
def sync_enabled_categories(marketplace_id):
    """Refresh a bounded batch of stale enabled category schemas."""
    marketplace = Marketplace.query.get_or_404(marketplace_id)
    unsupported = _legacy_wb_only(marketplace)
    if unsupported:
        return unsupported
    payload = request.get_json(silent=True) or {}
    try:
        limit = int(payload.get('limit', 50))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "limit должен быть числом"}), 400
    result = MarketplaceService.sync_stale_characteristics(
        marketplace_id,
        limit=limit,
    )
    return jsonify(result), (200 if result.get('success') else 207)


@marketplaces_bp.route('/characteristics/<int:charc_id>/update', methods=['POST'])
@login_required
@admin_required
def update_characteristic(charc_id):
    """Update characteristic properties and its manual WB allowlist."""
    charc = MarketplaceCategoryCharacteristic.query.get_or_404(charc_id)
    unsupported = _legacy_wb_only(charc.category.marketplace)
    if unsupported:
        return unsupported

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({
            "success": False,
            "error": "Ожидается JSON-объект",
        }), 400

    if 'dictionary_values' in data:
        if len(data) != 1:
            return jsonify({
                "success": False,
                "error": "Словарь сохраняется отдельным запросом",
            }), 400
        try:
            result = MarketplaceService.save_characteristic_allowlist(
                charc.id,
                data['dictionary_values'],
            )
        except LookupError as exc:
            return jsonify({"success": False, "error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception:
            current_app.logger.exception(
                'Failed to save WB characteristic allowlist charc_id=%s',
                charc.id,
            )
            return jsonify({
                "success": False,
                "error": "Не удалось сохранить словарь",
            }), 500
        return jsonify(result)

    if 'is_enabled' in data:
        if charc.required and not bool(data['is_enabled']):
            return jsonify({
                "success": False,
                "error": "Обязательную характеристику WB нельзя отключить",
            }), 409
        charc.is_enabled = bool(data['is_enabled'])
    if 'ai_instruction' in data:
        charc.ai_instruction = data['ai_instruction']
        charc.ai_instruction_source = (
            'custom' if str(data['ai_instruction'] or '').strip() else 'generated'
        )
        
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
        category_id=category_id, is_enabled=True, is_available=True,
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
