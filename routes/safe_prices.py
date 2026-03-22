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

import ast
import logging
import operator
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
from services.wb_api_client import WildberriesAPIClient, WBAPIException
from utils.safe_error import safe_error_message

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

    Использует AST-парсер вместо eval() для безопасного вычисления.

    Поддерживаемые переменные:
    - P: закупочная цена (price)
    - Q: желаемая прибыль (profit)
    - A: процент прибыли из диапазона
    - S: стоимость доставки
    - R, T, U: промежуточные переменные
    - Z: финальная цена
    - X: премиум цена
    - Y: завышенная цена

    Поддерживаемые операции: +, -, *, /, (), min(), max(), abs(), round()
    """

    ALLOWED_FUNCTIONS = {
        'min': min,
        'max': max,
        'abs': abs,
        'round': round,
    }

    # Допустимые бинарные операторы
    _BINARY_OPS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }

    # Допустимые унарные операторы
    _UNARY_OPS = {
        ast.UAdd: operator.pos,
        ast.USub: operator.neg,
    }

    # Допустимые операторы сравнения
    _CMP_OPS = {
        ast.Gt: operator.gt,
        ast.GtE: operator.ge,
        ast.Lt: operator.lt,
        ast.LtE: operator.le,
        ast.Eq: operator.eq,
        ast.NotEq: operator.ne,
    }

    # Максимальная длина формулы
    MAX_FORMULA_LENGTH = 500

    def __init__(self, formula: str, variables: Dict[str, float] = None):
        self.formula = formula
        self.variables = variables or {}

    def _eval_node(self, node: ast.AST) -> float:
        """Рекурсивно вычисляет AST-узел безопасным способом."""
        if isinstance(node, ast.Expression):
            return self._eval_node(node.body)

        # Числовые литералы
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)

        # Бинарные операции: a + b, a * b, ...
        if isinstance(node, ast.BinOp):
            op_func = self._BINARY_OPS.get(type(node.op))
            if op_func is None:
                raise ValueError(f"Недопустимый оператор: {type(node.op).__name__}")
            left = self._eval_node(node.left)
            right = self._eval_node(node.right)
            if isinstance(node.op, ast.Pow) and right > 10:
                raise ValueError("Степень не может быть больше 10")
            if isinstance(node.op, (ast.Div, ast.FloorDiv)) and right == 0:
                raise ValueError("Деление на ноль")
            return float(op_func(left, right))

        # Унарные операции: -a, +a
        if isinstance(node, ast.UnaryOp):
            op_func = self._UNARY_OPS.get(type(node.op))
            if op_func is None:
                raise ValueError(f"Недопустимый унарный оператор: {type(node.op).__name__}")
            return float(op_func(self._eval_node(node.operand)))

        # Вызовы функций: min(), max(), abs(), round()
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ValueError("Вызовы методов не разрешены")
            func_name = node.func.id
            func = self.ALLOWED_FUNCTIONS.get(func_name)
            if func is None:
                raise ValueError(f"Функция '{func_name}' не разрешена")
            args = [self._eval_node(arg) for arg in node.args]
            return float(func(*args))

        # Тернарный оператор: a if condition else b
        if isinstance(node, ast.IfExp):
            test_val = self._eval_node(node.test)
            if test_val:
                return self._eval_node(node.body)
            return self._eval_node(node.orelse)

        # Сравнения: a > b, a <= b, ...
        if isinstance(node, ast.Compare):
            left = self._eval_node(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                op_func = self._CMP_OPS.get(type(op))
                if op_func is None:
                    raise ValueError(f"Недопустимый оператор сравнения: {type(op).__name__}")
                right = self._eval_node(comparator)
                if not op_func(left, right):
                    return 0.0
                left = right
            return 1.0

        # Булевы операции: and, or
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                result = 1.0
                for value in node.values:
                    result = self._eval_node(value)
                    if not result:
                        return 0.0
                return result
            elif isinstance(node.op, ast.Or):
                for value in node.values:
                    result = self._eval_node(value)
                    if result:
                        return result
                return 0.0

        # Имена переменных (уже подставлены как числа, но на всякий случай)
        if isinstance(node, ast.Name):
            raise ValueError(f"Неизвестная переменная: {node.id}")

        raise ValueError(f"Недопустимая конструкция в формуле: {type(node).__name__}")

    def evaluate(self) -> float:
        """Безопасно вычислить формулу через AST-парсер."""
        try:
            if len(self.formula) > self.MAX_FORMULA_LENGTH:
                raise ValueError("Формула слишком длинная")

            # Заменяем переменные на значения
            expr = self.formula
            for var, value in self.variables.items():
                expr = re.sub(rf'\b{re.escape(var)}\b', str(value), expr)

            # Парсим выражение в AST
            tree = ast.parse(expr, mode='eval')

            # Вычисляем через безопасный обход дерева
            result = self._eval_node(tree)
            return float(result)
        except (ValueError, TypeError, SyntaxError, ZeroDivisionError) as e:
            logger.warning(f"Formula evaluation error: {type(e).__name__}: {e}")
            return 0.0
        except Exception:
            logger.warning("Unexpected formula evaluation error")
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

        # Пропускаем товары без nm_id — они не существуют на WB
        if not product.nm_id or product.nm_id <= 0:
            continue

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
        # Товары с ценой 0 → warning, чтобы продавец видел их
        if new_price <= 0:
            safety_level = 'warning'
        else:
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

        # Проверяем лимит товаров (если не разрешен безлимитный режим)
        allow_unlimited = getattr(settings, 'allow_unlimited_batch', True)
        if not allow_unlimited and len(selected_ids) > settings.max_products_per_batch:
            return jsonify({
                'error': f'Превышен лимит товаров ({settings.max_products_per_batch}). Выбрано: {len(selected_ids)}. '
                         f'Включите "Разрешить массовые операции" в настройках.'
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
        return jsonify({'error': safe_error_message(e)}), 500


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

        # Собираем данные для WB API, фильтруя невалидные элементы
        prices_data = []
        items = batch.items.filter_by(status='pending').all()
        valid_items = []     # Элементы, отправленные в WB
        skipped_count = 0

        for item in items:
            # Валидация: пропускаем элементы с невалидными данными
            price_val = int(item.new_price) if item.new_price else 0
            if not item.nm_id or item.nm_id <= 0:
                item.status = 'skipped'
                item.error_message = 'Нет nmID — товар не существует на WB'
                skipped_count += 1
                continue
            if price_val <= 0:
                item.status = 'skipped'
                item.error_message = 'Цена = 0 — невозможно установить нулевую цену на WB'
                skipped_count += 1
                continue

            prices_data.append({
                'nmID': item.nm_id,
                'price': price_val
            })
            valid_items.append(item)

        if skipped_count > 0:
            logger.warning(
                f"Batch {batch_id}: пропущено {skipped_count} товаров "
                f"(невалидные nmID или цена=0)"
            )

        # Отправляем в WB только валидные элементы
        result = {'total': 0, 'success': 0, 'failed': 0, 'errors': []}
        if prices_data:
            result = api_client.upload_prices_batch(
                prices_data,
                log_to_db=True,
                seller_id=seller.id
            )

        # Обновляем статусы элементов
        applied_count = 0
        failed_count = skipped_count  # Считаем пропущенные как failed

        # Собираем множество всех nmID, которые попали в ошибки
        failed_nm_ids = set()
        error_by_nm_id = {}
        for error in result.get('errors', []):
            for nm_id in error.get('nm_ids', []):
                failed_nm_ids.add(nm_id)
                error_by_nm_id[nm_id] = error.get('error', 'Неизвестная ошибка')

        for item in valid_items:
            if item.nm_id in failed_nm_ids:
                item.status = 'failed'
                item.error_message = error_by_nm_id.get(item.nm_id, 'Ошибка WB API')
                failed_count += 1
            else:
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
        batch.apply_errors = [{'error': safe_error_message(e)}]
        db.session.commit()
        logger.error(f"WB API error applying batch {batch_id}: {e}")
        return jsonify({'error': safe_error_message(e)}), 500

    except Exception as e:
        batch.status = 'failed'
        db.session.commit()
        logger.error(f"Error applying batch {batch_id}: {e}")
        return jsonify({'error': safe_error_message(e)}), 500


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
            old_price_val = int(item.old_price) if item.old_price else 0
            # Пропускаем невалидные элементы (нет nmID или цена=0)
            if not item.nm_id or item.nm_id <= 0 or old_price_val <= 0:
                continue
            prices_data.append({
                'nmID': item.nm_id,
                'price': old_price_val
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
        return jsonify({'error': safe_error_message(e)}), 500


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


@prices_bp.route('/api/products/all-ids')
@login_required
def api_get_all_product_ids():
    """
    API: Получить все ID товаров (для кнопки "Выбрать все")

    Query params:
    - search: поиск по названию/артикулу
    - brand: фильтр по бренду
    - category: фильтр по категории

    Returns:
    {
        "success": true,
        "ids": [1, 2, 3, ...],
        "total": 1234
    }
    """
    seller = get_current_seller()
    if not seller:
        return jsonify({'error': 'Unauthorized'}), 401

    # Те же фильтры что и в api_get_products
    search = request.args.get('search', '').strip()
    brand = request.args.get('brand', '').strip()
    category = request.args.get('category', '').strip()

    # Базовый запрос - только ID
    query = db.session.query(Product.id).filter(
        Product.seller_id == seller.id,
        Product.is_active == True
    )

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

    # Получаем все ID
    product_ids = [row[0] for row in query.all()]

    return jsonify({
        'success': True,
        'ids': product_ids,
        'total': len(product_ids)
    })


@prices_bp.route('/api/settings')
@login_required
def api_get_settings():
    """API: Получить текущие настройки безопасности"""
    seller = get_current_seller()
    if not seller:
        return jsonify({'error': 'Unauthorized'}), 401

    settings = get_or_create_settings(seller.id)

    return jsonify({
        'success': True,
        'settings': {
            'max_products_per_batch': settings.max_products_per_batch,
            'allow_unlimited_batch': getattr(settings, 'allow_unlimited_batch', False),
            'safe_threshold_percent': settings.safe_threshold_percent,
            'warning_threshold_percent': settings.warning_threshold_percent,
            'mode': settings.mode
        }
    })


def register_routes(app):
    """Зарегистрировать blueprint в приложении"""
    app.register_blueprint(prices_bp)
