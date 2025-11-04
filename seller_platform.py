# -*- coding: utf-8 -*-
"""
Платформа для продавцов WB - основной файл приложения
"""
import os
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, render_template, redirect, url_for, flash, request, send_file, abort
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from sqlalchemy import or_

from app import (
    DEFAULT_COLUMN_INDICES,
    column_letters_to_indices,
    compute_profit_table,
    gather_columns,
    read_statistics,
    save_processed_report,
)
from models import db, User, Seller, SellerReport, Product, APILog, ProductStock
from wildberries_api import WildberriesAPIError, list_cards
import json
import time
from wb_api_client import WildberriesAPIClient, WBAPIException, WBAuthException

# Настройка приложения
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'wb-seller-platform-secret-key-change-in-production')
BASE_DIR = Path(__file__).resolve().parent
DATA_ROOT = BASE_DIR / 'data'
DEFAULT_DB_PATH = DATA_ROOT / 'seller_platform.db'
DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
default_db_uri = os.environ.get('DATABASE_URL') or f"sqlite:///{DEFAULT_DB_PATH.as_posix()}"
app.config['SQLALCHEMY_DATABASE_URI'] = default_db_uri
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.jinja_env.filters['basename'] = lambda value: Path(value).name if value else ''

# Helper функции для работы с WB фотографиями
def wb_photo_url(nm_id: int, photo_index: int = 1, size: str = 'big') -> str:
    """
    Формирует URL фотографии товара Wildberries

    Args:
        nm_id: Артикул WB (nmID)
        photo_index: Номер фотографии (1, 2, 3, ...)
        size: Размер фотографии ('big', 'c246x328', 'c516x688', 'tm')

    Returns:
        Полный URL фотографии

    Формат URL: https://basket-{basket}.wbbasket.ru/vol{vol}/part{part}/{nmId}/images/{size}/{index}.webp
    """
    if not nm_id:
        return ''

    vol = nm_id // 100000
    part = nm_id // 1000

    # Определяем номер корзины по vol
    if vol >= 0 and vol <= 143:
        basket = '01'
    elif vol >= 144 and vol <= 287:
        basket = '02'
    elif vol >= 288 and vol <= 431:
        basket = '03'
    elif vol >= 432 and vol <= 719:
        basket = '04'
    elif vol >= 720 and vol <= 1007:
        basket = '05'
    elif vol >= 1008 and vol <= 1061:
        basket = '06'
    elif vol >= 1062 and vol <= 1115:
        basket = '07'
    elif vol >= 1116 and vol <= 1169:
        basket = '08'
    elif vol >= 1170 and vol <= 1313:
        basket = '09'
    elif vol >= 1314 and vol <= 1601:
        basket = '10'
    elif vol >= 1602 and vol <= 1655:
        basket = '11'
    elif vol >= 1656 and vol <= 1919:
        basket = '12'
    elif vol >= 1920 and vol <= 2045:
        basket = '13'
    elif vol >= 2046 and vol <= 2189:
        basket = '14'
    else:
        basket = '15'

    return f"https://basket-{basket}.wbbasket.ru/vol{vol}/part{part}/{nm_id}/images/{size}/{photo_index}.webp"


# Добавляем встроенные функции Python в контекст Jinja2
app.jinja_env.globals.update({
    'min': min,
    'max': max,
    'len': len,
    'int': int,
    'str': str,
    'wb_photo_url': wb_photo_url,
})

# Добавляем фильтр для парсинга JSON в шаблонах
app.jinja_env.filters['from_json'] = json.loads

# Инициализация расширений
db.init_app(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Пожалуйста, войдите в систему'
login_manager.login_message_category = 'info'


@app.after_request
def after_request(response):
    """Устанавливаем правильную кодировку UTF-8 для всех ответов"""
    if response.content_type and response.content_type.startswith('text/html'):
        response.content_type = 'text/html; charset=utf-8'
    return response


UPLOAD_ROOT = BASE_DIR / 'uploads'
PROCESSED_ROOT = BASE_DIR / 'processed'


def ensure_storage_roots() -> None:
    for folder in (UPLOAD_ROOT, PROCESSED_ROOT, DATA_ROOT):
        folder.mkdir(parents=True, exist_ok=True)


def get_seller_storage_dirs(seller_id: int) -> tuple[Path, Path]:
    ensure_storage_roots()
    upload_dir = UPLOAD_ROOT / f'seller_{seller_id}'
    processed_dir = PROCESSED_ROOT / f'seller_{seller_id}'
    upload_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir, processed_dir


def save_uploaded_file(file_storage, destination: Path) -> Optional[Path]:
    if not file_storage or not file_storage.filename:
        return None
    filename = Path(file_storage.filename).name
    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    safe_name = f'{timestamp}_{filename}'
    target = destination / safe_name
    file_storage.save(target)
    return target


def build_preview_data(
    statistics_path: Optional[str],
    selected_columns: Optional[List[str]],
) -> tuple[List[str], List[dict], List[str], Optional[str]]:
    if not statistics_path or not Path(statistics_path).exists():
        return [], [], selected_columns or [], None
    try:
        df = read_statistics(Path(statistics_path))
        available_columns = gather_columns(df)
        resolved_columns = selected_columns or column_letters_to_indices(
            df.columns,
            DEFAULT_COLUMN_INDICES,
        )
        preview_rows: List[dict] = []
        if resolved_columns:
            preview_rows = (
                df[resolved_columns]
                .head(10)
                .fillna('')
                .to_dict(orient='records')
            )
        return available_columns, preview_rows, resolved_columns, None
    except Exception as exc:
        return [], [], selected_columns or [], str(exc)


def get_latest_report(seller_id: int) -> Optional[SellerReport]:
    return (
        SellerReport.query.filter_by(seller_id=seller_id)
        .order_by(SellerReport.created_at.desc())
        .first()
    )


def summarise_card(card: Dict[str, Any]) -> Dict[str, Any]:
    """Подготовить ключевые поля карточки для отображения."""
    sizes = card.get('sizes') or []
    media_files = card.get('mediaFiles') or card.get('photos') or []
    barcode_count = 0
    for size in sizes:
        if isinstance(size, dict):
            barcode_count += len(size.get('skus') or [])

    return {
        'vendor_code': card.get('vendorCode') or card.get('supplierVendorCode'),
        'nm_id': card.get('nmID') or card.get('nmId'),
        'title': card.get('title') or card.get('subjectName') or card.get('name'),
        'brand': card.get('brand'),
        'subject': card.get('subjectName') or card.get('subjectID'),
        'updated_at': card.get('updatedAt') or card.get('updateAt') or card.get('modifiedAt'),
        'photo_count': len(media_files) if isinstance(media_files, list) else 0,
        'barcode_count': barcode_count,
        'card': card,
    }


@login_manager.user_loader
def load_user(user_id):
    """Загрузка пользователя для Flask-Login"""
    return User.query.get(int(user_id))


def admin_required(f):
    """Декоратор для проверки прав администратора"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('У вас нет прав для доступа к этой странице', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function


# ============= МАРШРУТЫ АВТОРИЗАЦИИ =============

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Страница входа в систему"""
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = request.form.get('remember', False)

        if not username or not password:
            flash('Введите имя пользователя и пароль', 'warning')
            return render_template('login.html')

        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            if not user.is_active:
                flash('Ваш аккаунт деактивирован. Обратитесь к администратору.', 'danger')
                return render_template('login.html')

            # Обновляем время последнего входа
            user.last_login = datetime.utcnow()
            db.session.commit()

            login_user(user, remember=remember)
            flash(f'Добро пожаловать, {user.username}!', 'success')

            # Перенаправление на запрошенную страницу или на дашборд
            next_page = request.args.get('next')
            if next_page and next_page.startswith('/'):
                return redirect(next_page)
            return redirect(url_for('dashboard'))
        else:
            flash('Неверное имя пользователя или пароль', 'danger')

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    """Выход из системы"""
    logout_user()
    flash('Вы вышли из системы', 'info')
    return redirect(url_for('login'))


# ============= ДАШБОРД ПРОДАВЦА =============

@app.route('/')
@app.route('/dashboard')
@login_required
def dashboard():
    """Главная страница - дашборд продавца"""
    latest_report = None
    summary = None
    latest_filename = None
    if current_user.seller:
        latest_report = get_latest_report(current_user.seller.id)
        if latest_report:
            summary = latest_report.summary or {}
            processed_path = Path(latest_report.processed_path)
            latest_filename = processed_path.name if processed_path.exists() else None

    return render_template(
        'dashboard.html',
        user=current_user,
        latest_report=latest_report,
        summary=summary,
        latest_filename=latest_filename,
    )


@app.route('/reports', methods=['GET', 'POST'])
@login_required
def reports():
    if not current_user.seller:
        if current_user.is_admin:
            flash('Создайте продавца в админ-панели и войдите под его учётной записью, чтобы работать с отчётами.', 'info')
            return redirect(url_for('admin_panel'))
        flash('Для работы с отчётами обратитесь к администратору.', 'warning')
        return redirect(url_for('dashboard'))

    seller = current_user.seller
    upload_dir, processed_dir = get_seller_storage_dirs(seller.id)

    latest_report = get_latest_report(seller.id)
    statistics_path = latest_report.statistics_path if latest_report else None
    price_path = latest_report.price_path if latest_report else None
    selected_columns: List[str] = list(latest_report.selected_columns or []) if latest_report else []

    if request.method == 'POST':
        selected_columns = request.form.getlist('columns') or selected_columns
        stat_file = request.files.get('statistics')
        price_file = request.files.get('prices')

        new_stat_path = save_uploaded_file(stat_file, upload_dir)
        new_price_path = save_uploaded_file(price_file, upload_dir)

        stat_path = new_stat_path or statistics_path
        price_path_effective = new_price_path or price_path

        if not stat_path or not Path(stat_path).exists():
            flash('Загрузите отчёт Wildberries (xlsx).', 'warning')
            return redirect(url_for('reports'))

        if not price_path_effective or not Path(price_path_effective).exists():
            flash('Загрузите прайс поставщика (csv).', 'warning')
            return redirect(url_for('reports'))

        try:
            df, _, summary = compute_profit_table(
                Path(stat_path),
                Path(price_path_effective),
                selected_columns or None,
            )
        except Exception as exc:
            flash(f'Ошибка обработки: {exc}', 'danger')
            return redirect(url_for('reports'))

        try:
            processed_path = save_processed_report(
                df,
                summary,
                output_dir=processed_dir,
            )
            report = SellerReport(
                seller_id=seller.id,
                statistics_path=str(stat_path),
                price_path=str(price_path_effective),
                processed_path=str(processed_path),
                selected_columns=selected_columns or [],
                summary=summary,
            )
            db.session.add(report)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            flash(f'Не удалось сохранить отчёт: {exc}', 'danger')
            return redirect(url_for('reports'))

        flash('Отчёт обновлён.', 'success')
        return redirect(url_for('reports'))

    available_columns, preview_rows, selected_columns, preview_error = build_preview_data(
        statistics_path,
        selected_columns,
    )
    if preview_error:
        flash(f'Не удалось подготовить предпросмотр: {preview_error}', 'warning')

    reports_history = (
        SellerReport.query.filter_by(seller_id=seller.id)
        .order_by(SellerReport.created_at.desc())
        .limit(10)
        .all()
    )

    return render_template(
        'reports.html',
        seller=seller,
        latest_report=latest_report,
        reports=reports_history,
        available_columns=available_columns,
        selected_columns=selected_columns,
        preview_rows=preview_rows,
        statistics_ready=bool(statistics_path and Path(statistics_path).exists()),
        price_ready=bool(price_path and Path(price_path).exists()),
        statistics_path=statistics_path,
        price_path=price_path,
    )




@app.route('/cards', methods=['GET'])
@login_required
def cards():
    seller: Optional[Seller] = None
    seller_options: List[Seller] = []
    selected_seller_id: Optional[int] = None

    if current_user.is_admin:
        seller_options = (
            Seller.query.join(User)
            .order_by(Seller.company_name.asc())
            .all()
        )
        selected_seller_id = request.args.get('seller_id', type=int)
        if selected_seller_id:
            seller = Seller.query.get(selected_seller_id)
            if not seller:
                flash('Продавец с указанным ID не найден.', 'warning')
    else:
        seller = current_user.seller
        if not seller:
            flash('Учетная запись продавца не привязана к текущему пользователю.', 'warning')
            return redirect(url_for('dashboard'))

    search = request.args.get('search', '').strip()
    updated_at = request.args.get('updated_at', '').strip() or None
    limit = request.args.get('limit', type=int) or 50
    limit = max(1, min(limit, 1_000))

    cards_payload: List[Dict[str, Any]] = []
    prepared_cards: List[Dict[str, Any]] = []
    cursor: Dict[str, Any] = {}
    api_error: Optional[str] = None
    additional_errors: List[str] = []

    if seller:
        if not seller.wb_api_key:
            api_error = 'Для выбранного продавца не указан API токен Wildberries.'
        else:
            try:
                response = list_cards(
                    seller.wb_api_key,
                    limit=limit,
                    search=search or None,
                    updated_at=updated_at,
                )
                cards_payload = response.get('cards') or []
                cursor = response.get('cursor') or {}
                prepared_cards = [summarise_card(card) for card in cards_payload]

                error_text = (response.get('errorText') or '').strip()
                if error_text:
                    additional_errors.append(error_text)
                for extra in response.get('additionalErrors') or []:
                    text_message = str(extra).strip()
                    if text_message:
                        additional_errors.append(text_message)
            except WildberriesAPIError as exc:
                api_error = str(exc)

    return render_template(
        'cards.html',
        seller=seller,
        seller_options=seller_options,
        selected_seller_id=selected_seller_id,
        cards=prepared_cards,
        raw_cards=cards_payload,
        cursor=cursor,
        api_error=api_error,
        additional_errors=additional_errors,
        search=search,
        limit=limit,
        updated_at=updated_at,
    )


@app.route('/reports/<int:report_id>/download')
@login_required
def download_report(report_id: int):
    if not current_user.seller:
        flash('Недостаточно прав для скачивания отчёта.', 'warning')
        return redirect(url_for('dashboard'))

    report = SellerReport.query.get_or_404(report_id)
    if report.seller_id != current_user.seller.id:
        abort(404)

    path = Path(report.processed_path)
    if not path.exists():
        flash('Файл отчёта не найден. Сформируйте новый отчёт.', 'warning')
        return redirect(url_for('reports'))

    return send_file(path, as_attachment=True, download_name=path.name)


# ============= АДМИН ПАНЕЛЬ =============

@app.route('/admin')
@login_required
@admin_required
def admin_panel():
    """Главная страница админ панели"""
    sellers = Seller.query.join(User).all()
    total_sellers = len(sellers)
    active_sellers = sum(1 for s in sellers if s.user.is_active)

    return render_template('admin.html',
                         sellers=sellers,
                         total_sellers=total_sellers,
                         active_sellers=active_sellers)


@app.route('/admin/sellers/add', methods=['GET', 'POST'])
@login_required
@admin_required
def add_seller():
    """Добавление нового продавца"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        company_name = request.form.get('company_name', '').strip()
        contact_phone = request.form.get('contact_phone', '').strip()
        wb_seller_id = request.form.get('wb_seller_id', '').strip()
        wb_api_key = request.form.get('wb_api_key', '').strip()
        notes = request.form.get('notes', '').strip()

        # Валидация
        if not all([username, email, password, company_name]):
            flash('Заполните все обязательные поля', 'warning')
            return render_template('add_seller.html')

        # Проверка на существующего пользователя
        if User.query.filter_by(username=username).first():
            flash(f'Пользователь с именем "{username}" уже существует', 'danger')
            return render_template('add_seller.html')

        if User.query.filter_by(email=email).first():
            flash(f'Пользователь с email "{email}" уже существует', 'danger')
            return render_template('add_seller.html')

        try:
            # Создаем пользователя
            user = User(
                username=username,
                email=email,
                is_admin=False,
                is_active=True
            )
            user.set_password(password)
            db.session.add(user)
            db.session.flush()  # Получаем ID пользователя

            # Создаем продавца
            seller = Seller(
                user_id=user.id,
                company_name=company_name,
                contact_phone=contact_phone,
                wb_seller_id=wb_seller_id,
                wb_api_key=wb_api_key or None,
                notes=notes
            )
            db.session.add(seller)
            db.session.commit()

            flash(f'Продавец "{company_name}" успешно добавлен', 'success')
            return redirect(url_for('admin_panel'))

        except Exception as e:
            db.session.rollback()
            flash(f'Ошибка при создании продавца: {str(e)}', 'danger')
            return render_template('add_seller.html')

    return render_template('add_seller.html')


@app.route('/admin/sellers/<int:seller_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_seller(seller_id):
    """Редактирование продавца"""
    seller = Seller.query.get_or_404(seller_id)

    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        company_name = request.form.get('company_name', '').strip()
        contact_phone = request.form.get('contact_phone', '').strip()
        wb_seller_id = request.form.get('wb_seller_id', '').strip()
        wb_api_key = request.form.get('wb_api_key', '').strip()
        notes = request.form.get('notes', '').strip()
        is_active = request.form.get('is_active') == 'on'
        new_password = request.form.get('new_password', '').strip()

        if not all([email, company_name]):
            flash('Заполните все обязательные поля', 'warning')
            return render_template('edit_seller.html', seller=seller)

        # Проверка email на уникальность
        existing_user = User.query.filter_by(email=email).first()
        if existing_user and existing_user.id != seller.user_id:
            flash(f'Email "{email}" уже используется', 'danger')
            return render_template('edit_seller.html', seller=seller)

        try:
            # Обновляем данные пользователя
            seller.user.email = email
            seller.user.is_active = is_active

            if new_password:
                seller.user.set_password(new_password)

            # Обновляем данные продавца
            seller.company_name = company_name
            seller.contact_phone = contact_phone
            seller.wb_seller_id = wb_seller_id
            seller.wb_api_key = wb_api_key or None
            seller.notes = notes

            db.session.commit()
            flash(f'Продавец "{company_name}" успешно обновлен', 'success')
            return redirect(url_for('admin_panel'))

        except Exception as e:
            db.session.rollback()
            flash(f'Ошибка при обновлении: {str(e)}', 'danger')

    return render_template('edit_seller.html', seller=seller)


@app.route('/admin/sellers/<int:seller_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_seller(seller_id):
    """Удаление продавца"""
    seller = Seller.query.get_or_404(seller_id)
    company_name = seller.company_name

    try:
        # Удаляем пользователя (продавец удалится автоматически через cascade)
        db.session.delete(seller.user)
        db.session.commit()
        flash(f'Продавец "{company_name}" успешно удален', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка при удалении: {str(e)}', 'danger')

    return redirect(url_for('admin_panel'))


# ============= НАСТРОЙКИ API =============

@app.route('/api-settings', methods=['GET', 'POST'])
@login_required
def api_settings():
    """Настройка API ключей Wildberries"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'danger')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        wb_api_key = request.form.get('wb_api_key', '').strip()
        force_save = request.form.get('force_save') == 'on'

        if not wb_api_key:
            flash('Введите API ключ', 'warning')
            return render_template('api_settings.html', seller=current_user.seller)

        # Проверить валидность ключа (если не force_save)
        if not force_save:
            try:
                app.logger.info(f"Testing WB API connection for seller {current_user.seller.id}")
                with WildberriesAPIClient(wb_api_key, sandbox=False, timeout=10) as client:
                    if client.test_connection():
                        # Ключ валиден — сохраняем
                        current_user.seller.wb_api_key = wb_api_key
                        current_user.seller.api_sync_status = 'ready'
                        db.session.commit()

                        flash('API ключ успешно сохранен и проверен ✓', 'success')
                        return redirect(url_for('dashboard'))
                    else:
                        # Тест не прошёл, но сохраняем с warning
                        app.logger.warning(f"API test failed for seller {current_user.seller.id}")
                        current_user.seller.wb_api_key = wb_api_key
                        current_user.seller.api_sync_status = 'error'
                        db.session.commit()

                        flash('API ключ сохранен, но проверка соединения не удалась. '
                              'Проверьте правильность ключа и наличие доступа к API Wildberries.', 'warning')
                        return redirect(url_for('dashboard'))

            except WBAuthException as e:
                app.logger.error(f"Auth error for seller {current_user.seller.id}: {e}")
                # Сохраняем с ошибкой
                current_user.seller.wb_api_key = wb_api_key
                current_user.seller.api_sync_status = 'auth_error'
                db.session.commit()
                flash(f'Ошибка авторизации: {str(e)}. Ключ сохранен, но требует проверки.', 'warning')
                return redirect(url_for('dashboard'))

            except WBAPIException as e:
                app.logger.error(f"API error for seller {current_user.seller.id}: {e}")
                # Сохраняем с ошибкой
                current_user.seller.wb_api_key = wb_api_key
                current_user.seller.api_sync_status = 'api_error'
                db.session.commit()
                flash(f'Ошибка API: {str(e)}. Ключ сохранен, возможны проблемы с соединением.', 'warning')
                return redirect(url_for('dashboard'))

            except Exception as e:
                app.logger.exception(f"Unexpected error testing API for seller {current_user.seller.id}: {e}")
                # Сохраняем с ошибкой
                current_user.seller.wb_api_key = wb_api_key
                current_user.seller.api_sync_status = 'unknown_error'
                db.session.commit()
                flash(f'Не удалось проверить ключ: {str(e)}. Ключ сохранен для повторной попытки.', 'warning')
                return redirect(url_for('dashboard'))
        else:
            # Принудительное сохранение без проверки
            app.logger.info(f"Force saving API key for seller {current_user.seller.id}")
            current_user.seller.wb_api_key = wb_api_key
            current_user.seller.api_sync_status = 'unchecked'
            db.session.commit()
            flash('API ключ сохранен без проверки', 'info')
            return redirect(url_for('dashboard'))

    return render_template('api_settings.html', seller=current_user.seller)


# ============= КАРТОЧКИ ТОВАРОВ =============

@app.route('/products')
@login_required
def products_list():
    """Список карточек товаров с пагинацией"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'danger')
        return redirect(url_for('dashboard'))

    try:
        # Параметры пагинации
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 50, type=int)
        per_page = min(per_page, 100)  # Максимум 100 на странице

        # Фильтры
        search = request.args.get('search', '').strip()
        active_only = request.args.get('active_only', type=bool)

        # Построение запроса
        query = Product.query.filter_by(seller_id=current_user.seller.id)

        if active_only:
            query = query.filter_by(is_active=True)

        if search:
            # Поиск по артикулу, названию или бренду
            search_filter = or_(
                Product.vendor_code.ilike(f'%{search}%'),
                Product.title.ilike(f'%{search}%'),
                Product.brand.ilike(f'%{search}%'),
                Product.nm_id.cast(db.String).ilike(f'%{search}%')
            )
            query = query.filter(search_filter)

        # Сортировка по дате обновления (новые первыми)
        query = query.order_by(Product.updated_at.desc())

        # Пагинация
        pagination = query.paginate(
            page=page,
            per_page=per_page,
            error_out=False
        )

        products = pagination.items
        total_products = current_user.seller.products.count()
        active_products = current_user.seller.products.filter_by(is_active=True).count()

        return render_template(
            'products.html',
            products=products,
            pagination=pagination,
            total_products=total_products,
            active_products=active_products,
            search=search,
            active_only=active_only,
            seller=current_user.seller
        )
    except Exception as e:
        app.logger.exception(f"Error in products_list: {e}")
        flash(f'Ошибка при загрузке карточек товаров: {str(e)}', 'danger')
        return redirect(url_for('dashboard'))


@app.route('/products/sync', methods=['POST'])
@login_required
def sync_products():
    """Синхронизация карточек товаров через API WB"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'danger')
        return redirect(url_for('dashboard'))

    if not current_user.seller.has_valid_api_key():
        flash('API ключ Wildberries не настроен. Настройте его в разделе "Настройки API".', 'warning')
        return redirect(url_for('api_settings'))

    try:
        current_user.seller.api_sync_status = 'syncing'
        db.session.commit()

        start_time = time.time()

        with WildberriesAPIClient(current_user.seller.wb_api_key) as client:
            # Получаем все карточки
            app.logger.info(f"🔄 Начинаем загрузку карточек для seller_id={current_user.seller.id}")
            all_cards = client.get_all_cards(batch_size=100)
            app.logger.info(f"✅ Получено {len(all_cards)} карточек из WB API")

            # Логируем структуру первой карточки для отладки
            if all_cards:
                first_card = all_cards[0]
                app.logger.info(f"📦 Первая карточка: nmID={first_card.get('nmID')}, keys={list(first_card.keys())[:10]}")
                app.logger.info(f"📷 mediaFiles в первой карточке: {len(first_card.get('mediaFiles', []))}")
                app.logger.info(f"📷 photos в первой карточке: {len(first_card.get('photos', []))}")
            else:
                app.logger.warning("⚠️ API вернул пустой список карточек!")

            # Статистика
            created_count = 0
            updated_count = 0

            for card_data in all_cards:
                nm_id = card_data.get('nmID')
                if not nm_id:
                    continue

                # Ищем существующую карточку
                product = Product.query.filter_by(
                    seller_id=current_user.seller.id,
                    nm_id=nm_id
                ).first()

                # Извлекаем данные из API
                vendor_code = card_data.get('vendorCode', '')
                title = card_data.get('title', '')
                brand = card_data.get('brand', '')
                object_name = card_data.get('object', '')
                description = card_data.get('description', '')

                # Характеристики
                characteristics = card_data.get('characteristics', [])
                characteristics_json = json.dumps(characteristics, ensure_ascii=False) if characteristics else None

                # Габариты
                dimensions = card_data.get('dimensions', {})
                dimensions_json = json.dumps(dimensions, ensure_ascii=False) if dimensions else None

                # Медиа - подсчитываем количество фотографий
                # WB хранит фото на CDN, генерируем URL динамически в шаблонах
                media = card_data.get('mediaFiles', [])

                # Пробуем несколько способов подсчета фотографий
                # Вариант 1: по полю big в mediaFiles (старый формат)
                photo_count_v1 = len([m for m in media if m.get('big') and m.get('mediaType') != 'video'])

                # Вариант 2: просто все медиафайлы кроме видео
                photo_count_v2 = len([m for m in media if m.get('mediaType') != 'video'])

                # Вариант 3: photos field может содержать количество
                photos_field = card_data.get('photos', [])
                photo_count_v3 = len(photos_field) if photos_field else 0

                # Используем максимальное значение
                photo_count = max(photo_count_v1, photo_count_v2, photo_count_v3)

                # Если фоток нет, пробуем альтернативный подход - просто предполагаем что есть до 10 фото
                if photo_count == 0 and card_data.get('mediaFiles'):
                    # Предполагаем что есть хотя бы несколько фото
                    photo_count = len(media) if media else 0

                # Логируем для первых 3 товаров для отладки
                if created_count + updated_count < 3:
                    app.logger.info(f"Product {nm_id}: mediaFiles={len(media)}, photos_field={len(photos_field) if photos_field else 0}, photo_count={photo_count}")

                # Сохраняем список индексов фотографий [1, 2, 3, ...]
                photo_indices = list(range(1, photo_count + 1)) if photo_count > 0 else []
                photos_json = json.dumps(photo_indices) if photo_indices else None

                # Видео обрабатываем отдельно
                video_media = next((m for m in media if m.get('mediaType') == 'video'), None)
                video = video_media.get('big') if video_media else None

                # Размеры
                sizes = card_data.get('sizes', [])
                sizes_json = json.dumps(sizes, ensure_ascii=False) if sizes else None

                if product:
                    # Обновление существующей карточки
                    product.vendor_code = vendor_code
                    product.title = title
                    product.brand = brand
                    product.object_name = object_name
                    product.description = description
                    product.characteristics_json = characteristics_json
                    product.dimensions_json = dimensions_json
                    product.photos_json = photos_json
                    product.video_url = video
                    product.sizes_json = sizes_json
                    product.last_sync = datetime.utcnow()
                    product.is_active = True
                    updated_count += 1
                else:
                    # Создание новой карточки
                    product = Product(
                        seller_id=current_user.seller.id,
                        nm_id=nm_id,
                        imt_id=card_data.get('imtID'),
                        vendor_code=vendor_code,
                        title=title,
                        brand=brand,
                        object_name=object_name,
                        description=description,
                        supplier_vendor_code=card_data.get('supplierVendorCode', ''),
                        characteristics_json=characteristics_json,
                        dimensions_json=dimensions_json,
                        photos_json=photos_json,
                        video_url=video,
                        sizes_json=sizes_json,
                        last_sync=datetime.utcnow(),
                        is_active=True
                    )
                    db.session.add(product)
                    created_count += 1

            # Сохраняем все изменения
            db.session.commit()

            app.logger.info(f"💾 Сохранено в БД: {created_count} новых, {updated_count} обновлено")

            # Обновляем статус синхронизации
            current_user.seller.api_last_sync = datetime.utcnow()
            current_user.seller.api_sync_status = 'success'
            db.session.commit()

            elapsed = time.time() - start_time

            # Логируем успешный запрос
            APILog.log_request(
                seller_id=current_user.seller.id,
                endpoint='/content/v2/get/cards/list',
                method='POST',  # Исправлено на POST
                status_code=200,
                response_time=elapsed,
                success=True
            )

            app.logger.info(f"✅ Синхронизация завершена успешно за {elapsed:.1f}с")

            flash(
                f'Синхронизация завершена за {elapsed:.1f}с: '
                f'{created_count} новых, {updated_count} обновлено',
                'success'
            )

    except WBAuthException as e:
        current_user.seller.api_sync_status = 'auth_error'
        db.session.commit()

        app.logger.error(f"❌ Ошибка авторизации: {str(e)}")

        APILog.log_request(
            seller_id=current_user.seller.id,
            endpoint='/content/v2/get/cards/list',
            method='POST',
            status_code=401,
            response_time=0,
            success=False,
            error_message=f'Authentication failed: {str(e)}'
        )

        flash('Ошибка авторизации. Проверьте API ключ.', 'danger')

    except WBAPIException as e:
        current_user.seller.api_sync_status = 'error'
        db.session.commit()

        app.logger.error(f"❌ Ошибка WB API: {str(e)}")

        APILog.log_request(
            seller_id=current_user.seller.id,
            endpoint='/content/v2/get/cards/list',
            method='POST',
            status_code=500,
            response_time=0,
            success=False,
            error_message=str(e)
        )

        flash(f'Ошибка API WB: {str(e)}', 'danger')

    except Exception as e:
        current_user.seller.api_sync_status = 'error'
        db.session.commit()

        app.logger.exception(f"❌ Неожиданная ошибка синхронизации: {str(e)}")

        flash(f'Ошибка синхронизации: {str(e)}', 'danger')

    return redirect(url_for('products_list'))


@app.route('/products/sync-stocks', methods=['POST'])
@login_required
def sync_warehouse_stocks():
    """Синхронизация остатков по складам через API WB"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'danger')
        return redirect(url_for('dashboard'))

    if not current_user.seller.has_valid_api_key():
        flash('API ключ Wildberries не настроен. Настройте его в разделе "Настройки API".', 'warning')
        return redirect(url_for('api_settings'))

    try:
        start_time = time.time()

        with WildberriesAPIClient(current_user.seller.wb_api_key) as client:
            # Получаем все остатки по складам
            app.logger.info(f"🔄 Начинаем загрузку остатков по складам для seller_id={current_user.seller.id}")
            all_stocks = client.get_all_warehouse_stocks(batch_size=1000)
            app.logger.info(f"✅ Получено {len(all_stocks)} записей об остатках из WB API")

            # Статистика
            created_count = 0
            updated_count = 0

            # Обрабатываем каждую запись об остатках
            for stock_data in all_stocks:
                nm_id = stock_data.get('nmId')
                warehouse_id = stock_data.get('warehouseId')

                if not nm_id:
                    continue

                # Находим товар по nm_id
                product = Product.query.filter_by(
                    seller_id=current_user.seller.id,
                    nm_id=nm_id
                ).first()

                if not product:
                    # Товар не найден - пропускаем (возможно не синхронизированы карточки)
                    continue

                # Ищем существующую запись об остатках для этого товара и склада
                stock = ProductStock.query.filter_by(
                    product_id=product.id,
                    warehouse_id=warehouse_id
                ).first()

                warehouse_name = stock_data.get('warehouseName', '')
                quantity = stock_data.get('quantity', 0)
                quantity_full = stock_data.get('quantityFull', 0)
                in_way_to_client = stock_data.get('inWayToClient', 0)
                in_way_from_client = stock_data.get('inWayFromClient', 0)

                if stock:
                    # Обновляем существующую запись
                    stock.warehouse_name = warehouse_name
                    stock.quantity = quantity
                    stock.quantity_full = quantity_full
                    stock.in_way_to_client = in_way_to_client
                    stock.in_way_from_client = in_way_from_client
                    stock.updated_at = datetime.utcnow()
                    updated_count += 1
                else:
                    # Создаем новую запись
                    stock = ProductStock(
                        product_id=product.id,
                        warehouse_id=warehouse_id,
                        warehouse_name=warehouse_name,
                        quantity=quantity,
                        quantity_full=quantity_full,
                        in_way_to_client=in_way_to_client,
                        in_way_from_client=in_way_from_client
                    )
                    db.session.add(stock)
                    created_count += 1

            # Сохраняем все изменения
            db.session.commit()

            app.logger.info(f"💾 Сохранено остатков в БД: {created_count} новых, {updated_count} обновлено")

            elapsed = time.time() - start_time

            # Логируем успешный запрос
            APILog.log_request(
                seller_id=current_user.seller.id,
                endpoint='/api/v3/stocks/0',
                method='POST',
                status_code=200,
                response_time=elapsed,
                success=True
            )

            app.logger.info(f"✅ Синхронизация остатков завершена успешно за {elapsed:.1f}с")

            flash(
                f'Синхронизация остатков завершена за {elapsed:.1f}с: '
                f'{created_count} новых, {updated_count} обновлено',
                'success'
            )

    except WBAuthException as e:
        app.logger.error(f"❌ Ошибка авторизации при загрузке остатков: {str(e)}")

        APILog.log_request(
            seller_id=current_user.seller.id,
            endpoint='/api/v3/stocks/0',
            method='POST',
            status_code=401,
            response_time=0,
            success=False,
            error_message=f'Authentication failed: {str(e)}'
        )

        flash('Ошибка авторизации. Проверьте API ключ.', 'danger')

    except WBAPIException as e:
        app.logger.error(f"❌ Ошибка WB API при загрузке остатков: {str(e)}")

        APILog.log_request(
            seller_id=current_user.seller.id,
            endpoint='/api/v3/stocks/0',
            method='POST',
            status_code=500,
            response_time=0,
            success=False,
            error_message=str(e)
        )

        flash(f'Ошибка API WB: {str(e)}', 'danger')

    except Exception as e:
        app.logger.exception(f"❌ Неожиданная ошибка синхронизации остатков: {str(e)}")
        flash(f'Ошибка синхронизации остатков: {str(e)}', 'danger')

    return redirect(url_for('products_list'))


@app.route('/products/<int:product_id>')
@login_required
def product_detail(product_id):
    """Детальная информация о товаре"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'danger')
        return redirect(url_for('dashboard'))

    product = Product.query.get_or_404(product_id)

    # Проверка доступа
    if product.seller_id != current_user.seller.id:
        flash('У вас нет доступа к этому товару', 'danger')
        return redirect(url_for('products_list'))

    # Парсим JSON данные
    photos = json.loads(product.photos_json) if product.photos_json else []
    sizes = json.loads(product.sizes_json) if product.sizes_json else []
    characteristics = json.loads(product.characteristics_json) if product.characteristics_json else []
    dimensions = json.loads(product.dimensions_json) if product.dimensions_json else {}

    # Получаем остатки по складам для этого товара
    warehouse_stocks = ProductStock.query.filter_by(product_id=product.id).all()

    # Вычисляем общие остатки
    total_quantity = sum(stock.quantity for stock in warehouse_stocks)
    total_quantity_full = sum(stock.quantity_full for stock in warehouse_stocks)

    return render_template(
        'product_detail.html',
        product=product,
        photos=photos,
        sizes=sizes,
        characteristics=characteristics,
        dimensions=dimensions,
        warehouse_stocks=warehouse_stocks,
        total_quantity=total_quantity,
        total_quantity_full=total_quantity_full
    )


# ============= API ЛОГИ =============

@app.route('/api-logs')
@login_required
def api_logs():
    """Просмотр логов API запросов"""
    if not current_user.seller:
        flash('У вас нет профиля продавца', 'danger')
        return redirect(url_for('dashboard'))

    page = request.args.get('page', 1, type=int)
    per_page = 50

    # Логи для текущего продавца
    pagination = APILog.query.filter_by(
        seller_id=current_user.seller.id
    ).order_by(
        APILog.created_at.desc()
    ).paginate(
        page=page,
        per_page=per_page,
        error_out=False
    )

    logs = pagination.items

    # Статистика
    total_requests = current_user.seller.api_logs.count()
    failed_requests = current_user.seller.api_logs.filter_by(success=False).count()
    success_rate = ((total_requests - failed_requests) / total_requests * 100) if total_requests > 0 else 0

    return render_template(
        'api_logs.html',
        logs=logs,
        pagination=pagination,
        total_requests=total_requests,
        failed_requests=failed_requests,
        success_rate=success_rate
    )


# ============= ИНИЦИАЛИЗАЦИЯ БД =============

@app.cli.command()
def init_db():
    """Инициализация базы данных"""
    db.create_all()
    print('База данных инициализирована')


@app.cli.command()
def create_admin():
    """Создание администратора"""
    username = input('Введите имя пользователя: ')
    email = input('Введите email: ')
    password = input('Введите пароль: ')

    if User.query.filter_by(username=username).first():
        print(f'Пользователь {username} уже существует')
        return

    admin = User(
        username=username,
        email=email,
        is_admin=True,
        is_active=True
    )
    admin.set_password(password)

    db.session.add(admin)
    db.session.commit()

    print(f'Администратор {username} успешно создан')


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5001)))
