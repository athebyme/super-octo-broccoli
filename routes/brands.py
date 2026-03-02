# -*- coding: utf-8 -*-
"""
Routes: Brand Registry — управление брендами в админке.

Blueprint 'brands' с маршрутами для:
- Список брендов с фильтрами
- Карточка бренда (aliases, категории, товары)
- Review queue для pending/needs_review брендов
- API эндпоинты для фронтенда (resolve, search, stats)
"""
import logging
from datetime import datetime

from flask import (
    Blueprint, render_template, request, jsonify, flash, redirect, url_for
)
from flask_login import login_required, current_user

from models import db, Brand, BrandAlias, BrandCategoryLink, MarketplaceBrand, ImportedProduct, SupplierProduct, Marketplace

logger = logging.getLogger(__name__)

brands_bp = Blueprint('brands', __name__, url_prefix='/admin/brands')


def admin_required(f):
    """Декоратор для проверки прав администратора."""
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('Доступ запрещён', 'error')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated


# ------------------------------------------------------------------
# Страницы
# ------------------------------------------------------------------

@brands_bp.route('/')
@login_required
@admin_required
def index():
    """Список брендов с фильтрами. Поддерживает marketplace_id для контекста маркетплейса."""
    status_filter = request.args.get('status', '')
    search_query = request.args.get('q', '').strip()
    marketplace_id = request.args.get('marketplace_id', type=int)
    page = request.args.get('page', 1, type=int)
    per_page = 50

    # Получаем текущий маркетплейс для контекста
    current_marketplace = None
    if marketplace_id:
        current_marketplace = Marketplace.query.get(marketplace_id)

    if marketplace_id:
        # Фильтруем бренды, привязанные к маркетплейсу
        query = Brand.query.join(MarketplaceBrand).filter(
            MarketplaceBrand.marketplace_id == marketplace_id
        )
    else:
        query = Brand.query

    if status_filter:
        query = query.filter(Brand.status == status_filter)

    if search_query:
        query = query.filter(Brand.name.ilike(f'%{search_query}%'))

    query = query.order_by(
        db.case(
            (Brand.status == 'needs_review', 0),
            (Brand.status == 'pending', 1),
            (Brand.status == 'verified', 2),
            (Brand.status == 'rejected', 3),
            else_=4
        ),
        Brand.name
    )

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    # Статистика (marketplace-scoped если передан marketplace_id)
    from services.brand_engine import get_brand_engine
    stats = get_brand_engine().get_stats()

    # Все маркетплейсы для селектора
    marketplaces = Marketplace.query.order_by(Marketplace.name).all()

    return render_template('admin_brands.html',
                           brands=pagination.items,
                           pagination=pagination,
                           stats=stats,
                           status_filter=status_filter,
                           search_query=search_query,
                           marketplace_id=marketplace_id,
                           current_marketplace=current_marketplace,
                           marketplaces=marketplaces)


@brands_bp.route('/<int:brand_id>')
@login_required
@admin_required
def detail(brand_id):
    """Карточка бренда."""
    brand = Brand.query.get_or_404(brand_id)
    marketplace_id = request.args.get('marketplace_id', type=int)
    current_marketplace = Marketplace.query.get(marketplace_id) if marketplace_id else None

    aliases = BrandAlias.query.filter_by(brand_id=brand_id).order_by(BrandAlias.created_at.desc()).all()

    # Маркетплейс-привязки бренда
    marketplace_brands = MarketplaceBrand.query.filter_by(brand_id=brand_id).all()

    # Category links через marketplace_brands
    category_links = []
    for mp_brand in marketplace_brands:
        for link in BrandCategoryLink.query.filter_by(marketplace_brand_id=mp_brand.id).all():
            category_links.append(link)

    imported_count = ImportedProduct.query.filter_by(resolved_brand_id=brand_id).count()
    supplier_count = SupplierProduct.query.filter_by(resolved_brand_id=brand_id).count()

    # Также считаем товары по строковому совпадению brand
    brand_name_lower = brand.name.lower()
    imported_by_name = ImportedProduct.query.filter(
        db.func.lower(ImportedProduct.brand) == brand_name_lower
    ).count()
    supplier_by_name = SupplierProduct.query.filter(
        db.func.lower(SupplierProduct.brand) == brand_name_lower
    ).count()

    return render_template('admin_brand_detail.html',
                           brand=brand,
                           aliases=aliases,
                           marketplace_brands=marketplace_brands,
                           category_links=category_links,
                           imported_count=imported_count,
                           supplier_count=supplier_count,
                           imported_by_name=imported_by_name,
                           supplier_by_name=supplier_by_name,
                           marketplace_id=marketplace_id,
                           current_marketplace=current_marketplace)


@brands_bp.route('/review')
@login_required
@admin_required
def review():
    """Очередь проверки брендов."""
    marketplace_id = request.args.get('marketplace_id', type=int)
    current_marketplace = Marketplace.query.get(marketplace_id) if marketplace_id else None

    query = Brand.query.filter(
        Brand.status.in_(['pending', 'needs_review'])
    )
    if marketplace_id:
        query = query.join(MarketplaceBrand).filter(
            MarketplaceBrand.marketplace_id == marketplace_id
        )

    pending_brands = query.order_by(
        db.case(
            (Brand.status == 'needs_review', 0),
            (Brand.status == 'pending', 1),
            else_=2
        ),
        Brand.created_at.desc()
    ).limit(100).all()

    # Для каждого бренда получаем кол-во связанных товаров
    brand_data = []
    for brand in pending_brands:
        aliases = BrandAlias.query.filter_by(brand_id=brand.id).all()
        product_count = ImportedProduct.query.filter(
            db.or_(
                ImportedProduct.resolved_brand_id == brand.id,
                db.func.lower(ImportedProduct.brand) == brand.name.lower(),
            )
        ).count()
        supplier_product_count = SupplierProduct.query.filter(
            db.or_(
                SupplierProduct.resolved_brand_id == brand.id,
                db.func.lower(SupplierProduct.brand) == brand.name.lower(),
            )
        ).count()

        brand_data.append({
            'brand': brand,
            'aliases': aliases,
            'product_count': product_count + supplier_product_count,
        })

    stats = {
        'pending': Brand.query.filter_by(status='pending').count(),
        'needs_review': Brand.query.filter_by(status='needs_review').count(),
    }

    return render_template('admin_brand_review.html',
                           brand_data=brand_data,
                           stats=stats,
                           marketplace_id=marketplace_id,
                           current_marketplace=current_marketplace)


# ------------------------------------------------------------------
# API: CRUD операции
# ------------------------------------------------------------------

@brands_bp.route('/api/create', methods=['POST'])
@login_required
@admin_required
def api_create():
    """Создать бренд вручную."""
    data = request.get_json()
    name = data.get('name', '').strip()

    if not name:
        return jsonify({'success': False, 'error': 'Название обязательно'}), 400

    from services.brand_engine import normalize_for_comparison
    name_norm = normalize_for_comparison(name)

    existing = Brand.query.filter_by(name_normalized=name_norm).first()
    if existing:
        return jsonify({'success': False, 'error': f'Бренд "{existing.name}" уже существует'}), 400

    brand = Brand(
        name=name,
        name_normalized=name_norm,
        status=data.get('status', 'pending'),
        country=data.get('country', ''),
        notes=data.get('notes', ''),
    )
    db.session.add(brand)
    db.session.flush()

    # Добавляем каноническое имя как alias
    alias = BrandAlias(
        brand_id=brand.id,
        alias=name,
        alias_normalized=name_norm,
        source='manual',
        confidence=1.0,
    )
    db.session.add(alias)
    db.session.commit()

    from services.brand_engine import get_brand_engine
    get_brand_engine().invalidate_cache()

    return jsonify({'success': True, 'brand': brand.to_dict()})


@brands_bp.route('/api/<int:brand_id>/update', methods=['POST'])
@login_required
@admin_required
def api_update(brand_id):
    """Обновить бренд."""
    brand = Brand.query.get_or_404(brand_id)
    data = request.get_json()

    if 'name' in data:
        new_name = data['name'].strip()
        if new_name:
            from services.brand_engine import normalize_for_comparison
            new_norm = normalize_for_comparison(new_name)
            existing = Brand.query.filter(
                Brand.name_normalized == new_norm,
                Brand.id != brand_id
            ).first()
            if existing:
                return jsonify({'success': False, 'error': f'Бренд "{existing.name}" уже существует'}), 400
            brand.name = new_name
            brand.name_normalized = new_norm

    if 'status' in data:
        brand.status = data['status']

    if 'country' in data:
        brand.country = data['country']

    if 'notes' in data:
        brand.notes = data['notes']

    brand.updated_at = datetime.utcnow()
    db.session.commit()

    from services.brand_engine import get_brand_engine
    get_brand_engine().invalidate_cache()

    return jsonify({'success': True, 'brand': brand.to_dict()})


@brands_bp.route('/api/<int:brand_id>/aliases', methods=['POST'])
@login_required
@admin_required
def api_add_alias(brand_id):
    """Добавить alias к бренду."""
    data = request.get_json()
    alias_text = data.get('alias', '').strip()

    if not alias_text:
        return jsonify({'success': False, 'error': 'Alias обязателен'}), 400

    from services.brand_engine import get_brand_engine
    result = get_brand_engine().add_alias(
        brand_id=brand_id,
        alias=alias_text,
        source=data.get('source', 'manual'),
    )

    if result is None:
        return jsonify({'success': False, 'error': 'Alias уже существует или бренд не найден'}), 400

    return jsonify({'success': True, 'alias': result})


@brands_bp.route('/api/aliases/<int:alias_id>/delete', methods=['POST'])
@login_required
@admin_required
def api_delete_alias(alias_id):
    """Удалить alias."""
    alias = BrandAlias.query.get_or_404(alias_id)
    brand_id = alias.brand_id

    # Не удаляем последний alias
    count = BrandAlias.query.filter_by(brand_id=brand_id).count()
    if count <= 1:
        return jsonify({'success': False, 'error': 'Нельзя удалить последний alias бренда'}), 400

    db.session.delete(alias)
    db.session.commit()

    from services.brand_engine import get_brand_engine
    get_brand_engine().invalidate_cache()

    return jsonify({'success': True})


@brands_bp.route('/api/<int:brand_id>/verify', methods=['POST'])
@login_required
@admin_required
def api_verify(brand_id):
    """Проверить бренд через API маркетплейса."""
    brand = Brand.query.get_or_404(brand_id)
    data = request.get_json() or {}
    marketplace_id = data.get('marketplace_id')

    # Если маркетплейс не указан — берём WB по умолчанию
    if not marketplace_id:
        wb = Marketplace.query.filter_by(code='wb').first()
        if wb:
            marketplace_id = wb.id

    if not marketplace_id:
        return jsonify({'success': False, 'error': 'Маркетплейс не найден'}), 400

    # Находим продавца с WB API ключом
    from models import Seller
    seller = Seller.query.filter(Seller._wb_api_key_encrypted.isnot(None)).first()
    if not seller or not seller.wb_api_key:
        return jsonify({'success': False, 'error': 'Нет доступных WB API ключей'}), 400

    try:
        from services.wb_api_client import WildberriesAPIClient
        with WildberriesAPIClient(seller.wb_api_key) as wb_client:
            result = wb_client.validate_brand(brand.name)

            # Если API вернул ошибку (например, WB pattern validation)
            if result.get('error'):
                return jsonify({
                    'success': False,
                    'error': f'Ошибка API маркетплейса: {result["error"]}',
                }), 502

            if result.get('valid') and result.get('exact_match'):
                match = result['exact_match']

                # Обновляем глобальный бренд
                brand.status = 'verified'
                brand.updated_at = datetime.utcnow()

                # Создаём/обновляем MarketplaceBrand
                from services.brand_engine import get_brand_engine
                engine = get_brand_engine()
                engine.ensure_marketplace_brand(
                    brand_id=brand.id,
                    marketplace_id=marketplace_id,
                    marketplace_name=match.get('name', brand.name),
                    marketplace_ext_id=match.get('id'),
                )

                db.session.commit()
                engine.invalidate_cache()

                return jsonify({
                    'success': True,
                    'verified': True,
                    'wb_brand': match,
                    'brand': brand.to_dict(),
                })
            else:
                return jsonify({
                    'success': True,
                    'verified': False,
                    'suggestions': result.get('suggestions', [])[:10],
                })

    except Exception as e:
        logger.error(f"Brand verification error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@brands_bp.route('/api/<int:brand_id>/merge', methods=['POST'])
@login_required
@admin_required
def api_merge(brand_id):
    """Объединить бренд с другим."""
    data = request.get_json()
    target_id = data.get('target_id')

    if not target_id:
        return jsonify({'success': False, 'error': 'target_id обязателен'}), 400

    try:
        from services.brand_engine import get_brand_engine
        stats = get_brand_engine().merge_brands(brand_id, target_id)
        return jsonify({'success': True, 'stats': stats})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Brand merge error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@brands_bp.route('/api/review/<int:brand_id>/approve', methods=['POST'])
@login_required
@admin_required
def api_review_approve(brand_id):
    """Подтвердить маппинг бренда из review queue."""
    brand = Brand.query.get_or_404(brand_id)
    data = request.get_json() or {}

    # Если передан target_name — маппим на существующий бренд
    target_name = data.get('target_name', '').strip()
    target_id = data.get('target_id')

    if target_id:
        # Мерж в существующий бренд
        try:
            from services.brand_engine import get_brand_engine
            get_brand_engine().merge_brands(brand_id, target_id)
            return jsonify({'success': True, 'action': 'merged', 'target_id': target_id})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 400

    elif target_name:
        # Переименование и подтверждение
        from services.brand_engine import normalize_for_comparison
        brand.name = target_name
        brand.name_normalized = normalize_for_comparison(target_name)
        brand.status = 'verified'
        brand.updated_at = datetime.utcnow()
    else:
        # Просто подтверждаем текущее имя
        brand.status = 'verified'
        brand.updated_at = datetime.utcnow()

    db.session.commit()

    from services.brand_engine import get_brand_engine
    get_brand_engine().invalidate_cache()

    return jsonify({'success': True, 'action': 'approved', 'brand': brand.to_dict()})


@brands_bp.route('/api/review/<int:brand_id>/reject', methods=['POST'])
@login_required
@admin_required
def api_review_reject(brand_id):
    """Отклонить бренд."""
    brand = Brand.query.get_or_404(brand_id)
    data = request.get_json() or {}

    brand.status = 'rejected'
    brand.notes = data.get('reason', brand.notes)
    brand.updated_at = datetime.utcnow()
    db.session.commit()

    from services.brand_engine import get_brand_engine
    get_brand_engine().invalidate_cache()

    return jsonify({'success': True, 'brand': brand.to_dict()})


# ------------------------------------------------------------------
# API: Поиск и резолв
# ------------------------------------------------------------------

@brands_bp.route('/api/resolve', methods=['POST'])
@login_required
@admin_required
def api_resolve():
    """Резолв бренда (для inline-валидации в UI)."""
    data = request.get_json()
    brand_name = data.get('brand', '').strip()
    marketplace_id = data.get('marketplace_id')
    category_id = data.get('category_id') or data.get('subject_id')

    # Если маркетплейс не указан — берём WB по умолчанию
    if not marketplace_id:
        wb = Marketplace.query.filter_by(code='wb').first()
        if wb:
            marketplace_id = wb.id

    if not brand_name:
        return jsonify({'success': False, 'error': 'brand обязателен'}), 400

    from services.brand_engine import get_brand_engine
    engine = get_brand_engine()
    result = engine.resolve(brand_name, marketplace_id=marketplace_id, category_id=category_id)

    return jsonify({'success': True, 'resolution': result.to_dict()})


@brands_bp.route('/api/search')
@login_required
@admin_required
def api_search():
    """Autocomplete поиск бренда."""
    q = request.args.get('q', '').strip()
    if not q or len(q) < 2:
        return jsonify({'success': True, 'brands': []})

    brands = Brand.query.filter(
        Brand.name.ilike(f'%{q}%')
    ).order_by(Brand.name).limit(20).all()

    return jsonify({
        'success': True,
        'brands': [b.to_dict() for b in brands],
    })


@brands_bp.route('/api/stats')
@login_required
@admin_required
def api_stats():
    """Статистика для dashboard."""
    from services.brand_engine import get_brand_engine
    stats = get_brand_engine().get_stats()
    return jsonify({'success': True, 'stats': stats})


@brands_bp.route('/api/sync', methods=['POST'])
@login_required
@admin_required
def api_sync():
    """Запуск синхронизации брендов в фоновом потоке."""
    from models import Seller
    from services.brand_engine import get_brand_engine

    data = request.get_json() or {}
    marketplace_id = data.get('marketplace_id')

    # По умолчанию синхронизируем WB
    if not marketplace_id:
        wb = Marketplace.query.filter_by(code='wb').first()
        if wb:
            marketplace_id = wb.id

    if not marketplace_id:
        return jsonify({'success': False, 'error': 'Маркетплейс не найден'}), 400

    engine = get_brand_engine()

    # Проверяем, не запущена ли уже синхронизация
    progress = engine.get_sync_progress(marketplace_id)
    if progress and progress.get('status') == 'running':
        return jsonify({'success': False, 'error': 'Синхронизация уже запущена'}), 400

    seller = Seller.query.filter(Seller._wb_api_key_encrypted.isnot(None)).first()
    if not seller or not seller.wb_api_key:
        return jsonify({'success': False, 'error': 'Нет доступных WB API ключей'}), 400

    from flask import current_app
    started = engine.sync_marketplace_brands_async(
        marketplace_id, seller.wb_api_key,
        app=current_app._get_current_object(),
    )
    if not started:
        return jsonify({'success': False, 'error': 'Синхронизация уже запущена'}), 400

    return jsonify({
        'success': True,
        'message': 'Синхронизация запущена в фоне',
        'marketplace_id': marketplace_id,
    })


@brands_bp.route('/api/sync/progress', methods=['GET'])
@login_required
@admin_required
def api_sync_progress():
    """Получить прогресс синхронизации брендов."""
    from services.brand_engine import get_brand_engine

    marketplace_id = request.args.get('marketplace_id', type=int)
    if not marketplace_id:
        wb = Marketplace.query.filter_by(code='wb').first()
        if wb:
            marketplace_id = wb.id

    if not marketplace_id:
        return jsonify({'success': False, 'error': 'Маркетплейс не найден'}), 400

    progress = get_brand_engine().get_sync_progress(marketplace_id)
    if not progress:
        return jsonify({'success': True, 'progress': None})

    return jsonify({'success': True, 'progress': progress})


@brands_bp.route('/api/test-wb-brands', methods=['GET'])
@login_required
@admin_required
def api_test_wb_brands():
    """
    Диагностический эндпоинт: делает один запрос к WB API и возвращает raw ответ.

    Параметры (query string):
      pattern  — строка поиска (по умолчанию "Nike")
      top      — макс результатов (по умолчанию 10)
      locale   — язык (по умолчанию "ru")
      endpoint — путь API (по умолчанию "/api/content/v1/brands")

    Пример:
      /admin/brands/api/test-wb-brands?pattern=Nike&top=10
    """
    import traceback
    from models import Seller
    from services.wb_api_client import WildberriesAPIClient

    pattern = request.args.get('pattern', 'Nike')
    top = request.args.get('top', 10, type=int)
    locale = request.args.get('locale', 'ru')
    endpoint = request.args.get('endpoint', '/api/content/v1/brands')

    seller = Seller.query.filter(Seller._wb_api_key_encrypted.isnot(None)).first()
    if not seller or not seller.wb_api_key:
        return jsonify({'success': False, 'error': 'Нет WB API ключей'}), 400

    api_key = seller.wb_api_key
    result = {
        'api_key_prefix': api_key[:15] + '...' if len(api_key) > 15 else '(empty)',
        'api_key_length': len(api_key),
        'endpoint': endpoint,
        'params': {'pattern': pattern, 'top': top, 'locale': locale},
    }

    try:
        with WildberriesAPIClient(api_key) as client:
            base_url = client._get_base_url('content')
            from urllib.parse import urljoin
            full_url = urljoin(base_url, endpoint)
            result['base_url'] = base_url
            result['full_url'] = full_url

            params = {'pattern': pattern, 'top': top, 'locale': locale}
            response = client._make_request('GET', 'content', endpoint, params=params)

            result['status_code'] = response.status_code
            result['response_url'] = response.url
            result['response_body'] = response.text[:2000]
            result['response_headers'] = dict(response.headers)

            try:
                result['json'] = response.json()
            except Exception:
                result['json'] = None

    except Exception as e:
        result['error'] = f'{type(e).__name__}: {str(e)}'
        result['traceback'] = traceback.format_exc()

    return jsonify(result)


def register_brand_routes(app):
    """Регистрация blueprint в приложении."""
    app.register_blueprint(brands_bp)
