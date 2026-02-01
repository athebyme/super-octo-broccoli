"""
Роуты для безопасного изменения цен

Endpoints:
- GET  /prices - главная страница управления ценами
- GET  /prices/settings - настройки безопасности
- POST /prices/settings - сохранить настройки
- GET  /prices/change - форма изменения цен с пагинацией
- POST /prices/create-batch - создать батч изменений
- GET  /prices/batch/<id> - просмотр батча
- POST /prices/batch/<id>/confirm - подтвердить опасные изменения
- POST /prices/batch/<id>/apply - применить изменения
- POST /prices/batch/<id>/revert - откатить изменения
- POST /prices/batch/<id>/cancel - отменить батч
- GET  /prices/history - история изменений
- GET  /prices/formulas - управление формулами

API Endpoints:
- GET  /api/prices/products - получить товары с пагинацией
- POST /api/prices/preview - предпросмотр изменений
- GET  /api/prices/batch/<id>/status - статус батча
"""

import logging
import re
import math
from datetime import datetime
from decimal import Decimal
from typing import List, Dict, Any, Optional, Tuple

from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash
from flask_login import login_required, current_user

from models import (
    db, Product, Seller, SafePriceChangeSettings,
    PriceChangeBatch, PriceChangeItem, PriceHistory
)
from wb_api_client import WildberriesAPIClient, WBAPIException

logger = logging.getLogger(__name__)

# Blueprint для роутов цен
prices_bp = Blueprint('prices', __name__, url_prefix='/prices')


def get_current_seller():
    """Получить текущего продавца"""
    if not current_user.is_authenticated:
        return None
    return current_user.seller


def get_or_create_settings(seller_id: int) -> SafePriceChangeSettings:
    """Получить или создать настройки безопасности для продавца"""
    settings = SafePriceChangeSettings.query.filter_by(seller_id=seller_id).first()
    if not settings:
        settings = SafePriceChangeSettings(seller_id=seller_id)
        db.session.add(settings)
        db.session.commit()
    return settings


class FormulaEvaluator:
    """
    Безопасный вычислитель формул для расчета цен.

    Поддерживаемые переменные:
    - P: закупочная цена (price)
    - Q: желаемая прибыль (profit)
    - A: процент прибыли из диапазона
    - S: стоимость доставки
    - R, T, U: промежуточные переменные
    - Z: финальная цена
    - X: премиум цена
    - Y: завышенная цена

    Поддерживаемые операции: +, -, *, /, (), min(), max(), if условия
    """

    ALLOWED_NAMES = {
        'min': min,
        'max': max,
        'abs': abs,
        'round': round,
    }

    def __init__(self, formula: str, variables: Dict[str, float] = None):
        self.formula = formula
        self.variables = variables or {}

    def evaluate(self) -> float:
        """Безопасно вычислить формулу"""
        try:
            # Заменяем переменные на значения
            expr = self.formula
            for var, value in self.variables.items():
                expr = re.sub(rf'\b{var}\b', str(value), expr)

            # Проверяем на безопасность - только разрешенные символы
            allowed_chars = set('0123456789.+-*/() ,<>=!')
            allowed_words = {'min', 'max', 'abs', 'round', 'if', 'else', 'and', 'or'}

            # Удаляем разрешенные слова для проверки
            check_expr = expr
            for word in allowed_words:
                check_expr = re.sub(rf'\b{word}\b', '', check_expr)

            # Проверяем что остались только разрешенные символы
            remaining = set(check_expr.replace(' ', ''))
            if not remaining.issubset(allowed_chars):
                raise ValueError(f"Недопустимые символы в формуле: {remaining - allowed_chars}")

            # Вычисляем
            result = eval(expr, {"__builtins__": {}}, self.ALLOWED_NAMES)
            return float(result)
        except Exception as e:
            logger.error(f"Formula evaluation error: {e}, formula: {self.formula}")
            return 0.0


def calculate_price_with_formula(
    old_price: float,
    formula: str,
    variables: Dict[str, float] = None
) -> float:
    """
    Рассчитать новую цену по формуле

    Args:
        old_price: Текущая цена
        formula: Формула расчета
        variables: Дополнительные переменные

    Returns:
        Новая цена
    """
    vars_dict = {'P': old_price}
    if variables:
        vars_dict.update(variables)

    evaluator = FormulaEvaluator(formula, vars_dict)
    return evaluator.evaluate()


def calculate_price_changes(
    products: List[Product],
    change_type: str,
    change_value: float,
    settings: SafePriceChangeSettings,
    formula: str = None,
    formula_variables: Dict[str, float] = None
) -> Tuple[List[Dict], Dict]:
    """
    Рассчитать изменения цен и классифицировать их по безопасности

    Args:
        products: Список товаров
        change_type: Тип изменения ('fixed', 'percent', 'set', 'formula')
        change_value: Значение изменения
        settings: Настройки безопасности
        formula: Формула для расчета (если change_type='formula')
        formula_variables: Переменные для формулы

    Returns:
        (items, stats) - список изменений и статистика
    """
    items = []
    stats = {
        'total': 0,
        'safe': 0,
        'warning': 0,
        'dangerous': 0
    }

    for product in products:
        old_price = float(product.price) if product.price else 0

        # Рассчитываем новую цену
        if change_type == 'fixed':
            # Изменение на фиксированную сумму
            new_price = old_price + change_value
        elif change_type == 'percent':
            # Изменение на процент
            new_price = old_price * (1 + change_value / 100)
        elif change_type == 'set':
            # Установить конкретную цену
            new_price = change_value
        elif change_type == 'formula' and formula:
            # Расчет по формуле
            new_price = calculate_price_with_formula(old_price, formula, formula_variables)
        else:
            new_price = old_price

        # Округляем до целого (WB требует целые цены)
        new_price = round(max(0, new_price))

        # Рассчитываем изменение
        if old_price > 0:
            change_percent = ((new_price - old_price) / old_price) * 100
            change_amount = new_price - old_price
        else:
            change_percent = 100 if new_price > 0 else 0
            change_amount = new_price

        # Классифицируем изменение
        safety_level = settings.classify_change(old_price, new_price)

        items.append({
            'product_id': product.id,
            'nm_id': product.nm_id,
            'vendor_code': product.vendor_code,
            'title': product.title,
            'brand': product.brand,
            'old_price': old_price,
            'new_price': new_price,
            'change_amount': round(change_amount, 2),
            'change_percent': round(change_percent, 2),
            'safety_level': safety_level
        })

        stats['total'] += 1
        stats[safety_level] += 1

    return items, stats


# ==================== WEB ROUTES ====================

@prices_bp.route('/')
@login_required
def prices_dashboard():
    """Главная страница управления ценами"""
    seller = get_current_seller()
    if not seller:
        flash('Необходимо быть продавцом для доступа к этой странице', 'warning')
        return redirect(url_for('dashboard'))

    settings = get_or_create_settings(seller.id)

    # Получаем статистику батчей
    pending_batches = PriceChangeBatch.query.filter_by(
        seller_id=seller.id,
        status='pending_review'
    ).count()

    recent_batches = PriceChangeBatch.query.filter_by(
        seller_id=seller.id
    ).order_by(PriceChangeBatch.created_at.desc()).limit(10).all()

    # Статистика товаров
    products_count = Product.query.filter_by(seller_id=seller.id, is_active=True).count()

    return render_template(
        'prices_dashboard.html',
        settings=settings,
        pending_batches=pending_batches,
        recent_batches=recent_batches,
        products_count=products_count
    )


@prices_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def prices_settings():
    """Настройки безопасности изменения цен"""
    seller = get_current_seller()
    if not seller:
        flash('Необходимо быть продавцом для доступа к этой странице', 'warning')
        return redirect(url_for('dashboard'))

    settings = get_or_create_settings(seller.id)

    if request.method == 'POST':
        try:
            settings.is_enabled = request.form.get('is_enabled') == 'on'
            settings.safe_threshold_percent = float(request.form.get('safe_threshold_percent', 10))
            settings.warning_threshold_percent = float(request.form.get('warning_threshold_percent', 20))
            settings.mode = request.form.get('mode', 'confirm')
            settings.require_comment_for_dangerous = request.form.get('require_comment_for_dangerous') == 'on'
            settings.allow_bulk_dangerous = request.form.get('allow_bulk_dangerous') == 'on'
            settings.max_products_per_batch = int(request.form.get('max_products_per_batch', 100))
            settings.notify_on_dangerous = request.form.get('notify_on_dangerous') == 'on'
            settings.notify_email = request.form.get('notify_email', '').strip() or None

            db.session.commit()
            flash('Настройки успешно сохранены', 'success')
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error saving price settings: {e}")
            flash(f'Ошибка сохранения настроек: {str(e)}', 'danger')

        return redirect(url_for('prices.prices_settings'))

    return render_template('prices_settings.html', settings=settings)


@prices_bp.route('/change', methods=['GET'])
@login_required
def prices_change():
    """Форма изменения цен с пагинацией"""
    seller = get_current_seller()
    if not seller:
        flash('Необходимо быть продавцом для доступа к этой странице', 'warning')
        return redirect(url_for('dashboard'))

    settings = get_or_create_settings(seller.id)

    # Получаем уникальные бренды и категории для фильтров
    brands = db.session.query(Product.brand).filter(
        Product.seller_id == seller.id,
        Product.is_active == True,
        Product.brand.isnot(None),
        Product.brand != ''
    ).distinct().order_by(Product.brand).all()
    brands = [b[0] for b in brands if b[0]]

    categories = db.session.query(Product.object_name).filter(
        Product.seller_id == seller.id,
        Product.is_active == True,
        Product.object_name.isnot(None),
        Product.object_name != ''
    ).distinct().order_by(Product.object_name).all()
    categories = [c[0] for c in categories if c[0]]

    return render_template(
        'prices_change.html',
        settings=settings,
        brands=brands,
        categories=categories
    )


@prices_bp.route('/create-batch', methods=['POST'])
@login_required
def create_batch():
    """Создать батч изменений из выбранных товаров"""
    seller = get_current_seller()
    if not seller:
        return jsonify({'error': 'Unauthorized'}), 401

    settings = get_or_create_settings(seller.id)
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No data provided'}), 400

    try:
        change_type = data.get('change_type', 'percent')
        change_value = float(data.get('change_value', 0)) if data.get('change_value') else 0
        name = data.get('name', '').strip()
        description = data.get('description', '').strip()
        selected_ids = data.get('product_ids', [])
        formula = data.get('formula', '').strip()
        formula_variables = data.get('formula_variables', {})

        if not selected_ids:
            return jsonify({'error': 'Выберите хотя бы один товар'}), 400

        # Проверяем лимит товаров
        if len(selected_ids) > settings.max_products_per_batch:
            return jsonify({
                'error': f'Превышен лимит товаров ({settings.max_products_per_batch}). Выбрано: {len(selected_ids)}'
            }), 400

        # Получаем выбранные товары
        selected_products = Product.query.filter(
            Product.id.in_(selected_ids),
            Product.seller_id == seller.id
        ).all()

        if not selected_products:
            return jsonify({'error': 'Товары не найдены'}), 404

        # Рассчитываем изменения
        items, stats = calculate_price_changes(
            selected_products, change_type, change_value, settings,
            formula=formula, formula_variables=formula_variables
        )

        # Проверяем режим блокировки
        if stats['dangerous'] > 0 and settings.mode == 'block':
            return jsonify({
                'error': 'Обнаружены опасные изменения. Операция заблокирована настройками безопасности.',
                'stats': stats
            }), 403

        # Создаем батч
        batch = PriceChangeBatch(
            seller_id=seller.id,
            name=name or f'Изменение цен ({datetime.now().strftime("%d.%m.%Y %H:%M")})',
            description=description,
            change_type=change_type,
            change_value=change_value if change_type != 'formula' else None,
            change_formula=formula if change_type == 'formula' else None,
            total_items=stats['total'],
            safe_count=stats['safe'],
            warning_count=stats['warning'],
            dangerous_count=stats['dangerous'],
            has_safe_changes=stats['safe'] > 0,
            has_warning_changes=stats['warning'] > 0,
            has_dangerous_changes=stats['dangerous'] > 0
        )

        # Определяем статус батча
        if stats['dangerous'] > 0 and settings.mode == 'confirm':
            batch.status = 'pending_review'
        else:
            batch.status = 'confirmed'

        db.session.add(batch)
        db.session.flush()

        # Создаем элементы батча
        for item in items:
            price_item = PriceChangeItem(
                batch_id=batch.id,
                product_id=item['product_id'],
                nm_id=item['nm_id'],
                vendor_code=item['vendor_code'],
                product_title=item['title'],
                old_price=item['old_price'],
                new_price=item['new_price'],
                price_change_amount=item['change_amount'],
                price_change_percent=item['change_percent'],
                safety_level=item['safety_level']
            )
            db.session.add(price_item)

        db.session.commit()

        return jsonify({
            'success': True,
            'batch_id': batch.id,
            'status': batch.status,
            'stats': stats,
            'redirect': url_for('prices.batch_confirm', batch_id=batch.id) if batch.status == 'pending_review' else url_for('prices.batch_detail', batch_id=batch.id)
        })

    except Exception as e:
        db.session.rollback()
        logger.error(f"Error creating price batch: {e}")
        return jsonify({'error': str(e)}), 500


@prices_bp.route('/batch/<int:batch_id>')
@login_required
def batch_detail(batch_id: int):
    """Просмотр деталей батча"""
    seller = get_current_seller()
    if not seller:
        return redirect(url_for('dashboard'))

    batch = PriceChangeBatch.query.filter_by(
        id=batch_id,
        seller_id=seller.id
    ).first_or_404()

    items = batch.items.order_by(PriceChangeItem.safety_level.desc()).all()

    return render_template(
        'prices_batch_detail.html',
        batch=batch,
        items=items
    )


@prices_bp.route('/batch/<int:batch_id>/confirm', methods=['GET', 'POST'])
@login_required
def batch_confirm(batch_id: int):
    """Страница подтверждения опасных изменений"""
    seller = get_current_seller()
    if not seller:
        return redirect(url_for('dashboard'))

    batch = PriceChangeBatch.query.filter_by(
        id=batch_id,
        seller_id=seller.id
    ).first_or_404()

    if batch.status != 'pending_review':
        flash('Этот батч не требует подтверждения', 'info')
        return redirect(url_for('prices.batch_detail', batch_id=batch_id))

    settings = get_or_create_settings(seller.id)

    # Получаем изменения по категориям
    dangerous_items = batch.items.filter_by(safety_level='dangerous').all()
    warning_items = batch.items.filter_by(safety_level='warning').all()
    safe_items = batch.items.filter_by(safety_level='safe').all()

    if request.method == 'POST':
        action = request.form.get('action')
        comment = request.form.get('comment', '').strip()

        if action == 'confirm':
            if settings.require_comment_for_dangerous and not comment:
                flash('Требуется комментарий для подтверждения опасных изменений', 'warning')
                return redirect(request.url)

            batch.status = 'confirmed'
            batch.confirmed_at = datetime.utcnow()
            batch.confirmed_by_user_id = current_user.id
            batch.confirmation_comment = comment
            db.session.commit()

            flash('Изменения подтверждены', 'success')
            return redirect(url_for('prices.batch_detail', batch_id=batch_id))

        elif action == 'reject':
            batch.status = 'cancelled'
            db.session.commit()
            flash('Изменения отклонены', 'info')
            return redirect(url_for('prices.prices_dashboard'))

    return render_template(
        'prices_batch_confirm.html',
        batch=batch,
        dangerous_items=dangerous_items,
        warning_items=warning_items,
        safe_items=safe_items,
        settings=settings
    )


@prices_bp.route('/batch/<int:batch_id>/apply', methods=['POST'])
@login_required
def batch_apply(batch_id: int):
    """Применить изменения к WB"""
    seller = get_current_seller()
    if not seller:
        return jsonify({'error': 'Unauthorized'}), 401

    batch = PriceChangeBatch.query.filter_by(
        id=batch_id,
        seller_id=seller.id
    ).first_or_404()

    if not batch.can_apply():
        return jsonify({'error': 'Батч не может быть применен в текущем статусе'}), 400

    try:
        # Получаем API клиент
        if not seller.has_valid_api_key():
            return jsonify({'error': 'API ключ не настроен'}), 400

        api_client = WildberriesAPIClient(seller.wb_api_key)

        batch.status = 'applying'
        db.session.commit()

        # Собираем данные для WB API
        prices_data = []
        items = batch.items.filter_by(status='pending').all()

        for item in items:
            prices_data.append({
                'nmID': item.nm_id,
                'price': int(item.new_price)  # WB требует целое число
            })

        # Отправляем в WB
        result = api_client.upload_prices_batch(
            prices_data,
            log_to_db=True,
            seller_id=seller.id
        )

        # Обновляем статусы элементов
        applied_count = 0
        failed_count = 0

        for item in items:
            # Проверяем есть ли ошибки для этого товара
            item_failed = False
            for error in result.get('errors', []):
                if item.nm_id in error.get('nm_ids', []):
                    item.status = 'failed'
                    item.error_message = error.get('error')
                    item_failed = True
                    failed_count += 1
                    break

            if not item_failed:
                item.status = 'applied'
                item.wb_applied_at = datetime.utcnow()
                applied_count += 1

                # Сохраняем в историю цен
                price_history = PriceHistory(
                    product_id=item.product_id,
                    seller_id=seller.id,
                    old_price=item.old_price,
                    new_price=item.new_price,
                    price_change_percent=item.price_change_percent
                )
                db.session.add(price_history)

                # Обновляем цену в Product
                product = Product.query.get(item.product_id)
                if product:
                    product.price = item.new_price

        # Обновляем статус батча
        batch.applied_count = applied_count
        batch.failed_count = failed_count
        batch.applied_at = datetime.utcnow()

        if failed_count == 0:
            batch.status = 'applied'
        elif applied_count > 0:
            batch.status = 'partially_applied'
        else:
            batch.status = 'failed'

        batch.apply_errors = result.get('errors')
        db.session.commit()

        api_client.close()

        return jsonify({
            'success': True,
            'applied': applied_count,
            'failed': failed_count,
            'status': batch.status
        })

    except WBAPIException as e:
        batch.status = 'failed'
        batch.apply_errors = [{'error': str(e)}]
        db.session.commit()
        logger.error(f"WB API error applying batch {batch_id}: {e}")
        return jsonify({'error': str(e)}), 500

    except Exception as e:
        batch.status = 'failed'
        db.session.commit()
        logger.error(f"Error applying batch {batch_id}: {e}")
        return jsonify({'error': str(e)}), 500


@prices_bp.route('/batch/<int:batch_id>/revert', methods=['POST'])
@login_required
def batch_revert(batch_id: int):
    """Откатить изменения"""
    seller = get_current_seller()
    if not seller:
        return jsonify({'error': 'Unauthorized'}), 401

    batch = PriceChangeBatch.query.filter_by(
        id=batch_id,
        seller_id=seller.id
    ).first_or_404()

    if not batch.can_revert():
        return jsonify({'error': 'Батч не может быть откачен'}), 400

    try:
        # Получаем API клиент
        if not seller.has_valid_api_key():
            return jsonify({'error': 'API ключ не настроен'}), 400

        api_client = WildberriesAPIClient(seller.wb_api_key)

        # Создаем обратный батч
        revert_batch = PriceChangeBatch(
            seller_id=seller.id,
            name=f'Откат: {batch.name}',
            description=f'Откат изменений батча #{batch.id}',
            change_type='revert',
            status='applying'
        )
        db.session.add(revert_batch)
        db.session.flush()

        # Собираем данные для отката (старые цены)
        prices_data = []
        applied_items = batch.items.filter_by(status='applied').all()

        for item in applied_items:
            prices_data.append({
                'nmID': item.nm_id,
                'price': int(item.old_price) if item.old_price else 0
            })

            # Создаем элемент отката
            revert_item = PriceChangeItem(
                batch_id=revert_batch.id,
                product_id=item.product_id,
                nm_id=item.nm_id,
                vendor_code=item.vendor_code,
                product_title=item.product_title,
                old_price=item.new_price,
                new_price=item.old_price,
                safety_level='safe'
            )
            revert_item.calculate_change()
            db.session.add(revert_item)

        # Отправляем в WB
        result = api_client.upload_prices_batch(
            prices_data,
            log_to_db=True,
            seller_id=seller.id
        )

        # Обновляем статусы
        revert_batch.total_items = len(applied_items)
        revert_batch.applied_count = result.get('success', 0)
        revert_batch.failed_count = result.get('failed', 0)
        revert_batch.applied_at = datetime.utcnow()
        revert_batch.status = 'applied' if result.get('failed', 0) == 0 else 'partially_applied'

        # Обновляем оригинальный батч
        batch.reverted = True
        batch.reverted_at = datetime.utcnow()
        batch.reverted_by_user_id = current_user.id
        batch.revert_batch_id = revert_batch.id

        # Обновляем цены в Product
        for item in applied_items:
            product = Product.query.get(item.product_id)
            if product:
                product.price = item.old_price

        db.session.commit()
        api_client.close()

        return jsonify({
            'success': True,
            'revert_batch_id': revert_batch.id
        })

    except Exception as e:
        db.session.rollback()
        logger.error(f"Error reverting batch {batch_id}: {e}")
        return jsonify({'error': str(e)}), 500


@prices_bp.route('/batch/<int:batch_id>/cancel', methods=['POST'])
@login_required
def batch_cancel(batch_id: int):
    """Отменить батч"""
    seller = get_current_seller()
    if not seller:
        return jsonify({'error': 'Unauthorized'}), 401

    batch = PriceChangeBatch.query.filter_by(
        id=batch_id,
        seller_id=seller.id
    ).first_or_404()

    if not batch.can_cancel():
        return jsonify({'error': 'Батч не может быть отменен'}), 400

    batch.status = 'cancelled'
    db.session.commit()

    return jsonify({'success': True})


@prices_bp.route('/history')
@login_required
def prices_history():
    """История изменений цен"""
    seller = get_current_seller()
    if not seller:
        return redirect(url_for('dashboard'))

    page = request.args.get('page', 1, type=int)
    per_page = 20

    # Фильтры
    status = request.args.get('status')
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

    query = PriceChangeBatch.query.filter_by(seller_id=seller.id)

    if status:
        query = query.filter_by(status=status)
    if date_from:
        query = query.filter(PriceChangeBatch.created_at >= date_from)
    if date_to:
        query = query.filter(PriceChangeBatch.created_at <= date_to)

    batches = query.order_by(PriceChangeBatch.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    return render_template(
        'prices_history.html',
        batches=batches,
        status=status,
        date_from=date_from,
        date_to=date_to
    )


# ==================== API ROUTES ====================

@prices_bp.route('/api/products')
@login_required
def api_get_products():
    """API: Получить товары с пагинацией и фильтрами"""
    seller = get_current_seller()
    if not seller:
        return jsonify({'error': 'Unauthorized'}), 401

    # Параметры пагинации
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    per_page = min(per_page, 100)  # Максимум 100 на страницу

    # Фильтры
    search = request.args.get('search', '').strip()
    brand = request.args.get('brand', '').strip()
    category = request.args.get('category', '').strip()

    # Базовый запрос
    query = Product.query.filter_by(seller_id=seller.id, is_active=True)

    # Применяем фильтры
    if search:
        search_term = f'%{search}%'
        query = query.filter(
            db.or_(
                Product.title.ilike(search_term),
                Product.vendor_code.ilike(search_term),
                Product.nm_id.cast(db.String).ilike(search_term)
            )
        )

    if brand:
        query = query.filter(Product.brand == brand)

    if category:
        query = query.filter(Product.object_name == category)

    # Общее количество
    total = query.count()

    # Пагинация
    products = query.order_by(Product.title).offset((page - 1) * per_page).limit(per_page).all()

    # Формируем ответ
    products_data = []
    for p in products:
        products_data.append({
            'id': p.id,
            'nm_id': p.nm_id,
            'vendor_code': p.vendor_code,
            'title': p.title[:80] if p.title else '',
            'brand': p.brand or '',
            'category': p.object_name or '',
            'price': float(p.price) if p.price else 0,
            'discount_price': float(p.discount_price) if p.discount_price else None
        })

    return jsonify({
        'success': True,
        'products': products_data,
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': math.ceil(total / per_page)
    })


@prices_bp.route('/api/preview', methods=['POST'])
@login_required
def api_preview_changes():
    """API: Предпросмотр изменений цен"""
    seller = get_current_seller()
    if not seller:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    product_ids = data.get('product_ids', [])
    change_type = data.get('change_type', 'percent')
    change_value = float(data.get('change_value', 0)) if data.get('change_value') else 0
    formula = data.get('formula', '')
    formula_variables = data.get('formula_variables', {})

    if not product_ids:
        return jsonify({'error': 'No products selected'}), 400

    settings = get_or_create_settings(seller.id)

    products = Product.query.filter(
        Product.id.in_(product_ids),
        Product.seller_id == seller.id
    ).all()

    items, stats = calculate_price_changes(
        products, change_type, change_value, settings,
        formula=formula, formula_variables=formula_variables
    )

    return jsonify({
        'success': True,
        'items': items,
        'stats': stats
    })


@prices_bp.route('/api/batch/<int:batch_id>/status')
@login_required
def api_batch_status(batch_id: int):
    """API: Получить статус батча"""
    seller = get_current_seller()
    if not seller:
        return jsonify({'error': 'Unauthorized'}), 401

    batch = PriceChangeBatch.query.filter_by(
        id=batch_id,
        seller_id=seller.id
    ).first_or_404()

    return jsonify(batch.to_dict())


def register_routes(app):
    """Зарегистрировать blueprint в приложении"""
    app.register_blueprint(prices_bp)
